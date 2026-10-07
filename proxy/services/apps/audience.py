"""Who receives an app's live frames (APPS.md "Live apps"), and who uses it
for the ``app.audience`` platform method (``describe_audience``, uncached).

One database job per fan-out: the users who may see the row, minus those
who hid it for themselves. The answer is cached per row for a moment so a
burst of pushes costs one lookup; a viewer who just hid the app may still
receive frames for that long. Synchronous: call it on the DB executor.
"""

from __future__ import annotations

import time

from storage import database as task_store
from storage.automation import notification_store
from storage.identity import db_users
from auth import roles
from core.session import session_kind

_CACHE_TTL_S = 2.0
_cache: dict[str, tuple[float, list[str]]] = {}


def app_audience(row: dict) -> list[str]:
    """User subs that may see ``row`` right now (empty for a soft-unpinned
    row: nobody has it on screen)."""
    app_id = row.get("id") or ""
    now = time.monotonic()
    cached = _cache.get(app_id)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return list(cached[1])
    subs = _compute(row)
    _cache[app_id] = (now, subs)
    if len(_cache) > 512:
        for key in [k for k, (t, _) in _cache.items() if now - t >= _CACHE_TTL_S]:
            _cache.pop(key, None)
    return list(subs)


def forget(app_id: str) -> None:
    _cache.pop(app_id, None)


AUDIENCE_LIST_MAX = 500


def describe_audience(row: dict, *, names: bool = True) -> dict:
    """Who uses the app, for the ``app.audience`` platform method (APPS.md
    "Platform catalog"): the home agent's members at their row (a personal
    app's owner alone, as its manager), every agent a share placed it in
    with its members at the capped role, and the live person shares at
    their cap, each with the way they got in (``via``). Read now, never
    cached, hides not subtracted (a hide never removes access), no emails;
    with ``names`` off the usernames and display names are blank. Each
    list is cut at ``AUDIENCE_LIST_MAX`` and ``truncated`` says so.
    Synchronous: one DB job."""
    from storage.sharing import share_store
    agent = row.get("agent") or ""
    truncated = False

    def _cut(items: list) -> list:
        nonlocal truncated
        if len(items) > AUDIENCE_LIST_MAX:
            truncated = True
            return items[:AUDIENCE_LIST_MAX]
        return items

    def _person(sub: str, username: str, display_name: str, name: str) -> dict:
        shown = display_name or name
        return {"sub": sub, "username": username if names else "",
                "display_name": shown if names else ""}

    if row.get("username"):
        owner_sub = row.get("owner_sub") or task_store.get_user_sub_by_username(row["username"]) or ""
        owner = task_store.get_user(owner_sub) or {}
        members = [{**_person(owner_sub, owner.get("username") or "", owner.get("display_name") or "",
                              owner.get("name") or ""),
                    "role": roles.MANAGER, "via": "membership"}] if owner_sub else []
    else:
        members = [{**_person(r["sub"], r["username"], r["display_name"], r["name"]),
                    "role": r["agent_role"], "via": "membership"}
                   for r in db_users.get_agent_users_with_profile(agent)]
    placements = []
    for p in share_store.describe_placements(row.get("id") or ""):
        placements.append({
            "agent": p["agent"], "agent_name": p["agent_name"], "via": "placement",
            "kind": p["kind"], "share_id": p["share_id"], "role_cap": p["role_cap"],
            "department": p["department"],
            "members": _cut([{**_person(m["sub"], m["username"], m["display_name"], m["name"]),
                              "role": m["role"]} for m in p["members"]]),
        })
    grantees = []
    now = share_store._now()
    for s in share_store.list_target_shares("app", row.get("id") or ""):
        if s.get("grantee_kind") != share_store.PERSON or not share_store.is_live(s, now):
            continue
        grantees.append({
            **_person(s.get("grantee_sub") or "", s.get("grantee_username") or "",
                      s.get("grantee_display_name") or "", s.get("grantee_name") or ""),
            "role": s.get("role_cap") or roles.VIEWER, "via": "share", "share_id": s["id"],
            "decision": s.get("decision") or "", "placed_agent": s.get("placed_agent") or "",
        })
    return {"members": _cut(members), "placements": _cut(placements), "grantees": _cut(grantees),
            "truncated": truncated}


def _compute(row: dict) -> list[str]:
    if row.get("hidden"):
        return []
    agent = row.get("agent") or ""
    admins = set(notification_store.get_admin_user_subs())
    if row.get("username"):
        # A personal row reaches its owner only: admins may open it on
        # demand, but its frames are not theirs to receive.
        owner = row.get("owner_sub") or task_store.get_user_sub_by_username(row["username"]) or ""
        members: dict[str, str] = {owner: "owner"} if owner else {}
        candidates = set(members)
    else:
        members = {r["sub"]: r["agent_role"] for r in db_users.get_agent_users(agent)}
        candidates = (set(members) | admins) - task_store.hidden_for_users(row.get("id") or "")
    # People a share admits (SHARING.md): the person grantees who have not
    # hidden the app, and the members of every agent an agent or department
    # share places it in, minus their hide there; a grantee of a Dock pin
    # still has to pass its chat below.
    from storage.sharing import share_store
    for sub in share_store.internal_grantees(row.get("id") or ""):
        candidates.add(sub)
        members.setdefault(sub, "grantee")
    if not row.get("username"):
        for sub, role in share_store.placement_audience(row.get("id") or ""):
            candidates.add(sub)
            members.setdefault(sub, role)
    chat_id = row.get("scope_chat_id") or ""
    project_id = row.get("scope_project_id") or ""
    if not chat_id and not project_id:
        return sorted(candidates)
    # A Dock pin follows its chat's access rule, the chat fetched once.
    if chat_id:
        chat = task_store.get_chat(chat_id)
        chats = [chat] if chat else []
    else:
        chats = [c for c in task_store.list_chats_by_project(project_id) if c]
    if not chats:
        return []
    return sorted(
        sub for sub in candidates
        if any(_may_open_chat(sub, sub in admins, sub in members, c,
                              is_manager=roles.can_manage(members.get(sub))) for c in chats)
    )


def _may_open_chat(sub: str, is_admin: bool, is_member: bool, chat: dict, *,
                   is_manager: bool = False) -> bool:
    """``api.agents.chats.can_access_chat`` for a user known by sub and
    membership alone, so the fan-out builds no principal per candidate."""
    from api.agents.chats import _run_for_chat
    from core.session.visibility import is_phone_chat_owner, is_shared_chat_owner, is_task_chat_owner
    if is_admin:
        return True
    owner = chat.get("user_sub", "") or ""
    if session_kind.of_chat(chat) is session_kind.PHONE or is_phone_chat_owner(owner):
        return is_manager
    if owner == sub:
        return True
    if is_shared_chat_owner(owner):
        return is_member
    if is_task_chat_owner(owner) or session_kind.of_chat(chat) is session_kind.TASK:
        run = _run_for_chat(chat)
        if run is not None:
            if (run.get("scope") or "agent") == "user":
                return run.get("created_by") == sub
            return is_member
    return False
