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
import json
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
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "get_idle_timeout", lambda: 900)
    C._sessions.clear()
    C._session_est.clear()
    C._session_added_at.clear()
    C._session_owner.clear()
    C._line.clear()
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
    C._session_owner.clear()
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
        return C._Scan(None, 0, 0)
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

    async def fake_oldest(min_idle, **_kw):
        for sid in ("s0", "s1", "s2"):
            if sid in C._sessions:
                return C._Scan((sid, "cli", False), 0, 0)
        return C._Scan(None, 0, 0)

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


@pytest.mark.asyncio
async def test_direct_estimate_counts_stdio_mcps(tmp_path):
    # A Direct session starts every stdio server of its config in the proxy:
    # 5 typed stdio + 3 typeless (stdio by default) + 2 http = 8 → 100 + 8 × 65.
    import json
    servers = {f"s{i}": {"type": "stdio", "command": "x"} for i in range(5)}
    servers.update({f"t{i}": {"command": "x"} for i in range(3)})
    servers.update({f"h{i}": {"type": "http", "url": "http://x"} for i in range(2)})
    cfg = tmp_path / "mcp-config.json"
    cfg.write_text(json.dumps({"mcpServers": servers}))
    assert await C.acquire("d", "chat", execution_path="direct-llm", mcp_config_path=str(cfg))
    assert C._session_est["d"] == 620
    # Never above a CLI session carrying the same servers.
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"mcpServers": {f"s{i}": {"command": "x"} for i in range(14)}}))
    assert await C.acquire("d2", "chat", execution_path="direct-llm", mcp_config_path=str(big))
    assert C._session_est["d2"] == 1000
    # Unreadable or not JSON: the flat LIGHT floor.
    bad = tmp_path / "config.toml"
    bad.write_text("[mcp_servers.x]\ncommand = 'x'\n")
    assert await C.acquire("d3", "chat", execution_path="direct-llm", mcp_config_path=str(bad))
    assert await C.acquire("d4", "chat", execution_path="direct-llm",
                           mcp_config_path=str(tmp_path / "missing.json"))
    assert C._session_est["d3"] == C._session_est["d4"] == 200
    # A subprocess engine is HEAVY whatever its config holds.
    assert await C.acquire("c", "chat", execution_path="claude-code-cli", mcp_config_path=str(cfg))
    assert C._session_est["c"] == 1000
    # Meetings count the same way.
    assert await C.acquire_meeting_slots(["m"], targets={"m": "local"},
                                         exec_paths={"m": "direct-llm"}, mcp_paths={"m": str(cfg)})
    assert C._session_est["m"] == 620


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


@pytest.mark.asyncio
async def test_task_keeps_one_count_slot_under_a_cap(monkeypatch):
    # Under a hard cap a task never takes the last slot a person needs.
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 2)
    assert await C.acquire("c1", "chat")
    waiter = asyncio.create_task(C.acquire("t", "task", blocking=True))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    assert await C.acquire("c2", "chat")            # the person still gets the slot
    C.release("c1")
    await asyncio.sleep(0.05)
    assert not waiter.done()                        # one chat left: still parked
    C.release("c2")
    assert await asyncio.wait_for(waiter, timeout=1.0)
    assert C._sessions == {"t": "task"}


@pytest.mark.asyncio
async def test_count_headroom_off_at_cap_one(monkeypatch):
    # A cap of 1 keeps no slot back (a task would never run).
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 1)
    assert await asyncio.wait_for(C.acquire("t", "task", blocking=True), timeout=1.0)


# --- background wakes: reserve before the spawn -----------------------------------

def _never() -> bool:
    return False


