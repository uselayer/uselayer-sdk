"""Polymarket US's fee history and Kalshi's 2025-10-01 schedule, so replays before 2026-09-25 price.

- Each era boundary: the day before prices with the old schedule, the day of with the new one.
- The premium era's fee (rate × C × p) rounds to that era's step ($0.0001, $0.001, then the cent),
  half to even, in integer math, with its minimum fee.
- Today's prices are unchanged.
- quote() in backtest mode prices a January and an August 2026 replay with that day's fees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from uselayer import Book, Client, Level, VenueError, calc, rules_at
from uselayer.fees import dollars, polymarket_us_premium_fee
from uselayer.fill import FeeSettings, calculate_fee, fee_per_contract
from uselayer.venue_rules import ENTRIES, PolymarketUSFees, PolymarketUSPremiumFees, history

EST = timezone(timedelta(hours=-5))
EDT = timezone(timedelta(hours=-4))
US = FeeSettings(venue="polymarket_us")


def us_fee(at: datetime, contracts: float = 100, price: float = 0.55, role: Any = "taker") -> float:
    return dollars(calculate_fee(US, contracts=contracts, price=price, role=role, at=at))


# 100 contracts at 0.55 (a $55 premium), from the moment each schedule starts and just before it:
# (start, taker from, taker before, maker from, maker before). Takers: 1 bp $0.0055; 10 bp $0.055;
# 30 bp $0.165 → $0.16 (a tie, to even); Θ 0.05 $1.2375 → $1.24; Θ 0.06 $1.485 → $1.48 (a tie);
# Θ 0.0695 $1.720125 → $1.72. Makers: nothing, then 10 bp back ($0.055), 20 bp ($0.11), then
# Θ 0.0125 ($0.309375 → $0.31).
BOUNDARIES = [
    (datetime(2026, 1, 9, tzinfo=EST), 0.055, 0.0055, 0.0, 0.0),
    (datetime(2026, 3, 3, tzinfo=EST), 0.055, 0.055, -0.055, 0.0),
    (datetime(2026, 3, 9, tzinfo=EDT), 0.16, 0.055, -0.11, -0.055),
    (datetime(2026, 4, 3, 15, 0, tzinfo=EDT), 1.24, 0.16, -0.31, -0.11),
    (datetime(2026, 7, 1, tzinfo=EDT), 1.48, 1.24, -0.31, -0.31),
    (datetime(2026, 9, 17, tzinfo=EDT), 1.72, 1.48, -0.31, -0.31),
]


@pytest.mark.parametrize(("starts", "taker_from", "taker_before", "maker_from", "maker_before"), BOUNDARIES)
def test_each_polymarket_us_era_starts_on_its_day(
    starts: datetime, taker_from: float, taker_before: float, maker_from: float, maker_before: float
) -> None:
    assert rules_at("polymarket_us", starts).effective_from == starts
    for at in (starts, starts + timedelta(hours=12)):
        assert (us_fee(at), us_fee(at, role="maker")) == (taker_from, maker_from)
    for at in (starts - timedelta(days=1), starts - timedelta(minutes=1)):
        assert (us_fee(at), us_fee(at, role="maker")) == (taker_before, maker_before)


def test_the_theta_era_starts_at_3pm_eastern_not_midnight() -> None:
    morning = datetime(2026, 4, 3, 9, 0, tzinfo=EDT)
    assert isinstance(rules_at("polymarket_us", morning).fees, PolymarketUSPremiumFees)
    assert us_fee(morning) == 0.16
    assert isinstance(
        rules_at("polymarket_us", datetime(2026, 4, 3, 19, 0, tzinfo=UTC)).fees, PolymarketUSFees
    )


def test_the_first_schedule_is_1bp_and_before_it_none() -> None:
    first = datetime(2025, 11, 3, tzinfo=EST)
    assert us_fee(first) == 0.0055 and us_fee(first, role="maker") == 0.0
    assert us_fee(datetime(2025, 12, 3, tzinfo=EST)) == 0.0055  # the public launch
    assert rules_at("polymarket_us", first).fees == PolymarketUSPremiumFees(
        taker_rate=0.0001, maker_rebate_rate=0.0, rounding=0.0001, minimum_fee=0.0001
    )
    for before in (first - timedelta(days=1), first - timedelta(minutes=1)):
        with pytest.raises(VenueError) as e:
            rules_at("polymarket_us", before)
        assert e.value.code == "no_venue_rules" and "2025-11-03" in str(e.value)


def test_kalshi_0_07_from_2025_10_01() -> None:
    start = datetime(2025, 10, 1, tzinfo=EDT)
    k = FeeSettings(venue="kalshi")
    for at in (
        start,
        start + timedelta(hours=12),
        datetime(2026, 7, 6, tzinfo=UTC),
        datetime(2026, 7, 8, tzinfo=UTC),
    ):
        assert dollars(calculate_fee(k, contracts=100, price=0.5, role="taker", at=at)) == 1.75
    maker = FeeSettings(venue="kalshi", fee_type="quadratic_with_maker_fees")
    assert dollars(calculate_fee(maker, contracts=100, price=0.5, role="maker", at=start)) == 0.44
    assert "quadratic_with_combo_maker_fees" not in rules_at("kalshi", start).fees.maker_rates  # type: ignore[union-attr]
    with pytest.raises(VenueError):
        rules_at("kalshi", start - timedelta(days=1))


def test_premium_fee_rounds_to_its_step_half_to_even_with_a_minimum() -> None:
    def fee(c: float, p: float, rate: float, step: float = 0.01, minimum: float = 0.0) -> int:
        return polymarket_us_premium_fee(contracts=c, price=p, rate=rate, increment=step, minimum=minimum)

    # To the cent (30 bp, from 2026-03-09).
    assert fee(100, 0.55, 0.003) == 160_000  # 16.5¢ → 16 (even)
    assert fee(100, 0.45, 0.003) == 140_000  # 13.5¢ → 14 (even)
    assert fee(33.333333, 0.3, 0.003) == 30_000  # 2.99999997¢ → 3; fractional contracts work
    assert fee(1, 0.5, 0.003) == 0  # 0.15¢ → 0: no minimum in that era
    # Ties that floats miss: 0.001 × 50 × 0.9 is 4.5¢ exactly, but 4.500000000000001 in floats (→ 5);
    # 0.001 × 50 × 0.7 is 3.5¢, but 3.4999999999999996 in floats (→ 3). The integer math sees the tie.
    assert 0.001 * 50 * 0.9 * 100 > 4.5 and 0.001 * 50 * 0.7 * 100 < 3.5
    assert fee(50, 0.9, 0.001) == 40_000 and fee(50, 0.7, 0.001) == 40_000
    # To $0.001 with a $0.001 minimum (10 bp, from 2026-01-09).
    assert fee(100, 0.55, 0.001, 0.001, 0.001) == 55_000
    assert fee(125, 0.5, 0.001, 0.001, 0.001) == 62_000  # $0.0625 → $0.062 (even)
    assert fee(175, 0.5, 0.001, 0.001, 0.001) == 88_000  # $0.0875 → $0.088 (even)
    assert fee(1, 0.10, 0.001, 0.001, 0.001) == 1_000  # $0.0001 → the $0.001 minimum
    # To $0.0001 with a $0.0001 minimum (1 bp, at launch).
    assert fee(100, 0.464, 0.0001, 0.0001, 0.0001) == 4_600  # $0.00464 → $0.0046
    assert fee(1, 0.5, 0.0001, 0.0001, 0.0001) == 100  # $0.00005 → the minimum
    # No premium, no fee: the minimum is for a trade.
    assert fee(0, 0.5, 0.001, 0.001, 0.001) == 0


def test_premium_era_per_contract_and_market_theta() -> None:
    jan = datetime(2026, 1, 20, tzinfo=UTC)
    assert fee_per_contract(US, 0.55, at=jan) == pytest.approx(0.001 * 0.55)
    # A market's own coefficient is a Θ: passed, it prices the taker Θ × C × p × (1 − p) in any era.
    theta = FeeSettings(venue="polymarket_us", coefficient=0.0695)
    assert dollars(calculate_fee(theta, contracts=100, price=0.5, role="taker", at=jan)) == 1.74
    assert fee_per_contract(theta, 0.5, at=jan) == pytest.approx(0.0695 * 0.25)
    # Makers are paid the era's premium rebate whatever the coefficient: nothing in January.
    assert dollars(calculate_fee(theta, contracts=100, price=0.5, role="maker", at=jan)) == 0.0


def test_todays_prices_are_unchanged() -> None:
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    r = rules_at("polymarket_us", now)
    assert r.fees == PolymarketUSFees(taker_coefficient=0.0695, maker_rebate=0.0125)
    assert us_fee(now, contracts=1000, price=0.5) == 17.38
    assert us_fee(now, contracts=1000, price=0.5, role="maker") == -3.12
    assert us_fee(now, contracts=100, price=0.5) == 1.74
    assert fee_per_contract(US, 0.5, at=now) == pytest.approx(0.0695 * 0.25)
    assert rules_at("kalshi", now).effective_from == datetime(2026, 7, 7, tzinfo=EDT)
    r1 = calc.profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}}, now)
    assert r1["fees"] == 3.43 and r1["polymarket_us"]["fee_coefficient"] == 0.0695  # $1.71 + $1.72


def test_profit_and_size_price_the_premium_era() -> None:
    jan = datetime(2026, 1, 20, tzinfo=UTC)
    r = calc.profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}}, jan)
    assert r["polymarket_us"]["fee"] == 0.055 and r["polymarket_us"]["fee_premium_rate"] == 0.001
    assert "fee_coefficient" not in r["polymarket_us"]
    explicit = calc.profit(
        {
            "contracts": 100,
            "kalshi": {"price": 0.42},
            "polymarket_us": {"price": 0.55, "fee_coefficient": 0.05},
        },
        jan,
    )
    assert explicit["polymarket_us"]["fee_coefficient"] == 0.05 and explicit["polymarket_us"]["fee"] == 1.24
    s = calc.size(
        {
            "kalshi": {"asks": [{"price": 0.42, "size": 100}]},
            "polymarket_us": {"asks": [{"price": 0.5, "size": 100}]},
        },
        jan,
    )
    assert s["contracts"] == 100 and s["polymarket_us"]["fee_premium_rate"] == 0.001
    # Polymarket (not US) has no schedule before 2026-03-30: asking for it still raises.
    with pytest.raises(VenueError) as e:
        calc.profit(
            {"contracts": 1, "kalshi": {"price": 0.4}, "polymarket": {"price": 0.5, "category": "sports"}},
            jan,
        )
    assert e.value.code == "no_venue_rules"


def _replay(day: datetime) -> list[Book]:
    """Kalshi YES at 0.42 and Polymarket US NO at 0.50 (its YES bid), two minutes running."""
    out = []
    for m in range(2):
        at = day + timedelta(minutes=m)
        out.append(
            Book(
                venue="kalshi",
                market="KXT-1",
                bids=(Level(price=0.40, size=500),),
                asks=(Level(price=0.42, size=500),),
                as_of=at,
            )
        )
        out.append(
            Book(
                venue="polymarket_us",
                market="us-1",
                bids=(Level(price=0.50, size=500),),
                asks=(Level(price=0.52, size=500),),
                as_of=at,
            )
        )
    return out


@pytest.mark.parametrize(
    ("day", "us_fee", "net"),
    [
        # 10 bp of the $50 premium = $0.05; Kalshi round up(0.07 × 100 × 0.42 × 0.58) = $1.71.
        (datetime(2026, 1, 15, 18, tzinfo=UTC), 0.05, 8.0 - 1.71 - 0.05),
        # Θ 0.06 × 100 × 0.5 × 0.5 = $1.50.
        (datetime(2026, 8, 15, 18, tzinfo=UTC), 1.50, 8.0 - 1.71 - 1.50),
    ],
)
def test_backtest_quote_uses_the_fees_of_the_replays_day(day: datetime, us_fee: float, net: float) -> None:
    pair = [("kalshi", "KXT-1"), ("polymarket_us", "us-1")]
    quotes: list[Any] = []
    bt = Client(mode="backtest", books=_replay(day))

    def on_book(c: Client, b: Book) -> None:
        if b.venue == "polymarket_us":  # both books are in
            quotes.append(c.quote(pair, size=100, settles_at=day + timedelta(days=2)))

    bt.replay(on_book)
    q = quotes[-1]
    assert (q.a.venue, q.a.side, q.b.venue, q.b.side) == ("kalshi", "yes", "polymarket_us", "no")
    assert q.contracts == 100 and q.b.fee == us_fee and q.a.fee == 1.71
    assert q.net_profit == pytest.approx(net)


def test_every_entry_has_a_source_note() -> None:
    for e in ENTRIES:
        assert e.notes, (e.venue, e.effective_from)
    us = history("polymarket_us")
    assert [type(e.fees).__name__ for e in us] == ["PolymarketUSPremiumFees"] * 4 + ["PolymarketUSFees"] * 3
