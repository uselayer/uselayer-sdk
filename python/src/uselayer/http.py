"""HTTP with pacing and retries, shared by every venue adapter.

- **Pacing:** each host gets a token bucket at 80% of the venue's published limit, so the SDK stays
  under it on its own.
- **Reads and cancels** retry a 429, a 5xx or a timeout up to 3 times, waiting 0.5, 1 and 2 seconds
  plus a little jitter (or the venue's ``Retry-After``, or its own minimum wait).
- **Orders are never resent blindly.** A 429 means the venue didn't take the order, so it's retried.
  A 5xx or a timeout means the outcome is unknown: the request raises ``outcome_unknown`` and the
  adapter looks the order up before anything is sent again.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from ._version import __version__
from .errors import VenueError

Kind = Literal["read", "cancel", "order"]

BACKOFF_S = (0.5, 1.0, 2.0)
MAX_RETRIES = 3
TIMEOUT_S = 10.0
USER_AGENT = f"uselayer-python/{__version__}"


@dataclass(frozen=True)
class HostLimits:
    """A venue host's published limit and how to behave under it."""

    per_second: float
    share: float = 0.8
    min_retry_wait_s: float = 0.0


# Published limits (see each venue's rate-limit page).
HOSTS: dict[str, HostLimits] = {
    # Polymarket US: 20 requests a second per API key, and 20 a second per IP for public reads.
    # "Stop immediately, wait at least 1 second, then retry with exponential backoff."
    "gateway.polymarket.us": HostLimits(20, min_retry_wait_s=1.0),
    "api.polymarket.us": HostLimits(20, min_retry_wait_s=1.0),
    # Kalshi Basic tier: 200 read and 100 write tokens a second, 10 tokens a request, so 20 reads or
    # 10 orders a second. One bucket per host, so paced at the order rate.
    "api.elections.kalshi.com": HostLimits(10),
    "demo-api.kalshi.co": HostLimits(10),
    # Layer: 60 requests a minute per key.
    "uselayer.sh": HostLimits(1.0),
}


class _Bucket:
    def __init__(self, rate: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.rate = rate
        self.capacity = max(1.0, rate)
        self.tokens = self.capacity
        self.at = clock()
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()

    def take(self) -> None:
        with self.lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.at) * self.rate)
                self.at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                self.sleep((1 - self.tokens) / self.rate)


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


def _message(raw: Any) -> str:
    # Venues answer {"error": "..."}, {"error": {"code", "message"}}, {"status": 429, "message": ...}
    # or {"code": 5, "message": ...}.
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or raw)
        if isinstance(err, str):
            return err
        if raw.get("message"):
            return str(raw["message"])
    return str(raw)[:300]


def error_for(venue: str, resp: httpx.Response, *, kind: Kind) -> VenueError:
    """The SDK error for a venue's non-2xx answer."""
    raw = _body(resp)
    msg = _message(raw)
    s = resp.status_code
    if s == 429:
        wait = _retry_after(resp)
        return VenueError(
            "rate_limited",
            f"{venue} said too many requests.",
            venue=venue,
            status=s,
            raw=raw,
            retry_after_s=wait,
            hint="The SDK already paces requests under the venue's limit; another program may share this key or IP.",
            next=f"Wait {wait or 1:g}s and try again.",
        )
    low = msg.lower()
    if "insufficient" in low and "balance" in low:
        return VenueError(
            "insufficient_balance",
            f"{venue} says there isn't enough money for this order: {msg}",
            venue=venue,
            status=s,
            raw=raw,
            retryable=False,
            hint="Some venues hold money in separate pots (for example per sub-account); the order's market must draw on one that's funded.",
            next="client.balances()",
        )
    if s == 403 and not any(
        w in msg.lower() for w in ("signature", "credential", "api key", "unauthorized", "not_found", "key")
    ):
        return VenueError(
            "not_allowed",
            f"{venue} won't let this account do that: {msg}",
            venue=venue,
            status=s,
            raw=raw,
            retryable=False,
            hint="The venue restricts this account here (for example by region or market category). The key is fine.",
            next="Pick a market this account may trade.",
        )
    if s in (401, 403):
        return VenueError(
            "auth_failed",
            f"{venue} refused the credentials: {msg}",
            venue=venue,
            status=s,
            raw=raw,
            retryable=False,
            hint="Check the key id and the key file, and that the machine's clock is right (signatures carry a timestamp).",
            next="Fix the key and create the Client again.",
        )
    if s == 404:
        return VenueError(
            "not_found",
            f"{venue} doesn't know that: {msg}",
            venue=venue,
            status=s,
            raw=raw,
            retryable=False,
            hint="Check the market id.",
            next="Look the market up again.",
        )
    if s == 503 and venue == "polymarket_us":
        return VenueError(
            "venue_maintenance",
            "Polymarket US is unavailable (503), usually a maintenance window; open orders are canceled during maintenance.",
            venue=venue,
            status=s,
            raw=raw,
            hint="Windows are announced at status.polymarketexchange.com.",
            next="Try again after the window.",
        )
    if s >= 500:
        if kind == "order":
            return outcome_unknown(venue, f"{venue} answered {s} to an order.", raw=raw, status=s)
        return VenueError(
            "venue_unavailable",
            f"{venue} answered {s}.",
            venue=venue,
            status=s,
            raw=raw,
            next="Try again shortly.",
        )
    return VenueError(
        "invalid_order",
        f"{venue} rejected the request: {msg}",
        venue=venue,
        status=s,
        raw=raw,
        retryable=False,
        hint="The venue's own message is in .raw.",
        next="Fix the request and send it again.",
    )


