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

from core.session.session_state import (  # noqa: E402
    wait_for_permission,
    wait_for_question,
    resolve_permission,
    resolve_question,
    resolve_session_permissions,
    cleanup_session_permission_state,
    _permission_events,
    _permission_decisions,
    _session_permission_requests,
    _question_events,
    _question_answers,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with empty permission maps."""
    for m in (_permission_events, _permission_decisions, _session_permission_requests,
              _question_events, _question_answers):
        m.clear()
    yield
    for m in (_permission_events, _permission_decisions, _session_permission_requests,
              _question_events, _question_answers):
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
