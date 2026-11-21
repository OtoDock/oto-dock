"""The script runner (APPS.md "Steps", CHECKS.md "The script kind"): runs a
script text where an identity's sessions run, on a machine, through the
satellite's ``step_run`` frame. A compute machine beside the platform gets
the description of the sandbox ``resolve_sandbox_config`` builds with that
identity's mount row (never ``/config``, no MCP, the script's own folder
read-only) and reads the script from the agent's folder. A paired machine
gets the script's bytes (the private steps directory there, the hash
checked). The output streams back. No model. The verdict is the exit code
and the first 32 KB of output. Every value the platform handed the script is
scrubbed from what it printed.

The two callers keep their own vocabulary (a delivery, a check run) and
hand this module a ``ScriptSpec``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import shlex
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import config
from core import layout

logger = logging.getLogger("otodock.scripts")

OUTPUT_KEEP_BYTES = 32 * 1024
# The satellite's ack has the script's timeout plus this to arrive.
REMOTE_MARGIN_S = 60
REDACTED = "[redacted]"
# The script's hashed bytes in the run's own folder (``_stage_run``).
STAGED_SCRIPT = "script"


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
    # The identity's mount row (the sandbox a compute machine renders).
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
    # The scratch directory a compute machine's run reads its payload from.
    scratch_root: Path | None = None
    on_output: Callable[[str], Awaitable[None]] | None = None


# ── small pieces ────────────────────────────────────────────────────────────


class MissingInterpreter(Exception):
    """A script's first line names an interpreter this computer lacks;
    ``args[0]`` names it as a person reads it (``bash``, ``Python``, the
    word itself)."""


_ENV_WORD = "env"
_SHELL_WORDS = ("sh", "bash", "dash")
_PYTHON_WORDS = ("python3", "python")


def argv_for(script: bytes, path: str, *, windows: dict[str, str] | None = None,
             which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """The script's first line names its interpreter (``#!/usr/bin/env
    bash``); without one it runs under ``/bin/sh``. Read from the content,
    so a script needs no mode bit on either placement.

    ``windows``: the interpreters a Windows machine has, by kind (the
    satellite's ``host/interpreters``; the platform's own runner runs Linux
    and passes none). There a shell line and the default run under Git
    Bash (the path given with forward slashes), a Python line under the
    machine's Python, an ``env`` line's assignments and ``-u NAME``
    removals through Git's ``env.exe`` (its other options were already
    applied by the split or change nothing there), and any other word as
    the program PATH names; one the
    machine lacks, or a ``.cmd``/``.bat`` it cannot start without a shell,
    raises ``MissingInterpreter``."""
    first = script.split(b"\n", 1)[0].strip()
    words = shlex.split(first[2:].decode("utf-8", "replace")) if first.startswith(b"#!") else []
    if windows is None:
        return [*words, path] if words else ["/bin/sh", path]
    word, flags = (words[0], words[1:]) if words else ("sh", [])
    env_args: list[str] = []
    if re.split(r"[\\/]", word)[-1] == _ENV_WORD:
        while flags and (flags[0].startswith("-") or "=" in flags[0]):
            opt = flags.pop(0)
            if not opt.startswith("-"):
                env_args.append(opt)
            elif opt in ("-u", "--unset") and flags:
                env_args += [opt, flags.pop(0)]
            elif opt.startswith(("-u", "--unset=")):
                env_args.append(opt)
        if not flags:
            raise MissingInterpreter(_ENV_WORD)
        word, flags = flags[0], flags[1:]
    name = re.split(r"[\\/]", word)[-1]
    if name in _SHELL_WORDS:
        exe, path = windows.get("bash" if name == "bash" else "sh", ""), path.replace("\\", "/")
        if not exe:
            raise MissingInterpreter("bash")
    elif name in _PYTHON_WORDS:
        exe = windows.get("python", "")
        if not exe:
            raise MissingInterpreter("Python")
    else:
        exe = which(word) or which(name) or ""
        if not exe or exe.lower().endswith((".cmd", ".bat")):
            raise MissingInterpreter(name)
    if env_args:
        if not windows.get("env"):
            raise MissingInterpreter("bash")
        return [windows["env"], *env_args, exe, *flags, path]
    return [exe, *flags, path]


def scrub(text: str, secrets) -> str:
    """Every value the platform handed the script replaced in what it
    printed, so a token never lands on a row, in a log or in an event."""
    for s in secrets:
        if isinstance(s, str) and len(s) >= 8 and s in text:
            text = text.replace(s, REDACTED)
    return text


# ── the sandbox of a run ────────────────────────────────────────────────────


def _stage_run(spec: ScriptSpec, step_dir: Path) -> Path:
    """The run's own folder beneath the agent's real folder
    (``core.agent_folder``), with no link followed: the payload, and the
    script's bytes as they were read and hashed (``STAGED_SCRIPT``), which
    the sandbox binds over the script's name. The staged file's path."""
    from core import agent_folder
    from services.infra import safe_fs
    try:
        root, rel = agent_folder.beneath_agent(step_dir, spec.agent)
        safe_fs.mkdirs_beneath(root, rel)
        safe_fs.atomic_write_beneath(root, f"{rel}/payload.json", spec.payload_json.encode("utf-8"),
                                     mode=0o644, fsync=False)
        safe_fs.atomic_write_beneath(root, f"{rel}/{STAGED_SCRIPT}", spec.script, mode=0o644, fsync=False)
    except OSError as e:
        raise ScriptRefused(f"the run's folder could not be made: {type(e).__name__}") from e
    return step_dir / STAGED_SCRIPT


def remove_run_folder(agent: str, step_dir: Path) -> None:
    """A run's folder removed beneath the agent's real folder, no link
    followed on the way; best-effort, as the run is over. Blocking."""
    from core import agent_folder
    from services.infra import safe_fs
    try:
        safe_fs.rmtree_beneath(*agent_folder.beneath_agent(step_dir, agent), missing_ok=True)
    except OSError:
        logger.warning("scripts: the run's folder %s was not removed", step_dir, exc_info=True)


def _build_sandbox(spec: ScriptSpec):
    """The sandbox of a run on a compute machine: ``(the description that
    was checked, the inner command, the env, the scratch directory)``. The
    payload and the script's hashed bytes are staged into that directory,
    which sits in the agent's folder (so the machine sees it); the staged
    script is bound read-only over the script's name, after the script's own
    folder, so the bytes run are the bytes judged and ``$0`` and the
    script's siblings keep their paths. The folder is bound at its own place
    beneath the agent's real folder, never where a link at it or above it
    leads. Synchronous."""
    from core import agent_folder
    from core.sandbox import oto_env, sandbox_render
    from core.sandbox.sandbox import (
        SandboxBuilder,
        SandboxMount,
        resolve_sandbox_config,
    )
    if spec.script_dir is None or spec.scratch_root is None:
        raise ScriptRefused("a run on a compute machine needs the script's folder and a scratch root")
    agents_dir = Path(config.AGENTS_DIR).resolve()
    step_dir = Path(spec.scratch_root) / spec.run_id
    try:
        staged = _stage_run(spec, step_dir)
        run_at = f"{spec.mount_at}/{spec.run_name}"
        cfg = resolve_sandbox_config(
            role=spec.role, username=spec.username, agent_name=spec.agent,
            is_admin_agent=spec.is_admin_agent,
            host_claude_dir=agents_dir / spec.agent / layout.WORKSPACE / ".claude",
            user_sub=spec.user_sub or "",
            mcp_sandbox_mounts=[SandboxMount(str(spec.script_dir), spec.mount_at, "ro"),
                                SandboxMount(str(staged), run_at, "ro"),
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
        desc = builder.describe()
        binds = {b.sandbox: b for b in desc.extra_binds}
        if run_at not in binds:
            # A bind whose source is missing is left out at the build and at the
            # render: the run would then execute the folder's own file.
            raise ScriptRefused("the script could not be staged for its run")
        # The script's folder is bound as it was read: at its own place beneath
        # the agent's real folder, never where a link at it or above it leads.
        _root, folder_rel = agent_folder.beneath_agent(spec.script_dir, spec.agent)
        folder = binds.get(spec.mount_at)
        if folder is None or folder.root != sandbox_render.ROOT_AGENT or folder.rel != folder_rel:
            raise ScriptRefused("the script's folder is reached through a link")
        inner = argv_for(spec.script, run_at)
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
        return desc, inner, env, step_dir
    except BaseException:
        # A refusal at the staging or after it: the run never reaches the
        # step that removes its folder after.
        remove_run_folder(spec.agent, step_dir)
        raise


# ── the remote run (the satellite's ``step_run``) ───────────────────────────


def compute_frame(spec: ScriptSpec) -> tuple[dict, Path]:
    """``(the frame, the scratch directory to remove after)`` of a run on a
    compute machine: the sandbox as its description, the command to run in
    it and the env. The script itself stays in the agent's folder, staged
    read-only by ``spec.script_dir``. Synchronous."""
    desc, inner, env, step_dir = _build_sandbox(spec)
    return {
        "type": "step_run",
        "delivery_id": spec.run_id,
        "app_id": spec.env.get("OTODOCK_APP_ID", ""),
        "agent_slug": spec.agent,
        "run": spec.run_name,
        "sha256": spec.sha256,
        "sandbox": desc.to_dict(),
        "argv": inner,
        "env": env,
        "timeout": int(spec.timeout),
    }, step_dir


def remote_frame(spec: ScriptSpec) -> dict:
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
    extra = [n for n in spec.payload_env if n != "OTODOCK_STEP_PAYLOAD"]
    if extra:
        frame["payload_env"] = extra
    if spec.cwd_absolute:
        frame["cwd_absolute"] = spec.cwd_absolute
    return frame


async def run_remote(spec: ScriptSpec, machine_id: str) -> ScriptResult:
    """One ``step_run`` frame to the machine; the ack carries the verdict.
    A compute machine beside the platform reads the agent's folder itself: it
    takes the sandbox's description, and the scratch directory is removed
    here afterwards. One with its own copy holds neither: refused."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        raise ScriptUnavailable("the machine is offline")
    if cm.owns_copy(machine_id):
        raise ScriptRefused("a compute machine with its own copy runs no script")
    step_dir: Path | None = None
    if cm.shares_folders(machine_id):
        frame, step_dir = await asyncio.to_thread(compute_frame, spec)
    else:
        frame = remote_frame(spec)
        # The files a person or a platform tool just wrote (a deploy's, an
        # import's) reach this machine before the script reads them: the
        # turn's barrier, capped at ``BARRIER_TIMEOUT_S``.
        from core.remote import upload_inflight
        await upload_inflight.wait_settled(spec.agent, machine_id=machine_id,
                                           ceiling=upload_inflight.BARRIER_TIMEOUT_S)
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
            raise ScriptUnavailable("the machine is offline") from e
        if "command timeout" in text:
            raise ScriptRefused(f"the machine did not answer within {spec.timeout} s") from e
        if "Satellite command error" in text:
            raise ScriptRefused(text.split(":", 1)[-1].strip() or "the satellite refused the script") from e
        raise ScriptRefused("the machine's link dropped while the script ran") from e
    finally:
        cm.unregister_step_output(command_id)
        if step_dir is not None:
            await asyncio.to_thread(remove_run_folder, spec.agent, step_dir)
    code = ack.get("exit_code")
    return ScriptResult(exit_code=int(code) if isinstance(code, int) else None,
                        output=scrub(str(ack.get("output") or ""), spec.secrets),
                        timed_out=bool(ack.get("timed_out")), ran_on=machine_id,
                        seconds=time.monotonic() - started)


def payload_text(payload) -> str:
    return json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True,
                      separators=(",", ":"), default=str)
