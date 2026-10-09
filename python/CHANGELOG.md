# Changelog

## Unreleased

- `client.whales.score()` / `discover()`: merging a set back no longer makes a trader "arbitrage" on its
  own. It's also how a trader with a view closes a bet, so directional traders who merged a few times
  (4 merges was enough) were left out as "no view". Arbitrage is now only buying both outcomes of a
  market within a minute for $1 or less, and its reason and "Takes a side" check say so (the reason
  could read "Market maker" before). One request fewer per wallet scored.

## 0.5.0

- `client.whales`: whale tracking and copy trading across Kalshi and Polymarket, read without a key.
  `top(by="pnl"|"volume")` merges both venues' leaderboards; `trader(venue, id)` gives stats, open
  positions and recent trades (`visibility="hidden"` when a Kalshi trader hides them);
  `big_trades(min_usd)` lists recent large trades with the trader's name where the venue shows it;
  `links(trader)` lists accounts on the other venue that may be the same person, each with a score, a
  `likely`/`possible` tier and the evidence (a name on an account that really trades, an X handle,
  bets on the same market the same way within 5 minutes, found through Layer's matching; bots and
  market makers that trade everything are filtered out); `follow(trader, size=)` returns a `Copier`
  whose `poll()`/`run()` copy new trades through the client (every guardrail applies; paper by
  default), placing a Polymarket trader's bets on the twin market on Kalshi or Polymarket US.
  Kalshi's data comes from the undocumented public endpoints behind its Leaderboard and profile pages,
  which can change; Polymarket's from its public data API.
- `client.whales.score(wallet)`: whether a Polymarket wallet is worth following, from its last 30 days of
  public trades. Each bet is checked against Polymarket's price history (`/v2/prices-history`, 5-minute
  steps) 1, 5, 15 and 60 minutes after the buy. The `Score` has a `segment` (`proven`, `quiet`,
  `rising`, `too_fast`, `lucky`, `no_view`, `no_edge`), a one-sentence `reason`, a `confidence`, and
  `checks` with the evidence for six rules: beats the price (the 5-minute move), enough independent
  bets (one per event), steady (holds without the two best bets and the biggest win), copyable (buying
  1–6 minutes later and paying the taker fee still gains by the hour mark), takes a side (market makers
  and arbitrage are `no_view`; `"bot"` is only a tag) and category strengths. Combo (parlay) bets have
  no price history and are counted apart. `end=` scores an earlier window.
- `client.whales.discover(wallets=200)`: finds wallets on the leaderboards (overall and per category)
  and, for small accounts, among wallets trading in busy markets that the top 1,000 doesn't list, and
  scores each. Takes several minutes; `on_progress` reports each wallet.
- `follow(..., categories=("Sports",))` copies a Polymarket trader only in those categories;
  `follow(..., copy_sells=False)` copies buys only and holds them until the market settles.
  `client.whales.copy_results(order_ids)` says how each copied buy is doing: `open` (valued at the bid),
  `won`, `lost` or `void`, with profit after fees.

## 0.4.3

- `buy_best()` and `preview_best()` take `spend=` (dollars) instead of `size` (contracts):
  `client.buy_best(match, "yes", spend=50)`. Each venue gets the most whole contracts whose cost plus
  fees fits in `spend`, within `max_price` and the collar, priced on the same one read of its book (a
  binary search over the same per-size walk; no extra requests). The venue where they pay more if
  you're right wins, with the new `reason_code` `wins_more` and `saving` = how much more it pays; the
  same number on both is compared on cost as before. A venue where `spend` doesn't buy the market's
  smallest order is skipped as `spend_too_small`. `BestVenue.spend` is the amount given, and
  `BestVenue.size` the chosen venue's contracts (`None` when no venue can take it). Give `size` or
  `spend`, not both; `spend` is for buys only.

## 0.4.2

- `Match.caveat_notes`: why each of a match's `caveats` applies, one plain sentence per code, as
  Layer sends it (`{"source_differs": "Kalshi settles on the league's official box score; Polymarket
  US uses ESPN."}`). `{}` when Layer has no sentence for that match. `client.match()` returns it in
  its dict as well.

## 0.4.1

