"""Polymarket Data API v2 (v1 retired 2026-10-24): v2 requests out, v1-shaped rows back."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from uselayer.errors import VenueError
from uselayer.http import Http
from uselayer.venues.polymarket_data import PolymarketData

W = "0xabc0000000000000000000000000000000000001"


def client(answer: Any, seen: list[httpx.Request] | None = None) -> PolymarketData:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = answer(request) if callable(answer) else answer
        return httpx.Response(200, json=body)

    return PolymarketData(Http(transport=httpx.MockTransport(handler), sleep=lambda s: None, limits={}))


def cursor_list(rows: list[dict[str, Any]]) -> Any:
    """A v2 list paged by cursor (the cursor is the next row's index)."""

    def answer(request: httpx.Request) -> dict[str, Any]:
        assert "offset" not in request.url.params  # v2 refuses offset
        start = int(request.url.params.get("cursor", 0))
        limit = int(request.url.params["limit"])
        page = rows[start : start + limit]
        nxt = start + len(page)
        return {"data": page, "pagination": {"next_cursor": str(nxt) if nxt < len(rows) else None}}

    return answer


def closed(i: int) -> dict[str, Any]:
    return {"token_id": f"t{i}", "condition_id": f"c{i}", "realized_pnl": i, "total_size": 10, "current_price": 1,
            "last_event_at": 1_000 + i, "avg_price": 0.5}  # fmt: skip


def test_rows_come_back_in_v1_shape() -> None:
    rows = client(cursor_list([closed(1)])).closed_positions(W)
    assert rows == [
        {"asset": "t1", "conditionId": "c1", "realizedPnl": 1, "totalBought": 10, "curPrice": 1, "lastEventAt": 1_001,
         "avgPrice": 0.5, "timestamp": 1_001}
    ]  # fmt: skip


def test_offset_walks_the_cursors_and_paging_on_reuses_them() -> None:
    seen: list[httpx.Request] = []
    poly = client(cursor_list([closed(i) for i in range(120)]), seen)
    got = [r["asset"] for o in (0, 50, 100) for r in poly.closed_positions(W, 50, offset=o)]
    assert got == [f"t{i}" for i in range(120)]
    assert len(seen) == 3  # one read per page: each page starts from the cursor the last one left
    seen.clear()
    cold = client(cursor_list([closed(i) for i in range(120)]), seen).closed_positions(W, 50, offset=100)
    assert [r["asset"] for r in cold] == [f"t{i}" for i in range(100, 120)]
    assert seen[0].url.params["status"] == "CLOSED" and "cursor" not in seen[0].url.params


def test_positions_skip_dust_and_still_fill_the_limit() -> None:
    rows = [{"token_id": f"t{i}", "current_size": 0.5 if i % 2 else 5} for i in range(10)]
    seen: list[httpx.Request] = []
    got = client(cursor_list(rows), seen).positions(W, 3, market="0xcid")
    assert [r["asset"] for r in got] == ["t0", "t2", "t4"] and all(r["size"] >= 1 for r in got)
    assert seen[0].url.params["sortBy"] == "CURRENT_VALUE" and seen[0].url.params["condition"] == "0xcid"


def test_leaderboard_names_volume_and_ranks_as_v1_did() -> None:
    seen: list[httpx.Request] = []
    board = [{"rank": 1, "user_id": W, "user_name": "a", "pnl": 5, "volume": 9, "verified": True}]
    got = client({"data": board, "pagination": {"next_cursor": None}}, seen).leaderboard("VOL", "WEEK", 10)
    assert got == [
        {"rank": "1", "proxyWallet": W, "userName": "a", "pnl": 5, "vol": 9, "verifiedBadge": True}
    ]
    assert seen[0].url.params["orderBy"] == "VOLUME"


@pytest.mark.parametrize(("order", "rank"), [("PNL", "12"), ("VOL", "269")])
def test_one_wallets_row_takes_the_rank_of_the_board_asked_for(order: str, rank: str) -> None:
    row = {"user_id": W, "pnl": 7.0, "volume": 4.0, "rank_pnl": 12, "rank_volume": 269, "user_name": "j"}
    got = client({"data": row}).leaderboard(order, "ALL", 1, category="SPORTS", user=W)
    assert got[0]["rank"] == rank and got[0]["proxyWallet"] == W


def test_a_wallet_with_no_row_and_an_empty_list_are_empty() -> None:
    assert client({"data": None}).leaderboard("PNL", "ALL", 1, user=W) == []
    assert client({"data": None}).trades(W) == []


def test_positions_value_reads_v2s_envelope() -> None:
    assert client({"data": {"proxy_wallet": W, "value": 12.5}}).positions_value(W) == 12.5
    assert client({"data": {"proxy_wallet": W}}).positions_value(W) is None


def test_an_unexpected_shape_is_a_venue_error() -> None:
    with pytest.raises(VenueError):
        client([{"old": "v1 list"}]).trades(W)
