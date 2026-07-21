"""Codex execution layer — CodexCLIExecutionLayer + event translator.

Wraps the persistent ``codex app-server`` JSON-RPC daemon as an ExecutionLayer
(one long-lived process per session; multi-turn continuity via the warm thread).

Event translation converts the daemon's JSON-RPC notifications (item/started /
item/completed carrying a ThreadItem, item/agentMessage/delta, turn/completed,
…) into the platform's CommonEvent stream so the ChatStreamPump processes them
identically to CLI or Direct LLM events.
"""

import json
import logging
import re
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import config as app_config
from core.events.common_events import CommonEvent, ERROR, METADATA, DONE, PLAN_MODE
from core.execution_layer import ExecutionLayer, AgentConfig, LayerCapabilities
from core.layers.codex.session import (
    create_codex_session, get_codex_session, close_codex_session,
)
from core.session.session_state import (
    _record_session_use, set_session_security, set_session_mode,
    set_session_codex_dir,
    resolve_permission,
    resolve_session_permissions,
)

logger = logging.getLogger("codex-layer")



# The event translator now lives in translator.py; re-exported here so
# `from core.layers.codex.layer import CodexEventTranslator` keeps working
# (remote_execution + the codex tests import it from this module).
from core.layers.codex.translator import CodexEventTranslator  # noqa: F401


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

