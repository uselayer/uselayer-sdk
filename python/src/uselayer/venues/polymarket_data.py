"""Polymarket's public trader data, read without a key: leaderboard, wallet stats, positions, trades.

Polymarket (international) settles on-chain, so every trade belongs to a public wallet. These are
reads only: this package has no Polymarket international trading code. A copied Polymarket trade is
placed on its twin market on Kalshi or Polymarket US, found through Layer's matching.

Money is in USDC (dollars). ``size`` on a trade is outcome tokens (contracts), ``price`` is the price
of the outcome that was traded, and ``outcomeIndex`` says which outcome (0 or 1).

Polymarket retired Data API v1 on 2026-10-24. The reads below use v2 and hand back rows in v1's shape
(camelCase, ``asset``, ``curPrice``, ``totalBought``...), so callers didn't change. v2 pages by cursor only;
``offset`` here is kept by walking the cursors, and the cursor at each offset is remembered so reading a list
page after page costs one read per page.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
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
CURSORS_KEPT = 4096  # remembered (list, offset) -> cursor pairs, oldest dropped first

# v2 names that aren't just v1's in snake_case.
_V1_NAMES = {
    "token_id": "asset",
    "opposite_token_id": "oppositeAsset",
    "current_price": "curPrice",
    "current_size": "size",
    "total_size": "totalBought",
    "unrealized_pnl": "cashPnl",
    "entry_cost_usdc": "initialValue",
    "total_cost_usdc": "grossInitialValue",
    "user_id": "proxyWallet",
    "volume": "vol",
    "verified": "verifiedBadge",
}
_SNAKE = re.compile(r"_([a-z0-9])")
_APOSTROPHES = "'’"
_PLAIN = frozenset({"Yes", "No", "Over", "Under", "Up", "Down"})  # outcome names with no apostrophe to lose
OUTCOMES_KEPT = 20_000  # markets whose Gamma outcome names are kept, oldest dropped first


def _bare(name: str) -> str:
    return name.translate({ord(a): None for a in _APOSTROPHES})


def _with_apostrophes(name: Any, title: Any) -> Any:
    """``name`` as the market's title spells it. v2 drops apostrophes from outcome names ("Anyones Legend") but not
    from titles ("Anyone's Legend vs LGD Gaming"), so a name the title holds with apostrophes added gets them back.
    A name the title doesn't hold is left as it is."""
    if not isinstance(name, str) or not isinstance(title, str) or not name or name in title:
        return name
    if not any(a in title for a in _APOSTROPHES):
        return name
    spelled = re.search("".join(re.escape(c) + f"[{_APOSTROPHES}]?" for c in name), title)
    return spelled.group(0) if spelled else name


def _v1_row(row: dict[str, Any]) -> dict[str, Any]:
    """A v2 row with v1's field names (and the apostrophes v2 drops from outcome names put back)."""
    out = {_V1_NAMES.get(k) or _SNAKE.sub(lambda m: m.group(1).upper(), k): v for k, v in row.items()}
    for k in ("outcome", "oppositeOutcome"):
        if k in out:
            out[k] = _with_apostrophes(out[k], out.get("title"))
    return out


