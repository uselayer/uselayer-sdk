"""Is a Polymarket trader worth following: each rule against fixed answers from Polymarket (no network)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from uselayer.http import Http
from uselayer.layer_api import LayerApi
from uselayer.whale_scores import _decisions, category_of, taker_fee
from uselayer.whales import Whales

DATA = "data-api.polymarket.com"
GAMMA = "gamma-api.polymarket.com"
WALLET = "0x00000000000000000000000000000000000000aa"
# Bets two days ago (so every hour-later price exists), 10 s into a 5-minute step of the price history: the
# first price a minute later is then 290 s after the buy, and the first one 5 minutes later 590 s after.
NOW = (int(time.time()) - 2 * 86400) // 300 * 300 + 10
FEES = {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True}}


def fill(
    i: int, *, at: int | None = None, side: str = "BUY", outcome: int = 0, price: float = 0.5, **kw: Any
) -> dict[str, Any]:
    return {
        "proxyWallet": WALLET,
        "timestamp": NOW - i * 3600 if at is None else at,
        "conditionId": f"0xc{i}",
        "asset": f"tok{i}-{outcome}",
        "eventSlug": f"event-{i}",
        "size": 100,
        "usdcSize": 100 * price,
        "price": price,
        "side": side,
        "outcomeIndex": outcome,
        "title": f"Game {i}",
        "outcome": "Yes" if outcome == 0 else "No",
        "name": "sharpie",
        "transactionHash": f"0xtx{i}-{side}-{outcome}-{at}",
    } | kw


def market(i: int, tags: tuple[str, ...] = ("Sports",), **kw: Any) -> dict[str, Any]:
    return {
        "conditionId": f"0xc{i}",
        "closed": False,
        "tags": [{"label": t} for t in tags],
        "clobTokenIds": f'["tok{i}-0", "tok{i}-1"]',
        **FEES,
    } | kw


def whales(
    fills: list[dict[str, Any]],
    path: Callable[[int, int], float],
    *,
    markets: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
    merges: int = 0,
) -> tuple[Whales, list[httpx.Request]]:
    """A Whales on fake Polymarket answers. ``path(i, seconds_after_buy)`` is market i's price."""
    seen: list[httpx.Request] = []
    by_cid = {m["conditionId"]: m for m in markets or [market(i) for i in range(len(fills) + 5)]}
    buy_at = {f["asset"]: int(f["timestamp"]) for f in fills if f["side"] == "BUY"}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        q = r.url.params
        host, p = r.url.host, r.url.path
        if (host, p) == (DATA, "/activity"):
            if q["type"] == "MERGE":
                return httpx.Response(200, json=[{"type": "MERGE"}] * merges)
            rows = sorted(fills, key=lambda f: -f["timestamp"])
            return httpx.Response(200, json=rows if q.get("offset") in (None, "0") else [])
        if (host, p) == (DATA, "/v2/user-stats"):
            return httpx.Response(200, json={"data": stats or {}})
        if (host, p) == (DATA, "/v2/user-pnl"):
            return httpx.Response(200, json={"data": {"points": []}})
        if (host, p) == (DATA, "/trades"):
            return httpx.Response(200, json=[])
        if (host, p) == (GAMMA, "/markets"):
            closed = q["closed"] == "true"
            ids = q.get_list("condition_ids")
            return httpx.Response(
                200, json=[by_cid[c] for c in ids if c in by_cid and bool(by_cid[c]["closed"]) == closed]
            )
        if (host, p) == (DATA, "/v2/prices-history"):
            tok = q["token_id"]
            if tok not in buy_at:
                return httpx.Response(200, json={"data": []})
            i, t0 = int(tok[3:].split("-")[0]), buy_at[tok]
            start = int(q["start"]) // 300 * 300  # 5-minute steps, each the price at its start
            pts = [
                {"timestamp": t, "price": path(i, t - t0), "resolution_seconds": 300}
                for t in range(start, int(q["end"]) + 1, 300)
            ]
            return httpx.Response(200, json={"data": pts})
        return httpx.Response(404, json={"error": "not found"})

    http = Http(transport=httpx.MockTransport(handler), sleep=lambda s: None, limits={})  # no pacing
    return Whales(http, LayerApi(None, http)), seen


def sharp(i: int, s: int) -> float:
    """Drifts their way for an hour: 52¢ a minute in, 53¢ (±0.5¢) at 5 minutes, 56¢ (±1¢) an hour in."""
    wobble = 1 if i % 2 else -1
    if s < 60:
        return 0.5
    if s < 300:
        return 0.52
    if s < 3600:
        return 0.53 + 0.005 * wobble
    return 0.56 + 0.01 * wobble


