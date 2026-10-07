"""Import order-book history you already have, and check it before a backtest trusts it.

:func:`import_events` turns a file into the SDK's market events (``book``, ``book_change``, ``trade``,
``status``, ``resolution``; see ``docs/events.md``), ready for ``Client(mode="backtest", books=...)``:

    data = import_events("ticks.csv", venue="kalshi", columns={"time": "ts", "market": "ticker"})
    bt = Client(mode="backtest", books=data)
    bt.replay(on_book)

Formats:

- ``"csv"`` and ``"parquet"``: one row per event, with ``columns`` mapping the SDK's field names to
  yours (Parquet needs ``pip install 'uselayer[parquet]'``).
- ``"jsonl"``: the SDK's own event format, as :func:`~uselayer.backtest.save_events` writes it.
- ``"polymarket_us"``, ``"polymarket"``, ``"kalshi"``: the raw messages of each venue's market-data
  WebSocket, one JSON message per line, optionally wrapped as ``{"received_at": ..., "message": ...}``.
- ``"pmxt"``: PMXT's hourly Polymarket order-book Parquet files (both of their schema versions).

Every import is checked (:func:`check_events`): gaps, rows out of time order, crossed or impossible
books and prices off the tick size. Problems that would make a backtest wrong (``severity="error"``)
raise :class:`~uselayer.errors.VenueError` ``bad_data`` unless you pass ``strict=False``; the full
report is on ``data.report`` either way.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from .books import Book, BookLevelChange, Level, reconstruct_book
from .errors import VenueError
from .events import MarketEvent, MarketStatus, Resolution, TradePrint

Format = Literal["csv", "parquet", "jsonl", "polymarket_us", "polymarket", "kalshi", "pmxt"]
Side = Literal["yes", "no"]

FIELDS = (
    "time",
    "received_at",
    "venue",
    "market",
    "kind",
    "book_side",
    "price",
    "size",
    "bid",
    "bid_size",
    "ask",
    "ask_size",
    "bids",
    "asks",
    "status",
    "outcome",
    "trade_id",
    "aggressor",
)
"""Field names ``columns=`` can map for CSV and Parquet files."""

# The finest tick each venue uses anywhere. Used when the data doesn't say a market's tick size.
FINEST_TICK: dict[str, float] = {"polymarket_us": 0.001, "polymarket": 0.001, "kalshi": 0.001}

MAX_EXAMPLES = 20  # examples kept per problem kind; ``counts`` has the full numbers


def _apply(book: Book, change: BookLevelChange) -> Book:
    out = reconstruct_book([book, change])
    assert out is not None
    return out


# ---- the report ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """One thing wrong with the data.

    ``kind`` is one of ``unreadable``, ``impossible``, ``crossed``, ``off_tick``, ``sequence_gap``
    (errors), or ``out_of_order``, ``gap``, ``stale_book``, ``no_starting_book``, ``repaired`` (warnings).
    ``row`` is the 1-based row (or line) in the file, when it came from one.
    """

    kind: str
    severity: Literal["error", "warning"]
    message: str
    row: int | None = None
    venue: str | None = None
    market: str | None = None
    at: datetime | None = None
    file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "row": self.row,
            "venue": self.venue,
            "market": self.market,
            "at": self.at.isoformat() if self.at else None,
            "file": self.file,
        }


ERRORS = frozenset({"unreadable", "impossible", "crossed", "off_tick", "sequence_gap"})
"""Problem kinds that make a replay wrong. The rest are warnings: worth reading, but a replay of what's
there is still faithful."""


@dataclass
class CheckReport:
    """What :func:`check_events` found. ``ok`` is ``True`` when there are no errors (warnings allowed).

    report = check_events(events)
    print(report.summary())
    """

    events: int = 0
    markets: int = 0
    start: datetime | None = None
    end: datetime | None = None
    counts: Counter[str] = field(default_factory=Counter)
    problems: list[Problem] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(n for k, n in self.counts.items() if k in ERRORS)

    @property
    def warnings(self) -> int:
        return sum(n for k, n in self.counts.items() if k not in ERRORS)

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def add(self, p: Problem) -> None:
        self.counts[p.kind] += 1
        if self.counts[p.kind] <= MAX_EXAMPLES:
            self.problems.append(p)

    def summary(self) -> str:
        """A few lines a person can read: the span, then each problem kind with its first example."""
        span = (
            f"{self.start.isoformat()} → {self.end.isoformat()}" if self.start and self.end else "no events"
        )
        lines = [f"{self.events} events, {self.markets} markets, {span}."]
        if not self.counts:
            lines.append("No problems found.")
        for kind, n in sorted(self.counts.items(), key=lambda kv: (kv[0] not in ERRORS, kv[0])):
            first = next(p for p in self.problems if p.kind == kind)
            where = f" (row {first.row})" if first.row is not None else ""
            if first.file:
                where = f" ({first.file}{', row ' + str(first.row) if first.row is not None else ''})"
            sev = "error" if kind in ERRORS else "warning"
            lines.append(f"{sev} {kind} ×{n}: {first.message}{where}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "events": self.events,
            "markets": self.markets,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "errors": self.errors,
            "warnings": self.warnings,
            "counts": dict(self.counts),
            "problems": [p.to_dict() for p in self.problems],
        }


@dataclass
class Imported:
    """Imported events (oldest first) and the checker's report. Pass it straight to a backtest:

    data = import_events("ticks.csv", venue="kalshi")
    Client(mode="backtest", books=data).replay(on_book)
    """

    events: list[MarketEvent]
    report: CheckReport

    def __iter__(self) -> Iterator[MarketEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, i: int) -> MarketEvent:
        return self.events[i]


# ---- the checker --------------------------------------------------------------------------------


def _off_tick(price: float, tick: float) -> bool:
    units = price / tick
    return abs(units - round(units)) > 1e-6


def check_events(
    events: Iterable[MarketEvent],
    *,
    tick_size: float | Mapping[str, float] | None = None,
    max_gap_s: float | None = None,
    rows: list[int | None] | None = None,
    files: list[str | None] | None = None,
    report: CheckReport | None = None,
    tick_changes: Iterable[tuple[str, str, datetime, float]] = (),
) -> CheckReport:
    """Check market events, in the order given (file order), before a replay trusts them.

        report = check_events(load_books("books.jsonl"), tick_size=0.01)
        report.ok, report.summary()

    Errors: impossible values (a price not between 0 and 1, a size that isn't a positive finite
    number), books whose best bid is at or above the best ask, prices off the tick size.
    Warnings: events that go back in time for their market, gaps (silences much longer than the
    data's usual spacing, and at least a minute), and level changes before a market's first book.

    Args:
        tick_size: one tick for every market, or ``{market: tick}``. Without it, ticks come from
            ``tick_changes`` (e.g. Polymarket's ``tick_size_change`` messages) or else the venue's
            finest tick (0.001), which still catches unit mistakes like cents read as dollars.
        max_gap_s: report silences longer than this. Default: 10 × the median spacing, at least 60 s.
        rows, files: each event's row (1-based) and file, for the report.
    """
    rep = report if report is not None else CheckReport()
    evs = list(events)
    rows = rows if rows is not None else [None] * len(evs)
    ticks_by_market: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for venue, market, at, tick in tick_changes:
        ticks_by_market.setdefault((venue, market), []).append((at, tick))

    def tick_for(e: MarketEvent) -> float:
        if isinstance(tick_size, (int, float)):
            return float(tick_size)
        if isinstance(tick_size, Mapping) and e.market in tick_size:
            return float(tick_size[e.market])
        tick = FINEST_TICK.get(e.venue, 0.001)
        for at, t in ticks_by_market.get((e.venue, e.market), ()):
            if at <= e.as_of:
                tick = t
        return tick

    files = files if files is not None else [None] * len(evs)
    markets = {(e.venue, e.market) for e in evs}

    def problem(i: int, kind: str, msg: str) -> None:
        e = evs[i]
        sev: Literal["error", "warning"] = "error" if kind in ERRORS else "warning"
        rep.add(Problem(kind, sev, msg, rows[i], e.venue, e.market, e.as_of, files[i]))

    # Pass 1, in file order: does the file's own clock go backwards? That's the receive time when
    # events have one (a stream batches messages, so venue times inside one batch can be shuffled,
    # and the replay sorts those), else the venue time.
    last: dict[tuple[str, str], datetime] = {}
    for i, e in enumerate(evs):
        key = (e.venue, e.market)
        recv = getattr(e, "received_at", None)  # a recorder's gap has only its own clock
        clock = recv or e.as_of
        prev = last.get(key)
        if prev is not None and clock < prev:
            what = "received" if recv else "stamped"
            problem(
                i,
                "out_of_order",
                f"{e.kind} {what} at {clock.isoformat()} comes after one at {prev.isoformat()}.",
            )
        else:
            last[key] = clock

    # Pass 2, in the order a replay uses (venue time, file order on ties): values, ticks and books.
    books: dict[tuple[str, str], Book] = {}
    crossed: set[tuple[str, str]] = set()
    order = sorted(range(len(evs)), key=lambda i: evs[i].as_of)
    for i in order:
        e = evs[i]
        key = (e.venue, e.market)
        prices: list[float] = []
        sizes: list[float] = []
        if (
            str(e.kind) == "gap"
        ):  # a recorder's marked gap (record_stream): the book is unknown until the next one
            until = getattr(e, "until", e.as_of)
            problem(
                i,
                "gap",
                f"the recording has a gap for {e.market} from {e.as_of.isoformat()} to {until.isoformat()}.",
            )
            books.pop(key, None)
            crossed.discard(key)
            continue
        if isinstance(e, Book):
            prices = [lv.price for lv in (*e.bids, *e.asks)]
            sizes = [lv.size for lv in (*e.bids, *e.asks)]
        elif isinstance(e, (BookLevelChange, TradePrint)):
            prices, sizes = [e.price], [e.size]
        bad = [x for x in sizes if not math.isfinite(x) or x < 0]
        if bad:
            problem(i, "impossible", f"size {bad[0]} isn't a finite number of contracts.")
            continue
        tick = tick_for(e)
        off = [x for x in prices if _off_tick(x, tick)]
        if off:
            problem(i, "off_tick", f"price {off[0]} isn't a multiple of the {tick} tick for {e.market}.")

        if isinstance(e, Book):
            books[key] = e
        elif isinstance(e, BookLevelChange):
            cur = books.get(key)
            if cur is None:
                problem(i, "no_starting_book", f"a level change for {e.market} comes before any full book.")
                continue
            books[key] = _apply(cur, e)
        else:
            continue
        bk = books[key]
        if bk.bids and bk.asks and bk.bids[0].price >= bk.asks[0].price:
            if key not in crossed:
                crossed.add(key)
                problem(
                    i,
                    "crossed",
                    f"{e.market}'s best bid {bk.bids[0].price} is at or above its best ask {bk.asks[0].price}.",
                )
        else:
            crossed.discard(key)

    times = [evs[i].as_of for i in order]
    if times:
        rep.start, rep.end = times[0], times[-1]
        spacing = [(b - a).total_seconds() for a, b in pairwise(times)]
        limit = max_gap_s
        if limit is None:
            positive = [s for s in spacing if s > 0]
            limit = max(60.0, 10 * statistics.median(positive)) if positive else 60.0
        for a, b in pairwise(times):
            if (b - a).total_seconds() > limit:
                rep.add(
                    Problem(
                        "gap",
                        "warning",
                        f"no events for {(b - a).total_seconds():.0f} s, from {a.isoformat()} to {b.isoformat()}.",
                        at=a,
                    )
                )
    rep.events += len(evs)
    rep.markets = len(markets)
    return rep


# ---- shared parsing helpers ---------------------------------------------------------------------


class _Bad(Exception):
    """A row that can't become an event."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def to_time(v: Any) -> datetime:
    """A timestamp from a datetime, an ISO 8601 string, or a Unix number (s, ms, µs or ns, by size).

    Naive times are read as UTC.
    """
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if hasattr(v, "to_pydatetime"):  # pandas Timestamp
        return to_time(v.to_pydatetime())
    if isinstance(v, str):
        s = v.strip()
        try:
            return to_time(float(s))
        except ValueError:
            pass
        s = s.replace("Z", "+00:00").replace(" ", "T", 1)
        if "." in s:  # keep microseconds of a nanosecond timestamp
            head, rest = s.split(".", 1)
            digits = len(rest) - len(rest.lstrip("0123456789"))
            s = f"{head}.{rest[: min(digits, 6)]}{rest[digits:]}"
        try:
            return to_time(datetime.fromisoformat(s))
        except ValueError as e:
            raise _Bad("unreadable", f"{v!r} isn't a time.") from e
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        x = float(v)
        if not math.isfinite(x):
            raise _Bad("unreadable", f"{v!r} isn't a time.")
        for scale in (1e9, 1e6, 1e3):
            if abs(x) > scale * 1e8:  # beyond 1973 in that unit
                x /= scale
                break
        return datetime.fromtimestamp(x, UTC)
    raise _Bad("unreadable", f"{v!r} isn't a time.")


def _num(v: Any, what: str) -> float:
    if v is None or v == "":
        raise _Bad("unreadable", f"{what} is missing.")
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise _Bad("unreadable", f"{what} {v!r} isn't a number.") from e
    return x


def _price(v: Any, what: str, scale: float = 1.0) -> float:
    x = round(_num(v, what) * scale, 9)
    if not 0 < x < 1:
        raise _Bad("impossible", f"{what} {x} isn't between 0 and 1 dollars.")
    return x


def _levels(raw: Any, scale: float = 1.0) -> tuple[Level, ...]:
    """Levels from ``[[price, size], ...]``, ``[{"price":..,"size":..}]``, or a JSON string of either."""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            try:
                raw = json.loads(raw.replace("'", '"'))  # Python-repr lists, as PMXT's first schema has
            except json.JSONDecodeError as e:
                raise _Bad("unreadable", f"book levels {raw[:80]!r} aren't a list.") from e
    out = []
    for lv in raw:
        if isinstance(lv, Mapping):
            p, s = lv.get("price"), lv.get("size")
        else:
            p, s = lv[0], lv[1]
        size = _num(s, "size")
        if size == 0:
            continue
        if size < 0 or not math.isfinite(size):
            raise _Bad("impossible", f"a book level has size {size}.")
        out.append(Level(price=_price(p, "price", scale), size=size))
    return tuple(out)


def _mirror(lv: Level) -> Level:
    return Level(price=round(1 - lv.price, 9), size=lv.size)


_BOOK_SIDE: dict[str, Literal["bid", "ask"]] = {
    "bid": "bid",
    "bids": "bid",
    "buy": "bid",
    "b": "bid",
    "ask": "ask",
    "asks": "ask",
}
_BOOK_SIDE |= {"sell": "ask", "offer": "ask", "offers": "ask", "a": "ask", "s": "ask"}
_KIND = {"book": "book", "snapshot": "book", "book_snapshot": "book", "book_change": "book_change"}
_KIND |= {"delta": "book_change", "price_change": "book_change", "change": "book_change", "trade": "trade"}
_KIND |= {"trades": "trade", "status": "status", "resolution": "resolution", "settlement": "resolution"}
_STATUS = {"open": "open", "active": "open", "paused": "paused", "closed": "closed", "halted": "halted"}
_OUTCOME = {"yes": "yes", "no": "no", "void": "void", "1": "yes", "0": "no", "refund": "void"}


@dataclass
class _Sink:
    """Collects events and problems from a parser, in file order."""

    events: list[MarketEvent] = field(default_factory=list)
    rows: list[int | None] = field(default_factory=list)
    report: CheckReport = field(default_factory=CheckReport)
    ticks: list[tuple[str, str, datetime, float]] = field(default_factory=list)
    files: list[str | None] = field(default_factory=list)
    # The venue's own best bid/ask after a level change (YES side), when the message carries it.
    stamps: dict[int, tuple[float | None, float | None]] = field(default_factory=dict)
    file: str | None = None  # the file being read

    def event(self, e: MarketEvent, row: int | None) -> None:
        self.events.append(e)
        self.rows.append(row)
        self.files.append(self.file)

    def bad(
        self, kind: str, message: str, row: int | None, venue: str | None = None, market: str | None = None
    ) -> None:
        sev: Literal["error", "warning"] = "error" if kind in ERRORS else "warning"
        self.report.add(Problem(kind, sev, message, row, venue, market, file=self.file))

    def build(
        self,
        row: int | None,
        fn: Callable[[], MarketEvent | None],
        venue: str | None = None,
        market: str | None = None,
        stamp: tuple[float | None, float | None] | None = None,
    ) -> None:
        """Run one event constructor, turning a bad value into a problem instead of a crash."""
        try:
            e = fn()
        except _Bad as b:
            self.bad(b.kind, str(b), row, venue, market)
            return
        except ValidationError as v:
            err = v.errors()[0]
            loc = ".".join(str(x) for x in err["loc"])
            self.bad("impossible", f"{loc}: {err['msg']} (got {err.get('input')!r}).", row, venue, market)
            return
        if e is not None:
            self.event(e, row)
            if stamp is not None and stamp != (None, None):
                self.stamps[len(self.events) - 1] = stamp


# ---- tables: CSV and Parquet ---------------------------------------------------------------------


def _table_rows(
    rows: Iterable[Mapping[str, Any]],
    sink: _Sink,
    *,
    columns: Mapping[str, str],
    venue: str | None,
    market: str | None,
    kind: str | None,
    price_scale: float,
) -> None:
    unknown = set(columns) - set(FIELDS)
    if unknown:
        raise VenueError(
            "invalid_order",
            f"columns= names fields the SDK doesn't have: {sorted(unknown)}.",
            retryable=False,
            hint=f"Map these SDK fields to your column names: {', '.join(FIELDS)}.",
        )
    col = {f: columns.get(f, f) for f in FIELDS}
    for n, r in enumerate(rows, start=1):

        def get(f: str, r: Mapping[str, Any] = r) -> Any:
            v = r.get(col[f])
            return None if v == "" else v

        v = get("venue") or venue
        m = get("market") or market
        k = get("kind") or kind
        if k is None:
            if (
                get("bids") is not None
                or get("asks") is not None
                or get("bid") is not None
                or get("ask") is not None
            ):
                k = "book"
            elif get("book_side") is not None:
                k = "book_change"
            elif get("status") is not None:
                k = "status"
            elif get("outcome") is not None:
                k = "resolution"
            else:
                k = "trade"

        def make(r: Mapping[str, Any] = r, v: Any = v, m: Any = m, k: Any = k) -> MarketEvent:
            if not v or not m:
                raise _Bad(
                    "unreadable", "the row has no venue or market (pass venue= / market= or map a column)."
                )
            kk = _KIND.get(str(k).lower())
            if kk is None:
                raise _Bad("unreadable", f"kind {k!r} isn't book, book_change, trade, status or resolution.")
            t_raw, recv_raw = get("time", r), get("received_at", r)
            if t_raw is None and recv_raw is None:
                raise _Bad("unreadable", "the row has no time.")
            recv = to_time(recv_raw) if recv_raw is not None else None
            at = to_time(t_raw) if t_raw is not None else recv
            assert at is not None
            base = {"venue": str(v), "market": str(m), "as_of": at, "received_at": recv}
            if kk == "book":
                if get("bids", r) is not None or get("asks", r) is not None:
                    bids, asks = _levels(get("bids", r), price_scale), _levels(get("asks", r), price_scale)
                else:
                    bids = asks = ()
                    if get("bid", r) is not None:
                        bids = (
                            Level(
                                price=_price(get("bid", r), "bid", price_scale),
                                size=_num(get("bid_size", r), "bid_size"),
                            ),
                        )
                    if get("ask", r) is not None:
                        asks = (
                            Level(
                                price=_price(get("ask", r), "ask", price_scale),
                                size=_num(get("ask_size", r), "ask_size"),
                            ),
                        )
                return Book(bids=bids, asks=asks, source="recorded", **base)
            if kk == "book_change":
                side = _BOOK_SIDE.get(str(get("book_side", r)).lower())
                if side is None:
                    raise _Bad("unreadable", f"book_side {get('book_side', r)!r} isn't bid or ask.")
                return BookLevelChange(
                    book_side=side,
                    price=_price(get("price", r), "price", price_scale),
                    size=_num(get("size", r), "size"),
                    **base,
                )
            if kk == "trade":
                tid = get("trade_id", r)
                return TradePrint(
                    trade_id=None if tid is None else str(tid),
                    aggressor=_aggressor(get("aggressor", r)),
                    price=_price(get("price", r), "price", price_scale),
                    size=_num(get("size", r), "size"),
                    **base,
                )
            if kk == "status":
                st = _STATUS.get(str(get("status", r)).lower())
                if st is None:
                    raise _Bad(
                        "unreadable", f"status {get('status', r)!r} isn't open, paused, closed or halted."
                    )
                return MarketStatus(status=st, **base)
            out = _OUTCOME.get(str(get("outcome", r)).lower())
            if out is None:
                raise _Bad("unreadable", f"outcome {get('outcome', r)!r} isn't yes, no or void.")
            return Resolution(outcome=out, **base)

        sink.build(n, make, v, m)


def _csv_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(newline="") as f:
        yield from csv.DictReader(f)


def _pyarrow() -> Any:
    try:
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
    except ImportError as e:
        raise VenueError(
            "not_available",
            "Reading Parquet needs pyarrow, which isn't installed.",
            retryable=False,
            hint="Install the Parquet extra.",
            next="pip install 'uselayer[parquet]'",
        ) from e
    return pq


def _parquet_rows(path: Path) -> Iterator[dict[str, Any]]:
    pq = _pyarrow()
    for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
        yield from batch.to_pylist()


# ---- the SDK's own JSON lines -------------------------------------------------------------------

_MODELS: dict[str, Any] = {
    "book": Book,
    "book_change": BookLevelChange,
    "trade": TradePrint,
    "status": MarketStatus,
    "resolution": Resolution,
}


def _json_lines(path: Path, sink: _Sink) -> Iterator[tuple[int, Any]]:
    with path.open() as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                yield n, json.loads(line)
            except json.JSONDecodeError:
                sink.bad("unreadable", f"line {n} isn't JSON.", n)


def _sdk_jsonl(path: Path, sink: _Sink) -> None:
    from . import events

    models = {**_MODELS}
    if hasattr(events, "StreamGap"):  # the gap marker record_stream writes
        models["gap"] = events.StreamGap
    for n, d in _json_lines(path, sink):
        cls = models.get(d.get("kind", "book")) if isinstance(d, dict) else None
        if cls is None:
            sink.bad(
                "unreadable", f"kind {d.get('kind') if isinstance(d, dict) else d!r} isn't a market event.", n
            )
            continue

        def make(cls: Any = cls, d: Any = d) -> MarketEvent:
            e: MarketEvent = cls.model_validate(d)
            return e.model_copy(update={"source": "recorded"}) if isinstance(e, Book) else e

        sink.build(n, make, d.get("venue"), d.get("market"))


def _unwrap(d: Any) -> tuple[datetime | None, Any]:
    """``{"received_at": t, "message": m}`` → ``(t, m)``; a bare message → ``(None, message)``."""
    if isinstance(d, dict) and "message" in d:
        for k in ("received_at", "recv_ts", "received", "ts_received", "timestamp_received"):
            if d.get(k) is not None:
                return to_time(d[k]), d["message"]
        return None, d["message"]
    return None, d


# ---- polymarket.com market channel --------------------------------------------------------------


class _Tokens:
    """Polymarket outcome tokens → (market, side). Unmapped tokens are their own market (YES = the token)."""

    def __init__(self, tokens: Mapping[str, Any] | None) -> None:
        self.map: dict[str, tuple[str, Side]] = {}
        for tok, target in (tokens or {}).items():
            if isinstance(target, str):
                self.map[str(tok)] = (target, "yes")
            else:
                m, side = target
                if side not in ("yes", "no"):
                    raise VenueError(
                        "invalid_order", f"tokens[{tok!r}] side must be 'yes' or 'no'.", retryable=False
                    )
                self.map[str(tok)] = (str(m), side)

    def __call__(self, token: str) -> tuple[str, Side]:
        return self.map.get(token, (token, "yes"))


def _poly_book(
    venue: str,
    market: str,
    side: Side,
    bids: tuple[Level, ...],
    asks: tuple[Level, ...],
    at: datetime,
    recv: datetime | None,
) -> Book:
    if side == "no":  # the NO token's book, seen from YES: its asks are YES bids at 1 − p
        bids, asks = tuple(_mirror(lv) for lv in asks), tuple(_mirror(lv) for lv in bids)
    return Book(
        venue=venue, market=market, bids=bids, asks=asks, as_of=at, received_at=recv, source="recorded"
    )


def _poly_change(
    venue: str,
    market: str,
    side: Side,
    trade_side: str,
    price: float,
    size: float,
    at: datetime,
    recv: datetime | None,
) -> BookLevelChange:
    book_side = _BOOK_SIDE.get(trade_side.lower())
    if book_side is None:
        raise _Bad("unreadable", f"price change side {trade_side!r} isn't BUY or SELL.")
    if size < 0 or not math.isfinite(size):
        raise _Bad("impossible", f"a level change has size {size}.")
    if side == "no":
        book_side = "ask" if book_side == "bid" else "bid"
        price = round(1 - price, 9)
    return BookLevelChange(
        venue=venue,
        market=market,
        book_side=book_side,
        price=price,
        size=size,
        as_of=at,
        received_at=recv,
    )


def _aggressor(taker_side: Any, flip: bool = False) -> Literal["buy", "sell"] | None:
    """The taker's side (``BUY``/``SELL``, ``yes``/``no``) as what it did to YES."""
    t = str(taker_side or "").lower()
    out: Literal["buy", "sell"] | None = (
        "buy"
        if t in ("buy", "yes", "order_side_buy")
        else "sell"
        if t in ("sell", "no", "order_side_sell")
        else None
    )
    if flip and out is not None:
        return "sell" if out == "buy" else "buy"
    return out


def _poly_trade(
    venue: str,
    market: str,
    side: Side,
    price: float,
    size: float,
    at: datetime,
    recv: datetime | None,
    taker_side: Any = None,
) -> TradePrint:
    return TradePrint(
        venue=venue,
        market=market,
        price=round(1 - price, 9) if side == "no" else price,
        size=size,
        as_of=at,
        aggressor=_aggressor(taker_side, flip=side == "no"),
        received_at=recv,
    )


def _stamp(best_bid: Any, best_ask: Any, side: Side) -> tuple[float | None, float | None]:
    """Polymarket's best bid/ask after a change, for the YES book (a NO token's are mirrored)."""

    def f(v: Any) -> float | None:
        try:
            return None if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return None

    bb, ba = f(best_bid), f(best_ask)
    if side == "no":
        return (None if ba is None else round(1 - ba, 9), None if bb is None else round(1 - bb, 9))
    return bb, ba


def _repair_from_stamps(sink: _Sink) -> None:
    """Drop levels the venue's own best bid/ask says are gone.

    Polymarket stamps the best bid and ask on every level change. A level better than that stamp is
    one whose removal never reached the file (the stream drops some), and left in, it can cross the
    book. Replaying PMXT's files this way, rebuilt books match the venue's best bid/ask about 99% of
    the time, against 88-99% without it, and stop crossing.
    """
    if not sink.stamps:
        return
    order = sorted(range(len(sink.events)), key=lambda i: sink.events[i].as_of)
    books: dict[tuple[str, str], Book] = {}
    extra: dict[int, list[BookLevelChange]] = {}  # fixes to put before change i
    after: dict[int, list[BookLevelChange]] = {}  # a fix of the level change i itself set
    removed = 0
    for i in order:
        e = sink.events[i]
        key = (e.venue, e.market)
        if isinstance(e, Book):
            books[key] = e
            continue
        if not isinstance(e, BookLevelChange) or key not in books:
            continue
        book = _apply(books[key], e)
        stamp = sink.stamps.get(i)
        if stamp is not None:
            bb, ba = stamp
            gone = [("bid", lv.price) for lv in book.bids if bb is not None and lv.price > bb + 1e-9]
            gone += [("ask", lv.price) for lv in book.asks if ba is not None and lv.price < ba - 1e-9]
            for side, price in gone:
                fix = BookLevelChange(
                    venue=e.venue,
                    market=e.market,
                    book_side=side,
                    price=price,
                    size=0,
                    as_of=e.as_of,
                    received_at=e.received_at,
                )
                # Before the change, so a replay never sees the crossed book; after it only when the
                # stamp contradicts the level the change itself set.
                (after if price == e.price and side == e.book_side else extra).setdefault(i, []).append(fix)
                book = _apply(book, fix)
                removed += 1
        books[key] = book
    if not extra and not after:
        return
    events: list[MarketEvent] = []
    rows: list[int | None] = []
    files: list[str | None] = []
    for i, e in enumerate(sink.events):
        # Fixes share the change's time and sit next to it, so a replay applies them at the same moment.
        for ev in (*extra.get(i, ()), e, *after.get(i, ())):
            events.append(ev)
            rows.append(sink.rows[i])
            files.append(sink.files[i])
    sink.events, sink.rows, sink.files = events, rows, files
    e0 = next(iter((extra or after).values()))[0]
    msg = f"removed {removed} book level(s) that the venue's own best bid/ask showed were already gone."
    sink.report.counts["repaired"] += removed
    sink.report.problems.append(Problem("repaired", "warning", msg, None, e0.venue, e0.market, e0.as_of))


class _StaleBooks:
    """Drops full books whose venue time is older than what's already been seen for that market.

    Polymarket re-sends old books on reconnects (about 1 in 10 books in PMXT's files); replaying one
    would roll the book back in time.
    """

    def __init__(self) -> None:
        self.latest: dict[str, datetime] = {}

    def stale(self, market: str, at: datetime) -> bool:
        last = self.latest.get(market)
        return last is not None and at < last

    def seen(self, market: str, at: datetime) -> None:
        if at > self.latest.get(market, at - timedelta(seconds=1)):
            self.latest[market] = at


def _polymarket_message(
    msg: Any,
    n: int,
    recv: datetime | None,
    sink: _Sink,
    tokens: _Tokens,
    keep: Callable[[str, str], bool],
    stale: _StaleBooks,
) -> None:
    if isinstance(msg, list):
        for m in msg:
            _polymarket_message(m, n, recv, sink, tokens, keep, stale)
        return
    if not isinstance(msg, dict):
        return
    et = msg.get("event_type")
    venue = "polymarket"
    cond = str(msg.get("market") or "")

    def when(m: Mapping[str, Any]) -> datetime:
        t = m.get("timestamp")
        if t is not None:
            return to_time(t)
        if recv is not None:
            return recv
        raise _Bad("unreadable", "the message has no timestamp and no received_at.")

    if et == "price_change" and isinstance(msg.get("price_changes"), list):
        for ch in msg["price_changes"]:
            tok = str(ch.get("asset_id") or "")
            if not keep(tok, cond):
                continue
            market, side = tokens(tok)

            def make(ch: Any = ch, market: str = market, side: Side = side) -> MarketEvent:
                stale.seen(market, when(msg))
                return _poly_change(
                    venue,
                    market,
                    side,
                    str(ch.get("side")),
                    _price(ch.get("price"), "price"),
                    _num(ch.get("size"), "size"),
                    when(msg),
                    recv,
                )

            sink.build(n, make, venue, market, _stamp(ch.get("best_bid"), ch.get("best_ask"), side))
        return
    tok = str(msg.get("asset_id") or "")
    if et not in ("book", "price_change", "last_trade_price", "tick_size_change", "market_resolved"):
        return
    if et != "market_resolved" and not keep(tok, cond):
        return
    market, side = tokens(tok)
    if et == "book":

        def make_book() -> MarketEvent | None:
            at = when(msg)
            if stale.stale(market, at):
                sink.bad(
                    "stale_book",
                    f"a re-sent book from {at.isoformat()} arrived after newer data; skipped.",
                    n,
                    venue,
                    market,
                )
                return None
            stale.seen(market, at)
            return _poly_book(
                venue,
                market,
                side,
                _levels(msg.get("bids") or msg.get("buys")),
                _levels(msg.get("asks") or msg.get("sells")),
                at,
                recv,
            )

        sink.build(n, make_book, venue, market)
    elif et == "price_change":  # the older shape: one asset, a list of changes
        for ch in msg.get("changes") or []:

            def make_old(ch: Any = ch) -> MarketEvent:
                return _poly_change(
                    venue,
                    market,
                    side,
                    str(ch.get("side")),
                    _price(ch.get("price"), "price"),
                    _num(ch.get("size"), "size"),
                    when(msg),
                    recv,
                )

            sink.build(n, make_old, venue, market)
    elif et == "last_trade_price":
        sink.build(
            n,
            lambda: _poly_trade(
                venue,
                market,
                side,
                _price(msg.get("price"), "price"),
                _num(msg.get("size"), "size"),
                when(msg),
                recv,
                msg.get("side"),
            ),
            venue,
            market,
        )
    elif et == "tick_size_change":
        try:
            sink.ticks.append((venue, market, when(msg), _num(msg.get("new_tick_size"), "new_tick_size")))
        except _Bad as b:
            sink.bad(b.kind, str(b), n, venue, market)
    elif et == "market_resolved":
        winner = str(msg.get("winning_asset_id") or "")
        done: set[str] = set()  # a market whose two tokens are folded together resolves once
        for tok in msg.get("assets_ids") or msg.get("asset_ids") or [winner]:
            tok = str(tok)
            if not keep(tok, cond):
                continue
            m, s = tokens(tok)
            if m in done:
                continue
            done.add(m)
            won = tok == winner
            outcome: Literal["yes", "no"] = "yes" if won == (s == "yes") else "no"

            def make_resolution(m: str = m, outcome: Literal["yes", "no"] = outcome) -> MarketEvent:
                return Resolution(venue=venue, market=m, outcome=outcome, as_of=when(msg), received_at=recv)

            sink.build(n, make_resolution, venue, m)


# ---- Kalshi market-data WebSocket ---------------------------------------------------------------


def _kalshi_price(msg: Mapping[str, Any], cents_key: str, dollars_key: str) -> float:
    if msg.get(dollars_key) is not None:
        return _price(msg[dollars_key], dollars_key)
    return _price(msg.get(cents_key), cents_key, 0.01)


def _kalshi_levels(msg: Mapping[str, Any], side: str) -> dict[float, float]:
    """``yes_dollars`` / ``yes_dollars_fp`` / ``yes`` (cents) bid levels → {price: size}."""
    for key, scale in (
        (f"{side}_dollars_fp", 1.0),
        (f"{side}_dollars", 1.0),
        (f"{side}_fp", 1.0),
        (side, 0.01),
    ):
        raw = msg.get(key)
        if raw is None:
            continue
        out: dict[float, float] = {}
        for p, q in raw:
            size = _num(q, "size")
            if size > 0:
                out[_price(p, "price", scale)] = size
        return out
    return {}


@dataclass
class _KalshiBook:
    yes: dict[float, float]  # YES bids: price → contracts
    no: dict[float, float]  # NO bids


def _kalshi_book(
    ticker: str,
    kb: _KalshiBook,
    at: datetime,
    recv: datetime | None,
    source: Literal["venue", "recorded"] = "recorded",
) -> Book:
    """The whole YES book: Kalshi books are bids only, so a NO bid at p is a YES ask at 1 − p."""
    return Book(
        venue="kalshi",
        market=ticker,
        bids=tuple(Level(price=p, size=q) for p, q in kb.yes.items()),
        asks=tuple(Level(price=round(1 - p, 9), size=q) for p, q in kb.no.items()),
        as_of=at,
        received_at=recv,
        source=source,
    )


_KALSHI_RESULT: dict[str, Literal["yes", "no", "void"]] = {"yes": "yes", "no": "no", "void": "void"}
_LIFECYCLE_STATUS: dict[str, Literal["open", "paused", "closed", "halted"]] = {
    "activated": "open",
    "deactivated": "paused",
    "closed": "closed",
    "halted": "halted",
}


def _kalshi_message(
    msg: Any,
    n: int,
    recv: datetime | None,
    sink: _Sink,
    keep: Callable[[str, str], bool],
    books: dict[str, _KalshiBook],
    seqs: dict[Any, int],
) -> None:
    if not isinstance(msg, dict):
        return
    typ = msg.get("type")
    body = msg.get("msg") or {}
    venue = "kalshi"
    ticker = str(body.get("market_ticker") or "")
    if not ticker or not keep(ticker, str(body.get("event_ticker") or "")):
        return

    sid, seq = msg.get("sid"), msg.get("seq")
    if isinstance(seq, int) and typ in ("orderbook_snapshot", "orderbook_delta"):
        last = seqs.get(sid)
        if last is not None and seq != last + 1:
            sink.report.add(
                Problem(
                    "sequence_gap",
                    "error",
                    f"Kalshi sequence jumped from {last} to {seq} on subscription {sid}: "
                    "messages were lost, so the book is wrong until the next snapshot.",
                    n,
                    venue,
                    ticker,
                    recv,
                    sink.file,
                )
            )
        seqs[sid] = seq

    def when() -> datetime:
        # Kalshi's own times, finest first; a snapshot has none, so it takes the envelope's send time.
        for v in (
            body.get("ts_ms"),
            body.get("ts"),
            body.get("determination_ts"),
            body.get("settled_ts"),
            msg.get("sending_ts_ms"),
        ):
            if v is not None:
                return to_time(v)
        if recv is not None:
            return recv
        raise _Bad("unreadable", "the message has no ts and no received_at.")

    if typ == "orderbook_snapshot":

        def make_snapshot() -> MarketEvent:
            kb = _KalshiBook(_kalshi_levels(body, "yes"), _kalshi_levels(body, "no"))
            books[ticker] = kb
            return _kalshi_book(ticker, kb, when(), recv)

        sink.build(n, make_snapshot, venue, ticker)
    elif typ == "orderbook_delta":

        def make_delta() -> MarketEvent | None:
            kb = books.get(ticker)
            side = str(body.get("side"))
            if side not in ("yes", "no"):
                raise _Bad("unreadable", f"delta side {side!r} isn't yes or no.")
            if body.get("price_dollars") is not None:
                price = _price(body["price_dollars"], "price_dollars")
            else:
                price = _price(body.get("price"), "price", 0.01)
            change = _num(body.get("delta_fp", body.get("delta")), "delta")
            at = when()
            if kb is None:
                sink.bad(
                    "no_starting_book",
                    f"a delta for {ticker} arrived before its snapshot; skipped.",
                    n,
                    venue,
                    ticker,
                )
                return None
            levels = kb.yes if side == "yes" else kb.no
            size = round(levels.get(price, 0.0) + change, 6)
            if size < 0:
                raise _Bad("impossible", f"the delta takes {side} {price} to {size} contracts.")
            if size == 0:
                levels.pop(price, None)
            else:
                levels[price] = size
            # Kalshi books are bids only: a NO bid at p is a YES ask at 1 − p.
            return BookLevelChange(
                venue=venue,
                market=ticker,
                book_side="bid" if side == "yes" else "ask",
                price=price if side == "yes" else round(1 - price, 9),
                size=size,
                as_of=at,
                received_at=recv,
            )

        sink.build(n, make_delta, venue, ticker)
    elif typ == "trade":
        sink.build(
            n,
            lambda: TradePrint(
                venue=venue,
                market=ticker,
                price=_kalshi_price(body, "yes_price", "yes_price_dollars"),
                size=_num(body.get("count_fp", body.get("count")), "count"),
                as_of=when(),
                trade_id=str(body["trade_id"]) if body.get("trade_id") else None,
                aggressor=_aggressor(body.get("taker_side")),
                received_at=recv,
            ),
            venue,
            ticker,
        )
    elif typ in ("market_lifecycle_v2", "market_lifecycle"):
        ev = str(body.get("event_type") or "")
        result = str(body.get("result") or "").lower()
        if ev in ("determined", "settled") and result:
            outcome = _KALSHI_RESULT.get(result)
            if outcome is None:
                sink.bad("unreadable", f"result {result!r} isn't yes, no or void.", n, venue, ticker)
            else:

                def make_resolution(outcome: Literal["yes", "no", "void"] = outcome) -> MarketEvent:
                    return Resolution(
                        venue=venue, market=ticker, outcome=outcome, as_of=when(), received_at=recv
                    )

                sink.build(n, make_resolution, venue, ticker)
        elif ev in _LIFECYCLE_STATUS:
            st = _LIFECYCLE_STATUS[ev]
            if ev == "deactivated" and body.get("is_deactivated") is False:
                st = "open"  # Kalshi sends unpausing as a deactivated event with is_deactivated false

            def make_status(st: Literal["open", "paused", "closed", "halted"] = st) -> MarketEvent:
                return MarketStatus(venue=venue, market=ticker, status=st, as_of=when(), received_at=recv)

            sink.build(n, make_status, venue, ticker)


# ---- Polymarket US market-data WebSocket --------------------------------------------------------

_PMUS_STATE: dict[str, Literal["open", "paused", "closed", "halted"]] = {
    "MARKET_STATE_OPEN": "open",
    "MARKET_STATE_PREOPEN": "paused",
    "MARKET_STATE_SUSPENDED": "paused",
    "MARKET_STATE_HALTED": "halted",
    "MARKET_STATE_EXPIRED": "closed",
    "MARKET_STATE_TERMINATED": "closed",
}


def _amount(v: Any) -> Any:
    """``{"value": "0.5", "currency": "USD"}`` → ``"0.5"``; anything else unchanged."""
    return v.get("value") if isinstance(v, Mapping) else v


def _pmus_levels(raw: Any) -> tuple[Level, ...]:
    out = []
    for lv in raw or []:
        if not isinstance(lv, Mapping):
            raise _Bad("unreadable", f"a level isn't {{px: {{value}}, qty}}: {lv!r}")
        size = _num(lv.get("qty"), "qty")
        if size > 0:
            out.append(Level(price=_price(_amount(lv.get("px")), "px"), size=size))
    return tuple(out)


def _pmus_message(
    msg: Any,
    n: int,
    recv: datetime | None,
    sink: _Sink,
    keep: Callable[[str, str], bool],
    states: dict[str, str],
) -> None:
    """One message from Polymarket US's markets WebSocket (``SUBSCRIPTION_TYPE_MARKET_DATA``, ``_LITE``
    and ``_TRADE``). ``marketData`` is always the full book; prices are the YES (long) side."""
    if not isinstance(msg, dict):
        return
    venue = "polymarket_us"
    md = msg.get("marketData")
    if isinstance(md, dict):
        slug = str(md.get("marketSlug") or "")
        if not slug or not keep(slug, ""):
            return

        def when() -> datetime:
            t = md.get("transactTime")
            if t:
                return to_time(t)
            if recv is not None:
                return recv
            raise _Bad("unreadable", "marketData has no transactTime and no received_at.")

        def make_book() -> MarketEvent:
            return Book(
                venue=venue,
                market=slug,
                bids=_pmus_levels(md.get("bids")),
                asks=_pmus_levels(md.get("offers")),
                as_of=when(),
                received_at=recv,
                source="recorded",
            )

        sink.build(n, make_book, venue, slug)
        mapped = _PMUS_STATE.get(str(md.get("state") or ""))
        if mapped and states.get(slug) != mapped:
            states[slug] = mapped

            def make_status(st: Literal["open", "paused", "closed", "halted"] = mapped) -> MarketEvent:
                return MarketStatus(venue=venue, market=slug, status=st, as_of=when(), received_at=recv)

            sink.build(n, make_status, venue, slug)
    lite = msg.get("marketDataLite")
    if isinstance(lite, dict) and lite.get("settlementPx") is not None:
        # The lite feed is the only place the stream says how a market settled: 1 = YES won.
        slug = str(lite.get("marketSlug") or "")
        if slug and keep(slug, "") and states.get(slug) != "settled":
            states[slug] = "settled"

            def make_resolution() -> MarketEvent:
                px = _num(_amount(lite.get("settlementPx")), "settlementPx")
                outcome: Literal["yes", "no", "void"] = "yes" if px == 1 else "no" if px == 0 else "void"
                if recv is None:
                    raise _Bad("unreadable", "marketDataLite has no time of its own; it needs received_at.")
                payout = px if 0 <= px <= 1 else None
                return Resolution(
                    venue=venue, market=slug, outcome=outcome, payout=payout, as_of=recv, received_at=recv
                )

            sink.build(n, make_resolution, venue, slug)
    trade = msg.get("trade")
    if isinstance(trade, dict) and trade.get("state") in (None, "TRADE_STATE_NEW"):
        slug = str(trade.get("marketSlug") or "")
        if not slug or not keep(slug, ""):
            return

        def make_trade() -> MarketEvent:
            t = trade.get("tradeTime") or trade.get("transactTime")
            at = to_time(t) if t else recv
            if at is None:
                raise _Bad("unreadable", "the trade has no tradeTime and no received_at.")
            # quantity is shares, though Polymarket US wraps it like a dollar amount
            return TradePrint(
                venue=venue,
                market=slug,
                price=_price(_amount(trade.get("price", trade.get("px"))), "price"),
                size=_num(_amount(trade.get("quantity", trade.get("qty"))), "quantity"),
                as_of=at,
                trade_id=str(trade["id"]) if trade.get("id") else None,
                aggressor=_aggressor((trade.get("taker") or {}).get("side")),
                received_at=recv,
            )

        sink.build(n, make_trade, venue, slug)


# ---- PMXT hourly Parquet files ------------------------------------------------------------------


def _pmxt(path: Path, sink: _Sink, tokens: _Tokens, markets: set[str], stale: _StaleBooks) -> None:
    pq = _pyarrow()
    import pyarrow as pa
    import pyarrow.compute as pc  # type: ignore[import-untyped]

    f = pq.ParquetFile(path)
    names = set(f.schema_arrow.names)
    v2 = "event_type" in names
    if not v2 and "update_type" not in names:
        raise VenueError(
            "format_changed",
            f"{path.name} isn't a PMXT file: no event_type or update_type column.",
            retryable=False,
            hint="Pass format='parquet' with columns= for other Parquet files.",
        )
    want = pa.array(sorted(markets))
    row0 = 0
    for batch in f.iter_batches(batch_size=200_000):
        n0 = row0
        row0 += batch.num_rows
        if v2:
            mask = pc.or_(
                pc.is_in(batch.column("asset_id"), value_set=want),
                pc.is_in(
                    pc.cast(batch.column("market"), pa.binary()),
                    value_set=pa.array([m.encode() for m in markets], type=pa.binary()),
                ),
            )
        else:
            mask = pc.is_in(batch.column("market_id"), value_set=want)
            # v1 keeps the token inside its JSON: keep rows that mention a wanted token too
            for m in markets:
                if not m.startswith("0x"):
                    mask = pc.or_(mask, pc.match_substring(batch.column("data"), m))
        idx = pc.indices_nonzero(mask).to_pylist()
        if not idx:
            continue
        sub = batch.take(pa.array(idx)).to_pylist()
        for i, r in zip(idx, sub, strict=True):
            n = n0 + i + 1
            if v2:
                _pmxt_v2(r, n, sink, tokens, stale)
            else:
                _pmxt_v1(r, n, sink, tokens, stale, markets)


def _pmxt_v2(r: dict[str, Any], n: int, sink: _Sink, tokens: _Tokens, stale: _StaleBooks) -> None:
    venue = "polymarket"
    tok = str(r["asset_id"])
    market, side = tokens(tok)
    at, recv = to_time(r["timestamp"]), to_time(r["timestamp_received"])
    et = r["event_type"]
    if et == "book":

        def make_book() -> MarketEvent | None:
            if stale.stale(market, at):
                sink.bad(
                    "stale_book",
                    f"a re-sent book from {at.isoformat()} arrived after newer data; skipped.",
                    n,
                    venue,
                    market,
                )
                return None
            stale.seen(market, at)
            return _poly_book(venue, market, side, _levels(r["bids"]), _levels(r["asks"]), at, recv)

        sink.build(n, make_book, venue, market)
    elif et == "price_change":
        stale.seen(market, at)
        sink.build(
            n,
            lambda: _poly_change(
                venue,
                market,
                side,
                str(r["side"]),
                _price(r["price"], "price"),
                _num(r["size"], "size"),
                at,
                recv,
            ),
            venue,
            market,
            stamp=_stamp(r["best_bid"], r["best_ask"], side),
        )
    elif et == "last_trade_price":
        sink.build(
            n,
            lambda: _poly_trade(
                venue, market, side, _price(r["price"], "price"), _num(r["size"], "size"), at, recv, r["side"]
            ),
            venue,
            market,
        )
    elif et == "tick_size_change" and r.get("new_tick_size") is not None:
        sink.ticks.append((venue, market, at, float(r["new_tick_size"])))


def _pmxt_v1(
    r: dict[str, Any], n: int, sink: _Sink, tokens: _Tokens, stale: _StaleBooks, markets: set[str]
) -> None:
    venue = "polymarket"
    try:
        d = json.loads(r["data"])
    except json.JSONDecodeError:
        sink.bad("unreadable", "the data column isn't JSON.", n)
        return
    tok = str(d.get("token_id") or "")
    if tok not in markets and str(r.get("market_id")) not in markets:
        return
    market, side = tokens(tok)
    # PMXT's first schema has no venue time; its receive time is the best there is.
    at = to_time(r["timestamp_received"])
    ut = r["update_type"]
    if ut == "book_snapshot":

        def make_book() -> MarketEvent | None:
            if stale.stale(market, at):
                return None
            stale.seen(market, at)
            return _poly_book(venue, market, side, _levels(d.get("bids")), _levels(d.get("asks")), at, at)

        sink.build(n, make_book, venue, market)
    elif ut == "price_change":
        sink.build(
            n,
            lambda: _poly_change(
                venue,
                market,
                side,
                str(d.get("change_side")),
                _price(d.get("change_price"), "change_price"),
                _num(d.get("change_size"), "change_size"),
                at,
                at,
            ),
            venue,
            market,
            stamp=_stamp(d.get("best_bid"), d.get("best_ask"), side),
        )


# ---- the entry point ----------------------------------------------------------------------------


def _detect(path: Path) -> Format:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".parquet":
        pq = _pyarrow()
        names = set(pq.ParquetFile(path).schema_arrow.names)
        return (
            "pmxt" if {"timestamp_received"} <= names and names & {"event_type", "update_type"} else "parquet"
        )
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                _, m = _unwrap(json.loads(line))
            except (json.JSONDecodeError, _Bad):
                break
            if isinstance(m, list) and m:
                m = m[0]
            if not isinstance(m, dict):
                break
            if m.get("kind") in _MODELS:
                return "jsonl"
            if "event_type" in m:
                return "polymarket"
            if m.get("type") in (
                "orderbook_snapshot",
                "orderbook_delta",
                "trade",
                "market_lifecycle_v2",
                "market_lifecycle",
                "subscribed",
                "ok",
                "error",
            ):
                return "kalshi"
            if "marketData" in m or "trade" in m or "heartbeat" in m:
                return "polymarket_us"
            break
    raise VenueError(
        "invalid_order",
        f"Can't tell what format {path.name} is.",
        retryable=False,
        hint="Pass format=: csv, parquet, jsonl, polymarket_us, polymarket, kalshi or pmxt.",
        next=f"import_events({str(path)!r}, format='csv', venue=..., columns={{...}})",
    )


def import_events(
    path: str | Path | Iterable[str | Path],
    *,
    format: Format | None = None,
    venue: str | None = None,
    market: str | None = None,
    columns: Mapping[str, str] | None = None,
    kind: Literal["book", "book_change", "trade", "status", "resolution"] | None = None,
    price_scale: float = 1.0,
    markets: Iterable[str] | None = None,
    tokens: Mapping[str, str | tuple[str, Side]] | None = None,
    tick_size: float | Mapping[str, float] | None = None,
    max_gap_s: float | None = None,
    strict: bool = True,
) -> Imported:
    """Read a file (or several, e.g. consecutive hours) of order-book history into the SDK's market
    events, checked and oldest first.

        data = import_events("ticks.csv", venue="kalshi", columns={"time": "ts", "market": "ticker"})
        data = import_events("polymarket_orderbook_2026-07-21T04.parquet", markets=["<token id>"])
        Client(mode="backtest", books=data).replay(on_book)

    Args:
        format: ``csv``, ``parquet``, ``jsonl`` (the SDK's own), ``polymarket_us``, ``polymarket``,
            ``kalshi`` or ``pmxt``. Guessed from the file when left out.
        venue, market: for CSV and Parquet, the venue and market of every row, when the file has no
            such column.
        columns: for CSV and Parquet, ``{sdk_field: your_column}``. Fields: see :data:`FIELDS`. A row
            is a full book (``bids``/``asks`` as JSON levels, or a top of book from ``bid``,
            ``bid_size``, ``ask``, ``ask_size``), a level change (``book_side``, ``price``, ``size``,
            where size is the level's new total and 0 removes it), a trade (``price``, ``size``), a
            ``status`` or a resolution (``outcome``). Prices are YES-side dollars.
        kind: what every row is, when the file has no ``kind`` column and guessing isn't wanted.
        price_scale: multiply prices by this, e.g. ``0.01`` for cents.
        markets: keep only these markets. For ``pmxt`` (every Polymarket market, tens of millions of
            rows an hour) it's required: outcome token ids or condition ids.
        tokens: for ``polymarket`` and ``pmxt``, ``{token_id: (market, "yes" | "no")}`` to name a
            market and fold its NO token into the YES book. Unmapped tokens are their own market, with
            the token as the YES side.
        tick_size, max_gap_s: passed to :func:`check_events`.
        strict: raise ``bad_data`` when the checker finds errors (the default). With ``False`` the
            import returns anyway; read ``data.report``.
    """
    paths = [Path(path)] if isinstance(path, (str, Path)) else [Path(x) for x in path]
    if not paths:
        raise VenueError("not_found", "No files given.", retryable=False)
    for p in paths:
        if not p.exists():
            raise VenueError("not_found", f"No file at {p}.", retryable=False)
    keep_set = {str(m) for m in markets} if markets is not None else None

    def keep(a: str, b: str) -> bool:
        return keep_set is None or a in keep_set or (bool(b) and b in keep_set)

    sink = _Sink()
    toks = _Tokens(tokens)
    stale = _StaleBooks()  # these three carry across files, so consecutive hours join up
    kalshi_books: dict[str, _KalshiBook] = {}
    seqs: dict[Any, int] = {}
    states: dict[str, str] = {}

    for p in paths:
        fmt = format or _detect(p)
        sink.file = p.name if len(paths) > 1 else None
        start = len(sink.events)
        if fmt in ("csv", "parquet"):
            rows = _csv_rows(p) if fmt == "csv" else _parquet_rows(p)
            _table_rows(
                rows,
                sink,
                columns=columns or {},
                venue=venue,
                market=market,
                kind=kind,
                price_scale=price_scale,
            )
        elif fmt == "jsonl":
            _sdk_jsonl(p, sink)
        elif fmt in ("polymarket", "kalshi", "polymarket_us"):
            for n, d in _json_lines(p, sink):
                try:
                    recv, msg = _unwrap(d)
                except _Bad as b:
                    sink.bad(b.kind, str(b), n)
                    continue
                if fmt == "polymarket":
                    _polymarket_message(msg, n, recv, sink, toks, keep, stale)
                elif fmt == "kalshi":
                    _kalshi_message(msg, n, recv, sink, keep, kalshi_books, seqs)
                else:
                    _pmus_message(msg, n, recv, sink, keep, states)
        elif fmt == "pmxt":
            if keep_set is None:
                raise VenueError(
                    "invalid_order",
                    "A PMXT hour holds every Polymarket market (tens of millions of rows): pass markets=.",
                    retryable=False,
                    hint="markets= takes outcome token ids (asset_id) or condition ids (0x…).",
                    next=f"import_events({str(p)!r}, markets=['<token id>'])",
                )
            _pmxt(p, sink, toks, keep_set, stale)
        else:
            raise VenueError(
                "invalid_order",
                f"format {fmt!r} isn't one this SDK reads.",
                retryable=False,
                hint="Use csv, parquet, jsonl, polymarket_us, polymarket, kalshi or pmxt.",
            )
        if keep_set is not None and fmt in ("csv", "parquet", "jsonl"):
            kept = [i for i in range(start, len(sink.events)) if sink.events[i].market in keep_set]
            sink.events[start:] = [sink.events[i] for i in kept]
            sink.rows[start:] = [sink.rows[i] for i in kept]
            sink.files[start:] = [sink.files[i] for i in kept]

    _repair_from_stamps(sink)
    report = check_events(
        sink.events,
        tick_size=tick_size,
        max_gap_s=max_gap_s,
        rows=sink.rows,
        files=sink.files,
        report=sink.report,
        tick_changes=sink.ticks,
    )
    order = sorted(range(len(sink.events)), key=lambda i: sink.events[i].as_of)
    data = Imported([sink.events[i] for i in order], report)
    if strict and not report.ok:
        name = paths[0].name if len(paths) == 1 else f"These {len(paths)} files"
        raise VenueError(
            "bad_data",
            f"{name} has {report.errors} problem(s) that would make a backtest wrong.\n{report.summary()}",
            retryable=False,
            hint="Fix the file, or pass strict=False to import it anyway and read data.report.",
            next="import_events(..., strict=False).report.summary()",
            raw=report.to_dict(),
        )
    return data
