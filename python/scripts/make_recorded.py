"""Writes examples/recorded.json: MADE-UP venue answers the examples replay, so CI needs no network.

Every market, price and size in it is invented. None of it came from a venue. The answers have the
same shape as Polymarket US's public gateway, which is all the replay needs.

    python scripts/make_recorded.py
"""

from __future__ import annotations

import json
from pathlib import Path

GATEWAY = "https://gateway.polymarket.us"
OUT = Path(__file__).resolve().parents[1] / "examples" / "recorded.json"

# slug, question, [(bid, size)...], [(ask, size)...]. The examples pick the first market whose YES
# ask is between 0.10 and 0.90 with at least 5 contracts, so the first one is skipped on purpose.
MARKETS = [
    (
        "example-longshot",
        "Example: a long shot (made-up data)",
        [(0.03, 400), (0.02, 1000)],
        [(0.05, 300), (0.06, 900)],
    ),
    (
        "example-coin-flip",
        "Example: a coin flip (made-up data)",
        [(0.48, 120), (0.47, 300), (0.45, 800), (0.40, 2000)],
        [(0.50, 150), (0.51, 400), (0.53, 900), (0.60, 2500)],
    ),
    (
        "example-favourite",
        "Example: a favourite (made-up data)",
        [(0.71, 60), (0.70, 250), (0.66, 1000)],
        [(0.73, 80), (0.74, 300), (0.78, 1200)],
    ),
]


def _levels(levels: list[tuple[float, int]]) -> list[dict[str, object]]:
    return [{"px": {"value": f"{p:.4f}", "currency": "USD"}, "qty": f"{q:.4f}"} for p, q in levels]


def main() -> None:
    answers: dict[str, list[dict[str, object]]] = {
        "_note": [
            {
                "status": 0,
                "body": "Made-up answers for the examples (scripts/make_recorded.py). Not venue data.",
            }
        ],
        f"GET {GATEWAY}/v1/markets?active=true&closed=false&limit=50&offset=0": [
            {
                "status": 200,
                "body": {
                    "markets": [
                        {
                            "question": q,
                            "slug": slug,
                            "endDate": "2030-01-01T00:00:00Z",
                            "active": True,
                            "closed": False,
                            "orderPriceMinTickSize": 0.01,
                            "status": "MARKET_STATUS_OPEN",
                            "feeCoefficient": 0.05,
                            "minimumTradeQty": 1,
                        }
                        for slug, q, _, _ in MARKETS
                    ]
                },
            }
        ],
    }
    for slug, _, bids, asks in MARKETS:
        answers[f"GET {GATEWAY}/v1/markets/{slug}/book"] = [
            {
                "status": 200,
                "body": {
                    "marketData": {
                        "marketSlug": slug,
                        "bids": _levels(bids),
                        "offers": _levels(asks),
                        "state": "MARKET_STATE_OPEN",
                        "transactTime": "2030-01-01T00:00:00.000000000Z",
                    }
                },
            }
        ]
    OUT.write_text(json.dumps(answers, indent=1) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
