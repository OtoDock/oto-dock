"""Shares (SHARING.md): who may hand an app or a chat to someone else, and
the surfaces a grantee uses.

Every route that creates or changes a share is HUMAN-ONLY (``require_human``):
a share is an exfiltration channel, so no session token or API key — the
credentials a prompt can wield — ever reaches it. Authority follows the
target: the owner of a personal app, editor or above on a shared app, and
never a Dock pin (a chat- or project-scoped app is reachable only with its
chat). Grantees are platform users; a grant is a union on top of the
default visibility and changes nothing when memberships change.
"""

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

import config
from api.apps.apps import _can_manage, _visible_row
from api.media.ui import request_origin
from auth import confirm, rate_limiter
from auth.password import MAX_PASSWORD_BYTES, HashBusy, hash_password_async
from auth.providers import (
    UserContext,
    get_current_user,
    require_admin,
    require_auth,
    require_human,
)
from services.apps import audience
from storage import database as task_store
from storage.sharing import share_store
from core.session import session_kind
from core.session.visibility import is_task_chat_owner

logger = logging.getLogger("claude-proxy.shares")
router = APIRouter()

MAX_EXPIRY_DAYS_HARD = 3650
SETTING_DIRECTORY = "user_directory_visible_to_members"
SETTING_MAX_EXPIRY = "sharing_max_expiry_days"
SETTING_EXTERNAL = "sharing_external_enabled"
SETTING_PUBLIC = "sharing_public_links_enabled"
EXTERNAL_DEFAULT_EXPIRY = "30d"
# Link passwords: eight characters from an alphabet without look-alikes.
_LINK_PW_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"


class ShareCreateRequest(BaseModel):
    target_kind: str
    target_id: str
    scope: str = "internal"
    # Internal: the grantee's sub (from the directory) or their exact
    # username or email.
    grantee: str = ""
    # ISO instant, a number of days as "30d", or "never"; empty = no
    # expiry (external links default to 30 days, so a link that must not
    # expire says "never"). An admin cap refuses "never".
    expires_in: str = ""
    # External: a link without a password (needs the platform switch), the
    # Buttons switch, an own password instead of a generated one.
    public: bool = False
    allow_actions: bool = False
    link_password: str = ""
    # Chats: keep the tool-call blocks in the snapshot.
    include_tools: bool = False
    # The confirm (``auth.confirm``): the account password, or the
    # token of a passkey confirm ceremony.
    password: str = ""
    confirm_token: str = ""


class SharePatchRequest(BaseModel):
    revoke: bool | None = None
    resume: bool | None = None
    expires_in: str | None = None
    # External links: the Buttons switch (turning it on confirms) and a new
    # link password (logs every viewer out).
    allow_actions: bool | None = None
    link_password: str | None = None
    password: str = ""
    confirm_token: str = ""


def _setting_on(key: str) -> bool:
    return task_store.get_platform_setting(key) != "0"


def _generate_link_password() -> str:
    return "".join(secrets.choice(_LINK_PW_CHARS) for _ in range(8))


def _directory_open(u: UserContext) -> bool:
    if u.is_admin:
        return True
    return task_store.get_platform_setting(SETTING_DIRECTORY) != "0"


