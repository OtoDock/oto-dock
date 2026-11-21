"""The Codex engine's spawn: the plan a session starts from, the config
file it reads, and the TUI's environment and flags.
"""

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import tomllib

import config as app_config
from core import file_commit, layout, placement
from core.engine import Engine
from core.execution_layer import (
    AgentConfig,
    CredentialFileSpec,
    SessionHomeSpec,
    SpawnPlan,
)
from core.layers.codex import session_text
from core.layers.codex.app_server_client import mcp_server_names_from_toml
from core.layers.codex.env import daemon_env
from core.layers.codex.helpers import (
    LOCAL_ENDPOINT_KEY_ENV as _LOCAL_ENDPOINT_KEY_ENV,
    LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS as _STREAM_IDLE_TIMEOUT_MS,
    codex_hooks_floor,
    map_effort_to_codex as _map_effort_to_codex,
    permission_to_sandbox as _permission_to_sandbox,
    tui_box_network_flags,
    with_box_network_note,
    with_local_provider_note,
)
from core.layers.codex.local_model_catalog import (
    CATALOG_FILE_NAME as _CATALOG_FILE_NAME,
    LOCAL_MODEL_ROWS_ENV as _LOCAL_MODEL_ROWS_ENV,
    catalog_toml_line as _catalog_toml_line,
    local_model_catalog_json,
    parse_local_model_rows,
)
from core.layers.codex.mcp_toml import (
    _inject_enabled_tools_toml,
    _inject_fetch_tokens_toml,
    _inject_proxy_vars_toml,
    _inject_session_id_toml,
    _inject_session_jwt_toml,
    with_tool_approvals,
)
from core.sandbox.session_config_dir import unlink_in, write_if_changed
from core.session import session_kind
from storage.pg import run_db

if TYPE_CHECKING:
    from auth.path_policy import SecurityContext
    from core.sandbox.sandbox import SandboxBuilder

logger = logging.getLogger("codex-layer")


# config.toml [tools] table every session gets — see _write_config_toml.
_CODEX_TOOLS_TABLE = "[tools]\nupdate_plan = { enabled = true }"

def _tui_env(session_id: str, agent_name: str, username: str, user_role: str,
             codex_bin: str, extra_env: dict[str, str]) -> dict[str, str]:
    """The bare Codex TUI's env: the daemon's (``env.daemon_env``) with
    TERM and ``OTO_INTERACTIVE`` (the PostToolUse forwarder no-ops;
    ``decide_tool_permission`` answers "ask" for the ask tier, the deny-only
    gate stays silent on it, so Codex's own ``-a on-request`` prompts), and,
    last, the shell's command-line token over the final session token."""
    import os

    from auth.session_token import attach_cli_token
    from core.sandbox.env_builder import build_session_env
    proc_env = build_session_env(session_id, agent_name, username=username, user_role=user_role)
    codex_dir = os.path.dirname(os.path.realpath(codex_bin))
    if codex_dir and codex_dir not in proc_env.get("PATH", ""):
        proc_env["PATH"] = codex_dir + ":" + proc_env.get("PATH", "/usr/bin:/bin")
    proc_env.update(extra_env)  # CODEX_HOME + subscription + credential + proxy vars
    proc_env["TERM"] = proc_env.get("TERM") or "xterm-256color"
    # xterm.js renders 24-bit; without the hint the TUI downgrades to
    # 256-color SGR (see the Claude twin in cli/spawn.py).
    proc_env.setdefault("COLORTERM", "truecolor")
    proc_env["OTO_INTERACTIVE"] = "1"
    # Codex's PreToolUse hook rejects permissionDecision:"allow" — tell
    # permission_gate.py to emit JSON only to DENY (allow/defer → silent
    # exit 0 → Codex proceeds). The hard-deny FLOOR still enforces.
    proc_env["OTO_HOOK_DENY_ONLY"] = "1"
    attach_cli_token(proc_env)
    return proc_env

def _tui_flags(*, sandbox_mode: str, cwd: str, model: str, permission_mode: str,
               effort: str) -> list[str]:
    """The bare Codex TUI's flags, which precede the optional PROMPT
    (``codex [opts] [PROMPT]``). ``--no-alt-screen`` = inline mode (matches
    Claude's inline model + keeps xterm scrollback); the command-level FLOOR
    runs via .codex/hooks.json (config has hooks=true) with
    ``--dangerously-bypass-hook-trust`` (the platform vets the hook source).
    Sandbox/approval are derived from the permission mode (same mapping as
    the app-server), the box's network is off in every mode
    (``tui_box_network_flags``). ``--no-daemon``: Codex 0.157 auto-starts a
    shared background server (its own Codex install, outside the pin) for
    eligible TUIs; the trust flag already keeps this TUI out of it, and
    ``--no-daemon`` turns the auto-start off with the "Running without the
    shared background server" line it printed at start (the flag exists on
    the previous pin 0.156.1 too)."""
    from core.layers.codex.codex_approvals import (
        approval_for_sandbox,
        tui_reviewer_flags,
    )
    flags = [
        "--no-alt-screen",
        "-s", sandbox_mode,
        "-a", approval_for_sandbox(sandbox_mode),
        "--dangerously-bypass-hook-trust",
        "--no-daemon",
        "-C", cwd,
    ]
    if model:
        flags += ["-m", model]
    flags += tui_reviewer_flags(permission_mode)
    flags += tui_box_network_flags()
    if effort:
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    return flags

