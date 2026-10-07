"""Delegate result delivery and continuation wakes: the persistent and one-shot
delivery paths, the echo turn, wake redelivery after a restart and the
interactive re-warm.

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import contextlib
import functools
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING


import config
from storage import database as task_store
from storage.automation import run_status
from services.scheduler import interactive, lanes, shared
from core import placement
from core.session import session_kind
from core.session.visibility import (
    is_shared_chat_owner, is_synthetic_owner, is_task_chat_owner,
)
from auth import roles
from ws import wire_events as wire
from services.scheduler.delivery_identity import (  # noqa: F401
    _shared_chat_person, _standing_of, _wake_refused, _wake_visibility,
    resolve_delivery_person,
)

if TYPE_CHECKING:
    from pathlib import Path
    from auth.path_policy import SecurityContext
    from core.execution_layer import ExecutionLayer
    from core.session.session_delivery import DeliveryOutcome
    from core.session.visibility import VisibilityResolution
    from storage.remote_store import BrowserTargetSettings

logger = logging.getLogger("claude-proxy.scheduler")

# Chats one wake sweep delivers at once: a wake that waits for a slot on a
# full box holds up only its own chat.
_SWEEP_CONCURRENCY = 4


def _chat_pin(chat: dict | None, user_sub: str | None) -> str:
    """Where a chat woken as a person runs when it is that person's own chat
    or a Shared-only chat: the chat row's own placement, as the dashboard
    resumes it (``pinned_target``), since the person's own resolution (their
    machine, the tier rule for an admin-paired one) may name a machine the
    chat never ran on. "" for every other delivery, which resolves from its
    person and role. Synchronous: call it on the DB executor."""
    from core.session.visibility import is_shared_only, shared_chat_owner
    agent = (chat or {}).get("agent") or ""
    if not user_sub or not agent:
        return ""
    owner = chat.get("user_sub")
    if owner == user_sub:
        # A chat with no recorded placement resolves from its person, as
        # the dashboard resumes it.
        pin = chat.get("execution_target") or ""
        if not pin:
            return ""
    elif owner != shared_chat_owner(agent) or not is_shared_only(agent):
        return ""
    else:
        pin = chat.get("execution_target") or placement.LOCAL
    if placement.is_local(pin):
        return placement.LOCAL
    from services.remote.remote_status import is_reachable
    return pin if is_reachable(pin) else placement.offline_sentinel(pin)


def _wake_credential_env(agent: str, session_id: str, *, user_sub: str | None, role: str,
                         vis: "VisibilityResolution", where: placement.PlacementCapabilities,
                         mcp_config: "Path | None", flat_env: dict[str, str] | None,
                         bash_env_keys: set, mcp_format: str | None,
                         ) -> "tuple[Path | None, dict[str, str], dict[str, str], int]":
    """The credential environment of a respawned session, as the chat builder
    (``core/config/config_builder.py``) assembles it: the MCP build's flat env,
    each manifest's ``path_env`` at the mount, the ``OTO_*`` set and a session
    token for the delivery's person (a Shared-only chat mounts no person, so
    the layers' own token would name nobody; the credential environment is
    applied last by every spawn path). A TOML config gets it injected (Codex
    stdio MCPs inherit nothing from the parent), the bash-only keys excluded.
    Returns ``(mcp_config, credential_env, multi_value_envs, minted_at)``, the
    last the token's ``iat`` (the session's floor). Synchronous: call it off
    the loop."""
    from auth.session_token import create_session_token
    from core.sandbox import oto_env
    from services import path_roles
    from services.mcp import mcp_registry
    env = dict(flat_env or {})
    multi_value_envs: dict[str, str] = {}
    for manifest in (mcp_registry.get_agent_mcps(agent, placement=where) or []):
        for env_var, decl in (manifest.path_env or {}).items():
            try:
                env[env_var] = path_roles.resolve_path_env_entry(
                    decl, username=vis.mount_username, user_role=role or "",
                )
            except ValueError as e:
                logger.warning("path_env injection failed for %s.%s: %s",
                               manifest.name, env_var, e)
                continue
            if decl.is_multi:
                multi_value_envs[env_var] = decl.join
    platform_role = ((task_store.get_user(user_sub) or {}).get("role") or "") if user_sub else ""
    env.update(oto_env.build_oto_env(
        agent_name=agent,
        username=vis.mount_username,
        user_sub=user_sub or "",
        user_role=role or "",
        platform_role=platform_role,
        session_id=session_id,
        memory_user_enabled=vis.memory_user_enabled,
        memory_agent_enabled=vis.memory_agent_enabled,
        default_scope=vis.effective_default_scope,
        task_type="",
        available_scopes=vis.available_scopes,
        force_config=vis.config_visible,
    ))
    minted_at = int(time.time())
    env["PROXY_API_KEY"] = create_session_token(session_id, agent, user_sub or "",
                                                issued_at=minted_at)
    multi_value_envs.update(oto_env.OTO_MULTI_VALUE_ENVS)
    if mcp_format == "toml" and mcp_config:
        mcp_config = mcp_registry.inject_credential_env_into_toml(
            mcp_config, env, exclude_keys=bash_env_keys,
        )
    return mcp_config, env, multi_value_envs, minted_at


def _wake_delegation_targets(agent: str, user_sub: str | None, role: str) -> list[str]:
    """The agents a respawned session may delegate to, as the chat builder
    resolves them: the agent's configured targets, those of a person's wake
    narrowed to the agents the person holds (a platform admin keeps them
    all), and the agent itself first. Synchronous: call it on the DB executor."""
    from storage.agents import agent_store
    targets = agent_store.get_delegation_targets(agent)
    if user_sub and not roles.is_admin(role) and not roles.is_admin(
            (task_store.get_user(user_sub) or {}).get("role")):
        held = set(task_store.get_user_agents(user_sub))
        targets = [t for t in targets if t in held]
    return targets if agent in targets else [agent] + targets


def _wake_mcp_build(
    agent: str, user_sub: str | None, *, username: str, role: str,
    vis: "VisibilityResolution", chat_id: str, where: placement.PlacementCapabilities,
    browser: "BrowserTargetSettings", mcp_format: str | None, session_id: str,
    targets: list[str],
) -> tuple:
    """The MCP build of a respawned session for the delivery's person, as the
    chat builder makes it (the delegation targets included). Returns the
    build's five values. Synchronous: it reads agent files and platform
    settings, so call it off the loop."""
    from services.mcp import mcp_registry
    return mcp_registry.build_session_mcp_config(
        agent, user_sub or None, task_mode=True, task_scope=vis.mount_scope,
        username=username or "", user_role=role or "", chat_id=chat_id or "",
        placement=where, target_browser=browser, mcp_config_format=mcp_format,
        session_id=session_id, delegation_targets=targets,
    )


def _wake_prompt(
    agent: str, *, role: str, vis: "VisibilityResolution",
    where: placement.PlacementCapabilities, exec_path: str, security: "SecurityContext",
    excluded: dict, dynamic_contexts: list | None,
) -> str:
    """The system prompt of a respawned session, as the chat builder makes
    it: from the same visibility and role as the config (the mount's
    username, folders and user context, the role's view of the workspace),
    with the dynamic MCP context, followed by the identity, scope and folder
    sections of the session's security context. Synchronous: it reads agent
    files and platform settings, so call it off the loop."""
    from auth.path_policy import build_permission_context
    from services.mcp import mcp_registry
    prompt = config.build_agent_prompt(
        agent, username=vis.mount_username, role=role or roles.MANAGER,
        excluded_mcps=excluded or None, dynamic_contexts=dynamic_contexts or None,
        client_type=session_kind.TASK.name, placement=where,
        mount_shared=vis.mount_shared, execution_path=exec_path or "",
    )
    assigned = [m.name for m in (mcp_registry.get_agent_mcps(agent, placement=where) or [])
                if m.name not in excluded]
    return (prompt or "") + build_permission_context(
        security, assigned_mcp_names=tuple(assigned), execution_path=exec_path or "",
    )


# A respawned session's dynamic MCP context asks remote MCPs: bounded, so a
# slow one never holds the wake's admission slot.
_WAKE_CONTEXT_TIMEOUT_S = 30.0


async def _wake_dynamic_contexts(
    agent: str, excluded: dict, *, user_sub: str | None, role: str,
    targets: list[str], where: placement.PlacementCapabilities,
) -> list | None:
    """The dynamic MCP context of a respawned session, as the chat builder
    asks for it (the attached MCPs, the delegation roster, the meetings
    access); None when it cannot be had in time."""
    from services.mcp import dynamic_context, mcp_registry
    assigned = [m.name for m in (await asyncio.to_thread(
        functools.partial(mcp_registry.get_agent_mcps, agent, placement=where)) or [])
                if m.name not in (excluded or {})]

    async def _contexts() -> list:
        return await dynamic_context.get_dynamic_contexts(
            agent, assigned, user_sub=user_sub or "", user_role=role,
            trigger_payload=None, delegation_targets=targets,
            delegation_roster=await asyncio.to_thread(
                dynamic_context.build_delegation_roster, targets),
            meetings_access=await asyncio.to_thread(
                dynamic_context.build_meetings_access, user_sub or "", role or ""),
            nouser_reads=not user_sub, placement=where,
        )

    try:
        return await asyncio.wait_for(_contexts(), _WAKE_CONTEXT_TIMEOUT_S)
    except Exception:
        logger.warning("Wake respawn: the dynamic MCP context of agent '%s' was not built",
                       agent, exc_info=True)
        return None


def _wake_superseded(session_id: str, chat_id: str) -> bool:
    """Someone else is starting or using this chat's session: a live
    interactive session under the id, a live terminal or a warmup on the
    chat, or a turn streaming on it. A background wake then spawns nothing
    (its wake is stored for the chat's next turn instead)."""
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session, warmup_registry

    live = interactive_session.get(session_id) if session_id else None
    if live is not None and live.alive:
        return True
    if chat_id:
        if interactive_session.find_live_for_chat(chat_id) is not None:
            return True
        if warmup_registry.get(chat_id) is not None:
            return True
        pump = _active_pumps.get(chat_id)
        if pump is not None and not pump.is_done:
            return True
    return False


def _release_wake_slot(session_id: str, chat_id: str) -> None:
    """A wake that reserved and gave up before its session lived frees the
    slot, unless a person took the chat over meanwhile: their warmup may
    have adopted the reservation by acquiring the same id, and its own
    lifecycle (or the reconciler) releases it then."""
    from core import concurrency
    if not _wake_superseded(session_id, chat_id):
        concurrency.release_unless_live(session_id)


async def _store_undelivered_wakes(chat_id: str, wakes: list[str], *, person: str = "",
                                   role: str = "", by: str = "") -> bool:
    """Every rung failed: keep the wakes on the chat for its next warmup or
    turn, each naming the person and role it runs as (``by``: the creator
    whose role a wake with no person runs at). A live terminal on the chat
    (a person opened it while the wake waited) takes them at once. True when
    stored."""
    from storage.pg import run_db
    from core.session import interactive_session

    stored = False
    for wake in wakes:
        stored = bool(await run_db(functools.partial(
            task_store.append_pending_delegate_wake, chat_id, wake,
            person=person, role=role, by=by))) or stored
    if not stored:
        return False
    isess = interactive_session.find_live_for_chat(chat_id)
    if isess is not None and isess.alive:
        for rec in await run_db(task_store.claim_pending_wake_records, chat_id):
            if isess.queue_prompt(rec["prompt"], "delegate_result_replay", chat_id=chat_id) is None:
                await run_db(functools.partial(
                    task_store.append_pending_delegate_wake, chat_id, rec["prompt"],
                    person=rec["person"], role=rec["role"], by=rec["by"]))
    return True


async def _deliver_task_result(task: shared.TaskDefinition, final_status: str, output_text: str,
                               *, worker_chat_id: str = "", output_cursor: int = 0,
                               prompt_row_id: int = 0, prompt_text: str = "",
                               run_started_at: str = "", ending=None,
                               run_id: str = "") -> None:
    """Deliver completed task result to originating session/chat.

    The originating ``on_complete_session_id`` is the primary route key
    (matches the in-memory notify-queue / pump tables). ``on_complete_chat_id``
    is the persistent anchor that survives browser close / proxy restart;
    when the original session is gone, ``_do_deliver`` walks the chat row
    to find the user's current session and routes there instead.

    ``worker_chat_id`` (the run's own chat) turns on LANE FINALIZATION inside
    the spawned delivery task — after the run releases its RAM slot: await
    lane quiescence (a user may still be steering the worker), re-check the
    chat's abort flag, and re-collect cursor-based so the callback carries the
    complete round including ``[User interjected]`` rows. It also feeds the
    ``{{chat_id}}`` template token. ``run_started_at`` bounds the check
    verdict a failed run carries to this run (a worker chat is reused).
    ``ending`` (``core.events.turn_ending.TurnEnding``) is the turn's typed
    ending when the engine stopped it: the callback names it and the next step.
    ``run_id`` names the run whose attached files the result names
    (``services/delegation/result_files``): read after the lane settled,
    never copied here; a second delivery of the run names the same rows.
    """
    row = await asyncio.to_thread(task_store.get_dynamic_task, task.id)
    on_complete_agent = (row.get("on_complete_agent") if row else None) or task.on_complete_agent
    on_complete_prompt = (row.get("on_complete_prompt") if row else None) or task.on_complete_prompt
    on_complete_session_id = (row.get("on_complete_session_id") if row else None) or task.on_complete_session_id
    on_complete_chat_id = (row.get("on_complete_chat_id") if row else None) if row else None

    if not on_complete_agent or not on_complete_prompt:
        logger.info(
            f"Task result delivery skipped (no on_complete): task={task.id}, "
            f"agent={on_complete_agent}, prompt={'yes' if on_complete_prompt else 'no'}, "
            f"session={on_complete_session_id}, chat={on_complete_chat_id}, "
            f"row={'found' if row else 'missing'}"
        )
        return

    # Resolve session_id from chat_id if we have a chat but no/stale session
    if on_complete_chat_id and not on_complete_session_id:
        chat = await asyncio.to_thread(task_store.get_chat, on_complete_chat_id)
        if chat:
            on_complete_session_id = chat.get("session_id")

    if not on_complete_session_id:
        logger.info(f"Task result delivery skipped (no session): task={task.id}")
        return

    logger.info(
        f"Task result delivery starting: task={task.id}, "
        f"session={on_complete_session_id[:8]}, chat={on_complete_chat_id or 'none'}"
    )

    async def _finalize_and_deliver() -> None:
        status, output = final_status, output_text
        if worker_chat_id:
            # Finalization is best-effort: a failure here must never strand the
            # delegating agent — fall through and deliver what the run already
            # collected.
            try:
                # A user interrupt DEFERS the callback instead of racing the
                # user: the worker serves them directly in its own chat, and
                # the orchestrator gets ONE callback when the lane goes
                # genuinely quiet — carrying the interjections AND the
                # worker's replies. Normal terminals keep the short settle
                # (fire-and-forget pays no latency).
                if status == run_status.USER_INTERRUPTED:
                    await lanes._await_lane_quiescence(
                        worker_chat_id,
                        ceiling_seconds=1800.0,
                        settle_seconds=120.0,
                        immediate_quiet_ok=False,
                    )
                else:
                    await lanes._await_lane_quiescence(
                        worker_chat_id, ceiling_seconds=1800.0,
                    )
                chat = await asyncio.to_thread(task_store.get_chat, worker_chat_id)
                # A run the engine ended (a typed ending) stays failed: the
                # abort flag it also stamps is for the next turn's context.
                if (chat and chat.get("last_turn_aborted")
                        and status != run_status.FAILED):
                    status = run_status.USER_INTERRUPTED
                collected = await asyncio.to_thread(
                    lanes._collect_lane_output_since, worker_chat_id,
                    output_cursor, prompt_row_id, prompt_text,
                )
                if collected:
                    output = collected
            except Exception:
                logger.exception(
                    f"Lane finalization failed for chat {worker_chat_id[:8]} — "
                    f"delivering the run's own collection"
                )
            finally:
                lanes.release_report(worker_chat_id)
        # A worker that failed a check (CHECKS.md): the parent receives the
        # final failing result WITH the verdict, never an unchecked one.
        verdict = None
        if status == run_status.FAILED and worker_chat_id:
            try:
                from services.checks import evaluator as _checks
                verdict = await asyncio.to_thread(
                    _checks.latest_failure, worker_chat_id, run_started_at)
            except Exception:
                logger.debug("delegate verdict lookup failed", exc_info=True)
        # The files the worker attached; a store error names none.
        files: list = []
        files_skipped: list = []
        files_note = ""
        if run_id:
            try:
                from services.delegation import result_files as _result_files
                from storage.pg import run_db as _run_db
                # The run's attach lock: a copy still in flight records first.
                async with _result_files.run_lock(run_id):
                    files, files_skipped, files_note = await _run_db(_result_files.delivery_lists, run_id)
            except Exception:
                logger.exception(f"Result files lookup failed for run {run_id}")
        result_prompt = (on_complete_prompt
            .replace("{{output}}", output or "")
            .replace("{{task_id}}", task.id)
            .replace("{{task_name}}", task.name)
            .replace("{{status}}", status)
            .replace("{{chat_id}}", worker_chat_id or "")
            .replace("{{agent}}", task.agent))
        if verdict:
            result_prompt += (f"\n\n[Check {verdict['check']} did not pass after "
                              f"{verdict['round']} round(s): {verdict.get('summary') or ''}]")
        # Appended here, not a template token: task rows stored before this
        # carry the old template and get the note all the same.
        if ending is not None:
            result_prompt += f"\n\n[{ending.callback_note(worker_chat_id)}]"
        if files_note:
            result_prompt += f"\n\n{files_note}"
        await _do_deliver(
            on_complete_session_id, on_complete_agent, result_prompt, task,
            chat_id=on_complete_chat_id,
            output_text=output,
            status=status,
            verdict=verdict,
            ending=ending,
            files=files,
            files_skipped=files_skipped,
        )

    # The worker chat owes this report until the collection above (taken
    # before the task exists, so no turn starts between them).
    lanes.hold_report(worker_chat_id)
    asyncio.create_task(_finalize_and_deliver())


def _ending_fields(ending) -> dict:
    """The delegate_result event's typed ending fields (none without one)."""
    if ending is None:
        return {}
    return {"reason": ending.reason, "resets_at": ending.resets_at}


def _result_extras(verdict: dict | None, files: list | None,
                   files_skipped: list | None) -> dict:
    """The delegate_result event's optional keys beyond the typed ending: a
    failed run's check verdict, and the worker's attached files (both lists
    ride together once either has an entry; absent otherwise, so a result
    without files keeps its old shape)."""
    out: dict = {"verdict": verdict} if verdict else {}
    if files or files_skipped:
        out["files"] = list(files or [])
        out["files_skipped"] = list(files_skipped or [])
    return out


async def _do_deliver(session_id: str, agent: str, result_prompt: str, task: shared.TaskDefinition,
                      *, chat_id: str | None = None, output_text: str = "",
                      status: str = run_status.COMPLETED, verdict: dict | None = None,
                      ending=None, files: list | None = None,
                      files_skipped: list | None = None) -> None:
    """Deliver a delegate result via the session-delivery ladder.

    The routing itself (WS notify → pump → persistent → one-shot) lives in
    ``core.session.session_delivery.deliver_prompt``; this wrapper owns the
    DELEGATE semantics — the ``delegate_result`` event payload, the live-state
    badge, the ``task_result_prompt`` notify shape, and the echo persistence.
    The persistent/one-shot rungs are passed by name so they resolve through
    this module at call time (tests monkeypatch them here).
    """
    try:
        from core.session.session_delivery import deliver_prompt
        from core.session.session_state import mark_delegate_completed
        from storage.pg import run_db

        # Update live state immediately (for reconnect accuracy)
        if chat_id:
            mark_delegate_completed(chat_id, task.name, task_id=task.id, status=status)

        # Delegate event data shared across all paths. task_id is the stable
        # spawn↔result correlation key (the frontend keys the delegate block by it).
        # status (completed/failed/cancelled) drives the badge icon + lets the
        # frontend skip minting an empty bubble for a no-output terminal.
        # The bubble keeps the TAIL on overflow: output_text is the whole lane
        # narration and the worker's actual deliverable is its final message —
        # a head cut always amputates it (the old silent [:2000] did exactly
        # that). The result PROMPT to the parent agent stays uncapped.
        output_preview = lanes._delegate_output_preview(output_text or "")
        delegate_event = {
            "type": wire.DELEGATE_RESULT,
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
            **_result_extras(verdict, files, files_skipped),
            **(_ending_fields(ending)),
        }
        delegate_event_data = json.dumps({
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
            **_result_extras(verdict, files, files_skipped),
            **(_ending_fields(ending)),
        }, default=str)

        from core.events import chat_writer
        persisted: list[str] = []
        written: list[asyncio.Future] = []

        def _persist_delegate_event(target_chat_id: str) -> None:
            # The delegate_result event is the source of truth — it renders the
            # delegate's output and completes the badge regardless of whether
            # the LLM-echo delivery succeeds. Invoked exactly once by the
            # ladder on every non-WS path (the dashboard handler persists it
            # on the WS path after rendering). Written on the chat's lane, off
            # the loop, ahead of the echo turn's rows.
            persisted.append(target_chat_id)
            if target_chat_id:
                written.append(chat_writer.submit(
                    target_chat_id,
                    functools.partial(task_store.add_chat_message, target_chat_id, "event", "",
                                      event_type=wire.DELEGATE_RESULT,
                                      event_data=delegate_event_data),
                    label="delegate_result",
                ))

        # Resolve with the originating session's user + role so a user-pinned
        # remote target (or admin-default remote) is honored on the
        # persistent/one-shot rungs — a bare resolve would default to local and
        # miss a user override / break remote start.
        # The chat decides who wakes: a person's own chat as its owner, a
        # Shared-only chat as the person whose session delegated the work,
        # any other (a task or phone chat, none at all) by the worker's scope.
        identity = await resolve_delivery_person(
            chat_id, session_id, task.created_by, task.scope, agent)
        shared_person = identity.person if identity.shared else ""
        if identity.refused:
            # The person no longer holds the agent: the result is kept on
            # the chat and nothing is warmed for them (no rung, no stored
            # wake to replay later).
            await _persist_refused_result(
                chat_id, session_id, delegate_event, delegate_event_data)
            logger.info(
                f"Task result kept as an event only: {identity.person[:8]} no longer "
                f"holds agent '{agent}': chat={(chat_id or '-')[:8]}, task={task.name}"
            )
            return
        deliver_user_sub: str | None = identity.person or None
        deliver_role = identity.role

        async def _on_outcome(outcome: "DeliveryOutcome") -> None:
            # Durable replay: every rung failed, so the orchestrator was never
            # woken (the delegate_result event row IS persisted, but no turn
            # ran). Store the wake on the parent chat; the next warmup/turn
            # claims and injects it so the orchestrator continues then (a
            # manual re-warm included). Here, not after the ladder, so the
            # hand-back of a result queued on a terminal that closed first
            # stores it too.
            if outcome.path == "none" and chat_id:
                if deliver_user_sub and _wake_refused(
                        (await run_db(_standing_of, deliver_user_sub, agent))[0],
                        shared=bool(shared_person)):
                    logger.info(
                        f"Delegate wake not stored: {deliver_user_sub[:8]} no longer holds "
                        f"agent '{agent}': chat={chat_id[:8]}, task={task.name}"
                    )
                    return
                if await _store_undelivered_wakes(chat_id, [result_prompt],
                                                  person=deliver_user_sub or "",
                                                  role=deliver_role):
                    logger.info(
                        f"Delegate wake undeliverable: stored for replay on next "
                        f"warmup: chat={chat_id[:8]}, task={task.name}"
                    )
            # The delegate_result event is already persisted by the ladder —
            # only the assistant echo (the LLM's reaction to the result) needs
            # a real response. A failed --resume (dead session) returns None
            # and must NOT be saved as the assistant message (the old "No
            # conversation found" bug, now also guarded by a resumability
            # pre-check in _deliver_via_oneshot); a pump-driven echo turn
            # returns "" — the pump persisted the turn itself, saving here
            # would duplicate it. Runs via the ladder's on_outcome hook so a
            # PTY close-handback's late persistent/one-shot echo is saved
            # identically.
            if not outcome.response:
                return
            if outcome.chat_id:
                # On the chat's lane, behind the event row, off the loop.
                await chat_writer.submit(
                    outcome.chat_id,
                    functools.partial(task_store.add_chat_message, outcome.chat_id,
                                      "assistant", outcome.response),
                    label="delegate_echo",
                )
                logger.info(f"Task result echo saved to chat DB: chat={outcome.chat_id[:8]}, task={task.name}")
            else:
                from core.session import session_state as _state_mod
                _state_mod._save_pending_result(outcome.session_id, outcome.response)
                logger.info(f"Task result echo saved as pending: session={outcome.session_id[:8]}, task={task.name}")

        try:
            outcome = await deliver_prompt(
                chat_id or "", result_prompt,
                source="delegate_result",
                session_id=session_id,
                agent=agent,
                user_sub=deliver_user_sub,
                role=deliver_role,
                notify_payload={
                    "type": wire.NOTIFY_TASK_RESULT_PROMPT,
                    # Originating (delegating) chat/session so the dashboard handler
                    # runs the synthesis turn on THIS chat, not whatever the socket
                    # happens to be viewing (chat-scoped server turns). The ladder
                    # overwrites session_id/chat_id with the resolved anchors.
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "task_id": task.id,
                    "task_name": task.name,
                    "result_prompt": result_prompt,
                    "delegate_agent": task.agent,
                    "output_text": output_preview,
                    "status": status,
                    **_result_extras(verdict, files, files_skipped),
                    **_ending_fields(ending),
                },
                pump_event=delegate_event,
                persist_event=_persist_delegate_event,
                persistent_fn=_deliver_via_persistent,
                oneshot_fn=_deliver_via_oneshot,
                on_outcome=_on_outcome,
            )
        except Exception:
            # A rung that raises (a broken spawn, a config or DB error) never
            # reaches the ladder's hook: the event row is written if no rung
            # wrote it, and the wake is stored the way the hook stores an
            # undelivered one (the standing re-check included).
            logger.exception(
                f"Task result delivery raised: session={session_id[:8]}, task={task.id}")
            from core.session.session_delivery import DeliveryOutcome
            if not persisted:
                _persist_delegate_event(chat_id or "")
            outcome = DeliveryOutcome("none", chat_id=chat_id or "", session_id=session_id)
            await _on_outcome(outcome)
        for row in written:
            # The event row is on the chat before the badge frame goes out
            # (the lane logs a failed write itself).
            with contextlib.suppress(Exception):
                await row
        logger.info(
            f"Task result delivered via {outcome.path}: "
            f"session={outcome.session_id[:8] if outcome.session_id else '-'}, task={task.name}"
        )

        # Live badge correctness: the ws/steer/pump rungs deliver the
        # delegate_result frame to the viewer themselves; the other rungs
        # (pty/persistent/oneshot/none) carry no socket, so a live viewer's
        # "Delegated" badge would spin until a full history reload. Broadcast
        # the terminal frame chat-scoped, independent of the wake delivery.
        # Idempotent on the frontend (status keyed by task_id).
        if chat_id and outcome.path not in ("ws", "steer", "pump"):
            from core.session.session_state import broadcast_chat_frame
            broadcast_chat_frame(chat_id, delegate_event)
    except Exception as e:
        logger.error(f"Task result delivery failed: session={session_id[:8]}, task={task.id}: {e}", exc_info=True)


async def _persist_refused_result(chat_id: str | None, session_id: str,
                                  delegate_event: dict, delegate_event_data: str) -> None:
    """The delegate_result event of a delivery that warms nothing: written on
    the chat's lane (the chat resolved by its session when the caller had
    none) and shown to a live viewer."""
    from storage.pg import run_db
    from core.events import chat_writer
    from core.session.session_state import broadcast_chat_frame
    target = chat_id or ""
    if not target and session_id:
        row = await run_db(task_store.get_chat_by_session, session_id)
        target = (row or {}).get("id") or ""
    if not target:
        return
    await chat_writer.submit(
        target,
        functools.partial(task_store.add_chat_message, target, "event", "",
                          event_type=wire.DELEGATE_RESULT, event_data=delegate_event_data),
        label="delegate_result",
    )
    broadcast_chat_frame(target, delegate_event)


async def redeliver_pending_wakes(machine_id: str | None = None,
                                  session_ids: list[str] | tuple[str, ...] = ()) -> int:
    """Redeliver delegate wakes parked on chats (stored when every delivery
    rung failed — typically the proxy restarted inside the delivery window).

    Startup runs it once after boot settles; every satellite reconnect runs it
    scoped to that machine's chats, pinned there or running one of
    ``session_ids`` (an idle satellite chat's wake can only deliver once its
    machine is back). Per-chat claims are atomic, so a
    concurrent warmup/turn claim wins cleanly and this pass no-ops for that
    chat. A chat whose session is parked for Mode C re-adopt is skipped —
    the reconnect pass retries it after the adopt settles. Failed deliveries
    re-persist the original wakes (same caller-side contract as
    ``_deliver_task_result``). Chats are delivered ``_SWEEP_CONCURRENCY`` at a
    time. Returns the number of chats woken."""
    rows = await asyncio.to_thread(
        task_store.list_chats_with_pending_wakes, machine_id, session_ids,
    )
    gate = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def _one(chat: dict) -> bool:
        async with gate:
            return await _redeliver_chat(chat)

    results = await asyncio.gather(*(_one(chat) for chat in rows), return_exceptions=True)
    woken = 0
    for chat, result in zip(rows, results):
        if isinstance(result, BaseException):
            logger.error("Wake redelivery: chat %s failed", chat["id"][:8], exc_info=result)
        elif result:
            woken += 1
    return woken


async def _redeliver_chat(chat: dict) -> bool:
    """One chat of the wake sweep; True when a rung woke it. The claimed
    wakes go in groups, one per person and role they were stored for, one
    group after another (a person's own chat: all as its owner)."""
    from services.scheduler import run_recovery
    from storage.pg import run_db

    chat_id = chat["id"]
    if (chat.get("session_id") or "") in run_recovery._parked:
        return False
    agent = chat.get("agent") or ""
    owner = chat.get("user_sub") or ""
    personal = bool(owner) and not is_synthetic_owner(owner)
    if personal:
        # Checked before the claim, so a failed read leaves the wakes stored.
        standing, _row = await run_db(_standing_of, owner, agent)
        if standing == roles.NO_ACCESS:
            dropped = await run_db(task_store.claim_pending_delegate_wake, chat_id)
            logger.info(
                "Wake redelivery: chat %s dropped %d wake(s): its owner no longer "
                "holds agent '%s'", chat_id[:8], len(dropped), agent,
            )
            return False
    records = await run_db(task_store.claim_pending_wake_records, chat_id)
    groups: dict[tuple[str, str, str], list[str]] = {}
    for rec in records:
        key = (owner, "", "") if personal else (rec["person"], rec["role"], rec["by"])
        groups.setdefault(key, []).append(rec["prompt"])
    woken = False
    for (person, role, by), prompts in groups.items():
        try:
            woken = await _redeliver_group(chat, prompts, person=person, role=role,
                                           by=by) or woken
        except Exception:
            # One group's broken spawn (bad config, dead layer) must neither
            # kill the sweep nor eat the CLAIMED wakes: re-persist and move
            # on (a corrupt user config.toml fails the oneshot, live-hit).
            logger.exception(
                "Wake redelivery: chat %s delivery raised, re-storing %d wake(s)",
                chat_id[:8], len(prompts),
            )
            for w in prompts:
                await run_db(functools.partial(
                    task_store.append_pending_delegate_wake, chat_id, w,
                    person=person, role=role, by=by))
    return woken


def _sweep_identity(chat: dict, person: str, role: str, by: str,
                    ) -> tuple[str | None, str, bool, bool]:
    """Who one group of stored wakes runs as: ``(user_sub, role, shared,
    refused)``. A person's own chat runs as its owner at their row; a stored
    person as that person (on a Shared-only chat at their standing, refused
    below the editor tier); no person at the role the wake was stored with,
    read again from its creator's row when a creator is on record, else the
    owner kind's default (a task chat or none: manager; a shared or phone
    chat: viewer). Synchronous: call it on the DB executor."""
    agent = chat.get("agent") or ""
    owner = chat.get("user_sub") or ""
    if owner and not is_synthetic_owner(owner):
        standing, row = _standing_of(owner, agent)
        return owner, row or roles.VIEWER, False, standing == roles.NO_ACCESS
    if person:
        shared = bool(_shared_chat_person(chat, person))
        standing, row = _standing_of(person, agent)
        return (person, standing if shared else (row or roles.VIEWER), shared,
                _wake_refused(standing, shared=shared))
    if by and by != agent:
        return None, roles.row_role(task_store.get_user_agent_roles(by), agent) or roles.VIEWER, \
            False, False
    if role:
        return None, role, False, False
    if not owner or is_task_chat_owner(owner):
        return None, roles.MANAGER, False, False
    return None, roles.VIEWER, False, False


async def _redeliver_group(chat: dict, prompts: list[str], *, person: str, role: str,
                           by: str) -> bool:
    """Deliver one group of claimed wakes; True when a rung woke the chat.
    A wake that runs as nobody below the editor tier can start no session
    (it would run from the agent's own state, which the start floor
    refuses), so it rides a live process only: undelivered, it stays for a
    person's next turn on a shared chat and is dropped elsewhere."""
    from core.session.session_delivery import deliver_prompt
    from storage.pg import run_db

    chat_id = chat["id"]
    agent = chat.get("agent") or ""
    owner = chat.get("user_sub") or ""
    user_sub, deliver_role, shared, refused = await run_db(
        _sweep_identity, chat, person, role, by)
    if refused:
        logger.info(
            "Wake redelivery: chat %s dropped %d wake(s): %s no longer holds agent '%s'%s",
            chat_id[:8], len(prompts), (user_sub or "-")[:8], agent,
            " at the editor tier" if shared else "",
        )
        return False
    spawns = user_sub is not None or roles.can_edit(deliver_role)
    # Kept for a person's next turn only on a shared chat of an agent that is
    # Shared-only now: the chat of an agent that left the mode takes no such
    # turn, so the wake is dropped there as the continuation fire drops it.
    keep = spawns
    if not keep and is_shared_chat_owner(owner):
        from core.session.visibility import is_shared_only
        keep = bool(await run_db(is_shared_only, agent))

    async def _on_outcome(outcome: "DeliveryOutcome") -> None:
        # Same contract as _do_deliver's hook: every rung failed →
        # re-store the original wakes; pump-driven turns return ""
        # (already persisted); dead resumes return None.
        if outcome.path == "none":
            if user_sub and _wake_refused(
                    (await run_db(_standing_of, user_sub, agent))[0], shared=shared):
                return
            if not keep:
                logger.info(
                    "Wake redelivery: chat %s dropped %d wake(s): no live process took "
                    "them and none may start below the editor tier", chat_id[:8], len(prompts),
                )
                return
            await _store_undelivered_wakes(chat_id, prompts, person=person, role=role, by=by)
        if outcome.response and outcome.chat_id:
            from core.events import chat_writer
            await chat_writer.submit(
                outcome.chat_id,
                functools.partial(task_store.add_chat_message, outcome.chat_id,
                                  "assistant", outcome.response),
                label="delegate_echo",
            )

    outcome = await deliver_prompt(
        chat_id, "\n\n".join(prompts),
        source="delegate_result_replay",
        session_id=chat.get("session_id") or "",
        agent=agent,
        user_sub=user_sub,
        role=deliver_role,
        persistent_fn=_deliver_via_persistent,
        oneshot_fn=_deliver_via_oneshot if spawns else None,
        on_outcome=_on_outcome,
    )
    if outcome.path == "none":
        logger.info(
            "Wake redelivery: chat %s undeliverable (%d wake(s))", chat_id[:8], len(prompts),
        )
        return False
    logger.info(
        "Wake redelivery: chat %s woken via %s (%d wake(s))",
        chat_id[:8], outcome.path, len(prompts),
    )
    return True


async def _run_echo_turn_pumped(layer, session_id: str, chat_id: str,
                                agent: str, result_prompt: str, *,
                                person: str = "", only_as_chat_turn: bool = False) -> str | None:
    """Run a delegate-echo turn through a headless ChatStreamPump on the
    delegating chat.

    The old direct collection (``layer.send_message`` → join TEXT parts) ran
    the turn INVISIBLY: no ``chat_status`` streaming/ready broadcasts, no live
    pump a viewer could attach to, no ``last_response_at`` stamp (so no unread
    dot or Active-now row), and every non-text block dropped — a delivered
    result nobody was told about (2026-07-13 incident, chat 75eab195). The
    pump is the one turn pipeline that does all of that, and a viewer opening
    the chat mid-turn attaches to it like any streaming chat.

    Returns "" on success — the pump persisted the turn itself, so the caller
    must NOT save an echo row — or None when a pump can't run here (the caller
    falls through / logs the refusal). A stalled turn is reaped by the task
    watchdog with its partial output persisted; that still counts as delivered
    (the delegate_result event row is the source of truth either way).

    A turn on a live process runs in the chat's own permission mode: when
    its next step waits on a person while nobody views the chat, ``person``
    (the delivery's, else the chat's owner when a person owns it) is told
    once, so the card does not wait unseen.

    The turn ends at the engine's result, as the chat's own turns do, and the
    chat's monitors take what it leaves running (``lanes.takes_chat_contract``);
    only inside a run's report window, or in a phone chat, does it wait on its
    background work as a task turn.
    """
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.events.task_producer import task_produce
    from core.session import visibility as _vis
    from core.session.session_state import get_permission_queue
    from storage.pg import run_db

    # Read before the pump check: nothing may await between it and the
    # pump's registration.
    chat_row = await run_db(task_store.get_chat, chat_id)
    from core.events import input_queue
    if _active_pumps.get(chat_id) is not None or input_queue.get(chat_id).claimed:
        # A turn started on the chat between the ladder's pump check and now,
        # or a send, a drain or a queued delivery holds the chat for the turn
        # it is starting (its pump registers a few steps later): never
        # dual-pump a chat. The caller keeps the prompt (a stored wake, a
        # review's next round).
        return None
    chat_turn = lanes.takes_chat_contract(chat_row, session_id)
    if only_as_chat_turn and not chat_turn:
        return None   # a review never runs inside a run's report
    echo_id = f"echo-{chat_id[:8]}"
    event_queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(
        lanes._chat_turn_produce(layer, session_id, result_prompt, event_queue)
        if chat_turn else task_produce(
            layer, session_id, result_prompt, event_queue, echo_id, settle_timeout=30.0))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=session_id,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(session_id),
        scope="agent" if _vis.is_shared_only(agent) else "user",
    )
    _active_pumps[chat_id] = pump
    pump.start()
    async def _tell_parked() -> None:
        who = person
        if not who:
            owner = (chat_row or {}).get("user_sub") or ""
            who = "" if is_synthetic_owner(owner) else owner
        if not who:
            return
        from services.notifications import notification_manager
        await notification_manager.fire_notification(
            "A chat waits on your answer",
            f"{agent} was woken in a chat (a delegated result, a scheduled wake or a "
            f"finished background job) and its next step waits for your answer there. "
            f"Open the chat to answer.",
            severity="info", scope="user", target=who, source="delegation",
            agent_slug=agent, chat_id=chat_id,
        )

    # A _TaskTurnStalled reap persisted the partial turn and closed the chat
    # status — delivered as far as it went; never fall through to another rung
    # (that would run a SECOND echo turn).
    with contextlib.suppress(lanes._TaskTurnStalled):
        await lanes._watch_task_pump(layer, pump, echo_id, chat_id, session_id,
                                     on_parked=_tell_parked)
    if chat_turn:
        from core.events.pump_bg_monitors import arm_bg_monitors
        arm_bg_monitors(layer, session_id, chat_id)
    return ""


async def _deliver_via_persistent(
    session_id: str, agent: str, result_prompt: str,
    *, user_sub: str | None = None, role: str = "manager", chat_id: str = "",
) -> str | None:
    """Path A/B: Use alive persistent session.

    With a delegating chat the echo turn runs through a headless pump (see
    ``_run_echo_turn_pumped``) and returns "" — the pump persisted it.
    Chat-less deliveries keep the direct collection so the caller can save the
    response as a pending result. None = this rung could not deliver."""
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer
    from core.events.common_events import TEXT

    from storage.pg import run_db
    chat_row = await run_db(task_store.get_chat, chat_id) if (chat_id and user_sub) else None
    pin = await run_db(_chat_pin, chat_row, user_sub)
    try:
        layer = get_execution_layer(agent, user_sub=user_sub, role=role, execution_target=pin)
    except RuntimeError:
        # Resolved remote target offline / disabled — session unreachable here.
        return None
    if not await layer.is_session_alive(session_id):
        return None
    try:
        # Update last_active so this session stays "most recent" for the agent.
        # Without this, task sessions could have a more recent timestamp and
        # /v1/session/current would return the wrong session during re-delegation.
        if session_id in _state._sessions:
            _state._sessions[session_id]["last_active"] = datetime.now(timezone.utc).isoformat()
            _state._save_sessions()
        if chat_id:
            return await _run_echo_turn_pumped(
                layer, session_id, chat_id, agent, result_prompt, person=user_sub or "",
            )
        parts: list[str] = []
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, result_prompt):
                if event.type == TEXT:
                    content = event.data.get("content", "")
                    if content:
                        parts.append(content)
        return "".join(parts) if parts else ""
    except asyncio.TimeoutError:
        # Lock held too long — session may be stuck but don't close it
        return None
    except Exception:
        # Session broken (BrokenPipeError, OSError, RuntimeError, etc.)
        await layer.close_session(session_id)
        return None


