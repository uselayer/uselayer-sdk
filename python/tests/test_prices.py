"""client.prices(): each leg's best YES and NO bid and ask, in paper, live (reads only) and backtest mode."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_kalshi import FakeKalshi, KMarket, pem
from test_live_polymarket_us import live  # noqa: F401  (the fixture)

from uselayer import Book, Client, Kalshi, LegPrices, Level, Match, Prices, VenueError

PAIR = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]


def test_paper_reads_both_books_and_takes_the_best_of_each_side(make_client: Any, clock: Clock) -> None:
    p = make_client().prices(PAIR)
    a, b = p.legs
    # mkt-a: YES bids 0.40 (50), 0.39 (100); YES asks 0.42 (10), 0.43, 0.45. NO is the mirror.
    assert (a.venue, a.market) == ("polymarket_us", "mkt-a")
    assert (a.yes_bid, a.yes_bid_size, a.yes_ask, a.yes_ask_size) == (0.40, 50, 0.42, 10)
    assert (a.no_bid, a.no_bid_size, a.no_ask, a.no_ask_size) == (0.58, 10, 0.60, 50)
    assert (b.yes_bid, b.yes_ask, b.no_bid, b.no_ask) == (0.60, 0.62, 0.38, 0.40)
    # as_of is the venue's time for the book (its Date header here); read_at is this client's clock.
    assert a.as_of == T0 and a.read_at == clock.now and a.source == "venue"


def test_prices_agree_with_book_and_quote_is_unchanged(make_client: Any) -> None:
    c = make_client()
    p = c.prices(PAIR)
    book = c.book("mkt-a")
    assert p.a.yes_ask == book.outcome("yes").best_ask.price  # type: ignore[union-attr]
    assert p.a.no_ask == book.outcome("no").best_ask.price  # type: ignore[union-attr]
    q = c.quote(PAIR)
    assert q.a is not None and q.a.book_as_of == p.a.as_of


def test_a_match_works_and_leg_finds_a_venue(make_client: Any, venue: FakeVenue) -> None:
    m = Match.model_validate(
        {
            "polymarket_us": {"market_id": "mkt-a"},
            "kalshi": {"market_id": "KX-1"},
            "confidence": 0.99,
        }
    )
    with pytest.raises(VenueError) as e:  # Kalshi books need the developer's Kalshi key
        make_client().prices(m)
    assert e.value.code == "auth_failed"
    p = make_client().prices(PAIR)
    with pytest.raises(KeyError):
        p.leg("polymarket_us")  # both legs are on it
    with pytest.raises(KeyError):
        p.leg("kalshi")


def test_an_empty_side_is_none_with_no_size(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("thin", bids=[], asks=[(0.30, 5)]))
    p = make_client().prices([("polymarket_us", "thin"), ("polymarket_us", "mkt-b")])
    assert (p.a.yes_bid, p.a.yes_bid_size, p.a.no_ask, p.a.no_ask_size) == (None, None, None, None)
    assert (p.a.yes_ask, p.a.no_bid) == (0.30, 0.70)


def test_plain_data_round_trips(make_client: Any) -> None:
    p = make_client().prices(PAIR)
    d = json.loads(json.dumps(p.to_dict()))
    assert Prices.from_dict(d) == p
    assert set(d["a"]) == {
        "venue",
        "market",
        "yes_bid",
        "yes_bid_size",
        "yes_ask",
        "yes_ask_size",
        "no_bid",
        "no_bid_size",
        "no_ask",
        "no_ask_size",
        "as_of",
        "read_at",
        "source",
    }
    assert LegPrices.from_dict(d["b"]) == p.b


def test_kalshi_leg_reads_with_the_developers_key(clock: Clock, venue: FakeVenue, tmp_path: Any) -> None:
    priv = Ed25519PrivateKey.generate()
    k = FakeKalshi(priv.public_key(), clock)
    k.add(KMarket("KXEV-1-A", yes_bids=[(0.40, 10)], no_bids=[(0.55, 7)]))

    def handler(r: httpx.Request) -> httpx.Response:
        return k.handle(r) if r.url.host == "api.elections.kalshi.com" else venue.handler(r)

    c = Client(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
        store=str(tmp_path / "s.db"),
        kalshi=Kalshi(key_id="k", private_key_pem=pem(priv)),
    )
    p = c.prices([("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-a")])
    kal = p.leg("kalshi")
    assert (kal.yes_bid, kal.yes_bid_size, kal.yes_ask, kal.yes_ask_size) == (0.40, 10, 0.45, 7)
    assert (kal.no_bid, kal.no_ask) == (0.55, 0.60)
    assert p.leg("polymarket_us").market == "mkt-a"
    assert all(call.startswith("GET ") for call in k.calls)
    c.close()


def test_live_mode_only_reads(live: Any) -> None:  # noqa: F811
    make, api, _ = live
    c = make()
    before, orders = len(api.signed), dict(api.orders)
    p = c.prices(PAIR)
    assert p.a.yes_ask == 0.42 and p.b.no_ask == 0.40
    assert all(call.startswith("GET ") for call in api.signed[before:])
    assert api.orders == orders  # nothing was sent


def test_backtest_uses_the_replayed_books() -> None:
    def book(market: str, bid: float, ask: float, s: int) -> Book:
        return Book(
            venue="polymarket_us",
            market=market,
            bids=[Level(price=bid, size=3)],
            asks=[Level(price=ask, size=4)],
            as_of=T0 + timedelta(seconds=s),
            source="recorded",
        )

    c = Client(
        mode="backtest", books=[book("x", 0.2, 0.25, 0), book("y", 0.7, 0.75, 1), book("x", 0.3, 0.35, 2)]
    )
    seen: list[Prices] = []

    def on_book(client: Client, b: Book) -> None:
        if b.as_of >= T0 + timedelta(seconds=1):
            seen.append(client.prices([("polymarket_us", "x"), ("polymarket_us", "y")]))

    c.replay(on_book)
    first, last = seen
    assert (first.a.yes_bid, first.a.yes_ask, first.b.yes_ask) == (0.2, 0.25, 0.75)
    assert (last.a.yes_bid, last.a.yes_ask) == (0.3, 0.35)
    assert last.a.source == "recorded" and last.a.as_of == T0 + timedelta(seconds=2)
    assert last.a.read_at == T0 + timedelta(seconds=2)  # the replay's clock
    with pytest.raises(VenueError) as e:
        Client(mode="backtest").prices([("polymarket_us", "x"), ("polymarket_us", "y")])
    assert e.value.code == "stale_quote"
