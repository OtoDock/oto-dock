"""Who receives an app's live frames (APPS.md "Live apps"), and who uses it
for the ``app.audience`` platform method (``describe_audience``, uncached).

One database job per fan-out: the users who may see the row, minus those
who hid it for themselves. The answer is cached per row for a moment so a
burst of pushes costs one lookup; a viewer who just hid the app may still
receive frames for that long. Synchronous: call it on the DB executor.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from auth import roles
from storage import database as task_store, db_apps
from storage.automation import notification_store
from storage.identity import db_users
from ws import wire_events as wire

if TYPE_CHECKING:
    from auth.providers import UserContext

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
    # Each person's row on the app's agent: the role a Dock pin's chat is
    # judged at below.
    agent_rows: dict[str, str] = {}
    if row.get("username"):
        # A personal row reaches its owner only: admins may open it on
        # demand, but its frames are not theirs to receive.
        owner = row.get("owner_sub") or task_store.get_user_sub_by_username(row["username"]) or ""
        candidates = {owner} if owner else set()
    else:
        agent_rows = {r["sub"]: r["agent_role"] for r in db_users.get_agent_users(agent)}
        candidates = (set(agent_rows) | admins) - task_store.hidden_for_users(row.get("id") or "")
    # People a share admits (SHARING.md): the person grantees who have not
    # hidden the app, and the members of every agent an agent or department
    # share places it in, minus their hide there; a grantee of a Dock pin
    # still has to pass its chat below.
    from storage.sharing import share_store
    candidates.update(share_store.internal_grantees(row.get("id") or ""))
    if not row.get("username"):
        candidates.update(sub for sub, _role in share_store.placement_audience(row.get("id") or ""))
    chat_id = row.get("scope_chat_id") or ""
    project_id = row.get("scope_project_id") or ""
    if not chat_id and not project_id:
        return sorted(candidates)
    # A Dock pin follows its chat's access rule (``visibility.may_open_chat``),
    # each candidate at their own row on the app's agent: a grantee or a
    # placed agent's member holds none there unless they are a member too.
    from core.session import visibility
    if chat_id:
        chat = task_store.get_chat(chat_id)
        chats = [chat] if chat else []
    else:
        chats = [c for c in task_store.list_chats_by_project(project_id) if c]
    if not chats:
        return []
    if row.get("username"):
        agent_rows = {r["sub"]: r["agent_role"] for r in db_users.get_agent_users(agent)}
    facts = [(c, *visibility.chat_facts(c)) for c in chats]
    return sorted(
        sub for sub in candidates
        if any(visibility.may_open_chat(c, sub, agent_rows.get(sub) or roles.NO_ACCESS, admin=sub in admins,
                                        run=run, record=record)
               for c, run, record in facts)
    )


def app_access(row: dict, user: UserContext) -> bool:
    """May ``user`` see/serve this app? Personal rows → owner only; shared
    rows → anyone assigned to the agent; a live internal share grants
    either (SHARING.md); admin always. A chat- or project-scoped pin is
    additionally reachable only from its scope (``_scope_access``), grant or
    not. Reads the DB for scoped rows and for non-member callers — call it
    off the loop (``_visible_row``)."""
    # A session acts on the agent it was minted for and never across one,
    # whoever drives it (the app API's agent basis keeps the same rule): its
    # bearer resolves to its user, who may belong to other agents too.
    if user.is_session and (row.get("agent") or "") != (user.agent or ""):
        return False
    if user.is_admin:
        return True
    # A personal app whose owner lost the agent is dormant (APPS.md
    # "Lifecycle"): to the owner, a grantee and the render alike it is gone.
    if db_apps.personal_row_dormant(row):
        return False
    # The platform's own headless render sees the one app it was minted for
    # (auth/render_principal.py), whatever the row's scope.
    if user.render_app and user.render_app == row.get("id"):
        return True
    if row.get("username"):
        direct = (row.get("owner_sub") or "") == user.sub
    else:
        direct = user.can_access_agent(row.get("agent") or "")
    if not direct and not _granted(row, user):
        return False
    return _scope_access(row, user)


def _granted(row: dict, user: UserContext) -> bool:
    """A live share admits the viewer: their own person share, or an agent
    or department placement on an agent they hold (SHARING.md)."""
    from storage.sharing import share_store
    app_id = row.get("id") or ""
    if share_store.internal_grant("app", app_id, user.sub) is not None:
        return True
    return bool(share_store.placements_for_user(app_id, user.sub, list(user.agents)))


def _scope_access(row: dict, user: UserContext) -> bool:
    """A Dock pin follows its chat's access rule: the chat itself, or for a
    project pin any lane of the project the viewer may open. Without this a
    dashboard pinned in a private chat would serve to any member holding
    its id (the Dock route checked the anchor chat; the app routes did
    not)."""
    chat_id = row.get("scope_chat_id") or ""
    project_id = row.get("scope_project_id") or ""
    if not chat_id and not project_id:
        return True
    from core.session.visibility import can_access_chat
    if chat_id:
        chat = task_store.get_chat(chat_id)
        return bool(chat) and can_access_chat(user, cast("dict", chat))
    return any(can_access_chat(user, c)
               for c in task_store.list_chats_by_project(project_id))


async def announce_deploy(row: dict, release: int, *, file_updated: bool = True) -> int:
    """Tell every screen that may see the row that a new release serves:
    ``app_deployed`` on the live queue (the frame reloads once its row
    carries this manifest signature) and, for dashboards built before the
    frame existed, the working file's ``file_updated``."""
    from services.notifications import notification_manager
    if file_updated:
        await notification_manager.broadcast_file_updated(
            row["agent"], row["rel_path"], source="disk",
        )
    frame = {"type": wire.APP_DEPLOYED, "app_id": row["id"], "release": release,
             "actions_sig": task_store.manifest_sig(row)}
    subs = await asyncio.to_thread(app_audience, row)
    return sum(notification_manager.push_live(sub, frame) for sub in subs)
