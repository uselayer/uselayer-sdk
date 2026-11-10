"""Polymarket international's public order book for one outcome, read without a key.

The one place this package talks to Polymarket's order book host, and only to read: a book, or an
outcome's price history. There is no Polymarket international order code. A copy preview walks the
asks to price a budget the way a real order would fill, level by level, instead of assuming
everything fills at the best price.
"""

from __future__ import annotations

from ..errors import VenueError
from ..http import Http

BOOK = "https://clob.polymarket.com/book"
PRICES = "https://clob.polymarket.com/prices-history"
VENUE = "polymarket"


class PolymarketBook:
    """Read-only: the asks for one outcome token, cheapest first, as ``(price, contracts)``; and an
    outcome token's price history, about one point a minute."""

    def __init__(self, http: Http, url: str = BOOK, prices_url: str = PRICES) -> None:
        self._http = http
        self._url = url
        self._prices_url = prices_url

    def price_history(self, token_id: str, start: int, end: int) -> list[tuple[int, float]]:
        """An outcome token's price from ``start`` to ``end`` (unix seconds, both included) as
        ``(time, price)``, oldest first, about one a minute: each at the second Polymarket took it, not on
        the minute. Closed markets too. Empty before the market opened and after it stopped trading."""
        body = self._http.request(
            "GET",
            self._prices_url,
            venue=VENUE,
            params={"market": token_id, "startTs": start, "endTs": end, "fidelity": 1},
        )
        rows = body.get("history") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise VenueError(
                "venue_unavailable",
                "Polymarket's price history answered in an unexpected shape.",
                venue=VENUE,
                raw=body,
                next="Try again later.",
            )
        out: list[tuple[int, float]] = []
        for r in rows:
            try:
                out.append((int(r["t"]), float(r["p"])))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(p for p in out if start <= p[0] <= end)

    def asks(self, token_id: str) -> list[tuple[float, float]]:
        body = self._http.request("GET", self._url, venue=VENUE, params={"token_id": token_id})
        if not isinstance(body, dict) or not isinstance(body.get("asks"), list):
            raise VenueError(
                "venue_unavailable",
                "Polymarket's order book answered in an unexpected shape.",
                venue=VENUE,
                raw=body,
                next="Try again later.",
            )
        out: list[tuple[float, float]] = []
        for lvl in body["asks"]:
            try:
                price, size = float(lvl["price"]), float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < price < 1 and size > 0:
                out.append((price, size))
        return sorted(out)
