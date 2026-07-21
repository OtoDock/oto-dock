"""Unit tests for the two-gate concurrency manager (core/concurrency.py).

Covers the correctness-critical behaviors of the live-RAM admission design:
  - GATE 1 (reservation budget) bounds a burst deterministically
  - GATE 2 (live-RAM veto) denies under non-session pressure WITHOUT evicting;
    attribution is by ACCOUNTING (reserved vs shortfall), not gate identity —
    session pressure surfacing as a Gate-2 veto (uncapped small host) still
    evicts, and an EMPTY platform last-resort-admits its only session
  - Gate 2 debits the un-grown remainder of freshly admitted sessions (grow-in)
  - tasks keep a HEAVY of headroom and block-wait; a SYNC release wakes them
  - light (Direct-LLM) sessions reserve less → pack denser
  - remote sessions reserve 0 and are untracked; idempotent re-acquire never
    double-counts; release frees the reservation
  - atomic-N meetings reserve only the local subset, all-or-nothing
  - the reconciler excludes tasks + spares freshly-added sids

State + the live-RAM read are pinned in the fixture so the gates are deterministic
regardless of the host the suite runs on.
"""

import asyncio
import time

import pytest
import pytest_asyncio

import config
import core.concurrency as C


@pytest_asyncio.fixture(autouse=True)
async def env(monkeypatch):
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "SESSION_EST_LIGHT_MB", 200)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "get_idle_timeout", lambda: 900)
    C._sessions.clear()
    C._session_est.clear()
    C._session_added_at.clear()
    C._reserved_mb = 0
    C._parked_tasks = 0
    C._live_cache = None
    C._cond = asyncio.Condition()
    C._budget_mb = 5000      # 5 heavy fit in GATE 1
    C._floor_mb = 300
    C._total_mb = 8000
    live = {"mb": 100_000}   # plenty of free RAM ⇒ GATE 2 passes by default
    monkeypatch.setattr(C, "_live_available_mb", lambda: live["mb"])
    yield live
    C._sessions.clear()
    C._session_est.clear()
    C._session_added_at.clear()
    C._reserved_mb = 0
    C._parked_tasks = 0


# --- GATE 1: reservation budget bounds a burst ------------------------------

@pytest.mark.asyncio
async def test_gate1_budget_bound():
    for i in range(5):  # 5 × 1000 = 5000 = budget
        assert await C.acquire(f"s{i}", "chat")
    assert C._reserved_mb == 5000
    adm = await C.acquire("s5", "chat")  # 6th exceeds budget
    assert not adm and adm.reason == "busy"
    assert "Too many active sessions" in adm.user_message
    assert len(C._sessions) == 5


# --- GATE 2: live-RAM veto denies WITHOUT evicting --------------------------

@pytest.mark.asyncio
async def test_gate2_veto_denies_without_eviction(env, monkeypatch):
    # One aged LIGHT session (reserved 200) is tracked — NOT the empty-platform
    # last-resort case — and 200 can't plausibly account for the shortfall, so
    # evicting it would sacrifice a session without closing the gap.
    assert await C.acquire("bg", "chat", execution_path="direct-llm")
    C._session_added_at["bg"] -= 200  # aged out of the grow-in window
    env["mb"] = 500  # 500 − 1000 = −500 < floor(300) ⇒ GATE 2 fails
    called = {"n": 0}

    async def spy(*a, **k):
        called["n"] += 1
        return None
    monkeypatch.setattr(C, "_oldest_evictable_local", spy)

    # Budget has room (GATE 1 fine) and tracked sessions can't account for the
    # missing RAM ⇒ deny WITHOUT evicting. The denial must name HOST MEMORY,
    # not "too many sessions" — the admin page would truthfully contradict a
    # busy-message (the exact confusion this reason exists to prevent).
    adm = await C.acquire("s", "chat")
    assert not adm and adm.reason == "host_memory"
    assert "low on memory" in adm.user_message
    assert "500 MB free" in adm.user_message          # live reading surfaces
    assert "1300 MB" in adm.user_message              # est(1000) + floor(300)
    assert "sessions" not in adm.user_message.split("—")[0]  # no session-blame lead
    assert called["n"] == 0
    assert "bg" in C._sessions  # the innocent session was not sacrificed


