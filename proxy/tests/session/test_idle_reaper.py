"""Idle-reaper safety around session startup (the 2026-09-01 task race).

A persistent session sits in the pool with ``_started=False`` between the
pool insert and the end of ``start()``. The reaper's dead-session check
(``not is_alive``) used to match that window and could kill a session
mid-birth — observed live on the internal VM when the 60s reaper tick
landed inside a task cron's session spawn ("Session not found" after
319ms). These tests pin the fixed semantics of ``_reap_idle_pass``:

* never reap an un-started session inside the startup grace window;
* still collect an un-started entry stranded past the grace (crashed
  ``start()`` with no cleanup);
* keep reaping dead-process and idle-timeout sessions;
* a failing ``start()`` in ``get_or_create_persistent_session`` removes the
  pool entry and releases the chat slot + subscription (the reaper can no
  longer see the entry, so the creator must clean up).
"""

import asyncio
import time
import uuid

import pytest

from core.layers.cli import session as cli_session
from core.layers.cli.session import (
    PersistentSession,
    _persistent_sessions,
    _reap_idle_pass,
    _STARTUP_REAP_GRACE_S,
    get_or_create_persistent_session,
)


class _FakeStdin:
    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = None
        self.stderr = None
        self.returncode: int | None = None
        self.pid = 4242

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _mk_session(*, started: bool, proc_alive: bool | None = None,
                created_ago_s: float = 0.0, idle_s: float = 0.0) -> PersistentSession:
    s = PersistentSession(
        session_id=f"sess-{uuid.uuid4().hex[:12]}",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-5",
        agent_name="agent",
    )
    s._started = started
    if proc_alive is not None:
        s.proc = _FakeProc()
        if not proc_alive:
            s.proc.returncode = 1
    now = time.monotonic()
    s._created = now - created_ago_s
    s.last_activity = now - idle_s
    return s


@pytest.fixture
def pool():
    """Isolated pool: snapshot + restore the module-global session dict."""
    saved = dict(_persistent_sessions)
    _persistent_sessions.clear()
    yield _persistent_sessions
    _persistent_sessions.clear()
    _persistent_sessions.update(saved)


class TestReapPass:
    @pytest.mark.asyncio
    async def test_starting_session_survives_the_pass(self, pool):
        s = _mk_session(started=False)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id in pool
        assert not s._closed

    @pytest.mark.asyncio
    async def test_stranded_unstarted_entry_reaped_past_grace(self, pool):
        s = _mk_session(started=False, created_ago_s=_STARTUP_REAP_GRACE_S + 1)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_dead_process_still_reaped(self, pool):
        s = _mk_session(started=True, proc_alive=False)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_idle_timeout_still_reaped(self, pool, monkeypatch):
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 10)
        s = _mk_session(started=True, proc_alive=True, idle_s=11)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_active_session_untouched(self, pool, monkeypatch):
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 10)
        s = _mk_session(started=True, proc_alive=True, idle_s=1)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id in pool
        assert not s._closed


class TestStartFailureCleanup:
    @pytest.mark.asyncio
    async def test_failed_start_pops_entry_and_releases(self, pool, monkeypatch):
        released = {"slot": None, "sub": None}

        async def boom(self):
            raise OSError("spawn failed")

        monkeypatch.setattr(PersistentSession, "start", boom)
        monkeypatch.setattr("core.concurrency.release_chat_slot",
                            lambda sid: released.__setitem__("slot", sid))
        monkeypatch.setattr("services.engines.subscription_pool.release_subscription",
                            lambda sid: released.__setitem__("sub", sid))

        sid = f"sess-{uuid.uuid4().hex[:12]}"
        with pytest.raises(OSError):
            await get_or_create_persistent_session(
                session_id=sid, agent_prompt=None, mcp_config_path=None,
                model="claude-opus-5", agent_name="agent",
            )
        assert sid not in pool
        assert released == {"slot": sid, "sub": sid}

    @pytest.mark.asyncio
    async def test_close_during_start_kills_fresh_process(self, monkeypatch):
        """The in-start guard: close() flipping _closed while the spawn is in
        flight makes start() kill the fresh process and raise, not leak it."""
        s = _mk_session(started=False)
        s._closed = True  # closed while spawn was in flight
        killed = []

        async def fake_exec(*a, **k):
            return _FakeProc()

        async def fake_kill(proc, sid):
            killed.append(sid)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cli_session, "_kill_process", fake_kill)
        monkeypatch.setattr(PersistentSession, "build_spawn_command",
                            lambda self: (["claude"], {}, "/tmp"))
        with pytest.raises(RuntimeError, match="closed during start"):
            await s.start()
        assert killed == [s.session_id]
        assert not s.is_alive
