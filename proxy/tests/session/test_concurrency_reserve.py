"""The reserve admission keeps and the headroom a task or a wake leaves
(``core/concurrency.py`` with ``core/sandbox/host_resources.py``), on the
cloud's free plan: a 4 GB server of which the platform holds about 2 GB.

The reserve is the larger of ``SESSION_RESERVE_FLOOR_MB`` and 5 % of the
machine, swap counts for nothing by default, and a task or a wake keeps one
HEAVY free on a large machine but only a sixteenth of the room on a small one,
so the smallest server still starts its scheduled work.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

import config
from core import concurrency as C
from core.remote import satellite_connection
from core.sandbox import host_resources
from core.session import session_state

MIB = 1 << 20


class _Rig:
    total_mb = 4_096
    live_mb = 2_048


@pytest_asyncio.fixture
async def server(monkeypatch):
    """The free plan's server with the shipped defaults."""
    for key in ("SESSION_EST_HEAVY_MB", "SESSION_EST_LIGHT_MB", "SESSION_RESERVE_FLOOR_MB",
                "SESSION_SWAP_CREDIT_MB", "BUDGET_FRACTION"):
        monkeypatch.setattr(config, key, type(getattr(config, key))(
            {"SESSION_EST_HEAVY_MB": 850, "SESSION_EST_LIGHT_MB": 250, "SESSION_RESERVE_FLOOR_MB": 512,
             "SESSION_SWAP_CREDIT_MB": 0, "BUDGET_FRACTION": 0.9}[key]))
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    rig = _Rig()
    monkeypatch.setattr(host_resources, "detect_memory_limit_bytes", lambda: rig.total_mb * MIB)
    monkeypatch.setattr(host_resources, "live_available_bytes", lambda: rig.live_mb * MIB)
    monkeypatch.setattr(host_resources, "swap_free_bytes", lambda: 2_048 * MIB)
    manager = SimpleNamespace(is_compute=lambda _m: False, owns_copy=lambda _m: False,
                              machine_memory_report=lambda _m, compute=False: None, compute_machine_ids=frozenset)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: None)
    tables = (C._sessions, C._session_est, C._session_added_at, C._session_owner, C._session_box,
              C._session_host, C._session_grown, C._evicted_credit)
    for table in tables:
        table.clear()
    C._line.clear()
    C._task_ring.clear()
    C._task_ring_order.clear()
    C._grow_debits.clear()
    C._reserved_mb = 0
    C._parked_tasks = 0
    C._live_cache = None
    C.init()
    yield rig
    for table in tables:
        table.clear()
    C._line.clear()
    C._reserved_mb = 0
    C._live_cache = None


def _resting_chat(sid: str) -> None:
    """A chat admitted long ago (its grow-in debit is over) that holds about 400 MB."""
    C._add(sid, "chat", 500, "", "")
    C._session_added_at[sid] = time.monotonic() - 600


def test_the_reserve_is_512_mb_or_5_percent():
    assert host_resources.reserve_mb(4_096, floor_mb=512, share=host_resources.COMPUTE_SHARE) == 512
    assert host_resources.reserve_mb(20_480, floor_mb=512, share=host_resources.COMPUTE_SHARE) == 1_024
    assert host_resources.reserve_mb(65_536, floor_mb=512, share=host_resources.COMPUTE_SHARE) == 3_276
    assert host_resources.reserve_mb(8_192, floor_mb=host_resources.COMPUTER_FLOOR_MB,
                                     share=host_resources.COMPUTER_SHARE) == 1_024
    assert host_resources.reserve_mb(65_536, floor_mb=host_resources.COMPUTER_FLOOR_MB,
                                     share=host_resources.COMPUTER_SHARE) == 6_553


def test_a_task_keeps_one_heavy_on_a_large_machine_and_a_32nd_of_the_room_on_a_small_one():
    assert host_resources.task_headroom_mb(4_096, 512, 850) == 112
    assert host_resources.task_headroom_mb(8_192, 512, 850) == 240
    assert host_resources.task_headroom_mb(20_480, 1_024, 850) == 608
    assert host_resources.task_headroom_mb(32_768, 1_638, 850) == 850


@pytest.mark.asyncio
async def test_on_the_4_gb_server_with_nothing_running_a_task_starts(server):
    assert C._floor_mb == 512 and C._live_available_mb() == 2_048   # the swap credit is off
    adm = await asyncio.wait_for(C.acquire("t-1", "task", blocking=True), 1.0)
    assert adm.ok and C._sessions["t-1"] == "task"


@pytest.mark.asyncio
async def test_on_the_4_gb_server_with_nothing_running_a_wake_starts(server):
    got = await C.reserve_background("w-1", timeout_s=0.5, superseded=lambda: False)
    assert got == "reserved"


@pytest.mark.asyncio
async def test_on_the_4_gb_server_a_task_starts_beside_one_resting_chat(server):
    _resting_chat("c-1")
    server.live_mb = 2_048 - 400
    C._live_cache = None
    adm = await asyncio.wait_for(C.acquire("t-1", "task", blocking=True), 1.0)
    assert adm.ok


@pytest.mark.asyncio
async def test_on_the_4_gb_server_a_task_starts_beside_a_resting_chat_of_500_mb(server):
    _resting_chat("c-1")
    server.live_mb = 2_048 - 500    # a chat with two tools that start with it
    C._live_cache = None
    adm = await asyncio.wait_for(C.acquire("t-1", "task", blocking=True), 1.0)
    assert adm.ok


@pytest.mark.asyncio
async def test_on_the_4_gb_server_a_task_waits_beside_two_resting_chats(server):
    _resting_chat("c-1")
    _resting_chat("c-2")
    server.live_mb = 2_048 - 800
    C._live_cache = None
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(C.acquire("t-1", "task", blocking=True), 0.3)
    assert "t-1" not in C._sessions and C._parked_tasks == 0


@pytest.mark.asyncio
async def test_on_the_4_gb_server_two_chats_start_at_once(server, monkeypatch):
    async def _known(_sid, _path, _mcp):
        return 500    # a chat's counted config: the engine and its tools
    monkeypatch.setattr(C, "_session_estimate", _known)
    for sid in ("c-1", "c-2"):
        assert (await C.acquire(sid, "chat", execution_path="claude-code-cli")).ok
    # The second reads the first's grow-in debit: 2,048 − 500 − 500 ≥ 512.
    assert len(C._sessions) == 2
