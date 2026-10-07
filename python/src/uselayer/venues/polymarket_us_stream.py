"""Polymarket US's live market stream: every book change and trade, as the SDK's market events.

One connection to ``wss://api.polymarket.us/v1/ws/markets`` (signed with your key) subscribes to
full books (``SUBSCRIPTION_TYPE_MARKET_DATA``) and trades (``SUBSCRIPTION_TYPE_TRADE``), at most 100
markets per subscription. Every book message is the whole book, so nothing has to be rebuilt.

Times: a book's ``as_of`` is the venue's ``transactTime``. The first book after subscribing carries
the time of the market's last change, which can be minutes old, so it is stamped with the venue's
``Date`` from the connection's handshake instead (the book was current then). A trade's ``as_of`` is
its ``tradeTime``. Every event also carries ``received_at``, this machine's clock.

Used by :func:`uselayer.record.record_stream`; the parsing is here so each venue keeps its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterator, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from ..books import Book
from ..errors import VenueError
from ..events import MarketEvent, MarketStatus, TradePrint
from .polymarket_us import VENUE, _changed, _levels, _ts, split_market
from .polymarket_us_live import WS_MARKETS, PolymarketUS, Signer, _amount
from .stream import PING_S, STALL_S, LiveStream

__all__ = ["PING_S", "STALL_S", "SUBSCRIPTION_LIMIT", "PolymarketUSStream"]

SUBSCRIPTION_LIMIT = 100
"""Polymarket US takes at most 100 markets in one subscription (and ignores the rest silently)."""

_STATUS: dict[str, Literal["open", "paused", "closed", "halted"]] = {
    "MARKET_STATE_OPEN": "open",
    "MARKET_STATE_PAUSED": "paused",
    "MARKET_STATE_SUSPENDED": "paused",
    "MARKET_STATE_HALTED": "halted",
    "MARKET_STATE_CLOSED": "closed",
    "MARKET_STATE_EXPIRED": "closed",
    "MARKET_STATE_SETTLED": "closed",
}


class PolymarketUSStream(LiveStream):
    """Polymarket US's market stream for a list of markets (``slug`` or ``slug:short``).

    stream = PolymarketUSStream(PolymarketUS.from_env())
    for event in stream.session(["some-slug"]):   # None about once a second when nothing arrives
        ...

    A session is one connection. It ends by raising when the connection drops; reconnecting and
    marking the gap is :func:`uselayer.record.record_stream`'s job.
    """

    venue = VENUE

    def __init__(
        self,
        key: PolymarketUS,
        *,
        ws_url: str = WS_MARKETS,
        ws_connect: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        idle_s: float = 1.0,
    ) -> None:
        super().__init__(clock=clock, idle_s=idle_s)
        self._signer = Signer(key)
        self._ws_url = ws_url
        self._ws_connect = ws_connect

    def session(self, markets: Sequence[str]) -> Generator[MarketEvent | None, None, None]:
        """Connect, subscribe, then yield events as they arrive.

        Yields ``None`` once right after subscribing and whenever ``idle_s`` passes with nothing
        received, so the caller can stop between messages. Raises when the connection drops.
        """
        self.alive_at = None
        by_slug: dict[str, list[str]] = {}
        for m in markets:
            by_slug.setdefault(split_market(m)[0], []).append(m)
        connect = self._ws_connect
        if connect is None:
            from websockets.sync.client import connect as ws_connect

            connect = ws_connect
        headers = self._signer.headers("GET", "/v1/ws/markets")
        headers.pop("Content-Type", None)
        with connect(self._ws_url, additional_headers=headers, open_timeout=10, close_timeout=1) as ws:
            served_at = _date_header(ws)
            slugs = list(by_slug)
            for i in range(0, len(slugs), SUBSCRIPTION_LIMIT):
                chunk = slugs[i : i + SUBSCRIPTION_LIMIT]
                for kind in ("MARKET_DATA", "TRADE"):
                    ws.send(
                        json.dumps(
                            {
                                "subscribe": {
                                    "requestId": f"uselayer-record-{kind.lower()}-{i // SUBSCRIPTION_LIMIT}",
                                    "subscriptionType": f"SUBSCRIPTION_TYPE_{kind}",
                                    "marketSlugs": chunk,
                                    "responsesDebounced": False,
                                }
                            }
                        )
                    )
            parser = _Parser(by_slug, served_at)
            self.alive_at = self._clock()
            yield None
            for now, raw in self._frames(ws):
                if raw is None:
                    yield None
                    continue
                yield from parser.parse(json.loads(raw), now)


def _date_header(ws: Any) -> datetime | None:
    resp = getattr(ws, "response", None)
    date = resp.headers.get("Date") if resp is not None else None
    if not date:
        return None
    try:
        return parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return None


class _Parser:
    """Turns one connection's messages into events. Keeps each market's times moving forward."""

    def __init__(self, by_slug: dict[str, list[str]], served_at: datetime | None) -> None:
        self._by_slug = by_slug
        self._served_at = served_at
        self._last: dict[str, datetime] = {}

    def parse(self, msg: dict[str, Any], received: datetime) -> Iterator[MarketEvent]:
        if msg.get("error"):
            raise VenueError(
                "venue_unavailable", f"Polymarket US stream: {msg['error']}", venue=VENUE, raw=msg
            )
        try:
            out: list[MarketEvent] = []
            md = msg.get("marketData")
            if isinstance(md, dict):
                out.extend(self._book(md, received))
            trade = msg.get("trade")
            if isinstance(trade, dict):
                out.extend(self._trade(trade, received))
        except (KeyError, TypeError, ValueError) as e:  # a bad time or a level outside 0..1
            raise _changed("stream", f"{type(e).__name__}: {str(e)[:200]}", msg) from e
        yield from out

    def _book(self, md: dict[str, Any], received: datetime) -> Iterator[MarketEvent]:
        slug = md.get("marketSlug")
        if not isinstance(slug, str) or slug not in self._by_slug:
            return
        changed = _ts(md["transactTime"]) if isinstance(md.get("transactTime"), str) else None
        prev = self._last.get(slug)
        if prev is None:
            # The subscription's first book: current as of the handshake, whatever its last change was.
            known = [t for t in (changed, self._served_at) if t is not None]
            as_of = max(known) if known else received
        else:
            as_of = max(changed or received, prev)
        self._last[slug] = as_of
        bids, asks = _levels(md.get("bids"), "stream"), _levels(md.get("offers"), "stream")
        status = _STATUS.get(str(md.get("state") or ""))
        for market in self._by_slug[slug]:
            book = Book(venue=VENUE, market=market, bids=bids, asks=asks, as_of=as_of, received_at=received)
            if split_market(market)[1]:
                no = book.outcome("no")
                book = book.model_copy(update={"bids": no.bids, "asks": no.asks})
            yield book
            if status is not None:
                yield MarketStatus(
                    venue=VENUE, market=market, status=status, as_of=as_of, received_at=received
                )

    def _trade(self, t: dict[str, Any], received: datetime) -> Iterator[MarketEvent]:
        slug = t.get("marketSlug")
        if not isinstance(slug, str) or slug not in self._by_slug:
            return
        if t.get("state") not in (None, "TRADE_STATE_NEW"):
            return  # only trades as they print; a later correction isn't a new trade
        price, size = _amount(t.get("price")), _amount(t.get("quantity"))
        when = t.get("tradeTime")
        if price is None or size is None or size <= 0 or not 0 < price < 1 or not isinstance(when, str):
            return
        taker = str((t.get("taker") or {}).get("side") or "")
        aggressor: Literal["buy", "sell"] | None = (
            "buy" if taker == "ORDER_SIDE_BUY" else "sell" if taker == "ORDER_SIDE_SELL" else None
        )
        for market in self._by_slug[slug]:
            short = split_market(market)[1]
            yield TradePrint(
                venue=VENUE,
                market=market,
                price=round(1 - price, 6) if short else price,
                size=size,
                as_of=_ts(when),
                trade_id=str(t["id"]) if t.get("id") else None,
                aggressor=_flip(aggressor) if short else aggressor,
                received_at=received,
            )


def _flip(a: Literal["buy", "sell"] | None) -> Literal["buy", "sell"] | None:
    return None if a is None else "sell" if a == "buy" else "buy"
