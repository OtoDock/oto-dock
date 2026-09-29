"""Remote session start: capacity, provisioning, sync, spawn (mixin).

``start_session`` for a satellite target: the capacity pre-check with idle
eviction, the credential and session-file brokers, the workspace sync and
MCP install lifecycle behind one install-registry bar, the ``start_session``
command (or the interactive PTY branch), the subscription bind. The
pairing-scope isolation helpers (``_machine_sync_role``,
``_collect_session_files``) live here with their caller. Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import logging
import time

from core.execution_layer import AgentConfig
from core import placement
from core.remote.remote_session_info import RemoteSessionInfo
from core.session import session_state as _state
from core.session.session_state import (
    _record_session_use, set_session_security, set_session_mode,
)
from auth.roles import row_role
from ws import wire_events as wire

logger = logging.getLogger("remote-layer")


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
    from storage import database as _db
    if not machine or placement.machine_is_admin_paired(machine):
        return session_role
    owner_sub = machine.get("registered_by", "") or ""
    # The owner's row alone — never their platform role (the docstring says why).
    return row_role(_db.get_user_agent_roles(owner_sub) if owner_sub else {}, agent_slug)


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
    from core.sandbox.session_config_dir import (
        collect_authorized_ssh_keys, session_takes_ssh_keys,
    )
    from services.oauth import credential_resolver

    admin_paired = placement.machine_is_admin_paired(machine)
    files: dict[str, mcp_broker.SessionFile] = {}

    if admin_paired and session_takes_ssh_keys(config.security_context):
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


class RemoteSessionStartMixin:
    # --- ExecutionLayer interface: start ---

    async def start_session(self, session_id: str, config: AgentConfig) -> None:
        machine_id = config.execution_target
        if placement.is_local(machine_id):
            raise RuntimeError("RemoteExecutionLayer called with local target")
        # A machine has no mount to mask the agent's CLI state with: below
        # the editor tier the session is refused as it is locally.
        from core.sandbox.session_config_dir import refuse_session_on_agent_state
        refuse_session_on_agent_state(config.security_context)

        if not self._cm.is_connected(machine_id):
            raise RuntimeError(
                f"Satellite {machine_id[:8]} is not connected"
            )

        # Per-satellite soft pre-check: reject early when the satellite
        # reports it's at its session ceiling (admin override or its own physical
        # recommendation). Best-effort + fail-open; the satellite's hard reject in
        # session_manager._check_capacity is the authoritative backstop.
        # At the ceiling, first try the LOCAL admit path's move: evict the
        # most-idle idle sessions this proxy tracks on the machine (never a
        # turn in flight) — an evicted session resumes with full context via
        # --resume exactly like an idle-reaped one, so a new session beats a
        # hard refusal (operator-requested parity with local eviction,
        # 2026-08-12).
        if self._cm.machine_at_capacity(machine_id):
            await self._evict_idle_on_machine(machine_id)
        if self._cm.machine_at_capacity(machine_id):
            raise RuntimeError(
                f"Remote machine {machine_id[:8]} is at capacity — too many active sessions"
            )

        # The engine this session runs: the config's (set by the builders from
        # the chat / dashboard selection), else the agent's default — the same
        # resolution every other spawn path uses.
        from core.session.session_manager import (
            capabilities_for_path, get_layer_by_path, resolve_execution_path,
        )
        execution_path = resolve_execution_path(config.agent_name, config.execution_path)
        _rcaps = capabilities_for_path(execution_path)
        if not _rcaps.runtime.supports_remote_execution:
            raise RuntimeError(f"{_rcaps.display_name} cannot run remotely")
        # The satellite must have said it can run this engine: no frame
        # reaches a machine whose code cannot dispatch it (a fielded satellite
        # that reports no ``engines`` runs the two it always has).
        engines = self._cm.satellite_engines(machine_id)
        if execution_path not in engines:
            raise RuntimeError(
                f"{_rcaps.display_name} is not available on "
                f"{self._cm.satellite_name(machine_id) or machine_id[:8]} — its satellite "
                f"({self._cm.satellite_version(machine_id) or 'unknown version'}) runs "
                f"{', '.join(sorted(engines)) or 'no engine'}. Update the satellite or "
                "run this agent on the server."
            )
        adapter = get_layer_by_path(execution_path).remote_adapter()
        if adapter is None:
            raise RuntimeError(f"{_rcaps.display_name} has no remote adapter")

        # Build layer-specific config payload for satellite
        plan = await self._build_start_payload(session_id, config, execution_path)
        payload = plan.payload

        # Credential broker: provision THIS session's per-MCP secrets so
        # the satellite's stdio interceptor can fetch them over the tunnel at MCP
        # spawn (the cap-token was injected into each stdio server's env by
        # _build_start_payload's rewrite). The store is in-memory on the proxy —
        # never sent to the satellite as a file. Idempotent — a no-op for
        # sessions with no secret bundles.
        from core.credentials import mcp_broker
        mcp_broker.provision(session_id, config.mcp_secret_bundles or {})

        # Derive the set of MCP names the engine will try to launch on the
        # satellite (its adapter reads its own config format). mcp_sync
        # reconciles this against what's already installed so missing /
        # out-of-date MCPs are shipped + installed before the CLI starts.
        assigned_mcps = adapter.mcp_names(payload)

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
        if machine and not placement.machine_is_admin_paired(machine):
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
                "type": wire.INSTALL_STARTED,
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
                                    "type": wire.INSTALL_HEARTBEAT,
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
                "type": wire.INSTALL_MCP_PLAN,
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
                    "type": wire.INSTALL_VERIFYING,
                    "machine_id": machine_id,
                    "agent": config.agent_name,
                    "message": ev.get("message", "Checking MCPs…"),
                })
                return
            await install_registry.emit(machine_id, config.agent_name, {
                "type": wire.INSTALL_PROGRESS,
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
                "type": wire.INSTALL_PROGRESS,
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
                    session_knowledge_rw=bool(getattr(
                        config.security_context, "knowledge_rw", False)),
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
                    payload = adapter.without_mcps(payload, sync_result.excluded_names)
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
                            "type": wire.MCP_INSTALL_FAILED,
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
                        "type": wire.INSTALL_DONE,
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
                    "type": wire.INSTALL_FAILED,
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
                first_prompt_via_argv=_rcaps.runtime.interactive_first_prompt_via_argv,
                credential_file_delivered=plan.credential_file_delivered,
            )
            return

        # Create event queue
        queue = self._cm.create_session_queue(
            machine_id, session_id, execution_path
        )

        # Send start_session command to satellite. The satellite acks AFTER the
        # session started; the engine's plan says how long that may take (a
        # Codex session on a local model waits out its MCP warm gate first).
        try:
            await self._cm.send_command(machine_id, {
                "type": "start_session",
                "session_id": session_id,
                "agent_slug": config.agent_name,
                "execution_path": execution_path,
                "config": payload,
            }, timeout=plan.start_timeout_s)
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
            allow_full_fs=bool(
                config.security_context is not None
                and config.security_context.placement.allow_full_fs
            ),
            model=config.model,
            mode=config.permission_mode,
            used_mcps=set(assigned_mcps),
            fallback_reason=getattr(config, "fallback_reason", None),
            resume_handle=config.resume_handle,
        )
        # The engine's own per-session state (a translator, a router that
        # consumes the event queue — whatever the engine keeps across turns).
        adapter.init_session(info, config, self._cm)

        self._sessions[session_id] = info

        # Set session state
        _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)
        if config.security_context:
            set_session_security(session_id, config.security_context)
        set_session_mode(session_id, config.permission_mode)
        self._bind_subscription(
            session_id, config, execution_path, plan.credential_file_delivered,
        )

        logger.info(
            "Remote session %s started on satellite %s (path=%s)",
            session_id[:8], machine_id[:8], execution_path,
        )

    @staticmethod
    def _bind_subscription(
        session_id: str, config: AgentConfig, execution_path: str,
        credential_file_delivered: bool,
    ) -> None:
        """Bind the acquired subscription + register the session's satellite
        credential file for rotation fan-out. Mirrors the local layers' bind at
        the end of ``start_session`` — without it a remote session leaked its
        pool seat (acquire incremented ``active_sessions``; release found no
        binding to decrement) and was invisible to the turn-start guard and
        the freshness fan-out. ``credential_file_delivered`` is the payload
        builder's word (``RemoteStartPlan``) that the engine wrote its login
        into the start payload — the file the satellite now holds."""
        if not config.subscription_id:
            return
        from services.engines.subscription_pool import (
            bind_session, credential_scope_key,
        )
        bind_session(
            session_id, config.subscription_id,
            layer=execution_path, user_sub=config.subscription_user_sub,
            scope_key=credential_scope_key(
                config.execution_target or placement.LOCAL,
                config.sandbox_host_claude_dir,
            ),
        )
        # The engine's credential-file declaration says where the file lives
        # on the satellite — the scope root the payload builder used + the
        # declared dirname, the same tree the satellite clamps the file to;
        # the builder said whether this session has one to rewrite.
        if not credential_file_delivered:
            return  # env-delivered credentials or an API-key session — nothing to rewrite
        from core.session.session_manager import capabilities_for_path
        spec = capabilities_for_path(execution_path).auth.credential_file
        if spec is None:
            return
        from core import layout
        from services.engines import token_fanout
        mount_username = (
            getattr(config.security_context, "mount_username", "")
            if config.security_context else ""
        )
        token_fanout.register_session_target(
            session_id,
            token_fanout.CredentialFileTarget(
                layer=execution_path,
                machine_id=config.execution_target,
                agent_name=config.agent_name,
                dir_relative=f"{layout.scope_root(mount_username)}/{spec.dirname}",
            ),
        )

    async def _evict_idle_on_machine(self, machine_id: str) -> int:
        """Capacity twin of the local ``_admit_with_eviction``: a satellite at
        its session ceiling closes its most-idle idle sessions (this proxy's
        view; never a turn in flight) until the cached count shows headroom
        or nothing evictable remains. Same floor as local —
        ``min(SESSION_EVICT_FLOOR_S, idle_timeout)`` — so a *just*-parked
        session isn't sacrificed. Headless sessions only in v1 (evicting a
        remote interactive PTY kills a terminal someone may be looking at).
        Best-effort: the satellite's hard capacity check remains the
        authoritative backstop."""
        import config as app_config
        floor_age = min(
            app_config.SESSION_EVICT_FLOOR_S, await _state.cached_idle_timeout(),
        )
        evicted = 0
        while self._cm.machine_at_capacity(machine_id):
            now = time.monotonic()
            candidates = [
                (now - i.last_activity, sid)
                for sid, i in list(self._sessions.items())
                if i.machine_id == machine_id and i.alive
                and not i.turn_active
                and (now - i.last_activity) >= floor_age
            ]
            if not candidates:
                break
            idle_s, victim = max(candidates)
            logger.info(
                "Remote eviction: closing idle session %s on %s (idle %.0fs) "
                "to make room for a new spawn", victim[:8], machine_id[:8],
                idle_s,
            )
            # close_session debits the cached heartbeat count, so the loop
            # condition observes the reclaimed headroom immediately.
            await self.close_session(victim)
            evicted += 1
        return evicted

    async def _start_interactive_remote(
        self,
        session_id: str,
        config: AgentConfig,
        execution_path: str,
        payload: dict,
        machine_id: str,
        *,
        first_prompt_via_argv: bool,
        credential_file_delivered: bool,
    ) -> None:
        """Remote interactive spawn: register an InteractiveSession whose
        PTY runs on the satellite (``pty_open``), reusing the already-built
        ``payload`` + the workspace/MCP sync from ``start_session``. The proxy
        keeps all interactive intelligence; the satellite is a dumb PTY pipe.
        No -p pump, no ``RemoteSessionInfo`` / event queue.
        ``first_prompt_via_argv`` is the engine's
        ``runtime.interactive_first_prompt_via_argv`` and
        ``credential_file_delivered`` the payload plan's word (both resolved
        by the caller).
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
            first_prompt_via_argv
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
            prompt_in_argv=prompt_in_argv,
            tui_theme=getattr(config, "interactive_theme", "") or "dark",
        )
        # Subscription binding + fan-out target (InteractiveSession.close()
        # releases the seat, mirroring the local interactive branches).
        self._bind_subscription(
            session_id, config, execution_path, credential_file_delivered,
        )
        logger.info(
            "Remote INTERACTIVE session %s started on satellite %s (path=%s)",
            session_id[:8], machine_id[:8], execution_path,
        )

