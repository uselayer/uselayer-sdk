"""Best-venue proof, live mode, on Kalshi's DEMO exchange (mock money): buy_best(), sell_best(), preview_best().

    KALSHI_KEY_ID=<demo key id> KALSHI_PRIVATE_KEY_PATH=<demo key .pem> python scripts/prove_best_kalshi_demo.py

Live mode with a Kalshi demo key only: no Polymarket US key, so no Polymarket US request can be made.
Every request goes to demo-api.kalshi.co; any other host is refused. Each order is 2 contracts, and
everything bought is sold back.

A. A Kalshi ↔ Polymarket US pair with no Polymarket US key: Polymarket US is skipped (``no_key``),
   the buy goes to Kalshi's demo exchange as a real demo order; sell_best() sells it back there.
B. Two Kalshi demo markets as the pair: the comparison is hand-checked from the books it read, the
   cheaper market gets the real demo order, and sell_best() sells it back where it's held
   (the other is ``not_held``).
C. The guardrails apply to the chosen order: ``max_position`` and the kill switch block it before
   any order reaches Kalshi.
D. A ``max_price`` under both best asks: nothing can take it, ``not_available``, nothing sent.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import httpx

from uselayer import Admin, Book, Client, Kalshi, VenueError
from uselayer.venues.kalshi import KalshiLive

DEMO = "demo-api.kalshi.co"
SIZE = 2
ALLOWED_SERIES = ("KXBTC", "KXETH", "KXNASDAQ", "KXINX", "KXRAIN", "KXHIGH", "KXAAAGAS")
TWIN = "no-polymarket-us-key-here"
sent: list[str] = []


class DemoOnly(httpx.BaseTransport):
    def __init__(self) -> None:
        self.real = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.host} {request.url.path}")
        if request.url.host != DEMO:
            raise AssertionError(f"refused: {request.url.host} (only {DEMO})")
        return self.real.handle_request(request)


def posts() -> int:
    return sum(1 for s in sent if s.startswith(f"POST {DEMO}") and s.endswith("/portfolio/events/orders"))


env_key = Kalshi.from_env()
key = Kalshi(
    key_id=env_key.key_id,
    private_key_path=env_key.private_key_path,
    private_key_pem=env_key.private_key_pem,
    environment="demo",
)
store = str(Path(tempfile.mkdtemp()) / "best-live.db")
transport = DemoOnly()


def live(**kw: Any) -> Client:
    c = Client(mode="live", kalshi=key, transport=transport, store=store, on_alert=lambda e: None, **kw)
    k = c._live["kalshi"]
    assert isinstance(k, KalshiLive) and k._base.startswith(f"https://{DEMO}/"), "demo only"
    assert "polymarket_us" not in c._live
    return c


client = live()
if client.killed:
    # A new store while the demo account already holds positions starts killed, as it should.
    print("started killed (demo account has positions); resuming as the person, on demo")
    Admin(mode="live", store=store).resume()
k: KalshiLive = client._live["kalshi"]  # type: ignore[assignment]


def liquid_markets(n: int) -> list[str]:
    """Allowed, funded demo markets: YES ask 3–30¢ with 10+ contracts, a bid within 3¢ with 10+, 2+ hours left."""
    soon = datetime.now(UTC) + timedelta(hours=2)
    funded = {
        b["exchange_index"] for b in k.balance().raw.get("balance_breakdown") or [] if float(b["balance"]) > 1
    }
    held = {p.market for p in k.positions() if not p.settled and p.contracts > 0}
    out: list[str] = []
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
                    out.append(str(m["ticker"]))
                    if len(out) == n:
                        return out
        cursor = body.get("cursor")
        if not cursor:
            break
    raise SystemExit("not enough liquid demo markets right now; try again later")


def held(ticker: str) -> float:
    n = 0.0
    for f in client.fills():
        if f.venue == "kalshi" and f.market == ticker:
            n += f.contracts if f.action == "buy" else -f.contracts
    return round(n, 6)


def hand(c: Client, v: Any, book: Book) -> Decimal:
    """All-in for a buy, by hand: walk the YES asks up to the cap, Kalshi's fee per level up to the cent."""
    mult = Decimal(str(c.market(v.market, venue="kalshi").fees.multiplier))
    left, total = Decimal(str(v.size)), Decimal(0)
    for lv in book.outcome(v.side).asks:
        if left <= 0 or Decimal(str(lv.price)) > Decimal(str(v.cap)):
            break
        n, p = min(left, Decimal(str(lv.size))), Decimal(str(lv.price))
        total += n * p + (mult * Decimal("0.07") * n * p * (1 - p)).quantize(Decimal("0.01"), ROUND_CEILING)
        left -= n
    assert left == 0
    return total


