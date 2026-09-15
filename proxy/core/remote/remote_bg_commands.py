"""Between-turns reads of a remote session's event queue (mixin).

``drain_bg_commands`` resolves background bash completions (and captures a
self-wake turn) from the frames the satellite forwards between turns; the
Codex twin reconciles the registry against the satellite's terminal list
instead. ``_make_queue_frame_reader`` is the queue reader the wake-bracket
capture uses here and in ``send_message``'s stale drain. Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.session.session_state import resolve_bg_command, resolve_bg_command_frame
from core.events.bg_command_state import get_bg_command_registry

if TYPE_CHECKING:
    from core.remote.remote_session_info import RemoteSessionInfo  # noqa: F401 (quoted annotations)

logger = logging.getLogger("remote-layer")


class RemoteBgCommandsMixin:
    # --- Background commands between turns ---

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        """Resolve background bash-command completions for a REMOTE CLI session
        between turns. The satellite forwards claude's stdout CONTINUOUSLY into
        the per-session ``event_queue`` (a persistent WS pipe), so post-turn
        ``system task_updated{patch.status:completed}`` frames just accumulate
        there. This pulls them with a short budget, resolving each via
        ``resolve_bg_command_frame`` (badge clear + registry). Returns True if any
        resolved. The post-turn bg-command monitor (stream_pump.py) polls this —
        bg bash has no completion hook, so this active read is the only signal.

        CODEX: a Codex session's ``event_queue`` is owned by the router
        (``_route_remote_notifications``), whose OOB hook already resolves a
        between-turns ``item/completed`` — so the codex branch never reads the
        queue; it reconciles the registry against the satellite's
        ``codex_bg_terminals`` pull RPC instead (the loss-window backstop,
        mirroring the local session's drain).
        Acquires the session lock with a short timeout so it never races an
        in-flight turn (the lock blocks the next turn's send_message, so we only
        ever see idle post-turn frames)."""
        info = self._sessions.get(session_id)
        if info is None:
            return False
        if info.execution_path == "codex-cli":
            return await self._drain_codex_bg_commands(info, budget=budget)
        try:
            await asyncio.wait_for(info.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — retry next poll
        progressed = False
        try:
            from core.events import wake_capture
            from core.session.session_state import reconcile_background_snapshot
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
                        session_id, raw, self._make_queue_frame_reader(info),
                        source="remote_idle_drain",
                    )
                    progressed = True
                    continue
                if resolve_bg_command_frame(session_id, raw):
                    progressed = True
                reconcile_background_snapshot(session_id, raw)
        finally:
            info.lock.release()
        return progressed

    def _make_queue_frame_reader(self, info: "RemoteSessionInfo"):
        """Frame reader over ``info.event_queue`` for a wake-bracket capture:
        skips synthetic markers, bumps ``last_activity`` (idle reaper), None
        on timeout. The caller holds ``info.lock`` for the whole capture."""
        async def _read(timeout: float) -> dict | None:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    raw = await asyncio.wait_for(
                        info.event_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
                if isinstance(raw, dict):
                    info.last_activity = time.monotonic()
                    return raw
        return _read

    async def _drain_codex_bg_commands(
        self, info: "RemoteSessionInfo", *, budget: float,
    ) -> bool:
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
        if not (info.bg_supervised
                and self._cm.supports_codex_bg_terminals(info.machine_id)):
            return False
        reg = get_bg_command_registry(info.session_id)
        if not reg.has_pending:
            return False
        if not info.alive or info.cli_dead:
            return False
        if time.monotonic() - info._last_bg_terminals_list < 1.0:
            return False
        try:
            await asyncio.wait_for(info.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — its translator resolves these
        try:
            info._last_bg_terminals_list = time.monotonic()
            try:
                ack = await self._cm.send_command(info.machine_id, {
                    "type": "codex_bg_terminals",
                    "session_id": info.session_id,
                }, timeout=min(budget, 5.0))
            except Exception:
                return False  # satellite unreachable/slow — nothing resolved
            if not ack.get("ok"):
                return False  # satellite couldn't list — fail-closed
            rows = [t for t in (ack.get("terminals") or []) if isinstance(t, dict)]
            live_items = {t.get("itemId") for t in rows}
            live_pids = {t.get("processId") for t in rows}
            pid_by_item: dict[str, str] = {}
            if info.codex_translator is not None:
                pid_by_item = {
                    p["item_id"]: p["process_id"]
                    for p in info.codex_translator.pending_bg_commands()
                }
            progressed = False
            for iid in reg.spawned - reg.completed:
                pid = pid_by_item.get(iid)
                if iid in live_items or (pid and pid in live_pids):
                    continue  # still running — its exit resolves it later
                if resolve_bg_command(info.session_id, iid, "completed"):
                    progressed = True
                if info.codex_translator is not None:
                    info.codex_translator._bg_commands.pop(iid, None)
            return progressed
        finally:
            info.lock.release()
