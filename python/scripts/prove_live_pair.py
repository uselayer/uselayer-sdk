"""Live pair proof: client.trade() in live mode, Kalshi's leg on Kalshi's DEMO exchange (mock money).

    KALSHI_KEY_ID=<demo key id> KALSHI_PRIVATE_KEY_PATH=<demo key .pem> python scripts/prove_live_pair.py

Polymarket US has no demo exchange, so its leg goes to the fake Polymarket US API from the tests
(tests/test_live_polymarket_us.py: it checks every signature and fills against a book this script
sets). The real Polymarket US adapter builds, signs and reads every request; nothing reaches
polymarket.us. Every Kalshi request goes to demo-api.kalshi.co, and any other host is refused.

Each pair is 2 contracts; the whole run spends well under $1 of mock money and sells back what it buys.

A. Hedged, Kalshi first (its book is thinner): a real demo fill, then the Polymarket US leg.
B. Hedged, Polymarket US first: the Kalshi demo order is the second leg.
C. Polymarket US misses after the Kalshi leg fills: the Kalshi leg is sold back on demo ("unwound").
D. The kill switch is pressed after the Kalshi leg fills: no second leg; the unwind still runs on demo.
E. The unwind can't happen (no bid at or above entry with max_unwind_loss=0): "exposed", reported.
F. Polymarket US answers 500 to the second leg: outcome unknown, so nothing is unwound; "exposed".
G. Guardrails: a rule the Polymarket US leg breaks blocks the pair before the Kalshi order is sent.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import FakeMarket, FakeVenue
from test_live_polymarket_us import FakeApi, FakeWs, keypair

from uselayer import Admin, Client, Kalshi, Order, Trade, VenueError
from uselayer.venues.kalshi import KalshiLive

DEMO = "demo-api.kalshi.co"
SIZE = 2
# Kalshi bars residents of some states from some categories, so stay with crypto, indexes, weather and gas.
ALLOWED_SERIES = ("KXBTC", "KXETH", "KXNASDAQ", "KXINX", "KXRAIN", "KXHIGH", "KXAAAGAS")
TWIN = "kalshi-demo-twin"  # the fake Polymarket US market


class RealClock:
    @property
    def now(self) -> datetime:
        return datetime.now(UTC)


key = Kalshi.from_env()
key = Kalshi(
    key_id=key.key_id,
    private_key_path=key.private_key_path,
    private_key_pem=key.private_key_pem,
    environment="demo",
)
pm_key, pm_pub = keypair()  # a made-up Polymarket US key that only the fake knows
pm = FakeVenue(RealClock())  # type: ignore[arg-type]
pm_api = FakeApi(pm, pm_pub)
sent: list[str] = []  # "METHOD host path" of every request


class Router(httpx.BaseTransport):
    """Kalshi demo over the network; Polymarket US to the fake; anything else refused."""

    def __init__(self) -> None:
        self.real = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        sent.append(f"{request.method} {host} {request.url.path}")
        if host == DEMO:
            return self.real.handle_request(request)
        if host == "api.polymarket.us":
            return pm_api.handle(request)
        if host == "gateway.polymarket.us":
            return pm.handler(request)
        raise AssertionError(f"refused: {host} (only {DEMO} and the fake Polymarket US)")


store = str(Path(tempfile.mkdtemp()) / "live-pair.db")
alerts: list[dict[str, Any]] = []
transport = Router()


def live_client(**kw: Any) -> Client:
    c = Client(
        mode="live",
        kalshi=key,
        polymarket_us=pm_key,
        transport=transport,
        ws_connect=FakeWs(pm, pm_pub, []),
        store=store,
        on_alert=alerts.append,
        **kw,
    )
    k = c._live["kalshi"]
    assert isinstance(k, KalshiLive) and k._base.startswith(f"https://{DEMO}/"), "demo only"
    return c


def kalshi_posts() -> int:
    return sum(1 for s in sent if s.startswith(f"POST {DEMO}") and s.endswith("/portfolio/events/orders"))


def pm_posts() -> int:
    return pm_api.signed.count("POST /v1/orders")


def after_first_leg(c: Client, fn: Callable[[Order], None]) -> None:
    orig = c._execute
    state = {"done": False}

    def wrapped(order: Order, checked: bool = False) -> Order:
        r = orig(order, checked=checked)
        if not state["done"] and order.action == "buy" and r.filled:
            state["done"] = True
            fn(r)
        return r

    c._execute = wrapped  # type: ignore[method-assign]


client = live_client()
if client.killed:
    # A new store while the demo account already holds positions: the client starts killed, as it
    # should. A person turns it back on; here that person is this script, on the demo exchange.
    print(f"started killed ({Admin(mode='live', store=store).status()['kill_info']}); resuming as the person")
    Admin(mode="live", store=store).resume()
k: KalshiLive = client._live["kalshi"]  # type: ignore[assignment]


def liquid_market() -> str:
    """An allowed, funded demo market: YES ask 3–30¢ with 10+ contracts, a bid within 3¢ with 10+, 2+ hours left."""
    soon = datetime.now(UTC) + timedelta(hours=2)
    funded = {
        b["exchange_index"] for b in k.balance().raw.get("balance_breakdown") or [] if float(b["balance"]) > 1
    }
    held = {p.market for p in k.positions() if not p.settled and p.contracts > 0}
    cursor = None
    for _ in range(40):
        params: dict[str, Any] = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        body = k._call("GET", "/markets", params=params)
        for m in body.get("markets") or []:
            ask, bid = float(m.get("yes_ask_dollars") or 1), float(m.get("yes_bid_dollars") or 0)
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            if (
                str(m.get("ticker", "")).startswith(ALLOWED_SERIES)
                and m.get("exchange_index", 0) in funded
                and m["ticker"] not in held
                and 0.03 <= ask <= 0.30
                and 0 < ask - bid <= 0.03
                and float(m.get("yes_ask_size_fp") or 0) >= 10
                and close > soon
            ):
                y = client.book(m["ticker"], venue="kalshi").outcome("yes")
                if (
                    y.best_ask
                    and y.best_bid
                    and y.best_ask.size >= 10
                    and y.best_bid.size >= 10
                    and y.best_ask.price - y.best_bid.price <= 0.03 + 1e-9
                ):
                    return str(m["ticker"])
        cursor = body.get("cursor")
        if not cursor:
            break
    raise SystemExit("no liquid demo market right now; try again later")


start_cash = client.balances()["kalshi"].cash
ticker = liquid_market()
PAIR = [("kalshi", ticker), ("polymarket_us", TWIN)]
print(f"demo balance ${start_cash:.4f}; Kalshi demo market {ticker}")


def set_twin(*, thinner: bool) -> None:
    """The fake Polymarket US twin: NO asks 8¢ under 1 − Kalshi's YES ask (an edge).

    The thinner leg goes first, so ``thinner`` picks the order: SIZE deep, or 10× Kalshi's ask.
    """
    ask = client.book(ticker, venue="kalshi").outcome("yes").best_ask
    assert ask is not None
    depth = SIZE if thinner else ask.size * 10 + 1_000
    pm.add(FakeMarket(TWIN, bids=[(round(ask.price + 0.08, 3), depth)], asks=[(0.99, 10_000)]))


def held_on_kalshi() -> float:
    n = 0.0
    for f in client.fills():
        if f.venue == "kalshi" and f.market == ticker:
            n += f.contracts if f.action == "buy" else -f.contracts
    return round(n, 6)


def sell_back(c: Client) -> None:
    """Sell whatever this run still holds on the demo market, at the bid."""
    for _ in range(3):
        n = held_on_kalshi()
        if n <= 0:
            return
        bid = c.book(ticker, venue="kalshi").outcome("yes").best_bid
        assert bid is not None, "no bid to sell back into"
        c.sell(venue="kalshi", market=ticker, side="yes", price=bid.price, size=n)
    assert held_on_kalshi() <= 0, f"still holding {held_on_kalshi()} on {ticker}"


def show(name: str, t: Trade) -> None:
    print(f"\n{name}: {t.status}  hedged={t.hedged} locked_in={t.locked_in} unwind_loss={t.unwind_loss}")
    for o in t.orders:
        print(
            f"  {o.venue:<13} {o.action:<4} {o.side:<3} {o.reason or '':<6} size={o.size:g} filled={o.filled:g} "
            f"avg={o.avg_price} fees={o.fees} status={o.status} venue_id={o.venue_order_id}"
        )
    if t.exposure:
        e = t.exposure
        print(f"  exposure: {e.contracts:g} {e.side} on {e.venue} {e.market} @ {e.avg_price} (mark {e.mark})")
    for n in t.notes:
        print(f"  note: {n}")


results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {'✓' if ok else '✗'} {name}")


# A. hedged, Kalshi first
set_twin(thinner=False)
k0, p0 = kalshi_posts(), pm_posts()
t = client.trade(PAIR, size=SIZE, min_edge=0.01)
show("A. hedged, Kalshi first", t)
kal = t.orders[0] if t.orders else None
check(
    "A hedged; Kalshi demo leg first, filled with a venue order id and Kalshi's fee",
    t.status == "hedged"
    and kal is not None
    and kal.venue == "kalshi"
    and kal.filled == SIZE
    and bool(kal.venue_order_id)
    and kal.fees is not None,
)
check("A one Kalshi demo order, one Polymarket US order", (kalshi_posts() - k0, pm_posts() - p0) == (1, 1))
sell_back(client)

# B. hedged, Polymarket US first
set_twin(thinner=True)
k0 = kalshi_posts()
t = client.trade(PAIR, size=SIZE, min_edge=0.01)
show("B. hedged, Polymarket US first", t)
check(
    "B hedged; Polymarket US first, Kalshi demo second and filled",
    t.status == "hedged"
    and [o.venue for o in t.orders] == ["polymarket_us", "kalshi"]
    and t.orders[1].filled == SIZE,
)
sell_back(client)

# C. Polymarket US misses → the Kalshi leg is unwound on demo
set_twin(thinner=False)
c = live_client()
after_first_leg(c, lambda _: setattr(pm.markets[TWIN], "bids", []))
p0 = pm_posts()
t = c.trade(PAIR, size=SIZE, min_edge=0.01, chase_s=1.0)
show("C. Polymarket US misses", t)
last = t.orders[-1] if t.orders else None
check(
    "C unwound: Kalshi leg sold back on demo",
    t.status == "unwound"
    and last is not None
    and (last.venue, last.action, last.reason, last.filled) == ("kalshi", "sell", "unwind", SIZE),
)
check("C no Polymarket US order (no book to buy from)", pm_posts() == p0)
c.close()
sell_back(client)

# D. kill switch mid-pair
set_twin(thinner=False)
c = live_client()
after_first_leg(c, lambda _: Admin(mode="live", store=store).kill())
p0 = pm_posts()
t = c.trade(PAIR, size=SIZE, min_edge=0.01)
show("D. kill switch after the first leg", t)
check(
    "D second leg not sent, Kalshi leg unwound on demo",
    t.status == "unwound"
    and "kill switch pressed: second leg not sent" in t.notes
    and [(o.venue, o.reason) for o in t.orders] == [("kalshi", "open"), ("kalshi", "unwind")]
    and pm_posts() == p0,
)
c.close()
Admin(mode="live", store=store).resume()
sell_back(client)

# E. the unwind can't happen → exposed
set_twin(thinner=False)
c = live_client()
after_first_leg(c, lambda _: setattr(pm.markets[TWIN], "bids", []))
n_alerts = len(alerts)
t = c.trade(PAIR, size=SIZE, min_edge=0.01, chase_s=1.0, max_unwind_loss=0.0)
show("E. no bid to unwind into", t)
check(
    "E exposed: 2 YES on the Kalshi demo market reported, alert sent",
    t.status == "exposed"
    and t.exposure is not None
    and (t.exposure.venue, t.exposure.contracts) == ("kalshi", SIZE)
    and any(a["kind"] == "exposed" for a in alerts[n_alerts:]),
)
c.close()
sell_back(client)

# F. Polymarket US answers 500 to the second leg → outcome unknown, nothing unwound
set_twin(thinner=False)
c = live_client()
after_first_leg(c, lambda _: setattr(pm_api, "fail_next_order", 500))
k0 = kalshi_posts()
t = c.trade(PAIR, size=SIZE, min_edge=0.01, chase_s=1.0)
show("F. second leg outcome unknown", t)
check(
    "F exposed, not unwound, told to sync; only the first Kalshi order was sent",
    t.status == "exposed"
    and "not unwound: a second-leg order's outcome is unknown" in t.notes
    and kalshi_posts() - k0 == 1,
)
c.close()
sell_back(client)

# G. guardrails check both legs before the first is sent
set_twin(thinner=False)
c = live_client(rules={"max_position": {"per_market": 1.0}})  # the Polymarket US leg costs ~$1.7
k0, p0 = kalshi_posts(), pm_posts()
try:
    c.trade(PAIR, size=SIZE, min_edge=0.01)
    blocked = None
except VenueError as e:
    blocked = e
print(f"\nG. guardrails: {blocked.code if blocked else 'not blocked'} {blocked.rule if blocked else ''}")
check(
    "G blocked by max_position before any order; no Kalshi or Polymarket US order sent",
    blocked is not None
    and blocked.code == "blocked_by_rule"
    and (kalshi_posts() - k0, pm_posts() - p0) == (0, 0),
)
c.close()

end_cash = client.balances()["kalshi"].cash
print(f"\nheld on {ticker} at the end: {held_on_kalshi()}; demo balance ${start_cash:.4f} → ${end_cash:.4f}")
check("sold back everything bought on demo", held_on_kalshi() <= 0)
print(
    f"requests to polymarket.us over the network: 0 (all {sum(1 for s in sent if 'polymarket' in s)} answered by the fake)"
)
client.close()
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
