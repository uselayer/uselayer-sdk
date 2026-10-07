"""What leaves the machine: Layer gets only the key, Layer's own market ids and filters; nothing else gets anything."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeVenue

from uselayer.layer_api import ALLOWED

VENUE_KEY_MARKERS = ("PRIVATE KEY", "secret", "ACCESS-KEY", "ACCESS-SIGNATURE", "X-PM-")


def test_layer_only_ever_gets_gets_with_allowed_params(make_client: Any, venue: FakeVenue) -> None:
    venue.layer_answers["/v0/matches"] = {
        "matches": [
            {
                "event_date": "2026-10-04",
                "kalshi": {"market_id": "K1", "venue": "kalshi"},
                "polymarket_us": {"market_id": "mkt-a", "venue": "polymarket_us"},
            }
        ]
    }
    c = make_client(layer_key="lyr_test", rules={"max_position": {"per_market": 100}})
    m = c.matches(q="chiefs", venue="polymarket_us", limit=5)[0]
    assert m.polymarket_us.market_id == "mkt-a"
    c.match("mkt-a", venue="polymarket_us")
    # A full paper session afterwards: book, preview, send, positions, kill.
    o = c.order(venue="polymarket_us", market=m.polymarket_us, side="yes", price=0.42, size=5)
    c.preview(o)
    c.send(o)
    c.positions()
    c.kill(flatten=True)

    layer = [r for r in venue.requests if r.url.host == "uselayer.sh"]
    assert len(layer) == 2
    for r in layer:
        assert r.method == "GET" and r.content == b""
        assert r.url.path in ALLOWED and set(r.url.params.keys()) <= ALLOWED[r.url.path]
        assert r.headers["authorization"] == "Bearer lyr_test"
        sent = json.dumps(dict(r.url.params)) + str(r.url)
        for word in ("price", "size", "order", "0.42", *VENUE_KEY_MARKERS):
            assert word not in sent

    # No telemetry: every request goes to Layer or to the venue's public market data, all GETs.
    assert {r.url.host for r in venue.requests} <= {"uselayer.sh", "gateway.polymarket.us"}
    assert all(r.method == "GET" for r in venue.requests)


def test_the_layer_client_refuses_anything_else(make_client: Any) -> None:
    c = make_client(layer_key="lyr_test")
    with pytest.raises(AssertionError):
        c._layer._get("/v0/profit", {})
    with pytest.raises(AssertionError):
        c._layer._get("/v0/matches", {"price": 0.42})
    with pytest.raises(AssertionError):
        c._layer._get("/v0/matches", {"q": 0.42})


def test_matches_without_a_key_says_what_to_do(make_client: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("LAYER_API_KEY", raising=False)
    from uselayer import VenueError

    with pytest.raises(VenueError) as e:
        make_client().matches()
    assert e.value.code == "auth_failed" and "layer_key" in (e.value.next or "")
