#!/usr/bin/env python3
"""Generic session-lifecycle sidecar for streamable-HTTP Docker MCPs.

A thin streaming reverse-proxy that sits in front of a streamable-HTTP MCP server
(which runs on an internal port). It is **MCP-agnostic** and fully **env-driven**
— it knows nothing about the browser/playwright behind it.

Why it exists: ``--isolated``-style MCP servers (e.g. ``@playwright/mcp``) give
each MCP session its own server-side state (a browser context) but have NO
*explicit* idle/session GC at the OtoDock layer — when an agent's CLI exits
without sending the MCP session-terminating ``DELETE``, the server keeps that
context around and they pile up (observed on camoufox: 25-45s navigates, then a
watchdog wedge). This sidecar adds the lifecycle the OtoDock layer needs, without
otherwise touching the protocol:

  * **Learns each session**: maps the OtoDock ``?session_id=<oto>`` the proxy
    injects on every request → the ``mcp-session-id`` the server assigns on
    ``initialize``, plus a last-activity stamp.
  * **Idle GC** (``SESSION_IDLE_S``): a session idle this long is torn down by
    sending the server a ``DELETE`` for its ``mcp-session-id`` — the backstop.
  * **Active close**: ``POST /internal/close-session {"session_id": "<oto>"}``
    (the OtoDock proxy calls this at agent-session close) tears the matching
    session down immediately. So "kill the proxy session → kill the MCP session."

Everything else is **streamed straight through** (transparent proxy) with no
overall read-timeout — a long-running browser action must not be cut off, and the
long-lived server→client SSE ``GET`` (which carries e.g. an MCP ``roots/list``
request) passes untouched so the client sees server messages immediately. An idle
SSE stream gets periodic keepalive comments so an idle-read timeout on any hop
can't drop it.

Session lifetime (measured + read from the @playwright/mcp 0.0.68 source,
2026-07-06): the MCP server HEARTBEATS every streamable session — a
server→client ``ping`` every 3s that must be answered within 5s, else it
closes the session and destroys its browser context. Delivering the ping
requires the session's standalone server→client SSE ``GET`` stream, so a
session lives exactly as long as its client holds that GET open and answers
pings. Agent sessions therefore persist across think-gaps while the CLI stays
connected (page/tab state survives), and the container's healthprobe keepalive
daemon does the same for the ONE probe session (see healthprobe.py — a
fire-and-forget prober's sessions die ~5-8s after initialize, each leaking a
browser context). A wedged tool call returns an error after
``PER_CALL_TIMEOUT_S`` rather than hanging the agent forever (non-streaming
requests only; the long-lived SSE ``GET`` stays unbounded so real streams are
never cut).

The MCP server always runs on the proxy host, so the proxy reaches
``/internal/...`` over a direct localhost hop for both local and remote agents.
Stdlib + aiohttp; aiohttp is imported lazily so the lifecycle helpers are
unit-testable without it.
"""
import asyncio
import contextlib
import logging
import os
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [sidecar] %(message)s")
log = logging.getLogger("sidecar")

UPSTREAM = os.environ.get("MCP_UPSTREAM", "http://127.0.0.1:8930")
LISTEN_PORT = int(os.environ.get("ROUTER_PORT", "8931"))
SESSION_IDLE_S = int(os.environ.get("SESSION_IDLE_S", "600"))   # 10 min → idle-GC
GC_INTERVAL_S = 60
# Safety sweep of the SHARED /screenshots source. The proxy relocates each shot
# into the user's workspace within seconds, so anything left past the age cutoff
# is an orphan; a hard count cap bounds a concurrent-multi-user burst. The GRACE
# floor guarantees the count cap can never reap a still-in-flight shot. (No-op
# when SCREENSHOTS_DIR is absent — only the camoufox MCP populates it.)
SCREENSHOTS_DIR = os.environ.get("SCREENSHOTS_DIR", "/screenshots")
SCREENSHOTS_MAX_AGE_S = int(os.environ.get("SCREENSHOTS_MAX_AGE_S", str(40 * 60)))
SCREENSHOTS_MAX_COUNT = int(os.environ.get("SCREENSHOTS_MAX_COUNT", "200"))
SCREENSHOTS_GRACE_S = int(os.environ.get("SCREENSHOTS_GRACE_S", "180"))
# Emit an SSE keepalive comment on an idle text/event-stream so a client/hop with
# an idle read-timeout doesn't drop a quiet long-lived stream.
SSE_KEEPALIVE_S = int(os.environ.get("SSE_KEEPALIVE_S", "20"))
_KEEPALIVE = b": keepalive\n\n"

# Upstream read budget for NON-streaming requests: a wedged tools/call (e.g. a
# screenshot of a stuck page) fails after this instead of hanging the agent forever.
# The long-lived SSE GET stays unbounded (sock_read=None) so real streams aren't cut.
PER_CALL_TIMEOUT_S = int(os.environ.get("OTO_MCP_CALL_TIMEOUT_S", "90"))

