"""Installing a department set (the registry's departments key): the plan a person sees,
the job that installs or adopts every member, then creates or reuses the
department and places them all in one transaction, shares the head's
library and lists the paused schedules; and the offer's placement, when a
second member of a set is installed alone.

Members first, the department second: a member that fails leaves the
installed ones as standalone agents and no department, with a line saying
so. One job per set at a time (the ``set_installs`` row's insert is the
claim: a partial unique index on the running jobs); the job is that row, so
a card's outcome reads it and a restart marks the job it cut off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from fastapi import HTTPException

import config
from core import layout
from services.community import community_agent_installer, community_catalog
from services.community.community_department_sets import (
    DepartmentSet,
    consent_of,
    template_entry,
)
from services.departments import assignment, edge_compiler
from services.knowledge import library_rules
from storage.agents import agent_store, db_departments, db_set_installs
from storage.automation import db_tasks
from storage.knowledge import db_knowledge_libraries

logger = logging.getLogger("otodock.department-set-installer")

# A member's actions in the plan and its states in the job, spelled here
# alone (the dashboard mirrors the states as SET_MEMBER in lib/kinds/card.ts;
# the job's own states are db_set_installs', mirrored in
# lib/status/setInstall.ts).
ACTION_NEW = "new"
ACTION_ADOPT = "adopt"
MEMBER_PENDING = "pending"
MEMBER_INSTALLING = "installing"
MEMBER_INSTALLED = "installed"
MEMBER_ADOPTED = "adopted"
MEMBER_FAILED = "failed"
ALREADY_RUNNING = "This department set is being installed already. Wait for it to finish."
INTERRUPTED_LINE = ("The platform restarted while this department was being installed. "
                    "The agents it installed stand alone. Install the department again to place them.")
INTERRUPTED_PLACED_LINE = ("The platform restarted while this department was being installed. "
                           "The department stands with its agents placed. Install the department again "
                           "to share the library and check the schedules.")

_locks: dict[str, asyncio.Lock] = {}


class SetInstallRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _lock_for(set_slug: str) -> asyncio.Lock:
    lock = _locks.get(set_slug)
    if lock is None:
        lock = _locks[set_slug] = asyncio.Lock()
    return lock


def _names(slugs: Iterable[str]) -> str:
    """``a, b and c`` of display names."""
    names = [agent_store.get_agent(s) or {"display_name": s} for s in slugs]
    words = [str(a.get("display_name") or a.get("slug") or "") for a in names]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _managed(u, slug: str) -> bool:
    return bool(u.is_admin or u.can_manage_agent(slug))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def reusable_department(dset: DepartmentSet, department_name: str, u) -> dict | None:
    """The department this set made under ``department_name`` that a retry
    reuses: the person's own (an admin's: the newest of anyone's), one
    they may edit, whose levels still match the set's."""
    if not department_name:
        return None
    existing = db_departments.find_by_set(dset.slug, department_name.strip(),
                                          None if u.is_admin else (u.acting_sub or ""))
    if existing is None or not assignment.can_edit_department(u, existing):
        return None
    return existing if [lv["name"] for lv in existing["levels"]] == dset.levels else None


