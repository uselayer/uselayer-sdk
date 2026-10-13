"""A Polymarket wallet's cash, read from the Polygon chain without a key.

Polymarket (international) keeps a trader's cash in their wallet on Polygon as pUSD, its collateral token
since the exchange upgrade of 28 April 2026 (before, USDC.e). Neither Polymarket API says how much cash a
wallet holds, so it's read from the chain: one ``balanceOf`` call to the pUSD contract through a public
Polygon RPC node. This is a read only: nothing here signs or sends a transaction.

Free public nodes now and then refuse or drop a call, so the read goes to the next of :data:`RPCS` when one
fails. ``USELAYER_POLYGON_RPC`` points it at one node of your own (or a paid one) instead, the steady choice.
"""

from __future__ import annotations

import os

from ..errors import VenueError
from ..http import Http

#: Public Polygon nodes that need no key, tried in this order until one answers.
RPCS = ("https://1rpc.io/matic", "https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org")
RPC = RPCS[0]
#: pUSD, Polymarket's collateral (6 decimals), from https://docs.polymarket.com/resources/contracts
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
PUSD_DECIMALS = 6
BALANCE_OF = "0x70a08231"  # balanceOf(address)
VENUE = "polygon"
ATTEMPTS = 2  # a call a node refuses is tried once more on that node
TIMEOUT_S = 4.0  # a node that doesn't answer in this long is skipped for the next


class Polygon:
    """Reads ERC-20 balances on Polygon through a JSON-RPC node."""

    def __init__(self, http: Http, rpc_url: str | None = None) -> None:
        self._http = http
        own = rpc_url or os.environ.get("USELAYER_POLYGON_RPC")
        self._rpcs: tuple[str, ...] = (own,) if own else RPCS

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
        failed: VenueError | None = None
        for rpc in self._rpcs:
            for _ in range(ATTEMPTS):
                try:
                    body = self._http.request(
                        "POST", rpc, venue=VENUE, json=call, timeout_s=TIMEOUT_S, retries=0
                    )
                    return self._balance(body)
                except VenueError as e:
                    # A free public node now and then refuses a call it answers a moment later. One that
                    # doesn't answer at all (venue_unavailable) isn't asked again: the next node is.
                    failed = e
                    if e.code == "venue_unavailable":
                        break
        assert failed is not None
        raise failed

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
