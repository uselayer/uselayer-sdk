"""Buy in paper mode against the live book, then check the fee by hand.

python examples/02_paper_trade.py
"""

from _pick import pick_market

from uselayer import Client

client = Client(store=":memory:")
slug, book = pick_market(client)
ask = book.outcome("yes").best_ask

order = client.buy(venue="polymarket_us", market=slug, side="yes", price=ask.price, size=5)
print(f"{order.status}: {order.filled} @ {order.avg_price}, fees ${order.fees}")

coefficient = client.market(slug).fee_coefficient or 0.0695
for fill in client.fills():
    by_hand = coefficient * fill.contracts * fill.price * (1 - fill.price)
    print(
        f"  fill {fill.contracts} @ {fill.price}: fee ${fill.fee} (by hand ${by_hand:.4f}, billed to the cent)"
    )
    assert abs(fill.fee - by_hand) <= 0.005 + 1e-9

for p in client.positions():
    print(f"position: {p.contracts} {p.side} of {p.market} at {p.avg_price} (simulated: {p.simulated})")
