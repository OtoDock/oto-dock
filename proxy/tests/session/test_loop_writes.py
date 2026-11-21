"""A slow disk never holds the loop on a session's close (a close once
rewrote the security index on the loop, and every page waited for the disk).

The removal of a closed session's context is in memory at once and on disk
before the close returns; while the disk takes its time the loop answers
everyone else. The write is held here on an event the test controls, standing
in for the stalled disk.

The tests under "the guard on the close paths" use only names that existed
before the fix, so they show it failing there first.

Run: cd proxy && venv/bin/pytest tests/session/test_loop_writes.py -v
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from auth.path_policy import SecurityContext
from core import placement
from core.session import session_state

_MACHINE = placement.PlacementCapabilities(
    kind=placement.KIND_COMPUTE, label="compute", agents_dir="/agents", machine_id="m1",
)


def _ctx() -> SecurityContext:
    return SecurityContext(role="manager", username="alice", agent="demo",
                           is_admin_agent=False, placement=_MACHINE)


@pytest.fixture
def index(tmp_path, monkeypatch):
    """The security index under the test's own folder, the dicts emptied; the
    module's own writer, idle."""
    path = tmp_path / "security_index.json"
    monkeypatch.setattr(session_state, "_SECURITY_INDEX", path)
    monkeypatch.setattr(session_state, "_session_security", {})
    monkeypatch.setattr(session_state, "_session_security_ts", {})
    writer = getattr(session_state, "_security_writer", None)
    if writer is not None:
        writer.reset_for_tests()
    session_state.reset_liveness_for_tests()
    yield path
    session_state.reset_liveness_for_tests()
    if writer is not None:
        writer.reset_for_tests()


def _on_disk(path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


async def _until(cond, limit_s: float = 10.0) -> None:
    deadline = time.monotonic() + limit_s
    while not cond():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.01)


def _remote_layer():
    from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
    cm = MagicMock()
    cm.send_command = AsyncMock()
    layer = RemoteExecutionLayer(cm)

    def add(sid: str) -> None:
        layer._sessions[sid] = RemoteSessionInfo(
            session_id=sid, machine_id="m1", agent_name="demo",
            execution_path="claude-code-cli", event_queue=asyncio.Queue())
    return layer, add


class _Tool:
    async def close(self):
        pass


# ── the guard on the close paths (names that existed before the fix) ────────


def test_a_remote_close_writes_nothing_on_the_loop(index, loop_disk_guard):
    layer, add = _remote_layer()
    sid = str(uuid.uuid4())

    async def scenario():
        session_state.register_session_state(sid, "default", _ctx())
        await _until(lambda: sid in _on_disk(index))
        add(sid)
        with loop_disk_guard.active():
            await layer.close_session(sid)
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def test_an_app_buttons_tool_manager_close_writes_nothing_on_the_loop(index, loop_disk_guard):
    from services.apps import headless_exec as hx
    sid = f"appx-{uuid.uuid4().hex[:12]}"

    async def scenario():
        session_state.register_session_state(sid, "default", _ctx())
        await _until(lambda: sid in _on_disk(index))
        session_state.mark_headless_live(sid)
        with loop_disk_guard.active():
            await hx.close_manager(hx._Entry(_Tool(), sid, "", frozenset()))
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def test_a_start_and_a_mode_change_write_nothing_on_the_loop(index, loop_disk_guard):
    sid = str(uuid.uuid4())

    async def scenario():
        with loop_disk_guard.active():
            session_state.register_session_state(sid, "default", _ctx())
            session_state.set_session_mode(sid, "plan")
        await _until(lambda: _on_disk(index).get(sid, {}).get("_mode") == "plan")

    asyncio.run(scenario())


# ── the proof: the loop runs while the disk holds the close's write ──────────


class _HeldDisk:
    """The security index's writes held until ``release``."""

    def __init__(self, monkeypatch, path) -> None:
        from core import file_commit
        self.release = threading.Event()
        self.entered = threading.Event()
        real = file_commit.atomic_replace

        def held(p, blob, *, fsync):
            if p == path:
                self.entered.set()
                assert self.release.wait(10), "the test never released the disk"
            return real(p, blob, fsync=fsync)

        monkeypatch.setattr(file_commit, "atomic_replace", held)

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 5), "no write reached the disk"


