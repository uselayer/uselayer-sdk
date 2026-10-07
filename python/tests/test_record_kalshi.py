"""record_stream() on Kalshi: whole books from snapshot + deltas, seq jumps as gaps, mixed with Polymarket US.

Every ticker, price and trade here is made up.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import T0, Clock
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_record import KEY, Connect, Sock, md, trade
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from uselayer import Book, BookLevelChange, Client, Kalshi, StreamGap, TradePrint, VenueError
from uselayer.__main__ import main
from uselayer.backtest import load_books
from uselayer.imports import _apply, import_events
from uselayer.record import market_venue, record_stream
from uselayer.venues.kalshi_stream import KalshiStream

PEM = (
    Ed25519PrivateKey.generate()
    .private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    .decode()
)
KKEY = Kalshi(key_id="kid-1", private_key_pem=PEM)
A, B = "KXSAMPLE-26OCT04-T50", "KXSAMPLE-26OCT04-T60"


def ms(at: float) -> int:
    return int((T0 + timedelta(seconds=at)).timestamp() * 1000)


def snap(t: str, yes: list[tuple[float, float]], no: list[tuple[float, float]], seq: int, at: float) -> Any:
    lv = lambda xs: [[f"{p:.4f}", f"{q:.2f}"] for p, q in xs]  # noqa: E731
    body = {"market_ticker": t, "yes_dollars_fp": lv(yes), "no_dollars_fp": lv(no)}
    return {"type": "orderbook_snapshot", "sid": 1, "seq": seq, "msg": body, "sending_ts_ms": ms(at)}


def delta(t: str, side: str, price: float, change: float, seq: int, at: float) -> Any:
    body = {"market_ticker": t, "price_dollars": f"{price:.4f}", "delta_fp": f"{change:.2f}", "side": side}
    return {"type": "orderbook_delta", "sid": 1, "seq": seq, "msg": {**body, "ts_ms": ms(at)}}


def ktrade(t: str, price: float, count: float, seq: int, at: float, tid: str, taker: str = "yes") -> Any:
    body = {
        "trade_id": tid,
        "market_ticker": t,
        "yes_price_dollars": f"{price:.4f}",
        "no_price_dollars": f"{1 - price:.4f}",
        "count_fp": f"{count:.2f}",
        "taker_side": taker,
        "ts_ms": ms(at),
    }
    return {"type": "trade", "sid": 2, "seq": seq, "msg": body}


def life(t: str, event: str, seq: int, at: float, **kw: Any) -> Any:
    body = {"market_ticker": t, "event_type": event, **kw}
    return {"type": "market_lifecycle_v2", "sid": 3, "seq": seq, "msg": body, "sending_ts_ms": ms(at)}


KSock = Sock  # a fake Kalshi connection plays its script like a Polymarket US one


def krec(clock: Clock, connect: Connect, markets: list[str], path: Any, **kw: Any) -> Any:
    return record_stream(
        markets,
        path,
        kalshi=KKEY,
        check_markets=False,
        ws_connect=connect,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


SCRIPT = [
    {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}},
    snap(A, [(0.40, 50), (0.42, 20)], [(0.50, 30), (0.55, 10)], 1, 0),
    delta(A, "yes", 0.42, -5, 2, 1),
    delta(A, "no", 0.55, -10, 3, 2),
    ktrade(A, 0.45, 10, 1, 2.5, "t-1"),
    ktrade(A, 0.45, 10, 2, 2.5, "t-1"),  # Kalshi sent the same trade twice: written once
    delta(A, "yes", 0.44, 12.5, 4, 3),
    ktrade(A, 0.44, 3, 3, 3.5, "t-2", taker="no"),
    life(A, "deactivated", 1, 4, is_deactivated=True),
    delta("KXSAMPLE-OTHER", "yes", 0.1, 1, 5, 4),  # not asked for: ignored, but its seq still counts
]


def test_records_whole_kalshi_books_trades_and_statuses(tmp_path: Any) -> None:
    clock = Clock()
    sock = KSock(clock, *SCRIPT)
    urls: list[tuple[str, dict[str, str]]] = []

    def spy(url: str, **kw: Any) -> Any:
        urls.append((url, kw["additional_headers"]))
        return sock

    p = tmp_path / "ticks.jsonl"
    s = record_stream(
        [A],
        p,
        kalshi=KKEY,
        check_markets=False,
        ws_connect=spy,
        clock=clock,
        sleep=clock.sleep,
        duration_s=30,
    )
    assert sock.closed and s.venue == "kalshi"
    ((url, headers),) = urls
    assert url == "wss://api.elections.kalshi.com/trade-api/ws/v2"
    assert headers["KALSHI-ACCESS-KEY"] == "kid-1" and headers["KALSHI-ACCESS-SIGNATURE"]
    assert [m["params"] for m in sock.sent] == [
        {"channels": ["orderbook_delta"], "market_tickers": [A]},
        {"channels": ["trade", "market_lifecycle_v2"], "market_tickers": [A]},
    ]
    assert (s.books, s.trades, s.statuses, s.gaps, s.reconnects) == (4, 2, 1, [], 0)

    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["book", "book", "book", "trade", "book", "trade", "status"]
    first, *_, last = [x for x in lines if x["kind"] == "book"]
    # A NO bid at p is a YES ask at 1 − p; each book is whole, best first.
    assert [(lv["price"], lv["size"]) for lv in first["bids"]] == [(0.42, 20), (0.40, 50)]
    assert [(lv["price"], lv["size"]) for lv in first["asks"]] == [(0.45, 10), (0.5, 30)]
    assert [(lv["price"], lv["size"]) for lv in last["bids"]] == [(0.44, 12.5), (0.42, 15), (0.40, 50)]
    assert [(lv["price"], lv["size"]) for lv in last["asks"]] == [(0.5, 30)]
    assert first["as_of"] == "2026-10-01T12:00:00Z" and last["as_of"] == "2026-10-01T12:00:03Z"
    assert lines[3] == {
        "kind": "trade",
        "origin": "venue",
        "venue": "kalshi",
        "market": A,
        "price": 0.45,
        "size": 10.0,
        "as_of": "2026-10-01T12:00:02.500000Z",
        "trade_id": "t-1",
        "aggressor": "buy",
        "received_at": lines[3]["received_at"],
    }
    assert lines[5]["aggressor"] == "sell" and lines[6]["status"] == "paused"

    out = Client(mode="backtest", books=load_books(p)).replay()
    assert (out["books"], out["trades"], out["gaps"]) == (4, 2, 0)


def test_a_demo_key_records_from_the_demo_exchange() -> None:
    demo = Kalshi(key_id="kid-1", private_key_pem=PEM, environment="demo")
    assert KalshiStream(demo)._ws_url == "wss://demo-api.kalshi.co/trade-api/ws/v2"


def test_a_recording_and_an_import_of_the_same_messages_hold_the_same_books(tmp_path: Any) -> None:
    clock = Clock()
    recorded = tmp_path / "recorded.jsonl"
    krec(clock, Connect(KSock(clock, *SCRIPT)), [A], recorded, duration_s=30)

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "".join(json.dumps({"received_at": "2026-10-01T12:00:00Z", "message": m}) + "\n" for m in SCRIPT)
    )
    imported = import_events(raw, format="kalshi", markets=[A])
    books: list[Book] = []
    for e in imported:  # the import keeps each delta as a level change; rebuild the whole book
        if isinstance(e, Book):
            books.append(e)
        elif isinstance(e, BookLevelChange):
            books.append(_apply(books[-1], e))

    others = [e for e in imported if not isinstance(e, (Book, BookLevelChange))]
    # The import keeps both copies of the repeated trade; the recorder writes it once.
    assert [e.trade_id for e in others if isinstance(e, TradePrint)] == ["t-1", "t-1", "t-2"]
    others.pop(1)

    def shape(e: Any) -> Any:  # every field but this machine's receive time
        return {k: v for k, v in e.to_dict().items() if k not in ("received_at", "source")}

    rec_events = load_books(recorded)
    imp_events = sorted(books + others, key=lambda e: e.as_of)
    assert [shape(e) for e in rec_events] == [shape(e) for e in imp_events]


def test_a_kalshi_seq_jump_is_a_gap_and_subscribes_again_for_fresh_books(tmp_path: Any) -> None:
    clock = Clock()
    first = KSock(
        clock,
        snap(A, [(0.40, 5)], [(0.55, 5)], 1, 0),
        snap(B, [(0.20, 5)], [(0.75, 5)], 2, 0),
        2,
        delta(A, "yes", 0.41, 3, 3, 2),  # last message before the jump, received at T0+2
        3,
        delta(A, "yes", 0.42, 1, 5, 5),  # seq 4 never arrived
    )
    second = KSock(clock, snap(A, [(0.40, 5)], [(0.55, 5)], 1, 6), snap(B, [(0.20, 5)], [(0.75, 5)], 2, 6))
    alerts: list[dict[str, Any]] = []
    p = tmp_path / "t.jsonl"
    s = krec(clock, Connect(first, second), [A, B], p, duration_s=10, on_alert=alerts.append)
    assert clock.slept == []  # the connection was fine: subscribed again at once
    assert s.reconnects == 1 and [g.market for g in s.gaps] == [A, B]
    gap = s.gaps[0]
    assert gap.as_of == T0 + timedelta(seconds=2) and gap.until == T0 + timedelta(seconds=5)
    assert gap.reason.startswith("missed messages: SequenceGap: Kalshi sequence jumped from 3 to 5")
    assert [a["kind"] for a in alerts] == ["stream_disconnected", "stream_reconnected"]
    assert all(a["venue"] == "kalshi" for a in alerts)
    kinds = [json.loads(x)["kind"] for x in p.read_text().splitlines()]
    # The change after the jump is never applied; the fresh books are written although A's is unchanged.
    assert kinds == ["book", "book", "book", "gap", "gap", "book", "book"]


def test_a_kalshi_trade_seq_jump_is_a_gap_too(tmp_path: Any) -> None:
    clock = Clock()
    first = KSock(
        clock, snap(A, [(0.40, 5)], [], 1, 0), ktrade(A, 0.4, 1, 1, 1, "t1"), ktrade(A, 0.4, 1, 3, 2, "t3")
    )
    s = krec(clock, Connect(first, KSock(clock)), [A], tmp_path / "t.jsonl", duration_s=5)
    assert s.trades == 1 and len(s.gaps) == 1 and "from 1 to 3" in s.gaps[0].reason


def test_a_dropped_kalshi_connection_waits_and_marks_a_gap(tmp_path: Any) -> None:
    clock = Clock()
    first = KSock(clock, snap(A, [(0.40, 5)], [], 1, 0), 4, ConnectionClosedError(None, None))
    s = krec(
        clock,
        Connect(first, KSock(clock, snap(A, [(0.40, 5)], [], 1, 6))),
        [A],
        tmp_path / "t.jsonl",
        duration_s=10,
    )
    assert clock.slept[0] == 1.0 and s.reconnects == 1
    (gap,) = s.gaps
    assert gap.as_of == T0 and gap.reason.startswith("disconnected: ConnectionClosedError")


def test_a_kalshi_error_message_reconnects(tmp_path: Any) -> None:
    clock = Clock()
    err = {"id": 1, "type": "error", "msg": {"code": 18, "msg": "Command timeout"}}
    s = krec(clock, Connect(KSock(clock, err), KSock(clock)), [A], tmp_path / "t.jsonl", duration_s=5)
    assert s.reconnects == 1 and "error 18" in s.gaps[0].reason


def test_a_changed_kalshi_format_stops_the_recording(tmp_path: Any) -> None:
    clock = Clock()
    bad = delta(A, "maybe", 0.4, 1, 2, 1)
    with pytest.raises(VenueError) as e:
        krec(
            clock,
            Connect(KSock(clock, snap(A, [(0.4, 1)], [], 1, 0), bad)),
            [A],
            tmp_path / "t.jsonl",
            duration_s=60,
        )
    assert e.value.code == "format_changed" and e.value.venue == "kalshi"


def test_a_refused_kalshi_key_stops_instead_of_retrying(tmp_path: Any) -> None:
    refused = InvalidStatus(Response(401, "Unauthorized", Headers()))
    connect = Connect(refused, refused)
    with pytest.raises(VenueError) as e:
        krec(Clock(), connect, [A], tmp_path / "t.jsonl", duration_s=60)
    assert e.value.code == "auth_failed" and "Kalshi refused" in str(e.value) and connect.calls == 1


def test_unknown_kalshi_tickers_are_refused_before_connecting(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/markets/KXSAMPLE-TYPO"):
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})
        if "/series/" in path:
            return httpx.Response(
                200, json={"series": {"ticker": "KXSAMPLE", "fee_type": "quadratic", "fee_multiplier": 1}}
            )
        return httpx.Response(
            200, json={"market": {"ticker": A, "status": "active", "event_ticker": "KXSAMPLE-26OCT04"}}
        )

    connect = Connect()
    with pytest.raises(VenueError) as e:
        record_stream(
            [A, "KXSAMPLE-TYPO"],
            tmp_path / "t.jsonl",
            kalshi=KKEY,
            transport=httpx.MockTransport(handler),
            ws_connect=connect,
        )
    assert e.value.code == "not_found" and "KXSAMPLE-TYPO" in str(e.value) and connect.calls == 0


def test_kalshi_needs_a_key(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    with pytest.raises(VenueError) as e:
        record_stream([A], tmp_path / "t.jsonl", check_markets=False)
    assert e.value.code == "auth_failed" and "Kalshi" in str(e.value)
    assert main(["record", A, "--out", str(tmp_path / "t.jsonl")]) == 1


def test_each_id_is_read_as_its_venues() -> None:
    assert market_venue(A) == "kalshi"
    assert market_venue("some-slug") == market_venue("some-slug:short") == "polymarket_us"
    assert market_venue("2026-10-04") == "polymarket_us"  # no letters: not a ticker


class SlowSock(Sock):
    """Plays its script, then waits on the real clock like a quiet connection."""

    def recv(self, timeout: float | None = None) -> str:
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return json.dumps(item)
        time.sleep(0.02)
        raise TimeoutError


def test_one_call_records_kalshi_and_polymarket_us_side_by_side(tmp_path: Any) -> None:
    clock = Clock()  # only for the fake sockets' Date header; the recording runs on the real clock
    kalshi = [
        SlowSock(clock, snap(A, [(0.40, 5)], [(0.55, 5)], 1, 0), ConnectionClosedError(None, None)),
        SlowSock(clock, snap(A, [(0.40, 5)], [(0.55, 5)], 1, 0), ktrade(A, 0.45, 2, 1, 0, "kt")),
    ]
    poly = [
        SlowSock(clock, md("some-slug", [(0.40, 10)], [(0.42, 5)], 0), trade("some-slug", 0.42, 5, 0, "pt"))
    ]

    def connect(url: str, **kw: Any) -> Any:
        return (kalshi if "kalshi" in url else poly).pop(0)

    alerts: list[dict[str, Any]] = []
    p = tmp_path / "t.jsonl"
    s = record_stream(
        ["some-slug", A],
        p,
        polymarket_us=KEY,
        kalshi=KKEY,
        check_markets=False,
        ws_connect=connect,
        sleep=lambda _s: None,
        duration_s=0.5,
        on_alert=alerts.append,
    )
    assert s.venue == "kalshi+polymarket_us" and s.markets == ["some-slug", A]
    assert (s.books, s.trades, s.reconnects) == (3, 2, 1)
    # Kalshi's drop is a gap on Kalshi's market only.
    assert [(g.venue, g.market) for g in s.gaps] == [("kalshi", A)]
    assert [a["venue"] for a in alerts] == ["kalshi", "kalshi"]
    events = load_books(p)
    assert {(e.venue, e.kind) for e in events} == {
        ("kalshi", "book"),
        ("kalshi", "trade"),
        ("kalshi", "gap"),
        ("polymarket_us", "book"),
        ("polymarket_us", "trade"),
    }
    out = Client(mode="backtest", books=events).replay()
    assert (out["books"], out["trades"], out["gaps"]) == (3, 2, 1)


def test_an_error_on_one_venue_stops_both(tmp_path: Any) -> None:
    clock = Clock()
    bad = delta(A, "maybe", 0.4, 1, 2, 1)

    def connect(url: str, **kw: Any) -> Any:
        if "kalshi" in url:
            return SlowSock(clock, snap(A, [(0.4, 1)], [], 1, 0), bad)
        return SlowSock(clock)

    started = time.monotonic()
    with pytest.raises(VenueError) as e:
        record_stream(
            ["some-slug", A],
            tmp_path / "t.jsonl",
            polymarket_us=KEY,
            kalshi=KKEY,
            check_markets=False,
            ws_connect=connect,
            duration_s=60,
        )
    assert e.value.code == "format_changed" and time.monotonic() - started < 5


def test_the_cli_names_each_venue(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import uselayer.record as record

    seen: dict[str, Any] = {}

    def fake(markets: list[str], path: str, **kw: Any) -> Any:
        seen.update(kw, markets=markets)
        kw["on_alert"]({"kind": "stream_disconnected", "venue": "kalshi", "error": "boom"})
        kw["on_event"](
            StreamGap(venue="kalshi", market=A, as_of=T0, until=T0 + timedelta(seconds=2), reason="x")
        )
        return record.RecordSummary(path, "kalshi+polymarket_us", markets, T0, books=3, trades=1)

    monkeypatch.setattr(record, "record_stream", fake)
    assert main(["record", "some-slug", A, B, "--out", str(tmp_path / "t.jsonl"), "--minutes", "1"]) == 0
    out = capsys.readouterr().out
    assert "● recording 2 Kalshi + 1 Polymarket US market(s)" in out
    assert "✗ Kalshi disconnected: boom" in out and f"! gap {A}: 2.0 s" in out
    assert seen["duration_s"] == 60 and seen["venue"] is None
