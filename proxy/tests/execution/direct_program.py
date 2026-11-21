"""The file program run in the test's own process, on real files.

For the handler tests that work on a temporary agents tree: the real
operations of ``file_program.py`` run here, and the session's mount table
stands where the kernel's mounts are (by text, the longest row first, a mask
as its empty folder, a read-only row refusing a write as the kernel does).
What the kernel itself adds is ``test_direct_files_sandboxed.py``'s.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.layers.direct import file_program, files as df
from core.sandbox.skills_materializer import _MAX_SKILL_BYTES

#: The operations that need a writable mount: the kernel refuses them under a
#: read-only one.
_WRITES = frozenset({"write", "edit", "stat"})


def _rows(session) -> list[tuple[str, str, bool]]:
    """``(sandbox path, host folder, writable)`` of each row the file tools reach."""
    return [(m.sandbox.rstrip("/") or "/", m.host, m.rw) for m in session.mount_table()
            if not any(part in df.REFUSED_COMPONENTS for part in m.sandbox.split("/"))]


def _longest(path: str, pairs: list[tuple[str, str]]) -> tuple[str, str] | None:
    best = None
    for here, there in pairs:
        if (path == here or path.startswith(here.rstrip("/") + "/")) and (best is None or len(here) >= len(best[0])):
            best = (here, there)
    return best


def _swap(path: str, pairs: list[tuple[str, str]]) -> str | None:
    """``path`` moved from the first folder of its longest pair to the second."""
    best = _longest(path, pairs)
    return None if best is None else best[1] + path[len(best[0]):]


def run(session, operation: str, args: dict) -> dict:
    """One operation of the file program on the session's own folders."""
    rows = _rows(session)
    facts = {
        "rows": [host for _sandbox, host, _rw in rows], "refused": sorted(df.REFUSED_COMPONENTS),
        "glob_skip": sorted(df.GLOB_SKIP_DIRS), "read_max": df.READ_MAX_BYTES, "glob_max": df.GLOB_MAX_ENTRIES,
        "write_max": df.WRITE_MAX_BYTES,
        "skills": str(Path(session.sandbox_cfg.host_claude_dir) / "skills"), "skill_max": _MAX_SKILL_BYTES,
    }
    args = dict(args)
    if "path" in args:
        there = [(sandbox, host) for sandbox, host, _rw in rows]
        host = _swap(args["path"], there)
        if host is None:
            return {"ok": False, "code": "not_a_session_folder"}
        row = _longest(args["path"], there)
        if operation in _WRITES and not next(rw for sandbox, _host, rw in reversed(rows) if sandbox == row[0]):
            # The kernel's word for a write under a read-only mount.
            return {"ok": False, "code": "read_only"}
        args["path"] = host
    answer = file_program.OPERATIONS[operation](facts, args)
    if isinstance(answer.get("landed"), str):
        answer = {**answer, "landed": _swap(answer["landed"], [(host, sandbox) for sandbox, host, _rw in rows])}
    return answer


def install(monkeypatch) -> list[tuple[str, dict]]:
    """Every session's file program runs in this process. Returns the calls
    made. What follows a call's answer (``then``) runs right after it, as
    the general part runs it once the program was asked: one that raises
    is told and the answer stands."""
    calls: list[tuple[str, dict]] = []

    async def call_program(session, operation, args, *, then=None):
        calls.append((operation, args))
        answer = run(session, operation, args)
        if then is not None:
            try:
                await then(answer)
            except Exception:  # as ``SandboxProgram._whole``: the record's own failure never changes the answer
                logging.getLogger("mcp-manager").exception("the stand-in program: what follows its answer failed")
        return answer

    monkeypatch.setattr(df, "call_program", call_program)
    return calls
