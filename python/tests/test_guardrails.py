from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from conftest import T0, Clock, FakeMarket, FakeVenue
from pydantic import ValidationError

from uselayer import Context, Decision, Mark, Order, RulesConfig, VenueError
from uselayer.guardrails import Group, Guardrails, PositionView


def order(**kw: Any) -> Order:
    base: dict[str, Any] = {
        "venue": "polymarket_us",
        "market": "mkt-a",
        "side": "yes",
        "price": 0.42,
        "size": 10,
    }
    return Order(**{**base, **kw})


def ctx(**kw: Any) -> Context:
    marks = {
        ("polymarket_us", "mkt-a", "yes"): Mark(0.40, 0.42, T0),
        ("polymarket_us", "mkt-b", "yes"): Mark(0.6, 0.62, T0),
    }
    return Context(**{"now": T0, "marks": marks, **kw})


def test_always_on_limits_can_only_be_tightened() -> None:
    assert RulesConfig(price_collar=0.02, max_orders_per_s=2).price_collar == 0.02
    with pytest.raises(ValidationError):
        RulesConfig(price_collar=0.10)
    with pytest.raises(ValidationError):
        RulesConfig(max_orders_per_s=50)
    with pytest.raises(ValidationError):
        RulesConfig(trailing_stop={"pct": 10})  # type: ignore[arg-type]


def test_price_collar_and_missing_quotes() -> None:
    g = Guardrails()
    assert g.check(order(price=0.47), ctx()).allowed
    v = g.check(order(price=0.48), ctx())
    assert v.blocked_by == "price_collar"
    assert g.check(order(action="sell", price=0.34), ctx()).blocked_by == "price_collar"
    assert g.check(order(market="unknown"), ctx()).blocked_by == "price_collar"


def test_stale_quotes_block_new_risk_but_not_exits() -> None:
    g = Guardrails({"max_quote_age_s": 10})
    old = ctx(now=T0 + timedelta(seconds=11))
    assert g.check(order(), old).blocked_by == "stale_quote"
    assert g.check(order(action="sell", price=0.4, reason="exit"), old).allowed


def test_kill_switch_lets_only_kill_and_unwind_through() -> None:
    g = Guardrails()
    k = ctx(killed=True)
    assert g.check(order(), k).blocked_by == "kill_switch"
    assert g.check(order(action="sell", price=0.4, reason="exit"), k).blocked_by == "kill_switch"
    assert g.check(order(action="sell", price=0.4, reason="kill"), k).allowed
    assert g.check(order(action="sell", price=0.4, reason="unwind"), k).allowed


def test_position_budget_markets_actions_and_expiry() -> None:
    o = order(fee_estimate=0.17)  # $4.37 at risk
    assert Guardrails({"max_position": {"per_market": 4}}).check(o, ctx()).blocked_by == "max_position"
    assert (
        Guardrails({"max_position": {"per_market": 5}})
        .check(o, ctx(exposure={"market:polymarket_us:mkt-a": 1.0}))
        .blocked_by
        == "max_position"
    )
    assert Guardrails({"budget": 10}).check(o, ctx(exposure={"total": 6.0})).blocked_by == "budget"
    assert Guardrails({"markets": ["nfl-*"]}).check(o, ctx()).blocked_by == "allowed"
    assert Guardrails({"markets": ["mkt-*"]}).check(o, ctx()).allowed
    assert Guardrails({"venues": ["kalshi"]}).check(o, ctx()).blocked_by == "allowed"
    exits_only = Guardrails({"actions": ["close"]})
    assert exits_only.check(o, ctx()).blocked_by == "allowed"
    assert exits_only.check(order(action="sell", price=0.4, reason="exit"), ctx()).allowed
    assert Guardrails({"expires_at": T0}).check(o, ctx()).blocked_by == "expires_at"


def test_max_daily_loss_blocks_or_kills() -> None:
    assert (
        Guardrails({"max_daily_loss": {"amount": 50}}).check(order(), ctx(pnl_today=-50)).blocked_by
        == "max_daily_loss"
    )
    v = Guardrails({"max_daily_loss": {"amount": 50, "on_breach": "kill"}}).check(order(), ctx(pnl_today=-60))
    assert v.decision.result == "kill"
    only = Guardrails({"max_daily_loss": {"amount": 50, "counts": "realized_only"}})
    assert only.check(order(), ctx(pnl_today=-80, realized_today=-10)).allowed


def test_pairs_are_checked_all_or_nothing() -> None:
    g = Guardrails({"max_position": {"per_market": 6}})
    a = order(group_id="g1", fee_estimate=0.0)  # $4.20
    b = order(market="mkt-b", price=0.62, group_id="g1", fee_estimate=0.0)  # $6.20 more in the same pair
    assert (
        g.check(a, ctx()).allowed and Guardrails({"max_position": {"per_market": 7}}).check(b, ctx()).allowed
    )
    v = g.check_group([a, b], ctx())
    assert v.blocked_by == "max_position"  # leg B is counted on top of leg A, so neither is sent


