"""Who reads whom in the Notebook.

An agent reads its own entries and those of every agent one delegation edge
away, in either direction, compiled and manual alike: a department's mode
decides who shares with whom through the edges it compiles (``down``: the
head and each member, siblings apart; ``down_across`` and ``both``: the
siblings too; ``off``: nothing). Who drives the session does not narrow
it: an entry is written for the agents one hop away and the people who use
them (the one exception to "an edge never widens a chat session"). An
admin-only agent stays out of every read set but another admin-only
agent's, since its sessions see the whole platform.
"""

from __future__ import annotations

from typing import Any

from storage.agents import agent_store
from storage.notebook import db_notebook


def read_set(agent: str) -> list[str]:
    """The agent first, then its neighbours along the delegation edges,
    sorted. An agent that does not exist reads nothing."""
    if agent_store.get_agent(agent) is None:
        return []
    own_admin_only = agent_store.is_admin_only(agent)
    neighbours = [
        row["slug"] for row in db_notebook.neighbours(agent)
        if own_admin_only or not row["admin_only"]
    ]
    return [agent, *neighbours]


def department_members(department_id: str, user: Any) -> list[dict]:
    """The department's members this person can open, head level first
    (``slug``, ``display_name``). The union on the department page is their
    entries whatever the mode: the mode decides what agents read, the page
    shows a person what they could read agent by agent."""
    return [
        {"slug": m["slug"], "display_name": m["display_name"] or m["slug"]}
        for m in db_notebook.department_members(department_id)
        if user.can_access_agent(m["slug"])
    ]


def department_visible(user: Any, department: dict, members: list[str]) -> bool:
    """The rule of ``GET /v1/departments``: an admin sees every department,
    anyone else one with a member they can open or one they created."""
    if user.is_admin:
        return True
    if members:
        return True
    owner = department.get("created_by_sub") or ""
    return bool(owner) and owner == (user.acting_sub or "")
