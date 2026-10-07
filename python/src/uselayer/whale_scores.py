"""Is a Polymarket trader worth following? A score from their public trades.

    s = client.whales.score("0x…")                   # one wallet, its last 30 days
    print(s.segment, s.reason)                        # "quiet", "Small account, off the leaderboards…"
    for c in s.checks:
        print(c.rule, c.passed, c.detail)             # the evidence, one plain sentence per rule
    found = client.whales.discover(wallets=150)       # wallets from leaderboards AND from markets' trades

Polymarket only: every wallet's full history is public there. The rules, in order of weight:

1. **Beats the price.** After they buy, does the price move their way? Each bet is checked 1, 5, 15 and
   60 minutes later against Polymarket's own price history. That history comes in 5-minute steps, so
   "5 minutes later" is the first step at least 5 minutes after the buy (5 to 10 minutes). The test is
   the 5-minute move: on 160 wallets (2026-10-07) it was the one that held up, a trader's earlier bets
   predicting their later ones, where the hour mark is mostly the noise of games playing out.
2. **Enough independent bets.** One bet per market, and bets on the same event (a game's winner and its
   total) count once. The confidence level comes from how many there are and how far the average is
   from zero.
3. **Steady.** The record doesn't hang on one or two lucky bets: the edge holds with their two best
   bets removed, and their all-time profit holds without their biggest win.
4. **Copyable.** Buying the same thing at the first price at least a minute later (1 to 6 minutes:
   Polymarket's public trade feed itself runs minutes behind), and paying Polymarket's taker fee
   (``rate × (p(1−p))^exponent`` a contract, from the market's ``feeSchedule``), still gains by the
   hour mark.
5. **Takes a side.** Market makers (trading both ways in the same market within the hour, mostly with
   resting orders) and arbitrage (buying both outcomes for under $1, or merging sets back) have no
   view to follow and are left out. Being a bot isn't a penalty: ``"bot"`` is only a tag.
6. **Category strengths.** The same test per category (sports, crypto…), so you can follow a trader
   only where their edge is.

Every number is per bet with equal weight, so one huge bet can't decide a score. A score is a reading of
the past, not a promise: show its ``confidence`` and ``checks`` with it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .errors import VenueError
from .venues.polymarket_data import PolymarketData

log = logging.getLogger(__name__)

Segment = Literal["proven", "quiet", "rising", "too_fast", "lucky", "no_view", "no_edge"]

#: Each segment's name on a page and what to do about it.
SEGMENTS: dict[str, tuple[str, str]] = {
    "proven": ("Proven sharps", "follow"),
    "quiet": ("Quiet sharps", "follow"),
    "rising": ("Rising", "watch"),
    "too_fast": ("Sharp but too fast", "signal"),
    "lucky": ("Lucky big bettors", "avoid"),
    "no_view": ("No view", "skip"),
    "no_edge": ("No clear edge", "skip"),
}

DELAY_S = 60  # how late a copier gets in
HORIZONS = (60, 300, 900, 3600)  # when the price is checked after their buy
SKILL_S = 300  # the move that tests skill ("beats the price")
HOLD_S = 3600  # when a copier's gain is measured
RISING_BETS = 8  # bets before early signs count
MERGE_S = 60  # fills on the same outcome this close together are one decision
PROVEN_BETS = 40  # independent bets for a proven sharp
QUIET_BETS = 25  # ... and for a quiet one
CATEGORY_BETS = 8  # bets in a category before it can count as a strength
BIG_PROFIT = 50_000  # all-time profit that makes a "big bettor"
BIG_VOLUME = 1_000_000  # all-time volume that makes an account big (not "quiet")
MAKER_TWO_WAY = 0.5  # share of their volume traded both ways in the same market-hour
ARB_SHARE = 0.3  # share of markets with a complete set bought, or merged

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


# ---- what you get back ----


@dataclass(frozen=True)
class Stat:
    """An average per bet with its 95% margin: ``mean ± margin`` over ``n`` independent bets.

    Bets on the same event are averaged into one first, so ``n`` counts events. ``z`` is the mean over
    its standard error: 2 or more means it's unlikely to be luck.
    """

    mean: float
    margin: float
    n: int
    z: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Bet:
    """One buy decision and what the price did next.

    ``price`` is what they paid. ``moved`` maps seconds after the buy (60, 300, 900, 3600) to how far the price
    had moved their way, in dollars a contract (0.02 = 2¢). ``copy`` is what a copier buying a minute
    later made by the hour mark, after the taker fee. ``payout`` is 1 or 0 once the market settled.
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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        d["moved"] = {str(k): v for k, v in self.moved.items()}
        return d


