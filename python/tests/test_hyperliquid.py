"""Hyperliquid's outcome markets: read through a fake info API, paper-traded through the client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from conftest import Clock

from uselayer import Client
from uselayer.venues.hyperliquid import fields, question

BTC = {
    "outcome": 14726,
    "name": "Recurring",
    "description": "class:priceBinary|underlying:BTC|expiry:20261021-0600|targetPrice:81697|period:1d",
    "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
    "quoteToken": "USDC",
}
SPORTS = {
    "outcome": 14200,
    "name": "template:sportsContestParticipant2",
    "description": "participant:Lyon",
    "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
    "quoteToken": "USDC",
}


class FakeHyperliquid:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.outcomes: list[dict[str, Any]] = [BTC, SPORTS]
        # YES book of each outcome: (price, size) levels, best first.
        self.books: dict[int, tuple[list[tuple[float, float]], list[tuple[float, float]]]] = {
            14726: ([(0.863, 1000), (0.862, 50)], [(0.87817, 15), (0.88198, 30), (0.8825, 65)]),
        }
        self.bodies: list[dict[str, Any]] = []
        self.settled: dict[int, str] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.hyperliquid.xyz" and request.url.path == "/info"
        body = json.loads(request.content)
        self.bodies.append(body)
        if body["type"] == "outcomeMeta":
            return httpx.Response(
                200, json={"outcomes": self.outcomes, "questions": [], "deployers": [], "feeScale": "1.0"}
            )
        if body["type"] == "l2Book":
            oc, side = divmod(int(body["coin"][1:]), 10)
            if oc not in self.books:
                return httpx.Response(200, json=None)
            bids, asks = self.books[oc]
            if side == 1:
                bids, asks = [(round(1 - p, 5), s) for p, s in asks], [(round(1 - p, 5), s) for p, s in bids]
            lv = lambda p, s: {"px": f"{p}", "sz": f"{s:.1f}", "n": 1}  # noqa: E731
            return httpx.Response(
                200,
                json={
                    "coin": body["coin"],
                    "time": int(self.clock.now.timestamp() * 1000) - 1500,
                    "levels": [[lv(p, s) for p, s in bids], [lv(p, s) for p, s in asks]],
                },
            )
        if body["type"] == "settledOutcome":
            oc = body["outcome"]
            if oc not in self.settled:
                return httpx.Response(200, json=None)
            spec = next(o for o in [BTC, SPORTS] if o["outcome"] == oc)
            return httpx.Response(
                200, json={"spec": spec, "settleFraction": self.settled[oc], "details": "price:82719"}
            )
        return httpx.Response(422, text="Failed to deserialize the JSON body into the target type")


# Hyperliquid's outcome fees are recorded from 2026-10-20, so these tests run on that day.
T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def hl(clock: Clock) -> FakeHyperliquid:
    return FakeHyperliquid(clock)


@pytest.fixture
def client(hl: FakeHyperliquid, clock: Clock, tmp_path: Any) -> Any:
    c = Client(
        transport=httpx.MockTransport(hl.handle), clock=clock, sleep=clock.sleep, store=str(tmp_path / "s.db")
    )
    yield c
    c.close()


def test_fields_and_question() -> None:
    assert fields(BTC["description"])["targetPrice"] == "81697"
    assert question(BTC) == "BTC at or above $81,697?"
    assert question(SPORTS) is None


def test_markets_lists_every_outcome_with_its_settle_time(client: Any) -> None:
    ms = {m.market: m for m in client.markets(venue="hyperliquid")}
    assert set(ms) == {"14726", "14200"}
    btc = ms["14726"]
    assert btc.question == "BTC at or above $81,697?"
    assert btc.event_time == datetime(2026, 10, 21, 6, 0, tzinfo=UTC).isoformat()
    assert btc.open and btc.venue == "hyperliquid"


def test_meta_is_read_once_for_many_market_calls(client: Any, hl: FakeHyperliquid) -> None:
    client.markets(venue="hyperliquid")
    client.market("14726", venue="hyperliquid")
    assert sum(1 for b in hl.bodies if b["type"] == "outcomeMeta") == 1


def test_book_reads_the_yes_coin_and_no_is_its_mirror(client: Any, hl: FakeHyperliquid) -> None:
    book = client.book("14726", venue="hyperliquid")
    assert hl.bodies[-1] == {"type": "l2Book", "coin": "#147260"}
    assert book.outcome("yes").best_ask.price == 0.87817
    assert book.outcome("yes").best_bid.price == 0.863
    no = book.outcome("no")
    assert no.best_ask.price == pytest.approx(0.137) and no.best_ask.size == 1000
    assert no.best_bid.price == pytest.approx(0.12183) and no.best_bid.size == 15
    assert book.as_of == datetime.fromtimestamp((int(T0.timestamp() * 1000) - 1500) / 1000, tz=UTC)


def test_unknown_outcome_is_not_found(client: Any) -> None:
    from uselayer import VenueError

    with pytest.raises(VenueError) as e:
        client.market("99999", venue="hyperliquid")
    assert e.value.code == "not_found"


def test_paper_buy_walks_the_asks_with_no_fee(client: Any) -> None:
    order = client.buy(venue="hyperliquid", market="14726", side="yes", price=0.88198, size=40, tif="ioc")
    assert order.filled == 40
    assert order.fees == 0  # Hyperliquid bills outcome buys nothing
    assert order.avg_price == pytest.approx((15 * 0.87817 + 25 * 0.88198) / 40, abs=1e-6)


def test_paper_sell_pays_the_taker_rate_on_what_it_sells(client: Any) -> None:
    client.buy(venue="hyperliquid", market="14726", side="no", price=0.137, size=100, tif="ioc")
    sold = client.sell(venue="hyperliquid", market="14726", side="no", price=0.12, size=100, tif="ioc")
    assert sold.filled == 15
    # NO bids are 1 − the YES asks: 15 at 0.12183, then 30 at 0.11802 is below the 0.12 limit.
    fills = [f for f in client.fills() if f.action == "sell"]
    assert sum(f.contracts for f in fills) == 15
    assert sum(f.fee for f in fills) == pytest.approx(0.0007 * 15 * 0.12183, abs=1e-6)


def test_paper_position_settles_from_settled_outcome(client: Any, hl: FakeHyperliquid, clock: Clock) -> None:
    client.buy(venue="hyperliquid", market="14726", side="yes", price=0.87817, size=10, tif="ioc")
    assert client.settle() == []  # not settled yet: settledOutcome answers null
    hl.settled[14726] = "1.0"
    clock.advance(120)
    paid = client.settle()
    assert [(s.outcome, s.payout, s.proceeds) for s in paid] == [("yes", 1.0, 10.0)]
    assert not [p for p in client.positions() if p.contracts]


def test_deployer_markets_bill_scale_plus_max_scale_one() -> None:
    from uselayer.venues.hyperliquid import market_info

    assert market_info(BTC).fees.multiplier == 1.0
    assert market_info({**SPORTS, "deployerFeeScale": "1.0"}).fees.multiplier == 2.0
    assert market_info({**SPORTS, "deployerFeeScale": "0.5"}).fees.multiplier == 1.5


def test_targets_are_never_rounded() -> None:
    eth = {
        **BTC,
        "description": "class:priceBinary|underlying:ETH|expiry:20261021-0600|targetPrice:2395.7|period:1d",
    }
    hype = {
        **BTC,
        "description": "class:priceBinary|underlying:HYPE|expiry:20261021-0600|targetPrice:78.658|period:1d",
    }
    assert question(eth) == "ETH at or above $2,395.7?"
    assert question(hype) == "HYPE at or above $78.658?"
