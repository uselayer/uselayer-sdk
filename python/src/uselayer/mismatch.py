"""Resolution mismatches: a hedged pair the two venues settled differently.

YES on one market plus NO on its twin pays $1 per contract only if both venues settle the bet the
same way, at about the same time. That is a real risk, not a formality: the venues' rules can differ,
one can void a market the other settles, or settle days apart. The SDK checks each hedged pair (the
two legs one ``trade()`` filled, which share its ``group_id``) as its markets settle, and flags it:

- ``both_lost``: both legs paid $0.
- ``both_won``: both legs paid $1 (a gain, but still the venues disagreeing).
- ``void_one_leg``: one venue voided or refunded its market and the other settled it.
- ``settle_gap``: one leg settled and the other was still open :data:`SETTLE_GAP_S` later, so the
  open leg had no hedge. It stays ``pending`` until that leg settles; then ``impact`` is final, and
  it's 0 if the two legs still paid $1.

``impact`` is what the hedged contracts paid minus the $1 each was meant to pay: negative is money
lost. Each mismatch is saved in the mode's store with both venues' results;
:meth:`~uselayer.Client.resolution_mismatches` lists them, and ``pnl().resolution_mismatch_loss``
shows their total, the part of ``realized`` lost this way (already inside ``realized``).

Paper and backtest read each leg's result from the store's settlements. Live mode asks each venue
what its market paid (reads only, with your own keys) and keeps the answer in the store. Nothing
is sent to Layer.

When both venues void their markets, each refunds its leg: that's not flagged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .events import Fill, SimulatedFill, SimulatedSettlement

Kind = Literal["both_lost", "both_won", "void_one_leg", "settle_gap"]
LegKey = tuple[str, str, str]  # (venue, market, side)

# How far apart the two legs can settle before the pair counts as unhedged: Kalshi pays a postponed
# game's fair price after 48 hours, while the twin market can stay open for the rescheduled game.
SETTLE_GAP_S = 48 * 3600.0
_EPS = 1e-9


@dataclass(frozen=True)
class LegOutcome:
    """How one side of one market settled: ``"yes"``, ``"no"`` or ``"void"``, what a contract paid, and when."""

    outcome: str
    payout: float
    at: datetime


@dataclass(frozen=True)
class MismatchLeg:
    """One leg of a mismatched pair and what its venue paid. ``outcome`` is ``None`` while it hasn't settled."""

    venue: str
    market: str
    side: str
    contracts: float
    outcome: str | None = None
    payout: float | None = None  # per contract, in dollars
    settled_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["settled_at"] = self.settled_at.isoformat() if self.settled_at else None
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> MismatchLeg:
        at = d.get("settled_at")
        return cls(
            venue=d["venue"],
            market=d["market"],
            side=d["side"],
            contracts=d["contracts"],
            outcome=d.get("outcome"),
            payout=d.get("payout"),
            settled_at=datetime.fromisoformat(at) if at else None,
        )


@dataclass(frozen=True)
class ResolutionMismatch:
    """A hedged pair the two venues settled differently, and what it cost against the $1 per contract expected.

    ``contracts`` is how many were hedged (held on both legs), ``expected`` what they should pay
    (``contracts × $1``), ``paid`` what both venues paid for them, and ``impact = paid − expected``
    (negative: lost). ``paid`` and ``impact`` are ``None`` while a ``settle_gap`` is ``pending``.
    ``settle_gap_s`` is how far apart the legs settled (or how long one has waited for the other).
    """

    group_id: str
    kind: Kind
    contracts: float
    expected: float
    paid: float | None
    impact: float | None
    pending: bool
    settle_gap_s: float | None
    a: MismatchLeg
    b: MismatchLeg
    detected_at: datetime
    mode: str

    @property
    def legs(self) -> tuple[MismatchLeg, MismatchLeg]:
        return (self.a, self.b)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["a"], d["b"] = self.a.to_dict(), self.b.to_dict()
        d["detected_at"] = self.detected_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ResolutionMismatch:
        return cls(
            group_id=d["group_id"],
            kind=d["kind"],
            contracts=d["contracts"],
            expected=d["expected"],
            paid=d.get("paid"),
            impact=d.get("impact"),
            pending=d["pending"],
            settle_gap_s=d.get("settle_gap_s"),
            a=MismatchLeg.from_dict(d["a"]),
            b=MismatchLeg.from_dict(d["b"]),
            detected_at=datetime.fromisoformat(d["detected_at"]),
            mode=d["mode"],
        )


# ---- finding the pairs and their results ----------------------------------------------------------


