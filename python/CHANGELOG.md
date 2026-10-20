# Changelog

## Unreleased

- **`whales.profit_history(wallet)`: a Polymarket wallet's hourly profit history, in one read.** It returns a
  `ProfitHistory` (exported from `uselayer`), the history a score reads, to cut into any period with no more
  reads:
  - `since`: their first trade's hour (epoch seconds); every total is 0 before it.
  - `period(start, end)`: `trading_pnl` (realized plus the change in unrealized, after fees), `realized`,
    `unrealized`, `income` (rewards, rebates, yield, kept apart), `volume` (dollars traded) and
    `return_per_dollar`.
  - `trading_at(t)`: trading profit, all time, at `t`.
  - `weekly(start, end)`: profit each week, None for a week that ended before their first trade (never a flat or
    losing week). `known_steps(start, end, every)` for steps of any length.
  - `daily(start, end)`: profit since `start` once a day, `[(date, dollars), …]`, or None when any day is unknown.
  - `worst_drop(start, end)`: the biggest fall from a high, from every hourly point (`usd`, `peak_at`,
    `trough_at`).
  - Times are epoch seconds or aware datetimes. Every figure is None when there's no history, never 0.
  - A score's `periods`, `pnl_weeks`, `pnl_daily` and `worst_drop` now come from the same class, cut at the
    score's times: no change to what they say. 1 read (`/v2/user-pnl`), tried once more when Polymarket's
    firewall turns it away; a malformed wallet raises `ValueError` before any read.

## 0.14.0

Released 2026-10-19. A Polymarket trader's figures say what they count, a trader's whole record of positions,
a worst drop that doesn't depend on the hour of the score, and realized profit by category.

- **Expect these Polymarket figures to change meaning (breaking).**
  - `trader("polymarket", …).stats["trades"]` is now the number of fills (trades). What it was before, the number
    of markets traded, is `stats["markets_traded"]`.
  - `stats["volume_usd"]` and a Polymarket trader page's `Trader.volume` are now real dollars bought and sold:
    about half what they read before (0.4–0.8× on 7 wallets checked), since they were contracts.
  - `top()`'s Polymarket traders say `volume_unit="contracts"` (it was `"usd"`); the figure is unchanged.
  - New `stats["pnl_as_of"]`: when Polymarket last worked their figures out, a batch time every few hours.
