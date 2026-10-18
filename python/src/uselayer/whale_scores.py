"""Is a Polymarket trader worth following? A score from their public trades.

    s = client.whales.score("0x…")                   # one wallet, its last 30 days
    print(s.segment, s.reason)                        # "quiet", "Small account, off the leaderboards…"
    print(s.skill, s.copyable)                        # two verdicts: "yes", "yes"
    for c in s.checks:
        print(c.rule, c.passed, c.detail)             # the evidence, one plain sentence per rule
    found = client.whales.discover(wallets=150)       # wallets from leaderboards AND from markets' trades

Three separate questions: is the trader profitable from trading (their profit figures and ``risks``), is
there evidence of skill (``skill``), and can a copier a few minutes behind still gain (``copyable``)? A
profitable-looking trader isn't always skilled, and a skilled one isn't always worth copying. ``segment``
is the tier, from skill and track record only: copying never moves a trader into or out of one (since
0.10.0). A proven sharp whose copies don't pay is still a proven sharp, with ``copyable`` "no".

Polymarket only: every wallet's full history is public there. The rules, in order of weight:

1. **Beats the price.** After they buy, does the price move their way? Each bet is checked 1, 5, 15 and
   60 minutes later against Polymarket's own price history. That history comes in 5-minute steps, so
   "5 minutes later" is the first step at least 5 minutes after the buy (5 to 10 minutes). The test is
   the 5-minute move: on 160 wallets (2026-10-07) it was the one that held up, a trader's earlier bets
   predicting their later ones, where the hour mark is mostly the noise of games playing out. A buy that
   rested on the book (a maker's bid) counts from the middle of the book, not from what they paid: it filled
   below the middle price, half the spread their way before the price moved at all. That half spread
   (:func:`half_spread`, for the share of the buy that rested, at most what they paid below the middle) is how
   they buy, not what they foresaw: it's taken off the test and kept apart (``spread_earned``, ``maker_bets``,
   ``edge_at_fill``). A buy that took liquidity counts from what they paid, as before. Which fills rested comes
   from Polymarket's taker-only trade feed: the read a live score already makes for ``maker_share`` when it
   reaches back far enough, else one read per 10 markets checked. A buy that can't be told isn't checked.
2. **Enough independent bets.** One bet per market, and bets on the same event (a game's winner and its
   total) count once. The confidence level comes from how many there are and how far the average is
   from zero.
3. **Steady.** The record doesn't hang on one or two lucky bets: the edge holds with their two best
   bets removed, and their all-time profit holds without their biggest win.
4. **Copyable.** Buying the same thing at the first price at least a minute later (1 to 6 minutes:
   Polymarket's public trade feed itself runs minutes behind), at the ask, and paying Polymarket's taker
   fee (``rate × (p(1−p))^exponent`` a contract, from the market's ``feeSchedule``), still gains by the
   hour mark, reliably: the bottom of the 95% range of that gain is above zero, over at least
   ``COPY_BETS`` independent bets. A positive average that could still be zero isn't copyable.
   Polymarket's price history is the middle of the book, not what a buyer pays, and past books can't be
   read, so the ask is estimated from where that middle price sits (:func:`half_spread`): half a cent above
   it at least, a cent when the book was two ticks wide or more.
5. **Takes a side.** Market makers (trading both ways in the same market within the hour, mostly with
   resting orders) and arbitrage (buying both outcomes of a market within a minute for $1 or less) have
   no view to follow and are left out. Merging a set back alone isn't arbitrage: it's also how a trader
   with a view closes a bet (buy the other outcome later, merge for $1). Being a bot isn't a penalty: ``"bot"`` is only a tag.
6. **Category strengths.** The same test per category (sports, crypto…), so you can follow a trader
   only where their edge is.
7. **Profitable from trading.** Their profit over the window, from Polymarket's own profit history, counts
   trading only: realized plus unrealized, after fees. Rewards, maker rebates and yield
   (``income_window``) are kept apart, so a market maker paid in rewards doesn't look like a winning
   trader. A skilled trader made money from trading over the window.
8. **Trading lately.** No buy in the last ``ACTIVE_DAYS`` days: not recommended, whatever the record.

The tiers (``segment``), from rules 1–3 and 5–8; rule 4 is ``copyable`` and never moves a tier:

- **Proven / Quiet sharps**: ``skill`` "yes" over ``PROVEN_BETS`` bets on a big account (on a leaderboard,
  ``BIG_VOLUME`` traded or twice ``BIG_PROFIT`` from trading), or ``QUIET_BETS`` on a small one.
- **Rising**: ``skill`` "yes" or "early", short of a sharp's bets.
- **Profit rests on one win** (``lucky``): ``BIG_PROFIT`` or more from trading, but down without their
  biggest win: evidence of luck.
- **Big profit, edge unproven** (``big_profit``): ``BIG_PROFIT`` or more from trading, but the price test
  doesn't show skill. Not called luck: there's no evidence of that either.
- **Parlays: skill unmeasured** (``parlays``): most of their buys are parlays (combo bets), which have no
  price history, and too few of the rest were checked to judge. Their profit still counts as a track record.
- **Limited evidence** (``too_few``): ``skill`` "unknown": too few bets checked (a trader who buys a few
  times a month can't reach them in 30 days: not a verdict), or profit unreadable.
- **Edge, but lost money lately** (``edge_losing``): the price test shows an edge (``edge_verdict``
  "measured"), but they lost from trading over the window, so ``skill`` isn't "yes" and they're no sharp.
- **Edge not proven** (``no_edge``): the rest: the price doesn't clearly move their way, or it does but
  it rests on a few bets.
- **Market makers and arbitrage** (``no_view``), and **Not trading lately** for a recommended tier.

``edge_verdict`` is the price test alone, profit never an input: ``"measured"`` (beats the price over
``QUIET_BETS`` bets and still without their two best), ``"early"`` (signs of it), ``"not_shown"`` or
``"unmeasured"`` (too few bets with a price history, parlays included, or no view to test). ``skill`` is
that plus the track record (profitable from trading, not resting on one win), and sharps need ``skill``.

Every number is per bet with equal weight, so one huge bet can't decide a score. A score is a reading of
the past, not a promise: show its ``confidence``, ``checks`` and ``risks`` with it. Typical buy size,
entry price, maker share and return per dollar traded are context: none of them decides the segment.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import logging
import math
import statistics
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, TypeVar

from .errors import VenueError
from .http import is_firewall_block
from .venues.polygon import Polygon
from .venues.polymarket_data import PolymarketData

log = logging.getLogger(__name__)
T = TypeVar("T")

Segment = Literal[
    "proven",
    "quiet",
    "rising",
    "too_fast",
    "big_profit",
    "lucky",
    "parlays",
    "too_few",
    "edge_losing",
    "no_view",
    "inactive",
    "no_edge",
]
Skill = Literal["yes", "early", "no", "unknown"]
EdgeVerdict = Literal["measured", "early", "not_shown", "unmeasured"]
Copyable = Literal["yes", "no", "unknown"]
Style = Literal["takes_a_side", "market_maker", "arbitrage"]

#: Each segment's name on a page and what to do about it. The tier is skill and track record only; whether
#: copying pays is ``copyable``, a separate verdict.
SEGMENTS: dict[str, tuple[str, str]] = {
    "proven": ("Proven sharps", "follow"),
    "quiet": ("Quiet sharps", "follow"),
    "rising": ("Rising", "watch"),
    # before 0.10.0 only: now a proven or quiet sharp with copyable "no"
    "too_fast": ("Sharp but too fast", "signal"),
    "big_profit": ("Big profit, edge unproven", "skip"),
    "lucky": ("Profit rests on one win", "avoid"),
    "parlays": ("Parlays: skill unmeasured", "skip"),
    "too_few": ("Limited evidence", "skip"),
    "edge_losing": ("Edge, but lost money lately", "skip"),
    "no_view": ("Market makers and arbitrage", "skip"),
    "inactive": ("Not trading lately", "skip"),
    "no_edge": ("Edge not proven", "skip"),
}
RECOMMENDED = (
    "proven",
    "quiet",
    "rising",
    "too_fast",  # never given since 0.10.0, but a score kept from before may have it
)  # the segments a trader who stops trading drops out of

DELAY_S = 60  # how late a copier gets in
HORIZONS = (60, 300, 900, 3600)  # when the price is checked after their buy
SKILL_S = 300  # the move that tests skill ("beats the price")
HOLD_S = 3600  # when a copier's gain is measured
RISING_BETS = 8  # bets before early signs count
MERGE_S = 60  # fills on the same outcome this close together are one decision
PROVEN_BETS = 40  # independent bets for a proven sharp
QUIET_BETS = 25  # ... and for a quiet one
CATEGORY_BETS = 8  # bets in a category before it can count as a strength
COPY_BETS = 8  # independent bets with a copy result before copying can be called reliable
BIG_PROFIT = 50_000  # all-time trading profit that makes a "big bettor"
BIG_VOLUME = 1_000_000  # all-time dollars traded that make an account big (not "quiet")
ACTIVE_DAYS = 7  # no buy in this many days before the window's end: not trading lately
MAKER_TWO_WAY = 0.5  # share of their volume traded both ways in the same market-hour
ARB_SHARE = 0.3  # share of markets with a complete set bought
SOLD_SHARE = 0.5  # sold at least this share of a position before it settled: they got out by selling
HELD_MIN = 10  # closed positions (with a known exit) before ``held_share`` is given
# the reads a score makes alongside it, by the figure each gives (``Score.unread`` names the figure)
UNREAD_FIELDS = {"held": "held_share", "positions": "positions_usd", "cash": "cash_usd"}
BAND_MIN = 5  # bets checked before ``price_band`` is given
TAKER_MARKETS = 10  # markets per read of the taker-only trade feed (which buys rested on the book)
TAKER_LIMIT = 1000  # fills per read of it: a read that comes back full is read again a market at a time
# A market with more taker fills than that in one read is left untold: its checked buys aren't checked.
SPREAD_SLICES = (
    10  # when the max_fills cap stops short of the window's start: a page from each of these slices
)
NOW_WAIT_S = 10  # how long a score waits, after its own reads, for how they exit, their positions and cash
POLYMARKET_EVENT = "https://polymarket.com/event/"

#: Polymarket tags, in the order they decide a market's category.
CATEGORY_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Esports", ("esports", "counter strike 2", "league of legends", "dota 2", "valorant")),
    ("Sports", ("sports", "games", "soccer", "nba", "nfl", "mlb", "nhl", "tennis", "cricket", "ufc")),
    ("Crypto", ("crypto", "crypto prices", "bitcoin", "ethereum", "solana", "xrp")),
    ("Weather", ("weather", "climate", "temperature")),
    ("Economy", ("economy", "fed", "fed rates", "macro indicators", "finance", "stocks", "earnings")),
    ("Politics", ("politics", "elections", "geopolitics", "world", "trump", "us election")),
    ("Tech", ("tech", "ai", "science")),
    ("Culture", ("culture", "mentions", "tweet markets", "awards", "movies", "music", "pop culture")),
)
FEE_TYPE_CATEGORY = {
    "sports": "Sports", "crypto": "Crypto", "politics": "Politics", "economics": "Economy",
    "finance": "Economy", "culture": "Culture", "weather": "Weather", "tech": "Tech",
}  # fmt: skip


def category_of(market: dict[str, Any]) -> str:
    """A Gamma market's category, from its tags (or its fee type when it has none)."""
    labels = {str(t.get("label") or "").lower() for t in market.get("tags") or []}
    for name, tags in CATEGORY_TAGS:
        if labels & set(tags):
            return name
    fee_type = str(market.get("feeType") or "")
    return FEE_TYPE_CATEGORY.get(fee_type.split("_", 1)[0], "Other")


def taker_fee(market: dict[str, Any], p: float) -> float:
    """Polymarket's taker fee per contract at price ``p``: ``rate × (p(1−p))^exponent``."""
    s = market.get("feeSchedule") or {}
    if not market.get("feesEnabled") or not s.get("rate"):
        return 0.0
    fee: float = float(s["rate"]) * (p * (1 - p)) ** float(s.get("exponent") or 1)
    return fee


def half_spread(mid: float) -> float:
    """How far above Polymarket's middle price ``mid`` a buyer paid, at least: half the bid–ask spread, read
    from where ``mid`` sits. Polymarket's price history is the midpoint of the book, and past books can't be
    read. A midpoint on a half cent (52.5¢) is a 1¢-tick book an odd number of ticks wide, 1 at least: half a
    cent. One on a whole cent (52¢) is two ticks wide or more: a cent. Anything finer (99.95¢) is a 0.1¢-tick
    book, near 0 or 1: 0.05¢. A lower bound: a wider book cost more."""
    v = round(mid * 10_000)  # hundredths of a cent, so 0.9995 isn't rounded onto a whole cent
    if v % 100 == 0:
        return 0.01
    if v % 50 == 0:
        return 0.005
    return 0.0005


