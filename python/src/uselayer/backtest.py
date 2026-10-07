"""Backtest helpers: replay books through the same fill model, rules and store as paper mode.

``Client(mode="backtest", books=...)`` replays books you supply: snapshots you saved with
:func:`record_books`, or every tick recorded with :func:`~uselayer.record.record_stream`. Replaying
Layer's own recorded history is switched off in this release.

    books = [Book(...), Book(...)]
    bt = Client(mode="backtest", books=books)
    bt.replay(lambda client, book: ...)
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import _switches
from .books import Book, BookLevelChange
from .errors import VenueError
from .events import MarketEvent, MarketStatus, Resolution, StreamGap, TradePrint
from .record import RecordSummary, record_stream

__all__ = [
    "RecordSummary",
    "layer_history",
    "load_books",
    "record_books",
    "record_stream",
    "save_events",
]

_KINDS: dict[str, type[BaseModel]] = {
    "book": Book,
    "book_change": BookLevelChange,
    "trade": TradePrint,
    "status": MarketStatus,
    "resolution": Resolution,
    "gap": StreamGap,
}


def layer_history(market: str, *, start: datetime, end: datetime) -> Iterator[MarketEvent]:
    """Layer's recorded books for a market. Not available in this release.

    The plumbing for replaying Layer's recordings is built, but serving them is switched off.
    """
    if not _switches.LAYER_HISTORY:
        raise VenueError(
            "not_available",
            "Replaying Layer's recorded books isn't available in this release.",
            retryable=False,
            hint="Backtest with books you record yourself: uselayer.backtest.record_stream(...) or record_books(...).",
            next="Client(mode='backtest', books=load_books('books.jsonl'))",
        )
    raise AssertionError("unreachable")  # pragma: no cover


def save_events(events: Iterable[MarketEvent], path: str | Path) -> int:
    """Append market events to a JSON-lines file, one per line. Returns how many were written.

    save_events([client.book("some-slug")], "books.jsonl")
    """
    n = 0
    with Path(path).open("a") as f:
        for e in events:
            f.write(json.dumps(e.to_dict()) + "\n")
            n += 1
    return n


def load_books(path: str | Path) -> list[MarketEvent]:
    """Read market events saved with :func:`save_events`, oldest first.

    bt = Client(mode="backtest", books=load_books("books.jsonl"))
    """
    out: list[MarketEvent] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        cls = _KINDS.get(d.get("kind", "book"))
        if cls is None:
            continue
        e = cls.model_validate(d)
        assert isinstance(e, (Book, BookLevelChange, TradePrint, MarketStatus, Resolution, StreamGap))
        if isinstance(e, Book):
            e = e.model_copy(update={"source": "recorded"})
        out.append(e)
    return sorted(out, key=lambda e: e.as_of)


def record_books(client: Any, markets: list[str], path: str | Path, *, venue: str = "polymarket_us") -> int:
    """Read each market's current book once and append it to ``path``. Call it on a schedule to build a history.

    To save every change instead of one snapshot per call, use :func:`~uselayer.record.record_stream`.

    record_books(client, ["some-slug"], "books.jsonl")
    """
    book = client.book
    return save_events((book(m, venue=venue) for m in markets), path)
