"""Where a resting paper order stands in line at its price, and what fills it.

A real resting order fills only after everyone ahead of it at its price. Venues publish the total size
at each price, not single orders, so paper and backtest mode estimate the line:

- When the order starts resting, everything already at its price is ahead of it.
- A trade at its price that hits its side (a sell into the bids, for a resting buy) takes from the
  front of the line first. Only what's left of the trade fills the order.
- A trade through its price (a sell below a resting buy) means nobody was left at the price, so the
  order fills, up to the trade's size.
- A book whose other side reaches the price (an ask at or below a resting buy) means the line is gone
  too. The order fills up to the size offered there, and the same contracts never fill it twice.
- When a level shrinks by more than the trades at that price explain, the rest were cancels.
  ``cancels="proportional"`` (the default) spreads them through the line, so the part ahead of the
  order shrinks by its share. ``cancels="behind"`` (the worst case) puts them all behind the order.
- Venues can send the book change before the trade that caused it. A trade at the price within
  :data:`BOOK_LEADS_TRADE_S` of such a drop is matched to it first: those contracts were traded from
  the front of the line, not cancelled. The fill is the same whichever message arrives first.

With books alone and no trades, a resting order fills only when the book crosses its price. Sizes are
in millionths of a contract, like the rest of the fill model.

    line = start(order, book, taken)                  # line.ahead_est: the estimated line ahead
    filled = on_trade(order, line, trade, last_book)   # millionths of a contract
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from .books import Book, Level
from .events import TradePrint
from .fees import MICRO, js_round, to_micro
from .orders import Order

Cancels = Literal["proportional", "behind"]
CANCELS: tuple[Cancels, ...] = ("proportional", "behind")

_KEEP_TRADE_IDS = 500

BOOK_LEADS_TRADE_S = 0.5
"""How long a drop at the order's price waits for the trade that explains it. Venues can send the
book change before the trade message; matching them stops one trade from moving the line twice. On
polymarket.com (497 trades, 2026-10-04) the trade came a median 26 ms after its change, 206 ms at the
99th percentile. A drop no trade explains in this window stays counted as cancels."""


@dataclass
class Line:
    """One resting order's estimated place in line, in millionths of a contract.

    ``ahead_est``: the estimated contracts in front of the order at its price (venues show totals per
    price, not single orders, so this is never exact). ``level``: the size at its price when the
    last book was seen. ``traded``: contracts traded at its price, on its side, since then. ``drop``:
    contracts a book showed leaving the price that no trade has explained yet, ``drop_cut`` how far the
    line moved for them, and ``drop_at`` when (epoch seconds). ``used``: contracts on the other side, by
    price, the order already filled against. ``since``: when the order reached the book (epoch seconds;
    set with an order latency): books and trades stamped earlier don't touch it.
    """

    ahead_est: int
    level: int
    traded: int = 0
    drop: int = 0
    drop_cut: int = 0
    drop_at: float | None = None
    used: dict[int, int] = field(default_factory=dict)
    trade_ids: list[str] = field(default_factory=list)
    since: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "ahead_est": self.ahead_est,
                "level": self.level,
                "traded": self.traded,
                "drop": self.drop,
                "drop_cut": self.drop_cut,
                "drop_at": self.drop_at,
                "used": {str(k): v for k, v in self.used.items()},
                "trade_ids": self.trade_ids,
                "since": self.since,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> Line:
        d = json.loads(s)
        return cls(
            ahead_est=d["ahead_est"],
            level=d["level"],
            traded=d.get("traded", 0),
            drop=d.get("drop", 0),
            drop_cut=d.get("drop_cut", 0),
            drop_at=d.get("drop_at"),
            used={int(k): v for k, v in d.get("used", {}).items()},
            trade_ids=list(d.get("trade_ids", [])),
            since=d.get("since"),
        )


def _units(size: float) -> int:
    return js_round(size * MICRO)


def _own_side(order: Order, book: Book) -> tuple[Level, ...]:
    ob = book.outcome(order.side)
    return ob.bids if order.action == "buy" else ob.asks


def _other_side(order: Order, book: Book) -> tuple[Level, ...]:
    ob = book.outcome(order.side)
    return ob.asks if order.action == "buy" else ob.bids


def _reaches(order: Order, price: int) -> bool:
    """Whether a level on the other side at ``price`` reaches the order (and so would fill it)."""
    p = to_micro(order.price)
    return price <= p if order.action == "buy" else price >= p


def size_at_price(order: Order, book: Book) -> int:
    """Contracts on the order's own side at its price: the line it would join."""
    p = to_micro(order.price)
    return sum(_units(lv.size) for lv in _own_side(order, book) if to_micro(lv.price) == p)


def start(order: Order, book: Book, taken: dict[int, int] | None = None) -> Line:
    """The line for an order that starts resting against ``book``: everything at its price is ahead.

    ``taken`` is what the order already took as a taker, by price (millionths of a contract), so a
    later book showing the same contracts doesn't fill it again.
    """
    n = size_at_price(order, book)
    return Line(ahead_est=n, level=n, used=dict(taken or {}))


