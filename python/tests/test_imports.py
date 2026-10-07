"""import_events(): every format imports a sample file and replays in backtest; the checker flags each problem."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from uselayer import Book, BookLevelChange, Client, Level, MarketStatus, Resolution, TradePrint, VenueError
from uselayer.backtest import save_events
from uselayer.books import reconstruct_book
from uselayer.imports import check_events, import_events, to_time

SAMPLES = Path(__file__).parent / "fixtures" / "imports"
T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def levels(side: tuple[Level, ...]) -> list[tuple[float, float]]:
    return [(lv.price, lv.size) for lv in side]


def final_book(events: Any, market: str) -> Book:
    book = reconstruct_book(
        [e for e in events if isinstance(e, (Book, BookLevelChange)) and e.market == market]
    )
    assert book is not None
    return book


# ---- each format: a sample file imports and replays --------------------------------------------


def test_csv_top_of_book_with_column_mapping_and_cents_replays_and_fills() -> None:
    data = import_events(
        SAMPLES / "top_of_book.csv",
        venue="kalshi",
        columns={
            "time": "ts",
            "market": "ticker",
            "bid": "yes_bid",
            "bid_size": "yes_bid_qty",
            "ask": "yes_ask",
            "ask_size": "yes_ask_qty",
        },
        price_scale=0.01,
    )
    assert data.report.ok and len(data) == 4
    first = data[0]
    assert (
        isinstance(first, Book) and levels(first.bids) == [(0.41, 120)] and levels(first.asks) == [(0.43, 80)]
    )
    assert first.venue == "kalshi" and first.source == "recorded" and first.as_of == T0

    def on_book(c: Client, b: Book) -> None:
        if b.as_of == T0:  # rest a bid at 40¢; the 14:01 book's ask comes down to it
            c.buy(venue="kalshi", market=b.market, side="yes", price=0.40, size=10, tif="gtc")

    bt = Client(mode="backtest", books=data, rules={"order_ttl_s": 600})
    out = bt.replay(on_book)
    assert out["books"] == 4
    (fill,) = bt.fills()
    assert (fill.venue, fill.price, fill.role, fill.at) == (
        "kalshi",
        0.40,
        "maker",
        T0 + timedelta(seconds=60),
    )


def test_csv_of_every_event_kind() -> None:
    data = import_events(SAMPLES / "events.csv")
    kinds = [e.kind for e in data]
    assert kinds == ["book", "book_change", "trade", "book_change", "book_change", "status", "resolution"]
    trade = data[2]
    assert isinstance(trade, TradePrint) and (trade.trade_id, trade.aggressor, trade.price) == (
        "T1",
        "buy",
        0.56,
    )
    assert data[0].received_at == datetime(2026, 9, 7, 17, 0, 0, 70000, tzinfo=UTC)
    book = final_book(data, "aec-nfl-sample-2026-09-07")
    assert levels(book.bids) == [(0.56, 25), (0.55, 100), (0.54, 250)]
    assert levels(book.asks) == [(0.57, 80), (0.58, 300)]
    assert isinstance(data[5], MarketStatus) and isinstance(data[6], Resolution) and data[6].outcome == "yes"
    # Quiet hours between the last tick and the close are a warning, not an error.
    assert data.report.ok and data.report.counts["gap"] >= 1
    out = Client(mode="backtest", books=data).replay()
    assert (out["books"], out["trades"]) == (4, 1)


def test_parquet_table() -> None:
    data = import_events(SAMPLES / "events.parquet")
    assert data.report.ok and [e.kind for e in data] == [
        "book",
        "book_change",
        "trade",
        "book_change",
        "book_change",
    ]
    book = final_book(data, "KXSAMPLE")
    assert levels(book.bids) == [(0.42, 30), (0.40, 50)] and levels(book.asks) == [(0.47, 60)]
    assert Client(mode="backtest", books=data).replay()["books"] == 4


def test_the_sdks_own_jsonl_round_trips(tmp_path: Path) -> None:
    src = import_events(SAMPLES / "events.csv")
    p = tmp_path / "saved.jsonl"
    save_events(src, p)
    back = import_events(p)
    assert [e.to_dict() for e in back] == [e.to_dict() for e in src]


def test_polymarket_stream_with_both_tokens_folded_into_one_market() -> None:
    data = import_events(
        SAMPLES / "polymarket.jsonl", tokens={"111": ("0xc0ffee", "yes"), "222": ("0xc0ffee", "no")}
    )
    assert data.report.ok
    assert {e.market for e in data} == {"0xc0ffee"} and {e.venue for e in data} == {"polymarket"}
    # The NO token's book is the mirror of the YES token's, so both give the same YES book.
    yes_book, no_book = (e for e in data if isinstance(e, Book))
    assert levels(yes_book.bids) == levels(no_book.bids) == [(0.42, 100), (0.41, 200), (0.40, 500)]
    assert levels(yes_book.asks) == levels(no_book.asks)[:3] == [(0.44, 120), (0.45, 150), (0.46, 300)]
    (trade,) = (e for e in data if isinstance(e, TradePrint))
    assert (trade.price, trade.size, trade.aggressor) == (0.44, 120, "buy")
    book = final_book(data, "0xc0ffee")
    assert book.bids[0] == Level(price=0.43, size=75) and book.asks[0] == Level(price=0.45, size=150)
    (res,) = (e for e in data if isinstance(e, Resolution))
    assert res.outcome == "yes"
    assert Client(mode="backtest", books=data).replay()["books"] == 2 + 4


def test_polymarket_stream_without_a_token_map_keeps_each_token_as_its_own_market() -> None:
    data = import_events(SAMPLES / "polymarket.jsonl")
    assert {e.market for e in data} == {"111", "222"}
    no_token = final_book(data, "222")  # the NO token's own book, as published
    assert no_token.bids[0] == Level(price=0.55, size=150) and no_token.asks[0] == Level(price=0.57, size=75)
    outcomes = {e.market: e.outcome for e in data if isinstance(e, Resolution)}
    assert outcomes == {"111": "yes", "222": "no"}


def test_kalshi_stream_applies_quantity_changes_and_mirrors_no_bids() -> None:
    data = import_events(SAMPLES / "kalshi.jsonl")
    assert data.report.ok
    snap = data[0]
    assert isinstance(snap, Book)
    assert snap.as_of == to_time(1790002800090)  # a snapshot has no time of its own: Kalshi's send time
    assert levels(snap.bids) == [(0.42, 20), (0.40, 50)] and levels(snap.asks) == [(0.45, 10), (0.50, 30)]
    book = final_book(data, "KXSAMPLE-26SEP21-T50")
    assert levels(book.bids) == [(0.44, 12.5), (0.42, 15), (0.40, 50)] and levels(book.asks) == [(0.50, 30)]
    (trade,) = (e for e in data if isinstance(e, TradePrint))
    assert (trade.price, trade.size, trade.trade_id, trade.aggressor) == (0.45, 10, "t-1", "buy")
    assert [e.status for e in data if isinstance(e, MarketStatus)] == ["paused"]
    assert [e.outcome for e in data if isinstance(e, Resolution)] == ["no"]
    out = Client(mode="backtest", books=data).replay()
    assert (out["books"], out["trades"]) == (4, 1)


def test_polymarket_us_stream() -> None:
    data = import_events(SAMPLES / "polymarket_us.jsonl")
    assert data.report.ok
    books = [e for e in data if isinstance(e, Book)]
    assert levels(books[0].bids) == [(0.495, 1200), (0.49, 3000)] and books[0].asks[0] == Level(
        price=0.5, size=800
    )
    assert books[0].as_of == datetime(2026, 9, 22, 23, 0, 0, 1234, tzinfo=UTC)
    (trade,) = (e for e in data if isinstance(e, TradePrint))
    assert (trade.price, trade.size, trade.trade_id, trade.aggressor) == (0.5, 300, "TRADE1", "buy")
    assert [e.status for e in data if isinstance(e, MarketStatus)] == ["open", "closed"]
    assert [e.outcome for e in data if isinstance(e, Resolution)] == ["yes"]
    assert Client(mode="backtest", books=data).replay()["books"] == 3


def test_pmxt_v2_hour() -> None:
    data = import_events(SAMPLES / "pmxt_v2.parquet", markets=["1001"])
    assert data.report.ok
    assert {e.market for e in data} == {"1001"}  # the other market in the hour is filtered out
    # The re-sent old book is skipped, and the ask the venue's stamp says is gone is removed.
    assert data.report.counts["stale_book"] == 1 and data.report.counts["repaired"] == 1
    book = final_book(data, "1001")
    assert levels(book.bids) == [(0.43, 75), (0.42, 100), (0.41, 200), (0.40, 450)]
    assert book.asks[0] == Level(price=0.45, size=150)
    trade = next(e for e in data if isinstance(e, TradePrint))
    assert (trade.price, trade.aggressor, trade.received_at) == (
        0.44,
        "buy",
        to_time("2026-07-21T04:00:02.100Z"),
    )
    # Inside a batch, venue times were shuffled; events come back in venue-time order.
    changes = [e for e in data if isinstance(e, BookLevelChange)]
    assert [c.price for c in changes[:2]] == [0.40, 0.43]
    assert Client(mode="backtest", books=data).replay()["books"] >= 4


def test_pmxt_v2_folds_the_no_token_and_tracks_tick_changes() -> None:
    data = import_events(
        SAMPLES / "pmxt_v2.parquet",
        markets=["1001", "1002"],
        tokens={"1001": ("m", "yes"), "1002": ("m", "no")},
    )
    no_book = next(e for e in data if isinstance(e, Book) and e.as_of == to_time("2026-07-21T04:00:04.100Z"))
    assert levels(no_book.bids) == [(0.43, 75), (0.42, 100)] and levels(no_book.asks) == [
        (0.45, 150),
        (0.46, 120),
    ]


def test_pmxt_v1_hour() -> None:
    data = import_events(SAMPLES / "pmxt_v1.parquet", markets=["1001"])
    assert data.report.ok and len(data) == 3
    book = final_book(data, "1001")
    assert levels(book.bids) == [(0.43, 75), (0.42, 100), (0.41, 200)] and levels(book.asks) == [(0.45, 150)]


def test_pmxt_needs_a_market_filter() -> None:
    with pytest.raises(VenueError) as e:
        import_events(SAMPLES / "pmxt_v2.parquet")
    assert e.value.code == "invalid_order" and "markets=" in str(e.value)


def test_formats_are_detected_from_the_file() -> None:
    for name, venue in [
        ("polymarket.jsonl", "polymarket"),
        ("kalshi.jsonl", "kalshi"),
        ("polymarket_us.jsonl", "polymarket_us"),
        ("events.parquet", "kalshi"),
    ]:
        assert {e.venue for e in import_events(SAMPLES / name)} == {venue}, name
    assert {e.venue for e in import_events(SAMPLES / "pmxt_v1.parquet", markets=["1001"])} == {"polymarket"}


def test_several_files_join_up(tmp_path: Path) -> None:
    lines = (SAMPLES / "kalshi.jsonl").read_text().splitlines()
    a, b = tmp_path / "hour1.jsonl", tmp_path / "hour2.jsonl"
    a.write_text("\n".join(lines[:4]) + "\n")
    b.write_text("\n".join(lines[4:]) + "\n")
    joined = import_events([a, b])
    assert [e.to_dict() for e in joined] == [e.to_dict() for e in import_events(SAMPLES / "kalshi.jsonl")]


# ---- the checker: a broken file for each problem ------------------------------------------------

HEAD = "kind,venue,market,time,received_at,bids,asks,book_side,price,size\n"


def book_row(t: str, bids: str, asks: str, recv: str = "") -> str:
    return f'book,kalshi,M,{t},{recv},"{bids}","{asks}",,,\n'


BROKEN: dict[str, tuple[str, str]] = {
    "crossed": (
        "error",
        HEAD
        + book_row("2026-09-01T14:00:00Z", "[[0.40,5]]", "[[0.45,5]]")
        + "book_change,kalshi,M,2026-09-01T14:00:01Z,,,,bid,0.46,5\n",
    ),
    "off_tick": ("error", HEAD + book_row("2026-09-01T14:00:00Z", "[[0.4005,5]]", "[[0.45,5]]")),
    "impossible": (
        "error",
        HEAD
        + book_row("2026-09-01T14:00:00Z", "[[0.40,5]]", "[[0.45,5]]")
        + "trade,kalshi,M,2026-09-01T14:00:01Z,,,,,45,5\n",  # cents read as dollars
    ),
    "unreadable": ("error", HEAD + "book,kalshi,M,not-a-time,,[],[],,,\n"),
    "out_of_order": (
        "warning",
        HEAD
        + book_row("2026-09-01T14:00:05Z", "[[0.40,5]]", "[[0.45,5]]")
        + book_row("2026-09-01T14:00:01Z", "[[0.40,5]]", "[[0.45,5]]"),
    ),
    "gap": (
        "warning",
        HEAD
        + "".join(book_row(f"2026-09-01T14:00:{s:02d}Z", "[[0.40,5]]", "[[0.45,5]]") for s in range(10))
        + book_row("2026-09-01T16:00:00Z", "[[0.40,5]]", "[[0.45,5]]"),
    ),
    "no_starting_book": ("warning", HEAD + "book_change,kalshi,M,2026-09-01T14:00:01Z,,,,bid,0.40,5\n"),
}


@pytest.mark.parametrize("problem", sorted(BROKEN))
def test_the_checker_flags_a_broken_file(problem: str, tmp_path: Path) -> None:
    severity, text = BROKEN[problem]
    p = tmp_path / f"{problem}.csv"
    p.write_text(text)
    data = import_events(p, strict=False)
    assert data.report.counts[problem] >= 1, data.report.summary()
    found = next(x for x in data.report.problems if x.kind == problem)
    assert found.severity == severity
    if problem not in ("gap",):
        assert found.row is not None
    if severity == "error":
        with pytest.raises(VenueError) as e:
            import_events(p)
        assert e.value.code == "bad_data" and problem in str(e.value) and e.value.raw["errors"] >= 1
    else:
        import_events(p)  # warnings never stop an import


def test_a_kalshi_sequence_gap_is_an_error(tmp_path: Path) -> None:
    lines = (SAMPLES / "kalshi.jsonl").read_text().splitlines()
    p = tmp_path / "lost.jsonl"
    p.write_text("\n".join(lines[:2] + lines[3:]) + "\n")  # seq 2 lost
    with pytest.raises(VenueError) as e:
        import_events(p)
    assert "sequence_gap" in str(e.value) and "jumped from 1 to 3" in str(e.value)


def test_a_polymarket_re_sent_book_is_skipped_not_replayed(tmp_path: Path) -> None:
    lines = (SAMPLES / "polymarket.jsonl").read_text().splitlines()
    old = json.loads(lines[0])
    resent = {
        "received_at": "2026-09-21T14:13:30Z",
        "message": {**old["message"][0], "bids": [{"price": "0.1", "size": "1"}]},
    }
    p = tmp_path / "resent.jsonl"
    p.write_text("\n".join([*lines[:4], json.dumps(resent)]) + "\n")
    data = import_events(p)
    assert data.report.counts["stale_book"] == 1
    assert final_book(data, "111").bids[0] == Level(price=0.43, size=75)


def test_tick_size_comes_from_the_data_or_the_caller() -> None:
    e = Book(venue="kalshi", market="M", bids=(Level(price=0.405, size=1),), asks=(), as_of=T0)
    assert check_events([e]).ok  # the finest Kalshi tick is 0.001
    assert check_events([e], tick_size=0.01).counts["off_tick"] == 1
    assert check_events([e], tick_size={"M": 0.01}).counts["off_tick"] == 1
    late = e.model_copy(update={"as_of": T0 + timedelta(seconds=10)})
    changes = [("kalshi", "M", T0 + timedelta(seconds=5), 0.01)]
    assert check_events([e, late], tick_changes=changes).counts["off_tick"] == 1  # only after the change


def test_summary_and_report_are_readable() -> None:
    e = Book(
        venue="kalshi",
        market="M",
        bids=(Level(price=0.5, size=1),),
        asks=(Level(price=0.5, size=1),),
        as_of=T0,
    )
    rep = check_events([e])
    assert not rep.ok and rep.errors == 1 and "crossed" in rep.summary()
    d = rep.to_dict()
    assert d["ok"] is False and d["counts"] == {"crossed": 1} and d["problems"][0]["market"] == "M"


def test_times_in_every_shape() -> None:
    want = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    for v in (
        want,
        want.replace(tzinfo=None),
        "2026-09-01T14:00:00Z",
        "2026-09-01 14:00:00",
        "2026-09-01T14:00:00.000000000Z",
        want.timestamp(),
        want.timestamp() * 1000,
        int(want.timestamp() * 1_000_000),
        int(want.timestamp() * 1_000_000_000),
        str(int(want.timestamp())),
    ):
        assert to_time(v) == want, v


# ---- backtest replays any venue it has fee rules for ---------------------------------------------


def test_backtest_replays_polymarket_and_kalshi_but_paper_still_refuses_them() -> None:
    data = import_events(SAMPLES / "polymarket.jsonl")
    assert Client(mode="backtest", books=data).replay()["books"] > 0
    with pytest.raises(VenueError) as e:
        Client(mode="paper", store=":memory:").book("111", venue="polymarket")
    assert e.value.code == "venue_switched_off"


def test_missing_file_and_unknown_columns() -> None:
    with pytest.raises(VenueError) as e:
        import_events(SAMPLES / "nope.csv")
    assert e.value.code == "not_found"
    with pytest.raises(VenueError) as e:
        import_events(SAMPLES / "events.csv", columns={"timestamp": "time"})
    assert "timestamp" in str(e.value)


def test_a_record_stream_gap_is_reported_and_clears_the_book(tmp_path: Path) -> None:
    from uselayer.events import StreamGap

    def book(s: int) -> Book:
        return Book(
            venue="polymarket_us",
            market="m",
            bids=(Level(price=0.4, size=5),),
            asks=(Level(price=0.45, size=5),),
            as_of=T0 + timedelta(seconds=s),
        )

    gap = StreamGap(
        venue="polymarket_us",
        market="m",
        as_of=T0 + timedelta(seconds=2),
        until=T0 + timedelta(seconds=30),
        reason="connection closed",
    )
    after = BookLevelChange(
        venue="polymarket_us",
        market="m",
        book_side="bid",
        price=0.41,
        size=5,
        as_of=T0 + timedelta(seconds=31),
    )
    p = tmp_path / "ticks.jsonl"
    save_events([book(0), book(1), gap, after, book(40)], p)
    data = import_events(p)
    assert data.report.ok and data.report.counts["gap"] >= 1
    assert (
        data.report.counts["no_starting_book"] == 1
    )  # the change right after the gap has no book to apply to
    out = Client(mode="backtest", books=data).replay()
    assert (out["books"], out["gaps"]) == (3, 1)
