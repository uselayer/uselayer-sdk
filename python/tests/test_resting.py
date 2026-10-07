"""The estimated line ahead of a resting order, on hand-worked book and trade sequences (roadmap 11.9 + 18.7).

Sizes in the model are millionths of a contract; ``U`` turns contracts into them.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue

from uselayer import Book, Client, Level, VenueError
from uselayer.events import TradePrint
from uselayer.orders import Order
from uselayer.resting import Line, on_book, on_trade, start

U = 1_000_000


def book(
    s: float, bids: list[tuple[float, float]], asks: list[tuple[float, float]], market: str = "m"
) -> Book:
    return Book(
        venue="polymarket_us",
        market=market,
        bids=tuple(Level(price=p, size=n) for p, n in bids),
        asks=tuple(Level(price=p, size=n) for p, n in asks),
        as_of=T0 + timedelta(seconds=s),
    )


def trade(
    s: float, price: float, size: float, aggressor: str | None, tid: str | None = None, market: str = "m"
) -> TradePrint:
    return TradePrint(
        venue="polymarket_us",
        market=market,
        price=price,
        size=size,
        aggressor=aggressor,  # type: ignore[arg-type]
        trade_id=tid,
        as_of=T0 + timedelta(seconds=s),
    )


def order(
    side: str = "yes", action: str = "buy", price: float = 0.40, size: float = 10, filled: float = 0
) -> Order:
    return Order(
        venue="polymarket_us",
        market="m",
        side=side,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        price=price,
        size=size,
        tif="gtc",
        filled=filled,
    )


B0 = book(0, [(0.40, 50), (0.39, 100)], [(0.42, 30)])


# ---- the model alone ----------------------------------------------------------------------------


def test_trades_at_the_price_use_up_the_line_ahead_first() -> None:
    o = order()
    line = start(o, B0)
    assert line.ahead_est == 50 * U  # everything already at 0.40 is ahead
    assert on_trade(o, line, trade(1, 0.40, 30, "sell"), B0) == 0 and line.ahead_est == 20 * U
    assert on_trade(o, line, trade(2, 0.40, 25, "sell"), B0) == 5 * U and line.ahead_est == 0
    assert on_trade(order(filled=5), line, trade(3, 0.40, 10, "sell"), B0) == 5 * U  # only what's left


def test_a_trade_through_the_price_means_nobody_was_left_there() -> None:
    o = order()
    line = start(o, B0)
    assert on_trade(o, line, trade(1, 0.39, 3, "sell"), B0) == 3 * U and line.ahead_est == 0
    assert on_trade(o, start(o, B0), trade(1, 0.38, 40, "sell"), B0) == 10 * U  # capped at the order


def test_trades_on_the_other_side_or_at_better_prices_fill_nothing() -> None:
    o = order()
    line = start(o, B0)
    assert on_trade(o, line, trade(1, 0.40, 30, "buy"), B0) == 0  # lifted the asks
    assert on_trade(o, line, trade(2, 0.41, 30, "sell"), B0) == 0  # hit a better bid, ahead in price
    assert line.ahead_est == 50 * U


def test_a_repeated_trade_id_counts_once() -> None:
    o = order()
    line = start(o, B0)
    t = trade(1, 0.40, 30, "sell", tid="t-1")
    on_trade(o, line, t, B0)
    on_trade(o, line, t, B0)
    assert line.ahead_est == 20 * U


def test_unknown_aggressor_is_judged_from_the_book_before() -> None:
    o = order()
    line = start(o, B0)
    assert on_trade(o, line, trade(1, 0.40, 30, None), B0) == 0 and line.ahead_est == 20 * U  # at the bid
    assert (
        on_trade(o, line, trade(2, 0.41, 30, None), B0) == 0 and line.ahead_est == 20 * U
    )  # inside: ignored
    assert on_trade(o, line, trade(3, 0.40, 30, None), None) == 0 and line.ahead_est == 20 * U  # no book


def test_cancels_spread_through_the_line_or_all_behind() -> None:
    o = order()
    for cancels, ahead in (("proportional", 30), ("behind", 50)):
        line = start(o, B0)  # 50 ahead
        # 50 more join behind: 100 at the price, 50 of them ahead.
        assert on_book(o, line, book(1, [(0.40, 100)], [(0.42, 30)]), cancels=cancels) == 0  # type: ignore[arg-type]
        assert line.ahead_est == 50 * U
        # 40 leave with no trades: cancels. Proportional: 40 × 50/100 = 20 of them were ahead.
        on_book(o, line, book(2, [(0.40, 60)], [(0.42, 30)]), cancels=cancels)  # type: ignore[arg-type]
        assert line.ahead_est == ahead * U, cancels


def test_a_level_drop_the_trades_explain_is_not_cancels() -> None:
    o = order()
    line = start(o, B0)
    on_trade(o, line, trade(1, 0.40, 30, "sell"), B0)  # 50 → 20 ahead
    on_book(o, line, book(2, [(0.40, 20)], [(0.42, 30)]))  # the 30 traded are the whole drop
    assert line.ahead_est == 20 * U


def test_a_trade_arriving_after_its_book_change_moves_the_line_once() -> None:
    # Seen on polymarket.com: the level at the order's price empties, then the trade message arrives.
    o = order(price=0.62, size=50)
    b = book(0, [(0.62, 70)], [(0.63, 100)])
    line = start(o, b)  # 70 ahead
    assert on_book(o, line, book(1, [(0.61, 40)], [(0.63, 100)])) == 0 and line.ahead_est == 0
    # The 70 that left were this trade, not cancels: only the half contract beyond them reaches the order.
    assert on_trade(o, line, trade(1.01, 0.62, 70.5, "sell"), b) == 500_000


def test_book_first_trades_come_from_the_front_with_either_cancel_setting() -> None:
    o = order()
    for cancels in ("proportional", "behind"):
        line = start(o, B0)  # 50 ahead
        on_book(o, line, book(1, [(0.40, 100)], [(0.42, 30)]), cancels=cancels)  # type: ignore[arg-type]
        on_book(o, line, book(2, [(0.40, 70)], [(0.42, 30)]), cancels=cancels)  # type: ignore[arg-type]
        # proportional moved 30 × 50/100 = 15 for "cancels", behind moved 0; the trade moves the rest
        assert on_trade(o, line, trade(2.01, 0.40, 30, "sell"), B0) == 0
        assert line.ahead_est == 20 * U, cancels
        assert on_trade(o, line, trade(3, 0.40, 25, "sell"), B0) == 5 * U, cancels


def test_a_book_first_trade_bigger_than_the_line_fills_the_order() -> None:
    # The coordinator's repro on #19: a book-first trade of 10 with 5 ahead filled nothing.
    o = order(price=0.40, size=50)
    b0 = book(0, [(0.40, 5)], [(0.42, 30)])
    line = start(o, b0)  # 5 ahead
    b1 = book(1, [(0.40, 20)], [(0.42, 30)])
    on_book(o, line, b1)  # 15 join behind
    on_book(o, line, book(1.5, [(0.40, 10)], [(0.42, 30)]))  # the level drops 10 first
    assert on_trade(o, line, trade(1.51, 0.40, 10, "sell"), b1) == 5 * U


@pytest.mark.parametrize("cancels", ["proportional", "behind"])
@pytest.mark.parametrize("size", [3, 5, 10, 20, 25])  # < ahead, = ahead, > ahead, = level, > level
def test_the_fill_is_the_same_whichever_arrives_first(cancels: Any, size: float) -> None:
    o = order(price=0.40, size=50)
    b0 = book(0, [(0.40, 5)], [(0.42, 30)])
    b1 = book(1, [(0.40, 20)], [(0.42, 30)])  # 5 ahead, 15 behind
    left = [(0.40, 20 - size)] if size < 20 else []  # the level empties
    after = book(2, [*left, (0.39, 100)], [(0.42, 30)])
    t = trade(2.01, 0.40, size, "sell")

    trade_first = start(o, b0)
    on_book(o, trade_first, b1, cancels=cancels)
    a = on_trade(o, trade_first, t.model_copy(update={"as_of": T0 + timedelta(seconds=1.99)}), b1)
    a += on_book(o, trade_first, after, cancels=cancels)

    book_first = start(o, b0)
    on_book(o, book_first, b1, cancels=cancels)
    b = on_book(o, book_first, after, cancels=cancels)
    b += on_trade(o, book_first, t, b1)

    assert a == b == min(50, max(0, size - 5)) * U
    assert trade_first.ahead_est == book_first.ahead_est


def test_a_drop_no_trade_explains_in_time_stays_cancels() -> None:
    o = order()
    line = start(o, B0)  # 50 ahead, all of the level
    on_book(o, line, book(1, [(0.40, 20), (0.39, 100)], [(0.42, 30)]))  # 30 cancelled: 20 ahead
    assert line.ahead_est == 20 * U
    assert on_trade(o, line, trade(10, 0.40, 25, "sell"), B0) == 5 * U  # 9 s later: a new trade


def test_the_line_never_holds_more_than_the_level_shows() -> None:
    o = order()
    line = start(o, B0)
    on_book(o, line, book(1, [(0.39, 100)], [(0.42, 30)]))  # 0.40 emptied: first in line now
    assert line.ahead_est == 0


def test_a_crossing_book_fills_once_from_the_same_contracts() -> None:
    o = order(size=100)
    line = start(o, B0)
    crossed = book(1, [(0.39, 100)], [(0.40, 10)])
    assert on_book(o, line, crossed) == 10 * U and line.ahead_est == 0
    assert on_book(order(size=100, filled=10), line, crossed) == 0  # the same 10 again
    assert on_book(order(size=100, filled=10), line, book(2, [(0.39, 100)], [(0.40, 25)])) == 15 * U  # 15 new


def test_the_no_side_works_on_the_mirrored_book() -> None:
    # Buying NO at 0.60 rests on the YES asks at 0.40; the YES ask 0.40 × 20 is the line ahead.
    o = order(side="no", price=0.60)
    b = book(0, [(0.38, 50)], [(0.40, 20), (0.42, 30)])
    line = start(o, b)
    assert line.ahead_est == 20 * U
    # Someone buys 25 YES at 0.40: that sells NO at 0.60 into the bids, through the 20 ahead.
    assert on_trade(o, line, trade(1, 0.40, 25, "buy"), b) == 5 * U


def test_a_resting_sell_fills_on_buys_at_its_price() -> None:
    o = order(action="sell", price=0.42, size=10)
    line = start(o, B0)  # 30 at the ask ahead
    assert on_trade(o, line, trade(1, 0.42, 35, "buy"), B0) == 5 * U
    assert on_trade(o, line, trade(2, 0.42, 35, "sell"), B0) == 0


def test_line_round_trips_through_json() -> None:
    line = Line(ahead_est=5, level=7, traded=1, used={400_000: 3}, trade_ids=["a"])
    assert Line.from_json(line.to_json()) == line


# ---- paper mode -----------------------------------------------------------------------------------


def test_regression_resting_order_never_refills_from_the_same_contracts(
    make_client: Any, venue: FakeVenue, clock: Clock
) -> None:
    """Before 11.9/18.7, each poll filled 10 more from the same 10 offered: 10 → 60 after five polls."""
    c = make_client()
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=100, tif="gtc")
    assert (o.status, o.filled) == ("open", 10)  # took the 10 at 0.42 as a taker
    for _ in range(5):
        clock.advance(5)
        c.monitor()
    assert c.orders()[0].filled == 10
    venue.markets["mkt-a"] = FakeMarket("mkt-a", bids=[(0.40, 50)], asks=[(0.42, 25)])  # 15 more arrive
    clock.advance(5)
    c.monitor()
    assert c.orders()[0].filled == 25 and c.fills()[-1].role == "maker"


def test_feed_fills_a_resting_paper_order_from_stream_trades(make_client: Any, clock: Clock) -> None:
    c = make_client()
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.40, size=10, tif="gtc")
    assert o.status == "open"  # 50 already bid at 0.40 are ahead of it
    t = lambda s, n, tid: TradePrint(  # noqa: E731
        venue="polymarket_us",
        market="mkt-a",
        price=0.40,
        size=n,
        aggressor="sell",
        trade_id=tid,
        as_of=clock.now + timedelta(seconds=s),
    )
    assert c.feed(t(1, 45, "a")) == []  # 5 still ahead
    (changed,) = c.feed(t(2, 8, "b"))
    assert (changed.filled, changed.status) == (3, "open")
    assert c.feed(t(2, 8, "b")) == []  # the same trade again
    f = c.fills()[-1]
    assert (f.role, f.price, f.contracts) == ("maker", 0.40, 3)


def test_feed_is_for_paper_mode() -> None:
    with pytest.raises(VenueError) as e:
        Client(mode="backtest", books=[]).feed(trade(1, 0.4, 1, "sell"))
    assert e.value.code == "not_available"


def test_queue_cancels_must_be_known() -> None:
    with pytest.raises(VenueError):
        Client(mode="backtest", books=[], queue_cancels="ahead")  # type: ignore[arg-type]


def test_a_store_from_before_lines_opens_and_its_orders_join_the_line(
    make_client: Any, venue: FakeVenue, clock: Clock, tmp_path: Any
) -> None:
    path = str(tmp_path / "old.db")
    c = make_client(store=path)
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.40, size=10, tif="gtc")
    c.close()
    db = sqlite3.connect(path)
    db.execute("drop table resting_lines")  # what a 0.2.0 / 0.3.0 store looks like
    db.commit()
    db.close()
    c2 = make_client(store=path)
    assert c2.store.line(o.id) is None
    clock.advance(5)
    c2.monitor()  # no line yet: it joins the back of the line from this book, and nothing fills
    assert c2.store.line(o.id) is not None and c2.orders()[0].filled == 0


# ---- backtest -------------------------------------------------------------------------------------


def _replay(events: list[Any], **kw: Any) -> Client:
    def place(c: Client, b: Book) -> None:
        if b.as_of == T0:
            c.buy(venue="polymarket_us", market="m", side="yes", price=0.40, size=10, tif="gtc")

    bt = Client(mode="backtest", books=events, rules={"order_ttl_s": 600}, **kw)
    bt.replay(place)
    return bt


def test_backtest_fills_resting_orders_on_trades_after_the_line() -> None:
    events = [
        B0,
        trade(1, 0.40, 30, "sell", "t1"),  # 20 ahead left
        trade(2, 0.40, 26, "sell", "t2"),  # 6 fill
        book(3, [(0.40, 4)], [(0.42, 30)]),  # 50 − 56 traded: no cancels to place
        trade(4, 0.39, 2, "sell", "t3"),  # through the price: 2 fill
        trade(5, 0.40, 9, "buy", "t4"),  # the other side: nothing
    ]
    bt = _replay(events)
    assert [(f.contracts, f.role, f.price, f.at) for f in bt.fills()] == [
        (6, "maker", 0.40, T0 + timedelta(seconds=2)),
        (2, "maker", 0.40, T0 + timedelta(seconds=4)),
    ]
    assert bt.orders()[0].filled == 8


def test_backtest_worst_case_cancels_fill_later() -> None:
    events = [
        B0,  # 50 ahead
        book(1, [(0.40, 10), (0.39, 100)], [(0.42, 30)]),  # 40 cancelled, no trades
        trade(2, 0.40, 25, "sell", "t1"),
    ]
    # proportional: 40 of the 50 at the level left, all of them ahead → 10 ahead, then 25 trade: 10 fill
    # behind: the 40 left from behind, but only 10 show → 10 ahead all the same
    assert _replay(events).orders(open=False)[0].filled == 10
    events = [
        B0,
        book(1, [(0.40, 80), (0.39, 100)], [(0.42, 30)]),  # 30 join behind
        book(2, [(0.40, 60), (0.39, 100)], [(0.42, 30)]),  # 20 cancelled
        trade(3, 0.40, 48, "sell", "t1"),
    ]
    # proportional: 20 × 50/80 = 12.5 of them ahead → 37.5 ahead; 48 trade → 10 fill (capped)
    # behind: 50 still ahead; 48 trade → nothing
    assert _replay(events).orders(open=False)[0].filled == 10
    assert _replay(events, queue_cancels="behind").orders(open=False)[0].filled == 0


def test_one_trade_fills_your_orders_oldest_first_up_to_its_size() -> None:
    # Seen in the 2026-10-04 proof: one 80-contract sell through two resting buys filled 50 + 50.
    def place(c: Client, b: Book) -> None:
        if b.as_of in (T0, T0 + timedelta(seconds=1)):
            c.buy(venue="polymarket_us", market="m", side="yes", price=0.40, size=50, tif="gtc")

    events = [B0, book(1, [(0.40, 50)], [(0.42, 30)]), trade(2, 0.37, 80, "sell", "t1")]
    bt = Client(mode="backtest", books=events, rules={"order_ttl_s": 600})
    bt.replay(place)
    assert [o.filled for o in bt.orders(open=False)] == [50, 30]


def test_one_offer_fills_your_orders_once_between_them(
    make_client: Any, venue: FakeVenue, clock: Clock
) -> None:
    c = make_client()
    for _ in range(2):
        c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.41, size=10, tif="gtc")
    venue.markets["mkt-a"] = FakeMarket("mkt-a", bids=[(0.39, 50)], asks=[(0.41, 12)])
    for _ in range(3):
        clock.advance(5)
        c.monitor()
    assert [o.filled for o in c.orders(open=False)] == [10, 2]


def test_backtest_trades_before_the_order_dont_count() -> None:
    events = [trade(-1, 0.39, 50, "sell", "early"), B0, trade(0, 0.39, 50, "sell", "same-time")]
    assert _replay(events).fills() == []


# ---- order latency (18.7) -------------------------------------------------------------------------


def _send_at_start(events: list[Any], latency: float, **order: Any) -> Client:
    def place(c: Client, b: Book) -> None:
        if b.as_of == T0:
            c.buy(venue="polymarket_us", market="m", side="yes", **order)

    bt = Client(mode="backtest", books=events, rules={"order_ttl_s": 600}, order_latency_s=latency)
    bt.replay(place)
    return bt


def test_latency_a_trade_before_arrival_cant_fill_and_the_line_is_the_book_at_arrival() -> None:
    events = [
        B0,  # 50 bid at 0.40 when the order is sent
        trade(0.5, 0.39, 20, "sell", "early"),  # through its price, but before it arrives
        book(1, [(0.40, 10), (0.39, 100)], [(0.42, 30)]),  # what it meets at 1 s: 10 ahead
        trade(2, 0.40, 15, "sell", "late"),  # 10 ahead go first, 5 fill
    ]
    order = {"price": 0.40, "size": 10, "tif": "gtc"}
    now = _send_at_start(events, 0.0, **order)
    assert [(f.contracts, f.at) for f in now.fills()] == [(10, T0 + timedelta(seconds=0.5))]
    late = _send_at_start(events, 1.0, **order)
    assert [(f.contracts, f.at) for f in late.fills()] == [(5, T0 + timedelta(seconds=2))]
    assert late.orders(open=False)[0].created_at == T0  # sent then; it reached the book at 1 s


def test_latency_an_ioc_fills_against_the_book_it_reaches() -> None:
    events = [B0, book(0.5, [(0.40, 50)], [(0.42, 3), (0.45, 100)]), book(5, [(0.40, 50)], [(0.42, 30)])]
    order = {"price": 0.42, "size": 10}
    assert _send_at_start(events, 0.0, **order).orders(open=False)[0].filled == 10
    late = _send_at_start(events, 1.0, **order)
    (o,) = late.orders(open=False)
    (f,) = late.fills()
    assert (o.filled, o.status, f.at) == (3, "canceled", T0 + timedelta(seconds=1))


def test_latency_in_paper_waits_and_reads_the_book_again(
    make_client: Any, venue: FakeVenue, clock: Clock, monkeypatch: Any
) -> None:
    def sleep(s: float) -> None:
        clock.advance(s)
        clock.slept.append(s)
        venue.markets["mkt-a"] = FakeMarket("mkt-a", bids=[(0.40, 50)], asks=[(0.42, 4)])  # taken meanwhile

    monkeypatch.setattr(clock, "sleep", sleep)
    c = make_client(order_latency_s=0.7)
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=10)
    assert 0.7 in clock.slept and (o.filled, o.status) == (4, "canceled")


def test_order_latency_must_not_be_negative() -> None:
    with pytest.raises(VenueError):
        Client(mode="backtest", books=[], order_latency_s=-1)
