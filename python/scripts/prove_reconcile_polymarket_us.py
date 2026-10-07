"""Reconciliation on a real Polymarket US account, READ ONLY: no order is sent, nothing is repaired.

    POLYMARKET_US_KEY_ID=... POLYMARKET_US_SECRET_KEY=... python scripts/prove_reconcile_polymarket_us.py [hours]

A fresh, empty store is compared with the account, so every trade in the last ``hours`` (default 24)
must show as an outside fill, and every open position as a position the store doesn't have. The fills
must add up to the venue's position for each market whose trades all fall in that window.
"""

import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from uselayer import Client
from uselayer.reconcile import yes_delta

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
store = str(Path(tempfile.mkdtemp()) / "live.db")
client = Client(mode="live", store=store, on_alert=lambda e: None)
assert list(client._live) == ["polymarket_us"], list(client._live)
venue = client._live["polymarket_us"]
# Reconciling sends nothing: refuse any write, so this script can't place or cancel by mistake.
read = venue._call


def reads_only(method: str, *a: Any, **kw: Any) -> Any:
    if method != "GET":
        raise SystemExit(f"refused {method}: this proof only reads")
    return read(method, *a, **kw)


venue._call = reads_only  # type: ignore[method-assign]

since = datetime.now(UTC) - timedelta(hours=hours)
r = client.reconcile(since=since)
kinds = Counter(m.kind for m in r.mismatches)
print(f"Polymarket US, read only, since {since:%Y-%m-%d %H:%M} UTC: {r.checked['polymarket_us']}")
for m in r.mismatches:
    print(f"  {m.kind}: {m.market}: {m.message}")
fills = [m.fill for m in r.of("outside_fill") if m.fill is not None]
every = list(venue.fills())
assert kinds["outside_fill"] == len(fills) == len(venue.fills(since=since)), kinds
assert not (set(kinds) - {"outside_fill", "position", "outside_order"}), kinds
for m in r.of("position"):
    assert m.store == 0, m
    window = sum(yes_delta(f) for f in fills if f.market == m.market)
    alltime = sum(yes_delta(f) for f in every if f.market == m.market)
    agree = abs(alltime - (m.venue_says or 0)) < 1e-6
    print(
        f"  ✓ {m.market}: venue {m.venue_says:+g}; fills in the window {window:+g}, all-time fills {alltime:+g}"
        + (" (all-time fills add up to the venue's position)" if agree else " (DIFFERENT)")
    )
    assert agree, m
print(f"✓ {dict(kinds) or 'no mismatch'}; nothing was sent")
client.close()
