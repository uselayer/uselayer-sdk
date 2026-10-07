"""Positions and profit or loss, worked out from simulated fills and settlements."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from .events import Fill, SimulatedFill, SimulatedPosition, SimulatedSettlement

AnyFill = Fill | SimulatedFill


@dataclass
class _Pos:
    venue: str
    market: str
    side: str
    group_id: str | None
    contracts: float = 0.0
    cost: float = 0.0
    fees: float = 0.0
    opened_at: datetime | None = None
    realized: float = 0.0  # from sells and settlements since the first fill, before fees
    fees_total: float = 0.0  # every fee since the first fill, on contracts held now or not
    outcome: str | None = None  # how the market settled, once it has


@dataclass
class Ledger:
    """Positions plus realized profit and fees, split at a day boundary.

    ``positions`` are the open ones; ``rows`` is every position ever held, open, closed or settled.
    """

    positions: list[_Pos] = field(default_factory=list)
    rows: list[_Pos] = field(default_factory=list)
    realized_today: float = 0.0
    fees_today: float = 0.0


def _key(f: AnyFill | SimulatedSettlement) -> tuple[str, str, str]:
    return (f.venue, f.market, f.side)


def _merge(
    fills: Iterable[AnyFill], settlements: Iterable[SimulatedSettlement]
) -> Iterator[AnyFill | SimulatedSettlement]:
    """Fills in their stored order, each settlement placed after its own position's fills up to its time.

    A settlement only changes its own position, so fills of other markets never decide where it goes.
    """
    pending: dict[tuple[str, str, str], list[SimulatedSettlement]] = {}
    for s in sorted(settlements, key=lambda s: s.at):
        pending.setdefault(_key(s), []).append(s)
    for f in fills:
        waiting = pending.get(_key(f))
        while waiting and waiting[0].at < f.at:
            yield waiting.pop(0)
        yield f
    for rest in pending.values():
        yield from rest


def build(
    fills: Iterable[AnyFill], day_start: datetime, settlements: Iterable[SimulatedSettlement] = ()
) -> Ledger:
    """Replay fills and settlements in order: buys add contracts at their cost; sells close them at the
    average cost; a settlement closes the rest at its payout.

    ``realized_today`` counts profit from sells and settlements since ``day_start`` (before fees);
    ``fees_today`` counts every fee since then.
    """
    pos: dict[tuple[str, str, str], _Pos] = {}
    out = Ledger()
    for f in _merge(fills, settlements):
        p = pos.setdefault(_key(f), _Pos(f.venue, f.market, f.side, f.group_id))
        today = f.at >= day_start
        if isinstance(f, SimulatedSettlement):
            gain = f.proceeds - p.cost
            p.realized += gain
            if today:
                out.realized_today += gain
            p.contracts = p.cost = p.fees = 0.0
            p.outcome = f.outcome
            continue
        if today:
            out.fees_today += f.fee
        p.fees_total += f.fee
        if f.action == "buy":
            if p.contracts <= 1e-9:
                p.opened_at = f.at
                p.cost = p.fees = 0.0
            p.contracts += f.contracts
            p.cost += f.cost
            p.fees += f.fee
            p.group_id = p.group_id or f.group_id
        else:
            n = min(f.contracts, p.contracts)
            avg = p.cost / p.contracts if p.contracts else 0.0
            gain = f.cost - avg * n
            p.realized += gain
            if today:
                out.realized_today += gain
            p.cost -= avg * n
            p.fees = p.fees * ((p.contracts - n) / p.contracts) if p.contracts else 0.0
            p.contracts = round(p.contracts - n, 6)
    out.rows = list(pos.values())
    out.positions = [p for p in out.rows if p.contracts > 1e-9]
    return out


def held(
    fills: Iterable[AnyFill],
    venue: str,
    market: str,
    side: str,
    settlements: Iterable[SimulatedSettlement] = (),
) -> float:
    """Contracts of one side of one market held now. A settled position holds none."""
    if any(_key(s) == (venue, market, side) for s in settlements):
        return 0.0
    n = 0.0
    for f in fills:
        if (f.venue, f.market, f.side) == (venue, market, side):
            n += f.contracts if f.action == "buy" else -f.contracts
    return round(max(n, 0.0), 6)


def as_positions(book: Ledger, mode: str) -> list[SimulatedPosition]:
    return [
        SimulatedPosition(
            mode=mode,
            venue=p.venue,
            market=p.market,
            side=p.side,
            contracts=p.contracts,
            avg_price=round(p.cost / p.contracts, 6),
            cost=round(p.cost, 6),
            fees=round(p.fees, 6),
            opened_at=p.opened_at or datetime.min,
            group_id=p.group_id,
        )
        for p in book.positions
    ]
