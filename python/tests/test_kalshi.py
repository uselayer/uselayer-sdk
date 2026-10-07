"""Kalshi, offline: signing with both key types, the order mapping, books, fees, paper fills, pairs with
Polymarket US, and live orders sent with your own key through every guardrail."""

from __future__ import annotations

import base64
import email.utils
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uselayer import Book, Client, Kalshi, Level, Order, Resolution, VenueError, _switches
from uselayer.fees import dollars, kalshi_fee
from uselayer.http import Http
from uselayer.venues.base import Payout
from uselayer.venues.kalshi import KalshiLive, Signer, order_body


def pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


# ---- signing and the order mapping ----


@pytest.mark.parametrize("kind", ["rsa", "ed25519"])
def test_signs_with_either_key_type_over_ts_method_and_prefixed_path(kind: str) -> None:
    priv: Any = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if kind == "rsa"
        else ed25519.Ed25519PrivateKey.generate()
    )
    key = Kalshi(key_id="k", private_key_pem=pem(priv))
    s = Signer(key, clock_ms=lambda: 1700000000123)
    assert s.kind == kind
    h = s.headers("GET", "/trade-api/v2/portfolio/balance")
    msg = b"1700000000123GET/trade-api/v2/portfolio/balance"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    if kind == "rsa":
        pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
        priv.public_key().verify(sig, msg, pss, hashes.SHA256())
    else:
        priv.public_key().verify(sig, msg)
    assert h["KALSHI-ACCESS-KEY"] == "k" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000123"
    assert pem(priv) not in repr(key)


@pytest.mark.parametrize(
    ("side", "action", "price", "want_side", "want_price"),
    [
        ("yes", "buy", 0.42, "bid", "0.4200"),
        ("yes", "sell", 0.42, "ask", "0.4200"),
        ("no", "buy", 0.55, "ask", "0.4500"),
        ("no", "sell", 0.123, "bid", "0.8770"),
    ],
)
def test_orders_are_quoted_from_the_yes_side(
    side: str, action: str, price: float, want_side: str, want_price: str
) -> None:
    o = Order(venue="kalshi", market="KX-1", side=side, action=action, price=price, size=2.5)  # type: ignore[arg-type]
    b = order_body(o)
    assert (b["side"], b["price"], b["count"], b["time_in_force"]) == (
        want_side,
        want_price,
        "2.50",
        "immediate_or_cancel",
    )
    assert b["self_trade_prevention_type"] == "taker_at_cross" and b["cancel_order_on_pause"] is True
    assert b["client_order_id"] == o.client_id and "expiration_time" not in b


def test_gtc_orders_carry_an_expiry_in_unix_seconds() -> None:
    o = Order(
        venue="kalshi", market="KX-1", side="yes", price=0.1, size=1, tif="gtc", post_only=True, expires_at=T0
    )
    b = order_body(o)
    assert (b["time_in_force"], b["expiration_time"], b["post_only"]) == (
        "good_till_canceled",
        int(T0.timestamp()),
        True,
    )


def test_key_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("KALSHI_KEY_ID", "k")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "~/k.pem")
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    assert Kalshi.from_env() == Kalshi(key_id="k", private_key_path="~/k.pem")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH")
    with pytest.raises(VenueError) as e:
        Kalshi.from_env()
    assert e.value.code == "auth_failed"
    with pytest.raises(VenueError) as e2:
        Kalshi(key_id="k", private_key_path="/nonexistent/k.pem").load()
    assert e2.value.code == "auth_failed" and "/nonexistent/k.pem" in e2.value.message


# ---- a fake Kalshi ----


@dataclass
class KMarket:
    ticker: str
    yes_bids: list[tuple[float, float]]
    no_bids: list[tuple[float, float]]
    event: str = "KXEV-1"
    status: str = "active"
    step: str = "0.0100"
    result: str = ""
    settlement_value: str | None = None
    settlement_ts: str | None = None


