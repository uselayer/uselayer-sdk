"""Kalshi with your own API key: markets, books and fees for paper, backtest and pairs, and live orders.

Built against Kalshi's Trade API v2 (openapi 3.32.0, checked 2026-10-01) and tested on Kalshi's demo
exchange. Every request is signed with your key, on your machine; nothing about it goes to Layer.

Live Kalshi orders go to Kalshi for your own account (``TRADING["kalshi"]`` in :mod:`uselayer._switches`
is on in this release; :meth:`KalshiLive.place`, :meth:`~KalshiLive.cancel` and
:meth:`~KalshiLive.cancel_all` read it on every call). Paper and backtest mode fill Kalshi orders against
Kalshi's real books.

How orders map (Kalshi quotes everything from the YES side, V2 ``POST /portfolio/events/orders``):

- buy YES at p   → side ``bid``, price p
- sell YES at p  → side ``ask``, price p
- buy NO at p    → side ``ask``, price 1 − p
- sell NO at p   → side ``bid``, price 1 − p

    from uselayer import Client, Kalshi
    client = Client(kalshi=Kalshi(key_id="...", private_key_path="~/.kalshi/key.pem"))
    client.book("KXNFLGAME-26OCT04KCBUF-KC", venue="kalshi").outcome("yes").best_ask
"""

from __future__ import annotations

import base64
import contextlib
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .. import _switches
from ..books import Book, Level
from ..errors import VenueError, live_switched_off
from ..events import Fill
from ..fees import dollars
from ..fill import FeeSettings, calculate_fee
from ..http import Http
from ..orders import Order
from .base import Balance, BookRead, MarketInfo, Payout, VenuePosition

VENUE = "kalshi"
PREFIX = "/trade-api/v2"
HOSTS = {
    "production": "https://api.elections.kalshi.com",
    "demo": "https://demo-api.kalshi.co",
}
WS_HOSTS = {
    "production": "wss://api.elections.kalshi.com",
    "demo": "wss://demo-api.kalshi.co",
}
WS_PATH = "/trade-api/ws/v2"
"""Kalshi's market-data WebSocket (signed like a REST call: ``GET /trade-api/ws/v2``)."""

_TIF = {"ioc": "immediate_or_cancel", "fok": "fill_or_kill", "gtc": "good_till_canceled"}


@dataclass(frozen=True)
class Kalshi:
    """Your Kalshi API key: the key id and the private key PEM (Ed25519 or RSA), from a file or a string.

        Kalshi(key_id="...", private_key_path="~/.kalshi/key.pem")
        Kalshi.from_env()   # KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY)

    ``environment="demo"`` uses Kalshi's demo exchange; a key works only on the exchange that made it.
    Nothing is printed or saved.
    """

    key_id: str
    private_key_path: str | None = None
    private_key_pem: str | None = field(default=None, repr=False)
    environment: Literal["production", "demo"] = "production"

    @staticmethod
    def from_env() -> Kalshi:
        key_id = os.environ.get("KALSHI_KEY_ID")
        path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        pem = os.environ.get("KALSHI_PRIVATE_KEY")
        if not key_id or not (path or pem):
            raise VenueError(
                "auth_failed",
                "KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY) aren't both set.",
                venue=VENUE,
                retryable=False,
                hint="Create an API key in your Kalshi account settings.",
                next="Kalshi(key_id=..., private_key_path=...)",
            )
        env = os.environ.get("KALSHI_ENV", "production")
        if env not in HOSTS:
            raise VenueError("auth_failed", "KALSHI_ENV is production or demo.", venue=VENUE, retryable=False)
        return Kalshi(key_id=key_id, private_key_path=path, private_key_pem=pem, environment=env)  # type: ignore[arg-type]

    def load(self) -> Ed25519PrivateKey | RSAPrivateKey:
        pem = self.private_key_pem
        if pem is None and self.private_key_path is not None:
            try:
                pem = Path(self.private_key_path).expanduser().read_text()
            except OSError as e:
                raise VenueError(
                    "auth_failed",
                    f"Can't read the Kalshi private key file {self.private_key_path!r}: {e.strerror}.",
                    venue=VENUE,
                    retryable=False,
                    next="Kalshi(key_id=..., private_key_path=<the .pem file Kalshi gave you>)",
                ) from e
        if not pem:
            raise VenueError(
                "auth_failed",
                "No Kalshi private key given.",
                venue=VENUE,
                retryable=False,
                next="Kalshi(key_id=..., private_key_path=...)",
            )
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, (Ed25519PrivateKey, RSAPrivateKey)):
            raise VenueError("auth_failed", "Kalshi keys are Ed25519 or RSA.", venue=VENUE, retryable=False)
        return key


