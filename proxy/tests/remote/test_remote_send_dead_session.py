"""Regression: a satellite that no longer has the session must route the
send into the dashboard's auto-resume path, not brick the chat.

Bug (2026-07-20 incident): a transient satellite WS drop/reconnect killed and
dropped in-flight sessions, but the proxy kept a stale, connected-looking
``RemoteSessionInfo`` — so ``is_session_process_dead`` read False and let the
next user message through to ``send_message``. The satellite answered the
send with an ACK error ``"Session not found"`` (session gone from its
registry), which the proxy surfaced verbatim as
``"Satellite command error: Session not found"`` WITHOUT setting
``info.cli_dead``. Every resend repeated it: the chat was permanently bricked
with no auto-resume.

The dead-subprocess sibling error ``"CLI process not running"`` was already
mapped to ``cli_dead`` (→ auto-resume from the on-disk transcript). The fix
maps the missing-session error the same way; these tests pin both, plus a
guard that an UNRELATED send error (e.g. a command timeout) does NOT falsely
flag the session dead — that would rotate a recoverable session to a fresh
spawn on a mere network hiccup.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.events.common_events import DONE, ERROR
from core.remote import remote_execution as re_mod


def _layer_with_live_session(send_error: Exception | None):
    """A RemoteExecutionLayer whose one CLI session's satellite send raises
    ``send_error`` (or succeeds when None). Mirrors the harness in
    test_upload_inflight.test_send_message_dispatches_only_after_push_lands.
    """
    cm = MagicMock()
    cm.wait_abort_acked = AsyncMock(return_value=True)
    if send_error is not None:
        cm.send_command = AsyncMock(side_effect=send_error)
    else:
        cm.send_command = AsyncMock()

    layer = re_mod.RemoteExecutionLayer(cm)
    info = re_mod.RemoteSessionInfo(
        session_id="sess-1",
        machine_id="m-1",
        agent_name="agent-x",
        execution_path="claude-code-cli",
        event_queue=asyncio.Queue(),
    )
    layer._sessions["sess-1"] = info
    return layer, info


async def _drive(layer):
    return [e async for e in layer.send_message("sess-1", "did you finish?")]


@pytest.mark.asyncio
async def test_session_not_found_flags_cli_dead_for_autoresume():
    """The fix: the satellite's 'Session not found' ACK error sets cli_dead,
    so the next turn's is_session_process_dead reports dead → auto-resume."""
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite command error: Session not found")
    )

    events = await _drive(layer)

    assert info.cli_dead is True
    types = [e.type for e in events]
    assert types == [ERROR, DONE]
    assert "Session not found" in events[0].data["message"]


@pytest.mark.asyncio
async def test_cli_process_not_running_still_flags_cli_dead():
    """The pre-existing dead-subprocess mapping must keep working."""
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite command error: CLI process not running")
    )

    events = await _drive(layer)

    assert info.cli_dead is True
    assert [e.type for e in events] == [ERROR, DONE]


@pytest.mark.asyncio
async def test_unrelated_command_error_does_not_flag_cli_dead():
    """A transient/unrelated send failure must NOT flag the session dead —
    else a network hiccup rotates a recoverable session to a fresh spawn."""
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite m-1 command timeout (30s)")
    )

    events = await _drive(layer)

    assert info.cli_dead is False
    assert [e.type for e in events] == [ERROR, DONE]
