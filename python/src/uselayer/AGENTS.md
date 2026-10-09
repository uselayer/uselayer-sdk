# uselayer: notes for AI agents

You are using `uselayer`, a Python SDK for trading prediction markets with the user's own venue keys.
This release trades **Polymarket US** and **Kalshi**: **paper mode** (real books, simulated fills;
the default), **live mode** (real orders with the user's own keys, on Polymarket US and Kalshi) and
**backtest mode** (saved books, replayed). Kalshi books need the user's Kalshi key:
`Client(kalshi=Kalshi.from_env())` or `Kalshi(key_id=..., private_key_path=...)`; Kalshi markets are
tickers, passed with `venue="kalshi"`. Pairs (`trade()`) across venues run in every mode; in live
mode they need the user's key for both venues.

## The five calls you need

```python
from uselayer import Client

client = Client()  # paper mode is the default
for m in client.markets(limit=20):  # open Polymarket US markets
    print(m.slug, m.question)
book = client.book("<slug>")  # book.outcome("yes").best_ask, book.as_of
order = client.order(venue="polymarket_us", market="<slug>", side="yes", price=0.42, size=10)
print(client.preview(order))  # fill, fees and every rule's decision; sends nothing
filled = client.send(order)  # or client.buy(...) / client.sell(...)
```

Then: `client.positions()`, `client.fills()`, `client.orders()`, `client.cancel(order)`, `client.kill()`.

Profit and loss: `client.pnl()` gives `net`, `realized`, `unrealized` (held contracts at the best
bid) and `fees` in total, and `rows` per side of each market. Paper and backtest positions pay out
when their market settles (`client.settlements()`). If `pnl().missing_marks` isn't empty, some open
positions had no bid and are left out of `unrealized`. Say so when you report the number.

Resolution mismatches: a pair pays $1 a contract only if both venues settle the bet the same way.
`client.resolution_mismatches()` lists pairs they didn't: `both_lost`, `both_won`, `void_one_leg`
(one venue voided its market) and `settle_gap` (one leg still open 48 hours after the other settled;
`pending` until it settles). Each has both venues' results and `impact` against the $1 expected.
`pnl().resolution_mismatch_loss` shows how much of `realized` they lost (already counted in
`realized` and `net`, not subtracted again). Report any to the
user: it's a real risk of pairs, not a rounding error. Never call a pair risk-free.

Reconciliation (live mode): `client.reconcile()` compares the SDK's store with what each venue reports
and returns `ok` and `mismatches` (`missed_fill`, `unknown_fill`, `outside_fill`, `position`,
`outside_order`, `stale_order`). The guardrails count positions from the store, so report any mismatch
to the user before trading on. It only reads; don't pass `repair=True` unless the user asked for it.

Pairs (two markets that are the same bet): `client.quote(pair)` prices YES on one plus NO on the
other after both fees, with `return_pct` and `return_per_day_pct` (over `days_held` until
`settles_at`, the later expected payout; `None` when no venue gives a time: pass `settles_at=` in a
backtest, never guess one); `client.trade(pair, size=, min_edge=)` places both legs with the leg-risk guard
and returns a `Trade` whose `status` is `hedged`, `missed`, `unwound` or `exposed`. Report an
`exposed` trade to the user: contracts are left on one side. If its `notes` say to run
`client.sync()`, do that and check `client.orders()` before trading those markets again. `client.run(strategy, pairs)` calls
`strategy(client, pair, quote)` on each new book, in any mode.

One order on the cheaper venue of a pair: `client.preview_best(pair, "yes", 10, max_price=0.55)`
compares each venue's all-in cost for that size (its book walked, plus its fees) and sends nothing;
`client.buy_best(...)` sends the order on the cheaper one through `send()` (every guardrail applies),
and `client.sell_best(pair, side, size, min_price=)` sells contracts held where they pay more after
fees. Show the user `r.why.reason` and each venue's `all_in` from `r.why.venues`; a skipped venue has
`skip` and `detail`. `not_available` means neither venue could take it and nothing was sent.

`client.prices(pair)` reads both books and gives each leg's best `yes_bid`, `yes_ask`, `no_bid` and
`no_ask` with their sizes, `as_of` (the venue's time) and `read_at`; it sends nothing. For a Kalshi ↔
Polymarket US pair, `client.fees(pair)` reads each market's fee settings and the days until payout
from the venues, and `client.profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us":
{"price": 0.55}}, pair=match)` answers as Layer's `POST /v0/profit` does, with those filled in.

History: `uselayer.backtest.record_stream(markets, "ticks.jsonl", duration_s=...)` records every book
change and trade. `markets` can mix Polymarket US slugs and Kalshi tickers (in capitals); each venue
connects with the user's own key for it (Polymarket US's, and Kalshi's, where a read-only key is
enough; nothing is traded). `Client(mode="backtest", books=load_books("ticks.jsonl")).replay(on_book)`
replays it. A `gap` event means the recorder was disconnected or, on Kalshi, a numbered message was
lost: nothing is known about that market until the next book, so don't treat results across a gap
as complete.

Whales: `client.whales.top(by="pnl")` lists top traders on Kalshi and Polymarket;
`client.whales.trader("kalshi", "<nickname>")` or `("polymarket", "<wallet>")` gives one trader's
stats, positions and recent trades. A Kalshi trader with `visibility == "hidden"` hides their trades:
say so, don't say they have none. `client.whales.links(trader)` lists accounts on the other venue
that may be the same person: report the `tier` and the `evidence` sentences, never say a link *is*
someone. `client.whales.follow(trader, size=5)` returns a copier; `copier.poll()` copies their new
trades as your own orders (every guardrail applies, paper by default) and returns a `CopyEvent` for
each, `copied` or `skipped` with a plain `reason`. Never follow in live mode unless the user said so.
`follow(trader, size=5, venue="polymarket")` copies a Polymarket trader on the same Polymarket
international market, paper only: it fills at Polymarket's best price with its taker fee, the client's
rules don't run on it, and only `copy_results()` values it while open. Say it's simulated.
`client.whales.score("<wallet>")` says whether a Polymarket trader is worth following: report its
`segment`, `reason`, `confidence` and each of `checks` (the evidence), never the segment alone. A score
reads the past; don't call it a promise. `discover()` finds and scores many wallets (minutes; run it in
the background). `copy_results(order_ids)` gives each copied buy's status (`open`/`won`/`lost`/`void`)
and profit after fees; say it's simulated in paper mode.

