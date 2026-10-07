"""Live mode against a fake Polymarket US API that checks every request's Ed25519 signature."""

from __future__ import annotations

import base64
import email.utils
import itertools
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import Clock, FakeMarket, FakeVenue
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from uselayer import Client, Fill, Order, PolymarketUS, VenueError
from uselayer.portfolio import build
from uselayer.venues.polymarket_us_live import order_body


def keypair(sixty_four: bool = False) -> tuple[PolymarketUS, Ed25519PublicKey]:
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes_raw()
    pub = k.public_key()
    secret = seed + pub.public_bytes(Encoding.Raw, PublicFormat.Raw) if sixty_four else seed
    return PolymarketUS(key_id="key-1", secret_key=base64.b64encode(secret).decode()), pub


@dataclass
class FakeApi:
    """api.polymarket.us: orders against the FakeVenue's books, positions and a balance."""

    venue: FakeVenue
    pub: Ed25519PublicKey
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    fail_next_order: int | None = None
    record_failed_order: bool = False
    positions: dict[str, Any] = field(default_factory=dict)
    activities: list[dict[str, Any]] = field(default_factory=list)
    balance_extra: dict[str, Any] = field(default_factory=dict)
    signed: list[str] = field(default_factory=list)

    def _verify(self, r: httpx.Request) -> None:
        ts, sig = r.headers["X-PM-Timestamp"], base64.b64decode(r.headers["X-PM-Signature"])
        assert r.headers["X-PM-Access-Key"] == "key-1"
        self.pub.verify(sig, f"{ts}{r.method}{r.url.path}".encode())  # raises if wrong
        self.signed.append(f"{r.method} {r.url.path}")

    def handle(self, r: httpx.Request) -> httpx.Response:
        self._verify(r)
        path, body = r.url.path, json.loads(r.content or b"{}")
        if r.method == "POST" and path == "/v1/orders":
            oid = f"o{next(self.ids)}"
            if self.fail_next_order:
                code, self.fail_next_order = self.fail_next_order, None
                if self.record_failed_order:
                    self.orders[oid] = self._new(oid, body, filled=0, state="ORDER_STATE_NEW")
                return httpx.Response(code, json={"message": "upstream"})
            m = self.venue.markets[body["marketSlug"]]
            yes_px, qty = float(body["price"]["value"]), float(body["quantity"])
            if body["intent"] in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT"):
                avail = sum(s for p, s in m.asks if p <= yes_px + 1e-9)
                fill_px = min((p for p, _ in m.asks), default=yes_px)
            else:
                avail = sum(s for p, s in m.bids if p >= yes_px - 1e-9)
                fill_px = max((p for p, _ in m.bids), default=yes_px)
            crossing = avail > 0
            if body.get("participateDontInitiate") and crossing:
                o = self._new(oid, body, filled=0, state="ORDER_STATE_REJECTED")
                return httpx.Response(
                    200,
                    json={
                        "id": oid,
                        "executions": [
                            {"type": "EXECUTION_TYPE_REJECTED", "order": o, "text": "would cross"}
                        ],
                    },
                )
            filled = min(qty, avail) if body["tif"] != "TIME_IN_FORCE_GOOD_TILL_DATE" or crossing else 0
            if body["tif"] == "TIME_IN_FORCE_FILL_OR_KILL" and filled < qty:
                filled = 0
            if body["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE":
                state = "ORDER_STATE_FILLED" if filled >= qty else "ORDER_STATE_NEW"
            else:
                state = "ORDER_STATE_FILLED" if filled >= qty else "ORDER_STATE_CANCELED"
            o = self._new(oid, body, filled=filled, state=state, avg=fill_px if filled else None)
            self.orders[oid] = o
            ex = (
                [
                    {
                        "id": f"x{oid}",
                        "type": "EXECUTION_TYPE_FILL",
                        "order": o,
                        "lastShares": str(filled),
                        "lastPx": {"value": str(fill_px), "currency": "USD"},
                        "tradeId": f"t{oid}",
                        "aggressor": True,
                        "commissionNotionalCollected": {"value": "0.01", "currency": "USD"},
                    }
                ]
                if filled
                else []
            )
            if not body.get("synchronousExecution"):
                ex = []
            return httpx.Response(200, json={"id": oid, "executions": ex})
        if r.method == "GET" and path.startswith("/v1/order/"):
            o = self.orders.get(path.split("/")[3])
            return (
                httpx.Response(200, json={"order": o}) if o else httpx.Response(404, json={"message": "no"})
            )
        if r.method == "POST" and path.endswith("/cancel") and path.startswith("/v1/order/"):
            self.orders[path.split("/")[3]]["state"] = "ORDER_STATE_CANCELED"
            assert body == {"marketSlug": self.orders[path.split("/")[3]]["marketSlug"]}
            return httpx.Response(200, json={})
        if r.method == "POST" and path == "/v1/orders/open/cancel":
            ids = [i for i, o in self.orders.items() if o["state"] == "ORDER_STATE_NEW"]
            for i in ids:
                self.orders[i]["state"] = "ORDER_STATE_CANCELED"
            return httpx.Response(200, json={"canceledOrderIds": ids})
        if r.method == "GET" and path == "/v1/orders/open":
            return httpx.Response(
                200, json={"orders": [o for o in self.orders.values() if o["state"] == "ORDER_STATE_NEW"]}
            )
        if r.method == "GET" and path == "/v1/portfolio/positions":
            return httpx.Response(200, json={"positions": self.positions, "eof": True})
        if r.method == "GET" and path == "/v1/portfolio/activities":
            return httpx.Response(200, json={"activities": self.activities, "eof": True})
        if r.method == "GET" and path == "/v1/account/balances":
            return httpx.Response(
                200,
                json={
                    "balances": [
                        {
                            "currency": "USD",
                            "currentBalance": 100.5,
                            "openOrders": 2.0,
                            "assetNotional": 7.25,
                            **self.balance_extra,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"message": "unknown"})

    def _new(
        self, oid: str, body: dict[str, Any], *, filled: float, state: str, avg: float | None = None
    ) -> dict[str, Any]:
        return {
            "id": oid,
            "marketSlug": body["marketSlug"],
            "intent": body["intent"],
            "price": body["price"],
            "quantity": body["quantity"],
            "cumQuantity": filled,
            "leavesQuantity": float(body["quantity"]) - filled,
            "state": state,
            "avgPx": {"value": str(avg), "currency": "USD"} if avg else None,
            "createTime": "2026-10-01T12:00:00Z",
            "tif": body["tif"],
            "goodTillTime": body.get("goodTillTime"),
        }


class FakeWs:
    """Stands in for websockets' sync connect(): answers a subscribe with the FakeVenue's book."""

    def __init__(self, venue: FakeVenue, pub: Ed25519PublicKey, seen: list[dict[str, str]]) -> None:
        self.venue, self.pub, self.seen = venue, pub, seen

    def __call__(self, url: str, *, additional_headers: dict[str, str], **_: Any) -> FakeWs:
        h = additional_headers
        self.pub.verify(
            base64.b64decode(h["X-PM-Signature"]), f"{h['X-PM-Timestamp']}GET/v1/ws/markets".encode()
        )
        self.seen.append(h)
        self.response = type(
            "R", (), {"headers": {"Date": email.utils.format_datetime(self.venue.clock.now, usegmt=True)}}
        )()
        self.slug = ""
        return self

    def __enter__(self) -> FakeWs:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def send(self, msg: str) -> None:
        sub = json.loads(msg)["subscribe"]
        assert sub["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
        self.slug = sub["marketSlugs"][0]

    def recv(self, timeout: float | None = None) -> str:
        m = self.venue.markets[self.slug]
        lv = lambda p, q: {"px": {"value": str(p), "currency": "USD"}, "qty": str(q)}  # noqa: E731
        return json.dumps(
            {
                "marketData": {
                    "marketSlug": self.slug,
                    "bids": [lv(*b) for b in m.bids],
                    "offers": [lv(*a) for a in m.asks],
                    "state": m.state,
                    "transactTime": "2026-10-01T11:00:00Z",
                }
            }
        )


@pytest.fixture
def live(venue: FakeVenue, clock: Clock, tmp_path: Any) -> Iterator[Any]:
    key, pub = keypair()
    api = FakeApi(venue, pub)
    seen: list[dict[str, str]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        return api.handle(r) if r.url.host == "api.polymarket.us" else venue.handler(r)

    made: list[Client] = []

    def make(**kw: Any) -> Client:
        kw.setdefault("store", str(tmp_path / f"live-{len(made)}.db"))
        c = Client(
            mode="live",
            polymarket_us=key,
            transport=httpx.MockTransport(handler),
            clock=clock,
            sleep=clock.sleep,
            ws_connect=FakeWs(venue, pub, seen),
            **kw,
        )
        made.append(c)
        return c

    yield make, api, seen
    for c in made:
        c.close()


def test_secret_key_works_as_32_or_64_bytes_and_is_never_printed() -> None:
    for sixty_four in (False, True):
        key, pub = keypair(sixty_four)
        from uselayer.venues.polymarket_us_live import Signer

        h = Signer(key, clock_ms=lambda: 1700000000000).headers("GET", "/v1/account/balances")
        pub.verify(base64.b64decode(h["X-PM-Signature"]), b"1700000000000GET/v1/account/balances")
        assert key.secret_key and key.secret_key not in repr(key)


@pytest.mark.parametrize(
    ("market", "side", "action", "price", "intent", "yes_price"),
    [
        ("m", "yes", "buy", 0.42, "ORDER_INTENT_BUY_LONG", "0.42"),
        ("m", "no", "buy", 0.55, "ORDER_INTENT_BUY_SHORT", "0.45"),
        ("m", "yes", "sell", 0.4, "ORDER_INTENT_SELL_LONG", "0.4"),
        ("m", "no", "sell", 0.123, "ORDER_INTENT_SELL_SHORT", "0.877"),
        ("m:short", "yes", "buy", 0.17, "ORDER_INTENT_BUY_SHORT", "0.83"),
        ("m:long", "yes", "buy", 0.83, "ORDER_INTENT_BUY_LONG", "0.83"),
    ],
)
def test_orders_map_to_intents_and_yes_prices(
    market: str, side: str, action: str, price: float, intent: str, yes_price: str
) -> None:
    b = order_body(Order(venue="polymarket_us", market=market, side=side, action=action, price=price, size=3))  # type: ignore[arg-type]
    assert (b["marketSlug"], b["intent"], b["price"], b["type"]) == (
        "m",
        intent,
        {"value": yes_price, "currency": "USD"},
        "ORDER_TYPE_LIMIT",
    )
    assert (
        b["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC" and b["synchronousExecution"] is True
    )
    assert b["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and b["quantity"] == 3


def test_gtc_orders_expire_on_their_own_and_post_only_never_takes(clock: Clock) -> None:
    o = Order(
        venue="polymarket_us",
        market="m",
        side="yes",
        price=0.4,
        size=1,
        tif="gtc",
        post_only=True,
        expires_at=clock.now,
    )
    b = order_body(o)
    assert b["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE" and b["goodTillTime"] == "2026-10-01T12:00:00Z"
    assert b["participateDontInitiate"] is True and "synchronousExecution" not in b


def test_a_live_ioc_buy_is_signed_sent_and_recorded_as_a_real_fill(live: Any) -> None:
    make, api, seen = live
    c = make()
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=5)
    assert (o.status, o.filled, o.avg_price, o.venue_order_id, o.mode) == ("filled", 5, 0.42, "o1", "live")
    (f,) = c.fills()
    assert (
        not f.simulated and f.venue_fill_id == "to1" and f.fee == 0.01 and f.fee_estimate == 0.08
    )  # 0.0695 × 5 × .42 × .58 = $0.0846
    assert seen and "POST /v1/orders" in api.signed  # the book came over the signed WebSocket


def test_no_side_fills_are_reported_at_the_no_price(live: Any) -> None:
    make, _, _ = live
    o = make().buy(
        venue="polymarket_us", market="mkt-a", side="no", price=0.6, size=5
    )  # NO ask = 1 − 0.40 YES bid
    assert (o.status, o.avg_price) == ("filled", 0.6)


def test_resting_orders_are_read_back_cancelled_and_expire_at_the_venue(live: Any) -> None:
    make, api, _ = live
    c = make()
    o = c.buy(
        venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc", post_only=True
    )
    assert o.status == "open" and api.orders["o1"]["goodTillTime"].endswith("Z")
    assert [x.venue_order_id for x in c.orders()] == ["o1"]
    assert c.cancel(o).status == "canceled"
    o2 = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.36, size=2, tif="gtc")
    c.cancel_all()
    assert api.orders[o2.venue_order_id]["state"] == "ORDER_STATE_CANCELED"  # type: ignore[index]


def test_a_5xx_on_an_order_is_never_resent(live: Any) -> None:
    make, api, _ = live
    c = make()
    api.fail_next_order = 500
    with pytest.raises(VenueError) as e:
        c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc")
    assert e.value.code == "outcome_unknown" and e.value.next == "client.sync(), then check client.orders()"
    assert sum(1 for s in api.signed if s == "POST /v1/orders") == 1
    assert c.store.orders()[0].status == "pending"  # kept, so sync() can look for it


def test_an_order_that_did_arrive_is_found_after_a_5xx(live: Any) -> None:
    make, api, _ = live
    c = make()
    api.fail_next_order, api.record_failed_order = 502, True
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc")
    assert (o.venue_order_id, o.status) == ("o1", "open")
    assert sum(1 for s in api.signed if s == "POST /v1/orders") == 1


def usd(x: str) -> dict[str, str]:
    return {"value": x, "currency": "USD"}


_trade_ids = itertools.count(1)


def trade(slug: str, commission: str, *, mine_passive: bool = False) -> dict[str, Any]:
    """A trade as Polymarket US's activity history shows it: both sides' executions, and ``isAggressor`` for ours.

    The other side's order can come back in full, with its own maker rebate (seen 10-04).
    """
    n = next(_trade_ids)
    common = {"lastShares": "1.0000", "lastPx": usd("0.4000"), "tradeId": f"t{n}",
              "transactTime": "2026-10-01T11:00:00Z"}  # fmt: skip
    ours = {"order": {"id": "mine", "marketSlug": slug, "intent": "ORDER_INTENT_BUY_SHORT"},
            "commissionNotionalCollected": usd(commission), **common}  # fmt: skip
    theirs = {"order": {"id": "theirs", "marketSlug": slug, "intent": "ORDER_INTENT_SELL_SHORT"},
              "commissionNotionalCollected": usd("-0.0100"), **common}  # fmt: skip
    legs = (theirs, ours) if mine_passive else (ours, theirs)
    return {
        "type": "ACTIVITY_TYPE_TRADE",
        "trade": {"id": f"t{n}", "marketSlug": slug, "isAggressor": not mine_passive,
                  "aggressorExecution": legs[0], "passiveExecution": legs[1]},
    }  # fmt: skip


def test_live_pnl_is_what_polymarket_us_reports(live: Any) -> None:
    make, api, _ = live
    api.positions = {
        "mkt-a": {  # fees not filled in yet: cost is the contracts alone (seen 10-04)
            "netPositionDecimal": "-4.0000", "cost": usd("2.4000"), "realized": usd("0.3000"),
            "fees": None, "baseCost": None, "expired": False,
        },
        "mkt-b": {  # cost = baseCost + fees
            "netPositionDecimal": "10.0000", "cost": usd("5.1000"), "realized": usd("0.0000"),
            "fees": "0.1000", "baseCost": "5.0000", "expired": False,
        },
    }  # fmt: skip
    api.activities = [
        trade("mkt-a", "0.0200"),
        trade("mkt-a", "0.0300", mine_passive=True),
        trade("old-game", "0.6600"),
        {  # settled, so gone from the positions list
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": "old-game",
                "beforePosition": {
                    "netPositionDecimal": "44.6600",
                    "cost": usd("24.9947"),
                    "fees": "0.6550",
                    "baseCost": "24.3397",
                    "realized": usd("0.0000"),
                },
                "afterPosition": {
                    "netPositionDecimal": "0.0000",
                    "cost": usd("0.0000"),
                    "realized": usd("-24.3397"),
                },
            },
        },
    ]
    c = make()
    assert [p.market for p in c.positions()] == ["mkt-a", "mkt-b"]
    assert [p.avg_price for p in c.positions()] == [0.6, 0.5]  # before fees
    p = c.pnl()
    a, b, old = p.rows
    # 4 NO at the NO bid (1 − .42 YES ask = .58): 4 × .58 − 2.40 = −.08; fees charged .02 + .03
    assert (a.side, a.contracts, a.cost, a.unrealized, a.realized, a.fees) == ("no", 4, 2.4, -0.08, 0.3, 0.05)
    # 10 YES at the .60 bid: 6.00 − 5.00 = 1.00; the position's own fees field
    assert (b.cost, b.unrealized, b.fees) == (5.0, 1.0, 0.1)
    # A settled loss: realized is minus the cost before fees; the fee is counted once, in fees
    assert (old.market, old.settled, old.contracts, old.realized, old.fees) == (
        "old-game",
        True,
        0,
        -24.3397,
        0.66,
    )
    assert (p.realized, p.unrealized, p.fees, p.net) == (-24.0397, 0.92, 0.81, -23.9297)


def test_positions_balances_and_a_lost_store_starting_killed(live: Any) -> None:
    make, api, _ = live
    api.positions = {
        "mkt-a": {"netPositionDecimal": "-4", "cost": {"value": "2.4", "currency": "USD"}, "expired": False}
    }
    c = make()
    (p,) = c.positions()
    assert (p.market, p.side, p.contracts, p.avg_price) == ("mkt-a", "no", 4.0, 0.6)
    assert c.balances()["polymarket_us"].cash == 100.5
    # With a short position, currentBalance also counts the margin held against it: cash is what can be spent.
    api.balance_extra = {"currentBalance": 13.72965, "buyingPower": 9.09965, "marginRequirement": 4.63}
    assert c.balances()["polymarket_us"].cash == 9.09965
    assert c.killed  # a new store, but the venue shows a position: start killed until a person resumes
    with pytest.raises(VenueError):
        c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=1)


def test_the_kill_switch_cancels_everything_at_the_venue(live: Any) -> None:
    make, api, _ = live
    c = make()
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc")
    c.kill()
    assert all(o["state"] == "ORDER_STATE_CANCELED" for o in api.orders.values())
    assert "POST /v1/orders/open/cancel" in api.signed


def test_latency_stopgap_rejects_say_nothing_was_placed(live: Any, venue: FakeVenue) -> None:
    make, api, _ = live
    c = make()
    orig = api.handle

    def stopgap(r: httpx.Request) -> httpx.Response:
        if r.method == "POST" and r.url.path == "/v1/orders":
            api._verify(r)
            o = {"id": "o9", "state": "ORDER_STATE_REJECTED", "cumQuantity": 0}
            return httpx.Response(
                200,
                json={
                    "id": "o9",
                    "executions": [
                        {"type": "EXECUTION_TYPE_REJECTED", "order": o, "text": "Global Rate Limit Exceeded"}
                    ],
                },
            )
        return orig(r)

    api.handle = stopgap  # type: ignore[method-assign]
    with pytest.raises(VenueError) as e:
        c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=1)
    assert (
        e.value.code == "venue_unavailable"
        and e.value.retryable
        and "isn't a rate limit" in (e.value.hint or "")
    )


def test_the_real_websocket_library_signs_the_handshake(venue: FakeVenue, clock: Clock) -> None:
    """Uses the websockets library end to end against a local server."""
    from websockets.sync.server import serve

    from uselayer.http import Http
    from uselayer.venues.polymarket_us_live import PolymarketUSLive

    key, pub = keypair()
    got: dict[str, Any] = {}

    def handler(ws: Any) -> None:
        h = ws.request.headers
        pub.verify(base64.b64decode(h["X-PM-Signature"]), f"{h['X-PM-Timestamp']}GET/v1/ws/markets".encode())
        got["sub"] = json.loads(ws.recv())
        ws.send(json.dumps({"heartbeat": {}}))
        ws.send(
            json.dumps(
                {
                    "marketData": {
                        "marketSlug": "mkt-a",
                        "bids": [{"px": {"value": "0.4"}, "qty": "5"}],
                        "offers": [{"px": {"value": "0.42"}, "qty": "7"}],
                        "state": "MARKET_STATE_OPEN",
                        "transactTime": "2026-10-01T11:59:00Z",
                    }
                }
            )
        )

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        live_adapter = PolymarketUSLive(
            Http(transport=venue.transport()), key, ws_url=f"ws://127.0.0.1:{port}/v1/ws/markets"
        )
        r = live_adapter.read_book("mkt-a")
        server.shutdown()
    assert got["sub"]["subscribe"]["marketSlugs"] == ["mkt-a"]
    assert (r.book.bids[0].price, r.book.asks[0].price, r.state) == (0.4, 0.42, "MARKET_STATE_OPEN")


def test_a_failed_websocket_falls_back_to_the_public_book(venue: FakeVenue) -> None:
    from uselayer.http import Http
    from uselayer.venues.polymarket_us_live import PolymarketUSLive

    alerts: list[dict[str, Any]] = []
    key, _ = keypair()

    def broken(*_: Any, **__: Any) -> Any:
        raise OSError("no route")

    a = PolymarketUSLive(Http(transport=venue.transport()), key, ws_connect=broken, on_alert=alerts.append)
    assert a.read_book("mkt-a").book.asks[0].price == 0.42
    assert alerts[0]["kind"] == "websocket_book_failed"


def test_paper_mode_never_signs_or_contacts_the_trading_api(make_client: Any, venue: FakeVenue) -> None:
    c = make_client()
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=1)
    assert all(
        r.url.host != "api.polymarket.us" and "X-PM-Signature" not in r.headers for r in venue.requests
    )


def test_market_with_zero_bid_side(live: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("thin", bids=[], asks=[(0.5, 3)]))
    make, _, _ = live
    o = make().buy(venue="polymarket_us", market="thin", side="yes", price=0.5, size=3)
    assert o.status == "filled"


def test_a_new_order_that_briefly_404s_is_read_back_not_failed(live: Any) -> None:
    """Seen live on 2026-10-01: GET /v1/order/{id} answers 404 for a moment right after the order is placed."""
    make, api, _ = live
    c = make()
    orig = api.handle
    misses = {"left": 2}

    def lagging(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path.startswith("/v1/order/") and misses["left"]:
            api._verify(r)
            misses["left"] -= 1
            return httpx.Response(404, json={"code": 5, "message": "Order not found"})
        return orig(r)

    api.handle = lagging  # type: ignore[method-assign]
    o = c.buy(
        venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc", post_only=True
    )
    assert (o.status, o.venue_order_id) == ("open", "o1")
    misses["left"] = 99
    o2 = c.buy(
        venue="polymarket_us", market="mkt-a", side="yes", price=0.36, size=2, tif="gtc", post_only=True
    )
    assert (o2.status, o2.venue_order_id) == ("pending", "o2")  # taken by the venue; sync() picks it up later


def test_a_cancel_that_lands_a_moment_later_is_waited_for(live: Any) -> None:
    """Seen live on 2026-10-01: right after POST /cancel, the order still reads as open for a moment."""
    make, api, _ = live
    c = make()
    o = c.buy(
        venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc", post_only=True
    )
    orig = api.handle
    reads = {"open": 2}

    def slow_cancel(r: httpx.Request) -> httpx.Response:
        if r.method == "POST" and r.url.path.endswith("/cancel") and r.url.path.startswith("/v1/order/"):
            api._verify(r)
            return httpx.Response(200, json={})  # accepted, applied later
        if r.method == "GET" and r.url.path == f"/v1/order/{o.venue_order_id}":
            if reads["open"]:
                reads["open"] -= 1
            else:
                api.orders[o.venue_order_id]["state"] = "ORDER_STATE_CANCELED"  # type: ignore[index]
        return orig(r)

    api.handle = slow_cancel  # type: ignore[method-assign]
    assert c.cancel(o).status == "canceled"


# ---- fills from refresh(): the venue's trade id and time ----


def _z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def filled_at_venue(
    api: FakeApi,
    oid: str,
    *,
    trades: list[tuple[str, float, str, datetime]],
    cum: float,
    avg_yes: str,
    aggressor: bool = False,
) -> None:
    """The venue fills order ``oid``: its state moves to ``cum`` filled, and each trade (id, shares, YES price,
    time) goes on top of the account's activity history (newest first), this account's side passive unless
    ``aggressor``."""
    o = api.orders[oid]
    o.update(
        cumQuantity=cum,
        leavesQuantity=float(o["quantity"]) - cum,
        avgPx=usd(avg_yes),
        state="ORDER_STATE_FILLED" if cum >= float(o["quantity"]) else "ORDER_STATE_PARTIALLY_FILLED",
    )
    for tid, shares, yes_px, at in trades:
        leg = {"lastShares": str(shares), "lastPx": usd(yes_px), "tradeId": tid, "transactTime": _z(at)}
        ours = {"order": {"id": oid, "marketSlug": o["marketSlug"], "intent": o["intent"]},
                "commissionNotionalCollected": usd("0.0000"), **leg}  # fmt: skip
        theirs = {"order": {"id": "theirs", "marketSlug": o["marketSlug"], "intent": "ORDER_INTENT_BUY_LONG"},
                  "commissionNotionalCollected": usd("0.0100"), **leg}  # fmt: skip
        first, second = (ours, theirs) if aggressor else (theirs, ours)
        t = {"id": tid, "marketSlug": o["marketSlug"], "isAggressor": aggressor,
             "aggressorExecution": first, "passiveExecution": second}  # fmt: skip
        api.activities.insert(0, {"type": "ACTIVITY_TYPE_TRADE", "trade": t})


def test_a_fill_seen_after_midnight_counts_toward_the_day_it_happened(live: Any, clock: Clock) -> None:
    """Issue #25: the venue fills a losing sell at 23:59:50 UTC; sync() only sees it at 00:00:05."""
    make, api, _ = live
    c = make(rules={"max_daily_loss": {"amount": 0.05, "counts": "realized_only"}})
    day1 = datetime(2026, 10, 1, tzinfo=UTC)
    clock.now = day1 + timedelta(hours=23, minutes=59)
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=5)  # $0.01 fee
    o = c.sell(venue="polymarket_us", market="mkt-a", side="yes", price=0.41, size=5, tif="gtc")
    assert o.status == "open"
    traded_at = day1 + timedelta(hours=23, minutes=59, seconds=50)
    filled_at_venue(api, "o2", trades=[("T-SELL", 5, "0.41", traded_at)], cum=5, avg_yes="0.41")
    clock.now = day1 + timedelta(days=1, seconds=5)
    c.sync()
    sell = next(f for f in c.fills() if f.action == "sell")
    assert isinstance(sell, Fill) and (sell.venue_fill_id, sell.at, sell.role) == (
        "T-SELL",
        traded_at,
        "maker",
    )
    # The $0.05 loss and the fees are day 1's: that day is over its limit, today isn't.
    day1_ledger = build(c.fills(), day1)
    assert day1_ledger.realized_today - day1_ledger.fees_today == pytest.approx(-0.06)
    assert c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=1).status == "filled"


def test_refresh_records_each_trade_and_whatever_the_history_doesnt_show_yet(live: Any, clock: Clock) -> None:
    make, api, _ = live
    c = make()
    o = c.buy(venue="polymarket_us", market="mkt-a", side="no", price=0.55, size=5, tif="gtc")  # rests
    t0 = clock.now
    filled_at_venue(api, "o1", trades=[("T-A", 2, "0.45", t0 + timedelta(seconds=10))], cum=2, avg_yes="0.45")
    clock.advance(30)
    c.sync()
    (a,) = c.fills()
    assert isinstance(a, Fill)
    assert (a.venue_fill_id, a.at, a.role, a.side, a.price, a.contracts, a.fee) == (
        "T-A", t0 + timedelta(seconds=10), "maker", "no", 0.55, 2, 0.0
    )  # fmt: skip
    # 3 more fill; the history shows one of the two trades so far.
    filled_at_venue(api, "o1", trades=[("T-B", 1, "0.45", t0 + timedelta(seconds=40))], cum=5, avg_yes="0.45")
    clock.advance(30)
    seen_at = clock.now
    c.sync()
    _, b, rest = c.fills()
    assert isinstance(b, Fill) and isinstance(rest, Fill)
    assert (b.venue_fill_id, b.at, b.contracts) == ("T-B", t0 + timedelta(seconds=40), 1)
    assert (rest.venue_fill_id, rest.at, rest.contracts, rest.price) == ("o1:5.0", seen_at, 2, 0.55)
    assert c.store.order(o.id or "").filled == 5  # type: ignore[union-attr]
    # Once the history catches up, reconcile() takes "o1:5.0" for the trade it stands for.
    filled_at_venue(api, "o1", trades=[("T-C", 2, "0.45", t0 + timedelta(seconds=50))], cum=5, avg_yes="0.45")
    api.positions = {"mkt-a": {"netPositionDecimal": "-5", "cost": usd("2.75"), "expired": False}}
    assert c.reconcile().ok


def test_a_store_from_before_the_upgrade_gets_no_double_fills(live: Any, clock: Clock) -> None:
    """A fill stored as "<venue order id>:<filled>" (before 0.3.0) stays the only record of its trade."""
    make, api, _ = live
    c = make()
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=3, tif="gtc")
    t0 = clock.now
    filled_at_venue(api, "o1", trades=[("T-A", 1, "0.35", t0 + timedelta(seconds=5))], cum=1, avg_yes="0.35")
    clock.advance(10)
    # What the old refresh() stored for that trade.
    old = Fill(venue="polymarket_us", market="mkt-a", order_id=o.id or "", venue_fill_id="o1:1.0", side="yes",
               action="buy", price=0.35, contracts=1, role="maker", cost=0.35, fee=0.0, fee_estimate=0.0,
               at=clock.now)  # fmt: skip
    c.store.add_fill(old)
    c.store.save_order(o.model_copy(update={"filled": 1.0, "avg_price": 0.35, "status": "open"}))
    filled_at_venue(api, "o1", trades=[("T-B", 2, "0.35", t0 + timedelta(seconds=20))], cum=3, avg_yes="0.35")
    clock.advance(20)
    c.sync()
    got = [(f.venue_fill_id, f.contracts) for f in c.fills() if isinstance(f, Fill)]
    assert got == [("o1:1.0", 1), ("T-B", 2)]
    api.positions = {"mkt-a": {"netPositionDecimal": "3", "cost": usd("1.05"), "expired": False}}
    r = c.reconcile(repair=True)
    assert r.ok and not r.repaired
    assert sum(f.contracts for f in c.fills()) == 3


