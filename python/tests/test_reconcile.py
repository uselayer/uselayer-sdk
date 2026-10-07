"""Reconciliation, offline: reading every account fill from each venue, and client.reconcile() finding each
kind of difference between the live store and a venue."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import T0, Clock
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uselayer import Client, Fill, Kalshi, Order, PolymarketUS, VenueError, VenuePosition
from uselayer import __main__ as cli
from uselayer.http import Http
from uselayer.reconcile import yes_delta
from uselayer.venues.kalshi import KalshiLive
from uselayer.venues.polymarket_us_live import PolymarketUSLive


def kalshi_key() -> Kalshi:
    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        .decode()
    )
    return Kalshi(key_id="k", private_key_pem=pem)


def pmus_key() -> PolymarketUS:
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    return PolymarketUS(key_id="key-1", secret_key=base64.b64encode(seed).decode())


# ---- reading every fill on the account ----


def test_kalshi_fills_are_read_from_yes_by_book_side_with_paging_and_min_ts() -> None:
    seen: list[httpx.URL] = []
    pages = [
        {
            "fills": [
                # A YES sell, as the demo exchange reported one on 2026-10-04: side "no", action "sell".
                {
                    "fill_id": "f1",
                    "order_id": "v1",
                    "ticker": "KX-A",
                    "book_side": "ask",
                    "side": "no",
                    "action": "sell",
                    "count_fp": "1.00",
                    "yes_price_dollars": "0.0200",
                    "no_price_dollars": "0.9800",
                    "is_taker": True,
                    "fee_cost": "0.001400",
                    "ts": 1791082625,
                },
            ],
            "cursor": "next",
        },
        {
            "fills": [
                {
                    "fill_id": "f2",
                    "order_id": "v2",
                    "ticker": "KX-A",
                    "book_side": "bid",
                    "side": "yes",
                    "action": "buy",
                    "count_fp": "2.50",
                    "yes_price_dollars": "0.0300",
                    "is_taker": False,
                    "fee_cost": "0",
                    "ts": 1791082624,
                },
                # Older fills without book_side keep the older meaning of side and action.
                {
                    "fill_id": "f3",
                    "order_id": "v3",
                    "ticker": "KX-B",
                    "side": "no",
                    "action": "buy",
                    "count_fp": "3.00",
                    "no_price_dollars": "0.6000",
                    "ts": 1791082000,
                },
            ],
            "cursor": "",
        },
    ]

    old = {
        "fill_id": "f0",
        "order_id": "v0",
        "ticker": "KX-OLD",
        "book_side": "bid",
        "count_fp": "1.00",
        "yes_price_dollars": "0.5000",
        "ts": 1754000000,
    }

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/trade-api/v2/historical/cutoff":
            return httpx.Response(200, json={"trades_created_ts": "2026-08-05T00:00:00Z"})
        if r.url.path == "/trade-api/v2/historical/fills":
            historical.append(r.url)
            return httpx.Response(200, json={"fills": [old, pages[1]["fills"][0]], "cursor": ""})
        assert r.url.path == "/trade-api/v2/portfolio/fills"
        seen.append(r.url)
        return httpx.Response(200, json=pages[(len(seen) - 1) % 2])

    historical: list[httpx.URL] = []

    k = KalshiLive(
        Http(transport=httpx.MockTransport(handler), sleep=lambda s: None), kalshi_key(), clock=Clock()
    )
    fs = k.fills(since=T0)
    assert seen[0].params["min_ts"] == str(int(T0.timestamp())) and seen[1].params["cursor"] == "next"
    assert [(f.venue_fill_id, f.order_id, f.side, f.action, f.contracts, f.price) for f in fs] == [
        ("f1", "v1", "yes", "sell", 1.0, 0.02),
        ("f2", "v2", "yes", "buy", 2.5, 0.03),
        ("f3", "v3", "yes", "sell", 3.0, 0.4),
    ]
    assert [yes_delta(f) for f in fs] == [-1.0, 2.5, -3.0]
    assert (fs[0].role, fs[0].fee, fs[1].role) == ("taker", 0.0014, "maker")
    assert historical == []  # since is after Kalshi's cutoff: nothing to read there
    # Without since (or before the cutoff), fills Kalshi moved to /historical/fills are read too, once each.
    every = k.fills()
    assert [f.venue_fill_id for f in every] == ["f1", "f2", "f3", "f0"] and len(historical) == 1


def trade(
    tid: str, *, mine: dict[str, Any], aggressor: bool, at: str, state: str = "TRADE_STATE_NEW"
) -> dict[str, Any]:
    other = {
        "order": {"id": "someone-else", "intent": "ORDER_INTENT_UNDEFINED"},
        "lastShares": mine["lastShares"],
    }
    return {
        "type": "ACTIVITY_TYPE_TRADE",
        "trade": {
            "id": tid,
            "marketSlug": "slug-a",
            "state": state,
            "createTime": at,
            "isAggressor": aggressor,
            "aggressorExecution": mine if aggressor else other,
            "passiveExecution": other if aggressor else mine,
        },
    }


def execution(oid: str, intent: str, shares: str, yes_px: str, at: str, fee: str = "0.02") -> dict[str, Any]:
    return {
        "order": {"id": oid, "intent": intent},
        "lastShares": shares,
        "lastPx": {"value": yes_px, "currency": "USD"},
        "tradeId": f"t-{oid}",
        "transactTime": at,
        "commissionNotionalCollected": {"value": fee, "currency": "USD"},
    }


def test_polymarket_us_fills_take_this_accounts_side_of_each_trade_newest_first() -> None:
    at1, at2, old = "2026-10-01T12:00:05Z", "2026-10-01T11:59:00Z", "2026-09-30T00:00:00Z"
    acts = [
        trade(
            "T1", mine=execution("o1", "ORDER_INTENT_BUY_SHORT", "1.63", "0.795", at1), aggressor=True, at=at1
        ),
        trade(
            "T2",
            mine=execution("o2", "ORDER_INTENT_SELL_LONG", "2", "0.40", at2, "0"),
            aggressor=False,
            at=at2,
        ),
        trade(
            "T3",
            mine=execution("o3", "ORDER_INTENT_BUY_LONG", "9", "0.5", at2),
            aggressor=True,
            at=at2,
            state="TRADE_STATE_BUSTED",
        ),
        trade("T4", mine=execution("o4", "ORDER_INTENT_BUY_LONG", "5", "0.5", old), aggressor=True, at=old),
    ]
    seen: list[httpx.URL] = []

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/v1/portfolio/activities"
        seen.append(r.url)
        return httpx.Response(200, json={"activities": acts, "eof": True})

    p = PolymarketUSLive(
        Http(transport=httpx.MockTransport(handler), sleep=lambda s: None), pmus_key(), clock=Clock()
    )
    fs = p.fills(since=T0 - timedelta(hours=1))
    assert seen[0].params.get_list("types") == ["ACTIVITY_TYPE_TRADE"]
    assert seen[0].params["sortOrder"] == "SORT_ORDER_DESCENDING"
    assert [
        (f.order_id, f.venue_fill_id, f.market, f.side, f.action, f.contracts, f.price, f.role, f.fee)
        for f in fs
    ] == [
        ("o1", "t-o1", "slug-a", "no", "buy", 1.63, 0.205, "taker", 0.02),
        ("o2", "t-o2", "slug-a", "yes", "sell", 2.0, 0.4, "maker", 0.0),
    ]
    assert [yes_delta(f) for f in fs] == [-1.63, -2.0]
    assert len(p.fills()) == 3  # without since: every trade but the busted one


def test_polymarket_us_trade_without_a_readable_side_raises_format_changed() -> None:
    bad = trade(
        "T1",
        mine=execution("o1", "ORDER_INTENT_UNDEFINED", "1", "0.5", "2026-10-01T12:00:00Z"),
        aggressor=True,
        at="2026-10-01T12:00:00Z",
    )

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"activities": [bad], "eof": True})

    p = PolymarketUSLive(
        Http(transport=httpx.MockTransport(handler), sleep=lambda s: None), pmus_key(), clock=Clock()
    )
    with pytest.raises(VenueError) as e:
        p.fills()
    assert e.value.code == "format_changed"


# ---- client.reconcile() against a scripted venue ----


@dataclass
class FakeVenueAccount:
    """A live adapter whose fills, positions and open orders a test sets by hand."""

    venue: str = "kalshi"
    venue_fills: list[Fill] = field(default_factory=list)
    venue_positions: list[VenuePosition] = field(default_factory=list)
    open: list[Order] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)

    def fills(self, *, since: datetime | None = None) -> list[Fill]:
        return [f for f in self.venue_fills if since is None or f.at >= since]

    def positions(self, *, include_closed: bool = False) -> list[VenuePosition]:
        return [p for p in self.venue_positions if include_closed or (p.contracts > 0 and not p.settled)]

    def open_orders(self) -> list[Order]:
        return list(self.open)

    def refresh(self, order: Order) -> tuple[Order, list[Fill]]:
        self.refreshed.append(order.id or "")
        return order, []


def empty_venues(r: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"orders": [], "market_positions": [], "cursor": "", "positions": {}, "eof": True}
    )


@pytest.fixture
def account() -> FakeVenueAccount:
    return FakeVenueAccount()


@pytest.fixture
def live(account: FakeVenueAccount, tmp_path: Any) -> Any:
    clock = Clock(T0 + timedelta(hours=1))
    alerts: list[dict[str, Any]] = []
    c = Client(
        mode="live",
        kalshi=kalshi_key(),
        store=str(tmp_path / "live.db"),
        transport=httpx.MockTransport(empty_venues),
        clock=clock,
        sleep=clock.sleep,
        on_alert=alerts.append,
    )
    c._live = {account.venue: account}  # type: ignore[dict-item]
    c.alerts = alerts  # type: ignore[attr-defined]
    yield c
    c.close()


def sdk_order(
    c: Client,
    oid: str,
    vid: str,
    *,
    market: str = "KX-A",
    side: str = "no",
    action: str = "buy",
    price: float = 0.6,
    size: float = 5,
    filled: float = 0,
    status: str = "filled",
    venue: str = "kalshi",
) -> Order:
    o = Order(
        venue=venue,
        market=market,
        side=side,
        action=action,
        price=price,
        size=size,
        tif="gtc",
        expires_at=T0 + timedelta(days=1),
    )
    o = o.model_copy(
        update={
            "id": oid,
            "client_id": oid,
            "venue_order_id": vid,
            "mode": "live",
            "status": status,
            "filled": filled,
            "created_at": T0,
            "updated_at": T0,
        }
    )
    c.store.save_order(o)
    return o


def vfill(
    fid: str,
    vid: str,
    n: float,
    *,
    market: str = "KX-A",
    side: str = "yes",
    action: str = "sell",
    price: float = 0.4,
    at: datetime = T0 + timedelta(minutes=1),
    venue: str = "kalshi",
) -> Fill:
    return Fill(
        venue=venue,
        market=market,
        order_id=vid,
        venue_fill_id=fid,
        side=side,
        action=action,
        price=price,
        contracts=n,
        role="taker",
        cost=round(price * n, 6),
        fee=0.01,
        fee_estimate=0.01,
        at=at,
    )


def stored(o: Order, fid: str, n: float, at: datetime = T0 + timedelta(minutes=1)) -> Fill:
    f = Fill(
        venue=o.venue,
        market=o.market,
        order_id=o.id or "",
        venue_fill_id=fid,
        side=o.side,
        action=o.action,
        price=o.price,
        contracts=n,
        role="taker",
        cost=round(o.price * n, 6),
        fee=0.01,
        fee_estimate=0.01,
        at=at,
    )
    return f


def pos(market: str, net_yes: float, *, settled: bool = False, venue: str = "kalshi") -> VenuePosition:
    return VenuePosition(
        venue=venue,
        market=market,
        side="yes" if net_yes >= 0 else "no",
        contracts=abs(net_yes),
        settled=settled,
    )


def test_a_store_that_matches_the_venue_reports_nothing(live: Client, account: FakeVenueAccount) -> None:
    o = sdk_order(live, "o1", "v1", filled=5)
    live.store.add_fill(stored(o, "f1", 5))
    account.venue_fills = [vfill("f1", "v1", 5)]  # buy 5 NO at 0.60 = sell 5 YES at 0.40, as Kalshi lists it
    account.venue_positions = [pos("KX-A", -5)]
    r = live.reconcile()
    assert r.ok and r.mismatches == () and r.since == T0 - timedelta(minutes=1)
    assert r.checked["kalshi"] == {"venue_fills": 1, "store_fills": 1, "positions": 1, "open_orders": 0}
    assert live.alerts == []  # type: ignore[attr-defined]


def test_a_fill_the_store_missed_is_reported_in_the_orders_own_terms(
    live: Client, account: FakeVenueAccount
) -> None:
    o = sdk_order(live, "o1", "v1", filled=2)
    live.store.add_fill(stored(o, "f1", 2))
    account.venue_fills = [vfill("f1", "v1", 2), vfill("f2", "v1", 3, at=T0 + timedelta(minutes=2))]
    account.venue_positions = [pos("KX-A", -5)]
    before = live.store.fills()
    r = live.reconcile()
    assert [m.kind for m in r.mismatches] == ["missed_fill", "position"]
    missed = r.of("missed_fill")[0]
    assert (missed.order_id, missed.venue_order_id, missed.store, missed.venue_says) == ("o1", "v1", 2, 5)
    assert missed.fill is not None
    assert (
        missed.fill.order_id,
        missed.fill.side,
        missed.fill.action,
        missed.fill.price,
        missed.fill.contracts,
    ) == ("o1", "no", "buy", 0.6, 3)
    p = r.of("position")[0]
    assert (p.store, p.venue_says) == (-2, -5)
    assert live.store.fills() == before  # reporting changes nothing
    assert live.alerts[-1]["kind"] == "reconcile_mismatch"  # type: ignore[attr-defined]
    assert live.alerts[-1]["kinds"] == {"missed_fill": 1, "position": 1}  # type: ignore[attr-defined]


def test_a_store_fill_the_venue_doesnt_show_is_unknown(live: Client, account: FakeVenueAccount) -> None:
    o = sdk_order(live, "o1", "v1", filled=5)
    live.store.add_fill(stored(o, "f1", 2))
    live.store.add_fill(stored(o, "f-ghost", 3, at=T0 + timedelta(minutes=3)))
    account.venue_fills = [vfill("f1", "v1", 2)]
    account.venue_positions = [pos("KX-A", -2)]
    r = live.reconcile()
    assert [m.kind for m in r.mismatches] == ["unknown_fill", "position"]
    u = r.of("unknown_fill")[0]
    assert u.fill is not None and u.fill.venue_fill_id == "f-ghost" and (u.store, u.venue_says) == (5, 2)


def test_trades_outside_the_sdk_show_as_outside_fills_and_explain_the_position(
    live: Client, account: FakeVenueAccount
) -> None:
    account.venue_fills = [vfill("x1", "website-order", 3, market="KX-B", action="buy", price=0.3)]
    account.venue_positions = [pos("KX-B", 3), pos("KX-OLD", 10)]
    r = live.reconcile(since=T0)
    assert [(m.kind, m.market) for m in r.mismatches] == [
        ("outside_fill", "KX-B"),
        ("position", "KX-B"),
        ("position", "KX-OLD"),
    ]
    out = r.of("outside_fill")[0]
    assert out.venue_order_id == "website-order" and out.fill is not None and out.fill.contracts == 3
    assert "account for +3" in r.of("position")[0].message
    assert r.of("position")[1].venue_says == 10 and "account for" not in r.of("position")[1].message


def test_open_orders_are_compared_both_ways(live: Client, account: FakeVenueAccount) -> None:
    resting = sdk_order(live, "o1", "v1", status="open")
    sdk_order(live, "o2", "v2", status="open")  # the store thinks it's open; the venue doesn't list it
    theirs = Order(
        venue="kalshi", market="KX-C", side="yes", price=0.1, size=4, tif="gtc", expires_at=T0
    ).model_copy(update={"venue_order_id": "v9", "id": "v9", "mode": "live"})
    account.open = [resting, theirs]
    r = live.reconcile()
    assert [(m.kind, m.venue_order_id) for m in r.mismatches] == [
        ("outside_order", "v9"),
        ("stale_order", "v2"),
    ]


def test_markets_the_venue_settled_are_not_compared(live: Client, account: FakeVenueAccount) -> None:
    o = sdk_order(live, "o1", "v1", market="KX-DONE", filled=5)
    live.store.add_fill(stored(o, "f1", 5))
    account.venue_fills = [vfill("f1", "v1", 5, market="KX-DONE")]
    account.venue_positions = [pos("KX-DONE", 0, settled=True)]
    r = live.reconcile()
    assert r.ok and r.settled == ("kalshi:KX-DONE",)


def test_polymarket_us_short_slugs_compare_as_the_same_market(live: Client) -> None:
    account = FakeVenueAccount(venue="polymarket_us")
    live._live = {"polymarket_us": account}  # type: ignore[dict-item]
    # The SDK bought 2 YES of "slug-a:short" (the short side); the venue reports 2 short on "slug-a".
    o = sdk_order(
        live, "o1", "v1", venue="polymarket_us", market="slug-a:short", side="yes", price=0.3, filled=2
    )
    live.store.add_fill(stored(o, "t1", 2))
    account.venue_fills = [
        vfill("t1", "v1", 2, venue="polymarket_us", market="slug-a", side="no", action="buy", price=0.3)
    ]
    account.venue_positions = [pos("slug-a", -2, venue="polymarket_us")]
    assert live.reconcile().ok


def test_repair_adds_missed_fills_of_closed_sdk_orders_only(live: Client, account: FakeVenueAccount) -> None:
    done = sdk_order(live, "o1", "v1", filled=2)
    live.store.add_fill(stored(done, "f1", 2))
    sdk_order(live, "o2", "v2", market="KX-OPEN", status="open")
    account.open = [live.store.order("o2")]  # type: ignore[list-item]
    account.venue_fills = [
        vfill("f1", "v1", 2),
        vfill("f2", "v1", 3, at=T0 + timedelta(minutes=2)),
        vfill("f3", "v2", 1, market="KX-OPEN"),  # still open in the store: sync()'s job
        vfill("x1", "website-order", 4, market="KX-B", action="buy", price=0.3),  # never added
    ]
    account.venue_positions = [pos("KX-A", -5), pos("KX-OPEN", -1), pos("KX-B", 4)]
    r = live.reconcile(repair=True)
    assert account.refreshed == ["o2"]  # repair ran sync() first
    assert [f.venue_fill_id for f in r.repaired] == ["f2"]
    added = next(f for f in live.store.fills() if isinstance(f, Fill) and f.venue_fill_id == "f2")
    assert (added.order_id, added.side, added.action, added.price, added.contracts) == (
        "o1",
        "no",
        "buy",
        0.6,
        3,
    )
    assert sorted((m.kind, m.market) for m in r.mismatches) == [
        ("missed_fill", "KX-OPEN"),
        ("outside_fill", "KX-B"),
        ("position", "KX-B"),
        ("position", "KX-OPEN"),
    ]
    assert [d["result"] for d in live.decisions() if d["result"] == "reconcile_repair"] == [
        "reconcile_repair"
    ]
    again = live.reconcile(repair=True)
    assert again.repaired == ()  # the same fill is never added twice


def test_old_polymarket_us_fill_ids_stand_for_the_trades_they_counted(
    live: Client, account: FakeVenueAccount
) -> None:
    """A store fill "v1:2.0" (refresh() before 0.3.0) is the order's first 2 contracts: the venue's trades
    f1 and f2. Only f3 is missing, and repair adds it alone."""
    account.venue = "polymarket_us"
    live._live = {"polymarket_us": account}  # type: ignore[dict-item]
    o = sdk_order(live, "o1", "v1", venue="polymarket_us", market="m", side="yes", filled=3)
    live.store.add_fill(stored(o, "v1:2.0", 2, at=T0 + timedelta(minutes=5)))
    account.venue_fills = [
        vfill(f, "v1", 1, venue="polymarket_us", market="m", action="buy", at=T0 + timedelta(minutes=i))
        for i, f in ((1, "f1"), (2, "f2"), (8, "f3"))
    ]
    account.venue_positions = [pos("m", 3, venue="polymarket_us")]
    before = live.reconcile()
    assert sorted(m.kind for m in before.mismatches) == ["missed_fill", "position"]
    (m,) = before.of("missed_fill")
    assert m.fill is not None and m.fill.venue_fill_id == "f3"
    r = live.reconcile(repair=True)
    assert r.ok and [f.venue_fill_id for f in r.repaired] == ["f3"]
    # The other way: a store fill "v1:4.0" the venue's trades don't reach is the one reported.
    live.store.add_fill(stored(o, "v1:4.0", 1, at=T0 + timedelta(minutes=9)))
    (u,) = live.reconcile().of("unknown_fill")
    assert u.fill is not None and u.fill.venue_fill_id == "v1:4.0"


def test_reconcile_is_live_only(tmp_path: Any) -> None:
    with (
        Client(mode="paper", store=str(tmp_path / "p.db"), transport=httpx.MockTransport(empty_venues)) as c,
        pytest.raises(VenueError) as e,
    ):
        c.reconcile()
    assert e.value.code == "not_available"


def test_cli_prints_each_mismatch_and_exits_1(
    live: Client,
    account: FakeVenueAccount,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account.venue_positions = [pos("KX-B", 3)]

    class Same:
        def __init__(self, **kw: Any) -> None:
            assert kw["mode"] == "live" and kw["store"] == "x.db"

        def __enter__(self) -> Client:
            return live

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr("uselayer.client.Client", Same)
    assert cli.main(["reconcile", "--store", "x.db"]) == 1
    out = capsys.readouterr().out
    assert "✗ position · Kalshi · KX-B: Net YES contracts: the store says +0, the venue says +3." in out
    account.venue_positions = []
    assert cli.main(["reconcile", "--store", "x.db", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["ok"] is True
