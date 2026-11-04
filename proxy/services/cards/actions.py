"""The action catalog (services/cards/cards.py draws and acts): every action a card's
``do`` button may run, with the schema its parameters must fit, the words
the server renders from them, the draw-time check that tells the agent
when the person could never act, and the run that executes on the click
with the clicker's own role on the side it touches, through the existing
service path. Adding an action is one entry in ``CATALOG`` or in a
module's ``ACTIONS``.

The first three are the Agent Creator's proposals (``agent-creator-mcp``):
install a community agent, install a department set, move an agent into a
department. The actions that call a route's own function live beside the
routes (``api/cards/actions_team.py``, ``api/cards/actions_departments.py``:
a service never imports the api layer) and join the catalog through
``register`` when the cards router loads.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from auth import roles
from services.cards import status as st
from services.community import (
    community_agent_installer,
    community_agents_catalog,
    community_department_sets as sets,
    department_set_installer,
)
from services.departments import assignment, edge_compiler
from storage.agents import agent_store, db_departments, db_set_installs

SLUG = {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,38}[a-z0-9]$"}
IDENT = {"type": "string", "minLength": 1, "maxLength": 80}


@dataclass(frozen=True)
class Outcome:
    """What a ``do`` click did: ``status`` is one of ``st.OUTCOME_STATES``,
    ``text`` the sentence the card shows, ``job_id`` a running job's."""

    status: str
    text: str
    job_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "text": self.text, "job_id": self.job_id, **self.extra}


@dataclass(frozen=True)
class Action:
    name: str
    schema: dict[str, Any]
    words: Callable[[dict[str, Any], Any], Awaitable[dict[str, str]]]
    check: Callable[[dict[str, Any], Any], Awaitable[str | None]]
    run: Callable[[dict[str, Any], dict[str, bool], Any], Awaitable[Outcome]]
    # The options a draw may offer, each a boolean the person sets at the click.
    options: Callable[[dict[str, Any], Any], list[dict[str, Any]]] = lambda _p, _u: []
    # A running outcome's live state, read when the card is read.
    progress: Callable[[Outcome], dict[str, Any]] | None = None
    # What of the asking person's own the parameters name (their files,
    # their memory, their personal apps), else "": such a card is its
    # asker's alone to press, since anyone else would act on their own
    # same-named things under words written for the asker.
    personal: Callable[[dict[str, Any]], str] = lambda _p: ""


