"""Admission on a compute machine: a session it runs for the platform
reserves on that machine's box (``core/concurrency.py``, ``_session_box``).

The install's compute machine shares the host with the platform, so the
budget, the count cap and the grow-in debit stay one; Gate 2 reads the box's
own free memory (the machine's report, nothing known admits nothing), a box
with a cap of its own keeps its reservations under it, a victim on the box is
closed through the remote layer, and a session the machine kept across a
restart takes a slot at the next reconcile. The refusals are today's words.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

import config
from core import concurrency as C, concurrency_scan, placement
from core.remote import satellite_connection
from core.session import interactive_session, session_manager, session_state

MACHINE = "c-1"
GIB = 1 << 30


class _Manager:
    """The connection manager as the gates ask it: one compute machine, its
    last memory report and its age."""

    def __init__(self) -> None:
        self.memory: dict | None = {"total_bytes": 16 * GIB, "available_bytes": 12 * GIB,
                                    "swap_free_bytes": 0, "capped": False}
        self.age = 1.0
        self.connected = True

    def is_compute(self, machine_id: str) -> bool:
        return machine_id == MACHINE and self.connected

    def owns_copy(self, machine_id: str) -> bool:
        return False

    def exit_state(self, machine_id: str) -> tuple[int, bool]:
        return 0, False

    def compute_machine_ids(self) -> frozenset[str]:
        return frozenset({MACHINE}) if self.connected else frozenset()

    def machine_memory_report(self, machine_id: str, compute: bool = False):
        if machine_id != MACHINE or self.memory is None:
            return None
        return self.memory, self.age

    def concurrency_stats(self) -> list:
        return []


@pytest_asyncio.fixture
async def box(monkeypatch):
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "SESSION_EST_LIGHT_MB", 200)
    monkeypatch.setattr(config, "SESSION_RESERVE_FLOOR_MB", 300)
    monkeypatch.setattr(config, "SESSION_SWAP_CREDIT_MB", 0)
    monkeypatch.setattr(config, "BUDGET_FRACTION", 0.9)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "get_idle_timeout", lambda: 900)
    tables = (C._sessions, C._session_est, C._session_added_at, C._session_owner, C._session_box,
              C._session_host, C._session_grown, C._evicted_credit, concurrency_scan._grow_debits)
    for table in tables:
        table.clear()
    C._line.clear()
    # Put back when the test ends, whatever a test assigned to them.
    for name, value in (("_reserved_mb", 0), ("_parked_tasks", 0), ("_live_cache", None),
                        ("_cond", asyncio.Condition()), ("_budget_mb", 50_000), ("_floor_mb", 300),
                        ("_total_mb", 64_000)):
        monkeypatch.setattr(C, name, value)
    manager = _Manager()
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    platform_live = {"mb": 100_000}
    monkeypatch.setattr(C, "_live_available_mb", lambda: platform_live["mb"])
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: None)
    monkeypatch.setattr(session_state, "cached_idle_timeout", _async(900))
    yield SimpleNamespace(manager=manager, platform_live=platform_live)
    for table in tables:
        table.clear()
    C._line.clear()


def _async(value):
    async def _f(*_a, **_k):
        return value
    return _f


@pytest.mark.asyncio
async def test_a_meeting_reserves_each_participant_on_its_own_box(box):
    """A Direct LLM participant's tools run on the compute machine: its
    reservation is that box's, a participant on the platform stays there,
    and a paired machine's counts on that machine alone."""
    adm = await C.acquire_meeting_slots(
        ["m-direct", "m-platform", "m-paired"],
        targets={"m-direct": MACHINE, "m-platform": "local", "m-paired": "m-paired"},
    )
    assert adm
    assert C._session_box["m-direct"] == MACHINE and C._session_box.get("m-platform", "") == ""
    assert "m-platform" in C._sessions and "m-paired" not in C._sessions
    C.release_meeting_slots(["m-direct", "m-platform", "m-paired"])
    assert C._reserved_mb == 0


