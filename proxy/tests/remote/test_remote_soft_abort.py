"""Remote abort parity — graceful-first for both engines.

Local abort went graceful-first (control_request{interrupt}, process + MCPs
survive) while the remote path still hard-killed the satellite CLI subprocess
and set ``cli_dead`` — so the next prompt paid a full re-warm (the observed
>10s think delay on a Windows satellite). The remote layer now mirrors the
local seam: ``interrupt_turn`` frame for claude-code-cli on satellites ≥
0.5.89 with a live turn, watchdog escalation to the hard abort otherwise.

Codex followed on 2026-07-09: the satellite's codex twin was ALWAYS soft
(``turn/interrupt``, warm daemon) but the proxy still returned False —
cancelling the producer and re-injecting cancelled context, a semantic gap
vs the local graceful codex abort. The proxy now returns True (same wire
frame, ``abort``), keeps the producer consuming the closing turn's tail,
and arms the same watchdog. The ``session_aborted`` ack drain is gated on
an ARMED hard abort so it can't steal the tail from the live producer
(covered in test_satellite_abort_ack_drain.py).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote import remote_execution
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo


def _make_info(
    session_id: str = "sess-1",
    machine_id: str = "m-1",
    execution_path: str = "claude-code-cli",
    turn_active: bool = True,
) -> RemoteSessionInfo:
    translator = ClaudeCLIEventTranslator(session_id)
    info = RemoteSessionInfo(
        session_id=session_id,
        machine_id=machine_id,
        agent_name="agent-1",
        execution_path=execution_path,
        event_queue=asyncio.Queue(),
    )
    info.cli_translator = translator
    info.cli_settle = SettleController(session_id, 0, translator)
    info.turn_active = turn_active
    info.current_send_command_id = "cmd-1"
    return info


def _make_layer(*, soft_supported: bool = True) -> RemoteExecutionLayer:
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._cm.satellite_supports_soft_interrupt = MagicMock(
        return_value=soft_supported,
    )
    layer._cm.send_fire_and_forget = AsyncMock()
    layer._cm.drop_grace_session = MagicMock()
    layer._cm.arm_abort_acked = MagicMock()
    layer._sessions = {}
    return layer


def _sent_frame_types(layer) -> list[str]:
    return [
        c.args[1].get("type")
        for c in layer._cm.send_fire_and_forget.call_args_list
    ]


@pytest.mark.asyncio
async def test_soft_abort_graceful_for_cli_on_supported_satellite():
    layer = _make_layer(soft_supported=True)
    info = _make_info()
    layer._sessions[info.session_id] = info

    result = await layer.abort(info.session_id)

    assert result is True
    assert _sent_frame_types(layer) == ["interrupt_turn"]
    assert info.cli_dead is False           # process survives — no re-warm
    layer._cm.arm_abort_acked.assert_not_called()
    layer._cm.drop_grace_session.assert_not_called()
    # Let the armed watchdog observe the closed turn and stand down.
    info.turn_active = False
    await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_old_satellite_keeps_hard_abort():
    layer = _make_layer(soft_supported=False)
    info = _make_info()
    layer._sessions[info.session_id] = info

    result = await layer.abort(info.session_id)

    assert result is False
    assert _sent_frame_types(layer) == ["abort"]
    assert info.cli_dead is True
    layer._cm.arm_abort_acked.assert_called_once()
    layer._cm.drop_grace_session.assert_called_once()


@pytest.mark.asyncio
async def test_codex_abort_graceful_on_supported_satellite():
    """Codex mirrors the local graceful abort: same ``abort`` wire frame
    (the deployed satellites' interrupt_turn handler is CLI-only), but the
    proxy returns True — producer stays alive for the terminal turn event,
    no cancelled-context injection, and no ack-drain armed."""
    layer = _make_layer(soft_supported=True)
    info = _make_info(execution_path="codex-cli")
    layer._sessions[info.session_id] = info

    result = await layer.abort(info.session_id)

    assert result is True
    assert _sent_frame_types(layer) == ["abort"]
    assert info.cli_dead is False           # daemon stays warm
    # No armed ack → the satellite's unconditional session_aborted ack must
    # NOT drain the queue out from under the live producer.
    layer._cm.arm_abort_acked.assert_not_called()
    layer._cm.drop_grace_session.assert_not_called()
    # Let the armed watchdog observe the closed turn and stand down.
    info.turn_active = False
    await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_codex_abort_stays_hard_on_old_satellite():
    layer = _make_layer(soft_supported=False)
    info = _make_info(execution_path="codex-cli")
    layer._sessions[info.session_id] = info

    result = await layer.abort(info.session_id)

    assert result is False
    assert _sent_frame_types(layer) == ["abort"]
    # The codex daemon soft-interrupts its turn and stays alive even on the
    # hard path — only claude-code-cli flags the process dead.
    assert info.cli_dead is False
    layer._cm.arm_abort_acked.assert_called_once()


@pytest.mark.asyncio
async def test_soft_abort_requires_live_turn():
    layer = _make_layer(soft_supported=True)
    info = _make_info(turn_active=False)
    layer._sessions[info.session_id] = info

    result = await layer.abort(info.session_id)

    assert result is False
    assert _sent_frame_types(layer) == ["abort"]
    assert info.cli_dead is True


@pytest.mark.asyncio
async def test_watchdog_escalates_when_turn_never_closes(monkeypatch):
    monkeypatch.setattr(remote_execution, "_REMOTE_INTERRUPT_WATCHDOG_S", 0.3)
    layer = _make_layer(soft_supported=True)
    info = _make_info()
    layer._sessions[info.session_id] = info

    flags: list = []
    fake_db = MagicMock()
    fake_db.get_chat_by_session = MagicMock(
        return_value={"id": "chat-1", "last_turn_aborted": True},
    )
    fake_db.update_chat = MagicMock(
        side_effect=lambda cid, **kw: flags.append(kw),
    )
    monkeypatch.setattr("storage.database.get_chat_by_session",
                        fake_db.get_chat_by_session)
    monkeypatch.setattr("storage.database.update_chat", fake_db.update_chat)

    assert await layer.abort(info.session_id) is True
    # Turn never closes → the watchdog fires the hard abort.
    await asyncio.sleep(1.0)
    assert _sent_frame_types(layer) == ["interrupt_turn", "abort"]
    assert info.cli_dead is True
    # The ws site stamped graceful=True optimistically — flipped back.
    assert flags == [{"last_abort_graceful": False}]


@pytest.mark.asyncio
async def test_codex_watchdog_escalates_when_turn_never_closes(monkeypatch):
    """Codex escalation: a second ``abort`` frame rides the hard path (arm +
    grace-drop + ack drain), the daemon still isn't flagged dead, and the
    optimistic graceful flag is flipped back so the next turn re-injects the
    cancelled context."""
    monkeypatch.setattr(remote_execution, "_REMOTE_INTERRUPT_WATCHDOG_S", 0.3)
    layer = _make_layer(soft_supported=True)
    info = _make_info(execution_path="codex-cli")
    layer._sessions[info.session_id] = info

    flags: list = []
    fake_db = MagicMock()
    fake_db.get_chat_by_session = MagicMock(
        return_value={"id": "chat-1", "last_turn_aborted": True},
    )
    fake_db.update_chat = MagicMock(
        side_effect=lambda cid, **kw: flags.append(kw),
    )
    monkeypatch.setattr("storage.database.get_chat_by_session",
                        fake_db.get_chat_by_session)
    monkeypatch.setattr("storage.database.update_chat", fake_db.update_chat)

    assert await layer.abort(info.session_id) is True
    await asyncio.sleep(1.0)
    assert _sent_frame_types(layer) == ["abort", "abort"]
    assert info.cli_dead is False
    layer._cm.arm_abort_acked.assert_called_once()
    assert flags == [{"last_abort_graceful": False}]


@pytest.mark.asyncio
async def test_watchdog_stands_down_when_turn_closes(monkeypatch):
    monkeypatch.setattr(remote_execution, "_REMOTE_INTERRUPT_WATCHDOG_S", 0.3)
    layer = _make_layer(soft_supported=True)
    info = _make_info()
    layer._sessions[info.session_id] = info

    assert await layer.abort(info.session_id) is True
    info.turn_active = False  # the CLI closed the turn gracefully
    await asyncio.sleep(0.6)
    assert _sent_frame_types(layer) == ["interrupt_turn"]
    assert info.cli_dead is False


@pytest.mark.asyncio
async def test_watchdog_pins_to_the_interrupted_turn(monkeypatch):
    """A successor turn (new command id) must never be killed by a stale
    watchdog even if it is active at the deadline."""
    monkeypatch.setattr(remote_execution, "_REMOTE_INTERRUPT_WATCHDOG_S", 0.3)
    layer = _make_layer(soft_supported=True)
    info = _make_info()
    layer._sessions[info.session_id] = info

    assert await layer.abort(info.session_id) is True
    info.current_send_command_id = "cmd-2"  # next turn started
    await asyncio.sleep(0.6)
    assert _sent_frame_types(layer) == ["interrupt_turn"]
    assert info.cli_dead is False


# ---------------------------------------------------------------------------
# interrupt_for_queued — stop-and-send: the graceful arm ONLY, send-then-
# resolve, no watchdog, no hard fallback.
# ---------------------------------------------------------------------------

def _released(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        remote_execution, "resolve_session_permissions",
        lambda sid, approved: calls.append((sid, approved)),
    )
    return calls


@pytest.mark.asyncio
async def test_interrupt_for_queued_fires_graceful_only(monkeypatch):
    layer = _make_layer(soft_supported=True)
    layer._cm.is_connected = MagicMock(return_value=True)
    info = _make_info()
    layer._sessions[info.session_id] = info
    released = _released(monkeypatch)

    assert await layer.interrupt_for_queued(info.session_id) is True
    assert _sent_frame_types(layer) == ["interrupt_turn"]
    # Send-THEN-resolve: permissions denied only after a successful send.
    assert released == [(info.session_id, False)]
    # No hard-abort machinery touched, process bookkeeping untouched.
    assert info.cli_dead is False
    layer._cm.arm_abort_acked.assert_not_called()
    layer._cm.drop_grace_session.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_for_queued_gates_return_false(monkeypatch):
    released_total: list = []
    monkeypatch.setattr(
        remote_execution, "resolve_session_permissions",
        lambda sid, approved: released_total.append(sid),
    )

    # No live turn.
    layer = _make_layer(soft_supported=True)
    layer._cm.is_connected = MagicMock(return_value=True)
    info = _make_info(turn_active=False)
    layer._sessions[info.session_id] = info
    assert await layer.interrupt_for_queued(info.session_id) is False

    # Codex sessions take steer, never this path.
    layer2 = _make_layer(soft_supported=True)
    layer2._cm.is_connected = MagicMock(return_value=True)
    info2 = _make_info(session_id="sess-2", execution_path="codex-cli")
    layer2._sessions[info2.session_id] = info2
    assert await layer2.interrupt_for_queued(info2.session_id) is False

    # Old satellite (no soft interrupt support).
    layer3 = _make_layer(soft_supported=False)
    layer3._cm.is_connected = MagicMock(return_value=True)
    info3 = _make_info(session_id="sess-3")
    layer3._sessions[info3.session_id] = info3
    assert await layer3.interrupt_for_queued(info3.session_id) is False

    # Disconnected machine — send_fire_and_forget would silently no-op, so
    # the gate must refuse rather than claim an interrupt landed.
    layer4 = _make_layer(soft_supported=True)
    layer4._cm.is_connected = MagicMock(return_value=False)
    info4 = _make_info(session_id="sess-4")
    layer4._sessions[info4.session_id] = info4
    assert await layer4.interrupt_for_queued(info4.session_id) is False

    # Unknown session.
    assert await layer4.interrupt_for_queued("no-such") is False

    for lyr in (layer, layer2, layer3, layer4):
        assert _sent_frame_types(lyr) == []
    # No blind denials on any refused path.
    assert released_total == []


@pytest.mark.asyncio
async def test_interrupt_for_queued_send_failure_never_denies_blind(monkeypatch):
    layer = _make_layer(soft_supported=True)
    layer._cm.is_connected = MagicMock(return_value=True)
    layer._cm.send_fire_and_forget = AsyncMock(side_effect=RuntimeError("ws died"))
    info = _make_info()
    layer._sessions[info.session_id] = info
    released = _released(monkeypatch)

    assert await layer.interrupt_for_queued(info.session_id) is False
    # A failed send must NOT un-park the turn by denying its permission.
    assert released == []
