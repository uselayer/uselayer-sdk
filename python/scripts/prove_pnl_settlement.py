"""Prove client.pnl() and settlement against the venues' real data. Sends no orders.

    python scripts/prove_pnl_settlement.py                 # Polymarket US only, no key needed
    KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... python scripts/prove_pnl_settlement.py   # + Kalshi
    ... --live                                             # + live pnl() against your accounts (reads only)

1. Polymarket US's settlement reader on real settled markets.
2. A paper run on three real open books: buy at the ask, value at the bid, then settle one YES, one NO
   and one void, each number checked by hand.
3. Venue-driven settlement: a fill planted in the store on a market the venue has already settled,
   paid out by ``client.settle()`` from the venue's own answer (Polymarket US, and Kalshi with a key).
4. ``--live``: ``pnl()`` next to the venue's own position rows, with your keys, reads only.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uselayer import Client, Kalshi, PolymarketUS, Resolution, SimulatedFill, VenueError
from uselayer.venues.polymarket_us import GATEWAY

failures: list[str] = []


def check(what: str, got: Any, want: Any) -> None:
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {what}: {got}" + ("" if ok else f" (want {want})"))
    if not ok:
        failures.append(what)


def money(x: float) -> float:
    return round(x, 6)


def settled_pm_us(c: Client, want: int) -> dict[float, str]:
    """Up to ``want`` settled Polymarket US markets, one per payout seen (1, 0, other)."""
    found: dict[float, str] = {}
    for offset in range(0, 2000, 200):
        body = c._http.request(
            "GET",
            f"{GATEWAY}/v1/markets",
            venue="polymarket_us",
            params={"limit": 200, "offset": offset, "closed": "true"},
        )
        for m in body.get("markets") or []:
            slug = m.get("slug")
            if not isinstance(slug, str):
                continue
            try:
                paid = c._venues["polymarket_us"].payout(slug)  # type: ignore[attr-defined]
            except VenueError:
                continue
            yes = None if paid is None else paid.yes
            if yes is not None and yes not in found:
                found[yes] = slug
                print(f"  {slug}: one YES paid {yes}")
            if len(found) >= want or {0.0, 1.0} <= set(found):
                return found
    return found


def plant(c: Client, venue: str, market: str, side: str, price: float, n: float) -> SimulatedFill:
    f = SimulatedFill(
        mode="paper",
        venue=venue,
        market=market,
        order_id="planted",
        side=side,  # type: ignore[arg-type]
        action="buy",
        price=price,
        contracts=n,
        role="taker",
        cost=money(price * n),
        fee=0.0,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        book_as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    c.store.add_fill(f)
    return f


def part_paper_run(c: Client) -> None:
    print("\n2. Paper run on three real Polymarket US books, settled yes / no / void")
    picked = []
    for info in c.markets(limit=60):
        try:
            b = c.book(info.slug)
        except VenueError:
            continue
        yes = b.outcome("yes")
        if (
            yes.best_ask
            and yes.best_bid
            and yes.best_ask.size >= info.min_size
            and 0.05 < yes.best_ask.price < 0.95
        ):
            picked.append((info, b))
        if len(picked) == 3:
            break
    if len(picked) < 3:
        failures.append("three open markets with a bid and an ask")
        return
    for info, b in picked:
        ask = b.outcome("yes").best_ask
        assert ask is not None
        c.buy(venue="polymarket_us", market=info.slug, side="yes", price=ask.price, size=info.min_size)
    for info, _ in picked:
        fills = [f for f in c.fills() if f.market == info.slug]
        n, cost, fee = sum(f.contracts for f in fills), sum(f.cost for f in fills), sum(f.fee for f in fills)
        (row,) = c.pnl().market(info.slug)
        print(
            f"  {info.slug}: bought {n} for {cost:.4f}, fee {fee:.2f}; bid {row.mark} as of {row.mark_as_of}"
        )
        check(f"{info.slug} cost", row.cost, money(cost))
        if row.mark is not None:
            check(f"{info.slug} unrealized = n × bid − cost", row.unrealized, money(n * row.mark - cost))
    now = c._now()
    outcomes = ["yes", "no", "void"]
    c.settle(
        [
            Resolution(venue="polymarket_us", market=info.slug, outcome=o, as_of=now)  # type: ignore[arg-type]
            for (info, _), o in zip(picked, outcomes, strict=True)
        ]
    )
    p = c.pnl()
    want_net = 0.0
    for (info, _), o in zip(picked, outcomes, strict=True):
        fills = [f for f in c.fills() if f.market == info.slug]
        n, cost, fee = sum(f.contracts for f in fills), sum(f.cost for f in fills), sum(f.fee for f in fills)
        paid = {"yes": n * 1.0, "no": 0.0, "void": cost}[o]
        (row,) = p.market(info.slug)
        check(
            f"{info.slug} settled {o}: realized = {paid:.4f} − {cost:.4f}", row.realized, money(paid - cost)
        )
        check(f"{info.slug} fees", row.fees, money(fee))
        want_net += paid - cost - fee
    check("total net = Σ(payout − cost − fee)", p.net, money(want_net))
    check("no open positions left", c.positions(), [])


def part_venue_settles(c: Client, venue: str, market: str, yes: float) -> None:
    plant(c, venue, market, "yes", 0.40, 10)
    plant(c, venue, market, "no", 0.55, 4)
    paid = c.settle()
    got = {(s.side, s.payout, s.proceeds) for s in paid if s.market == market}
    check(
        f"{venue} {market}: settle() reads the venue's result",
        got,
        {("yes", yes, money(10 * yes)), ("no", money(1 - yes), money(4 * (1 - yes)))},
    )
    rows = {r.side: r for r in c.pnl().market(market)}
    check(f"{venue} {market}: YES realized = 10 × {yes} − 4.00", rows["yes"].realized, money(10 * yes - 4.0))
    check(
        f"{venue} {market}: NO realized = 4 × {1 - yes:g} − 2.20",
        rows["no"].realized,
        money(4 * (1 - yes) - 2.2),
    )


def part_live(store: Path) -> None:
    print("\n4. Live pnl() next to the venues' own position rows (reads only)")
    kw: dict[str, Any] = {}
    if _has("POLYMARKET_US_KEY_ID"):
        kw["polymarket_us"] = PolymarketUS.from_env()
    if _has("KALSHI_KEY_ID"):
        kw["kalshi"] = Kalshi.from_env()
    if not kw:
        print("  skipped: no venue key in the environment")
        return
    live = Client(mode="live", store=store / "live.db", on_alert=lambda e: None, **kw)
    try:
        p = live.pnl()
        venue_rows = {
            (v.venue, v.market): v for a in live._live.values() for v in a.positions(include_closed=True)
        }
        print(
            f"  {len(p.rows)} rows; realized {p.realized}, unrealized {p.unrealized}, fees {p.fees}, net {p.net}"
        )
        for r in p.rows:
            v = venue_rows[(r.venue, r.market)]
            print(
                f"  {r.venue} {r.market} {r.side}: contracts {r.contracts} (venue {v.contracts}, settled {v.settled}), "
                f"cost {r.cost} (venue {v.cost}), realized {r.realized} (venue {v.realized_pnl}), "
                f"fees {r.fees} (venue {v.fees}), bid {r.mark}, unrealized {r.unrealized}"
            )
            check(f"{r.market} realized is the venue's", r.realized, v.realized_pnl or 0.0)
            check(f"{r.market} cost is the venue's", r.cost, v.cost)
        for venue in live._live:
            print(f"  {venue} balance: {live.balances()[venue].to_dict()}")
    finally:
        live.close()


def _has(name: str) -> bool:
    import os

    return bool(os.environ.get(name))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="uselayer-pnl-proof-"))
    kalshi = Kalshi.from_env() if _has("KALSHI_KEY_ID") else None
    c = Client(store=tmp / "paper.db", kalshi=kalshi, on_alert=lambda e: None)
    try:
        print("1. Polymarket US settlement reader on real settled markets")
        found = settled_pm_us(c, 3)
        check("found a market that paid YES and one that paid NO", {0.0, 1.0} <= set(found), True)

        part_paper_run(c)

        print("\n3. Settlement read from the venue, on positions planted in real settled markets")
        for yes in (1.0, 0.0):
            if yes in found:
                part_venue_settles(c, "polymarket_us", found[yes], yes)
        if kalshi is not None:
            body = c._venues["kalshi"]._call("GET", "/markets", params={"status": "settled", "limit": 100})  # type: ignore[attr-defined]
            seen: set[float] = set()
            for m in body.get("markets") or []:
                paid = c._venues["kalshi"].payout(m["ticker"])  # type: ignore[attr-defined]
                yes = None if paid is None else paid.yes
                if yes is None or yes in seen:
                    continue
                print(f"  Kalshi {m['ticker']}: settled at {paid.at} (settlement_ts)")
                seen.add(yes)
                print(
                    f"  Kalshi {m['ticker']}: status {m.get('status')}, result {m.get('result')!r}, YES paid {yes}"
                )
                part_venue_settles(c, "kalshi", m["ticker"], yes)
                if {0.0, 1.0} <= seen:
                    break
        else:
            print("  Kalshi skipped: no KALSHI_KEY_ID")

        if "--live" in sys.argv:
            part_live(tmp)
    finally:
        c.close()
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
