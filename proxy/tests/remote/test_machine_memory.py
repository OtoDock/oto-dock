"""A paired machine's memory on the platform (``core/concurrency_paired.py``,
``core/remote/machine_memory.py``, the count in ``satellite_connection``).

A personal or admin machine whose satellite reports its memory starts a NEW
session only when it leaves the machine's reserve free: idle sessions there
are closed first, then the start is refused with one sentence; a task waits in
that machine's own line; a session already there, and a machine that reports
nothing, pass as before. The count limits such a machine only when an admin
set one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

import config
from core import concurrency as C, concurrency_paired as P
from core.events import turn_ending
from core.remote import machine_memory, satellite_connection
from core.session import interactive_registry, session_manager, session_state

LAPTOP = "m-laptop"
MIB = 1 << 20
GIB = 1 << 30


class _Manager:
    def __init__(self) -> None:
        self.memory: dict | None = {"total_bytes": 8 * GIB, "available_bytes": 5 * GIB,
                                    "swap_free_bytes": 0, "capped": False, "known": True}
        self.age = 1.0

    def is_compute(self, _mid):
        return False

    def owns_copy(self, _mid):
        return False

    def compute_machine_ids(self):
        return frozenset()

    def machine_memory_report(self, mid, compute=False):
        return (self.memory, self.age) if mid == LAPTOP and self.memory and not compute else None


class _Layer:
    """The remote layer's eviction, planted: ``idle`` sessions it may close."""

    def __init__(self) -> None:
        self.idle = 0
        self.closed = 0

    def idle_count_on_machine(self, _mid):
        return self.idle

    async def _evict_idle_on_machine(self, _mid, *, short=None, on_close=None):
        n = 0
        while short() and self.idle:
            self.idle -= 1
            self.closed += 1
            n += 1
            on_close()
        return n


@pytest_asyncio.fixture
async def laptop(monkeypatch):
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 850)
    monkeypatch.setattr(config, "SESSION_SWAP_CREDIT_MB", 0)
    manager, layer = _Manager(), _Layer()
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: None)
    monkeypatch.setattr(session_manager, "live_session", lambda _sid: None)
    monkeypatch.setattr(interactive_registry, "get", lambda _sid: None)
    from core.remote import remote_execution
    monkeypatch.setattr(remote_execution, "get_remote_layer", lambda: layer)

    async def _est(_sid, _path, _mcp):
        return 500
    monkeypatch.setattr(C, "_session_estimate", _est)
    C._cond = asyncio.Condition()
    P._admitted.clear()
    P._lines.clear()
    P._credits.clear()
    C._grow_debits.clear()
    C._parked_tasks = 0
    yield SimpleNamespace(manager=manager, layer=layer)
    P._admitted.clear()
    P._lines.clear()
    P._credits.clear()
    C._grow_debits.clear()


def _free(laptop, mb: int) -> None:
    laptop.manager.memory = {**laptop.manager.memory, "available_bytes": mb * MIB}


@pytest.mark.asyncio
async def test_a_new_chat_starts_with_room_and_is_debited(laptop):
    adm = await C.acquire("s-1", "chat", target=LAPTOP)
    assert adm.ok and "s-1" not in C._sessions        # nothing is tracked
    assert C._growin_debit_mb(LAPTOP) >= 490           # its start grows in


@pytest.mark.asyncio
async def test_a_low_laptop_refuses_a_new_chat_with_one_sentence(laptop):
    _free(laptop, 1_200)                     # 1,200 − 500 < 1,024 (the 8 GB laptop's reserve)
    adm = await C.acquire("s-1", "chat", target=LAPTOP)
    assert not adm and adm.reason == "machine_memory"
    assert adm.user_message == "This computer is low on memory. Close something on it or try again."


@pytest.mark.asyncio
async def test_idle_sessions_there_are_closed_first(laptop):
    _free(laptop, 1_300)                     # 276 MB short: two closes at 150 MB
    laptop.layer.idle = 3
    adm = await C.acquire("s-1", "chat", target=LAPTOP)
    assert adm.ok and laptop.layer.closed == 2


