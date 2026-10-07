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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from core.events import turn_ending, turn_life
from core.events.common_events import CommonEvent, DONE, ERROR, METADATA, PLAN_MODE
from core.execution_layer import (
    AgentConfig, RemoteEngineAdapter, RemoteStartContext, RemoteStartPlan,
)
from core.layers.codex.helpers import (
    LOCAL_ENDPOINT_KEY_ENV,
    LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS,
    codex_hooks_floor,
    map_effort_to_codex,
    permission_to_sandbox,
    will_retry,
    with_local_provider_note,
)
from core.layers.codex.layer import CodexCLIExecutionLayer
from core.layers.codex.local_model_catalog import (
    LOCAL_MODEL_ROWS_ENV, local_model_catalog_json, parse_local_model_rows,
)
from core.layers.codex.session import _TOOL_ITEM_TYPES, CodexEvent
from core.layers.codex.translator import CodexEventTranslator
from core.layers.cli.remote import _lost_events, _satellite_error_events
from core.session import session_kind

if TYPE_CHECKING:
    from core.remote.remote_session_info import RemoteSessionInfo

logger = logging.getLogger("remote-layer")

# The start-payload keys the satellite's CodexSession / CodexPtySession read —
# this engine's wire vocabulary, frozen with the satellites in the field.
CREDENTIALS_PAYLOAD_KEY = "auth_json"
CONFIG_DIR_PAYLOAD_KEY = "codex_dir_relative"
RESUME_HANDLE_PAYLOAD_KEY = "thread_id"
MCP_CONFIG_PAYLOAD_KEY = "mcp_config_toml"

# The satellite frames this engine's session answers to. ``abort`` IS the
# graceful interrupt for Codex: the satellite's Codex twin handles it softly
# (``turn/interrupt``, the warm daemon survives) and the deployed satellites'
# ``interrupt_turn`` handler requires a CLI ``proc``.
SOFT_INTERRUPT_FRAME = "abort"
CONTROL_REQUEST_FRAME = "control_request"   # set_model / set_permission_mode → per-turn override
STEER_FRAME = "codex_steer"                 # turn/steer into the running turn (satellite ≥ 0.5.98)
COMPACT_FRAME = "codex_compact"             # thread/compact/start (satellite ≥ 0.5.98)
BG_TERMINALS_FRAME = "codex_bg_terminals"   # thread/backgroundTerminals/list (satellite ≥ 0.5.105)

# The local-endpoint variables the pool hands a Codex session: they never ride
# the satellite env in this form (the provider travels as a payload field, the
# key under the provider's own env name).
_ENDPOINT_URL_ENV = "_CODEX_ENDPOINT_URL"
_ENDPOINT_KEY_ENV = "_CODEX_LOCAL_API_KEY"
_ENDPOINT_PROVIDER_ENV = "_CODEX_ENDPOINT_PROVIDER"

# start_session ack budget for a Codex session on a LOCAL model: the satellite
# acks after its pre-turn MCP warm gate, which waits for every server with a
# 90 s cap on a local model (a changed tool list re-prefills for minutes
# there), on top of the daemon spawn. Hosted sessions keep the 60 s budget.
_LOCAL_MODEL_START_TIMEOUT_S = 180.0

# A background sub-agent's supervisor gives up waiting for its terminal after
# this long (a lost terminal must not hold the registry forever).
_BG_SUBAGENT_CEILING_S = 600.0

# How long the turn loop waits for a notification before it checks the
# turn's life against its silence ceiling (turn_life).
_SILENCE_SLICE_S = 60.0


@dataclass
class CodexRemoteState:
    """Per-session state of a remote Codex session — the mirror of the local
    ``CodexAppServerSession``'s router/supervisor fields, kept here because the
    proxy holds the translator and does the demux while the satellite stays a
    dumb pipe."""
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


