"""Shared utilities for the file-tools MCP server.

Config, path mapping, LibreOffice lock, preview push helpers.
"""

import asyncio
import base64
import contextvars
import json
import logging
import os
import unicodedata
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL", "")
# `/agents` is the canonical mount point inside file-tools containers (declared
# in docker-compose.yml). Hardcoded — no longer a config knob; the env var
# `MOUNT_AGENTS_DIR` is gone in v2.
MOUNT_AGENTS_DIR = "/agents"
MCP_PORT = int(os.environ.get("MCP_PORT", "8932"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("file-tools")

# ---------------------------------------------------------------------------
# Per-request session binding (session_id + auth) via contextvars
# ---------------------------------------------------------------------------
#
# file-tools is a SHARED container serving every session of the install, so it
# can't hold a session-scoped credential in its env the way a per-session stdio
# MCP does. Instead the proxy injects, per session, a `?session_id=` URL param
# AND an `Authorization: Bearer <session-JWT>` header (see the platform's
# build_session_mcp_config + per-layer swap of the OTO_SESSION_JWT sentinel).
# We bind BOTH per request via contextvars.
#
# Why contextvars (not a module global): the streamable-HTTP transport runs in
# STATELESS mode (server.py: StreamableHTTPSessionManager(stateless=True)), so
# each request gets its own task group spawned AFTER the ASGI handler sets these
# — the values propagate to the tool handler and never bleed across concurrent
# sessions. A global would race two sessions onto one value; a ContextVar
# isolates per request and, crucially, fails CLOSED on any propagation gap
# (empty default → a clean "not session-bound" error, never another session).
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "file_tools_session_id", default=""
)
# Full `Authorization` header value the client sent (e.g. "Bearer eyJ..."),
# forwarded verbatim on every proxy callback. Empty when none was sent.
_auth_header_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "file_tools_auth", default=""
)


def set_request_context(session_id: str, auth_header: str) -> None:
    """Bind the in-flight request's session_id + Authorization header.

    Called at the transport boundary (server.py handle_sse / mcp_asgi_app)
    BEFORE the MCP SDK dispatches the tool handler.
    """
    _session_id_var.set(session_id or "")
    _auth_header_var.set(auth_header or "")


def _current_session() -> tuple[str, str]:
    """``(session_id, authorization_header)`` for the in-flight request."""
    return _session_id_var.get(), _auth_header_var.get()

# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------
#
# Inbound (LLM → file-tools): paths arrive as sandbox-virtual or agents-relative
# strings. We always ask the proxy's /v1/hooks/resolve-path to translate them
# to a canonical agents-relative path, then prepend `/agents/` for the mounted
# container view. The old direct host→container translation (HOST_AGENTS_DIR
# replacement) is gone — it broke remote-satellite sessions where the host path
# doesn't exist platform-side.
#
# Outbound (file-tools → proxy hooks): we post agents-relative paths to
# /v1/hooks/file*, which the hooks then translate per-target (local sandbox,
# remote satellite, plain host). See `_to_agents_relative` below.

# For satellite-homed sessions the resolve/file/file-written hooks perform a
# FULL synchronous file transfer over the satellite's WebSocket (lazy pull in,
# push-back out) before replying — over a WAN/tunnel link a few MB takes tens
# of seconds. The read budget must cover that transfer (the proxy's own pull
# budget is 180 s); connect stays short because the proxy itself is always
# platform-local to this container.
HOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=150.0, write=30.0, pool=5.0)


def _to_agents_relative(container_path: str) -> str:
    """Strip the `/agents/` mount prefix to produce an agents-relative path
    suitable for posting to /v1/hooks/file, /v1/hooks/document-preview, and
    /v1/hooks/file-written.

        /agents/personal-assistant/users/<user>/workspace/foo.docx
            → personal-assistant/users/<user>/workspace/foo.docx
    """
    if container_path.startswith(MOUNT_AGENTS_DIR + "/"):
        return container_path[len(MOUNT_AGENTS_DIR) + 1:]
    if container_path == MOUNT_AGENTS_DIR:
        return ""
    return container_path  # already agents-relative or out-of-tree


