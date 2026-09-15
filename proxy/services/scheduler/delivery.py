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
import json
import logging
from datetime import datetime, timezone


import config
from storage import database as task_store
from services.scheduler import interactive, lanes, shared

logger = logging.getLogger("claude-proxy.scheduler")


async def _deliver_task_result(task: shared.TaskDefinition, final_status: str, output_text: str,
                               *, worker_chat_id: str = "", output_cursor: int = 0,
                               prompt_row_id: int = 0, prompt_text: str = "") -> None:
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
    ``{{chat_id}}`` template token.
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
                if status == "user_interrupted":
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
                if chat and chat.get("last_turn_aborted"):
                    status = "user_interrupted"
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
        result_prompt = (on_complete_prompt
            .replace("{{output}}", output or "")
            .replace("{{task_id}}", task.id)
            .replace("{{task_name}}", task.name)
            .replace("{{status}}", status)
            .replace("{{chat_id}}", worker_chat_id or "")
            .replace("{{agent}}", task.agent))
        await _do_deliver(
            on_complete_session_id, on_complete_agent, result_prompt, task,
            chat_id=on_complete_chat_id,
            output_text=output,
            status=status,
        )

    asyncio.create_task(_finalize_and_deliver())


async def _do_deliver(session_id: str, agent: str, result_prompt: str, task: shared.TaskDefinition,
                      *, chat_id: str | None = None, output_text: str = "",
                      status: str = "completed") -> None:
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
            "type": "delegate_result",
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
        }
        delegate_event_data = json.dumps({
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
        })

        def _persist_delegate_event(target_chat_id: str) -> None:
            # The delegate_result event is the source of truth — it renders the
            # delegate's output and completes the badge regardless of whether
            # the LLM-echo delivery succeeds. Invoked exactly once by the
            # ladder on every non-WS path (the dashboard handler persists it
            # on the WS path after rendering).
            if target_chat_id:
                task_store.add_chat_message(target_chat_id, "event", "",
                    event_type="delegate_result",
                    event_data=delegate_event_data)

        # Resolve with the originating session's user + role so a user-pinned
        # remote target (or admin-default remote) is honored on the
        # persistent/one-shot rungs — a bare resolve would default to local and
        # miss a user override / break remote start.
        if task.scope == "user" and task.created_by:
            deliver_user_sub: str | None = task.created_by
            deliver_role = task_store.get_user_agent_roles(task.created_by).get(agent, "viewer")
        else:
            deliver_user_sub = None
            deliver_role = "manager"

        def _save_echo(outcome) -> None:
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
                task_store.add_chat_message(outcome.chat_id, "assistant", outcome.response)
                logger.info(f"Task result echo saved to chat DB: chat={outcome.chat_id[:8]}, task={task.name}")
            else:
                from core.session import session_state as _state_mod
                _state_mod._save_pending_result(outcome.session_id, outcome.response)
                logger.info(f"Task result echo saved as pending: session={outcome.session_id[:8]}, task={task.name}")

        outcome = await deliver_prompt(
            chat_id or "", result_prompt,
            source="delegate_result",
            session_id=session_id,
            agent=agent,
            user_sub=deliver_user_sub,
            role=deliver_role,
            notify_payload={
                "type": "task_result_prompt",
                # Originating (delegating) chat/session so the dashboard handler
                # runs the synthesis turn on THIS chat — not whatever the socket
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
            },
            pump_event=delegate_event,
            persist_event=_persist_delegate_event,
            persistent_fn=_deliver_via_persistent,
            oneshot_fn=_deliver_via_oneshot,
            on_outcome=_save_echo,
        )
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

        # Durable replay: every rung failed — the orchestrator was never woken
        # (the delegate_result event row IS persisted, but no turn ran). Store
        # the wake on the parent chat; the next warmup/turn claims and injects
        # it so the orchestrator continues then (a manual re-warm included).
        if chat_id and outcome.path == "none":
            stored = task_store.append_pending_delegate_wake(chat_id, result_prompt)
            if stored:
                logger.info(
                    f"Delegate wake undeliverable — stored for replay on next "
                    f"warmup: chat={chat_id[:8]}, task={task.name}"
                )
    except Exception as e:
        logger.error(f"Task result delivery failed: session={session_id[:8]}, task={task.id}: {e}", exc_info=True)


