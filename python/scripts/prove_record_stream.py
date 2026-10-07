"""Proof for record_stream(): record live Polymarket US markets, force a disconnect, replay, check books.

    POLYMARKET_US_KEY_ID=... POLYMARKET_US_SECRET_KEY=... python scripts/prove_record_stream.py --minutes 60

1. Scans 300 open markets for a minute and picks the 5 whose books change most.
2. Records them with record_stream() for --minutes, and closes the socket at --drop-at minutes, so a
   reconnect and a marked gap must follow.
3. Meanwhile reads each market's REST book every --check-every minutes.
4. Loads the recording into backtest mode and replays it (a resting order that later books may fill).
5. Rebuilds each market's book from the recording at each REST book's moment and compares levels.
"""

from __future__ import annotations

import argparse
import collections
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from websockets.sync.client import connect as ws_connect

from uselayer import Book, Client, StreamGap, TradePrint
from uselayer.backtest import load_books
from uselayer.record import record_stream
from uselayer.venues.polymarket_us_live import PolymarketUS
from uselayer.venues.polymarket_us_stream import PolymarketUSStream

p = argparse.ArgumentParser()
p.add_argument("--minutes", type=float, default=60)
p.add_argument("--drop-at", type=float, default=20)
p.add_argument("--check-every", type=float, default=5)
p.add_argument("--out", default="proof-ticks.jsonl")
a = p.parse_args()

key = PolymarketUS.from_env()
client = Client(store=":memory:")
slugs: list[str] = []
for offset in range(0, 300, 100):
    slugs += [m.market for m in client.markets(limit=100, offset=offset)]
print(f"● scanning {len(slugs)} open markets for 60 s", flush=True)
changes: collections.Counter[str] = collections.Counter()
trades: collections.Counter[str] = collections.Counter()
last: dict[str, Any] = {}
end = time.monotonic() + 60
for e in PolymarketUSStream(key).session(slugs):
    if isinstance(e, Book) and last.get(e.market) != (e.bids, e.asks):
        changes[e.market] += last.get(e.market) is not None
        last[e.market] = (e.bids, e.asks)
    elif isinstance(e, TradePrint):
        trades[e.market] += 1
    if time.monotonic() > end:
        break
# Busy books, and trades too: a trade is worth 20 book changes.
score = collections.Counter({m: changes[m] + 20 * trades[m] for m in set(changes) | set(trades)})
markets = [m for m, _ in score.most_common(5)]
print(
    f"✓ picked {markets} (book changes {[changes[m] for m in markets]}, "
    f"trades {[trades[m] for m in markets]} in the scan)",
    flush=True,
)

out = Path(a.out)
out.unlink(missing_ok=True)
holder: dict[str, Any] = {}


def connect(*args: Any, **kw: Any) -> Any:
    holder["ws"] = ws_connect(*args, **kw)
    return holder["ws"]


stop = threading.Event()
dropped_at: list[datetime] = []


def drop() -> None:
    at = time.time() + a.drop_at * 60  # wall clock: a monotonic wait pauses while the Mac sleeps
    while time.time() < at:
        if stop.wait(1):
            return
    dropped_at.append(datetime.now(UTC))
    print(f"  ! {dropped_at[0]:%H:%M:%S} closing the socket to force a disconnect", flush=True)
    holder["ws"].socket.shutdown(socket.SHUT_RDWR)


rest: list[Book] = []


def check_rest() -> None:
    while not stop.wait(a.check_every * 60):
        for m in markets:
            try:
                rest.append(client.book(m))
            except Exception as err:  # a missed check is reported, never fatal
                print(f"  ✗ REST book {m}: {err}", flush=True)


threads = [threading.Thread(target=f, daemon=True) for f in (drop, check_rest)]
for t in threads:
    t.start()
counts: collections.Counter[str] = collections.Counter()
last_print = [time.monotonic()]


def progress(e: Any) -> None:
    counts[e.kind] += 1
    if isinstance(e, StreamGap):
        print(f"  ! gap {e.market}: {e.seconds:.2f} s ({e.reason})", flush=True)
    if time.monotonic() - last_print[0] > 300:
        last_print[0] = time.monotonic()
        print(f"  {datetime.now(UTC):%H:%M:%S} {dict(counts)}", flush=True)


