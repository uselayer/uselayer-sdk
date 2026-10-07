"""uselayer: trade prediction markets with your own venue keys.

Paper is the default. Read AGENTS.md (in this package) before letting an agent trade.

    from uselayer import Client
    client = Client()                       # paper mode: real books, fake money
    book = client.book("some-polymarket-us-slug")
"""

from __future__ import annotations

from ._switches import paper_venues, trading_venues
from ._version import __version__
from .best import BestOrder, BestVenue, VenueCost
from .books import Book, BookLevelChange, Level, OutcomeBook, reconstruct_book
from .calc import profit, size
from .client import Admin, Client, Preview
from .errors import VenueError
from .events import (
    Fill,
    MarketStatus,
    Resolution,
    SimulatedFill,
    SimulatedPosition,
    SimulatedSettlement,
    StreamGap,
    TradePrint,
)
from .fill import FeeSettings, FillEstimate, calculate_fee, estimate_fill
from .guardrails import Context, Decision, Mark, RulesConfig
from .imports import CheckReport, Imported, check_events, import_events
from .layer_api import Market, Match
from .mismatch import MismatchLeg, ResolutionMismatch
from .orders import Order
from .pnl import Pnl, PnlRow
from .prices import LegPrices, Prices
from .reconcile import Mismatch, Reconciliation
from .record import RecordSummary, record_stream
from .trading import Exposure, Quote, QuoteLeg, Trade
from .venue_rules import VenueRules, rules_at
from .venues.base import Balance, MarketInfo, VenuePosition
from .venues.kalshi import Kalshi
from .venues.polymarket_us_live import PolymarketUS
from .whale_scores import Discovery, Score
from .whales import Copier, CopyEvent, Evidence, Link, Trader, TraderDetail, WhalePosition, Whales, WhaleTrade

__all__ = [
    "Admin",
    "Balance",
    "BestOrder",
    "BestVenue",
    "Book",
    "BookLevelChange",
    "CheckReport",
    "Client",
    "Context",
    "Copier",
    "CopyEvent",
    "Decision",
    "Discovery",
    "Evidence",
    "Exposure",
    "FeeSettings",
    "Fill",
    "FillEstimate",
    "Imported",
    "Kalshi",
    "LegPrices",
    "Level",
    "Link",
    "Mark",
    "Market",
    "MarketInfo",
    "MarketStatus",
    "Match",
    "Mismatch",
    "MismatchLeg",
    "Order",
    "OutcomeBook",
    "Pnl",
    "PnlRow",
    "PolymarketUS",
    "Preview",
    "Prices",
    "Quote",
    "QuoteLeg",
    "Reconciliation",
    "RecordSummary",
    "Resolution",
    "ResolutionMismatch",
    "RulesConfig",
    "Score",
    "SimulatedFill",
    "SimulatedPosition",
    "SimulatedSettlement",
    "StreamGap",
    "Trade",
    "TradePrint",
    "Trader",
    "TraderDetail",
    "VenueCost",
    "VenueError",
    "VenuePosition",
    "VenueRules",
    "WhalePosition",
    "WhaleTrade",
    "Whales",
    "__version__",
    "calculate_fee",
    "check_events",
    "estimate_fill",
    "import_events",
    "paper_venues",
    "profit",
    "reconstruct_book",
    "record_stream",
    "rules_at",
    "size",
    "trading_venues",
]
