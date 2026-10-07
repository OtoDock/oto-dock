"""Shares (SHARING.md): who may hand an app or a chat to someone else, and
the surfaces a recipient uses.

Every route that creates or changes a share is HUMAN-ONLY (``require_human``):
a share is an exfiltration channel, so no session token or API key — the
credentials a prompt can wield — ever reaches it. Authority follows the
target: the owner of a personal app, editor or above on a shared app, and
never a Dock pin (a chat- or project-scoped app is reachable only with its
chat). A share names a person, an agent or a department and carries a role
cap; a person's app share waits in their "Shared with you" section until
they accept it into one of their agents (it opens meanwhile), an agent share
lands in that agent's Apps panel (editor or above on the receiving agent
shares to it; nobody below), a department share lands in every agent of the
department. Placements follow the membership and
department rows at read time; a person share itself changes nothing when
memberships change.
"""

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

import config
from api.apps import manifest as _mf
from api.apps.apps import _can_manage, _visible_row
from api.media.ui import request_origin
from auth import confirm, rate_limiter, roles
from auth.password import MAX_PASSWORD_BYTES, HashBusy, hash_password_async
from auth.providers import (
    UserContext,
    get_current_user,
    require_admin,
    require_auth,
    require_human,
    require_user,
)
from services.apps import audience
from services.sharing import share_inbox
from storage import database as task_store
from storage.agents import agent_store, db_departments
from storage.sharing import share_store
from core.session import session_kind
from core.session.visibility import is_task_chat_owner

logger = logging.getLogger("claude-proxy.shares")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])

MAX_EXPIRY_DAYS_HARD = 3650
SETTING_DIRECTORY = "user_directory_visible_to_members"
SETTING_MAX_EXPIRY = "sharing_max_expiry_days"
SETTING_EXTERNAL = "sharing_external_enabled"
SETTING_PUBLIC = "sharing_public_links_enabled"
SETTING_TO_AGENTS = share_store.SETTING_TO_AGENTS
SETTING_TO_DEPARTMENTS = share_store.SETTING_TO_DEPARTMENTS
EXTERNAL_DEFAULT_EXPIRY = "30d"
# Link passwords: eight characters from an alphabet without look-alikes.
_LINK_PW_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"


class ShareCreateRequest(BaseModel):
    target_kind: str
    target_id: str
    scope: str = "internal"
    # Internal: who receives it. A person's sub (from the directory), exact
    # username or email; an agent's slug; a department's id.
    grantee_kind: str = share_store.PERSON
    grantee: str = ""
    # The role the recipient acts with on the app (``roles.AGENT_ROLES``,
    # viewer when empty); never above the sharer's own.
    role_cap: str = ""
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


class ShareAcceptRequest(BaseModel):
    # A person's app share: the agent to place it in (one they hold).
    agent: str = ""


class SharePlacementRequest(BaseModel):
    # An agent or department placement: the agent whose panel the viewer
    # hides or restores it in. A person's own share needs no body.
    agent: str = ""


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
    """The cap on a person's live CHAT shares (the snapshots it bounds); app
    shares of every kind are not counted."""
    from services.sharing import chat_snapshot
    n = share_store.count_live_shares_by(u.sub, target_kind="chat")
    if n >= chat_snapshot.MAX_CHAT_SHARES_PER_USER:
        raise HTTPException(status_code=400,
                            detail=f"You already have {n} live chat shares — revoke one first")


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


def _resolve_cap(req_cap: str, row: dict, u: UserContext, target_kind: str) -> str:
    """The role cap a share carries: viewer for a chat (a copy is
    read-only); for an app, the word asked for (viewer when empty), never
    above the sharer's own standing on the app (``caller_role``: admin, the
    owner of a personal app as its manager, else their per-agent row)."""
    cap = (req_cap or "").strip() or roles.VIEWER
    if target_kind != "app":
        return roles.VIEWER
    if cap not in roles.AGENT_ROLES:
        raise HTTPException(status_code=400, detail="role_cap must be one of "
                            + ", ".join(roles.AGENT_ROLES))
    own = _mf.caller_role(row, u)
    if roles.rank(cap) > roles.rank(own):
        raise HTTPException(status_code=400,
                            detail=f"You cannot share above your own role ({roles.label(own)})")
    return cap


