"""Viewer focus in the agent's turn (APPS.md "Live apps").

The dashboard tells the proxy what each tab shows; a turn that starts while
an app is on that screen carries one line naming it, so "update this" needs
no explanation. The line is built here, once per turn, on the DB executor:
nothing is read unless the focused surface is an app, and the user's own
setting can turn it off.
"""

from __future__ import annotations

import re
import time

from services.notifications import notification_manager
from storage import database as task_store
from storage.prefs import user_ui_prefs_store

PREF_SHARE_FOCUS = "share_focus_with_agents"
PREF_AGENTS_MAY_OPEN = "agents_may_open_apps"

# Matches the line at the start of a prompt; the same shape as the time
# prelude so every prelude matcher can accept both.
FOCUS_PRELUDE_RE = re.compile(
    r'^\[The user is looking at the app [^\]\n]{1,200}\][ \t]*(?:\r?\n+|$)'
)

_TITLE_MAX = 120
_PREFS_TTL_S = 30.0
_prefs_cache: dict[str, tuple[float, dict]] = {}


def user_pref_on(user_sub: str, key: str) -> bool:
    """A boolean ui pref with "absent means on" semantics."""
    now = time.monotonic()
    cached = _prefs_cache.get(user_sub)
    if cached and now - cached[0] < _PREFS_TTL_S:
        prefs = cached[1]
    else:
        prefs = user_ui_prefs_store.get_prefs(user_sub) or {}
        _prefs_cache[user_sub] = (now, prefs)
        if len(_prefs_cache) > 1024:
            for sub in [s for s, (t, _) in _prefs_cache.items() if now - t >= _PREFS_TTL_S]:
                _prefs_cache.pop(sub, None)
    return prefs.get(key, True) is not False


def invalidate_prefs(user_sub: str) -> None:
    _prefs_cache.pop(user_sub, None)


def format_focus_line(title: str, slug: str) -> str:
    """The prompt line. The title is flattened and cannot close the
    bracket; the slug (validated at pin time) is the handle an agent can
    trust."""
    kept = "".join(
        c for c in (title or "")
        if c not in "[]" and (ord(c) >= 32 or c in "\t\n\r")
    )
    clean = " ".join(kept.split())[:_TITLE_MAX].strip() or slug
    return f'[The user is looking at the app "{clean}" ({slug}) right now.]'


def focus_line(user_sub: str, *, connection_id: str | None = None) -> str:
    """The line for the next turn of ``user_sub``, or "". A chat turn passes
    the connection it came from; a spoken turn passes none and gets the
    newest focus across the user's screens."""
    if connection_id:
        focus = notification_manager.connection_focus(user_sub, connection_id)
    else:
        focus = notification_manager.user_focus(user_sub)
    if not focus or focus.get("surface") != "app":
        return ""
    if not user_pref_on(user_sub, PREF_SHARE_FOCUS):
        return ""
    row = task_store.get_app(focus.get("app_id") or "")
    if not row or row.get("hidden"):
        return ""
    from api.apps.apps import app_access
    from auth.providers import user_context_for_sub
    user = user_context_for_sub(user_sub)
    if user is None or not app_access(row, user):
        return ""
    return format_focus_line(row.get("title") or "", row.get("slug") or "")