async def _deliver_via_oneshot(
    session_id: str, agent: str, result_prompt: str,
    *, user_sub: str | None = None, role: str = "manager", chat_id: str = "",
) -> str | None:
    """Path C: Dead session, use one-shot --resume via execution layer.

    With a delegating chat the resumed turn runs through a headless pump (see
    ``_run_echo_turn_pumped``) and returns "" — the pump persisted it.
    Chat-less deliveries keep the direct collection so the caller can save the
    response as a pending result. The spawn takes a slot of the admission
    budget first (``concurrency.reserve_background``); None when the wake
    waited out its turn or gave way to a person on the chat."""
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer, resolve_execution_path
    from storage import remote_store
    from storage.pg import run_db

    # Update last_active upfront so concurrent lookups (e.g. delegate_task
    # calling /v1/session/current during the resumed session) see this session
    # as most recent before the layer sends the message.
    if session_id in _state._sessions:
        _state._sessions[session_id]["last_active"] = datetime.now(timezone.utc).isoformat()
        _state._save_sessions()
    # Resolve the target once so the layer and the config agree — a bare
    # resolve would default the config target to "local" and the remote layer
    # would reject the start (RemoteExecutionLayer called with local target).
    # The CHAT's pinned layer beats the agent default: a wake for a chat on
    # the non-default layer (agent default codex, chat on claude) resolved
    # the wrong layer and always failed the resumability pre-check.
    _chat_row: dict | None = None
    chat_exec = ""
    if chat_id:
        _chat_row = await asyncio.to_thread(task_store.get_chat, chat_id)
        chat_exec = (_chat_row or {}).get("execution_path") or ""
    exec_path = resolve_execution_path(agent, chat_exec)
    pin = await run_db(_chat_pin, _chat_row, user_sub)
    target = pin or (await run_db(
        remote_store.resolve_execution_target, agent, user_sub, role))[0]
    if placement.is_offline_sentinel(target):
        logger.warning(
            f"Delegate one-shot delivery skipped — agent '{agent}' remote target "
            f"offline: session={session_id[:8]}"
        )
        return None
    layer = get_execution_layer(
        agent, execution_path=exec_path, user_sub=user_sub, role=role,
        execution_target=target,
    )
    # Resumability pre-check: if the session has no conversation file to
    # --resume, the oneshot would emit a "No conversation found" error that we'd
    # otherwise collect and save as the delegate's response. Skip instead — the
    # delegate_result event (saved by _do_deliver) already carries the output.
    username = (await run_db(task_store.get_username_by_sub, user_sub)) if user_sub else ""
    # The conversation lives in the session's MOUNT: a Shared-only chat's in
    # the agent scope even when a person is on the delivery.
    from core.session.visibility import SCOPE_USER
    vis = await run_db(_wake_visibility, agent, username or "", role, user_sub)
    if not await layer.can_resume_session(session_id, agent_name=agent,
                                          username=vis.mount_username):
        logger.info(
            f"Delegate one-shot delivery skipped — session {session_id[:8]} has no "
            f"resumable conversation (agent={agent})"
        )
        return None
    # Reserve before anything is built for the spawn: on a full box the wake
    # waits here like a task (never taking the last slot a person needs) and
    # gives way to a person who opens the chat meanwhile. A wake that gives
    # up returns None: the caller stores it for the chat's next turn.
    from core import concurrency
    state = await concurrency.reserve_background(
        session_id, target=target, execution_path=exec_path,
        timeout_s=config.ADMISSION_WAKE_WAIT_S,
        superseded=lambda: _wake_superseded(session_id, chat_id),
    )
    if state in ("superseded", "timeout"):
        logger.info(
            f"Delegate one-shot delivery deferred ({state}): session={session_id[:8]}, "
            f"chat={chat_id[:8] if chat_id else '-'}"
        )
        return None
    reserved = state == "reserved"
    try:
        spawn_role = (await _role_after_wait(agent, user_sub, role, target, session_id,
                                             agent_state=SCOPE_USER not in vis.available_scopes,
                                             pinned=bool(pin))
                      if user_sub else role)
        started, response = False, None
        if spawn_role is not None:
            started, response = await _spawn_wake_session(
                layer, session_id, agent, result_prompt, user_sub=user_sub, role=spawn_role,
                chat_id=chat_id, chat_row=_chat_row, exec_path=exec_path, target=target,
                username=username or "",
            )
    except BaseException:
        if reserved:
            _release_wake_slot(session_id, chat_id)
        raise
    if not started and reserved:
        _release_wake_slot(session_id, chat_id)
    return response


