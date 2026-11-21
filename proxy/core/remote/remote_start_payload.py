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

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from core import layout
from core.execution_layer import (
    AgentConfig,
    RemoteStartContext,
    RemoteStartPlan,
    SpawnPlan,
)
from core.permission_modes import cli_mode
from core.remote.tunnel_state import valid_port
from core.sandbox import session_config_dir as _scd

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
    # The same scripts the local sandbox installs (HOOK_SCRIPTS is the one
    # list).
    from core.sandbox.session_config_dir import HOOK_SCRIPTS
    for name in HOOK_SCRIPTS:
        path = hooks_dir / name
        if path.exists():
            scripts[name] = path.read_text()
        else:
            logger.warning("Hook script missing on proxy: %s", path)
    _HOOK_SCRIPTS_CACHE = scripts
    return scripts


def _gateway_facts(bundles: dict) -> dict[str, dict]:
    """What the engine's config rewrite needs per credentialed HTTP MCP, by
    mcpServers key: the upstream origin and path, the header, and whether
    the upstream is a sidecar the proxy reaches itself or a credential the
    proxy forwards through the tunnel. Secret-free: the machine's gateway
    adds the credential on the way out."""
    facts: dict[str, dict] = {}
    for key, bundle in (bundles or {}).items():
        cred = getattr(bundle, "gateway", None)
        if cred is None:
            continue
        facts[key] = {"upstream": cred.upstream, "path": cred.path, "header": cred.header,
                      "proxy_local": bool(cred.proxy_local), "tunneled": cred.forwarded_here}
    return facts


def _strip_private_carriers(env: dict) -> None:
    """No engine's PRIVATE carrier (a credential blob, a local endpoint,
    the model rows) ever rides the satellite env: the engine's own builder
    consumed its own; this strips every registered engine's, so a session
    of one engine can never ship another's."""
    from core.session.session_manager import get_all_layers
    for other in get_all_layers().values():
        other_adapter = other.remote_adapter()
        if other_adapter is not None:
            for key in other_adapter.private_env_keys:
                env.pop(key, None)


if TYPE_CHECKING:
    from core.remote.remote_host import RemoteLayerHost as _Host
else:
    _Host = object


async def ssh_agent_payload(config: AgentConfig) -> dict | None:
    """The ``ssh_agent`` field of a paired machine's start payload: the
    agent's public key lines by key name, for a session that takes the
    keys (``session_takes_ssh_keys``) on a machine (every satellite at the
    platform's floor opens the socket). None otherwise."""
    from core.credentials import ssh_signer
    from core.sandbox.session_config_dir import session_takes_ssh_keys
    from storage.pg import run_db
    ctx = config.security_context
    if not session_takes_ssh_keys(ctx):
        return None
    machine = getattr(ctx, "placement", None)
    if machine is None:
        return None
    lines = await run_db(ssh_signer.public_lines, config.agent_name)
    if not lines:
        return None
    return {"public_keys": dict(lines)}