async def _ticker(counter: list[int], stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(0.005)
        counter[0] += 1


async def _registered(index, *sids: str) -> None:
    for sid in sids:
        session_state.register_session_state(sid, "default", _ctx())
    assert await session_state.security_index_written()
    assert all(sid in _on_disk(index) for sid in sids)


def test_the_removal_leaves_for_the_disk_inside_the_cleanup(index):
    """No timer and no later turn of the loop: the write is handed to the
    file's thread before ``cleanup_session_permission_state`` returns."""
    sid = str(uuid.uuid4())

    async def scenario():
        await _registered(index, sid)
        session_state.cleanup_session_permission_state(sid)
        writer = session_state._security_writer
        assert writer._inflight is not None and writer._handle is None
        assert await session_state.security_index_written()

    asyncio.run(scenario())
    assert sid not in _on_disk(index)


def test_a_remote_close_waits_for_its_removal_without_holding_the_loop(index, monkeypatch):
    from app import app
    layer, add = _remote_layer()
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        await _registered(index, sid, other)
        add(sid)
        disk = _HeldDisk(monkeypatch, index)
        ticks, stop = [0], asyncio.Event()
        ticker = asyncio.ensure_future(_ticker(ticks, stop))

        close = asyncio.ensure_future(layer.close_session(sid))
        await disk.wait_entered()
        # The removal is in memory at once: the gate already finds no context.
        assert session_state.get_session_security(sid) is None
        assert session_state.get_session_security(other) is not None
        # The loop answers a request meanwhile, and keeps ticking.
        before = ticks[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        await asyncio.sleep(0.2)
        assert ticks[0] - before > 10
        # The close is not done while its removal is not on disk.
        assert not close.done()
        assert sid in _on_disk(index)
        disk.release.set()
        await asyncio.wait_for(close, 5)
        on_disk = _on_disk(index)              # read the moment the close returned
        stop.set()
        await ticker
        return on_disk

    on_disk = asyncio.run(scenario())
    assert sid not in on_disk and other in on_disk


def test_an_app_buttons_tool_manager_close_waits_for_its_removal(index, monkeypatch):
    from services.apps import headless_exec as hx
    sid = f"appx-{uuid.uuid4().hex[:12]}"

    async def scenario():
        await _registered(index, sid)
        session_state.mark_headless_live(sid)
        disk = _HeldDisk(monkeypatch, index)
        close = asyncio.ensure_future(hx.close_manager(hx._Entry(_Tool(), sid, "", frozenset())))
        await disk.wait_entered()
        assert session_state.get_session_security(sid) is None
        await asyncio.sleep(0.1)
        assert not close.done()
        disk.release.set()
        await asyncio.wait_for(close, 5)
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def test_a_remote_close_tells_the_machine_before_it_forgets(index):
    """The machine hears first, as the recovery's declines: it ends the process
    on the word, and only then does the close forget the session and wait for
    the disk. The reaper's case rests on this order."""
    from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
    sid = str(uuid.uuid4())
    seen: list[bool] = []
    cm = MagicMock()

    async def send_command(machine_id, frame, timeout=None):
        if frame.get("type") == "close_session":
            seen.append(session_state.get_session_security(sid) is not None)

    cm.send_command = send_command
    layer = RemoteExecutionLayer(cm)

    async def scenario():
        await _registered(index, sid)
        layer._sessions[sid] = RemoteSessionInfo(
            session_id=sid, machine_id="m1", agent_name="demo",
            execution_path="claude-code-cli", event_queue=asyncio.Queue())
        await layer.close_session(sid)

    asyncio.run(scenario())
    assert seen == [True]
    assert session_state.get_session_security(sid) is None


def test_a_close_behind_a_held_write_rides_the_next_one(index, monkeypatch):
    """Two closes while the disk holds the first one's write: the second's
    removal is written once the first write ends, and both closes return only
    then."""
    layer, add = _remote_layer()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        await _registered(index, a, b)
        add(a)
        add(b)
        disk = _HeldDisk(monkeypatch, index)
        first = asyncio.ensure_future(layer.close_session(a))
        await disk.wait_entered()
        second = asyncio.ensure_future(layer.close_session(b))
        await asyncio.sleep(0.1)
        assert session_state.get_session_security(b) is None
        assert not first.done() and not second.done()
        disk.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        return _on_disk(index)

    on_disk = asyncio.run(scenario())
    assert a not in on_disk and b not in on_disk


def test_a_disk_that_never_answers_lets_the_close_go_on_after_its_bound(index, monkeypatch, caplog):
    layer, add = _remote_layer()
    sid = str(uuid.uuid4())
    monkeypatch.setattr(session_state, "SECURITY_WRITE_WAIT_S", 0.2)

    async def scenario():
        await _registered(index, sid)
        add(sid)
        disk = _HeldDisk(monkeypatch, index)
        await asyncio.wait_for(layer.close_session(sid), 5)
        assert session_state.get_session_security(sid) is None
        assert sid in _on_disk(index)          # still held: lands when the disk answers
        disk.release.set()
        assert await session_state.security_index_written()
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())
    assert "security-index" in caplog.text


def test_a_failed_remote_start_waits_for_its_removal(index, monkeypatch):
    """The machine may run the process of a start that failed after it
    registered: the removal is on disk before the failure is told."""
    from core.remote.remote_execution import RemoteExecutionLayer
    cm = MagicMock()
    layer = RemoteExecutionLayer(cm)
    sid = str(uuid.uuid4())

    async def failing_impl(session_id, config, registered):
        session_state.register_session_state(session_id, "default", _ctx())
        registered.append(True)
        assert await session_state.security_index_written()
        raise RuntimeError("the machine refused")

    monkeypatch.setattr(layer, "_start_session_impl", failing_impl)

    async def scenario():
        disk = _HeldDisk(monkeypatch, index)
        start = asyncio.ensure_future(layer.start_session(sid, MagicMock()))
        await disk.wait_entered()
        await asyncio.sleep(0.1)
        assert not start.done()
        disk.release.set()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(start, 5)
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def test_a_cancelled_remote_start_never_waits_for_the_disk(index, monkeypatch):
    """A cancel is the platform or a person stopping the start: it ends at once,
    with the context gone from memory, whatever the disk does."""
    from core.remote.remote_execution import RemoteExecutionLayer
    layer = RemoteExecutionLayer(MagicMock())
    sid = str(uuid.uuid4())
    started = asyncio.Event()

    async def hanging_impl(session_id, config, registered):
        session_state.register_session_state(session_id, "default", _ctx())
        registered.append(True)
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(layer, "_start_session_impl", hanging_impl)

    async def scenario():
        disk = _HeldDisk(monkeypatch, index)
        start = asyncio.ensure_future(layer.start_session(sid, MagicMock()))
        await started.wait()
        await disk.wait_entered()               # the registration's write, held
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, 1)
        assert session_state.get_session_security(sid) is None
        disk.release.set()
        assert await session_state.security_index_written()
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def _homes_thread(monkeypatch) -> list[str]:
    """The thread names the close's home removal runs on."""
    from core.sandbox import session_config_dir
    names: list[str] = []

    def recording(session_id):
        names.append(threading.current_thread().name)
        return False

    monkeypatch.setattr(session_config_dir, "close_session_home", recording)
    return names


