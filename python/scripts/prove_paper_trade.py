"""Step-1 proof: a paper trade against live Polymarket US books, checked by hand and against Layer's math.

    python scripts/prove_paper_trade.py

1. Finds an open market whose YES asks have at least two levels, and buys through both in paper mode.
2. Checks each fill's fee by hand: coefficient × contracts × price × (1 − price), to the cent, ties to even.
3. Prices the same book with uselayer.calc.size, the port of Layer's POST /v0/size that is tested
   against fee-golden.json, and checks the fills and fees are identical.
"""

from decimal import ROUND_HALF_EVEN, Decimal

from uselayer import Client
from uselayer.calc import size

client = Client(store=":memory:")
for m in client.markets(limit=100):
    book = client.book(m.slug)
    asks = book.outcome("yes").asks
    if len(asks) >= 2 and asks[1].price - asks[0].price <= 0.05 and 0.05 < asks[0].price < 0.95:
        break
else:
    raise SystemExit("no market with two close ask levels right now")

info = client.market(m.slug)
want = asks[0].size + 1
print(f"market {m.slug}  coefficient {info.fee_coefficient}  tick {info.tick_size}  book as of {book.as_of}")
print(f"YES asks: {[(lv.price, lv.size) for lv in asks[:3]]}")
order = client.buy(venue="polymarket_us", market=m.slug, side="yes", price=asks[1].price, size=want)
print(f"paper order: {order.status}, {order.filled} @ {order.avg_price}, fees ${order.fees}")

coef = Decimal(str(info.fee_coefficient))
for f in client.fills():
    exact = coef * Decimal(str(f.contracts)) * Decimal(str(f.price)) * (1 - Decimal(str(f.price)))
    cents = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    print(f"  fill {f.contracts} @ {f.price}: SDK fee ${f.fee}; by hand {exact:.6f} → ${cents}")
    assert Decimal(str(f.fee)) == cents

ref = size(
    {
        "kalshi": {
            "asks": [{"price": 0.01, "size": 1_000_000}],
            "fee_multiplier": 0,
        },  # a free, unlimited other leg
        "polymarket_us": {
            "asks": [{"price": lv.price, "size": lv.size} for lv in asks if lv.price <= asks[1].price],
            "fee_coefficient": info.fee_coefficient,
        },
        "max_contracts": int(want),
    }
)["polymarket_us"]
sdk = [{"price": f.price, "contracts": f.contracts, "cost": f.cost, "fee": f.fee} for f in client.fills()]
print(f"/v0/size port: fills {ref['fills']}, fee ${ref['fee']}")
assert sdk == ref["fills"] and round(sum(f["fee"] for f in sdk), 6) == ref["fee"]
print("✓ paper fills and fees match the hand check and Layer's /v0/size math")
