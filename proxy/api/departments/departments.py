"""Departments REST API — org structure behind the 3D agents map.

Permission model (locked):
- Admins: create/edit/delete ALL departments + levels.
- Creators: create; edit/delete ONLY their own (created_by_sub — compared
  against ``acting_sub``, never ``sub``, so an agent-scope session can't
  edit departments as its owning user). Admin-created departments are
  read-only to them.
- Managers (an AGENT role) get NO department powers: assigning an agent to
  a department auto-wires delegation edges into other teams — a capability
  grant, so platform-role territory. Agent ASSIGNMENT itself goes through
  PATCH /v1/agents/{name} (department_id/department_level_id) with its own
  conjunction gate there.

Visibility is membership-scoped: non-admins see a department only when they
can access at least one member agent (or created it themselves); a
department where they have no agent is entirely invisible. Inside a visible
department, member agents they cannot access are flagged ``accessible:
false`` (the map renders them grayed, info-popup only, never enterable).

Every edge-affecting write ends with a global edge-compiler recompile.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    require_creator,
)
from services.departments import edge_compiler
from storage import agent_store, db_departments

logger = logging.getLogger("claude-proxy.departments")

router = APIRouter()


class CreateDepartmentRequest(BaseModel):
    name: str
    auto_delegation: bool = True
    reach: str = "adjacent"  # 'adjacent' | 'subtree'
    levels: list[str] | None = None  # level names, rank = list order


class UpdateDepartmentRequest(BaseModel):
    name: str | None = None
    auto_delegation: bool | None = None
    reach: str | None = None
    position_hint: str | None = None


class LevelItem(BaseModel):
    id: str = ""  # existing level id, or "" for a new level
    name: str


class SetLevelsRequest(BaseModel):
    levels: list[LevelItem]


def _can_edit_department(u: UserContext, dept: dict) -> bool:
    if u.is_admin:
        return True
    if u.role != "creator" or getattr(u, "is_api_key", False):
        return False
    owner = dept.get("created_by_sub") or ""
    return bool(owner) and owner == (u.acting_sub or "")


def _require_dept_edit(u: UserContext, dept: dict) -> None:
    if not _can_edit_department(u, dept):
        raise HTTPException(
            403,
            "Only admins or the creator of this department may modify it",
        )


def _members_by_department() -> dict[str, list[dict]]:
    members: dict[str, list[dict]] = {}
    for a in agent_store.get_all_agents():
        dept_id = a.get("department_id") or ""
        if not dept_id:
            continue
        members.setdefault(dept_id, []).append({
            "name": a["slug"],
            "display_name": a.get("display_name") or a["slug"],
            "color": a.get("color", "") or "",
            "description": a.get("description", "") or "",
            "level_id": a.get("department_level_id") or "",
        })
    return members


def _serialize(u: UserContext, dept: dict, members: list[dict]) -> dict:
    return {
        "id": dept["id"],
        "name": dept["name"],
        "created_by_sub": dept.get("created_by_sub", ""),
        "auto_delegation": dept["auto_delegation"],
        "reach": dept["reach"],
        "position_hint": dept.get("position_hint", ""),
        "levels": dept["levels"],
        "members": [
            {**m, "accessible": u.can_access_agent(m["name"])} for m in members
        ],
        "can_edit": _can_edit_department(u, dept),
    }


@router.get("/v1/departments")
async def list_departments(user: UserContext | None = Depends(get_current_user)):
    """Viewer-scoped department list with levels + member agents."""
    u = require_auth(user)
    depts = await asyncio.to_thread(db_departments.list_departments)
    members = await asyncio.to_thread(_members_by_department)

    out = []
    for dept in depts:
        dept_members = members.get(dept["id"], [])
        if not u.is_admin:
            is_member = any(
                u.can_access_agent(m["name"]) for m in dept_members
            )
            created_it = bool(dept.get("created_by_sub")) and \
                dept["created_by_sub"] == (u.acting_sub or "")
            if not is_member and not created_it:
                continue
        out.append(_serialize(u, dept, dept_members))
    return {"departments": out}


@router.post("/v1/departments")
async def create_department(
    req: CreateDepartmentRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a department (admins + creators)."""
    u = require_creator(user)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Department name is required")
    if len(name) > 100:
        raise HTTPException(400, "Department name too long (max 100)")
    try:
        dept = await asyncio.to_thread(
            db_departments.create_department,
            name,
            u.acting_sub or "",
            auto_delegation=req.auto_delegation,
            reach=req.reach,
            level_names=req.levels,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info("department created: %s (%s) by %s", dept["id"], name, u.sub)
    return _serialize(u, dept, [])


@router.patch("/v1/departments/{department_id}")
async def update_department(
    department_id: str,
    req: UpdateDepartmentRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Edit a department (admin or its creator)."""
    u = require_auth(user)
    dept = await asyncio.to_thread(db_departments.get_department, department_id)
    if not dept:
        raise HTTPException(404, "Department not found")
    _require_dept_edit(u, dept)
    if req.name is not None and not req.name.strip():
        raise HTTPException(400, "Department name cannot be empty")
    # Same bounds as create — PATCH must not grow rows/map payloads unbounded.
    if req.name is not None and len(req.name.strip()) > 100:
        raise HTTPException(400, "Department name too long (max 100 chars)")
    if req.position_hint is not None and len(str(req.position_hint)) > 200:
        raise HTTPException(400, "position_hint too long (max 200 chars)")
    try:
        updated = await asyncio.to_thread(
            db_departments.update_department,
            department_id,
            name=req.name.strip() if req.name is not None else None,
            auto_delegation=req.auto_delegation,
            reach=req.reach,
            position_hint=req.position_hint,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # auto_delegation / reach change what the department compiles to.
    if req.auto_delegation is not None or req.reach is not None:
        await asyncio.to_thread(edge_compiler.recompile)
    members = (await asyncio.to_thread(_members_by_department)).get(
        department_id, []
    )
    return _serialize(u, updated, members)


@router.delete("/v1/departments/{department_id}")
async def delete_department(
    department_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a department (admin or its creator); members drop to unassigned."""
    u = require_auth(user)
    dept = await asyncio.to_thread(db_departments.get_department, department_id)
    if not dept:
        raise HTTPException(404, "Department not found")
    _require_dept_edit(u, dept)
    members = await asyncio.to_thread(
        db_departments.delete_department, department_id
    )
    await asyncio.to_thread(edge_compiler.recompile)
    logger.info(
        "department deleted: %s (%s) by %s — %d member(s) unassigned",
        department_id, dept["name"], u.sub, len(members),
    )
    return {"status": "deleted", "unassigned_agents": members}


@router.put("/v1/departments/{department_id}/levels")
async def set_department_levels(
    department_id: str,
    req: SetLevelsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Replace a department's levels (admin or its creator).

    Agents on a removed level drop out of the department; their slugs are
    returned as ``unassigned_agents`` so the UI can surface it."""
    u = require_auth(user)
    dept = await asyncio.to_thread(db_departments.get_department, department_id)
    if not dept:
        raise HTTPException(404, "Department not found")
    _require_dept_edit(u, dept)
    try:
        levels, unassigned = await asyncio.to_thread(
            db_departments.set_department_levels,
            department_id,
            [item.model_dump() for item in req.levels],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(edge_compiler.recompile)
    return {"levels": levels, "unassigned_agents": unassigned}
