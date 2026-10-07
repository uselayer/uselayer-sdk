"""Import your own Kalshi and Polymarket US data, check it, and backtest a pair trade across the two.

The two sample files hold made-up stream messages in each venue's own format (a pretend Kalshi
ticker and a pretend Polymarket US slug for the same bet); they aren't real prices. Swap in files
you recorded with ``record_stream()`` or exported from your own systems.

    uv run python examples/08_import_and_backtest_a_pair.py
"""

from __future__ import annotations

from pathlib import Path

from uselayer import Client, check_events, import_events

SAMPLES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "imports"
KALSHI = "KXSAMPLE-26OCT05-PAIR"
POLY_US = "aec-sample-pair-2026-10-05"

# 1. Import each venue's raw stream messages into the SDK's event format (checked on the way in).
kalshi = import_events(SAMPLES / "pair_kalshi.jsonl", format="kalshi")
poly_us = import_events(SAMPLES / "pair_polymarket_us.jsonl", format="polymarket_us")

# 2. Check both together before trusting a replay: gaps, order, crossed books, prices off the tick.
events = sorted([*kalshi, *poly_us], key=lambda e: e.as_of)
report = check_events(events, tick_size=0.01)
print(report.summary())
if not report.ok:
    raise SystemExit("Fix the data first.")

# 3. Replay both venues in one backtest and trade the pair with the leg-risk guard.
pair = [("kalshi", KALSHI), ("polymarket_us", POLY_US)]
# Imported data carries no payout time, so say when the money comes back (made up, like the
# markets); return_per_day_pct is None without it.
SETTLES_AT = "2026-10-06T18:00:00Z"


def strategy(client: Client, pair: list[tuple[str, str]], quote) -> None:  # type: ignore[no-untyped-def]
    if quote.a is None or quote.b is None:
        print("quote: no price on one side yet")
        return
    print(
        f"{max(quote.a.book_as_of, quote.b.book_as_of):%H:%M:%S} quote: "
        f"{quote.a.side.upper()} on {quote.a.venue} at {quote.a.best_price} + "
        f"{quote.b.side.upper()} on {quote.b.venue} at {quote.b.best_price}: "
        f"gap {quote.gross_at_best:+.4f}, after fees {quote.edge_at_best:+.4f} a contract"
    )
    print(
        f"  {quote.contracts} contracts: gross ${quote.gross_spread:.2f} - fees ${quote.fees:.2f} "
        f"= net ${quote.net_profit:.2f}"
    )
    if quote.contracts:
        print(f"  return {quote.return_pct}% over {quote.days_held} days = {quote.return_per_day_pct}% a day")
    if quote.net_profit_per_contract >= 0.01 and not client.positions():
        t = client.trade(pair, size=20, min_edge=0.01)
        print(f"trade: {t.status}, {t.hedged} hedged, ${t.locked_in} locked in after fees")


bt = Client(mode="backtest", books=events, rules={"max_position": {"per_market": 50}})
bt.run(strategy, [pair], settles_at=SETTLES_AT)

# 4. What it's worth now: both legs valued at the last replayed bids.
for p in bt.positions():
    print(f"position: {p.contracts:g} {p.side} of {p.market} ({p.venue}) at {p.avg_price}")
pnl = bt.pnl()
print(
    f"pnl: realized ${pnl.realized:.2f}, unrealized ${pnl.unrealized:.2f}, fees ${pnl.fees:.2f}, net ${pnl.net:.2f}"
)
# Selling both legs at today's bids would cost the spread; held to settlement, the pair pays $1 a
# contract whichever way the bet goes, which is what "locked in" above counts.