def test_a_steady_copyable_edge_off_the_leaderboards_is_a_quiet_sharp() -> None:
    w, _ = whales(
        [fill(i) for i in range(30)],
        sharp,
        stats={"biggest_win": 500, "all_time_pnl": {"economic_pnl": 4000, "volume": 90_000}},
    )
    s = w.score(WALLET)
    assert s.segment == "quiet" and s.action == "follow"
    assert s.edge is not None and s.edge.mean == pytest.approx(0.03, abs=0.002) and s.edge.n == 30
    fee = 0.04 * 0.52 * 0.48
    assert s.copy is not None and s.copy.mean == pytest.approx(0.56 - 0.52 - fee, abs=0.002)
    assert s.coverage == {"bets": 30, "priced_1m": 30, "priced_5m": 30, "priced_1h": 30, "combos": 0}
    assert s.confidence == "medium"  # "high" needs 50 bets
    assert {c.rule: c.passed for c in s.checks} == {
        "beats_price": True, "enough_bets": True, "steady": True, "copyable": True, "takes_a_side": True,
        "categories": True,
    }  # fmt: skip
    assert s.strong_categories == ("Sports",) and s.name == "sharpie"
    assert "after fees" in s.reason and "30 bets" in s.reason


def test_on_a_leaderboard_the_same_record_is_a_proven_sharp_once_there_are_enough_bets() -> None:
    w, _ = whales([fill(i) for i in range(45)], sharp)
    assert w.score(WALLET, on_leaderboard=True).segment == "proven"
    w, _ = whales([fill(i) for i in range(30)], sharp)
    assert w.score(WALLET, on_leaderboard=True).segment != "proven"  # 30 bets: not enough for "proven"


def test_an_edge_gone_a_minute_later_is_too_fast_to_copy() -> None:
    def jump(i: int, s: int) -> float:
        return 0.5 if s < 60 else 0.56 + (0.01 if i % 2 else -0.01) * (s >= 300)

    w, _ = whales([fill(i) for i in range(30)], jump)
    s = w.score(WALLET)
    assert s.segment == "too_fast" and s.action == "signal"
    assert s.copy is not None and s.copy.mean < 0
    assert next(c for c in s.checks if c.rule == "copyable").passed is False


def test_trading_both_ways_in_the_same_hour_is_a_market_maker_with_no_view() -> None:
    fills = []
    for i in range(20):
        fills += [fill(i, at=NOW - i * 3600), fill(i, at=NOW - i * 3600 + 30, side="SELL", price=0.51)]
    w, _ = whales(fills, sharp)
    s = w.score(WALLET)
    assert s.segment == "no_view" and "market maker" in s.tags
    assert next(c for c in s.checks if c.rule == "takes_a_side").passed is False


def test_buying_both_outcomes_for_under_a_dollar_is_arbitrage() -> None:
    fills = []
    for i in range(10):
        fills += [fill(i, price=0.48), fill(i, outcome=1, price=0.49, at=NOW - i * 3600 + 10)]
    w, _ = whales(fills, sharp)
    s = w.score(WALLET)
    assert s.segment == "no_view" and "arbitrage" in s.tags and "Arbitrage" in s.reason


def test_merging_sets_back_alone_is_not_arbitrage() -> None:
    # Closing a bet by buying the other outcome later and merging for $1 is normal for a trader with a view.
    w, seen = whales(
        [fill(i) for i in range(30)],
        sharp,
        stats={"biggest_win": 500, "all_time_pnl": {"economic_pnl": 4000, "volume": 90_000}},
        merges=25,
    )
    s = w.score(WALLET)
    assert s.segment == "quiet" and "arbitrage" not in s.tags
    assert next(c for c in s.checks if c.rule == "takes_a_side").passed is True
    assert not any(r.url.params.get("type") == "MERGE" for r in seen)  # merges aren't read at all


def test_arbitrage_and_market_makers_each_get_their_own_reason() -> None:
    arb = []
    for i in range(10):
        arb += [fill(i, price=0.48), fill(i, outcome=1, price=0.49, at=NOW - i * 3600 + 10)]
    s = whales(arb, sharp)[0].score(WALLET)
    assert s.reason.startswith("Arbitrage") and "Market maker" not in s.reason
    side = next(c for c in s.checks if c.rule == "takes_a_side").detail
    assert "within a minute" in side and "merge" not in side
    mm = []
    for i in range(20):
        mm += [fill(i, at=NOW - i * 3600), fill(i, at=NOW - i * 3600 + 30, side="SELL", price=0.51)]
    s = whales(mm, sharp)[0].score(WALLET)
    assert s.reason.startswith("Market maker") and "arbitrage" not in s.tags


