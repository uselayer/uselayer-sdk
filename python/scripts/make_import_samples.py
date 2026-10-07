"""Write the Parquet sample files in tests/fixtures/imports/ (run from python/: uv run python scripts/make_import_samples.py).

The values are made up; the column names and types are copied from real files: PMXT's two hourly
schemas (``polymarket_orderbook_2026-02-24T16.parquet`` for v1, ``..._2026-07-21T04.parquet`` for v2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "imports"
T0 = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)
COND = "0x" + "ab" * 32
YES, NO = "1001", "1002"


def ms(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def generic() -> None:
    rows = [
        {
            "kind": "book",
            "venue": "kalshi",
            "market": "KXSAMPLE",
            "time": ms(0),
            "bids": "[[0.40,50]]",
            "asks": "[[0.45,20]]",
            "book_side": None,
            "price": None,
            "size": None,
        },
        {
            "kind": "book_change",
            "venue": "kalshi",
            "market": "KXSAMPLE",
            "time": ms(1),
            "bids": None,
            "asks": None,
            "book_side": "bid",
            "price": 0.42,
            "size": 30.0,
        },
        {
            "kind": "trade",
            "venue": "kalshi",
            "market": "KXSAMPLE",
            "time": ms(2),
            "bids": None,
            "asks": None,
            "book_side": None,
            "price": 0.45,
            "size": 20.0,
        },
        {
            "kind": "book_change",
            "venue": "kalshi",
            "market": "KXSAMPLE",
            "time": ms(2),
            "bids": None,
            "asks": None,
            "book_side": "ask",
            "price": 0.45,
            "size": 0.0,
        },
        {
            "kind": "book_change",
            "venue": "kalshi",
            "market": "KXSAMPLE",
            "time": ms(3),
            "bids": None,
            "asks": None,
            "book_side": "ask",
            "price": 0.47,
            "size": 60.0,
        },
    ]
    schema = pa.schema(
        [
            ("kind", pa.string()),
            ("venue", pa.string()),
            ("market", pa.string()),
            ("time", pa.timestamp("ms", tz="UTC")),
            ("bids", pa.string()),
            ("asks", pa.string()),
            ("book_side", pa.string()),
            ("price", pa.float64()),
            ("size", pa.float64()),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), OUT / "events.parquet")


def pmxt_v2() -> None:
    d = Decimal
    base = {
        "market": COND.encode(),
        "bids": None,
        "asks": None,
        "price": None,
        "size": None,
        "side": None,
        "best_bid": None,
        "best_ask": None,
        "fee_rate_bps": None,
        "transaction_hash": None,
        "old_tick_size": None,
        "new_tick_size": None,
    }

    def row(recv: float, venue: float, event: str, asset: str, **kw: object) -> dict[str, object]:
        return {
            **base,
            "timestamp_received": ms(recv),
            "timestamp": ms(venue),
            "event_type": event,
            "asset_id": asset,
            **kw,
        }

    rows = [
        # Polymarket lists bids worst first and asks worst first, as here.
        row(
            0.10,
            0.05,
            "book",
            YES,
            bids=json.dumps([["0.40", "500"], ["0.41", "200"], ["0.42", "100"]]),
            asks=json.dumps([["0.46", "300"], ["0.45", "150"], ["0.44", "120"]]),
        ),
        # One batch: venue times shuffled inside it, as the real stream does.
        row(
            1.20,
            1.10,
            "price_change",
            YES,
            price=d("0.4300"),
            size=d("75"),
            side="BUY",
            best_bid=d("0.4300"),
            best_ask=d("0.4400"),
        ),
        row(
            1.20,
            1.00,
            "price_change",
            YES,
            price=d("0.4000"),
            size=d("450"),
            side="BUY",
            best_bid=d("0.4200"),
            best_ask=d("0.4400"),
        ),
        row(
            2.10,
            2.00,
            "last_trade_price",
            YES,
            price=d("0.4400"),
            size=d("120"),
            side="BUY",
            fee_rate_bps=0,
            transaction_hash="0xfeed",
        ),
        # The ask at 0.44 was taken, but its removal never reached the file: the stamp says best ask 0.45.
        row(
            2.20,
            2.05,
            "price_change",
            YES,
            price=d("0.4500"),
            size=d("150"),
            side="SELL",
            best_bid=d("0.4300"),
            best_ask=d("0.4500"),
        ),
        # A re-sent old book, stamped long before newer data.
        row(3.00, 0.01, "book", YES, bids=json.dumps([["0.30", "1"]]), asks=json.dumps([["0.70", "1"]])),
        row(4.00, 3.90, "tick_size_change", YES, old_tick_size=d("0.0100"), new_tick_size=d("0.0010")),
        # Another market in the same hour, filtered out by markets=.
        row(4.10, 4.00, "book", "9999", bids=json.dumps([["0.10", "5"]]), asks=json.dumps([["0.90", "5"]])),
        # The NO token's mirror of the first book.
        row(
            4.20,
            4.10,
            "book",
            NO,
            bids=json.dumps([["0.54", "120"], ["0.55", "150"]]),
            asks=json.dumps([["0.58", "100"], ["0.57", "75"]]),
        ),
    ]
    dec = pa.decimal128(9, 4)
    schema = pa.schema(
        [
            pa.field("timestamp_received", pa.timestamp("ms", tz="UTC"), nullable=False),
            pa.field("timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
            pa.field("market", pa.binary(66), nullable=False),
            pa.field("event_type", pa.string(), nullable=False),
            pa.field("asset_id", pa.string(), nullable=False),
            ("bids", pa.string()),
            ("asks", pa.string()),
            ("price", dec),
            ("size", pa.decimal128(18, 6)),
            ("side", pa.string()),
            ("best_bid", dec),
            ("best_ask", dec),
            ("fee_rate_bps", pa.uint16()),
            ("transaction_hash", pa.string()),
            ("old_tick_size", dec),
            ("new_tick_size", dec),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), OUT / "pmxt_v2.parquet")


def pmxt_v1() -> None:
    def row(recv: float, update: str, token: str, side: str, data: dict[str, object]) -> dict[str, object]:
        body = {
            "update_type": update,
            "market_id": COND,
            "token_id": token,
            "side": side,
            "timestamp": ms(recv).timestamp(),
            **data,
        }
        return {
            "timestamp_received": ms(recv),
            "timestamp_created_at": ms(recv + 0.5),
            "market_id": COND,
            "update_type": update,
            "data": json.dumps(body),
        }

    rows = [
        # v1 books are Python-repr lists with single quotes, as PMXT wrote them.
        row(
            0.1,
            "book_snapshot",
            YES,
            "YES",
            {
                "best_bid": "0.42",
                "best_ask": "0.44",
                "bids": str([["0.41", "200"], ["0.42", "100"]]),
                "asks": str([["0.45", "150"], ["0.44", "120"]]),
            },
        ),
        row(
            1.0,
            "price_change",
            YES,
            "YES",
            {
                "best_bid": "0.43",
                "best_ask": "0.44",
                "change_price": "0.43",
                "change_size": "75",
                "change_side": "BUY",
            },
        ),
        row(
            2.0,
            "price_change",
            YES,
            "YES",
            {
                "best_bid": "0.43",
                "best_ask": "0.45",
                "change_price": "0.44",
                "change_size": "0",
                "change_side": "SELL",
            },
        ),
    ]
    schema = pa.schema(
        [
            pa.field("timestamp_received", pa.timestamp("ms", tz="UTC"), nullable=False),
            pa.field("timestamp_created_at", pa.timestamp("ms", tz="UTC"), nullable=False),
            pa.field("market_id", pa.string(), nullable=False),
            pa.field("update_type", pa.string(), nullable=False),
            pa.field("data", pa.string(), nullable=False),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), OUT / "pmxt_v1.parquet")


if __name__ == "__main__":
    generic()
    pmxt_v2()
    pmxt_v1()
    print(f"wrote {OUT}/events.parquet, pmxt_v2.parquet, pmxt_v1.parquet")