- Polymarket US's fee history: `quote()`, backtests, paper fills and `calc.profit()` / `calc.size()`
  now price any time from 2025-11-03, not only from 2026-09-25. 0.4.0 raised `no_venue_rules`
  ("Layer has no polymarket_us fee schedule before 2026-09-25") on every older replay. Each dated
  entry in `venue_rules` cites its fee page or CFTC filing:
  - 2025-11-03 (earliest copy of the fee page; public launch 2025-12-03): takers pay 1 bp of the
    premium, rounded to $0.0001, at least $0.0001; makers pay nothing.
  - 2026-01-09: 10 bp, rounded to $0.001, at least $0.001; makers pay nothing.
  - 2026-03-03: makers get 10 bp of the premium back.
  - 2026-03-09: 30 bp taker, 20 bp maker rebate, both to the cent. The go-live hour isn't known, so
    it's dated from 00:00 ET on the filing's day (the higher fee).
  - 2026-04-03 3pm ET: Θ 0.05 × C × p × (1 − p), maker rebate Θ 0.0125.
  - 2026-07-01: Θ 0.06.
  - 2026-09-17: Θ 0.0695. 0.4.0 dated it 2026-09-25 from the docs banner; the filing says it took
    effect at 12:00 a.m. ET on September 17.
- The premium era is a new fee kind, `PolymarketUSPremiumFees`: rate × contracts × price, to that
  era's rounding step (half to even), in whole millionths of a dollar. A market's own `coefficient`
  is a Θ, so passing one still prices Θ × C × p × (1 − p) in any era. `calc.profit()` and
  `calc.size()` report `fee_premium_rate` instead of `fee_coefficient` for a premium-era leg.
- Kalshi's schedule effective 2025-10-01 (taker 0.07, maker 0.0175 on the markets Kalshi lists) is
  now an entry. Before, Kalshi had none before 2026-07-07, which stopped any older replay too.
- `calc.profit()` and `calc.size()` look up only the venues the request names, so a Kalshi ↔
  Polymarket US pair before 2026-03-30 no longer fails on Polymarket's (not US) schedule.
- Not counted: Polymarket US's volume and promotional taker rebates (paid weekly, by volume), and the
  tiered maker rebate filed on 2026-10-02 (not in force yet).

## 0.4.0

- `Quote` shows the return per day the money is tied up: `settles_at` (when it's expected back, the
  later of the two markets' payouts), `days_held` (days until then, at least 1) and
  `return_per_day_pct`, next to `return_pct`. The payout time follows the rule `client.profit()`
  uses, from each venue's own market times, with the same rounding, so `quote()` and `profit()` agree
  on the same pair. When neither venue gives a time, the three are `None`: no date is guessed.
  `quote()`, `trade()` and `run()` take `settles_at=` to set it, which a backtest needs (it has no
  venue times). Carried through `to_dict()` and `Trade.quote`.
- Resolution mismatches: a hedged pair pays $1 per contract only if both venues settle the bet the
  same way, and that is a real risk the SDK now tracks. `client.resolution_mismatches()` lists each
  pair (the two legs of one `trade()`, linked by its `group_id`) the venues settled differently:
  `both_lost`, `both_won`, `void_one_leg` (one venue voided or refunded its market, the other
  didn't) or `settle_gap` (one leg still open 48 hours after the other settled; `pending` until it
  settles). Each `ResolutionMismatch` has both venues' results and `impact`, what the pair paid
  minus the $1 per contract expected; it's saved in the mode's store. `pnl()` gains
  `resolution_mismatch_loss`: how much of `realized` was lost to mismatched settlement, broken out
  so the cause is visible. It's already inside `realized` (what the venues paid minus what it
  cost) and `net` (`realized + unrealized - fees`, unchanged), never subtracted twice. Paper, backtest and
  live mode; live asks each venue what the pair's markets paid (reads only). Nothing is sent to
  Layer.

- `Quote` shows the spread before fees: `gross_spread` (what the quoted contracts pay at settlement
  minus what they cost), `gross_spread_per_contract`, and `gross_at_best` (`1 - ask a - ask b` at the
  top of both books, the before-fees twin of `edge_at_best`). `gross_spread - fees == net_profit`.
