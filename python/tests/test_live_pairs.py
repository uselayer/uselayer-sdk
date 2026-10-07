"""Live Kalshi ↔ Polymarket US pairs: trade() in live mode, every leg-risk branch, against fake venues.

Both fakes check every request's signature. Kalshi fills immediate-or-cancel orders against its book;
Polymarket US is the fake API from test_live_polymarket_us. No real venue is ever contacted.

The pair: YES on Kalshi KXEV-1-P (YES asks 0.40 × 100, YES bid 0.38) and NO on Polymarket US mkt-c
(YES bids 0.55, so NO asks 0.45). $1 at settlement for $0.85 before fees.
"""

from __future__ import annotations

import base64
import email.utils
import itertools
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from conftest import Clock, FakeMarket, FakeVenue
from cryptography.hazmat.primitives.asymmetric import ed25519
from test_kalshi import FakeKalshi, KMarket, pem
from test_live_polymarket_us import FakeApi, FakeWs, keypair

from uselayer import Admin, Client, Kalshi, Order, VenueError
from uselayer.trading import trade_pair

PAIR = [("kalshi", "KXEV-1-P"), ("polymarket_us", "mkt-c")]


@dataclass
class BookKalshi(FakeKalshi):
    """FakeKalshi whose immediate-or-cancel orders fill against the market's book, with their own fills."""

    order_fills: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    fail_next_order: int | None = None
    posted: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, r: httpx.Request) -> httpx.Response:
        p = r.url.path[len("/trade-api/v2") :]
        if not (p in ("/portfolio/events/orders", "/portfolio/fills") and r.method in ("POST", "GET")):
            return super().handle(r)
        h = r.headers
        self.pub.verify(
            base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
            f"{h['KALSHI-ACCESS-TIMESTAMP']}{r.method}{r.url.path}".encode(),
        )
        self.calls.append(f"{r.method} {p}")
        if p == "/portfolio/fills":
            return httpx.Response(200, json={"fills": self.order_fills.get(r.url.params["order_id"], [])})
        body = json.loads(r.content)
        self.posted.append(body)
        if self.fail_next_order:
            code, self.fail_next_order = self.fail_next_order, None
            return httpx.Response(code, json={"error": {"code": "invalid_order", "message": "rejected"}})
        m = self.markets[body["ticker"]]
        yes_px, count = float(body["price"]), float(body["count"])
        if body["side"] == "bid":  # buy YES (or sell NO): takes YES asks, which are 1 − NO bids
            levels = [(round(1 - p, 4), s) for p, s in m.no_bids if 1 - p <= yes_px + 1e-9]
            fill_yes = min((p for p, _ in levels), default=yes_px)
        else:  # sell YES (or buy NO): hits YES bids
            levels = [(p, s) for p, s in m.yes_bids if p >= yes_px - 1e-9]
            fill_yes = max((p for p, _ in levels), default=yes_px)
        filled = min(count, sum(s for _, s in levels))
        oid = f"k{next(self.ids)}"
        if filled:
            self.order_fills[oid] = [
                {
                    "fill_id": f"f{oid}",
                    "order_id": oid,
                    "count_fp": f"{filled:.2f}",
                    "yes_price_dollars": f"{fill_yes:.4f}",
                    "no_price_dollars": f"{1 - fill_yes:.4f}",
                    "is_taker": True,
                    "fee_cost": "0.02",
                    "ts": int(self.clock.now.timestamp()),
                }
            ]
        return httpx.Response(
            201,
            json={
                "order_id": oid,
                "client_order_id": body["client_order_id"],
                "fill_count": f"{filled:.2f}",
                "remaining_count": f"{count - filled:.2f}",
            },
            headers={"date": email.utils.format_datetime(self.clock.now, usegmt=True)},
        )


@pytest.fixture
def kalshi_venue(clock: Clock) -> tuple[BookKalshi, Kalshi]:
    priv = ed25519.Ed25519PrivateKey.generate()
    k = BookKalshi(priv.public_key(), clock)
    k.add(KMarket("KXEV-1-P", yes_bids=[(0.38, 100)], no_bids=[(0.60, 100)]))
    return k, Kalshi(key_id="k", private_key_pem=pem(priv))


@pytest.fixture
def pair_live(
    venue: FakeVenue, clock: Clock, tmp_path: Any, kalshi_venue: tuple[BookKalshi, Kalshi]
) -> Iterator[Any]:
    """A live client with both keys: Kalshi and Polymarket US, each a fake that checks signatures."""
    kalshi, kkey = kalshi_venue
    pkey, pub = keypair()
    api = FakeApi(venue, pub)
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    alerts: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "api.elections.kalshi.com":
            return kalshi.handle(r)
        if r.url.host == "api.polymarket.us":
            return api.handle(r)
        return venue.handler(r)

    made: list[Client] = []

    def make(*, keys: tuple[str, ...] = ("kalshi", "polymarket_us"), **kw: Any) -> Client:
        kw.setdefault("store", str(tmp_path / f"pair-{len(made)}.db"))
        c = Client(
            mode="live",
            kalshi=kkey if "kalshi" in keys else None,
            polymarket_us=pkey if "polymarket_us" in keys else None,
            transport=httpx.MockTransport(handler),
            clock=clock,
            sleep=clock.sleep,
            ws_connect=FakeWs(venue, pub, []),
            on_alert=alerts.append,
            **kw,
        )
        made.append(c)
        return c

    yield make, kalshi, api, alerts
    for c in made:
        c.close()


