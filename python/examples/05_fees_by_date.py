"""Fees come from the schedule in force at the time of the trade.

python examples/05_fees_by_date.py
"""

from datetime import UTC, datetime

from uselayer import FeeSettings, calculate_fee, rules_at
from uselayer.fees import dollars

now = datetime.now(UTC)
r = rules_at("polymarket_us", now)
print(f"Polymarket US schedule from {r.effective_from.date()} ({r.source})")
fee = calculate_fee(FeeSettings(venue="polymarket_us"), contracts=100, price=0.5, role="taker", at=now)
print(f"100 contracts at $0.50, taker: ${dollars(fee)}")

for when in (datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 11, tzinfo=UTC)):
    s = FeeSettings(venue="polymarket", category="sports")
    print(
        f"Polymarket sports, {when.date()}: ${dollars(calculate_fee(s, contracts=100, price=0.5, role='taker', at=when))}"
    )
