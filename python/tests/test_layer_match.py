"""``client.match``'s ``with_``: a Kalshi market's choice of Polymarket, never one Polymarket to the other."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeVenue

from uselayer import VenueError


def _layer_calls(venue: FakeVenue) -> list[dict[str, str]]:
    return [dict(r.url.params) for r in venue.requests if r.url.host == "uselayer.sh"]


@pytest.mark.parametrize(
    ("source", "other"), [("polymarket", "polymarket_us"), ("polymarket_us", "polymarket")]
)
def test_the_other_polymarket_is_refused_before_anything_is_sent(
    make_client: Any, venue: FakeVenue, source: str, other: str
) -> None:
    c = make_client(layer_key="lyr_test")
    with pytest.raises(VenueError) as e:
        c.match("0xabc:0", venue=source, with_=other)
    err = e.value
    assert err.code == "invalid_order" and err.venue == "layer" and not err.retryable
    assert err.message == (
        f"with_={other!r} doesn't apply to a {source} market: Layer pairs each Polymarket with Kalshi, "
        "not with each other."
    )
    assert err.hint is not None and "two steps: look up its Kalshi match" in err.hint
    assert f"with_={other!r}" in err.hint
    assert _layer_calls(venue) == []


def test_with_still_picks_the_polymarket_for_a_kalshi_market(make_client: Any, venue: FakeVenue) -> None:
    c = make_client(layer_key="lyr_test")
    c.match("KXFEDDECISION-26OCT-C25", venue="kalshi", with_="polymarket", titles=False)
    c.match("KXFEDDECISION-26OCT-C25", venue="kalshi", titles=False)
    assert _layer_calls(venue) == [
        {"venue": "kalshi", "market_id": "KXFEDDECISION-26OCT-C25", "with": "polymarket"},
        {"venue": "kalshi", "market_id": "KXFEDDECISION-26OCT-C25"},
    ]


def test_a_polymarket_markets_own_venue_is_passed_through(make_client: Any, venue: FakeVenue) -> None:
    c = make_client(layer_key="lyr_test")
    c.match("0xabc:0", venue="polymarket", with_="polymarket", titles=False)
    assert _layer_calls(venue) == [{"venue": "polymarket", "market_id": "0xabc:0", "with": "polymarket"}]