@pytest.mark.asyncio
async def test_reserve_background_parks_with_task_headroom():
    for i in range(4):
        assert await C.acquire(f"c{i}", "chat")          # 4000 of 5000
    wake = asyncio.create_task(C.reserve_background(
        "wake", execution_path="claude-code-cli", timeout_s=5, superseded=_never))
    await asyncio.sleep(0.05)
    assert not wake.done() and C._parked_tasks == 1       # never the last HEAVY
    assert await C.acquire("person", "chat")              # a person still gets it
    C.release("c0")
    C.release("c1")
    assert await asyncio.wait_for(wake, 2.0) == "reserved"
    assert C._sessions["wake"] == "chat" and "wake" not in C._session_owner
    assert C._parked_tasks == 0


@pytest.mark.asyncio
async def test_reserve_background_supersedes_when_the_sid_is_taken(monkeypatch):
    assert await C.acquire("sid", "chat")
    assert await C.reserve_background("sid", timeout_s=5, superseded=_never) == "superseded"
    for i in range(4):
        assert await C.acquire(f"c{i}", "chat")
    monkeypatch.setattr(C, "_WAKE_SLICE_S", 0.05)
    wake = asyncio.create_task(C.reserve_background("sid2", timeout_s=5, superseded=_never))
    await asyncio.sleep(0.05)
    async with C._cond:                                   # a person's warmup takes the sid
        C._add("sid2", "chat", 1000, "u1")
        C._cond.notify_all()
    assert await asyncio.wait_for(wake, 2.0) == "superseded"
    assert C._session_owner["sid2"] == "u1"              # theirs, untouched


@pytest.mark.asyncio
async def test_reserve_background_supersedes_on_the_predicate(monkeypatch):
    monkeypatch.setattr(C, "_WAKE_SLICE_S", 0.05)
    for i in range(5):
        assert await C.acquire(f"c{i}", "chat")
    opened = {"now": False}
    wake = asyncio.create_task(C.reserve_background(
        "w", timeout_s=5, superseded=lambda: opened["now"]))
    await asyncio.sleep(0.1)
    assert not wake.done()
    opened["now"] = True                                  # nobody notifies: the slice sees it
    assert await asyncio.wait_for(wake, 1.0) == "superseded"
    assert "w" not in C._sessions and C._parked_tasks == 0
    assert await C.reserve_background("w2", timeout_s=5, superseded=lambda: True) == "superseded"


@pytest.mark.asyncio
async def test_reserve_background_times_out():
    for i in range(5):
        assert await C.acquire(f"c{i}", "chat")
    assert await C.reserve_background("w", timeout_s=0.2, superseded=_never) == "timeout"
    assert "w" not in C._sessions and C._parked_tasks == 0


@pytest.mark.asyncio
async def test_reserve_background_remote_untracked():
    assert await C.reserve_background("r", target="machine-x", timeout_s=1,
                                      superseded=_never) == "untracked"
    assert "r" not in C._sessions


@pytest.mark.asyncio
async def test_release_unless_live_keeps_a_live_session(monkeypatch):
    monkeypatch.setattr(C, "_live_local_sids", lambda: {"live"})
    assert await C.reserve_background("live", timeout_s=1, superseded=_never) == "reserved"
    assert await C.reserve_background("dead", timeout_s=1, superseded=_never) == "reserved"
    assert C.release_unless_live("live") is False and "live" in C._sessions
    assert C.release_unless_live("dead") is True and "dead" not in C._sessions
    assert C.release_unless_live("dead") is False


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

    async def fake_oldest(min_idle, **_kw):
        return C._Scan(("s0", "cli", False) if "s0" in C._sessions else None, 0, 0)

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
    return C._Scan(None, 0, 0)


@pytest.mark.asyncio
async def test_idle_timeout_read_once_off_the_loop(monkeypatch):
    # Every denied admit needs the eviction floor; the setting is read on the
    # DB executor and cached, never once per denial on the event loop.
    import threading
    reads: list[bool] = []

    def _read():
        reads.append(threading.current_thread() is threading.main_thread())
        return 900
    monkeypatch.setattr(config, "get_idle_timeout", _read)
    for i in range(5):
        await C.acquire(f"s{i}", "chat")
    monkeypatch.setattr(C, "_oldest_evictable_local", lambda *a, **k: _async_none())
    for i in range(20):
        assert not await C.acquire(f"denied{i}", "chat")
    assert reads == [False]


