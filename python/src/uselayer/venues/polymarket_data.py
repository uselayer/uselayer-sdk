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
CATEGORIES = (
    "OVERALL", "SPORTS", "ESPORTS", "CRYPTO", "POLITICS", "ECONOMICS", "FINANCE", "CULTURE", "TECH",
    "WEATHER", "MENTIONS",
)  # fmt: skip


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
        self,
        order_by: str = "PNL",
        period: str = "ALL",
        limit: int = 25,
        *,
        category: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One page of a leaderboard (at most 50 rows); ``category`` is one of :data:`CATEGORIES`."""
        if order_by not in ORDER_BY or period not in PERIODS:
            raise ValueError(f"order_by must be one of {ORDER_BY} and period one of {PERIODS}")
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        params: dict[str, Any] = {"timePeriod": period, "orderBy": order_by, "limit": limit}
        if category:
            params["category"] = category
        if offset:
            params["offset"] = offset
        return self._list(self._data + "/v1/leaderboard", params)

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

    def activity(
        self,
        wallet: str,
        *,
        type: str = "TRADE",
        start: int | None = None,
        end: int | None = None,
        limit: int = 500,
        offset: int = 0,
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        """A wallet's activity of one ``type`` (``TRADE``, ``MERGE``, ``REDEEM``, ``REWARD``...) between
        ``start`` and ``end`` (unix seconds). Polymarket refuses an ``offset`` past 5,000: page by time."""
        params: dict[str, Any] = {
            "user": wallet,
            "type": type,
            "limit": limit,
            "offset": offset,
            "sortDirection": "ASC" if ascending else "DESC",
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self._list(self._data + "/activity", params)

    def taker_trades(self, wallet: str, limit: int = 500) -> list[dict[str, Any]]:
        """A wallet's recent fills where it took liquidity (not its resting orders), newest first."""
        return self._list(self._data + "/trades", {"user": wallet, "limit": limit, "takerOnly": "true"})

    def pnl_history(self, wallet: str) -> list[dict[str, Any]]:
        """A wallet's profit over time: points with ``timestamp`` and ``economic_pnl`` (dollars)."""
        body = self._get(self._data + "/v2/user-pnl", {"user": wallet, "interval": "all"})
        data = (body or {}).get("data") if isinstance(body, dict) else None
        return list((data or {}).get("points") or [])

    def markets(self, condition_ids: list[str], *, closed: bool) -> list[dict[str, Any]]:
        """Gamma's markets for up to 20 ``conditionId``s, with their tags. Gamma returns open and closed
        markets separately, so ask for each."""
        return self._list(
            self._profiles + "/markets",
            {
                "condition_ids": condition_ids,
                "closed": "true" if closed else "false",
                "include_tag": "true",
                "limit": max(20, len(condition_ids)),
            },
        )

    def price_history(self, token: str, start: int, end: int) -> list[tuple[int, float]]:
        """An outcome token's price between ``start`` and ``end`` (unix seconds, at most 7 days apart), as
        ``(time, price)`` in 5-minute steps: each is the price at the start of its 5 minutes. Empty before
        the market opened and after it stopped trading. (Asking for 1-minute steps returns nothing.)
        """
        body = self._get(self._data + "/v2/prices-history", {"token_id": token, "start": start, "end": end})
        rows = (body or {}).get("data") if isinstance(body, dict) else None
        return sorted(
            (int(r["timestamp"]), float(r["price"])) for r in rows or [] if "timestamp" in r and "price" in r
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
