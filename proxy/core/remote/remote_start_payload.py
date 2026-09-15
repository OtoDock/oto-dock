"""The ``start_session`` payload for a satellite (mixin).

``_build_start_payload`` assembles what the satellite needs to spawn the
CLI or Codex process: the session env with the tunnel identity, the
relative config dirs, the bundled hook scripts, the effort and sandbox
mapping, the local-model provider, the OAuth files and the MCP config
rewritten for the remote host (``remote_mcp_rewrite``). Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import json
import logging

from core.execution_layer import AgentConfig
from core.remote.remote_mcp_rewrite import (
    _rewrite_mcp_json_for_remote,
    _rewrite_mcp_toml_for_remote,
)

logger = logging.getLogger("remote-layer")


# --- Hook script cache ---------------------------------------------------
# Hook scripts are bundled into every `start_session` payload so the
# satellite never needs them pre-deployed. Cached at module scope — they
# change only when the proxy is redeployed.

_HOOK_SCRIPTS_CACHE: dict[str, str] | None = None


def _load_hook_scripts() -> dict[str, str]:
    """Return {filename: contents} for the hook scripts the CLI runs.

    These are the same scripts the local sandbox installs; on remote they're
    written into the satellite's per-session .claude/ dir before CLI spawn.
    """
    global _HOOK_SCRIPTS_CACHE
    if _HOOK_SCRIPTS_CACHE is not None:
        return _HOOK_SCRIPTS_CACHE
    from pathlib import Path
    import config as app_config
    # Canonical proxy hooks dir (config.HOOKS_DIR == BASE_DIR/"hooks"). Do NOT
    # recompute from __file__ here — this module lives at core/remote/, so a
    # naive parent-count silently points at the wrong directory.
    hooks_dir = Path(app_config.HOOKS_DIR)
    scripts: dict[str, str] = {}
    for name in ("permission_gate.py", "tool_result_forwarder.py",
                 "subagent_tracker.py"):
        path = hooks_dir / name
        if path.exists():
            scripts[name] = path.read_text()
        else:
            logger.warning("Hook script missing on proxy: %s", path)
    _HOOK_SCRIPTS_CACHE = scripts
    return scripts


class RemoteStartPayloadMixin:
    # --- Payload builders ---

    async def _build_start_payload(
        self, session_id: str, config: AgentConfig, execution_path: str,
    ) -> dict:
        """Build the config payload sent to the satellite for start_session."""
        import config as app_config
        from core.layers.codex.helpers import (
            LOCAL_ENDPOINT_KEY_ENV,
            LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS,
            codex_hooks_floor,
            map_effort_to_codex,
            permission_to_sandbox,
            build_auth_json_from_env,
            with_local_provider_note,
        )
        from core.layers.codex.local_model_catalog import (
            LOCAL_MODEL_ROWS_ENV, local_model_catalog_json, parse_local_model_rows,
        )

        # Resolve the MOUNT username for CWD — "" for ANY agent-scope mount
        # (Shared-only human chats included, where ctx.username is set but the
        # session works in the agent's shared workspace). Keys on the resolver's
        # mount scope, not raw username, so the satellite runs in the right tree.
        # The session JWT below still carries the REAL user (attribution).
        username = ""
        if config.security_context:
            username = getattr(config.security_context, "mount_username", "")

        # Determine relative paths within agent dir
        if username:
            cwd_relative = f"users/{username}"
            claude_dir_relative = f"users/{username}/.claude"
            codex_dir_relative = f"users/{username}/.codex"
        else:
            cwd_relative = "workspace"
            claude_dir_relative = "workspace/.claude"
            codex_dir_relative = "workspace/.codex"

        # Build env vars (may have Codex-specific entries stripped below).
        # Crucially: inject PROXY_URL + PROXY_API_KEY + OTO_SESSION_ID so
        # the hook scripts on the satellite can call back to the proxy.
        # Without these, `permission_gate.py` exits silently with "allow"
        # (its missing-env early return), which breaks permission
        # prompts and AskUserQuestion on remote agents.
        #
        # Local `PersistentSession.start()` uses `build_session_env()` to
        # add the same three vars for the local subprocess; here we do the
        # equivalent for the payload env the satellite will inherit into
        # the spawned CLI/Codex process.
        from auth.session_token import create_session_token
        # PROXY_URL points at the satellite's local tunnel server
        # on 127.0.0.1, NOT a public platform endpoint. Subprocess hooks +
        # MCP HTTP traffic ride the existing WS tunnel back to the platform.
        # The satellite reports its chosen ephemeral port in capabilities
        # at auth time.
        from storage import remote_store as _remote_store
        machine = _remote_store.get_remote_machine(config.execution_target)
        sat_port = 0
        # ``target_os`` drives OS-aware MCP path rewriting (Windows uses
        # ~/OtoDock/...venv/Scripts/...exe vs Unix ~/.oto-dock/...venv/bin/...).
        # Sourced from ``platform.system().lower()`` in the satellite's
        # ``detect_capabilities`` (linux / darwin / windows).
        target_os = "linux"
        if machine:
            try:
                _caps = json.loads(machine.get("capabilities", "{}") or "{}")
                sat_port = int(_caps.get("local_tunnel_port") or 0)
                target_os = (_caps.get("os") or "linux").lower()
            except (ValueError, TypeError):
                sat_port = 0
        if not sat_port:
            raise RuntimeError(
                f"Satellite {config.execution_target[:8]} has not reported "
                f"a local_tunnel_port in its capabilities — start_session "
                f"requires satellite 0.4.0+."
            )
        # Scope the session JWT to the user (mirror env_builder.py:77) so
        # user-scoped tunnel calls carry the right identity. The AgentConfig
        # doesn't carry the raw sub, so derive it from the security context's
        # username; agent-scope sessions have no user → "".
        _sec = getattr(config, "security_context", None)
        _sec_username = getattr(_sec, "username", "") if _sec else ""
        _user_sub = ""
        if _sec_username:
            from storage import database as _db_us
            _user_sub = _db_us.get_user_sub_by_username(_sec_username) or ""
        env: dict[str, str] = {
            "PROXY_URL": f"http://127.0.0.1:{sat_port}",
            "PROXY_API_KEY": create_session_token(
                session_id, config.agent_name, _user_sub,
            ),
            "OTO_SESSION_ID": session_id,
        }
        # Workspace paths come from manifest-declared `path_env` (already
        # baked into config.credential_env as sandbox-style virtual paths).
        # The satellite's `path_translator.py` rewrites them to
        # satellite-absolute paths before subprocess spawn — same convention
        # as bwrap on local. See proxy/services/path_roles.py.
        env.update(config.extra_env)
        env.update(config.credential_env)
        # A local OpenAI-compatible endpoint (a codex-cli ``local_endpoint``
        # subscription) reaches the layer as two private variables. They never
        # ride the satellite env in this form — popped for EVERY execution path.
        # The satellite writes its own config.toml, so the provider travels as
        # the ``local_model_provider`` payload field (satellite >= 0.5.116) and
        # the key as the child-env variable the provider's ``env_key`` names —
        # the same two artifacts the LOCAL layer produces (``_write_config_toml``).
        # An older satellite would run the local model name against Codex's
        # built-in OpenAI provider, which is exactly the leak this refuses.
        local_endpoint = env.pop("_CODEX_ENDPOINT_URL", "")
        local_api_key = env.pop("_CODEX_LOCAL_API_KEY", "")
        local_provider = env.pop("_CODEX_ENDPOINT_PROVIDER", "")
        local_model_rows = parse_local_model_rows(env.pop(LOCAL_MODEL_ROWS_ENV, ""))
        local_model_provider: dict | None = None
        if local_endpoint and execution_path == "codex-cli":
            machine_id = config.execution_target
            if not self._cm.satellite_supports_local_model_provider(machine_id):
                _ver = self._cm.satellite_version(machine_id) or "an unknown version"
                raise RuntimeError(
                    "Local model endpoints need satellite 0.5.116 or newer — "
                    f"{self._cm.satellite_name(machine_id)} runs {_ver}. Update it "
                    "from the Remote Machines page, run this agent on the server, "
                    "or pick a hosted model."
                )
            # Dialed by the SATELLITE host exactly as configured: no loopback
            # rewrite (``loopback_if_host_self`` maps the PROXY host's own IPs
            # for the sandbox splice); a loopback URL here means the satellite's
            # own machine. There is no netns on a satellite, so no egress carve.
            local_model_provider = {
                "base_url": local_endpoint,
                "env_key": LOCAL_ENDPOINT_KEY_ENV if local_api_key else "",
            }
            # The stream idle timeout and the per-session model catalog (Ollama
            # only: Codex defers its MCP tools) are built here — the satellite
            # has no model registry — and written by its two Codex writers from
            # 0.5.117 on (core/layers/codex/local_model_catalog). An older
            # satellite ignores the fields, so they are only sent (and the gap
            # logged) when it can honour them.
            if self._cm.satellite_supports_local_model_catalog(machine_id):
                local_model_provider["stream_idle_timeout_ms"] = LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS
                local_model_provider["catalog_json"] = local_model_catalog_json(
                    config.model, local_provider, local_model_rows,
                )
            else:
                logger.warning(
                    f"Remote Codex on a local model: {self._cm.satellite_name(machine_id)} "
                    f"runs satellite {self._cm.satellite_version(machine_id) or 'unknown'} "
                    "(< 0.5.117) — no deferred MCP tools, Codex's 5-minute idle timeout "
                    "and the old MCP warm gate until it updates at its next reconnect"
                )
            if local_api_key:
                env[LOCAL_ENDPOINT_KEY_ENV] = local_api_key

        # Common payload fields. Hook scripts travel with every start_session
        # so satellites never need to have them pre-deployed — they live only
        # in the proxy repo.
        #
        # ``multi_value_envs``: tells the satellite's path translator which
        # env vars carry separator-joined sandbox-path lists (e.g.
        # ``ALLOWED_FILE_DIRS=/users/{u}:/workspace:/config``). Built by the
        # config builders from manifest path_env decls + the standard
        # OTO_ALLOWED_ROOTS injection. Without this hint the translator
        # would treat the joined string as a single (non-matching) path
        # and fail to translate.
        # CLI effort: map xhigh→max for models that don't support xhigh — the
        # satellite has no model registry, so the proxy is the single source of
        # truth (mirrors core/layers/cli/session.py). "ultra" is Codex-only
        # (gpt-5.6 multi-agent orchestration) — the `claude` CLI rejects it,
        # clamp to the ceiling. The Codex branch below overrides effort with
        # its own mapping from the raw value.
        cli_effort = config.effort
        if cli_effort == "ultra":
            cli_effort = "max"
        if cli_effort == "xhigh" and not app_config.get_model_supports_xhigh(config.model):
            cli_effort = "max"
        payload = {
            "system_prompt": config.system_prompt,
            "permission_mode": config.permission_mode,
            "client_type": config.client_type,
            "model": config.model,
            "effort": cli_effort,
            "max_thinking_tokens": app_config.MAX_THINKING_TOKENS,
            "env": env,
            "cwd_relative": cwd_relative,
            # otodock-CLI: an absolute satellite-host cwd OUTSIDE agent_dir.
            # When set, the satellite spawns the PTY here while config dirs +
            # username derivation stay keyed on cwd_relative (agent_dir-rooted).
            # Empty = today's in-tree behavior.
            "work_cwd": config.work_cwd or "",
            # otodock-CLI: the local terminal's $TERM (empty for dashboard /
            # headless → satellite keeps its xterm-256color default).
            "term": config.term or "",
            "hook_scripts": _load_hook_scripts(),
            "use_native_permissions": config.use_native_permissions,
            "multi_value_envs": config.multi_value_envs or {},
        }

        # Credential broker: the stdio servers that have a secret bundle
        # — each gets a per-(session, mcp) cap-token injected into its env by the
        # rewrite below, which makes the satellite wrap it with the interceptor.
        secret_bundle_keys = set(config.mcp_secret_bundles or {})
        # HTTP bearer-swap: the subset of bundle MCPs that carry an
        # http_bearer (proxy-terminable github/m365). Their satellite config ships
        # the per-session JWT as the Authorization bearer; the tunnel `_dispatch`
        # swaps it for the real token server-side, so the real bearer never lands
        # on the satellite disk.
        bearer_swap_keys = {
            k for k, b in (config.mcp_secret_bundles or {}).items()
            if getattr(b, "http_bearer", None)
        }

        if execution_path == "codex-cli":
            payload["codex_dir_relative"] = codex_dir_relative
            payload["thread_id"] = config.codex_thread_id
            # The PreToolUse permission FLOOR for unattended sessions — the
            # same rule as the local layer (helpers.codex_hooks_floor). The
            # satellite (>= 0.5.118) writes `[features] hooks = true`, trusts
            # the hook per thread (bypass_hook_trust) and sets the deny-only /
            # no-forward hook env; under `approvalPolicy: never` the approval
            # bridge never fires, so without this a remote task / meeting /
            # phone / trigger session is gated by the prompt rules only. An
            # older satellite ignores the field: warn, never refuse — the
            # session runs the way it did before this field existed.
            floor = codex_hooks_floor(config.client_type, config.interactive)
            payload["codex_hooks_floor"] = floor
            if floor and not self._cm.satellite_supports_codex_hooks_floor(
                    config.execution_target):
                logger.warning(
                    f"Remote Codex {config.client_type} session {session_id[:8]}: "
                    f"{self._cm.satellite_name(config.execution_target)} runs satellite "
                    f"{self._cm.satellite_version(config.execution_target) or 'unknown'} "
                    "(< 0.5.118) — no PreToolUse permission floor for this unattended "
                    "session until it updates at its next reconnect"
                )
            if local_model_provider:
                payload["local_model_provider"] = local_model_provider
                # Tell a local model the truth about its MCP tools for its
                # server (one prompt string, both fields).
                payload["system_prompt"] = with_local_provider_note(
                    config.system_prompt, local_provider,
                )
            payload["agents_md_content"] = payload["system_prompt"]
            # Remote Codex interactive: the satellite builds the `codex` argv,
            # so it needs the resume signal + the cold first prompt (a FRESH codex
            # TUI auto-runs the prompt passed as a positional arg; resume rides the
            # PTY flush). Harmless for the -p app-server path (CodexSession ignores
            # them). interactive_first_prompt is "" unless the proxy set it for a
            # fresh interactive spawn (dashboard.py:_first_prompt_in_argv).
            payload["resume"] = config.resume
            payload["interactive_first_prompt"] = config.interactive_first_prompt or ""
            # Effort mapping must happen on the proxy (single source of truth)
            payload["effort"] = map_effort_to_codex(config.effort, config.model)
            # Same resolution as the local layer, plus the pairing's full-FS
            # grant (remote-only; local bwrap sessions never set it).
            payload["sandbox_mode"] = permission_to_sandbox(
                config.permission_mode,
                allow_full_fs=bool(
                    getattr(config.security_context, "target_allow_full_fs", False)
                ),
            )
            # Construct auth.json from OAuth env vars (pops them from env)
            auth_json = build_auth_json_from_env(env)
            if auth_json is not None:
                payload["auth_json"] = auth_json

            # Read and rewrite TOML MCP config for the satellite.
            #
            # Credential env vars are NOT injected here — they were already
            # baked into the TOML on disk by
            # ``mcp_registry.inject_credential_env_into_toml`` during
            # ``config_builder``'s build step. That injector works on the
            # in-memory servers dict (``.update()`` semantics → keys
            # automatically dedupe). A second injection pass on the
            # serialized TOML string would have to splice keys in via
            # regex, which can't see what's already there — it appended
            # every credential key a second time, producing duplicate keys
            # in each MCP's ``env = { ... }`` inline table, which TOML
            # forbids. Codex's parser rejected the whole config with
            # ``Error loading config.toml: ... duplicate key``, killing
            # the session before any MCP started. CLI didn't hit this
            # (its JSON format passes credentials via process env, not
            # via the config file). The proper fix is to read the file
            # as-is — same as local Codex does.
            if config.mcp_config_path:
                try:
                    from pathlib import Path
                    mcp_path = Path(config.mcp_config_path)
                    if mcp_path.exists():
                        payload["mcp_config_toml"] = _rewrite_mcp_toml_for_remote(
                            mcp_path.read_text(), sat_port,
                            target_os=target_os, session_id=session_id,
                            proxy_api_key=env["PROXY_API_KEY"],
                            secret_bundle_keys=secret_bundle_keys,
                            bearer_swap_keys=bearer_swap_keys,
                        )
                except Exception:
                    logger.exception("Codex MCP TOML build failed")
            # Enable request_user_input in DEFAULT collaboration mode for remote
            # HEADLESS dashboard chats (plan mode gets it natively). The shipped
            # headless remote config carries only [mcp_servers.*] sections, so a
            # standalone [features] block is safe there. NEVER for INTERACTIVE:
            # the satellite's TUI preamble (_build_codex_config_toml) already
            # emits [features] — with this flag in it — and a second [features]
            # table is a DUPLICATE KEY the strict TUI hard-exits on (the
            # "interactive codex terminal is empty" bug; the tolerant app-server
            # masked it on headless). OFF for autonomous runs (task/phone/
            # meeting/trigger — nobody answers).
            if (config.client_type == "dashboard" and not config.interactive
                    and payload.get("mcp_config_toml")):
                payload["mcp_config_toml"] = (
                    "[features]\ndefault_mode_request_user_input = true\n\n"
                    + payload["mcp_config_toml"]
                )

        else:  # claude-code-cli
            payload["claude_dir_relative"] = claude_dir_relative
            payload["resume"] = config.resume
            payload["session_id_for_resume"] = session_id if config.resume else ""
            # OAuth file delivery (mirrors the local CLI layer + the Codex
            # auth_json payload): the satellite writes .credentials.json into
            # the session's CLAUDE_CONFIG_DIR. Popped so no token rides the
            # spawned env — env is frozen at exec and outranks the file, which
            # would defeat rotation fan-out.
            _creds_blob_json = env.pop("_CLAUDE_CREDS_BLOB", "")
            if _creds_blob_json:
                payload["credentials_json"] = {
                    "claudeAiOauth": json.loads(_creds_blob_json),
                }
                # Rotation fan-out reaches a satellite after a WS round-trip;
                # a request racing that push 401s first. This arms Claude's
                # 401-recovery poll window so it re-reads the credential file
                # until the push lands (local sessions write synchronously and
                # don't need it).
                from services.engines.token_fanout import REMOTE_CLAUDE_401_WAIT_MS
                env["CLAUDE_CODE_OAUTH_401_WAIT_MS"] = str(REMOTE_CLAUDE_401_WAIT_MS)
            # Ship the built-in-tool deny list so the satellite's settings.json
            # carries the SAME permissions.deny the local sandbox applies (Skill,
            # the claude.ai Cron/Trigger/Push/integration tools). Single source of
            # truth = core.sandbox.sandbox._DISALLOWED_BUILTIN_TOOLS; without this the deny
            # silently did not apply on ANY remote session.
            from core.sandbox.sandbox import _DISALLOWED_BUILTIN_TOOLS
            payload["disallowed_tools"] = list(_DISALLOWED_BUILTIN_TOOLS)

            # Read and rewrite JSON MCP config
            if config.mcp_config_path:
                try:
                    from pathlib import Path
                    mcp_path = Path(config.mcp_config_path)
                    if mcp_path.exists():
                        mcp_json = json.loads(mcp_path.read_text())
                        payload["mcp_config"] = _rewrite_mcp_json_for_remote(
                            mcp_json, sat_port, target_os=target_os,
                            session_id=session_id,
                            secret_bundle_keys=secret_bundle_keys,
                            bearer_swap_keys=bearer_swap_keys,
                            proxy_api_key=env["PROXY_API_KEY"],
                        )
                except Exception:
                    pass

        return payload
