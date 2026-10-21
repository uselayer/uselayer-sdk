"""match()/matches() fill each market's event, question, outcome and times from its venue (0.3.0).

Layer answers with ids, urls, confidence and rule flags only. Every answer here is made up.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from uselayer import Client
from uselayer.errors import VenueError
from uselayer.layer_api import Match

KALSHI_EVENT = "KXFAKEGAME-30JAN01AAABBB"
US_EVENT = "fake-aaa-bbb-2030-01-01"

LAYER_MATCH = {
    "kalshi": {
        "venue": "kalshi",
        "market_id": f"{KALSHI_EVENT}-AAA",
        "group_id": KALSHI_EVENT,
        "url": "https://kalshi.com/markets/kxfakegame/x/kxfakegame-30jan01aaabbb",
        "series": "KXFAKEGAME",
    },
    "polymarket_us": {
        "venue": "polymarket_us",
        "market_id": "fake-aaa-bbb-2030-01-01-aaa",
        "group_id": US_EVENT,
        "url": f"https://polymarket.us/event/{US_EVENT}",
        "slug": "fake-aaa-bbb-2030-01-01-aaa",
    },
    "category": "sports",
    "tier": "human_verified",
    "confidence": 0.98,
    "basis": "equivalent_with_caveats",
    "caveats": ["timing_differs"],
    "caveat_notes": {"timing_differs": "Kalshi counts the game by its Eastern date; Polymarket US by UTC."},
}

KALSHI_ANSWER = {
    "event": {
        "event_ticker": KALSHI_EVENT,
        "title": "Aaa vs Bbb",
        "sub_title": "AAA vs BBB (Jan 1)",
        "markets": [
            {
                "ticker": f"{KALSHI_EVENT}-AAA",
                "title": "Aaa wins",
                "yes_sub_title": "Aaa",
                "close_time": "2030-01-15T00:00:00Z",
                "occurrence_datetime": "2030-01-01T20:00:00Z",
            }
        ],
    }
}

US_ANSWER = {
    "events": [
        {
            "slug": US_EVENT,
            "title": "Team Aaa vs. Team Bbb",
            "startDate": "2030-01-01T20:00:00Z",
            "markets": [
                {
                    "slug": "fake-aaa-bbb-2030-01-01-aaa",
                    "question": "Will Team Aaa beat Team Bbb on Jan 1, 2030?",
                    "title": "Team Aaa",
                    "endDate": "2030-01-08T00:00:00Z",
                    "gameStartTime": "2030-01-01T20:00:00Z",
                }
            ],
        }
    ]
}


class Fake:
    def __init__(self, *, kalshi_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.kalshi_status = kalshi_status

    def handler(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        if r.url.host == "uselayer.sh" and r.url.path == "/v0/matches":
            return httpx.Response(200, json={"count": 1, "matches": [LAYER_MATCH]})
        if r.url.host == "uselayer.sh" and r.url.path == "/v0/match":
            return httpx.Response(
                200,
                json={
                    "source_market": LAYER_MATCH["kalshi"],
                    "matched_market": LAYER_MATCH["polymarket_us"],
                    "side": "same",
                    "tier": "human_verified",
                    "confidence": 0.98,
                    "basis": "identical",
                    "caveats": [],
                    "match_reason": "approved by a reviewer",
                },
            )
        if r.url.host == "api.elections.kalshi.com" and r.url.path == f"/trade-api/v2/events/{KALSHI_EVENT}":
            assert r.url.params["with_nested_markets"] == "true"
            assert "KALSHI-ACCESS-KEY" not in r.headers, "public market data: no key sent"
            if self.kalshi_status != 200:
                return httpx.Response(self.kalshi_status, json={"error": "down"})
            return httpx.Response(200, json=KALSHI_ANSWER)
        if r.url.host == "gateway.polymarket.us" and r.url.path == "/v1/events":
            assert r.url.params["slug"] == US_EVENT
            return httpx.Response(200, json=US_ANSWER)
        return httpx.Response(404, json={"message": "unknown"})


def client(fake: Fake, tmp_path: Any) -> Client:
    return Client(
        layer_key="lyr_test",
        transport=httpx.MockTransport(fake.handler),
        store=str(tmp_path / "s.db"),
        sleep=lambda _: None,
    )


def test_matches_fill_titles_from_both_venues(tmp_path: Any) -> None:
    fake = Fake()
    c = client(fake, tmp_path)
    m = c.matches(venue="polymarket_us")[0]
    assert m.kalshi.event == "Aaa vs Bbb — AAA vs BBB (Jan 1)"
    assert (m.kalshi.question, m.kalshi.outcome) == ("Aaa wins", "Aaa")
    assert (m.kalshi.event_time, m.kalshi.close_time) == ("2030-01-01T20:00:00Z", "2030-01-15T00:00:00Z")
    assert m.polymarket_us.event == "Team Aaa vs. Team Bbb"
    assert m.polymarket_us.outcome == "Team Aaa"
    assert m.polymarket_us.question == "Will Team Aaa beat Team Bbb on Jan 1, 2030?"
    assert m.polymarket_us.close_time == "2030-01-08T00:00:00Z"
    # Layer's own fields come through untouched.
    assert (m.confidence, m.caveats, m.kalshi.series, m.kalshi.url) == (
        0.98,
        ["timing_differs"],
        "KXFAKEGAME",
        LAYER_MATCH["kalshi"]["url"],
    )
    assert m.caveat_notes == LAYER_MATCH["caveat_notes"]
    assert set(m.markets()) == {"kalshi", "polymarket_us"}
    c.close()


def test_match_without_caveat_notes_has_none() -> None:
    # Layer before caveat_notes, or a match it has no sentence for.
    row = {k: v for k, v in LAYER_MATCH.items() if k != "caveat_notes"}
    assert Match.model_validate(row).caveat_notes == {}


def test_match_fills_both_sides_and_reads_each_event_once(tmp_path: Any) -> None:
    fake = Fake()
    c = client(fake, tmp_path)
    r = c.match(f"{KALSHI_EVENT}-AAA", venue="kalshi", with_="polymarket_us")
    assert r["source_market"]["outcome"] == "Aaa" and r["matched_market"]["outcome"] == "Team Aaa"
    assert r["matched_market"]["market_id"] == "fake-aaa-bbb-2030-01-01-aaa"
    c.match(f"{KALSHI_EVENT}-AAA", venue="kalshi", with_="polymarket_us")
    venue_calls = [x for x in fake.requests if x.url.host != "uselayer.sh"]
    assert len(venue_calls) == 2, "one call per event per venue, then cached"
    c.close()


def test_titles_false_makes_no_venue_calls(tmp_path: Any) -> None:
    fake = Fake()
    c = client(fake, tmp_path)
    m = c.matches(titles=False)[0]
    assert m.kalshi.outcome is None and m.kalshi.market_id == f"{KALSHI_EVENT}-AAA"
    assert {x.url.host for x in fake.requests} == {"uselayer.sh"}
    c.close()


def test_a_venue_that_is_down_leaves_its_fields_empty(tmp_path: Any) -> None:
    fake = Fake(kalshi_status=503)
    c = client(fake, tmp_path)
    m = c.matches()[0]
    assert m.kalshi.outcome is None and m.kalshi.market_id == f"{KALSHI_EVENT}-AAA"
    assert m.polymarket_us.outcome == "Team Aaa"
    c.close()


def test_layer_gets_nothing_from_the_venues(tmp_path: Any) -> None:
    fake = Fake()
    c = client(fake, tmp_path)
    c.matches(q="aaa")
    c.match(f"{KALSHI_EVENT}-AAA", venue="kalshi")
    for r in fake.requests:
        if r.url.host == "uselayer.sh":
            sent = json.dumps(dict(r.url.params))
            assert r.method == "GET" and r.content == b""
            assert "Aaa wins" not in sent and "Team Aaa" not in sent
    c.close()


def test_sandbox_mode_is_gone(tmp_path: Any) -> None:
    with pytest.raises(VenueError) as e:
        Client(mode="sandbox", layer_key="lyr_test", store=str(tmp_path / "s.db"))  # type: ignore[arg-type]
    assert e.value.code == "not_available" and e.value.next == "Client(mode='paper')"


def test_layer_market_type_groups_and_two_sided_markets(tmp_path: Any) -> None:
    from uselayer.http import Http
    from uselayer.titles import Titles

    answer = {
        "events": [
            {
                "slug": US_EVENT,
                "title": "Team Aaa vs. Team Bbb",
                "markets": [
                    {
                        "slug": "fake-spread",
                        "question": "Spread: Team Aaa (-1.5)",
                        "marketSides": [
                            {"long": True, "team": {"name": "Team Aaa"}},
                            {"long": False, "team": {"name": "Team Bbb"}},
                        ],
                    }
                ],
            }
        ]
    }
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return httpx.Response(200, json=answer)

    t = Titles(Http(transport=httpx.MockTransport(handler), sleep=lambda _: None))
    m = t.fill(
        {"venue": "polymarket_us", "market_id": "fake-spread:short", "group_id": f"{US_EVENT}#spreads"}
    )
    assert m["event"] == "Team Aaa vs. Team Bbb — spreads"  # Layer's per-market-type group, by its event slug
    assert (m["outcome"], m["question"]) == ("Team Bbb", "Spread: Team Aaa (-1.5) — Team Bbb")
    assert seen == [f"https://gateway.polymarket.us/v1/events?slug={US_EVENT}"]


# Hyperliquid (Layer pairs its sports games with Kalshi from 10-21): every market's text is its
# template filled with its fields, read once per client from POST /info.
HL_META = {
    "outcomes": [
        {
            "outcome": 1496,
            "name": "template:sportsContestWinner",
            "description": "competition:NBA|participantA:Aaa City|participantB:Bbb Town|resolutionDeadline:20300102-2300"
            "|scheduledStart:20300101-2300|shortNameA:AAA|shortNameB:BBB|stage:Regular Season",
            "sideSpecs": [{"name": "template:{shortNameA}"}, {"name": "template:{shortNameB}"}],
        },
        {
            "outcome": 1500,
            "name": "template:sportsSpread",
            "description": "participantA:Aaa City|participantB:Bbb Town|scheduledStart:20300101-2300|shortNameA:AAA|spread:+2.5",
            "sideSpecs": [{"name": "template:Yes"}, {"name": "template:No"}],
        },
    ],
    "questions": [],
}
HL_TEMPLATES = [
    {
        "id": "sportsContestWinner",
        "name": "{competition} {stage}: {participantA} v {participantB}",
        "description": "…",
    },
    {
        "id": "sportsSpread",
        "name": "{participantA} v {participantB}: {shortNameA} {spread}",
        "description": "…",
    },
]


def test_hyperliquid_markets_get_their_text_from_one_read(tmp_path: Any) -> None:
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "uselayer.sh":
            row = {
                **{k: v for k, v in LAYER_MATCH.items() if k != "polymarket_us"},
                "hyperliquid": {
                    "venue": "hyperliquid",
                    "market_id": "#14961",
                    "group_id": "o1496",
                    "url": "https://app.hyperliquid.xyz/outcomes",
                    "series": "nba",
                },
            }
            spread = {
                **row,
                "hyperliquid": {
                    "venue": "hyperliquid",
                    "market_id": "#15001",
                    "group_id": "aaa-city-bbb-town-20300101-2300#spreads",
                },
            }
            return httpx.Response(200, json={"count": 2, "matches": [row, spread]})
        if r.url.host == "api.hyperliquid.xyz":
            kind = json.loads(r.content)["type"]
            calls.append(kind)
            return httpx.Response(200, json=HL_META if kind == "outcomeMeta" else HL_TEMPLATES)
        if r.url.host == "api.elections.kalshi.com":
            return httpx.Response(200, json=KALSHI_ANSWER)
        return httpx.Response(404, json={})

    c = Client(
        layer_key="lyr_test",
        transport=httpx.MockTransport(handler),
        store=str(tmp_path / "s.db"),
        sleep=lambda _: None,
    )
    winner, spread = c.matches(venue="hyperliquid")
    assert winner.hyperliquid.event == "NBA Regular Season: Aaa City v Bbb Town"
    assert (winner.hyperliquid.outcome, winner.hyperliquid.question) == (
        "Bbb Town",
        "NBA Regular Season: Aaa City v Bbb Town: Bbb Town wins",
    )
    assert (winner.hyperliquid.event_time, winner.hyperliquid.close_time) == (
        "2030-01-01T23:00:00Z",
        "2030-01-02T23:00:00Z",
    )
    # Side 1 of a "+2.5" spread is the other team at -2.5; the group's kind goes on its title, as Layer writes it.
    assert (spread.hyperliquid.outcome, spread.hyperliquid.event) == (
        "Bbb Town -2.5",
        "Aaa City v Bbb Town — spreads",
    )
    assert winner.hyperliquid.series == "nba" and set(winner.markets()) == {"kalshi", "hyperliquid"}
    assert calls == ["outcomeMeta", "outcomeTemplates"], "one read for every Hyperliquid market"
    c.close()


def test_hyperliquid_twin_is_never_looked_up_on_a_polymarket(tmp_path: Any) -> None:
    c = client(Fake(), tmp_path)
    with pytest.raises(VenueError) as e:
        c.match("#14960", venue="hyperliquid", with_="polymarket_us")
    assert e.value.code == "invalid_order" and "Hyperliquid" in e.value.message
    c.close()