@pytest.mark.asyncio
async def test_idle_sessions_are_not_closed_when_they_cannot_cover_the_shortfall(laptop):
    _free(laptop, 600)                       # 924 MB short; two idle sessions free 300 at most
    laptop.layer.idle = 2
    adm = await C.acquire("s-1", "chat", target=LAPTOP)
    assert not adm and laptop.layer.closed == 0


@pytest.mark.asyncio
async def test_a_session_already_there_is_never_refused_or_debited_again(laptop, monkeypatch):
    assert (await C.acquire("s-1", "chat", target=LAPTOP)).ok
    debit = C._growin_debit_mb(LAPTOP)
    _free(laptop, 100)
    # The second acquire before it is registered (a terminal's), then a
    # re-attach of a session the remote layer holds alive there.
    assert (await C.acquire("s-1", "chat", target=LAPTOP)).ok
    assert C._growin_debit_mb(LAPTOP) <= debit
    P._admitted.clear()
    layer = SimpleNamespace(holds_alive=lambda _sid: True)
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(layer=layer, machine_id=LAPTOP, grace=False))
    assert (await C.acquire("s-1", "chat", target=LAPTOP)).ok


@pytest.mark.asyncio
async def test_a_dead_session_there_is_checked_as_a_new_start(laptop, monkeypatch):
    layer = SimpleNamespace(holds_alive=lambda _sid: False)
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(layer=layer, machine_id=LAPTOP, grace=False))
    _free(laptop, 100)
    assert not await C.acquire("s-1", "chat", target=LAPTOP)


@pytest.mark.asyncio
async def test_a_machine_that_reports_nothing_starts_as_before(laptop):
    laptop.manager.memory = None
    assert (await C.acquire("s-1", "chat", target=LAPTOP)).ok
    laptop.manager.memory = {"total_bytes": 8 * GIB, "available_bytes": 0, "swap_free_bytes": 0,
                             "capped": False, "known": False}
    assert (await C.acquire("s-2", "chat", target=LAPTOP)).ok
    laptop.manager.memory = {"total_bytes": 8 * GIB, "available_bytes": 0, "swap_free_bytes": 0,
                             "capped": False, "known": True}
    laptop.manager.age = 61.0             # stale: not checked by memory
    assert (await C.acquire("s-3", "chat", target=LAPTOP)).ok


@pytest.mark.asyncio
async def test_a_task_waits_in_its_machines_line_and_starts_when_a_report_frees_memory(laptop):
    _free(laptop, 1_000)
    task = asyncio.create_task(C.acquire("t-1", "task", target=LAPTOP, blocking=True))
    await asyncio.sleep(0.05)
    assert not task.done() and P._lines.get(LAPTOP) and C._parked_tasks == 0
    # Another person's task on the platform is not held by it.
    _free(laptop, 4_000)
    C.machine_report(LAPTOP)
    adm = await asyncio.wait_for(task, 2.0)
    assert adm.ok and not P._lines


@pytest.mark.asyncio
async def test_a_task_that_fits_at_once_says_nothing_about_waiting(laptop, caplog):
    _free(laptop, 4_000)
    caplog.set_level(logging.INFO, logger="otodock")
    adm = await asyncio.wait_for(C.acquire("t-1", "task", target=LAPTOP, blocking=True), 2.0)
    assert adm.ok
    assert "memory on" not in caplog.text


@pytest.mark.asyncio
async def test_a_waiting_task_says_so_once_and_once_more_when_it_starts(laptop, caplog):
    _free(laptop, 1_000)
    caplog.set_level(logging.INFO, logger="otodock")
    task = asyncio.create_task(C.acquire("t-1", "task", target=LAPTOP, blocking=True))
    await asyncio.sleep(0.05)
    C.machine_report(LAPTOP)
    await asyncio.sleep(0.05)
    _free(laptop, 4_000)
    C.machine_report(LAPTOP)
    assert (await asyncio.wait_for(task, 2.0)).ok
    lines = [r.getMessage() for r in caplog.records if "memory on" in r.getMessage()]
    assert lines == ["Task t-1 waits for memory on m-laptop",
                     "Task t-1 got memory on m-laptop after 0 s"]


