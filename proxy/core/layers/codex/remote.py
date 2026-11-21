"""OpenAI Codex on a satellite — this engine's half of the remote path.

``CodexRemoteAdapter`` implements ``RemoteEngineAdapter`` for the Codex
app-server: the start payload keys its satellite session classes read
(``satellite/sessions/codex_session.py``, ``satellite/terminal/codex_pty_session.py``
— the TOML MCP config, the thread id to resume, the sandbox and effort
mapping, the hook floor, the local-model provider, ``auth.json``), the
per-session router that is the sole consumer of the session's event queue
(demuxing the main thread from background sub-agent threads, exactly like
the LOCAL session's router over the daemon's notification queue), the turn
stream, the between-turns background-terminal reconcile, the ``abort`` /
``codex_steer`` / ``codex_compact`` / ``codex_bg_terminals`` frames, and the
thread-id resume answer. The remote layer (``core/remote``) keeps everything
shared and calls in here through the adapter; nothing in ``core/remote``
names this engine.

Import rule: the leaf and this package at module level; ``core.remote.**``
function-locally only (the resume-handle writer, the MCP config rewriters).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from core.background import spawn
from core.events import turn_ending, turn_life
from core.events.common_events import DONE, ERROR, METADATA, CommonEvent
from core.execution_layer import (
    AgentConfig,
    RemoteEngineAdapter,
    RemoteStartContext,
    RemoteStartPlan,
    ResumeVerdict,
)
from core.layers.cli.remote import (
    _exited_events,
    _lost_events,
    _memory_events,
    _satellite_error_events,
)
from core.layers.codex.events import _TOOL_ITEM_TYPES, CodexEvent
from core.layers.codex.helpers import (
    LOCAL_ENDPOINT_KEY_ENV,
    LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS,
    LOCAL_MODEL_START_TIMEOUT_S,
    codex_hooks_floor,
    map_effort_to_codex,
    permission_to_sandbox,
    will_retry,
    with_box_network_note,
    with_local_provider_note,
)
from core.layers.codex.layer import CodexCLIExecutionLayer
from core.layers.codex.local_model_catalog import (
    LOCAL_MODEL_ROWS_ENV,
    local_model_catalog_json,
    parse_local_model_rows,
)
from core.layers.codex.mcp_toml import with_tool_approvals
from core.layers.codex.plan_review import PLAN_ITEM, queue_plan_review
from core.layers.codex.translator import CodexEventTranslator
from core.permission_modes import PLAN_MODE as PLAN_MODE_WORD
from core.session import session_kind

if TYPE_CHECKING:
    from auth.path_policy import SecurityContext
    from core.remote.remote_session_info import RemoteSessionInfo

logger = logging.getLogger("remote-layer")

# The start-payload keys the satellite's CodexSession / CodexPtySession read —
# this engine's wire vocabulary, frozen with the satellites in the field.
CREDENTIALS_PAYLOAD_KEY = "auth_json"
CONFIG_DIR_PAYLOAD_KEY = "codex_dir_relative"
RESUME_HANDLE_PAYLOAD_KEY = "thread_id"
# The session's home in that dir (``SessionHomeSpec.to_dict``): present only
# for a satellite that makes Codex homes (the key Claude's start sends too).
SESSION_HOME_PAYLOAD_KEY = "session_home"
MCP_CONFIG_PAYLOAD_KEY = "mcp_config_toml"

# The satellite frames this engine's session answers to. ``abort`` IS the
# graceful interrupt for Codex: the satellite's Codex twin handles it softly
# (``turn/interrupt``, the warm daemon survives) and the deployed satellites'
# ``interrupt_turn`` handler requires a CLI ``proc``.
SOFT_INTERRUPT_FRAME = "abort"
CONTROL_REQUEST_FRAME = "control_request"   # set_model / set_permission_mode → per-turn override
STEER_FRAME = "codex_steer"                 # turn/steer into the running turn
COMPACT_FRAME = "codex_compact"             # thread/compact/start
BG_TERMINALS_FRAME = "codex_bg_terminals"   # thread/backgroundTerminals/list

# The local-endpoint variables the pool hands a Codex session: they never ride
# the satellite env in this form (the provider travels as a payload field, the
# key under the provider's own env name).
_ENDPOINT_URL_ENV = "_CODEX_ENDPOINT_URL"
_ENDPOINT_KEY_ENV = "_CODEX_LOCAL_API_KEY"
_ENDPOINT_PROVIDER_ENV = "_CODEX_ENDPOINT_PROVIDER"


# How long the turn loop waits for a notification before it checks the
# turn's life against its silence ceiling (turn_life).
_SILENCE_SLICE_S = 60.0


@dataclass
class CodexRemoteState:
    """Per-session state of a Codex session on a machine: the router's and
    the supervisor's fields, kept here because the proxy holds the
    translator and does the demux while the satellite stays a dumb pipe."""
    translator: CodexEventTranslator
    router_task: asyncio.Task | None = None
    default_consumer: asyncio.Queue | None = None      # the active main turn (router → here)
    thread_consumers: dict[str, asyncio.Queue] = field(default_factory=dict)  # sub thread → buffer
    bg_supervisors: dict[str, asyncio.Task] = field(default_factory=dict)     # sub thread → supervisor
    # Self-pacing for the bg-command drain: at most one codex_bg_terminals RPC
    # per second (the monitor retries every 0.3 s).
    last_bg_terminals_list: float = 0.0
    # The live turn's open tool items: its life while the stream is silent
    # (core/events/turn_life), read by the turn loop and by ``steer``.
    open_items: set = field(default_factory=set)


async def _reseed_after_revive(session_id: str) -> None:
    """The chat of a session whose revive opened a new Codex thread: its next
    turn restores the history (``history_seed.mark_resume_refused``), and the
    new thread holds none of the start's data (``start_context.reset``; a
    satellite without the start hook has no ``startup`` source to send it)."""
    from core.session import start_context
    from core.session.history_seed import mark_resume_refused
    from storage import database as task_store
    from storage.pg import run_db
    start_context.reset(session_id)
    chat = await run_db(task_store.get_chat_by_session, session_id)
    if chat:
        await run_db(mark_resume_refused, chat.get("id") or "")


def _silence_ends(info: RemoteSessionInfo, open_items: set) -> bool:
    """Whether the remote Codex turn's silence (since its own last stream
    event) ends it now, with its open tool items as its life."""
    return turn_life.ends(
        info.session_id, tools_open=bool(open_items),
        silent_for=time.monotonic() - info.last_event_at, task=info.task_turn,
    )


class CodexRemoteAdapter(RemoteEngineAdapter):
    private_env_keys = frozenset({
        CodexCLIExecutionLayer._AUTH_JSON_ENV,
        _ENDPOINT_URL_ENV, _ENDPOINT_KEY_ENV, _ENDPOINT_PROVIDER_ENV,
        LOCAL_MODEL_ROWS_ENV,
    })
    owns_event_queue = True

    def __init__(self, layer) -> None:
        self.layer = layer

    # --- spawn ---------------------------------------------------------------

    def _ask_for_home(self, payload: dict, config) -> None:
        """A home of its own in the scope's folder (``session_home_for``
        decided it for a machine that makes Codex homes): the engine's
        declaration, by which a paired machine makes it and a compute
        machine with its own copy makes its links."""
        spec = self.layer.capabilities.auth.session_home
        if config.session_home and spec is not None:
            payload[SESSION_HOME_PAYLOAD_KEY] = spec.to_dict()

    def start_payload(self, base: dict, ctx: RemoteStartContext) -> RemoteStartPlan:
        config, env, payload = ctx.config, ctx.env, base
        start_timeout_s = 60.0
        if ctx.spawn is not None:
            # A compute machine runs the plan's argv (a terminal's flags) or
            # the app-server (headless) against the ``config.toml``, the
            # session's own files and login file the platform's layer wrote in
            # the shared folder; it reads here what its thread start and its
            # warm gate need. The sandbox is the platform's render, so the
            # full-filesystem grant of a paired machine never applies.
            payload[CONFIG_DIR_PAYLOAD_KEY] = ctx.spawn.config_dir_relative
            payload[RESUME_HANDLE_PAYLOAD_KEY] = config.resume_handle
            payload["resume"] = config.resume
            floor = codex_hooks_floor(config.client_type, config.interactive)
            payload["codex_hooks_floor"] = floor
            payload["codex_attended_floor"] = not floor and not config.interactive
            payload["effort"] = map_effort_to_codex(config.effort, config.model)
            payload["sandbox_mode"] = permission_to_sandbox(config.permission_mode)
            payload["mcp_server_names"] = list(ctx.spawn.mcp_server_names)
            payload["local_model"] = ctx.spawn.local_model
            # The platform wrote the session's own files, never AGENTS.md:
            # the machine reads them, and only under this flag.
            payload["codex_session_text"] = True
            self._ask_for_home(payload, config)
            return RemoteStartPlan(
                payload=payload, credential_file_delivered=ctx.spawn.credential_file,
                start_timeout_s=LOCAL_MODEL_START_TIMEOUT_S if ctx.spawn.local_model else start_timeout_s,
            )
        payload[CONFIG_DIR_PAYLOAD_KEY] = (
            f"{ctx.scope_root}/{self.layer.capabilities.runtime.config_dir_name}"
        )
        payload[RESUME_HANDLE_PAYLOAD_KEY] = config.resume_handle
        self._ask_for_home(payload, config)

        # A local OpenAI-compatible endpoint (a ``local_endpoint`` subscription)
        # reaches the layer as private variables. The satellite writes its own
        # config.toml, so the provider travels as the ``local_model_provider``
        # payload field and the key as the child-env variable the provider's
        # ``env_key`` names, the same two artifacts the LOCAL layer produces
        # (``_write_config_toml``).
        local_endpoint = env.pop(_ENDPOINT_URL_ENV, "")
        local_api_key = env.pop(_ENDPOINT_KEY_ENV, "")
        local_provider = env.pop(_ENDPOINT_PROVIDER_ENV, "")
        local_model_rows = parse_local_model_rows(env.pop(LOCAL_MODEL_ROWS_ENV, ""))
        # ``less`` is a read, so it starts with no card, and Codex calls no
        # hook for what is then typed into it: its shell and editor escapes
        # are off on the machine as on the platform.
        env["LESSSECURE"] = "1"
        local_model_provider: dict | None = None
        if local_endpoint:
            # Dialed by the SATELLITE host exactly as configured: no loopback
            # rewrite (``loopback_if_host_self`` maps the PROXY host's own IPs
            # for the sandbox splice); a loopback URL here means the satellite's
            # own machine. There is no netns on a satellite, so no egress carve.
            # The stream idle timeout and the per-session model catalog (Ollama
            # only: Codex defers its MCP tools) are built here (the satellite
            # has no model registry) and written by its two Codex writers
            # (core/layers/codex/local_model_catalog).
            local_model_provider = {
                "base_url": local_endpoint,
                "env_key": LOCAL_ENDPOINT_KEY_ENV if local_api_key else "",
                "stream_idle_timeout_ms": LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS,
                "catalog_json": local_model_catalog_json(
                    config.model, local_provider, local_model_rows,
                ),
            }
            if local_api_key:
                env[LOCAL_ENDPOINT_KEY_ENV] = local_api_key

        # The PreToolUse permission FLOOR for unattended sessions — the same
        # rule as the spawn plan's (helpers.codex_hooks_floor). The satellite
        # writes `[features] hooks = true`, trusts the hook per thread
        # (bypass_hook_trust) and sets the deny-only / no-forward hook env.
        # Under `approvalPolicy: never` the approval bridge never fires, so
        # without this a remote task / meeting / phone / trigger session is
        # gated by the prompt rules only.
        floor = codex_hooks_floor(config.client_type, config.interactive)
        payload["codex_hooks_floor"] = floor
        # An attended app-server chat runs the same hook on every thread,
        # judging every call while the bridge takes what crosses the
        # sandbox: the local session's rule (session.py).
        payload["codex_attended_floor"] = not floor and not config.interactive
        if local_model_provider:
            payload["local_model_provider"] = local_model_provider
            # Tell a local model the truth about its MCP tools for its
            # server (one prompt string, both fields).
            # On the payload's own text: an older satellite's terminal
            # carries the start's data appended to it.
            payload["system_prompt"] = with_local_provider_note(
                payload["system_prompt"], local_provider,
            )
            # The satellite acks after the local-model MCP warm gate.
            start_timeout_s = LOCAL_MODEL_START_TIMEOUT_S
        # What a Codex session reads about its box's network (helpers): one
        # prompt string, both fields.
        payload["system_prompt"] = with_box_network_note(payload["system_prompt"])
        payload["agents_md_content"] = payload["system_prompt"]
        # The session's text is its own (its thread's developerInstructions,
        # the terminal's profile, the SessionStart hook): a satellite that
        # reads the flag writes no AGENTS.md; an older one ignores it and
        # writes AGENTS.md from the two keys above, as before.
        payload["codex_session_text"] = True
        # Remote Codex interactive: the satellite builds the `codex` argv, so
        # it needs the resume signal + the cold first prompt (a FRESH codex TUI
        # auto-runs the prompt passed as a positional arg; resume rides the PTY
        # flush). Harmless for the -p app-server path (CodexSession ignores
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
                config.security_context is not None
                and cast("SecurityContext", config.security_context).placement.allow_full_fs
            ),
        )
        # The sign-in's auth.json, a ChatGPT login or an OpenAI key (popped
        # from env; the satellite writes it verbatim into the session's
        # CODEX_HOME).
        delivered = False
        auth_json = self.layer.credential_file_from_env(env)
        if auth_json is not None:
            payload[CREDENTIALS_PAYLOAD_KEY] = auth_json
            delivered = True

        # Read and rewrite the TOML MCP config for the satellite.
        #
        # Credential env vars are NOT injected here — they were already baked
        # into the TOML on disk by ``mcp_registry.inject_credential_env_into_toml``
        # during ``config_builder``'s build step. A second injection pass on
        # the serialized TOML string would splice keys in via regex, blind to
        # what is already there — it appended every credential key a second
        # time, producing duplicate keys in each MCP's ``env = { ... }`` inline
        # table, which TOML forbids and Codex's parser rejected whole. Read the
        # file as-is — same as local Codex does.
        if config.mcp_config_path:
            try:
                mcp_path = Path(config.mcp_config_path)
                if mcp_path.exists():
                    from core.remote.remote_mcp_rewrite import (
                        _rewrite_mcp_toml_for_remote,
                    )
                    payload[MCP_CONFIG_PAYLOAD_KEY] = _rewrite_mcp_toml_for_remote(
                        mcp_path.read_text(), ctx.sat_port,
                        target_os=ctx.target_os, session_id=ctx.session_id,
                        proxy_api_key=ctx.proxy_api_key,
                        secret_bundle_keys=ctx.secret_bundle_keys,
                        gateway=ctx.gateway,
                    )
            except Exception:
                logger.exception("Codex MCP TOML build failed")
        # The functions each server keeps (an outside session's picks, every
        # session's limits): written into the shipped TOML as the local spawn
        # writes them, so the machine's Codex offers the same list.
        if payload.get(MCP_CONFIG_PAYLOAD_KEY) and config.security_context is not None:
            from core.layers.codex.mcp_toml import _inject_enabled_tools_toml
            from services.mcp import tool_limits
            grants = tool_limits.engine_pairs(config.security_context)
            if grants:
                payload[MCP_CONFIG_PAYLOAD_KEY] = _inject_enabled_tools_toml(
                    payload[MCP_CONFIG_PAYLOAD_KEY], grants)
        # Codex's tool approvals (layer.with_tool_approvals), headless only:
        # a terminal's own prompt is its gate.
        if payload.get(MCP_CONFIG_PAYLOAD_KEY) and not config.interactive:
            payload[MCP_CONFIG_PAYLOAD_KEY] = with_tool_approvals(payload[MCP_CONFIG_PAYLOAD_KEY])
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
        if (config.client_type == session_kind.DASHBOARD.name and not config.interactive
                and payload.get(MCP_CONFIG_PAYLOAD_KEY)):
            payload[MCP_CONFIG_PAYLOAD_KEY] = (
                "[features]\ndefault_mode_request_user_input = true\n\n"
                + payload[MCP_CONFIG_PAYLOAD_KEY]
            )
        return RemoteStartPlan(
            payload=payload, credential_file_delivered=delivered,
            start_timeout_s=start_timeout_s,
        )

    def mcp_names(self, payload: dict) -> set[str]:
        """The TOML's ``[mcp_servers.<server_name>]`` sections, mapped back to
        manifest names (what the satellite MCP sync expects)."""
        import re
        toml = payload.get(MCP_CONFIG_PAYLOAD_KEY) or ""
        keys = re.findall(r"\[mcp_servers\.([A-Za-z0-9_\-]+)\]", toml)
        if not keys:
            return set()
        server_to_name = _server_to_manifest_name()
        return {server_to_name.get(key, key) for key in keys}

    # The session's text, built from the prompt (an older satellite writes
    # it to AGENTS.md).
    prompt_payload_keys = ("system_prompt", "agents_md_content")

    def without_mcps(self, payload: dict, excluded: set[str]) -> dict:
        toml = payload.get(MCP_CONFIG_PAYLOAD_KEY) or ""
        drop_keys = {
            key for key, name in _server_to_manifest_name().items() if name in excluded
        }
        if drop_keys and toml:
            from core.remote.remote_mcp_rewrite import _strip_toml_mcp_sections
            payload[MCP_CONFIG_PAYLOAD_KEY] = _strip_toml_mcp_sections(toml, drop_keys)
        return payload

    # --- per-session state ---------------------------------------------------

    def init_session(self, info: RemoteSessionInfo, config: AgentConfig, cm) -> None:
        """One translator for the session's life (cumulative token deltas
        live in it; the thread id is emitted once) and the router — the SOLE
        consumer of ``info.event_queue`` — demuxing main-thread events to the
        active turn and each background sub-agent thread to its own buffer.
        Every
        satellite that can connect forwards bg-thread events past the main
        turn (that landed in 0.5.18, far below ``MIN_SATELLITE_VERSION``)."""
        self._build_state(info, config.model)

    def adopt_state(self, info: RemoteSessionInfo, cm) -> None:
        """An idle session the satellite kept across a proxy restart: the
        same state a start builds, on the model and the thread the adoption
        put on ``info``. What ran in the old process's background is not
        known to the new state (its registries died with that process). The
        session's mode and model go to the satellite again: its sandbox
        follows the mode and the machine's file policy as they are now, not
        at spawn, and the model is the one the adoption chose. The MCP server
        keys the satellite reported become manifest names, as a start's are."""
        server_to_name = _server_to_manifest_name()
        info.used_mcps = {server_to_name.get(key, key) for key in info.used_mcps}
        self._build_state(info, info.model)
        if info.turn_active:
            # A replayed turn (0.5.171): the router feeds the main thread's
            # notifications to this consumer, as ``begin_turn`` registers one
            # for a live turn; an idle adoption registers none.
            cast("CodexRemoteState", info.engine_state).default_consumer = asyncio.Queue()
        if info.mode:
            spawn(self.control_request(
                info, cm, "set_permission_mode", mode=info.mode),
                  name="control_request", owner="remote")
        if info.model:
            spawn(self.control_request(
                info, cm, "set_model", model=info.model), name="control_request", owner="remote")

    def _build_state(self, info: RemoteSessionInfo, model: str) -> None:
        state = CodexRemoteState(translator=CodexEventTranslator(
            model=model, supervised_bg=True,
            session_id=info.session_id, supervised_bg_commands=True,
        ))
        info.engine_state = state
        state.router_task = asyncio.create_task(
            self._route_notifications(info),
            name=f"remote-codex-router-{info.session_id[:8]}",
        )

    async def close_state(self, info: RemoteSessionInfo) -> None:
        """Cancel the router + every bg supervisor and resolve their registry
        entries (so close / offline can't leave the _bg_agent_monitor waiting on
        a dead sub-agent). Idempotent. Mirrors the LOCAL _teardown_bg."""
        # A compute session's own files the platform wrote in the shared
        # folder (a paired computer's satellite removes its own).
        from core.layers.codex import session_text
        session_text.forget(info.session_id)
        state = cast("CodexRemoteState | None", info.engine_state)
        if state is None:
            return
        if state.router_task is not None:
            state.router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state.router_task
            state.router_task = None
        sups = list(state.bg_supervisors.items())
        for _sub_tid, task in sups:
            task.cancel()
        for sub_tid, task in sups:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            # A cancelled supervisor's own finally already resolved it; this is
            # the idempotent backstop for one that never reached its finally.
            self._resolve_bg_subagent(info, sub_tid)
        # Background terminals died with their satellite session (thread close
        # kills unified_exec PTYs, and a resumed thread knows nothing of old
        # processIds) — resolve their registry entries + badges as killed.
        # Mirrors the LOCAL _teardown_bg sweep; covers close AND prepare_resume.
        from core.session.session_state import resolve_bg_command
        pending_cmds: list[dict] = []
        with contextlib.suppress(Exception):
            pending_cmds = state.translator.pending_bg_commands()
        for cmd in pending_cmds:
            resolve_bg_command(info.session_id, cmd["item_id"], "killed")
            state.translator._bg_commands.pop(cmd["item_id"], None)
        state.thread_consumers.clear()
        state.default_consumer = None

    # --- turns ---------------------------------------------------------------

    async def begin_turn(self, info: RemoteSessionInfo, settle_after_result: int) -> None:
        # Per-turn bg-command registry prune. reset() PRESERVES still-pending terminals; without it
        # `unsurfaced` never clears remotely and task_producer's cohort math
        # fires spurious review turns.
        from core.events.bg_command_state import reset_bg_command_registry
        reset_bg_command_registry(info.session_id)
        # The router owns info.event_queue and demuxes by thread, so there are
        # no main-thread stragglers to drain (a prior turn's leftovers either
        # went to a bg buffer or the now-discarded prior consumer). Register a
        # fresh main-turn consumer for the router to feed.
        state = cast("CodexRemoteState", info.engine_state)
        state.default_consumer = asyncio.Queue()

    async def stream_turn(self, info: RemoteSessionInfo, cm) -> AsyncIterator[CommonEvent]:
        """Stream one Codex turn to the pump using the shared translator: the
        router feeds this turn's MAIN-thread events to the consumer
        ``begin_turn`` registered; at turn end the still-running background
        sub-agents are handed to per-thread supervisors."""
        try:
            async for event in self._stream_main_turn(info, cm):
                yield event
        finally:
            # At main-turn end, hand any still-running background sub-agents
            # off to per-thread supervisors (mirrors the LOCAL session's
            # send_message finally).
            self._handoff_bg_subagents(info)

    async def _stream_main_turn(self, info: RemoteSessionInfo, cm) -> AsyncIterator[CommonEvent]:
        state = cast("CodexRemoteState", info.engine_state)
        expected_cmd_id = info.current_send_command_id
        turn_start = time.monotonic()
        # Plan mode: mirror the local layer — a completed plan turn's `plan`
        # item becomes a plan review right before DONE (layer.queue_plan_review;
        # no plan file on the machine: the reviewed rows are a remote session's
        # plans); a turn with none ends as a plain turn. `info.mode == "plan"`
        # is the read-only plan signal (set by change_mode / at session start).
        in_plan = info.mode == PLAN_MODE_WORD
        plan_text = ""
        turn_interrupted = False

        # Emit the resume handle once → the pump persists chats.codex_thread_id
        # (the frozen METADATA key) so the thread resumes after a proxy
        # restart. The SAME event the local layer yields (codex/layer.py);
        # the translator emits it once per session, and a turn that runs
        # before the satellite's frame arrived leaves it for the next one.
        for ev in state.translator.thread_id_metadata(info.resume_handle):
            yield ev

        async def _plan_review() -> None:
            plan = plan_text.strip()
            if in_plan and not turn_interrupted and plan:
                await queue_plan_review(info.session_id, plan)

        q = state.default_consumer
        assert q is not None  # invariant: begin_turn registered the consumer
        open_items = state.open_items
        open_items.clear()
        completed = False
        await state.translator.read_turn_rates()
        while True:
            try:
                raw = await asyncio.wait_for(q.get(), timeout=_SILENCE_SLICE_S)
            except asyncio.TimeoutError:
                # Silence: the turn keeps its life (an open tool item, a
                # prompt on a person, background work, a recent hook) or ends
                # typed past the ceiling. The satellite kills the orphaned
                # codex turn process (its killpg targets only the turn
                # group), so its late output never leaks into the next turn.
                # During the reconnect grace the turn may still arrive.
                silent_for = time.monotonic() - info.last_event_at
                if (cm.is_session_in_grace(info.machine_id, info.session_id)
                        or not _silence_ends(info, open_items)):
                    continue
                logger.warning(
                    "Remote session %s: no notification for %.0fs with nothing running — "
                    "ending the turn (codex)", info.session_id[:8], silent_for,
                )
                await cm.send_fire_and_forget(info.machine_id, {
                    "type": "abort", "session_id": info.session_id,
                })
                ending = turn_ending.TurnEnding(
                    reason=turn_ending.SILENT, detail=f"no output for {int(silent_for)} s",
                )
                yield CommonEvent(type=ERROR, data={"message": ending.line(),
                                                    "ending": ending.as_dict()})
                yield CommonEvent(type=DONE)
                return

            if raw is None:
                if info.proxy_killed:
                    yield CommonEvent(type=DONE)
                    return
                info.cli_dead = True   # the next send re-warms the session
                for event in _lost_events("the machine ended the session mid-turn"):
                    yield event
                return

            rtype = raw.get("type", "")
            if rtype == "error":
                for event in _satellite_error_events(raw):
                    yield event
                return
            if rtype == "_turn_ended":
                # A turn_ended from a PREVIOUS turn (the satellite's late
                # file-scan delivered past the proxy's drain budget) must not
                # end THIS turn — discard on a command-id mismatch.
                ended_cmd_id = raw.get("command_id", "")
                if expected_cmd_id and ended_cmd_id and ended_cmd_id != expected_cmd_id:
                    logger.info(
                        "Remote session %s: discarding stale turn_ended "
                        "(codex, expected=%s, got=%s)",
                        info.session_id[:8], expected_cmd_id[:8], ended_cmd_id[:8],
                    )
                    continue
                if raw.get("reason") == "memory" and not info.proxy_killed:
                    # The machine's memory guard ended the engine.
                    info.cli_dead = True
                    for event in _memory_events():
                        yield event
                    return
                if raw.get("reason") == "exited" and not info.proxy_killed:
                    # The machine says the daemon exited inside the turn.
                    info.cli_dead = True
                    for event in _exited_events(raw):
                        yield event
                    return
                if raw.get("reason") == "interrupted":
                    # The daemon did not take the interrupt this side sent, so
                    # the machine dropped it and ended the turn: stopped, as
                    # asked. Its next turn re-warms on the same thread.
                    yield CommonEvent(type=DONE)
                    return
                if not completed and not info.proxy_killed:
                    # The satellite closed the turn before turn/completed:
                    # its process went mid-turn.
                    info.cli_dead = True
                    for event in _lost_events("the machine ended the turn before its result"):
                        yield event
                    return
                await _plan_review()
                yield CommonEvent(type=DONE)
                return

            _m = raw.get("method", "")
            if _m == "turn/completed":
                completed = True
            elif _m in ("item/started", "item/completed"):
                _item = (raw.get("params") or {}).get("item") or {}
                _iid = _item.get("id") or ""
                if _iid and _item.get("type") in _TOOL_ITEM_TYPES:
                    (open_items.add if _m == "item/started" else open_items.discard)(_iid)

            # Plan-mode capture (mirrors codex/layer.py): the `plan` item is the
            # plan; a `turn/completed` interrupted status suppresses the
            # review. Read the raw notification before translation.
            if in_plan:
                _method = raw.get("method", "")
                if _method == "item/completed":
                    _item = (raw.get("params") or {}).get("item") or {}
                    if _item.get("type") == PLAN_ITEM and _item.get("text"):
                        plan_text = _item["text"]
                elif _method == "turn/completed":
                    if ((raw.get("params") or {}).get("turn") or {}).get("status") == "interrupted":
                        turn_interrupted = True

            info.last_activity = info.last_event_at = time.monotonic()
            for event in self._translate(info, raw):
                # Codex doesn't report turn duration — measure it proxy-side
                # and stamp it onto the METADATA event (mirrors codex/layer.py;
                # remote Codex turns were persisting duration_ms=0).
                if event.type == METADATA and "cost_usd" in event.data:
                    event.data["duration_ms"] = int((time.monotonic() - turn_start) * 1000)
                if event.type == DONE:
                    await _plan_review()
                    # A revive on a new thread announced it during this turn.
                    for ev in state.translator.thread_id_metadata(info.resume_handle):
                        yield ev
                yield event
                if event.type == DONE:
                    return

    async def drain_bg_commands(self, info: RemoteSessionInfo, cm, *, budget: float) -> bool:
        """Between turns, reconcile pending background-terminal registry entries
        against the satellite's ``codex_bg_terminals`` RPC (which proxies
        ``thread/backgroundTerminals/list``) and resolve any whose PTY is gone.
        Matching is by itemId OR the late-bound processId, the contract is
        fail-closed (RPC failure / satellite refusal is NO progress, never
        resolve-on-error), with >=1s self-pacing and a 0.1s lock timeout. NEVER touches
        info.event_queue: the router owns it."""
        from core.events.bg_command_state import get_bg_command_registry
        from core.session.session_state import resolve_bg_command
        state = cast("CodexRemoteState | None", info.engine_state)
        if state is None:
            return False
        reg = get_bg_command_registry(info.session_id)
        if not reg.has_pending:
            return False
        if not info.alive or info.cli_dead:
            return False
        if time.monotonic() - state.last_bg_terminals_list < 1.0:
            return False
        try:
            await asyncio.wait_for(info.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — its translator resolves these
        try:
            state.last_bg_terminals_list = time.monotonic()
            try:
                ack = await cm.send_command(info.machine_id, {
                    "type": BG_TERMINALS_FRAME,
                    "session_id": info.session_id,
                }, timeout=min(budget, 5.0))
            except RuntimeError:
                return False  # satellite unreachable/slow — nothing resolved
            if not ack.get("ok"):
                return False  # satellite couldn't list — fail-closed
            rows = [t for t in (ack.get("terminals") or []) if isinstance(t, dict)]
            live_items = {t.get("itemId") for t in rows}
            live_pids = {t.get("processId") for t in rows}
            pid_by_item = {
                p["item_id"]: p["process_id"]
                for p in state.translator.pending_bg_commands()
            }
            progressed = False
            for iid in reg.spawned - reg.completed:
                pid = pid_by_item.get(iid)
                if iid in live_items or (pid and pid in live_pids):
                    continue  # still running — its exit resolves it later
                if resolve_bg_command(info.session_id, iid, "completed"):
                    progressed = True
                state.translator._bg_commands.pop(iid, None)
            return progressed
        finally:
            info.lock.release()

    # --- control -------------------------------------------------------------

    def soft_interrupt_frame(self) -> str:
        return SOFT_INTERRUPT_FRAME

    async def control_request(self, info: RemoteSessionInfo, cm, subtype: str, **kwargs) -> bool:
        """The app-server has no stdin control channel; the satellite's
        ``CodexSession.send_control_request`` applies ``set_model`` and
        ``set_permission_mode`` as per-turn overrides on the next
        ``turn/start``. The mode travels as the SANDBOX mode and as the
        platform mode: the satellite re-derives approvalPolicy and the plan
        collaboration mode from them and rebuilds the per-turn sandboxPolicy
        (the proxy-side gate verdict already follows the new mode via
        set_session_mode)."""
        if subtype == "set_model":
            frame_kwargs = {"model": kwargs.get("model", "")}
            # The machine applies it at its next turn/start: that turn's price
            # is read from here (``read_turn_rates``).
            state = info.engine_state
            if isinstance(state, CodexRemoteState) and frame_kwargs["model"]:
                state.translator._model = frame_kwargs["model"]
        elif subtype == "set_permission_mode":
            mode = kwargs.get("mode", "")
            frame_kwargs = {
                "sandbox_mode": permission_to_sandbox(mode, allow_full_fs=info.allow_full_fs),
                "permission_mode": mode,
            }
        else:
            return False  # no other control subtype has a Codex meaning
        await cm.send_fire_and_forget(info.machine_id, {
            "type": CONTROL_REQUEST_FRAME,
            "session_id": info.session_id,
            "subtype": subtype,
            "kwargs": frame_kwargs,
        })
        return True

    async def steer(self, info: RemoteSessionInfo, cm, text: str) -> bool:
        """Mid-turn steering: the machine's ``turn/steer``.
        ``steered`` in the ack is strict (True only on daemon accept): the
        exactly-once contract the steer-vs-queue branch depends on."""
        if not text:
            return False
        state = cast("CodexRemoteState | None", info.engine_state)
        if state is not None and info.turn_active and _silence_ends(info, state.open_items):
            # The turn is about to end silent: the caller queues the input.
            return False
        # A photo attached to the steer must be on the machine before the
        # daemon reads its path — the same barrier the turn dispatch runs.
        from core.remote import upload_inflight
        if info.files_synced and not await upload_inflight.wait_settled(
                info.agent_name, machine_id=info.machine_id,
                ceiling=upload_inflight.BARRIER_TIMEOUT_S):
            text = await upload_inflight.turn_goes_on_without_files(
                info.session_id, info.machine_id, info.agent_name, text)
        try:
            ack = await cm.send_command(info.machine_id, {
                "type": STEER_FRAME,
                "session_id": info.session_id,
                "text": text,
            }, timeout=15.0)
        except RuntimeError as e:
            logger.warning("remote steer RPC failed for %s: %s", info.session_id[:8], e)
            return False
        return bool(ack.get("steered"))

    async def compact(self, info: RemoteSessionInfo, cm) -> dict | None:
        """Manual context compaction: the machine's
        ``thread/compact/start`` flow. The satellite runs the whole wait loop
        (up to ~125s) and acks ``{ok, post_tokens|reason}``."""
        try:
            ack = await cm.send_command(info.machine_id, {
                "type": COMPACT_FRAME,
                "session_id": info.session_id,
            }, timeout=150.0)
        except RuntimeError as e:
            logger.warning("remote compact RPC failed for %s: %s", info.session_id[:8], e)
            return None
        if not ack.get("ok"):
            logger.info(
                "remote compact refused for %s: %s",
                info.session_id[:8], ack.get("reason", "unknown"),
            )
            return None
        return {"post_tokens": ack.get("post_tokens")}

    # --- resume --------------------------------------------------------------

    async def can_resume(
        self, cm, *, machine_id: str, session_id: str, agent_name: str,
        username: str, resume_handle: str, external_home: str = "",
    ) -> ResumeVerdict:
        """Codex resumes by THREAD id: the thread and its rollout persist under
        the satellite's CODEX_HOME independently of the daemon, and the
        satellite's own session guards a missing rollout at spawn. No RPC —
        the handle is the answer."""
        return ResumeVerdict.RESUMABLE if resume_handle else ResumeVerdict.FRESH

    # --- the router and the background sub-agent supervision -----------------
    # The proxy's router and supervisor of a machine's Codex session, reading
    # the WS-forwarded session_event stream. Reuses the shared SubagentRegistry + resolve_bg_subagent +
    # _bg_agent_monitor + wait_for_bg_subagents, so a delegated REMOTE Codex
    # agent waits for its bg subs exactly like a local one.

    async def _route_notifications(self, info: RemoteSessionInfo) -> None:
        """Sole consumer of info.event_queue.

        Demultiplexes by ``threadId``: MAIN-thread codex events (plus the
        synthetic turn-control markers _turn_ended / _resume_handle / a
        satellite-level error, which carry no codex ``method``) go to the active
        turn's consumer; each spawned sub-agent thread's events go to its own
        buffer so a background sub keeps streaming to the proxy after the main
        turn ends. Runs for the session's lifetime; cancelled by close_state.
        Mirrors the LOCAL _route_notifications."""
        state = cast("CodexRemoteState", info.engine_state)
        q = info.event_queue
        cq: asyncio.Queue | None
        while True:
            try:
                raw = await q.get()
            except asyncio.CancelledError:
                return
            info.last_activity = time.monotonic()
            if raw is None:
                # Session ended on the satellite — fan the sentinel out to the
                # active turn + every supervisor so none hang on a terminal that
                # will never come.
                if state.default_consumer is not None:
                    state.default_consumer.put_nowait(None)
                for cq in list(state.thread_consumers.values()):
                    cq.put_nowait(None)
                return
            # Synthetic turn-control markers (dicts without a codex ``method``).
            if isinstance(raw, dict) and "method" not in raw:
                if raw.get("type") == "_resume_handle":
                    # The main thread id — the router's demux key. Sent once at
                    # session start (before the first turn registers a
                    # consumer), so the router consumes it directly instead of
                    # forwarding it to a (still-None) turn. The chat row is
                    # written on the first turn (_stream_main_turn's METADATA
                    # event) — the chat is not bound to the session yet here.
                    # A revive that had to open a new thread sends it again:
                    # the next turn's METADATA then writes the new one.
                    from core.remote.remote_turn import record_resume_handle
                    handle = raw.get("handle", "")
                    if handle and info.resume_handle and handle != info.resume_handle:
                        state.translator._emitted_thread_id = False
                    record_resume_handle(info, handle)
                    if raw.get("resumed") is False:
                        # A revive whose thread would not open: the turn under
                        # way runs without the memory, the next one restores it.
                        spawn(_reseed_after_revive(info.session_id),
                              name="codex-revive-reseed", owner="codex-remote")
                    continue
                # Other markers (_turn_ended / satellite error) → the active turn.
                if state.default_consumer is not None:
                    state.default_consumer.put_nowait(raw)
                continue
            params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
            tid = params.get("threadId")
            if tid and info.resume_handle and tid != info.resume_handle:
                cq = state.thread_consumers.get(tid)
                if cq is None:
                    cq = asyncio.Queue()
                    state.thread_consumers[tid] = cq
                cq.put_nowait(raw)
            elif state.default_consumer is not None:
                state.default_consumer.put_nowait(raw)
            elif raw.get("method") in ("thread/goal/updated", "thread/goal/cleared"):
                # Codex accounts goal progress AT TURN STOP, so the final goal
                # update (often the completion) lands after turn/completed —
                # apply out-of-band (chat-durable state) instead of dropping.
                # Mirrors the LOCAL router's _apply_goal_oob.
                try:
                    from core.layers.codex.goals import apply_goal_events_oob
                    apply_goal_events_oob(info.session_id, self._translate(info, raw))
                except Exception:
                    logger.exception(
                        "Remote session %s: out-of-band goal apply failed", info.session_id[:8],
                    )
            elif (raw.get("method") == "item/completed"
                    and state.translator.tracks_bg_command(params)):
                # A background terminal's exit (its item/completed, tagged with
                # the ORIGINAL turn id) landed between turns — the badge gate
                # must still resolve. Apply it out-of-band instead of dropping.
                # Twin of the LOCAL router's _apply_bg_command_oob: the
                # terminal's own badge clears on the chat at once (no pump is
                # attached between turns to consume its BG_COMMAND_END).
                # Guarded — a failure here must never kill the router.
                try:
                    from core.session.session_state import clear_ended_bg_commands
                    clear_ended_bg_commands(info.session_id, self._translate(info, raw))
                except Exception:
                    logger.exception(
                        "Remote session %s: out-of-band bg-command apply failed",
                        info.session_id[:8],
                    )
            # else: between turns, a main-thread straggler with no active
            # consumer — drop it (the daemon is idle on the main thread).

    def _handoff_bg_subagents(self, info: RemoteSessionInfo) -> None:
        """At main-turn end: register + arm a supervisor for each background
        sub-agent still active (read from the translator, which saw the main
        thread's collabAgentToolCall states), drop buffers for foreground subs
        that already terminated, and clear the main-turn consumer. Feeds the
        per-session SubagentRegistry so the shared _bg_agent_monitor nudges and
        wait_for_bg_subagents covers remote delegated agents. Sync (mirrors the
        LOCAL _handoff_bg_subagents)."""
        state = cast("CodexRemoteState | None", info.engine_state)
        if state is None:
            return
        state.default_consumer = None  # stop feeding the finished main turn
        pending: list[dict] = []
        if info.alive:
            try:
                pending = state.translator.pending_bg_subagents()
            except Exception:
                logger.exception(
                    "Remote session %s: pending_bg_subagents failed", info.session_id[:8],
                )
        pending_ids = {p["agent_id"] for p in pending}
        # Drop buffers for sub threads we won't supervise (foreground subs that
        # already reached terminal — their buffered events are dead weight).
        for tid in list(state.thread_consumers.keys()):
            if tid not in pending_ids and tid not in state.bg_supervisors:
                state.thread_consumers.pop(tid, None)
        if not pending:
            return
        from core.events.bg_command_state import shorten_label
        from core.session.session_state import get_subagent_registry
        reg = get_subagent_registry(info.session_id)
        for p in pending:
            aid = p["agent_id"]
            if aid in state.bg_supervisors:
                continue  # already supervised (carried over from a prior turn)
            state.thread_consumers.setdefault(aid, asyncio.Queue())
            reg.register_spawn(aid, aid, label=shorten_label(p.get("description", "")),
                               background=True)
            state.bg_supervisors[aid] = asyncio.create_task(
                self._supervise_bg_subagent(info, aid),
                name=f"remote-codex-bgsup-{info.session_id[:8]}-{aid[-6:]}",
            )
        logger.info(
            "Remote session %s: %d background sub-agent(s) running past turn end "
            "→ supervising %s", info.session_id[:8], len(pending), sorted(pending_ids),
        )

    async def _supervise_bg_subagent(self, info: RemoteSessionInfo, sub_tid: str) -> None:
        """Drain ONE background sub-agent's thread buffer until it terminates,
        then resolve it. Runs concurrently with user/nudge turns (never blocks a
        turn). It waits as long as the session lives; a lost terminal is the
        chat monitor's to give up on, after the agent's silence. Mirrors the
        LOCAL _supervise_bg_subagent."""
        from core.session.session_state import get_subagent_registry
        state = cast("CodexRemoteState", info.engine_state)
        reg = get_subagent_registry(info.session_id)
        q = state.thread_consumers.get(sub_tid)
        try:
            while q is not None:
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if not info.alive:
                        break
                    continue
                info.last_activity = time.monotonic()
                reg.note_alive(sub_tid)
                if raw is None:  # session ended
                    break
                method = raw.get("method", "") if isinstance(raw, dict) else ""
                if method == "turn/completed":
                    break
                if method == "error" and not will_retry(raw.get("params") if isinstance(raw, dict) else None):
                    break
        finally:
            self._resolve_bg_subagent(info, sub_tid)

    def _resolve_bg_subagent(self, info: RemoteSessionInfo, sub_tid: str) -> None:
        """Mark a remote background sub-agent done + clear its badge via the shared
        resolve_bg_subagent (one source of truth with the local path). Sync +
        idempotent; pops our own per-thread bookkeeping first."""
        from core.session.session_state import resolve_bg_subagent
        state = cast("CodexRemoteState | None", info.engine_state)
        translator = None
        if state is not None:
            state.thread_consumers.pop(sub_tid, None)
            state.bg_supervisors.pop(sub_tid, None)
            translator = state.translator
        resolve_bg_subagent(info.session_id, sub_tid, translator)

    def _translate(self, info: RemoteSessionInfo, raw_event: dict) -> list[CommonEvent]:
        """Translate a forwarded Codex app-server notification to CommonEvents.

        The satellite forwards each ``codex app-server`` notification verbatim
        as ``{"method": ..., "params": ...}`` (the same NDJSON the local layer
        sees); the shared :class:`CodexEventTranslator` keys on the method.
        """
        state = cast("CodexRemoteState | None", info.engine_state)
        if state is None:
            return []
        # Seed the multi-agent demux with the AUTHORITATIVE main thread id (the
        # satellite-reported resume handle) so a spawned sub-agent's thread
        # can't hijack `_main_thread_id` and suppress the MAIN agent's output.
        # Idempotent; mirrors the local layer + the satellite session-loop guard.
        if info.resume_handle:
            state.translator._main_thread_id = info.resume_handle
        method = raw_event.get("method", "")
        return state.translator.translate(CodexEvent(type=method, data=raw_event.get("params") or {}))


def _server_to_manifest_name() -> dict[str, str]:
    """``[mcp_servers.<key>]`` keys are ``server_name``s; the satellite MCP
    sync speaks manifest names."""
    from services.mcp import mcp_registry
    return {
        (m.server_name or m.name): n
        for n, m in mcp_registry.get_all_manifests().items()
    }