def test_big_profit_carried_by_one_win_without_an_edge_is_lucky() -> None:
    def flat(i: int, s: int) -> float:
        return 0.5 + (0.03 if i % 2 else -0.03) * (s >= 60)

    w, _ = whales(
        [fill(i) for i in range(30)],
        flat,
        stats={"biggest_win": 900_000, "all_time_pnl": {"economic_pnl": 600_000, "volume": 5e6}},
    )
    s = w.score(WALLET)
    assert s.segment == "lucky" and s.action == "avoid"
    assert "without their biggest win" in s.reason
    steady = next(c for c in s.checks if c.rule == "steady")
    assert steady.passed is False and "−$300K" in steady.detail


def test_combo_bets_have_no_market_and_are_left_out_of_coverage() -> None:
    fills = [fill(i) for i in range(30)]
    w, _ = whales(fills, sharp, markets=[market(i) for i in range(25)])  # 25..29 are combos
    s = w.score(WALLET)
    assert s.coverage["bets"] == 25 and s.coverage["combos"] == 5 and s.bets == 25


def test_few_bets_with_a_good_start_are_rising_not_sharp() -> None:
    w, _ = whales([fill(i) for i in range(12)], sharp)
    s = w.score(WALLET)
    assert s.segment == "rising" and s.action == "watch" and s.confidence != "high"


def test_one_price_read_covers_several_bets_on_the_same_token() -> None:
    fills = [fill(0, at=NOW), fill(1), {**fill(0, at=NOW + 600), "conditionId": "0xc0"}]
    w, seen = whales(fills, sharp)
    w.score(WALLET)
    assert sum(1 for r in seen if r.url.path == "/v2/prices-history") == 2  # tok0 (merged decisions) + tok1


# ---- pieces ----


def test_fills_within_a_minute_are_one_decision_and_only_the_first_bet_per_outcome_counts() -> None:
    ds = _decisions(
        [
            fill(1, at=100, price=0.40),
            fill(1, at=130, price=0.50),  # same decision: average 45¢
            fill(1, at=5000, price=0.60),  # adding later: not a new bet
            fill(1, at=200, outcome=1, price=0.5),  # the other outcome: its own bet
            fill(2, at=300, side="SELL"),
        ]
    )
    assert [(d.condition, d.outcome, d.at) for d in ds] == [("0xc1", 0, 100), ("0xc1", 1, 200)]
    assert ds[0].price == pytest.approx(0.45) and ds[0].size == 200


def test_category_comes_from_tags_then_fee_type() -> None:
    assert category_of({"tags": [{"label": "Esports"}, {"label": "Sports"}]}) == "Esports"
    assert category_of({"tags": [{"label": "Bitcoin"}]}) == "Crypto"
    assert category_of({"tags": [], "feeType": "weather_fees"}) == "Weather"
    assert category_of({}) == "Other"


def test_taker_fee_is_rate_times_p_one_minus_p_to_the_exponent() -> None:
    m = {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 2}}
    assert taker_fee(m, 0.4) == pytest.approx(0.05 * (0.4 * 0.6) ** 2)
    assert taker_fee({"feesEnabled": False, "feeSchedule": {"rate": 0.05}}, 0.4) == 0


def test_big_profit_with_no_recent_bets_is_not_called_lucky() -> None:
    w, _ = whales(
        [fill(i) for i in range(3)],
        sharp,
        stats={"biggest_win": 10_000, "all_time_pnl": {"economic_pnl": 600_000, "volume": 5e6}},
    )
    s = w.score(WALLET)
    assert s.segment == "no_edge" and "Too few recent bets" in s.reason


def test_reading_fills_pages_by_time_past_the_offset_cap_and_stops_on_a_stuck_page() -> None:
    from uselayer.whale_scores import Scorer

    calls: list[dict[str, Any]] = []

    class Poly:
        def activity(self, wallet: str, **kw: Any) -> list[dict[str, Any]]:
            calls.append(kw)
            return [fill(i, at=1000) for i in range(500)]  # every page: 500 fills in the same second

    rows = Scorer(Poly())._fills(WALLET, 0, 2000, max_fills=100_000)  # type: ignore[arg-type]
    assert len(rows) == 500 and calls[-1]["end"] == 1000 and len(calls) <= 20
