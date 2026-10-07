"""How each venue worked at a given time: fees, order types, tick sizes and minimum sizes.

Venues change their fees and rules. A replay of last month's trade has to use last month's fees, so
every schedule here is a dated entry with the date it took effect and the page it comes from.
:func:`rules_at` picks the entry in force at a moment. Before the earliest entry Layer knows, it
raises ``no_venue_rules`` instead of guessing.

A market's own settings win over the venue default where the venue publishes them per market
(Polymarket US ``feeCoefficient``, ``orderPriceMinTickSize`` and ``minimumTradeQty``; Polymarket
``feeSchedule``).

    from datetime import datetime, timezone
    from uselayer.venue_rules import rules_at
    r = rules_at("polymarket_us", datetime(2026, 10, 1, tzinfo=timezone.utc))
    r.fees.taker_coefficient, r.effective_from.date()  # 0.0695, 2026-09-17
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .errors import VenueError
from .fees import Role, polymarket_us_fee, polymarket_us_premium_fee

_ET_DAYLIGHT = timezone(timedelta(hours=-4), "EDT")
_ET_STANDARD = timezone(timedelta(hours=-5), "EST")


@dataclass(frozen=True)
class KalshiFees:
    """Kalshi: fee = round up(multiplier × rate × C × P × (1 − P)) to the cent."""

    taker_rate: float
    # The maker rate each series fee_type adds.
    maker_rates: dict[str, float]


@dataclass(frozen=True)
class PolymarketFees:
    """Polymarket: fee = C × rate × (p × (1 − p))^exponent, takers only, to 5 decimal places.

    ``category_rates`` holds the default taker rate per category. A market's own
    ``feeSchedule.rate`` wins when it has one. A category missing here is unknown for that period.
    """

    category_rates: dict[str, float]


@dataclass(frozen=True)
class PolymarketUSFees:
    """Polymarket US: fee = Θ × C × p × (1 − p), to the cent, half to even; makers get a rebate."""

    taker_coefficient: float
    maker_rebate: float

    def fee(self, *, contracts: float, price: float, role: Role, coefficient: float | None = None) -> int:
        """One order's fee in millionths of a dollar; ``coefficient`` is the market's own Θ, if it gives one."""
        theta = self.taker_coefficient if coefficient is None else coefficient
        return polymarket_us_fee(
            contracts=contracts, price=price, coefficient=theta, role=role, maker_rebate=self.maker_rebate
        )

    def per_contract(self, price: float, coefficient: float | None = None) -> float:
        """The unrounded taker fee for one contract at ``price``."""
        return (self.taker_coefficient if coefficient is None else coefficient) * price * (1 - price)


@dataclass(frozen=True)
class PolymarketUSPremiumFees:
    """Polymarket US before 2026-04-03: a share of the premium (C × p), not Θ × C × p × (1 − p).

    Takers pay ``taker_rate`` × C × p and makers get ``maker_rebate_rate`` × C × p back, each to the
    nearest ``rounding`` dollars; a taker fee is at least ``minimum_fee``. A market's own
    ``coefficient`` is a Θ: passed for a time in this era, it prices the taker fee as
    Θ × C × p × (1 − p), to the cent, instead of the premium share.
    """

    taker_rate: float
    maker_rebate_rate: float
    rounding: float
    minimum_fee: float

    def fee(self, *, contracts: float, price: float, role: Role, coefficient: float | None = None) -> int:
        """One order's fee in millionths of a dollar: positive for takers, a rebate (negative) for makers."""
        if role == "maker":
            return -polymarket_us_premium_fee(
                contracts=contracts, price=price, rate=self.maker_rebate_rate, increment=self.rounding
            )
        if coefficient is not None:
            return polymarket_us_fee(contracts=contracts, price=price, coefficient=coefficient, role="taker")
        return polymarket_us_premium_fee(
            contracts=contracts,
            price=price,
            rate=self.taker_rate,
            increment=self.rounding,
            minimum=self.minimum_fee,
        )

    def per_contract(self, price: float, coefficient: float | None = None) -> float:
        """The unrounded taker fee for one contract at ``price``."""
        return self.taker_rate * price if coefficient is None else coefficient * price * (1 - price)