async def _role_after_wait(agent: str, user_sub: str, role: str, target: str,
                           session_id: str, *, agent_state: bool = False,
                           pinned: bool = False) -> str | None:
    """The role a person's wake spawns at once it holds its slot (it may have
    waited minutes): their agent row now, or their effective role when the
    wake runs from the agent's own state (``agent_state``: a Shared-only
    chat). None when they no longer hold the agent (below the editor tier
    counts as gone from a Shared-only chat), or when the new role would place
    the session elsewhere (a ``pinned`` chat's placement is its own)."""
    from storage import remote_store
    from storage.pg import run_db
    standing, row = await run_db(_standing_of, user_sub, agent)
    if _wake_refused(standing, shared=agent_state):
        logger.info(
            f"Delegate one-shot delivery dropped: {user_sub[:8]} no longer holds "
            f"agent '{agent}': session={session_id[:8]}"
        )
        return None
    fresh = standing if agent_state else (row or roles.VIEWER)
    if fresh != role and not pinned:
        fresh_target, _ = await run_db(
            remote_store.resolve_execution_target, agent, user_sub, fresh)
        if fresh_target != target:
            logger.info(
                f"Delegate one-shot delivery deferred: the role change moves session "
                f"{session_id[:8]} to another machine"
            )
            return None
    return fresh