def _resolve_via_proxy(path: str, writing: bool = False) -> tuple[str | None, str]:
    """Ask the proxy to translate a path to an agents-relative path.

    Returns ``(agents_relative, "")`` on success (the agents-relative path is
    usable directly as a container path under MOUNT_AGENTS_DIR). On failure
    returns ``(None, reason)`` where ``reason`` carries the proxy's REAL
    verdict — a 403 policy reject (e.g. "outside the OS user's home directory;
    enable full filesystem access") or a 404 not-reachable — so the caller can
    surface it instead of a generic "within the agents directory" error that
    masked every cause (Issue C).

    ``writing`` marks a WRITE target (output/save path): the proxy then
    tolerates a missing file — on remote sessions a not-yet-existing output
    resolves to the platform creation path instead of failing the lazy pull.
    """
    session_id, auth = _current_session()
    logger.info(f"_resolve_via_proxy: path={path}, session_id={session_id[:12] if session_id else '(empty)'}, PROXY_URL={PROXY_URL}")
    if not session_id or not PROXY_URL or not auth:
        logger.warning(f"_resolve_via_proxy: skipping — session_id={'empty' if not session_id else 'set'}, PROXY_URL={'empty' if not PROXY_URL else 'set'}, auth={'empty' if not auth else 'set'}")
        return None, "file-tools is not session-bound (missing session_id/PROXY_URL/auth)"
    try:
        resp = httpx.post(
            f"{PROXY_URL}/v1/hooks/resolve-path",
            json={"session_id": session_id, "path": path, "writing": writing},
            headers={"Authorization": auth},
            timeout=HOOK_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            agents_rel = data.get("agents_relative", "")
            if agents_rel:
                return agents_rel, ""
            return None, (
                "proxy resolved the path but returned no agents-relative "
                "mapping (it is outside the synced agent tree)"
            )
        # Surface the proxy's real reason (403 policy reject / 404 not reachable).
        detail = ""
        try:
            detail = str(resp.json().get("detail", "")).strip()
        except Exception:
            detail = (resp.text or "")[:200].strip()
        return None, f"proxy resolve-path {resp.status_code}: {detail or '(no detail)'}"
    except Exception as e:
        logger.debug(f"resolve-path failed for '{path}': {e}")
        return None, f"resolve-path request failed: {e}"


def _unicode_match_on_disk(path: str) -> str:
    """If ``path`` doesn't exist verbatim but a Unicode-normalized variant
    of its basename exists in the parent dir, return the matching on-disk
    path.

    Linux filesystems do byte-exact lookups, but LLM/JSON transports often
    normalize Unicode to NFC when echoing tool-result paths back, while
    files written by Google Drive / macOS / Slack / etc. may use NFD
    (decomposed form like ``ι`` + combining ``U+0301`` instead of precomposed
    ``ί``). Without this fallback, every non-ASCII filename round-tripped
    through a tool result becomes unreadable.

    Falls through with the original path when:
      - the path exists verbatim (fast path — no listdir)
      - the parent dir is missing
      - no normalized match is found (caller's open() raises a clear error)
    """
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return path
    basename = os.path.basename(path)
    if not basename:
        return path
    target_nfc = unicodedata.normalize("NFC", basename)
    try:
        for entry in os.listdir(parent):
            if unicodedata.normalize("NFC", entry) == target_nfc:
                return os.path.join(parent, entry)
    except OSError:
        pass
    return path


def _resolve_path(path: str, writing: bool = False) -> str:
    """Resolve a tool-input path to a container-local path, validate it.

    Always uses the proxy's resolve-path API: the LLM's path string may be
    sandbox-virtual (`/users/alice/workspace/foo`), agents-relative
    (`personal-assistant/users/alice/...`), or already container-absolute
    (`/agents/...`) — the proxy normalizes them all to agents-relative.

    Pass ``writing=True`` for WRITE targets (output/save paths) so a
    not-yet-existing file resolves to its creation path on remote sessions
    instead of failing the lazy pull. Read-modify-write targets (e.g.
    ``write_docx`` on an existing doc) also use ``writing=True`` — the proxy
    still pulls the current satellite bytes first when the file exists.

    After prefix translation, falls back to a Unicode-normalized lookup in
    the parent dir if the exact path doesn't exist on disk (see
    ``_unicode_match_on_disk``).
    """
    # Already container-absolute? Just validate it sits under the mount.
    if path.startswith(MOUNT_AGENTS_DIR + "/") or path == MOUNT_AGENTS_DIR:
        resolved = str(Path(path).resolve())
        if resolved.startswith(MOUNT_AGENTS_DIR):
            return _unicode_match_on_disk(resolved)

    # Otherwise ask the proxy to translate.
    agents_rel, reason = _resolve_via_proxy(path, writing=writing)
    if agents_rel:
        cp = MOUNT_AGENTS_DIR + ("/" + agents_rel.lstrip("/"))
        resolved = str(Path(cp).resolve())
        if resolved.startswith(MOUNT_AGENTS_DIR):
            return _unicode_match_on_disk(resolved)

    # Surface the proxy's real verdict (policy reject / not reachable / out of
    # tree) instead of a generic message that masked the actual cause.
    raise ValueError(
        f"Cannot open '{path}': {reason}"
        if reason else
        f"Path could not be resolved: {path}"
    )


def _op_type(op: dict) -> str:
    """Extract operation type — LLMs may use 'type', 'op', 'operation', or 'action'."""
    return op.get("type") or op.get("op") or op.get("operation") or op.get("action") or ""


def _normalize_operations(ops) -> tuple[list[dict], int]:
    """Normalize an operations argument to a list of dicts.

    Some LLMs double-encode array parameters as JSON strings. Accept any of:
    - list of dicts (correct shape)
    - list of JSON-encoded strings: ["{\"type\":\"resize\"}", ...]
    - JSON-encoded string of a list: "[{\"type\":\"resize\"}, ...]"
    - JSON-encoded string of a single op: "{\"type\":\"resize\"}"
    - single dict (wrap in list)

    Returns (operations, dropped): malformed items are dropped so other
    operations still run, but the count is reported — a caller that silently
    swallows them leaves the model believing everything was applied.
    """
    if ops is None:
        return [], 0
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except (json.JSONDecodeError, ValueError):
            return [], 1  # the whole blob was unparseable
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list):
        return [], 1
    normalized: list[dict] = []
    dropped = 0
    for op in ops:
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except (json.JSONDecodeError, ValueError):
                dropped += 1
                continue
        if isinstance(op, dict):
            normalized.append(op)
        else:
            dropped += 1
    return normalized, dropped


