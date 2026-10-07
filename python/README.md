# uselayer

A Python SDK for trading prediction markets with your own venue keys.

This release trades **Polymarket US** and **Kalshi**. **Paper mode** (the default) fills orders
against the venue's real order books with simulated money and sends nothing to the venue. **Live
mode** sends orders with your own Polymarket US or Kalshi API key, or both. **Backtest mode** replays
books you saved or data you import.

- One order shape for every venue and mode, published as a JSON Schema (`schema/order.json`).
- Paper mode is the default. `preview()` shows what an order would do and sends nothing.
- Guardrails check every order before it's sent: position size, budget, daily loss, allowed markets,
  approvals, stop-loss and take-profit. A price collar, an order throttle and a kill switch are
  always on.
- Fees come from each venue's published schedule in force at the time of the trade. They match
  Layer's API (`POST /v0/profit`, `POST /v0/size`) to the millionth of a dollar.
- Everything stays on your machine: a local SQLite file per mode, no telemetry.

## Install

```bash
pip install uselayer
```

Python 3.11 or newer.

## Paper trade in five lines

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

`client.positions()`, `client.fills()` and `client.orders()` read the local store. Every fill in paper
mode is a `SimulatedFill` with `simulated=True`.

## Live mode

```python
from uselayer import Client, PolymarketUS

client = Client(mode="live", polymarket_us=PolymarketUS(key_id="...", secret_key_path="~/.pmus/secret"))
client.balances()["polymarket_us"].cash
order = client.buy(venue="polymarket_us", market="<slug>", side="yes", price=0.42, size=5)
client.positions()                                     # from the venue
```

