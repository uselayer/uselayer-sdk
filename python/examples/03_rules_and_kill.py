"""Set guardrails, see one block an order, then press the kill switch.

python examples/03_rules_and_kill.py
"""

from _pick import pick_market

from uselayer import Client, VenueError

client = Client(store=":memory:", rules={"max_position": {"per_market": 3}, "budget": 10})
slug, book = pick_market(client)
ask = book.outcome("yes").best_ask

big = client.order(venue="polymarket_us", market=slug, side="yes", price=ask.price, size=50)
print("preview of a big order:", client.preview(big).blocked_by)  # max_position

small = client.buy(venue="polymarket_us", market=slug, side="yes", price=ask.price, size=1)
print("small order:", small.status)

client.kill()  # cancels resting orders and blocks new ones (python -m uselayer kill does the same from a terminal)
try:
    client.buy(venue="polymarket_us", market=slug, side="yes", price=ask.price, size=1)
except VenueError as e:
    print(f"after kill: {e.code} by {e.rule}. {e.next}")