@dataclass
class FakeKalshi:
    """Kalshi's trade API: checks every signature, answers markets, events, series, books and orders."""

    pub: Any
    clock: Clock
    markets: dict[str, KMarket] = field(default_factory=dict)
    series: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"KXEV": {"ticker": "KXEV", "fee_type": "quadratic", "fee_multiplier": 1}}
    )
    calls: list[str] = field(default_factory=list)
    fail_create: int = 0
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: list[dict[str, Any]] = field(default_factory=list)
    positions_lag: int = 0  # how many position reads still miss the latest fill
    settled: set[str] = field(default_factory=set)  # tickers whose positions read as settled

    def add(self, m: KMarket) -> KMarket:
        self.markets[m.ticker] = m
        return m

    def _market(self, m: KMarket) -> dict[str, Any]:
        return {
            "ticker": m.ticker,
            "event_ticker": m.event,
            "title": f"Q {m.ticker}",
            "status": m.status,
            "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": m.step}],
            "close_time": "2026-12-31T00:00:00Z",
            "result": m.result,
            "settlement_value_dollars": m.settlement_value,
            "settlement_ts": m.settlement_ts,
        }

    def handle(self, r: httpx.Request) -> httpx.Response:
        h = r.headers
        self.pub.verify(
            base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
            f"{h['KALSHI-ACCESS-TIMESTAMP']}{r.method}{r.url.path}".encode(),
        )
        assert r.url.path.startswith("/trade-api/v2/")
        p = r.url.path[len("/trade-api/v2") :]
        self.calls.append(f"{r.method} {p}")
        date = {"date": email.utils.format_datetime(self.clock.now, usegmt=True)}
        if p == "/markets":
            ms = [self._market(m) for m in self.markets.values() if m.status == "active"]
            start = int(r.url.params.get("cursor") or 0)
            n = int(r.url.params["limit"])
            page = ms[start : start + n]
            cursor = str(start + n) if start + n < len(ms) else ""
            return httpx.Response(200, json={"markets": page, "cursor": cursor})
        if p.startswith("/markets/") and p.endswith("/orderbook"):
            m = self.markets.get(p.split("/")[2])
            if m is None:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "no market"}})
            fp = lambda xs: [[f"{a:.4f}", f"{b:.2f}"] for a, b in xs]  # noqa: E731
            body = {"orderbook_fp": {"yes_dollars": fp(m.yes_bids), "no_dollars": fp(m.no_bids)}}
            return httpx.Response(200, json=body, headers=date)
        if p.startswith("/markets/"):
            m = self.markets.get(p.split("/")[2])
            if m is None:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "no market"}})
            return httpx.Response(200, json={"market": self._market(m)})
        if p.startswith("/events/"):
            return httpx.Response(200, json={"event": {"series_ticker": p.split("/")[2].split("-")[0]}})
        if p.startswith("/series/"):
            return httpx.Response(200, json={"series": self.series[p.split("/")[2]]})
        if p == "/portfolio/events/orders" and r.method == "POST":
            body = json.loads(r.content)
            if self.fail_create:
                self.fail_create -= 1
                self.orders["o1"] = {
                    "order_id": "o1",
                    "client_order_id": body["client_order_id"],
                    "status": "resting",
                    "fill_count_fp": "0.00",
                    "remaining_count_fp": body["count"],
                }
                return httpx.Response(500, json={"code": "internal", "message": "x"})
            return httpx.Response(
                201,
                json={
                    "order_id": "o2",
                    "client_order_id": body["client_order_id"],
                    "fill_count": body["count"],
                    "remaining_count": "0.00",
                },
            )
        if p == "/portfolio/fills":
            fill = {
                "fill_id": "f1",
                "order_id": "o2",
                "count_fp": "2.00",
                "yes_price_dollars": "0.4500",
                "no_price_dollars": "0.5500",
                "is_taker": True,
                "fee_cost": "0.04",
                "ts": int(T0.timestamp()),
            }
            return httpx.Response(200, json={"fills": [fill]})
        if p == "/portfolio/orders":
            return httpx.Response(200, json={"orders": list(self.orders.values())})
        if p.startswith("/portfolio/events/orders") and r.method == "DELETE":
            return httpx.Response(200, json={})
        if p in ("/portfolio/positions", "/historical/positions"):
            rows = self.positions
            if r.url.params.get("settlement_status") == "unsettled":
                rows = [x for x in rows if x.get("ticker") not in self.settled]
            if p == "/portfolio/positions" and self.positions_lag:
                self.positions_lag -= 1
                rows = []
            return httpx.Response(200, json={"market_positions": rows, "cursor": ""})
        if p == "/portfolio/balance":
            return httpx.Response(200, json={"balance_dollars": "100.0000", "portfolio_value": 0})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no"}})


@pytest.fixture
def kpriv() -> Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture
def kalshi(kpriv: Ed25519PrivateKey, clock: Clock) -> FakeKalshi:
    k = FakeKalshi(kpriv.public_key(), clock)
    # YES bids 0.40 (10) and 0.41 (5); NO bids 0.60 (100) and 0.59 (50), so YES asks 0.40 and 0.41.
    k.add(KMarket("KXEV-1-A", yes_bids=[(0.40, 10), (0.41, 5)], no_bids=[(0.60, 100), (0.59, 50)]))
    return k


