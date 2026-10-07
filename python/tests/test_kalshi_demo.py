"""The full Kalshi order lifecycle on Kalshi's DEMO exchange (mock money).

    KALSHI_DEMO_E2E=1 KALSHI_DEMO_KEY_ID=... KALSHI_DEMO_PRIVATE_KEY_PATH=... uv run pytest tests/test_kalshi_demo.py -s

It refuses any host but the demo one and spends at most about 10¢ of mock money: one contract bought at
the ask and sold back at the bid. ``scripts/prove_kalshi_live.py`` runs the same lifecycle through
``Client(mode="live")`` and its guardrails.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from uselayer import Kalshi, Order
from uselayer.http import Http
from uselayer.venues.kalshi import KalshiLive

pytestmark = pytest.mark.skipif(
    os.environ.get("KALSHI_DEMO_E2E") != "1", reason="set KALSHI_DEMO_E2E=1 to run on Kalshi's demo"
)


@pytest.fixture(scope="module")
def demo() -> Iterator[KalshiLive]:
    key = Kalshi(
        key_id=os.environ["KALSHI_DEMO_KEY_ID"],
        private_key_path=os.environ["KALSHI_DEMO_PRIVATE_KEY_PATH"],
        environment="demo",
    )
    a = KalshiLive(Http(), key)
    assert a._base.startswith("https://demo-api.kalshi.co/"), "demo only"
    yield a


# Kalshi bars residents of some states from opening positions in some categories (sports, elections,
# politics, culture, tech and science, mentions), so the test stays with crypto, indexes, weather and gas.
ALLOWED_SERIES = ("KXBTC", "KXETH", "KXNASDAQ", "KXINX", "KXRAIN", "KXHIGH", "KXAAAGAS")


def liquid_market(a: KalshiLive) -> dict[str, Any]:
    """An open market in an allowed series with a YES ask of at most 10¢, 5+ contracts, a bid, closing 2+ hours out."""
    soon = datetime.now(UTC) + timedelta(hours=2)
    # Demo money sits on one exchange shard; a market on another shard answers "insufficient shard balance".
    funded = {
        b["exchange_index"] for b in a.balance().raw.get("balance_breakdown") or [] if float(b["balance"]) > 1
    }
    held = {p.market for p in a.positions() if not p.settled and p.contracts > 0}
    cursor = None
    for _ in range(40):
        params: dict[str, Any] = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        body = a._call("GET", "/markets", params=params)
        for m in body.get("markets") or []:
            ask, bid = float(m.get("yes_ask_dollars") or 1), float(m.get("yes_bid_dollars") or 0)
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            allowed = (
                str(m.get("ticker", "")).startswith(ALLOWED_SERIES)
                and m.get("exchange_index", 0) in funded
                and m["ticker"] not in held
            )
            liquid = 0.02 <= ask <= 0.10 and float(m.get("yes_ask_size_fp") or 0) >= 5 and bid >= 0.01
            if allowed and liquid and close > soon:
                # The listing can lag the book: check the live book has both sides, with room to sell back.
                live = a.read_book(m["ticker"]).book.outcome("yes")
                if (
                    live.best_ask
                    and live.best_bid
                    and live.best_ask.price <= 0.10
                    and live.best_bid.size >= 5
                ):
                    return dict(m)
        cursor = body.get("cursor")
        if not cursor:
            break
    pytest.skip("no liquid demo market right now")


def _live(o: Order) -> Order:
    return o.model_copy(update={"id": o.client_id, "mode": "live", "created_at": datetime.now(UTC)})


def test_full_lifecycle_on_demo(demo: KalshiLive) -> None:
    log: list[str] = []
    start = demo.balance().cash
    log.append(f"balance ${start:.4f}, key {demo.key_kind}")
    m = liquid_market(demo)
    t = m["ticker"]
    info = demo.market(t)
    log.append(f"market {t}: tick {info.tick_size}, fees x{info.fees.multiplier} {info.fees.fee_type}")

    # 1. A resting order far below the market: place, find by client id, list, cancel.
    expires = datetime.now(UTC) + timedelta(minutes=5)
    rest = _live(
        Order(
            venue="kalshi",
            market=t,
            side="yes",
            price=info.tick_size,
            size=1,
            tif="gtc",
            post_only=True,
            expires_at=expires,
        )
    )
    placed, fills = demo.place(rest)
    assert placed.status == "open" and placed.venue_order_id and fills == []
    log.append(f"placed resting {placed.venue_order_id}: {placed.status}")
    found = demo.find(rest, since=datetime.now(UTC) - timedelta(minutes=1))
    assert found is not None and found.venue_order_id == placed.venue_order_id
    assert placed.venue_order_id in [o.venue_order_id for o in demo.open_orders()]
    canceled = demo.cancel(placed)
    assert canceled.status == "canceled"
    log.append(f"canceled {canceled.venue_order_id}: {canceled.status}")

    # 2. Cancel-all: both resting orders end up canceled at the venue.
    two = []
    for _ in range(2):
        o = Order(
            venue="kalshi",
            market=t,
            side="yes",
            price=info.tick_size,
            size=1,
            tif="gtc",
            post_only=True,
            expires_at=expires,
        )
        two.append(demo.place(_live(o))[0])
    demo.cancel_all()
    states: list[str] = []
    for _ in range(20):  # Kalshi applies cancel-all within moments, not instantly
        states = [demo.refresh(o)[0].status for o in two]
        if states == ["canceled", "canceled"] and not demo.open_orders():
            break
        time.sleep(0.5)
    assert states == ["canceled", "canceled"] and demo.open_orders() == []
    log.append("cancel-all: both resting orders canceled, 0 open")

    # 3. A fill: buy 1 YES at the ask (IOC), read the real fill and fee, see the position.
    ask = demo.read_book(t).book.outcome("yes").best_ask
    assert ask is not None and ask.price <= 0.10
    bought, bfills = demo.place(_live(Order(venue="kalshi", market=t, side="yes", price=ask.price, size=1)))
    assert bought.status == "filled" and bought.filled == 1 and bfills, bought
    f = bfills[0]
    log.append(
        f"filled: {f.contracts} YES @ {f.price}, Kalshi fee ${f.fee} (Layer estimate ${f.fee_estimate})"
    )
    pos = [p for p in demo.positions() if p.market == t and not p.settled]
    assert pos and pos[0].side == "yes" and pos[0].contracts == 1
    log.append(f"position: {pos[0].contracts} {pos[0].side}")

    # 4. Close it again at the bid.
    bid = demo.read_book(t).book.outcome("yes").best_bid
    assert bid is not None
    sell = Order(venue="kalshi", market=t, side="yes", action="sell", price=bid.price, size=1)
    sold, sfills = demo.place(_live(sell))
    log.append(f"sold back: {sold.status} @ {sold.avg_price}, fee ${sum(x.fee for x in sfills):.4f}")
    end = demo.balance().cash
    log.append(f"balance ${end:.4f} (change ${end - start:+.4f})")
    print("\n" + "\n".join("  ✓ " + line for line in log))
    assert sold.status == "filled"
