"""Tear down a session's browser (camoufox) context when the proxy kills it.

camoufox's stream sidecar (the shared ``mcps/_shared/stream_sidecar.py``) maps the
OtoDock ``session_id`` — injected as ``?session_id=`` on every request — to the
browser context, and exposes ``POST /internal/close-session``. The proxy calls it
on session close so a killed proxy session frees its browser tab immediately,
honouring the "session lives for the agent process, closes on exit" contract.

Best-effort + fire-and-forget: the sidecar's idle-GC is the correctness backstop,
a session with no browser context is a harmless no-op, and this never blocks or
fails session teardown. camoufox always runs beside the proxy, so this hop runs
on the proxy host for both local and remote agents — T1 reaches the published
loopback port, T2 the sibling container by service-DNS (same
``deployment.docker_mcp_host`` resolution the satellite HTTP tunnel uses).
"""
import asyncio
import logging

logger = logging.getLogger("claude-proxy.browser_session")

_MCP_NAME = "camoufox"


def _base_url() -> str | None:
    """camoufox base URL as the proxy reaches it, via the MCP registry port
    (same resolution the satellite HTTP tunnel uses)."""
    try:
        from core.config import deployment
        from services.mcp import mcp_registry
        manifest = mcp_registry.get_manifest(_MCP_NAME)
        port = getattr(getattr(manifest, "server", None), "port", None)
        if port:
            host = deployment.docker_mcp_host(manifest)
            return f"http://{host}:{port}"
    except Exception:
        pass
    return None


async def _close(session_id: str) -> None:
    base = _base_url()
    if not base:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{base}/internal/close-session", json={"session_id": session_id},
            )
            if resp.status_code == 200 and resp.json().get("closed"):
                logger.info("browser context closed for session %s", session_id[:8])
    except Exception as e:
        logger.debug("browser close for %s best-effort failed: %s", session_id[:8], e)


def schedule_close(session_id: str) -> None:
    """Fire-and-forget the browser-context close on the running event loop.

    No-ops if called outside a loop (the sidecar's idle-GC then reclaims it)."""
    if not session_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_close(session_id))
