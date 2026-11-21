"""A meeting participant's session: the layer it runs on, and getting it
ready for a turn.

The orchestrator starts each participant on the layer its built config
names and records it here, so every turn, monitor and close goes to the
layer the session was started on. Before a turn a session whose machine is
reconnecting is waited for, and one that is gone (its machine came back
without it between two turns, the machine's memory guard stopped its
engine) is started again under the same id, as a call's is. A participant
the memory guard stopped keeps its seat and gets the meeting's own line.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from auth import roles
from core import placement
from core.events.common_events import SYSTEM, CommonEvent
from core.session import session_kind
from core.session.session_manager import (
    _get_remote_layer,
    find_layer_for_session,
    get_execution_layer,
    live_session,
)
from core.session.session_state import _save_sessions, _sessions
from services.meetings.meeting_context import build_meeting_agent_config
from storage.agents import agent_store
from storage.pg import run_db
from ws import wire_events as wire

if TYPE_CHECKING:
    from core.execution_layer import ExecutionLayer

logger = logging.getLogger("otodock")


# Per-meeting-session execution layer, captured at session-create so every
# turn and close reuses the SAME layer the session was started on. A bare
# get_execution_layer at those sites would re-resolve WITHOUT the meeting
# creator's user_sub/role and, for a user-override meeting, return the wrong
# layer (sending/closing on local while the session lives on the user's box).
_meeting_session_layers: dict[str, ExecutionLayer] = {}


def _session_layer(session_id: str, agent_slug: str) -> ExecutionLayer:
    """The layer a participant's turn, monitor or close goes to: the one its
    session was started on, else the layer that holds the session (a session
    on a compute machine is never asked of the proxy's own layer), else the
    agent's own, where a close of an id nobody holds does nothing. An agent
    of the platform's place with nobody holding its session reads as the
    remote layer: the session is not alive there, and a turn's restart is
    refused by its build."""
    held = _meeting_session_layers.get(session_id) or find_layer_for_session(session_id)
    if held is not None:
        return held
    try:
        return get_execution_layer(agent_slug)
    except placement.NoComputeMachine:
        return _get_remote_layer()


async def _wait_out_a_reconnect(session_id: str) -> None:
    """A participant's turn start: a session whose machine is reconnecting
    is waited for first, as a chat's and a call's turn are, at most
    ``RECONNECT_WAIT_S``. One still away fails its turn at the link."""
    live = live_session(session_id)
    if live is not None and live.grace:
        await live.layer.wait_session_reconnect(session_id)


def stopped_for_memory_line(agent_slug: str) -> str:
    """What the meeting says about a participant whose engine the machine's
    memory guard stopped: the agent by its name, and that it is not gone."""
    name = (agent_store.get_agent(agent_slug) or {}).get("display_name") or agent_slug
    return f"{name} used too much memory, so its engine was stopped. It answers again at its next turn."


async def _stopped_for_memory_event(agent_slug: str) -> CommonEvent:
    """The meeting's own line for a participant the memory guard stopped:
    never the chat's card for that ending, and never a departure."""
    return CommonEvent(type=SYSTEM, data={
        "subtype": wire.SUBTYPE_MEETING_AGENT_STOPPED, "agent": agent_slug,
        "message": await run_db(stopped_for_memory_line, agent_slug),
    })


async def _ready_for_turn(meeting: dict, agent_slug: str, session_id: str) -> ExecutionLayer:
    """A participant's turn start, under its session's lock: a machine that
    is reconnecting is waited for, and a session that is gone (its machine
    came back without it between two turns, the memory guard stopped its
    engine) is started again under the same id, as a call's is before its
    turn. The layer the turn runs on. One still reconnecting after the wait
    is left alone: its turn fails at the link."""
    await _wait_out_a_reconnect(session_id)
    layer = _session_layer(session_id, agent_slug)
    live = live_session(session_id)
    if live is not None and live.grace:
        return layer
    try:
        gone = (await layer.is_session_process_dead(session_id)
                or not await layer.is_session_alive(session_id))
    except Exception:  # noqa: BLE001  # a failed liveness probe reads as a live session
        gone = False
    if not gone:
        return layer
    return await _restart_participant(meeting, agent_slug, session_id, layer)


async def _restart_participant(meeting: dict, agent_slug: str, session_id: str,
                               held_on: ExecutionLayer) -> ExecutionLayer:
    """Start a participant's session again under its id: its config built
    as at the meeting's start, resumed from its conversation when the engine
    kept one (asked of the layer that held it), on the layer the rebuilt
    config names, its slot taken on the box it lands on. A build or a start
    that fails gives its seat back and raises: the turn fails, and the
    meeting goes on without the participant."""
    from core.concurrency import rehome_slot
    from core.config.config_builder import release_config_seat
    from core.execution_layer import ResumeVerdict
    from core.session.resume_probe import RESUME_PROBE_BUDGET_INLINE_S, settle_resume
    logger.info("Meeting %s: participant %s has no live session, starting it again",
                meeting.get("id"), agent_slug)
    cfg = await build_meeting_agent_config(agent_slug, meeting, session_id)
    ctx = cfg.security_context
    built = placement.compute_of(getattr(ctx, "placement", None))
    try:
        verdict = await settle_resume(
            held_on, session_id, agent_name=agent_slug,
            username=getattr(ctx, "mount_username", "") or "",
            budget_s=RESUME_PROBE_BUDGET_INLINE_S)
        if verdict is ResumeVerdict.UNKNOWN:
            raise RuntimeError("The participant's machine did not answer.")
        await held_on.prepare_resume(session_id)
        cfg.resume = verdict is ResumeVerdict.RESUMABLE
        layer = get_execution_layer(
            agent_slug, execution_path=cfg.execution_path, user_sub=cfg.user_sub or None,
            role=getattr(ctx, "role", roles.MANAGER) or roles.MANAGER,
            execution_target=cfg.execution_target,
            **({"placement_built": built} if built else {}),
        )
        await layer.start_session(session_id, cfg)
    except BaseException:
        release_config_seat(session_id, cfg)
        raise
    _meeting_session_layers[session_id] = layer
    # The close released its slot: taken again where the session runs now.
    # A refusal ends nothing, the session is already running.
    await rehome_slot(session_id, kind="meeting",
                      target=placement.admission_target(cfg.execution_target or placement.LOCAL, built),
                      execution_path=cfg.execution_path, mcp_config_path=cfg.mcp_config_path or "")
    _sessions.setdefault(session_id, {"created": True, "message_count": 0})
    _sessions[session_id].update({
        "is_task": True, "is_meeting": True, "client_type": session_kind.MEETING.name,
        "agent": agent_slug, "last_active": datetime.now(timezone.utc).isoformat(),
        "meeting_id": meeting.get("id"),
    })
    _save_sessions()
    return layer