def hedged_pairs(
    fills: Iterable[Fill | SimulatedFill],
) -> dict[str, tuple[tuple[LegKey, float], tuple[LegKey, float]]]:
    """Each group of fills that is a pair: two markets, opposite sides, contracts still held on both.

    Contracts are what the group bought minus what it sold (an unwind sells with the same group id).
    """
    groups: dict[str, dict[LegKey, float]] = {}
    for f in fills:
        if not f.group_id:
            continue
        legs = groups.setdefault(f.group_id, {})
        k = (f.venue, f.market, f.side)
        legs[k] = legs.get(k, 0.0) + (f.contracts if f.action == "buy" else -f.contracts)
    out: dict[str, tuple[tuple[LegKey, float], tuple[LegKey, float]]] = {}
    for gid, legs in groups.items():
        held = [(k, round(n, 6)) for k, n in legs.items() if n > _EPS]
        if len(held) != 2:
            continue
        (ka, na), (kb, nb) = held
        if ka[:2] == kb[:2] or {ka[2], kb[2]} != {"yes", "no"}:
            continue
        out[gid] = ((ka, na), (kb, nb))
    return out


def from_settlements(settlements: Iterable[SimulatedSettlement]) -> dict[LegKey, LegOutcome]:
    """Paper and backtest: each settled position's result."""
    return {(s.venue, s.market, s.side): LegOutcome(s.outcome, s.payout, s.at) for s in settlements}


def from_venue_payouts(payouts: Iterable[tuple[str, str, float, datetime]]) -> dict[LegKey, LegOutcome]:
    """Live: what one YES contract of each market paid, as both sides' results."""
    out: dict[LegKey, LegOutcome] = {}
    for venue, market, yes, at in payouts:
        outcome = "yes" if yes >= 1 - _EPS else "no" if yes <= _EPS else "void"
        out[(venue, market, "yes")] = LegOutcome(outcome, round(yes, 6), at)
        out[(venue, market, "no")] = LegOutcome(outcome, round(1 - yes, 6), at)
    return out


# ---- classifying ----------------------------------------------------------------------------------


def _state(r: LegOutcome) -> str:
    if r.outcome == "void":
        return "void"
    return "won" if r.payout >= 1 - _EPS else "lost" if r.payout <= _EPS else "void"


def _kind(ra: LegOutcome, rb: LegOutcome, gap: float, gap_s: float) -> Kind | None:
    sa, sb = _state(ra), _state(rb)
    if (sa == "void") != (sb == "void"):
        return "void_one_leg"
    if sa == sb == "void":
        return None  # both refunded
    if sa == sb == "lost":
        return "both_lost"
    if sa == sb == "won":
        return "both_won"
    return "settle_gap" if gap > gap_s else None


def check(
    pairs: Mapping[str, tuple[tuple[LegKey, float], tuple[LegKey, float]]],
    results: Mapping[LegKey, LegOutcome],
    *,
    now: datetime,
    mode: str,
    previous: Mapping[str, ResolutionMismatch] | None = None,
    settle_gap_s: float = SETTLE_GAP_S,
) -> list[ResolutionMismatch]:
    """Every pair that settled differently on its two venues (see the module notes for the kinds).

    ``previous`` keeps each mismatch's first ``detected_at``.
    """
    previous = previous or {}
    out: list[ResolutionMismatch] = []
    for gid, ((ka, na), (kb, nb)) in pairs.items():
        ra, rb = results.get(ka), results.get(kb)
        if ra is None and rb is None:
            continue
        hedged = round(min(na, nb), 6)
        kind: Kind | None
        if ra is not None and rb is not None:
            gap = abs((ra.at - rb.at).total_seconds())
            kind = _kind(ra, rb, gap, settle_gap_s)
            paid: float | None = round(hedged * (ra.payout + rb.payout), 6)
            impact: float | None = round(hedged * (ra.payout + rb.payout) - hedged, 6)
            pending = False
        else:
            done = ra or rb
            assert done is not None
            gap = (now - done.at).total_seconds()
            kind = "settle_gap" if gap > settle_gap_s else None
            paid = impact = None
            pending = True
        if kind is None:
            continue

        def leg(k: LegKey, n: float, r: LegOutcome | None) -> MismatchLeg:
            if r is None:
                return MismatchLeg(k[0], k[1], k[2], n)
            return MismatchLeg(k[0], k[1], k[2], n, r.outcome, r.payout, r.at)

        before = previous.get(gid)
        out.append(
            ResolutionMismatch(
                group_id=gid,
                kind=kind,
                contracts=hedged,
                expected=hedged,
                paid=paid,
                impact=impact,
                pending=pending,
                settle_gap_s=round(gap, 3),
                a=leg(ka, na, ra),
                b=leg(kb, nb, rb),
                detected_at=before.detected_at if before else now,
                mode=mode,
            )
        )
    return out
