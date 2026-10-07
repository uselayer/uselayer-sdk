"""Kalshi's live market stream with your own key: every book change and trade, as the SDK's market events.

One connection to Kalshi's market-data WebSocket (signed with your key, like a REST call) subscribes
to the markets' books (``orderbook_delta``), trades (``trade``) and opening, pausing and settling
(``market_lifecycle_v2``). Kalshi sends each book once (``orderbook_snapshot``), then one
``orderbook_delta`` per change; this stream applies each delta and yields the whole book, so a
recording holds full books, like Polymarket US's.

Messages are read with :mod:`uselayer.imports`' Kalshi mapping, so a recorded file and an imported
file of the same messages hold the same events.

Every message of a subscription carries ``sid`` and ``seq``, and ``seq`` goes up by one. A jump
means a message was lost, so the books can't be trusted: the session raises :class:`SequenceGap`,
and :func:`uselayer.record.record_stream` writes a gap and subscribes again for fresh books.

Times: a book's ``as_of`` is Kalshi's time for the change (``ts_ms``), or for the first book the
time Kalshi sent it. A trade's is its ``ts_ms``. Every event also carries ``received_at``, this
machine's clock. Kalshi doesn't ping, so a quiet connection is pinged from here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime
from typing import Any

from ..books import Book, BookLevelChange
from ..errors import VenueError
from ..events import MarketEvent
from ..imports import _kalshi_book, _kalshi_message, _KalshiBook, _Sink
from .kalshi import VENUE, WS_HOSTS, WS_PATH, Kalshi, Signer
from .stream import LiveStream

BOOK_CHANNELS = ["orderbook_delta"]
OTHER_CHANNELS = ["trade", "market_lifecycle_v2"]

# A message the mapping can't apply means messages were lost (resubscribe), not that the format changed.
_RESYNC = {"sequence_gap", "impossible", "no_starting_book"}


class SequenceGap(ConnectionError):
    """Kalshi's ``seq`` jumped, or a change didn't fit the book: messages were lost on this connection."""


class KalshiStream(LiveStream):
    """Kalshi's market stream for a list of tickers.

    stream = KalshiStream(Kalshi.from_env())
    for event in stream.session(["KXSAMPLE-26OCT04-T50"]):   # None about once a second when nothing arrives
        ...

    A session is one connection. It ends by raising when the connection drops or a message is lost;
    reconnecting and marking the gap is :func:`uselayer.record.record_stream`'s job.
    """

    venue = VENUE

    def __init__(
        self,
        key: Kalshi,
        *,
        ws_url: str | None = None,
        ws_connect: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        idle_s: float = 1.0,
    ) -> None:
        super().__init__(clock=clock, idle_s=idle_s)
        self._signer = Signer(key)
        self._ws_url = ws_url or WS_HOSTS[key.environment] + WS_PATH
        self._ws_connect = ws_connect

    def session(self, markets: Sequence[str]) -> Generator[MarketEvent | None, None, None]:
        """Connect, subscribe, then yield events as they arrive.

        Yields ``None`` once right after subscribing and whenever ``idle_s`` passes with nothing
        received, so the caller can stop between messages. Raises when the connection drops, and
        :class:`SequenceGap` when a message was lost.
        """
        self.alive_at = None
        tickers = list(dict.fromkeys(markets))
        connect = self._ws_connect
        if connect is None:
            from websockets.sync.client import connect as ws_connect

            connect = ws_connect
        headers = self._signer.headers("GET", WS_PATH)
        # No compression: inflating each small message costs more CPU than the bytes save.
        with connect(
            self._ws_url,
            additional_headers=headers,
            open_timeout=10,
            close_timeout=1,
            compression=None,
            max_size=None,
        ) as ws:
            for i, channels in enumerate((BOOK_CHANNELS, OTHER_CHANNELS), start=1):
                ws.send(
                    json.dumps(
                        {
                            "id": i,
                            "cmd": "subscribe",
                            "params": {"channels": channels, "market_tickers": tickers},
                        }
                    )
                )
            parser = _Parser(set(tickers))
            self.alive_at = self._clock()
            yield None
            last_message = self.alive_at
            for now, raw in self._frames(ws):
                if raw is None:
                    yield None
                    continue
                try:
                    events = parser.parse(json.loads(raw), now)
                except SequenceGap:
                    # Lost before this message: the stream was last known complete at the one before.
                    self.alive_at = last_message
                    raise
                last_message = now
                yield from events


class _Parser:
    """One connection's books, rebuilt from Kalshi's snapshot and deltas with the import mapping."""

    def __init__(self, tickers: set[str]) -> None:
        self._tickers = tickers
        self._books: dict[str, _KalshiBook] = {}
        self._seqs: dict[Any, int] = {}
        self._other_seqs: dict[Any, int] = {}
        self._sink = _Sink()
        self._last: dict[str, datetime] = {}
        self._n = 0

    def _keep(self, ticker: str, _event: str) -> bool:
        return ticker in self._tickers

    def parse(self, msg: Any, received: datetime) -> list[MarketEvent]:
        if isinstance(msg, dict) and msg.get("type") == "error":
            body = msg.get("msg") or {}
            raise VenueError(
                "venue_unavailable",
                f"Kalshi stream: error {body.get('code')}: {body.get('msg')}",
                venue=VENUE,
                raw=msg,
            )
        self._n += 1
        if isinstance(msg, dict) and msg.get("type") not in ("orderbook_snapshot", "orderbook_delta"):
            # The mapping checks the book subscription's seq; trades and lifecycle are checked here.
            sid, seq = msg.get("sid"), msg.get("seq")
            if isinstance(seq, int):
                last = self._other_seqs.get(sid)
                self._other_seqs[sid] = seq
                if last is not None and seq != last + 1:
                    raise SequenceGap(f"Kalshi sequence jumped from {last} to {seq} on subscription {sid}.")
        sink = self._sink
        _kalshi_message(msg, self._n, received, sink, self._keep, self._books, self._seqs)
        problems, events = sink.report.problems, sink.events
        sink.report.problems, sink.events, sink.rows, sink.files = [], [], [], []
        for p in problems:
            if p.kind in _RESYNC:
                raise SequenceGap(p.message)
            raise VenueError(
                "format_changed",
                f"Kalshi's stream changed format: {p.message}",
                venue=VENUE,
                raw=msg,
                retryable=False,
                hint="The venue changed its API. Update uselayer, or report it at github.com/Dave-56/uselayer-sdk/issues.",
                next="pip install -U uselayer",
            )
        return [self._whole(e, received) for e in events]

    def _whole(self, e: MarketEvent, received: datetime) -> MarketEvent:
        """A snapshot or a change → the market's whole book, its time never going backwards."""
        if not isinstance(e, (Book, BookLevelChange)):
            return e
        prev = self._last.get(e.market)
        at = e.as_of if prev is None else max(e.as_of, prev)
        self._last[e.market] = at
        return _kalshi_book(e.market, self._books[e.market], at, received, source="venue")
