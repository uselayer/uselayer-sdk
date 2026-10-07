"""Best-venue orders: one order, placed on whichever venue of a pair is cheaper for its size, after fees.

    r = client.buy_best(match, "yes", 10, max_price=0.55)
    r.order                 # the order, sent through client.send() like any other
    r.why.reason            # e.g. "Kalshi is $0.06 cheaper all-in for 10 contracts: $4.35 vs $4.41 on Polymarket US."
    for v in r.why.venues:  # each venue's all-in cost, or why it was skipped
        v.venue, v.all_in, v.skip, v.detail

    client.preview_best(match, "yes", 10)   # the same comparison; sends nothing
    client.buy_best(match, "yes", spend=50) # $50 on YES, on the venue where it wins more

A pair is two markets that are the same bet, usually a Kalshi ↔ Polymarket US match from
``client.matches()``. ``side`` is the outcome you trade, the same on both: each market id in a match
already names the matched outcome (a Polymarket US market with two named sides is ``<slug>:long`` or
``<slug>:short``, and YES on ``<slug>:short`` is the short side), so YES on one leg pays when YES on
the other does.

For each leg, the venue's book is walked for ``size`` contracts, level by level from the best price,
and each level is billed with that venue's fee, as a paper fill would be (:func:`~uselayer.fill.estimate_fill`).
A buy may walk up to ``max_price`` and never more than the price collar above the best ask, the
most :meth:`Client.send` lets a buy pay; a sell, down to ``min_price`` and the collar below the best
bid. The all-in cost of a buy is what the contracts cost plus the fees; of a sell, what they pay
minus the fees. The cheaper buy (or the better-paying sell) wins; on a tie, the venue with more
contracts on offer at or better than its limit price; then the pair's first leg.

A buy can be given in dollars instead (``spend=50``), the way people bet: each venue then gets its
own size, the most whole contracts whose cost plus fees fits in ``spend`` within the limit, found
on the same one read of its book. The venue whose contracts pay more if you're right ($1 each) wins
(``wins_more``); the same number on both is compared as above. A venue where ``spend`` doesn't buy
the market's smallest order is skipped (``spend_too_small``).

The winning order is a limit at the walk's deepest price, immediate-or-cancel by default, sent
through :meth:`Client.send`: every guardrail, the price collar and the kill switch apply to it. The
comparison is a snapshot of both books at the moment you call it. A book can move before the order
arrives; the limit price (bounded by the collar and ``max_price`` / ``min_price``) caps what the
order can pay, and an immediate-or-cancel order fills only what's still there at that price.

A leg is skipped, with a reason, when its venue is switched off in this release (``switched_off``),
has no key for this mode (``no_key``), isn't allowed by the ``venues``, ``markets`` or ``actions``
rules (``not_allowed``), its market is unknown or closed (``not_found``, ``market_closed``), it has
no fresh book (``no_book``, ``stale_book``), nobody is on the other side (``no_offers``), the best
price is past your limit (``above_max_price``, ``below_min_price``), the book can't fill the size
within the limit (``not_enough_size``), the order breaks the market's tick or minimum size
(``invalid_order``), ``spend`` is too little for one order there (``spend_too_small``), a sell is for more than this account holds there (``not_held``: prediction
markets don't let you sell what you don't hold), or the venue didn't answer (``unavailable``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from . import _switches
from .books import Book
from .errors import VENUE_NAMES, VenueError
from .fill import EstimatedFill, estimate_fill
from .guardrails import Allowed, Context
from .orders import Order
from .paper import check_order_against_market
from .portfolio import held
from .trading import _ticks, legs_of
from .venues.base import MarketInfo

if TYPE_CHECKING:
    from .client import Client, Preview

Action = Literal["buy", "sell"]
Tif = Literal["ioc", "fok"]


@dataclass(frozen=True)
class VenueCost:
    """One venue's side of the comparison: what ``size`` contracts would cost there all-in, or why it was skipped.

    ``all_in``: a buy's cost plus fees, or a sell's proceeds minus fees, in dollars. ``limit_price``:
    the deepest level the walk reached (the order's limit if this venue wins). ``cap``: the furthest
    the walk could go, set by ``max_price`` / ``min_price`` or the price collar (``capped_by``).
    ``size_at_limit``: contracts on offer at or better than ``limit_price``. ``skip`` and ``detail``
    are set when the venue was left out.
    """

    venue: str
    market: str
    side: str
    action: Action
    size: float
    skip: str | None = None
    detail: str | None = None
    best_price: float | None = None
    cap: float | None = None
    capped_by: str | None = None
    limit_price: float | None = None
    avg_price: float | None = None
    cost: float | None = None
    fees: float | None = None
    all_in: float | None = None
    all_in_per_contract: float | None = None
    size_at_limit: float | None = None
    fills: tuple[EstimatedFill, ...] = ()
    book_as_of: datetime | None = None

    @property
    def ok(self) -> bool:
        """This venue can take the whole order within its limit."""
        return self.skip is None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("fills", "book_as_of")}
        d["ok"] = self.ok
        d["fills"] = [f.to_dict() for f in self.fills]
        d["book_as_of"] = self.book_as_of.isoformat() if self.book_as_of else None
        return d


@dataclass(frozen=True)
class BestVenue:
    """Why the order went where it did: every venue's all-in cost (or skip reason) and the winner.

    ``chosen`` is ``None`` when no venue could take the order. ``reason_code`` is one of
    ``cheaper`` (a buy), ``pays_more`` (a sell), ``wins_more`` (a buy by ``spend``: the same money
    buys more contracts there), ``tie_more_size``, ``tie_first_listed``, ``only_venue`` or
    ``no_venue``; ``reason`` says the same in words. ``saving`` is how much better the winner is than
    the runner-up, in dollars, when both could take it; for ``wins_more``, how much more it pays if
    you're right.

    ``spend`` is the amount a buy by dollars was given (``None`` for a buy or sell by contracts).
    Then each venue's ``size`` is the most whole contracts that amount buys there, and ``size`` here
    is the chosen venue's (``None`` when no venue could take it).
    """

    action: Action
    side: str
    size: float | None
    limit: float | None
    venues: tuple[VenueCost, ...]
    chosen: VenueCost | None
    reason_code: str
    reason: str
    saving: float | None
    as_of: datetime
    spend: float | None = None

    @property
    def venue(self) -> str | None:
        """The chosen venue's name, or ``None``."""
        return self.chosen.venue if self.chosen else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "side": self.side,
            "size": self.size,
            "spend": self.spend,
            "limit": self.limit,
            "venue": self.venue,
            "market": self.chosen.market if self.chosen else None,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "saving": self.saving,
            "venues": [v.to_dict() for v in self.venues],
            "as_of": self.as_of.isoformat(),
        }