@pytest.mark.asyncio
async def test_a_meeting_is_denied_whole_when_a_box_has_no_memory(box):
    """All or nothing across the boxes: Gate 2 of one box refuses the whole
    meeting, and nothing stays reserved."""
    box.manager.memory = None           # no fresh report: the box admits nothing
    adm = await C.acquire_meeting_slots(
        ["m-direct", "m-platform"], targets={"m-direct": MACHINE, "m-platform": "local"},
    )
    assert not adm and adm.reason == "host_memory"
    assert C._sessions == {} and C._reserved_mb == 0


def _never_gives_it_up(monkeypatch) -> None:
    def _no_release(_sid):
        raise AssertionError("a slot the call can keep was given up")
    monkeypatch.setattr(C, "_remove", _no_release)


@pytest.mark.asyncio
async def test_a_rebuilt_calls_slot_on_the_right_box_is_left_alone(box, monkeypatch):
    """Given up and asked for again, a waiter could take the room in between
    and a call that held a good slot would hold nothing."""
    await C.acquire_chat_slot("c-1", kind="phone", target=MACHINE, user_sub="u-1")
    added = C._session_added_at["c-1"]
    _never_gives_it_up(monkeypatch)
    assert await C.rehome_slot("c-1", kind="phone", target=MACHINE, user_sub="u-1")
    assert C._session_box["c-1"] == MACHINE and C._session_added_at["c-1"] == added and C._reserved_mb == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("held_on, lands_on, box_after", [(MACHINE, "local", ""), ("local", MACHINE, MACHINE)])
async def test_a_rebuilt_calls_slot_moves_to_the_box_its_tools_landed_on(box, monkeypatch, held_on, lands_on, box_after):
    await C.acquire_chat_slot("c-1", kind="phone", target=held_on, user_sub="u-1")
    _never_gives_it_up(monkeypatch)
    assert await C.rehome_slot("c-1", kind="phone", target=lands_on, user_sub="u-1")
    assert C._session_box.get("c-1", "") == box_after
    assert C._sessions["c-1"] == "phone" and C._session_owner["c-1"] == "u-1" and C._reserved_mb == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [None, {"total_bytes": GIB // 2, "available_bytes": GIB // 2,
                                           "swap_free_bytes": 0, "capped": True}], ids=["no-report", "its-cap-binds"])
async def test_a_slot_that_cannot_move_stays_where_it_was(box, monkeypatch, memory):
    """Never neither: a box with no room leaves the call the slot it holds."""
    await C.acquire_chat_slot("c-1", kind="phone", target="local", user_sub="u-1")
    box.manager.memory = memory
    _never_gives_it_up(monkeypatch)
    adm = await C.rehome_slot("c-1", kind="phone", target=MACHINE, user_sub="u-1")
    assert not adm and adm.reason == "host_memory"
    assert C._sessions["c-1"] == "phone" and C._session_box.get("c-1", "") == "" and C._reserved_mb == 1000


