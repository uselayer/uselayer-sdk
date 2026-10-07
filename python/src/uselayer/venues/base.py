"""What every venue adapter provides, so the client treats every venue the same way.

A read adapter gives market facts and books. A live adapter can also place, find and cancel
orders and read fills, positions and the balance, all with the customer's own key.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..books import Book
from ..events import Fill
from ..fill import FeeSettings
from ..orders import Order


@dataclass(frozen=True)
class MarketInfo:
    """What the SDK needs to know about a market before trading it."""

    venue: str
    market: str
    question: str | None
    status: str
    open: bool
    tick_size: float
    min_size: float
    fees: FeeSettings
    end_date: str | None = None
    event_time: str | None = None

    # ``end_date`` is when the venue stops trading the market at the latest (Kalshi's ``close_time``,
    # Polymarket US's ``endDate``). ``event_time`` is when the venue says the event happens: Kalshi's
    # ``occurrence_datetime`` (else ``expected_expiration_time``), Polymarket US's ``gameStartTime``
    # (else ``endDate``). Both are the venue's own strings, ``None`` when it gives none.

    @property
    def slug(self) -> str:
        """The market id (a slug on Polymarket US)."""
        return self.market

    @property
    def fee_coefficient(self) -> float | None:
        """Polymarket US: the market's taker fee coefficient."""
        return self.fees.coefficient

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["fees"] = dict(self.fees.__dict__)
        return d


@dataclass(frozen=True)
class Payout:
    """What one YES contract paid once the venue settled a market, and when (``None`` if the venue doesn't say)."""

    yes: float
    at: datetime | None = None


@dataclass(frozen=True)
class BookRead:
    """A book plus what the venue's answer said about its freshness.

    ``cache_max_age_s`` is how long the venue's cache keeps an answer and ``cache_age_s`` how old this
    copy already was (both ``None`` when the answer wasn't cached).
    """

    book: Book
    state: str | None
    cache_age_s: float | None = None
    cache_max_age_s: float | None = None


@dataclass(frozen=True)
class Balance:
    """Money on one venue, in dollars."""

    venue: str
    cash: float
    in_orders: float | None = None
    positions_value: float | None = None
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "raw"}


@dataclass(frozen=True)
class VenuePosition:
    """A position as the venue reports it."""

    venue: str
    market: str
    side: str
    contracts: float
    avg_price: float | None = None
    cost: float | None = None
    realized_pnl: float | None = None
    fees: float | None = None
    settled: bool = False
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "raw"}


@runtime_checkable
class ReadAdapter(Protocol):
    venue: str

    def market(self, market: str) -> MarketInfo: ...

    def markets(self, *, limit: int = 50, offset: int = 0) -> list[MarketInfo]: ...

    def read_book(self, market: str) -> BookRead: ...


@runtime_checkable
class LiveAdapter(ReadAdapter, Protocol):
    """Trading with the customer's own key. Every method talks to the venue."""

    def place(self, order: Order) -> tuple[Order, list[Fill]]:
        """Send ``order``; return it with ``venue_order_id`` and ``status``, plus fills reported at once.

        Raises ``outcome_unknown`` when the venue's answer didn't arrive.
        """
        ...

    def refresh(self, order: Order) -> tuple[Order, list[Fill]]:
        """Re-read one of our orders from the venue, with any new fills."""
        ...

    def find(self, order: Order, *, since: datetime) -> Order | None:
        """Look an order up by its ``client_id`` (or venue id) after an unknown outcome."""
        ...

    def cancel(self, order: Order) -> Order: ...

    def cancel_all(self) -> int:
        """Cancel every open order on the venue; return how many were open."""
        ...

    def open_orders(self) -> list[Order]: ...

    def fills(self, *, since: datetime | None = None) -> Sequence[Fill]: ...

    def positions(self, *, include_closed: bool = False) -> list[VenuePosition]:
        """Positions with contracts held, or with ``include_closed`` also the ones closed out to zero (for their realized P&L)."""
        ...

    def balance(self) -> Balance: ...