- **`whales.completed_positions(wallet)`: a trader's whole record of positions**, losers never redeemed
  included, with `summary` ("Made money on 215 of 400 positions · parlays not included", or why there's none).
  Parlays are left out and said to be (`parlays_excluded`). It's given only when the positions add up to
  Polymarket's realized profit without parlays within 0.2%: on a big wallet that's thousands of dollars, a
  trade-off the owner accepted. 3 reads for most wallets, 13 at most.
- **`Score.worst_drop`:** the biggest fall from a high over 7, 30 and 90 days, from every hourly point, so it's
  the same at every hour of the day the score is made. No extra reads.
- **`Whales.realized_by_category(venue, wallet)`:** realized profit, all time, by Polymarket category, parlays
  included. 12 reads. From the leaderboard, a batch that can lag the user stats by hours.

- **A Polymarket trader's volume is dollars, and `trades` is how many trades they made.**
  - Polymarket's user-stats `volume` counts contracts: it equals the sizes of a wallet's fills added up. Its
    dollars are `volume_usdc`.
  - `trader("polymarket", …).stats["volume_usd"]` and `Trader.volume` were `volume`, so they showed contracts
    as dollars (1.8–2.5× too high on the top 3 all-time wallets). They're now `volume_usdc`, and the
    contracts are `volume_contracts`, as on Kalshi.
  - `stats["trades"]` was user-stats' own `trades`, which isn't a trade count (14 against 16,002 fills on
    one wallet). It's now `trade_count`, the number of fills.
  - New `stats["markets_traded"]`: user-stats' own `trades`, which is how many markets they've traded (the
    "Predictions" on their Polymarket profile: 84, 14 and 45 on three wallets checked), as on Kalshi.
  - A Polymarket account `links()` finds by name had contracts as its dollar volume too; it's dollars now.
  - New `stats["pnl_as_of"]`: when Polymarket last worked these figures out. That's every few hours for an
    active wallet, and can be months for one that stopped trading. It looks like a batch time shared across
    wallets (many read 2026-10-18 19:47:41 UTC at once).
  - `top()` says `volume_unit="contracts"` for Polymarket: its leaderboard volume is the same `volume`.
- **`whales.completed_positions(wallet)`: every position a wallet is done with.**
  - Polymarket's closed positions leave out positions whose market settled but which were never redeemed.
    Those are mostly losses: a losing position pays nothing, so there's nothing to redeem. A record from
    closed positions alone flatters anyone who leaves losers unredeemed. For one recommended trader it read 41
    of 41 profitable; the whole record is 41 of 79.
  - This returns both kinds (`CompletedPosition`, with `redeemed`) in a `PositionRecord`.
  - `reconciles` is True when every position was read and they add up to Polymarket's all-time realized
    profit without parlays (`market_pnl`) within 0.2% (10¢ for a small wallet). Only then is `made_money` of
    `positions` the whole record of their positions.
    Checked 2026-10-18 on 33 wallets read in full: 27 within a cent, 2 within 0.1%; the other 4 (2–119% off)
    were trading that day, with positions closing or settling around Polymarket's last batch (`pnl_as_of`), and
    reconcile only after the next one.
  - The trade-off accepted: 0.2% is many dollars on a big wallet. On Kch-Temp it's about $6,071, so any one of
    60 of its 402 positions (52 losers, the largest $5,809) could be missing and it would still reconcile,
    moving the rate about 0.1 point. On a 12-position wallet, 1 of them (a $15 one).
  - `positions` counts positions (each outcome of a market), not markets: say "positions", never
    "predictions" (Polymarket's profile word for markets traded).
  - Each market outcome is counted once. Polymarket's paging can repeat a row on the next page (and so skip
    another): a wallet where it did is `complete=False`. A position listed as closed that they also still hold,
    never redeemed, is counted as the closed one, which has its whole profit.
  - Parlays are left out, and said to be: Polymarket lists them in neither list. `parlay_pnl` is their
    realized parlay profit and `parlays_excluded` is True when it isn't zero; say "parlays not included" with
    the rate then. A trader with a few parlays keeps their record (Kch-Temp: 215 of 400 positions, −$98 of
    parlays). One who trades only parlays reconciles with no positions, and `summary` says there are none to
    count.
  - `summary`: the record in one line ("Made money on 215 of 400 positions · parlays not included"), or why it
    can't be given: not all read, no profit figure from Polymarket, or not adding up to its total right now.
  - Positions held are read before closed ones, so one sold or redeemed between the reads is in both lists
    (counted once), never in neither.
  - Reads: 1 for the stats, 1 per 500 positions held (up to `held_pages`, 2) and 1 per 50 closed (up to
    `max_pages`, 10): 3 for most wallets, 13 at most. A wallet with more is `complete=False` and never
    `reconciles`. A read Polymarket's firewall turns away is tried once more (as in scores); a second refusal
    raises `VenueError`.
- **New `Score.worst_drop`: the biggest fall from a high, from every hourly point (#31).**
  - For the last 7, 30 and 90 days: `usd` (the fall in trading profit from a high to a later low), `peak_at`
    and `trough_at`. From the hourly profit history the score already reads: no extra reads.
  - Trading profit is realized plus unrealized, after fees, parlays included; rewards and rebates aren't in it.
  - `peak_at` is the window's start, off the hour, when the window opens partway down a fall; for a wallet newer
    than the window, that's before their first trade.
  - A worst drop worked out from `pnl_daily` changed with the hour the score was made, since its one reading a
    day is at that clock hour: on a busy in-play trader (Kch-Temp), $215K to $642K over 30 days depending on
    the hour, against $690,521 from every hourly point. `worst_drop` is the same at every hour of a day, and
    moves only when a new high or low comes into or leaves the window (so the 7 days can move within a day).
  - It's measured from hourly points: a fall and recovery within one hour isn't in it, and open positions count
    at their price at the time, which can swing during live games.
  - `pnl_daily`'s docstring now says its readings are at the score's clock hour, not at the end of each day.
- **`Whales.realized_by_category(venue, wallet)`, Polymarket only: realized profit from trading, all time, by
  category.** Returns a `RealizedByCategory` with these fields:
  - `categories`: Polymarket's category → dollars after fees, for the categories they traded only. One they never
    traded is left out, never 0.
  - `overall`: the leaderboard's own all-categories figure.
  - `uncategorised`: `overall` minus the categories, for markets in no category.
  - `realized_pnl` (with parlays), `realized_market_pnl` and `realized_combo_pnl`: the wallet's user stats, read
    now.
  - Every figure is to the cent.
  - `read_at`.
- **Parlays are included.** The leaderboard puts a parlay's profit in a category: a trader who only trades parlays
  has category figures, and `overall` tracks `realized_pnl` (with parlays), not `realized_market_pnl`.
- **12 reads for that:** the leaderboard for this wallet, `timePeriod=ALL` and `orderBy=PNL`, once per category
  (10) and once overall, plus user stats.
  - Each read goes through the per-host limits and is tried once more on a firewall refusal.
  - It's all time only. The leaderboard's day, week and month figures are before fees, count bets still open and
    don't add up across categories.
  - The leaderboard is a batch Polymarket updates every few hours, so `overall` can differ from `realized_pnl` by
    what changed since.
  - A wallet that isn't `0x` and 40 hex digits raises `ValueError` before any read: given one, the board ignores
    `user` and answers with its own top row.
  - Checked live on three wallets: the categories plus `uncategorised` match `overall`, `uncategorised` is $0,
    $17 and $612, and `overall` is within $2.33 and $0.25 of `realized_pnl` on the two with parlays.
- **`PolymarketData.leaderboard(..., user=)`** reads one wallet's own row: `[]` when it has none on that board.

## 0.13.0

Released 2026-10-18. Whale scores are steadier, measure skill without a resting order's spread, and say what they
couldn't read. Thresholds are unchanged.

- **Expect a one-time change of tier.**
  - On the 43 traders Signals recommended on 2026-10-15, about 7 or 8 change tier, mostly traders who buy with
    resting orders, whose measured edge was the spread their bids earned. Most become "Big profit, edge
    unproven". Takers are unchanged.
  - The stable sample also moves a few traders near a threshold once.
- **About 14% more reads per score than 0.12.x**, measured live on 73 wallets (65.2 → 74.6). Most of it is the stable
  sample below; telling resting buys apart adds about 1.3 a score.
- **`Score.unread` is new.** It names `held_share`, `positions_usd` or `cash_usd` when their read failed, and `edge`
  when the taker feed couldn't be read for some checked buys (those are left out of the skill test).
- **Shared threads:** a score no longer starts its own thread pools.

- **The bets a score checks are a stable sample (`sample_decisions`).**
  - A score checks up to `sample` (60) of a trader's buys against the price history. Before, it took every
    `k`-th buy counting from the window's oldest. When the window moved (a later score, or new trades for a
    busy trader), one buy leaving at the start changed every buy picked.
  - Now it takes the buys whose market and outcome hash lowest. That's as good as a random draw over the
    window, and a moving window changes the sample only by the buys that enter or leave it.
  - Measured on 58 Polymarket wallets: the tier depended on which 60 bets were picked for 12 of them, and on
    whether they were recommended at all for 6.
  - Expect a one-time change of tier for traders near a threshold. After that, a tier changes when trading
    does.
- **A busy trader's bets span the whole window.**
  - A score reads at most `max_fills` (5,000) trades, newest first. For a trader who trades that much, their
    "30 days" were really their last hours or days, and the window moved with every trade.
  - When the cap stops short of the window's start, the score now also reads one page (their latest 500
    trades) from each of 10 equal slices of the rest of the window. That's 10 more reads, only for capped
    traders. The bets checked then come from the whole window: `coverage["spread"]` counts those trades.
  - Their style (market making, arbitrage) and the bot tag still use the trades read in full, as before.
  - `fills_from` keeps its meaning: where the full read stopped.
  - A slice that can't be read is left out, and the score stands on the rest.