def outcome_unknown(venue: str, message: str, *, raw: Any = None, status: int | None = None) -> VenueError:
    return VenueError(
        "outcome_unknown",
        message,
        venue=venue,
        status=status,
        raw=raw,
        retryable=False,
        hint="The venue may or may not have taken the order. Sending it again could double the position.",
        next="client.sync(), then check client.orders()",
    )


class Http:
    """One HTTP client for all venues, with per-host pacing and the retry rules above.

    ``transport`` swaps the network for something else (tests, recorded answers).
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        limits: Mapping[str, HostLimits] | None = None,
    ) -> None:
        self._client = httpx.Client(
            transport=transport, timeout=timeout_s, headers={"user-agent": USER_AGENT}
        )
        self._clock, self._sleep, self._rng = clock, sleep, rng
        self._limits = dict(HOSTS if limits is None else limits)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def _bucket(self, host: str) -> _Bucket | None:
        lim = self._limits.get(host)
        if lim is None:
            return None
        with self._lock:
            if host not in self._buckets:
                self._buckets[host] = _Bucket(lim.per_second * lim.share, self._clock, self._sleep)
            return self._buckets[host]

    def request(
        self,
        method: str,
        url: str,
        *,
        venue: str,
        kind: Kind = "read",
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | Callable[[], Mapping[str, str]] | None = None,
    ) -> Any:
        """Send one request and return the parsed JSON body, or raise a :class:`VenueError`.

        ``headers`` may be a function, so a signed request gets a fresh timestamp on each retry.
        """
        return self.request_with_headers(
            method, url, venue=venue, kind=kind, params=params, json=json, headers=headers
        )[0]

    def request_with_headers(
        self,
        method: str,
        url: str,
        *,
        venue: str,
        kind: Kind = "read",
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | Callable[[], Mapping[str, str]] | None = None,
    ) -> tuple[Any, httpx.Headers]:
        """Like :meth:`request`, and also return the response headers."""
        host = urlsplit(url).hostname or ""
        lim = self._limits.get(host, HostLimits(1e9))
        attempt = 0
        while True:
            bucket = self._bucket(host)
            if bucket:
                bucket.take()
            h = headers() if callable(headers) else headers
            try:
                resp = self._client.request(method, url, params=params, json=json, headers=h)
            except httpx.TimeoutException as e:
                if kind == "order":
                    raise outcome_unknown(venue, f"{venue} didn't answer the order in time.") from e
                err = VenueError(
                    "venue_unavailable",
                    f"{venue} didn't answer in time.",
                    venue=venue,
                    next="Try again shortly.",
                )
            except httpx.TransportError as e:
                if kind == "order":
                    raise outcome_unknown(
                        venue, f"The connection to {venue} failed while sending an order."
                    ) from e
                err = VenueError(
                    "venue_unavailable",
                    f"Couldn't reach {venue}: {e}",
                    venue=venue,
                    next="Check the network and try again.",
                )
            else:
                if resp.status_code < 400:
                    return (_body(resp) if resp.content else None), resp.headers
                err = error_for(venue, resp, kind=kind)
                retry = resp.status_code == 429 or (resp.status_code >= 500 and kind != "order")
                if not retry:
                    raise err
            if attempt >= MAX_RETRIES:
                raise err
            wait = (
                err.retry_after_s
                if err.retry_after_s is not None
                else BACKOFF_S[attempt] + self._rng() * 0.25
            )
            self._sleep(max(wait, lim.min_retry_wait_s))
            attempt += 1