class RemoteStartPayloadMixin(_Host):
    # --- Payload builders ---

    async def _build_start_payload(
        self, session_id: str, config: AgentConfig, execution_path: str,
        spawn: SpawnPlan | None = None,
    ) -> RemoteStartPlan:
        """Build the config payload sent to the satellite for start_session,
        plus what the caller needs to know about it (``RemoteStartPlan``):
        the shared part here, the engine's part through its remote adapter.
        ``spawn`` is a compute session's plan: the machine runs the plan's
        argv and environment in the sandbox it renders from the plan's
        description, reading the platform's folder itself, so the payload
        carries the plan and the words the machine reads and none of the
        files a paired computer is sent."""
        import config as app_config
        from core.session.session_manager import get_layer_by_path
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
        # The three vars `build_session_env()` gives a spawn plan, for the
        # payload env the satellite will inherit into the spawned CLI/Codex
        # process.
        from auth.session_token import create_session_token

        # PROXY_URL points at the satellite's local tunnel server
        # on 127.0.0.1, NOT a public platform endpoint. Subprocess hooks +
        # MCP HTTP traffic ride the existing WS tunnel back to the platform.
        # The satellite reports its chosen ephemeral port in capabilities
        # at auth time.
        from core.remote.remote_session_start import machine_of_config
        from storage import remote_store as _remote_store
        machine_id = machine_of_config(config)
        machine = _remote_store.get_remote_machine(machine_id)
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
        # The live connection's port first: a heartbeat that reported the
        # local link on a new port (satellite 0.5.168) reaches the row only
        # through the off-loop caps lane.
        sat_port = valid_port(self._cm.tunnel_port(machine_id)) or sat_port
        if not sat_port:
            raise RuntimeError(
                f"Satellite {machine_id[:8]} has not reported "
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
        # The start's riding part (``core/session/start_context.py``): a fresh
        # conversation holds none of it, on a compute machine too; a Codex
        # start takes it through its start hook where the machine writes the
        # hook (a compute machine reads the platform's own hooks).
        from core.session import start_context
        if not config.resume:
            start_context.reset(session_id)
        if start_context.engine_door(execution_path) == start_context.DOOR_HOOK and (
                spawn is not None or self._cm.satellite_takes_start_context(machine_id)):
            start_context.set_door(session_id, start_context.DOOR_HOOK)
        if spawn is not None:
            return self._compute_payload(session_id, config, execution_path, spawn,
                                         machine_id=machine_id, sat_port=sat_port,
                                         target_os=target_os, scope_root=cwd_relative)
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
        # Where git's credential helper lives (the engine's config dir, in
        # sandbox-virtual form the satellite translates like every path
        # value) and the interpreter that runs it; satellite 0.5.139 sets
        # both itself after the translation and prepends the wrappers'
        # directory to PATH. The git entries the resolver writes expand them
        # through the shell.
        env["OTO_HOOKS_DIR"] = f"/{cwd_relative}/{engine.capabilities.runtime.config_dir_name}"
        env["OTO_HOOK_PY"] = "python3"
        # The shell's command-line token, over the final session token. Every
        # satellite overlays this env on the CLI's, so a 1.8 one passes it on.
        from auth.session_token import attach_cli_token
        attach_cli_token(env)

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
            # Claude Code's own word for the mode, which the satellite spawns
            # by (the CLI's auto mode for Auto).
            "cli_permission_mode": cli_mode(config.permission_mode),
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
            # The command wrappers (the satellite writes them under the
            # config dir's bin/).
            "command_wrappers": dict(_scd.COMMAND_WRAPPERS),
            "use_native_permissions": config.use_native_permissions,
            "multi_value_envs": config.multi_value_envs or {},
        }

        # A terminal whose satellite runs the start hook asks for the start's
        # riding part at each start, and so does a headless Codex session;
        # a terminal whose satellite cannot gets it in its text, as before.
        # Asked of every start there: the settings file is the scope's.
        if self._cm.satellite_takes_start_context(machine_id):
            payload["start_context_parts"] = start_context.HOOK_PARTS
        elif config.interactive:
            payload["system_prompt"] = (config.system_prompt or "") + await asyncio.to_thread(
                start_context.inline_block, session_id)

        # The session's SSH agent socket on the machine: the
        # satellite opens it and forwards each message to the proxy's
        # signer; the public halves ride along for the key dir.
        ssh_agent = await ssh_agent_payload(config)
        if ssh_agent is not None:
            payload["ssh_agent"] = ssh_agent
        # The machine's credential exit (the machine admitted):
        # the session's secret, the hash its pushed values are keyed by and
        # its entries, nothing of a value (``credential_exit.machine_block``).
        from core.credentials import credential_exit
        from core.remote.mcp_gateway_push import token_hash_of
        block = credential_exit.machine_block(session_id, config.mcp_secret_bundles,
                                              token_hash_of(env["PROXY_API_KEY"]))
        if block is not None:
            payload["exit"] = block

        # Credential broker: the stdio servers that have a secret bundle
        # — each gets a per-(session, mcp) cap-token injected into its env by the
        # engine's MCP config rewrite, which makes the satellite wrap it with
        # the interceptor.
        secret_bundle_keys = set(config.mcp_secret_bundles or {})
        # The credential gateway: what the engine's rewrite needs per
        # credentialed HTTP MCP. A vendor entry dials the machine's loopback
        # gateway and a sidecar entry the tunnel.
        gateway = _gateway_facts(config.mcp_secret_bundles or {})

        # The engine's part: its config dir, credential file, MCP config in
        # its format, resume handle, effort and sandbox mapping.
        plan = adapter.start_payload(payload, RemoteStartContext(
            session_id=session_id,
            machine_id=machine_id,
            sat_port=sat_port,
            target_os=target_os,
            config=config,
            scope_root=cwd_relative,
            env=env,
            proxy_api_key=env["PROXY_API_KEY"],
            secret_bundle_keys=secret_bundle_keys,
            gateway=gateway,
            cm=self._cm,
        ))
        _strip_private_carriers(env)
        return plan

    def _compute_payload(
        self, session_id: str, config: AgentConfig, execution_path: str, spawn: SpawnPlan, *,
        machine_id: str, sat_port: int, target_os: str, scope_root: str,
    ) -> RemoteStartPlan:
        """The payload of a session on a compute machine. The environment
        is the plan's exactly (``PROXY_URL`` on the proxy port, which the
        rendered sandbox splices onto the machine's link; the machine adds
        nothing). No hook scripts, wrappers, MCP config, login file or
        prompt ride it: the engine's layer wrote them into the platform's
        folder, which the machine's session reads. ``tunnel_port`` names
        the link the session is started on, which its ``PROXY_URL`` cannot."""
        import config as app_config
        from core.session.session_manager import get_layer_by_path
        adapter = get_layer_by_path(execution_path).remote_adapter()
        if adapter is None:
            raise RuntimeError(f"{execution_path} cannot run remotely")
        env = spawn.env
        payload = {
            "permission_mode": config.permission_mode,
            "cli_permission_mode": cli_mode(config.permission_mode),
            "client_type": config.client_type,
            "model": config.model,
            "effort": config.effort,
            "max_thinking_tokens": app_config.MAX_THINKING_TOKENS,
            "env": env,
            "cwd_relative": scope_root,
            "work_cwd": "",
            "term": config.term or "",
            "use_native_permissions": config.use_native_permissions,
            "sandbox": spawn.sandbox,
            "argv": list(spawn.argv),
            "cwd": spawn.cwd,
            "tunnel_port": sat_port,
        }
        # The description names the session's agent socket: the machine
        # opens it under that name and forwards the messages; the key dir
        # with the public halves is the platform's folder, which it reads.
        if spawn.sandbox.get("ssh_agent"):
            payload["ssh_agent"] = {"public_keys": {}}
        # The description names the exit's splice: the machine's exit takes
        # the session's door from this block (its secret, the hash its
        # pushed values are keyed by, its entries, its carves for the dial rule).
        if spawn.sandbox.get("exit_port"):
            from core.credentials import credential_exit
            from core.remote.mcp_gateway_push import token_hash_of
            block = credential_exit.machine_block(session_id, config.mcp_secret_bundles,
                                                  token_hash_of(str(env.get("PROXY_API_KEY") or "")))
            if block is not None:
                payload["exit"] = block
        plan = adapter.start_payload(payload, RemoteStartContext(
            session_id=session_id,
            machine_id=machine_id,
            sat_port=sat_port,
            target_os=target_os,
            config=config,
            scope_root=scope_root,
            env=env,
            proxy_api_key=str(env.get("PROXY_API_KEY") or ""),
            secret_bundle_keys=set(config.mcp_secret_bundles or {}),
            gateway=_gateway_facts(config.mcp_secret_bundles or {}),
            cm=self._cm,
            spawn=spawn,
        ))
        _strip_private_carriers(env)
        return plan
