"""Is a Polymarket trader worth following: each rule against fixed answers from Polymarket (no network)."""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from uselayer import whale_scores as ws
from uselayer.errors import VenueError
from uselayer.http import Http, is_firewall_block
from uselayer.layer_api import LayerApi
from uselayer.venues.polygon import PUSD, RPCS, Polygon
from uselayer.whale_scores import (
    MarketBook,
    PriceTape,
    Stat,
    _Decision,
    _decisions,
    _stat,
    category_of,
    copyable_verdict,
    half_spread,
    sample_decisions,
    segment_of,
    t95,
    taker_fee,
)
from uselayer.whales import Whales

DATA = "data-api.polymarket.com"
GAMMA = "gamma-api.polymarket.com"
WALLET = "0x00000000000000000000000000000000000000aa"
# Bets two days ago (so every hour-later price exists), 10 s into a 5-minute step of the price history: the
# first price a minute later is then 290 s after the buy, and the first one 5 minutes later 590 s after.
NOW = (int(time.time()) - 2 * 86400) // 300 * 300 + 10
FEES = {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True}}
DAY = 86400
# Polymarket's closed positions and trades for one wallet, recorded 2026-10-12: 8 sold, 6 held until they won.
CLOSED = json.loads((Path(__file__).parent / "fixtures" / "whales" / "closed_positions.json").read_text())


def curve(
    made: float = 3000,
    *,
    income: float = 0.0,
    unrealized: float = 0.0,
    traded: float = 60_000,
    one_day: bool = False,
) -> list[dict[str, Any]]:
    """Polymarket's profit history, a point a day for 100 days: flat until 30 days ago, then ``made`` from
    trading (``unrealized`` of it still open), ``income`` in rewards and ``traded`` dollars, evenly over the
    30 days, or all on one day 3 days ago with ``one_day``."""
    now = int(time.time())
    pts = []
    for k in range(100, -1, -1):
        f = (1.0 if k < 3 else 0.0) if one_day else max(0.0, (30 - k) / 30)
        pts.append(
            {
                "timestamp": now - k * DAY - 60,
                "realized_pnl": f * (made - unrealized),
                "unrealized_pnl": f * unrealized,
                "wallet_income": f * income,
                "economic_pnl": f * (made + income),
                "volume_usdc": f * traded,
            }
        )
    return pts


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
    pnl_points: list[dict[str, Any]] | None = None,
    taker: list[dict[str, Any]] | None = None,
    closed_positions: list[dict[str, Any]] | None = None,
    value: float | None = None,
    cash: float | None = None,
    firewall: int = 0,
) -> tuple[Whales, list[httpx.Request]]:
    """A Whales on fake Polymarket answers. ``path(i, seconds_after_buy)`` is market i's price. The profit
    history is :func:`curve` (profitable from trading) unless ``pnl_points`` is given; ``taker`` is their
    taker-only trade feed. ``closed_positions`` (their closed positions, with the recorded trades behind them),
    ``value`` (their open positions' worth) and ``cash`` (pUSD on Polygon) are unreadable unless given.
    ``firewall``: the first that many reads of their trades in closed markets get Polymarket's firewall page
    (a 403 that's a web page, not the API's JSON); -1, every one."""
    points = curve() if pnl_points is None else pnl_points
    seen: list[httpx.Request] = []
    by_cid = {m["conditionId"]: m for m in markets or [market(i) for i in range(len(fills) + 5)]}
    buy_at = {f["asset"]: int(f["timestamp"]) for f in fills if f["side"] == "BUY"}
    blocks = {"left": firewall}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        q = r.url.params
        host, p = r.url.host, r.url.path
        if (host, p) == (DATA, "/activity") and "market" in q:  # their trades in their closed markets
            if blocks["left"]:
                blocks["left"] -= 1 if blocks["left"] > 0 else 0
                return httpx.Response(403, text=FIREWALL_PAGE, headers={"content-type": "text/html"})
            wanted = set(q["market"].split(","))
            return httpx.Response(200, json=[t for t in CLOSED["trades"] if t["conditionId"] in wanted])
        if (host, p) == (DATA, "/closed-positions") and closed_positions is not None:
            start = int(q.get("offset", 0))
            return httpx.Response(200, json=closed_positions[start : start + int(q["limit"])])
        if (host, p) == (DATA, "/value") and value is not None:
            return httpx.Response(200, json=[{"user": WALLET, "value": value}])
        if host == "1rpc.io" and cash is not None:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(round(cash * 1e6))})
        if (host, p) == (DATA, "/activity"):
            if q["type"] == "MERGE":
                return httpx.Response(200, json=[{"type": "MERGE"}] * merges)
            rows = [f for f in sorted(fills, key=lambda f: -f["timestamp"])
                    if int(q.get("start", 0)) <= f["timestamp"] <= int(q.get("end", 2**40))]  # fmt: skip
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 500))
            return httpx.Response(200, json=rows[off : off + lim])
        if (host, p) == (DATA, "/v2/user-stats"):
            return httpx.Response(200, json={"data": stats or {}})
        if (host, p) == (DATA, "/v2/user-pnl"):
            return httpx.Response(200, json={"data": {"points": points}})
        if (host, p) == (DATA, "/trades"):
            return httpx.Response(200, json=taker or [])
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
    # The history's 52¢ a minute in is the middle of the book: on a whole cent, so the book was two ticks
    # wide or more and a copier paid 53¢ at least, plus the fee at that price.
    fee = 0.04 * 0.53 * 0.47
    assert s.copy is not None and s.copy.mean == pytest.approx(0.56 - 0.53 - fee, abs=0.002)
    assert s.copy_entry == pytest.approx(0.53) and s.to_dict()["copy_entry"] == s.copy_entry
    assert s.coverage == {
        "bets": 30,
        "priced_1m": 30,
        "priced_5m": 30,
        "priced_1h": 30,
        "combos": 0,
        "spread": 0,
    }
    assert s.confidence == "medium"  # "high" needs 50 bets
    assert {c.rule: c.passed for c in s.checks} == {
        "beats_price": True, "enough_bets": True, "steady": True, "profitable": True, "copyable": True,
        "takes_a_side": True, "active": True, "categories": True,
    }  # fmt: skip
    assert s.strong_categories == ("Sports",) and s.name == "sharpie"
    assert "30 bets" in s.reason and "after fees" not in s.reason  # the tier's reason is skill, not copying
    assert (s.skill, s.copyable, s.style, s.active) == ("yes", "yes", "takes_a_side", True)
    assert s.trading_pnl_window == 3000 and s.risks == ()


def test_on_a_leaderboard_the_same_record_is_a_proven_sharp_once_there_are_enough_bets() -> None:
    w, _ = whales([fill(i) for i in range(45)], sharp)
    assert w.score(WALLET, on_leaderboard=True).segment == "proven"
    w, _ = whales([fill(i) for i in range(30)], sharp)
    assert w.score(WALLET, on_leaderboard=True).segment != "proven"  # 30 bets: not enough for "proven"


def jump(i: int, s: int) -> float:
    """The whole move comes within the first minute: a copier a minute late pays for it and gains nothing."""
    return 0.5 if s < 60 else 0.56 + (0.01 if i % 2 else -0.01) * (s >= 300)


