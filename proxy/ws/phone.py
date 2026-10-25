"""WebSocket phone endpoint — persistent connection for the phone server.

Uses the ExecutionLayer abstraction — same code for CLI and Direct LLM agents.
Routes through ChatStreamPump for DB persistence, cost tracking, and usage metering.

Registered by app.py as @app.websocket("/ws/phone").

A call session carries client_type / source_type "phone" (the discriminator
that drives MCP filtering — see proxy/adapters/phone.py).
"""

import asyncio
import contextlib
import functools
import json
import logging
import time
import uuid
from pathlib import Path
from typing import cast

from fastapi import Query, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

import config
from adapters.phone import is_outbound
from core.config.phone_config_builder import (
    build_phone_agent_config,
    resolve_phone_execution_target,
)
from core.events import common_events as _ce
from core.events.common_events import (
    ERROR,
    PRODUCER_DONE,
    CommonEvent,
)
from core.events.stream_pump import ChatStreamPump, _active_pumps
from core.execution_layer import AgentConfig, ExecutionLayer, ResumeVerdict
from core.session import external_identity, session_kind
from core.session.resume_probe import RESUME_PROBE_BUDGET_INLINE_S, settle_resume
from core.session.session_manager import get_execution_layer
from core.session.visibility import PHONE_CHAT_OWNER
from services.phone import call_control
from services.phone.phone_identity import (
    RouteIdentity,
    pop_call_identity,  # noqa: F401 — re-exported for the handler + tests
    remember_call_identity,
    resolve_route_identity,
)
from storage import database as task_store
from storage.agents.schema import DEFAULT_EXECUTION_PATH
from storage.automation import trigger_store
from storage.phone import phone_route_store
from ws import phone_attach, wire_events as wire

logger = logging.getLogger("otodock")

# Another turn starter bringing the call's chat a turn (a stored wake's
# replay): how long a caller's turn waits for it.
_CLAIM_WAIT_S = 5.0


def _validated_reuse_sid(existing_sid, agent_name: str) -> str:
    """A pre-warmed session id the daemon asks to reuse is honoured ONLY when
    it is a UUID that names a phone chat of THIS agent — the daemon holds the
    master key, but it must not be able to attach a call to an arbitrary live
    session (a dashboard chat, another agent's call)."""
    if not existing_sid:
        return ""
    try:
        sid = str(uuid.UUID(str(existing_sid)))
    except ValueError:
        logger.warning("WS warmup: ignoring non-UUID session_id in reuse request")
        return ""
    chat = task_store.get_chat_by_session(sid)
    if not chat or session_kind.of_chat(chat) is not session_kind.PHONE or chat.get("agent") != agent_name:
        logger.warning(
            "WS warmup: ignoring reuse of session %s — not a phone chat of agent %s",
            sid[:8], agent_name,
        )
        return ""
    return sid


def _external_home_for(identity: RouteIdentity | None, agent_name: str) -> str:
    """Host path of the call's caller tree ("" when the identity has none)."""
    if identity is None or identity.external is None or not identity.external.has_tree:
        return ""
    home = external_identity.external_home(config.get_agent_dir(agent_name), identity.external)
    return str(home) if home is not None else ""


# ---------------------------------------------------------------------------
# Pump event → phone WS JSON translator
# ---------------------------------------------------------------------------

def _pump_item_to_phone_ws(item: dict, turn_id: int) -> dict | None:
    """Translate a pump queue item to a phone WebSocket JSON message.

    Every turn-scoped frame carries ``turn`` (echoed from the chat message)
    so the phone client can drop frames from turns it abandoned or aborted —
    without this, a barged-in turn's tail would bleed into the next turn's
    stream. Returns None for events the phone server doesn't need
    (thinking, metadata, permissions, plan, todo, etc.).
    """
    pump_type = item.get("pump_type")

    if pump_type == wire.PUMP_WS_EVENT:
        event = item.get("event", {})
        etype = event.get("type", "")

        if etype == wire.TEXT:
            content = event.get("content", "")
            if content:
                return {"type": wire.TEXT, "turn": turn_id, "data": {"content": content}}

        elif etype in (_ce.TOOL_USE, wire.TOOL_START):
            # The pump emits the wire's tool_start (its TOOL_USE handler);
            # the raw CommonEvent word is kept for any raw-event pusher —
            # the dual matching is frozen. Matching only the raw word
            # silently dropped EVERY tool_start — the phone
            # pipeline never got the early pre-tool TTS flush and callers
            # sat through silent tool runs before hearing the pre-tool
            # sentence (live-hit 2026-08-14, inbound home-assistant call:
            # 10 s of dead air, pre-tool text played only with the final).
            return {"type": wire.TOOL_START, "turn": turn_id, "data": {
                "name": event.get("name", ""),
                "tool_use_id": event.get("tool_id", ""),
            }}

        elif etype in (wire.TOOL_RESULT, wire.TOOL_END):
            # tool_result = the hook path's content-carrying frame (live);
            # tool_end = the pump's TOOL_RESULT close frame. Both mark the
            # boundary; downstream repeats are no-ops on an empty buffer.
            return {"type": wire.TOOL_END, "turn": turn_id, "data": {
                "tool_use_id": event.get("tool_id", ""),
                "result_preview": event.get("result_preview", ""),
            }}

        elif etype == wire.PHONE_SESSION:
            return {"type": wire.PHONE_SESSION, "turn": turn_id, "data": {
                "session_id": event.get("session_id", ""),
            }}

        # Skip: thinking, metadata, subagent, delegate, plan, todo, etc.
        return None

    elif pump_type in (wire.PUMP_ALL_DONE, wire.PUMP_ENDED):
        return {"type": wire.DONE, "turn": turn_id, "data": {}}

    elif pump_type == wire.PUMP_ERROR:
        return {"type": wire.ERROR, "turn": turn_id, "data": {
            "message": item.get("message", "Unknown error"),
        }}

    return None


