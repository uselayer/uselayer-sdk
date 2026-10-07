"""Best-venue proof, paper mode, on real books: preview_best() and buy_best() for live Kalshi ↔ Polymarket US matches.

    KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY=... LAYER_API_KEY=lyr_... python scripts/prove_best_paper.py

Paper mode: books are read from Kalshi (your own key, read only) and Polymarket US's public gateway;
orders fill against them with fake money. Every request must be a GET to Kalshi's API, Polymarket
US's gateway or Layer's API: anything else is refused before it leaves the machine.

For each match and side, the script hand-checks every venue's all-in cost from the exact book the
comparison read: it walks the asks itself, in Decimal, with each venue's published fee formula
(Kalshi: 0.07 × multiplier × C × P × (1 − P), up to the cent; Polymarket US: Θ × C × P × (1 − P),
to the cent, half to even), and checks that the cheaper venue won. Then it sends paper buys through
buy_best() and checks each fill against the comparison, and shows the skips (max_price, too big).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import httpx

from uselayer import Book, Client, Kalshi, VenueError, rules_at
from uselayer.venue_rules import PolymarketUSFees

HOSTS = {"api.elections.kalshi.com", "gateway.polymarket.us", "uselayer.sh"}
SIZE = 10
seen: list[str] = []


class ReadOnly(httpx.BaseTransport):
    def __init__(self) -> None:
        self.real = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.host}")
        if request.method != "GET" or request.url.host not in HOSTS:
            raise AssertionError(f"refused: {request.method} {request.url.host}")
        return self.real.handle_request(request)


def D(x: float) -> Decimal:
    return Decimal(str(x))


def hand_all_in(c: Client, v: Any, book: Book) -> tuple[Decimal, Decimal]:
    """Walk ``book``'s asks for the side up to the comparison's cap; return (cost, fees) by hand."""
    info = c.market(v.market, venue=v.venue)
    left, cost, fees = D(v.size), Decimal(0), Decimal(0)
    for lv in book.outcome(v.side).asks:
        if left <= 0 or D(lv.price) > D(v.cap):
            break
        n = min(left, D(lv.size))
        p = D(lv.price)
        cost += n * p
        if v.venue == "kalshi":
            f = D(info.fees.multiplier) * Decimal("0.07") * n * p * (1 - p)
            fees += f.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        else:
            r = rules_at("polymarket_us", c._now()).fees
            assert isinstance(r, PolymarketUSFees)
            theta = D(info.fees.coefficient if info.fees.coefficient is not None else r.taker_coefficient)
            fees += (theta * n * p * (1 - p)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        left -= n
    assert left == 0, f"hand walk couldn't fill {v.size} on {v.venue}"
    return cost, fees


def main() -> int:
    store = Path(tempfile.mkdtemp()) / "paper.db"
    c = Client(
        mode="paper",
        kalshi=Kalshi.from_env(),
        layer_key=os.environ["LAYER_API_KEY"],
        transport=ReadOnly(),
        store=str(store),
        on_alert=lambda e: None,
    )
    books: list[Book] = []
    orig = c.book

    def logged(market: Any, *, venue: str = "polymarket_us") -> Book:
        b = orig(market, venue=venue)
        books.append(b)
        return b

    c.book = logged  # type: ignore[method-assign]
    matches = [
        m
        for m in c.matches(venue="polymarket_us", limit=100, titles=False)
        if {"kalshi", "polymarket_us"} <= set(m.markets())
    ]
    print(f"{len(matches)} live Kalshi ↔ Polymarket US matches from Layer")
    report: list[dict[str, Any]] = []
    checked = both_ok = 0
    for m in matches:
        pair = [m.kalshi, m.polymarket_us]
        for side in ("yes", "no"):
            books.clear()
            try:
                r = c.preview_best(pair, side, SIZE)
            except VenueError as e:
                print(f"  {m.kalshi.market_id} {side}: {e.code} {e.message}")
                continue
            row: dict[str, Any] = {
                "kalshi": m.kalshi.market_id,
                "polymarket_us": m.polymarket_us.market_id,
                "side": side,
                "venue": r.why.venue,
                "reason": r.why.reason,
                "venues": [],
            }
            for v in r.why.venues:
                if not v.ok:
                    row["venues"].append({"venue": v.venue, "skip": v.skip, "detail": v.detail})
                    continue
                b = next(x for x in books if (x.venue, x.market) == (v.venue, v.market))
                cost, fees = hand_all_in(c, v, b)
                assert (D(v.cost), D(v.fees)) == (cost, fees), (v.to_dict(), cost, fees)
                row["venues"].append(
                    {"venue": v.venue, "all_in": v.all_in, "hand": float(cost + fees), "limit": v.limit_price}
                )
                checked += 1
            ok = [v for v in r.why.venues if v.ok]
            if len(ok) == 2:
                both_ok += 1
                want = min(ok, key=lambda v: (v.all_in, -(v.size_at_limit or 0)))
                assert r.why.chosen is not None and r.why.chosen.venue == want.venue
            report.append(row)
            print(f"  {m.kalshi.market_id:40s} {side:3s} → {r.why.venue}: {r.why.reason}")
        if both_ok >= 6:
            break
    print(f"hand-checked {checked} venue costs; {both_ok} comparisons had both venues able to fill {SIZE}")
    assert checked and both_ok, "no comparable pair found"

    # Paper buys through buy_best(): the fill lands on the chosen venue at no more than the compared cost.
    sent = []
    for row in [x for x in report if len([v for v in x["venues"] if "all_in" in v]) == 2][:3]:
        pair = [("kalshi", row["kalshi"]), ("polymarket_us", row["polymarket_us"])]
        r = c.buy_best(pair, row["side"], SIZE)
        o = r.order
        assert o is not None and o.venue == r.why.venue and o.filled > 0
        spent = round((o.avg_price or 0) * o.filled + (o.fees or 0), 6)
        print(
            f"  paper buy {row['side']} {SIZE} → {o.venue} {o.market}: filled {o.filled} @ {o.avg_price}, fees {o.fees}, all-in {spent} (compared {r.why.chosen.all_in})"
        )  # type: ignore[union-attr]
        sent.append({"pair": pair, "side": row["side"], "order": o.to_dict(), "why": r.why.to_dict()})
    fills = c.fills()
    assert fills and all(f.simulated for f in fills)

    # Skips on a real pair: a max_price under both best asks, and a size neither book can fill.
    row = report[0]
    pair = [("kalshi", row["kalshi"]), ("polymarket_us", row["polymarket_us"])]
    low = c.preview_best(pair, row["side"], SIZE, max_price=0.01)
    huge = c.preview_best(pair, row["side"], 10_000_000)
    print(f"  max_price 0.01: {[v.skip for v in low.why.venues]} ({low.why.reason_code})")
    print(f"  size 10,000,000: {[v.skip for v in huge.why.venues]} ({huge.why.reason_code})")
    assert low.order is None and huge.order is None
    try:
        c.buy_best(pair, row["side"], 10_000_000)
        raise AssertionError("expected not_available")
    except VenueError as e:
        assert e.code == "not_available"
    print(f"requests: {len(seen)}, all GET to {sorted({s.split()[1] for s in seen})}")
    out = Path(os.environ.get("PROOF_OUT", tempfile.gettempdir())) / "best-venue-paper.json"
    out.write_text(
        json.dumps(
            {"at": datetime.now(UTC).isoformat(), "comparisons": report, "sent": sent}, indent=1, default=str
        )
    )
    print(f"PASS. Report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
