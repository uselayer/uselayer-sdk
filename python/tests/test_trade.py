"""quote(), trade() with the leg-risk guard, and run(strategy): every branch, against fake books.

The pair: buy YES on mkt-a (asks 0.42 × 10, 0.43 × 20, 0.45 × 100) and NO on mkt-b (YES bids
0.60 × 30, so NO asks 0.40 × 30). $1 at settlement for $0.82 before fees.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue

from uselayer import Admin, Book, Client, Level, VenueError

PAIR = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]


def after_first_leg(c: Client, fn: Any) -> None:
    """Run ``fn`` right after the first buy of the pair fills (to change the books mid-trade)."""
    orig = c._execute
    state = {"done": False}

    def wrapped(order: Any, checked: bool = False) -> Any:
        r = orig(order, checked=checked)
        if not state["done"] and order.action == "buy" and r.filled:
            state["done"] = True
            fn(r)
        return r

    c._execute = wrapped  # type: ignore[method-assign]


def test_quote_picks_the_cheaper_pairing_and_prices_it_like_v0_size(make_client: Any) -> None:
    q = make_client().quote(PAIR, size=10)
    assert (q.a.side, q.b.side) == ("yes", "no")  # type: ignore[union-attr]
    assert (q.a.limit_price, q.b.limit_price, q.contracts) == (0.42, 0.40, 10)  # type: ignore[union-attr]
    assert q.fees == 0.34 and q.net_profit == 1.46  # 10 − 4.20 − 4.00 − 0.17 − 0.17


def test_hedged(make_client: Any) -> None:
    c = make_client()
    t = c.trade(PAIR, size=10)
    assert (t.status, t.hedged, t.locked_in, t.exposure) == ("hedged", 10, 1.46, None)
    assert [o.market for o in t.orders] == ["mkt-a", "mkt-b"]  # mkt-a is thinner up to its limit (10 vs 30)
    assert all(o.group_id == t.group_id for o in t.orders)


def test_the_thinner_leg_goes_first(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("mkt-b", bids=[(0.60, 5)], asks=[(0.62, 40)]))
    t = make_client().trade(PAIR, size=10)
    assert t.status == "hedged" and t.hedged == 5 and t.orders[0].market == "mkt-b"


def test_first_leg_misses_nothing_else_is_sent(make_client: Any, venue: FakeVenue) -> None:
    c = make_client()
    orig = c._decide_group

    def then_empty(orders: Any) -> Any:
        v = orig(orders)
        venue.add(FakeMarket("mkt-a", bids=[(0.40, 50)], asks=[(0.50, 10)]))
        return v

    c._decide_group = then_empty  # type: ignore[method-assign]
    t = c.trade(PAIR, size=10)
    assert (t.status, len(t.orders), t.orders[0].filled, t.exposure) == ("missed", 1, 0, None)


def test_a_partial_second_leg_is_chased_to_completion(make_client: Any, venue: FakeVenue) -> None:
    c = make_client()
    after_first_leg(c, lambda _: venue.add(FakeMarket("mkt-b", bids=[(0.60, 4)], asks=[(0.62, 40)])))
    orig = c._execute
    calls = {"b": 0}

    def refill(order: Any, checked: bool = False) -> Any:
        r = orig(order, checked=checked)
        if order.market == "mkt-b":
            calls["b"] += 1
            venue.add(FakeMarket("mkt-b", bids=[(0.60, 30)], asks=[(0.62, 40)]))
        return r

    c._execute = refill  # type: ignore[method-assign]
    t = c.trade(PAIR, size=10)
    assert (t.status, t.hedged, calls["b"]) == ("hedged", 10, 2)


def test_second_leg_misses_and_the_first_is_unwound(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client()
    after_first_leg(c, lambda _: venue.add(FakeMarket("mkt-b", bids=[], asks=[(0.62, 40)])))
    start = clock.now
    t = c.trade(PAIR, size=10, chase_s=3)
    assert (t.status, t.hedged, t.exposure) == ("unwound", 0, None)
    unwind = t.orders[-1]
    assert (unwind.action, unwind.reason, unwind.filled, unwind.avg_price) == ("sell", "unwind", 10, 0.40)
    assert t.unwind_loss == 0.54  # (0.42 − 0.40) × 10 + 0.17 sell fee + 0.17 buy fee
    assert (clock.now - start).total_seconds() >= 3  # it chased for chase_s first
    assert c.positions() == []


def test_unwind_never_sells_below_entry_minus_the_loss_cap(make_client: Any, venue: FakeVenue) -> None:
    alerts: list[dict[str, Any]] = []
    c = make_client(on_alert=alerts.append)

    def crash(_: Any) -> None:
        venue.add(FakeMarket("mkt-b", bids=[], asks=[(0.62, 40)]))
        venue.add(FakeMarket("mkt-a", bids=[(0.30, 50)], asks=[(0.42, 10)]))

    after_first_leg(c, crash)
    t = c.trade(PAIR, size=10)
    assert t.status == "exposed" and t.exposure is not None
    assert (t.exposure.contracts, t.exposure.avg_price, t.exposure.mark, t.exposure.cost_with_fees) == (
        10,
        0.42,
        0.30,
        4.37,
    )
    assert any(a["kind"] == "exposed" for a in alerts)
    assert all(o.action == "buy" for o in t.orders)  # nothing was dumped below 0.37


def test_hold_reports_instead_of_unwinding(make_client: Any, venue: FakeVenue) -> None:
    c = make_client()
    after_first_leg(c, lambda _: venue.add(FakeMarket("mkt-b", bids=[], asks=[(0.62, 40)])))
    t = c.trade(PAIR, size=10, on_miss="hold")
    assert t.status == "exposed" and t.exposure is not None and t.exposure.contracts == 10
    assert all(o.reason != "unwind" for o in t.orders)


def test_kill_during_a_trade_stops_the_second_leg_but_still_unwinds(make_client: Any, tmp_path: Any) -> None:
    store = str(tmp_path / "k.db")
    c = make_client(store=store)
    after_first_leg(c, lambda _: Admin(mode="paper", store=store).kill())
    t = c.trade(PAIR, size=10)
    assert t.status == "unwound" and "kill switch pressed: second leg not sent" in t.notes
    assert [o.reason for o in t.orders] == ["open", "unwind"]


def test_pairs_are_checked_all_or_nothing_before_anything_is_sent(make_client: Any) -> None:
    c = make_client(rules={"max_position": {"per_market": 6}})
    with pytest.raises(VenueError) as e:
        c.trade(PAIR, size=10)
    assert e.value.rule == "max_position" and c.fills() == []


def test_a_pair_needs_one_approval_not_two(make_client: Any) -> None:
    asked: list[str] = []
    c = make_client(rules={"approve_above": 5}, on_approval=lambda o, r: asked.append(r) or True)
    assert c.trade(PAIR, size=10).status == "hedged"
    assert len(asked) == 1 and asked[0].startswith("pair:")


def test_stale_books_send_nothing(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client()
    c.book("mkt-a")
    orig = c.book

    def old_book(market: Any, *, venue: str = "polymarket_us") -> Book:
        b = orig(market, venue=venue)
        return b.model_copy(update={"as_of": b.as_of - timedelta(seconds=60)})

    c.book = old_book  # type: ignore[method-assign]
    with pytest.raises(VenueError) as e:
        c.trade(PAIR, size=10)
    assert e.value.code == "stale_quote" and c.fills() == []


def test_nothing_clears_min_edge_is_a_miss(make_client: Any) -> None:
    t = make_client().trade(PAIR, size=10, min_edge=0.5)
    assert t.status == "missed" and t.orders == ()


def test_run_calls_the_strategy_in_paper_mode(make_client: Any) -> None:
    seen: list[float] = []

    def strategy(client: Client, pair: Any, quote: Any) -> None:
        seen.append(quote.net_profit_per_contract)
        if quote.net_profit_per_contract >= 0.02 and not client.positions():
            client.trade(pair, size=5)

    c = make_client()
    assert c.run(strategy, [PAIR], iterations=2) == 2
    assert len(seen) == 2 and len(c.positions()) == 2


def test_the_same_strategy_runs_in_backtest() -> None:
    def book(market: str, s: int, bid: float, ask: float) -> Book:
        return Book(
            venue="polymarket_us",
            market=market,
            bids=(Level(price=bid, size=50),),
            asks=(Level(price=ask, size=50),),
            as_of=T0 + timedelta(seconds=s),
        )

    books = [
        book("mkt-a", 0, 0.40, 0.42),
        book("mkt-b", 1, 0.60, 0.62),
        book("mkt-a", 60, 0.40, 0.45),
        book("mkt-b", 61, 0.50, 0.52),
    ]
    trades: list[str] = []

    def strategy(client: Client, pair: Any, quote: Any) -> None:
        if quote.net_profit_per_contract >= 0.02 and not client.positions():
            trades.append(client.trade(pair, size=5).status)

    bt = Client(mode="backtest", books=books)
    assert bt.run(strategy, [PAIR]) == 3  # once both books exist, on each new book
    assert trades == ["hedged"] and all(f.mode == "backtest" for f in bt.fills())
