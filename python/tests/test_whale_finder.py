"""Finding Polymarket wallets: every leaderboard and markets of every size, against fixed answers (no network)."""

from __future__ import annotations

import random
from typing import Any

import httpx
from v1_compat import v1_compat

from uselayer.http import Http
from uselayer.layer_api import LayerApi
from uselayer.whale_finder import MARKET_BANDS, all_boards
from uselayer.whales import Whales

DATA = "data-api.polymarket.com"
GAMMA = "gamma-api.polymarket.com"


def w(i: int) -> str:
    return f"0x{i:040x}"


def finder(
    boards: dict[tuple[str, str, str], int] | None = None,
    markets: list[dict[str, Any]] | None = None,
    trades: dict[str, list[dict[str, Any]]] | None = None,
    fail: set[str] | None = None,
    broken: set[tuple[str, str, str]] | None = None,
    page: int = 100,
) -> tuple[Whales, list[httpx.Request]]:
    """A Whales on fake Polymarket answers. ``boards`` gives how many wallets each leaderboard has (wallet i at
    rank i + 1; a board not given is empty); ``markets`` the open markets, busiest first, listed ``page`` at a
    time; ``trades`` each market's trades. Paths in ``fail`` and boards in ``broken`` answer 500."""
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        q, p = r.url.params, r.url.path
        if p in (fail or set()):
            return httpx.Response(500, json={"error": "down"})
        if (r.url.host, p) == (DATA, "/v1/leaderboard"):
            key = (q["orderBy"], q["timePeriod"], q.get("category", "OVERALL"))
            if key in (broken or set()):
                return httpx.Response(500, json={"error": "down"})
            n, off, lim = (boards or {}).get(key, 0), int(q.get("offset", 0)), int(q["limit"])
            off = min(off, 10_000)  # past this Polymarket repeats its last page
            return httpx.Response(200, json=[
                {"rank": str(i + 1), "proxyWallet": w(i), "userName": f"user{i}" if i % 2 else w(i)}
                for i in range(off, min(n, off + lim))
            ])  # fmt: skip
        if (r.url.host, p) == (GAMMA, "/markets/keyset"):
            start = int(q.get("after_cursor", 0))
            rows = (markets or [])[start : start + page]
            more = start + page < len(markets or [])
            return httpx.Response(
                200, json={"markets": rows, "next_cursor": str(start + page) if more else None}
            )
        if (r.url.host, p) == (DATA, "/trades"):
            return httpx.Response(200, json=(trades or {}).get(q["market"], []))
        return httpx.Response(404, json={"error": "not found"})

    http = Http(
        transport=httpx.MockTransport(v1_compat(handler)), sleep=lambda s: None, limits={}
    )  # no pacing
    return Whales(http, LayerApi(None, http)), seen


def market(i: int, vol: float, tag: str = "Sports") -> dict[str, Any]:
    return {"conditionId": f"0xc{i}", "question": f"Market {i}?", "volume24hr": vol, "tags": [{"label": tag}]}


def trade(wallet: str, usd: float, name: str | None = None) -> dict[str, Any]:
    return {"proxyWallet": wallet, "size": usd * 2, "price": 0.5, "name": name or wallet}


def test_every_leaderboard_is_read_each_ordering_period_and_category() -> None:
    assert len(all_boards()) == 2 * 4 * 11
    wh, seen = finder()
    assert list(wh.find_wallets(markets=False)) == []
    asked = {
        (r.url.params["orderBy"], r.url.params["timePeriod"], r.url.params.get("category")) for r in seen
    }
    assert len(asked) == 88
    assert ("PNL", "MONTH", None) in asked  # overall is the board without a category
    assert ("VOL", "DAY", "MENTIONS") in asked


def test_a_board_is_paged_to_its_end_with_each_wallets_rank_name_and_category() -> None:
    wh, seen = finder({("PNL", "MONTH", "POLITICS"): 120})
    got = list(wh.find_wallets(markets=False, boards=[("PNL", "MONTH", "POLITICS")]))
    assert len(got) == 120 and len(seen) == 3  # 50 + 50 + 20: a short page is the last
    first, second = got[0], got[1]
    assert (first.wallet, first.source, first.where, first.rank) == (
        w(0),
        "leaderboard",
        "PNL MONTH POLITICS",
        1,
    )
    assert first.name is None and second.name == "user1"  # no username: Polymarket sends the wallet
    assert first.category == "Politics"