async def _spawn_wake_session(
    layer: "ExecutionLayer", session_id: str, agent: str, result_prompt: str, *,
    user_sub: str | None, role: str, chat_id: str, chat_row: dict | None,
    exec_path: str, target: str, username: str,
) -> tuple[bool, str | None]:
    """Build the resumed session's config and start it (headless, or the
    chat's own terminal). Returns whether a session was started and the rung's
    answer; after a start the session's lifecycle owns the slot."""
    from core.events.common_events import TEXT
    from storage import remote_store
    from core.execution_layer import AgentConfig
    from core.config.task_config_builder import build_delivery_security_context
    from storage.pg import run_db
    # The placement of this (possibly remote) one-shot. `target` is already
    # resolved above (local | machine_id; offline returned earlier); the
    # object threads the display and the grants so a remote agent's device
    # MCPs aren't dropped from the resume config + prompt.
    _os_placement = await run_db(remote_store.placement_of, target, user_sub, agent)
    _os_is_remote = _os_placement.is_remote
    _os_browser = await run_db(remote_store.get_target_browser_settings, _os_placement)
    # Who the session is: the person on the delivery (their own tree on a
    # personal chat, the agent scope on a Shared-only one) or nobody.
    vis = await run_db(_wake_visibility, agent, username, role, user_sub)
    # Close/reap dropped the session's security context (JWT-replay defense):
    # rebuilt for the resumed turn, or every hook fail-closes with "Session
    # is no longer active" and the callback runs tool-dead. The prompt's
    # identity sections are rendered from it.
    oneshot_security = await build_delivery_security_context(
        agent, user_sub=user_sub, role=role, target=target,
    )
    # In the engine's own format (Codex reads TOML), as every config builder
    # does, for the delivery's person at the session's mount scope: at agent
    # scope the MCP accounts are the agent's service bindings, at user scope
    # the person's own. All five values are kept: the credential environment
    # and the per-MCP secret bundles reach the session through its config.
    from core.session.session_manager import capabilities_for_path
    _mcp_format = capabilities_for_path(exec_path).mcp_config_format
    targets = await run_db(_wake_delegation_targets, agent, user_sub, role)
    mcp_config, flat_env, excluded, secret_bundles, bash_env_keys = await asyncio.to_thread(
        functools.partial(
            _wake_mcp_build, agent, user_sub, username=username, role=role, vis=vis,
            chat_id=chat_id, where=_os_placement, browser=_os_browser,
            mcp_format=_mcp_format, session_id=session_id, targets=targets,
        ))
    contexts = await _wake_dynamic_contexts(
        agent, excluded or {}, user_sub=user_sub, role=role, targets=targets,
        where=_os_placement)
    agent_prompt = await asyncio.to_thread(functools.partial(
        _wake_prompt, agent, role=role, vis=vis, where=_os_placement, exec_path=exec_path,
        security=oneshot_security, excluded=excluded or {}, dynamic_contexts=contexts,
    ))
    mcp_config, credential_env, multi_value_envs, token_minted_at = await asyncio.to_thread(
        functools.partial(
            _wake_credential_env, agent, session_id,
            user_sub=user_sub, role=role, vis=vis, where=_os_placement,
            mcp_config=mcp_config, flat_env=flat_env,
            bash_env_keys=bash_env_keys, mcp_format=_mcp_format,
        ),
    )
    # A LOCAL one-shot resume MUST run sandboxed + network-isolated like every
    # other session — the local layers fail closed without a sandbox dir. Resolve
    # the SAME persistent config dir the session was built with (its mount:
    # the person's tree for a personal chat, else agent/workspace) so --resume
    # finds its conversation. Remote targets ignore this (the satellite owns
    # the config).
    oneshot_claude_dir = ""
    if not _os_is_remote:
        from core.sandbox.sandbox import ensure_persistent_agent_dir
        _hcd = await asyncio.to_thread(
            ensure_persistent_agent_dir, agent,
            execution_path=exec_path,
            username=vis.mount_username,
            scope=vis.mount_scope,
        )
        oneshot_claude_dir = str(_hcd)
    # The CHAT's pinned model: an empty model falls back to the AGENT
    # default, which belongs to the agent's default LAYER; on a chat
    # pinned to the other CLI that yields an unusable/empty model
    # ("API Error: 400 model: String should have at least 1 character").
    _os_model = (chat_row or {}).get("model") or ""
    # The resumed session draws on a subscription like every other spawn:
    # the person's own when the delivery names one (the sender pays on a
    # Shared-only chat too, as their own turn there does), the platform pool
    # otherwise. The layer binds the seat at start and the session's close
    # releases it; every exit below that starts nothing gives it back here.
    # Without usable credentials nothing spawns (the wake is stored for the
    # chat's next turn, which fails visibly if it has none either).
    from services.engines import subscription_pool
    from core.config.config_builder import release_config_seat
    from storage.agents import agent_store
    creds_sub = user_sub or None
    try:
        subscription_id, sub_env = await asyncio.to_thread(
            subscription_pool.resolve_subscription_env, exec_path, creds_sub,
            model=_os_model, agent_info=await run_db(agent_store.get_agent, agent),
            sticky_scope=subscription_pool.credential_scope_key(target, oneshot_claude_dir),
        )
    except subscription_pool.NoSubscriptionError as e:
        logger.warning(
            f"Delegate one-shot delivery skipped: no credentials for the wake "
            f"(agent={agent}, session={session_id[:8]}): {e}"
        )
        return False, None
    if creds_sub and not subscription_id:
        logger.warning(
            f"Delegate one-shot delivery skipped: {creds_sub[:8]} has no usable "
            f"subscription on {exec_path} (agent={agent}, session={session_id[:8]})"
        )
        return False, None
    oneshot_cfg = AgentConfig(
        agent_name=agent,
        user_sub=user_sub or "",
        system_prompt=agent_prompt or "",
        mcp_config_path=str(mcp_config) if mcp_config else "",
        credential_env=credential_env,
        mcp_secret_bundles=secret_bundles or {},
        multi_value_envs=multi_value_envs,
        permission_mode="auto",
        client_type="",
        resume=True,
        security_context=oneshot_security,
        execution_target=target,
        execution_path=exec_path,
        sandbox_host_claude_dir=oneshot_claude_dir,
        model=_os_model,
        # The chat's own thread: without it a Codex resume starts a new one
        # (Claude resumes by the session id and stores none).
        resume_handle=(chat_row or {}).get("codex_thread_id") or "",
        token_minted_at=token_minted_at,
        extra_env=sub_env,
        subscription_id=subscription_id,
        subscription_user_sub=creds_sub or "",
    )
    # A chat pinned interactive gets its wake in ITS OWN MODE: re-warm the
    # terminal session, inject the result, await the orchestrator's turn,
    # close (unless a viewer attached meanwhile). The headless echo below
    # would run the wake invisibly outside the terminal the chat lives in
    # (previously: "interactive resume/delegation is future work"). On any
    # failure return None — the caller's path="none" stores the wake for
    # durable replay at the chat's next warmup.
    from core import execution_mode as _exec_mode
    # Until the layer binds the seat at start (or the re-warm takes it over),
    # every exit gives it back: a superseded wake, a raise, a cancel.
    handed_over = False
    try:
        if _wake_superseded(session_id, chat_id):
            logger.info(
                f"Delegate one-shot delivery deferred (superseded): session={session_id[:8]}, "
                f"chat={chat_id[:8] if chat_id else '-'}"
            )
            return False, None
        if chat_row and await run_db(functools.partial(
            _exec_mode.is_interactive, chat_override=chat_row.get("execution_mode") or None,
        )):
            # The re-warm owns the seat from here: a failed one closes any
            # terminal it started (the close releases) and returns the seat
            # of one it never started.
            handed_over = True
            out = await _rewarm_interactive_and_wake(
                layer, session_id, agent, result_prompt,
                chat_id=chat_id, base_cfg=oneshot_cfg, chat_row=chat_row,
            )
            return out is not None, out
        await layer.start_session(session_id, oneshot_cfg)
        handed_over = True
    finally:
        if not handed_over:
            release_config_seat(session_id, oneshot_cfg)
    if chat_id:
        return True, await _run_echo_turn_pumped(
            layer, session_id, chat_id, agent, result_prompt, person=user_sub or "",
        )
    parts: list[str] = []
    async with layer.session_lock(session_id):
        async for event in layer.send_message(session_id, result_prompt):
            if event.type == TEXT:
                content = event.data.get("content", "")
                if content:
                    parts.append(content)
    return True, "".join(parts) if parts else ""