# --- the owner map and the per-person cap -------------------------------------

@pytest.mark.asyncio
async def test_owner_filled_on_idempotent_reacquire_never_overwritten():
    assert await C.acquire("x", "chat")
    assert "x" not in C._session_owner
    assert await C.acquire("x", "chat", user_sub="u1")   # the first caller that knows
    assert await C.acquire("x", "chat", user_sub="u2")   # never overwritten
    assert C._session_owner["x"] == "u1"
    C.release("x")
    assert "x" not in C._session_owner


@pytest.mark.asyncio
async def test_per_user_cap_refuses_then_admits_after_release(monkeypatch):
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 2)
    scans: list[dict] = []

    async def spy(min_idle, **kw):
        scans.append(kw)
        return C._Scan(None, 0, 0)
    monkeypatch.setattr(C, "_oldest_evictable_local", spy)
    assert await C.acquire("a", "chat", user_sub="u1")
    assert await C.acquire("b", "chat", user_sub="u1")
    adm = await C.acquire("c", "chat", user_sub="u1")
    assert not adm and adm.reason == "user_cap"
    assert adm.user_message.startswith("You already have 2 sessions running on this platform.")
    # Only this person's own sessions were considered; nobody else's.
    assert scans and all(k.get("only_user") == "u1" for k in scans)
    assert "c" not in C._sessions
    # Someone else is not held by u1's cap.
    assert await C.acquire("d", "chat", user_sub="u2")
    C.release("a")
    assert await C.acquire("c", "chat", user_sub="u1")


@pytest.mark.asyncio
async def test_two_admits_of_one_person_at_the_cap_admit_one(monkeypatch):
    # Both start one below the cap and go through the eviction path on a full
    # budget; the owner count is re-checked in the lock hold of the add, so
    # the second tab cannot slip past the cap.
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 2)
    assert await C.acquire("mine", "chat", user_sub="u1")
    for i in range(4):
        assert await C.acquire(f"other{i}", "chat", user_sub=f"someone{i}")
    others = [f"other{i}" for i in range(4)]

    async def fake_oldest(min_idle, **kw):
        await asyncio.sleep(0)
        for sid in others:
            if sid in C._sessions:
                others.remove(sid)
                return C._Scan((sid, "cli", False), 0, 0)
        return C._Scan(None, 0, 0)

    async def fake_evict(sid, source, is_pw=False):
        async with C._cond:
            C._remove(sid)
        return True
    monkeypatch.setattr(C, "_oldest_evictable_local", fake_oldest)
    monkeypatch.setattr(C, "_evict_one", fake_evict)
    first, second = await asyncio.gather(
        C.acquire("tab1", "chat", user_sub="u1"), C.acquire("tab2", "chat", user_sub="u1"))
    assert sorted([bool(first), bool(second)]) == [False, True]
    assert (first.reason or second.reason) == "user_cap"
    assert C._owned("u1") == 2


@pytest.mark.asyncio
async def test_per_user_cap_skips_reattach_tasks_meetings_and_phone(monkeypatch):
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 1)
    assert await C.acquire("a", "chat", user_sub="u1")
    assert await C.acquire("a", "chat", user_sub="u1")               # its own session
    assert await C.acquire("r", "chat", user_sub="u1", per_user_cap=False)
    assert await C.acquire("p", "phone", user_sub="u1")
    assert await C.acquire("t", "task", blocking=True, user_sub="u1")
    assert await C.acquire_chat_slot("n", user_sub=None)             # no owner known
    assert not await C.acquire("b", "chat", user_sub="u1")


