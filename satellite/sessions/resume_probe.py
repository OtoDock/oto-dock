"""The resume probe: whether a Claude session's transcript holds a user
message, found and read beneath the agent's folder (``host/safe_fs``) with no
link followed, line by line under a cap. ``SessionManager``'s
``check_session_resumable`` answers with it, a compute machine too."""

from __future__ import annotations

import errno
import os
from pathlib import Path

# The read errors a resume probe answers as a definite "not resumable": a
# name nobody can load as a transcript (a planted directory, a dropped
# permission). The chat takes a fresh session instead of waiting on a file
# that will never open. Anything else (memory, I/O, a full disk) is the
# machine's moment and is answered as an error the platform asks again.
_PROBE_UNREADABLE_ERRNOS = frozenset({
    errno.EISDIR, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM,
})
# The two spellings claude-code has used for a user turn's line.
_PROBE_USER_MARKS = (b'"type":"user"', b'"type": "user"')
_PROBE_READ_BUFFER = 1024 * 1024
# How many entries ``projects`` may list before a probe could not tell (the
# platform's ``transcript_tailer._PROJECTS_MAX_ENTRIES``).
_PROBE_PROJECTS_MAX_ENTRIES = 10_000
# One transcript record is one line; a longer one is read past, never held
# (the platform's ``transcript_tailer._RECORD_MAX_BYTES``).
_PROBE_RECORD_MAX_BYTES = 64 * 1024 * 1024

def _probe_records(fh):
    """Each line of a binary file, or None for one longer than
    ``_PROBE_RECORD_MAX_BYTES`` (read past, never held)."""
    cap = _PROBE_RECORD_MAX_BYTES
    while True:
        rec = fh.readline(cap + 1)
        if not rec:
            return
        if len(rec) <= cap or rec.endswith(b"\n"):
            yield rec
            continue
        while True:
            more = fh.readline(_PROBE_READ_BUFFER)
            if not more or more.endswith(b"\n"):
                break
        yield None


def _open_probe(agent_dir: Path, claude_rel: str, session_id: str) -> tuple[str, int]:
    """``("", fd)`` of the session JSONL in ``<claude_rel>/projects/<p>/``
    beneath the agent's folder, else ``(reason, -1)``: the folder listed and
    the file opened there (``host/safe_fs``), a project folder that is a link
    never searched; a link, a folder or a special file at the name is
    "unreadable". A ``projects`` folder past ``_PROBE_PROJECTS_MAX_ENTRIES``
    entries raises ``TooManyEntries``."""
    from ..host import safe_fs
    projects = f"{claude_rel}/projects"
    name = f"{session_id}.jsonl"
    try:
        with safe_fs.open_root(agent_dir) as root:
            # The project-hash dir name varies by CWD; search all.
            listing = safe_fs.list_beneath(root, projects, max_entries=_PROBE_PROJECTS_MAX_ENTRIES)
            for project in listing.dirs:
                rel = f"{projects}/{project}/{name}"
                try:
                    safe_fs.lstat_beneath(root, rel)
                except (FileNotFoundError, NotADirectoryError, safe_fs.SymlinkRefused):
                    continue  # not here, or the project folder went or became a link
                return "", safe_fs.open_regular_for_read(root, rel)[0]
    except safe_fs.TooManyEntries:
        raise
    except FileNotFoundError:
        return "missing", -1
    except safe_fs.SafeFsError:
        return "unreadable", -1
    except OSError as e:
        if e.errno in _PROBE_UNREADABLE_ERRNOS:
            return "unreadable", -1
        raise
    return "missing", -1


def _probe_session_file(agent_dir: Path, claude_rel: str, session_id: str) -> tuple[bool, str, int]:
    """Whether the session JSONL of the agent's ``claude_rel`` holds a user
    message: ``(resumable, reason, lines_read)``. The file is found and
    opened beneath the agent's folder (``_open_probe``) and read line by line
    (one message a line), a line over ``_PROBE_RECORD_MAX_BYTES`` read past;
    the read stops at the first user line, so a transcript of any size costs
    one line of memory. A missing file is ``(False, "missing", 0)``, one with
    no user line ``(False, "empty", n)``, one that cannot be loaded for good
    ``(False, "unreadable", n)`` (``_PROBE_UNREADABLE_ERRNOS``, or a link or
    a special file at its name); any other ``OSError`` is raised, a
    ``projects`` folder past its cap among them. Runs in a worker thread."""
    reason, fd = _open_probe(agent_dir, claude_rel, session_id)
    if reason:
        return False, reason, 0
    lines = 0
    try:
        with os.fdopen(fd, "rb", buffering=_PROBE_READ_BUFFER) as fh:
            for rec in _probe_records(fh):
                lines += 1
                if rec is not None and any(mark in rec for mark in _PROBE_USER_MARKS):
                    return True, "", lines
    except OSError as e:
        if e.errno in _PROBE_UNREADABLE_ERRNOS:
            return False, "unreadable", lines
        raise
    return False, "empty", lines
