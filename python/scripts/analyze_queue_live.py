"""Roadmap 18.7: the fill model's prediction next to the real fills from ``prove_queue_live.py``.

No money involved: this replays the recorded ``ticks.jsonl`` in backtest mode, places the same orders
(same market, price, size, expiry) at the moment each was really placed, and compares what the model
filled, and when, with what Polymarket US really filled. Both cancel settings are run.

    uv run python scripts/analyze_queue_live.py ~/uselayer-proof/queue-live
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from uselayer import Book, Client
from uselayer.backtest import load_books
from uselayer.events import TradePrint
from uselayer.resting import Line


def t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def stamp(e: Any) -> datetime:
    """This machine's clock when it has one (to line up with when the order was placed)."""
    return e.received_at or e.as_of


def model(
    events: list[Any], orders: list[dict[str, Any]], cancels: str, latency: float | None = None
) -> dict[str, dict[str, Any]]:
    """The model's fills. With ``latency`` None each order is placed when the SDK got the venue's
    answer; with a latency it's sent when it was really sent and reaches the book that much later."""
    key = "acked_at" if latency is None else "sent_at"
    todo = sorted(orders, key=lambda o: o[key])
    out: dict[str, dict[str, Any]] = {}
    ids: dict[str, str] = {}

    def place(c: Client, b: Book) -> None:
        for o in todo:
            if o["order_id"] in out or o["market"] != b.market or stamp(b) < t(o[key]):
                continue
            try:
                bo = c.buy(
                    venue="polymarket_us",
                    market=o["market"],
                    side="yes",
                    price=o["price"],
                    size=o["size"],
                    tif="gtc",
                    post_only=True,
                    expires_at=b.as_of + (t(o["expires_at"]) - t(o[key])),
                )
            except Exception as e:
                out[o["order_id"]] = {"status": f"not placed: {e}"}
                continue
            body = c.store.line(bo.id or bo.client_id)
            out[o["order_id"]] = {
                "sent_at": t(o["sent_at"]),
                "status": bo.status,
                "line_ahead_est": Line.from_json(body).ahead_est / 1e6 if body else None,
            }
            ids[bo.id or bo.client_id] = o["order_id"]

    bt = Client(
        mode="backtest",
        books=events,
        rules={"order_ttl_s": 3600},
        queue_cancels=cancels,  # type: ignore[arg-type]
        order_latency_s=latency or 0.0,
    )
    bt.replay(place)
    for f in bt.fills():
        r = out[ids[f.order_id]]
        r["filled"] = r.get("filled", 0) + f.contracts
        r.setdefault("fill_at", f.at)
        r.setdefault("first_fill_s", round((f.at - r["sent_at"]).total_seconds(), 1))
    return out


def main() -> None:
    d = Path(sys.argv[1]).expanduser()
    latency = float(sys.argv[sys.argv.index("--latency") + 1]) if "--latency" in sys.argv else None
    run = json.loads((d / "orders.json").read_text())
    events = load_books(d / "ticks.jsonl")
    orders = run["orders"]
    real: dict[str, dict[str, Any]] = {}
    by_venue_id = {o["venue_order_id"]: o["order_id"] for o in orders}
    for f in run["fills"]:
        oid = (
            f["order_id"]
            if f["order_id"] in {o["order_id"] for o in orders}
            else by_venue_id.get(f["order_id"])
        )
        if oid is None:
            continue
        o = next(x for x in orders if x["order_id"] == oid)
        r = real.setdefault(oid, {"filled": 0.0})
        r["filled"] += f["contracts"]
        # A live fill's time is when the SDK saw it (the script polls every 3 s), not the venue's.
        r.setdefault("fill_at", t(f["at"]))
        r.setdefault("first_fill_s", round((t(f["at"]) - t(o["sent_at"])).total_seconds(), 1))
    prop = model(events, orders, "proportional", latency)
    behind = model(events, orders, "behind", latency)

    how = "placed when the SDK got the venue's answer" if latency is None else f"order latency {latency} s"
    print(f"Model: {how}. Fill times are seconds after the order was sent.")
    print()
    print(
        "| Market | Buy | Line at placement (book / model) | Trades hitting its price (n, contracts) "
        "| Model, proportional | Model, behind | Real (seen by the script) | SDK call |"
    )
    print("|---|---|---|---|---|---|---|---|")
    agree = 0
    for o in orders:
        end = t(o["expires_at"])
        hits = [
            e
            for e in events
            if isinstance(e, TradePrint)
            and e.market == o["market"]
            and abs(e.price - o["price"]) < 1e-9
            and e.aggressor != "buy"
            and t(o["acked_at"]) < stamp(e) <= end
        ]
        p, b, r = prop.get(o["order_id"], {}), behind.get(o["order_id"], {}), real.get(o["order_id"], {})

        def cell(x: dict[str, Any]) -> str:
            if "filled" not in x:
                return x.get("status", "0") if str(x.get("status", "")).startswith("not") else "0"
            return f"{x['filled']:g} after {x['first_fill_s']} s"

        agree += bool(p.get("filled")) == bool(r.get("filled"))
        print(
            f"| {o['market']} | {o['size']:g} @ {o['price']} | {o['line_ahead_at_placement']:g} / "
            f"{p.get('line_ahead_est', '?')} | {len(hits)}, {sum(h.size for h in hits):g} | {cell(p)} | "
            f"{cell(b)} | {cell(r)} | {o['answer_ms']} ms |"
        )
    ms = sorted(o["answer_ms"] for o in orders)
    print()
    print(f"Orders: {len(orders)}. Filled or not, the default model agreed with the venue on {agree}.")
    windows = run.get("fill_windows", {})
    for oid, r in real.items():
        p = prop.get(oid, {})
        if "fill_at" not in p:
            continue
        gap = (r["fill_at"] - p["fill_at"]).total_seconds()
        line = f"  {oid[:8]}: model filled at {p['fill_at']:%H:%M:%S.%f}; the script saw the real fill {gap:.1f} s later"
        w = windows.get(oid) or next(
            (windows[v] for v in windows if v in by_venue_id and by_venue_id[v] == oid), None
        )
        if w:
            after, by = t(w["after"]), t(w["by"])
            if after <= p["fill_at"] <= by:
                line += (
                    f"; the venue filled it between {after:%H:%M:%S.%f} and {by:%H:%M:%S.%f}: model inside"
                )
            else:
                off = (after - p["fill_at"]) if p["fill_at"] < after else (p["fill_at"] - by)
                side = "early" if p["fill_at"] < after else "late"
                line += (
                    f"; venue window {after:%H:%M:%S}–{by:%H:%M:%S}: model {off.total_seconds():.1f} s {side}"
                )
        print(line)
    if ms:
        print(f"SDK call (whole buy()): median {ms[len(ms) // 2]} ms, max {ms[-1]} ms.")
    print(f"Open orders left after the run: {len(run['open_orders_left'])}. Positions: {run['positions']}")


if __name__ == "__main__":
    main()
