"""Pairs: quote two markets that are the same bet, trade both sides with the leg-risk guard, run a strategy.

Buying YES on one market and NO on its twin pays $1 per contract whichever way it settles. The risk
is that one leg fills and the other doesn't. ``trade()`` handles that:

1. Read both books fresh; refuse if either is older than ``max_quote_age_s``.
2. Size the pair with the same walk as ``POST /v0/size``, and check both legs against the guardrails
   together (all or nothing).
3. Send the thinner leg first, immediate-or-cancel. If nothing fills: ``"missed"``, no exposure.
4. Send the other leg for what filled, immediate-or-cancel, up to its break-even price, and keep
   trying until ``chase_s`` runs out.
5. Still short: ``on_miss="unwind"`` sells the extra first-leg contracts, never below its entry
   price minus ``max_unwind_loss`` (``"unwound"``); otherwise the open contracts are reported
   (``"exposed"``).

The same steps run in paper, backtest and live mode. Live, both legs go to the venues with your own
keys: ``Client(mode="live", kalshi=..., polymarket_us=...)`` needs a key for each leg's venue, checked
before anything is sent. If a venue answers an error mid-pair, the open contracts are still unwound
or reported. When a venue can't say whether a second-leg order went through (``outcome_unknown``),
nothing is unwound: the trade comes back ``"exposed"`` with a note to run ``client.sync()`` first.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from . import _switches
from .books import Book, OutcomeBook
from .calc import MAX_CONTRACTS, LegBook, _days, _hold_fields, pair_size
from .errors import VENUE_NAMES, VenueError, live_switched_off
from .fee_lookup import days_until, payout_times
from .fill import FeeSettings, calculate_fee, fee_per_contract
from .layer_api import Market, Match
from .orders import Order

if TYPE_CHECKING:
    from .client import Client

Side = Literal["yes", "no"]
LegId = tuple[str, str]  # (venue, market)
OnMiss = Literal["unwind", "hold"]


def legs_of(pair: Match | Sequence[Any]) -> tuple[LegId, LegId]:
    """The two (venue, market) legs of a Layer match, or of two markets given directly.

    legs_of(match)                                    # from client.matches()
    legs_of([("polymarket_us", "a"), ("polymarket_us", "b")])
    """
    if isinstance(pair, Match):
        ms = list(pair.markets().values())
        if len(ms) != 2:
            raise VenueError("invalid_order", "A match needs exactly two markets.", retryable=False)
        return (ms[0].venue, ms[0].market_id), (ms[1].venue, ms[1].market_id)
    items = list(pair)
    if len(items) != 2:
        raise VenueError(
            "invalid_order",
            "A pair is exactly two markets.",
            retryable=False,
            hint="Pass a Match from client.matches(), or two (venue, market) pairs.",
        )
    out: list[LegId] = []
    for x in items:
        if isinstance(x, Market):
            out.append((x.venue, x.market_id))
        elif isinstance(x, (tuple, list)) and len(x) == 2:
            out.append((str(x[0]), str(x[1])))
        else:
            raise VenueError("invalid_order", f"Not a market: {x!r}.", retryable=False)
    return out[0], out[1]


@dataclass(frozen=True)
class QuoteLeg:
    """One side of a quoted pair: what to buy where, and what the walk says it costs."""

    venue: str
    market: str
    side: Side
    best_price: float
    limit_price: float
    average_price: float | None
    contracts_at_best: float
    cost: float
    fee: float
    book_as_of: datetime

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["book_as_of"] = self.book_as_of.isoformat()
        return d


@dataclass(frozen=True)
class Quote:
    """Buying YES on one market and NO on the other, priced from both books after both fees.

    ``contracts`` clear ``min_edge`` (dollars per contract after fees). Read it as three numbers:

    - ``gross_spread``: what those contracts pay at settlement ($1 each) minus what they cost, before fees.
    - ``fees``: both venues' taker fees on them.
    - ``net_profit``: what they lock in, ``gross_spread - fees``.

    ``gross_at_best`` and ``edge_at_best`` are the same gap per contract at the top of both books,
    before and after fees: a quote with no contracts still shows the raw gap the fees closed.

    ``return_pct`` is the whole return on the money spent; ``return_per_day_pct`` spreads it over
    ``days_held``, the days until ``settles_at``, when the money is expected back: the later of the
    two markets' payouts, by the rule :meth:`~uselayer.Client.profit` uses, so the two agree on the
    same pair. All three are ``None`` when neither venue gives a time (and in a backtest, unless you
    pass ``settles_at``).
    """

    a: QuoteLeg | None
    b: QuoteLeg | None
    contracts: int
    min_edge: float
    edge_at_best: float | None
    net_profit: float
    net_profit_per_contract: float
    fees: float
    cost: float
    return_pct: float
    limited_by: str
    as_of: datetime
    gross_at_best: float | None = None
    gross_spread: float = 0.0
    gross_spread_per_contract: float = 0.0
    settles_at: datetime | None = None
    days_held: float | None = None
    return_per_day_pct: float | None = None

    @property
    def legs(self) -> tuple[QuoteLeg, QuoteLeg] | None:
        return (self.a, self.b) if self.a and self.b else None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["a"] = self.a.to_dict() if self.a else None
        d["b"] = self.b.to_dict() if self.b else None
        d["as_of"] = self.as_of.isoformat()
        d["settles_at"] = self.settles_at.isoformat() if self.settles_at else None
        return d


@dataclass(frozen=True)
class Exposure:
    """Contracts left on one side when a pair couldn't be completed or unwound."""

    venue: str
    market: str
    side: str
    contracts: float
    avg_price: float
    cost_with_fees: float
    worst_case: float
    mark: float | None
    as_of: datetime | None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["as_of"] = self.as_of.isoformat() if self.as_of else None
        return d


