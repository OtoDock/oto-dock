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
"""

import asyncio
import contextlib
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect, Query
from websockets.exceptions import ConnectionClosed

import config
from services.phone.phone_config import assemble_phone_config, _management_clients

logger = logging.getLogger("claude-proxy")

_PING_INTERVAL_S = 30
_PONG_TIMEOUT_S = 60


async def ws_phone_management_handler(websocket: WebSocket, key: str = Query(default="")):
    """Persistent management WebSocket for phone server config push."""
    # Auth: master key via Authorization: Bearer header (query params are
    # written to access logs; ``?key=`` still accepted for older phone
    # daemons). Constant-time compare; fail closed if unset.
    auth_header = websocket.headers.get("authorization", "")
    bearer = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if not config.is_master_key(bearer or key):
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
        logger.info("Phone management WebSocket disconnected")