start = client.balances()["kalshi"].cash
x, y = liquid_markets(2)
print(f"demo balance ${start:.4f}; demo markets {x} and {y}")
checks: list[str] = []

# A. Kalshi ↔ Polymarket US, no Polymarket US key.
pair_a = [("kalshi", x), ("polymarket_us", TWIN)]
p = client.preview_best(pair_a, "yes", SIZE)
assert [v.skip for v in p.why.venues] == [None, "no_key"] and p.why.reason_code == "only_venue", (
    p.why.to_dict()
)
assert posts() == 0 and p.preview is not None and p.preview.allowed
r = client.buy_best(pair_a, "yes", SIZE)
o = r.order
assert o is not None and (o.venue, o.mode) == ("kalshi", "live") and o.venue_order_id and o.filled > 0
assert posts() == 1
print(
    f"A. {r.why.reason}\n   demo order {o.venue_order_id}: {o.filled} YES {x} @ {o.avg_price}, fees {o.fees}"
)
s = client.sell_best(pair_a, "yes", o.filled)
assert s.order is not None and s.order.venue == "kalshi" and s.order.action == "sell"
assert s.order.filled == o.filled and posts() == 2 and held(x) == 0
print(
    f"   sold back: {s.order.filled} @ {s.order.avg_price} ({s.why.reason_code}; Polymarket US {s.why.venues[1].skip})"
)
checks.append("A no_key skip, real demo buy, sell_best back")

# B. Two Kalshi demo markets: the cheaper one, hand-checked.
pair_b = [("kalshi", x), ("kalshi", y)]
books: list[Book] = []
orig_book = client.book


def logged(market: Any, *, venue: str = "polymarket_us") -> Book:
    b = orig_book(market, venue=venue)
    books.append(b)
    return b


client.book = logged  # type: ignore[method-assign]
pv = client.preview_best(pair_b, "yes", SIZE)
for v in pv.why.venues:
    b = next(bk for bk in books if bk.market == v.market)
    assert Decimal(str(v.all_in)) == hand(client, v, b), (v.to_dict(), hand(client, v, b))
want = min(pv.why.venues, key=lambda v: (v.all_in, -(v.size_at_limit or 0)))
assert pv.why.chosen is not None and pv.why.chosen.market == want.market
print(f"B. preview: {pv.why.reason} (both hand-checked)")
client.book = orig_book  # type: ignore[method-assign]
before = posts()
rb = client.buy_best(pair_b, "yes", SIZE)
ob = rb.order
assert ob is not None and ob.filled > 0 and posts() == before + 1
print(f"   {rb.why.reason}\n   demo order {ob.venue_order_id}: {ob.filled} YES {ob.market} @ {ob.avg_price}")
sb = client.sell_best(pair_b, "yes", ob.filled)
other = next(v for v in sb.why.venues if v.market != ob.market)
assert sb.order is not None and sb.order.market == ob.market and other.skip == "not_held"
assert sb.order.filled == ob.filled and held(ob.market) == 0
print(f"   sold back on {ob.market}: {sb.order.filled} @ {sb.order.avg_price}; {other.market}: {other.skip}")
checks.append("B cheaper market chosen (hand-checked), real demo buy, sold back, not_held skip")

# C. Guardrails on the chosen order, before anything is sent.
before = posts()
capped = live(rules={"max_position": {"per_market": 0.05}})
try:
    capped.buy_best(pair_b, "yes", SIZE)
    raise AssertionError("max_position should block")
except VenueError as e:
    assert (e.code, e.rule) == ("blocked_by_rule", "max_position"), e
Admin(mode="live", store=store).kill()
try:
    client.buy_best(pair_b, "yes", SIZE)
    raise AssertionError("the kill switch should block")
except VenueError as e:
    assert e.rule == "kill_switch", e
Admin(mode="live", store=store).resume()
assert posts() == before
print("C. max_position and the kill switch blocked the chosen order; no order reached Kalshi")
checks.append("C guardrails + kill switch on the chosen order")

# D. max_price under both best asks.
try:
    client.buy_best(pair_b, "yes", SIZE, max_price=0.01)
    raise AssertionError("expected not_available")
except VenueError as e:
    assert e.code == "not_available" and [v["skip"] for v in e.raw["venues"]] == ["above_max_price"] * 2
assert posts() == before
print("D. max_price 0.01: not_available, nothing sent")
checks.append("D max_price respected")

end = client.balances()["kalshi"].cash
left = {t: held(t) for t in (x, y)}
assert all(v == 0 for v in left.values()), left
print(f"demo balance ${start:.4f} → ${end:.4f} (spent {start - end:.4f} of mock money); held after: {left}")
print(f"{len(checks)}/4 checks passed; {posts()} demo orders; hosts: {sorted({s.split()[1] for s in sent})}")
sys.exit(0)
