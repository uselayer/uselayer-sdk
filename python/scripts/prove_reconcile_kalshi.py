"""Reconciliation proof on Kalshi's DEMO exchange (mock money): make each mismatch on purpose, then check
that client.reconcile() reports it, and that repair=True fixes only what it should.

    KALSHI_KEY_ID=<demo key id> KALSHI_PRIVATE_KEY_PATH=<demo key .pem> python scripts/prove_reconcile_kalshi.py

It refuses any host but the demo one and buys at most 4 contracts at 10¢ or less (then sells them back).
"Outside the SDK" means an order sent straight to Kalshi's API with the same key, which the SDK's store
never sees, the way a trade on kalshi.com or from another bot would look.

0. A fresh store on a quiet account: no mismatch.
1. The SDK buys 1 YES: still no mismatch.
2. 1 YES bought outside the SDK: an outside fill, and a position the store has at +1 and Kalshi at +2.
3. A crash mid-order (the order saved as pending, its fill never saved) and a fill row lost from a
   finished order: two missed fills. repair=True puts both back (one via sync(), one directly).
4. An outside resting order, and an SDK resting order canceled outside the SDK: an outside order and a
   stale order.
5. python -m uselayer reconcile prints them and exits 1.
6. Everything sold back: positions agree; only the outside fills stay reported.
"""

import sqlite3
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from uselayer import Admin, Client, Kalshi, Order, Reconciliation
from uselayer import __main__ as cli
from uselayer.http import Http
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

client = Client(mode="live", kalshi=key, store=store, on_alert=alerts.append)
k: KalshiLive = client._live["kalshi"]  # type: ignore[assignment]
assert k._base.startswith("https://demo-api.kalshi.co/"), "demo only"
if client.killed:
    print(f"started killed ({Admin(mode='live', store=store).status()['kill_info']}); resuming as the person")
    Admin(mode="live", store=store).resume()
# The same key, straight to Kalshi's API: orders the SDK's store never sees.
outside = KalshiLive(Http(), key)
assert outside._base.startswith("https://demo-api.kalshi.co/"), "demo only"
start_at = datetime.now(UTC) - timedelta(seconds=5)


def liquid_market() -> str:
    """An allowed, funded market with a YES ask of at most 10¢, 5+ contracts at the bid and the ask, closing 2+ hours out."""
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


def settle_in(s: float = 3.0) -> None:
    """Kalshi's fills and positions lag an order by a moment; the SDK waits for its own orders only."""
    time.sleep(s)


def check(label: str, want: Counter[str], *, repair: bool = False) -> Reconciliation:
    r = client.reconcile(since=start_at, repair=repair)
    got = Counter(m.kind for m in r.mismatches)
    print(f"\n{label}")
    for m in r.mismatches:
        print(f"    {m.kind}: {m.message}")
    for f in r.repaired:
        print(
            f"    + repaired: {f.action} {f.contracts:g} {f.side} @ {f.price} (order {f.order_id}, fill {f.venue_fill_id})"
        )
    assert got == want, f"expected {dict(want)}, got {dict(got)}"
    print(f"  ✓ {dict(got) or 'no mismatch'}" + (f", {len(r.repaired)} fill(s) repaired" if repair else ""))
    return r


def net_yes(r: Reconciliation, market: str) -> tuple[float | None, float | None]:
    p = [m for m in r.of("position") if m.market == market]
    return (p[0].store, p[0].venue_says) if p else (None, None)


start = client.balances()["kalshi"].cash
t = liquid_market()
print(f"demo balance ${start:.4f}; market {t}")

# 0. a fresh store on a quiet account
check("0. fresh store", Counter())

# 1. the SDK buys 1 YES
ask = client.book(t, venue="kalshi").outcome("yes").best_ask
assert ask is not None
b1 = client.buy(venue="kalshi", market=t, side="yes", price=ask.price, size=1)
assert b1.status == "filled", b1
settle_in()
check("1. the SDK bought 1 YES", Counter())