def test_an_edge_gone_a_minute_later_is_still_a_sharp_but_not_copyable() -> None:
    w, _ = whales([fill(i) for i in range(30)], jump)
    s = w.score(WALLET)
    assert s.segment == "quiet" and s.action == "follow"  # the tier is skill: copying never moves it
    assert (s.skill, s.copyable) == ("yes", "no")  # skilled, not copyable: two verdicts, said apart
    assert s.copy is not None and s.copy.mean < 0
    assert next(c for c in s.checks if c.rule == "copyable").passed is False
    assert "copy" not in s.reason


def test_early_signs_are_rising_even_when_copies_lose() -> None:
    s = whales([fill(i) for i in range(12)], jump)[0].score(WALLET)
    assert (s.skill, s.copyable, s.segment) == ("early", "no", "rising")
    assert s.copy is not None and s.copy.mean < 0
    assert s.reason.startswith("Early signs: the price moved +6")  # never "doesn't clearly move"


def test_trading_both_ways_in_the_same_hour_is_a_market_maker_with_no_view() -> None:
    fills = []
    for i in range(20):
        fills += [fill(i, at=NOW - i * 3600), fill(i, at=NOW - i * 3600 + 30, side="SELL", price=0.51)]
    w, _ = whales(fills, sharp)
    s = w.score(WALLET)
    assert s.segment == "no_view" and "market maker" in s.tags
    assert s.style == "market_maker" and s.skill == "unknown"  # a different strategy, not called unskilled
    assert next(c for c in s.checks if c.rule == "takes_a_side").passed is False


def test_buying_both_outcomes_for_under_a_dollar_is_arbitrage() -> None:
    fills = []
    for i in range(10):
        fills += [fill(i, price=0.48), fill(i, outcome=1, price=0.49, at=NOW - i * 3600 + 10)]
    w, _ = whales(fills, sharp)
    s = w.score(WALLET)
    assert s.segment == "no_view" and "arbitrage" in s.tags and "Arbitrage" in s.reason
    assert s.style == "arbitrage"


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


def test_profit_over_the_window_counts_from_zero_for_an_account_newer_than_the_window() -> None:
    stats = {"all_time_pnl": {"economic_pnl": 5000, "volume": 90_000}}
    day = 86400
    # History from before the window: profit over the window is now minus the value at its start.
    old = [
        {"timestamp": NOW - 40 * day, "economic_pnl": 100},
        {"timestamp": NOW - 31 * day, "economic_pnl": 1000},
    ]
    s = whales([fill(i) for i in range(10)], sharp, stats=stats, pnl_points=old)[0].score(WALLET, end=None)
    assert s.pnl_window == 4000
    # History that starts inside the window: the account had nothing before, so it all counts.
    new = [{"timestamp": NOW - 10 * day, "economic_pnl": 0}, {"timestamp": NOW, "economic_pnl": 5000}]
    s = whales([fill(i) for i in range(10)], sharp, stats=stats, pnl_points=new)[0].score(WALLET)
    assert s.pnl_window == 5000
    assert (
        whales([fill(i) for i in range(10)], sharp, stats=stats, pnl_points=[])[0].score(WALLET).pnl_window
        is None
    )  # no history


def test_big_profit_carried_by_one_win_without_an_edge_is_lucky() -> None:
    def flat(i: int, s: int) -> float:
        return 0.5 + (0.03 if i % 2 else -0.03) * (s >= 60)

    w, _ = whales(
        [fill(i) for i in range(30)],
        flat,
        stats={
            "biggest_win": 900_000,
            "all_time_pnl": {
                "economic_pnl": 600_000,
                "realized_pnl": 600_000,
                "unrealized_pnl": 0,
                "volume_usdc": 5e6,
            },
        },
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


def test_a_copy_buys_at_the_ask_estimated_from_where_the_middle_price_sits() -> None:
    assert half_spread(0.515) == 0.005  # a half cent: a 1¢-tick book an odd number of ticks wide
    assert half_spread(0.52) == 0.01  # a whole cent: two ticks wide or more
    assert half_spread(0.9995) == 0.0005  # a 0.1¢-tick book, not rounded onto a whole cent
    assert half_spread(0.0215) == 0.0005 and half_spread(0.195) == 0.005

    def mid(i: int, s: int) -> float:
        """A minute in the middle is 52.5¢ (a 1¢-wide book, 52¢ bid, 53¢ ask); 58¢ by the hour mark."""
        return 0.5 if s < 60 else 0.525 if s < 300 else 0.53 if s < 3600 else 0.58

    s = whales([fill(i) for i in range(10)], mid)[0].score(WALLET)
    fee = 0.04 * 0.53 * 0.47
    assert s.copy is not None and s.copy.mean == pytest.approx(0.58 - 0.53 - fee, abs=1e-4)
    assert s.copy_entry == pytest.approx(0.53)
    b = s.sample[0]
    assert b.entry == pytest.approx(0.53) and b.fee == pytest.approx(fee, abs=1e-5)
    assert b.to_dict()["entry"] == b.entry
    one = whales([fill(0)], mid)[0].score(WALLET)  # one bet: no copy result to divide, so no copy_entry
    assert one.copy is None and one.copy_entry is None and one.sample[0].entry == pytest.approx(0.53)


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
    assert (
        s.segment == "too_few"
        and s.label == "Limited evidence"
        and s.reason.startswith("Limited evidence: 3 bets")
    )


def test_big_profit_without_a_clear_edge_is_unproven_not_lucky() -> None:
    def flat(i: int, s: int) -> float:
        return 0.5 + (0.03 if i % 2 else -0.03) * (s >= 60)

    stats = {
        "biggest_win": 10_000,
        "all_time_pnl": {"realized_pnl": 600_000, "unrealized_pnl": 0, "volume_usdc": 5e6},
    }
    s = whales([fill(i) for i in range(30)], flat, stats=stats)[0].score(WALLET)
    assert (s.segment, s.action, s.label) == ("big_profit", "skip", "Big profit, edge unproven")
    assert s.reason.startswith("$600K trading profit, but the price doesn't clearly move their way")
    assert "isn't shown to be skill" in s.reason and "luck" not in s.reason


def test_a_wallet_whose_bets_are_all_combos_is_parlays_with_skill_unmeasured() -> None:
    s = whales([fill(i) for i in range(30)], sharp, markets=[market(99)])[0].score(
        WALLET
    )  # none listed: combos, even without the flag
    assert (s.segment, s.label, s.action) == ("parlays", "Parlays: skill unmeasured", "skip")
    assert (s.skill, s.edge_verdict, s.coverage["combos"]) == ("unknown", "unmeasured", 30)
    assert s.reason == (
        "Mostly parlays (30 of their 30 buys over these 30 days), which have no price history, so their skill"
        " can't be measured. Their profit still counts."
    )
    assert s.trading_pnl_window == 3000  # the track record is still read


def parlays(n: int, start: int = 1000) -> list[dict[str, Any]]:
    """``n`` parlay buys as Polymarket gives them: flagged ``isCombo``, no event, no listed market."""
    return [
        fill(
            start + i,
            at=NOW - 2 * DAY - i * 600,
            isCombo=True,
            eventSlug="",
            title=f"Game {i} AND Game {i + 1}",
        )
        for i in range(n)
    ]


def test_parlays_are_left_out_before_the_sample_is_picked() -> None:
    # 100 parlays among 30 single-market bets: picking 60 first would leave 10 single bets to check.
    singles = [fill(i) for i in range(30)]
    w, seen = whales(singles + parlays(100), sharp, markets=[market(i) for i in range(30)])
    s = w.score(WALLET, sample=60)
    assert s.coverage["bets"] == 30 and s.coverage["combos"] == 100 and s.decisions == 130
    assert (s.edge_verdict, s.skill, s.segment) == ("measured", "yes", "quiet")
    assert (
        "100 parlay buys (combo bets) can't be priced"
        in next(c for c in s.checks if c.rule == "enough_bets").detail
    )
    asked = {c for r in seen if r.url.path == "/markets" for c in r.url.params.get_list("condition_ids")}
    assert not asked & {f"0xc{1000 + i}" for i in range(100)}  # a parlay's market is never looked up


def test_mostly_parlays_with_a_few_single_bets_is_parlays_not_no_edge() -> None:
    s = whales([fill(i) for i in range(5)] + parlays(40), sharp, markets=[market(i) for i in range(5)])[
        0
    ].score(WALLET)
    assert (s.segment, s.edge_verdict, s.coverage["combos"], s.decisions) == ("parlays", "unmeasured", 40, 45)
    # Few parlays don't make a parlay trader: too few bets is still too few bets.
    s = whales([fill(i) for i in range(5)] + parlays(2), sharp, markets=[market(i) for i in range(5)])[
        0
    ].score(WALLET)
    assert s.segment == "too_few"


def test_a_measured_edge_with_a_losing_window_is_never_a_sharp_and_never_no_edge() -> None:
    # 45 bets on a leaderboard would be a proven sharp, but they lost $2K from trading over the window: the
    # profit gate keeps them from sharp, and the tier says the edge is there.
    s = whales([fill(i) for i in range(45)], sharp, pnl_points=curve(-2000))[0].score(
        WALLET, on_leaderboard=True
    )
    assert (s.skill, s.edge_verdict, s.segment, s.action) == ("no", "measured", "edge_losing", "skip")
    assert s.label == "Edge, but lost money lately"
    assert s.reason.startswith("The price moves their way after they buy (+3")
    assert s.reason.endswith("but they lost $2K from trading over these 30 days.")
    assert "doesn't clearly move" not in s.reason


def test_the_edge_verdict_is_the_price_test_alone() -> None:
    def flat(i: int, s: int) -> float:
        return 0.5 + (0.03 if i % 2 else -0.03) * (s >= 60)

    assert whales([fill(i) for i in range(30)], sharp)[0].score(WALLET).edge_verdict == "measured"
    assert whales([fill(i) for i in range(12)], sharp)[0].score(WALLET).edge_verdict == "early"
    assert whales([fill(i) for i in range(30)], flat)[0].score(WALLET).edge_verdict == "not_shown"
    assert whales([fill(i) for i in range(3)], sharp)[0].score(WALLET).edge_verdict == "unmeasured"
    # Unreadable profit leaves skill unknown, but the price test stands on its own.
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=[])[0].score(WALLET)
    assert (s.skill, s.edge_verdict) == ("unknown", "measured")
    assert s.to_dict()["edge_verdict"] == "measured"