def test_a_remote_close_removes_its_home_on_a_lane_of_its_own(index, monkeypatch):
    """Never on the default executor, which every to_thread of the server
    shares: a stalled disk with many closes would hold every route that hops."""
    layer, add = _remote_layer()
    sid = str(uuid.uuid4())
    names = _homes_thread(monkeypatch)

    async def scenario():
        await _registered(index, sid)
        add(sid)
        await layer.close_session(sid)

    asyncio.run(scenario())
    assert names and all(n.startswith("session-homes") for n in names), names


# ── a terminal's close ───────────────────────────────────────────────────────


@pytest.fixture
def terminals():
    from core import concurrency
    from core.session import interactive_session as isess
    concurrency.init()
    isess._lock = None
    isess._sessions.clear()
    real_live = concurrency._live_available_mb
    concurrency._live_available_mb = lambda: 32768
    yield isess
    concurrency._live_available_mb = real_live
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()


async def _terminal(isess, sid: str):
    from tests._terminal import register
    return await register(session_id=sid, chat_id=f"chat-{sid[:8]}", agent_name="demo")


def test_a_terminals_close_writes_nothing_on_the_loop(index, terminals, loop_disk_guard):
    sid = str(uuid.uuid4())

    async def scenario():
        term = await _terminal(terminals, sid)
        session_state.register_session_state(sid, "default", _ctx())
        await _until(lambda: sid in _on_disk(index))
        with loop_disk_guard.active():
            await term.close()
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def test_a_terminals_close_waits_for_its_removal(index, terminals, monkeypatch):
    sid = str(uuid.uuid4())

    async def scenario():
        term = await _terminal(terminals, sid)
        await _registered(index, sid)
        disk = _HeldDisk(monkeypatch, index)
        close = asyncio.ensure_future(term.close())
        await disk.wait_entered()
        assert session_state.get_session_security(sid) is None
        await asyncio.sleep(0.1)
        assert not close.done()
        disk.release.set()
        await asyncio.wait_for(close, 5)
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())


def test_a_terminals_close_removes_its_home_on_a_lane_of_its_own(index, terminals, monkeypatch):
    sid = str(uuid.uuid4())
    names = _homes_thread(monkeypatch)

    async def scenario():
        term = await _terminal(terminals, sid)
        await _registered(index, sid)
        await term.close()

    asyncio.run(scenario())
    assert names and all(n.startswith("session-homes") for n in names), names


