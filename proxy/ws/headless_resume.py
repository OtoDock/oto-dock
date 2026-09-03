"""Headless dead-session resume — the connection-free twin of the dashboard's
``_resume_dead_session_for_chat`` funnel (ws/dashboard_warmup.py).

The dashboard heals a dead CLI process at its turn chokepoint, but that
helper is a ``DashboardConnection`` method: it adopts into the connection's
viewed attributes and reads the connection's user snapshot. Turn sources with
no dashboard connection — duplex voice utterances today, any future headless
dispatcher — need the same heal or they send into the corpse forever
(incident 2026-08-12: the soft-interrupt watchdog escalated to a hard abort
on a slow satellite → ``cli_dead`` → every voice turn errored until the
dashboard happened to run a typed turn). This module rebuilds the spawn from
the CHAT ROW + the DB user row and funnels ``build_agent_config`` →
``start_session`` directly (the ws/phone.py headless spawn precedent).

Contract mirrors the dashboard helper:
- ``can_resume_session`` BEFORE ``prepare_resume`` — the remote resumability
  check needs the session info's machine_id, which ``prepare_resume`` pops.
- Resumable → same session id, ``resume=True`` (CLI ``--resume`` reloads the
  on-disk history). Not resumable → fresh id, old slot released, chat row
  repointed and flagged ``pending_history_seed`` so the next turn's seed
  chokepoint prepends the DB-history digest.
- The chat's pinned ``execution_target`` drives the rebuild, so a remote
  session resumes on its own machine (or hard-fails if that machine is
  offline — never a silent migration off the workspace).

Scope (v1): HEADLESS sessions only. An interactive-mode chat raises
``ResumeUnavailable`` — a PTY respawn needs the dashboard's streaming
consumers (theme, WS tail), and the duplex interactive path feeds from rows
of a session the dashboard owns.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from core.config.config_builder import build_agent_config, is_hard_fail_target
from core.config.task_config_builder import (
    resolve_task_identity, task_allows_knowledge_rw,
)
from core.execution_layer import ExecutionLayer
from core.session import interactive_session
from core.session.session_manager import get_execution_layer
from core.session.session_state import (
    clear_session_liveness,
    get_user_tz,
    set_session_user_tz,
)
from storage import database as task_store
# Module-level pure helpers of ws/dashboard.py (same intra-unit import
# ws/duplex_attach.py already leans on for _effective_agent_role).
from ws.dashboard import (
    _effective_agent_role,
    _resolve_session_interactive,
    _resume_username_for_chat,
)

logger = logging.getLogger("claude-proxy")


class ResumeUnavailable(RuntimeError):
    """The chat cannot be healed headless (interactive-mode chat, missing
    chat/user row, live PTY on the session id). Callers report and keep
    today's behavior — nothing was mutated."""


@dataclass
class HeadlessSpawn:
    session_id: str
    layer: ExecutionLayer
    # False = fresh session (no engine history; the chat row carries
    # pending_history_seed for the next turn's seed chokepoint).
    resumed: bool