# Turn-open verification window for interactive wakes: covers CLI spawn →
# readiness gate → paste flush → one transcript-tail poll of lag. Two windows
# run back to back (initial submit, then the bare-Enter nudge).
_WAKE_TURN_OPEN_S = 45.0


async def _wait_turn_open(isess, timeout: float) -> bool:
    """Poll until the session's transcript-derived turn-open flag flips."""
    waited = 0.0
    while waited < timeout:
        if isess.turn_open:
            return True
        if not isess.alive:
            return False
        await asyncio.sleep(1.0)
        waited += 1.0
    return bool(isess.turn_open)


async def _rewarm_interactive_and_wake(
    layer, session_id: str, agent: str, result_prompt: str,
    *, chat_id: str, base_cfg, chat_row: dict,
) -> str | None:
    """Re-warm a DEAD interactive-pinned chat and inject the delegate wake.

    The interactive sibling of the headless one-shot: resume the chat's
    session as a PTY (``interactive=True``, chat-bound, the chat's own
    permission mode + baked TUI theme), submit the wake with the settle
    Enter, VERIFY the turn opened (transcript-derived — an unverified submit
    silently loses the payload), await the orchestrator's continuation turn
    (``interactive._run_interactive_task`` shape: turn-end callback + liveness poll + max
    backstop), then leave the terminal to the idle reaper so a user opening
    the chat shortly after still attaches to it. The chat-bound tailer
    persists the whole exchange to chat history as it runs.

    A ``warmup_registry`` entry brackets the spawn so a racing delivery's
    ``_oneshot_blocked`` defers to this re-warm and a dashboard opening the
    chat mid-wake attaches instead of double-spawning.

    Returns "" on success (the tailer persisted the turn — no echo row to
    save) or None on any failure (the caller stores the wake for durable
    replay)."""
    from core.session import interactive_session, warmup_registry
    from core.config.config_builder import release_config_seat

    cfg = base_cfg
    cfg.interactive = True
    # The chat binding is LOAD-BEARING: the InteractiveSession registers with
    # cfg.chat_id, and transcript tailing → chat_messages, the turn-open /
    # turn-complete signals, the streaming-status broadcasts AND the
    # dashboard's pty_attach chat guard all gate on it. Without it the wake
    # turn runs invisibly and unpersisted, and an open chat refuses to attach
    # to the live terminal (the original wake-recovery shipped that way).
    cfg.chat_id = chat_id
    # The wake turn runs unattended — keep the task-grade "auto" permission
    # mode from base_cfg (a "default"-mode prompt would sit unanswered with
    # nobody watching; same rationale as headless task re-warms).
    cfg.interactive_theme = chat_row.get("tui_theme") or ""

    registered = False
    spawned = False
    try:
        try:
            await warmup_registry.register(chat_id, cfg.user_sub or "", agent)
            registered = True
        except Exception:
            pass  # registry is an optimization, never a spawn blocker

        await layer.start_session(session_id, cfg)
        spawned = True
        isess = interactive_session.get(session_id)
        if isess is None:
            logger.warning(
                f"Delegate interactive re-warm: no PTY registered for "
                f"session={session_id[:8]} (chat={chat_id[:8]})"
            )
            return None

        done = asyncio.Event()
        isess.on_turn_complete = lambda _msg: done.set()
        # Cold wake prompt: buffered behind the readiness gate, flushed with
        # the settle Enter (large pastes were losing the fixed-gap Enter).
        isess.submit_prompt(result_prompt, settle=True)
        logger.info(
            f"Delegate wake re-warmed interactive: session={session_id[:8]}, "
            f"chat={chat_id[:8]}, agent={agent}"
        )
        if registered:
            # Spawn is live — release the registry so the queued-prompt drain
            # and dashboard attach gates stop deferring to it.
            await warmup_registry.unregister(chat_id)
            registered = False

        # Verify the turn actually OPENED before counting the wake delivered.
        # turn_open is transcript-derived (both CLIs write the user line at
        # submit), so a paste that never submitted leaves it False while the
        # PTY idles — previously that "delivery" silently lost the payload.
        # One bare-Enter nudge covers a paste that ate its settle Enter.
        if not await _wait_turn_open(isess, _WAKE_TURN_OPEN_S):
            if isess.alive:
                isess.submit_prompt("", settle=True)
            if not await _wait_turn_open(isess, _WAKE_TURN_OPEN_S):
                logger.warning(
                    f"Delegate wake never opened a turn (paste lost?): "
                    f"session={session_id[:8]}, chat={chat_id[:8]} — storing "
                    f"for durable replay"
                )
                if isess.alive and not isess.has_viewer:
                    await interactive_session.close_session(
                        session_id, reason="delegate_wake_unsubmitted",
                    )
                return None

        waited = 0.0
        _STEP = 5.0
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=_STEP)
                break  # orchestrator's continuation turn completed
            except asyncio.TimeoutError:
                if not isess.alive:
                    logger.warning(
                        f"Delegate wake interactive session died mid-turn: "
                        f"session={session_id[:8]} (chat={chat_id[:8]})"
                    )
                    # The turn opened; the tailer persisted whatever ran.
                    # Treat as delivered — a re-store would double-wake.
                    return ""
                waited += _STEP
                if waited >= interactive.INTERACTIVE_TASK_MAX_S:
                    logger.warning(
                        f"Delegate wake turn exceeded {int(interactive.INTERACTIVE_TASK_MAX_S)}s "
                        f"backstop: session={session_id[:8]} — leaving session to "
                        f"the idle reaper"
                    )
                    return ""

        # Turn done. Leave the terminal ALIVE either way — the idle reaper
        # owns its end. A user opening the chat within the idle window
        # attaches to the real terminal with the wake turn on screen instead
        # of finding it torn down the instant the turn ended.
        logger.info(
            f"Delegate wake turn complete: session={session_id[:8]}, "
            f"viewer={'yes' if isess.has_viewer else 'no'} — terminal left "
            f"to the idle reaper"
        )
        return ""
    except asyncio.CancelledError:
        # Cancelled before the terminal started: the seat the config holds
        # goes back (a started terminal's close releases its own).
        if not spawned:
            release_config_seat(session_id, cfg)
        raise
    except Exception:
        logger.exception(
            f"Delegate interactive re-warm failed: session={session_id[:8]}, "
            f"chat={chat_id[:8]}"
        )
        if spawned:
            try:
                isess = interactive_session.get(session_id)
                if isess is not None and not isess.has_viewer:
                    await interactive_session.close_session(
                        session_id, reason="delegate_wake_failed",
                    )
            except Exception:
                pass
        else:
            # Nothing started: the seat the config holds goes back.
            release_config_seat(session_id, cfg)
        return None
    finally:
        if registered:
            with contextlib.suppress(Exception):
                await warmup_registry.unregister(chat_id)
