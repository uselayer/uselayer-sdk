"""Prices for a pair: each market's best YES and NO bid and ask, with the size at each, from its book.

    p = client.prices(match)              # a Match from client.matches(), or two (venue, market) pairs
    p.a.yes_ask, p.a.yes_ask_size, p.b.no_ask, p.a.as_of

Each leg is read with :meth:`Client.book`, so it works wherever ``book()`` does: paper and live mode
read the venues with your own keys (reads only, nothing is sent), and backtest mode uses the replayed
books. ``source`` says where a leg's book came from. The object is plain data (:meth:`Prices.to_dict`
and :meth:`Prices.from_dict` round-trip it), so the same call can later be answered from another
source without your code changing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .books import Book, Level

if TYPE_CHECKING:
    from .client import Client


def _level(lv: Level | None) -> tuple[float | None, float | None]:
    return (None, None) if lv is None else (lv.price, lv.size)


def _time(s: Any) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))


@dataclass(frozen=True)
class LegPrices:
    """One market's best prices, in dollars, with the contracts offered at each.

    ``yes_ask`` is the cheapest YES you can buy and ``yes_bid`` the most you can sell YES for; the NO
    prices are the same book seen from the other side (a NO ask at ``1 − yes_bid``). A side with no
    orders is ``None``, with its size. ``as_of`` is the venue's own time for the book; ``read_at`` is when
    this client read it (the replay's time in backtest mode). ``source`` is ``"venue"`` (read just now)
    or ``"recorded"`` (a saved book replayed).
    """

    venue: str
    market: str
    yes_bid: float | None
    yes_bid_size: float | None
    yes_ask: float | None
    yes_ask_size: float | None
    no_bid: float | None
    no_bid_size: float | None
    no_ask: float | None
    no_ask_size: float | None
    as_of: datetime
    read_at: datetime
    source: str

    @classmethod
    def from_book(cls, book: Book, read_at: datetime) -> LegPrices:
        yes, no = book.outcome("yes"), book.outcome("no")
        yb, ybs = _level(yes.best_bid)
        ya, yas = _level(yes.best_ask)
        nb, nbs = _level(no.best_bid)
        na, nas = _level(no.best_ask)
        return cls(
            book.venue, book.market, yb, ybs, ya, yas, nb, nbs, na, nas, book.as_of, read_at, book.source
        )

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["as_of"] = self.as_of.isoformat()
        d["read_at"] = self.read_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LegPrices:
        return cls(**{**d, "as_of": _time(d["as_of"]), "read_at": _time(d["read_at"])})


@dataclass(frozen=True)
class Prices:
    """Both legs of a pair, in the order the pair gave them: ``a`` then ``b``."""

    a: LegPrices
    b: LegPrices

    @property
    def legs(self) -> tuple[LegPrices, LegPrices]:
        return (self.a, self.b)

    def leg(self, venue: str) -> LegPrices:
        """The leg on ``venue``: ``p.leg("kalshi")``. Raises ``KeyError`` when neither or both legs are on it."""
        found = [x for x in self.legs if x.venue == venue]
        if len(found) != 1:
            raise KeyError(venue)
        return found[0]

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Prices:
        return cls(LegPrices.from_dict(d["a"]), LegPrices.from_dict(d["b"]))


def pair_prices(client: Client, pair: Any) -> Prices:
    """Read both legs' books through ``client.book()`` and take the best prices from each."""
    from .trading import legs_of

    (va, ma), (vb, mb) = legs_of(pair)
    book_a = client.book(ma, venue=va)
    read_a = client._now()
    book_b = client.book(mb, venue=vb)
    read_b = client._now()
    return Prices(LegPrices.from_book(book_a, read_a), LegPrices.from_book(book_b, read_b))
