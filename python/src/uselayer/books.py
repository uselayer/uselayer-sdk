"""Order books: what a venue is offering, and when it said so.

A :class:`Book` is one market's book as the venue publishes it, for the instrument's YES side, with
``as_of`` taken from the venue's own timestamp (never from when the SDK received it).
:meth:`Book.outcome` gives the bids and asks for the side you trade: buying NO at ``p`` fills against
YES bids at ``1 − p``.

    book = client.book("some-market-slug")
    book.outcome("no").asks[0]      # the cheapest NO you can buy, as a Level
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .fees import js_round

Side = Literal["yes", "no"]


def _r6(p: float) -> float:
    return js_round(p * 1_000_000) / 1_000_000


class Level(BaseModel):
    """One price level: ``size`` contracts offered at ``price`` dollars."""

    model_config = ConfigDict(frozen=True)

    price: float = Field(gt=0, lt=1)
    size: float = Field(gt=0)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class OutcomeBook(BaseModel):
    """Bids (best first, highest) and asks (best first, lowest) for one side of a market."""

    model_config = ConfigDict(frozen=True)

    side: Side
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    as_of: datetime

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Book(BaseModel):
    """A market's order book for its YES side, as the venue published it at ``as_of``.

    ``source`` says where it came from: ``"venue"`` (read just now) or ``"recorded"`` (a saved book
    replayed in backtest mode).

        b = Book(venue="polymarket_us", market="slug", bids=[Level(price=0.4, size=10)],
                 asks=[Level(price=0.42, size=5)], as_of=now)
        b.outcome("no").best_ask.price   # 0.6
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["book"] = "book"
    venue: str
    market: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    as_of: datetime
    source: Literal["venue", "recorded"] = "venue"
    received_at: datetime | None = Field(
        default=None, description="When this machine received it (recordings only); this machine's clock."
    )

    def model_post_init(self, _: Any) -> None:
        object.__setattr__(self, "bids", tuple(sorted(self.bids, key=lambda lv: -lv.price)))
        object.__setattr__(self, "asks", tuple(sorted(self.asks, key=lambda lv: lv.price)))

    def outcome(self, side: Side) -> OutcomeBook:
        """Bids and asks for buying or selling ``side``.

        YES is the book as published. NO is the mirror: a NO ask at ``1 − b`` for each YES bid ``b``.
        """
        if side == "yes":
            return OutcomeBook(side="yes", bids=self.bids, asks=self.asks, as_of=self.as_of)
        bids = tuple(Level(price=_r6(1 - lv.price), size=lv.size) for lv in self.asks)
        asks = tuple(Level(price=_r6(1 - lv.price), size=lv.size) for lv in self.bids)
        return OutcomeBook(side="no", bids=bids, asks=asks, as_of=self.as_of)

    def age_s(self, now: datetime) -> float:
        """Seconds between the venue's timestamp and ``now``."""
        return (now - self.as_of).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """The book as plain data."""
        return self.model_dump(mode="json")


class BookLevelChange(BaseModel):
    """One level changing in a book (a delta). ``size`` 0 removes the level."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["book_change"] = "book_change"
    venue: str
    market: str
    book_side: Literal["bid", "ask"]
    price: float = Field(gt=0, lt=1)
    size: float = Field(ge=0)
    as_of: datetime
    received_at: datetime | None = Field(
        default=None, description="When this machine received it (recordings only); this machine's clock."
    )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def reconstruct_book(events: Iterable[Book | BookLevelChange]) -> Book | None:
    """Rebuild a market's book from a full book and the level changes after it, in time order.

        reconstruct_book([book_at_10_00, change_at_10_00_01, change_at_10_00_02])

    Returns ``None`` until the first full book arrives. Changes for other markets are ignored.
    """
    current: Book | None = None
    for e in events:
        if isinstance(e, Book):
            current = e
            continue
        if current is None or e.market != current.market or e.venue != current.venue:
            continue
        levels = {lv.price: lv for lv in (current.bids if e.book_side == "bid" else current.asks)}
        if e.size == 0:
            levels.pop(e.price, None)
        else:
            levels[e.price] = Level(price=e.price, size=e.size)
        new = tuple(levels.values())
        current = current.model_copy(
            update={"bids": new, "as_of": e.as_of, "received_at": e.received_at}
            if e.book_side == "bid"
            else {"asks": new, "as_of": e.as_of, "received_at": e.received_at}
        )
        current.model_post_init(None)
    return current
