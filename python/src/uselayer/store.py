"""The local store: one SQLite file per mode, on your machine only.

``~/.uselayer/paper.db``, ``~/.uselayer/live.db`` and ``~/.uselayer/backtest.db`` by default, or a
path you choose (``":memory:"`` for tests). Keeping one file per mode means paper fills can never count
toward live positions or live daily loss.

What's in it: orders, fills, settlements (paper and backtest), what each venue paid on the markets
of live pairs, resolution mismatches (:mod:`uselayer.mismatch`), the decision journal, start-of-day
marks, the kill flag and each resting paper order's estimated place in line. What's not:
venue keys. Nothing in it is ever uploaded.

The file uses WAL mode, so ``python -m uselayer kill`` can set the kill flag while a strategy runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .events import Fill, SimulatedFill, SimulatedSettlement
from .mismatch import ResolutionMismatch
from .orders import Order

AnyFill = Fill | SimulatedFill

SCHEMA = """
create table if not exists orders (
  id text primary key,
  mode text not null,
  venue text not null,
  market text not null,
  status text not null,
  group_id text,
  created_at text not null,
  updated_at text not null,
  body text not null
);
create index if not exists orders_status on orders(status);
create table if not exists fills (
  seq integer primary key autoincrement,
  uid text unique,
  order_id text not null,
  venue text not null,
  market text not null,
  at text not null,
  body text not null
);
create index if not exists fills_at on fills(at);
create table if not exists settlements (
  seq integer primary key autoincrement,
  venue text not null,
  market text not null,
  side text not null,
  at text not null,
  body text not null,
  unique (venue, market, side)
);
create table if not exists decisions (
  seq integer primary key autoincrement,
  at text not null,
  order_id text,
  rule text,
  result text not null,
  reason text,
  inputs text,
  config_hash text not null
);
create table if not exists flags (
  name text primary key,
  value text not null,
  at text not null,
  by text
);
create table if not exists day_marks (
  day text not null,
  venue text not null,
  market text not null,
  side text not null,
  mark real not null,
  primary key (day, venue, market, side)
);
create table if not exists resting_lines (
  order_id text primary key,
  body text not null
);
create table if not exists resolution_mismatches (
  group_id text primary key,
  kind text not null,
  detected_at text not null,
  updated_at text not null,
  body text not null
);
create table if not exists venue_payouts (
  venue text not null,
  market text not null,
  yes real not null,
  at text not null,
  seen_at text not null,
  primary key (venue, market)
);
"""


def default_path(mode: str) -> Path:
    """Where a mode's store lives unless you pass ``store=``: ``~/.uselayer/<mode>.db``."""
    return Path(os.environ.get("USELAYER_HOME", Path.home() / ".uselayer")) / f"{mode}.db"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


