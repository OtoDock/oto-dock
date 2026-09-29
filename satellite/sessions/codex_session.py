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
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..host import env_hygiene, path_translator
from ..transport import file_sync
from .._vendored.app_server_client import (
    AppServerClient, AppServerError, mcp_server_names_from_toml, wait_for_mcp_startup,
)
from .._vendored.codex_approvals import (
    UNTRUSTED_POLICY, approval_for_sandbox, build_sandbox_policy,
    is_untrusted_policy_rejection, make_server_request_handler,
)
from ..config import codex_hook_command, otodock_dir, reap_descendants, snapshot_descendants
import contextlib
from .. import config
from .._vendored import layout

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


def _format_time() -> str:
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M")


def _write_codex_hooks(codex_dir: Path) -> None:
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
    ``CodexSession._thread_overrides``). Attended dashboard chats leave the
    file dormant (a user-layer hook runs only when trusted) and gate through
    the JSON-RPC approval bridge. Uses ``codex_hook_command()`` — on Windows a
    quote-free .cmd wrapper, because Codex's cmd.exe /C hook runner cannot
    re-parse the quoted two-token form (exit-1 on every call otherwise)."""
    def _hook(script: str, timeout: int) -> dict:
        return {
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": codex_hook_command(codex_dir / script),
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
            "PreToolUse": [_hook("permission_gate.py", 604800)],
            "PostToolUse": [_hook("tool_result_forwarder.py", 10)],
            "SubagentStop": [_hook("subagent_tracker.py", 10)],
            "Stop": [_hook("stop_tracker.py", 604800)],
        },
    }
    (codex_dir / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n")


def _inject_display_env_toml(toml_content: str) -> str:
    """Append the satellite's own ``DISPLAY`` (and ``XAUTHORITY`` when set) to
    every stdio MCP ``env = { … }`` block that doesn't already declare one.

    Codex spawns MCP children from the config.toml env tables ONLY — the
    daemon's environment is not propagated — so on Linux a GUI-needing MCP
    (browser-control, camoufox, computer-control) loses the satellite's
    DISPLAY and a HEADED browser can never launch: "-32000 browser
    unavailable" from every codex session while claude sessions (whose CLI
    children inherit the full env) work fine. No-op on Windows/macOS and on
    hosts with no DISPLAY. Mirrors the proxy layer's section-aware env
    append (same brace-blind regex — env values never contain '}' today)."""
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
        return f"{m.group(1)}{inner.rstrip()}{sep}{parts} }}"

    return _re.sub(r"(env\s*=\s*\{)([^}]*)\}", _append, toml_content)


def _validate_config_toml(text: str, path: Path) -> None:
    """Best-effort TOML validation before handing a config to codex: the strict
    TUI hard-exits (code 1, blank terminal) on invalid TOML and the app-server
    silently "uses defaults" (drops every MCP) — both are hard to diagnose from
    the outside, so make the corruption LOUD at the write site. Warn-only: the
    satellite's floor is py3.10 (no tomllib) and the proxy-side writer already
    fail-closes; here a visible ERROR beats killing the spawn inconsistently
    across hosts."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        logger.error("generated codex config.toml is INVALID (%s): %s", path, e)


# File name of the per-session model catalog inside CODEX_HOME (twin of the
# proxy's ``local_model_catalog.CATALOG_FILE_NAME``).
MODEL_CATALOG_FILE_NAME = "models.json"

# config.toml [tools] table every Codex session gets (twin of the proxy's
# ``layer._CODEX_TOOLS_TABLE``): Codex 0.152.0 made the update_plan tool
# opt-in (default off) — without it the daemon never emits turn/plan/updated
# and the dashboard's TODO checklist silently disappears. Valid on older
# Codex too (where on was the default).
CODEX_TOOLS_TABLE = "[tools]\nupdate_plan = { enabled = true }"

# Header every headless (app-server) remote Codex session gets — the twin of
# the local ``layer._write_config_toml`` header and of the interactive
# satellite writer (``codex_pty_session._build_codex_config_toml``). Before
# 0.5.118 the headless writer emitted none of it, so a paired machine ran
# Codex's defaults: the 32 KiB ``project_doc_max_bytes`` cap silently dropped
# the tail of a platform AGENTS.md (persona + memory + skills + knowledge),
# Codex's own memory subsystem ran next to otodock memory (the single source
# of memory truth; there is no ``.codex/memories/`` wipe on a satellite), and
# every daemon start cloned OpenAI's curated-plugins repo (``plugins`` below).
# Root keys must precede every ``[table]`` header.
CODEX_HEADLESS_ROOT_KEYS = "project_doc_max_bytes = 300000"
CODEX_MEMORIES_TABLE = "[memories]\nuse_memories = false\ngenerate_memories = false"