@pytest.fixture
def key(kpriv: Ed25519PrivateKey) -> Kalshi:
    return Kalshi(key_id="k", private_key_pem=pem(kpriv))


@pytest.fixture
def adapter(kalshi: FakeKalshi, key: Kalshi, clock: Clock) -> KalshiLive:
    http = Http(transport=httpx.MockTransport(kalshi.handle), sleep=lambda s: None)
    return KalshiLive(http, key, clock=clock, sleep=lambda s: None)


@pytest.fixture
def both(venue: FakeVenue, kalshi: FakeKalshi) -> httpx.MockTransport:
    def handler(r: httpx.Request) -> httpx.Response:
        return kalshi.handle(r) if r.url.host == "api.elections.kalshi.com" else venue.handler(r)

    return httpx.MockTransport(handler)


@pytest.fixture
def make(both: httpx.MockTransport, key: Kalshi, clock: Clock, tmp_path: Any) -> Any:
    made: list[Client] = []

    def mk(**kw: Any) -> Client:
        kw.setdefault("store", str(tmp_path / f"k-{len(made)}.db"))
        kw.setdefault("kalshi", key)
        c = Client(transport=both, clock=clock, sleep=clock.sleep, **kw)
        made.append(c)
        return c

    yield mk
    for c in made:
        c.close()


# ---- the adapter ----


def test_book_reads_yes_bids_and_no_bids_as_yes_asks(adapter: KalshiLive) -> None:
    r = adapter.read_book("KXEV-1-A")
    assert [(lv.price, lv.size) for lv in r.book.bids] == [(0.41, 5.0), (0.40, 10.0)]
    assert [(lv.price, lv.size) for lv in r.book.asks] == [(0.40, 100.0), (0.41, 50.0)]
    assert r.book.as_of == T0


def test_market_reads_tick_min_size_and_series_fees_once_per_event(
    adapter: KalshiLive, kalshi: FakeKalshi
) -> None:
    kalshi.series["KXEV"] = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}
    kalshi.add(KMarket("KXEV-1-B", [], [], step="0.0010"))
    a, b = adapter.market("KXEV-1-A"), adapter.market("KXEV-1-B")
    assert (a.tick_size, a.min_size, a.open) == (0.01, 0.01, True)
    assert (b.tick_size, b.min_size) == (0.001, 0.01)
    assert (a.fees.fee_type, a.fees.multiplier) == ("quadratic_with_maker_fees", 0.5)
    assert kalshi.calls.count("GET /series/KXEV") == 1
    with pytest.raises(VenueError) as e:
        adapter.market("NOPE")
    assert e.value.code == "not_found" and "ticker" in (e.value.hint or "")


def test_markets_follow_the_cursor_for_offset_and_limit(adapter: KalshiLive, kalshi: FakeKalshi) -> None:
    for i in range(5):
        kalshi.add(KMarket(f"KXEV-1-M{i}", [], []))
    got = [m.market for m in adapter.markets(limit=3, offset=2)]
    assert got == ["KXEV-1-M1", "KXEV-1-M2", "KXEV-1-M3"]


def test_writes_refuse_if_a_release_switches_kalshi_trading_off(
    adapter: KalshiLive, kalshi: FakeKalshi, monkeypatch: Any
) -> None:
    # The switch is still read on every write, so a release can turn Kalshi off again.
    monkeypatch.setitem(_switches.TRADING, "kalshi", False)
    o = Order(venue="kalshi", market="KXEV-1-A", side="yes", price=0.4, size=1)
    for call in (
        lambda: adapter.place(o),
        lambda: adapter.cancel(o.model_copy(update={"venue_order_id": "o9"})),
        adapter.cancel_all,
    ):
        with pytest.raises(VenueError) as e:
            call()
        assert e.value.code == "venue_switched_off" and "paper" in (e.value.hint or "")
    assert not [c for c in kalshi.calls if not c.startswith("GET")]


def test_a_filled_order_reads_its_real_fills_and_fees(adapter: KalshiLive, kalshi: FakeKalshi) -> None:
    o = Order(venue="kalshi", market="KXEV-1-A", side="no", price=0.55, size=2)
    placed, fills = adapter.place(o)
    assert (placed.status, placed.filled, placed.avg_price, placed.venue_order_id) == (
        "filled",
        2.0,
        0.55,
        "o2",
    )
    assert (fills[0].price, fills[0].fee, fills[0].venue_fill_id, fills[0].simulated) == (
        0.55,
        0.04,
        "f1",
        False,
    )
    assert kalshi.calls[:2] == ["POST /portfolio/events/orders", "GET /portfolio/fills"]