def after_first_leg(c: Client, fn: Any) -> None:
    """Run ``fn`` right after the pair's first buy fills (to change a venue mid-trade)."""
    orig = c._execute
    state = {"done": False}

    def wrapped(order: Order, checked: bool = False) -> Order:
        r = orig(order, checked=checked)
        if not state["done"] and order.action == "buy" and r.filled:
            state["done"] = True
            fn(r)
        return r

    c._execute = wrapped  # type: ignore[method-assign]


def posts(kalshi: BookKalshi, api: FakeApi) -> tuple[int, int]:
    return kalshi.calls.count("POST /portfolio/events/orders"), api.signed.count("POST /v1/orders")


def test_a_live_pair_is_hedged_with_real_orders_on_both_venues(pair_live: Any) -> None:
    make, kalshi, api, _ = pair_live
    c = make()
    t = c.trade(PAIR, size=50, min_edge=0.01)
    assert (t.status, t.hedged, t.exposure) == ("hedged", 50, None)
    # Kalshi is thinner up to its limit (100 vs 200), so it goes first.
    assert [(o.venue, o.side, o.mode, o.filled) for o in t.orders] == [
        ("kalshi", "yes", "live", 50),
        ("polymarket_us", "no", "live", 50),
    ]
    assert posts(kalshi, api) == (1, 1)
    assert kalshi.posted[0]["time_in_force"] == "immediate_or_cancel"
    assert t.locked_in == round(50 - 0.40 * 50 - 0.45 * 50 - 0.02 - 0.01, 6)
    assert {(f.venue, f.simulated) for f in c.fills()} == {("kalshi", False), ("polymarket_us", False)}
    assert all(o.group_id == t.group_id for o in t.orders)


def test_polymarket_us_first_when_it_is_thinner(pair_live: Any, venue: FakeVenue) -> None:
    make, kalshi, api, _ = pair_live
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 5)], asks=[(0.57, 200)]))
    t = make().trade(PAIR, size=5, min_edge=0.01)
    assert t.status == "hedged" and [o.venue for o in t.orders] == ["polymarket_us", "kalshi"]
    assert posts(kalshi, api) == (1, 1)


def test_a_live_pair_needs_both_keys_before_anything_is_sent(pair_live: Any) -> None:
    make, kalshi, api, _ = pair_live
    c = make(keys=("kalshi",))
    with pytest.raises(VenueError) as e:
        c.trade(PAIR, size=5)
    assert (e.value.code, e.value.venue) == ("auth_failed", "polymarket_us") and "Nothing was sent" in (
        e.value.hint or ""
    )
    assert posts(kalshi, api) == (0, 0)


@pytest.mark.parametrize(
    "rules",
    [
        {"max_position": {"per_market": 21}},  # the Polymarket US leg is $22.50
        {"budget": 30},
        {"markets": ["KXEV-1-P"]},  # Polymarket US market not allowed
        {"venues": ["kalshi"]},
    ],
)
def test_guardrails_check_both_live_legs_before_the_first_is_sent(
    pair_live: Any, rules: dict[str, Any]
) -> None:
    make, kalshi, api, _ = pair_live
    c = make(rules=rules)
    with pytest.raises(VenueError) as e:
        c.trade(PAIR, size=50, min_edge=0.01)
    assert e.value.code == "blocked_by_rule"
    assert posts(kalshi, api) == (0, 0) and c.fills() == []


def test_a_live_pair_over_approve_above_is_asked_once_and_refusal_sends_nothing(pair_live: Any) -> None:
    make, kalshi, api, _ = pair_live
    asked: list[str] = []
    c = make(rules={"approve_above": 10}, on_approval=lambda o, r: asked.append(r) or False)
    with pytest.raises(VenueError):
        c.trade(PAIR, size=50, min_edge=0.01)
    assert len(asked) == 1 and asked[0].startswith("pair:") and posts(kalshi, api) == (0, 0)


def test_polymarket_us_misses_and_the_kalshi_leg_is_unwound_live(pair_live: Any, venue: FakeVenue) -> None:
    make, kalshi, api, _ = pair_live
    c = make()
    after_first_leg(c, lambda _: setattr(venue.markets["mkt-c"], "bids", []))
    t = c.trade(PAIR, size=50, min_edge=0.01, chase_s=1.0)
    assert (t.status, t.hedged) == ("unwound", 0)
    unwind = t.orders[-1]
    assert (unwind.venue, unwind.action, unwind.reason, unwind.filled) == ("kalshi", "sell", "unwind", 50)
    assert kalshi.posted[-1]["side"] == "ask"  # sold back into Kalshi's YES bid
    assert t.unwind_loss == round((0.40 - 0.38) * 50 + 0.02 + 0.02, 6)
    assert api.signed.count("POST /v1/orders") == 0  # nothing to buy on Polymarket US


