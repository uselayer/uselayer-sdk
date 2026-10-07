"""Guardrails: rules every order passes before it's sent, in paper and live alike.

Rules are pure: they get the order and a read-only :class:`Context` (with ``ctx.now`` passed in) and
return a :class:`Decision`. They never call the network, read the clock or act. That's what makes
``client.preview()`` safe and a backtest replay exact.

Three limits are always on and can only be tightened: a price collar (a limit price at most 5¢ past
the best price on the other side), at most 5 orders a second, and the kill switch.

    client = Client(rules={"max_position": {"per_market": 200}, "approve_above": 100})
    client.preview(order).blocked_by   # e.g. "max_position"

Rule settings (all optional; money in US dollars):

    venues: [polymarket_us]          only these venues
    markets: ["nfl-*"]               only these markets ("*" matches any ending)
    actions: [open, close]           [close] means exits only
    expires_at: 2026-12-31T23:59Z    no new positions after this
    max_position: {per_market: 200}  $ at risk in one market (or one pair)
    budget: 1000                     $ at risk across everything at once
    max_daily_loss: {amount: 150, counts: realized_and_open, on_breach: block}
    day_timezone: UTC                when "today" starts
    approve_above: 100               orders (or pairs) costing more wait for a yes
    approval_timeout_s: 120          no answer in time means no
    stop_loss: {pct: 25}             exit when the bid falls 25% below entry
    take_profit: {pct: 40}           exit when the bid rises 40% above entry
    exits_apply_to_pairs: false      pairs are held to settlement
    max_exit_slippage: 0.05          exits go out at most 5¢ below the bid
    max_quote_age_s: 10              older prices don't count
    order_ttl_s: 60                  resting orders expire
    price_collar: 0.05               always on; can only be lowered
    max_orders_per_s: 5              always on; can only be lowered
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import VenueError
from .orders import Order

ALWAYS_ON_PRICE_COLLAR = 0.05
ALWAYS_ON_MAX_ORDERS_PER_S = 5

# ---- decisions ---------------------------------------------------------------------------------

Result = Literal["allow", "block", "approve", "kill"]
_RANK: dict[str, int] = {"allow": 0, "approve": 1, "block": 2, "kill": 3}


@dataclass(frozen=True)
class Decision:
    """A rule's answer: ``allow``, ``block(reason)``, ``approve(reason)`` or ``kill(reason)``.

    Decision.block("max_position", "$250 at risk in this market; the limit is $200")
    """

    result: Result
    rule: str | None = None
    reason: str | None = None

    @staticmethod
    def allow(rule: str | None = None) -> Decision:
        return Decision("allow", rule)

    @staticmethod
    def block(rule: str, reason: str) -> Decision:
        return Decision("block", rule, reason)

    @staticmethod
    def approve(rule: str, reason: str) -> Decision:
        return Decision("approve", rule, reason)

    @staticmethod
    def kill(rule: str, reason: str) -> Decision:
        return Decision("kill", rule, reason)

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result, "rule": self.rule, "reason": self.reason}


# ---- what a rule can see -------------------------------------------------------------------------


@dataclass(frozen=True)
class Mark:
    """The best bid and ask for one side of a market, and the venue's time for them."""

    bid: float | None
    ask: float | None
    as_of: datetime | None

    def age_s(self, now: datetime) -> float | None:
        return None if self.as_of is None else (now - self.as_of).total_seconds()


@dataclass(frozen=True)
class PositionView:
    """An open position as rules see it."""

    venue: str
    market: str
    side: str
    contracts: float
    avg_price: float
    cost: float
    fees: float
    group_id: str | None = None


def risk_key(venue: str, market: str, group_id: str | None) -> str:
    """Which limit bucket an order or position counts toward: its pair if it has one, else its market."""
    return f"group:{group_id}" if group_id else f"market:{venue}:{market}"


def order_risk(order: Order) -> float:
    """Money an order puts at risk if it all fills: price × size + its fee estimate. Sells add none."""
    if order.action == "sell":
        return 0.0
    return order.price * order.remaining + max(order.fee_estimate or 0.0, 0.0)


def reduces_risk(order: Order) -> bool:
    """Exits, unwinds, kill orders and sells only ever shrink a position."""
    return order.reason in ("exit", "unwind", "kill") or order.action == "sell"