@pytest.mark.asyncio
async def test_gate2_veto_last_resort_admits_only_session(env):
    # ZERO tracked sessions + veto: the platform must never hard-lock itself
    # out — the one session is admitted over both gates (operator policy
    # 2026-07-06: swap can carry the final session; a slow session beats a
    # locked-out user).
    env["mb"] = 500
    adm = await C.acquire("only", "chat")
    assert adm
    assert "only" in C._sessions and C._reserved_mb == 1000
    # A SECOND session under the same pressure is NOT covered: "only" is fresh
    # (full grow-in debit) and can't account for the gap → honest host-memory.
    adm2 = await C.acquire("second", "chat")
    assert not adm2 and adm2.reason == "host_memory"
    assert len(C._sessions) == 1


@pytest.mark.asyncio
async def test_gate2_session_pressure_evicts_by_accounting(env, monkeypatch):
    # Uncapped-small-host shape: the budget never fills (GATE 1 fine) but idle
    # sessions hold the RAM — the Gate-2 shortfall (100MB) is well inside
    # reserved (3000MB), so attribution says "sessions" and the loop evicts to
    # admit. Pre-fix this denied host_memory while reclaimable sessions sat
    # idle (the 2026-07-06 audit's headline inversion).
    for i in range(3):
        assert await C.acquire(f"s{i}", "chat")
        C._session_added_at[f"s{i}"] -= 200  # aged: no grow-in debit
    env["mb"] = 1200  # shortfall = 1000 + 300 − 1200 = 100 ≤ reserved 3000

    async def fake_oldest(min_idle, *, prefer_user=None):
        for sid in ("s0", "s1", "s2"):
            if sid in C._sessions:
                return (sid, "cli", False)
        return None

    async def fake_evict(sid, source, is_pw=False):
        async with C._cond:
            C._remove(sid)
        env["mb"] += 1000  # the victim's RSS returns to the host
        async with C._cond:
            C._cond.notify_all()
        return True

    monkeypatch.setattr(C, "_oldest_evictable_local", fake_oldest)
    monkeypatch.setattr(C, "_evict_one", fake_evict)

    adm = await C.acquire("new", "chat", user_sub="u1")
    assert adm
    assert "new" in C._sessions
    assert "s0" not in C._sessions            # exactly one eviction sufficed
    assert "s1" in C._sessions and "s2" in C._sessions


@pytest.mark.asyncio
async def test_growin_debit_blocks_burst_overcommit(env):
    # Two near-simultaneous warmups on a small box: live RAM (1500) fits ONE
    # heavy session. Without the grow-in debit both read 1500 free (the first
    # hasn't grown yet) and both admit → overcommit. The debit makes the
    # second see the first's un-materialized reservation.
    env["mb"] = 1500
    assert await C.acquire("first", "chat")
    adm = await C.acquire("burst", "chat")
    assert not adm
    # The fresh first session fully accounts for the shortfall → honest "busy".
    assert adm.reason == "busy"
    assert len(C._sessions) == 1

    # ...and the debit DECAYS: once the grow-in window has passed, live RAM is
    # the truth again (its reading includes the first session's real RSS).
    C._session_added_at["first"] -= 200
    assert await C.acquire("later", "chat")
    assert len(C._sessions) == 2


# --- idempotency / remote / release -----------------------------------------

@pytest.mark.asyncio
async def test_idempotent_no_double_reserve():
    assert await C.acquire("x", "chat")
    assert await C.acquire("x", "chat")  # no-op success
    assert len(C._sessions) == 1
    assert C._reserved_mb == 1000


@pytest.mark.asyncio
async def test_remote_untracked_reserves_zero():
    assert await C.acquire("r", "chat", target="machine-x")
    assert "r" not in C._sessions
    assert C._reserved_mb == 0
    C.release("r")  # no-op for a never-tracked id
    assert C._reserved_mb == 0


@pytest.mark.asyncio
async def test_release_frees_reservation():
    await C.acquire("x", "chat")
    assert C._reserved_mb == 1000
    C.release("x")
    assert "x" not in C._session_est and C._reserved_mb == 0
    C.release("x")  # idempotent
    assert C._reserved_mb == 0