def features_table(hooks_floor: bool, mcp_toml: str) -> "tuple[str, str]":
    """The ONE ``[features]`` table of a headless config.toml, plus the MCP
    TOML with its own leading ``[features]`` block lifted out.

    Returns ``(features_block, mcp_toml_rest)``. The block always carries
    ``plugins = false`` (lean start, see ``CODEX_HEADLESS_ROOT_KEYS``), adds
    ``hooks = true`` when this session runs the PreToolUse permission floor
    (``codex_hooks_floor`` in the start payload — the key is default-on
    upstream, explicit on purpose), and keeps every key of a ``[features]``
    block the proxy prepends to the MCP TOML (``default_mode_request_user_input``
    for headless dashboard chats). Merging is what keeps the file valid: two
    ``[features]`` tables are a duplicate key Codex rejects, after which the
    app-server "uses defaults" and drops every MCP. Comments and blank lines
    of the lifted block are dropped; a key already present is not repeated.
    """
    lines = mcp_toml.split("\n") if mcp_toml else []
    keys: list[str] = ["plugins = false"]
    rest = mcp_toml
    if lines and lines[0].strip() == "[features]":
        end = len(lines)
        for i in range(1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("[") and stripped != "[features]":
                end = i
                break
        for raw in lines[1:end]:
            line = raw.strip()
            if line and not line.startswith("#"):
                keys.append(line)
        rest = "\n".join(lines[end:]).strip() if end < len(lines) else ""
    if hooks_floor:
        keys.append("hooks = true")
    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        name = key.split("=", 1)[0].strip()
        if name in seen:
            continue
        seen.add(name)
        unique.append(key)
    return "[features]\n" + "\n".join(unique), rest


def _esc_toml(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def local_provider_toml(local: "dict | None", codex_dir: "Path | None" = None) -> "tuple[str, str]":
    """Codex config.toml lines for a local OpenAI-compatible endpoint the proxy
    carried in the start payload (``local_model_provider``: ``base_url`` plus the
    optional ``env_key`` naming the child-env variable that holds its bearer,
    ``stream_idle_timeout_ms`` and the optional ``catalog_json``).

    Returns ``(root_lines, table)``: ``model_provider = "oto_local"`` (and, with
    a catalog, ``model_catalog_json = "<codex_dir>/models.json"`` — the file
    ``write_or_drop_model_catalog`` writes) are ROOT keys and must precede every
    ``[table]`` header; the ``[model_providers.oto_local]`` table may close the
    file. Twin of the proxy's ``_write_config_toml`` provider block —
    ``wire_api`` MUST be ``"responses"`` (codex-rs dropped the "chat" wire),
    ``env_key`` is emitted ONLY when the endpoint is keyed (codex-rs refuses a
    provider whose ``env_key`` names an unset variable) and the idle timeout
    only when the proxy sent one (a local model prefills at CPU speed; Codex's
    5-minute default killed a first turn before its first token). ``("", "")``
    without an endpoint, so a hosted session never inherits a provider.
    """
    base_url = str((local or {}).get("base_url") or "").strip()
    if not base_url:
        return "", ""

    root = ['model_provider = "oto_local"']
    if codex_dir is not None and str((local or {}).get("catalog_json") or "").strip():
        # Codex requires an ABSOLUTE path here (a relative agents_dir in
        # satellite.conf would otherwise reject the whole config.toml).
        catalog_path = os.path.abspath(str(Path(codex_dir) / MODEL_CATALOG_FILE_NAME))
        root.append(f'model_catalog_json = "{_esc_toml(catalog_path)}"')
    lines = [
        "[model_providers.oto_local]",
        'name = "Local"',
        f'base_url = "{_esc_toml(base_url)}"',
        'wire_api = "responses"',
    ]
    try:
        idle_ms = int((local or {}).get("stream_idle_timeout_ms") or 0)
    except (TypeError, ValueError):
        idle_ms = 0
    if idle_ms > 0:
        lines.append(f"stream_idle_timeout_ms = {idle_ms}")
    env_key = str((local or {}).get("env_key") or "").strip()
    if env_key:
        lines.append(f'env_key = "{_esc_toml(env_key)}"')
    return "\n".join(root), "\n".join(lines)


def write_or_drop_model_catalog(codex_dir: Path, local: "dict | None") -> None:
    """``models.json`` follows the start payload exactly: write the proxy-built
    per-session model catalog (``local_model_provider.catalog_json`` — the entry
    that makes Codex defer its MCP tools on an Ollama model) owner-only, or
    remove a previous session's file when this session carries none (CODEX_HOME
    persists per agent and user; config.toml no longer points at it, so the
    directory stays honest)."""
    path = codex_dir / MODEL_CATALOG_FILE_NAME
    text = str((local or {}).get("catalog_json") or "")
    if text.strip():
        path.write_text(text, encoding="utf-8")
        chmod_private(path)
        return
    with contextlib.suppress(OSError):
        path.unlink()


def write_or_drop_auth_json(codex_dir: Path, auth_json: "dict | None") -> None:
    """``auth.json`` follows the start payload exactly. With ``auth_json`` (a
    ChatGPT OAuth session) write it owner-only; WITHOUT it (an API key or a
    local endpoint) remove a previous session's file from this persistent
    CODEX_HOME — Codex would otherwise load the stale, refresh-neutralized
    token and loop on refreshing it every request ("Failed to refresh token:
    400 … refresh_token: empty string", verified 2026-09-07 on a local-endpoint
    session that followed a ChatGPT one)."""
    auth_path = codex_dir / "auth.json"
    if auth_json:
        auth_path.write_text(json.dumps(auth_json, indent=2))
        # Live OAuth tokens — keep them owner-only (the satellite host can be
        # multi-user; default umask would leave them world-readable).
        chmod_private(auth_path)
        return
    with contextlib.suppress(OSError):
        auth_path.unlink()


def chmod_private(path: Path) -> None:
    """Owner-only for a config file that carries per-session bearers (MCP
    cap-tokens, the session JWT, a local endpoint's URL) — the proxy locks its
    own config.toml 0600 the same way. No-op on Windows (no POSIX bits)."""
    if not config.HOST.posix:
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _write_codex_hook_scripts(codex_dir: Path, scripts: dict[str, str]) -> None:
    """Write hook scripts sent by the proxy into the .codex/ dir."""
    if not scripts:
        return
    for filename, content in scripts.items():
        if not content:
            continue
        path = codex_dir / filename
        path.write_text(content)
        if config.HOST.posix:
            path.chmod(0o755)


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
        self.pid: int | None = None
        self._file_snapshot: dict[str, str] = {}
        self._cwd: Path | None = None
        self._codex_dir: Path | None = None
        self._username: str = ""
        self._client: AppServerClient | None = None
        self._current_turn_id: str | None = None
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
        # Side-taps on the forwarder stream (compact() watches for its
        # completion signals here without stealing the sole-consumer role).
        self._sniffers: list[asyncio.Queue] = []
        # The [mcp_servers.*] this session's config.toml declares (the warm
        # gate waits for each) and whether the model is a local endpoint (the
        # longer warm cap). Set by start() from the written config.
        self._mcp_server_names: list[str] = []
        self._local_model: bool = bool(
            ((config.get("local_model_provider") or {}).get("base_url") or "").strip()
        )
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
        # An ATTENDED chat (no floor — a person answers the bridge) sends the
        # "untrusted" approval policy so every command and patch reaches the
        # proxy's decide_tool_permission, like Claude's hook (proxy HOOKS.md
        # "Attended Codex chats"); a daemon that rejects it drops the session
        # to on-request once, logged. Twin of the proxy session's
        # _note_untrusted_rejection.
        self._untrusted_rejected = False

    def _plan_mode(self, sandbox_mode: str) -> bool:
        """The plan collaboration mode follows the platform's ``permission_mode``
        (0.5.123): plan iff plan. A proxy that ships no mode (older) keeps the
        former rule, read-only ⟺ plan."""
        mode = self.config.get("permission_mode")
        if mode:
            return mode == "plan"
        return sandbox_mode == "read-only"

    def _approval_policy(self) -> str:
        sandbox_mode = self.config.get("sandbox_mode", "workspace-write")
        attended = not self._hooks_floor and not self._untrusted_rejected
        return approval_for_sandbox(sandbox_mode, attended=attended)

    def _note_untrusted_rejection(self, error: Exception) -> bool:
        if self._approval_policy() != UNTRUSTED_POLICY or not is_untrusted_policy_rejection(error):
            return False
        self._untrusted_rejected = True
        logger.warning(
            f"Codex [{self.session_id[:8]}] rejected approvalPolicy 'untrusted' ({error}); "
            f"this attended chat falls back to '{self._approval_policy()}'"
        )
        return True

    async def start(self) -> None:
        """Write config files, spawn the app-server daemon, open/resume thread."""
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

        # --- write the .codex/ config tree (unchanged from the exec model) ---
        _write_codex_hook_scripts(self._codex_dir, self.config.get("hook_scripts", {}))
        (self._codex_dir / "AGENTS.md").write_text(
            self.config.get("agents_md_content", self.config.get("system_prompt", ""))
        )
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
        root_line, provider_table = local_provider_toml(local_provider, self._codex_dir)
        features, mcp_sections = features_table(self._hooks_floor, toml_content.strip())
        parts = [p for p in (
            CODEX_HEADLESS_ROOT_KEYS, root_line, CODEX_TOOLS_TABLE, CODEX_MEMORIES_TABLE,
            features, mcp_sections, provider_table,
        ) if p]
        config_text = "\n\n".join(parts) + "\n"
        self._mcp_server_names = mcp_server_names_from_toml(config_text)
        config_path = self._codex_dir / "config.toml"
        if config_text:
            _validate_config_toml(config_text, config_path)
        write_or_drop_model_catalog(self._codex_dir, local_provider)
        config_path.write_text(config_text)
        chmod_private(config_path)
        write_or_drop_auth_json(self._codex_dir, self.config.get("auth_json"))
        _write_codex_hooks(self._codex_dir)

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
        env["CODEX_HOME"] = str(self._codex_dir)
        env["OTO_SESSION_ID"] = self.session_id
        # Interpreter for the Windows .cmd hook wrappers (codex_hook_command).
        env["OTO_HOOK_PY"] = sys.executable
        if self._hooks_floor:
            # The hook processes inherit the daemon env. Codex's PreToolUse
            # hook rejects permissionDecision:"allow", so permission_gate.py
            # emits JSON only to DENY; the PostToolUse forwarder stays quiet
            # (the JSON-RPC stream already carries every tool result — a
            # forward would render each card twice). Assigned, not defaulted:
            # a stray operator variable must not win. Twin of the local
            # layer's _build_env; the interactive TUI path sets DENY_ONLY too.
            env["OTO_HOOK_DENY_ONLY"] = "1"
            env["OTO_HOOK_NO_FORWARD"] = "1"
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # Per-session secret files (SSH keys + OAuth token files) — see
        # session_files.py; injects OTO_SSH_KEY_DIR and lands token files
        # at their agent-tree target before spawn (mirrors cli_session).
        # No-op without a payload token.
        from . import session_files
        env.update(await asyncio.to_thread(
            session_files.materialize, self.config, self.session_id,
            self.agent_dir,
        ))
        env = path_translator.translate_env(
            env, agent_dir=self.agent_dir, username=self._username,
            session_id=self.session_id,
            multi_value_envs=self.config.get("multi_value_envs") or {},
        )
        # The native-approval bridge posts to the proxy's /v1/hooks/permission
        # over this loopback tunnel (translate_env rewrote PROXY_URL to it).
        self._proxy_url = env.get("PROXY_URL", "")
        self._proxy_api_key = env.get("PROXY_API_KEY", "")

        await self._connect_with_retry(env)

        overrides = self._thread_overrides()
        if self.thread_id:
            try:
                await self._client.request("thread/resume", {"threadId": self.thread_id, **overrides})
            except AppServerError as e:
                if self._note_untrusted_rejection(e):
                    overrides = self._thread_overrides()
                    await self._client.request("thread/resume", {"threadId": self.thread_id, **overrides})
                else:
                    logger.warning(f"Codex resume failed ({e}); starting new thread")
                    self.thread_id = None
        if not self.thread_id:
            try:
                res = await self._client.request("thread/start", overrides)
            except AppServerError as e:
                if not self._note_untrusted_rejection(e):
                    raise
                res = await self._client.request("thread/start", self._thread_overrides())
            self.thread_id = (res.get("thread") or {}).get("id") or ""

        await self._warm_mcps()
        # The forwarder now becomes the SOLE consumer of notif_queue (warm-up
        # drained the startup notifications itself, above).
        self._forwarder_task = asyncio.create_task(self._run_forwarder())
        self.pid = self._client.proc.pid if self._client.proc else 0
        logger.info(f"Codex app-server ready: session={self.session_id}, thread={self.thread_id}, pid={self.pid}")

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
        # Chat uploads arrive as sandbox-virtual paths — translate like run_turn.
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
            logger.info(f"Codex [{self.session_id[:8]}] steer rejected: {e}")
            return False
        logger.info(f"Codex [{self.session_id[:8]}] steered turn {self._current_turn_id}")
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
                            f"Codex [{self.session_id[:8]}] compacted thread "
                            f"(post_tokens={post_tokens})"
                        )
                        return {"post_tokens": post_tokens}
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] compaction timed out"
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
                            f"Codex [{self.session_id[:8]}] compacted thread "
                            f"(post_tokens={post_tokens})"
                        )
                        return {"post_tokens": post_tokens}
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] compaction turn ended "
                        f"without a contextCompaction item"
                    )
                    return None
                if method == "error" and not (
                        (params.get("error") or {}).get("willRetry")):
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] compaction error: "
                        f"{(params.get('error') or {}).get('message')}"
                    )
                    return None
        except AppServerError as e:
            logger.warning(f"Codex [{self.session_id[:8]}] compaction failed: {e}")
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
                f"Codex [{self.session_id[:8]}] backgroundTerminals list failed: {e}")
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
            logger.info(f"Codex daemon dead for {self.session_id}; re-warming")
            self._client = None
            await self.start()

        if inject_time:
            prompt = f"[Current time: {_format_time()}]\n{prompt}"
        # Translate sandbox-virtual paths (chat photos/files) to satellite-absolute.
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
            "approvalsReviewer": "user",
            "sandboxPolicy": build_sandbox_policy(sandbox_mode, str(self._cwd or "")),
            # Plan collaboration mode, re-asserted per turn (mirror of the local
            # layer): plan iff the platform mode is `plan` (0.5.123 — a check's
            # judge runs the read-only sandbox WITHOUT planning); minimal
            # {mode:...} so the user's effort isn't overridden by a preset.
            "settings": {"collaborationMode": {
                "mode": "plan" if self._plan_mode(sandbox_mode) else "default"}},
        }
        model = self.config.get("model", "")
        effort = self.config.get("effort", "")
        if model:
            turn_params["model"] = model
        if effort:
            turn_params["effort"] = effort

        self._main_turn_done.clear()
        self._stop_requested = False
        try:
            res = await self._client.request("turn/start", turn_params)
        except AppServerError as e:
            if not self._note_untrusted_rejection(e):
                raise
            turn_params["approvalPolicy"] = self._approval_policy()
            res = await self._client.request("turn/start", turn_params)
        self._current_turn_id = (res.get("turn") or {}).get("id")
        try:
            await self._main_turn_done.wait()
        finally:
            self._current_turn_id = None

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
                # Tell the proxy + release any waiting run_turn.
                await self._forward({"method": "error", "params": {
                    "error": {"message": "Codex app-server exited unexpectedly"},
                }})
                self._main_turn_done.set()
                return
            if method == "item/started":
                self._track_item_paths(params)
            await self._forward({"method": method, "params": params})
            # End the active run_turn on the MAIN thread's terminal ONLY. A
            # spawned sub-agent runs on its OWN thread and emits its own
            # turn/completed MID-turn (foreground/waited) or AFTER the main turn
            # (background) — neither ends the main turn; we keep forwarding them
            # so the proxy can track + supervise the background ones.
            ev_tid = params.get("threadId") if isinstance(params, dict) else None
            is_main = (not ev_tid) or (not self.thread_id) or (ev_tid == self.thread_id)
            if is_main and method == "turn/completed":
                self._main_turn_done.set()
            elif (is_main and method == "error"
                    and not (params or {}).get("error", {}).get("willRetry")):
                self._main_turn_done.set()

    async def _forward(self, event: dict) -> None:
        """Ship one notification to the proxy via the session_manager-set cb."""
        cb = self._forward_event
        if cb is None:
            return
        try:
            await cb(event)
        except Exception:
            logger.exception(f"Codex forward failed for {self.session_id}")

    async def _stop_forwarder(self) -> None:
        """Cancel + await the persistent forwarder (idempotent)."""
        if self._forwarder_task is not None:
            self._forwarder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._forwarder_task
            self._forwarder_task = None

    async def abort(self) -> None:
        """turn/interrupt the in-flight turn; the daemon stays warm."""
        if self._client is None or not self._client.is_alive or not self._current_turn_id:
            return
        try:
            await self._client.request("turn/interrupt", {
                "threadId": self.thread_id, "turnId": self._current_turn_id,
            }, timeout=10.0)
        except AppServerError as e:
            logger.warning(f"Codex interrupt failed for {self.session_id}: {e}")
        logger.info(f"Codex session aborted: {self.session_id}")

    async def close(self) -> None:
        self._closed = True
        await self._stop_forwarder()
        self._main_turn_done.set()  # release any run_turn still waiting
        if self._client is not None:
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
        # Per-session secret files (SSH keys) die with the session.
        from . import session_files
        session_files.wipe(self.session_id)
        logger.info(f"Codex session closed: {self.session_id}")

    async def send_control_request(self, subtype: str, **kwargs) -> None:
        """Model/mode change → applied as a per-turn override on the next turn
        (the persistent daemon picks it up on the next turn/start — no respawn).
        """
        if subtype == "set_model" and "model" in kwargs:
            self.config["model"] = kwargs["model"]
        elif subtype == "set_permission_mode" and "sandbox_mode" in kwargs:
            self.config["sandbox_mode"] = kwargs["sandbox_mode"]

    def detect_file_changes(self) -> list[dict]:
        return file_sync.detect_changes(self.agent_dir, self._file_snapshot)

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._client is not None and self._client.is_alive

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _thread_overrides(self) -> dict:
        # Approval policy is derived from the sandbox mode: danger-full-
        # access → never; else on-request, so a sandbox escape fires an approval
        # we route — over the loopback tunnel → /v1/hooks/permission — through
        # the proxy's decide_tool_permission. turn/start re-asserts these per
        # turn with the structured sandboxPolicy. (No bwrap on the satellite;
        # Codex's own sandbox is the FS boundary.) Under `never` nothing fires,
        # so unattended sessions run the PreToolUse hook floor instead — the
        # hook posts to the same endpoint over the same tunnel.
        sandbox_mode = self.config.get("sandbox_mode", "workspace-write")
        overrides: dict = {
            "approvalPolicy": self._approval_policy(),
            "approvalsReviewer": "user",
            "sandbox": sandbox_mode,
        }
        if self.config.get("model"):
            overrides["model"] = self.config["model"]
        if self.config.get("effort"):
            overrides["effort"] = self.config["effort"]
        if self._hooks_floor:
            # Codex runs a user-layer hooks.json only when trusted; the
            # app-server has no CLI flag for it (the TUI's
            # --dangerously-bypass-hook-trust), the per-thread `config` map is
            # the switch (codex-rs app-server/src/config_manager.rs). Sent on
            # thread/start AND thread/resume (same dict). The platform wrote
            # the hook it is trusting. Twin of the local _thread_overrides.
            overrides["config"] = {"bypass_hook_trust": True}
        return overrides

    async def _decide_permission_remote(self, tool_name: str, tool_input: dict) -> dict:
        """The injected decision authority for the approval bridge (remote).

        The satellite is the JSON-RPC client but the decision authority lives on
        the proxy, so we POST the translated tool to ``/v1/hooks/permission`` over
        the loopback tunnel (the same channel the CLI hook uses) and block on the
        proxy's verdict. Fail-closed (deny) if the tunnel/credentials are missing
        or the request fails — e.g. a WS drop mid-wait — so the daemon's escape is
        rejected cleanly and resilience reconnect handles the turn.
        """
        if not self._proxy_url or not self._proxy_api_key:
            logger.warning("Codex approval: no proxy tunnel coords; denying")
            return {"decision": "deny"}
        import aiohttp
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{self._proxy_url}/v1/hooks/permission",
                    json={"session_id": self.session_id,
                          "tool_name": tool_name, "tool_input": tool_input},
                    headers={"Authorization": f"Bearer {self._proxy_api_key}"},
                    timeout=aiohttp.ClientTimeout(total=604800),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Codex approval: proxy returned {resp.status}; denying")
                        return {"decision": "deny"}
                    return await resp.json()
        except Exception as e:  # noqa: BLE001 — fail closed on any transport error
            logger.warning(f"Codex approval: tunnel call failed ({e}); denying")
            return {"decision": "deny"}

    async def _ask_question_remote(self, questions: list) -> dict:
        """Injected question authority for request_user_input (remote).

        The daemon holds the turn open on the question; the answering human is on
        the proxy dashboard, so we POST the questions to ``/v1/hooks/codex-question``
        over the loopback tunnel and block on the proxy surfacing the card + the
        human answer. Returns the answers MAP ``{<id>: {"answers": [...]}}``.
        Fail-safe to empty answers on any transport error so the held turn unwinds
        (the model continues rather than hanging), matching the local decline path.
        """
        if not self._proxy_url or not self._proxy_api_key:
            logger.warning("Codex question: no proxy tunnel coords; empty answer")
            return {}
        import aiohttp
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{self._proxy_url}/v1/hooks/codex-question",
                    json={"session_id": self.session_id, "questions": questions},
                    headers={"Authorization": f"Bearer {self._proxy_api_key}"},
                    timeout=aiohttp.ClientTimeout(total=604800),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Codex question: proxy returned {resp.status}; empty answer")
                        return {}
                    data = await resp.json()
                    return (data or {}).get("answers") or {}
        except Exception as e:  # noqa: BLE001 — never hang the turn on a transport error
            logger.warning(f"Codex question: tunnel call failed ({e}); empty answer")
            return {}

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

    async def _connect_with_retry(self, env: dict) -> None:
        """Spawn the daemon + initialize, resetting a stale state runtime once.

        ``codex app-server`` keeps SQLite runtime DBs directly under
        CODEX_HOME. A copy written by a different codex version — or one that
        arrived from another machine — makes the daemon abort during init
        ("migration N was previously applied but has been modified"). On that
        failure we delete the runtime DBs (the thread rollouts in
        ``sessions/`` are untouched, so history survives) and retry once.
        """
        from ..host.cli_versions import resolve_spawn_bin_async
        codex_bin = await resolve_spawn_bin_async(
            "codex", self.sat_config.bin_hint("codex"),
        )
        last_err: Exception | None = None
        for attempt in (1, 2):
            self._client = AppServerClient(
                env=env, cwd=str(self._cwd),
                codex_bin=codex_bin,
                label=f"codex[{self.session_id[:8]}]",
            )
            self._client.set_server_request_handler(make_server_request_handler(
                self._decide_permission_remote,
                ask_question=self._ask_question_remote,
                get_item_paths=lambda: self._item_paths,
                log=lambda m: logger.info(f"Codex [{self.session_id[:8]}] {m}"),
            ))
            try:
                await self._client.start({
                    "clientInfo": {"name": "otodock-satellite", "title": "OtoDock", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                return
            except AppServerError as e:
                last_err = e
                await self._client.close()
                if attempt == 1:
                    logger.warning(
                        f"Codex app-server init failed ({e}); resetting state runtime + retrying"
                    )
                    self._reset_codex_state()
        raise last_err if last_err else RuntimeError("codex app-server init failed")

    def _reset_codex_state(self) -> None:
        """Delete codex's SQLite runtime DBs in CODEX_HOME (regenerable).

        Every DB family the daemon keeps directly under CODEX_HOME (state,
        logs, goals, memories, and whatever future releases add) can carry the
        "migration … has been modified" poison, so wipe them all — thread
        rollouts in ``sessions/`` survive. Matches ``_CODEX_RUNTIME_GLOBS`` in
        transport/file_sync.py so the reset covers exactly what is excluded
        from sync.
        """
        if not self._codex_dir:
            return
        for path in self._codex_dir.glob("*.sqlite*"):
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"Codex state reset: couldn't delete {path.name}: {e}")

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
                f"Codex MCP warm-up ({self.session_id}) hit the {cap:.0f}s cap after "
                f"{res.elapsed:.1f}s: still starting={res.pending} ready={res.ready} "
                f"failed={res.failed}{extra}"
            )
        else:
            logger.info(
                f"Codex MCP warm-up ({self.session_id}) done in {res.elapsed:.1f}s: "
                f"ready={res.ready} failed={res.failed}{extra}"
            )
