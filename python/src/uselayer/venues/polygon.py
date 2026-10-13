"""A Polymarket wallet's cash, read from the Polygon chain without a key.

Polymarket (international) keeps a trader's cash in their wallet on Polygon as pUSD, its collateral token
since the exchange upgrade of 28 April 2026 (before, USDC.e). Neither Polymarket API says how much cash a
wallet holds, so it's read from the chain: one ``balanceOf`` call to the pUSD contract through a public
Polygon RPC node. This is a read only: nothing here signs or sends a transaction.

``USELAYER_POLYGON_RPC`` points it at another node (your own, or a paid one) instead of the default.
"""

from __future__ import annotations

import os

from ..errors import VenueError
from ..http import Http

#: A public Polygon node that needs no key.
RPC = "https://1rpc.io/matic"
#: pUSD, Polymarket's collateral (6 decimals), from https://docs.polymarket.com/resources/contracts
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
PUSD_DECIMALS = 6
BALANCE_OF = "0x70a08231"  # balanceOf(address)
VENUE = "polygon"
ATTEMPTS = 2  # a refused call is tried once more
TIMEOUT_S = 5.0  # a public node that doesn't answer in this long is given one retry, then skipped


class Polygon:
    """Reads ERC-20 balances on Polygon through a JSON-RPC node."""

    def __init__(self, http: Http, rpc_url: str | None = None) -> None:
        self._http = http
        self._rpc = rpc_url or os.environ.get("USELAYER_POLYGON_RPC") or RPC

    def cash(self, wallet: str) -> float:
        """The pUSD a wallet holds, in dollars. Raises :class:`VenueError` if the node can't be read."""
        address = wallet.lower().removeprefix("0x")
        if len(address) != 40 or any(c not in "0123456789abcdef" for c in address):
            raise ValueError(f"{wallet!r} is not a wallet address")
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": PUSD, "data": BALANCE_OF + address.rjust(64, "0")}, "latest"],
        }
        for attempt in range(ATTEMPTS):
            try:
                body = self._http.request(
                    "POST", self._rpc, venue=VENUE, json=call, timeout_s=TIMEOUT_S, retries=1
                )
                return self._balance(body)
            except VenueError as e:
                # A free public node now and then refuses a call it answers a moment later. One that
                # doesn't answer at all (venue_unavailable, already retried) isn't asked again.
                if attempt == ATTEMPTS - 1 or e.code == "venue_unavailable":
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def _balance(body: object) -> float:
        result = body.get("result") if isinstance(body, dict) else None
        try:
            return float(int(str(result), 16) / 10**PUSD_DECIMALS)
        except ValueError:
            raise VenueError(
                "format_changed",
                "The Polygon node didn't return a balance.",
                venue=VENUE,
                raw=body,
                next="Try again later, or set USELAYER_POLYGON_RPC to another node.",
            ) from None