def test_after_a_5xx_the_order_is_found_by_client_id_never_resent(
    adapter: KalshiLive, kalshi: FakeKalshi
) -> None:
    kalshi.fail_create = 1
    o = Order(
        venue="kalshi",
        market="KXEV-1-A",
        side="yes",
        price=0.1,
        size=1,
        tif="gtc",
        expires_at=T0 + timedelta(minutes=1),
    )
    with pytest.raises(VenueError) as e:
        adapter.place(o)
    assert e.value.code == "outcome_unknown"
    found = adapter.find(o, since=T0)
    assert found is not None and (found.venue_order_id, found.status) == ("o1", "open")
    assert kalshi.calls.count("POST /portfolio/events/orders") == 1


# ---- the client: paper, backtest, pairs ----


def test_paper_buy_on_kalshi_fills_against_its_book_with_kalshis_fee(make: Any, kalshi: FakeKalshi) -> None:
    c = make()
    o = c.buy(venue="kalshi", market="KXEV-1-A", side="yes", price=0.41, size=120)
    # 100 at 0.40 and 20 at 0.41; Kalshi rounds each level's fee up to the cent.
    fee_40 = dollars(kalshi_fee(contracts=100, price=0.40, rate=0.07, multiplier=1))  # 0.07×100×.4×.6 = 1.68
    fee_41 = dollars(kalshi_fee(contracts=20, price=0.41, rate=0.07, multiplier=1))  # 0.33866 → 0.34
    assert (fee_40, fee_41) == (1.68, 0.34)
    assert (o.status, o.filled, o.avg_price) == ("filled", 120.0, round((40 + 8.2) / 120, 6))
    assert o.fees == pytest.approx(fee_40 + fee_41)
    fills = c.fills()
    assert [(f.price, f.contracts, f.fee, f.simulated) for f in fills] == [
        (0.40, 100.0, 1.68, True),
        (0.41, 20.0, 0.34, True),
    ]
    # Buying NO takes the YES bids, turned round: NO asks 0.59 (5) and 0.60 (10).
    no = c.buy(venue="kalshi", market="KXEV-1-A", side="no", price=0.60, size=15)
    assert (no.filled, no.avg_price) == (15.0, round((0.59 * 5 + 0.60 * 10) / 15, 6))
    # Kalshi takes fractional contracts down to 0.01, and nothing smaller.
    half = c.buy(venue="kalshi", market="KXEV-1-A", side="yes", price=0.41, size=0.5)
    assert half.filled == 0.5
    with pytest.raises(VenueError) as e:
        c.buy(venue="kalshi", market="KXEV-1-A", side="yes", price=0.41, size=0.005)
    assert e.value.code == "invalid_order"
    assert not [x for x in kalshi.calls if not x.startswith("GET")], "paper never writes to Kalshi"


def test_series_fee_multiplier_and_maker_fees_apply_in_paper(make: Any, kalshi: FakeKalshi) -> None:
    kalshi.series["KXEV"] = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 2}
    c = make()
    p = c.preview(c.order(venue="kalshi", market="KXEV-1-A", side="yes", price=0.40, size=100))
    assert p.fees == dollars(kalshi_fee(contracts=100, price=0.40, rate=0.07, multiplier=2)) == 3.36


def test_pair_kalshi_and_polymarket_us_runs_in_paper_with_the_leg_risk_guard(
    make: Any, venue: FakeVenue, kalshi: FakeKalshi
) -> None:
    # Kalshi YES asks 0.40; Polymarket US mkt-c NO asks 0.45 (YES bid 0.55): 0.85 + fees < $1.
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    c = make()
    pair = [("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")]
    q = c.quote(pair, size=50)
    assert q.a is not None and q.b is not None
    assert (q.a.venue, q.a.side, q.a.best_price) == ("kalshi", "yes", 0.40)
    assert (q.b.venue, q.b.side, q.b.best_price) == ("polymarket_us", "no", 0.45)
    assert q.contracts == 50 and q.net_profit_per_contract > 0.1
    t = c.trade(pair, size=50, min_edge=0.01)
    assert t.status == "hedged" and t.hedged == 50
    legs = {(o.venue, o.side): o for o in t.orders}
    assert legs[("kalshi", "yes")].filled == 50 and legs[("polymarket_us", "no")].filled == 50
    assert {p.venue for p in c.positions()} == {"kalshi", "polymarket_us"}
    assert not [x for x in kalshi.calls if not x.startswith("GET")]


