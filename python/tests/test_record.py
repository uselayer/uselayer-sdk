"""record_stream(): every book change and trade from Polymarket US's stream, gaps marked, replayable."""

from __future__ import annotations

import base64
import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from uselayer import Book, BookLevelChange, Client, Level, StreamGap, TradePrint, VenueError
from uselayer.__main__ import main
from uselayer.backtest import load_books
from uselayer.record import record_stream
from uselayer.venues.polymarket_us_live import PolymarketUS

KEY = PolymarketUS(key_id="key-1", secret_key=base64.b64encode(bytes(range(32))).decode())
DATE = "Thu, 01 Oct 2026 12:00:00 GMT"  # = T0


def md(
    slug: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]], at: float, **kw: Any
) -> Any:
    lv = lambda xs: [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in xs]  # noqa: E731
    t = (T0 + timedelta(seconds=at)).isoformat().replace("+00:00", "Z")
    return {
        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
        "marketData": {"marketSlug": slug, "bids": lv(bids), "offers": lv(asks), "transactTime": t, **kw},
    }


def trade(slug: str, px: float, qty: float, at: float, tid: str, taker: str = "ORDER_SIDE_BUY") -> Any:
    t = (T0 + timedelta(seconds=at)).isoformat().replace("+00:00", "Z")
    return {
        "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
        "trade": {
            "marketSlug": slug,
            "price": {"value": str(px), "currency": "USD"},
            "quantity": {"value": str(qty), "currency": "USD"},
            "tradeTime": t,
            "taker": {"side": taker},
            "id": tid,
            "state": "TRADE_STATE_NEW",
        },
    }


class Sock:
    """One fake connection. Plays its script; a float advances the clock; an exception is raised."""

    def __init__(self, clock: Clock, *script: Any) -> None:
        self.clock, self.script, self.sent = clock, list(script), []  # type: ignore[var-annotated]
        self.response = SimpleNamespace(headers={"Date": DATE})
        self.closed = False

    def __enter__(self) -> Sock:
        return self

    def __exit__(self, *a: Any) -> None:
        self.closed = True

    def send(self, m: str) -> None:
        self.sent.append(json.loads(m))

    def recv(self, timeout: float | None = None) -> str:
        while self.script:
            item = self.script.pop(0)
            if isinstance(item, (int, float)):
                self.clock.advance(item)
                continue
            if isinstance(item, BaseException):
                raise item
            return json.dumps(item)
        self.clock.advance(1)
        raise TimeoutError


class Connect:
    def __init__(self, *sessions: Any) -> None:
        self.sessions, self.calls = list(sessions), 0

    def __call__(self, url: str, **kw: Any) -> Any:
        self.calls += 1
        s = self.sessions.pop(0)
        if isinstance(s, BaseException):
            raise s
        return s


