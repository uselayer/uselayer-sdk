"""Each matched market's title, question, outcome and times, read from its venue on your machine.

Layer's API answers a match with ids, urls and its own confidence and rule flags. The venues' own
text stays with the venues, whose terms don't let Layer pass it on. So the SDK reads it here, from
each venue's public market data, one call per event, and fills it into the match. Nothing is sent
to Layer.

    Kalshi:          its public event, with nested markets (venues/kalshi.py, ``event_titles``)
    Polymarket US:   GET gateway.polymarket.us/v1/events?slug={event}

Polymarket (international) markets keep their ids only: the SDK doesn't read that venue yet.
A venue that doesn't answer leaves its fields empty; the match is still returned.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .errors import VenueError
from .http import Http
from .venues.kalshi import event_titles as _kalshi_event

log = logging.getLogger("uselayer")

POLYMARKET_US = "https://gateway.polymarket.us"

# The fields this module fills in, on each market.
FIELDS = ("event", "question", "outcome", "event_time", "close_time")

Fields = dict[str, str | None]


def _us_side(m: dict[str, Any], long: bool) -> str | None:
    """The team or answer one side of a Polymarket US market is on (its marketSides)."""
    for s in m.get("marketSides") or []:
        if isinstance(s, dict) and bool(s.get("long")) == long:
            team = s.get("team")
            name = team.get("name") if isinstance(team, dict) else team
            return name or s.get("description")
    return None


def _polymarket_us_event(http: Http, event: str) -> dict[str, Fields]:
    body = http.request("GET", f"{POLYMARKET_US}/v1/events", venue="polymarket_us", params={"slug": event})
    events = (body or {}).get("events") if isinstance(body, dict) else None
    e = next((x for x in events or [] if isinstance(x, dict) and x.get("slug") == event), None)
    if e is None:
        return {}
    out: dict[str, Fields] = {}
    for m in e.get("markets") or []:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        slug = str(m["slug"])
        base: Fields = {
            "event": e.get("title"),
            "question": m.get("question"),
            "outcome": m.get("title") or m.get("question"),
            "event_time": m.get("gameStartTime") or e.get("startDate"),
            "close_time": m.get("endDate"),
        }
        out[slug] = base
        # A market with two named sides is matched per side: <slug>:long and <slug>:short.
        for side, long in (("long", True), ("short", False)):
            name = _us_side(m, long)
            out[f"{slug}:{side}"] = (
                {**base, "outcome": name, "question": f"{base['question']} — {name}"} if name else base
            )
    return out


READERS = {"kalshi": _kalshi_event, "polymarket_us": _polymarket_us_event}


class Titles:
    """Reads and remembers venue text per event, for one client."""

    def __init__(self, http: Http) -> None:
        self._http = http
        self._events: dict[tuple[str, str], dict[str, Fields]] = {}

    def _event(self, venue: str, group: str) -> dict[str, Fields]:
        # Layer splits one event into a group per market type, <event slug>#<kind>; the venue knows
        # the event by its slug, and the kind goes on its title, as Layer wrote it.
        key = (venue, group)
        if key not in self._events:
            event, _, kind = group.partition("#")
            try:
                found = READERS[venue](self._http, event)
                if kind and kind != "moneyline":
                    suffix = " — " + kind.replace("_", " ")
                    found = {k: {**v, "event": (v["event"] or event) + suffix} for k, v in found.items()}
                self._events[key] = found
            except VenueError as e:
                # Titles are a convenience: a venue that's down doesn't stop the match being returned.
                log.warning("uselayer: couldn't read %s event %s for titles: %s", venue, group, e)
                return {}
        return self._events[key]

    def fill(self, market: dict[str, Any]) -> dict[str, Any]:
        """``market`` (a Layer market dict) with its venue's text and times added where it has none."""
        venue, event, mid = market.get("venue"), market.get("group_id"), market.get("market_id")
        if venue not in READERS or not event or not mid or all(market.get(f) for f in FIELDS[:3]):
            return market
        found = self._event(str(venue), str(event)).get(str(mid))
        if not found:
            return market
        return {**market, **{k: v for k, v in found.items() if market.get(k) is None}}

    def fill_all(self, markets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.fill(m) for m in markets]
