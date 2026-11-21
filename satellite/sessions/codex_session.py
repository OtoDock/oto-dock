"""Codex app-server session management for the satellite daemon.

Drives a persistent ``codex app-server`` JSON-RPC daemon (no bwrap), mirroring
the proxy's ``CodexAppServerSession`` but satellite-flavoured: native execution,
path translation on the prompt + env, and forwarding each app-server
notification verbatim as a ``session_event`` ``{method, params}`` for the proxy's
shared ``CodexEventTranslator``. Replaces the old per-turn ``codex exec`` model.

The transport (:class:`AppServerClient`) is vendored from the proxy via
``scripts/sync-satellite-code.sh`` so both sides share one NDJSON JSON-RPC client.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config
from .._vendored import layout, session_text
from .._vendored.app_server_client import (
    AppServerClient,
    AppServerError,
    mcp_server_names_from_toml,
    wait_for_mcp_startup,
)
from .._vendored.codex_approvals import (
    approval_policy,
    approvals_reviewer,
    build_sandbox_policy,
    make_server_request_handler,
)
from ..config import (
    codex_hook_command,
    otodock_dir,
    reap_descendants,
    session_tools_env,
    snapshot_descendants,
    write_command_wrappers,
    write_if_changed,
)
from ..host import env_hygiene, path_translator
from ..transport import file_sync
from . import sandbox_host, session_homes, start_slots
from .codex_files import (
    CODEX_HEADLESS_ROOT_KEYS,
    CODEX_MEMORIES_TABLE,
    CODEX_TOOLS_TABLE,
    LOCAL_PROVIDER,
    _validate_config_toml,
    chmod_private,
    drop_store_leftovers,
    features_table,
    local_provider_toml,
    runs_on_local_model,
    write_or_drop_auth_json,
    write_or_drop_model_catalog,
)

if TYPE_CHECKING:
    from ..config import SatelliteConfig

logger = logging.getLogger("satellite")

# Bounded MCP warm-gate before the first turn: the vendored
# ``app_server_client.wait_for_mcp_startup`` (shared with the proxy) waits
# until every server the session configured has a terminal startup status —
# see its docstring. The caps bound a server that hangs past Codex's own
# startup_timeout_sec: 15 s on hosted models; 90 s on a local model, where a
# changed tool list means minutes of re-prefill (covers the remote MCP startup
# floor of 60 s; the proxy's start ack budget allows for it). The policy
# constants live here and in proxy/core/layers/codex/session.py — keep them in
# lock-step.
_WARM_POLL_S = 0.5
_WARM_CAP_S = 15.0
_WARM_CAP_LOCAL_MODEL_S = 90.0
_WARM_NO_STATUS_S = 5.0
# How long an interrupt that drew an error waits for the turn to end by
# itself (its ``turn/completed`` was already on the way) before the daemon
# is dropped.
_INTERRUPT_SETTLE_S = 2.0
# How long a Stop waits for the id of a turn that is inside its ``turn/start``.
_TURN_ID_WAIT_S = 2.0
# A daemon that exited inside a turn: how long its exit code is waited for,
# and the most of its last stderr line the turn's end carries.
_EXIT_CODE_WAIT_S = 2.0
_LAST_LINE_MAX = 300


def _format_time() -> str:
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M")


def session_text_mode(config: dict) -> bool:
    """The platform's flag: the session's text is its own; none: ``AGENTS.md``."""
    return bool(config.get("codex_session_text"))


def session_text_of(config: dict) -> str:
    """The session's text as the platform composed it (the notes included)."""
    return str(config.get("agents_md_content", config.get("system_prompt", "")) or "")


def _write_codex_hooks(codex_dir: Path, *, session_text_hook: bool = False, start_context: bool = False,
                       scripts_dir: Path | None = None) -> None:
    """Write hooks.json for the Codex hook system.

    MUST match the schema the proxy's ``core/layers/codex/config_dir.build_hooks`` emits
    (per the Codex docs): an OBJECT
    ``{"hooks": {"<Event>": [{"matcher": "", "hooks":
    [{"type": "command", "command": <cmd>, "timeout": <s>}]}]}}``. The OLD shape
    here was a LIST ``[{event, matcher, commands}]`` — Codex's strict parser
    rejected it (``failed to parse hooks config … trailing characters``) and the
    permission FLOOR silently failed to load. Who runs it: the interactive
    Codex TUI (``--dangerously-bypass-hook-trust``) and, from 0.5.118, the
    ``-p`` app-server for UNATTENDED sessions (``codex_hooks_floor`` in the
    start payload → thread-level ``bypass_hook_trust``; see
    ``CodexSession._thread_overrides``); from 0.5.143 attended dashboard
    chats too (``codex_attended_floor``), whose gate judges every call (the
    JSON-RPC approval bridge takes what crosses the sandbox). Uses ``codex_hook_command()`` — on Windows a
    quote-free .cmd wrapper, because Codex's cmd.exe /C hook runner cannot
    re-parse the quoted two-token form (exit-1 on every call otherwise).
    ``codex_dir`` is the folder the engine reads (a session's home when it
    has one); the scripts stay in ``scripts_dir``, the store."""
    scripts = scripts_dir or codex_dir

    def _hook(script: str, timeout: int) -> dict:
        return {
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": codex_hook_command(scripts / script),
                "timeout": timeout,
            }],
        }
    # The same four events as the proxy's core/layers/codex/config_dir.build_hooks (satellite
    # 0.5.121; proxy docs/architecture/HOOKS.md): SubagentStop feeds the
    # proxy's SubagentRegistry over the tunnel, Stop carries the turn-end
    # verdict that may hold the turn open (the legacy {"decision":"block"}
    # shape, the one Codex accepts on Stop). Both stay quiet where the
    # scripts self-gate (the interactive TUI's OTO_INTERACTIVE).
    hooks = {
        "hooks": {
            "PreToolUse": [_hook("permission_gate.py", config.HOOK_WAIT_S)],
            "PostToolUse": [_hook("tool_result_forwarder.py", 10)],
            "SubagentStop": [_hook("subagent_tracker.py", 10)],
            "Stop": [_hook("stop_tracker.py", config.HOOK_WAIT_S)],
        },
    }
    # The proxy's config_dir.session_start_hook: the thread's text and a terminal's start data, neither trimmed.
    starts = [{**_hook(s, t)["hooks"][0], "additionalContextLimit": 0} for s, t, on in (
        ("session_text.py", 10, session_text_hook), ("start_context.py", 20, start_context)) if on]
    if starts:
        hooks["hooks"]["SessionStart"] = [{"matcher": "", "hooks": starts}]
    write_if_changed(codex_dir / "hooks.json", (json.dumps(hooks, indent=2) + "\n").encode("utf-8"))


def rewrite_codex_hooks(session) -> None:
    """A live session's hooks again, as its start wrote them, after a push
    of the store's file (``SessionManager._reapply_hooks_after_push``): in
    the folder the engine reads (its home when it has one), naming the
    store's scripts."""
    cfg = getattr(session, "config", {})
    codex_dir = session._codex_dir
    _write_codex_hooks(getattr(session, "_engine_dir", None) or codex_dir, session_text_hook=session_text_mode(cfg),
                       start_context=bool(cfg.get("start_context_parts")), scripts_dir=codex_dir)


