"""A Codex session's own text and settings, kept in files no other session
of its Codex home writes.

A Codex home (``CODEX_HOME``) serves every session of one mount scope, so a
file every start rewrites (``AGENTS.md``, the tool sections of
``config.toml``) hands a running or starting session its neighbour's. Each
session gets its own files instead:

- ``<session>.config.toml``: the terminal's profile (``codex -p <session>``,
  layered over the home's ``config.toml``): the session's text as
  ``developer_instructions`` and its own settings;
- ``<session>.session.json``: the same text and the settings as the
  app-server takes them on ``thread/start`` and ``thread/resume``
  (``developerInstructions``, the ``config`` map), read by the compute
  machine's satellite and by the ``SessionStart`` hook
  (``hooks/session_text.py``);
- ``<thread>.text-sha256``: the hash of the text a thread holds, written by
  the hook, so a resumed thread gets a changed text once.

Stdlib only: vendored into the satellite (``scripts/sync-satellite-code.sh``),
which writes the same files on a paired computer. The hook script carries its
own copy of the names (it runs alone in the engine's sandbox); a test holds
the two equal.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

PROFILE_SUFFIX = ".config.toml"
SESSION_SUFFIX = ".session.json"
RECORD_SUFFIX = ".text-sha256"
# Files every session of a home would read: never written, removed at a start.
SHARED_TEXT_FILES = ("AGENTS.md", "AGENTS.override.md")

# Codex's profile names (``ProfileV2Name``): letters, digits, '_' and '-'.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# A session's files and a thread's record, by name in the home: host-local,
# never synced (``file_sync``), each host writes its own.
SESSION_FILE_RE = re.compile(r"([A-Za-z0-9_-]{1,128})(\.config\.toml|\.session\.json|\.text-sha256)")

# The homes this process wrote a session's files in, so its end removes them
# (a restart forgets them: the retention pass removes what is left).
_written: dict[str, Path] = {}


def self_hash() -> str:
    """SHA256 of this module's source: the satellite's drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def checked_name(name: str) -> str:
    """``name`` when it can name a profile and a file of the home, else
    ``ValueError`` (a session id or a thread id; never a path)."""
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(f"not a session or thread name: {name!r}")
    return name


def is_session_file(name: str) -> bool:
    """A session's profile or ``.session.json``, or a thread's record
    (``config.toml`` itself is not: it is the home's)."""
    return SESSION_FILE_RE.fullmatch(name) is not None and name != "config.toml"


def is_own_file(name: str, session_id: str) -> bool:
    """``name`` is ``session_id``'s own profile or ``.session.json``."""
    m = SESSION_FILE_RE.fullmatch(name)
    return m is not None and m.group(1) == session_id and m.group(2) != RECORD_SUFFIX


def profile_path(home: Path | str, session_id: str) -> Path:
    return Path(home) / f"{checked_name(session_id)}{PROFILE_SUFFIX}"


def session_path(home: Path | str, session_id: str) -> Path:
    return Path(home) / f"{checked_name(session_id)}{SESSION_SUFFIX}"


def record_path(home: Path | str, thread: str) -> Path:
    return Path(home) / f"{checked_name(thread)}{RECORD_SUFFIX}"


def toml_string(text: str) -> str:
    """``text`` as a TOML basic string. JSON escapes every control character
    TOML forbids raw except DEL, which TOML forbids too."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_session_files(home: Path | str, session_id: str, *, text: str,
                        config: dict, writer: Callable[[Path, bytes, int], object],
                        profile: str | None = None) -> None:
    """The session's own files: ``.session.json`` always, the terminal's
    profile when ``profile`` (its TOML, without the text) is given. The
    text rides the profile as ``developer_instructions``; an empty text
    leaves the key out.

    ``writer(path, data, mode)`` is the host's link-safe writer (the
    platform's ``session_config_dir.write_if_changed``, the satellite's
    ``config.write_if_changed``): the home is writable from inside the
    session, which knows its own id and can plant a link at any name."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    writer(session_path(home, session_id), json.dumps(
        {"developer_instructions": text, "config": config}, ensure_ascii=False).encode("utf-8"), 0o600)
    if profile is not None:
        head = f"developer_instructions = {toml_string(text)}\n\n" if text else ""
        writer(profile_path(home, session_id), (head + profile).encode("utf-8"), 0o600)
    _written[session_id] = home


@contextlib.contextmanager
def _in_home(home: Path | str):
    """``(dir_fd, name_of)`` for the names of a home: where the OS takes a
    folder descriptor, the home is opened once without following a link at
    its own name and every name is reached from that descriptor (a link at
    the home removes and reads nothing); elsewhere (Windows) the names are
    the paths under the home, as before."""
    if {os.open, os.stat, os.unlink} <= os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
        try:
            yield fd, lambda path: Path(path).name
        finally:
            os.close(fd)
    else:
        yield None, str


def read_session(home: Path | str, session_id: str) -> dict | None:
    """``{"developer_instructions": str, "config": dict}``, or None when the
    session has no file (a start that wrote none) or it cannot be read. The
    home is writable from inside a session: the file is opened from the
    home's own descriptor without following a link or blocking on a FIFO
    and read only when the descriptor is a regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with _in_home(home) as (dir_fd, name_of):
            fd = os.open(name_of(session_path(home, session_id)), flags, dir_fd=dir_fd)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as f:
            fd = -1
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        if fd != -1:
            os.close(fd)
    if not isinstance(data, dict):
        return None
    text = data.get("developer_instructions")
    config = data.get("config")
    return {"developer_instructions": text if isinstance(text, str) else "",
            "config": config if isinstance(config, dict) else {}}


def remove_shared_text(home: Path | str) -> None:
    """Remove the home's ``AGENTS.md`` and ``AGENTS.override.md``: Codex
    reads them for every session of the home, at every turn. An empty file
    stays: Codex skips it, and on the platform it is the mountpoint of the
    sandbox's read-only mask over the name, which removing would detach in
    every running sandbox of the home."""
    with contextlib.suppress(OSError), _in_home(home) as (dir_fd, name_of):
        for name in SHARED_TEXT_FILES:
            target = name_of(Path(home) / name)
            with contextlib.suppress(OSError):
                st = os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
                if not (stat.S_ISREG(st.st_mode) and st.st_size == 0):
                    os.unlink(target, dir_fd=dir_fd)


def drop(home: Path | str, session_id: str) -> None:
    """The session's files, at its end (the thread records stay: the next
    session of the chat resumes the same thread)."""
    _written.pop(session_id, None)
    paths = (profile_path(home, session_id), session_path(home, session_id))
    with contextlib.suppress(OSError), _in_home(home) as (dir_fd, name_of):
        for path in paths:
            with contextlib.suppress(OSError):
                os.unlink(name_of(path), dir_fd=dir_fd)


def forget(session_id: str) -> None:
    """``drop`` in the home this process wrote the session's files in; a
    session it wrote none for is left alone. Every Codex close calls it."""
    home = _written.get(session_id)
    if home is not None:
        drop(home, session_id)