@dataclass(frozen=True)
class Context:
    """Everything a rule may look at. Read-only; built fresh by the SDK before each check.

    ``exposure`` maps each :func:`risk_key` (and ``"total"``) to dollars at risk, counting resting and
    in-flight orders as if they all fill. ``marks`` maps ``(venue, market, side)`` to a :class:`Mark`.
    ``pnl_today`` is realized + fees + open positions marked at the bid (see max_daily_loss).
    """

    now: datetime
    positions: tuple[PositionView, ...] = ()
    exposure: Mapping[str, float] = field(default_factory=dict)
    marks: Mapping[tuple[str, str, str], Mark] = field(default_factory=dict)
    pnl_today: float = 0.0
    realized_today: float = 0.0
    killed: bool = False
    config_hash: str = ""
    missing_marks: tuple[str, ...] = ()

    def mark_for(self, order: Order) -> Mark | None:
        return self.marks.get((order.venue, order.market, order.side))

    def with_order(self, order: Order) -> Context:
        """A copy that already counts ``order`` as filled: how pairs are checked all or nothing."""
        risk = order_risk(order)
        if risk == 0:
            return self
        key = risk_key(order.venue, order.market, order.group_id)
        exposure = dict(self.exposure)
        exposure[key] = exposure.get(key, 0.0) + risk
        exposure["total"] = exposure.get("total", 0.0) + risk
        return replace(self, exposure=exposure)


@dataclass(frozen=True)
class Group:
    """Positions that belong together: a pair's two legs, or a lone position."""

    group_id: str | None
    positions: tuple[PositionView, ...]


# ---- settings ------------------------------------------------------------------------------------


class MaxPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_market: float = Field(gt=0)


class MaxDailyLoss(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: float = Field(gt=0)
    counts: Literal["realized_and_open", "realized_only"] = "realized_and_open"
    on_breach: Literal["block", "kill", "kill_and_flatten"] = "block"


class Pct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pct: float = Field(gt=0, lt=100)


class RulesConfig(BaseModel):
    """The rule settings, from a dict or a YAML/JSON file. Frozen once the client starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    venues: list[str] | None = None
    markets: list[str] | None = None
    actions: list[Literal["open", "close"]] = ["open", "close"]
    expires_at: datetime | None = None
    max_position: MaxPosition | None = None
    budget: float | None = Field(default=None, gt=0)
    max_daily_loss: MaxDailyLoss | None = None
    day_timezone: str = "UTC"
    approve_above: float | None = Field(default=None, gt=0)
    approval_timeout_s: float = Field(default=120, gt=0)
    stop_loss: Pct | None = None
    take_profit: Pct | None = None
    trailing_stop: None = None
    exits_apply_to_pairs: bool = False
    max_exit_slippage: float = Field(default=0.05, ge=0, lt=1)
    max_quote_age_s: float = Field(default=10, gt=0)
    order_ttl_s: float = Field(default=60, gt=0)
    price_collar: float = Field(default=ALWAYS_ON_PRICE_COLLAR, gt=0)
    max_orders_per_s: float = Field(default=ALWAYS_ON_MAX_ORDERS_PER_S, gt=0)

    @field_validator("price_collar")
    @classmethod
    def _collar_only_tighter(cls, v: float) -> float:
        if v > ALWAYS_ON_PRICE_COLLAR:
            raise ValueError(f"price_collar can only be tightened: at most {ALWAYS_ON_PRICE_COLLAR}")
        return v

    @field_validator("max_orders_per_s")
    @classmethod
    def _throttle_only_tighter(cls, v: float) -> float:
        if v > ALWAYS_ON_MAX_ORDERS_PER_S:
            raise ValueError(f"max_orders_per_s can only be tightened: at most {ALWAYS_ON_MAX_ORDERS_PER_S}")
        return v

    @field_validator("day_timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    def fingerprint(self) -> str:
        """A short hash of these settings. It's the same in paper and live for the same settings."""
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:16]


