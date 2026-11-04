"""Shared knowledge libraries REST API.

Promote subtrees of an agent's ``knowledge/`` folder (``subdir = ''`` =
the whole folder) to installation-wide libraries and attach them
(read-only or writable) to other agents. Design + audit:
the shared-libraries design (v1) and its per-folder revision.

Wiring is platform-role territory (departments precedent): every mutation
gates on ``require_creator_interactive`` — dashboard admins/creators AND
real-user-backed session principals with those platform roles (the
operator-decided carve-out for the sensitive-tier agent-config-mcp
tools). Per-agent managers get the read endpoint for the config UI, and
never the mutations.

This module owns PROMOTE-TIME VALIDATION — the one funnel every library
mutation passes (dashboard + agent-config-mcp both call these routes):
subdir shape + existence + symlink containment, disjointness across the
agent's libraries, and the display name's filename-safety (the name
drives the library's bulletin filename, Windows/macOS satellites
included).
"""

import asyncio
import functools
import logging
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel

import config
from api.agents._router import router
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    require_creator_interactive,
    session_bound_to,
)
from core import layout
from core.background import spawn
from services.infra.path_confinement import (
    PathOutsideRoot,
    resolve_under,
)
from services.knowledge import library_rules
from storage.agents import agent_store
from storage.knowledge import db_knowledge_libraries

logger = logging.getLogger("otodock.knowledge-libraries")

# A session token changes an agent's libraries only for the agent it was
# started for, the consumer ``{name}``; ``{source}`` is another agent by
# design (``auth.providers.session_bound_to``).
_BOUND = [Depends(session_bound_to("name"))]


def _require_agent_exists(name: str) -> None:
    if not agent_store.get_agent(name):
        raise HTTPException(status_code=404, detail=f"No such agent '{name}'")


_require_library_authority = library_rules.require_library_authority
# The refusal a person reads, here and on an attach card.
NO_SOURCE_ACCESS = "You have no access to the source agent."


# The name, subdir, on-disk and disjointness rules live in
# services/knowledge/library_rules.py (the set installer applies them too);
# the names stay importable from here for the tests that read them.
validate_library_name = library_rules.validate_library_name
normalize_subdir = library_rules.normalize_subdir
_validate_subdir_on_disk = library_rules.validate_subdir_on_disk
_validate_disjoint = library_rules.validate_disjoint


def _bulletin_file(agent: str, subdir: str, name: str) -> Path | None:
    """Host path of a library's source-resident bulletin, or None for an
    unnamed library (or one whose name would leave ``knowledge/``)."""
    rel = db_knowledge_libraries.bulletin_rel(subdir, name)
    if not rel:
        return None
    knowledge = config.get_agent_dir(agent) / layout.KNOWLEDGE
    try:
        return resolve_under(knowledge / rel, knowledge)
    except PathOutsideRoot:
        return None


def _has_bulletin(agent: str, subdir: str, name: str) -> bool:
    p = _bulletin_file(agent, subdir, name)
    return bool(p and p.is_file())


def _wopi_locked(agent: str, knowledge_rel: str) -> bool:
    """Is an active Collabora edit lock held on this knowledge file?"""
    from api.media import wopi as _wopi
    file_id = _wopi.encode_file_id(f"{agent}/{layout.KNOWLEDGE}/{knowledge_rel}")
    return _wopi._get_lock(file_id) is not None


def _rename_bulletin_for(agent: str, subdir: str, old_name: str,
                         new_name: str) -> tuple[str, str] | None:
    """Re-promoting with a new name renames the source-side bulletin file
    with it. Refused (409) — and the label change with it — when the
    target filename already exists as real content, or an active Collabora
    lock holds either file: the operator resolves and retries. Mirrors
    catch up via projection (they briefly show the old copy).

    Synchronous (runs in a worker thread); returns ``(old_rel, new_rel)``
    when a file was moved so the ASYNC caller schedules the tombstone +
    projection bookkeeping, or None when there was nothing to carry."""
    old_file = _bulletin_file(agent, subdir, old_name)
    if old_file is None or not old_file.is_file():
        return None  # nothing to carry over
    new_file = _bulletin_file(agent, subdir, new_name)
    if new_file is None:
        return None
    old_rel = db_knowledge_libraries.bulletin_rel(subdir, old_name)
    new_rel = db_knowledge_libraries.bulletin_rel(subdir, new_name)
    if new_file.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Renaming would move the bulletin to '{new_rel}', which "
                   "already exists — move or delete that file first, then "
                   "rename again.")
    if _wopi_locked(agent, old_rel) or _wopi_locked(agent, new_rel):
        raise HTTPException(
            status_code=409,
            detail="The library's bulletin is open in the document editor — "
                   "close it and rename again.")
    old_file.rename(new_file)
    return old_rel, new_rel