def _inject_display_env_toml(toml_content: str) -> str:
    """Append the satellite's own ``DISPLAY`` (and ``XAUTHORITY`` when set) to
    every stdio MCP ``env = { … }`` block that doesn't already declare one.

    Codex spawns MCP children from the config.toml env tables ONLY — the
    daemon's environment is not propagated — so on Linux a GUI-needing MCP
    (browser-control, camoufox, computer-control) loses the satellite's
    DISPLAY and a HEADED browser can never launch: "-32000 browser
    unavailable" from every codex session while claude sessions (whose CLI
    children inherit the full env) work fine. No-op on Windows/macOS and on
    hosts with no DISPLAY. The writer puts each inline table on one line
    and a value may carry braces (``OTO_TOOL_ARG_PATHS`` is a JSON list of
    objects), so the table's end is the line's last brace, never the
    first."""
    import re as _re
    display = os.environ.get("DISPLAY", "")
    if not display or not toml_content:
        return toml_content
    extra = {"DISPLAY": display}
    xauth = os.environ.get("XAUTHORITY", "")
    if xauth:
        extra["XAUTHORITY"] = xauth

    def _append(m):
        inner = m.group(2)
        if '"DISPLAY"' in inner:
            return m.group(0)
        parts = ", ".join(
            f'"{k}" = "{v}"' for k, v in extra.items()
        )
        sep = "" if inner.strip() == "" else ", "
        return f"{m.group(1)}{inner.rstrip()}{sep}{parts} }}{m.group(3)}"

    return _re.sub(
        r"^(\s*env\s*=\s*\{)(.*)\}([ \t]*)$", _append, toml_content, flags=_re.MULTILINE,
    )


# The bridges' hold while the platform cannot be reached (the permission
# gate's twin, ``proxy/hooks/permission_gate.py``, held equal by the
# vocabulary gate's twins): retried with this backoff for up to two minutes,
# or as long as the link's 502 or 503 names when that is longer
# (``_hold_named``, at most ``_HOLD_MAX_S``), then denied; a refused token
# gets a short ladder.
_HOOK_HOLD_S = 120.0
_HOOK_BACKOFF = (2.0, 4.0, 8.0, 15.0)
_HOOK_AUTH_LADDER = (2.0, 4.0, 8.0)
_HOLD_MAX_S = 3600.0
# A wait on a person streams a keepalive every 30 s.
_HOOK_SOCK_READ_S = 150