def test_reading_fills_pages_by_time_past_the_offset_cap_and_stops_on_a_stuck_page() -> None:
    from uselayer.whale_scores import Scorer

    calls: list[dict[str, Any]] = []

    class Poly:
        def activity(self, wallet: str, **kw: Any) -> list[dict[str, Any]]:
            calls.append(kw)
            return [fill(i, at=1000) for i in range(500)]  # every page: 500 fills in the same second

    rows = Scorer(Poly())._fills(WALLET, 0, 2000, max_fills=100_000)  # type: ignore[arg-type]
    assert len(rows) == 500 and calls[-1]["end"] == 1000 and len(calls) <= 20


# ---- profit, verdicts and context ----


def test_rewards_and_rebates_are_not_trading_profit() -> None:
    # Polymarket's own figure says +$3.5K, but trading lost $500: the rest is rewards and rebates.
    w, _ = whales([fill(i) for i in range(30)], sharp, pnl_points=curve(-500, income=4000))
    s = w.score(WALLET)
    assert s.pnl_window == 3500 and s.trading_pnl_window == -500 and s.income_window == 4000
    # The price moves their way, but trading lost money: an edge that's measured, kept from being a sharp.
    assert (s.skill, s.edge_verdict, s.segment) == ("no", "measured", "edge_losing")
    assert "lost $500 from trading" in s.reason
    profitable = next(c for c in s.checks if c.rule == "profitable")
    assert profitable.passed is False and "counted apart" in profitable.detail
    assert any(r.startswith("Rewards and rebates ($4K) made more than trading") for r in s.risks)


def test_a_sharp_who_stopped_buying_is_not_trading_lately() -> None:
    w, _ = whales([fill(i, at=NOW - 10 * DAY - i * 3600) for i in range(30)], sharp)
    s = w.score(WALLET)
    assert s.segment == "inactive" and s.action == "skip" and s.skill == "yes" and not s.active
    assert s.reason.startswith("No buys in the last 12 days")
    assert next(c for c in s.checks if c.rule == "active").passed is False


def test_return_per_dollar_traded_and_profit_by_period() -> None:
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=curve(3000, traded=60_000))[0].score(WALLET)
    assert s.volume_window == 60_000 and s.return_per_dollar == pytest.approx(0.05)
    assert s.periods["7d"]["trading_pnl"] == pytest.approx(700) and s.periods["90d"]["trading_pnl"] == 3000
    assert s.periods["30d"]["return_per_dollar"] == pytest.approx(0.05)
    assert s.pnl_weeks is not None and len(s.pnl_weeks) == 12 and sum(s.pnl_weeks) == pytest.approx(3000)
    assert s.pnl_weeks[:6] == (0, 0, 0, 0, 0, 0)  # flat before the last 30 days
    assert s.pnl_daily is not None and len(s.pnl_daily) == 91
    assert s.pnl_daily[0][1] == 0 and s.pnl_daily[-1][1] == 3000


def test_profit_from_one_day_is_a_risk_not_a_verdict() -> None:
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=curve(one_day=True))[0].score(WALLET)
    assert s.segment == "quiet" and s.best_day_share == 1.0
    assert "100% of their trading profit over these 30 days came on one day." in s.risks


def test_unsettled_profit_is_a_risk() -> None:
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=curve(3000, unrealized=2000))[0].score(WALLET)
    assert any("haven't settled" in r for r in s.risks)
    assert "($2K of it on positions still open)" in next(c for c in s.checks if c.rule == "profitable").detail


def test_volume_is_dollars_traded_not_contracts() -> None:
    # 5 million contracts at cheap prices is $90K traded: a small account, so a quiet sharp, not a big one.
    stats = {"biggest_win": 500, "all_time_pnl": {"economic_pnl": 4000, "volume": 5e6, "volume_usdc": 90_000}}
    s = whales([fill(i) for i in range(30)], sharp, stats=stats)[0].score(WALLET)
    assert s.volume == 90_000 and s.segment == "quiet"


