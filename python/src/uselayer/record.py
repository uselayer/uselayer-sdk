"""Record your own ticks: every book change and trade from a venue's live stream, to a file on your machine.

:func:`~uselayer.backtest.record_books` saves one snapshot each time you call it. :func:`record_stream`
keeps a connection open and saves everything the venue sends, in the SDK's event format (one JSON
object per line), so you build tick history from day one. The file loads straight into backtest mode:

    from uselayer.record import record_stream
    from uselayer.backtest import load_books

    record_stream(["slug-a", "KXSAMPLE-26OCT04-T50"], "ticks.jsonl", duration_s=3600)   # your own keys
    Client(mode="backtest", books=load_books("ticks.jsonl")).replay(on_book)

Or from a terminal, with live progress:
``python -m uselayer record slug-a KXSAMPLE-26OCT04-T50 --out ticks.jsonl --minutes 60``.

Venues: Polymarket US (slugs, or ``slug:short``) and Kalshi (tickers, in capitals). One list can mix
them; each venue gets its own connection, signed with your own key for that venue.

What it writes, the same for both venues:

- ``book``: the whole book each time its levels change (an unchanged book isn't written again).
  Kalshi sends one book and then each change; the recorder applies the change and writes the whole
  book, so a Kalshi recording reads like a Polymarket US one.
- ``trade``: every trade, with the venue's trade id and whether the taker bought or sold YES.
- ``status``: when a market opens, pauses, halts or closes. ``resolution``: when Kalshi settles one.
- ``gap``: when the connection dropped. It reconnects on its own (waiting 1, 2, 4 … up to 30 s) and
  writes one ``gap`` per market of that venue from the last moment the connection was known to be up
  (its last message, or a ping it answered; quiet connections are pinged every 5 s) to when the
  market was subscribed again. If this machine sleeps or the process freezes, that counts as a drop
  too. Kalshi numbers every message (``seq``): a skipped number means a message was lost, so that is
  a gap as well, and the recorder subscribes again at once for fresh books. In backtest mode the
  market has no book during a gap.

Every event carries ``received_at`` (this machine's clock) beside ``as_of`` (the venue's). Nothing
is sent anywhere but the venues; the file stays on your machine.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from websockets.exceptions import WebSocketException

from .books import Book
from .errors import VenueError
from .events import MarketEvent, MarketStatus, Resolution, StreamGap, TradePrint
from .http import Http
from .venues.kalshi import Kalshi, KalshiLive
from .venues.kalshi_stream import KalshiStream, SequenceGap
from .venues.polymarket_us import PolymarketUSPublic
from .venues.polymarket_us_live import PolymarketUS
from .venues.polymarket_us_stream import PolymarketUSStream
from .venues.stream import LiveStream

STREAM_VENUES = ("polymarket_us", "kalshi")
"""Venues :func:`record_stream` can record in this release."""

_MAX_WAIT_S = 30.0


@dataclass
class RecordSummary:
    """What a recording wrote."""

    path: str
    venue: str
    """The venue, or ``"kalshi+polymarket_us"`` when one recording covered both."""
    markets: list[str]
    started_at: datetime
    ended_at: datetime | None = None
    books: int = 0
    trades: int = 0
    statuses: int = 0
    gaps: list[StreamGap] = field(default_factory=list)
    reconnects: int = 0
    resolutions: int = 0

    @property
    def events(self) -> int:
        return self.books + self.trades + self.statuses + self.resolutions + len(self.gaps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "venue": self.venue,
            "markets": self.markets,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "books": self.books,
            "trades": self.trades,
            "statuses": self.statuses,
            "resolutions": self.resolutions,
            "gaps": [g.to_dict() for g in self.gaps],
            "reconnects": self.reconnects,
        }


def market_venue(market: str) -> str:
    """The venue a market id belongs to: Kalshi tickers are in capitals, Polymarket US slugs aren't."""
    return "kalshi" if market == market.upper() and any(c.isalpha() for c in market) else "polymarket_us"


