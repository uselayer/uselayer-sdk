"""Layer's API: which markets are the same bet on different venues.

The SDK sends Layer three things only: your Layer API key, market ids Layer gave you, and filters such
as ``q="nfl"``. It never sends prices, orders, positions or venue keys. Every request is checked
against that list before it leaves your machine, and ``tests/test_layer_traffic.py`` records the
traffic to prove it.

    client = Client(layer_key="lyr_...")
    m = client.matches(q="chiefs", venue="polymarket_us")[0]

Layer answers with each market's ids, url and series/slug, plus its own match confidence and
rule-difference flags. The venues' own text (event, question, outcome) and times aren't Layer's to
pass on, so the SDK reads them from each venue on your machine and fills them in (see
:mod:`uselayer.titles`). ``titles=False`` skips those venue calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from .errors import VenueError
from .http import Http
from .titles import Titles

BASE_URL = "https://uselayer.sh"

# The only requests the SDK makes to Layer: GET, these paths, these query parameters.
ALLOWED: Mapping[str, frozenset[str]] = {
    "/v0/matches": frozenset({"limit", "offset", "category", "from", "to", "q", "venue"}),
    "/v0/match": frozenset({"venue", "market_id", "with"}),
}


class Market(BaseModel):
    """One venue's market in a match.

    From Layer: ``venue``, ``market_id``, ``group_id`` (the event), ``url``, ``series``, ``slug``
    (Polymarket US) and ``yes_token_id`` (Polymarket). From the venue, read on your machine:
    ``event``, ``question``, ``outcome``, ``event_time`` and ``close_time`` (``None`` if the venue
    didn't answer, or for Polymarket international).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    venue: str
    market_id: str
    group_id: str | None = None
    url: str | None = None
    series: str | None = None
    slug: str | None = None
    yes_token_id: str | None = None
    event: str | None = None
    question: str | None = None
    outcome: str | None = None
    event_time: str | None = None
    close_time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Match(BaseModel):
    """Two markets on different venues that Layer says are the same bet.

    Each venue's market is an attribute named after the venue: ``m.kalshi``, ``m.polymarket_us`` or
    ``m.polymarket``. Pass one to ``client.book()`` or as ``market=`` in an order.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    event_date: str | None = None
    category: str | None = None
    confidence: float | None = None
    basis: str | None = None
    caveats: list[str] = []
    #: Why each caveat applies, one plain sentence per code, as Layer sent it:
    #: ``{"source_differs": "Kalshi settles on ...; Polymarket US uses ..."}``. Empty when Layer has none.
    caveat_notes: dict[str, str] = {}
    tier: str | None = None

    def markets(self) -> dict[str, Market]:
        """Each venue's market in this match, by venue name."""
        out: dict[str, Market] = {}
        for k, v in (self.model_extra or {}).items():
            if isinstance(v, dict) and "market_id" in v:
                out[k] = Market.model_validate({"venue": k, **v})
        return out

    def __getattr__(self, name: str) -> Any:
        if name in ("kalshi", "polymarket", "polymarket_us"):
            m = self.markets().get(name)
            if m is None:
                raise AttributeError(f"this match has no {name} market")
            return m
        return super().__getattr__(name)  # type: ignore[misc]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class LayerApi:
    """The SDK's Layer client. It has exactly two calls, both reads."""

    def __init__(self, key: str | None, http: Http, base_url: str = BASE_URL) -> None:
        self._key = key
        self._http = http
        self._base = base_url.rstrip("/")
        self._titles = Titles(http)

    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        if self._key is None:
            raise VenueError(
                "auth_failed",
                "Matches come from Layer's API, which needs a Layer API key.",
                venue="layer",
                retryable=False,
                hint="Create one at uselayer.sh, then pass Client(layer_key=...) or set LAYER_API_KEY.",
                next="Client(layer_key='lyr_...')",
            )
        allowed = ALLOWED.get(path)
        if allowed is None:
            raise AssertionError(f"the SDK never calls {path} on Layer")
        clean: dict[str, str] = {}
        for k, v in params.items():
            if v is None:
                continue
            if k not in allowed:
                raise AssertionError(f"the SDK never sends {k!r} to Layer")
            if not isinstance(v, (str, int)) or isinstance(v, bool):
                raise AssertionError(f"Layer filters are strings or whole numbers, not {type(v).__name__}")
            clean[k] = str(v)
        return self._http.request(
            "GET",
            self._base + path,
            venue="layer",
            params=clean,
            headers={"authorization": f"Bearer {self._key}"},
        )

    def _filled(self, d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        """``d`` with the venue text filled into each market under ``keys``."""
        out = dict(d)
        for k in keys:
            m = out.get(k)
            if isinstance(m, dict) and "market_id" in m:
                out[k] = self._titles.fill(m)
        return out

    def matches(self, *, titles: bool = True, **filters: Any) -> list[Match]:
        """``GET /v0/matches`` with filters: limit, offset, category, from, to, q, venue."""
        params = {("from" if k == "from_" else k): v for k, v in filters.items()}
        body = self._get("/v0/matches", params)
        rows = body.get("matches", [])
        if titles:
            rows = [self._filled(m, ("kalshi", "polymarket", "polymarket_us")) for m in rows]
        return [Match.model_validate(m) for m in rows]

    def match(
        self, market_id: str, *, venue: str, with_: str | None = None, titles: bool = True
    ) -> dict[str, Any]:
        """``GET /v0/match``: the twin of one market on the other venue."""
        body: dict[str, Any] = self._get("/v0/match", {"venue": venue, "market_id": market_id, "with": with_})
        return self._filled(body, ("source_market", "matched_market")) if titles else body
