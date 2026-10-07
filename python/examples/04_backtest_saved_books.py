"""Save a few books, then replay them in backtest mode through the same fill model and rules.

python examples/04_backtest_saved_books.py
"""

import tempfile
from pathlib import Path

from _pick import pick_market

from uselayer import Book, Client
from uselayer.backtest import load_books, record_books

live = Client(store=":memory:")
slug, _ = pick_market(live)
path = Path(tempfile.mkdtemp()) / "books.jsonl"
for _ in range(3):
    record_books(live, [slug], path)  # in real use, call this on a schedule to build a history


def strategy(client: Client, book: Book) -> None:
    ask = book.outcome("yes").best_ask
    if ask and not client.positions():
        client.buy(venue=book.venue, market=book.market, side="yes", price=ask.price, size=2)


bt = Client(mode="backtest", books=load_books(path))
result = bt.replay(strategy)
print(f"replayed {result['books']} books, {result['fills']} simulated fills")
for p in bt.positions():
    print(f"position: {p.contracts} @ {p.avg_price}, fees ${p.fees}")
