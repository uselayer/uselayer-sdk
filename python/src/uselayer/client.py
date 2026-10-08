"""The client: find markets, read books, preview and send orders, inside your guardrails.

Paper is the default. ``Client()`` with no ``mode`` fills orders against real books with fake money.

    from uselayer import Client

    client = Client(rules={"max_position": {"per_market": 50}})
    order = client.order(venue="polymarket_us", market="some-slug", side="yes", price=0.42, size=10)
    print(client.preview(order))      # what would happen; sends nothing
    filled = client.send(order)       # paper fill against the live book
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from . import _switches, best
from .best import BestOrder
from .books import Book, BookLevelChange, reconstruct_book
from .errors import VENUE_NAMES, VenueError, live_switched_off, switched_off
from .events import (
    Fill,
    MarketEvent,
    Resolution,
    SimulatedFill,
    SimulatedPosition,
    SimulatedSettlement,
    StreamGap,
    TradePrint,
)
from .fee_lookup import pair_fees
from .fee_lookup import profit as lookup_profit
from .fees import dollars
from .fill import FeeSettings, FillEstimate, calculate_fee, estimate_fill
from .guardrails import (
    Context,
    CustomRule,
    Decision,
    Group,
    Guardrails,
    Mark,
    MaxDailyLossRule,
    PositionView,
    RulesConfig,
    Verdict,
    order_risk,
    reduces_risk,
    risk_key,
)
from .http import Http
from .layer_api import LayerApi, Match
from .mismatch import ResolutionMismatch, check, from_settlements, from_venue_payouts, hedged_pairs
from .orders import Order
from .paper import PaperVenue, check_order_against_market
from .pnl import Pnl, PnlRow
from .portfolio import Ledger, as_positions, build, held
from .prices import Prices, pair_prices
from .reconcile import Reconciliation, compare, missed_fills_to_add
from .resting import CANCELS, Cancels
from .store import Store, default_path
from .trading import Quote, Strategy, Trade, quote_pair, run_strategy, trade_pair
from .venues.base import Balance, LiveAdapter, MarketInfo, ReadAdapter, VenuePosition
from .venues.kalshi import Kalshi, KalshiLive
from .venues.polymarket_us import PolymarketUSPublic
from .venues.polymarket_us_live import PolymarketUS, PolymarketUSLive
from .whales import Whales

log = logging.getLogger("uselayer")

Mode = Literal["paper", "live", "backtest"]
Approval = Callable[[Order, str], bool]
Alert = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Preview:
    """What an order would do now. Nothing was sent and nothing was saved.

    ``allowed``: every rule says yes. ``blocked_by``: the rule that says no. ``needs_approval``: a rule
    wants your yes first. ``est_fill``: the fill against the current book. ``fees``: its fees in dollars.
    """

    order: Order
    verdict: Verdict
    est_fill: FillEstimate
    book_as_of: datetime
    problems: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict.allowed and not self.problems

    @property
    def blocked_by(self) -> str | None:
        return self.verdict.blocked_by

    @property
    def needs_approval(self) -> bool:
        return self.verdict.decision.result == "approve"

    @property
    def fees(self) -> float:
        return self.est_fill.fees

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked_by": self.blocked_by,
            "needs_approval": self.needs_approval,
            "problems": list(self.problems),
            "rules": self.verdict.to_dict(),
            "est_fill": self.est_fill.to_dict(),
            "fees": self.fees,
            "book_as_of": self.book_as_of.isoformat(),
            "order": self.order.to_dict(),
        }

    def __str__(self) -> str:
        import json

        return json.dumps(self.to_dict(), indent=1)


class _ReplayClock:
    def __init__(self) -> None:
        self.now = datetime(1970, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, s: float) -> None:
        # Waiting during a replay moves the replayed clock, never the real one.
        self.now += timedelta(seconds=max(s, 0.0))


@dataclass
class _Throttle:
    per_s: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    sent: deque[float] = field(default_factory=deque)

    def wait(self) -> None:
        limit = max(1, int(self.per_s))
        while True:
            now = self.clock()
            while self.sent and now - self.sent[0] >= 1.0:
                self.sent.popleft()
            if len(self.sent) < limit:
                self.sent.append(now)
                return
            self.sleep(1.0 - (now - self.sent[0]))


def _default_alert(event: dict[str, Any]) -> None:
    log.warning("uselayer alert: %s", event)


def _terminal_approval(timeout_s: float) -> Approval:
    def ask(order: Order, reason: str) -> bool:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        answer: list[str] = []

        def read() -> None:
            with contextlib.suppress(EOFError):
                answer.append(input(f"\nApprove? {reason}\n{order}\n[y/N] "))

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout_s)
        return bool(answer) and answer[0].strip().lower() in ("y", "yes")

    return ask


class Client:
    """Trade with your own venue keys, in paper (the default), live or backtest mode.

    Args:
        mode: ``"paper"`` (default; real books, fake money), ``"backtest"`` (books you supply,
            replayed) or ``"live"`` (real orders with your own venue keys).
        layer_key: your Layer API key, for :meth:`matches`. Defaults to ``LAYER_API_KEY``.
        rules: guardrail settings: a dict, a path to a YAML or JSON file, or ``None`` for only the
            always-on limits. See :mod:`uselayer.guardrails`.
        custom_rules: your own rules: objects with ``check(order, ctx)``, or plain functions.
        store: where this mode's local store lives (default ``~/.uselayer/<mode>.db``; backtests
            default to memory).
        on_approval: called as ``on_approval(order, reason) -> bool`` when a rule asks for a yes.
            Default: a prompt in the terminal when there is one, otherwise no.
        on_alert: called with a dict for anything you should know about (a stale price, a kill).
        books: for backtest mode: the books (and other market events) to replay, oldest first.
        transport: an ``httpx`` transport, to send the SDK's HTTP somewhere else (tests).
        polymarket_us: your Polymarket US key, for live Polymarket US orders. Defaults to
            ``POLYMARKET_US_KEY_ID``.
        kalshi: your Kalshi key, to read Kalshi markets and books (paper, pairs) and, in live mode,
            to send Kalshi orders. Defaults to ``KALSHI_KEY_ID``. Live mode needs at least one of the two.
        queue_cancels: paper and backtest: where cancels at a resting order's price come from when
            estimating the line ahead of it. ``"proportional"`` (default) spreads them through the line;
            ``"behind"`` is the worst case, all behind your order. See :mod:`uselayer.resting`.
        order_latency_s: paper and backtest: seconds from sending an order to it reaching the book
            (default 0). The order fills against the book as it stands then, and books and trades
            before then can't fill it. In paper mode the call waits that long and reads the book again.
            Measured on Polymarket US (2026-10-04, 8 orders): an order landed in the book 0.6–1.4 s
            after ``buy()`` was called, median about 0.7 s. In paper mode the client holds its lock
            while it waits, so ``monitor()`` and other threads using this client wait too.

    The client you hand to a strategy or an agent can trade and press the kill switch. It can't
    resume after a kill or change its rules: that's :class:`Admin` or ``python -m uselayer resume``.
    """

    def __init__(
        self,
        *,
        mode: Mode = "paper",
        layer_key: str | None = None,
        rules: RulesConfig | Mapping[str, Any] | str | Path | None = None,
        custom_rules: Sequence[CustomRule] = (),
        store: str | Path | None = None,
        on_approval: Approval | None = None,
        on_alert: Alert | None = None,
        books: Iterable[MarketEvent] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        polymarket_us: PolymarketUS | None = None,
        kalshi: Kalshi | None = None,
        ws_connect: Callable[..., Any] | None = None,
        on_miss: Literal["unwind", "hold"] = "unwind",
        queue_cancels: Cancels = "proportional",
        order_latency_s: float = 0.0,
    ) -> None:
        if mode == "sandbox":  # type: ignore[comparison-overlap]
            raise VenueError(
                "not_available",
                "Layer's hosted sandbox was removed in uselayer 0.3.0.",
                retryable=False,
                hint="Paper mode fills the same orders against the venues' real books with fake money, on your machine.",
                next="Client(mode='paper')",
            )
        if queue_cancels not in CANCELS:
            raise VenueError(
                "invalid_order",
                f"queue_cancels must be one of {', '.join(CANCELS)}, not {queue_cancels!r}.",
                retryable=False,
                hint="'proportional' spreads cancels through the line; 'behind' is the worst case.",
            )
        if not (0 <= order_latency_s < 3600):
            raise VenueError(
                "invalid_order",
                f"order_latency_s must be 0 or more seconds (under an hour), not {order_latency_s!r}.",
                retryable=False,
            )
        self._latency_s = float(order_latency_s)
        self._replay_events: list[MarketEvent] = []
        self._replay_i = 0
        if mode not in ("paper", "live", "backtest"):
            raise VenueError(
                "invalid_order",
                f"mode must be paper, live or backtest, not {mode!r}.",
                retryable=False,
            )
        if mode == "live" and not _switches.LIVE_ADAPTERS:
            raise VenueError(
                "not_available",
                "This release trades in paper and backtest mode only.",
                retryable=False,
                hint="Use mode='paper' to fill orders against real books with fake money.",
                next="Client(mode='paper')",
            )
        self.mode: Mode = mode
        self._on_miss: Literal["unwind", "hold"] = on_miss
        self._guard = Guardrails(rules, custom_rules)
        self._replay_clock = _ReplayClock() if mode == "backtest" else None
        self._clock: Callable[[], datetime] = clock or self._replay_clock or (lambda: datetime.now(UTC))
        if self._replay_clock is not None:
            sleep = self._replay_clock.sleep
        self._sleep = sleep
        self._own_clock = clock is not None or self._replay_clock is not None
        if transport is None and mode != "live":
            from ._recorded import RecordTransport, ReplayTransport

            if os.environ.get("USELAYER_RECORDED"):
                transport = ReplayTransport(os.environ["USELAYER_RECORDED"])
            elif os.environ.get("USELAYER_RECORD"):
                transport = RecordTransport(os.environ["USELAYER_RECORD"])
        self._http = Http(transport=transport, sleep=sleep)
        self._layer = LayerApi(layer_key or os.environ.get("LAYER_API_KEY"), self._http)
        #: Top traders, their trades, cross-venue links and copy trading (:mod:`uselayer.whales`).
        self.whales = Whales(self._http, self._layer, self)
        self._venues: dict[str, ReadAdapter] = {"polymarket_us": PolymarketUSPublic(self._http)}
        self._live: dict[str, LiveAdapter] = {}
        kalshi_key = kalshi
        if kalshi_key is None and os.environ.get("KALSHI_KEY_ID"):
            kalshi_key = Kalshi.from_env()
        if mode == "live":
            key = polymarket_us
            if key is None and os.environ.get("POLYMARKET_US_KEY_ID"):
                key = PolymarketUS.from_env()
            if key is None and kalshi_key is None:
                raise VenueError(
                    "auth_failed",
                    "Live mode needs your venue key.",
                    retryable=False,
                    hint="Pass Client(mode='live', polymarket_us=PolymarketUS(key_id=..., secret_key_path=...)) "
                    "or Client(mode='live', kalshi=Kalshi(key_id=..., private_key_path=...)), or both.",
                    next="Client(mode='live', kalshi=Kalshi(...))",
                )
            if key is not None:
                live = PolymarketUSLive(
                    self._http,
                    key,
                    clock=self._clock,
                    ws_connect=ws_connect,
                    on_alert=lambda e: self._on_alert(e),
                    sleep=sleep,
                )
                self._live["polymarket_us"] = live
                self._venues["polymarket_us"] = live
        if kalshi_key is not None and mode != "backtest":
            k = KalshiLive(self._http, kalshi_key, clock=self._clock, sleep=sleep)
            self._venues["kalshi"] = k
            if mode == "live" and "kalshi" in _switches.LIVE_ADAPTERS:
                self._live["kalshi"] = k
        path = store if store is not None else (":memory:" if mode == "backtest" else default_path(mode))
        self._store = Store(path)
        self._paper = PaperVenue(
            self._store, "backtest" if mode == "backtest" else "paper", cancels=queue_cancels
        )
        self._on_approval = on_approval or _terminal_approval(self._guard.config.approval_timeout_s)
        self._on_alert = on_alert or _default_alert
        self._throttle = _Throttle(self._guard.config.max_orders_per_s, self._monotonic, sleep)
        self._books: dict[tuple[str, str], Book] = {}
        self._info: dict[tuple[str, str], MarketInfo] = {}
        self._events: list[MarketEvent] = list(books) if books is not None else []
        self._lock = threading.RLock()
        self._settle_checked: dict[tuple[str, str], datetime] = {}
        if self._live:
            self._startup()

    # ---- small helpers ----

    def _monotonic(self) -> float:
        # A replay or a supplied clock drives the throttle too, so tests and backtests never wait for real.
        return self._clock().timestamp() if self._own_clock else time.monotonic()

    def _now(self) -> datetime:
        return self._clock()

    def _alert(self, kind: str, **fields: Any) -> None:
        self._on_alert({"kind": kind, "at": self._now().isoformat(), **fields})

    @property
    def rules(self) -> RulesConfig:
        """The rule settings in force (frozen)."""
        return self._guard.config

    @property
    def store(self) -> Store:
        return self._store

    def close(self) -> None:
        """Close the HTTP client and the store."""
        self._http.close()
        self._store.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- Layer ----

    def matches(self, *, titles: bool = True, **filters: Any) -> list[Match]:
        """Markets that are the same bet on two venues, from Layer.

            m = client.matches(q="chiefs", venue="polymarket_us")[0]
            m.polymarket_us.market_id, m.polymarket_us.outcome, m.confidence, m.caveats

        Filters: ``limit``, ``offset``, ``category``, ``from_``, ``to``, ``q``, ``venue``.
        Each market's event, question, outcome and times are read from its venue on your machine
        (one public call per event); ``titles=False`` skips that.
        """
        return self._layer.matches(titles=titles, **filters)

    def match(
        self, market_id: str, *, venue: str, with_: str | None = None, titles: bool = True
    ) -> dict[str, Any]:
        """The twin of one market on the other venue, from Layer, with venue titles filled in.

        client.match("some-slug", venue="polymarket_us")["matched_market"]
        """
        return self._layer.match(market_id, venue=venue, with_=with_, titles=titles)

    # ---- market data ----

    def _check_venue(self, venue: str) -> None:
        if self.mode == "backtest" and venue in _switches.BACKTEST:
            return  # a backtest replays your own data and sends nothing
        if venue not in _switches.PAPER:
            raise switched_off(venue, _switches.paper_venues())
        if self.mode != "backtest" and venue not in self._venues:
            raise VenueError(
                "auth_failed",
                f"{VENUE_NAMES.get(venue, venue)} books are read with your own API key for it.",
                venue=venue,
                retryable=False,
                hint="Pass Client(kalshi=Kalshi(key_id=..., private_key_path=...)) or set KALSHI_KEY_ID "
                "and KALSHI_PRIVATE_KEY_PATH. The key stays on your machine.",
                next="Client(kalshi=Kalshi(key_id=..., private_key_path=...))",
            )

    def _where_unavailable(self, order: Order) -> VenueError | None:
        """Why this mode can't send ``order`` to its venue, or None."""
        if self.mode == "live" and not _switches.TRADING.get(order.venue, False):
            return live_switched_off(order.venue)
        return None

    def market(self, market: Any, *, venue: str = "polymarket_us") -> MarketInfo:
        """A market's status, tick size, minimum size and fee settings.

        client.market("some-slug").tick_size
        """
        venue, mid = _resolve(market, venue)
        self._check_venue(venue)
        key = (venue, mid)
        if key not in self._info:
            if self.mode == "backtest":
                book = self._books.get(key)
                self._info[key] = MarketInfo(
                    venue, mid, None, "replayed", True, 0.000001, 0.000001, FeeSettings(venue=venue)
                )
                if book is None:
                    return self._info[key]
            else:
                self._info[key] = self._venues[venue].market(mid)
        return self._info[key]

    def markets(self, *, venue: str = "polymarket_us", limit: int = 50, offset: int = 0) -> list[MarketInfo]:
        """Open markets on a venue, straight from the venue (no Layer key needed).

        for m in client.markets(limit=10): print(m.slug, m.question)
        """
        self._check_venue(venue)
        out = []
        for info in self._venues[venue].markets(limit=limit, offset=offset):
            self._info[(venue, info.market)] = info
            if info.open:
                out.append(info)
        return out

    def book(self, market: Any, *, venue: str = "polymarket_us") -> Book:
        """The market's current book (YES side), stamped with the venue's own time.

            b = client.book("some-slug")
            b.outcome("no").best_ask, b.as_of

        In backtest mode, the latest replayed book.
        """
        venue, mid = _resolve(market, venue)
        self._check_venue(venue)
        if self.mode == "backtest":
            b = self._books.get((venue, mid))
            if b is None:
                raise VenueError(
                    "stale_quote",
                    f"No replayed book for {mid} yet.",
                    venue=venue,
                    hint="In backtest mode, books come from the replay.",
                    next="client.replay(...)",
                )
            return b
        r = self._venues[venue].read_book(mid)
        limit = self._guard.config.max_quote_age_s
        age = r.book.age_s(self._now())
        if age > limit and r.cache_max_age_s is not None and r.cache_age_s is not None:
            # The public book is cached; a fresh copy exists only once this one expires. Wait for it once.
            wait = max(0.0, r.cache_max_age_s - r.cache_age_s) + 0.5
            if wait <= r.cache_max_age_s + 1:
                self._sleep(wait)
                r = self._venues[venue].read_book(mid)
        self._books[(venue, mid)] = r.book
        if r.state not in (None, "MARKET_STATE_OPEN"):
            self._alert("market_not_open", venue=venue, market=mid, state=r.state)
        return r.book

    def _fresh_books(self, legs: Sequence[tuple[str, str]], *, passes: int = 2) -> list[Book | VenueError]:
        """Every leg's book, read so they're fresh together, in the order given.

        One read can wait: a cached public book waits for a fresh copy (up to ~30 s on Polymarket
        US), which leaves a book read before it older than ``max_quote_age_s``. Those legs are read
        again, up to ``passes`` more rounds. A book already too old when it arrives isn't read again
        (another read would only wait again), so the error names that venue. A leg whose read fails
        comes back as its error; a book that's still too old comes back as it is, for the caller to
        refuse. Backtests read each replayed book once.
        """
        limit = self._guard.config.max_quote_age_s
        old_on_arrival: set[int] = set()

        def read(i: int) -> Book | VenueError:
            venue, market = legs[i]
            try:
                b = self.book(market, venue=venue)
            except VenueError as e:
                return e
            if b.age_s(self._now()) > limit:
                old_on_arrival.add(i)
            else:
                old_on_arrival.discard(i)
            return b

        out = [read(i) for i in range(len(legs))]
        if self.mode == "backtest":
            return out
        for _ in range(passes):
            now = self._now()
            stale = [
                i
                for i, b in enumerate(out)
                if isinstance(b, Book) and i not in old_on_arrival and b.age_s(now) > limit
            ]
            if not stale:
                break
            for i in stale:
                out[i] = read(i)
        return out

    def _settings(self, venue: str, market: str) -> FeeSettings:
        info = self._info.get((venue, market))
        return FeeSettings(venue=venue) if info is None else info.fees

    # ---- orders ----

    def order(
        self,
        *,
        venue: str,
        market: Any,
        side: str,
        price: float,
        size: float,
        action: str = "buy",
        tif: str = "ioc",
        expires_at: datetime | None = None,
        post_only: bool = False,
        group_id: str | None = None,
    ) -> Order:
        """An order, not sent yet: pass it to :meth:`preview` or :meth:`send`.

        o = client.order(venue="polymarket_us", market="some-slug", side="yes", price=0.42, size=10)
        """
        venue, mid = _resolve(market, venue)
        return Order(
            venue=venue,
            market=mid,
            side=side,
            action=action,
            price=price,
            size=size,
            tif=tif,
            expires_at=expires_at,
            post_only=post_only,
            group_id=group_id,
        )

    def _prepare(self, order: Order) -> tuple[Order, Book, MarketInfo]:
        self._check_venue(order.venue)
        info = self.market(order.market, venue=order.venue)
        book = self.book(order.market, venue=order.venue)
        now = self._now()
        fee = dollars(
            calculate_fee(
                self._settings(order.venue, order.market),
                contracts=order.size,
                price=order.price,
                role="taker",
                at=now,
            )
        )
        upd: dict[str, Any] = {"fee_estimate": fee}
        if order.tif == "gtc" and order.expires_at is None:
            upd["expires_at"] = now + timedelta(seconds=self._guard.config.order_ttl_s)
        return order.model_copy(update=upd), book, info

    def preview(self, order: Order) -> Preview:
        """What ``order`` would do now: the fill, its fees, and every rule's decision. Sends nothing.

        p = client.preview(order)
        p.allowed, p.blocked_by, p.est_fill.avg_price, p.fees
        """
        order, book, info = self._prepare(order)
        problems: list[str] = []
        try:
            check_order_against_market(
                order,
                tick=info.tick_size,
                min_size=info.min_size,
                held_now=held(
                    self._store.fills(), order.venue, order.market, order.side, self._store.settlements()
                ),
            )
        except VenueError as e:
            problems.append(e.message)
        if not info.open and not reduces_risk(order):
            problems.append(f"{order.market} isn't open for trading ({info.status}).")
        unavailable = self._where_unavailable(order)
        if unavailable is not None:
            problems.append(unavailable.message)
        est = estimate_fill(order, book, self._settings(order.venue, order.market), at=self._now())
        verdict = self._guard.check(order, self._context(order))
        return Preview(order, verdict, est, book.as_of, tuple(problems))

    def buy(self, **kw: Any) -> Order:
        """Send a buy. Same arguments as :meth:`order`.

        client.buy(venue="polymarket_us", market="some-slug", side="yes", price=0.42, size=10)
        """
        return self.send(self.order(action="buy", **kw))

    def sell(self, **kw: Any) -> Order:
        """Send a sell of contracts you hold. Same arguments as :meth:`order`."""
        return self.send(self.order(action="sell", **kw))

    def send(self, order: Order) -> Order:
        """Check ``order`` against the rules and, if allowed, send it (paper: fill it against the book).

        Raises ``VenueError("blocked_by_rule")`` naming the rule when a rule says no.
        """
        with self._lock:
            unavailable = self._where_unavailable(order)
            if unavailable is not None:
                raise unavailable
            prepared, _, info = self._prepare(order)
            self._require_open(prepared, info)
            self._decide(prepared)
            return self._execute(prepared, checked=True)

    def _require_open(self, order: Order, info: MarketInfo) -> None:
        if not info.open and not reduces_risk(order):
            raise VenueError(
                "market_closed",
                f"{order.market} isn't open for trading ({info.status}).",
                venue=order.venue,
                retryable=False,
                next="Pick an open market.",
            )

    def _execute(self, order: Order, *, checked: bool = False) -> Order:
        """Send an order the rules already allowed (alone, or as one leg of a pair checked together).

        The throttle, the kill switch and the venue's own limits still apply.
        """
        with self._lock:
            if checked:
                book = self._books[(order.venue, order.market)]
                info = self.market(order.market, venue=order.venue)
            else:
                order, book, info = self._prepare(order)
                self._require_open(order, info)
            unavailable = self._where_unavailable(order)
            if unavailable is not None:
                raise unavailable
            self._throttle.wait()
            # The flag can be set from another terminal at any moment: read it again right before sending.
            if self._store.killed() and order.reason not in ("kill", "unwind"):
                raise _blocked("kill_switch", "The kill switch is on.")
            if self.mode == "live":
                return self._send_live(order, info)
            # Fill against the same book the rules just checked, or the one there when it arrives.
            sent = self._now()
            book, arrives = self._arrival(order, book, sent)
            return self._paper.submit(
                order,
                book,
                self._settings(order.venue, order.market),
                at=arrives,
                sent_at=sent,
                tick=info.tick_size,
                min_size=info.min_size,
            )

    def _arrival(self, order: Order, book: Book, sent: datetime) -> tuple[Book, datetime]:
        """The book a paper or backtest order meets when it reaches the venue, and when."""
        if self._latency_s <= 0:
            return book, sent
        arrives = sent + timedelta(seconds=self._latency_s)
        if self.mode == "backtest":
            # The replay is at ``sent``: look ahead through its own events to the book at arrival.
            current: Book | None = book
            for ev in self._replay_events[self._replay_i + 1 :]:
                if ev.as_of > arrives:
                    break
                if (ev.venue, ev.market) != (order.venue, order.market):
                    continue
                if isinstance(ev, Book):
                    current = ev
                elif isinstance(ev, BookLevelChange) and current is not None:
                    current = reconstruct_book([current, ev])
            return (current or book), arrives
        self._sleep(self._latency_s)
        return self.book(order.market, venue=order.venue), self._now()

    def _decide_group(self, orders: list[Order]) -> Verdict:
        """Both legs of a pair against the rules at once: no leg is sent unless every leg may be."""
        ctx = self._context(None)
        verdict = self._guard.check_group(orders, ctx)
        if verdict.decision.result == "approve":
            for o in orders:
                self._journal(o, verdict, ctx)
            if not self._on_approval(orders[0], f"pair: {verdict.decision.reason}"):
                raise _blocked(
                    verdict.decision.rule or "approval", f"Not approved: {verdict.decision.reason}"
                )
            ctx = self._context(None)
            verdict = self._guard.check_group(orders, ctx)
            if verdict.decision.result == "approve":
                verdict = Verdict(Decision.allow("approved"), verdict.decisions)
        for o in orders:
            self._journal(o, verdict, ctx)
        d = verdict.decision
        if d.result == "kill":
            self._press_kill(f"rule {d.rule}: {d.reason}")
            raise _blocked(d.rule or "kill", d.reason or "A rule pressed the kill switch.")
        if d.result == "block":
            raise _blocked(d.rule or "rule", d.reason or "Blocked by a rule.")
        return verdict

    # ---- pairs ----

    def quote(
        self,
        pair: Any,
        *,
        size: int | None = None,
        min_edge: float = 0.0,
        settles_at: str | datetime | None = None,
    ) -> Quote:
        """Price buying YES on one market and NO on its twin, after both fees, from both books now.

        q = client.quote(match)          # a Match from client.matches(), or two (venue, market) pairs
        q.contracts, q.net_profit_per_contract, q.a.side, q.b.side
        q.return_pct, q.return_per_day_pct, q.days_held, q.settles_at

        ``settles_at`` is when the money comes back: the later of the two markets' expected payouts,
        read from the venues as :meth:`profit` reads it, unless you pass it (a date like
        ``"2026-10-05"``, a time with a zone, or an aware datetime). With neither, the three time
        fields are ``None``; a backtest has no venue times, so pass it there.
        """
        return quote_pair(self, pair, size=size, min_edge=min_edge, settles_at=settles_at)

    def prices(self, pair: Any) -> Prices:
        """Each market's best YES and NO bid and ask, with the size at each and when it was read.

            p = client.prices(match)          # a Match from client.matches(), or two (venue, market) pairs
            p.a.yes_ask, p.a.yes_ask_size, p.b.no_bid, p.b.as_of
            p.leg("kalshi").no_ask

        Read with :meth:`book` (paper and live mode: from the venues with your own keys, reads only;
        backtest mode: the replayed books). ``p.to_dict()`` is plain JSON.
        """
        return pair_prices(self, pair)

    def fees(self, pair: Any) -> dict[str, Any]:
        """A Kalshi ↔ Polymarket US pair's fee settings and days until payout, read from the venues now.

            client.fees(match)
            # {"kalshi": {"fee_type": "quadratic", "fee_multiplier": 1.0},
            #  "polymarket_us": {"fee_coefficient": 0.0695}, "days_held": 2.25,
            #  "match": {"kalshi": ..., "polymarket_us": ..., "expected_payout_at": ..., "latest_payout_at": ...}}

        What Layer's ``POST /v0/profit`` fills in for ``kalshi.market_id``, read with your own venue keys.
        ``polymarket_us`` is empty when the market gives no coefficient (the published taker rate
        applies); ``days_held`` is left out when neither venue gives a time. Paper and live mode.
        """
        return pair_fees(self, pair)

    def profit(self, request: Mapping[str, Any], *, pair: Any = None) -> dict[str, Any]:
        """``POST /v0/profit`` on your machine: fees and net profit for YES on one venue and NO on the other.

            client.profit({"contracts": 100, "kalshi": {"market_id": "KX...", "price": 0.42},
                           "polymarket_us": {"price": 0.55}})
            client.profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}},
                          pair=match)

        The same body as Layer's endpoint. With ``kalshi.market_id`` (its Polymarket US twin comes from
        Layer's match) or ``pair``, each market's fee settings and ``days_held`` are filled in from
        :meth:`fees`, and the answer adds ``match`` and ``filled_in``. Anything the request sends wins.
        Without either, it's :func:`uselayer.calc.profit` and reads nothing.
        """
        return lookup_profit(self, request, pair)

    def trade(
        self,
        pair: Any,
        *,
        size: int | None = None,
        min_edge: float = 0.01,
        on_miss: Literal["unwind", "hold"] | None = None,
        max_unwind_loss: float = 0.05,
        chase_s: float = 3.0,
        settles_at: str | datetime | None = None,
    ) -> Trade:
        """Buy both sides of a pair with the leg-risk guard (see :mod:`uselayer.trading`). Every mode.

        t = client.trade(match, size=100, min_edge=0.01)
        t.status          # "hedged" | "missed" | "unwound" | "exposed"

        Live, it needs your key for both legs' venues (checked before anything is sent).
        ``settles_at`` is passed to :meth:`quote` (``t.quote.return_per_day_pct``).
        """
        return trade_pair(
            self,
            pair,
            size=size,
            min_edge=min_edge,
            on_miss=on_miss or self._on_miss,
            max_unwind_loss=max_unwind_loss,
            chase_s=chase_s,
            settles_at=settles_at,
        )

    def buy_best(
        self,
        pair: Any,
        side: str,
        size: float | None = None,
        max_price: float | None = None,
        *,
        spend: float | None = None,
        tif: str = "ioc",
    ) -> BestOrder:
        """Buy ``size`` contracts of ``side`` on whichever venue of ``pair`` is cheaper for that size, after fees.

            r = client.buy_best(match, "yes", 10, max_price=0.55)
            r.order.venue, r.order.filled, r.why.reason
            [(v.venue, v.all_in, v.skip) for v in r.why.venues]

            r = client.buy_best(match, "yes", spend=50)   # $50 on YES, fees included
            r.order.size, r.why.reason_code              # e.g. 69, "wins_more"

        Give ``size`` (contracts) or ``spend`` (dollars), not both. With ``spend``, each venue gets
        the most whole contracts whose cost plus fees fits in it, and the venue where they pay more
        if you're right wins (the same number on both: the cheaper one).

        Each venue's book is walked for ``size`` (never past ``max_price`` or the price collar) and
        priced with its own fees; the cheaper all-in cost wins, and on a tie the venue with more on
        offer at its price. The order is a limit at the walk's deepest price, sent through
        :meth:`send`, so every guardrail, the price collar and the kill switch apply. The comparison
        is a snapshot: a book can move before the order arrives, and the limit caps what it can pay
        (``tif="ioc"``, the default, fills what's still there; ``"fok"`` all or nothing).

        A venue is skipped with a reason (see :mod:`uselayer.best`): switched off, no key, not in
        the ``venues`` / ``markets`` rules, a closed market, a stale book, or not enough size within
        the limit. With no venue left, raises ``VenueError("not_available")`` and sends nothing.
        Paper, backtest and live mode.
        """
        return self._best(pair, side, size, action="buy", limit=max_price, tif=tif, send=True, spend=spend)

    def sell_best(
        self, pair: Any, side: str, size: float, min_price: float | None = None, *, tif: str = "ioc"
    ) -> BestOrder:
        """Sell ``size`` contracts of ``side`` you hold on whichever venue of ``pair`` pays more for them, after fees.

            r = client.sell_best(match, "yes", 10, min_price=0.40)

        Only a venue where this account holds at least ``size`` of that side is considered (``not_held``
        otherwise: prediction markets don't let you sell what you don't hold). The walk never goes below
        ``min_price`` or the price collar under the best bid. Otherwise the same as :meth:`buy_best`.
        """
        return self._best(pair, side, size, action="sell", limit=min_price, tif=tif, send=True)

    def preview_best(
        self,
        pair: Any,
        side: str,
        size: float | None = None,
        max_price: float | None = None,
        *,
        action: Literal["buy", "sell"] = "buy",
        min_price: float | None = None,
        spend: float | None = None,
        tif: str = "ioc",
    ) -> BestOrder:
        """What :meth:`buy_best` (or :meth:`sell_best`, with ``action="sell"``) would do now. Sends nothing.

            p = client.preview_best(match, "yes", 10)
            p.why.venue, p.why.reason, p.order.price, p.preview.allowed
            client.preview_best(match, "yes", spend=50)   # a buy by dollars

        ``p.order`` is the order that would be sent (``None`` when no venue can take it) and
        ``p.preview`` its :meth:`preview`: the fill, fees and every rule's decision.
        """
        if action not in ("buy", "sell"):
            raise VenueError(
                "invalid_order", f"action must be 'buy' or 'sell', not {action!r}.", retryable=False
            )
        if action == "buy" and min_price is not None:
            raise VenueError(
                "invalid_order", "min_price is for sells; a buy takes max_price.", retryable=False
            )
        if action == "sell" and max_price is not None:
            raise VenueError(
                "invalid_order", "max_price is for buys; a sell takes min_price.", retryable=False
            )
        limit = max_price if action == "buy" else min_price
        return self._best(pair, side, size, action=action, limit=limit, tif=tif, send=False, spend=spend)

    def _best(
        self,
        pair: Any,
        side: str,
        size: float | None,
        *,
        action: Literal["buy", "sell"],
        limit: float | None,
        tif: str,
        send: bool,
        spend: float | None = None,
    ) -> BestOrder:
        t = best.check_tif(tif)
        if spend is not None and size is not None:
            raise VenueError(
                "invalid_order", "Give size (contracts) or spend (dollars), not both.", retryable=False
            )
        if spend is not None:
            if action == "sell":
                raise VenueError(
                    "invalid_order",
                    "spend is for buys; a sell takes size, the contracts you hold.",
                    retryable=False,
                )
            why = best.compare_spend(self, pair, side, spend, limit=limit)
        elif size is None:
            raise VenueError("invalid_order", "Give size (contracts) or spend (dollars).", retryable=False)
        else:
            why = best.compare(self, pair, side, size, action=action, limit=limit)
        order = best.order_for(why, t)
        if not send:
            return BestOrder(order, why, False, self.preview(order) if order is not None else None)
        if order is None:
            raise best.no_venue(why)
        return BestOrder(self.send(order), why, True)

    def run(
        self,
        strategy: Strategy,
        pairs: Iterable[Any],
        *,
        interval_s: float = 1.0,
        iterations: int | None = None,
        stop: Callable[[], bool] | None = None,
        min_edge: float = 0.0,
        size: int | None = None,
        settles_at: str | datetime | None = None,
    ) -> int:
        """Call ``strategy(client, pair, quote)`` for each pair on each new book. The same function runs in every mode.

            def strategy(client, pair, quote):
                if quote.net_profit_per_contract >= 0.02:
                    client.trade(pair, size=100)

            Client(mode="backtest", books=saved).run(strategy, pairs)   # the past
            Client().run(strategy, pairs, iterations=10)                # now, paper

        Returns how many times the strategy ran. In paper and live mode it runs every ``interval_s``
        until ``iterations`` rounds, ``stop()`` or the kill switch. ``settles_at`` is passed to
        :meth:`quote`.
        """
        return run_strategy(
            self,
            strategy,
            pairs,
            interval_s=interval_s,
            iterations=iterations,
            stop=stop,
            min_edge=min_edge,
            size=size,
            settles_at=settles_at,
        )

    def _send_live(self, order: Order, info: MarketInfo) -> Order:
        adapter = self._live.get(order.venue)
        if adapter is None:
            raise VenueError(
                "auth_failed",
                f"No {order.venue} key was given.",
                venue=order.venue,
                retryable=False,
                next=f"Client(mode='live', {order.venue}=...)",
            )
        check_order_against_market(order, tick=info.tick_size, min_size=info.min_size, held_now=float("inf"))
        now = self._now()
        order = order.model_copy(
            update={
                "id": order.client_id,
                "mode": "live",
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            }
        )
        # Saved before sending: if this process dies mid-send, sync() still knows to look for it.
        self._store.save_order(order)
        try:
            placed, fills = adapter.place(order)
        except VenueError as e:
            if e.code != "outcome_unknown":
                self._store.save_order(
                    order.model_copy(update={"status": "rejected", "updated_at": self._now()})
                )
                raise
            found = adapter.find(order, since=now)
            if found is None:
                self._alert("outcome_unknown", order=order.to_dict())
                raise
            placed, fills = found, []
        if not fills:
            placed, fills = self._fills_not_seen(adapter, order, placed)
        for f in fills:
            self._store.add_fill(f)
        if placed.fees is None and fills:
            # Some venues bill per execution only; the order's fee is then what its fills were billed.
            placed = placed.model_copy(update={"fees": round(sum(f.fee for f in fills), 6)})
        self._store.save_order(placed)
        return placed

    def _decide(self, order: Order) -> Verdict:
        ctx = self._context(order)
        verdict = self._guard.check(order, ctx)
        if verdict.decision.result == "approve":
            self._journal(order, verdict, ctx)
            yes = self._on_approval(order, verdict.decision.reason or "")
            if not yes:
                raise _blocked(
                    verdict.decision.rule or "approval", f"Not approved: {verdict.decision.reason}"
                )
            ctx = self._context(order)
            verdict = self._guard.check(order, ctx)
            if verdict.decision.result == "approve":
                verdict = Verdict(Decision.allow("approved"), verdict.decisions)
        self._journal(order, verdict, ctx)
        d = verdict.decision
        if d.result == "kill":
            self._press_kill(f"rule {d.rule}: {d.reason}")
            raise _blocked(d.rule or "kill", d.reason or "A rule pressed the kill switch.")
        if d.result == "block":
            raise _blocked(d.rule or "rule", d.reason or "Blocked by a rule.")
        return verdict

    def _journal(self, order: Order, verdict: Verdict, ctx: Context) -> None:
        inputs = {
            "order": {
                k: order.to_dict()[k]
                for k in ("venue", "market", "side", "action", "price", "size", "reason", "group_id")
            },
            "exposure_total": round(ctx.exposure.get("total", 0.0), 6),
            "pnl_today": round(ctx.pnl_today, 6),
            "killed": ctx.killed,
        }
        for d in verdict.decisions:
            if d.result != "allow":
                self._store.journal(
                    at=ctx.now,
                    order_id=order.client_id,
                    rule=d.rule,
                    result=d.result,
                    reason=d.reason,
                    inputs=inputs,
                    config_hash=self._guard.fingerprint,
                )
        self._store.journal(
            at=ctx.now,
            order_id=order.client_id,
            rule=verdict.decision.rule,
            result="final:" + verdict.decision.result,
            reason=verdict.decision.reason,
            inputs=inputs,
            config_hash=self._guard.fingerprint,
        )

    def cancel(self, order: Order | str) -> Order:
        """Cancel a resting order.

        client.cancel(order)   # or client.cancel(order.id)
        """
        oid = order if isinstance(order, str) else (order.id or order.client_id)
        if self.mode != "live":
            return self._paper.cancel(oid, at=self._now())
        stored = self._store.order(oid)
        if stored is None:
            raise VenueError(
                "not_found", f"No order {oid} in the live store.", retryable=False, next="client.sync()"
            )
        adapter = self._live[stored.venue]
        canceled, fills = self._fills_not_seen(adapter, stored, adapter.cancel(stored))
        for f in fills:
            self._store.add_fill(f)
        self._store.save_order(canceled)
        return canceled

    def _fills_not_seen(self, adapter: LiveAdapter, before: Order, after: Order) -> tuple[Order, list[Fill]]:
        """The fills behind ``after``'s filled size beyond ``before``'s, when ``after`` came without them: an
        order that filled while it was being canceled, or one found after an unknown outcome.

        ``sync()`` counts new fills from the order's saved filled size, so it would never record these.
        They're read with ``refresh()`` from ``before``. ``after``'s status stands.
        """
        if after.filled <= before.filled + 1e-6 or not after.venue_order_id:
            return after, []
        updated, fills = adapter.refresh(before.model_copy(update={"venue_order_id": after.venue_order_id}))
        if updated.filled + 1e-6 < after.filled:
            return after, fills
        return (
            after.model_copy(
                update={
                    "filled": updated.filled,
                    "avg_price": updated.avg_price,
                    "fees": updated.fees if updated.fees is not None else after.fees,
                }
            ),
            fills,
        )

    def cancel_all(self) -> list[Order]:
        """Cancel every resting order (in live mode, every open order on every venue, not only this SDK's)."""
        if self.mode != "live":
            return self._paper.cancel_all(at=self._now())
        for venue, adapter in self._live.items():
            try:
                adapter.cancel_all()
            except VenueError as e:
                self._alert("cancel_all_failed", venue=venue, error=e.to_dict())
        self.sync()
        return self._store.orders(open_only=True)

    def orders(self, *, open: bool = True) -> list[Order]:
        """Orders in this mode's store: resting ones only (default) or all. Live mode re-reads open ones first."""
        self.sync()
        return self._store.orders(open_only=open)

    def fills(self, *, since: datetime | None = None) -> list[Fill | SimulatedFill]:
        """Fills in this mode's store. In paper and backtest mode they're all ``SimulatedFill``; live fills are ``Fill``."""
        return self._store.fills(since=since)

    def positions(self) -> list[SimulatedPosition] | list[VenuePosition]:
        """Open positions: from the venues in live mode, from this mode's simulated fills otherwise.

        In paper mode, positions whose market the venue has settled are paid out first (see :meth:`settle`).
        """
        if self.mode == "live":
            out: list[VenuePosition] = []
            for adapter in self._live.values():
                out.extend(p for p in adapter.positions() if not p.settled)
            return out
        if self.mode == "paper":
            self._settle_from_venues()
        return as_positions(self._ledger(self._day_start()), self._paper.mode)

    def _ledger(self, day_start: datetime) -> Ledger:
        return build(self._store.fills(), day_start, self._store.settlements())

    def settlements(self) -> list[SimulatedSettlement]:
        """Paper and backtest positions paid out because their market settled, oldest first."""
        return self._store.settlements()

    def settle(self, resolutions: Iterable[Resolution] | None = None) -> list[SimulatedSettlement]:
        """Pay out paper or backtest positions whose market has settled. Returns the new payouts.

            client.settle()        # paper: ask each venue about the markets you hold
            client.settle([Resolution(venue="polymarket_us", market="some-slug", outcome="yes", as_of=now)])

        Each contract pays what the venue paid: $1 if its side won, $0 if it lost, the venue's price
        when it gives one (Kalshi's fair price for a canceled game), or what it cost on a ``void``
        with no price. The position closes and resting orders on the market are canceled. Paper mode
        also does this on its own in :meth:`positions`, :meth:`pnl` and :meth:`monitor`; a backtest
        does it at each ``resolution`` it replays. Live positions are settled by the venues.
        """
        if self.mode == "live":
            if resolutions is not None:
                raise VenueError(
                    "not_available",
                    "Live positions are settled by the venues, not by the SDK.",
                    retryable=False,
                    next="client.pnl()",
                )
            return []
        if resolutions is None:
            return self._settle_from_venues(force=True) if self.mode == "paper" else []
        out: list[SimulatedSettlement] = []
        with self._lock:
            for r in resolutions:
                out.extend(self._resolve(r))
        return out

    def _resolve(self, r: Resolution) -> list[SimulatedSettlement]:
        """Pay out one resolution. The market takes no new orders after it."""
        paid = self._paper.settle(r)
        key = (r.venue, r.market)
        info = self._info.get(key) or MarketInfo(
            r.venue, r.market, None, "replayed", True, 0.000001, 0.000001, FeeSettings(venue=r.venue)
        )
        self._info[key] = replace(info, status="settled", open=False)
        for s in paid:
            self._alert("settled", settlement=s.to_dict())
        if any(s.group_id for s in paid):
            self._check_mismatches()
        return paid

    def resolution_mismatches(self) -> list[ResolutionMismatch]:
        """Hedged pairs the two venues settled differently, oldest first. Every mode.

            for m in client.resolution_mismatches():
                m.kind, m.impact, m.pending          # "both_lost", -100.0, False
                m.a.venue, m.a.outcome, m.a.payout   # each venue's result
                m.b.venue, m.b.outcome, m.b.payout

        A pair is the two legs one :meth:`trade` filled (they share its ``group_id``). It should pay
        $1 per contract at settlement; it's flagged when both legs lost (``both_lost``), both won
        (``both_won``), one venue voided its market and the other didn't (``void_one_leg``), or one
        leg was still open 48 hours after the other settled (``settle_gap``, ``pending`` until it
        settles). ``impact`` is what the pair paid minus the $1 per contract expected; ``pnl()``
        breaks their total out of ``realized`` as ``resolution_mismatch_loss``. Each is saved in this
        mode's store.

        Paper mode first asks the venues about the markets you hold, as :meth:`pnl` does; live mode
        asks each venue what the pair's markets paid (reads only, with your keys). Nothing is sent to
        Layer. See :mod:`uselayer.mismatch`.
        """
        if self.mode == "paper":
            self._settle_from_venues()
        return self._check_mismatches()

    def _check_mismatches(self) -> list[ResolutionMismatch]:
        """Check every hedged pair's results, save new or changed mismatches and alert on them; return all saved."""
        pairs = hedged_pairs(self._store.fills())
        if self.mode == "live":
            self._read_venue_payouts(pairs)
            results = from_venue_payouts(self._store.venue_payouts())
        else:
            results = from_settlements(self._store.settlements())
        now = self._now()
        before = {m.group_id: m for m in self._store.mismatches()}
        for m in check(pairs, results, now=now, mode=self.mode, previous=before):
            old = before.get(m.group_id)
            if old is None or (old.kind, old.paid, old.pending) != (m.kind, m.paid, m.pending):
                self._store.save_mismatch(m, at=now)
                self._alert("resolution_mismatch", mismatch=m.to_dict())
        return self._store.mismatches()

    def _read_venue_payouts(self, pairs: dict[str, Any]) -> None:
        """Live: ask each venue what a hedged pair's markets paid, at most once a minute each, until it answers.

        A payout is dated when the venue says the market settled (Kalshi), else when it was first seen.
        """
        known = {(v, m) for v, m, _, _ in self._store.venue_payouts()}
        markets = dict.fromkeys(k[:2] for legs in pairs.values() for k, _ in legs)
        for venue, market in markets:
            if (venue, market) in known:
                continue
            now = self._now()
            last = self._settle_checked.get((venue, market))
            if last is not None and (now - last).total_seconds() < 60:
                continue
            reader = getattr(self._venues.get(venue), "payout", None)
            if reader is None:
                continue
            self._settle_checked[(venue, market)] = now
            try:
                paid = reader(market)
            except VenueError as e:
                self._alert("settlement_check_failed", venue=venue, market=market, error=e.to_dict())
                continue
            if paid is not None:
                at = min(paid.at or now, now)
                self._store.add_venue_payout(venue, market, paid.yes, at=at, seen_at=now)

    def _settle_from_venues(self, *, force: bool = False) -> list[SimulatedSettlement]:
        """Paper: ask each venue whether the markets of open positions have settled, at most once a minute each.

        A payout is dated when the venue says the market settled (Kalshi), else when it was noticed,
        and never before the position's last fill.
        """
        now = self._now()
        out: list[SimulatedSettlement] = []
        fills = self._store.fills()
        for venue, market in dict.fromkeys((p.venue, p.market) for p in self._ledger(now).positions):
            last = self._settle_checked.get((venue, market))
            if not force and last is not None and (now - last).total_seconds() < 60:
                continue
            reader = getattr(self._venues.get(venue), "payout", None)
            if reader is None:
                continue
            self._settle_checked[(venue, market)] = now
            try:
                paid = reader(market)
            except VenueError as e:
                self._alert("settlement_check_failed", venue=venue, market=market, error=e.to_dict())
                continue
            if paid is None:
                continue
            yes = paid.yes
            outcome: Literal["yes", "no", "void"] = "yes" if yes == 1 else "no" if yes == 0 else "void"
            at = min(paid.at or now, now)
            last_fill = max((f.at for f in fills if (f.venue, f.market) == (venue, market)), default=at)
            with self._lock:
                out.extend(
                    self._resolve(
                        Resolution(
                            venue=venue, market=market, outcome=outcome, payout=yes, as_of=max(at, last_fill)
                        )
                    )
                )
        return out

    def pnl(self) -> Pnl:
        """Profit and loss per position and in total: realized, unrealized at the bid, and fees.

            p = client.pnl()
            p.net, p.realized, p.unrealized, p.fees     # dollars
            p.resolution_mismatch_loss                   # of realized: lost to pairs the venues settled differently
            p.missing_marks                              # open positions with no bid to value them at
            for r in p.rows: r.market, r.side, r.contracts, r.realized, r.unrealized, r.fees, r.outcome

        Paper and backtest: from this mode's fills and settlements, every position ever held (paper
        mode pays out settled markets first). Live: what each venue reports for your positions,
        settled ones included. Open positions are valued at the best bid of a fresh book (backtest:
        the latest replayed book), the mark ``max_daily_loss`` uses. What hedged pairs lost because
        the venues settled them differently is broken out as ``resolution_mismatch_loss``, already
        inside ``realized`` (see :meth:`resolution_mismatches`). See :mod:`uselayer.pnl`.
        """
        if self.mode == "live":
            return self._pnl_live()
        if self.mode == "paper":
            self._settle_from_venues()
        now = self._now()
        rows = []
        for p in self._ledger(now).rows:
            mark = self._mark(p.venue, p.market, p.side) if p.contracts > 1e-9 else None
            rows.append(
                PnlRow(
                    venue=p.venue,
                    market=p.market,
                    side=p.side,
                    contracts=round(p.contracts, 6),
                    cost=round(p.cost, 6),
                    realized=round(p.realized, 6),
                    unrealized=None if mark is None else round(mark[0] * p.contracts - p.cost, 6),
                    fees=round(p.fees_total, 6),
                    mark=None if mark is None else mark[0],
                    mark_as_of=None if mark is None else mark[1],
                    settled=p.outcome is not None,
                    outcome=p.outcome,
                    group_id=p.group_id,
                )
            )
        return Pnl(self.mode, now, tuple(rows), tuple(self._check_mismatches()))

    def _pnl_live(self) -> Pnl:
        rows = []
        for adapter in self._live.values():
            for p in adapter.positions(include_closed=True):
                held_now = p.contracts > 1e-9 and not p.settled
                mark = self._mark(p.venue, p.market, p.side) if held_now else None
                rows.append(
                    PnlRow(
                        venue=p.venue,
                        market=p.market,
                        side=p.side,
                        contracts=round(p.contracts, 6) if held_now else 0.0,
                        cost=p.cost,
                        realized=p.realized_pnl or 0.0,
                        unrealized=None
                        if mark is None or p.cost is None
                        else round(mark[0] * p.contracts - p.cost, 6),
                        fees=p.fees,
                        mark=None if mark is None else mark[0],
                        mark_as_of=None if mark is None else mark[1],
                        settled=p.settled,
                    )
                )
        return Pnl(self.mode, self._now(), tuple(rows), tuple(self._check_mismatches()))

    def _mark(self, venue: str, market: str, side: str) -> tuple[float, datetime] | None:
        """The best bid for one side of a market, from a fresh book (backtest: the latest replayed one)."""
        b = self._books.get((venue, market))
        stale = b is None or b.age_s(self._now()) > self._guard.config.max_quote_age_s
        if self.mode != "backtest" and stale:
            try:
                b = self.book(market, venue=venue)
            except VenueError as e:
                self._alert("mark_missing", venue=venue, market=market, error=e.to_dict())
                return None
        if b is None:
            return None
        bid = b.outcome("yes" if side == "yes" else "no").best_bid
        return None if bid is None else (bid.price, b.as_of)

    def balances(self) -> dict[str, Balance]:
        """Each venue's balance, from the venues in live mode. Paper and backtest have no money to report."""
        return {venue: adapter.balance() for venue, adapter in self._live.items()}

    def decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The latest rule decisions from the journal, newest first."""
        return self._store.decisions(limit=limit)

    def sync(self) -> None:
        """Bring the local store up to date with the venues: order states and new fills.

        Live mode re-reads every order the store thinks is open, and reports open orders on the venue
        that this SDK didn't send as an outside change. Paper and backtest only expire old orders.
        """
        if self.mode != "live":
            self._paper.expire(at=self._now())
            return
        for o in self._store.orders(open_only=True):
            adapter = self._live.get(o.venue)
            if adapter is None or not o.venue_order_id:
                continue
            try:
                updated, fills = adapter.refresh(o)
            except VenueError as e:
                self._alert("sync_failed", order=o.to_dict(), error=e.to_dict())
                continue
            for f in fills:
                self._store.add_fill(f)
            self._store.save_order(updated)
        known = {o.venue_order_id for o in self._store.orders()}
        for venue, adapter in self._live.items():
            try:
                outside = [o for o in adapter.open_orders() if o.venue_order_id not in known]
            except VenueError as e:
                self._alert("sync_failed", venue=venue, error=e.to_dict())
                continue
            for o in outside:
                self._store.journal(
                    at=self._now(),
                    order_id=o.venue_order_id,
                    rule=None,
                    result="outside_change",
                    reason="An open order on the venue that this SDK didn't send.",
                    inputs=o.to_dict(),
                    config_hash=self._guard.fingerprint,
                )
                self._alert("outside_order", venue=venue, order=o.to_dict())

    def reconcile(self, *, since: datetime | None = None, repair: bool = False) -> Reconciliation:
        """Compare the local store with what each venue reports, and list every difference. Live mode only.

            r = client.reconcile()
            r.ok                       # the store and every venue agree
            for m in r.mismatches:     # missed_fill, unknown_fill, outside_fill, position, outside_order, stale_order
                print(m.kind, m.venue, m.market, m.message)

        The guardrails count positions from the store, so a fill the store missed or a trade made outside
        the SDK means they check the wrong numbers. Reading only: nothing in the store changes, and any
        mismatch is also sent to ``on_alert``.

        Args:
            since: compare fills from this time on. Default: a minute before the store's first live order
                (7 days back for an empty store). Positions and open orders are compared as they are now.
            repair: run :meth:`sync`, then add the fills the venue reported for SDK orders that the store
                had missed, journaled as ``reconcile_repair``. Fills of orders the SDK didn't send are never
                added: they stay reported for you to decide on. ``repaired`` lists every fill added, by
                ``sync()`` or here; ``mismatches`` lists what's still different.
        """
        if self.mode != "live":
            raise VenueError(
                "not_available",
                f"reconcile() compares the live store with the venues; this client is in {self.mode} mode.",
                retryable=False,
                hint="Paper and backtest fills are made by the SDK itself, so there's nothing to compare them with.",
                next="Client(mode='live').reconcile()",
            )
        now = self._now()
        if since is None:
            first = min((o.created_at for o in self._store.orders() if o.created_at), default=None)
            since = first - timedelta(minutes=1) if first is not None else now - timedelta(days=7)
        repaired: list[Fill] = []
        if repair:
            had = {f.model_dump_json() for f in self._store.fills()}
            self.sync()
            repaired = [
                f for f in self._store.fills() if isinstance(f, Fill) and f.model_dump_json() not in had
            ]
        mismatches, checked, settled = compare(self._store, self._live, since=since)
        if repair:
            for f in missed_fills_to_add(mismatches):
                if self._store.add_fill(f):
                    repaired.append(f)
                    self._store.journal(
                        at=now,
                        order_id=f.order_id,
                        rule=None,
                        result="reconcile_repair",
                        reason="A fill the venue reported for this order, missing from the store, added by reconcile(repair=True).",
                        inputs=f.to_dict(),
                        config_hash=self._guard.fingerprint,
                    )
            if repaired:
                mismatches, checked, settled = compare(self._store, self._live, since=since)
        if mismatches:
            kinds: dict[str, int] = {}
            for m in mismatches:
                kinds[m.kind] = kinds.get(m.kind, 0) + 1
            self._alert("reconcile_mismatch", count=len(mismatches), kinds=kinds)
        return Reconciliation(
            as_of=now,
            since=since,
            venues=tuple(self._live),
            mismatches=tuple(mismatches),
            checked=checked,
            settled=tuple(settled),
            repaired=tuple(repaired),
        )

    def _startup(self) -> None:
        """Live start: if the local store is new but a venue shows open orders or positions, start killed."""
        busy = []
        for venue, adapter in self._live.items():
            try:
                if adapter.open_orders() or any(not p.settled for p in adapter.positions()):
                    busy.append(venue)
            except VenueError as e:
                self._alert("startup_check_failed", venue=venue, error=e.to_dict())
        if busy and not self._store.existed and not self._store.killed():
            self._store.set_killed(
                True, by=f"startup: no local store, but {', '.join(busy)} shows open orders or positions"
            )
            self._alert("started_killed", venues=busy)

    # ---- the kill switch ----

    def _press_kill(self, by: str) -> None:
        self._store.set_killed(True, by=by)
        if self.mode == "live":
            self.cancel_all()
        else:
            self._paper.cancel_all(at=self._now())
        self._alert("killed", by=by)

    def kill(self, *, flatten: bool = False) -> list[Order]:
        """Press the kill switch: cancel every resting order and block every new one.

        With ``flatten=True``, also send orders to close every position (at most ``price_collar``
        below the bid). It stays on, even after a restart, until someone runs ``python -m uselayer resume``.

            client.kill()
        """
        self._press_kill("client.kill()")
        out: list[Order] = []
        if flatten:
            collar = self._guard.config.price_collar
            for p in self.positions():
                try:
                    b = self.book(p.market, venue=p.venue).outcome("yes" if p.side == "yes" else "no")
                    if b.best_bid is None:
                        self._alert("flatten_no_bid", venue=p.venue, market=p.market, side=p.side)
                        continue
                    tick = self.market(p.market, venue=p.venue).tick_size
                    price = max(_floor_tick(b.best_bid.price - collar, tick), tick)
                    o = Order(
                        venue=p.venue,
                        market=p.market,
                        side=p.side,
                        action="sell",
                        price=price,
                        size=p.contracts,
                        tif="ioc",
                        group_id=getattr(p, "group_id", None),
                        reason="kill",
                    )
                    out.append(self.send(o))
                except VenueError as e:
                    self._alert("flatten_failed", venue=p.venue, market=p.market, error=e.to_dict())
        return out

    @property
    def killed(self) -> bool:
        """Whether the kill switch is on."""
        return self._store.killed()

    # ---- watching open positions ----

    def monitor(
        self, *, once: bool = True, interval_s: float = 1.0, stop: Callable[[], bool] | None = None
    ) -> list[Order]:
        """Check open positions against stop-loss and take-profit, and fill resting paper orders.

        With ``once=True`` (default) it runs one pass. Otherwise it runs every ``interval_s`` seconds
        until ``stop()`` returns true or the kill switch is pressed.

            client.monitor()
        """
        sent: list[Order] = []
        while True:
            sent.extend(self._monitor_pass())
            if once or self._store.killed() or (stop and stop()):
                return sent
            self._sleep(interval_s)

    def feed(self, event: MarketEvent) -> list[Order]:
        """Paper mode: hand the fill model a live book, book change or trade. Returns orders it changed.

        :meth:`monitor` reads books only, so a resting paper order fills only when a book crosses its
        price. Trades from the venue's stream also fill it once the estimated line ahead of it is used
        up. Pass this as ``on_event`` to :func:`~uselayer.record.record_stream`:

            record_stream(["some-slug"], "ticks.jsonl", on_event=client.feed)
        """
        if self.mode != "paper":
            raise VenueError(
                "not_available",
                "feed() is for paper mode; backtest mode replays its events with replay().",
                retryable=False,
                next="client.replay()" if self.mode == "backtest" else "Client(mode='paper')",
            )
        with self._lock:
            settings = {event.market: self._settings(event.venue, event.market)}
            if isinstance(event, TradePrint):
                return self._paper.on_trade(event, settings, at=self._now())
            if isinstance(event, BookLevelChange):
                current = self._paper.last_books.get((event.venue, event.market))
                if current is None:
                    return []  # a change with no full book before it can't be applied
                book = reconstruct_book([current, event])
                assert book is not None
            elif isinstance(event, Book):
                book = event
            else:
                return []
            return self._paper.on_book(book, settings, at=self._now())

    def _monitor_pass(self) -> list[Order]:
        if self.mode == "live":
            self.sync()
        elif self.mode != "backtest":
            self._settle_from_venues()
            for o in self._store.orders(open_only=True):
                try:
                    b = self.book(o.market, venue=o.venue)
                    self._paper.on_book(b, {o.market: self._settings(o.venue, o.market)}, at=self._now())
                except VenueError as e:
                    self._alert("book_read_failed", market=o.market, error=e.to_dict())
        self._paper.expire(at=self._now())
        if self._store.killed():
            return []
        ctx = self._context(None, refresh_marks=True)
        groups: dict[str, list[PositionView]] = {}
        for p in ctx.positions:
            groups.setdefault(p.group_id or f"{p.venue}:{p.market}:{p.side}", []).append(p)
        sent = []
        for gid, ps in groups.items():
            for exit_order in self._guard.watch(
                Group(gid if ps[0].group_id else None, tuple(ps)), ctx.marks, ctx
            ):
                try:
                    sent.append(self.send(exit_order))
                    self._alert("exit_sent", order=exit_order.to_dict())
                except VenueError as e:
                    self._alert("exit_failed", order=exit_order.to_dict(), error=e.to_dict())
        return sent

    # ---- the context rules see ----

    def _day_start(self) -> datetime:
        tz = ZoneInfo(self._guard.config.day_timezone)
        local = self._now().astimezone(tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _context(self, order: Order | None, *, refresh_marks: bool = False) -> Context:
        now = self._now()
        day_start = self._day_start()
        ledger = self._ledger(day_start)
        positions = tuple(
            PositionView(
                p.venue,
                p.market,
                p.side,
                p.contracts,
                round(p.cost / p.contracts, 6),
                p.cost,
                p.fees,
                p.group_id,
            )
            for p in ledger.positions
        )
        needs_marks = refresh_marks or any(isinstance(r, MaxDailyLossRule) for r in self._guard.rules)
        if needs_marks and self.mode != "backtest":
            age_limit = self._guard.config.max_quote_age_s
            for p in positions:
                b = self._books.get((p.venue, p.market))
                if b is None or b.age_s(now) > age_limit:
                    try:
                        self.book(p.market, venue=p.venue)
                    except VenueError as e:
                        self._alert("mark_missing", venue=p.venue, market=p.market, error=e.to_dict())
        marks: dict[tuple[str, str, str], Mark] = {}
        for (venue, mid), b in self._books.items():
            for side in ("yes", "no"):
                ob = b.outcome(side)
                marks[(venue, mid, side)] = Mark(
                    ob.best_bid.price if ob.best_bid else None,
                    ob.best_ask.price if ob.best_ask else None,
                    b.as_of,
                )
        exposure: dict[str, float] = {"total": 0.0}
        for p in positions:
            k = risk_key(p.venue, p.market, p.group_id)
            exposure[k] = exposure.get(k, 0.0) + p.cost + max(p.fees, 0.0)
            exposure["total"] += p.cost + max(p.fees, 0.0)
        for o in self._store.orders(open_only=True):
            r = order_risk(o)
            k = risk_key(o.venue, o.market, o.group_id)
            exposure[k] = exposure.get(k, 0.0) + r
            exposure["total"] += r
        # Today's P&L: realized − fees + open positions at the bid, from the start-of-day mark when held overnight.
        unrealized = 0.0
        missing: list[str] = []
        day = day_start.date().isoformat()
        stored = {(p.venue, p.market, p.side): p for p in ledger.positions}
        for p in positions:
            m = marks.get((p.venue, p.market, p.side))
            age = None if m is None else m.age_s(now)
            fresh = (
                m is not None
                and m.bid is not None
                and age is not None
                and age <= self._guard.config.max_quote_age_s
            )
            bid = m.bid if fresh and m is not None and m.bid is not None else 0.0
            if not fresh:
                missing.append(f"{p.venue}:{p.market}:{p.side}")
            opened = stored[(p.venue, p.market, p.side)].opened_at
            if opened is not None and opened < day_start:
                sod = self._store.day_mark(day, p.venue, p.market, p.side)
                if sod is None and fresh:
                    self._store.set_day_mark(day, p.venue, p.market, p.side, bid)
                    sod = bid
                basis = p.contracts * sod if sod is not None else p.cost
            else:
                basis = p.cost
            unrealized += bid * p.contracts - basis
        if missing and needs_marks:
            self._alert("stale_or_missing_marks", positions=missing)
        pnl_today = ledger.realized_today - ledger.fees_today + unrealized
        return Context(
            now=now,
            positions=positions,
            exposure=exposure,
            marks=marks,
            pnl_today=pnl_today,
            realized_today=ledger.realized_today - ledger.fees_today,
            killed=self._store.killed(),
            config_hash=self._guard.fingerprint,
            missing_marks=tuple(missing),
        )

    # ---- backtest ----

    def replay(self, on_book: Callable[[Client, Book], None] | None = None) -> dict[str, Any]:
        """Backtest mode: replay the books given to ``Client(books=...)``, oldest first.

        For each book, the clock moves to the book's time, resting orders that the book reaches are
        filled, and ``on_book(client, book)`` runs, where you can send orders as usual. A
        ``book_change`` (e.g. from :func:`~uselayer.imports.import_events`) is applied to the market's
        last book and replayed as a book. A ``gap`` (from :func:`~uselayer.record.record_stream`) clears
        the market's book until the next one arrives. A ``resolution`` pays out the market's positions
        (see :meth:`settle`) and closes it to new orders. A ``trade`` fills resting orders it reaches
        once the estimated line ahead of them at their price is used up (see :mod:`uselayer.resting`).

            bt = Client(mode="backtest", books=my_saved_books)
            bt.replay(lambda c, b: c.buy(venue=b.venue, market=b.market, side="yes", price=0.4, size=5))
        """
        if self.mode != "backtest" or self._replay_clock is None:
            raise VenueError(
                "not_available",
                "replay() is for backtest mode.",
                retryable=False,
                next="Client(mode='backtest', books=...)",
            )
        events = sorted(self._events, key=lambda e: e.as_of)
        self._replay_events = events
        books = trades = gaps = resolutions = 0
        for i, ev in enumerate(events):
            self._replay_i = i
            self._replay_clock.now = ev.as_of
            if isinstance(ev, Resolution):
                resolutions += 1
                self._resolve(ev)
                continue
            if isinstance(ev, StreamGap):
                # Nothing is known about this market until the next book after the gap.
                gaps += 1
                self._books.pop((ev.venue, ev.market), None)
                continue
            if isinstance(ev, TradePrint):
                trades += 1
                self._paper.on_trade(ev, {ev.market: self._settings(ev.venue, ev.market)}, at=ev.as_of)
                continue
            if isinstance(ev, BookLevelChange):
                current = self._books.get((ev.venue, ev.market))
                if current is None:
                    continue  # a change with no full book before it can't be applied
                e = reconstruct_book([current, ev])
                assert e is not None
            elif isinstance(ev, Book):
                e = ev
            else:
                continue
            books += 1
            self._books[(e.venue, e.market)] = e
            self.market(e.market, venue=e.venue)
            self._paper.on_book(e, {e.market: self._settings(e.venue, e.market)}, at=e.as_of)
            self._paper.expire(at=e.as_of)
            if on_book is not None:
                on_book(self, e)
        return {
            "books": books,
            "trades": trades,
            "gaps": gaps,
            "resolutions": resolutions,
            "fills": len(self._store.fills()),
            "settlements": len(self._store.settlements()),
            "positions": [p.to_dict() for p in self.positions()],
        }


class Admin:
    """The human's controls: resume after a kill and read the journal. Not for strategies or agents.

    Admin(mode="paper").resume()
    """

    def __init__(self, *, mode: Mode = "paper", store: str | Path | None = None) -> None:
        self.mode = mode
        self._store = Store(store if store is not None else default_path(mode))

    def resume(self) -> None:
        """Turn the kill switch off."""
        self._store.set_killed(False, by="admin resume")

    def kill(self) -> None:
        """Press the kill switch from outside the running program."""
        self._store.set_killed(True, by="admin kill")

    def status(self) -> dict[str, Any]:
        """The kill switch and the store's counts."""
        return {
            "mode": self.mode,
            "store": self._store.path,
            "killed": self._store.killed(),
            "kill_info": self._store.kill_info(),
            "open_orders": len(self._store.orders(open_only=True)),
            "fills": len(self._store.fills()),
        }

    def decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._store.decisions(limit=limit)


def _blocked(rule: str, reason: str) -> VenueError:
    if rule == "kill_switch":
        return VenueError(
            "blocked_by_rule",
            reason,
            rule=rule,
            retryable=False,
            hint="The kill switch is on. Only a person can turn it off; a strategy or agent can't.",
            next="A person runs: python -m uselayer resume",
        )
    return VenueError(
        "blocked_by_rule",
        reason,
        rule=rule,
        retryable=False,
        hint=f"The {rule} rule stopped this order before it was sent.",
        next="client.preview(order) shows every rule's decision.",
    )


def _floor_tick(price: float, tick: float) -> float:
    units = round(price * 1_000_000)
    step = round(tick * 1_000_000)
    return (units // step) * step / 1_000_000


def _resolve(market: Any, venue: str) -> tuple[str, str]:
    """A market given as a string, or as a Market from Layer (which names its own venue)."""
    if isinstance(market, str):
        return venue, market
    v = getattr(market, "venue", None)
    mid = getattr(market, "market_id", None)
    if isinstance(v, str) and isinstance(mid, str):
        return v, mid
    raise VenueError(
        "invalid_order",
        "market must be a venue id string or a Market from client.matches().",
        retryable=False,
    )
