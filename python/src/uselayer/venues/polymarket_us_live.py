"""Polymarket US trading with your own API key (live mode).

Keys come from polymarket.us/developer: a Key ID and a Secret Key (base64, Ed25519). Every request
is signed over ``timestamp_ms + METHOD + path`` and sent to ``api.polymarket.us``. Your key never
leaves your machine except as that signature.

How orders map (Polymarket US quotes everything from the YES / long side):

- buy YES at p   → ``ORDER_INTENT_BUY_LONG``, price p
- buy NO at p    → ``ORDER_INTENT_BUY_SHORT``, price 1 − p
- sell YES at p  → ``ORDER_INTENT_SELL_LONG``, price p
- sell NO at p   → ``ORDER_INTENT_SELL_SHORT``, price 1 − p

On a ``slug:short`` market, YES is the short side, so the long/short choice flips.

    from uselayer import Client, PolymarketUS
    client = Client(mode="live", polymarket_us=PolymarketUS(key_id="...", secret_key_path="~/.pmus/secret"))
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..books import Book
from ..errors import VenueError
from ..events import Fill
from ..fees import dollars
from ..fill import calculate_fee
from ..http import Http, outcome_unknown
from ..orders import Order
from .base import Balance, BookRead, MarketInfo, VenuePosition
from .polymarket_us import VENUE, PolymarketUSPublic, _levels, _ts, split_market

API = "https://api.polymarket.us"
WS_MARKETS = "wss://api.polymarket.us/v1/ws/markets"
GLOBAL_RATE_LIMIT_REJECT = "Global Rate Limit Exceeded"

_TIF = {
    "ioc": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "fok": "TIME_IN_FORCE_FILL_OR_KILL",
    "gtc": "TIME_IN_FORCE_GOOD_TILL_DATE",
}
_STATE = {
    "ORDER_STATE_PENDING_NEW": "pending",
    "ORDER_STATE_PENDING_RISK": "pending",
    "ORDER_STATE_NEW": "open",
    "ORDER_STATE_PARTIALLY_FILLED": "open",
    "ORDER_STATE_PENDING_REPLACE": "open",
    "ORDER_STATE_PENDING_CANCEL": "open",
    "ORDER_STATE_FILLED": "filled",
    "ORDER_STATE_CANCELED": "canceled",
    "ORDER_STATE_REPLACED": "canceled",
    "ORDER_STATE_REJECTED": "rejected",
    "ORDER_STATE_EXPIRED": "expired",
}
# Trades that never happened, as far as positions go.
_DEAD_TRADES = frozenset({"TRADE_STATE_BUSTED", "TRADE_STATE_REJECTED"})


@dataclass(frozen=True)
class PolymarketUS:
    """Your Polymarket US API key. Pass ``secret_key`` or ``secret_key_path``; nothing is printed or saved.

    PolymarketUS(key_id="...", secret_key_path="~/.pmus/secret")
    PolymarketUS.from_env()   # POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY
    """

    key_id: str
    secret_key: str | None = field(default=None, repr=False)
    secret_key_path: str | None = None

    @staticmethod
    def from_env() -> PolymarketUS:
        key_id = os.environ.get("POLYMARKET_US_KEY_ID")
        secret = os.environ.get("POLYMARKET_US_SECRET_KEY")
        if not key_id or not secret:
            raise VenueError(
                "auth_failed",
                "POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY aren't both set.",
                venue=VENUE,
                retryable=False,
                hint="Create a key at polymarket.us/developer.",
                next="PolymarketUS(key_id=..., secret_key_path=...)",
            )
        return PolymarketUS(key_id=key_id, secret_key=secret)

    def _private_key(self) -> Ed25519PrivateKey:
        raw = self.secret_key
        if raw is None and self.secret_key_path is not None:
            raw = Path(self.secret_key_path).expanduser().read_text()
        if not raw:
            raise VenueError(
                "auth_failed",
                "No Polymarket US secret key given.",
                venue=VENUE,
                retryable=False,
                next="PolymarketUS(key_id=..., secret_key_path=...)",
            )
        try:
            seed = base64.b64decode(raw.strip())
        except ValueError as e:
            raise VenueError(
                "auth_failed",
                "The Polymarket US secret key isn't base64.",
                venue=VENUE,
                retryable=False,
                hint="Paste the Secret Key exactly as polymarket.us/developer showed it.",
            ) from e
        if len(seed) not in (32, 64):
            raise VenueError(
                "auth_failed",
                f"The Polymarket US secret key decodes to {len(seed)} bytes, not 32 or 64.",
                venue=VENUE,
                retryable=False,
                hint="Paste the Secret Key exactly as it was shown.",
            )
        return Ed25519PrivateKey.from_private_bytes(seed[:32])


class Signer:
    """Signs requests for one key. The key stays in memory only."""

    def __init__(self, key: PolymarketUS, clock_ms: Callable[[], int] | None = None) -> None:
        self._key_id = key.key_id
        self._private = key._private_key()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(self._clock_ms())
        sig = self._private.sign(f"{ts}{method.upper()}{path}".encode())
        return {
            "X-PM-Access-Key": self._key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }


def _is_long(market: str, side: str) -> bool:
    _, short = split_market(market)
    return (side == "yes") != short


def _yes_price(market: str, side: str, price: float) -> float:
    return price if _is_long(market, side) else round(1 - price, 6)


def _from_yes(market: str, side: str, yes_price: float) -> float:
    return yes_price if _is_long(market, side) else round(1 - yes_price, 6)


def _fmt(x: float) -> str:
    """A price as the venue's decimal string: 0.55 → "0.55", 0.123 → "0.123"."""
    return format(Decimal(str(round(x, 6))).normalize(), "f")


