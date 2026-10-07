"""A fake Polymarket US venue and a fixed clock, so tests never touch the network."""

from __future__ import annotations

import email.utils
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from uselayer import Client

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += timedelta(seconds=s)

    def advance(self, s: float) -> None:
        self.now += timedelta(seconds=s)


@dataclass
class FakeMarket:
    slug: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    tick: float = 0.001
    min_qty: float = 1
    fee_coefficient: float | None = 0.0695
    status: str = "MARKET_STATUS_OPEN"
    state: str = "MARKET_STATE_OPEN"


@dataclass
class FakeVenue:
    """Answers Polymarket US gateway reads (and records every request the SDK makes anywhere)."""

    clock: Clock
    markets: dict[str, FakeMarket] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    layer_answers: dict[str, Any] = field(default_factory=dict)
    cache_age_s: float = 0.0
    settlements: dict[str, Any] = field(default_factory=dict)  # slug -> /settlement answer; 404 until set

    def add(self, m: FakeMarket) -> FakeMarket:
        self.markets[m.slug] = m
        return m

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "date": email.utils.format_datetime(self.clock.now, usegmt=True),
            "age": str(int(self.cache_age_s)),
            "cache-control": "public, max-age=30",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "uselayer.sh":
            body = self.layer_answers.get(path, {"matches": []})
            return httpx.Response(200, json=body, headers={"content-type": "application/json"})
        if host != "gateway.polymarket.us":
            return httpx.Response(404, json={"message": "unknown host"})
        if path == "/v1/markets":
            slug = request.url.params.get("slug")
            ms = [self.markets[slug]] if slug in self.markets else []
            body = {
                "markets": [
                    {
                        "slug": m.slug,
                        "question": f"Q {m.slug}",
                        "active": True,
                        "closed": m.status != "MARKET_STATUS_OPEN",
                        "status": m.status,
                        "orderPriceMinTickSize": m.tick,
                        "minimumTradeQty": m.min_qty,
                        "feeCoefficient": m.fee_coefficient,
                        "endDate": "2026-12-31T00:00:00Z",
                    }
                    for m in ms
                ]
            }
            return httpx.Response(200, json=body, headers=self._headers())
        if path.startswith("/v1/markets/") and path.endswith("/settlement"):
            slug = path.split("/")[3]
            if slug not in self.settlements:
                return httpx.Response(404, json={"code": 5, "message": "market not found or not settled"})
            return httpx.Response(200, json={"slug": slug, "settlement": self.settlements[slug]})
        if path.startswith("/v1/markets/") and path.endswith("/book"):
            slug = path.split("/")[3]
            m = self.markets.get(slug)
            if m is None:
                return httpx.Response(404, json={"code": 5, "message": "not found"})
            lv = lambda p, q: {"px": {"value": f"{p:.4f}", "currency": "USD"}, "qty": f"{q:.4f}"}  # noqa: E731
            md = {
                "marketSlug": slug,
                "bids": [lv(p, q) for p, q in m.bids],
                "offers": [lv(p, q) for p, q in m.asks],
                "state": m.state,
                "transactTime": (self.clock.now - timedelta(seconds=120)).isoformat().replace("+00:00", "Z"),
            }
            return httpx.Response(200, json={"marketData": md}, headers=self._headers())
        return httpx.Response(404, json={"message": "unknown path"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self) -> list[str]:
        return [r.content.decode() for r in self.requests]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def venue(clock: Clock) -> FakeVenue:
    v = FakeVenue(clock)
    v.add(FakeMarket("mkt-a", bids=[(0.40, 50), (0.39, 100)], asks=[(0.42, 10), (0.43, 20), (0.45, 100)]))
    v.add(FakeMarket("mkt-b", bids=[(0.60, 30)], asks=[(0.62, 40)]))
    return v


@pytest.fixture
def make_client(venue: FakeVenue, clock: Clock, tmp_path: Any) -> Iterator[Any]:
    made: list[Client] = []

    def make(**kw: Any) -> Client:
        kw.setdefault("store", str(tmp_path / f"store-{len(made)}.db"))
        c = Client(transport=venue.transport(), clock=clock, sleep=clock.sleep, **kw)
        made.append(c)
        return c

    yield make
    for c in made:
        c.close()


def dumps(x: Any) -> str:
    return json.dumps(x, default=str)
