"""What this public package ships: Polymarket US and Kalshi live with your own keys, no Polymarket
international trading code and no telemetry."""

from __future__ import annotations

from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "uselayer"
FORBIDDEN = [
    "clob.polymarket.com",  # Polymarket international's order book and trading API
    "py_clob_client",
    "polymarket_client",
    "telemetry",
    "sentry",
    "posthog",
]
# Kalshi's signing and order code lives in one file; nowhere else talks to Kalshi.
KALSHI_ONLY_IN = {"venues/kalshi.py", "http.py"}
KALSHI_WORDS = ["trade-api/v2", "KALSHI-ACCESS", "demo-api.kalshi", "elections.kalshi", "portfolio/events"]


def test_no_switched_off_venue_code_or_telemetry_ships() -> None:
    for f in PKG.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".txt", ".md"):
            text = f.read_text()
            for word in FORBIDDEN:
                assert word not in text, f"{word!r} in {f.relative_to(PKG)}"


def test_kalshi_code_is_in_its_adapter_only() -> None:
    for f in PKG.rglob("*.py"):
        rel = str(f.relative_to(PKG))
        if rel in KALSHI_ONLY_IN:
            continue
        text = f.read_text()
        for word in KALSHI_WORDS:
            assert word not in text, f"{word!r} in {rel}"


def test_live_kalshi_orders_are_on() -> None:
    from uselayer import _switches

    assert _switches.TRADING["kalshi"] is True and "kalshi" in _switches.LIVE_ADAPTERS
    assert "kalshi" in _switches.PAPER


def test_the_package_talks_only_to_layer_polymarket_us_and_kalshi() -> None:
    hosts = set()
    for f in PKG.rglob("*.py"):
        for token in f.read_text().split('"'):
            if token.startswith("https://") and "/" in token[8:]:
                hosts.add(token[8:].split("/")[0])
            elif token.startswith("https://"):
                hosts.add(token[8:])
    # Sources cited in venue_rules (fee pages, archived copies and CFTC filings) are links, never called.
    cited = ("docs.", "kalshi.com", "github.com", "json-schema.org")
    cited += ("web.archive.org", "www.cftc.gov", "www.polymarketexchange.com")
    api_hosts = {h for h in hosts if not h.startswith(cited)}
    assert api_hosts <= {
        "uselayer.sh",
        "gateway.polymarket.us",
        "api.polymarket.us",
        "api.elections.kalshi.com",
        "demo-api.kalshi.co",
    }, api_hosts