# ---------------------------------------------------------------------------
# TEMPORARY WORKAROUND — suppress server→client (server-initiated) requests.
#
# History (workaround REMOVED 2026-07-06): @playwright/mcp@0.0.55 failed to
# correlate server→client request responses (roots/list, ping), hanging every
# first tool call ~60s; the OTO_MCP_SUPPRESS_SERVER_REQUESTS workaround
# stripped the client's `roots` capability from `initialize` and 405'd the
# standalone SSE GET so the server could never issue those requests. camoufox
# now runs 0.0.68 (correlation fixed — sub-second navigates verified in
# production with the flag unset everywhere), and rejecting the GET would kill
# every session within seconds anyway: it carries the server's heartbeat ping
# (see the module docstring + healthprobe.py). Do not resurrect the flag.
# ---------------------------------------------------------------------------

# Headers that must not be copied verbatim across a proxy hop.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

# mcp-session-id → {"last": float, "oto": str}
_sessions: dict[str, dict] = {}
# oto session_id → mcp-session-id (for active close-by-oto)
_oto_to_mcp: dict[str, str] = {}

# One shared client to the upstream; no total timeout (long-lived SSE streams).
_client = None  # aiohttp.ClientSession | None — set in main()


def _touch(mcp_sid: str, oto: str = "") -> None:
    """Record/refresh a session's last-activity + maintain the oto↔mcp map."""
    if not mcp_sid:
        return
    s = _sessions.get(mcp_sid)
    if s is None:
        s = {"last": time.time(), "oto": oto}
        _sessions[mcp_sid] = s
        log.info("session open mcp=%s oto=%s (live=%d)",
                 mcp_sid[:8], (oto[:8] or "-"), len(_sessions))
    s["last"] = time.time()
    if oto:
        s["oto"] = oto
        _oto_to_mcp[oto] = mcp_sid


def _forget(mcp_sid: str) -> None:
    s = _sessions.pop(mcp_sid, None)
    if s and s.get("oto"):
        oto = s["oto"]
        # Only clear the oto→mcp map if it still points at THIS session. A
        # re-init reuses the oto with a fresh mcp-session-id and remaps it; when
        # the old session is later GC'd/DELETEd we must not clobber the newer
        # mapping (that would orphan the live session's active-close → a leak).
        if _oto_to_mcp.get(oto) == mcp_sid:
            _oto_to_mcp.pop(oto, None)


async def _delete_upstream(mcp_sid: str) -> int:
    """Terminate an upstream MCP session → releases its server-side state."""
    from aiohttp import ClientTimeout
    assert _client is not None
    try:
        async with _client.delete(
            f"{UPSTREAM}/mcp", headers={"mcp-session-id": mcp_sid},
            timeout=ClientTimeout(total=30),
        ) as r:
            await r.read()
            return r.status
    except Exception as e:
        log.warning("delete mcp=%s failed: %s", mcp_sid[:8], e)
        return 0


def _sweep_screenshots() -> None:
    """Bound the SHARED /screenshots source. Delete a file if it's older than
    SCREENSHOTS_MAX_AGE_S (an orphan the proxy never relocated), OR — under a
    burst — if it's beyond the newest SCREENSHOTS_MAX_COUNT *and* older than
    SCREENSHOTS_GRACE_S (so a brand-new, still-relocating shot is never reaped)."""
    try:
        names = os.listdir(SCREENSHOTS_DIR)
    except OSError:
        return
    entries = []
    for name in names:
        p = os.path.join(SCREENSHOTS_DIR, name)
        try:
            if os.path.isfile(p):
                entries.append((p, os.path.getmtime(p)))
        except OSError:
            continue
    now = time.time()
    entries.sort(key=lambda e: e[1], reverse=True)  # newest first
    removed = 0
    for rank, (p, mtime) in enumerate(entries):
        age = now - mtime
        if age > SCREENSHOTS_MAX_AGE_S or (
            rank >= SCREENSHOTS_MAX_COUNT and age > SCREENSHOTS_GRACE_S
        ):
            with contextlib.suppress(OSError):
                os.remove(p)
                removed += 1
    if removed:
        log.info("screenshots sweep: removed %d orphan(s)", removed)


async def _gc_loop() -> None:
    while True:
        await asyncio.sleep(GC_INTERVAL_S)
        now = time.time()
        stale = [sid for sid, s in list(_sessions.items())
                 if now - s["last"] > SESSION_IDLE_S]
        for sid in stale:
            idle = now - _sessions[sid]["last"]
            log.info("idle-GC mcp=%s (idle %.0fs)", sid[:8], idle)
            await _delete_upstream(sid)
            _forget(sid)
        _sweep_screenshots()


