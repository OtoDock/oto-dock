"""The external identities picks of an agent: what a caller on its phone line
or another person who is not a member gets (tools, files, web, knowledge
folders, apps), and what happens after a conversation with one. ``GET`` / ``PUT /v1/agents/{name}/outside``, managers
only; a ``PUT`` changes only what it names. The picks are read by
``services/mcp/outside_picks.py`` when a session builds. Attaches to the
shared package router."""

import logging
from typing import cast

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

import config
from api.agents._router import router
from auth.providers import (
    UserContext,
    get_current_user,
    require_agent_access,
    require_auth,
)
from core import layout
from services.apps import app_folders
from services.mcp import mcp_permissions, mcp_registry, outside_picks
from services.mcp.mcp_agent_skills import is_skill_package
from storage import database as task_store
from storage.agents import agent_store, outside_store
from storage.knowledge import db_knowledge_libraries
from storage.pg import run_db

logger = logging.getLogger("otodock")



class OutsideToolPick(BaseModel):
    mcp: str
    access: str
    functions: list[str] = []


class OutsidePeopleRequest(BaseModel):
    """A change: every field left out (None) stays as it is, so a page that
    changed one thing never undoes what another session changed meanwhile.
    An unknown field is refused, so a caller that still sends a whole list
    is told rather than ignored."""
    model_config = ConfigDict(extra="forbid")

    # Each named tool's choice, written alone; ``remove_tools`` takes tools away.
    tools: list[OutsideToolPick] | None = None
    remove_tools: list[str] | None = None
    files: str | None = None
    web: bool | None = None
    # Knowledge folders added and taken away, each a path inside the
    # knowledge folder (a subfolder too).
    add_knowledge: list[str] | None = None
    remove_knowledge: list[str] | None = None
    # After a conversation: whether the agent writes down what it learned,
    # its note for the team, and its model.
    wrapup_enabled: bool | None = None
    shared_notes: bool | None = None
    wrapup_model: str | None = None
    # Apps opened to outside people and taken away, by the app's id.
    add_apps: list[str] | None = None
    remove_apps: list[str] | None = None


def _wrapup_models(name: str) -> list[dict]:
    """The models the agent's engine offers for the wrap-up, the agent's
    own first ('' = the same model as the conversation)."""
    from configuration.models import get_layer_models
    from storage.agents.schema import DEFAULT_EXECUTION_PATH
    layer = (agent_store.get_agent(name) or {}).get("execution_path") or DEFAULT_EXECUTION_PATH
    return [{"id": m["value"], "label": m["label"]} for m in get_layer_models(layer) if m["value"]]


def _manager(user: UserContext | None, name: str) -> UserContext:
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")
    return u


def _offered_tools(name: str) -> list:
    """The MCPs a manager may pick: the agent's enabled ones a caller's session
    can get once picked (``outside_picks.a_call_can_get``), never a skill
    package (no tools)."""
    return sorted(
        (m for m in mcp_registry.get_agent_mcps_all_placements(name)
         if outside_picks.a_call_can_get(m) and not is_skill_package(m)),
        key=lambda m: ((m.label or m.name).lower(), m.name),
    )


def knowledge_choices(name: str) -> list[str]:
    """The roots on offer to external identities, a manager picking any of
    them or any folder inside: the agent's own top-level knowledge folders
    the rule admits (the memory, the credentials and the libraries' root
    never) and each attached library by its mirror path."""
    out: list[str] = []
    root = config.get_agent_dir(name) / layout.KNOWLEDGE
    try:
        for entry in sorted(root.iterdir(), key=lambda e: e.name):
            if entry.is_dir() and not entry.is_symlink() and outside_picks.knowledge_parts(entry.name):
                out.append(entry.name)
    except OSError:
        pass
    for a in db_knowledge_libraries.attachments_for_consumer(name):
        sub = a.get("subdir") or ""
        mirror = f"{outside_picks.SHARED_ROOT}/{a['source_agent']}"
        out.append(f"{mirror}/{sub}" if sub else mirror)
    return out


def _app_line(row: dict, app_id: str) -> dict:
    return {"id": app_id, "slug": row.get("slug") or "", "title": row.get("title") or row.get("slug") or "",
            "methods": outside_picks.app_methods(row)}


