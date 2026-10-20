"""whales.preview_kalshi, offline: a buy priced on Kalshi's public book (no key), its taker fee per level, the
limit from the cheapest ask, and each way it says no."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from uselayer import WhaleTrade
from uselayer.fees import dollars, kalshi_fee
from uselayer.http import Http
from uselayer.layer_api import LayerApi
from uselayer.whales import Whales

API = "/trade-api/v2"
TICKER = "KXNBAGAME-26OCT20BOSDET-DET"

# YES asks are 1 − each NO bid: 0.40 × 10 and 0.42 × 100 within 3¢ of the cheapest, 0.50 × 1000 past it.
NO_BIDS = [["0.5000", "1000.00"], ["0.5800", "100.00"], ["0.6000", "10.00"]]
YES_BIDS = [["0.3500", "50.00"], ["0.3800", "20.00"]]  # NO asks 0.62 × 20, 0.65 × 50


def trade() -> WhaleTrade:
    return WhaleTrade(venue="polymarket", trader="0xabc", name="them", market="0xc1:0", side="yes", action="buy",
                      price=0.31, size=100, usd=31, at=datetime(2026, 10, 20, tzinfo=UTC), trade_id="t1")  # fmt: skip


def fake(
    status: str = "active", multiplier: str = "1", yes: Any = YES_BIDS, no: Any = NO_BIDS, found: bool = True
):
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        p = r.url.path
        if p == f"{API}/markets/{TICKER}":
            if not found:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json={"market": {
                "ticker": TICKER, "event_ticker": "KXNBAGAME-26OCT20BOSDET", "status": status,
                "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}})  # fmt: skip
        if p == f"{API}/events/KXNBAGAME-26OCT20BOSDET":
            return httpx.Response(200, json={"event": {"series_ticker": "KXNBAGAME"}})
        if p == f"{API}/series/KXNBAGAME":
            return httpx.Response(
                200, json={"series": {"fee_type": "quadratic", "fee_multiplier": multiplier}}
            )
        if p == f"{API}/markets/{TICKER}/orderbook":
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}})
        return httpx.Response(404, json={})

    w = Whales(Http(transport=httpx.MockTransport(handler), sleep=lambda s: None), LayerApi(None, Http()))
    return w, seen


def fee(n: float, p: float, m: float = 1.0) -> float:
    return dollars(kalshi_fee(contracts=n, price=p, rate=0.07, multiplier=m))


def test_spend_buys_whole_contracts_up_the_book_with_kalshis_fee_at_each_level_and_no_key() -> None:
    w, seen = fake()
    p = w.preview_kalshi(trade(), TICKER, "yes", spend=10)
    # 10 at 0.40 = $4.17 with the fee; 13 more at 0.42 = $5.69; a 14th would cross $10
    assert p.ok and (p.venue, p.market, p.side, p.spend) == ("kalshi", TICKER, "yes", 10)
    assert p.contracts == 23 and p.best_price == 0.40 and p.worst_price == 0.42 and p.limit == 0.43
    assert p.cost == pytest.approx(4.0 + 13 * 0.42)
    assert p.fee == pytest.approx(fee(10, 0.40) + fee(13, 0.42))
    assert p.total == pytest.approx(p.cost + p.fee) and p.total <= 10
    assert 4.0 + 14 * 0.42 + fee(10, 0.40) + fee(14, 0.42) > 10
    assert (
        p.price == pytest.approx(p.cost / 23)
        and p.payout == 23
        and p.profit_if_win == pytest.approx(23 - p.total)
    )
    assert p.max_spend == pytest.approx(round(4.0 + 42.0 + fee(10, 0.40) + fee(100, 0.42), 2))
    assert p.depth_checked and "on Kalshi" in p.reason
    assert all(not any(h.lower().startswith("kalshi-access") for h in r.headers) for r in seen)


def test_no_is_priced_from_the_yes_bids_and_contracts_default_to_five() -> None:
    w, _ = fake()
    p = w.preview_kalshi(trade(), TICKER, "no")
    assert (
        p.ok
        and p.side == "no"
        and p.contracts == 5
        and p.price == pytest.approx(0.62)
        and p.best_price == 0.62
    )
    assert p.fee == pytest.approx(fee(5, 0.62)) and p.limit == 0.65


def test_the_series_fee_multiplier_counts_and_is_read_once_per_event() -> None:
    w, seen = fake(multiplier="0.5")
    p = w.preview_kalshi(trade(), TICKER, "yes", size=10)
    assert p.fee == pytest.approx(fee(10, 0.40, 0.5))
    first = len(seen)
    w.preview_kalshi(trade(), TICKER, "yes", size=10)
    assert first == 4 and len(seen) - first == 2  # market + book; the event and series once


def test_more_than_the_book_holds_within_the_limit_says_what_would_fill() -> None:
    w, _ = fake()
    p = w.preview_kalshi(trade(), TICKER, "yes", spend=1000)
    assert not p.ok and p.max_spend is not None and p.max_spend < 1000 and p.best_price == 0.40
    assert f"Only ${p.max_spend:,.2f} is for sale on Kalshi at 0.43" in p.reason
    q = w.preview_kalshi(trade(), TICKER, "yes", size=500)
    assert not q.ok and q.contracts == 500 and "110 contracts" in q.reason


def test_too_little_to_buy_one_contract() -> None:
    w, _ = fake()
    p = w.preview_kalshi(trade(), TICKER, "yes", spend=0.30)
    assert not p.ok and "doesn't buy one contract on Kalshi" in p.reason and p.best_price == 0.40


@pytest.mark.parametrize(
    ("kw", "said"),
    [
        ({"status": "closed"}, "Kalshi isn't taking orders on this market now."),
        ({"found": False}, "Kalshi doesn't list this market any more."),
        ({"no": []}, "Nothing is for sale on Kalshi on this side right now."),
    ],
)
def test_says_no_plainly(kw: dict[str, Any], said: str) -> None:
    w, _ = fake(**kw)
    p = w.preview_kalshi(trade(), TICKER, "yes", spend=10)
    assert not p.ok and p.reason == said and p.venue == "kalshi"


def test_refuses_what_it_cant_price() -> None:
    w, _ = fake()
    with pytest.raises(ValueError):
        w.preview_kalshi(trade(), TICKER, "maybe")
    with pytest.raises(ValueError):
        w.preview_kalshi(trade(), TICKER, "yes", size=1, spend=1)
    with pytest.raises(ValueError):
        w.preview_kalshi(trade(), TICKER, "yes", spend=0)
