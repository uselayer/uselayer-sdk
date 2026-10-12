"""Polymarket international's public order book for one outcome, read without a key.

The one place this package talks to Polymarket's order book host, and only to read a book: there is
no Polymarket international order code. A copy preview walks the asks to price a budget the way a
real order would fill, level by level, instead of assuming everything fills at the best price.
"""

from __future__ import annotations

from ..errors import VenueError
from ..http import Http

BOOK = "https://clob.polymarket.com/book"
VENUE = "polymarket"


class PolymarketBook:
    """Read-only: the asks for one outcome token, cheapest first, as ``(price, contracts)``."""

    def __init__(self, http: Http, url: str = BOOK) -> None:
        self._http = http
        self._url = url

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
