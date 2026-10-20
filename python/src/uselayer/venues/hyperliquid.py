"""Hyperliquid: outcome markets (HIP-4), public data, no key needed. Paper mode only.

Reads go to ``api.hyperliquid.xyz/info`` (POST, JSON), which allows 1,200 request weight a minute per IP.
A market id is the outcome's number as a string (``"14726"``). Hyperliquid quotes each side on its own
coin (``#<outcome×10 + side>``), but the two are one book seen from either side (the NO bids are
1 − the YES asks, same sizes), so the SDK reads the YES coin and turns it around for NO.

Hyperliquid lists only outcomes still trading: one that settled is gone from ``outcomeMeta``.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from ..books import Book, Level
from ..errors import VenueError
from ..fill import FeeSettings
from ..http import Http
from .base import BookRead, MarketInfo, Payout

INFO = "https://api.hyperliquid.xyz/info"
VENUE = "hyperliquid"
# outcomeMeta lists every outcome in one answer (weight 20): one copy serves every market() call for this long.
META_TTL_S = 30.0
# Prices have at most 5 decimals and sizes are whole contracts (seen on every book and fill, 2026-10-20).
TICK = 0.00001
MIN_SIZE = 1.0


def _changed(what: str, detail: str, raw: Any) -> VenueError:
    return VenueError(
        "format_changed",
        f"Hyperliquid's {what} answer changed format: {detail}",
        venue=VENUE,
        raw=raw,
        retryable=False,
        hint="The venue changed its API. Update uselayer, or report it at github.com/Dave-56/uselayer-sdk/issues.",
        next="pip install -U uselayer",
    )


def fields(description: str) -> dict[str, str]:
    """``"class:priceBinary|underlying:BTC|…"`` → ``{"class": "priceBinary", "underlying": "BTC", …}``."""
    out: dict[str, str] = {}
    for part in description.split("|"):
        k, sep, v = part.partition(":")
        if sep:
            out[k] = v
    return out


def _when(s: str | None) -> datetime | None:
    """Hyperliquid's ``20261021-0600`` (UTC) as a time; ``None`` when missing or unreadable."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d-%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def _money(x: str) -> str:
    """The target exactly as Hyperliquid wrote it, with thousands separators: never rounded (``2395.7`` → ``$2,395.7``)."""
    whole, _, frac = x.strip().partition(".")
    frac = frac.rstrip("0")
    return f"${int(whole):,}" + (f".{frac}" if frac else "")


def question(o: dict[str, Any]) -> str | None:
    """Plain words for the outcomes this release knows how to read; ``None`` for the rest.

    A recurring price market pays YES when the mark price at expiry is at or above its target (the
    contract specifications); a template one when its average over the window ends above the threshold.
    """
    f = fields(str(o.get("description") or ""))
    name = str(o.get("name") or "")
    if name == "Recurring" and f.get("class") == "priceBinary" and "underlying" in f and "targetPrice" in f:
        return f"{f['underlying']} at or above {_money(f['targetPrice'])}?"
    if name == "template:binaryPrice" and "perp" in f and "threshold" in f:
        return f"{f['perp'].split(':')[-1]} above {_money(f['threshold'])}?"
    return None


def _scale(o: dict[str, Any]) -> float:
    """The fee multiplier: ``scale + max(scale, 1)`` for a deployer's market, 1 for a recurring one."""
    raw = o.get("deployerFeeScale")
    if raw is None:
        return 1.0
    try:
        scale = float(raw)
    except (TypeError, ValueError):
        raise _changed("outcomeMeta", f"deployerFeeScale isn't a number: {raw!r}", o) from None
    return scale + max(scale, 1.0)


def market_info(o: dict[str, Any]) -> MarketInfo:
    """A MarketInfo from one outcome in ``outcomeMeta``. Every outcome it lists is still trading."""
    f = fields(str(o.get("description") or ""))
    expiry = _when(f.get("expiry") or f.get("time"))
    return MarketInfo(
        venue=VENUE,
        market=str(o["outcome"]),
        question=question(o),
        status="open",
        open=True,
        tick_size=TICK,
        min_size=MIN_SIZE,
        fees=FeeSettings(venue=VENUE, multiplier=_scale(o)),
        end_date=expiry.isoformat() if expiry else None,
        event_time=expiry.isoformat() if expiry else None,
    )


