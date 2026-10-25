"""The ``files`` block of an app's manifest: the prefixes its file methods
reach, and the folders it needs made (``create``).

An app that lists or reads a folder other tools fill names it under
``create``, inside a ``read`` or ``write`` prefix of the same signed block,
and the platform makes the folder wherever a served release's manifest is
approved: when a release goes live (``app_deploy.go_live``), when the plain
card approves a folder app's manifest, and at the end of a rollback whose
restored manifest is approved. Never in a check, a preview or a render,
which run code nobody approved.

A folder that is there is kept as it is, a file or a link in the way is
reported and skipped, and nothing here ever deletes a folder: other tools
and people keep files there. ``read`` and ``write`` keep their exact stored
text and ``create`` is written only when it lists a folder, so every
manifest without it signs as it always did.
"""

from __future__ import annotations

import logging
import stat

import psycopg

import config
from core import layout
from services.infra import path_confinement, safe_fs

logger = logging.getLogger("otodock.apps")

FILES_READ = "read"
FILES_WRITE = "write"
FILES_CREATE = "create"
FILES_KEYS = (FILES_READ, FILES_WRITE, FILES_CREATE)
MAX_CREATE_DIRS = 4
# A declared folder at call time (``deploy_status``).
FOLDER_PRESENT = "present"
FOLDER_MISSING = "missing"
FOLDER_BLOCKED = "blocked"
# What one pass of ``ensure_created_dirs`` did, per path.
FOLDERS_CREATED = "created"
FOLDERS_KEPT = "kept"
FOLDERS_SKIPPED = "skipped"
# The app's own tree under the workspace: the deploy and the import own it.
_APPS_SEGMENT = "apps"
# A data folder is never declared, the app's own or another (it holds an
# app server's database).
_DATA_SEGMENT = "data"


def validate_files(block, shared: bool) -> dict[str, list[str]]:
    """The declared prefixes, agent-dir-relative (``workspace/…`` or
    ``knowledge/…``; a personal app's are stored under ``users/{owner}/``
    and resolved from the owner at call time), and the folders to create.
    Knowledge is never writable; the never-declarable segments are refused
    literally and by the credential-path rule. Raises ``DeployError``."""
    from services import path_roles
    from services.apps.app_deploy import MAX_FILE_PREFIXES, NEVER_SEGMENTS, DeployError
    if block is None:
        return {FILES_READ: [], FILES_WRITE: []}
    if not isinstance(block, dict) or any(k not in FILES_KEYS for k in block):
        raise DeployError("files must be an object with read, write and create lists")
    out: dict[str, list[str]] = {FILES_READ: [], FILES_WRITE: []}
    total = 0
    for mode in (FILES_READ, FILES_WRITE):
        prefixes = block.get(mode) or []
        if not isinstance(prefixes, list):
            raise DeployError(f"files.{mode} must be a list")
        for p in prefixes:
            if not isinstance(p, str):
                raise DeployError("file prefixes must be strings")
            rel = p.strip().strip("/")
            parts = rel.split("/") if rel else []
            if not parts or parts[0] not in (layout.WORKSPACE, layout.KNOWLEDGE):
                raise DeployError(f"file prefix {p!r}: must start with workspace/ or knowledge/")
            try:
                rel = path_confinement.normalize_rel_path(rel)
            except path_confinement.PathOutsideRoot as exc:
                raise DeployError(f"file prefix {p!r}: no empty or dot segments") from exc
            if any(seg in NEVER_SEGMENTS for seg in parts) or (
                    len(parts) >= 3 and parts[1] == _APPS_SEGMENT and _DATA_SEGMENT in parts):
                raise DeployError(f"file prefix {p!r}: that path is never declarable")
            if path_roles.is_protected_credentials_path(rel):
                raise DeployError(f"file prefix {p!r}: that path holds credentials")
            if mode == FILES_WRITE and parts[0] == layout.KNOWLEDGE:
                raise DeployError(f"file prefix {p!r}: knowledge is read-only for apps")
            stored = rel if shared else f"{layout.USERS}/{{owner}}/{rel}"
            if not shared and parts[0] == layout.KNOWLEDGE:
                raise DeployError(f"file prefix {p!r}: a personal app names the owner's tree only")
            if stored not in out[mode]:
                out[mode].append(stored)
                total += 1
            if total > MAX_FILE_PREFIXES:
                raise DeployError(f"at most {MAX_FILE_PREFIXES} file prefixes")
    create = validate_create(block.get(FILES_CREATE), out, shared)
    if create:
        out[FILES_CREATE] = create
    return out


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def _unowned(stored: str) -> str:
    """A stored prefix without a personal app's ``users/{owner}/``."""
    head = f"{layout.USERS}/{{owner}}/"
    return stored[len(head):] if stored.startswith(head) else stored


