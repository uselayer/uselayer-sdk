"""Fee lookup proof: client.profit() answers like Layer's POST /v0/profit on live Kalshi ↔ Polymarket US matches.

    KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... LAYER_API_KEY=lyr_... \\
        python scripts/prove_fee_lookup.py [--matches 5] [--record ~/layer-fee-lookup-recording]

Reads only: nothing is traded. For each match, the same body (100 contracts at each leg's best ask, or
0.42 / 0.55 when a side is empty), once with the Kalshi leg as taker and once as maker, goes to Layer's
``POST /v0/profit`` with ``kalshi.market_id`` and to ``client.profit()`` twice: with ``kalshi.market_id``
(its twin from Layer's match) and with ``pair=``. Every field must agree, ``kalshi.fee_type`` and the
order of ``filled_in`` included. Fields that count days from "now" agree to their last rounded place,
since the two answers are computed a moment apart; the SDK's clock is set to the middle of Layer's request.

``--record DIR`` saves every venue answer the SDK read (``venues.json``) and Layer's answers
(``hosted.json``) to DIR, for checking on your own machine. DIR must be outside this repository: the
venues' terms don't allow sharing their data, so recordings are never committed. The tests replay a
made-up set in ``tests/fixtures/fee_lookup`` instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from uselayer import Client, Kalshi, Match
from uselayer._recorded import RecordTransport

# Recordings of live answers never go in here (see --record).
REPO = Path(__file__).resolve().parents[2]

# Each time-dependent field and one unit in its last rounded place.
TIME_FIELDS = {"days_held": 0.01, "return_per_day_pct": 0.0001, "annualized_return_pct": 0.01}


class Clock:
    def __init__(self) -> None:
        self.at: datetime | None = None

    def __call__(self) -> datetime:
        return self.at or datetime.now(UTC)


def differences(hosted: dict[str, Any], sdk: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(differences that fail, differences that are only reported)."""
    bad: list[str] = []
    noted: list[str] = []
    for k in sorted(set(hosted) | set(sdk)):
        h, s = hosted.get(k), sdk.get(k)
        if k in TIME_FIELDS and isinstance(h, (int, float)) and isinstance(s, (int, float)):
            if abs(h - s) > TIME_FIELDS[k] + 1e-9:
                bad.append(f"{k}: Layer {h}, SDK {s}")
        elif k == "match" and isinstance(h, dict) and isinstance(s, dict) and _closed_early(h, s):
            noted.append(
                f"match times: the venue already closed it at {s['latest_payout_at']}; Layer's stored "
                f"times are from before (Layer: expected {h['expected_payout_at']}, latest {h['latest_payout_at']})"
            )
        elif h != s:
            bad.append(f"{k}: Layer {h}, SDK {s}")
    return bad, noted


