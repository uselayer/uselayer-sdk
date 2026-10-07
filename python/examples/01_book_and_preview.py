"""Read a market's book and preview an order. Nothing is sent.

python examples/01_book_and_preview.py
"""

from _pick import pick_market

from uselayer import Client

client = Client(store=":memory:")  # paper mode (the default); this store lives in memory only
slug, book = pick_market(client)
yes = book.outcome("yes")
print(f"market {slug}: best bid {yes.best_bid.price}, best ask {yes.best_ask.price}, as of {book.as_of}")

order = client.order(venue="polymarket_us", market=slug, side="yes", price=yes.best_ask.price, size=5)
preview = client.preview(order)
print(
    f"allowed: {preview.allowed}, would fill {preview.est_fill.filled} @ {preview.est_fill.avg_price}, fees ${preview.fees}"
)
for d in preview.verdict.decisions:
    if d.result != "allow":
        print(f"  {d.rule}: {d.result} — {d.reason}")
