"""Duplex session manager — capability advert + duplex_open handling.

The daemon advertises duplex support on the management socket right after
connecting (``capabilities`` frame — old proxies ignore it). When the proxy
asks for a session (``duplex_open``), the manager dials back the per-session
engine socket with the master key, wires the connection + providers from
the per-session config, and runs a ``DuplexSession`` to completion. Any
failure before the dial-back succeeds is reported with
``duplex_open_failed`` so the browser isn't left waiting out its timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os

import websockets

import config

logger = logging.getLogger("duplex")


def duplex_supported() -> bool:
    """Duplex needs the local models (Silero VAD on ONNX Runtime) — the
    ``localmodels`` extra plus the bundled model file. A lean install simply
    doesn't advertise the capability."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    from audio.providers.vad.silero_model import default_model_path
    return os.path.isfile(default_model_path())


def capabilities_frame() -> dict:
    return {
        "type": "capabilities",
        "duplex": {"supported": duplex_supported(), "rates": [16000], "version": 1},
        # The call tools (``call_control`` frames on the per-call socket) and
        # outbound calls that run inside the caller's chat (the warmup's
        # ``attach_grant``): the proxy offers each only to a daemon that
        # advertises it.
        "call_control": True,
        "in_session_calls": True,
        # The PIN gate runs on an outbound call too (a route tied to a user):
        # the proxy accepts a PIN on such a route only while a daemon says so.
        "outbound_pin": True,
    }


def _engine_ws_url(duplex_id: str) -> str:
    return (
        f"{config.PROXY_WS_SCHEME}://{config.PROXY_WS_HOST_PORT}"
        f"/ws/duplex-engine/{duplex_id}"
    )


def _session_stt_provider(session_cfg: dict) -> dict | None:
    """The per-session STT row, with the CHAT endpointing driving the
    pipeline's silence math (the pipeline reads the call-endpointing slot —
    inject the chat value there; the proxy resolved which value applies)."""
    stt = session_cfg.get("stt") or None
    if not stt:
        return None
    stt = copy.deepcopy(stt)
    endpointing = session_cfg.get("chat_endpointing_ms")
    if endpointing:
        adv = dict(stt.get("advanced") or {})
        adv["call_endpointing_ms"] = endpointing
        stt["advanced"] = adv
    return stt


class DuplexManager:
    """Owns every live duplex session task on this daemon."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._sessions: dict[str, asyncio.Task] = {}

    async def handle_open(self, msg: dict, mgmt_ws) -> None:
        """``duplex_open`` from the proxy: dial back and run the session."""
        duplex_id = str(msg.get("duplex_id") or "")
        if not duplex_id:
            return

        async def _fail(reason: str) -> None:
            logger.warning("duplex_open %s refused: %s", duplex_id[:8], reason)
            with contextlib.suppress(Exception):
                await mgmt_ws.send(json.dumps({
                    "type": "duplex_open_failed",
                    "duplex_id": duplex_id,
                    "reason": reason,
                }))

        if not duplex_supported():
            await _fail("localmodels not installed")
            return
        session_cfg = dict(msg.get("config") or {})
        session_cfg["stt"] = _session_stt_provider(session_cfg)
        rate_in = int(msg.get("rate_in") or 16000)
        rate_out = int(msg.get("rate_out") or 24000)

        try:
            ws = await websockets.connect(
                _engine_ws_url(duplex_id),
                additional_headers={
                    "Authorization": f"Bearer {config.PROXY_API_KEY}"},
                max_size=10 * 1024 * 1024,
                close_timeout=5,
            )
        except Exception as e:  # noqa: BLE001  # the open is refused with the dial-back's error, whatever it was
            await _fail(f"dial-back failed: {e}")
            return

        from .connection import DuplexEngineConnection
        from .session import DuplexSession

        conn = DuplexEngineConnection(
            ws, duplex_id, rate_in=rate_in, rate_out=rate_out)
        try:
            session = DuplexSession(conn, self._cfg, session_cfg)
        except Exception as e:
            logger.exception("duplex %s: session build failed: %s", duplex_id[:8], e)
            await conn.close()
            await _fail(f"session build failed: {e}")
            return

        task = asyncio.create_task(self._run(duplex_id, session, conn))
        self._sessions[duplex_id] = task

    async def _run(self, duplex_id: str, session, conn) -> None:
        try:
            await session.run()
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
            self._sessions.pop(duplex_id, None)
            logger.info("duplex %s: session ended", duplex_id[:8])
