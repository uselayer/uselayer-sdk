"""Trade both sides of a pair with the leg-risk guard, in backtest mode.

The books below are made up for the example; they aren't real prices. Two markets that are the same
bet: buying YES on one and NO on the other pays $1 per contract either way.

    python examples/06_pair_trade_backtest.py
"""

from datetime import UTC, datetime, timedelta

from uselayer import Book, Client, Level

t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def book(market: str, seconds: int, bid: float, ask: float, size: float = 50) -> Book:
    return Book(
        venue="polymarket_us",
        market=market,
        bids=(Level(price=bid, size=size),),
        asks=(Level(price=ask, size=size),),
        as_of=t0 + timedelta(seconds=seconds),
    )


pair = [("polymarket_us", "example-a"), ("polymarket_us", "example-b")]
# A backtest has no venue times, so say when the money comes back: these made-up markets pay out on
# 2026-10-05. Without it, return_per_day_pct is None (the SDK never guesses a date).
SETTLES_AT = "2026-10-05"
books = [
    book("example-a", 0, 0.40, 0.42),  # YES on A costs 0.42
    book("example-b", 1, 0.60, 0.62),  # NO on B costs 1 − 0.60 = 0.40
    book("example-a", 60, 0.47, 0.49),
    book("example-b", 61, 0.52, 0.54),
]


def strategy(client: Client, pair: list[tuple[str, str]], quote) -> None:  # type: ignore[no-untyped-def]
    print(
        f"quote: {quote.contracts} contracts clear the edge: gross ${quote.gross_spread:.2f} "
        f"- fees ${quote.fees:.2f} = net ${quote.net_profit:.2f} "
        f"({quote.net_profit_per_contract} a contract after fees)"
    )
    print(
        f"  return {quote.return_pct}% over {quote.days_held} days until {quote.settles_at:%Y-%m-%d} "
        f"= {quote.return_per_day_pct}% a day"
    )
    if quote.net_profit_per_contract >= 0.02 and not client.positions():
        t = client.trade(pair, size=20, min_edge=0.01)
        print(f"trade: {t.status}, {t.hedged} hedged, ${t.locked_in} locked in after fees")


bt = Client(mode="backtest", books=books, rules={"max_position": {"per_market": 50}})
bt.run(strategy, [pair], settles_at=SETTLES_AT)
for p in bt.positions():
    print(f"position: {p.contracts} {p.side} of {p.market} at {p.avg_price}")
