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


def _layer_with_live_session(send_error: Exception | None,
                             execution_path: str = "claude-code-cli"):
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
        execution_path=execution_path,
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
    from core.events import turn_ending
    ending = turn_ending.from_dict(events[0].data["ending"])
    assert ending.reason == turn_ending.LOST and "Session not found" in ending.detail


@pytest.mark.asyncio
async def test_cli_process_not_running_still_flags_cli_dead():
    """The pre-existing dead-subprocess mapping must keep working."""
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite command error: CLI process not running")
    )

    events = await _drive(layer)

    assert info.cli_dead is True
    assert [e.type for e in events] == [ERROR, DONE]


def _ending_of(events):
    from core.events import turn_ending
    return turn_ending.from_dict(events[0].data["ending"])


@pytest.mark.asyncio
async def test_a_dead_process_after_the_proxys_own_kill_is_the_stopped_ending():
    """The proxy's abort ended the CLI (a Stop, its watchdog) and a message
    reaches it afterwards (a review turn, a queued message): the machine was
    never lost. The 2026-10-13 finding read it as 'machine lost' and the
    delegating agent was told to wait for the machine."""
    from core.events import turn_ending
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite command error: CLI process not running")
    )
    info.proxy_killed = True

    events = await _drive(layer)

    assert [e.type for e in events] == [ERROR, DONE]
    ending = _ending_of(events)
    assert ending.reason == turn_ending.STOPPED and "CLI process not running" in ending.detail
    assert info.cli_dead is True
    assert info.proxy_killed is False  # the next turn's own clock


@pytest.mark.asyncio
async def test_a_missing_session_after_the_proxys_own_kill_stays_lost():
    """The satellite's abort keeps the session object: a missing one means a
    reconnect dropped it, never our kill."""
    from core.events import turn_ending
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite command error: Session not found")
    )
    info.proxy_killed = True

    events = await _drive(layer)

    assert _ending_of(events).reason == turn_ending.LOST
    assert info.cli_dead is True


@pytest.mark.asyncio
async def test_a_codex_daemon_never_reads_as_stopped(monkeypatch):
    """A Codex abort soft-interrupts a daemon that stays warm: the kill flag
    stays set on a live process, so a later dead-process error is not its
    doing."""
    from core.events import turn_ending
    layer, info = _layer_with_live_session(
        RuntimeError("Satellite command error: CLI process not running"),
        execution_path="codex-cli",
    )
    info.proxy_killed = True
    # The send fails before the turn streams: the adapter's per-turn state
    # (a router this harness never started) is not needed.
    monkeypatch.setattr(type(layer._adapter(info)), "begin_turn", AsyncMock(return_value=None))

    events = await _drive(layer)

    assert _ending_of(events).reason == turn_ending.LOST


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
