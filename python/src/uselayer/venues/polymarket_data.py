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

    def positions(self, wallet: str, limit: int = 50, market: str | None = None) -> list[dict[str, Any]]:
        """A wallet's positions, biggest first; with ``market`` (a conditionId), in that market only."""
        params: dict[str, Any] = {"user": wallet, "limit": limit, "sortBy": "CURRENT", "sizeThreshold": 1}
        if market:
            params["market"] = market
        return self._list(self._data + "/positions", params)

    def closed_positions(self, wallet: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """A wallet's closed positions (sold, or settled and redeemed), newest first, at most 50 a page. A position
        whose market settled but that was never redeemed isn't here: it's in :meth:`held_positions`.

        Each has ``avgPrice`` (the price of their last buy, not their average), ``totalBought`` (contracts),
        ``realizedPnl`` and ``curPrice``: the market's price now (1 or 0 once settled), not the price they got
        out at.
        """
        return self._list(
            self._data + "/closed-positions",
            {"user": wallet, "limit": min(limit, 50), "offset": offset, "sortBy": "TIMESTAMP",
             "sortDirection": "DESC"},
        )  # fmt: skip

    def held_positions(self, wallet: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        """Every position a wallet still holds, at most 500 a page, any size: open ones, and ones whose market
        settled but which they never redeemed (``redeemable``; a losing one is worth 0, so it often never is)."""
        return self._list(
            self._data + "/positions",
            {"user": wallet, "limit": min(limit, 500), "offset": offset, "sizeThreshold": 0},
        )

    def positions_value(self, wallet: str) -> float | None:
        """What a wallet's open positions are worth now, in dollars (None if Polymarket doesn't say)."""
        rows = self._list(self._data + "/value", {"user": wallet})
        value = rows[0].get("value") if rows and isinstance(rows[0], dict) else None
        return None if value is None else float(value)

    def trades_in(
        self, wallet: str, markets: list[str], limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        """A wallet's trades in up to 20 markets (``conditionId``s), newest first. (Asking for sells only
        across 10 or more markets is refused by Polymarket's firewall, so both sides come back.)"""
        return self._list(
            self._data + "/activity",
            {"user": wallet, "market": ",".join(markets), "type": "TRADE", "limit": limit, "offset": offset},
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

    def taker_trades_in(self, wallet: str, markets: list[str], limit: int = 1000) -> list[dict[str, Any]]:
        """A wallet's fills where it took liquidity in up to 10 markets (``conditionId``s), newest first. Their
        other fills in those markets were resting orders (a maker's)."""
        return self._list(
            self._data + "/trades",
            {"user": wallet, "market": ",".join(markets), "takerOnly": "true", "limit": limit},
        )

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

    def open_markets(
        self, after: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of open markets, busiest first (by dollars traded in the last day), with their tags, and
        the cursor of the next page (None after the last). Gamma refuses an ``offset`` past about 2,000, so
        this pages by cursor: all ~18,000 open markets in about 180 reads."""
        params: dict[str, Any] = {
            "closed": "false", "limit": min(limit, 100), "order": "volume24hr", "ascending": "false",
            "include_tag": "true",
        }  # fmt: skip
        if after:
            params["after_cursor"] = after
        body = self._get(self._profiles + "/markets/keyset", params)
        if not isinstance(body, dict):
            raise VenueError(
                "venue_unavailable",
                "Polymarket's /markets/keyset answered in an unexpected shape.",
                venue=VENUE,
                raw=body,
                next="Try again later.",
            )
        return list(body.get("markets") or []), body.get("next_cursor") or None

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