- `client.buy_best(pair, side, size, max_price=None)` places one order on whichever venue of a
  matched pair is cheaper for that size, after fees, at the moment you call it. Each venue's book is
  walked for the size (never past `max_price` or the price collar) and priced with that venue's fees,
  the way a paper fill is; on a tie, the venue with more contracts on offer at its price wins. The
  order is an immediate-or-cancel limit at the walk's deepest price, sent through `send()`, so every
  guardrail, the price collar and the kill switch apply. `client.sell_best(pair, side, size,
  min_price=None)` sells contracts you hold where they pay more after fees, only on a venue where
  you hold them. `client.preview_best(...)` does the same comparison and sends nothing. Each returns
  the order and `why`: every venue's all-in cost, or why it was left out (switched off, no key, not
  allowed by the `venues` / `markets` rules, a closed market, a stale book, not enough size within
  the limit), and the reason the winner won. Paper, backtest and live mode. Proved on real Kalshi
  and Polymarket US books in paper mode (`scripts/prove_best_paper.py`) and with real orders on
  Kalshi's demo exchange (`scripts/prove_best_kalshi_demo.py`).
- Fix: `trade()`, `quote()`, `run()` and `buy_best()` / `sell_best()` / `preview_best()` read both
  books fresh together. When Polymarket US's public book came back from its cache older than
  `max_quote_age_s`, the SDK waited for a fresh copy (up to ~30 s), and by then the other leg's book,
  read first, was too old: `trade()` refused with `stale_quote` though both venues had fresh books,
  and `buy_best()` could compare against that leg's old price. Now a leg that goes old while the
  other is read is read again (up to two more rounds); `stale_quote` / `stale_book` are left for a
  book that can't be read fresh, and name that venue.

## 0.3.0

- Fix: in live mode, every filled contract now reaches the store with its fill. Before, three orders
  were saved with their filled size but without the fills behind it: a Polymarket US resting order
  that filled as it was placed, an order found again after the venue's answer was lost, and an order
  that filled before its cancel landed. `sync()` then saw nothing new, so the guardrails counted
  fewer contracts than you held, and only `reconcile(repair=True)` would add them.
- Live Polymarket US: a fill that `sync()` finds on a resting order now carries the venue's trade id
  and trade time, from the account's trade history, not `<order id>:<filled>` and the time the SDK
  saw it. A fill made just before midnight counts toward that day's `max_daily_loss`. Any part the
  history doesn't list yet keeps the old form. Stores from earlier versions need no change: `sync()`
  never adds a second copy of a fill stored the old way, and `reconcile()` matches both forms.
- `Client(order_latency_s=...)` (paper and backtest; default 0): an order reaches the book that
  many seconds after it's sent. It fills against the book it meets then and joins the line as that
  book stands; books and trades before then can't fill it. Paper mode waits that long and reads the
  book again; a backtest looks ahead in its own data. On Polymarket US (2026-10-04, 8 real orders)
  an order landed in the book 0.6–1.4 s after `buy()` was called, median about 0.7 s.
- Live Polymarket US: `pnl()` counts the fees charged, from the account's trade history, and
  includes settled positions, which Polymarket US drops from its positions list. A position's
  `cost` is before fees. `balances()` cash is what you can spend now (`buyingPower`), not counting
  the margin Polymarket US holds against short positions.
- `client.reconcile()` compares the live store with what Kalshi and Polymarket US report and lists
  every difference: fills the store missed, store fills the venue doesn't show, fills of orders the
  SDK didn't send, positions that differ, and open orders either side doesn't know about. It only
  reads; `repair=True` runs `sync()` and adds the missed fills of orders the SDK sent. Also
  `python -m uselayer reconcile`, which exits 1 when anything differs. The venue adapters' `fills()`
  now read every fill on the account (Kalshi's fills, Polymarket US's trade activity). Proved on
  Kalshi's demo exchange (`scripts/prove_reconcile_kalshi.py`) and read only on a real Polymarket US
  account (`scripts/prove_reconcile_polymarket_us.py`).
- Live pairs: `client.trade()` runs in live mode across Kalshi and Polymarket US, with your own key
  for each venue (`Client(mode="live", kalshi=Kalshi(...), polymarket_us=PolymarketUS(...))`). Both
  keys are checked before anything is sent. The leg-risk guard is the same as in paper mode. If a
  venue answers with an error mid-pair, the open leg is still sold back or reported. If a venue can't
  say whether the second leg's order went through, nothing is sold back: the trade comes back
  `exposed`, with a note to run `client.sync()` first. Kalshi's leg was tested on Kalshi's demo
  exchange (`scripts/prove_live_pair.py`). Polymarket US has no demo exchange, so its leg was tested
  against a mock of its API, not with real orders.
