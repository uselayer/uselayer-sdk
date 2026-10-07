"""Layer's fill model: what an order would fill against a book, and what it would cost.

Paper and backtest mode fill every order through :func:`estimate_fill`, and :func:`calculate_fee`
prices every fill with the fee schedule in force at the time (see :mod:`uselayer.venue_rules`). The
walk is the same one ``POST /v0/size`` uses: level by level from the best price, each level's fee
worked out separately with the venue's rounding.

A resting (maker) order waits in line at its price: :mod:`uselayer.resting` estimates how many
contracts are ahead of it and fills it from trades and later books (:func:`maker_fill`). What the
model still can't know: the venue's real line (only totals per price are published) and how fast the
venue answers.

    est = estimate_fill(order, book, FeeSettings(venue="polymarket_us", coefficient=0.0695), at=now)
    est.filled, est.avg_price, est.fees
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .books import Book, Level
from .errors import VenueError
from .fees import MICRO, dollars, js_round, kalshi_fee, polymarket_fee, round_to, to_micro
from .orders import Order
from .venue_rules import (
    KalshiFees,
    PolymarketFees,
    PolymarketUSFees,
    PolymarketUSPremiumFees,
    polymarket_category_rate,
    rules_at,
)

Role = Literal["taker", "maker"]


@dataclass(frozen=True)
class FeeSettings:
    """A market's own fee settings. Leave a field ``None`` to use the venue's default at the time.

    - Polymarket US: ``coefficient`` (the market's ``feeCoefficient``).
    - Polymarket: ``rate`` (the market's ``feeSchedule.rate``) or ``category``, and ``exponent``.
    - Kalshi: ``multiplier`` (the series' ``fee_multiplier``) and ``fee_type``.
    """

    venue: str
    coefficient: float | None = None
    rate: float | None = None
    category: str | None = None
    exponent: int = 1
    multiplier: float = 1.0
    fee_type: str = "quadratic"


def calculate_fee(settings: FeeSettings, *, contracts: float, price: float, role: Role, at: datetime) -> int:
    """The fee in millionths of a dollar for one fill, with the venue's schedule in force at ``at``.

    calculate_fee(FeeSettings(venue="polymarket_us"), contracts=100, price=0.5, role="taker", at=now)  # 1_740_000
    """
    r = rules_at(settings.venue, at)
    if isinstance(r.fees, (PolymarketUSFees, PolymarketUSPremiumFees)):
        return r.fees.fee(contracts=contracts, price=price, role=role, coefficient=settings.coefficient)
    if isinstance(r.fees, PolymarketFees):
        rate = settings.rate
        if rate is None:
            if settings.category is None:
                raise VenueError(
                    "invalid_order",
                    "Polymarket needs the market's fee rate or category.",
                    venue="polymarket",
                    hint="Pass FeeSettings(venue='polymarket', rate=<feeSchedule.rate>) or category=.",
                    retryable=False,
                )
            rate = polymarket_category_rate(settings.category, at)
        return polymarket_fee(
            contracts=contracts, price=price, rate=rate, exponent=settings.exponent, role=role
        )
    assert isinstance(r.fees, KalshiFees)
    rate = r.fees.taker_rate if role == "taker" else r.fees.maker_rates.get(settings.fee_type, 0.0)
    return kalshi_fee(contracts=contracts, price=price, rate=rate, multiplier=settings.multiplier)


def fee_per_contract(settings: FeeSettings, price: float, *, at: datetime) -> float:
    """The unrounded taker fee for one contract at ``price``: what the size walk uses to decide the edge."""
    r = rules_at(settings.venue, at)
    pq = price * (1 - price)
    if isinstance(r.fees, (PolymarketUSFees, PolymarketUSPremiumFees)):
        return r.fees.per_contract(price, settings.coefficient)
    if isinstance(r.fees, PolymarketFees):
        rate = (
            settings.rate
            if settings.rate is not None
            else polymarket_category_rate(settings.category or "other", at)
        )
        return float(rate * pq**settings.exponent)
    assert isinstance(r.fees, KalshiFees)
    return settings.multiplier * r.fees.taker_rate * pq


@dataclass(frozen=True)
class EstimatedFill:
    """One level of a fill: ``contracts`` at ``price``, what they cost and their fee, in dollars."""

    price: float
    contracts: float
    cost: float
    fee: float
    role: Role

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class FillEstimate:
    """What an order would do against a book right now.

    ``filled`` contracts at ``avg_price``, costing ``cost`` plus ``fees`` (dollars). ``rests`` is how
    many contracts would wait in the book (``gtc`` orders only). ``would_cross`` is set when a
    post-only order would fill at once, which the venue rejects.
    """

    fills: tuple[EstimatedFill, ...]
    filled: float
    avg_price: float | None
    cost: float
    fees: float
    rests: float
    would_cross: bool
    book_as_of: datetime
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["fills"] = [f.to_dict() for f in self.fills]
        d["book_as_of"] = self.book_as_of.isoformat()
        d["notes"] = list(self.notes)
        return d


def _crossing(order: Order, book: Book) -> list[Level]:
    ob = book.outcome(order.side)
    if order.action == "buy":
        return [lv for lv in ob.asks if to_micro(lv.price) <= to_micro(order.price)]
    return [lv for lv in ob.bids if to_micro(lv.price) >= to_micro(order.price)]


def estimate_fill(
    order: Order, book: Book, settings: FeeSettings, *, at: datetime, size: float | None = None
) -> FillEstimate:
    """Fill ``order`` (or ``size`` of it) against ``book`` as a taker, level by level.

    Buys take asks at or below the limit price; sells take bids at or above it. Each level is billed
    on its own, with the venue's rounding, the way ``POST /v0/size`` prices fills. An ``fok`` order
    fills completely or not at all; a ``post_only`` order never takes.

        estimate_fill(order, book, FeeSettings(venue="polymarket_us"), at=now).fees
    """
    want = order.remaining if size is None else size
    crossing = _crossing(order, book)
    if order.post_only:
        return FillEstimate((), 0.0, None, 0.0, 0.0, want, bool(crossing), book.as_of)
    available = sum(js_round(lv.size * MICRO) for lv in crossing)
    left = js_round(want * MICRO)
    if order.tif == "fok" and available < left:
        return FillEstimate(
            (),
            0.0,
            None,
            0.0,
            0.0,
            0.0,
            False,
            book.as_of,
            ("fok: not enough at the limit price; nothing filled",),
        )
    fills: list[EstimatedFill] = []
    cost = fee = taken = 0
    for lv in crossing:
        if left == 0:
            break
        n_units = min(left, js_round(lv.size * MICRO))
        n = n_units / MICRO
        c = (to_micro(lv.price) * to_micro(n)) // MICRO
        f = calculate_fee(settings, contracts=n, price=lv.price, role="taker", at=at)
        fills.append(EstimatedFill(lv.price, n, dollars(c), dollars(f), "taker"))
        cost += c
        fee += f
        taken += n_units
        left -= n_units
    filled = taken / MICRO
    rests = left / MICRO if order.tif == "gtc" else 0.0
    avg = round_to(dollars(cost) / filled, 6) if filled else None
    return FillEstimate(tuple(fills), filled, avg, dollars(cost), dollars(fee), rests, False, book.as_of)


def maker_fill(
    order: Order, units: int, settings: FeeSettings, *, at: datetime, as_of: datetime
) -> FillEstimate:
    """Fill ``units`` millionths of a contract of a resting order, at its own price, as a maker.

    How much fills comes from :mod:`uselayer.resting` (the order's place in line); ``as_of`` is the
    venue time of the book or trade that filled it.
    """
    n_units = min(js_round(order.remaining * MICRO), units)
    if n_units <= 0:
        return FillEstimate((), 0.0, None, 0.0, 0.0, order.remaining, False, as_of)
    n = n_units / MICRO
    c = (to_micro(order.price) * to_micro(n)) // MICRO
    f = calculate_fee(settings, contracts=n, price=order.price, role="maker", at=at)
    rests = (js_round(order.remaining * MICRO) - n_units) / MICRO
    return FillEstimate(
        (EstimatedFill(order.price, n, dollars(c), dollars(f), "maker"),),
        n,
        order.price,
        dollars(c),
        dollars(f),
        rests,
        False,
        as_of,
    )