def test_how_they_trade_is_context() -> None:
    taker = [{"timestamp": NOW}] * 6  # 6 of their last 30 fills took liquidity
    s = whales([fill(i) for i in range(30)], sharp, taker=taker)[0].score(WALLET)
    assert s.typical_usd == 50 and s.avg_price == 0.5 and s.maker_share == pytest.approx(0.8)
    assert s.last_buy is not None and int(s.last_buy.timestamp()) == NOW
    d = s.to_dict()
    assert d["maker_share"] == pytest.approx(0.8) and d["last_buy"] == s.last_buy.isoformat()
    assert d["pnl_daily"][-1][1] == 3000 and d["skill"] == "yes"


def test_unreadable_profit_leaves_skill_unknown() -> None:
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=[])[0].score(WALLET)
    assert s.skill == "unknown" and s.copyable == "yes" and s.segment == "too_few"
    assert "couldn't be read" in s.reason
    assert next(c for c in s.checks if c.rule == "profitable").passed is None


def test_an_edge_that_rests_on_one_big_win_is_not_skill() -> None:
    def stats(trading: float) -> dict[str, Any]:
        return {"biggest_win": 900_000, "all_time_pnl": {"realized_pnl": trading, "unrealized_pnl": 0}}

    # The price moves their way, but without their biggest win they'd be down: lucky when the profit is big...
    s = whales([fill(i) for i in range(30)], sharp, stats=stats(600_000))[0].score(WALLET)
    assert (s.skill, s.segment) == ("no", "lucky")
    # ... and no clear edge (never Rising, which a feed would show) when it isn't.
    s = whales([fill(i) for i in range(30)], sharp, stats=stats(20_000))[0].score(WALLET)
    assert (s.skill, s.segment) == ("no", "no_edge")
    assert "without their biggest win ($900K) they'd be down from trading" in s.reason


def test_profit_from_rewards_does_not_make_a_big_bettor() -> None:
    # $77K by Polymarket's figure, but $42K of it is rewards: $35K from trading isn't a big bettor's profit.
    stats = {
        "biggest_win": 500,
        "all_time_pnl": {
            "economic_pnl": 77_000,
            "realized_pnl": 35_000,
            "unrealized_pnl": 0,
            "wallet_income": 42_000,
        },
    }
    s = whales([fill(i) for i in range(30)], sharp, stats=stats)[0].score(WALLET)
    assert s.pnl == 77_000 and s.trading_pnl == 35_000


# ---- the trader profile: how they trade and exit, their account now ----


def test_a_live_score_says_their_price_band_how_they_exit_and_their_account_now() -> None:
    prices = (0.3, 0.4, 0.5, 0.6, 0.7)
    w, seen = whales(
        [fill(i, price=prices[i % 5]) for i in range(30)], sharp, closed_positions=CLOSED["closed"], value=1234.567,
        cash=2500.75,
    )  # fmt: skip
    s = w.score(WALLET)
    assert s.price_band == (0.4, 0.6)  # the middle half of what they paid: "buys around 40–60¢"
    assert s.held_share == pytest.approx(6 / 14, abs=0.001)  # 6 of 14 held until they settled, 8 sold
    assert s.positions_usd == 1234.57 and s.cash_usd == 2500.75
    rpc = next(r for r in seen if r.url.host == "1rpc.io")
    call = json.loads(rpc.content)["params"][0]
    assert (
        call["to"] == PUSD
        and call["data"].endswith(WALLET[2:])
        and json.loads(rpc.content)["method"] == "eth_call"
    )
    d = s.to_dict()
    assert d["price_band"] == [0.4, 0.6] and d["held_share"] == s.held_share
    assert (d["positions_usd"], d["cash_usd"]) == (1234.57, 2500.75)


def test_a_past_window_leaves_out_what_describes_now() -> None:
    w, seen = whales(
        [fill(i) for i in range(30)], sharp, closed_positions=CLOSED["closed"], value=10.0, cash=5.0
    )
    s = w.score(WALLET, end=datetime.now(UTC) - timedelta(hours=1))
    assert (s.held_share, s.positions_usd, s.cash_usd) == (None, None, None)
    assert not [r for r in seen if r.url.path in ("/closed-positions", "/value") or r.url.host == "1rpc.io"]
    assert s.price_band == (0.5, 0.5)  # from the window's bets, so still given


# Polymarket's firewall page (Cloudflare's), as production got it for a 403 from the data API, 2026-10-15
FIREWALL_PAGE = (
    '<!DOCTYPE html>\n<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->\n'
    '<!--[if gt IE 8]><!--> <html class="no-js" lang="en-US"> <!--<![endif]-->\n<head><title>Attention Required! | '
    "Cloudflare</title></head><body>Sorry, you have been blocked</body></html>"
)


def _trades_reads(seen: list[httpx.Request]) -> int:
    return sum(1 for r in seen if r.url.path == "/activity" and "market" in r.url.params)


def test_only_a_403_that_is_a_web_page_is_the_firewall() -> None:
    page = VenueError("not_allowed", "x", venue="polymarket", status=403, raw=FIREWALL_PAGE)
    api = VenueError("not_allowed", "x", venue="polymarket", status=403, raw={"error": "not allowed"})
    html_500 = VenueError("venue_unavailable", "x", venue="polymarket", status=500, raw=FIREWALL_PAGE)
    assert is_firewall_block(page) and not is_firewall_block(api) and not is_firewall_block(html_500)


def test_a_read_the_firewall_turns_away_is_tried_once_more_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws, "FIREWALL_RETRY_S", 0.0)
    whole, seen_whole = whales([fill(i) for i in range(30)], sharp, closed_positions=CLOSED["closed"])
    full = whole.score(WALLET)
    before = ws.firewall_retries()
    w, seen = whales([fill(i) for i in range(30)], sharp, closed_positions=CLOSED["closed"], firewall=1)
    s = w.score(WALLET)
    after = ws.firewall_retries()
    # complete, as without the firewall (value and cash aren't given here, so both are unread either way)
    assert s.held_share == full.held_share == pytest.approx(6 / 14, abs=0.001)
    assert "held_share" not in s.unread and s.unread == full.unread
    assert _trades_reads(seen) == _trades_reads(seen_whole) + 1  # the one extra read
    assert {k: after[k] - before[k] for k in after} == {"retried": 1, "recovered": 1, "failed": 0}


def test_when_the_firewall_turns_the_retry_away_too_the_figure_is_unread_never_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws, "FIREWALL_RETRY_S", 0.0)
    full = whales([fill(i) for i in range(30)], sharp, closed_positions=CLOSED["closed"])[0].score(WALLET)
    before = ws.firewall_retries()
    w, seen = whales([fill(i) for i in range(30)], sharp, closed_positions=CLOSED["closed"], firewall=-1)
    s = w.score(WALLET)
    after = ws.firewall_retries()
    # unread, and says so: not "fewer than HELD_MIN closed positions", and not a share of whichever were read
    assert s.held_share is None and "held_share" in s.unread and s.to_dict()["unread"] == list(s.unread)
    assert _trades_reads(seen) == 2  # the first read, and one more: it stops there
    assert {k: after[k] - before[k] for k in after} == {"retried": 1, "recovered": 0, "failed": 1}
    assert (s.segment, s.skill, s.copyable) == (full.segment, full.skill, full.copyable)  # context only
    # a trader page (not strict) still gets the positions, with how they got out unknown, never guessed
    w, _ = whales([], sharp, closed_positions=CLOSED["closed"], firewall=-1)
    shown = ws.closed_positions(w._poly, WALLET)
    assert shown and all(c.avg_price is None and c.exit_price is None and c.result is None for c in shown)
    with pytest.raises(VenueError):
        ws.closed_positions(w._poly, WALLET, strict=True)


