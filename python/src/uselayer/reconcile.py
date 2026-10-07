"""Reconciliation: what :meth:`~uselayer.Client.reconcile` returns.

In live mode the SDK keeps its own record of your orders and fills (the local store), and the
guardrails (max position, max daily loss) count your positions from it. If a fill never reaches the
store, or the account trades outside the SDK (the venue's website, another bot), the store and the
venue disagree, and the limits check the wrong numbers. ``reconcile()`` reads each venue's fills,
positions and open orders with your key and lists every difference. It changes nothing unless you
ask it to (``repair=True``).

What it can report (``Mismatch.kind``):

- ``missed_fill``: the venue filled an order this SDK sent, and the store doesn't have that fill.
- ``unknown_fill``: the store has a fill for an SDK order that the venue doesn't show.
- ``outside_fill``: a fill of an order this SDK didn't send.
- ``position``: the contracts you hold in a market, per the store, differ from the venue's.
- ``outside_order``: an open order on the venue that this SDK didn't send.
- ``stale_order``: the store thinks an order is open, and the venue doesn't list it as open.

Positions are compared as net YES contracts (Polymarket US: net long), so buying NO shows as a
negative number. Markets the venue reports as settled aren't compared: the live store doesn't record
venue payouts. Positions are compared for the whole account, so positions from orders sent with
another store show as ``position`` mismatches. Fills are compared from ``since`` on. Polymarket US
can take a moment to list a new trade, so a fill made just before ``reconcile()`` can show as
``unknown_fill`` until it does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .events import Fill, SimulatedFill
from .orders import Order
from .store import Store
from .venues.base import LiveAdapter
from .venues.polymarket_us import split_market
from .venues.polymarket_us_live import filled_range, oldest_first, trades_within

Kind = Literal["missed_fill", "unknown_fill", "outside_fill", "position", "outside_order", "stale_order"]
EPS = 1e-6


@dataclass(frozen=True)
class Mismatch:
    """One difference between the store and a venue.

    ``store`` and ``venue_says`` are contracts: filled on the order (fills) or held as net YES (positions).
    ``fill`` is the fill in question (the venue's for ``missed_fill`` and ``outside_fill``, the store's for
    ``unknown_fill``); ``order`` is the order in question.
    """

    kind: Kind
    venue: str
    market: str
    message: str
    store: float | None = None
    venue_says: float | None = None
    order_id: str | None = None
    venue_order_id: str | None = None
    fill: Fill | None = None
    order: Order | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "venue": self.venue,
            "market": self.market,
            "message": self.message,
            "store": self.store,
            "venue_says": self.venue_says,
            "order_id": self.order_id,
            "venue_order_id": self.venue_order_id,
            "fill": None if self.fill is None else self.fill.to_dict(),
            "order": None if self.order is None else self.order.to_dict(),
        }


@dataclass(frozen=True)
class Reconciliation:
    """The result of :meth:`~uselayer.Client.reconcile`.

    r = client.reconcile()
    r.ok              # True when the store and every venue agree
    r.mismatches      # every difference, as Mismatch
    r.of("outside_fill")
    r.repaired        # fills added to the store, by sync() or reconcile (repair=True only)
    """

    as_of: datetime
    since: datetime
    venues: tuple[str, ...]
    mismatches: tuple[Mismatch, ...]
    checked: Mapping[str, Mapping[str, int]]
    settled: tuple[str, ...] = ()
    repaired: tuple[Fill, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def of(self, kind: Kind) -> list[Mismatch]:
        return [m for m in self.mismatches if m.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "as_of": self.as_of.isoformat(),
            "since": self.since.isoformat(),
            "venues": list(self.venues),
            "checked": {v: dict(c) for v, c in self.checked.items()},
            "settled": list(self.settled),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "repaired": [f.to_dict() for f in self.repaired],
        }

    def __str__(self) -> str:
        return json.dumps(self.to_dict(), indent=1)


# ---- YES terms --------------------------------------------------------------------------------------


def _key(venue: str, market: str) -> str:
    """The venue's own market id: a Polymarket US order's ``slug:short`` trades the slug's market."""
    return split_market(market)[0] if venue == "polymarket_us" else market


def _long(venue: str, market: str, side: str) -> bool:
    """Whether ``side`` of ``market`` is the venue's YES (Polymarket US: its long side)."""
    if venue == "polymarket_us":
        return (side == "yes") != split_market(market)[1]
    return side == "yes"


