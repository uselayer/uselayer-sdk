"""Why each of Layer's rule flags applies, written on your machine from both venues' own rules.

Layer marks how two matched markets' rules differ with codes (``source_differs``, ``definition_differs``,
...). Its plain-English note for each code quotes the venues' rules, which aren't Layer's to pass on, so
its API sends the codes only. Once the SDK has read each market's rules from its venue (see
:mod:`uselayer.titles`), this module writes the note: the sentence of each venue's rules about that
point, quoted as the venue wrote it, or the two settlement sources by name. Nothing is guessed: a
venue whose rules say nothing on the point is said to say nothing, and a code neither venue's rules
speak to gets no note.

    {"definition_differs": 'Kalshi\'s rules don\'t say. Hyperliquid\'s rules: "The Counted Play is the full game including overtime."'}
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .errors import VENUE_NAMES

# The words that mark a sentence as being about each kind of difference.
CODE_WORDS: Mapping[str, str] = {
    "source_differs": r"source|official|according to|as reported|box score|publish|record",
    "timing_differs": r"deadline|no later than|before|by \d|time ?zone|\bET\b|\bUTC\b|expir|fix(?:ed|ing)",
    "rounding_differs": r"round|decimal|nearest|precision|or more|or higher|at least|exceed|equal",
    "carveout_differs": r"unless|except|in the event|ambigu|death|dies|disqualif|forfeit|no contest|walkover",
    "definition_differs": r"overtime|extra time|extra innings|shoot-?out|tiebreak|counted play|regulation|includ",
}

MAX_QUOTE = 400
# A sentence read alone can mislead: Hyperliquid's "Play outside the Counted Play, including ... overtime
# ... is disregarded" comes right after "The Counted Play is the full game including overtime."
QUOTED = 2


def sentences(text: str) -> list[str]:
    """The rules split into sentences (on ". " and line breaks)."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"(])|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def quote(rules: str | None, code: str) -> str | None:
    """The sentences of ``rules`` most about ``code`` (the ``QUOTED`` with the most matching words), in
    the order the rules give them."""
    words = CODE_WORDS.get(code)
    if not rules or not words:
        return None
    hits = [(len(re.findall(words, s, flags=re.I)), i, s) for i, s in enumerate(sentences(rules))]
    top = sorted((h for h in hits if h[0]), key=lambda h: (-h[0], h[1]))[:QUOTED]
    if not top:
        return None
    s = " ".join(h[2] for h in sorted(top, key=lambda h: h[1]))
    return s if len(s) <= MAX_QUOTE else s[: MAX_QUOTE - 1].rstrip() + "…"


def source_names(market: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for s in market.get("resolution_sources") or []:
        if isinstance(s, Mapping):
            n = s.get("name") or s.get("url")
            if n and str(n) not in out:
                out.append(str(n))
    return out


def explain(caveats: list[str], kalshi: Mapping[str, Any], other: Mapping[str, Any]) -> dict[str, str]:
    """One note per code in ``caveats`` that the two markets' rules speak to, keyed by code.

    ``kalshi`` and ``other`` are market dicts with ``venue``, ``rules`` and ``resolution_sources``
    filled in from the venues. A market whose rules weren't read (its venue didn't answer, or the SDK
    doesn't read it yet) gives no notes."""
    if not kalshi.get("rules") or not other.get("rules"):
        return {}
    k_name = VENUE_NAMES.get(str(kalshi.get("venue")), "Kalshi")
    o_name = VENUE_NAMES.get(str(other.get("venue")), str(other.get("venue")))
    out: dict[str, str] = {}
    for code in caveats:
        if code == "source_differs":
            ks, os_ = source_names(kalshi), source_names(other)
            if ks and os_ and ks != os_:
                out[code] = f"{k_name} settles on {', '.join(ks)}; {o_name} on {', '.join(os_)}."
                continue
        kq, oq = quote(kalshi.get("rules"), code), quote(other.get("rules"), code)
        if kq is None and oq is None:
            continue
        said = [
            f'{name}\'s rules: "{q}"' if q else f"{name}'s rules don't say."
            for name, q in ((k_name, kq), (o_name, oq))
        ]
        out[code] = " ".join(said)
    return out