def _closed_early(h: dict[str, Any], s: dict[str, Any]) -> bool:
    """Only the times differ, and the venue's own close time is already past: the market closed early."""
    ids = lambda d: {k: v for k, v in d.items() if not k.endswith("_payout_at")}  # noqa: E731
    latest = s.get("latest_payout_at")
    return ids(h) == ids(s) and isinstance(latest, str) and latest < datetime.now(UTC).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=5)
    ap.add_argument("--record", type=Path)
    ap.add_argument("--base", default="https://uselayer.sh")
    args = ap.parse_args()

    layer_key = os.environ["LAYER_API_KEY"]
    if args.record:
        args.record = args.record.expanduser().resolve()
        if args.record.is_relative_to(REPO):
            print(
                f"--record {args.record} is inside the repository. Record outside it: the venues' terms "
                "don't allow sharing their data, so live answers are never committed.",
                file=sys.stderr,
            )
            return 2
        args.record.mkdir(parents=True, exist_ok=True)
        (args.record / "venues.json").unlink(missing_ok=True)
    clock = Clock()
    client = Client(
        store=":memory:",
        kalshi=Kalshi.from_env(),
        layer_key=layer_key,
        clock=clock,
        transport=RecordTransport(args.record / "venues.json") if args.record else None,
    )
    layer = httpx.Client(timeout=30, headers={"authorization": f"Bearer {layer_key}"})

    matches: list[Match] = []
    for q in (None, "nfl", "mlb", "nhl", "wnba", "ncaaf", "nba", "epl"):
        page = client.matches(venue="polymarket_us", limit=200, q=q)
        matches += [m for m in page if {"kalshi", "polymarket_us"} <= set(m.markets())]
    # One match per Kalshi series (each has its own fee settings), latest event first, so the check covers
    # different fee multipliers and fee types and payouts days away, not one league's games today.
    matches.sort(key=lambda m: m.event_date or "", reverse=True)
    picked: dict[str, Match] = {}
    for m in matches:
        k = m.markets()["kalshi"]
        picked.setdefault(getattr(k, "series", None) or k.market_id.split("-")[0], m)
    print(f"{len(matches)} Kalshi ↔ Polymarket US matches from Layer in {len(picked)} Kalshi series")

    recorded: list[dict[str, Any]] = []
    failed = tried = 0
    for m in list(picked.values())[: args.matches]:
        kalshi_id, us_id = m.markets()["kalshi"].market_id, m.markets()["polymarket_us"].market_id
        clock.at = None
        p = client.prices(m)
        k_ask, u_ask = p.leg("kalshi").yes_ask, p.leg("polymarket_us").no_ask
        for role in ("taker", "maker"):
            tried += 1
            clock.at = None
            body = {
                "contracts": 100,
                "kalshi": {"market_id": kalshi_id, "price": k_ask or 0.42, "role": role},
                "polymarket_us": {"price": u_ask or 0.55},
            }
            t0 = datetime.now(UTC)
            r = layer.post(f"{args.base}/v0/profit", json=body)
            t1 = datetime.now(UTC)
            if r.status_code != 200:
                print(f"✗ {kalshi_id} ({role}): Layer answered {r.status_code} {r.text[:200]}")
                failed += 1
                break
            hosted = r.json()
            clock.at = t0 + (t1 - t0) / 2
            by_id = client.profit(body)
            no_id = {**body, "kalshi": {"price": body["kalshi"]["price"], "role": role}}
            by_pair = client.profit(no_id, pair=m)
            bad, noted = differences(hosted, by_id)
            bad2, _ = differences(hosted, by_pair)
            bad += [f"(pair=) {b}" for b in bad2]
            ok = "✓" if not bad else "✗"
            failed += bool(bad)
            kal = by_id["kalshi"]
            print(
                f"{ok} {kalshi_id} ↔ {us_id} ({role}): net {by_id['net_profit']} (Layer {hosted['net_profit']}), "
                f"Kalshi fee {kal['fee']} (Layer {hosted['kalshi']['fee']}) at {kal['fee_type']} ×{kal['fee_multiplier']}, "
                f"fees {by_id['fees']} (Layer {hosted['fees']}), days_held {by_id.get('days_held')} "
                f"(Layer {hosted.get('days_held')}), filled {by_id['filled_in']}"
            )
            for line in bad:
                print(f"    differs: {line}")
            for line in noted:
                print(f"    note: {line}")
            recorded.append(
                {
                    "pair": [["kalshi", kalshi_id], ["polymarket_us", us_id]],
                    "body": no_id,
                    "at": clock.at.isoformat(),
                    "response": hosted,
                }
            )
    if args.record:
        (args.record / "hosted.json").write_text(json.dumps(recorded, indent=1) + "\n")
        print(f"saved {len(recorded)} answers to {args.record}")
    print(f"{tried - failed} of {tried} checks agree ({tried // 2} matches, taker and maker)")
    return 1 if failed or tried < 6 else 0


if __name__ == "__main__":
    sys.exit(main())
