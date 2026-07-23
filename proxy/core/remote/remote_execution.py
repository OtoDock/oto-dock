"""RemoteExecutionLayer — routes CLI and Codex sessions to satellite daemons.

This layer implements the ExecutionLayer ABC by delegating subprocess management
to a connected satellite daemon over WebSocket. Raw events from the satellite
are translated to CommonEvent using the same translators as local sessions.
"""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from core.events.common_events import CommonEvent, DONE, ERROR, METADATA, PLAN_MODE, TEXT
from core.execution_layer import ExecutionLayer, AgentConfig, LayerCapabilities
from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.layers.cli.settle import (
    FOREIGN_SKIP_SILENCE_S,
    ForeignSkipGate,
    SettleController,
    chunk_is_content,
    is_foreign_result,
)
from core.layers.codex.layer import CodexEventTranslator
from core.layers.codex.session import CodexEvent
from core.remote import upload_inflight
from core.remote.satellite_connection import SatelliteConnectionManager
from core.session.session_state import (
    _record_session_use, set_session_security, set_session_mode,
    resolve_permission,
    cleanup_session_permission_state, get_session_user_tz,
    resolve_session_permissions,
    reset_subagent_registry,
    resolve_bg_command_frame,
)
from core.events.bg_command_state import reset_bg_command_registry
import config

logger = logging.getLogger("remote-layer")

# Soft-interrupt escalation window. Longer than the local layer's 8s: the
# frame rides the satellite WS and the CLI's result event rides back the
# same way, so budget for two hops plus the CLI's own reaction time.
_REMOTE_INTERRUPT_WATCHDOG_S = 12.0

# Strong refs — a bare create_task is GC-collectable mid-flight.
_remote_watchdog_tasks: set[asyncio.Task] = set()


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


def get_remote_layer():
    """Accessor for other modules that need the RemoteExecutionLayer
    singleton without triggering a circular import. Delegates to
    session_manager's lazy initializer.
    """
    from core.session.session_manager import _get_remote_layer
    return _get_remote_layer()


@dataclass
class RemoteSessionInfo:
    """Tracks a remote session's state on the proxy side."""
    session_id: str
    machine_id: str
    agent_name: str
    execution_path: str  # "claude-code-cli" or "codex-cli"
    event_queue: asyncio.Queue
    alive: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Codex per-session state (translator persists cumulative token deltas)
    codex_translator: CodexEventTranslator | None = None
    codex_thread_id: str = ""
    # CLI per-session state (created fresh per turn in send_message)
    cli_translator: ClaudeCLIEventTranslator | None = None
    cli_settle: SettleController | None = None
    # Permission-mode tracking (native vs hook-based permissions)
    use_native_permissions: bool = False
    model: str = ""
    mode: str = "default"
    last_activity: float = field(default_factory=time.monotonic)
    # Set of MCP names this session has in its shipped config. Consumed by
    # mcp_sync to compute the union across active sessions on the same
    # machine (deferred-uninstall guard).
    used_mcps: set[str] = field(default_factory=set)
    # Fallback reason surfaced in warmup_ready for UI badges. None means
    # the session is running on the user/admin-intended target.
    fallback_reason: str | None = None
    # command_id of the in-flight `send_message` command. Used to filter
    # stale `_turn_ended` events from a previous turn whose satellite-side
    # file-scan ran past the proxy's drain timeout — without this match,
    # a late turn_ended would terminate the NEW turn with zero events.
    current_send_command_id: str = ""
    # Set when the satellite's CLI subprocess is known to have died (user
    # abort on the first turn, satellite-reported CLI crash). Drives
    # ``is_session_process_dead`` so the dashboard's auto-resume path
    # spawns a fresh session on the next user message — mirrors local
    # ``PersistentSession`` detecting a dead proc via ``returncode``.
    cli_dead: bool = False
    # True while a send_message turn is streaming. The idle reaper must not
    # close a session mid-turn on event silence alone — a network stall on
    # the satellite box leaves the CLI alive and working with zero stream
    # events for many minutes (the Mode D incident).
    turn_active: bool = False
    # --- Remote Codex background sub-agent demux (mirrors the LOCAL session's
    # router/supervisor in core/layers/codex/session.py, but consumes the
    # WS-forwarded session_event stream instead of the daemon's notif_queue).
    # Only active for codex-cli on a satellite new enough to forward bg-thread
    # events past the main turn (version-gated; see start_session). When off,
    # _stream_codex_turn reads event_queue directly = today's behavior. ---
    bg_supervised: bool = False
    router_task: asyncio.Task | None = None
    default_consumer: asyncio.Queue | None = None      # active main turn (router → here)
    thread_consumers: dict[str, asyncio.Queue] = field(default_factory=dict)  # sub_tid → buffer
    bg_supervisors: dict[str, asyncio.Task] = field(default_factory=dict)     # sub_tid → supervisor


from core.remote.remote_bg_subagent import RemoteBgSubagentMixin
from core.remote.remote_workspace_sync import (  # noqa: F401
    RemoteWorkspaceSyncMixin,
    _partition_deferred_pulls,
    _DEFER_PULL_MIN_BYTES,
)


def _machine_sync_role(
    machine: dict | None, agent_slug: str, session_role: str,
) -> str:
    """The role the workspace SYNC runs with for this session's machine.

    ADMIN-paired machines sync with the session's own role (the person
    driving the session there gates config/ push per-session). USER-paired
    machines sync with the OWNER's per-agent role — never the session's
    platform-inflated one. A platform admin who is per-agent viewer/editor
    carries role "admin" in the SecurityContext, which used to grant their
    personal machine config/ push + write-back — letting a satellite that
    lost its copy delete-attribute the agent's prompt at session start.
    Machine-scope sync authority comes from the pairing (mirrors
    ``resolve_machine_sync_identity``); the SESSION keeps its full role
    everywhere else. Missing owner / role ⇒ ``""`` (fail-closed: syncs
    like a viewer — personal dirs only).
    """
    if not machine or machine.get("pairing_scope", "") == "admin":
        return session_role
    from storage import database as _db
    owner_sub = machine.get("registered_by", "") or ""
    roles = _db.get_user_agent_roles(owner_sub) if owner_sub else {}
    return (roles or {}).get(agent_slug, "")


def _collect_session_files(
    config: AgentConfig,
    machine: dict | None,
    target_username: str | None,
) -> dict:
    """Build the per-session secret-FILE map for the session-file broker.

    Two sources, two key shapes:
      - ``ssh/<key_name>`` — ssh-hosts private keys, materialized under the
        satellite's session-secrets dir (env: ``OTO_SSH_KEY_DIR``).
        ADMIN-PAIRED machines only — infra key material never reaches a
        user-paired satellite, mirroring the agent-scope-credentials rule.
      - ``/users/{u}/.credentials/…`` / ``/knowledge/.credentials/…`` —
        OAuth token files for credentials_dir MCPs, keyed by the
        sandbox-virtual path the MCP's env already points at; the satellite
        path-translates and lands them inside the agent tree. Delivered to
        admin-paired machines for any session scope; to user-paired
        machines ONLY for the owner's own user-scope sessions (exactly
        what the old persistent `.credentials` sync used to deliver —
        agent-scope service tokens never land on user hardware).
    """
    import base64

    from core.credentials import mcp_broker
    from core.sandbox.session_config_dir import collect_authorized_ssh_keys
    from services.oauth import credential_resolver

    admin_paired = bool(machine) and machine.get("pairing_scope", "") == "admin"
    files: dict[str, mcp_broker.SessionFile] = {}

    if admin_paired:
        for name, src in collect_authorized_ssh_keys(config.agent_name).items():
            files[f"ssh/{name}"] = mcp_broker.SessionFile(
                content_b64=base64.b64encode(src.read_bytes()).decode(),
            )

    ctx = config.security_context
    username = getattr(ctx, "username", "") or ""
    user_sub = getattr(config, "user_sub", "") or ""
    # Credentials follow the MOUNT scope — the same invariant the config
    # builders enforce (config_builder passes task_scope=vis.mount_scope):
    # user-scope sessions resolve the engaging user's accounts under
    # /users/{u}/.credentials, agent-scope sessions (Shared-only chats,
    # tasks, phone) resolve the agent's bound SERVICE account under
    # /knowledge/.credentials. SecurityContext.session_scope IS that mount
    # scope, so keying on it keeps the delivered file and the MCP's
    # credentials_dir env pointing at the same path.
    scope = getattr(ctx, "session_scope", "") or "user"
    allow_tokens = admin_paired or (
        scope == "user" and bool(username) and username == (target_username or "")
    )
    if allow_tokens:
        token_files = credential_resolver.collect_oauth_token_files(
            config.agent_name,
            user_sub=user_sub,
            session_scope=scope,
        )
        for vpath, content in token_files.items():
            files[vpath] = mcp_broker.SessionFile(
                content_b64=base64.b64encode(content).decode(),
            )
    return files