def load_rules(rules: RulesConfig | Mapping[str, Any] | str | Path | None) -> RulesConfig:
    """Rule settings from a dict, a ``.json`` / ``.yaml`` file, or ``None`` (only the always-on limits).

    load_rules({"budget": 1000}).budget  # 1000.0
    """
    if rules is None:
        return RulesConfig()
    if isinstance(rules, RulesConfig):
        return rules
    if isinstance(rules, (str, Path)):
        text = Path(rules).read_text()
        if str(rules).endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError as e:
                raise VenueError(
                    "invalid_order",
                    "Reading a YAML rules file needs PyYAML.",
                    hint='pip install "uselayer[yaml]"',
                    retryable=False,
                ) from e
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        return RulesConfig.model_validate(data)
    return RulesConfig.model_validate(dict(rules))


# ---- rules ---------------------------------------------------------------------------------------


@runtime_checkable
class Rule(Protocol):
    """A guardrail. ``check`` runs before every order; ``watch`` runs on open positions."""

    name: str

    def check(self, order: Order, ctx: Context) -> Decision: ...


class _Base:
    name = "rule"

    def check(self, order: Order, ctx: Context) -> Decision:
        return Decision.allow(self.name)

    def watch(self, group: Group, marks: Mapping[tuple[str, str, str], Mark], ctx: Context) -> list[Order]:
        return []


class KillSwitch(_Base):
    """Always on. While the kill switch is pressed, only kill and unwind orders go through."""

    name = "kill_switch"

    def check(self, order: Order, ctx: Context) -> Decision:
        if ctx.killed and order.reason not in ("kill", "unwind"):
            return Decision.block(self.name, "The kill switch is on; only kill and unwind orders are sent.")
        return Decision.allow(self.name)


class PriceCollar(_Base):
    """Always on. A limit price may be at most ``price_collar`` past the best price on the other side."""

    name = "price_collar"

    def __init__(self, collar: float) -> None:
        self.collar = collar

    def check(self, order: Order, ctx: Context) -> Decision:
        mark = ctx.mark_for(order)
        if order.action == "buy":
            if mark is None or mark.ask is None:
                return Decision.block(
                    self.name, "No ask to check the price against: nobody is selling this side right now."
                )
            if order.price > mark.ask + self.collar + 1e-9:
                return Decision.block(
                    self.name,
                    f"Limit {order.price} is more than {self.collar} above the best ask {mark.ask}.",
                )
        else:
            if mark is None or mark.bid is None:
                return Decision.block(
                    self.name, "No bid to check the price against: nobody is buying this side right now."
                )
            if order.price < mark.bid - self.collar - 1e-9:
                return Decision.block(
                    self.name,
                    f"Limit {order.price} is more than {self.collar} below the best bid {mark.bid}.",
                )
        return Decision.allow(self.name)


class StaleQuote(_Base):
    """New risk needs a price no older than ``max_quote_age_s``, by the venue's own clock."""

    name = "stale_quote"

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s

    def check(self, order: Order, ctx: Context) -> Decision:
        if reduces_risk(order):
            return Decision.allow(self.name)
        mark = ctx.mark_for(order)
        age = None if mark is None else mark.age_s(ctx.now)
        if age is None:
            return Decision.block(
                self.name, "No price for this market; new positions wait until there is one."
            )
        if age > self.max_age_s:
            return Decision.block(
                self.name, f"The price is {age:.0f}s old; max_quote_age_s is {self.max_age_s:g}."
            )
        return Decision.allow(self.name)


class Expiry(_Base):
    name = "expires_at"

    def __init__(self, expires_at: datetime) -> None:
        self.expires_at = expires_at

    def check(self, order: Order, ctx: Context) -> Decision:
        if not reduces_risk(order) and ctx.now >= self.expires_at:
            return Decision.block(
                self.name, f"These rules stopped allowing new positions at {self.expires_at.isoformat()}."
            )
        return Decision.allow(self.name)


class Allowed(_Base):
    """Only the listed venues, markets and actions. Exits are allowed anywhere."""

    name = "allowed"

    def __init__(self, venues: list[str] | None, markets: list[str] | None, actions: list[str]) -> None:
        self.venues, self.markets, self.actions = venues, markets, actions

    def check(self, order: Order, ctx: Context) -> Decision:
        if reduces_risk(order):
            if "close" not in self.actions and order.reason == "open":
                return Decision.block(self.name, "actions doesn't include close.")
            return Decision.allow(self.name)
        if "open" not in self.actions:
            return Decision.block(self.name, "These rules allow exits only (actions: [close]).")
        if self.venues is not None and order.venue not in self.venues:
            return Decision.block(self.name, f"{order.venue} isn't in venues: {self.venues}.")
        if self.markets is not None and not any(fnmatch.fnmatchcase(order.market, m) for m in self.markets):
            return Decision.block(self.name, f"{order.market} isn't in markets: {self.markets}.")
        return Decision.allow(self.name)


