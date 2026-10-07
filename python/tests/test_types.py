from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import T0
from pydantic import ValidationError

from uselayer import Book, BookLevelChange, Fill, Level, Order, SimulatedFill, reconstruct_book
from uselayer.orders import order_schema

SCHEMA = Path(__file__).resolve().parents[2] / "schema" / "order.json"


def test_published_schema_is_the_models_schema() -> None:
    assert json.loads(SCHEMA.read_text()) == order_schema(), "run: python scripts/write_schema.py"


def test_orders_have_a_limit_price_and_safe_defaults() -> None:
    o = Order(venue="polymarket_us", market="m", side="yes", price=0.42, size=10)
    assert (o.action, o.tif, o.reason, o.post_only, o.status) == ("buy", "ioc", "open", False, None)
    assert o.to_dict()["client_id"] == o.client_id
    with pytest.raises(ValidationError):
        Order(venue="polymarket_us", market="m", side="yes", price=1.0, size=1)
    with pytest.raises(ValidationError):
        Order(venue="polymarket_us", market="m", side="yes", price=0.4, size=1, expires_at=T0)  # gtc only
    with pytest.raises(ValidationError):
        Order(venue="polymarket_us", market="m", side="yes", price=0.4, size=1, post_only=True)  # must rest
    with pytest.raises(ValidationError):
        Order(venue="nasdaq", market="m", side="yes", price=0.4, size=1)  # type: ignore[arg-type]


def test_no_side_mirrors_the_yes_book() -> None:
    b = Book(
        venue="v", market="m", bids=(Level(price=0.4, size=5),), asks=(Level(price=0.42, size=7),), as_of=T0
    )
    no = b.outcome("no")
    assert (no.best_ask.price, no.best_ask.size) == (0.6, 5)  # type: ignore[union-attr]
    assert (no.best_bid.price, no.best_bid.size) == (0.58, 7)  # type: ignore[union-attr]


def test_reconstruct_book_applies_level_changes() -> None:
    b = Book(
        venue="v", market="m", bids=(Level(price=0.4, size=5),), asks=(Level(price=0.42, size=7),), as_of=T0
    )
    ch = [
        BookLevelChange(venue="v", market="m", book_side="ask", price=0.41, size=3, as_of=T0),
        BookLevelChange(venue="v", market="m", book_side="bid", price=0.4, size=0, as_of=T0),
        BookLevelChange(venue="v", market="other", book_side="ask", price=0.1, size=3, as_of=T0),
    ]
    r = reconstruct_book([b, *ch])
    assert r is not None and [lv.price for lv in r.asks] == [0.41, 0.42] and r.bids == ()
    assert reconstruct_book(ch) is None


def test_simulated_fills_can_never_pass_as_real_fills() -> None:
    sim = SimulatedFill(
        mode="paper",
        venue="v",
        market="m",
        order_id="o",
        side="yes",
        action="buy",
        price=0.4,
        contracts=1,
        role="taker",
        cost=0.4,
        fee=0.02,
        at=T0,
        book_as_of=T0,
    )
    assert sim.simulated is True and sim.to_dict()["kind"] == "simulated_fill"
    assert not isinstance(sim, Fill)
    with pytest.raises(ValidationError):
        Fill.model_validate(sim.to_dict())