def _visible_agents(u: UserContext, directory: bool) -> list[dict]:
    """The agents a share may name, as the popover lists them: every agent
    while the directory is open to the viewer (admin-only agents only to an
    admin), the viewer's own agents while it is closed."""
    if not _setting_on(SETTING_TO_AGENTS):
        return []
    out = []
    for a in agent_store.get_all_agents():
        if a.get("admin_only") and not u.is_admin:
            continue
        if not directory and not u.can_access_agent(a["slug"]):
            continue
        out.append({"slug": a["slug"], "display_name": a.get("display_name") or a["slug"],
                    "color": a.get("color") or ""})
    return out


def _visible_departments(u: UserContext) -> list[dict]:
    if not u.is_admin or not _setting_on(SETTING_TO_DEPARTMENTS):
        return []
    return [{"id": d["id"], "name": d.get("name") or d["id"]}
            for d in db_departments.list_departments()]


def _shape(row: dict, viewer_sub: str | None = None) -> dict:
    """A share for the API; ``placed_agent`` (the agent the recipient chose)
    is the recipient's own and shows to them alone."""
    grantee = None
    if row.get("grantee_sub"):
        grantee = {
            "sub": row["grantee_sub"],
            "name": row.get("grantee_display_name") or row.get("grantee_name") or "",
            "username": row.get("grantee_username") or "",
        }
    to_agent = None
    if row.get("grantee_agent"):
        to_agent = {"slug": row["grantee_agent"],
                    "name": row.get("to_agent_name") or row["grantee_agent"]}
    to_department = None
    if row.get("grantee_department"):
        to_department = {"id": row["grantee_department"],
                         "name": row.get("to_department_name") or ""}
    return {
        "id": row["id"],
        "target_kind": row["target_kind"],
        "target_id": row.get("app_id") or row.get("chat_id") or "",
        "scope": row["scope"],
        "state": row.get("state") or "active",
        "grantee_kind": row.get("grantee_kind") or "",
        "grantee": grantee,
        "to_agent": to_agent,
        "to_department": to_department,
        "role_cap": row.get("role_cap") or roles.VIEWER,
        "decision": row.get("decision") or "",
        "decided_by": row.get("decided_by") or "",
        "decided_by_name": row.get("decider_display_name") or row.get("decider_name") or "",
        "placed_agent": (row.get("placed_agent") or "") if viewer_sub and row.get("grantee_sub") == viewer_sub else "",
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
        return [_shape(r, u.sub) for r in share_store.list_target_shares(target_kind, target)]

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


def _target_words(row: dict, target_kind: str) -> tuple[str, str]:
    """The title and the agent of a share's target, for a notice."""
    if target_kind == "app":
        return (row.get("title") or row.get("slug") or ""), (row.get("agent") or "")
    return (row.get("title") or "a chat"), (row.get("agent") or "")


@router.post("/v1/shares")
async def create_share(
    req: ShareCreateRequest,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Share an app or a chat with a person, an app with an agent or a
    department (internal), or make a link (external). With the user
    directory closed to members, a known and an unknown identifier get the
    same answer, so the route is no existence oracle; the sharer sees the
    share in their share list when it landed."""
    u = require_human(user)
    if req.scope == "external":
        return await _create_external(req, u, request)
    if req.scope != "internal":
        raise HTTPException(status_code=400, detail="scope must be 'internal' or 'external'")
    kind = (req.grantee_kind or share_store.PERSON).strip()
    if kind not in share_store.GRANTEE_KINDS:
        raise HTTPException(status_code=400, detail="grantee_kind must be one of "
                            + ", ".join(share_store.GRANTEE_KINDS))
    allowed, retry_after = rate_limiter.hit("share_create", u.sub)
    if not allowed:
        raise HTTPException(status_code=429,
                            detail=f"Too many shares — try again in {retry_after} s")

    def _create() -> dict:
        row = _share_target(u, req.target_kind, req.target_id)
        is_app = req.target_kind == "app"
        if not is_app:
            _check_chat_share_cap(u)
        if kind != share_store.PERSON:
            # A chat share is a read-only copy for a person (an agent has no
            # section to read it from); a personal app runs on its owner's
            # authority and goes dormant when the owner leaves its agent, so
            # a team never depends on one (the operator, 2026-10-04).
            if not is_app:
                raise HTTPException(status_code=400, detail="A chat is shared with people only")
            if row.get("username"):
                raise HTTPException(status_code=400,
                                    detail="A personal app is shared with people only. An "
                                           "app meant for a team is the agent's shared app.")
            if kind == share_store.AGENT and not _setting_on(SETTING_TO_AGENTS):
                raise HTTPException(status_code=403,
                                    detail="Sharing to agents is turned off by an admin")
            if kind == share_store.DEPARTMENT:
                if not u.is_admin:
                    raise HTTPException(status_code=403,
                                        detail="Only an admin shares with a department")
                if not _setting_on(SETTING_TO_DEPARTMENTS):
                    raise HTTPException(status_code=403,
                                        detail="Sharing to departments is turned off by an admin")
        cap = _resolve_cap(req.role_cap, row, u, req.target_kind)
        directory = _directory_open(u)
        title, agent = _target_words(row, req.target_kind)
        fields: dict = {"grantee_kind": kind, "role_cap": cap}
        notify: dict | None = None
        if kind == share_store.PERSON:
            grantee = _resolve_grantee(req.grantee)
            if grantee is None:
                if directory:
                    raise HTTPException(status_code=404, detail="No such user")
                return {"status": "ok"}
            if grantee["sub"] == u.sub:
                raise HTTPException(status_code=400, detail="You already have access")
            if is_app and (row.get("owner_sub") or "") == grantee["sub"]:
                raise HTTPException(status_code=400, detail="That user owns this app")
            fields.update(grantee_sub=grantee["sub"],
                          decision=share_store.PENDING if is_app else share_store.ACCEPTED,
                          decided_by=None if is_app else u.sub)
            # A person share also fires a notification (toast and push)
            # beside its section item.
            notify = {"subs": [grantee["sub"]], "what": "app" if is_app else "chat"}
        elif kind == share_store.AGENT:
            slug = (req.grantee or "").strip()
            target = agent_store.get_agent(slug) if slug else None
            if not target or (target.get("admin_only") and not u.is_admin):
                if directory:
                    raise HTTPException(status_code=404, detail="No such agent")
                return {"status": "ok"}
            if not directory and not u.can_access_agent(slug):
                # With the directory closed an agent the sharer does not
                # hold answers like an unknown one (no existence oracle):
                # they could never share to it.
                return {"status": "ok"}
            if slug == (row.get("agent") or ""):
                raise HTTPException(status_code=400, detail="That is the app's own agent")
            if not u.can_edit_agent(slug):
                raise HTTPException(status_code=403,
                                    detail="Sharing with an agent needs the editor role or "
                                           "above on it")
            fields.update(grantee_agent=slug, decision=share_store.ACCEPTED, decided_by=u.sub)
        else:
            dept_id = (req.grantee or "").strip()
            dept = db_departments.get_department(dept_id) if dept_id else None
            if not dept:
                raise HTTPException(status_code=404, detail="No such department")
            fields.update(grantee_department=dept_id, decision=share_store.ACCEPTED,
                          decided_by=u.sub)
        expires_at = _parse_expiry(req.expires_in, _max_expiry_days())
        try:
            share = share_store.create_internal_share(
                target_kind=req.target_kind, target_id=req.target_id, created_by=u.sub,
                expires_at=expires_at, **fields,
            )
        except share_store.ShareExists:
            if directory:
                raise HTTPException(status_code=409, detail="Already shared with that "
                                    + ("user" if kind == share_store.PERSON else kind))
            return {"status": "ok"}
        if not is_app:
            _snapshot_for(u, share, row, bool(req.include_tools))
        else:
            audience.forget(req.target_id)
        # The INSERT answers the bare row: the names the dialog shows at
        # once come from what the route already resolved.
        if kind == share_store.PERSON:
            share.update(grantee_display_name=grantee.get("display_name") or "",
                         grantee_name=grantee.get("name") or "",
                         grantee_username=grantee.get("username") or "")
        elif kind == share_store.AGENT:
            share["to_agent_name"] = target.get("display_name") or slug
        else:
            share["to_department_name"] = dept.get("name") or ""
        shaped = _shape(share, u.sub)
        shaped["landed"] = share["decision"] == share_store.ACCEPTED
        out = {"status": "ok"} if not directory else {"status": "ok", "share": shaped}
        if notify is not None:
            notify.update(title=title, agent=agent, sharer=u.display_name or u.name or u.email,
                          href=(f"/apps/{row['id']}" if is_app else f"/shared/{share['id']}"))
        out["_notify"] = notify
        out["_targets"] = share_inbox.targets_for(share)
        return out

    out = await asyncio.to_thread(_create)
    notify = out.pop("_notify", None)
    targets = out.pop("_targets", None)
    if targets:
        await share_inbox.send(targets)
    if notify:
        from services.notifications import notification_manager
        body = ("Accept it into one of your agents from Shared with you, or open it from "
                "this notice." if notify["what"] == "app" else
                "A read-only copy of the conversation as it stood when it was shared.")
        for sub in notify["subs"]:
            try:
                await notification_manager.fire_notification(
                    title=f"{notify['sharer']} shared “{notify['title']}” with you",
                    body=body, severity="info", scope="user", target=sub,
                    source="share", agent_slug=notify["agent"] or None, href=notify["href"],
                )
            except Exception:
                logger.exception("share notification failed")
    if out.get("share") or notify or targets:
        logger.info(f"Share created: kind={req.target_kind}, to={kind}, "
                    f"target={req.target_id}, by={u.sub[:16]}")
    return out


@router.patch("/v1/shares/{share_id}")
async def patch_share(
    share_id: str,
    req: SharePatchRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Revoke, resume, re-date a share, or flip a link's Buttons switch (on
    needs the confirm) or password. A revoke: the creator, an admin, an
    editor or manager of the agent an agent share names ("Remove from this
    agent"), the person an app share names while it is pending or accepted
    ("Remove for me"), or whoever may share the target today. Anything
    else: whoever may share the target today, the creator included (a
    demoted creator cannot re-date or resume a grant). A revoked share
    stays revoked."""
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

    def _receiving_editor(share: dict) -> bool:
        return (share.get("grantee_kind") == share_store.AGENT
                and u.can_edit_agent(share.get("grantee_agent") or ""))

    def _own_app_grant(share: dict) -> bool:
        # A chat share is left out: it needs no decision, the person hides
        # it, and a revoke would close the creator's own view of the copy.
        return (share.get("scope") == share_store.INTERNAL
                and share.get("grantee_kind") == share_store.PERSON
                and share.get("grantee_sub") == u.sub
                and share.get("target_kind") == "app"
                and share.get("decision") in (share_store.PENDING, share_store.ACCEPTED))

    def _authorized() -> tuple[dict, str, bool]:
        """The live share, its target id and whether the caller's only
        right is their own (a person removing a share of theirs), once the
        caller may make this change to it."""
        share = share_store.get_share(share_id)
        if not share or share.get("revoked_at"):
            raise HTTPException(status_code=404, detail="Share not found")
        target_id = share.get("app_id") or share.get("chat_id") or ""
        # An admin's revoke needs no view of the target: an unpinned app or
        # a deleted chat still leaves a link the admin list must close.
        team_right = (share.get("created_by") == u.sub or u.is_admin
                      or _receiving_editor(share))
        own_only = bool(req.revoke) and not team_right and _own_app_grant(share)
        if not (req.revoke and team_right) and not own_only:
            _may_still_share(u, share["target_kind"], target_id)
        if new_password and (share.get("scope") != "external" or share.get("public")):
            raise HTTPException(status_code=400, detail="Only a password link has a password")
        return share, target_id, own_only

    new_password_hash = ""
    if new_password:
        await asyncio.to_thread(_authorized)
        new_password_hash = await _link_password_hash(new_password)

    def _apply() -> tuple[dict, dict]:
        share, target_id, own_only = _authorized()
        if req.revoke and own_only:
            if not share_store.revoke_share(
                    share_id, decisions=(share_store.PENDING, share_store.ACCEPTED)):
                # Declined or revoked since the read above.
                raise HTTPException(status_code=404, detail="Share not found")
        elif req.revoke:
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
        targets = share_inbox.targets_for(fresh) if fresh.get("scope") == "internal" else {}
        return _shape(fresh, u.sub), targets

    shaped, targets = await asyncio.to_thread(_apply)
    if targets:
        await share_inbox.send(targets)
    return shaped


# ───────────────────────── the "Shared with you" section ────────────────────


def _inbox_item(r: dict) -> dict:
    """One row of the section, with the actions its kind offers: a pending
    app share takes a decision (and opens meanwhile); a received app or a
    chat opens and hides."""
    is_app = r["target_kind"] == "app"
    target_id = r.get("app_id") or r.get("chat_id") or ""
    waiting = is_app and r.get("decision") == share_store.PENDING
    actions = ["accept", "decline"] if waiting else ["open", "hide"]
    return {
        "id": r["id"],
        "kind": r["target_kind"],
        "target_id": target_id,
        "title": r.get("target_title") or r.get("app_slug") or "",
        "agent": r.get("agent") or "",
        "agent_name": r.get("agent_display_name") or r.get("agent") or "",
        "agent_color": r.get("agent_color") or "",
        "shared_by": r.get("created_by") or "",
        "shared_by_name": r.get("creator_display_name") or r.get("creator_name") or "",
        "created_at": r.get("created_at") or "",
        "expires_at": r.get("expires_at"),
        "role_cap": r.get("role_cap") or roles.VIEWER,
        "href": f"/apps/{target_id}" if is_app else f"/shared/{r['id']}",
        "placed_agent": r.get("placed_now") or "",
        "actions": actions,
    }


@router.get("/v1/shares/inbox")
async def share_inbox_read(user: UserContext | None = Depends(get_current_user)):
    """The viewer's "Shared with you" section (SHARING.md): their own live
    shares, pending first, with each item's actions, and the count of
    decisions waiting on them."""
    u = require_human(user)

    def _load() -> dict:
        rows = share_store.inbox_for(u.sub)
        pending = share_store.pending_count_for([u.sub]).get(u.sub, 0)
        return {"items": [_inbox_item(r) for r in rows], "pending": pending}

    return await asyncio.to_thread(_load)


def _own_app_share(u: UserContext, share_id: str) -> dict:
    """The viewer's own live app share, or the same 404 as a missing one
    (a chat needs no decision; another person's share is not theirs to see)."""
    share = share_store.get_share(share_id)
    if (not share or not share_store.is_live(share) or share.get("scope") != "internal"
            or share.get("grantee_kind") != share_store.PERSON
            or share.get("grantee_sub") != u.sub):
        raise HTTPException(status_code=404, detail="Share not found")
    if share["target_kind"] != "app":
        raise HTTPException(status_code=400, detail="A chat needs no decision")
    return share


@router.post("/v1/shares/{share_id}/accept")
async def accept_share(
    share_id: str,
    req: ShareAcceptRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """A person accepts an app into one of their agents (the body names
    it, one they hold; an accepted share may be moved to another). The
    app's own agent is never a place: a member sees it there already."""
    u = require_human(user)

    def _apply() -> tuple[dict, dict]:
        share = _own_app_share(u, share_id)
        agent = (req.agent or "").strip()
        if not agent:
            raise HTTPException(status_code=400, detail="Choose an agent to place it in")
        if not u.can_access_agent(agent) or not agent_store.get_agent(agent):
            raise HTTPException(status_code=403, detail="You are not a member of that agent")
        row = task_store.get_app(share["app_id"]) or {}
        if agent == (row.get("agent") or ""):
            raise HTTPException(status_code=400, detail="That is the app's own agent")
        left = share.get("placed_agent") or ""
        if not share_store.set_decision(share_id, share_store.ACCEPTED, u.sub,
                                        placed_agent=agent):
            # Declined or revoked since the read above.
            raise HTTPException(status_code=404, detail="Share not found")
        audience.forget(share["app_id"])
        fresh = share_store.get_share(share_id) or share
        targets = share_inbox.targets_for(fresh)
        if left and left != agent:
            # A move: the panel the app left changed too.
            targets.setdefault(u.sub, set()).add(left)
        return _shape(fresh, u.sub), targets

    shaped, targets = await asyncio.to_thread(_apply)
    await share_inbox.send(targets)
    return {"status": "ok", "share": shaped}


@router.post("/v1/shares/{share_id}/decline")
async def decline_share(
    share_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """A person declines an app share waiting on them. The row stays in
    the sharer's list as declined (until they remove it, or 30 days); a new
    share to the same person replaces it. One they accepted is removed
    with the share's revoke ("Remove for me")."""
    u = require_human(user)

    def _apply() -> tuple[dict, dict]:
        share = _own_app_share(u, share_id)
        if share["decision"] != share_store.PENDING:
            raise HTTPException(status_code=400, detail="Only a share waiting for a decision "
                                                        "can be declined")
        targets = share_inbox.targets_for(share)
        if not share_store.set_decision(share_id, share_store.DECLINED, u.sub,
                                        expect=share_store.PENDING):
            # Revoked, declined or accepted since the read above.
            raise HTTPException(status_code=404, detail="Share not found")
        audience.forget(share["app_id"])
        fresh = share_store.get_share(share_id) or share
        return _shape(fresh, u.sub), targets

    shaped, targets = await asyncio.to_thread(_apply)
    await share_inbox.send(targets)
    return {"status": "ok", "share": shaped}


def _set_hidden(u: UserContext, share_id: str, agent: str, hidden: bool) -> tuple[str, dict]:
    """A viewer's own hide of a share: their person share (an app or a
    chat), or an agent or department placement in the agent the body names,
    which they must hold. Returns the app id (empty for a chat) and the
    frame targets."""
    share = share_store.get_share(share_id)
    if not share or share.get("revoked_at") or share.get("scope") != "internal":
        raise HTTPException(status_code=404, detail="Share not found")
    kind = share.get("grantee_kind")
    app_id = share.get("app_id") or ""
    if kind == share_store.PERSON:
        if share.get("grantee_sub") != u.sub:
            raise HTTPException(status_code=404, detail="Share not found")
        share_store.set_share_hidden(share_id, u.sub, hidden)
        placed = share.get("placed_agent") or ""
        return app_id, {u.sub: {placed} if placed else set()}
    agent = (agent or "").strip()
    if not agent or not u.can_access_agent(agent):
        raise HTTPException(status_code=404, detail="Share not found")
    if not share_store.placement_for(app_id, agent, u.sub):
        raise HTTPException(status_code=404, detail="Share not found")
    share_store.set_placement_hidden(app_id, agent, u.sub, hidden)
    return app_id, {u.sub: {agent}}


@router.post("/v1/shares/{share_id}/hide")
async def hide_share(
    share_id: str,
    req: SharePlacementRequest | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """Hide a received share for the viewer alone: their own share leaves
    the section (an app also leaves its panel), a placed app leaves the
    panel of the agent the body names. Nothing is revoked."""
    u = require_human(user)
    agent = req.agent if req else ""
    app_id, targets = await asyncio.to_thread(_set_hidden, u, share_id, agent, True)
    if app_id:
        audience.forget(app_id)
    await share_inbox.send(targets)
    return {"status": "ok"}


@router.post("/v1/shares/{share_id}/unhide")
async def unhide_share(
    share_id: str,
    req: SharePlacementRequest | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """Restore a hide. Idempotent."""
    u = require_human(user)
    agent = req.agent if req else ""
    app_id, targets = await asyncio.to_thread(_set_hidden, u, share_id, agent, False)
    if app_id:
        audience.forget(app_id)
    await share_inbox.send(targets)
    return {"status": "ok"}


# ───────────────────────── snapshots, settings, the directory ───────────────


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
    longest expiry (null when unset), whether shares to agents and to
    departments are on, and whether the directory is open to the viewer."""
    u = require_human(user)

    def _load() -> dict:
        return {
            "max_expiry_days": _max_expiry_days(),
            "sharing_to_agents_enabled": _setting_on(SETTING_TO_AGENTS),
            "sharing_to_departments_enabled": _setting_on(SETTING_TO_DEPARTMENTS),
            "directory_open": _directory_open(u),
        }

    return await asyncio.to_thread(_load)


@router.get("/v1/users/directory")
async def user_directory(user: UserContext | None = Depends(get_current_user)):
    """Who a share can go to. ``users`` is null when an admin closed the
    directory to members (they then share by exact username or email);
    ``agents`` lists every agent while the directory is open (admin-only
    agents to an admin), else the viewer's own; ``departments`` for an
    admin. A switch turned off empties its list."""
    u = require_human(user)

    def _load() -> dict:
        directory = _directory_open(u)
        users = None
        if directory:
            users = [{
                "sub": row["sub"],
                "name": row.get("display_name") or row.get("name") or "",
                "username": row.get("username") or "",
            } for row in task_store.list_users() if row["sub"] != u.sub]
        return {"users": users, "agents": _visible_agents(u, directory),
                "departments": _visible_departments(u)}

    return await asyncio.to_thread(_load)


ADMIN_LIST_MAX = 500
_ADMIN_SCOPES = {share_store.INTERNAL: (share_store.INTERNAL,),
                 share_store.EXTERNAL: (share_store.EXTERNAL,),
                 "all": share_store.SCOPES}


@router.get("/v1/admin/shares")
async def admin_list_shares(
    kind: str = Query("all"),
    agent: str = Query(""),
    standing: str = Query(""),
    user: UserContext | None = Depends(get_current_user),
):
    """Every share on the platform (admin): the shares to people, agents
    and departments (``internal``) and the links (``external``), the newest
    ``ADMIN_LIST_MAX`` of each kind asked, filtered by agent and standing in
    the query; ``truncated`` names a kind that was cut. A link row keeps its
    shape; every row adds ``title``, ``agent`` and ``standing``."""
    require_admin(user)
    scopes = _ADMIN_SCOPES.get(kind)
    if scopes is None:
        raise HTTPException(status_code=400, detail="kind must be internal, external or all")
    standing = (standing or "").strip()
    if standing and standing not in share_store.ADMIN_STANDINGS:
        raise HTTPException(status_code=400, detail="standing must be one of "
                            + ", ".join(share_store.ADMIN_STANDINGS))
    agent = (agent or "").strip()

    def _load() -> dict:
        shares: list[dict] = []
        truncated: dict[str, bool] = {}
        for scope in scopes:
            rows = share_store.list_admin_shares(scope, agent=agent, standing=standing,
                                                 limit=ADMIN_LIST_MAX + 1)
            truncated[scope] = len(rows) > ADMIN_LIST_MAX
            for r in rows[:ADMIN_LIST_MAX]:
                shaped = _shape(r)
                shaped["title"] = r.get("target_title") or ""
                shaped["agent"] = r.get("agent") or ""
                shaped["standing"] = r.get("standing") or ""
                shares.append(shaped)
        return {"shares": shares, "truncated": truncated}

    return await asyncio.to_thread(_load)
