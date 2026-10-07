"""Kalshi live proof on Kalshi's DEMO exchange (mock money), through Client(mode="live") and every guardrail.

    KALSHI_KEY_ID=<demo key id> KALSHI_PRIVATE_KEY_PATH=<demo key .pem> python scripts/prove_kalshi_live.py

It refuses any host but the demo one and spends at most about 10¢ of mock money.

1. Balance, then a resting order far below the market: place → it shows as open → cancel.
2. Two resting orders, then cancel_all(): both canceled at Kalshi, none open.
3. Buy 1 YES at the ask (IOC): the real fill and Kalshi's fee, the position, the fills list.
4. Sell it back at the bid.
5. A guardrail: with max_position at $0.50, an order risking more is blocked before anything is sent.
6. The kill switch: kill() cancels a resting order at Kalshi and blocks the next order.
"""

import math
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from uselayer import Admin, Client, Kalshi, VenueError
from uselayer.venues.kalshi import KalshiLive

key = Kalshi.from_env()
key = Kalshi(
    key_id=key.key_id,
    private_key_path=key.private_key_path,
    private_key_pem=key.private_key_pem,
    environment="demo",
)
# Kalshi bars residents of some states from some categories, so stay with crypto, indexes, weather and gas.
ALLOWED_SERIES = ("KXBTC", "KXETH", "KXNASDAQ", "KXINX", "KXRAIN", "KXHIGH", "KXAAAGAS")
store = str(Path(tempfile.mkdtemp()) / "live.db")
alerts: list[dict[str, Any]] = []


def live_client(**kw: Any) -> Client:
    c = Client(mode="live", kalshi=key, store=store, on_alert=alerts.append, **kw)
    k = c._live["kalshi"]
    assert isinstance(k, KalshiLive) and k._base.startswith("https://demo-api.kalshi.co/"), "demo only"
    return c


client = live_client()
if client.killed:
    # A new store while the demo account already holds positions: the client starts killed, as it
    # should. A person turns it back on; here that person is this script, on the demo exchange.
    print(f"started killed ({Admin(mode='live', store=store).status()['kill_info']}); resuming as the person")
    Admin(mode="live", store=store).resume()
k: KalshiLive = client._live["kalshi"]  # type: ignore[assignment]


def liquid_market() -> str:
    """An allowed, funded market with a YES ask of at most 10¢, 5+ contracts at the bid, closing 2+ hours out."""
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
            ok = (
                str(m.get("ticker", "")).startswith(ALLOWED_SERIES)
                and m.get("exchange_index", 0) in funded
                and m["ticker"] not in held
                and 0.02 <= ask <= 0.10
                and float(m.get("yes_ask_size_fp") or 0) >= 5
                and bid >= 0.01
                and close > soon
            )
            if ok:
                y = client.book(m["ticker"], venue="kalshi").outcome("yes")
                if y.best_ask and y.best_bid and y.best_ask.price <= 0.10 and y.best_bid.size >= 5:
                    return str(m["ticker"])
        cursor = body.get("cursor")
        if not cursor:
            break
    raise SystemExit("no liquid demo market right now; try again later")


start = client.balances()["kalshi"].cash
print(f"demo balance ${start:.4f} (key {k.key_kind})")
t = liquid_market()
info = client.market(t, venue="kalshi")
print(f"market {t}: tick {info.tick_size}, fees x{info.fees.multiplier} {info.fees.fee_type}")
expires = datetime.now(UTC) + timedelta(minutes=5)


def resting() -> Any:
    return client.order(
        venue="kalshi",
        market=t,
        side="yes",
        price=info.tick_size,
        size=1,
        tif="gtc",
        post_only=True,
        expires_at=expires,
    )


# 1. place → open → cancel
placed = client.send(resting())
assert placed.status == "open" and placed.venue_order_id, placed
assert placed.venue_order_id in [o.venue_order_id for o in client.orders()]
print(f"✓ placed resting {placed.venue_order_id} @ {placed.price}: {placed.status}")
canceled = client.cancel(placed)
assert canceled.status == "canceled", canceled
print(f"✓ canceled {canceled.venue_order_id}: {canceled.status}")

# 2. cancel_all
two = [client.send(resting()) for _ in range(2)]
client.cancel_all()
states: list[str] = []
for _ in range(20):  # Kalshi applies cancel-all within moments
    states = [k.refresh(o)[0].status for o in two]
    if states == ["canceled", "canceled"] and not k.open_orders():
        break
    time.sleep(0.5)
assert states == ["canceled", "canceled"] and not k.open_orders(), states
print("✓ cancel_all: both resting orders canceled, 0 open at Kalshi")

# 3. a fill, the position, the fills list
ask = client.book(t, venue="kalshi").outcome("yes").best_ask
assert ask is not None
bought = client.buy(venue="kalshi", market=t, side="yes", price=ask.price, size=1)
assert bought.status == "filled" and bought.filled == 1, bought
f = next(x for x in client.fills() if x.order_id == bought.id)
print(
    f"✓ filled {f.contracts} YES @ {f.price}: Kalshi billed ${f.fee} (Layer's cent-rounded estimate ${f.fee_estimate})"
)
pos = [p for p in client.positions() if p.market == t]
assert pos and pos[0].side == "yes" and pos[0].contracts == 1, pos
print(f"✓ position from Kalshi: {pos[0].contracts} {pos[0].side}, cost ${pos[0].cost}")
print(f"✓ fills in the store: {len(client.fills())}, balance now ${client.balances()['kalshi'].cash:.4f}")

# 4. sell it back
bid = client.book(t, venue="kalshi").outcome("yes").best_bid
assert bid is not None
sold = client.sell(venue="kalshi", market=t, side="yes", price=bid.price, size=1)
print(f"✓ sold back: {sold.status} @ {sold.avg_price}, fees ${sold.fees}")
client.close()

# 5. a guardrail blocks a live Kalshi order before it's sent
guarded = live_client(rules={"max_position": {"per_market": 0.5}})
k = guarded._live["kalshi"]  # type: ignore[assignment]
ask = guarded.book(t, venue="kalshi").outcome("yes").best_ask
assert ask is not None
size = math.ceil(0.6 / ask.price) + 1
big = guarded.order(venue="kalshi", market=t, side="yes", price=ask.price, size=size)
before = len(k.open_orders())
try:
    guarded.send(big)
    raise SystemExit("max_position did not block the order")
except VenueError as e:
    assert (e.code, e.rule) == ("blocked_by_rule", "max_position"), e
    print(f"✓ guardrail: {size} @ {ask.price} blocked by {e.rule}: {e.message}")
assert guarded.orders(open=False)[-1].id != big.client_id and len(k.open_orders()) == before

# 6. the kill switch
r = guarded.send(resting())
assert r.status == "open"
guarded.kill()
for _ in range(20):
    if not k.open_orders():
        break
    time.sleep(0.5)
assert not k.open_orders(), "kill() left an order open at Kalshi"
print(f"✓ kill(): resting {r.venue_order_id} canceled at Kalshi, 0 open")
try:
    guarded.send(resting())
    raise SystemExit("the kill switch did not block the order")
except VenueError as e:
    assert e.rule == "kill_switch", e
    print(f"✓ kill switch blocks the next order: {e.message} (next: {e.next})")
guarded.close()
Admin(mode="live", store=store).resume()

end = Client(mode="live", kalshi=key, store=store, on_alert=alerts.append).balances()["kalshi"].cash
print(f"demo balance ${end:.4f} (change ${end - start:+.4f})")
