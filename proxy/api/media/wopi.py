"""WOPI (Web Application Open Platform Interface) endpoints for Collabora Online.

Collabora uses WOPI to fetch/save files from the proxy. The dashboard requests
WOPI URLs to embed Collabora iframes for live document preview and editing.

Security: Every WOPI call is authenticated via a short-lived JWT access_token
that is scoped to a specific file, user, agent, and permission level.
"""

import base64
import logging
import time
import urllib.parse
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

import config
from auth.providers import get_current_user, require_auth, UserContext

logger = logging.getLogger("claude-proxy")
router = APIRouter(tags=["wopi"])

# ---------------------------------------------------------------------------
# WOPI Token Management
# ---------------------------------------------------------------------------

_WOPI_TOKEN_TTL = 14400  # 4 hours

# file_path namespace for version-pinned preview snapshots
# (services/media/preview_snapshots.py): "<ns>/<chat_id>/<snapshot_id>".
# The leading dot keeps it disjoint from every agent slug, so a snapshot
# file_id can never collide with (or traverse into) an agent tree.
_SNAPSHOT_NS = ".preview-snapshots"


def encode_file_id(relative_path: str) -> str:
    """Base64url-encode a relative file path for use as WOPI file_id."""
    return base64.urlsafe_b64encode(relative_path.encode()).decode().rstrip("=")


def decode_file_id(file_id: str) -> str:
    """Decode base64url file_id back to a relative file path."""
    padded = file_id + "=" * (4 - len(file_id) % 4)
    return base64.urlsafe_b64decode(padded).decode()


def snapshot_rel_path(chat_id: str, snapshot_id: str) -> str:
    """The token file_path / file_id payload for a preview snapshot."""
    return f"{_SNAPSHOT_NS}/{chat_id}/{snapshot_id}"


def create_wopi_token(
    file_path: str,
    user_sub: str,
    user_name: str,
    permissions: str,
    agent: str,
    display_name: str = "",
) -> tuple[str, int]:
    """Create a JWT WOPI access token. Returns (token, expiry_ms).

    ``display_name`` overrides CheckFileInfo's BaseFileName — snapshot files
    are stored under opaque extension-less ids, and Collabora picks its
    renderer from the BaseFileName extension, so snapshot tokens must carry
    the original filename."""
    now = int(time.time())
    exp = now + _WOPI_TOKEN_TTL
    payload = {
        # Purpose discriminator: WOPI_SECRET defaults to JWT_SECRET, which
        # also signs session/audio/broker tokens — without this claim the
        # verifier below would accept ANY platform JWT whose claim shape
        # happens to fit (and vice versa).
        "purpose": "wopi",
        "file_path": file_path,
        "user_sub": user_sub,
        "user_name": user_name,
        "permissions": permissions,  # "view" or "edit"
        "agent": agent,
        "iat": now,
        "exp": exp,
    }
    if display_name:
        payload["display_name"] = display_name
    token = jwt.encode(payload, config.WOPI_SECRET, algorithm="HS256")
    return token, exp * 1000  # expiry in milliseconds for Collabora


