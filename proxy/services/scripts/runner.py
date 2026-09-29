"""The script runner (APPS.md "Steps", CHECKS.md "The script kind"): runs a
script text where an identity's sessions run — the local sandbox built by
``resolve_sandbox_config`` + ``build_command_prefix`` with that identity's
mount row (never ``/config``, no MCP, the script's own folder read-only), or
a machine through the satellite's ``step_run`` frame (the private steps
directory there, the hash checked, the output streamed back). No model. The
verdict is the exit code and the first 32 KB of output; every value the
platform handed the script is scrubbed from what it printed.

The two callers keep their own vocabulary (a delivery, a check run) and
hand this module a ``ScriptSpec``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shlex
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core import placement
from typing import Awaitable, Callable

import config
from core import layout

logger = logging.getLogger("claude-proxy.scripts")

OUTPUT_KEEP_BYTES = 32 * 1024
TAIL_KEEP_BYTES = 4096
KILL_GRACE_S = 5.0
# The satellite's ack has the script's timeout plus this to arrive.
REMOTE_MARGIN_S = 60
REDACTED = "[redacted]"


class ScriptRefused(Exception):
    """The script must not run; the reason is the verdict's."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ScriptUnavailable(Exception):
    """The place the script runs is not there right now (a machine offline)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ScriptResult:
    exit_code: int | None
    output: str
    timed_out: bool
    ran_on: str
    seconds: float


@dataclass
class ScriptSpec:
    """Everything one run needs, resolved by the caller."""
    run_id: str                      # a UUID: the satellite's private directory name
    agent: str
    script: bytes
    sha256: str
    run_name: str                    # the file name the script is written as
    # The identity's mount row (the local sandbox).
    role: str
    username: str                    # the mount username ("" agent scope)
    user_sub: str = ""
    is_admin_agent: bool = False
    mount_shared: bool = True
    knowledge_rw: bool = False
    read_only: bool = False
    default_scope: str = ""
    available_scopes: tuple[str, ...] = ()
    # Where the script runs: the tree relative to the agent folder on a
    # machine (the sandbox derives its own), or an absolute machine folder
    # for a session that works outside the trees (``cwd_absolute``).
    workspace_relative: str = "workspace"
    knowledge_relative: str = "knowledge"
    cwd_absolute: str = ""
    # The script's own folder, read-only inside the sandbox at ``mount_at``.
    script_dir: Path | None = None
    mount_at: str = "/script"
    # What the script gets: the env (secrets included — scrubbed after), the
    # payload file's JSON and the env names that point at it.
    env: dict[str, str] = field(default_factory=dict)
    payload_json: str = "{}"
    payload_env: tuple[str, ...] = ("OTODOCK_STEP_PAYLOAD",)
    timeout: int = 60
    # Values that must never land in the output.
    secrets: list[str] = field(default_factory=list)
    # The scratch directory the local run writes the payload into.
    scratch_root: Path | None = None
    on_output: Callable[[str], Awaitable[None]] | None = None


# ── small pieces ────────────────────────────────────────────────────────────


def argv_for(script: bytes, path: str) -> list[str]:
    """The script's first line names its interpreter (``#!/usr/bin/env
    bash``); without one it runs under ``/bin/sh``. Read from the content,
    so a script needs no mode bit on either placement."""
    first = script.split(b"\n", 1)[0].strip()
    if first.startswith(b"#!"):
        words = shlex.split(first[2:].decode("utf-8", "replace"))
        if words:
            return [*words, path]
    return ["/bin/sh", path]


def scrub(text: str, secrets) -> str:
    """Every value the platform handed the script replaced in what it
    printed, so a token never lands on a row, in a log or in an event."""
    for s in secrets:
        if isinstance(s, str) and len(s) >= 8 and s in text:
            text = text.replace(s, REDACTED)
    return text


async def _read_capped(stream: asyncio.StreamReader) -> tuple[bytes, bytes, int]:
    head = bytearray()
    tail = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if len(head) < OUTPUT_KEEP_BYTES:
            head.extend(chunk[:OUTPUT_KEEP_BYTES - len(head)])
        tail.extend(chunk)
        if len(tail) > TAIL_KEEP_BYTES:
            del tail[:len(tail) - TAIL_KEEP_BYTES]
    return bytes(head), bytes(tail), total


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return


async def _reap(proc: asyncio.subprocess.Process) -> None:
    _kill_group(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        await proc.wait()


def output_text(head: bytes, total: int) -> str:
    text = head.decode("utf-8", "replace").replace("\x00", "")
    if total > len(head):
        text += f"\n… [{total - len(head)} more bytes not kept]"
    return text


# ── the local run ───────────────────────────────────────────────────────────


def build_local(spec: ScriptSpec) -> tuple[list[str], dict[str, str], Path]:
    """The argv (the netns launcher, bwrap with the identity's mount row,
    the script's folder read-only at ``mount_at`` and the run's scratch
    directory at ``/step``) and the env; synchronous."""
    from core.sandbox import oto_env
    from core.sandbox.sandbox import SandboxBuilder, SandboxMount, resolve_sandbox_config
    if spec.script_dir is None or spec.scratch_root is None:
        raise ScriptRefused("the local run needs the script's folder and a scratch root")
    agents_dir = Path(config.AGENTS_DIR).resolve()
    step_dir = Path(spec.scratch_root) / spec.run_id
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "payload.json").write_text(spec.payload_json, "utf-8")
    cfg = resolve_sandbox_config(
        role=spec.role, username=spec.username, agent_name=spec.agent,
        is_admin_agent=spec.is_admin_agent,
        host_claude_dir=agents_dir / spec.agent / layout.WORKSPACE / ".claude",
        user_sub=spec.user_sub or "",
        mcp_sandbox_mounts=[SandboxMount(str(spec.script_dir), spec.mount_at, "ro"),
                            SandboxMount(str(step_dir), "/step", "ro")],
        config_visible=False, mount_shared=spec.mount_shared, knowledge_rw=spec.knowledge_rw,
        mcp_dir_binds=[], read_only=spec.read_only,
    )
    # The working folder follows the session's scope: the person's workspace
    # for a personal session, the agent's for a shared one (the sandbox's own
    # default for a named person is their tree's root, not a workspace).
    scope = spec.default_scope or ("user" if spec.username else "agent")
    personal = bool(spec.username) and scope == "user"
    cfg = dataclasses.replace(
        cfg, app_cwd=f"{layout.virtual_user_root(spec.username)}/{layout.WORKSPACE}" if personal else layout.V_WORKSPACE,
    )
    builder = SandboxBuilder(cfg)
    inner = argv_for(spec.script, f"{spec.mount_at}/{spec.run_name}")
    argv = builder.build_command_prefix(inner)
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"}
    env.update(oto_env.build_oto_env(
        agent_name=spec.agent, username=spec.username, user_sub=spec.user_sub or "",
        user_role=spec.role, session_id="",
        default_scope=spec.default_scope or ("user" if spec.username else "agent"),
        task_type=spec.env.get("OTO_TASK_TYPE", "step"),
        available_scopes=spec.available_scopes or (("user", "agent") if spec.mount_shared else ("user",)),
    ))
    env.update(spec.env)
    env["OTODOCK_PROXY_URL"] = f"http://127.0.0.1:{config.PORT}"
    for name in spec.payload_env:
        env[name] = "/step/payload.json"
    env["OTODOCK_WORKSPACE_DIR"] = builder.get_cwd()
    env["OTODOCK_KNOWLEDGE_DIR"] = layout.V_KNOWLEDGE if (spec.knowledge_relative and spec.mount_shared) else ""
    return argv, env, step_dir


async def run_local(spec: ScriptSpec) -> ScriptResult:
    argv, env, step_dir = await asyncio.to_thread(build_local, spec)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True,
    )
    timed_out = False
    head, total = b"", 0
    try:
        assert proc.stdout is not None
        head, _tail, total = await asyncio.wait_for(_read_capped(proc.stdout), timeout=spec.timeout)
        await asyncio.wait_for(proc.wait(), timeout=max(1.0, spec.timeout - (time.monotonic() - started)))
    except asyncio.TimeoutError:
        timed_out = True
        await _reap(proc)
    except asyncio.CancelledError:
        await _reap(proc)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, step_dir, True)
    return ScriptResult(exit_code=None if timed_out else proc.returncode,
                        output=scrub(output_text(head, total), spec.secrets), timed_out=timed_out,
                        ran_on=placement.LOCAL, seconds=time.monotonic() - started)


# ── the remote run (satellite 0.5.122 ``step_run``; 0.5.123 the two fields) ─


def remote_frame(spec: ScriptSpec, *, with_checks_fields: bool) -> dict:
    frame = {
        "type": "step_run",
        "delivery_id": spec.run_id,
        "app_id": spec.env.get("OTODOCK_APP_ID", ""),
        "agent_slug": spec.agent,
        "run": spec.run_name,
        "script": spec.script.decode("utf-8"),
        "sha256": spec.sha256,
        "workspace_relative": spec.workspace_relative,
        "knowledge_relative": spec.knowledge_relative,
        "env": dict(spec.env),
        "payload_json": spec.payload_json,
        "timeout": int(spec.timeout),
    }
    if with_checks_fields:
        extra = [n for n in spec.payload_env if n != "OTODOCK_STEP_PAYLOAD"]
        if extra:
            frame["payload_env"] = extra
        if spec.cwd_absolute:
            frame["cwd_absolute"] = spec.cwd_absolute
    return frame


async def run_remote(spec: ScriptSpec, machine_id: str, *, needs_checks_fields: bool = False) -> ScriptResult:
    """One ``step_run`` frame to the machine; the ack carries the verdict.
    ``needs_checks_fields``: the run needs 0.5.123's fields (an absolute
    cwd, an extra payload env name) and is refused below it."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        raise ScriptUnavailable("the machine is offline")
    if not cm.satellite_supports_steps(machine_id):
        raise ScriptRefused(
            f"this machine's satellite is too old for scripts "
            f"({cm.satellite_version(machine_id) or 'unknown'} < 0.5.122) — it updates at "
            "its next reconnect")
    supports_checks = getattr(cm, "satellite_supports_checks", None)
    with_fields = bool(supports_checks(machine_id)) if supports_checks else False
    if needs_checks_fields and not with_fields:
        raise ScriptRefused(
            f"this machine's satellite is too old for a check outside the synced tree "
            f"({cm.satellite_version(machine_id) or 'unknown'} < 0.5.123)")
    frame = remote_frame(spec, with_checks_fields=with_fields)
    command_id = f"step-{uuid.uuid4().hex[:12]}"
    started = time.monotonic()

    async def _on_output(text: str) -> None:
        if spec.on_output is not None:
            await spec.on_output(scrub(text, spec.secrets))

    cm.register_step_output(command_id, _on_output)
    try:
        ack = await cm.send_command(machine_id, frame, timeout=float(spec.timeout) + REMOTE_MARGIN_S,
                                    command_id=command_id)
    except RuntimeError as e:
        text = str(e)
        if "not connected" in text:
            raise ScriptUnavailable("the machine is offline")
        if "command timeout" in text:
            raise ScriptRefused(f"the machine did not answer within {spec.timeout} s")
        if "Satellite command error" in text:
            raise ScriptRefused(text.split(":", 1)[-1].strip() or "the satellite refused the script")
        raise ScriptRefused("the machine's link dropped while the script ran")
    finally:
        cm.unregister_step_output(command_id)
    code = ack.get("exit_code")
    return ScriptResult(exit_code=int(code) if isinstance(code, int) else None,
                        output=scrub(str(ack.get("output") or ""), spec.secrets),
                        timed_out=bool(ack.get("timed_out")), ran_on=machine_id,
                        seconds=time.monotonic() - started)


def payload_text(payload) -> str:
    return json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True,
                      separators=(",", ":"), default=str)