class HyperliquidOutcomes:
    """Hyperliquid's outcome markets from its public info API.

    hl = HyperliquidOutcomes(Http())
    hl.read_book("14726").book.outcome("yes").best_ask
    """

    venue = VENUE

    def __init__(self, http: Http, url: str = INFO) -> None:
        self._http = http
        self._url = url
        self._meta: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def _post(self, body: dict[str, Any]) -> Any:
        return self._http.request("POST", self._url, venue=VENUE, json=body)

    def meta(self) -> dict[str, Any]:
        """``outcomeMeta``, read at most once every :data:`META_TTL_S` seconds."""
        with self._lock:
            now = time.monotonic()
            if self._meta is not None and now - self._meta[0] < META_TTL_S:
                return self._meta[1]
            body = self._post({"type": "outcomeMeta"})
            if not isinstance(body, dict) or not isinstance(body.get("outcomes"), list):
                raise _changed("outcomeMeta", "no outcomes list", body)
            self._meta = (now, body)
            return body

    def _outcome(self, market: str) -> dict[str, Any] | None:
        meta = self.meta()
        return next(
            (o for o in meta["outcomes"] if isinstance(o, dict) and str(o.get("outcome")) == market), None
        )

    def market(self, market: str) -> MarketInfo:
        """The outcome's question, fee settings and when it settles."""
        o = self._outcome(market)
        if o is None:
            raise VenueError(
                "not_found",
                f"Hyperliquid lists no open outcome {market!r}.",
                venue=VENUE,
                retryable=False,
                hint="Market ids are outcome numbers, as in client.markets(venue='hyperliquid'). A settled outcome is no longer listed.",
                next="client.markets(venue='hyperliquid')",
            )
        return market_info(o)

    def markets(self, *, limit: int = 50, offset: int = 0) -> list[MarketInfo]:
        """Outcomes Hyperliquid lists now, in its order."""
        rows = [
            o for o in self.meta()["outcomes"] if isinstance(o, dict) and isinstance(o.get("outcome"), int)
        ]
        return [market_info(o) for o in rows[offset : offset + limit]]

    def payout(self, market: str) -> Payout | None:
        """What one YES contract paid once Hyperliquid settled the outcome; ``None`` before.

        From ``settledOutcome`` (``settleFraction``: YES gets it, NO gets 1 − it; ``null`` until then).
        Dated at the outcome's expiry when its description gives one. The taker fee Hyperliquid takes
        off a winning payout isn't in it.
        """
        if not market.isdigit():
            return None
        body = self._post({"type": "settledOutcome", "outcome": int(market)})
        if body is None:
            return None
        frac = body.get("settleFraction") if isinstance(body, dict) else None
        try:
            yes = float(frac)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise _changed("settledOutcome", "settleFraction isn't a number", body) from None
        if not 0 <= yes <= 1:
            raise _changed("settledOutcome", "settleFraction isn't from 0 to 1", body)
        spec = body.get("spec") if isinstance(body.get("spec"), dict) else {}
        f = fields(str(spec.get("description") or ""))
        return Payout(round(yes, 6), _when(f.get("expiry") or f.get("time")))

    def read_book(self, market: str) -> BookRead:
        """The YES book, stamped with Hyperliquid's own time for it."""
        if not market.isdigit():
            raise VenueError(
                "not_found", f"{market!r} isn't a Hyperliquid outcome number.", venue=VENUE, retryable=False
            )
        body = self._post({"type": "l2Book", "coin": f"#{int(market) * 10}"})
        if body is None:
            raise VenueError(
                "not_found", f"Hyperliquid has no book for outcome {market}.", venue=VENUE, retryable=False
            )
        levels = body.get("levels") if isinstance(body, dict) else None
        stamp = body.get("time") if isinstance(body, dict) else None
        if not isinstance(levels, list) or len(levels) != 2 or not isinstance(stamp, int):
            raise _changed("l2Book", "no levels pair or time", body)
        bids, asks = _levels(levels[0]), _levels(levels[1])
        as_of = datetime.fromtimestamp(stamp / 1000, tz=UTC)
        return BookRead(Book(venue=VENUE, market=market, bids=bids, asks=asks, as_of=as_of), None)

    def book(self, market: str) -> Book:
        return self.read_book(market).book


def _levels(raw: Any) -> tuple[Level, ...]:
    out = []
    for lv in raw or []:
        try:
            price, size = float(lv["px"]), float(lv["sz"])
        except (KeyError, TypeError, ValueError) as e:
            raise _changed("l2Book", f"a level isn't {{px, sz}}: {lv!r}", raw) from e
        if 0 < price < 1 and size > 0:
            out.append(Level(price=price, size=size))
    return tuple(out)