def validate_create(entries, declared: dict[str, list[str]], shared: bool) -> list[str]:
    """The ``create`` list of a ``files`` block, stored the way the block's
    prefixes are; ``declared`` holds the block's validated ``read`` and
    ``write`` prefixes. Raises ``DeployError``."""
    from services import path_roles
    from services.apps.app_deploy import NEVER_SEGMENTS, DeployError
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise DeployError("files.create must be a list of folders")
    if len(entries) > MAX_CREATE_DIRS:
        raise DeployError(f"files.create names at most {MAX_CREATE_DIRS} folders")
    reach = [_unowned(p) for p in declared[FILES_READ] + declared[FILES_WRITE]]
    out: list[str] = []
    for p in entries:
        if not isinstance(p, str):
            raise DeployError("files.create lists folders as text")
        if "\\" in p or "\x00" in p:
            raise DeployError(f"files.create {p!r}: a folder path uses forward slashes only")
        rel = p.strip().strip("/")
        parts = rel.split("/") if rel else []
        if len(parts) < 2 or parts[0] != layout.WORKSPACE:
            raise DeployError(f"files.create {p!r}: a folder to create sits inside workspace/")
        if any(not seg or seg.startswith(".") for seg in parts):
            raise DeployError(f"files.create {p!r}: no empty part and no part that starts with a dot")
        if any(seg in NEVER_SEGMENTS for seg in parts) or parts[1].lower() == _APPS_SEGMENT:
            raise DeployError(f"files.create {p!r}: the platform never creates that folder for an app")
        if path_roles.is_protected_credentials_path(rel):
            raise DeployError(f"files.create {p!r}: that path holds credentials")
        if not any(_under(rel, q) for q in reach):
            raise DeployError(f"files.create {p!r}: the app reads no folder there. Declare it "
                              "under read or write as well.")
        stored = rel if shared else f"{layout.USERS}/{{owner}}/{rel}"
        if stored not in out:
            out.append(stored)
    return out


def declared_folders(row: dict) -> list[str]:
    """The row's ``create`` folders, agent-dir-relative with a personal
    app's owner resolved; empty when the owner cannot be read."""
    from services.apps import catalog
    try:
        return catalog._declared_prefixes(row, FILES_CREATE)
    except psycopg.Error:
        logger.exception("App %s: the folders to create did not resolve", row.get("slug"))
        return []


def _why(exc: OSError) -> str:
    if isinstance(exc, safe_fs.SymlinkRefused):
        return "a link is in the way, and a link is never followed"
    if isinstance(exc, (FileExistsError, NotADirectoryError, safe_fs.NotRegularFile)):
        return "a file is in the way"
    if isinstance(exc, PermissionError):
        return "the platform may not write there"
    return "the folder could not be made"


def ensure_created_dirs(row: dict) -> dict[str, list]:
    """Make the folders the row's approved manifest declares under
    ``create``: ``{created, kept, skipped: [{path, reason}]}``. Blocking
    (a worker thread's job); never raises."""
    from storage import database as task_store
    out: dict[str, list] = {FOLDERS_CREATED: [], FOLDERS_KEPT: [], FOLDERS_SKIPPED: []}
    try:
        approved = task_store.app_actions_approved(row)
    except psycopg.Error:
        logger.exception("App %s: the approval did not read back", row.get("slug"))
        return out
    if not approved:
        return out
    agent = row.get("agent") or ""
    for rel in declared_folders(row):
        target = f"{agent}/{rel}"
        try:
            st = safe_fs.lstat_beneath(config.AGENTS_DIR, target)
        except FileNotFoundError:
            st = None
        except OSError as e:
            out[FOLDERS_SKIPPED].append({"path": rel, "reason": _why(e)})
            continue
        if st is not None:
            if stat.S_ISDIR(st.st_mode):
                out[FOLDERS_KEPT].append(rel)
            else:
                blocker = safe_fs.SymlinkRefused() if stat.S_ISLNK(st.st_mode) else FileExistsError()
                out[FOLDERS_SKIPPED].append({"path": rel, "reason": _why(blocker)})
            continue
        try:
            safe_fs.mkdirs_beneath(config.AGENTS_DIR, target)
        except OSError as e:
            out[FOLDERS_SKIPPED].append({"path": rel, "reason": _why(e)})
            continue
        out[FOLDERS_CREATED].append(rel)
    if out[FOLDERS_CREATED] or out[FOLDERS_SKIPPED]:
        logger.info("App %s: folders created=%s skipped=%s", row.get("slug"),
                    out[FOLDERS_CREATED], [s["path"] for s in out[FOLDERS_SKIPPED]])
    return out


def folder_states(row: dict) -> list[dict]:
    """Each declared ``create`` folder as it stands now: present, missing
    or blocked (a file or a link in the way)."""
    agent = row.get("agent") or ""
    out = []
    for rel in declared_folders(row):
        try:
            st = safe_fs.lstat_beneath(config.AGENTS_DIR, f"{agent}/{rel}")
        except FileNotFoundError:
            state = FOLDER_MISSING
        except OSError:
            state = FOLDER_BLOCKED
        else:
            state = FOLDER_PRESENT if stat.S_ISDIR(st.st_mode) else FOLDER_BLOCKED
        out.append({"path": rel, "state": state})
    return out


def declares_folders(row: dict) -> bool:
    """Whether the row's manifest lists a folder to create (a read of the
    stored block, no owner lookup)."""
    from services.apps import manifest as _mf
    return bool(_mf.parse_files(row)[FILES_CREATE])
