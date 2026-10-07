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

import logging
import math
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from .errors import VenueError
from .http import Http
from .layer_api import LayerApi
from .titles import Titles
from .venues.kalshi_social import PNL_UNITS, KalshiSocial
from .venues.polymarket_data import PolymarketData

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
    ``volume`` is contracts on Kalshi and dollars on Polymarket (``volume_unit`` says which).
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
class TraderDetail:
    """A trader's page: stats, open positions and recent trades.

    ``visibility`` is ``"hidden"`` when a Kalshi trader hides their trades and holdings: ``positions``
    and ``trades`` are then empty, which is not the same as having none.
    """

    trader: Trader
    visibility: str
    stats: dict[str, Any]
    positions: list[WhalePosition]
    trades: list[WhaleTrade]
    bio: str | None = None
    joined: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trader": self.trader.to_dict(),
            "visibility": self.visibility,
            "stats": self.stats,
            "positions": [p.to_dict() for p in self.positions],
            "trades": [t.to_dict() for t in self.trades],
            "bio": self.bio,
            "joined": self.joined,
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
    plain sentence. ``simulated`` is True in paper mode: no venue saw the order.
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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        d["source"] = self.source.to_dict()
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
    )


# ---- the API ----


class Whales:
    """Top traders, their trades, cross-venue links and copy trading. Use ``client.whales``."""

    def __init__(self, http: Http, layer: LayerApi, client: Client | None = None) -> None:
        self._kalshi = KalshiSocial(http)
        self._poly = PolymarketData(http)
        self._layer = layer
        self._client = client
        self._titles = Titles(http)
        self._twins: dict[tuple[str, str, str], str | None] = {}

    # -- traders --

    def top(
        self, venue: WhaleVenue | Literal["all"] = "all", *, by: str = "pnl", limit: int = 25
    ) -> list[Trader]:
        """The top traders by all-time profit (``by="pnl"``) or volume (``by="volume"``).

        With ``venue="all"``, both venues' lists merged by profit (volume isn't comparable: Kalshi counts
        contracts, Polymarket dollars), each trader keeping their own venue's ``rank``.
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
                        volume_unit="usd",
                        x_handle=r.get("xUsername") or None,
                        url=POLYMARKET_PROFILE + w,
                    )
                )
        if venue == "all":
            key = (lambda t: t.pnl or 0.0) if by == "pnl" else (lambda t: t.rank or 10**9)
            out.sort(key=key, reverse=by == "pnl")
        return out[: limit * (2 if venue == "all" else 1)]

    def trader(self, venue: WhaleVenue, id: str, *, trades: int = 50, positions: int = 50) -> TraderDetail:
        """One trader's page: stats, open positions and recent trades (newest first)."""
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
            stats = {
                "pnl": _f(pnl.get("economic_pnl") if pnl else None),
                "volume_usd": _f(pnl.get("volume")) if pnl else None,
                "trades": s.get("trades"),
                "biggest_win": _f(s.get("biggest_win")),
                "fees_paid": _f(pnl.get("fees_paid")) if pnl else None,
            }
            w = str(prof.get("proxyWallet") or id)
            return TraderDetail(
                trader=Trader(
                    venue="polymarket",
                    id=w,
                    name=prof.get("name") or prof.get("pseudonym") or _short_wallet(w),
                    pnl=stats["pnl"],
                    volume=stats["volume_usd"],
                    volume_unit="usd",
                    x_handle=prof.get("xUsername") or None,
                    url=POLYMARKET_PROFILE + w,
                ),
                visibility="visible",
                stats=stats,
                # Open bets only: a settled market's position sits at 0 or 1 until it's redeemed.
                positions=[
                    self._poly_position(p)
                    for p in self._poly.positions(w, positions)
                    if not p.get("redeemable") and 0 < (_f(p.get("curPrice")) or 0) < 1
                ],
                trades=[_polymarket_trade(t) for t in self._poly.trades(w, trades)],
                bio=prof.get("bio") or None,
                joined=prof.get("createdAt"),
            )
        raise ValueError('venue must be "kalshi" or "polymarket"')

    def trades(self, venue: WhaleVenue, id: str, *, limit: int = 50) -> list[WhaleTrade]:
        """A trader's recent trades, newest first. Empty for a Kalshi trader who hides them."""
        if venue == "kalshi":
            rows, _, _ = self._kalshi.trades(id, page_size=limit)
            return [_kalshi_trade(t, id, None) for t in rows]
        return [_polymarket_trade(t) for t in self._poly.trades(id, limit)]

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
                            volume=_f((pnl or {}).get("volume")),
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

    # -- copy trading --

    def follow(
        self,
        trader: Trader,
        *,
        size: float | None = None,
        ratio: float | None = None,
        max_size: float = 100,
        max_slippage: float = 0.03,
        venue: Literal["kalshi", "polymarket_us"] = "kalshi",
        max_age_s: float = 300,
        min_usd: float = 0,
    ) -> Copier:
        """A copier for ``trader``'s new trades. Nothing is copied until you call ``poll()`` or ``run()``.

        Size each copy as ``size`` contracts, or ``ratio`` of theirs, never more than ``max_size``.
        The limit price is their price plus ``max_slippage`` (dollars); an order that can't fill there
        isn't sent. Kalshi traders are copied on the same Kalshi market; Polymarket traders on the twin
        market on ``venue`` (needs a Layer key). Trades older than ``max_age_s`` when first seen, and
        every trade before you start following, are never copied.
        """
        if self._client is None:
            raise ValueError("follow() needs a client: use client.whales.follow(...)")
        if (size is None) == (ratio is None):
            raise ValueError("pass exactly one of size= or ratio=")
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
        )

    # -- helpers --

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
    venue: Literal["kalshi", "polymarket_us"]
    max_age_s: float
    min_usd: float
    events: list[CopyEvent] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)
    _started: bool = False

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
        for t in sorted(rows, key=lambda r: r.at):
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
        if t.usd < self.min_usd:
            return self._skip(t, f"Only ${t.usd:,.2f}, under your ${self.min_usd:,.0f} minimum.")
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
            qty = min(qty, held)
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
            reason=f"{action.capitalize()} {sent.filled:g} of {qty:g} {side.upper()} at up to {limit:.2f}",
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
        )
        self.events.append(done)
        return done
