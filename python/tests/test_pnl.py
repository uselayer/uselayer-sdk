"""client.pnl() and settlement, hand-checked.

The fake Polymarket US market ``mkt-a`` offers 10 @ .42 and bids .40; ``mkt-b`` offers 40 @ .62 and
bids .60. Its taker fee is 0.0695 × C × p × (1 − p), rounded to the cent. Every position here is
opened by a taker fill, so these amounts don't depend on how resting orders fill.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from conftest import T0, Clock, FakeVenue

from uselayer import Book, Client, Level, Resolution, SimulatedFill, VenueError
from uselayer.events import SimulatedSettlement
from uselayer.http import Http
from uselayer.portfolio import build
from uselayer.venues.base import Payout
from uselayer.venues.polymarket_us import PolymarketUSPublic


def buy(c: Client, **kw: Any) -> Any:
    base: dict[str, Any] = {"venue": "polymarket_us", "market": "mkt-a", "side": "yes"}
    return c.buy(**{**base, **kw})


def resolved(market: str, outcome: str, at: Any, **kw: Any) -> Resolution:
    return Resolution(venue="polymarket_us", market=market, outcome=outcome, as_of=at, **kw)  # type: ignore[arg-type]


def test_paper_pnl_by_hand_through_a_yes_settlement(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client()
    buy(c, price=0.42, size=10)  # 10 @ .42 = 4.20; fee .17 (0.0695 × 10 × .42 × .58 = .1693)
    p = c.pnl()
    (r,) = p.rows
    # Held at the .40 bid: 10 × .40 − 4.20 = −.20
    assert (r.contracts, r.cost, r.realized, r.mark, r.unrealized, r.fees) == (10, 4.2, 0, 0.40, -0.2, 0.17)
    assert (p.net, p.missing_marks, r.settled) == (-0.37, (), False)

    c.sell(venue="polymarket_us", market="mkt-a", side="yes", price=0.40, size=4)  # 1.60 for 1.68; fee .07
    venue.settlements["mkt-a"] = 1  # Polymarket US settles YES
    clock.advance(61)
    p = c.pnl()
    (r,) = p.rows
    # realized: (1.60 − 1.68) + (6 × $1 − 2.52) = −.08 + 3.48 = 3.40; fees .17 + .07 = .24
    assert (r.contracts, r.realized, r.unrealized, r.fees) == (0, 3.4, None, 0.24)
    assert (r.settled, r.outcome) == (True, "yes")
    assert (p.realized, p.unrealized, p.fees, p.net) == (3.4, 0, 0.24, 3.16)
    # By cash: paid 4.20 + .17; got 1.60 − .07 + 6.00. 7.53 − 4.37 = 3.16
    assert c.positions() == []
    (s,) = c.settlements()
    assert (s.side, s.outcome, s.contracts, s.payout, s.proceeds, s.mode) == (
        "yes",
        "yes",
        6,
        1.0,
        6.0,
        "paper",
    )


def test_the_side_that_lost_pays_nothing(make_client: Any, clock: Clock) -> None:
    c = make_client()
    buy(c, market="mkt-b", side="no", price=0.40, size=10)  # NO ask = 1 − .60; 4.00, fee .17 (.1668)
    (s,) = c.settle([resolved("mkt-b", "yes", clock.now)])
    assert (s.side, s.payout, s.proceeds) == ("no", 0.0, 0.0)
    p = c.pnl()
    assert (p.realized, p.unrealized, p.fees, p.net) == (-4.0, 0, 0.17, -4.17)


def test_a_void_pays_back_the_cost_or_the_venues_price(make_client: Any, clock: Clock) -> None:
    c = make_client()
    buy(c, price=0.42, size=10)  # 4.20; fee .17
    buy(c, market="mkt-b", price=0.62, size=5)  # 3.10; fee .08 (0.0695 × 5 × .62 × .38 = .0819)
    c.settle([resolved("mkt-a", "void", clock.now), resolved("mkt-b", "void", clock.now, payout=0.5)])
    a, b = sorted(c.pnl().rows, key=lambda r: r.market)
    assert (a.outcome, a.realized, a.fees, a.net) == ("void", 0.0, 0.17, -0.17)  # 4.20 back; the fee isn't
    assert (b.outcome, b.realized, b.fees, b.net) == ("void", -0.6, 0.08, -0.68)  # 5 × .50 = 2.50 for 3.10


def test_a_settled_market_takes_no_orders_and_pays_once(make_client: Any, clock: Clock) -> None:
    c = make_client()
    buy(c, price=0.42, size=10)
    resting = buy(c, price=0.39, size=5, tif="gtc")
    r = resolved("mkt-a", "no", clock.now)
    assert len(c.settle([r])) == 1
    assert c.store.order(resting.id).status == "canceled"
    with pytest.raises(VenueError) as e:
        buy(c, price=0.42, size=1)
    assert e.value.code == "market_closed"
    with pytest.raises(VenueError) as e2:
        c.sell(venue="polymarket_us", market="mkt-a", side="yes", price=0.40, size=1)
    assert "held" in e2.value.message
    assert c.settle([r]) == [] and len(c.settlements()) == 1


def test_paper_asks_the_venue_at_most_once_a_minute(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client()
    buy(c, price=0.42, size=10)

    def asked() -> int:
        return sum(r.url.path.endswith("/settlement") for r in venue.requests)

    c.positions(), c.pnl(), c.monitor()
    assert asked() == 1
    clock.advance(61)
    c.positions()
    assert asked() == 2
    c.settle()  # asking directly always reads
    assert asked() == 3 and c.settlements() == []


def test_pnl_and_max_daily_loss_agree(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client()
    buy(c, price=0.42, size=10)
    c.sell(venue="polymarket_us", market="mkt-a", side="yes", price=0.40, size=4)
    # −.08 realized, 6 × .40 − 2.52 = −.12 open, .24 fees
    assert c.pnl().net == round(c._context(None).pnl_today, 6) == -0.44
    venue.settlements["mkt-a"] = 0
    c.settle()
    assert c.pnl().net == round(c._context(None).pnl_today, 6) == -2.84  # −.08 − 2.52 − .24


def test_polymarket_us_payout_reads_the_settlement_price(venue: FakeVenue) -> None:
    pm = PolymarketUSPublic(Http(transport=venue.transport()))
    assert pm.payout("mkt-a") is None  # 404 until it settles
    venue.settlements["mkt-a"] = 1
    assert (pm.payout("mkt-a"), pm.payout("mkt-a:short"), pm.payout("mkt-a:long")) == (
        Payout(1.0),
        Payout(0.0),
        Payout(1.0),
    )  # Polymarket US's answer carries no time
    venue.settlements["mkt-a"] = "0.5"
    assert pm.payout("mkt-a") == Payout(0.5)
    venue.settlements["mkt-a"] = 7
    with pytest.raises(VenueError) as e:
        pm.payout("mkt-a")
    assert e.value.code == "format_changed"


def test_a_settlement_follows_its_own_positions_fills_whatever_else_was_stored_first() -> None:
    def fill(market: str, s: int) -> SimulatedFill:
        at = T0 + timedelta(seconds=s)
        return SimulatedFill(
            mode="paper", venue="polymarket_us", market=market, order_id="o", side="yes", action="buy",
            price=0.4, contracts=10, role="taker", cost=4.0, fee=0.0, at=at, book_as_of=at,
        )  # fmt: skip

    # A later fill on another market was stored before an older fill on the settled one.
    fills = [fill("b", 100), fill("a", 0)]
    paid = SimulatedSettlement(
        mode="paper", venue="polymarket_us", market="a", side="yes", outcome="yes",
        contracts=10, payout=1.0, proceeds=10.0, at=T0 + timedelta(seconds=50),
    )  # fmt: skip
    rows = {p.market: p for p in build(fills, T0, [paid]).rows}
    assert (rows["a"].realized, rows["a"].contracts, rows["b"].contracts) == (6.0, 0, 10)


def test_resolution_pays_each_side() -> None:
    def r(outcome: str, payout: float | None = None) -> Resolution:
        return resolved("m", outcome, T0, payout=payout)

    assert (r("yes").paid("yes", 0.3), r("yes").paid("no", 0.3)) == (1.0, 0.0)
    assert (r("no").paid("yes", 0.3), r("no").paid("no", 0.3)) == (0.0, 1.0)
    assert (r("void").paid("yes", 0.3), r("void").paid("no", 0.7)) == (0.3, 0.7)
    assert (r("void", 0.37).paid("yes", 0.3), r("void", 0.37).paid("no", 0.3)) == (0.37, 0.63)


# ---- backtest ----


def book(s: int, bid: float, ask: float) -> Book:
    return Book(
        venue="polymarket_us",
        market="m",
        bids=(Level(price=bid, size=50),),
        asks=(Level(price=ask, size=50),),
        as_of=T0 + timedelta(seconds=s),
    )


def test_backtest_marks_at_the_latest_replayed_bid() -> None:
    bt = Client(mode="backtest", books=[book(0, 0.40, 0.42), book(60, 0.38, 0.40)])
    bt.replay(
        lambda c, b: (
            c.buy(venue="polymarket_us", market="m", side="yes", price=0.42, size=10)
            if b.as_of == T0
            else None
        )
    )
    (r,) = bt.pnl().rows
    fee = bt.fills()[0].fee
    assert (r.mark, r.mark_as_of, r.unrealized, r.fees) == (0.38, T0 + timedelta(seconds=60), -0.4, fee)


def test_backtest_pays_out_at_each_resolution_it_replays() -> None:
    events = [book(0, 0.40, 0.42), resolved("m", "yes", T0 + timedelta(seconds=30)), book(60, 0.38, 0.40)]
    seen: list[str] = []

    def on_book(c: Client, b: Book) -> None:
        try:
            c.buy(venue="polymarket_us", market="m", side="yes", price=b.asks[0].price, size=10)
            seen.append("filled")
        except VenueError as e:
            seen.append(e.code)

    bt = Client(mode="backtest", books=events)
    out = bt.replay(on_book)
    assert seen == ["filled", "market_closed"]
    assert (out["resolutions"], out["settlements"], out["positions"]) == (1, 1, [])
    (r,) = bt.pnl().rows
    fee = bt.fills()[0].fee
    assert fee > 0
    # 10 × $1 − 4.20 = 5.80, less the fee
    assert (r.realized, r.fees, r.net, r.outcome) == (5.8, fee, round(5.8 - fee, 6), "yes")
    assert bt.settlements()[0].at == T0 + timedelta(seconds=30)
