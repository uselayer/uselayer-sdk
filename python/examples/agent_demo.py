"""An agent on the SDK, start to finish: match a market across Kalshi and Polymarket US, compare the
prices, let the fees decide, paper-trade, then build the same order in live mode.

Layer finds the match. Everything else runs here, with your own venue keys: books, fees, paper
fills and the live preview. The live step only previews: this script never sends a real order.

    export LAYER_API_KEY=lyr_...
    export KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=~/.kalshi/key.pem     # reads Kalshi books
    python examples/agent_demo.py --q nfl
    python examples/agent_demo.py --q nfl --live-preview                   # + POLYMARKET_US_KEY_ID/SECRET_KEY

Real gaps after fees are rare. Most runs end with "fees say no trade", which is the point: the
agent checks before it spends anything.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from uselayer import Client, Kalshi, Match, VenueError
from uselayer.trading import Quote

NAMES = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US"}


def say(step: int, text: str) -> None:
    print(f"\n[{step}] {text}")


def cents(p: float | None) -> str:
    return "—" if p is None else f"{p * 100:.1f}¢"


def describe(m: Match) -> str:
    k, u = m.kalshi, m.polymarket_us
    return (
        f"{k.event or k.group_id}\n"
        f"    Kalshi         {k.market_id}  {k.outcome or ''}\n"
        f"    Polymarket US  {u.market_id}  {u.outcome or ''}\n"
        f"    Layer: {m.confidence:.0%} sure · rules differ: {', '.join(m.caveats) or 'no'}"
    )


def best_pair(client: Client, matches: list[Match]) -> tuple[Match, Quote] | None:
    """The match whose cheapest pair (YES on one venue, NO on the other) is best after both fees."""
    best: tuple[Match, Quote] | None = None
    for m in matches:
        try:
            q = client.quote(m)
        except VenueError as e:
            print(f"    skip {m.kalshi.market_id}: {e.message}")
            continue
        if q.edge_at_best is None:
            continue
        if best is None or q.edge_at_best > (best[1].edge_at_best or -1):
            best = (m, q)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q", default=None, help="search Layer's matches, e.g. nfl or fed")
    ap.add_argument("--limit", type=int, default=10, help="how many matches to price (default 10)")
    ap.add_argument("--size", type=int, default=10, help="contracts per leg (default 10)")
    ap.add_argument("--min-edge", type=float, default=0.01, help="$ per contract after fees (default 0.01)")
    ap.add_argument("--live-preview", action="store_true", help="also preview the order in live mode")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="uselayer-demo-"))
    client = Client(kalshi=Kalshi.from_env(), store=home / "paper.db", rules={"budget": 100})

    say(1, "Layer: which markets are the same bet on Kalshi and Polymarket US?")
    matches = client.matches(venue="polymarket_us", q=args.q, limit=args.limit)
    if not matches:
        raise SystemExit("    No live matches for that search. Try another --q.")
    for m in matches[:3]:
        print("  " + describe(m))
    print(f"    … {len(matches)} matches. Titles came from the venues, read on this machine.")

    say(2, "Compare prices: both books, both ways round, after both venues' fees.")
    found = best_pair(client, matches)
    if found is None:
        raise SystemExit("    No match has asks on both venues right now.")
    m, q = found
    assert q.a and q.b
    print("  " + describe(m))
    for leg in (q.a, q.b):
        print(f"    buy {leg.side.upper():3} on {NAMES[leg.venue]:13} best ask {cents(leg.best_price)}")
    gap, edge = q.gross_at_best or 0.0, q.edge_at_best or 0.0
    print("    per contract at the best asks (both legs pay $1.00 together):")
    print(f"      gross spread {cents(gap)}  −  fees {cents(gap - edge)}  =  net {cents(edge)}")

    say(3, f"Check fees: {args.size} contracts, each worth at least {cents(args.min_edge)} after fees?")
    sized = client.quote(m, size=args.size, min_edge=args.min_edge)
    print(f"    gross spread  ${sized.gross_spread:7.2f}   ({sized.contracts} contracts before fees)")
    print(f"    − fees        ${sized.fees:7.2f}")
    print(f"    = net profit  ${sized.net_profit:7.2f}")
    if sized.return_per_day_pct is None:
        print(f"    return {sized.return_pct}% (no payout date from either venue, so no per-day return)")
    else:
        print(
            f"    return {sized.return_pct}% over {sized.days_held} days until "
            f"{sized.settles_at:%Y-%m-%d %H:%M} UTC = {sized.return_per_day_pct}% a day"
        )
    if sized.contracts:
        print(f"    trade: {sized.contracts} contracts lock in ${sized.net_profit:.2f} after both fees")
    else:
        print(f"    skip: fees say no trade: no contract clears {cents(args.min_edge)} after both fees")

    say(4, f"Paper-trade it: {args.size} contracts a leg, real books, fake money.")
    t = client.trade(m, size=args.size, min_edge=args.min_edge)
    print(f"    {t.status}: hedged {t.hedged:g} contracts, locked in ${t.locked_in:.2f}")
    leg = q.a
    if t.status == "missed":
        # Nothing was worth both legs; show what one paper fill looks like anyway.
        o = client.buy(venue=leg.venue, market=leg.market, side=leg.side, price=leg.best_price, size=1)
        fee = sum(f.fee for f in client.fills())
        print(
            f"    one paper contract instead: {o.status}, {NAMES[leg.venue]} {leg.side.upper()} at {cents(leg.best_price)}, fee ${fee:.4f}"
        )

    say(5, "Execute: the same call in live mode.")
    if not args.live_preview:
        print(
            "    Client(mode='live', ...) takes the same order. Run with --live-preview to see it previewed."
        )
        return
    from uselayer import PolymarketUS

    live = Client(
        mode="live", polymarket_us=PolymarketUS.from_env(), kalshi=Kalshi.from_env(), store=home / "live.db"
    )
    leg = q.a if q.a.venue == "polymarket_us" else q.b
    order = live.order(venue=leg.venue, market=leg.market, side=leg.side, price=leg.best_price, size=1)
    p = live.preview(order)
    print(
        f"    live preview, nothing sent: allowed={p.allowed}, fill {p.est_fill.filled:g} at {cents(p.est_fill.avg_price)}, fees ${p.fees:.4f}"
    )
    for problem in p.problems:
        print(f"    note: {problem}")
    print("    live.send(order) would place it. This demo stops here.")
    live.close()
    client.close()


if __name__ == "__main__":
    main()