def plan_members(dset: DepartmentSet, registry: dict, u, choices: dict[str, str] | None = None,
                 department_name: str = "") -> list[dict]:
    """One row per member: ``new`` (installed under ``slug``, the
    template's when free else the suggestion) or ``adopt`` (an installed
    agent from the template that the department of this name holds
    already, else one that sits in no department and the person manages;
    the oldest when several; ``choices`` may ask for ``new`` instead), with
    the agents from the template that sit in other departments
    (``placed``) so the dialog can say so."""
    choices = choices or {}
    reused = reusable_department(dset, department_name, u)
    reused_id = reused["id"] if reused else ""
    by_template: dict[str, list[dict]] = {}
    for a in agent_store.get_all_agents():
        if a.get("community_template"):
            by_template.setdefault(str(a["community_template"]), []).append(a)
    out = []
    for m in dset.members:
        entry = template_entry(registry, m.template) or {}
        rows = by_template.get(m.template, [])
        in_department = sorted(
            (a for a in rows if reused_id and a.get("department_id") == reused_id and _managed(u, a["slug"])),
            key=lambda a: str(a.get("created_at") or ""))
        standalone = in_department or sorted(
            (a for a in rows if not a.get("department_id") and _managed(u, a["slug"])),
            key=lambda a: str(a.get("created_at") or ""))
        placed = [a["slug"] for a in rows if a.get("department_id") and a.get("department_id") != reused_id]
        row = {
            "template": m.template, "level": m.level,
            "display_name": str(entry.get("display_name") or m.template),
            "version": str(entry.get("version") or ""),
            "platform_compat_ok": community_catalog.platform_version_ok(entry.get("platform_min_version")),
            "in_catalog": bool(entry),
            "placed": placed,
            "standalone": [a["slug"] for a in standalone],
        }
        if standalone and choices.get(m.template) != ACTION_NEW:
            row.update(action=ACTION_ADOPT, slug=standalone[0]["slug"])
        else:
            free = (agent_store.reserved_slug_refusal(m.template) is None
                    and not agent_store.agent_exists(m.template))
            target = m.template if free else community_agent_installer._propose_free_slug(m.template)
            row.update(action=ACTION_NEW, slug=target)
        out.append(row)
    return out


def departments_from(dset: DepartmentSet, u) -> list[dict]:
    """The departments this set made, as the person may see them (an
    admin: all; else the ones they created or hold a member of)."""
    out = []
    members: dict[str, list[str]] = {}
    for a in agent_store.get_all_agents():
        if a.get("department_id"):
            members.setdefault(a["department_id"], []).append(a["slug"])
    for d in db_departments.list_departments():
        if d.get("community_set") != dset.slug:
            continue
        slugs = members.get(d["id"], [])
        if not u.is_admin and d.get("created_by_sub") != (u.acting_sub or "") \
                and not any(u.can_access_agent(s) for s in slugs):
            continue
        out.append({"id": d["id"], "name": d["name"], "version": d.get("community_set_version") or "",
                    "members": slugs})
    return out


def department_name_problem(name: str) -> str | None:
    """Why ``name`` cannot name the department (it names the library too,
    so the library's length is the one limit)."""
    if not name.strip():
        return "Give the department a name."
    if len(name.strip()) > library_rules.NAME_MAX:
        return f"A department name has at most {library_rules.NAME_MAX} characters."
    return library_rules.library_name_problem(name.strip())


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def require_platform_compat(dset: DepartmentSet) -> None:
    """The set's own floor, on every path that installs or places (the
    Browse list and the dialog's route check it first; the card and the
    offer come here alone)."""
    if not community_catalog.platform_version_ok(dset.platform_min_version):
        raise SetInstallRefused(
            400, f"This department set needs OtoDock {dset.platform_min_version} or newer. Upgrade the platform first.")


def running_job(set_slug: str) -> dict | None:
    return db_set_installs.running_for_set(set_slug)


async def start(dset: DepartmentSet, registry: dict, department_name: str, u, *,
                consent: bool, choices: dict[str, str] | None = None) -> dict:
    """Record the job and run it in the background. Refused (409) while a
    job runs on the set; (400) for a name a library cannot carry."""
    problem = department_name_problem(department_name)
    if problem:
        raise SetInstallRefused(400, problem)
    require_platform_compat(dset)
    assignment.require_assigner(u)
    lock = _lock_for(dset.slug)
    running = await asyncio.to_thread(running_job, dset.slug)
    if lock.locked() or running is not None:
        raise SetInstallRefused(409, ALREADY_RUNNING)
    plan = await asyncio.to_thread(plan_members, dset, registry, u, choices, department_name.strip())
    members = [{**row, "status": MEMBER_PENDING, "detail": ""} for row in plan]
    try:
        job = await asyncio.to_thread(
            db_set_installs.create, dset.slug, dset.version, department_name.strip(), u.acting_sub or u.sub, members)
    except db_set_installs.AlreadyRunning:
        # Two starts passed the check above together: the row is the claim.
        raise SetInstallRefused(409, ALREADY_RUNNING) from None
    from core.background import spawn
    spawn(_run(job["id"], dset, registry, department_name.strip(), u, consent),
          name=f"set-install-{job['id']}", owner="department_set_installer")
    return job