def _resume_provider_changed(host_codex_home: str, thread_id: str, local: bool, agent: str = "") -> bool:
    """Whether the thread's rollout recorded another model provider than the
    one this spawn writes (``oto_local`` for a local endpoint, else Codex's
    built-in ``openai``). Unknown reads as unchanged. The only check on a
    terminal's provider: from 0.168 ``codex resume <id>`` resumes a thread on
    the provider its rollout recorded, whatever ``config.toml`` names, with
    no notice (0.166 exited, 0.167 parked on a notice). The rollout is found
    and its first record read beneath the agent's real folder
    (``codex_rollout_tailer.find_rollout``, ``read_session_meta``), named by
    ``agent`` when the caller knows it. Blocking: off the event loop."""
    from core.session import codex_rollout_tailer as rollouts
    state, path = rollouts.find_rollout(host_codex_home, thread_id, agent)
    if state != rollouts.FOUND or path is None:
        return False
    payload = rollouts.read_session_meta(path, agent)
    recorded = payload.get("model_provider") if payload else None
    if not isinstance(recorded, str) or not recorded:
        return False
    return recorded != ("oto_local" if local else "openai")

def _log_invalid_toml(label: str, config_path: Path, text: str,
                      e: tomllib.TOMLDecodeError) -> None:
    """Log a generated TOML that does not parse: never handed to Codex (the
    strict TUI exits on it with a blank terminal, the app-server drops every
    MCP). The offending line with its quoted values REDACTED (env blocks
    carry tokens). tomllib carries the position only in the message ("(at
    line N, column M)"); older builds exposed a ``lineno`` attribute."""
    lineno = getattr(e, "lineno", None)
    if not isinstance(lineno, int):
        m = re.search(r"at line (\d+)", str(e))
        lineno = int(m.group(1)) if m else None
    bad = ""
    if isinstance(lineno, int) and lineno >= 1:
        lines = text.splitlines()
        if lineno <= len(lines):
            bad = re.sub(r'"[^"]*"', '"▮"', lines[lineno - 1])[:200]
    logger.error("generated codex %s is INVALID (%s): %s | line: %s",
                 label, config_path, e, bad or "<unavailable>")


@dataclass
class _Prepared:
    """What a start readies before its spawn: the parts of the plan a
    machine spawns from (``prepare_spawn``)."""
    sandbox_builder: "SandboxBuilder"
    extra_env: dict[str, str]
    sandbox_mode: str
    codex_effort: str
    username: str
    user_role: str
    work_dir: str
    mcp_toml_content: str
    local_endpoint: str
    wrote_auth_json: bool
    codex_bin: str
    hooks_floor: bool
    mcp_names: tuple[str, ...]
    # The session's own text and settings (``_write_config_toml``): the
    # app-server takes them on its thread, the terminal from its profile.
    session_text: str = ""
    session_config: dict | None = None


