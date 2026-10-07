"""The fee lookup: client.fees() and client.profit() fill in what Layer's POST /v0/profit fills in for a match.

- Recorded: made-up Kalshi and Polymarket US answers for 4 made-up pairs (fixtures/fee_lookup, in the
  venues' answer formats), replayed, give the /v0/profit answers Layer gives for them (computed from those
  answers with calc.profit and Layer's payout-time rule); prices() on the same books gives the prices sent.
  No real venue data is committed: the venues' terms don't allow sharing it.
- Golden: every Polymarket US profit case in fee-golden.json, with its fee settings served by the venues
  instead of sent, gives Layer's answer.
- The rules: what the request sends wins, what's filled and when, and the errors.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_kalshi import FakeKalshi, KMarket, pem

from uselayer import Client, Kalshi, Match, VenueError, calc
from uselayer._recorded import ReplayTransport
from uselayer.fee_lookup import PAYOUT_BUFFER_MS, iso_ms, payout_times, time_ms
from uselayer.fill import FeeSettings
from uselayer.venues.base import MarketInfo

HERE = Path(__file__).resolve().parent
RECORDED = HERE / "fixtures" / "fee_lookup"
GOLDEN = json.loads((HERE.parents[1] / "fee-golden.json").read_text())
GOLDEN_NOW = datetime.fromisoformat(GOLDEN["now"].replace("Z", "+00:00"))
TIME_FIELDS = {"days_held": 0.01, "return_per_day_pct": 0.0001, "annualized_return_pct": 0.01}


# ---- fakes ----


class TimedKalshi(FakeKalshi):
    """The fake Kalshi, with each market's own event and close times."""

    times: dict[str, tuple[str | None, str | None]]

    def _market(self, m: KMarket) -> dict[str, Any]:
        d = super()._market(m)
        event, close = getattr(self, "times", {}).get(m.ticker, (None, "2026-12-31T00:00:00Z"))
        d["close_time"] = close
        if event is not None:
            d["occurrence_datetime"] = event
        return d


