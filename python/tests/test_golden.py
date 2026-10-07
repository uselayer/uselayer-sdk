"""The fee port, profit() and size() must match Layer's own answers in fee-golden.json exactly."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from uselayer import calc
from uselayer.fees import kalshi_fee, polymarket_fee, polymarket_us_fee
from uselayer.venue_rules import rules_at

GOLDEN = json.loads((Path(__file__).resolve().parents[2] / "fee-golden.json").read_text())
NOW = datetime.fromisoformat(GOLDEN["now"].replace("Z", "+00:00"))


def _fee(c: dict[str, Any]) -> int:
    if c["venue"] == "kalshi":
        return kalshi_fee(
            contracts=c["contracts"], price=c["price"], rate=c["rate"], multiplier=c["multiplier"]
        )
    if c["venue"] == "polymarket":
        return polymarket_fee(
            contracts=c["contracts"], price=c["price"], rate=c["rate"], exponent=c["exponent"], role=c["role"]
        )
    return polymarket_us_fee(
        contracts=c["contracts"], price=c["price"], coefficient=c["coefficient"], role=c["role"]
    )


def test_every_fee_case() -> None:
    bad = [(c, _fee(c)) for c in GOLDEN["fees"] if _fee(c) != int(c["fee_micro"])]
    assert not bad, f"{len(bad)} of {len(GOLDEN['fees'])} differ, first: {bad[0]}"


@pytest.mark.parametrize("i", range(len(GOLDEN["profit"])))
def test_profit(i: int) -> None:
    case = GOLDEN["profit"][i]
    assert calc.profit(case["request"], NOW) == case["response"]


@pytest.mark.parametrize("i", range(len(GOLDEN["size"])))
def test_size(i: int) -> None:
    case = GOLDEN["size"][i]
    assert calc.size(case["request"], NOW) == case["response"]


def test_todays_rules_are_the_golden_schedules() -> None:
    # The golden file names the schedules it was made from; the SDK's entry in force then must be that
    # one. It can start earlier than Layer's label: Polymarket US's 0.0695 is dated 2026-09-17 from its
    # CFTC filing, while Layer's lib/fees.ts still says 2026-09-25 (the docs banner).
    for venue, s in GOLDEN["fee_schedules"].items():
        labelled = datetime.fromisoformat(s["effective"] + "T12:00:00+00:00")
        assert rules_at(venue, NOW) == rules_at(venue, labelled), venue
        assert rules_at(venue, NOW).effective_from <= labelled, venue


def test_golden_is_not_older_than_the_sdk_schedules() -> None:
    # A newer schedule in the SDK than the golden file was made from means the golden copy is stale.
    for venue, s in GOLDEN["fee_schedules"].items():
        latest = max(
            e.effective_from.date().isoformat()
            for e in __import__("uselayer.venue_rules").venue_rules.history(venue)
        )
        assert latest <= s["effective"], f"{venue}: SDK has a schedule from {latest}; refresh fee-golden.json"


def _leg_cases() -> list[tuple[int, str]]:
    out = []
    for i, case in enumerate(GOLDEN["size"]):
        if case["response"]["contracts"] > 0:
            out.extend((i, v) for v in ("kalshi", "polymarket", "polymarket_us") if v in case["response"])
    return out


@pytest.mark.parametrize(("i", "venue"), _leg_cases())
def test_fill_model_matches_size_fills(i: int, venue: str) -> None:
    """Layer's fill model (estimate_fill) bills each level exactly like /v0/size."""
    from uselayer.books import Book, Level
    from uselayer.fill import FeeSettings, estimate_fill
    from uselayer.orders import Order

    req, resp = GOLDEN["size"][i]["request"], GOLDEN["size"][i]["response"]
    leg = resp[venue]
    asks = [Level(price=a["price"], size=a["size"]) for a in req[venue]["asks"]]
    book = Book(venue=venue, market="m", bids=(), asks=tuple(asks), as_of=NOW)
    if venue == "kalshi":
        settings = FeeSettings(venue=venue, multiplier=req["kalshi"].get("fee_multiplier", 1))
    elif venue == "polymarket_us":
        settings = FeeSettings(venue=venue, coefficient=leg["fee_coefficient"])
    else:
        settings = FeeSettings(venue=venue, rate=leg["fee_rate"], exponent=leg["exponent"])
    top = max(a["price"] for a in req[venue]["asks"])
    order = Order(venue="polymarket_us", market="m", side="yes", price=top, size=resp["contracts"])
    est = estimate_fill(order, book, settings, at=NOW)
    got = [{"price": f.price, "contracts": f.contracts, "cost": f.cost, "fee": f.fee} for f in est.fills]
    assert got == leg["fills"]
    assert est.cost == leg["cost"] and est.fees == leg["fee"]