def _hold_named(body):
    """The seconds a tunnel answer asks the call to be held for, 0 for none."""
    value = body.get("hold_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 0.0
    return min(float(value), _HOLD_MAX_S)


def _hold_seconds(text: str) -> float:
    """``hold_s`` of the local link's 502 or 503 body (the platform said for
    how long it stops), 0 when the body names none."""
    try:
        body = json.loads(text)
    except ValueError:
        return 0.0
    return _hold_named(body) if isinstance(body, dict) else 0.0


def _error_word(text: str) -> str:
    """The ``error`` word of a synthetic tunnel answer, "" for any other body."""
    try:
        return str(json.loads(text or "{}").get("error") or "")
    except (ValueError, AttributeError):
        return ""


def _will_retry(params) -> bool:
    """Whether Codex retries the error an ``error`` notification reports: the
    flag sits at the params' top level (app-server 0.156.1), older builds
    nested it in ``error``. A retried error does not end the turn."""
    if not isinstance(params, dict):
        return False
    if "willRetry" in params:
        return bool(params.get("willRetry"))
    return bool((params.get("error") or {}).get("willRetry"))


# How the Responses API refuses a reasoning summary (an organization that is
# not verified, on an API key): a 400 naming the parameter. Twin of the
# proxy's ``core/layers/codex/helpers.summary_refused``.
_SUMMARY_REFUSAL = "verified to generate reasoning summaries"
_SUMMARY_PARAM = "reasoning.summary"


def _summary_refused(params) -> bool:
    """Whether an ``error`` notification or a failed ``turn/completed``
    reports the API's refusal of the reasoning summary."""
    if not isinstance(params, dict):
        return False
    err = params.get("error")
    if not isinstance(err, dict):
        turn = params.get("turn")
        err = turn.get("error") if isinstance(turn, dict) else None
    if not isinstance(err, dict):
        return False
    text = f"{err.get('message') or ''} {err.get('additionalDetails') or ''}".lower()
    if _SUMMARY_REFUSAL in text:
        return True
    return _SUMMARY_PARAM in text and ("400" in text or err.get("codexErrorInfo") == "badRequest")


def _write_codex_hook_scripts(codex_dir: Path, scripts: dict[str, str]) -> None:
    """Write hook scripts sent by the proxy into the .codex/ dir (an
    unchanged one left alone, a changed one replaced whole:
    ``config.write_if_changed``)."""
    if not scripts:
        return
    for filename, content in scripts.items():
        if not content:
            continue
        write_if_changed(codex_dir / filename, content.encode("utf-8"), 0o755)


class CodexSession:
    """Persistent ``codex app-server`` session (direct, no bwrap)."""

    execution_path = "codex-cli"

    def __init__(
        self,
        session_id: str,
        agent_dir: Path,
        config: dict,
        sat_config: "SatelliteConfig",
    ):
        self.session_id = session_id
        self.agent_dir = agent_dir
        self.agent_slug = agent_dir.name
        self.config = config
        self.sat_config = sat_config
        self.thread_id: str | None = config.get("thread_id")
        # The model and effort the thread runs (thread/start or thread/resume
        # answer): the collaboration mode needs a model (the proxy's twin).
        self._thread_model = ""
        self._thread_effort = ""
        # Codex refused the thread the platform asked to resume and a new one
        # started: the session runs without that thread's memory (reported in
        # the start's ack, and on the revive's _resume_handle as resumed False).
        self.resume_refused = False
        self.pid: int | None = None
        self._file_snapshot: dict[str, str] = {}
        self._cwd: Path | None = None
        self._codex_dir: Path | None = None
        # The folder the engine reads: the session's home when the start asks
        # for one (``session_homes``), else the store. A pushed sign-in for
        # it reaches the start config a revive writes (``credentials_update``).
        self._engine_dir: Path | None = None
        self._username: str = ""
        self._client: AppServerClient | None = None
        self._current_turn_id: str | None = None
        # A ``turn/start`` is on its way and the turn has no id yet, and the
        # event its answer sets (``_start_turn``).
        self._turn_starting = False
        self._turn_start_answered = asyncio.Event()
        # Serialize turns per session. The satellite dispatches every WS command
        # as an independent task (ws_client create_task) and the proxy drops its
        # session lock the instant a turn is aborted (its producer is cancelled),
        # so a fast abort→resend could otherwise start a SECOND run_turn while
        # the aborted one is still unwinding — two overlapping turns share
        # _current_turn_id + the single _main_turn_done event and corrupt each
        # other's completion. abort() deliberately does NOT take this lock.
        self._turn_lock = asyncio.Lock()
        self._closed = False
        # Proxy loopback-tunnel coords for the native-approval bridge —
        # captured from the translated env in start().
        self._proxy_url: str = ""
        self._proxy_api_key: str = ""
        # itemId → [paths] from fileChange item/started (lean approval recovery).
        self._item_paths: dict[str, list[str]] = {}
        # --- Persistent event forwarder (dumb pipe) ---
        # A single task is the SOLE consumer of the daemon's notif_queue. It
        # forwards EVERY notification to the proxy verbatim as a session_event —
        # including a BACKGROUND sub-agent's events AFTER the main turn ends — so
        # the proxy (which holds the translator) demuxes main vs sub threads and
        # supervises bg sub-agents centrally. run_turn just starts the turn and
        # waits for the MAIN thread's completion. No thread tracking or event
        # parsing happens here (the satellite stays dumb).
        self._forwarder_task: asyncio.Task | None = None
        self._forward_event = None             # async callable: event_dict -> awaitable
        self._main_turn_done: asyncio.Event = asyncio.Event()
        self._stop_requested = False
        # Why the turn's wait ended when the daemon did not end it itself
        # (``take_turn_end``): the session manager puts it on the turn's end.
        self._turn_end: dict = {}
        # A drop in progress (``_drop_daemon``): a second Stop drops nothing.
        self._dropping = False
        # The reasoning summary: whether the turn running asked for it, whether
        # the API refused it (no later turn asks), and whether the forwarder is
        # dropping a refused turn the turn method then runs again without it.
        self._summary_this_turn = False
        self._summary_refused = False
        self._summary_retry = False
        # Side-taps on the forwarder stream (compact() watches for its
        # completion signals here without stealing the sole-consumer role).
        self._sniffers: list[asyncio.Queue] = []
        # The [mcp_servers.*] this session's config.toml declares (the warm
        # gate waits for each) and whether the model is a local endpoint (the
        # longer warm cap). Set by start() from the written config.
        self._mcp_server_names: list[str] = []
        self._local_model: bool = runs_on_local_model(config)
        # A compute session (``sandbox_host``): the platform's files in the
        # shared folder and its sandbox, nothing written or translated here.
        self._compute = sandbox_host.is_compute(config)
        self._own_copy = False
        # The proxy decides which sessions run permission_gate.py as the
        # app-server's PreToolUse hook (UNATTENDED client types: task / phone /
        # meeting / trigger / internal — helpers.codex_hooks_floor; satellite
        # >= 0.5.118). Under `approvalPolicy: never` the approval bridge never
        # fires, so without the floor such a session is gated by the prompt
        # rules only. Three pieces, each per session: `[features] hooks = true`
        # in config.toml, `bypass_hook_trust` on the thread (Codex runs a
        # user-layer hook only when trusted — the operative switch on a shared
        # CODEX_HOME), and the deny-only / no-forward hook env below.
        self._hooks_floor: bool = bool(config.get("codex_hooks_floor"))
        # An ATTENDED chat (0.5.143, ``codex_attended_floor``) runs the same
        # three pieces: its gate judges every call (the card in Ask, the floor
        # and Auto's allow set in Auto) and the bridge takes what crosses the
        # sandbox. Read from the payload, never from this satellite's own
        # version.
        self._attended_floor: bool = bool(config.get("codex_attended_floor"))
        # The session's own text and thread map (``session_text``); empty: AGENTS.md.
        self._developer_instructions = ""
        self._session_config: dict = {}

    @property
    def _hooks_on(self) -> bool:
        return self._hooks_floor or self._attended_floor

    def _collaboration_mode(self, sandbox_mode: str) -> dict:
        """turn/start's experimental top-level ``collaborationMode`` (0.5.166):
        the session's model and effort (the shipped ones, else the thread's)
        in ``settings``, which takes precedence over the turn's own (a
        ``collaborationMode/list`` preset would bring its own effort); a null
        ``developer_instructions`` is the mode's built-in text. Empty when no
        model is known (``settings.model`` is required)."""
        model = self.config.get("model", "") or self._thread_model
        if not model:
            return {}
        return {
            "mode": "plan" if self._plan_mode(sandbox_mode) else "default",
            "settings": {"model": model,
                         "reasoning_effort": (self.config.get("effort", "")
                                              or self._thread_effort or None),
                         "developer_instructions": None},
        }

    def _note_thread_settings(self, res) -> None:
        """The model and effort a thread/start or thread/resume answer names."""
        res = res if isinstance(res, dict) else {}
        self._thread_model = str(res.get("model") or "")
        self._thread_effort = str(res.get("reasoningEffort") or "")

    def _plan_mode(self, sandbox_mode: str) -> bool:
        """The plan collaboration mode follows the platform's ``permission_mode``
        (0.5.123): plan iff plan. A proxy that ships no mode (older) keeps the
        former rule, read-only ⟺ plan."""
        mode = self.config.get("permission_mode")
        if mode:
            return mode == "plan"
        return sandbox_mode == "read-only"

    def _platform_mode(self) -> str:
        """The shipped platform mode, a word before 1.9 read as its current
        one (``default`` → Ask, ``acceptEdits`` → Auto)."""
        mode = self.config.get("permission_mode") or ""
        return {"default": "ask", "acceptEdits": "autoReview"}.get(mode, mode)

    def _approval_policy(self) -> str:
        """``codex_approvals.approval_policy`` from the shipped platform mode
        (on-request with Codex's own reviewer in Auto, the sandbox's policy
        otherwise; a proxy that ships no mode leaves the sandbox to decide)."""
        return approval_policy(self._platform_mode(),
                               self.config.get("sandbox_mode", "workspace-write"))

    def _reviewer(self) -> str:
        return approvals_reviewer(self._platform_mode())

    async def _start_compute(self) -> None:
        """A session on a compute machine: the platform wrote the home's and
        the session's own files into the shared folder and planned the spawn;
        the daemon runs in the sandbox the plan describes, the thread in its
        folder, the approval and question bridges on this machine's link."""
        await self._stop_forwarder()
        self._own_copy = await sandbox_host.ready_own_copy(self.config, self.sat_config,
                                                           self.session_id, self.agent_dir)
        if self._own_copy:
            self._file_snapshot = await asyncio.to_thread(file_sync.snapshot_agent_dir, self.agent_dir)
        # The session's own file the platform wrote (its text and thread map),
        # in the session's home when it has one.
        self._codex_dir = self.agent_dir / self.config["codex_dir_relative"]
        self._engine_dir = session_homes.home_path(self._codex_dir, self.session_id,
                                                   self.config.get("session_home"))
        if session_text_mode(self.config):
            session_text.remove_shared_text(self._codex_dir)
            own = session_text.read_session(self._engine_dir, self.session_id)
            if own is None:
                logger.error("Codex compute session %s: its own file is missing", self.session_id[:8])
                raise RuntimeError("the session's own file is missing")
            self._developer_instructions, self._session_config = own["developer_instructions"], own["config"]
        self._mcp_server_names = [str(n) for n in (self.config.get("mcp_server_names") or [])]
        env = sandbox_host.spawn_env(self.config)
        self._proxy_url = f"http://127.0.0.1:{int(self.config.get('tunnel_port') or 0)}"
        self._proxy_api_key = env.get("PROXY_API_KEY", "")
        # The session's SSH agent socket before the render: the description
        # names it as a bind source. The plan's environment carries the
        # sandbox's own SSH_AUTH_SOCK and OTO_SSH_KEY_DIR, so they win here.
        from . import credential_exit, ssh_agent
        agent_env = await asyncio.to_thread(ssh_agent.start, self.session_id, self.config)
        await asyncio.to_thread(credential_exit.start, self.session_id, self.config)
        prefix, codex_bin = await sandbox_host.sandbox_prefix(self.config, self.sat_config, "codex")
        ssh_agent.merge_env(env, agent_env, self.config)
        await self._connect_with_retry(env, prefix=prefix, codex_bin=codex_bin)
        await self._open_thread()
        await self._warm_mcps()
        self._forwarder_task = asyncio.create_task(self._run_forwarder())
        self.pid = self._client.proc.pid if self._client.proc else 0
        logger.info("Codex compute session ready: session=%s, thread=%s, pid=%s",
                    self.session_id, self.thread_id, self.pid)

    async def _open_thread(self) -> None:
        """Resume the thread the platform named, else start one; a refused
        resume starts fresh and is reported in the ack."""
        overrides = self._thread_overrides()
        if self.thread_id:
            # ``excludeTurns``: the history stays in the rollout, unread here
            # (Codex deprecates hydrating it into the answer).
            resume = {"threadId": self.thread_id, "excludeTurns": True}
            if "model" in overrides and self._local_model:
                # Codex 0.168 and later resumes a thread on the provider its
                # rollout saved, whatever config.toml names (0.166 refused the
                # change): a chat moved from a hosted model to a local one
                # would run on the old provider. Naming the local provider
                # makes every Codex refuse that change, and the refusal starts
                # fresh below. Only beside a model: with none named, Codex
                # reads the provider as a model override and stops restoring
                # the thread's saved model and effort. A hosted session names
                # none: Codex refuses a local thread there as not configured,
                # and a computer whose own Codex config sets a provider keeps
                # resuming the threads born on it.
                resume["modelProvider"] = LOCAL_PROVIDER
            try:
                res = await self._client.request("thread/resume", {**resume, **overrides})
                self._note_thread_settings(res)
            except AppServerError as e:
                logger.warning("Codex resume failed (%s); starting new thread", e)
                self.thread_id = None
                self.resume_refused = True
        if not self.thread_id:
            res = await self._client.request("thread/start", overrides)
            self.thread_id = (res.get("thread") or {}).get("id") or ""
            self._note_thread_settings(res)

    async def start(self) -> None:
        """Write config files, spawn the app-server daemon, open/resume thread."""
        if self._compute:
            await self._start_compute()
            return
        # Cancel a stale forwarder from a prior (dead) client before re-warming.
        await self._stop_forwarder()
        # Honor an absolute work_cwd the same soft way cli_session does — a
        # bad path falls back to the in-tree default rather than failing the
        # spawn. Codex resume is thread-id keyed (rollouts under CODEX_HOME),
        # so cwd changes are safe here.
        from ..terminal.pty_session_base import resolve_work_cwd
        try:
            self._cwd = resolve_work_cwd(
                self.config.get("work_cwd") or "",
                self.agent_dir,
                self.config["cwd_relative"],
            )
        except ValueError:
            logger.warning(
                "invalid work_cwd for codex session %s; using the agent dir",
                self.session_id,
            )
            self._cwd = self.agent_dir / self.config["cwd_relative"]
        self._codex_dir = self.agent_dir / self.config["codex_dir_relative"]
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        self._cwd.mkdir(parents=True, exist_ok=True)
        # The session's home when the start asks for one: its settings, hooks,
        # model catalog, text and sign-in there; the scripts, the rollouts
        # and the databases the store's.
        self._engine_dir = session_homes.open_home(self._codex_dir, self.session_id,
                                                   self.config.get("session_home"))
        in_home = self._engine_dir != self._codex_dir
        if in_home:
            drop_store_leftovers(self.agent_dir, self.config["codex_dir_relative"])

        # --- write the .codex/ config tree (unchanged from the exec model) ---
        _write_codex_hook_scripts(self._codex_dir, self.config.get("hook_scripts", {}))
        write_command_wrappers(self._codex_dir, self.config.get("command_wrappers", {}))
        own_text = session_text_mode(self.config)
        if own_text:  # the thread's developerInstructions, no shared file
            self._developer_instructions = session_text_of(self.config)
            session_text.remove_shared_text(self._codex_dir)
            session_text.write_session_files(
                self._engine_dir, self.session_id, text=self._developer_instructions, config={},
                writer=write_if_changed)
        else:
            (self._codex_dir / "AGENTS.md").write_text(session_text_of(self.config))
        toml_content = self.config.get("mcp_config_toml", "")
        if toml_content:
            otodock_fwd = str(otodock_dir()).replace("\\", "/")
            home_fwd = str(Path.home()).replace("\\", "/")
            toml_content = toml_content.replace("~/.oto-dock", otodock_fwd)
            toml_content = toml_content.replace("~/", home_fwd + "/")
            # Codex doesn't propagate the daemon env to MCP children, so the
            # config.toml env tables are the only copy — translate their
            # sandbox-virtual paths to satellite-absolute (else an MCP that opens
            # one at startup, e.g. google-workspace's credentials dir, dies with
            # "connection closed: initialize response"). Mirrors `translate_env`.
            toml_content = path_translator.translate_codex_mcp_env_paths(
                toml_content,
                agent_dir=self.agent_dir,
                username=layout.user_of(
                    self.config.get("cwd_relative", ""),
                ),
                session_id=self.session_id,
            )
            toml_content = _inject_display_env_toml(toml_content)
            from .mcp_interceptor import wrap_interceptor_in_mcp_config_toml
            toml_content = wrap_interceptor_in_mcp_config_toml(toml_content)
        # ALWAYS write config.toml: CODEX_HOME persists across sessions of the
        # same (agent, user) and file sync never resets it, so a hosted session
        # after a local-endpoint one must not inherit the provider block (nor
        # stale MCP sections). Root keys first (the headless header's
        # project_doc_max_bytes, then the provider's model_provider /
        # model_catalog_json), then the tables: [tools] (the plan tool stays
        # on), [memories] off, the ONE [features] table (lean start, the hook
        # floor when the proxy asked for it, and whatever [features] keys the
        # proxy prepended to its MCP TOML — lifted out AFTER the MCP transforms
        # above, so their input is unchanged), the MCP sections, and the
        # provider table last (none of the MCP transforms sees it).
        local_provider = self.config.get("local_model_provider")
        root_line, provider_table = local_provider_toml(local_provider, self._engine_dir)
        features, mcp_sections = features_table(self._hooks_on, toml_content.strip())
        parts = [p for p in (
            CODEX_HEADLESS_ROOT_KEYS, root_line, CODEX_TOOLS_TABLE, CODEX_MEMORIES_TABLE,
            features, mcp_sections, provider_table,
        ) if p]
        config_text = "\n\n".join(parts) + "\n"
        self._mcp_server_names = mcp_server_names_from_toml(config_text)
        config_path = self._engine_dir / "config.toml"
        if config_text:
            _validate_config_toml(config_text, config_path)
        write_or_drop_model_catalog(self._engine_dir, local_provider)
        config_path.write_text(config_text)
        chmod_private(config_path)
        write_or_drop_auth_json(self._engine_dir, self.config.get("auth_json"))
        _write_codex_hooks(self._engine_dir, session_text_hook=own_text,
                           start_context=bool(self.config.get("start_context_parts")),
                           scripts_dir=self._codex_dir)

        # Off-thread — a big agent tree's hash walk must not stall the event
        # loop at session start (parity with CLISession).
        self._file_snapshot = await asyncio.to_thread(
            file_sync.snapshot_agent_dir, self.agent_dir,
        )

        # --- spawn the persistent daemon ---
        self._username = layout.user_of(
            self.config.get("cwd_relative", ""),
        )
        # Curate the operator's ambient secrets BEFORE the platform config["env"]
        # overlay (which survives curation). Defense-in-depth — see env_hygiene.py.
        env = env_hygiene.curate_satellite_env(os.environ)
        env.update(self.config.get("env", {}))
        env["CODEX_HOME"] = str(self._engine_dir)
        if in_home:
            # The databases stay the store's, as the rollouts do (its link).
            env["CODEX_SQLITE_HOME"] = str(self._codex_dir)
        env["OTO_SESSION_ID"] = self.session_id
        # Interpreter for the Windows .cmd hook wrappers (codex_hook_command).
        env["OTO_HOOK_PY"] = sys.executable
        if self._hooks_on:
            # The hook processes inherit the daemon env. Codex's PreToolUse
            # hook rejects permissionDecision:"allow", so permission_gate.py
            # emits JSON only to DENY; the PostToolUse forwarder stays quiet
            # (the JSON-RPC stream already carries every tool result — a
            # forward would render each card twice). Assigned, not defaulted:
            # a stray operator variable must not win. Twin of the local
            # layer's _build_env; the interactive TUI path sets DENY_ONLY too.
            env["OTO_HOOK_DENY_ONLY"] = "1"
            env["OTO_HOOK_NO_FORWARD"] = "1"
        # The gate judges every call of every session (an attended chat's
        # too: the bridge takes only what crosses the sandbox).
        env.pop("OTO_HOOK_BRIDGED", None)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # The credential exit's door (the payload's ``exit`` block), and the
        # session's SSH agent socket and public keys (the proxy signs).
        from . import credential_exit, ssh_agent
        await asyncio.to_thread(credential_exit.start, self.session_id, self.config)
        ssh_agent.merge_env(
            env, await asyncio.to_thread(ssh_agent.start, self.session_id, self.config),
            self.config,
        )
        env = path_translator.translate_env(
            env, agent_dir=self.agent_dir, username=self._username,
            session_id=self.session_id,
            multi_value_envs=self.config.get("multi_value_envs") or {},
        )
        env = session_tools_env(env, self._codex_dir)
        # The native-approval bridge posts to the proxy's /v1/hooks/permission
        # over this loopback tunnel (translate_env rewrote PROXY_URL to it).
        self._proxy_url = env.get("PROXY_URL", "")
        self._proxy_api_key = env.get("PROXY_API_KEY", "")

        await self._connect_with_retry(env)
        await self._open_thread()
        await self._warm_mcps()
        # The forwarder now becomes the SOLE consumer of notif_queue (warm-up
        # drained the startup notifications itself, above).
        self._forwarder_task = asyncio.create_task(self._run_forwarder())
        self.pid = self._client.proc.pid if self._client.proc else 0
        logger.info("Codex app-server ready: session=%s, thread=%s, pid=%s", self.session_id, self.thread_id, self.pid)

    def set_event_forwarder(self, cb) -> None:
        """Set the async callback the forwarder uses to ship each notification to
        the proxy (session_manager sets it per turn with the current ws)."""
        self._forward_event = cb

    def request_stop_turn(self, drain_bg: bool = False) -> None:
        """Proxy asked to end the current turn early — release run_turn's wait.
        (The proxy normally ends a Codex turn on the main thread's turn/completed;
        this is the stop_turn safety path. ``drain_bg`` is a CLI-session concept —
        accepted for signature parity with CLISession, ignored here.)"""
        self._stop_requested = True
        self._main_turn_done.set()

    async def steer(self, text: str) -> bool:
        """Inject user input into the RUNNING turn via ``turn/steer`` — twin of
        the proxy's ``CodexAppServerSession.steer`` (see its docstring for the
        exactly-once contract). Returns True ONLY when the daemon accepted the
        steer; the proxy's caller queues the message otherwise, so a soft
        failure here must never claim acceptance."""
        if (self._closed or self._client is None or not self._client.is_alive
                or not self._current_turn_id):
            return False
        # Chat uploads arrive as sandbox-virtual paths — translate like run_turn
        # (a compute session runs in the sandbox, where they are right as sent).
        if not self._compute:
            text = path_translator.translate_paths_in_text(
                text, agent_dir=self.agent_dir, username=self._username,
            )
        try:
            await self._client.request("turn/steer", {
                "threadId": self.thread_id,
                "expectedTurnId": self._current_turn_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            }, timeout=10.0)
        except AppServerError as e:
            logger.info("Codex [%s] steer rejected: %s", self.session_id[:8], e)
            return False
        logger.info("Codex [%s] steered turn %s", self.session_id[:8], self._current_turn_id)
        return True

    async def compact(self) -> dict | None:
        """Manual thread compaction between turns — twin of the proxy's
        ``CodexAppServerSession.compact``, adapted to the satellite's dumb-pipe
        architecture: the persistent forwarder stays the sole notif consumer
        and this method watches a side-tap (``_sniffers``) for the compaction's
        completion signals. Returns ``{"post_tokens": int|None}`` or None."""
        if self._closed or self._client is None or not self._client.is_alive:
            return None
        if self._current_turn_id or self._turn_lock.locked():
            return None
        sniffer: asyncio.Queue = asyncio.Queue()
        self._sniffers.append(sniffer)
        try:
            await self._client.request("thread/compact/start", {
                "threadId": self.thread_id,
            }, timeout=30.0)
            post_tokens: int | None = None
            compacted = False
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 120.0
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if compacted:
                        logger.info(
                            "Codex [%s] compacted thread "
                            "(post_tokens=%s)", self.session_id[:8], post_tokens
                        )
                        return {"post_tokens": post_tokens}
                    logger.warning(
                        "Codex [%s] compaction timed out", self.session_id[:8]
                    )
                    return None
                try:
                    method, params = await asyncio.wait_for(
                        sniffer.get(), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    continue     # loop re-checks the deadline / grace window
                params = params if isinstance(params, dict) else {}
                if method == "__daemon_exit__":
                    return None
                if method == "thread/tokenUsage/updated":
                    usage = (params.get("tokenUsage") or {})
                    last = usage.get("last") or {}
                    post_tokens = int(last.get("inputTokens", 0) or 0) or post_tokens
                    continue
                # Canonical v2 completion — the contextCompaction ITEM (the
                # app-server swallows the deprecated thread/compacted for v2
                # clients); short grace window for the recomputed size after.
                if ((method == "item/completed"
                     and (params.get("item") or {}).get("type")
                     == "contextCompaction")
                        or method == "thread/compacted"):
                    compacted = True
                    deadline = min(deadline, loop.time() + 5.0)
                    continue
                if method == "turn/completed":
                    if compacted:
                        logger.info(
                            "Codex [%s] compacted thread "
                            "(post_tokens=%s)", self.session_id[:8], post_tokens
                        )
                        return {"post_tokens": post_tokens}
                    logger.warning(
                        "Codex [%s] compaction turn ended "
                        "without a contextCompaction item", self.session_id[:8]
                    )
                    return None
                if method == "error" and not _will_retry(params):
                    logger.warning(
                        "Codex [%s] compaction error: "
                        "%s",
                        self.session_id[:8], (params.get('error') or {}).get('message')
                    )
                    return None
        except AppServerError as e:
            logger.warning("Codex [%s] compaction failed: %s", self.session_id[:8], e)
            return None
        finally:
            with contextlib.suppress(ValueError):
                self._sniffers.remove(sniffer)

    async def list_background_terminals(self) -> list | None:
        """Pull ``thread/backgroundTerminals/list`` rows for the proxy's
        bg-command drain (remote twin of the proxy session's list RPC — the
        proxy does all the reconciliation; we forward the rows verbatim).
        None on error: the proxy treats it as no-progress (fail-closed,
        never resolve-on-error)."""
        if self._closed or self._client is None or not self._client.is_alive:
            return None
        try:
            res = await self._client.request("thread/backgroundTerminals/list", {
                "threadId": self.thread_id,
            }, timeout=10.0)
        except AppServerError as e:
            logger.warning(
                "Codex [%s] backgroundTerminals list failed: %s",
                self.session_id[:8], e)
            return None
        data = res.get("data") if isinstance(res, dict) else None
        return data if isinstance(data, list) else []

    async def run_turn(
        self, prompt: str, *, inject_time: bool = False, forward=None,
    ) -> None:
        """Drive ONE turn, serialized per session so turns can never overlap.

        ``forward`` (the session manager's per-turn event shipper) becomes the
        persistent forwarder's callback: EVERY app-server notification —
        including a background sub-agent's after the main turn ends — rides
        it, so the proxy (which holds the translator) supervises bg work
        centrally while this side stays a dumb pipe. None keeps the current
        forwarder (tests drive turns without one).

        Why the lock matters: the satellite runs every WS command as its own
        task and the proxy releases its per-session lock the moment a turn is
        aborted (the producer is cancelled), so a fast **abort → new message**
        pair would otherwise start a second ``run_turn`` while the aborted one is
        still unwinding. Both would clear/overwrite the shared ``_main_turn_done``
        event and ``_current_turn_id``; the new turn's wait then gets released by
        the OLD turn's terminal and returns with **zero** streamed events — the
        "I hit Stop, sent again, the daemon ran it but the dashboard showed
        nothing" bug (fingerprinted by the daemon's ``interrupt failed: expected
        active turn X but found Y``). ``abort()`` deliberately does NOT acquire
        this lock — it interrupts the in-flight turn, which lets the holder
        unwind and release so the next turn proceeds cleanly."""
        if forward is not None:
            self.set_event_forwarder(forward)
        async with self._turn_lock:
            await self._run_turn_locked(prompt, inject_time=inject_time)

    async def _run_turn_locked(self, prompt: str, *, inject_time: bool = False) -> None:
        """Drive ONE turn: ``turn/start``, then wait for the MAIN thread's terminal.

        Does NOT consume notif_queue — the persistent forwarder (_run_forwarder)
        streams every notification to the proxy, including a background
        sub-agent's events AFTER this turn ends. This coroutine only starts the
        turn and waits for the main thread's completion, so the proxy is the sole
        place that demuxes main vs background threads (the satellite stays a dumb
        pipe). Replaces the old per-turn generator."""
        if self._closed or self._client is None:
            raise RuntimeError(f"Codex session {self.session_id} is closed")
        if not self._client.is_alive:
            # Daemon died between turns — re-warm + resume.
            logger.info("Codex daemon dead for %s; re-warming", self.session_id)
            self._client = None
            before = self.thread_id
            await self.start()
            if self.thread_id and self.thread_id != before and self._forward_event:
                # The resume was refused and a new thread started: the
                # platform routes this session's events by its thread, so
                # it hears of the new one before the turn's first event (the
                # marker it turns the start's codex_thread_id frame into).
                await self._forward_event({"type": "_resume_handle", "handle": self.thread_id,
                                           "resumed": False})

        if inject_time:
            prompt = f"[Current time: {_format_time()}]\n{prompt}"
        # Translate sandbox-virtual paths (chat photos/files) to satellite-absolute;
        # a compute session runs in the sandbox, where they are right as sent.
        if not self._compute:
            prompt = path_translator.translate_paths_in_text(
                prompt, agent_dir=self.agent_dir, username=self._username,
            )

        self._item_paths.clear()
        sandbox_mode = self.config.get("sandbox_mode", "workspace-write")
        turn_params: dict = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
            # Per-turn permission knobs — authoritative for the session's
            # current mode so a propagated set_permission_mode takes effect.
            "approvalPolicy": self._approval_policy(),
            "approvalsReviewer": self._reviewer(),
            "sandboxPolicy": build_sandbox_policy(sandbox_mode, str(self._cwd or "")),
        }
        # The collaboration mode, re-asserted per turn (mirror of the local
        # layer): plan iff the platform mode is `plan` (0.5.123: a check's
        # judge runs the read-only sandbox WITHOUT planning).
        collaboration_mode = self._collaboration_mode(sandbox_mode)
        if collaboration_mode:
            turn_params["collaborationMode"] = collaboration_mode
        if not self._summary_refused:
            # Reasoning summaries (twin of the proxy's helpers.REASONING_SUMMARY):
            # Codex's catalog defaults every hosted model to none.
            turn_params["summary"] = "auto"
        model = self.config.get("model", "")
        effort = self.config.get("effort", "")
        if model:
            turn_params["model"] = model
        if effort:
            turn_params["effort"] = effort

        self._main_turn_done.clear()
        self._stop_requested = False
        self._turn_end = {}
        self._summary_this_turn = "summary" in turn_params
        self._summary_retry = False
        await self._start_turn(turn_params)
        try:
            await self._main_turn_done.wait()
            if self._summary_retry and not self._stop_requested:
                # The forwarder dropped the refused turn: once more without
                # the summary (the thread keeps the refused turn's prompt).
                self._summary_retry = False
                self._summary_this_turn = False
                turn_params.pop("summary", None)
                self._main_turn_done.clear()
                await self._start_turn(turn_params)
                await self._main_turn_done.wait()
        finally:
            self._summary_retry = False
            self._current_turn_id = None

    async def _start_turn(self, turn_params: dict) -> None:
        """``turn/start``, and the turn's id once the daemon answers. Until
        then a Stop has no id to interrupt, so ``abort`` waits for it here.
        An earlier turn's id is not this one's: it goes first."""
        assert self._client is not None  # invariant: the turn method checked it
        self._current_turn_id = None
        self._turn_start_answered.clear()
        self._turn_starting = True
        try:
            res = await self._client.request("turn/start", turn_params)
            self._current_turn_id = (res.get("turn") or {}).get("id")
        finally:
            self._turn_starting = False
            self._turn_start_answered.set()

    async def _run_forwarder(self) -> None:
        """Persistent SOLE consumer of the daemon's notif_queue. Forwards every
        notification to the proxy verbatim as a session_event (the proxy's shared
        CodexEventTranslator + router do all parsing/demux) and signals the active
        run_turn when the MAIN thread's turn reaches its terminal. A background
        sub-agent's events keep flowing here AFTER the main turn ends, so the
        proxy can supervise it + nudge on completion. Runs for the daemon's
        lifetime; cancelled on close / re-warm (a fresh client gets a fresh one)."""
        client = self._client
        if client is None:
            return
        q = client.notif_queue
        while True:
            try:
                method, params = await q.get()
            except asyncio.CancelledError:
                return
            for sniffer in list(self._sniffers):
                # Non-blocking tap (unbounded queue) — a slow compact() waiter
                # must never stall the proxy-bound pipe.
                sniffer.put_nowait((method, params))
            if method == "__daemon_exit__":
                if self.turn_active:
                    # A turn waits for this daemon: its end says the engine
                    # exited (``take_turn_end``), and that is the one ending.
                    self._turn_end = await self._exit_of(client)
                else:
                    await self._forward({"method": "error", "params": {
                        "error": {"message": "Codex app-server exited unexpectedly"},
                    }})
                self._main_turn_done.set()
                return
            if method == "item/started":
                self._track_item_paths(params)
            ev_tid = params.get("threadId") if isinstance(params, dict) else None
            is_main = (not ev_tid) or (not self.thread_id) or (ev_tid == self.thread_id)
            if is_main and (self._summary_retry or self._refuses_summary(method, params)):
                # The refused turn never reaches the platform; its end lets
                # the turn method run it again without the summary.
                if method == "turn/completed":
                    self._main_turn_done.set()
                continue
            await self._forward({"method": method, "params": params})
            # End the active run_turn on the MAIN thread's terminal ONLY. A
            # spawned sub-agent runs on its OWN thread and emits its own
            # turn/completed MID-turn (foreground/waited) or AFTER the main turn
            # (background) — neither ends the main turn; we keep forwarding them
            # so the proxy can track + supervise the background ones.
            if is_main and method == "turn/completed":
                self._main_turn_done.set()
            elif is_main and method == "error" and not _will_retry(params):
                self._main_turn_done.set()

    def _refuses_summary(self, method: str, params) -> bool:
        """The turn running asked for the reasoning summary and the API
        refused it (a final ``error`` or a failed ``turn/completed``): the
        session stops asking, with one log line."""
        if not self._summary_this_turn or not _summary_refused(params):
            return False
        if method == "error":
            if _will_retry(params):
                return False
        elif method != "turn/completed":
            return False
        self._summary_this_turn = False
        self._summary_refused = True
        self._summary_retry = True
        logger.warning("Codex %s: the API refused the reasoning summary; the turn runs "
                       "again without it, and so do the next ones", self.session_id)
        return True

    async def _forward(self, event: dict) -> None:
        """Ship one notification to the proxy via the session_manager-set cb."""
        cb = self._forward_event
        if cb is None:
            return
        try:
            await cb(event)
        except Exception:
            logger.exception("Codex forward failed for %s", self.session_id)

    async def _stop_forwarder(self) -> None:
        """Cancel + await the persistent forwarder (idempotent)."""
        if self._forwarder_task is not None:
            self._forwarder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._forwarder_task
            self._forwarder_task = None

    async def abort(self) -> None:
        """turn/interrupt the in-flight turn; the daemon stays warm. A daemon
        that does not take the interrupt (no answer in time, or an error
        while the turn stays open) never sends the turn's end: the turn
        would keep the session's lock and every later message, a Stop
        included, would wait behind it. That daemon is dropped
        (``_drop_daemon``). A turn inside its ``turn/start`` has no id yet:
        the interrupt waits for it, at most ``_TURN_ID_WAIT_S``."""
        if self._client is None or not self._client.is_alive:
            return
        if self._turn_starting:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._turn_start_answered.wait(), _TURN_ID_WAIT_S)
        if not self._current_turn_id:
            return
        turn_id = self._current_turn_id
        try:
            await self._client.request("turn/interrupt", {
                "threadId": self.thread_id, "turnId": turn_id,
            }, timeout=10.0)
        except AppServerError as e:
            logger.warning("Codex interrupt failed for %s: %s", self.session_id, e)
            # An error for a turn that just completed: its end is already
            # on the way and ends the wait by itself.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._main_turn_done.wait(), _INTERRUPT_SETTLE_S)
            if self._current_turn_id == turn_id and not self._main_turn_done.is_set():
                await self._drop_daemon()
        logger.info("Codex session aborted: %s", self.session_id)

    async def _drop_daemon(self) -> None:
        """End the turn a daemon no longer answers for, and the daemon with
        it. The thread is kept: the next turn finds a dead client and
        re-warms with ``thread/resume`` (``_run_turn_locked``). The order is
        ``close()``'s but for the wait, which ends last, so that turn meets
        a closed client and never a dying one: the forwarder first (the
        daemon's exit is this session's own doing and no error for the
        platform), then the client and its children. Two Stops on one
        unanswered interrupt both reach here: the second drops nothing, or
        the client it meets, closed and still dying, would end the wait
        early and the first drop's end would end the next turn's."""
        if self._dropping:
            return
        self._dropping = True
        logger.warning("Codex %s: the daemon did not take the interrupt; dropping it, the thread is kept",
                       self.session_id)
        try:
            self._turn_end = {"reason": "interrupted"}
            await self._stop_forwarder()
            await self._close_client()
            self._main_turn_done.set()
        finally:
            self._dropping = False

    @staticmethod
    async def _exit_of(client) -> dict:
        """A daemon's exit as a turn's end says it: its exit code, waited
        for a moment when the process is not reaped yet, and the last line
        it wrote to stderr."""
        proc = client.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), _EXIT_CODE_WAIT_S)
        code = proc.returncode if proc is not None else None
        line = (getattr(client, "last_stderr_line", "") or "")[:_LAST_LINE_MAX]
        return {"reason": "exited", **({"exit_code": code} if isinstance(code, int) else {}),
                **({"last_line": line} if line else {})}

    def take_turn_end(self) -> dict:
        """What the turn that just ended adds to its end frame, once: the
        reason its wait ended when the daemon did not end it itself."""
        ended, self._turn_end = self._turn_end, {}
        return ended

    async def _close_client(self) -> None:
        """Close the daemon and reap the children it leaves."""
        if self._client is None:
            return
        # Snapshot the app-server's MCP child tree BEFORE shutting it down.
        # On Windows a clean daemon exit does NOT cascade to its MCP children
        # (no job/process group there); they then keep venv files locked →
        # WinError 5 on the next MCP-update swap. Reap any survivors after
        # close. POSIX is already covered by the daemon's process-group kill
        # inside AppServerClient._kill_proc.
        proc = self._client.proc
        children = (
            await asyncio.to_thread(snapshot_descendants, proc.pid)
            if proc is not None and proc.returncode is None
            else []
        )
        await self._client.close()
        await asyncio.to_thread(reap_descendants, children, 5.0)

    async def close(self) -> None:
        self._closed = True
        from . import credential_exit, session_files, ssh_agent
        try:
            await self._stop_forwarder()
            self._main_turn_done.set()  # release any run_turn still waiting
            await self._close_client()
            # The session's SSH agent socket and public keys die with the session.
            await asyncio.to_thread(ssh_agent.stop, self.session_id)
            credential_exit.stop(self.session_id)
        finally:
            # Whatever a step above raised: the home with its sign-in and the
            # session's files go here or nowhere while this satellite runs.
            if not self._compute:
                # Per-session secret files (SSH keys) and own Codex files die with it.
                session_files.wipe(self.session_id)
                session_text.forget(self.session_id)
                session_homes.close_home(self._codex_dir, self.session_id, self.config.get("session_home"))
            elif self._own_copy:
                await asyncio.to_thread(session_files.wipe_agent_files, self.session_id)
                session_homes.close_home(self.agent_dir / self.config.get("codex_dir_relative", ""),
                                         self.session_id, self.config.get("session_home"))
        logger.info("Codex session closed: %s", self.session_id)

    async def send_control_request(self, subtype: str, **kwargs) -> None:
        """Model/mode change → applied as a per-turn override on the next turn
        (the persistent daemon picks it up on the next turn/start — no respawn).
        """
        if subtype == "set_model" and "model" in kwargs:
            self.config["model"] = kwargs["model"]
        elif subtype == "set_permission_mode" and "sandbox_mode" in kwargs:
            self.config["sandbox_mode"] = kwargs["sandbox_mode"]
            # The platform mode (a 0.5.143 proxy): the approval policy and the
            # plan collaboration mode follow it on the next turn/start.
            if "permission_mode" in kwargs:
                self.config["permission_mode"] = kwargs["permission_mode"]

    def detect_file_changes(self) -> list[dict]:
        if self._compute and not self._own_copy:
            return []
        return file_sync.detect_changes(self.agent_dir, self._file_snapshot)

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._client is not None and self._client.is_alive

    # What the connect report reads (sessions/alive_report.py).
    @property
    def resume_handle(self) -> str:
        return self.thread_id or ""

    @property
    def turn_active(self) -> bool:
        return self._turn_lock.locked()

    @property
    def mcp_server_names(self) -> list[str]:
        return list(self._mcp_server_names)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _thread_overrides(self) -> dict:
        # Approval policy (_approval_policy): an attended chat asks through
        # the bridge in every mode that asks, an unattended session by the
        # sandbox (on-request unless danger-full-access), so a request fires
        # an approval we route — over the loopback tunnel →
        # /v1/hooks/permission — through the proxy's decide_tool_permission.
        # turn/start re-asserts these per turn with the structured
        # sandboxPolicy. (No bwrap on the satellite; Codex's own sandbox is
        # the FS boundary.) Under `never` nothing fires, so the PreToolUse
        # hook floor judges instead — the hook posts to the same endpoint
        # over the same tunnel.
        sandbox_mode = self.config.get("sandbox_mode", "workspace-write")
        overrides: dict = {
            "approvalPolicy": self._approval_policy(),
            "approvalsReviewer": self._reviewer(),
            "sandbox": sandbox_mode,
        }
        if self.config.get("model"):
            overrides["model"] = self.config["model"]
        if self.config.get("effort"):
            overrides["effort"] = self.config["effort"]
        if self._compute:
            # The sandbox's folder (the platform's ``thread_cwd``): the daemon
            # runs inside the platform's sandbox, where that is the cwd.
            overrides["cwd"] = str(self.config.get("cwd") or "/")
        elif self._cwd is not None:
            # A resumed thread otherwise runs in the cwd its rollout recorded:
            # a thread moved here from the platform recorded the sandbox's
            # /users/<name>, which does not exist on this machine, and Codex
            # then fails every stdio MCP at spawn and runs commands there.
            # The platform's twin sends the sandbox's folder (``thread_cwd``).
            overrides["cwd"] = str(self._cwd)
        thread_config = dict(self._session_config)
        if self._hooks_on:
            # Codex runs a user-layer hooks.json only when trusted; the
            # app-server has no CLI flag for it (the TUI's
            # --dangerously-bypass-hook-trust), the per-thread `config` map is
            # the switch (codex-rs app-server/src/config_manager.rs). Sent on
            # thread/start AND thread/resume (same dict). The platform wrote
            # the hook it is trusting. Twin of the local _thread_overrides.
            thread_config["bypass_hook_trust"] = True
        if thread_config:
            overrides["config"] = thread_config
        if self._developer_instructions:  # the platform's _thread_overrides twin
            overrides["developerInstructions"] = self._developer_instructions
        return overrides

    async def _decide_permission_remote(self, tool_name: str, tool_input: dict) -> dict:
        """The injected decision authority for the approval bridge (remote).

        The satellite is the JSON-RPC client but the decision authority lives on
        the proxy, so we POST the translated tool to ``/v1/hooks/permission`` over
        the loopback tunnel (the same channel the CLI hook uses) and block on the
        proxy's verdict. Held while the platform cannot be reached
        (``_post_to_platform``), then fail-closed (deny), so the daemon's escape
        is rejected cleanly.
        """
        if not self._proxy_url or not self._proxy_api_key:
            logger.warning("Codex approval: no proxy tunnel coords; denying")
            return {"decision": "deny"}
        data = await self._post_to_platform("/v1/hooks/permission", {
            "session_id": self.session_id, "tool_name": tool_name, "tool_input": tool_input,
        })
        return data if isinstance(data, dict) else {"decision": "deny"}

    async def _ask_question_remote(self, questions: list) -> dict:
        """Injected question authority for request_user_input (remote).

        The daemon holds the turn open on the question; the answering human is on
        the proxy dashboard, so we POST the questions to ``/v1/hooks/codex-question``
        over the loopback tunnel and block on the proxy surfacing the card + the
        human answer. Returns the answers MAP ``{<id>: {"answers": [...]}}``.
        Empty answers when the platform stays unreachable, so the held turn
        unwinds (the model continues rather than hanging), matching the local
        decline path.
        """
        if not self._proxy_url or not self._proxy_api_key:
            logger.warning("Codex question: no proxy tunnel coords; empty answer")
            return {}
        data = await self._post_to_platform("/v1/hooks/codex-question", {
            "session_id": self.session_id, "questions": questions,
        })
        return ((data if isinstance(data, dict) else {}).get("answers")) or {}

    async def _post_to_platform(self, path: str, body: dict):
        """POST a bridge's request to the platform over the loopback tunnel.

        The platform answers a wait on a person with whitespace keepalives and
        the JSON last. While it cannot be reached (a connection error, the
        tunnel's 502/503/504, a body that ends before its JSON: a proxy restart,
        a dropped socket) the call is held and retried for up to
        ``_HOOK_HOLD_S`` from the first failure, or for the ``hold_s`` the link
        names when that is longer (a prompt may wait days before its stream
        drops); a refused token gets a short ladder (the satellite
        reports its sessions a moment after it reconnects). The parsed JSON,
        or None when the platform stayed unreachable or refused."""
        import aiohttp
        loop = asyncio.get_running_loop()
        deadline = None
        hold = _HOOK_HOLD_S
        unreachable = auth = 0
        # The hold's last failure, named in its final warning: which link
        # failed (a refused loopback port, the tunnel's own 503) costs hours
        # to find otherwise.
        last = ""
        while True:
            status = 0
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.post(
                        f"{self._proxy_url}{path}", json=body,
                        headers={"Authorization": f"Bearer {self._proxy_api_key}"},
                        timeout=aiohttp.ClientTimeout(
                            total=None, sock_connect=10, sock_read=_HOOK_SOCK_READ_S),
                    ) as resp:
                        status = resp.status
                        if status == 200:
                            text = (await resp.text()).strip()
                            if text:
                                return json.loads(text)
                            status = 0  # the stream ended before its JSON
                            last = "the answer was cut off"
                        elif status in (502, 503, 504):
                            text = await resp.text()
                            last = f"HTTP {status} {_error_word(text)}".strip()
                            # The platform said how long it is away: held that long.
                            told = _hold_seconds(text)
                            if told:
                                hold = max(hold, told)
                                if deadline is not None:
                                    deadline = max(deadline, loop.time() + told)
            except Exception as e:  # noqa: BLE001  # fail closed: held, then refused
                logger.info("Codex bridge: %s unreachable (%s)", path, e)
                status = 0
                last = f"{type(e).__name__}: {e}"[:200]
            if status == 401 and auth < len(_HOOK_AUTH_LADDER):
                await asyncio.sleep(_HOOK_AUTH_LADDER[auth])
                auth += 1
                continue
            if status not in (0, 502, 503, 504):
                logger.warning("Codex bridge: %s refused (%s)", path, status)
                return None
            wait = _HOOK_BACKOFF[min(unreachable, len(_HOOK_BACKOFF) - 1)]
            if deadline is None:
                deadline = loop.time() + hold
            if loop.time() + wait > deadline:
                logger.warning("Codex bridge: %s unreachable for %.0fs (last: %s)",
                               path, hold, last or "no answer")
                return None
            await asyncio.sleep(wait)
            unreachable += 1

    def _track_item_paths(self, params: dict) -> None:
        """Record a fileChange item's target paths (from ``item/started``) so a
        following lean ``item/fileChange/requestApproval`` can name what it edits."""
        item = params.get("item") or {}
        if item.get("type") != "fileChange":
            return
        item_id = item.get("id")
        paths = [
            c.get("path") for c in (item.get("changes") or [])
            if isinstance(c, dict) and c.get("path")
        ]
        if item_id and paths:
            self._item_paths[item_id] = paths

    async def _connect_with_retry(self, env: dict, *, prefix: list[str] | None = None,
                                  codex_bin: str = "") -> None:
        """Spawn the daemon + initialize, resetting a stale state runtime once.
        ``prefix`` and ``codex_bin`` are a compute session's rendered sandbox
        and resolved binary (the daemon then runs with no cwd of this
        machine's: bubblewrap changes into the sandbox's).

        ``codex app-server`` keeps SQLite runtime DBs directly under
        CODEX_HOME. A copy written by a different codex version — or one that
        arrived from another machine — makes the daemon abort during init
        ("migration N was previously applied but has been modified"). On that
        failure we delete the runtime DBs (the thread rollouts in
        ``sessions/`` are untouched, so history survives) and retry once.
        """
        if not codex_bin:
            from ..host.cli_versions import resolve_spawn_bin_async
            codex_bin = await resolve_spawn_bin_async(
                "codex", self.sat_config.bin_hint("codex"),
            )
        last_err: Exception | None = None
        for attempt in (1, 2):
            self._client = AppServerClient(
                env=env, cwd=None if prefix else str(self._cwd),
                sandbox_cmd_prefix=prefix or sandbox_host.paired_command([]),
                codex_bin=codex_bin,
                label=f"codex[{self.session_id[:8]}]",
            )
            self._client.set_server_request_handler(make_server_request_handler(
                self._decide_permission_remote,
                ask_question=self._ask_question_remote,
                get_item_paths=lambda: self._item_paths,
                log=lambda m: logger.info("Codex [%s] %s", self.session_id[:8], m),
            ))
            try:
                async with start_slots.slot():
                    await self._client.start({
                        "clientInfo": {"name": "otodock-satellite", "title": "OtoDock", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    })
                return
            except AppServerError as e:
                last_err = e
                await self._client.close()
                if attempt == 1:
                    # Retried once as it is: the store's databases are every
                    # session's (CODEX_SQLITE_HOME), and Codex 0.169 rebuilds a
                    # damaged one itself.
                    logger.warning("Codex app-server init failed (%s); retrying", e)
        raise last_err if last_err else RuntimeError("codex app-server init failed")

    async def _warm_mcps(self) -> None:
        """Wait for the session's MCP servers (the ``[mcp_servers.*]`` this
        session wrote) to finish starting before the first turn — the shared
        ``wait_for_mcp_startup``, with the local-model cap when the session
        runs on a local endpoint. Twin of the proxy's
        ``CodexAppServerSession._warm_mcps``."""
        if self._client is None:
            return
        cap = _WARM_CAP_LOCAL_MODEL_S if self._local_model else _WARM_CAP_S
        res = await wait_for_mcp_startup(
            self._client, self._mcp_server_names, cap_s=cap,
            poll_s=_WARM_POLL_S, no_status_grace_s=_WARM_NO_STATUS_S,
        )
        if res.daemon_exited:
            return
        extra = f" unexpected={res.unexpected}" if res.unexpected else ""
        if res.pending:
            logger.warning(
                "Codex MCP warm-up (%s) hit the %.0fs cap after "
                "%.1fs: still starting=%s ready=%s "
                "failed=%s%s",
                self.session_id, cap, res.elapsed, res.pending, res.ready, res.failed, extra
            )
        else:
            logger.info(
                "Codex MCP warm-up (%s) done in %.1fs: "
                "ready=%s failed=%s%s",
                self.session_id, res.elapsed, res.ready, res.failed, extra
            )
