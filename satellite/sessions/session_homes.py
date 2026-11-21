"""A session's home on this machine: the folder its engine reads, inside its
scope's engine folder (the platform's ``core/sandbox/session_home.py``,
vendored), so two sessions of one scope run on two accounts at once.

The platform asks for a home in the start payload (``session_home``: the
engine's declaration of the links and the seeded files) and only of a
satellite that reports ``session_homes``; a start without it runs from the
scope's folder, as before. The scope's folder keeps everything else the
start writes (the hook scripts, the prompt file, the tools' config), so the
home holds only the session's own files: its sign-in, its account record
and its settings.
"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path

from .._vendored import layout, session_home
from ..config import write_if_changed

logger = logging.getLogger(__name__)

#: The engine folders this satellite makes homes in (its report's
#: ``session_home_dirs``).
ENGINE_DIRS = (".claude", ".codex")


def open_home(store: Path, session_id: str, spec: object) -> Path:
    """The folder the engine runs from: ``session_id``'s home in ``store``,
    made now, when the start asks for one; else ``store`` itself."""
    if not isinstance(spec, dict):
        return store
    seed = {str(name): tuple(str(k) for k in keys)
            for name, keys in (spec.get("seed") or {}).items()}
    return session_home.prepare(store, session_id, _links(spec), seed)


def home_path(store: Path, session_id: str, spec: object) -> Path:
    """The folder the engine runs from when another made it (the platform,
    for a compute machine that shares its folder): the session's home when
    the start asks for one, else ``store``."""
    if not isinstance(spec, dict) or not session_home.valid_id(session_id):
        return store
    return session_home.home_of(store, session_id)


def close_home(store: Path | None, session_id: str, spec: object) -> None:
    """Remove the session's home at its close when its start asked for one:
    its conversation stays in the store, and a later start of the same chat
    makes its home again."""
    if store is None or not isinstance(spec, dict) or not session_home.valid_id(session_id):
        return
    try:
        session_home.drop(store, session_id, _links(spec))
    except Exception:  # noqa: BLE001  # a home's removal must never stop a session's close
        logger.warning("session home of %s not removed", session_id[:8], exc_info=True)


def write_sign_in(folder: Path, filename: str, content: object, *, indent: int | None = None) -> None:
    """A sign-in file in ``folder``, mode 0600, written to a temp beside it
    and renamed onto its name: a link at the name is replaced, never
    followed (``config.write_if_changed``)."""
    write_if_changed(folder / filename, json.dumps(content, indent=indent).encode(), mode=0o600)


def sign_in_dir(parts: list[str], config_dir: str) -> bool:
    """Whether a pushed sign-in's folder (``credentials_update``) is one the
    platform writes: a scope's engine folder (``<scope>/.claude``), or a
    session's home in it (``<scope>/.claude/own/<session id>``)."""
    return bool(parts) and parts[-1] == config_dir or is_home_dir(parts, config_dir)


def is_home_dir(parts: list[str], config_dir: str) -> bool:
    """Whether a pushed sign-in's folder is a session's home: the one test
    the accept (``sign_in_dir``) and the write share, so a person named
    ``own`` keeps a scope (``users/own/.claude``)."""
    return (len(parts) >= 3 and parts[-3] == config_dir and parts[-2] == session_home.OWN
            and session_home.valid_id(parts[-1]))