# ---- fills a read-back finds: every filled contract reaches the store ----


def test_a_resting_order_that_fills_at_once_records_its_fills(live: Any, clock: Clock) -> None:
    """A gtc order that crosses the book is answered without executions; its read-back shows 10 filled."""
    make, api, _ = live
    c = make()
    orig, at = api.handle, clock.now

    def fills_on_arrival(r: httpx.Request) -> httpx.Response:
        resp = orig(r)
        if r.method == "POST" and r.url.path == "/v1/orders":
            filled_at_venue(
                api, "o1", trades=[("T-X", 10, "0.42", at)], cum=10, avg_yes="0.42", aggressor=True
            )
        return resp

    api.handle = fills_on_arrival  # type: ignore[method-assign]
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=15, tif="gtc")
    assert (o.status, o.filled, o.avg_price) == ("open", 10, 0.42)
    got = [(f.venue_fill_id, f.contracts, f.role, f.at) for f in c.fills()]  # type: ignore[union-attr]
    assert got == [("T-X", 10, "taker", at)]
    c.sync()
    assert sum(f.contracts for f in c.fills()) == 10  # recorded once


def test_a_fill_before_the_cancel_landed_is_recorded(live: Any, clock: Clock) -> None:
    make, api, _ = live
    c = make()
    o = c.buy(
        venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=5, tif="gtc", post_only=True
    )
    traded_at = clock.now + timedelta(seconds=3)
    filled_at_venue(api, "o1", trades=[("T-A", 2, "0.35", traded_at)], cum=2, avg_yes="0.35")
    clock.advance(5)
    canceled = c.cancel(o)
    assert (canceled.status, canceled.filled, canceled.avg_price) == ("canceled", 2, 0.35)
    (f,) = c.fills()
    assert isinstance(f, Fill) and (f.venue_fill_id, f.contracts, f.at) == ("T-A", 2, traded_at)
    assert c.store.order(o.id or "") == canceled


def test_an_order_found_after_a_5xx_brings_its_fills(live: Any, clock: Clock) -> None:
    """The venue took the order and filled part of it; the answer was lost, and find() sees 1 filled."""
    make, api, _ = live
    c = make()
    api.fail_next_order, api.record_failed_order = 502, True
    orig, at = api.handle, clock.now

    def fills_unseen(r: httpx.Request) -> httpx.Response:
        resp = orig(r)
        if r.method == "POST" and r.url.path == "/v1/orders" and "o1" in api.orders and not api.activities:
            filled_at_venue(api, "o1", trades=[("T-L", 1, "0.35", at)], cum=1, avg_yes="0.35")
            api.orders["o1"]["state"] = "ORDER_STATE_NEW"  # still open, so find() lists it
        return resp

    api.handle = fills_unseen  # type: ignore[method-assign]
    o = c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.35, size=2, tif="gtc")
    assert (o.venue_order_id, o.status, o.filled) == ("o1", "open", 1)
    assert [(f.venue_fill_id, f.contracts) for f in c.fills()] == [("T-L", 1)]  # type: ignore[union-attr]
