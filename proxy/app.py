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

Client adapters (adapters/): handle client-specific behavior (prompt context,
file display, task result delivery) for phone, dashboard, etc.

Unified event format across layers:
  {"type": "<event>", "data": {...}}
  Types: session, text, tool_start, tool_end, done, error
"""

import logging
import logging.handlers

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from startup import lifespan
from middleware import register_middlewares

# --- Logging ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        # Self-rotating: 20 MB x (1 live + 5 backups) = 120 MB hard cap.
        # No external logrotate dependency (proxy.log grew unbounded before).
        logging.handlers.RotatingFileHandler(
            str(config.BASE_DIR / "proxy.log"),
            maxBytes=20 * 1024 * 1024, backupCount=5,
        ),
    ],
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
from api.notifications import notifications as notifications_router
from api.mcp import credentials as credentials_router
from api.auth import oauth as oauth_router
from api.mcp import mcps as mcps_router
from api.mcp import community as community_router
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
from api.audio import audio as audio_router
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
app.include_router(audio_router.router)
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

app.add_api_websocket_route("/ws/phone", ws_phone_handler)
app.add_api_websocket_route("/ws/dashboard", ws_dashboard_handler)
app.add_api_websocket_route("/ws/phone-management", ws_phone_management_handler)
app.add_api_websocket_route("/v1/satellite", ws_satellite_handler)
app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
app.add_api_websocket_route("/ws/audio/tts", ws_audio_tts_handler)


# --- Dashboard static files ---

if config.DASHBOARD_ENABLED and config.DASHBOARD_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(config.DASHBOARD_DIST / "assets")),
        name="dashboard-assets",
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
async def spa_catchall(path: str):
    """Serve the React SPA for client-side routing on the subdomain."""
    if path.startswith(("v1/", "auth/", "ws/", "api/", "assets/", "wopi/", "collabora/")):
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
            # assets, no credentials: '*' is correct.
            return FileResponse(str(safe), headers={"Access-Control-Allow-Origin": "*"})
        return FileResponse(str(safe))
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
    from storage import agent_store as _as
    try:
        logger.info(f"Agents: {', '.join(_as.get_agent_slugs())}")
    except Exception as e:
        # Fresh DB: the schema (init_schema / run_migrations) is
        # created by the lifespan startup AFTER this banner runs. Don't crash
        # the boot just to log agent slugs — they exist once the app serves.
        logger.info(f"Agents: (schema not yet initialized — {type(e).__name__})")
    logger.info(f"Working dir: {config.AGENTS_DIR}")
    logger.info("MCP configs: per-agent (in agents/<name>/mcp-config.json)")
    uvicorn.run(
        app, host=config.HOST, port=config.PORT, log_level="info",
        timeout_keep_alive=2,
        access_log=True,
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
