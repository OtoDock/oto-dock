"""CLIExecutionLayer — wraps PersistentSession for Claude Code CLI subprocess.

Translates ClaudeStreamChunk → CommonEvent. Does NOT modify PersistentSession
(core/layers/cli/session.py) — wraps it.
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from core.events.common_events import (
    CommonEvent,
    TEXT, THINKING, TOOL_USE, TOOL_INPUT, TOOL_RESULT,
    PERMISSION_REQUEST, SUBAGENT_START, SUBAGENT_END, DELEGATE_SPAWN,
    BG_COMMAND_START, BG_COMMAND_END,
    WORKFLOW_START, WORKFLOW_PROGRESS, WORKFLOW_END,
    PLAN_MODE, SYSTEM, METADATA, DONE, ERROR,
    TODO_UPDATE, CONTEXT_COMPACT,
)
from core.execution_layer import ExecutionLayer, AgentConfig, LayerCapabilities
from core.layers.cli.helpers import ClaudeStreamChunk
from core.layers.cli.session import (
    PersistentSession,
    get_persistent_session, get_or_create_persistent_session,
    close_persistent_session,
    interrupt_persistent_session, _persistent_sessions,
)
import config as app_config
from core.session.session_state import (
    _record_session_use,
    set_session_security, set_session_mode,
    _sessions,
    resolve_permission,
    resolve_session_permissions,
)

logger = logging.getLogger("claude-proxy")

# How long the graceful interrupt gets to close the turn (result event →
# producer completion) before the watchdog falls back to killpg. The CLI
# normally reacts within a second or two; a wedged pipe / skipped foreign
# result never closes on its own.
_INTERRUPT_WATCHDOG_S = 8.0

# Strong refs — a bare create_task is GC-collectable mid-flight.
_watchdog_tasks: set[asyncio.Task] = set()


async def _interrupt_watchdog(session: PersistentSession, armed_seq: int) -> None:
    """Fall back to killpg when a graceful interrupt fails to close the turn.

    Polls the session's turn-active span; `armed_seq` pins the watch to the
    interrupted turn so a slow watchdog can never kill a successor turn. On
    fallback, the CLI history may have lost the partial turn after all — flip
    the chat's graceful flag back so the next turn re-injects the cancelled
    context (the ws abort site stamped graceful=True optimistically).
    """
    deadline = time.monotonic() + _INTERRUPT_WATCHDOG_S
    while time.monotonic() < deadline:
        if not session.is_alive:
            return
        if not session._turn_active or session._turn_seq != armed_seq:
            return  # the interrupted turn closed gracefully
        await asyncio.sleep(0.25)
    # Final re-check: a turn that closed exactly at the deadline must not be
    # punished with a killpg + a duplicated cancelled-context injection.
    if (not session.is_alive or not session._turn_active
            or session._turn_seq != armed_seq):
        return
    logger.warning(
        f"Session {session.session_id}: graceful interrupt did not close the "
        f"turn within {_INTERRUPT_WATCHDOG_S:.0f}s — killing process group"
    )
    await interrupt_persistent_session(session.session_id)
    try:
        from storage import database as task_store
        chat = task_store.get_chat_by_session(session.session_id)
        if chat and chat.get("last_turn_aborted"):
            task_store.update_chat(chat["id"], last_abort_graceful=False)
    except Exception:
        logger.exception(
            f"Session {session.session_id}: watchdog graceful-flag reset failed"
        )


# ---------------------------------------------------------------------------
# Credential dir writeback
# ---------------------------------------------------------------------------

from core.credentials.credential_writeback import writeback_credential_dirs as _writeback_credential_dirs


# ---------------------------------------------------------------------------
# ClaudeStreamChunk → CommonEvent translator
# ---------------------------------------------------------------------------

def cli_chunk_to_events(chunk: ClaudeStreamChunk) -> list[CommonEvent]:
    """Translate a single ClaudeStreamChunk into one or more CommonEvents.

    A chunk can carry both an event_type AND is_done/is_error flags,
    so we may yield multiple events from a single chunk.
    """
    events: list[CommonEvent] = []

    et = chunk.event_type

    # CLI-synthesized interrupt noise: on a graceful interrupt the CLI can
    # emit a "[ede_diagnostic] result_type=… stop_reason=…" marker — as
    # assistant text (invisible before N1 — the hard kill never persisted
    # partial turns), or as an is_error RESULT when the interrupt lands
    # mid-tool-use (stop_reason=tool_use; live-hit 2026-08-11: a duplex
    # barge-in during a web search rendered "Error: [ede_diagnostic] …" in
    # the chat). Never user-facing content; drop it on BOTH paths.
    _ede_noise = bool(chunk.text) and chunk.text.lstrip().startswith("[ede_diagnostic]")

    if et == "text" and chunk.text:
        if not _ede_noise:
            events.append(CommonEvent(type=TEXT, data={"content": chunk.text}))

    elif et == "thinking":
        events.append(CommonEvent(type=THINKING, data=chunk.event_data))

    elif et == "tool_start":
        events.append(CommonEvent(type=TOOL_USE, data=chunk.event_data))

    elif et == "tool_info":
        events.append(CommonEvent(type=TOOL_INPUT, data=chunk.event_data))
        # Emit TODO_UPDATE for TodoWrite tool (cross-layer abstraction)
        if chunk.event_data.get("name") == "TodoWrite":
            ti = chunk.event_data.get("tool_input") or {}
            if "todos" in ti:
                events.append(CommonEvent(type=TODO_UPDATE, data={"todos": ti["todos"]}))

    elif et == "todo_update":
        # Translator-maintained checklist snapshot (the TaskCreate/TaskUpdate
        # harness that replaced TodoWrite — see translator._cc_tasks).
        events.append(CommonEvent(type=TODO_UPDATE, data=chunk.event_data))

    elif et == "tool_end":
        events.append(CommonEvent(type=TOOL_RESULT, data=chunk.event_data))

    elif et == "task_spawn":
        events.append(CommonEvent(type=SUBAGENT_START, data=chunk.event_data))

    elif et == "subagent_end":
        events.append(CommonEvent(type=SUBAGENT_END, data=chunk.event_data))

    elif et == "bg_command_start":
        events.append(CommonEvent(type=BG_COMMAND_START, data=chunk.event_data))

    elif et == "bg_command_end":
        events.append(CommonEvent(type=BG_COMMAND_END, data=chunk.event_data))

    elif et == "workflow_started":
        events.append(CommonEvent(type=WORKFLOW_START, data=chunk.event_data))

    elif et == "workflow_progress":
        events.append(CommonEvent(type=WORKFLOW_PROGRESS, data=chunk.event_data))

    elif et == "workflow_ended":
        events.append(CommonEvent(type=WORKFLOW_END, data=chunk.event_data))

    elif et == "delegate_spawn":
        events.append(CommonEvent(type=DELEGATE_SPAWN, data=chunk.event_data))

    elif et == "plan_mode":
        events.append(CommonEvent(type=PLAN_MODE, data=chunk.event_data))

    elif et == "permission_prompt":
        events.append(CommonEvent(type=PERMISSION_REQUEST, data=chunk.event_data))

    elif et == "system":
        subtype = chunk.event_data.get("subtype", "")
        # Translate compaction system events to first-class CONTEXT_COMPACT
        if subtype == "compacting":
            events.append(CommonEvent(type=CONTEXT_COMPACT, data={
                "phase": "started", "trigger": "auto",
            }))
        elif subtype == "compact_boundary":
            meta = chunk.event_data.get("compact_metadata") or {}
            events.append(CommonEvent(type=CONTEXT_COMPACT, data={
                "phase": "completed",
                "trigger": meta.get("trigger", "auto"),
                "pre_tokens": meta.get("pre_tokens"),
                "post_tokens": None,
                "messages_summarized": meta.get("messages_summarized"),
            }))
        else:
            events.append(CommonEvent(type=SYSTEM, data=chunk.event_data))

    elif et == "metadata":
        events.append(CommonEvent(type=METADATA, data=chunk.event_data))

    # Error flag — can coexist with any event type
    if chunk.is_error and chunk.text and not _ede_noise:
        events.append(CommonEvent(type=ERROR, data={"message": chunk.text}))

    # Done flag — turn boundary, always last
    if chunk.is_done:
        events.append(CommonEvent(type=DONE, data={}))

    return events


# ---------------------------------------------------------------------------
# CLI layer capabilities
# ---------------------------------------------------------------------------

_CLI_CAPABILITIES = LayerCapabilities(
    name="claude-code-cli",
    display_name="Claude Code CLI",
    supports_resume=True,
    supports_permissions=True,
    supports_plan_mode=True,
    supports_todos=True,
    supports_subagents=True,
    supports_context_compression=True,
    supports_control_commands=True,
    supports_mcps=True,
    permission_modes=["default", "acceptEdits", "plan", "dontAsk"],
    control_commands=["set_model", "set_permission_mode"],
    models=app_config.get_layer_models("claude-code-cli"),
    effort_levels=["low", "medium", "high", "xhigh", "max"],
    effort_changeable_mid_session=False,
    compression_threshold_pct=83,
    mcp_delivery="external_config",
    mcp_config_format="json",
)


# ---------------------------------------------------------------------------
# CLIExecutionLayer
# ---------------------------------------------------------------------------

class CLIExecutionLayer(ExecutionLayer):
    """Execution layer wrapping the Claude Code CLI via PersistentSession.

    Delegates to the existing PersistentSession machinery
    (core/layers/cli/session.py) — does not modify it. The translator converts
    ClaudeStreamChunk → CommonEvent.
    """

    async def start_session(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        """Create or resume a persistent CLI session."""
        # Fail CLOSED: a local agent MUST run sandboxed + network-isolated. An
        # empty sandbox dir means the config builder omitted it — refuse rather
        # than launch un-sandboxed (the local CLI layer is never remote).
        if not config.sandbox_host_claude_dir:
            raise RuntimeError(
                f"refusing to start a local CLI session for agent "
                f"'{config.agent_name}' without a sandbox dir — local agents "
                f"must run sandboxed + network-isolated."
            )
        mcp_path = Path(config.mcp_config_path) if config.mcp_config_path else None

        # Build sandbox (every local CLI session is sandboxed — guarded above)
        extra_env = dict(config.extra_env) if config.extra_env else {}
        sandbox_builder = None

        from core.sandbox.sandbox import (
            SandboxBuilder, SandboxMount, cli_install_ro_binds,
            resolve_sandbox_config,
        )
        from services.mcp import mcp_registry as mcp_reg

        ctx = config.security_context

        # Resolve MCP sandbox mounts from assigned MCP manifests.
        # is_remote=False (fail-closed default, explicit here): this is the
        # LOCAL bwrap mount path, so satellite_only device MCPs must never
        # appear — they run native on the satellite.
        mcp_mounts: list[SandboxMount] = []
        assigned_mcps = mcp_reg.get_agent_mcps(config.agent_name, is_remote=False) or []
        for manifest in assigned_mcps:
            for m in getattr(manifest, "sandbox_mounts", []):
                # Resolve the ${mcp_dir} template in the host path. The host
                # is allowlisted to the agent / mcps tree in the sandbox
                # builder, so a platform-root template is not offered here.
                host = m.host.replace("${mcp_dir}", str(manifest.mcp_dir))
                mcp_mounts.append(SandboxMount(host=host, sandbox=m.sandbox, mode=m.mode))

        sandbox_cfg = resolve_sandbox_config(
            role=ctx.role if ctx else "viewer",
            username=ctx.mount_username if ctx else "",
            agent_name=config.agent_name,
            is_admin_agent=ctx.is_admin_agent if ctx else False,
            host_claude_dir=Path(config.sandbox_host_claude_dir),
            user_sub=config.user_sub,
            mcp_sandbox_mounts=mcp_mounts,
            # A claude installed outside the system mounts (user-prefix npm,
            # e.g. ~/.npm-global on a native install) must be mounted into the
            # sandbox or bwrap can't exec it — mirrors the Codex layer's
            # long-standing treatment of CODEX_BIN.
            extra_ro_binds=cli_install_ro_binds(app_config.CLAUDE_BIN),
            config_visible=ctx.config_visible if ctx else None,
            mount_shared=ctx.mount_shared if ctx else True,
            knowledge_rw=bool(getattr(ctx, "knowledge_rw", False)) if ctx else False,
            # The PRE-sandbox build file (sessions/) still carries host
            # mcps/ paths — scanned for the per-MCP dir binds.
            mcp_config_path=config.mcp_config_path,
        )
        sandbox_builder = SandboxBuilder(sandbox_cfg)

        # Get the sandbox-internal .claude/ path from env overrides
        sandbox_claude_dir = sandbox_builder.get_env_overrides().get(
            "CLAUDE_CONFIG_DIR", "/workspace/.claude"
        )

        # OAuth file delivery: write the session's .credentials.json into the
        # scope config dir (CLAUDE_CONFIG_DIR). The CLI re-reads this file
        # (mtime-watch + 401-recovery), which is what lets the pool rotate the
        # token under a LIVE process — env delivery is frozen at exec and
        # outranks the file, so the blob must never reach the child env (pop).
        creds_blob_json = extra_env.pop("_CLAUDE_CREDS_BLOB", "")
        if creds_blob_json:
            from services.engines.token_fanout import write_claude_credentials_file
            write_claude_credentials_file(
                Path(config.sandbox_host_claude_dir), json.loads(creds_blob_json),
            )

        # Copy MCP config into .claude/ dir (avoids mounting proxy/sessions/)
        if mcp_path:
            from core.sandbox.sandbox import prepare_mcp_config_for_sandbox
            sandbox_mcp_path = prepare_mcp_config_for_sandbox(
                mcp_path, config.sandbox_host_claude_dir,
                sandbox_config_dir=sandbox_claude_dir,
                session_id=session_id,
                secret_bundles=config.mcp_secret_bundles,
            )
            mcp_path = Path(sandbox_mcp_path) if sandbox_mcp_path else None

        # ssh-hosts (context-only MCP): provision the agent's authorized
        # SSH keys into <.claude>/ssh so the prompt's ready-to-run
        # `ssh -i "$OTO_SSH_KEY_DIR/…"` lines work from bash. Independent
        # of mcp_path — ssh-hosts emits no mcpServers entry, so it may be
        # the session's ONLY MCP with no config file at all.
        from core.sandbox.session_config_dir import (
            materialize_ssh_keys_for_sandbox,
        )
        if materialize_ssh_keys_for_sandbox(
            config.agent_name, config.sandbox_host_claude_dir,
        ):
            extra_env["OTO_SSH_KEY_DIR"] = f"{sandbox_claude_dir}/ssh"

        # If resume is requested and session was used before, set up _sessions
        # so get_or_create uses --resume
        if config.resume:
            if session_id not in _sessions or _sessions[session_id].get("message_count", 0) == 0:
                _sessions[session_id] = {"created": True, "message_count": 1}

        # Credential broker: populate the per-session secret store so
        # the wrapped stdio MCPs can fetch their secrets at spawn. Must precede
        # the CLI launch, which spawns the MCP children.
        from core.credentials import mcp_broker
        mcp_broker.provision(session_id, config.mcp_secret_bundles or {})

        if config.interactive:
            # Interactive mode: spawn the native Claude TUI under a PTY and
            # register it with core.session.interactive_session (which owns the lease +
            # drainer + idle lifecycle). PersistentSession is reused ONLY to
            # assemble the argv/env (interactive=True → no -p/stream-json + TERM,
            # see build_spawn_command); the session is not pump-driven. All the
            # sandbox/MCP/broker prep above + the identity block below are shared.
            from core.session import interactive_session
            ctx = config.security_context
            _builder = PersistentSession(
                session_id=session_id,
                agent_prompt=config.system_prompt,
                mcp_config_path=mcp_path,
                permission_mode=config.permission_mode,
                client_type=config.client_type,
                resume=config.resume,
                use_native_permissions=config.use_native_permissions,
                model=config.model,
                effort=config.effort,
                extra_env=extra_env or None,
                credential_env=config.credential_env or None,
                sandbox_builder=sandbox_builder,
                agent_name=config.agent_name,
                interactive=True,
            )
            argv, proc_env, cwd = _builder.build_spawn_command()
            # Seed the CLI config so the TUI launches past the first-run wizard
            # (theme-picker/login/trust) and authenticates via the env OAuth token
            # like headless. Local sessions have a sandbox
            # builder → CLAUDE_CONFIG_DIR maps to config.sandbox_host_claude_dir.
            if sandbox_builder and config.sandbox_host_claude_dir:
                from services.engines.cli_settings_manager import seed_interactive_cli_config
                seed_interactive_cli_config(
                    config.sandbox_host_claude_dir,
                    sandbox_builder.get_cwd(),
                    theme=config.interactive_theme or "dark",
                )
            await interactive_session.register(
                session_id=session_id,
                chat_id=config.chat_id,
                agent_name=config.agent_name,
                argv=argv,
                env=proc_env,
                cwd=cwd,
                user_sub=config.user_sub,
                role=(getattr(ctx, "role", "") or ""),
                username=(getattr(ctx, "username", "") or ""),
                target=config.execution_target or "local",
                tui_theme=config.interactive_theme or "dark",
                # Claude pre-fills the cold prompt but does NOT auto-submit it
                # (verified: `claude "<prompt>"` only pre-fills); interactive_session
                # fires ONE Enter once the composer settles.
            )
        else:
            await get_or_create_persistent_session(
                session_id=session_id,
                agent_prompt=config.system_prompt,
                mcp_config_path=mcp_path,
                permission_mode=config.permission_mode,
                client_type=config.client_type,
                allow_resume=config.resume,
                use_native_permissions=config.use_native_permissions,
                model=config.model,
                effort=config.effort,
                extra_env=extra_env or None,
                credential_env=config.credential_env or None,
                sandbox_builder=sandbox_builder,
                agent_name=config.agent_name,
            )

        set_session_mode(session_id, config.permission_mode)
        if config.security_context is not None:
            set_session_security(session_id, config.security_context)

        # Store host .claude/ dir for plan API and session resume
        if config.sandbox_host_claude_dir:
            from core.session.session_state import set_session_claude_dir
            set_session_claude_dir(session_id, config.sandbox_host_claude_dir)
        _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)

        # Bind subscription to session for pool cleanup on close
        if config.subscription_id:
            from services.engines.subscription_pool import (
                bind_session, credential_scope_key,
            )
            bind_session(
                session_id, config.subscription_id,
                layer="claude-code-cli", user_sub=config.subscription_user_sub,
                scope_key=credential_scope_key(
                    config.execution_target or "local",
                    config.sandbox_host_claude_dir,
                ),
            )
            # Rotation fan-out target — only sessions with a credential FILE
            # register (API-key sessions have nothing to rewrite).
            if creds_blob_json:
                from services.engines import token_fanout
                token_fanout.register_session_target(
                    session_id,
                    token_fanout.CredentialFileTarget(
                        kind="claude",
                        host_dir=config.sandbox_host_claude_dir,
                    ),
                )

    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """Send message and yield CommonEvents translated from CLI chunks.

        Kwargs:
            inject_time: bool — prepend current datetime to message
            settle_after_result: float — wait for background agents
        """
        session = await get_persistent_session(session_id)
        if not session:
            yield CommonEvent(type=ERROR, data={"message": "Session not found"})
            return

        inject_time = kwargs.get("inject_time", False)
        settle_after_result = kwargs.get("settle_after_result", 0)

        async for chunk in session.send_message(
            message,
            inject_time=inject_time,
            settle_after_result=settle_after_result,
        ):
            for event in cli_chunk_to_events(chunk):
                yield event

    async def abort(self, session_id: str) -> bool:
        """Abort the in-flight turn — graceful-first.

        Graceful path: write ``control_request {interrupt}`` into the live
        turn's stdin; the CLI closes the turn with a normal result event and
        keeps the partial turn in its own history, so the producer/pump must
        be left running (they persist the partial turn) and no cancelled-
        context injection is needed. A watchdog falls back to killpg if the
        turn doesn't close. Returns True on the graceful path, False when the
        process was killed (no live turn / dead pipe / dead process).
        """
        session = _persistent_sessions.get(session_id)
        if session is not None and session.is_alive and await session.interrupt_turn():
            # Same release as the hard path (interrupt_persistent_session):
            # a pending hook/native permission waiter must not strand the
            # turn we just asked to close.
            resolve_session_permissions(session_id, approved=False)
            _wd = asyncio.create_task(
                _interrupt_watchdog(session, session._turn_seq),
                name=f"interrupt-watchdog-{session_id[:8]}",
            )
            _watchdog_tasks.add(_wd)
            _wd.add_done_callback(_watchdog_tasks.discard)
            return True
        await interrupt_persistent_session(session_id)
        return False

    async def interrupt_for_queued(self, session_id: str) -> bool:
        """Stop-and-send: graceful-ONLY interrupt for a queued typed message.

        Same stdin ``control_request {interrupt}`` as ``abort``'s graceful
        path, but with NO killpg fallback and NO watchdog — if there is no
        live turn (or the pipe is dead) the message just waits for the
        natural turn end. The pending-permission release mirrors ``abort``:
        a turn parked on a hook permission cannot close otherwise, and a
        user typing instead of clicking the card is redirecting the agent.
        """
        session = _persistent_sessions.get(session_id)
        if session is None or not session.is_alive:
            return False
        if not await session.interrupt_turn():
            return False
        resolve_session_permissions(session_id, approved=False)
        return True

    async def close_session(self, session_id: str) -> None:
        """Close and remove the persistent session."""
        # Best-effort: writeback any credential_dir files to central location
        await _writeback_credential_dirs(session_id)

        await close_persistent_session(session_id)
        # Credential broker: drop this session's secrets (a cap token replayed
        # after close then finds nothing).
        from core.credentials import mcp_broker
        mcp_broker.purge_session(session_id)
        # Release subscription + concurrency slot
        from services.engines.subscription_pool import release_subscription
        release_subscription(session_id)
        from core.concurrency import release_chat_slot
        release_chat_slot(session_id)

    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        """Answer a permission prompt (hook-based or native)."""
        # Hook-based permissions use the global resolve_permission
        resolve_permission(request_id, approved)

        # Native CLI permissions use the control channel
        session = await get_persistent_session(session_id)
        if session and session.use_native_permissions:
            await session.send_control_response(request_id, approved)

    async def change_model(
        self, session_id: str, model: str,
    ) -> None:
        """Change model via CLI control channel."""
        session = await get_persistent_session(session_id)
        if session:
            async with session.lock:
                await session.send_control_request("set_model", model=model)

    async def change_mode(
        self, session_id: str, mode: str,
    ) -> None:
        """Change permission mode via CLI control channel."""
        session = await get_persistent_session(session_id)
        if session:
            async with session.lock:
                await session.send_control_request(
                    "set_permission_mode", mode=mode,
                )
        set_session_mode(session_id, mode)

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        """Send a control request to the CLI process."""
        session = await get_persistent_session(session_id)
        if not session:
            return {}
        return await session.send_control_request(subtype, **kwargs)

    # --- Capabilities ---

    @property
    def capabilities(self) -> LayerCapabilities:
        return _CLI_CAPABILITIES

    # --- Session access ---

    async def get_session(self, session_id: str) -> PersistentSession | None:
        """Return the underlying PersistentSession."""
        return await get_persistent_session(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        """Check if the CLI process is alive."""
        session = _persistent_sessions.get(session_id)
        if not session:
            return False
        return session.is_alive

    # --- Session lock + lifecycle ---

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        """Wrap PersistentSession.lock for multi-turn producers."""
        session = _persistent_sessions.get(session_id)
        if session:
            async with session.lock:
                yield
        else:
            # Session gone — yield no-op so caller's try/finally works
            yield

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        """Drain an idle CLI session's stdout to resolve background-command
        completions (badge clear + registry) and capture self-wake turns.
        Used post-turn by the bg monitors — bg bash has no completion hook,
        so this active read is the only signal. Safe under the shared
        PersistentSession lock (no double reader)."""
        session = _persistent_sessions.get(session_id)
        if session is None:
            return False
        return await session.drain_bg_commands(budget=budget)

    async def session_self_wakes(self, session_id: str) -> bool:
        """Claude ≥2.1.243 self-wakes after background completions — the bg
        monitors give that wake a grace window before nudging (codex layers
        have no such method → no grace)."""
        return session_id in _persistent_sessions

    async def is_session_process_dead(self, session_id: str) -> bool:
        """Check if session exists in pool but its CLI process has died."""
        session = _persistent_sessions.get(session_id)
        if not session:
            return False  # not in pool = not "dead" (it's absent)
        if session.proc and session.proc.returncode is not None:
            return True
        if not session.is_alive:
            return True
        return False

    async def prepare_resume(self, session_id: str) -> None:
        """Remove dead session from pool so start_session(resume=True) works."""
        _persistent_sessions.pop(session_id, None)

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> bool:
        """Check if session file has conversation data for --resume.

        CLI stores sessions at <CLAUDE_CONFIG_DIR>/projects/<project-hash>/<session_id>.jsonl.
        The file must contain at least one user message to be resumable.

        Checks the session's persistent .claude/ dir first.  After a proxy
        restart the in-memory mapping is lost, so *agent_name* + *username*
        are used to derive the .claude/ path (same logic as
        ``ensure_persistent_claude_dir``).  Falls back to ~/.claude/ for
        legacy pre-sandbox sessions.
        """
        from core.session.session_state import get_session_claude_dir

        # 1. In-memory mapping (works when proxy hasn't restarted)
        claude_dir = get_session_claude_dir(session_id)

        # 2. Restart fallback: derive from agent + username
        if not claude_dir and agent_name:
            agent_dir = app_config.get_agent_dir(agent_name)
            if username:
                candidate = agent_dir / "users" / username / ".claude"
            else:
                candidate = agent_dir / "workspace" / ".claude"
            if candidate.is_dir():
                claude_dir = str(candidate)

        if claude_dir:
            session_file = self._find_session_file(Path(claude_dir), session_id)
            if session_file:
                return self._session_file_has_user_message(session_file)

        # 3. Legacy fallback: ~/.claude/ path (pre-sandbox sessions)
        project_hash = str(app_config.AGENTS_DIR).replace("/", "-")
        session_file = Path.home() / ".claude" / "projects" / project_hash / f"{session_id}.jsonl"
        if session_file.exists():
            return self._session_file_has_user_message(session_file)

        return False

    @staticmethod
    def _find_session_file(claude_dir: Path, session_id: str) -> Path | None:
        """Find session JSONL file in a .claude/ dir.

        The project hash is derived from the CWD the CLI sees. For sandboxed
        sessions this is the sandbox-internal CWD (e.g., /users/alice/).
        We search all project dirs since the hash may vary.
        """
        projects_dir = claude_dir / "projects"
        if not projects_dir.is_dir():
            return None
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _session_file_has_user_message(session_file: Path) -> bool:
        """Check if a session JSONL file contains at least one user message."""
        try:
            content = session_file.read_text()
            return '"type":"user"' in content or '"type": "user"' in content
        except Exception:
            return False