@dataclass(frozen=True)
class BestOrder:
    """What :meth:`Client.buy_best`, :meth:`Client.sell_best` or :meth:`Client.preview_best` did.

    ``order``: the order as sent (``sent=True``), or the one that would be sent (preview). ``why``:
    the comparison. ``preview``: the chosen order's :class:`~uselayer.client.Preview` (fill, fees,
    every rule's decision), from ``preview_best()`` only.
    """

    order: Order | None
    why: BestVenue
    sent: bool
    preview: Preview | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "order": self.order.to_dict() if self.order else None,
            "why": self.why.to_dict(),
            "preview": self.preview.to_dict() if self.preview else None,
        }

    def __str__(self) -> str:
        return json.dumps(self.to_dict(), indent=1)


# ---- one venue ---------------------------------------------------------------------------------


def _usd(x: float) -> str:
    return f"${x:,.2f}" if abs(round(x, 2) - x) < 1e-9 else f"${x:,.4f}"


def _held(client: Client, venue: str, market: str, side: str) -> float:
    """Contracts of this side held on this venue: the venue's own count live, the store's otherwise."""
    if client.mode == "live":
        return round(
            sum(
                p.contracts
                for p in client._live[venue].positions()
                if (p.market, p.side) == (market, side) and not p.settled
            ),
            6,
        )
    store = client._store
    return held(store.fills(), venue, market, side, store.settlements())