class RemoteExecutionLayer(
    RemoteWorkspaceSyncMixin,
    RemoteBgSubagentMixin,
    ExecutionLayer,
):
    """Routes sessions to satellite daemons via WebSocket.

    Supports both CLI and Codex execution paths. Direct LLM is always
    handled locally and never routes through this layer.
    """

    def __init__(self, connection_manager: SatelliteConnectionManager):
        self._cm = connection_manager
        self._sessions: dict[str, RemoteSessionInfo] = {}
        # Background deferred-pull tasks: held so create_task'd coroutines
        # aren't GC'd mid-flight; each removes itself on completion.
        self._deferred_sync_tasks: set[asyncio.Task] = set()

    # --- ExecutionLayer interface ---

    async def start_session(self, session_id: str, config: AgentConfig) -> None:
        machine_id = config.execution_target
        if machine_id == "local":
            raise RuntimeError("RemoteExecutionLayer called with local target")

        if not self._cm.is_connected(machine_id):
            raise RuntimeError(
                f"Satellite {machine_id[:8]} is not connected"
            )

        # Per-satellite soft pre-check: reject early when the satellite
        # reports it's at its session ceiling (admin override or its own physical
        # recommendation). Best-effort + fail-open; the satellite's hard reject in
        # session_manager._check_capacity is the authoritative backstop.
        if self._cm.machine_at_capacity(machine_id):
            raise RuntimeError(
                f"Remote machine {machine_id[:8]} is at capacity — too many active sessions"
            )

        # Determine the actual execution path from the config (set by config builders)
        from storage import agent_store
        execution_path = config.execution_path
        if not execution_path:
            agent = agent_store.get_agent(config.agent_name)
            execution_path = (agent or {}).get("execution_path", "claude-code-cli")
        if execution_path == "direct-llm":
            raise RuntimeError("Direct LLM cannot run remotely")

        # Build layer-specific config payload for satellite
        payload = await self._build_start_payload(
            session_id, config, execution_path
        )

        # Credential broker: provision THIS session's per-MCP secrets so
        # the satellite's stdio interceptor can fetch them over the tunnel at MCP
        # spawn (the cap-token was injected into each stdio server's env by
        # _build_start_payload's rewrite). The store is in-memory on the proxy —
        # never sent to the satellite as a file. Idempotent — a no-op for
        # sessions with no secret bundles.
        from core.credentials import mcp_broker
        mcp_broker.provision(session_id, config.mcp_secret_bundles or {})

        # Derive the set of MCP names the CLI/Codex will try to launch on
        # the satellite. mcp_sync reconciles this against what's already
        # installed so missing/out-of-date MCPs are shipped + installed
        # before the CLI starts.
        assigned_mcps = self._extract_assigned_mcps(payload, execution_path)

        # Initial workspace sync: push platform-side files (workspace,
        # config/, .claude/, .codex/, users/) to the satellite before the
        # CLI starts so the agent on the satellite sees the same workspace
        # the user sees in the dashboard. Best-effort — log and continue
        # on failure (CLI start still proceeds; missing files surface as
        # MCP errors).
        #
        # Per-user satellite isolation: user-paired satellites (pairing_scope
        # != 'admin') must never receive OTHER users' data or agent-scope
        # credentials. Compute the target username once and pass it down;
        # admin-shared machines pass None and see everything. Keyed on the
        # stable pairing_scope, NOT the owner's mutable platform role.
        from storage import remote_store as _rs
        from storage import database as _db
        target_username: str | None = None
        machine = _rs.get_remote_machine(machine_id)
        if machine and machine.get("pairing_scope", "") != "admin":
            # User-paired (or orphaned-owner) machine. Resolve the owner's
            # username so the sync filter scopes data to that one user;
            # coerce to empty string when the user record has been deleted
            # so all users/<u>/* paths get filtered (fail-safe — never
            # silently leak orphaned tokens by falling back to admin
            # behavior).
            owner_sub = machine.get("registered_by", "")
            target_username = (
                _db.get_username_by_sub(owner_sub) if owner_sub else None
            ) or ""
        # Session-file broker: provision per-session secret FILES (SSH keys
        # for ssh-hosts + OAuth token files for credentials_dir MCPs) and
        # hand the satellite a one-shot capability token. The satellite
        # fetches over the tunnel BEFORE the CLI spawns, materializes 0600
        # (ssh keys under its session-secrets dir; token files at their
        # virtual credentials_dir target inside the agent tree), and wipes
        # everything at session close. The token rides the payload but never
        # enters the spawned agent env. Gating lives in
        # _collect_session_files: SSH keys are admin-paired-only; OAuth
        # token files reach user-paired machines only for the owner's own
        # user-scope sessions. `.credentials` is NOT part of the persistent
        # file sync, so this channel is the ONLY way tokens reach a
        # satellite disk — transiently, by design.
        try:
            session_files = _collect_session_files(
                config, machine, target_username,
            )
            if session_files:
                mcp_broker.provision_session_files(session_id, session_files)
                payload["session_files_token"] = mcp_broker.mint_files_token(
                    session_id,
                )
                if any(p.startswith("ssh/") for p in session_files):
                    # env var → subdir under the satellite's materialized
                    # session-secrets dir. The satellite resolves and injects.
                    payload["session_files_env"] = {"OTO_SSH_KEY_DIR": "ssh"}
        except Exception:
            logger.exception(
                "session-file provisioning failed for %s — session "
                "starts without SSH keys / OAuth token files", session_id[:8],
            )

        # Per-agent role drives the config/ filter — editor and
        # viewer satellite sessions never receive the agent's prompt /
        # context files on disk. The session's authenticated human
        # (SecurityContext slug; "" for service sessions) rides along as the
        # write-back identity for ADMIN-SHARED machines, where
        # target_username is None by design — the sync's owner-tier config/
        # write-back must key on the person driving the session there,
        # mirroring the live-path file_changed applier.
        target_role = _machine_sync_role(
            machine, config.agent_name,
            getattr(config.security_context, "role", "") or "",
        )
        session_username = getattr(config.security_context, "username", "") or ""

        # Workspace sync + MCP install share ONE install-registry lifecycle,
        # keyed by (machine_id, agent_slug) — NOT chat_id: both are
        # satellite-level operations shared across chats. Registered BEFORE
        # the workspace sync (it used to cover only the MCP install) so the
        # sync's progress events ride the same dashboard bar — the initial
        # sync of a big workspace used to be pure dead air. Any failed MCP
        # install soft-fails and removes that MCP from the session's config
        # so the CLI doesn't try to spawn a broken stdio server. Phone +
        # scheduler paths drive the same lifecycle with no dashboard WS
        # listener; events accumulate in the registry's bounded history and
        # the sweeper drops the entry after 600s ("fire and drop").
        from services.mcp import mcp_sync
        from core.remote import install_registry

        await install_registry.register(
            machine_id, config.agent_name, getattr(config, "user_sub", "") or "",
        )
        heartbeat_stop = asyncio.Event()
        # Defer install_started until there's REAL work (the first plan/progress
        # event). On an already-synced satellite the diff is empty and we emit
        # nothing at all — so the dashboard shows no bar instead of a 100%-flash.
        install_started_flag = {"v": False}

        async def _emit_install_started_once() -> None:
            if install_started_flag["v"]:
                return
            install_started_flag["v"] = True
            await install_registry.emit(machine_id, config.agent_name, {
                "type": "install_started",
                "machine_id": machine_id,
                "agent": config.agent_name,
            })

        async def _install_heartbeat_loop() -> None:
            try:
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=15)
                        return
                    except asyncio.TimeoutError:
                        if not install_started_flag["v"]:
                            continue  # no real work yet — don't heartbeat into the void
                        rec = install_registry.get(machine_id, config.agent_name)
                        if rec is None:
                            return
                        if time.monotonic() - rec.last_emit_ts >= 15:
                            await install_registry.emit(
                                machine_id, config.agent_name, {
                                    "type": "install_heartbeat",
                                    "machine_id": machine_id,
                                    "agent": config.agent_name,
                                },
                            )
            except Exception:
                logger.exception("install heartbeat loop crashed")

        hb_task = asyncio.create_task(_install_heartbeat_loop())

        async def _on_plan(ev: dict) -> None:
            await _emit_install_started_once()
            await install_registry.emit(machine_id, config.agent_name, {
                "type": "install_mcp_plan",
                "machine_id": machine_id,
                "agent": config.agent_name,
                "mcps_to_install": ev.get("mcps_to_install", []),
                "mcps_to_update": ev.get("mcps_to_update", []),
            })

        async def _on_progress(ev: dict) -> None:
            await _emit_install_started_once()
            # The satellite emits one aggregate phase="verifying" event (no
            # mcp) when it begins the post-install pre-warm boot check. Map
            # it to a distinct install_verifying so the dashboard can show
            # "Checking MCPs…" without polluting per-MCP progress rows.
            if ev.get("phase") == "verifying":
                await install_registry.emit(machine_id, config.agent_name, {
                    "type": "install_verifying",
                    "machine_id": machine_id,
                    "agent": config.agent_name,
                    "message": ev.get("message", "Checking MCPs…"),
                })
                return
            await install_registry.emit(machine_id, config.agent_name, {
                "type": "install_progress",
                "machine_id": machine_id,
                "agent": config.agent_name,
                "mcp": ev.get("mcp", ""),
                "phase": ev.get("phase", ""),
                "pct": int(ev.get("pct", 0) or 0),
                "message": ev.get("message", ""),
            })

        async def _sync_progress(done: int, total: int) -> None:
            # W2 (sync-performance): live workspace-sync progress in the SAME
            # install bar ("workspace files — N%"). Throttling lives in
            # _initial_workspace_sync; the started-once defer keeps an
            # already-synced tree bar-free (no 100% flash).
            await _emit_install_started_once()
            await install_registry.emit(machine_id, config.agent_name, {
                "type": "install_progress",
                "machine_id": machine_id,
                "agent": config.agent_name,
                "mcp": "workspace files",
                "phase": "syncing",
                "pct": int(done * 100 / max(total, 1)),
                "message": f"{done}/{total} files",
            })

        try:
            try:
                await self._initial_workspace_sync(
                    machine_id, config.agent_name,
                    target_username=target_username,
                    target_role=target_role,
                    session_username=session_username,
                    progress_cb=_sync_progress,
                )
            except Exception as e:
                logger.warning(
                    "Initial workspace sync failed for session %s: %s",
                    session_id[:8], e,
                )
            try:
                sync_result = await mcp_sync.sync_mcps_for_session(
                    machine_id, session_id, list(assigned_mcps),
                    plan_cb=_on_plan, progress_cb=_on_progress,
                )
                if sync_result.excluded_names:
                    # Filter out failed MCPs from the config the CLI will read.
                    payload = self._strip_excluded_mcps_from_payload(
                        payload, execution_path, sync_result.excluded_names,
                    )
                    assigned_mcps -= sync_result.excluded_names
                    logger.warning(
                        "sync_mcps excluded %d MCP(s) from session %s: %s",
                        len(sync_result.excluded_names),
                        session_id[:8],
                        {
                            n: sync_result.failed.get(n, "install failed")
                            for n in sorted(sync_result.excluded_names)
                        },
                    )
                    for failed_name in sorted(sync_result.excluded_names):
                        # Memoized skips attempted nothing this session — a
                        # lone failure frame outside a started/done lifecycle
                        # would seed a ghost "installing" bar client-side.
                        # The exclusion itself (config strip) still applies.
                        if failed_name in sync_result.memoized:
                            continue
                        await install_registry.emit(machine_id, config.agent_name, {
                            "type": "mcp_install_failed",
                            "machine_id": machine_id,
                            "agent": config.agent_name,
                            "mcp": failed_name,
                            "error": sync_result.failed.get(failed_name, "install failed"),
                        })
                warmup_failures = sorted(sync_result.warmup_failed.keys())
                if warmup_failures:
                    logger.warning(
                        "session %s: %d MCP(s) failed the pre-warm boot check "
                        "(installed but didn't answer initialize): %s",
                        session_id[:8], len(warmup_failures),
                        {k: sync_result.warmup_failed[k] for k in warmup_failures},
                    )
                # Only close the lifecycle if we actually opened it (real work).
                # An empty diff emits nothing — no bar, no 100%-flicker.
                if install_started_flag["v"]:
                    await install_registry.emit(machine_id, config.agent_name, {
                        "type": "install_done",
                        "machine_id": machine_id,
                        "agent": config.agent_name,
                        "warmup_failures": warmup_failures,
                    })
            except Exception as e:
                # Missing MCP sync shouldn't block session start entirely — the
                # CLI will simply fail to launch those MCPs. Log and surface
                # as install_failed so the UI can show the error.
                logger.warning("mcp_sync_for_session failed: %s", e)
                await _emit_install_started_once()
                await install_registry.emit(machine_id, config.agent_name, {
                    "type": "install_failed",
                    "machine_id": machine_id,
                    "agent": config.agent_name,
                    "error": str(e),
                })
        finally:
            heartbeat_stop.set()
            try:
                await asyncio.wait_for(hb_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                hb_task.cancel()
            await install_registry.unregister(machine_id, config.agent_name)

        # Remote interactive: instead of the -p pump (event queue +
        # start_session + RemoteSessionInfo), register an InteractiveSession
        # backed by a RemotePtyProcess — it opens a PTY on the satellite
        # (pty_open) with the SAME `payload` and streams bytes both ways. ALL the
        # interactive intelligence stays on the proxy (dumb-pipe satellite). The
        # payload build + MCP/workspace sync above are shared with the -p path.
        if config.interactive:
            await self._start_interactive_remote(
                session_id, config, execution_path, payload, machine_id,
            )
            return

        # Create event queue
        queue = self._cm.create_session_queue(
            machine_id, session_id, execution_path
        )

        # Send start_session command to satellite
        try:
            await self._cm.send_command(machine_id, {
                "type": "start_session",
                "session_id": session_id,
                "agent_slug": config.agent_name,
                "execution_path": execution_path,
                "config": payload,
            }, timeout=60.0)
        except Exception:
            self._cm.remove_session_queue(machine_id, session_id)
            raise

        # Register session info
        info = RemoteSessionInfo(
            session_id=session_id,
            machine_id=machine_id,
            agent_name=config.agent_name,
            execution_path=execution_path,
            event_queue=queue,
            use_native_permissions=config.use_native_permissions,
            model=config.model,
            mode=config.permission_mode,
            used_mcps=set(assigned_mcps),
            fallback_reason=getattr(config, "fallback_reason", None),
        )
        if execution_path == "codex-cli":
            # Enable remote bg-sub-agent supervision only when the satellite is
            # new enough to forward bg-thread events past the main turn. On an
            # old satellite those events never arrive, so a supervisor would spin
            # to its 600 s ceiling and fire a spurious nudge — instead we leave
            # supervised_bg off and the translator sweeps bg subs at turn end
            # (today's behavior, no regression). Mirrors the LOCAL layer's
            # supervised_bg=True; the only difference is the version gate.
            bg_on = self._cm.satellite_supports_bg(machine_id)
            info.bg_supervised = bg_on
            info.codex_translator = CodexEventTranslator(
                model=config.model, supervised_bg=bg_on,
            )
            info.codex_thread_id = config.codex_thread_id
            if bg_on:
                # The router becomes the SOLE consumer of info.event_queue,
                # demuxing main-thread events to the active turn and bg-thread
                # events to per-thread buffers (see _route_remote_notifications).
                info.router_task = asyncio.create_task(
                    self._route_remote_notifications(info),
                    name=f"remote-codex-router-{session_id[:8]}",
                )

        self._sessions[session_id] = info

        # Set session state
        _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)
        if config.security_context:
            set_session_security(session_id, config.security_context)
        set_session_mode(session_id, config.permission_mode)
        self._bind_subscription(session_id, config, execution_path, payload)

        logger.info(
            "Remote session %s started on satellite %s (path=%s)",
            session_id[:8], machine_id[:8], execution_path,
        )

    @staticmethod
    def _bind_subscription(
        session_id: str, config: AgentConfig, execution_path: str, payload: dict,
    ) -> None:
        """Bind the acquired subscription + register the session's satellite
        credential file for rotation fan-out. Mirrors the local layers' bind at
        the end of ``start_session`` — without it a remote session leaked its
        pool seat (acquire incremented ``active_sessions``; release found no
        binding to decrement) and was invisible to the turn-start guard and
        the freshness fan-out."""
        if not config.subscription_id:
            return
        from services.engines.subscription_pool import (
            bind_session, credential_scope_key,
        )
        bind_session(
            session_id, config.subscription_id,
            layer=execution_path, user_sub=config.subscription_user_sub,
            scope_key=credential_scope_key(
                config.execution_target or "local",
                config.sandbox_host_claude_dir,
            ),
        )
        if execution_path == "codex-cli":
            kind = "codex"
            dir_relative = payload.get("codex_dir_relative", "")
            wrote_file = "auth_json" in payload
        else:
            kind = "claude"
            dir_relative = payload.get("claude_dir_relative", "")
            wrote_file = "credentials_json" in payload
        if not (wrote_file and dir_relative):
            return  # API-key session — no credential file to rewrite
        from services.engines import token_fanout
        token_fanout.register_session_target(
            session_id,
            token_fanout.CredentialFileTarget(
                kind=kind,
                machine_id=config.execution_target,
                agent_name=config.agent_name,
                dir_relative=dir_relative,
            ),
        )

    @staticmethod
    def _restore_adopted_credentials(
        session_id: str, machine_id: str, agent_name: str,
    ) -> None:
        """Sync (run via ``to_thread``) — the adopt-time counterpart of
        ``_bind_subscription``. Restores the in-memory subscription binding
        from its persisted row, re-registers the satellite credential file as
        a fan-out target, and pushes the CURRENT stored token to repair any
        rotation the restart window swallowed. Adopt is claude-cli-only, and
        ``dir_relative`` is recomputed from the persisted SecurityContext's
        ``mount_username`` with the same rule the spawn used. Best-effort:
        the adopted turn streams fine without it; only future rotations and
        seat accounting depend on this."""
        from core.session.session_state import get_session_security
        from services.engines import subscription_pool, token_fanout
        from storage import subscription_store
        try:
            sub_id = subscription_pool.restore_session_binding(session_id)
            if not sub_id:
                return
            cred = subscription_store.get_credential_data(sub_id) or {}
            oauth = cred.get("oauth_token") or {}
            if not (isinstance(oauth, dict) and oauth.get("accessToken")):
                return  # API-key subscription — no credential file to rewrite
            ctx = get_session_security(session_id)
            mount_username = getattr(ctx, "mount_username", "") if ctx else ""
            dir_relative = (
                f"users/{mount_username}/.claude" if mount_username
                else "workspace/.claude"
            )
            token_fanout.register_session_target(
                session_id,
                token_fanout.CredentialFileTarget(
                    kind="claude",
                    machine_id=machine_id,
                    agent_name=agent_name,
                    dir_relative=dir_relative,
                ),
            )
            subscription_pool.fan_out_current_token(sub_id)
        except Exception:
            logger.exception(
                "adopt %s: credential rebind failed (session streams on; "
                "future rotations may miss it until re-warm)", session_id[:8],
            )

    async def adopt_session(
        self, *, machine_id: str, session_id: str, agent_name: str,
        command_id: str, use_native_permissions: bool = False,
    ) -> AsyncIterator[CommonEvent]:
        """Re-adopt a CLI turn the satellite kept alive across a proxy restart
        (Mode C). Rebuilds a minimal RemoteSessionInfo (no spawn, no
        subscription bind), asks the satellite to replay the retained turn
        buffer, and drives ``_stream_cli_turn`` over it — the replayed
        `_resume_replay_begin` gates the consumer, `_seq` dedupes an overlap,
        and the buffered turn's sentinel/turn_ended closes it. A truncated
        replay injects a durable ⚠ block first."""
        # A larger queue: the replay arrives as one burst (session_event
        # dispatch drops on a full queue).
        queue = self._cm.create_session_queue(
            machine_id, session_id, "claude-code-cli", maxsize=4096,
        )
        translator = ClaudeCLIEventTranslator(session_id)
        settle = SettleController(session_id, 0, translator)
        info = RemoteSessionInfo(
            session_id=session_id,
            machine_id=machine_id,
            agent_name=agent_name,
            execution_path="claude-code-cli",
            event_queue=queue,
            cli_translator=translator,
            cli_settle=settle,
            use_native_permissions=use_native_permissions,
        )
        info.current_send_command_id = command_id
        self._sessions[session_id] = info
        reset_subagent_registry(session_id)
        reset_bg_command_registry(session_id)
        # Restore the runtime state a spawn would have created: last_active
        # (idle reaper + /v1/session/current), the in-memory subscription
        # binding, and the rotation fan-out target — all in-memory-only, so a
        # re-adopted session would otherwise miss every future token rotation
        # until its next full re-warm (the GH_TOKEN incident class).
        _record_session_use(session_id, client_type="", agent=agent_name)
        await asyncio.to_thread(
            self._restore_adopted_credentials, session_id, machine_id, agent_name,
        )

        # Ask the satellite to replay. Fire-and-forget: the replay arrives as
        # session_event frames on the queue we just created.
        try:
            await self._cm.send_fire_and_forget(machine_id, {
                "type": "resume_session_stream",
                "session_id": session_id,
            })
        except Exception as e:
            logger.warning(
                "adopt_session: resume_session_stream send failed for %s: %s",
                session_id[:8], e,
            )

        info.turn_active = True
        try:
            # Discard frames until the replay-begin marker (the satellite
            # pauses live forwarding for the replay, so [begin][replay][live]
            # is strictly ordered — no dedupe needed), then stream the turn
            # through the shared CLI loop.
            began = False
            deadline = time.monotonic() + 30.0
            while not began:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if time.monotonic() > deadline:
                        logger.warning(
                            "adopt_session %s: no replay-begin — giving up",
                            session_id[:8],
                        )
                        yield CommonEvent(type=DONE)
                        return
                    continue
                if isinstance(raw, dict) and raw.get("type") == "_resume_replay_begin":
                    began = True
                    if raw.get("truncated"):
                        yield CommonEvent(type=TEXT, data={
                            "content": "\n⚠ stream interrupted — earlier "
                                       "output was truncated during a platform "
                                       "restart.\n",
                        })
            async for event in self._stream_cli_turn(info):
                yield event
        finally:
            info.turn_active = False

    async def _start_interactive_remote(
        self,
        session_id: str,
        config: AgentConfig,
        execution_path: str,
        payload: dict,
        machine_id: str,
    ) -> None:
        """Remote interactive spawn: register an InteractiveSession whose
        PTY runs on the satellite (``pty_open``), reusing the already-built
        ``payload`` + the workspace/MCP sync from ``start_session``. The proxy
        keeps all interactive intelligence; the satellite is a dumb PTY pipe.
        No -p pump, no ``RemoteSessionInfo`` / event queue.
        """
        from core.session import interactive_session
        # Proxy-side identity BEFORE the PTY starts so the PreToolUse hook resolves
        # the moment the CLI launches (mirrors the -p path + the local CLI layer).
        _record_session_use(
            session_id, client_type=config.client_type, agent=config.agent_name,
        )
        if config.security_context:
            set_session_security(session_id, config.security_context)
        set_session_mode(session_id, config.permission_mode)
        ctx = config.security_context
        # Codex fresh delivers the first prompt via the launch argv (the satellite
        # appends it; the TUI auto-runs it after MCP warm), so there is NO cold
        # prompt to gate — start the session READY so the viewer's xterm bytes pass
        # straight through instead of being buffered + flushed late into the
        # composer (the remote "cursor bouncing" bug). Claude (+ Codex resume)
        # keep the readiness gate (their first prompt rides a PTY flush).
        prompt_in_argv = (
            execution_path == "codex-cli"
            and bool((getattr(config, "interactive_first_prompt", "") or "").strip())
        )
        await interactive_session.register_remote(
            session_id=session_id,
            chat_id=config.chat_id,
            agent_name=config.agent_name,
            machine_id=machine_id,
            execution_path=execution_path,
            config_payload=payload,
            user_sub=getattr(config, "user_sub", "") or "",
            role=(getattr(ctx, "role", "") or ""),
            username=(getattr(ctx, "username", "") or ""),
            transcript_kind=("codex" if execution_path == "codex-cli" else "claude"),
            prompt_in_argv=prompt_in_argv,
            tui_theme=getattr(config, "interactive_theme", "") or "dark",
        )
        # Subscription binding + fan-out target (InteractiveSession.close()
        # releases the seat, mirroring the local interactive branches).
        self._bind_subscription(session_id, config, execution_path, payload)
        logger.info(
            "Remote INTERACTIVE session %s started on satellite %s (path=%s)",
            session_id[:8], machine_id[:8], execution_path,
        )

    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        info = self._sessions.get(session_id)
        if not info:
            yield CommonEvent(type=ERROR, data={"message": "Remote session not found"})
            return

        info.last_activity = time.monotonic()

        # Barrier on in-flight dashboard-upload pushes for this agent:
        # ``/v1/upload`` returns before its satellite push completes
        # (``api/media/uploads.py``), so a prompt referencing a just-attached
        # file must wait for the bytes to land on the machine first. Bounded —
        # a wedged push logs and the turn proceeds (the pre-existing
        # best-effort failure mode). Returns immediately (no wait) when
        # nothing is in flight, i.e. the vast majority of turns.
        await upload_inflight.wait_settled(info.agent_name)

        inject_time = kwargs.get("inject_time", False)
        settle_after_result = kwargs.get("settle_after_result", 0)

        # Inject the time prefix on the proxy side so the satellite doesn't
        # need to know the user's TZ. Satellite is told inject_time=False —
        # we pre-inject here using the session's user_tz (or platform fallback).
        if inject_time:
            user_tz = get_session_user_tz(session_id)
            message = f"[Current time: {config.format_current_time(user_tz)}]\n\n{message}"
            inject_time = False

        # If this turn follows an abort, wait for the satellite's
        # `session_aborted` confirmation first so the dying CLI's flushed output
        # has fully arrived before we drain it (otherwise late stale events —
        # e.g. a buffered image — leak into this turn, and we'd race the
        # still-dying subprocess on resume). No-op for normal turns: no abort
        # event is armed, so this returns immediately.
        acked = await self._cm.wait_abort_acked(info.machine_id, session_id)
        if not acked and info.cli_dead:
            logger.warning(
                "Remote session %s: resume proceeding without abort ack "
                "(old satellite or lost message)", session_id[:8],
            )

        if info.bg_supervised:
            # The router owns info.event_queue and demuxes by thread, so there are
            # no main-thread stragglers to drain here (a prior turn's leftovers
            # either went to a bg buffer or the now-discarded prior consumer).
            # Register a fresh main-turn consumer for the router to feed.
            info.default_consumer = asyncio.Queue()
        else:
            # Drain any stale events left over from a previous aborted turn —
            # analog of PersistentSession._drain_stale_output on local. Log a
            # bounded type census (never payloads — frames can carry base64
            # bodies): a swallowed late answer from an orphaned prior turn is
            # otherwise invisible (queue events buffered satellite-side
            # between turns arrive AFTER this drain and are handled by the
            # foreign-skip gate instead).
            drained: Counter[str] = Counter()
            while True:
                try:
                    ev = info.event_queue.get_nowait()
                    drained[(ev or {}).get("type", "?") if isinstance(ev, dict) else "?"] += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                logger.warning(
                    "Remote session %s: drained %d stale events (types=%s, results=%d)",
                    session_id[:8], sum(drained.values()), dict(drained),
                    drained.get("result", 0),
                )

        # Fresh per-turn state for CLI. Codex translator persists across turns
        # (cumulative token tracking lives in it).
        if info.execution_path == "claude-code-cli":
            reset_subagent_registry(session_id)
            reset_bg_command_registry(session_id)
            info.cli_translator = ClaudeCLIEventTranslator(session_id)
            info.cli_settle = SettleController(
                session_id, settle_after_result, info.cli_translator,
            )

        # Pre-mint the command_id so we can correlate the satellite's
        # eventual ``turn_ended`` back to THIS send_message. A previous turn's
        # late turn_ended (delayed by the satellite-side file-scan running
        # past our 2s drain budget) carries the prior command_id and is
        # filtered out by _stream_cli_turn / _stream_codex_turn / _drain_*.
        # Without this, a stale turn_ended terminates the new turn with zero
        # events and the agent's response is never persisted.
        command_id = str(uuid.uuid4())
        info.current_send_command_id = command_id

        # Send message to satellite
        try:
            await self._cm.send_command(info.machine_id, {
                "type": "send_message",
                "session_id": session_id,
                "message": message,
                "execution_path": info.execution_path,
                "inject_time": inject_time,
            }, command_id=command_id)
        except Exception as e:
            # If the satellite reports there's no live session to take the
            # message, flag it so the dashboard's auto-resume path spawns a
            # fresh one (resumed from the on-disk transcript) on the next
            # attempt. Two shapes mean the same "no session on the satellite":
            #   - "CLI process not running": the session object survives but
            #     its subprocess died (crash / prior abort) — see
            #     session_manager.send_message.
            #   - "Session not found": the session object itself is gone from
            #     the satellite registry — the dominant case after a satellite
            #     WS drop/reconnect kills and drops in-flight sessions while
            #     the proxy still holds a stale, connected-looking info (so
            #     is_session_process_dead read False and let the send through).
            # Without mapping the latter to cli_dead the chat was bricked:
            # every resend hit the same dead id and re-raised "Session not
            # found", never triggering auto-resume. can_resume_session still
            # RPC-verifies the transcript before issuing --resume, so a truly
            # unresumable chat reseeds from history rather than resuming blind.
            err_text = str(e)
            if "CLI process not running" in err_text or "Session not found" in err_text:
                info.cli_dead = True
            yield CommonEvent(type=ERROR, data={"message": err_text})
            yield CommonEvent(type=DONE)
            return

        # Read events and translate. For CLI, SettleController decides the
        # turn-end timing (mirrors local PersistentSession.send_message).
        # For Codex, the Codex translator handles its own turn lifecycle.
        # ``turn_active`` guards the whole stream (cleared even when the
        # generator is abandoned) so the idle reaper can tell an in-flight
        # turn from a genuinely idle session.
        info.turn_active = True
        try:
            if info.execution_path == "claude-code-cli":
                async for event in self._stream_cli_turn(info):
                    yield event
            else:
                try:
                    async for event in self._stream_codex_turn(info):
                        yield event
                finally:
                    # At main-turn end, hand any still-running background
                    # sub-agents off to per-thread supervisors (mirrors the
                    # LOCAL session's send_message finally). No-op when bg
                    # supervision is off.
                    if info.bg_supervised:
                        self._handoff_remote_bg_subagents(info)
        finally:
            info.turn_active = False

    # --- Per-layer turn streaming ---

    async def _stream_cli_turn(
        self, info: "RemoteSessionInfo",
    ) -> AsyncIterator[CommonEvent]:
        """Stream one CLI turn to the pump using the shared translator + settle.

        Satellite is a dumb pipe: it forwards every NDJSON line as a
        session_event. This method owns turn-end decisions and sends
        ``stop_turn`` to the satellite when the turn is over.
        """
        translator = info.cli_translator
        settle = info.cli_settle
        assert translator and settle  # invariant: set in send_message

        expected_cmd_id = info.current_send_command_id
        # Foreign-result skip regime (see settle.ForeignSkipGate): resume
        # handshake / settlement results must not close the driven turn —
        # the valve, not a count, ends the regime.
        gate = ForeignSkipGate(info.session_id, translator)
        while True:
            timeout = gate.clamp_timeout(settle.effective_timeout())
            try:
                raw = await asyncio.wait_for(
                    info.event_queue.get(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                if gate.expired() and not settle.settling:
                    if self._cm.is_session_in_grace(
                        info.machine_id, info.session_id,
                    ):
                        # WS-drop grace: events are buffering satellite-side
                        # and Mode A may still deliver the turn — closing
                        # here would recreate the empty-turn/orphan pair.
                        # Bounded: grace expiry pushes error+None sentinels
                        # that close the turn regardless of the valve.
                        gate.re_arm()
                        continue
                    # Silence valve: nothing followed the skip regime — the
                    # mis-skipped result was probably legitimate after all
                    # (empty answer / idle CLI). Close the turn instead of
                    # hanging it forever.
                    logger.warning(
                        "Remote session %s: no events %.0fs after a skipped "
                        "foreign result — closing the turn",
                        info.session_id[:8], FOREIGN_SKIP_SILENCE_S,
                    )
                    await self._send_stop_turn(info)
                    await self._drain_until_turn_ended(
                        info, timeout=2.0,
                        expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                if not settle.settling:
                    # Pre-settle: long silence, keep waiting.
                    continue
                settle.maybe_log_heartbeat(proc_alive=True)
                if settle.should_exit_on_silence(timeout):
                    await self._send_stop_turn(info)
                    # Wait briefly for satellite to drain + ack turn_ended
                    await self._drain_until_turn_ended(
                        info, timeout=2.0,
                        expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                continue

            if raw is None:
                # Session ended on satellite (process exit) — terminal.
                yield CommonEvent(type=DONE)
                return

            # An event arrived — slide the post-skip valve (never disarm:
            # noise must not remove the skip regime's only closer).
            gate.note_event()

            rtype = raw.get("type", "")
            # Stale-turn filter: the satellite tags each CLI turn event with
            # the send_message command it was streamed under. A result/error
            # from a PREVIOUS turn (the replaced process's dying flush racing
            # past the send-start drain) must not terminate THIS turn.
            # Untagged events (older satellite) pass through; content-type
            # events pass regardless (a late bg task_updated frame from the
            # prior turn must still reach the registries).
            ev_cmd = raw.get("_command_id", "")
            if (ev_cmd and expected_cmd_id and ev_cmd != expected_cmd_id
                    and rtype in ("result", "error")):
                logger.warning(
                    "Remote session %s: dropping stale %s event from a "
                    "previous turn (expected=%s, got=%s)",
                    info.session_id[:8], rtype,
                    expected_cmd_id[:8], ev_cmd[:8],
                )
                continue
            if rtype == "error":
                yield CommonEvent(type=ERROR, data=raw)
                yield CommonEvent(type=DONE)
                return
            if rtype == "_turn_ended":
                # Filter stale turn_ended from a previous turn whose
                # satellite-side file-scan delivered late (past the proxy's
                # drain timeout). The matching command_id is set by
                # send_message before dispatch; an empty/non-matching id
                # means the marker is leftover and must be discarded so the
                # actual response of THIS turn can stream.
                ended_cmd_id = raw.get("command_id", "")
                if expected_cmd_id and ended_cmd_id and ended_cmd_id != expected_cmd_id:
                    logger.info(
                        "Remote session %s: discarding stale turn_ended "
                        "(expected=%s, got=%s)",
                        info.session_id[:8],
                        expected_cmd_id[:8], ended_cmd_id[:8],
                    )
                    continue
                yield CommonEvent(type=DONE)
                return

            # Feed the translator and forward all chunks. Content counts
            # only from events streamed under THIS command (untagged passes
            # for old satellites): content-typed stale events deliberately
            # bypass the stale filter above, and a dying prior process's
            # junk text must not flip the next zero-content result to
            # "real" and close the turn on it.
            info.last_activity = time.monotonic()
            for chunk in translator.feed(raw):
                if (chunk_is_content(chunk)
                        and (not ev_cmd or ev_cmd == expected_cmd_id)):
                    gate.note_content()
                for event in cli_chunk_to_events(chunk):
                    yield event

            # Result-event book-keeping — turn-end logic. A foreign result
            # (resume handshake / zero-content settlement mini-turn) never
            # ends the turn in EITHER mode: interactive must not close on
            # it, and task mode must not enter settle on it (the job-done
            # 5s fast-grace would close the turn just the same).
            if rtype == "result":
                if not settle.settling and is_foreign_result(
                    raw, gate.content_since_skip,
                ):
                    gate.record_skip(raw)
                    continue
                if settle.is_interactive_done():
                    # Interactive chat: no settle — tell satellite to stop.
                    await self._send_stop_turn(info)
                    await self._drain_until_turn_ended(
                        info, timeout=2.0,
                        expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                # Task settle mode: translator resets parsing state, keeps counters.
                settle.enter_settle()

    async def _stream_codex_turn(
        self, info: "RemoteSessionInfo",
    ) -> AsyncIterator[CommonEvent]:
        """Stream one Codex turn to the pump using the shared translator.

        When bg supervision is on (info.bg_supervised), the per-session router
        owns info.event_queue and feeds this turn's MAIN-thread events to
        info.default_consumer (bg sub-agent threads go to per-thread buffers);
        otherwise we read event_queue directly (today's behavior)."""
        expected_cmd_id = info.current_send_command_id
        turn_start = time.monotonic()
        # Plan mode: mirror the local layer's synthetic implement-card. Codex
        # delivers the plan as the turn's final agentMessage (no ExitPlanMode on
        # the -p path), so a completed plan-mode turn synthesizes a `plan_mode
        # exit` right before DONE — the SAME event the local codex layer emits,
        # rendered by the same PlanView card. `info.mode == "plan"` is the
        # read-only plan signal (set by change_mode / at session start).
        in_plan = info.mode == "plan"
        final_plan_msg = ""
        turn_interrupted = False

        def _plan_card() -> "CommonEvent | None":
            if in_plan and not turn_interrupted and final_plan_msg.strip():
                return CommonEvent(type=PLAN_MODE, data={
                    "action": "exit", "synthetic": True,
                    "tool_input": {"plan": final_plan_msg},
                })
            return None

        q = (
            info.default_consumer
            if (info.bg_supervised and info.default_consumer is not None)
            else info.event_queue
        )
        while True:
            try:
                raw = await asyncio.wait_for(q.get(), timeout=300.0)
            except asyncio.TimeoutError:
                # Tell the satellite to kill the orphaned codex turn process
                # (the CLI path sends stop_turn on its own timeout; codex had
                # no equivalent, leaking the process + its late output into the
                # next turn). The satellite's killpg now targets only the turn
                # group thanks to start_new_session.
                try:
                    await self._cm.send_fire_and_forget(info.machine_id, {
                        "type": "abort", "session_id": info.session_id,
                    })
                except Exception:
                    logger.warning(
                        "codex turn-timeout abort send failed for %s",
                        info.session_id[:8],
                    )
                yield CommonEvent(type=ERROR, data={"message": "Remote session timeout"})
                yield CommonEvent(type=DONE)
                return

            if raw is None:
                yield CommonEvent(type=DONE)
                return

            rtype = raw.get("type", "")
            if rtype == "error":
                yield CommonEvent(type=ERROR, data=raw)
                yield CommonEvent(type=DONE)
                return
            if rtype == "_turn_ended":
                # See _stream_cli_turn for the same stale-turn filter rationale.
                ended_cmd_id = raw.get("command_id", "")
                if expected_cmd_id and ended_cmd_id and ended_cmd_id != expected_cmd_id:
                    logger.info(
                        "Remote session %s: discarding stale turn_ended "
                        "(codex, expected=%s, got=%s)",
                        info.session_id[:8],
                        expected_cmd_id[:8], ended_cmd_id[:8],
                    )
                    continue
                card = _plan_card()
                if card:
                    yield card
                yield CommonEvent(type=DONE)
                return
            if rtype == "_codex_thread_id":
                info.codex_thread_id = raw.get("thread_id", "")
                from storage.database import update_chat, get_chat_by_session
                chat = get_chat_by_session(info.session_id)
                if chat:
                    update_chat(chat["id"], codex_thread_id=info.codex_thread_id)
                continue

            # Plan-mode capture (mirrors codex/layer.py): the last agentMessage
            # is the plan; a `turn/completed` interrupted status suppresses the
            # card. Read the raw notification before translation.
            if in_plan:
                _method = raw.get("method", "")
                if _method == "item/completed":
                    _item = (raw.get("params") or {}).get("item") or {}
                    if _item.get("type") == "agentMessage" and _item.get("text"):
                        final_plan_msg = _item["text"]
                elif _method == "turn/completed":
                    if ((raw.get("params") or {}).get("turn") or {}).get("status") == "interrupted":
                        turn_interrupted = True

            info.last_activity = time.monotonic()
            for event in self._translate_codex_event(info, raw):
                # Codex doesn't report turn duration — measure it proxy-side
                # and stamp it onto the METADATA event (mirrors codex/layer.py;
                # remote Codex turns were persisting duration_ms=0).
                if event.type == METADATA and "cost_usd" in event.data:
                    event.data["duration_ms"] = int(
                        (time.monotonic() - turn_start) * 1000
                    )
                if event.type == DONE:
                    card = _plan_card()
                    if card:
                        yield card
                yield event
                if event.type == DONE:
                    return

    async def _send_stop_turn(self, info: "RemoteSessionInfo") -> None:
        """Tell the satellite to exit its stdout read loop for this turn. If a
        backgrounded command is still pending, ask the satellite to keep draining
        + forwarding stdout post-turn (``drain_bg``) so the command's completion
        frame — which claude emits idle, AFTER this turn ends — still reaches the
        proxy's bg-command monitor (the satellite's per-turn forwarder otherwise
        stops here, stranding the badge + skipping the autonudge)."""
        from core.events.bg_command_state import get_bg_command_registry
        drain_bg = get_bg_command_registry(info.session_id).has_pending
        try:
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "stop_turn",
                "session_id": info.session_id,
                "drain_bg": drain_bg,
            })
        except Exception as e:
            logger.warning(
                "Remote stop_turn send failed for session %s: %s",
                info.session_id[:8], e,
            )

    async def _drain_until_turn_ended(
        self, info: "RemoteSessionInfo", *, timeout: float,
        expected_command_id: str = "",
    ) -> None:
        """After sending stop_turn, drain any remaining queue events until
        ``_turn_ended`` arrives or ``timeout`` expires.

        Prevents orphaned events from leaking into the next turn's queue
        and gives the satellite time to deliver any final in-flight events.

        When ``expected_command_id`` is provided, only a turn_ended carrying
        the same command_id terminates the drain — stale turn_ended markers
        from a PRIOR turn (whose late file-scan delivered after that turn's
        own drain budget) are silently discarded so they can't be confused
        with this turn's end.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(info.event_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            if raw is None:
                return
            if isinstance(raw, dict) and raw.get("type") == "_turn_ended":
                ended_cmd_id = raw.get("command_id", "")
                if expected_command_id and ended_cmd_id and ended_cmd_id != expected_command_id:
                    # Stale marker from a previous turn — keep draining.
                    continue
                return


    async def abort(self, session_id: str) -> bool:
        """Abort the in-flight turn — graceful-first for both engines.

        Graceful path (live turn, satellite ≥ 0.5.89):
        - claude-code-cli: send ``interrupt_turn`` — the satellite writes the
          same ``control_request {interrupt}`` frame the local layer uses into
          the CLI's stdin, the CLI closes the turn with a normal result event
          that flows back through the event stream, and the process + MCP
          sidecars SURVIVE for the next prompt (no re-warm).
        - codex-cli: send the regular ``abort`` frame — the satellite's codex
          twin handles it softly (``turn/interrupt``; the warm daemon
          survives) and its persistent forwarder keeps relaying events, so
          the daemon's terminal turn event reaches the kept-alive producer
          just like the LOCAL graceful codex abort (same app-server binary,
          same shared translator; 2026-07-09 live drill closed the turn in
          ~100ms). Codex reuses ``abort`` because the deployed satellites'
          ``interrupt_turn`` handler is CLI-only (requires a ``proc``); the
          ``session_aborted`` ack the satellite sends back no longer drains
          the event queue unless a HARD abort armed it (see
          satellite_connection), so it can't steal the closing turn's tail.

        Either way the producer/pump keep running to persist the partial
        turn, and a watchdog falls back to the hard path if the turn doesn't
        close.

        Hard path (no live turn, older satellite, send failure, or watchdog
        escalation): the satellite kills the CLI subprocess (its codex twin
        soft-interrupts the daemon turn instead), and the caller keeps the
        producer-cancel + cancelled-context injection.
        """
        info = self._sessions.get(session_id)
        if not info:
            return False
        if (
            info.turn_active
            and self._cm.satellite_supports_soft_interrupt(info.machine_id)
        ):
            # Same release as the hard path: a pending hook/native permission
            # waiter must not strand the turn we just asked to close.
            resolve_session_permissions(session_id, approved=False)
            frame = (
                "interrupt_turn"
                if info.execution_path == "claude-code-cli"
                else "abort"
            )
            try:
                await self._cm.send_fire_and_forget(info.machine_id, {
                    "type": frame,
                    "session_id": session_id,
                })
            except Exception as e:
                logger.warning(
                    "Remote soft interrupt send failed for session %s: %s — "
                    "falling back to hard abort", session_id[:8], e,
                )
            else:
                _wd = asyncio.create_task(
                    self._soft_interrupt_watchdog(
                        session_id, info.current_send_command_id,
                    ),
                    name=f"remote-interrupt-watchdog-{session_id[:8]}",
                )
                _remote_watchdog_tasks.add(_wd)
                _wd.add_done_callback(_remote_watchdog_tasks.discard)
                return True
        return await self._hard_abort(session_id)

    async def _hard_abort(self, session_id: str) -> bool:
        """Kill-path abort: the satellite tree-kills the CLI subprocess (and
        its codex twin marks the turn aborted before the flush), so the caller
        keeps today's producer-cancel + cancelled-context injection."""
        info = self._sessions.get(session_id)
        if not info:
            return False
        # If the session was held in reconnect-grace, drop it — the
        # user aborted, so a reconnect must NOT re-adopt + resume this turn.
        self._cm.drop_grace_session(info.machine_id, session_id)
        # Release any proxy-side permission waiter for this remote session
        # (the satellite Codex bridge / stdio gate block here via the tunnel) so
        # an abort denies it promptly instead of hanging the daemon's child.
        resolve_session_permissions(session_id, approved=False)
        # Arm the abort-ack event BEFORE sending so the satellite's
        # `session_aborted` reply (sent after the graceful kill + output flush)
        # can't be missed by a race. The auto-resume path awaits this so the
        # next turn starts only once the dying CLI is gone and its flushed
        # events have been drained.
        self._cm.arm_abort_acked(info.machine_id, session_id)
        # Fire-and-forget the abort command itself: we don't block the Stop
        # button on it (the dashboard gets `aborted` immediately); the
        # confirmation rides back asynchronously as `session_aborted`.
        try:
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "abort",
                "session_id": session_id,
            })
        except Exception as e:
            logger.warning("Remote abort send failed for session %s: %s",
                           session_id[:8], e)
        # CLI persistent subprocess on the satellite exits as part of the
        # abort. The dashboard's auto-resume path needs ``is_session_process_dead``
        # to return True on the next user message so it spawns a fresh
        # session — without this flag, the satellite would respond to the
        # next ``send_message`` with ``RuntimeError: CLI process not running``.
        # The codex daemon soft-interrupts its turn and stays alive, so this
        # flag only applies to claude-code-cli.
        if info.execution_path == "claude-code-cli":
            info.cli_dead = True
        return False

    async def _soft_interrupt_watchdog(
        self, session_id: str, armed_cmd_id: str,
    ) -> None:
        """Fall back to the hard abort when a soft interrupt fails to close
        the remote turn (dead CLI process, dropped frame, wedged stream).

        ``armed_cmd_id`` pins the watch to the interrupted turn so a slow
        watchdog can never kill a successor turn. On escalation the CLI
        history may have lost the partial turn — flip the chat's graceful
        flag back so the next turn re-injects the cancelled context (the ws
        abort site stamped graceful=True optimistically). Mirrors the local
        layer's ``_interrupt_watchdog``.
        """
        deadline = time.monotonic() + _REMOTE_INTERRUPT_WATCHDOG_S
        while time.monotonic() < deadline:
            info = self._sessions.get(session_id)
            if info is None or not info.alive:
                return
            if (not info.turn_active
                    or info.current_send_command_id != armed_cmd_id):
                return  # the interrupted turn closed gracefully
            await asyncio.sleep(0.25)
        info = self._sessions.get(session_id)
        if (info is None or not info.alive or not info.turn_active
                or info.current_send_command_id != armed_cmd_id):
            return
        logger.warning(
            "Remote session %s: soft interrupt did not close the turn within "
            "%.0fs — falling back to hard abort", session_id[:8],
            _REMOTE_INTERRUPT_WATCHDOG_S,
        )
        await self._hard_abort(session_id)
        try:
            from storage import database as task_store
            chat = task_store.get_chat_by_session(session_id)
            if chat and chat.get("last_turn_aborted"):
                task_store.update_chat(chat["id"], last_abort_graceful=False)
        except Exception:
            logger.exception(
                "Remote session %s: watchdog graceful-flag reset failed",
                session_id[:8],
            )

    def _extract_assigned_mcps(self, payload: dict, execution_path: str) -> set[str]:
        """Extract MCP names from the built start_session payload.

        CLI payloads carry the JSON mcpServers dict in `mcp_config`; Codex
        payloads embed them as TOML `[mcp_servers.*]` sections in
        `mcp_config_toml`. We return the *manifest* name for each active
        entry, which is what `mcp_sync` expects.
        """
        names: set[str] = set()
        if execution_path == "claude-code-cli":
            mcp_config = payload.get("mcp_config") or {}
            servers = mcp_config.get("mcpServers") or {}
            # Keys in mcpServers are `server_name` (from manifest) — map back
            # to `name` via registry.
            from services.mcp import mcp_registry
            server_to_name = {}
            for n, m in mcp_registry.get_all_manifests().items():
                server_to_name[m.server_name or m.name] = n
            for key in servers.keys():
                mapped = server_to_name.get(key, key)
                names.add(mapped)
        elif execution_path == "codex-cli":
            toml = payload.get("mcp_config_toml") or ""
            import re
            # [mcp_servers.<server_name>]
            for m in re.finditer(r"\[mcp_servers\.([A-Za-z0-9_\-]+)\]", toml):
                key = m.group(1)
                from services.mcp import mcp_registry
                server_to_name = {}
                for n, mf in mcp_registry.get_all_manifests().items():
                    server_to_name[mf.server_name or mf.name] = n
                names.add(server_to_name.get(key, key))
        return names

    def _strip_excluded_mcps_from_payload(
        self, payload: dict, execution_path: str, excluded: set[str],
    ) -> dict:
        """Return a payload with excluded MCPs removed from mcpServers.

        Used when mcp_sync couldn't install an MCP — we drop it from the
        session's config so the CLI doesn't error trying to spawn a
        non-existent stdio binary.
        """
        from services.mcp import mcp_registry
        server_to_name = {}
        for n, m in mcp_registry.get_all_manifests().items():
            server_to_name[m.server_name or m.name] = n

        if execution_path == "claude-code-cli":
            mcp_config = payload.get("mcp_config")
            if mcp_config and "mcpServers" in mcp_config:
                keep: dict = {}
                for key, val in mcp_config["mcpServers"].items():
                    manifest_name = server_to_name.get(key, key)
                    if manifest_name not in excluded:
                        keep[key] = val
                payload["mcp_config"] = {"mcpServers": keep}
        elif execution_path == "codex-cli":
            toml = payload.get("mcp_config_toml") or ""
            drop_keys = {
                key for key, name in server_to_name.items() if name in excluded
            }
            if drop_keys and toml:
                payload["mcp_config_toml"] = _strip_toml_mcp_sections(toml, drop_keys)
        return payload

    async def close_session(self, session_id: str) -> None:
        info = self._sessions.pop(session_id, None)
        if not info:
            return
        info.alive = False
        # Cancel the bg router + supervisors BEFORE removing the event queue
        # (the router is its sole consumer) so nothing is left waiting on an
        # orphaned queue, and resolve any still-pending bg sub-agents.
        if info.bg_supervised:
            await self._teardown_remote_bg(info)
        try:
            await self._cm.send_command(info.machine_id, {
                "type": "close_session",
                "session_id": session_id,
            }, timeout=15.0)
        except Exception as e:
            logger.warning("Remote close failed: %s", e)
        self._cm.remove_session_queue(info.machine_id, session_id)
        cleanup_session_permission_state(session_id)

        # Purge the remote-file-flow cache for this session (per-session dir
        # under AGENTS_DIR/.remote-host-cache/). Must come AFTER _sessions.pop so
        # is_remote_session() on any in-flight hook returns False.
        try:
            from core.remote import remote_file_flow
            remote_file_flow.cleanup_session(session_id)
        except Exception as e:
            logger.warning("remote_file_flow cleanup failed: %s", e)

        # Purge any per-session `outputs` subdirectories (e.g. camoufox
        # screenshots under workspace/.screenshots/{session_id}/).
        try:
            from core.session import session_state
            from services.mcp import mcp_output_relocation
            ctx = session_state.get_session_security(session_id)
            if ctx is not None:
                mcp_output_relocation.cleanup_session(
                    session_id, ctx.agent, ctx.username,
                )
        except Exception as e:
            logger.warning("mcp_output_relocation cleanup failed: %s", e)

        # Release subscription + concurrency slot
        from services.engines.subscription_pool import release_subscription
        release_subscription(session_id)
        from core.concurrency import release_chat_slot
        release_chat_slot(session_id)

    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        """Answer a permission prompt.

        Hook-based permissions (default): the hook script on the satellite
        is blocking on an HTTP call to the proxy; resolve_permission unblocks
        it locally — no satellite round-trip needed.

        Native CLI permissions (use_native_permissions=True): the CLI
        emitted a ``control_request.can_use_tool`` on its stdout and is
        waiting for a ``control_response`` on stdin. We forward it through
        the satellite.
        """
        # Hook-based path always works (harmless if no hook is waiting).
        resolve_permission(request_id, approved)

        # Native-permission path: forward control_response to satellite stdin.
        info = self._sessions.get(session_id)
        if info and info.use_native_permissions:
            try:
                await self._cm.send_fire_and_forget(info.machine_id, {
                    "type": "control_response",
                    "session_id": session_id,
                    "request_id": request_id,
                    "approved": approved,
                })
            except Exception as e:
                logger.warning(
                    "Remote native permission forward failed for session %s: %s",
                    session_id[:8], e,
                )

    async def change_model(self, session_id: str, model: str) -> None:
        info = self._sessions.get(session_id)
        if not info:
            return
        info.model = model
        if info.execution_path == "claude-code-cli":
            # Subtype must match what Claude Code CLI expects on its stdin
            # control channel (same as local CLIExecutionLayer.change_model).
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": "set_model",
                "kwargs": {"model": model},
            })
        # Codex: model change applies on next turn automatically

    async def change_mode(self, session_id: str, mode: str) -> None:
        info = self._sessions.get(session_id)
        if not info:
            return
        info.mode = mode
        set_session_mode(session_id, mode)
        if info.execution_path == "claude-code-cli":
            # Subtype must match what Claude Code CLI expects on its stdin
            # control channel (same as local CLIExecutionLayer.change_mode).
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": "set_permission_mode",
                "kwargs": {"mode": mode},
            })
        elif info.execution_path == "codex-cli":
            # Codex app-server gates at the sandbox boundary: the satellite
            # re-derives approvalPolicy from the sandbox mode + rebuilds the
            # per-turn sandboxPolicy, so the new escape behaviour takes effect on
            # the next turn. (The proxy-side gate verdict already follows the new
            # mode via set_session_mode above.)
            from core.layers.codex.helpers import permission_to_sandbox
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": "set_permission_mode",
                "kwargs": {"sandbox_mode": permission_to_sandbox(mode)},
            })

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        info = self._sessions.get(session_id)
        if not info:
            return {}
        if info.execution_path == "claude-code-cli":
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": subtype,
                "kwargs": kwargs,
            })
        return {}

    @property
    def capabilities(self) -> LayerCapabilities:
        # Remote layer inherits capabilities from the underlying execution path
        # The dashboard should check the agent's execution_path for specific capabilities
        return LayerCapabilities(
            name="remote",
            display_name="Remote Execution",
            supports_resume=True,
            supports_permissions=True,
            supports_plan_mode=True,
            supports_todos=True,
            supports_subagents=True,
            supports_control_commands=True,
            supports_mcps=True,
        )

    async def get_session(self, session_id: str):
        return self._sessions.get(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        info = self._sessions.get(session_id)
        if not info:
            return False
        # cli_dead → the persistent CLI subprocess crashed/was aborted but the
        # entry isn't reaped yet; report not-alive so callers don't reuse it.
        return (
            info.alive
            and not info.cli_dead
            and self._cm.is_connected(info.machine_id)
        )

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        info = self._sessions.get(session_id)
        if info:
            async with info.lock:
                yield
        else:
            yield

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        """Resolve background bash-command completions for a REMOTE CLI session
        between turns. The satellite forwards claude's stdout CONTINUOUSLY into
        the per-session ``event_queue`` (a persistent WS pipe), so post-turn
        ``system task_updated{patch.status:completed}`` frames just accumulate
        there. This pulls them with a short budget, resolving each via
        ``resolve_bg_command_frame`` (badge clear + registry). Returns True if any
        resolved. The post-turn bg-command monitor (stream_pump.py) polls this —
        bg bash has no completion hook, so this active read is the only signal.

        CLI-ONLY: a Codex session's ``event_queue`` is owned by the bg-subagent
        router (``_route_remote_notifications``); draining it here would steal its
        frames, and Codex has no background bash. Acquires the session lock with a
        short timeout so it never races an in-flight turn (the lock blocks the
        next turn's send_message, so we only ever see idle post-turn frames)."""
        info = self._sessions.get(session_id)
        if info is None or info.execution_path != "claude-code-cli":
            return False
        try:
            await asyncio.wait_for(info.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — retry next poll
        progressed = False
        try:
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(info.event_queue.get(), timeout=0.4)
                except asyncio.TimeoutError:
                    break  # no more buffered frames right now
                if not isinstance(raw, dict):
                    continue  # synthetic marker (_turn_ended, None) — ignore
                if resolve_bg_command_frame(session_id, raw):
                    progressed = True
        finally:
            info.lock.release()
        return progressed

    async def is_session_process_dead(self, session_id: str) -> bool:
        info = self._sessions.get(session_id)
        if not info:
            return True
        if info.cli_dead:
            return True
        return not self._cm.is_connected(info.machine_id)

    async def probe_session_process_dead(self, session_id: str) -> bool:
        """RPC the satellite for actual process liveness before a reap.

        The cached ``cli_dead`` flag reads False during a network stall —
        exactly when the stall-reap needs a real answer. Fails toward ALIVE
        on any uncertainty (old satellite, RPC timeout): a wrong "alive"
        just leaves the pump for the hard ceiling; a wrong "dead" kills a
        recoverable turn (the Mode D incident)."""
        info = self._sessions.get(session_id)
        if not info:
            return True
        if info.cli_dead:
            return True
        if not self._cm.is_connected(info.machine_id):
            # Unreachable satellite: not provably dead — grace/severance
            # handles this case; don't let the stall path reap on it.
            return False
        try:
            ack = await self._cm.send_command(info.machine_id, {
                "type": "check_session_process",
                "session_id": session_id,
            }, timeout=5.0)
            return not bool(ack.get("alive", True))
        except Exception:
            return False

    def session_idle_seconds(self, session_id: str) -> float | None:
        """Seconds since the last real event for a remote session. Lets the
        dashboard detect a wedged turn whose satellite event stream was severed
        (a reconnect orphaned the session queue, so the producer is parked on
        q.get() with no activity) and reap it instead of re-attaching a zombie
        pump. ``last_activity`` advances on every real event, so a healthy
        streaming turn never looks idle."""
        info = self._sessions.get(session_id)
        if not info:
            return None
        # While the session is held in reconnect-grace it is
        # 'reconnecting', not idle — return None so the staleness reap can't
        # fire on a turn that's waiting for its satellite to come back.
        if self._cm.is_session_in_grace(info.machine_id, session_id):
            return None
        return time.monotonic() - info.last_activity

    def remote_stream_severed(self, session_id: str) -> bool:
        """True when the session survives but its event queue is no longer
        attached to the current satellite connection (a reconnect built a fresh
        connection with an empty session_queues and did not re-register it), so
        an in-flight turn's producer is wedged on a dead queue."""
        info = self._sessions.get(session_id)
        if not info:
            return False
        return not self._cm.is_session_stream_attached(info.machine_id, session_id)

    async def steer(self, session_id: str, text: str) -> bool:
        """Mid-turn steering for a REMOTE headless Codex session — tunnel twin
        of the local layer's ``turn/steer``. Version-gated: an old satellite
        would silently drop the frame, so return False immediately and let the
        caller take today's queue fallback. ``steered`` in the ack is strict
        (True only on daemon accept) — the exactly-once contract the
        steer-vs-queue branch in dashboard_chat depends on."""
        info = self._sessions.get(session_id)
        if not info or info.execution_path != "codex-cli" or not text:
            return False
        if not self._cm.supports_codex_thread_ops(info.machine_id):
            logger.info(
                "remote steer unsupported by satellite %s (< 0.5.98) — "
                "falling back to queue", info.machine_id[:8],
            )
            return False
        try:
            ack = await self._cm.send_command(info.machine_id, {
                "type": "codex_steer",
                "session_id": session_id,
                "text": text,
            }, timeout=15.0)
        except RuntimeError as e:
            logger.warning("remote steer RPC failed for %s: %s", session_id[:8], e)
            return False
        return bool(ack.get("steered"))

    async def compact(self, session_id: str) -> dict | None:
        """Manual context compaction for a REMOTE headless Codex session —
        tunnel twin of the local layer's ``thread/compact/start`` flow. The
        satellite runs the whole wait loop (up to ~125s) and acks
        ``{ok, post_tokens|reason}``; version-gated like ``steer``."""
        info = self._sessions.get(session_id)
        if not info or info.execution_path != "codex-cli":
            return None
        if not self._cm.supports_codex_thread_ops(info.machine_id):
            logger.info(
                "remote compact unsupported by satellite %s (< 0.5.98)",
                info.machine_id[:8],
            )
            return None
        try:
            ack = await self._cm.send_command(info.machine_id, {
                "type": "codex_compact",
                "session_id": session_id,
            }, timeout=150.0)
        except RuntimeError as e:
            logger.warning(
                "remote compact RPC failed for %s: %s", session_id[:8], e,
            )
            return None
        if not ack.get("ok"):
            logger.info(
                "remote compact refused for %s: %s",
                session_id[:8], ack.get("reason", "unknown"),
            )
            return None
        return {"post_tokens": ack.get("post_tokens")}

    async def prepare_resume(self, session_id: str) -> None:
        # Remove dead session entry so a new start_session can be issued
        info = self._sessions.pop(session_id, None)
        if info is not None:
            # Drop any grace-held queue for this session (the reap
            # path) so a reconnect won't re-adopt a turn we're discarding.
            self._cm.drop_grace_session(info.machine_id, session_id)
        # Tear down the orphaned bg router/supervisors of the dead session so
        # they don't leak (the fresh session starts its own).
        if info and info.bg_supervised:
            await self._teardown_remote_bg(info)

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> bool:
        """Decide whether to issue ``start_session(resume=True)`` for a
        dead/lost remote session.

        The satellite owns the CLI session JSONL file
        (``~/<otodock>/agents/<slug>/users/<u>/.claude/projects/<hash>/
        <session_id>.jsonl``). After an ``abort()`` (or idle-reap) the
        subprocess exits but the file claude-code wrote during the turn
        remains on disk — ``--resume`` from a new subprocess picks it up.
        The old naive ``return True`` didn't actually verify the file
        existed and had conversation data, so callers issued ``--resume``
        against a missing/empty file. claude-code then silently fell back
        to creating a fresh session — chat memory evaporated.

        Two paths to find the machine to RPC:
        1. Fast: ``_sessions[session_id]`` holds the original ``machine_id``.
        2. Slow: when ``_sessions`` is empty (after ``prepare_resume``, after
           a reap, after a proxy restart), resolve the agent's current
           target via the same precedence the dashboard uses
           (``user_remote_targets`` → ``agent_remote_targets`` → ``local``).
           Required so the post-abort / post-reap auto-resume paths in
           ``ws/dashboard.py`` reach the right satellite.

        Codex remains in-memory: each turn is its own subprocess, the
        ``thread_id`` is what resume keys on, and Codex's own JSONL lives
        in ``.codex/sessions/`` which is checked by the Codex CLI itself
        when given the thread_id at spawn time.
        """
        machine_id = ""
        is_codex = False
        codex_thread_id = ""
        info = self._sessions.get(session_id)
        if info:
            is_codex = info.execution_path == "codex-cli"
            codex_thread_id = info.codex_thread_id
            machine_id = info.machine_id
        else:
            # Fallback (proxy restart / reap / prepare_resume popped the
            # info): the chat ROW still knows what this session was. This
            # must run BEFORE the connectivity/RPC path below — a remote
            # CODEX chat resumes by thread id (chats.codex_thread_id), and
            # sending its session id through check_session_resumable (which
            # stats a .claude JSONL that never existed for codex) refused
            # every remote codex resume after a proxy restart, silently
            # reseeding the chat from DB history.
            try:
                from storage.database import get_chat_by_session
                chat = get_chat_by_session(session_id)
            except Exception:
                chat = None
            if chat:
                is_codex = (chat.get("execution_path") or "") == "codex-cli"
                codex_thread_id = chat.get("codex_thread_id") or ""
                machine_id = chat.get("execution_target") or ""
            if not machine_id or machine_id == "local":
                # No chat row (or an unpinned/local one) — derive the target
                # from the agent's resolved execution target. Requires
                # agent_name + username so we can apply the per-user override
                # that ``resolve_execution_target`` checks first. Without
                # username we fall back to the agent's default — fine for
                # admin-paired agents but may pick the wrong machine for
                # user-paired sessions.
                if not agent_name:
                    return False
                try:
                    from storage import remote_store as _rs
                    from storage import database as _db
                    user_sub = (
                        _db.get_user_sub_by_username(username)
                        if username else None
                    )
                    target, _reason = _rs.resolve_execution_target(
                        agent_name, user_sub,
                    )
                except Exception as e:
                    logger.warning(
                        "can_resume_session: target resolve failed for agent %r "
                        "user %r: %s",
                        agent_name, username, e,
                    )
                    return False
                if not target or target == "local" or target.startswith("__offline__"):
                    # Not a remote target (or it's offline) — caller's layer
                    # routing should already have skipped this codepath, but
                    # be defensive.
                    return False
                machine_id = target

        if is_codex:
            return bool(codex_thread_id)
        if not self._cm.is_connected(machine_id):
            # Satellite unreachable — can't validate. Refuse to resume so
            # the dashboard takes the fresh-session branch instead of
            # claude-code silently materializing an empty session.
            return False
        try:
            ack = await self._cm.send_command(machine_id, {
                "type": "check_session_resumable",
                "session_id": session_id,
                "agent_slug": agent_name or (info.agent_name if info else ""),
                "username": username,
            }, timeout=5.0)
        except Exception as e:
            logger.warning(
                "check_session_resumable RPC failed for %s: %s — refusing resume",
                session_id[:8], e,
            )
            return False
        return bool(ack.get("resumable"))


    # --- Payload builders ---

    async def _build_start_payload(
        self, session_id: str, config: AgentConfig, execution_path: str,
    ) -> dict:
        """Build the config payload sent to the satellite for start_session."""
        import config as app_config
        from core.layers.codex.helpers import (
            map_effort_to_codex,
            permission_to_sandbox,
            build_auth_json_from_env,
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
            payload["agents_md_content"] = config.system_prompt
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
            # Sandbox mode is the same resolution the local layer uses
            payload["sandbox_mode"] = permission_to_sandbox(config.permission_mode)
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

    # --- Event translators ---

    def _translate_codex_event(
        self, info: RemoteSessionInfo, raw_event: dict,
    ) -> list[CommonEvent]:
        """Translate a forwarded Codex app-server notification to CommonEvents.

        The satellite forwards each ``codex app-server`` notification verbatim as
        ``{"method": ..., "params": ...}`` (the same NDJSON the local layer sees);
        the shared :class:`CodexEventTranslator` keys on the method. CLI events go
        through ``ClaudeCLIEventTranslator + cli_chunk_to_events`` in
        ``_stream_cli_turn`` — there is no equivalent helper here.
        """
        if not info.codex_translator:
            return []

        # Seed the multi-agent demux with the AUTHORITATIVE main thread id (the
        # satellite-reported `_codex_thread_id`) so a spawned sub-agent's thread
        # can't hijack `_main_thread_id` and suppress the MAIN agent's output.
        # Idempotent; mirrors the local layer + the satellite session-loop guard.
        if info.codex_thread_id:
            info.codex_translator._main_thread_id = info.codex_thread_id

        method = raw_event.get("method", "")
        codex_event = CodexEvent(type=method, data=raw_event.get("params") or {})
        return info.codex_translator.translate(codex_event)


# The MCP config-rewriting functions live in remote_mcp_rewrite.py; re-exported
# here so the staying payload builders call them by bare name and tests that
# import them from core.remote.remote_execution keep working. Tests that MONKEYPATCH the
# internally-called ones (_resolve_satellite_mcp_path_info / _rewrite_stdio_paths /
# _rewrite_env_for_remote) must patch core.remote.remote_mcp_rewrite instead.
from core.remote.remote_mcp_rewrite import (  # noqa: F401
    _REMOTE_MCP_STARTUP_FLOOR,
    _REMOTE_MCP_STARTUP_OVERRIDES,
    _strip_toml_mcp_sections,
    _resolve_satellite_mcp_path_info,
    _rewrite_mcp_json_for_remote,
    _rewrite_mcp_toml_for_remote,
    _rewrite_stdio_paths,
    _translate_venv_for_windows,
    _rewrite_env_for_remote,
)


# ---------------------------------------------------------------------------
# Remote session reaper
# ---------------------------------------------------------------------------

def _idle_session_has_pending_work(
    sid: str, now: float, idle_timeout: float,
) -> bool:
    """Between-turns leash for the idle reaper (mirrors the LOCAL reaper's
    hook-activity extension): a warm session whose subagent children are
    still running — e.g. a gracefully-aborted lane whose interrupt resolved
    the registry but not the child processes — must not be closed under them
    while hooks still fire. A silent subagent fires no hooks until its Stop —
    the registry-pending check is the leg that covers that window."""
    from core.session.session_state import (
        get_hook_activity, get_subagent_registry,
    )
    last_hook = get_hook_activity(sid)
    # Truthiness, not `is not None`: get_hook_activity returns 0 for a
    # session that never fired a hook, and `now - 0 <= idle_timeout` is
    # true for the first idle_timeout seconds after HOST boot (monotonic
    # starts near 0) — which suppressed reaping on freshly booted servers.
    # The local reaper (cli/session.py) guards the same way.
    if last_hook and now - last_hook <= idle_timeout:
        return True
    return get_subagent_registry(sid).has_pending


async def reap_idle_remote_sessions() -> None:
    """Background task: reap idle remote sessions periodically."""
    import config as app_config
    from core.session.session_manager import _get_remote_layer
    while True:
        await asyncio.sleep(60)
        # Fail parked runs whose satellite never reconnected (Mode C deadline).
        try:
            from services.scheduler import run_recovery
            await run_recovery.sweep_expired()
        except Exception:
            logger.exception("recovery sweep failed")
        try:
            layer = _get_remote_layer()
            now = time.monotonic()
            to_reap: list[str] = []

            idle_timeout = app_config.get_idle_timeout()
            for sid, info in list(layer._sessions.items()):
                idle = now - info.last_activity
                # Grace fix: a session held in reconnect-grace (a WS blip where
                # the satellite may return) reports not-connected but must NOT be
                # reaped — the grace machinery re-adopts it on reconnect. Per-
                # session grace for headless (vs. the interactive reaper's
                # per-machine is_pty_in_grace).
                connected = (layer._cm.is_connected(info.machine_id)
                             or layer._cm.is_session_in_grace(info.machine_id, sid))
                if info.turn_active and connected:
                    # Mid-turn event silence is not idleness: a network stall
                    # on the satellite box leaves the CLI alive and working
                    # with zero stream events (Mode D). Give in-flight turns
                    # the CLI turn ceiling, and inside it reap only on a
                    # probe-confirmed dead process.
                    if idle <= app_config.CLAUDE_TIMEOUT:
                        if (idle > idle_timeout
                                and await layer.probe_session_process_dead(sid)):
                            logger.warning(
                                f"Idle reaper: in-flight turn {sid[:8]} idle "
                                f"{idle:.0f}s with a dead process — reaping"
                            )
                            to_reap.append(sid)
                        continue
                if idle > idle_timeout or not connected:
                    # Between-turns leash, bounded by the CLI turn ceiling so
                    # a stuck subagent can't pin the session forever.
                    if (connected and idle <= app_config.CLAUDE_TIMEOUT
                            and _idle_session_has_pending_work(
                                sid, now, idle_timeout)):
                        continue
                    to_reap.append(sid)

            for sid in to_reap:
                logger.info(f"Reaping idle remote session {sid[:8]}")
                await layer.close_session(sid)
        except Exception as e:
            logger.error(f"Remote session reaper error: {e}")
