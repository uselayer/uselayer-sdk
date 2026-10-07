"""Each venue's trading fee, from its published schedule.

A line-for-line port of Layer's ``lib/fees.ts``, checked against ``fee-golden.json`` to the millionth
of a dollar. The math runs on integers (millionths of a dollar) because float math breaks the
rounding: 0.07 × 100 × 0.5 × 0.5 is 1.7500000000000002, which a round-up to the cent would bill as
$1.76.

Rates come from the dated schedules in :mod:`uselayer.venue_rules`, looked up by the time of the
trade. The functions here take the rate as an argument, so they work for any date.

    from uselayer.fees import polymarket_us_fee, dollars
    dollars(polymarket_us_fee(contracts=100, price=0.5, coefficient=0.0695, role="taker"))  # 1.74
"""

from __future__ import annotations

import math
from typing import Literal

MICRO = 1_000_000
_M = MICRO
_FIVE_DP = 100_000

Role = Literal["taker", "maker"]


def js_round(x: float) -> int:
    """JavaScript's ``Math.round``: the nearest whole number, halves toward +infinity.

    Python's ``round`` sends halves to the even number, which would not match Layer's answers.

        js_round(2.5), js_round(-2.5)  # 3, -2
    """
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def round_to(n: float, places: int) -> float:
    """Layer's ``round(n, places)``: ``Math.round(n * 10 ** places) / 10 ** places``.

    round_to(1.23456, 2)  # 1.23
    """
    scale = 10.0**places
    return js_round(n * scale) / scale


def to_micro(dollars_: float) -> int:
    """Dollars to whole millionths of a dollar.

    to_micro(0.42)  # 420000
    """
    return js_round(dollars_ * MICRO)


def dollars(micro: int) -> float:
    """Millionths of a dollar to dollars.

    dollars(1_750_000)  # 1.75
    """
    return float(micro) / MICRO


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def _round_div(a: int, b: int) -> int:
    return (2 * a + b) // (2 * b)


def _round_half_even(a: int, b: int) -> int:
    """a / b to the nearest whole number, ties to even. a, b > 0."""
    q = a // b
    twice = (a % b) * 2
    if twice > b or (twice == b and q % 2 == 1):
        return q + 1
    return q


def kalshi_fee(*, contracts: float, price: float, rate: float, multiplier: float) -> int:
    """Fee in millionths of a dollar for one Kalshi order: round up(M × rate × C × P × (1 − P)) to the cent.

    ``contracts`` may have up to 6 decimal places (a fill against part of a book level).

        kalshi_fee(contracts=100, price=0.5, rate=0.07, multiplier=1)  # 1_750_000
    """
    p = to_micro(price)
    num = to_micro(multiplier) * to_micro(rate) * to_micro(contracts) * p * (_M - p)
    cents = _ceil_div(num * 100, _M**5)
    return cents * (_M // 100)


def polymarket_us_fee(
    *, contracts: float, price: float, coefficient: float, role: Role, maker_rebate: float = 0.0125
) -> int:
    """Fee in millionths of a dollar for one Polymarket US order: Θ × C × p × (1 − p), to the cent, half to even.

    Positive for takers; negative (a rebate) for makers. ``coefficient`` is the market's taker Θ.

        polymarket_us_fee(contracts=1000, price=0.5, coefficient=0.0695, role="taker")  # 17_380_000
    """
    theta = maker_rebate if role == "maker" else coefficient
    p = to_micro(price)
    num = to_micro(theta) * to_micro(contracts) * p * (_M - p)
    cents = 0 if num == 0 else _round_half_even(num * 100, _M**4)
    fee = cents * (_M // 100)
    return -fee if role == "maker" else fee


def polymarket_us_premium_fee(
    *, contracts: float, price: float, rate: float, increment: float = 0.01, minimum: float = 0.0
) -> int:
    """Fee in millionths of a dollar for one Polymarket US order in its premium era (before 2026-04-03).

    A share of the premium: rate × C × p, to the nearest ``increment`` (half to even: the pages say
    "nearest" without a tie rule; even is the one Polymarket US states now), and at least ``minimum``
    when there's a premium. 1 bp was rounded to $0.0001 with a $0.0001 minimum, 10 bp to $0.001 with a
    $0.001 minimum, 30 bp to the cent. Also prices that era's maker rebate (pass its rate, no minimum).

        polymarket_us_premium_fee(contracts=100, price=0.55, rate=0.003)  # 160_000 ($0.165 → $0.16)
        polymarket_us_premium_fee(contracts=100, price=0.464, rate=0.0001, increment=0.0001, minimum=0.0001)  # 4_600
    """
    p = to_micro(price)
    num = to_micro(rate) * to_micro(contracts) * p  # millionths of a dollar × _M²
    if num == 0:
        return 0
    step = to_micro(increment)
    fee = _round_half_even(num, _M * _M * step) * step
    return max(fee, to_micro(minimum))


def polymarket_fee(*, contracts: float, price: float, rate: float, exponent: int, role: Role) -> int:
    """Fee in millionths of a dollar for one Polymarket order: C × rate × (p × (1 − p))^exponent, to 5 decimal places.

    Makers never pay.

        polymarket_fee(contracts=100, price=0.5, rate=0.07, exponent=1, role="taker")  # 1_750_000
    """
    if role == "maker":
        return 0
    p = to_micro(price)
    num = to_micro(contracts) * to_micro(rate) * (p * (_M - p)) ** exponent
    den = _M * _M * (_M * _M) ** exponent
    units = _round_div(num * _FIVE_DP, den)
    return units * (_M // _FIVE_DP)
