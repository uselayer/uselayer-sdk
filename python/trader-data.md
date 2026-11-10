# Trader data: the developer guide

The full guide to `client.whales`. To follow traders without writing code, use [Signals](https://usesignals.app).

`client.whales` reads traders on **Polymarket** and **Kalshi** from the venues' public data, on your machine, with no venue key: who they are, what they hold, their trades, their track record and a score. It also prices what buying one of their trades would cost you now, and copies trades in paper mode. [Signals](https://github.com/uselayer/signals) is built on it.

```python
from uselayer import Client

client = Client()  # layer_key="lyr_..." only for cross-venue matches
who = client.whales.search_traders("<username>")[0]
detail = client.whales.trader("polymarket", who.id)
```

**Upgrade to uselayer 0.18.0 or later** (`pip install -U uselayer`). Polymarket retired its Data API v1 on Oct 24, 2026, and 0.18.0 reads v2; older versions can't read Polymarket traders.

Every call reads public data and sends no order. Each one says in its docstring how many reads it makes. Reads go through the client's per-host limits, and a read Polymarket's firewall turns away is tried once more; a second refusal raises `VenueError`, never a partial answer.

## Find traders

```python
client.whales.top(by="pnl", limit=20)          # both venues' leaderboards, merged by all-time profit
client.whales.top("polymarket", by="volume")   # one venue, by volume
client.whales.search_traders("@name")          # Polymarket usernames or profile links, in one read
client.whales.big_trades(min_usd=1000)         # recent trades of $1,000 or more on each venue, newest first
```

- `search_traders()` finds Polymarket traders the way Polymarket's own site search does: an exact match first, then the rest, at most `limit` (5). Each `Trader` has the wallet as `id`, the username as `name` and the profile `url`, but no figures; read `trader()` for those.
- `big_trades()`: Polymarket's feed runs a few minutes behind. Kalshi names only traders who opted into its social features (about 1 trade in 10); the rest have `trader=None`.
- `find_wallets()` sweeps every Polymarket leaderboard and the last trades of open markets, and yields each wallet with where it was seen (`Sighting`). All of it is about 18,000 reads, so pass `per_second=` to leave room for other reads.

## Read a trader

```python
d = client.whales.trader("polymarket", "0x...")   # stats, open positions, recent trades, closed positions
d.stats["trading_pnl"], d.stats["volume_usd"], d.stats["trades"], d.stats["markets_traded"]

client.whales.trades("polymarket", "0x...", limit=50)     # their recent trades, newest first
client.whales.trades_between("0x...", start, end)         # their trades in a time range, oldest first
client.whales.price_history(t.token, start, end)          # an outcome's price about once a minute
client.whales.positions("polymarket", "0x...")            # what they hold now, in one read
client.whales.positions("polymarket", "0x...", market="<conditionId>")  # one market
a = client.whales.account("0x...")                        # open positions' worth and cash
a.total_usd if a.complete else None
```

- `trades_between(wallet, start, end)` (Polymarket only; datetimes or unix seconds, both ends included) reads by time, so it reaches past the 5,000 trades Polymarket lists by offset. At most `limit` (10,000): if it stops there with more trades left, it logs a warning on the `uselayer` logger, so read on from the last trade's `at` or pass a higher `limit`. Each trade's `slug` and `event_slug` name its market; filter 15-minute Bitcoin windows with `(t.slug or "").startswith("btc-updown-15m-")`.
- `price_history(token, start, end)` gives `(unix seconds, price)` points about a minute apart, each at the second Polymarket took it, for open and closed markets. `token` is a trade's `token` or one of a market's `clobTokenIds`.

A Polymarket trader's `stats`:

| Key | What it is |
|---|---|
| `trading_pnl` | Profit from trading, after fees: realized plus unrealized |
| `realized_pnl`, `unrealized_pnl` | The two parts |
| `income` | Rewards, maker rebates and yield, kept apart from trading |
| `pnl` | Polymarket's own figure, rewards included |
| `volume_usd` | Dollars bought and sold (`volume_contracts` is contracts) |
| `trades` | How many fills they made |
| `markets_traded` | How many markets they've traded: the "Predictions" on their Polymarket profile |
| `pnl_as_of` | When Polymarket last worked these figures out (it does every few hours) |

These meanings are from 0.14.0. Before it, `trades` counted markets and the volume was in contracts.

A Kalshi trader can hide their trades and holdings: `visibility` is then `"hidden"`, and an empty list isn't the same as holding nothing.

## Track record

Three reads of a Polymarket wallet's history, each with no guessing: a figure that can't be read is `None`, never 0.

```python
r = client.whales.completed_positions("0x...")
r.summary          # "Made money on N of M positions", or why their record can't be given
r.reconciles       # True only when the positions add up to Polymarket's realized profit
r.positions        # every position they're done with, each market outcome once

h = client.whales.profit_history("0x...")    # their hourly profit since their first trade, in one read
h.period(start, end)       # trading_pnl, income, volume, return_per_dollar
h.weekly(start, end)       # profit each week; None for a week before their first trade
h.daily(start, end)        # [(date, profit since start), ...]
h.worst_drop(start, end)   # the biggest fall from a high: usd, peak_at, trough_at

c = client.whales.realized_by_category("polymarket", "0x...")
c.categories               # {"SPORTS": 12100000.0, "POLITICS": 33300.0, ...}, all time, after fees
```

- **`completed_positions()`** counts positions they closed and positions whose market settled but which they never redeemed. Polymarket's own closed list leaves the second kind out, and those are mostly losses, so a record from it alone flatters anyone who leaves losers unredeemed. Give a rate only when `reconciles` is True; `summary` says so in one line. Parlays aren't in it, since Polymarket lists them nowhere, and `parlays_excluded` says when there were any. Usually 3 reads, 13 at most.
- **`profit_history()`** is one read, and you can cut it into as many periods as you like with no more reads. Trading profit counts open positions at their price at the time, so it moves with prices.
- **`realized_by_category()`** is all time only, with parlays included: Polymarket's day, week and month figures by category are before fees and don't add up. 12 reads.

## Score a trader

```python
s = client.whales.score("0x...")    # their last 30 days; 5–30 seconds
s.segment, s.reason                 # "proven", and one plain sentence why
s.skill, s.edge_verdict, s.copyable
for c in s.checks:
    print(c.title, c.passed, c.detail)

found = client.whales.discover(wallets=200)   # find and score: several minutes, run it in the background
```

`score()` checks up to 60 of their trades against Polymarket's price history. It gives three separate verdicts:

- **`skill`**: `"yes"`, `"early"` (good signs, too few trades), `"no"` or `"unknown"`.
- **`edge_verdict`**: the price test alone, with no profit in it.
- **`copyable`**: whether buying the same thing a few minutes later, after the taker fee, still gained. It never moves a trader's tier.

The checks behind them:

| Check | Passes when |
|---|---|
| Beats the price | The price moves their way within 5 minutes of their buy |
| Enough independent trades | One trade per market, and trades on the same event count once |
| Steady, not lucky | The edge holds without their two best trades, and their profit without their biggest win |
| Profitable from trading | They made money from trading over the window, after fees; rewards and rebates don't count |
| Copyable | A copier 1 to 6 minutes late, paying the taker fee, still gained by the hour mark, reliably |
| Takes a side | They aren't a market maker or doing arbitrage. Being a bot is only a tag |
| Trading lately | A buy in the last 7 days |
| Where their edge is | The same test per category, so you can follow them only there |

The tier, `segment`, comes from skill and track record:

| `segment` | What it means |
|---|---|
| `proven`, `quiet` | Skilled, with enough trades. `quiet` is a smaller account no leaderboard lists |
| `rising` | Good early signs, not enough trades yet |
| `big_profit` | Big profit, but the price test doesn't show skill |
| `lucky` | Their profit rests on one win |
| `parlays` | Most of their buys are parlays, which have no price history to test. Their profit still counts as a record |
| `too_few` | Too few trades checked to judge either way. Not a verdict |
| `edge_losing` | An edge, but they lost money from trading lately |
| `no_view` | Market makers and arbitrage: no view to follow |
| `inactive` | Would be recommended, but hasn't bought in the last 7 days |
| `no_edge` | The price doesn't clearly move their way |

`s.action` says what to do about it (`follow`, `watch`, `avoid` or `skip`), and `s.worst_drop` is their biggest fall in trading profit for each period it reads. A score reads the past; it isn't a promise. `too_fast` and the copyability part of the tier were removed in 0.10.0, and a score saved before that may still have them.

## Price a buy now

```python
trade = next(t for t in d.trades if t.action == "buy")   # one of their buys
p = client.whales.preview_copy(trade, spend=50)       # $50, fee included, on the same Polymarket market
if p.ok:
    p.contracts, p.price, p.fee, p.total, p.profit_if_win
    p.best_price, p.worst_price       # the cheapest ask, and the dearest level this amount reaches
else:
    p.reason, p.max_spend             # e.g. only $31.20 is for sale that close to their price

k = client.whales.preview_kalshi(trade, "<Kalshi ticker>", "yes", spend=50)   # the same buy on Kalshi
```

- **`preview_copy()`** walks Polymarket's order book for the amount, up to their price plus `max_slippage` (3¢), so `price` is what that many contracts really average, with Polymarket's taker fee level by level. `ok` is False, with a plain `reason`, when nothing is for sale that close to their price, the book holds less than the amount (`max_spend` says what it holds), the amount doesn't buy the smallest order, or the market isn't taking orders. `depth_checked` is False when the book couldn't be read and the price is the best price alone.
- **`preview_kalshi()`** prices a buy of one side of a Kalshi market, such as the market a Polymarket market is matched to, from Kalshi's public book: no Kalshi key. The limit is the cheapest ask now plus `max_slippage`, since their price is on another venue. The fee is Kalshi's for the market's series. Whole contracts only.

Neither sends anything.

## Copy trades in paper mode

```python
event = client.whales.copy_trade(trade, spend=50)   # buy it now, if it's still within 3¢ of their price

copier = client.whales.follow(
    who, size=5,                      # or ratio=0.1 of their size, never more than max_size (100)
    venue="polymarket",               # the same Polymarket market; or "polymarket_us" / "kalshi" via Layer's matching
    categories=("Sports",), min_usd=100, copy_sells=True,
)
for e in copier.run(duration_s=3600):  # or copier.poll() on your own loop
    print(e.status, e.reason)

client.whales.copy_results([e.order_id for e in copier.events if e.order_id])  # open, won or lost, after fees
```

- `copy_trade()` copies one buy they already made, priced as `preview_copy()` prices it. Live mode raises.
- `follow()` copies only trades made after it starts, and skips any first seen more than `max_age_s` (5 minutes) late. With `copy_sells=True` (the default), their sells are copied in proportion: when they sell 40% of their position, the copy sells 40% of yours. With `copy_sells=False`, buys are held until the market settles.
- `venue="polymarket"` is paper only. Each copy fills at once at Polymarket's best price, which doesn't show how much is for sale there. `"polymarket_us"` and `"kalshi"` copy the same trade found through Layer's matching (needs a Layer key), and every guardrail applies.

## The same person on the other venue

```python
for link in client.whales.links(who):
    print(link.trader.name, link.tier, [e.detail for e in link.evidence])
```

A link is an inference from public trading, never an identity: show its `tier` (`likely` or `possible`) and its evidence, such as the same name or X handle, and trades in the same market, the same way, within 5 minutes.

## Good to know

- **Trader reads cover Polymarket International and Kalshi**, not Polymarket US.
- **Kalshi's trader data can change.** It comes from the public endpoints behind kalshi.com's Leaderboard and profile pages, which Kalshi doesn't document.
- **Polymarket's figures lag.** It works out profit in batches every few hours (`pnl_as_of`), so a wallet that just closed a position may not reconcile until the next batch.
- **Copying lands late.** Polymarket's public trade feed runs a few minutes behind, and by then the price has often moved. We haven't found that copying traders makes money after costs.