@dataclass(frozen=True)
class Trade:
    """What ``trade()`` did: ``"hedged"``, ``"missed"``, ``"unwound"`` or ``"exposed"``.

    ``hedged`` contracts are matched on both sides and ``locked_in`` is what they pay at settlement
    after their cost and fees. ``unwind_loss`` is what unwinding cost. ``exposure`` is set when
    contracts are left on one side.
    """

    status: Literal["hedged", "missed", "unwound", "exposed"]
    group_id: str
    quote: Quote
    orders: tuple[Order, ...] = ()
    hedged: float = 0.0
    locked_in: float = 0.0
    unwind_loss: float = 0.0
    exposure: Exposure | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "group_id": self.group_id,
            "hedged": self.hedged,
            "locked_in": self.locked_in,
            "unwind_loss": self.unwind_loss,
            "exposure": self.exposure.to_dict() if self.exposure else None,
            "orders": [o.to_dict() for o in self.orders],
            "quote": self.quote.to_dict(),
            "notes": list(self.notes),
        }


# ---- quoting ------------------------------------------------------------------------------------


def _leg_book(name: str, ob: OutcomeBook, settings: FeeSettings, at: datetime) -> LegBook:
    return LegBook(
        name,
        [{"price": lv.price, "size": lv.size} for lv in ob.asks],
        lambda p: fee_per_contract(settings, p, at=at),
        lambda c, p: calculate_fee(settings, contracts=c, price=p, role="taker", at=at),
        {},
    )


SettlesAt = str | datetime | None


def payout_at(
    client: Client, legs: tuple[LegId, LegId], settles_at: SettlesAt = None
) -> tuple[datetime | None, float | None]:
    """When a pair's money is expected back and the days until then, the way ``client.profit()`` works them out.

    ``settles_at`` (a date, a time with a zone, or an aware datetime) wins, checked as ``profit()``
    checks it. Otherwise the later of the two markets' expected payouts, from each venue's own market
    times (:func:`uselayer.fee_lookup.payout_times`), at least a day away. ``(None, None)`` when
    neither venue gives a time: it's never guessed.
    """
    now = client._now()
    if settles_at is not None:
        s = settles_at.isoformat() if isinstance(settles_at, datetime) else settles_at
        days = _days({"settles_at": s}, now)
        when = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (when if when.tzinfo else when.replace(tzinfo=UTC)), days
    expected, _ = payout_times([client.market(m, venue=v) for v, m in legs])
    if expected is None:
        return None, None
    return datetime.fromtimestamp(expected / 1000, UTC), days_until(expected, now)