@pytest.mark.asyncio
async def test_a_waiter_for_the_room_cannot_strand_a_rebuilt_call(box, monkeypatch, tmp_path):
    """Released and asked for again, the slot would go to the session in line
    while the estimate's read yields, and the call would hold nothing."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"tool": {"type": "stdio"}}}))
    monkeypatch.setattr(C, "_budget_mb", 1100)
    terms = {"kind": "phone", "target": "local", "execution_path": "direct-llm", "mcp_config_path": str(cfg)}
    assert await C.acquire_chat_slot("c-1", **terms)
    assert C._session_est["c-1"] < 1000     # the next session (1000) does not fit beside it
    waiter = asyncio.ensure_future(C.acquire_chat_slot("c-2", queue_wait_s=0.5))
    await asyncio.sleep(0.05)
    await C.rehome_slot("c-1", **terms)
    assert not await waiter
    assert "c-1" in C._sessions and "c-2" not in C._sessions


@pytest.mark.asyncio
async def test_a_slot_the_reaper_released_is_taken_again_on_the_new_box(box):
    adm = await C.rehome_slot("c-1", kind="phone", target=MACHINE, user_sub="u-1")
    assert adm and C._sessions["c-1"] == "phone" and C._session_box["c-1"] == MACHINE
    assert C._session_owner["c-1"] == "u-1" and C._reserved_mb == 1000


@pytest.mark.asyncio
async def test_a_call_with_no_slot_and_no_room_is_told_so_and_nothing_raises(box):
    box.manager.memory = None
    adm = await C.rehome_slot("c-1", kind="phone", target=MACHINE, user_sub="u-1")
    assert not adm and adm.reason == "host_memory" and "c-1" not in C._sessions and C._reserved_mb == 0


@pytest.mark.asyncio
async def test_a_compute_session_reserves_on_its_machines_box(box):
    adm = await C.acquire("s-1", "chat", target=MACHINE)
    assert adm and C._session_box["s-1"] == MACHINE and C._reserved_mb == 1000
    # A paired machine's session still counts on that machine alone.
    assert await C.acquire("p-1", "chat", target="m-paired")
    assert "p-1" not in C._sessions
    C.release("s-1")
    assert "s-1" not in C._session_box and C._reserved_mb == 0


@pytest.mark.parametrize("switch", ["compute", "local"])
@pytest.mark.asyncio
async def test_a_kept_placement_on_a_machine_that_is_gone_admits_on_the_platform(box, monkeypatch, switch):
    """After a restart the reloaded security context still names the compute
    machine. Past the hold's bound, or under ``local``, the chat resumes on the
    platform: it reserves there, never on a box nothing is known of ("0 MB
    free"). A machine that is connected, or a session the remote layer still
    holds (its machine in its grace), keeps the box."""
    monkeypatch.setattr(config, "OTODOCK_COMPUTE_SESSIONS", switch, raising=False)
    kept = SimpleNamespace(placement=SimpleNamespace(kind=placement.KIND_COMPUTE, machine_id="c-gone"))
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: kept)
    monkeypatch.setattr(session_manager, "live_session", lambda _sid: None)
    adm = await C.acquire("s-1", "chat", target="local")
    assert adm and "s-1" in C._sessions and C._session_box.get("s-1", "") == ""
    monkeypatch.setattr(session_manager, "live_session", lambda _sid: SimpleNamespace(grace=True))
    assert C._box_for("s-2", "local") == "c-gone"
    monkeypatch.setattr(session_manager, "live_session", lambda _sid: None)
    kept.placement = SimpleNamespace(kind=placement.KIND_COMPUTE, machine_id=MACHINE)
    assert C._box_for("s-3", "local") == MACHINE


@pytest.mark.asyncio
async def test_the_compute_machines_own_free_memory_decides_with_todays_words(box):
    # 1024 MB free there, plenty here; its reserve is 5 % of its 16 GiB, 819 MB.
    box.manager.memory["available_bytes"] = 1 * GIB
    C._add("l-0", "chat", 200)  # a light session on the platform: sessions hold none of it
    adm = await C.acquire("s-1", "chat", target=MACHINE)
    assert not adm and adm.reason == "host_memory"
    assert adm.user_message == (
        "This server is low on memory: 1024 MB free, about 1819 MB needed to start. "
        "Free some memory on the server, then try again.")
    # The platform's own box is not that machine's: a local session goes in.
    assert await C.acquire("l-1", "chat", target="local")


@pytest.mark.asyncio
async def test_nothing_known_of_the_machines_memory_admits_nothing(box):
    """No report, or one past REPORT_FRESH_S, reads as zero free, and the
    empty platform's last-resort admit does not apply there."""
    box.manager.memory = None
    adm = await C.acquire("s-1", "chat", target=MACHINE)
    assert not adm and adm.reason == "host_memory" and "0 MB free" in adm.user_message
    box.manager.memory = {"total_bytes": 16 * GIB, "available_bytes": 12 * GIB,
                          "swap_free_bytes": 0, "capped": False}
    box.manager.age = concurrency_scan.REPORT_FRESH_S + 1
    adm = await C.acquire("s-2", "chat", target=MACHINE)
    assert not adm and adm.reason == "host_memory"
    box.manager.age = 1.0
    assert await C.acquire("s-3", "chat", target=MACHINE)


@pytest.mark.asyncio
async def test_a_capped_compute_machine_keeps_its_sessions_under_its_own_cap(box):
    # A 4 GiB cap of its own: 0.9 x 4096 = 3686 MB of reservations, three of 1000.
    box.manager.memory.update(total_bytes=4 * GIB, available_bytes=4 * GIB, capped=True)
    for sid in ("s-1", "s-2", "s-3"):
        assert await C.acquire(sid, "chat", target=MACHINE)
    assert not C._gate1(1000, is_task=False, box=MACHINE)
    # The platform's own box is not held to that cap.
    assert C._gate1(1000, is_task=False, box="")
    # The cap is read from each report: raised, the fourth fits.
    box.manager.memory.update(total_bytes=8 * GIB, available_bytes=8 * GIB)
    assert C._gate1(1000, is_task=False, box=MACHINE)


@pytest.mark.asyncio
async def test_the_host_budget_and_the_count_cap_count_both_boxes(box, monkeypatch):
    C._budget_mb = 2500
    assert await C.acquire("l-1", "chat", target="local")
    assert await C.acquire("s-1", "chat", target=MACHINE)
    assert not C._gate1(1000, is_task=False, box=MACHINE)
    assert not C._gate1(1000, is_task=False, box="")
    C._budget_mb = 50_000
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 2)
    assert not C._gate1(200, is_task=False, box=MACHINE)