def test_a_403_the_api_answers_itself_is_never_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "FIREWALL_RETRY_S", 0.0)
    calls: list[int] = []

    def refused() -> list[Any]:
        calls.append(1)
        raise VenueError("not_allowed", "x", venue="polymarket", status=403, raw={"error": "not allowed"})

    before = ws.firewall_retries()
    with pytest.raises(VenueError):
        ws._once_more(refused)
    assert calls == [1] and ws.firewall_retries() == before


def test_figures_that_cant_be_read_or_rest_on_too_little_are_none_not_zero() -> None:
    s = whales([fill(i) for i in range(30)], sharp)[0].score(WALLET)  # closed, value and cash unreadable
    assert (s.held_share, s.positions_usd, s.cash_usd) == (None, None, None)
    assert s.segment == "quiet"  # the score itself doesn't depend on them
    s = whales([fill(i) for i in range(4)], sharp, closed_positions=CLOSED["closed"][:9])[0].score(WALLET)
    assert s.price_band is None and s.held_share is None  # 4 bets, 9 closed positions: too few to say
    assert whales([], sharp, cash=0.0)[0].score(WALLET).cash_usd == 0.0  # an empty wallet is 0, not None


def test_scoring_one_wallet_gives_the_same_profit_history_as_discover() -> None:
    d = whales([fill(i) for i in range(30)], sharp)[0].score(WALLET).to_dict()
    assert len(d["pnl_weeks"]) == 12 and len(d["pnl_daily"]) == 91
    assert set(d["periods"]) == {"7d", "30d", "90d"} and d["periods"]["30d"]["trading_pnl"] == 3000


# ---- cash, read from the Polygon chain ----


def polygon(answers: list[httpx.Response], env: str | None = None) -> tuple[Polygon, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return answers[min(len(seen), len(answers)) - 1]

    return Polygon(Http(transport=httpx.MockTransport(handler), sleep=lambda s: None, limits={}), env), seen


def test_cash_is_the_wallets_pusd_balance_in_dollars() -> None:
    chain, seen = polygon(
        [httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 54 + "9502f900"})]
    )
    assert chain.cash("0x" + "AB" * 20) == 2500.0  # 2,500,000,000 at 6 decimals
    body = json.loads(seen[0].content)
    assert seen[0].url.host == "1rpc.io" and body["method"] == "eth_call"
    assert body["params"] == [{"to": PUSD, "data": "0x70a08231" + "0" * 24 + "ab" * 20}, "latest"]
    with pytest.raises(ValueError):
        chain.cash("0x123")  # never sent


def test_a_refused_cash_read_is_tried_once_more_then_the_next_node(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(5_000_000)})
    refused = httpx.Response(403, json={"error": "The network of this endpoint requires a key"})
    chain, seen = polygon([refused, ok])
    assert chain.cash(WALLET) == 5.0 and [r.url.host for r in seen] == ["1rpc.io", "1rpc.io"]
    chain, seen = polygon([refused, refused, ok])  # the first node refuses twice: the next one answers
    assert chain.cash(WALLET) == 5.0
    assert [r.url.host for r in seen] == ["1rpc.io", "1rpc.io", "polygon-bor-rpc.publicnode.com"]
    chain, seen = polygon([refused])  # every node refuses: an error, never a balance of 0
    with pytest.raises(VenueError):
        chain.cash(WALLET)
    assert len(seen) == 2 * len(RPCS)
    no_result = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})
    with pytest.raises(VenueError):
        polygon([no_result])[0].cash(WALLET)
    monkeypatch.setenv("USELAYER_POLYGON_RPC", "https://node.example/polygon")
    chain, seen = polygon([refused, refused, ok])
    with pytest.raises(VenueError):
        chain.cash(WALLET)  # your own node is the only one asked
    assert {str(r.url) for r in seen} == {"https://node.example/polygon"}


def test_a_node_that_doesnt_answer_is_skipped_for_the_next() -> None:
    calls: list[httpx.Request] = []

    def hang(r: httpx.Request) -> httpx.Response:
        calls.append(r)
        raise httpx.ReadTimeout("no answer", request=r)

    chain = Polygon(Http(transport=httpx.MockTransport(hang), sleep=lambda s: None, limits={}))
    with pytest.raises(VenueError) as e:
        chain.cash(WALLET)
    assert e.value.code == "venue_unavailable"
    assert [r.url.host for r in calls] == [u.split("/")[2] for u in RPCS]  # each node once, no retry

    def first_hangs(r: httpx.Request) -> httpx.Response:
        if r.url.host == "1rpc.io":
            raise httpx.ReadTimeout("no answer", request=r)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(7_250_000)})

    chain = Polygon(Http(transport=httpx.MockTransport(first_hangs), sleep=lambda s: None, limits={}))
    assert chain.cash(WALLET) == 7.25


def test_a_score_doesnt_wait_long_for_a_slow_cash_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import uselayer.whale_scores as ws

    monkeypatch.setattr(ws, "NOW_WAIT_S", 0.05)
    w, _ = whales([fill(i) for i in range(30)], sharp, value=10.0, cash=5.0)
    slow = w.scorer()._chain
    assert slow is not None
    real = slow.cash
    monkeypatch.setattr(slow, "cash", lambda wallet: (time.sleep(1), real(wallet))[1])
    started = time.monotonic()
    s = w.score(WALLET)
    assert s.cash_usd is None and s.positions_usd == 10.0  # the slow one is left out, the rest kept
    assert time.monotonic() - started < 0.9


def test_scores_reuse_the_shared_read_threads_instead_of_starting_new_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # each score used to start its own pools (up to 13 threads), and that churn grew glibc's per-thread memory.
    # Now however many scores, no more threads than the shared pools hold (here 3 and 4; ~9 a score before).
    import uselayer.whale_scores as ws

    monkeypatch.setattr(ws, "_pools", {})
    monkeypatch.setattr(ws, "NOW_READ_WORKERS", 3)
    monkeypatch.setattr(ws, "PRICE_READ_WORKERS", 4)
    started: list[str] = []
    real = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda t: (started.append(t.name), real(t))[1])
    w, _ = whales(
        [fill(i) for i in range(30)], sharp, closed_positions=CLOSED["closed"], value=10.0, cash=5.0
    )  # fmt: skip
    first = w.score(WALLET)
    again = [w.score(WALLET) for _ in range(9)]
    for _ in range(5):
        w.account(WALLET)
    assert len(started) <= 3 + 4
    assert all(n.startswith(("uselayer-now", "uselayer-prices")) for n in started)
    same = first.to_dict() | {"account_at": None}  # when the account was read: each score's own
    assert all(s.to_dict() | {"account_at": None} == same for s in again)  # whichever threads read it


