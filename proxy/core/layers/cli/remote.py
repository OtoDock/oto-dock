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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import config as app_config
from core.background import spawn
from core.events import turn_ending, turn_life
from core.events.common_events import DONE, ERROR, CommonEvent
from core.execution_layer import (
    AgentConfig,
    RemoteEngineAdapter,
    RemoteStartContext,
    RemoteStartPlan,
    ResumeVerdict,
)
from core.layers.cli import usage as claude_usage
from core.layers.cli.helpers import _STEER_INIT_GRACE_S, _THINKING_SIG_RE
from core.layers.cli.layer import CLIExecutionLayer, cli_chunk_to_events
from core.layers.cli.oauth import REMOTE_401_WAIT_MS
from core.layers.cli.settle import (
    FOREIGN_SKIP_SILENCE_S,
    ForeignSkipGate,
    SettleController,
    chunk_is_content,
    is_foreign_result,
    result_chunks,
)
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.permission_modes import ASK_MODE, AUTO_REVIEW_MODE, cli_mode

if TYPE_CHECKING:
    from core.remote.remote_session_info import RemoteSessionInfo

logger = logging.getLogger("remote-layer")

# The start-payload keys the satellite's CLISession / PtySession read — this
# engine's wire vocabulary, frozen with the satellites in the field.
CREDENTIALS_PAYLOAD_KEY = "credentials_json"
CONFIG_DIR_PAYLOAD_KEY = "claude_dir_relative"
# The session's home in that dir (``SessionHomeSpec.to_dict``): present only
# for a satellite that reports ``session_homes``.
SESSION_HOME_PAYLOAD_KEY = "session_home"
MCP_CONFIG_PAYLOAD_KEY = "mcp_config"

# The satellite frames this engine's session answers to.
SOFT_INTERRUPT_FRAME = "interrupt_turn"     # control_request {interrupt} into the CLI's stdin
STOP_TURN_FRAME = "stop_turn"               # exit the satellite's stdout read loop
CONTROL_REQUEST_FRAME = "control_request"   # set_model / set_permission_mode → CLI stdin
STEER_FRAME = "steer_turn"                  # a user frame into the live turn's stdin
RESUME_PROBE_FRAME = "check_session_resumable"  # read the session JSONL on the machine
# One call of ``can_resume`` waits this long for the probe's answer; the probe
# itself stays outstanding across calls (``_probes``).
_PROBE_WAIT_S = 5.0


@dataclass
class _OutstandingProbe:
    """A resume probe sent and not yet answered: the connection it went out
    on (a new connection means a new probe), its command id and its ack."""

    conn: object
    command_id: str
    future: asyncio.Future


_probes: dict[tuple[str, str], _OutstandingProbe] = {}


