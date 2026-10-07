"""Paper trading: orders fill against real books through Layer's fill model; nothing reaches a venue.

An order that crosses the book fills at once, level by level, as a taker, at the venue's real fee. A
``gtc`` remainder rests in the local store, behind everything already at its price, and fills (as a
maker, at its own price) once trades or a later book get through that line (see
:mod:`uselayer.resting`), or expires at ``expires_at``. Fills are
:class:`~uselayer.events.SimulatedFill`, kept in the paper (or backtest) store, apart from live.

When a market settles, each position in it pays what the venue paid (see
:class:`~uselayer.events.Resolution`) and closes, as a :class:`~uselayer.events.SimulatedSettlement`,
and resting orders on it are canceled.
"""

from __future__ import annotations

from datetime import datetime

from . import resting
from .books import Book
from .errors import VenueError
from .events import Resolution, SimulatedFill, SimulatedSettlement, TradePrint
from .fees import MICRO, js_round, to_micro
from .fill import FeeSettings, FillEstimate, estimate_fill, maker_fill
from .orders import Order
from .portfolio import build, held
from .resting import Cancels, Line
from .store import Store


def _ticks_ok(price: float, tick: float) -> bool:
    units = round(price * 1_000_000)
    step = round(tick * 1_000_000)
    return step > 0 and units % step == 0


def check_order_against_market(order: Order, *, tick: float, min_size: float, held_now: float) -> None:
    """The venue's own limits, checked before anything is filled or sent. Never rounds a price."""
    if not _ticks_ok(order.price, tick):
        raise VenueError(
            "invalid_order",
            f"Price {order.price} isn't on this market's tick size of {tick}.",
            venue=order.venue,
            retryable=False,
            hint=f"Use a price that's a multiple of {tick}. The SDK never rounds a price for you.",
            next="Send the order again with a price on the tick.",
        )
    if order.size < min_size - 1e-9:
        raise VenueError(
            "invalid_order",
            f"Size {order.size} is below this market's minimum of {min_size}.",
            venue=order.venue,
            retryable=False,
            hint=f"Send at least {min_size} contracts.",
            next="Send the order again with a larger size.",
        )
    if order.action == "sell" and order.size > held_now + 1e-9:
        raise VenueError(
            "invalid_order",
            f"Selling {order.size} but only {held_now} are held.",
            venue=order.venue,
            retryable=False,
            hint="Sell at most what you hold. To bet the other way, buy the other side.",
            next="client.positions()",
        )


