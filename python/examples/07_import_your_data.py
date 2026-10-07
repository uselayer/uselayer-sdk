"""Backtest on data you already have: import a CSV of Kalshi top-of-book quotes, check it, replay it.

uv run python examples/07_import_your_data.py
"""

from __future__ import annotations

from pathlib import Path

from uselayer import Book, Client, import_events

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "imports" / "top_of_book.csv"

data = import_events(
    SAMPLE,
    venue="kalshi",
    # your column names → the SDK's fields
    columns={
        "time": "ts",
        "market": "ticker",
        "bid": "yes_bid",
        "bid_size": "yes_bid_qty",
        "ask": "yes_ask",
        "ask_size": "yes_ask_qty",
    },
    price_scale=0.01,  # the file has cents
)
print(data.report.summary())


def on_book(client: Client, book: Book) -> None:
    # Rest a bid 2¢ under the first ask; later books decide whether it fills.
    if not client.orders():
        ask = book.outcome("yes").best_ask
        if ask is not None:
            client.buy(
                venue=book.venue,
                market=book.market,
                side="yes",
                price=round(ask.price - 0.03, 2),
                size=10,
                tif="gtc",
            )


bt = Client(mode="backtest", books=data, rules={"order_ttl_s": 600})
result = bt.replay(on_book)
print(f"replayed {result['books']} books")
for fill in bt.fills():
    print(f"simulated fill: {fill.contracts:g} YES at {fill.price} ({fill.role}) at {fill.at:%H:%M:%S}")
