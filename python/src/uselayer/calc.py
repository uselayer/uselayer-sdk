"""Profit and size calculators that give the same answers as Layer's ``POST /v0/profit`` and ``POST /v0/size``.

Ports of Layer's ``calculateProfit`` (``lib/profit.ts``) and ``calculateSize`` (``lib/size.ts``). They
take the same JSON bodies and return the same fields, to the millionth of a dollar. They run on your
machine: prices you read from a venue never leave it.

    from uselayer.calc import profit
    profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}})["net_profit"]
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal, cast

from .errors import VenueError
from .fees import (
    MICRO,
    dollars,
    kalshi_fee,
    polymarket_fee,
    round_to,
    to_micro,
)
from .venue_rules import (
    KalshiFees,
    PolymarketFees,
    PolymarketUSFees,
    PolymarketUSPremiumFees,
    PolymarketUSSchedule,
    rules_at,
)

MAX_CONTRACTS = 1_000_000
MAX_DAYS = 3_650
MAX_LEVELS = 500
MAX_LEVEL_SIZE = 10_000_000
_DAY_S = 86_400.0
_EPSILON = 1e-9

Venue = Literal["kalshi", "polymarket", "polymarket_us"]


# ---- validation -----------------------------------------------------------------------------


def _bad(field: str, hint: str) -> VenueError:
    return VenueError(
        "invalid_order",
        f"{field} is not valid.",
        hint=hint,
        next="Fix that field and call again.",
        retryable=False,
    )


def _micro_ok(n: float) -> bool:
    return abs(n * MICRO - round(n * MICRO)) < 1e-6


def _num(d: dict[str, Any], key: str, field: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise _bad(field, f"{field} must be a number.")
    return float(v) if isinstance(v, float) else v


def _price(d: dict[str, Any], field: str) -> float:
    p = _num(d, "price", field)
    if p is None or not (0 < p < 1) or not _micro_ok(p):
        raise _bad(
            field, "Prices are in dollars, above 0 and below 1, with up to 6 decimal places: 0.42 means 42¢."
        )
    return p


def _role(d: dict[str, Any], field: str) -> Literal["taker", "maker"]:
    r = d.get("role", "taker")
    if r not in ("taker", "maker"):
        raise _bad(field, 'role is "taker" (your order fills against the book, the default) or "maker".')
    return cast(Literal["taker", "maker"], r)


def _ranged(
    d: dict[str, Any], key: str, field: str, lo: float, hi: float, default: float | None
) -> float | None:
    v = _num(d, key, field)
    if v is None:
        return default
    if not (lo <= v <= hi) or not _micro_ok(v):
        raise _bad(field, f"{key} must be between {lo} and {hi}, with up to 6 decimal places.")
    return v


def _exponent(d: dict[str, Any], field: str) -> int:
    v = d.get("exponent", 1)
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 4:
        raise _bad(
            field, "exponent is the market's feeSchedule.exponent: a whole number from 1 to 4, usually 1."
        )
    return v


def _days(req: dict[str, Any], now: datetime) -> float | None:
    if "settles_at" in req and "days_held" in req:
        raise _bad("days_held", "Send settles_at or days_held, not both.")
    if req.get("days_held") is not None:
        d = _num(req, "days_held", "days_held")
        if d is None or not (0 < d <= MAX_DAYS):
            raise _bad(
                "days_held", "days_held is how many days your money is tied up: above 0 and up to 3,650."
            )
        return d
    s = req.get("settles_at")
    if s is None:
        return None
    if not isinstance(s, str):
        raise _bad(
            "settles_at", 'Send a date like "2026-10-05" or a time with a zone like "2026-10-05T17:00:00Z".'
        )
    try:
        when = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise _bad(
            "settles_at", 'Send a date like "2026-10-05" or a time with a zone like "2026-10-05T17:00:00Z".'
        ) from e
    if when.tzinfo is None:
        if len(s) != 10:
            raise _bad("settles_at", "A time needs a zone, e.g. 2026-10-05T17:00:00Z.")
        when = when.replace(tzinfo=UTC)  # a date alone is 00:00 UTC
    if when <= now:
        raise _bad("settles_at", "settles_at is when your money comes back, so it has to be later than now.")
    # Date.parse keeps whole milliseconds.
    ms = math.floor(when.timestamp() * 1000 + 0.5) - math.floor(now.timestamp() * 1000 + 0.5)
    return ms / (_DAY_S * 1000)


def _rules(
    now: datetime, req: dict[str, Any]
) -> tuple[KalshiFees, PolymarketFees | None, PolymarketUSSchedule | None]:
    """The schedules in force at ``now`` for the venues the request names, so a Polymarket US pair
    from before Polymarket's first known schedule still prices."""
    k = rules_at("kalshi", now).fees
    p = rules_at("polymarket", now).fees if req.get("polymarket") is not None else None
    u = rules_at("polymarket_us", now).fees if req.get("polymarket_us") is not None else None
    assert isinstance(k, KalshiFees)
    assert p is None or isinstance(p, PolymarketFees)
    assert u is None or isinstance(u, (PolymarketUSFees, PolymarketUSPremiumFees))
    return k, p, u


