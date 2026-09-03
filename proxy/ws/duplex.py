"""Full-duplex chat voice — the browser↔daemon bridge.

Two websockets per duplex session, both terminating here:

  ``/ws/duplex``               the BROWSER. First frame ``{"type":"init",
                               "token":...}`` (single-use duplex token from
                               ``POST /v1/duplex/session``), then binary
                               16 kHz PCM mic frames up / binary 24 kHz TTS
                               frames down, plus JSON control both ways.
  ``/ws/duplex-engine/{id}``   the DAEMON's dial-back (master-key auth),
                               opened in response to a ``duplex_open``
                               management frame. Binary + JSON mirror of the
                               browser socket, plus the turn frames
                               (``utterance`` / ``abort_turn``) that drive
                               the attached chat session.

The proxy only forwards audio — no DSP here. Identity lives in the token
(sub + chat_id); the daemon's master key authenticates it as the engine
peer, never as a user.

Lifecycle anchor rule (the leak-proofing): a duplex session's lifetime is
anchored to its sockets — when EITHER side drops, ``_teardown`` runs once,
aborts any in-flight attached turn, closes both sockets, and unregisters.
A daemon that dials back after the browser gave up is refused (4404); a
duplicate dial-back for a claimed id is refused (4409). The session budget
(``max_seconds`` from the token) is enforced here with a timer.

Browser-visible JSON frames:
  → ``{"type":"ready","duplex_id","rate_in","rate_out"}``  bridge is live
  → ``{"type":"state","state":"listening|thinking|speaking"}``
  → ``{"type":"interim","text"}`` overlay partial /
    ``{"type":"interim_final","text"}`` accumulate chunk /
    ``{"type":"final","text"}`` dispatched utterance
  → ``{"type":"flush"}`` barge-in: drop scheduled playback NOW (the client
    buffers 250 ms–1 s of lead — without this it talks over the user)
  → ``{"type":"pause"}`` / ``{"type":"resume"}`` barge-in pause: suspend the
    player in place (lead retained — lossless resume) while the daemon
    waits for transcript confirmation; resume continues where it froze
  → ``{"type":"end","reason"}`` / ``{"type":"error","reason"}``
  ← ``{"type":"barge_in"}`` manual tap, ``{"type":"mute"}``/``{"type":"unmute"}``,
    ``{"type":"end"}``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

import config
from services.media import audio_service, duplex_service, ws_audio_token

logger = logging.getLogger("claude-proxy")

# How long the browser side waits for the daemon's dial-back before giving
# up (this wait IS the pending-entry TTL — no sweeper needed).
_DIAL_BACK_TIMEOUT_S = 10.0

# Duplex audio format (v1): browser mic in / TTS out.
RATE_IN = 16000
RATE_OUT = 24000


@dataclass
class DuplexBridge:
    """One live duplex session: both sockets + lifecycle state."""

    duplex_id: str
    sub: str
    chat_id: str
    max_seconds: int
    browser_ws: WebSocket
    engine_ws: WebSocket | None = None
    engine_ready: asyncio.Event = field(default_factory=asyncio.Event)
    open_failed_reason: str | None = None
    closed: bool = False
    budget_task: asyncio.Task | None = None
    # A4: the attached-turn runner registers its abort hook here so teardown
    # can kill an in-flight turn (graceful-first, per layer).
    abort_inflight_turn = None  # async callable | None


# Sessions awaiting the daemon dial-back / live sessions, by duplex_id.
_pending: dict[str, DuplexBridge] = {}
_active: dict[str, DuplexBridge] = {}


def on_open_failed(duplex_id: str, reason: str) -> None:
    """Management-socket callback: the daemon refused ``duplex_open``."""
    bridge = _pending.get(duplex_id)
    if bridge is not None:
        bridge.open_failed_reason = reason or "daemon refused"
        bridge.engine_ready.set()


async def _send_json(ws: WebSocket | None, payload: dict) -> None:
    if ws is None:
        return
    with contextlib.suppress(Exception):
        await ws.send_json(payload)


async def _close_ws(ws: WebSocket | None, code: int = 1000) -> None:
    if ws is None:
        return
    with contextlib.suppress(Exception):
        await ws.close(code=code)


async def _teardown(bridge: DuplexBridge, reason: str) -> None:
    """Idempotent whole-session teardown — the anchor rule's single exit."""
    if bridge.closed:
        return
    bridge.closed = True
    _pending.pop(bridge.duplex_id, None)
    _active.pop(bridge.duplex_id, None)
    if bridge.budget_task is not None:
        bridge.budget_task.cancel()
        bridge.budget_task = None
    # A dead duplex session must never leave a running producer behind.
    # Exiting spoken mode is NOT Stop (operator decision 2026-08-14): an
    # in-flight attached turn keeps producing into the chat — the dashboard
    # Stop button owns deliberate aborts. duplex_attach queues a mode-exit
    # system note the agent sees once the turn drains.
    from ws import duplex_attach
    resumed = False
    with contextlib.suppress(Exception):
        resumed = duplex_attach.on_teardown_resume(bridge.duplex_id)
    bridge.abort_inflight_turn = None
    duplex_attach.on_teardown_state(bridge.duplex_id)
    await _send_json(bridge.browser_ws, {"type": "end", "reason": reason})
    await _send_json(bridge.engine_ws, {"type": "end", "reason": reason})
    await _close_ws(bridge.browser_ws)
    await _close_ws(bridge.engine_ws)
    logger.info(
        f"Duplex {bridge.duplex_id[:8]}: closed ({reason})"
        + (" — in-flight turn left running" if resumed else "")
    )