def quote_pair(
    client: Client,
    pair: Match | Sequence[Any],
    *,
    size: int | None = None,
    min_edge: float = 0.0,
    settles_at: SettlesAt = None,
) -> Quote:
    """Price both ways of pairing two markets and return the better one."""
    (va, ma), (vb, mb) = legs_of(pair)
    client.market(ma, venue=va), client.market(mb, venue=vb)  # each market's own fee settings
    when, days = payout_at(client, ((va, ma), (vb, mb)), settles_at)
    books: list[Book] = []
    for b in client._fresh_books([(va, ma), (vb, mb)]):  # fresh together: trade() refuses an old one
        if isinstance(b, VenueError):
            raise b
        books.append(b)
    book_a, book_b = books
    sa_, sb_ = client._settings(va, ma), client._settings(vb, mb)
    at = client._now()
    best: tuple[tuple[bool, float, float], dict[str, Any], Side, Side] | None = None
    for side_a, side_b in (("yes", "no"), ("no", "yes")):
        oa, ob = book_a.outcome(side_a), book_b.outcome(side_b)  # type: ignore[arg-type]
        if not oa.asks or not ob.asks:
            continue
        r = pair_size(
            _leg_book("a", oa, sa_, at),
            _leg_book("b", ob, sb_, at),
            min_edge=min_edge,
            max_contracts=size or MAX_CONTRACTS,
            days=days,
        )
        key = (r["contracts"] > 0, r["net_profit"], r["edge_at_best"])
        if best is None or key > best[0]:
            best = (key, r, side_a, side_b)  # type: ignore[assignment]
    as_of = min(book_a.as_of, book_b.as_of)
    if best is None:
        hold = _hold_fields(days, 0.0)
        return Quote(
            a=None,
            b=None,
            contracts=0,
            min_edge=min_edge,
            edge_at_best=None,
            net_profit=0.0,
            net_profit_per_contract=0.0,
            fees=0.0,
            cost=0.0,
            return_pct=0.0,
            limited_by="no_asks",
            as_of=as_of,
            gross_at_best=None,
            gross_spread=0.0,
            gross_spread_per_contract=0.0,
            settles_at=when,
            days_held=hold.get("days_held"),
            return_per_day_pct=hold.get("return_per_day_pct"),
        )
    _, r, side_a, side_b = best

    def leg(venue: str, market: str, side: Side, book: Book, d: dict[str, Any]) -> QuoteLeg:
        asks = book.outcome(side).asks
        fills = d["fills"]
        limit = max((f["price"] for f in fills), default=asks[0].price)
        return QuoteLeg(
            venue,
            market,
            side,
            asks[0].price,
            limit,
            d["average_price"],
            asks[0].size,
            d["cost"],
            d["fee"],
            book.as_of,
        )

    return Quote(
        a=leg(va, ma, side_a, book_a, r["a"]),
        b=leg(vb, mb, side_b, book_b, r["b"]),
        contracts=r["contracts"],
        min_edge=min_edge,
        edge_at_best=r["edge_at_best"],
        net_profit=r["net_profit"],
        net_profit_per_contract=r["net_profit_per_contract"],
        fees=r["fees"],
        cost=r["cost"],
        return_pct=r["return_pct"],
        limited_by=r["limited_by"],
        as_of=as_of,
        gross_at_best=r["gross_at_best"],
        gross_spread=r["gross_spread"],
        gross_spread_per_contract=r["gross_spread_per_contract"],
        settles_at=when,
        days_held=r.get("days_held"),
        return_per_day_pct=r.get("return_per_day_pct"),
    )


# ---- trading ------------------------------------------------------------------------------------


def _ticks(price: float, tick: float, *, up: bool) -> float:
    units, step = round(price * 1_000_000), round(tick * 1_000_000)
    n = math.ceil(units / step) if up else units // step
    return n * step / 1_000_000


def break_even_price(
    *, first_cost_per_contract: float, settings: FeeSettings, tick: float, min_edge: float, at: datetime
) -> float | None:
    """The highest price on the tick grid at which the second leg still clears ``min_edge`` after its fee."""
    lo, hi = 1, round(1_000_000 / round(tick * 1_000_000)) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        p = round(mid * tick, 6)
        edge = 1 - first_cost_per_contract - p - fee_per_contract(settings, p, at=at)
        if edge >= min_edge - 1e-9 and edge > 1e-9:
            best, lo = p, mid + 1
        else:
            hi = mid - 1
    return best


def _available(ob: OutcomeBook, limit: float) -> float:
    return sum(lv.size for lv in ob.asks if lv.price <= limit + 1e-9)


def _require_live_keys(client: Client, legs: tuple[LegId, LegId]) -> None:
    """Live: every leg's venue is switched on in this release and has your key, before anything is sent."""
    for venue, _ in legs:
        if not _switches.TRADING.get(venue, False):
            raise live_switched_off(venue)
        if venue not in client._live:
            name = VENUE_NAMES.get(venue, venue)
            raise VenueError(
                "auth_failed",
                f"A live pair needs your {name} key too: one leg of this pair is on {name}.",
                venue=venue,
                retryable=False,
                hint="Nothing was sent.",
                next="Client(mode='live', kalshi=Kalshi(...), polymarket_us=PolymarketUS(...))",
            )