@pytest.mark.parametrize("fee_type", ["quadratic", "quadratic_with_maker_fees"])
def test_pair_quote_gross_spread_minus_fees_is_net_profit_on_a_kalshi_maker_fee_series(
    make: Any, venue: FakeVenue, kalshi: FakeKalshi, fee_type: str
) -> None:
    kalshi.series["KXEV"] = {"fee_type": fee_type, "fee_multiplier": 2}
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    q = make().quote([("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")], size=50)
    assert q.a is not None and q.b is not None
    assert q.gross_at_best == 0.15  # 1 − 0.40 − 0.45
    assert q.gross_spread == 7.5 and q.gross_spread_per_contract == 0.15
    assert q.a.fee == dollars(
        kalshi_fee(contracts=50, price=0.40, rate=0.07, multiplier=2)
    )  # takers pay 0.07
    assert round(q.gross_spread - q.fees, 6) == q.net_profit


def cached_until_waited(venue: FakeVenue, clock: Clock, age_s: float = 12, then: Any = None) -> None:
    """Polymarket US serves a cached book ``age_s`` old until the SDK waits for a fresh copy (then fresh)."""
    venue.cache_age_s = age_s
    real = clock.sleep

    def sleep(s: float) -> None:
        real(s)
        if venue.cache_age_s:
            venue.cache_age_s = 0
            if then:
                then()

    clock.sleep = sleep  # type: ignore[method-assign]


def test_pair_rereads_the_kalshi_book_after_waiting_for_a_fresh_polymarket_us_copy(
    make: Any, venue: FakeVenue, kalshi: FakeKalshi, clock: Clock
) -> None:
    # Kalshi is read first; Polymarket US's cached copy is 12 s old, so the SDK waits 18.5 s for a
    # fresh one. The Kalshi book is then 18.5 s old, past max_quote_age_s (10 s), and is read again.
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    cached_until_waited(venue, clock)
    c = make()
    pair = [("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")]
    t = c.trade(pair, size=50, min_edge=0.01)
    assert clock.slept[0] == 18.5
    assert t.status == "hedged" and t.hedged == 50
    assert kalshi.calls.count("GET /markets/KXEV-1-A/orderbook") >= 2
    assert all((clock.now - lg.book_as_of).total_seconds() <= 10 for lg in (t.quote.a, t.quote.b))  # type: ignore[union-attr]


def test_pair_quote_has_both_books_fresh_together(
    make: Any, venue: FakeVenue, kalshi: FakeKalshi, clock: Clock
) -> None:
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    cached_until_waited(venue, clock)
    q = make().quote([("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")], size=50)
    assert q.a is not None and q.b is not None
    assert all((clock.now - lg.book_as_of).total_seconds() <= 10 for lg in (q.a, q.b))
    assert q.contracts == 50


def test_pair_still_refuses_a_book_that_never_comes_back_fresh(
    make: Any, venue: FakeVenue, kalshi: FakeKalshi, clock: Clock
) -> None:
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    venue.cache_age_s = 12  # every copy Polymarket US serves is 12 s old
    c = make()
    with pytest.raises(VenueError) as e:
        c.trade([("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")], size=50, min_edge=0.01)
    assert e.value.code == "stale_quote" and e.value.venue == "polymarket_us"
    assert not [x for x in kalshi.calls if not x.startswith("GET")]


def test_fresh_books_returns_a_failed_read_as_its_error(make: Any, venue: FakeVenue) -> None:
    a, b = make()._fresh_books([("kalshi", "KXEV-1-A"), ("polymarket_us", "no-such-market")])
    assert isinstance(a, Book) and isinstance(b, VenueError)


def test_pair_leg_risk_guard_unwinds_a_kalshi_leg_when_the_other_side_is_gone(
    make: Any, venue: FakeVenue, kalshi: FakeKalshi, clock: Clock
) -> None:
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    kalshi.markets["KXEV-1-A"].yes_bids = [(0.39, 100)]  # room to sell the Kalshi leg back
    c = make()
    pair = [("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")]
    q = c.quote(pair, size=50)
    assert q.contracts == 50
    # The Polymarket US side (deeper) goes second; it vanishes after the Kalshi leg fills.
    real = c._execute

    def first_then_pull(o: Order, **kw: Any) -> Order:
        done = real(o, **kw)
        if o.venue == "kalshi" and o.action == "buy":
            venue.markets["mkt-c"].bids = []
        return done

    c._execute = first_then_pull  # type: ignore[method-assign]
    t = c.trade(pair, size=50, min_edge=0.01, chase_s=1.0)
    assert t.status == "unwound" and t.hedged == 0
    sells = [o for o in t.orders if o.action == "sell"]
    assert sells and sells[0].venue == "kalshi" and sells[0].reason == "unwind" and sells[0].filled == 50