async def _run(job_id: str, dset: DepartmentSet, registry: dict, department_name: str, u, consent: bool) -> None:
    lock = _lock_for(dset.slug)
    async with lock:
        try:
            await _install_members(job_id, dset, registry, u, consent)
            job = await asyncio.to_thread(db_set_installs.get, job_id)
            if job is None or job["status"] != db_set_installs.RUNNING:
                return
            lines = list(job["lines"])
            slugs = {m["template"]: m["slug"] for m in job["members"]}
            installed_here = {m["slug"] for m in job["members"] if m["status"] == MEMBER_INSTALLED}
            dept = await _place(dset, department_name, slugs, u, lines, job_id=job_id)
            if dept is None:
                await asyncio.to_thread(db_set_installs.update, job_id, status=db_set_installs.FAILED, lines=lines)
                await _notify(u, f"{department_name} was not installed", lines)
                return
            await asyncio.to_thread(db_set_installs.update, job_id, department_id=dept["id"], lines=lines)
            await _share_library(dset, department_name, slugs, installed_here, u, lines)
            await asyncio.to_thread(_check_schedules, dset, slugs, lines)
            _bindings_note(dset, slugs, lines)
            await asyncio.to_thread(db_set_installs.update, job_id, status=db_set_installs.DONE, lines=lines)
            await _notify(u, f"{department_name} is installed", lines)
        except Exception as exc:  # the job's row carries the failure, the log the trace
            logger.exception("set install %s failed", job_id)
            job = await asyncio.to_thread(db_set_installs.get, job_id)
            lines = list(job["lines"]) if job else []
            lines.append(f"The install stopped on an unexpected error: {exc}")
            await asyncio.to_thread(db_set_installs.update, job_id, status=db_set_installs.FAILED, lines=lines)


async def _install_members(job_id: str, dset: DepartmentSet, registry: dict, u, consent: bool) -> None:
    """Every member installed or adopted, the job's row updated after each;
    a member that fails ends the job with the standalone line."""
    job = await asyncio.to_thread(db_set_installs.get, job_id)
    members = list(job["members"]) if job else []
    lines: list[str] = []
    reused = reusable_department(dset, job["department_name"], u) if job else None
    reused_id = reused["id"] if reused else ""
    for i, m in enumerate(members):
        if m["action"] == ACTION_ADOPT:
            row = agent_store.get_agent(m["slug"])
            held = row is not None and (not row.get("department_id") or row.get("department_id") == reused_id)
            if row is not None and held and _managed(u, m["slug"]):
                members[i] = {**m, "status": MEMBER_ADOPTED}
                where = "in the department already" if row.get("department_id") else "installed before"
                lines.append(f"Uses {row.get('display_name') or m['slug']} ({m['slug']}), {where}.")
            else:
                members[i] = {**m, "status": MEMBER_FAILED,
                              "detail": "The agent to use is gone, or in a department, or not yours to manage."}
            await asyncio.to_thread(db_set_installs.update, job_id, members=members, lines=lines)
            if members[i]["status"] == MEMBER_FAILED:
                break
            continue
        members[i] = {**m, "status": MEMBER_INSTALLING}
        await asyncio.to_thread(db_set_installs.update, job_id, members=members)
        entry = template_entry(registry, m["template"]) or {}
        app_consent, check_consent = consent_of(entry) if consent else ({}, {})
        slug, detail = await _install_one(m["template"], m["slug"], u, app_consent, check_consent)
        if slug:
            members[i] = {**m, "status": MEMBER_INSTALLED, "slug": slug}
            lines.append(f"Installed {m['display_name']} as {slug}.")
        else:
            members[i] = {**m, "status": MEMBER_FAILED, "detail": detail}
        await asyncio.to_thread(db_set_installs.update, job_id, members=members, lines=lines)
        if not slug:
            break
    failed = [m for m in members if m["status"] == MEMBER_FAILED]
    if failed:
        rest = [m for m in members if m["status"] in (MEMBER_INSTALLED, MEMBER_ADOPTED)]
        for m in failed:
            lines.append(f"{m['display_name']} was not installed: {m['detail']}")
        if rest:
            lines.append("The installed agents stand alone: " + _names(m["slug"] for m in rest) + ".")
        else:
            lines.append("No agent was installed.")
        left = [{**m, "status": MEMBER_PENDING} if m["status"] == MEMBER_PENDING else m for m in members]
        await asyncio.to_thread(db_set_installs.update, job_id, status=db_set_installs.FAILED,
                                members=left, lines=lines)
        await _notify(u, "The department was not installed", lines)