class PolymarketData:
    """Read-only client for Polymarket's public data and profile APIs. Needs no key."""

    def __init__(self, http: Http, data_url: str = DATA, profiles_url: str = PROFILES) -> None:
        self._http = http
        self._data = data_url.rstrip("/")
        self._profiles = profiles_url.rstrip("/")
        self._cursors: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._cursors_lock = threading.Lock()
        self._outcomes: OrderedDict[str, list[str]] = OrderedDict()  # conditionId -> Gamma's outcome names

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        return self._http.request("GET", url, venue=VENUE, params=params)

    def _spelled(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``rows`` with outcome names their title doesn't hold ("Côte dIvoire" under "Spread: Norway (-1.5)") spelled
        as Gamma's market spells them. One Gamma read per 20 markets (open and closed asked apart), each market's names
        kept after; a market Gamma can't give keeps v2's name. For the reads people see, not the scorer's."""

        def unsure(name: Any, title: Any) -> bool:
            return (
                isinstance(name, str)
                and name not in _PLAIN
                and not (isinstance(title, str) and name in title)
            )

        cids = {
            str(r["conditionId"])
            for r in rows
            if r.get("conditionId")
            and (unsure(r.get("outcome"), r.get("title")) or unsure(r.get("oppositeOutcome"), r.get("title")))
        }
        with self._cursors_lock:
            todo = sorted(c for c in cids if c not in self._outcomes)
        for i in range(0, len(todo), 20):
            for closed in (False, True):
                try:
                    markets = self.markets(todo[i : i + 20], closed=closed)
                except VenueError:
                    continue
                for m in markets:
                    try:
                        names = json.loads(m.get("outcomes") or "[]")
                    except (TypeError, ValueError):
                        continue
                    if m.get("conditionId") and isinstance(names, list):
                        with self._cursors_lock:
                            self._outcomes[str(m["conditionId"])] = [str(n) for n in names]
                            while len(self._outcomes) > OUTCOMES_KEPT:
                                self._outcomes.popitem(last=False)
        out = []
        for r in rows:
            with self._cursors_lock:
                names = self._outcomes.get(str(r.get("conditionId")))
            if names and len(names) == 2 and r.get("outcomeIndex") in (0, 1):
                i = int(r["outcomeIndex"])
                fix = {}
                for k, name in (("outcome", names[i]), ("oppositeOutcome", names[1 - i])):
                    if isinstance(r.get(k), str) and _bare(r[k]) == _bare(name) and r[k] != name:
                        fix[k] = name
                r = r | fix
            out.append(r)
        return out

    def _unexpected(self, path: str, body: Any) -> VenueError:
        return VenueError(
            "venue_unavailable",
            f"Polymarket's {path} answered in an unexpected shape.",
            venue=VENUE,
            raw=body,
            next="Try again later.",
        )

    def _pages(
        self, path: str, params: dict[str, Any], limit: int, offset: int, page: int
    ) -> list[dict[str, Any]]:
        """Rows ``offset`` to ``offset + limit`` of a v2 list, in v1's shape, at most ``page`` per read. Starts from
        the remembered cursor at ``offset`` when there is one, else walks from the top."""
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        at, cursor = 0, None
        if offset:
            with self._cursors_lock:
                cursor = self._cursors.get((*key, offset))
            if cursor:
                at = offset
        out: list[dict[str, Any]] = []
        while len(out) < limit:
            q = dict(params, limit=min(page, max(0, offset - at) + limit - len(out)))
            if cursor:
                q["cursor"] = cursor
            body = self._get(self._data + path, q)
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                if isinstance(body, dict) and "data" in body and rows is None:
                    rows = []  # v2 answers a miss with data: null
                else:
                    raise self._unexpected(path, body)
            out += rows[max(0, offset - at) :]
            at += len(rows)
            cursor = ((body.get("pagination") or {}).get("next_cursor")) or None
            if not rows or not cursor:
                break
            with self._cursors_lock:
                self._cursors[(*key, at)] = cursor
                while len(self._cursors) > CURSORS_KEPT:
                    self._cursors.popitem(last=False)
        return [_v1_row(r) for r in out[:limit]]

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
        user: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of a leaderboard (at most 50 rows); ``category`` is one of :data:`CATEGORIES`. With ``user``,
        that wallet's own row only (``[]`` when it has none on that board, such as a category it never traded)."""
        if order_by not in ORDER_BY or period not in PERIODS:
            raise ValueError(f"order_by must be one of {ORDER_BY} and period one of {PERIODS}")
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        params: dict[str, Any] = {
            "timePeriod": period,
            "orderBy": "VOLUME" if order_by == "VOL" else order_by,
        }
        if category:
            params["category"] = category
        if user:
            # v2 answers one wallet with a single row (or null) ranked on both boards
            body = self._get(self._data + "/v2/leaderboard", params | {"user": user, "limit": 1})
            if not isinstance(body, dict):
                raise self._unexpected("/v2/leaderboard", body)
            row = body.get("data")
            if isinstance(row, list):
                return [_v1_row(r) | {"rank": str(r.get("rank") or "")} for r in row]
            if not isinstance(row, dict):
                return []
            rank = row.get("rank_pnl" if order_by == "PNL" else "rank_volume") or row.get("rank")
            return [_v1_row(row) | {"rank": "" if rank is None else str(rank)}]
        rows = self._pages("/v2/leaderboard", params, min(limit, 50), offset, 50)
        return [r | {"rank": "" if r.get("rank") is None else str(r["rank"])} for r in rows]

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
        params: dict[str, Any] = {"user": wallet, "sortBy": "CURRENT_VALUE"}
        if market:
            params["condition"] = market
        # v1's sizeThreshold=1, which v2 ignores: read past the dust so ``limit`` rows remain
        out: list[dict[str, Any]] = []
        offset = 0
        while len(out) < limit:
            rows = self._pages("/v2/positions", params, min(500, 2 * limit), offset, 500)
            out += [r for r in rows if float(r.get("size") or 0) >= 1]
            offset += len(rows)
            if len(rows) < min(500, 2 * limit):
                break
        return self._spelled(out[:limit])

    def closed_positions(self, wallet: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """A wallet's closed positions (sold, or settled and redeemed), newest first, at most 50 a page. A position
        whose market settled but that was never redeemed isn't here: it's in :meth:`held_positions`.

        Each has ``avgPrice`` (the price of their last buy, not their average), ``totalBought`` (contracts),
        ``realizedPnl`` and ``curPrice``: the market's price now (1 or 0 once settled), not the price they got
        out at.
        """
        rows = self._pages(
            "/v2/positions",
            {"user": wallet, "status": "CLOSED", "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            min(limit, 50),
            offset,
            500,
        )
        return self._spelled([r | {"timestamp": r.get("lastEventAt")} for r in rows])  # v1's close time

    def held_positions(self, wallet: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        """Every position a wallet still holds, at most 500 a page, any size: open ones, and ones whose market
        settled but which they never redeemed (``redeemable``; a losing one is worth 0, so it often never is)."""
        return self._spelled(self._pages("/v2/positions", {"user": wallet}, min(limit, 500), offset, 500))

    def positions_value(self, wallet: str) -> float | None:
        """What a wallet's open positions are worth now, in dollars (None if Polymarket doesn't say)."""
        body = self._get(self._data + "/v2/value", {"user": wallet})
        data = body.get("data") if isinstance(body, dict) else None
        value = data.get("value") if isinstance(data, dict) else None
        return None if value is None else float(value)

    def trades_in(
        self, wallet: str, markets: list[str], limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        """A wallet's trades in up to 20 markets (``conditionId``s), newest first. (Asking for sells only
        across 10 or more markets is refused by Polymarket's firewall, so both sides come back.)"""
        return self._pages(
            "/v2/activity",
            {"user": wallet, "condition": ",".join(markets), "type": "TRADE"},
            limit,
            offset,
            500,
        )

    def trades(self, wallet: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """A wallet's trades, newest first."""
        return self._spelled(
            self._pages("/v2/activity", {"user": wallet, "type": "TRADE"}, limit, offset, 500)
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
            "sortDirection": "ASC" if ascending else "DESC",
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self._pages("/v2/activity", params, limit, offset, 500)

    def taker_trades(self, wallet: str, limit: int = 500) -> list[dict[str, Any]]:
        """A wallet's recent fills where it took liquidity (not its resting orders), newest first."""
        return self._pages("/v2/trades", {"user": wallet, "takerOnly": "true"}, limit, 0, 1000)

    def taker_trades_in(self, wallet: str, markets: list[str], limit: int = 1000) -> list[dict[str, Any]]:
        """A wallet's fills where it took liquidity in up to 10 markets (``conditionId``s), newest first. Their
        other fills in those markets were resting orders (a maker's)."""
        return self._pages(
            "/v2/trades",
            {"user": wallet, "condition": ",".join(markets), "takerOnly": "true"},
            limit,
            0,
            1000,
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
        params: dict[str, Any] = {"condition": condition_id, "takerOnly": "false"}
        if min_usd:
            params |= {"filterType": "CASH", "filterAmount": min_usd}
        return self._pages("/v2/trades", params, limit, 0, 1000)

    def big_trades(self, min_usd: float, limit: int = 100) -> list[dict[str, Any]]:
        """Everyone's recent trades worth at least ``min_usd``, newest first, with the wallet.

        This feed runs a few minutes behind the chain.
        """
        params = {"filterType": "CASH", "filterAmount": min_usd, "takerOnly": "true"}
        return self._spelled(self._pages("/v2/trades", params, limit, 0, 1000))
