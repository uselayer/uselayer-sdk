"""buy_best(), sell_best() and preview_best(): every choice and every skip, against fake venues.

Paper pairs are Kalshi KXEV-1-A (YES asks 0.40 × 100, 0.41 × 50: NO bids 0.60 and 0.59; YES bids 0.41
× 5, 0.40 × 10) and Polymarket US markets set per test. Live pairs reuse test_live_pairs' fakes,
which check every signature. No real venue is ever contacted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue
from cryptography.hazmat.primitives.asymmetric import ed25519
from test_kalshi import FakeKalshi, KMarket, cached_until_waited, pem
from test_live_pairs import kalshi_venue, pair_live, posts  # noqa: F401  (fixtures)

from uselayer import Admin, Book, Client, Kalshi, Level, Match, VenueError, _switches

K = ("kalshi", "KXEV-1-A")


@pytest.fixture
def kalshi(clock: Clock) -> tuple[FakeKalshi, Kalshi]:
    priv = ed25519.Ed25519PrivateKey.generate()
    k = FakeKalshi(priv.public_key(), clock)
    k.add(KMarket("KXEV-1-A", yes_bids=[(0.40, 10), (0.41, 5)], no_bids=[(0.60, 100), (0.59, 50)]))
    return k, Kalshi(key_id="k", private_key_pem=pem(priv))


@pytest.fixture
def paper(venue: FakeVenue, kalshi: tuple[FakeKalshi, Kalshi], clock: Clock, tmp_path: Any) -> Any:
    """Paper clients over fake Kalshi + Polymarket US; ``kalshi_key=False`` leaves the Kalshi key out."""
    fake, key = kalshi
    made: list[Client] = []

    def handler(r: httpx.Request) -> httpx.Response:
        return fake.handle(r) if r.url.host == "api.elections.kalshi.com" else venue.handler(r)

    def mk(*, kalshi_key: bool = True, **kw: Any) -> Client:
        kw.setdefault("store", str(tmp_path / f"b-{len(made)}.db"))
        c = Client(
            transport=httpx.MockTransport(handler),
            clock=clock,
            sleep=clock.sleep,
            kalshi=key if kalshi_key else None,
            **kw,
        )
        made.append(c)
        return c

    yield mk
    for c in made:
        c.close()


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch: Any) -> None:
    for k in ("KALSHI_KEY_ID", "POLYMARKET_US_KEY_ID", "LAYER_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def costs(r: Any) -> dict[str, Any]:
    return {v.market: (v.all_in, v.skip) for v in r.why.venues}


# ---- the choice ----


def test_cheaper_after_fees_wins_even_at_a_higher_price(paper: Any, venue: FakeVenue, kalshi: Any) -> None:
    kalshi[0].series["KXEV"] = {"fee_type": "quadratic", "fee_multiplier": 2}
    venue.add(FakeMarket("pm-x", bids=[(0.38, 50)], asks=[(0.41, 100)]))
    c = paper()
    r = c.buy_best([K, ("polymarket_us", "pm-x")], "yes", 10)
    # Kalshi: 10 × 0.40 = 4.00 + 0.07 × 2 × 10 × 0.4 × 0.6 = 0.336 → 0.34 = 4.34.
    # Polymarket US: 10 × 0.41 = 4.10 + 0.0695 × 10 × 0.41 × 0.59 = 0.168 → 0.17 = 4.27.
    assert costs(r) == {"KXEV-1-A": (4.34, None), "pm-x": (4.27, None)}
    assert (r.why.venue, r.why.reason_code, r.why.saving) == ("polymarket_us", "cheaper", 0.07)
    assert r.why.reason == "Polymarket US is $0.07 cheaper all-in for 10 contracts: $4.27 vs $4.34 on Kalshi."
    assert r.sent and (r.order.venue, r.order.price, r.order.filled, r.order.tif) == (
        "polymarket_us",
        0.41,
        10,
        "ioc",
    )
    assert [(f.venue, f.simulated) for f in c.fills()] == [("polymarket_us", True)]


def test_a_kalshi_book_read_before_a_polymarket_us_wait_is_read_again(
    paper: Any, venue: FakeVenue, kalshi: Any, clock: Clock
) -> None:
    # Kalshi YES asks 0.40 (cheaper) when first read. Polymarket US's cached copy is 12 s old, so the
    # SDK waits 18.5 s for a fresh one; meanwhile Kalshi's ask moves to 0.48. The choice uses 0.48.
    venue.add(FakeMarket("pm-x", bids=[(0.38, 50)], asks=[(0.41, 100)]))

    def kalshi_moves() -> None:
        kalshi[0].markets["KXEV-1-A"].no_bids = [(0.52, 100)]

    cached_until_waited(venue, clock, then=kalshi_moves)
    c = paper()
    r = c.buy_best([K, ("polymarket_us", "pm-x")], "yes", 10)
    k, pm = r.why.venues
    assert clock.slept[0] == 18.5
    assert (k.best_price, k.skip) == (0.48, None) and (pm.best_price, pm.skip) == (0.41, None)
    assert all((clock.now - v.book_as_of).total_seconds() <= 10 for v in (k, pm))
    assert r.why.venue == "polymarket_us" and r.sent and r.order.filled == 10


def test_preview_best_compares_books_fresh_together(paper: Any, venue: FakeVenue, clock: Clock) -> None:
    venue.add(FakeMarket("pm-x", bids=[(0.38, 50)], asks=[(0.41, 100)]))
    cached_until_waited(venue, clock)
    c = paper()
    r = c.preview_best([K, ("polymarket_us", "pm-x")], "yes", 10)
    assert [v.skip for v in r.why.venues] == [None, None]
    assert all((clock.now - v.book_as_of).total_seconds() <= 10 for v in r.why.venues)
    assert r.why.venue == "kalshi" and r.sent is False and c.fills() == []


def test_the_walk_goes_level_by_level_and_the_limit_is_the_deepest_level(
    paper: Any, venue: FakeVenue
) -> None:
    venue.add(FakeMarket("pm-x", bids=[(0.38, 50)], asks=[(0.39, 50), (0.43, 100)]))
    c = paper()
    r = c.preview_best([K, ("polymarket_us", "pm-x")], "yes", 120)
    k, pm = r.why.venues
    # Kalshi: 100 × 0.40 + 20 × 0.41 = 48.20 + fees 1.68 + 0.34 = 50.22.
    assert (k.limit_price, k.cost, k.fees, k.all_in, len(k.fills)) == (0.41, 48.2, 2.02, 50.22, 2)
    # Polymarket US: 50 × 0.39 + 70 × 0.43 = 49.60 + fees 0.83 + 1.19 = 51.62.
    assert (pm.limit_price, pm.cost, pm.fees, pm.all_in) == (0.43, 49.6, 2.02, 51.62)
    assert r.why.venue == "kalshi" and r.order.price == 0.41 and r.order.size == 120
    assert c.fills() == [] and r.sent is False and r.preview.allowed


def test_a_tie_goes_to_the_venue_with_more_size_at_its_price(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("pm-p", bids=[(0.40, 5)], asks=[(0.42, 10)]))
    venue.add(FakeMarket("pm-q", bids=[(0.40, 5)], asks=[(0.42, 60)]))
    r = make_client().buy_best([("polymarket_us", "pm-p"), ("polymarket_us", "pm-q")], "yes", 5)
    assert r.why.venues[0].all_in == r.why.venues[1].all_in
    assert (r.why.reason_code, r.order.market, r.why.saving) == ("tie_more_size", "pm-q", 0.0)
    assert "(60 vs 10 contracts)" in r.why.reason


def test_a_full_tie_goes_to_the_pairs_first_leg(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("pm-p", bids=[(0.40, 5)], asks=[(0.42, 10)]))
    venue.add(FakeMarket("pm-q", bids=[(0.40, 5)], asks=[(0.42, 10)]))
    r = make_client().preview_best([("polymarket_us", "pm-q"), ("polymarket_us", "pm-p")], "yes", 5)
    assert (r.why.reason_code, r.order.market) == ("tie_first_listed", "pm-q")


def test_side_comes_from_each_market_id_so_an_inverted_polymarket_us_side_compares_right(
    paper: Any, venue: FakeVenue
) -> None:
    # Layer pairs Kalshi's YES with the Polymarket US *short* side: YES on pm-s:short is the long side's NO.
    venue.add(FakeMarket("pm-s", bids=[(0.62, 100)], asks=[(0.64, 100)]))
    match = Match.model_validate(
        {
            "kalshi": {"market_id": "KXEV-1-A"},
            "polymarket_us": {"market_id": "pm-s:short", "slug": "pm-s"},
        }
    )
    c = paper()
    r = c.preview_best(match, "yes", 10)
    pm = r.why.venues[1]
    assert (pm.market, pm.best_price, pm.limit_price) == ("pm-s:short", 0.38, 0.38)  # 1 − the long bid 0.62
    assert r.why.venue == "polymarket_us"  # 0.38 beats Kalshi's 0.40
    sent = c.buy_best(match, "yes", 10)
    f = c.fills()[0]
    assert (sent.order.market, f.market, f.side, f.price) == ("pm-s:short", "pm-s:short", "yes", 0.38)


def test_sell_best_sells_where_it_pays_more_after_fees(make_client: Any, venue: FakeVenue) -> None:
    c = make_client()
    pair = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=5)
    c.buy(venue="polymarket_us", market="mkt-b", side="yes", price=0.62, size=5)
    r = c.sell_best(pair, "yes", 5)
    # mkt-a pays 5 × 0.40 − 0.08 (0.0834); mkt-b 5 × 0.60 − 0.08 (0.0834).
    assert costs(r) == {"mkt-a": (1.92, None), "mkt-b": (2.92, None)}
    assert (r.why.reason_code, r.order.market, r.order.action, r.order.filled) == (
        "pays_more",
        "mkt-b",
        "sell",
        5,
    )
    assert "pays $1.00 more after fees" in r.why.reason


# ---- the skips ----


def test_sell_best_skips_a_venue_where_the_account_holds_too_little(make_client: Any) -> None:
    c = make_client()
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=5)
    r = c.sell_best([("polymarket_us", "mkt-b"), ("polymarket_us", "mkt-a")], "yes", 5)
    assert costs(r)["mkt-b"] == (None, "not_held")
    assert (r.why.reason_code, r.order.market) == ("only_venue", "mkt-a")
    with pytest.raises(VenueError) as e:
        c.sell_best([("polymarket_us", "mkt-b"), ("polymarket_us", "mkt-a")], "yes", 5)  # nothing left
    assert e.value.code == "not_available"


def test_no_key_is_skipped(paper: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("pm-x", bids=[(0.38, 50)], asks=[(0.45, 100)]))
    r = paper(kalshi_key=False).buy_best([K, ("polymarket_us", "pm-x")], "yes", 10)
    assert costs(r)["KXEV-1-A"] == (None, "no_key")
    assert r.why.reason.startswith(
        "Polymarket US is the only venue that can take it: Kalshi was skipped (no_key"
    )
    assert r.order.venue == "polymarket_us"


def test_a_switched_off_venue_is_skipped(make_client: Any) -> None:
    r = make_client().preview_best([("polymarket", "0xabc"), ("polymarket_us", "mkt-a")], "yes", 5)
    assert costs(r)["0xabc"] == (None, "switched_off") and r.why.venue == "polymarket_us"


def test_markets_and_venues_rules_skip_a_venue(paper: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("pm-x", bids=[(0.30, 50)], asks=[(0.35, 100)]))
    pair = [K, ("polymarket_us", "pm-x")]
    r = paper(rules={"markets": ["KX*"]}).buy_best(pair, "yes", 10)
    assert costs(r)["pm-x"] == (None, "not_allowed") and r.order.venue == "kalshi"
    r2 = paper(rules={"venues": ["kalshi"]}).preview_best(pair, "yes", 10)
    assert r2.why.venues[1].skip == "not_allowed" and "isn't in venues" in (r2.why.venues[1].detail or "")


def test_a_closed_market_is_skipped(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("pm-c", bids=[(0.30, 50)], asks=[(0.31, 100)], status="MARKET_STATUS_CLOSED"))
    r = make_client().preview_best([("polymarket_us", "pm-c"), ("polymarket_us", "mkt-a")], "yes", 5)
    assert costs(r)["pm-c"] == (None, "market_closed") and r.why.venue == "polymarket_us"
    assert r.order.market == "mkt-a"


def test_a_venue_that_cant_fill_the_size_is_skipped(make_client: Any) -> None:
    # mkt-b offers 40 YES at 0.62; mkt-a 130 up to 0.45, within the collar of its 0.42 best ask.
    r = make_client().preview_best([("polymarket_us", "mkt-b"), ("polymarket_us", "mkt-a")], "yes", 100)
    b = r.why.venues[0]
    assert (b.skip, b.cap, b.capped_by) == ("not_enough_size", 0.67, "price_collar")
    assert b.detail == (
        "Only 40 contracts on Polymarket US at or below 0.67, the limit set by the price collar (0.05 from the best price)."
    )
    assert r.order.market == "mkt-a" and r.order.price == 0.45


def test_the_collar_caps_the_walk(make_client: Any, venue: FakeVenue) -> None:
    # 10 at 0.42, then 100 at 0.48: past best ask + 5¢, so only 10 count.
    venue.add(FakeMarket("pm-w", bids=[(0.40, 5)], asks=[(0.42, 10), (0.48, 100)]))
    r = make_client().preview_best([("polymarket_us", "pm-w"), ("polymarket_us", "mkt-b")], "yes", 20)
    assert (r.why.venues[0].skip, r.why.venues[0].cap) == ("not_enough_size", 0.47)
    assert r.why.venue == "polymarket_us" and r.order.market == "mkt-b"


def test_max_price_is_respected(make_client: Any) -> None:
    pair = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]
    r = make_client().preview_best(pair, "yes", 20, max_price=0.425)
    a, b = r.why.venues
    assert (a.skip, a.cap, a.capped_by) == ("not_enough_size", 0.425, "max_price")  # 10 at 0.42 only
    assert "the limit set by max_price 0.425" in (a.detail or "")
    assert (b.skip, b.best_price) == ("above_max_price", 0.62)
    assert r.order is None and r.why.reason_code == "no_venue"
    r2 = make_client().preview_best(pair, "yes", 20, max_price=0.43)
    assert r2.order.market == "mkt-a" and r2.order.price == 0.43  # within max_price


def test_min_price_is_respected_on_a_sell(make_client: Any) -> None:
    c = make_client()
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=5)
    r = c.preview_best(
        [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")], "yes", 5, action="sell", min_price=0.45
    )
    assert r.why.venues[0].skip == "below_min_price" and r.order is None


def test_a_stale_book_is_skipped(make_client: Any) -> None:
    c = make_client()
    orig = c.book

    def old(market: Any, *, venue: str = "polymarket_us") -> Book:
        b = orig(market, venue=venue)
        return b.model_copy(update={"as_of": b.as_of - timedelta(seconds=60)}) if market == "mkt-a" else b

    c.book = old  # type: ignore[method-assign]
    r = c.preview_best([("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")], "yes", 5)
    assert costs(r)["mkt-a"] == (None, "stale_book") and r.order.market == "mkt-b"


def test_an_unknown_market_or_a_venue_that_fails_is_skipped(make_client: Any) -> None:
    r = make_client().preview_best([("polymarket_us", "nope"), ("polymarket_us", "mkt-a")], "yes", 5)
    assert r.why.venues[0].skip in ("not_found", "unavailable") and r.order.market == "mkt-a"


def test_no_venue_left_raises_and_sends_nothing(make_client: Any) -> None:
    c = make_client()
    with pytest.raises(VenueError) as e:
        c.buy_best([("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")], "yes", 1000)
    assert e.value.code == "not_available" and e.value.message.startswith("No venue can take this order.")
    assert [v["skip"] for v in e.value.raw["venues"]] == ["not_enough_size", "not_enough_size"]
    assert c.fills() == [] and c.orders(open=False) == []


# ---- the normal send() path ----


def test_every_guardrail_applies_to_the_chosen_order(make_client: Any, tmp_path: Any) -> None:
    pair = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]
    c = make_client(rules={"max_position": {"per_market": 2}})
    p = c.preview_best(pair, "yes", 10)
    assert p.order.market == "mkt-a" and p.preview.blocked_by == "max_position"
    with pytest.raises(VenueError) as e:
        c.buy_best(pair, "yes", 10)
    assert (e.value.code, e.value.rule) == ("blocked_by_rule", "max_position") and c.fills() == []

    store = str(tmp_path / "killed.db")
    k = make_client(store=store)
    Admin(mode="paper", store=store).kill()
    with pytest.raises(VenueError) as e2:
        k.buy_best(pair, "yes", 5)
    assert e2.value.rule == "kill_switch" and k.fills() == []


def test_fok_and_bad_arguments(make_client: Any) -> None:
    pair = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]
    c = make_client()
    assert c.buy_best(pair, "yes", 5, tif="fok").order.tif == "fok"
    for kw in ({"tif": "gtc"}, {"side": "maybe"}, {"size": 0}, {"max_price": 1.2}):
        args = {"side": "yes", "size": 5, **kw}
        with pytest.raises(VenueError) as e:
            c.preview_best(pair, **args)
        assert e.value.code == "invalid_order"
    with pytest.raises(VenueError):
        c.preview_best(pair, "yes", 5, min_price=0.4)  # min_price is for sells
    with pytest.raises(VenueError):
        c.preview_best(pair, "yes", 5, max_price=0.4, action="sell")


def test_to_dict_is_plain_json(make_client: Any) -> None:
    import json

    r = make_client().buy_best([("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")], "yes", 5)
    d = json.loads(str(r))
    assert d["sent"] and d["why"]["venue"] == "polymarket_us" and d["why"]["market"] == "mkt-a"
    assert {v["market"] for v in d["why"]["venues"]} == {"mkt-a", "mkt-b"}


# ---- backtest ----


def book(market: str, asks: list[tuple[float, float]], s: int, venue: str = "polymarket_us") -> Book:
    return Book(
        venue=venue,
        market=market,
        bids=[Level(price=0.30, size=100)],
        asks=[Level(price=p, size=n) for p, n in asks],
        as_of=T0 + timedelta(seconds=s),
        source="recorded",
    )


def test_backtest_picks_from_the_replayed_books() -> None:
    books = [
        book("bt-a", [(0.50, 100)], 0),
        book("KXBT", [(0.45, 100)], 1, venue="kalshi"),
        book("bt-a", [(0.40, 100)], 2),
    ]
    c = Client(mode="backtest", books=books)
    got: list[Any] = []
    pair = [("kalshi", "KXBT"), ("polymarket_us", "bt-a")]

    def on_book(cl: Client, b: Book) -> None:
        r = cl.preview_best(pair, "yes", 10)
        got.append((b.as_of.second, r.why.venue, [v.skip for v in r.why.venues]))
        if b.as_of.second == 2:
            got.append(cl.buy_best(pair, "yes", 10).order.filled)

    c.replay(on_book)
    assert got == [
        (0, "polymarket_us", ["no_book", None]),
        (1, "kalshi", [None, None]),  # 0.45 on Kalshi beats 0.50
        (2, "polymarket_us", [None, None]),  # until Polymarket US drops to 0.40
        10,
    ]
    assert [(f.venue, f.price, f.simulated) for f in c.fills()] == [("polymarket_us", 0.40, True)]


# ---- live ----

LIVE = [("kalshi", "KXEV-1-P"), ("polymarket_us", "mkt-c")]


def test_live_sends_one_real_order_to_the_cheaper_venue(pair_live: Any) -> None:  # noqa: F811
    make, kalshi, api, _ = pair_live
    c = make()
    # YES: Kalshi asks 0.40, Polymarket US 0.57.
    r = c.buy_best(LIVE, "yes", 5)
    assert (r.order.venue, r.order.mode, r.order.filled) == ("kalshi", "live", 5)
    assert posts(kalshi, api) == (1, 0)
    # NO: Kalshi asks 1 − 0.38 = 0.62, Polymarket US 1 − 0.55 = 0.45.
    r2 = c.buy_best(LIVE, "no", 5)
    assert (r2.order.venue, r2.order.side, r2.order.filled) == ("polymarket_us", "no", 5)
    assert posts(kalshi, api) == (1, 1)


def test_live_preview_sends_nothing(pair_live: Any) -> None:  # noqa: F811
    make, kalshi, api, _ = pair_live
    r = make().preview_best(LIVE, "yes", 5)
    assert r.why.venue == "kalshi" and r.preview.allowed and posts(kalshi, api) == (0, 0)


def test_live_without_a_venue_key_skips_that_venue(pair_live: Any) -> None:  # noqa: F811
    make, kalshi, api, _ = pair_live
    r = make(keys=("kalshi",)).buy_best(LIVE, "no", 5)  # Polymarket US would be cheaper
    assert costs(r)["mkt-c"] == (None, "no_key") and r.order.venue == "kalshi"
    assert posts(kalshi, api) == (1, 0)


def test_live_switched_off_is_skipped(pair_live: Any, monkeypatch: Any) -> None:  # noqa: F811
    make, kalshi, api, _ = pair_live
    c = make()
    monkeypatch.setitem(_switches.TRADING, "kalshi", False)
    r = c.buy_best(LIVE, "yes", 5)
    assert costs(r)["KXEV-1-P"] == (None, "switched_off") and r.order.venue == "polymarket_us"
    assert posts(kalshi, api) == (0, 1)


# ---- a buy by dollars (spend=) ----

AB = [("polymarket_us", "mkt-a"), ("polymarket_us", "mkt-b")]


def most_by_brute_force(c: Client, venue: str, market: str, spend: float, top: int) -> int:
    """The most whole contracts on one leg whose all-in fits in ``spend``: every size priced one at a
    time, against one read of the book (the per-size walk buy_best() uses)."""
    from uselayer import best

    info, book = c.market(market, venue=venue), c.book(market, venue=venue)
    sizes = [best._cost(c, venue, market, "yes", "buy", n, None, info, book) for n in range(1, top + 1)]
    return max((int(v.size) for v in sizes if v.ok and (v.all_in or 0) <= spend), default=0)


@pytest.mark.parametrize("spend", [1.0, 4.99, 5.0, 12.34, 50.0, 100.0])
def test_spend_buys_the_most_whole_contracts_that_fit_on_each_venue(make_client: Any, spend: float) -> None:
    c = make_client()
    r = c.preview_best(AB, "yes", spend=spend)
    for v in r.why.venues:
        most = most_by_brute_force(c, v.venue, v.market, spend, 140)
        assert (v.size, v.skip) == ((most, None) if most else (1, "spend_too_small"))
        if v.ok:
            assert v.all_in <= spend
    assert r.why.spend == spend and r.sent is False and c.fills() == []


def test_spend_goes_where_the_same_money_wins_more(make_client: Any) -> None:
    # $20: mkt-a (asks 0.42, 0.43, 0.45) buys ~45 contracts; mkt-b (0.62 × 40) only ~31.
    c = make_client()
    r = c.buy_best(AB, "yes", spend=20)
    a, b = r.why.venues
    assert a.size > b.size and a.all_in <= 20 and b.all_in <= 20
    assert (r.why.reason_code, r.why.venue, r.why.size, r.why.saving) == (
        "wins_more",
        "polymarket_us",
        a.size,
        a.size - b.size,
    )
    assert r.why.reason == (
        f"Polymarket US mkt-a wins ${a.size - b.size:,.2f} more if you're right: $20.00 buys {a.size:g} contracts "
        f"there for ${a.all_in:,.2f} all-in, vs {b.size:g} for ${b.all_in:,.2f} on Polymarket US mkt-b."
    )
    assert r.sent and (r.order.market, r.order.size, r.order.filled) == ("mkt-a", a.size, a.size)
    assert r.order.price == a.limit_price
    f = c.fills()
    assert sum(x.contracts for x in f) == a.size and round(sum(x.cost + x.fee for x in f), 6) <= 20


def test_spend_runs_out_of_book_before_money(make_client: Any) -> None:
    # $100 is more than either book holds within the collar: mkt-a 130 up to 0.45, mkt-b 40 at 0.62.
    r = make_client().preview_best(AB, "yes", spend=100)
    a, b = r.why.venues
    assert (a.size, b.size) == (130, 40) and a.all_in < 100 and b.all_in < 100
    assert r.why.reason_code == "wins_more" and r.order.size == 130


def test_spend_respects_max_price(make_client: Any) -> None:
    r = make_client().preview_best(AB, "yes", spend=100, max_price=0.425)
    a, b = r.why.venues
    assert (a.size, a.limit_price) == (10, 0.42)  # only the 10 at 0.42 are within max_price
    assert b.skip == "above_max_price"
    assert (r.why.reason_code, r.order.size) == ("only_venue", 10)


def test_the_same_number_of_contracts_is_compared_on_cost(make_client: Any, venue: FakeVenue) -> None:
    venue.add(FakeMarket("pm-p", bids=[(0.40, 5)], asks=[(0.42, 10)]))
    venue.add(FakeMarket("pm-q", bids=[(0.40, 5)], asks=[(0.42, 60)]))
    r = make_client().preview_best([("polymarket_us", "pm-p"), ("polymarket_us", "pm-q")], "yes", spend=2)
    p, q = r.why.venues
    assert p.size == q.size and p.all_in == q.all_in
    assert (r.why.reason_code, r.order.market) == ("tie_more_size", "pm-q")


def test_too_little_to_buy_one_contract(make_client: Any) -> None:
    c = make_client()
    r = c.preview_best(AB, "yes", spend=0.30)
    assert [v.skip for v in r.why.venues] == ["spend_too_small", "spend_too_small"]
    assert r.why.venues[0].detail.startswith("$0.30 doesn't buy one contract on Polymarket US: it costs $0.4")
    assert (r.order, r.why.reason_code, r.why.size) == (None, "no_venue", None)
    with pytest.raises(VenueError) as e:
        c.buy_best(AB, "yes", spend=0.30)
    assert e.value.code == "not_available" and c.fills() == []


def test_spend_reads_each_book_once(make_client: Any) -> None:
    c = make_client()
    reads: list[Any] = []
    orig = c._fresh_books

    def counted(legs: Any) -> Any:
        reads.append(list(legs))
        return orig(legs)

    c._fresh_books = counted  # type: ignore[method-assign]
    c.preview_best(AB, "yes", spend=50)
    assert reads == [AB]


def test_spend_arguments(make_client: Any) -> None:
    c = make_client()
    for args, kw in (
        ((5,), {"spend": 5}),  # both
        ((), {}),  # neither
        ((), {"spend": 0}),
        ((), {"spend": -3}),
        ((), {"spend": float("nan")}),
        ((), {"spend": float("inf")}),
        ((), {"spend": 5, "max_price": 1.5}),
    ):
        with pytest.raises(VenueError) as e:
            c.preview_best(AB, "yes", *args, **kw)
        assert e.value.code == "invalid_order"
    with pytest.raises(VenueError) as e2:
        c.preview_best(AB, "yes", spend=5, action="sell")
    assert "spend is for buys" in e2.value.message
    with pytest.raises(VenueError):
        c.buy_best(AB, "yes")


def test_spend_to_dict(make_client: Any) -> None:
    import json

    d = json.loads(str(make_client().preview_best(AB, "yes", spend=20)))
    assert d["why"]["spend"] == 20 and d["why"]["size"] == d["order"]["size"]
    assert json.loads(str(make_client().preview_best(AB, "yes", 5)))["why"]["spend"] is None


def test_live_spend_sends_one_real_order_sized_for_the_money(pair_live: Any) -> None:  # noqa: F811
    make, kalshi, api, _ = pair_live
    # YES asks: Kalshi 0.40, Polymarket US 0.57. $5 buys more on Kalshi.
    r = make().buy_best(LIVE, "yes", spend=5)
    k, pm = r.why.venues
    assert k.size > pm.size and k.all_in <= 5 and pm.all_in <= 5
    assert (r.why.reason_code, r.order.venue, r.order.mode, r.order.filled) == (
        "wins_more",
        "kalshi",
        "live",
        k.size,
    )
    assert posts(kalshi, api) == (1, 0)
