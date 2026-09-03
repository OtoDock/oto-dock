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
import os
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel

import config
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    require_creator_interactive,
)
from storage import agent_store, db_knowledge_libraries

from api.agents._router import router

logger = logging.getLogger("claude-proxy.knowledge-libraries")


def _require_agent_exists(name: str) -> None:
    if not agent_store.get_agent(name):
        raise HTTPException(status_code=404, detail=f"No such agent '{name}'")


def _require_library_authority(u: UserContext, agent: str) -> None:
    """Admin, or a creator who manages the agent being wired."""
    if u.role == "admin":
        return
    if not u.can_manage_agent(agent):
        raise HTTPException(
            status_code=403,
            detail="Creator access requires manager on this agent",
        )


_NAME_MAX = 64
# Filename-hostile characters (Windows superset — satellites include
# Windows/macOS filesystems, and the name becomes ``bulletin/<name>.md``).
_NAME_BAD_CHARS = set('/\\:*?"<>|')
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
# The source-root dirs a library subtree may never start in: agent memory,
# the mirror namespace itself, and OAuth service tokens. First-SEGMENT
# check — exact-string-only would let ``memory/private`` through.
_RESERVED_FIRST_SEGMENTS = frozenset({"memory", "shared", ".credentials"})


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def validate_library_name(label: str) -> None:
    """Filename-safety for the display name (drives ``bulletin/<name>.md``).

    Raises HTTPException(400). Existing rows with names that predate this
    check are untouched — their bulletin simply won't match until renamed.
    """
    if len(label) > _NAME_MAX:
        raise _bad(f"Library name is too long (max {_NAME_MAX} characters).")
    if any(ord(c) < 32 or ord(c) == 0x7F for c in label):
        raise _bad("Library name may not contain control characters.")
    hit = sorted(set(label) & _NAME_BAD_CHARS)
    if hit:
        raise _bad(
            "Library name may not contain " + " ".join(hit) +
            " (it names the library's bulletin file on every platform).")
    if label.startswith("."):
        raise _bad("Library name may not start with a dot.")
    if label.endswith(".") or label.endswith(" "):
        raise _bad("Library name may not end with a dot or space.")
    if label.split(".", 1)[0].strip().lower() in _WINDOWS_RESERVED:
        raise _bad(
            "Library name collides with a reserved device name on Windows "
            "(con, prn, aux, nul, com1-9, lpt1-9).")


def normalize_subdir(subdir: str) -> str:
    """Shape-check + normalize a library subdir ('' = whole folder).

    Raises HTTPException(400) on anything that is not a clean relative
    forward-slash path: absolute, backslashes, ``..``/``.``/empty
    segments, dot-leading segments, or a reserved first segment.
    """
    sub = (subdir or "").strip().strip("/")
    if not sub:
        return ""
    if "\\" in sub:
        raise _bad("Library folder must use forward slashes.")
    if any(ord(c) < 32 for c in sub):
        raise _bad("Library folder may not contain control characters.")
    segs = sub.split("/")
    for seg in segs:
        if not seg or seg in (".", ".."):
            raise _bad("Library folder must be a clean relative path "
                       "(no empty, '.' or '..' segments).")
        if seg.startswith("."):
            raise _bad("Library folder segments may not start with a dot.")
    if segs[0] in _RESERVED_FIRST_SEGMENTS:
        raise _bad(
            f"'{segs[0]}/' is reserved (agent memory, library mirrors and "
            "credentials can never be shared).")
    return sub


def _validate_subdir_on_disk(agent: str, sub: str) -> None:
    """The subtree must exist as a REAL directory chain (no symlink at any
    level) and contain no symlink escaping it — a symlinked file crossing
    knowledge subtrees would leak content past the disjointness rule."""
    if not sub:
        return
    knowledge_root = (config.get_agent_dir(agent) / "knowledge").resolve()
    cur = knowledge_root
    for seg in sub.split("/"):
        cur = cur / seg
        if cur.is_symlink():
            raise _bad(f"'{sub}' contains a symlink — share a real folder.")
        if not cur.is_dir():
            raise _bad(f"'{sub}' is not a folder under this agent's "
                       "knowledge/.")
    subtree = cur.resolve()
    for dirpath, dirnames, filenames in os.walk(cur):
        for entry in dirnames + filenames:
            p = Path(dirpath) / entry
            if not p.is_symlink():
                continue
            try:
                target = p.resolve()
            except OSError:
                target = None
            if target is None or not target.is_relative_to(subtree):
                raise _bad(
                    f"'{sub}' contains a symlink leaving the folder "
                    f"({p.relative_to(cur)}) — libraries must be "
                    "self-contained.")