class MaxPositionRule(_Base):
    name = "max_position"

    def __init__(self, per_market: float) -> None:
        self.per_market = per_market

    def check(self, order: Order, ctx: Context) -> Decision:
        if reduces_risk(order):
            return Decision.allow(self.name)
        key = risk_key(order.venue, order.market, order.group_id)
        after = ctx.exposure.get(key, 0.0) + order_risk(order)
        if after > self.per_market + 1e-9:
            return Decision.block(
                self.name, f"${after:.2f} would be at risk here; the limit is ${self.per_market:g}."
            )
        return Decision.allow(self.name)


class Budget(_Base):
    name = "budget"

    def __init__(self, budget: float) -> None:
        self.budget = budget

    def check(self, order: Order, ctx: Context) -> Decision:
        if reduces_risk(order):
            return Decision.allow(self.name)
        after = ctx.exposure.get("total", 0.0) + order_risk(order)
        if after > self.budget + 1e-9:
            return Decision.block(
                self.name, f"${after:.2f} would be at risk in total; the budget is ${self.budget:g}."
            )
        return Decision.allow(self.name)


class MaxDailyLossRule(_Base):
    name = "max_daily_loss"

    def __init__(self, cfg: MaxDailyLoss) -> None:
        self.cfg = cfg

    def check(self, order: Order, ctx: Context) -> Decision:
        if reduces_risk(order):
            return Decision.allow(self.name)
        pnl = ctx.realized_today if self.cfg.counts == "realized_only" else ctx.pnl_today
        if pnl <= -self.cfg.amount:
            reason = f"Today's loss is ${-pnl:.2f}; the limit is ${self.cfg.amount:g}."
            if self.cfg.on_breach == "block":
                return Decision.block(self.name, reason)
            return Decision.kill(self.name, reason)
        return Decision.allow(self.name)


class ApproveAbove(_Base):
    name = "approve_above"

    def __init__(self, amount: float) -> None:
        self.amount = amount

    def check(self, order: Order, ctx: Context) -> Decision:
        if reduces_risk(order):
            return Decision.allow(self.name)
        cost = order_risk(order)
        if cost > self.amount + 1e-9:
            return Decision.approve(
                self.name, f"This order costs ${cost:.2f}, above approve_above ${self.amount:g}."
            )
        return Decision.allow(self.name)

    def check_group(self, orders: Sequence[Order], ctx: Context) -> Decision:
        cost = sum(order_risk(o) for o in orders if not reduces_risk(o))
        if cost > self.amount + 1e-9:
            return Decision.approve(
                self.name, f"This pair costs ${cost:.2f}, above approve_above ${self.amount:g}."
            )
        return Decision.allow(self.name)


class Exits(_Base):
    """Stop-loss and take-profit: exits sent by ``client.monitor()``, never on a stale price."""

    name = "exits"

    def __init__(self, cfg: RulesConfig) -> None:
        self.cfg = cfg

    def watch(self, group: Group, marks: Mapping[tuple[str, str, str], Mark], ctx: Context) -> list[Order]:
        if len(group.positions) > 1 and not self.cfg.exits_apply_to_pairs:
            return []
        marked: list[tuple[PositionView, Mark]] = []
        for p in group.positions:
            m = marks.get((p.venue, p.market, p.side))
            age = None if m is None else m.age_s(ctx.now)
            if m is None or m.bid is None or age is None or age > self.cfg.max_quote_age_s:
                return []  # never exit on a missing or stale price
            marked.append((p, m))
        cost = sum(p.cost for p, _ in marked)
        if cost <= 0:
            return []
        value = sum(p.contracts * (m.bid or 0.0) for p, m in marked)
        change_pct = (value - cost) / cost * 100
        hit = (self.cfg.stop_loss is not None and change_pct <= -self.cfg.stop_loss.pct) or (
            self.cfg.take_profit is not None and change_pct >= self.cfg.take_profit.pct
        )
        if not hit:
            return []
        exits = []
        for p, m in marked:
            assert m.bid is not None
            price = max(round(m.bid - self.cfg.max_exit_slippage, 6), 0.000001)
            exits.append(
                Order(
                    venue=p.venue,
                    market=p.market,
                    side=p.side,
                    action="sell",
                    price=price,
                    size=p.contracts,
                    tif="ioc",
                    group_id=p.group_id,
                    reason="exit",
                )
            )
        return exits


