"""Finding Polymarket wallets, and where each one was seen. Nothing here is scored.

    for s in client.whales.find_wallets(per_second=4):   # every leaderboard, then markets of every size
        keep(s.wallet, s.source, s.where)

Two sources, read without a key:

- **Leaderboards.** Every one Polymarket publishes: profit and volume, over a day, a week, a month and all
  time, overall and in each category, each paged as deep as Polymarket goes (rank ~10,050). One read gives
  50 wallets.
- **Markets.** The last trades of open markets across the whole range of activity, not only the busiest:
  open markets are put in bands by dollars traded in the last day and the same number is picked at random
  from each band, so slow markets and the small accounts trading them are found too. One read gives up to
  500 trades.

A wallet seen in several places comes back once per place: keep them all to know where it was found. Hand
the wallets to :meth:`uselayer.whales.Whales.score` to judge them; finding one says nothing about its skill.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .errors import VenueError
from .http import _Bucket
from .venues.polymarket_data import CATEGORIES, ORDER_BY, PERIODS, PolymarketData
from .whale_scores import category_of

log = logging.getLogger(__name__)

LEADERBOARD_DEPTH = 10_000  # the last offset Polymarket pages a leaderboard to; past it the last page repeats
LEADERBOARD_PAGE = 50
# Bands of open markets by dollars traded in the last day, each band's floor: on 13 October 2026, 61 markets
# traded over $100k, 584 over $10k, 3,063 over $1k, 7,120 over $200 and 16,021 over $10 (of ~18,200 open).
MARKET_BANDS = (100_000.0, 10_000.0, 1_000.0, 200.0, 10.0)
MARKETS_PER_BAND = 60
MARKET_TRADES = 500  # each market's most recent trades read
# The leaderboard's categories as :func:`uselayer.whale_scores.category_of` names them (None: all markets).
BOARD_CATEGORY: dict[str, str | None] = {
    "OVERALL": None, "SPORTS": "Sports", "ESPORTS": "Esports", "CRYPTO": "Crypto", "POLITICS": "Politics",
    "ECONOMICS": "Economy", "FINANCE": "Economy", "CULTURE": "Culture", "MENTIONS": "Culture", "TECH": "Tech",
    "WEATHER": "Weather",
}  # fmt: skip

Board = tuple[str, str, str]  # (order, period, category): ("PNL", "MONTH", "POLITICS")


@dataclass(frozen=True)
class Sighting:
    """One place a wallet was seen.

    ``source`` is ``"leaderboard"`` or ``"market"``. ``where`` names the place: the board (``"PNL MONTH
    POLITICS"``) or the market's ``conditionId``. ``category`` is the board's or the market's, as
    :func:`~uselayer.whale_scores.category_of` names it (None for an overall board). On a board, ``rank``
    is their place on it; in a market, ``trades`` is how many of its last ``MARKET_TRADES`` trades were
    theirs and ``usd`` the dollars across them. ``name`` is their Polymarket username, None without one.
    """

    wallet: str
    source: Literal["leaderboard", "market"]
    where: str
    name: str | None = None
    category: str | None = None
    rank: int | None = None
    title: str | None = None
    trades: int = 0
    usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def all_boards() -> list[Board]:
    """Every leaderboard Polymarket publishes: each ordering, period and category."""
    return [(o, p, c) for c in CATEGORIES for p in PERIODS for o in ORDER_BY]


def _name(v: Any) -> str | None:
    s = str(v or "").strip()
    return None if not s or s.startswith("0x") else s  # no username: Polymarket sends the wallet


def _pace(poly: PolymarketData, per_second: float | None) -> Callable[[], None]:
    """Waits before each read so the finder keeps to ``per_second`` on top of the per-host limits, leaving the
    rest of each host's allowance to whatever else the client is reading."""
    if not per_second:
        return lambda: None
    http = poly._http
    return _Bucket(
        per_second, getattr(http, "_clock", time.monotonic), getattr(http, "_sleep", time.sleep)
    ).take


def leaderboard_wallets(
    poly: PolymarketData,
    boards: Iterable[Board] | None = None,
    *,
    depth: int = LEADERBOARD_DEPTH,
    per_second: float | None = None,
) -> Iterator[Sighting]:
    """Every wallet on ``boards`` (default: :func:`all_boards`), down to ``depth`` places, one
    :class:`Sighting` per wallet per board. A board that can't be read is skipped with a log line."""
    pace = _pace(poly, per_second)
    for order, period, cat in all_boards() if boards is None else boards:
        where = f"{order} {period} {cat}"
        for offset in range(0, depth + 1, LEADERBOARD_PAGE):
            pace()
            try:
                rows = poly.leaderboard(
                    order, period, LEADERBOARD_PAGE, category=None if cat == "OVERALL" else cat, offset=offset
                )
            except VenueError as e:
                log.warning("uselayer: leaderboard %s failed at %d: %s", where, offset, e)
                break
            for r in rows:
                w = str(r.get("proxyWallet") or "").lower()
                if w:
                    rank = str(r.get("rank") or "")
                    yield Sighting(
                        w, "leaderboard", where, name=_name(r.get("userName")), category=BOARD_CATEGORY.get(cat),
                        rank=int(rank) if rank.isdigit() else None,
                    )  # fmt: skip
            if len(rows) < LEADERBOARD_PAGE:
                break


def pick_markets(
    poly: PolymarketData,
    *,
    bands: tuple[float, ...] = MARKET_BANDS,
    per_band: int = MARKETS_PER_BAND,
    rng: random.Random | None = None,
    pace: Callable[[], None] = lambda: None,
) -> list[dict[str, Any]]:
    """Open markets, ``per_band`` picked at random from each band of ``bands`` (dollars traded in the last
    day, each band's floor, highest first), busiest band first. Reads the list of open markets down to the
    lowest floor; if that list stops early, picks from what was read."""
    rng = rng or random.Random()
    floors = sorted(bands, reverse=True)
    banded: list[list[dict[str, Any]]] = [[] for _ in floors]
    after: str | None = None
    while True:
        pace()
        try:
            rows, after = poly.open_markets(after)
        except VenueError as e:
            log.warning("uselayer: couldn't list open Polymarket markets: %s", e)
            break
        lowest = None
        for m in rows:
            vol = float(m.get("volume24hr") or 0)
            lowest = vol
            band = next((i for i, f in enumerate(floors) if vol >= f), None)
            if band is not None and m.get("conditionId"):
                banded[band].append(m)
        if not after or not rows or (lowest is not None and lowest < floors[-1]):
            break
    return [m for ms in banded for m in rng.sample(ms, min(per_band, len(ms)))]


def market_wallets(
    poly: PolymarketData,
    *,
    bands: tuple[float, ...] = MARKET_BANDS,
    per_band: int = MARKETS_PER_BAND,
    trades: int = MARKET_TRADES,
    rng: random.Random | None = None,
    per_second: float | None = None,
) -> Iterator[Sighting]:
    """Every wallet in the last ``trades`` trades of markets from :func:`pick_markets`, one :class:`Sighting`
    per wallet per market. A market whose trades can't be read is skipped."""
    pace = _pace(poly, per_second)
    for m in pick_markets(poly, bands=bands, per_band=per_band, rng=rng, pace=pace):
        cid = str(m["conditionId"])
        pace()
        try:
            rows = poly.market_trades(cid, limit=trades)
        except VenueError as e:
            log.warning("uselayer: couldn't read trades in %s: %s", cid, e)
            continue
        seen: dict[str, list[Any]] = {}  # wallet: [name, trades, usd]
        for r in rows:
            w = str(r.get("proxyWallet") or "").lower()
            if not w:
                continue
            s = seen.setdefault(w, [None, 0, 0.0])
            s[0] = s[0] or _name(r.get("name"))
            s[1] += 1
            s[2] += float(r.get("size") or 0) * float(r.get("price") or 0)
        cat, title = category_of(m), m.get("question")
        for w, (name, n, usd) in seen.items():
            yield Sighting(
                w, "market", cid, name=name, category=cat, title=title, trades=n, usd=round(usd, 2)
            )