@pytest.mark.asyncio
async def test_a_cancelled_waiting_task_leaves_the_line(laptop):
    _free(laptop, 1_000)
    task = asyncio.create_task(C.acquire("t-1", "task", target=LAPTOP, blocking=True))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not P._lines


@pytest.mark.asyncio
async def test_a_wake_waits_for_memory_and_times_out(laptop):
    _free(laptop, 1_000)
    got = await C.reserve_background("w-1", target=LAPTOP, timeout_s=0.2, superseded=lambda: False)
    assert got == "timeout"
    _free(laptop, 4_000)
    got = await C.reserve_background("w-2", target=LAPTOP, timeout_s=0.2, superseded=lambda: False)
    assert got == "untracked"


@pytest.mark.asyncio
async def test_a_tools_late_start_on_a_low_laptop_answers_the_tools_sentence(laptop, monkeypatch):
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(layer=None, machine_id=LAPTOP, grace=False))
    _free(laptop, 1_050)
    adm = await C.grow("s-1", judged=True, server="memory-mcp")
    assert not adm and adm.reason == "tool_memory"
    assert (await C.grow("s-1", judged=False, server="memory-mcp")).ok


def test_the_count_limits_a_machine_that_reports_memory_only_when_an_admin_set_one():
    manager = satellite_connection.SatelliteConnectionManager.__new__(satellite_connection.SatelliteConnectionManager)
    conn = SimpleNamespace(max_sessions=None, capabilities={"recommended_max_sessions": 2},
                           reported_sessions=5, sessions_reported=True, memory={"total_bytes": 1}, compute=False, load={},
                           memory_guard=True, name="laptop")
    manager._connections = {LAPTOP: conn}
    assert not manager.machine_at_capacity(LAPTOP)
    assert manager.concurrency_stats()[0]["max_sessions"] is None
    conn.max_sessions = 4
    assert manager.machine_at_capacity(LAPTOP)
    assert manager.concurrency_stats()[0]["max_sessions"] == 4
    conn.max_sessions, conn.memory = None, {}
    assert manager.machine_at_capacity(LAPTOP)            # an older satellite: its own count


def test_a_report_that_read_nothing_is_not_kept_and_the_guards_state_is():
    conn = SimpleNamespace(compute=False, memory={}, memory_at=0.0, memory_guard=None, machine_id=LAPTOP)
    satellite_connection._note_memory(conn, {"total_bytes": 1, "available_bytes": 0, "known": False}, True)
    assert conn.memory == {} and conn.memory_guard is True


def test_the_card_reads_free_memory_and_low_memory():
    conn = SimpleNamespace(compute=False, memory_guard=True,
                           memory={"total_bytes": 16 * GIB, "available_bytes": 1 * GIB})
    fields = machine_memory.stats_fields(conn)
    assert fields == {"memory_watched": True, "mem_total_mb": 16_384, "mem_free_mb": 1_024, "low_memory": True}
    assert machine_memory.stats_fields(SimpleNamespace(compute=False, memory={}, memory_guard=None)) == {
        "memory_watched": False}
    big = SimpleNamespace(compute=False, memory_guard=True,
                          memory={"total_bytes": 64 * GIB, "available_bytes": 5 * GIB})
    assert machine_memory.stats_fields(big)["low_memory"] is False   # its reserve stops at 4 GB
    mac = SimpleNamespace(compute=False, memory={}, memory_guard=None, capabilities={"os": "darwin"})
    assert machine_memory.stats_fields(mac)["memory_unwatched"] is True


