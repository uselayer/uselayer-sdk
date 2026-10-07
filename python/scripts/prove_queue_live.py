"""Roadmap 18.7, real-order check: tiny post-only buys on Polymarket US, with every tick recorded.

REAL MONEY. The account owner runs this from their own terminal; nothing else may. Every order is a
post-only (maker) buy at the best bid, so it joins the back of the line and never takes an offer.

Limits (owner, 2026-10-04), enforced twice: by the SDK's guardrails (``budget`` $8, ``max_daily_loss``
$3, ``max_position`` $2 a market, kill switch) and by this script (at most 10 contracts a market, $7.50
of orders in total, post-only only). At the end, or on Ctrl-C or any error, each order this run placed
that is still resting is cancelled, one by one, and the script checks none of them is left open. It
never cancels orders you placed elsewhere (no ``cancel_all()``). Filled buys are real positions held
until their markets settle: the most this run can lose is what it spent (at most $7.50). The $3
daily-loss limit only blocks new orders.

Keys stay in the environment, never in a file:

    cd python
    POLYMARKET_US_KEY_ID="$(getkey polymarket-us-key-id)" \\
    POLYMARKET_US_SECRET_KEY="$(getkey polymarket-us-secret-key)" \\
    uv run python scripts/prove_queue_live.py --out ~/uselayer-proof/queue-live

What it does:
1. Scouts: records up to 100 open markets' trades for ``--scout-s`` seconds and keeps the ``--markets``
   busiest with a two-sided book (candidates: the slugs you pass with ``--slugs``, if any).
2. Records every book change and trade on those markets to ``ticks.jsonl`` for the whole run.
3. Places ``--rounds`` rounds, ``--every-s`` apart: one post-only buy per market at the best bid, of
   the market's minimum size, resting ``--hold-s`` seconds. Writes the line it joined (the size already
   at that price) and how long the venue took to answer.
4. Polls fills every 2 s (and writes the window each fill happened in). Then cancels this run's resting orders, checks none is open, and writes ``orders.json``.

Then ``scripts/analyze_queue_live.py <out>`` replays ``ticks.jsonl`` through the fill model with the
same orders and puts the model's fills next to the real ones (no money involved).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from uselayer import Client, PolymarketUS, VenueError
from uselayer.backtest import record_stream
from uselayer.events import TradePrint

MAX_CONTRACTS_PER_MARKET = 10
MAX_TOTAL_COST = 7.50
RULES = {
    "venues": ["polymarket_us"],
    "budget": 8,
    "max_daily_loss": {"amount": 3, "counts": "realized_and_open", "on_breach": "block"},
    "max_position": {"per_market": 2},
    "order_ttl_s": 900,
}


def now() -> datetime:
    return datetime.now(UTC)


def log(*a: Any) -> None:
    print(f"[{now():%H:%M:%S}]", *a, flush=True)


def scout(client: Client, seconds: float, want: int, candidates: list[str] | None) -> list[str]:
    """The busiest markets by trades printed in ``seconds``, each with a two-sided book.

    Candidates are ``--slugs`` when given, else the first 100 open markets the venue lists.
    """
    markets = candidates or [m.market for m in client.markets(venue="polymarket_us", limit=100)]
    log(f"scouting {len(markets)} open markets for {seconds:.0f} s")
    trades: Counter[str] = Counter()

    def count(e: Any) -> None:
        if isinstance(e, TradePrint):
            trades[e.market] += 1

    record_stream(markets, Path("/dev/null"), duration_s=seconds, on_event=count, check_markets=False)
    picked = []
    for m, n in trades.most_common():
        if n < 3 or len(picked) == want:
            break
        ob = client.book(m).outcome("yes")
        if ob.best_bid and ob.best_ask and 0.05 <= ob.best_bid.price <= 0.90:
            picked.append(m)
            log(f"  {m}: {n} trades, bid {ob.best_bid.price} × {ob.best_bid.size:g}")
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--slugs", nargs="*", default=None)
    ap.add_argument("--markets", type=int, default=6)
    ap.add_argument("--scout-s", type=float, default=90)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--every-s", type=float, default=240)
    ap.add_argument("--hold-s", type=float, default=360)
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    client = Client(mode="live", polymarket_us=PolymarketUS.from_env(), rules=RULES)
    if client.killed:
        raise SystemExit(
            "The kill switch is on. A person runs `python -m uselayer resume` first, then this again."
        )
    markets = scout(client, args.scout_s, args.markets, args.slugs)
    if not markets:
        raise SystemExit("No busy two-sided market found. Try again later or pass --slugs.")
    log("markets:", markets)

    stop = threading.Event()
    run_s = args.rounds * args.every_s + args.hold_s + 60
    recorder = threading.Thread(
        target=record_stream,
        args=(markets, out / "ticks.jsonl"),
        kwargs={"duration_s": run_s, "stop": stop},
        daemon=True,
    )
    recorder.start()
    time.sleep(5)  # the recording has the books before the first order

    placed: list[dict[str, Any]] = []
    spent = 0.0
    per_market: Counter[str] = Counter()
    seen_fills: set[str] = set()
    windows: dict[str, dict[str, str]] = {}
    last_check = now()
    try:
        for r in range(args.rounds):
            for m in markets:
                info = client.market(m)
                book = client.book(m)
                bid = book.outcome("yes").best_bid
                if bid is None:
                    log(f"{m}: no bid, skipped")
                    continue
                size = max(info.min_size, 1.0)
                cost = bid.price * size
                if per_market[m] + size > MAX_CONTRACTS_PER_MARKET or spent + cost > MAX_TOTAL_COST:
                    log(f"{m}: would pass the script's limits, skipped")
                    continue
                sent = now()
                try:
                    o = client.buy(
                        venue="polymarket_us",
                        market=m,
                        side="yes",
                        price=bid.price,
                        size=size,
                        tif="gtc",
                        post_only=True,
                        expires_at=sent + timedelta(seconds=args.hold_s),
                    )
                except VenueError as e:
                    log(f"{m}: not placed: {e.code} {e.message}")
                    continue
                acked = now()
                spent += cost
                per_market[m] += size
                placed.append(
                    {
                        "order_id": o.id,
                        "venue_order_id": o.venue_order_id,
                        "market": m,
                        "price": bid.price,
                        "size": size,
                        "status_at_ack": o.status,
                        "sent_at": sent.isoformat(),
                        "acked_at": acked.isoformat(),
                        "answer_ms": round((acked - sent).total_seconds() * 1000),
                        "expires_at": (sent + timedelta(seconds=args.hold_s)).isoformat(),
                        "line_ahead_at_placement": bid.size,
                        "book_as_of": book.as_of.isoformat(),
                    }
                )
                log(
                    f"{m}: buy {size:g} @ {bid.price} rests behind {bid.size:g} ({o.status}, {placed[-1]['answer_ms']} ms)"
                )
            deadline = time.time() + (args.every_s if r < args.rounds - 1 else args.hold_s)
            while time.time() < deadline:
                time.sleep(2)
                checked = now()  # a fill seen now happened after this check began
                client.sync()
                mine = {p["order_id"] for p in placed} | {p["venue_order_id"] for p in placed}
                for f in client.fills():
                    if f.order_id not in mine:
                        continue
                    key = f"{f.order_id}:{f.at.isoformat()}:{f.contracts}"
                    if key not in seen_fills:
                        seen_fills.add(key)
                        # The SDK stamps a live fill when it sees it, so the venue filled it in this window.
                        windows.setdefault(
                            f.order_id, {"after": last_check.isoformat(), "by": now().isoformat()}
                        )
                        log(f"FILL {f.market} {f.contracts:g} @ {f.price} ({f.role})")
                last_check = checked
    finally:
        # Only this run's orders: cancel_all() would cancel every open order on the account,
        # including ones placed on the website.
        mine = {p["order_id"] for p in placed} | {p["venue_order_id"] for p in placed}
        still_open: list[Any] = []
        for _ in range(3):
            client.sync()
            still_open = [o for o in client.orders() if o.id in mine or o.venue_order_id in mine]
            if not still_open:
                break
            log(f"cancelling {len(still_open)} resting order(s) from this run")
            for o in still_open:
                try:
                    client.cancel(o)
                except VenueError as e:
                    log(f"cancel {o.market} failed: {e.code} {e.message}")
            time.sleep(2)
        log("this run's orders still open:", len(still_open))
        stop.set()
        recorder.join(timeout=30)
        result = {
            "markets": markets,
            "orders": placed,
            "fills": [f.to_dict() for f in client.fills() if f.order_id in mine],
            "final_orders": [
                o.to_dict() for o in client.orders(open=False) if o.id in mine or o.venue_order_id in mine
            ],
            "open_orders_left": [o.to_dict() for o in still_open],
            "positions": [p.to_dict() for p in client.positions()],
            "spent_at_most": round(spent, 2),
            "fill_windows": windows,
        }
        (out / "orders.json").write_text(json.dumps(result, indent=2, default=str))
        log(f"wrote {out / 'orders.json'} and {out / 'ticks.jsonl'}")
        if still_open:
            log("WARNING: this run's orders still open. Cancel them in the Polymarket US app.")


if __name__ == "__main__":
    main()