- A live order whose venue reports fees only per fill now carries its fills' total in `order.fees`.
- Resting (`gtc`) orders in paper and backtest mode wait in an estimated line at their price.
  Everything already at the price is ahead of the order; trades at that price use the line up
  first, and only the rest fills the order. A trade through the price, or a book whose other side
  reaches it, fills it too. Cancels (a level shrinking by more than its trades explain) are spread
  through the line; `Client(queue_cancels="behind")` puts them all behind your order, the worst
  case. Backtest replays now fill resting orders from `trade` events; in paper mode, `client.feed()`
  takes them, e.g. from `record_stream(..., on_event=client.feed)`.
- Fix: a resting paper or backtest order no longer fills again from contracts it already filled
  against. Before, each poll or book that still showed the same offer filled it again (a buy of 100
  at $0.42 against 10 offered reached 60 filled after five polls). Paper and backtest results with
  resting orders can show fewer fills than before.
- `client.pnl()`: realized, unrealized and fees, per side of each market and in total, in every
  mode. Open positions are valued at the best bid, as `max_daily_loss` values them. In live mode,
  the numbers are what Kalshi and Polymarket US report, including closed and settled positions.
- Paper and backtest positions settle. A `resolution` pays $1 or $0 a contract, or on a `void` the
  venue's price (new `Resolution.payout`) or else the cost. The position closes as a
  `SimulatedSettlement` (`client.settlements()`), and the market's resting orders are canceled.
  Paper mode asks the venue at most once a minute per market (`client.settle()` asks now). In a
  backtest, a resolved market takes no more orders. `replay()` also counts `resolutions` and
  `settlements`.
- `record_stream()` and `python -m uselayer record` record Kalshi too, with your own Kalshi key
  (`kalshi=Kalshi(...)`, or `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`; a read-only key is
  enough). One call can mix Kalshi tickers and Polymarket US slugs: each venue gets its own
  connection, and a drop marks gaps on that venue's markets only. Kalshi books are written whole on
  every change, like Polymarket US's, and match what `import_events(format="kalshi")` reads from the
  same messages. A skipped Kalshi sequence number is written as a gap, followed by fresh books.
- `match()` and `matches()` fill in each market's `event`, `question`, `outcome`, `event_time` and
  `close_time` from its venue's public market data, on your machine (one call per event, cached).
  Layer's API now answers with ids, urls, `series`/`slug`, match confidence and rule-difference
  flags only. `titles=False` skips the venue calls. Polymarket international markets keep ids only.
- Sandbox mode is removed, with `sandbox_account()` and `sandbox_reset()`. `Client(mode="sandbox")`
  raises `not_available`; paper mode runs the same orders on your machine.
- `examples/recorded.json` is made-up data (`scripts/make_recorded.py`), not recorded venue answers.
- Kalshi in paper and backtest mode. `Client(kalshi=Kalshi(key_id=..., private_key_path=...))` (or
  `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`) reads Kalshi markets and books with your own key,
  signed on your machine with Ed25519 or RSA-PSS. Paper and backtest fills use Kalshi's dated fee
  schedule and each series' fee multiplier and fee type; all 305 Kalshi cases in `fee-golden.json`
  pass. Kalshi ↔ Polymarket US pairs run through `quote()`, `trade()` and `run()`.
- Live Kalshi orders: `Client(mode="live", kalshi=Kalshi(...))` sends Kalshi orders with your own
  key, for your own account (a Polymarket US key is no longer required for live mode). Every
  guardrail applies: price collar, order pacing, `max_position`, `budget`, `max_daily_loss`,
  `markets`, `approve_above`, `stop_loss`/`take_profit` and the kill switch, which cancels resting
  Kalshi orders. `cancel()`, `cancel_all()`, `orders()`, `fills()`, `positions()` and `balances()`
  read Kalshi; `positions()` waits briefly for Kalshi's positions to catch up with a fill. Proved on
  Kalshi's demo exchange (`scripts/prove_kalshi_live.py`).
- `uselayer.paper_venues()` lists the venues paper and backtest mode fill; `trading_venues()` still
  lists the venues that trade live.
- `record_stream()` (and `python -m uselayer record`): every book change and trade from Polymarket
  US's live stream, appended to a JSON-lines file in the SDK's event format. Reconnects on its own
  and writes a `gap` event (`StreamGap`) per market for the time it was disconnected. Recordings
  load into backtest mode with `load_books()`.