def _payout(market: dict[str, Any], outcome: int) -> float | None:
    """1 or 0 once settled; None while open, or if voided or split."""
    if not market.get("closed"):
        return None
    try:
        prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return None
    if len(prices) <= outcome or prices[outcome] not in (0.0, 1.0):
        return None
    return prices[outcome]


def polymarket_url(event: str | None, market: str | None) -> str | None:
    """The market's page on polymarket.com: its event, opened on the market when it's one of several."""
    if not event:
        return None
    return POLYMARKET_EVENT + event + (f"/{market}" if market and market != event else "")


# ---- what you get back ----


# Student's t at 97.5% for 1 to 30 degrees of freedom: a 95% range on a few bets is wider than 1.96 errors.
_T95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
    2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def t95(df: int) -> float:
    """How many standard errors either side of a mean make its 95% range, with ``df`` degrees of freedom."""
    if df < 1:
        return math.inf
    return _T95[df - 1] if df <= len(_T95) else 1.96 + 2.5 / df  # within 0.002 of the exact value past 30


@dataclass(frozen=True)
class Stat:
    """An average per bet with its 95% range: ``mean ± margin`` over ``n`` independent bets.

    Bets on the same event are averaged into one first, so ``n`` counts events. ``margin`` is Student's t
    for ``n − 1`` degrees of freedom times the standard error, so a few bets get a wide range. ``low`` and
    ``high`` are the ends of the range: ``low`` above zero means the average is reliably positive. ``z`` is
    the mean over its standard error: 2 or more means it's unlikely to be luck.
    """

    mean: float
    margin: float
    n: int
    z: float

    @property
    def low(self) -> float:
        return round(self.mean - self.margin, 5)

    @property
    def high(self) -> float:
        return round(self.mean + self.margin, 5)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"low": self.low, "high": self.high}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Stat:
        """A stat from :meth:`to_dict`. One kept by uselayer before 0.8.0 (no ``low``) had a margin of 1.96
        standard errors whatever the number of bets: it's widened to Student's t, as a new one would be."""
        s = cls(mean=float(d["mean"]), margin=float(d["margin"]), n=int(d["n"]), z=float(d["z"]))
        if "low" not in d:
            s = replace(s, margin=round(s.margin / 1.96 * t95(s.n - 1), 5))
        return s


def copyable_verdict(copy: Stat | None) -> Copyable:
    """Would copying a few minutes late have gained, reliably? ``"yes"`` when the bottom of the 95% range of
    what a copier kept after the fee is above zero, ``"no"`` when it isn't, and ``"unknown"`` under
    ``COPY_BETS`` independent bets with a copy result. A positive average that could still be zero is ``"no"``."""
    if copy is None or copy.n < COPY_BETS:
        return "unknown"
    return "yes" if copy.low > 0 else "no"


@dataclass(frozen=True)
class Bet:
    """One buy decision and what the price did next.

    ``price`` is what they paid. ``moved`` maps seconds after the buy (60, 300, 900, 3600) to how far the price
    had moved their way, in dollars a contract (0.02 = 2¢). ``copy`` is what a copier buying a minute
    later made by the hour mark, after the taker fee: bought at ``entry``, the ask estimated from the middle
    price then (:func:`half_spread`), with ``fee`` the taker fee at that price. ``payout`` is 1 or 0 once the
    market settled.

    ``moved`` counts from their fill price. ``mid`` is the middle of the book when they bought, as the price
    history has it. ``maker`` is the share of the buy's contracts that rested on the book (a maker's order) and
    ``spread_earned`` what that got them: ``maker`` × the half spread (:func:`half_spread`), at most what they
    paid below the middle (None when either can't be told). :meth:`skill` is the move the skill test uses:
    resting buys count from the middle, takers from what they paid.
    """

    market: str
    title: str | None
    outcome: str | None
    event: str
    category: str
    at: datetime
    price: float
    usd: float
    moved: dict[int, float]
    copy: float | None
    fee: float | None
    payout: float | None
    entry: float | None = None
    mid: float | None = None
    maker: float | None = None
    spread_earned: float | None = None

    def skill(self, h: int = SKILL_S) -> float | None:
        """The price move their way ``h`` seconds after the buy for the skill test: the part that rested on the
        book counts from the middle of the book (``moved`` less ``spread_earned``), the part that took liquidity
        from what they paid. None when it can't be told which part rested (``maker`` None), or how much a resting
        part got (``spread_earned`` None): such a buy isn't checked, so it can't carry a tier on a spread."""
        m = self.moved.get(h)
        if m is None or self.maker is None or (self.maker > 0 and self.spread_earned is None):
            return None
        return round(m - (self.spread_earned or 0.0), 5)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        d["moved"] = {str(k): v for k, v in self.moved.items()}
        return d


@dataclass(frozen=True)
class ClosedPosition:
    """A position they no longer hold: sold, or held until its market settled.

    ``market`` is ``<conditionId>:<outcomeIndex>``. ``size`` is the contracts they bought (Polymarket's
    count), ``avg_price`` the average price they paid on their buys (their entry, before fees) and
    ``exit_price`` what they got for one on average: the price of their sells, and 1 or 0 for any still held
    when the market settled. Both come from their trades in that market, and are None when those couldn't be
    read (or, for the exit, when contracts they still held haven't paid out). ``pnl`` is Polymarket's realized
    profit on the position, after fees.

    ``result`` is ``"won"`` or ``"lost"`` when they held it until the market settled (it paid 1 or 0), and
    ``"sold"`` when they sold at least half of it first. It's None when that can't be told: their sells
    couldn't be read, or the market was voided or they merged the set back. ``settled`` is True for won or
    lost, False for sold, None with it. ``closed_at`` is Polymarket's time for the position.
    """

    market: str
    title: str | None
    outcome: str | None
    url: str | None
    size: float
    avg_price: float | None
    exit_price: float | None
    pnl: float | None
    result: Literal["won", "lost", "sold"] | None
    settled: bool | None
    closed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["closed_at"] = self.closed_at.isoformat() if self.closed_at else None
        return d


@dataclass(frozen=True)
class Check:
    """One rule's result: ``passed`` is True, False, or None when there's too little data to say."""

    rule: Literal[
        "beats_price",
        "enough_bets",
        "steady",
        "profitable",
        "copyable",
        "takes_a_side",
        "active",
        "categories",
    ]
    title: str
    passed: bool | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CategoryScore:
    """The price test in one category. ``strong`` when the edge there is clear (z ≥ 2, enough bets)."""

    category: str
    bets: int
    edge: Stat | None
    copy: Stat | None
    strong: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "bets": self.bets,
            "edge": self.edge.to_dict() if self.edge else None,
            "copy": self.copy.to_dict() if self.copy else None,
            "strong": self.strong,
        }


@dataclass(frozen=True)
class Score:
    """Whether one Polymarket wallet is worth following, and why.

    ``segment`` is one of :data:`SEGMENTS`, the tier: from ``skill`` and their track record, never from
    ``copyable``. ``action`` is what to do about it (``"follow"``, ``"watch"``, ``"avoid"`` or ``"skip"``;
    ``"signal"`` only on a ``too_fast`` score kept from before 0.10.0). Following a trader is for watching
    what they trade: whether a copy would gain is ``copyable``. ``reason`` is one plain sentence; ``checks``
    hold the evidence for each rule. ``edge`` is the price move their way 5 minutes
    after they buy (the skill test, :meth:`Bet.skill`: resting buys count from the middle of the book, takers
    from what they paid); ``edge_at_fill`` the same with every buy from what they paid, before the half spread a
    resting order got is taken off. ``spread_earned`` is that half
    spread per bet (how they buy, not a forecast) and ``maker_bets`` the share of the checked buys that rested
    on the book; and ``copy`` what a copier 1–6 minutes late made by the hour mark after
    the taker fee, buying at the ask (estimated: :func:`half_spread`), both in dollars a contract.
    ``copy_entry`` is what that copier paid a contract on average over the same bets (events weighted as in
    ``copy``), so ``copy.mean / copy_entry`` is their return per dollar paid; None without ``copy``.
    ``coverage`` is how many of the bets checked had a price history.

    The two verdicts: ``skill`` is ``"yes"`` (beats the price, steady, enough bets, profitable from trading),
    ``"early"`` (good signs, too few bets to be sure), ``"no"`` or ``"unknown"`` (too little to tell, or a
    market maker or arbitrage trader, whose ``style`` this test doesn't fit). ``copyable`` is ``"yes"``,
    ``"no"`` or ``"unknown"``, from ``copy`` alone, and it has no part in ``segment``. ``edge_verdict`` is
    the price test alone, with no profit in it (``"measured"``, ``"early"``, ``"not_shown"``,
    ``"unmeasured"``): a trader who lost money over the window can still have a measured edge, and
    ``skill`` is where the track record comes in. ``coverage["combos"]`` counts their parlay buys over the
    window (none of which can be priced, so none are checked) and any bet checked whose market isn't listed.

    Profit: ``pnl`` and ``pnl_window`` are Polymarket's own figure, rewards and rebates included.
    ``trading_pnl`` (all time) and ``trading_pnl_window`` count trading only (realized plus unrealized, after
    fees); ``income_window`` is the rewards, maker rebates and yield over the window (never in a trading
    figure). ``realized_window`` is the part of the window's trading profit taken (sold or settled) and
    ``unrealized_window`` the change in open positions' paper profit: the two add up to ``trading_pnl_window``. ``volume_window`` is the
    dollars they traded (bought and sold), and ``return_per_dollar`` the trading profit over it.
    ``best_day_share`` is how much of a positive trading profit came on their best day. ``periods`` has the
    same for the last 7, 30 and 90 days (``trading_pnl``, ``realized``, ``unrealized``, ``income``, ``volume``,
    ``return_per_dollar``), ``pnl_weeks`` the trading profit of each of the last 12 weeks (oldest first; None
    for a week that ended before their history starts, which is neither a losing nor a flat week), and
    ``pnl_daily`` the trading profit since 90 days ago at the end of each day (``(date, dollars)``, from 0).
    ``pnl_since`` is when their history starts (their first trade): a period that begins earlier counts from
    then. All None when Polymarket's profit history couldn't be read. ``volume`` is all-time dollars traded.
    ``fills_from`` is set only when the ``max_fills`` cap was reached, so the window's older trades weren't
    all read: it's the time of the oldest trade read in full. None when the whole window was read (a trader who
    only started 12 days ago had every trade read, and gets None). From 0.13 a capped trader's bets still span
    the window: a page from each of SPREAD_SLICES slices of the rest is read for them (``coverage["spread"]``
    trades), while their style and the bot tag stay on the trades read in full.

    Context, never deciding the segment: ``typical_usd`` (their median buy over the window), ``avg_price``
    (their average price paid), ``maker_share`` (share of recent fills that were resting orders; None for
    a past window or under 20 fills), ``last_buy``, and ``active`` (a buy within ``ACTIVE_DAYS`` of the
    window's end). ``risks`` are plain sentences worth saying next to any recommendation.

    How they trade, also context: ``price_band`` is the middle half of the prices they paid on the bets
    checked, ``(25th, 75th percentile)`` from 0 to 1 (``(0.3, 0.7)``: they buy around 30–70¢); None under
    ``BAND_MIN`` bets. ``held_share`` is the share of their last 50 closed positions they held until the
    market settled rather than sold (see :class:`ClosedPosition`); None under ``HELD_MIN`` closed positions
    with a known exit. Their account now: ``positions_usd`` is what their open positions are worth
    (Polymarket's figure) and ``cash_usd`` the pUSD in their wallet, read from the Polygon chain;
    ``account_at`` is when they were read. These three describe the present, so they're None for a past
    window (``end``) and whenever they can't be read. One read without the other is part of their account,
    never all of it. ``unread`` names those of ``held_share``, ``positions_usd`` and ``cash_usd`` that are None
    because their read failed or took too long, rather than because there's nothing to give (fewer than
    ``HELD_MIN`` closed positions, no Polygon reader): so a page can say "couldn't be read", never imply none.
    Context only, like the rest of these: the tier never reads them.
    """

    wallet: str
    name: str
    segment: Segment
    action: str
    reason: str
    confidence: Literal["high", "medium", "low"]
    tags: tuple[str, ...]
    checks: tuple[Check, ...]
    edge: Stat | None
    edge_by_delay: dict[int, Stat]
    copy: Stat | None
    categories: tuple[CategoryScore, ...]
    strong_categories: tuple[str, ...]
    bets: int
    decisions: int
    coverage: dict[str, int]
    pnl: float | None
    pnl_window: float | None
    volume: float | None
    biggest_win: float | None
    on_leaderboard: bool
    source: str
    window: tuple[datetime, datetime]
    recent: tuple[Bet, ...]
    skill: Skill = "unknown"
    copyable: Copyable = "unknown"
    style: Style = "takes_a_side"
    trading_pnl: float | None = None
    trading_pnl_window: float | None = None
    income_window: float | None = None
    unrealized_window: float | None = None
    volume_window: float | None = None
    return_per_dollar: float | None = None
    best_day_share: float | None = None
    periods: dict[str, dict[str, float | None]] = field(default_factory=dict)
    pnl_weeks: tuple[float | None, ...] | None = None
    pnl_daily: tuple[tuple[str, float], ...] | None = None
    pnl_since: datetime | None = None
    realized_window: float | None = None
    typical_usd: float | None = None
    avg_price: float | None = None
    maker_share: float | None = None
    last_buy: datetime | None = None
    active: bool = False
    risks: tuple[str, ...] = ()
    price_band: tuple[float, float] | None = None
    held_share: float | None = None
    positions_usd: float | None = None
    cash_usd: float | None = None
    account_at: datetime | None = None
    fills_from: datetime | None = None
    copy_entry: float | None = None
    edge_verdict: EdgeVerdict = "unmeasured"
    edge_at_fill: Stat | None = None
    spread_earned: float | None = None
    maker_bets: float | None = None
    unread: tuple[str, ...] = ()
    sample: tuple[Bet, ...] = field(default=(), repr=False)

    @property
    def label(self) -> str:
        return SEGMENTS[self.segment][0]

    def to_dict(self, *, sample: bool = True) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "name": self.name,
            "segment": self.segment,
            "label": self.label,
            "action": self.action,
            "reason": self.reason,
            "confidence": self.confidence,
            "tags": list(self.tags),
            "checks": [c.to_dict() for c in self.checks],
            "edge": self.edge.to_dict() if self.edge else None,
            "edge_by_delay": {str(k): v.to_dict() for k, v in self.edge_by_delay.items()},
            "edge_at_fill": self.edge_at_fill.to_dict() if self.edge_at_fill else None,
            "spread_earned": self.spread_earned,
            "maker_bets": self.maker_bets,
            "copy": self.copy.to_dict() if self.copy else None,
            "copy_entry": self.copy_entry,
            "categories": [c.to_dict() for c in self.categories],
            "strong_categories": list(self.strong_categories),
            "bets": self.bets,
            "decisions": self.decisions,
            "coverage": self.coverage,
            "pnl": self.pnl,
            "pnl_window": self.pnl_window,
            "volume": self.volume,
            "biggest_win": self.biggest_win,
            "on_leaderboard": self.on_leaderboard,
            "source": self.source,
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "recent": [b.to_dict() for b in self.recent],
            "skill": self.skill,
            "edge_verdict": self.edge_verdict,
            "copyable": self.copyable,
            "style": self.style,
            "trading_pnl": self.trading_pnl,
            "trading_pnl_window": self.trading_pnl_window,
            "income_window": self.income_window,
            "unrealized_window": self.unrealized_window,
            "volume_window": self.volume_window,
            "return_per_dollar": self.return_per_dollar,
            "best_day_share": self.best_day_share,
            "periods": self.periods,
            "pnl_weeks": list(self.pnl_weeks) if self.pnl_weeks is not None else None,
            "pnl_daily": [list(d) for d in self.pnl_daily] if self.pnl_daily is not None else None,
            "pnl_since": self.pnl_since.isoformat() if self.pnl_since else None,
            "realized_window": self.realized_window,
            "typical_usd": self.typical_usd,
            "avg_price": self.avg_price,
            "maker_share": self.maker_share,
            "last_buy": self.last_buy.isoformat() if self.last_buy else None,
            "active": self.active,
            "risks": list(self.risks),
            "price_band": list(self.price_band) if self.price_band is not None else None,
            "held_share": self.held_share,
            "positions_usd": self.positions_usd,
            "cash_usd": self.cash_usd,
            "account_at": self.account_at.isoformat() if self.account_at else None,
            "fills_from": self.fills_from.isoformat() if self.fills_from else None,
            "unread": list(self.unread),
            "sample": [b.to_dict() for b in self.sample] if sample else [],
        }


