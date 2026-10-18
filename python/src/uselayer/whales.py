"""Whale tracking and copy trading across Kalshi and Polymarket.

    client = Client(layer_key="lyr_...", kalshi=Kalshi.from_env())
    top = client.whales.top(by="pnl", limit=20)              # both venues, one list
    who = client.whales.trader("kalshi", "year.lamp")       # stats, positions, recent trades
    for link in client.whales.links(who.trader):             # the same person on the other venue?
        print(link.trader.name, link.tier, link.score, [e.detail for e in link.evidence])
    copier = client.whales.follow(who.trader, size=5)        # copy their new trades (paper by default)
    for event in copier.poll():
        print(event.status, event.reason)

Where the data comes from, all read on your machine without a key:

- **Kalshi**: the public endpoints behind kalshi.com's Leaderboard and profile pages
  (:mod:`uselayer.venues.kalshi_social`). Kalshi doesn't document them, so they can change. Traders
  appear only if they opted in, and a trader can hide their trades and holdings.
- **Polymarket**: its public data API (:mod:`uselayer.venues.polymarket_data`). Every wallet is public.

Linking a trader across venues is an inference, never an identity. ``links()`` returns a score and the
evidence behind it: a matching name, and trades on the same bet (found through Layer's matching), in the
same direction, close together in time. Show the evidence with the score, and never say a link "is"
someone.

Copying places your own orders through the client, so every guardrail applies and paper mode (the
default) fills against the real book with fake money. A Polymarket trade is copied onto its twin on
Kalshi or Polymarket US, found through Layer's matching; it needs a Layer key.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from .errors import VenueError
from .events import Resolution, SimulatedFill
from .http import Http
from .layer_api import LayerApi
from .titles import Titles
from .venues.kalshi_social import PNL_UNITS, KalshiSocial
from .venues.polygon import Polygon
from .venues.polymarket_book import PolymarketBook
from .venues.polymarket_data import PolymarketData
from .whale_finder import (
    LEADERBOARD_DEPTH,
    MARKET_BANDS,
    MARKETS_PER_BAND,
    Board,
    Sighting,
    leaderboard_wallets,
    market_wallets,
)
from .whale_scores import (
    Account,
    ClosedPosition,
    Discovery,
    Score,
    Scorer,
    _once_more,
    category_of,
    closed_positions,
    discover,
    polymarket_url,
    taker_fee,
)

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger(__name__)

WhaleVenue = Literal["kalshi", "polymarket"]
VENUES: tuple[WhaleVenue, ...] = ("kalshi", "polymarket")
POLYMARKET_PROFILE = "https://polymarket.com/profile/"


def _ts(s: Any) -> datetime:
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(float(s), UTC)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _f(x: Any) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _outcome_quote(market: dict[str, Any], outcome: int) -> tuple[float | None, float | None]:
    """``(best bid, best ask)`` for one outcome of a Gamma market. Gamma quotes the first outcome; the
    second is its mirror (a bid for one is an ask for the other at 1 − price)."""
    bid, ask = _f(market.get("bestBid")), _f(market.get("bestAsk"))
    if outcome == 0:
        return bid, ask
    return (None if ask is None else round(1 - ask, 6), None if bid is None else round(1 - bid, 6))


def _outcome_index(
    market: dict[str, Any], index: str, name: str | None, token: str | None = None
) -> int | None:
    """Which outcome of a Gamma market a trade bought: the outcome whose token is the one traded, first.
    Without a token (or a market without token ids), the feed's index if it's one of the market's
    outcomes (Polymarket's feed sometimes sends a junk ``outcomeIndex``, 999 seen 10-12), then the
    outcome's name. None when none of them says, or when the token belongs to neither outcome."""
    try:
        tokens = [str(x) for x in json.loads(market.get("clobTokenIds") or "[]")]
    except (TypeError, ValueError):
        tokens = []
    if token and tokens:
        return tokens.index(token) if token in tokens else None
    try:
        outcomes = [str(o) for o in json.loads(market.get("outcomes") or "[]")]
    except (TypeError, ValueError):
        outcomes = []
    i = int(index) if index.isdigit() else -1
    if not outcomes:
        return i if i in (0, 1) else None
    if 0 <= i < len(outcomes):
        return i
    hits = [n for n, o in enumerate(outcomes) if name and o.strip().lower() == name.strip().lower()]
    return hits[0] if len(hits) == 1 else None


def _token_id(market: dict[str, Any], outcome: int) -> str | None:
    """The order book token of one outcome of a Gamma market, if Gamma gave its ``clobTokenIds``."""
    try:
        ids = json.loads(market.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    return str(ids[outcome]) if outcome < len(ids) and ids[outcome] else None


def _walk(asks: list[tuple[float, float]], qty: float, market: dict[str, Any]) -> tuple[float, float, float]:
    """``(cost, fee, worst price)`` of buying ``qty`` contracts up the asks, cheapest first, with
    Polymarket's taker fee at each level's price. The asks must hold ``qty``."""
    cost = fee = worst = 0.0
    left = qty
    for price, size in asks:
        take = min(size, left)
        if take <= 0:
            break
        cost += price * take
        fee += taker_fee(market, price) * take
        worst, left = price, left - take
    return round(cost, 6), round(fee, 6), worst


def _affordable(asks: list[tuple[float, float]], spend: float, market: dict[str, Any]) -> float:
    """How many contracts (fractional) ``spend`` buys up the asks, fee included."""
    left, qty = spend, 0.0
    for price, size in asks:
        each = price + taker_fee(market, price)
        take = min(size, left / each)
        qty, left = qty + take, left - take * each
        if take < size:
            break
    return qty


def _polymarket_payout(market: dict[str, Any], outcome: int) -> float | None:
    """What one contract of ``outcome`` paid, once Polymarket has resolved the market; else None."""
    if not market.get("closed"):
        return None
    try:
        prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return None
    final = all(p in (0.0, 1.0) for p in prices)
    if outcome >= len(prices) or not (market.get("umaResolutionStatus") == "resolved" or final):
        return None
    return prices[outcome]


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _short_wallet(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w


ACTIVE_VOLUME = 10_000  # contracts on Kalshi, dollars on Polymarket


def _active(t: Trader) -> bool:
    return (t.volume or 0) >= ACTIVE_VOLUME


def _volume_text(t: Trader) -> str:
    v = t.volume or 0
    return f"{v:,.0f} contracts traded" if t.volume_unit == "contracts" else f"${v:,.0f} traded"


# ---- what you get back ----


@dataclass(frozen=True)
class Trader:
    """One trader on one venue.

    ``id`` is the Kalshi nickname or the Polymarket wallet. ``pnl`` is all-time profit in dollars.
    ``volume`` is in ``volume_unit``: contracts on both venues' leaderboards (Polymarket's counts contracts too),
    dollars bought and sold on a Polymarket trader's page and for a Polymarket account found by name (``links()``).
    """

    venue: WhaleVenue
    id: str
    name: str
    rank: int | None = None
    pnl: float | None = None
    volume: float | None = None
    volume_unit: str = "usd"
    x_handle: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WhaleTrade:
    """One trade by a trader, in one shape for both venues.

    ``market`` is the Kalshi ticker, or ``<conditionId>:<outcomeIndex>`` on Polymarket (the id Layer's
    matching takes). ``side`` is ``"yes"``/``"no"`` on Kalshi; on Polymarket it is ``"yes"`` for the
    outcome in ``market`` (so buying the other outcome shows as that market's own id). ``price`` is what
    one contract of ``side`` cost, ``size`` is contracts and ``usd`` is ``price * size``.

    On Kalshi, ``role`` says whether the trader took (``taker``) or rested (``maker``) the order. A
    maker's ``action`` is inferred from the taker's: Kalshi doesn't say whether a maker bought one side
    or sold the other, so treat a maker's direction as reliable and its action as a guess.

    On Polymarket, ``token`` is the exact outcome token traded (Polymarket's ``asset``). Copies match it
    against the market's outcomes first, since the outcome index in Polymarket's feed is sometimes junk.
    """

    venue: WhaleVenue
    trader: str | None
    name: str | None
    market: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    price: float
    size: float
    usd: float
    at: datetime
    trade_id: str
    title: str | None = None
    outcome: str | None = None
    role: Literal["taker", "maker"] | None = None
    url: str | None = None
    token: str | None = None

    @property
    def long_yes(self) -> bool:
        """True when the trade adds to a YES position on ``market`` (buy YES or sell NO)."""
        return (self.side == "yes") == (self.action == "buy")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        d["long_yes"] = self.long_yes
        return d


@dataclass(frozen=True)
class WhalePosition:
    market: str
    title: str | None
    outcome: str | None
    size: float
    avg_price: float | None
    current_price: float | None
    value: float | None
    pnl: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompletedPosition:
    """One position a Polymarket wallet is done with: closed, or settled and never redeemed.

    ``market`` is ``<conditionId>:<outcomeIndex>``. ``pnl`` is its realized profit after fees: Polymarket's
    ``realizedPnl`` for a closed one, and for one never redeemed what it pays at settlement minus what it cost.
    ``redeemed`` is False for one never redeemed: Polymarket's closed positions leave those out. ``closed_at`` is
    when a closed one closed; one never redeemed has only its market's ``end_date``.
    """

    market: str
    title: str | None
    outcome: str | None
    pnl: float
    redeemed: bool
    closed_at: datetime | None = None
    end_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["closed_at"] = self.closed_at.isoformat() if self.closed_at else None
        return d


@dataclass(frozen=True)
class PositionRecord:
    """Every position a Polymarket wallet is done with, and whether that's all of them.

    ``positions`` has each market outcome once: it counts positions, not markets (both sides of one market are two),
    so say "positions", never "predictions" (Polymarket's profile word for markets traded). ``complete`` is True when every closed position and every position
    held was read, and nothing is left out of both lists. It's False when a list was cut at the read limit, when
    Polymarket's paging slipped (a page repeated a row already read, so another was skipped), or when they have
    profit or loss from parlays (``parlay_pnl``, Polymarket's realized combo profit): Polymarket lists parlays in
    neither list, so a record without them isn't theirs. ``realized_pnl`` is Polymarket's all-time realized profit;
    ``accounted_pnl`` is what the positions read add up to (closed, never redeemed, and what positions still held
    realized by selling part of them). ``reconciles`` is True when the record is complete and the two agree within
    the tolerance asked for: only then is a rate from ``positions`` the wallet's whole record. ``made_money`` counts
    positions with ``pnl`` above zero.

    Polymarket works ``realized_pnl`` out in batches (``trader().stats["pnl_as_of"]``, the same time for many
    wallets, hours apart): a wallet that closed or settled a position since then doesn't reconcile until the next
    batch. A caller that wants the market positions alone, parlays left out and said to be, can compare
    ``accounted_pnl`` with ``realized_pnl - parlay_pnl`` instead (on two wallets checked, that gap was the parlay
    profit to the dollar); ``reconciles`` never does.
    """

    wallet: str
    positions: list[CompletedPosition]
    complete: bool
    realized_pnl: float | None
    accounted_pnl: float
    reconciles: bool
    read_at: datetime
    parlay_pnl: float | None = None

    @property
    def made_money(self) -> int:
        return sum(1 for p in self.positions if p.pnl > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "positions": [p.to_dict() for p in self.positions],
            "complete": self.complete,
            "realized_pnl": self.realized_pnl,
            "accounted_pnl": self.accounted_pnl,
            "reconciles": self.reconciles,
            "parlay_pnl": self.parlay_pnl,
            "made_money": self.made_money,
            "count": len(self.positions),
            "read_at": self.read_at.isoformat(),
        }


@dataclass(frozen=True)
class TraderDetail:
    """A trader's page: stats, open positions, recent trades and, on Polymarket, closed positions.

    ``visibility`` is ``"hidden"`` when a Kalshi trader hides their trades and holdings: ``positions``
    and ``trades`` are then empty, which is not the same as having none.

    ``closed`` is the positions they no longer hold, newest first: what they paid, what they got out at,
    their profit and whether they sold or held until the market settled (see
    :class:`~uselayer.whale_scores.ClosedPosition`). None on Kalshi, which doesn't publish them, and when
    Polymarket's couldn't be read; empty when they've closed none.
    """

    trader: Trader
    visibility: str
    stats: dict[str, Any]
    positions: list[WhalePosition]
    trades: list[WhaleTrade]
    bio: str | None = None
    joined: str | None = None
    closed: list[ClosedPosition] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trader": self.trader.to_dict(),
            "visibility": self.visibility,
            "stats": self.stats,
            "positions": [p.to_dict() for p in self.positions],
            "trades": [t.to_dict() for t in self.trades],
            "bio": self.bio,
            "joined": self.joined,
            "closed": [c.to_dict() for c in self.closed] if self.closed is not None else None,
        }


@dataclass(frozen=True)
class Evidence:
    """One reason two traders might be the same person. ``weight`` is its share of the score (log-odds)."""

    kind: Literal["same_name", "same_x_handle", "same_direction", "opposite_direction"]
    detail: str
    weight: float
    markets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Link:
    """A trader on the other venue who may be the same person, with the evidence.

    ``tier`` is ``"likely"`` (score ≥ 0.8) or ``"possible"`` (≥ 0.4). The score is a heuristic, not a
    calibrated probability. ``checked_markets`` is how many of the trader's recent bets could be looked
    up on the other venue; with few of them, no co-trading evidence was possible.
    """

    trader: Trader
    score: float
    tier: Literal["likely", "possible"]
    evidence: tuple[Evidence, ...]
    shared_markets: int
    checked_markets: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "trader": self.trader.to_dict(),
            "score": self.score,
            "tier": self.tier,
            "evidence": [e.to_dict() for e in self.evidence],
            "shared_markets": self.shared_markets,
            "checked_markets": self.checked_markets,
        }


@dataclass(frozen=True)
class CopyEvent:
    """What the copier did with one of the trader's trades.

    ``status`` is ``"copied"`` (an order was sent; see ``filled``) or ``"skipped"`` with ``reason`` in a
    plain sentence. ``simulated`` is True in paper mode: no venue saw the order. On one of their sells,
    ``share`` is the part of their position they sold (0.4 = 40%), and the copy sells that part of yours.
    """

    at: datetime
    source: WhaleTrade
    status: Literal["copied", "skipped"]
    reason: str
    venue: str | None = None
    market: str | None = None
    side: str | None = None
    action: str | None = None
    price: float | None = None
    size: float | None = None
    filled: float = 0.0
    avg_price: float | None = None
    fees: float | None = None
    order_id: str | None = None
    simulated: bool = True
    share: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        d["source"] = self.source.to_dict()
        return d


@dataclass(frozen=True)
class CopyPreview:
    """What copying one trade now would do, from :meth:`Whales.preview_copy`. Nothing is sent.

    ``ok`` is False when it can't be copied now, with ``reason`` in a plain sentence. Otherwise ``price``
    is the average a contract costs now, from walking Polymarket's order book for this many contracts
    (``worst_price`` is the dearest level reached), ``limit`` the most you'd pay (their price plus
    ``max_slippage``), ``cost`` = ``price`` × ``contracts``, ``fee`` Polymarket's taker fee at each level
    and ``total`` = ``cost`` + ``fee``: what you'd pay, and lose if the outcome loses. ``payout`` is $1 a
    contract if it wins and ``profit_if_win`` = ``payout`` − ``total``. All in dollars. ``spend`` is the
    amount asked for when the copy was sized in dollars (``None`` when sized in contracts).

    ``max_spend`` is everything for sale at ``limit`` or better, fee included: when the book can't fill the
    whole amount, ``ok`` is False and ``max_spend`` says what would. ``depth_checked`` is False when the
    order book couldn't be read and the price is Gamma's best price alone, which a large amount may not
    really fill at. For one of their sells (a copier's), ``share`` is the part of their position they sold.

    ``best_price`` is the lowest ask on the outcome's order book at preview time, before the fee: what the
    cheapest contract on offer costs. It comes from the same book read that prices the amount, so no other
    request is made and it can't disagree with ``price``. On an ``ok`` preview ``price`` is the average
    over all the contracts you'd buy, so it's ``best_price`` or more (more when the amount takes dearer
    levels), ``worst_price`` is the dearest level reached, and ``price`` − ``best_price`` is this amount's
    slippage, before the fee. ``best_price`` is set when ``ok`` is False too (nothing near their price, the
    book too thin for the amount, under the smallest order), so you can still show what's on offer, even
    above ``limit``. It's None when the book has no asks or wasn't read (``depth_checked`` False, and for
    every sell).
    """

    trade: WhaleTrade
    ok: bool
    reason: str
    venue: str
    market: str
    side: str
    spend: float | None = None
    action: str | None = None
    contracts: float | None = None
    price: float | None = None
    limit: float | None = None
    cost: float | None = None
    fee: float | None = None
    total: float | None = None
    payout: float | None = None
    profit_if_win: float | None = None
    worst_price: float | None = None
    max_spend: float | None = None
    depth_checked: bool = False
    share: float | None = None
    best_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade"] = self.trade.to_dict()
        return d


@dataclass(frozen=True)
class CopyResult:
    """How one copied buy is doing, from your own fills and the market's settlement.

    ``status`` is ``"open"`` (market not settled: valued at the best bid, ``mark``), ``"won"``,
    ``"lost"`` or ``"void"`` (settled: ``payout`` is what one contract paid), or ``"unfilled"``.
    ``pnl`` is after fees, in dollars; ``None`` while open with no bid to value it at. ``simulated`` is
    True in paper mode: no venue saw the order.
    """

    order_id: str
    venue: str
    market: str
    side: str
    contracts: float
    avg_price: float | None
    cost: float
    fees: float
    status: Literal["open", "won", "lost", "void", "unfilled"]
    payout: float | None
    mark: float | None
    pnl: float | None
    settled_at: datetime | None
    simulated: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["settled_at"] = None if self.settled_at is None else self.settled_at.isoformat()
        return d


# ---- reading each venue into one shape ----


def _kalshi_trade(t: dict[str, Any], nickname: str | None, titles: Titles | None) -> WhaleTrade:
    yes = float(t.get("price_dollars") or (_f(t.get("price")) or 0) / 100)
    size = _f(t.get("count_fp")) or _f(t.get("count")) or 0.0
    taker_side: Literal["yes", "no"] = "yes" if t.get("taker_side") == "yes" else "no"
    taker_action: Literal["buy", "sell"] = "sell" if t.get("taker_action") == "sell" else "buy"
    taker, maker = t.get("taker_nickname") or None, t.get("maker_nickname") or None
    role: Literal["taker", "maker"] | None = None
    side: Literal["yes", "no"] = taker_side
    action: Literal["buy", "sell"] = taker_action
    who = taker or maker
    if nickname is not None:
        who = nickname
        role = "taker" if taker == nickname else "maker"
    elif taker is None and maker is not None:
        role = "maker"
    elif taker is not None:
        role = "taker"
    if role == "maker":
        # The maker took the other direction. Report it as a buy of the other side (see WhaleTrade).
        taker_long_yes = (taker_side == "yes") == (taker_action == "buy")
        side, action = ("no" if taker_long_yes else "yes"), "buy"
    price = yes if side == "yes" else round(1 - yes, 6)
    ticker = str(t.get("ticker") or "")
    title = outcome = None
    if titles is not None and ticker:
        filled = titles.fill({"venue": "kalshi", "group_id": ticker.rsplit("-", 1)[0], "market_id": ticker})
        title = filled.get("question") or filled.get("event")
        outcome = filled.get("outcome") if filled.get("outcome") != title else None  # combos repeat it
    return WhaleTrade(
        venue="kalshi",
        trader=who,
        name=who,
        market=ticker,
        side=side,
        action=action,
        price=price,
        size=size,
        usd=round(price * size, 2),
        at=_ts(t.get("create_date")),
        trade_id=str(t.get("trade_id") or ""),
        title=title,
        outcome=outcome,
        role=role,
    )


def _polymarket_trade(t: dict[str, Any]) -> WhaleTrade:
    size = _f(t.get("size")) or 0.0
    price = _f(t.get("price")) or 0.0
    idx = int(t.get("outcomeIndex") or 0)
    wallet = t.get("proxyWallet")
    name = t.get("name") or t.get("pseudonym") or ""
    if not name or name.startswith("0x"):  # no username: Polymarket sends "<wallet>-<id>"
        name = _short_wallet(wallet) if wallet else ""
    return WhaleTrade(
        venue="polymarket",
        trader=wallet,
        name=name or None,
        market=f"{t.get('conditionId')}:{idx}",
        side="yes",
        action="sell" if str(t.get("side")).upper() == "SELL" else "buy",
        price=price,
        size=size,
        usd=round(_f(t.get("usdcSize")) or price * size, 2),
        at=_ts(t.get("timestamp")),
        trade_id=str(t.get("transactionHash") or ""),
        title=t.get("title"),
        outcome=t.get("outcome"),
        url=_polymarket_url(t.get("eventSlug"), t.get("slug")),
        token=str(t["asset"]) if t.get("asset") else None,
    )


_polymarket_url = polymarket_url


# ---- copying their sells in proportion ----


def _position_change(t: WhaleTrade) -> float:
    """What ``t`` did to the position ``Whales._position_now`` reads: contracts of its outcome on
    Polymarket; signed YES contracts on Kalshi, where buying NO nets against YES."""
    if t.venue == "kalshi":
        return t.size if t.long_yes else -t.size
    return t.size if t.action == "buy" else -t.size


def _sell_qty(held: float, share: float) -> float:
    """``share`` of the ``held`` contracts, in whole contracts; all of them when they sold out."""
    return held if share >= 1 - 1e-9 else min(held, float(math.floor(held * share + 0.5)))


def _no_share(e: VenueError) -> str:
    return f"They sold, but their position couldn't be read, so how much to sell isn't known: {e.message}"


def _too_small(share: float, held: float) -> str:
    return f"They sold {share:.0%} of their position: that part of your {held:g} contracts rounds to zero."


def _as_they_sold(share: float) -> str:
    return (
        ", all of yours: they sold out"
        if share >= 1 - 1e-9
        else f", {share:.0%} of yours as they sold {share:.0%}"
    )


# ---- the API ----


class Whales:
    """Top traders, their trades, cross-venue links and copy trading. Use ``client.whales``."""

    def __init__(self, http: Http, layer: LayerApi, client: Client | None = None) -> None:
        self._kalshi = KalshiSocial(http)
        self._poly = PolymarketData(http)
        self._chain = Polygon(http)
        self._book = PolymarketBook(http)
        self._layer = layer
        self._client = client
        self._titles = Titles(http)
        self._twins: dict[tuple[str, str, str], str | None] = {}
        self._scorer: Scorer | None = None

    # -- traders --

    def top(
        self, venue: WhaleVenue | Literal["all"] = "all", *, by: str = "pnl", limit: int = 25
    ) -> list[Trader]:
        """The top traders by all-time profit (``by="pnl"``) or volume (``by="volume"``).

        With ``venue="all"``, both venues' lists merged by profit (volume isn't merged: each venue counts its
        own contracts), each trader keeping their own venue's ``rank``.
        """
        if by not in ("pnl", "volume"):
            raise ValueError('by must be "pnl" or "volume"')
        out: list[Trader] = []
        if venue in ("all", "kalshi"):
            metric = "projected_pnl" if by == "pnl" else "volume"
            for r in self._kalshi.leaderboard(metric, limit):
                nick = str(r.get("nickname") or "")
                if not nick or r.get("is_anonymous"):
                    continue
                v = _f(r.get("value"))
                out.append(
                    Trader(
                        venue="kalshi",
                        id=nick,
                        name=nick,
                        rank=int(r.get("rank") or 0) or None,
                        pnl=v if by == "pnl" else None,
                        volume=v if by == "volume" else None,
                        volume_unit="contracts",
                    )
                )
        if venue in ("all", "polymarket"):
            for r in self._poly.leaderboard("PNL" if by == "pnl" else "VOL", "ALL", limit):
                w = str(r.get("proxyWallet") or "")
                if not w:
                    continue
                out.append(
                    Trader(
                        venue="polymarket",
                        id=w,
                        name=r.get("userName") or _short_wallet(w),
                        rank=int(r.get("rank") or 0) or None,
                        pnl=_f(r.get("pnl")),
                        volume=_f(r.get("vol")),
                        volume_unit="contracts",  # the leaderboard's vol is user-stats' volume: contracts
                        x_handle=r.get("xUsername") or None,
                        url=POLYMARKET_PROFILE + w,
                    )
                )
        if venue == "all":
            key = (lambda t: t.pnl or 0.0) if by == "pnl" else (lambda t: t.rank or 10**9)
            out.sort(key=key, reverse=by == "pnl")
        return out[: limit * (2 if venue == "all" else 1)]

    def trader(
        self, venue: WhaleVenue, id: str, *, trades: int = 50, positions: int = 50, closed: int = 50
    ) -> TraderDetail:
        """One trader's page: stats, open positions, recent trades and (Polymarket) up to ``closed``
        closed positions, newest first."""
        if venue == "kalshi":
            prof = self._kalshi.profile(id)
            if not prof:
                raise VenueError(
                    "not_found",
                    f"Kalshi has no public profile named {id!r}.",
                    venue="kalshi",
                    retryable=False,
                )
            m = self._kalshi.metrics(id)
            holdings, vis = self._kalshi.holdings(id, positions)
            stats: dict[str, Any] = {
                "pnl": None if m.get("pnl") is None else round(float(m["pnl"]) / PNL_UNITS, 2),
                "volume_contracts": _f(m.get("volume_fp") or m.get("volume")),
                "open_interest_contracts": _f(m.get("open_interest_fp") or m.get("open_interest")),
                "markets_traded": m.get("num_markets_traded"),
                "followers": prof.get("follower_count"),
            }
            rows, tvis, _ = self._kalshi.trades(id, page_size=trades)
            if "hidden" in (vis, tvis):
                vis = "hidden"
            return TraderDetail(
                trader=Trader(
                    venue="kalshi",
                    id=id,
                    name=str(prof.get("nickname") or id),
                    pnl=stats["pnl"],
                    volume=stats["volume_contracts"],
                    volume_unit="contracts",
                ),
                visibility=vis,
                stats=stats,
                positions=self._kalshi_positions(holdings),
                trades=[_kalshi_trade(t, id, self._titles) for t in rows],
                bio=prof.get("description") or None,
                joined=prof.get("joined_at"),
            )
        if venue == "polymarket":
            prof = self._poly.profile(id)
            s = self._poly.stats(id)
            pnl = (s.get("all_time_pnl") or {}) if isinstance(s.get("all_time_pnl"), dict) else {}
            realized, unrealized = (
                _f(pnl.get(k)) if pnl else None for k in ("realized_pnl", "unrealized_pnl")
            )
            trading = None if realized is None or unrealized is None else round(realized + unrealized, 2)
            stats = {
                "pnl": _f(pnl.get("economic_pnl") if pnl else None),  # Polymarket's figure: rewards in
                "trading_pnl": trading,  # from trading only, after fees: realized plus unrealized
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "income": _f(pnl.get("wallet_income")) if pnl else None,  # rewards, rebates, yield
                # dollars bought and sold; Polymarket's ``volume`` counts contracts (the fills' sizes added up)
                "volume_usd": _f(pnl.get("volume_usdc")) if pnl else None,
                "volume_contracts": _f(pnl.get("volume")) if pnl else None,
                # how many trades (fills) they made
                "trades": pnl.get("trade_count") if pnl else None,
                # how many markets they've traded: user-stats' top-level ``trades``, the "Predictions" on their
                # Polymarket profile (as on Kalshi)
                "markets_traded": s.get("trades"),
                "biggest_win": _f(s.get("biggest_win")),
                "fees_paid": _f(pnl.get("fees_paid")) if pnl else None,
                # Polymarket works these figures out every few hours: when it last did
                "pnl_as_of": _ts(pnl["timestamp"]).isoformat() if pnl and pnl.get("timestamp") else None,
            }
            w = str(prof.get("proxyWallet") or id)
            shut: list[ClosedPosition] | None = None
            if closed:
                try:
                    shut = closed_positions(self._poly, w, closed)
                except VenueError as e:
                    log.warning("uselayer: couldn't read the closed positions of %s: %s", w, e)
            return TraderDetail(
                trader=Trader(
                    venue="polymarket",
                    id=w,
                    name=prof.get("name") or prof.get("pseudonym") or _short_wallet(w),
                    pnl=stats["trading_pnl"],  # as on the leaderboard: trading, not rewards
                    volume=stats["volume_usd"],
                    volume_unit="usd",
                    x_handle=prof.get("xUsername") or None,
                    url=POLYMARKET_PROFILE + w,
                ),
                visibility="visible",
                stats=stats,
                positions=self._poly_open(w, positions),
                trades=[_polymarket_trade(t) for t in self._poly.trades(w, trades)],
                bio=prof.get("bio") or None,
                joined=prof.get("createdAt"),
                closed=shut,
            )
        raise ValueError('venue must be "kalshi" or "polymarket"')

    def trades(self, venue: WhaleVenue, id: str, *, limit: int = 50) -> list[WhaleTrade]:
        """A trader's recent trades, newest first. Empty for a Kalshi trader who hides them."""
        if venue == "kalshi":
            rows, _, _ = self._kalshi.trades(id, page_size=limit)
            return [_kalshi_trade(t, id, None) for t in rows]
        return [_polymarket_trade(t) for t in self._poly.trades(id, limit)]

    def positions(
        self, venue: WhaleVenue, id: str, *, limit: int = 50, market: str | None = None
    ) -> list[WhalePosition]:
        """A trader's open positions, the same list as ``trader(...).positions``, in one request.

        ``market`` narrows it to one market: a Kalshi ticker, or a Polymarket ``conditionId`` (or
        ``"conditionId:outcomeIndex"``, as :attr:`WhalePosition.market` gives it, for one outcome).
        Empty for a Kalshi trader who hides their holdings; ``trader()`` tells that apart with
        ``visibility``.
        """
        if venue == "kalshi":
            holdings, _ = self._kalshi.holdings(id, limit)
            out = self._kalshi_positions(holdings)
            return [p for p in out if p.market == market] if market else out
        if venue == "polymarket":
            return self._poly_open(id, limit, market)
        raise ValueError('venue must be "kalshi" or "polymarket"')

    def completed_positions(
        self, wallet: str, *, max_pages: int = 10, held_pages: int = 2, tolerance: float = 0.002
    ) -> PositionRecord:
        """Every position a Polymarket wallet is done with: the ones Polymarket lists as closed, and the ones whose
        market settled but which they never redeemed. Polymarket's closed positions leave the second kind out, and
        they're mostly losses (a losing position pays nothing, so there's nothing to redeem): a record from closed
        positions alone flatters anyone who leaves losers unredeemed.

        Reads: 1 for their all-time stats, then 1 per 500 positions held (up to ``held_pages``) and 1 per 50 closed
        positions (up to ``max_pages``), a read more for a list that ends a page exactly. That's 3 for most wallets
        and 13 at most. A read Polymarket's firewall turns away is tried once more; a second refusal raises
        :class:`VenueError`, never a record from what was read.

        Give a rate from it (``made_money`` of ``positions``) only when ``reconciles``: the positions read then
        account for Polymarket's all-time realized profit within ``tolerance`` (0.2%), or 10¢ for a small one.
        Otherwise part of their history is missing, Polymarket hasn't yet counted their latest positions in its
        figures, or they trade parlays (``parlay_pnl``), which Polymarket lists in neither list. Measured
        2026-10-18 on 33 wallets read in full: 27 matched within a cent and 2 within 0.1% (0.099% and 0.015%).
        The other 4, 2% to 119% off, were trading that day, with positions closing or settling around
        Polymarket's last batch.

        Each market outcome is counted once. One listed as closed that they also still hold (a remainder never
        redeemed) is the closed one: Polymarket's closed row has its whole profit. Positions held are read before
        closed ones, so one sold or redeemed between the reads is in both lists (and counted once), never in
        neither. A wallet holding more than 500 can still lose one between the held pages, when a position before
        it leaves the list: rare, and its profit or loss then stops the record reconciling unless it's under the
        tolerance.
        """
        # each read Polymarket's firewall turns away is tried once more; a second refusal raises, never a short page
        s = _once_more(lambda: self._poly.stats(wallet))
        pnl: dict[str, Any] = s["all_time_pnl"] if isinstance(s.get("all_time_pnl"), dict) else {}
        realized = _f(pnl.get("realized_pnl"))
        parlays = _f(pnl.get("realized_combo_pnl"))

        def market(r: dict[str, Any]) -> str:
            return f"{r.get('conditionId')}:{int(r.get('outcomeIndex') or 0)}"

        def read_all(
            read: Callable[[int], list[dict[str, Any]]], size: int, pages: int
        ) -> tuple[dict[str, dict[str, Any]], bool]:
            """Every row of a paged list by market outcome, and whether it was all read once each."""
            rows: dict[str, dict[str, Any]] = {}
            whole, offset = True, 0
            for _ in range(pages):
                page = _once_more(partial(read, offset))
                offset += len(page)
                for r in page:
                    if market(r) in rows:
                        # Polymarket's paging slipped: a row read twice means another was skipped
                        whole = False
                    else:
                        rows[market(r)] = r
                if len(page) < size:
                    return rows, whole
            return rows, False  # a full last page: there may be more

        # Held before closed: a position sold or redeemed between the two reads is then in both lists (and counted
        # once, as closed), never in neither.
        held_all, held_whole = read_all(
            lambda o: self._poly.held_positions(wallet, 500, offset=o), 500, held_pages
        )
        closed, closed_whole = read_all(
            lambda o: self._poly.closed_positions(wallet, 50, offset=o), 50, max_pages
        )
        # a position listed as closed is that one, even if they still hold a remainder of it never redeemed
        held = {k: r for k, r in held_all.items() if k not in closed}
        unredeemed = [r for r in held.values() if r.get("redeemable")]
        # parlays are in neither list: a wallet with parlay profit or loss has history no list here shows
        complete = closed_whole and held_whole and not round(parlays or 0.0, 2)

        out = [
            CompletedPosition(
                market=market(r),
                title=r.get("title"),
                outcome=r.get("outcome"),
                pnl=round(_f(r.get("realizedPnl")) or 0.0, 2),
                redeemed=True,
                closed_at=_ts(r["timestamp"]) if r.get("timestamp") else None,
                end_date=r.get("endDate"),
            )
            for r in closed.values()
        ]
        # settled, never redeemed: what it pays now (its value) minus what it cost, fees in (realizedPnl)
        out += [
            CompletedPosition(
                market=market(r),
                title=r.get("title"),
                outcome=r.get("outcome"),
                pnl=round((_f(r.get("cashPnl")) or 0.0) + (_f(r.get("realizedPnl")) or 0.0), 2),
                redeemed=False,
                end_date=r.get("endDate"),
            )
            for r in unredeemed
        ]
        accounted = (
            sum(_f(r.get("realizedPnl")) or 0.0 for r in closed.values())
            # positions still held: what selling part of one made, and fees
            + sum(_f(r.get("realizedPnl")) or 0.0 for r in held.values())
            + sum(_f(r.get("cashPnl")) or 0.0 for r in unredeemed)
        )
        agrees = realized is not None and abs(accounted - realized) <= max(0.10, tolerance * abs(realized))
        return PositionRecord(
            wallet=wallet,
            positions=out,
            complete=complete,
            realized_pnl=realized,
            accounted_pnl=round(accounted, 2),
            reconciles=complete and agrees,
            read_at=datetime.now(UTC),
            parlay_pnl=parlays,
        )

    def big_trades(
        self, min_usd: float = 1000, *, venues: tuple[WhaleVenue, ...] = VENUES, kalshi_pages: int = 3
    ) -> list[WhaleTrade]:
        """Recent trades worth at least ``min_usd`` on each venue, newest first, with the trader's name
        when the venue shows it.

        Kalshi names only traders who opted into its social features (about 1 trade in 10); the rest
        have ``trader=None``. Each Kalshi page is 1,000 trades, a few seconds of a busy market, so call
        this on a loop to watch. Polymarket's feed runs a few minutes behind.
        """
        out: list[WhaleTrade] = []
        if "kalshi" in venues:
            cursor = None
            for _ in range(max(1, kalshi_pages)):
                rows, _, cursor = self._kalshi.trades(None, page_size=1000, cursor=cursor)
                for t in rows:
                    wt = _kalshi_trade(t, None, None)
                    if wt.usd >= min_usd:
                        out.append(wt)
                if not cursor:
                    break
        if "polymarket" in venues:
            out += [_polymarket_trade(t) for t in self._poly.big_trades(min_usd)]
        out.sort(key=lambda t: t.at, reverse=True)
        return out

    # -- cross-venue links --

    def _twin(self, venue: str, market: str, with_: str | None = None) -> str | None:
        """The market id of ``market``'s twin through Layer, or None."""
        key = (venue, market, with_ or "")
        if key not in self._twins:
            try:
                body = self._layer.match(market, venue=venue, with_=with_, titles=False)
            except VenueError as e:
                if e.code == "auth_failed":
                    raise
                log.warning("uselayer: no twin for %s %s: %s", venue, market, e)
                return None
            mm = body.get("matched_market") or {}
            self._twins[key] = mm.get("market_id")
        return self._twins[key]

    def _poly_to_kalshi(self, market: str) -> tuple[str, bool] | None:
        """``(kalshi ticker, flip)`` for a Polymarket ``<conditionId>:<outcome>``, through Layer.

        ``flip`` is True when buying that outcome is buying NO on the ticker (a yes/no market's "No").
        """
        ticker = self._twin("polymarket", market)
        if ticker:
            return ticker, False
        cid, _, idx = market.partition(":")
        if idx == "1":
            ticker = self._twin("polymarket", cid)
            if ticker:
                return ticker, True
        return None

    def _kalshi_to_poly(self, ticker: str) -> tuple[str, int] | None:
        """``(conditionId, outcome that pays like YES)`` for a Kalshi ticker, through Layer."""
        mid = self._twin("kalshi", ticker, "polymarket")
        if not mid:
            return None
        cid, _, idx = mid.partition(":")
        return cid, int(idx) if idx.isdigit() else 0

    def links(
        self,
        trader: Trader,
        *,
        candidates: int = 25,
        max_markets: int = 15,
        window_s: float = 300,
    ) -> list[Link]:
        """Traders on the other venue who may be the same person, best first.

        Candidates are the other venue's top traders by profit and by volume. Evidence: the same name or
        X handle, and trades on the same bet in the same direction within ``window_s`` seconds. Bets are
        put side by side on their Kalshi ticker; the twins come from Layer's matching (needs a Layer key,
        one lookup per market for the ``max_markets`` most recent markets the trader bet on).
        Opposite-direction trades close together look like one trader's arbitrage and count a little.
        Only links scoring 0.4 or more are returned.
        """
        other: WhaleVenue = "polymarket" if trader.venue == "kalshi" else "kalshi"
        pool = {c.id: c for c in self.top(other, by="pnl", limit=candidates)}
        for c in self.top(other, by="volume", limit=candidates):
            pool.setdefault(c.id, c)

        # The trader's recent bets, keyed by (Kalshi ticker, long YES).
        keyed: dict[tuple[str, bool], list[datetime]] = {}
        poly_twins: dict[str, tuple[str, int]] = {}  # conditionId -> (ticker, YES outcome)
        if self._layer_ok():
            mine = self.trades(trader.venue, trader.id, limit=200)
            markets: list[str] = []
            for t in mine:
                if t.market not in markets and len(markets) < max_markets:
                    markets.append(t.market)
            if trader.venue == "kalshi":
                for ticker in markets:
                    twin = self._kalshi_to_poly(ticker)
                    if twin:
                        poly_twins[twin[0]] = (ticker, twin[1])
                for t in mine:
                    if t.market in markets:
                        keyed.setdefault((t.market, t.long_yes), []).append(t.at)
            else:
                to_k = {m: self._poly_to_kalshi(m) for m in markets}
                for t in mine:
                    k = to_k.get(t.market)
                    if k:
                        keyed.setdefault((k[0], t.long_yes != k[1]), []).append(t.at)
        checked = len({k[0] for k in keyed})

        # Who on Polymarket traded the same bets: each twin market's recent trades, with wallets. These
        # wallets join the candidates, so a trader who isn't on any leaderboard can still be found.
        scanned: dict[str, list[WhaleTrade]] = {}
        for cid in poly_twins:
            try:
                raw = self._poly.market_trades(cid, limit=500)
            except VenueError as e:
                log.warning("uselayer: couldn't read Polymarket trades on %s: %s", cid, e)
                continue
            for r in raw:
                t = _polymarket_trade(r)
                if t.trader:
                    scanned.setdefault(t.trader, []).append(t)
        for wallet, wts in scanned.items():
            pool.setdefault(
                wallet,
                Trader(
                    venue="polymarket",
                    id=wallet,
                    name=wts[0].name or _short_wallet(wallet),
                    url=POLYMARKET_PROFILE + wallet,
                ),
            )

        # Co-trading per candidate, then how common each match was: matching on a market where many
        # traders went the same way within the window says little, so each market counts by how rare
        # the match was there (log of traders on it over traders who matched), up to 2.
        co: dict[str, tuple[set[str], set[str]]] = {}
        if keyed:
            for c in pool.values():
                theirs = scanned.get(c.id, []) if trader.venue == "kalshi" else self._their_trades(c)
                co[c.id] = self._co_trades(keyed, poly_twins, theirs, window_s)
        on_market: dict[str, int] = {}
        matched_on: dict[str, int] = {}
        for c in pool.values():
            seen_on = scanned.get(c.id) or []
            if seen_on:
                for cid in {r.market.partition(":")[0] for r in seen_on}:
                    if cid in poly_twins:
                        on_market[poly_twins[cid][0]] = on_market.get(poly_twins[cid][0], 0) + 1
            for ticker in co.get(c.id, (set(), set()))[0]:
                matched_on[ticker] = matched_on.get(ticker, 0) + 1

        def rarity(ticker: str) -> float:
            n, m = on_market.get(ticker), matched_on.get(ticker, 1)
            return 1.5 if not n else max(0.3, min(2.0, math.log(n / m)))

        for c in self._namesakes(trader):
            pool[c.id] = c

        mins = max(1, int(window_s // 60))
        out: list[Link] = []
        for c in pool.values():
            ev: list[Evidence] = []
            names = {_norm(trader.name), _norm(trader.x_handle)} - {""}
            if c.x_handle and _norm(c.x_handle) in names:
                ev.append(Evidence("same_x_handle", f"Their X handle is @{c.x_handle}", 4.0))
            elif _norm(c.name) in names and len(_norm(c.name)) >= 4:
                # Anyone can take a name, so it counts only for an account that really trades.
                if _active(c):
                    ev.append(
                        Evidence(
                            "same_name",
                            f"Same name on both venues ({c.name}), and that account trades: {_volume_text(c)}",
                            3.5,
                        )
                    )
                else:
                    ev.append(
                        Evidence("same_name", f"Same name on both venues ({c.name}), little trading", 1.5)
                    )
            same, opposite = co.get(c.id, (set(), set()))
            if same:
                ev.append(
                    Evidence(
                        "same_direction",
                        f"Bet the same way on {len(same)} of the same markets within {mins} min",
                        round(min(6.0, sum(rarity(t) for t in same)), 2),
                        tuple(sorted(same)),
                    )
                )
            if opposite:
                ev.append(
                    Evidence(
                        "opposite_direction",
                        f"Took the other side on {len(opposite)} of the same markets within {mins} min"
                        " (an arbitrage pattern)",
                        round(min(2.0, 0.5 * len(opposite)), 2),
                        tuple(sorted(opposite)),
                    )
                )
            if not ev:
                continue
            score = 1 / (1 + math.exp(-(-3.0 + sum(e.weight for e in ev))))
            if score < 0.4:
                continue
            out.append(
                Link(
                    trader=c,
                    score=round(score, 3),
                    tier="likely" if score >= 0.8 and (len(same) >= 3 or len(ev) > 1) else "possible",
                    evidence=tuple(ev),
                    shared_markets=len(same) + len(opposite),
                    checked_markets=checked,
                )
            )
        out.sort(key=lambda link: link.score, reverse=True)
        return out

    def _layer_ok(self) -> bool:
        return getattr(self._layer, "_key", None) is not None

    def _namesakes(self, trader: Trader) -> list[Trader]:
        """Accounts on the other venue with exactly the trader's name, with their volume and profit."""
        name = _norm(trader.name)
        if len(name) < 4 or trader.name.startswith("0x"):
            return []
        out: list[Trader] = []
        try:
            if trader.venue == "kalshi":
                for p in self._poly.search_profiles(trader.name):
                    w = str(p.get("proxyWallet") or "")
                    if not w or _norm(p.get("name")) != name:
                        continue
                    s = self._poly.stats(w)
                    pnl = s.get("all_time_pnl") if isinstance(s.get("all_time_pnl"), dict) else {}
                    out.append(
                        Trader(
                            venue="polymarket",
                            id=w,
                            name=str(p.get("name")),
                            pnl=_f((pnl or {}).get("economic_pnl")),
                            volume=_f((pnl or {}).get("volume_usdc")),  # ``volume`` counts contracts
                            volume_unit="usd",
                            x_handle=p.get("xUsername") or None,
                            url=POLYMARKET_PROFILE + w,
                        )
                    )
            else:
                prof = self._kalshi.profile(trader.name)
                nick = str(prof.get("nickname") or "")
                if nick and _norm(nick) == name:
                    m = self._kalshi.metrics(nick)
                    out.append(
                        Trader(
                            venue="kalshi",
                            id=nick,
                            name=nick,
                            pnl=None if m.get("pnl") is None else round(float(m["pnl"]) / PNL_UNITS, 2),
                            volume=_f(m.get("volume_fp") or m.get("volume")),
                            volume_unit="contracts",
                        )
                    )
        except VenueError as e:
            if e.code != "not_found":  # no account by that name is the usual answer
                log.warning("uselayer: name lookup for %s failed: %s", trader.name, e)
        return out

    def _their_trades(self, c: Trader) -> list[WhaleTrade]:
        try:
            return self.trades(c.venue, c.id, limit=300)
        except VenueError as e:
            log.warning("uselayer: couldn't read %s trades for %s: %s", c.venue, c.id, e)
            return []

    @staticmethod
    def _co_trades(
        keyed: dict[tuple[str, bool], list[datetime]],
        poly_twins: dict[str, tuple[str, int]],
        theirs: list[WhaleTrade],
        window_s: float,
    ) -> tuple[set[str], set[str]]:
        """Kalshi tickers where ``theirs`` traded the same / the other way within the window.

        Two controls keep busy traders (bots and market makers that trade everything) from matching:
        a market where they traded both ways counts for neither, and a market where they also match
        the trader's times shifted 2 and 4 windows earlier or later is background, not evidence.
        Matched locally: no Layer lookups per candidate.
        """
        tickers = {k[0] for k in keyed}
        mine: list[tuple[str, bool, datetime]] = []
        for t in theirs:
            if t.venue == "kalshi":
                if t.market not in tickers:
                    continue
                mine.append((t.market, t.long_yes, t.at))
            else:
                cid, _, idx = t.market.partition(":")
                twin = poly_twins.get(cid)
                if twin is None:
                    continue
                mine.append((twin[0], (int(idx or 0) == twin[1]) == (t.action == "buy"), t.at))
        directions: dict[str, set[bool]] = {}
        for ticker, long_yes, _ in mine:
            directions.setdefault(ticker, set()).add(long_yes)
        two_sided = {k for k, v in directions.items() if len(v) > 1}

        def matched(shift_s: float) -> tuple[set[str], set[str]]:
            same: set[str] = set()
            opposite: set[str] = set()
            for ticker, long_yes, at in mine:
                if ticker in two_sided:
                    continue
                for direction, bucket in ((long_yes, same), (not long_yes, opposite)):
                    times = keyed.get((ticker, direction))
                    if times and any(abs((at - x).total_seconds() - shift_s) <= window_s for x in times):
                        bucket.add(ticker)
            return same, opposite

        same, opposite = matched(0)
        for k in (-4, -2, 2, 4):
            s, o = matched(k * window_s)
            same -= s
            opposite -= o
        return same, opposite - same

    # -- worth following? (Polymarket) --

    def scorer(self, cache_dir: str | None = None) -> Scorer:
        """The scorer behind :meth:`score` and :meth:`discover`, sharing price histories between calls.
        ``cache_dir`` keeps past price histories on disk so reruns don't read them again."""
        if self._scorer is None or cache_dir is not None:
            self._scorer = Scorer(self._poly, cache_dir=cache_dir, chain=self._chain)
        return self._scorer

    def score(
        self,
        wallet: str,
        *,
        days: float = 30,
        end: datetime | None = None,
        sample: int = 60,
        on_leaderboard: bool = False,
    ) -> Score:
        """Whether a Polymarket wallet is worth following, from its last ``days`` of public trades.

        Returns a :class:`~uselayer.whale_scores.Score`: a ``segment``, the tier from skill and track record
        (proven / quiet sharp, rising, big profit with the edge unproven, profit resting on one win, parlays
        with skill unmeasured, not enough data, an edge but lost money lately, market maker or arbitrage, not
        trading lately, edge not proven), the ``skill`` verdict it comes from, ``edge_verdict`` (the price
        test alone, no profit in it) and, apart, ``copyable`` (never part of the tier), a one-sentence ``reason``, the evidence per rule in
        ``checks`` and ``risks``. Profit is from trading, with rewards and rebates kept apart. Up to ``sample`` bets are checked against Polymarket's price history. ``end`` scores
        an earlier window, to test a score against what the trader did next. Takes 5–30 seconds.
        See :mod:`uselayer.whale_scores` for the rules.
        """
        return self.scorer().score(
            wallet.lower(), days=days, end=end, sample=sample, on_leaderboard=on_leaderboard
        )

    def account(self, wallet: str) -> Account:
        """A Polymarket wallet's account now: open positions' worth and pUSD cash, each None when it can't be
        read, with ``read_at``. Show ``total_usd`` only when ``complete``; otherwise say which part is missing."""
        return self.scorer().account(wallet.lower())

    def discover(
        self,
        *,
        wallets: int = 200,
        beyond: int | None = None,
        days: float = 30,
        sample: int = 60,
        on_progress: Callable[[int, int, Score | None], None] | None = None,
    ) -> Discovery:
        """Find and score Polymarket wallets worth following.

        Candidates are the leaderboards (overall and per category) and, so small accounts can be found,
        ``beyond`` wallets (default half) seen trading in busy markets that the top 1,000 doesn't list.
        Each is scored as :meth:`score` does; ``on_progress(done, total, score)`` is called after each.
        Takes several minutes for 200 wallets: run it in the background and keep the result.
        """
        return discover(
            self.scorer(), self._poly, wallets=wallets, beyond=beyond, days=days, sample=sample,
            on_progress=on_progress,
        )  # fmt: skip

    def find_wallets(
        self,
        *,
        leaderboards: bool = True,
        markets: bool = True,
        boards: Iterable[Board] | None = None,
        depth: int = LEADERBOARD_DEPTH,
        bands: tuple[float, ...] = MARKET_BANDS,
        per_band: int = MARKETS_PER_BAND,
        per_second: float | None = None,
    ) -> Iterator[Sighting]:
        """Polymarket wallets, each with where it was seen: every leaderboard (``boards``, default all of
        them, ``depth`` places deep), then the last trades of open markets picked across every level of
        activity (``per_band`` from each of ``bands``). Scores nothing; see :mod:`uselayer.whale_finder`.

        All of it is about 18,000 reads (nearly all leaderboard pages) and yields far more sightings than
        wallets: keep each wallet once, with all the places it was seen. ``per_second`` holds the reads to
        that rate, below the per-host limits, so a long sweep leaves room for other reads.
        """
        if leaderboards:
            yield from leaderboard_wallets(self._poly, boards, depth=depth, per_second=per_second)
        if markets:
            yield from market_wallets(self._poly, bands=bands, per_band=per_band, per_second=per_second)

    def category(self, market: str) -> str | None:
        """A Polymarket market's category (Sports, Crypto, Politics…), for ``<conditionId>[:outcome]``."""
        cid = market.partition(":")[0]
        m = self.scorer().markets.get([cid]).get(cid)
        return category_of(m) if m else None

    # -- copy trading --

    def follow(
        self,
        trader: Trader,
        *,
        size: float | None = None,
        ratio: float | None = None,
        max_size: float = 100,
        max_slippage: float = 0.03,
        venue: Literal["kalshi", "polymarket_us", "polymarket"] = "kalshi",
        max_age_s: float = 300,
        min_usd: float = 0,
        categories: tuple[str, ...] = (),
        copy_sells: bool = True,
    ) -> Copier:
        """A copier for ``trader``'s new trades. Nothing is copied until you call ``poll()`` or ``run()``.

        Size each copy as ``size`` contracts, or ``ratio`` of theirs, never more than ``max_size``.
        The limit price is their price plus ``max_slippage`` (dollars); an order that can't fill there
        isn't sent. Kalshi traders are copied on the same Kalshi market; Polymarket traders on the twin
        market on ``venue`` (needs a Layer key). Trades older than ``max_age_s`` when first seen, and
        every trade before you start following, are never copied. With ``categories`` (e.g.
        ``("Sports",)``), a Polymarket trader is copied only in those categories (see ``score().
        strong_categories``). ``min_usd`` skips their small buys; their sells are copied whatever their size.

        Their sells are mirrored in proportion: when they sell 40% of their position, the copy sells 40% of
        the contracts you hold on that side (all of them when they sell out), whatever ``size`` says,
        at no less than their price minus ``max_slippage``. The share comes from their position now, read
        from the venue when the sell is copied, plus the trades they've made since. "You hold" is your
        whole position on that side, copies of other traders included. With ``copy_sells=False`` only
        their buys are copied, and held until the market settles (see :meth:`copy_results`).

        ``venue="polymarket"`` copies a Polymarket trader on the same Polymarket international market and
        outcome, in paper mode only (this SDK doesn't trade there). Each copy fills at once, as a taker,
        at that outcome's best price on Polymarket's public market data (Gamma), with Polymarket's taker
        fee; Gamma gives the best price but not how much is offered there, so the whole size fills at
        it. It goes into the paper store like any fill and pays out when Polymarket resolves the market.
        The client's rules (``preview()``) don't run on these copies; the kill switch does.
        :meth:`copy_results` values them at Polymarket's best bid and pays them out; ``client.pnl()``
        counts them once paid out, but can't value one while it's open (no Polymarket book in the client).
        """
        if self._client is None:
            raise ValueError("follow() needs a client: use client.whales.follow(...)")
        if (size is None) == (ratio is None):
            raise ValueError("pass exactly one of size= or ratio=")
        if venue == "polymarket" and self._client.mode == "live":
            raise ValueError(
                "Copies on Polymarket international are paper only: this SDK doesn't trade there. "
                "Use a paper client, or venue='kalshi' / 'polymarket_us'."
            )
        return Copier(
            self,
            self._client,
            trader,
            size=size,
            ratio=ratio,
            max_size=max_size,
            max_slippage=max_slippage,
            venue=venue,
            max_age_s=max_age_s,
            min_usd=min_usd,
            categories=tuple(categories),
            copy_sells=copy_sells,
        )

    def preview_copy(
        self,
        trade: WhaleTrade,
        *,
        size: float | None = None,
        spend: float | None = None,
        max_slippage: float = 0.03,
    ) -> CopyPreview:
        """What buying one Polymarket trader's buy would do now, on the same Polymarket international market
        and outcome: the price, the fee, what you'd pay, and what you'd get if it wins. Sends nothing.

            p = client.whales.preview_copy(trade, spend=50)   # $50, fee included
            p.contracts, p.price, p.fee, p.total, p.payout, p.profit_if_win

        Size it as ``size`` contracts or ``spend`` dollars (the most whole contracts whose cost plus fee
        fits in it; 5 contracts when you give neither). The amount is priced by walking Polymarket's order
        book up to their price plus ``max_slippage``, so ``price`` is what that many contracts really
        average. ``ok`` is False (with ``reason``) when nothing is for sale that close to their price, the
        book there holds less than the amount (``max_spend`` says what it holds), ``spend`` doesn't buy
        the market's smallest order, the market isn't taking orders, or it isn't a Polymarket buy. Paper
        prices, as :meth:`follow` with ``venue="polymarket"``.

        ``best_price`` is the cheapest ask on that book, before the fee, from the same read: on an ``ok``
        preview, compare it with ``price`` (your average) to see how much this amount moves the price.
        It's there when ``ok`` is False as well, and None only when the book has no asks or couldn't be
        read.
        """
        return self._one_trade_copier(trade, size, spend, max_slippage)._polymarket_plan(trade)

    def copy_trade(
        self,
        trade: WhaleTrade,
        *,
        size: float | None = None,
        spend: float | None = None,
        max_slippage: float = 0.03,
    ) -> CopyEvent:
        """Copy one Polymarket trader's buy now, however old it is: buy ``size`` contracts or ``spend``
        dollars' worth (fee included) on the same Polymarket international market and outcome, in paper
        mode, if the price is at most ``max_slippage`` above theirs (see :meth:`preview_copy`). Returns a
        ``CopyEvent`` (``copied``, or ``skipped`` with the reason); its ``order_id`` works with
        :meth:`copy_results`. Live mode raises.
        """
        return self._one_trade_copier(trade, size, spend, max_slippage)._copy_on_polymarket(trade)

    def _one_trade_copier(
        self, trade: WhaleTrade, size: float | None, spend: float | None, max_slippage: float
    ) -> Copier:
        if trade.action != "buy":
            raise ValueError("Only a buy can be copied: pass one of the trader's buys.")
        if size is not None and spend is not None:
            raise ValueError("pass size= or spend=, not both")
        if spend is not None and not (math.isfinite(spend) and spend > 0):
            raise ValueError(f"spend must be more than 0 dollars, not {spend!r}")
        who = Trader(venue=trade.venue, id=trade.trader or "", name=trade.name or "", volume_unit="usd")
        cp = self.follow(
            who,
            size=size if size is not None else 5,
            max_size=math.inf if spend is not None else max(100.0, size or 0),
            max_slippage=max_slippage,
            venue="polymarket",
            copy_sells=False,
        )
        cp.spend = spend
        return cp

    def copy_results(self, order_ids: list[str]) -> dict[str, CopyResult]:
        """How each copied buy (by ``CopyEvent.order_id``) is doing, keyed by order id.

        In paper mode it first pays out positions whose market has settled (``client.settle()``), so a
        bet turns ``won`` or ``lost`` when its market settles. An open bet is valued at the best bid of the
        venue's latest book, read once and not waited on (``client.pnl()`` waits for a fresh one, which
        on a quiet market can take a minute). Two copies on the same market share its settlement and bid.
        """
        if self._client is None:
            raise ValueError("copy_results() needs a client: use client.whales.copy_results(...)")
        c = self._client
        if c.mode != "live":
            c.settle()
            self._settle_polymarket()
        settled: dict[tuple[str, str, str], Any] = {(x.venue, x.market, x.side): x for x in c.settlements()}
        books: dict[tuple[str, str], Any] = {}  # one read per market: a book has both sides
        poly: dict[str, dict[str, Any]] | None = None  # Polymarket international markets, read once

        def bid_for(key: tuple[str, str, str]) -> float | None:
            nonlocal poly
            venue, market, side = key
            if venue == "polymarket":
                if poly is None:
                    wanted = {f.market for fs in fills.values() for f in fs if f.venue == "polymarket"}
                    try:
                        poly = self._poly_markets_now(m.partition(":")[0] for m in wanted)
                    except VenueError as e:
                        log.warning("uselayer: no Polymarket prices: %s", e)
                        poly = {}
                cid, _, outcome = market.partition(":")
                m = poly.get(cid)
                return None if m is None or m.get("closed") else _outcome_quote(m, int(outcome or 0))[0]
            if (venue, market) not in books:
                try:
                    books[(venue, market)] = c._venues[venue].read_book(market).book
                except (VenueError, KeyError) as e:
                    log.warning("uselayer: no bid for %s %s: %s", venue, market, e)
                    books[(venue, market)] = None
            book = books[(venue, market)]
            bid = book.outcome("yes" if side == "yes" else "no").best_bid if book is not None else None
            return bid.price if bid is not None else None

        fills: dict[str, list[Any]] = {}
        for f in c.fills():
            if f.action == "buy":
                fills.setdefault(f.order_id, []).append(f)
        out: dict[str, CopyResult] = {}
        for oid in order_ids:
            fs = fills.get(oid) or []
            n = sum(f.contracts for f in fs)
            cost = sum(f.cost for f in fs)
            fee = sum(f.fee for f in fs)
            first = fs[0] if fs else None
            key: tuple[str, str, str] = (first.venue, first.market, first.side) if first else ("", "", "")
            st = settled.get(key)
            status: Literal["open", "won", "lost", "void", "unfilled"]
            payout = mark = pnl = None
            if not n:
                status, pnl = "unfilled", 0.0
            elif st is not None:
                payout = st.payout
                status = "void" if st.outcome == "void" else "won" if st.payout > cost / n else "lost"
                pnl = round(st.payout * n - cost - fee, 4)
            else:
                status = "open"
                mark = bid_for(key)
                pnl = None if mark is None else round(mark * n - cost - fee, 4)
            out[oid] = CopyResult(
                order_id=oid,
                venue=key[0],
                market=key[1],
                side=key[2],
                contracts=n,
                avg_price=round(cost / n, 6) if n else None,
                cost=round(cost, 4),
                fees=round(fee, 4),
                status=status,
                payout=payout,
                mark=mark,
                pnl=pnl,
                settled_at=st.at if st is not None else None,
                simulated=c.mode != "live",
            )
        return out

    # -- helpers --

    def _poly_markets_now(self, condition_ids: Any) -> dict[str, dict[str, Any]]:
        """Gamma's markets by ``conditionId``, read now (prices and settlement change; ``MarketBook``
        keeps its first read). Open markets first, then closed ones for any not found."""
        want = list(dict.fromkeys(condition_ids))
        out: dict[str, dict[str, Any]] = {}
        for closed in (False, True):
            left = [c for c in want if c not in out]
            for i in range(0, len(left), 20):
                for m in self._poly.markets(left[i : i + 20], closed=closed):
                    if m.get("conditionId"):
                        out[str(m["conditionId"])] = m
        return out

    def _settle_polymarket(self) -> list[Any]:
        """Paper: pay out copies held on Polymarket international markets that Polymarket has resolved."""
        c = self._client
        if c is None or c.mode == "live":
            return []
        done = {s.market for s in c.settlements() if s.venue == "polymarket"}
        held = {f.market for f in c.fills() if f.venue == "polymarket"} - done
        if not held:
            return []
        try:
            markets = self._poly_markets_now(m.partition(":")[0] for m in held)
        except VenueError as e:
            log.warning("uselayer: can't check Polymarket settlements: %s", e)
            return []
        now = c._now()
        resolutions = []
        for key in sorted(held):
            cid, _, outcome = key.partition(":")
            m = markets.get(cid)
            paid = None if m is None else _polymarket_payout(m, int(outcome or 0))
            if paid is None:
                continue
            kind: Literal["yes", "no", "void"] = "yes" if paid == 1 else "no" if paid == 0 else "void"
            resolutions.append(
                Resolution(
                    venue="polymarket",
                    market=key,
                    outcome=kind,
                    payout=paid if kind == "void" else None,
                    as_of=now,
                )
            )
        return c.settle(resolutions) if resolutions else []

    def _kalshi_positions(self, holdings: list[dict[str, Any]]) -> list[WhalePosition]:
        """Kalshi groups holdings by event; each market's ``signed_open_position`` is contracts, YES when
        positive and NO when negative, and ``pnl`` is in ten-thousandths of a dollar. Kalshi gives no
        average or current price here."""
        out: list[WhalePosition] = []
        for event in holdings:
            for m in event.get("market_holdings") or []:
                ticker = str(m.get("market_ticker") or "")
                size = _f(m.get("signed_open_position_fp") or m.get("signed_open_position")) or 0.0
                if not ticker or not size:
                    continue
                text = self._titles.fill(
                    {"venue": "kalshi", "group_id": event.get("event_ticker"), "market_id": ticker}
                )
                pnl = _f(m.get("pnl"))
                out.append(
                    WhalePosition(
                        market=ticker,
                        title=text.get("question") or text.get("event"),
                        outcome=("YES" if size > 0 else "NO")
                        + (
                            f" · {text['outcome']}"
                            if text.get("outcome") and text["outcome"] != text.get("question")
                            else ""
                        ),
                        size=abs(size),
                        avg_price=None,
                        current_price=None,
                        value=None,
                        pnl=None if pnl is None else round(pnl / PNL_UNITS, 2),
                    )
                )
        return out

    def _position_now(self, t: WhaleTrade) -> float:
        """The trader's position now in ``t``'s market, in contracts: of ``t``'s outcome token on Polymarket,
        or signed on Kalshi (YES when positive, NO when negative, as Kalshi nets them). 0 when they hold none."""
        if t.venue == "kalshi":
            holdings, _ = self._kalshi.holdings(t.trader or "", 200)
            for event in holdings:
                for m in event.get("market_holdings") or []:
                    if m.get("market_ticker") == t.market:
                        return _f(m.get("signed_open_position_fp") or m.get("signed_open_position")) or 0.0
            return 0.0
        cid, _, outcome = t.market.partition(":")
        for p in self._poly.positions(t.trader or "", 50, market=cid):
            same = str(p.get("asset")) == t.token if t.token else str(p.get("outcomeIndex")) == outcome
            if p.get("conditionId") == cid and same:
                return _f(p.get("size")) or 0.0
        return 0.0

    def _poly_open(self, wallet: str, limit: int, market: str | None = None) -> list[WhalePosition]:
        """A Polymarket wallet's open bets, in ``market`` (a conditionId, or ``"cid:outcome"``) if given."""
        cid, _, outcome = (market or "").partition(":")
        # Open bets only: a settled market's position sits at 0 or 1 until it's redeemed.
        out = [
            self._poly_position(p)
            for p in self._poly.positions(wallet, limit, market=cid or None)
            if not p.get("redeemable") and 0 < (_f(p.get("curPrice")) or 0) < 1
        ]
        return [p for p in out if p.market == market] if outcome else out

    def _poly_position(self, p: dict[str, Any]) -> WhalePosition:
        return WhalePosition(
            market=f"{p.get('conditionId')}:{int(p.get('outcomeIndex') or 0)}",
            title=p.get("title"),
            outcome=p.get("outcome"),
            size=_f(p.get("size")) or 0.0,
            avg_price=_f(p.get("avgPrice")),
            current_price=_f(p.get("curPrice")),
            value=_f(p.get("currentValue")),
            pnl=_f(p.get("cashPnl")),
        )


@dataclass
class Copier:
    """Copies one trader's new trades onto your account. Paper by default, like the client."""

    whales: Whales
    client: Client
    trader: Trader
    size: float | None
    ratio: float | None
    max_size: float
    max_slippage: float
    venue: Literal["kalshi", "polymarket_us", "polymarket"]
    max_age_s: float
    min_usd: float
    categories: tuple[str, ...] = ()
    copy_sells: bool = True
    spend: float | None = None  # size one-trade copies (preview_copy / copy_trade) in dollars, fee included
    events: list[CopyEvent] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)
    _started: bool = False
    _rows: list[WhaleTrade] = field(default_factory=list)  # the last poll's trades, oldest first

    def _skip(self, t: WhaleTrade, reason: str, **kw: Any) -> CopyEvent:
        e = CopyEvent(at=self.client._now(), source=t, status="skipped", reason=reason, **kw)
        self.events.append(e)
        return e

    def poll(self) -> list[CopyEvent]:
        """Check for new trades once and copy them. The first call only remembers what's there."""
        rows = self.whales.trades(self.trader.venue, self.trader.id, limit=50)
        if not self._started:
            self._seen.update(t.trade_id or f"{t.market}{t.at}" for t in rows)
            self._started = True
            return []
        out: list[CopyEvent] = []
        self._rows = sorted(rows, key=lambda r: r.at)
        for t in self._rows:
            tid = t.trade_id or f"{t.market}{t.at}"
            if tid in self._seen:
                continue
            self._seen.add(tid)
            try:
                out.append(self._copy(t))
            except Exception as e:  # never lose a trade silently: say why it wasn't copied
                log.exception("uselayer: copying %s failed", tid)
                out.append(self._skip(t, f"Couldn't copy it: {e}"))
        return out

    def run(
        self,
        on_event: Callable[[CopyEvent], None] | None = None,
        *,
        every_s: float = 5,
        duration_s: float | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[CopyEvent]:
        """Poll every ``every_s`` seconds, yielding each event, until ``duration_s`` passes or ``stop()``."""
        end = None if duration_s is None else time.monotonic() + duration_s
        while True:
            try:
                events = self.poll()
            except VenueError as e:
                log.warning("uselayer: copier poll failed: %s", e)
                events = []
            for ev in events:
                if on_event:
                    on_event(ev)
                yield ev
            if (end is not None and time.monotonic() >= end) or (stop is not None and stop()):
                return
            time.sleep(every_s)

    def _target(self, t: WhaleTrade) -> tuple[str, str, Literal["yes", "no"]] | None:
        """``(venue, market, side)`` to place the copy on, or None if the bet has no twin."""
        if t.venue == "kalshi":
            if self.venue == "kalshi":
                return "kalshi", t.market, t.side
            slug = self.whales._twin("kalshi", t.market, "polymarket_us")
            return ("polymarket_us", slug, t.side) if slug else None
        twin = self.whales._poly_to_kalshi(t.market)
        if not twin:
            return None
        ticker, flip = twin
        side: Literal["yes", "no"] = "no" if flip else "yes"
        if self.venue == "kalshi":
            return "kalshi", ticker, side
        slug = self.whales._twin("kalshi", ticker, "polymarket_us")
        return ("polymarket_us", slug, side) if slug else None

    def _copy(self, t: WhaleTrade) -> CopyEvent:
        age = (self.client._now() - t.at).total_seconds()
        if age > self.max_age_s:
            return self._skip(t, f"Seen {int(age)} s after they traded, too late to copy.")
        if t.action == "buy" and t.usd < self.min_usd:
            return self._skip(t, f"Only ${t.usd:,.2f}, under your ${self.min_usd:,.0f} minimum.")
        if t.action == "sell" and not self.copy_sells:
            return self._skip(t, "They sold. You copy buys only and hold them until the market settles.")
        if self.categories and t.venue == "polymarket":
            cat = self.whales.category(t.market)
            if cat not in self.categories:
                return self._skip(
                    t, f"A {cat or 'uncategorised'} bet: you copy them only in {', '.join(self.categories)}."
                )
        if self.venue == "polymarket":
            return self._copy_on_polymarket(t)
        try:
            target = self._target(t)
        except VenueError as e:
            return self._skip(t, e.message)
        if target is None:
            return self._skip(t, "This bet has no matched market on the venue you copy to.")
        venue, market, side = target
        qty = self.size if self.size is not None else round(t.size * (self.ratio or 0), 2)
        qty = min(qty, self.max_size)
        action = t.action
        share: float | None = None
        if action == "sell":
            held = sum(
                p.contracts
                for p in self.client.positions()
                if getattr(p, "venue", None) == venue
                and getattr(p, "market", None) == market
                and getattr(p, "side", None) == side
            )
            if held <= 0:
                return self._skip(t, "They sold, and you don't hold this side.", venue=venue, market=market)
            try:
                share = self._sell_share(t)
            except VenueError as e:
                return self._skip(t, _no_share(e), venue=venue, market=market)
            qty = _sell_qty(held, share)
            if qty <= 0:
                return self._skip(t, _too_small(share, held), venue=venue, market=market, share=share)
        if qty <= 0:
            return self._skip(t, "Copy size rounds to zero.", venue=venue, market=market)
        limit = t.price + self.max_slippage if action == "buy" else t.price - self.max_slippage
        try:
            tick = self.client.market(market, venue=venue).tick_size or 0.01
        except VenueError as e:
            return self._skip(t, e.message, venue=venue, market=market)
        steps = math.floor(limit / tick + 1e-9) if action == "buy" else math.ceil(limit / tick - 1e-9)
        limit = round(min(max(steps * tick, tick), 1 - tick), 6)
        order = self.client.order(venue=venue, market=market, side=side, action=action, price=limit, size=qty)
        try:
            p = self.client.preview(order)
            if not p.allowed:
                why = p.blocked_by or "; ".join(p.problems) or "a rule said no"
                return self._skip(
                    t, f"Not sent: {why}.", venue=venue, market=market, side=side, price=limit, size=qty
                )
            if not p.est_fill.filled:
                return self._skip(
                    t,
                    f"Nothing to {action} at {limit:.2f} or better right now.",
                    venue=venue,
                    market=market,
                    side=side,
                    price=limit,
                    size=qty,
                )
            sent = self.client.send(order)
        except VenueError as e:
            return self._skip(t, e.message, venue=venue, market=market, side=side, price=limit, size=qty)
        done = CopyEvent(
            at=self.client._now(),
            source=t,
            status="copied",
            reason=f"{action.capitalize()} {sent.filled:g} of {qty:g} {side.upper()} at "
            + (f"up to {limit:.2f}" if action == "buy" else f"no less than {limit:.2f}")
            + (_as_they_sold(share) if share is not None else ""),
            venue=venue,
            market=market,
            side=side,
            action=action,
            price=limit,
            size=qty,
            filled=sent.filled,
            avg_price=sent.avg_price,
            fees=sent.fees,
            order_id=sent.id,
            simulated=self.client.mode != "live",
            share=share,
        )
        self.events.append(done)
        return done

    def _polymarket_plan(self, t: WhaleTrade) -> CopyPreview:
        """What copying ``t`` on its own Polymarket international market would do now; sends nothing."""
        where: dict[str, Any] = {"venue": "polymarket", "market": t.market, "side": t.side}

        spend = self.spend if t.action == "buy" else None

        def no(reason: str, **kw: Any) -> CopyPreview:
            return CopyPreview(trade=t, ok=False, reason=reason, **where, spend=spend, **kw)

        if t.venue != "polymarket":
            return no("A Kalshi bet: copies on Polymarket international follow Polymarket traders only.")
        c = self.client
        if c.store.killed():
            return no("Not sent: the kill switch is on.")
        cid, _, outcome = t.market.partition(":")
        try:
            m = self.whales._poly_markets_now([cid]).get(cid)
        except VenueError as e:
            return no(e.message)
        if m is None:
            return no("Polymarket doesn't list this market any more.")
        if m.get("closed") or not m.get("acceptingOrders"):
            return no("Polymarket isn't taking orders on this market now.")
        idx = _outcome_index(m, outcome, t.outcome, t.token)
        if idx is None:
            return no("Polymarket's trade doesn't say clearly which outcome they bought, so it isn't copied.")
        outcome, where["market"] = str(idx), f"{cid}:{idx}"
        qty = self.size if self.size is not None else round(t.size * (self.ratio or 0), 2)
        qty = min(qty, self.max_size)
        action = t.action
        smallest = _f(m.get("orderMinSize")) or 0
        if action == "sell":
            held = sum(
                p.contracts
                for p in c.positions()
                if (p.venue, p.market, p.side) == ("polymarket", where["market"], t.side)
            )
            if held <= 0:
                return no("They sold, and you don't hold this side.")
            try:
                where["share"] = share = self._sell_share(t)
            except VenueError as e:
                return no(_no_share(e))
            qty = _sell_qty(held, share)
            if qty <= 0:
                return no(_too_small(share, held))
        if spend is None and qty <= 0:
            return no("Copy size rounds to zero.")
        if spend is None and action == "buy" and qty < smallest:
            return no(f"Polymarket's smallest order on this market is {smallest:g} contracts.")
        tick = _f(m.get("orderPriceMinTickSize")) or 0.01
        limit = t.price + self.max_slippage if action == "buy" else t.price - self.max_slippage
        steps = math.floor(limit / tick + 1e-9) if action == "buy" else math.ceil(limit / tick - 1e-9)
        limit = round(min(max(steps * tick, tick), 1 - tick), 6)
        bid, ask = _outcome_quote(m, int(outcome or 0))
        asks: list[tuple[float, float]] | None = None
        token = _token_id(m, int(outcome or 0)) if action == "buy" else None
        if token is not None:
            try:
                asks = self.whales._book.asks(token)
            except VenueError:
                asks = None  # priced from Gamma's best price alone; depth_checked says so
        if asks is not None:
            return self._walk_plan(t, m, asks, qty, limit, spend, smallest, where, no)
        price = ask if action == "buy" else bid
        if price is None or (price > limit + 1e-9 if action == "buy" else price < limit - 1e-9):
            return no(
                f"Nothing to {action} at {limit:.2f} or better right now.",
                contracts=None if spend is not None else qty,
                limit=limit,
                price=price,
            )
        if spend is not None:  # the most whole contracts whose cost plus fee fits in spend, at this price
            each = price + taker_fee(m, price)
            qty = float(math.floor(spend / each + 1e-9))
            least = max(smallest, 1)
            if qty < least:
                return no(
                    f"${spend:,.2f} doesn't buy Polymarket's smallest order here: {least:g} contracts "
                    f"at {price:.2f} cost ${least * each:,.2f} with the fee.",
                    limit=limit,
                    price=price,
                )
        cost = round(price * qty, 6)
        fee = round(taker_fee(m, price) * qty, 6)
        total = round(cost + fee, 6) if action == "buy" else round(cost - fee, 6)
        return CopyPreview(
            trade=t,
            ok=True,
            reason=f"{'Buy' if action == 'buy' else 'Sell'} {qty:g} at {price:.2f} on Polymarket (limit {limit:.2f})",
            **where,
            spend=spend,
            action=action,
            contracts=qty,
            price=price,
            limit=limit,
            cost=cost,
            fee=fee,
            total=total,
            payout=qty if action == "buy" else None,
            profit_if_win=round(qty - total, 6) if action == "buy" else None,
        )

    def _walk_plan(
        self,
        t: WhaleTrade,
        m: dict[str, Any],
        asks: list[tuple[float, float]],
        qty: float,
        limit: float,
        spend: float | None,
        smallest: float,
        where: dict[str, Any],
        no: Callable[..., CopyPreview],
    ) -> CopyPreview:
        """A buy priced from the order book: only asks at ``limit`` or better count, walked level by level."""
        near = [(p, s) for p, s in asks if p <= limit + 1e-9]
        best = asks[0][0] if asks else None
        if not near:
            return no(
                f"Nothing to buy at {limit:.2f} or better right now.",
                contracts=None if spend is not None else qty,
                limit=limit,
                price=best,
                max_spend=0.0,
                depth_checked=True,
                best_price=best,
            )
        held = sum(s for _, s in near)
        cost_all, fee_all, _ = _walk(near, held, m)
        max_spend = round(cost_all + fee_all, 2)
        least = max(smallest, 1)
        if spend is not None:
            qty = float(math.floor(_affordable(near, spend, m) + 1e-9))
            short = qty >= math.floor(held + 1e-9) and spend > max_spend + 0.01
        else:
            short = qty > held + 1e-9
        if short:
            return no(
                f"Only ${max_spend:,.2f} is for sale at {limit:.2f} or better right now"
                f" ({held:,.0f} contracts), so this amount wouldn't fill near their price.",
                contracts=None if spend is not None else qty,
                limit=limit,
                price=best,
                max_spend=max_spend,
                depth_checked=True,
                best_price=best,
            )
        if qty < least:
            each = near[0][0] + taker_fee(m, near[0][0])
            what = f"${spend:,.2f} doesn't buy" if spend is not None else f"{qty:g} contracts is under"
            return no(
                f"{what} Polymarket's smallest order here: {least:g} contracts "
                f"at {near[0][0]:.2f} cost ${least * each:,.2f} with the fee.",
                limit=limit,
                price=best,
                max_spend=max_spend,
                depth_checked=True,
                best_price=best,
            )
        cost, fee, worst = _walk(near, qty, m)
        price = round(cost / qty, 6)
        total = round(cost + fee, 6)
        return CopyPreview(
            trade=t,
            ok=True,
            reason=f"Buy {qty:g} at {price:.3f} on average on Polymarket (limit {limit:.2f})",
            **where,
            spend=spend,
            action="buy",
            contracts=qty,
            price=price,
            limit=limit,
            cost=cost,
            fee=fee,
            total=total,
            payout=qty,
            profit_if_win=round(qty - total, 6),
            worst_price=worst,
            max_spend=max_spend,
            depth_checked=True,
            best_price=best,
        )

    def _copy_on_polymarket(self, t: WhaleTrade) -> CopyEvent:
        """Paper: fill the copy on the trader's own Polymarket international market and outcome, at the
        average price of walking its order book (Gamma's best price if the book can't be read), with
        Polymarket's taker fee (see :meth:`Whales.follow`)."""
        p = self._polymarket_plan(t)
        if not p.ok or p.price is None or p.fee is None or p.contracts is None or p.cost is None:
            kw = {"price": p.limit, "size": p.contracts} if p.limit is not None else {}
            return self._skip(t, p.reason, venue=p.venue, market=p.market, side=p.side, share=p.share, **kw)
        c = self.client
        now = c._now()
        oid = f"pm-paper-{uuid.uuid4().hex[:16]}"
        c.store.add_fill(
            SimulatedFill(
                mode="backtest" if c.mode == "backtest" else "paper",
                venue="polymarket",
                market=p.market,  # the outcome checked against the market, not the feed's raw index
                order_id=oid,
                side=t.side,
                action=p.action,
                price=p.price,
                contracts=p.contracts,
                role="taker",
                cost=p.cost,
                fee=p.fee,
                at=now,
                book_as_of=now,
            )
        )
        done = CopyEvent(
            at=now,
            source=t,
            status="copied",
            reason=f"{'Bought' if p.action == 'buy' else 'Sold'} {p.contracts:g} at {p.price:.2f} on Polymarket (limit {p.limit:.2f})"
            + (_as_they_sold(p.share) if p.share is not None else ""),
            venue="polymarket",
            market=p.market,
            side=t.side,
            action=p.action,
            price=p.limit,
            size=p.contracts,
            filled=p.contracts,
            avg_price=p.price,
            fees=p.fee,
            order_id=oid,
            simulated=True,
            share=p.share,
        )
        self.events.append(done)
        return done

    def _sell_share(self, t: WhaleTrade) -> float:
        """The part of their position ``t`` sold, from 0 to 1: its size over what they held just before it.

        That is their position now, read from the venue, with every later trade in the same position
        undone (they're in it already), and ``t`` itself put back. When the venue's position is ahead
        of its trade feed, what they held can come out at or under ``t``'s size: that counts as selling
        everything, so a full exit is never left half copied."""
        key = t.token or t.market
        p = self.whales._position_now(t)  # contracts of t's outcome on Polymarket; signed YES on Kalshi
        later = False
        for r in self._rows:
            if r is t:
                later = True
            elif later and (r.token or r.market) == key:
                p -= _position_change(r)
        p -= _position_change(t)  # before t
        before = p if t.venue == "polymarket" or t.side == "yes" else -p
        return 1.0 if before <= t.size + 1e-9 else t.size / before