def record_stream(
    markets: Sequence[str],
    path: str | Path,
    *,
    venue: str | None = None,
    polymarket_us: PolymarketUS | None = None,
    kalshi: Kalshi | None = None,
    duration_s: float | None = None,
    until: datetime | None = None,
    max_events: int | None = None,
    stop: threading.Event | None = None,
    on_event: Callable[[MarketEvent], None] | None = None,
    on_alert: Callable[[dict[str, Any]], None] | None = None,
    check_markets: bool = True,
    transport: httpx.BaseTransport | None = None,
    ws_connect: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RecordSummary:
    """Record every book change and trade for ``markets`` to ``path`` (appended, one JSON event per line).

    Runs until ``duration_s`` passes, ``until`` arrives, ``max_events`` are written, ``stop`` is set
    or you press Ctrl-C; with none of them it runs until stopped. Returns what it wrote.

        record_stream(["some-slug", "KXSAMPLE-26OCT04-T50"], "ticks.jsonl", duration_s=3600)

    Args:
        markets: Polymarket US slugs (or ``slug:short``) and Kalshi tickers (in capitals), in any
            mix. Each is checked with its venue first, since a stream can silently ignore a market
            it doesn't know.
        venue: ``"polymarket_us"`` or ``"kalshi"`` to read every id as that venue's. By default each
            id's venue is told from the id (:func:`market_venue`).
        polymarket_us: your Polymarket US key; defaults to ``POLYMARKET_US_KEY_ID`` and
            ``POLYMARKET_US_SECRET_KEY``.
        kalshi: your Kalshi key; defaults to :meth:`Kalshi.from_env` (``KALSHI_KEY_ID`` and
            ``KALSHI_PRIVATE_KEY_PATH``). A read-only key is enough. Each venue's stream needs a key
            even though nothing is traded.
        on_event: called with each event as it's written (for progress, or to act on ticks live).
        on_alert: called with a dict when a connection drops or comes back (``venue`` says which).
        ws_connect: opens a WebSocket (for tests); it's given each venue's URL.
    """
    if venue is not None and venue not in STREAM_VENUES:
        raise VenueError(
            "not_available",
            f"record_stream() can't record {venue!r} in this release.",
            venue=venue,
            retryable=False,
            hint=f"It records {', '.join(STREAM_VENUES)}. For other venues, save snapshots with record_books().",
            next="record_stream(markets, path)",
        )
    markets = list(dict.fromkeys(markets))
    if not markets:
        raise VenueError("invalid_order", "record_stream() needs at least one market.", retryable=False)
    by_venue: dict[str, list[str]] = {}
    for m in markets:
        by_venue.setdefault(venue or market_venue(m), []).append(m)
    now = clock or (lambda: datetime.now(UTC))
    streams: list[tuple[LiveStream, list[str]]] = []
    for v in sorted(by_venue):
        ids = by_venue[v]
        if v == "kalshi":
            k = kalshi or _kalshi_key()
            if check_markets:
                rest = KalshiLive(Http(transport=transport), k)
                for m in ids:
                    rest.market(m)  # raises not_found for an unknown ticker
            streams.append((KalshiStream(k, ws_connect=ws_connect, clock=now), ids))
        else:
            pm = polymarket_us or _polymarket_us_key()
            if check_markets:
                public = PolymarketUSPublic(Http(transport=transport))
                for m in ids:
                    public.market(m)  # raises not_found for an unknown slug
            streams.append((PolymarketUSStream(pm, ws_connect=ws_connect, clock=now), ids))
    summary = RecordSummary(str(path), "+".join(sorted(by_venue)), markets, now())
    run = _Run(Path(path), summary, now, stop, on_event, duration_s, until, max_events)
    return run.go([_Recorder(s, ids, run, sleep, on_alert) for s, ids in streams])


def _polymarket_us_key() -> PolymarketUS:
    if not os.environ.get("POLYMARKET_US_KEY_ID"):
        raise VenueError(
            "auth_failed",
            "Polymarket US's stream needs your API key, even to read.",
            venue="polymarket_us",
            retryable=False,
            hint="Create a key at polymarket.us/developer. It only signs the connection; nothing is traded.",
            next="record_stream(..., polymarket_us=PolymarketUS(key_id=..., secret_key_path=...))",
        )
    return PolymarketUS.from_env()


def _kalshi_key() -> Kalshi:
    if not os.environ.get("KALSHI_KEY_ID"):
        raise VenueError(
            "auth_failed",
            "Kalshi's stream needs your API key, even to read.",
            venue="kalshi",
            retryable=False,
            hint="Create a key in your Kalshi account settings (read-only is enough). It only signs the "
            "connection; nothing is traded.",
            next="record_stream(..., kalshi=Kalshi(key_id=..., private_key_path=...))",
        )
    return Kalshi.from_env()


class _Run:
    """One recording: the file, the summary and when to stop, shared by each venue's recorder."""

    def __init__(
        self,
        path: Path,
        summary: RecordSummary,
        clock: Callable[[], datetime],
        stop: threading.Event | None,
        on_event: Callable[[MarketEvent], None] | None,
        duration_s: float | None,
        until: datetime | None,
        max_events: int | None,
    ) -> None:
        self.path, self.summary, self.clock, self.stop = path, summary, clock, stop
        self.on_event = on_event or (lambda _e: None)
        started = summary.started_at
        ends = [t for t in (until, started + timedelta(seconds=duration_s) if duration_s else None) if t]
        self.deadline = min(ends) if ends else None
        self.max_events = max_events
        self.halt = threading.Event()  # set when one venue's recorder fails, or on Ctrl-C
        self.lock = threading.Lock()

    def done(self) -> bool:
        return (
            self.halt.is_set()
            or (self.stop is not None and self.stop.is_set())
            or (self.deadline is not None and self.clock() >= self.deadline)
            or (self.max_events is not None and self.summary.events >= self.max_events)
        )

    def wait(self, seconds: float) -> None:
        """Sleep, waking early when the recording should stop."""
        end = time.monotonic() + seconds
        while not self.done() and (left := end - time.monotonic()) > 0:
            (self.stop or self.halt).wait(min(left, 0.5))

    def go(self, recorders: list[_Recorder]) -> RecordSummary:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", buffering=1) as f:
            self.f = f
            try:
                if len(recorders) == 1:
                    recorders[0].loop()
                else:
                    self._threads(recorders)
            except KeyboardInterrupt:
                pass
            finally:
                for r in recorders:
                    r.finish()
                self.summary.ended_at = self.clock()
        return self.summary

    def _threads(self, recorders: list[_Recorder]) -> None:
        """One thread per venue. The first error stops them all and is raised here."""
        errors: list[BaseException] = []

        def work(r: _Recorder) -> None:
            try:
                r.loop()
            except BaseException as e:  # raised again in the calling thread
                errors.append(e)
                self.halt.set()

        threads = [threading.Thread(target=work, args=(r,), daemon=True) for r in recorders]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(0.2)
        finally:
            self.halt.set()  # after Ctrl-C too: each thread stops within about a second
            for t in threads:
                t.join()
        if errors:
            raise errors[0]

    def emit(self, e: MarketEvent) -> None:
        with self.lock:
            self.f.write(json.dumps(e.to_dict()) + "\n")
            self.on_event(e)


class _Recorder:
    """One venue's connection: reconnects, marks gaps and leaves out repeats."""

    def __init__(
        self,
        stream: LiveStream,
        markets: list[str],
        run: _Run,
        sleep: Callable[[float], None] | None,
        on_alert: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self.stream, self.markets, self.run = stream, markets, run
        self.clock, self.done = run.clock, run.done
        self.sleep = sleep or run.wait
        self.on_alert = on_alert or (lambda _e: None)
        self.last_book: dict[str, tuple[Any, Any]] = {}
        self.last_status: dict[str, str] = {}
        self.trade_ids: deque[str] = deque(maxlen=10_000)
        self.down_since: datetime | None = None
        self.down_reason = ""
        self.last_as_of: datetime | None = None

    def finish(self) -> None:
        if self.down_since is not None:
            self._close_gap(f"{self.down_reason}; the recording ended before it reconnected")

    def loop(self) -> None:
        wait = 1.0
        venue = self.stream.venue
        while not self.done():
            pause: float | None = None
            try:
                with contextlib.closing(self.stream.session(self.markets)) as session:
                    for e in session:
                        if self.down_since is not None:
                            with self.run.lock:
                                self.run.summary.reconnects += 1
                            self._close_gap(self.down_reason)
                            self.on_alert({"kind": "stream_reconnected", "venue": venue})
                        wait = 1.0
                        if e is not None:
                            self._write(e)
                        if self.done():
                            return  # closing the session closes the connection
                raise ConnectionError("the stream ended")  # pragma: no cover (a session ends by raising)
            except (OSError, TimeoutError, WebSocketException, json.JSONDecodeError, VenueError) as err:
                # Network trouble, a closed socket, a lost message or a garbled one: reconnect.
                # Anything else is a bug.
                if isinstance(err, VenueError) and not err.retryable:
                    raise
                _raise_if_refused(err, venue)
                if isinstance(err, SequenceGap) and wait == 1.0:
                    pause = 0.0  # the connection itself is fine: subscribe again at once for fresh books
                if self.down_since is None:
                    # The gap starts when the connection was last known to be up, not when the drop was
                    # noticed (a dead socket or a sleeping machine can go unnoticed for a while), and
                    # never before something already written.
                    alive = self.stream.alive_at
                    self.down_since = self.clock() if alive is None else max(alive, self.last_as_of or alive)
                    what = "missed messages" if isinstance(err, SequenceGap) else "disconnected"
                    self.down_reason = f"{what}: {type(err).__name__}: {str(err)[:200]}".rstrip(": ")
                    self.on_alert({"kind": "stream_disconnected", "venue": venue, "error": str(err)[:300]})
            if self.done():
                return
            if pause is None:
                pause = wait
                wait = min(wait * 2, _MAX_WAIT_S)
            if self.run.deadline is not None:
                pause = min(pause, max(0.0, (self.run.deadline - self.clock()).total_seconds()))
            if pause > 0:
                self.sleep(pause)

    def _close_gap(self, reason: str) -> None:
        assert self.down_since is not None
        end = max(self.clock(), self.down_since)
        for m in self.markets:
            gap = StreamGap(
                venue=self.stream.venue, market=m, as_of=self.down_since, until=end, reason=reason
            )
            with self.run.lock:
                self.run.summary.gaps.append(gap)
            self.run.emit(gap)
        self.down_since = None
        # After a gap the next book is written even if it looks the same: the replay forgot the old one.
        self.last_book.clear()
        self.last_status.clear()

    def _write(self, e: MarketEvent) -> None:
        s, lock = self.run.summary, self.run.lock
        if isinstance(e, Book):
            levels = (e.bids, e.asks)
            if self.last_book.get(e.market) == levels:
                return
            self.last_book[e.market] = levels
            with lock:
                s.books += 1
        elif isinstance(e, MarketStatus):
            if self.last_status.get(e.market) == e.status:
                return
            self.last_status[e.market] = e.status
            with lock:
                s.statuses += 1
        elif isinstance(e, TradePrint):
            if e.trade_id is not None:
                uid = f"{e.market}:{e.trade_id}"
                if uid in self.trade_ids:
                    return
                self.trade_ids.append(uid)
            with lock:
                s.trades += 1
        elif isinstance(e, Resolution):
            with lock:
                s.resolutions += 1
        self.last_as_of = e.as_of if self.last_as_of is None else max(self.last_as_of, e.as_of)
        self.run.emit(e)


_REFUSED: dict[str, tuple[str, str, str]] = {
    "polymarket_us": (
        "Polymarket US",
        "Check the Key ID and Secret Key from polymarket.us/developer.",
        "PolymarketUS(key_id=..., secret_key_path=...)",
    ),
    "kalshi": (
        "Kalshi",
        "Check the key id and private key. A key works only on the exchange (production or demo) that made it.",
        "Kalshi(key_id=..., private_key_path=...)",
    ),
}


def _raise_if_refused(err: Exception, venue: str) -> None:
    """A rejected key won't get better by reconnecting: stop with a clear error."""
    status = getattr(getattr(err, "response", None), "status_code", None)
    if status in (401, 403):
        name, hint, nxt = _REFUSED[venue]
        raise VenueError(
            "auth_failed",
            f"{name} refused the stream connection ({status}).",
            venue=venue,
            retryable=False,
            hint=hint,
            next=nxt,
        ) from err