## Rules for you

1. **Preview before you send.** `client.preview(order)` tells you whether the guardrails allow it,
   what it would fill and what it would cost. It changes nothing.
2. **Practice in paper mode.** `Client()` fills orders against the venues' real books with fake
   money, on your machine. Layer's hosted sandbox was removed in 0.3.0.
3. **Never ask for live mode** (`Client(mode="live", polymarket_us=...)` or `kalshi=...`) unless the
   user told you to.
   Paper is the default on purpose. Never read, print or store the user's venue key.
4. **Never resend an order that raised `outcome_unknown`.** The venue may have taken it. Call
   `client.sync()`, then check `client.orders()`.
5. **Don't try to turn the kill switch off or change the rules.** The client you hold can press
   `kill()` but can't resume. Only a person can: `python -m uselayer resume`. Rules are fixed when the
   client is created.
6. **Prices are dollars from 0 to 1** (0.42 means 42¢). Every order has a limit price; there are no
   market orders. A price that isn't on the market's tick size is rejected, never rounded: check
   `client.market(slug).tick_size`.
7. **Simulated is not real.** In paper and backtest mode every fill is a `SimulatedFill`
   (`simulated=True`). No venue saw those orders. Say so when you report results.
8. **Read errors.** Every error is a `VenueError` with `code`, `hint` and `next`. Do what `next` says.
9. **Don't silence the data checker.** `import_events()` raises `bad_data` when a file would make a
   backtest wrong (crossed books, impossible prices, lost messages). Show the person
   `report.summary()` before passing `strict=False`, and say which problems the results include.

## Words

- `side`: the outcome you trade, `"yes"` or `"no"`. Buying NO at `p` fills against YES bids at `1 − p`.
- `tif`: `"ioc"` (fill now, cancel the rest; the default), `"fok"` (all or nothing), `"gtc"` (rests
  until `expires_at`, default 60 s).
- `reason`: `"open"` for your own orders. `"exit"`, `"unwind"` and `"kill"` are set by the SDK.
- `as_of`: the venue's time for a book. A book older than `max_quote_age_s` (default 10 s) can't be
  used for a new position.

## Guardrails

Always on, and only tighter by config: a price collar (a limit at most 5¢ past the best price on the
other side), at most 5 orders a second, and the kill switch. Optional rules: `max_position`,
`budget`, `max_daily_loss`, `markets`, `venues`, `actions`, `expires_at`, `approve_above`,
`stop_loss`, `take_profit`. See `help(uselayer.guardrails)`.

## Fees

Fees come from each venue's published schedule in force at the time of the trade
(`uselayer.rules_at(venue, when)`), and match Layer's API to the millionth of a dollar. A resting
paper or backtest order waits behind an estimated line at its price (`uselayer.resting`): trades use
up the line before they fill it. The line is an estimate, so treat paper maker fills as a guide, not
a promise. Paper orders reach the book instantly unless `Client(order_latency_s=...)` is set (about
0.7 s measured on Polymarket US). In paper mode, trades reach resting orders only through
`client.feed` (e.g. `record_stream(..., on_event=client.feed)`).