_CODEX_CAPABILITIES = LayerCapabilities(
    name="codex-cli",
    display_name="OpenAI Codex",
    supports_resume=True,
    supports_permissions=True,
    supports_plan_mode=True,          # `plan` mode → read-only sandbox + codex's
                                      # `plan` collaboration mode (per-turn
                                      # settings.collaborationMode); turn-end
                                      # synthesizes the implement card (CODEX.md)
    supports_todos=True,
    supports_subagents=True,          # collabAgentToolCall → SUBAGENT_START/END (0.5)
    supports_context_compression=False,
    supports_control_commands=True,   # app-server: model/mode via per-turn override
    supports_mcps=True,
    permission_modes=["default", "acceptEdits", "plan", "dontAsk"],
    control_commands=["set_model", "set_permission_mode"],
    models=app_config.get_layer_models("codex-cli"),
    # "max" is real wire vocabulary from the GPT-5.6 family on; older models
    # clamp to xhigh in map_effort_to_codex. "ultra" (max reasoning + Codex-
    # native proactive multi-agent orchestration) is per-model: gpt-5.6
    # Sol/Terra only — the dashboard gates it on the model's supports_ultra
    # flag and map_effort_to_codex clamps it everywhere else (see helpers).
    effort_levels=["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    effort_changeable_mid_session=True,   # effort is a per-turn override now
    compression_threshold_pct=None,
    mcp_delivery="external_config",
    mcp_config_format="toml",
    providers=[
        {"id": "openai", "label": "OpenAI", "requires_key": True},
        # Local providers reach the operator's own network — unavailable on
        # hosted OtoDock (no operator LAN). Gated off at import on cloud.
        *([] if app_config.OTODOCK_CLOUD else [
            {"id": "ollama", "label": "Ollama (Local)", "requires_key": False},
            {"id": "openai_compatible", "label": "OpenAI-compatible endpoint", "requires_key": False},
        ]),
    ],
)


from core.credentials.credential_writeback import writeback_credential_dirs as _writeback_credential_dirs


_TOML_MCP_SECTION_RE = re.compile(r"^\[mcp_servers\.([a-zA-Z0-9_-]+)\]")
_TOML_ENV_LINE_RE = re.compile(r'^(\s*env\s*=\s*\{)(.*)\}(\s*)$')
_TOML_HTTP_HEADERS_RE = re.compile(r"^\[mcp_servers\.([a-zA-Z0-9_-]+)\.http_headers\]")
_TOML_AUTH_LINE_RE = re.compile(r'^(\s*)"?Authorization"?\s*=\s*"Bearer\s+[^"]*"(\s*)$')


def _inject_fetch_tokens_toml(
    toml_content: str, bundle_keys: set, session_id: str,
) -> str:
    """Append ``OTO_MCP_FETCH_TOKEN`` to the env inline-table of each
    ``[mcp_servers.<slug>]`` whose slug has a secret bundle (local Codex broker).

    Section-aware + append-only — every stdio MCP already has an ``env`` block
    (the OTO_* set is injected at config build), so the bundle MCPs always have
    one to extend. Mirrors ``remote_execution._rewrite_mcp_toml_for_remote``'s
    env branch; the token's presence then triggers the interceptor wrap.
    """
    from core.credentials import mcp_broker
    out_lines: list[str] = []
    slug: str | None = None
    for line in toml_content.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        body = line[: -len(nl)] if nl else line
        m = _TOML_MCP_SECTION_RE.match(body.strip())
        if m:
            slug = m.group(1)
            out_lines.append(line)
            continue
        em = _TOML_ENV_LINE_RE.match(body)
        if em and slug in bundle_keys:
            head, inner, trail = em.group(1), em.group(2), em.group(3)
            tok = mcp_broker.mint_token(session_id, slug)
            esc = tok.replace("\\", "\\\\").replace('"', '\\"')
            sep = "" if inner.strip() == "" else ", "
            out_lines.append(
                f'{head}{inner.rstrip()}{sep}"OTO_MCP_FETCH_TOKEN" = "{esc}" }}{trail}{nl}'
            )
            continue
        out_lines.append(line)
    return "".join(out_lines)


def _inject_real_bearers_toml(toml_content: str, bundles: dict) -> str:
    """Swap the sentinel ``Authorization`` bearer in each proxy-terminable HTTP
    MCP's ``http_headers`` sub-table for the REAL token from its bundle (local
    Codex).

    Local Codex has no tunnel ``_dispatch`` to swap at, so — on the TRUSTED proxy
    host — the real bearer lands inline in this per-session ``config.toml``
    (agent-read-denied; never the shared sessions/ file). Only fires for slugs
    whose bundle carries an ``http_bearer`` (github/m365); vendor HTTP MCPs (real
    bearer already inline) and stdio MCPs are untouched.
    """
    out_lines: list[str] = []
    slug: str | None = None
    for line in toml_content.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        body = line[: -len(nl)] if nl else line
        stripped = body.strip()
        m = _TOML_MCP_SECTION_RE.match(stripped) or _TOML_HTTP_HEADERS_RE.match(stripped)
        if m:
            slug = m.group(1)
            out_lines.append(line)
            continue
        am = _TOML_AUTH_LINE_RE.match(body)
        if am and slug is not None:
            bearer = getattr(bundles.get(slug), "http_bearer", None)
            if bearer:
                esc = bearer.replace("\\", "\\\\").replace('"', '\\"')
                out_lines.append(
                    f'{am.group(1)}"Authorization" = "Bearer {esc}"{am.group(2)}{nl}'
                )
                continue
        out_lines.append(line)
    return "".join(out_lines)


def _inject_session_jwt_toml(
    toml_content: str, session_id: str, agent_name: str
) -> str:
    """Swap the session-JWT sentinel ``Authorization`` bearer for a real,
    session-scoped JWT in each Docker MCP's ``http_headers`` sub-table.

    The sentinel (``Bearer OTO_SESSION_JWT``) is set at config-build time for
    MCPs declaring ``server.proxy_callbacks`` (today only file-tools), which
    forward this bearer on their proxy callbacks. This is Codex's analog of the
    CLI ``_swap_session_jwt`` swap. The sentinel string is unique, so a direct
    value replace is safe — real vendor bearers (``_inject_real_bearers_toml``)
    are untouched.
    """
    from auth.session_token import (
        SESSION_JWT_SENTINEL_BEARER, swap_session_jwt_bearer,
    )
    if SESSION_JWT_SENTINEL_BEARER not in toml_content:
        return toml_content
    new_bearer = swap_session_jwt_bearer(
        SESSION_JWT_SENTINEL_BEARER, session_id, agent_name,
    )
    if not new_bearer:
        return toml_content
    esc = new_bearer.replace("\\", "\\\\").replace('"', '\\"')
    return toml_content.replace(
        f'"{SESSION_JWT_SENTINEL_BEARER}"', f'"{esc}"',
    )


# ---------------------------------------------------------------------------
# CodexCLIExecutionLayer
# ---------------------------------------------------------------------------

class CodexCLIExecutionLayer(ExecutionLayer):
    """ExecutionLayer driving the Codex CLI's ``codex app-server`` daemon."""

    @property
    def capabilities(self) -> LayerCapabilities:
        return _CODEX_CAPABILITIES

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_session(self, session_id: str, config: AgentConfig) -> None:
        # Fail CLOSED: a local agent MUST run sandboxed + network-isolated. The
        # codex layer reads sandbox_host_claude_dir AS CODEX_HOME; empty means a
        # config-builder omission — refuse rather than launch un-sandboxed.
        if not config.sandbox_host_claude_dir:
            raise RuntimeError(
                f"refusing to start a local Codex session for agent "
                f"'{config.agent_name}' without a sandbox dir — local agents "
                f"must run sandboxed + network-isolated."
            )
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
        if local_endpoint:
            # A local endpoint on the proxy host itself (T1) is reached via the
            # loopback splice — rewrite its host to 127.0.0.1 so the Codex CLI
            # (which dials it from the sandbox) hits the forwarded port. No-op in
            # T2 / for remote endpoints.
            from services.mcp import mcp_registry as _mr
            local_endpoint = _mr.loopback_if_host_self(local_endpoint)
        sandbox_cmd_prefix: list[str] = []

        # Map permission mode → Codex sandbox mode
        sandbox_mode = _permission_to_sandbox(config.permission_mode)

        # Determine working directory from the MOUNT scope — Shared-only is
        # agent-scope even with a human present, so it works in /workspace, not
        # /users/{u}. ``mount_username`` is "" for any agent-scope mount.
        agent_dir = app_config.get_agent_dir(config.agent_name)
        _ctx = config.security_context
        username = _ctx.mount_username if _ctx else ""
        if username:
            work_dir = str(agent_dir / "users" / username)
        else:
            work_dir = str(agent_dir / "workspace")

        # Sandbox setup
        from core.sandbox.sandbox import resolve_sandbox_config, SandboxBuilder
        from services.mcp import mcp_registry
        import os as _os

        # is_remote=False (fail-closed default, explicit here): LOCAL bwrap mount
        # path — satellite_only device MCPs run native on the satellite and must
        # never be mounted locally.
        mcp_mounts = []
        for manifest in (mcp_registry.get_agent_mcps(config.agent_name, is_remote=False) or []):
            mcp_mounts.extend(manifest.sandbox_mounts)

        # Mount the Codex CLI installation directory inside the sandbox
        # (it lives in ~/.npm-global/ which isn't in the default system mounts).
        # Shared helper — the CLI layer mounts CLAUDE_BIN the same way.
        from core.sandbox.sandbox import cli_install_ro_binds
        codex_bin = getattr(app_config, "CODEX_BIN", "codex")
        codex_extra_mounts = cli_install_ro_binds(codex_bin)

        sandbox_cfg = resolve_sandbox_config(
            role=config.security_context.role if config.security_context else "viewer",
            username=username,   # already the MOUNT username (see work_dir above)
            agent_name=config.agent_name,
            is_admin_agent=config.security_context.is_admin_agent if config.security_context else False,
            host_claude_dir=Path(config.sandbox_host_claude_dir),
            user_sub=config.user_sub,
            mcp_sandbox_mounts=mcp_mounts,
            extra_ro_binds=codex_extra_mounts,
            # Carve sandbox egress to a local OpenAI-compatible LLM endpoint the
            # Codex CLI dials directly (Ollama / openai_compatible). Empty for
            # OpenAI/ChatGPT subscriptions.
            extra_egress_targets=[local_endpoint] if local_endpoint else None,
            config_visible=_ctx.config_visible if _ctx else None,
            mount_shared=_ctx.mount_shared if _ctx else True,
            # TOML build file with host mcps/ paths — scanned (plain-text)
            # for the per-MCP dir binds.
            mcp_config_path=config.mcp_config_path,
        )
        sandbox_builder = SandboxBuilder(sandbox_cfg)

        sandbox_cmd_prefix = sandbox_builder.build_command_prefix([])
        env_overrides = sandbox_builder.get_env_overrides(
            config_dir_name=".codex", config_env_var="CODEX_HOME",
        )
        extra_env.update(env_overrides)

        # ssh-hosts (context-only MCP): provision the agent's authorized SSH
        # keys into <.codex>/ssh so the prompt's `ssh -i "$OTO_SSH_KEY_DIR/…"`
        # lines work from the Codex shell (mirrors the CLI layer).
        from core.sandbox.session_config_dir import (
            materialize_ssh_keys_for_sandbox,
        )
        if materialize_ssh_keys_for_sandbox(
            config.agent_name, config.sandbox_host_claude_dir,
        ):
            _codex_home = env_overrides.get("CODEX_HOME", "/workspace/.codex")
            extra_env["OTO_SSH_KEY_DIR"] = f"{_codex_home}/ssh"

        # Read MCP TOML content (generated by build_session_mcp_config with format="toml")
        mcp_toml_content = ""
        if mcp_path and mcp_path.exists():
            mcp_toml_content = mcp_path.read_text()

        # Inject session_id into HTTP MCP URLs (same purpose as CLI's
        # _inject_session_id_into_sse — Docker MCPs need it for path resolution)
        if mcp_toml_content:
            mcp_toml_content = re.sub(
                r'(url\s*=\s*"http[^"]+)',
                rf'\1?session_id={session_id}',
                mcp_toml_content,
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
        # here or TOML parse fails with "duplicate key".
        if mcp_toml_content:
            from auth.session_token import create_session_token
            proxy_vars = {
                "PROXY_URL": f"http://localhost:{app_config.PORT}",
                "PROXY_API_KEY": create_session_token(session_id, config.agent_name),
            }
            for key, val in proxy_vars.items():
                escaped_key = key.replace('"', '\\"')
                escaped_val = val.replace('\\', '\\\\').replace('"', '\\"')
                needle = f'"{key}"'
                # Append to each env = { ... } block, but SKIP a block that already
                # declares this key. ``config_builder.inject_credential_env_into_toml``
                # already injects PROXY_API_KEY (with the user_sub), so re-appending
                # it produced a DUPLICATE TOML key — invalid TOML. The Codex
                # app-server's parser tolerated it (last-wins) but the strict
                # interactive ``codex`` TUI parser rejects it ("duplicate key" →
                # exit 1 → blank terminal). Idempotent both ways: inject if absent,
                # skip if present.
                def _inject(m, _needle=needle, _ek=escaped_key, _ev=escaped_val):
                    if _needle in m.group(2):
                        return m.group(0)
                    return f'{m.group(1)}{m.group(2)}, "{_ek}" = "{_ev}" }}'
                mcp_toml_content = re.sub(
                    r'(env\s*=\s*\{)([^}]*)\}', _inject, mcp_toml_content,
                )

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
                # Swap the sentinel bearer for the real token on
                # proxy-terminable github/m365 HTTP MCPs (local — no tunnel
                # _dispatch — so it lands inline in this trusted, agent-read-denied
                # config.toml). No-op when no HTTP MCP has a bundle bearer.
                mcp_toml_content = _inject_real_bearers_toml(
                    mcp_toml_content, config.mcp_secret_bundles or {},
                )
            from core.sandbox.interceptor_wrap import wrap_toml_text
            sandbox_codex_dir = env_overrides.get("CODEX_HOME", "/workspace/.codex")
            mcp_toml_content = wrap_toml_text(
                mcp_toml_content, interpreter="python3",
                interceptor_path=f"{sandbox_codex_dir}/stdio_path_interceptor.py",
            )

        # Write combined config.toml (instructions + MCP servers). interactive=
        # the bare-TUI path → adds `[features] hooks = true` so our permission
        # gate runs (the app-server path leaves it off; see _write_config_toml).
        config_dir = Path(config.sandbox_host_claude_dir)
        self._write_config_toml(
            config_dir, config.system_prompt, mcp_toml_content,
            interactive=config.interactive,
            trusted_cwd=(sandbox_builder.get_cwd() if config.interactive else ""),
            local_endpoint=local_endpoint,
            client_type=config.client_type,
        )

        # Write OAuth auth.json if token provided
        wrote_auth_json = bool(extra_env.get("_CODEX_OAUTH_TOKEN"))
        if wrote_auth_json:
            config_dir = Path(config.sandbox_host_claude_dir)
            auth_blob_json = extra_env.pop("_CODEX_AUTH_BLOB", "")
            auth_blob = json.loads(auth_blob_json) if auth_blob_json else None
            self._write_auth_json(
                config_dir,
                extra_env.pop("_CODEX_OAUTH_TOKEN"),
                auth_blob=auth_blob,
            )

        # Create session (thread_id from DB enables resume after proxy restart)
        codex_effort = _map_effort_to_codex(config.effort, config.model)
        _user_role = (
            config.security_context.role if config.security_context else ""
        )

        # Credential broker: populate the per-session secret store so
        # the wrapped stdio MCPs can fetch at spawn (must precede the daemon).
        from core.credentials import mcp_broker
        mcp_broker.provision(session_id, config.mcp_secret_bundles or {})

        # Interactive mode: spawn the bare `codex` Ratatui TUI under a PTY
        # and register it with core.session.interactive_session — NOT the app-server
        # daemon. Mirrors the Claude interactive branch (cli/layer.py). The whole
        # prelude above (sandbox prefix, .codex config.toml + auth.json, MCP TOML,
        # credential broker) is shared; only the spawned process differs.
        if config.interactive:
            from core.session import interactive_session
            from core.sandbox.env_builder import build_session_env
            from core.layers.codex.codex_approvals import approval_for_sandbox
            # Daemon-equivalent env for the bare TUI (mirror _build_env) + TERM +
            # OTO_INTERACTIVE (the PostToolUse forwarder no-ops; decide_tool_permission
            # returns "ask" for the ask-tier — the deny-only gate stays silent on it,
            # so Codex's own -a on-request prompts).
            proc_env = build_session_env(
                session_id, config.agent_name,
                username=username, user_role=_user_role,
            )
            _codex_dir_path = _os.path.dirname(_os.path.realpath(codex_bin))
            if _codex_dir_path and _codex_dir_path not in proc_env.get("PATH", ""):
                proc_env["PATH"] = _codex_dir_path + ":" + proc_env.get("PATH", "/usr/bin:/bin")
            proc_env.update(extra_env)  # CODEX_HOME + subscription + credential + proxy vars
            proc_env["TERM"] = proc_env.get("TERM") or "xterm-256color"
            # xterm.js renders 24-bit; without the hint the TUI downgrades to
            # 256-color SGR (see the Claude twin in cli/session.py).
            proc_env.setdefault("COLORTERM", "truecolor")
            proc_env["OTO_INTERACTIVE"] = "1"
            # Codex's PreToolUse hook rejects permissionDecision:"allow" — tell
            # permission_gate.py to emit JSON only to DENY (allow/defer → silent
            # exit 0 → Codex proceeds). The hard-deny FLOOR still enforces.
            proc_env["OTO_HOOK_DENY_ONLY"] = "1"
            sandbox_cwd = sandbox_builder.get_cwd()
            approval = approval_for_sandbox(sandbox_mode)
            # Flags precede the optional PROMPT (codex [opts] [PROMPT]).
            # --no-alt-screen = inline mode (matches Claude's inline model + keeps
            # xterm scrollback); the command-level FLOOR runs via .codex/hooks.json
            # (config has hooks=true) with --dangerously-bypass-hook-trust (the
            # platform vets the hook source). Sandbox/approval are derived from the
            # permission mode (same mapping as the app-server).
            flags = [
                "--no-alt-screen",
                "-s", sandbox_mode,
                "-a", approval,
                "--dangerously-bypass-hook-trust",
                "-C", sandbox_cwd,
            ]
            if config.model:
                flags += ["-m", config.model]
            if codex_effort:
                flags += ["-c", f'model_reasoning_effort="{codex_effort}"']
            _prompt_in_argv = False
            if config.codex_thread_id and config.resume:
                # Resume the existing rollout/thread (codex resume <id>). The
                # continuation prompt arrives via the PTY, not argv.
                argv = [*sandbox_cmd_prefix, codex_bin, "resume", config.codex_thread_id, *flags]
            else:
                argv = [*sandbox_cmd_prefix, codex_bin, *flags]
                # Cold first prompt as the trailing positional PROMPT → codex
                # auto-runs it once MCP warm finishes (deterministic first-turn
                # submit; the PTY type-then-Enter race is unreliable during warm).
                _first_prompt = (config.interactive_first_prompt or "").strip()
                if _first_prompt:
                    argv.append(_first_prompt)
                    _prompt_in_argv = True
            # Proxy-side identity BEFORE register() so the PreToolUse hook resolves
            # the moment the TUI starts. The app-server path sets
            # this at the BOTTOM of start_session, which this interactive branch
            # returns before reaching — without it, get_session_security() is None
            # and every tool call fail-closes with "Session is no longer active".
            set_session_mode(session_id, config.permission_mode)
            if config.security_context:
                set_session_security(session_id, config.security_context)
            _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)
            # Record the host CODEX_HOME (= the .codex config dir) + sandbox CWD so the
            # rollout tailer can locate THIS session's rollout JSONL (CODEX_HOME is
            # per-(user, agent) scope, shared across that scope's chats).
            set_session_codex_dir(session_id, config.sandbox_host_claude_dir, sandbox_cwd)
            await interactive_session.register(
                session_id=session_id,
                chat_id=config.chat_id,
                agent_name=config.agent_name,
                argv=argv,
                env=proc_env,
                cwd=(None if sandbox_cmd_prefix else (work_dir or None)),
                user_sub=config.user_sub,
                role=_user_role,
                username=username,
                target=config.execution_target or "local",
                tui_theme=config.interactive_theme or "dark",
                # Persist turns from the Codex rollout JSONL (not the Claude transcript).
                transcript_kind="codex",
                # Fresh codex delivers the prompt via argv (auto-run) → no cold
                # prompt to gate: start READY so viewer xterm bytes pass through live
                # instead of being buffered + flushed late into the composer.
                prompt_in_argv=_prompt_in_argv,
            )
            # Bind the subscription for pool cleanup — this branch returns
            # before the common tail below, and without the binding the seat
            # acquired at config build is never released (active_sessions
            # drifts up until restart). Interactive teardown releases it in
            # InteractiveSession.close().
            if config.subscription_id:
                from services.engines.subscription_pool import (
                    bind_session, credential_scope_key,
                )
                bind_session(
                    session_id, config.subscription_id,
                    layer="codex-cli", user_sub=config.subscription_user_sub,
                    scope_key=credential_scope_key(
                        config.execution_target or "local",
                        config.sandbox_host_claude_dir,
                    ),
                )
                if wrote_auth_json:
                    self._register_fanout_target(session_id, config)
            return

        await create_codex_session(
            session_id=session_id,
            agent_name=config.agent_name,
            model=config.model,
            sandbox_mode=sandbox_mode,
            working_dir="",
            config_dir=config.sandbox_host_claude_dir,
            extra_env=extra_env,
            sandbox_cmd_prefix=sandbox_cmd_prefix,
            effort=codex_effort,
            thread_id=config.codex_thread_id or None,
            user_role=_user_role,
        )

        # Store session metadata (same pattern as CLI layer)
        set_session_mode(session_id, config.permission_mode)
        if config.security_context:
            set_session_security(session_id, config.security_context)
        _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)

        # Bind subscription for cleanup on close
        if config.subscription_id:
            from services.engines.subscription_pool import (
                bind_session, credential_scope_key,
            )
            bind_session(
                session_id, config.subscription_id,
                layer="codex-cli", user_sub=config.subscription_user_sub,
                scope_key=credential_scope_key(
                    config.execution_target or "local",
                    config.sandbox_host_claude_dir,
                ),
            )
            if wrote_auth_json:
                self._register_fanout_target(session_id, config)

    @staticmethod
    def _register_fanout_target(session_id: str, config: AgentConfig) -> None:
        """Register this session's auth.json for rotation fan-out — only
        sessions that actually wrote one (API-key sessions have nothing to
        rewrite)."""
        from services.engines import token_fanout
        token_fanout.register_session_target(
            session_id,
            token_fanout.CredentialFileTarget(
                kind="codex",
                host_dir=config.sandbox_host_claude_dir,
            ),
        )

    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        import time as _time

        session = await get_codex_session(session_id)
        if not session:
            yield CommonEvent(type=ERROR, data={
                "message": "Codex session not found or closed",
            })
            return

        # The translator persists on the session across turns (held by the
        # session) so codex_thread_id is emitted once and token state survives.
        # supervised_bg=True: the local session runs a per-thread bg supervisor,
        # so background sub-agents are NOT swept at turn end (the supervisor emits
        # their SUBAGENT_END on real completion). The remote path leaves this off.
        if not session.translator:
            session.translator = CodexEventTranslator(
                model=session.model, supervised_bg=True,
            )
        translator = session.translator
        # Seed the multi-agent demux with the AUTHORITATIVE main thread id (the
        # started/resumed thread) instead of letting the translator guess it from
        # the first ``turn/started``. A missed/late main ``turn/started`` would
        # otherwise let the first spawned sub-agent's thread hijack
        # ``_main_thread_id`` → the MAIN agent's own messages get suppressed and a
        # sub-agent's ``turn/completed`` ends the turn. Idempotent (the thread id
        # is stable across turns); the session loop gates turn-end on the same id.
        if session.thread_id:
            translator._main_thread_id = session.thread_id
        inject_time = kwargs.get("inject_time", False)
        turn_start = _time.monotonic()

        # Plan mode: codex delivers the plan as the turn's final agentMessage (no
        # ExitPlanMode / structured Plan item on the -p path — probe-verified), so
        # we SYNTHESIZE the implement card. Capture the last agentMessage + whether
        # the turn was interrupted; emit a `plan_mode exit` (the SAME event the
        # Claude ExitPlanMode path uses → same pump persistence + PlanView card)
        # right before DONE, but only for a normally-completed plan turn with a
        # non-empty plan. A held request_user_input can't leave a question pending
        # at turn-end (codex resumes the turn only after it's answered).
        in_plan = getattr(session, "sandbox_mode", "") == "read-only"
        final_plan_msg = ""
        turn_interrupted = False

        # Emit codex_thread_id once → pump persists to chats.codex_thread_id so
        # the thread resumes after a proxy restart (captured from thread/start).
        for ev in translator.thread_id_metadata(session.thread_id or ""):
            yield ev

        async for codex_event in session.send_message(
            message, inject_time=inject_time,
        ):
            if in_plan and codex_event.type == "item/completed":
                _item = (codex_event.data or {}).get("item") or {}
                if _item.get("type") == "agentMessage" and _item.get("text"):
                    final_plan_msg = _item["text"]          # last one wins = the plan
            elif in_plan and codex_event.type == "turn/completed":
                if ((codex_event.data or {}).get("turn") or {}).get("status") == "interrupted":
                    turn_interrupted = True
            for common_event in translator.translate(codex_event):
                # Inject wall-clock duration into METADATA (Codex binary
                # doesn't provide it — we measure at the proxy level).
                if common_event.type == METADATA and "cost_usd" in common_event.data:
                    duration_ms = int((_time.monotonic() - turn_start) * 1000)
                    common_event.data["duration_ms"] = duration_ms
                if (common_event.type == DONE and in_plan and not turn_interrupted
                        and final_plan_msg.strip()):
                    # `synthetic` distinguishes this from Claude's ExitPlanMode
                    # plan_mode-exit (a no-op in the FE, since Claude's actionable
                    # card is the held plan_review) → the FE renders the implement
                    # card ONLY for the codex synthetic exit.
                    yield CommonEvent(type=PLAN_MODE, data={
                        "action": "exit", "synthetic": True,
                        "tool_input": {"plan": final_plan_msg},
                    })
                yield common_event

    async def close_session(self, session_id: str) -> None:
        await _writeback_credential_dirs(session_id)
        await close_codex_session(session_id)
        # Credential broker: drop this session's secrets (a cap token replayed
        # after close then finds nothing).
        from core.credentials import mcp_broker
        mcp_broker.purge_session(session_id)
        from services.engines.subscription_pool import release_subscription
        release_subscription(session_id)
        from core.concurrency import release_chat_slot
        release_chat_slot(session_id)

    async def abort(self, session_id: str) -> bool:
        # The app-server daemon, its warm MCP subprocesses and the stdio
        # interceptor all SURVIVE turn/interrupt — so unlike the old per-turn
        # model nothing incidentally drops a pending permission wait. Release
        # them explicitly (deny) before interrupting.
        resolve_session_permissions(session_id, approved=False)
        session = await get_codex_session(session_id)
        if not session:
            return False
        # turn/interrupt aborts the in-flight turn; the daemon stays warm and
        # owns its own rollout integrity — no trim hack, no thread-id clear
        # (the upstream openai/codex#12382 resume-hang is moot under app-server).
        # GRACEFUL: the stream closes with turn/completed status="interrupted"
        # (verified live, 0.142.5) and the rollout keeps the partial turn —
        # the caller leaves the producer running and skips the injection.
        await session.abort()
        return session.is_alive

    async def steer(self, session_id: str, text: str) -> bool:
        """turn/steer into the live turn — see CodexAppServerSession.steer."""
        session = await get_codex_session(session_id)
        if not session:
            return False
        return await session.steer(text)

    async def compact(self, session_id: str) -> dict | None:
        """thread/compact/start between turns — see CodexAppServerSession.compact."""
        session = await get_codex_session(session_id)
        if not session:
            return None
        return await session.compact()

    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        resolve_permission(request_id, approved)

    async def change_model(self, session_id: str, model: str) -> None:
        session = await get_codex_session(session_id)
        if session:
            session.set_model(model)  # applied as a per-turn override next turn/start

    async def change_mode(self, session_id: str, mode: str) -> None:
        set_session_mode(session_id, mode)
        session = await get_codex_session(session_id)
        if session:
            session.set_sandbox_mode(_permission_to_sandbox(mode))

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        # The persistent daemon applies model/mode as per-turn overrides on the
        # next turn/start (no respawn needed — unlike the old exec model).
        if subtype == "set_model" and kwargs.get("model"):
            await self.change_model(session_id, kwargs["model"])
            return {"ok": True}
        if subtype == "set_permission_mode" and kwargs.get("mode"):
            await self.change_mode(session_id, kwargs["mode"])
            return {"ok": True}
        return {"error": f"Codex: unsupported control command {subtype!r}"}

    async def get_session(self, session_id: str):
        return await get_codex_session(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        session = await get_codex_session(session_id)
        return session is not None and session.is_alive

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        session = await get_codex_session(session_id)
        if not session:
            yield
            return
        async with session.lock:
            yield

    async def is_session_process_dead(self, session_id: str) -> bool:
        # The app-server daemon is long-lived now — a dead daemon is a real
        # death (triggers re-warm + thread/resume on the next send_message).
        session = await get_codex_session(session_id)
        return session is None or not session.is_alive

    async def prepare_resume(self, session_id: str) -> None:
        pass  # Resume handled by thread_id in send_message

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> bool:
        """Codex resumability is THREAD-backed, not pool-backed.

        The thread + its rollout persist on disk independently of the
        in-memory app-server session, so a proxy restart / idle reap is NOT
        context loss — treating it as one made the warmup stamp a false
        ``resume_failed`` (digest reseed + "fresh session" card) on a chat
        whose ``thread/resume`` then succeeded anyway. Live pool session
        first; otherwise resolve the chat's ``codex_thread_id`` and check its
        rollout under the scope's .codex dir (the same restart fallback the
        CLI layer runs against its session JSONL).
        """
        session = await get_codex_session(session_id)
        if session is not None and session.thread_id is not None:
            return True

        from storage.database import get_chat_by_session
        chat = get_chat_by_session(session_id)
        thread_id = (chat or {}).get("codex_thread_id") or ""
        if not thread_id:
            return False

        from core.session import codex_rollout_tailer
        from core.session.session_state import get_session_codex_dir
        rec = get_session_codex_dir(session_id)
        codex_dir = (rec or {}).get("home", "")
        if not codex_dir and agent_name:
            agent_dir = app_config.get_agent_dir(agent_name)
            candidate = (agent_dir / "users" / username / ".codex") if username \
                else (agent_dir / "workspace" / ".codex")
            if candidate.is_dir():
                codex_dir = str(candidate)
        return bool(codex_dir) and codex_rollout_tailer.rollout_exists(
            codex_dir, thread_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_config_toml(
        config_dir: Path, system_prompt: str, mcp_toml: str = "",
        interactive: bool = False, trusted_cwd: str = "",
        local_endpoint: str = "", client_type: str = "",
    ) -> None:
        """Write config.toml and AGENTS.md for a Codex session.

        Codex's config.toml `instructions` field is reserved but NOT read
        by any code path in exec mode. Instead, the system prompt is
        delivered via AGENTS.md (Codex's recommended mechanism — loaded as
        a user-role message and read each turn).

        config.toml contains: project_doc_max_bytes (to allow large prompts)
        + [mcp_servers.*] sections. Rewrites both files each session start.

        ``local_endpoint`` (non-empty for a local ollama / openai_compatible
        subscription) wires a custom ``model_provider`` pointing at that base
        URL, so both the bare TUI and the app-server daemon (which both read
        CODEX_HOME/config.toml) dial the operator's local model. Codex's
        built-in OpenAI provider is used when this is empty.
        """
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.toml"

        parts: list[str] = []

        # Allow large system prompts via AGENTS.md (default limit is 32KB)
        parts.append("project_doc_max_bytes = 300000")

        # Pin protection: the TUI's startup update check renders an INTERACTIVE
        # "Update now (runs `npm install -g @openai/codex`)" prompt — one Enter
        # away from breaking the version pin wherever npm is writable. Root key,
        # so it MUST stay above the first [table] header; harmless for the
        # app-server path (no TUI notices there).
        parts.append("check_for_update_on_startup = false")
        # Root twin for the default_mode_request_user_input feature below: the
        # TUI otherwise prints an "Under-development features enabled" warning
        # at every session start. Only our own vetted flag is enabled, so the
        # blanket suppression doesn't hide anything an operator chose.
        parts.append("suppress_unstable_features_warning = true")

        # Local OpenAI-compatible endpoint → select our custom provider. This is
        # a top-level key, so it MUST be emitted before any [table] header below.
        if local_endpoint:
            parts.append('model_provider = "oto_local"')

        # Disable Codex's optional memory subsystem (``/memories`` slash
        # command + memory generation/reuse). Defense-in-depth alongside
        # the ``.codex/memories/`` directory wipe in
        # ``close_codex_session`` — even if a user types ``/memories``
        # mid-session, the toml flags prevent Codex from acting on it.
        # The otodock memory system (topic files injected into AGENTS.md
        # by our prompt builder, written via memory-mcp) is the single
        # source of memory truth across all execution layers.
        parts.append("[memories]")
        parts.append("use_memories = false")
        parts.append("generate_memories = false")

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
        parts.append("[features]")
        parts.append("plugins = false")
        # request_user_input (the AskUserQuestion analogue): expose it in the
        # DEFAULT collaboration mode for interactive-USER sessions — the bare TUI
        # (native picker + question-parked turn) AND the headless -p dashboard,
        # which now HOLDS the item/tool/requestUserInput server-request and
        # surfaces a dashboard question card (codex_approvals →
        # ask_user_question). OFF for autonomous runs (task/phone/meeting/trigger:
        # nobody answers → the model must not ask; the config flag is the primary
        # gate). Native to plan mode regardless of this flag. Upstream marks it
        # under-development — drop this line (+ suppress_unstable_features_warning)
        # when a Codex bump graduates it to default-on (see CODEX.md).
        if interactive or client_type == "dashboard":
            parts.append("default_mode_request_user_input = true")
        if interactive:
            # Enable Codex's hook system so our PreToolUse permission_gate.py runs
            # as the command-level FLOOR (baseline restricted-tools + dangerous +
            # RBAC + path) — the SAME decide_tool_permission authority every other
            # surface uses. ONLY for interactive: the app-server gates via its
            # JSON-RPC approval bridge, so enabling both would double-gate.
            # Canonical key is `hooks` (the older `codex_hooks` is deprecated).
            parts.append("hooks = true")

        if interactive and trusted_cwd:
            # Pre-trust the working directory so the interactive `codex` TUI skips
            # its "Do you trust the contents of this directory?" startup prompt on
            # every new session (config.toml [projects."<path>"].trust_level). Our
            # config + hooks live in CODEX_HOME (user-level, always loaded); this
            # only silences the CWD-trust prompt so sessions launch straight in.
            _esc_cwd = trusted_cwd.replace('\\', '\\\\').replace('"', '\\"')
            parts.append(f'[projects."{_esc_cwd}"]')
            parts.append('trust_level = "trusted"')

        # Custom model provider for a local OpenAI-compatible endpoint
        # (Ollama, LM Studio, LiteLLM, vLLM, …). wire_api="chat" = the
        # OpenAI /chat/completions wire format every such server speaks; no
        # env_key (these servers are keyless). The agent dials base_url; the
        # sandbox carves egress to that host.
        if local_endpoint:
            _esc_ep = local_endpoint.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(
                "[model_providers.oto_local]\n"
                'name = "Local"\n'
                f'base_url = "{_esc_ep}"\n'
                'wire_api = "chat"'
            )

        # MCP server sections (already formatted as TOML by mcp_registry)
        if mcp_toml:
            parts.append(mcp_toml.strip())

        config_text = "\n\n".join(parts) + "\n"
        # Validation gate: never hand Codex invalid TOML. The strict TUI
        # hard-exits (code 1, blank terminal) and the app-server silently
        # "uses defaults" (drops every MCP) — both are far worse failure
        # modes than a loud spawn error here. Log line/col + the offending
        # line with quoted values REDACTED (env blocks carry tokens).
        try:
            tomllib.loads(config_text)
        except tomllib.TOMLDecodeError as e:
            lineno = getattr(e, "lineno", None)
            bad = ""
            if isinstance(lineno, int) and lineno >= 1:
                lines = config_text.splitlines()
                if lineno <= len(lines):
                    bad = re.sub(r'"[^"]*"', '"▮"', lines[lineno - 1])[:200]
            logger.error(
                "generated codex config.toml is INVALID (%s): %s | line: %s",
                config_path, e, bad or "<unavailable>",
            )
            raise RuntimeError(
                f"generated codex config.toml failed TOML validation: {e}"
            )
        config_path.write_text(config_text)
        # config.toml can carry an inline MCP bearer (see the module header) —
        # lock it owner-only, same as auth.json in this dir.
        config_path.chmod(0o600)

        # Write system prompt as AGENTS.md — Codex's official mechanism
        # for injecting project-level instructions. Placed in CODEX_HOME
        # so it's read as the global AGENTS.md. Rewritten every session
        # start to stay up to date (survives resume/restart).
        if system_prompt:
            (config_dir / "AGENTS.md").write_text(system_prompt)

    @staticmethod
    def _write_auth_json(
        config_dir: Path,
        token: str,
        *,
        auth_blob: dict | None = None,
    ) -> None:
        """Write ChatGPT OAuth token to auth.json in .codex/ dir (same writer
        the rotation fan-out uses, so spawn and fan-out can't drift)."""
        from core.layers.codex.helpers import build_auth_json
        from services.engines.token_fanout import write_codex_auth_file
        write_codex_auth_file(config_dir, build_auth_json(token, auth_blob=auth_blob))


# Permission mode → Codex sandbox mode and effort mappings live in helpers.py
# so both the local layer and RemoteExecutionLayer use the exact same logic.
from core.layers.codex.helpers import (
    permission_to_sandbox as _permission_to_sandbox,
    map_effort_to_codex as _map_effort_to_codex,
)