def test_backtest_replays_kalshi_books_without_a_key(tmp_path: Any) -> None:
    books = [
        Book(
            venue="kalshi",
            market="KX-1",
            bids=(Level(price=0.39, size=10),),
            asks=(Level(price=0.41, size=10),),
            as_of=T0,
        ),
        Book(
            venue="kalshi",
            market="KX-1",
            bids=(Level(price=0.40, size=10),),
            asks=(Level(price=0.42, size=10),),
            as_of=T0 + timedelta(seconds=5),
        ),
    ]
    bt = Client(mode="backtest", books=books, kalshi=None)
    got: list[Order] = []
    bt.replay(
        lambda c, b: got.append(
            c.buy(venue="kalshi", market="KX-1", side="yes", price=b.asks[0].price, size=2)
        )
    )
    assert [(o.filled, o.avg_price) for o in got] == [(2.0, 0.41), (2.0, 0.42)]
    assert got[0].fees == dollars(kalshi_fee(contracts=2, price=0.41, rate=0.07, multiplier=1)) == 0.04


# ---- live refuses Kalshi orders ----


@pytest.fixture
def live(kalshi: FakeKalshi, key: Kalshi, clock: Clock, tmp_path: Any) -> Any:
    """A live-mode client with only a Kalshi key: no Polymarket US key needed."""
    made: list[Client] = []

    def mk(**kw: Any) -> Client:
        def handler(r: httpx.Request) -> httpx.Response:
            assert r.url.host == "api.elections.kalshi.com", r.url
            return kalshi.handle(r)

        kw.setdefault("on_alert", lambda e: None)
        c = Client(
            mode="live",
            kalshi=key,
            transport=httpx.MockTransport(handler),
            clock=clock,
            sleep=clock.sleep,
            store=str(tmp_path / f"live-{len(made)}.db"),
            **kw,
        )
        made.append(c)
        return c

    yield mk
    for c in made:
        c.close()


def test_live_kalshi_order_is_sent_with_your_key_after_the_rules(
    live: Any, kalshi: FakeKalshi, monkeypatch: Any
) -> None:
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    c = live()
    assert c.balances()["kalshi"].cash == 100.0
    o = c.buy(venue="kalshi", market="KXEV-1-A", side="no", price=0.59, size=2)
    assert (o.mode, o.status, o.filled, o.venue_order_id) == ("live", "filled", 2.0, "o2")
    f = c.fills()[0]
    assert (f.venue, f.price, f.fee, f.simulated) == ("kalshi", 0.55, 0.04, False)
    assert kalshi.calls.count("POST /portfolio/events/orders") == 1
    assert c.decisions()[0]["result"] == "final:allow"


def test_live_kalshi_orders_meet_every_guardrail(live: Any, kalshi: FakeKalshi) -> None:
    c = live(rules={"max_position": {"per_market": 1}})
    o = c.order(venue="kalshi", market="KXEV-1-A", side="no", price=0.59, size=2)
    assert c.preview(o).blocked_by == "max_position"
    with pytest.raises(VenueError) as e:
        c.send(o)
    assert (e.value.code, e.value.rule) == ("blocked_by_rule", "max_position")
    # The always-on price collar: a limit more than 5¢ past the best ask is refused too.
    with pytest.raises(VenueError) as e2:
        c.buy(venue="kalshi", market="KXEV-1-A", side="no", price=0.70, size=1)
    assert (e2.value.code, e2.value.rule) == ("blocked_by_rule", "price_collar")
    assert "POST /portfolio/events/orders" not in kalshi.calls


def test_positions_wait_for_kalshi_to_catch_up_with_a_fill(live: Any, kalshi: FakeKalshi) -> None:
    c = live()
    c.buy(venue="kalshi", market="KXEV-1-A", side="no", price=0.59, size=2)
    kalshi.positions = [
        {
            "ticker": "KXEV-1-A",
            "position_fp": "-2.00",
            "market_exposure_dollars": "1.1000",
            "last_updated_ts": T0.isoformat().replace("+00:00", ".5Z"),
        }
    ]
    kalshi.positions_lag = 4  # two reads of open + all positions answer without the new fill
    ps = c.positions()
    assert [(p.market, p.side, p.contracts) for p in ps] == [("KXEV-1-A", "no", 2.0)]
    assert kalshi.positions_lag == 0
    # Once caught up, it reads once.
    n = kalshi.calls.count("GET /portfolio/positions")
    c.positions()
    assert kalshi.calls.count("GET /portfolio/positions") == n + 2