async def close_session_handler(request):
    """Control endpoint — the OtoDock proxy calls this at agent-session close."""
    from aiohttp import web
    try:
        body = await request.json()
    except Exception:
        body = {}
    oto = (body.get("session_id") or "").strip()
    if not oto:
        return web.json_response({"error": "session_id required"}, status=400)
    mcp_sid = _oto_to_mcp.get(oto)
    if not mcp_sid:
        return web.json_response({"closed": False, "reason": "no live session"})
    status = await _delete_upstream(mcp_sid)
    _forget(mcp_sid)
    log.info("active close oto=%s mcp=%s status=%s", oto[:8], mcp_sid[:8], status)
    return web.json_response({"closed": True, "status": status})


async def proxy_handler(request):
    """Stream everything through to the upstream MCP server (transparent)."""
    from aiohttp import ClientTimeout, web
    assert _client is not None

    path = request.rel_url.raw_path
    qs = request.rel_url.query_string
    url = f"{UPSTREAM}{path}" + (f"?{qs}" if qs else "")

    # The proxy injects ?session_id=<oto> on every request; capture it.
    oto = request.rel_url.query.get("session_id", "")
    req_sid = request.headers.get("mcp-session-id", "")
    if req_sid:
        _touch(req_sid, oto)

    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _HOP_BY_HOP}
    body = await request.read()

    try:
        upstream = await _client.request(
            request.method, url, headers=fwd_headers, data=body or None,
            # A streaming GET (server→client SSE) must never time out; a
            # request/response call (POST/DELETE) is bounded so a wedged tool call
            # surfaces an error instead of hanging the agent forever.
            timeout=ClientTimeout(
                total=None, sock_connect=10,
                sock_read=None if request.method == "GET" else PER_CALL_TIMEOUT_S,
            ),
        )
    except Exception as e:
        # Exception text can embed upstream URLs/headers — keep it in the
        # sidecar log; the client gets the exception class for triage.
        log.warning("upstream %s %s: %s", request.method, path, e)
        return web.Response(
            status=502,
            text=f"sidecar upstream error: {type(e).__name__} (see sidecar log)",
        )

    # Register the session id the server assigns on initialize.
    resp_sid = upstream.headers.get("mcp-session-id", "")
    if resp_sid:
        _touch(resp_sid, oto)

    resp = web.StreamResponse(
        status=upstream.status,
        headers={k: v for k, v in upstream.headers.items()
                 if k.lower() not in _HOP_BY_HOP},
    )
    await resp.prepare(request)
    is_sse = "text/event-stream" in upstream.headers.get("content-type", "").lower()
    try:
        if is_sse:
            # Long-lived SSE channel: forward each event as it arrives, and keep
            # the client's read alive across idle gaps with periodic keepalive
            # comments. Race the read against a timer WITHOUT cancelling it (keep
            # the pending read across keepalives so aiohttp's reader isn't
            # disturbed mid-read).
            ait = upstream.content.iter_any().__aiter__()
            read_task: asyncio.Task | None = None
            try:
                while True:
                    if read_task is None:
                        read_task = asyncio.ensure_future(ait.__anext__())
                    done, _ = await asyncio.wait({read_task}, timeout=SSE_KEEPALIVE_S)
                    if read_task not in done:
                        await resp.write(_KEEPALIVE)  # idle → keepalive; read stays pending
                        continue
                    try:
                        chunk = read_task.result()
                    except StopAsyncIteration:
                        read_task = None
                        break
                    read_task = None
                    await resp.write(chunk)
            finally:
                if read_task is not None and not read_task.done():
                    read_task.cancel()
        else:
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
    except Exception as e:
        log.warning("stream %s: %s", path, e)
    finally:
        upstream.release()
    await resp.write_eof()

    # A client-initiated DELETE terminated the session — forget it locally too.
    if request.method == "DELETE" and req_sid:
        _forget(req_sid)
    return resp


def make_app():
    from aiohttp import web
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_post("/internal/close-session", close_session_handler)
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    return app


async def main() -> None:
    global _client
    from aiohttp import ClientSession, TCPConnector, web
    from aiohttp.web_log import AccessLogger

    class _NoLoopbackAccessLogger(AccessLogger):
        """Skip access-log lines for in-container (127.0.0.1) requests — the
        healthprobe + its keepalive daemon (one ping answer every 3s ≈ 29k
        lines/day of pure noise). Real traffic arrives via the container
        network (the proxy's forwarded requests), never loopback, and stays
        logged; the sidecar's own session open/close lifecycle lines are
        unaffected."""
        def log(self, request, response, time):
            if request is not None and request.remote == "127.0.0.1":
                return
            super().log(request, response, time)

    _client = ClientSession(connector=TCPConnector(limit=0))  # no pool cap
    app = make_app()
    runner = web.AppRunner(app, access_log_class=_NoLoopbackAccessLogger)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", LISTEN_PORT).start()
    log.info("session sidecar on :%d → %s (idle-GC %ds)",
             LISTEN_PORT, UPSTREAM, SESSION_IDLE_S)
    asyncio.create_task(_gc_loop())
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
