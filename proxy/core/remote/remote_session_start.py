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
from core.layers.codex.layer import CodexEventTranslator
from core.remote.remote_mcp_rewrite import _strip_toml_mcp_sections
from core.remote.remote_session_info import RemoteSessionInfo
from core.session.session_state import (
    _record_session_use, set_session_security, set_session_mode,
)

logger = logging.getLogger("remote-layer")

# start_session ack budget for a Codex session on a LOCAL model: the satellite
# acks after its pre-turn MCP warm gate, which waits for every server with a
# 90 s cap on a local model (a changed tool list re-prefills for minutes
# there), on top of the daemon spawn. Hosted sessions keep the 60 s budget.
_LOCAL_MODEL_START_TIMEOUT_S = 180.0


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


class RemoteSessionStartMixin:
    # --- ExecutionLayer interface: start ---

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

        # Determine the actual execution path from the config (set by config builders)
        from storage.agents import agent_store
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

        # Send start_session command to satellite. The satellite acks AFTER the
        # session started, which for Codex includes the pre-turn MCP warm gate;
        # a local-model session waits for every server (its gate cap is 90 s,
        # satellite ``_WARM_CAP_LOCAL_MODEL_S``), so its ack budget covers the
        # cap on top of the spawn.
        start_timeout = 60.0
        if execution_path == "codex-cli" and payload.get("local_model_provider"):
            start_timeout = _LOCAL_MODEL_START_TIMEOUT_S
        try:
            await self._cm.send_command(machine_id, {
                "type": "start_session",
                "session_id": session_id,
                "agent_slug": config.agent_name,
                "execution_path": execution_path,
                "config": payload,
            }, timeout=start_timeout)
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
                getattr(config.security_context, "target_allow_full_fs", False)
            ),
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
            # supervised_bg_commands rides the SAME >=0.5.18 gate: that version
            # means exactly "the forwarder streams past main-turn end", which is
            # all live cross-turn terminal completion needs — the router's OOB
            # hook resolves a between-turns item/completed; the 0.5.105-gated
            # codex_bg_terminals drain is only the loss-window backstop. Older
            # satellites keep the turn-end sweep (badge resolves at turn end).
            info.codex_translator = CodexEventTranslator(
                model=config.model, supervised_bg=bg_on,
                session_id=session_id, supervised_bg_commands=bg_on,
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
            app_config.SESSION_EVICT_FLOOR_S, app_config.get_idle_timeout(),
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