def on_book(order: Order, line: Line, book: Book, *, cancels: Cancels = "proportional") -> int:
    """Move the line to a newer book. Returns how many millionths of a contract the book fills.

    Updates ``line`` in place.
    """
    # The order's own price level: what left it that trades don't explain was cancelled, unless
    # the trades are still on their way (a venue can send the book change before its trade).
    if line.since is not None and book.as_of.timestamp() < line.since:
        return 0  # from before the order reached the venue
    now = size_at_price(order, book)
    gone = line.level - now
    before = line.ahead_est
    unexplained = max(0, gone - line.traded)
    if unexplained and cancels == "proportional":
        rest = line.level - line.traded  # the level after the trades, before the cancels
        if rest > 0 and line.ahead_est > 0:
            line.ahead_est -= min(line.ahead_est, unexplained * line.ahead_est // rest)
    line.ahead_est = min(line.ahead_est, now)
    if unexplained:
        at = book.as_of.timestamp()
        if not _recent(line, at):
            line.drop = line.drop_cut = 0
        line.drop += unexplained
        line.drop_cut += min(before - line.ahead_est, unexplained)  # how far the line moved for it
        line.drop_at = at
    line.level = now
    line.traded = 0

    # The other side reaching the price: the line is gone, and the order fills from what's new there.
    reaching = [
        (to_micro(lv.price), _units(lv.size))
        for lv in _other_side(order, book)
        if _reaches(order, to_micro(lv.price))
    ]
    present = {p for p, _ in reaching}
    for p in list(line.used):
        if p not in present:
            del line.used[p]  # the level emptied: whatever is there later is new
    fresh = [(p, n - line.used.get(p, 0)) for p, n in reaching if n > line.used.get(p, 0)]
    if not fresh:
        return 0
    line.ahead_est = 0
    want = _units(order.remaining)
    filled = 0
    for p, n in fresh:
        if filled == want:
            break
        take = min(n, want - filled)
        line.used[p] = line.used.get(p, 0) + take
        filled += take
    return filled


def _recent(line: Line, at: float) -> bool:
    return line.drop > 0 and line.drop_at is not None and abs(at - line.drop_at) <= BOOK_LEADS_TRADE_S


def _aggressor(trade: TradePrint, order: Order, last_book: Book | None) -> Literal["buy", "sell"] | None:
    """What the taker did to the order's side. When the venue didn't say, judged from the book before."""
    a = trade.aggressor
    if a is not None:
        return a if order.side == "yes" else ("sell" if a == "buy" else "buy")
    if last_book is None:
        return None
    ob = last_book.outcome(order.side)
    t = _trade_price(trade, order)
    if ob.best_ask is not None and t >= to_micro(ob.best_ask.price):
        return "buy"
    if ob.best_bid is not None and t <= to_micro(ob.best_bid.price):
        return "sell"
    return None  # inside the spread: can't tell which side it hit, so it fills nothing


def _trade_price(trade: TradePrint, order: Order) -> int:
    p = to_micro(trade.price)
    return p if order.side == "yes" else MICRO - p


def on_trade(order: Order, line: Line, trade: TradePrint, last_book: Book | None) -> int:
    """Apply a trade the venue printed. Returns how many millionths of a contract it fills.

    Updates ``line`` in place. A trade id seen before for this order is ignored.
    """
    if line.since is not None and trade.as_of.timestamp() < line.since:
        return 0  # printed before the order reached the venue
    if trade.trade_id is not None:
        if trade.trade_id in line.trade_ids:
            return 0
        line.trade_ids.append(trade.trade_id)
        del line.trade_ids[:-_KEEP_TRADE_IDS]
    agg = _aggressor(trade, order, last_book)
    if agg is None or agg == order.action:
        return 0  # it hit the other side, or which side is unknown
    t = _trade_price(trade, order)
    p = to_micro(order.price)
    size = _units(trade.size)
    want = _units(order.remaining)
    through = t < p if order.action == "buy" else t > p
    if through:
        line.ahead_est = 0
        return min(want, size)
    if t != p:
        return 0  # it hit better-priced orders, which come before this one
    filled = 0
    if _recent(line, trade.as_of.timestamp()):
        # A book already showed these contracts leaving the price, and moved the line for them as
        # cancels. They were this trade, so put the line back where it stood and let them take it
        # from the front, as if the trade had come first: same fill in either order.
        seen = min(size, line.drop)
        cut = line.drop_cut * seen // line.drop
        ahead_before = line.ahead_est + cut
        filled = min(want, max(0, seen - ahead_before))
        line.ahead_est = max(0, ahead_before - seen)
        line.drop -= seen
        line.drop_cut -= cut
        size -= seen
    if size == 0:
        return filled
    line.traded += size
    more = min(want - filled, max(0, size - line.ahead_est))
    line.ahead_est = max(0, line.ahead_est - size)
    return filled + more