def test_kalshi_misses_and_the_polymarket_us_leg_is_unwound_live(
    pair_live: Any, venue: FakeVenue, kalshi_venue: tuple[BookKalshi, Kalshi]
) -> None:
    make, kalshi, _api, _ = pair_live
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 5)], asks=[(0.57, 200)]))
    c = make()
    after_first_leg(c, lambda _: setattr(kalshi.markets["KXEV-1-P"], "no_bids", []))
    t = c.trade(PAIR, size=5, min_edge=0.01, chase_s=1.0)
    assert t.status == "unwound"
    unwind = t.orders[-1]
    assert (unwind.venue, unwind.side, unwind.action, unwind.filled) == ("polymarket_us", "no", "sell", 5)
    assert kalshi.calls.count("POST /portfolio/events/orders") == 0


def test_kill_mid_pair_stops_the_second_leg_and_still_unwinds_live(pair_live: Any, tmp_path: Any) -> None:
    make, _kalshi, api, _ = pair_live
    store = str(tmp_path / "kill.db")
    c = make(store=store)
    after_first_leg(c, lambda _: Admin(mode="live", store=store).kill())
    t = c.trade(PAIR, size=50, min_edge=0.01)
    assert t.status == "unwound" and "kill switch pressed: second leg not sent" in t.notes
    assert [(o.venue, o.reason) for o in t.orders] == [("kalshi", "open"), ("kalshi", "unwind")]
    assert api.signed.count("POST /v1/orders") == 0


def test_an_unwind_the_venue_rejects_is_reported_as_exposure(pair_live: Any, venue: FakeVenue) -> None:
    make, kalshi, _api, alerts = pair_live
    c = make()

    def pull_and_break(_: Order) -> None:
        venue.markets["mkt-c"].bids = []
        kalshi.fail_next_order = 400

    after_first_leg(c, pull_and_break)
    t = c.trade(PAIR, size=50, min_edge=0.01, chase_s=0.5)
    assert t.status == "exposed" and t.exposure is not None
    assert (t.exposure.venue, t.exposure.contracts, t.exposure.avg_price) == ("kalshi", 50, 0.40)
    assert any(n.startswith("unwind: ") for n in t.notes)
    assert next(a for a in alerts if a["kind"] == "exposed")["exposure"]["contracts"] == 50


def test_a_rejected_second_leg_is_unwound_not_left_open(pair_live: Any) -> None:
    make, _kalshi, api, _ = pair_live
    c = make()
    after_first_leg(c, lambda _: setattr(api, "fail_next_order", 400))
    t = c.trade(PAIR, size=50, min_edge=0.01, chase_s=1.0)
    assert t.status == "unwound" and any(n.startswith("second leg: ") for n in t.notes)
    assert [(o.venue, o.reason) for o in t.orders] == [("kalshi", "open"), ("kalshi", "unwind")]


def test_a_retryable_second_leg_error_is_chased_through(pair_live: Any) -> None:
    make, _kalshi, api, _ = pair_live
    c = make()
    after_first_leg(c, lambda _: setattr(api, "fail_next_order", 503))  # Polymarket US maintenance
    t = c.trade(PAIR, size=50, min_edge=0.01, chase_s=1.0)
    assert (t.status, t.hedged) == ("hedged", 50)
    assert any(n.startswith("second leg: venue_maintenance") for n in t.notes)
    assert api.signed.count("POST /v1/orders") == 2


def test_an_unknown_second_leg_is_never_unwound_and_says_sync(pair_live: Any) -> None:
    make, kalshi, api, alerts = pair_live
    c = make()
    after_first_leg(c, lambda _: setattr(api, "fail_next_order", 500))
    t = c.trade(PAIR, size=50, min_edge=0.01, chase_s=1.0)
    assert t.status == "exposed" and t.exposure is not None and t.exposure.venue == "kalshi"
    assert "not unwound: a second-leg order's outcome is unknown" in t.notes
    assert any("client.sync()" in n for n in t.notes)
    assert kalshi.calls.count("POST /portfolio/events/orders") == 1  # no unwind sent
    assert api.signed.count("POST /v1/orders") == 1  # and the unknown order was never resent
    assert {a["kind"] for a in alerts} >= {"outcome_unknown", "exposed"}


def test_a_switched_off_venue_refuses_a_live_pair_before_sending(pair_live: Any, monkeypatch: Any) -> None:
    from uselayer import _switches

    make, kalshi, api, _ = pair_live
    c = make()
    monkeypatch.setitem(_switches.TRADING, "kalshi", False)  # type: ignore[index]
    with pytest.raises(VenueError) as e:
        trade_pair(c, PAIR, size=5, min_edge=0.01, on_miss="unwind", max_unwind_loss=0.05, chase_s=0)
    assert e.value.code == "venue_switched_off" and posts(kalshi, api) == (0, 0)
