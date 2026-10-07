"""Fee settings and payout time for a Kalshi ↔ Polymarket US pair, read from the venues on your machine.

What Layer's ``POST /v0/profit`` fills in when it's sent ``kalshi.market_id``, done here with your own
venue keys instead (a port of Layer's ``fillFromMatch`` and ``payoutTimes``):

- ``kalshi.fee_multiplier`` and ``kalshi.fee_type``: the market's series, from Kalshi's API.
- ``polymarket_us.fee_coefficient``: the market's ``feeCoefficient``, from Polymarket US. A market that
  gives none is priced at the venue's published taker rate, as Layer does.
- ``days_held``: until the pair's expected payout, from each venue's own market times.

Anything the request already says wins. The math is :func:`uselayer.calc.profit`, unchanged: it never
touches the network.

    client.fees(match)                    # the settings alone
    client.profit({"contracts": 100, "kalshi": {"price": 0.42}, "polymarket_us": {"price": 0.55}}, pair=match)
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import calc
from .errors import VenueError
from .venue_rules import KalshiFees, rules_at
from .venues.base import MarketInfo

if TYPE_CHECKING:
    from .client import Client

DAY_MS = 86_400_000
# From the later event time to the money being back: Polymarket's result is proposed after the game and
# confirmed after a challenge window, and its event time for a game is the kickoff (Layer's PAYOUT_BUFFER_MS).
PAYOUT_BUFFER_MS = 6 * 60 * 60 * 1000

_FRACTION = re.compile(r"\.(\d+)")


def time_ms(s: str | None) -> int | None:
    """A venue's time string as whole milliseconds since 1970 (as JavaScript's ``Date.parse``), or None."""
    if not s:
        return None
    t = _FRACTION.sub(lambda m: "." + m.group(1)[:3].ljust(3, "0"), s.strip().replace("Z", "+00:00"), count=1)
    try:
        when = datetime.fromisoformat(t)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return round(when.timestamp() * 1000)


def iso_ms(ms: int | None) -> str | None:
    """Milliseconds as ``2026-10-05T17:00:00.000Z`` (JavaScript's ``toISOString``)."""
    if ms is None:
        return None
    when = datetime.fromtimestamp(ms / 1000, UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def payout_times(legs: list[MarketInfo]) -> tuple[int | None, int | None]:
    """When the pair's money is expected back, and the latest it can be, in milliseconds.

    Both legs pay once both venues settle, so it's the later venue. ``latest`` is the later close time.
    ``expected`` is the later event time (a market without one counts its close time) plus
    :data:`PAYOUT_BUFFER_MS`, never past ``latest``.
    """
    closes = [t for t in (time_ms(m.end_date) for m in legs) if t is not None]
    events = [t for t in (time_ms(m.event_time) or time_ms(m.end_date) for m in legs) if t is not None]
    latest = max(closes) if closes else None
    if not events:
        return latest, latest
    expected = max(events) + PAYOUT_BUFFER_MS
    return (latest if latest is not None and latest < expected else expected), latest


def days_until(expected_ms: int, now: datetime) -> float:
    """Days from ``now`` until a payout time in milliseconds, at least 1 (Layer's floor for ``days_held``)."""
    return max(1.0, (expected_ms - math.floor(now.timestamp() * 1000)) / DAY_MS)


def _not_available(message: str, hint: str, next_: str) -> VenueError:
    return VenueError("not_available", message, retryable=False, hint=hint, next=next_)


def kalshi_and_us(pair: Any) -> tuple[str, str]:
    """The Kalshi ticker and the Polymarket US market of a pair, whichever order it gives them in."""
    from .trading import legs_of

    legs = dict(legs_of(pair))
    if set(legs) != {"kalshi", "polymarket_us"}:
        if "polymarket" in legs:
            raise _not_available(
                "The fee lookup reads Kalshi and Polymarket US; this SDK doesn't read Polymarket.",
                'Send Polymarket\'s fee yourself, e.g. "polymarket": {"price": 0.55, "fee_rate": 0.05}, to calc.profit().',
                "uselayer.calc.profit(request)",
            )
        raise VenueError(
            "invalid_order",
            "The fee lookup needs one Kalshi market and one Polymarket US market.",
            retryable=False,
            hint="Pass a Kalshi ↔ Polymarket US Match from client.matches(venue='polymarket_us'), "
            "or [('kalshi', ticker), ('polymarket_us', slug)].",
        )
    return legs["kalshi"], legs["polymarket_us"]


def pair_fees(client: Client, pair: Any) -> dict[str, Any]:
    """Each leg's fee settings, the days until payout and the payout times, from the venues now.

    {"kalshi": {"fee_type": "quadratic", "fee_multiplier": 1.0},
     "polymarket_us": {"fee_coefficient": 0.0695},       # left out when the market gives none
     "days_held": 2.25,                                  # left out when neither venue gives a time
     "match": {"kalshi": "KX...", "polymarket_us": "slug",
               "expected_payout_at": "...Z", "latest_payout_at": "...Z"}}
    """
    if client.mode == "backtest":
        raise _not_available(
            "The fee lookup reads the venues now, so it doesn't run in backtest mode.",
            "Look the settings up once with a paper client, or pass fee_multiplier, fee_coefficient and "
            "days_held yourself.",
            "Client().fees(pair)",
        )
    kalshi_id, us_id = kalshi_and_us(pair)
    k = client.market(kalshi_id, venue="kalshi")
    u = client.market(us_id, venue="polymarket_us")
    expected, latest = payout_times([k, u])
    out: dict[str, Any] = {
        "kalshi": {"fee_type": k.fees.fee_type, "fee_multiplier": k.fees.multiplier},
        "polymarket_us": {} if u.fees.coefficient is None else {"fee_coefficient": u.fees.coefficient},
    }
    if expected is not None:
        out["days_held"] = days_until(expected, client._now())
    out["match"] = {
        "kalshi": kalshi_id,
        "polymarket_us": us_id,
        "expected_payout_at": iso_ms(expected),
        "latest_payout_at": iso_ms(latest),
    }
    return out


def _twin(client: Client, kalshi_id: str) -> str:
    """The Polymarket US twin of a Kalshi market, from Layer's ``GET /v0/match``."""
    found = client._layer.match(kalshi_id, venue="kalshi", with_="polymarket_us")
    twin = found.get("matched_market") if isinstance(found, dict) else None
    mid = twin.get("market_id") if isinstance(twin, dict) else None
    if not isinstance(mid, str):
        reason = found.get("reason") if isinstance(found, dict) else None
        raise VenueError(
            "not_found",
            f"kalshi.market_id: {kalshi_id} has no live match on Polymarket US ({reason or 'not_indexed'}).",
            venue="layer",
            retryable=False,
            hint=f"Check the ticker with client.match({kalshi_id!r}, venue='kalshi', with_='polymarket_us'), "
            "or leave market_id out and send the fees yourself.",
            next="client.matches(venue='polymarket_us')",
        )
    return mid


def profit(client: Client, request: Mapping[str, Any], pair: Any = None) -> dict[str, Any]:
    """``POST /v0/profit``, with the fee lookup done on your machine. See :meth:`Client.profit`."""
    req: dict[str, Any] = copy.deepcopy(dict(request))
    k = req.get("kalshi")
    market_id = k.pop("market_id", None) if isinstance(k, dict) else None
    if market_id is not None and (not isinstance(market_id, str) or not market_id):
        raise VenueError(
            "invalid_order",
            "kalshi.market_id is not valid.",
            retryable=False,
            hint="kalshi.market_id is a Kalshi ticker, e.g. KXNFLGAME-26OCT12BUFLAR-BUF.",
        )
    now = client._now()
    if market_id is None and pair is None:
        return calc.profit(req, now)
    if req.get("polymarket") is not None:
        raise _not_available(
            "The fee lookup reads Polymarket US; this SDK doesn't read Polymarket.",
            "Send Polymarket's fee_rate yourself and leave market_id out.",
            "uselayer.calc.profit(request)",
        )
    calc.profit(req, now)  # every field checked before any venue is asked, as Layer checks the body first
    if pair is None:
        pair = [("kalshi", market_id), ("polymarket_us", _twin(client, str(market_id)))]
    elif market_id is not None and kalshi_and_us(pair)[0] != market_id:
        raise VenueError(
            "invalid_order",
            f"kalshi.market_id {market_id} isn't the Kalshi market in pair.",
            retryable=False,
            hint="Send one of them: the pair, or kalshi.market_id.",
        )
    found = pair_fees(client, pair)
    filled: list[str] = []
    kal, us = req["kalshi"], req["polymarket_us"]
    if kal.get("fee_multiplier") is None:
        kal["fee_multiplier"] = found["kalshi"]["fee_multiplier"]
        filled.append("kalshi.fee_multiplier")
    kf = rules_at("kalshi", now).fees
    # A fee_type the schedule has no maker rate for keeps Layer's default, "quadratic".
    if (
        kal.get("fee_type") is None
        and isinstance(kf, KalshiFees)
        and found["kalshi"]["fee_type"] in kf.maker_rates
    ):
        kal["fee_type"] = found["kalshi"]["fee_type"]
        filled.append("kalshi.fee_type")
    if us.get("fee_coefficient") is None and "fee_coefficient" in found["polymarket_us"]:
        us["fee_coefficient"] = found["polymarket_us"]["fee_coefficient"]
        filled.append("polymarket_us.fee_coefficient")
    if req.get("settles_at") is None and req.get("days_held") is None and "days_held" in found:
        req.pop("settles_at", None)
        req.pop("days_held", None)
        req["days_held"] = found["days_held"]
        filled.append("days_held")
    return {**calc.profit(req, now), "match": found["match"], "filled_in": filled}