def write_sign_in_beneath(agents_dir: Path, agent_slug: str, dir_relative: str,
                          filename: str, content: object, config_dir: str) -> Path | None:
    """A pushed sign-in file, mode 0600, written in the agent's folder with
    its folder opened by descriptor beneath it (``host/safe_fs``: a link on
    any part of the path is refused, also one swapped in after a check) and
    no link followed at its name. The folder written, or None for a
    session's home that is gone: a renewal that lands after the session's
    close makes no folder, not even the scope's (it would hold a live token
    nobody reads)."""
    import stat

    from .. import config as _config
    from ..host import safe_fs
    rel = f"{agent_slug}/{dir_relative}"
    data = json.dumps(content).encode()
    agents_dir.mkdir(parents=True, exist_ok=True)
    home = is_home_dir(dir_relative.split("/"), config_dir)
    if home:
        try:
            st = safe_fs.lstat_beneath(agents_dir, rel)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"a session's home is not a folder: {rel}")
    folder = agents_dir / agent_slug / dir_relative
    if not _config.HOST.posix:
        # Windows keeps the writer that falls back to writing in place: a
        # rename onto a file its engine holds open fails there, and no
        # descriptor walk exists to keep.
        if not home:
            safe_fs.mkdirs_beneath(agents_dir, rel)
        write_if_changed(folder / filename, data, mode=0o600)
        return folder
    target = f"{rel}/{filename}"
    if _holds_beneath(safe_fs, agents_dir, target, data):
        return folder
    # A killed satellite's half-written renewal holds a token: it goes once
    # it is older than any write in progress.
    safe_fs.sweep_temps_beneath(agents_dir, target, older_than_s=_config.STALE_TEMP_S)
    try:
        safe_fs.atomic_write_beneath(agents_dir, target, data, mkdirs=not home, mode=0o600)
    except FileNotFoundError:
        if home:
            return None
        raise
    except IsADirectoryError:
        # A folder at the file's name (any process of the computer's user
        # can make one): an empty one is removed beneath its parent and the
        # write runs once more; one that holds anything stays refused.
        try:
            safe_fs.rmdir_beneath(agents_dir, target)
        except OSError as e:
            raise FolderAtSignIn(errno.EISDIR, "a folder that is not empty holds the sign-in file's name",
                                 target) from e
        safe_fs.atomic_write_beneath(agents_dir, target, data, mkdirs=not home, mode=0o600)
    return folder


class FolderAtSignIn(IsADirectoryError):
    """A folder that is not empty sits at a pushed sign-in file's name: the
    push is refused, said in one line (never a traceback per renewal)."""


def _holds_beneath(safe_fs, root: Path, rel: str, data: bytes) -> bool:
    """Whether the file beneath ``root`` already holds ``data`` as a private
    file: an unchanged renewal renames nothing (a rename onto a file a
    session's sandbox holds a mount over would lift it)."""
    import os
    import stat
    try:
        fd, st = safe_fs.open_regular_for_read(root, rel, max_size=len(data))
    except OSError:
        return False
    with os.fdopen(fd, "rb") as fh:
        held = fh.read(len(data) + 1)
    return held == data and stat.S_IMODE(st.st_mode) == 0o600


def sweep_all(agents_dir: Path) -> int:
    """At the satellite's start every session of the last run is gone:
    remove the homes they left in every scope's engine folder here. The
    number removed."""
    removed = 0
    for store in _stores(agents_dir):
        try:
            removed += len(session_home.sweep(store, (), ()))
        except OSError:
            logger.warning("session homes under %s not swept", store, exc_info=True)
    return removed


def _links(spec: dict) -> list[str]:
    return [str(name) for name in spec.get("links") or ()]


def _stores(agents_dir: Path) -> list[Path]:
    """Every scope's engine folder under the agents' folder: the agent
    scope's and each person's, never through a link."""
    stores: list[Path] = []
    try:
        agents = [a for a in agents_dir.iterdir() if a.is_dir() and not a.is_symlink()]
    except OSError:
        return stores
    for agent in agents:
        roots = [agent / layout.WORKSPACE]
        users = agent / layout.USERS
        if users.is_dir() and not users.is_symlink():
            roots += [u for u in users.iterdir() if u.is_dir() and not u.is_symlink()]
        for root in roots:
            for name in ENGINE_DIRS:
                store = root / name
                if (store / session_home.OWN).is_dir() and not store.is_symlink():
                    stores.append(store)
    return stores
