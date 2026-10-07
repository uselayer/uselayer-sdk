"""Quote's spread before fees: gross_spread − fees == net_profit, whichever way round, sized or not.

The default books (conftest): YES on mkt-a asks 0.42 × 10, 0.43 × 20, 0.45 × 100; NO on mkt-b asks
0.40 × 30 (its YES bids 0.60 × 30).
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeMarket, FakeVenue

from uselayer.books import Level
from uselayer.calc import pair_size
from uselayer.fees import round_to
from uselayer.fill import FeeSettings
from uselayer.trading import Quote, _leg_book

PAIR = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]


def holds(q: Quote | dict[str, Any]) -> None:
    d = q if isinstance(q, dict) else q.to_dict()
    assert round(d["gross_spread"] - d["fees"], 6) == d["net_profit"]
    assert round(d["contracts"] - d["cost"], 6) == d["gross_spread"]
    n = d["contracts"]
    assert d["gross_spread_per_contract"] == (round_to(d["gross_spread"] / n, 6) if n else 0)


def test_yes_on_a_no_on_b(make_client: Any) -> None:
    q = make_client().quote(PAIR, size=10)
    assert (q.a.side, q.b.side) == ("yes", "no")  # type: ignore[union-attr]
    assert (q.gross_spread, q.fees, q.net_profit) == (1.80, 0.34, 1.46)  # 10 − 4.20 − 4.00, then − 0.34
    assert q.gross_spread_per_contract == 0.18 and q.gross_at_best == 0.18
    assert q.gross_at_best > q.edge_at_best  # type: ignore[operator]
    holds(q)


def test_no_on_a_yes_on_b(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("mkt-a", bids=[(0.61, 25)], asks=[(0.64, 25)]))  # NO on a asks 0.39
    venue.add(FakeMarket("mkt-b", bids=[(0.30, 25)], asks=[(0.41, 15), (0.44, 40)]))
    q = make_client().quote(PAIR, size=20)
    assert (q.a.side, q.b.side) == ("no", "yes")  # type: ignore[union-attr]
    assert q.gross_at_best == 0.20  # 1 − 0.39 − 0.41
    assert q.contracts == 20 and q.gross_spread == 3.85  # 20 − 7.80 − (15 × 0.41 + 5 × 0.44)
    holds(q)


def test_walk_across_levels(make_client: Any) -> None:
    q = make_client().quote(PAIR)
    assert q.contracts == 30 and q.gross_spread_per_contract < q.gross_at_best  # type: ignore[operator]
    holds(q)


def test_nothing_clears_still_shows_the_raw_gap(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("mkt-a", bids=[(0.48, 50)], asks=[(0.49, 50)]))
    venue.add(FakeMarket("mkt-b", bids=[(0.50, 50)], asks=[(0.53, 50)]))  # NO on b asks 0.50
    q = make_client().quote(PAIR)
    assert q.contracts == 0 and q.limited_by == "min_edge"
    assert q.gross_at_best == 0.01 and q.edge_at_best is not None and q.edge_at_best < 0
    assert (q.gross_spread, q.gross_spread_per_contract, q.fees, q.net_profit) == (0, 0, 0, 0)
    holds(q)


def test_min_edge_above_the_gap(make_client: Any) -> None:
    q = make_client().quote(PAIR, min_edge=0.5)
    assert q.contracts == 0 and q.gross_at_best == 0.18
    holds(q)


def test_one_sided_books_price_the_only_way_round(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("mkt-a", bids=[], asks=[(0.42, 10)]))  # no bids: no NO asks on a
    q = make_client().quote(PAIR, size=10)
    assert (q.a.side, q.b.side) == ("yes", "no")  # type: ignore[union-attr]
    assert q.gross_spread == 1.80
    holds(q)


def test_no_asks_either_way(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("mkt-a", bids=[], asks=[]))
    q = make_client().quote(PAIR)
    assert q.limited_by == "no_asks" and q.gross_at_best is None and q.edge_at_best is None
    assert (q.gross_spread, q.gross_spread_per_contract) == (0.0, 0.0)
    holds(q)


def test_serialised_with_the_net_fields(make_client: Any) -> None:
    c = make_client()
    q = c.quote(PAIR, size=10)
    d = json.loads(json.dumps(q.to_dict()))
    assert (d["gross_spread"], d["gross_spread_per_contract"], d["gross_at_best"]) == (1.80, 0.18, 0.18)
    assert "gross_spread=1.8" in repr(q) and "gross_at_best=0.18" in repr(q)
    t = c.trade(PAIR, size=10)
    assert t.to_dict()["quote"]["gross_spread"] == 1.80


def test_backtest_strategy_sees_the_gross_fields(make_client: Any) -> None:
    seen: list[tuple[float, float, float]] = []

    def strategy(client: Any, pair: Any, quote: Quote) -> None:
        seen.append((quote.gross_spread, quote.fees, quote.net_profit))
        holds(quote)

    make_client().run(strategy, [PAIR], iterations=1, size=10)
    assert seen == [(1.80, 0.34, 1.46)]


FEES = [
    FeeSettings(venue="polymarket_us"),
    FeeSettings(venue="polymarket_us", coefficient=0.02),
    FeeSettings(venue="kalshi"),
    FeeSettings(venue="kalshi", fee_type="quadratic_with_maker_fees", multiplier=2.0),
    FeeSettings(venue="kalshi", fee_type="quadratic_with_maker_fees", multiplier=0.5),
    FeeSettings(venue="kalshi", fee_type="quadratic_with_combo_maker_fees"),
    FeeSettings(venue="polymarket", category="sports"),
]


def _asks(rng: random.Random) -> Any:
    start = rng.randint(5, 70)
    prices = sorted({start + rng.randint(0, 20) for _ in range(rng.randint(1, 5))})
    sizes = [0.5, 1, 3, 7.25, 40, 250]
    return SimpleNamespace(asks=[Level(price=p / 100, size=rng.choice(sizes)) for p in prices])


@pytest.mark.parametrize("fa", FEES, ids=lambda f: f"{f.venue}-{f.fee_type}-{f.multiplier}")
@pytest.mark.parametrize("fb", FEES[:3], ids=lambda f: f"{f.venue}-{f.coefficient}")
def test_invariant_across_fee_types_and_random_books(fa: FeeSettings, fb: FeeSettings) -> None:
    at = datetime(2026, 10, 1, tzinfo=UTC)
    rng = random.Random(f"{fa}{fb}")
    for _ in range(60):
        a, b = _leg_book("a", _asks(rng), fa, at), _leg_book("b", _asks(rng), fb, at)
        r = pair_size(a, b, min_edge=rng.choice([0, 0, 0.01, 0.05]), max_contracts=rng.choice([1, 17, 10**6]))
        holds(r)
        assert r["gross_at_best"] == round(1 - a.levels[0]["price"] - b.levels[0]["price"], 6)
        assert r["gross_at_best"] >= r["edge_at_best"] - 1e-9


def test_new_fields_are_appended_so_positional_construction_still_works(make_client: Any) -> None:
    q = make_client().quote(PAIR, size=10)
    old = Quote(q.a, q.b, 10, 0.0, q.edge_at_best, 1.46, 0.146, 0.34, 8.2, 17.8, "max_contracts", q.as_of)
    assert (old.gross_at_best, old.gross_spread, old.gross_spread_per_contract) == (None, 0.0, 0.0)
    assert (old.settles_at, old.days_held, old.return_per_day_pct) == (None, None, None)
    assert list(q.to_dict())[-6:] == [
        "gross_at_best",
        "gross_spread",
        "gross_spread_per_contract",
        "settles_at",
        "days_held",
        "return_per_day_pct",
    ]
    assert list(q.to_dict()) == list(q.__dict__)