def test_each_kind_gets_its_numbers(monkeypatch):
    monkeypatch.setattr(config, "SESSION_RESERVE_FLOOR_MB", 512)
    monkeypatch.setattr(config, "OTODOCK_SESSION_MEMORY_MAX_MB", 0)
    compute = machine_memory.policy({"pairing_scope": "compute"})
    assert compute["share"] == 0.05 and compute["floor_mb"] == 512 and compute["session_max_mb"] == 0
    assert compute["cap_mb"] == 0
    personal = machine_memory.policy({"pairing_scope": "user"})
    assert personal["share"] == 0.10 and personal["floor_mb"] == 1024 and personal["cap_mb"] == 4_096
    monkeypatch.setattr(config, "OTODOCK_SESSION_MEMORY_MAX_MB", 2_000)
    assert machine_memory.policy({"pairing_scope": "admin"})["session_max_mb"] == 6_144
    assert machine_memory.policy({"pairing_scope": "user"})["session_max_mb"] == 0


def test_a_stop_for_a_session_on_another_machine_is_named_not_acted_on(monkeypatch, caplog):
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: SimpleNamespace(machine_id="m-other"))
    machine_memory.note_stop(LAPTOP, {"session_id": "s-1", "kind": "session", "what": "command"})
    assert "does not run" in caplog.text


def test_a_stop_reads_as_what_was_stopped(caplog):
    """The log names a build or a sync as the machine's own work, and a
    command by its session."""
    import logging
    caplog.set_level(logging.WARNING)
    machine_memory.note_stop("a2b1723b-0000", {"session_id": "", "kind": "own", "what": "own", "mb": 2883,
                                               "reading_mb": 994, "reserve_mb": 1024, "why": "reserve"})
    assert ("Machine a2b1723b stopped its own work, a build or a sync, for memory: 2883 MB, 994 MB free, "
            "reserve 1024 MB (reserve)") in caplog.text


def test_a_terminals_stop_reads_as_a_stop(monkeypatch, caplog):
    """A terminal the platform runs on that machine is known by the terminal
    registry, not by the headless sessions: its stop is logged as a stop
    (mutation: the headless lookup alone)."""
    import logging
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: None)
    monkeypatch.setitem(interactive_registry._sessions, "t-1", SimpleNamespace(target=LAPTOP, alive=True))
    machine_memory.note_stop(LAPTOP, {"session_id": "t-1", "kind": "terminal", "what": "session", "mb": 181,
                                      "reading_mb": 3154, "reserve_mb": 1024, "why": "ceiling"})
    assert "does not run" not in caplog.text
    assert "Machine m-laptop stopped a session of terminal t-1, for memory: 181 MB, 3154 MB free" in caplog.text


def test_a_frame_writes_one_line_of_known_words(monkeypatch, caplog):
    """A person's computer sends the frame: a kind or a reason it is not one of
    the guard's words, a number that is not a number and an id with a newline
    write one line of the platform's log (mutation: the frame's words as sent)."""
    import logging
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: None)
    machine_memory.note_stop(LAPTOP, {"session_id": "", "kind": "own\nFAKE line", "what": "command",
                                      "mb": "9\nFAKE", "reading_mb": True, "reserve_mb": 1024,
                                      "why": "reserve\nFAKE"})
    machine_memory.note_stop(LAPTOP, {"session_id": "s\nFAKE", "kind": "session"})
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and not any("\n" in m for m in lines), lines
    assert lines[0] == "Machine m-laptop stopped a command of ? -, for memory: ? MB, ? MB free, reserve 1024 MB ()"
    assert "'s\\nFAKE'" in lines[1]


def test_the_memory_ending_tells_the_chat_it_continues():
    ending = turn_ending.TurnEnding(reason=turn_ending.EXITED, detail=turn_ending.MEMORY)
    assert ending.line() == ("⚠ This chat used too much memory, so its engine was stopped. Send the "
                             "message again, the conversation continues where it left off.")
    from core.layers.cli.remote import _memory_events
    events = _memory_events()
    assert events[0].data["ending"]["reason"] == "exited" and events[0].data["ending"]["detail"] == "memory"


def test_the_admitted_record_ends_after_its_window(laptop):
    P.note_admitted("s-1", LAPTOP, 500)
    P._admitted["s-1"] = (LAPTOP, time.monotonic() - P.ADMITTED_S - 1)
    assert not P.live("s-1", LAPTOP)
    C.release("s-1")
    assert "s-1" not in P._admitted
