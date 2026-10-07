"""What every venue's live stream shares: reading one connection, pinging it, and noticing a stall.

A venue's stream (:class:`~uselayer.venues.polymarket_us_stream.PolymarketUSStream`,
:class:`~uselayer.venues.kalshi_stream.KalshiStream`) opens a connection, subscribes, and turns each
message into the SDK's market events. Reconnecting and marking gaps is
:func:`uselayer.record.record_stream`'s job.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from ..events import MarketEvent

PING_S = 5.0
"""How often a quiet connection is pinged, so a drop is dated to within about this many seconds."""

STALL_S = 10.0
"""A pause this much longer than ``idle_s`` between reads means the machine slept or the process froze."""


class LiveStream:
    """The connection-reading half of a venue stream."""

    venue: str

    def __init__(
        self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC), idle_s: float = 1.0
    ) -> None:
        self._clock = clock
        self._idle_s = idle_s
        self.alive_at: datetime | None = None
        """The last moment the current connection was known to be up (a message, or a ping answered).
        After a drop, anything later than this may be missing. ``None`` before a connection is up."""

    def session(self, markets: Sequence[str]) -> Generator[MarketEvent | None, None, None]:
        """Connect, subscribe, then yield events (``None`` when nothing arrives); raises on a drop."""
        raise NotImplementedError

    def _frames(self, ws: Any) -> Iterator[tuple[datetime, str | bytes | None]]:
        """``(received, message)`` forever; the message is ``None`` when ``idle_s`` passes with nothing.

        Pings a quiet connection every :data:`PING_S` and raises ``ConnectionError`` when the machine
        slept or the process froze (whatever the venue sent meanwhile may be lost).
        """
        tick = self._clock()
        if self.alive_at is None:
            self.alive_at = tick
        ping: tuple[datetime, Any] | None = None
        while True:
            try:
                raw: str | bytes | None = ws.recv(timeout=self._idle_s)
            except TimeoutError:
                raw = None
            now = self._clock()
            stalled = (now - tick).total_seconds()
            if stalled > self._idle_s + STALL_S:
                raise ConnectionError(f"this machine was asleep or stalled for {stalled:.0f} s")
            tick = now
            if ping is not None and ping[1].is_set():
                self.alive_at, ping = max(self.alive_at, ping[0]), None
            if ping is None and (now - self.alive_at).total_seconds() >= PING_S and hasattr(ws, "ping"):
                ping = (now, ws.ping())  # answered → the connection was alive at `now`
            if raw is not None:
                self.alive_at = now
            yield now, raw
