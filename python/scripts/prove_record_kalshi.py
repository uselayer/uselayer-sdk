"""Proof for record_stream() on Kalshi: live Kalshi and Polymarket US markets in one call, a forced
Kalshi disconnect, a backtest replay, and books rebuilt from the recording against Kalshi's REST book.

    KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... POLYMARKET_US_KEY_ID=... POLYMARKET_US_SECRET_KEY=... \\
        python scripts/prove_record_kalshi.py --minutes 30 --out ~/uselayer-proof/kalshi.jsonl

1. Scans open Kalshi markets for a minute and picks the --kalshi busiest (book changes, and trades
   worth 20 changes each); does the same for --polymarket Polymarket US markets.
2. Records all of them with one record_stream() call for --minutes, and closes the Kalshi socket at
   --drop-at minutes, so a Kalshi reconnect and marked gaps on Kalshi's markets (only) must follow.
3. Meanwhile reads each market's REST book every --check-every minutes.
4. Loads the recording into backtest mode and replays it (a resting order that later books may fill).
5. Rebuilds each market's book from the recording at each REST book's moment and compares levels.

Recording only reads; no order is sent anywhere. The recording is the venues' data: keep it out of
the repo (--out must be outside it) and don't share it.
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

from uselayer import Book, Client, Kalshi, StreamGap, TradePrint
from uselayer.backtest import load_books
from uselayer.http import Http
from uselayer.record import record_stream
from uselayer.venues.kalshi import KalshiLive
from uselayer.venues.kalshi_stream import KalshiStream
from uselayer.venues.polymarket_us_live import PolymarketUS
from uselayer.venues.polymarket_us_stream import PolymarketUSStream

p = argparse.ArgumentParser()
p.add_argument("--minutes", type=float, default=30)
p.add_argument("--drop-at", type=float, default=10)
p.add_argument("--check-every", type=float, default=2)
p.add_argument("--kalshi", type=int, default=4, help="how many Kalshi markets")
p.add_argument("--polymarket", type=int, default=2, help="how many Polymarket US markets")
p.add_argument("--scan", type=float, default=60, help="seconds to watch each venue before picking")
p.add_argument("--out", default="~/uselayer-proof/record-kalshi.jsonl")
a = p.parse_args()

out = Path(a.out).expanduser().resolve()
repo = Path(__file__).resolve().parents[2]
if out.is_relative_to(repo):
    raise SystemExit(f"✗ --out {out} is inside the repo; venue recordings stay outside it.")

kkey, pkey = Kalshi.from_env(), PolymarketUS.from_env()
kalshi = KalshiLive(Http(), kkey)
client = Client(store=":memory:")


def busiest(stream: Any, ids: list[str], n: int, name: str) -> list[str]:
    print(f"● scanning {len(ids)} open {name} markets for {a.scan:g} s", flush=True)
    changes: collections.Counter[str] = collections.Counter()
    trades: collections.Counter[str] = collections.Counter()
    last: dict[str, Any] = {}
    end = time.monotonic() + a.scan
    session = stream.session(ids)
    for e in session:
        if isinstance(e, Book) and last.get(e.market) != (e.bids, e.asks):
            changes[e.market] += last.get(e.market) is not None
            last[e.market] = (e.bids, e.asks)
        elif isinstance(e, TradePrint):
            trades[e.market] += 1
        if time.monotonic() > end:
            break
    session.close()
    score = collections.Counter({m: changes[m] + 20 * trades[m] for m in set(changes) | set(trades)})
    picked = [m for m, _ in score.most_common(n)]
    print(
        f"✓ picked {picked} (book changes {[changes[m] for m in picked]}, trades {[trades[m] for m in picked]})",
        flush=True,
    )
    return picked


k_ids = [m.market for m in kalshi.markets(limit=1000)]
k_markets = busiest(KalshiStream(kkey), k_ids, a.kalshi, "Kalshi")
p_ids: list[str] = []
for offset in range(0, 200, 100):
    p_ids += [m.market for m in client.markets(limit=100, offset=offset)]
p_markets = busiest(PolymarketUSStream(pkey), p_ids, a.polymarket, "Polymarket US")
markets = k_markets + p_markets

out.parent.mkdir(parents=True, exist_ok=True)
out.unlink(missing_ok=True)
sockets: dict[str, Any] = {}


def connect(url: str, *args: Any, **kw: Any) -> Any:
    ws = ws_connect(url, *args, **kw)
    sockets["kalshi" if "kalshi" in url else "polymarket_us"] = ws
    return ws


stop = threading.Event()
dropped_at: list[datetime] = []


def drop() -> None:
    at = time.time() + a.drop_at * 60  # wall clock: a monotonic wait pauses while the Mac sleeps
    while time.time() < at:
        if stop.wait(1):
            return
    dropped_at.append(datetime.now(UTC))
    print(f"  ! {dropped_at[0]:%H:%M:%S} closing the Kalshi socket to force a disconnect", flush=True)
    sockets["kalshi"].socket.shutdown(socket.SHUT_RDWR)


rest: list[Book] = []


def check_rest() -> None:
    while not stop.wait(a.check_every * 60):
        for m in markets:
            try:
                book = kalshi.read_book(m).book if m in k_markets else client.book(m)
                rest.append(book)
            except Exception as err:  # a missed check is reported, never fatal
                print(f"  ✗ REST book {m}: {err}", flush=True)


for f in (drop, check_rest):
    threading.Thread(target=f, daemon=True).start()
counts: collections.Counter[tuple[str, str]] = collections.Counter()
last_print = [time.monotonic()]


def progress(e: Any) -> None:
    counts[(e.venue, e.kind)] += 1
    if isinstance(e, StreamGap):
        print(f"  ! gap {e.venue} {e.market}: {e.seconds:.2f} s ({e.reason})", flush=True)
    if time.monotonic() - last_print[0] > 120:
        last_print[0] = time.monotonic()
        print(f"  {datetime.now(UTC):%H:%M:%S} {dict(counts)}", flush=True)


started = datetime.now(UTC)
print(f"● recording {len(k_markets)} Kalshi + {len(p_markets)} Polymarket US markets → {out}", flush=True)
s = record_stream(
    markets,
    out,
    kalshi=kkey,
    polymarket_us=pkey,
    duration_s=a.minutes * 60,
    ws_connect=connect,
    on_event=progress,
    on_alert=lambda e: print(f"  · {e['venue']} {e['kind']}", flush=True),
)
stop.set()
print(
    f"✓ recorded {s.books} books, {s.trades} trades, {s.statuses} statuses, {len(s.gaps)} gaps, "
    f"{s.reconnects} reconnects, {out.stat().st_size / 1e6:.2f} MB; by venue {dict(counts)}",
    flush=True,
)
asleep = [g for g in s.gaps if "asleep or stalled" in g.reason]
print(
    f"  machine asleep/stalled: {len({(g.as_of, g.until) for g in asleep})} stretch(es), "
    f"{sum(g.seconds for g in asleep) / max(1, len(markets)):.0f} s per market",
    flush=True,
)

# ---- 2. the forced disconnect shows as a marked gap on Kalshi's markets only ----
assert dropped_at, "the disconnect never fired"
forced = [g for g in s.gaps if g.as_of <= dropped_at[0] + timedelta(seconds=1) and dropped_at[0] <= g.until]
assert {g.market for g in forced} == set(k_markets), (forced, dropped_at)
assert all(g.venue == "kalshi" for g in forced)
assert all(dropped_at[0] - g.as_of <= timedelta(seconds=6) for g in forced), forced
pm_gaps = [g for g in s.gaps if g.venue == "polymarket_us"]
print(
    f"✓ forced Kalshi disconnect at {dropped_at[0]:%H:%M:%S.%f}: gaps "
    f"{[f'{g.market} {g.seconds:.2f}s' for g in forced]}; Polymarket US gaps in the run: {len(pm_gaps)} "
    f"({', '.join(sorted({g.reason[:60] for g in pm_gaps})) or 'none'})",
    flush=True,
)
seq_gaps = [g for g in s.gaps if g.reason.startswith("missed messages")]
print(f"  Kalshi seq jumps: {len({g.as_of for g in seq_gaps})}", flush=True)

# ---- 1. the recording replays in backtest ----
events = load_books(out)
target = k_markets[0]


def on_book(c: Client, b: Book) -> None:
    if b.market == target and not c.orders() and b.bids:
        c.buy(venue=b.venue, market=b.market, side="yes", price=b.bids[0].price, size=1, tif="gtc")


bt = Client(mode="backtest", books=events, rules={"order_ttl_s": 3600}, kalshi=kkey)
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


def levels(b: Book) -> tuple[Any, Any]:
    return (b.bids, b.asks)


# A REST book's time is a 1-second Date header (and Polymarket US's goes through a cache), so the
# check finds the recorded state equal to it and measures how long before the REST time that state
# ended (0 = still the book at that moment).
for venue, ids in (("kalshi", k_markets), ("polymarket_us", p_markets)):
    exact, lags, miss = 0, [], 0
    for r in (r for r in rest if r.market in ids):
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
        f"✓ {venue} REST check: {exact} of {total} equal the recorded book at the same moment; {len(lags)} "
        f"equal a recorded book that ended {', '.join(f'{x:.1f}' for x in sorted(lags)) or '-'} s before the "
        f"REST time; {miss} never seen in the recording",
        flush=True,
    )
print(f"  ran {started:%H:%M:%S}–{datetime.now(UTC):%H:%M:%S} UTC", flush=True)