class TimedVenue(FakeVenue):
    """The fake Polymarket US gateway, with each market's gameStartTime and endDate."""

    times: dict[str, tuple[str | None, str | None]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        r = super().handler(request)
        if request.url.path == "/v1/markets" and r.status_code == 200:
            body = json.loads(r.content)
            for m in body["markets"]:
                game, end = getattr(self, "times", {}).get(m["slug"], (None, "2026-12-31T00:00:00Z"))
                m["endDate"] = end
                if game is not None:
                    m["gameStartTime"] = game
            return httpx.Response(200, json=body, headers=r.headers)
        return r


def pair(ticker: str = "KXEV-1-A", slug: str = "mkt-a") -> list[tuple[str, str]]:
    return [("kalshi", ticker), ("polymarket_us", slug)]


@pytest.fixture
def world(clock: Clock, tmp_path: Any) -> Any:
    priv = Ed25519PrivateKey.generate()
    k = TimedKalshi(priv.public_key(), clock)
    k.times = {}
    k.series["KXEV"] = {"ticker": "KXEV", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}
    k.add(KMarket("KXEV-1-A", yes_bids=[(0.40, 10)], no_bids=[(0.55, 7)]))
    v = TimedVenue(clock)
    v.times = {}
    v.add(FakeMarket("mkt-a", bids=[(0.40, 50)], asks=[(0.42, 10)], fee_coefficient=0.05))
    made: list[Client] = []

    def handler(r: httpx.Request) -> httpx.Response:
        return k.handle(r) if r.url.host == "api.elections.kalshi.com" else v.handler(r)

    def make(**kw: Any) -> Client:
        kw.setdefault("store", str(tmp_path / f"s{len(made)}.db"))
        kw.setdefault("clock", clock)
        c = Client(
            transport=httpx.MockTransport(handler),
            sleep=clock.sleep,
            kalshi=Kalshi(key_id="k", private_key_pem=pem(priv)),
            layer_key="lyr_test",
            **kw,
        )
        made.append(c)
        return c

    yield make, k, v
    for c in made:
        c.close()


BODY = {"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}}


# ---- recorded (made-up) venue answers ----


def _cases() -> list[dict[str, Any]]:
    return json.loads((RECORDED / "hosted.json").read_text())


def _replay_client(at: datetime, tmp_path: Path) -> Client:
    t = ReplayTransport(RECORDED / "venues.json")
    # A book read twice while recording was stale the first time; the price sent came from the re-read.
    t.answers = {k: v[-1:] for k, v in t.answers.items()}
    return Client(
        transport=t,
        clock=lambda: at,
        sleep=lambda s: None,
        store=str(tmp_path / "r.db"),
        kalshi=Kalshi(key_id="k", private_key_pem=pem(Ed25519PrivateKey.generate())),
    )


@pytest.mark.parametrize("i", range(len(_cases())))
def test_recorded_matches_answer_like_layer(i: int, tmp_path: Path) -> None:
    case = _cases()[i]
    at = datetime.fromisoformat(case["at"])
    hosted = case["response"]
    with _replay_client(at, tmp_path) as c:
        got = c.profit(case["body"], pair=[tuple(x) for x in case["pair"]])
    for k in set(hosted) | set(got):
        h, s = hosted.get(k), got.get(k)
        if k in TIME_FIELDS:
            # Layer's answer was computed a moment before or after this clock.
            assert abs(h - s) <= TIME_FIELDS[k] + 1e-9, k
        elif k == "match" and h != s:
            # The venue closed this market early: its own close time is before this answer. Layer's
            # stored times are from before the close.
            assert s["latest_payout_at"] < case["at"] and h["latest_payout_at"] > s["latest_payout_at"]
            assert {x: y for x, y in h.items() if "payout" not in x} == {
                x: y for x, y in s.items() if "payout" not in x
            }
        else:
            assert h == s, k


def test_recorded_cover_more_than_one_day_and_the_series_fee_type(tmp_path: Path) -> None:
    cases = _cases()
    assert len(cases) >= 3
    assert max(c["response"]["days_held"] for c in cases) > 30
    with _replay_client(datetime.fromisoformat(cases[1]["at"]), tmp_path) as c:
        assert c.fees(cases[1]["pair"])["kalshi"]["fee_type"] == "quadratic_with_maker_fees"
    # Layer fills kalshi.fee_type from the series too, and lists it right after the multiplier.
    assert cases[1]["response"]["kalshi"]["fee_type"] == "quadratic_with_maker_fees"
    assert all(c["response"]["filled_in"][:2] == ["kalshi.fee_multiplier", "kalshi.fee_type"] for c in cases)


@pytest.mark.parametrize("i", range(len(_cases())))
def test_prices_on_the_recorded_books_are_the_prices_sent(i: int, tmp_path: Path) -> None:
    case = _cases()[i]
    with _replay_client(datetime.fromisoformat(case["at"]), tmp_path) as c:
        p = c.prices([tuple(x) for x in case["pair"]])
    k, u = p.leg("kalshi"), p.leg("polymarket_us")
    assert (k.yes_ask or 0.42) == case["body"]["kalshi"]["price"]
    assert (u.no_ask or 0.55) == case["body"]["polymarket_us"]["price"]
    for leg in (k, u):
        assert leg.source == "venue"
        for bid, ask in ((leg.yes_bid, leg.yes_ask), (leg.no_bid, leg.no_ask)):
            assert bid is None or ask is None or bid <= ask


# ---- fee-golden.json ----


GOLDEN_US = [c for c in GOLDEN["profit"] if "polymarket_us" in c["request"]]


@pytest.mark.parametrize("i", range(len(GOLDEN_US)))
def test_golden_cases_with_the_fees_served_by_the_venues(i: int, tmp_path: Path) -> None:
    case = GOLDEN_US[i]
    req = json.loads(json.dumps(case["request"]))
    k, u = req["kalshi"], req["polymarket_us"]
    clock = Clock(GOLDEN_NOW)
    priv = Ed25519PrivateKey.generate()
    kal = TimedKalshi(priv.public_key(), clock)
    kal.times = {"KXG-1-A": (None, None)}  # no times: days_held comes from the request or not at all
    kal.series = {
        "KXG": {
            "ticker": "KXG",
            "fee_type": k.pop("fee_type", "quadratic"),
            "fee_multiplier": k.pop("fee_multiplier", 1),
        }
    }
    kal.add(KMarket("KXG-1-A", yes_bids=[], no_bids=[], event="KXG-1"))
    v = TimedVenue(clock)
    v.times = {"g": (None, None)}
    v.add(FakeMarket("g", bids=[], asks=[], fee_coefficient=u.pop("fee_coefficient", None)))

    def handler(r: httpx.Request) -> httpx.Response:
        return kal.handle(r) if r.url.host == "api.elections.kalshi.com" else v.handler(r)

    with Client(
        transport=httpx.MockTransport(handler),
        clock=clock,
        store=str(tmp_path / "g.db"),
        kalshi=Kalshi(key_id="k", private_key_pem=pem(priv)),
    ) as c:
        got = c.profit(req, pair=pair("KXG-1-A", "g"))
    assert {x: y for x, y in got.items() if x not in ("match", "filled_in")} == case["response"]
    assert "days_held" not in got["filled_in"]


def test_without_a_pair_or_market_id_it_is_calc_and_reads_nothing(world: Any) -> None:
    make, k, v = world
    c = make(clock=lambda: GOLDEN_NOW)
    for case in GOLDEN["profit"][:50]:
        assert c.profit(case["request"]) == case["response"]
    assert k.calls == [] and v.requests == []


# ---- the rules ----


def test_fills_each_markets_fees_and_days_held(world: Any, clock: Clock) -> None:
    make, k, v = world
    k.times["KXEV-1-A"] = ("2026-10-03T17:00:00Z", "2026-10-17T17:00:00Z")
    v.times["mkt-a"] = ("2026-10-03T16:00:00Z", "2026-10-03T20:00:00Z")
    c = make()
    r = c.profit(BODY, pair=pair())
    assert r["filled_in"] == [
        "kalshi.fee_multiplier",
        "kalshi.fee_type",
        "polymarket_us.fee_coefficient",
        "days_held",
    ]
    assert r["kalshi"]["fee_multiplier"] == 0.5 and r["kalshi"]["fee_type"] == "quadratic_with_maker_fees"
    assert r["polymarket_us"]["fee_coefficient"] == 0.05
    # Expected: the later event (Kalshi's 17:00) + 6 h = 23:00 on 10-03; latest: the later close.
    assert r["match"] == {
        "kalshi": "KXEV-1-A",
        "polymarket_us": "mkt-a",
        "expected_payout_at": "2026-10-03T23:00:00.000Z",
        "latest_payout_at": "2026-10-17T17:00:00.000Z",
    }
    days = (datetime(2026, 10, 3, 23, tzinfo=UTC) - T0).total_seconds() / 86400
    want = calc.profit(
        {
            "contracts": 100,
            "days_held": days,
            "kalshi": {"price": 0.42, "fee_multiplier": 0.5, "fee_type": "quadratic_with_maker_fees"},
            "polymarket_us": {"price": 0.55, "fee_coefficient": 0.05},
        },
        T0,
    )
    assert {x: y for x, y in r.items() if x not in ("match", "filled_in")} == want
    assert c.fees(pair()) == {
        "kalshi": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5},
        "polymarket_us": {"fee_coefficient": 0.05},
        "days_held": days,
        "match": r["match"],
    }


def test_what_the_request_sends_wins(world: Any) -> None:
    make, _, _ = world
    body = {
        "contracts": 10,
        "days_held": 3,
        "kalshi": {"price": 0.42, "fee_multiplier": 1, "fee_type": "quadratic", "role": "maker"},
        "polymarket_us": {"price": 0.55, "fee_coefficient": 0.0695},
    }
    r = make().profit(body, pair=pair())
    assert r["filled_in"] == []
    assert {x: y for x, y in r.items() if x not in ("match", "filled_in")} == calc.profit(body, T0)


def test_a_maker_pays_the_series_maker_rate(world: Any) -> None:
    make, _, _ = world
    body = {"contracts": 100, "kalshi": {"price": 0.5, "role": "maker"}, "polymarket_us": {"price": 0.4}}
    r = make().profit(body, pair=pair())
    assert r["kalshi"]["fee_rate"] == 0.0175  # quadratic_with_maker_fees
    assert (
        make().profit({**body, "kalshi": {**body["kalshi"], "fee_type": "quadratic"}}, pair=pair())["kalshi"][
            "fee_rate"
        ]
        == 0
    )


def test_a_fee_type_the_schedule_doesnt_know_keeps_the_default(world: Any) -> None:
    make, k, _ = world
    k.series["KXEV"] = {"ticker": "KXEV", "fee_type": "flat", "fee_multiplier": 1}
    r = make().profit(BODY, pair=pair())
    assert r["kalshi"]["fee_type"] == "quadratic" and "kalshi.fee_type" not in r["filled_in"]


def test_a_market_without_a_coefficient_gets_the_published_rate(world: Any) -> None:
    make, _, v = world
    v.markets["mkt-a"].fee_coefficient = None
    c = make()
    r = c.profit(BODY, pair=pair())
    assert "polymarket_us.fee_coefficient" not in r["filled_in"]
    assert r["polymarket_us"]["fee_coefficient"] == 0.0695
    assert c.fees(pair())["polymarket_us"] == {}


def test_no_times_no_days_held_and_settles_at_wins(world: Any) -> None:
    make, k, v = world
    k.times["KXEV-1-A"] = (None, None)
    v.times["mkt-a"] = (None, None)
    c = make()
    r = c.profit(BODY, pair=pair())
    assert "days_held" not in r and "days_held" not in r["filled_in"]
    assert r["match"]["expected_payout_at"] is None and "days_held" not in c.fees(pair())
    k.times["KXEV-1-A"] = ("2026-10-03T17:00:00Z", "2026-10-17T17:00:00Z")
    r2 = make().profit({**BODY, "settles_at": "2026-10-11"}, pair=pair())
    assert r2["days_held"] == 9.5 and "days_held" not in r2["filled_in"]  # from 10-01 12:00


def test_days_held_is_at_least_one(world: Any, clock: Clock) -> None:
    make, k, v = world
    soon = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    k.times["KXEV-1-A"] = (None, soon)
    v.times["mkt-a"] = (None, soon)
    assert make().fees(pair())["days_held"] == 1


def test_kalshi_market_id_finds_its_twin_through_layer(world: Any) -> None:
    make, _, v = world
    v.layer_answers["/v0/match"] = {"matched_market": {"venue": "polymarket_us", "market_id": "mkt-a"}}
    c = make()
    r = c.profit({**BODY, "kalshi": {"market_id": "KXEV-1-A", "price": 0.42}})
    assert r["match"]["polymarket_us"] == "mkt-a" and "kalshi.fee_multiplier" in r["filled_in"]
    asked = [q for q in v.requests if q.url.host == "uselayer.sh"]
    assert [dict(q.url.params) for q in asked] == [
        {"venue": "kalshi", "market_id": "KXEV-1-A", "with": "polymarket_us"}
    ]
    assert r == c.profit(BODY, pair=pair())  # the same answer as with the pair


def test_errors(world: Any) -> None:
    make, k, v = world
    c = make()
    v.layer_answers["/v0/match"] = {"matched_market": None, "reason": "no_candidate"}
    with pytest.raises(VenueError) as e:
        c.profit({**BODY, "kalshi": {"market_id": "KXEV-1-A", "price": 0.42}})
    assert e.value.code == "not_found" and "no_candidate" in e.value.message
    with pytest.raises(VenueError) as e:
        c.profit({**BODY, "kalshi": {"market_id": "KX-OTHER", "price": 0.42}}, pair=pair())
    assert e.value.code == "invalid_order"
    with pytest.raises(VenueError) as e:
        c.profit(
            {"contracts": 1, "kalshi": {"market_id": "KXEV-1-A", "price": 0.4}, "polymarket": {"price": 0.5}}
        )
    assert e.value.code == "not_available"
    with pytest.raises(VenueError) as e:
        c.fees([("kalshi", "KXEV-1-A"), ("polymarket", "0xabc")])
    assert e.value.code == "not_available"
    with pytest.raises(VenueError) as e:
        c.fees([("polymarket_us", "a"), ("polymarket_us", "b")])
    assert e.value.code == "invalid_order"
    calls = len(k.calls)
    with pytest.raises(VenueError) as e:  # the body is checked before any venue is asked
        c.profit({**BODY, "contracts": 0}, pair=pair())
    assert e.value.code == "invalid_order" and len(k.calls) == calls
    with pytest.raises(VenueError) as e:
        Client(mode="backtest").fees(pair())
    assert e.value.code == "not_available"


def test_a_match_object_works_either_way_round(world: Any) -> None:
    make, _, _ = world
    m = Match.model_validate(
        {"polymarket_us": {"market_id": "mkt-a"}, "kalshi": {"market_id": "KXEV-1-A"}, "confidence": 1}
    )
    c = make()
    assert c.profit(BODY, pair=m) == c.profit(BODY, pair=list(reversed(pair())))


# ---- times ----


def _info(event: str | None, close: str | None) -> MarketInfo:
    return MarketInfo("v", "m", None, "", True, 0.01, 1, FeeSettings(venue="v"), close, event)


def test_payout_times_follow_layer() -> None:
    h = 3_600_000
    assert payout_times([_info("2026-10-03T17:00:00Z", "2026-10-17T00:00:00Z"), _info(None, None)]) == (
        time_ms("2026-10-03T17:00:00Z") + PAYOUT_BUFFER_MS,  # type: ignore[operator]
        time_ms("2026-10-17T00:00:00Z"),
    )
    # Never past the latest close; a market without an event time counts its close.
    close = time_ms("2026-10-03T18:00:00Z")
    assert payout_times([_info("2026-10-03T17:00:00Z", "2026-10-03T18:00:00Z")]) == (close, close)
    assert payout_times([_info(None, "2026-10-03T18:00:00Z"), _info(None, "2026-10-03T12:00:00Z")]) == (
        close,
        close,
    )
    assert payout_times([_info(None, None)]) == (None, None)
    assert time_ms("2026-10-01T17:07:02.700446463Z") == time_ms("2026-10-01T17:07:02.700Z")
    assert time_ms("2026-10-05") == time_ms("2026-10-05T00:00:00Z") and time_ms("soon") is None
    assert iso_ms(time_ms("2026-10-05T17:00:00.5Z")) == "2026-10-05T17:00:00.500Z"
    assert iso_ms(0 + 6 * h) == "1970-01-01T06:00:00.000Z" and iso_ms(None) is None
