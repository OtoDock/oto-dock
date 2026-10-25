"""Phone management WebSocket — persistent config push to phone servers.

The phone server connects once at startup and stays connected. The proxy
pushes the full config on connect and after any DB change (route/setting/
credential mutation).

Protocol:
  Server → Client: {"type": "config_full", "data": {...}}     on connect
  Server → Client: {"type": "config_update", "data": {...}}   on DB change
  Server → Client: {"type": "ping"}                            every 30s
  Client → Server: {"type": "pong"}                            keepalive response
  Client → Server: {"type": "request_config"}                  force re-push
  Client → Server: {"type": "capabilities", "duplex": {...}}   daemon feature advert (on connect)
  Server → Client: {"type": "duplex_open", ...}                open a duplex session (ws/duplex.py)
  Client → Server: {"type": "duplex_open_failed", ...}         dial-back refusal (ws/duplex.py)

Unknown frame types are ignored on both sides, so old daemons and old proxies
interop cleanly — a daemon that never sends ``capabilities`` simply has no
duplex support recorded.
"""

import asyncio
import contextlib
import json
import logging
import time

from fastapi import Query, WebSocket

import config
from services.phone import phone_config
from services.phone.phone_config import _management_clients, build_phone_config
from ws.socket_gone import GONE, GONE_WHILE_OPENING

logger = logging.getLogger("otodock")

_PING_INTERVAL_S = 30
_PONG_TIMEOUT_S = 60

def _gone_while_opening(e: BaseException) -> None:
    logger.info("Phone management: the daemon's socket closed while opening (%s)",
                type(e).__name__)


async def ws_phone_management_handler(websocket: WebSocket, key: str = Query(default="")):
    """Persistent management WebSocket for phone server config push."""
    # Auth: master key via Authorization: Bearer header ONLY — query params
    # are written to access logs, so the legacy ``?key=`` fallback would
    # deposit the master key there (dropped for the first public release;
    # the shipped daemon has sent the header for many versions).
    auth_header = websocket.headers.get("authorization", "")
    bearer = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if key:
        logger.warning("phone WS: ignoring legacy ?key= query auth — use the Authorization header")
    authorized = config.is_master_key(bearer)
    try:
        if not authorized:
            await websocket.close(code=4001, reason="Invalid API key")
            return
        await websocket.accept()
    except GONE_WHILE_OPENING as e:
        _gone_while_opening(e)
        return
    logger.info("Phone management WebSocket connected")

    last_pong = time.monotonic()

    try:
        # Registered before the full config is assembled, so a change saved
        # meanwhile reaches it as a push; the finally below unregisters it on
        # every exit.
        _management_clients.add(websocket)
        # Assembled and sent under the push lock: a change saved meanwhile
        # broadcasts after this push, never before it.
        async with phone_config.push_lock():
            config_data = await build_phone_config()
            try:
                # Before the daemon's capabilities frame the push carries no
                # outbound PIN; the frame below re-sends the full config.
                await websocket.send_json({
                    "type": "config_full",
                    "data": phone_config.config_for_daemon(config_data, None),
                })
            except GONE_WHILE_OPENING as e:
                _gone_while_opening(e)
                return
        logger.info("Phone management: sent config_full (version=%s)", config_data['version'])

        # Concurrent read + ping loop
        async def _ping_loop():
            nonlocal last_pong
            while True:
                await asyncio.sleep(_PING_INTERVAL_S)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:  # noqa: BLE001  # the ASGI server's send raises by implementation once the socket is gone
                    break
                # Check pong timeout. Actually close the socket — just breaking
                # out of the ping loop would leave the half-open connection
                # registered in _management_clients (the receive loop blocks
                # forever on a peer that stopped answering). close() wakes it
                # with WebSocketDisconnect → normal teardown.
                if time.monotonic() - last_pong > _PONG_TIMEOUT_S:
                    logger.warning("Phone management: pong timeout, disconnecting")
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1001, reason="pong timeout")
                    break

        ping_task = asyncio.create_task(_ping_loop())

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "pong":
                    last_pong = time.monotonic()

                elif msg_type == "capabilities":
                    caps = {k: v for k, v in msg.items() if k != "type"}
                    phone_config.set_management_capabilities(websocket, caps)
                    logger.info(
                        "Phone management: daemon capabilities %s", caps
                    )
                    if caps.get(phone_config.OUTBOUND_PIN_CAPABILITY):
                        # The connect-time push withheld the outbound PINs;
                        # a daemon that gates outbound calls gets them now.
                        async with phone_config.push_lock():
                            await websocket.send_json({
                                "type": "config_update",
                                "data": phone_config.config_for_daemon(
                                    await build_phone_config(), caps),
                            })

                elif msg_type == "duplex_open_failed":
                    from ws import duplex as ws_duplex
                    ws_duplex.on_open_failed(
                        str(msg.get("duplex_id") or ""),
                        str(msg.get("reason") or "daemon refused"),
                    )

                elif msg_type == "request_config":
                    async with phone_config.push_lock():
                        config_data = await build_phone_config()
                        await websocket.send_json({
                            "type": "config_full",
                            "data": phone_config.config_for_daemon(
                                config_data, phone_config.management_capabilities(websocket)),
                        })
                    logger.info("Phone management: config re-pushed on request")

        except GONE:
            pass
        finally:
            ping_task.cancel()

    except GONE:
        pass
    except Exception as e:
        logger.exception("Phone management WebSocket error: %s", e)
    finally:
        _management_clients.discard(websocket)
        phone_config.clear_management_capabilities(websocket)
        logger.info("Phone management WebSocket disconnected")