- Market events carry `received_at` (this machine's clock) beside `as_of`; `TradePrint` carries the
  venue's `trade_id` and `aggressor` (whether the taker bought or sold YES).
- Backtest replay applies `book_change` events to the market's last book, clears a market's book
  during a `gap`, and reports `trades` and `gaps`.
- `import_events()`: backtest on data you already have. Reads CSV and Parquet (with a column
  mapping), the SDK's own JSON lines, raw market-data WebSocket messages from Polymarket US,
  polymarket.com and Kalshi, and PMXT's hourly Polymarket Parquet files, into the SDK's events.
- `check_events()`: every import is checked for crossed or impossible books, prices off the tick
  size, unreadable rows, lost Kalshi messages (errors, which stop the import unless
  `strict=False`), and gaps, rows out of time order and re-sent old books (warnings).
- Polymarket books rebuilt from level changes drop levels the venue's own best bid/ask says are gone.
- Backtest replays Kalshi and polymarket.com data as well as Polymarket US (a new `BACKTEST`
  switch); paper and live are unchanged.
- `pip install 'uselayer[parquet]'` adds pyarrow for Parquet files.
- `client.prices(pair)`: each market's best YES and NO bid and ask, with the size at each, the
  venue's time for the book (`as_of`) and when it was read (`read_at`). Takes what `quote()` takes (a
  `Match` or two `(venue, market)` pairs) and reads through `book()`: the venues with your own keys
  in paper and live mode (reads only), the replayed books in backtest. `Prices.to_dict()` and
  `Prices.from_dict()` round-trip it as plain data.
- `client.fees(pair)`: a Kalshi ↔ Polymarket US pair's fee settings (the Kalshi series'
  `fee_multiplier` and `fee_type`, the Polymarket US market's `feeCoefficient`) and `days_held` until
  the expected payout, read from the venues with your own keys: what Layer's `POST /v0/profit` fills
  in for `kalshi.market_id`, done on your machine. `MarketInfo` carries the venue's `event_time`.
- `client.profit(request, pair=None)`: the `POST /v0/profit` body, `kalshi.market_id` included (its
  Polymarket US twin comes from Layer's match), or `pair=`; fills in what `fees()` finds, keeps what
  the request sends, and adds `match` and `filled_in`. On 12 live matches it gave Layer's answer.
  `calc.profit()` and `calc.size()` are unchanged and still read nothing.
- Requests send `User-Agent: uselayer-python/<version>` (was `uselayer-python`), so Layer can see
  which SDK versions are in use.

## 0.2.0 (2026-10-01)

- Sandbox mode: `Client(mode="sandbox", layer_key=...)` sends practice orders to Layer's hosted
  sandbox (simulated money, Polymarket US, fill-now orders), after your guardrails pass them on
  your machine. `positions()`, `balances()`, `sandbox_account()` and `sandbox_reset()` read and
  reset that account. Retries are safe: the same `client_id` returns the first order.

## 0.1.0 (2026-10-01)

- `Client` in paper mode (the default): orders fill against Polymarket US's live public order books
  through Layer's fill model; nothing is sent to the venue.
- Live mode for Polymarket US with your own API key (`PolymarketUS`): signed orders, cancel and
  cancel-all, open orders, fills, positions and balance; fresh books from the venue's WebSocket; an
  unknown order outcome is looked up and never resent; a new store starts killed when the account
  already has open orders or positions.
- Backtest mode: replay books you saved with `uselayer.backtest.record_books`.
- Pairs: `quote()` prices YES on one market plus NO on its twin after both fees; `trade()` places both
  legs with the leg-risk guard (thinner leg first, second leg up to break-even, chase, then unwind or
  report the exposure); `run(strategy, pairs)` runs the same strategy in backtest, paper or live.
- One order shape for every venue and mode, published as `schema/order.json`.
- `preview()`: the fill, fees and every rule's decision, without sending anything.
- Guardrails: max position, budget, max daily loss, allowed venues/markets/actions, expiry,
  approval above a size, stop-loss and take-profit; an always-on price collar, order throttle and
  kill switch (`python -m uselayer kill | resume | status`).
- Dated venue rules: fee schedules looked up by the time of the trade.
- Fee math, `profit()` and `size()` that match Layer's API to the millionth of a dollar.
- One error type, `VenueError`, with `code`, `hint`, `next` and `retryable`; retries and pacing per
  venue host.
- Local SQLite store per mode; no telemetry.
