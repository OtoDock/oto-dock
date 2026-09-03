"""Full-duplex chat voice — the chat-attach layer.

Runs the LLM half of a duplex session: daemon ``utterance`` frames become
turns on the bridge's chat session (any execution layer), the reply streams
back as ``text``/``tool_start``/``tool_end``/``done`` frames (the exact
``/ws/phone`` vocabulary — the daemon reuses its proven per-turn routing),
and ``abort_turn`` is the mid-generation barge-in.

Deliberate upgrades over the phone path (plan A4):

- **Pump guard, not clobber**: ``proxy/ws/phone.py`` registers its pump
  unconditionally — safe for phone-only chats, wrong for dashboard chats. A
  duplex utterance that finds a LIVE pump queues onto it
  (``pump.queue_message``) and streams that pump's tail instead; voice and
  typed turns serialize on the one-pump-per-chat invariant.
- **Graceful-first abort**: ``layer.abort()`` first (dashboard Stop
  ordering); only a hard abort also kills the pump. CLI/Codex keep the
  partial turn in engine history — real barge-in the raw producer-cancel
  never gave phone. The duplex path NEVER stamps ``last_turn_aborted`` (its
  own interruption prefix carries the fidelity; double-injection guard).
- **Layer-agnostic barge-in fidelity**: ``barge_in_chars`` rides
  ``send_message`` on every layer (direct-llm annotates its in-memory
  history; CLI/Codex ignore the kwarg) — non-direct layers additionally get
  a text note in the next utterance so the engine knows what was heard.
- **Producer drains**: the producer carries the dashboard shape's post-turn
  message/system/artifact drain loop, so a typed message queued mid-voice
  is delivered, never acknowledged-and-dropped.

The duplex context (``chat_duplex_context``) is injected as a
``<system-reminder>`` prefix on the FIRST duplex turn after each attach —
persisted session history carries it forward, so later turns need no repeat.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from storage import database as task_store

logger = logging.getLogger("claude-proxy")


@dataclass
class _AttachState:
    """Per-bridge attach state (created on the first utterance)."""

    chat_id: str = ""
    session_id: str = ""
    agent: str = ""
    execution_path: str = ""
    layer: object = None
    context_injected: bool = False
    interrupted_last_turn: bool = False
    # The pump WE created for the current duplex turn (never a foreign one).
    pump: object = None
    # A FOREIGN live pump the current duplex turn was queued onto (the
    # typed-turn-streaming branch). Recorded so abort_turn can route a
    # barge-in to it — but only while it is still the chat's live pump
    # (identity re-checked at abort time; a newer pump is never killed).
    abort_target: object = None
    forward_task: asyncio.Task | None = None
    # Interactive (PTY) chats: no pump to tap — replies feed from the rows
    # the transcript tailer persists (see on_interactive_batch).
    interactive: bool = False
    row_cursor: int = 0
    active_turn: int | None = None
    # Interactive turn-close gate: a `turn_open=False` batch may precede the
    # turn ever OPENING (cold PTY: the injected prompt's own echoed user row
    # persists seconds before the CLI starts responding) — closing on it sent
    # an empty `done` and the whole spoken reply was never forwarded
    # (live-hit 2026-08-12 06:07). The close edge only counts once the turn
    # was seen open or reply output was forwarded.
    turn_saw_open: bool = False
    feed_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_states: dict[str, _AttachState] = {}
# Interactive feeds: chat_id → bridge (one duplex session per chat).
_interactive_feeds: dict[str, object] = {}


def on_teardown_resume(duplex_id: str) -> bool:
    """The spoken-mode session is closing — do NOT abort an in-flight
    attached turn (operator decision 2026-08-14: exiting phone mode is not
    Stop; the dashboard Stop button owns deliberate aborts). The producer +
    pump keep running, so the turn finishes into the chat and the dashboard
    picks it up through the normal live-pump reattach. A system note rides
    the pump's post-turn drain so the agent learns the mode changed.
    Returns True when a live turn was left running."""
    st = _states.get(duplex_id)
    if st is None or st.pump is None:
        return False
    queue = getattr(st.pump, "system_queue", None)
    if queue is not None:
        queue.append(
            "System note: the user exited spoken phone mode while you were "
            "responding. The conversation continues as regular text chat — "
            "drop the spoken-style delivery from here on. If your previous "
            "reply already covered everything, confirm briefly and stop."
        )
    return True


def on_teardown_state(duplex_id: str) -> None:
    st = _states.pop(duplex_id, None)
    if st is None:
        return
    if st.forward_task is not None:
        st.forward_task.cancel()
    if st.interactive and st.chat_id:
        _interactive_feeds.pop(st.chat_id, None)


async def _engine_send(bridge, payload: dict) -> None:
    ws = bridge.engine_ws
    if ws is None:
        return
    with contextlib.suppress(Exception):
        await ws.send_json(payload)


async def _resolve(bridge) -> "_AttachState | str":
    """Resolve the bridge's chat to (session, layer). Returns the state or a
    refusal reason. Resolution is row/registry-only — LIVENESS is the
    new-turn chokepoint's job (``_ensure_live_session``), which also heals a
    dead process instead of refusing: voice has no warmup path, so a refusal
    here would strand the session until a dashboard typed turn healed it."""
    st = _states.get(bridge.duplex_id)
    # Interactive states have no layer — without the second clause every
    # utterance re-resolved into a FRESH state and re-injected the whole
    # duplex context prompt into the terminal (live-test finding).
    if st is not None and (st.layer is not None or st.interactive):
        return st

    chat = await asyncio.to_thread(task_store.get_chat, bridge.chat_id)
    if not chat:
        return "chat_not_found"
    session_id = chat.get("session_id") or ""
    if not session_id:
        return "no_session"

    # Interactive (PTY) chats take the row-feed path: replies arrive as
    # tailer-persisted rows, the utterance goes in through the PTY prompt
    # queue (visible pasted text — the CLI echoes it, the tailer persists
    # the user row, so this path never writes its own).
    from core.session import interactive_session
    if interactive_session.find_live_for_chat(bridge.chat_id) is not None:
        rows = await asyncio.to_thread(
            task_store.get_chat_messages, bridge.chat_id, 1)
        st = _AttachState(
            chat_id=bridge.chat_id,
            agent=chat.get("agent") or "",
            execution_path=chat.get("execution_path") or "",
            interactive=True,
            row_cursor=rows[-1]["id"] if rows else 0,
        )
        _states[bridge.duplex_id] = st
        _interactive_feeds[bridge.chat_id] = bridge
        return st

    from core.session.session_manager import get_execution_layer
    from ws.dashboard import _effective_agent_role
    role = await asyncio.to_thread(
        _effective_agent_role, bridge.sub, chat.get("agent") or "")
    try:
        layer = get_execution_layer(
            chat.get("agent") or "",
            execution_path=chat.get("execution_path") or "",
            user_sub=bridge.sub or None, role=role,
        )
    except RuntimeError:
        return "session_unreachable"

    st = _AttachState(
        chat_id=bridge.chat_id,
        session_id=session_id,
        agent=chat.get("agent") or "",
        execution_path=chat.get("execution_path") or "",
        layer=layer,
    )
    _states[bridge.duplex_id] = st
    return st


def _build_prompt(st: _AttachState, text: str, barge_in_chars: int | None) -> str:
    parts: list[str] = []
    if not st.context_injected:
        ctx = task_store.get_platform_setting("chat_duplex_context") or ""
        if ctx.strip():
            parts.append(f"<system-reminder>\n{ctx.strip()}\n</system-reminder>")
        st.context_injected = True
    if st.interrupted_last_turn or barge_in_chars:
        heard = (
            f"; they heard only the first {barge_in_chars} characters"
            if barge_in_chars else ""
        )
        # Direct-llm gets the precise history annotation via the
        # barge_in_chars kwarg; this textual note is for the layers that
        # can't rewrite their own history (CLI/Codex).
        if "direct" not in (st.execution_path or ""):
            parts.append(
                "[The user interrupted your previous spoken reply"
                f"{heard} — they did not hear the rest.]"
            )
        st.interrupted_last_turn = False
    parts.append(text)
    return "\n\n".join(parts)


async def run_utterance(bridge, frame: dict) -> None:
    """One daemon utterance → one chat turn (or a queue onto a live one)."""
    turn = int(frame.get("turn") or 0)
    text = str(frame.get("text") or "").strip()
    raw_chars = frame.get("barge_in_chars")
    barge_in_chars = int(raw_chars) if raw_chars else None
    if not text:
        await _engine_send(bridge, {"type": "done", "turn": turn, "data": {}})
        return

    st = await _resolve(bridge)
    if isinstance(st, str):
        await _engine_send(bridge, {
            "type": "error", "turn": turn, "data": {"message": st},
        })
        return

    if st.interactive:
        await _run_interactive_utterance(bridge, st, turn, text, barge_in_chars)
        return

    from core.events.stream_pump import _active_pumps

    st.active_turn = turn

    live = _active_pumps.get(bridge.chat_id)
    if live is not None and live is not st.pump:
        # A typed turn is streaming — queue the utterance (delivered by that
        # producer's drain loop as its own framed turn) and speak that pump's
        # tail from here on. Never clobber a live pump (audit F2). Record it
        # as the abort target: a barge-in against this turn must interrupt
        # THAT pump's generation (st.pump stays None by invariant, which
        # used to silently drop the abort).
        live.queue_message(_build_prompt(st, text, barge_in_chars))
        st.abort_target = live
        _start_forward(bridge, st, live, turn)
        return

    if st.pump is not None and _active_pumps.get(bridge.chat_id) is st.pump:
        # Our own previous duplex turn is still running (common after a
        # barge-in abort: the pump is draining) — queue onto it AND restart
        # the forward under the NEW turn label. The daemon routes frames by
        # turn id and drops stale labels, so without the restart the queued
        # utterance's reply would be relayed under the old turn and silently
        # discarded.
        st.pump.queue_message(_build_prompt(st, text, barge_in_chars))
        _start_forward(bridge, st, st.pump, turn)
        return

    # New turn — no live pump to ride, so the session process must actually
    # exist. The dashboard heals a dead process at its turn chokepoint
    # (_resume_dead_session_for_chat); voice has no warmup path, so without
    # this every utterance after a hard abort / idle reap errors into the
    # corpse (incident 2026-08-12).
    try:
        await _ensure_live_session(bridge, st)
    except Exception as e:
        logger.error(
            "duplex %s: dead-session heal failed: %s",
            bridge.duplex_id[:8], e, exc_info=True,
        )
        st.active_turn = None
        await _engine_send(bridge, {
            "type": "error", "turn": turn, "data": {"message": "session_dead"},
        })
        return

    await _run_new_turn(bridge, st, turn, text, barge_in_chars)


async def _ensure_live_session(bridge, st: _AttachState) -> None:
    """Heal a dead/reaped session before dispatching a NEW turn.

    Dead = process died (hard abort after the soft-interrupt watchdog, a
    satellite-side crash) OR the session left the registry entirely (idle
    reap while the user sat silent in voice). Healing goes through the
    headless resume seam: same session id + ``--resume`` when the on-disk
    history survives, else a fresh session with the DB-history seed flagged.

    Fails toward ALIVE on probe uncertainty (the remote layer's doctrine): a
    wrong "alive" just means this turn errors like today; a wrong "dead"
    would respawn under a recoverable blip. A satellite inside its reconnect
    grace window is "reconnecting", never healed."""
    layer, sid = st.layer, st.session_id
    try:
        grace_held = getattr(layer, "is_session_grace_held", None)
        if grace_held is not None and grace_held(sid):
            return
        dead = (await layer.is_session_process_dead(sid)
                or not await layer.is_session_alive(sid))
    except Exception:
        logger.debug(
            "duplex %s: liveness probe failed for %s — dispatching anyway",
            bridge.duplex_id[:8], sid[:8], exc_info=True,
        )
        return
    if not dead:
        return
    # Someone may already have replaced the session (the dashboard's own
    # auto-resume spawns a FRESH id when --resume is refused) — adopt the
    # chat row's current session instead of resurrecting the stale one,
    # which would fork the chat across two live sessions. If the adopted
    # session is dead too, fall through and heal THAT one (keeping the row
    # consistent).
    chat = await asyncio.to_thread(task_store.get_chat, bridge.chat_id)
    row_sid = (chat or {}).get("session_id") or ""
    if row_sid and row_sid != sid:
        st.session_id = sid = row_sid
        logger.info(
            "duplex %s: adopting replaced session %s from the chat row",
            bridge.duplex_id[:8], sid[:8],
        )
        try:
            if (not await layer.is_session_process_dead(sid)
                    and await layer.is_session_alive(sid)):
                return
        except Exception:
            return
    from ws.headless_resume import resume_dead_session_headless
    logger.info(
        "duplex %s: session %s dead — headless auto-resume",
        bridge.duplex_id[:8], sid[:8],
    )
    res = await resume_dead_session_headless(
        bridge.chat_id, sid, layer, user_sub=bridge.sub,
    )
    st.session_id = res.session_id
    st.layer = res.layer
    if not res.resumed:
        # Fresh session — the spoken-mode context prompt died with the old
        # history; re-inject it on this turn (the seed digest only carries
        # the chat transcript).
        st.context_injected = False


async def _run_interactive_utterance(
    bridge, st: _AttachState, turn: int, text: str,
    barge_in_chars: int | None,
) -> None:
    """PTY chats: the utterance goes in through the prompt queue
    (steer-eligible — spoken words are user words, an open turn takes them
    mid-turn), the reply comes back via on_interactive_batch as the tailer
    persists it. Barge-in fidelity is prefix-only here (no engine-history
    rewrite exists for a TUI)."""
    from core.session import interactive_session

    isess = interactive_session.find_live_for_chat(bridge.chat_id)
    if isess is None:
        await _engine_send(bridge, {
            "type": "error", "turn": turn, "data": {"message": "session_dead"},
        })
        return
    st.active_turn = turn
    st.turn_saw_open = False
    prompt = _build_prompt(st, text, barge_in_chars)
    ok = isess.queue_prompt(prompt, "duplex", steer=True, chat_id=bridge.chat_id)
    if not ok:
        st.active_turn = None
        await _engine_send(bridge, {
            "type": "error", "turn": turn, "data": {"message": "session_dead"},
        })


def on_interactive_batch(chat_id: str, *, persisted: int, turn_open: bool) -> None:
    """Tailer hook (every persisted batch + turn transitions): feed new
    assistant rows to an attached duplex session and close its turn when
    the PTY turn closes. No-op without an attached duplex session."""
    bridge = _interactive_feeds.get(chat_id)
    if bridge is None or getattr(bridge, "closed", True):
        return
    st = _states.get(getattr(bridge, "duplex_id", ""))
    if st is None or st.active_turn is None:
        return
    asyncio.create_task(_feed_interactive(bridge, st, persisted, turn_open))


async def _feed_interactive(
    bridge, st: _AttachState, persisted: int, turn_open: bool,
) -> None:
    async with st.feed_lock:
        turn = st.active_turn
        if turn is None:
            return
        if turn_open:
            st.turn_saw_open = True
        if persisted > 0:
            rows = await asyncio.to_thread(
                task_store.get_chat_messages, bridge.chat_id, 50)
            new = [r for r in rows if r.get("id", 0) > st.row_cursor]
            if new:
                st.row_cursor = new[-1]["id"]
            for r in new:
                if (r.get("role") == "assistant" and not r.get("event_type")
                        and (r.get("content") or "").strip()):
                    st.turn_saw_open = True
                    await _engine_send(bridge, {
                        "type": "text", "turn": turn,
                        "data": {"content": r["content"] + "\n"},
                    })
                elif r.get("event_type") in ("tool", "task_spawn"):
                    # A persisted tool row means the tool already COMPLETED
                    # (interactive rows land post-hoc), so forward the
                    # boundary as tool_end — the engine finalizes the
                    # pre-tool TTS segment instead of letting it sit in the
                    # synthesis context until the turn closes (same rule as
                    # the headless layers' tool frames).
                    st.turn_saw_open = True
                    await _engine_send(bridge, {
                        "type": "tool_end", "turn": turn, "data": {},
                    })
        if not turn_open:
            if not st.turn_saw_open:
                # PRE-OPEN batch: on a cold PTY the injected prompt's own
                # echoed user row persists BEFORE the CLI opens the turn
                # (injection held while not-quiet + cold-submit ≈ seconds of
                # gap). Closing here sent an empty `done` — the daemon shut
                # its TTS turn with 0 chunks and the whole spoken reply that
                # persisted moments later was never forwarded (live-hit
                # 2026-08-12 06:07). Skip: the REAL close edge always
                # follows a seen-open or forwarded output.
                return
            st.active_turn = None
            await _engine_send(bridge, {"type": "done", "turn": turn, "data": {}})


async def _run_new_turn(
    bridge, st: _AttachState, turn: int, text: str,
    barge_in_chars: int | None,
) -> None:
    from core.events.common_events import (
        ARTIFACT_TURN, ERROR, PRODUCER_DONE, QUEUE_TURN, CommonEvent,
    )
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.session import visibility as _vis
    from core.session.session_state import get_permission_queue

    prompt = _build_prompt(st, text, barge_in_chars)
    # A fresh session spawned by the heal (resume refused) has no context —
    # claim the chat's pending DB-history digest into this prompt. Same
    # chokepoint rule as the dashboard turn path; direct-llm rebuilds full
    # history from the DB on its own and is never seeded.
    if st.layer.capabilities.name != "direct-llm":
        from core.session.history_seed import consume_pending_seed
        prompt, _seed_notice = await asyncio.to_thread(
            consume_pending_seed, bridge.chat_id, prompt,
        )
    # Persist the user's spoken words like any user message (the prompt's
    # reminder/interruption framing stays out of the DB row).
    await asyncio.to_thread(
        task_store.add_chat_message, bridge.chat_id, "user", text,
        author_sub=bridge.sub,
    )

    layer, sid = st.layer, st.session_id
    event_queue: asyncio.Queue = asyncio.Queue()
    msg_queue: list = []
    sys_queue: list = []
    art_queue: list = []

    async def _produce():
        try:
            async with layer.session_lock(sid):
                async for event in layer.send_message(
                    sid, prompt, inject_time=True,
                    barge_in_chars=barge_in_chars,
                ):
                    await event_queue.put(event)
                # Post-turn drains — the dashboard producer shape (audit F3):
                # a typed message queued mid-voice must run, not drop.
                while msg_queue or art_queue or sys_queue:
                    if msg_queue:
                        combined = "\n\n".join(msg_queue)
                        msg_queue.clear()
                        await event_queue.put(
                            CommonEvent(type=QUEUE_TURN, data={"text": combined}))
                        async for event in layer.send_message(
                                sid, combined, inject_time=True):
                            await event_queue.put(event)
                    if art_queue:
                        from ws import artifact_interactions as _ai
                        batch = list(art_queue)
                        art_queue.clear()
                        framed = _ai.frame_text(batch)
                        await event_queue.put(CommonEvent(
                            type=ARTIFACT_TURN,
                            data={"interactions": batch, "text": framed},
                        ))
                        async for event in layer.send_message(
                                sid, framed, inject_time=True):
                            await event_queue.put(event)
                    while sys_queue:
                        sys_prompt = sys_queue.pop(0)
                        async for event in layer.send_message(sid, sys_prompt):
                            await event_queue.put(event)
        except Exception as e:
            await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
        finally:
            await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

    producer = asyncio.create_task(_produce())
    pump = ChatStreamPump(
        chat_id=bridge.chat_id,
        session_id=sid,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(sid),
        scope="agent" if _vis.is_shared_only(st.agent) else "user",
    )
    pump.message_queue = msg_queue
    pump.system_queue = sys_queue
    pump.system_queue_consumer = True  # duplex drains it each turn
    pump.artifact_queue = art_queue
    _active_pumps[bridge.chat_id] = pump
    pump.start()
    st.pump = pump
    _start_forward(bridge, st, pump, turn)

    async def _abort_inflight():
        await _abort(bridge, st)

    bridge.abort_inflight_turn = _abort_inflight


def _start_forward(bridge, st: _AttachState, pump, turn: int) -> None:
    """Stream one pump's items to the daemon (fresh fan-out subscription —
    the dashboard's own subscription is untouched)."""
    if st.forward_task is not None:
        st.forward_task.cancel()
    st.forward_task = asyncio.create_task(_forward(bridge, st, pump, turn))


async def _forward(bridge, st: _AttachState, pump, turn: int) -> None:
    from ws.phone import _pump_item_to_phone_ws

    q = pump.attach()
    t_start = time.monotonic()
    first_text_ms = -1.0
    try:
        while True:
            item = await q.get()
            msg = _pump_item_to_phone_ws(item, turn)
            if msg is not None:
                # Forward-timing log: pins whether reply latency lives in the
                # engine (late deltas) or downstream (TTS) — tuning aid. The
                # first text frame carries the forward-start elapsed: the
                # proxy-side engine leg, comparable with the daemon's
                # dispatch→text.
                if first_text_ms < 0 and msg.get("type") == "text":
                    first_text_ms = (time.monotonic() - t_start) * 1000
                    logger.info(
                        "duplex %s turn %s → text (%d chars, first "
                        "+%.0fms)",
                        bridge.duplex_id[:8], turn,
                        len((msg.get("data") or {}).get("content", "") or ""),
                        first_text_ms,
                    )
                else:
                    logger.info(
                        "duplex %s turn %s → %s (%d chars)",
                        bridge.duplex_id[:8], turn, msg.get("type"),
                        len((msg.get("data") or {}).get("content", "") or ""),
                    )
                await _engine_send(bridge, msg)
            if item.get("pump_type") in ("all_done", "pump_ended"):
                break
    except asyncio.CancelledError:
        pass
    finally:
        with contextlib.suppress(Exception):
            pump.detach(q)
        # Only the still-registered forward may clear the shared state: a
        # forward replaced by _start_forward (same pump, new turn label —
        # the queued-utterance/barge-in path) is cancelled AFTER the new
        # task is registered, and its cleanup must not null out the state
        # the successor is using.
        if st.forward_task is asyncio.current_task():
            if st.pump is pump:
                st.pump = None
            if st.abort_target is pump:
                st.abort_target = None
            if st.active_turn == turn:
                st.active_turn = None
            if bridge.abort_inflight_turn is not None:
                bridge.abort_inflight_turn = None


async def _abort(bridge, st: _AttachState, pump=None) -> None:
    """Mid-generation barge-in: graceful-first, per layer. Queued messages
    are NOT cleared — a barge-in interrupts the reply, not the queue.
    ``pump`` overrides the hard-abort target (the foreign-pump case);
    default is our own st.pump."""
    layer, sid = st.layer, st.session_id
    graceful = False
    with contextlib.suppress(Exception):
        graceful = bool(await layer.abort(sid))
    if pump is None:
        pump = st.pump
    if pump is not None and not graceful:
        with contextlib.suppress(Exception):
            pump.abort()
    st.interrupted_last_turn = True


async def abort_turn(bridge, frame: dict) -> None:
    st = _states.get(bridge.duplex_id)
    if st is None:
        return
    turn = frame.get("turn")
    if (turn is not None and st.active_turn is not None
            and turn != st.active_turn):
        return  # stale abort for an already-finished turn — never kill a newer one
    if st.interactive:
        # PTY graceful interrupt — the same ESC the dashboard Stop button
        # sends (gated on an open turn inside interrupt_turn itself). The
        # tailer's interrupt markers close the turn; the row feed then
        # sends `done` as usual.
        from core.session import interactive_session
        isess = interactive_session.find_live_for_chat(bridge.chat_id)
        if isess is not None:
            with contextlib.suppress(Exception):
                if isess.interrupt_turn():
                    st.interrupted_last_turn = True
        return
    if st.pump is None:
        # Queued-onto-a-foreign-pump turn: route the abort to that pump —
        # but ONLY while it is still the chat's live pump. A newer pump
        # (st.active_turn may already be cleared by forward cleanup, so the
        # stale filter can't protect it) must never be killed by a late
        # duplex barge-in.
        from core.events.stream_pump import _active_pumps
        target = st.abort_target
        st.abort_target = None
        if target is not None and _active_pumps.get(st.chat_id) is target:
            await _abort(bridge, st, pump=target)
        elif target is not None:
            logger.info(
                "duplex %s: abort dropped — queued-onto pump no longer live",
                bridge.duplex_id[:8],
            )
        return
    await _abort(bridge, st)