@dataclass
class ClaudeRemoteState:
    """Per-turn state of a Claude session on a machine: the translator (a
    pure state machine, fresh every turn), the settle controller that owns
    the turn-end decision, and the steer bookkeeping — ``result_seen``
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
        # A terminal's first message waits for its stand-ins where they say
        # when the engine has their lists (a compute machine always runs the
        # platform's own version; a paired one says so).
        reports = getattr(ctx.cm, "satellite_reports_listed", None)
        if config.interactive and (ctx.spawn is not None or (
                callable(reports) and reports(ctx.machine_id))):
            from core.layers.cli.layer import expect_terminal_tools
            expect_terminal_tools(ctx.session_id, config)
        if ctx.spawn is not None:
            # A compute machine runs the plan's argv (the flags, the prompt
            # file, the MCP config and the login file are the platform
            # folder's); it reads the config dir for the transcript tail
            # and the resume words alone.
            payload[CONFIG_DIR_PAYLOAD_KEY] = ctx.spawn.config_dir_relative
            spec = self.layer.capabilities.auth.session_home
            if config.session_home and spec is not None:
                # A machine with its own copy makes the home's links itself.
                payload[SESSION_HOME_PAYLOAD_KEY] = spec.to_dict()
            payload["resume"] = config.resume
            payload["session_id_for_resume"] = ctx.session_id if config.resume else ""
            return RemoteStartPlan(payload=payload,
                                   credential_file_delivered=ctx.spawn.credential_file)
        # CLI effort: xhigh→max for models that don't support xhigh — the
        # satellite has no model registry, so the proxy is the single source
        # of truth (as core/layers/cli/spawn.py does for the argv). "ultra" is Codex-only
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
        # A home of its own in that dir (``session_config_dir.session_home_for``
        # decided it for a satellite that makes homes): the satellite makes it
        # by the engine's declaration and points the engine there.
        spec = self.layer.capabilities.auth.session_home
        if config.session_home and spec is not None:
            payload[SESSION_HOME_PAYLOAD_KEY] = spec.to_dict()
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
        # claude.ai Cron/Trigger/Push/Artifact/integration tools — one source of
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
                    from core.remote.remote_mcp_rewrite import (
                        _rewrite_mcp_json_for_remote,
                    )
                    payload[MCP_CONFIG_PAYLOAD_KEY] = _rewrite_mcp_json_for_remote(
                        json.loads(mcp_path.read_text()), ctx.sat_port,
                        target_os=ctx.target_os, session_id=ctx.session_id,
                        secret_bundle_keys=ctx.secret_bundle_keys,
                        proxy_api_key=ctx.proxy_api_key,
                        gateway=ctx.gateway,
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

    def init_session(self, info: RemoteSessionInfo, config: AgentConfig, cm) -> None:
        # The translator and settle controller are per TURN (begin_turn);
        # nothing lives across turns for this engine.
        info.engine_state = None

    def adopt_state(self, info: RemoteSessionInfo, cm=None) -> None:
        """Mode C: the replayed turn streams through a fresh translator and an
        interactive settle (the buffered turn's sentinel / turn_ended closes
        it). The MCP server keys the satellite reported become manifest
        names, as a start's are."""
        server_to_name = _server_to_manifest_name()
        info.used_mcps = {server_to_name.get(key, key) for key in info.used_mcps}
        translator = ClaudeCLIEventTranslator(info.session_id)
        info.engine_state = ClaudeRemoteState(
            translator=translator,
            settle=SettleController(info.session_id, 0, translator),
        )

    # --- turns ---------------------------------------------------------------

    async def begin_turn(self, info: RemoteSessionInfo, settle_after_result: int) -> None:
        from core.events.bg_command_state import reset_bg_command_registry
        from core.session.session_state import reset_subagent_registry
        reset_subagent_registry(info.session_id)
        reset_bg_command_registry(info.session_id)
        translator = ClaudeCLIEventTranslator(info.session_id)
        info.engine_state = ClaudeRemoteState(
            translator=translator,
            settle=SettleController(info.session_id, settle_after_result, translator),
        )

    async def stream_turn(self, info: RemoteSessionInfo, cm) -> AsyncIterator[CommonEvent]:
        """Stream one CLI turn to the pump using the shared translator + settle.

        The satellite is a dumb pipe: it forwards every NDJSON line as a
        session_event. This owns the turn-end decision and sends ``stop_turn``
        to the satellite when the turn is over — also when the consumer
        stops early (an ERROR it ended on, a cancel), so the satellite's
        per-turn reader never outlives the turn.
        """
        turn = {"closed": False}
        try:
            async for event in self._stream_turn(info, cm, turn):
                yield event
        finally:
            if not turn["closed"]:
                _stop_turn_in_background(cm, info)

    async def _stream_turn(self, info: RemoteSessionInfo, cm, turn: dict) -> AsyncIterator[CommonEvent]:
        from core.events import wake_capture
        from core.session import engine_prompts
        from core.session.background_frames import resolve_background_frame
        state = cast("ClaudeRemoteState", info.engine_state)
        assert state is not None  # invariant: begin_turn / adopt_state ran
        translator, settle = state.translator, state.settle

        expected_cmd_id = info.current_send_command_id
        # Foreign-result skip regime (see settle.ForeignSkipGate): resume
        # handshake / settlement results must not close the driven turn —
        # the valve, not a count, ends the regime.
        gate = ForeignSkipGate(info.session_id, translator, own_message=info.own_message_turn)
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
                    turn["closed"] = True
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                if not settle.settling or settle.wake_open:
                    # Pre-settle silence, or silence inside the CLI's own wake
                    # turn open in the settle (a turn before its result
                    # too): the turn keeps its life (an open
                    # tool, a prompt on a person, background work, a recent
                    # hook) or ends typed past the ceiling, the twin of the
                    # local drive loop. The satellite's reader is stopped
                    # here and the remote turn's kill branch ends the CLI
                    # (``silent`` is in ``KILLS_PROCESS``), so the next
                    # message resumes the session. During the reconnect
                    # grace the turn may still arrive: the grace's own
                    # expiry closes it.
                    silent_for = time.monotonic() - info.last_event_at
                    if (cm.is_session_in_grace(info.machine_id, info.session_id)
                            or not _silence_ends(info, translator)):
                        continue
                    logger.warning(
                        "Remote session %s: no events for %.0fs with nothing running — "
                        "ending the turn", info.session_id[:8], silent_for,
                    )
                    await send_stop_turn(cm, info)
                    turn["closed"] = True
                    ending = turn_ending.TurnEnding(
                        reason=turn_ending.SILENT, detail=f"no output for {int(silent_for)} s",
                    )
                    yield CommonEvent(type=ERROR, data={"message": ending.line(),
                                                        "ending": ending.as_dict()})
                    yield CommonEvent(type=DONE)
                    return
                settle.maybe_log_heartbeat(proc_alive=True)
                if settle.should_exit_on_silence(timeout):
                    await send_stop_turn(cm, info)
                    turn["closed"] = True
                    # Wait briefly for satellite to drain + ack turn_ended
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                continue

            if raw is None:
                # Session ended on satellite (process exit) — terminal. After
                # the proxy's own kill (a Stop, the watchdog) that is the
                # kill, not a lost machine.
                turn["closed"] = True
                if info.proxy_killed:
                    yield CommonEvent(type=DONE)
                    return
                # The CLI is gone: the next send resumes it instead of
                # meeting the dead process first.
                info.cli_dead = True
                for event in _lost_events("the machine ended the session mid-turn"):
                    yield event
                return

            # An event arrived — slide the post-skip valve (never disarm:
            # noise must not remove the skip regime's only closer).
            gate.note_event()

            rtype = raw.get("type", "")
            # Stale-turn filter: the satellite tags each CLI turn event with
            # the send_message command it was streamed under. A result/error
            # from a PREVIOUS turn (the replaced process's dying flush racing
            # past the send-start drain) must not terminate THIS turn.
            # An untagged event passes through, and a content-type event
            # passes regardless (a late bg task_updated frame from the prior
            # turn must still reach the registries).
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
                # The satellite's own send failed (no reader is open), or the
                # reconnect grace expired on a severed stream.
                turn["closed"] = True
                for event in _satellite_error_events(raw):
                    yield event
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
                turn["closed"] = True
                for event in _turn_end_events(info, state, settle, raw):
                    yield event
                return

            # Feed the translator and forward all chunks. Content counts
            # only from events streamed under THIS command (untagged passes
            # for old satellites): content-typed stale events deliberately
            # bypass the stale filter above, and a dying prior process's
            # junk text must not flip the next zero-content result to
            # "real" and close the turn on it.
            info.last_activity = info.last_event_at = time.monotonic()
            # An init DURING settle = the CLI's self-wake review turn running
            # INLINE through this stream (its content joins the task output).
            # The settle holds until its result, where the producer and the
            # monitors learn the review ran. Twin of the local session loop's
            # marker.
            if (settle.settling and rtype == "system"
                    and raw.get("subtype") == "init"):
                settle.note_wake_open()
            engine_prompts.note_frame(info.session_id, raw, in_turn=True)
            # A result's chunks wait for the foreign check below: a skipped
            # result's metadata (its own time and cost) is not the turn's.
            held: list = []
            for chunk in translator.feed(raw):
                if chunk.event_type == "rate_limit":
                    # The account's window state: for the pool, never the chat.
                    claude_usage.record_event_async(info.session_id, chunk.event_data)
                    continue
                if (chunk_is_content(chunk)
                        and (not ev_cmd or ev_cmd == expected_cmd_id)):
                    gate.note_content()
                if rtype == "result":
                    held.append(chunk)
                    continue
                for event in cli_chunk_to_events(chunk):
                    yield event

            # Result-event book-keeping — turn-end logic. A foreign result
            # (resume handshake / zero-content settlement mini-turn) never
            # ends the turn in EITHER mode: interactive must not close on
            # it, and task mode must not enter settle on it (the job-done
            # 5s fast-grace would close the turn just the same).
            if rtype == "result":
                skipped = not settle.settling and is_foreign_result(
                    raw, gate.content_since_skip,
                )
                if skipped:
                    gate.record_skip(raw)
                for event in [e for c in result_chunks(held, skipped) for e in cli_chunk_to_events(c)]:
                    yield event
                if skipped:
                    continue
                if info.post_interrupt_watch:
                    if raw.get("is_error") and _THINKING_SIG_RE.search(str(raw.get("result", ""))):
                        info.post_interrupt_watch = False
                        logger.warning(
                            "Remote session %s: thinking-signature error after an "
                            "interrupt; ending the process for a resume", info.session_id[:8],
                        )
                        spawn(self._repair_wedge(info), name="repair_wedge", owner="remote-layer")
                    elif not raw.get("is_error"):
                        info.post_interrupt_watch = False
                if settle.note_wake_result():
                    # The wake turn ran to its result: it is the review of
                    # the finished background work.
                    wake_capture.note_captured(info.session_id)
                state.result_seen = True
                if settle.is_interactive_done():
                    if state.steer_written or state.steer_pending:
                        # A steer the turn did not consume becomes the
                        # CLI's next turn by itself: its init follows the
                        # result at once, and this stream reads that turn
                        # through. The
                        # satellite's loop is still reading — no stop_turn
                        # was sent — so the frames keep coming.
                        nxt = await _peek_after_result(info, expected_cmd_id)
                        state.steer_written = False
                        if nxt is not None and wake_capture.opens_turn(nxt):
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
                            resolve_background_frame(info.session_id, nxt)
                            engine_prompts.note_frame(info.session_id, nxt)
                    # Interactive chat: no settle — tell satellite to stop.
                    await send_stop_turn(cm, info)
                    turn["closed"] = True
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                if settle.stopped():
                    # The person stopped the turn and the interrupt closed it
                    # with this result: nothing to settle for (the background
                    # work stays with the warm process), and the turn ends
                    # before the interrupt watchdog could take it for stuck.
                    logger.info(
                        "Remote session %s: the person stopped the task turn — "
                        "ending it at its result", info.session_id[:8],
                    )
                    await send_stop_turn(cm, info)
                    turn["closed"] = True
                    await drain_until_turn_ended(
                        info, timeout=2.0, expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                # Task settle mode: translator resets parsing state, keeps counters.
                settle.enter_settle()

    async def drain_bg_commands(self, info: RemoteSessionInfo, cm, *, budget: float) -> bool:
        """The monitors' poll of the session between turns: the one reader
        between turns (``remote_bg_commands.read_between_turns``) for up to
        ``budget``, resolving background lines (badge clear + registry) and
        recording a turn the engine starts by itself. Returns True if any
        work ended. Takes the session lock with a short timeout so it never
        races an in-flight turn (the lock blocks the next turn's
        send_message, so only idle post-turn frames are seen)."""
        try:
            await asyncio.wait_for(info.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — retry next poll
        try:
            from core.remote.remote_bg_commands import read_between_turns
            progressed, _ = await read_between_turns(
                info, cm, quiet_s=0.4, budget_s=budget, source="remote_idle_drain")
            return progressed
        finally:
            info.lock.release()

    # --- control -------------------------------------------------------------

    def soft_interrupt_frame(self) -> str:
        return SOFT_INTERRUPT_FRAME

    def note_interrupt(self, info: RemoteSessionInfo) -> None:
        from core.events import wake_capture
        state = cast("ClaudeRemoteState | None", info.engine_state)
        if state is not None:
            state.translator.interrupted = True
        wake_capture.note_interrupt(info.session_id)
        info.post_interrupt_watch = True

    async def _repair_wedge(self, info: RemoteSessionInfo) -> None:
        """The thinking-signature error after an interrupt: the CLI's
        process is ended through the layer's hard abort (the next send
        resumes it by its handle, which drops the unsigned block) and the
        chat's cancelled context is armed again, as the local session's
        kill does."""
        from core.session.session_manager import _get_remote_layer
        await _get_remote_layer()._hard_abort(info.session_id)
        await _arm_cancelled_context(info.session_id)

    def turn_settled(self, info: RemoteSessionInfo) -> bool:
        state = cast("ClaudeRemoteState | None", info.engine_state)
        return state is not None and state.settle.settling

    async def control_request(self, info: RemoteSessionInfo, cm, subtype: str, **kwargs) -> bool:
        """The CLI's stdin control channel, proxied by the satellite (every
        released satellite writes the frame to the CLI verbatim): the subtype
        and kwargs must match what Claude Code expects (same as the local
        ``CLIExecutionLayer.change_model`` / ``change_mode``), so a platform
        mode goes as the CLI's own word."""
        if subtype == "set_model":
            kwargs = {"model": kwargs.get("model", "")}
        elif subtype == "set_permission_mode":
            kwargs = {"mode": cli_mode(kwargs.get("mode", ""))}
        await cm.send_fire_and_forget(info.machine_id, {
            "type": CONTROL_REQUEST_FRAME,
            "session_id": info.session_id,
            "subtype": subtype,
            "kwargs": kwargs,
        })
        return True

    async def reenter_review(self, info: RemoteSessionInfo, cm) -> bool:
        """The CLI's pause cleared by leaving auto and coming back, two
        control frames the satellite writes to its stdin."""
        await self.control_request(info, cm, "set_permission_mode", mode=ASK_MODE)
        await self.control_request(info, cm, "set_permission_mode", mode=AUTO_REVIEW_MODE)
        return True

    async def steer(self, info: RemoteSessionInfo, cm, text: str) -> bool:
        """Mid-turn steering: the satellite writes the user frame into the
        live turn's stdin (``steer_turn``). The result-seen guard is this
        side's: a foreign result (the resume handshake) never sets it. The
        accept is strict: True only when the satellite reached
        the pipe. A steer that races the result is not lost: the frame
        becomes the CLI's next turn, which ``stream_turn`` reads through
        while an RPC is pending or a frame is written. A turn the engine
        started by itself (``info.engine_turn``) takes a steer the same way on
        a satellite that streams between turns, and its record reads a late
        one through; an older satellite refuses it there, so the message
        takes the stop-and-send path."""
        engine = info.engine_turn
        if engine is not None and not cm.satellite_streams_between_turns(info.machine_id):
            return False
        state = engine if engine is not None else cast("ClaudeRemoteState | None", info.engine_state)
        if not text or state is None or state.result_seen:
            return False
        # A turn parked on a permission card: the card is the question, and
        # a typed message keeps the auto-deny plus queue path.
        from core.events.stream_pump import _pending_permissions
        if _pending_permissions.get(info.session_id):
            return False
        if engine is None and _silence_ends(info, state.translator):
            # The turn is about to end silent: the frame would be lost with
            # the process, so the caller queues it for the next turn.
            return False
        # A photo attached to the steer must be on the machine before the
        # CLI reads its path — the same barrier the turn dispatch runs.
        from core.remote import upload_inflight
        if info.files_synced and not await upload_inflight.wait_settled(
                info.agent_name, machine_id=info.machine_id,
                ceiling=upload_inflight.BARRIER_TIMEOUT_S):
            text = await upload_inflight.turn_goes_on_without_files(
                info.session_id, info.machine_id, info.agent_name, text)
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

    def probe_future(self, cm, *, machine_id: str, session_id: str) -> asyncio.Future | None:
        held = _probes.get((machine_id, session_id))
        if held is None or held.conn is not cm.get_connection(machine_id):
            return None
        return held.future

    def forget_probe(self, cm, *, machine_id: str, session_id: str) -> None:
        held = _probes.pop((machine_id, session_id), None)
        if held is not None:
            cm.forget_command(held.command_id)

    async def can_resume(
        self, cm, *, machine_id: str, session_id: str, agent_name: str,
        username: str, resume_handle: str, external_home: str = "",
    ) -> ResumeVerdict:
        """The satellite owns the CLI session JSONL
        (``~/<otodock>/agents/<slug>/users/<u>/.claude/projects/<hash>/
        <session_id>.jsonl``). After an ``abort()`` (or idle-reap) the
        subprocess exits but the file claude-code wrote during the turn
        remains on disk — ``--resume`` from a new subprocess picks it up.
        Ask the machine to read it: a blind ``--resume`` against a missing or
        empty file makes claude-code silently start a fresh session and the
        chat memory evaporates.

        Only a definite answer decides: ``resumable: true`` is RESUMABLE,
        ``false`` is FRESH. A machine away, a probe unanswered within
        ``_PROBE_WAIT_S``, a failed send or the satellite's ``status: error``
        (a read the machine could not finish) is UNKNOWN: the fresh branch is
        irreversible, and a chat with an intact transcript was once lost to
        a 5 s timeout read as "no". ``settle_resume`` calls again while it
        waits; the probe sent stays outstanding across those calls
        (``_probes``: the frame goes out once per connection, never once per
        call, so a slow machine is not asked to read the same file five
        times), and a reconnect sends a new one. ``forget_probe`` drops it
        when the caller gives up; ``probe_future`` hands it to a watcher."""
        if not cm.is_connected(machine_id):
            return ResumeVerdict.UNKNOWN
        key = (machine_id, session_id)
        conn = cm.get_connection(machine_id)
        held = _probes.get(key)
        future = None
        if held is not None and held.conn is conn:
            # Still waiting, or answered since the last call: the answer is
            # read below. A probe that failed (the machine dropped and came
            # back) or was cancelled is sent again.
            answered = (held.future.done() and not held.future.cancelled()
                        and held.future.exception() is None)
            if not held.future.done() or answered:
                future = held.future
        if future is None:
            if held is not None and not held.future.done():
                cm.forget_command(held.command_id)
            try:
                command_id, future = await cm.begin_command(machine_id, {
                    "type": RESUME_PROBE_FRAME,
                    "session_id": session_id,
                    "agent_slug": agent_name,
                    "username": username,
                })
            except RuntimeError as e:
                logger.warning(
                    "check_session_resumable could not be sent for %s: %s",
                    session_id[:8], e,
                )
                return ResumeVerdict.UNKNOWN
            _probes[key] = _OutstandingProbe(conn, command_id, future)
        try:
            ack = await asyncio.wait_for(asyncio.shield(future), timeout=_PROBE_WAIT_S)
        except asyncio.TimeoutError:
            logger.info(
                "check_session_resumable for %s: no answer in %.0fs, the probe stays out",
                session_id[:8], _PROBE_WAIT_S,
            )
            return ResumeVerdict.UNKNOWN
        except RuntimeError as e:
            # The connection manager failed the ack: the machine dropped. The
            # next call sends a fresh probe on the new connection.
            _probes.pop(key, None)
            logger.warning(
                "check_session_resumable for %s failed: %s", session_id[:8], e,
            )
            return ResumeVerdict.UNKNOWN
        _probes.pop(key, None)
        if ack.get("status") == "error":
            logger.warning(
                "check_session_resumable for %s: the machine could not read (%s)",
                session_id[:8], ack.get("error", ""),
            )
            return ResumeVerdict.UNKNOWN
        if ack.get("resumable"):
            return ResumeVerdict.RESUMABLE
        return ResumeVerdict.FRESH


# --- helpers the turn stream uses --------------------------------------------

async def _peek_after_result(info: RemoteSessionInfo, expected_command_id: str) -> dict | None:
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


_stop_turn_tasks: set[asyncio.Task] = set()


def _stop_turn_in_background(cm, info: RemoteSessionInfo) -> None:
    """``send_stop_turn`` from a turn generator's close: a closing generator
    must not await, so the frame goes out on its own task."""
    try:
        task = asyncio.get_running_loop().create_task(send_stop_turn(cm, info))
    except RuntimeError:
        return
    _stop_turn_tasks.add(task)
    task.add_done_callback(_stop_turn_tasks.discard)


async def send_stop_turn(cm, info: RemoteSessionInfo) -> None:
    """Tell the satellite to exit its stdout read loop for this turn. If
    background work is still pending, ask the satellite to keep draining
    + forwarding stdout post-turn (``drain_bg``) so the completion frames
    — which claude emits idle, AFTER this turn ends — still reach the
    proxy (the satellite's per-turn forwarder otherwise stops here,
    stranding the badge + skipping the autonudge). Armed for pending
    SUBAGENTS too — their completion also triggers the CLI's self-wake turn
    on stdout, which the proxy-side drain captures, and their own lines
    and stdout completions reach the agent monitor's drain; the satellite's
    drain loop forwards every frame unfiltered (verified 0.5.110
    ``_bg_drain_loop``). ``drain_bg_s`` bounds it. Work that ended during
    the turn asks for it too: the CLI's review of that end can follow the
    result at once (``background_frames.read_after_turn``). A satellite that
    streams between turns (``satellite_streams_between_turns``) is asked to
    forward everything until the next turn, so a turn the engine starts by
    itself is read whatever the registries know."""
    from core.session import background_leash
    from core.session.background_frames import read_after_turn
    stream = cm.satellite_streams_between_turns(info.machine_id)
    await cm.send_fire_and_forget(info.machine_id, {
        "type": STOP_TURN_FRAME,
        "session_id": info.session_id,
        "drain_bg": stream or read_after_turn(info.session_id),
        # How long the satellite keeps forwarding: the background-work
        # ceiling, as long as the proxy waits for the work itself.
        "drain_bg_s": background_leash.background_work_ceiling(),
        **({"stream_between_turns": True} if stream else {}),
    })


def _silence_ends(info: RemoteSessionInfo, translator) -> bool:
    """Whether the remote turn's silence (since its own last stream event)
    ends it now, with the translator's open tool calls as its life."""
    return turn_life.ends(
        info.session_id, tools_open=bool(translator.open_tools),
        silent_for=time.monotonic() - info.last_event_at, task=info.task_turn,
    )


async def _arm_cancelled_context(session_id: str) -> None:
    """The chat's flags for the cancelled-context line the graceful
    interrupt suppressed: the next turn tells the agent its last turn was
    cut (``last_abort_graceful`` off)."""
    from storage import database as task_store
    from storage.pg import run_db
    try:
        chat = await run_db(task_store.get_chat_by_session, session_id)
        if chat:
            await run_db(task_store.update_chat, chat["id"],
                         last_turn_aborted=True, last_abort_graceful=False)
    except Exception:
        logger.exception("Remote session %s: the chat's abort flags were not written",
                         session_id[:8])


def _memory_events() -> list[CommonEvent]:
    """The ending of a turn whose engine the machine's memory guard ended,
    and the turn's end; the next message resumes the session."""
    ending = turn_ending.TurnEnding(reason=turn_ending.EXITED, detail=turn_ending.MEMORY)
    return [CommonEvent(type=ERROR, data={"message": ending.line(), "ending": ending.as_dict()}),
            CommonEvent(type=DONE)]


def _turn_end_events(info: RemoteSessionInfo, state: ClaudeRemoteState, settle: SettleController,
                     raw: dict) -> list[CommonEvent]:
    """A machine's turn end as the turn's reader ends on it. After the
    proxy's own kill it is that kill: a plain end, whatever the machine says."""
    reason = "" if info.proxy_killed else raw.get("reason")
    if reason == "memory":
        # The machine's memory guard ended the engine.
        info.cli_dead = True
        return _memory_events()
    if reason == "exited":
        # The machine says the engine's process exited in the turn. Before
        # its result, or inside its own wake turn open in the settle, a turn
        # was cut: the exited ending. After the result the answer is whole.
        # Either way the process is gone, and the next send resumes the session.
        info.cli_dead = True
        if not state.result_seen or settle.wake_open:
            return _exited_events(raw)
        return [CommonEvent(type=DONE)]
    if not state.result_seen and not settle.settling and not info.proxy_killed:
        # The satellite closed the turn before the CLI's result: its
        # process went, and what it ran may not be on disk.
        info.cli_dead = True
        return _lost_events("the machine ended the turn before its result")
    return [CommonEvent(type=DONE)]


def _exited_events(raw: dict) -> list[CommonEvent]:
    """The ``exited`` ending of a turn whose engine the machine says exited
    in it (``satellite_connection.turn_end_of``): the exit code and the last
    line the engine said, then the turn's end."""
    code, line = raw.get("exit_code"), raw.get("last_line") or ""
    detail = f"exit {code if code is not None else 'unknown'}" + (f": {line}" if line else "")
    ending = turn_ending.TurnEnding(reason=turn_ending.EXITED, detail=detail, exit_code=code)
    return [CommonEvent(type=ERROR, data={"message": ending.line(), "ending": ending.as_dict()}),
            CommonEvent(type=DONE)]


def _lost_events(detail: str) -> list[CommonEvent]:
    """The ``lost`` ending (the machine's stream or process gone mid-turn)
    and the turn's end."""
    ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail=detail)
    return [CommonEvent(type=ERROR, data={"message": ending.line(), "ending": ending.as_dict()}),
            CommonEvent(type=DONE)]


def _satellite_error_events(raw: dict) -> list[CommonEvent]:
    """A satellite ``error`` frame as the turn's typed end: the reconnect
    grace's expiry (``durable_marker``, the stream severed) is ``lost``, a
    refused send is ``error`` with the satellite's words."""
    message = str(raw.get("message") or "the machine reported an error")
    if raw.get("durable_marker"):
        return _lost_events(str(raw.get("detail") or message))
    ending = turn_ending.TurnEnding(reason=turn_ending.ERROR, detail=message)
    data = {k: v for k, v in raw.items() if k != "type"}
    data.update({"message": ending.line(), "ending": ending.as_dict()})
    return [CommonEvent(type=ERROR, data=data), CommonEvent(type=DONE)]


async def drain_until_turn_ended(
    info: RemoteSessionInfo, *, timeout: float, expected_command_id: str = "",
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
        if isinstance(raw, dict):
            # The turn is over: what arrives here is mostly background work (a
            # sub-agent's lines, a command's or an agent's end, a sub-agent's
            # ask for the engine prompts). It can also be the CLI's own next
            # turn (a steer it took late, a wake): from its opening line on,
            # its lines are kept whole for the reader between turns, which
            # records it.
            from core.events import wake_capture
            if info.carried or wake_capture.opens_turn(raw):
                info.carried.append(raw)
                continue
            from core.session import engine_prompts
            from core.session.background_frames import resolve_background_frame
            resolve_background_frame(info.session_id, raw)
            engine_prompts.note_frame(info.session_id, raw)


def _server_to_manifest_name() -> dict[str, str]:
    """``mcpServers`` keys are ``server_name``s; the satellite MCP sync speaks
    manifest names."""
    from services.mcp import mcp_registry
    return {
        (m.server_name or m.name): n
        for n, m in mcp_registry.get_all_manifests().items()
    }