def _us_detail(uf: PolymarketUSSchedule, role: str, coefficient: float | None) -> dict[str, Any]:
    """The Polymarket US fee setting a leg was priced with, as Layer's API reports it.

    In the premium era (before 2026-04-03) a taker without a ``fee_coefficient`` paid a share of the
    premium: that's ``fee_premium_rate``, since there was no Θ (negative for a maker's rebate).
    """
    if isinstance(uf, PolymarketUSPremiumFees):
        if role == "maker":
            return {"fee_premium_rate": -uf.maker_rebate_rate}
        if coefficient is None:
            return {"fee_premium_rate": uf.taker_rate}
        return {"fee_coefficient": coefficient}
    if role == "maker":
        return {"fee_coefficient": -uf.maker_rebate}
    return {"fee_coefficient": uf.taker_coefficient if coefficient is None else coefficient}


def _one_polymarket(req: dict[str, Any]) -> None:
    if (req.get("polymarket") is None) == (req.get("polymarket_us") is None):
        raise _bad(
            "polymarket",
            "The trade is Kalshi plus one Polymarket: send polymarket_us or polymarket, not both.",
        )


def _category_rate(p: dict[str, Any], field: str, pf: PolymarketFees) -> float:
    rate = _ranged(p, "fee_rate", field + ".fee_rate", 0, 1, None)
    if rate is not None:
        return rate
    cat = p.get("category")
    if not isinstance(cat, str):
        raise _bad(
            field,
            'Tell Layer Polymarket\'s fee: "fee_rate": 0.05 (the market\'s feeSchedule.rate), or "category": "sports".',
        )
    key = cat.lower()
    if key not in pf.category_rates:
        raise _bad(field + ".category", f"category is one of {', '.join(pf.category_rates)}.")
    return pf.category_rates[key]


def _hold_fields(days: float | None, ret: float) -> dict[str, float]:
    if days is None:
        return {}
    return {
        "days_held": round_to(days, 2),
        "return_per_day_pct": round_to((ret * 100) / days, 4),
        "annualized_return_pct": round_to((ret * 100 * 365) / days, 2),
    }


# ---- POST /v0/profit --------------------------------------------------------------------------