def _ready(
    client: Client, venue: str, market: str, side: str, action: Action, size: float
) -> VenueCost | MarketInfo:
    """Everything that can rule a venue out before its book is read: the market's info, or why it's skipped."""

    def skip(code: str, detail: str) -> VenueCost:
        return VenueCost(venue, market, side, action, size, skip=code, detail=detail)

    name = VENUE_NAMES.get(venue, venue)
    # Switched off, or no key for this mode.
    if client.mode == "live":
        if not _switches.TRADING.get(venue, False):
            return skip("switched_off", f"Live {name} orders are switched off in this release.")
        if venue not in client._live:
            return skip("no_key", f"No {name} key was given, so this client can't trade there.")
    else:
        try:
            client._check_venue(venue)
        except VenueError as e:
            code = "switched_off" if e.code == "venue_switched_off" else "no_key"
            return skip(code, e.message)
    # The venues / markets / actions rules: the same check send() makes.
    probe = Order(venue=venue, market=market, side=side, action=action, price=0.5, size=size)
    for r in client._guard.rules:
        if isinstance(r, Allowed):
            d = r.check(probe, Context(now=client._now()))
            if d.result != "allow":
                return skip("not_allowed", d.reason or "Not allowed by the rules.")
    try:
        info = client.market(market, venue=venue)
    except VenueError as e:
        return skip("not_found" if e.code == "not_found" else "unavailable", e.message)
    if action == "buy" and not info.open:
        return skip("market_closed", f"{market} isn't open for trading ({info.status}).")
    return info