def _apps_page(name: str, knowledge: list[str]) -> tuple[list[dict], list[dict]]:
    """``(apps, app_choices)``: every stored app pick that still names an
    app, with what a session gets from it now (``problem`` "" when it
    serves, else why not) and whether a release changed its methods since
    the pick; and every app of the agent a manager may add (shared, on the
    agent's apps, with a server), picked or not."""
    picked = outside_store.get_app_picks(name)
    rows = task_store.get_apps_by_ids(list(picked))
    apps = []
    for app_id, pick in picked.items():
        row = rows.get(app_id)
        if row is None:
            continue
        apps.append({**_app_line(row, app_id), "changed": bool(pick["changed_at"]),
                     "problem": outside_picks.app_problem(row, name, knowledge)})
    choices = [_app_line(row, row["id"]) for row in task_store.list_apps(name)
               if not outside_picks.app_refusal(row, name)]
    return sorted(apps, key=lambda a: a["title"].lower()), sorted(choices, key=lambda a: a["title"].lower())


def _read_page(name: str) -> dict:
    picks = outside_store.get_tool_picks(name)
    settings = outside_store.get_settings(name)
    tools = []
    for m in _offered_tools(name):
        key = outside_picks.server_key(m)
        known = mcp_permissions.known_tools(key)
        pick = picks.get(m.name)
        tools.append({
            "mcp": m.name,
            "label": m.label or m.name,
            "access": pick["access"] if pick else "",
            "functions": pick["functions"] if pick else [],
            "known_functions": (
                [{"name": t, "reads_only": mcp_permissions.reads_only(mcp_permissions.tool_hints(key, t))}
                 for t in known] if known else None),
            "changed": bool(pick and pick["changed_at"]),
        })
    apps, app_choices = _apps_page(name, list(outside_picks.load(name).knowledge))
    return {
        "tools": tools,
        "files": settings["files_access"],
        "web": settings["web"],
        "knowledge": settings["knowledge"],
        "knowledge_choices": knowledge_choices(name),
        "apps": apps,
        "app_choices": app_choices,
        **outside_store.get_wrapup(name),
        "wrapup_models": _wrapup_models(name),
    }


@router.get("/v1/agents/{name}/outside")
async def get_outside_people(name: str, user: UserContext | None = Depends(get_current_user)):
    """The picks and what is on offer: every enabled tool with its pick and
    its listed functions (``null`` while the server's list was never read),
    the person's files, the web, the knowledge folders and the folders on
    offer."""
    _manager(user, name)
    if not await run_db(agent_store.agent_exists, name):
        raise HTTPException(404, "Agent not found")
    return await run_db(_read_page, name)


def _folder_problem(name: str, folder: str) -> str:
    """Why ``folder`` cannot be added to the agent's knowledge picks, or "".
    A folder inside the knowledge folder: never the memory or the credentials
    (``outside_picks.knowledge_parts``), a library's only inside an attachment
    of this agent, and one that is not an attachment's own root (which the
    session makes) an existing folder reached with no link."""
    from services.infra import safe_fs
    refused = f"{folder} is not a knowledge folder of this agent."
    parts = outside_picks.knowledge_parts(folder)
    if parts is None:
        return refused
    if parts[0] == outside_picks.SHARED_ROOT:
        library = outside_picks.library_of(parts, db_knowledge_libraries.attachments_for_consumer(name))
        if library is None:
            return refused
        src, sub = library
        if parts[2:] == (tuple(sub.split("/")) if sub else ()):
            return ""
    try:
        with safe_fs.open_root(config.AGENTS_DIR, f"{name}/{layout.KNOWLEDGE}/{folder}"):
            return ""
    except OSError:
        return refused


def tool_pick_rows(offered: dict, picks, *, words: tuple[str, ...], governs: str,
                   refused: str) -> list[dict]:
    """The store rows of a request's tool picks, checked: each names a tool
    on offer (else 400 with ``refused``, the tool's name in it), an access
    word of ``words``, a custom list of at least one function, and no tool
    twice. A Read only row records the read-only functions the labels give
    today. The one validation of the External identities picks and of the
    agent's tool limits (``api/agents/tool_limits.py``)."""
    rows: list[dict] = []
    seen: set[str] = set()
    for mcp, access, functions in picks:
        m = offered.get(mcp)
        if m is None:
            raise HTTPException(400, refused.format(tool=mcp))
        label = m.label or mcp
        if access not in words:
            raise HTTPException(400, f"The access of {label} is "
                                     + ", ".join(ACCESS_LABELS[w] for w in words[:-1]) + f" or {ACCESS_LABELS[words[-1]]}.")
        if mcp in seen:
            raise HTTPException(400, f"{label} is named twice.")
        seen.add(mcp)
        names = sorted({f.strip() for f in functions if f.strip()}) if access == outside_picks.CUSTOM else []
        if access == outside_picks.CUSTOM and not names:
            raise HTTPException(400, f"A custom pick of {label} names at least one function.")
        granted = outside_picks.read_only_functions(outside_picks.server_key(m)) \
            if access == outside_picks.READ_ONLY else []
        rows.append({"mcp_name": mcp, "access": access, "functions": names, "granted": granted, "governs": governs})
    return rows


