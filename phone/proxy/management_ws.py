"""Persistent WebSocket client for proxy config management.

Connects to the proxy's /ws/phone-management endpoint at startup and
stays connected for the lifetime of the phone server. Receives config
pushes from the proxy whenever settings change in the dashboard.

Auto-reconnects with exponential backoff on disconnect.
"""

import asyncio
import contextlib
import json
import logging
from typing import Callable

import websockets

logger = logging.getLogger("management_ws")


class ManagementWSClient:
    """WebSocket client for receiving config from the proxy."""

    def __init__(self, proxy_url: str, api_key: str):
        # Parse proxy URL into WS URL. The key travels in the Authorization
        # header, never the URL — the proxy's access log records the full
        # path+query of every WS handshake.
        host_port = proxy_url.rstrip("/").replace("http://", "").replace("https://", "")
        scheme = "wss" if proxy_url.lower().startswith("https://") else "ws"
        self._ws_url = f"{scheme}://{host_port}/ws/phone-management"
        self._api_key = api_key
        self._config: dict = {}
        self._config_ready = asyncio.Event()
        self._config_version: int = 0
        self.on_config_changed: Callable[[dict], None] | None = None
        # duplex_open handler: async (msg, ws) — wired by main.py to the
        # DuplexManager. The capabilities frame advertises support on connect.
        self.on_duplex_open = None
        self.capabilities_frame: dict | None = None
        self._running = True

    async def connect(self) -> None:
        """Connect and maintain the WebSocket with auto-reconnect."""
        backoff = 1.0
        max_backoff = 30.0

        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    additional_headers={"Authorization": f"Bearer {self._api_key}"},
                    ping_interval=None,  # proxy sends pings, we send pongs
                    close_timeout=5,
                ) as ws:
                    logger.info("Management WebSocket connected")
                    backoff = 1.0  # Reset on successful connect

                    # Advertise daemon capabilities (duplex …) before anything
                    # else — old proxies ignore unknown frames.
                    if self.capabilities_frame:
                        with contextlib.suppress(Exception):
                            await ws.send(json.dumps(self.capabilities_frame))

                    await self._handle_messages(ws)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    f"Management WebSocket disconnected: {e}. "
                    f"Reconnecting in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _handle_messages(self, ws) -> None:
        """Process messages from the proxy."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type in ("config_full", "config_update"):
                data = msg.get("data", {})
                version = data.get("version", 0)

                self._config = data
                self._config_version = version

                if not self._config_ready.is_set():
                    self._config_ready.set()
                    logger.info(f"Initial config received (version={version})")
                else:
                    logger.info(f"Config updated (version={version})")

                if self.on_config_changed:
                    try:
                        self.on_config_changed(data)
                    except Exception as e:
                        logger.error(f"Config change handler error: {e}")

            elif msg_type == "ping":
                try:
                    await ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    break

            elif msg_type == "duplex_open":
                handler = self.on_duplex_open
                if handler is not None:
                    asyncio.create_task(handler(msg, ws))

    async def wait_for_config(self) -> dict:
        """Block until the first config is received from the proxy."""
        await self._config_ready.wait()
        return self._config

    def stop(self) -> None:
        """Signal the client to stop reconnecting."""
        self._running = False