# Either era of Polymarket US's fees: both price an order with .fee() and .per_contract().
PolymarketUSSchedule = PolymarketUSFees | PolymarketUSPremiumFees


@dataclass(frozen=True)
class VenueRules:
    """One dated entry: how ``venue`` worked from ``effective_from`` until the next entry."""

    venue: str
    effective_from: datetime
    source: str
    fees: KalshiFees | PolymarketFees | PolymarketUSSchedule
    # Order types this SDK sends: every order has a limit price.
    time_in_force: tuple[str, ...] = ("ioc", "fok", "gtc")
    # Venue defaults; a market's own value wins when the venue gives one.
    tick_size: float = 0.01
    min_size: float = 1.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """The entry as plain data."""
        return {
            "venue": self.venue,
            "effective_from": self.effective_from.isoformat(),
            "source": self.source,
            "fees": self.fees.__dict__,
            "time_in_force": list(self.time_in_force),
            "tick_size": self.tick_size,
            "min_size": self.min_size,
            "notes": list(self.notes),
        }


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


# Oldest first per venue. Add a new entry when a venue changes a fee or rule; never edit an old one.
ENTRIES: tuple[VenueRules, ...] = (
    VenueRules(
        venue="kalshi",
        # "Last updated and effective: Oct 1, 2025"; read as 00:00 US Eastern. Its 0.07 taker rate is
        # the one Kalshi has published since 2021 (the July 25, 2021 schedule's table onward).
        effective_from=datetime(2025, 10, 1, tzinfo=_ET_DAYLIGHT),
        source="https://web.archive.org/web/20251008232930/https://kalshi.com/docs/kalshi-fee-schedule.pdf",
        fees=KalshiFees(
            taker_rate=0.07,
            # Maker fees round up(0.0175 × C × P × (1 − P)), only on the markets Kalshi lists for them.
            # This schedule has no combo maker fee, so that fee_type has no rate here.
            maker_rates={"quadratic": 0.0, "quadratic_with_maker_fees": 0.0175},
        ),
        tick_size=0.01,
        min_size=1.0,
        notes=(
            "Kalshi fee schedule effective 2025-10-01 (Wayback copy of 2025-10-08): taker 0.07, maker 0.0175 on listed markets.",
        ),
    ),
    VenueRules(
        venue="kalshi",
        # The schedule names the day only; read as the start of that day, US Eastern.
        effective_from=datetime(2026, 7, 7, tzinfo=_ET_DAYLIGHT),
        source="https://kalshi.com/docs/kalshi-fee-schedule.pdf",
        fees=KalshiFees(
            taker_rate=0.07,
            maker_rates={
                "quadratic": 0.0,
                "quadratic_with_maker_fees": 0.0175,
                "quadratic_with_combo_maker_fees": 0.035,
            },
        ),
        tick_size=0.01,
        min_size=1.0,
        notes=("Fee schedule effective 2026-07-07; the day is read as starting at 00:00 US Eastern.",),
    ),
    VenueRules(
        venue="polymarket",
        effective_from=_utc(2026, 3, 30),
        source="https://docs.polymarket.com/changelog/predictions",
        # Fee Structure V2 started category fees on 2026-03-30. The sports rate until 2026-07-10 is
        # in the changelog; the other categories' rates for this period aren't, so they're unknown.
        fees=PolymarketFees(category_rates={"sports": 0.03}),
        tick_size=0.01,
        min_size=5.0,
        notes=("Only the sports rate (0.03) is known for this period.",),
    ),
    VenueRules(
        venue="polymarket",
        effective_from=_utc(2026, 7, 10),
        source="https://docs.polymarket.com/trading/fees",
        fees=PolymarketFees(
            category_rates={
                "crypto": 0.07,
                "sports": 0.05,
                "finance": 0.04,
                "politics": 0.04,
                "economics": 0.05,
                "culture": 0.05,
                "weather": 0.05,
                "climate": 0.05,
                "other": 0.05,
                "mentions": 0.04,
                "tech": 0.04,
                "geopolitics": 0.0,
                "world": 0.0,
            }
        ),
        tick_size=0.01,
        min_size=5.0,
        notes=(
            "Sports taker rate rose from 0.03 to 0.05 at midnight UTC on 2026-07-10 (Polymarket changelog).",
            "'climate' and 'world' are Layer's names for Polymarket's weather and geopolitics categories.",
        ),
    ),
    # Polymarket US (QCX LLC), from its fee pages and the fee filings it self-certifies with the CFTC
    # (copies at polymarketexchange.com/files/notices). A filing "effective one (1) business day
    # following certification" with no time is read from 00:00 US Eastern that day. Volume and
    # promotional taker rebates (from 2026-04-03, paid weekly) aren't here: they depend on a trader's
    # volume, not on one trade.
    VenueRules(
        venue="polymarket_us",
        # The earliest copy of the fee page (2025-11-03; the exchange was already trading). The app
        # opened to the public on 2025-12-03 with this fee.
        effective_from=datetime(2025, 11, 3, tzinfo=_ET_STANDARD),
        source="https://web.archive.org/web/20251103153146/https://polymarketexchange.com/fees-hours.html",
        fees=PolymarketUSPremiumFees(
            taker_rate=0.0001, maker_rebate_rate=0.0, rounding=0.0001, minimum_fee=0.0001
        ),
        tick_size=0.001,
        min_size=1.0,
        notes=(
            "Taker fee 1 bp of the total contract premium; makers pay nothing.",
            "'All fees are rounded to the nearest basis point (0.01%). The minimum fee for any trade is 1 basis point ($0.0001).'",
        ),
    ),
    VenueRules(
        venue="polymarket_us",
        # Certified Thursday 2026-01-08, effective one business day later.
        effective_from=datetime(2026, 1, 9, tzinfo=_ET_STANDARD),
        source="https://www.polymarketexchange.com/files/notices/Fee%20Schedule%20%282026.01.08%29.pdf",
        fees=PolymarketUSPremiumFees(
            taker_rate=0.001, maker_rebate_rate=0.0, rounding=0.001, minimum_fee=0.001
        ),
        tick_size=0.001,
        min_size=1.0,
        notes=(
            "Taker fee from 1 bp to 10 bp of the total contract premium; makers 'are not charged fees'.",
            "'All fees are rounded to the nearest $0.0010 (one-tenth of a cent). The minimum fee for any trade is $0.0010.' (fee page, 2026-02-19)",
        ),
    ),
    VenueRules(
        venue="polymarket_us",
        # Certified Monday 2026-03-02, effective one business day later.
        effective_from=datetime(2026, 3, 3, tzinfo=_ET_STANDARD),
        source="https://www.polymarketexchange.com/files/notices/Maker%20Rebate%20%282026.03.02%29.pdf",
        fees=PolymarketUSPremiumFees(
            taker_rate=0.001, maker_rebate_rate=0.001, rounding=0.001, minimum_fee=0.001
        ),
        tick_size=0.001,
        min_size=1.0,
        notes=(
            "Maker rebate of 10 bp of the total contract premium; the 10 bp taker fee is unchanged.",
            "No fee page from this week was found: the rebate is rounded like the fee, to $0.001 (an assumption).",
        ),
    ),
    VenueRules(
        venue="polymarket_us",
        # Certified Friday 2026-03-06, effective one business day later. The fee page still showed 10 bp
        # at 21:37 UTC on 03-09 and the docs showed 30 bp by 19:28 UTC on 03-10: the go-live hour isn't
        # known, so the filing's day is used (the higher fee, earlier).
        effective_from=datetime(2026, 3, 9, tzinfo=_ET_DAYLIGHT),
        source="https://www.cftc.gov/filings/orgrules/rules03062640472.pdf",
        fees=PolymarketUSPremiumFees(
            taker_rate=0.003, maker_rebate_rate=0.002, rounding=0.01, minimum_fee=0.0
        ),
        tick_size=0.001,
        min_size=1.0,
        notes=(
            "Taker fee from 10 bp to 30 bp, maker rebate from 10 bp to 20 bp, of the total contract premium.",
            "'All fees and rebates are rounded to the nearest $0.01' (docs.polymarket.us/faqs/fees, 2026-03-10).",
            "Go-live hour unverified: dated from 00:00 ET on the filing's effective day.",
        ),
    ),
    VenueRules(
        venue="polymarket_us",
        # "Effective exchange-wide from 3pm ET, Friday April 3, 2026."
        effective_from=datetime(2026, 4, 3, 15, 0, tzinfo=_ET_DAYLIGHT),
        source="https://www.polymarketexchange.com/files/notices/Exchange%20Fees%20Update%20%282026.03.27%29.pdf",
        fees=PolymarketUSFees(taker_coefficient=0.05, maker_rebate=0.0125),
        tick_size=0.001,
        min_size=1.0,
        notes=(
            "Taker Θ 0.05 × C × p × (1 − p); maker rebate Θ 0.0125, at the trade; to the cent, half to even.",
            "A 50% taker rebate, paid weekly, isn't counted.",
        ),
    ),
    VenueRules(
        venue="polymarket_us",
        # "Effective 12:00 AM ET July 1, 2026."
        effective_from=datetime(2026, 7, 1, tzinfo=_ET_DAYLIGHT),
        source="https://www.cftc.gov/filings/orgrules/rules0707269057.pdf",
        fees=PolymarketUSFees(taker_coefficient=0.06, maker_rebate=0.0125),
        tick_size=0.001,
        min_size=1.0,
        notes=("Taker Θ from 0.05 to 0.06; the maker rebate coefficient is unchanged.",),
    ),
    VenueRules(
        venue="polymarket_us",
        # The filing: it "took effect on Thursday, September 17, 2026 at 12:00 a.m. ET". 0.4.0 dated it
        # 2026-09-25, from the banner on docs.polymarket.us/fees.
        effective_from=datetime(2026, 9, 17, tzinfo=_ET_DAYLIGHT),
        source="https://www.cftc.gov/filings/orgrules/rules09212628660.pdf",
        fees=PolymarketUSFees(taker_coefficient=0.0695, maker_rebate=0.0125),
        tick_size=0.001,
        min_size=1.0,
        notes=(
            "Taker Θ from 0.06 to 0.0695; the at-trade maker rebate is unchanged (docs.polymarket.us/fees).",
            "Combo (parlay) markets have their own fee curve; this SDK doesn't trade them.",
        ),
    ),
)