def _cost(
    client: Client,
    venue: str,
    market: str,
    side: str,
    action: Action,
    size: float,
    limit: float | None,
    info: MarketInfo,
    book: Book | VenueError,
) -> VenueCost:
    """Walk one venue's book for ``size``, within the limit and the collar, and price it with the venue's fees."""
    base = VenueCost(venue, market, side, action, size)

    def skip(code: str, detail: str, **kw: Any) -> VenueCost:
        return VenueCost(venue, market, side, action, size, skip=code, detail=detail, **kw)

    name = VENUE_NAMES.get(venue, venue)
    probe = Order(venue=venue, market=market, side=side, action=action, price=0.5, size=size)
    if isinstance(book, VenueError):
        if client.mode == "backtest" and book.code == "stale_quote":
            return skip("no_book", book.message)
        return skip("unavailable", book.message)
    now = client._now()
    max_age = client.rules.max_quote_age_s
    if action == "buy" and book.age_s(now) > max_age:
        return skip(
            "stale_book",
            f"The book is {book.age_s(now):.0f}s old; max_quote_age_s is {max_age:g}.",
            book_as_of=book.as_of,
        )
    ob = book.outcome(side)  # type: ignore[arg-type]
    collar = client.rules.price_collar
    tick = info.tick_size
    if action == "buy":
        best = ob.best_ask
        if best is None:
            return skip(
                "no_offers", f"Nobody is selling {side.upper()} on {name} right now.", book_as_of=book.as_of
            )
        collar_cap = best.price + collar
        capped_by = "price_collar" if limit is None or collar_cap <= limit + 1e-9 else "max_price"
        cap = _ticks(collar_cap if capped_by == "price_collar" else limit, tick, up=False)  # type: ignore[arg-type]
        if best.price > cap + 1e-9:
            return skip(
                "above_max_price",
                f"The best ask is {best.price}, above max_price {limit}.",
                best_price=best.price,
                book_as_of=book.as_of,
            )
    else:
        best = ob.best_bid
        if best is None:
            return skip(
                "no_offers", f"Nobody is buying {side.upper()} on {name} right now.", book_as_of=book.as_of
            )
        collar_floor = best.price - collar
        capped_by = "price_collar" if limit is None or collar_floor >= limit - 1e-9 else "min_price"
        cap = _ticks(collar_floor if capped_by == "price_collar" else limit, tick, up=True)  # type: ignore[arg-type]
        cap = max(cap, tick)
        if best.price < cap - 1e-9:
            return skip(
                "below_min_price",
                f"The best bid is {best.price}, below min_price {limit}.",
                best_price=best.price,
                book_as_of=book.as_of,
            )
    priced = {"best_price": best.price, "cap": cap, "capped_by": capped_by, "book_as_of": book.as_of}
    if action == "sell":
        try:
            have = _held(client, venue, market, side)
        except VenueError as e:
            return skip("unavailable", e.message, **priced)
        if have < size - 1e-9:
            return skip(
                "not_held",
                f"This account holds {have:g} {side.upper()} on {name}; selling {size:g} needs that many.",
                **priced,
            )
    walk = estimate_fill(
        probe.model_copy(update={"price": cap, "tif": "ioc"}),
        book,
        client._settings(venue, market),
        at=now,
    )
    if walk.filled < size - 1e-9:
        bound = {"price_collar": f"the price collar ({collar:g} from the best price)"}.get(
            capped_by, f"{capped_by} {limit}"
        )
        return skip(
            "not_enough_size",
            f"Only {walk.filled:g} contracts on {name} at or {'below' if action == 'buy' else 'above'} {cap}, "
            f"the limit set by {bound}.",
            **priced,
        )
    prices = [f.price for f in walk.fills]
    limit_price = max(prices) if action == "buy" else min(prices)
    try:
        check_order_against_market(
            probe.model_copy(update={"price": limit_price}),
            tick=tick,
            min_size=info.min_size,
            held_now=float("inf"),
        )
    except VenueError as e:
        return skip("invalid_order", e.message, **priced)
    levels = ob.asks if action == "buy" else ob.bids
    at_limit = sum(
        lv.size
        for lv in levels
        if (lv.price <= limit_price + 1e-9 if action == "buy" else lv.price >= limit_price - 1e-9)
    )
    all_in = round(walk.cost + walk.fees if action == "buy" else walk.cost - walk.fees, 6)
    return VenueCost(
        **{
            **base.__dict__,
            **priced,
            "limit_price": limit_price,
            "avg_price": walk.avg_price,
            "cost": walk.cost,
            "fees": walk.fees,
            "all_in": all_in,
            "all_in_per_contract": round(all_in / size, 6),
            "size_at_limit": round(at_limit, 6),
            "fills": walk.fills,
        }
    )


# ---- the choice --------------------------------------------------------------------------------


def _label(v: VenueCost, other: VenueCost) -> str:
    name = VENUE_NAMES.get(v.venue, v.venue)
    return f"{name} {v.market}" if v.venue == other.venue else name


