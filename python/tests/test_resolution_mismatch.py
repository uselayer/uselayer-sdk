# ruff: noqa: F811  (fixtures imported from other test modules)
"""Resolution mismatches: a hedged pair the two venues settled differently, with made-up settlements.

The paper pair (conftest's fakes): YES on mkt-a at 0.42 and NO on mkt-b at 0.40, 10 contracts, fees
0.17 + 0.17. It should pay $10 at settlement for $8.20 + $0.34. Each case settles the two markets
some way and checks the mismatch saved, its dollar impact against the $10 expected, and that pnl()
breaks it out of ``realized`` (already inside it, never subtracted twice).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import T0, Clock, FakeVenue
from test_live_pairs import PAIR as LIVE_PAIR
from test_live_pairs import kalshi_venue, pair_live  # noqa: F401  (fixtures)

from uselayer import Book, Client, Level, Resolution
from uselayer.events import SimulatedFill
from uselayer.mismatch import SETTLE_GAP_S, LegOutcome, check, hedged_pairs
from uselayer.store import Store

PAIR = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]
COST, FEES = 8.20, 0.34


def res(market: str, outcome: str, at: datetime, **kw: Any) -> Resolution:
    return Resolution(venue="polymarket_us", market=market, outcome=outcome, as_of=at, **kw)  # type: ignore[arg-type]


def hedge(c: Client) -> str:
    t = c.trade(PAIR, size=10)
    assert (t.status, t.hedged) == ("hedged", 10)
    return t.group_id


def by_hand(p: Any, *, paid: float) -> None:
    """net is what came back minus what went out, whatever the lines say."""
    assert p.net == round(paid - COST - FEES, 6)
    assert p.realized == round(paid - COST, 6)  # what the venues paid minus what it cost
    assert p.realized == round(sum(r.realized for r in p.rows), 6)  # the rows add up to it
    assert p.resolution_mismatch_loss == round(10 - paid, 6)  # of which: lost to mismatched settlement
    assert p.net == round(p.realized + p.unrealized - p.fees, 6)  # the loss isn't subtracted twice


def test_both_legs_lost(make_client: Any, clock: Clock) -> None:
    alerts: list[dict[str, Any]] = []
    c = make_client(on_alert=alerts.append)
    gid = hedge(c)
    c.settle([res("mkt-a", "no", clock.now), res("mkt-b", "yes", clock.now)])
    (m,) = c.resolution_mismatches()
    assert (m.group_id, m.kind, m.pending, m.mode) == (gid, "both_lost", False, "paper")
    assert (m.contracts, m.expected, m.paid, m.impact) == (10, 10, 0, -10)
    assert [(lg.market, lg.side, lg.outcome, lg.payout) for lg in m.legs] == [
        ("mkt-a", "yes", "no", 0.0),
        ("mkt-b", "no", "yes", 0.0),
    ]
    p = c.pnl()
    by_hand(p, paid=0)
    assert (p.resolution_mismatch_loss, p.net) == (10, -8.54)
    assert p.realized == sum(r.realized for r in p.rows) == -8.2  # paid $0 for $8.20
    d = p.to_dict()
    assert d["resolution_mismatch_loss"] == 10 and d["resolution_mismatches"][0]["kind"] == "both_lost"
    assert [a["kind"] for a in alerts].count("resolution_mismatch") == 1  # once, not on every check


def test_both_legs_won_is_flagged_too(make_client: Any, clock: Clock) -> None:
    c = make_client()
    hedge(c)
    c.settle([res("mkt-a", "yes", clock.now), res("mkt-b", "no", clock.now)])
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.paid, m.impact) == ("both_won", 20, 10)
    p = c.pnl()
    by_hand(p, paid=20)
    assert p.resolution_mismatch_loss == -10  # a gain, broken out of realized


@pytest.mark.parametrize(
    ("a", "b", "paid"),
    [
        ({"outcome": "void"}, {"outcome": "yes"}, 4.2),  # mkt-a refunded at cost (0.42); NO on mkt-b lost
        ({"outcome": "void", "payout": 0.37}, {"outcome": "yes"}, 3.7),  # a fair price, as Kalshi pays
        ({"outcome": "no"}, {"outcome": "void"}, 4.0),  # NO on mkt-b refunded at 0.40; YES on mkt-a lost
        ({"outcome": "yes"}, {"outcome": "void"}, 14.0),
    ],
)
def test_one_leg_voided_and_the_other_settled(
    make_client: Any, clock: Clock, a: Any, b: Any, paid: float
) -> None:
    c = make_client()
    hedge(c)
    c.settle([res("mkt-a", at=clock.now, **a), res("mkt-b", at=clock.now, **b)])
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.paid, m.impact) == ("void_one_leg", paid, round(paid - 10, 6))
    by_hand(c.pnl(), paid=paid)


def test_one_leg_still_open_two_days_after_the_other_settled(make_client: Any, clock: Clock) -> None:
    c = make_client()
    hedge(c)
    c.settle([res("mkt-a", "yes", clock.now)])
    clock.advance(SETTLE_GAP_S - 60)
    assert c.resolution_mismatches() == []  # not yet: venues settle hours apart all the time
    clock.advance(120)
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.pending, m.paid, m.impact) == ("settle_gap", True, None, None)
    assert (m.a.outcome, m.b.outcome, m.b.settled_at) == ("yes", None, None)
    first = m.detected_at
    assert c.pnl().resolution_mismatch_loss == 0  # nothing final to count yet

    clock.advance(3600)
    c.settle([res("mkt-b", "yes", clock.now)])  # settled the same way, just late: still paid $1
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.pending, m.paid, m.impact, m.detected_at) == ("settle_gap", False, 10, 0, first)
    assert m.settle_gap_s == SETTLE_GAP_S + 3660
    by_hand(c.pnl(), paid=10)


def test_a_late_leg_that_settles_the_other_way_becomes_both_lost(make_client: Any, clock: Clock) -> None:
    c = make_client()
    hedge(c)
    c.settle([res("mkt-a", "no", clock.now)])
    clock.advance(SETTLE_GAP_S + 1)
    assert c.resolution_mismatches()[0].kind == "settle_gap"
    c.settle([res("mkt-b", "yes", clock.now)])
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.impact, m.pending) == ("both_lost", -10, False)


@pytest.mark.parametrize(
    ("a", "b"),
    [("yes", "yes"), ("no", "no"), ("void", "void")],  # the pair paid $1, or both venues refunded it
)
def test_settled_the_same_way_is_not_a_mismatch(make_client: Any, clock: Clock, a: str, b: str) -> None:
    c = make_client()
    hedge(c)
    c.settle([res("mkt-a", a, clock.now)])
    clock.advance(3600)
    c.settle([res("mkt-b", b, clock.now)])
    p = c.pnl()
    assert c.resolution_mismatches() == [] and p.resolution_mismatches == ()
    assert p.resolution_mismatch_loss == 0 and p.realized == round(sum(r.realized for r in p.rows), 6)


def test_paper_finds_it_from_the_venues_settlements(make_client: Any, venue: FakeVenue, clock: Clock) -> None:
    c = make_client()
    hedge(c)
    venue.settlements["mkt-a"] = 0  # Polymarket US: NO won on mkt-a ...
    venue.settlements["mkt-b"] = 1  # ... and YES on mkt-b: both legs of the pair lost
    clock.advance(61)
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.impact) == ("both_lost", -10)


def test_saved_in_the_store_and_read_back(make_client: Any, clock: Clock, tmp_path: Any) -> None:
    path = str(tmp_path / "kept.db")
    c = make_client(store=path)
    gid = hedge(c)
    c.settle([res("mkt-a", "no", clock.now), res("mkt-b", "yes", clock.now)])
    c.close()
    s = Store(path)
    (m,) = s.mismatches()
    assert (m.group_id, m.kind, m.impact, m.a.venue, m.b.venue) == (
        gid,
        "both_lost",
        -10,
        "polymarket_us",
        "polymarket_us",
    )
    s.close()
    again = make_client(store=path)
    assert [x.group_id for x in again.resolution_mismatches()] == [gid]


def test_only_pairs_are_checked(make_client: Any, clock: Clock) -> None:
    c = make_client()
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=5)  # one leg, no pair
    c.settle([res("mkt-a", "no", clock.now)])
    clock.advance(SETTLE_GAP_S * 2)
    assert c.resolution_mismatches() == [] and c.pnl().resolution_mismatch_loss == 0


def _fill(gid: str, market: str, side: str, n: float, action: str = "buy") -> SimulatedFill:
    return SimulatedFill(
        mode="paper",
        order_id="o",
        venue="polymarket_us",
        market=market,
        side=side,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        contracts=n,
        price=0.5,
        cost=n * 0.5,
        fee=0.0,
        role="taker",
        at=T0,
        book_as_of=T0,
        group_id=gid,
    )


def test_hedged_pairs_counts_what_each_group_still_holds() -> None:
    fills = [
        _fill("g1", "a", "yes", 10),
        _fill("g1", "b", "no", 6),  # exposed: 4 left on one side, 6 hedged
        _fill("g2", "a", "yes", 5),
        _fill("g2", "a", "yes", 5, "sell"),  # unwound
        _fill("g2", "b", "no", 0.0),
        _fill("g3", "a", "yes", 5),
        _fill("g3", "b", "yes", 5),  # same side twice: not a hedge
    ]
    pairs = hedged_pairs(fills)
    assert set(pairs) == {"g1"}
    results = {
        ("polymarket_us", "a", "yes"): LegOutcome("no", 0.0, T0),
        ("polymarket_us", "b", "no"): LegOutcome("yes", 0.0, T0),
    }
    (m,) = check(pairs, results, now=T0, mode="paper")
    assert (m.contracts, m.paid, m.impact) == (6, 0, -6)  # only the hedged contracts count


# ---- backtest ----


def test_backtest_replays_the_resolutions_and_flags_the_pair() -> None:
    def book(market: str, s: int, bid: float, ask: float) -> Book:
        return Book(
            venue="polymarket_us",
            market=market,
            bids=(Level(price=bid, size=50),),
            asks=(Level(price=ask, size=50),),
            as_of=T0 + timedelta(seconds=s),
        )

    events = [
        book("mkt-a", 0, 0.40, 0.42),
        book("mkt-b", 1, 0.60, 0.62),
        res("mkt-a", "no", T0 + timedelta(hours=5)),
        res("mkt-b", "yes", T0 + timedelta(hours=6)),
    ]
    alerts: list[dict[str, Any]] = []
    bt = Client(mode="backtest", books=events, on_alert=alerts.append)

    def strategy(client: Client, pair: Any, quote: Any) -> None:
        if not client.positions() and not client.settlements():
            client.trade(pair, size=10)

    bt.run(strategy, [PAIR])
    (m,) = bt.resolution_mismatches()
    assert (m.kind, m.mode, m.impact, m.settle_gap_s) == ("both_lost", "backtest", -10, 3600)
    assert any(a["kind"] == "resolution_mismatch" for a in alerts)  # flagged as the replay settled it
    p = bt.pnl()
    assert p.resolution_mismatch_loss == 10 and p.net == round(-COST - FEES, 6)


# ---- live: what each venue paid, read with your own keys ----


def _finalize(kalshi: Any, *, result: str = "", value: str | None = None, at: str | None = None) -> datetime:
    """Kalshi finalizes the market, by default an hour before the fake clock's now. Returns its settlement time.

    The fake clock only moves forward (the Kalshi fake's throttle can push it hours on a slow machine), so a
    time in its past stays in the past; a payout is never dated after the moment it was read.
    """
    m = kalshi.markets["KXEV-1-P"]
    at = at or (kalshi.clock.now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    m.status, m.result, m.settlement_value, m.settlement_ts = "finalized", result, value, at
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


def test_live_both_lost_from_each_venues_payout(pair_live: Any, venue: FakeVenue, clock: Clock) -> None:
    make, kalshi, _, alerts = pair_live
    c = make()
    t = c.trade(LIVE_PAIR, size=50, min_edge=0.01)  # YES on Kalshi, NO on Polymarket US
    assert t.status == "hedged"
    assert c.resolution_mismatches() == []  # neither venue has settled
    settled = _finalize(kalshi, result="no")  # Kalshi: NO won, so the YES leg lost
    venue.settlements["mkt-c"] = 1  # Polymarket US: YES won, so the NO leg lost
    clock.advance(61)
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.mode, m.contracts, m.paid, m.impact) == ("both_lost", "live", 50, 0, -50)
    assert [(lg.venue, lg.side, lg.outcome, lg.payout) for lg in m.legs] == [
        ("kalshi", "yes", "no", 0.0),
        ("polymarket_us", "no", "yes", 0.0),
    ]
    assert m.a.settled_at == settled  # Kalshi's settlement_ts
    assert {(v, mk) for v, mk, _, _ in c._store.venue_payouts()} == {
        ("kalshi", "KXEV-1-P"),
        ("polymarket_us", "mkt-c"),
    }
    assert any(a["kind"] == "resolution_mismatch" for a in alerts)
    layer = [r for r in venue.requests if r.url.host == "uselayer.sh"]
    assert layer == []  # nothing about the pair or its results goes to Layer


def test_live_kalshi_fair_price_against_a_settled_twin(
    pair_live: Any, venue: FakeVenue, clock: Clock
) -> None:
    make, kalshi, _, _ = pair_live
    c = make()
    c.trade(LIVE_PAIR, size=50, min_edge=0.01)
    _finalize(kalshi, result="scalar", value="0.3700")  # a canceled game, paid at a fair price
    venue.settlements["mkt-c"] = 0  # the twin settled NO: the NO leg pays $1
    clock.advance(61)
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.paid, m.impact) == ("void_one_leg", 68.5, 18.5)


def test_live_one_venue_settled_the_other_still_open(pair_live: Any, venue: FakeVenue, clock: Clock) -> None:
    make, kalshi, _, _ = pair_live
    c = make()
    c.trade(LIVE_PAIR, size=50, min_edge=0.01)
    _finalize(kalshi, result="yes", at=(clock.now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"))
    clock.advance(61)
    assert c.resolution_mismatches() == []
    clock.advance(SETTLE_GAP_S)
    (m,) = c.resolution_mismatches()
    assert (m.kind, m.pending, m.b.venue, m.b.outcome) == ("settle_gap", True, "polymarket_us", None)


def test_live_venue_reads_are_rate_limited(pair_live: Any, venue: FakeVenue, clock: Clock) -> None:
    make, kalshi, _, _ = pair_live
    c = make()
    c.trade(LIVE_PAIR, size=50, min_edge=0.01)
    _finalize(kalshi, result="yes")
    pm_reads = lambda: sum(1 for r in venue.requests if r.url.path.endswith("/settlement"))  # noqa: E731
    k_reads = lambda: kalshi.calls.count("GET /markets/KXEV-1-P")  # noqa: E731
    k0 = k_reads()
    c.resolution_mismatches()
    c.resolution_mismatches()
    assert (pm_reads(), k_reads() - k0) == (1, 1)  # once a minute per market; a settled one, once
    clock.advance(61)
    c.resolution_mismatches()
    assert (pm_reads(), k_reads() - k0) == (2, 1)


def test_live_pnl_shows_the_loss_line(pair_live: Any, venue: FakeVenue, clock: Clock) -> None:
    make, kalshi, _, _ = pair_live
    c = make()
    c.trade(LIVE_PAIR, size=50, min_edge=0.01)
    _finalize(kalshi, result="no")
    venue.settlements["mkt-c"] = 1
    clock.advance(61)
    p = c.pnl()
    assert p.mode == "live" and p.resolution_mismatch_loss == 50
    assert [m.kind for m in p.resolution_mismatches] == ["both_lost"]
    assert p.net == round(p.realized + p.unrealized - p.fees, 6)
    assert p.realized == round(sum(r.realized for r in p.rows), 6)
