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
  * The close of a session on a machine and a Stop of its turn release its
    waiters (``RemoteExecutionLayer.close_session``, ``.abort``).

The primitive's tests are pure asyncio against ``core.session.session_state``
(no DB); the last two close and stop a session through the remote layer.
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
    _session_permission_requests,
    cleanup_session_permission_state,
    resolve_permission,
    resolve_session_permissions,
    wait_for_permission,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with empty permission maps."""
    for m in (_permission_events, _permission_decisions, _session_permission_requests,
              _permission_request_sessions):
        m.clear()
    yield
    for m in (_permission_events, _permission_decisions, _session_permission_requests,
              _permission_request_sessions):
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
# A session on a machine: its close and a Stop release its parked card
# ─────────────────────────────────────────────────────────────────────────


def _on_a_machine(session_id: str = "sess-park"):
    """The remote layer holding one Claude Code session, and its record."""
    from unittest.mock import AsyncMock, MagicMock

    from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
    cm = MagicMock()
    cm.send_command = AsyncMock()
    cm.send_fire_and_forget = AsyncMock()
    layer = RemoteExecutionLayer(cm)
    info = RemoteSessionInfo(session_id=session_id, machine_id="m-1", agent_name="agent",
                             execution_path="claude-code-cli", event_queue=asyncio.Queue())
    layer._sessions[session_id] = info
    return layer, info


@pytest.mark.asyncio
@pytest.mark.parametrize("machine_answers", (True, False), ids=("closed", "machine-silent"))
async def test_the_close_of_a_session_on_a_machine_releases_its_parked_card(machine_answers):
    """The close denies the card the turn is parked on, so nothing waits out
    the card's own bound on a session that is gone, whether the machine
    took the close or never answered it."""
    layer, _info = _on_a_machine()
    if not machine_answers:
        layer._cm.send_command.side_effect = RuntimeError("the machine did not answer")
    parked = await _spawn_waiter("rid-card", "sess-park")
    other = await _spawn_waiter("rid-other", "sess-other")
    await layer.close_session("sess-park")
    assert layer._cm.send_command.await_args.args[1]["type"] == "close_session"
    assert await asyncio.wait_for(parked, 1) is False
    assert "sess-park" not in _session_permission_requests
    assert not other.done()   # another session's card still waits
    resolve_session_permissions("sess-other")
    assert await other is False


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_live", (True, False), ids=("soft-interrupt", "hard-abort"))
async def test_a_stop_of_a_session_on_a_machine_releases_its_parked_card(turn_live):
    """A Stop denies the card the turn is parked on, with the soft interrupt
    of a live turn and on the hard path alike: the turn it asked to close
    is not left waiting on a person."""
    layer, info = _on_a_machine()
    info.turn_active = turn_live
    info.current_send_command_id = "cmd-1"
    parked = await _spawn_waiter("rid-card", "sess-park")
    other = await _spawn_waiter("rid-other", "sess-other")
    assert await layer.abort("sess-park") is turn_live
    assert await asyncio.wait_for(parked, 1) is False
    assert [c.args[1]["type"] for c in layer._cm.send_fire_and_forget.await_args_list] == [
        "interrupt_turn" if turn_live else "abort"]
    assert not other.done()
    resolve_session_permissions("sess-other")
    assert await other is False
    # The turn closed: the watchdog the soft interrupt armed stands down.
    info.turn_active = False
    await asyncio.sleep(0.3)