def yes_delta(f: Fill | SimulatedFill) -> float:
    """How a fill changed the net YES contracts held: buying YES or selling NO adds, the rest takes away."""
    up = _long(f.venue, f.market, f.side) == (f.action == "buy")
    return f.contracts if up else -f.contracts


def _in_order_terms(f: Fill, order: Order) -> Fill:
    """A venue fill (seen from YES or long/short) restated as a fill of ``order``, as ``refresh()`` records it."""
    same = _long(f.venue, f.market, f.side) == _long(order.venue, order.market, order.side)
    price = f.price if same else round(1 - f.price, 6)
    return f.model_copy(
        update={
            "market": order.market,
            "order_id": order.id or order.client_id,
            "side": order.side,
            "action": order.action,
            "price": price,
            "cost": round(price * f.contracts, 6),
            "group_id": order.group_id,
        }
    )


def _held_as_filled_ids(venue: Sequence[Fill], store: Sequence[Fill], vid: str | None) -> set[str | None]:
    """The venue trades that store fills with an ``"<order id>:<filled>"`` id already stand for: Polymarket US
    ``refresh()`` fills from before 0.3.0, or ones its activity history didn't show yet."""
    trades = oldest_first(venue)
    out: set[str | None] = set()
    for f in store:
        r = filled_range(f, vid)
        if r is not None:
            out |= {t.venue_fill_id for t in trades_within(trades, *r)}
    return out


def _filled_ids_shown(venue_filled: float, store: Sequence[Fill], vid: str | None) -> set[str | None]:
    """The store's ``"<order id>:<filled>"`` fills that the venue's trades of the order reach."""
    out: set[str | None] = set()
    for f in store:
        r = filled_range(f, vid)
        if r is not None and r[1] <= venue_filled + EPS:
            out.add(f.venue_fill_id)
    return out


def _n(x: float) -> str:
    return f"{x:+g}"


# ---- the comparison ---------------------------------------------------------------------------------


