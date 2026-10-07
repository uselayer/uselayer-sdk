from __future__ import annotations

from typing import Any

import pytest
from conftest import Clock, FakeMarket, FakeVenue

from uselayer import Admin, Client, VenueError


def buy(c: Client, **kw: Any) -> Any:
    base: dict[str, Any] = {"venue": "polymarket_us", "market": "mkt-a", "side": "yes"}
    return c.buy(**{**base, **kw})


def test_ioc_fills_what_crosses_and_cancels_the_rest(make_client: Any) -> None:
    c = make_client()
    o = buy(c, price=0.43, size=50)  # 10 @ .42 + 20 @ .43 cross; 20 more don't
    assert (o.status, o.filled, o.avg_price) == ("canceled", 30, round((4.2 + 8.6) / 30, 6))
    fees = [f.fee for f in c.fills()]
    assert (
        fees == [0.17, 0.34] and o.fees == 0.51
    )  # 0.0695 × 10 × .42 × .58 = 0.1693; × 20 × .43 × .57 = 0.3407
    assert all(f.simulated for f in c.fills())


def test_fok_fills_all_or_nothing(make_client: Any) -> None:
    c = make_client()
    assert (buy(c, price=0.43, size=31, tif="fok").status, c.fills()) == ("canceled", [])
    assert buy(c, price=0.43, size=30, tif="fok").status == "filled"


def test_gtc_rests_then_fills_as_maker_or_expires(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client(rules={"order_ttl_s": 120})
    o = buy(c, price=0.41, size=5, tif="gtc")
    assert o.status == "open" and o.expires_at is not None and c.orders()[0].id == o.id
    venue.markets["mkt-a"] = FakeMarket("mkt-a", bids=[(0.39, 50)], asks=[(0.41, 3)])
    clock.advance(5)
    c.monitor()
    (o2,) = c.orders(open=False)
    assert (o2.status, o2.filled) == ("open", 3)
    f = c.fills()[-1]
    assert (f.role, f.price, f.fee) == ("maker", 0.41, -0.01)  # a maker rebate: 0.0125 × 3 × .41 × .59
    clock.advance(200)
    assert c.orders() == [] and c.orders(open=False)[0].status == "expired"


def test_post_only_that_would_cross_is_rejected(make_client: Any) -> None:
    c = make_client()
    assert buy(c, price=0.42, size=1, tif="gtc", post_only=True).status == "rejected"
    assert buy(c, price=0.41, size=1, tif="gtc", post_only=True).status == "open"


def test_venue_limits_never_round_a_price(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("cents", bids=[(0.40, 5)], asks=[(0.42, 5)], tick=0.01, min_qty=2))
    c = make_client()
    for kw, word in [({"price": 0.425}, "tick"), ({"size": 1, "price": 0.42}, "minimum")]:
        with pytest.raises(VenueError) as e:
            buy(c, market="cents", **{"size": 2, **kw})
        assert e.value.code == "invalid_order" and word in e.value.message
    with pytest.raises(VenueError) as e2:
        c.sell(venue="polymarket_us", market="cents", side="yes", price=0.40, size=2)
    assert "held" in e2.value.message


def test_no_side_buys_against_yes_bids_and_sells_back(make_client: Any) -> None:
    c = make_client()
    o = buy(c, side="no", price=0.60, size=10)  # NO ask = 1 − .40 bid
    assert (o.status, o.avg_price) == ("filled", 0.6)
    s = c.sell(venue="polymarket_us", market="mkt-a", side="no", price=0.58, size=10)  # NO bid = 1 − .42 ask
    assert s.status == "filled" and c.positions() == []


def test_kill_switch_blocks_cancels_flattens_and_survives_restarts(make_client: Any, tmp_path: Any) -> None:
    store = str(tmp_path / "k.db")
    c = make_client(store=store)
    buy(c, price=0.42, size=10)
    resting = buy(c, price=0.39, size=5, tif="gtc")
    sold = c.kill(flatten=True)
    assert c.killed and c.store.order(resting.id).status == "canceled"
    assert sold[0].reason == "kill" and sold[0].status == "filled" and c.positions() == []
    with pytest.raises(VenueError) as e:
        buy(c, price=0.42, size=1)
    assert e.value.rule == "kill_switch"
    c2 = make_client(store=store)  # a restart stays killed
    assert c2.killed
    assert not hasattr(c2, "resume")  # the client an agent holds can't turn it off
    Admin(mode="paper", store=store).resume()
    assert buy(c2, price=0.42, size=1).status == "filled"


def test_kill_from_another_terminal_stops_the_next_send(make_client: Any, tmp_path: Any) -> None:
    from uselayer.__main__ import main

    store = str(tmp_path / "x.db")
    c = make_client(store=store)
    assert main(["kill", "--mode", "paper", "--store", store]) == 0
    with pytest.raises(VenueError):
        buy(c, price=0.42, size=1)


def test_throttle_waits_instead_of_exceeding_five_orders_a_second(make_client: Any, clock: Clock) -> None:
    c = make_client()
    for _ in range(6):
        buy(c, price=0.45, size=1)
    assert sum(clock.slept) == pytest.approx(1.0, abs=0.01)


def test_waits_for_a_fresh_copy_of_a_cached_book(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    venue.cache_age_s = 25  # the venue's cache served a 25-second-old copy
    c = make_client()
    c.book("mkt-a")
    assert clock.slept and clock.slept[0] == pytest.approx(5.5)


def test_closed_markets_and_switched_off_venues(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("shut", bids=[(0.4, 1)], asks=[(0.42, 1)], status="MARKET_STATUS_CLOSED"))
    c = make_client()
    with pytest.raises(VenueError) as e:
        buy(c, market="shut", price=0.42, size=1)
    assert e.value.code == "market_closed"
    with pytest.raises(VenueError) as e2:
        c.buy(venue="polymarket", market="X", side="yes", price=0.4, size=1)
    assert e2.value.code == "venue_switched_off" and "polymarket_us" in (e2.value.hint or "")
    # Kalshi is read with the customer's own key: without one, the error says how to pass it.
    with pytest.raises(VenueError) as e3:
        c.buy(venue="kalshi", market="X", side="yes", price=0.4, size=1)
    assert e3.value.code == "auth_failed" and "Kalshi(" in (e3.value.next or "")


def test_live_mode_needs_a_key(monkeypatch: Any) -> None:
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    with pytest.raises(VenueError) as e:
        Client(mode="live")
    assert e.value.code == "auth_failed" and "PolymarketUS" in (e.value.hint or "")
    assert "Kalshi" in (e.value.hint or "")


def test_switches_have_no_override(monkeypatch: Any) -> None:
    import uselayer._switches as sw

    monkeypatch.setenv("USELAYER_KALSHI", "1")
    monkeypatch.setenv("USELAYER_POLYMARKET", "1")
    assert sw.TRADING == {"polymarket_us": True, "kalshi": True, "polymarket": False}
    assert sw.LAYER_HISTORY is False and sw.trading_venues() == ["polymarket_us", "kalshi"]
    assert sw.paper_venues() == ["kalshi", "polymarket_us"]
    assert {"polymarket_us", "kalshi"} == sw.LIVE_ADAPTERS