def _max_expiry_days() -> int | None:
    raw = (task_store.get_platform_setting(SETTING_MAX_EXPIRY) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _parse_expiry(spec: str, cap_days: int | None) -> str | None:
    """``""`` → no expiry (unless a cap applies, then the cap); ``"never"``
    → no expiry, refused under a cap; ``"<n>d"`` → n days from now; an ISO
    instant → itself. Returns ISO text."""
    spec = (spec or "").strip()
    now = datetime.now(timezone.utc)
    if not spec:
        if cap_days is None:
            return None
        return (now + timedelta(days=cap_days)).isoformat()
    if spec == "never":
        if cap_days is not None:
            raise HTTPException(status_code=400,
                                detail=f"an admin capped share expiry at {cap_days} days")
        return None
    if spec.endswith("d") and spec[:-1].isdigit():
        days = int(spec[:-1])
        if not 1 <= days <= MAX_EXPIRY_DAYS_HARD:
            raise HTTPException(status_code=400, detail="expiry must be 1 to 3650 days")
        if cap_days is not None and days > cap_days:
            raise HTTPException(status_code=400,
                                detail=f"an admin capped share expiry at {cap_days} days")
        return (now + timedelta(days=days)).isoformat()
    try:
        when = datetime.fromisoformat(spec.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="expiry must be an ISO instant, '<n>d' or 'never'")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= now:
        raise HTTPException(status_code=400, detail="expiry must be in the future")
    if cap_days is not None and when > now + timedelta(days=cap_days):
        raise HTTPException(status_code=400,
                            detail=f"an admin capped share expiry at {cap_days} days")
    return when.astimezone(timezone.utc).isoformat()


def _app_share_target(u: UserContext, app_id: str) -> dict:
    """The app row the caller may share, or the same 404 as every app route;
    403 for a viewer, 400 for a Dock pin."""
    row = _visible_row(app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if task_store.app_is_scoped(row):
        raise HTTPException(status_code=400,
                            detail="A chat or project dashboard is reached through its chat "
                                   "and cannot be shared on its own")
    if not _can_manage(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to share this app")
    return row


def _chat_share_target(u: UserContext, chat_id: str) -> dict:
    """The chat the caller may share as a snapshot: one they may open, that
    is not a task run, owned by them or (a shared-only agent's chat) with
    editor rights; the same 404 as a missing chat otherwise."""
    from api.agents.chats import can_access_chat, can_share_chat
    chat = task_store.get_chat(chat_id)
    if not chat or not can_access_chat(u, chat):
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_share_chat(u, chat):
        if is_task_chat_owner(chat.get("user_sub")) or session_kind.of_chat(chat) is session_kind.TASK:
            raise HTTPException(status_code=400, detail="Task runs cannot be shared")
        raise HTTPException(status_code=403, detail="Not authorized to share this chat")
    return chat


def _share_target(u: UserContext, target_kind: str, target_id: str) -> dict:
    if target_kind == "app":
        return _app_share_target(u, target_id)
    if target_kind == "chat":
        return _chat_share_target(u, target_id)
    raise HTTPException(status_code=400, detail="target_kind must be 'app' or 'chat'")


def _may_still_share(u: UserContext, target_kind: str, target_id: str) -> None:
    """``_share_target`` for a change to an existing share. An unpinned
    app is hidden from every list, so its authority is judged on the row
    itself: its sharer still re-dates a suspended share and learns, on
    resume, that the app must be pinned again first."""
    if target_kind == "app":
        row = task_store.get_app(target_id)
        if row and row.get("hidden"):
            if not _can_manage(row, u):
                raise HTTPException(status_code=403, detail="Not authorized to share this app")
            return
    _share_target(u, target_kind, target_id)


def _snapshot_for(u: UserContext, share: dict, chat: dict, include_tools: bool) -> None:
    """Write the chat's snapshot for a fresh share; a snapshot past the cap
    undoes the share. Sync."""
    from services.sharing import chat_snapshot
    from storage.sharing import share_store as _ss
    username = task_store.get_username_by_sub(u.sub) or u.sub
    try:
        ref = chat_snapshot.build(chat, share["id"], username, include_tools=include_tools)
    except chat_snapshot.SnapshotTooLarge:
        _ss.revoke_share(share["id"])
        raise HTTPException(status_code=400,
                            detail="This chat's files exceed what one share may hold (50 MB)")
    _ss.set_snapshot_ref(share["id"], ref)
    share["snapshot_ref"] = ref


def _check_chat_share_cap(u: UserContext) -> None:
    from services.sharing import chat_snapshot
    n = share_store.count_live_shares_by(u.sub)
    if n >= chat_snapshot.MAX_CHAT_SHARES_PER_USER:
        raise HTTPException(status_code=400,
                            detail=f"You already have {n} live shares — revoke one first")


def _resolve_grantee(identifier: str) -> dict | None:
    ident = (identifier or "").strip()
    if not ident:
        return None
    user = task_store.get_user(ident)
    if user:
        return user
    sub = task_store.get_user_sub_by_username(ident)
    if sub:
        return task_store.get_user(sub)
    if "@" in ident:
        return task_store.get_user_by_email(ident)
    return None


def _shape(row: dict) -> dict:
    grantee = None
    if row.get("grantee_sub"):
        grantee = {
            "sub": row["grantee_sub"],
            "name": row.get("grantee_display_name") or row.get("grantee_name") or "",
            "username": row.get("grantee_username") or "",
        }
    return {
        "id": row["id"],
        "target_kind": row["target_kind"],
        "target_id": row.get("app_id") or row.get("chat_id") or "",
        "scope": row["scope"],
        "state": row.get("state") or "active",
        "grantee": grantee,
        "hidden_by_grantee": bool(row.get("hidden_by_grantee")),
        "public": bool(row.get("public")),
        "allow_actions": bool(row.get("allow_actions")),
        "created_by": row.get("created_by") or "",
        "created_by_name": row.get("creator_name") or "",
        "created_at": row.get("created_at") or "",
        "expires_at": row.get("expires_at"),
        "last_access_at": row.get("last_access_at"),
        "access_count": int(row.get("access_count") or 0),
    }


@router.get("/v1/shares")
async def list_shares(
    target_kind: str = Query(...),
    target: str = Query(...),
    user: UserContext | None = Depends(get_current_user),
):
    """The unrevoked shares of one target, for whoever may share it."""
    u = require_human(user)

    def _load() -> list[dict]:
        _share_target(u, target_kind, target)
        return [_shape(r) for r in share_store.list_target_shares(target_kind, target)]

    return {"shares": await asyncio.to_thread(_load)}


def _check_link_password(plain: str) -> None:
    """A link password is 8 to 64 characters and at most bcrypt's 72 bytes."""
    if not 8 <= len(plain) <= 64:
        raise HTTPException(status_code=400, detail="a link password is 8 to 64 characters")
    if len(plain.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"a link password is at most {MAX_PASSWORD_BYTES} bytes")


async def _link_password_hash(plain: str) -> str:
    """The hash of a checked link password, through the bounded password
    gate the sign-ins share, never on the loop. Callers run it only after
    the caller's authority over the share is established, so a request
    that may not change anything costs no hash."""
    _check_link_password(plain)
    try:
        return await hash_password_async(plain)
    except HashBusy:
        raise confirm.hash_busy()


async def _create_external(req: ShareCreateRequest, u: UserContext, request: Request) -> dict:
    """A link (SHARING.md "External links"): the platform switches, the
    confirm, then a token the caller sees once, with the generated
    password once. The row keeps the token's hash and the password's hash."""
    on, public_on = await asyncio.to_thread(
        lambda: (_setting_on(SETTING_EXTERNAL), _setting_on(SETTING_PUBLIC)))
    if not on:
        raise HTTPException(status_code=403, detail="External links are turned off by an admin")
    if req.public and not public_on:
        raise HTTPException(status_code=403,
                            detail="Links without a password are turned off by an admin")
    await confirm.confirm_human(u, password=req.password, confirm_token=req.confirm_token)
    allowed, retry_after = rate_limiter.hit("share_create", u.sub)
    if not allowed:
        raise HTTPException(status_code=429,
                            detail=f"Too many shares — try again in {retry_after} s")

    def _authorized() -> dict:
        target = _share_target(u, req.target_kind, req.target_id)
        if req.target_kind == "chat":
            _check_chat_share_cap(u)
        return target

    plain = ""
    password_hash = ""
    if not req.public:
        plain = (req.link_password or "").strip() or _generate_link_password()
        _check_link_password(plain)
        await asyncio.to_thread(_authorized)
        password_hash = await _link_password_hash(plain)

    def _create() -> tuple[dict, str, str]:
        target = _authorized()
        expires_at = _parse_expiry(req.expires_in or EXTERNAL_DEFAULT_EXPIRY, _max_expiry_days())
        token = secrets.token_urlsafe(32)
        share = share_store.create_external_share(
            target_kind=req.target_kind, target_id=req.target_id, created_by=u.sub,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            password_hash=password_hash, public=bool(req.public),
            allow_actions=bool(req.allow_actions) and req.target_kind == "app",
            expires_at=expires_at, include_tools=bool(req.include_tools),
        )
        if req.target_kind == "chat":
            _snapshot_for(u, share, target, bool(req.include_tools))
        return share, token, plain

    share, token, plain = await asyncio.to_thread(_create)
    base = (config.DASHBOARD_PUBLIC_URL or request_origin(request)).rstrip("/")
    out = {"status": "ok", "share": _shape(share), "link": f"{base}/s/{token}"}
    if plain:
        out["password"] = plain
    logger.info(f"External link created: share={share['id']}, kind={req.target_kind}, "
                f"target={req.target_id}, public={bool(req.public)}, "
                f"actions={bool(req.allow_actions)}, by={u.sub[:16]}")
    return out


@router.post("/v1/shares")
async def create_share(
    req: ShareCreateRequest,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Grant a platform user access to an app (internal share), or make a
    link (external). With the user directory closed to members, a known and
    an unknown identifier get the same answer, so the route is no existence
    oracle; the sharer sees the grant in their share list when it landed."""
    u = require_human(user)
    if req.scope == "external":
        return await _create_external(req, u, request)
    if req.scope != "internal":
        raise HTTPException(status_code=400, detail="scope must be 'internal' or 'external'")
    allowed, retry_after = rate_limiter.hit("share_create", u.sub)
    if not allowed:
        raise HTTPException(status_code=429,
                            detail=f"Too many shares — try again in {retry_after} s")

    def _create() -> dict:
        row = _share_target(u, req.target_kind, req.target_id)
        if req.target_kind == "chat":
            _check_chat_share_cap(u)
        directory = _directory_open(u)
        grantee = _resolve_grantee(req.grantee)
        if grantee is None:
            if directory:
                raise HTTPException(status_code=404, detail="No such user")
            return {"status": "ok"}
        if grantee["sub"] == u.sub:
            raise HTTPException(status_code=400, detail="You already have access")
        if req.target_kind == "app" and (row.get("owner_sub") or "") == grantee["sub"]:
            raise HTTPException(status_code=400, detail="That user owns this app")
        expires_at = _parse_expiry(req.expires_in, _max_expiry_days())
        try:
            share = share_store.create_internal_share(
                target_kind=req.target_kind, target_id=req.target_id,
                grantee_sub=grantee["sub"], created_by=u.sub, expires_at=expires_at,
            )
        except share_store.ShareExists:
            if directory:
                raise HTTPException(status_code=409, detail="Already shared with that user")
            return {"status": "ok"}
        if req.target_kind == "chat":
            _snapshot_for(u, share, row, bool(req.include_tools))
        else:
            audience.forget(req.target_id)
        out = {"status": "ok"} if not directory else {"status": "ok", "share": _shape(share)}
        is_app = req.target_kind == "app"
        out["_notify"] = {
            "grantee_sub": grantee["sub"],
            "title": (row.get("title") or row.get("slug") or "") if is_app else (row.get("title") or "a chat"),
            "agent": row.get("agent") or "",
            "href": f"/apps/{row['id']}" if is_app else f"/shared/{share['id']}",
            "sharer": u.display_name or u.name or u.email,
            "what": "app" if is_app else "chat",
        }
        return out

    out = await asyncio.to_thread(_create)
    notify = out.pop("_notify", None)
    if notify:
        from services.notifications import notification_manager
        try:
            await notification_manager.fire_notification(
                title=f"{notify['sharer']} shared “{notify['title']}” with you",
                body=("Open it from Shared with me, or from this notice."
                      if notify.get("what") == "app" else
                      "A read-only copy of the conversation as it stood when it was shared."),
                severity="info", scope="user", target=notify["grantee_sub"],
                source="share", agent_slug=notify["agent"] or None, href=notify["href"],
            )
        except Exception:
            logger.exception("share notification failed")
        logger.info(f"Share created: kind={req.target_kind}, target={req.target_id}, "
                    f"by={u.sub[:16]}")
    return out


@router.patch("/v1/shares/{share_id}")
async def patch_share(
    share_id: str,
    req: SharePatchRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Revoke, resume, re-date a share, or flip a link's Buttons switch (on
    needs the confirm) or password. A revoke: the creator, an admin, or
    whoever may share the target today. Anything else: whoever may share
    the target today, the creator included (a demoted creator cannot
    re-date or resume a grant). A revoked share stays revoked."""
    u = require_human(user)
    new_password = ""
    if req.link_password is not None and not req.revoke:
        new_password = req.link_password.strip()
        _check_link_password(new_password)
        # A new link password costs a bcrypt on the gate the sign-ins
        # share: counted per person before the first await.
        allowed, retry_after = rate_limiter.hit("share_create", u.sub)
        if not allowed:
            raise HTTPException(status_code=429,
                                detail=f"Too many link changes: try again in {retry_after} s")
    if req.allow_actions:
        await confirm.confirm_human(u, password=req.password, confirm_token=req.confirm_token)

    def _authorized() -> tuple[dict, str]:
        """The live share and its target id, once the caller may make this
        change to it."""
        share = share_store.get_share(share_id)
        if not share or share.get("revoked_at"):
            raise HTTPException(status_code=404, detail="Share not found")
        target_id = share.get("app_id") or share.get("chat_id") or ""
        # An admin's revoke needs no view of the target: an unpinned app or
        # a deleted chat still leaves a link the admin list must close.
        if not (req.revoke and (share.get("created_by") == u.sub or u.is_admin)):
            _may_still_share(u, share["target_kind"], target_id)
        if new_password and (share.get("scope") != "external" or share.get("public")):
            raise HTTPException(status_code=400, detail="Only a password link has a password")
        return share, target_id

    new_password_hash = ""
    if new_password:
        await asyncio.to_thread(_authorized)
        new_password_hash = await _link_password_hash(new_password)

    def _apply() -> dict:
        share, target_id = _authorized()
        if req.revoke:
            share_store.revoke_share(share_id)
        elif req.resume:
            if share["target_kind"] == "app":
                row = task_store.get_app(target_id)
                if not row or row.get("hidden"):
                    raise HTTPException(status_code=409,
                                        detail="The app is unpinned — pin it again first")
            share_store.set_share_state(share_id, "active")
        if req.expires_in is not None and not req.revoke:
            share_store.set_share_expiry(share_id, _parse_expiry(req.expires_in, _max_expiry_days()))
        if req.allow_actions is not None and not req.revoke:
            if share.get("scope") != "external":
                raise HTTPException(status_code=400, detail="Only a link has a Buttons switch")
            share_store.set_allow_actions(share_id, bool(req.allow_actions))
        if new_password_hash:
            share_store.set_password_hash(share_id, new_password_hash)
        audience.forget(target_id)
        fresh = share_store.get_share(share_id) or share
        return _shape(fresh)

    return await asyncio.to_thread(_apply)


@router.get("/v1/shares/mine")
async def list_my_shares(user: UserContext | None = Depends(get_current_user)):
    """What was shared with the caller ("Shared with me"): live grants only,
    each with the page that opens it. Needs no agent access."""
    u = require_auth(user)

    def _load() -> list[dict]:
        out = []
        for r in share_store.list_grants_for(u.sub):
            target_id = r.get("app_id") or r.get("chat_id") or ""
            out.append({
                "id": r["id"],
                "target_kind": r["target_kind"],
                "target_id": target_id,
                "title": r.get("target_title") or r.get("app_slug") or "",
                "agent": r.get("agent") or "",
                "shared_by": r.get("created_by") or "",
                "shared_by_name": r.get("creator_name") or "",
                "created_at": r.get("created_at") or "",
                "expires_at": r.get("expires_at"),
                "hidden": bool(r.get("hidden_by_grantee")),
                "href": (f"/apps/{target_id}" if r["target_kind"] == "app"
                         else f"/shared/{r['id']}"),
            })
        return out

    return {"shares": await asyncio.to_thread(_load)}


def _snapshot_viewer(u: UserContext, share: dict) -> bool:
    """May this user read a chat share's snapshot: its grantee while the
    share is live, its creator, whoever may still open the chat, an admin."""
    from api.agents.chats import can_access_chat
    if share.get("target_kind") != "chat":
        return False
    if u.is_admin or share.get("created_by") == u.sub:
        return True
    if share.get("scope") == "internal" and share.get("grantee_sub") == u.sub \
            and share_store.is_live(share):
        return True
    chat = task_store.get_chat(share.get("chat_id") or "")
    return bool(chat) and can_access_chat(u, chat)


def _load_snapshot_share(u: UserContext, share_id: str) -> dict:
    share = share_store.get_share(share_id)
    if not share or share.get("revoked_at") or not _snapshot_viewer(u, share):
        raise HTTPException(status_code=404, detail="Share not found")
    return share


@router.get("/v1/shares/{share_id}/snapshot")
async def read_snapshot(share_id: str, user: UserContext | None = Depends(get_current_user)):
    """A chat share's snapshot for the read-only page (``/shared/<id>``)."""
    u = require_auth(user)

    def _load() -> dict:
        from services.sharing import chat_snapshot
        share = _load_snapshot_share(u, share_id)
        doc = chat_snapshot.load(share)
        if not doc:
            raise HTTPException(status_code=404, detail="Share not found")
        creator = task_store.get_user(share.get("created_by") or "") or {}
        return {
            "id": share["id"],
            "title": doc.get("title") or "",
            "agent": doc.get("agent") or "",
            "created_at": doc.get("created_at") or "",
            "shared_by_name": creator.get("display_name") or creator.get("name") or "",
            "include_tools": bool(doc.get("include_tools")),
            "messages": doc.get("messages") or [],
        }

    return await asyncio.to_thread(_load)


@router.get("/v1/shares/{share_id}/ui/{token}")
async def read_snapshot_ui(share_id: str, token: str, request: Request,
                           user: UserContext | None = Depends(get_current_user)):
    """A snapshot's artifact page, in the same sandbox as any artifact."""
    from api.media.ui import _placeholder, _ui_response, inject_runtime, is_full_document, wrap_fragment
    from services.sharing import chat_snapshot
    origin = request_origin(request)
    if user is None:
        return _ui_response(_placeholder("Sign in to OtoDock to view this artifact."), origin, 401)

    def _load() -> str | None:
        share = _load_snapshot_share(user, share_id)
        path = chat_snapshot.file_path(share, token)
        return path.read_text("utf-8", "replace") if path else None

    try:
        content = await asyncio.to_thread(_load)
    except HTTPException:
        content = None
    if content is None:
        return _ui_response(_placeholder("This artifact no longer exists."), origin, 404)
    if is_full_document(content):
        return _ui_response(inject_runtime(content), origin)
    return _ui_response(wrap_fragment(content), origin)


@router.get("/v1/shares/{share_id}/media/{token}")
async def read_snapshot_media(share_id: str, token: str,
                              user: UserContext | None = Depends(get_current_user)):
    """A snapshot's image, clip or file; inert types inline, the rest as a
    download (the ``/v1/media`` rule)."""
    import mimetypes
    from fastapi.responses import FileResponse
    from services.sharing import chat_snapshot
    u = require_auth(user)

    def _load():
        share = _load_snapshot_share(u, share_id)
        return chat_snapshot.file_path(share, token)

    path = await asyncio.to_thread(_load)
    if path is None:
        raise HTTPException(status_code=404, detail="media not found")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    if chat_snapshot.inline_ok(mime):
        return FileResponse(path, media_type=mime, headers=headers)
    return FileResponse(path, media_type=mime, filename=path.name, headers=headers)


@router.get("/v1/sharing/settings")
async def sharing_settings(user: UserContext | None = Depends(get_current_user)):
    """What the share form needs from the admin's sharing settings: the
    longest expiry (null when unset), so a link offers no expiry only
    while nothing caps it."""
    require_human(user)
    return {"max_expiry_days": await asyncio.to_thread(_max_expiry_days)}


@router.get("/v1/users/directory")
async def user_directory(user: UserContext | None = Depends(get_current_user)):
    """The people a share can go to. 404 when an admin closed the directory
    to members (they then share by exact username or email)."""
    u = require_human(user)

    def _load() -> list[dict] | None:
        if not _directory_open(u):
            return None
        return [{
            "sub": row["sub"],
            "name": row.get("display_name") or row.get("name") or "",
            "username": row.get("username") or "",
        } for row in task_store.list_users() if row["sub"] != u.sub]

    users = await asyncio.to_thread(_load)
    if users is None:
        raise HTTPException(status_code=404, detail="The user directory is not available")
    return {"users": users}


@router.get("/v1/admin/shares")
async def admin_list_shares(user: UserContext | None = Depends(get_current_user)):
    """Every external link on the platform (admin)."""
    require_admin(user)

    def _load() -> list[dict]:
        out = []
        for r in share_store.list_external_shares():
            shaped = _shape(r)
            shaped["title"] = r.get("target_title") or ""
            shaped["agent"] = r.get("agent") or ""
            out.append(shaped)
        return out

    return {"shares": await asyncio.to_thread(_load)}
