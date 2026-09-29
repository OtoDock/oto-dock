"""The ``start_session`` payload for a satellite (mixin).

``_build_start_payload`` assembles what every engine's satellite session
needs — the session env with the tunnel identity, the working root, the
bundled hook scripts, the path-translation hints — and hands the payload to
the ENGINE's remote adapter (``RemoteEngineAdapter.start_payload``), which
adds its own keys: its config dir, its credential file, its MCP config in
its format, its resume handle, its effort and sandbox mapping. Nothing here
names an engine. Mixed into RemoteExecutionLayer; split out of
remote_execution.py.
"""

import json
import logging

from core import layout
from core.execution_layer import AgentConfig, RemoteStartContext, RemoteStartPlan

logger = logging.getLogger("remote-layer")


# --- Hook script cache ---------------------------------------------------
# Hook scripts are bundled into every `start_session` payload so the
# satellite never needs them pre-deployed. Cached at module scope — they
# change only when the proxy is redeployed.

_HOOK_SCRIPTS_CACHE: dict[str, str] | None = None


def _load_hook_scripts() -> dict[str, str]:
    """Return {filename: contents} for the hook scripts the CLI runs.

    These are the same scripts the local sandbox installs; on remote they're
    written into the satellite's per-session config dir before the spawn.
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
    # The same four scripts the local sandbox installs (HOOK_SCRIPTS is the
    # one list); a satellite older than 0.5.121 writes stop_tracker.py and
    # never names it in its hook files — harmless.
    from core.sandbox.session_config_dir import HOOK_SCRIPTS
    for name in HOOK_SCRIPTS:
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
    ) -> RemoteStartPlan:
        """Build the config payload sent to the satellite for start_session,
        plus what the caller needs to know about it (``RemoteStartPlan``):
        the shared part here, the engine's part through its remote adapter."""
        import config as app_config
        from core.session.session_manager import get_all_layers, get_layer_by_path
        engine = get_layer_by_path(execution_path)
        adapter = engine.remote_adapter()
        if adapter is None:
            raise RuntimeError(f"{engine.capabilities.display_name} cannot run remotely")

        # Resolve the MOUNT username for CWD — "" for ANY agent-scope mount
        # (Shared-only human chats included, where ctx.username is set but the
        # session works in the agent's shared workspace). Keys on the resolver's
        # mount scope, not raw username, so the satellite runs in the right tree.
        # The session JWT below still carries the REAL user (attribution).
        username = ""
        if config.security_context:
            username = getattr(config.security_context, "mount_username", "")
        # The working root inside the agent dir; the engine names its scope
        # config dir under it (its declared dirname — the satellite clamps the
        # credential file to that suffix).
        cwd_relative = layout.scope_root(username)

        # Build env vars. Crucially: inject PROXY_URL + PROXY_API_KEY +
        # OTO_SESSION_ID so the hook scripts on the satellite can call back
        # to the proxy. Without these, `permission_gate.py` exits silently
        # with "allow" (its missing-env early return), which breaks
        # permission prompts and AskUserQuestion on remote agents.
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
        # ``effort`` is seeded raw; the engine's adapter overwrites it with
        # its own mapping (the satellite has no model registry, so the proxy
        # is the single source of truth).
        payload = {
            "system_prompt": config.system_prompt,
            "permission_mode": config.permission_mode,
            "client_type": config.client_type,
            "model": config.model,
            "effort": config.effort,
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

        # Hook parity (HOOKS.md): a satellite older than 0.5.121 runs this
        # session without a Stop hook (no turn-end signal from the machine)
        # and, for a Codex chat, answers a question empty. Warn, never
        # refuse — auto-update brings it up at its next reconnect.
        if not self._cm.satellite_supports_hook_parity(config.execution_target):
            logger.warning(
                f"Remote {execution_path} {config.client_type or 'session'} "
                f"{session_id[:8]}: {self._cm.satellite_name(config.execution_target)} "
                f"runs satellite {self._cm.satellite_version(config.execution_target) or 'unknown'} "
                "(< 0.5.121) — no Stop hook and no Codex question bridge on "
                "this machine until it updates at its next reconnect"
            )

        # Credential broker: the stdio servers that have a secret bundle
        # — each gets a per-(session, mcp) cap-token injected into its env by the
        # engine's MCP config rewrite, which makes the satellite wrap it with
        # the interceptor.
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

        # The engine's part: its config dir, credential file, MCP config in
        # its format, resume handle, effort and sandbox mapping.
        plan = adapter.start_payload(payload, RemoteStartContext(
            session_id=session_id,
            machine_id=config.execution_target,
            sat_port=sat_port,
            target_os=target_os,
            config=config,
            scope_root=cwd_relative,
            env=env,
            proxy_api_key=env["PROXY_API_KEY"],
            secret_bundle_keys=secret_bundle_keys,
            bearer_swap_keys=bearer_swap_keys,
            cm=self._cm,
        ))
        # No engine's PRIVATE carrier (a credential blob, a local endpoint,
        # the model rows) ever rides the satellite env — the engine's own
        # builder consumed its own above; this strips every registered
        # engine's, so a session of one engine can never ship another's.
        for other in get_all_layers().values():
            other_adapter = other.remote_adapter()
            if other_adapter is not None:
                for key in other_adapter.private_env_keys:
                    env.pop(key, None)
        return plan