print(f"● recording {len(markets)} markets → {out} for {a.minutes:g} min", flush=True)
s = record_stream(
    markets,
    out,
    polymarket_us=key,
    duration_s=a.minutes * 60,
    ws_connect=connect,
    on_event=progress,
    on_alert=lambda e: print(f"  · {e['kind']}", flush=True),
)
stop.set()
print(
    f"✓ recorded {s.books} books, {s.trades} trades, {s.statuses} statuses, {len(s.gaps)} gaps, "
    f"{s.reconnects} reconnects, {out.stat().st_size / 1e6:.2f} MB",
    flush=True,
)

# ---- 2. the forced disconnect shows as a marked gap ----
assert dropped_at, "the disconnect never fired"
assert s.reconnects >= 1 and {g.market for g in s.gaps} == set(markets), s.gaps
forced = [g for g in s.gaps if g.as_of <= dropped_at[0] <= g.until]
assert {g.market for g in forced} == set(markets), (s.gaps, dropped_at)
# The gap starts at the last proof the line was up: within a ping interval (5 s) of the drop.
assert all(dropped_at[0] - g.as_of <= timedelta(seconds=6) for g in forced), forced
print(
    f"✓ forced disconnect at {dropped_at[0]:%H:%M:%S.%f}: gaps "
    f"{[f'{g.market} {g.seconds:.2f}s' for g in forced]}; {len(s.gaps) - len(forced)} other gap(s)",
    flush=True,
)

# ---- 1. the recording replays in backtest ----
events = load_books(out)
target = markets[0]


def on_book(c: Client, b: Book) -> None:
    if b.market == target and not c.orders() and b.bids:
        c.buy(venue=b.venue, market=b.market, side="yes", price=b.bids[0].price, size=1, tif="gtc")


bt = Client(mode="backtest", books=events, rules={"order_ttl_s": 3600})
result = bt.replay(on_book)
print(
    f"✓ backtest replay: {result['books']} books, {result['trades']} trades, {result['gaps']} gaps, "
    f"{result['fills']} fills",
    flush=True,
)
assert result["books"] == s.books and result["gaps"] == len(s.gaps) and result["trades"] == s.trades

# ---- 3. a rebuilt book matches the REST book at the same moment ----
by_market: dict[str, list[Book]] = collections.defaultdict(list)
for e in events:
    if isinstance(e, Book):
        by_market[e.market].append(e)
gaps = [(g.market, g.as_of, g.until) for g in s.gaps]


# The REST copy comes through a cache (up to 30 s) and its time is from a 1-second Date header, so
# the check finds the recorded state equal to it and measures how long before the REST time that
# state ended (0 = still the book at that moment).
def levels(b: Book) -> tuple[Any, Any]:
    return (b.bids, b.asks)


exact, lags, miss = 0, [], 0
for r in rest:
    if any(m == r.market and lo <= r.as_of <= hi for m, lo, hi in gaps):
        continue
    books = by_market[r.market]
    if not books or books[0].as_of > r.as_of:
        continue
    best = None
    for i, b in enumerate(books):
        if b.as_of > r.as_of + timedelta(seconds=1) or levels(b) != levels(r):
            continue
        ended = books[i + 1].as_of if i + 1 < len(books) else None
        lag = 0.0 if ended is None or ended >= r.as_of else (r.as_of - ended).total_seconds()
        if lag <= 30 and (best is None or lag < best):
            best = lag
    if best is None:
        miss += 1
        at = [b for b in books if b.as_of <= r.as_of][-1]
        print(
            f"  ✗ {r.market} at {r.as_of}: REST {levels(r)[0][:2]}…/{levels(r)[1][:2]}… "
            f"vs recorded {levels(at)[0][:2]}…/{levels(at)[1][:2]}…",
            flush=True,
        )
    elif best == 0:
        exact += 1
    else:
        lags.append(best)
total = exact + len(lags) + miss
print(
    f"✓ REST check: {exact} of {total} equal the recorded book at the same moment; {len(lags)} equal "
    f"a recorded book that ended {', '.join(f'{x:.1f}' for x in sorted(lags))} s before the REST time "
    f"(the REST copy was that stale); {miss} never seen in the recording",
    flush=True,
)
assert exact > 0 and miss == 0