class Store:
    """The SQLite file for one mode. Safe to share between threads of one process.

    store = Store(":memory:")
    store.set_killed(True, by="test")
    store.killed()  # True
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.existed = self.path == ":memory:" or Path(self.path).exists()
        if self.path != ":memory:":
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not p.exists():
                fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            os.chmod(p, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("begin immediate")
            try:
                yield self._db
                self._db.execute("commit")
            except BaseException:
                self._db.execute("rollback")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- orders ----

    def save_order(self, order: Order) -> None:
        assert order.id and order.mode and order.status and order.created_at and order.updated_at
        with self._tx() as db:
            db.execute(
                "insert into orders(id, mode, venue, market, status, group_id, created_at, updated_at, body) values (?,?,?,?,?,?,?,?,?) "
                "on conflict(id) do update set status=excluded.status, updated_at=excluded.updated_at, body=excluded.body",
                (
                    order.id,
                    order.mode,
                    order.venue,
                    order.market,
                    order.status,
                    order.group_id,
                    _iso(order.created_at),
                    _iso(order.updated_at),
                    order.model_dump_json(),
                ),
            )

    def order(self, order_id: str) -> Order | None:
        with self._lock:
            row = self._db.execute("select body from orders where id = ?", (order_id,)).fetchone()
        return Order.model_validate_json(row["body"]) if row else None

    def orders(self, *, open_only: bool = False) -> list[Order]:
        q = (
            "select body from orders"
            + (" where status in ('pending','open')" if open_only else "")
            + " order by created_at"
        )
        with self._lock:
            rows = self._db.execute(q).fetchall()
        return [Order.model_validate_json(r["body"]) for r in rows]

    # ---- fills ----

    def add_fill(self, fill: AnyFill) -> bool:
        """Save a fill. A real fill the venue already reported (same ``venue_fill_id``) is saved once.

        Returns whether it was new.
        """
        uid = f"{fill.venue}:{fill.venue_fill_id}" if isinstance(fill, Fill) and fill.venue_fill_id else None
        with self._tx() as db:
            cur = db.execute(
                "insert or ignore into fills(uid, order_id, venue, market, at, body) values (?,?,?,?,?,?)",
                (uid, fill.order_id, fill.venue, fill.market, _iso(fill.at), fill.model_dump_json()),
            )
            return cur.rowcount == 1

    def fills(self, *, since: datetime | None = None) -> list[AnyFill]:
        with self._lock:
            if since is None:
                rows = self._db.execute("select body from fills order by seq").fetchall()
            else:
                rows = self._db.execute(
                    "select body from fills where at >= ? order by seq", (_iso(since),)
                ).fetchall()
        out: list[AnyFill] = []
        for r in rows:
            body = json.loads(r["body"])
            out.append(
                SimulatedFill.model_validate(body) if body.get("simulated") else Fill.model_validate(body)
            )
        return out

    # ---- settlements (paper and backtest) ----

    def add_settlement(self, s: SimulatedSettlement) -> bool:
        """Save a payout. A position settles once: a second payout for it is ignored. Returns whether it was new."""
        with self._tx() as db:
            cur = db.execute(
                "insert or ignore into settlements(venue, market, side, at, body) values (?,?,?,?,?)",
                (s.venue, s.market, s.side, _iso(s.at), s.model_dump_json()),
            )
            return cur.rowcount == 1

    def settlements(self) -> list[SimulatedSettlement]:
        with self._lock:
            rows = self._db.execute("select body from settlements order by seq").fetchall()
        return [SimulatedSettlement.model_validate_json(r["body"]) for r in rows]

    # ---- what each venue paid on a live pair's markets, and resolution mismatches ----

    def add_venue_payout(
        self, venue: str, market: str, yes: float, *, at: datetime, seen_at: datetime
    ) -> bool:
        """Save what one YES contract of a market paid (live). A market's first answer is kept. Returns whether it was new."""
        with self._tx() as db:
            cur = db.execute(
                "insert or ignore into venue_payouts(venue, market, yes, at, seen_at) values (?,?,?,?,?)",
                (venue, market, yes, _iso(at), _iso(seen_at)),
            )
            return cur.rowcount == 1

    def venue_payouts(self) -> list[tuple[str, str, float, datetime]]:
        """``(venue, market, yes, at)`` for every market whose payout was saved."""
        with self._lock:
            rows = self._db.execute("select venue, market, yes, at from venue_payouts").fetchall()
        return [(r["venue"], r["market"], float(r["yes"]), datetime.fromisoformat(r["at"])) for r in rows]

    def save_mismatch(self, m: ResolutionMismatch, *, at: datetime) -> None:
        """Save or update a pair's resolution mismatch (one per ``group_id``)."""
        with self._tx() as db:
            db.execute(
                "insert into resolution_mismatches(group_id, kind, detected_at, updated_at, body) values (?,?,?,?,?) "
                "on conflict(group_id) do update set kind=excluded.kind, updated_at=excluded.updated_at, body=excluded.body",
                (m.group_id, m.kind, _iso(m.detected_at), _iso(at), json.dumps(m.to_dict())),
            )

    def mismatches(self) -> list[ResolutionMismatch]:
        """Every resolution mismatch saved, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "select body from resolution_mismatches order by detected_at, group_id"
            ).fetchall()
        return [ResolutionMismatch.from_dict(json.loads(r["body"])) for r in rows]

    # ---- decision journal ----

    def journal(
        self,
        *,
        at: datetime,
        order_id: str | None,
        rule: str | None,
        result: str,
        reason: str | None,
        inputs: dict[str, Any],
        config_hash: str,
    ) -> None:
        with self._tx() as db:
            db.execute(
                "insert into decisions(at, order_id, rule, result, reason, inputs, config_hash) values (?,?,?,?,?,?,?)",
                (
                    _iso(at),
                    order_id,
                    rule,
                    result,
                    reason,
                    json.dumps(inputs, default=str, sort_keys=True),
                    config_hash,
                ),
            )

    def decisions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """The latest decisions, newest first."""
        with self._lock:
            rows = self._db.execute("select * from decisions order by seq desc limit ?", (limit,)).fetchall()
        return [{**dict(r), "inputs": json.loads(r["inputs"] or "{}")} for r in rows]

    # ---- kill flag ----

    def set_killed(self, killed: bool, *, by: str) -> None:
        with self._tx() as db:
            db.execute(
                "insert into flags(name, value, at, by) values ('killed', ?, ?, ?) "
                "on conflict(name) do update set value=excluded.value, at=excluded.at, by=excluded.by",
                ("1" if killed else "0", _iso(datetime.now(UTC)), by),
            )

    def killed(self) -> bool:
        with self._lock:
            row = self._db.execute("select value from flags where name = 'killed'").fetchone()
        return bool(row and row["value"] == "1")

    def kill_info(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("select value, at, by from flags where name = 'killed'").fetchone()
        return dict(row) if row else None

    # ---- start-of-day marks (max daily loss) ----

    def day_mark(self, day: str, venue: str, market: str, side: str) -> float | None:
        with self._lock:
            row = self._db.execute(
                "select mark from day_marks where day=? and venue=? and market=? and side=?",
                (day, venue, market, side),
            ).fetchone()
        return float(row["mark"]) if row else None

    def set_day_mark(self, day: str, venue: str, market: str, side: str, mark: float) -> None:
        with self._tx() as db:
            db.execute(
                "insert or ignore into day_marks(day, venue, market, side, mark) values (?,?,?,?,?)",
                (day, venue, market, side, mark),
            )

    # ---- each resting paper order's place in line (see uselayer.resting) ----

    def line(self, order_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "select body from resting_lines where order_id = ?", (order_id,)
            ).fetchone()
        return str(row["body"]) if row else None

    def save_line(self, order_id: str, body: str) -> None:
        with self._tx() as db:
            db.execute(
                "insert into resting_lines(order_id, body) values (?, ?) "
                "on conflict(order_id) do update set body=excluded.body",
                (order_id, body),
            )

    def drop_line(self, order_id: str) -> None:
        with self._tx() as db:
            db.execute("delete from resting_lines where order_id = ?", (order_id,))