@pytest.mark.asyncio
async def test_a_compute_victim_is_closed_through_the_remote_layer(box, monkeypatch):
    closed: list[str] = []

    class _Remote:
        _sessions = {"idle-c": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE,
                                               last_activity=time.monotonic() - 3600,
                                               turn_active=False, user_sub="")}

        def is_session_grace_held(self, _sid):
            return False

        async def close_session(self, sid):
            closed.append(sid)

    monkeypatch.setattr(session_manager, "_remote_layer", _Remote())
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(grace=_Remote().is_session_grace_held(sid)))
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set())
    monkeypatch.setattr(session_state, "get_hook_activity", lambda _sid: float("-inf"))
    assert await C.acquire("idle-c", "chat", target=MACHINE)
    C._session_added_at["idle-c"] -= 3600
    C._budget_mb = 1500  # one fits: the next admit must evict
    adm = await C.acquire("new", "chat", target=MACHINE)
    assert adm and closed == ["idle-c"] and "idle-c" not in C._sessions


@pytest.mark.asyncio
async def test_a_victim_whose_engine_holds_no_session_gives_back_what_it_owns(box, monkeypatch):
    """A victim whose engine has no holder in the proxy has no layer to
    close it on: the slot and the seat are given back without one."""
    given: list[str] = []
    monkeypatch.setattr(session_manager, "holding_engines", dict)
    monkeypatch.setattr(session_manager, "release_unheld", given.append)
    assert await C.acquire("gone", "chat", target=MACHINE)
    assert await C._evict_one("gone", "direct")
    assert given == ["gone"] and "gone" not in C._sessions


