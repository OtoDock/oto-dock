"""Placing an agent in a department (the agent PATCH route's rule): the one
definition of who may, and what a valid placement is. The agent PATCH
route, the move card and the set installer all read it.

Assigning wires delegation edges into other teams, so it is platform-role
territory: an admin, or a creator who manages the agent, acting as a
person (``acting_sub``): the master key and a no-user session never.
"""

from __future__ import annotations

import asyncio

from auth import roles
from services.departments import edge_compiler
from storage.agents import agent_store, db_departments

ONLY_ADMINS_AND_CREATORS = "Only admins and creators can change an agent's department."
MANAGER_REQUIRED = "Manager or admin access required for this agent"
BOTH_OR_NEITHER = ("department_id and department_level_id must be set together "
                   "(or both empty to clear the assignment).")
NO_SUCH_DEPARTMENT = "Department not found"
LEVEL_NOT_IN_DEPARTMENT = "Level does not belong to that department"


class AssignmentRefused(Exception):
    """A placement the rules refuse: the status and the sentence a route
    answers with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def can_edit_department(u, dept: dict) -> bool:
    """An admin, or the creator whose ``acting_sub`` made the department
    (never ``sub``: an agent-scope session must not edit as its owner)."""
    if u.is_admin:
        return True
    if u.role != roles.CREATOR or getattr(u, "is_api_key", False):
        return False
    owner = dept.get("created_by_sub") or ""
    return bool(owner) and owner == (u.acting_sub or "")


def require_assigner(u) -> None:
    """The role half of the rule: a person who is a creator or above."""
    if u.acting_sub is None or not roles.is_creator_or_above(u.role):
        raise AssignmentRefused(403, ONLY_ADMINS_AND_CREATORS)


def validate(agent: str, department_id: str | None, level_id: str | None, u, *,
             manages: bool = False) -> dict | None:
    """The placement ``(department_id, level_id)`` of ``agent`` by ``u``:
    the role, the manager tier on the agent (``manages`` when the caller
    proved it: an agent the person installed a moment ago, before its
    principal was read again), the pair set together or cleared together,
    the department and its level. Returns the department row, or None for
    a clearing."""
    require_assigner(u)
    if not manages and not u.can_manage_agent(agent):
        raise AssignmentRefused(403, MANAGER_REQUIRED)
    dept = (department_id or "").strip()
    level = (level_id or "").strip()
    if bool(dept) != bool(level):
        raise AssignmentRefused(400, BOTH_OR_NEITHER)
    if not dept:
        return None
    row = db_departments.get_department(dept)
    if not row:
        raise AssignmentRefused(400, NO_SUCH_DEPARTMENT)
    if level not in {lv["id"] for lv in row["levels"]}:
        raise AssignmentRefused(400, LEVEL_NOT_IN_DEPARTMENT)
    return row


async def assign(agent: str, department_id: str, level_id: str, u, *, manages: bool = False) -> dict:
    """Place ``agent`` (or clear its placement with two empty values) and
    recompile the edges. Returns the agent's row."""
    if not agent_store.agent_exists(agent):
        raise AssignmentRefused(404, "Agent not found")
    await asyncio.to_thread(validate, agent, department_id, level_id, u, manages=manages)
    row = await asyncio.to_thread(
        agent_store.update_agent, agent,
        department_id=(department_id or "").strip(), department_level_id=(level_id or "").strip())
    await asyncio.to_thread(edge_compiler.recompile)
    return row or {}
