from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from conftest import T0

from uselayer import Book, Client, Level, VenueError
from uselayer.backtest import layer_history, load_books, save_events


def books() -> list[Book]:
    def b(s: int, bid: float, ask: float) -> Book:
        return Book(
            venue="polymarket_us",
            market="m",
            bids=(Level(price=bid, size=50),),
            asks=(Level(price=ask, size=50),),
            as_of=T0 + timedelta(seconds=s),
        )

    return [b(0, 0.40, 0.42), b(60, 0.38, 0.40), b(120, 0.50, 0.52)]


def test_backtest_replays_through_the_same_fill_model_and_rules() -> None:
    seen: list[float] = []

    def on_book(c: Client, book: Book) -> None:
        seen.append(book.outcome("yes").best_ask.price)  # type: ignore[union-attr]
        if len(seen) == 1:
            c.buy(venue="polymarket_us", market="m", side="yes", price=0.40, size=10, tif="gtc")

    bt = Client(mode="backtest", books=books(), rules={"order_ttl_s": 300})
    out = bt.replay(on_book)
    assert seen == [0.42, 0.40, 0.52] and out["books"] == 3
    (f,) = bt.fills()
    assert (f.role, f.price, f.mode, f.at) == ("maker", 0.40, "backtest", T0 + timedelta(seconds=60))
    assert bt.positions()[0].contracts == 10


def test_backtest_rules_use_the_replayed_clock() -> None:
    bt = Client(
        mode="backtest", books=books(), rules={"expires_at": (T0 + timedelta(seconds=30)).isoformat()}
    )
    results: list[str] = []

    def on_book(c: Client, book: Book) -> None:
        try:
            c.buy(venue="polymarket_us", market="m", side="yes", price=book.asks[0].price, size=1)
            results.append("ok")
        except VenueError as e:
            results.append(e.rule or e.code)

    bt.replay(on_book)
    assert results == ["ok", "expires_at", "expires_at"]


def test_saved_books_round_trip(tmp_path: Any) -> None:
    p = tmp_path / "books.jsonl"
    assert save_events(books(), p) == 3
    loaded = load_books(p)
    assert [b.as_of for b in loaded] == [b.as_of for b in books()] and all(
        b.source == "recorded" for b in loaded
    )


def test_layer_history_is_switched_off() -> None:
    with pytest.raises(VenueError) as e:
        list(layer_history("m", start=T0, end=T0))
    assert e.value.code == "not_available"
