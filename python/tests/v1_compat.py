"""Serve the tests' Data API v1 fixtures as Polymarket's v2 answers.

The SDK reads v2 since v1's retirement (2026-10-24) and turns rows back into v1's shape. Most tests were written
against v1: their handlers see v1 paths and params and return v1 rows. ``v1_compat(handler)`` sits in front of such
a handler: a v2 request is turned into the v1 request the handler expects, and the handler's v1 answer into v2's
envelope (``data`` + ``pagination``, snake_case, v2's own names, cursor paging).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

DATA_HOST = "data-api.polymarket.com"
_KEEP_V2 = {"/v2/user-stats", "/v2/user-pnl", "/v2/prices-history"}  # already v2 before the move
_CAMEL = re.compile(r"(?<!^)([A-Z])")
_COMMON = {
    "asset": "token_id",
    "oppositeAsset": "opposite_token_id",
    "curPrice": "current_price",
    "totalBought": "total_size",
    "cashPnl": "unrealized_pnl",
    "initialValue": "entry_cost_usdc",
    "grossInitialValue": "total_cost_usdc",
    "verifiedBadge": "verified",
}


def _snake(k: str) -> str:
    return _CAMEL.sub(r"_\1", k).lower()


def _v2_row(row: dict[str, Any], v1_path: str) -> dict[str, Any]:
    names = dict(_COMMON)
    if v1_path in ("/positions", "/closed-positions"):
        names["size"] = "current_size"
    if v1_path == "/closed-positions":
        names["timestamp"] = "last_event_at"
    if v1_path == "/v1/leaderboard":
        names |= {"proxyWallet": "user_id", "vol": "volume"}
    out = {names.get(k) or _snake(k): v for k, v in row.items()}
    if v1_path == "/v1/leaderboard" and "rank" in out:
        out["rank"] = int(out["rank"]) if str(out["rank"]).isdigit() else out["rank"]
    return out


def _v1_request(request: httpx.Request) -> tuple[httpx.Request, str, int, int]:
    """The v1 request a v2 one stands for, its v1 path, the offset the cursor stands for and the limit."""
    q = dict(request.url.params)
    path = request.url.path
    v1_path = {
        "/v2/activity": "/activity",
        "/v2/trades": "/trades",
        "/v2/value": "/value",
        "/v2/leaderboard": "/v1/leaderboard",
    }.get(path, "/positions")
    if path == "/v2/positions" and q.pop("status", None) == "CLOSED":
        v1_path = "/closed-positions"
    offset = int(q.pop("cursor", 0) or 0)
    limit = int(q.get("limit", 100))
    if offset:
        q["offset"] = str(offset)
    if "condition" in q:
        q["market"] = q.pop("condition")
    if q.get("orderBy") == "VOLUME":
        q["orderBy"] = "VOL"
    if q.get("sortBy") == "CURRENT_VALUE":
        q["sortBy"] = "CURRENT"
    url = httpx.URL(f"https://{DATA_HOST}{v1_path}", params=q)
    return httpx.Request("GET", url), v1_path, offset, limit


def v1_compat(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    def serve(request: httpx.Request) -> httpx.Response:
        if (
            request.url.host != DATA_HOST
            or not request.url.path.startswith("/v2/")
            or request.url.path in _KEEP_V2
        ):
            return handler(request)
        v1_req, v1_path, offset, limit = _v1_request(request)
        answer = handler(v1_req)
        if answer.status_code != 200:
            return answer
        body = json.loads(answer.read() or b"null")
        if v1_path == "/value":
            row = body[0] if isinstance(body, list) and body else {}
            return httpx.Response(
                200, json={"data": {"proxy_wallet": row.get("user"), "value": row.get("value")}}
            )
        if v1_path == "/v1/leaderboard" and "user" in request.url.params:
            row = body[0] if isinstance(body, list) and body else None
            if row is None:
                return httpx.Response(200, json={"data": None})
            v2 = _v2_row(row, v1_path)
            rank = v2.pop("rank", None)
            return httpx.Response(200, json={"data": v2 | {"rank_pnl": rank, "rank_volume": rank}})
        rows = [_v2_row(r, v1_path) for r in body or []]
        more = len(rows) >= limit
        page = {
            "limit": limit,
            "offset": 0,
            "has_more": more,
            "next_cursor": str(offset + len(rows)) if more else None,
        }
        return httpx.Response(200, json={"data": rows, "pagination": page})

    return serve