class _CodexSpawn(Engine):
    """A session's spawn plan and its ``config.toml``."""

    async def _prepare(self, session_id: str, config: AgentConfig) -> "_Prepared":
        """Everything a start readies before its spawn: the egress and
        mount resolve, the SSH keys, the token files, the MCP TOML with its
        tokens and wrappers, ``config.toml`` and the session's own files,
        the login file, the broker. Shared by the local spawn and ``prepare_spawn``."""
        # Fail CLOSED: a local agent MUST run sandboxed + network-isolated. The
        # codex layer reads sandbox_host_claude_dir AS CODEX_HOME; empty means a
        # config-builder omission — refuse rather than launch un-sandboxed.
        if not config.sandbox_host_claude_dir:
            raise RuntimeError(
                f"refusing to plan a Codex session for agent "
                f"'{config.agent_name}' without a sandbox dir: every session "
                f"runs sandboxed and network-isolated."
            )
        # Below the editor tier a session never runs from the agent's own
        # CODEX_HOME (its sandbox masks it).
        from core.sandbox.session_config_dir import refuse_session_on_agent_state
        refuse_session_on_agent_state(config.security_context)
        mcp_path = Path(config.mcp_config_path) if config.mcp_config_path else None
        extra_env = dict(config.extra_env)
        # Merge MCP credential env vars (Google tokens, API keys, etc.)
        # These propagate to MCP child processes via environment inheritance.
        if config.credential_env:
            extra_env.update(config.credential_env)
        # A local OpenAI-compatible subscription (ollama / openai_compatible)
        # carries its base URL here. Pop it now so it never leaks into the child
        # env; it drives (a) the Codex model_provider written into config.toml
        # below and (b) the sandbox egress carve-out to that endpoint.
        # Empty for OpenAI / ChatGPT subscriptions → Codex uses its built-in
        # provider and no carve-out is needed.
        local_endpoint = extra_env.pop("_CODEX_ENDPOINT_URL", "")
        # An API key protecting the local endpoint: handed to Codex through
        # the custom provider's ``env_key`` (a named child-env variable), so
        # it never becomes Codex's own sign-in.
        local_api_key = extra_env.pop("_CODEX_LOCAL_API_KEY", "")
        # The subscription's provider (ollama / openai_compatible) picks the
        # AGENTS.md note about MCP tools on a local server; its model rows
        # (read off-loop by the pool) feed the per-session model catalog.
        local_provider = extra_env.pop("_CODEX_ENDPOINT_PROVIDER", "")
        local_model_rows = parse_local_model_rows(extra_env.pop(_LOCAL_MODEL_ROWS_ENV, ""))
        if local_endpoint:
            # A local endpoint on the proxy host itself (T1) is reached via the
            # loopback splice — rewrite its host to 127.0.0.1 so the Codex CLI
            # (which dials it from the sandbox) hits the forwarded port. No-op in
            # T2 / for remote endpoints.
            from services.mcp import mcp_registry as _mr
            local_endpoint = _mr.loopback_if_host_self(local_endpoint)
            if local_api_key:
                extra_env[_LOCAL_ENDPOINT_KEY_ENV] = local_api_key

        # Map permission mode → Codex sandbox mode
        sandbox_mode = _permission_to_sandbox(config.permission_mode)

        # Determine working directory from the MOUNT scope — Shared-only is
        # agent-scope even with a human present, so it works in /workspace, not
        # /users/{u}. ``mount_username`` is "" for any agent-scope mount.
        agent_dir = app_config.get_agent_dir(config.agent_name)
        _ctx = cast("SecurityContext | None", config.security_context)
        username = _ctx.mount_username if _ctx else ""
        from core.session.external_identity import external_home_of, is_external_ctx
        _ext_home = external_home_of(_ctx)
        if _ext_home:
            work_dir = _ext_home   # an external caller's own tree (/caller)
        elif username:
            work_dir = str(layout.user_dir(agent_dir, username))
        else:
            work_dir = str(agent_dir / layout.WORKSPACE)

        # Sandbox setup

        from core.sandbox.sandbox import (
            SandboxBuilder,
            mcp_sandbox_mounts,
            own_engine_home,
            resolve_sandbox_config,
        )
        from services.mcp import mcp_registry

        # The MCPs the session's build attached (the assigned set minus
        # ``AgentConfig.excluded_mcps``): their manifest mounts and their
        # routes alone. is_remote=False (fail-closed default, explicit here):
        # LOCAL bwrap mount path — satellite_only device MCPs run native on
        # the satellite and must never be mounted locally.
        attached = [
            m for m in (await run_db(mcp_registry.get_agent_mcps, config.agent_name,
                                     placement=placement.LOCAL_PLACEMENT) or [])
            if m.name not in (config.excluded_mcps or ())
        ]
        mcp_mounts = mcp_sandbox_mounts(attached)

        # Mount the Codex CLI installation directory inside the sandbox
        # (it lives in ~/.npm-global/ which isn't in the default system mounts).
        # Shared helper — the CLI layer mounts CLAUDE_BIN the same way.
        from core.sandbox.sandbox_render import cli_install_ro_binds
        codex_bin = getattr(app_config, "CODEX_BIN", "codex")
        codex_extra_mounts = cli_install_ro_binds(codex_bin)

        sandbox_cfg = await run_db(
            resolve_sandbox_config,
            role=cast("SecurityContext", config.security_context).role if config.security_context else "viewer",
            username=username,   # already the MOUNT username (see work_dir above)
            agent_name=config.agent_name,
            is_admin_agent=cast("SecurityContext", config.security_context).is_admin_agent if config.security_context else False,
            host_claude_dir=Path(config.sandbox_host_claude_dir),
            user_sub=config.user_sub,
            mcp_sandbox_mounts=mcp_mounts,
            account_choices=config.account_choices or None,
            extra_ro_binds=codex_extra_mounts,
            # Carve sandbox egress to a local OpenAI-compatible LLM endpoint the
            # Codex CLI dials directly (Ollama / openai_compatible). Empty for
            # OpenAI/ChatGPT subscriptions.
            extra_egress_targets=[local_endpoint] if local_endpoint else None,
            config_visible=_ctx.config_visible if _ctx else None,
            mount_shared=_ctx.mount_shared if _ctx else True,
            knowledge_rw=bool(getattr(_ctx, "knowledge_rw", False)) if _ctx else False,
            # TOML build file with host mcps/ paths — scanned (plain-text)
            # for the per-MCP dir binds.
            mcp_config_path=config.mcp_config_path,
            external=is_external_ctx(_ctx),
            external_home=_ext_home,
            attached_mcps=[m.name for m in attached],
            outside_files_write=bool(getattr(_ctx, "outside_files_write", False)),
            outside_knowledge=tuple(getattr(_ctx, "outside_knowledge", ()) or ()),
            wrapup_home=getattr(_ctx, "wrapup_home", "") if getattr(_ctx, "wrapup", "") else None,
            engine_home=own_engine_home(_ctx, config.sandbox_host_claude_dir),
            # The session's own home, bound in over the masked homes
            # (``session_config_dir.session_home_for``).
            session_home=config.session_home,
            # A compute machine on another server opens no tool's local
            # address: its network is not the platform's.
            own_copy=bool(getattr(getattr(_ctx, "placement", None), "own_copy", False)),
        )
        sandbox_builder = SandboxBuilder(sandbox_cfg)

        env_overrides = sandbox_builder.get_env_overrides(
            config_dir_name=self.capabilities.runtime.config_dir_name,
            config_env_var="CODEX_HOME",
        )
        extra_env.update(env_overrides)
        # The store as Codex sees it INSIDE the sandbox (config.toml paths):
        # the hook scripts, the keys, the worktrees beside it all stay named
        # by it.
        sandbox_codex_dir = env_overrides.get("CODEX_HOME", "/workspace/.codex")
        # The folder the engine reads: the session's home when it has one
        # (its settings, hooks, profile, text and sign-in), else the store.
        codex_home = Path(config.session_home or config.sandbox_host_claude_dir)
        sandbox_home = sandbox_codex_dir
        if config.session_home:
            from core.sandbox import session_home
            sandbox_home = f"{sandbox_codex_dir}/{session_home.relative(session_id)}"
            # The rollouts and the databases stay the store's: the home links
            # its ``sessions`` and Codex keeps its databases where this names.
            extra_env["CODEX_HOME"] = sandbox_home
            extra_env["CODEX_SQLITE_HOME"] = sandbox_codex_dir
            await asyncio.to_thread(self._make_home, session_id, Path(config.sandbox_host_claude_dir))
        from core.layers.codex.config_dir import write_hooks
        await asyncio.to_thread(write_hooks, codex_home, sandbox_codex_dir)

        # ssh-hosts: provision the agent's authorized SSH keys into
        # <.codex>/ssh so the prompt's `ssh -i "$OTO_SSH_KEY_DIR/…"` lines
        # work from the Codex shell (mirrors the CLI layer). A session
        # that takes no keys is left none from an earlier one (the helper's
        # rule).
        from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox
        ssh_dir = await provision_ssh_keys_for_sandbox(
            _ctx, config.agent_name, config.sandbox_host_claude_dir, sandbox_codex_dir,
        )
        if ssh_dir:
            extra_env["OTO_SSH_KEY_DIR"] = ssh_dir

        # The credential exit and the SSH agent socket, as the
        # Claude layer attaches them; the exit's variables also reach each
        # stdio MCP's env table below (``_inject_proxy_vars_toml``), since
        # Codex spawns its MCPs from the file.
        from core.credentials import credential_exit, ssh_signer
        machine = placement.compute_of(getattr(_ctx, "placement", None))
        await credential_exit.attach_sandbox(session_id, sandbox_builder, extra_env,
                                             config.sandbox_host_claude_dir,
                                             sandbox_config_dir=sandbox_codex_dir,
                                             machine=machine)
        await ssh_signer.attach_sandbox(session_id, _ctx, sandbox_builder, extra_env, compute=machine is not None)

        # Read MCP TOML content (generated by build_session_mcp_config with format="toml")
        mcp_toml_content = ""
        if mcp_path and mcp_path.exists():
            mcp_toml_content = mcp_path.read_text()

        # Inject session_id into HTTP MCP URLs (same purpose as CLI's
        # _inject_session_id_into_sse — Docker MCPs need it for path resolution)
        if mcp_toml_content:
            mcp_toml_content = _inject_session_id_toml(
                mcp_toml_content, session_id,
            )
            # Swap the session-JWT sentinel (Docker MCPs w/ server.proxy_callbacks,
            # e.g. file-tools) for a real session JWT now that session_id is known.
            mcp_toml_content = _inject_session_jwt_toml(
                mcp_toml_content, session_id, config.agent_name,
            )

        # Inject proxy callback env vars into each stdio MCP's TOML env section.
        # These are also set on the Codex process itself (via _build_env()), but
        # Codex spawns MCPs from TOML config which may not inherit the parent
        # process env. Both injection points are needed:
        # - _build_env(): for hooks (permission_gate.py, tool_result_forwarder.py)
        # - TOML env: for MCPs (display, location, notifications, etc.)
        # Note: OTO_* (incl. OTO_SESSION_ID) are baked into the env block by
        # config_builder via inject_credential_env_into_toml — don't re-inject
        # here or TOML parse fails with "duplicate key". The chat and task
        # builders bake PROXY_API_KEY in too, naming the session's person; the
        # token below, which names nobody, reaches only a config that carries
        # none (the meeting and external phone builders mint no token).
        if mcp_toml_content:
            from auth.session_token import create_session_token
            proxy_vars = {
                "PROXY_URL": f"http://localhost:{app_config.PORT}",
                "PROXY_API_KEY": create_session_token(session_id, config.agent_name),
            }
            # The exit's variables, when the session has them: every tool
            # child reads the proxy and trust variables from its own env.
            proxy_vars.update({k: extra_env[k] for k in credential_exit.ENV_NAMES if k in extra_env})
            mcp_toml_content = _inject_proxy_vars_toml(mcp_toml_content, proxy_vars)

            # Credential broker: inject the per-(session, mcp) cap-token
            # into each stdio MCP that has a secret bundle, then wrap its command
            # with the stdio interceptor (copied into the sandbox .codex/ dir) so
            # it fetches its secrets at spawn. Codex gates MCP TOOL CALLS natively
            # (mcpServer/elicitation/request) — this interceptor is pure credential
            # delivery, not a permission gate, so it never double-prompts.
            bundle_keys = set(config.mcp_secret_bundles or {})
            if bundle_keys:
                mcp_toml_content = _inject_fetch_tokens_toml(
                    mcp_toml_content, bundle_keys, session_id,
                )
            from core.sandbox.interceptor_wrap import wrap_toml_text
            from core.sandbox.session_config_dir import sandbox_python
            mcp_toml_content = wrap_toml_text(
                mcp_toml_content, interpreter=sandbox_python(), interpreter_args=("-I",),
                interceptor_path=f"{sandbox_codex_dir}/stdio_path_interceptor.py",
            )

        # The per-session model catalog (Ollama only): Codex defers its MCP
        # tools and searches them on demand instead of sending every schema on
        # every request — see local_model_catalog. Every row of the provider
        # rides along (a same-provider model switch keeps its entry); the
        # context window is the row's when the admin set one.
        local_catalog_json = ""
        if local_endpoint:
            local_catalog_json = local_model_catalog_json(
                config.model, local_provider, local_model_rows,
            )

        # Write combined config.toml (instructions + MCP servers). The hook
        # floor (`[features] hooks = true`) is written for every session; an
        # external caller also loses the shell tool (see _write_config_toml).
        config_dir = codex_home
        # ONE rule with the remote start payload (helpers.codex_hooks_floor).
        hooks_floor = codex_hooks_floor(config.client_type, config.interactive)
        walls = self._tool_walls(_ctx)
        if walls["tool_grants"]:
            mcp_toml_content = _inject_enabled_tools_toml(mcp_toml_content, walls["tool_grants"])
        own_text, own_config = self._write_config_toml(
            config_dir, config.system_prompt, mcp_toml_content,
            interactive=config.interactive,
            trusted_cwd=(sandbox_builder.get_cwd() if config.interactive else ""),
            local_endpoint=local_endpoint,
            local_endpoint_keyed=bool(local_endpoint and local_api_key),
            local_provider=local_provider,
            client_type=config.client_type,
            local_catalog_json=local_catalog_json,
            sandbox_codex_dir=sandbox_codex_dir,
            sandbox_home_dir=sandbox_home,
            session_id=session_id, for_compute=machine is not None,
            **walls,
        )

        # The sign-in rides as the full auth.json payload (the pool's adapter
        # built it: a ChatGPT login with its refresh token neutralized, or an
        # OpenAI key); write it verbatim with the same writer the rotation
        # fan-out uses, so spawn and fan-out cannot drift. A session without
        # one must not inherit a previous session's file from this persistent
        # CODEX_HOME.
        auth_json = self.credential_file_from_env(extra_env)
        wrote_auth_json = auth_json is not None
        if wrote_auth_json:
            from services.engines.token_fanout import write_credential_file
            write_credential_file(
                codex_home,
                cast("CredentialFileSpec", self.capabilities.auth.credential_file).filename, cast("dict", auth_json),
            )
        else:
            self._drop_stale_auth_json(codex_home)

        # Create session (thread_id from DB enables resume after proxy restart)
        codex_effort = _map_effort_to_codex(config.effort, config.model)
        _user_role = (
            cast("SecurityContext", config.security_context).role if config.security_context else ""
        )

        # Credential broker: populate the per-session secret store so
        # the wrapped stdio MCPs can fetch at spawn (must precede the daemon).
        from core.credentials import mcp_broker
        mcp_broker.provision(session_id, config.mcp_secret_bundles or {})
        return _Prepared(
            sandbox_builder=sandbox_builder, extra_env=extra_env, sandbox_mode=sandbox_mode,
            codex_effort=codex_effort, username=username, user_role=_user_role, work_dir=work_dir,
            mcp_toml_content=mcp_toml_content, local_endpoint=local_endpoint,
            wrote_auth_json=wrote_auth_json, codex_bin=codex_bin, hooks_floor=hooks_floor,
            mcp_names=tuple(m.name for m in attached),
            session_text=own_text, session_config=own_config,
        )

    async def prepare_spawn(self, session_id: str, config: AgentConfig) -> SpawnPlan:
        """The session's spawn for a machine: the preparation, then the
        argv and the environment. The machine renders the sandbox around
        them itself, from the description. Headless, the argv is the binary
        alone: the machine's app-server client adds ``app-server``."""
        ready = await self._prepare(session_id, config)
        desc = ready.sandbox_builder.describe()
        # The binary by its bare name: the machine resolves its own pinned install.
        bare = Path(ready.codex_bin).name
        if config.interactive:
            env = await run_db(_tui_env, session_id, config.agent_name, ready.username,
                               ready.user_role, ready.codex_bin, ready.extra_env)
            tail, _ = await self._tui_argv(session_id, config, ready, desc.cwd)
            argv = [bare, *tail]
        else:
            env = await run_db(daemon_env, session_id, config.agent_name, username="",
                               user_role=ready.user_role, codex_bin=ready.codex_bin,
                               extra_env=ready.extra_env)
            argv = [bare]
        from core.sandbox.session_config_dir import agent_relative
        return SpawnPlan(
            sandbox=desc.to_dict(), argv=argv, env=env, cwd=desc.cwd,
            config_dir=config.sandbox_host_claude_dir,
            config_dir_relative=agent_relative(config.agent_name, config.sandbox_host_claude_dir),
            credential_file=ready.wrote_auth_json,
            mcp_names=frozenset(ready.mcp_names),
            mcp_server_names=tuple(mcp_server_names_from_toml(ready.mcp_toml_content)),
            local_model=bool(ready.local_endpoint),
        )

    async def _tui_argv(self, session_id: str, config: AgentConfig, ready: "_Prepared",
                        sandbox_cwd: str) -> tuple[list[str], bool]:
        """The bare TUI's argv after the binary (``resume <thread>`` with the
        flags, or the flags with the cold first prompt last) and whether the
        prompt rides it. A thread recorded with another model provider is
        not resumed (Codex would run it on that provider)."""
        flags = _tui_flags(sandbox_mode=ready.sandbox_mode, cwd=sandbox_cwd, model=config.model,
                           permission_mode=config.permission_mode, effort=ready.codex_effort)
        # The session's own profile (its text and settings, layered over the
        # home's config.toml): no other session of the home writes it.
        flags += ["-p", session_text.checked_name(session_id)]
        prompt_in_argv = False
        if (config.resume_handle and config.resume
                and await file_commit.run_in("session-probes", _resume_provider_changed,
                                             config.sandbox_host_claude_dir, config.resume_handle,
                                             bool(ready.local_endpoint), config.agent_name)):
            # From 0.168 the TUI resumes a thread on the provider its
            # rollout recorded (the chat moved between a local model and
            # OpenAI while no session ran), with no notice. A fresh terminal
            # instead, which the warmup tail reseeds from the chat's history.
            logger.warning("Codex terminal of %s: thread %s was recorded with another "
                           "model provider; starting a fresh session", config.agent_name,
                           config.resume_handle)
            config.resume = False
            if config.chat_id:
                from core.session.history_seed import mark_resume_refused
                await run_db(mark_resume_refused, config.chat_id)
        if config.resume_handle and config.resume:
            # Resume the existing rollout/thread (codex resume <id>). The
            # continuation prompt arrives via the PTY, not argv.
            argv = ["resume", config.resume_handle, *flags]
        else:
            argv = [*flags]
            # Cold first prompt as the trailing positional PROMPT → codex
            # auto-runs it once MCP warm finishes (deterministic first-turn
            # submit; the PTY type-then-Enter race is unreliable during warm).
            _first_prompt = (config.interactive_first_prompt or "").strip()
            if _first_prompt:
                argv.append(_first_prompt)
                prompt_in_argv = True
        return argv, prompt_in_argv

    def prepare_config_dir(
        self, agent_name: str, *, username: str = "", scope: str = "user",
        external_home=None, no_shell: bool = False, read_only: bool = False,
        outside_picks=None, kind_rule=None,
    ) -> Path:
        """The ``.codex`` dir: hooks.json, the hook scripts, the skills dir.
        ``no_shell`` and ``read_only`` are not written here — the Codex
        builder has no settings file for them (the permission hook floors an
        external session, the judge's sandbox is read-only by mount), which
        is today's behaviour, kept. The builder
        (``core/layers/codex/config_dir``) is reached through its module at
        call time (tests patch it there)."""
        from core.layers.codex import config_dir as _cd
        extra: dict = {}
        if external_home:
            extra["external_home"] = external_home
            extra["outside_picks"] = outside_picks
            extra["kind_rule"] = kind_rule
        return _cd.ensure_persistent_codex_dir(
            agent_name, username=username, scope=scope, **extra)

    @staticmethod
    def _tool_walls(ctx) -> dict:
        """What a session's own context takes off Codex: an outside person
        and a session with no shell (the wrap-up, the agent's own line) keep
        no shell and no apps; the web is off for a session with no web (the
        wrap-up) and for an outside person unless their managers picked it;
        ``tool_grants`` are the functions each server keeps (an outside
        session's picks and every session's limits, ``tool_limits.engine_pairs``)."""
        from core.session.external_identity import is_external_ctx
        from services.mcp import tool_limits
        external = is_external_ctx(ctx)
        no_shell = external or bool(getattr(ctx, "no_shell", False))
        return {
            "no_shell": no_shell,
            "web_off": ((external and not getattr(ctx, "outside_web", False))
                        or bool(getattr(ctx, "no_web", False))),
            "tool_grants": tool_limits.engine_pairs(ctx),
            "apps_off": no_shell,
        }

    @staticmethod
    def _write_config_toml(
        config_dir: Path, system_prompt: str, mcp_toml: str = "",
        interactive: bool = False, trusted_cwd: str = "",
        local_endpoint: str = "", local_endpoint_keyed: bool = False,
        local_provider: str = "", client_type: str = "",
        local_catalog_json: str = "", sandbox_codex_dir: str = "",
        sandbox_home_dir: str = "",
        no_shell: bool = False, web_off: bool = False,
        tool_grants: tuple = (), apps_off: bool = False,
        session_id: str = "", for_compute: bool = False,
    ) -> tuple[str, dict]:
        """Write the home's ``config.toml`` and the session's own files.

        The home serves every session of its scope, so ``config.toml`` holds
        only what they all share (the root switches, ``[memories]``, the
        lean-start and hook ``[features]``, ``[tools]``, the provider): every
        start writes the same bytes for one subscription. What is the
        session's own (its text, ``web_search``, its ``[features]``
        switches, the terminal's trust and worktree lines, its tool sections)
        goes to the session's files (``session_text``): the terminal's
        profile ``<session>.config.toml`` and ``<session>.session.json``,
        which the app-server takes as ``developerInstructions`` and the
        thread's ``config`` map. Returns ``(text, config)``: the session's
        text and its settings as that map. ``AGENTS.md`` is never written
        and is removed: Codex reads it for every session of the home.

        ``apps_off`` (an outside session) writes ``apps = false`` under
        ``[features]``: the signed-in ChatGPT account's apps connector, whose
        tools the floor refuses one by one, is not offered at all.

        ``tool_grants`` (an outside session's picks, by server key and
        function names) writes ``enabled_tools`` under each server's section
        whose pick names functions, so the model is offered only those; the
        hook floor refuses the rest by name too.

        ``web_off`` (an outside session whose managers did not pick the
        web) writes the root key ``web_search = "disabled"``, the one the
        pinned binary reads for its Responses web search tool; the hook
        floor refuses the tool's call too.


        ``local_endpoint`` (non-empty for a local ollama / openai_compatible
        subscription) wires a custom ``model_provider`` pointing at that base
        URL, so both the bare TUI and the app-server daemon (which both read
        CODEX_HOME/config.toml) dial the operator's local model. Codex's
        built-in OpenAI provider is used when this is empty.

        ``local_catalog_json`` (the per-session model catalog of
        ``local_model_catalog``, Ollama only) is written next to config.toml
        as ``models.json`` and referenced by the root ``model_catalog_json``
        key with its SANDBOX path (``sandbox_codex_dir`` — Codex reads it from
        inside bwrap, where CODEX_HOME is ``/workspace/.codex`` or
        ``/users/<u>/.codex``, not the host path ``config_dir``). A session
        without one removes a previous session's file: CODEX_HOME persists per
        (agent, user).
        """
        # The folder is there: the funnel made the store and the home's
        # builder the home, each beneath the agent's real folder.
        config_path = config_dir / "config.toml"
        catalog_path = config_dir / _CATALOG_FILE_NAME
        if not (local_endpoint and local_catalog_json and sandbox_codex_dir):
            local_catalog_json = ""
            with contextlib.suppress(OSError):
                unlink_in(config_dir, _CATALOG_FILE_NAME)

        # ``shared``: the home's config.toml. ``own``: the session's part
        # (root keys first, then its tables), never in a file another
        # session writes.
        shared: list[str] = []
        own: list[str] = []

        # Allow large repository AGENTS.md files the person keeps in a
        # project folder (Codex's default is 32KB).
        shared.append("project_doc_max_bytes = 300000")

        # Pin protection: the TUI's startup update check renders an INTERACTIVE
        # "Update now (runs `npm install -g @openai/codex`)" prompt — one Enter
        # away from breaking the version pin wherever npm is writable. Root key,
        # so it MUST stay above the first [table] header; harmless for the
        # app-server path (no TUI notices there).
        shared.append("check_for_update_on_startup = false")
        if web_off:
            own.append('web_search = "disabled"')
        # Root twin for the default_mode_request_user_input feature below: the
        # TUI otherwise prints an "Under-development features enabled" warning
        # at every session start. Only our own vetted flag is enabled, so the
        # blanket suppression doesn't hide anything an operator chose.
        shared.append("suppress_unstable_features_warning = true")

        # Local OpenAI-compatible endpoint → select our custom provider. A
        # top-level key, so it MUST be emitted before any [table] header below.
        # The provider is the process's, not a thread's (a thread keeps the
        # one its rollout recorded), so it stays in the home's file with the
        # login (auth.json): one subscription per home.
        if local_endpoint:
            shared.append('model_provider = "oto_local"')
        # The per-session model catalog that makes Codex defer its MCP tools
        # (tool_search) on a local model — root key, absolute SANDBOX path.
        if local_catalog_json:
            write_if_changed(catalog_path, local_catalog_json.encode(), mode=0o600)
            shared.append(_catalog_toml_line(
                f"{(sandbox_home_dir or sandbox_codex_dir).rstrip('/')}/{_CATALOG_FILE_NAME}"))

        # Disable Codex's optional memory subsystem (``/memories`` slash
        # command + memory generation/reuse): even if a user types
        # ``/memories`` mid-session, the toml flags prevent Codex from
        # acting on it.
        # The otodock memory system (topic files injected into the session's
        # text by our prompt builder, written via memory-mcp) is the single
        # source of memory truth across all execution layers.
        shared.append("[memories]")
        shared.append("use_memories = false")
        shared.append("generate_memories = false")

        # Lean-start: disable Codex's curated-plugins startup sync.
        # `features.plugins` (stable, on by default) makes every app-server
        # start `git ls-remote` + clone OpenAI's ~383-entry `openai/plugins`
        # repo into each CODEX_HOME, competing for CPU/disk/network exactly
        # while our MCPs spawn. An OtoDock agent's whole toolset comes from the
        # [mcp_servers.*] sections below (a headless agent never opens the
        # interactive Apps picker the sync populates), so it adds no capability
        # — pure cold-start cost. Scoped to the sessions we spawn (a
        # self-hoster's own `codex` keeps the picker). Applies to REMOTE too:
        # the satellite writes this same config.toml and the remote rewriter
        # passes non-MCP lines through unchanged.
        shared.append("[features]")
        shared.append("plugins = false")
        # Enable Codex's hook system so our PreToolUse permission_gate.py runs
        # as the command-level FLOOR (baseline restricted-tools + dangerous +
        # RBAC + path) — the SAME decide_tool_permission authority every other
        # surface uses. Interactive TUI: trusted via
        # --dangerously-bypass-hook-trust. App-server sessions: trusted per
        # thread via thread/start.config (session.py::_thread_overrides) —
        # under `never` the JSON-RPC bridge never fires, so the hook is the
        # gate there. An attended chat's gate judges every call too, and the
        # bridge takes what crosses the sandbox. Every session of the scope
        # writes the same line. The key is default-on upstream; explicit on purpose.
        # Canonical key is `hooks` (the older `codex_hooks` is deprecated).
        shared.append("hooks = true")
        own_features: list[str] = []
        # request_user_input (the AskUserQuestion analogue): expose it in the
        # DEFAULT collaboration mode for every interactive session — the bare TUI
        # with a person (native picker + question-parked turn) and an interactive
        # TASK too, whose question the PreToolUse hook draws as a card and denies
        # (the terminal's tailer writes the row) — AND the headless -p dashboard,
        # where the hook draws the question card and denies the call (the
        # item/tool/requestUserInput server request is the fallback the hook did
        # not see, answered at once: codex_approvals → ask_user_question). OFF
        # for headless autonomous runs (task/phone/meeting/trigger: nobody
        # answers → the model must not ask; the config flag is the primary
        # gate). Native to plan mode regardless of this flag. Upstream marks it
        # under-development — drop this line (+ suppress_unstable_features_warning)
        # when a Codex bump graduates it to default-on (see CODEX.md).
        if interactive or client_type == session_kind.DASHBOARD.name:
            own_features.append("default_mode_request_user_input = true")
        if no_shell:
            # External callers (a phone caller who is not a platform user) get
            # no exec_command / write_stdin at all: the shell is what could read
            # this file's [mcp_servers.*.env], the MCP children's environment,
            # token files or /proc. apply_patch (mount-enforced), view_image,
            # the Responses web_search tool and the MCP tools stay; file reads
            # go through the file-tools MCP. Claude's twin is
            # auth/path_policy.EXTERNAL_DENIED_CLI_TOOLS.
            own_features.append("shell_tool = false")
        if apps_off:
            own_features.append("apps = false")
        if own_features:
            own.append("[features]")
            own.extend(own_features)

        # Codex 0.152.0 made the update_plan tool OPT-IN (tools.update_plan,
        # default off): without it the daemon never emits turn/plan/updated
        # and the dashboard's TODO checklist silently disappears. Every
        # session keeps it (valid on older Codex too, where it was the
        # default). Twins: both satellite writers.
        shared.append(_CODEX_TOOLS_TABLE)

        if interactive and trusted_cwd:
            # Pre-trust the working directory so the interactive `codex` TUI skips
            # its "Do you trust the contents of this directory?" startup prompt on
            # every new session (config.toml [projects."<path>"].trust_level). Our
            # config + hooks live in CODEX_HOME (user-level, always loaded); this
            # only silences the CWD-trust prompt so sessions launch straight in.
            _esc_cwd = trusted_cwd.replace('\\', '\\\\').replace('"', '\\"')
            own.append(f'[projects."{_esc_cwd}"]')
            own.append('trust_level = "trusted"')

        if interactive and sandbox_codex_dir:
            # Codex 0.162's managed worktrees (the TUI's create_worktree and
            # list_worktrees tools, a trusted project) check out under
            # $CODEX_HOME/worktrees by default: the .codex dir the path policy
            # protects and whose children the sync manifest does not prune. A
            # hidden sibling at the scope root is neither protected nor synced.
            # Absolute, as Codex requires; the satellite twin writes its own
            # host path (codex_pty_session._build_codex_config_toml).
            _scope_root = sandbox_codex_dir.rstrip("/").rsplit("/", 1)[0]
            own.append("[desktop]")
            own.append(f'git-worktree-root = "{_scope_root}/.codex-worktrees"')

        if interactive:
            # Near a usage limit the TUI shows a model picker in place of its
            # composer, and a message the platform types (a continuation, a
            # queued one) would choose in it. The pool moves accounts by model
            # itself. Twin: the satellite's terminal writer.
            own.append("[notice]")
            own.append("hide_rate_limit_model_nudge = true")

        # Custom model provider for a local OpenAI-compatible endpoint
        # (Ollama, LM Studio, LiteLLM, vLLM, …). wire_api MUST be "responses":
        # codex-rs removed the "chat" wire format (deserializing it is a hard
        # serde error since ≤0.144.1 — model-provider-info/src/lib.rs), so the
        # local server must expose an OpenAI Responses-compatible endpoint.
        # ``env_key`` names the child-env variable carrying the endpoint's
        # bearer token — emitted ONLY when a key is configured: codex-rs
        # refuses to start a provider whose env_key is unset. The agent dials
        # base_url; the sandbox carves egress to that host. A local model
        # prefills at CPU speed, so the stream idle timeout (Codex's 5-minute
        # default killed a first turn before its first token) is raised for
        # every local endpoint (local_model_catalog.STREAM_IDLE_TIMEOUT_MS).
        if local_endpoint:
            _esc_ep = local_endpoint.replace("\\", "\\\\").replace('"', '\\"')
            provider_lines = [
                "[model_providers.oto_local]",
                'name = "Local"',
                f'base_url = "{_esc_ep}"',
                'wire_api = "responses"',
                f"stream_idle_timeout_ms = {_STREAM_IDLE_TIMEOUT_MS}",
            ]
            if local_endpoint_keyed:
                provider_lines.append(f'env_key = "{_LOCAL_ENDPOINT_KEY_ENV}"')
            shared.append("\n".join(provider_lines))

        # MCP sections (already formatted as TOML by mcp_registry): the
        # session's own, with its tokens and session ids.
        if mcp_toml:
            own.append((mcp_toml if interactive else with_tool_approvals(mcp_toml)).strip())

        shared_text = "\n\n".join(shared) + "\n"
        own_text = ("\n\n".join(own) + "\n") if own else ""
        own_config: dict = {}
        for label, text in (("config.toml", shared_text), ("the session's settings", own_text)):
            try:
                parsed = tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                _log_invalid_toml(label, config_path, text, e)
                raise RuntimeError(
                    f"generated codex {label} failed TOML validation: {e}"
                ) from e
            if text is own_text:
                own_config = parsed
        write_if_changed(config_path, shared_text.encode(), mode=0o600)

        if local_endpoint:
            # Tell a local model the truth about its MCP tools for its server
            # (helpers.local_provider_note: ollama vs openai_compatible).
            system_prompt = with_local_provider_note(system_prompt, local_provider)
        text = with_box_network_note(system_prompt) if system_prompt else ""
        session_text.remove_shared_text(config_dir)
        if session_id:
            session_text.write_session_files(
                # The thread's map only for a compute machine, which reads it
                # here; a paired computer gets it in its start payload.
                config_dir, session_id, text=text, config=own_config if for_compute else {},
                profile=own_text if interactive else None, writer=write_if_changed,
            )
        return text, own_config

    def _make_home(self, session_id: str, store: Path) -> None:
        """The session's home in its scope's folder on the platform, which a
        compute machine beside it shares (``session_config_dir.make_session_home``):
        the links and the agent's ``installation_id`` (written once into the
        store when it has none). The store keeps nothing the session's
        terminal would read as its project's (``drop_store_leftovers``),
        unless a session that runs from the store itself still lives (one
        started before this version, on a compute machine that outlived the
        platform's restart)."""
        import uuid

        from core.layers.codex.config_dir import drop_store_leftovers
        from core.sandbox import session_config_dir
        spec = cast("SessionHomeSpec", self.capabilities.auth.session_home)
        if not (store / "installation_id").exists():
            session_config_dir.write_no_follow(store / "installation_id", str(uuid.uuid4()).encode())
        session_config_dir.make_session_home(store, session_id, spec)
        from services.engines import pool_bindings
        if not pool_bindings.folder_bound(str(store)):
            drop_store_leftovers(store)

    @staticmethod
    def _drop_stale_auth_json(config_dir: Path) -> None:
        """A session with no sign-in file of its own (a local endpoint) must not
        load a previous session's ``auth.json`` from this persistent CODEX_HOME:
        Codex would take the stale, refresh-neutralized token and loop on
        refreshing it every request ("Failed to refresh token: 400 …
        refresh_token: empty string" — verified 2026-09-07 on a local-endpoint
        session that followed a ChatGPT one). The satellite writers do the same
        (``codex_files.write_or_drop_auth_json``)."""
        with contextlib.suppress(OSError):
            unlink_in(config_dir, "auth.json")
