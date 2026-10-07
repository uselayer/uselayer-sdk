"""Polymarket US live smoke test: run it yourself, with your own key.

It places ONE post-only buy at half the best bid, which can't fill, reads it back, then cancels it.
At the minimum size its worst-case cost is a few cents, and it's canceled within seconds.

    POLYMARKET_US_KEY_ID=... POLYMARKET_US_SECRET_KEY=... python scripts/smoke_polymarket_us.py --yes

Without --yes it does everything except place the order.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from uselayer import Admin, Client, PolymarketUS, VenueError


def step(label: str, fn):  # type: ignore[no-untyped-def]
    print(f"… {label}", end="", flush=True)
    t = time.monotonic()
    try:
        out = fn()
    except VenueError as e:
        print(f"\r✗ {label}: {e.code}: {e.message}")
        if e.next:
            print(f"  Next: {e.next}")
        sys.exit(1)
    print(f"\r✓ {label} ({time.monotonic() - t:.1f}s)")
    return out


place = "--yes" in sys.argv
store = Path(tempfile.mkdtemp()) / "smoke-live.db"
key = step("read the key from POLYMARKET_US_KEY_ID / POLYMARKET_US_SECRET_KEY", PolymarketUS.from_env)
client = step("connect in live mode", lambda: Client(mode="live", polymarket_us=key, store=store))
if client.killed:
    # A brand-new store plus existing positions or orders on the account starts killed (on purpose).
    # This test uses its own throwaway store, so resuming it here is safe.
    print(
        "  · started killed because your account already has positions or open orders; resuming this test's own store"
    )
    Admin(mode="live", store=store).resume()

bal = step("read your balance", lambda: client.balances()["polymarket_us"])
print(f"  cash ${bal.cash:,.2f}")


def pick() -> tuple[str, float, float, float]:
    for m in client.markets(limit=100):
        b = client.book(m.slug).outcome("yes")
        if b.best_bid and b.best_bid.price >= 0.10 and b.best_ask:
            price = round((b.best_bid.price / 2) // m.tick_size * m.tick_size, 6)
            return m.slug, price, m.min_size, client.book(m.slug).age_s(client._now())
    raise VenueError("not_found", "No open market with a bid of at least 10¢ right now.")


slug, price, size, age = step("pick a market and read a fresh book (WebSocket)", pick)
print(f"  {slug}: post-only buy YES {size} @ {price} (half the best bid); book age {age:.1f}s")
if not place:
    print("Dry run: add --yes to place and cancel the order.")
    sys.exit(0)

order = client.order(
    venue="polymarket_us", market=slug, side="yes", price=price, size=size, tif="gtc", post_only=True
)
preview = step("preview it (guardrails, fees)", lambda: client.preview(order))
if not preview.allowed:
    print(f"✗ the guardrails would block it: {preview.blocked_by} {preview.problems}")
    sys.exit(1)
sent = step("place it", lambda: client.send(order))
print(f"  status {sent.status}, venue id {sent.venue_order_id}")
assert sent.status == "open", "a post-only order far below the market should rest"
listed = step("read it back from the venue", lambda: [o.venue_order_id for o in client.orders()])
assert sent.venue_order_id in listed, "the order isn't among the open orders"
canceled = step("cancel it", lambda: client.cancel(sent))
print(f"  status {canceled.status}")
assert canceled.status == "canceled"
still = step("check it's gone", lambda: [o.venue_order_id for o in client.orders()])
assert sent.venue_order_id not in still
print(f"✓ smoke test passed. Fills recorded: {len(client.fills())} (expected 0)")