def _dropped_note(dropped: int) -> str:
    """Result-message suffix reporting operations lost in normalization."""
    if not dropped:
        return ""
    return (
        f"\nWARNING: {dropped} malformed operation item(s) could not be parsed "
        "and were NOT applied (expected objects like {\"type\": \"...\", ...})."
    )


# ---------------------------------------------------------------------------
# LibreOffice lock (serialize concurrent headless calls)
# ---------------------------------------------------------------------------

_libreoffice_lock = asyncio.Lock()


async def _libreoffice_convert(
    input_path: str, output_format: str, output_dir: str | None = None
) -> str:
    """Convert a file with LibreOffice headless. Returns output path."""
    if output_dir is None:
        output_dir = str(Path(input_path).parent)
    async with _libreoffice_lock:
        proc = await asyncio.create_subprocess_exec(
            "libreoffice", "--headless", "--norestore", "--convert-to",
            output_format, "--outdir", output_dir, input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("LibreOffice conversion timed out (120s)")
        if proc.returncode != 0:
            raise RuntimeError(f"LibreOffice error: {stderr.decode()[:500]}")
    stem = Path(input_path).stem
    out = Path(output_dir) / f"{stem}.{output_format}"
    if not out.exists():
        raise RuntimeError(f"Conversion produced no output file at {out}")
    return str(out)


# ---------------------------------------------------------------------------
# Preview push helpers
# ---------------------------------------------------------------------------


async def _notify_file_written(file_path: str) -> bool:
    """Tell the proxy that we just finished writing a file.

    For remote agent sessions, the proxy uses this signal to push the file
    from its platform-side cache back to the satellite so the agent CLI and
    downstream MCPs on the satellite see the updated content. No-op for
    local sessions (proxy returns `local: true`). Fire-and-forget — the
    tool's success is independent of sync success.
    """
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return False
    agents_rel = _to_agents_relative(file_path)
    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/file-written",
                json={"session_id": session_id, "path": agents_rel},
                headers={"Authorization": auth},
            )
            if resp.status_code == 200:
                return bool(resp.json().get("ok"))
    except Exception as exc:
        logger.warning(f"file-written notify failed (non-fatal): {exc}")
    return False


async def _push_preview(file_path: str, filename: str | None = None):
    """Push a document preview event to the dashboard via proxy hook.

    Also notifies the proxy that the file was written so remote sessions
    can sync the new bytes back to the satellite (no-op for local).
    """
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return
    # Flush any platform-cache write back to the remote satellite before
    # the preview loads — otherwise the dashboard's download link could
    # race with the sync and serve stale bytes.
    await _notify_file_written(file_path)
    agents_rel = _to_agents_relative(file_path)
    fname = filename or Path(file_path).name
    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
            await client.post(
                f"{PROXY_URL}/v1/hooks/document-preview",
                json={
                    "session_id": session_id,
                    "file_path": agents_rel,
                    "filename": fname,
                },
                headers={"Authorization": auth},
            )
    except Exception as exc:
        logger.warning(f"Preview push failed (non-fatal): {exc}")


async def _push_image_preview(
    image_bytes: bytes, mime: str, caption: str = ""
):
    """Push an inline image preview to the dashboard."""
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return
    b64 = base64.b64encode(image_bytes).decode()
    # Posts a 1-item gallery — the unified /v1/hooks/images endpoint renders
    # single images identically to the old /v1/hooks/image flow.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{PROXY_URL}/v1/hooks/images",
                json={
                    "session_id": session_id,
                    "images": [{
                        "image_data": b64,
                        "mime_type": mime,
                        "caption": caption,
                    }],
                },
                headers={"Authorization": auth},
            )
    except Exception as exc:
        logger.warning(f"Image preview push failed (non-fatal): {exc}")
