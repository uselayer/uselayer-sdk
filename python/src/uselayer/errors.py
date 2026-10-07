"""One error type for every venue and every mode.

Every error says what went wrong (``code``, ``message``), whether trying again can help
(``retryable``), what to change (``hint``) and the one call that moves things forward (``next``).

    try:
        client.buy(venue="polymarket_us", market="some-slug", side="yes", price=0.42, size=10)
    except VenueError as e:
        print(e.code, e.hint, e.next)
"""

from __future__ import annotations

import json
from typing import Any, Literal

ErrorCode = Literal[
    "rate_limited",
    "insufficient_balance",
    "market_closed",
    "invalid_order",
    "not_found",
    "auth_failed",
    "not_allowed",
    "venue_unavailable",
    "venue_maintenance",
    "outcome_unknown",
    "blocked_by_rule",
    "venue_switched_off",
    "not_available",
    "stale_quote",
    "no_venue_rules",
    "format_changed",
    "bad_data",
    "killed",
]

RETRYABLE: frozenset[str] = frozenset(
    {"rate_limited", "venue_unavailable", "venue_maintenance", "stale_quote"}
)


class VenueError(Exception):
    """The SDK's only error type.

    Fields:
        venue: ``"polymarket_us"``, ``"kalshi"``, ``"polymarket"``, ``"layer"`` or ``None``.
        code: one of :data:`ErrorCode`.
        message: what happened, in one sentence.
        retryable: whether sending the same request again later can succeed.
        hint: what to change, in plain English.
        next: the one call that moves things forward, e.g. ``"client.sync()"``.
        status: the HTTP status, when a venue answered.
        retry_after_s: seconds to wait first, when the venue said so.
        raw: the venue's own answer, unchanged.
        rule: for ``blocked_by_rule``, the rule's name.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        venue: str | None = None,
        hint: str | None = None,
        next: str | None = None,
        retryable: bool | None = None,
        status: int | None = None,
        retry_after_s: float | None = None,
        raw: Any = None,
        rule: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.venue = venue
        self.hint = hint
        self.next = next
        self.retryable = code in RETRYABLE if retryable is None else retryable
        self.status = status
        self.retry_after_s = retry_after_s
        self.raw = raw
        self.rule = rule

    def to_dict(self) -> dict[str, Any]:
        """The error as plain data, with the same field names as above.

        VenueError("not_found", "no such market").to_dict()["code"]  # "not_found"
        """
        raw = self.raw
        if raw is not None and not isinstance(raw, (str, int, float, bool, list, dict)):
            raw = str(raw)
        return {
            "code": self.code,
            "message": self.message,
            "venue": self.venue,
            "retryable": self.retryable,
            "hint": self.hint,
            "next": self.next,
            "status": self.status,
            "retry_after_s": self.retry_after_s,
            "rule": self.rule,
            "raw": raw,
        }

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        if self.next:
            parts.append(f"Next: {self.next}")
        return " ".join(parts)

    def __repr__(self) -> str:
        return f"VenueError({json.dumps(self.to_dict(), default=str)})"


def switched_off(venue: str, trading: list[str]) -> VenueError:
    on = ", ".join(trading) or "none"
    return VenueError(
        "venue_switched_off",
        f"This release doesn't trade on {venue}.",
        venue=venue,
        hint=f"This release trades on: {on}.",
        next="Use one of the venues this release trades on.",
        retryable=False,
    )


VENUE_NAMES = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US", "polymarket": "Polymarket"}


def live_switched_off(venue: str) -> VenueError:
    """A live order on a venue this release reads and paper-trades, but doesn't trade live yet."""
    name = VENUE_NAMES.get(venue, venue)
    return VenueError(
        "venue_switched_off",
        f"Live {name} orders are switched off in this release.",
        venue=venue,
        hint=f"{name} works in paper and backtest mode, against its real books with your own key. "
        "A later release turns live orders on.",
        next=f"Client(mode='paper', {venue}=...)",
        retryable=False,
    )
