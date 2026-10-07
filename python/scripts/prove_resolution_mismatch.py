"""A paper pair through to a resolution mismatch, with made-up markets and made-up settlements.

    uv run python scripts/prove_resolution_mismatch.py

Two made-up Polymarket US markets that are meant to be the same bet are served from this script
(nothing touches the network). The paper client quotes the pair, trades it (YES on one, NO on the
other) and then each "venue" settles its market: one NO, the other YES, so both legs lose. The
paper client finds those settlements itself, the way it does against the real venue, flags the pair,
and pnl() breaks the loss out of realized (already inside it). Then it checks the numbers by hand.
"""

from __future__ import annotations

import email.utils
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from uselayer import Client

NOW = [datetime(2026, 10, 5, 12, 0, tzinfo=UTC)]
BOOKS = {  # slug -> (YES bids, YES asks), made up
    "made-up-team-wins": ([(0.40, 200)], [(0.42, 200)]),
    "made-up-team-wins-twin": ([(0.60, 200)], [(0.62, 200)]),
}
SETTLED: dict[str, float] = {}  # slug -> what one YES contract paid; 404 until set


def gateway(r: httpx.Request) -> httpx.Response:
    headers = {"content-type": "application/json", "date": email.utils.format_datetime(NOW[0], usegmt=True)}
    path = r.url.path
    if r.url.host != "gateway.polymarket.us":
        return httpx.Response(404, json={"message": "this script only serves its made-up markets"})
    if path == "/v1/markets":
        slug = r.url.params.get("slug")
        ms = [
            {
                "slug": slug,
                "question": f"Made up: {slug}",
                "active": True,
                "closed": False,
                "status": "MARKET_STATUS_OPEN",
                "orderPriceMinTickSize": 0.01,
                "minimumTradeQty": 1,
                "feeCoefficient": 0.0695,
                "gameStartTime": "2026-10-06T00:00:00Z",
                "endDate": "2026-10-07T00:00:00Z",
            }
        ]
        return httpx.Response(200, json={"markets": ms if slug in BOOKS else []}, headers=headers)
    slug = path.split("/")[3]
    if path.endswith("/settlement"):
        if slug not in SETTLED:
            return httpx.Response(404, json={"code": 5, "message": "market not found or not settled"})
        return httpx.Response(200, json={"slug": slug, "settlement": SETTLED[slug]}, headers=headers)
    bids, asks = BOOKS[slug]
    lv = lambda p, q: {"px": {"value": f"{p:.4f}", "currency": "USD"}, "qty": f"{q:.4f}"}  # noqa: E731
    md = {
        "marketSlug": slug,
        "bids": [lv(p, q) for p, q in bids],
        "offers": [lv(p, q) for p, q in asks],
        "state": "MARKET_STATE_OPEN",
        "transactTime": NOW[0].isoformat().replace("+00:00", "Z"),
    }
    return httpx.Response(200, json={"marketData": md}, headers=headers)


def main() -> None:
    alerts: list[dict[str, Any]] = []
    home = Path(tempfile.mkdtemp(prefix="uselayer-mismatch-"))
    c = Client(
        transport=httpx.MockTransport(gateway),
        clock=lambda: NOW[0],
        sleep=lambda s: None,
        store=home / "paper.db",
        on_alert=alerts.append,
    )
    pair = [("polymarket_us", "made-up-team-wins"), ("polymarket_us", "made-up-team-wins-twin")]

    q = c.quote(pair, size=100)
    print(f"[1] quote: {q.contracts} contracts, net ${q.net_profit:.2f} after fees ${q.fees:.2f}")
    print(
        f"    return {q.return_pct}% over {q.days_held} days until {q.settles_at:%Y-%m-%d %H:%M} UTC "
        f"= {q.return_per_day_pct}% a day"
    )

    t = c.trade(pair, size=100, min_edge=0.01)
    print(
        f"[2] trade (paper): {t.status}, {t.hedged:g} hedged, ${t.locked_in:.2f} locked in, group {t.group_id[:8]}"
    )
    paid_in = sum(f.cost + f.fee for f in c.fills())

    NOW[0] += timedelta(days=1)
    SETTLED["made-up-team-wins"] = 0  # venue 1: NO won, so the YES leg lost
    SETTLED["made-up-team-wins-twin"] = 1  # venue 2: YES won, so the NO leg lost too
    print("[3] the venues settle the same bet differently: made-up-team-wins NO, its twin YES")

    p = c.pnl()
    print("[4] pnl():")
    print(
        json.dumps(
            {k: v for k, v in p.to_dict().items() if k not in ("rows", "resolution_mismatches")}, indent=1
        )
    )
    (m,) = c.resolution_mismatches()
    print("[5] client.resolution_mismatches():")
    print(json.dumps(m.to_dict(), indent=1))
    print(f"    alerts: {[a['kind'] for a in alerts if a['kind'] in ('settled', 'resolution_mismatch')]}")

    # By hand: the pair paid nothing, so realized is minus what the contracts cost, net is minus
    # everything paid in, and the loss line says $100 of it (what the 100 hedged contracts were meant
    # to pay) went to the venues settling differently. It's inside realized, not subtracted again.
    cost = round(sum(f.cost for f in c.fills()), 6)
    assert m.kind == "both_lost" and (m.contracts, m.expected, m.paid, m.impact) == (100, 100, 0, -100)
    assert p.resolution_mismatch_loss == 100
    assert p.net == round(-paid_in, 6), (p.net, paid_in)
    assert p.realized == round(0 - cost, 6) == round(sum(r.realized for r in p.rows), 6)
    assert p.net == round(p.realized + p.unrealized - p.fees, 6)
    print(
        f"[6] checked by hand: paid in ${paid_in:.2f}, got back $0.00, net {p.net}; loss line {p.resolution_mismatch_loss}"
    )
    c.close()


if __name__ == "__main__":
    main()