async def _install_one(template: str, target: str, u, app_consent: dict, check_consent: dict) -> tuple[str, str]:
    """One member through the template installer; a taken slug takes the
    suggestion, up to three times. ``(slug, '')`` or ``('', why)``."""
    for _attempt in range(4):
        try:
            result = await community_agent_installer.install_from_catalog(
                template_slug=template, target_slug=target,
                installer_user_sub=u.acting_sub or u.sub, installer_role=u.role,
                app_consent=app_consent or None, check_consent=check_consent or None,
                consent_by=(u.acting_sub or u.sub) if (app_consent or check_consent) else "")
            return str(result["agent_slug"]), ""
        except HTTPException as exc:
            detail: dict = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code == 409 and detail.get("suggested_slug"):
                target = str(detail["suggested_slug"])
                continue
            message = detail.get("message") if detail else exc.detail
            return "", str(message or exc.detail)
        except Exception as exc:  # the member's row carries the reason
            logger.exception("set install: member %s failed", template)
            return "", f"{type(exc).__name__}: {exc}"
    return "", "no free slug was found"


# ---------------------------------------------------------------------------
# The department and the placement
# ---------------------------------------------------------------------------

async def _place(dset: DepartmentSet, department_name: str, slugs: dict[str, str], u,
                 lines: list[str], job_id: str = "") -> dict | None:
    """The department created (written to the job at once, so a restart
    between the create and the placement finds it), or reused when this
    set made one under the same name that the person may edit and whose
    levels still match; every member placed in one transaction; one
    recompile. None with a line when a placement cannot be made."""
    existing = await asyncio.to_thread(reusable_department, dset, department_name, u)
    reuse = existing is not None
    placements: list[tuple[str, str]] = []
    missing = [t for t, s in slugs.items() if not agent_store.get_agent(s)]
    if missing:
        lines.append("Some agents are gone: " + ", ".join(missing) + ". Nothing was placed.")
        return None
    dept: dict
    if reuse and existing is not None:
        dept = existing
        lines.append(f"The department {department_name} existed already and was used.")
    else:
        dept = await asyncio.to_thread(
            db_departments.create_department, department_name, u.acting_sub or "",
            mode=dset.mode, reach=dset.reach, level_names=list(dset.levels),
            community_set=dset.slug, community_set_version=dset.version)
        if job_id:
            await asyncio.to_thread(db_set_installs.update, job_id, department_id=dept["id"])
    level_ids = {lv["name"]: lv["id"] for lv in dept["levels"]}
    for m in dset.members:
        placements.append((slugs[m.template], level_ids[m.level]))
    try:
        await asyncio.to_thread(db_departments.place_members, dept["id"], placements)
    except Exception:
        logger.exception("set install: placement failed")
        if not reuse:
            await asyncio.to_thread(db_departments.delete_department, dept["id"])
        lines.append("The agents could not be placed in the department. They stand alone.")
        return None
    await asyncio.to_thread(edge_compiler.recompile)
    lines.append(f"The department {department_name} holds " + _names(slugs.values()) + ".")
    return dept