async def redeliver_pending_wakes(machine_id: str | None = None) -> int:
    """Redeliver delegate wakes parked on chats (stored when every delivery
    rung failed — typically the proxy restarted inside the delivery window).

    Startup runs it once after boot settles; every satellite reconnect runs it
    scoped to that machine's pinned chats (an idle satellite chat's wake can
    only deliver once its machine is back). Per-chat claims are atomic, so a
    concurrent warmup/turn claim wins cleanly and this pass no-ops for that
    chat. A chat whose session is parked for Mode C re-adopt is skipped —
    the reconnect pass retries it after the adopt settles. Failed deliveries
    re-persist the original wakes (same caller-side contract as
    ``_deliver_task_result``). Returns the number of chats woken."""
    from services.scheduler import run_recovery
    from core.session.session_delivery import deliver_prompt

    rows = await asyncio.to_thread(
        task_store.list_chats_with_pending_wakes, machine_id,
    )
    woken = 0
    for chat in rows:
        chat_id = chat["id"]
        if (chat.get("session_id") or "") in run_recovery._parked:
            continue
        wakes = await asyncio.to_thread(
            task_store.claim_pending_delegate_wake, chat_id,
        )
        if not wakes:
            continue
        agent = chat.get("agent") or ""
        owner = chat.get("user_sub") or ""
        # ``task::`` owners are the unified task chats — agent-scope delivery
        # semantics, never a personal remote-target pin.
        if owner and not owner.startswith("task::"):
            deliver_user_sub: str | None = owner
            role = (await asyncio.to_thread(
                task_store.get_user_agent_roles, owner,
            )).get(agent, "viewer")
        else:
            deliver_user_sub = None
            role = "manager"

        def _save_echo(outcome) -> None:
            # Same contract as _deliver_task_result's echo hook: pump-driven
            # turns return "" (already persisted); dead resumes return None.
            if outcome.response and outcome.chat_id:
                task_store.add_chat_message(
                    outcome.chat_id, "assistant", outcome.response,
                )

        try:
            outcome = await deliver_prompt(
                chat_id, "\n\n".join(wakes),
                source="delegate_result_replay",
                session_id=chat.get("session_id") or "",
                agent=agent,
                user_sub=deliver_user_sub,
                role=role,
                persistent_fn=_deliver_via_persistent,
                oneshot_fn=_deliver_via_oneshot,
                on_outcome=_save_echo,
            )
        except Exception:
            # One chat's broken spawn (bad config, dead layer) must neither
            # kill the sweep nor eat the CLAIMED wakes — re-persist and move
            # on (live-hit on T1: a corrupt user config.toml failed the
            # oneshot and the claimed wake was lost with the sweep).
            logger.exception(
                "Wake redelivery: chat %s delivery raised — re-storing %d "
                "wake(s)", chat_id[:8], len(wakes),
            )
            for w in wakes:
                await asyncio.to_thread(
                    task_store.append_pending_delegate_wake, chat_id, w,
                )
            continue
        if outcome.path == "none":
            for w in wakes:
                await asyncio.to_thread(
                    task_store.append_pending_delegate_wake, chat_id, w,
                )
            logger.info(
                "Wake redelivery: chat %s undeliverable — re-stored %d wake(s)",
                chat_id[:8], len(wakes),
            )
        else:
            woken += 1
            logger.info(
                "Wake redelivery: chat %s woken via %s (%d wake(s))",
                chat_id[:8], outcome.path, len(wakes),
            )
    return woken


