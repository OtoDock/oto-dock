"""Claude Code CLI on a satellite — this engine's half of the remote path.

``ClaudeRemoteAdapter`` implements ``RemoteEngineAdapter`` for the Claude
CLI: the start payload keys its satellite session classes read
(``satellite/sessions/cli_session.py``, ``satellite/terminal/pty_session.py``),
the per-turn translator + settle controller, the turn stream over the
satellite's forwarded NDJSON (with the shared settle and foreign-result
gate), the ``stop_turn`` / ``interrupt_turn`` / ``control_request`` frames,
the between-turns queue read for background commands, and the JSONL resume
probe. The remote layer (``core/remote``) keeps everything shared and calls
in here through the adapter; nothing in ``core/remote`` names this engine.

Import rule: this module imports the leaf and its own package at module
level, and ``core.remote.**`` function-locally only (the generic queue
reader, the MCP config rewriter, the resume-handle writer) — the remote
package imports the layers function-locally too, so neither side runs
against a half-initialised module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

import config as app_config
from core.events.common_events import CommonEvent, DONE, ERROR
from core.execution_layer import (
    AgentConfig, RemoteEngineAdapter, RemoteStartContext, RemoteStartPlan,
)
from core.layers.cli import usage as claude_usage
from core.layers.cli.layer import CLIExecutionLayer, cli_chunk_to_events
from core.layers.cli.oauth import REMOTE_401_WAIT_MS
from core.layers.cli.session import _STEER_INIT_GRACE_S
from core.layers.cli.settle import (
    FOREIGN_SKIP_SILENCE_S,
    ForeignSkipGate,
    SettleController,
    chunk_is_content,
    is_foreign_result,
)
from core.layers.cli.translator import ClaudeCLIEventTranslator

if TYPE_CHECKING:
    from core.remote.remote_session_info import RemoteSessionInfo

logger = logging.getLogger("remote-layer")

# The start-payload keys the satellite's CLISession / PtySession read — this
# engine's wire vocabulary, frozen with the satellites in the field.
CREDENTIALS_PAYLOAD_KEY = "credentials_json"
CONFIG_DIR_PAYLOAD_KEY = "claude_dir_relative"
MCP_CONFIG_PAYLOAD_KEY = "mcp_config"

# The satellite frames this engine's session answers to.
SOFT_INTERRUPT_FRAME = "interrupt_turn"     # control_request {interrupt} into the CLI's stdin
STOP_TURN_FRAME = "stop_turn"               # exit the satellite's stdout read loop
CONTROL_REQUEST_FRAME = "control_request"   # set_model / set_permission_mode → CLI stdin
STEER_FRAME = "steer_turn"                  # a user frame into the live turn's stdin (satellite ≥ 0.5.128)
RESUME_PROBE_FRAME = "check_session_resumable"  # stat the session JSONL on the machine


@dataclass
class ClaudeRemoteState:
    """Per-turn state of a remote Claude session: the translator (a pure
    state machine, fresh every turn like the local ``PersistentSession``),
    the settle controller that owns the turn-end decision, and the steer
    bookkeeping ``PersistentSession`` keeps on itself — ``result_seen``
    (the DRIVEN result was read: a steer would start a turn nobody reads),
    ``steer_written`` (a frame is in the CLI's stdin: the post-result peek
    reads the turn it may start), ``steer_pending`` (RPCs whose ack is in
    flight: the peek covers them too, since the satellite may have written
    the frame before this side sees the result)."""
    translator: ClaudeCLIEventTranslator
    settle: SettleController
    result_seen: bool = False
    steer_written: bool = False
    steer_pending: int = 0


class ClaudeRemoteAdapter(RemoteEngineAdapter):
    private_env_keys = frozenset({CLIExecutionLayer._CREDENTIALS_ENV})
    owns_event_queue = False

    def __init__(self, layer) -> None:
        self.layer = layer

    # --- spawn ---------------------------------------------------------------

    def start_payload(self, base: dict, ctx: RemoteStartContext) -> RemoteStartPlan:
        config, env, payload = ctx.config, ctx.env, base
        # CLI effort: xhigh→max for models that don't support xhigh — the
        # satellite has no model registry, so the proxy is the single source
        # of truth (mirrors core/layers/cli/session.py). "ultra" is Codex-only
        # (gpt-5.6 multi-agent orchestration) — the `claude` CLI rejects it,
        # clamp to the ceiling.
        effort = config.effort
        if effort == "ultra":
            effort = "max"
        if effort == "xhigh" and not app_config.get_model_supports_xhigh(config.model):
            effort = "max"
        payload["effort"] = effort
        payload[CONFIG_DIR_PAYLOAD_KEY] = (
            f"{ctx.scope_root}/{self.layer.capabilities.runtime.config_dir_name}"
        )
        payload["resume"] = config.resume
        payload["session_id_for_resume"] = ctx.session_id if config.resume else ""
        # OAuth file delivery (mirrors the local layer): the satellite writes
        # .credentials.json into the session's CLAUDE_CONFIG_DIR. Popped so no
        # token rides the spawned env — env is frozen at exec and outranks the
        # file, which would defeat rotation fan-out.
        delivered = False
        credentials = self.layer.credential_file_from_env(env)
        if credentials is not None:
            payload[CREDENTIALS_PAYLOAD_KEY] = credentials
            delivered = True
            # Rotation fan-out reaches a satellite after a WS round-trip; a
            # request racing that push 401s first. This arms Claude's
            # 401-recovery poll window so it re-reads the credential file
            # until the push lands (local sessions write synchronously and
            # don't need it).
            env["CLAUDE_CODE_OAUTH_401_WAIT_MS"] = str(REMOTE_401_WAIT_MS)
        # Ship the built-in-tool deny list so the satellite's settings.json
        # carries the SAME permissions.deny the local sandbox applies (the
        # claude.ai Cron/Trigger/Push/integration tools — one source of
        # truth, core/layers/cli/config_dir.py). Never the session's own
        # denials: the satellite writes this list into the scope's shared
        # settings.json, which the CLI reloads live, so a judge's write tools
        # or an external caller's shell would come off every live session of
        # that scope on the machine. The gate over the tunnel refuses them.
        from core.layers.cli.config_dir import DISALLOWED_BUILTIN_TOOLS
        payload["disallowed_tools"] = list(DISALLOWED_BUILTIN_TOOLS)
        # The JSON MCP config, rewritten for the satellite host.
        if config.mcp_config_path:
            try:
                mcp_path = Path(config.mcp_config_path)
                if mcp_path.exists():
                    from core.remote.remote_mcp_rewrite import _rewrite_mcp_json_for_remote
                    payload[MCP_CONFIG_PAYLOAD_KEY] = _rewrite_mcp_json_for_remote(
                        json.loads(mcp_path.read_text()), ctx.sat_port,
                        target_os=ctx.target_os, session_id=ctx.session_id,
                        secret_bundle_keys=ctx.secret_bundle_keys,
                        bearer_swap_keys=ctx.bearer_swap_keys,
                        proxy_api_key=ctx.proxy_api_key,
                    )
            except Exception:
                logger.exception("Claude MCP JSON build failed")
        return RemoteStartPlan(payload=payload, credential_file_delivered=delivered)

    def mcp_names(self, payload: dict) -> set[str]:
        """The payload's ``mcpServers`` keys are ``server_name``s — map each
        back to its manifest name (what the satellite MCP sync expects)."""
        servers = (payload.get(MCP_CONFIG_PAYLOAD_KEY) or {}).get("mcpServers") or {}
        if not servers:
            return set()
        server_to_name = _server_to_manifest_name()
        return {server_to_name.get(key, key) for key in servers}

    def without_mcps(self, payload: dict, excluded: set[str]) -> dict:
        mcp_config = payload.get(MCP_CONFIG_PAYLOAD_KEY)
        if mcp_config and "mcpServers" in mcp_config:
            server_to_name = _server_to_manifest_name()
            payload[MCP_CONFIG_PAYLOAD_KEY] = {"mcpServers": {
                key: val for key, val in mcp_config["mcpServers"].items()
                if server_to_name.get(key, key) not in excluded
            }}
        return payload

    # --- per-session state ---------------------------------------------------

    def init_session(self, info: "RemoteSessionInfo", config: AgentConfig, cm) -> None:
        # The translator and settle controller are per TURN (begin_turn);
        # nothing lives across turns for this engine.
        info.engine_state = None

    def adopt_state(self, info: "RemoteSessionInfo") -> None:
        """Mode C: the replayed turn streams through a fresh translator and an
        interactive settle (the buffered turn's sentinel / turn_ended closes
        it)."""
        translator = ClaudeCLIEventTranslator(info.session_id)
        info.engine_state = ClaudeRemoteState(
            translator=translator,
            settle=SettleController(info.session_id, 0, translator),
        )

    # --- turns ---------------------------------------------------------------

    async def begin_turn(self, info: "RemoteSessionInfo", settle_after_result: int) -> None:
        from core.events.bg_command_state import reset_bg_command_registry
        from core.session.session_state import reset_subagent_registry
        reset_subagent_registry(info.session_id)
        reset_bg_command_registry(info.session_id)
        translator = ClaudeCLIEventTranslator(info.session_id)
        info.engine_state = ClaudeRemoteState(
            translator=translator,
            settle=SettleController(info.session_id, settle_after_result, translator),
        )

    async def stream_turn(self, info: "RemoteSessionInfo", cm) -> AsyncIterator[CommonEvent]:
        """Stream one CLI turn to the pump using the shared translator + settle.

        The satellite is a dumb pipe: it forwards every NDJSON line as a
        session_event. This owns the turn-end decision and sends ``stop_turn``
        to the satellite when the turn is over.
        """
        from core.events import wake_capture
        from core.session.session_state import (
            reconcile_background_snapshot, resolve_bg_command_frame,
        )
        state: ClaudeRemoteState = info.engine_state
        assert state is not None  # invariant: begin_turn / adopt_state ran
        translator, settle = state.translator, state.settle

        expected_cmd_id = info.current_send_command_id
        # Foreign-result skip regime (see settle.ForeignSkipGate): resume
        # handshake / settlement results must not close the driven turn —
        # the valve, not a count, ends the regime.
        gate = ForeignSkipGate(info.session_id, translator)
        while True:
            timeout = gate.clamp_timeout(settle.effective_timeout())
            try:
                raw = await asyncio.wait_for(info.event_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if gate.expired() and not settle.settling:
                    if cm.is_session_in_grace(info.machine_id, info.session_id):
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
                    await send_stop_turn(cm, info)
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                if not settle.settling:
                    # Pre-settle: long silence, keep waiting.
                    continue
                settle.maybe_log_heartbeat(proc_alive=True)
                if settle.should_exit_on_silence(timeout):
                    await send_stop_turn(cm, info)
                    # Wait briefly for satellite to drain + ack turn_ended
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
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
                    info.session_id[:8], rtype, expected_cmd_id[:8], ev_cmd[:8],
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
                        info.session_id[:8], expected_cmd_id[:8], ended_cmd_id[:8],
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
            # An init DURING settle = the CLI's self-wake review turn running
            # INLINE through this stream (its content joins the task output)
            # — note it so the producer/monitors skip the redundant nudge.
            # Twin of the local session loop's marker.
            if (settle.settling and rtype == "system"
                    and raw.get("subtype") == "init"):
                wake_capture.note_captured(info.session_id)
            for chunk in translator.feed(raw):
                if chunk.event_type == "rate_limit":
                    # The account's window state: for the pool, never the chat.
                    claude_usage.record_event_async(info.session_id, chunk.event_data)
                    continue
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
                state.result_seen = True
                if settle.is_interactive_done():
                    if state.steer_written or state.steer_pending:
                        # A steer the turn did not consume becomes the
                        # CLI's next turn by itself: its init follows the
                        # result at once, and this stream reads that turn
                        # through (twin of the local _drive_turn peek). The
                        # satellite's loop is still reading — no stop_turn
                        # was sent — so the frames keep coming.
                        nxt = await _peek_after_result(info, expected_cmd_id)
                        state.steer_written = False
                        if nxt is not None and wake_capture.is_wake_init(nxt):
                            state.result_seen = False
                            translator.reset_for_foreign_skip()
                            # Its result is the driven one even when empty.
                            gate.note_content()
                            logger.info(
                                "Remote session %s: the steer became the next "
                                "turn — reading it through", info.session_id[:8],
                            )
                            continue
                        if nxt is not None:
                            resolve_bg_command_frame(info.session_id, nxt)
                            reconcile_background_snapshot(info.session_id, nxt)
                    # Interactive chat: no settle — tell satellite to stop.
                    await send_stop_turn(cm, info)
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                # Task settle mode: translator resets parsing state, keeps counters.
                settle.enter_settle()

    async def drain_bg_commands(self, info: "RemoteSessionInfo", cm, *, budget: float) -> bool:
        """Resolve background bash-command completions between turns. The
        satellite forwards claude's stdout CONTINUOUSLY into the per-session
        ``event_queue`` (a persistent WS pipe), so post-turn
        ``system task_updated{patch.status:completed}`` frames just accumulate
        there. This pulls them with a short budget, resolving each via
        ``resolve_bg_command_frame`` (badge clear + registry). Returns True if any
        resolved. The post-turn bg-command monitor (stream_pump.py) polls this —
        bg bash has no completion hook, so this active read is the only signal.
        Acquires the session lock with a short timeout so it never races an
        in-flight turn (the lock blocks the next turn's send_message, so we only
        ever see idle post-turn frames)."""
        try:
            await asyncio.wait_for(info.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — retry next poll
        progressed = False
        try:
            from core.events import wake_capture
            from core.remote.remote_bg_commands import queue_frame_reader
            from core.session.session_state import (
                reconcile_background_snapshot, resolve_bg_command_frame,
            )
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(info.event_queue.get(), timeout=0.4)
                except asyncio.TimeoutError:
                    break  # no more buffered frames right now
                if not isinstance(raw, dict):
                    continue  # synthetic marker (_turn_ended, None) — ignore
                info.last_activity = time.monotonic()
                if wake_capture.is_wake_init(raw):
                    # A self-wake turn (2.1.243) forwarded by the satellite's
                    # post-turn drain — record it as a real chat turn (twin
                    # of the local drain). Ignores the drain budget: the
                    # bracket has its own ceiling; abandoning it mid-turn
                    # would orphan the remaining frames.
                    await wake_capture.capture_wake_turn(
                        info.session_id, raw, queue_frame_reader(info),
                        source="remote_idle_drain",
                    )
                    progressed = True
                    continue
                if resolve_bg_command_frame(info.session_id, raw):
                    progressed = True
                reconcile_background_snapshot(info.session_id, raw)
        finally:
            info.lock.release()
        return progressed

    # --- control -------------------------------------------------------------

    def soft_interrupt_frame(self) -> str:
        return SOFT_INTERRUPT_FRAME

    async def control_request(self, info: "RemoteSessionInfo", cm, subtype: str, **kwargs) -> bool:
        """The CLI's stdin control channel, proxied by the satellite: the
        subtype and kwargs must match what Claude Code expects (same as the
        local ``CLIExecutionLayer.change_model`` / ``change_mode``)."""
        if subtype == "set_model":
            kwargs = {"model": kwargs.get("model", "")}
        elif subtype == "set_permission_mode":
            kwargs = {"mode": kwargs.get("mode", "")}
        await cm.send_fire_and_forget(info.machine_id, {
            "type": CONTROL_REQUEST_FRAME,
            "session_id": info.session_id,
            "subtype": subtype,
            "kwargs": kwargs,
        })
        return True

    async def steer(self, info: "RemoteSessionInfo", cm, text: str) -> bool:
        """Mid-turn steering — the tunnel twin of ``PersistentSession.steer``:
        the satellite writes the user frame into the live turn's stdin
        (``steer_turn``, satellite ≥ 0.5.128; an older one would drop the
        frame silently, so refuse before sending and let the caller queue).
        The result-seen guard is this side's — a foreign result (the resume
        handshake) never sets it, exactly as the local loop — and the accept
        is strict: True only when the satellite reached the pipe. A steer
        that races the result is not lost: the frame becomes the CLI's next
        turn, which ``stream_turn`` reads through while an RPC is pending or
        a frame is written."""
        state: ClaudeRemoteState | None = info.engine_state
        if not text or state is None or state.result_seen:
            return False
        if not cm.satellite_supports_steer_turn(info.machine_id):
            logger.info(
                "remote steer unsupported by satellite %s (< 0.5.128) — "
                "falling back to queue", info.machine_id[:8],
            )
            return False
        # A photo attached to the steer must be on the machine before the
        # CLI reads its path — the same barrier the turn dispatch runs.
        from core.remote import upload_inflight
        await upload_inflight.wait_settled(info.agent_name)
        state.steer_pending += 1
        try:
            ack = await cm.send_command(info.machine_id, {
                "type": STEER_FRAME,
                "session_id": info.session_id,
                "text": text,
            }, timeout=15.0)
        except RuntimeError as e:
            logger.warning("remote steer RPC failed for %s: %s", info.session_id[:8], e)
            return False
        finally:
            state.steer_pending -= 1
        if not ack.get("steered"):
            return False
        state.steer_written = True
        info.last_activity = time.monotonic()
        logger.info(
            "Remote session %s: steered the live turn (%d chars)",
            info.session_id[:8], len(text),
        )
        return True

    # --- resume --------------------------------------------------------------

    async def can_resume(
        self, cm, *, machine_id: str, session_id: str, agent_name: str,
        username: str, resume_handle: str,
    ) -> bool:
        """The satellite owns the CLI session JSONL
        (``~/<otodock>/agents/<slug>/users/<u>/.claude/projects/<hash>/
        <session_id>.jsonl``). After an ``abort()`` (or idle-reap) the
        subprocess exits but the file claude-code wrote during the turn
        remains on disk — ``--resume`` from a new subprocess picks it up.
        Ask the machine to stat it: a blind ``--resume`` against a missing or
        empty file makes claude-code silently start a fresh session and the
        chat memory evaporates."""
        if not cm.is_connected(machine_id):
            # Satellite unreachable — can't validate. Refuse to resume so
            # the dashboard takes the fresh-session branch instead of
            # claude-code silently materializing an empty session.
            return False
        try:
            ack = await cm.send_command(machine_id, {
                "type": RESUME_PROBE_FRAME,
                "session_id": session_id,
                "agent_slug": agent_name,
                "username": username,
            }, timeout=5.0)
        except Exception as e:
            logger.warning(
                "check_session_resumable RPC failed for %s: %s — refusing resume",
                session_id[:8], e,
            )
            return False
        return bool(ack.get("resumable"))


# --- helpers the turn stream uses --------------------------------------------

async def _peek_after_result(info: "RemoteSessionInfo", expected_command_id: str) -> dict | None:
    """The frame right after the driven result, or None when nothing follows
    within the steer grace (the satellite's loop is still forwarding). A
    stale ``_turn_ended`` from the previous turn is skipped like everywhere
    else in this stream; a ``None`` sentinel (session gone) ends the peek."""
    deadline = time.monotonic() + _STEER_INIT_GRACE_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(info.event_queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if not isinstance(raw, dict):
            return None
        info.last_activity = time.monotonic()
        if raw.get("type") == "_turn_ended":
            ended = raw.get("command_id", "")
            if expected_command_id and ended and ended != expected_command_id:
                continue
            return None
        return raw


async def send_stop_turn(cm, info: "RemoteSessionInfo") -> None:
    """Tell the satellite to exit its stdout read loop for this turn. If
    background work is still pending, ask the satellite to keep draining
    + forwarding stdout post-turn (``drain_bg``) so the completion frames
    — which claude emits idle, AFTER this turn ends — still reach the
    proxy (the satellite's per-turn forwarder otherwise stops here,
    stranding the badge + skipping the autonudge). Armed for pending
    SUBAGENTS too — their completion also triggers the CLI's self-wake turn
    on stdout, which the proxy-side drain captures; the satellite's drain
    loop forwards every frame unfiltered (verified 0.5.110
    ``_bg_drain_loop``), so no satellite change/gate is needed."""
    from core.events.bg_command_state import get_bg_command_registry
    from core.session.session_state import get_subagent_registry
    drain_bg = (get_bg_command_registry(info.session_id).has_pending
                or get_subagent_registry(info.session_id).has_pending)
    try:
        await cm.send_fire_and_forget(info.machine_id, {
            "type": STOP_TURN_FRAME,
            "session_id": info.session_id,
            "drain_bg": drain_bg,
        })
    except Exception as e:
        logger.warning(
            "Remote stop_turn send failed for session %s: %s", info.session_id[:8], e,
        )


async def drain_until_turn_ended(
    info: "RemoteSessionInfo", *, timeout: float, expected_command_id: str = "",
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


def _server_to_manifest_name() -> dict[str, str]:
    """``mcpServers`` keys are ``server_name``s; the satellite MCP sync speaks
    manifest names."""
    from services.mcp import mcp_registry
    return {
        (m.server_name or m.name): n
        for n, m in mcp_registry.get_all_manifests().items()
    }
