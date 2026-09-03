"""Session-delivery primitive: route a server-originated prompt to a chat.

``deliver_prompt`` is the ONE ladder every server-originated in-context prompt
walks — delegation results today; session interjections and schedules-mcp
wake-ups later. Callers describe WHAT to deliver (the prompt text, an optional
UI event, an optional exactly-once persistence hook); this module decides the
best available HOW:

1. **Interactive PTY** — a live PTY owns the chat's session: queue the prompt
   on it for injection at CLI quiescence (visible pasted text in the terminal;
   ``interactive_session._try_drain_prompt_queue`` owns the gating). This rung
   MUST come first: a viewed interactive chat also has a dashboard notify
   queue, but the WS handler's synthesis turn dead-ends for PTY sessions —
   and the one-shot rung below would fork the live TUI's transcript.
2. **WS notify** — a dashboard socket is connected for the target session: the
   notification queue carries the payload and the dashboard handler drives the
   turn (it also owns persistence on this path — see ``persist_event``).
3. **Pump** — a turn is streaming on the chat: queue the prompt for in-context
   delivery after the current turn (no user bubble).
4. **Persistent** — the session's engine process is alive but idle: run the
   echo turn through a headless ChatStreamPump on the chat (direct
   ``send_message`` collection only for chat-less callers).
5. **One-shot** — the session is dead: ``--resume``, then the same headless
   pump turn. Guarded against a live PTY anywhere on the chat (dual-writer
   fork) and an in-flight warmup (the mode-toggle re-warm window). The pump
   is what makes these turns VISIBLE — chat_status broadcasts, live attach
   for a viewer opening the chat mid-turn, unread stamping; the pre-pump
   direct collection ran them silently (2026-07-13 incident).

The persistent/one-shot rungs are INJECTED callables (``persistent_fn`` /
``oneshot_fn``) rather than imports: they carry caller-specific identity
resolution (the scheduler's task-scope user/role), and the scheduler's tests
monkeypatch them on the scheduler module — resolving them by name at each call
keeps that surface intact.

Event-persistence asymmetry (deliberate, mirrors the pre-extraction ladder):
the WS rung does NOT invoke ``persist_event`` — the dashboard handler persists
after rendering; every other chosen path invokes it exactly once BEFORE the
delivery attempt, so the caller's event (e.g. ``delegate_result``) is never
lost even when the echo delivery fails. The PTY rung persists at ENQUEUE time
(the injection may happen much later, or be handed back on close).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("claude-proxy.session-delivery")

# Injected delivery callables:
# (session_id, agent, text, *, user_sub, role, chat_id) -> response|None.
# With a chat_id the rung runs the echo turn through a headless pump and
# returns "" (the pump persisted it); chat-less calls return the collected
# response text for the pending-result flow. None = rung could not deliver.
DeliverFn = Callable[..., "Awaitable[Optional[str]]"]

# In-flight one-shot echo turns, keyed by chat id. An interactive re-warm that
# starts while a one-shot ``--resume`` is still writing the same JSONL renders
# the transcript as-of-open and never re-reads it — the delivered turn is then
# invisible until the next toggle (and the two CLIs dual-write the file). The
# warmup awaits this event (``oneshot_inflight``) before resuming; the one-shot
# registers BEFORE its ``_oneshot_blocked`` check and releases the claim the
# moment it refuses, so a concurrent start resolves deterministically and no
# wait cycle exists (the one-shot never waits while holding a claim).
_oneshot_inflight: dict[str, asyncio.Event] = {}

# How long a refused one-shot waits for the blocking warmup to settle before
# giving up on the PTY retry (the event row is already persisted either way).
_ONESHOT_WARMUP_WAIT_S = 90.0


def oneshot_inflight(chat_id: str) -> "asyncio.Event | None":
    """The in-flight one-shot delivery on ``chat_id``, or None. Warmups await
    the returned event (set on completion) before resuming the session."""
    return _oneshot_inflight.get(chat_id) if chat_id else None


@dataclass
class DeliveryOutcome:
    """Which rung delivered, plus the anchors the caller needs afterwards.

    ``path``: "pty" | "ws" | "steer" | "pump" | "persistent" | "oneshot" |
    "none". "steer" = injected INTO the chat's running turn (Codex
    turn/steer) — consumed at the next sampling-round boundary, exactly-once.
    ``response``: the assistant echo text when a headless echo turn ran
    (persistent/one-shot rungs); None otherwise — including a failed echo and
    the deferred paths (pty/ws/steer/pump), whose response arrives via their
    own machinery.
    ``chat_id`` / ``session_id``: the RESOLVED anchors (the chat row may point
    at a newer session than the caller knew about) — use these to persist an
    echo or a pending result.
    """
    path: str
    response: str | None = None
    chat_id: str = ""
    session_id: str = ""


async def deliver_prompt(
    chat_id: str,
    text: str,
    *,
    source: str,
    session_id: str = "",
    agent: str = "",
    user_sub: str | None = None,
    role: str = "manager",
    notify_payload: dict | None = None,
    pump_event: dict | None = None,
    persist_event: Callable[[str], None] | None = None,
    persistent_fn: DeliverFn | None = None,
    oneshot_fn: DeliverFn | None = None,
    on_outcome: Callable[[DeliveryOutcome], "Optional[Awaitable[None]]"] | None = None,
    allow_pty: bool = True,
    allow_ws: bool = True,
    hops: int = 0,
) -> DeliveryOutcome:
    """Deliver ``text`` to the chat's session over the best available path.

    ``source`` tags the origin ("delegate_result", …) for logging and future
    per-source routing. ``notify_payload`` is the complete WS-rung notification
    message — its ``session_id``/``chat_id`` keys are overwritten with the
    RESOLVED anchors so the dashboard handler always targets the originating
    chat's current session. ``pump_event`` is a UI event pushed to the live
    pump alongside the WS/pump rungs (best-effort). ``persist_event`` receives
    the resolved chat id ("" when none could be resolved).

    ``on_outcome`` is carried with a PTY-queued item so a close-time handback
    can finish the caller's post-delivery work (e.g. persisting the echo) —
    the direct return value covers the immediate paths.

    ``allow_pty``/``allow_ws``/``hops`` exist for the close-handback re-entry
    (``interactive_session._redeliver_pending``): a handed-back item must not
    ping-pong onto a successor PTY or into the dead-end WS handler, and
    ``hops`` caps the close→handback→close loop.
    """
    from core.session.session_state import (
        _dashboard_notify_queues,
        push_pump_event,
        queue_pump_prompt,
    )
    from storage import database as task_store

    # Exactly-once event persistence across whichever rung is chosen.
    _persisted = False

    def _persist_once(target: str) -> None:
        nonlocal _persisted
        if not _persisted and persist_event is not None:
            _persisted = True
            persist_event(target)

    # Resolve the chat's CURRENT session — it may have been replaced
    # (reconnect, re-warm) since the caller captured its anchor.
    chat = None
    if chat_id:
        chat = await asyncio.to_thread(task_store.get_chat, chat_id)
    target_chat_id = chat_id
    if not target_chat_id and session_id:
        by_session = await asyncio.to_thread(task_store.get_chat_by_session, session_id)
        target_chat_id = by_session["id"] if by_session else ""

    # Sibling-awareness piggyback: server-originated prompts carry the changed
    # parallel-activity line too — the PTY rung writes raw bytes and never
    # passes the layer prepend sites. Hash-deduped per chat, so a downstream
    # layer-site injection won't double it.
    if target_chat_id:
        from core.session import sibling_awareness
        text = await sibling_awareness.prepend_if_changed(target_chat_id, text)

    # Rung 1: a live interactive PTY owns this chat — queue for quiescence
    # injection. Chat-first lookup (newest-alive) is robust to session-id drift
    # across mode toggles / re-warms; the sid lookup covers chat-less callers.
    if allow_pty:
        from core.session import interactive_session
        isess = interactive_session.find_live_for_chat(target_chat_id) if target_chat_id else None
        if isess is None and session_id:
            isess = interactive_session.get(session_id)
        if isess is not None and isess.alive:
            # steer=True: a LOCAL PTY with an open turn injects mid-turn (both
            # TUIs consume typed input between tool calls) — mirrors rung 3a's
            # steer-first semantics for headless. With the turn closed, or on
            # a satellite-attached PTY (turn-end inject by design), the flag
            # is inert and the item waits for quiescence as before.
            queued = isess.queue_prompt(
                text, source, steer=True,
                chat_id=target_chat_id or "",
                agent=agent, user_sub=user_sub, role=role, hops=hops,
                persistent_fn=persistent_fn, oneshot_fn=oneshot_fn,
                on_outcome=on_outcome,
            )
            if queued:
                # Persist right after the enqueue (synchronous — the drain task
                # can't run before this coroutine yields): the injection may
                # happen much later, but the event is durable now. A FAILED
                # queue falls through un-persisted so the WS rung's handler
                # can't double-persist.
                _persist_once(target_chat_id or "")
                logger.info(
                    f"deliver_prompt[{source}]: queued on interactive PTY, "
                    f"session={isess.session_id[:8]}"
                )
                outcome = DeliveryOutcome(
                    "pty", chat_id=target_chat_id or "",
                    session_id=isess.session_id,
                )
                return await _finish(outcome, on_outcome)
            # Session died between lookup and queue — fall through (the event
            # is persisted; later rungs skip the duplicate via _persist_once).

    # Rung 2: WS connected — the dashboard handler drives the turn AND owns
    # the event persistence on this path. The chat's CURRENT session is
    # preferred over the caller's captured anchor: a queue registered under a
    # replaced/reaped sid is stale (registrations are never cleaned on session
    # close), and routing under it hands the handler a dead target.
    notify_queue = None
    if chat and chat.get("session_id"):
        current_sid = chat["session_id"]
        queue = _dashboard_notify_queues.get(current_sid)
        if queue:
            notify_queue = queue
            session_id = current_sid
    if not notify_queue and session_id:
        notify_queue = _dashboard_notify_queues.get(session_id)

    logger.info(
        f"deliver_prompt[{source}]: session={session_id[:8] if session_id else '-'}, "
        f"chat={target_chat_id[:8] if target_chat_id else '-'}, "
        f"notify_queue={'found' if notify_queue else 'not found'}"
    )

    # Liveness gate: the WS handler can only run the turn on a session that is
    # actually alive — a dead/reaped target must take the ladder instead
    # (persistent/one-shot warm it, or the durable wake replays on the next
    # warmup). Only a POSITIVE dead verdict skips the route; a resolution
    # failure keeps the old behavior.
    if notify_queue and session_id:
        try:
            from core.session.session_manager import (
                get_layer_by_path, resolve_execution_path,
            )
            _ws_layer = get_layer_by_path(resolve_execution_path(
                (chat or {}).get("agent", ""),
                (chat or {}).get("execution_path", ""),
            ))
            if not await _ws_layer.is_session_alive(session_id):
                logger.info(
                    f"deliver_prompt[{source}]: session {session_id[:8]} not "
                    f"alive — skipping WS route, using the ladder"
                )
                notify_queue = None
        except Exception:
            logger.warning(
                f"deliver_prompt[{source}]: liveness resolution failed for "
                f"session {session_id[:8]} — keeping the WS route", exc_info=True,
            )

    if allow_ws and notify_queue and notify_payload is not None:
        if target_chat_id and pump_event:
            push_pump_event(target_chat_id, pump_event)
        notify_payload["session_id"] = session_id
        notify_payload["chat_id"] = chat_id or None
        await notify_queue.put(notify_payload)
        logger.info(
            f"deliver_prompt[{source}]: routed via dashboard WS, session={session_id[:8]}"
        )
        return await _finish(
            DeliveryOutcome("ws", chat_id=target_chat_id or "", session_id=session_id),
            on_outcome,
        )

    # Rungs 3-5 persist the caller's event EXACTLY ONCE before attempting
    # delivery — the event is the source of truth; the echo below is optional.
    _persist_once(target_chat_id or "")

    # Rung 3a: a pump is streaming this chat AND the engine supports mid-turn
    # steering (Codex turn/steer) — the prompt goes INTO the running turn,
    # consumed at the next sampling-round boundary. Accept = exactly-once
    # (never also queued); reject (turn just ended, review/compaction turn,
    # unsupported engine) falls through to the post-turn queue.
    if target_chat_id:
        from core.events.stream_pump import _active_pumps
        from core.session.session_manager import (
            get_layer_by_path, resolve_execution_path,
        )
        _pump = _active_pumps.get(target_chat_id)
        if _pump is not None and not _pump.is_done:
            _layer = get_layer_by_path(resolve_execution_path(
                (chat or {}).get("agent", ""),
                (chat or {}).get("execution_path", ""),
            ))
            if await _layer.steer(_pump.session_id, text):
                if pump_event:
                    push_pump_event(target_chat_id, pump_event)
                logger.info(
                    f"deliver_prompt[{source}]: steered into live turn, "
                    f"chat={target_chat_id[:8]}"
                )
                return await _finish(
                    DeliveryOutcome(
                        "steer", chat_id=target_chat_id,
                        session_id=_pump.session_id,
                    ),
                    on_outcome,
                )

    # Rung 3: a pump is streaming this chat — queue for in-context delivery
    # after the current turn (system=True: no user bubble). Only pumps whose
    # producer drains system_queue accept (pump.system_queue_consumer) — a
    # task/scheduler pump refuses instead of swallowing the prompt forever.
    if target_chat_id and queue_pump_prompt(target_chat_id, text, system=True):
        if pump_event:
            push_pump_event(target_chat_id, pump_event)
        logger.info(
            f"deliver_prompt[{source}]: queued on pump, chat={target_chat_id[:8]}"
        )
        return await _finish(
            DeliveryOutcome("pump", chat_id=target_chat_id, session_id=session_id),
            on_outcome,
        )

    # A live pump we could NOT queue on (non-consumer producer — a mid-run
    # task lane receiving a nested delegate's result) also blocks the
    # persistent/one-shot rungs: both would drive a SECOND producer onto the
    # same chat/session (pump-registry clobber + dual-writer). Land on "none"
    # — the persisted event carries the result and the caller parks a durable
    # wake for the next turn chokepoint.
    if target_chat_id:
        from core.events.stream_pump import _active_pumps as _pumps_now
        _live_pump = _pumps_now.get(target_chat_id)
        if _live_pump is not None and not _live_pump.is_done:
            logger.info(
                f"deliver_prompt[{source}]: live non-consumer pump on "
                f"chat={target_chat_id[:8]} — deferring to durable wake"
            )
            return await _finish(
                DeliveryOutcome("none", chat_id=target_chat_id, session_id=session_id),
                on_outcome,
            )

    # Rung 4: persistent session alive, no pump. With a chat the rung runs a
    # headless pump turn (visible: chat_status, live attach, unread) and
    # returns "" — the pump persisted the echo itself; the chat-less form
    # collects the response text directly for the pending-result flow.
    response: str | None = None
    path = "none"
    if persistent_fn is not None:
        response = await persistent_fn(
            session_id, agent, text, user_sub=user_sub, role=role,
            chat_id=target_chat_id or "",
        )
        if response is not None:
            path = "persistent"

    # Rung 5: session dead — one-shot --resume. Guarded: a --resume while a
    # live PTY holds this session/chat would spawn a SECOND CLI on the same
    # transcript (dual-writer fork); an in-flight warmup (mode-toggle re-warm)
    # is about to own it the same way. A refusal no longer strands the echo:
    # the event is already persisted, and when the blocker is a warmup/live
    # PTY we wait for it to settle and queue on the fresh session's PTY
    # instead (the operator's design: interactive chats get their delegate
    # results interactively).
    if response is None and oneshot_fn is not None:
        inflight_evt: asyncio.Event | None = None

        def _release_inflight() -> None:
            nonlocal inflight_evt
            if inflight_evt is None:
                return
            if _oneshot_inflight.get(target_chat_id) is inflight_evt:
                _oneshot_inflight.pop(target_chat_id, None)
            inflight_evt.set()
            inflight_evt = None

        if target_chat_id:
            # Registered BEFORE the blocked check (see _oneshot_inflight).
            inflight_evt = asyncio.Event()
            _oneshot_inflight[target_chat_id] = inflight_evt
        try:
            if _oneshot_blocked(session_id, target_chat_id):
                _release_inflight()  # refusing — never wait while holding a claim
                logger.info(
                    f"deliver_prompt[{source}]: one-shot refused — live interactive "
                    f"session/warmup holds chat={target_chat_id[:8] if target_chat_id else '-'}"
                )
                if allow_pty and target_chat_id:
                    retry = await _queue_on_pty_after_warmup(
                        target_chat_id, text, source,
                        agent=agent, user_sub=user_sub, role=role, hops=hops,
                        persistent_fn=persistent_fn, oneshot_fn=oneshot_fn,
                        on_outcome=on_outcome,
                    )
                    if retry is not None:
                        return await _finish(retry, on_outcome)
            else:
                response = await oneshot_fn(
                    session_id, agent, text, user_sub=user_sub, role=role,
                    chat_id=target_chat_id or "",
                )
                if response is not None:
                    path = "oneshot"
        finally:
            _release_inflight()

    return await _finish(
        DeliveryOutcome(
            path, response=response,
            chat_id=target_chat_id or "", session_id=session_id,
        ),
        on_outcome,
    )


async def _queue_on_pty_after_warmup(
    chat_id: str,
    text: str,
    source: str,
    **context,
) -> "DeliveryOutcome | None":
    """A refused one-shot's recovery: wait (bounded) for the blocking warmup
    to settle, then queue the prompt on the chat's live PTY for quiescence
    injection. Returns the ``pty`` outcome, or None when no live session
    materialized (caller falls through to the plain refusal — the event row
    is already persisted)."""
    from core.session import interactive_session, warmup_registry

    deadline = time.monotonic() + _ONESHOT_WARMUP_WAIT_S
    while warmup_registry.get(chat_id) is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    isess = interactive_session.find_live_for_chat(chat_id)
    if isess is None or not isess.alive:
        return None
    if not isess.queue_prompt(text, source, chat_id=chat_id, **context):
        return None
    logger.info(
        f"deliver_prompt[{source}]: one-shot refusal recovered — queued on the "
        f"warmed PTY, session={isess.session_id[:8]}, chat={chat_id[:8]}"
    )
    return DeliveryOutcome("pty", chat_id=chat_id, session_id=isess.session_id)


def _oneshot_blocked(session_id: str, chat_id: str) -> bool:
    """True when a ``--resume`` one-shot would dual-write a live interactive
    session's transcript (same sid OR any live PTY on the chat — mode toggles
    reuse the JSONL under a new sid) or race an in-flight warmup re-warm."""
    from core.session import interactive_session, warmup_registry

    live = interactive_session.get(session_id) if session_id else None
    if live is not None and live.alive:
        return True
    if chat_id:
        by_chat = interactive_session.find_live_for_chat(chat_id)
        if by_chat is not None:
            return True
        if warmup_registry.get(chat_id) is not None:
            return True
    return False


async def _finish(
    outcome: DeliveryOutcome,
    on_outcome: Callable[[DeliveryOutcome], "Optional[Awaitable[None]]"] | None,
) -> DeliveryOutcome:
    """Run the caller's post-delivery hook (echo persistence etc.) and return
    the outcome. The hook also rides PTY-queued items so a close-time handback
    finishes the same work — call sites must therefore tolerate both timings."""
    if on_outcome is not None:
        try:
            res = on_outcome(outcome)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            logger.exception("deliver_prompt: on_outcome hook failed")
    return outcome