@pytest.mark.asyncio
async def test_light_sessions_pack_denser():
    # Direct-LLM reserves LIGHT (200) — 10 of them fit where ~5 heavy would.
    for i in range(10):
        assert await C.acquire(f"L{i}", "chat", execution_path="direct-llm")
    assert C._reserved_mb == 2000  # 10 × 200
    assert len(C._sessions) == 10


# --- tasks: HEAVY headroom + blocking + sync-release wakeup ------------------

@pytest.mark.asyncio
async def test_task_headroom_blocks_then_sync_release_wakes():
    # Tasks need reserved + est ≤ budget − HEAVY = 5000 − 1000 = 4000.
    for i in range(4):
        assert await C.acquire(f"c{i}", "chat")     # reserved = 4000
    # Interactive still admits (gate1: 4000 + 1000 ≤ 5000) ...
    assert await C.acquire("c4", "chat")            # reserved = 5000
    C.release("c4")                                  # reserved = 4000
    # ... but a task blocks (needs ≤ 4000, 4000 + 1000 > 4000).
    waiter = asyncio.create_task(C.acquire("t", "task", blocking=True))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    C.release("c0")                                  # reserved = 3000 → task fits
    assert await asyncio.wait_for(waiter, timeout=1.0)
    assert "t" in C._sessions and C._parked_tasks == 0


# --- atomic-N meetings, target-aware ----------------------------------------

@pytest.mark.asyncio
async def test_meeting_atomic_local_subset():
    ok = await C.acquire_meeting_slots(
        ["s1", "s2", "s3", "s4"],
        targets={"s1": "local", "s2": "machine-x", "s3": "local", "s4": "local"},
    )
    assert ok                                # 3 local heavy = 3000 ≤ 5000
    assert len(C._sessions) == 3 and "s2" not in C._sessions
    assert C._reserved_mb == 3000
    C.release_meeting_slots(["s1", "s2", "s3", "s4"])  # s2 release is a no-op
    assert len(C._sessions) == 0 and C._reserved_mb == 0


@pytest.mark.asyncio
async def test_meeting_all_or_nothing_over_budget():
    for i in range(4):
        await C.acquire(f"c{i}", "chat")     # reserved = 4000
    adm = await C.acquire_meeting_slots(["m1", "m2"], targets={"m1": "local", "m2": "local"})
    assert not adm and adm.reason == "busy"  # 4000 + 2000 > 5000 budget
    assert len(C._sessions) == 4             # nothing partially acquired


# --- eviction fires only on GATE 1 (attributable) pressure ------------------

@pytest.mark.asyncio
async def test_eviction_on_gate1_full(env, monkeypatch):
    for i in range(5):
        await C.acquire(f"s{i}", "chat")     # budget full (reserved 5000)

    async def fake_oldest(min_idle, *, prefer_user=None):
        return ("s0", "cli", False) if "s0" in C._sessions else None

    async def fake_evict(sid, source, is_pw=False):
        async with C._cond:
            C._remove(sid)
        async with C._cond:
            C._cond.notify_all()
        return True

    monkeypatch.setattr(C, "_oldest_evictable_local", fake_oldest)
    monkeypatch.setattr(C, "_evict_one", fake_evict)

    # New interactive admit: GATE 1 is the binding failure ⇒ evict s0 ⇒ admit.
    assert await C.acquire("new", "chat", user_sub="u1")
    assert "s0" not in C._sessions and "new" in C._sessions
    assert len(C._sessions) == 5


@pytest.mark.asyncio
async def test_eviction_denies_when_no_candidate(monkeypatch):
    for i in range(5):
        await C.acquire(f"s{i}", "chat")
    monkeypatch.setattr(C, "_oldest_evictable_local",
                        lambda *a, **k: _async_none())
    adm = await C.acquire("new", "chat")  # nothing idle → deny
    assert not adm and adm.reason == "busy"


async def _async_none():
    return None


# --- Admission semantics ------------------------------------------------------

@pytest.mark.asyncio
async def test_admission_truthiness_and_fields():
    adm = await C.acquire("ok", "chat")
    assert adm and adm.ok and adm.reason is None and adm.user_message is None
    remote = await C.acquire("r", "chat", target="machine-x")
    assert remote and remote.reason is None
    # A denied Admission must be FALSY despite being a non-empty NamedTuple —
    # every call site gates on `if not await acquire...`.
    assert not C.Admission(False, "busy", "x")
    assert bool(C.Admission(True)) is True