def validate_wopi_token(token: str) -> dict | None:
    """Validate and decode a WOPI access token. Returns claims or None."""
    try:
        claims = jwt.decode(token, config.WOPI_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if claims.get("purpose") != "wopi":
        return None
    return claims


def _resolve_wopi_path(file_id: str, claims: dict) -> Path:
    """Resolve and validate a WOPI file path. Raises HTTPException on failure."""
    rel_path = decode_file_id(file_id)

    # Verify token's file_path matches the file_id
    if claims.get("file_path") != rel_path:
        raise HTTPException(status_code=403, detail="Token/file mismatch")

    # Preview-snapshot namespace: resolve under the proxy-private snapshot
    # cache. The store validates both id segments against a strict charset
    # (no separators, no dots), so the decoded path cannot traverse.
    if rel_path.startswith(_SNAPSHOT_NS + "/"):
        from services.media import preview_snapshots
        parts = rel_path.split("/")
        snap = (
            preview_snapshots.snapshot_path(parts[1], parts[2])
            if len(parts) == 3 else None
        )
        if snap is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return snap

    full_path = (config.AGENTS_DIR / rel_path).resolve()

    # Security: ensure path is within AGENTS_DIR (no traversal)
    agents_resolved = config.AGENTS_DIR.resolve()
    if not str(full_path).startswith(str(agents_resolved)):
        raise HTTPException(status_code=403, detail="Path traversal blocked")

    return full_path


def _validate_access(file_id: str, access_token: str) -> tuple[Path, dict]:
    """Common validation for all WOPI endpoints. Returns (file_path, claims)."""
    claims = validate_wopi_token(access_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired WOPI token")
    return _resolve_wopi_path(file_id, claims), claims


# ---------------------------------------------------------------------------
# WOPI Lock Management (in-memory, for conflict prevention)
# ---------------------------------------------------------------------------

_wopi_locks: dict[str, dict] = {}  # file_id → {"lock_id": str, "expires": float}


def _get_lock(file_id: str) -> str | None:
    """Get current lock for a file, or None if unlocked/expired."""
    info = _wopi_locks.get(file_id)
    if not info:
        return None
    if time.time() > info["expires"]:
        _wopi_locks.pop(file_id, None)
        return None
    return info["lock_id"]


def _set_lock(file_id: str, lock_id: str):
    _wopi_locks[file_id] = {"lock_id": lock_id, "expires": time.time() + _WOPI_TOKEN_TTL}


def _remove_lock(file_id: str, lock_id: str) -> bool:
    current = _get_lock(file_id)
    if current == lock_id:
        _wopi_locks.pop(file_id, None)
        return True
    return False


def _post_message_origin() -> str:
    """Origin Collabora targets when posting status (e.g. ``Doc_ModifiedStatus``)
    to the embedding dashboard frame — the WOPI ``PostMessageOrigin`` field.

    Returns the ``scheme://host[:port]`` of the dashboard's public URL so the
    dashboard can read the doc's modified state for the reload dirty-guard.
    Falls back to ``"*"`` only if ``DASHBOARD_PUBLIC_URL`` is unconfigured."""
    raw = (getattr(config, "DASHBOARD_PUBLIC_URL", "") or "").strip()
    if raw:
        try:
            p = urllib.parse.urlparse(raw)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}"
        except ValueError:
            pass
    return "*"


# ---------------------------------------------------------------------------
# WOPI Endpoints (called by Collabora)
# ---------------------------------------------------------------------------

@router.get("/wopi/files/{file_id}")
async def wopi_check_file_info(
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI CheckFileInfo — returns file metadata for Collabora."""
    file_path, claims = _validate_access(file_id, access_token)

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    can_write = claims.get("permissions") == "edit"

    return {
        # Snapshot tokens carry display_name (opaque on-disk id, no
        # extension) — Collabora picks its renderer from this extension.
        "BaseFileName": claims.get("display_name") or file_path.name,
        "Size": file_path.stat().st_size,
        "OwnerId": claims.get("user_sub", ""),
        "UserId": claims.get("user_sub", ""),
        "UserFriendlyName": claims.get("user_name", "User"),
        "UserCanWrite": can_write,
        "UserCanNotWriteRelative": True,
        "SupportsLocks": True,
        "SupportsUpdate": can_write,
        "LastModifiedTime": time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(file_path.stat().st_mtime)
        ),
        # Co-edit presence: show co-editors' names/cursors and their
        # join/leave messages so two users editing the same doc see each other.
        "HideUserList": "false",
        "DisableInactiveMessages": "false",
        "HidePrintOption": True,
        # Enable Collabora→host postMessage so the dashboard can read the doc's
        # modified state and only auto-reload on an external change when there
        # are no unsaved edits (dirty-guard).
        "PostMessageOrigin": _post_message_origin(),
    }


@router.get("/wopi/files/{file_id}/contents")
async def wopi_get_file(
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI GetFile — returns raw file bytes."""
    file_path, claims = _validate_access(file_id, access_token)

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return Response(
        content=file_path.read_bytes(),
        media_type="application/octet-stream",
        headers={"X-WOPI-ItemVersion": str(int(file_path.stat().st_mtime))},
    )


@router.post("/wopi/files/{file_id}/contents")
async def wopi_put_file(
    request: Request,
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI PutFile — saves file from Collabora (user editing)."""
    file_path, claims = _validate_access(file_id, access_token)

    if claims.get("permissions") != "edit":
        raise HTTPException(status_code=403, detail="Write permission required")
    # Snapshots are immutable history; tokens for them are minted view-only,
    # but refuse writes on the namespace itself as defence in depth.
    if (claims.get("file_path") or "").startswith(_SNAPSHOT_NS + "/"):
        raise HTTPException(status_code=403, detail="Snapshots are read-only")

    # Check lock (if locked by someone else, reject)
    lock_header = request.headers.get("X-WOPI-Lock", "")
    current_lock = _get_lock(file_id)
    if current_lock and lock_header != current_lock:
        return Response(
            status_code=409,
            headers={"X-WOPI-Lock": current_lock},
        )

    body = await request.body()

    # Persist + propagate. ``propagate_write`` does the authoritative
    # atomic write to the platform agent tree AND fans the saved bytes out to
    # every OTHER satellite running this agent — all under the global
    # per-(agent, rel_path) lock, so a concurrent satellite / file-tools write
    # can't interleave (disk and satellites converge on the same bytes).
    # Collabora already live-merged concurrent human editors, so ``body`` IS the
    # merged result → no conflict capture (see propagate_write). agent_slug +
    # agent-tree rel come from the token's AGENTS_DIR-relative file_path
    # ("<agent>/workspace/..." or "<agent>/users/{u}/...").
    rel = claims.get("file_path", "")
    agent_slug, _, tree_rel = rel.partition("/")
    if agent_slug and tree_rel:
        from services.remote import workspace_fanout
        from storage import database as _db
        _sub = claims.get("user_sub", "")
        _writer = _db.get_username_by_sub(_sub) if _sub else None
        await workspace_fanout.propagate_write(
            agent_slug, tree_rel, body, exclude_machine_id=None, writer=_writer,
        )
        # Tell OTHER users' dashboards the file changed so an open
        # preview / workspace tree refreshes. source="collabora": a peer with the
        # SAME doc open in Collabora is already live-merged (won't force-reload);
        # a non-Collabora view still refreshes. Exclude the saving user
        # (claims user_sub — "agent" for inline tokens → excludes nothing real).
        from services.notifications import notification_manager
        await notification_manager.broadcast_file_updated(
            agent_slug, tree_rel, source="collabora",
            exclude_user_sub=claims.get("user_sub", "") or "",
        )
    else:
        # Malformed / edge token path — fall back to a plain write (no fan-out).
        file_path.write_bytes(body)
    logger.info(
        "WOPI PutFile: %s (%d bytes) by %s",
        file_path.name, len(body), claims.get("user_name"),
    )

    return Response(status_code=200)


@router.post("/wopi/files/{file_id}")
async def wopi_lock_operations(
    request: Request,
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI Lock/Unlock/RefreshLock operations."""
    _file_path, claims = _validate_access(file_id, access_token)

    override = request.headers.get("X-WOPI-Override", "").upper()
    lock_id = request.headers.get("X-WOPI-Lock", "")
    old_lock = request.headers.get("X-WOPI-OldLock", "")

    # Mutating lock ops require edit capability — a view-only session must
    # not be able to place/steal a lock and 409 the real editor's saves.
    # GET_LOCK stays readable with any valid token.
    if override in ("LOCK", "UNLOCK", "REFRESH_LOCK") and claims.get("permissions") != "edit":
        raise HTTPException(status_code=403, detail="Write permission required")

    current = _get_lock(file_id)

    if override == "LOCK":
        if old_lock:
            # Unlock and relock
            if current and current != old_lock:
                return Response(status_code=409, headers={"X-WOPI-Lock": current or ""})
            _set_lock(file_id, lock_id)
        elif current:
            if current == lock_id:
                # RefreshLock
                _set_lock(file_id, lock_id)
            else:
                return Response(status_code=409, headers={"X-WOPI-Lock": current})
        else:
            _set_lock(file_id, lock_id)
        return Response(status_code=200)

    elif override == "UNLOCK":
        if not current or current != lock_id:
            return Response(status_code=409, headers={"X-WOPI-Lock": current or ""})
        _remove_lock(file_id, lock_id)
        return Response(status_code=200)

    elif override == "REFRESH_LOCK":
        if not current or current != lock_id:
            return Response(status_code=409, headers={"X-WOPI-Lock": current or ""})
        _set_lock(file_id, lock_id)
        return Response(status_code=200)

    elif override == "GET_LOCK":
        return Response(status_code=200, headers={"X-WOPI-Lock": current or ""})

    return Response(status_code=501)


# ---------------------------------------------------------------------------
# Dashboard endpoint: generate Collabora WOPI URL
# ---------------------------------------------------------------------------


def build_cool_url(file_id: str, token: str, token_ttl: int) -> str:
    """The Collabora iframe URL for a minted WOPI token (shared by the
    dashboard endpoints and the document-preview hook)."""
    wopi_src = urllib.parse.quote(
        f"{config.WOPI_BASE_URL.rstrip('/')}/wopi/files/{file_id}",
        safe="",
    )
    return (
        f"{config.COLLABORA_URL}/browser/dist/cool.html"
        f"?WOPISrc={wopi_src}"
        f"&access_token={token}"
        f"&access_token_ttl={token_ttl}"
        f"&closebutton=0&homebutton=0"
        f"&ui_defaults=UIMode%3Dcompact%3BTextSidebar%3Dfalse"
        f"%3BSpreadsheetSidebar%3Dfalse%3BPresentationSidebar%3Dfalse"
    )


class WopiUrlRequest(BaseModel):
    file_path: str  # relative to agent dir, e.g. "users/alice/workspace/report.docx" or "workspace/report.docx"
    agent: str
    edit: bool = False


@router.post("/v1/documents/wopi-url")
async def generate_wopi_url(
    req: WopiUrlRequest,
    user: UserContext = Depends(get_current_user),
):
    """Generate a Collabora iframe URL for the dashboard."""
    require_auth(user)

    if not config.COLLABORA_URL:
        # Fail loud: an empty COLLABORA_URL would emit a relative iframe src
        # (`/browser/dist/cool.html?...`), which the SPA catch-all happily
        # returns as the dashboard's index.html — the user sees the platform
        # home page inside the iframe with no error to explain it. Raising
        # 503 here makes the misconfig visible to operators.
        raise HTTPException(
            status_code=503,
            detail="Document preview is unavailable: COLLABORA_URL is not configured. Set it in config.env (e.g., https://prifiles.example.com) and restart the proxy.",
        )

    if not user.can_access_agent(req.agent):
        raise HTTPException(status_code=403, detail="No access to agent")

    # Resolve full path
    full_path = (config.AGENTS_DIR / req.agent / req.file_path).resolve()
    agent_root = (config.AGENTS_DIR / req.agent).resolve()
    workspace = (agent_root / "workspace").resolve()
    users_dir = (agent_root / "users").resolve()

    # Security: must be within agent workspace or users directory
    if not (str(full_path).startswith(str(workspace)) or str(full_path).startswith(str(users_dir))):
        raise HTTPException(status_code=403, detail="Path must be within agent workspace or users directory")

    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Read-scope gate: minting ANY token must be authorized like READING the
    # file — without this a user could mint a (view) token for another user's
    # document. Checked on the RESOLVED agent-relative path. A trusted no-user /
    # service caller (acting_sub None) gets full access, like the file API.
    if user.acting_sub is not None:
        from api.agents.agents import _check_file_role
        from storage import database as _db
        agent_rel = full_path.relative_to(agent_root).as_posix()
        _check_file_role(
            agent_rel, user.get_agent_role(req.agent), writing=False,
            username=_db.get_username_by_sub(user.sub) or "",
        )

    # Relative path from AGENTS_DIR (for file_id)
    rel_path = str(full_path.relative_to(config.AGENTS_DIR.resolve()))
    file_id = encode_file_id(rel_path)

    # Role-gate the edit decision SERVER-SIDE. The client `req.edit`
    # bool is a request, not authority — without this a viewer could mint an
    # edit token via the API. ``can_write_back`` is the same write matrix the
    # satellite write-back guard + simple file editor enforce; req.file_path is
    # already agent-tree-relative ("workspace/..." or "users/{u}/..."). API-key
    # callers (trusted master key) bypass, mirroring the file API's role check.
    if user.acting_sub is None:
        can_edit = req.edit
    else:
        from core.remote.file_sync import can_write_back
        from storage import database as _db
        _uname = _db.get_username_by_sub(user.sub) or ""
        # Authorize the RESOLVED agent-relative path (== req.file_path for a
        # well-formed request) so a '..'-laundered body can't flip the verdict.
        _rel = full_path.relative_to(agent_root).as_posix()
        can_edit = req.edit and can_write_back(
            _rel, user.get_agent_role(req.agent), _uname,
        )
    permissions = "edit" if can_edit else "view"
    token, token_ttl = create_wopi_token(
        rel_path, user.sub, user.name, permissions, req.agent
    )

    wopi_url = build_cool_url(file_id, token, token_ttl)

    return {"wopi_url": wopi_url, "file_id": file_id, "permissions": permissions}


@router.get("/v1/documents/snapshot-wopi-url")
async def generate_snapshot_wopi_url(
    chat_id: str = Query(...),
    snapshot_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Mint a VIEW-only Collabora URL for a version-pinned preview snapshot.

    Called by the dashboard at render/swap time — never persisted, so a
    "previous version" block always gets a live token. Access is gated on the
    requester's CHAT access (the snapshot belongs to the chat's preview
    history, not to any workspace path), and the snapshot must still be
    referenced by a non-dismissed preview event. 404 (pruned/unknown snapshot)
    tells the dashboard to degrade that block to a chip."""
    u = require_auth(user)

    if not config.COLLABORA_URL:
        raise HTTPException(
            status_code=503,
            detail="Document preview is unavailable: COLLABORA_URL is not configured.",
        )

    from storage import database as task_store
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    from api.agents.chats import can_access_chat
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")

    event = task_store.get_preview_event_by_snapshot(chat_id, snapshot_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    from services.media import preview_snapshots
    if preview_snapshots.snapshot_path(chat_id, snapshot_id) is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    rel_path = snapshot_rel_path(chat_id, snapshot_id)
    token, token_ttl = create_wopi_token(
        rel_path, u.sub, u.name, "view", chat.get("agent") or "",
        display_name=event.get("filename") or "document",
    )
    file_id = encode_file_id(rel_path)
    return {"wopi_url": build_cool_url(file_id, token, token_ttl)}