class Signer:
    """Signs ``timestamp_ms + METHOD + path`` (path with /trade-api/v2, no query) with either key type.

    Kalshi's web app makes Ed25519 keys by default since 2026-10-01; older keys and the API's own key
    generator are RSA (signed with RSA-PSS, SHA-256, digest-length salt). The scheme follows the key.
    """

    def __init__(self, key: Kalshi, clock_ms: Callable[[], int] | None = None) -> None:
        self._key_id = key.key_id
        self._private = key.load()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @property
    def kind(self) -> str:
        return "ed25519" if isinstance(self._private, Ed25519PrivateKey) else "rsa"

    def sign(self, message: bytes) -> bytes:
        if isinstance(self._private, Ed25519PrivateKey):
            return self._private.sign(message)
        return self._private.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(self._clock_ms())
        sig = self.sign(f"{ts}{method.upper()}{path}".encode())
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }


def _d(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _when_iso(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _price_str(p: float) -> str:
    return format(Decimal(str(round(p, 6))).quantize(Decimal("0.0001")), "f")


def _count_str(n: float) -> str:
    return format(Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_DOWN), "f")


def book_side(order: Order) -> tuple[str, float]:
    """The venue's ``side`` and YES ``price`` for an order."""
    yes_buy = (order.side == "yes") == (order.action == "buy")
    yes_price = order.price if order.side == "yes" else round(1 - order.price, 6)
    return ("bid" if yes_buy else "ask"), yes_price


def order_body(order: Order) -> dict[str, Any]:
    side, yes_price = book_side(order)
    body: dict[str, Any] = {
        "ticker": order.market,
        "side": side,
        "count": _count_str(order.size),
        "price": _price_str(yes_price),
        "time_in_force": _TIF[order.tif],
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "client_order_id": order.client_id,
    }
    if order.tif == "gtc":
        assert order.expires_at is not None, "gtc orders always carry expires_at (order_ttl_s)"
        body["expiration_time"] = int(order.expires_at.timestamp())
    if order.post_only:
        body["post_only"] = True
    return body


def _side_price(order: Order, yes_price: float | None, no_price: float | None) -> float | None:
    if order.side == "yes":
        return yes_price if yes_price is not None else (None if no_price is None else round(1 - no_price, 6))
    return no_price if no_price is not None else (None if yes_price is None else round(1 - yes_price, 6))


def _status(order: Order, *, status: str | None, filled: float, remaining: float) -> str:
    if status == "resting":
        return "open"
    if status == "executed" or (remaining <= 1e-9 and filled > 0):
        return "filled"
    if status == "canceled":
        return "canceled"
    if order.tif == "gtc" and remaining > 0:
        return "open"
    return "canceled"


def _require_trading() -> None:
    # Read at call time, so the release's switch is the only thing that decides.
    if not _switches.TRADING.get(VENUE, False):
        raise live_switched_off(VENUE)


class KalshiLive:
    """Kalshi with your key: markets, books and fee settings; orders, fills, positions and balance.

        k = KalshiLive(Http(), Kalshi(key_id=..., private_key_path=...))
        k.read_book("KX...").book.outcome("yes").best_ask

    Sending and canceling orders refuse if a release switches Kalshi trading off.
    """

    venue = VENUE

    def __init__(
        self,
        http: Http,
        key: Kalshi,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        base_url: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._signer = Signer(key)
        self._base = (base_url or HOSTS[key.environment]).rstrip("/") + PREFIX
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._event_fees: dict[str, FeeSettings] = {}
        self._market_fees: dict[str, FeeSettings] = {}
        # When this adapter last saw a fill on each ticker, so positions() can wait for Kalshi to catch up.
        self._filled_at: dict[str, datetime] = {}

    @property
    def key_kind(self) -> str:
        """``"ed25519"`` or ``"rsa"``: the kind of key loaded."""
        return self._signer.kind

    def _call(
        self,
        method: str,
        path: str,
        *,
        kind: Any = "read",
        params: dict[str, Any] | None = None,
        body: Any = None,
        with_headers: bool = False,
    ) -> Any:
        full = PREFIX + path
        r = self._http.request_with_headers(
            method,
            self._base + path,
            venue=VENUE,
            kind=kind,
            params=params,
            json=body,
            headers=lambda: self._signer.headers(method, full),
        )
        return r if with_headers else r[0]

    # ---- markets and books ----

    def _fee_settings(self, market: dict[str, Any]) -> FeeSettings:
        """The series' ``fee_type`` and ``fee_multiplier``, read once per event."""
        event = str(market.get("event_ticker") or "")
        if event not in self._event_fees:
            series = market.get("series_ticker")
            if not series and event:
                series = ((self._call("GET", f"/events/{event}") or {}).get("event") or {}).get(
                    "series_ticker"
                )
            fee_type, multiplier = "quadratic", 1.0
            if series:
                s = (self._call("GET", f"/series/{series}") or {}).get("series") or {}
                fee_type = str(s.get("fee_type") or "quadratic")
                fm = s.get("fee_multiplier")
                multiplier = 1.0 if fm is None else float(fm)
            self._event_fees[event] = FeeSettings(venue=VENUE, multiplier=multiplier, fee_type=fee_type)
        settings = self._event_fees[event]
        self._market_fees[str(market.get("ticker"))] = settings
        return settings

    def _info(self, m: dict[str, Any]) -> MarketInfo:
        # Every price on the coarsest step of the market's price ranges is valid in all of them.
        steps = [_d(r.get("step")) for r in m.get("price_ranges") or []]
        tick = max((s for s in steps if s), default=0.01)
        status = str(m.get("status") or "")
        return MarketInfo(
            venue=VENUE,
            market=str(m["ticker"]),
            question=m.get("title") or m.get("yes_sub_title"),
            status=status,
            open=status == "active",
            tick_size=tick,
            # Kalshi takes fractional contracts on every market, to 0.01 (openapi: "minimum granularity").
            min_size=0.01,
            fees=self._fee_settings(m),
            end_date=m.get("close_time"),
            event_time=m.get("occurrence_datetime") or m.get("expected_expiration_time"),
        )

    def market(self, market: str) -> MarketInfo:
        """The market's status, tick size, minimum size and its series' fee settings."""
        try:
            m = (self._call("GET", f"/markets/{market}") or {}).get("market")
        except VenueError as e:
            if e.code != "not_found":
                raise
            m = None
        if not isinstance(m, dict):
            raise VenueError(
                "not_found",
                f"Kalshi has no market {market!r}.",
                venue=VENUE,
                retryable=False,
                hint="Kalshi market ids are tickers, like the last part of the market's kalshi.com URL, in capitals.",
                next="Check the ticker.",
            )
        return self._info(m)

    def markets(self, *, limit: int = 50, offset: int = 0) -> list[MarketInfo]:
        """Open markets (no multivariate combos), ``offset`` markets in, following Kalshi's cursor."""
        raw: list[dict[str, Any]] = []
        cursor = None
        while len(raw) < offset + limit:
            params: dict[str, Any] = {
                "status": "open",
                "limit": min(1000, offset + limit - len(raw)),
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            body = self._call("GET", "/markets", params=params) or {}
            raw.extend(m for m in body.get("markets") or [] if isinstance(m, dict) and m.get("ticker"))
            cursor = body.get("cursor")
            if not cursor:
                break
        return [self._info(m) for m in raw[offset : offset + limit]]

    def payout(self, market: str) -> Payout | None:
        """What one YES contract paid once Kalshi finalized the market, and when; ``None`` before.

        ``settlement_value_dollars`` ($1 or $0, or a fair price for a canceled game), else ``result``;
        the time is ``settlement_ts``. A ``determined`` market can still be disputed, so it isn't paid
        out yet.
        """
        m = (self._call("GET", f"/markets/{market}") or {}).get("market") or {}
        if m.get("status") not in ("finalized", "settled"):
            return None
        value = _d(m.get("settlement_value_dollars"))
        if value is None:
            value = {"yes": 1.0, "no": 0.0}.get(str(m.get("result") or ""))
        if value is None or not 0 <= value <= 1:
            return None
        at = None
        with contextlib.suppress(TypeError, ValueError):
            at = datetime.fromisoformat(str(m["settlement_ts"]).replace("Z", "+00:00"))
        return Payout(round(value, 6), at if at is not None and at.tzinfo is not None else None)

    def read_book(self, market: str) -> BookRead:
        """The YES book: bids from ``yes_dollars``, asks as 1 − each NO bid. ``as_of`` is the venue's Date header."""
        body, headers = self._call(
            "GET", f"/markets/{market}/orderbook", params={"depth": 0}, with_headers=True
        )
        ob = (body or {}).get("orderbook_fp")
        if not isinstance(ob, dict):
            raise VenueError(
                "format_changed",
                f"Kalshi's orderbook answer changed format: no orderbook_fp for {market}.",
                venue=VENUE,
                raw=body,
                retryable=False,
                hint="The venue changed its API. Update uselayer, or report it at github.com/Dave-56/uselayer-sdk/issues.",
                next="pip install -U uselayer",
            )
        bids = [
            Level(price=float(p), size=float(q))
            for p, q in ob.get("yes_dollars") or []
            if 0 < float(p) < 1 and float(q) > 0
        ]
        asks = [
            Level(price=round(1 - float(p), 6), size=float(q))
            for p, q in ob.get("no_dollars") or []
            if 0 < float(p) < 1 and float(q) > 0
        ]
        as_of = self._clock()
        if headers.get("date"):
            with contextlib.suppress(TypeError, ValueError):
                as_of = parsedate_to_datetime(headers["date"])
        book = Book(venue=VENUE, market=market, bids=tuple(bids), asks=tuple(asks), as_of=as_of)
        return BookRead(book, None)

    # ---- orders ----

    def _venue_fills(self, order: Order) -> list[Fill]:
        if not order.venue_order_id:
            return []
        out = []
        cursor = None
        settings = self._market_fees.get(order.market, FeeSettings(venue=VENUE))
        while True:
            params: dict[str, Any] = {"order_id": order.venue_order_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = self._call("GET", "/portfolio/fills", params=params) or {}
            for f in body.get("fills") or []:
                n = _d(f.get("count_fp")) or 0.0
                price = _side_price(order, _d(f.get("yes_price_dollars")), _d(f.get("no_price_dollars")))
                if n <= 0 or price is None:
                    continue
                role: Literal["taker", "maker"] = "taker" if f.get("is_taker") else "maker"
                at = datetime.fromtimestamp(f["ts"], UTC) if isinstance(f.get("ts"), int) else self._clock()
                billed = _d(f.get("fee_cost"))
                try:
                    estimate = dollars(calculate_fee(settings, contracts=n, price=price, role=role, at=at))
                except VenueError:
                    # A fill older than the earliest fee schedule the SDK knows: only Kalshi's own number exists.
                    estimate = billed if billed is not None else 0.0
                out.append(
                    Fill(
                        venue=VENUE,
                        market=order.market,
                        order_id=order.id or order.client_id,
                        venue_fill_id=str(f.get("fill_id") or f.get("trade_id")),
                        side=order.side,
                        action=order.action,
                        price=price,
                        contracts=n,
                        role=role,
                        cost=round(price * n, 6),
                        fee=billed if billed is not None else estimate,
                        fee_estimate=estimate,
                        at=at,
                        group_id=order.group_id,
                    )
                )
            cursor = body.get("cursor")
            if not cursor:
                return out

    def _fills_covering(self, order: Order, filled: float) -> list[Fill]:
        """The order's fills from Kalshi, waiting a moment for the fills list to catch up with ``filled``."""
        if filled <= 0:
            return []
        fills: list[Fill] = []
        for i in range(6):
            fills = self._venue_fills(order)
            if sum(f.contracts for f in fills) >= filled - 1e-9:
                break
            if i < 5:
                self._sleep(0.5)
        if fills:
            self._filled_at[order.market] = max(f.at for f in fills)
        return fills

    def _apply(self, order: Order, v: dict[str, Any]) -> Order:
        filled = _d(v.get("fill_count_fp")) or 0.0
        remaining = _d(v.get("remaining_count_fp")) or 0.0
        cost = (_d(v.get("taker_fill_cost_dollars")) or 0.0) + (_d(v.get("maker_fill_cost_dollars")) or 0.0)
        fees = (_d(v.get("taker_fees_dollars")) or 0.0) + (_d(v.get("maker_fees_dollars")) or 0.0)
        upd: dict[str, Any] = {
            "venue_order_id": str(v.get("order_id") or order.venue_order_id),
            "status": _status(order, status=v.get("status"), filled=filled, remaining=remaining),
            "filled": filled,
            "fees": round(fees, 6),
            "updated_at": self._clock(),
        }
        if filled > 0 and cost > 0:
            upd["avg_price"] = round(cost / filled, 6)
        return order.model_copy(update=upd)

    def place(self, order: Order) -> tuple[Order, list[Fill]]:
        """Send an order with ``POST /portfolio/events/orders``. Its client id is the SDK's ``client_id``."""
        _require_trading()
        r = self._call("POST", "/portfolio/events/orders", kind="order", body=order_body(order)) or {}
        vid = str(r.get("order_id") or "")
        filled = _d(r.get("fill_count")) or 0.0
        remaining = _d(r.get("remaining_count")) or 0.0
        placed = order.model_copy(
            update={
                "venue_order_id": vid or None,
                "filled": filled,
                "status": _status(order, status=None, filled=filled, remaining=remaining),
                "updated_at": self._clock(),
            }
        )
        fills = self._fills_covering(placed, filled)
        if fills:
            n = sum(f.contracts for f in fills)
            placed = placed.model_copy(
                update={
                    "avg_price": round(sum(f.price * f.contracts for f in fills) / n, 6),
                    "fees": round(sum(f.fee for f in fills), 6),
                }
            )
        return placed, fills

    def refresh(self, order: Order) -> tuple[Order, list[Fill]]:
        if not order.venue_order_id:
            return order, []
        v = self._get_order(order)
        if v is None:
            return order, []
        updated = self._apply(order, v)
        fills = self._fills_covering(updated, updated.filled) if updated.filled > order.filled else []
        if fills:
            n = sum(f.contracts for f in fills)
            updated = updated.model_copy(
                update={"avg_price": round(sum(f.price * f.contracts for f in fills) / n, 6)}
            )
        return updated, fills

    def _get_order(self, order: Order) -> dict[str, Any] | None:
        """One order by id. Kalshi can answer 404 for a moment right after an order is placed or canceled,
        so it tries again, then looks for the order's client id in the order list."""
        for i in range(4):
            try:
                v = (self._call("GET", f"/portfolio/orders/{order.venue_order_id}") or {}).get("order")
                if isinstance(v, dict):
                    return v
            except VenueError as e:
                if e.code != "not_found":
                    raise
            if i < 3:
                self._sleep(0.5)
        params = {"ticker": order.market, "limit": 200}
        for v in (self._call("GET", "/portfolio/orders", params=params) or {}).get("orders") or []:
            if v.get("order_id") == order.venue_order_id or v.get("client_order_id") == order.client_id:
                return dict(v)
        return None

    def find(self, order: Order, *, since: datetime, attempts: int = 4, wait_s: float = 0.5) -> Order | None:
        """After an unknown outcome: the order with our ``client_order_id`` on that ticker since the send.

        Kalshi's order list can lag a new order by a moment, so it looks a few times before saying no.
        """
        params = {"ticker": order.market, "min_ts": int(since.timestamp()) - 5, "limit": 200}
        for i in range(attempts):
            for v in (self._call("GET", "/portfolio/orders", params=params) or {}).get("orders") or []:
                if v.get("client_order_id") == order.client_id:
                    return self._apply(order, v)
            if i < attempts - 1:
                self._sleep(wait_s)
        return None

    def cancel(self, order: Order) -> Order:
        _require_trading()
        if not order.venue_order_id:
            raise VenueError(
                "not_found",
                "This order has no Kalshi id.",
                venue=VENUE,
                retryable=False,
                next="client.sync()",
            )
        self._call(
            "DELETE",
            f"/portfolio/events/orders/{order.venue_order_id}",
            kind="cancel",
            params={"market_ticker": order.market},
        )
        # A 200 means the cancel executed (Kalshi's changelog, 2026-05-21: the answer's details can be
        # wrong, the cancel itself is right). Reads can lag it for a moment, so keep what's filled and
        # mark the rest canceled.
        updated = self.refresh(order)[0]
        if updated.status in ("open", "pending"):
            updated = updated.model_copy(update={"status": "canceled"})
        return updated

    def cancel_all(self) -> int:
        _require_trading()
        n = len(self._open_raw())
        self._call("DELETE", "/portfolio/events/orders", kind="cancel")
        return n

    def _open_raw(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = self._call("GET", "/portfolio/orders", params=params) or {}
            out.extend(o for o in body.get("orders") or [] if isinstance(o, dict))
            cursor = body.get("cursor")
            if not cursor:
                return out

    def open_orders(self) -> list[Order]:
        now = self._clock()
        out = []
        for v in self._open_raw():
            side = "yes" if v.get("outcome_side") == "yes" else "no"
            yes_px = _d(v.get("yes_price_dollars")) or 0.5
            price = yes_px if side == "yes" else round(1 - yes_px, 6)
            buy = (v.get("book_side") == "bid") == (side == "yes")
            expires = v.get("expiration_time")
            o = Order(
                venue=VENUE,
                market=str(v.get("ticker")),
                side=side,
                action="buy" if buy else "sell",
                price=price,
                size=_d(v.get("initial_count_fp")) or 1.0,
                tif="gtc",
                expires_at=datetime.fromisoformat(expires.replace("Z", "+00:00")) if expires else now,
                client_id=str(v.get("client_order_id") or uuid.uuid4()),
            )
            out.append(
                self._apply(o.model_copy(update={"id": o.client_id, "mode": "live", "created_at": now}), v)
            )
        return out

    def fills(self, *, since: datetime | None = None) -> Sequence[Fill]:
        """Every fill on the account since ``since`` (all of them without it), whoever sent the order.

        Each one is seen from YES: ``side="yes"`` at the YES price, ``action="buy"`` when it added YES
        contracts. ``order_id`` is Kalshi's order id. :meth:`uselayer.Client.reconcile` compares these
        with the store; an SDK order's own fills reach the store through :meth:`refresh`.

        Kalshi moves fills older than its cutoff (``/historical/cutoff``, about two months back) to
        ``/historical/fills``; those are read too when ``since`` is older than the cutoff.
        """
        out = self._account_fills("/portfolio/fills", since)
        try:
            cutoff = _when_iso((self._call("GET", "/historical/cutoff") or {}).get("trades_created_ts"))
        except VenueError as e:
            if e.code != "not_found":
                raise
            cutoff = None
        if cutoff is not None and (since is None or since < cutoff):
            seen = {f.venue_fill_id for f in out}
            out += [f for f in self._account_fills("/historical/fills", since) if f.venue_fill_id not in seen]
        return out

    def _account_fills(self, path: str, since: datetime | None) -> list[Fill]:
        out: list[Fill] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if since is not None:
                params["min_ts"] = int(since.timestamp())
            if cursor:
                params["cursor"] = cursor
            body = self._call("GET", path, params=params) or {}
            for f in body.get("fills") or []:
                n = _d(f.get("count_fp")) or 0.0
                yes = _d(f.get("yes_price_dollars"))
                if yes is None and _d(f.get("no_price_dollars")) is not None:
                    yes = round(1 - (_d(f.get("no_price_dollars")) or 0.0), 6)
                if n <= 0 or yes is None:
                    continue
                # ``book_side`` says which way YES moved. Kalshi's ``side``/``action`` don't: a YES sell
                # read side "no", action "sell" on the demo exchange (2026-10-04). Fills without
                # ``book_side`` predate it and use the older meaning.
                if f.get("book_side") in ("bid", "ask"):
                    buy_yes = f["book_side"] == "bid"
                else:
                    buy_yes = (f.get("side") == "yes") == (f.get("action") == "buy")
                at = datetime.fromtimestamp(f["ts"], UTC) if isinstance(f.get("ts"), int) else self._clock()
                billed = _d(f.get("fee_cost")) or 0.0
                out.append(
                    Fill(
                        venue=VENUE,
                        market=str(f.get("ticker") or f.get("market_ticker")),
                        order_id=str(f.get("order_id") or ""),
                        venue_fill_id=str(f.get("fill_id") or f.get("trade_id")),
                        side="yes",
                        action="buy" if buy_yes else "sell",
                        price=yes,
                        contracts=n,
                        role="taker" if f.get("is_taker") else "maker",
                        cost=round(yes * n, 6),
                        fee=billed,
                        fee_estimate=billed,
                        at=at,
                    )
                )
            cursor = body.get("cursor")
            if not cursor:
                return out

    def _positions(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            p = dict(params, limit=1000)
            if cursor:
                p["cursor"] = cursor
            body = self._call("GET", path, params=p) or {}
            out.extend(m for m in body.get("market_positions") or [] if isinstance(m, dict))
            cursor = body.get("cursor")
            if not cursor:
                return out

    def _behind(self, rows: list[dict[str, Any]]) -> set[str]:
        """Tickers this adapter filled on whose position row Kalshi hasn't updated since that fill."""
        updated: dict[str, datetime] = {}
        for m in rows:
            with contextlib.suppress(TypeError, ValueError, AttributeError):
                at = datetime.fromisoformat(str(m["last_updated_ts"]).replace("Z", "+00:00"))
                updated[str(m.get("ticker"))] = max(at, updated.get(str(m.get("ticker")), at))
        return {t for t, filled in self._filled_at.items() if t not in updated or updated[t] < filled}

    def positions(self, *, include_closed: bool = False) -> list[VenuePosition]:
        """Open positions, then settled ones (``settlement_status=all``), then older ones from ``/historical/positions``,
        deduplicated by ticker.

        Kalshi's positions can lag a fill by a moment, so after a fill it reads again (for up to about
        3 s) until that market's position shows it."""
        for i in range(6):
            open_ = self._positions("/portfolio/positions", {"settlement_status": "unsettled"})
            every = self._positions("/portfolio/positions", {"settlement_status": "all"})
            if not self._behind([*open_, *every]) or i == 5:
                break
            self._sleep(0.5)
        self._filled_at.clear()  # wait once per fill, never on every later read
        historical = self._positions("/historical/positions", {})
        seen: set[str] = set()
        out: list[VenuePosition] = []
        open_tickers = {m.get("ticker") for m in open_}
        for m in [*open_, *every, *historical]:
            t = str(m.get("ticker"))
            if t in seen:
                continue
            seen.add(t)
            n = _d(m.get("position_fp")) or 0.0
            if n == 0 and not include_closed:
                continue  # a closed-out market: Kalshi keeps the row, with 0 contracts
            out.append(
                VenuePosition(
                    venue=VENUE,
                    market=t,
                    side="yes" if n >= 0 else "no",
                    contracts=abs(n),
                    cost=_d(m.get("market_exposure_dollars")),
                    realized_pnl=_d(m.get("realized_pnl_dollars")),
                    fees=_d(m.get("fees_paid_dollars")),
                    settled=t not in open_tickers,
                    raw=m,
                )
            )
        return out

    def balance(self) -> Balance:
        b = self._call("GET", "/portfolio/balance") or {}
        cash = _d(b.get("balance_dollars"))
        if cash is None:
            cash = (b.get("balance") or 0) / 100
        pv = b.get("portfolio_value")
        return Balance(venue=VENUE, cash=cash, positions_value=None if pv is None else pv / 100, raw=b)


# ---- titles for Layer's matches ----


def event_titles(http: Http, event: str) -> dict[str, dict[str, str | None]]:
    """Each market's event title, question, outcome and times in one Kalshi event, from Kalshi's public
    market data (no key needed). Used by :mod:`uselayer.titles` to fill in Layer's matches."""
    body = http.request(
        "GET",
        f"{HOSTS['production']}{PREFIX}/events/{event}",
        venue="kalshi",
        params={"with_nested_markets": "true"},
    )
    e = (body or {}).get("event") or {}
    title = " — ".join(t for t in (e.get("title"), e.get("sub_title")) if t) or None
    out: dict[str, dict[str, str | None]] = {}
    for m in e.get("markets") or (body or {}).get("markets") or []:
        if not isinstance(m, dict) or not m.get("ticker"):
            continue
        out[str(m["ticker"])] = {
            "event": title,
            "question": m.get("title"),
            "outcome": m.get("yes_sub_title") or m.get("title"),
            "event_time": m.get("occurrence_datetime") or m.get("expected_expiration_time"),
            "close_time": m.get("close_time"),
        }
    return out
