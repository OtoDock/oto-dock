"""OtoDock Proxy — multi-agent LLM gateway with per-agent routing.

Execution layers (core/layers/ + core/remote/):
  - Claude Code CLI (core/layers/cli/): persistent `claude -p` subprocesses —
    session resumption, image uploads, permission hooks, AskUserQuestion.
  - Codex CLI (core/layers/codex/): `codex` app-server sessions (TOML MCP
    config, rollout files).
  - Direct LLM (core/layers/direct/): calls provider APIs directly — MCP tool
    execution, prompt caching, session state.
  - Remote (core/remote/): brokers a session onto a paired satellite machine
    over the satellite WS.

WebSocket routes:
  - /ws/dashboard          dashboard chat / streaming / notifications
  - /ws/phone              low-latency phone-call turns
  - /ws/phone-management   config push to the phone daemon
  - /v1/satellite          paired remote machines
  - /ws/audio/stt, /ws/audio/tts   chat-audio speech sessions
  - /ws/duplex, /ws/duplex-engine/{id}   full-duplex chat voice bridge
  - /v1/twilio/media/{server_id}   Twilio media stream, bridged to the phone daemon

Client adapters (adapters/): handle client-specific behavior (prompt context,
file display, task result delivery) for phone, dashboard, etc.

Unified event format across layers:
  {"type": "<event>", "data": {...}}
  Types: session, text, tool_start, tool_end, done, error
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import config
from core import log_queue
from startup import lifespan
from middleware import register_middlewares
from static_assets import PrecompressedStaticFiles, is_hashed_asset, negotiated_file_response

# --- Logging ---

# stderr + a self-rotating file: 20 MB x (1 live + 5 backups) = 120 MB hard
# cap, no external logrotate dependency (proxy.log grew unbounded before).
# Every write happens on log_queue's writer thread — nothing may write a
# log line from the event loop (see that module).
log_queue.configure(
    log_path=config.BASE_DIR / "proxy.log",
    max_bytes=20 * 1024 * 1024, backup_count=5,
)
logger = logging.getLogger("claude-proxy")


# --- App ---

app = FastAPI(
    title="OtoDock Proxy",
    version=config.PINNED_OTODOCK_VERSION or "0.0.0",
    lifespan=lifespan,
)

register_middlewares(app)


# --- Include routers ---

from api.auth import auth as auth_router
from api.sessions import sessions as sessions_router
from api.hooks import hooks as hooks_router
from api.tasks import tasks as tasks_router
from api.tasks import delegation as delegation_router
from api.tasks import continuations as continuations_router
from api.events import triggers as triggers_router
from api.agents import agents as agents_router
from api.agents import chats as chats_router
from api.departments import departments as departments_router
from api.notifications import notifications as notifications_router
from api.mcp import credentials as credentials_router
from api.auth import oauth as oauth_router
from api.mcp import mcps as mcps_router
from api.mcp import community as community_router
from api.mcp import icons as mcp_icons_router
from api.mcp import local_templates as local_templates_router
from api.media import uploads as uploads_router
from api.media import images as images_router
from api.media import media as media_router
from api.media import ui as ui_router
from api.apps import apps as apps_router
from api.media import wopi as wopi_router
from api.billing import usage as usage_router
from api.admin import admin_storage as admin_storage_router
from api.meetings import meetings as meetings_router
from api.admin import execution_layers as execution_layers_router
from api.auth import claude_oauth as claude_oauth_router
from api.auth import openai_oauth as openai_oauth_router
from api.auth import setup as setup_router
from api.phone import phone as phone_router
from api.phone import phone_relay as phone_relay_router
from api.phone import phone_usage as phone_usage_router
from api.phone import twilio_relay as twilio_relay_router
from api.audio import audio as audio_router
from api.duplex import duplex as duplex_router
from api.admin import title_generation as title_generation_router
from api.internal import internal as internal_router
from api.agent_data import memory as memory_router
from api.agent_data import git_history as git_history_router
from api.remote import remote_machines as remote_machines_router
from api.media import collabora_proxy as collabora_proxy_router
from api.auth import agent_api_keys as agent_api_keys_router
from api.auth import user_api_keys as user_api_keys_router
from api.events import webhooks as webhooks_router
from api.events import subscriptions as subscriptions_router
from api.billing import billing as billing_router
from api.billing import account as account_router

app.include_router(auth_router.router)
app.include_router(setup_router.router)
app.include_router(sessions_router.router)
app.include_router(hooks_router.router)
app.include_router(tasks_router.router)
app.include_router(delegation_router.router)
app.include_router(continuations_router.router)
app.include_router(triggers_router.router)
app.include_router(webhooks_router.router)
app.include_router(subscriptions_router.router)
app.include_router(agent_api_keys_router.router)
app.include_router(user_api_keys_router.router)
app.include_router(billing_router.router)
app.include_router(account_router.router)
app.include_router(agents_router.router)
app.include_router(chats_router.router)
app.include_router(departments_router.router)
app.include_router(notifications_router.router)
app.include_router(credentials_router.router)
# claude_oauth and openai_oauth use the same `/v1/oauth/{provider}/*` prefix
# as the generic MCP OAuth router; FastAPI routing is first-match-wins, so
# they MUST register before `oauth_router` to keep `/v1/oauth/claude/*`
# and `/v1/oauth/openai/*` from being shadowed.
app.include_router(claude_oauth_router.router)
app.include_router(openai_oauth_router.router)
app.include_router(oauth_router.router)
app.include_router(mcps_router.router)
app.include_router(community_router.router)
app.include_router(mcp_icons_router.router)
app.include_router(local_templates_router.router)
app.include_router(execution_layers_router.router)
app.include_router(uploads_router.router)
app.include_router(images_router.router)
app.include_router(media_router.router)
app.include_router(ui_router.router)
app.include_router(apps_router.router)
app.include_router(wopi_router.router)
app.include_router(usage_router.router)
app.include_router(admin_storage_router.router)
app.include_router(meetings_router.router)
app.include_router(phone_router.router)
app.include_router(phone_relay_router.router)
app.include_router(phone_usage_router.router)
app.include_router(twilio_relay_router.router)
app.include_router(audio_router.router)
app.include_router(duplex_router.router)
app.include_router(title_generation_router.router)
app.include_router(internal_router.router)
app.include_router(memory_router.router)
app.include_router(git_history_router.router)
app.include_router(remote_machines_router.router)
app.include_router(collabora_proxy_router.router)


# --- WebSocket endpoints ---

from ws.phone import ws_phone_handler
from ws.dashboard import ws_dashboard_handler
from ws.phone_management import ws_phone_management_handler
from ws.satellite import ws_satellite_handler
from ws.audio import ws_audio_stt_handler, ws_audio_tts_handler
from ws.duplex import ws_duplex_handler, ws_duplex_engine_handler

app.add_api_websocket_route("/ws/phone", ws_phone_handler)
app.add_api_websocket_route("/ws/dashboard", ws_dashboard_handler)
app.add_api_websocket_route("/ws/phone-management", ws_phone_management_handler)
app.add_api_websocket_route("/v1/satellite", ws_satellite_handler)
app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
app.add_api_websocket_route("/ws/audio/tts", ws_audio_tts_handler)
app.add_api_websocket_route("/ws/duplex", ws_duplex_handler)
app.add_api_websocket_route("/ws/duplex-engine/{duplex_id}", ws_duplex_engine_handler)
# Twilio's media WebSocket enters through the public proxy and is bridged to
# the phone daemon (see api/phone/twilio_relay.py — daemon-side signature auth).
app.add_api_websocket_route(
    "/v1/twilio/media/{server_id}", twilio_relay_router.ws_twilio_media_relay)


# --- Dashboard static files ---

# The build writes .br/.gz siblings next to every text asset and the mount
# serves whichever the client accepts (static_assets.py). Vite content-hashes
# the file names, so those are immutable for a year; the unhashed favicon
# copied from public/ stays revalidated.
if config.DASHBOARD_ENABLED and config.DASHBOARD_DIST.exists():
    app.mount(
        "/assets",
        PrecompressedStaticFiles(
            directory=str(config.DASHBOARD_DIST / "assets"), immutable=is_hashed_asset),
        name="dashboard-assets",
    )

# The wake-word wasm/model bundle (~18 MB the browser must never re-download):
# the version directory in the URL is the cache buster, so every file is
# immutable.
if config.DASHBOARD_ENABLED and config.KWS_ASSETS_DIR.exists():
    app.mount(
        "/kws-assets",
        PrecompressedStaticFiles(directory=str(config.KWS_ASSETS_DIR), immutable=True),
        name="kws-assets",
    )


def _safe_dashboard_file(path: str):
    """Resolve ``path`` under DASHBOARD_DIST, returning the file Path only if it
    stays inside DIST — else None (caller falls through to index.html / 404).

    uvicorn has already percent-decoded ``scope['path']`` (so ``%2e%2f`` →
    ``../``) and the ``{path:path}`` convertor does NOT normalize dot-segments,
    so a substring ``".." not in path`` check is insufficient. Resolve and
    confine instead.
    """
    dist = config.DASHBOARD_DIST.resolve()
    candidate = (dist / path).resolve()
    if candidate.is_relative_to(dist) and candidate.is_file():
        return candidate
    return None


@app.get("/dashboard/{path:path}", include_in_schema=False)
async def dashboard_legacy_redirect(path: str):
    """Redirect old /dashboard/* URLs to the new subdomain root."""
    if config.DASHBOARD_PUBLIC_URL:
        from starlette.responses import RedirectResponse
        return RedirectResponse(url=f"{config.DASHBOARD_PUBLIC_URL}/{path}")
    if not config.DASHBOARD_ENABLED or not config.DASHBOARD_DIST.exists():
        raise HTTPException(status_code=404, detail="Dashboard not enabled")
    safe = _safe_dashboard_file(path)
    if safe is not None:
        return FileResponse(str(safe))
    return FileResponse(str(config.DASHBOARD_DIST / "index.html"))


# SPA catch-all: serve index.html for all paths that don't match API/auth/ws routes.
# This MUST be registered last so it doesn't shadow other routes.
@app.get("/{path:path}", include_in_schema=False)
async def spa_catchall(path: str, request: Request):
    """Serve the React SPA for client-side routing on the subdomain."""
    # kws-assets/ included: a worker importScripts miss must 404 loudly, never
    # serve index.html-as-JS (same discipline as ui-kit below).
    if path.startswith(("v1/", "auth/", "ws/", "api/", "assets/", "wopi/",
                        "collabora/", "kws-assets/")):
        raise HTTPException(status_code=404, detail="Not found")
    if path == "health":
        return JSONResponse({"status": "ok", "service": "otodock"})
    if not config.DASHBOARD_ENABLED or not config.DASHBOARD_DIST.exists():
        raise HTTPException(status_code=404, detail="Dashboard not enabled")
    # Serve actual files from dist/ (favicon, APK downloads, etc.)
    safe = _safe_dashboard_file(path)
    if safe is not None:
        if path.startswith("ui-kit/"):
            # Artifact iframes run at an OPAQUE origin (Origin: null), and
            # @font-face fetches are CORS-mode requests (unlike script/style/
            # img) — without this header the kit woff2s are CORS-blocked and
            # artifacts silently fall back to system fonts. Public static
            # assets, no credentials: '*' is correct. The kit JS (echarts,
            # three, tailwind) has precompressed siblings like /assets; the
            # kit names are stable across releases, so no immutable caching.
            return negotiated_file_response(
                safe, safe.stat(), request.headers,
                extra_headers={"Access-Control-Allow-Origin": "*"})
        # Unhashed names that keep their URL across releases (the wake-word
        # worker, the manifest, the APKs): revalidate on every load (a 304
        # on the ETag), or a browser's heuristic freshness keeps running the
        # previous file after the page reloaded onto a new build.
        return FileResponse(str(safe), headers={"Cache-Control": "no-cache"})
    # /ui-kit/* are subresources of sandboxed artifact iframes (echarts, tokens
    # CSS, fonts) — a miss must 404 loudly, not serve index.html with 200
    # (a <script src> would silently load HTML-as-JS on a build/copy mistake).
    if path.startswith("ui-kit/"):
        raise HTTPException(status_code=404, detail="Not found")
    # index.html must never be cached — it references hashed JS/CSS bundles
    return FileResponse(
        str(config.DASHBOARD_DIST / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting OtoDock proxy on {config.HOST}:{config.PORT}")
    from storage.agents import agent_store as _as
    try:
        logger.info(f"Agents: {', '.join(_as.get_agent_slugs())}")
    except Exception as e:
        # Fresh DB: the schema (init_schema / run_migrations) is
        # created by the lifespan startup AFTER this banner runs. Don't crash
        # the boot just to log agent slugs — they exist once the app serves.
        logger.info(f"Agents: (schema not yet initialized — {type(e).__name__})")
    logger.info(f"Working dir: {config.AGENTS_DIR}")
    logger.info("MCP configs: per-agent (in agents/<name>/mcp-config.json)")
    # uvicorn re-raises the SIGTERM/SIGINT it served once its graceful
    # shutdown is done (the process exits by the signal, so atexit never
    # runs) — with this as the restored handler the log queue is drained
    # before the default disposition ends the process.
    import signal
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, log_queue.exit_signal_handler)
    uvicorn.run(
        app, host=config.HOST, port=config.PORT, log_level="info",
        timeout_keep_alive=2,
        access_log=True,
        # No uvicorn-owned handlers: its default config installs synchronous
        # stream handlers (one access line per request, written on the
        # loop). With none, the uvicorn loggers propagate to the root queue
        # handler and take the root format (timestamped, off the loop).
        log_config=None,
        # Use the modern sans-I/O websockets implementation, NOT uvicorn's
        # default legacy one. The legacy impl (websockets/legacy/protocol.py)
        # asserts in `_drain_helper` when two coroutines drain the socket at
        # once — e.g. the keepalive PING / auto-PONG colliding with a large
        # app send. Under a fresh satellite's full sync (heavy concurrent
        # writes) that reliably crashed the satellite WS (1011) mid-install;
        # it also caused sporadic dashboard drops. The sans-I/O impl serializes
        # writes correctly and is the maintained path (legacy is deprecated).
        # Requires uvicorn>=0.35; we pin 0.49.
        ws="websockets-sansio",
        ws_ping_interval=20,
        # Generous pong timeout (30s, not 10s) so a slow/busy satellite (e.g. a
        # laptop mid-spawn that briefly starves its event loop) isn't dropped with
        # "1011 keepalive ping timeout" on a transient stall. INTERVAL stays 20s
        # (< the 60s reverse-proxy read timeout) so sockets stay warm. Symmetric
        # with satellite/ws_client.py _WS_PING_TIMEOUT.
        ws_ping_timeout=30,
        # Force-close lingering connections (satellite / dashboard / phone
        # WebSockets) 10s after SIGTERM so `systemctl restart` doesn't hang in
        # uvicorn's graceful drain until systemd's 90s TimeoutStopSec SIGKILL.
        timeout_graceful_shutdown=10,
    )
