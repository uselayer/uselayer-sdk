"""#11.3: retries, one error shape, client-side pacing — forced 429s, 5xxs and timeouts."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from uselayer import VenueError, __version__
from uselayer.http import HostLimits, Http


class Script:
    def __init__(self, steps: list[Callable[[httpx.Request], httpx.Response]]) -> None:
        self.steps, self.calls = steps, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        return step(request)


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def status(
    code: int, headers: dict[str, str] | None = None, body: object = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(code, json=body or {"message": "x"}, headers=headers or {})


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


def make(script: Script, sleeps: list[float], limits: dict[str, HostLimits] | None = None) -> Http:
    return Http(
        transport=httpx.MockTransport(script), sleep=sleeps.append, rng=lambda: 0.0, limits=limits or {}
    )


def test_requests_send_the_sdk_version_in_the_user_agent() -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return ok(request)

    make(Script([record]), []).request("GET", "https://x.test/r", venue="v")
    assert seen == [f"uselayer-python/{__version__}"]


@pytest.mark.parametrize("fail", [status(429), status(500), status(502), timeout])
def test_reads_retry_429_5xx_and_timeouts_then_succeed(
    fail: Callable[[httpx.Request], httpx.Response],
) -> None:
    sleeps: list[float] = []
    s = Script([fail, fail, ok])
    assert make(s, sleeps).request("GET", "https://x.test/r", venue="v") == {"ok": True}
    assert s.calls == 3 and sleeps == [0.5, 1.0]


def test_reads_give_up_after_three_retries_with_one_error_shape() -> None:
    sleeps: list[float] = []
    s = Script([status(503)])
    with pytest.raises(VenueError) as e:
        make(s, sleeps).request("GET", "https://x.test/r", venue="v")
    assert s.calls == 4 and sleeps == [0.5, 1.0, 2.0]
    assert e.value.code == "venue_unavailable" and e.value.retryable and e.value.status == 503


def test_retry_after_and_the_venues_minimum_wait_are_honoured() -> None:
    sleeps: list[float] = []
    make(Script([status(429, {"retry-after": "3"}), ok]), sleeps).request(
        "GET", "https://x.test/r", venue="v"
    )
    assert sleeps == [3.0]
    sleeps2: list[float] = []
    limits = {"gateway.polymarket.us": HostLimits(1e6, min_retry_wait_s=1.0)}
    make(Script([status(429), ok]), sleeps2, limits).request(
        "GET", "https://gateway.polymarket.us/x", venue="polymarket_us"
    )
    assert sleeps2 == [1.0]  # "wait at least 1 second"


def test_orders_retry_a_429_but_never_resend_after_a_5xx_or_timeout() -> None:
    sleeps: list[float] = []
    s = Script([status(429), ok])
    assert make(s, sleeps).request("POST", "https://x.test/o", venue="v", kind="order", json={}) == {
        "ok": True
    }
    assert s.calls == 2
    for fail in (status(500), timeout):
        s2 = Script([fail, ok])
        with pytest.raises(VenueError) as e:
            make(s2, []).request("POST", "https://x.test/o", venue="v", kind="order", json={})
        assert s2.calls == 1 and e.value.code == "outcome_unknown"
        assert e.value.next == "client.sync(), then check client.orders()"


def test_cancels_retry_like_reads() -> None:
    s = Script([status(500), ok])
    assert make(s, []).request("DELETE", "https://x.test/c", venue="v", kind="cancel") == {"ok": True}
    assert s.calls == 2


def test_errors_carry_code_hint_next_and_raw() -> None:
    cases = {401: "auth_failed", 404: "not_found", 400: "invalid_order"}
    for code, want in cases.items():
        with pytest.raises(VenueError) as e:
            make(Script([status(code, body={"error": "nope"})]), []).request(
                "GET", "https://x.test/r", venue="v"
            )
        d = e.value.to_dict()
        assert (
            d["code"] == want
            and d["hint"]
            and d["next"]
            and d["raw"] == {"error": "nope"}
            and not d["retryable"]
        )
    with pytest.raises(VenueError) as m:
        make(Script([status(503)]), []).request(
            "GET", "https://gateway.polymarket.us/x", venue="polymarket_us"
        )
    assert m.value.code == "venue_maintenance"


def test_pacing_holds_each_host_to_80_percent_of_its_limit() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    http = Http(
        transport=httpx.MockTransport(ok),
        sleep=sleep,
        clock=lambda: now[0],
        rng=lambda: 0.0,
        limits={"a.test": HostLimits(10)},
    )  # 8 a second allowed
    for _ in range(16):
        http.request("GET", "https://a.test/r", venue="v")
    assert now[0] == pytest.approx(1.0, abs=0.01)  # 8 at once, then 8 more over the next second
    for _ in range(3):
        http.request("GET", "https://unlisted.test/r", venue="v")  # no limit known: not paced