def _error_note(what: str, e: VenueError) -> str:
    if e.code == "outcome_unknown":
        return (
            f"{what}: the venue didn't say whether it was taken ({e.message.rstrip('.')}). "
            "Run client.sync() and check client.orders() before trading these markets again."
        )
    return f"{what}: {e.code}: {e.message}"


def trade_pair(
    client: Client,
    pair: Match | Sequence[Any],
    *,
    size: int | None,
    min_edge: float,
    on_miss: OnMiss,
    max_unwind_loss: float,
    chase_s: float,
    settles_at: SettlesAt = None,
) -> Trade:
    live = client.mode == "live"
    if live:
        _require_live_keys(client, legs_of(pair))
    q = quote_pair(client, pair, size=size, min_edge=min_edge, settles_at=settles_at)
    gid = str(uuid.uuid4())
    now = client._now()
    limit_age = client.rules.max_quote_age_s
    for lg in (q.a, q.b):
        if lg is not None and (now - lg.book_as_of).total_seconds() > limit_age:
            raise VenueError(
                "stale_quote",
                f"The {lg.venue} book for {lg.market} is older than max_quote_age_s.",
                venue=lg.venue,
                hint="Nothing was sent.",
                next="Try again when a fresh book is available.",
            )
    if q.contracts == 0 or q.a is None or q.b is None:
        return Trade("missed", gid, q, notes=(f"nothing clears min_edge {min_edge} ({q.limited_by})",))
    a, b = q.a, q.b
    oa = Order(
        venue=a.venue, market=a.market, side=a.side, price=a.limit_price, size=q.contracts, group_id=gid
    )
    ob = Order(
        venue=b.venue, market=b.market, side=b.side, price=b.limit_price, size=q.contracts, group_id=gid
    )
    (oa, _, info_oa), (ob, _, info_ob) = client._prepare(oa), client._prepare(ob)
    client._require_open(oa, info_oa)  # both markets open before either leg is sent
    client._require_open(ob, info_ob)
    client._decide_group([oa, ob])

    # The thinner leg first: fewer contracts up to its limit; on a tie, less room to its limit.
    def thin(o: Order, leg: QuoteLeg) -> tuple[float, float]:
        ob_ = client._books[(o.venue, o.market)].outcome(o.side)
        return _available(ob_, o.price), o.price - leg.best_price

    (first, _first_leg), (second, _second_leg) = sorted([(oa, a), (ob, b)], key=lambda t: thin(*t))
    sent: list[Order] = []
    # Live, both legs were just prepared and checked: send at once rather than read the book again.
    first_done = client._execute(first, checked=live)
    sent.append(first_done)
    n = first_done.filled
    if n <= 1e-9:
        return Trade("missed", gid, q, tuple(sent), notes=("first leg filled nothing",))

    notes: list[str] = []
    first_cost = ((first_done.avg_price or first.price) * n + (first_done.fees or 0.0)) / n
    info_b = client.market(second.market, venue=second.venue)
    be = break_even_price(
        first_cost_per_contract=first_cost,
        settings=client._settings(second.venue, second.market),
        tick=info_b.tick_size,
        min_edge=min_edge,
        at=client._now(),
    )
    filled_b = 0.0
    uncertain = False  # a second-leg order the venue couldn't confirm either way
    if be is None:
        notes.append("no price clears min_edge for the second leg")
    elif client.killed:
        notes.append("kill switch pressed: second leg not sent")
    else:
        start = client._monotonic()
        while True:
            short = round(n - filled_b, 6)
            if short <= 1e-9:
                break
            if client.killed:
                notes.append("kill switch pressed: chase stopped")
                break
            try:
                book_b = client.book(second.market, venue=second.venue).outcome(second.side)
                ask = book_b.best_ask
                if ask is not None:
                    # The collar always applies: never more than price_collar above the best ask.
                    limit = min(be, _ticks(ask.price + client.rules.price_collar, info_b.tick_size, up=False))
                    if ask.price <= limit + 1e-9:
                        leg = second.model_copy(
                            update={
                                "size": short,
                                "price": limit,
                                "client_id": str(uuid.uuid4()),
                                "filled": 0.0,
                            }
                        )
                        done = client._execute(leg, checked=live)
                        sent.append(done)
                        filled_b = round(filled_b + done.filled, 6)
            except VenueError as e:
                # The first leg is open: an error here must never leave it unreported.
                notes.append(_error_note("second leg", e))
                if e.code == "outcome_unknown":
                    uncertain = True
                    break
                if not e.retryable:
                    break
            if round(n - filled_b, 6) <= 1e-9 or client._monotonic() - start >= chase_s:
                break
            client._sleep(0.25)

    short = round(n - filled_b, 6)
    a_cost = (first_done.avg_price or first.price) * filled_b + (first_done.fees or 0.0) * (filled_b / n)
    b_cost = sum((o.avg_price or 0.0) * o.filled + (o.fees or 0.0) for o in sent[1:])
    locked = round(filled_b - a_cost - b_cost, 6) if filled_b else 0.0
    if short <= 1e-9:
        return Trade("hedged", gid, q, tuple(sent), hedged=filled_b, locked_in=locked, notes=tuple(notes))

    unwind_loss = 0.0
    entry = first_done.avg_price or first.price
    if uncertain:
        # Unwinding now could leave the other side open instead, if that order did go through.
        notes.append("not unwound: a second-leg order's outcome is unknown")
    elif on_miss == "unwind":
        try:
            info_a = client.market(first.market, venue=first.venue)
            bid = client.book(first.market, venue=first.venue).outcome(first.side).best_bid
            floor = entry - max_unwind_loss
            if bid is not None:
                floor = max(floor, bid.price - client.rules.price_collar)
            limit = max(_ticks(floor, info_a.tick_size, up=True), info_a.tick_size)
            if bid is not None and bid.price >= limit - 1e-9:
                u = first.model_copy(
                    update={
                        "action": "sell",
                        "size": short,
                        "price": limit,
                        "reason": "unwind",
                        "client_id": str(uuid.uuid4()),
                        "filled": 0.0,
                    }
                )
                done = client._execute(u, checked=live)
                sent.append(done)
                sold = done.filled
                unwind_loss = round(
                    (entry - (done.avg_price or limit)) * sold
                    + (done.fees or 0.0)
                    + (first_done.fees or 0.0) * (sold / n),
                    6,
                )
                short = round(short - sold, 6)
            else:
                notes.append(f"no bid at or above {limit} to unwind into")
        except VenueError as e:
            notes.append(_error_note("unwind", e))
        if short <= 1e-9:
            return Trade(
                "unwound",
                gid,
                q,
                tuple(sent),
                hedged=filled_b,
                locked_in=locked,
                unwind_loss=unwind_loss,
                notes=tuple(notes),
            )

    mark_book = client._books.get((first.venue, first.market))
    bid_now = mark_book.outcome(first.side).best_bid if mark_book else None
    cost = round(entry * short + (first_done.fees or 0.0) * (short / n), 6)
    exposure = Exposure(
        first.venue,
        first.market,
        first.side,
        short,
        entry,
        cost,
        cost,
        bid_now.price if bid_now else None,
        mark_book.as_of if mark_book else None,
    )
    client._alert("exposed", group_id=gid, exposure=exposure.to_dict(), notes=list(notes))
    return Trade(
        "exposed",
        gid,
        q,
        tuple(sent),
        hedged=filled_b,
        locked_in=locked,
        unwind_loss=unwind_loss,
        exposure=exposure,
        notes=tuple(notes),
    )