def test_a_read_still_waiting_for_a_thread_at_the_deadline_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uselayer.whale_scores as ws

    monkeypatch.setattr(ws, "_pools", {})
    monkeypatch.setattr(ws, "NOW_READ_WORKERS", 1)
    gate = threading.Event()
    busy = ws._pool("now").submit(gate.wait)  # the only thread, busy
    ran: list[str] = []
    queued = ws._pool("now").submit(lambda: ran.append("read") or 1.0)
    assert ws._by(queued, time.monotonic() + 0.05, WALLET, "cash") is None
    gate.set()
    busy.result(timeout=5)
    ws._pool("now").submit(lambda: None).result(timeout=5)  # the queue has drained
    assert queued.cancelled() and ran == []


def test_a_forked_child_starts_its_own_pools_even_if_the_lock_was_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uselayer.whale_scores as ws

    monkeypatch.setattr(ws, "_pools", {})
    monkeypatch.setattr(ws, "_pools_lock", threading.Lock())
    ws._pool("now")
    held = ws._pools_lock
    held.acquire()  # another thread was starting a pool at the fork
    ws._after_fork()  # what the child runs
    assert ws._pools == {} and ws._pools_lock is not held and not ws._pools_lock.locked()
    assert ws._pool("now").submit(lambda: 7).result(timeout=5) == 7
    held.release()


# ---- copyable only when it's reliably above zero ----


def test_copying_is_copyable_only_when_the_bottom_of_its_95_range_is_above_zero() -> None:
    def noisy(i: int, s: int) -> float:
        """The sharp edge at 5 minutes, but the hour mark swings 8¢ either way: copies gain 2¢ on average,
        with a range that still reaches below zero."""
        wobble = 1 if i % 2 else -1
        if s < 60:
            return 0.5
        if s < 300:
            return 0.52
        if s < 3600:
            return 0.53 + 0.005 * wobble
        return 0.56 + 0.08 * wobble

    s = whales([fill(i) for i in range(30)], noisy)[0].score(WALLET)
    assert s.copy is not None and s.copy.mean > 0 and s.copy.low < 0
    assert 1 <= s.copy.z < 2  # the old rule (mean > 0, z ≥ 1) called this copyable
    assert (s.skill, s.copyable, s.segment) == ("yes", "no", "quiet")
    check = next(c for c in s.checks if c.rule == "copyable")
    assert check.passed is False and "not reliably above zero" in check.detail and "95% range" in check.detail


def test_too_few_copy_results_leave_copyable_unknown() -> None:
    s = whales([fill(i) for i in range(6)], sharp)[0].score(WALLET)
    assert s.copy is not None and s.copy.n == 6 and s.copy.low > 0  # reliably positive, but on 6 bets
    assert s.copyable == "unknown"
    check = next(c for c in s.checks if c.rule == "copyable")
    assert check.passed is None and "we want 8" in check.detail
    assert copyable_verdict(None) == "unknown"
    assert copyable_verdict(Stat(mean=0.01, margin=0.009, n=8, z=2.3)) == "yes"
    assert copyable_verdict(Stat(mean=0.01, margin=0.01, n=8, z=2.0)) == "no"  # the range touches zero


def test_a_95_range_on_a_few_bets_uses_students_t() -> None:
    st = _stat({"a": [0.01], "b": [0.03], "c": [0.02], "d": [0.04], "e": [0.0]})
    assert st is not None and st.n == 5
    se = 0.0158114 / 5**0.5
    assert st.margin == pytest.approx(2.776 * se, abs=1e-5)  # t for 4 degrees of freedom, not 1.96
    assert (st.low, st.high) == (round(st.mean - st.margin, 5), round(st.mean + st.margin, 5))
    assert st.to_dict()["low"] == st.low
    assert t95(1) == 12.706 and t95(30) == 2.042 and t95(200) == pytest.approx(1.972, abs=0.002)
    assert Stat.from_dict(st.to_dict()) == st
    old = {
        "mean": 0.02,
        "margin": 1.96 * 0.005,
        "n": 5,
        "z": 4.0,
    }  # kept by uselayer 0.7: 1.96 errors at any n
    assert Stat.from_dict(old).margin == pytest.approx(2.776 * 0.005, abs=1e-5)


# ---- profit: trading only, realized and unrealized apart, weeks with history only ----


def test_trading_profit_splits_into_realized_and_unrealized() -> None:
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=curve(3000, unrealized=2000, income=900))[
        0
    ].score(WALLET)
    assert (s.trading_pnl_window, s.realized_window, s.unrealized_window) == (3000, 1000, 2000)
    assert s.income_window == 900  # rewards stay out of every trading figure
    for p in s.periods.values():
        assert p["realized"] is not None and p["unrealized"] is not None and p["trading_pnl"] is not None
        assert p["realized"] + p["unrealized"] == pytest.approx(p["trading_pnl"])
    d = s.to_dict()
    assert d["realized_window"] == 1000 and d["periods"]["30d"]["unrealized"] == 2000


def test_weeks_before_their_history_starts_are_none_not_losing_or_flat() -> None:
    now = int(time.time())
    young = [p for p in curve(3000) if p["timestamp"] >= now - 20 * DAY]  # their first trade 20 days ago
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=young)[0].score(WALLET)
    assert s.pnl_weeks is not None and len(s.pnl_weeks) == 12
    assert s.pnl_weeks[:9] == (None,) * 9  # those weeks ended before their first trade
    known = [w for w in s.pnl_weeks if w is not None]
    assert len(known) == 3 and sum(known) == pytest.approx(3000)
    assert s.pnl_since is not None and abs(s.pnl_since.timestamp() - young[0]["timestamp"]) < 1
    assert s.to_dict()["pnl_weeks"][:9] == [None] * 9
    # An account older than 12 weeks has all 12, flat ones included.
    s = whales([fill(i) for i in range(30)], sharp)[0].score(WALLET)
    assert s.pnl_weeks is not None and None not in s.pnl_weeks and s.pnl_weeks[0] == 0


def test_fills_from_is_set_only_when_the_cap_cut_the_window_short() -> None:
    # Every trade in the window read: None, even though their first trade is only 29 hours before the end.
    w, _ = whales([fill(i) for i in range(30)], sharp)
    assert w.score(WALLET).fills_from is None
    assert whales([], sharp)[0].score(WALLET).fills_from is None
    # The cap reached: the newest 10 of their 30 trades were read, back to the 10th newest.
    s = w.scorer().score(WALLET, max_fills=10)
    assert s.fills_from is not None and int(s.fills_from.timestamp()) == NOW - 9 * 3600
    assert s.to_dict()["fills_from"] == s.fills_from.isoformat()
    # Exactly at the cap counts as cut short: there may have been more.
    assert w.scorer().score(WALLET, max_fills=30).fills_from is not None


# ---- their account now: both parts, or say which is missing ----


def test_an_account_is_complete_only_when_both_parts_were_read() -> None:
    w, _ = whales([], sharp, value=1234.567, cash=2500.75)
    a = w.account(WALLET)
    assert (a.positions_usd, a.cash_usd, a.total_usd, a.complete, a.missing) == (
        1234.57,
        2500.75,
        3735.32,
        True,
        (),
    )
    assert abs(a.read_at.timestamp() - time.time()) < 5
    d = a.to_dict()
    assert d["total_usd"] == 3735.32 and d["complete"] is True and d["read_at"] == a.read_at.isoformat()
    a = whales([], sharp, value=1234.567)[0].account(WALLET)  # cash unreadable
    assert (a.positions_usd, a.cash_usd, a.total_usd, a.complete, a.missing) == (
        1234.57,
        None,
        None,
        False,
        ("cash",),
    )
    a = whales([], sharp, cash=0.0)[0].account(WALLET)  # an empty wallet is $0 cash; positions unreadable
    assert (a.cash_usd, a.total_usd, a.missing) == (0.0, None, ("positions",))