@pytest.mark.asyncio
async def test_per_user_cap_off_when_zero(monkeypatch):
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    for i in range(5):
        assert await C.acquire(f"s{i}", "chat", user_sub="u1")


# --- the admission queue ---------------------------------------------------------

def _no_victims(monkeypatch):
    monkeypatch.setattr(C, "_oldest_evictable_local", lambda *a, **k: _async_none())


@pytest.mark.asyncio
async def test_queued_admit_is_served_in_arrival_order(monkeypatch):
    for i in range(5):
        assert await C.acquire(f"s{i}", "chat")
    _no_victims(monkeypatch)
    dave = asyncio.create_task(C.acquire("dave", "chat", queue_wait_s=5))
    await asyncio.sleep(0.02)
    frank = asyncio.create_task(C.acquire("frank", "chat", queue_wait_s=5))
    await asyncio.sleep(0.02)
    assert not dave.done() and not frank.done() and len(C._line) == 2
    assert not C.prewarm_allowed()                  # people are waiting
    C.release("s0")
    assert await asyncio.wait_for(dave, 1.0)        # first in line, first served
    await asyncio.sleep(0.05)
    assert not frank.done()
    walk_in = await C.acquire("walk_in", "chat")    # a non-queued admit never waits
    assert not walk_in and walk_in.reason == "busy"
    C.release("s1")
    assert await asyncio.wait_for(frank, 1.0)
    assert C._line == []


@pytest.mark.asyncio
async def test_queued_head_retries_on_its_slice(monkeypatch):
    # No release comes, but an idle session becomes reclaimable meanwhile.
    monkeypatch.setattr(C, "_QUEUE_SLICE_S", 0.05)
    for i in range(5):
        assert await C.acquire(f"s{i}", "chat")
    calls = {"n": 0}

    async def ages_in(*a, **k):
        calls["n"] += 1
        return C._Scan(("s0", "cli", False) if calls["n"] >= 3 and "s0" in C._sessions
                       else None, 0, 0)

    async def fake_evict(sid, source, is_pw=False):
        async with C._cond:
            C._remove(sid)
        return True
    monkeypatch.setattr(C, "_oldest_evictable_local", ages_in)
    monkeypatch.setattr(C, "_evict_one", fake_evict)
    assert await asyncio.wait_for(C.acquire("q", "chat", queue_wait_s=5), 2.0)
    assert "s0" not in C._sessions and "q" in C._sessions


@pytest.mark.asyncio
async def test_queued_admit_times_out_with_the_last_denial(monkeypatch):
    for i in range(5):
        assert await C.acquire(f"s{i}", "chat")
    _no_victims(monkeypatch)
    t0 = time.monotonic()
    adm = await C.acquire("late", "chat", queue_wait_s=0.3)
    assert not adm and adm.reason == "busy"
    assert 0.25 <= time.monotonic() - t0 < 2.0
    assert C._line == [] and "late" not in C._sessions


@pytest.mark.asyncio
async def test_queue_never_waits_on_host_memory_or_user_cap(env, monkeypatch):
    _no_victims(monkeypatch)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 1)
    assert await C.acquire("mine", "chat", user_sub="u1")
    t0 = time.monotonic()
    adm = await C.acquire("again", "chat", user_sub="u1", queue_wait_s=5)
    assert not adm and adm.reason == "user_cap"
    C.release("mine")
    assert await C.acquire("bg", "chat", execution_path="direct-llm")
    C._session_added_at["bg"] -= 200
    env["mb"] = 500                                   # non-session pressure
    adm = await C.acquire("other", "chat", queue_wait_s=5)
    assert not adm and adm.reason == "host_memory"
    assert time.monotonic() - t0 < 1.0 and C._line == []


