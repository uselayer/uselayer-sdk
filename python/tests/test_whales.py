"""Whale tracking and copy trading: one trade shape for both venues, links with evidence, paper copies."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import T0, Clock, FakeVenue

from uselayer import Resolution, Trader, WhaleTrade
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
            (DATA, "/v2/user-stats"): {"data": {"all_time_pnl": {"volume": volume, "economic_pnl": 5.0}}},
        }

    me = Trader(venue="kalshi", id="Blackbriar", name="Blackbriar", volume_unit="contracts")
    assert whales(routes(50)).links(me) == []
    links = whales(routes(2_000_000)).links(me)
    assert len(links) == 1 and links[0].tier == "possible" and links[0].trader.id == "0xbb"
    assert "$2,000,000 traded" in links[0].evidence[0].detail


# ---- copy trading (paper, against the fake Polymarket US book) ----


def test_copier_copies_new_trades_onto_the_twin_and_explains_skips(
    make_client: Any, venue: FakeVenue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue.layer_answers["/v0/match"] = {"matched_market": {"venue": "polymarket_us", "market_id": "mkt-a"}}
    c = make_client(layer_key="lyr_test")
    feed: list[WhaleTrade] = []
    monkeypatch.setattr(c.whales, "trades", lambda *a, **k: list(feed))

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
    assert cp.poll() == []  # nothing new


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
