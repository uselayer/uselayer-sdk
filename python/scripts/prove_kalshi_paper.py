"""Kalshi proof: paper trades on live Kalshi books, a Kalshi ↔ Polymarket US pair, a live order previewed.

    KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... LAYER_API_KEY=lyr_... python scripts/prove_kalshi_paper.py

Nothing is sent to Kalshi but reads signed with your key: paper mode fills against the books.

1. Picks a Kalshi market from Layer's Kalshi ↔ Polymarket US matches whose YES asks have two close
   levels, and buys through both in paper mode. Checks the walk and each fill's fee by hand:
   multiplier × 0.07 × contracts × price × (1 − price), rounded up to the cent.
2. Takes the pair with the best edge in Layer's ``best_trade``, quotes it from both live books with the
   SDK, and trades it in paper through the leg-risk guard. Real books rarely leave an edge; with none,
   the guard sends nothing (``missed``) and leaves no exposure.
3. Builds a live-mode client with only the Kalshi key and previews a Kalshi order: the rules run, nothing
   is sent. (``scripts/prove_kalshi_live.py`` sends real orders, on Kalshi's demo exchange only.) A live
   pair across venues is still refused.
"""

from decimal import ROUND_CEILING, Decimal
from typing import Any

from uselayer import Client, Kalshi, Match, VenueError

key = Kalshi.from_env()
client = Client(store=":memory:", kalshi=key)

matches: list[Match] = []
for offset in range(0, 1000, 200):
    page = client.matches(venue="polymarket_us", limit=200, offset=offset)
    matches += [m for m in page if {"kalshi", "polymarket_us"} <= set(m.markets())]
    if len(page) < 200:
        break
print(f"{len(matches)} Kalshi ↔ Polymarket US matches from Layer")

# 1. A single paper trade on a live Kalshi book.
ticker, asks = "", []
for m in matches:
    t = m.markets()["kalshi"].market_id
    a = client.book(t, venue="kalshi").outcome("yes").asks
    if len(a) >= 2 and a[0].size <= 500 and a[1].price - a[0].price <= 0.03 and 0.05 < a[0].price < 0.95:
        ticker, asks = t, list(a)
        break
if not ticker:
    raise SystemExit("no Kalshi market with two close, small ask levels right now")

info = client.market(ticker, venue="kalshi")
book = client.book(ticker, venue="kalshi")
asks = list(book.outcome("yes").asks)
want = asks[0].size + 1
print(f"\nKalshi {ticker}: tick {info.tick_size}, fees x{info.fees.multiplier} {info.fees.fee_type}")
print(f"  book as of {book.as_of}; YES asks {[(lv.price, lv.size) for lv in asks[:3]]}")
order = client.buy(venue="kalshi", market=ticker, side="yes", price=asks[1].price, size=want)
print(f"  paper order: {order.status}, {order.filled} @ {order.avg_price}, fees ${order.fees}")

mult = Decimal(str(info.fees.multiplier))
fills = client.fills()
assert [(f.price, f.contracts) for f in fills] == [(asks[0].price, asks[0].size), (asks[1].price, 1.0)]
for f in fills:
    p = Decimal(str(f.price))
    exact = mult * Decimal("0.07") * Decimal(str(f.contracts)) * p * (1 - p)
    cents = exact.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    print(f"  fill {f.contracts} @ {f.price}: SDK fee ${f.fee}; by hand {exact:.6f} → ${cents}")
    assert Decimal(str(f.fee)) == cents
print("✓ Kalshi paper fills and fees match the hand check")

# 2. The pair with Layer's best edge, quoted by the SDK from both books and traded in paper.
priced = [m for m in matches if getattr(m, "best_trade", None)]


def layer_edge(m: Match) -> float:
    bt: dict[str, Any] = m.best_trade  # type: ignore[attr-defined]
    return float(bt["net_profit_per_contract"])


pair = max(priced, key=layer_edge) if priced else matches[0]
k, pm = pair.markets()["kalshi"].market_id, pair.markets()["polymarket_us"].market_id
print(f"\npair: kalshi {k} ↔ polymarket_us {pm}")
if priced:
    bt = pair.best_trade  # type: ignore[attr-defined]
    print(
        f"  Layer best_trade: {bt['contracts']} contracts, {bt['net_profit_per_contract']} a contract "
        f"(kalshi {bt['kalshi']['buy']} @ {bt['kalshi']['price']}, "
        f"polymarket_us {bt['polymarket_us']['buy']} @ {bt['polymarket_us']['price']})"
    )
q = client.quote(pair)
if q.a and q.b:
    print(
        f"  SDK quote now: edge at best {q.edge_at_best} a contract after both fees "
        f"({q.a.venue} {q.a.side} @ {q.a.best_price}, {q.b.venue} {q.b.side} @ {q.b.best_price}); "
        f"{q.contracts} contracts clear 0"
    )
t = client.trade(pair, size=5, min_edge=0.0)
print(
    f"  trade (up to 5, min_edge 0): {t.status}, hedged {t.hedged}, locked in ${t.locked_in}, notes {list(t.notes)}"
)
for o in t.orders:
    print(
        f"    {o.venue} {o.action} {o.side} {o.filled}/{o.size} @ {o.avg_price} fees ${o.fees} ({o.status})"
    )
assert t.status in ("hedged", "missed", "unwound", "exposed")
if t.status == "missed":
    assert t.exposure is None and not [o for o in t.orders if o.filled]
print("✓ Kalshi ↔ Polymarket US pair ran in paper through the leg-risk guard")

# 3. Live mode with only the Kalshi key: preview a Kalshi order (sends nothing); a live pair is refused.
live = Client(mode="live", kalshi=key, store=":memory:", on_alert=lambda e: None)
p = live.preview(live.order(venue="kalshi", market=ticker, side="yes", price=asks[0].price, size=1))
print(f"\nlive Kalshi preview: allowed={p.allowed}, fees ${p.fees}, problems {list(p.problems)}")
assert not any("switched off" in x for x in p.problems), p.problems
print("✓ live Kalshi orders pass to the rules (previewed, nothing sent)")
try:
    live.trade(pair, size=1, min_edge=0.0)
    raise SystemExit("a live cross-venue pair was not refused")
except VenueError as e:
    assert e.code == "not_available", e
    print(f"✓ live pairs across venues stay off: {e.message}")