@pytest.mark.asyncio
async def test_an_eviction_on_compute_counts_before_the_next_report(box, monkeypatch):
    """A compute box's free memory is its last report, which an eviction does
    not change: each evicted estimate counts as freed on its box until a
    report newer than the eviction, so one admit evicts what it needs and no
    more."""
    closed: list[str] = []
    old = time.monotonic() - 3600
    records = {f"idle-{i}": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE,
                                            last_activity=old, turn_active=False, user_sub="")
               for i in range(3)}

    class _Remote:
        _sessions = records

        def is_session_grace_held(self, _sid):
            return False

        async def close_session(self, sid):
            closed.append(sid)
            records.pop(sid, None)

    monkeypatch.setattr(session_manager, "_remote_layer", _Remote())
    monkeypatch.setattr(session_manager, "live_session", lambda sid: SimpleNamespace(grace=False))
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set())
    monkeypatch.setattr(session_state, "get_hook_activity", lambda _sid: float("-inf"))
    for sid in records:
        assert await C.acquire(sid, "chat", target=MACHINE)
        C._session_added_at[sid] -= 3600
    # The machine is short by about one session's estimate; its report stays as it was.
    box.manager.memory["available_bytes"] = (1000 + 300) * (1 << 20)
    box.manager.age = 5.0
    adm = await C.acquire("new", "chat", target=MACHINE)
    assert adm and len(closed) == 1, closed
    # A report newer than the eviction replaces the credit.
    box.manager.age = 0.0
    assert C._box_live_mb(MACHINE) == 1300


@pytest.mark.asyncio
async def test_an_evicted_young_session_is_credited_only_what_the_report_does_not_show(box, monkeypatch):
    """A session admitted inside the grow-in window was debited only the share
    not yet in the machine's free memory; its eviction credits that share
    alone, never the whole estimate on top of the debit it also drops."""
    async def _close(_sid):
        return None
    monkeypatch.setattr(session_manager, "_remote_layer", SimpleNamespace(close_session=_close))
    for sid, age in (("young", 45.0), ("old", 3600.0)):
        C._add(sid, "chat", 1000, box=MACHINE)
        C._session_added_at[sid] = time.monotonic() - age
        assert await C._evict_one(sid, "remote")
    credits = [mb for _, mb in C._evicted_credit[MACHINE]]
    assert 490 <= credits[0] <= 510 and credits[1] == 1000


@pytest.mark.asyncio
async def test_a_turn_on_compute_and_a_record_in_its_grace_are_spared(box, monkeypatch):
    class _Remote:
        _sessions = {
            "busy": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE,
                                    last_activity=time.monotonic() - 3600, turn_active=True),
            "graced": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE,
                                      last_activity=time.monotonic() - 3600, turn_active=False),
        }

        def is_session_grace_held(self, sid):
            return sid == "graced"

    monkeypatch.setattr(session_manager, "_remote_layer", _Remote())
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(grace=_Remote().is_session_grace_held(sid)))
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set())
    monkeypatch.setattr(session_state, "get_hook_activity", lambda _sid: float("-inf"))
    # Both are live (the reconciler keeps their slots); neither is a victim.
    assert [sid for sid, *_rest in concurrency_scan.compute_sessions()] == ["busy", "graced"]
    assert concurrency_scan._turn_live("busy", _Remote._sessions["busy"])
    for sid in ("busy", "graced"):
        C._add(sid, "chat", 1000, box=MACHINE)
        C._session_added_at[sid] -= 3600
    scan = await concurrency_scan._oldest_evictable_local(0, prefer_user=None, pending_leash_s=10 ** 9,
                                                         turn_leash_s=10 ** 9, prewarm_any_age=True)
    assert scan.victim is None


@pytest.mark.asyncio
async def test_the_reconciler_gives_a_kept_compute_session_a_slot_and_frees_an_orphan(box, monkeypatch):
    class _Remote:
        _sessions = {
            "kept": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE),
            "run": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE),
            "paired": SimpleNamespace(alive=True, files_synced=True, compute=False, machine_id="m-1"),
        }

        def is_session_grace_held(self, _sid):
            return False

    monkeypatch.setattr(session_manager, "_remote_layer", _Remote())
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(grace=_Remote().is_session_grace_held(sid)))
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set())
    monkeypatch.setattr(session_state, "get_session_client_type",
                        lambda sid: "task" if sid == "run" else "dashboard")
    monkeypatch.setattr(session_manager, "holding_engines", lambda: {})
    C._add("gone", "chat", 1000, box=MACHINE)
    C._session_added_at["gone"] -= 3600
    released = await C.reconcile_chat_slots()
    assert released == 1 and "gone" not in C._sessions
    assert C._sessions.get("kept") == "chat" and C._session_box["kept"] == MACHINE
    assert not C._session_owner.get("kept")
    assert "run" not in C._sessions and "paired" not in C._sessions
    assert C._growin_debit_mb() == 0  # memory it already held is not debited again


