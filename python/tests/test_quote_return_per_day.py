# ruff: noqa: F811  (fixtures imported from other test modules)
"""quote() shows the return per day the money is tied up, worked out as client.profit() works it out.

- The payout time is the later of the two markets' expected payouts (Layer's rule, fee_lookup.payout_times),
  at least a day away; ``settles_at`` passed in wins, checked as profit() checks it.
- No venue time and no settles_at: the three fields are None, never guessed.
- quote() and profit() agree on the same pair, to the rounding profit() uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import T0
from test_fee_lookup import pair, world  # noqa: F401  (the fixture)
from test_kalshi import KMarket

from uselayer import Book, Client, Level, VenueError

PAIR = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]


def _kalshi_edge(k: Any) -> None:
    """Kalshi NO at 0.50 against Polymarket US YES at 0.42: a gap the fees don't close."""
    k.markets["KXEV-1-A"] = KMarket("KXEV-1-A", yes_bids=[(0.50, 20)], no_bids=[(0.40, 20)])


def test_quote_and_profit_agree_on_the_same_pair(world: Any) -> None:
    make, k, v = world
    _kalshi_edge(k)
    k.times["KXEV-1-A"] = ("2026-10-03T17:00:00Z", "2026-10-17T17:00:00Z")
    v.times["mkt-a"] = ("2026-10-03T16:00:00Z", "2026-10-03T20:00:00Z")
    c = make()
    q = c.quote(pair(), size=10)
    assert q.contracts == 10 and q.a and q.b
    legs = {q.a.venue: q.a, q.b.venue: q.b}
    r = c.profit(
        {
            "contracts": q.contracts,
            "kalshi": {"price": legs["kalshi"].average_price},
            "polymarket_us": {"price": legs["polymarket_us"].average_price},
        },
        pair=pair(),
    )
    assert (q.net_profit, q.fees, q.return_pct) == (r["net_profit"], r["fees"], r["return_pct"])
    assert (q.days_held, q.return_per_day_pct) == (r["days_held"], r["return_per_day_pct"])
    assert q.settles_at == datetime(2026, 10, 3, 23, tzinfo=UTC)  # Kalshi's 17:00 event + 6 h
    assert r["match"]["expected_payout_at"] == "2026-10-03T23:00:00.000Z"
    days = (q.settles_at - T0).total_seconds() / 86400
    assert q.days_held == round(days, 2)
    assert q.return_per_day_pct == pytest.approx(q.return_pct / days, abs=1e-3)


def test_settles_at_wins_and_is_checked_like_profit(world: Any) -> None:
    make, k, _ = world
    _kalshi_edge(k)
    c = make()
    q = c.quote(pair(), size=10, settles_at="2026-10-11")
    r = c.profit(
        {
            "contracts": 10,
            "settles_at": "2026-10-11",
            "kalshi": {"price": 0.5},
            "polymarket_us": {"price": 0.42},
        },
        pair=pair(),
    )
    assert q.days_held == r["days_held"] == 9.5 and q.return_per_day_pct == r["return_per_day_pct"]
    assert q.settles_at == datetime(2026, 10, 11, tzinfo=UTC)
    aware = c.quote(pair(), size=10, settles_at=datetime(2026, 10, 11, tzinfo=UTC))
    assert aware.days_held == 9.5
    for bad in ("2026-09-30", "2026-10-11T00:00:00", "soon"):
        with pytest.raises(VenueError) as e:
            c.quote(pair(), settles_at=bad)
        assert e.value.code == "invalid_order"


def test_no_venue_time_means_none_never_a_guess(world: Any) -> None:
    make, k, v = world
    _kalshi_edge(k)
    k.times["KXEV-1-A"] = (None, None)
    v.times["mkt-a"] = (None, None)
    q = make().quote(pair(), size=10)
    assert q.contracts == 10 and q.return_pct > 0
    assert (q.settles_at, q.days_held, q.return_per_day_pct) == (None, None, None)
    d = q.to_dict()
    assert (d["settles_at"], d["days_held"], d["return_per_day_pct"]) == (None, None, None)


def test_a_payout_within_a_day_counts_as_one_day(world: Any) -> None:
    make, k, v = world
    _kalshi_edge(k)
    soon = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    k.times["KXEV-1-A"] = (None, soon)
    v.times["mkt-a"] = (None, soon)
    q = make().quote(pair(), size=10)
    assert q.days_held == 1 and q.return_per_day_pct == pytest.approx(q.return_pct, abs=0.01)


def test_paper_quote_reads_polymarket_us_end_dates(make_client: Any) -> None:
    q = make_client().quote(PAIR, size=10)  # both fake markets end 2026-12-31: + 6 h, Layer's rule
    assert q.settles_at == datetime(2026, 12, 31, 0, tzinfo=UTC)  # never past the later close
    days = (q.settles_at - T0).total_seconds() / 86400
    assert q.days_held == round(days, 2)
    assert q.return_per_day_pct == pytest.approx(q.return_pct / days, abs=1e-3)
    d = json.loads(json.dumps(q.to_dict()))
    assert d["settles_at"] == "2026-12-31T00:00:00+00:00" and d["return_per_day_pct"] == q.return_per_day_pct


def test_a_quote_with_no_contracts_still_shows_the_days(make_client: Any) -> None:
    q = make_client().quote(PAIR, size=10, min_edge=0.5)
    assert q.contracts == 0 and q.return_per_day_pct == 0 and q.days_held is not None


def _books() -> list[Book]:
    def book(market: str, s: int, bid: float, ask: float) -> Book:
        return Book(
            venue="polymarket_us",
            market=market,
            bids=(Level(price=bid, size=50),),
            asks=(Level(price=ask, size=50),),
            as_of=T0 + timedelta(seconds=s),
        )

    return [book("mkt-a", 0, 0.40, 0.42), book("mkt-b", 1, 0.60, 0.62)]


def test_backtest_has_no_venue_times_unless_you_pass_settles_at() -> None:
    seen: list[Any] = []

    def strategy(client: Client, p: Any, quote: Any) -> None:
        seen.append(quote)
        if not client.positions():
            seen.append(client.trade(p, size=5, settles_at="2026-10-06"))

    bt = Client(mode="backtest", books=_books())
    bt.run(lambda c, p, q: seen.append(q), [PAIR])
    assert seen and all(q.return_per_day_pct is None and q.settles_at is None for q in seen)

    seen.clear()
    bt = Client(mode="backtest", books=_books())
    bt.run(strategy, [PAIR], settles_at="2026-10-06")
    q, t = seen[0], seen[1]
    # From the unrounded return, as profit() does: not return_pct (2 places) / days.
    assert q.days_held == 4.5 and q.return_per_day_pct == pytest.approx(q.return_pct / 4.5, abs=1e-3)
    assert t.status == "hedged" and t.quote.days_held == 4.5
    d = t.to_dict()["quote"]
    assert d["settles_at"] == "2026-10-06T00:00:00+00:00" and d["return_per_day_pct"] is not None