def test_a_board_stops_at_polymarkets_depth_instead_of_reading_its_repeated_last_page() -> None:
    wh, seen = finder({("PNL", "ALL", "OVERALL"): 50_000})
    got = list(wh.find_wallets(markets=False, boards=[("PNL", "ALL", "OVERALL")]))
    assert len(seen) == 201 and len(got) == 201 * 50  # offsets 0 to 10,000: ranks 1 to 10,050
    assert len({s.wallet for s in got}) == len(got)
    assert got[0].category is None


def test_a_board_that_fails_is_skipped_and_the_rest_are_still_read() -> None:
    wh, _ = finder(
        {("PNL", "WEEK", "OVERALL"): 10, ("VOL", "WEEK", "OVERALL"): 5}, broken={("PNL", "WEEK", "OVERALL")}
    )
    got = list(
        wh.find_wallets(markets=False, boards=[("PNL", "WEEK", "OVERALL"), ("VOL", "WEEK", "OVERALL")])
    )
    assert [s.where for s in got] == ["VOL WEEK OVERALL"] * 5


def test_markets_are_picked_from_every_band_of_activity_not_only_the_busiest() -> None:
    # 30 markets in each band, busiest first, then 30 too quiet to read
    vols = [v * 2 for v in MARKET_BANDS for _ in range(30)] + [1.0] * 30
    markets = [market(i, v) for i, v in enumerate(vols)]
    trades = {m["conditionId"]: [trade(w(i), 10)] for i, m in enumerate(markets)}
    wh, seen = finder(markets=markets, trades=trades, page=40)
    got = list(wh.find_wallets(leaderboards=False, per_band=4))
    assert len(got) == 4 * len(MARKET_BANDS)
    picked_vols = sorted({vols[int(s.where[3:])] for s in got}, reverse=True)
    assert picked_vols == [v * 2 for v in MARKET_BANDS]  # 4 from each band
    assert all(vols[int(s.where[3:])] >= MARKET_BANDS[-1] for s in got)  # none below the lowest band
    # stops listing once a page goes below the lowest band: the 4th page of 40 does
    assert sum(1 for r in seen if r.url.path == "/markets/keyset") == 4


def test_a_wallet_trading_a_market_several_times_is_one_sighting_with_its_trades_and_dollars() -> None:
    m = market(1, 500, tag="Politics")
    wh, _ = finder(markets=[m], trades={"0xc1": [trade(w(7), 10), trade(w(7), 30, "amy"), trade(w(8), 5)]})
    got = {s.wallet: s for s in wh.find_wallets(leaderboards=False, per_band=5)}
    assert set(got) == {w(7), w(8)}
    amy = got[w(7)]
    assert (amy.source, amy.where, amy.trades, amy.usd, amy.name) == ("market", "0xc1", 2, 40.0, "amy")
    assert (amy.category, amy.title) == ("Politics", "Market 1?")
    assert got[w(8)].name is None


def test_a_market_that_cant_be_read_is_skipped() -> None:
    wh, _ = finder(markets=[market(1, 500)], fail={"/trades"})
    assert list(wh.find_wallets(leaderboards=False)) == []


def test_picking_markets_at_random_can_be_repeated_with_a_seed() -> None:
    from uselayer.whale_finder import pick_markets

    markets = [market(i, 50) for i in range(100)]
    wh, _ = finder(markets=markets)
    a = [m["conditionId"] for m in pick_markets(wh._poly, per_band=10, rng=random.Random(1))]
    b = [m["conditionId"] for m in pick_markets(wh._poly, per_band=10, rng=random.Random(1))]
    assert a == b and len(a) == 10


def test_per_second_holds_the_finder_to_that_rate() -> None:
    clock = {"t": 0.0}
    waits: list[float] = []

    def sleep(s: float) -> None:
        waits.append(s)
        clock["t"] += s

    wh, _ = finder({("PNL", "DAY", "OVERALL"): 500})
    wh._poly._http._clock = lambda: clock["t"]  # type: ignore[method-assign]
    wh._poly._http._sleep = sleep  # type: ignore[method-assign]
    list(wh.find_wallets(markets=False, boards=[("PNL", "DAY", "OVERALL")], per_second=2))
    assert round(clock["t"], 6) == 4.5  # 11 reads at 2 a second, the first two at once (a bucket of 2)