@dataclass(frozen=True)
class Check:
    """One rule's result: ``passed`` is True, False, or None when there's too little data to say."""

    rule: Literal["beats_price", "enough_bets", "steady", "copyable", "takes_a_side", "categories"]
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

    ``segment`` is one of :data:`SEGMENTS`; ``action`` is what to do about it (``"follow"``,
    ``"watch"``, ``"signal"``: watch but don't copy, ``"avoid"`` or ``"skip"``). ``reason`` is one plain
    sentence; ``checks`` hold the evidence for each rule. ``edge`` is the price move their way 5 minutes
    after they buy (the skill test), and ``copy`` what a copier 1–6 minutes late made by the hour mark after
    the taker fee, both in dollars a contract. ``coverage`` is how many of the bets checked had a price history.
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
            "copy": self.copy.to_dict() if self.copy else None,
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
            "sample": [b.to_dict() for b in self.sample] if sample else [],
        }


@dataclass(frozen=True)
class Discovery:
    """The result of :meth:`Whales.discover`: every wallet scored, best first within each segment.

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


class PriceTape:
    """Price histories (5-minute steps), fetched once per token and range, shared by every wallet scored.

    With ``cache_dir``, ranges that ended over two hours ago are also kept on disk: the past doesn't
    change, so a rerun doesn't read them again.
    """

    def __init__(self, poly: PolymarketData, cache_dir: str | Path | None = None) -> None:
        self._poly = poly
        self._dir = Path(cache_dir) if cache_dir else None
        self._mem: dict[str, list[tuple[int, int, list[tuple[int, float]]]]] = defaultdict(list)
        self._lock = threading.Lock()

    def series(self, token: str, start: int, end: int) -> list[tuple[int, float]]:
        with self._lock:
            for s, e, pts in self._mem[token]:
                if s <= start and end <= e:
                    return pts
        pts = self._read(token, start, end)
        with self._lock:
            self._mem[token].append((start, end, pts))
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
    """Gamma markets by ``conditionId`` (tags, fees, settlement), read in chunks of 20 and kept."""

    def __init__(self, poly: PolymarketData) -> None:
        self._poly = poly
        self._known: dict[str, dict[str, Any] | None] = {}
        self._lock = threading.Lock()

    def get(self, condition_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = sorted(set(condition_ids))
        with self._lock:
            todo = [c for c in ids if c not in self._known]
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
        with self._lock:
            return {c: known for c in ids if (known := self._known.get(c)) is not None}


# ---- one wallet ----


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


def _stat(values_by_event: dict[str, list[float]]) -> Stat | None:
    """Mean over events (each event's bets averaged first), with a 95% margin."""
    xs = [statistics.mean(v) for v in values_by_event.values() if v]
    if len(xs) < 2:
        return None
    m = statistics.mean(xs)
    se = statistics.stdev(xs) / math.sqrt(len(xs))
    return Stat(mean=round(m, 5), margin=round(1.96 * se, 5), n=len(xs), z=round(m / se, 2) if se else 0.0)


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


class Scorer:
    """Scores Polymarket wallets. Use :meth:`Whales.score` and :meth:`Whales.discover`."""

    def __init__(self, poly: PolymarketData, *, cache_dir: str | Path | None = None) -> None:
        self._poly = poly
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

    def _maker_share(self, wallet: str, fills: list[dict[str, Any]]) -> float | None:
        """Share of their recent fills that were resting orders, from the taker-only feed."""
        if len(fills) < 20:
            return None
        try:
            taker = self._poly.taker_trades(wallet, 500)
        except VenueError:
            return None
        recent = fills[: min(len(fills), 500)]
        since = int(recent[-1].get("timestamp") or 0)
        took = sum(1 for t in taker if int(t.get("timestamp") or 0) >= since)
        return max(0.0, 1 - took / len(recent))

    def _pnl_at(self, points: list[dict[str, Any]], at: int) -> float | None:
        before = [p for p in points if int(p.get("timestamp") or 0) <= at]
        v = before[-1].get("economic_pnl") if before else None
        return None if v is None else float(v)

    # -- the score --

    def score(
        self,
        wallet: str,
        *,
        days: float = 30,
        end: datetime | None = None,
        sample: int = 80,
        max_fills: int = 5000,
        on_leaderboard: bool = False,
        source: str = "asked",
        name: str | None = None,
    ) -> Score:
        end_ts = int((end or datetime.now(UTC)).timestamp())
        start_ts = end_ts - int(days * 86400)
        live = end is None
        fills = self._fills(wallet, start_ts, end_ts, max_fills)
        stats: dict[str, Any] = {}
        pnl_points: list[dict[str, Any]] = []
        try:
            stats = self._poly.stats(wallet)
            pnl_points = self._poly.pnl_history(wallet)
        except VenueError as e:
            log.warning("uselayer: couldn't read stats for %s: %s", wallet, e)
        raw_all_time = stats.get("all_time_pnl")
        all_time: dict[str, Any] = raw_all_time if isinstance(raw_all_time, dict) else {}
        pnl = self._pnl_at(pnl_points, end_ts) if not live else _f(all_time.get("economic_pnl"))
        if pnl is None and live:
            pnl = self._pnl_at(pnl_points, end_ts)
        before = self._pnl_at(pnl_points, start_ts)
        pnl_window = None if pnl is None or before is None else round(pnl - before, 2)
        volume = _f(all_time.get("volume")) if live else None
        biggest = _f(stats.get("biggest_win")) if live else None  # all-time: unknown for a past window

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
        merges = 0
        if fills:
            try:
                merges = len(self._poly.activity(wallet, type="MERGE", start=start_ts, end=end_ts, limit=500))
            except VenueError:
                merges = 0
        markets_traded = len({f.get("conditionId") for f in fills})
        maker = self._maker_share(wallet, fills) if two_way >= MAKER_TWO_WAY and live else None
        tags: list[str] = []
        span_days = max(1.0, days)
        if len(fills) / span_days >= 100 or len(fills) >= max_fills:
            tags.append("bot")
        market_maker = two_way >= MAKER_TWO_WAY and (maker is None or maker >= 0.6)
        arbitrage = complete >= ARB_SHARE or (markets_traded >= 5 and merges >= ARB_SHARE * markets_traded)
        if market_maker:
            tags.append("market maker")
        if arbitrage:
            tags.append("arbitrage")

        decisions = _decisions(fills)
        usable = [d for d in decisions if 0.02 <= d.price <= 0.98 and d.token]
        step = max(1, math.ceil(len(usable) / sample)) if sample else 1
        picked = usable[::step][:sample]
        info = self.markets.get(d.condition for d in picked)
        # Combo bets (parlays like "X AND Y") have no listed market and no price history: left out.
        combos = sum(1 for d in picked if d.condition not in info)
        bets = self._check([d for d in picked if d.condition in info], info, end_ts)
        latest = sorted(decisions, key=lambda d: d.at)[-5:][::-1]
        recent = tuple(
            self._check(latest, self.markets.get(d.condition for d in latest), end_ts, prices=False)
        )

        edge = _stat(_by_event(bets, lambda b: b.moved.get(SKILL_S)))
        by_delay: dict[int, Stat] = {}
        for h in HORIZONS:
            st = _stat(_by_event(bets, lambda b, h=h: b.moved.get(h)))  # type: ignore[misc]
            if st is not None:
                by_delay[h] = st
        copy = _stat(_by_event(bets, lambda b: b.copy))
        n_events = len({b.event for b in bets if SKILL_S in b.moved})
        coverage = {
            "bets": len(bets),
            "priced_1m": sum(1 for b in bets if 60 in b.moved),
            "priced_5m": sum(1 for b in bets if SKILL_S in b.moved),
            "priced_1h": sum(1 for b in bets if 3600 in b.moved),
            "combos": combos,
        }

        # Rule 3: steady. The edge without their two best bets, and profit without the biggest win.
        best_two = sorted((b for b in bets if SKILL_S in b.moved), key=lambda b: b.moved[SKILL_S])[-2:]
        trimmed = _stat(_by_event([b for b in bets if b not in best_two], lambda b: b.moved.get(SKILL_S)))
        profit_wo_best = None if pnl is None or biggest is None else pnl - biggest

        cats = self._categories(bets)
        strong = tuple(c.category for c in cats if c.strong)

        beats = edge is not None and edge.mean > 0 and edge.z >= 2
        hint = edge is not None and edge.mean > 0 and edge.z >= 1
        copyable = copy is not None and copy.mean > 0 and copy.z >= 1
        steady_edge = trimmed is not None and trimmed.mean > 0
        steady = steady_edge and (profit_wo_best is None or profit_wo_best > 0)
        confidence: Literal["high", "medium", "low"] = (
            "high"
            if edge and n_events >= 50 and abs(edge.z) >= 3
            else "medium"
            if edge and n_events >= QUIET_BETS and abs(edge.z) >= 2
            else "low"
        )
        big = on_leaderboard or (volume or 0) >= BIG_VOLUME or (pnl or 0) >= 2 * BIG_PROFIT

        segment: Segment
        if market_maker or arbitrage:
            segment = "no_view"
        elif beats and n_events >= QUIET_BETS and not copyable and copy is not None:
            segment = "too_fast"
        elif beats and copyable and steady and n_events >= (PROVEN_BETS if big else QUIET_BETS):
            segment = "proven" if big else "quiet"
        elif (pnl or 0) >= BIG_PROFIT and (
            (profit_wo_best is not None and profit_wo_best <= 0)
            or (n_events >= RISING_BETS and not (beats and steady))
        ):
            segment = "lucky"  # big profit, and the evidence says it isn't skill
        elif hint and n_events >= RISING_BETS and (copy is None or copy.mean >= 0):
            segment = "rising"
        else:
            segment = "no_edge"

        checks = self._checks(
            edge, by_delay, copy, trimmed, best_two, n_events, confidence, pnl, biggest, profit_wo_best,
            two_way, complete, merges, markets_traded, maker, market_maker, arbitrage, cats, coverage,
        )  # fmt: skip
        reason = self._reason(
            segment, edge, copy, n_events, pnl, biggest, profit_wo_best, two_way, complete, big
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
            sample=tuple(sorted(bets, key=lambda b: b.at, reverse=True)),
        )

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
            copy = fee = None
            for h in HORIZONS:
                p = _price_at(pts, d.at + h, 330 if h < 3600 else 900)
                if p is None and payout is not None and pts and pts[-1][0] < d.at + h:
                    p = payout  # it settled before then
                if p is not None:
                    moved[h] = round(p - d.price, 5)
            p1 = _price_at(pts, d.at + DELAY_S, 330)
            if p1 is not None and 0 < p1 < 1 and HOLD_S in moved:
                fee = round(taker_fee(m, p1), 5)
                copy = round(d.price + moved[HOLD_S] - p1 - fee, 5)
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
            edge = _stat(_by_event(bs, lambda b: b.moved.get(SKILL_S)))
            copy = _stat(_by_event(bs, lambda b: b.copy))
            n = len({b.event for b in bs if SKILL_S in b.moved})
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
        merges: int,
        markets_traded: int,
        maker: float | None,
        market_maker: bool,
        arbitrage: bool,
        cats: tuple[CategoryScore, ...],
        coverage: dict[str, int],
    ) -> tuple[Check, ...]:
        out: list[Check] = []
        if edge is None:
            out.append(
                Check("beats_price", "Beats the price", None, "Too few bets with a price history to tell.")
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
                    + ("" if passed else " That's not clearly above zero."),
                )
            )
        sure = {"high": "We're fairly sure", "medium": "Likely, not certain", "low": "Not enough to be sure"}
        out.append(
            Check(
                "enough_bets",
                "Enough independent bets",
                n_events >= QUIET_BETS if edge else False,
                f"{n_events} separate bets checked (bets on the same event count once). {sure[confidence]}"
                f" (confidence: {confidence}). {coverage['priced_5m']} of {coverage['bets']} had a price"
                " history"
                + (f"; {coverage['combos']} combo bets can't be checked." if coverage.get("combos") else "."),
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
                    f" All-time profit {_money(pnl)}; without their biggest win ({_money(biggest)})"
                    f" it's {_money(profit_wo_best)}."
                )
        out.append(Check("steady", "Steady, not lucky", steady, steady_text))
        if copy is None:
            out.append(Check("copyable", "Copyable", None, "Too few bets with prices after the buy to tell."))
        else:
            ok = copy.mean > 0 and copy.z >= 1
            out.append(
                Check(
                    "copyable",
                    "Copyable",
                    ok,
                    f"Buying the same thing 1–6 minutes later and paying the taker fee made {_cents(copy.mean)}"
                    f" ± {_cents(copy.margin, False)} a contract by the hour mark"
                    + ("." if ok else ": the edge is gone by the time a copier gets in."),
                )
            )
        if market_maker:
            side = (
                f"Trades both ways in the same market within the hour ({two_way:.0%} of their volume)"
                + (f", {maker:.0%} with resting orders" if maker is not None else "")
                + ": a market maker, with no view to follow."
            )
        elif arbitrage:
            side = (
                f"Buys both outcomes of a market for $1 or less ({complete:.0%} of markets) or merges them"
                f" back ({merges} merges): arbitrage, with no view to follow."
            )
        else:
            side = f"Takes one side: {two_way:.0%} of their volume was traded both ways in the same hour."
        out.append(Check("takes_a_side", "Takes a side", not (market_maker or arbitrage), side))
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
        copy: Stat | None,
        n: int,
        pnl: float | None,
        biggest: float | None,
        profit_wo_best: float | None,
        two_way: float,
        complete: float,
        big: bool,
    ) -> str:
        e = _cents(edge.mean) if edge else "—"
        c = _cents(copy.mean) if copy else "—"
        if segment == "proven":
            return f"The price moves their way after they buy, and copying a few minutes later still made {c} a bet after fees, over {n} bets."
        if segment == "quiet":
            return f"A small account off the leaderboards with a steady edge you can copy: {c} a bet after fees, over {n} bets."
        if segment == "rising":
            return f"Early signs: the price moved {e} their way within 5 minutes, over {n} bets. Not enough to be sure."
        if segment == "too_fast":
            return f"Real edge (the price moves {e} their way within 5 minutes), but a copier a few minutes late made {c} a bet after fees."
        if segment == "lucky":
            if profit_wo_best is not None and profit_wo_best <= 0 and biggest:
                return f"{_money(pnl or 0)} profit, but without their biggest win ({_money(biggest)}) they'd be down."
            return f"{_money(pnl or 0)} profit, but the price doesn't clearly move their way after they buy ({e} within 5 minutes)."
        if segment == "no_view":
            if complete >= ARB_SHARE:
                return "Arbitrage: buys both outcomes for under $1, so there's no view to follow."
            return f"Market maker: trades both ways in the same market ({two_way:.0%} of volume), so there's no view to follow."
        if n < RISING_BETS:
            return f"Too few recent bets to judge ({n} in the window checked)."
        return f"The price doesn't clearly move their way after they buy ({e} within 5 minutes, {n} bets)."


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
    scores.sort(
        key=lambda s: (order.index(s.segment), -(s.copy.mean if s.copy else -1), -(s.edge.z if s.edge else 0))
    )
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