class LibraryToggleRequest(BaseModel):
    enabled: bool
    # The library's display label. Never becomes a MIRROR path (the mirror
    # stays ``knowledge/shared/<source_agent>/<subdir>/``) but it names the
    # library's bulletin file, so it is validated as a filename. Required
    # when enabling, ignored when disabling. Re-sending with a new value
    # renames in place.
    name: str = ""
    # Library subtree relative to the agent's knowledge/ root; '' shares
    # the whole folder. Part of the library's identity — promote/unpromote
    # address ``(source_agent, subdir)``.
    subdir: str = ""


class AttachRequest(BaseModel):
    source_agent: str
    subdir: str = ""
    writable: bool = False


@router.get("/v1/knowledge-libraries")
async def list_knowledge_libraries(
    user: UserContext = Depends(get_current_user),
):
    """Every promoted library with its consumer count (admin/creator)."""
    require_creator_interactive(user)
    libraries = await asyncio.to_thread(db_knowledge_libraries.list_libraries)
    return {"libraries": libraries}


@router.get("/v1/agents/{name}/knowledge-attachments", dependencies=_BOUND)
async def get_knowledge_attachments(
    name: str, user: UserContext = Depends(get_current_user),
):
    """This agent's library state for the config UI: its own shared
    libraries (+ per-library consumers), and what it has attached.
    Manager-tier read, mirroring the delegation-targets GET."""
    u = require_auth(user)
    if not u.can_manage_agent(name):
        raise HTTPException(status_code=403, detail="Manager access required")
    _require_agent_exists(name)
    own = await asyncio.to_thread(db_knowledge_libraries.libraries_of, name)
    all_consumers = await asyncio.to_thread(
        db_knowledge_libraries.consumers_of, name)
    by_subdir: dict[str, list[dict]] = {}
    for c in all_consumers:
        by_subdir.setdefault(c["subdir"] or "", []).append(
            {"consumer_agent": c["consumer_agent"],
             "writable": bool(c["writable"])})
    libraries = [
        {
            "subdir": row["subdir"] or "",
            "name": row["name"] or "",
            "consumers": by_subdir.get(row["subdir"] or "", []),
            "has_bulletin": _has_bulletin(
                name, row["subdir"] or "", row["name"] or ""),
        }
        for row in own
    ]
    attachments = [
        {**a,
         "has_bulletin": _has_bulletin(
             a["source_agent"], a["subdir"] or "", a["name"] or "")}
        for a in await asyncio.to_thread(
            db_knowledge_libraries.attachments_for_consumer, name)
    ]
    return {
        "is_library": bool(libraries),
        "libraries": libraries,
        "attachments": attachments,
    }


@router.put("/v1/agents/{name}/knowledge-library", dependencies=_BOUND)
async def set_knowledge_library(
    name: str, req: LibraryToggleRequest,
    user: UserContext = Depends(get_current_user),
):
    """Promote / un-promote ONE library — a subtree (``req.subdir``, '' =
    whole folder) of the agent's knowledge folder.

    Un-promoting detaches that library's consumers and tears their mirror
    subtrees down (sibling libraries of the same agent are untouched).
    """
    u = require_creator_interactive(user)
    _require_library_authority(u, name)
    _require_agent_exists(name)
    from services.knowledge import library_projector

    subdir = normalize_subdir(req.subdir)

    if req.enabled:
        label = (req.name or "").strip()
        if not label:
            raise HTTPException(
                status_code=400,
                detail="A library name is required when sharing.",
            )
        validate_library_name(label)
        await asyncio.to_thread(_validate_subdir_on_disk, name, subdir)
        await asyncio.to_thread(_validate_disjoint, name, subdir)
        # A rename carries the library's bulletin file with it; the checks
        # run BEFORE the row update so a refused file op (409) leaves the
        # label unchanged too — never a label/bulletin mismatch by rename.
        old_label = await asyncio.to_thread(
            db_knowledge_libraries.library_name, name, subdir)
        if old_label and old_label != label:
            moved = await asyncio.to_thread(
                _rename_bulletin_for, name, subdir, old_label, label)
            if moved:
                # Tombstone the old path + record the new one; both fire
                # the library projection, so mirrors and satellites follow
                # the rename instead of resurrecting the old file.
                from services.infra import file_bookkeeping
                old_rel, new_rel = moved
                spawn(file_bookkeeping.tombstone_path(name, f"{layout.KNOWLEDGE}/{old_rel}"),
                      name="tombstone_path", owner="knowledge_libraries")
                spawn(file_bookkeeping.record_platform_write(
                        name, f"{layout.KNOWLEDGE}/{new_rel}", None),
                      name="record_platform_write", owner="knowledge_libraries")
        # `name` here is the AGENT slug (the path param); `label` is the
        # library's display name. Spelled out so the two never blur.
        created = await asyncio.to_thread(
            functools.partial(
                db_knowledge_libraries.promote, source_agent=name,
                created_by=u.acting_sub or "", name=label, subdir=subdir))
        logger.info(
            "knowledge-library promote: %s subdir=%r name=%r by=%s created=%s",
            name, subdir, label, u.acting_sub, created)
        return {"status": "shared", "created": created, "name": label,
                "subdir": subdir,
                "has_bulletin": _has_bulletin(name, subdir, label)}

    consumers = await asyncio.to_thread(
        db_knowledge_libraries.unpromote, name, subdir)
    if consumers:
        spawn(library_projector.teardown_library(name, subdir, consumers),
              name="teardown_library", owner="knowledge_libraries")
    logger.info("knowledge-library unpromote: %s subdir=%r by=%s detached=%d",
                name, subdir, u.acting_sub, len(consumers))
    return {"status": "unshared", "subdir": subdir,
            "detached_consumers": consumers}


