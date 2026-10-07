"""The live proof of roadmap 11.2's Polymarket US leg: REAL Polymarket US + Kalshi's DEMO exchange.

REAL MONEY on Polymarket US (at most $1, hard-coded). Kalshi's leg is on Kalshi's demo exchange
(mock money). The owner runs this from their own terminal; it asks before any order.

    KALSHI_ENV=demo \\
    KALSHI_KEY_ID=$(getkey kalshi-demo-key-id) \\
    KALSHI_PRIVATE_KEY="$(getkey kalshi-demo-private-key | base64 -d)" \\
    POLYMARKET_US_KEY_ID=$(getkey polymarket-us-key-id) \\
    POLYMARKET_US_SECRET_KEY=$(getkey polymarket-us-secret-key) \\
    uv run python scripts/prove_live_pair_real.py

Keys come from the environment only; nothing is written to a file. ``--dry-run`` picks the markets
and prints the plan, then stops (reads only, no live client). ``--polymarket-us SLUG`` and
``--kalshi TICKER`` pick the markets yourself.

Polymarket US has no demo exchange, so this pairs a cheap real Polymarket US side (ask at most 15¢,
spread at most 2¢) with a cheap Kalshi demo side, priced so the pair clears ``min_edge`` and
``trade()`` runs. It isn't a real arbitrage: the point is the real Polymarket US leg.

1. Hedged: ``trade()`` buys 1 contract on each venue, Polymarket US first (its book is thinner).
   Records Polymarket US's real fill, the fee it billed and its order's own fee field.
2. Sell-back: ``trade()`` again, and the kill switch is pressed the moment the Polymarket US leg
   fills, so the Kalshi leg is never sent and the leg-risk guard sells the Polymarket US contract back
   for real (``"unwound"``).
3. Clean-up: run 1's contracts are sold back at the bid (Polymarket US real, Kalshi demo).
4. Cancels this run's resting orders only (never ``cancel_all()``: that would cancel every order on
   the account), runs ``client.reconcile()`` (reads only) for both venues, prints everything and
   saves it under ``~/uselayer-proof/live-pair-real/<time>/``. Other positions and orders on the
   account are never touched, and a Polymarket US market the account already holds is never picked.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PM_CAP = 1.0  # dollars of real money: every Polymarket US buy this run makes, fees included
MAX_PM_PRICE = 0.15
MAX_SPREAD = 0.02
SIZE = 1  # contracts a leg
MIN_EDGE = 0.01
MAX_UNWIND_LOSS = 0.05
RULES: dict[str, Any] = {
    "budget": 5,
    "max_daily_loss": {"amount": 3},
    "max_position": {"per_market": 3},
}
# Kalshi bars residents of some states from some categories, so stay with crypto, indexes, weather and gas.
KALSHI_SERIES = ("KXBTC", "KXETH", "KXNASDAQ", "KXINX", "KXRAIN", "KXHIGH", "KXAAAGAS")
OUT = Path.home() / "uselayer-proof" / "live-pair-real"


# ---- the decisions, kept apart so tests can check them without a venue ----


def other_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def cheap_side(book: Any, *, max_price: float = MAX_PM_PRICE, max_spread: float = MAX_SPREAD) -> str | None:
    """The side of ``book`` to buy: ask at most ``max_price``, a bid within ``max_spread`` (to sell back)."""
    best: tuple[float, str] | None = None
    for side in ("yes", "no"):
        o = book.outcome(side)
        a, b = o.best_ask, o.best_bid
        if a is None or b is None or a.size < SIZE or b.size < SIZE:
            continue
        cheap = 0.01 <= a.price <= max_price + 1e-9 and a.price - b.price <= max_spread + 1e-9
        if cheap and (best is None or a.price < best[0]):
            best = (a.price, side)
    return best[1] if best else None


def worst_case(pm_price: float, pm_fee: float) -> float:
    """The most real money this run can lose: both Polymarket US buys left open and settling at $0."""
    return round(2 * SIZE * (pm_price + pm_fee), 6)


def pm_bought(fills: Iterable[Any]) -> float:
    """Real dollars spent on Polymarket US buys, fees included."""
    return round(
        sum(f.price * f.contracts + f.fee for f in fills if f.venue == "polymarket_us" and f.action == "buy"),
        6,
    )


def pm_net(fills: Iterable[Any]) -> float:
    """Real dollars out (positive) or in on Polymarket US: buys and fees minus sales."""
    out = 0.0
    for f in fills:
        if f.venue != "polymarket_us":
            continue
        out += (f.price * f.contracts if f.action == "buy" else -f.price * f.contracts) + f.fee
    return round(out, 6)


def held(fills: Iterable[Any], venue: str, market: str, side: str) -> float:
    """Contracts this run still holds on one side of one market, from its own fills."""
    n = 0.0
    for f in fills:
        if (f.venue, f.market, f.side) == (venue, market, side):
            n += f.contracts if f.action == "buy" else -f.contracts
    return round(n, 6)


def own_open_orders(orders: Iterable[Any]) -> list[Any]:
    """The open orders in this run's store: a new store per run, so every one of them is this run's.
    The only orders the script may cancel (never ``cancel_all()``, which cancels the whole account's)."""
    return [o for o in orders if o.status in ("open", "pending")]


def first_leg_hook(client: Any, fn: Callable[[Any], None]) -> Callable[[], None]:
    """Run ``fn(order)`` once, right after the pair's first buy fills. Returns a function that undoes it."""
    orig = client._execute
    state = {"done": False}

    def wrapped(order: Any, checked: bool = False) -> Any:
        r = orig(order, checked=checked)
        if not state["done"] and order.action == "buy" and r.filled:
            state["done"] = True
            fn(r)
        return r

    client._execute = wrapped

    def undo() -> None:
        client._execute = orig

    return undo


MAX_KALSHI_PRICE = 0.70  # demo money; with Polymarket US at most 15¢ the pair still clears min_edge


def _rough_ok(m: dict[str, Any], side: str) -> bool:
    """Kalshi's market-list prices, before reading a book: the side's ask is 2–70¢."""
    yes_ask, yes_bid = float(m.get("yes_ask_dollars") or 1), float(m.get("yes_bid_dollars") or 0)
    ask = yes_ask if side == "yes" else 1 - yes_bid
    return 0.02 <= ask <= MAX_KALSHI_PRICE


def deeper_first(candidates: Iterable[tuple[str, float]], pm_size: float) -> tuple[str, bool] | None:
    """The Kalshi market to use: the first deeper than Polymarket US's ask (so Polymarket US goes first,
    the thinner leg), else the deepest. Returns (ticker, Polymarket US goes first)."""
    best: tuple[float, str] | None = None
    for ticker, size in candidates:
        if size > pm_size:
            return ticker, True
        if best is None or size > best[0]:
            best = (size, ticker)
    return (best[1], False) if best else None


# ---- picking the markets (reads only) ----


def _pick_pm(reader: Any, args: argparse.Namespace, skip: set[str]) -> tuple[str, str]:
    from uselayer import VenueError

    pub = reader._venues["polymarket_us"]
    slugs = [args.polymarket_us] if args.polymarket_us else []
    if not slugs:
        soon = datetime.now(UTC) + timedelta(hours=1)
        for offset in range(0, args.max_markets, 100):
            ms = reader.markets(limit=100, offset=offset)
            if not ms:
                break
            for m in ms:
                end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00")) if m.end_date else None
                if m.open and (end is None or end > soon):
                    slugs.append(m.market)
    for slug in slugs[: args.max_markets]:
        if slug.split(":")[0] in skip:
            continue  # the account already holds it
        try:
            # The public book, without waiting for a fresh copy: screening only.
            book = pub.read_book(slug).book
        except VenueError:
            continue
        side = cheap_side(book)
        if side:
            return slug, side
    raise SystemExit("No open Polymarket US market has a side at most 15¢ with a 2¢ spread right now.")


def _kalshi_candidates(reader: Any, side: str, pm_size: float) -> Iterable[tuple[str, float]]:
    """Funded, open demo markets (closing 30+ minutes out) whose ``side`` ask is 2–70¢, with its size."""
    k = reader._venues["kalshi"]
    funded = {
        b["exchange_index"] for b in k.balance().raw.get("balance_breakdown") or [] if float(b["balance"]) > 1
    }
    soon = datetime.now(UTC) + timedelta(minutes=30)
    cursor = None
    looked = 0
    for _ in range(40):
        params: dict[str, Any] = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        body = k._call("GET", "/markets", params=params)
        for m in body.get("markets") or []:
            ticker = str(m.get("ticker", ""))
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            ok = ticker.startswith(KALSHI_SERIES) and m.get("exchange_index", 0) in funded and close > soon
            if not ok or not _rough_ok(m, side) or looked >= 60:
                continue
            looked += 1
            ask = reader.book(ticker, venue="kalshi").outcome(side).best_ask
            if ask is not None and 0.02 <= ask.price <= MAX_KALSHI_PRICE and ask.size >= SIZE:
                yield ticker, ask.size
                if ask.size > pm_size:
                    return
        cursor = body.get("cursor")
        if not cursor:
            return


def _pick_kalshi(reader: Any, side: str, pm_size: float, args: argparse.Namespace) -> tuple[str, bool]:
    """The Kalshi demo market, and whether Polymarket US will go first (its ask is the thinner)."""
    if args.kalshi:
        size = reader.book(args.kalshi, venue="kalshi").outcome(side).best_ask
        return str(args.kalshi), size is not None and size.size > pm_size
    got = deeper_first(_kalshi_candidates(reader, side, pm_size), pm_size)
    if got is None:
        raise SystemExit("No funded Kalshi demo market fits right now; try again later.")
    return got


# ---- running it ----


def _plan(pair: Sequence[tuple[str, str]], q: Any) -> str:
    lines = [f"Pair: {pair[0][0]} {pair[0][1]} (DEMO)  ↔  {pair[1][0]} {pair[1][1]} (REAL)"]
    for leg in (q.a, q.b):
        tag = "REAL money" if leg.venue == "polymarket_us" else "demo, mock money"
        lines.append(
            f"  buy {SIZE} {leg.side.upper()} on {leg.venue} {leg.market} ({tag}): ask ${leg.best_price:.4f}, "
            f"cost ${leg.cost:.4f} + fee ${leg.fee:.4f}"
        )
    return "\n".join(lines)


def _show(name: str, t: Any) -> None:
    print(f"\n{name}: {t.status}  hedged={t.hedged} locked_in={t.locked_in} unwind_loss={t.unwind_loss}")
    for o in t.orders:
        print(
            f"  {o.venue:<13} {o.action:<4} {o.side:<3} {o.reason or '':<6} filled {o.filled:g} @ {o.avg_price} "
            f"fees {o.fees} status {o.status} id {o.venue_order_id}"
        )
    if t.exposure:
        print(f"  EXPOSED: {t.exposure.to_dict()}")
    for n in t.notes:
        print(f"  note: {n}")


@dataclass
class RunState:
    """What a run has done so far, so the wrap-up can report it whatever stopped the run."""

    trades: list[Any] = field(default_factory=list)
    cleanup: list[Any] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    venue_order: dict[str, Any] | None = None


def _fills(client: Any, started: datetime) -> list[Any]:
    return list(client.fills(since=started - timedelta(seconds=5)))


def _run_steps(
    client: Any, pair: Sequence[tuple[str, str]], pm_leg: Any, store: str, started: datetime, st: RunState
) -> None:
    from uselayer import Admin, VenueError

    # 1. Hedged.
    try:
        t1 = client.trade(pair, size=SIZE, min_edge=MIN_EDGE, max_unwind_loss=MAX_UNWIND_LOSS)
        st.trades.append(t1)
        _show("Run 1, hedged", t1)
        pm_order = next((o for o in t1.orders if o.venue == "polymarket_us" and o.venue_order_id), None)
        if pm_order is not None:
            # The venue's own order record, read back: its fee field and fill, as Polymarket US reports them.
            raw = client._live["polymarket_us"]._get_order(pm_order.venue_order_id)
            if raw is not None:
                keys = ("state", "cumQuantity", "avgPx", "commissionNotionalTotalCollected", "tif")
                st.venue_order = {k: raw.get(k) for k in keys}
                print(f"  Polymarket US order record: {st.venue_order}")
    except VenueError as e:
        st.errors.append({"step": "run 1", **e.to_dict()})
        print(f"\nRun 1 raised {e.code}: {e.message} (next: {e.next})")

    # 2. Sell-back through the leg-risk guard, if the cap allows another buy.
    if (
        not st.errors
        and pm_bought(_fills(client, started)) + pm_leg.limit_price + pm_leg.fee <= PM_CAP + 1e-9
    ):
        undo = first_leg_hook(client, lambda _: Admin(mode="live", store=store).kill())
        try:
            t2 = client.trade(pair, size=SIZE, min_edge=MIN_EDGE, max_unwind_loss=MAX_UNWIND_LOSS)
            st.trades.append(t2)
            _show("Run 2, kill switch after the first leg", t2)
            if t2.orders and t2.orders[0].venue != "polymarket_us":
                print("  note: Kalshi's leg went first this time, so the sell-back ran on Kalshi demo.")
        except VenueError as e:
            st.errors.append({"step": "run 2", **e.to_dict()})
            print(f"\nRun 2 raised {e.code}: {e.message} (next: {e.next})")
        finally:
            undo()
            Admin(mode="live", store=store).resume()
    else:
        print("\nRun 2 skipped (an error in run 1, or the Polymarket US cap).")

    # 3. Clean-up: sell back what this run still holds, at the bid (only this run's contracts).
    for venue, market in pair:
        for side in ("yes", "no"):
            n = held(_fills(client, started), venue, market, side)
            if n <= 0:
                continue
            try:
                bid = client.book(market, venue=venue).outcome(side).best_bid
                if bid is None:
                    print(f"\nClean-up: no bid to sell {n:g} {side} on {venue} {market}; held to settlement.")
                    continue
                o = client.sell(venue=venue, market=market, side=side, price=bid.price, size=n)
                st.cleanup.append(o)
                print(
                    f"\nClean-up: sold {o.filled:g} {side} on {venue} {market} @ {o.avg_price} (fees {o.fees})"
                )
            except VenueError as e:
                st.errors.append({"step": f"clean-up {venue}", **e.to_dict()})
                print(f"\nClean-up on {venue} raised {e.code}: {e.message}")


def _wrap_up(
    client: Any,
    pair: Sequence[tuple[str, str]],
    q: Any,
    started: datetime,
    st: RunState,
    run_dir: Path,
    alerts: list[dict[str, Any]],
) -> None:
    """Always runs, however the run stopped: cancel this run's open orders, reconcile, print, save."""
    canceled: list[dict[str, Any]] = []
    try:
        # This run's store is new and holds only this run's orders, even from a trade() cut off midway.
        for o in own_open_orders(client.orders(open=True)):
            try:
                canceled.append(client.cancel(o).to_dict())
            except Exception as e:
                st.errors.append({"step": "cancel", "order": o.client_id, "message": repr(e)})
    except Exception as e:
        st.errors.append({"step": "cancel", "message": repr(e)})
    rec: Any = None
    try:
        rec = client.reconcile(since=started)
    except Exception as e:
        st.errors.append({"step": "reconcile", "message": repr(e)})
    try:
        run_fills = _fills(client, started)
    except Exception as e:
        run_fills = []
        st.errors.append({"step": "fills", "message": repr(e)})

    print(f"\nFills this run: {len(run_fills)}")
    for f in run_fills:
        tag = "REAL" if f.venue == "polymarket_us" else "demo"
        print(
            f"  [{tag}] {f.venue} {f.action} {f.side} {f.contracts:g} @ {f.price} "
            f"fee {f.fee} (Layer's formula {f.fee_estimate})"
        )
    print(f"Polymarket US real money: bought ${pm_bought(run_fills):.4f}, net out ${pm_net(run_fills):.4f}")
    for venue, market in pair:
        left = {s: held(run_fills, venue, market, s) for s in ("yes", "no")}
        print(f"Still held from this run on {venue} {market}: {left}")
    print(f"Canceled (this run's open orders only): {len(canceled)}")
    if rec is not None:
        print(f"reconcile (reads only): ok={rec.ok}")
        for m in rec.mismatches:
            print(f"  {m.kind} {m.venue} {m.market}: {m.message}")

    result = {
        "pair": pair,
        "plan": q.to_dict(),
        "trades": [t.to_dict() for t in st.trades],
        "polymarket_us_order_record": st.venue_order,
        "cleanup": [o.to_dict() for o in st.cleanup],
        "fills": [f.to_dict() for f in run_fills],
        "pm_bought": pm_bought(run_fills),
        "pm_net": pm_net(run_fills),
        "canceled": canceled,
        "reconcile": rec.to_dict() if rec is not None else None,
        "errors": st.errors,
        "alerts": alerts,
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"\nsaved: {run_dir / 'result.json'}")


def main(argv: Sequence[str] | None = None) -> int:
    from uselayer import Admin, Client, Kalshi, PolymarketUS

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--polymarket-us", help="the Polymarket US market (slug)")
    ap.add_argument("--kalshi", help="the Kalshi demo ticker")
    ap.add_argument("--max-markets", type=int, default=300, help="Polymarket US markets to look at")
    ap.add_argument("--dry-run", action="store_true", help="pick and print the plan, then stop (reads only)")
    args = ap.parse_args(argv)

    kalshi = Kalshi.from_env()
    if kalshi.environment != "demo":
        print("Kalshi's leg must be on the demo exchange: set KALSHI_ENV=demo with the kalshi-demo-* key.")
        return 2
    run_dir = OUT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    # Never pick a Polymarket US market the account already holds: those positions aren't this run's.
    pm = None if args.dry_run else PolymarketUS.from_env()
    skip: set[str] = set()
    if pm is not None:
        probe = Client(mode="live", kalshi=kalshi, polymarket_us=pm, store=str(run_dir / "probe.db"))
        live_pm = probe._live["polymarket_us"]
        skip = {p.market.split(":")[0] for p in live_pm.positions() if not p.settled}
        others = len(live_pm.open_orders())
        probe.close()
        if others:
            print(f"Polymarket US shows {others} open order(s) on the account that this run didn't place.")
            print("They're never touched. If another run is going, wait for it to finish first.")

    print("Picking markets (reads only)…")
    reader = Client(kalshi=kalshi, store=str(run_dir / "reader.db"))
    slug, pm_side = _pick_pm(reader, args, skip)
    pm_ask = reader.book(slug).outcome(pm_side).best_ask  # type: ignore[arg-type]
    if pm_ask is None:
        print(f"{slug} lost its {pm_side} ask. Nothing was sent.")
        return 1
    ticker, pm_first = _pick_kalshi(reader, other_side(pm_side), pm_ask.size, args)
    order_note = (
        "Polymarket US goes first (its ask is thinner), so run 2 sells it back through the leg-risk guard."
        if pm_first
        else "Kalshi's demo ask is the thinner, so Kalshi goes first: run 2's sell-back runs on Kalshi demo, "
        "and the real Polymarket US sell-back is the clean-up's."
    )
    pair = [("kalshi", ticker), ("polymarket_us", slug)]
    q = reader.quote(pair, size=SIZE, min_edge=MIN_EDGE)
    reader.close()
    if q.contracts < SIZE or q.a is None or q.b is None:
        print(f"The pair doesn't clear min_edge {MIN_EDGE} ({q.limited_by}). Nothing was sent.")
        return 1
    pm_leg = q.b if q.b.venue == "polymarket_us" else q.a
    worst = worst_case(pm_leg.limit_price, pm_leg.fee)
    caps = (
        f"Real money: Polymarket US only, at most ${PM_CAP:g} of buys this run (hard cap). "
        f"Worst case −${worst:.4f}: both Polymarket US buys left open and settling at $0."
    )
    if pm_leg.limit_price > MAX_PM_PRICE + 1e-9 or worst > PM_CAP + 1e-9:
        print(f"Refusing: the Polymarket US leg is ${pm_leg.limit_price}, above the caps. Nothing was sent.")
        return 1
    if args.dry_run:
        print("\nDRY RUN (reads only, nothing sent).\n" + _plan(pair, q) + "\n" + order_note + "\n" + caps)
        return 0

    assert pm is not None
    store = str(run_dir / "live.db")
    alerts: list[dict[str, Any]] = []
    client = Client(
        mode="live",
        kalshi=kalshi,
        polymarket_us=pm,
        rules={**RULES, "markets": [ticker, slug]},
        store=store,
        on_alert=alerts.append,
    )
    q = client.quote(pair, size=SIZE, min_edge=MIN_EDGE)  # fresh books, with the live client
    if q.contracts < SIZE or q.a is None or q.b is None:
        print("The pair no longer clears min_edge on fresh books. Nothing was sent.")
        return 1
    pm_leg = q.b if q.b.venue == "polymarket_us" else q.a
    if pm_leg.limit_price > MAX_PM_PRICE + 1e-9:
        print(f"Refusing: the Polymarket US ask moved to ${pm_leg.limit_price}. Nothing was sent.")
        return 1
    print("\n" + _plan(pair, q))
    print(order_note)
    print(
        "Run 1: a hedged pair. Run 2: the kill switch after the Polymarket US leg fills, so it's sold back."
    )
    print("Then run 1's contracts are sold back at the bid.")
    print(caps)
    print(f"Guardrails: {json.dumps(RULES)}, these two markets only, kill switch on.")
    killed = client.killed
    if killed:
        print("\nThis run's new store started with the kill switch on, because the account already holds")
        print("positions or orders. Typing yes turns it off for this run's store only; those positions and")
        print("orders are never touched.")
    if input("\nType yes to send real Polymarket US orders: ").strip() != "yes":
        print("Nothing was sent.")
        client.close()
        return 1
    if killed:
        Admin(mode="live", store=store).resume()

    started = datetime.now(UTC)
    st = RunState()
    stopped: str | None = None
    try:
        _run_steps(client, pair, pm_leg, store, started, st)
    except KeyboardInterrupt:
        stopped = "interrupted (Ctrl-C)"
    except Exception as e:  # anything at all: the wrap-up below must still run
        stopped = f"crashed: {e!r}"
        traceback.print_exc()
    finally:
        if stopped:
            st.errors.append({"step": "stopped", "message": stopped})
            print(f"\n{stopped}: cancelling this run's open orders and saving what happened.")
        _wrap_up(client, pair, q, started, st, run_dir, alerts)
        client.close()
    if stopped and stopped.startswith("interrupted"):
        return 130
    return 0 if [t.status for t in st.trades] == ["hedged", "unwound"] and not st.errors else 1


if __name__ == "__main__":
    sys.exit(main())
