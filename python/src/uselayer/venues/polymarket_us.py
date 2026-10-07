"""Polymarket US: public market data, no key needed.

Reads go to ``gateway.polymarket.us``, which allows 20 requests a second per IP. Its answers are
cached for up to 30 seconds; a book's ``as_of`` is when the venue's servers produced the copy.

Market ids are slugs. A market with two named sides uses ``slug:long`` or ``slug:short``; the short
side's YES is the long side's NO.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..books import Book, Level
from ..errors import VenueError
from ..fill import FeeSettings
from ..http import Http
from .base import BookRead, MarketInfo, Payout

GATEWAY = "https://gateway.polymarket.us"
VENUE = "polymarket_us"


def split_market(market: str) -> tuple[str, bool]:
    """``"slug:short"`` → ``("slug", True)``; ``"slug"`` or ``"slug:long"`` → ``("slug", False)``."""
    if market.endswith(":short"):
        return market[: -len(":short")], True
    if market.endswith(":long"):
        return market[: -len(":long")], False
    return market, False


def _ts(s: str) -> datetime:
    # transactTime has nanoseconds: 2026-10-01T17:07:02.700446463Z. Keep microseconds.
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, _, tz = rest.partition("+")
        s = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s)


def _served_at(h: httpx.Headers) -> tuple[datetime | None, float | None, float | None]:
    """When the venue's servers produced this answer (``Date`` − ``Age``), the copy's age, and the cache's max-age."""
    try:
        date = parsedate_to_datetime(h["date"]) if "date" in h else None
    except (TypeError, ValueError):
        date = None
    try:
        age = float(h.get("age", "0"))
    except ValueError:
        age = 0.0
    max_age = None
    for part in h.get("cache-control", "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "max-age" and v.isdigit():
            max_age = float(v)
    if date is None:
        return None, None, max_age
    return date - timedelta(seconds=age), age, max_age


def _changed(what: str, detail: str, raw: Any) -> VenueError:
    return VenueError(
        "format_changed",
        f"Polymarket US's {what} answer changed format: {detail}",
        venue=VENUE,
        raw=raw,
        retryable=False,
        hint="The venue changed its API. Update uselayer, or report it at github.com/Dave-56/uselayer-sdk/issues.",
        next="pip install -U uselayer",
    )


def _levels(raw: Any, what: str) -> tuple[Level, ...]:
    out = []
    for lv in raw or []:
        try:
            price = float(lv["px"]["value"])
            size = float(lv["qty"])
        except (KeyError, TypeError, ValueError) as e:
            raise _changed(what, f"a level isn't {{px: {{value}}, qty}}: {lv!r}", raw) from e
        if 0 < price < 1 and size > 0:
            out.append(Level(price=price, size=size))
    return tuple(out)


def market_info(m: dict[str, Any]) -> MarketInfo:
    """A MarketInfo from one market in Polymarket US's /v1/markets answer."""
    status = str(m.get("status") or "")
    return MarketInfo(
        venue=VENUE,
        market=str(m["slug"]),
        question=m.get("question"),
        status=status,
        open=bool(m.get("active")) and not m.get("closed") and status in ("MARKET_STATUS_OPEN", ""),
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        min_size=float(m.get("minimumTradeQty") or 1),
        fees=FeeSettings(
            venue=VENUE, coefficient=None if m.get("feeCoefficient") is None else float(m["feeCoefficient"])
        ),
        end_date=m.get("endDate"),
        event_time=m.get("gameStartTime") or m.get("endDate"),
    )


class PolymarketUSPublic:
    """Polymarket US market data from its public gateway.

    pm = PolymarketUSPublic(Http())
    pm.book("some-slug").outcome("yes").best_ask
    """

    venue = VENUE

    def __init__(self, http: Http, base_url: str = GATEWAY) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def market(self, market: str) -> MarketInfo:
        """The market's status, tick size, minimum size and fee coefficient."""
        slug, _ = split_market(market)
        body = self._http.request("GET", f"{self._base}/v1/markets", venue=VENUE, params={"slug": slug})
        ms = body.get("markets") if isinstance(body, dict) else None
        if not isinstance(ms, list):
            raise _changed("/v1/markets", "no markets list", body)
        m = next((x for x in ms if isinstance(x, dict) and x.get("slug") == slug), None)
        if m is None:
            raise VenueError(
                "not_found",
                f"Polymarket US has no market {slug!r}.",
                venue=VENUE,
                retryable=False,
                hint="Market ids are slugs, as in the market's polymarket.us URL.",
                next="Check the slug.",
            )
        return market_info(m)

    def markets(self, *, limit: int = 50, offset: int = 0) -> list[MarketInfo]:
        """Markets the venue lists as active and not closed."""
        body = self._http.request(
            "GET",
            f"{self._base}/v1/markets",
            venue=VENUE,
            params={"limit": limit, "offset": offset, "active": "true", "closed": "false"},
        )
        ms = body.get("markets") if isinstance(body, dict) else None
        if not isinstance(ms, list):
            raise _changed("/v1/markets", "no markets list", body)
        return [market_info(m) for m in ms if isinstance(m, dict) and isinstance(m.get("slug"), str)]

    def book(self, market: str) -> Book:
        """The market's book for its YES side.

        ``as_of`` is when Polymarket US's servers produced this copy (its ``Date`` header minus the
        cache's ``Age``), or the book's own ``transactTime`` when those headers are missing. Both are
        the venue's clock, never the time this machine received the answer.

        For ``slug:short``, the book is turned around so YES means the short side.
        """
        return self.read_book(market).book

    def payout(self, market: str) -> Payout | None:
        """What one YES contract of ``market`` paid once Polymarket US settled it; ``None`` before.

        From ``/v1/markets/<slug>/settlement`` (``1`` = the long side won; a 404 until it settles).
        For ``slug:short``, YES is the short side. The answer carries no time.
        """
        slug, short = split_market(market)
        try:
            body = self._http.request("GET", f"{self._base}/v1/markets/{slug}/settlement", venue=VENUE)
        except VenueError as e:
            if e.code == "not_found":
                return None
            raise
        value = body.get("settlement") if isinstance(body, dict) else None
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                value = None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise _changed("/settlement", "settlement isn't a price from 0 to 1", body)
        return Payout(round(1.0 - value if short else float(value), 6))

    def read_book(self, market: str) -> BookRead:
        """The book, its state (``MARKET_STATE_OPEN``, ``MARKET_STATE_HALTED`` …) and its cache age, from one request."""
        slug, short = split_market(market)
        body, headers = self._http.request_with_headers(
            "GET", f"{self._base}/v1/markets/{slug}/book", venue=VENUE
        )
        md = body.get("marketData") if isinstance(body, dict) else None
        if not isinstance(md, dict):
            raise _changed("/book", "no marketData", body)
        when = md.get("transactTime") or (md.get("stats") or {}).get("lastPriceSample", {}).get("ts")
        if not isinstance(when, str):
            raise _changed("/book", "no transactTime", body)
        changed_at = _ts(when)
        served_at, age, max_age = _served_at(headers)
        as_of = max(changed_at, served_at) if served_at is not None else changed_at
        bids, asks = _levels(md.get("bids"), "/book"), _levels(md.get("offers"), "/book")
        book = Book(venue=VENUE, market=market, bids=bids, asks=asks, as_of=as_of)
        if short:
            no = book.outcome("no")
            book = Book(venue=VENUE, market=market, bids=no.bids, asks=no.asks, as_of=book.as_of)
        state = md.get("state")
        return BookRead(book, state if isinstance(state, str) else None, age, max_age)
