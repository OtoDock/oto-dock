"""Placing an agent in a department (the agent PATCH route's rule): the one
definition of who may, and what a valid placement is. The agent PATCH
route, the move card, the install card and the set installer all read it.

Assigning wires delegation edges into other teams and, through a place the
department's set left empty, decides what its apps read
(services/apps/binding_target.py), so it takes three halves: the role (an
admin or a creator, acting as a person: the master key and a no-user
session never), the agent (its manager), and the department (an admin, its
creator, or a creator who manages every agent in it now). Clearing a
placement takes the first two.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from auth import roles
from services.departments import edge_compiler
from storage.agents import agent_store, db_departments
from storage.pg import run_db

ONLY_ADMINS_AND_CREATORS = "Only admins and creators can change an agent's department."
MANAGER_REQUIRED = "Manager or admin access required for this agent"
BOTH_OR_NEITHER = ("department_id and department_level_id must be set together "
                   "(or both empty to clear the assignment).")
NO_SUCH_DEPARTMENT = "Department not found"
LEVEL_NOT_IN_DEPARTMENT = "Level does not belong to that department"
PLACE_REFUSED = ("Only an admin, this department's creator, or someone who manages all its agents "
                 "can add an agent to it.")


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


def can_see_department(u, dept: dict, members: Iterable[str]) -> bool:
    """Whether a person sees a department at all: an admin every one,
    anyone else one they created (``acting_sub``) or one that holds an agent
    they can access (``members``: the department's agent slugs). A
    department they may not see is never named to them."""
    if u.is_admin:
        return True
    owner = dept.get("created_by_sub") or ""
    if owner and owner == (u.acting_sub or ""):
        return True
    return any(u.can_access_agent(s) for s in members)


def require_assigner(u) -> None:
    """The role half of the rule: a person who is a creator or above."""
    if not _assigner(u):
        raise AssignmentRefused(403, ONLY_ADMINS_AND_CREATORS)


def _assigner(u) -> bool:
    # A person's own key reaches no placing route today: refused here too.
    return u.acting_sub is not None and roles.is_creator_or_above(u.role) and not getattr(u, "api_key_id", "")


def created_department(u, dept: dict) -> bool:
    """An admin, or the person who created the department with a creator's
    role: read on ``acting_sub``, so a session that names that person counts
    (its placement calls ask the person each time), unlike
    ``can_edit_department``, which refuses every session."""
    return bool(u.is_admin or (roles.is_creator_or_above(u.role) and dept.get("created_by_sub")
                               and dept["created_by_sub"] == (u.acting_sub or "")))


def may_place_into(u, dept: dict, members: list[str] | None = None) -> bool:
    """Whether ``u`` may place an agent in ``dept`` (the role and the
    department halves; the agent half is ``validate``'s): an admin, the
    department's creator, or a creator who manages every agent in it now
    (``members``, the slugs whose ``department_id`` is the department, read
    when not given). A department with no agent: only the first two. The
    department list answers ``can_place`` with this, so the dashboard holds
    no copy of the rule."""
    if not _assigner(u):
        return False
    if created_department(u, dept):
        return True
    slugs = db_departments.member_slugs(dept["id"]) if members is None else list(members)
    return bool(slugs) and all(u.can_manage_agent(s) for s in slugs)


def place_refusal(u, dept: dict, members: list[str] | None = None) -> str:
    """The sentence a placement into ``dept`` by ``u`` meets, '' when it
    may (the department list carries it): the role half's own sentence
    first."""
    if not _assigner(u):
        return ONLY_ADMINS_AND_CREATORS
    return "" if may_place_into(u, dept, members) else PLACE_REFUSED


def validate(agent: str, department_id: str | None, level_id: str | None, u, *,
             manages: bool = False) -> dict | None:
    """The placement ``(department_id, level_id)`` of ``agent`` by ``u``:
    the role, the manager tier on the agent (``manages`` when the caller
    proved it: an agent the person installed a moment ago, before its
    principal was read again; it skips that half alone), the pair set
    together or cleared together, the department and its level, and the
    department half (``may_place_into``) for a placement into one, a move
    to another level of the same department included. Returns the
    department row, or None for a clearing."""
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
    if not may_place_into(u, row):
        raise AssignmentRefused(403, PLACE_REFUSED)
    return row


async def assign(agent: str, department_id: str, level_id: str, u, *, manages: bool = False) -> dict:
    """Place ``agent`` (or clear its placement with two empty values) and
    recompile the edges. Returns the agent's row."""
    if not await run_db(agent_store.agent_exists, agent):
        raise AssignmentRefused(404, "Agent not found")
    await asyncio.to_thread(validate, agent, department_id, level_id, u, manages=manages)
    row = await asyncio.to_thread(
        agent_store.update_agent, agent,
        department_id=(department_id or "").strip(), department_level_id=(level_id or "").strip())
    await asyncio.to_thread(edge_compiler.recompile)
    return row or {}
