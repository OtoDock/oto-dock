"""Signatures of a template's apps (COMMUNITY-AGENTS-REGISTRY.md "Consent").

A template app is consented on the install dialog from the registry's copy
of its documents and approved at install from the tarball's folder, so both
sides hash the same things the same way, here: the canonical JSON of the
raw ``app.json`` and ``blueprint.json`` (whitespace and key order never
matter) plus the tree hash of the other files. The tree hash excludes
``app.json`` because the importer rewrites it (task ids, formatting) and a
member's copy differs from the next member's; ``blueprint.json`` travels
verbatim and stays in. Pure functions: the prep catalog's generator imports
this module too.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_DOC = "app.json"
BLUEPRINT_DOC = "blueprint.json"

# What an installer's consent may never cover on a copy seeded for someone
# else (the owner approves those on their own card): buttons that run with
# the owner's identity and credentials, wakes and scripts that run in the
# owner's name, secrets, public routes, file writes.
FIRE_TASK = "fire_task"
OWNER_ACTION_TYPES = frozenset({"mcp_tool", FIRE_TASK})
# The keys of a fire_task button that name its task: the slug as shipped,
# the seeded task's id after the importer rewrote it (an app manifest's
# words, not a session kind's).
BUTTON_TARGET_KEYS = frozenset({"task", "task_id"})
OWNER_METHODS = frozenset({"files.write"})
OWNER_BLOCKS = ("handlers", "steps", "inbound", "secrets")


def canonical(doc: object) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tree_sha(files: list[tuple[str, Path]]) -> str:
    """The hash of a release manifest over ``files`` (``walk_tree`` pairs)
    with ``app.json`` left out — what a working tree still equals when only
    the importer touched it."""
    entries: dict[str, dict] = {}
    for rel, path in files:
        if rel == MANIFEST_DOC:
            continue
        data = path.read_bytes()
        entries[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return sha256_text(json.dumps({"files": entries}, sort_keys=True, separators=(",", ":")))


def template_app_sig(app_json: object, blueprint_json: object | None, tree: str) -> str:
    """The signature the dialog shows and the installer checks."""
    return sha256_text(canonical({"app_json": app_json, "blueprint": blueprint_json,
                                  "tree_sha": tree}))


def targets_masked(doc: dict) -> dict:
    """``app.json`` with the targets of its ``fire_task`` buttons left out:
    the importer rewrites a button's task slug into the seeded task's id,
    so this is what a copy still shares with the template's manifest."""
    out = dict(doc)
    actions = doc.get("actions")
    if isinstance(actions, list):
        out["actions"] = [
            {k: v for k, v in a.items() if k not in BUTTON_TARGET_KEYS}
            if isinstance(a, dict) and a.get("type") == FIRE_TASK else a
            for a in actions]
    return out


def check_sig(raw_doc: dict, script_sha256: str) -> str:
    """The signature the dialog shows for a check: the document as shipped
    (raw, so the catalog's generator needs no validator) and its script."""
    return sha256_text(canonical({"doc": raw_doc, "script_sha256": script_sha256 or ""}))


def needs_owner(doc: dict) -> bool:
    """True when a per-user copy of this manifest must be approved by its
    owner, whoever installs the template."""
    for a in doc.get("actions") or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") in OWNER_ACTION_TYPES:
            return True
        if a.get("type") == "platform" and a.get("method") in OWNER_METHODS:
            return True
    for name in OWNER_BLOCKS:
        if doc.get(name):
            return True
    files = doc.get("files")
    return bool(isinstance(files, dict) and files.get("write"))