def test_a_live_score_says_when_its_account_was_read() -> None:
    s = whales([fill(i) for i in range(30)], sharp, value=10.0, cash=5.0)[0].score(WALLET)
    assert s.account_at is not None and abs(s.account_at.timestamp() - time.time()) < 30
    assert whales([fill(i) for i in range(30)], sharp)[0].score(WALLET).account_at is None  # nothing read


# ---- a kept score's tier, by this version's rule ----


def test_a_kept_score_gets_the_same_tier_it_was_given() -> None:
    def flat(i: int, s: int) -> float:
        return 0.5 + (0.03 if i % 2 else -0.03) * (s >= 60)

    big = {
        "biggest_win": 10_000,
        "all_time_pnl": {"realized_pnl": 600_000, "unrealized_pnl": 0, "volume_usdc": 5e6},
    }
    lucky = {"biggest_win": 900_000, "all_time_pnl": {"realized_pnl": 600_000, "unrealized_pnl": 0}}
    for fills, path, kw in (
        ([fill(i) for i in range(30)], sharp, {}),  # quiet
        ([fill(i) for i in range(45)], sharp, {"on_leaderboard": True}),  # proven
        ([fill(i) for i in range(30)], jump, {}),  # quiet, copies lose
        ([fill(i) for i in range(12)], jump, {}),  # rising
        ([fill(i) for i in range(30)], flat, {"stats": big}),  # big profit, edge unproven
        ([fill(i) for i in range(30)], sharp, {"stats": lucky}),  # profit rests on one win
        ([fill(i) for i in range(3)], sharp, {}),  # not enough data
        ([fill(i) for i in range(30)], flat, {}),  # edge not proven
        ([fill(i, at=NOW - 10 * DAY - i * 3600) for i in range(30)], sharp, {}),  # not trading lately
        ([fill(i) for i in range(30)], sharp, {"pnl_points": curve(-500)}),  # edge, but lost money lately
        (
            [fill(i) for i in range(3)] + parlays(30),
            sharp,
            {"markets": [market(i) for i in range(3)]},
        ),  # parlays
    ):
        on_board = kw.pop("on_leaderboard", False)
        s = whales(fills, path, **kw)[0].score(WALLET, on_leaderboard=on_board)
        assert segment_of(json.loads(json.dumps(s.to_dict()))) == s.segment


def test_a_score_kept_before_0_10_is_given_its_tier_now() -> None:
    s = whales([fill(i) for i in range(30)], jump)[0].score(WALLET).to_dict()
    assert segment_of(s | {"segment": "too_fast"}) == "quiet"  # a sharp whose copies don't pay: still a sharp
    s = whales([fill(i) for i in range(12)], jump)[0].score(WALLET).to_dict()
    assert segment_of(s | {"segment": "no_edge"}) == "rising"  # early signs, held back only by copies losing
    assert segment_of(s | {"skill": "unknown"}) == "too_few"
    assert segment_of(s | {"style": "market_maker"}) == "no_view"


def test_a_score_kept_before_0_11_is_given_its_tier_now() -> None:
    # Before 0.11.0 there was no edge_verdict, and combos counted among the 60 bets picked.
    s = whales([fill(i) for i in range(30)], sharp, pnl_points=curve(-500))[0].score(WALLET).to_dict()
    old = {k: v for k, v in s.items() if k != "edge_verdict"} | {"segment": "no_edge"}
    assert (
        segment_of(old) == "edge_losing"
    )  # the losing window was the only thing keeping a measured edge out
    s = whales([fill(i) for i in range(30)], sharp, markets=[market(99)])[0].score(WALLET).to_dict()
    old = {k: v for k, v in s.items() if k != "edge_verdict"} | {"segment": "too_few", "decisions": 2672}
    old["coverage"] = old["coverage"] | {"bets": 0, "combos": 60}  # all 60 picked were parlays
    assert segment_of(old) == "parlays"


# ---- which bets a score checks ----


def decision(i: int, at: int) -> _Decision:
    return _Decision(f"0xc{i}", 0, f"tok{i}-0", f"event-{i}", None, None, at, 0.5, 100, 50)


def test_the_sample_is_stable_when_the_window_moves() -> None:
    # 600 decisions an hour apart; the window then moves 6 hours: 6 old ones leave, 6 new ones come in.
    before = [decision(i, NOW - (600 - i) * 3600) for i in range(600)]
    after = before[6:] + [decision(600 + i, NOW + i * 3600) for i in range(6)]
    a, b = (
        {d.condition for d in sample_decisions(before, 60)},
        {d.condition for d in sample_decisions(after, 60)},
    )
    assert len(a) == len(b) == 60
    assert len(a & b) >= 58  # only what left or came in can change, about 1 in 10 of those

    # every k-th from the oldest (before 0.13): the same move replaced most of the sample
    def stride(ds: list[_Decision]) -> set[str]:
        return {d.condition for d in ds[:: math.ceil(len(ds) / 60)][:60]}

    assert len(stride(before) & stride(after)) < 15


def test_a_decision_keeps_its_place_when_its_first_buy_leaves_the_window() -> None:
    early = [decision(i, NOW - 1000 + i) for i in range(200)]
    later = [decision(0, NOW + 5000), *early[1:]]  # decision 0's first buy left; a later one is now its first
    assert ("0xc0" in {d.condition for d in sample_decisions(early, 60)}) == (
        "0xc0" in {d.condition for d in sample_decisions(later, 60)}
    )