CustomRule = Rule | Callable[[Order, Context], Decision]


class _FnRule(_Base):
    def __init__(self, fn: Callable[[Order, Context], Decision]) -> None:
        self.fn = fn
        self.name = getattr(fn, "__name__", "custom")

    def check(self, order: Order, ctx: Context) -> Decision:
        return self.fn(order, ctx)


# ---- the pipeline --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Every rule's answer for an order (or a pair), and the one that counts.

    ``decision`` is the strongest answer: kill beats block beats approve beats allow.
    """

    decision: Decision
    decisions: tuple[Decision, ...]

    @property
    def allowed(self) -> bool:
        return self.decision.result == "allow"

    @property
    def blocked_by(self) -> str | None:
        return self.decision.rule if self.decision.result in ("block", "kill") else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision.to_dict(),
            "decisions": [d.to_dict() for d in self.decisions if d.result != "allow"],
        }


def _strongest(ds: Iterable[Decision]) -> Decision:
    best = Decision.allow()
    for d in ds:
        if _RANK[d.result] > _RANK[best.result]:
            best = d
    return best


class Guardrails:
    """The rule set for one client, built once from the settings and frozen.

    g = Guardrails({"budget": 500})
    g.check(order, ctx).allowed
    """

    def __init__(
        self,
        rules: RulesConfig | Mapping[str, Any] | str | Path | None = None,
        custom: Sequence[CustomRule] = (),
    ) -> None:
        self.config = load_rules(rules)
        c = self.config
        built: list[Any] = [KillSwitch(), PriceCollar(c.price_collar), StaleQuote(c.max_quote_age_s)]
        if c.expires_at is not None:
            built.append(Expiry(c.expires_at))
        built.append(Allowed(c.venues, c.markets, list(c.actions)))
        if c.max_position is not None:
            built.append(MaxPositionRule(c.max_position.per_market))
        if c.budget is not None:
            built.append(Budget(c.budget))
        if c.max_daily_loss is not None:
            built.append(MaxDailyLossRule(c.max_daily_loss))
        if c.approve_above is not None:
            built.append(ApproveAbove(c.approve_above))
        if c.stop_loss is not None or c.take_profit is not None:
            built.append(Exits(c))
        for r in custom:
            built.append(r if hasattr(r, "check") else _FnRule(r))
        self.rules: tuple[Any, ...] = tuple(built)
        names = [getattr(r, "name", "custom") for r in custom]
        self.fingerprint = c.fingerprint() + (
            "+" + hashlib.sha256(",".join(names).encode()).hexdigest()[:8] if names else ""
        )

    def check(self, order: Order, ctx: Context) -> Verdict:
        """Every rule's decision for one order."""
        ds = tuple(r.check(order, ctx) for r in self.rules)
        return Verdict(_strongest(ds), ds)

    def check_group(self, orders: Sequence[Order], ctx: Context) -> Verdict:
        """Both legs of a pair, all or nothing: each leg is checked against a context that already
        counts the legs before it, and no leg may go unless every leg may."""
        ds: list[Decision] = []
        running = ctx
        for o in orders:
            for r in self.rules:
                if isinstance(r, ApproveAbove):
                    continue  # a pair's approval is counted on the pair below
                ds.append(r.check(o, running))
            running = running.with_order(o)
        for r in self.rules:
            if isinstance(r, ApproveAbove):
                ds.append(r.check_group(orders, ctx))
        return Verdict(_strongest(ds), tuple(ds))

    def watch(self, group: Group, marks: Mapping[tuple[str, str, str], Mark], ctx: Context) -> list[Order]:
        """Exit orders the rules want for one group right now (stop-loss, take-profit)."""
        out: list[Order] = []
        for r in self.rules:
            if hasattr(r, "watch"):
                out.extend(r.watch(group, marks, ctx))
        return out
