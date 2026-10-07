"""Event types, in two families that never mix.

**Market events** are things a venue did: a book, a trade, a status change, a resolution. They carry
``origin="venue"``.

**Simulated results** are things the SDK's fill model decided in paper or backtest mode: a fill, a
position, a settlement. They carry ``simulated=True`` and are separate classes, so code that expects a real
:class:`Fill` can never be handed a :class:`SimulatedFill` by mistake.

    for e in events:
        if isinstance(e, TradePrint): ...            # the venue printed a trade
        elif isinstance(e, SimulatedFill): ...  # the paper fill model filled your order
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .books import Book, BookLevelChange

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    def to_dict(self) -> dict[str, Any]:
        """The event as plain data."""
        return self.model_dump(mode="json")


# ---- market events: what a venue did ------------------------------------------------------------


class TradePrint(_Event):
    """A trade the venue printed: ``size`` contracts of the YES side at ``price``."""

    kind: Literal["trade"] = "trade"
    origin: Literal["venue"] = "venue"
    venue: str
    market: str
    price: float = Field(gt=0, lt=1)
    size: float = Field(gt=0)
    as_of: datetime
    trade_id: str | None = Field(default=None, description="The venue's id for this trade.")
    aggressor: Literal["buy", "sell"] | None = Field(
        default=None,
        description="What the taker did to YES: ``buy`` lifted the asks, ``sell`` hit the bids. None if unknown.",
    )
    received_at: datetime | None = Field(
        default=None, description="When this machine received it (recordings only); this machine's clock."
    )


class MarketStatus(_Event):
    """A market opening, pausing, closing or halting, as the venue reported it."""

    kind: Literal["status"] = "status"
    origin: Literal["venue"] = "venue"
    venue: str
    market: str
    status: Literal["open", "paused", "closed", "halted"]
    as_of: datetime
    received_at: datetime | None = Field(
        default=None, description="When this machine received it (recordings only); this machine's clock."
    )


class Resolution(_Event):
    """How a market settled: ``"yes"``, ``"no"`` or ``"void"`` (refunded).

    ``payout`` is what one YES contract paid, in dollars, when the venue says: Kalshi pays a fair price
    between 0 and 1 for some canceled games. Without it, YES pays $1 on ``yes`` and $0 on ``no``, and a
    ``void`` pays back what each contract cost.
    """

    kind: Literal["resolution"] = "resolution"
    origin: Literal["venue"] = "venue"
    venue: str
    market: str
    outcome: Literal["yes", "no", "void"]
    as_of: datetime
    payout: float | None = Field(
        default=None, ge=0, le=1, description="What one YES contract paid, in dollars, if the venue says."
    )
    received_at: datetime | None = Field(
        default=None, description="When this machine received it (recordings only); this machine's clock."
    )

    def paid(self, side: str, avg_price: float) -> float:
        """What one contract of ``side`` bought at ``avg_price`` pays."""
        if self.payout is not None:
            yes = self.payout
        elif self.outcome == "void":
            return avg_price
        else:
            yes = 1.0 if self.outcome == "yes" else 0.0
        return round(yes if side == "yes" else 1.0 - yes, 6)


class StreamGap(_Event):
    """A stretch with no data for a market: the recorder's connection dropped between ``as_of`` and ``until``.

    Anything the venue did in that stretch is missing from the recording, so the last book before it
    can't be trusted. Both times are this machine's clock, since the venue sent nothing. In backtest
    mode the market has no book from ``as_of`` until the next book after the gap.
    """

    kind: Literal["gap"] = "gap"
    origin: Literal["recorder"] = "recorder"
    venue: str
    market: str
    as_of: datetime = Field(description="When the connection was found to be down.")
    until: datetime = Field(description="When the market was subscribed again.")
    reason: str

    @property
    def seconds(self) -> float:
        return (self.until - self.as_of).total_seconds()


MarketEvent = Book | BookLevelChange | TradePrint | MarketStatus | Resolution | StreamGap
"""Anything a venue did, plus the gaps where a recording has nothing. A :class:`~uselayer.books.Book`
is a full book; a :class:`~uselayer.books.BookLevelChange` is one level changing."""


# ---- real results: what a venue did with your order (live mode) -----------------------------------


class Fill(_Event):
    """A real fill a venue reported for one of your orders. Live mode only."""

    kind: Literal["fill"] = "fill"
    simulated: Literal[False] = False
    mode: Literal["live"] = "live"
    venue: str
    market: str
    order_id: str
    venue_fill_id: str | None = None
    side: Side
    action: Action
    price: float
    contracts: float
    role: Literal["taker", "maker"]
    cost: float = Field(description="price × contracts, in dollars (proceeds for a sell).")
    fee: float = Field(description="What the venue billed.")
    fee_estimate: float = Field(description="Layer's formula, the same as /v0/profit.")
    at: datetime
    group_id: str | None = None


# ---- simulated results: what the SDK's fill model decided (paper, backtest) -----------------------


class SimulatedFill(_Event):
    """A fill the SDK's fill model made in paper or backtest mode. No venue saw this order."""

    kind: Literal["simulated_fill"] = "simulated_fill"
    simulated: Literal[True] = True
    mode: Literal["paper", "backtest"]
    venue: str
    market: str
    order_id: str
    side: Side
    action: Action
    price: float
    contracts: float
    role: Literal["taker", "maker"]
    cost: float = Field(description="price × contracts, in dollars (proceeds for a sell).")
    fee: float = Field(description="Layer's fee formula for this fill; negative is a maker rebate.")
    at: datetime
    book_as_of: datetime = Field(description="The venue timestamp of the book this fill was made against.")
    group_id: str | None = None


class SimulatedSettlement(_Event):
    """A paper or backtest position paid out because its market settled. The position closes.

    ``payout`` is what each contract paid; ``proceeds`` is ``payout × contracts``. No fee is charged.
    """

    kind: Literal["simulated_settlement"] = "simulated_settlement"
    simulated: Literal[True] = True
    mode: Literal["paper", "backtest"]
    venue: str
    market: str
    side: Side
    outcome: Literal["yes", "no", "void"]
    contracts: float
    payout: float = Field(description="What one contract paid, in dollars.")
    proceeds: float = Field(description="payout × contracts, in dollars.")
    at: datetime = Field(description="When the market settled (the resolution's time).")
    group_id: str | None = None


class SimulatedPosition(_Event):
    """An open position built from simulated fills, in paper or backtest mode."""

    kind: Literal["simulated_position"] = "simulated_position"
    simulated: Literal[True] = True
    mode: Literal["paper", "backtest"]
    venue: str
    market: str
    side: Side
    contracts: float
    avg_price: float
    cost: float = Field(description="What the open contracts cost, without fees.")
    fees: float
    opened_at: datetime
    group_id: str | None = None