def test_a_kalshi_fill_before_the_cancel_landed_is_recorded(live: Any, kalshi: FakeKalshi) -> None:
    c = live()
    kalshi.fail_create = 1  # the answer is lost; find() reads the order back, resting with nothing filled
    o = c.buy(venue="kalshi", market="KXEV-1-A", side="no", price=0.59, size=5, tif="gtc")
    assert (o.venue_order_id, o.status, o.filled) == ("o1", "open", 0)
    kalshi.orders["o1"].update(fill_count_fp="2.00", remaining_count_fp="3.00")  # 2 fill, then the cancel
    canceled = c.cancel(o)
    assert (canceled.status, canceled.filled) == ("canceled", 2)
    assert [(f.venue_fill_id, f.contracts) for f in c.fills()] == [("f1", 2)]  # type: ignore[union-attr]


def test_kill_switch_cancels_on_kalshi_and_blocks_new_live_orders(live: Any, kalshi: FakeKalshi) -> None:
    c = live()
    c.kill()
    assert "DELETE /portfolio/events/orders" in kalshi.calls and c.killed
    with pytest.raises(VenueError) as e:
        c.buy(venue="kalshi", market="KXEV-1-A", side="no", price=0.59, size=1)
    assert (e.value.code, e.value.rule) == ("blocked_by_rule", "kill_switch")
    assert "POST /portfolio/events/orders" not in kalshi.calls


def test_sandbox_mode_is_gone(make: Any) -> None:
    with pytest.raises(VenueError) as e:
        make(mode="sandbox", layer_key="lyr_test")
    assert e.value.code == "not_available" and "removed in uselayer 0.3.0" in e.value.message
    assert e.value.next == "Client(mode='paper')"


def test_layer_never_sees_kalshi_traffic(make: Any, venue: FakeVenue, kalshi: FakeKalshi) -> None:
    venue.add(FakeMarket("mkt-c", bids=[(0.55, 200)], asks=[(0.57, 200)]))
    c = make()
    c.trade([("kalshi", "KXEV-1-A"), ("polymarket_us", "mkt-c")], size=10, min_edge=0.01)
    assert not [r for r in venue.requests if r.url.host == "uselayer.sh"]


def test_every_kalshi_golden_fee_case_through_the_paper_fee_path() -> None:
    """Paper and backtest bill Kalshi fills with calculate_fee: all of fee-golden.json's Kalshi cases agree."""
    from pathlib import Path

    from uselayer.fill import FeeSettings, calculate_fee

    golden = json.loads((Path(__file__).resolve().parents[2] / "fee-golden.json").read_text())
    now = datetime.fromisoformat(golden["now"].replace("Z", "+00:00"))
    fee_type = {
        0.0: "quadratic",
        0.0175: "quadratic_with_maker_fees",
        0.035: "quadratic_with_combo_maker_fees",
    }
    cases = [c for c in golden["fees"] if c["venue"] == "kalshi"]
    assert len(cases) == 305
    for c in cases:
        role = "taker" if c["rate"] == 0.07 else "maker"
        settings = FeeSettings(
            venue="kalshi", multiplier=c["multiplier"], fee_type=fee_type.get(c["rate"], "quadratic")
        )
        got = calculate_fee(settings, contracts=c["contracts"], price=c["price"], role=role, at=now)
        assert got == int(c["fee_micro"]), c


# ---- settlement and P&L ----


def test_payout_waits_for_finalized_and_reads_the_fair_price(adapter: KalshiLive, kalshi: FakeKalshi) -> None:
    m = kalshi.markets["KXEV-1-A"]
    assert adapter.payout(m.ticker) is None
    m.status, m.result = "determined", "yes"
    assert adapter.payout(m.ticker) is None  # a determined result can still be disputed
    m.status = "finalized"
    assert adapter.payout(m.ticker) == Payout(1.0)
    m.result, m.settlement_ts = "no", "2026-10-01T23:58:00Z"
    assert adapter.payout(m.ticker) == Payout(0.0, datetime(2026, 10, 1, 23, 58, tzinfo=UTC))
    m.result, m.settlement_value = "scalar", "0.3700"  # a fair price for a canceled game
    assert adapter.payout(m.ticker).yes == 0.37  # type: ignore[union-attr]


