"""Proof for roadmap 11.9 + 18.7: resting orders on recorded data fill on trades, behind an estimated line.

Takes a recording of polymarket.com's public market stream (one ``{"received_at", "message"}`` line per
message) and its ``{token: [market, side]}`` map, then backtests the same strategy three ways:

- the 0.2.0/0.3.0 fill model, rebuilt here: a resting order fills from any later book that reaches
  its price, up to the size shown there, every time, with no line ahead and no trades;
- this release with ``queue_cancels="proportional"`` (the default);
- this release with ``queue_cancels="behind"`` (the worst case).

The strategy: on each market's first book, and again every 2 minutes, rest a 50-contract buy of YES at
the best bid (joining the back of the line) for 5 minutes.

    uv run python scripts/prove_resting_fills.py ticks.jsonl ticks.jsonl.tokens.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, ClassVar

from uselayer import Book, Client, VenueError
from uselayer.fees import MICRO, js_round
from uselayer.fill import FeeSettings, _crossing
from uselayer.imports import import_events
from uselayer.paper import PaperVenue
from uselayer.resting import Line


class Counting(PaperVenue):
    """This release's fill model, counting how many contracts trades and books filled."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.by: Counter[str] = Counter()
        self.src = ""

    def on_book(self, *a: Any, **kw: Any) -> list[Any]:
        self.src = "books"
        return super().on_book(*a, **kw)

    def on_trade(self, trade: Any, *a: Any, **kw: Any) -> list[Any]:
        self.src = "trades"
        self.trade = trade
        return super().on_trade(trade, *a, **kw)

    def _maker(self, o: Any, units: int, *a: Any, **kw: Any) -> Any:
        n = min(units, js_round(o.remaining * MICRO)) / MICRO
        self.by[self.src] += n
        if self.src == "trades":
            t = self.trade
            self.log.append(
                f"{t.as_of:%H:%M:%S.%f} {o.market[:24]:24} resting buy {o.size:g}@{o.price} "
                f"(placed {o.created_at:%H:%M:%S}) ← {t.aggressor} {t.size:g}@{t.price}: filled {n:g}"
            )
        return super()._maker(o, units, *a, **kw)

    log: ClassVar[list[str]] = []


class OldModel(Counting):
    """The fill model before 11.9/18.7: a later book reaching the price fills, from what it shows."""

    def on_book(self, book: Book, settings_for: dict[str, FeeSettings], *, at: datetime) -> list[Any]:
        self.src = "books"
        changed = []
        for o in self.store.orders(open_only=True):
            if o.venue != book.venue or o.market != book.market:
                continue
            gone = self._expired(o, at)
            if gone is not None:
                changed.append(gone)
                continue
            units = sum(js_round(lv.size * MICRO) for lv in _crossing(o, book))
            if units:
                settings = settings_for.get(o.market, FeeSettings(venue=o.venue))
                changed.append(self._maker(o, units, settings, at=at, as_of=book.as_of))
        return changed

    def on_trade(self, *a: Any, **kw: Any) -> list[Any]:
        return []  # trades were only counted

    def _line(self, o: Any) -> Line | None:
        return None


def run(events: Any, *, cancels: str, old: bool = False) -> dict[str, Any]:
    last: dict[str, datetime] = {}
    blocked: Counter[str] = Counter()

    def strategy(c: Client, b: Book) -> None:
        ob = b.outcome("yes")
        if ob.best_bid is None or ob.best_ask is None:
            return
        if b.market in last and b.as_of - last[b.market] < timedelta(minutes=2):
            return
        last[b.market] = b.as_of
        try:
            c.buy(
                venue="polymarket",
                market=b.market,
                side="yes",
                price=ob.best_bid.price,
                size=50,
                tif="gtc",
                post_only=True,
                expires_at=b.as_of + timedelta(minutes=5),
            )
        except VenueError as e:
            blocked[e.rule or e.code] += 1

    bt = Client(mode="backtest", books=events, queue_cancels=cancels, rules={"order_ttl_s": 600})  # type: ignore[arg-type]
    # The recording carries no fee settings; every market here is sports. Fees don't change what fills.
    bt._settings = lambda venue, market: FeeSettings(venue=venue, category="sports")  # type: ignore[method-assign]
    model = (OldModel if old else Counting)(bt._paper.store, "backtest", cancels=cancels)  # type: ignore[arg-type]
    bt._paper = model
    out = bt.replay(strategy)
    orders = bt.orders(open=False)
    fills = bt.fills()
    return {
        "orders": len(orders),
        "contracts_asked": sum(o.size for o in orders),
        "contracts_filled": round(sum(o.filled for o in orders), 2),
        "orders_any_fill": sum(1 for o in orders if o.filled),
        "orders_full_fill": sum(1 for o in orders if o.status == "filled"),
        "fills": len(fills),
        "filled_by_trades": round(model.by["trades"], 2),
        "filled_by_books": round(model.by["books"], 2),
        "statuses": dict(Counter(o.status for o in orders)),
        "blocked_by_rules": dict(blocked),
        "replay": {k: out[k] for k in ("books", "trades", "gaps")},
    }


if __name__ == "__main__":
    path, tokens_path = sys.argv[1], sys.argv[2]
    with open(tokens_path) as f:
        tokens = {k: tuple(v) for k, v in json.load(f).items()}
    data = import_events(path, format="polymarket", tokens=tokens, strict=False)
    events = list(data)
    print("import:", data.report.to_dict() if hasattr(data.report, "to_dict") else data.report)
    print("events:", dict(Counter(e.kind for e in events)))
    # Live-venue market info isn't needed: every market is replayed from the file.
    for name, kw in (
        ("0.3.0 model (books only, no line)", {"cancels": "proportional", "old": True}),
        ("new: proportional cancels (default)", {"cancels": "proportional"}),
        ("new: all cancels behind (worst case)", {"cancels": "behind"}),
    ):
        Counting.log = []
        print(name, json.dumps(run(events, **kw)))
        for line in Counting.log:
            print("   trade fill:", line)
