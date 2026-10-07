"""Kalshi's social layer, read without a key: leaderboard, profiles and trades with nicknames.

These are the public endpoints behind kalshi.com's Leaderboard and profile pages. Kalshi doesn't
document them, so their shape can change without notice; every reader here returns plain dicts and
raises ``VenueError`` on anything unexpected.

Units, checked against Kalshi's documented trades API on 2026-10-07:

- ``price`` on a trade is the YES price in cents (``price_dollars`` in dollars), whichever side the
  taker took. A NO taker paid ``1 - price``.
- ``count_fp`` is contracts.
- Leaderboard ``projected_pnl`` is dollars; ``volume`` is contracts.
- Profile ``metrics.pnl`` is in ten-thousandths of a dollar; ``metrics.volume`` is contracts.

A trader can hide their trades and holdings: the answer then has ``visibility_state: "hidden"`` and
no rows. The leaderboard is opt-in, so it lists only traders who chose to appear.
"""

from __future__ import annotations

from typing import Any

from ..errors import VenueError
from ..http import Http

BASE = "https://api.elections.kalshi.com/v1/social"
VENUE = "kalshi"
PNL_UNITS = 10_000  # metrics.pnl is in 1/10,000 of a dollar
METRICS = ("projected_pnl", "volume", "num_markets_traded")


class KalshiSocial:
    """Read-only client for Kalshi's public social endpoints. Needs no key."""

    def __init__(self, http: Http, base_url: str = BASE) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        body = self._http.request("GET", self._base + path, venue=VENUE, params=params)
        if not isinstance(body, dict):
            raise VenueError(
                "venue_unavailable",
                f"Kalshi's {path} answered in an unexpected shape.",
                venue=VENUE,
                raw=body,
                hint="Kalshi doesn't document its social endpoints, so they may have changed.",
                next="Try again later, or report it with the raw answer.",
            )
        return body

    def leaderboard(self, metric: str = "projected_pnl", limit: int = 25) -> list[dict[str, Any]]:
        if metric not in METRICS:
            raise ValueError(f"metric must be one of {METRICS}")
        body = self._get("/leaderboard", {"metric_name": metric, "limit": limit, "time_period": "all_time"})
        return list(body.get("rank_list") or [])

    def profile(self, nickname: str) -> dict[str, Any]:
        return dict(self._get("/profile", {"nickname": nickname}).get("social_profile") or {})

    def metrics(self, nickname: str) -> dict[str, Any]:
        return dict(self._get("/profile/metrics", {"nickname": nickname}).get("metrics") or {})

    def holdings(self, nickname: str, limit: int = 50) -> tuple[list[dict[str, Any]], str]:
        body = self._get(
            "/profile/holdings", {"nickname": nickname, "limit": limit, "closed_positions": "false"}
        )
        return list(body.get("holdings") or []), str(body.get("visibility_state") or "unknown")

    def trades(
        self, nickname: str | None = None, page_size: int = 100, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str, str | None]:
        """One page of trades, newest first: a trader's own, or everyone's when ``nickname`` is None.

        Returns ``(trades, visibility_state, next_cursor)``.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if nickname:
            params["nickname"] = nickname
        if cursor:
            params["cursor"] = cursor
        body = self._get("/trades", params)
        vis = str(body.get("visibility_state") or ("visible" if nickname is None else "unknown"))
        return list(body.get("trades") or []), vis, body.get("cursor") or None