def validate_params(action: Action, params: Any) -> list[str]:
    """The parameters against the action's schema, as sentences."""
    from jsonschema import Draft202012Validator
    if not isinstance(params, dict):
        return ["the parameters must be an object"]
    out = []
    for err in sorted(Draft202012Validator(action.schema).iter_errors(params), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "the parameters"
        out.append(f"{where}: {err.message}")
    return out


def display(slug: str) -> str:
    row = agent_store.get_agent(slug)
    return str((row or {}).get("display_name") or slug)


def sentence(text: str) -> str:
    """A refusal or an outcome as the person reads it: a capital first, a
    full stop last. Several routes' details carry neither, and the tools
    repeat only a sentence."""
    text = " ".join(str(text or "").split())
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def quoted(text: str) -> str:
    """Free text from a card's parameters (a department's or an app's
    name, a file name), quoted in the server's words."""
    return f'"{text}"'


_ERROR_PREFIX = re.compile(r"^\s*Error:\s*")


def plain(text: str) -> str:
    """A service's message as the person reads it: the "Error: " the agent
    tools print taken off, then a sentence."""
    return sentence(_ERROR_PREFIX.sub("", str(text or "")))


def _refusal_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return sentence(str(detail.get("message") or detail.get("error") or detail))
        return sentence(str(detail))
    if isinstance(exc, (assignment.AssignmentRefused, department_set_installer.SetInstallRefused)):
        return sentence(exc.detail)
    # The domain errors of the routes the catalog calls, by name (lazy: the
    # apps and memory services are heavy imports the draw never needs).
    from services.apps import app_blueprints, app_deploy
    from services.memory import memory_file, memory_promote
    if isinstance(exc, (memory_promote.MemoryRoleError, memory_file.MemoryOpError,
                        app_blueprints.BlueprintError, app_deploy.DeployError)):
        return plain(str(exc))
    return st.NO_ANSWER


# ---------------------------------------------------------------------------
# install_agent
# ---------------------------------------------------------------------------

async def _registry() -> dict:
    return await community_agents_catalog.fetch_registry()


def _dept_words(params: dict[str, Any]) -> str:
    dept = db_departments.get_department(str(params.get("department_id") or ""))
    if not dept:
        return ""
    level = next((lv["name"] for lv in dept["levels"] if lv["id"] == params.get("level_id")), "")
    return f" It is placed in {quoted(dept['name'])} as {level}." if level else ""


async def _install_words(params: dict[str, Any], u) -> dict[str, str]:
    entry = sets.template_entry(await _registry(), params["template_slug"]) or {}
    name = str(entry.get("display_name") or params["template_slug"])
    slug = str(params.get("target_slug") or params["template_slug"])
    text = f"Installs the {name} template as the agent {slug}."
    if params.get("display_name"):
        text += f" Its name will be {quoted(params['display_name'])}."
    text += _dept_words(params)
    if entry.get("apps") or entry.get("checks"):
        text += " Its apps wait for approval on their cards and its checks are offered."
    return {"title": f"Install {name}", "text": text, "label": "Install"}


INSTALLERS_ONLY = "Only admins and creators install agents."


async def _install_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None or not roles.is_creator_or_above(u.role):
        return INSTALLERS_ONLY
    entry = sets.template_entry(await _registry(), params["template_slug"])
    if entry is None:
        return f"The catalog has no template named {params['template_slug']}."
    if params.get("department_id"):
        try:
            assignment.require_assigner(u)
            dept = db_departments.get_department(str(params["department_id"]))
            if not dept:
                return assignment.NO_SUCH_DEPARTMENT + "."
            if params.get("level_id") not in {lv["id"] for lv in dept["levels"]}:
                return assignment.LEVEL_NOT_IN_DEPARTMENT + "."
        except assignment.AssignmentRefused as exc:
            return exc.detail
    return None


def _install_options(params: dict[str, Any], u) -> list[dict[str, Any]]:
    agent = str(params.get("_agent") or "")
    if agent and (u.is_admin or u.can_manage_agent(agent)):
        return [{"id": "delegation_edge", "label": f"Let {display(agent)} hand work to the new agent",
                 "default": True}]
    return []


async def _install_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    # The role again at the click: the person may have lost it since the draw.
    if u.acting_sub is None or not roles.is_creator_or_above(u.role):
        return Outcome(st.OUTCOME_FAILED, INSTALLERS_ONLY)
    target = str(params.get("target_slug") or params["template_slug"])
    slug = ""
    for _attempt in range(3):
        try:
            result = await community_agent_installer.install_from_catalog(
                template_slug=params["template_slug"], target_slug=target,
                installer_user_sub=u.acting_sub, installer_role=u.role,
                display_name=str(params.get("display_name") or ""))
            slug = str(result["agent_slug"])
            break
        except HTTPException as exc:
            detail: dict = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code == 409 and detail.get("suggested_slug"):
                target = str(detail["suggested_slug"])
                continue
            return Outcome(st.OUTCOME_FAILED, f"The agent was not installed: {_refusal_text(exc)}")
    if not slug:
        return Outcome(st.OUTCOME_FAILED, "The agent was not installed: no free name was found.")
    # Every viewer of the chat reads the outcome: it names who manages it.
    manager = str(getattr(u, "display_name", "") or getattr(u, "name", "") or "the person who pressed")
    lines = [f"{display(slug)} is installed as {slug}, managed by {manager}."]
    if params.get("department_id"):
        try:
            await assignment.assign(slug, str(params["department_id"]), str(params.get("level_id") or ""), u,
                                    manages=True)
            lines.append("It is placed in its department.")
        except assignment.AssignmentRefused as exc:
            lines.append(f"It was not placed: {exc.detail}")
    agent = str(params.get("_agent") or "")
    if options.get("delegation_edge") and agent and (u.is_admin or u.can_manage_agent(agent)):
        await asyncio.to_thread(agent_store.add_manual_delegation_target, agent, slug)
        await asyncio.to_thread(edge_compiler.recompile)
        lines.append(f"{display(agent)} can hand work to it.")
    return Outcome(st.OUTCOME_DONE, " ".join(lines), extra={"agent_slug": slug})


INSTALL_AGENT = Action(
    name="install_agent",
    schema={"type": "object", "required": ["template_slug"], "additionalProperties": False,
            "properties": {"template_slug": SLUG, "target_slug": SLUG, "display_name": IDENT,
                           "department_id": {"type": "string", "maxLength": 40},
                           "level_id": {"type": "string", "maxLength": 40},
                           "_agent": {"type": "string", "maxLength": 40}}},
    words=_install_words, check=_install_check, run=_install_run, options=_install_options,
)


# ---------------------------------------------------------------------------
# install_department_set
# ---------------------------------------------------------------------------

async def _set_of(params: dict[str, Any]) -> tuple[dict, sets.DepartmentSet | None, list[str]]:
    registry = await _registry()
    entry = sets.find_set(registry, params["set_slug"])
    if entry is None:
        return registry, None, [f"The catalog has no department set named {params['set_slug']}."]
    dset, problems = sets.read_entry(entry)
    return registry, dset, problems


async def _set_words(params: dict[str, Any], u) -> dict[str, str]:
    registry, dset, _problems = await _set_of(params)
    name = str(params.get("department_name") or (dset.name if dset else params["set_slug"]))
    if dset is None:
        return {"title": f"Install the {params['set_slug']} department", "text": "", "label": "Install"}
    members = ", ".join(
        f"{(sets.template_entry(registry, m.template) or {}).get('display_name') or m.template} ({m.level})"
        for m in dset.members)
    text = (f"Creates the department {quoted(name)} with {len(dset.members)} agents: {members}. "
            f"Delegation runs {db_departments_mode_words(dset.mode)}.")
    taken = [row for row in department_set_installer.plan_members(dset, registry, u, None, name)
             if row["action"] == department_set_installer.ACTION_ADOPT]
    if taken:
        who = ", ".join(f"{display(r['slug'])} ({r['slug']})" for r in taken)
        text += (f" {who} {'joins' if len(taken) == 1 else 'join'} it: installed from "
                 "these templates before and in no department.")
    if dset.library:
        text += " The head's library is shared with the team."
    if dset.schedules:
        text += " The schedules stay paused."
    text += " The apps wait for approval on their cards and the checks are offered."
    return {"title": f"Install the {dset.name} department", "text": text, "label": "Install"}


def db_departments_mode_words(mode: str) -> str:
    return {"off": "nowhere", "down": "downward only", "down_across": "downward and across each level",
            "both": "in every direction"}.get(mode, mode)


async def _set_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None or not roles.is_creator_or_above(u.role):
        return "Only admins and creators install departments."
    registry, dset, problems = await _set_of(params)
    if dset is None:
        return problems[0]
    missing = sets.missing_members(dset, registry)
    if missing:
        return "A template of this set is not in the catalog: " + ", ".join(missing) + "."
    name = str(params.get("department_name") or dset.name)
    problem = department_set_installer.department_name_problem(name)
    if problem:
        return problem
    if department_set_installer.running_job(dset.slug):
        return department_set_installer.ALREADY_RUNNING
    return None


async def _set_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    registry, dset, problems = await _set_of(params)
    if dset is None:
        return Outcome(st.OUTCOME_FAILED, problems[0])
    name = str(params.get("department_name") or dset.name)
    try:
        job = await department_set_installer.start(dset, registry, name, u, consent=False)
    except (department_set_installer.SetInstallRefused, assignment.AssignmentRefused) as exc:
        return Outcome(st.OUTCOME_FAILED, exc.detail)
    return Outcome(st.OUTCOME_RUNNING, f"The department {quoted(name)} is being installed.", job_id=job["id"])


def _set_progress(outcome: Outcome) -> dict[str, Any]:
    job = db_set_installs.get(outcome.job_id) if outcome.job_id else None
    if job is None:
        return {"status": st.OUTCOME_FAILED, "text": "The install's record is gone.", "lines": [], "members": []}
    status = {db_set_installs.RUNNING: st.OUTCOME_RUNNING, db_set_installs.DONE: st.OUTCOME_DONE}.get(
        job["status"], st.OUTCOME_FAILED)
    text = outcome.text if status == st.OUTCOME_RUNNING else (job["lines"][-1] if job["lines"] else outcome.text)
    return {"status": status, "text": text, "lines": job["lines"], "members": job["members"],
            "department_id": job.get("department_id") or "", "job_id": job["id"]}


INSTALL_DEPARTMENT_SET = Action(
    name="install_department_set",
    schema={"type": "object", "required": ["set_slug"], "additionalProperties": False,
            "properties": {"set_slug": SLUG, "department_name": {"type": "string", "minLength": 1, "maxLength": 100}}},
    words=_set_words, check=_set_check, run=_set_run, progress=_set_progress,
)


# ---------------------------------------------------------------------------
# move_agent
# ---------------------------------------------------------------------------

async def _move_words(params: dict[str, Any], u) -> dict[str, str]:
    dept = db_departments.get_department(params["department_id"]) or {}
    level = next((lv["name"] for lv in dept.get("levels") or [] if lv["id"] == params["level_id"]), "")
    name = display(params["agent"])
    dept_name = quoted(dept["name"]) if dept.get("name") else ""
    return {"title": f"Move {name} to {dept_name or 'a department'}",
            "text": f"Places {name} at the {level or 'chosen'} level of {dept_name or 'the department'}. "
                    "Delegation follows the department's wiring.",
            "label": "Move"}


async def _move_check(params: dict[str, Any], u) -> str | None:
    if not agent_store.get_agent(params["agent"]):
        return f"There is no agent named {params['agent']}."
    try:
        assignment.validate(params["agent"], params["department_id"], params["level_id"], u)
    except assignment.AssignmentRefused as exc:
        return exc.detail.rstrip(".") + "."
    return None


async def _move_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    try:
        await assignment.assign(params["agent"], params["department_id"], params["level_id"], u)
    except assignment.AssignmentRefused as exc:
        return Outcome(st.OUTCOME_FAILED, exc.detail.rstrip(".") + ".")
    dept = db_departments.get_department(params["department_id"]) or {}
    level = next((lv["name"] for lv in dept.get("levels") or [] if lv["id"] == params["level_id"]), "")
    return Outcome(st.OUTCOME_DONE, f"{display(params['agent'])} is now in {quoted(dept.get('name') or '')} as {level}.")


MOVE_AGENT = Action(
    name="move_agent",
    schema={"type": "object", "required": ["agent", "department_id", "level_id"], "additionalProperties": False,
            "properties": {"agent": SLUG, "department_id": {"type": "string", "minLength": 1, "maxLength": 40},
                           "level_id": {"type": "string", "minLength": 1, "maxLength": 40}}},
    words=_move_words, check=_move_check, run=_move_run,
)


CATALOG: dict[str, Action] = {a.name: a for a in (INSTALL_AGENT, INSTALL_DEPARTMENT_SET, MOVE_AGENT)}


def register(*actions_: Action) -> None:
    """The actions of a module beside the routes join the catalog (the
    cards router calls it at import). A name registers once: a second
    action under a taken name is a mistake, never a silent replacement."""
    for action in actions_:
        if action.name in CATALOG and CATALOG[action.name] is not action:
            raise ValueError(f"the card action {action.name!r} is registered already")
        CATALOG[action.name] = action


def get(name: str) -> Action | None:
    return CATALOG.get(name)