# ---------------------------------------------------------------------------
# Trigger payload resolver
# ---------------------------------------------------------------------------

async def _resolve_trigger_payload(
    *,
    agent_name: str,
    phone_route_id: str,
    audiosocket_uuid: str,
    caller_phone: str,
    caller_did: str,
    dial_event: dict,
) -> dict | None:
    """Look up the route + trigger for a phone call and assemble a payload.

    Returns ``None`` if the route can't be found, has no ``trigger_slug``,
    or the trigger row no longer exists. Any failure here degrades to "no
    enrichment" (the agent answers the call with its base prompt) — never
    blocks the warmup. The agent name in the row is verified to match the
    warmup's agent so a stale-but-valid route bound to a different agent
    can't leak context.

    Route lookup uses ``phone_route_id`` (the authoritative DB key, sent by
    the phone server for both inbound and outbound calls — see
    ``phone/proxy/llm_factory.py``).
    """
    if not phone_route_id:
        return None
    route = await asyncio.to_thread(phone_route_store.get_route, phone_route_id)
    if not route or not route.get("trigger_slug"):
        return None
    # Cross-agent safety net — warmup agent must match route agent.
    if route.get("agent") != agent_name:
        logger.warning(
            "Phone route %s bound to agent=%s but warmup requested agent=%s "
            "— skipping trigger enrichment",
            route.get("id"), route.get("agent"), agent_name,
        )
        return None
    trigger = await asyncio.to_thread(
        trigger_store.get_trigger_by_slug,
        scope="agent", owner=agent_name, slug=route["trigger_slug"],
    )
    if not trigger:
        # Trigger was deleted after the route was bound. Logged, not fatal.
        logger.warning(
            "Phone route %s references trigger slug=%r that no longer exists "
            "for agent=%s — skipping enrichment",
            route.get("id"), route["trigger_slug"], agent_name,
        )
        return None
    return {
        # ``source`` is the trigger-payload session-type token.
        "source": "phone",
        "route": route.get("name") or route["trigger_slug"],
        "phone": caller_phone,
        "did": caller_did or audiosocket_uuid,
        "email": "",
        "body": dial_event,
    }


# ---------------------------------------------------------------------------
# Phone WebSocket handler
# ---------------------------------------------------------------------------