class PaperVenue:
    """The paper (and backtest) order book keeper for one client.

    ``cancels`` says where cancels at a resting order's price come from (see :mod:`uselayer.resting`).
    """

    def __init__(self, store: Store, mode: str, *, cancels: Cancels = "proportional") -> None:
        self.store = store
        self.mode = mode
        self.cancels = cancels
        self.last_books: dict[tuple[str, str], Book] = {}

    def _record(self, order: Order, est: FillEstimate, at: datetime) -> None:
        for f in est.fills:
            self.store.add_fill(
                SimulatedFill(
                    mode=self.mode,
                    venue=order.venue,
                    market=order.market,
                    order_id=order.id or order.client_id,
                    side=order.side,
                    action=order.action,
                    price=f.price,
                    contracts=f.contracts,
                    role=f.role,
                    cost=f.cost,
                    fee=f.fee,
                    at=at,
                    book_as_of=est.book_as_of,
                    group_id=order.group_id,
                )
            )

    def _apply(self, order: Order, est: FillEstimate, at: datetime) -> Order:
        if est.filled:
            prev_cost = (order.avg_price or 0.0) * order.filled
            filled = round(order.filled + est.filled, 6)
            avg = round((prev_cost + est.cost) / filled, 6)
            order = order.model_copy(
                update={"filled": filled, "avg_price": avg, "fees": round((order.fees or 0.0) + est.fees, 6)}
            )
        return order.model_copy(update={"updated_at": at})

    def submit(
        self,
        order: Order,
        book: Book,
        settings: FeeSettings,
        *,
        at: datetime,
        tick: float,
        min_size: float,
        sent_at: datetime | None = None,
    ) -> Order:
        """Fill what crosses now; rest a ``gtc`` remainder; cancel the rest of an ``ioc`` / ``fok``.

        ``at`` is when the order reaches ``book``; ``sent_at`` (default ``at``) when it was sent. With
        an order latency they differ: books and trades before ``at`` never touch it.
        """
        check_order_against_market(
            order,
            tick=tick,
            min_size=min_size,
            held_now=held(
                self.store.fills(), order.venue, order.market, order.side, self.store.settlements()
            ),
        )
        order = order.model_copy(
            update={
                "id": order.client_id,
                "mode": self.mode,
                "status": "pending",
                "created_at": sent_at or at,
                "updated_at": at,
            }
        )
        est = estimate_fill(order, book, settings, at=at)
        if order.post_only and est.would_cross:
            order = order.model_copy(update={"status": "rejected"})
            self.store.save_order(order)
            return order
        self._record(order, est, at)
        order = self._apply(order, est, at)
        if order.remaining <= 1e-9:
            status = "filled"
        elif order.tif == "gtc":
            status = "open"
        else:
            status = "canceled"
        order = order.model_copy(update={"status": status})
        self.store.save_order(order)
        if status == "open":
            # What it just took as a taker can't fill it again when a later book still shows it.
            taken: dict[int, int] = {}
            for f in est.fills:
                p = to_micro(f.price)
                taken[p] = taken.get(p, 0) + js_round(f.contracts * MICRO)
            line = resting.start(order, book, taken)
            if sent_at is not None and at > sent_at:
                line.since = at.timestamp()  # it reached the book then: nothing earlier touches it
            self.store.save_line(order.client_id, line.to_json())
        return order

    def _line(self, o: Order) -> Line | None:
        body = self.store.line(o.id or o.client_id)
        return None if body is None else Line.from_json(body)

    def _expired(self, o: Order, at: datetime) -> Order | None:
        if o.expires_at is None or at < o.expires_at:
            return None
        o = o.model_copy(update={"status": "expired", "updated_at": at})
        self.store.save_order(o)
        self.store.drop_line(o.id or o.client_id)
        return o

    def _maker(self, o: Order, units: int, settings: FeeSettings, *, at: datetime, as_of: datetime) -> Order:
        est = maker_fill(o, units, settings, at=at, as_of=as_of)
        self._record(o, est, at)
        o = self._apply(o, est, at)
        o = o.model_copy(update={"status": "filled" if o.remaining <= 1e-9 else "open"})
        self.store.save_order(o)
        if o.status == "filled":
            self.store.drop_line(o.id or o.client_id)
        return o

    def on_book(self, book: Book, settings_for: dict[str, FeeSettings], *, at: datetime) -> list[Order]:
        """A newer book for a market: expire the resting orders it finds, and fill those it reaches.

        A book older than the last one seen for the market is ignored.
        """
        key = (book.venue, book.market)
        last = self.last_books.get(key)
        if last is not None and book.as_of < last.as_of:
            return []
        self.last_books[key] = book
        changed = []
        # Contracts on the other side this book already filled for an older order of yours (same
        # side and action) can't fill a newer one too.
        taken: dict[tuple[str, str], dict[int, int]] = {}
        for o in self.store.orders(open_only=True):
            if o.venue != book.venue or o.market != book.market:
                continue
            gone = self._expired(o, at)
            if gone is not None:
                changed.append(gone)
                continue
            line = self._line(o)
            if line is None:
                # Rested before the store kept lines: it joins the back of the line from this book.
                self.store.save_line(o.id or o.client_id, resting.start(o, book).to_json())
                continue
            mine = taken.setdefault((o.side, o.action), {})
            for p, n in mine.items():
                line.used[p] = line.used.get(p, 0) + n
            before = dict(line.used)
            units = resting.on_book(o, line, book, cancels=self.cancels)
            for p, n in line.used.items():
                if n > before.get(p, 0):
                    mine[p] = mine.get(p, 0) + n - before.get(p, 0)
            self.store.save_line(o.id or o.client_id, line.to_json())
            if units:
                settings = settings_for.get(o.market, FeeSettings(venue=o.venue))
                changed.append(self._maker(o, units, settings, at=at, as_of=book.as_of))
        return changed

    def on_trade(
        self, trade: TradePrint, settings_for: dict[str, FeeSettings], *, at: datetime
    ) -> list[Order]:
        """A trade the venue printed: it fills the resting orders it reaches once the line ahead is used up.

        Trades from before an order was placed don't count. One trade's contracts fill your orders
        oldest first and never more than the trade's size between them.
        """
        changed = []
        left = js_round(trade.size * MICRO)
        for o in self.store.orders(open_only=True):
            if o.venue != trade.venue or o.market != trade.market:
                continue
            if o.created_at is not None and trade.as_of <= o.created_at:
                continue
            gone = self._expired(o, at)
            if gone is not None:
                changed.append(gone)
                continue
            line = self._line(o)
            if line is None:
                continue  # no book seen since it rested, so its place in line isn't known yet
            # The whole trade moves each order's line (the venue's line ahead was traded either way);
            # what fills is capped by what older orders of yours haven't already taken from it.
            units = resting.on_trade(o, line, trade, self.last_books.get((trade.venue, trade.market)))
            units = min(units, left)
            left -= units
            self.store.save_line(o.id or o.client_id, line.to_json())
            if units:
                settings = settings_for.get(o.market, FeeSettings(venue=o.venue))
                changed.append(self._maker(o, units, settings, at=at, as_of=trade.as_of))
        return changed

    def expire(self, *, at: datetime) -> list[Order]:
        """Mark resting orders past their expiry as expired."""
        out = []
        for o in self.store.orders(open_only=True):
            gone = self._expired(o, at)
            if gone is not None:
                out.append(gone)
        return out

    def settle(self, resolution: Resolution) -> list[SimulatedSettlement]:
        """Pay out every open position in the resolved market and cancel its resting orders."""
        at = resolution.as_of
        ledger = build(self.store.fills(), at, self.store.settlements())
        out = []
        for p in ledger.positions:
            if (p.venue, p.market) != (resolution.venue, resolution.market):
                continue
            paid = resolution.paid(p.side, p.cost / p.contracts)
            s = SimulatedSettlement(
                mode=self.mode,
                venue=p.venue,
                market=p.market,
                side=p.side,
                outcome=resolution.outcome,
                contracts=p.contracts,
                payout=paid,
                # A void without a venue price pays back exactly what the contracts cost.
                proceeds=round(
                    p.cost
                    if resolution.outcome == "void" and resolution.payout is None
                    else paid * p.contracts,
                    6,
                ),
                at=at,
                group_id=p.group_id,
            )
            if self.store.add_settlement(s):
                out.append(s)
        for o in self.store.orders(open_only=True):
            if (o.venue, o.market) == (resolution.venue, resolution.market):
                self.store.save_order(o.model_copy(update={"status": "canceled", "updated_at": at}))
                self.store.drop_line(o.id or o.client_id)
        return out

    def cancel(self, order_id: str, *, at: datetime) -> Order:
        o = self.store.order(order_id)
        if o is None:
            raise VenueError(
                "not_found",
                f"No order {order_id} in this {self.mode} store.",
                retryable=False,
                next="client.orders()",
            )
        if o.is_open:
            o = o.model_copy(update={"status": "canceled", "updated_at": at})
            self.store.save_order(o)
            self.store.drop_line(order_id)
        return o

    def cancel_all(self, *, at: datetime) -> list[Order]:
        return [self.cancel(o.id or o.client_id, at=at) for o in self.store.orders(open_only=True)]
