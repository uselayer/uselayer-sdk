from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from uselayer import VenueError, rules_at
from uselayer.fees import dollars, js_round, polymarket_us_fee, round_to, to_micro
from uselayer.fill import FeeSettings, calculate_fee
from uselayer.venue_rules import polymarket_category_rate


def test_js_round_sends_halves_up_like_javascript() -> None:
    assert [js_round(x) for x in (0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 0.49999999999999994)] == [
        1,
        2,
        3,
        0,
        -1,
        -2,
        0,
    ]
    assert round_to(1.005 * 100, 0) == 100.0  # 100.49999999999999, as JavaScript sees it
    assert to_micro(0.42) == 420_000


def test_polymarket_us_ties_round_half_to_even() -> None:
    # The fee page's worked examples: $17.375 → $17.38, $3.125 → $3.12 (rebate).
    assert polymarket_us_fee(contracts=1000, price=0.5, coefficient=0.0695, role="taker") == 17_380_000
    assert polymarket_us_fee(contracts=1000, price=0.5, coefficient=0.0695, role="maker") == -3_120_000


def test_rules_are_looked_up_by_the_time_of_the_trade() -> None:
    before = datetime(2026, 7, 9, 23, 59, tzinfo=UTC)
    after = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    assert polymarket_category_rate("sports", before) == 0.03
    assert polymarket_category_rate("sports", after) == 0.05
    s = FeeSettings(venue="polymarket", category="sports")
    assert dollars(calculate_fee(s, contracts=100, price=0.5, role="taker", at=before)) == 0.75
    assert dollars(calculate_fee(s, contracts=100, price=0.5, role="taker", at=after)) == 1.25


def test_unknown_periods_raise_instead_of_guessing() -> None:
    with pytest.raises(VenueError) as e:
        rules_at("polymarket_us", datetime(2025, 11, 2, tzinfo=UTC))
    assert e.value.code == "no_venue_rules" and e.value.next
    with pytest.raises(VenueError) as e2:
        polymarket_category_rate("crypto", datetime(2026, 5, 1, tzinfo=UTC))
    assert e2.value.code == "no_venue_rules"
    with pytest.raises(ValueError):
        rules_at("polymarket_us", datetime(2026, 10, 1))


def test_polymarket_us_schedule_starts_at_midnight_eastern() -> None:
    edt = timezone(timedelta(hours=-4))
    assert (
        rules_at("polymarket_us", datetime(2026, 9, 17, 0, 0, tzinfo=edt)).effective_from.date().isoformat()
        == "2026-09-17"
    )
    assert (
        rules_at("polymarket_us", datetime(2026, 9, 16, 23, 59, tzinfo=edt)).effective_from.date().isoformat()
        == "2026-07-01"
    )


def test_every_entry_names_its_source() -> None:
    from uselayer.venue_rules import ENTRIES

    for e in ENTRIES:
        assert e.source.startswith("https://") and e.effective_from.tzinfo is not None
        assert e.to_dict()["venue"] == e.venue