async def resume_dead_session_headless(
    chat_id: str, dead_sid: str, layer_for_chat: ExecutionLayer, *,
    user_sub: str,
) -> HeadlessSpawn:
    """Re-warm dead session ``dead_sid`` for ``chat_id``, headless.

    Agent / model / exec-path / pinned target resolve from the CHAT ROW,
    identity from the DB user row for ``user_sub``. Raises
    ``ResumeUnavailable`` when healing is out of scope (see module doc) and
    propagates spawn failures for the caller to report.
    """
    chat = await asyncio.to_thread(task_store.get_chat, chat_id) if chat_id else None
    if not chat:
        raise ResumeUnavailable("chat_not_found")
    agent = chat.get("agent") or ""
    if not agent:
        raise ResumeUnavailable("chat_has_no_agent")
    user = await asyncio.to_thread(task_store.get_user, user_sub) if user_sub else None
    if not user:
        raise ResumeUnavailable("user_not_found")
    # Spawn-collision guard (the 2026-08-06 twin-fork incident class): a LIVE
    # PTY on this id means the dashboard owns it — never start a headless
    # twin on the same session id.
    live_isess = interactive_session.get(dead_sid)
    if live_isess is not None and live_isess.alive:
        raise ResumeUnavailable("live_interactive_session")

    perm_mode = chat.get("permission_mode", "default")
    chat_model = chat.get("model") or ""
    exec_path = chat.get("execution_path") or ""
    pinned = chat.get("execution_target") or ""
    exec_mode = chat.get("execution_mode") or ""

    # ORDER IS LOAD-BEARING: can_resume_session BEFORE prepare_resume (see
    # module doc). Same rule as the dashboard helper.
    can_resume = await layer_for_chat.can_resume_session(
        dead_sid, agent_name=agent,
        username=_resume_username_for_chat(
            chat_id, agent, user.get("username", "") or "",
        ),
    )
    await layer_for_chat.prepare_resume(dead_sid)

    sid = dead_sid
    if not can_resume:
        # No conversation data → fresh session. Release the old slot
        # (prepare_resume removed the session from the registry) and clear
        # any stuck liveness badges from its dead background work.
        from core.concurrency import release_chat_slot
        release_chat_slot(dead_sid)
        clear_session_liveness(dead_sid, reason="resume_failed")
        sid = str(uuid.uuid4())

    effective_role = _effective_agent_role(user_sub, agent, fallback_user=user)
    task_identity = None
    if chat_id.startswith("task-"):
        run = task_store.get_run(chat_id.removeprefix("task-"))
        if run:
            # Same fire, same provenance: the knowledge-RW opt-in follows
            # the run's stored task shape (delegate stays False).
            task_identity = resolve_task_identity(
                agent, run.get("scope") or "agent", run.get("created_by"),
                allow_knowledge_rw=task_allows_knowledge_rw(
                    run.get("task_type")),
            )
    agent_cfg = await build_agent_config(
        agent_name=agent, user=user, user_sub=user_sub,
        user_role=effective_role, permission_mode=perm_mode,
        client_type="dashboard", resume=can_resume, model=chat_model,
        execution_path=exec_path, chat_id=chat_id, session_id=sid,
        task_identity=task_identity, pinned_target=pinned,
    )
    if is_hard_fail_target(agent_cfg.execution_target):
        # Pinned remote machine offline with fallback disabled — surface it;
        # migrating the session off its workspace would be worse.
        raise RuntimeError("session_machine_offline")
    if _resolve_session_interactive(agent_cfg, exec_mode):
        # v1 scope guard — nothing acquired or mutated yet beyond
        # prepare_resume, which is what a refused dashboard resume does too.
        raise ResumeUnavailable("interactive_chat")
    agent_cfg.interactive = False

    from core.concurrency import acquire_chat_slot
    adm = await acquire_chat_slot(sid, target=agent_cfg.execution_target,
                                  execution_path=agent_cfg.execution_path,
                                  user_sub=user_sub)
    if not adm:
        raise RuntimeError(adm.user_message)

    resolved_layer = get_execution_layer(
        agent, execution_path=agent_cfg.execution_path or exec_path,
        user_sub=user_sub, role=effective_role,
        execution_target=agent_cfg.execution_target,
    )
    try:
        await resolved_layer.start_session(sid, agent_cfg)
    except Exception:
        from core.concurrency import release_chat_slot
        release_chat_slot(sid)
        raise

    # Per-turn time injection needs the session's TZ; no client_info frame
    # exists on a headless heal, so seed from the user's last known TZ.
    tz = get_user_tz(user_sub)
    if tz:
        set_session_user_tz(sid, tz)
    if not pinned and not is_hard_fail_target(agent_cfg.execution_target):
        await asyncio.to_thread(
            task_store.update_chat, chat_id,
            execution_target=agent_cfg.execution_target,
        )
    if not can_resume:
        await asyncio.to_thread(
            task_store.update_chat, chat_id, session_id=sid,
            pending_history_seed="resume_failed",
        )
    logger.info(
        "headless_resume: chat %s session %s → %s (resumed=%s, target=%s)",
        chat_id[:8], dead_sid[:8], sid[:8], can_resume,
        agent_cfg.execution_target or "local",
    )
    return HeadlessSpawn(session_id=sid, layer=resolved_layer, resumed=can_resume)