#: The access words as a refusal names them.
ACCESS_LABELS = {outside_picks.READ_ONLY: "read only", outside_picks.READ_WRITE: "read and write",
                 outside_picks.CUSTOM: "custom"}


def _write_page(name: str, req: OutsidePeopleRequest) -> dict:
    """Check what the request adds and write it, in one transaction. A
    stored pick the request does not name is never checked, so a folder that
    went away refuses nothing else."""
    offered = {m.name: m for m in _offered_tools(name)}
    if req.files is not None and req.files not in outside_picks.FILES_WORDS:
        raise HTTPException(400, "The files choice is read only or read and write.")
    rows = tool_pick_rows(
        offered, ((p.mcp, p.access, p.functions) for p in req.tools or ()),
        words=outside_picks.ACCESS_WORDS, governs=outside_store.OUTSIDE,
        refused="{tool} cannot be given to external identities on this agent.")
    for folder in req.add_knowledge or ():
        problem = _folder_problem(name, folder)
        if problem:
            raise HTTPException(400, problem)
    add_apps: list[dict] = []
    if req.add_apps:
        knowledge = list(outside_picks.load(name).knowledge)
        app_rows = task_store.get_apps_by_ids(list(req.add_apps))
        for app_id in dict.fromkeys(req.add_apps):
            row = app_rows.get(app_id)
            refused = outside_picks.app_refusal(row, name)
            if not refused:
                folder = app_folders.outside_folder_problem(cast("dict", row), knowledge)
                refused = f"This app reads a folder external identities may not see: {folder}." if folder else ""
            if refused:
                raise HTTPException(400, refused)
            add_apps.append({"app_id": app_id, "methods": outside_picks.app_methods(cast("dict", row))})
    if req.wrapup_model and req.wrapup_model not in {m["id"] for m in _wrapup_models(name)}:
        raise HTTPException(400, f"{req.wrapup_model} is not a model of this agent's engine.")
    remove_knowledge = list(req.remove_knowledge or ())
    if req.add_knowledge:
        # A stored folder no session reads any more (a library re-attached
        # narrower) never takes a new pick inside it as covered: it goes.
        attachments = db_knowledge_libraries.attachments_for_consumer(name)
        remove_knowledge += [
            old for old in outside_store.get_settings(name)["knowledge"]
            if not outside_picks.read_by_a_session(old, attachments)
            and any(new.startswith(old + "/") for new in req.add_knowledge)]
    outside_store.update(
        name, upsert_tools=rows if req.tools is not None else None, remove_tools=req.remove_tools,
        files_access=req.files, web=req.web,
        add_knowledge=req.add_knowledge, remove_knowledge=remove_knowledge or req.remove_knowledge,
        wrapup_enabled=req.wrapup_enabled, shared_notes=req.shared_notes, wrapup_model=req.wrapup_model,
        add_apps=add_apps or None, remove_apps=req.remove_apps,
    )
    return _read_page(name)


@router.put("/v1/agents/{name}/outside")
async def set_outside_people(
    name: str, req: OutsidePeopleRequest, user: UserContext | None = Depends(get_current_user),
):
    """Change the agent's picks: only what the request names. A tool pick
    names an enabled tool; a Read only pick records the read-only functions
    the labels give today, so a later change by the tool's maker can be
    told."""
    u = _manager(user, name)
    if not await run_db(agent_store.agent_exists, name):
        raise HTTPException(404, "Agent not found")
    page = await run_db(_write_page, name, req)
    changed = sorted(k for k, v in req.model_dump().items() if v is not None)
    logger.info("outside people: %s changed the picks of %s (%s)", u.sub[:12], name, ", ".join(changed) or "nothing")
    # A tool this request picked on a server never listed: list it now, so
    # Read only has its functions. Only the tools it names, so a change of
    # anything else starts no listing.
    from services.mcp import tool_listing
    named = {p.mcp for p in req.tools or ()}
    for tool in page["tools"]:
        if tool["mcp"] in named and tool["access"] and tool["known_functions"] is None:
            tool_listing.schedule(name, tool["mcp"])
    return page
