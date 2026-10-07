"""Profit and loss: what :meth:`~uselayer.Client.pnl` returns.

Per position (one side of one market) and in total:

- ``realized``: profit from contracts sold or settled, before fees.
- ``unrealized``: contracts held now at the best bid (what you could sell them for), minus what they
  cost. ``None`` when the market has no bid or its book couldn't be read; the position is then listed
  in ``missing_marks`` and left out of the total.
- ``fees``: every fee paid on the position, on contracts held now or not (negative is a rebate).
- ``net``: ``realized + unrealized - fees``.

In total only, ``resolution_mismatch_loss``: how much of ``realized`` hedged pairs lost because the
two venues settled them differently (both legs lost, one voided, ...), against the $1 per contract
they were meant to pay; negative when a mismatch paid more (both legs won). See
:mod:`uselayer.mismatch`. It's a breakdown ("of which, lost to mismatched settlement"), already
inside ``realized`` and ``net``, never subtracted a second time. ``realized`` is what the venues
paid minus what it cost, and the rows add up to it.

Paper and backtest work it out from the store's fills and settlements. Live mode reports what each
venue says about your positions, plus the open contracts at the bid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .mismatch import ResolutionMismatch


@dataclass(frozen=True)
class PnlRow:
    """One side of one market."""

    venue: str
    market: str
    side: str
    contracts: float
    cost: float | None
    realized: float
    unrealized: float | None
    fees: float | None
    mark: float | None = None
    mark_as_of: datetime | None = None
    settled: bool = False
    outcome: str | None = None  # how the market settled (yes, no, void), when known
    group_id: str | None = None

    @property
    def net(self) -> float:
        return round(self.realized + (self.unrealized or 0.0) - (self.fees or 0.0), 6)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["mark_as_of"] = None if self.mark_as_of is None else self.mark_as_of.isoformat()
        d["net"] = self.net
        return d


@dataclass(frozen=True)
class Pnl:
    """Profit and loss in total and per position.

    p = client.pnl()
    p.net, p.realized, p.unrealized, p.fees, p.missing_marks
    p.resolution_mismatch_loss, p.resolution_mismatches
    p.market("some-slug")      # that market's rows
    """

    mode: str
    as_of: datetime
    rows: tuple[PnlRow, ...]
    resolution_mismatches: tuple[ResolutionMismatch, ...] = ()

    @property
    def resolution_mismatch_loss(self) -> float:
        """Of ``realized``, the dollars hedged pairs lost to the venues settling them differently (negative: they paid more than $1).

        Already counted in ``realized`` and ``net``; shown on its own so the cause is visible.
        """
        return round(0.0 - sum(m.impact for m in self.resolution_mismatches if m.impact is not None), 6)

    @property
    def realized(self) -> float:
        """Profit from contracts sold or settled, before fees: what the venues paid minus what it cost (the rows' sum)."""
        return round(sum(r.realized for r in self.rows), 6)

    @property
    def unrealized(self) -> float:
        return round(sum(r.unrealized for r in self.rows if r.unrealized is not None), 6)

    @property
    def fees(self) -> float:
        return round(sum(r.fees for r in self.rows if r.fees is not None), 6)

    @property
    def net(self) -> float:
        return round(self.realized + self.unrealized - self.fees, 6)

    @property
    def missing_marks(self) -> tuple[str, ...]:
        """Open positions with no bid to value them at: ``venue:market:side``. They're left out of ``unrealized``."""
        return tuple(
            f"{r.venue}:{r.market}:{r.side}"
            for r in self.rows
            if r.contracts > 1e-9 and not r.settled and r.unrealized is None
        )

    def market(self, market: str, *, venue: str | None = None) -> list[PnlRow]:
        return [r for r in self.rows if r.market == market and (venue is None or r.venue == venue)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "as_of": self.as_of.isoformat(),
            "realized": self.realized,
            "unrealized": self.unrealized,
            "fees": self.fees,
            "resolution_mismatch_loss": self.resolution_mismatch_loss,
            "net": self.net,
            "missing_marks": list(self.missing_marks),
            "rows": [r.to_dict() for r in self.rows],
            "resolution_mismatches": [m.to_dict() for m in self.resolution_mismatches],
        }

    def __str__(self) -> str:
        import json

        return json.dumps(self.to_dict(), indent=1)
