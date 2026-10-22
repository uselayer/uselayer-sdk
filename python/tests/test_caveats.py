"""The SDK writes Layer's rule-flag notes from both venues' rules, read on your machine (uselayer.caveats).

Rules text below is as the venues write it (Kalshi's NBA game, Hyperliquid's sportsContestWinner
template filled in, 10-21); the markets are made up.
"""

from __future__ import annotations

from typing import Any

from uselayer.caveats import explain, quote
from uselayer.layer_api import with_notes

KALSHI_RULES = (
    "If Orlando wins the Orlando vs Minnesota professional basketball game originally scheduled for "
    "Oct 23, 2026, then the market resolves to Yes.\n\nThe outcome is based on the official result from the NBA."
)
HL_RULES = (
    "It covers the Game between Orlando Magic and Minnesota Timberwolves in the 2026/27 NBA Regular Season, "
    'scheduled for October 23, 2026 at 11:00 PM UTC (the "Contest"). The Counted Play is the full game '
    "including overtime. The Contest Result is the official record of the Counted Play. Play outside the "
    "Counted Play, including any extra time, overtime, shoot-out, or tiebreak, is disregarded, and a winner "
    "decided by such play is not the winner of the Counted Play, even if any source treats it as part of the Contest."
)

KALSHI: dict[str, Any] = {
    "venue": "kalshi",
    "market_id": "KXNBAGAME-26OCT23ORLMIN-ORL",
    "rules": KALSHI_RULES,
    "resolution_sources": [{"name": "NBA", "url": None}],
}
HL: dict[str, Any] = {
    "venue": "hyperliquid",
    "market_id": "#149650",
    "rules": HL_RULES,
    "resolution_sources": [{"name": "National Basketball Association (NBA)", "url": None}],
}


def test_a_definition_note_quotes_each_venue_or_says_it_is_silent() -> None:
    notes = explain(["definition_differs"], KALSHI, HL)
    assert notes == {
        "definition_differs": "Kalshi's rules don't say. Hyperliquid's rules: \"The Counted Play is the full game "
        "including overtime. Play outside the Counted Play, including any extra time, overtime, shoot-out, or "
        "tiebreak, is disregarded, and a winner decided by such play is not the winner of the Counted Play, even if "
        'any source treats it as part of the Contest."'
    }


def test_quotes_keep_the_rules_order_so_a_sentence_is_read_in_context() -> None:
    q = quote(HL_RULES, "definition_differs")
    assert q is not None and q.startswith("The Counted Play is the full game including overtime.")


def test_a_source_note_names_both_sources() -> None:
    assert explain(["source_differs"], KALSHI, HL) == {
        "source_differs": "Kalshi settles on NBA; Hyperliquid on National Basketball Association (NBA)."
    }


def test_no_rules_read_means_no_notes() -> None:
    assert explain(["definition_differs"], {**KALSHI, "rules": None}, HL) == {}
    # A code neither venue's rules speak to gets no note rather than a guess.
    assert (
        explain(["rounding_differs"], KALSHI, {**HL, "rules": "The market resolves to Yes if Orlando wins."})
        == {}
    )


def test_layers_own_notes_win_and_say_so() -> None:
    row = {
        "kalshi": KALSHI,
        "hyperliquid": HL,
        "caveats": ["definition_differs"],
        "caveat_notes": {"definition_differs": "Layer's."},
    }
    assert with_notes(row)["caveat_notes_from"] == "layer"
    built = with_notes({**row, "caveat_notes": {}})
    assert (
        built["caveat_notes_from"] == "venues"
        and "Counted Play" in built["caveat_notes"]["definition_differs"]
    )
    assert with_notes({**row, "caveat_notes": {}, "caveats": []})["caveat_notes_from"] is None