@pytest.mark.asyncio
async def test_eviction_path_ram_veto_reports_host_memory(env, monkeypatch):
    # Budget full AND RAM low: eviction frees the budget, but GATE 2 still
    # vetoes → the denial must blame host memory (evicting more user sessions
    # would not reclaim non-session RAM).
    for i in range(5):
        await C.acquire(f"s{i}", "chat")
    env["mb"] = 500

    async def fake_oldest(min_idle, *, prefer_user=None):
        return ("s0", "cli", False) if "s0" in C._sessions else None

    async def fake_evict(sid, source, is_pw=False):
        async with C._cond:
            C._remove(sid)
        return True

    monkeypatch.setattr(C, "_oldest_evictable_local", fake_oldest)
    monkeypatch.setattr(C, "_evict_one", fake_evict)
    adm = await C.acquire("new", "chat")
    assert not adm and adm.reason == "host_memory"


@pytest.mark.asyncio
async def test_meeting_ram_veto_reports_host_memory(env):
    env["mb"] = 500
    adm = await C.acquire_meeting_slots(["m1"], targets={"m1": "local"})
    assert not adm and adm.reason == "host_memory"
    assert "low on memory" in adm.user_message
    assert len(C._sessions) == 0


# --- stats -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_shape_and_breakdown():
    await C.acquire("c1", "chat")
    await C.acquire("p1", "phone")
    await C.acquire("k1", "task", blocking=True)
    st = C.get_stats()
    assert set(st) == {"sessions", "tasks", "by_surface", "satellites"}
    assert set(st["sessions"]) == {
        "active", "reserved_mb", "budget_mb", "available_mb", "total_mb",
        "fit_heavy", "fit_light",
    }
    assert st["sessions"]["active"] == 3
    assert st["sessions"]["reserved_mb"] == 3000
    assert st["sessions"]["budget_mb"] == 5000
    assert st["tasks"]["active"] == 1
    assert st["by_surface"] == {"chat": 1, "task": 1, "meeting": 0, "phone": 1}
    assert st["satellites"] == []


# --- reconciler: excludes tasks + spares freshly-added ----------------------

@pytest.mark.asyncio
async def test_reconcile_excludes_tasks_and_fresh():
    await C.acquire("stale_chat", "chat")
    C._session_added_at["stale_chat"] = time.monotonic() - 99999
    await C.acquire("a_task", "task", blocking=True)
    C._session_added_at["a_task"] = time.monotonic() - 99999
    await C.acquire("fresh_chat", "chat")  # added_at = now

    released = await C.reconcile_chat_slots()
    assert released == 1
    assert "stale_chat" not in C._sessions and C._reserved_mb == 2000
    assert "a_task" in C._sessions       # task excluded
    assert "fresh_chat" in C._sessions   # fresh spared


class TestSwapCredit:
    """Gate-2 swap credit — min(SwapFree/2, cap) added to the live reading.

    Tests the pure helper (the suite's autouse fixture replaces
    _live_available_mb itself); the truthy-mb guard in _live_available_mb
    keeps a fail-closed 0 read uncredited.
    """

    def _credit(self, monkeypatch, swap_mb, cap):
        from core import concurrency
        from core.sandbox import host_resources
        monkeypatch.setattr(config, "SESSION_SWAP_CREDIT_MB", cap)
        monkeypatch.setattr(host_resources, "swap_free_bytes",
                            lambda: swap_mb * 1024 * 1024)
        return concurrency._swap_credit_mb()

    def test_credit_capped(self, monkeypatch):
        # 4GB free swap → half is 2048, capped at 512.
        assert self._credit(monkeypatch, 4096, 512) == 512

    def test_credit_half_of_small_swap(self, monkeypatch):
        # 512MB free swap → credit 256 (half), under the cap.
        assert self._credit(monkeypatch, 512, 512) == 256

    def test_no_swap_no_credit(self, monkeypatch):
        assert self._credit(monkeypatch, 0, 512) == 0

    def test_disabled_by_config(self, monkeypatch):
        assert self._credit(monkeypatch, 4096, 0) == 0