class _PhoneCall:
    """One phone-daemon socket: the call's state and its handlers.

    The former ``ws_phone_handler`` closures are methods; the state their
    ``nonlocal`` lines and late-binding reads carried (the session, the
    chat, the layer, the call parameters a heal rebuilds from, the attach,
    the turn tasks) lives as attributes, read at the same moments. The
    receive loop stays in ``ws_phone_handler``, which hands each frame to
    the ``on_<frame>`` method ``_PHONE_FRAMES`` names and ends the call
    with ``teardown``.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.session_id: str | None = None
        self.chat_id: str | None = None
        self.layer: ExecutionLayer | None = None  # resolved on warmup
        self.model_name: str = "personal-assistant"
        self.first_turn = True
        # Set by a graceful abort; the NEXT turn's dispatch carries the duplex-
        # style interruption note (CLI/Codex can't rewrite their own history).
        self.interrupted_last_turn = False
        # Call parameters captured at warmup so the dead-process heal of
        # run_turn can rebuild the SAME phone config. trigger_payload stays None
        # for a reused pre-warmed session (its enrichment lives in the resumed
        # history) — a heal-rebuild without it just resolves ${trigger.*} empty.
        self.call_type: str = "inbound"
        self.phone_context_override: str = ""
        self.phone_mode = False
        # The daemon understands ``call_control`` frames (its warmup says so):
        # the session carries the call tools instead of the markers alone.
        self.call_tools = False
        self.transfer_targets: list[dict] = []
        self.trigger_payload: dict | None = None
        # Who the caller IS on this call (services/phone/phone_identity.py) — the
        # heal rebuilds the same identity; the teardown prunes an ephemeral tree.
        self.route_identity: RouteIdentity | None = None
        self.ephemeral_home: str = ""
        # A call that runs inside the caller's own chat (ws/phone_attach.py):
        # the socket borrows the chat's session instead of warming one.
        self.attach: phone_attach.CallAttach | None = None

        # Turns stream from tasks so the handler's receive loop stays responsive to
        # "abort" (barge-in) and follow-up "chat" while a turn is in flight.
        # Sends are serialized: two turns can overlap when the client moved on
        # from a turn it abandoned (proxy mode drains it to completion).
        self.send_lock = asyncio.Lock()
        self.turn_producers: dict[int, asyncio.Task] = {}
        self.turn_tasks: set[asyncio.Task] = set()
        # The warmup's config and whether its session started: the failure
        # arm reads them to release what the warmup took.
        self.agent_cfg: AgentConfig | None = None
        self.phone_started = False
        # call_control.bind and unbind compare the sender by identity: one
        # bound sender for the socket's life (a fresh bound method per read
        # would leave a dead binding and fail a second warmup's requests).
        self.send = self._send

    async def _send(self, payload: dict) -> None:
        async with self.send_lock:
            await self.websocket.send_json(payload)

    async def close(self) -> None:
        """The attach's terminal signal to the daemon: its socket closes (the
        handler's receive loop then ends and the teardown runs)."""
        with contextlib.suppress(Exception):
            await self.websocket.close(code=1000)

    async def run_turn(self, turn_id: int, prompt: str, barge_in_chars) -> None:
        """Run one chat turn: producer -> pump -> WS frames stamped with turn_id."""
        producer: asyncio.Task | None = None
        t_sent = time.monotonic()   # sent→first_text: the engine leg, proxy-side
        from core.events import input_queue
        # on_chat starts a turn only on a warmed call (session, layer, chat).
        queue = input_queue.get(cast("str", self.chat_id))
        claim = None
        try:
            # The chat's turn starters take turns: the call waits a moment
            # for another one bringing the chat a turn, and supersedes the
            # pump the chat has: its own previous turn's (abandoned, still
            # draining), or a turn the platform drove into this live session
            # (a delegate-result wake, a background nudge; the dashboard
            # never starts one on a call's chat). That turn still completes
            # under the session lock before the caller's runs and keeps
            # writing its rows; superseded, it leaves the turn-end stamp and
            # the ready broadcast to the pump that owns the chat now. A
            # caller's turn is never refused for a wake.
            claim = await input_queue.claim_when_free(
                cast("str", self.chat_id), timeout=_CLAIM_WAIT_S, supersede=True)
            if claim is None:
                await self.send({"type": wire.ERROR, "turn": turn_id, "data": {
                    "message": "The chat is busy with another turn; say it again in a moment."}})
                return
            # The user row and the first title land on the chat's writer
            # lane, off the loop, before the turn runs (its rows follow them).
            from core.events import chat_writer
            await chat_writer.submit(
                cast("str", self.chat_id),
                functools.partial(task_store.add_chat_message, cast("str", self.chat_id), "user", prompt),
                label="phone_user_row",
            )
            if self.first_turn:
                title = prompt[:50].strip()
                if len(prompt) > 50:
                    title += "..."
                await chat_writer.submit(
                    cast("str", self.chat_id), functools.partial(task_store.update_chat, cast("str", self.chat_id), title=title),
                    label="phone_title",
                )
                self.first_turn = False

            prompt = await self._heal_dead_process(prompt)

            # Interruption note for layers that can't rewrite their own
            # history (an engine that rebuilds it from the DB gets the precise
            # annotation via the barge_in_chars kwarg instead). Same wording
            # as the duplex chat attach. The DB row above keeps the RAW spoken
            # words; only the dispatch carries the note.
            dispatch_prompt = prompt
            _rebuilds = bool(getattr(
                getattr(getattr(self.layer, "capabilities", None), "behaviour", None),
                "rebuilds_history_from_db", False))
            if (not _rebuilds
                    and (self.interrupted_last_turn or barge_in_chars)):
                heard = (
                    f"; they heard only the first {barge_in_chars} characters"
                    if barge_in_chars else ""
                )
                dispatch_prompt = (
                    "[The user interrupted your previous spoken reply"
                    f"{heard} — they did not hear the rest.]\n\n" + prompt
                )
            self.interrupted_last_turn = False

            event_queue: asyncio.Queue = asyncio.Queue()
            local_session_id = cast("str", self.session_id)

            async def _produce():
                try:
                    async with self.layer.session_lock(local_session_id):
                        # The call tools name the turn whose engine turn
                        # runs: the daemon refuses an action from a turn
                        # the caller already interrupted.
                        call_control.set_turn(local_session_id, turn_id)
                        try:
                            async for event in self.layer.send_message(
                                local_session_id, dispatch_prompt,
                                barge_in_chars=barge_in_chars,
                                inject_time=True,
                            ):
                                await event_queue.put(event)
                        finally:
                            call_control.set_turn(local_session_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("Phone producer error: %s", e)
                    await event_queue.put(CommonEvent(ERROR, {"message": str(e)}))
                finally:
                    event_queue.put_nowait(CommonEvent(PRODUCER_DONE, {}))

            producer = asyncio.create_task(_produce())
            self.turn_producers[turn_id] = producer

            # Create pump for this turn
            pump = ChatStreamPump(
                chat_id=cast("str", self.chat_id),
                session_id=local_session_id,
                producer=producer,
                event_queue=event_queue,
                perm_queue=None,  # phone uses auto mode — no permission prompts
                scope="agent",
                source_type=session_kind.PHONE.source_type,
            )
            _active_pumps[cast("str", self.chat_id)] = pump
            queue.release(claim)
            claim = None
            pump.start()

            # Attach as phone subscriber and stream events
            ws_queue = pump.attach()

            first_text_logged = False
            while True:
                item = await ws_queue.get()

                phone_msg = _pump_item_to_phone_ws(item, turn_id)
                if phone_msg:
                    if (not first_text_logged
                            and phone_msg.get("type") == wire.TEXT):
                        first_text_logged = True
                        logger.info(
                            "phone turn %s: sent→first_text "
                            "%.0fms",
                            turn_id, (time.monotonic() - t_sent) * 1000
                        )
                    await self.send(phone_msg)

                # Exit subscriber loop when pump is done
                pt = item.get("pump_type", "")
                if pt in wire.PUMP_TERMINAL:
                    break

        except (WebSocketDisconnect, ConnectionClosed):
            # Socket died mid-stream — stop generating into the void; the
            # main receive loop tears the connection down.
            if producer and not producer.done():
                producer.cancel()
        except Exception as e:
            logger.exception("WS chat error: %s", e)
            with contextlib.suppress(Exception):
                await self.send({"type": wire.ERROR, "turn": turn_id,
                                 "data": {"message": str(e)}})
        finally:
            queue.release(claim)
            self.turn_producers.pop(turn_id, None)

    async def _heal_dead_process(self, prompt: str) -> str:
        """Respawn a dead engine process under the same session id before the
        turn runs; returns the prompt, re-seeded with the transcript digest
        when the history could not be resumed."""
        # Dead-process heal (the dashboard turn chokepoint's twin, cf.
        # ws/headless_resume.py): a hard abort (soft-interrupt watchdog
        # escalation) or a satellite-side death between turns leaves the
        # CLI dead while the call keeps sending turns. Respawn under the
        # SAME session id with the phone's own config shape (phone
        # prompt + call-scoped MCP set — never the chat-row/dashboard
        # rebuild), resuming the on-disk history when it survives. Fail
        # toward ALIVE on probe uncertainty; a satellite inside its
        # reconnect grace window is "reconnecting", not dead. Direct-llm
        # never process-dies (is_session_process_dead is always False).
        # Probe + respawn hold the session lock so overlapping turns
        # (abandoned-turn drain) can't double-heal; the producer takes
        # the same lock again afterwards, sequentially — never nested.
        # Called from run_turn: the call is warmed.
        async with cast("ExecutionLayer", self.layer).session_lock(cast("str", self.session_id)):
            try:
                _grace = getattr(self.layer, "is_session_grace_held", None)
                proc_dead = (
                    not (_grace is not None and _grace(self.session_id))
                    and (await cast("ExecutionLayer", self.layer).is_session_process_dead(cast("str", self.session_id))
                         or not await cast("ExecutionLayer", self.layer).is_session_alive(cast("str", self.session_id)))
                )
            except Exception:  # noqa: BLE001  # a failed liveness probe reads as a live process
                proc_dead = False
            if proc_dead:
                logger.info(
                    "WS phone: session %s process dead — "
                    "respawning in place", self.session_id
                )
                # A caller is on the line: the probe waits the inline
                # budget, and a machine that did not answer ends the heal
                # with nothing mutated (the call's cannot-continue path).
                verdict = await settle_resume(
                    cast("ExecutionLayer", self.layer), cast("str", self.session_id), agent_name=self.model_name, username="",
                    external_home=_external_home_for(self.route_identity, self.model_name),
                    budget_s=RESUME_PROBE_BUDGET_INLINE_S,
                )
                if verdict is ResumeVerdict.UNKNOWN:
                    raise RuntimeError("session_machine_busy")
                can_resume = verdict is ResumeVerdict.RESUMABLE
                await cast("ExecutionLayer", self.layer).prepare_resume(cast("str", self.session_id))
                heal_cfg = await build_phone_agent_config(
                    agent_name=self.model_name,
                    call_type=self.call_type,
                    phone_context_override=self.phone_context_override,
                    phone_mode=self.phone_mode,
                    trigger_payload=self.trigger_payload,
                    route_identity=self.route_identity,
                    session_id=cast("str", self.session_id),
                    call_tools=self.call_tools,
                    transfer_targets=self.transfer_targets,
                )
                heal_cfg.resume = can_resume
                try:
                    await cast("ExecutionLayer", self.layer).start_session(cast("str", self.session_id), heal_cfg)
                except Exception:
                    # The build's seat (and the dead session's stale
                    # binding under this id) would otherwise stay counted.
                    from core.config.config_builder import release_config_seat
                    release_config_seat(cast("str", self.session_id), heal_cfg)
                    raise
                if (not can_resume
                        and not cast("ExecutionLayer", self.layer).capabilities_for(cast("str", self.session_id))
                        .behaviour.rebuilds_history_from_db):
                    # History unrecoverable — seed the fresh session with
                    # the DB-transcript digest so the call keeps context.
                    from core.session.history_seed import (
                        consume_pending_seed,
                    )
                    task_store.update_chat(
                        cast("str", self.chat_id), pending_history_seed="resume_failed")
                    prompt, _seed_notice = consume_pending_seed(
                        cast("str", self.chat_id), prompt)
        return prompt

    async def on_warmup(self, msg: dict) -> None:
        """The daemon's warmup: an attach grant, a pre-warmed session reused,
        or a fresh session built, started and answered with ``warmup_ready``
        (the failure answered with an error, the slot and the seat released)."""
        t_warm = time.monotonic()
        self.model_name = msg.get("model", "")
        if not self.model_name:
            await self.send({"type": wire.ERROR, "data": {"message": "Agent name required in warmup 'model' field"}})
            return
        llm_mode = msg.get("llm_mode", "proxy")  # echoed back for phone server compat
        if msg.get("attach_grant"):
            await self._warmup_attach(msg, llm_mode)
            return
        self.phone_mode = msg.get("phone_mode", False)
        self.call_type = msg.get("call_type", "inbound")
        self.phone_context_override = msg.get("phone_context_override", "")
        existing_sid = msg.get("session_id")  # pre-warmed session to reuse
        # An older daemon never sends the flag and keeps the markers.
        self.call_tools = bool(msg.get("call_control", False))

        # Phone server sends these so the framework can
        # resolve the route → trigger linkage and build the
        # ${trigger.*} payload. Missing fields = no enrichment,
        # ${trigger.*} tokens resolve empty, requires-gated blocks skip.
        #
        # ``phone_route_id`` is the authoritative route lookup key —
        # works for both inbound and outbound. ``audiosocket_uuid``
        # is retained for the ``"did"`` field on the enrichment
        # payload (used when ``caller_did`` is missing).
        audiosocket_uuid = msg.get("audiosocket_uuid", "")
        phone_route_id = msg.get("phone_route_id", "")
        caller_phone = msg.get("caller_phone") or msg.get("caller_id", "")
        caller_did = msg.get("caller_did", "")
        dial_event = msg.get("dial_event") or {}
        # The daemon's PIN gate ran before this warmup; the resolver
        # honours the flag only on routes that actually have a PIN.
        pin_verified = bool(msg.get("pin_verified", False))

        self.agent_cfg = None
        self.phone_started = False
        try:
            route, reuse_sid, fresh_sid, phone_target = await self._warmup_prepare(
                msg, phone_route_id, existing_sid, caller_phone, pin_verified,
            )
            if await self._warmup_reuse(reuse_sid, t_warm, llm_mode):
                return
            await self._warmup_fresh(
                route, reuse_sid, fresh_sid, phone_target, phone_route_id,
                audiosocket_uuid, caller_phone, caller_did, dial_event,
                pin_verified, t_warm, llm_mode,
            )
        except Exception as e:
            logger.exception("WS warmup failed: %s", e)
            await self._warmup_failed(e)

    async def _warmup_attach(self, msg: dict, llm_mode: str) -> None:
        """An attach grant: the call borrows the chat's session (ws/phone_attach.py)."""
        # An in-session call: the chat's session, no build, no
        # slot. A second warmup on the same socket (the live
        # pipeline adopting the pre-warm client) is answered again.
        if self.attach is None or self.attach.ended:
            result = await phone_attach.begin(msg, self.send, self.close)
            if isinstance(result, str):
                await self.send({"type": wire.ERROR, "data": {"message": result}})
                return
            self.attach = result
            self.session_id, self.chat_id = self.attach.session_id, self.attach.chat_id
        await self.send({
            "type": wire.WARMUP_READY,
            "data": {"session_id": self.attach.session_id, "llm_mode": llm_mode},
        })

    async def _warmup_prepare(
        self, msg: dict, phone_route_id: str, existing_sid: str | None,
        caller_phone: str, pin_verified: bool,
    ) -> tuple[dict | None, str, str, str]:
        """The route, the transfer targets, the reuse id, the identity, the
        target and the layer; returns the route, the reuse and fresh ids and
        the target the paths after it read."""
        # Identity FIRST (services/phone/phone_identity.py): the
        # route decides who the caller is, which decides the role,
        # the target and the layer. A reuse request is honoured
        # only for a phone chat of this agent; an ephemeral caller
        # tree is keyed by the session id, so the id is settled
        # before the identity resolves.
        route = (
            await asyncio.to_thread(phone_route_store.get_route, phone_route_id)
            if phone_route_id else None
        )
        # The prompt lists the route's targets only on a line that
        # can hand the caller over (the daemon's warmup says; an
        # outbound AMI call's media leg names no channel to move).
        # An older daemon never says: today's shape.
        can_transfer = bool(msg.get("transfer", True))
        self.transfer_targets = [
            t for t in ((route or {}).get("transfer_targets") or [])
            if isinstance(t, dict)
        ] if can_transfer else []
        reuse_sid = await asyncio.to_thread(
            _validated_reuse_sid, existing_sid, self.model_name,
        )
        fresh_sid = str(uuid.uuid4())
        self.route_identity = await asyncio.to_thread(
            resolve_route_identity, route, agent=self.model_name,
            caller_phone=caller_phone, session_id=reuse_sid or fresh_sid,
            pin_verified=pin_verified,
        )
        if self.route_identity.mode == "user":
            _target_role = self.route_identity.user_role
            _target_sub = (self.route_identity.user or {}).get("sub")
        else:
            _target_role, _target_sub = self.route_identity.role, None
        # Resolve the target up front (shared helper → same result
        # as build_phone_agent_config) so the layer matches where
        # the (pre-warmed or new) session actually runs.
        phone_target = await asyncio.to_thread(
            resolve_phone_execution_target, self.model_name,
            role=_target_role, user_sub=_target_sub,
        )
        self.layer = get_execution_layer(
            self.model_name, execution_target=phone_target,
        )
        return route, reuse_sid, fresh_sid, phone_target

    async def _warmup_reuse(self, reuse_sid: str, t_warm: float, llm_mode: str) -> bool:
        """The pre-warmed session the daemon named, when it is still alive: True
        when the warmup was answered on it (the fresh path never runs after it)."""
        # Reuse pre-warmed session if provided and still alive
        # (_warmup_prepare set the layer and the identity before this runs).
        if reuse_sid and await cast("ExecutionLayer", self.layer).is_session_alive(reuse_sid):
            self.session_id = reuse_sid
            self.ephemeral_home = (
                _external_home_for(self.route_identity, self.model_name)
                if (ext := cast("RouteIdentity", self.route_identity).external) is not None
                and ext.ephemeral else ""
            )
            remember_call_identity(self.session_id, cast("RouteIdentity", self.route_identity).label)
            # Recover this session's chat_id — the reused connection
            # needs it for chat turns (this branch runs on a NEW ws
            # connection after a daemon reconnect, so chat_id is
            # otherwise unset). Fall back to a fresh chat row if the
            # link is somehow missing so the call still proceeds.
            prior = await asyncio.to_thread(
                task_store.get_chat_by_session, self.session_id,
            )
            if prior:
                self.chat_id = prior["id"]
                self.first_turn = False
            else:
                # Link missing (shouldn't happen now that warmup
                # persists it) — rebuild a chat row so the call
                # still proceeds. Resolve execution_path the same
                # way the fresh-session path below does.
                _cfg = await build_phone_agent_config(
                    agent_name=self.model_name, call_type=self.call_type,
                    phone_context_override=self.phone_context_override,
                    phone_mode=self.phone_mode, trigger_payload=None,
                    route_identity=self.route_identity, session_id=self.session_id,
                    call_tools=self.call_tools, transfer_targets=self.transfer_targets,
                )
                # Built only to read execution_path: the seat the
                # build acquired goes straight back (keep_binding:
                # the id is the live pre-warmed session's).
                from core.config.config_builder import release_config_seat
                release_config_seat(self.session_id, _cfg, keep_binding=True)
                self.chat_id = str(uuid.uuid4())
                self.first_turn = True
                _row = task_store.create_chat(
                    self.chat_id, PHONE_CHAT_OWNER, self.model_name, "auto",
                    model=config.get_cli_model(self.model_name),
                    execution_path=_cfg.execution_path or DEFAULT_EXECUTION_PATH,
                    source_type=session_kind.PHONE.source_type,
                )
                task_store.update_chat(self.chat_id, session_id=self.session_id)
                from services.apps import catalog as _catalog
                _catalog.announce_new_chat(_row)
            logger.info(
                "WS warmup: reusing pre-warmed session=%s, "
                "chat=%s, identity=%s "
                "(%.0fms)",
                self.session_id, self.chat_id, cast("RouteIdentity", self.route_identity).label,
                (time.monotonic() - t_warm) * 1000
            )
            if self.call_tools:
                # The call moved to this socket: its tools too.
                call_control.bind(self.session_id, self.send,
                                  ask_back=is_outbound(self.call_type))
            await self.send({
                "type": wire.WARMUP_READY,
                "data": {"session_id": self.session_id, "llm_mode": llm_mode},
            })
            return True
        return False

    async def _warmup_fresh(
        self, route: dict | None, reuse_sid: str, fresh_sid: str, phone_target: str,
        phone_route_id: str, audiosocket_uuid: str, caller_phone: str, caller_did: str,
        dial_event: dict, pin_verified: bool, t_warm: float, llm_mode: str,
    ) -> None:
        """A fresh session: the identity settled on its id, the trigger payload,
        the build, the slot, the tools bound, the start, the chat row and its
        link, ``warmup_ready``."""
        if reuse_sid:
            # The pre-warmed session is gone: a fresh session, and
            # (for an ephemeral caller) a tree keyed by ITS id.
            self.route_identity = await asyncio.to_thread(
                resolve_route_identity, route, agent=self.model_name,
                caller_phone=caller_phone, session_id=fresh_sid,
                pin_verified=pin_verified,
            )

        # Resolve route → trigger → payload. Best-effort: any
        # miss (no route_id/UUID, no route, no trigger_slug, no
        # trigger row) leaves trigger_payload=None and the call
        # proceeds without enrichment.
        self.trigger_payload = await _resolve_trigger_payload(
            agent_name=self.model_name,
            phone_route_id=phone_route_id,
            audiosocket_uuid=audiosocket_uuid,
            caller_phone=caller_phone,
            caller_did=caller_did,
            dial_event=dial_event,
        )

        self.session_id = fresh_sid
        self.chat_id = str(uuid.uuid4())
        self.first_turn = True

        # _warmup_prepare set the layer and the identity before this runs.
        self.agent_cfg = await build_phone_agent_config(
            agent_name=self.model_name,
            call_type=self.call_type,
            phone_context_override=self.phone_context_override,
            phone_mode=self.phone_mode,
            trigger_payload=self.trigger_payload,
            route_identity=self.route_identity,
            session_id=self.session_id,
            call_tools=self.call_tools,
            transfer_targets=self.transfer_targets,
        )
        self.ephemeral_home = (
            _external_home_for(self.route_identity, self.model_name)
            if (ext := cast("RouteIdentity", self.route_identity).external) is not None
            and ext.ephemeral else ""
        )

        # Check concurrency limit before spawning session. Phone can
        # run on a remote satellite (phone_target) — pass it so a
        # remote call doesn't consume a local-G slot (its satellite
        # enforces). Local calls take a unit of the local ceiling,
        # of their own kind: never evicted and outside the
        # per-person cap; a user-mode route's call is owned by its
        # tied user for the count, and a Direct-LLM call reserves
        # for its stdio MCPs.
        from core.concurrency import acquire_chat_slot
        from core.config.config_builder import release_config_seat
        adm = await acquire_chat_slot(
            self.session_id, kind="phone", target=phone_target,
            execution_path=self.agent_cfg.execution_path,
            user_sub=(cast("RouteIdentity", self.route_identity).user or {}).get("sub") or None,
            mcp_config_path=self.agent_cfg.mcp_config_path or "",
        )
        if not adm:
            release_config_seat(self.session_id, self.agent_cfg)
            await self.send({
                "type": wire.ERROR,
                "data": {"message": adm.user_message},
            })
            return

        if self.call_tools:
            # Bound before the engine starts: a tool call can
            # come as soon as the first turn runs.
            call_control.bind(self.session_id, self.send,
                              ask_back=is_outbound(self.call_type))
        await cast("ExecutionLayer", self.layer).start_session(self.session_id, self.agent_cfg)
        self.phone_started = True
        remember_call_identity(self.session_id, cast("RouteIdentity", self.route_identity).label)
        # A read-only call is activity too (the retention sweep
        # keys on the tree's newest mtime).
        _home = _external_home_for(self.route_identity, self.model_name)
        if _home:
            with contextlib.suppress(OSError):
                Path(_home).touch()  # noqa: ASYNC240  # one utime per call start, before the session's chat link

        # Create chat row for persistence. ``"phone"`` is the
        # sentinel owner (a call has no real user) + the source_type
        # discriminator.
        execution_path = self.agent_cfg.execution_path or DEFAULT_EXECUTION_PATH
        _row = task_store.create_chat(
            self.chat_id, PHONE_CHAT_OWNER, self.model_name, "auto",
            model=config.get_cli_model(self.model_name),
            execution_path=execution_path,
            source_type=session_kind.PHONE.source_type,
        )
        from services.apps import catalog as _catalog
        _catalog.announce_new_chat(_row)
        # Link session→chat so a later reconnect that reuses this
        # pre-warmed session can recover its chat_id (the reuse
        # branch above resolves session_id but not chat_id — without
        # this the reconnected connection's chat turns 404 with
        # "No session — send warmup first").
        task_store.update_chat(self.chat_id, session_id=self.session_id)

        logger.info(
            "WS warmup: session=%s, chat=%s, "
            "agent=%s, identity=%s "
            "(mode=%s"
            "%s), "
            "trigger=%s "
            "(%.0fms)",
            self.session_id, self.chat_id, self.model_name, cast("RouteIdentity", self.route_identity).label, cast("RouteIdentity", self.route_identity).mode,
            ', fallback: ' + cast("RouteIdentity", self.route_identity).fallback_reason if cast("RouteIdentity", self.route_identity).fallback_reason else '',
            'yes' if self.trigger_payload else 'no', (time.monotonic() - t_warm) * 1000
        )

        await self.send({
            "type": wire.WARMUP_READY,
            "data": {"session_id": self.session_id, "llm_mode": llm_mode},
        })

    async def _warmup_failed(self, e: Exception) -> None:
        """The warmup's except arm after its log line: the slot, the binding and
        the seat the try took go back; the daemon hears the failure."""
        from core.concurrency import release_chat_slot
        from core.config.config_builder import release_config_seat
        release_chat_slot(cast("str", self.session_id))  # no id yet: nothing to release
        if self.session_id:
            call_control.unbind(self.session_id, self.send)
        if self.agent_cfg is not None and not self.phone_started:
            # Built, then the spawn failed before binding (the build
            # runs after the id is set).
            release_config_seat(cast("str", self.session_id), self.agent_cfg)
        await self.send({
            "type": wire.ERROR,
            "data": {"message": f"Warmup failed: {e}"},
        })

    async def on_chat(self, msg: dict) -> None:
        """A caller's utterance: one turn as a task (an attach hands it to the
        chat's call)."""
        prompt = msg.get("prompt", "")
        if not prompt:
            await self.send({
                "type": wire.ERROR,
                "data": {"message": "Empty prompt"},
            })
            return

        if self.attach is not None:
            await phone_attach.utterance(self.attach, msg)
            return
        if not self.session_id or not self.layer or not self.chat_id:
            await self.send({
                "type": wire.ERROR,
                "data": {"message": "No session — send warmup first"},
            })
            return

        turn_id = int(msg.get("turn") or 0)
        t = asyncio.create_task(
            self.run_turn(turn_id, prompt, msg.get("barge_in_chars"))
        )
        self.turn_tasks.add(t)
        t.add_done_callback(self.turn_tasks.discard)

    async def on_abort(self, msg: dict) -> None:
        """Barge-in on an in-flight turn."""
        # Barge-in: graceful-first, per layer — the same shape as the
        # duplex chat attach. layer.abort() sends the CLI's stdin
        # interrupt / Codex interrupt; the turn CLOSES normally with
        # the partial reply persisted, so the producer is left
        # running to drain it (the daemon's turn-id filter drops the
        # tail frames). Direct returns False there and falls back to
        # the producer cancel, whose CancelledError unwinds the API
        # stream and pops the un-answered user message so the phone
        # server can resend it batched with the caller's new speech.
        # No-op if the turn already finished (its history stays
        # committed — the client resends regardless, which the model
        # absorbs harmlessly).
        turn_id = msg.get("turn")
        if self.attach is not None:
            await phone_attach.abort(self.attach, msg)
            return
        # A frame without a turn finds no producer.
        producer = self.turn_producers.get(cast("int", turn_id))
        if producer and not producer.done():
            graceful = False
            if self.layer is not None:
                try:
                    # A producer runs only on a warmed call.
                    graceful = bool(await self.layer.abort(cast("str", self.session_id)))
                except Exception:  # noqa: BLE001  # a failed abort reads as not graceful, the call ends either way
                    graceful = False
            logger.info(
                "WS abort: turn %s "
                "(%s, "
                "session=%s)",
                turn_id, 'graceful interrupt' if graceful else 'producer cancel',
                self.session_id
            )
            if not graceful:
                producer.cancel()
            self.interrupted_last_turn = True
        else:
            logger.info("WS abort: turn %s not in flight", turn_id)

    async def on_call_control_result(self, msg: dict) -> None:
        """The daemon's answer to a call-control request."""
        # The daemon's verdict on a call tool (services/phone/call_control.py).
        data = msg.get("data") or {}
        if self.session_id and not call_control.resolve(
                self.session_id, str(data.get("request_id") or ""), data):
            logger.info("WS call control: unmatched result on session=%s", self.session_id)

    async def teardown(self) -> None:
        """The socket's end: the turns stopped and awaited, the attach ended or
        the tools unbound, the session closed and its slot released, an
        ephemeral caller tree pruned."""
        # Stop in-flight turns before tearing the session down, and wait for
        # them — close_session must not race a producer mid-send_message.
        for producer in list(self.turn_producers.values()):
            if not producer.done():
                producer.cancel()
        for t in list(self.turn_tasks):
            if not t.done():
                t.cancel()
        if self.turn_tasks:
            await asyncio.gather(*self.turn_tasks, return_exceptions=True)
        if self.attach is not None:
            # The chat keeps its session; the call's pump finishes its notes.
            phone_attach.end(self.attach)
        elif self.session_id:
            call_control.unbind(self.session_id, self.send)
        # Cleanup session on disconnect
        if self.session_id and self.layer:
            try:
                await self.layer.close_session(self.session_id)
                logger.info("WS session cleaned up: %s", self.session_id)
            except Exception as e:  # noqa: BLE001  # the call's session cleanup at the socket's end
                logger.error("WS session cleanup failed: %s", e)
            from core.concurrency import release_chat_slot
            release_chat_slot(self.session_id)
            # A caller with no durable identity leaves nothing behind.
            if self.ephemeral_home:
                if external_identity.prune_ephemeral(Path(self.ephemeral_home)):
                    logger.info("WS session %s: ephemeral caller tree pruned", self.session_id[:8])


# One method per frame the daemon sends; the close frame and an unknown
# type stay with the receive loop.
_PHONE_FRAMES: dict[str, str] = {
    wire.PHONE_IN_WARMUP: "on_warmup",
    wire.PHONE_IN_CHAT: "on_chat",
    wire.PHONE_IN_ABORT: "on_abort",
    wire.PHONE_IN_CALL_CONTROL_RESULT: "on_call_control_result",
}


async def ws_phone_handler(websocket: WebSocket, key: str = Query(default="")):
    """Persistent WebSocket for phone server communication.

    Protocol (JSON messages):
    Client -> Server:
      {"type": "warmup", "model": "...", "llm_mode": "...", "phone_mode": true}
      {"type": "chat", "prompt": "...", "turn": 3, "barge_in_chars": null}
      {"type": "abort", "turn": 3}
      {"type": "close"}
    Server -> Client (turn-scoped frames echo the chat's "turn"):
      {"type": "warmup_ready", "data": {"session_id": "...", "llm_mode": "..."}}
      {"type": "session", "turn": 3, "data": {"session_id": "..."}}
      {"type": "text", "turn": 3, "data": {"content": "..."}}
      {"type": "tool_start", "turn": 3, "data": {"name": "...", "tool_use_id": "..."}}
      {"type": "tool_end", "turn": 3, "data": {"tool_use_id": "...", "result_preview": "..."}}
      {"type": "done", "turn": 3, "data": {}}
      {"type": "error", "data": {"message": "..."}}   (turn present when turn-scoped)

    Turns run as tasks so the receive loop stays live while streaming:
    "abort" cancels an in-flight turn's producer (barge-in on the Direct
    layer — the direct session pops the un-answered user message so the
    phone server can resend it batched with the caller's new speech), and a
    "chat" that arrives while a previous turn is still draining queues
    behind the layer's per-session lock instead of jamming the socket.
    """
    # Auth: master key via Authorization: Bearer header ONLY — query params
    # are written to access logs, so the legacy ``?key=`` fallback would
    # deposit the master key there (dropped for the first public release;
    # the shipped daemon has sent the header for many versions).
    auth_header = websocket.headers.get("authorization", "")
    bearer = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if key:
        logger.warning("phone WS: ignoring legacy ?key= query auth — use the Authorization header")
    if not config.is_master_key(bearer):
        await websocket.close(code=4001, reason="Invalid API key")
        return

    await websocket.accept()
    logger.info("WebSocket phone connection accepted")

    call = _PhoneCall(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await call.send({"type": wire.ERROR, "data": {"message": "Invalid JSON"}})
                continue

            msg_type = msg.get("type", "")

            # A frame whose type is not a string (a list) answers "Unknown
            # message type": the table never sees it (an unhashable key would
            # raise on the lookup).
            if isinstance(msg_type, str) and msg_type in _PHONE_FRAMES:
                await getattr(call, _PHONE_FRAMES[msg_type])(msg)
            elif msg_type == wire.PHONE_IN_CLOSE:
                logger.info("WS close requested: session=%s", call.session_id)
                break
            else:
                await call.send({
                    "type": wire.ERROR,
                    "data": {"message": f"Unknown message type: {msg_type}"},
                })

    except (WebSocketDisconnect, ConnectionClosed):
        logger.info("WebSocket disconnected: session=%s", call.session_id)
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        await call.teardown()
