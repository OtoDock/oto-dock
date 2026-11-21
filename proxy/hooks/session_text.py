#!/usr/bin/env python3
"""Codex SessionStart hook: a resumed thread gets its session's text once.

A Codex session takes its text as ``developer_instructions`` (the terminal's
profile, the app-server's ``developerInstructions``), which Codex sends only
when a thread starts or after a compaction. A thread resumed by a later
session of the same chat keeps the text it holds: when this session's text
is another (a tool added, the persona edited) or the thread holds none of
the platform's (a thread from before the per-session text), this hook adds
the text once, as a developer message after the history, and records its
hash.

Reads JSON from stdin (``session_id`` = the thread, ``source``, and for a
fork ``transcript_path``, whose first line names the parent), the session's
own file in ``CODEX_HOME`` (``<OTO_SESSION_ID>.session.json``, written by the
platform or the satellite: the session's home when it has one) and the
thread's record (``<thread>.text-sha256``) in the store, ``OTO_HOOKS_DIR``,
which outlives a home (``CODEX_HOME`` when that is unset). Stdlib only, no
network. Prints nothing and exits
0 on anything unexpected: the thread then keeps what it holds, and the next
start tries again (no record was written).

The names repeat ``core/layers/codex/session_text.py`` (a test holds them
equal): this script runs alone, inside the engine's sandbox.

Environment: ``CODEX_HOME``, ``OTO_SESSION_ID``, ``OTO_HOOKS_DIR`` (set on
the engine's process and inherited by its hooks).
"""

import contextlib
import hashlib
import json
import os
import re
import stat
import sys

SESSION_SUFFIX = ".session.json"
RECORD_SUFFIX = ".text-sha256"
REPLACEMENT_NOTE = "These instructions replace every earlier instruction in this conversation."
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The sources whose new context carries the session's text: record only.
_RECORD_ONLY = ("startup", "clear", "compact")


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _open(path):
    """A text file of the home, opened without following a link or blocking
    on a FIFO a neighbour planted; OSError for anything but a regular file."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        return os.fdopen(fd, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _read(path):
    with _open(path) as f:
        return f.read()


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def _held(home, thread):
    """The hash the thread's record holds, "" when it has none."""
    try:
        return _read(os.path.join(home, thread + RECORD_SUFFIX)).strip()
    except OSError:
        return ""


def _fork_parent(transcript_path):
    """The thread a fork was made from: its rollout's first line names it
    (``session_meta.forked_from_id``); "" when it cannot be read."""
    try:
        with _open(transcript_path) as f:
            parent = json.loads(f.readline())["payload"]["forked_from_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return parent if isinstance(parent, str) and _NAME_RE.fullmatch(parent) else ""


def decide(home, session_id, thread, source, transcript_path="", store=""):
    """The text to add (or "") and the text the thread then holds, to
    record (or "" to record nothing). The session's text is read in
    ``home``, the threads' records in ``store`` (``home`` when it is "").
    Pure but for the reads."""
    records = store or home
    if not (_NAME_RE.fullmatch(session_id or "") and _NAME_RE.fullmatch(thread or "")):
        return "", ""
    try:
        data = json.loads(_read(os.path.join(home, session_id + SESSION_SUFFIX)))
    except (OSError, ValueError):
        return "", ""
    text = data.get("developer_instructions") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text:
        return "", ""
    if source in _RECORD_ONLY:
        return "", text
    if source == "fork":
        # A fork holds its parent's history: recorded only when the parent
        # held this text, so a later resume of the fork gets a newer one.
        parent = _fork_parent(transcript_path) if transcript_path else ""
        return "", text if parent and _held(records, parent) == _text_hash(text) else ""
    if source != "resume" or _held(records, thread) == _text_hash(text):
        return "", ""
    return f"{REPLACEMENT_NOTE}\n\n{text}", text


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    home = os.environ.get("CODEX_HOME", "")
    store = os.environ.get("OTO_HOOKS_DIR", "") or home
    session_id = os.environ.get("OTO_SESSION_ID", "")
    thread = str(event.get("session_id") or "")
    if not home:
        return
    add, record = decide(home, session_id, thread, str(event.get("source") or ""),
                         str(event.get("transcript_path") or ""), store)
    if add:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": add}}))
        sys.stdout.flush()
    if record:
        # Written only after the text went out: a hook cut before this line
        # leaves no record, and the next start adds the text again, which is
        # the safe side.
        with contextlib.suppress(OSError):
            _write(os.path.join(store, thread + RECORD_SUFFIX), _text_hash(record) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook never fails the session
        sys.exit(0)