def intent(order: Order) -> str:
    """The venue's ``intent`` for an order."""
    long = _is_long(order.market, order.side)
    return f"ORDER_INTENT_{order.action.upper()}_{'LONG' if long else 'SHORT'}"


def order_body(order: Order) -> dict[str, Any]:
    """The ``POST /v1/orders`` body for an order. Every order is a limit order."""
    slug, _ = split_market(order.market)
    body: dict[str, Any] = {
        "marketSlug": slug,
        "type": "ORDER_TYPE_LIMIT",
        "intent": intent(order),
        "price": {"value": _fmt(_yes_price(order.market, order.side, order.price)), "currency": "USD"},
        "quantity": order.size,
        "tif": _TIF[order.tif],
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    if order.tif == "gtc":
        assert order.expires_at is not None, "gtc orders always carry expires_at (order_ttl_s)"
        body["goodTillTime"] = order.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    else:
        # Fill-now orders answer with their executions, so the fill is known at once.
        body["synchronousExecution"] = True
    if order.post_only:
        body["participateDontInitiate"] = True
    return body


def _amount(a: Any) -> float | None:
    if isinstance(a, dict):
        a = a.get("value")
    if a is None or a == "":
        return None
    try:
        return float(a)
    except (TypeError, ValueError):
        return None


def _when(s: Any) -> datetime | None:
    return _ts(s) if isinstance(s, str) and s else None


def apply_venue_order(order: Order, v: dict[str, Any], *, now: datetime) -> Order:
    """Update our order from the venue's Order object (state, filled, average price, fees)."""
    state = str(v.get("state") or "")
    status = _STATE.get(state, order.status or "pending")
    filled = float(v.get("cumQuantity") or 0.0)
    avg_yes = _amount(v.get("avgPx"))
    upd: dict[str, Any] = {
        "venue_order_id": str(v.get("id") or order.venue_order_id or ""),
        "status": status,
        "filled": filled,
        "updated_at": now,
    }
    if avg_yes is not None and filled > 0:
        upd["avg_price"] = round(_from_yes(order.market, order.side, avg_yes), 6)
    fees = _amount(v.get("commissionNotionalTotalCollected"))
    if fees is not None:
        upd["fees"] = fees
    return order.model_copy(update=upd)


def fills_from_executions(order: Order, executions: Sequence[dict[str, Any]], *, now: datetime) -> list[Fill]:
    """Real fills from the venue's executions for one of our orders."""
    out = []
    for ex in executions:
        if ex.get("type") not in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
            continue
        shares = float(ex.get("lastShares") or 0)
        yes_px = _amount(ex.get("lastPx"))
        if shares <= 0 or yes_px is None:
            continue
        price = round(_from_yes(order.market, order.side, yes_px), 6)
        role: Literal["taker", "maker"] = "taker" if ex.get("aggressor") in (True, "true", None) else "maker"
        at = _when(ex.get("transactTime")) or now
        estimate = dollars(calculate_fee(_settings(order), contracts=shares, price=price, role=role, at=at))
        billed = _amount(ex.get("commissionNotionalCollected"))
        out.append(
            Fill(
                venue=VENUE,
                market=order.market,
                order_id=order.id or order.client_id,
                venue_fill_id=str(ex.get("tradeId") or ex.get("id") or "") or None,
                side=order.side,
                action=order.action,
                price=price,
                contracts=shares,
                role=role,
                cost=round(price * shares, 6),
                fee=billed if billed is not None else estimate,
                fee_estimate=estimate,
                at=at,
                group_id=order.group_id,
            )
        )
    return out


def filled_id(venue_order_id: str, filled: float) -> str:
    """The id of a fill ``refresh()`` can't match to a venue trade: the order's filled size when it was seen.

    ``"<venue order id>:<filled>"``, e.g. ``"CX5M98730YHH:1.0"``. Before 0.3.0 every ``refresh()`` fill had one.
    """
    return f"{venue_order_id}:{filled}"


def filled_range(f: Fill, venue_order_id: str | None) -> tuple[float, float] | None:
    """For a :func:`filled_id` fill of this order: the slice of its filled size it stands for, ``(lo, hi]``."""
    vid, sep, filled = (f.venue_fill_id or "").rpartition(":")
    if not sep or not venue_order_id or vid != venue_order_id:
        return None
    try:
        hi = float(filled)
    except ValueError:
        return None
    return round(hi - f.contracts, 6), hi


def oldest_first(fills: Sequence[Fill]) -> list[Fill]:
    """Fills by time; trades at the same time keep the venue's order (its history lists newest first)."""
    return sorted(reversed(list(fills)), key=lambda f: f.at)


def trades_within(trades: Sequence[Fill], lo: float, hi: float) -> list[Fill]:
    """The trades of one order (oldest first) that make up the slice ``(lo, hi]`` of its filled size."""
    out, cum = [], 0.0
    for t in trades:
        if lo < cum + t.contracts / 2 <= hi:
            out.append(t)
        cum += t.contracts
    return out


def _settings(order: Order) -> Any:
    from ..fill import FeeSettings

    return FeeSettings(venue=VENUE)


def _reject_error(order: Order, v: dict[str, Any], text: str) -> VenueError:
    if GLOBAL_RATE_LIMIT_REJECT in text:
        return VenueError(
            "venue_unavailable",
            "Polymarket US rejected the order because it was slow to process it (its latency stopgap).",
            venue=VENUE,
            raw=v,
            retryable=True,
            hint="Despite the message, this isn't a rate limit: the order was rejected and nothing was placed.",
            next="Check the book again, then send the order again if you still want it.",
        )
    return VenueError(
        "invalid_order",
        f"Polymarket US rejected the order: {text or 'no reason given'}",
        venue=VENUE,
        raw=v,
        retryable=False,
        hint="The venue's own answer is in .raw.",
        next="Fix the order and send it again.",
    )


class PolymarketUSLive(PolymarketUSPublic):
    """Polymarket US with your key: fresh books over the WebSocket, orders, fills, positions and balance."""

    def __init__(
        self,
        http: Http,
        key: PolymarketUS,
        *,
        api_url: str = API,
        ws_url: str = WS_MARKETS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ws_connect: Callable[..., Any] | None = None,
        on_alert: Callable[[dict[str, Any]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(http)
        self._signer = Signer(key)
        self._api = api_url.rstrip("/")
        self._ws_url = ws_url
        self._clock = clock
        self._ws_connect = ws_connect
        self._alert = on_alert or (lambda _e: None)
        self._sleep = sleep

    # ---- signed HTTP ----

    def _call(
        self,
        method: str,
        path: str,
        *,
        kind: Any = "read",
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        return self._http.request(
            method,
            self._api + path,
            venue=VENUE,
            kind=kind,
            params=params,
            json=body,
            headers=lambda: self._signer.headers(method, path),
        )

    # ---- fresh books ----

    def read_book(self, market: str) -> BookRead:
        """A fresh book from the markets WebSocket; the public (cached) book if the WebSocket fails."""
        try:
            return self._ws_book(market)
        except Exception as e:  # any WebSocket problem falls back to the public book
            self._alert({"kind": "websocket_book_failed", "market": market, "error": str(e)[:300]})
            return super().read_book(market)

    def _ws_book(self, market: str) -> BookRead:
        slug, short = split_market(market)
        connect = self._ws_connect
        if connect is None:
            from websockets.sync.client import connect as ws_connect

            connect = ws_connect
        headers = self._signer.headers("GET", "/v1/ws/markets")
        headers.pop("Content-Type", None)
        with connect(self._ws_url, additional_headers=headers, open_timeout=5, close_timeout=1) as ws:
            served_at = None
            resp = getattr(ws, "response", None)
            date = resp.headers.get("Date") if resp is not None else None
            if date:
                try:
                    served_at = parsedate_to_datetime(date)
                except (TypeError, ValueError):
                    served_at = None
            ws.send(
                json.dumps(
                    {
                        "subscribe": {
                            "requestId": "uselayer-book",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": [slug],
                            "responsesDebounced": False,
                        }
                    }
                )
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                if msg.get("error"):
                    raise VenueError(
                        "venue_unavailable", f"Polymarket US WebSocket: {msg['error']}", venue=VENUE, raw=msg
                    )
                md = msg.get("marketData")
                if isinstance(md, dict) and md.get("marketSlug", slug) == slug:
                    changed = _when(md.get("transactTime"))
                    as_of = (
                        max(t for t in (changed, served_at) if t is not None)
                        if (changed or served_at)
                        else self._clock()
                    )
                    book = Book(
                        venue=VENUE,
                        market=market,
                        bids=_levels(md.get("bids"), "ws"),
                        asks=_levels(md.get("offers"), "ws"),
                        as_of=as_of,
                    )
                    if short:
                        no = book.outcome("no")
                        book = Book(venue=VENUE, market=market, bids=no.bids, asks=no.asks, as_of=book.as_of)
                    state = md.get("state")
                    return BookRead(book, state if isinstance(state, str) else None)
        raise VenueError(
            "venue_unavailable", "No book arrived on the Polymarket US WebSocket within 5 s.", venue=VENUE
        )

    # ---- orders ----

    def place(self, order: Order) -> tuple[Order, list[Fill]]:
        """Send an order. Returns it updated from the venue, plus any fills the venue reported at once."""
        now = self._clock()
        answer = self._call("POST", "/v1/orders", kind="order", body=order_body(order))
        vid = str((answer or {}).get("id") or "")
        executions = list((answer or {}).get("executions") or [])
        placed = order.model_copy(
            update={"venue_order_id": vid or None, "status": "pending", "updated_at": now}
        )
        if executions:
            last = executions[-1].get("order") or {}
            placed = apply_venue_order(placed, {**last, "id": vid or last.get("id")}, now=now)
            if placed.status == "rejected":
                text = " ".join(str(e.get("text") or e.get("orderRejectReason") or "") for e in executions)
                raise _reject_error(placed, {"id": vid, "executions": executions}, text)
        elif vid:
            # Read back, with whatever it filled at once (a resting order that crossed the book).
            return self.refresh(placed)
        return placed, fills_from_executions(placed, executions, now=now)

    def _reread(self, order: Order) -> Order:
        """One of our orders as the venue shows it now. Unchanged while the venue's lookup doesn't know it yet."""
        if not order.venue_order_id:
            return order
        v = self._get_order(order.venue_order_id)
        # None: still unknown to the lookup. The venue took it (it gave an id), so keep it as pending for sync().
        return order if v is None else apply_venue_order(order, v, now=self._clock())

    def refresh(self, order: Order) -> tuple[Order, list[Fill]]:
        """Re-read one of our orders, with the fills since it was last read.

        The order's filled size says how much is new. Each new fill is one of the order's trades from the
        account's activity history (:meth:`fills`), so it carries the venue's trade id, time and fee. Any part
        the history doesn't show yet is one fill at the order's average price, stamped with the time it was
        seen and a :func:`filled_id`.
        """
        updated = self._reread(order)
        new = round(updated.filled - order.filled, 6)
        if new <= 0 or updated.avg_price is None:
            return updated, []
        fills = [self._as_fill_of(order, t) for t in self._new_trades(order, updated)]
        traded = round(sum(f.contracts for f in fills), 6)
        rest = round(new - traded, 6)
        if rest > 1e-6:
            prev_cost = (order.avg_price or 0.0) * order.filled + sum(f.cost for f in fills)
            price = round((updated.avg_price * updated.filled - prev_cost) / rest, 6)
            at = self._clock()
            estimate = dollars(
                calculate_fee(_settings(order), contracts=rest, price=price, role="maker", at=at)
            )
            billed_total = updated.fees
            billed = (
                None
                if billed_total is None
                else round(billed_total - (order.fees or 0.0) - sum(f.fee for f in fills), 6)
            )
            fills.append(
                Fill(
                    venue=VENUE,
                    market=order.market,
                    order_id=order.id or order.client_id,
                    venue_fill_id=filled_id(order.venue_order_id or "", updated.filled),
                    side=order.side,
                    action=order.action,
                    price=price,
                    contracts=rest,
                    role="maker",
                    cost=round(price * rest, 6),
                    fee=billed if billed is not None else estimate,
                    fee_estimate=estimate,
                    at=at,
                    group_id=order.group_id,
                )
            )
        return updated, fills

    def _new_trades(self, order: Order, updated: Order) -> list[Fill]:
        """The order's trades that make up its filled size from ``order.filled`` to ``updated.filled``."""
        vid = order.venue_order_id
        since = None if order.created_at is None else order.created_at - timedelta(minutes=5)
        try:
            trades = oldest_first([f for f in self.fills(since=since) if f.order_id == vid])
        except VenueError as e:
            self._alert({"kind": "fill_history_failed", "venue_order_id": vid, "error": str(e)[:300]})
            return []
        return trades_within(trades, order.filled, updated.filled)

    def _as_fill_of(self, order: Order, t: Fill) -> Fill:
        """One of the order's trades from :meth:`fills` (long/short terms) as a fill of ``order``."""
        price = t.price if (t.side == "yes") == _is_long(order.market, order.side) else round(1 - t.price, 6)
        estimate = dollars(
            calculate_fee(_settings(order), contracts=t.contracts, price=price, role=t.role, at=t.at)
        )
        return t.model_copy(
            update={
                "market": order.market,
                "order_id": order.id or order.client_id,
                "side": order.side,
                "action": order.action,
                "price": price,
                "cost": round(price * t.contracts, 6),
                "fee_estimate": estimate,
                "group_id": order.group_id,
            }
        )

    def _get_order(
        self, venue_order_id: str, attempts: int = 5, wait_s: float = 0.5
    ) -> dict[str, Any] | None:
        """One order by id. Right after it's placed, Polymarket US can answer 404 for a moment
        (seen live 2026-10-01), so a 404 is retried briefly before giving up."""
        for i in range(attempts):
            try:
                v = (self._call("GET", f"/v1/order/{venue_order_id}") or {}).get("order")
                if isinstance(v, dict):
                    return v
            except VenueError as e:
                if e.code != "not_found":
                    raise
            if i < attempts - 1:
                self._sleep(wait_s)
        return None

    def find(self, order: Order, *, since: datetime) -> Order | None:
        """After an unknown outcome: our order among the venue's open orders, matched on market, intent, price and size.

        Polymarket US has no client order id, so this can only find an order that is still open.
        It returns ``None`` when it can't be sure; the caller must not send the order again then.
        """
        slug, _ = split_market(order.market)
        want_price = _fmt(_yes_price(order.market, order.side, order.price))
        found = []
        for v in self._open_raw([slug]):
            created = _when(v.get("createTime")) or _when(v.get("insertTime"))
            if (
                v.get("intent") == intent(order)
                and _fmt(_amount(v.get("price")) or -1) == want_price
                and float(v.get("quantity") or 0) == order.size
                and (created is None or created >= since - timedelta(seconds=5))
            ):
                found.append(v)
        if len(found) != 1:
            return None
        return apply_venue_order(order, found[0], now=self._clock())

    def cancel(self, order: Order) -> Order:
        if not order.venue_order_id:
            raise VenueError(
                "not_found",
                "This order has no Polymarket US id, so it can't be canceled there.",
                venue=VENUE,
                retryable=False,
                next="client.sync()",
            )
        slug, _ = split_market(order.market)
        self._call(
            "POST", f"/v1/order/{order.venue_order_id}/cancel", kind="cancel", body={"marketSlug": slug}
        )
        # The cancel is applied a moment after the venue accepts it (seen live 2026-10-01): re-read until
        # the order shows a final state. If it still shows open, it's reported as open, never assumed canceled.
        updated = order
        for i in range(6):
            updated = self._reread(order)
            if updated.status not in ("open", "pending"):
                break
            if i < 5:
                self._sleep(0.5)
        return updated

    def cancel_all(self) -> int:
        answer = self._call("POST", "/v1/orders/open/cancel", kind="cancel", body={"slugs": []})
        return len((answer or {}).get("canceledOrderIds") or [])

    def _open_raw(self, slugs: Sequence[str] = ()) -> list[dict[str, Any]]:
        params = {"slugs": list(slugs)} if slugs else None
        answer = self._call("GET", "/v1/orders/open", params=params) or {}
        return [o for o in answer.get("orders") or [] if isinstance(o, dict)]

    def open_orders(self) -> list[Order]:
        """Open orders on the venue, including ones not sent by this SDK."""
        now = self._clock()
        out = []
        for v in self._open_raw():
            long = str(v.get("intent") or "").endswith("_LONG")
            action: Literal["buy", "sell"] = "sell" if "_SELL_" in str(v.get("intent") or "") else "buy"
            yes_px = _amount(v.get("price")) or 0.5
            slug = str(v.get("marketSlug") or "")
            base = Order(
                venue=VENUE,
                market=slug,
                side="yes" if long else "no",
                action=action,
                price=yes_px if long else round(1 - yes_px, 6),
                size=float(v.get("quantity") or 1) or 1.0,
                tif="gtc",
                expires_at=_when(v.get("goodTillTime")) or now + timedelta(days=1),
                reason="open",
            )
            o = base.model_copy(
                update={
                    "id": str(v.get("id")),
                    "mode": "live",
                    "created_at": _when(v.get("createTime")) or now,
                }
            )
            out.append(apply_venue_order(o, v, now=now))
        return out

    def fills(self, *, since: datetime | None = None) -> Sequence[Fill]:
        """Every fill on the account since ``since`` (all of them without it), whoever sent the order.

        Read from the account's trade activity, newest first. Each trade lists both sides' executions;
        ``isAggressor`` says which is this account's. ``market`` is the slug and ``side`` is ``"yes"`` for
        the long side, ``"no"`` for the short side, at that side's price. ``order_id`` is the venue's order
        id and ``venue_fill_id`` its ``tradeId``. Busted and rejected trades are left out.
        :meth:`uselayer.Client.reconcile` compares these with the store.
        """
        out: list[Fill] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "types": ["ACTIVITY_TYPE_TRADE"],
                "sortOrder": "SORT_ORDER_DESCENDING",
            }
            if cursor:
                params["cursor"] = cursor
            answer = self._call("GET", "/v1/portfolio/activities", params=params) or {}
            for a in answer.get("activities") or []:
                t = a.get("trade") if isinstance(a, dict) else None
                if not isinstance(t, dict) or t.get("state") in _DEAD_TRADES:
                    continue
                f = self._own_fill(t)
                if since is not None and f.at < since:
                    return out
                out.append(f)
            cursor = answer.get("nextCursor")
            if answer.get("eof", True) or not cursor:
                return out

    def _own_fill(self, t: dict[str, Any]) -> Fill:
        mine = t.get("aggressorExecution" if t.get("isAggressor") else "passiveExecution")
        order = (mine or {}).get("order") if isinstance(mine, dict) else None
        intent_ = str((order or {}).get("intent") or "")
        if (
            not isinstance(mine, dict)
            or not isinstance(order, dict)
            or not intent_.endswith(("_LONG", "_SHORT"))
        ):
            raise VenueError(
                "format_changed",
                "A Polymarket US trade doesn't say which execution is this account's, or which way it went.",
                venue=VENUE,
                raw=t,
            )
        long = intent_.endswith("_LONG")
        shares = float(mine.get("lastShares") or t.get("qtyDecimal") or 0)
        yes_px = _amount(mine.get("lastPx")) or _amount(t.get("price")) or 0.0
        price = yes_px if long else round(1 - yes_px, 6)
        billed = _amount(mine.get("commissionNotionalCollected")) or 0.0
        return Fill(
            venue=VENUE,
            market=str(t.get("marketSlug") or order.get("marketSlug")),
            order_id=str(order.get("id") or ""),
            venue_fill_id=str(mine.get("tradeId") or t.get("id")),
            side="yes" if long else "no",
            action="sell" if "_SELL_" in intent_ else "buy",
            price=price,
            contracts=shares,
            role="taker" if t.get("isAggressor") else "maker",
            cost=round(price * shares, 6),
            fee=billed,
            fee_estimate=billed,
            at=_when(mine.get("transactTime")) or _when(t.get("createTime")) or self._clock(),
        )

    def positions(self, *, include_closed: bool = False) -> list[VenuePosition]:
        """Positions held. ``cost`` is what the contracts cost before fees (``baseCost``); ``fees`` is what was charged.

        With ``include_closed``, also the ones closed out or settled, for their realized P&L. Polymarket US
        drops a settled position from its positions list, so those come from the account's activity
        history (``POSITION_RESOLUTION``), as do the fees actually charged on each market.
        """
        out: list[VenuePosition] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            answer = self._call("GET", "/v1/portfolio/positions", params=params) or {}
            for slug, p in (answer.get("positions") or {}).items():
                net = float(p.get("netPositionDecimal") or 0)
                if net == 0 and not include_closed:
                    continue
                cost, fees, base = _amount(p.get("cost")), _amount(p.get("fees")), _amount(p.get("baseCost"))
                # ``cost`` counts the fees when the venue fills ``fees`` in (checked 10-04: cost = baseCost + fees).
                if base is None:
                    base = cost if cost is None or fees is None else round(cost - fees, 6)
                out.append(
                    VenuePosition(
                        venue=VENUE,
                        market=slug,
                        side="yes" if net > 0 else "no",
                        contracts=abs(net),
                        avg_price=None if base is None or net == 0 else round(abs(base) / abs(net), 6),
                        cost=base,
                        realized_pnl=_amount(p.get("realized")),
                        fees=fees,
                        settled=bool(p.get("expired")),
                        raw=p,
                    )
                )
            cursor = answer.get("nextCursor")
            if answer.get("eof", True) or not cursor:
                break
        if include_closed:
            out = self._with_history(out)
        return out

    def _resolutions(self) -> dict[str, dict[str, Any]]:
        """Each settled market's ``positionResolution`` (before and after the payout), by slug."""
        out: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100, "types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"]}
            if cursor:
                params["cursor"] = cursor
            answer = self._call("GET", "/v1/portfolio/activities", params=params) or {}
            for a in answer.get("activities") or []:
                pr = a.get("positionResolution") if isinstance(a, dict) else None
                if isinstance(pr, dict) and pr.get("marketSlug"):
                    out.setdefault(str(pr["marketSlug"]), pr)
            cursor = answer.get("nextCursor")
            if answer.get("eof", True) or not cursor:
                return out

    def _with_history(self, held: list[VenuePosition]) -> list[VenuePosition]:
        """Fees charged per market (from :meth:`fills`), and settled positions, from the activity history."""
        fees: dict[str, float] = {}
        for f in self.fills():
            fees[f.market] = fees.get(f.market, 0.0) + f.fee
        out = [p if p.market not in fees else replace(p, fees=round(fees[p.market], 6)) for p in held]
        listed = {p.market for p in out}
        for slug, pr in self._resolutions().items():
            if slug in listed:
                continue
            before = pr.get("beforePosition") or {}
            after = pr.get("afterPosition") or {}
            net = float(before.get("netPositionDecimal") or before.get("netPosition") or 0)
            out.append(
                VenuePosition(
                    venue=VENUE,
                    market=slug,
                    side="yes" if net >= 0 else "no",
                    contracts=0.0,
                    cost=0.0,
                    realized_pnl=_amount(after.get("realized")),
                    fees=round(fees.get(slug, 0.0), 6),
                    settled=True,
                    raw=pr,
                )
            )
        return out

    def balance(self) -> Balance:
        """``cash`` is what you can spend now (``buyingPower``). Polymarket US's ``currentBalance`` also counts
        the margin it holds against short positions (checked 10-04: currentBalance = buyingPower + marginRequirement).
        """
        answer = self._call("GET", "/v1/account/balances") or {}
        for b in answer.get("balances") or []:
            if str(b.get("currency", "USD")).upper() in ("USD", "CURRENCY_USD"):
                spendable = _amount(b.get("buyingPower"))
                return Balance(
                    venue=VENUE,
                    cash=spendable if spendable is not None else float(b.get("currentBalance") or 0),
                    in_orders=_amount(b.get("openOrders")),
                    positions_value=_amount(b.get("assetNotional")),
                    raw=b,
                )
        raise VenueError(
            "format_changed", "Polymarket US's balances answer has no USD balance.", venue=VENUE, raw=answer
        )

    def market(self, market: str) -> MarketInfo:
        return super().market(market)


def placed_or_unknown(adapter: PolymarketUSLive, order: Order, *, since: datetime) -> Order:
    """After ``outcome_unknown``: the order if the venue shows it, else raise ``outcome_unknown`` again (never resend)."""
    found = adapter.find(order, since=since)
    if found is not None:
        return found
    raise outcome_unknown(
        VENUE, "The order may or may not have reached Polymarket US, and it isn't among the open orders."
    )