@dataclass(frozen=True)
class Account:
    """A Polymarket wallet's account now, from :meth:`Whales.account`.

    ``positions_usd`` is what their open positions are worth (Polymarket's figure) and ``cash_usd`` the pUSD
    in their wallet (read from the Polygon chain), each None when it couldn't be read, never 0. ``read_at``
    is when they were read. ``total_usd`` is their sum only when both were read (``complete``): one part
    alone is never the whole account. ``missing`` names the parts that couldn't be read.
    """

    wallet: str
    positions_usd: float | None
    cash_usd: float | None
    read_at: datetime

    @property
    def complete(self) -> bool:
        return self.positions_usd is not None and self.cash_usd is not None

    @property
    def total_usd(self) -> float | None:
        if self.positions_usd is None or self.cash_usd is None:
            return None
        return round(self.positions_usd + self.cash_usd, 2)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(k for k, v in (("positions", self.positions_usd), ("cash", self.cash_usd)) if v is None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "positions_usd": self.positions_usd,
            "cash_usd": self.cash_usd,
            "total_usd": self.total_usd,
            "complete": self.complete,
            "missing": list(self.missing),
            "read_at": self.read_at.isoformat(),
        }


@dataclass(frozen=True)
class Discovery:
    """The result of :meth:`Whales.discover`: every wallet scored, in segment order, then by the strength of
    their edge (``edge.z``) within each segment: never by whether copies gain.

    ``coverage`` totals how many bets were checked (``bets``) and how many had a price a minute and an
    hour later (``priced_1m``, ``priced_5m``, ``priced_1h``); ``combos`` are parlay bets left out (no market listing). ``sources`` counts wallets by where they were found.
    """

    scores: tuple[Score, ...]
    counts: dict[str, int]
    coverage: dict[str, int]
    sources: dict[str, int]
    failed: int
    started: datetime
    finished: datetime

    def to_dict(self, *, sample: bool = False) -> dict[str, Any]:
        return {
            "scores": [s.to_dict(sample=sample) for s in self.scores],
            "counts": self.counts,
            "coverage": self.coverage,
            "sources": self.sources,
            "failed": self.failed,
            "started": self.started.isoformat(),
            "finished": self.finished.isoformat(),
        }


# ---- reading prices and markets once ----

# What one scorer keeps in memory, so a long-lived one (a service scoring all day) stays bounded: the most recently
# used markets and price ranges, the least recently used dropped first. Measured 2026-10-16 (uselayer 0.12.0, 40
# leaderboard wallets): ~12 KB a Gamma market and ~2 KB a price range, ~0.9 MB a score kept without a bound. So at
# most about 120 MB of markets and 40 MB of prices; what's dropped is read again when needed (prices from cache_dir).
MARKETS_KEPT = 10_000
PRICE_RANGES_KEPT = 20_000


class PriceTape:
    """Price histories (5-minute steps), fetched once per token and range, shared by every wallet scored.

    With ``cache_dir``, ranges that ended over two hours ago are also kept on disk: the past doesn't
    change, so a rerun doesn't read them again. In memory, the ``keep`` most recently used ranges.
    """

    def __init__(
        self, poly: PolymarketData, cache_dir: str | Path | None = None, keep: int = PRICE_RANGES_KEPT
    ) -> None:
        self._poly = poly
        self._dir = Path(cache_dir) if cache_dir else None
        self._mem: OrderedDict[str, list[tuple[int, int, list[tuple[int, float]]]]] = OrderedDict()
        self._ranges = 0
        self._keep = keep
        self._lock = threading.Lock()

    def series(self, token: str, start: int, end: int) -> list[tuple[int, float]]:
        with self._lock:
            for s, e, pts in self._mem.get(token, ()):
                if s <= start and end <= e:
                    self._mem.move_to_end(token)
                    return pts
        pts = self._read(token, start, end)
        with self._lock:
            self._mem.setdefault(token, []).append((start, end, pts))
            self._mem.move_to_end(token)
            self._ranges += 1
            while self._ranges > self._keep and len(self._mem) > 1:  # the token just read always stays
                _, gone = self._mem.popitem(last=False)
                self._ranges -= len(gone)
        return pts

    def _read(self, token: str, start: int, end: int) -> list[tuple[int, float]]:
        f = None
        if self._dir is not None and end < time.time() - 7200:
            f = self._dir / f"{hashlib.sha1(f'{token}:{start}:{end}'.encode()).hexdigest()}.json"
            if f.exists():
                try:
                    return [(int(t), float(p)) for t, p in json.loads(f.read_text())]
                except (ValueError, TypeError):
                    pass
        pts = self._poly.price_history(token, start, end)
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(pts))
        return pts