async def place_from_set(dset: DepartmentSet, registry: dict, department_name: str, u,
                         new_slug: str) -> dict:
    """The offer on a single install: the new agent and the person's
    standalone agents from the set's templates placed in the set's
    department (made or reused), the library shared. Answers the lines and
    the department, or the lines alone when nothing could be placed."""
    problem = department_name_problem(department_name)
    if problem:
        raise SetInstallRefused(400, problem)
    require_platform_compat(dset)
    assignment.require_assigner(u)
    new_row = agent_store.get_agent(new_slug) or {}
    new_template = str(new_row.get("community_template") or "")
    plan = await asyncio.to_thread(plan_members, dset, registry, u, None, department_name.strip())
    slugs: dict[str, str] = {}
    for row in plan:
        if row["template"] == new_template:
            slugs[row["template"]] = new_slug
        elif row["action"] == ACTION_ADOPT:
            slugs[row["template"]] = row["slug"]
    if new_template not in slugs or len(slugs) < 2:
        raise SetInstallRefused(409, "No second agent of this set stands alone to place with it.")
    lines: list[str] = []
    lock = _lock_for(dset.slug)
    async with lock:
        placed = {t: s for t, s in slugs.items()}
        partial = DepartmentSet(**{**dset.__dict__, "members": [m for m in dset.members if m.template in placed]})
        dept = await _place(partial, department_name.strip(), placed, u, lines)
        if dept is not None:
            await _share_library(partial, department_name.strip(), placed, {new_slug}, u, lines)
            await asyncio.to_thread(_check_schedules, partial, placed, lines)
    return {"department": dept, "lines": lines, "members": placed}


# ---------------------------------------------------------------------------
# The library, the schedules, the bindings
# ---------------------------------------------------------------------------

async def _share_library(dset: DepartmentSet, department_name: str, slugs: dict[str, str],
                         installed_here: set[str], u, lines: list[str]) -> None:
    """The head's folder as a library named after the department (or the
    library already covering it), attached to every other member the
    person may wire. The folder is made when missing."""
    lib = dset.library
    if lib is None or lib.source not in slugs:
        return
    source = slugs[lib.source]
    consumers = [s for t, s in slugs.items() if t != lib.source]
    try:
        subdir = library_rules.normalize_subdir(lib.subdir)
        if not (u.is_admin or source in installed_here or _managed(u, source)):
            raise HTTPException(403, f"You do not manage {source}, which holds the library.")
        folder = config.get_agent_dir(source) / layout.KNOWLEDGE / subdir if subdir \
            else config.get_agent_dir(source) / layout.KNOWLEDGE
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(library_rules.validate_subdir_on_disk, source, subdir)
        # Only the set's own folder is reused: a wider library would hand
        # the members more than the set says.
        own = [r for r in await asyncio.to_thread(db_knowledge_libraries.libraries_of, source)
               if str(r.get("subdir") or "") == subdir]
        covering = None if own else await asyncio.to_thread(db_knowledge_libraries.library_covering, source, subdir)
        if own:
            name = str(own[0].get("name") or department_name)
            lib_subdir = subdir
            lines.append(f"The library {name} was shared already.")
        elif covering is not None:
            folder_words = f"{subdir} folder" if subdir else "knowledge folder"
            lines.append(f"The library {covering.get('name') or 'of the head'} already covers the head's "
                         f"{folder_words}, so none was made for the department. Attach it to the others yourself.")
            return
        else:
            await asyncio.to_thread(library_rules.validate_disjoint, source, subdir)
            library_rules.validate_library_name(department_name)
            await asyncio.to_thread(
                db_knowledge_libraries.promote, source, created_by=u.acting_sub or "",
                name=department_name, subdir=subdir)
            name, lib_subdir = department_name, subdir
    except HTTPException as exc:
        lines.append(f"The library was not shared: {exc.detail}")
        return
    from services.knowledge import library_projector
    attached: list[str] = []
    skipped: list[str] = []
    for consumer in consumers:
        if not (u.is_admin or consumer in installed_here or _managed(u, consumer)):
            skipped.append(consumer)
            continue
        try:
            await asyncio.to_thread(
                db_knowledge_libraries.attach, source, consumer, writable=lib.writable,
                created_by=u.acting_sub or "", subdir=lib_subdir)
        except ValueError as exc:
            lines.append(f"The library was not attached to {_names([consumer])}: {exc}")
            continue
        await library_projector.attach_setup(source, consumer)
        attached.append(consumer)
    if attached:
        how = "writable" if lib.writable else "read only"
        lines.append(f"The library {name} is shared from {_names([source])} to {_names(attached)}, {how}.")
    if skipped:
        lines.append(f"The library was not attached to {_names(skipped)}: you do not manage them.")