def _silence_ends(info: "RemoteSessionInfo", open_items: set) -> bool:
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

    def start_payload(self, base: dict, ctx: RemoteStartContext) -> RemoteStartPlan:
        config, env, payload, cm = ctx.config, ctx.env, base, ctx.cm
        machine_id = ctx.machine_id
        start_timeout_s = 60.0
        payload[CONFIG_DIR_PAYLOAD_KEY] = (
            f"{ctx.scope_root}/{self.layer.capabilities.runtime.config_dir_name}"
        )
        payload[RESUME_HANDLE_PAYLOAD_KEY] = config.resume_handle

        # A local OpenAI-compatible endpoint (a ``local_endpoint`` subscription)
        # reaches the layer as private variables. The satellite writes its own
        # config.toml, so the provider travels as the ``local_model_provider``
        # payload field (satellite >= 0.5.116) and the key as the child-env
        # variable the provider's ``env_key`` names — the same two artifacts
        # the LOCAL layer produces (``_write_config_toml``). An older satellite
        # would run the local model name against Codex's built-in OpenAI
        # provider, which is exactly the leak this refuses.
        local_endpoint = env.pop(_ENDPOINT_URL_ENV, "")
        local_api_key = env.pop(_ENDPOINT_KEY_ENV, "")
        local_provider = env.pop(_ENDPOINT_PROVIDER_ENV, "")
        local_model_rows = parse_local_model_rows(env.pop(LOCAL_MODEL_ROWS_ENV, ""))
        local_model_provider: dict | None = None
        if local_endpoint:
            if not cm.satellite_supports_local_model_provider(machine_id):
                _ver = cm.satellite_version(machine_id) or "an unknown version"
                raise RuntimeError(
                    "Local model endpoints need satellite 0.5.116 or newer — "
                    f"{cm.satellite_name(machine_id)} runs {_ver}. Update it "
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
            if cm.satellite_supports_local_model_catalog(machine_id):
                local_model_provider["stream_idle_timeout_ms"] = LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS
                local_model_provider["catalog_json"] = local_model_catalog_json(
                    config.model, local_provider, local_model_rows,
                )
            else:
                logger.warning(
                    f"Remote Codex on a local model: {cm.satellite_name(machine_id)} "
                    f"runs satellite {cm.satellite_version(machine_id) or 'unknown'} "
                    "(< 0.5.117) — no deferred MCP tools, Codex's 5-minute idle timeout "
                    "and the old MCP warm gate until it updates at its next reconnect"
                )
            if local_api_key:
                env[LOCAL_ENDPOINT_KEY_ENV] = local_api_key

        # The PreToolUse permission FLOOR for unattended sessions — the same
        # rule as the local layer (helpers.codex_hooks_floor). The satellite
        # (>= 0.5.118) writes `[features] hooks = true`, trusts the hook per
        # thread (bypass_hook_trust) and sets the deny-only / no-forward hook
        # env; under `approvalPolicy: never` the approval bridge never fires,
        # so without this a remote task / meeting / phone / trigger session is
        # gated by the prompt rules only. An older satellite ignores the
        # field: warn, never refuse — the session runs the way it did before
        # this field existed.
        floor = codex_hooks_floor(config.client_type, config.interactive)
        payload["codex_hooks_floor"] = floor
        if floor and not cm.satellite_supports_codex_hooks_floor(machine_id):
            logger.warning(
                f"Remote Codex {config.client_type} session {ctx.session_id[:8]}: "
                f"{cm.satellite_name(machine_id)} runs satellite "
                f"{cm.satellite_version(machine_id) or 'unknown'} "
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
            # The satellite acks after the local-model MCP warm gate.
            start_timeout_s = _LOCAL_MODEL_START_TIMEOUT_S
        payload["agents_md_content"] = payload["system_prompt"]
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
        # A judge (CHECKS.md) on a satellite below 0.5.123 would run
        # read-only IN the plan collaboration mode (that satellite ties
        # the two); it gets the ordinary sandbox there and the gate alone
        # enforces — the honest state a machine has anyway.
        _mode_for_sandbox = config.permission_mode
        if _mode_for_sandbox == "judge" and not cm.satellite_supports_checks(machine_id):
            _mode_for_sandbox = "default"
        payload["sandbox_mode"] = permission_to_sandbox(
            _mode_for_sandbox,
            allow_full_fs=bool(
                config.security_context is not None
                and config.security_context.placement.allow_full_fs
            ),
        )
        # The satellite's plan collaboration mode follows a shipped
        # ``permission_mode`` (0.5.123) and otherwise the live sandbox
        # (read-only ⟺ plan). A mode change mid-session carries the sandbox
        # alone (``control_request``), so a shipped mode would freeze plan
        # at its start value: only a judge ships it, read-only without plan
        # and never changed.
        payload["permission_mode"] = "judge" if _mode_for_sandbox == "judge" else ""
        # The ChatGPT login's auth.json (popped from env; the satellite writes
        # it verbatim into the session's CODEX_HOME).
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
                    from core.remote.remote_mcp_rewrite import _rewrite_mcp_toml_for_remote
                    payload[MCP_CONFIG_PAYLOAD_KEY] = _rewrite_mcp_toml_for_remote(
                        mcp_path.read_text(), ctx.sat_port,
                        target_os=ctx.target_os, session_id=ctx.session_id,
                        proxy_api_key=ctx.proxy_api_key,
                        secret_bundle_keys=ctx.secret_bundle_keys,
                        gateway=ctx.gateway, gateway_mode=ctx.gateway_mode,
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

    # Codex reads its instructions from AGENTS.md, built from the prompt.
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

    def init_session(self, info: "RemoteSessionInfo", config: AgentConfig, cm) -> None:
        """One translator for the session's life (cumulative token deltas
        live in it; the thread id is emitted once) and the router — the SOLE
        consumer of ``info.event_queue`` — demuxing main-thread events to the
        active turn and each background sub-agent thread to its own buffer.
        Mirrors the LOCAL layer's ``supervised_bg=True`` session; every
        satellite that can connect forwards bg-thread events past the main
        turn (that landed in 0.5.18, far below ``MIN_SATELLITE_VERSION``)."""
        self._build_state(info, config.model, cm)

    def adopt_state(self, info: "RemoteSessionInfo", cm) -> None:
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
        self._build_state(info, info.model, cm)
        if info.mode:
            asyncio.create_task(self.control_request(
                info, cm, "set_permission_mode", mode=info.mode))
        if info.model:
            asyncio.create_task(self.control_request(
                info, cm, "set_model", model=info.model))

    def _build_state(self, info: "RemoteSessionInfo", model: str, cm) -> None:
        state = CodexRemoteState(translator=CodexEventTranslator(
            model=model, supervised_bg=True,
            session_id=info.session_id, supervised_bg_commands=True,
        ))
        info.engine_state = state
        state.translator.retried_errors_end_turn = not cm.satellite_supports_codex_retry_read(
            info.machine_id)
        state.router_task = asyncio.create_task(
            self._route_notifications(info),
            name=f"remote-codex-router-{info.session_id[:8]}",
        )

    async def close_state(self, info: "RemoteSessionInfo") -> None:
        """Cancel the router + every bg supervisor and resolve their registry
        entries (so close / offline can't leave the _bg_agent_monitor waiting on
        a dead sub-agent). Idempotent. Mirrors the LOCAL _teardown_bg."""
        state: CodexRemoteState | None = info.engine_state
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

    async def begin_turn(self, info: "RemoteSessionInfo", settle_after_result: int) -> None:
        # Per-turn bg-command registry prune (mirrors the LOCAL session's
        # send_message). reset() PRESERVES still-pending terminals; without it
        # `unsurfaced` never clears remotely and task_producer's cohort math
        # fires spurious review turns.
        from core.events.bg_command_state import reset_bg_command_registry
        reset_bg_command_registry(info.session_id)
        # The router owns info.event_queue and demuxes by thread, so there are
        # no main-thread stragglers to drain (a prior turn's leftovers either
        # went to a bg buffer or the now-discarded prior consumer). Register a
        # fresh main-turn consumer for the router to feed.
        state: CodexRemoteState = info.engine_state
        state.default_consumer = asyncio.Queue()

    async def stream_turn(self, info: "RemoteSessionInfo", cm) -> AsyncIterator[CommonEvent]:
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

    async def _stream_main_turn(self, info: "RemoteSessionInfo", cm) -> AsyncIterator[CommonEvent]:
        state: CodexRemoteState = info.engine_state
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

        # Emit the resume handle once → the pump persists chats.codex_thread_id
        # (the frozen METADATA key) so the thread resumes after a proxy
        # restart. The SAME event the local layer yields (codex/layer.py);
        # the translator emits it once per session, and a turn that runs
        # before the satellite's frame arrived leaves it for the next one.
        for ev in state.translator.thread_id_metadata(info.resume_handle):
            yield ev

        def _plan_card() -> "CommonEvent | None":
            if in_plan and not turn_interrupted and final_plan_msg.strip():
                return CommonEvent(type=PLAN_MODE, data={
                    "action": "exit", "synthetic": True,
                    "tool_input": {"plan": final_plan_msg},
                })
            return None

        q = state.default_consumer
        assert q is not None  # invariant: begin_turn registered the consumer
        open_items = state.open_items
        open_items.clear()
        completed = False
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
                try:
                    await cm.send_fire_and_forget(info.machine_id, {
                        "type": "abort", "session_id": info.session_id,
                    })
                except Exception:
                    logger.warning(
                        "codex silence abort send failed for %s", info.session_id[:8],
                    )
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
                if not completed and not info.proxy_killed:
                    # The satellite closed the turn before turn/completed:
                    # its process went mid-turn.
                    info.cli_dead = True
                    for event in _lost_events("the machine ended the turn before its result"):
                        yield event
                    return
                card = _plan_card()
                if card:
                    yield card
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

            info.last_activity = info.last_event_at = time.monotonic()
            for event in self._translate(info, raw):
                # Codex doesn't report turn duration — measure it proxy-side
                # and stamp it onto the METADATA event (mirrors codex/layer.py;
                # remote Codex turns were persisting duration_ms=0).
                if event.type == METADATA and "cost_usd" in event.data:
                    event.data["duration_ms"] = int((time.monotonic() - turn_start) * 1000)
                if event.type == DONE:
                    card = _plan_card()
                    if card:
                        yield card
                    # A revive on a new thread announced it during this turn.
                    for ev in state.translator.thread_id_metadata(info.resume_handle):
                        yield ev
                yield event
                if event.type == DONE:
                    return

    async def drain_bg_commands(self, info: "RemoteSessionInfo", cm, *, budget: float) -> bool:
        """Between turns, reconcile pending background-terminal registry entries
        against the satellite's ``codex_bg_terminals`` RPC (which proxies
        ``thread/backgroundTerminals/list``) and resolve any whose PTY is gone.
        Remote twin of ``CodexAppServerSession.drain_bg_commands`` — same
        matching (itemId OR the late-bound processId), same fail-closed contract
        (RPC failure / satellite refusal is NO progress, never resolve-on-error),
        same >=1s self-pacing and 0.1s lock timeout. No-op below the 0.5.105
        gate: an older satellite would silently drop the frame and every poll
        would burn its ack timeout (the router's OOB hook still resolves live
        completions there). NEVER touches info.event_queue — the router owns it."""
        from core.events.bg_command_state import get_bg_command_registry
        from core.session.session_state import resolve_bg_command
        state: CodexRemoteState | None = info.engine_state
        if state is None or not cm.supports_codex_bg_terminals(info.machine_id):
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
            except Exception:
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

    async def control_request(self, info: "RemoteSessionInfo", cm, subtype: str, **kwargs) -> bool:
        """The app-server has no stdin control channel; the satellite's
        ``CodexSession.send_control_request`` applies ``set_model`` and
        ``set_permission_mode`` as per-turn overrides on the next
        ``turn/start`` (every released satellite honours both). The mode
        travels as the SANDBOX mode: the satellite re-derives approvalPolicy
        from it and rebuilds the per-turn sandboxPolicy (the proxy-side gate
        verdict already follows the new mode via set_session_mode)."""
        if subtype == "set_model":
            frame_kwargs = {"model": kwargs.get("model", "")}
        elif subtype == "set_permission_mode":
            frame_kwargs = {"sandbox_mode": permission_to_sandbox(
                kwargs.get("mode", ""), allow_full_fs=info.allow_full_fs,
            )}
        else:
            return False  # no other control subtype has a Codex meaning
        await cm.send_fire_and_forget(info.machine_id, {
            "type": CONTROL_REQUEST_FRAME,
            "session_id": info.session_id,
            "subtype": subtype,
            "kwargs": frame_kwargs,
        })
        return True

    async def steer(self, info: "RemoteSessionInfo", cm, text: str) -> bool:
        """Mid-turn steering — tunnel twin of the local layer's ``turn/steer``.
        Version-gated: an old satellite would silently drop the frame, so
        return False immediately and let the caller take today's queue
        fallback. ``steered`` in the ack is strict (True only on daemon accept)
        — the exactly-once contract the steer-vs-queue branch depends on."""
        if not text:
            return False
        state: CodexRemoteState | None = info.engine_state
        if state is not None and info.turn_active and _silence_ends(info, state.open_items):
            # The turn is about to end silent: the caller queues the input.
            return False
        if not cm.supports_codex_thread_ops(info.machine_id):
            logger.info(
                "remote steer unsupported by satellite %s (< 0.5.98) — "
                "falling back to queue", info.machine_id[:8],
            )
            return False
        # A photo attached to the steer must be on the machine before the
        # daemon reads its path — the same barrier the turn dispatch runs.
        from core.remote import upload_inflight
        await upload_inflight.wait_settled(info.agent_name)
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

    async def compact(self, info: "RemoteSessionInfo", cm) -> dict | None:
        """Manual context compaction — tunnel twin of the local layer's
        ``thread/compact/start`` flow. The satellite runs the whole wait loop
        (up to ~125s) and acks ``{ok, post_tokens|reason}``; version-gated
        like ``steer``."""
        if not cm.supports_codex_thread_ops(info.machine_id):
            logger.info(
                "remote compact unsupported by satellite %s (< 0.5.98)", info.machine_id[:8],
            )
            return None
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
        username: str, resume_handle: str,
    ) -> bool:
        """Codex resumes by THREAD id: the thread and its rollout persist under
        the satellite's CODEX_HOME independently of the daemon, and the
        satellite's own session guards a missing rollout at spawn. No RPC —
        the handle is the answer."""
        return bool(resume_handle)

    # --- the router and the background sub-agent supervision -----------------
    # Proxy-side mirror of core/layers/codex/session.py's router/supervisor,
    # reading the WS-forwarded session_event stream instead of the daemon's
    # notif_queue. Reuses the shared SubagentRegistry + resolve_bg_subagent +
    # _bg_agent_monitor + wait_for_bg_subagents, so a delegated REMOTE Codex
    # agent waits for its bg subs exactly like a local one.

    async def _route_notifications(self, info: "RemoteSessionInfo") -> None:
        """Sole consumer of info.event_queue.

        Demultiplexes by ``threadId``: MAIN-thread codex events (plus the
        synthetic turn-control markers _turn_ended / _resume_handle / a
        satellite-level error, which carry no codex ``method``) go to the active
        turn's consumer; each spawned sub-agent thread's events go to its own
        buffer so a background sub keeps streaming to the proxy after the main
        turn ends. Runs for the session's lifetime; cancelled by close_state.
        Mirrors the LOCAL _route_notifications."""
        state: CodexRemoteState = info.engine_state
        q = info.event_queue
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
                # Twin of the LOCAL router's _apply_bg_command_oob. Guarded — a
                # failure here must never kill the router; the translated
                # events are discarded (no pump is attached between turns; the
                # monitor's ``bg_commands_complete`` nudge is the visible clear).
                try:
                    self._translate(info, raw)
                except Exception:
                    logger.exception(
                        "Remote session %s: out-of-band bg-command apply failed",
                        info.session_id[:8],
                    )
            # else: between turns, a main-thread straggler with no active
            # consumer — drop it (the daemon is idle on the main thread).

    def _handoff_bg_subagents(self, info: "RemoteSessionInfo") -> None:
        """At main-turn end: register + arm a supervisor for each background
        sub-agent still active (read from the translator, which saw the main
        thread's collabAgentToolCall states), drop buffers for foreground subs
        that already terminated, and clear the main-turn consumer. Feeds the
        per-session SubagentRegistry so the shared _bg_agent_monitor nudges and
        wait_for_bg_subagents covers remote delegated agents. Sync (mirrors the
        LOCAL _handoff_bg_subagents)."""
        state: CodexRemoteState | None = info.engine_state
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
            reg.register_spawn(aid, aid, label=shorten_label(p.get("description", "")))
            state.bg_supervisors[aid] = asyncio.create_task(
                self._supervise_bg_subagent(info, aid),
                name=f"remote-codex-bgsup-{info.session_id[:8]}-{aid[-6:]}",
            )
        logger.info(
            "Remote session %s: %d background sub-agent(s) running past turn end "
            "→ supervising %s", info.session_id[:8], len(pending), sorted(pending_ids),
        )

    async def _supervise_bg_subagent(self, info: "RemoteSessionInfo", sub_tid: str) -> None:
        """Drain ONE background sub-agent's thread buffer until it terminates,
        then resolve it. Runs concurrently with user/nudge turns (never blocks a
        turn). The ceiling backstops a lost terminal. Mirrors the LOCAL
        _supervise_bg_subagent."""
        state: CodexRemoteState = info.engine_state
        start = time.monotonic()
        q = state.thread_consumers.get(sub_tid)
        try:
            while q is not None and (time.monotonic() - start) < _BG_SUBAGENT_CEILING_S:
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if not info.alive:
                        break
                    continue
                info.last_activity = time.monotonic()
                if raw is None:  # session ended
                    break
                method = raw.get("method", "") if isinstance(raw, dict) else ""
                if method == "turn/completed":
                    break
                if method == "error" and not will_retry(raw.get("params") if isinstance(raw, dict) else None):
                    break
            else:
                if q is not None:
                    logger.warning(
                        "Remote session %s: bg sub-agent %s hit the %.0fs ceiling "
                        "without a terminal — resolving",
                        info.session_id[:8], sub_tid, _BG_SUBAGENT_CEILING_S,
                    )
        finally:
            self._resolve_bg_subagent(info, sub_tid)

    def _resolve_bg_subagent(self, info: "RemoteSessionInfo", sub_tid: str) -> None:
        """Mark a remote background sub-agent done + clear its badge via the shared
        resolve_bg_subagent (one source of truth with the local path). Sync +
        idempotent; pops our own per-thread bookkeeping first."""
        from core.session.session_state import resolve_bg_subagent
        state: CodexRemoteState | None = info.engine_state
        translator = None
        if state is not None:
            state.thread_consumers.pop(sub_tid, None)
            state.bg_supervisors.pop(sub_tid, None)
            translator = state.translator
        resolve_bg_subagent(info.session_id, sub_tid, translator)

    def _translate(self, info: "RemoteSessionInfo", raw_event: dict) -> list[CommonEvent]:
        """Translate a forwarded Codex app-server notification to CommonEvents.

        The satellite forwards each ``codex app-server`` notification verbatim
        as ``{"method": ..., "params": ...}`` (the same NDJSON the local layer
        sees); the shared :class:`CodexEventTranslator` keys on the method.
        """
        state: CodexRemoteState | None = info.engine_state
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