async def _budget_loop(bridge: DuplexBridge) -> None:
    """Whole-session cap from the token — the proxy owns the budget."""
    try:
        await asyncio.sleep(max(1, bridge.max_seconds))
    except asyncio.CancelledError:
        return
    await _teardown(bridge, "budget")


# ---------------------------------------------------------------------------
# Browser side
# ---------------------------------------------------------------------------

async def ws_duplex_handler(websocket: WebSocket):
    """The browser's duplex socket: token init → dial-back wait → pump."""
    await websocket.accept()

    # First frame: init {token}. Validate + consume before anything else.
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        init = json.loads(raw)
    except Exception:
        await _close_ws(websocket, 4400)
        return
    claims = ws_audio_token.validate_ws_audio_token(
        str(init.get("token") or ""), purpose=ws_audio_token.PURPOSE_DUPLEX,
    ) if init.get("type") == "init" else None
    if not claims or not ws_audio_token.consume_jti(claims.get("jti", "")):
        await _send_json(websocket, {"type": "error", "reason": "bad_token"})
        await _close_ws(websocket, 4401)
        return

    sub = str(claims.get("sub") or "")
    chat_id = str(claims.get("chat_id") or "")

    # Availability + access re-check at attach time (a token may outlive an
    # admin flip or a role change by up to its TTL).
    cap = await asyncio.to_thread(audio_service.duplex_capability)
    if not cap.get("available"):
        await _send_json(websocket, {"type": "error", "reason": "engine_offline"})
        await _close_ws(websocket, 4503)
        return
    user_row = await asyncio.to_thread(_get_user, sub)
    denial = await asyncio.to_thread(
        duplex_service.chat_access_denied_reason, chat_id,
        user_sub=sub, user_role=(user_row or {}).get("role") or "",
        user_agents=(user_row or {}).get("agents") or [],
    )
    if denial:
        await _send_json(websocket, {"type": "error", "reason": "access_denied"})
        await _close_ws(websocket, 4403)
        return

    bridge = DuplexBridge(
        duplex_id=uuid.uuid4().hex, sub=sub, chat_id=chat_id,
        max_seconds=int(claims.get("max_seconds") or 0) or 1800,
        browser_ws=websocket,
    )
    _pending[bridge.duplex_id] = bridge

    # Ask the daemon (newest duplex-capable management connection) to dial
    # back a per-session engine socket.
    from services.phone import phone_config
    mgmt_ws = phone_config.pick_duplex_client()
    if mgmt_ws is None:
        _pending.pop(bridge.duplex_id, None)
        await _send_json(websocket, {"type": "error", "reason": "engine_offline"})
        await _close_ws(websocket, 4503)
        return
    session_config = await asyncio.to_thread(
        phone_config.assemble_duplex_session_config, sub)
    try:
        await mgmt_ws.send_json({
            "type": "duplex_open",
            "duplex_id": bridge.duplex_id,
            "rate_in": RATE_IN,
            "rate_out": RATE_OUT,
            "config": session_config,
        })
    except Exception:
        _pending.pop(bridge.duplex_id, None)
        await _send_json(websocket, {"type": "error", "reason": "engine_offline"})
        await _close_ws(websocket, 4503)
        return

    # Wait for the dial-back (or a refusal). This wait is the pending TTL.
    try:
        await asyncio.wait_for(
            bridge.engine_ready.wait(), timeout=_DIAL_BACK_TIMEOUT_S)
    except asyncio.TimeoutError:
        await _teardown(bridge, "engine_timeout")
        return
    if bridge.open_failed_reason is not None:
        await _teardown(bridge, bridge.open_failed_reason)
        return

    bridge.budget_task = asyncio.create_task(_budget_loop(bridge))
    await _send_json(websocket, {
        "type": "ready", "duplex_id": bridge.duplex_id,
        "rate_in": RATE_IN, "rate_out": RATE_OUT,
    })
    logger.info(
        f"Duplex {bridge.duplex_id[:8]}: live (sub={sub}, chat={chat_id})"
    )

    # Pump browser → engine until either side dies.
    try:
        while not bridge.closed:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                with contextlib.suppress(Exception):
                    await bridge.engine_ws.send_bytes(data)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                continue
            ftype = frame.get("type", "")
            if ftype == "end":
                break
            if ftype in ("barge_in", "mute", "unmute", "hold"):
                # hold = click-to-edit: mic muted AND the engine drops its
                # pending turn accumulation (the composer owns the text now).
                await _send_json(bridge.engine_ws, {"type": ftype})
            elif ftype == "release":
                # Sticky-mute release: mic live again; an optional held
                # draft rides along to re-seed the engine's pending turn —
                # a bare-type relay would silently strip it (audit F2).
                await _send_json(bridge.engine_ws, {
                    "type": "release",
                    "text": str(frame.get("text") or "")[:8000],
                })
            elif ftype == "typed_utterance":
                # Click-to-edit send: the edited text runs as a spoken-mode
                # turn (reply comes back as TTS) instead of a typed chat
                # turn. Size-capped like any user text input.
                await _send_json(bridge.engine_ws, {
                    "type": "typed_utterance",
                    "text": str(frame.get("text") or "")[:8000],
                })
            elif ftype == "client_stat":
                # Browser-side playback diagnostics (2026-08-12 turn-end gap
                # hunt): audio underruns + main-thread long tasks, capped
                # client-side. Log-only — an underrun paired with a longtask
                # at the same moment = browser render jank; an underrun with
                # no longtask = upstream (relay) stall.
                logger.info(
                    "Duplex %s: client %s %s",
                    bridge.duplex_id[:8], frame.get("kind"),
                    {k: v for k, v in frame.items()
                     if k not in ("type", "kind")},
                )
    except (WebSocketDisconnect, ConnectionClosed):
        pass
    finally:
        await _teardown(bridge, "browser_closed")


