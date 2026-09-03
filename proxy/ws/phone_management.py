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

from fastapi import WebSocket, WebSocketDisconnect, Query
from websockets.exceptions import ConnectionClosed

import config
from services.phone import phone_config
from services.phone.phone_config import assemble_phone_config, _management_clients

logger = logging.getLogger("claude-proxy")

_PING_INTERVAL_S = 30
_PONG_TIMEOUT_S = 60


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
    if not config.is_master_key(bearer):
        await websocket.close(code=4001, reason="Invalid API key")
        return

    await websocket.accept()
    logger.info("Phone management WebSocket connected")

    _management_clients.add(websocket)
    last_pong = time.monotonic()

    try:
        # Send full config immediately on connect
        config_data = assemble_phone_config()
        await websocket.send_json({"type": "config_full", "data": config_data})
        logger.info(f"Phone management: sent config_full (version={config_data['version']})")

        # Concurrent read + ping loop
        async def _ping_loop():
            nonlocal last_pong
            while True:
                await asyncio.sleep(_PING_INTERVAL_S)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
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
                        f"Phone management: daemon capabilities {caps}"
                    )

                elif msg_type == "duplex_open_failed":
                    from ws import duplex as ws_duplex
                    ws_duplex.on_open_failed(
                        str(msg.get("duplex_id") or ""),
                        str(msg.get("reason") or "daemon refused"),
                    )

                elif msg_type == "request_config":
                    config_data = assemble_phone_config()
                    await websocket.send_json({
                        "type": "config_full",
                        "data": config_data,
                    })
                    logger.info("Phone management: config re-pushed on request")

        except (WebSocketDisconnect, ConnectionClosed):
            pass
        finally:
            ping_task.cancel()

    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as e:
        logger.error(f"Phone management WebSocket error: {e}", exc_info=True)
    finally:
        _management_clients.discard(websocket)
        phone_config.clear_management_capabilities(websocket)
        logger.info("Phone management WebSocket disconnected")