def _bulletin_file(agent: str, subdir: str, name: str) -> Path | None:
    """Host path of a library's source-resident bulletin, or None for an
    unnamed library."""
    rel = db_knowledge_libraries.bulletin_rel(subdir, name)
    if not rel:
        return None
    return config.get_agent_dir(agent) / "knowledge" / rel


def _has_bulletin(agent: str, subdir: str, name: str) -> bool:
    p = _bulletin_file(agent, subdir, name)
    return bool(p and p.is_file())


def _wopi_locked(agent: str, knowledge_rel: str) -> bool:
    """Is an active Collabora edit lock held on this knowledge file?"""
    from api.media import wopi as _wopi
    file_id = _wopi.encode_file_id(f"{agent}/knowledge/{knowledge_rel}")
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


def _validate_disjoint(agent: str, sub: str) -> None:
    """Subtrees of one agent must be non-nested (segment-prefix check both
    directions, case-folded — satellites include case-insensitive
    filesystems). A root ('') library excludes any other and vice versa."""
    mine = [s.casefold() for s in sub.split("/")] if sub else []
    for row in db_knowledge_libraries.libraries_of(agent):
        other = row["subdir"] or ""
        if other == sub:
            continue  # re-promote of the same library (rename)
        theirs = [s.casefold() for s in other.split("/")] if other else []
        n = min(len(mine), len(theirs))
        if mine[:n] == theirs[:n]:
            other_label = f"'{other}'" if other else "the whole folder"
            raise _bad(
                f"Overlaps the existing shared library covering "
                f"{other_label} — library subtrees must be disjoint.")


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


@router.get("/v1/agents/{name}/knowledge-attachments")
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


@router.put("/v1/agents/{name}/knowledge-library")
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
                from api.agents.files import (
                    _record_platform_write, _tombstone_path,
                )
                old_rel, new_rel = moved
                asyncio.create_task(
                    _tombstone_path(name, f"knowledge/{old_rel}"))
                asyncio.create_task(
                    _record_platform_write(name, f"knowledge/{new_rel}", None))
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
        asyncio.create_task(
            library_projector.teardown_library(name, subdir, consumers))
    logger.info("knowledge-library unpromote: %s subdir=%r by=%s detached=%d",
                name, subdir, u.acting_sub, len(consumers))
    return {"status": "unshared", "subdir": subdir,
            "detached_consumers": consumers}


@router.put("/v1/agents/{name}/knowledge-attachments")
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
    if u.role != "admin" and not u.can_access_agent(source):
        raise HTTPException(
            status_code=403, detail="No access to the source agent")
    # A WRITABLE attach is a write channel INTO the source's knowledge tree
    # (the projector adopts mirror edits back and fans them out to every
    # consumer, bulletin included) — read-level access to the source must
    # not grant that. Editor tier on the SOURCE is the same bar as writing
    # its knowledge folder directly.
    if req.writable and u.role != "admin" and not u.can_edit_agent(source):
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
        raise HTTPException(status_code=400, detail=str(e))
    if not created:
        await asyncio.to_thread(
            db_knowledge_libraries.set_writable, source, name, req.writable,
            subdir)
    asyncio.create_task(library_projector.attach_setup(source, name))
    logger.info(
        "knowledge-library attach: %s/%s→%s writable=%s by=%s created=%s",
        source, subdir or "<root>", name, req.writable, u.acting_sub, created)
    mount = (f"/knowledge/shared/{source}/{subdir}" if subdir
             else f"/knowledge/shared/{source}")
    return {"status": "attached", "source_agent": source, "subdir": subdir,
            "writable": req.writable, "created": created,
            "note": f"Mirror content materializes now at {mount}; sessions "
                    "mount it at their next start."}


@router.delete("/v1/agents/{name}/knowledge-attachments/{source}")
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
    asyncio.create_task(library_projector.detach_teardown(source, name, sub))
    logger.info("knowledge-library detach: %s/%s→%s by=%s",
                source, sub or "<root>", name, u.acting_sub)
    return {"status": "detached", "subdir": sub}