def test_a_terminals_supersede_never_waits_for_the_disk(index, terminals, monkeypatch):
    """A supersede closes the old terminal under the registry's lock and
    registers the same id again at once: it never waits for the disk, so a
    stalled disk never holds every terminal start of the install."""
    sid = str(uuid.uuid4())

    async def scenario():
        await _terminal(terminals, sid)
        await _registered(index, sid)
        disk = _HeldDisk(monkeypatch, index)
        again = asyncio.ensure_future(_terminal(terminals, sid))
        await disk.wait_entered()
        await asyncio.wait_for(again, 2)       # done while the disk still holds the write
        disk.release.set()
        await (await again).close()

    asyncio.run(scenario())


# ── one rule for which close waits: the context names a machine ──────────────


_PLATFORM = placement.PlacementCapabilities()


def test_a_forget_waits_for_the_disk_only_when_the_context_names_a_machine(index, monkeypatch):
    """A machine's report after a crash takes back only a session whose record
    the file kept: such a removal is on disk before the forget returns. The
    wait follows the context, not the engine: a context that names no machine
    never waits."""
    on_machine, on_platform = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        await _registered(index, on_machine)
        session_state.register_session_state(on_platform, "default", SecurityContext(
            role="manager", username="alice", agent="demo", is_admin_agent=False,
            placement=_PLATFORM))
        assert await session_state.security_index_written()
        disk = _HeldDisk(monkeypatch, index)
        await asyncio.wait_for(session_state.forget_session(on_platform), 1)
        await disk.wait_entered()
        forget = asyncio.ensure_future(session_state.forget_session(on_machine))
        await asyncio.sleep(0.1)
        assert session_state.get_session_security(on_machine) is None
        assert not forget.done()
        disk.release.set()
        await asyncio.wait_for(forget, 5)
        return _on_disk(index)

    on_disk = asyncio.run(scenario())
    assert on_machine not in on_disk and on_platform not in on_disk


def test_an_away_machines_session_leaves_the_file_before_its_person_is_told(index, monkeypatch):
    """The machine is away, so it cannot be told: the file's missing record is
    what refuses its report at its return. The person hears the account left
    only once the removal is on disk."""
    from services.agents import offboarding_sessions as closer
    from services.remote import chat_reload
    sid = str(uuid.uuid4())
    told: list[str] = []

    async def scenario():
        await _registered(index, sid)
        cm = MagicMock()
        cm.is_connected.return_value = False
        import core.remote.satellite_connection as sc
        monkeypatch.setattr(sc, "get_connection_manager", lambda: cm)
        live = closer.Live(sid, "demo", closer.POOL, "", "", "")

        async def to_close(session_ids, agent_name):
            return {sid: (live, {"id": "chat-away"})}

        async def close_sessions(victims, why, *, reason, revoked, after):
            for v in victims:
                await after(v)

        async def tell(lines):
            told.extend(lines)

        monkeypatch.setattr(chat_reload, "_sessions_to_close", to_close)
        monkeypatch.setattr(chat_reload, "_lines", lambda *a, **k: {"chat-away": "left"})
        monkeypatch.setattr(chat_reload, "_tell", tell)
        monkeypatch.setattr(closer, "close_sessions", close_sessions)
        disk = _HeldDisk(monkeypatch, index)
        leaving = chat_reload.Leaving(accounts=frozenset({("sub-1", "work")}))
        call = asyncio.ensure_future(chat_reload.account_left("tool", "demo", leaving))
        await disk.wait_entered()
        await asyncio.sleep(0.1)
        assert session_state.get_session_security(sid) is None
        assert told == [] and not call.done()
        disk.release.set()
        await asyncio.wait_for(call, 5)
        return _on_disk(index)

    assert sid not in asyncio.run(scenario())
    assert told == ["chat-away"]


def test_a_terminal_whose_context_names_no_machine_never_waits(index, terminals, monkeypatch):
    sid = str(uuid.uuid4())

    async def scenario():
        term = await _terminal(terminals, sid)
        session_state.register_session_state(sid, "default", SecurityContext(
            role="manager", username="alice", agent="demo", is_admin_agent=False,
            placement=_PLATFORM))
        assert await session_state.security_index_written()
        disk = _HeldDisk(monkeypatch, index)
        close = asyncio.ensure_future(term.close())
        await disk.wait_entered()
        await asyncio.wait_for(close, 1)        # done while the disk still holds the write
        disk.release.set()
        assert await session_state.security_index_written()

    asyncio.run(scenario())
