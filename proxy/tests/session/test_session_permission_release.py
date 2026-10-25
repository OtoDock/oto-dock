"""Session-scoped permission release.

Under the warm app-server model the Codex daemon, its MCP subprocesses and the
stdio interceptor all SURVIVE ``turn/interrupt`` — so unlike the old per-turn
``codex exec`` model, an abort no longer incidentally drops a pending permission
wait (which would otherwise hang to its 7-day timeout and hold the interceptor
pipe). These tests cover the explicit release primitive that fixes it:

  * ``wait_for_permission(rid, session_id)`` indexes the request under the
    session (``_session_permission_requests``) and de-indexes it on completion.
  * ``resolve_session_permissions(session_id, approved=False)`` denies every
    pending waiter for the session and clears the index.
  * ``cleanup_session_permission_state(session_id)`` releases pending waiters.
  * A waiter for a *different* session is never touched.
  * No leak: the index / event maps are empty after release.

Pure-asyncio against ``core.session.session_state`` — no DB, so this file is immune to
the conftest DB-pool flake.
"""

import asyncio
import sys

import pytest

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core.session.session_state import (
    _permission_decisions,
    _permission_events,
    _permission_request_sessions,
    _question_answers,
    _question_events,
    _session_permission_requests,
    cleanup_session_permission_state,
    resolve_permission,
    resolve_question,
    resolve_session_permissions,
    wait_for_permission,
    wait_for_question,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with empty permission maps."""
    for m in (_permission_events, _permission_decisions, _session_permission_requests,
              _permission_request_sessions, _question_events, _question_answers):
        m.clear()
    yield
    for m in (_permission_events, _permission_decisions, _session_permission_requests,
              _permission_request_sessions, _question_events, _question_answers):
        m.clear()


async def _spawn_waiter(request_id: str, session_id: str) -> asyncio.Task:
    """Start a wait_for_permission task and let it register before returning."""
    task = asyncio.create_task(
        wait_for_permission(request_id, session_id, timeout=30.0)
    )
    # Yield so the coroutine runs up to `await event.wait()` and registers.
    for _ in range(5):
        await asyncio.sleep(0)
        if request_id in _permission_events:
            break
    return task


# ─────────────────────────────────────────────────────────────────────────
# Index registration + de-registration
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_waiter_registers_under_session():
    task = await _spawn_waiter("rid-1", "sess-A")
    assert "rid-1" in _permission_events
    assert _session_permission_requests.get("sess-A") == {"rid-1"}
    # Resolve normally → index entry is cleaned up.
    assert resolve_permission("rid-1", True) is True
    assert await task is True
    assert "rid-1" not in _permission_events
    assert "sess-A" not in _session_permission_requests  # emptied set is popped


@pytest.mark.asyncio
async def test_no_session_id_skips_index():
    """Backwards-compatible call without a session_id still works, just unindexed."""
    task = await _spawn_waiter("rid-x", "")
    assert "rid-x" in _permission_events
    assert _session_permission_requests == {}
    resolve_permission("rid-x", False)
    assert await task is False


# ─────────────────────────────────────────────────────────────────────────
# resolve_session_permissions
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_denies_all_pending_for_session():
    t1 = await _spawn_waiter("rid-1", "sess-A")
    t2 = await _spawn_waiter("rid-2", "sess-A")
    t3 = await _spawn_waiter("rid-3", "sess-A")
    assert _session_permission_requests["sess-A"] == {"rid-1", "rid-2", "rid-3"}

    released = resolve_session_permissions("sess-A", approved=False)
    assert released == 3
    # All three waiters return DENY.
    assert await t1 is False
    assert await t2 is False
    assert await t3 is False
    # Index for the session is gone; no leaked events.
    assert "sess-A" not in _session_permission_requests
    assert _permission_events == {}


@pytest.mark.asyncio
async def test_release_does_not_touch_other_sessions():
    ta = await _spawn_waiter("rid-a", "sess-A")
    tb = await _spawn_waiter("rid-b", "sess-B")

    released = resolve_session_permissions("sess-A", approved=False)
    assert released == 1
    assert await ta is False
    # sess-B's waiter is still pending.
    assert not tb.done()
    assert _session_permission_requests.get("sess-B") == {"rid-b"}

    # Clean up sess-B explicitly.
    resolve_permission("rid-b", True)
    assert await tb is True


@pytest.mark.asyncio
async def test_release_empty_session_is_noop():
    assert resolve_session_permissions("ghost", approved=False) == 0


@pytest.mark.asyncio
async def test_release_can_approve():
    t1 = await _spawn_waiter("rid-1", "sess-A")
    released = resolve_session_permissions("sess-A", approved=True)
    assert released == 1
    assert await t1 is True


# ─────────────────────────────────────────────────────────────────────────
# cleanup_session_permission_state also releases
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_releases_pending():
    t1 = await _spawn_waiter("rid-1", "sess-A")
    t2 = await _spawn_waiter("rid-2", "sess-A")
    cleanup_session_permission_state("sess-A")
    assert await t1 is False
    assert await t2 is False
    assert "sess-A" not in _session_permission_requests


@pytest.mark.asyncio
async def test_concurrent_sessions_isolated_release():
    """Two sessions with pending prompts; releasing one leaves the other intact."""
    a1 = await _spawn_waiter("a1", "sess-A")
    a2 = await _spawn_waiter("a2", "sess-A")
    b1 = await _spawn_waiter("b1", "sess-B")

    assert resolve_session_permissions("sess-A") == 2
    assert await a1 is False
    assert await a2 is False
    assert not b1.done()
    assert _session_permission_requests == {"sess-B": {"b1"}}

    resolve_session_permissions("sess-B")
    assert await b1 is False
    assert _session_permission_requests == {}
    assert _permission_events == {}
    assert _permission_decisions == {}


@pytest.mark.asyncio
async def test_timeout_denies():
    """An unanswered gate fails CLOSED on timeout — never a silent approval."""
    approved = await wait_for_permission("rid-t", "sess-T", timeout=0.05)
    assert approved is False
    assert _permission_events == {}
    assert _session_permission_requests == {}


# ─────────────────────────────────────────────────────────────────────────
# request_user_input question waiters (share the session index)
# ─────────────────────────────────────────────────────────────────────────


async def _spawn_question_waiter(request_id: str, session_id: str) -> asyncio.Task:
    task = asyncio.create_task(wait_for_question(request_id, session_id, timeout=30.0))
    for _ in range(5):
        await asyncio.sleep(0)
        if request_id in _question_events:
            break
    return task


@pytest.mark.asyncio
async def test_question_resolves_with_answers_map():
    task = await _spawn_question_waiter("q-1", "sess-A")
    assert _session_permission_requests.get("sess-A") == {"q-1"}
    answers = {"color": {"answers": ["Dark"]}}
    assert resolve_question("q-1", answers) is True
    assert await task == answers
    # De-indexed on completion — no leak.
    assert "q-1" not in _question_events
    assert "q-1" not in _question_answers
    assert "sess-A" not in _session_permission_requests


@pytest.mark.asyncio
async def test_question_release_on_abort_returns_empty():
    """resolve_session_permissions releases a HELD question with empty answers so
    the codex turn unwinds instead of hanging to its 7-day timeout."""
    task = await _spawn_question_waiter("q-1", "sess-A")
    released = resolve_session_permissions("sess-A", approved=False)
    assert released == 1
    assert await task == {}
    assert _question_events == {}
    assert "sess-A" not in _session_permission_requests


@pytest.mark.asyncio
async def test_mixed_permission_and_question_release_together():
    """A session can hold BOTH a permission and a question at once (a Bash escape
    while a question is pending) — one abort releases both from the shared index."""
    perm = await _spawn_waiter("p-1", "sess-A")
    ques = await _spawn_question_waiter("q-1", "sess-A")
    assert _session_permission_requests["sess-A"] == {"p-1", "q-1"}
    released = resolve_session_permissions("sess-A", approved=False)
    assert released == 2
    assert await perm is False       # permission → deny
    assert await ques == {}          # question → empty answers
    assert _session_permission_requests == {}


@pytest.mark.asyncio
async def test_question_timeout_returns_empty():
    answers = await wait_for_question("q-t", "sess-T", timeout=0.05)
    assert answers == {}
    assert _question_events == {}
    assert _session_permission_requests == {}


# ─────────────────────────────────────────────────────────────────────────
# PersistentSession.close: a parked turn is interrupted and released first
# ─────────────────────────────────────────────────────────────────────────


class _CloseStdin:
    """Records the interrupt frames and, when closed, whether the session's
    waiters had been released by then; ``wedged`` never drains."""

    def __init__(self, proc: "_CloseProc", *, wedged: bool = False):
        self.proc = proc
        self.lines: list[dict] = []
        self.wedged = wedged
        self.closed_after_release: bool | None = None

    def write(self, data: bytes) -> None:
        import json
        self.lines.append(json.loads(data))

    async def drain(self) -> None:
        if self.wedged:
            await asyncio.Event().wait()

    def close(self) -> None:
        self.closed_after_release = not _session_permission_requests.get(self.proc.sid)
        self.proc.returncode = 0   # the CLI exits at its stdin's end
        self.proc.exited.set()


class _CloseProc:
    def __init__(self, sid: str, *, returncode: int | None = None, wedged: bool = False):
        self.sid = sid
        self.pid = 4343
        self.returncode = returncode
        self.exited = asyncio.Event()
        self.stdin = _CloseStdin(self, wedged=wedged)

    async def wait(self) -> int:
        await self.exited.wait()
        return self.returncode


@pytest.fixture
def parked(monkeypatch):
    """A session with a fake CLI; ``kills`` records the close's kill."""
    import types

    from core.layers.cli import session as cli_session_mod
    from core.layers.cli.session import PersistentSession

    kills: list[str] = []

    async def _kill(proc, sid):
        kills.append(sid)
    monkeypatch.setattr(cli_session_mod, "_kill_process", _kill)

    def _make(sid: str, **proc_kw) -> PersistentSession:
        s = PersistentSession(session_id=sid, agent_prompt=None, mcp_config_path=None,
                              model="claude-sonnet-5-5", agent_name="agent")
        s.proc = _CloseProc(sid, **proc_kw)
        s._started = True
        s._translator = types.SimpleNamespace(interrupted=False)
        return s
    _make.kills = kills
    return _make


def _interrupts(session) -> list[dict]:
    return [f for f in session.proc.stdin.lines
            if f.get("type") == "control_request" and f["request"]["subtype"] == "interrupt"]


@pytest.mark.asyncio
async def test_close_interrupts_a_parked_turn_and_releases_it_before_stdin_closes(parked):
    s = parked("sess-park")
    s._turn_active = True
    waiter = await _spawn_waiter("rid-card", "sess-park")
    await s.close()
    assert len(_interrupts(s)) == 1
    assert s._translator.interrupted is True   # the interrupted result ends quietly
    assert s.proc.stdin.closed_after_release is True
    assert await asyncio.wait_for(waiter, 1) is False
    assert parked.kills == []


@pytest.mark.asyncio
async def test_close_interrupts_for_the_engines_own_request_between_turns(parked):
    """Claude's own stdio request, read between our turns, holds the CLI's
    turn open and its release writes nothing back: the interrupt ends it."""
    from core.session.session_state import open_permission_wait
    s = parked("sess-engine")
    s._turn_active = False
    open_permission_wait("rid-engine", "sess-engine")
    await s.close()
    assert len(_interrupts(s)) == 1
    assert s.proc.stdin.closed_after_release is True
    assert "sess-engine" not in _session_permission_requests


@pytest.mark.asyncio
async def test_close_without_a_prompt_writes_nothing(parked):
    s = parked("sess-idle")
    s._turn_active = True
    await s.close()
    assert s.proc.stdin.lines == []
    assert s.proc.stdin.closed_after_release is True
    assert parked.kills == []


@pytest.mark.asyncio
async def test_close_of_a_dead_process_still_releases_its_waiter(parked):
    s = parked("sess-dead", returncode=1)
    waiter = await _spawn_waiter("rid-dead", "sess-dead")
    await s.close()
    assert s.proc.stdin.lines == []
    assert s.proc.stdin.closed_after_release is None   # stdin never touched
    assert await asyncio.wait_for(waiter, 1) is False


@pytest.mark.asyncio
async def test_a_wedged_pipe_does_not_hold_the_close(parked, monkeypatch):
    from core.layers.cli import session as cli_session_mod
    monkeypatch.setattr(cli_session_mod, "_CLOSE_INTERRUPT_S", 0.1)
    s = parked("sess-wedged", wedged=True)
    s._turn_active = True
    waiter = await _spawn_waiter("rid-wedged", "sess-wedged")
    await asyncio.wait_for(s.close(), timeout=2)
    assert await asyncio.wait_for(waiter, 1) is False
    assert s.proc.stdin.closed_after_release is True
    # Written but not drained: the frame may still reach the CLI, and its
    # result must read as the interrupt.
    assert s._translator.interrupted is True and s._post_interrupt_watch is True


@pytest.mark.asyncio
async def test_a_second_close_does_nothing(parked):
    s = parked("sess-twice")
    s._turn_active = True
    waiter = await _spawn_waiter("rid-twice", "sess-twice")
    await s.close()
    await s.close()
    assert len(_interrupts(s)) == 1
    assert await asyncio.wait_for(waiter, 1) is False
