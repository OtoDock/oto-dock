"""A session's own folders on this machine.

``<otodock_dir>/session-secrets/<session_id>/`` holds what a session's SSH
agent writes for it (``ssh_agent``: the public halves of its keys and its
socket's folder), everything 0700, removed at the session's close
(``wipe``). A satellite restart wipes the entire ``session-secrets/`` tree,
which also removes the private key files an older satellite fetched there,
and sweeps ``.credentials/`` dirs out of the agent tree, where an earlier
release may have left a token file.

A compute machine on another host (``[satellite] files = own``) takes the
files the platform prepared for a session from its start frame
(``host_files``, keyed by the path below the agent's folder;
``materialize_agent_files``): written under its own agent folder before the
sandbox is rendered, never through a link, and removed at the close once no
live session of this machine still uses the same file (``wipe_agent_files``).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import shutil
import threading
from pathlib import Path

from .._vendored import layout
from ..config import otodock_dir

logger = logging.getLogger("satellite.session_files")


def _secrets_root() -> Path:
    return otodock_dir() / "session-secrets"


def _session_dir(session_id: str) -> Path:
    return _secrets_root() / session_id


#: Each agent-folder file this machine wrote for its sessions, as (the agent
#: folder, the path below it), and the live sessions it was written for (a
#: scope's settings serve every session of it).
_agent_files: dict[tuple[Path, str], set[str]] = {}
#: Held across a file's write and its registration, and across the close's
#: choice and removal: both run in worker threads, a start's beside a close's.
_agent_files_lock = threading.Lock()


def materialize_agent_files(config: dict, session_id: str, agent_dir: Path) -> int:
    """A session on a compute machine on another host: write the files the
    platform prepared for it (the start frame's ``host_files``) under the
    agent's folder, each in one rename with the mode it had there (the
    umask applies). Every folder and file is made beneath the agent folder
    (``host/safe_fs``: the folder by its realpath, nothing below it reached
    through a link, a link at a file's name replaced, never written
    through); an absolute path or a ``..`` is refused. Returns how many
    were written."""
    from ..host import safe_fs
    files = config.get("host_files") or {}
    if not isinstance(files, dict):
        return 0
    agent_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    with safe_fs.open_root(agent_dir) as root:
        # The folders the sandbox binds that this copy lacks (a hidden folder
        # the sync never carries): made empty, never through a link.
        for rel in config.get("host_folders") or ():
            try:
                if not rel:
                    raise ValueError("an empty path")
                safe_fs.mkdirs_beneath(root, str(rel))
            except (OSError, ValueError) as e:
                logger.warning("session-files: refusing folder %r for a compute session: %s", rel, e)
        for rel, entry in files.items():
            rel = str(rel)
            if not isinstance(entry, dict):
                continue
            try:
                content = base64.b64decode(entry.get("content_b64", ""))
            except (ValueError, TypeError):
                continue
            with _agent_files_lock:
                try:
                    mode = int(entry.get("mode") or 0o600) & 0o777
                    # A rename over a file a running sandbox binds by name
                    # detaches the bind there: an unchanged file stays.
                    if not _holds(safe_fs, root, rel, content, mode):
                        safe_fs.atomic_write_beneath(root, rel, content, mkdirs=True, fsync=False, mode=mode)
                except OSError as e:
                    logger.warning("session-files: refusing %r for a compute session: %s", rel, e)
                    continue
                _agent_files.setdefault((agent_dir, rel), set()).add(session_id)
            written += 1
    logger.info("session-files: wrote %d prepared file(s) for compute session %s", written, session_id[:8])
    return written


def _holds(safe_fs, root, rel: str, data: bytes, mode: int) -> bool:
    """Whether the file at ``rel`` already holds ``data``, executable as
    ``mode`` says, reached with no link followed."""
    import stat
    try:
        fd, st = safe_fs.open_regular_for_read(root, rel, max_size=len(data))
    except OSError:
        return False
    with os.fdopen(fd, "rb") as fh:
        held = fh.read(len(data) + 1)
    return held == data and bool(stat.S_IMODE(st.st_mode) & 0o111) == bool(mode & 0o111)


def wipe_agent_files(session_id: str) -> None:
    """At a compute session's close: the files written for it that no other
    live session of this machine uses go, each removed beneath its agent
    folder with no link followed (a folder swapped for one since the start
    refuses the removal)."""
    from ..host import safe_fs
    with _agent_files_lock:
        for key, users in list(_agent_files.items()):
            users.discard(session_id)
            if not users:
                _agent_files.pop(key, None)
                agent_dir, rel = key
                try:
                    safe_fs.unlink_beneath(agent_dir, rel, missing_ok=True)
                except OSError as e:
                    logger.warning("session-files: leaving %s in %s: %s", rel, agent_dir, e)


def wipe(session_id: str) -> None:
    """Remove a session's own folder (session close)."""
    shutil.rmtree(_session_dir(session_id), ignore_errors=True)


def wipe_all() -> None:
    """Startup sweep: a satellite restart killed every session it was
    running, so the whole tree is dead secret material — remove it."""
    shutil.rmtree(_secrets_root(), ignore_errors=True)


def purge_agent_tree_credentials(agents_dir: Path | None) -> None:
    """Startup sweep: remove every ``.credentials/`` dir from the agent tree:
    what an earlier release left there (per-session token files of a session
    that crashed, the persistent copies of older file syncs)."""
    if not agents_dir or not agents_dir.is_dir():
        return
    removed = 0
    for pattern in (f"*/{layout.KNOWLEDGE}/{layout.CREDENTIALS_DIR}",
                    f"*/{layout.USERS}/*/{layout.CREDENTIALS_DIR}"):
        for d in agents_dir.glob(pattern):
            # The glob passes through a person's own link (``users/<u>``):
            # only a folder reached with no link from the agents folder goes.
            # This runs before any session, so nothing swaps a part after.
            if not _folder_beneath(agents_dir, d):
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        logger.info(
            "session-files: purged %d agent-tree .credentials dir(s)", removed,
        )


def purge_agent_tree_mcp_configs(agents_dir: Path | None) -> None:
    """Startup sweep: remove every session MCP config copy from the agent
    tree. They are rewritten at every session start, and the copies an
    earlier satellite wrote carried vendor bearers inline. A Codex home's
    profiles and session files go too (no session runs at startup), and its
    ``AGENTS.md``: every session of the home would read one left behind."""
    if not agents_dir or not agents_dir.is_dir():
        return
    removed = 0
    codex_names = ("config.toml", "*.config.toml", "*.session.json", "AGENTS.md", "AGENTS.override.md")
    from ..host import safe_fs
    for scope in (f"*/{layout.USERS}/*", f"*/{layout.WORKSPACE}"):
        for pattern in (f"{scope}/.claude/mcp-config*.json",
                        *(f"{scope}/.codex/{name}" for name in codex_names)):
            for f in agents_dir.glob(pattern):
                # The glob passes through a link (a session's own engine
                # folder may be one): the removal never does.
                with contextlib.suppress(OSError, ValueError):
                    safe_fs.unlink_beneath(agents_dir, f.relative_to(agents_dir).as_posix())
                    removed += 1
    if removed:
        logger.info("session-files: purged %d session MCP config copy(ies)", removed)


def _folder_beneath(root: Path, path: Path) -> bool:
    """``path`` is a folder reached beneath ``root`` with no link on the way."""
    import stat

    from ..host import safe_fs
    try:
        return stat.S_ISDIR(safe_fs.lstat_beneath(root, path.relative_to(root).as_posix()).st_mode)
    except (OSError, ValueError):
        return False