# 2. 1 YES bought outside the SDK
placed, _ = outside.place(Order(venue="kalshi", market=t, side="yes", price=ask.price, size=1))
assert placed.filled == 1, placed
settle_in()
r = check("2. 1 YES bought outside the SDK", Counter({"outside_fill": 1, "position": 1}))
assert net_yes(r, t) == (1, 2), net_yes(r, t)
assert r.of("outside_fill")[0].venue_order_id == placed.venue_order_id
print(f"  ✓ outside fill from Kalshi order {placed.venue_order_id}; position store +1, Kalshi +2")

# 3a. a crash mid-order: the order was saved as pending and its fill never reached the store
b2 = client.buy(venue="kalshi", market=t, side="yes", price=ask.price, size=1)
# 3b. a fill row lost from an order the store shows as filled
b3 = client.buy(venue="kalshi", market=t, side="yes", price=ask.price, size=1)
assert b2.status == b3.status == "filled", (b2, b3)
crashed = b2.model_copy(update={"status": "pending", "filled": 0.0, "avg_price": None, "fees": None})
client.store.save_order(crashed)
with sqlite3.connect(store) as db:
    gone = db.execute("delete from fills where order_id in (?, ?)", (b2.id, b3.id)).rowcount
assert gone == 2, gone
settle_in()
r = check(
    "3. a crash mid-order + a lost fill row",
    Counter({"missed_fill": 2, "stale_order": 1, "outside_fill": 1, "position": 1}),
)
assert net_yes(r, t) == (1, 4), net_yes(r, t)
assert sorted(m.order_id or "" for m in r.of("missed_fill")) == sorted([b2.id or "", b3.id or ""])
r = check("3. repair=True", Counter({"outside_fill": 1, "position": 1}), repair=True)
assert sorted(f.order_id for f in r.repaired) == sorted([b2.id or "", b3.id or ""])
assert net_yes(r, t) == (3, 4), net_yes(r, t)
print("  ✓ both missed fills back in the store; the outside fill is still reported, never added")

# 4. an outside resting order, and an SDK resting order canceled outside the SDK
expires = datetime.now(UTC) + timedelta(minutes=5)


def resting() -> Order:
    return Order(
        venue="kalshi",
        market=t,
        side="yes",
        price=0.01,
        size=1,
        tif="gtc",
        post_only=True,
        expires_at=expires,
    )


theirs, _ = outside.place(resting())
mine = client.send(
    client.order(
        venue="kalshi",
        market=t,
        side="yes",
        price=0.01,
        size=1,
        tif="gtc",
        post_only=True,
        expires_at=expires,
    )
)
assert theirs.status == mine.status == "open", (theirs, mine)
outside.cancel(mine.model_copy())
settle_in(2)
r = check(
    "4. an outside resting order + an SDK order canceled outside the SDK",
    Counter({"outside_order": 1, "stale_order": 1, "outside_fill": 1, "position": 1}),
)
assert r.of("outside_order")[0].venue_order_id == theirs.venue_order_id
assert r.of("stale_order")[0].order_id == mine.id
outside.cancel(theirs)
settle_in(2)
check(
    "4. outside order canceled, repair=True (sync reads the stale order)",
    Counter({"outside_fill": 1, "position": 1}),
    repair=True,
)
assert client.store.order(mine.id or "").status == "canceled"  # type: ignore[union-attr]

# 5. the command line
print("\n5. python -m uselayer reconcile --store <the proof's store>")
code = cli.main(["reconcile", "--store", store, "--since", start_at.isoformat()])
assert code == 1, code
print(f"  ✓ exit code {code}")

# 6. sell everything back: the SDK its 3, outside its 1
bid = client.book(t, venue="kalshi").outcome("yes").best_bid
assert bid is not None
sold = client.sell(venue="kalshi", market=t, side="yes", price=bid.price, size=3)
assert sold.filled == 3, sold
out_sold, _ = outside.place(
    Order(venue="kalshi", market=t, side="yes", action="sell", price=bid.price, size=1)
)
assert out_sold.filled == 1, out_sold
settle_in()
r = check("6. everything sold back", Counter({"outside_fill": 2}))
print("  ✓ positions agree (0 and 0); the two outside fills stay reported")
client.close()
end = Client(mode="live", kalshi=key, store=store, on_alert=alerts.append).balances()["kalshi"].cash
print(f"\ndemo balance ${end:.4f} (change ${end - start:+.4f}); alerts: {Counter(a['kind'] for a in alerts)}")