class MarketBook:
    """Gamma markets by ``conditionId`` (tags, fees, settlement), read in chunks of 20 and kept: the ``keep`` most
    recently used, a market not found among them."""

    def __init__(self, poly: PolymarketData, keep: int = MARKETS_KEPT) -> None:
        self._poly = poly
        self._known: OrderedDict[str, dict[str, Any] | None] = OrderedDict()
        self._keep = keep
        self._lock = threading.Lock()

    def get(self, condition_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = sorted(set(condition_ids))
        got: dict[str, dict[str, Any]] = {}
        todo: list[str] = []
        with self._lock:
            for c in ids:
                if c not in self._known:
                    todo.append(c)
                    continue
                self._known.move_to_end(c)
                if (known := self._known[c]) is not None:
                    got[c] = known
        for i in range(0, len(todo), 20):
            chunk = todo[i : i + 20]
            found: dict[str, dict[str, Any]] = {}
            for closed in (False, True):
                try:
                    for m in self._poly.markets([c for c in chunk if c not in found], closed=closed):
                        if m.get("conditionId"):
                            found[m["conditionId"]] = m
                except VenueError as e:
                    log.warning("uselayer: couldn't read Polymarket markets: %s", e)
                if len(found) == len(chunk):
                    break
            with self._lock:
                for c in chunk:
                    self._known[c] = found.get(c)
                    self._known.move_to_end(c)
                while len(self._known) > self._keep:
                    self._known.popitem(last=False)
            got |= {c: found[c] for c in chunk if c in found}
        return {c: got[c] for c in ids if c in got}  # what this call found, even if since dropped


# ---- one wallet ----


FIREWALL_RETRY_S = 5.0  # how long a read the firewall turned away waits before its one more try
_firewall = {"retried": 0, "recovered": 0, "failed": 0}
_firewall_lock = threading.Lock()


def firewall_retries() -> dict[str, int]:
    """Reads tried once more since this process started because Polymarket's firewall turned them away
    (:func:`~uselayer.http.is_firewall_block`): ``retried`` (each one extra read), ``recovered`` (the second try
    was answered) and ``failed`` (it wasn't either: that data stays unread, and says so)."""
    with _firewall_lock:
        return dict(_firewall)


def _once_more(read: Callable[[], T], sleep: Callable[[float], None] = time.sleep) -> T:
    """``read()``, tried once more after FIREWALL_RETRY_S when Polymarket's firewall turned it away (a web page
    for a 403), and only then: any other error, or a second refusal, is raised as it is."""
    try:
        return read()
    except VenueError as e:
        if not is_firewall_block(e):
            raise
    with _firewall_lock:
        _firewall["retried"] += 1
    sleep(FIREWALL_RETRY_S)
    try:
        got = read()
    except VenueError:
        with _firewall_lock:
            _firewall["failed"] += 1
        raise
    with _firewall_lock:
        _firewall["recovered"] += 1
    return got


def closed_positions(poly: PolymarketData, wallet: str, limit: int = 50, *, strict: bool = False,
                     sleep: Callable[[float], None] = time.sleep) -> list[ClosedPosition]:  # fmt: skip
    """Their last ``limit`` closed positions, newest first, each with how they got out.

    Polymarket's closed positions give the contracts bought and the realized profit, but not how they got
    out (``curPrice`` is the market's price, not theirs), and their ``avgPrice`` is the cost of the last lot
    only, fees in. So their trades in those markets are read too (one read per 20 markets, up to 2,500
    trades each): entry and exit prices come from them, and a position counts as sold when they sold at
    least ``SOLD_SHARE`` of what they bought. A trades read Polymarket's firewall turns away is tried once more
    (``_once_more``). Raises :class:`VenueError` if the closed positions can't be read; trades that can't be
    read leave ``avg_price``, ``exit_price`` and ``result`` None, or with ``strict``, raise too, so a caller
    that sums them (:func:`held_share`) can say they're unread rather than give a figure from what's left.
    """
    rows: list[dict[str, Any]] = []
    while len(rows) < limit:
        page = poly.closed_positions(wallet, min(50, limit - len(rows)), offset=len(rows))
        rows += page
        if len(page) < 50:
            break
    rows = rows[:limit]
    cids = sorted({str(r["conditionId"]) for r in rows if r.get("conditionId")})
    # By outcome token: contracts bought, dollars paid for them, contracts sold, dollars got (at their prices).
    book: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    lots: dict[str, list[float]] | None = book
    unsure: set[str] = set()  # markets with more trades than were read
    try:
        for i in range(0, len(cids), 20):
            chunk, offset = cids[i : i + 20], 0
            while True:
                page = _once_more(partial(poly.trades_in, wallet, chunk, offset=offset), sleep)
                for t in page:
                    size, price = _f(t.get("size")) or 0.0, _f(t.get("price")) or 0.0
                    lot = book[str(t.get("asset"))]
                    k = 0 if str(t.get("side")).upper() == "BUY" else 2
                    lot[k] += size
                    lot[k + 1] += size * price
                if len(page) < 500:
                    break
                offset += 500
                if offset >= 2500:
                    unsure |= set(chunk)
                    break
    except VenueError as e:
        log.warning("uselayer: couldn't read the trades of %s: %s", wallet, e)
        if strict:
            raise
        lots = None
    out = []
    for r in rows:
        size, pnl, now = _f(r.get("totalBought")) or 0.0, _f(r.get("realizedPnl")), _f(r.get("curPrice"))
        payout = now if now in (0.0, 1.0) else None
        avg = exit_price = None
        result: Literal["won", "lost", "sold"] | None = None
        if lots is not None and r.get("conditionId") not in unsure:
            bought, paid, sold, got = lots.get(str(r.get("asset")), [0.0, 0.0, 0.0, 0.0])
            held = max(0.0, bought - sold)
            if bought > 0:
                avg = round(paid / bought, 4)
                if held <= 0.01 * bought:
                    exit_price = round(got / sold, 4)
                elif payout is not None:  # what they still held paid 1 or 0
                    exit_price = round((got + held * payout) / (sold + held), 4)
            if bought or size:
                if sold >= SOLD_SHARE * (bought or size):
                    result = "sold"
                elif payout is not None:
                    result = "won" if payout == 1.0 else "lost"
        ts = r.get("timestamp")
        out.append(
            ClosedPosition(
                market=f"{r.get('conditionId')}:{int(r.get('outcomeIndex') or 0)}",
                title=r.get("title"),
                outcome=r.get("outcome"),
                url=polymarket_url(r.get("eventSlug"), r.get("slug")),
                size=round(size, 4),
                avg_price=avg,
                exit_price=exit_price,
                pnl=None if pnl is None else round(pnl, 2),
                result=result,
                settled=None if result is None else result != "sold",
                closed_at=datetime.fromtimestamp(int(ts), UTC) if ts else None,
            )
        )
    return out


def held_share(closed: list[ClosedPosition]) -> float | None:
    """Share of closed positions with a known exit that they held until the market settled."""
    known = [c for c in closed if c.settled is not None]
    return round(sum(1 for c in known if c.settled) / len(known), 3) if len(known) >= HELD_MIN else None


def price_band(prices: list[float]) -> tuple[float, float] | None:
    """The middle half of the prices paid: ``(25th, 75th percentile)``."""
    if len(prices) < BAND_MIN:
        return None
    q = statistics.quantiles(prices, n=4, method="inclusive")
    return round(q[0], 4), round(q[2], 4)


@dataclass
class _Decision:
    condition: str
    outcome: int
    token: str
    event: str
    title: str | None
    outcome_name: str | None
    at: int
    price: float
    size: float
    usd: float


def _decisions(fills: list[dict[str, Any]]) -> list[_Decision]:
    """Their buys, oldest first: fills on the same outcome within a minute are one decision, and only
    the first decision on each outcome counts (adding to a bet 20 times isn't 20 calls)."""
    out: list[_Decision] = []
    first: dict[tuple[str, int], _Decision] = {}
    for f in sorted(fills, key=lambda r: int(r.get("timestamp") or 0)):
        if str(f.get("side")).upper() != "BUY" or not f.get("price") or not f.get("conditionId"):
            continue
        key = (str(f["conditionId"]), int(f.get("outcomeIndex") or 0))
        size, price, at = float(f.get("size") or 0), float(f["price"]), int(f["timestamp"])
        d = first.get(key)
        if d is not None:
            if at - d.at <= MERGE_S:
                total = d.size + size
                if total > 0:
                    d.price = (d.price * d.size + price * size) / total
                d.size = total
                d.usd += float(f.get("usdcSize") or price * size)
            continue
        d = _Decision(
            condition=key[0],
            outcome=key[1],
            token=str(f.get("asset") or ""),
            event=str(f.get("eventSlug") or f.get("slug") or key[0]),
            title=f.get("title"),
            outcome_name=f.get("outcome"),
            at=at,
            price=price,
            size=size,
            usd=float(f.get("usdcSize") or price * size),
        )
        first[key] = d
        out.append(d)
    return out


def _point(wallet: str, k: int, lo: int, hi: int) -> int:
    """Where in slice ``k`` (``lo`` to ``hi``) a capped trader's page of trades ends: a fraction of the slice from
    their wallet and ``k``, so pages land at different points in different slices, and the same ones each time."""
    frac = int.from_bytes(hashlib.sha256(f"{wallet}:{k}".encode()).digest()[:8], "big") / 2**64
    return lo + int(frac * (hi - lo))


def _sample_key(d: _Decision) -> bytes:
    return hashlib.sha256(f"{d.condition}:{d.outcome}".encode()).digest()


def sample_decisions(
    decisions: list[_Decision], n: int, strata: list[tuple[int, int]] | None = None
) -> list[_Decision]:
    """The decisions a score checks: up to ``n`` of them, the ones whose market and outcome hash lowest, oldest
    first. As good as a random draw over the window, and stable: a decision is in the sample while it's in the
    window and among the ``n`` lowest, so a window that moves (a newer score, a busy trader's latest trades)
    changes the sample only by what enters or leaves it, never all of it at once. (Every ``k``-th decision from
    the window's oldest, before 0.13, picked a different set whenever one decision at the start dropped out.)
    The time isn't in the key: a decision whose first buy leaves the window, leaving a later buy as the first on
    that outcome, keeps its place. ``n`` of 0 checks every decision.

    ``strata`` (``[start, end)`` times covering the window) is for a trader whose trades weren't all read: each
    stratum gets a share of the ``n`` by how much of the window it covers, not by how many of their decisions
    happened to be read there, so the part read in full (their latest trades) doesn't crowd out the rest. A
    stratum with fewer decisions than its share gives the rest to the others, again by time."""
    if not n or len(decisions) <= n:
        return list(decisions)
    if not strata:
        return sorted(sorted(decisions, key=_sample_key)[:n], key=lambda d: d.at)
    groups: list[list[_Decision]] = [[] for _ in strata]
    for d in decisions:
        i = next((i for i, (a, b) in enumerate(strata) if a <= d.at < b), len(strata) - 1)
        groups[i].append(d)
    groups = [sorted(g, key=_sample_key) for g in groups]
    share = [0] * len(strata)
    open_ = [i for i in range(len(strata)) if groups[i]]
    left = n
    while left > 0 and open_:  # by time; a stratum with too few gives the rest to the others, again by time
        total = sum(strata[i][1] - strata[i][0] for i in open_) or 1
        exact = {i: left * (strata[i][1] - strata[i][0]) / total for i in open_}
        add = {i: int(x) for i, x in exact.items()}
        for i in sorted(open_, key=lambda i: exact[i] - add[i], reverse=True)[: left - sum(add.values())]:
            add[i] += 1  # largest remainders: this round's shares add up to what's left
        for i in open_:
            take = min(add[i], len(groups[i]) - share[i])
            share[i] += take
            left -= take
        open_ = [i for i in open_ if share[i] < len(groups[i])]
    picked = [d for g, k in zip(groups, share, strict=True) for d in g[:k]]
    return sorted(picked, key=lambda d: d.at)


def _stat(values_by_event: dict[str, list[float]]) -> Stat | None:
    """Mean over events (each event's bets averaged first), with its 95% range (Student's t)."""
    xs = [statistics.mean(v) for v in values_by_event.values() if v]
    if len(xs) < 2:
        return None
    m = statistics.mean(xs)
    se = statistics.stdev(xs) / math.sqrt(len(xs))
    return Stat(
        mean=round(m, 5), margin=round(t95(len(xs) - 1) * se, 5), n=len(xs), z=round(m / se, 2) if se else 0.0
    )


def _event_mean(values_by_event: dict[str, list[float]]) -> float | None:
    """Mean over events, each event's values averaged first: the weighting :func:`_stat` uses."""
    xs = [statistics.mean(v) for v in values_by_event.values() if v]
    return round(statistics.mean(xs), 5) if xs else None


def _by_event(bets: Iterable[Bet], value: Callable[[Bet], float | None]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for b in bets:
        v = value(b)
        if v is not None:
            out[b.event].append(v)
    return out


def _two_way_share(fills: list[dict[str, Any]]) -> float:
    """Share of their traded contracts that sit in a market-hour where they traded both ways (bought one
    outcome and sold it, or bought both outcomes), with the smaller way at least a quarter."""
    hours: dict[tuple[str, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for f in fills:
        size = float(f.get("size") or 0)
        if not size or not f.get("conditionId"):
            continue
        long_first = (int(f.get("outcomeIndex") or 0) == 0) == (str(f.get("side")).upper() == "BUY")
        hours[(str(f["conditionId"]), int(f.get("timestamp") or 0) // 3600)][0 if long_first else 1] += size
    total = sum(a + b for a, b in hours.values())
    if not total:
        return 0.0
    two_way = sum(a + b for a, b in hours.values() if min(a, b) >= 0.25 * (a + b))
    return two_way / total


def _complete_set_share(fills: list[dict[str, Any]]) -> float:
    """Share of their markets where they bought both outcomes within a minute for $1 or less together."""
    buys: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    for f in fills:
        if str(f.get("side")).upper() == "BUY" and f.get("conditionId"):
            buys[str(f["conditionId"])].append(
                (int(f.get("timestamp") or 0), int(f.get("outcomeIndex") or 0), float(f.get("price") or 0))
            )
    if not buys:
        return 0.0
    hits = 0
    for rows in buys.values():
        a = [r for r in rows if r[1] == 0]
        b = [r for r in rows if r[1] == 1]
        if any(abs(x[0] - y[0]) <= 60 and x[2] + y[2] <= 1.0 for x in a for y in b):
            hits += 1
    return hits / len(buys)


_AFTER = {60: "1 min", 300: "5 min", 900: "15 min", 3600: "1 hour"}


def _money(x: float) -> str:
    a = abs(x)
    s = f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a / 1e3:.0f}K" if a >= 1e3 else f"${a:.0f}"
    return ("−" if x < 0 else "") + s


def _cents(x: float, signed: bool = True) -> str:
    c = x * 100
    if signed:
        return f"{'+' if c >= 0 else '−'}{abs(c):.1f}¢"
    return f"{c:.1f}¢"


def _price_at(pts: list[tuple[int, float]], at: int, tolerance_s: int) -> float | None:
    """The first price at or after ``at``, if there's one within ``tolerance_s``."""
    for t, p in pts:
        if t >= at:
            return p if t - at <= tolerance_s else None
    return None


def _mid_at(pts: list[tuple[int, float]], at: int) -> float | None:
    """The middle of the book when they bought: the last price up to 2 minutes before, else the first within the
    next 5-minute step."""
    before = [p for t, p in pts if at - 120 <= t <= at]
    return before[-1] if before else _price_at(pts, at, 330)


def _resting_note(spread_earned: float | None, maker_bets: float | None, coverage: dict[str, int]) -> str:
    """The skill test's word on resting orders: they count from the middle of the book, and what the spread got
    them; and how many buys weren't checked because that couldn't be told."""
    out = ""
    if maker_bets is not None and spread_earned is not None and maker_bets >= 0.005:
        out = (
            f" Resting buys count from the middle of the book, takers from what they paid: {maker_bets:.0%} of these"
            f" buys rested on the book and got {_cents(spread_earned, False)} a bet from the spread before the price"
            " moved. That's how they buy, kept apart from this test."
        )
    unknown = coverage.get("priced_5m", 0) - coverage.get("resting_known", 0)
    if unknown > 0:
        out += (
            f" {unknown} weren't checked: we couldn't tell whether they rested on the book, so they don't count"
            " for or against them."
        )
    return out


def _union(parts: list[tuple[set[str], list[dict[str, Any]]]]) -> tuple[set[str], list[dict[str, Any]]]:
    """Reads of the taker feed, together: the markets read and their rows."""
    known: set[str] = set()
    rows: list[dict[str, Any]] = []
    for k, r in parts:
        known |= k
        rows += r
    return known, rows


def _fill_key(f: dict[str, Any]) -> tuple[str, str, float]:
    """One fill, the same in Polymarket's activity and trade feeds."""
    return str(f.get("transactionHash")), str(f.get("asset")), round(float(f.get("size") or 0), 4)


# ---- profit history ----


def _economic(p: dict[str, Any]) -> float | None:
    return _f(p.get("economic_pnl"))


def _trading(p: dict[str, Any]) -> float | None:
    """Profit from trading at one point: realized plus unrealized, after fees. Polymarket's ``economic_pnl``
    is this plus ``wallet_income`` (rewards, maker rebates, yield)."""
    r, u = _f(p.get("realized_pnl")), _f(p.get("unrealized_pnl"))
    return None if r is None or u is None else r + u


def _income(p: dict[str, Any]) -> float | None:
    return _f(p.get("wallet_income"))


def _unrealized(p: dict[str, Any]) -> float | None:
    return _f(p.get("unrealized_pnl"))


def _realized(p: dict[str, Any]) -> float | None:
    return _f(p.get("realized_pnl"))


def _traded(p: dict[str, Any]) -> float | None:
    return _f(p.get("volume_usdc"))  # dollars bought and sold; ``volume`` counts contracts


class _Curve:
    """Polymarket's profit history (hourly points, running totals), read at any time. The history starts at
    the wallet's first trade, so every total is 0 before it: the account had nothing yet."""

    def __init__(self, points: list[dict[str, Any]]) -> None:
        self.points = sorted(points, key=lambda p: int(p.get("timestamp") or 0))
        self.times = [int(p.get("timestamp") or 0) for p in self.points]

    def at(self, t: int, value: Callable[[dict[str, Any]], float | None]) -> float | None:
        if not self.points:
            return None
        i = bisect.bisect_right(self.times, t)
        return 0.0 if i == 0 else value(self.points[i - 1])

    def change(self, a: int, b: int, value: Callable[[dict[str, Any]], float | None]) -> float | None:
        x, y = self.at(a, value), self.at(b, value)
        return None if x is None or y is None else round(y - x, 2)

    @property
    def since(self) -> int | None:
        """When the history starts: their first trade's hour."""
        return self.times[0] if self.points else None

    def period(self, a: int, b: int) -> dict[str, float | None]:
        """Trading profit between ``a`` and ``b`` (``realized`` plus the change in ``unrealized``), income and
        dollars traded, and the profit per dollar."""
        pnl, traded = self.change(a, b, _trading), self.change(a, b, _traded)
        return {
            "trading_pnl": pnl,
            "realized": self.change(a, b, _realized),
            "unrealized": self.change(a, b, _unrealized),
            "income": self.change(a, b, _income),
            "volume": traded,
            "return_per_dollar": round(pnl / traded, 5) if pnl is not None and traded else None,
        }

    def steps(self, a: int, b: int, every: int) -> list[float] | None:
        """Trading profit made in each ``every`` seconds from ``a`` to ``b``."""
        marks: list[float] = []
        for t in range(a, b + 1, every):
            m = self.at(t, _trading)
            if m is None:
                return None
            marks.append(m)
        return [round(y - x, 2) for x, y in itertools.pairwise(marks)] if len(marks) > 1 else None

    def known_steps(self, a: int, b: int, every: int) -> list[float | None] | None:
        """:meth:`steps`, with None for each step that ended before the history starts: no history isn't a
        step with no profit, and a week before their first trade isn't a losing (or a flat) week."""
        steps = self.steps(a, b, every)
        if steps is None or self.since is None:
            return None if steps is None else list(steps)
        return [None if a + (i + 1) * every < self.since else x for i, x in enumerate(steps)]


def _verdicts(
    *,
    style: Style,
    edge: Stat | None,
    copy: Stat | None,
    steady: bool,
    n_events: int,
    profitable: bool | None,
) -> tuple[Skill, Copyable]:
    """Is the trader skilled, and is copying them a few minutes late worth it? Two separate answers."""
    copyable = copyable_verdict(copy)
    if style != "takes_a_side" or edge is None or n_events < RISING_BETS:
        # Too little to tell, or a market maker's or arbitrageur's buys, which aren't views: this test doesn't fit.
        return "unknown", copyable
    if not (edge.mean > 0 and edge.z >= 1):
        return "no", copyable
    if profitable is None:
        return "unknown", copyable  # signs of an edge, but their profit couldn't be read
    if not profitable or not steady:
        return "no", copyable  # losing from trading, or an edge that rests on a few lucky bets
    if edge.z >= 2 and steady and n_events >= QUIET_BETS:
        return "yes", copyable
    return "early", copyable


def _edge_verdict(*, style: Style, edge: Stat | None, trimmed: Stat | None, n_events: int) -> EdgeVerdict:
    """The price test alone: does the price move their way after they buy? Profit is never an input, so a
    trader who lost money over the window keeps the edge they showed; ``skill`` adds the track record."""
    if style != "takes_a_side" or edge is None or n_events < RISING_BETS:
        return "unmeasured"
    if edge.mean > 0 and edge.z >= 2 and n_events >= QUIET_BETS and trimmed is not None and trimmed.mean > 0:
        return "measured"
    if edge.mean > 0 and edge.z >= 1:
        return "early"
    return "not_shown"


def _mostly_parlays(combos: int, decisions: int) -> bool:
    """Most of their buys are parlays, which have no price history: why there's too little to check."""
    return combos > 0 and 2 * combos >= decisions


def _segment(
    *,
    style: Style,
    skill: Skill,
    n_events: int,
    big: bool,
    trading_pnl: float | None,
    profit_wo_best: float | None,
    beats_and_steady: bool,
    active: bool,
    edge_verdict: EdgeVerdict = "unmeasured",
    profitable: bool | None = None,
    parlays: bool = False,
) -> Segment:
    """The tier, from skill and track record. Whether copies gain (``copyable``) is never an input."""
    segment: Segment
    if style != "takes_a_side":
        return "no_view"
    if skill == "yes" and n_events >= (PROVEN_BETS if big else QUIET_BETS):
        segment = "proven" if big else "quiet"
    elif (trading_pnl or 0) >= BIG_PROFIT and profit_wo_best is not None and profit_wo_best <= 0:
        segment = "lucky"  # big profit, and without their biggest win they'd be down: evidence of luck
    elif (trading_pnl or 0) >= BIG_PROFIT and n_events >= RISING_BETS and not beats_and_steady:
        # big profit, but the price test doesn't show skill: unproven, and not evidence of luck either
        segment = "big_profit"
    elif skill in ("yes", "early"):
        segment = "rising"
    elif skill == "unknown" and parlays and n_events < RISING_BETS:
        segment = "parlays"  # their trading is parlays, which can't be priced: skill unmeasured, not absent
    elif skill == "unknown":
        segment = "too_few"  # too few bets checked, or their profit couldn't be read: no verdict either way
    elif edge_verdict == "measured" and profitable is False:
        segment = "edge_losing"  # the price test shows an edge; the losing window keeps them from sharp
    else:
        segment = "no_edge"
    return "inactive" if segment in RECOMMENDED and not active else segment


def segment_of(score: dict[str, Any]) -> Segment:
    """This version's tier for a score kept as :meth:`Score.to_dict` (uselayer 0.8.0 or later), from what it
    kept: ``skill``, ``edge``, the ``steady`` check, profit, biggest win, account size and ``active``. A
    score kept before 0.10.0 may say ``too_fast`` (a sharp whose copies don't pay) or ``lucky`` for big
    profit without a clear edge, and one kept before 0.11.0 may say ``no_edge`` for a measured edge with a
    losing window or ``too_few`` for a parlay trader: this says what it is now. ``copyable`` is never read. One
    without ``edge_at_fill`` predates the resting-order correction (0.13): its ``edge`` counts every buy from what
    they paid, half spread included, and this can't correct it; score the wallet again for a tier that can."""
    edge = Stat.from_dict(score["edge"]) if score.get("edge") else None
    n_events = edge.n if edge else 0
    trading_pnl, biggest = _f(score.get("trading_pnl")), _f(score.get("biggest_win"))
    profit_wo_best = None if trading_pnl is None or biggest is None else trading_pnl - biggest
    checks = {c.get("rule"): c.get("passed") for c in score.get("checks") or []}
    steady = checks.get("steady") is True
    big = (
        bool(score.get("on_leaderboard"))
        or (_f(score.get("volume")) or 0) >= BIG_VOLUME
        or (trading_pnl or 0) >= 2 * BIG_PROFIT
    )
    style: Style = score.get("style") or "takes_a_side"
    verdict: EdgeVerdict | None = score.get("edge_verdict")
    if verdict is None:  # kept before 0.11.0: the steady check stands in for the edge without the best two
        beats = edge is not None and edge.mean > 0 and edge.z >= 2
        verdict = (
            "unmeasured" if style != "takes_a_side" or edge is None or n_events < RISING_BETS
            else "measured" if beats and n_events >= QUIET_BETS and steady
            else "early" if edge.mean > 0 and edge.z >= 1
            else "not_shown"
        )  # fmt: skip
    coverage = score.get("coverage") or {}
    combos = int(coverage.get("combos") or 0)
    # Before 0.11.0 combos were counted among the bets picked, so the bets checked are the rest of the pick.
    decisions = (
        int(score.get("decisions") or 0)
        if "edge_verdict" in score
        else combos + int(coverage.get("bets") or 0)
    )
    return _segment(
        style=style, skill=score.get("skill") or "unknown", n_events=n_events,
        big=big, trading_pnl=trading_pnl, profit_wo_best=profit_wo_best,
        beats_and_steady=edge is not None and edge.mean > 0 and edge.z >= 2 and steady,
        active=bool(score.get("active")), edge_verdict=verdict, profitable=checks.get("profitable"),
        parlays=_mostly_parlays(combos, decisions),
    )  # fmt: skip


def _for(seconds: float) -> str:
    """``3 hours``, ``2 days``."""
    h = seconds / 3600
    if h < 24:
        n = max(1, round(h))
        return f"{n} hour{'s' if n != 1 else ''}"
    n = round(h / 24)
    return f"{n} day{'s' if n != 1 else ''}"


class Scorer:
    """Scores Polymarket wallets. Use :meth:`Whales.score` and :meth:`Whales.discover`."""

    def __init__(
        self, poly: PolymarketData, *, cache_dir: str | Path | None = None, chain: Polygon | None = None
    ) -> None:
        self._poly = poly
        self._chain = chain
        self.tape = PriceTape(poly, cache_dir)
        self.markets = MarketBook(poly)

    # -- reads --

    def _fills(self, wallet: str, start: int, end: int, max_fills: int) -> list[dict[str, Any]]:
        """Their trades between ``start`` and ``end``, newest first, at most ``max_fills``. Paged by time
        once the 5,000 offset cap is near."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        upper, offset = end, 0
        while len(out) < max_fills:
            rows = self._poly.activity(wallet, start=start, end=upper, offset=offset)
            for r in rows:
                key = f"{r.get('transactionHash')}:{r.get('asset')}:{r.get('size')}:{r.get('timestamp')}"
                if key not in seen:
                    seen.add(key)
                    out.append(r)
            if len(rows) < 500:
                break
            offset += 500
            if offset >= 4500:
                last = int(rows[-1]["timestamp"])
                if last >= upper:  # a whole page in one second: stop rather than loop
                    break
                upper, offset = last, 0
        return out[:max_fills]

    @staticmethod
    def _slices(start: int, oldest: int) -> list[tuple[int, int]]:
        """SPREAD_SLICES equal ``[lo, hi)`` slices from the window's ``start`` to ``oldest``, where the capped read
        stopped."""
        span = (oldest - start) / SPREAD_SLICES
        return [(int(start + k * span), int(start + (k + 1) * span)) for k in range(SPREAD_SLICES)]

    def _spread(self, wallet: str, slices: list[tuple[int, int]]) -> list[dict[str, Any]]:
        """For a trader the ``max_fills`` cap cut short: one page of their trades (500) from each slice, the page
        ending at a point inside it that their wallet and the slice decide (``_point``): somewhere different in
        each slice, the same each time they're scored. (The latest 500 of each slice, in 0.13's first version, put
        every page at a slice's end.) Their bets then come from the whole window. A slice that can't be read is
        left out: the score stands on what was read."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        oldest = slices[-1][1] if slices else 0
        for k, (lo, hi) in enumerate(slices):
            hi = min(hi, oldest) - 1
            if hi < lo:
                continue
            at = _point(wallet, k, lo, hi)
            try:
                rows = self._poly.activity(wallet, start=lo, end=at)
            except VenueError as e:
                log.warning("uselayer: couldn't read %s's trades from %d to %d: %s", wallet, lo, hi, e)
                continue
            for r in rows:
                key = f"{r.get('transactionHash')}:{r.get('asset')}:{r.get('size')}:{r.get('timestamp')}"
                if key not in seen and int(r.get("timestamp") or 0) < oldest:
                    seen.add(key)
                    out.append(r)
        return out

    def _taker_fills(self, wallet: str) -> list[dict[str, Any]] | None:
        """Their latest ``TAKER_LIMIT`` fills that took liquidity, newest first (None if they couldn't be read):
        for ``maker_share``, and for which checked buys rested when it reaches back far enough (``_resting``)."""
        try:
            return self._poly.taker_trades(wallet, TAKER_LIMIT)
        except VenueError:
            return None

    @staticmethod
    def _maker_share(fills: list[dict[str, Any]], taker: list[dict[str, Any]] | None) -> float | None:
        """Share of their recent fills that were resting orders, from the taker-only feed."""
        if len(fills) < 20 or taker is None:
            return None
        recent = fills[: min(len(fills), 500)]
        since = int(recent[-1].get("timestamp") or 0)
        took = sum(1 for t in taker if int(t.get("timestamp") or 0) >= since)
        return max(0.0, 1 - took / len(recent))

    def _held_share(self, wallet: str) -> float | None:
        # strict: trades that can't be read (even after the firewall's one more try) make the figure unread, never
        # a share worked out from the positions whose trades happened to be read
        return held_share(closed_positions(self._poly, wallet, strict=True))

    def _positions_usd(self, wallet: str) -> float | None:
        v = self._poly.positions_value(wallet)
        return None if v is None else round(v, 2)

    def _cash_usd(self, wallet: str) -> float | None:
        return None if self._chain is None else round(self._chain.cash(wallet), 2)

    def account(self, wallet: str, *, wait_s: float = NOW_WAIT_S) -> Account:
        """Their open positions' worth and their cash, read now in parallel, waiting at most ``wait_s``."""
        pool = ThreadPoolExecutor(max_workers=2)
        reads = {
            "positions": pool.submit(self._positions_usd, wallet),
            "cash": pool.submit(self._cash_usd, wallet),
        }
        pool.shutdown(wait=False)
        deadline = time.monotonic() + wait_s
        positions, cash = (_by(reads[k], deadline, wallet, k) for k in ("positions", "cash"))
        return Account(wallet=wallet, positions_usd=positions, cash_usd=cash, read_at=datetime.now(UTC))

    # -- the score --

    def score(
        self,
        wallet: str,
        *,
        days: float = 30,
        end: datetime | None = None,
        sample: int = 60,
        max_fills: int = 5000,
        on_leaderboard: bool = False,
        source: str = "asked",
        name: str | None = None,
    ) -> Score:
        end_ts = int((end or datetime.now(UTC)).timestamp())
        start_ts = end_ts - int(days * 86400)
        live = end is None
        # How they exit, their positions' value and their cash describe now: read alongside the rest, live only.
        now_reads = ThreadPoolExecutor(max_workers=3)
        now: dict[str, Future[float | None]] = (
            {k: now_reads.submit(f, wallet) for k, f in
             (("held", self._held_share), ("positions", self._positions_usd), ("cash", self._cash_usd))}
            if live else {}
        )  # fmt: skip
        now_reads.shutdown(wait=False)
        fills = self._fills(wallet, start_ts, end_ts, max_fills)
        # Cut short by the cap (a busy trader): their bets are read from the rest of the window too, so the ones
        # checked span all of it. The rest of the score (style, market making, the bot tag) uses ``fills`` as read.
        capped_at = int(fills[-1]["timestamp"]) if fills and len(fills) >= max_fills else None
        slices = self._slices(start_ts, capped_at) if capped_at is not None and capped_at > start_ts else []
        spread = self._spread(wallet, slices) if slices else []
        # The bets checked are spread over the window by time: the slices, then what was read in full.
        strata = [*slices, (capped_at, end_ts + 1)] if slices and capped_at is not None else None
        stats: dict[str, Any] = {}
        pnl_points: list[dict[str, Any]] = []
        try:
            stats = self._poly.stats(wallet)
            pnl_points = self._poly.pnl_history(wallet)
        except VenueError as e:
            log.warning("uselayer: couldn't read stats for %s: %s", wallet, e)
        raw_all_time = stats.get("all_time_pnl")
        all_time: dict[str, Any] = raw_all_time if isinstance(raw_all_time, dict) else {}
        curve = _Curve(pnl_points)
        # All-time figures: the stats are fresher than the hourly history, so they're used when live.
        pnl = _first(_economic(all_time) if live else None, curve.at(end_ts, _economic))
        trading_pnl = _first(_trading(all_time) if live else None, curve.at(end_ts, _trading))
        volume = _first(_traded(all_time) if live else None, curve.at(end_ts, _traded))
        biggest = _f(stats.get("biggest_win")) if live else None  # all-time: unknown for a past window
        # Over the window, from the history (a newer account's starts inside it, from 0).
        before = curve.at(start_ts, _economic)
        pnl_window = None if pnl is None or before is None else round(pnl - before, 2)
        window = curve.period(start_ts, end_ts)
        trading_window, income_window = window["trading_pnl"], window["income"]
        unrealized_window = curve.change(start_ts, end_ts, _unrealized)
        realized_window = window["realized"]
        days_steps = curve.steps(start_ts, end_ts, 86400)
        best_day_share = (
            round(max(days_steps) / trading_window, 3)
            if days_steps and trading_window is not None and trading_window > 0
            else None
        )
        periods = (
            {f"{d}d": curve.period(end_ts - d * 86400, end_ts) for d in (7, 30, 90)} if pnl_points else {}
        )
        weeks = curve.known_steps(end_ts - 12 * 7 * 86400, end_ts, 7 * 86400)
        since = curve.at(end_ts - 90 * 86400, _trading)
        days_pnl = [
            (datetime.fromtimestamp(t, UTC).date().isoformat(), curve.at(t, _trading))
            for t in range(end_ts - 90 * 86400, end_ts + 1, 86400)
        ]
        pnl_daily = (
            tuple((d, round(v - since, 2)) for d, v in days_pnl if v is not None)
            if since is not None and all(v is not None for _, v in days_pnl)
            else None
        )

        who = name
        if not who:
            for f in fills:
                n = f.get("name") or f.get("pseudonym") or ""
                if n and not str(n).startswith("0x"):
                    who = str(n)
                    break
        who = who or f"{wallet[:6]}…{wallet[-4:]}"

        # Rule 5 first: market makers and arbitrage have no view to score.
        two_way = _two_way_share(fills)
        complete = _complete_set_share(fills)
        taker = self._taker_fills(wallet) if live and len(fills) >= 20 else None
        maker = (
            self._maker_share(fills, taker) if live else None
        )  # context for all; the market-maker test too
        tags: list[str] = []
        span_days = max(1.0, days)
        if len(fills) / span_days >= 100 or len(fills) >= max_fills:
            tags.append("bot")
        market_maker = two_way >= MAKER_TWO_WAY and (maker is None or maker >= 0.6)
        arbitrage = complete >= ARB_SHARE
        if market_maker:
            tags.append("market maker")
        if arbitrage:
            tags.append("arbitrage")

        style: Style = "arbitrage" if arbitrage else "market_maker" if market_maker else "takes_a_side"

        decisions = _decisions(fills + spread)
        # Context: how they trade. Over every buy in the window, not just the ones checked.
        typical_usd = round(statistics.median(d.usd for d in decisions), 2) if decisions else None
        avg_price = round(statistics.mean(d.price for d in decisions), 4) if decisions else None
        last_at = max((d.at for d in decisions), default=None)
        last_buy = datetime.fromtimestamp(last_at, UTC) if last_at is not None else None
        active = last_at is not None and end_ts - last_at <= ACTIVE_DAYS * 86400
        # Parlays (combo bets like "X AND Y") have no listed market and no price history, so they're left out
        # before the sample is picked: a wallet that mixes them in still gets its single-market bets checked.
        parlay = {
            str(f.get("conditionId")) for f in fills + spread if str(f.get("isCombo")).lower() == "true"
        }
        parlay_buys = sum(1 for d in decisions if d.condition in parlay)
        usable = [d for d in decisions if 0.02 <= d.price <= 0.98 and d.token and d.condition not in parlay]
        picked = sample_decisions(usable, sample, strata)
        info = self.markets.get(d.condition for d in picked)
        # A combo without the flag shows up here instead, as a bet with no listed market: left out too.
        combos = parlay_buys + sum(1 for d in picked if d.condition not in info)
        bets = self._check([d for d in picked if d.condition in info], info, end_ts)
        band = price_band([b.price for b in bets])
        deadline = time.monotonic() + NOW_WAIT_S
        failed: list[str] = []  # the reads that failed or ran out of time: these figures are unread, not none
        held, positions_usd, cash_usd = (
            _by(now.get(k), deadline, wallet, k, failed) for k in ("held", "positions", "cash")
        )
        unread = tuple(UNREAD_FIELDS[k] for k in failed)
        account_at = datetime.now(UTC) if positions_usd is not None or cash_usd is not None else None
        latest = sorted(decisions, key=lambda d: d.at)[-5:][::-1]
        recent = tuple(
            self._check(latest, self.markets.get(d.condition for d in latest), end_ts, prices=False)
        )

        # Rule 1 counts from the middle of the book: the half spread a resting buy got is how they buy, kept apart.
        bets = self._resting(wallet, bets, fills + spread, taker)
        edge = _stat(_by_event(bets, lambda b: b.skill()))
        edge_at_fill = _stat(_by_event(bets, lambda b: b.moved.get(SKILL_S)))
        spread_earned = _event_mean(
            _by_event(bets, lambda b: b.spread_earned if SKILL_S in b.moved else None)
        )
        maker_bets = _event_mean(_by_event(bets, lambda b: b.maker if SKILL_S in b.moved else None))
        by_delay: dict[int, Stat] = {}
        for h in HORIZONS:
            st = _stat(_by_event(bets, lambda b, h=h: b.skill(h)))  # type: ignore[misc]
            if st is not None:
                by_delay[h] = st
        copy = _stat(_by_event(bets, lambda b: b.copy))
        copy_entry = _event_mean(_by_event(bets, lambda b: b.entry if b.copy is not None else None))
        n_events = len({b.event for b in bets if b.skill() is not None})
        coverage = {
            "bets": len(bets),
            "priced_1m": sum(1 for b in bets if 60 in b.moved),
            "priced_5m": sum(1 for b in bets if SKILL_S in b.moved),
            "priced_1h": sum(1 for b in bets if 3600 in b.moved),
            "combos": combos,
            "spread": len(spread),  # trades read from the rest of the window after the cap (``_spread``)
            # buys with a 5-minute price whose resting share could be told: only these are in the skill test
            "resting_known": sum(1 for b in bets if b.skill() is not None),
        }

        # Rule 3: steady. The edge without their two best bets, and profit without the biggest win.
        best_two = sorted((b for b in bets if b.skill() is not None), key=lambda b: b.skill() or 0.0)[-2:]
        trimmed = _stat(_by_event([b for b in bets if b not in best_two], lambda b: b.skill()))
        profit_wo_best = None if trading_pnl is None or biggest is None else trading_pnl - biggest

        cats = self._categories(bets)
        strong = tuple(c.category for c in cats if c.strong)

        beats = edge is not None and edge.mean > 0 and edge.z >= 2
        steady_edge = trimmed is not None and trimmed.mean > 0
        steady = steady_edge and (profit_wo_best is None or profit_wo_best > 0)
        profitable = None if trading_window is None else trading_window > 0
        confidence: Literal["high", "medium", "low"] = (
            "high"
            if edge and n_events >= 50 and abs(edge.z) >= 3
            else "medium"
            if edge and n_events >= QUIET_BETS and abs(edge.z) >= 2
            else "low"
        )
        big = on_leaderboard or (volume or 0) >= BIG_VOLUME or (trading_pnl or 0) >= 2 * BIG_PROFIT

        skill, copyable = _verdicts(
            style=style, edge=edge, copy=copy, steady=steady, n_events=n_events, profitable=profitable
        )
        edge_verdict = _edge_verdict(style=style, edge=edge, trimmed=trimmed, n_events=n_events)
        segment = _segment(
            style=style, skill=skill, n_events=n_events, big=big, trading_pnl=trading_pnl, profit_wo_best=profit_wo_best, beats_and_steady=beats and steady,
            active=active, edge_verdict=edge_verdict, profitable=profitable,
            parlays=_mostly_parlays(combos, len(decisions)),
        )  # fmt: skip

        idle = None if last_at is None else end_ts - last_at
        checks = self._checks(
            edge, by_delay, copy, trimmed, best_two, n_events, confidence, trading_pnl, biggest, profit_wo_best,
            two_way, complete, maker, market_maker, arbitrage, cats, coverage, days, window, unrealized_window,
            active, idle, spread_earned=spread_earned, maker_bets=maker_bets,
        )  # fmt: skip
        reason = self._reason(
            segment, edge, n_events, combos, trading_pnl, biggest, profit_wo_best, two_way, complete, big,
            days, trading_window, idle, steady, len(decisions),
        )  # fmt: skip
        risks: list[str] = []
        if (
            income_window is not None
            and trading_window is not None
            and income_window > max(trading_window, 0)
        ):
            risks.append(
                f"Rewards and rebates ({_money(income_window)}) made more than trading"
                f" ({_money(trading_window)}) over these {days:g} days."
            )
        if (
            trading_window
            and trading_window > 0
            and unrealized_window
            and unrealized_window > trading_window / 2
        ):
            risks.append(
                f"Over half of their trading profit over these {days:g} days ({_money(unrealized_window)}) is on"
                " positions that haven't settled."
            )
        if best_day_share is not None and best_day_share > 0.5:
            risks.append(
                f"{best_day_share:.0%} of their trading profit over these {days:g} days came on one day."
            )
        if profit_wo_best is not None and profit_wo_best <= 0 and biggest:
            risks.append(
                f"Without their biggest win ({_money(biggest)}) they'd be down from trading, all time."
            )
        return Score(
            wallet=wallet,
            name=who,
            segment=segment,
            action=SEGMENTS[segment][1],
            reason=reason,
            confidence=confidence,
            tags=tuple(tags),
            checks=checks,
            edge=edge,
            edge_by_delay=by_delay,
            copy=copy,
            copy_entry=copy_entry if copy is not None else None,
            edge_verdict=edge_verdict,
            edge_at_fill=edge_at_fill,
            spread_earned=spread_earned,
            maker_bets=maker_bets,
            categories=cats,
            strong_categories=strong,
            bets=n_events,
            decisions=len(decisions),
            coverage=coverage,
            pnl=pnl,
            pnl_window=pnl_window,
            volume=volume,
            biggest_win=biggest,
            on_leaderboard=on_leaderboard,
            source=source,
            window=(datetime.fromtimestamp(start_ts, UTC), datetime.fromtimestamp(end_ts, UTC)),
            recent=recent,
            skill=skill,
            copyable=copyable,
            style=style,
            trading_pnl=trading_pnl,
            trading_pnl_window=trading_window,
            income_window=income_window,
            unrealized_window=unrealized_window,
            realized_window=realized_window,
            volume_window=window["volume"],
            return_per_dollar=window["return_per_dollar"],
            best_day_share=best_day_share,
            periods=periods,
            pnl_weeks=tuple(weeks) if weeks is not None else None,
            pnl_daily=pnl_daily,
            pnl_since=datetime.fromtimestamp(curve.since, UTC) if curve.since is not None else None,
            typical_usd=typical_usd,
            avg_price=avg_price,
            maker_share=round(maker, 3) if maker is not None else None,
            last_buy=last_buy,
            active=active,
            risks=tuple(risks),
            price_band=band,
            held_share=held,
            unread=unread,
            positions_usd=positions_usd,
            cash_usd=cash_usd,
            account_at=account_at,
            fills_from=(  # only when the cap cut the window short
                datetime.fromtimestamp(int(fills[-1]["timestamp"]), UTC)
                if fills and len(fills) >= max_fills
                else None
            ),
            sample=tuple(sorted(bets, key=lambda b: b.at, reverse=True)),
        )

    def _resting(
        self,
        wallet: str,
        bets: list[Bet],
        fills: list[dict[str, Any]],
        taker: list[dict[str, Any]] | None = None,
    ) -> list[Bet]:
        """Each checked buy with the share of its contracts that rested on the book (``maker``) and what that got
        them from the spread (``spread_earned``). A fill is a taker's when it's in Polymarket's taker-only trade
        feed, a resting order's otherwise. ``taker`` (their latest fills that took liquidity, read once for
        ``maker_share``) is used as it is when it holds all of them or reaches back past the oldest checked buy:
        no more reads. Otherwise the feed is read for the checked markets, ``TAKER_MARKETS`` at a time; a read
        that fails is tried again a market at a time. A buy whose market still can't be read keeps ``maker`` None,
        so :meth:`Bet.skill` leaves it out of the test."""
        priced = [b for b in bets if SKILL_S in b.moved]
        conds = sorted({b.market.rsplit(":", 1)[0] for b in priced})
        if not conds:
            return bets
        oldest = min(int(b.at.timestamp()) for b in priced)
        if taker is not None and (
            len(taker) < TAKER_LIMIT or min(int(t.get("timestamp") or 0) for t in taker) <= oldest
        ):
            results = [(set(conds), taker)]
        else:

            def read(part: list[str], retry: bool = True) -> tuple[set[str], list[dict[str, Any]]]:
                try:
                    rows = self._poly.taker_trades_in(wallet, part, TAKER_LIMIT)
                except VenueError as e:
                    if retry:  # a refused or failed read: once more, a market at a time
                        log.warning("uselayer: couldn't read %s's taker fills, trying again: %s", wallet, e)
                        return _union([read([c], retry=False) for c in part])
                    log.warning("uselayer: couldn't read %s's taker fills in %s: %s", wallet, part[0], e)
                    return set(), []
                if len(rows) < TAKER_LIMIT:
                    return set(part), rows
                if len(part) == 1:
                    return set(), []  # more taker fills in one market than a read holds: left untold
                return _union([read([c], retry=retry) for c in part])

            # one after another: usually the reused read covers it, and a pool per score costs memory (glibc arenas)
            results = [read(conds[i : i + TAKER_MARKETS]) for i in range(0, len(conds), TAKER_MARKETS)]
        known = set().union(*(k for k, _ in results))
        took = Counter(_fill_key(r) for _, rows in results for r in rows)
        buys: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for f in fills:
            if str(f.get("side")).upper() == "BUY":
                buys[(str(f.get("conditionId")), int(f.get("outcomeIndex") or 0))].append(f)
        out = []
        for b in bets:
            cond, outcome = b.market.rsplit(":", 1)
            at = int(b.at.timestamp())
            # the fills that made this decision: its outcome, within a minute of its first (as ``_decisions``)
            mine = [
                f for f in buys[(cond, int(outcome))] if at <= int(f.get("timestamp") or 0) <= at + MERGE_S
            ]
            size = sum(float(f.get("size") or 0) for f in mine)
            if cond not in known or size <= 0:
                out.append(b)
                continue
            taken = 0.0
            for f in mine:
                k = _fill_key(f)
                if took[k] > 0:
                    took[k] -= 1
                    taken += float(f.get("size") or 0)
            maker = round(max(0.0, 1 - taken / size), 3)
            earned = None
            if b.mid is not None and 0 < b.mid < 1:
                # half the spread, read from where the middle sits, and never more than they paid below it
                earned = round(maker * min(half_spread(b.mid), max(0.0, b.mid - b.price)), 5)
            elif maker == 0:
                earned = 0.0  # all took liquidity: nothing to take off
            out.append(replace(b, maker=maker, spread_earned=earned))
        return out

    def _check(
        self, picked: list[_Decision], info: dict[str, dict[str, Any]], end_ts: int, *, prices: bool = True
    ) -> list[Bet]:
        """Each decision with the price 1, 5 and 60 minutes later. One read per token covers all of a
        wallet's decisions on it within three days."""
        series: dict[int, list[tuple[int, float]]] = {}
        if prices:
            # Decisions on the same token within a few hours share one read; the rest get their own
            # short window, read in parallel.
            groups: list[tuple[str, list[_Decision]]] = []
            by_token: dict[str, list[_Decision]] = defaultdict(list)
            for d in picked:
                by_token[d.token].append(d)
            for token, ds in by_token.items():
                ds.sort(key=lambda d: d.at)
                for d in ds:
                    if groups and groups[-1][0] == token and d.at - groups[-1][1][0].at <= 6 * 3600:
                        groups[-1][1].append(d)
                    else:
                        groups.append((token, [d]))
            now = int(time.time())

            def read(g: tuple[str, list[_Decision]]) -> None:
                token, ds = g
                hi = min(ds[-1].at + max(HORIZONS) + 900, now)
                try:
                    pts = self.tape.series(token, ds[0].at - 120, hi)
                except VenueError as e:
                    log.warning("uselayer: no price history for %s: %s", token, e)
                    pts = []
                for d in ds:
                    series[id(d)] = pts

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(read, groups))
        out: list[Bet] = []
        for d in picked:
            m = info.get(d.condition) or {}
            payout = _payout(m, d.outcome)
            pts = series.get(id(d), [])
            moved: dict[int, float] = {}
            copy = fee = entry = None
            for h in HORIZONS:
                p = _price_at(pts, d.at + h, 330 if h < 3600 else 900)
                if p is None and payout is not None and pts and pts[-1][0] < d.at + h:
                    p = payout  # it settled before then
                if p is not None:
                    moved[h] = round(p - d.price, 5)
            p1 = _price_at(pts, d.at + DELAY_S, 330)
            if p1 is not None and 0 < p1 < 1 and HOLD_S in moved:
                # The history is the middle of the book: a copier buys at the ask, above it.
                entry = round(min(p1 + half_spread(p1), 1.0), 5)
                fee = round(taker_fee(m, entry), 5)
                copy = round(d.price + moved[HOLD_S] - entry - fee, 5)
            out.append(
                Bet(
                    market=f"{d.condition}:{d.outcome}",
                    title=d.title,
                    outcome=d.outcome_name,
                    event=d.event,
                    category=category_of(m) if m else "Other",
                    at=datetime.fromtimestamp(d.at, UTC),
                    price=round(d.price, 4),
                    usd=round(d.usd, 2),
                    moved=moved,
                    copy=copy,
                    fee=fee,
                    payout=payout,
                    entry=entry,
                    mid=_mid_at(pts, d.at) if pts else None,
                )
            )
        return out

    @staticmethod
    def _categories(bets: list[Bet]) -> tuple[CategoryScore, ...]:
        by: dict[str, list[Bet]] = defaultdict(list)
        for b in bets:
            by[b.category].append(b)
        out = []
        for cat, bs in by.items():
            edge = _stat(_by_event(bs, lambda b: b.skill()))
            copy = _stat(_by_event(bs, lambda b: b.copy))
            n = len({b.event for b in bs if b.skill() is not None})
            strong = bool(edge and n >= CATEGORY_BETS and edge.mean > 0 and edge.z >= 2)
            out.append(CategoryScore(cat, n, edge, copy, strong))
        out.sort(key=lambda c: (-c.bets, c.category))
        return tuple(out)

    @staticmethod
    def _checks(
        edge: Stat | None,
        by_delay: dict[int, Stat],
        copy: Stat | None,
        trimmed: Stat | None,
        best_two: list[Bet],
        n_events: int,
        confidence: str,
        pnl: float | None,
        biggest: float | None,
        profit_wo_best: float | None,
        two_way: float,
        complete: float,
        maker: float | None,
        market_maker: bool,
        arbitrage: bool,
        cats: tuple[CategoryScore, ...],
        coverage: dict[str, int],
        days: float,
        window: dict[str, float | None],
        unrealized_window: float | None,
        active: bool,
        idle: int | None,
        spread_earned: float | None = None,
        maker_bets: float | None = None,
    ) -> tuple[Check, ...]:
        out: list[Check] = []
        if edge is None:
            out.append(
                Check(
                    "beats_price",
                    "Beats the price",
                    None,
                    "Too few bets with a price history to tell." + _resting_note(None, None, coverage),
                )
            )
        else:
            steps = ", ".join(
                f"{_cents(by_delay[h].mean)} after {_AFTER[h]}" for h in HORIZONS if h in by_delay
            )
            passed = edge.mean > 0 and edge.z >= 2
            out.append(
                Check(
                    "beats_price",
                    "Beats the price",
                    passed,
                    f"After they buy, the price moved {steps} on average"
                    f" ({_cents(edge.mean)} ± {_cents(edge.margin, False)} at 5 minutes, the test)."
                    + ("" if passed else " That's not clearly above zero.")
                    + _resting_note(spread_earned, maker_bets, coverage),
                )
            )
        out.append(
            Check(
                "enough_bets",
                "Enough independent bets",
                n_events >= QUIET_BETS,
                f"{n_events} separate bets checked (bets on the same event count once): "
                + (
                    "enough to judge."
                    if n_events >= QUIET_BETS
                    else f"too few to judge (we want {QUIET_BETS})."
                )
                + f" Confidence in their edge: {confidence}. {coverage['priced_5m']} of {coverage['bets']} had a"
                " price history"
                + (
                    f"; {coverage['combos']} parlay buys (combo bets) can't be priced and weren't checked."
                    if coverage.get("combos")
                    else "."
                ),
            )
        )
        if trimmed is None:
            steady_text, steady = "Too few bets to tell.", None
        else:
            steady = trimmed.mean > 0 and (profit_wo_best is None or profit_wo_best > 0)
            steady_text = (
                f"Without their two best bets the price still moved {_cents(trimmed.mean)} their way."
            )
            if trimmed.mean <= 0:
                steady_text = f"Without their two best bets the edge is gone ({_cents(trimmed.mean)})."
            if pnl is not None and biggest is not None and profit_wo_best is not None:
                steady_text += (
                    f" All-time trading profit {_money(pnl)}; without their biggest win ({_money(biggest)})"
                    f" it's {_money(profit_wo_best)}."
                )
        out.append(Check("steady", "Steady, not lucky", steady, steady_text))
        made, income, traded, per = (
            window[k] for k in ("trading_pnl", "income", "volume", "return_per_dollar")
        )
        if made is None:
            out.append(
                Check("profitable", "Profitable from trading", None, "Couldn't read their profit history.")
            )
        else:
            text = f"From trading they {'made' if made >= 0 else 'lost'} {_money(abs(made))} over these {days:g} days"
            if made > 0 and unrealized_window and unrealized_window > 0:
                text += f" ({_money(unrealized_window)} of it on positions still open)"
            if per is not None and traded:
                text += f": {_cents(per)} per dollar of the {_money(traded)} they traded"
            if income:
                text += f". Rewards, rebates and yield ({_money(income)}) are counted apart"
            out.append(Check("profitable", "Profitable from trading", made > 0, text + "."))
        verdict = copyable_verdict(copy)
        if copy is None or verdict == "unknown":
            out.append(
                Check(
                    "copyable",
                    "Copyable",
                    None,
                    "Too few bets with prices after the buy to tell"
                    + (f" ({copy.n}; we want {COPY_BETS})." if copy is not None else "."),
                )
            )
        else:
            ok = verdict == "yes"
            out.append(
                Check(
                    "copyable",
                    "Copyable",
                    ok,
                    f"Buying the same thing 1–6 minutes later at the ask and paying the taker fee made {_cents(copy.mean)}"
                    f" ± {_cents(copy.margin, False)} a contract by the hour mark, over {copy.n} bets (95% range"
                    f" {_cents(copy.low)} to {_cents(copy.high)})"
                    + (
                        "."
                        if ok
                        else ": not reliably above zero."
                        if copy.mean > 0
                        else ": the edge is gone by the time a copier gets in."
                    ),
                )
            )
        if arbitrage:  # checked first, like the reason: buying both outcomes also counts as trading both ways
            side = (
                f"Buys both outcomes of a market within a minute for $1 or less ({complete:.0%} of markets):"
                " arbitrage, with no view to follow."
            )
        elif market_maker:
            side = (
                f"Trades both ways in the same market within the hour ({two_way:.0%} of their volume)"
                + (f", {maker:.0%} with resting orders" if maker is not None else "")
                + ": a market maker, with no view to follow."
            )
        else:
            side = f"Takes one side: {two_way:.0%} of their volume was traded both ways in the same hour."
        out.append(Check("takes_a_side", "Takes a side", not (market_maker or arbitrage), side))
        out.append(
            Check(
                "active",
                "Trading lately",
                active,
                "No buys in the window."
                if idle is None
                else f"Last bought {_for(idle)} before the end of the window"
                + ("." if active else f": nothing in the last {ACTIVE_DAYS} days."),
            )
        )
        strong = [c for c in cats if c.strong]
        if strong:
            text = "Clear edge in " + ", ".join(
                f"{c.category} ({_cents(c.edge.mean)} over {c.bets} bets)" for c in strong if c.edge
            )
            weak = [c.category for c in cats if not c.strong and c.bets >= CATEGORY_BETS]
            text += f"; not in {', '.join(weak)}." if weak else "."
        else:
            text = "No category with a clear edge yet."
        out.append(Check("categories", "Where their edge is", bool(strong) if cats else None, text))
        return tuple(out)

    @staticmethod
    def _reason(
        segment: Segment,
        edge: Stat | None,
        n: int,
        combos: int,
        pnl: float | None,
        biggest: float | None,
        profit_wo_best: float | None,
        two_way: float,
        complete: float,
        big: bool,
        days: float,
        trading_window: float | None,
        idle: int | None,
        steady: bool,
        decisions: int = 0,
    ) -> str:
        # About skill and track record only: whether copies gain is ``copyable``, said apart.
        e = _cents(edge.mean) if edge else "—"
        if segment == "parlays":
            return (
                f"Mostly parlays ({combos} of their {decisions} buys over these {days:g} days), which have no price"
                " history, so their skill can't be measured. Their profit still counts."
            )
        if segment == "edge_losing":
            return (
                f"The price moves their way after they buy ({e} within 5 minutes, over {n} bets), but they lost"
                f" {_money(-(trading_window or 0))} from trading over these {days:g} days."
            )
        if segment == "proven":
            return f"The price moves their way after they buy: {e} within 5 minutes, over {n} bets, and it holds without their best bets."
        if segment == "quiet":
            return f"A small account off the leaderboards, and the price moves their way after they buy: {e} within 5 minutes, over {n} bets."
        if segment == "rising":
            return f"Early signs: the price moved {e} their way within 5 minutes, over {n} bets. Not enough to be sure."
        if segment == "lucky":
            return f"{_money(pnl or 0)} trading profit, but without their biggest win ({_money(biggest or 0)}) they'd be down."
        if segment == "big_profit":
            if edge is not None and edge.mean > 0 and edge.z >= 2 and not steady:
                return f"{_money(pnl or 0)} trading profit, and the price moved {e} their way within 5 minutes, but not without their two best bets."
            return (
                f"{_money(pnl or 0)} trading profit, but the price doesn't clearly move their way after they buy ({e}"
                f" within 5 minutes, {n} bets), so it isn't shown to be skill."
            )
        if segment == "no_view":
            if complete >= ARB_SHARE:
                return "Arbitrage: buys both outcomes of a market within a minute for $1 or less, so there's no view to follow."
            return f"Market maker: trades both ways in the same market ({two_way:.0%} of volume), so there's no view to follow."
        if segment == "inactive":
            return f"No buys in the last {_for(idle or 0)}, so not recommended while they're away."
        if n < RISING_BETS:
            # Not a verdict: a trader who buys a few times a month can't reach the bets the test needs.
            if combos:
                return (
                    f"Limited evidence: {n} bets could be checked over these {days:g} days ({combos} more were"
                    f" parlays, which have no price history), and {RISING_BETS} are needed to measure skill."
                )
            return (
                f"Limited evidence: {n} bets could be checked over these {days:g} days, and {RISING_BETS} are"
                " needed to measure skill. Too few to say either way."
            )
        if edge is not None and edge.mean > 0 and edge.z >= 1:
            if trading_window is None:
                return f"The price moved {e} their way within 5 minutes, but their profit couldn't be read."
            if trading_window <= 0:
                return (
                    f"The price moved {e} their way within 5 minutes, but they lost {_money(-trading_window)}"
                    f" from trading over these {days:g} days."
                )
            if not steady:
                if profit_wo_best is not None and profit_wo_best <= 0 and biggest:
                    return (
                        f"The price moved {e} their way within 5 minutes, but without their biggest win"
                        f" ({_money(biggest)}) they'd be down from trading."
                    )
                return f"The price moved {e} their way within 5 minutes, but not without their two best bets."
            return f"The price moved {e} their way within 5 minutes ({n} bets), but not enough to be sure."
        return f"The price doesn't clearly move their way after they buy ({e} within 5 minutes, {n} bets)."


def _by(f: Future[float | None] | None, deadline: float, wallet: str, what: str,
        unread: list[str] | None = None) -> float | None:  # fmt: skip
    """A read started alongside the score, if it's done by ``deadline`` and didn't fail; else None, and ``what``
    goes on ``unread`` (when given), so a figure that couldn't be read isn't mistaken for one there's none of."""
    if f is None:
        return None
    try:
        return f.result(timeout=max(0.0, deadline - time.monotonic()))
    except (VenueError, ValueError, TimeoutError) as e:
        log.warning("uselayer: couldn't read %s for %s: %s", what, wallet, str(e) or "took too long")
        if unread is not None:
            unread.append(what)
        return None


def _first(*xs: float | None) -> float | None:
    return next((x for x in xs if x is not None), None)


def _f(x: Any) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


# ---- finding wallets ----


@dataclass
class Candidate:
    wallet: str
    name: str | None
    source: str
    on_leaderboard: bool
    weight: float = 0.0


def find_candidates(
    poly: PolymarketData,
    markets: MarketBook,
    *,
    leaderboard: int = 100,
    market_count: int = 40,
    beyond: int = 100,
    min_usd: float = 20,
) -> tuple[list[Candidate], set[str]]:
    """Wallets to score: the leaderboards (overall and by category), and wallets seen trading in busy
    markets that no leaderboard lists. Returns the candidates and every leaderboard wallet."""
    boards: dict[str, Candidate] = {}

    def board(order: str, period: str, n: int, category: str | None = None) -> None:
        for offset in range(0, n, 50):
            try:
                rows = poly.leaderboard(order, period, min(50, n - offset), category=category, offset=offset)
            except VenueError as e:
                log.warning("uselayer: leaderboard %s %s %s failed: %s", order, period, category, e)
                return
            for r in rows:
                w = str(r.get("proxyWallet") or "").lower()
                if w and w not in boards:
                    where = f"{(category or 'overall').lower()} {period.lower()} {'profit' if order == 'PNL' else 'volume'}"
                    boards[w] = Candidate(
                        w, r.get("userName"), f"Leaderboard: #{r.get('rank')} {where}", True
                    )
            if len(rows) < 50:
                return

    board("PNL", "MONTH", leaderboard)
    board("PNL", "WEEK", leaderboard // 2)
    board("PNL", "ALL", leaderboard // 2)
    board("VOL", "MONTH", leaderboard // 2)
    for cat in ("SPORTS", "CRYPTO", "POLITICS", "ECONOMICS", "CULTURE", "WEATHER", "ESPORTS", "TECH"):
        board("PNL", "MONTH", 50, cat)
    listed = set(boards)
    # A deeper look at the monthly board, so "off the leaderboards" means off the top 1,000.
    for offset in range(leaderboard, 1000, 50):
        try:
            listed |= {
                str(r.get("proxyWallet") or "").lower()
                for r in poly.leaderboard("PNL", "MONTH", 50, offset=offset)
            }
        except VenueError:
            break

    # Beyond the leaderboards: who trades in markets that are busy right now, across categories.
    found: dict[str, Candidate] = {}
    seen_in: dict[str, set[str]] = defaultdict(set)
    try:
        busy = poly._list(
            poly._profiles + "/markets",
            {"closed": "false", "limit": market_count, "order": "volume24hr", "ascending": "false"},
        )
        busy += poly._list(
            poly._profiles + "/markets",
            {"closed": "false", "limit": market_count, "offset": market_count * 3, "order": "volume24hr",
             "ascending": "false"},
        )  # fmt: skip
    except VenueError as e:
        log.warning("uselayer: couldn't list busy Polymarket markets: %s", e)
        busy = []
    for m in busy:
        cid = m.get("conditionId")
        if not cid:
            continue
        try:
            rows = poly.market_trades(cid, limit=500)
        except VenueError:
            continue
        for r in rows:
            w = str(r.get("proxyWallet") or "").lower()
            usd = float(r.get("size") or 0) * float(r.get("price") or 0)
            if not w or w in listed or usd < min_usd:
                continue
            seen_in[w].add(cid)
            c = found.setdefault(
                w,
                Candidate(
                    w, r.get("name") or None, f"Found trading in “{(m.get('question') or '')[:60]}”", False
                ),
            )
            c.weight += usd
    for w, c in found.items():
        c.weight *= len(seen_in[w])  # wallets seen across several markets first
    extra = sorted(found.values(), key=lambda c: c.weight, reverse=True)[:beyond]
    return [*boards.values(), *extra], listed


def discover(
    scorer: Scorer,
    poly: PolymarketData,
    *,
    wallets: int = 200,
    beyond: int | None = None,
    days: float = 30,
    sample: int = 60,
    workers: int = 6,
    on_progress: Callable[[int, int, Score | None], None] | None = None,
) -> Discovery:
    started = datetime.now(UTC)
    beyond = wallets // 2 if beyond is None else beyond
    cands, listed = find_candidates(
        poly, scorer.markets, leaderboard=max(50, wallets - beyond), beyond=beyond
    )
    board_cands = [c for c in cands if c.on_leaderboard][: max(0, wallets - beyond)]
    pool = board_cands + [c for c in cands if not c.on_leaderboard][:beyond]
    scores: list[Score] = []
    failed = 0
    done = 0
    lock = threading.Lock()

    def one(c: Candidate) -> Score | None:
        try:
            return scorer.score(
                c.wallet,
                days=days,
                sample=sample,
                on_leaderboard=c.wallet in listed,
                source=c.source,
                name=c.name,
            )
        except VenueError as e:
            log.warning("uselayer: couldn't score %s: %s", c.wallet, e)
            return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for s in ex.map(one, pool):
            with lock:
                done += 1
                if s is None:
                    failed += 1
                else:
                    scores.append(s)
            if on_progress:
                on_progress(done, len(pool), s)

    order = list(SEGMENTS)
    # By tier, then the strength of their edge: never by whether copies gain.
    scores.sort(key=lambda s: (order.index(s.segment), -(s.edge.z if s.edge else 0)))
    counts = {k: sum(1 for s in scores if s.segment == k) for k in SEGMENTS}
    coverage = {
        k: sum(s.coverage.get(k, 0) for s in scores)
        for k in ("bets", "priced_1m", "priced_5m", "priced_1h", "combos")
    }
    sources = {
        "leaderboard": sum(1 for c in pool if c.on_leaderboard),
        "beyond_leaderboards": sum(1 for c in pool if not c.on_leaderboard),
    }
    return Discovery(
        scores=tuple(scores),
        counts=counts,
        coverage=coverage,
        sources=sources,
        failed=failed,
        started=started,
        finished=datetime.now(UTC),
    )
