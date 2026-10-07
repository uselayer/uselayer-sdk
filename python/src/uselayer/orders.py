"""The one order shape, the same for every venue and every mode.

Every order, whether you send it, a stop-loss sends it or the kill switch sends it, has this shape.
It's published as a JSON Schema in ``schema/order.json``. There are no market orders: every order has
a limit price.

    order = client.order(venue="polymarket_us", market="some-slug", side="yes", price=0.42, size=10)
    client.preview(order).allowed
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VenueName = Literal["polymarket_us", "kalshi", "polymarket"]
Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
TimeInForce = Literal["gtc", "ioc", "fok"]
Reason = Literal["open", "exit", "unwind", "kill"]
Mode = Literal["paper", "live", "backtest"]
Status = Literal["pending", "open", "filled", "canceled", "rejected", "expired"]

OPEN_STATUSES: frozenset[str] = frozenset({"pending", "open"})


class Order(BaseModel):
    """An order: what you ask for, plus what happened to it once sent.

    What you ask for:
        venue: ``"polymarket_us"`` or ``"kalshi"``; ``"polymarket"`` is switched off.
        market: the venue's id: a Polymarket US slug, or ``slug:long`` / ``slug:short``.
        side: the outcome you trade, ``"yes"`` or ``"no"``.
        action: ``"buy"`` or ``"sell"``.
        price: limit price in dollars per contract, above 0 and below 1.
        size: contracts.
        tif: ``"ioc"`` (fill now or cancel the rest, the default), ``"fok"`` (all now or nothing)
            or ``"gtc"`` (rest in the book until ``expires_at``).
        expires_at: when a ``gtc`` order stops resting; defaults to now + ``order_ttl_s``.
        post_only: rest only; an order that would fill at once is rejected.
        client_id: set by the SDK; finds the order again after a timeout.
        group_id: the pair this order belongs to.
        reason: ``"open"``, ``"exit"``, ``"unwind"`` or ``"kill"``.

    What you get back (``None`` until sent):
        id, venue_order_id, mode, status, filled, avg_price, fees, fee_estimate, created_at, updated_at.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    venue: VenueName
    market: str = Field(min_length=1, max_length=300)
    side: Side
    action: Action = "buy"
    price: float = Field(gt=0, lt=1)
    size: float = Field(gt=0)
    tif: TimeInForce = "ioc"
    expires_at: datetime | None = None
    post_only: bool = False
    client_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    group_id: str | None = None
    reason: Reason = "open"

    id: str | None = None
    venue_order_id: str | None = None
    mode: Mode | None = None
    status: Status | None = None
    filled: float = 0.0
    avg_price: float | None = None
    fees: float | None = None
    fee_estimate: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Order:
        if self.expires_at is not None and self.tif != "gtc":
            raise ValueError("expires_at is only for gtc orders")
        if self.post_only and self.tif != "gtc":
            raise ValueError("post_only orders rest in the book, so they must be gtc")
        return self

    @property
    def is_open(self) -> bool:
        """Still resting or in flight."""
        return self.status in OPEN_STATUSES

    @property
    def remaining(self) -> float:
        """Contracts not filled yet."""
        return max(0.0, round(self.size - self.filled, 6))

    def to_dict(self) -> dict[str, Any]:
        """The order as plain data, with the fields of ``schema/order.json``.

        client.order(venue="polymarket_us", market="m", side="yes", price=0.4, size=1).to_dict()["tif"]  # "ioc"
        """
        return self.model_dump(mode="json")

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


def order_schema() -> dict[str, Any]:
    """The JSON Schema for :class:`Order`, as published in ``schema/order.json``."""
    schema = Order.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/Dave-56/uselayer-sdk/blob/main/schema/order.json"
    return schema
