"""The PIN lockout windows survive a daemon restart: the proxy's call log
(``GET /v1/phone/pin-failures``) is replayed into a fresh store, once, with
only the calls from before this process started, so nothing counts twice."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
from calls import pin_failures
from calls.pin_failures import NUMBER_LIMIT, ROUTE_LIMIT, WINDOW_S, PinFailureStore

_WALL0 = 1_790_000_000.0   # this process started here (wall clock)
_MONO0 = 5_000.0           # ... which is here on the monotonic clock


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, timezone.utc).isoformat()


def _call(ago_s: float, outcome="pin_failed", attempts=3, number="+16085550100",
          route="r1", length_s=20.0) -> dict:
    start = _WALL0 - ago_s
    return {"from_number": number, "route_id": route, "outcome": outcome,
            "pin_attempts": attempts, "started_at": _iso(start),
            "ended_at": _iso(start + length_s)}


@pytest.fixture
def store():
    clock = {"mono": _MONO0, "wall": _WALL0}
    s = PinFailureStore(now=lambda: clock["mono"], wall=lambda: clock["wall"])
    return s, clock


def test_a_refused_callers_cooldown_survives_the_restart(store):
    s, _ = store
    assert s.seed([_call(300), _call(200, outcome="pin_timeout", attempts=2)]) == 5
    assert s.number_locked("+1 608 555 0100") and s.seeded


def test_failures_land_on_this_clock_and_age_out(store):
    s, clock = store
    s.seed([_call(600, attempts=3), _call(60, attempts=2)])
    assert s.number_locked("+16085550100")
    # The older call's failures leave the window first.
    clock["mono"] += WINDOW_S - 600 + 25
    assert not s.number_locked("+16085550100")


def test_a_call_outside_the_window_is_not_replayed(store):
    s, _ = store
    assert s.seed([_call(WINDOW_S + 120, attempts=3)]) == 0
    assert s._numbers == {} and s._routes == {}


def test_a_verified_call_clears_its_caller_but_not_the_route(store):
    s, _ = store
    calls = [_call(500, attempts=3), _call(400, attempts=2),
             _call(300, outcome="completed", attempts=2)]
    s.seed(calls)
    assert not s.number_locked("+16085550100")
    # 3 + 2 + 1 failed attempts stay on the route's breaker.
    assert len(s._routes["r1"]) == 6


def test_an_abandoned_entry_counts_the_attempts_before_it(store):
    s, _ = store
    s.seed([_call(100, outcome="hangup", attempts=1), _call(90, outcome="hangup", attempts=3)])
    assert len(s._numbers["16085550100"]) == 2


def test_calls_after_the_start_are_not_counted_twice(store):
    s, clock = store
    s.record_failure("+16085550100", "r1")         # this process saw it live
    later = _call(-5, attempts=3)                   # its log row arrives too
    assert s.seed([_call(100, attempts=1), later]) == 1
    assert len(s._numbers["16085550100"]) == 2
    assert s._numbers["16085550100"] == sorted(s._numbers["16085550100"])


def test_a_seeded_clear_never_touches_live_failures(store):
    s, _ = store
    s.record_failure("+16085550100", "r1")
    s.seed([_call(100, outcome="completed", attempts=1)])
    assert len(s._numbers["16085550100"]) == 1


def test_a_caller_cleared_before_a_late_seed_stays_clear(store):
    s, _ = store
    s.clear_number("+16085550100")        # verified on this process, seed not in yet
    s.seed([_call(300, attempts=3), _call(200, attempts=2)])
    assert "16085550100" not in s._numbers
    assert len(s._routes["r1"]) == 5      # the route breaker keeps its count


def test_the_route_breaker_is_restored_without_caller_numbers(store):
    s, _ = store
    s.seed([_call(300 - i, number="", attempts=3) for i in range(7)])
    assert s.route_locked("r1") and len(s._routes["r1"]) == 21 >= ROUTE_LIMIT
    assert s._numbers == {}


def test_junk_rows_are_skipped(store):
    s, _ = store
    rows = [{"pin_attempts": "x"}, {"pin_attempts": 2, "started_at": "not a date"},
            {"pin_attempts": 0, "started_at": _iso(_WALL0 - 10)}, _call(30, attempts=1)]
    assert s.seed(rows) == 1


@pytest.fixture
def daemon_store(monkeypatch):
    clock = {"mono": _MONO0, "wall": _WALL0}
    s = PinFailureStore(now=lambda: clock["mono"], wall=lambda: clock["wall"])
    monkeypatch.setattr(pin_failures, "store", s)
    monkeypatch.setattr(main, "_pin_seed_task", None)
    return s


@pytest.mark.asyncio
async def test_the_daemon_seeds_once_and_retries_after_a_miss(daemon_store, monkeypatch):
    import logging
    answers = [None, [_call(100, attempts=NUMBER_LIMIT)]]
    asked: list[int] = []

    async def fake_fetch(window_s):
        asked.append(window_s)
        return answers.pop(0) if answers else [_call(50, attempts=NUMBER_LIMIT)]
    monkeypatch.setattr(main, "fetch_pin_failures", fake_fetch)
    log = logging.getLogger("test")
    await main._start_pin_seed(log)
    assert not daemon_store.seeded
    await main._start_pin_seed(log)
    assert daemon_store.seeded and daemon_store.number_locked("+16085550100")
    assert main._start_pin_seed(log) is main._pin_seed_task   # no third ask
    assert len(asked) == 2 and asked[0] > WINDOW_S
