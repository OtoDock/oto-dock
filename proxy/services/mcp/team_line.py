"""The team line of the delegation block (dynamic_context.py): how many
people the agent serves and in which roles, the person in the session among
them, names only where the dashboard would show them to that person.

``data`` runs off the loop (the store reads) and ``render`` is pure, like the
department line's pair. Best effort both ways: a failure logs and the prompt
goes on without the line.
"""

from __future__ import annotations

import logging
from typing import Any

from auth import roles
from storage import db_settings
from storage.identity import db_users
from storage.sharing.share_store import SETTING_DIRECTORY

logger = logging.getLogger("otodock")

# Past this many people the line keeps to the counts.
NAMES_CAP = 12
NAME_CAP = 40
_PLURAL = {roles.MANAGER: "managers", roles.EDITOR: "editors",
           roles.CONTRIBUTOR: "contributors", roles.VIEWER: "viewers"}


def data(agent: str) -> dict[str, Any]:
    """The agent's assignments and whether the user directory is open to
    members (the sharing routes' setting, read through the store)."""
    try:
        rows = db_users.get_agent_users_with_profile(agent)
        people = [{"sub": r["sub"], "role": r["agent_role"],
                   "name": _clean(r.get("display_name") or r.get("name") or r.get("username") or "")}
                  for r in rows]
        directory_open = db_settings.get_platform_setting(SETTING_DIRECTORY) != "0"
    except Exception:
        logger.exception("team line: the assignments of %s were not read", agent)
        return {}
    return {"people": people, "directory_open": directory_open}


def _clean(name: str) -> str:
    return " ".join(str(name).split())[:NAME_CAP]


def _count(n: int, role: str) -> str:
    return f"{n} {role if n == 1 else _PLURAL[role]}"


def _joined(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def render(team: dict[str, Any] | None, user_sub: str, user_role: str) -> str:
    """One line, or "" when nothing was read. The counts always; the names
    when a platform admin reads, or a person of editor role or above while
    the directory is open to members; the person's own role last. A session
    with no person ends after the counts."""
    if not team:
        return ""
    people = team.get("people") or []
    by_role: dict[str, list[str]] = {r: [] for r in _PLURAL}
    for p in people:
        by_role.setdefault(p["role"], []).append(p["name"] or "a person")
    counts = [_count(len(names), role) for role, names in by_role.items() if names and role in _PLURAL]
    person = bool(user_sub)
    may_name = person and (roles.is_admin(user_role)
                           or (roles.can_edit(user_role) and bool(team.get("directory_open"))))
    if not people:
        line = "No person is assigned to this agent yet."
    else:
        n = len(people)
        line = f"This agent serves {n} {'person' if n == 1 else 'people'}: {_joined(counts)}."
        if may_name and n <= NAMES_CAP:
            line += "".join(
                f" The {role} is {names[0]}." if len(names) == 1
                else f" The {_PLURAL[role]} are {_joined(names)}."
                for role, names in by_role.items() if names and role in _PLURAL)
    if person:
        who = "a platform admin" if roles.is_admin(user_role) else f"{_article(user_role)} {user_role}"
        line += f" You are talking with {who}."
    return line


def _article(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"
