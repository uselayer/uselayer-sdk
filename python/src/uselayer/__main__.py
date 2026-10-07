"""``python -m uselayer kill | resume | status`` — the kill switch from any terminal on this machine.

python -m uselayer kill                  # press it for paper and live
python -m uselayer resume --mode paper   # turn it off again
python -m uselayer status

``python -m uselayer record`` records every book change and trade to a file (see :mod:`uselayer.record`),
from Polymarket US slugs and Kalshi tickers in any mix:

python -m uselayer record slug-a KXSAMPLE-26OCT04-T50 --out ticks.jsonl --minutes 60

``python -m uselayer reconcile`` compares the live store with what each venue reports (see
:meth:`uselayer.Client.reconcile`); it exits 1 when anything differs:

python -m uselayer reconcile              # report only
python -m uselayer reconcile --repair     # also add fills the store missed for orders the SDK sent
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .client import Admin
from .store import default_path

NAMES = {"polymarket_us": "Polymarket US", "kalshi": "Kalshi"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["record"]:
        return _record(args[1:])
    if args[:1] == ["reconcile"]:
        return _reconcile(args[1:])
    p = argparse.ArgumentParser(prog="python -m uselayer", description="The uselayer kill switch.")
    p.add_argument("command", choices=["kill", "resume", "status"])
    p.add_argument("--mode", choices=["paper", "live", "all"], default="all")
    p.add_argument("--store", help="path to a store file (default ~/.uselayer/<mode>.db)")
    a = p.parse_args(args)
    modes = ["paper", "live"] if a.mode == "all" else [a.mode]
    for mode in modes:
        if a.store is None and not default_path(mode).exists() and a.command != "kill":
            print(f"· {mode}: no store at {default_path(mode)}")
            continue
        admin = Admin(mode=mode, store=a.store)  # type: ignore[arg-type]
        if a.command == "kill":
            admin.kill()
            print(f"✓ {mode}: kill switch on. Running clients stop sending within about a second.")
            print("  Next: python -m uselayer resume --mode " + mode)
        elif a.command == "resume":
            admin.resume()
            print(f"✓ {mode}: kill switch off.")
        else:
            print(json.dumps(admin.status(), indent=1, default=str))
    return 0


def _record(argv: list[str]) -> int:
    from .errors import VenueError
    from .events import MarketEvent, StreamGap
    from .record import STREAM_VENUES, market_venue, record_stream

    p = argparse.ArgumentParser(
        prog="python -m uselayer record",
        description="Record every book change and trade from Polymarket US's and Kalshi's live streams "
        "to a file. Polymarket US needs POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY; Kalshi needs "
        "KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH (a read-only key is enough). Ctrl-C stops it.",
    )
    p.add_argument(
        "markets", nargs="+", help="Polymarket US slugs (or slug:short) and Kalshi tickers (in capitals)"
    )
    p.add_argument(
        "--venue", choices=STREAM_VENUES, help="read every id as this venue's (default: from the id)"
    )
    p.add_argument("--out", default="ticks.jsonl", help="file to append to (default ticks.jsonl)")
    p.add_argument("--minutes", type=float, help="stop after this many minutes (default: run until Ctrl-C)")
    a = p.parse_args(argv)

    counts = {"book": 0, "trade": 0, "status": 0, "gap": 0}
    last_print = 0.0

    def progress(e: MarketEvent) -> None:
        nonlocal last_print
        counts[e.kind] = counts.get(e.kind, 0) + 1
        if isinstance(e, StreamGap):
            print(f"  ! gap {e.market}: {e.seconds:.1f} s ({e.reason})", flush=True)
        if time.monotonic() - last_print >= 10:
            last_print = time.monotonic()
            print(
                f"  {datetime.now(UTC):%H:%M:%S} {counts['book']} books · {counts['trade']} trades · "
                f"{counts['gap']} gaps",
                flush=True,
            )

    def alert(e: dict[str, Any]) -> None:
        name = NAMES.get(str(e.get("venue")), str(e.get("venue")))
        if e.get("kind") == "stream_disconnected":
            print(f"  ✗ {name} disconnected: {e.get('error')}. Reconnecting…", flush=True)
        elif e.get("kind") == "stream_reconnected":
            print(f"  ✓ {name} reconnected", flush=True)

    until = f"for {a.minutes:g} min" if a.minutes else "until Ctrl-C"
    per = Counter(a.venue or market_venue(m) for m in dict.fromkeys(a.markets))
    target = " + ".join(f"{n} {NAMES[v]}" for v, n in sorted(per.items()))
    print(f"● recording {target} market(s) → {a.out} ({until})", flush=True)
    try:
        s = record_stream(
            a.markets,
            a.out,
            venue=a.venue,
            duration_s=a.minutes * 60 if a.minutes else None,
            on_event=progress,
            on_alert=alert,
        )
    except VenueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    gap_s = sum(g.seconds for g in s.gaps)
    print(
        f"✓ {s.books} books, {s.trades} trades, {len(s.gaps)} gap(s) ({gap_s:.1f} s), "
        f"{s.reconnects} reconnect(s) → {s.path}"
    )
    print(f"  Next: Client(mode='backtest', books=load_books({s.path!r})).replay(on_book)")
    return 0


def _reconcile(argv: list[str]) -> int:
    from .client import Client
    from .errors import VenueError

    p = argparse.ArgumentParser(
        prog="python -m uselayer reconcile",
        description="Compare the live store's fills, positions and open orders with what Polymarket US and "
        "Kalshi report, and list every difference. Uses the keys in POLYMARKET_US_KEY_ID / "
        "POLYMARKET_US_SECRET_KEY and KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH. Exits 1 if anything differs.",
    )
    p.add_argument("--store", help="path to the live store (default ~/.uselayer/live.db)")
    p.add_argument("--since", help="compare fills from this ISO time on (default: the store's first order)")
    p.add_argument(
        "--repair", action="store_true", help="add fills the store missed for orders this SDK sent"
    )
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = p.parse_args(argv)
    path = a.store or str(default_path("live"))
    if a.store is None and not default_path("live").exists():
        print(f"✗ no live store at {path}: nothing to compare yet.", file=sys.stderr)
        return 2
    since = datetime.fromisoformat(a.since.replace("Z", "+00:00")) if a.since else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    try:
        with Client(mode="live", store=path, on_alert=lambda e: None) as client:
            names = " + ".join(NAMES.get(v, v) for v in client._live)
            print(f"● reconciling {path} with {names}", flush=True)
            r = client.reconcile(since=since, repair=a.repair)
    except VenueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    if a.json:
        print(r)
        return 0 if r.ok else 1
    for venue, c in r.checked.items():
        print(
            f"  {NAMES.get(venue, venue)}: {c['venue_fills']} venue fills and {c['store_fills']} store fills "
            f"since {r.since:%Y-%m-%d %H:%M} UTC, {c['positions']} open position(s), {c['open_orders']} open order(s)"
        )
    for f in r.repaired:
        print(
            f"  + added {f.venue} fill {f.venue_fill_id}: {f.action} {f.contracts:g} {f.side} on {f.market}"
        )
    if r.settled:
        print(f"  · not compared, settled on the venue: {', '.join(r.settled)}")
    if r.ok:
        print("✓ the store matches every venue")
        return 0
    for m in r.mismatches:
        print(f"  ✗ {m.kind} · {NAMES.get(m.venue, m.venue)} · {m.market}: {m.message}")
    print(f"✗ {len(r.mismatches)} mismatch(es)")
    if r.of("missed_fill") and not a.repair:
        print("  Next: python -m uselayer reconcile --repair   (adds the missed fills of SDK orders)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