def _choose(
    costs: tuple[VenueCost, VenueCost], action: Action
) -> tuple[VenueCost | None, str, str, float | None]:
    a, b = costs
    label = {id(a): _label(a, b), id(b): _label(b, a)}
    ok = [v for v in costs if v.ok]
    n = f"{costs[0].size:g} contracts"
    if not ok:
        parts = "; ".join(f"{label[id(v)]}: {v.detail}" for v in costs)
        return None, "no_venue", f"No venue can take this order. {parts}", None
    if len(ok) == 1:
        win = ok[0]
        lost = b if win is a else a
        return (
            win,
            "only_venue",
            f"{label[id(win)]} is the only venue that can take it: {label[id(lost)]} was skipped "
            f"({lost.skip}: {lost.detail})",
            None,
        )
    sign = 1 if action == "buy" else -1
    # Better all-in first; then more contracts on offer at the limit; then the pair's own order.
    ranked = sorted(ok, key=lambda v: (sign * (v.all_in or 0.0), -(v.size_at_limit or 0.0)))
    win, run = ranked
    w, r = win.all_in or 0.0, run.all_in or 0.0
    saving = round(abs(w - r), 6)
    if saving > 1e-9:
        if action == "buy":
            text = (
                f"{label[id(win)]} is {_usd(saving)} cheaper all-in for {n}: "
                f"{_usd(w)} vs {_usd(r)} on {label[id(run)]}."
            )
            return win, "cheaper", text, saving
        text = (
            f"{label[id(win)]} pays {_usd(saving)} more after fees for {n}: "
            f"{_usd(w)} vs {_usd(r)} on {label[id(run)]}."
        )
        return win, "pays_more", text, saving
    what = "cost" if action == "buy" else "pay"
    if (win.size_at_limit or 0.0) > (run.size_at_limit or 0.0) + 1e-9:
        text = (
            f"Both {what} {_usd(w)} all-in for {n}; {label[id(win)]} has more on offer at its price "
            f"({win.size_at_limit:g} vs {run.size_at_limit:g} contracts)."
        )
        return win, "tie_more_size", text, 0.0
    text = (
        f"Both {what} {_usd(w)} all-in for {n}, with the same size on offer; "
        f"{label[id(win)]} is the pair's first leg."
    )
    return win, "tie_first_listed", text, 0.0


def compare(
    client: Client, pair: Any, side: str, size: float, *, action: Action, limit: float | None
) -> BestVenue:
    """Both legs' all-in costs for ``size`` and the venue that wins. Reads books; sends nothing."""
    if side not in ("yes", "no"):
        raise VenueError("invalid_order", f"side must be 'yes' or 'no', not {side!r}.", retryable=False)
    if not size > 0:
        raise VenueError("invalid_order", f"size must be more than 0, not {size!r}.", retryable=False)
    if limit is not None and not 0 < limit < 1:
        which = "max_price" if action == "buy" else "min_price"
        raise VenueError(
            "invalid_order", f"{which} must be above 0 and below 1, not {limit!r}.", retryable=False
        )
    legs = legs_of(pair)
    ready = [_ready(client, v, m, side, action, size) for v, m in legs]
    # Both books are read together, so neither is older than max_quote_age_s when they're compared.
    readable = [leg for leg, r in zip(legs, ready, strict=True) if isinstance(r, MarketInfo)]
    books = dict(zip(readable, client._fresh_books(readable), strict=True))
    a, b = (
        _cost(client, v, m, side, action, size, limit, r, books[(v, m)]) if isinstance(r, MarketInfo) else r
        for (v, m), r in zip(legs, ready, strict=True)
    )
    costs = (a, b)
    chosen, code, reason, saving = _choose(costs, action)
    return BestVenue(action, side, size, limit, costs, chosen, code, reason, saving, client._now())


# ---- a buy by dollars ----------------------------------------------------------------------------


def _most_for(
    client: Client,
    venue: str,
    market: str,
    side: str,
    spend: float,
    limit: float | None,
    info: MarketInfo,
    book: Book | VenueError,
) -> VenueCost:
    """The most whole contracts on one venue whose cost plus fees fits in ``spend``, walked on one book.

    Cost plus fees only grows with size, and a size the book can't fill within the limit stays
    unfillable as it grows, so a binary search over :func:`_cost` finds it. Every step walks the
    same book: nothing more is read.
    """
    lo = max(1, math.ceil(info.min_size - 1e-9))
    first = _cost(client, venue, market, side, "buy", lo, limit, info, book)
    if not first.ok:
        return first
    if (first.all_in or 0.0) > spend + 1e-9:
        name = VENUE_NAMES.get(venue, venue)
        what = "one contract" if lo == 1 else f"{lo:g} contracts, the market's minimum"
        return VenueCost(
            venue,
            market,
            side,
            "buy",
            lo,
            skip="spend_too_small",
            detail=f"{_usd(spend)} doesn't buy {what} on {name}: it costs {_usd(first.all_in or 0.0)} with fees.",
            best_price=first.best_price,
            cap=first.cap,
            capped_by=first.capped_by,
            book_as_of=first.book_as_of,
        )
    best = first
    # Fees only add to the price, so the money can't buy more than this many at the best price.
    hi = max(lo, math.floor(spend / (first.best_price or 1.0) + 1e-9))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        r = _cost(client, venue, market, side, "buy", mid, limit, info, book)
        if r.ok and (r.all_in or 0.0) <= spend + 1e-9:
            lo, best = mid, r
        else:
            hi = mid - 1
    return best