@pytest.mark.asyncio
async def test_queued_waiter_cancelled_leaves_no_ticket_and_no_slot(monkeypatch):
    for i in range(5):
        assert await C.acquire(f"s{i}", "chat")
    _no_victims(monkeypatch)
    dave = asyncio.create_task(C.acquire("dave", "chat", queue_wait_s=5))
    await asyncio.sleep(0.02)
    frank = asyncio.create_task(C.acquire("frank", "chat", queue_wait_s=5))
    await asyncio.sleep(0.02)
    dave.cancel()                                     # the socket closed
    with pytest.raises(asyncio.CancelledError):
        await dave
    assert len(C._line) == 1 and "dave" not in C._sessions
    C.release("s0")
    assert await asyncio.wait_for(frank, 1.0)
    assert C._line == []


# --- pre-warms: room for two, quiet, never evict --------------------------------

@pytest.mark.asyncio
async def test_prewarm_allowed_needs_room_for_two_and_the_person_below_the_cap(
        env, monkeypatch):
    for i in range(3):
        assert await C.acquire(f"s{i}", "chat")
    assert C.prewarm_allowed()                       # 3 of 5 HEAVY: room for two
    assert await C.acquire("s3", "chat")
    assert not C.prewarm_allowed()                   # room for one only
    C.release("s3")
    env["mb"] = 300 + 1500                           # the live read fits one more
    assert not C.prewarm_allowed()
    env["mb"] = 100_000
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 4)
    assert not C.prewarm_allowed()                   # 3 + 2 > 4
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 5)
    assert C.prewarm_allowed()
    C.release("s2")
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 1)
    assert await C.acquire("mine", "chat", user_sub="u1")
    assert not C.prewarm_allowed(user_sub="u1")      # u1 holds their share
    assert C.prewarm_allowed(user_sub="u2")
    C._parked_tasks = 1
    assert not C.prewarm_allowed(user_sub="u2")      # a task waits for room


@pytest.mark.asyncio
async def test_speculative_admit_never_evicts_and_denies_quietly(monkeypatch):
    scans: list = []

    async def spy(*a, **k):
        scans.append(k)
        return C._Scan(None, 0, 0)
    monkeypatch.setattr(C, "_oldest_evictable_local", spy)
    for i in range(4):
        assert await C.acquire(f"s{i}", "chat")
    adm = await C.acquire_chat_slot("pw", user_sub="u1", speculative=True)
    assert not adm and adm.reason == "speculative" and adm.user_message is None
    assert "pw" not in C._sessions and scans == []
    C.release("s0")
    assert await C.acquire_chat_slot("pw", user_sub="u1", speculative=True)
    assert C._session_owner["pw"] == "u1"
    # A person at the cap: refused quietly, none of their sessions closed.
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 1)
    C.release("s1")
    C.release("s2")
    adm = await C.acquire_chat_slot("pw2", user_sub="u1", speculative=True)
    assert not adm and adm.reason == "speculative" and scans == []


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

    async def fake_oldest(min_idle, **_kw):
        return C._Scan(("s0", "cli", False) if "s0" in C._sessions else None, 0, 0)

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


class TestStdioMcpCount:
    """The Direct-LLM estimate counts the stdio servers of a generated MCP
    config; which transport is stdio is the manifest module's answer."""

    def test_counts_stdio_servers_by_the_transport_authority(self, tmp_path):
        from services.mcp.mcp_manifest_types import is_stdio_transport
        assert is_stdio_transport(None) and is_stdio_transport("stdio")
        assert not is_stdio_transport("http") and not is_stdio_transport("sse")
        cfg = tmp_path / "mcp-config.json"
        cfg.write_text(json.dumps({"mcpServers": {
            "a": {"command": "x"},
            "b": {"type": "stdio", "command": "y"},
            "c": {"type": "http", "url": "http://h"},
            "d": "not a server",
        }}), encoding="utf-8")
        assert C._stdio_mcp_count(str(cfg)) == 2
        assert C._stdio_mcp_count(str(tmp_path / "missing.json")) == 0
