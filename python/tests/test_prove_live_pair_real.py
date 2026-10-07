"""The real-Polymarket-US proof script's decisions, checked without any venue (scripts/prove_live_pair_real.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "prove_live_pair_real.py"
    spec = importlib.util.spec_from_file_location("prove_live_pair_real", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses and typing look the module up by name
    spec.loader.exec_module(mod)
    return mod


S = _load()


def lv(price: float | None, size: float = 10) -> Any:
    return None if price is None else SimpleNamespace(price=price, size=size)


def book(yes: tuple[float | None, float | None], no: tuple[float | None, float | None]) -> Any:
    """``yes``/``no`` are (best bid, best ask) for each side."""
    sides = {"yes": yes, "no": no}
    return SimpleNamespace(
        outcome=lambda s: SimpleNamespace(best_bid=lv(sides[s][0]), best_ask=lv(sides[s][1]))
    )


def fill(venue: str, action: str, price: float, fee: float, *, side: str = "no", n: float = 1) -> Any:
    return SimpleNamespace(
        venue=venue, market="m", side=side, action=action, price=price, contracts=n, fee=fee
    )


def test_cheap_side_takes_the_cheaper_side_under_15c_with_a_tight_spread() -> None:
    assert S.cheap_side(book(yes=(0.90, 0.92), no=(0.08, 0.10))) == "no"
    assert S.cheap_side(book(yes=(0.05, 0.06), no=(0.94, 0.95))) == "yes"


@pytest.mark.parametrize(
    "b",
    [
        book(yes=(0.80, 0.82), no=(0.18, 0.20)),  # cheapest ask 20¢: above 15¢
        book(yes=(0.85, 0.95), no=(0.05, 0.15)),  # a 10¢ spread: too dear to sell back
        book(yes=(None, 0.95), no=(0.05, None)),  # one-sided
        book(yes=(0.995, 0.999), no=(0.001, 0.005)),  # under a cent
    ],
)
def test_cheap_side_refuses_dear_wide_one_sided_or_sub_cent_books(b: Any) -> None:
    assert S.cheap_side(b) is None


def test_worst_case_is_both_real_buys_settling_at_zero_and_fits_the_cap() -> None:
    assert S.worst_case(0.15, 0.01) == 0.32
    assert S.worst_case(S.MAX_PM_PRICE, 0.02) <= S.PM_CAP


def test_real_money_counts_only_polymarket_us() -> None:
    fs = [
        fill("polymarket_us", "buy", 0.10, 0.01),
        fill("polymarket_us", "sell", 0.09, 0.01),
        fill("kalshi", "buy", 0.20, 0.02, side="yes"),
    ]
    assert S.pm_bought(fs) == 0.11
    assert S.pm_net(fs) == 0.03  # 0.10 + 0.01 − 0.09 + 0.01


def test_held_nets_this_runs_buys_and_sells() -> None:
    fs = [fill("polymarket_us", "buy", 0.1, 0, n=2), fill("polymarket_us", "sell", 0.1, 0, n=1)]
    assert S.held(fs, "polymarket_us", "m", "no") == 1
    assert S.held(fs, "polymarket_us", "m", "yes") == 0
    assert S.held(fs, "kalshi", "m", "no") == 0


def test_only_open_orders_from_this_runs_store_are_canceled() -> None:
    # The run's store is new, so everything in it is this run's; filled and canceled ones are left alone.
    resting = SimpleNamespace(client_id="c1", status="open")
    pending = SimpleNamespace(client_id="c9", status="pending")
    done = SimpleNamespace(client_id="c2", status="filled")
    gone = SimpleNamespace(client_id="c3", status="canceled")
    assert S.own_open_orders([resting, pending, done, gone]) == [resting, pending]


def test_first_leg_hook_fires_once_after_a_filled_buy_and_undoes() -> None:
    seen: list[Any] = []

    class C:
        def _execute(self, order: Any, checked: bool = False) -> Any:
            return SimpleNamespace(filled=order.n)

    c = C()
    orig = c._execute
    undo = S.first_leg_hook(c, seen.append)
    c._execute(SimpleNamespace(action="buy", n=0))  # nothing filled: no call
    c._execute(SimpleNamespace(action="buy", n=1))
    c._execute(SimpleNamespace(action="buy", n=1))  # only once
    assert len(seen) == 1
    undo()
    assert c._execute == orig


def test_deeper_first_prefers_a_kalshi_ask_deeper_than_polymarket_us_else_the_deepest() -> None:
    assert S.deeper_first([("K1", 10), ("K2", 500), ("K3", 900)], 100) == ("K2", True)
    assert S.deeper_first([("K1", 10), ("K2", 40)], 100) == ("K2", False)
    assert S.deeper_first([], 100) is None


def test_kalshi_list_prices_screen_before_a_book_is_read() -> None:
    m = {"yes_ask_dollars": "0.05", "yes_bid_dollars": "0.04"}
    assert S._rough_ok(m, "yes") and not S._rough_ok(m, "no")  # NO ask is 96¢, above 70¢


def test_caps_and_guardrails_match_the_brief() -> None:
    assert (S.PM_CAP, S.MAX_PM_PRICE, S.SIZE) == (1.0, 0.15, 1)
    assert S.RULES == {"budget": 5, "max_daily_loss": {"amount": 3}, "max_position": {"per_market": 3}}


def test_production_kalshi_keys_are_refused(monkeypatch: Any, tmp_path: Any) -> None:
    import uselayer

    monkeypatch.setattr(S, "OUT", tmp_path)
    monkeypatch.setattr(
        uselayer.Kalshi, "from_env", staticmethod(lambda: SimpleNamespace(environment="production"))
    )
    assert S.main([]) == 2


def _fake_world(
    monkeypatch: Any, tmp_path: Any, *, pm_price: float = 0.10, trade_raises: BaseException | None = None
) -> list[Any]:
    """Every Client is a fake; returns the list of trade() calls."""
    import uselayer

    sent: list[Any] = []
    resting: list[Any] = []
    canceled: list[str] = []
    leg = lambda v, p: SimpleNamespace(  # noqa: E731
        venue=v, market="m", side="no", best_price=p, limit_price=p, cost=p, fee=0.01
    )
    q = SimpleNamespace(
        contracts=1, a=leg("kalshi", 0.20), b=leg("polymarket_us", pm_price), limited_by="", to_dict=dict
    )

    class FakeClient:
        killed = False

        def __init__(self, **kw: Any) -> None:
            pm = SimpleNamespace(positions=lambda: [], open_orders=lambda: [])
            self._live = {"polymarket_us": pm}

        def book(self, *a: Any, **k: Any) -> Any:
            return book(yes=(0.89, 0.90), no=(0.09, 0.10))

        def quote(self, *a: Any, **k: Any) -> Any:
            return q

        def trade(self, *a: Any, **k: Any) -> Any:
            sent.append(a)
            if trade_raises is not None:
                # As if the first leg had gone out and was resting when the run stopped.
                resting.append(
                    SimpleNamespace(client_id="leg-1", status="open", to_dict=lambda: {"id": "leg-1"})
                )
                raise trade_raises

        def orders(self, *, open: bool = True) -> list[Any]:
            return [o for o in resting if o.status == "open"]

        def cancel(self, o: Any) -> Any:
            o.status = "canceled"
            canceled.append(o.client_id)
            return o

        def fills(self, *, since: Any = None) -> list[Any]:
            return []

        def reconcile(self, *, since: Any = None) -> Any:
            return SimpleNamespace(ok=True, mismatches=[], to_dict=lambda: {"ok": True})

        def close(self) -> None:
            pass

    monkeypatch.setattr(S, "OUT", tmp_path)
    monkeypatch.setattr(uselayer, "Client", FakeClient)
    monkeypatch.setattr(S, "_pick_pm", lambda *a: ("pm-slug", "no"))
    monkeypatch.setattr(S, "_pick_kalshi", lambda *a: ("KXTEST", True))
    monkeypatch.setattr(
        uselayer.Kalshi, "from_env", staticmethod(lambda: SimpleNamespace(environment="demo"))
    )
    monkeypatch.setattr(uselayer.PolymarketUS, "from_env", staticmethod(lambda: object()))
    sent.append(canceled)  # sent[0]: the canceled order ids
    return sent


def test_nothing_is_sent_without_typing_yes(monkeypatch: Any, tmp_path: Any) -> None:
    sent = _fake_world(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert S.main([]) == 1 and sent[1:] == []


def test_a_dear_polymarket_us_leg_is_refused_before_the_prompt(monkeypatch: Any, tmp_path: Any) -> None:
    sent = _fake_world(monkeypatch, tmp_path, pm_price=0.40)
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda p: asked.append(p) or "yes")
    assert S.main([]) == 1 and sent[1:] == [] and asked == []


def test_dry_run_never_builds_a_live_client_or_asks(monkeypatch: Any, tmp_path: Any) -> None:
    sent = _fake_world(monkeypatch, tmp_path)
    import uselayer

    monkeypatch.setattr(
        uselayer.PolymarketUS, "from_env", staticmethod(lambda: pytest.fail("no PM key in a dry run"))
    )
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("no prompt in a dry run"))
    assert S.main(["--dry-run"]) == 0 and sent[1:] == []


@pytest.mark.parametrize(
    ("stop", "code"), [(KeyboardInterrupt(), 130), (RuntimeError("boom"), 1)], ids=["ctrl-c", "crash"]
)
def test_a_stopped_run_still_cancels_its_orders_and_saves_the_result(
    monkeypatch: Any, tmp_path: Any, stop: BaseException, code: int
) -> None:
    sent = _fake_world(monkeypatch, tmp_path, trade_raises=stop)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert S.main([]) == code
    assert sent[0] == ["leg-1"]  # the order left resting was canceled
    [result] = list(tmp_path.glob("*/result.json"))
    saved = __import__("json").loads(result.read_text())
    assert saved["canceled"] == [{"id": "leg-1"}]
    assert saved["errors"][-1]["step"] == "stopped"