def test_paper_kalshi_position_settles_from_kalshi(make: Any, kalshi: FakeKalshi, clock: Clock) -> None:
    c = make()
    c.buy(
        venue="kalshi", market="KXEV-1-A", side="yes", price=0.40, size=5
    )  # 2.00; fee .09 (0.07×5×.4×.6 = .084, up)
    p = c.pnl()
    assert (p.rows[0].mark, p.rows[0].unrealized, p.fees) == (0.41, 0.05, 0.09)  # 5 × .41 − 2.00
    m = kalshi.markets["KXEV-1-A"]
    m.status, m.result = "finalized", "no"
    c.settle()
    (r,) = c.pnl().rows
    assert (r.outcome, r.contracts, r.realized, r.fees, r.net) == ("no", 0, -2.0, 0.09, -2.09)
    assert not [x for x in kalshi.calls if not x.startswith("GET")], "paper never writes to Kalshi"


def test_live_kalshi_pnl_is_what_kalshi_reports(live: Any, kalshi: FakeKalshi) -> None:
    kalshi.positions = [
        {
            "ticker": "KXEV-1-A",
            "position_fp": "5.00",
            "market_exposure_dollars": "2.0000",
            "realized_pnl_dollars": "0.5000",
            "fees_paid_dollars": "0.0900",
            "last_updated_ts": "2026-10-01T11:00:00Z",
        },
        {  # closed out: no contracts, but its realized P&L counts
            "ticker": "KXEV-1-B",
            "position_fp": "0.00",
            "market_exposure_dollars": "0.0000",
            "realized_pnl_dollars": "-1.2000",
            "fees_paid_dollars": "0.0400",
            "last_updated_ts": "2026-10-01T11:00:00Z",
        },
        {  # settled
            "ticker": "KXEV-1-C",
            "position_fp": "3.00",
            "market_exposure_dollars": "1.5000",
            "realized_pnl_dollars": "1.5000",
            "fees_paid_dollars": "0.0300",
            "last_updated_ts": "2026-10-01T11:00:00Z",
        },
    ]
    kalshi.settled = {"KXEV-1-C"}
    c = live()
    assert [p.market for p in c.positions()] == ["KXEV-1-A"]
    p = c.pnl()
    a, b, s = p.rows
    assert (a.contracts, a.mark, a.unrealized, a.realized, a.fees) == (
        5,
        0.41,
        0.05,
        0.5,
        0.09,
    )  # 5 × .41 − 2.00
    assert (b.contracts, b.unrealized, b.realized, b.fees) == (0, None, -1.2, 0.04)
    assert (s.contracts, s.settled, s.unrealized, s.realized) == (0, True, None, 1.5)
    assert p.missing_marks == ()
    assert (p.realized, p.unrealized, p.fees, p.net) == (0.8, 0.05, 0.16, 0.69)
    with pytest.raises(VenueError):
        c.settle([Resolution(venue="kalshi", market="KXEV-1-A", outcome="yes", as_of=T0)])


def test_a_payout_counts_on_the_day_kalshi_settled_not_the_day_it_was_noticed(
    make: Any, kalshi: FakeKalshi, clock: Clock
) -> None:
    clock.now = datetime(2026, 10, 1, 23, 50, tzinfo=UTC)
    c = make()  # days start at midnight UTC
    c.buy(venue="kalshi", market="KXEV-1-A", side="yes", price=0.40, size=5)  # 2.00; fee .09
    m = kalshi.markets["KXEV-1-A"]
    m.status, m.result, m.settlement_ts = "finalized", "no", "2026-10-01T23:58:00Z"
    clock.now = datetime(2026, 10, 2, 0, 5, tzinfo=UTC)  # noticed after midnight
    (s,) = c.settle()
    assert s.at == datetime(2026, 10, 1, 23, 58, tzinfo=UTC)
    ctx = c._context(None)
    assert (ctx.realized_today, ctx.pnl_today) == (0, 0)  # the −2.00 belongs to Oct 1, not today
    assert c.pnl().net == -2.09  # all-time P&L is unchanged


def test_a_payout_is_never_dated_before_the_positions_last_fill(
    make: Any, kalshi: FakeKalshi, clock: Clock
) -> None:
    c = make()
    c.buy(venue="kalshi", market="KXEV-1-A", side="yes", price=0.40, size=5)
    m = kalshi.markets["KXEV-1-A"]
    m.status, m.result, m.settlement_ts = "finalized", "yes", "2026-09-01T00:00:00Z"
    (s,) = c.settle()
    assert s.at == clock.now and c.positions() == [] and c.pnl().rows[0].realized == 3.0