def rec(clock: Clock, connect: Connect, markets: list[str], path: Any, **kw: Any) -> Any:
    return record_stream(
        markets,
        path,
        polymarket_us=KEY,
        check_markets=False,
        ws_connect=connect,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


def test_records_book_changes_and_trades_in_the_event_format(tmp_path: Any) -> None:
    clock = Clock(T0 + timedelta(seconds=0.2))
    sock = Sock(
        clock,
        # The first book's last change was 5 minutes ago: it's stamped with the handshake's Date (T0).
        md("m", [(0.40, 10)], [(0.42, 5)], -300, state="MARKET_STATE_OPEN"),
        md("m", [(0.40, 10)], [(0.42, 5)], 1, state="MARKET_STATE_OPEN"),  # stats only: not written
        trade("m", 0.42, 5, 2, "t1"),
        trade("m", 0.42, 5, 2, "t1"),  # the same trade again: written once
        md("m", [(0.40, 10)], [(0.43, 7)], 2, state="MARKET_STATE_OPEN"),
        trade("m", 0.40, 3, 3, "t2", taker="ORDER_SIDE_SELL"),
        md("other-market", [(0.1, 1)], [(0.2, 1)], 3),  # not subscribed: ignored
    )
    p = tmp_path / "ticks.jsonl"
    s = rec(clock, Connect(sock), ["m"], p, duration_s=30)
    assert sock.closed  # the connection is closed when the recording ends
    assert (s.books, s.trades, s.statuses, s.gaps, s.reconnects) == (2, 2, 1, [], 0)
    assert [m["subscribe"]["subscriptionType"] for m in sock.sent] == [
        "SUBSCRIPTION_TYPE_MARKET_DATA",
        "SUBSCRIPTION_TYPE_TRADE",
    ]
    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["book", "status", "trade", "book", "trade"]
    assert lines[0]["as_of"] == "2026-10-01T12:00:00Z" and lines[0]["received_at"] is not None
    assert lines[2] == {
        "kind": "trade",
        "origin": "venue",
        "venue": "polymarket_us",
        "market": "m",
        "price": 0.42,
        "size": 5.0,
        "as_of": "2026-10-01T12:00:02Z",
        "trade_id": "t1",
        "aggressor": "buy",
        "received_at": lines[2]["received_at"],
    }
    assert lines[4]["aggressor"] == "sell"

    events = load_books(p)
    out = Client(mode="backtest", books=events).replay()
    assert (out["books"], out["trades"], out["gaps"]) == (2, 2, 0)


def test_a_dropped_connection_reconnects_and_is_marked_as_a_gap(tmp_path: Any) -> None:
    clock = Clock()
    first = Sock(
        clock, md("a", [(0.4, 1)], [(0.5, 1)], 0), 5, ConnectionClosedError(None, None)
    )  # drops at T0+5
    second = Sock(clock, md("a", [(0.4, 1)], [(0.5, 1)], 9))  # same levels as before the drop
    alerts: list[dict[str, Any]] = []
    p = tmp_path / "ticks.jsonl"
    s = rec(
        clock,
        Connect(first, OSError("connection refused"), second),
        ["a", "b"],
        p,
        duration_s=20,
        on_alert=alerts.append,
    )
    assert clock.slept[:2] == [1.0, 2.0]  # waits 1 s, then 2 s
    assert s.reconnects == 1 and [g.market for g in s.gaps] == ["a", "b"]
    gap = s.gaps[0]
    # The gap starts at the last message (T0), not when the drop was noticed (T0+5).
    assert (gap.as_of, gap.until) == (T0, T0 + timedelta(seconds=8))
    assert gap.reason.startswith("disconnected: ConnectionClosedError")
    assert [a["kind"] for a in alerts] == ["stream_disconnected", "stream_reconnected"]
    # After the gap the book is written again even though its levels didn't change.
    assert s.books == 2
    kinds = [json.loads(x)["kind"] for x in p.read_text().splitlines()]
    assert kinds == ["book", "gap", "gap", "book"]

    # In backtest, the market has no book during the gap.
    seen: list[str] = []
    bt = Client(mode="backtest", books=load_books(p))
    bt.replay(lambda c, b: seen.append(b.as_of.isoformat()))
    assert len(seen) == 2
    gap_only = Client(mode="backtest", books=[e for e in load_books(p) if e.as_of <= gap.as_of])
    gap_only.replay()
    with pytest.raises(VenueError) as e:
        gap_only.book("a")
    assert e.value.code == "stale_quote"


def test_a_recording_that_ends_while_down_closes_its_gap(tmp_path: Any) -> None:
    clock = Clock()
    s = rec(
        clock,
        Connect(Sock(clock, 2, ConnectionClosedError(None, None)), *[OSError("down")] * 10),
        ["a"],
        tmp_path / "t.jsonl",
        duration_s=10,
    )
    (gap,) = s.gaps
    assert gap.as_of == T0 and gap.until == T0 + timedelta(seconds=10)  # up since subscribing at T0
    assert "ended before it reconnected" in gap.reason
    assert sum(clock.slept) == pytest.approx(8.0)  # never waits past the end


def test_reconnect_waits_double_up_to_30_seconds(tmp_path: Any) -> None:
    clock = Clock()
    rec(clock, Connect(*[OSError("down")] * 12), ["a"], tmp_path / "t.jsonl", duration_s=150)
    assert clock.slept[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert sum(clock.slept) == pytest.approx(150.0)


def test_a_short_market_is_recorded_from_its_own_side(tmp_path: Any) -> None:
    clock = Clock()
    sock = Sock(clock, md("g", [(0.40, 10)], [(0.42, 5)], 0), trade("g", 0.42, 5, 1, "t1"))
    p = tmp_path / "t.jsonl"
    rec(clock, Connect(sock), ["g:short"], p, duration_s=5)
    book, tr = load_books(p)
    assert isinstance(book, Book) and isinstance(tr, TradePrint)
    assert (book.market, book.bids[0].price, book.asks[0].price) == ("g:short", 0.58, 0.6)
    assert (tr.price, tr.aggressor) == (0.58, "sell")  # taker bought the long side = sold the short side


def test_subscriptions_hold_at_most_100_markets(tmp_path: Any) -> None:
    clock = Clock()
    sock = Sock(clock)
    rec(clock, Connect(sock), [f"m{i}" for i in range(150)], tmp_path / "t.jsonl", duration_s=1)
    assert [len(m["subscribe"]["marketSlugs"]) for m in sock.sent] == [100, 100, 50, 50]


def test_a_refused_key_stops_instead_of_retrying(tmp_path: Any) -> None:
    clock = Clock()
    refused = InvalidStatus(Response(401, "Unauthorized", Headers()))
    connect = Connect(refused, refused)
    with pytest.raises(VenueError) as e:
        rec(clock, connect, ["a"], tmp_path / "t.jsonl", duration_s=60)
    assert e.value.code == "auth_failed" and connect.calls == 1


def test_a_changed_message_format_stops_the_recording(tmp_path: Any) -> None:
    clock = Clock()
    bad = {"marketData": {"marketSlug": "a", "bids": [{"price": 0.4}], "offers": [], "transactTime": "x"}}
    with pytest.raises(VenueError) as e:
        rec(clock, Connect(Sock(clock, bad)), ["a"], tmp_path / "t.jsonl", duration_s=60)
    assert e.value.code == "format_changed"


def test_unknown_markets_are_refused_before_connecting(venue: FakeVenue, tmp_path: Any) -> None:
    venue.add(FakeMarket("known", [(0.4, 1)], [(0.5, 1)]))
    with pytest.raises(VenueError) as e:
        record_stream(
            ["known", "typo"],
            tmp_path / "t.jsonl",
            polymarket_us=KEY,
            transport=venue.transport(),
            ws_connect=Connect(),
        )
    assert e.value.code == "not_found"


def test_the_stream_needs_a_key(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    with pytest.raises(VenueError) as e:
        record_stream(["a"], tmp_path / "t.jsonl", check_markets=False)
    assert e.value.code == "auth_failed"
    assert main(["record", "a", "--out", str(tmp_path / "t.jsonl")]) == 1


def test_other_venues_are_not_recorded_yet(tmp_path: Any) -> None:
    with pytest.raises(VenueError) as e:
        record_stream(["a"], tmp_path / "t.jsonl", venue="polymarket")
    assert e.value.code == "not_available"


def test_replay_applies_book_changes_and_skips_ones_without_a_book() -> None:
    def ch(s: int, side: str, price: float, size: float, market: str = "m") -> BookLevelChange:
        return BookLevelChange(
            venue="polymarket_us",
            market=market,
            book_side=side,  # type: ignore[arg-type]
            price=price,
            size=size,
            as_of=T0 + timedelta(seconds=s),
        )

    first = Book(
        venue="polymarket_us",
        market="m",
        bids=(Level(price=0.40, size=10),),
        asks=(Level(price=0.42, size=5),),
        as_of=T0,
    )
    seen: list[tuple[float, float]] = []
    # A change before the first book, and one for a market with no book, can't be applied: skipped.
    events = [
        ch(-1, "ask", 0.41, 1),
        first,
        ch(1, "ask", 0.42, 0),
        ch(2, "ask", 0.45, 3),
        ch(3, "bid", 0.3, 1, "n"),
    ]
    out = Client(mode="backtest", books=events).replay(
        lambda c, b: seen.append((b.bids[0].price, b.asks[0].price if b.asks else 0.0))
    )
    assert out["books"] == 3
    assert seen == [(0.40, 0.42), (0.40, 0.0), (0.40, 0.45)]


def test_gaps_load_back_as_stream_gaps(tmp_path: Any) -> None:
    clock = Clock()
    p = tmp_path / "t.jsonl"
    rec(
        clock, Connect(Sock(clock, 1, ConnectionClosedError(None, None)), Sock(clock)), ["a"], p, duration_s=5
    )
    (gap,) = load_books(p)
    assert isinstance(gap, StreamGap) and gap.seconds == 2.0  # up since T0, dropped at T0+1, back at T0+2


class PingSock(Sock):
    """A connection that answers pings (or stops answering after ``answer_until`` pings)."""

    def __init__(self, clock: Clock, *script: Any, answer_until: int = 99) -> None:
        super().__init__(clock, *script)
        self.pings, self.answer_until = 0, answer_until

    def ping(self) -> threading.Event:
        self.pings += 1
        e = threading.Event()
        if self.pings <= self.answer_until:
            e.set()
        return e


def test_a_quiet_connection_is_pinged_so_a_drop_is_dated_closely(tmp_path: Any) -> None:
    clock = Clock()
    # Quiet for 20 s (pings answered), then the venue stops answering and the drop is noticed at ~T0+40.
    sock = PingSock(clock, answer_until=3)
    sock.recv = _recv_after(sock, clock, quiet_s=40)  # type: ignore[method-assign]
    s = rec(clock, Connect(sock, Sock(clock)), ["a"], tmp_path / "t.jsonl", duration_s=60)
    (gap,) = s.gaps
    # Pings went out every 5 s from T0; three were answered, so the line was last known up at T0+15.
    assert sock.pings >= 4 and gap.as_of == T0 + timedelta(seconds=15)


def _recv_after(sock: Sock, clock: Clock, *, quiet_s: int) -> Any:
    calls = [0]

    def recv(timeout: float | None = None) -> str:
        calls[0] += 1
        if calls[0] <= quiet_s:
            clock.advance(1)
            raise TimeoutError
        raise ConnectionClosedError(None, None)

    return recv


def test_a_sleeping_machine_is_a_gap_from_before_it_slept(tmp_path: Any) -> None:
    clock = Clock()
    # A book at T0, quiet for 3 s, then the machine sleeps 10 minutes and a message is waiting on wake.
    first = Sock(clock, md("a", [(0.4, 1)], [(0.5, 1)], 0), 600, md("a", [(0.4, 2)], [(0.5, 1)], 601))
    s = rec(clock, Connect(first, Sock(clock)), ["a"], tmp_path / "t.jsonl", duration_s=900)
    (gap,) = s.gaps
    assert gap.as_of == T0 and gap.seconds >= 600
    assert gap.reason.startswith(
        "disconnected: ConnectionError: this machine was asleep or stalled for 600 s"
    )
    assert s.books == 1  # the message read after waking isn't trusted as part of the stream
