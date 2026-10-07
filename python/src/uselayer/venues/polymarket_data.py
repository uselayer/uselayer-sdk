"""Polymarket's public trader data, read without a key: leaderboard, wallet stats, positions, trades.

Polymarket (international) settles on-chain, so every trade belongs to a public wallet. These are
reads only: this package has no Polymarket international trading code. A copied Polymarket trade is
placed on its twin market on Kalshi or Polymarket US, found through Layer's matching.

Money is in USDC (dollars). ``size`` on a trade is outcome tokens (contracts), ``price`` is the price
of the outcome that was traded, and ``outcomeIndex`` says which outcome (0 or 1).
"""

from __future__ import annotations

from typing import Any

from ..errors import VenueError
from ..http import Http

DATA = "https://data-api.polymarket.com"
PROFILES = "https://gamma-api.polymarket.com"
VENUE = "polymarket"
ORDER_BY = ("PNL", "VOL")
PERIODS = ("DAY", "WEEK", "MONTH", "ALL")


class PolymarketData:
    """Read-only client for Polymarket's public data and profile APIs. Needs no key."""

    def __init__(self, http: Http, data_url: str = DATA, profiles_url: str = PROFILES) -> None:
        self._http = http
        self._data = data_url.rstrip("/")
        self._profiles = profiles_url.rstrip("/")

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        return self._http.request("GET", url, venue=VENUE, params=params)

    def _list(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        body = self._get(url, params)
        if not isinstance(body, list):
            raise VenueError(
                "venue_unavailable",
                f"Polymarket's {url.split('.com', 1)[-1]} answered in an unexpected shape.",
                venue=VENUE,
                raw=body,
                next="Try again later.",
            )
        return body

    def leaderboard(
        self, order_by: str = "PNL", period: str = "ALL", limit: int = 25
    ) -> list[dict[str, Any]]:
        if order_by not in ORDER_BY or period not in PERIODS:
            raise ValueError(f"order_by must be one of {ORDER_BY} and period one of {PERIODS}")
        return self._list(
            self._data + "/v1/leaderboard", {"timePeriod": period, "orderBy": order_by, "limit": limit}
        )

    def stats(self, wallet: str) -> dict[str, Any]:
        body = self._get(self._data + "/v2/user-stats", {"user": wallet})
        return dict((body or {}).get("data") or {}) if isinstance(body, dict) else {}

    def profile(self, wallet: str) -> dict[str, Any]:
        body = self._get(self._profiles + "/public-profile", {"address": wallet})
        return dict(body) if isinstance(body, dict) else {}

    def search_profiles(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        """Public profiles whose username matches ``name`` (Polymarket's site search)."""
        body = self._get(
            self._profiles + "/public-search",
            {"q": name, "search_profiles": "true", "limit_per_type": limit},
        )
        return list((body or {}).get("profiles") or []) if isinstance(body, dict) else []

    def positions(self, wallet: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._list(
            self._data + "/positions",
            {"user": wallet, "limit": limit, "sortBy": "CURRENT", "sizeThreshold": 1},
        )

    def trades(self, wallet: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """A wallet's trades, newest first."""
        return self._list(
            self._data + "/activity",
            {"user": wallet, "type": "TRADE", "limit": limit, "offset": offset},
        )

    def market_trades(
        self, condition_id: str, limit: int = 500, min_usd: float | None = None
    ) -> list[dict[str, Any]]:
        """Everyone's recent trades on one market (``conditionId``), newest first, with the wallet."""
        params: dict[str, Any] = {"market": condition_id, "limit": limit, "takerOnly": "false"}
        if min_usd:
            params |= {"filterType": "CASH", "filterAmount": min_usd}
        return self._list(self._data + "/trades", params)

    def big_trades(self, min_usd: float, limit: int = 100) -> list[dict[str, Any]]:
        """Everyone's recent trades worth at least ``min_usd``, newest first, with the wallet.

        This feed runs a few minutes behind the chain.
        """
        return self._list(
            self._data + "/trades",
            {"filterType": "CASH", "filterAmount": min_usd, "limit": limit, "takerOnly": "true"},
        )
