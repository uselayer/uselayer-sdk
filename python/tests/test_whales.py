"""Whale tracking and copy trading: one trade shape for both venues, links with evidence, paper copies."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import T0, Clock, FakeVenue

from uselayer import Resolution, Trader, WhaleTrade
from uselayer.errors import VenueError
from uselayer.http import Http
from uselayer.layer_api import LayerApi
from uselayer.whales import Whales, _kalshi_trade, _polymarket_trade

KALSHI = "api.elections.kalshi.com"
DATA = "data-api.polymarket.com"
GAMMA = "gamma-api.polymarket.com"


def kalshi_row(**kw: Any) -> dict[str, Any]:
    row = {
        "trade_id": "t1",
        "ticker": "KXGAME-26OCT07-ATL",
        "price": 59,
        "price_dollars": "0.5900",
        "count": 10,
        "count_fp": "10.00",
        "taker_side": "no",
        "taker_action": "buy",
        "maker_nickname": "",
        "taker_nickname": "",
        "create_date": "2026-10-01T12:00:00Z",
    }
    return row | kw


def poly_row(**kw: Any) -> dict[str, Any]:
    row = {
        "proxyWallet": "0xabc0000000000000000000000000000000000001",
        "timestamp": int(T0.timestamp()),
        "conditionId": "0xcid",
        "size": 100,
        "usdcSize": 41,
        "price": 0.41,
        "side": "BUY",
        "outcomeIndex": 1,
        "title": "Dodgers vs. Braves",
        "outcome": "Braves",
        "name": "whale",
        "transactionHash": "0xtx",
    }
    return row | kw


def whales(routes: dict[tuple[str, str], Any], layer_key: str | None = None) -> Whales:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get((request.url.host, request.url.path))
        if callable(body):
            body = body(request)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    http = Http(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return Whales(http, LayerApi(layer_key, http))


# ---- one trade shape ----


def test_kalshi_price_is_the_yes_price_and_a_no_taker_pays_the_rest() -> None:
    t = _kalshi_trade(kalshi_row(taker_nickname="bob"), None, None)
    assert (t.trader, t.role, t.side, t.action) == ("bob", "taker", "no", "buy")
    assert t.price == pytest.approx(0.41) and t.usd == pytest.approx(4.1)
    assert t.long_yes is False


def test_a_kalshi_maker_took_the_other_direction() -> None:
    t = _kalshi_trade(kalshi_row(taker_nickname="bob", maker_nickname="amy"), "amy", None)
    assert (t.trader, t.role, t.side, t.action, t.price) == ("amy", "maker", "yes", "buy", 0.59)
    assert t.long_yes is True


def test_polymarket_trade_keys_the_outcome_and_hides_raw_wallet_names() -> None:
    t = _polymarket_trade(poly_row(name="0xabc0000000000000000000000000000000000001-17834"))
    assert t.market == "0xcid:1" and t.side == "yes" and t.action == "buy"
    assert t.name == "0xabc0…0001" and t.usd == 41


def test_polymarket_trade_links_to_its_market_page() -> None:
    event = "https://polymarket.com/event/nba-sample-game"
    t = _polymarket_trade(poly_row(eventSlug="nba-sample-game", slug="nba-sample-game-total"))
    assert t.url == event + "/nba-sample-game-total"  # one market of several in the event
    assert _polymarket_trade(poly_row(eventSlug="nba-sample-game", slug="nba-sample-game")).url == event
    assert _polymarket_trade(poly_row()).url is None and _kalshi_trade(kalshi_row(), None, None).url is None


# ---- traders ----


def test_top_merges_both_venues_by_profit_and_skips_anonymous_kalshi_rows() -> None:
    w = whales(
        {
            (KALSHI, "/v1/social/leaderboard"): {
                "rank_list": [
                    {"nickname": "kbig", "rank": 1, "value": 5_000_000, "is_anonymous": False},
                    {"nickname": "anon", "rank": 2, "value": 4_000_000, "is_anonymous": True},
                ]
            },
            (DATA, "/v1/leaderboard"): [
                {"rank": "1", "proxyWallet": "0xp1", "userName": "pbig", "pnl": 9_000_000, "vol": 1e8},
                {"rank": "2", "proxyWallet": "0xp2", "userName": "", "pnl": 1_000_000, "vol": 1e7},
            ],
        }
    )
    top = w.top(by="pnl", limit=5)
    assert [(t.venue, t.name) for t in top] == [
        ("polymarket", "pbig"),
        ("kalshi", "kbig"),
        ("polymarket", "0xp2"),
    ]
    assert top[1].volume_unit == "contracts" and top[0].url and top[0].url.endswith("0xp1")
    assert top[0].volume_unit == "contracts"  # Polymarket's leaderboard vol counts contracts too


def test_kalshi_trader_who_hides_trades_says_so_and_pnl_is_in_dollars() -> None:
    w = whales(
        {
            (KALSHI, "/v1/social/profile"): {"social_profile": {"nickname": "kbig", "follower_count": 3}},
            (KALSHI, "/v1/social/profile/metrics"): {
                "metrics": {"pnl": 82_370_179_034, "volume_fp": "1000.00"}
            },
            (KALSHI, "/v1/social/profile/holdings"): {"holdings": [], "visibility_state": "hidden"},
            (KALSHI, "/v1/social/trades"): {"trades": [], "visibility_state": "hidden"},
        }
    )
    d = w.trader("kalshi", "kbig")
    assert d.visibility == "hidden" and d.trades == [] and d.stats["pnl"] == pytest.approx(8_237_017.9)
    assert d.closed is None  # Kalshi doesn't publish closed positions: unknown, not "none"


# ---- closed positions (Polymarket) ----

# Polymarket's answers for one wallet that mostly sells before its markets settle, recorded 2026-10-12.
CLOSED = json.loads((Path(__file__).parent / "fixtures" / "whales" / "closed_positions.json").read_text())
ME = "0x00000000000000000000000000000000000000aa"


def poly_trader(*, closed: Any = None, trades: Any = None) -> Whales:
    """A Polymarket trader page on the recorded closed positions and trades, unless replaced."""
    rows = CLOSED["closed"] if closed is None else closed

    def closed_page(r: httpx.Request) -> Any:
        q = r.url.params
        start = int(q.get("offset", 0))
        return rows[start : start + int(q["limit"])]

    def activity(r: httpx.Request) -> Any:
        if "market" not in r.url.params:
            return []  # their recent trades, not the subject here
        wanted = set(r.url.params["market"].split(","))
        return [t for t in (CLOSED["trades"] if trades is None else trades) if t["conditionId"] in wanted]

    return whales(
        {
            (GAMMA, "/public-profile"): {"proxyWallet": ME, "name": "seller"},
            (DATA, "/v2/user-stats"): {"data": {}},
            (DATA, "/positions"): [],
            (DATA, "/activity"): activity,
            (DATA, "/closed-positions"): closed_page,
        }
    )


def test_a_polymarket_trader_page_lists_closed_positions_newest_first_with_how_they_got_out() -> None:
    won = CLOSED["closed"][-1]  # held until it settled, and won
    cost = won["avgPrice"] * won["totalBought"]
    lost = won | {
        "asset": "tok-lost",
        "conditionId": "0xlost",
        "curPrice": 0,
        "realizedPnl": -cost,
        "timestamp": 2,
    }
    pending = won | {
        "asset": "tok-open",
        "conditionId": "0xopen",
        "curPrice": 0.4,
        "realizedPnl": 0,
        "timestamp": 1,
    }
    buys = [
        {
            "conditionId": r["conditionId"],
            "asset": r["asset"],
            "side": "BUY",
            "size": r["totalBought"],
            "price": 0.4,
        }
        for r in (lost, pending)
    ]
    w = poly_trader(closed=[*CLOSED["closed"], lost, pending], trades=[*CLOSED["trades"], *buys])
    d = w.trader("polymarket", ME)
    assert d.closed is not None and len(d.closed) == 16
    assert [c.closed_at for c in d.closed] == sorted((c.closed_at for c in d.closed), reverse=True)
    by: dict[str | None, list[Any]] = {}
    for c in d.closed:
        by.setdefault(c.result, []).append(c)
    assert {k: len(v) for k, v in by.items()} == {"sold": 8, "won": 6, "lost": 1, None: 1}
    for c in by["sold"]:  # sold out: entry and exit are their average buy and sell prices
        mine = [t for t in CLOSED["trades"] if t["asset"] == _asset(c.market)]
        buys, sells = [t for t in mine if t["side"] == "BUY"], [t for t in mine if t["side"] == "SELL"]
        assert c.settled is False
        assert c.avg_price == pytest.approx(
            sum(t["price"] * t["size"] for t in buys) / sum(t["size"] for t in buys), abs=1e-4
        )
        assert c.exit_price == pytest.approx(
            sum(t["price"] * t["size"] for t in sells) / sum(t["size"] for t in sells), abs=1e-4
        )
    # Bought, sold out, then bought again and sold at 4¢: Polymarket's avgPrice (51.75¢) is the last lot only.
    twice = next(c for c in by["sold"] if c.market.startswith("0xcddaf8") and c.market.endswith(":1"))
    assert twice.avg_price == pytest.approx((435 * 0.3 + 300 * 0.5) / 735, abs=1e-4)
    assert twice.pnl == -140.67  # Polymarket's realized profit, after fees
    for c in by["won"] + by["lost"]:  # held until it settled: paid 1 or 0
        assert c.settled is True and c.exit_price == pytest.approx(
            1.0 if c.result == "won" else 0.0, abs=0.001
        )
        assert (c.pnl or 0) > 0 if c.result == "won" else (c.pnl or 0) < 0
    # Held, but its market hadn't settled: how it ended, and at what price, can't be said yet.
    assert by[None][0].settled is None and by[None][0].exit_price is None and by[None][0].avg_price == 0.4
    c = d.closed[0]
    assert c.url == "https://polymarket.com/event/btc-updown-5m-1791853200" and c.outcome == "Down"
    assert c.market.endswith(":1") and c.size > 0 and c.avg_price is not None and c.pnl is not None
    assert json.dumps(d.to_dict()["closed"][0])  # plain JSON for Signals


def _asset(market: str) -> str:
    cid, outcome = market.split(":")
    return next(
        r["asset"] for r in CLOSED["closed"] if r["conditionId"] == cid and str(r["outcomeIndex"]) == outcome
    )


def test_closed_positions_cap_at_the_number_asked_and_page_through_polymarket() -> None:
    many = [CLOSED["closed"][i % 14] | {"timestamp": 2_000_000_000 - i} for i in range(80)]
    w = poly_trader(closed=many)
    assert len(w.trader("polymarket", ME).closed or []) == 50  # the default
    assert len(w.trader("polymarket", ME, closed=70).closed or []) == 70  # two pages of 50
    assert w.trader("polymarket", ME, closed=0).closed is None  # not asked for


def test_unreadable_sells_leave_the_exit_unknown_and_unreadable_positions_leave_none() -> None:
    w = poly_trader()
    w._poly.trades_in = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        VenueError("venue_unavailable", "down", venue="polymarket")
    )
    d = w.trader("polymarket", ME)
    assert d.closed is not None and len(d.closed) == 14
    assert all(c.result is None and c.settled is None for c in d.closed)  # unknown, never guessed
    assert all(c.avg_price is None and c.exit_price is None for c in d.closed)
    assert all(c.pnl is not None and c.size > 0 for c in d.closed)  # Polymarket's own figures still stand

    w = poly_trader()
    w._poly.closed_positions = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        VenueError("venue_unavailable", "down", venue="polymarket")
    )
    assert w.trader("polymarket", ME).closed is None


# ---- every position a wallet is done with (Polymarket) ----

# user-stats as Polymarket gives them: ``volume`` counts contracts, ``volume_usdc`` is dollars, ``trade_count`` is
# fills, and the top-level ``trades`` is how many markets they traded (79 here, against 7 fills).
STATS = {"data": {"trades": 79, "biggest_win": 100.0, "all_time_pnl": {
    "timestamp": 1760500000, "realized_pnl": 70.0, "unrealized_pnl": 5.0, "economic_pnl": 75.0, "wallet_income": 0.0,
    "volume": 400.0, "volume_usdc": 210.0, "trade_count": 7, "fees_paid": -1.0}}}  # fmt: skip


def paged(rows: list[dict[str, Any]]) -> Any:
    def page(r: httpx.Request) -> Any:
        start = int(r.url.params.get("offset", 0))
        return rows[start : start + int(r.url.params["limit"])]

    return page


def done_wallet(closed: list[dict[str, Any]], held: list[dict[str, Any]], stats: Any = STATS) -> Whales:
    return whales({(DATA, "/v2/user-stats"): stats, (DATA, "/closed-positions"): paged(closed),
                   (DATA, "/positions"): paged(held), (GAMMA, "/public-profile"): {"proxyWallet": ME},
                   (DATA, "/activity"): []})  # fmt: skip


WON = [{"conditionId": "0xw1", "outcomeIndex": 0, "title": "Won 1", "realizedPnl": 100.0, "timestamp": 1760400000},
       {"conditionId": "0xw2", "outcomeIndex": 1, "title": "Won 2", "realizedPnl": 20.0, "timestamp": 1760300000}]  # fmt: skip
# Settled against them, never redeemed (worth nothing): in their positions, not their closed ones. And a position
# still open that they sold part of (its realized part counts toward their realized profit, it isn't done).
HELD = [
    {"conditionId": "0xl", "outcomeIndex": 0, "title": "Lost", "redeemable": True, "curPrice": 0, "cashPnl": -60.0,
     "realizedPnl": -1.0, "endDate": "2026-10-01"},
    {"conditionId": "0xo", "outcomeIndex": 1, "title": "Open", "redeemable": False, "curPrice": 0.4, "cashPnl": 5.0,
     "realizedPnl": 11.0},
]  # fmt: skip


def test_completed_positions_count_the_losers_never_redeemed_and_add_up_to_realized_profit() -> None:
    r = done_wallet(WON, HELD).completed_positions(ME)
    # closed ones newest first, then the ones never redeemed
    assert [p.market for p in r.positions] == ["0xw1:0", "0xw2:1", "0xl:0"]
    lost = r.positions[2]
    assert (lost.redeemed, lost.pnl, lost.closed_at, lost.end_date) == (False, -61.0, None, "2026-10-01")
    assert r.made_money == 2 and len(r.positions) == 3  # closed positions alone would say 2 of 2
    assert (r.complete, r.realized_pnl, r.accounted_pnl, r.reconciles) == (True, 70.0, 70.0, True)
    assert r.positions[0].closed_at == datetime(2025, 10, 14, 0, 0, tzinfo=UTC)
    d = r.to_dict()
    assert (d["made_money"], d["count"], d["reconciles"]) == (2, 3, True) and json.dumps(d)


def test_a_record_cut_at_the_read_limit_or_not_adding_up_is_never_said_to_reconcile() -> None:
    many = [WON[0] | {"conditionId": f"0x{i}", "realizedPnl": 0.0} for i in range(60)]
    stats = {"data": {"all_time_pnl": {"realized_pnl": 0.0}}}
    cut = done_wallet(many, [], stats).completed_positions(ME, max_pages=1)
    # it adds up, but there may be more
    assert len(cut.positions) == 50 and not cut.complete and not cut.reconciles
    whole = done_wallet(many, [], stats).completed_positions(ME)
    assert len(whole.positions) == 60 and whole.complete and whole.reconciles
    off = done_wallet(WON, HELD, {"data": {"all_time_pnl": {"realized_pnl": 500.0}}}).completed_positions(ME)
    assert off.complete and not off.reconciles  # some history isn't in what was read
    unknown = done_wallet(WON, HELD, {"data": {}}).completed_positions(ME)
    assert unknown.realized_pnl is None and not unknown.reconciles


def test_a_row_polymarket_repeats_on_the_next_page_counts_once_and_the_record_isnt_complete() -> None:
    rows = [WON[0] | {"conditionId": f"0x{i}", "realizedPnl": 1.0} for i in range(60)]
    stats = {"data": {"all_time_pnl": {"realized_pnl": 60.0}}}

    def slipped(r: httpx.Request) -> Any:
        # the second page starts with the first page's last row again, and so skips row 50 (seen live)
        return rows[:50] if int(r.url.params.get("offset", 0)) == 0 else [rows[49], *rows[51:]]

    w = whales({(DATA, "/v2/user-stats"): stats, (DATA, "/closed-positions"): slipped,
                (DATA, "/positions"): paged([])})  # fmt: skip
    r = w.completed_positions(ME)
    assert len(r.positions) == len({p.market for p in r.positions}) == 59  # once each, row 50 never seen
    assert r.accounted_pnl == 59.0 and not r.complete and not r.reconciles
    assert done_wallet(rows, [], stats).completed_positions(ME).reconciles  # the same rows, paged cleanly


def test_a_closed_position_they_still_hold_a_remainder_of_counts_once_as_closed() -> None:
    # Seen live: listed as closed with its whole profit (0.45), and still held, 0.79 shares never redeemed, whose
    # value and fees (0.46 - 0.01) are that same profit again.
    closed = [*WON, {"conditionId": "0xd", "outcomeIndex": 0, "title": "Dust", "realizedPnl": 0.45,
                     "timestamp": 1760200000}]  # fmt: skip
    held = [*HELD, {"conditionId": "0xd", "outcomeIndex": 0, "title": "Dust", "redeemable": True, "size": 0.79,
                    "curPrice": 1, "cashPnl": 0.46, "realizedPnl": -0.01}]  # fmt: skip
    stats = {"data": {"all_time_pnl": {"realized_pnl": 70.45}}}
    r = done_wallet(closed, held, stats).completed_positions(ME)
    assert [p.market for p in r.positions] == ["0xw1:0", "0xw2:1", "0xd:0", "0xl:0"]
    assert r.positions[2].redeemed and r.positions[2].pnl == 0.45
    assert (r.accounted_pnl, r.complete, r.reconciles, r.made_money) == (70.45, True, True, 3)


def parlays(realized: float, parlay: float) -> dict[str, Any]:
    return {"data": {"all_time_pnl": {"realized_pnl": realized, "realized_market_pnl": realized - parlay,
                                      "realized_combo_pnl": parlay}}}  # fmt: skip


def test_parlays_are_left_out_and_said_to_be_while_the_market_positions_reconcile() -> None:
    # Polymarket lists parlays in neither list: the positions are checked against the profit without them
    small = done_wallet(WON, HELD, parlays(70.0 - 97.66, -97.66)).completed_positions(
        ME
    )  # Kch-Temp's -$97.66
    assert small.complete and small.reconciles and small.parlays_excluded
    assert (small.market_pnl, small.accounted_pnl, small.parlay_pnl) == (70.0, 70.0, -97.66)
    assert small.summary == "Made money on 2 of 3 positions · parlays not included"
    # most of their profit from parlays: their market positions still reconcile, flagged
    big = done_wallet(WON, HELD, parlays(70.0 + 50_000, 50_000)).completed_positions(ME)
    assert big.reconciles and big.parlays_excluded and len(big.positions) == 3
    d = big.to_dict()
    assert (d["parlays_excluded"], d["market_pnl"], d["parlay_pnl"]) == (True, 70.0, 50_000) and json.dumps(d)
    # positions missing from the market part still don't reconcile, parlays or not
    assert not done_wallet(WON, HELD, parlays(500.0 + 5, 5)).completed_positions(ME).reconciles
    # no parlays: no flag, no label
    plain = done_wallet(WON, HELD, parlays(70.0, 0.0)).completed_positions(ME)
    assert (
        plain.reconciles and not plain.parlays_excluded and plain.summary == "Made money on 2 of 3 positions"
    )


def test_a_wallet_that_trades_only_parlays_says_there_are_no_positions_to_count() -> None:
    only = done_wallet([], [], parlays(2_286_877.56, 2_286_877.56)).completed_positions(ME)  # MumiaTriste
    assert (len(only.positions), only.complete, only.reconciles, only.parlays_excluded) == (
        0,
        True,
        True,
        True,
    )
    assert only.market_pnl == 0 and only.made_money == 0
    assert only.summary == "They trade only parlays, which Polymarket doesn't list: no positions to count."
    assert only.to_dict()["summary"] == only.summary
    # and the other reasons a record can't be given say so
    assert "couldn't all be read" in done_wallet(WON, HELD).completed_positions(ME, max_pages=0).summary
    off = done_wallet(WON, HELD, parlays(500.0, 0.0)).completed_positions(ME).summary
    assert off == (
        "Their positions don't add up to Polymarket's total right now; it updates every few hours, "
        "so their record can't be given."
    )
    unknown = done_wallet(WON, HELD, {"data": {}}).completed_positions(ME)  # no profit figure: not "yet"
    assert (
        unknown.summary
        == "Polymarket gave no profit figure for them, so their record can't be checked or given."
    )


def test_a_record_off_by_more_than_a_fifth_of_a_percent_or_ten_cents_doesnt_reconcile() -> None:
    def record(realized: float, closed: list[dict[str, Any]] = WON, held: list[dict[str, Any]] = HELD) -> Any:
        return done_wallet(
            closed, held, {"data": {"all_time_pnl": {"realized_pnl": realized}}}
        ).completed_positions(ME)

    assert record(70.1).reconciles  # 0.14% off: within 0.2%
    assert not record(70.7).reconciles  # 1% off: a position can be missing (2% passed before)
    # a small wallet: 10 cents, not a dollar (a 90-cent position missing passed before)
    tiny = [WON[0] | {"realizedPnl": 0.5}]
    assert record(0.55, tiny, []).reconciles and not record(1.4, tiny, []).reconciles


def test_positions_held_are_read_before_closed_ones_so_one_closing_between_is_counted_once() -> None:
    order: list[str] = []
    gone = {"conditionId": "0xs", "outcomeIndex": 0, "title": "Sold between", "realizedPnl": 30.0}

    def held(r: httpx.Request) -> Any:
        order.append("held")
        return [*HELD, gone | {"redeemable": False, "cashPnl": 4.0, "realizedPnl": 0.0}]  # held when read

    def closed(r: httpx.Request) -> Any:
        order.append("closed")
        return [*WON, gone | {"timestamp": 1760450000}]  # and sold by the time closed ones were read

    w = whales({(DATA, "/v2/user-stats"): {"data": {"all_time_pnl": {"realized_pnl": 100.0}}},
                (DATA, "/closed-positions"): closed, (DATA, "/positions"): held})  # fmt: skip
    r = w.completed_positions(ME)
    assert order == ["held", "closed"]
    assert [p.market for p in r.positions].count("0xs:0") == 1 and r.reconciles  # 70 + 30, once


def test_a_read_polymarkets_firewall_turns_away_is_tried_once_more(monkeypatch: pytest.MonkeyPatch) -> None:
    from uselayer import whale_scores as ws

    monkeypatch.setattr(ws, "FIREWALL_RETRY_S", 0.0)
    page = "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head></html>"
    routes = {
        (DATA, "/v2/user-stats"): STATS,
        (DATA, "/closed-positions"): paged(WON),
        (DATA, "/positions"): paged(HELD),
    }

    def firewalled(refusals: dict[str, int]) -> Whales:
        def handler(request: httpx.Request) -> httpx.Response:
            if refusals.get(request.url.path, 0):
                refusals[request.url.path] -= 1
                return httpx.Response(403, text=page, headers={"content-type": "text/html"})
            body = routes[(request.url.host, request.url.path)]
            return httpx.Response(200, json=body(request) if callable(body) else body)

        http = Http(transport=httpx.MockTransport(handler), sleep=lambda s: None)
        return Whales(http, LayerApi(None, http))

    once = firewalled({"/v2/user-stats": 1, "/positions": 1, "/closed-positions": 1}).completed_positions(ME)
    assert len(once.positions) == 3 and once.reconciles  # each read refused once, then answered
    with pytest.raises(VenueError):  # refused twice: raised, never a short page
        firewalled({"/closed-positions": 2}).completed_positions(ME)


def test_a_polymarket_traders_volume_is_dollars_and_trades_are_fills() -> None:
    d = done_wallet([], HELD).trader("polymarket", ME, closed=0)
    s = d.stats
    assert (s["volume_usd"], s["volume_contracts"], s["trades"]) == (210.0, 400.0, 7)
    assert s["markets_traded"] == 79  # user-stats' own ``trades``: the "Predictions" on their profile
    assert s["pnl_as_of"] == "2025-10-15T03:46:40+00:00"
    assert (d.trader.volume, d.trader.volume_unit) == (210.0, "usd")


# ---- open positions on their own ----


def _poly_pos(cid: str, outcome: int, **kw: Any) -> dict[str, Any]:
    return {"conditionId": cid, "outcomeIndex": outcome, "size": 10, "curPrice": 0.5} | kw


def test_positions_are_the_trader_pages_open_bets_in_one_request() -> None:
    rows = [
        _poly_pos("0xa", 0, avgPrice=0.4, title="A"),
        _poly_pos("0xb", 1, redeemable=True),  # settled, waiting to be redeemed
        _poly_pos("0xc", 0, curPrice=1),  # settled, not redeemed yet
        _poly_pos("0xd", 1, curPrice=0),
    ]
    seen: list[str] = []

    def positions(r: httpx.Request) -> Any:
        seen.append(r.url.path)
        cid = r.url.params.get("market")
        return [p for p in rows if p["conditionId"] == cid] if cid else rows

    w = whales({(DATA, "/positions"): positions})
    got = w.positions("polymarket", ME)
    assert [p.market for p in got] == ["0xa:0"] and got[0].avg_price == pytest.approx(0.4)
    assert seen == ["/positions"]  # no profile, no stats

    page = whales(
        {
            (GAMMA, "/public-profile"): {"proxyWallet": ME},
            (DATA, "/v2/user-stats"): {"data": {}},
            (DATA, "/positions"): positions,
            (DATA, "/activity"): [],
            (DATA, "/closed-positions"): [],
        }
    ).trader("polymarket", ME, closed=0)
    assert page.positions == got


def test_positions_narrow_to_one_market_or_one_outcome() -> None:
    rows = [_poly_pos("0xa", 0), _poly_pos("0xa", 1), _poly_pos("0xb", 0)]
    w = whales(
        {(DATA, "/positions"): lambda r: [p for p in rows if p["conditionId"] == r.url.params.get("market")]}
    )
    assert [p.market for p in w.positions("polymarket", ME, market="0xa")] == ["0xa:0", "0xa:1"]
    assert [p.market for p in w.positions("polymarket", ME, market="0xa:1")] == ["0xa:1"]

    k = whales(
        {
            (KALSHI, "/v1/social/profile/holdings"): {
                "holdings": [
                    {"event_ticker": "KX", "market_holdings": [
                        {"market_ticker": "KX-A", "signed_open_position_fp": "-12.00", "pnl": 25_000},
                        {"market_ticker": "KX-B", "signed_open_position_fp": "0.00"},
                        {"market_ticker": "KX-C", "signed_open_position_fp": "3.00"},
                    ]}
                ],
                "visibility_state": "visible",
            },
        }
    )  # fmt: skip
    every = k.positions("kalshi", "amy")
    assert [(p.market, p.size, p.outcome) for p in every] == [("KX-A", 12, "NO"), ("KX-C", 3, "YES")]
    assert every[0].pnl == pytest.approx(2.5)
    assert [p.market for p in k.positions("kalshi", "amy", market="KX-C")] == ["KX-C"]
    with pytest.raises(ValueError):
        k.positions("polymarket_us", "amy")  # type: ignore[arg-type]


# ---- links ----


def _k(at_s: float, long_yes: bool = True, market: str = "KX-A") -> WhaleTrade:
    return WhaleTrade(
        "kalshi", "amy", "amy", market, "yes" if long_yes else "no", "buy", 0.5, 10, 5,
        T0 + timedelta(seconds=at_s), "t",
    )  # fmt: skip


def test_co_trades_count_same_direction_inside_the_window_only() -> None:
    keyed = {("KX-A", True): [T0], ("KX-B", True): [T0]}
    same, opposite = Whales._co_trades(keyed, {}, [_k(60), _k(5000, market="KX-B")], 300)
    assert same == {"KX-A"} and opposite == set()


def test_co_trades_ignore_two_sided_and_always_on_traders() -> None:
    keyed = {("KX-A", True): [T0], ("KX-B", True): [T0]}
    market_maker = [_k(10), _k(20, long_yes=False)]  # both ways on KX-A
    always_on = [_k(10, market="KX-B")] + [_k(s, market="KX-B") for s in (-1200, -600, 600, 1200)]
    same, _ = Whales._co_trades(keyed, {}, market_maker + always_on, 300)
    assert same == set()


def test_co_trades_map_polymarket_outcomes_onto_the_kalshi_ticker() -> None:
    keyed = {("KX-ATL", True): [T0]}
    twins = {"0xcid": ("KX-ATL", 1)}  # outcome 1 pays like YES on KX-ATL
    buy_atl = _polymarket_trade(poly_row(timestamp=int(T0.timestamp()) + 30))
    buy_lad = _polymarket_trade(poly_row(timestamp=int(T0.timestamp()) + 30, outcomeIndex=0))
    assert Whales._co_trades(keyed, twins, [buy_atl], 300) == ({"KX-ATL"}, set())
    assert Whales._co_trades(keyed, twins, [buy_lad], 300) == (set(), {"KX-ATL"})


def test_a_name_counts_only_when_that_account_trades() -> None:
    def routes(volume: float) -> dict[tuple[str, str], Any]:
        return {
            (KALSHI, "/v1/social/leaderboard"): {"rank_list": []},
            (DATA, "/v1/leaderboard"): [],
            (GAMMA, "/public-search"): {"profiles": [{"name": "BlackBriar", "proxyWallet": "0xbb"}]},
            # ``volume`` counts contracts, ``volume_usdc`` is the dollars
            (DATA, "/v2/user-stats"): {
                "data": {"all_time_pnl": {"volume": 2 * volume, "volume_usdc": volume, "economic_pnl": 5.0}}
            },
        }

    me = Trader(venue="kalshi", id="Blackbriar", name="Blackbriar", volume_unit="contracts")
    assert whales(routes(50)).links(me) == []
    links = whales(routes(2_000_000)).links(me)
    assert len(links) == 1 and links[0].tier == "possible" and links[0].trader.id == "0xbb"
    assert "$2,000,000 traded" in links[0].evidence[0].detail  # dollars, never the contracts
    assert (links[0].trader.volume, links[0].trader.volume_unit) == (2_000_000, "usd")


# ---- copy trading (paper, against the fake Polymarket US book) ----


def test_copier_copies_new_trades_onto_the_twin_and_explains_skips(
    make_client: Any, venue: FakeVenue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue.layer_answers["/v0/match"] = {"matched_market": {"venue": "polymarket_us", "market_id": "mkt-a"}}
    c = make_client(layer_key="lyr_test")
    feed: list[WhaleTrade] = []
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))
    monkeypatch.setattr(c.whales, "_position_now", lambda t: 0.0)  # she sells out

    amy = Trader(venue="kalshi", id="amy", name="amy", volume_unit="contracts")
    cp = c.whales.follow(amy, size=5, venue="polymarket_us", max_slippage=0.03)
    feed.append(_k(-3600))  # there before we started: never copied
    assert cp.poll() == []

    def trade(tid: str, age_s: float, **kw: Any) -> WhaleTrade:
        base = _k(0)
        return WhaleTrade(
            **{
                **base.__dict__,
                "trade_id": tid,
                "price": 0.40,
                "at": clock.now - timedelta(seconds=age_s),
                **kw,
            }
        )

    feed += [trade("new", 5), trade("old", 900), trade("sold", 5, action="sell")]
    events = {e.source.trade_id: e for e in cp.poll()}
    assert events["new"].status == "copied" and events["new"].market == "mkt-a"
    assert events["new"].filled == 5 and events["new"].avg_price == pytest.approx(0.42)
    assert events["new"].price == pytest.approx(0.43) and events["new"].simulated
    assert events["old"].status == "skipped" and "too late" in events["old"].reason
    assert events["sold"].status == "copied" and events["sold"].action == "sell"  # we hold 5 now
    assert (
        events["sold"].filled == 5 and events["sold"].share == 1 and "they sold out" in events["sold"].reason
    )
    assert cp.poll() == []  # nothing new


def test_their_sells_are_mirrored_in_proportion_even_when_both_arrive_in_one_poll(
    make_client: Any, venue: FakeVenue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue.layer_answers["/v0/match"] = {"matched_market": {"venue": "polymarket_us", "market_id": "mkt-a"}}
    c = make_client(layer_key="lyr_test")
    feed: list[WhaleTrade] = []
    held = {"amy": 10.0}  # her YES contracts on KX-A, as Kalshi reports them now
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))
    monkeypatch.setattr(c.whales, "_position_now", lambda t: held["amy"])
    amy = Trader(venue="kalshi", id="amy", name="amy", volume_unit="contracts")
    cp = c.whales.follow(amy, size=5, venue="polymarket_us", min_usd=100)
    assert cp.poll() == []

    def trade(tid: str, s: float, action: str, size: float) -> WhaleTrade:
        return WhaleTrade(
            **{**_k(0).__dict__, "trade_id": tid, "price": 0.40, "size": size, "usd": 0.4 * size,
               "action": action, "at": clock.now - timedelta(seconds=s)}
        )  # fmt: skip

    feed.append(trade("buy", 30, "buy", 250))
    (bought,) = cp.poll()
    assert bought.status == "copied" and bought.filled == 5

    # She sells 40% of her 10, then the other 6: Kalshi already shows 0 when we see both.
    held["amy"] = 0.0
    feed += [trade("trim", 20, "sell", 4), trade("out", 10, "sell", 6)]
    ev = {e.source.trade_id: e for e in cp.poll()}
    trim, out = ev["trim"], ev["out"]
    assert (
        trim.status == "copied" and trim.share == pytest.approx(0.4) and trim.filled == 2
    )  # $1.60: no minimum
    assert "40% of yours as they sold 40%" in trim.reason
    assert out.status == "copied" and out.share == 1 and out.filled == 3
    assert sum(p.contracts for p in c.positions()) == 0


def test_polymarket_copies_sell_their_share_and_skip_one_too_small_to_copy(
    make_client: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    market: dict[str, Any] = {
        "conditionId": "0xcid",
        "outcomes": '["Dodgers", "Braves"]',
        "outcomePrices": '["0.575", "0.425"]',
        "bestBid": 0.57,  # Braves (outcome 1) is bid 0.42, offered at 0.43
        "bestAsk": 0.58,
        "closed": False,
        "acceptingOrders": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
    }
    monkeypatch.setattr(c.whales._poly, "markets", lambda ids, closed: [market] if not closed else [])
    theirs = {"size": 100.0}
    seen: list[str | None] = []

    def positions(wallet: str, limit: int = 50, market: str | None = None) -> list[dict[str, Any]]:
        seen.append(market)
        return [
            {"conditionId": "0xcid", "outcomeIndex": 0, "size": 999},  # the other outcome: not theirs to sell
            {"conditionId": "0xcid", "outcomeIndex": 1, "size": theirs["size"]},
        ]

    monkeypatch.setattr(c.whales._poly, "positions", positions)
    feed: list[WhaleTrade] = []
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))
    whale = Trader(venue="polymarket", id="0xabc", name="whale", volume_unit="usd")
    cp = c.whales.follow(whale, size=5, venue="polymarket", max_slippage=0.03)
    assert cp.poll() == []

    now = int(clock.now.timestamp())
    feed.append(_polymarket_trade(poly_row(timestamp=now, transactionHash="0xa", price=0.41)))
    (bought,) = cp.poll()
    assert bought.status == "copied" and bought.filled == 5

    theirs["size"] = 60.0  # they sold 40 of their 100
    feed.append(
        _polymarket_trade(poly_row(timestamp=now, transactionHash="0xb", side="SELL", size=40, price=0.43))
    )
    (sold,) = cp.poll()
    assert (sold.status, sold.action, sold.share, sold.filled) == ("copied", "sell", pytest.approx(0.4), 2)
    assert sold.avg_price == pytest.approx(0.42) and seen[-1] == "0xcid"  # sold at the bid, their market only

    theirs["size"] = 57.0  # 3 of 60 is 5%: of our 3, that rounds to nothing
    feed.append(
        _polymarket_trade(poly_row(timestamp=now, transactionHash="0xc", side="SELL", size=3, price=0.43))
    )
    (tiny,) = cp.poll()
    assert (
        tiny.status == "skipped"
        and "5% of their position" in tiny.reason
        and tiny.share == pytest.approx(0.05)
    )
    assert sum(p.contracts for p in c.positions()) == 3


def test_their_position_is_read_from_each_venue() -> None:
    w = whales(
        {
            (KALSHI, "/v1/social/profile/holdings"): {
                "holdings": [
                    {"event_ticker": "KX", "market_holdings": [
                        {"market_ticker": "KX-B", "signed_open_position_fp": "7.00"},
                        {"market_ticker": "KX-A", "signed_open_position_fp": "-12.00"},
                    ]}
                ],
                "visibility_state": "visible",
            },
            (DATA, "/positions"): lambda r: [{"conditionId": "0xcid", "outcomeIndex": 1, "asset": "tok1", "size": 30}]
            if r.url.params.get("market") == "0xcid" else [],
        }
    )  # fmt: skip
    assert w._position_now(_k(0)) == -12  # Kalshi nets it: 12 NO
    assert w._position_now(_k(0, market="KX-C")) == 0
    sell = _polymarket_trade(poly_row(side="SELL"))
    assert w._position_now(sell) == 30
    assert w._position_now(WhaleTrade(**{**sell.__dict__, "token": "tok0"})) == 0  # a different outcome token


def test_copying_buys_only_holds_each_copy_and_reports_it_open_then_won(
    make_client: Any, venue: FakeVenue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue.layer_answers["/v0/match"] = {"matched_market": {"venue": "polymarket_us", "market_id": "mkt-a"}}
    c = make_client(layer_key="lyr_test")
    feed: list[WhaleTrade] = []
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))
    cp = c.whales.follow(
        Trader(venue="kalshi", id="amy", name="amy", volume_unit="contracts"),
        size=5,
        venue="polymarket_us",
        copy_sells=False,
    )
    assert cp.poll() == []
    base = _k(0)
    feed.append(WhaleTrade(**{**base.__dict__, "trade_id": "b", "price": 0.40, "at": clock.now}))
    feed.append(
        WhaleTrade(**{**base.__dict__, "trade_id": "s", "price": 0.40, "at": clock.now, "action": "sell"})
    )
    ev = {e.source.trade_id: e for e in cp.poll()}
    assert ev["s"].status == "skipped" and "buys only" in ev["s"].reason
    oid = ev["b"].order_id
    assert oid is not None

    r = c.whales.copy_results([oid])[oid]
    assert (r.status, r.contracts, r.avg_price, r.mark) == (
        "open",
        5,
        pytest.approx(0.42),
        pytest.approx(0.40),
    )
    assert r.pnl == pytest.approx(5 * 0.40 - 5 * 0.42 - r.fees) and r.simulated

    c.settle([Resolution(venue="polymarket_us", market="mkt-a", outcome="yes", as_of=clock.now)])
    r = c.whales.copy_results([oid, "never-sent"])
    assert r[oid].status == "won" and r[oid].payout == 1
    assert r[oid].pnl == pytest.approx(5 * 1.0 - 5 * 0.42 - r[oid].fees)
    assert r["never-sent"].status == "unfilled" and r["never-sent"].pnl == 0


# ---- copy trading on Polymarket international (paper, priced from Gamma) ----


def test_copying_onto_polymarket_fills_at_the_outcome_ask_and_pays_out_when_polymarket_resolves(
    make_client: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    market: dict[str, Any] = {
        "conditionId": "0xcid",
        "outcomes": '["Dodgers", "Braves"]',
        "outcomePrices": '["0.575", "0.425"]',
        "bestBid": 0.57,  # Gamma quotes the first outcome: Braves (outcome 1) is bid 0.42, offered at 0.43
        "bestAsk": 0.58,
        "closed": False,
        "acceptingOrders": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
    }
    monkeypatch.setattr(
        c.whales._poly,
        "markets",
        lambda ids, closed: [market] if "0xcid" in ids and market["closed"] == closed else [],
    )
    feed: list[WhaleTrade] = []
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))
    whale = Trader(venue="polymarket", id="0xabc", name="whale", volume_unit="usd")
    cp = c.whales.follow(whale, size=5, venue="polymarket", max_slippage=0.03, copy_sells=False)
    assert cp.poll() == []

    now = int(clock.now.timestamp())
    feed += [
        _polymarket_trade(poly_row(timestamp=now, transactionHash="0xa", price=0.41)),  # can pay up to 0.44
        _polymarket_trade(
            poly_row(timestamp=now, transactionHash="0xb", price=0.30)
        ),  # 0.33 is under the ask
    ]
    ev = {e.source.trade_id: e for e in cp.poll()}
    a, b = ev["0xa"], ev["0xb"]
    fee = 0.05 * 0.43 * 0.57 * 5
    assert (a.status, a.venue, a.market, a.side) == ("copied", "polymarket", "0xcid:1", "yes")
    assert (
        a.filled == 5 and a.avg_price == pytest.approx(0.43) and a.fees == pytest.approx(fee) and a.simulated
    )
    assert b.status == "skipped" and "Nothing to buy at 0.33" in b.reason
    assert a.order_id is not None

    r = c.whales.copy_results([a.order_id])[a.order_id]
    assert (r.status, r.contracts, r.avg_price, r.mark) == (
        "open",
        5,
        pytest.approx(0.43),
        pytest.approx(0.42),
    )
    assert r.pnl == pytest.approx(5 * 0.42 - 5 * 0.43 - fee, abs=1e-4)

    market |= {
        "closed": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "resolved",
        "outcomePrices": '["0", "1"]',
    }
    r = c.whales.copy_results([a.order_id])[a.order_id]
    assert (r.status, r.payout) == ("won", 1)
    assert r.pnl == pytest.approx(5 * 1.0 - 5 * 0.43 - fee, abs=1e-4)
    assert c.pnl().net == pytest.approx(r.pnl, abs=1e-4)  # the paper account counts it too

    feed.append(_polymarket_trade(poly_row(timestamp=now, transactionHash="0xc")))
    (late,) = cp.poll()
    assert late.status == "skipped" and "isn't taking orders" in late.reason


def test_polymarket_copies_follow_polymarket_traders_only(
    make_client: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    feed: list[WhaleTrade] = []
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))
    amy = Trader(venue="kalshi", id="amy", name="amy", volume_unit="contracts")
    cp = c.whales.follow(amy, size=5, venue="polymarket")
    assert cp.poll() == []
    feed.append(WhaleTrade(**{**_k(0).__dict__, "trade_id": "k1", "at": clock.now}))
    (e,) = cp.poll()
    assert e.status == "skipped" and "Polymarket traders only" in e.reason


def test_preview_then_copy_one_trade_now_however_old(
    make_client: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    market: dict[str, Any] = {
        "conditionId": "0xcid",
        "bestBid": 0.47,  # Braves (outcome 1): bid 0.52, offered at 0.53
        "bestAsk": 0.48,
        "closed": False,
        "acceptingOrders": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1},
    }
    monkeypatch.setattr(c.whales._poly, "markets", lambda ids, closed: [market] if not closed else [])
    hour_ago = int(clock.now.timestamp()) - 3600  # a follower would never copy this one: it's an hour old
    theirs = _polymarket_trade(poly_row(timestamp=hour_ago, price=0.52))

    p = c.whales.preview_copy(theirs, size=5)
    fee = 0.05 * 0.53 * 0.47 * 5
    assert p.ok and (p.price, p.limit, p.contracts) == (pytest.approx(0.53), pytest.approx(0.55), 5)
    assert (
        p.cost == pytest.approx(2.65) and p.fee == pytest.approx(fee) and p.total == pytest.approx(2.65 + fee)
    )
    assert p.payout == 5 and p.profit_if_win == pytest.approx(5 - 2.65 - fee)
    assert c.fills() == []  # a preview sends nothing

    e = c.whales.copy_trade(theirs, size=5)
    assert e.status == "copied" and e.avg_price == pytest.approx(0.53) and e.order_id
    assert c.whales.copy_results([e.order_id])[e.order_id].status == "open"

    pricey = _polymarket_trade(poly_row(timestamp=hour_ago, price=0.45))  # 0.53 is 8¢ above what they paid
    assert not c.whales.preview_copy(pricey).ok
    assert c.whales.copy_trade(pricey).status == "skipped"
    with pytest.raises(ValueError, match="Only a buy"):
        c.whales.preview_copy(_polymarket_trade(poly_row(side="SELL")))


def test_copy_one_trade_by_dollars_fee_included(
    make_client: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    market: dict[str, Any] = {
        "conditionId": "0xcid",
        "bestBid": 0.47,  # Braves (outcome 1) offered at 0.53
        "bestAsk": 0.48,
        "closed": False,
        "acceptingOrders": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1},
    }
    monkeypatch.setattr(c.whales._poly, "markets", lambda ids, closed: [market] if not closed else [])
    theirs = _polymarket_trade(poly_row(timestamp=int(clock.now.timestamp()), price=0.52))
    each = 0.53 + 0.05 * 0.53 * 0.47  # a contract plus its fee

    p = c.whales.preview_copy(theirs, spend=50)
    assert p.ok and p.spend == 50 and p.contracts == 92  # the most whole contracts that fit: 50 / 0.5425
    assert p.total == pytest.approx(92 * each) and p.total <= 50
    assert p.payout == 92 and p.profit_if_win == pytest.approx(92 - 92 * each)

    small = c.whales.preview_copy(theirs, spend=2)  # 3 contracts, under Polymarket's 5
    assert not small.ok and "$2.00 doesn't buy Polymarket's smallest order here: 5 contracts" in small.reason

    e = c.whales.copy_trade(theirs, spend=50)
    assert e.status == "copied" and e.filled == 92 and e.avg_price == pytest.approx(0.53)
    with pytest.raises(ValueError, match="not both"):
        c.whales.preview_copy(theirs, size=5, spend=50)
    with pytest.raises(ValueError, match="more than 0"):
        c.whales.copy_trade(theirs, spend=0)


def test_a_copy_is_priced_by_walking_the_order_book_for_its_size(
    make_client: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    market: dict[str, Any] = {
        "conditionId": "0xcid",
        "clobTokenIds": '["tok-dodgers", "tok-braves"]',
        "bestBid": 0.47,  # Gamma says Braves (outcome 1) is offered at 0.53
        "bestAsk": 0.48,
        "closed": False,
        "acceptingOrders": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1},
    }
    monkeypatch.setattr(c.whales._poly, "markets", lambda ids, closed: [market] if not closed else [])
    book = {"tok-braves": [(0.53, 20.0), (0.54, 30.0), (0.60, 1000.0)]}  # 0.60 is past their 0.52 + 3¢
    asked: list[str] = []

    def asks(token: str) -> list[tuple[float, float]]:
        asked.append(token)
        return book[token]

    monkeypatch.setattr(c.whales._book, "asks", asks)
    theirs = _polymarket_trade(poly_row(timestamp=int(clock.now.timestamp()), price=0.52))

    def fee(p: float) -> float:
        return 0.05 * p * (1 - p)

    p = c.whales.preview_copy(theirs, size=30)  # 20 at 0.53, then 10 at 0.54
    assert asked == ["tok-braves"] and p.ok and p.depth_checked
    assert p.cost == pytest.approx(20 * 0.53 + 10 * 0.54) and p.price == pytest.approx(p.cost / 30)
    assert p.fee == pytest.approx(20 * fee(0.53) + 10 * fee(0.54)) and p.worst_price == 0.54
    room = 20 * (0.53 + fee(0.53)) + 30 * (0.54 + fee(0.54))
    assert p.max_spend == pytest.approx(room, abs=0.01)  # everything at 0.55 or better, fee included

    p = c.whales.preview_copy(theirs, spend=20)
    assert p.ok and p.total is not None and p.total <= 20 and p.contracts == 36
    assert p.total + 0.54 + fee(0.54) > 20  # one more contract wouldn't fit

    big = c.whales.preview_copy(theirs, spend=100)  # only ~$27 sits within 3¢ of their price
    assert not big.ok and big.max_spend == pytest.approx(room, abs=0.01)
    assert f"Only ${room:,.2f} is for sale at 0.55 or better" in big.reason
    assert not c.whales.preview_copy(theirs, size=60).ok  # 50 contracts there
    assert c.whales.copy_trade(theirs, spend=100).status == "skipped"

    e = c.whales.copy_trade(theirs, size=30)  # the paper fill is the walked average
    assert e.status == "copied" and e.avg_price == pytest.approx((20 * 0.53 + 10 * 0.54) / 30)

    junk = _polymarket_trade(poly_row(timestamp=int(clock.now.timestamp()), price=0.52, outcomeIndex=999))
    assert not c.whales.preview_copy(junk, size=5).ok  # no outcome names to check it against
    market["outcomes"] = '["Dodgers", "Braves"]'
    fixed = c.whales.preview_copy(junk, size=5)  # Polymarket's feed sent index 999: the name says Braves
    assert fixed.ok and fixed.market == "0xcid:1" and asked[-1] == "tok-braves"
    assert c.whales.copy_trade(junk, size=5).market == "0xcid:1"  # and the fill settles on Braves
    nameless = _polymarket_trade(poly_row(price=0.52, outcomeIndex=999, outcome="Mets"))
    assert "doesn't say clearly which outcome" in c.whales.preview_copy(nameless, size=5).reason
    by_token = _polymarket_trade(poly_row(price=0.52, outcomeIndex=999, outcome="Mets", asset="tok-braves"))
    assert by_token.token == "tok-braves"  # the token decides, whatever the index and name say
    assert c.whales.preview_copy(by_token, size=5).market == "0xcid:1"
    stray = _polymarket_trade(poly_row(price=0.52, outcomeIndex=1, outcome="Braves", asset="tok-elsewhere"))
    assert "doesn't say clearly which outcome" in c.whales.preview_copy(stray, size=5).reason

    book["tok-braves"] = [(0.58, 100.0)]
    gone = c.whales.preview_copy(theirs, size=5)
    assert not gone.ok and "Nothing to buy at 0.55" in gone.reason and gone.price == 0.58

    def down(token: str) -> list[tuple[float, float]]:
        raise VenueError("venue_unavailable", "down", venue="polymarket")

    monkeypatch.setattr(c.whales._book, "asks", down)
    p = c.whales.preview_copy(theirs, size=5)  # Gamma's best price alone, and it says so
    assert p.ok and p.price == pytest.approx(0.53) and not p.depth_checked


def test_a_copy_preview_gives_the_best_ask_from_the_same_book_read(
    make_client: Any, venue: Any, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client()
    market: dict[str, Any] = {
        "conditionId": "0xcid",
        "clobTokenIds": '["tok-dodgers", "tok-braves"]',
        "bestBid": 0.47,  # Gamma says Braves (outcome 1) is offered at 0.53
        "bestAsk": 0.48,
        "closed": False,
        "acceptingOrders": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1},
    }
    reads = {"markets": 0, "asks": 0}

    def markets(ids: Any, closed: bool) -> list[dict[str, Any]]:
        reads["markets"] += 1
        return [market] if not closed else []

    book = {"tok-braves": [(0.53, 20.0), (0.54, 30.0), (0.60, 1000.0)]}  # 0.60 is past their 0.52 + 3¢

    def asks(token: str) -> list[tuple[float, float]]:
        reads["asks"] += 1
        return book[token]

    monkeypatch.setattr(c.whales._poly, "markets", markets)
    monkeypatch.setattr(c.whales._book, "asks", asks)
    theirs = _polymarket_trade(poly_row(timestamp=int(clock.now.timestamp()), price=0.52))
    sent = len(venue.requests)

    def preview(**kw: Any) -> Any:  # one market read and one book read per preview, and nothing else
        before = dict(reads)
        p = c.whales.preview_copy(theirs, **kw)
        assert reads == {"markets": before["markets"] + 1, "asks": before["asks"] + 1}
        assert len(venue.requests) == sent
        return p

    one = preview(size=10)  # all 10 at 0.53: no slippage
    assert one.ok and one.best_price == 0.53 and one.price == pytest.approx(0.53) and one.worst_price == 0.53
    assert one.to_dict()["best_price"] == 0.53

    many = preview(size=30)  # 20 at 0.53, then 10 at 0.54: the average is above the best ask
    assert many.ok and many.best_price == 0.53 and many.worst_price == 0.54
    assert many.price is not None and many.price - many.best_price == pytest.approx(10 * 0.01 / 30, abs=1e-6)

    thin = preview(spend=100)  # only ~$27 sits within 3¢ of their price
    assert not thin.ok and thin.depth_checked and thin.best_price == 0.53
    small = preview(spend=2)  # under Polymarket's smallest order of 5 contracts
    assert not small.ok and "smallest order" in small.reason and small.best_price == 0.53
    book["tok-braves"] = [(0.58, 100.0)]
    far = preview(size=5)  # nothing at 0.55 or better, but 0.58 is on offer
    assert not far.ok and "Nothing to buy" in far.reason and far.best_price == 0.58

    book["tok-braves"] = []
    empty = preview(size=5)
    assert not empty.ok and empty.depth_checked and empty.best_price is None
    assert empty.to_dict()["best_price"] is None

    def down(token: str) -> list[tuple[float, float]]:
        raise VenueError("venue_unavailable", "down", venue="polymarket")

    monkeypatch.setattr(c.whales._book, "asks", down)
    gamma = c.whales.preview_copy(theirs, size=5)  # Gamma's price alone: the book wasn't read
    assert gamma.ok and not gamma.depth_checked and gamma.best_price is None


def test_a_polymarket_trader_page_gives_trading_profit_apart_from_rewards() -> None:
    # One wallet's all-time figures as Polymarket gave them on 2026-10-13: $668K by its own figure, of which
    # $11K is rewards, rebates and yield, so $657K from trading (realized $740K, open positions −$83K).
    pnl = {"economic_pnl": 668060.346838, "realized_pnl": 740188.689621, "unrealized_pnl": -83318.367283,
           "wallet_income": 11190.0245, "volume": 19506184.37}  # fmt: skip
    w = whales(
        {
            (GAMMA, "/public-profile"): {"proxyWallet": ME, "name": "e46m3"},
            (DATA, "/v2/user-stats"): {"data": {"all_time_pnl": pnl, "biggest_win": 100.0}},
            (DATA, "/positions"): [],
            (DATA, "/activity"): [],
        }
    )
    d = w.trader("polymarket", ME, closed=0)
    assert d.stats["trading_pnl"] == pytest.approx(656870.32, abs=0.01)
    assert d.stats["pnl"] == pytest.approx(668060.35, abs=0.01)  # Polymarket's figure, kept apart
    assert d.stats["income"] == pytest.approx(11190.02, abs=0.01)
    assert d.stats["realized_pnl"] + d.stats["unrealized_pnl"] == pytest.approx(
        d.stats["trading_pnl"], abs=0.01
    )
    assert d.trader.pnl == d.stats["trading_pnl"]  # as on the leaderboard, which counts trading only
    w = whales(
        {
            (GAMMA, "/public-profile"): {"proxyWallet": ME},
            (DATA, "/v2/user-stats"): {"data": {"all_time_pnl": {"economic_pnl": 5.0}}},
            (DATA, "/positions"): [],
            (DATA, "/activity"): [],
        }
    )
    d = w.trader("polymarket", ME, closed=0)
    assert (
        d.stats["trading_pnl"] is None and d.trader.pnl is None
    )  # unknown, never Polymarket's figure instead