def rules_at(venue: str, at: datetime) -> VenueRules:
    """The entry for ``venue`` in force at ``at`` (a timezone-aware time).

        rules_at("polymarket", datetime(2026, 8, 1, tzinfo=timezone.utc)).fees.category_rates["sports"]  # 0.05

    Raises ``VenueError("no_venue_rules")`` before the earliest entry Layer has for that venue.
    """
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware, e.g. datetime.now(timezone.utc)")
    entries = [e for e in ENTRIES if e.venue == venue]
    if not entries:
        raise VenueError(
            "no_venue_rules", f"Layer has no rules for venue {venue!r}.", venue=venue, retryable=False
        )
    in_force = [e for e in entries if e.effective_from <= at]
    if not in_force:
        first = min(e.effective_from for e in entries)
        raise VenueError(
            "no_venue_rules",
            f"Layer has no {venue} fee schedule before {first.isoformat()}.",
            venue=venue,
            hint="Replays and fee lookups work from that date on. Earlier schedules aren't recorded yet.",
            next=f"Use a time on or after {first.isoformat()}.",
            retryable=False,
        )
    return max(in_force, key=lambda e: e.effective_from)


def polymarket_category_rate(category: str, at: datetime) -> float:
    """Polymarket's default taker rate for a category at a time.

    polymarket_category_rate("sports", datetime(2026, 5, 1, tzinfo=timezone.utc))  # 0.03
    """
    r = rules_at("polymarket", at)
    assert isinstance(r.fees, PolymarketFees)
    key = category.lower()
    if key not in r.fees.category_rates:
        raise VenueError(
            "no_venue_rules",
            f"Polymarket's {key!r} rate isn't known for {at.date().isoformat()}.",
            venue="polymarket",
            hint="Pass the market's own feeSchedule.rate instead.",
            next="Read the market's feeSchedule from Polymarket and pass fee_rate.",
            retryable=False,
        )
    return r.fees.category_rates[key]


def history(venue: str) -> list[VenueRules]:
    """Every entry for a venue, oldest first."""
    return sorted((e for e in ENTRIES if e.venue == venue), key=lambda e: e.effective_from)