# ---- running a strategy -------------------------------------------------------------------------

Strategy = Callable[["Client", Any, Quote], None]


def run_strategy(
    client: Client,
    strategy: Strategy,
    pairs: Iterable[Match | Sequence[Any]],
    *,
    interval_s: float,
    iterations: int | None,
    stop: Callable[[], bool] | None,
    min_edge: float,
    size: int | None,
    settles_at: SettlesAt = None,
) -> int:
    pairs = list(pairs)
    calls = 0

    def call(pair: Any) -> None:
        nonlocal calls
        try:
            q = quote_pair(client, pair, size=size, min_edge=min_edge, settles_at=settles_at)
        except VenueError as e:
            client._alert("quote_failed", error=e.to_dict())
            return
        try:
            strategy(client, pair, q)
        except VenueError as e:
            client._alert("strategy_order_failed", error=e.to_dict())
        calls += 1

    if client.mode == "backtest":

        def on_book(c: Client, book: Book) -> None:
            for pair in pairs:
                legs = legs_of(pair)
                if (book.venue, book.market) in legs and all(lg in c._books for lg in legs):
                    call(pair)

        client.replay(on_book)
        return calls

    rounds = 0
    while True:
        for pair in pairs:
            call(pair)
        rounds += 1
        if (iterations is not None and rounds >= iterations) or client.killed or (stop and stop()):
            return calls
        client.monitor()
        client._sleep(interval_s)