def _get_user(sub: str) -> dict | None:
    """User row + assigned agents for the access re-check."""
    from storage import database as task_store
    user = task_store.get_user(sub)
    if not user:
        return None
    return {
        "role": user.get("role") or "",
        "agents": list(task_store.get_user_agent_roles(sub).keys()),
    }


# ---------------------------------------------------------------------------
# Engine side (daemon dial-back)
# ---------------------------------------------------------------------------

async def ws_duplex_engine_handler(websocket: WebSocket, duplex_id: str):
    """The daemon's per-session socket. Master-key peer; claims a pending
    bridge and pumps engine frames to the browser (turn frames → A4)."""
    auth_header = websocket.headers.get("authorization", "")
    bearer = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if not config.is_master_key(bearer):
        await websocket.close(code=4001, reason="Invalid API key")
        return

    bridge = _pending.get(duplex_id)
    if bridge is None or bridge.closed:
        # Orphan dial-back: the browser already left (or never existed).
        await websocket.close(code=4404, reason="unknown duplex session")
        return
    if bridge.engine_ws is not None:
        await websocket.close(code=4409, reason="duplicate dial-back")
        return

    await websocket.accept()
    bridge.engine_ws = websocket
    _active[duplex_id] = _pending.pop(duplex_id, bridge)
    bridge.engine_ready.set()

    end_reason = "engine_closed"
    try:
        while not bridge.closed:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                with contextlib.suppress(Exception):
                    await bridge.browser_ws.send_bytes(data)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                continue
            ftype = frame.get("type", "")
            if ftype in ("state", "interim", "interim_final", "final", "flush",
                         "pause", "resume"):
                await _send_json(bridge.browser_ws, frame)
            elif ftype == "utterance":
                await _handle_utterance(bridge, frame)
            elif ftype == "abort_turn":
                await _handle_abort_turn(bridge, frame)
            elif ftype == "end":
                # The engine says WHY ("agent_complete" = [DUPLEX_COMPLETE]
                # farewell — the browser skips its exit note) — keep it.
                end_reason = str(frame.get("reason") or "engine_closed")
                break
    except (WebSocketDisconnect, ConnectionClosed):
        pass
    finally:
        await _teardown(bridge, end_reason)


# ---------------------------------------------------------------------------
# Turn frames — filled in by the chat-attach layer (A4)
# ---------------------------------------------------------------------------

async def _handle_utterance(bridge: DuplexBridge, frame: dict) -> None:
    from ws import duplex_attach
    await duplex_attach.run_utterance(bridge, frame)


async def _handle_abort_turn(bridge: DuplexBridge, frame: dict) -> None:
    from ws import duplex_attach
    await duplex_attach.abort_turn(bridge, frame)
