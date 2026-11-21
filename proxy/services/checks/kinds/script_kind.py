"""The script kind (CHECKS.md "The script kind"): the check's script runs
through the platform's script runner with the judged session's own
identity and mounts — the worker's authority, no escalation — where that
session runs: the session's machine through the ``step_run`` frame, or for
the platform's place the compute machine that serves it (with none, the
script does not run: the platform runs no script itself). The check's folder
is read-only at ``/check``; the
changed set is the payload (``OTODOCK_CHECK_INPUT``); no token and no
provider credentials. Exit 0 passes; a non-zero exit fails with the last
lines as the finding, unless the output ends with a JSON verdict block;
a timeout or a refusal is an ``error``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
import uuid
from pathlib import Path

import config
from core import placement
from services.checks import changed_set as cs, kinds
from services.checks.kinds import judge_kind
from services.checks.kinds.schema_kind import last_json_block
from services.checks.render import Verdict, parse_verdict_json
from services.infra.safe_fs import FileTooLarge, NotRegularFile, SymlinkRefused
from services.scripts import runner
from storage.checks import db_checks

logger = logging.getLogger("checks")

TAIL_LINES = 40
INPUT_ENV = "OTODOCK_CHECK_INPUT"
SCRATCH_DIR_NAME = "check-runs"


def _read_script(check) -> tuple[bytes, str]:
    """The script from the check's folder, read with no link followed, its
    hash checked against the one the check's load took and the index's (a
    hand edit is re-indexed at load; a script that changed under a running
    evaluation is refused, with or without an index row)."""
    from services.checks import documents
    path = check.script_path()
    if path is None:
        raise runner.ScriptRefused("the check has no script")
    try:
        data = documents.read_script(check)
    except FileTooLarge as e:
        raise runner.ScriptRefused("the script is larger than 256 KB") from e
    except (FileNotFoundError, SymlinkRefused, NotRegularFile) as e:
        raise runner.ScriptRefused(f"the script {path.name} is missing") from e
    except OSError as e:
        raise runner.ScriptRefused(f"the script cannot be read: {type(e).__name__}") from e
    sha = hashlib.sha256(data).hexdigest()
    row = db_checks.get_index(check.agent, check.owner, check.name)
    known = [s for s in (check.script_sha256, (row or {}).get("script_sha256")) if s]
    if any(s != sha for s in known):
        raise runner.ScriptRefused("the script changed since it was indexed — save the check again")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise runner.ScriptRefused("the script is not UTF-8 text") from exc
    return data, sha


def build_spec(check, target, changed: dict, *, script: bytes, sha256: str) -> runner.ScriptSpec:
    sec = target.security
    mount_shared = bool(getattr(sec, "mount_shared", True))
    # A shared session of a logged-in person still names the person; its
    # folder is the agent's workspace, not the person's (which a machine
    # may not even hold).
    return runner.ScriptSpec(
        run_id=str(uuid.uuid4()), agent=target.agent, script=script, sha256=sha256,
        run_name=str(check.doc["script"]["run"]),
        role=target.role, username=target.username, user_sub=target.user_sub,
        is_admin_agent=bool(getattr(sec, "is_admin_agent", False)),
        mount_shared=mount_shared,
        knowledge_rw=bool(getattr(sec, "knowledge_rw", False)),
        default_scope=target.scope,
        workspace_relative=cs.workspace_relative_of(target),
        knowledge_relative="knowledge" if mount_shared else "",
        cwd_absolute=target.work_cwd or "",
        script_dir=check.folder, mount_at="/check",
        env={
            "OTO_TASK_TYPE": "check",
            "OTODOCK_CHECK_NAME": check.name,
            "OTODOCK_CHECK_ROUND": str(changed.get("round") or 1),
            "OTODOCK_CHECK_SESSION": target.session_id,
            "OTODOCK_CHECK_CHAT": target.chat_id,
        },
        payload_json=runner.payload_text(changed),
        payload_env=(INPUT_ENV, "OTODOCK_STEP_PAYLOAD"),
        timeout=int(check.doc["script"].get("timeout") or 600),
        scratch_root=Path(config.AGENTS_DIR) / target.agent / SCRATCH_DIR_NAME,
    )


def verdict_of(res: runner.ScriptResult, *, sha256: str) -> Verdict:
    v = Verdict(section="script", ran_on=res.ran_on, script_sha256=sha256,
                duration_ms=int(res.seconds * 1000), status="error")
    if res.timed_out:
        v.reason = "the script timed out"
        return v
    parsed = parse_verdict_json(last_json_block(res.output or ""))
    if parsed is not None:
        passed, score, findings, summary = parsed
        # A failing exit is never overturned by a block: the output carries
        # text the judged agent wrote (a test's name, an assertion's
        # message), and a fenced block among it is as easy to print.
        if passed and res.exit_code != 0:
            passed = False
            summary = f"the script exited {res.exit_code}" + (f"; {summary}" if summary else "")
        v.status = "pass" if passed else "fail"
        v.passed, v.score, v.findings, v.summary = passed, score, findings, summary
        return v
    if res.exit_code == 0:
        v.status, v.passed = "pass", True
        v.summary = "the script passed"
        return v
    lines = [ln for ln in (res.output or "").strip().splitlines() if ln.strip()]
    tail = "\n".join(lines[-TAIL_LINES:])
    v.status = "fail"
    v.summary = f"the script exited {res.exit_code}"
    v.findings = [{"location": "", "severity": "error",
                   "text": tail[-400:] if tail else f"exit {res.exit_code} with no output"}]
    return v


async def _run_on_the_platforms_place(spec: runner.ScriptSpec) -> runner.ScriptResult:
    """A script of the platform's place: on the compute machine that serves
    it (``app_compute.place``, an app step's own answer), in the sandbox the
    platform describes. With none, or one that left before the script
    reached it, the place is not there (``ScriptUnavailable``): the platform
    runs no script itself. It ran on the platform's place: a compute
    machine's id is no place a person picked, so the verdict never names it."""
    from services.apps import app_compute
    machine = app_compute.place()
    if machine is None:
        raise runner.ScriptUnavailable("the machine is offline")
    res = await runner.run_remote(spec, str(machine["id"]))
    return dataclasses.replace(res, ran_on=placement.LOCAL)


async def run(check, target, changed: dict, *, round_no: int) -> Verdict:
    started = time.monotonic()
    # A compute machine with its own copy holds neither the platform's
    # scratch folder nor the staged script: the check runs on the platform's
    # place once that machine's file changes of the turn have arrived.
    remote = target.on_machine and not target.placement.own_copy
    # A refusal names the place it came from too: a machine's "folder is
    # missing" must not read as "on the platform" on the card.
    place = target.placement.machine_id if remote else placement.LOCAL
    try:
        script, sha = await asyncio.to_thread(_read_script, check)
        spec = build_spec(check, target, changed, script=script, sha256=sha)
        if remote:
            res = await runner.run_remote(spec, target.placement.machine_id)
        else:
            if target.on_machine:
                await judge_kind._wait_sync_quiet(target.placement.machine_id, target.agent)
            if spec.cwd_absolute:
                raise runner.ScriptRefused("the session works outside the platform's trees")
            res = await _run_on_the_platforms_place(spec)
    except runner.ScriptRefused as e:
        return Verdict(section="script", status="error", reason=e.reason, ran_on=place,
                       duration_ms=int((time.monotonic() - started) * 1000))
    except runner.ScriptUnavailable as e:
        return Verdict(section="script", status="error", reason=e.reason, ran_on=place,
                       duration_ms=int((time.monotonic() - started) * 1000))
    return verdict_of(res, sha256=sha)


kinds.register("script", run)