def _choose_spend(
    costs: tuple[VenueCost, VenueCost], spend: float
) -> tuple[VenueCost | None, str, str, float | None]:
    """The venue where ``spend`` wins more if you're right (more contracts); the same number: :func:`_choose`."""
    a, b = costs
    if not (a.ok and b.ok) or abs(a.size - b.size) < 1e-9:
        return _choose(costs, "buy")
    win, run = (a, b) if a.size > b.size else (b, a)
    more = round(win.size - run.size, 6)
    text = (
        f"{_label(win, run)} wins {_usd(more)} more if you're right: {_usd(spend)} buys {win.size:g} contracts "
        f"there for {_usd(win.all_in or 0.0)} all-in, vs {run.size:g} for {_usd(run.all_in or 0.0)} on {_label(run, win)}."
    )
    return win, "wins_more", text, more


def compare_spend(client: Client, pair: Any, side: str, spend: float, *, limit: float | None) -> BestVenue:
    """A buy by dollars: the most whole contracts ``spend`` buys on each leg, all-in, and the venue
    where it wins more if you're right. Reads each book once; sends nothing."""
    if side not in ("yes", "no"):
        raise VenueError("invalid_order", f"side must be 'yes' or 'no', not {side!r}.", retryable=False)
    if not (isinstance(spend, int | float) and math.isfinite(spend) and spend > 0):
        raise VenueError(
            "invalid_order", f"spend must be more than 0 dollars, not {spend!r}.", retryable=False
        )
    if limit is not None and not 0 < limit < 1:
        raise VenueError(
            "invalid_order", f"max_price must be above 0 and below 1, not {limit!r}.", retryable=False
        )
    legs = legs_of(pair)
    # The rules and the market don't depend on the size: one contract is the smallest order there is.
    ready = [_ready(client, v, m, side, "buy", 1) for v, m in legs]
    readable = [leg for leg, r in zip(legs, ready, strict=True) if isinstance(r, MarketInfo)]
    books = dict(zip(readable, client._fresh_books(readable), strict=True))
    a, b = (
        _most_for(client, v, m, side, spend, limit, r, books[(v, m)]) if isinstance(r, MarketInfo) else r
        for (v, m), r in zip(legs, ready, strict=True)
    )
    costs = (a, b)
    chosen, code, reason, saving = _choose_spend(costs, spend)
    size = chosen.size if chosen else None
    return BestVenue("buy", side, size, limit, costs, chosen, code, reason, saving, client._now(), spend)


def order_for(why: BestVenue, tif: Tif) -> Order | None:
    """The order the comparison picked: a limit at the walk's deepest price, ``tif`` (default ioc)."""
    v = why.chosen
    if v is None or v.limit_price is None:
        return None
    return Order(
        venue=v.venue,
        market=v.market,
        side=v.side,
        action=v.action,
        price=v.limit_price,
        size=v.size,
        tif=tif,
    )


def no_venue(why: BestVenue) -> VenueError:
    return VenueError(
        "not_available",
        why.reason,
        retryable=False,
        hint="Nothing was sent. Each venue's reason is in the error's raw['venues'].",
        next="client.preview_best(...) shows the comparison without sending.",
        raw=why.to_dict(),
    )


def check_tif(tif: str) -> Tif:
    if tif not in ("ioc", "fok"):
        raise VenueError(
            "invalid_order",
            f"tif must be 'ioc' or 'fok', not {tif!r}.",
            retryable=False,
            hint="A best-venue order takes what's there now; it doesn't rest in the book.",
        )
    return tif  # type: ignore[return-value]
