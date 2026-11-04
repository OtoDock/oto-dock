"""The department sets of the community catalog (services/community/department_set_installer.py):
the list the Browse page shows, the preview a dialog renders, the install
as a job, and the job's state. Creators and admins, as the catalog's
Browse page; the install takes a person at the dashboard only (its
consent approves apps).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api._guards import require_creator_or_admin
from auth.providers import UserContext, get_current_user, require_human, require_user
from services.community import (
    community_agents_catalog,
    community_catalog,
    community_department_sets as sets,
    department_set_installer as installer,
)
from services.departments import assignment
from storage.agents import db_set_installs

logger = logging.getLogger("otodock.department-sets-api")
router = APIRouter(dependencies=[Depends(require_user)])

_CREATOR_DETAIL = "Admin or creator only"


async def _registry(route: str) -> dict:
    try:
        return await community_agents_catalog.fetch_registry()
    except Exception as exc:
        logger.warning("%s: the community agents catalog is unreachable: %s", route, exc)
        raise HTTPException(
            503, "The community agents catalog could not be reached. Try again in a minute.") from exc


def _set_or_404(registry: dict, slug: str) -> tuple[dict, sets.DepartmentSet]:
    entry = sets.find_set(registry, slug)
    if entry is None:
        raise HTTPException(404, f"Department set '{slug}' not in catalog")
    dset, problems = sets.read_entry(entry)
    if dset is None:
        raise HTTPException(409, "This set cannot be installed. " + " ".join(p.rstrip(".") + "." for p in problems))
    missing = sets.missing_members(dset, registry)
    if missing:
        raise HTTPException(409, "A template of this set is not in the catalog: " + ", ".join(missing) + ".")
    return entry, dset


def _entry_view(registry: dict, entry: dict, u: UserContext) -> dict:
    dset, problems = sets.read_entry(entry)
    members = []
    for m in entry.get("members") or []:
        if not isinstance(m, dict):
            continue
        tpl = sets.template_entry(registry, str(m.get("template") or "")) or {}
        members.append({"template": str(m.get("template") or ""), "level": str(m.get("level") or ""),
                        "display_name": str(tpl.get("display_name") or m.get("template") or ""),
                        "color": str(tpl.get("color") or ""), "in_catalog": bool(tpl)})
    out = {**entry, "members": members, "problems": problems,
           "missing_members": sets.missing_members(dset, registry) if dset else [],
           "platform_compat_ok": community_catalog.platform_version_ok(entry.get("platform_min_version")),
           "installed": installer.departments_from(dset, u) if dset else []}
    return out


@router.get("/v1/community/departments")
async def list_department_sets(user: UserContext = Depends(get_current_user)) -> dict:
    """Every department set the catalog serves, with the departments this
    person may see that came from each."""
    u = require_creator_or_admin(user, detail=_CREATOR_DETAIL)
    registry = await _registry("GET /v1/community/departments")
    entries = sets.sets_in(registry)
    views = await asyncio.to_thread(lambda: [_entry_view(registry, e, u) for e in entries])
    return {"registry_version": registry.get("registry_version"), "updated_at": registry.get("updated_at"),
            "fetched_from": community_agents_catalog.REGISTRY_RAW_URL, "departments": views}


@router.get("/v1/community/departments/{slug}/preview")
async def preview_department_set(
    slug: str, department_name: str = "", new: str = "",
    user: UserContext = Depends(get_current_user),
) -> dict:
    """What installing the set would do for this person: each member
    installed new or adopted (``new`` names templates to install new
    instead of adopting, comma separated), the apps and checks the consent
    covers, the library, the schedules, the name's problem if any, a job
    already running."""
    u = require_creator_or_admin(user, detail=_CREATOR_DETAIL)
    registry = await _registry("GET /v1/community/departments/{slug}/preview")
    entry, dset = _set_or_404(registry, slug)
    name = (department_name or dset.name).strip()
    choices = {t.strip(): installer.ACTION_NEW for t in new.split(",") if t.strip()}
    plan = await asyncio.to_thread(installer.plan_members, dset, registry, u, choices, name)
    members = []
    for row in plan:
        tpl = sets.template_entry(registry, row["template"]) or {}
        apps, checks = sets.consent_preview(tpl)
        mcps = await community_agents_catalog.mcp_plan(tpl, row["slug"], u.is_admin)
        members.append({**row, "apps": apps, "checks": checks, "required_mcps": mcps,
                        "blocked_mcps": [m["name"] for m in mcps if m.get("blocked")]})
    running = await asyncio.to_thread(installer.running_job, dset.slug)
    return {
        "set_slug": dset.slug, "department_name": name,
        "name_problem": installer.department_name_problem(name),
        "mode": dset.mode, "reach": dset.reach, "levels": dset.levels,
        "members": members,
        "library": ({"source": dset.library.source, "subdir": dset.library.subdir, "writable": dset.library.writable}
                    if dset.library else None),
        "schedules": [{"member": s.member, "task": s.task} for s in dset.schedules],
        "consent_scope": "everyone" if u.is_admin else "own",
        "existing": await asyncio.to_thread(installer.departments_from, dset, u),
        "running_job": running["id"] if running else None,
    }


class InstallSetBody(BaseModel):
    department_name: str = ""
    # The dialog's one checkbox: approve every member's apps and checks
    # with the signatures the registry carries.
    consent: bool = False
    # Members to install new instead of adopting an installed agent.
    new: list[str] = []


@router.post("/v1/community/departments/{slug}/install", status_code=202)
async def install_department_set(
    slug: str, body: InstallSetBody, user: UserContext = Depends(get_current_user),
) -> dict:
    """Start the install: the job's row, polled at its route."""
    u = require_creator_or_admin(require_human(user), detail=_CREATOR_DETAIL)
    registry = await _registry("POST /v1/community/departments/{slug}/install")
    entry, dset = _set_or_404(registry, slug)
    community_catalog.require_platform_compat(dset.slug, entry.get("platform_min_version"))
    name = (body.department_name or dset.name).strip()
    try:
        job = await installer.start(dset, registry, name, u, consent=body.consent,
                                    choices={t: installer.ACTION_NEW for t in body.new})
    except (installer.SetInstallRefused, assignment.AssignmentRefused) as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"job_id": job["id"], "job": job}


@router.get("/v1/community/departments/installs/{job_id}")
async def department_set_install_status(job_id: str, user: UserContext = Depends(get_current_user)) -> dict:
    """The job's row: its starter's or an admin's."""
    u = require_creator_or_admin(user, detail=_CREATOR_DETAIL)
    job = await asyncio.to_thread(db_set_installs.get, job_id)
    if job is None:
        raise HTTPException(404, "No such install")
    if not u.is_admin and job["started_by"] != (u.acting_sub or ""):
        raise HTTPException(403, "This install is another person's.")
    return job
