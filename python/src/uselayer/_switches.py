"""Which venues this release trades live, which it paper-trades, and which data feeds it may use.

These values are fixed per release. There is no environment variable, flag or argument that
changes them: a venue is switched on by publishing a new release with its adapter included.
"""

from __future__ import annotations

from typing import Final

# Live orders, sent with your own key.
TRADING: Final[dict[str, bool]] = {"polymarket_us": True, "kalshi": True, "polymarket": False}

# Venues whose markets and books this release reads (Kalshi with your own key), for paper mode,
# backtest mode and pairs.
PAPER: Final[frozenset[str]] = frozenset({"polymarket_us", "kalshi"})

# Venues a backtest can replay: your own data, with the venue's fee rules. Nothing is sent anywhere.
BACKTEST: Final[frozenset[str]] = frozenset({"polymarket_us", "kalshi", "polymarket"})

# Replaying Layer's recorded order books in backtest mode. The plumbing ships; the feed stays off.
LAYER_HISTORY: Final[bool] = False

# Venues with a live adapter in this release (orders sent with your own key).
LIVE_ADAPTERS: Final[frozenset[str]] = frozenset({"polymarket_us", "kalshi"})


def trading_venues() -> list[str]:
    """The venues this release trades live, e.g. ``["polymarket_us", "kalshi"]``."""
    return [v for v, on in TRADING.items() if on]


def paper_venues() -> list[str]:
    """The venues this release reads and fills in paper and backtest mode, e.g. ``["kalshi", "polymarket_us"]``."""
    return sorted(PAPER)