def _check_schedules(dset: DepartmentSet, slugs: dict[str, str], lines: list[str]) -> None:
    """The briefing schedules the set names, each proved paused (the
    templates seed them so); one running already is named."""
    paused: list[str] = []
    running: list[str] = []
    absent: list[str] = []
    for s in dset.schedules:
        slug = slugs.get(s.member)
        if not slug:
            continue
        row = db_tasks.find_template_task(slug, s.task)
        label = f"{(row or {}).get('name') or s.task} ({_names([slug])})"
        if row is None:
            absent.append(label)
        elif row.get("enabled"):
            running.append(label)
        else:
            paused.append(label)
    if paused:
        lines.append("The briefing schedules are paused: " + ", ".join(paused) + ". Each setup page says which to start.")
    if running:
        lines.append("These schedules run already: " + ", ".join(running) + ".")
    if absent:
        lines.append("These schedules were not found: " + ", ".join(absent) + ".")


def _bindings_note(dset: DepartmentSet, slugs: dict[str, str], lines: list[str]) -> None:
    """A member that landed under another slug than its template's is not
    the one the from-app's bindings name."""
    for b in dset.bindings:
        actual = slugs.get(b.to_member)
        if actual and actual != b.to_member:
            lines.append(
                f"The app {b.app} of {_names([slugs.get(b.from_member, b.from_member)])} reads "
                f"{_names([actual])} by its first name, {b.to_member}, not {actual}. "
                "It shows nothing for this department yet.")


async def _notify(u, title: str, lines: list[str]) -> None:
    target = u.acting_sub or u.sub
    if not target:
        return
    try:
        from services.notifications import notification_manager
        await notification_manager.fire_notification(
            title=title, body="\n".join(lines) or title, severity="info", scope="user",
            target=target, source="community_agent", source_id="department-set")
    except Exception:  # a notification never fails the job
        logger.exception("set install: the notification failed")


def offer_for(registry: dict, template_slug: str, u) -> dict | None:
    """The offer the install dialog of one template makes: the first set naming the template whose other members the
    person holds as standalone agents, with those agents, so installing
    this one can create the department and place them all."""
    from services.community.community_department_sets import (
        read_entry,
        sets_of_template,
    )
    for entry in sets_of_template(registry, template_slug):
        dset, _problems = read_entry(entry)
        if dset is None or not community_catalog.platform_version_ok(dset.platform_min_version):
            continue
        others = [row for row in plan_members(dset, registry, u)
                  if row["template"] != template_slug and row["action"] == ACTION_ADOPT]
        if not others:
            continue
        return {
            "slug": dset.slug, "name": dset.name,
            "members": [{"template": r["template"], "agent_slug": r["slug"], "display_name": r["display_name"],
                         "level": r["level"]} for r in others],
            "level": next((m.level for m in dset.members if m.template == template_slug), ""),
            "existing": departments_from(dset, u),
        }
    return None


def interrupt_at_boot() -> list[str]:
    """Every job the last run left running was cut off: mark it so, with
    the sentence its department's state earns. An empty department the
    job had made (cut off before the placement) is dropped."""
    marked: list[str] = []
    for job in db_set_installs.running_jobs():
        dept = db_departments.get_department(job["department_id"]) if job.get("department_id") else None
        line = INTERRUPTED_LINE
        if dept is not None:
            if db_departments.member_slugs(dept["id"]):
                line = INTERRUPTED_PLACED_LINE
            else:
                db_departments.delete_department(dept["id"])
        db_set_installs.interrupt(job["id"], line)
        marked.append(job["id"])
    return marked