@router.put("/v1/agents/{name}/knowledge-attachments", dependencies=_BOUND)
async def attach_knowledge_library(
    name: str, req: AttachRequest,
    user: UserContext = Depends(get_current_user),
):
    """Attach a library to this agent (or update the writable flag).

    ``name`` is the CONSUMER. A creator needs manager on the consumer AND
    access to the source (observe-edge precedent: wiring another team's
    knowledge in requires being able to reach it).
    """
    u = require_creator_interactive(user)
    _require_library_authority(u, name)
    source = (req.source_agent or "").strip()
    if not source or source == name:
        raise HTTPException(
            status_code=400, detail="source_agent must be a different agent")
    subdir = normalize_subdir(req.subdir)
    _require_agent_exists(name)
    _require_agent_exists(source)
    if not u.is_admin and not u.can_access_agent(source):
        raise HTTPException(status_code=403, detail=NO_SOURCE_ACCESS)
    # A WRITABLE attach is a write channel INTO the source's knowledge tree
    # (the projector adopts mirror edits back and fans them out to every
    # consumer, bulletin included) — read-level access to the source must
    # not grant that. Editor tier on the SOURCE is the same bar as writing
    # its knowledge folder directly.
    if req.writable and not u.is_admin and not u.can_edit_agent(source):
        raise HTTPException(
            status_code=403,
            detail="Writable attachment requires editor, manager, or admin "
                   "role on the SOURCE agent — its library adopts your "
                   "edits. Attach read-only instead.")
    from services.knowledge import library_projector

    try:
        created = await asyncio.to_thread(
            functools.partial(
                db_knowledge_libraries.attach, source, name,
                writable=req.writable, created_by=u.acting_sub or "",
                subdir=subdir))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not created:
        await asyncio.to_thread(
            db_knowledge_libraries.set_writable, source, name, req.writable,
            subdir)
    spawn(library_projector.attach_setup(source, name),
          name="attach_setup", owner="knowledge_libraries")
    logger.info(
        "knowledge-library attach: %s/%s→%s writable=%s by=%s created=%s",
        source, subdir or "<root>", name, req.writable, u.acting_sub, created)
    mount = (f"{layout.V_KNOWLEDGE}/shared/{source}/{subdir}" if subdir
             else f"{layout.V_KNOWLEDGE}/shared/{source}")
    return {"status": "attached", "source_agent": source, "subdir": subdir,
            "writable": req.writable, "created": created,
            "note": f"Mirror content materializes now at {mount}; sessions "
                    "mount it at their next start."}


@router.delete("/v1/agents/{name}/knowledge-attachments/{source}", dependencies=_BOUND)
async def detach_knowledge_library(
    name: str, source: str,
    subdir: str = "",
    user: UserContext = Depends(get_current_user),
):
    """Detach one library from this agent and remove its mirror subtree."""
    u = require_creator_interactive(user)
    _require_library_authority(u, name)
    sub = normalize_subdir(subdir)
    removed = await asyncio.to_thread(
        db_knowledge_libraries.detach, source, name, sub)
    if not removed:
        raise HTTPException(status_code=404, detail="Not attached")
    from services.knowledge import library_projector
    spawn(library_projector.detach_teardown(source, name, sub),
          name="detach_teardown", owner="knowledge_libraries")
    logger.info("knowledge-library detach: %s/%s→%s by=%s",
                source, sub or "<root>", name, u.acting_sub)
    return {"status": "detached", "subdir": sub}
