# uselayer

Trade prediction markets on **Kalshi** and **Polymarket US** from Python, with your own venue keys.

One order shape for every venue. Paper mode by default: real order books, simulated money. Guardrails on every order. Fee math that matches each venue's published schedule to the millionth of a dollar. Everything runs on your machine.

[Website](https://uselayer.sh) · [Docs](https://uselayer.sh/docs/sdk) · [PyPI](https://pypi.org/project/uselayer/) · [Example bot](https://github.com/uselayer/layer-spread-bot)

```bash
pip install uselayer      # Python 3.11+
```

```python
from uselayer import Client

client = Client()  # paper mode: real books, simulated fills
m = client.markets(limit=20)[0]  # open Polymarket US markets, no key needed
book = client.book(m.slug)
order = client.order(
    venue="polymarket_us", market=m.slug, side="yes", price=book.outcome("yes").best_ask.price, size=5
)
print(client.preview(order))  # fill, fees, every rule's decision
print(client.send(order))  # the order, filled against the book
```

## What it does

- **Three modes, one API.** Paper (the default) fills against live books with fake money. Live sends orders with your Kalshi or Polymarket US key, or both. Backtest replays books you saved or data you import.
- **The cheaper venue for each order.** With a [Layer](https://uselayer.sh) API key, `client.matches()` returns markets that are the same bet on both venues, and `buy_best()` places your order on whichever is cheaper after fees. Give it contracts, or a dollar amount (`spend=50`) and it buys where that money wins more.
- **Both sides of a gap.** `quote()` prices a cross-venue pair after fees, depth and return per day; `trade()` buys both sides only if the gap is still there.
- **Guardrails.** Position size, budget, daily loss, allowed markets, approvals, stop-loss and take-profit. A price collar, an order throttle and a kill switch are always on.
- **Your keys stay yours.** Venue requests are signed locally. Layer only ever sees your Layer key, the market ids it gave you and your search filters: no prices, orders, positions or venue keys. No telemetry.

## In this repo

- [`python/`](python/): the `uselayer` package. Full guide in [python/README.md](python/README.md), runnable examples in [python/examples/](python/examples/).
- [`schema/order.json`](schema/order.json): the order shape every venue and mode shares, as JSON Schema.
- [`fee-golden.json`](fee-golden.json): Layer's own fee answers, which the SDK's fee math is tested against.

MIT licensed.