- **A busy trader's checked bets follow the window by time.**
  - Each slice's page now ends at a point inside the slice, set by the wallet and the slice: somewhere different
    in each slice, the same on every score. Before, it was the slice's latest 500 trades, so the checked bets
    bunched at slice ends.
  - The 60 bets are split across the window by how much of it each part covers (`sample_decisions(...,
    strata=)`). Before, the latest trades, read in full, held most of the decisions read and so most of the
    sample (about 3× their share of the month).
  - Same sample size and reads.
  - Measured on 34 capped traders: the recent part is weighted fairly (×1.0, from ×3.1). Accuracy against an
    independent 300-bet reference is unchanged.
- **A read Polymarket's firewall turns away is tried once more, and a figure that still can't be read says so.**
  No tier or verdict changes: these figures are context only.
  - From a cloud host, about 0.1% of data-API reads get Cloudflare's block page: a 403 that's a web page, not the
    API's JSON (`http.is_firewall_block`). The same read passes minutes later. In a score's closed-position
    trades read, that dropped every position's exit, so `held_share` came back None, as if there were too few
    closed positions (145 production scores on 2026-10-15; 9 of them lost a value they'd otherwise have had).
  - That read is now tried once more after `FIREWALL_RETRY_S` (5 s), only for that page; any other error, or a
    second refusal, is raised as before. Each is one extra read; `firewall_retries()` counts `retried`,
    `recovered` and `failed` since the process started.
  - **`Score.unread`** names those of `held_share`, `positions_usd` and `cash_usd` that are None because their
    read failed or ran out of time, not because there's nothing to give, so a page can say "couldn't be read"
    instead of implying none. The score reads closed positions with `closed_positions(..., strict=True)`, so a
    share is never worked out from part of them. A trader page's `closed_positions()` (not strict) is
    unchanged: positions whose trades couldn't be read keep their exit unknown.
- **Resting buys count from the middle of the book in the skill test; takers count from what they paid.**
  - Before, `edge` was the price 5 minutes after a buy minus their fill price. A buy that rested on the book (a
    maker's bid) fills below the middle price, so it got half the spread as "edge" before the price moved at
    all. On Signals' 43 recommended traders (2026-10-15), that half spread was more than the whole measured
    edge for several passive bid-posters.
  - Now each checked buy's fills are classified from Polymarket's taker-only trade feed. For the share of a buy
    that rested, the half spread comes off its move before the test (`Bet.skill()`): `half_spread(mid)`, at most
    0.1¢ when a resting fill was off a whole cent (a book in 0.1¢ ticks). `edge`, `edge_by_delay`, the steady
    check and category strengths all use it. Takers are unchanged.
  - **Reads:** a live score already reads their latest taker fills for `maker_share` (now up to 1,000). When
    that read holds all of them, or reaches back past the oldest checked buy, it's used as it is: no more reads.
    Otherwise the checked markets are read 10 at a time, one after another. Measured live on 73 wallets: 1.3 more
    reads a score on average (16 of 73 needed any); with #26 and #27, 74.7 reads a score against 0.12.0's 65.1
    (+14.6%).
  - **A read that fails** (a firewall 403, a 5xx, a timeout) is tried again a market at a time. A buy whose
    market still can't be read, or a resting buy with no middle price, **isn't checked**: it's left out of the
    test, so an inflated edge can't carry a tier. `coverage["resting_known"]` counts the buys checked, and
    the "Beats the price" check says how many weren't. A market with more than 1,000 taker fills in one read is
    left untold the same way.
  - Execution is kept apart, never thrown away: `Bet.moved` still counts from their fill, `Bet.mid` is the
    middle of the book at the buy, `Bet.maker` and `Bet.spread_earned` say how much rested and what it got;
    `Score.edge_at_fill` is the old figure, `Score.spread_earned` the half spread per bet and `Score.maker_bets`
    the share of checked buys that rested. Profit figures don't change. A kept score without `edge_at_fill`
    predates this: `segment_of` can't correct it, so score the wallet again.
  - How much comes off was checked against 1-minute prices, from this change's own scores (the 43 traders Signals
    recommended on 2026-10-15 and 100 others, their last 30 days): on 767 resting buys whose book didn't move
    around the fill, it takes off more than the gain on 18 (2.3%), 2.7% of everything taken off. A price is read
    on the 0.1¢ grid with a tolerance: Polymarket's prices carry float noise (0.4900000224 is 49¢). A cap at the
    score's own 5-minute middle price was tried and dropped: that price can be minutes stale, and it let the
    spread back in for 3 wallets with no edge.
  - `Score.unread` names `edge` when the taker feed couldn't be read for some checked buys (even market by market):
    the skill test stands on fewer of their priced buys, or none, and the check says so.
  - Thresholds are unchanged. Expect a one-time drop for traders whose edge was the spread: on Signals' list,
    8 of 43 (4 Proven sharps, 4 Rising), most to "Big profit, edge unproven".
- **Scores share long-lived read threads instead of starting new pools every score.**
  - Each score started its own pools for the reads that describe now, for `account()` and for price histories: up
    to 13 new threads a score. At Signals' ~280 scores an hour that churn kept growing glibc's per-thread memory
    pools (2026-10-17: 74 pools holding 1.6 GB of 2.4 GB).
  - Now two shared pools, started on first use. A read still waiting for a thread at its score's deadline is
    cancelled. A forked child starts its own pools and their lock. Scores are unchanged.

## 0.12.1

A scorer's memory stays bounded however long it runs. Scores are unchanged.

- **A scorer's memory is bounded.**
  - A scorer keeps the markets and price histories it reads, so the next score doesn't read them again. It kept
    all of them for as long as it lived: about 0.9 MB a score, measured on 40 leaderboard wallets. A service
    scoring all day with one scorer grew without limit (Signals: ~430 MB an hour).
  - Now it keeps the most recently used: `MARKETS_KEPT` (10,000) markets and `PRICE_RANGES_KEPT` (20,000) price
    ranges, about 120 MB and 40 MB at most. What's dropped is read again when needed; with `cache_dir`, past
    price ranges come from disk.
  - Scores are unchanged.

## 0.12.0

A public read of a trader's open positions, and `match()` refusing one Polymarket to the other.

- **`client.whales.positions(venue, id, limit=50, market=None)`: a trader's open positions in one request.**
  The same list as `trader(...).positions` (open bets only: a settled Polymarket position waiting to be
  redeemed is left out), without reading the profile and stats. Pollers that want only trades and positions
  now need 2 requests a trader, `trades()` and `positions()`, instead of `trader()`'s 4. `market=` narrows it
  to one Kalshi ticker, one Polymarket `conditionId`, or one outcome as `"conditionId:outcomeIndex"`. Empty
  for a Kalshi trader who hides their holdings; `trader()` tells that apart with `visibility`.
- **`client.match(..., with_=)` refuses one Polymarket to the other.** `with_` is for a Kalshi market only. Layer
  pairs each Polymarket with Kalshi, never the two Polymarkets with each other, so a Polymarket or Polymarket US
  market looked up `with_` the other Polymarket now raises `VenueError("invalid_order")` before anything is
  sent. Before, Layer answered about the market's Kalshi pair, so a `reason` like `pending_review` read as a
  twin waiting on the other Polymarket. Go through Kalshi instead: look up the market's Kalshi match, then
  that Kalshi `market_id` with `with_=` the other Polymarket. `with_` naming the market's own venue is still
  allowed and changes nothing.

## 0.11.0

Parlays no longer crowd out the bets that can be checked, and the price test is kept apart from profit, so a
losing trader with a measured edge isn't described as having none. Thresholds are unchanged.

- **Parlays are dropped before the sample is picked.**
  - Parlay buys (combo bets, flagged `isCombo` in the trade feed) have no market listing and no price history.
  - Before, they were dropped after the `sample` bets were picked. A wallet mixing them in could end up with
    few single-market bets checked, or none.
  - `coverage["combos"]` now counts every parlay buy over the window, plus any picked bet whose market isn't
    listed. Their markets are never looked up.
- **New tier `parlays` ("Parlays: skill unmeasured").**
  - Most of their buys are parlays and too few of the rest were checked to judge.
  - Their `skill` is "unknown" and `edge_verdict` is "unmeasured". The profit history is still read and
    shown.
  - Before, "Not enough data".
- **New `edge_verdict`: the price test alone, with no profit in it.**
  - Values: "measured", "early", "not_shown" or "unmeasured".
  - "measured" means: beats the price (z ≥ 2) over `QUIET_BETS` bets, and still does without their two best.
  - `skill` still adds the track record: profitable from trading over the window, and not resting on one win.
  - Sharps still need `skill` "yes".
- **New tier `edge_losing` ("Edge, but lost money lately").**
  - The price test shows an edge, but they lost from trading over the window.
  - Before, "Edge not proven", which was wrong about the edge.
  - It is not a recommended tier.
- **`too_few` is labelled "Limited evidence"** (before, "Not enough data").
  - Its reason says how many bets could be checked and that 8 are needed to measure skill: "Too few to say
    either way".
  - A trader who buys a few times a month can't reach those bets in 30 days. That isn't a verdict on them.
- **`segment_of(kept_score)` gives these tiers to scores kept before 0.11.0.** It works out `edge_verdict` from
  the kept edge and the `steady` check, and the parlay share from the bets picked.

Why: a pre-registered test of the 13 Oct tier audit's fixes 7–9 (signals PR #71) kept the 30-day profit gate
for sharps. Wallets it keeps out had no edge the next month: −0.14¢ (−2.13¢ to +1.24¢), 20 wallets. But
"measured edge" and "profitable" are different evidence, and are now said apart. On the live 200,
MumiaTriste and technician2 trade only parlays: now `parlays`.

## 0.10.0

A trader's tier is skill and track record only: whether copying pays (`copyable`) is a separate verdict and
never moves a trader into or out of a tier. Thresholds are unchanged.

- A sharp whose copies don't pay is a proven or quiet sharp with `copyable` "no", not `too_fast`, which is
  no longer given (it stays in `Segment`, `SEGMENTS` and `RECOMMENDED` for scores kept from before).
- Rising no longer needs copies to average above zero: early signs with copies losing are Rising, where
  before they were "No clear edge" with a reason that said the price doesn't clearly move their way.
- `lucky` ("Profit rests on one win") is only big profit that would be a loss without their biggest win.
  Big profit where the price test just doesn't show skill is the new `big_profit` ("Big profit, edge
  unproven"): no evidence of skill, and none of luck either.
- `too_few` ("Not enough data") is a wallet whose `skill` is "unknown": too few bets checked (its reason now
  counts the combo bets that have no price history) or profit that couldn't be read. Before, "No clear edge".
- `no_edge` is now labelled "Edge not proven". Reasons describe skill and track record only, never copying;
  one that once said "doesn't clearly move their way" next to a clear move can't any more.
- `discover()` orders each segment by the strength of the edge (`edge.z`), no longer by the copy result.
- `segment_of(kept_score)`: this version's tier for a score kept as `Score.to_dict()` (0.8.0 or later).

On a live check of 200 wallets (13 October), `segment_of` moves the 12 Sharp but too fast to 9 Proven sharps
and 3 Rising (all with `copyable` "no"), 5 No clear edge with early signs to Rising (4) or Not trading lately
(1), 60 of 69 Lucky to Big profit, edge unproven, and 39 of 107 No clear edge to Not enough data.

## 0.9.0

Finding Polymarket wallets beyond the top 100: every leaderboard, and markets at every level of activity.

- `client.whales.find_wallets()` yields a `Sighting` for each place a wallet was seen, scoring nothing.
  Leaderboards: all 88 Polymarket publishes (profit and volume; day, week, month and all time; overall and
  each of 10 categories), each paged as deep as Polymarket goes, rank ~10,050 (past offset 10,000 it repeats
  its last page). Markets: open markets put in bands by dollars traded in the last day ($100k, $10k, $1k,
  $200 and $10 floors) and `per_band` (60) picked at random from each, so slow markets and the small
  accounts trading them are found too; the last 500 trades of each are read. Of ~18,200 open markets on 13
  October, 61 traded over $100k that day and 16,021 over $10.
- A `Sighting` says where: `source` (`"leaderboard"` or `"market"`), `where` (the board, `"PNL MONTH
  POLITICS"`, or the market's `conditionId`), `category` (as `category_of` names it; None for an overall
  board), and `rank` on a board or `trades` and `usd` in a market. A wallet comes back once per place.
- `per_second` holds the finder's reads to that rate on top of the per-host limits, so a long sweep leaves
  room for other reads on the same client. A full sweep is about 18,000 reads; reads wait for each answer, so
  it runs at about 2 a second: roughly 2.5 hours. A sample on 13 October (boards to rank 500, 20 markets a
  band, 1,229 reads) found 19,967 wallets, 3,008 of them only in markets.
- `PolymarketData.open_markets()` pages open markets by cursor (Gamma refuses an `offset` past about 2,000).
- `uselayer.whale_finder` also has `leaderboard_wallets`, `market_wallets`, `pick_markets` and `all_boards`.
  `discover()` and `find_candidates()` are unchanged.

## 0.8.2

A copy preview gives the best price on offer.

- `CopyPreview.best_price`: the lowest ask on the outcome's Polymarket order book when the preview was made,
  before the fee. It comes from the same book read that prices the amount, so no other request is made and
  it can't disagree with `price`. `price` is the average over the contracts you'd buy and `worst_price` the
  dearest level reached, so on an `ok` preview `price` − `best_price` is the amount's slippage before the
  fee. A page showing slippage used to need a second, one-contract preview to learn the best ask: another
  market read and another book read, which could see a different book. It's set when `ok` is False too
  (nothing near their price, too little for the amount, under the smallest order), so you can still show
  what's on offer, and None when the book has no asks or wasn't read (`depth_checked` False, and sells).
  In `to_dict()`.

## 0.8.1

Copies are priced at the ask, not the middle of the book.

- Polymarket's price history is the midpoint of the book (a 19¢ bid and a 20¢ ask show as 19.5¢), so a copy
  bought "at the price a minute later" was bought with no spread. Each bet's `copy` now buys at `entry`, the
  ask estimated from where that middle price sits, since past books can't be read: half a cent above it on a
  half cent (a 1¢-tick book at least a tick wide), a cent on a whole cent (two ticks or more), 0.05¢ when it's
  finer (a 0.1¢-tick book, near 0 or 1). A lower bound: a wider book cost a copier more. The taker fee is
  taken at that price. `half_spread(mid)` is the rule. `copy`, `copyable`, the segments that rest on them and
  each category's `copy` all include it. Estimated on the 12 traders Signals recommended on 13 October, it
  takes about half a cent a bet off each copy result and changes none of their segments.
- `Score.copy_entry`: what a copier paid a contract on average over the same bets as `copy`, events
  weighted the same way, so `copy.mean / copy_entry` is a return per dollar paid on those bets.
  `avg_price` is over every buy in the window, not the ones checked, and isn't the right divisor. In
  `to_dict()`; None without `copy`. `Bet.entry` is each bet's.
- `uselayer.venues.polygon` says why USDC.e isn't counted as cash: it has to be wrapped into pUSD before
  it can trade, and the 200 wallets of one check held $10.61 of it.

## 0.8.0

Trader scores you can trust at face value: copying is called copyable only when it's reliably above zero,
profit says what's taken and what's still open, missing history isn't counted as losing weeks, and a
wallet's account says when it was read and whether it's whole.

- Copyable means reliably above zero. `copyable` is `"yes"` only when the bottom of the 95% range of what a
  copier a minute or more late kept after the fee (`copy.low`) is above zero, over at least `COPY_BETS` (8)
  independent bets; under 8 it's `"unknown"`. Before, an average above zero at one standard error (z ≥ 1)
  was enough, so a copy result that could well be zero was called copyable. Segments follow: a skilled trader
  whose copies aren't reliably positive is `too_fast` ("Sharp but too fast"), never proven or quiet. The
  check and reason say "not reliably above zero" and give the range. `copyable_verdict(stat)` is the rule.
  `discover()` ranks within a segment by `copy.low`.
- 95% ranges use Student's t. `Stat.margin` is t for `n − 1` degrees of freedom times the standard error
  (1.96 only for many bets), so a few bets get an honestly wide range. `Stat.low` and `Stat.high` are its
  ends, and are in `to_dict()`. `Stat.from_dict()` reads one back, widening a margin kept before 0.8.0.
- Realized and unrealized apart. `Score.realized_window` and each period's `realized` and `unrealized` split
  the trading profit into what was taken (sold or settled) and the change on open positions; they add up to
  the trading figure. Rewards, rebates and yield stay in `income` only.
- Weeks before a trader's history are None. Polymarket's profit history starts at the wallet's first trade;
  `pnl_weeks` used to give the weeks before it as $0, which a page counting "weeks in profit out of 12"
  counted against them. Those weeks are now None. `pnl_since` is when the history starts, so a period that
  begins earlier can say it counts from then.
- `Score.fills_from`: when the `max_fills` cap was reached, the time of the oldest trade read, so "a day"
  figures can use the span actually read. None when the whole window was read.
- A wallet's account: `client.whales.account(wallet)` reads their open positions' worth and their pUSD cash
  now, as an `Account` with `read_at`, `complete`, `total_usd` (only when both parts were read: one part is
  never the whole) and `missing`. `Score.account_at` says when a score's `positions_usd` and `cash_usd`
  were read.
- Cash reads fall back to other public Polygon nodes (`uselayer.venues.polygon.RPCS`: 1rpc.io, then
  publicnode, then dRPC) when one refuses or doesn't answer in 4 s, instead of giving up after one. With
  `USELAYER_POLYGON_RPC` set, only that node is asked. Still a read only.
- Polymarket trader pages: `stats["trading_pnl"]` (realized plus unrealized, after fees), `realized_pnl`,
  `unrealized_pnl` and `income` (rewards, rebates, yield), next to `stats["pnl"]`, Polymarket's own figure
  with rewards in. `Trader.pnl` on that page is now the trading figure, as on the leaderboard; None when
  Polymarket doesn't give it, never its rewards-inclusive figure instead.

## 0.7.0

What a trader's page needs beyond the score: their closed positions, how they get out, the prices they buy
at and their account now.

- `client.whales.trader("polymarket", wallet).closed`: up to 50 (`closed=`) positions they no longer hold,
  newest first, as `ClosedPosition`: `market`, `title`, `outcome`, `url`, `size` (contracts bought),
  `avg_price` (their entry), `exit_price` (their average sell price, and 1 or 0 for any held until the market
  settled), `pnl` (Polymarket's realized profit, after fees), `result` (`"won"`, `"lost"` or `"sold"`),
  `settled` and `closed_at`. From Polymarket's public closed positions, with their trades in those markets
  read too (one read per 20 markets): Polymarket's own `avgPrice` is the last lot's cost only, and its
  `curPrice` is the market's price, not their exit. A position counts as sold when they sold at least half
  of it before it settled. Unknown stays None: `closed` on Kalshi or when unreadable; `result`, `avg_price`
  and `exit_price` when their trades can't be read or a held market hasn't settled.
- New on `Score`, context that never decides a segment: `price_band`, the middle half of the prices they
  paid on the bets checked (`(p25, p75)`, 0 to 1; None under 5 bets); `held_share`, the share of their last
  50 closed positions held until the market settled rather than sold (None under 10 with a known exit);
  `positions_usd`, what their open positions are worth now (Polymarket's figure); and `cash_usd`, the pUSD
  in their wallet, read from the Polygon chain. `held_share`, `positions_usd` and `cash_usd` describe now:
  None for a past window (`end=`) and whenever they can't be read. They're read in parallel alongside the
  price checks, and a score waits at most `NOW_WAIT_S` (10 s) past its own reads for them, so a slow node
  can't hold a score up.
- Cash is the package's first read from a chain: one `eth_call` (`balanceOf` on Polymarket's pUSD,
  `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) through the public node `https://1rpc.io/matic`, or the node
  in `USELAYER_POLYGON_RPC`. `uselayer.venues.polygon` only reads: nothing signs or sends a transaction, and
  a test keeps it that way. The free public node now and then refuses a call; it's tried once more (and one
  that doesn't answer in 5 s, once), then `cash_usd` is None. Set `USELAYER_POLYGON_RPC` to your own node for steady reads.
- `PolymarketData.closed_positions()`, `positions_value()` and `trades_in(wallet, markets)`.
  `Http.request()` takes `timeout_s=` and `retries=` to tighten its defaults for one request.

## 0.6.0

Trader scores (`client.whales.score()` / `discover()`) separate skill from copyability and count profit
from trading only.

- Two verdicts on every `Score`: `skill` (`"yes"`, `"early"`, `"no"`, `"unknown"`) and `copyable` (`"yes"`,
  `"no"`, `"unknown"`). `segment` is built from them as before: proven and quiet sharps are both, too fast is
  skilled but not copyable. `style` (`"takes_a_side"`, `"market_maker"`, `"arbitrage"`) keeps market makers
  and arbitrage apart from traders without skill: their skill is `"unknown"`, not `"no"`.
- Profit from trading. Polymarket's own profit (`pnl`, `pnl_window`, unchanged) includes rewards, maker
  rebates and yield. New, from its profit history: `trading_pnl` (all time) and `trading_pnl_window`
  (realized plus unrealized, after fees), `income_window` (rewards, rebates, yield), `unrealized_window`,
  `volume_window` (dollars traded), `return_per_dollar` (trading profit per dollar traded; not a return on
  capital, which Polymarket's data can't give), `best_day_share`, `periods` (the same for 7, 30 and 90
  days), `pnl_weeks` (12 weeks) and `pnl_daily` (90 days).
- Stricter skill. Skill `"yes"` or `"early"` now also needs a profit from trading over the window and a
  steady edge (it holds without their two best bets, and their all-time trading profit without their biggest
  win). Before, a trader down $101K over the month could be Rising, and an edge resting on one big win could
  be Rising or too fast. Lucky big bettors are judged on trading profit, so rewards don't make one.
- Trading lately: a trader who'd be proven, quiet, rising or too fast but hasn't bought in `ACTIVE_DAYS` (7)
  days is the new segment `"inactive"` ("Not trading lately", `skip`). `active` and `last_buy` say so.
- New checks `profitable` and `active`, and `risks`: plain sentences for rewards that out-earned trading,
  profit mostly on open positions, profit mostly from one day, and an all-time record carried by one win.
- Context that never decides a segment: `typical_usd` (median buy), `avg_price` (average price paid) and
  `maker_share`, now read for every live score, not only for suspected market makers.
- Fix: `volume` (and the "big account" test behind proven vs quiet) was Polymarket's count of contracts
  traded; it is now dollars traded.
- `SEGMENTS["no_view"]` is labelled "Market makers and arbitrage".

Checked offline on the 310 wallets of `research/overlooked-*` in the Signals repo, scored on 13 Aug–12 Sep
and followed 12 Sep–12 Oct: the 21 traders with skill `"yes"` or `"early"` kept a 5-minute edge the next
month (+1.07¢ ± 0.58¢) and 81% made money from trading, against 30% of the 169 with skill `"no"`. Copying
still isn't shown to pay: the new recommended tiers made −0.13¢ ± 0.60¢ a contract after fees (7 wallets).

## 0.5.6

- Copiers mirror their sells in proportion. With `copy_sells=True` (the default), when the trader sells 40% of
  their position the copy sells 40% of the contracts you hold on that side, and all of them when they sell
  out, at no less than their price minus `max_slippage`. Before, a sell copied `size` contracts (capped at
  what you held), so one small trim could sell your whole copy. The share comes from their position now (the
  venue's public positions: Polymarket's by outcome token, in that market only; Kalshi's netted holdings),
  with the trades they've made since put back, so a trim and a sell-out seen in one poll are copied as a
  trim and a sell-out. `CopyEvent.share` (and `CopyPreview.share`) is the part they sold; the reason says
  it. A share of your contracts that rounds to zero, or a position that can't be read, is skipped with the
  reason. `min_usd` now skips small buys only: a small sell can still be a whole exit.
- Fix: a copied sell on Polymarket international looked for your position under the feed's raw outcome
  index; it now uses the checked market the buy was recorded under (as in 0.5.5).
- `PolymarketData.positions(wallet, market=conditionId)` reads one market's positions.

## 0.5.5

- Polymarket copies are priced for their size: `preview_copy`, `copy_trade` and `follow(venue="polymarket")`
  read the outcome's public order book and walk the asks up to their price plus `max_slippage`, with the
  taker fee at each level. `CopyPreview.price` is the average a contract costs for that amount,
  `worst_price` the dearest level reached, and `max_spend` everything for sale that close to their price,
  fee included. An amount the book there can't fill previews `ok=False` ("Only $X is for sale at 0.55 or
  better right now") and isn't copied. If the book can't be read, the price falls back to Gamma's best
  price and `depth_checked` is False. This is a read only: the package still has no Polymarket
  international order code (`test_public_repo.py` allows the book host in one file, for `/book` alone).
- Fix: Polymarket's trade feed sometimes sends a junk `outcomeIndex` (999). A copy no longer treats that as
  the second outcome (which bought the other side). `WhaleTrade.token` now keeps the exact outcome token
  traded, and copies match it against the market's outcome tokens first; without one, the index is checked
  against the market's outcomes, then the outcome's name. A trade none of these places is skipped. Copies
  record the checked market.

## 0.5.4

- `client.whales.preview_copy(trade, spend=50)` / `copy_trade(trade, spend=50)`: size a one-trade copy in
  dollars, fee included, instead of contracts: the most whole contracts whose cost plus Polymarket's taker
  fee fits in `spend`, at the price now (still at most `max_slippage` above theirs). `CopyPreview.spend` says
  the amount asked for. An amount that doesn't buy the market's smallest order previews `ok=False` and says
  what the smallest order costs. `size=` works as before (5 contracts when you give neither); passing both
  raises. The price is Polymarket's best price, and its public data doesn't show how much is offered there,
  so a large amount may not really fill at it.

## 0.5.3

- `client.whales.follow(trader, venue="polymarket")`: copy a Polymarket trader on the same Polymarket
  international market and outcome, in paper mode (live mode raises: this SDK doesn't trade there).
  Nothing is skipped for want of a twin on Kalshi or Polymarket US. Each copy fills at once at the
  outcome's best price on Polymarket's public market data (Gamma), with Polymarket's taker fee; Gamma
  has no depth, so the whole size fills at that price. The client's rules don't run on these copies (the
  kill switch does). `copy_results()` values them at Polymarket's best bid and pays them out when
  Polymarket resolves the market; `client.pnl()` counts them once paid out but can't value an open one.
- `client.whales.preview_copy(trade, size=5)`: what buying one Polymarket trader's buy would do now, on the
  same Polymarket international market and outcome, sending nothing: `price`, `fee`, `total` you'd pay (and
  lose if it loses), `payout` and `profit_if_win`, or `ok=False` with the `reason` (more than `max_slippage`
  above their price, market not taking orders). `copy_trade(trade, size=5)` buys it in paper mode however old
  the trade is (a follower only copies trades made after it starts) and returns a `CopyEvent` that works with
  `copy_results()`. New `CopyPreview`.

## 0.5.2

- `Score.pnl_window` is no longer `None` for an account newer than the window: its profit history
  starts inside it, so it counts from $0 (all its profit was made in the window).

## 0.5.1

- `WhaleTrade.url`: the bet's page on polymarket.com (its event, opened on the market). `None` on Kalshi.
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