def test_approval_above_counts_a_pair_together() -> None:
    g = Guardrails({"approve_above": 8})
    a, b = (
        order(group_id="g", fee_estimate=0),
        order(market="mkt-b", price=0.62, group_id="g", fee_estimate=0),
    )
    assert g.check(a, ctx()).allowed
    assert g.check_group([a, b], ctx()).decision.result == "approve"


def test_custom_rules_and_a_stable_fingerprint() -> None:
    def no_fridays(o: Order, c: Context) -> Decision:
        return Decision.block("no_fridays", "not on Fridays") if c.now.weekday() == 4 else Decision.allow()

    g = Guardrails({"budget": 100}, custom=[no_fridays])
    assert g.check(order(), ctx(now=T0)).allowed  # T0 is a Thursday
    assert g.check(order(), ctx(now=T0 + timedelta(days=1), marks={})).decision.rule in (
        "no_fridays",
        "price_collar",
    )
    assert Guardrails({"budget": 100}).fingerprint == Guardrails({"budget": 100}).fingerprint
    assert Guardrails({"budget": 100}).fingerprint != Guardrails({"budget": 101}).fingerprint


def test_stop_loss_take_profit_never_on_stale_prices_and_not_on_pairs() -> None:
    g = Guardrails({"stop_loss": {"pct": 25}, "take_profit": {"pct": 40}})
    pos = PositionView("polymarket_us", "mkt-a", "yes", 10, 0.5, 5.0, 0.1)
    lo = {("polymarket_us", "mkt-a", "yes"): Mark(0.37, 0.39, T0)}
    exits = g.watch(Group(None, (pos,)), lo, ctx())
    assert (
        len(exits) == 1
        and exits[0].reason == "exit"
        and exits[0].action == "sell"
        and exits[0].price == pytest.approx(0.32)
    )
    assert (
        g.watch(Group(None, (pos,)), {("polymarket_us", "mkt-a", "yes"): Mark(0.45, 0.47, T0)}, ctx()) == []
    )
    assert (
        len(g.watch(Group(None, (pos,)), {("polymarket_us", "mkt-a", "yes"): Mark(0.71, 0.72, T0)}, ctx()))
        == 1
    )
    assert g.watch(Group(None, (pos,)), lo, ctx(now=T0 + timedelta(seconds=60))) == []
    pair = Group("g", (pos, PositionView("polymarket_us", "mkt-b", "no", 10, 0.4, 4.0, 0.1, "g")))
    assert g.watch(pair, {**lo, ("polymarket_us", "mkt-b", "no"): Mark(0.1, 0.2, T0)}, ctx()) == []


def test_rules_file_yaml(tmp_path: Any) -> None:
    from uselayer.guardrails import load_rules

    p = tmp_path / "guardrails.yaml"
    p.write_text("budget: 1000\nmax_position:\n  per_market: 200\nday_timezone: America/New_York\n")
    r = load_rules(p)
    assert r.budget == 1000 and r.max_position is not None and r.max_position.per_market == 200
    with pytest.raises(ValidationError):
        load_rules({"buget": 5})  # typos fail loudly


def test_client_preview_has_no_side_effects(make_client: Any) -> None:
    c = make_client(rules={"approve_above": 1})
    o = c.order(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=10)
    p = c.preview(o)
    assert p.needs_approval and not p.allowed and p.est_fill.filled == 10
    assert c.decisions() == [] and c.fills() == [] and c.orders(open=False) == []


def test_approval_yes_sends_no_blocks(make_client: Any) -> None:
    asked: list[str] = []

    def approve(o: Order, reason: str) -> bool:
        asked.append(reason)
        return o.size < 20

    c = make_client(rules={"approve_above": 1}, on_approval=approve)
    assert c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.43, size=10).status == "filled"
    with pytest.raises(VenueError) as e:
        c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.45, size=25)
    assert e.value.code == "blocked_by_rule" and e.value.rule == "approve_above" and len(asked) == 2
    assert any(d["result"] == "approve" for d in c.decisions())


def test_daily_loss_counts_open_positions_at_the_bid(
    make_client: Any, venue: FakeVenue, clock: Clock
) -> None:
    c = make_client(rules={"max_daily_loss": {"amount": 1}}, on_approval=lambda o, r: True)
    c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.42, size=10)
    venue.markets["mkt-a"] = FakeMarket("mkt-a", bids=[(0.20, 50)], asks=[(0.22, 10)])
    clock.advance(20)
    with pytest.raises(VenueError) as e:
        c.buy(venue="polymarket_us", market="mkt-a", side="yes", price=0.22, size=1)
    assert e.value.rule == "max_daily_loss"