def profit(req: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Fees and net profit for buying one side on each venue, as ``POST /v0/profit`` answers.

    Buying YES on one venue and NO on the other for the same outcome pays exactly $1 per contract
    whichever way it settles. The second leg is ``polymarket`` or ``polymarket_us``: send exactly one.

        r = profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}})
        r["fees"], r["net_profit"], r["profitable"]
    """
    now = now or datetime.now(UTC)
    kf, pf, uf = _rules(now, req)
    contracts = req.get("contracts")
    if isinstance(contracts, bool) or not isinstance(contracts, int) or not 1 <= contracts <= MAX_CONTRACTS:
        raise _bad(
            "contracts", "contracts is how many you buy on each venue: a whole number from 1 to 1,000,000."
        )
    k = req.get("kalshi")
    if not isinstance(k, dict):
        raise _bad("kalshi", 'Add the Kalshi leg: "kalshi": {"price": 0.42}.')
    if "market_id" in k:
        raise _bad(
            "kalshi.market_id",
            "The SDK works out fees locally; send the fee settings instead of a market id.",
        )
    _one_polymarket(req)
    days = _days(req, now)

    k_price = _price(k, "kalshi.price")
    k_role = _role(k, "kalshi.role")
    fee_type = k.get("fee_type", "quadratic")
    if fee_type not in kf.maker_rates:
        raise _bad("kalshi.fee_type", f"fee_type is one of {', '.join(kf.maker_rates)}.")
    k_rate = kf.taker_rate if k_role == "taker" else kf.maker_rates[fee_type]
    multiplier = _ranged(k, "fee_multiplier", "kalshi.fee_multiplier", 0, 10, 1)
    assert multiplier is not None
    k_fee = kalshi_fee(contracts=contracts, price=k_price, rate=k_rate, multiplier=multiplier)
    k_cost = to_micro(k_price) * contracts

    venue, o_price, o_role, o_fee, detail = _other_leg(req, contracts, pf, uf)
    p_cost = to_micro(o_price) * contracts

    payout = MICRO * contracts
    outlay = k_cost + p_cost + k_fee + o_fee
    net = payout - outlay
    ret = float(net) / float(outlay)

    return {
        "contracts": contracts,
        "kalshi": {
            "price": k_price,
            "role": k_role,
            "cost": dollars(k_cost),
            "fee": dollars(k_fee),
            "fee_rate": k_rate,
            "fee_multiplier": multiplier,
            "fee_type": fee_type,
        },
        venue: {"price": o_price, "role": o_role, "cost": dollars(p_cost), "fee": dollars(o_fee), **detail},
        "payout": dollars(payout),
        "spread": dollars(MICRO - to_micro(k_price) - to_micro(o_price)),
        "gross_profit": dollars(payout - k_cost - p_cost),
        "fees": dollars(k_fee + o_fee),
        "net_profit": dollars(net),
        "return_pct": round_to(ret * 100, 2),
        **_hold_fields(days, ret),
        "profitable": net > 0,
    }


def _other_leg(
    req: dict[str, Any], contracts: int, pf: PolymarketFees | None, uf: PolymarketUSSchedule | None
) -> tuple[str, float, str, int, dict[str, Any]]:
    u = req.get("polymarket_us")
    if isinstance(u, dict):
        assert uf is not None
        price = _price(u, "polymarket_us.price")
        role = _role(u, "polymarket_us.role")
        coefficient = _ranged(u, "fee_coefficient", "polymarket_us.fee_coefficient", 0, 1, None)
        fee = uf.fee(contracts=contracts, price=price, role=role, coefficient=coefficient)
        return "polymarket_us", price, role, fee, _us_detail(uf, role, coefficient)
    p = req.get("polymarket")
    if not isinstance(p, dict):
        raise _bad("polymarket", "The second leg must be an object.")
    assert pf is not None
    price = _price(p, "polymarket.price")
    role = _role(p, "polymarket.role")
    rate = _category_rate(p, "polymarket", pf)
    exponent = _exponent(p, "polymarket.exponent")
    fee = polymarket_fee(contracts=contracts, price=price, rate=rate, exponent=exponent, role=role)
    return "polymarket", price, role, fee, {"fee_rate": 0 if role == "maker" else rate, "exponent": exponent}


# ---- POST /v0/size ----------------------------------------------------------------------------


class LegBook:
    """One leg of a pair for the size walk: the asks you'd buy (cheapest first) and how its fee works.

    ``fee_per_contract(price)`` is the unrounded taker fee per contract, used to decide the edge;
    ``fee(contracts, price)`` is one fill's fee in millionths of a dollar, with the venue's rounding.
    """

    def __init__(
        self,
        venue: str,
        levels: list[dict[str, float]],
        fee_per_contract: Any,
        fee: Any,
        detail: dict[str, Any],
    ):
        self.venue = venue
        self.levels = levels  # cheapest first
        self.fee_per_contract = fee_per_contract  # unrounded, for deciding the edge
        self.fee = fee  # one fill, with the venue's rounding
        self.detail = detail


def _asks(d: dict[str, Any], field: str) -> list[dict[str, float]]:
    asks = d.get("asks")
    if not isinstance(asks, list) or not 1 <= len(asks) <= MAX_LEVELS:
        raise _bad(
            field + ".asks",
            'asks is the order book for the side you buy: a list like [{"price": 0.42, "size": 100}], up to 500.',
        )
    out = []
    for i, lvl in enumerate(asks):
        if not isinstance(lvl, dict):
            raise _bad(f"{field}.asks.{i}", "Each level is {price, size}.")
        price = _price(lvl, f"{field}.asks.{i}.price")
        size = _num(lvl, "size", f"{field}.asks.{i}.size")
        if size is None or not (0 < size <= MAX_LEVEL_SIZE) or not _micro_ok(size):
            raise _bad(
                f"{field}.asks.{i}.size",
                "size is how many contracts are offered at that price: above 0, up to 10,000,000.",
            )
        out.append({"price": price, "size": size})
    return sorted(out, key=lambda x: x["price"])


def _pq(p: float) -> float:
    return p * (1 - p)


def _size_books(
    req: dict[str, Any], kf: KalshiFees, pf: PolymarketFees | None, uf: PolymarketUSSchedule | None
) -> tuple[LegBook, LegBook]:
    k = req.get("kalshi")
    if not isinstance(k, dict):
        raise _bad("kalshi", 'Add the Kalshi book: "kalshi": {"asks": [{"price": 0.42, "size": 100}]}.')
    k_rate = kf.taker_rate
    k_mult = _ranged(k, "fee_multiplier", "kalshi.fee_multiplier", 0, 10, 1)
    assert k_mult is not None
    kalshi = LegBook(
        "kalshi",
        _asks(k, "kalshi"),
        lambda p: k_mult * k_rate * _pq(p),
        lambda c, p: kalshi_fee(contracts=c, price=p, rate=k_rate, multiplier=k_mult),
        {"fee_rate": k_rate, "fee_multiplier": k_mult},
    )
    u = req.get("polymarket_us")
    if isinstance(u, dict):
        assert uf is not None
        us = uf
        coef = _ranged(u, "fee_coefficient", "polymarket_us.fee_coefficient", 0, 1, None)
        return kalshi, LegBook(
            "polymarket_us",
            _asks(u, "polymarket_us"),
            lambda p: us.per_contract(p, coef),
            lambda c, p: us.fee(contracts=c, price=p, role="taker", coefficient=coef),
            _us_detail(us, "taker", coef),
        )
    pm = req.get("polymarket")
    if not isinstance(pm, dict):
        raise _bad("polymarket", "The second leg must be an object.")
    assert pf is not None
    asks = _asks(pm, "polymarket")
    rate = _category_rate(pm, "polymarket", pf)
    exponent = _exponent(pm, "polymarket.exponent")
    return kalshi, LegBook(
        "polymarket",
        asks,
        lambda p: rate * _pq(p) ** exponent,
        lambda c, p: polymarket_fee(contracts=c, price=p, rate=rate, exponent=exponent, role="taker"),
        {"fee_rate": rate, "exponent": exponent},
    )


def edge_at(a: LegBook, b: LegBook, pa: float, pb: float) -> float:
    return float(1 - pa - pb - a.fee_per_contract(pa) - b.fee_per_contract(pb))


def _units(size: float) -> int:
    from .fees import js_round

    return js_round(size * MICRO)


def walk_pair(a: LegBook, b: LegBook, min_edge: float, cap: int) -> tuple[int, float | None, str]:
    i = j = 0
    left_a, left_b = _units(a.levels[0]["size"]), _units(b.levels[0]["size"])
    taken = 0
    chunks: list[tuple[int, float]] = []
    cap_units = cap * MICRO
    limited_by = "min_edge"
    while True:
        if taken >= cap_units:
            limited_by = "max_contracts"
            break
        if i >= len(a.levels) or j >= len(b.levels):
            limited_by = f"{a.venue if i >= len(a.levels) else b.venue}_book"
            break
        edge = edge_at(a, b, a.levels[i]["price"], b.levels[j]["price"])
        if edge <= _EPSILON or edge < min_edge - _EPSILON:
            break
        chunk = min(left_a, left_b, cap_units - taken)
        chunks.append((taken, edge))
        taken += chunk
        left_a -= chunk
        left_b -= chunk
        if left_a == 0:
            i += 1
            if i < len(a.levels):
                left_a = _units(a.levels[i]["size"])
        if left_b == 0:
            j += 1
            if j < len(b.levels):
                left_b = _units(b.levels[j]["size"])
    contracts = taken // MICRO
    last = next((e for start, e in reversed(chunks) if start < contracts * MICRO), None)
    return contracts, last, limited_by


def fill_leg(book: LegBook, contracts: int) -> tuple[int, int, int, dict[str, Any]]:
    from .fees import js_round

    fills: list[dict[str, float]] = []
    left = contracts * MICRO
    cost = fee = 0
    for lvl in book.levels:
        if left == 0:
            break
        n = min(left, js_round(lvl["size"] * MICRO)) / MICRO
        c = (to_micro(lvl["price"]) * to_micro(n)) // MICRO
        f = book.fee(n, lvl["price"])
        fills.append({"price": lvl["price"], "contracts": n, "cost": dollars(c), "fee": dollars(f)})
        cost += c
        fee += f
        left -= js_round(n * MICRO)
    best = book.levels[0]["price"]
    best_cost = to_micro(best) * contracts
    leg = {
        "best_price": best,
        "average_price": round_to(dollars(cost) / contracts, 6) if contracts else None,
        "slippage": round_to(dollars(cost - best_cost) / contracts, 6) if contracts else 0,
        "slippage_cost": dollars(cost - best_cost),
        "cost": dollars(cost),
        "fee": dollars(fee),
        **book.detail,
        "fills": fills,
    }
    return cost, fee, cost - best_cost, leg


def size(req: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """How many contracts still clear ``min_edge`` after both taker fees, as ``POST /v0/size`` answers.

    Send the asks for the side you'd buy on each venue (YES on one, NO on the other). The walk keeps
    adding contracts while the next one still clears the edge, then prices the fills level by level.

        r = size({"min_edge": 0.01,
                  "kalshi": {"asks": [{"price": 0.42, "size": 100}]},
                  "polymarket_us": {"asks": [{"price": 0.53, "size": 150}]}})
        r["contracts"], r["net_profit"]
    """
    now = now or datetime.now(UTC)
    kf, pf, uf = _rules(now, req)
    _one_polymarket(req)
    min_edge = _num(req, "min_edge", "min_edge")
    min_edge = 0 if min_edge is None else min_edge
    if not (0 <= min_edge < 1) or not _micro_ok(min_edge):
        raise _bad(
            "min_edge",
            "min_edge is the smallest profit per contract after fees you'll take, in dollars: at least 0 and below 1.",
        )
    cap = req.get("max_contracts", MAX_CONTRACTS)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_CONTRACTS:
        raise _bad("max_contracts", "max_contracts caps the answer: a whole number from 1 to 1,000,000.")
    days = _days(req, now)
    a, b = _size_books(req, kf, pf, uf)
    contracts, last_edge, limited_by = walk_pair(a, b, min_edge, cap)
    ca, fa, sa, leg_a = fill_leg(a, contracts)
    cb, fb, sb, leg_b = fill_leg(b, contracts)
    payout = MICRO * contracts
    outlay = ca + cb + fa + fb
    net = payout - outlay
    ret = float(net) / float(outlay) if contracts else 0.0
    return {
        "contracts": contracts,
        "limited_by": limited_by,
        "min_edge": min_edge,
        "edge_at_best": round_to(edge_at(a, b, a.levels[0]["price"], b.levels[0]["price"]), 6),
        "edge_at_last": None if last_edge is None else round_to(last_edge, 6),
        "kalshi": leg_a,
        b.venue: leg_b,
        "payout": dollars(payout),
        "cost": dollars(ca + cb),
        "fees": dollars(fa + fb),
        "slippage_cost": dollars(sa + sb),
        "net_profit": dollars(net),
        "net_profit_per_contract": round_to(dollars(net) / contracts, 6) if contracts else 0,
        "return_pct": round_to(ret * 100, 2),
        **_hold_fields(days, ret),
        "profitable": net > 0,
    }


def pair_size(
    a: LegBook,
    b: LegBook,
    *,
    min_edge: float = 0.0,
    max_contracts: int = MAX_CONTRACTS,
    days: float | None = None,
) -> dict[str, Any]:
    """The ``POST /v0/size`` walk for any two legs: how many contracts clear ``min_edge`` after both fees.

    Returns the same fields as :func:`size`, with the legs under ``"a"`` and ``"b"``, plus the spread
    before fees: ``gross_spread`` (payout minus cost, so ``gross_spread - fees == net_profit``),
    ``gross_spread_per_contract`` and ``gross_at_best`` (``1 - ask a - ask b`` at the top of both books).
    ``limited_by`` names the leg by its ``LegBook.venue`` (``"<venue>_book"``), so give the legs
    distinct names. With ``days`` (how long the money is tied up), it adds ``days_held``,
    ``return_per_day_pct`` and ``annualized_return_pct``, rounded as :func:`profit` rounds them.
    """
    contracts, last_edge, limited_by = walk_pair(a, b, min_edge, max_contracts)
    ca, fa, sa, leg_a = fill_leg(a, contracts)
    cb, fb, sb, leg_b = fill_leg(b, contracts)
    payout = MICRO * contracts
    gross = payout - ca - cb
    outlay = ca + cb + fa + fb
    net = payout - outlay
    ret = float(net) / float(outlay) if contracts else 0.0
    return {
        "contracts": contracts,
        "limited_by": limited_by,
        "min_edge": min_edge,
        "edge_at_best": round_to(edge_at(a, b, a.levels[0]["price"], b.levels[0]["price"]), 6),
        "edge_at_last": None if last_edge is None else round_to(last_edge, 6),
        "gross_at_best": round_to(1 - a.levels[0]["price"] - b.levels[0]["price"], 6),
        "a": leg_a,
        "b": leg_b,
        "payout": dollars(payout),
        "cost": dollars(ca + cb),
        "fees": dollars(fa + fb),
        "slippage_cost": dollars(sa + sb),
        "gross_spread": dollars(gross),
        "gross_spread_per_contract": round_to(dollars(gross) / contracts, 6) if contracts else 0,
        "net_profit": dollars(net),
        "net_profit_per_contract": round_to(dollars(net) / contracts, 6) if contracts else 0,
        "return_pct": round_to(ret * 100, 2),
        **_hold_fields(days, ret),
        "profitable": net > 0,
    }