def test_the_sample_spreads_over_the_whole_window_oldest_first() -> None:
    # 3,000 decisions evenly over 30 days: the 60 checked come from every part of it, in time order.
    ds = [decision(i, NOW - 30 * DAY + i * 864) for i in range(3000)]
    picked = sample_decisions(ds, 60)
    assert [d.at for d in picked] == sorted(d.at for d in picked)
    per_third = [
        sum(1 for d in picked if NOW - (30 - 10 * k) * DAY <= d.at < NOW - (20 - 10 * k) * DAY)
        for k in range(3)
    ]
    assert all(12 <= n <= 28 for n in per_third), per_third  # 20 each expected
    per_tenth = {(d.at - (NOW - 30 * DAY)) * 10 // (30 * DAY) for d in picked}
    assert len(per_tenth) == 10  # every tenth of the month has at least one


def test_with_fewer_decisions_than_the_sample_every_one_is_checked() -> None:
    ds = [decision(i, NOW - i) for i in range(30)]
    assert sample_decisions(ds, 60) == ds and sample_decisions(ds, 0) == ds


def busy(n: int, every: int, *, span: int = 29 * DAY) -> list[dict[str, Any]]:
    """A busy trader: ``n`` buys ``every`` seconds apart going back from NOW, each in its own market."""
    return [fill(i, at=NOW - i * every) for i in range(n) if i * every < span]


def test_a_busy_traders_bets_span_the_whole_window_not_only_the_last_trades_read() -> None:
    fills = busy(1400, 1800)  # a buy every 30 minutes for 29 days
    w, seen = whales(fills, sharp)
    s = w.scorer().score(WALLET, max_fills=200)  # the cap reached about 4 days back
    assert (
        s.fills_from is not None and NOW - s.fills_from.timestamp() < 5 * DAY
    )  # where the full read stopped
    reads = [r for r in seen if r.url.path == "/activity" and r.url.params.get("type") == "TRADE"]
    assert len(reads) == 1 + 10  # the capped read (one page here), then a page from each of 10 slices
    assert s.coverage["spread"] > 0 and s.decisions > 200
    checked = [b.at.timestamp() for b in s.sample]
    window = 30 * DAY
    tenths = {int((t - (NOW - window)) * 10 // window) for t in checked}
    assert len(tenths) >= 9, tenths  # from every part of the month, not its last 4 days
    assert min(checked) < NOW - 20 * DAY


def test_style_and_the_bot_tag_stay_on_the_trades_read_in_full() -> None:
    # Two-way trading is judged on the trades read in full, as before: the spread pages add bets, not style.
    w, _ = whales(busy(1400, 1800), sharp)
    s = w.scorer().score(WALLET, max_fills=200)
    assert s.style == "takes_a_side" and "bot" in s.tags  # the cap still marks a busy trader


def test_a_window_read_in_full_reads_nothing_more() -> None:
    w, seen = whales([fill(i) for i in range(30)], sharp)
    s = w.score(WALLET)
    assert s.coverage["spread"] == 0
    assert len([r for r in seen if r.url.path == "/activity" and r.url.params.get("type") == "TRADE"]) == 1


def test_a_slice_that_cant_be_read_is_left_out() -> None:
    w, _ = whales(busy(1400, 1800), sharp)
    sc = w.scorer()
    real = sc._poly.activity
    calls = {"n": 0}

    def flaky(wallet: str, **kw: Any) -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] in (3, 4):  # two of the slices
            raise VenueError("venue_unavailable", "Polymarket answered 502.", venue="polymarket")
        return real(wallet, **kw)

    sc._poly.activity = flaky  # type: ignore[method-assign]
    s = sc.score(WALLET, max_fills=200)
    assert s.coverage["spread"] > 0 and s.segment is not None  # scored on the rest


def test_a_busy_traders_slice_pages_land_inside_each_slice_not_at_its_end() -> None:
    w, seen = whales(busy(1400, 1800), sharp)
    w.scorer().score(WALLET, max_fills=200)
    pages = [r for r in seen if r.url.path == "/activity" and r.url.params.get("type") == "TRADE"][1:]
    assert len(pages) == 10
    where = [(int(r.url.params["end"]) - int(r.url.params["start"])) for r in pages]
    span = max(where) / 0.99 if where else 1
    assert (
        min(where) < 0.5 * span < max(where)
    )  # some pages end early in their slice, some late: not all at its end


def test_slice_pages_land_at_the_same_points_each_time() -> None:
    from uselayer.whale_scores import _point

    assert _point(WALLET, 3, 1000, 2000) == _point(WALLET, 3, 1000, 2000)
    points = [(_point(WALLET, k, 0, 10_000) / 10_000) for k in range(10)]
    assert len({round(p, 2) for p in points}) >= 8 and min(points) < 0.4 and max(points) > 0.6


def test_a_busy_traders_checked_bets_follow_time_not_how_much_was_read_in_full() -> None:
    # 1,400 buys over 29 days; the cap reads the latest 200 (about 4 days) in full and a page from each slice.
    s = whales(busy(1400, 1800), sharp)[0].scorer().score(WALLET, max_fills=200)
    assert s.fills_from is not None
    recent = s.window[1].timestamp() - s.fills_from.timestamp()  # the part read in full, to the window end
    share = recent / (30 * DAY)
    checked_recent = sum(1 for b in s.sample if b.at.timestamp() >= s.fills_from.timestamp())
    assert checked_recent <= round(60 * share) + 2  # its share of the month, not its share of what was read


def test_strata_split_the_sample_by_time() -> None:
    # 500 decisions in the last tenth of the window, 50 in the rest: by time, the rest gets 9 tenths of the sample.
    dense = [decision(i, NOW - i * 60) for i in range(500)]
    sparse = [decision(1000 + i, NOW - 3 * DAY - 3600 - i * 3 * 3600) for i in range(50)]
    strata = [(NOW - 30 * DAY, NOW - 3 * DAY), (NOW - 3 * DAY, NOW + 1)]
    picked = sample_decisions(dense + sparse, 60, strata)
    assert len(picked) == 60
    assert sum(1 for d in picked if d.at < NOW - 3 * DAY) == 50  # all 50 (its share is 54: it gives 4 back)
    plain = sample_decisions(dense + sparse, 60)
    assert sum(1 for d in plain if d.at < NOW - 3 * DAY) < 15  # without strata the dense part crowds it out


# ---- what a scorer keeps in memory ----


class _Reads:
    """Stands in for PolymarketData: counts the reads PriceTape and MarketBook make."""

    def __init__(self) -> None:
        self.prices: list[str] = []
        self.markets_asked: list[str] = []

    def price_history(self, token: str, start: int, end: int) -> list[tuple[int, float]]:
        self.prices.append(token)
        return [(start, 0.5), (end, 0.6)]

    def markets(self, ids: list[str], closed: bool = False) -> list[dict[str, Any]]:
        self.markets_asked += [] if closed else ids
        return [{"conditionId": c, "question": c} for c in ids if c != "gone"]


def test_the_price_tape_keeps_the_most_recently_used_ranges() -> None:
    reads = _Reads()
    tape = PriceTape(reads, keep=2)  # type: ignore[arg-type]
    tape.series("a", 0, 100)
    tape.series("b", 0, 100)
    tape.series("a", 10, 50)  # inside a's range: from memory, and a is now the most recently used
    tape.series("c", 0, 100)  # one range too many: b, the least recently used, goes
    assert reads.prices == ["a", "b", "c"]
    tape.series("a", 0, 100)
    tape.series("b", 0, 100)  # read again
    assert reads.prices == ["a", "b", "c", "b"]
    assert sum(len(v) for v in tape._mem.values()) <= 2


def test_the_market_book_keeps_the_most_recently_used_markets() -> None:
    reads = _Reads()
    book = MarketBook(reads, keep=3)  # type: ignore[arg-type]
    assert set(book.get(["m1", "m2", "gone"])) == {"m1", "m2"}  # "gone" isn't found, and that's kept too
    book.get(["m1"])  # m1 is now the most recently used
    assert set(book.get(["m3", "m4"])) == {"m3", "m4"}  # over 3: m2 and "gone" go, m1 stays
    asked = len(reads.markets_asked)
    assert set(book.get(["m1", "m3", "m4"])) == {"m1", "m3", "m4"} and len(reads.markets_asked) == asked
    assert set(book.get(["m2"])) == {"m2"} and reads.markets_asked[asked:] == ["m2"]  # read again
    assert len(book._known) == 3
    many = [f"x{i}" for i in range(10)]  # one call asking for more than it keeps still gets all of them
    assert set(book.get(many)) == set(many) and len(book._known) == 3
