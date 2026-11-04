"""The department actions of the cards catalog (services/cards/actions.py
holds the shape and ``move_agent``; api/cards/cards.py registers these):
create a department, set its delegation mode and reach. Each calls the
departments route's own function with the clicker's principal, so the
click takes the route's rule (``require_creator``, the creator who made
the department or an admin) and its sentences; the draw-time check spells
the rule for the agent on the session's bearer, which those helpers refuse.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from api.departments.departments import (
    NAME_TOO_LONG,
    CreateDepartmentRequest,
    UpdateDepartmentRequest,
    create_department,
    update_department,
)
from auth import roles
from services.cards import status as st
from services.cards.actions import (
    Action,
    Outcome,
    db_departments_mode_words,
    quoted,
    sentence,
)
from storage.agents import db_departments

CREATORS_ONLY = "Only admins and creators create departments."
NAME_TAKEN = "A department named {name} already exists."
NO_SUCH_DEPARTMENT = "There is no such department."
CREATOR_OR_ADMIN = "Only admins or the creator of this department may modify it."
NOTHING_TO_SET = "Name a mode or a reach to set."

REACH_WORDS = {"adjacent": "the next level", "subtree": "every level below"}


def _reach_words(reach: str) -> str:
    return REACH_WORDS.get(reach, reach)


def _levels(params: dict[str, Any]) -> list[str]:
    return [str(lv).strip() for lv in params.get("levels") or [] if str(lv).strip()] or ["Head", "Team"]


async def _create_words(params: dict[str, Any], u) -> dict[str, str]:
    name = str(params["name"]).strip()
    mode = str(params.get("mode") or db_departments.DEFAULT_MODE)
    reach = str(params.get("reach") or "adjacent")
    text = (f"Creates the department {quoted(name)} with the levels {', '.join(_levels(params))}. "
            f"Delegation runs {db_departments_mode_words(mode)} and reaches {_reach_words(reach)}.")
    return {"title": f"Create the department {quoted(name)}", "text": text, "label": "Create"}


async def _create_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None or not roles.is_creator_or_above(u.role):
        return CREATORS_ONLY
    name = str(params["name"]).strip()
    if not name:
        return "Department name is required."
    if len(name) > 100:
        return NAME_TOO_LONG
    if any(d.get("name", "").strip().lower() == name.lower()
           for d in await asyncio.to_thread(db_departments.list_departments)):
        return NAME_TAKEN.format(name=quoted(name))
    return None


async def _create_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    refusal = await _create_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    req = CreateDepartmentRequest(
        name=str(params["name"]).strip(), mode=str(params.get("mode") or db_departments.DEFAULT_MODE),
        reach=str(params.get("reach") or "adjacent"), levels=_levels(params))
    try:
        dept = await create_department(req, user=u)
    except HTTPException as exc:
        return Outcome(st.OUTCOME_FAILED, sentence(str(exc.detail)))
    return Outcome(st.OUTCOME_DONE,
                   f"The department {quoted(dept['name'])} exists. Place agents in it with a card, or from the Agents page.",
                   extra={"department_id": dept["id"]})


CREATE_DEPARTMENT = Action(
    name="create_department",
    schema={"type": "object", "required": ["name"], "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 100},
                           "levels": {"type": "array", "minItems": 1, "maxItems": 8,
                                      "items": {"type": "string", "minLength": 1, "maxLength": 60}},
                           "mode": {"type": "string", "enum": list(db_departments.MODES)},
                           "reach": {"type": "string", "enum": list(db_departments.REACHES)}}},
    words=_create_words, check=_create_check, run=_create_run,
)


def _may_set(u, dept: dict) -> bool:
    # The route's rule (assignment.can_edit_department) refuses a session
    # bearer outright; the draw spells it on the person the bearer names.
    return bool(u.is_admin or (roles.is_creator_or_above(u.role) and dept.get("created_by_sub")
                               and dept["created_by_sub"] == (u.acting_sub or "")))


async def _set_words(params: dict[str, Any], u) -> dict[str, str]:
    dept = await asyncio.to_thread(db_departments.get_department, str(params["department_id"])) or {}
    name = quoted(dept["name"]) if dept.get("name") else "the department"
    mode = str(params.get("mode") or dept.get("mode") or db_departments.DEFAULT_MODE)
    reach = str(params.get("reach") or dept.get("reach") or "adjacent")
    return {"title": f"Set the delegation of {name}",
            "text": f"Delegation in {name} runs {db_departments_mode_words(mode)} and reaches {_reach_words(reach)}.",
            "label": "Set"}


async def _set_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None:
        return CREATOR_OR_ADMIN
    if not params.get("mode") and not params.get("reach"):
        return NOTHING_TO_SET
    dept = await asyncio.to_thread(db_departments.get_department, str(params["department_id"]))
    if not dept:
        return NO_SUCH_DEPARTMENT
    if not _may_set(u, dept):
        return CREATOR_OR_ADMIN
    return None


async def _set_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    refusal = await _set_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    req = UpdateDepartmentRequest(mode=params.get("mode"), reach=params.get("reach"))
    try:
        dept = await update_department(str(params["department_id"]), req, user=u)
    except HTTPException as exc:
        return Outcome(st.OUTCOME_FAILED, sentence(str(exc.detail)))
    return Outcome(st.OUTCOME_DONE,
                   f"Delegation in {quoted(dept['name'])} now runs {db_departments_mode_words(dept['mode'])} "
                   f"and reaches {_reach_words(dept['reach'])}.")


SET_DEPARTMENT = Action(
    name="set_department",
    schema={"type": "object", "required": ["department_id"], "additionalProperties": False,
            "properties": {"department_id": {"type": "string", "minLength": 1, "maxLength": 40},
                           "mode": {"type": "string", "enum": list(db_departments.MODES)},
                           "reach": {"type": "string", "enum": list(db_departments.REACHES)}}},
    words=_set_words, check=_set_check, run=_set_run,
)


ACTIONS: tuple[Action, ...] = (CREATE_DEPARTMENT, SET_DEPARTMENT)
