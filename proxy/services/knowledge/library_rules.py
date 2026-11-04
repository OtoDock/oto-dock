"""The rules a shared knowledge library holds to (api/agents/knowledge_libraries.py calls them):
the display name as a filename, the subtree as a clean relative path, the
subtree real on disk and self-contained, the subtrees of one agent
disjoint, and who may wire a library. The library routes and the set
installer read the same rules from here.

Each check raises ``HTTPException`` with the sentence the route answers;
``library_name_problem`` answers the sentence alone for a dialog or a
preview that wants to show it before anything is written.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

import config
from core import layout
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
from storage.knowledge import db_knowledge_libraries

NAME_MAX = 64
# Filename-hostile characters (Windows superset: satellites include
# Windows/macOS filesystems, and the name becomes ``bulletin/<name>.md``).
_NAME_BAD_CHARS = set('/\\:*?"<>|')
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
# The source-root dirs a library subtree may never start in: agent memory,
# the mirror namespace itself, and OAuth service tokens. First-SEGMENT
# check: exact-string-only would let ``memory/private`` through.
_RESERVED_FIRST_SEGMENTS = frozenset({"memory", "shared", layout.CREDENTIALS_DIR})


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def library_name_problem(label: str) -> str | None:
    """Why ``label`` cannot name a library (it names the bulletin file on
    every platform), or None when it can."""
    if len(label) > NAME_MAX:
        return f"Library name is too long (max {NAME_MAX} characters)."
    if any(ord(c) < 32 or ord(c) == 0x7F for c in label):
        return "Library name may not contain control characters."
    hit = sorted(set(label) & _NAME_BAD_CHARS)
    if hit:
        return ("Library name may not contain " + " ".join(hit) +
                " (it names the library's bulletin file on every platform).")
    if label.startswith("."):
        return "Library name may not start with a dot."
    if label.endswith(".") or label.endswith(" "):
        return "Library name may not end with a dot or space."
    if label.split(".", 1)[0].strip().lower() in _WINDOWS_RESERVED:
        return ("Library name collides with a reserved device name on Windows "
                "(con, prn, aux, nul, com1-9, lpt1-9).")
    return None


def validate_library_name(label: str) -> None:
    """Filename-safety for the display name. Raises HTTPException(400).
    Existing rows with names that predate this check are untouched: their
    bulletin simply never matches until renamed."""
    problem = library_name_problem(label)
    if problem:
        raise _bad(problem)


def normalize_subdir(subdir: str) -> str:
    """Shape-check and normalize a library subdir ('' = the whole folder).

    Raises HTTPException(400) on anything that is not a clean relative
    forward-slash path: absolute, backslashes, ``..``/``.``/empty segments,
    dot-leading segments, or a reserved first segment.
    """
    sub = (subdir or "").strip().strip("/")
    if not sub:
        return ""
    if "\\" in sub:
        raise _bad("Library folder must use forward slashes.")
    if any(ord(c) < 32 for c in sub):
        raise _bad("Library folder may not contain control characters.")
    try:
        sub = normalize_rel_path(sub)
    except PathOutsideRoot as exc:
        raise _bad("Library folder must be a clean relative path "
                   "(no empty, '.' or '..' segments).") from exc
    segs = sub.split("/")
    for seg in segs:
        if seg.startswith("."):
            raise _bad("Library folder segments may not start with a dot.")
    if segs[0] in _RESERVED_FIRST_SEGMENTS:
        raise _bad(
            f"'{segs[0]}/' is reserved (agent memory, library mirrors and "
            "credentials can never be shared).")
    return sub


def validate_subdir_on_disk(agent: str, sub: str) -> None:
    """The subtree must exist as a REAL directory chain (no symlink at any
    level) and contain no symlink escaping it: a symlinked file crossing
    knowledge subtrees would leak content past the disjointness rule."""
    if not sub:
        return
    knowledge_root = (config.get_agent_dir(agent) / layout.KNOWLEDGE).resolve()
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


def validate_disjoint(agent: str, sub: str) -> None:
    """Subtrees of one agent must be non-nested (segment-prefix check both
    directions, case-folded: satellites include case-insensitive
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


def may_wire_library(u, agent: str) -> bool:
    """Admin, or a creator who manages the agent being wired (the consumer
    of an attach, the source of a promote)."""
    return bool(u.is_admin or u.can_manage_agent(agent))


# The refusal a person reads, here and on an attach card.
NOT_YOUR_LIBRARY_AGENT = "A creator wires a library only to an agent they manage."


def require_library_authority(u, agent: str) -> None:
    if not may_wire_library(u, agent):
        raise HTTPException(status_code=403, detail=NOT_YOUR_LIBRARY_AGENT)
