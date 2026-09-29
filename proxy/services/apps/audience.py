"""Who receives an app's live frames (APPS.md "Live apps").

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
    # Internal grantees who have not hidden the app (SHARING.md); a grantee
    # of a Dock pin still has to pass its chat below.
    from storage.sharing import share_store
    for sub in share_store.internal_grantees(row.get("id") or ""):
        candidates.add(sub)
        members.setdefault(sub, "grantee")
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
        if any(_may_open_chat(sub, sub in admins, sub in members, c) for c in chats)
    )


def _may_open_chat(sub: str, is_admin: bool, is_member: bool, chat: dict) -> bool:
    """``api.agents.chats.can_access_chat`` for a user known by sub and
    membership alone, so the fan-out builds no principal per candidate."""
    from api.agents.chats import _run_for_chat
    from core.session.visibility import is_shared_chat_owner, is_task_chat_owner
    if is_admin:
        return True
    owner = chat.get("user_sub", "") or ""
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