Kalshi works the same way with your Kalshi key (see [Kalshi](#kalshi)); pass both keys to trade both.

Create the Polymarket US key at polymarket.us/developer. It stays on your machine: requests are signed with it
locally and only the signature is sent. Books in live mode come from the venue's WebSocket, so they
aren't cached. An order whose answer never arrives raises `outcome_unknown` and is never sent again
on its own: call `client.sync()` and check `client.orders()`.

If the local store is new but your account already has open orders or positions, live mode starts
with the kill switch on, until you run `python -m uselayer resume --mode live`.

## Kalshi

Kalshi's books are read with your own Kalshi API key, on your machine. Paper mode fills Kalshi orders
against them with Kalshi's fee schedule and each series' fee multiplier; live mode sends them to
Kalshi.

```python
from uselayer import Client, Kalshi

client = Client(kalshi=Kalshi(key_id="...", private_key_path="~/.kalshi/key.pem"))  # or KALSHI_KEY_ID + KALSHI_PRIVATE_KEY_PATH
client.book("<TICKER>", venue="kalshi").outcome("yes").best_ask
client.buy(venue="kalshi", market="<TICKER>", side="yes", price=0.42, size=5)   # paper fill
```

Create the key in your Kalshi account settings; Ed25519 and RSA keys both work. Market ids are
Kalshi tickers. `Kalshi(..., environment="demo")` uses Kalshi's demo exchange. Backtest mode replays
Kalshi books you saved without a key.

**Live Kalshi orders** go to Kalshi with your own key, for your own account. Every guardrail applies
to them as to any other order, and nothing about them goes to Layer:

```python
client = Client(mode="live", kalshi=Kalshi.from_env())   # a Polymarket US key isn't needed
client.balances()["kalshi"].cash
o = client.buy(venue="kalshi", market="<TICKER>", side="yes", price=0.42, size=5)
client.cancel(o), client.positions(), client.fills()
client.kill()                                          # cancels every resting Kalshi order too
```

Try it first on Kalshi's demo exchange (mock money) with a demo key and
`Kalshi(..., environment="demo")` (or `KALSHI_ENV=demo`); `scripts/prove_kalshi_live.py` runs the whole
lifecycle there. Kalshi's positions can trail a fill by a moment; `positions()` waits for them.
Live pairs across Kalshi and Polymarket US (`trade()`) need both keys: see [Pairs](#pairs-both-sides-with-the-leg-risk-guard).


## Profit and loss

```python
p = client.pnl()
p.net, p.realized, p.unrealized, p.fees   # dollars, in total
p.resolution_mismatch_loss                # of realized: lost to pairs the venues settled differently
for r in p.rows:                          # one row per side of each market you've held
    r.market, r.side, r.contracts, r.realized, r.unrealized, r.fees, r.outcome
```

`realized` is the profit from contracts sold or settled, before fees. `unrealized` values the
contracts you hold at the best bid, what you could sell them for now, minus what they cost.
`fees` counts every fee paid. `net` is `realized + unrealized - fees`. A position with no bid to
value it at shows `unrealized=None`, is listed in `p.missing_marks` and is left out of the total.
`max_daily_loss` values positions at the same bid.

`resolution_mismatch_loss` is how much of `realized` hedged pairs lost because the two venues
settled them differently, against the $1 per contract they should have paid (see
[When the venues settle a pair differently](#when-the-venues-settle-a-pair-differently)). It's a
breakdown, "of which, lost to mismatched settlement": it's already inside `realized` and `net`, and
isn't subtracted again. It's negative when a mismatch paid more than $1 (both legs won).

In paper and backtest mode, positions settle when their market does. Each contract pays $1 if its
side won and $0 if it lost. On a `void`, it pays the venue's price when the venue gives one
(Kalshi's fair price for a canceled game), or else what the contract cost. Fees aren't refunded.
The position closes, and resting orders on the market are canceled. Paper mode asks the venue
whether a market you hold has settled, at most once a minute per market, whenever you call
`positions()`, `pnl()` or `monitor()`. `client.settle()` asks now. A backtest settles at each
`resolution` event it replays, and the market takes no more orders. Payouts are in
`client.settlements()`.

In live mode, `pnl()` reports what each venue says about your positions, including closed and
settled ones, and values the contracts you hold at the bid. `fees` is what each venue charged:
Kalshi reports it on each position. For Polymarket US it comes from your account's trade history,
and so do positions Polymarket US has settled, which drop off its positions list.

## Reconcile with the venues

```python
r = client.reconcile()          # live mode
r.ok                            # the store and every venue agree
for m in r.mismatches:
    m.kind, m.venue, m.market, m.message
```

In live mode the SDK keeps its own record of your orders and fills in the local store, and the
guardrails count your positions from it. `reconcile()` reads each venue's fills, positions and open
orders with your key and lists every difference:

- `missed_fill`: the venue filled an order the SDK sent, and the store doesn't have that fill (say,
  the process stopped mid-order).
- `unknown_fill`: the store has a fill for an SDK order that the venue doesn't show.
- `outside_fill`: a fill of an order the SDK didn't send, such as a trade on the venue's website or
  from another bot.
- `position`: the contracts you hold in a market, per the store, differ from the venue's. Positions
  are compared as net YES contracts, so holding NO shows as a negative number.
- `outside_order`: an open order on the venue that the SDK didn't send.
- `stale_order`: the store thinks an order is open, and the venue doesn't list it as open.

It only reads. Nothing in the store changes, and any mismatch also goes to `on_alert`.
`client.reconcile(repair=True)` runs `sync()`, then adds the fills the venue reported for orders the
SDK sent; fills of orders it didn't send stay reported, never added. `since=` sets where the fill
comparison starts (default: just before the store's first order). Markets the venue has settled
aren't compared. Positions are compared for the whole account, so positions from orders sent with
another store show as `position` mismatches. Polymarket US can take a moment to list a new trade:
a `reconcile()` run right after a fill can report it as `unknown_fill` (seen live: a trade 0.2 s
old wasn't listed yet), and running it again a little later clears it. From a terminal, `python -m uselayer reconcile` prints the same list and exits 1
when anything differs, so it can run on a schedule.

## Guardrails

```python
client = Client(
    rules={
        "max_position": {"per_market": 200},  # $ at risk in one market
        "budget": 1000,  # $ at risk in total
        "max_daily_loss": {"amount": 150},  # stop opening positions after this loss today
        "approve_above": 100,  # ask before orders above $100
        "stop_loss": {"pct": 25},  # exits sent by client.monitor()
    }
)
```

Rules can also come from a YAML or JSON file: `Client(rules="guardrails.yaml")` (YAML needs
`pip install "uselayer[yaml]"`). They're fixed when the client is created.

**Kill switch.** `client.kill()` cancels resting orders and blocks new ones. From another terminal:
`python -m uselayer kill`. It stays on, even after a restart, until a person runs
`python -m uselayer resume`. The client a strategy or agent holds can't resume.

## One order, on the cheaper venue

When you know the bet and only need to decide where to place it, `buy_best()` places the order on
whichever venue of a matched pair is cheaper for that size, after fees, at the moment you call it:

```python
pair = client.matches(venue="polymarket_us", q="nfl")[0]     # the same bet on Kalshi and Polymarket US
p = client.preview_best(pair, "yes", 10, max_price=0.55)       # the comparison; sends nothing
r = client.buy_best(pair, "yes", 10, max_price=0.55)
r.order                                                        # sent on the cheaper venue
r.why.reason        # "Kalshi is $0.06 cheaper all-in for 10 contracts: $4.35 vs $4.41 on Polymarket US."
for v in r.why.venues:
    v.venue, v.all_in, v.limit_price, v.skip, v.detail
```

For each venue it walks the book for your size, level by level, and adds that venue's fees, the way
a paper fill is priced. The lower all-in cost wins; on a tie, the venue with more contracts on offer
at its price. `side` is the same outcome on both venues: each market id in a match already names it
(Polymarket US's `<slug>:long` or `<slug>:short`). `client.sell_best(pair, "yes", 10, min_price=0.40)`
sells contracts you hold where they pay more after fees, and only on a venue where you hold them.

A buy can be given in dollars instead of contracts, the way people bet:

```python
r = client.buy_best(pair, "yes", spend=50)    # $50 on YES, fees included
r.order.size                                  # e.g. 69: the most whole contracts $50 buys there
r.why.reason        # "Kalshi wins $3.00 more if you're right: $50.00 buys 69 contracts there for $49.32 all-in, vs 66 for $49.80 on Polymarket US."
```

Each venue gets its own size: the most whole contracts whose cost plus fees fits in `spend`, within
`max_price` and the collar, walked on the same one read of its book. The venue where they pay more if
you're right ($1 a contract) wins; the same number on both is compared on cost, as above. Give `size`
or `spend`, not both; `spend` is for buys, and `preview_best(pair, "yes", spend=50)` takes it too.

The order is a limit at the deepest price the walk reached, immediate-or-cancel (`tif="fok"` for all
or nothing), sent through `send()`, so every guardrail, the price collar and the kill switch apply.
The comparison is a snapshot: a book can move before the order arrives, and the limit, never past
`max_price` or the price collar, caps what it can pay.

A venue is left out, with the reason in `r.why.venues`, when it's switched off, you gave no key for
it, the `venues` or `markets` rules don't allow it, its market is closed, its book is stale, or it
can't fill the size within your limit. When neither venue can take the order, `buy_best()` raises
`VenueError("not_available")` and sends nothing. It works in paper, backtest and live mode; live, you
choose to call it, on your own account and keys.

## Pairs: both sides, with the leg-risk guard

When two markets are the same bet, buying YES on one and NO on the other pays $1 per contract
either way. `quote()` prices that after both fees; `trade()` places both legs:

```python
q = client.quote(pair)                        # pair: a Match from client.matches(), or two (venue, market)
t = client.trade(pair, size=100, min_edge=0.01)
t.status                                      # "hedged" | "missed" | "unwound" | "exposed"
```

A quote reads as three numbers for the contracts that clear `min_edge`:

```python
q = client.quote(pair, size=100, min_edge=0.01)
q.gross_spread    # $1 a contract at settlement minus what they cost, before fees
q.fees            # both venues' taker fees on them
q.net_profit      # gross_spread - fees: what they lock in
q.gross_at_best, q.edge_at_best   # the gap per contract at the best asks, before and after fees
```

When fees close the gap, `q.contracts` is 0 and so are the three totals, but `q.gross_at_best` still
shows the gap before fees.

The money is tied up until both markets pay out, so a quote also gives the return per day:

```python
q.return_pct           # net profit over what the contracts cost with fees, in percent
q.settles_at           # when the money is expected back: the later of the two markets' payouts
q.days_held            # days from now until then (at least 1)
q.return_per_day_pct   # return_pct spread over days_held
```

`settles_at` follows the rule `client.profit()` uses, from each venue's own market times, so the two
agree on the same pair. When neither venue gives a time, all three are `None`: the SDK never guesses
a date. A backtest has no venue times, so pass one: `quote(pair, settles_at="2026-10-05")` (also
`trade()` and `run()`).

The thinner leg goes first, immediate-or-cancel. The other leg goes for what filled, up to its
break-even price. If it can't be completed within `chase_s`, the first leg is sold back, never below
its entry price minus `max_unwind_loss` (`on_miss="unwind"`, the default), or the open contracts are
reported (`on_miss="hold"`). Both legs pass the guardrails together before either is sent.

`trade()` runs in paper, backtest and live mode, including Kalshi ↔ Polymarket US pairs:

```python
client = Client(kalshi=Kalshi.from_env(), layer_key="lyr_...")
pair = client.matches(venue="polymarket_us", q="nfl")[0]   # m.kalshi and m.polymarket_us
client.quote(pair).net_profit_per_contract
```

Live, each leg goes to its venue with your own key. A Kalshi ↔ Polymarket US pair needs both keys,
and both are checked before anything is sent:

```python
client = Client(mode="live", kalshi=Kalshi.from_env(), polymarket_us=PolymarketUS.from_env())
t = client.trade(pair, size=10, min_edge=0.01)
if t.status == "exposed":
    print(t.exposure, t.notes)    # contracts left on one side
```

If a venue answers with an error mid-pair, the open leg is still sold back or reported. If a venue
can't say whether an order went through, nothing is sold back: the trade comes back `exposed`, and
its notes say to run `client.sync()` before trading those markets again.

### When the venues settle a pair differently

A hedged pair pays $1 per contract only if both venues settle the bet the same way. They don't
always: their rules can differ, one can void a market the other settles, or one can settle days
after the other. This is a real risk of trading pairs, and the SDK tracks it:

```python
for m in client.resolution_mismatches():
    m.kind      # "both_lost" | "both_won" | "void_one_leg" | "settle_gap"
    m.impact    # what the pair paid minus the $1 per contract expected (negative: lost)
    m.a.venue, m.a.outcome, m.a.payout    # each venue's result
    m.b.venue, m.b.outcome, m.b.payout
```

The two legs of a `trade()` share its `group_id`. A pair is flagged when both legs lost, both won,
one venue voided or refunded its market and the other didn't, or one leg was still open 48 hours
after the other settled (`settle_gap`, `pending` until it settles). Each one is saved in the mode's
store with both venues' results. In live mode the SDK asks each venue what the pair's markets paid
(reads only, with your keys). Nothing is sent to Layer.

## One strategy, every mode

```python
def strategy(client, pair, quote):
    if quote.net_profit_per_contract >= 0.02:
        client.trade(pair, size=100)

Client(mode="backtest", books=saved_books).run(strategy, [pair])   # the past
Client().run(strategy, [pair], iterations=60)                      # now, paper
```

## Backtest on books you saved

```python
from uselayer import Client
from uselayer.backtest import load_books, record_books

record_books(Client(), ["<slug>"], "books.jsonl")  # run on a schedule to build a history

bt = Client(mode="backtest", books=load_books("books.jsonl"))
bt.replay(lambda client, book: ...)  # place orders as each book arrives
```

The replay uses the same fill model, fees and rules as paper mode, on the replayed clock. Trades in
the data fill resting orders once the estimated line ahead of them at their price is used up.

### Record every tick

`record_books()` saves one snapshot per call. `record_stream()` keeps the venues' live streams open
and saves every book change and trade, with the venue's time (`as_of`) and your machine's
(`received_at`). One list can mix Polymarket US slugs and Kalshi tickers; each venue connects with
your own key for it (for Kalshi, a read-only key is enough), though nothing is traded. It reconnects
on its own and writes a `gap` event for each market while it was disconnected. Kalshi numbers its
messages, so a lost one is a gap too, followed by a fresh book. In backtest mode a market has no
book during a gap.

```python
from uselayer.backtest import load_books, record_stream

record_stream(["<slug>", "<KALSHI-TICKER>"], "ticks.jsonl", duration_s=3600)   # or until Ctrl-C
Client(mode="backtest", books=load_books("ticks.jsonl")).replay(on_book)
```

In paper mode, pass `on_event=client.feed` so the stream's trades fill your resting orders as they print:
`record_stream(["<slug>"], "ticks.jsonl", on_event=client.feed)`.

From a terminal, with progress: `python -m uselayer record <slug> <KALSHI-TICKER> --out ticks.jsonl --minutes 60`.

## Backtest on data you already have

`import_events()` turns history you already keep into the SDK's events, checks it, and hands it to a
backtest:

```python
from uselayer import Client, import_events

data = import_events(
    "ticks.csv",
    venue="kalshi",
    columns={"time": "ts", "market": "ticker", "bid": "yes_bid", "bid_size": "yes_bid_qty",
             "ask": "yes_ask", "ask_size": "yes_ask_qty"},
    price_scale=0.01,  # the file has cents
)
print(data.report.summary())
Client(mode="backtest", books=data).replay(on_book)
```

It reads:

| `format=` | What |
|---|---|
| `csv`, `parquet` | One row per event; `columns=` maps the SDK's field names to yours (Parquet needs `pip install 'uselayer[parquet]'`) |
| `jsonl` | The SDK's own format, as `save_events()` writes it |
| `polymarket_us`, `polymarket`, `kalshi` | Raw messages from each venue's market-data WebSocket, one per line, optionally as `{"received_at": ..., "message": ...}` |
| `pmxt` | PMXT's hourly Polymarket order-book Parquet files (both schemas); pass `markets=` |

The format is guessed from the file when you leave it out. Pass a list of files to join consecutive
hours.

Every import is checked before it can reach a replay. Crossed books, impossible values, prices off
the tick size, unreadable rows and lost Kalshi messages raise `VenueError("bad_data")` with a report;
gaps, rows out of time order and re-sent old books are warnings in `data.report`. Pass `strict=False`
to import a file with errors anyway. The event format and every field are documented at
[uselayer.sh/docs/backtest-data](https://uselayer.sh/docs/backtest-data).

`examples/08_import_and_backtest_a_pair.py` does it end to end for a Kalshi ↔ Polymarket US pair:
it imports a file from each venue, checks them together, and backtests `trade()` and `pnl()` on them.

## Fees by date

```python
from datetime import UTC, datetime
from uselayer import FeeSettings, calculate_fee, rules_at

rules_at("polymarket_us", datetime.now(UTC)).source  # the schedule's page
calculate_fee(
    FeeSettings(venue="polymarket_us"), contracts=100, price=0.5, role="taker", at=datetime.now(UTC)
)
```

Before the earliest schedule the SDK knows, it raises `no_venue_rules` instead of guessing. Kalshi's
schedules start on 2025-10-01 and Polymarket US's at its launch on 2025-12-03. Until 2026-04-03 at 3pm
ET, Polymarket US charged takers a share of the premium (rate × contracts × price: 1 bp, then 10 bp
from 2026-01-09, then 30 bp from 2026-03-09); from then on it's Θ × contracts × p × (1 − p).
`uselayer.venue_rules.history(venue)` lists every entry with its source.

## What paper mode can't tell you

- **Your exact place in line.** A resting paper order joins the back of the line at its price:
  everything already there is ahead of it. Trades at its price use up that line first, and only what's
  left fills your order. A trade through its price, or a book whose other side reaches it, fills it
  too, and the same contracts never fill it twice. Venues publish the total at each price, not single
  orders, so the line is an estimate: when a level shrinks by more than its trades explain, the
  difference counts as cancels, spread through the line (`Client(queue_cancels="behind")` puts them
  all behind you, the worst case). With books alone (`monitor()` in paper mode, or `record_books()`
  snapshots), a resting order fills only when a book crosses its price.
- **How fast the venue answers.** By default a paper order reaches the book the moment you send it.
  `Client(order_latency_s=0.7)` makes it arrive that many seconds later: it fills against the book
  it meets then, and trades before then can't fill it. On Polymarket US (2026-10-04, 8 orders) an
  order landed in the book 0.6–1.4 s after `buy()` was called, median about 0.7 s.
- **Freshness without a key.** In paper mode, Polymarket US's public book is cached for up to 30
  seconds. The SDK stamps each book with the venue's time and, when a copy is older than
  `max_quote_age_s` (10 s by default), waits for a fresh one before using it.
- **When a market settles.** Paper mode learns that a market settled when it next asks the venue
  (at most once a minute per market). Kalshi is asked only once a result is final, not while it can
  still be disputed.
- **Kalshi's sub-cent billing.** The SDK rounds each Kalshi fee up to the cent, from the published
  schedule, as Layer's API does. Kalshi bills fees to fractions of a cent, so a paper fee can be up to
  a cent higher than what Kalshi would charge.

## For AI agents

`AGENTS.md` and `llms.txt` ship inside the package. Every public method has a docstring with an
example, every object has `.to_dict()`, and every error is a `VenueError` with `code`, `hint` and
`next`. The `examples/` folder runs in CI.

## Layer

Layer (uselayer.sh) finds markets that are the same bet on different venues. With a Layer API key,
`client.matches(q="...")` returns them. The SDK sends Layer your key, the market ids Layer gave you
and your filters, and nothing else: no prices, orders, positions or venue keys.

Layer answers with each market's ids and url, and its own match confidence and rule-difference flags
(`m.confidence`, `m.caveats`, and `m.caveat_notes`: why each caveat applies, one sentence per code). The SDK then reads each market's event, question, outcome and times
from its venue's public API, on your machine, so `m.kalshi.outcome` and `m.polymarket_us.question`
are filled in. `client.matches(..., titles=False)` skips those venue calls.

## License

MIT
