"""WebSocket phone endpoint — persistent connection for the phone server.

Uses the ExecutionLayer abstraction — same code for CLI and Direct LLM agents.
Routes through ChatStreamPump for DB persistence, cost tracking, and usage metering.

Registered by app.py as @app.websocket("/ws/phone").

A call session carries client_type / source_type "phone" (the discriminator
that drives MCP filtering — see proxy/adapters/phone.py).
"""

import asyncio
import contextlib
import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect, Query
from websockets.exceptions import ConnectionClosed

import config
from storage import database as task_store
from storage import trigger_store
from storage import phone_route_store
from core.events.common_events import (
    CommonEvent, ERROR, PRODUCER_DONE,
)
from core.session.session_manager import get_execution_layer
from core.events.stream_pump import ChatStreamPump, _active_pumps
from core.config.phone_config_builder import build_phone_agent_config, resolve_phone_execution_target

logger = logging.getLogger("claude-proxy")


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

    if pump_type == "ws_event":
        event = item.get("event", {})
        etype = event.get("type", "")

        if etype == "text":
            content = event.get("content", "")
            if content:
                return {"type": "text", "turn": turn_id, "data": {"content": content}}

        elif etype == "tool_use":
            return {"type": "tool_start", "turn": turn_id, "data": {
                "name": event.get("name", ""),
                "tool_use_id": event.get("tool_id", ""),
            }}

        elif etype == "tool_result":
            return {"type": "tool_end", "turn": turn_id, "data": {
                "tool_use_id": event.get("tool_id", ""),
                "result_preview": event.get("result_preview", ""),
            }}

        elif etype == "session":
            return {"type": "session", "turn": turn_id, "data": {
                "session_id": event.get("session_id", ""),
            }}

        # Skip: thinking, metadata, subagent, delegate, plan, todo, etc.
        return None

    elif pump_type in ("all_done", "pump_ended"):
        return {"type": "done", "turn": turn_id, "data": {}}

    elif pump_type == "error":
        return {"type": "error", "turn": turn_id, "data": {
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
    # Auth: master key via Authorization: Bearer header (query params are
    # written to access logs; ``?key=`` still accepted for older phone
    # daemons). Constant-time compare; fail closed if unset.
    auth_header = websocket.headers.get("authorization", "")
    bearer = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if not config.is_master_key(bearer or key):
        await websocket.close(code=4001, reason="Invalid API key")
        return

    await websocket.accept()
    logger.info("WebSocket phone connection accepted")

    session_id: str | None = None
    chat_id: str | None = None
    layer = None  # ExecutionLayer — resolved on warmup
    model_name: str = "personal-assistant"
    first_turn = True

    # Turns stream from tasks so this receive loop stays responsive to
    # "abort" (barge-in) and follow-up "chat" while a turn is in flight.
    # Sends are serialized: two turns can overlap when the client moved on
    # from a turn it abandoned (proxy mode drains it to completion).
    send_lock = asyncio.Lock()
    turn_producers: dict[int, asyncio.Task] = {}
    turn_tasks: set[asyncio.Task] = set()

    async def _send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def _run_turn(turn_id: int, prompt: str, barge_in_chars) -> None:
        """Run one chat turn: producer -> pump -> WS frames stamped with turn_id."""
        nonlocal first_turn
        producer: asyncio.Task | None = None
        try:
            # Save user message to DB
            task_store.add_chat_message(chat_id, "user", prompt)

            # Set title from first user message
            if first_turn:
                title = prompt[:50].strip()
                if len(prompt) > 50:
                    title += "..."
                task_store.update_chat(chat_id, title=title)
                first_turn = False

            event_queue: asyncio.Queue = asyncio.Queue()
            local_session_id = session_id

            async def _produce():
                try:
                    async with layer.session_lock(local_session_id):
                        async for event in layer.send_message(
                            local_session_id, prompt,
                            barge_in_chars=barge_in_chars,
                            inject_time=True,
                        ):
                            await event_queue.put(event)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Phone producer error: {e}", exc_info=True)
                    await event_queue.put(CommonEvent(ERROR, {"message": str(e)}))
                finally:
                    try:
                        event_queue.put_nowait(CommonEvent(PRODUCER_DONE, {}))
                    except Exception as e:
                        logger.debug("phone producer done signal failed: %s", e)

            producer = asyncio.create_task(_produce())
            turn_producers[turn_id] = producer

            # Create pump for this turn
            pump = ChatStreamPump(
                chat_id=chat_id,
                session_id=local_session_id,
                producer=producer,
                event_queue=event_queue,
                perm_queue=None,  # phone uses auto mode — no permission prompts
                scope="agent",
                source_type="phone",
            )
            _active_pumps[chat_id] = pump
            pump.start()

            # Attach as phone subscriber and stream events
            ws_queue = pump.attach()

            while True:
                try:
                    item = await ws_queue.get()
                except Exception:
                    break

                phone_msg = _pump_item_to_phone_ws(item, turn_id)
                if phone_msg:
                    await _send(phone_msg)

                # Exit subscriber loop when pump is done
                pt = item.get("pump_type", "")
                if pt in ("all_done", "pump_ended", "error"):
                    break

        except (WebSocketDisconnect, ConnectionClosed):
            # Socket died mid-stream — stop generating into the void; the
            # main receive loop tears the connection down.
            if producer and not producer.done():
                producer.cancel()
        except Exception as e:
            logger.error(f"WS chat error: {e}", exc_info=True)
            with contextlib.suppress(Exception):
                await _send({"type": "error", "turn": turn_id,
                             "data": {"message": str(e)}})
        finally:
            turn_producers.pop(turn_id, None)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send({"type": "error", "data": {"message": "Invalid JSON"}})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "warmup":
                model_name = msg.get("model", "")
                if not model_name:
                    await _send({"type": "error", "data": {"message": "Agent name required in warmup 'model' field"}})
                    continue
                llm_mode = msg.get("llm_mode", "proxy")  # echoed back for phone server compat
                phone_mode = msg.get("phone_mode", False)
                call_type = msg.get("call_type", "inbound")
                phone_context_override = msg.get("phone_context_override", "")
                existing_sid = msg.get("session_id")  # pre-warmed session to reuse

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

                try:
                    # Resolve the target up front (shared helper → same result
                    # as build_phone_agent_config) so the layer matches where
                    # the (pre-warmed or new) session actually runs.
                    phone_target = await asyncio.to_thread(
                        resolve_phone_execution_target, model_name,
                    )
                    layer = get_execution_layer(
                        model_name, execution_target=phone_target,
                    )

                    # Reuse pre-warmed session if provided and still alive
                    if existing_sid and await layer.is_session_alive(existing_sid):
                        session_id = existing_sid
                        # Recover this session's chat_id — the reused connection
                        # needs it for chat turns (this branch runs on a NEW ws
                        # connection after a daemon reconnect, so chat_id is
                        # otherwise unset). Fall back to a fresh chat row if the
                        # link is somehow missing so the call still proceeds.
                        prior = await asyncio.to_thread(
                            task_store.get_chat_by_session, session_id,
                        )
                        if prior:
                            chat_id = prior["id"]
                            first_turn = False
                        else:
                            # Link missing (shouldn't happen now that warmup
                            # persists it) — rebuild a chat row so the call
                            # still proceeds. Resolve execution_path the same
                            # way the fresh-session path below does.
                            _cfg = await build_phone_agent_config(
                                agent_name=model_name, call_type=call_type,
                                phone_context_override=phone_context_override,
                                phone_mode=phone_mode, trigger_payload=None,
                            )
                            chat_id = str(uuid.uuid4())
                            first_turn = True
                            task_store.create_chat(
                                chat_id, "phone", model_name, "auto",
                                model=config.get_cli_model(model_name),
                                execution_path=_cfg.execution_path or "claude-code-cli",
                                source_type="phone",
                            )
                            task_store.update_chat(chat_id, session_id=session_id)
                        logger.info(
                            f"WS warmup: reusing pre-warmed session={session_id}, "
                            f"chat={chat_id}"
                        )
                        await _send({
                            "type": "warmup_ready",
                            "data": {"session_id": session_id, "llm_mode": llm_mode},
                        })
                        continue

                    # Resolve route → trigger → payload. Best-effort: any
                    # miss (no route_id/UUID, no route, no trigger_slug, no
                    # trigger row) leaves trigger_payload=None and the call
                    # proceeds without enrichment.
                    trigger_payload = await _resolve_trigger_payload(
                        agent_name=model_name,
                        phone_route_id=phone_route_id,
                        audiosocket_uuid=audiosocket_uuid,
                        caller_phone=caller_phone,
                        caller_did=caller_did,
                        dial_event=dial_event,
                    )

                    session_id = str(uuid.uuid4())
                    chat_id = str(uuid.uuid4())
                    first_turn = True

                    agent_cfg = await build_phone_agent_config(
                        agent_name=model_name,
                        call_type=call_type,
                        phone_context_override=phone_context_override,
                        phone_mode=phone_mode,
                        trigger_payload=trigger_payload,
                    )

                    # Check concurrency limit before spawning session. Phone can
                    # run on a remote satellite (phone_target) — pass it so a
                    # remote call doesn't consume a local-G slot (its satellite
                    # enforces). Local calls take a unit of the local ceiling.
                    from core.concurrency import acquire_chat_slot
                    adm = await acquire_chat_slot(session_id, target=phone_target,
                                                  execution_path=agent_cfg.execution_path)
                    if not adm:
                        await _send({
                            "type": "error",
                            "data": {"message": adm.user_message},
                        })
                        continue

                    await layer.start_session(session_id, agent_cfg)

                    # Create chat row for persistence. ``"phone"`` is the
                    # sentinel owner (a call has no real user) + the source_type
                    # discriminator.
                    execution_path = agent_cfg.execution_path or "claude-code-cli"
                    task_store.create_chat(
                        chat_id, "phone", model_name, "auto",
                        model=config.get_cli_model(model_name),
                        execution_path=execution_path,
                        source_type="phone",
                    )
                    # Link session→chat so a later reconnect that reuses this
                    # pre-warmed session can recover its chat_id (the reuse
                    # branch above resolves session_id but not chat_id — without
                    # this the reconnected connection's chat turns 404 with
                    # "No session — send warmup first").
                    task_store.update_chat(chat_id, session_id=session_id)

                    logger.info(
                        f"WS warmup: session={session_id}, chat={chat_id}, "
                        f"agent={model_name}, "
                        f"trigger={'yes' if trigger_payload else 'no'}"
                    )

                    await _send({
                        "type": "warmup_ready",
                        "data": {"session_id": session_id, "llm_mode": llm_mode},
                    })
                except Exception as e:
                    logger.error(f"WS warmup failed: {e}", exc_info=True)
                    from core.concurrency import release_chat_slot
                    release_chat_slot(session_id)
                    await _send({
                        "type": "error",
                        "data": {"message": f"Warmup failed: {e}"},
                    })

            elif msg_type == "chat":
                prompt = msg.get("prompt", "")
                if not prompt:
                    await _send({
                        "type": "error",
                        "data": {"message": "Empty prompt"},
                    })
                    continue

                if not session_id or not layer or not chat_id:
                    await _send({
                        "type": "error",
                        "data": {"message": "No session — send warmup first"},
                    })
                    continue

                turn_id = int(msg.get("turn") or 0)
                t = asyncio.create_task(
                    _run_turn(turn_id, prompt, msg.get("barge_in_chars"))
                )
                turn_tasks.add(t)
                t.add_done_callback(turn_tasks.discard)

            elif msg_type == "abort":
                # Barge-in: cancel the in-flight turn's producer. On the
                # Direct layer the CancelledError unwinds the API stream and
                # any in-flight MCP tool; the direct session pops the
                # un-answered user message so the phone server can resend it
                # batched with the caller's new speech. No-op if the turn
                # already finished (its history stays committed — the client
                # resends regardless, which the model absorbs harmlessly).
                turn_id = msg.get("turn")
                producer = turn_producers.get(turn_id)
                if producer and not producer.done():
                    logger.info(
                        f"WS abort: cancelling turn {turn_id} "
                        f"(session={session_id})"
                    )
                    producer.cancel()
                else:
                    logger.info(f"WS abort: turn {turn_id} not in flight")

            elif msg_type == "close":
                logger.info(f"WS close requested: session={session_id}")
                break

            else:
                await _send({
                    "type": "error",
                    "data": {"message": f"Unknown message type: {msg_type}"},
                })

    except (WebSocketDisconnect, ConnectionClosed):
        logger.info(f"WebSocket disconnected: session={session_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        # Stop in-flight turns before tearing the session down, and wait for
        # them — close_session must not race a producer mid-send_message.
        for producer in list(turn_producers.values()):
            if not producer.done():
                producer.cancel()
        for t in list(turn_tasks):
            if not t.done():
                t.cancel()
        if turn_tasks:
            await asyncio.gather(*turn_tasks, return_exceptions=True)
        # Cleanup session on disconnect
        if session_id and layer:
            try:
                await layer.close_session(session_id)
                logger.info(f"WS session cleaned up: {session_id}")
            except Exception as e:
                logger.error(f"WS session cleanup failed: {e}")
            from core.concurrency import release_chat_slot
            release_chat_slot(session_id)