async def _run_echo_turn_pumped(layer, session_id: str, chat_id: str,
                                agent: str, result_prompt: str) -> str | None:
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
    """
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.events.task_producer import task_produce
    from core.session import visibility as _vis
    from core.session.session_state import get_permission_queue

    if _active_pumps.get(chat_id) is not None:
        # A turn started on the chat between the ladder's pump check and now —
        # never dual-pump a chat.
        return None
    echo_id = f"echo-{chat_id[:8]}"
    event_queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(task_produce(
        layer, session_id, result_prompt, event_queue, echo_id,
        settle_timeout=30.0,
    ))
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
    # A _TaskTurnStalled reap persisted the partial turn and closed the chat
    # status — delivered as far as it went; never fall through to another rung
    # (that would run a SECOND echo turn).
    with contextlib.suppress(lanes._TaskTurnStalled):
        await lanes._watch_task_pump(layer, pump, echo_id, chat_id, session_id)
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

    try:
        layer = get_execution_layer(agent, user_sub=user_sub, role=role)
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
                layer, session_id, chat_id, agent, result_prompt,
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
    response as a pending result."""
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer, resolve_execution_path
    from core.events.common_events import TEXT
    from services.mcp import mcp_registry
    from storage import remote_store

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
    target, _reason = remote_store.resolve_execution_target(agent, user_sub, role)
    if target.startswith("__offline__:"):
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
    username = task_store.get_username_by_sub(user_sub) if user_sub else ""
    if not await layer.can_resume_session(session_id, agent_name=agent, username=username or ""):
        logger.info(
            f"Delegate one-shot delivery skipped — session {session_id[:8]} has no "
            f"resumable conversation (agent={agent})"
        )
        return None
    from core.execution_layer import AgentConfig
    from core.config.task_config_builder import build_delivery_security_context
    # Device-local MCP placement facts for this (possibly remote) one-shot.
    # `target` is already resolved above (local | machine_id; offline returned
    # earlier), so derive is_remote / display and thread them so a remote
    # agent's device MCPs aren't dropped from the resume config + prompt.
    _os_kind, _ = remote_store.get_target_metadata(target, user_sub, agent)
    _os_is_remote = _os_kind in ("admin_remote", "user_remote")
    _os_has_display = remote_store.get_target_has_display(_os_kind, target)
    _os_grants = remote_store.get_target_device_grants(_os_kind, target)
    _os_browser = remote_store.get_target_browser_settings(_os_kind, target)
    agent_prompt = config.build_agent_prompt(
        agent, client_type="task",
        is_remote=_os_is_remote, target_has_display=_os_has_display,
        target_device_grants=_os_grants,
    )
    mcp_config, _, _, _, _ = mcp_registry.build_session_mcp_config(
        agent, None, task_mode=True, task_scope="agent",
        is_remote=_os_is_remote, target_has_display=_os_has_display,
        target_device_grants=_os_grants, target_browser=_os_browser,
    )
    # A LOCAL one-shot resume MUST run sandboxed + network-isolated like every
    # other session — the local layers fail closed without a sandbox dir. Resolve
    # the SAME persistent config dir the session was built with (user scope when
    # the chat is user-owned, else agent/workspace) so --resume finds its
    # conversation. Remote targets ignore this (the satellite owns the config).
    oneshot_claude_dir = ""
    if not _os_is_remote:
        from core.sandbox.sandbox import ensure_persistent_agent_dir
        _hcd = await asyncio.to_thread(
            ensure_persistent_agent_dir, agent,
            execution_path=exec_path,
            username=username or "",
            scope="user" if username else "agent",
        )
        oneshot_claude_dir = str(_hcd)
    # Close/reap dropped the session's security context (JWT-replay defense) —
    # rebuild it for the resumed turn or every hook fail-closes with
    # "Session is no longer active" and the callback runs tool-dead.
    oneshot_security = await build_delivery_security_context(
        agent, user_sub=user_sub, role=role, target=target,
    )
    oneshot_cfg = AgentConfig(
        agent_name=agent,
        user_sub=user_sub or "",
        system_prompt=agent_prompt or "",
        mcp_config_path=str(mcp_config) if mcp_config else "",
        permission_mode="auto",
        client_type="",
        resume=True,
        security_context=oneshot_security,
        execution_target=target,
        execution_path=exec_path,
        sandbox_host_claude_dir=oneshot_claude_dir,
        # The CHAT's pinned model — an empty model falls back to the AGENT
        # default, which belongs to the agent's default LAYER; on a chat
        # pinned to the other CLI that yields an unusable/empty model
        # ("API Error: 400 model: String should have at least 1 character").
        model=(_chat_row or {}).get("model") or "",
    )
    # A chat pinned interactive gets its wake in ITS OWN MODE: re-warm the
    # terminal session, inject the result, await the orchestrator's turn,
    # close (unless a viewer attached meanwhile). The headless echo below
    # would run the wake invisibly outside the terminal the chat lives in
    # (previously: "interactive resume/delegation is future work"). On any
    # failure return None — the caller's path="none" stores the wake for
    # durable replay at the chat's next warmup.
    from core import execution_mode as _exec_mode
    chat_row = _chat_row
    if chat_row and _exec_mode.is_interactive(
        chat_override=chat_row.get("execution_mode") or None,
    ):
        return await _rewarm_interactive_and_wake(
            layer, session_id, agent, result_prompt,
            chat_id=chat_id, base_cfg=oneshot_cfg, chat_row=chat_row,
        )
    await layer.start_session(session_id, oneshot_cfg)
    if chat_id:
        return await _run_echo_turn_pumped(
            layer, session_id, chat_id, agent, result_prompt,
        )
    parts: list[str] = []
    async with layer.session_lock(session_id):
        async for event in layer.send_message(session_id, result_prompt):
            if event.type == TEXT:
                content = event.data.get("content", "")
                if content:
                    parts.append(content)
    return "".join(parts) if parts else ""


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
        return None
    finally:
        if registered:
            with contextlib.suppress(Exception):
                await warmup_registry.unregister(chat_id)