@pytest.mark.asyncio
async def test_a_session_in_its_machines_grace_keeps_its_slot_at_the_reconcile(box, monkeypatch):
    """Grace-held records are spared by eviction alone: the reconciler's live
    set still counts them, so the slot is there when the machine returns."""
    class _Remote:
        _sessions = {"graced": SimpleNamespace(alive=True, files_synced=False, compute=True, machine_id=MACHINE)}

    monkeypatch.setattr(session_manager, "_remote_layer", _Remote())
    monkeypatch.setattr(session_manager, "live_session", lambda sid: SimpleNamespace(grace=True))
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set())
    monkeypatch.setattr(session_state, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(session_manager, "holding_engines", lambda: {})
    C._add("graced", "chat", 1000, box=MACHINE)
    C._session_added_at["graced"] -= 3600
    assert await C.reconcile_chat_slots() == 0
    assert C._session_box.get("graced") == MACHINE


@pytest.mark.asyncio
async def test_a_wake_on_compute_reserves_on_the_box(box, monkeypatch):
    monkeypatch.setattr(session_state, "is_starting", lambda _sid: False)
    monkeypatch.setattr(session_state, "session_is_held", lambda _sid: False)
    state = await C.reserve_background("w-1", target=MACHINE, execution_path="claude-code-cli",
                                       timeout_s=1, superseded=lambda: False)
    assert state == "reserved" and C._session_box["w-1"] == MACHINE
    monkeypatch.setattr(session_state, "session_is_held", lambda sid: sid == "w-2")
    assert await C.reserve_background("w-2", target=MACHINE, timeout_s=1,
                                      superseded=lambda: False) == "superseded"
    assert await C.reserve_background("w-3", target="m-paired", timeout_s=1,
                                      superseded=lambda: False) == "untracked"


@pytest.mark.asyncio
async def test_a_prewarm_asks_the_box_it_would_start_on(box, monkeypatch):
    built = placement.from_machine(placement.KIND_COMPUTE, {"id": MACHINE, "name": "Compute",
                                                            "pairing_scope": "compute"})
    monkeypatch.setattr(session_manager, "compute_placement_for", lambda _path, **_kw: built)
    box.manager.memory["available_bytes"] = 2 * GIB  # one heavy and the floor, not two
    assert not C.prewarm_allowed("claude-code-cli")
    box.manager.memory["available_bytes"] = 8 * GIB
    assert C.prewarm_allowed("claude-code-cli")
    # With no compute machine nothing is pre-warmed, whatever room the
    # platform's own host has: nothing would run there.
    monkeypatch.setattr(session_manager, "compute_placement_for", lambda _path, **_kw: None)
    assert not C.prewarm_allowed("claude-code-cli")


@pytest.mark.asyncio
async def test_stats_show_each_compute_machines_box(box):
    C._add("s-1", "chat", 1000, box=MACHINE)
    stats = C.get_stats()["compute"]
    assert stats == [{"machine_id": MACHINE, "own_host": False, "total_mb": 16384, "available_mb": 12288,
                      "capped": False, "reported": True, "reserved_mb": 1000, "sessions": 1}]


def test_the_connection_keeps_a_machines_report_and_nothing_else():
    conn = satellite_connection.SatelliteConnection(machine_id="c-1", ws=None, capabilities={},
                                                    satellite_version="0.5.173")
    conn.compute = True
    satellite_connection._note_memory(conn, {"total_bytes": 8 * GIB, "available_bytes": GIB,
                                             "swap_free_bytes": 0, "capped": True, "extra": "x"})
    assert conn.memory == {"total_bytes": 8 * GIB, "available_bytes": GIB, "swap_free_bytes": 0,
                           "capped": True} and conn.memory_at > 0
    for bad in ({"total_bytes": "8"}, {"available_bytes": 1}, {"total_bytes": -1, "available_bytes": 1}, "x"):
        kept = dict(conn.memory)
        satellite_connection._note_memory(conn, bad)
        assert conn.memory == kept, bad
    paired = satellite_connection.SatelliteConnection(machine_id="m-1", ws=None, capabilities={},
                                                      satellite_version="0.5.173")
    # A paired machine's report is kept too (0.5.177: its guard runs), and
    # one that read nothing is not.
    satellite_connection._note_memory(paired, {"total_bytes": GIB, "available_bytes": GIB})
    assert paired.memory == {"total_bytes": GIB, "available_bytes": GIB, "capped": False}
    fresh = satellite_connection.SatelliteConnection(machine_id="m-2", ws=None, capabilities={},
                                                     satellite_version="0.5.177")
    satellite_connection._note_memory(fresh, {"total_bytes": GIB, "available_bytes": 0, "known": False})
    assert fresh.memory == {}


# --- the platform's place with no compute machine ---------------------------

NOWHERE = placement.NO_COMPUTE


@pytest.mark.asyncio
async def test_a_session_with_no_compute_machine_is_refused_before_any_gate(box):
    """The platform runs no session itself: with no compute machine to take
    it a chat, a call, a meeting and a slot's move are refused with their own
    reason and the one sentence, a pre-warm quietly, a wake is kept for
    later, and nothing is reserved anywhere: never on the platform's own
    box, never judged as a paired computer."""
    paired: list = []
    monkeypatch_paired = C._admit_on_paired
    C._admit_on_paired = lambda *a, **k: paired.append(a) or _async(C._ADMITTED)()  # type: ignore[assignment]
    try:
        for kind in ("chat", "phone"):
            adm = await C.acquire_chat_slot("s-" + kind, kind=kind, target=NOWHERE, user_sub="u-1")
            assert not adm and adm.reason == "no_compute"
            assert adm.user_message == placement.NO_COMPUTE_SENTENCE
        quiet = await C.acquire_chat_slot("s-pw", target=NOWHERE, speculative=True)
        assert not quiet and quiet.reason == "speculative" and quiet.user_message is None
        meeting = await C.acquire_meeting_slots(["m-1", "m-2"], targets={"m-1": MACHINE, "m-2": NOWHERE})
        assert not meeting and meeting.reason == "no_compute"
        moved = await C.rehome_slot("s-chat", kind="phone", target=NOWHERE)
        assert not moved and moved.reason == "no_compute"
        assert await C.reserve_background("w-1", target=NOWHERE, timeout_s=5,
                                          superseded=lambda: False) == "timeout"
        task = await C.acquire("t-1", "task", target=NOWHERE, blocking=True)
        assert not task and task.reason == "no_compute"
    finally:
        C._admit_on_paired = monkeypatch_paired  # type: ignore[assignment]
    assert C._sessions == {} and C._reserved_mb == 0 and paired == []


@pytest.mark.asyncio
async def test_a_session_a_compute_machine_holds_keeps_its_slot_under_that_word(box, monkeypatch):
    """A caller with no compute placement in hand asks with the word, and a
    session the machine already runs answers for itself: its box, idempotent."""
    monkeypatch.setattr(concurrency_scan, "placed_on_compute", lambda sid: MACHINE if sid == "live" else "")
    monkeypatch.setattr(C, "placed_on_compute", lambda sid: MACHINE if sid == "live" else "", raising=False)
    assert await C.acquire_chat_slot("live", target=MACHINE)
    assert await C.acquire_chat_slot("live", target=NOWHERE)
    assert C._session_box["live"] == MACHINE and len(C._sessions) == 1


def test_admissions_word_for_the_platforms_place_with_no_machine():
    built = placement.from_machine(placement.KIND_COMPUTE, {"id": MACHINE, "pairing_scope": "compute"})
    assert placement.admission_target("local", built) == MACHINE
    assert placement.admission_target("local", None) == NOWHERE
    assert placement.admission_target("", placement.LOCAL_PLACEMENT) == NOWHERE
    assert placement.admission_target("m-paired", None) == "m-paired"
    assert placement.machine_of(NOWHERE) == "" and not placement.is_local(NOWHERE)


@pytest.mark.asyncio
async def test_a_run_waits_for_a_compute_machine_and_is_woken_by_its_sign_in(box):
    """A task of the platform's place waits for a compute machine, at most
    the bound: a sign-in wakes it at once, and with none it gives up."""
    box.manager.connected = False

    def serves() -> bool:
        return box.manager.connected

    started = time.monotonic()
    assert await C.wait_for_compute(serves, timeout_s=0.2) is False
    assert 0.2 <= time.monotonic() - started < 2.0
    waiter = asyncio.ensure_future(C.wait_for_compute(serves, timeout_s=30))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    box.manager.connected = True
    C.compute_signed_in()
    assert await asyncio.wait_for(waiter, 2.0) is True
    assert await C.wait_for_compute(serves, timeout_s=0) is True


@pytest.mark.asyncio
async def test_a_parked_task_stays_in_line_across_a_short_absence_of_its_machine(box, monkeypatch):
    """A task waiting for room on a compute machine that restarts is not
    judged anywhere else: it stays parked, is admitted on that box when the
    machine is back, and gives up only after the machine stayed away for the
    bound (its run's build then says the sentence)."""
    box.manager.memory = {"total_bytes": 16 * GIB, "available_bytes": 1 * GIB,
                          "swap_free_bytes": 0, "capped": False}
    parked = asyncio.ensure_future(C.acquire("t-1", "task", target=MACHINE, blocking=True))
    await asyncio.sleep(0.05)
    assert not parked.done() and C._parked_tasks == 1
    box.manager.connected = False                    # the machine restarts
    async with C._cond:
        C._cond.notify_all()
    await asyncio.sleep(0.05)
    assert not parked.done(), "a short absence must not move or end the wait"
    box.manager.connected = True
    box.manager.memory = {"total_bytes": 16 * GIB, "available_bytes": 12 * GIB,
                          "swap_free_bytes": 0, "capped": False}
    C.compute_signed_in()
    adm = await asyncio.wait_for(parked, 2.0)
    assert adm and C._session_box["t-1"] == MACHINE

    box.manager.memory = {"total_bytes": 16 * GIB, "available_bytes": 1 * GIB,
                          "swap_free_bytes": 0, "capped": False}
    gone = asyncio.ensure_future(C.acquire("t-2", "task", target=MACHINE, blocking=True))
    await asyncio.sleep(0.05)
    box.manager.connected = False
    monkeypatch.setattr(C, "NO_COMPUTE_WAIT_S", 0.0)
    C._box_away_since.clear()
    async with C._cond:
        C._cond.notify_all()                         # first seen away
    await asyncio.sleep(0.02)
    async with C._cond:
        C._cond.notify_all()                         # away past the bound
    adm = await asyncio.wait_for(gone, 2.0)
    assert not adm and adm.reason == "no_compute" and "t-2" not in C._sessions
    assert C._parked_tasks == 0


def test_a_sign_in_forgets_when_the_machine_went_away(monkeypatch):
    """A machine that came back after every waiting task had given up keeps
    no old "away since": its sign-in forgets it. Otherwise a task parked
    there during its next absence would read the old time and give up at
    once, where it waits for the machine's return."""
    import time

    monkeypatch.setattr(C, "_box_away_since", {MACHINE: time.monotonic() - 10 * C.NO_COMPUTE_WAIT_S,
                                               "c-other": time.monotonic()})
    notified: list = []
    monkeypatch.setattr(C, "_schedule_notify", lambda: notified.append(1))
    C.compute_signed_in(MACHINE)
    assert MACHINE not in C._box_away_since and "c-other" in C._box_away_since
    assert notified == [1]
    C.compute_signed_in()
    assert "c-other" in C._box_away_since and notified == [1, 1]