def compare(
    store: Store,
    adapters: Mapping[str, LiveAdapter],
    *,
    since: datetime,
) -> tuple[list[Mismatch], dict[str, dict[str, int]], list[str]]:
    """Read every venue once and compare it with the store. Writes nothing."""
    out: list[Mismatch] = []
    checked: dict[str, dict[str, int]] = {}
    settled: list[str] = []
    orders = [o for o in store.orders() if o.mode == "live"]
    stored_fills = [f for f in store.fills() if isinstance(f, Fill)]
    for venue, adapter in adapters.items():
        mine = [o for o in orders if o.venue == venue]
        by_vid = {o.venue_order_id: o for o in mine if o.venue_order_id}
        by_id = {o.id: o for o in mine if o.id}
        venue_fills = list(adapter.fills(since=since))
        own = [f for f in stored_fills if f.venue == venue]
        positions = adapter.positions(include_closed=True)
        open_on_venue = adapter.open_orders()
        checked[venue] = {
            "venue_fills": len(venue_fills),
            "store_fills": len(own),
            "positions": sum(1 for p in positions if p.contracts > EPS and not p.settled),
            "open_orders": len(open_on_venue),
        }

        # Fills, order by order.
        venue_by_order: dict[str, list[Fill]] = {}
        outside: list[Fill] = []
        for f in venue_fills:
            o = by_vid.get(f.order_id)
            if o is None:
                outside.append(f)
            else:
                venue_by_order.setdefault(o.id or o.client_id, []).append(f)
        store_by_order: dict[str, list[Fill]] = {}
        for f in own:
            if f.at >= since:
                store_by_order.setdefault(f.order_id, []).append(f)
        for oid in sorted(set(venue_by_order) | set(store_by_order)):
            o = by_id.get(oid)
            v = venue_by_order.get(oid, [])
            s = store_by_order.get(oid, [])
            v_n = round(sum(f.contracts for f in v), 6)
            s_n = round(sum(f.contracts for f in s), 6)
            if abs(v_n - s_n) <= EPS:
                continue
            market = o.market if o else (v or s)[0].market
            vid = o.venue_order_id if o else None
            if v_n > s_n:
                # The venue's fills that the store lacks, oldest first, until they cover the difference.
                have = {f.venue_fill_id for f in s} | _held_as_filled_ids(v, s, vid)
                left = v_n - s_n
                for f in sorted((f for f in v if f.venue_fill_id not in have), key=lambda f: f.at):
                    if left <= EPS:
                        break
                    left -= f.contracts
                    out.append(
                        Mismatch(
                            "missed_fill",
                            venue,
                            market,
                            f"The venue filled {f.contracts:g} on order {oid} at {f.at.isoformat()}; "
                            f"the store has {s_n:g} of the {v_n:g} filled.",
                            store=s_n,
                            venue_says=v_n,
                            order_id=oid,
                            venue_order_id=vid,
                            fill=f if o is None else _in_order_terms(f, o),
                            order=o,
                        )
                    )
            else:
                seen = {f.venue_fill_id for f in v} | _filled_ids_shown(v_n, s, vid)
                left = s_n - v_n
                for f in sorted(
                    (f for f in s if f.venue_fill_id not in seen), key=lambda f: f.at, reverse=True
                ):
                    if left <= EPS:
                        break
                    left -= f.contracts
                    out.append(
                        Mismatch(
                            "unknown_fill",
                            venue,
                            market,
                            f"The store has a fill of {f.contracts:g} on order {oid} that the venue doesn't show "
                            f"(store {s_n:g} filled, venue {v_n:g}).",
                            store=s_n,
                            venue_says=v_n,
                            order_id=oid,
                            venue_order_id=vid,
                            fill=f,
                            order=o,
                        )
                    )
        for f in sorted(outside, key=lambda f: f.at):
            out.append(
                Mismatch(
                    "outside_fill",
                    venue,
                    f.market,
                    f"{f.action} {f.contracts:g} {f.side.upper()} at {f.price:g} on {f.at.isoformat()}, from venue "
                    f"order {f.order_id}, which this SDK didn't send.",
                    venue_says=f.contracts,
                    venue_order_id=f.order_id,
                    fill=f,
                )
            )

        # Positions, as net YES contracts per market.
        held: dict[str, float] = {}
        for f in own:
            k = _key(venue, f.market)
            held[k] = held.get(k, 0.0) + yes_delta(f)
        venue_net: dict[str, float] = {}
        for p in positions:
            k = _key(venue, p.market)
            if p.settled:
                if abs(held.get(k, 0.0)) > EPS:
                    settled.append(f"{venue}:{k}")
                held.pop(k, None)
                continue
            venue_net[k] = venue_net.get(k, 0.0) + (p.contracts if p.side == "yes" else -p.contracts)
        settled_keys = {s.split(":", 1)[1] for s in settled if s.startswith(venue + ":")}
        outside_by_market: dict[str, float] = {}
        for f in outside:
            k = _key(venue, f.market)
            outside_by_market[k] = outside_by_market.get(k, 0.0) + yes_delta(f)
        for k in sorted(set(held) | set(venue_net)):
            if k in settled_keys:
                continue
            s_net, v_net = round(held.get(k, 0.0), 6), round(venue_net.get(k, 0.0), 6)
            if abs(s_net - v_net) <= EPS:
                continue
            why = ""
            if abs(outside_by_market.get(k, 0.0)) > EPS:
                why = f" Fills outside the SDK since {since.isoformat()} account for {_n(outside_by_market[k])}."
            out.append(
                Mismatch(
                    "position",
                    venue,
                    k,
                    f"Net YES contracts: the store says {_n(s_net)}, the venue says {_n(v_net)}.{why}",
                    store=s_net,
                    venue_says=v_net,
                )
            )

        # Open orders, both ways.
        known = set(by_vid)
        live_ids = {o.venue_order_id for o in open_on_venue}
        for o in open_on_venue:
            if o.venue_order_id not in known:
                out.append(
                    Mismatch(
                        "outside_order",
                        venue,
                        o.market,
                        f"An open order on the venue that this SDK didn't send: {o.action} {o.size:g} "
                        f"{o.side.upper()} at {o.price:g}.",
                        venue_order_id=o.venue_order_id,
                        order=o,
                    )
                )
        for o in mine:
            if o.is_open and o.venue_order_id not in live_ids:
                out.append(
                    Mismatch(
                        "stale_order",
                        venue,
                        o.market,
                        f"The store thinks order {o.id} is {o.status}; the venue doesn't list it as open. "
                        "client.sync() reads its final state.",
                        order_id=o.id,
                        venue_order_id=o.venue_order_id,
                        order=o,
                    )
                )
    return out, checked, settled


def missed_fills_to_add(mismatches: Sequence[Mismatch]) -> list[Fill]:
    """The missed fills ``repair=True`` may add: fills of SDK orders the store already shows as closed.

    An order still open in the store gets its fills from ``sync()``; adding them here as well could
    count them twice, so they're left to it.
    """
    return [
        m.fill
        for m in mismatches
        if m.kind == "missed_fill" and m.fill is not None and m.order is not None and not m.order.is_open
    ]
