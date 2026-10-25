"""Remote sessions across restarts and drops: adopt, resume, liveness (mixin).

Re-adopting sessions the satellite kept alive across a proxy restart
(``adopt_session`` replays the retained turn, ``adopt_idle_session`` only
re-registers), the resume decision (``can_resume_session`` verifies the
transcript on the satellite), ``prepare_resume``, and the liveness reads
the dashboard and the reapers use (dead process, grace window, idle
seconds, severed stream). Mixed into RemoteExecutionLayer; split out of
remote_execution.py.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import psycopg

from core import placement
from core.events import turn_ending
from core.events.bg_command_state import reset_bg_command_registry
from core.events.common_events import DONE, ERROR, TEXT, CommonEvent
from core.execution_layer import RECONNECT_WAIT_S, MachineReach, ResumeVerdict
from core.remote.remote_session_info import RemoteSessionInfo
from core.session.session_state import (
    _record_session_use,
    clear_starting,
    mark_closing,
    reset_subagent_registry,
    restore_session_state,
)

logger = logging.getLogger("remote-layer")

#: A recovery adoption's first event when the satellite begins its replay
#: (never forwarded to a viewer): ``{"truncated"}``, the satellite dropped
#: the turn's start. The recovery deletes the rows the turn saved early only
#: when the replay writes the whole turn again (``run_recovery``).
REPLAY_BEGIN = "replay_begin"

# How often a turn start waiting on a reconnecting machine looks again.
_RECONNECT_POLL_S = 0.25


# The resolved targets of the sessions a resume is probing (session id →
# (when, target)): a settle asks where the machine stands between its probe
# rounds and its watcher asks for the probe's future, all on the loop, so
# they read what the probe resolved instead of the store.
_resume_targets: dict[str, tuple[float, object]] = {}
_RESUME_TARGET_TTL_S = 120.0
_RESUME_TARGETS_MAX = 2048


def _remember_resume_target(session_id: str, target) -> None:
    # A store error (UNKNOWN) is not remembered: the next round asks again.
    if target is ResumeVerdict.UNKNOWN:
        return
    now = time.monotonic()
    if len(_resume_targets) >= _RESUME_TARGETS_MAX:
        for sid, (at, _t) in list(_resume_targets.items()):
            if now - at >= _RESUME_TARGET_TTL_S:
                del _resume_targets[sid]
        while len(_resume_targets) >= _RESUME_TARGETS_MAX:
            del _resume_targets[next(iter(_resume_targets))]
    _resume_targets[session_id] = (now, target)


def forget_resume_target(session_id: str) -> None:
    """Drop the session's cached resume target (a chat move changed where
    the session lives: the next probe asks the store again)."""
    _resume_targets.pop(session_id, None)


if TYPE_CHECKING:
    from core.remote.remote_host import RemoteLayerHost as _Host
else:
    _Host = object


class RemoteResumeMixin(_Host):
    # --- Adopt, resume, liveness ---

    @staticmethod
    def _restore_adopted_credentials(
        session_id: str, machine_id: str, agent_name: str, execution_path: str,
    ) -> None:
        """Sync (run via ``to_thread``) — the adopt-time counterpart of
        ``_bind_subscription``. Restores the in-memory subscription binding
        from its persisted row, re-registers the satellite credential file as
        a fan-out target (the engine's declared dirname under the scope root,
        recomputed from the persisted SecurityContext's ``mount_username``
        with the same rule the spawn used), and pushes the CURRENT stored
        token to repair any rotation the restart window swallowed. Best-effort:
        the adopted turn streams fine without it; only future rotations and
        seat accounting depend on this."""
        from core import layout
        from core.session.session_manager import capabilities_for_path
        from core.session.session_state import get_session_security
        from services.engines import subscription_pool, token_fanout
        from storage.billing import subscription_store
        try:
            sub_id = subscription_pool.restore_session_binding(session_id)
            if not sub_id:
                return
            spec = capabilities_for_path(execution_path).auth.credential_file
            if spec is None:
                return  # env-delivered credentials — no file to rewrite
            cred = subscription_store.get_credential_data(sub_id) or {}
            oauth = cred.get("oauth_token") or {}
            if not (isinstance(oauth, dict) and oauth.get("accessToken")):
                return  # API-key subscription — no credential file to rewrite
            ctx = get_session_security(session_id)
            mount_username = getattr(ctx, "mount_username", "") if ctx else ""
            token_fanout.register_session_target(
                session_id,
                token_fanout.CredentialFileTarget(
                    layer=execution_path,
                    machine_id=machine_id,
                    agent_name=agent_name,
                    dir_relative=f"{layout.scope_root(mount_username)}/{spec.dirname}",
                ),
            )
            subscription_pool.fan_out_current_token(sub_id)
        except Exception:
            logger.exception(
                "adopt %s: credential rebind failed (session streams on; "
                "future rotations may miss it until re-warm)", session_id[:8],
            )

    @staticmethod
    def _restore_state(info: RemoteSessionInfo, mode: str, token_floor: int) -> None:
        """The session's mode and token floor as they were before the restart,
        set before the registry insert so its hooks never run in the default
        ``auto`` mode and the floor is the running process's, never "now"."""
        if not mode:
            return
        restore_session_state(info.session_id, mode, token_floor)
        info.mode = mode

    def _adoptable(self, session_id: str, execution_path: str, *, idle: bool = False) -> bool:
        """Whether the engine the satellite names takes this session back: a
        turn in flight only where the turn survives a proxy restart
        (``runtime.supports_reattach_after_restart``), an idle session where
        the engine re-adopts one (``runtime.readopts_idle_session``)."""
        from core.session.session_manager import capabilities_for_path
        try:
            runtime = capabilities_for_path(execution_path).runtime
        except ValueError:
            runtime = None
        if runtime is not None and (runtime.readopts_idle_session if idle
                                    else runtime.supports_reattach_after_restart):
            return True
        logger.info(
            "adopt %s: %s has no live re-adopt — skipped",
            session_id[:8], execution_path,
        )
        return False

    async def adopt_idle_session(
        self, *, machine_id: str, session_id: str, agent_name: str,
        execution_path: str, use_native_permissions: bool = False,
        mode: str = "", token_floor: int = 0, resume_handle: str = "",
        model: str = "", used_mcps=(), allow_full_fs: bool = False,
    ) -> bool:
        """Registry-only twin of ``adopt_session`` for sessions the satellite
        kept alive across a proxy restart with NO turn in flight. Nothing
        streams — the point is that the session EXISTS proxy-side again:
        the idle reaper can leash it, a later chat resume finds it, and
        token rotations reach it. Without this, restart-orphaned idle
        sessions lived forever satellite-side and ate the machine's
        session capacity (the operator's laptop hit its at-capacity wall
        after an evening of proxy redeploys). ``last_activity`` starts
        fresh: one more standard idle leash beats yanking a session a
        user is about to resume. ``mode`` and ``token_floor`` are what the
        session had before the restart (``run_recovery``), restored before
        the insert; ``resume_handle``, ``model``, ``used_mcps`` and
        ``allow_full_fs`` are what a start would have put on the record
        (the report's handle, the machine's file policy now). A start
        spawning this id meanwhile owns it, and from the checks to the
        insert nothing awaits. True when the session was taken back."""
        if session_id in self._spawning:
            return False
        if session_id in self._sessions:
            clear_starting(session_id)
            return False
        if not self._adoptable(session_id, execution_path, idle=True):
            clear_starting(session_id)
            return False
        queue = self._cm.create_session_queue(machine_id, session_id, execution_path)
        info = RemoteSessionInfo(
            session_id=session_id,
            machine_id=machine_id,
            agent_name=agent_name,
            execution_path=execution_path,
            event_queue=queue,
            use_native_permissions=use_native_permissions,
            allow_full_fs=allow_full_fs,
            model=model,
            used_mcps=set(used_mcps),
            resume_handle=resume_handle,
        )
        self._restore_state(info, mode, token_floor)
        self._adapter(info).adopt_state(info, self._cm)
        self._sessions[session_id] = info
        clear_starting(session_id)
        reset_subagent_registry(session_id)
        reset_bg_command_registry(session_id)
        _record_session_use(session_id, client_type="", agent=agent_name)
        await asyncio.to_thread(
            self._restore_adopted_credentials, session_id, machine_id,
            agent_name, info.execution_path,
        )
        await self._reprovision_gateway(session_id, machine_id)
        return True

    @staticmethod
    async def _reprovision_gateway(session_id: str, machine_id: str) -> None:
        """The session's gateway credentials, re-provisioned from its
        descriptor and pushed again (the broker store died with the old
        process; the machine kept its tokens under their lease)."""
        from core.remote import mcp_gateway_push
        try:
            await mcp_gateway_push.reprovision_adopted(session_id, machine_id)
        except Exception:
            logger.exception("adopt %s: gateway re-provision failed", session_id[:8])

    async def adopt_session(
        self, *, machine_id: str, session_id: str, agent_name: str,
        execution_path: str, command_id: str, use_native_permissions: bool = False,
        mode: str = "", token_floor: int = 0,
    ) -> AsyncIterator[CommonEvent]:
        """Re-adopt a turn the satellite kept alive across a proxy restart
        (Mode C). Rebuilds a minimal RemoteSessionInfo (no spawn, no
        subscription bind), asks the satellite to replay the retained turn
        buffer, and drives the engine's turn stream over it — the replayed
        `_resume_replay_begin` gates the consumer, `_seq` dedupes an overlap,
        and the buffered turn's sentinel/turn_ended closes it. A truncated
        replay injects a durable ⚠ block first."""
        if not self._adoptable(session_id, execution_path):
            clear_starting(session_id)
            yield CommonEvent(type=DONE)
            return
        # A larger queue: the replay arrives as one burst (session_event
        # dispatch drops on a full queue).
        queue = self._cm.create_session_queue(
            machine_id, session_id, execution_path, maxsize=4096,
        )
        info = RemoteSessionInfo(
            session_id=session_id,
            machine_id=machine_id,
            agent_name=agent_name,
            execution_path=execution_path,
            event_queue=queue,
            use_native_permissions=use_native_permissions,
        )
        info.current_send_command_id = command_id
        adapter = self._adapter(info)
        self._restore_state(info, mode, token_floor)
        adapter.adopt_state(info, self._cm)
        self._sessions[session_id] = info
        clear_starting(session_id)
        reset_subagent_registry(session_id)
        reset_bg_command_registry(session_id)
        # Restore the runtime state a spawn would have created: last_active
        # (idle reaper + /v1/session/current), the in-memory subscription
        # binding, and the rotation fan-out target — all in-memory-only, so a
        # re-adopted session would otherwise miss every future token rotation
        # until its next full re-warm (a re-adopted session once lost its
        # injected credentials this way).
        _record_session_use(session_id, client_type="", agent=agent_name)
        await asyncio.to_thread(
            self._restore_adopted_credentials, session_id, machine_id, agent_name,
            info.execution_path,
        )
        await self._reprovision_gateway(session_id, machine_id)

        # Ask the satellite to replay. Fire-and-forget: the replay arrives as
        # session_event frames on the queue we just created.
        await self._cm.send_fire_and_forget(machine_id, {
            "type": "resume_session_stream",
            "session_id": session_id,
        })

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
                    yield CommonEvent(type=REPLAY_BEGIN,
                                      data={"truncated": bool(raw.get("truncated"))})
                    if raw.get("truncated"):
                        yield CommonEvent(type=TEXT, data={
                            "content": "\n⚠ stream interrupted — earlier "
                                       "output was truncated during a platform "
                                       "restart.\n",
                        })
            async with contextlib.aclosing(adapter.stream_turn(info, self._cm)) as events:
                async for event in events:
                    ending = turn_ending.from_dict(event.data.get("ending")) \
                        if event.type == ERROR else None
                    if (ending is not None and ending.reason in turn_ending.KILLS_PROCESS
                            and self.capabilities_for(session_id).runtime.hard_abort_kills_process):
                        # A decline, a limit or a silence in a replayed turn
                        # ends the CLI as a live turn's does
                        # (``RemoteTurnMixin._send_turn``); an error, an exit
                        # or a loss leaves it as it is.
                        await self._hard_abort(session_id)
                        yield event
                        yield CommonEvent(type=DONE)
                        return
                    yield event
        finally:
            info.turn_active = False

    async def is_session_process_dead(self, session_id: str) -> bool:
        info = self._sessions.get(session_id)
        if not info:
            return True
        if info.cli_dead:
            return True
        return not self._cm.is_connected(info.machine_id)

    def is_session_grace_held(self, session_id: str) -> bool:
        """True while the session's satellite dropped but the reconnect-grace
        window is still open — 'reconnecting', not dead. ``is_session_alive``
        reads False in that window (the machine is deregistered), so callers
        deciding whether a session is truly abandonable (cross-engine switch,
        ``process_alive`` reporting) must OR this in — a 10-second WS blip
        must not read as a dead session."""
        info = self._sessions.get(session_id)
        return bool(
            info and self._cm.is_session_in_grace(info.machine_id, session_id)
        )

    async def wait_session_reconnect(self, session_id: str, *,
                                     timeout: float = RECONNECT_WAIT_S) -> bool:
        """Wait while the session is held in its machine's reconnect grace,
        at most ``timeout`` seconds; True when it is alive again. A reconnect
        re-attaches the held queues and an expiry pops them, so the grace
        ending either way ends the wait. A session not held (a machine away
        past the grace, no record) answers at once."""
        if not self.is_session_grace_held(session_id):
            return False
        deadline = time.monotonic() + timeout
        while self.is_session_grace_held(session_id) and time.monotonic() < deadline:  # noqa: ASYNC110  # the grace ends in several owners of the link: a poll misses none
            await asyncio.sleep(_RECONNECT_POLL_S)
        return await self.is_session_alive(session_id)

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
        except RuntimeError:
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

    async def prepare_resume(self, session_id: str) -> None:
        # Remove dead session entry so a new start_session can be issued
        info = self._sessions.pop(session_id, None)
        if info is not None:
            mark_closing(session_id)
            # Drop any grace-held queue for this session (the reap
            # path) so a reconnect won't re-adopt a turn we're discarding.
            self._cm.drop_grace_session(info.machine_id, session_id)
            # Tear down the dead session's engine state (a Codex router and
            # its supervisors) so nothing leaks — the fresh session starts
            # its own.
            await self._adapter(info).close_state(info)

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
        external_home: str = "",
    ) -> ResumeVerdict:
        """Decide whether to issue ``start_session(resume=True)`` for a
        dead/lost remote session — the ENGINE's answer
        (``RemoteEngineAdapter.can_resume``: Claude asks the satellite to
        read the session JSONL its CLI wrote, so a blind ``--resume``
        against a missing file cannot silently start a fresh session; Codex
        answers from the thread id its rollout persists under). The target
        comes from ``_resume_target``; a target that cannot be named now (a
        store error, an offline machine) is UNKNOWN, never FRESH: the fresh
        branch replaces the chat's context for good.
        """
        from core.session.session_manager import get_layer_by_path

        # The store reads of the slow path run off the loop; the target is
        # remembered for the probe's other questions (``resume_machine_reach``
        # between the rounds, the watcher's future), which read nothing.
        target = await asyncio.to_thread(self._resume_target, session_id, agent_name, username)
        _remember_resume_target(session_id, target)
        if isinstance(target, ResumeVerdict):
            return target
        machine_id, execution_path, resume_handle, agent_name = target
        adapter = get_layer_by_path(execution_path).remote_adapter()
        if adapter is None:
            return ResumeVerdict.FRESH
        return await adapter.can_resume(
            self._cm, machine_id=machine_id, session_id=session_id,
            agent_name=agent_name, username=username, resume_handle=resume_handle,
            external_home=external_home,
        )

    def _resume_target_remembered(self, session_id: str, agent_name: str, username: str):
        """The target ``can_resume_session`` resolved a moment ago, else the
        slow resolve itself (a caller that asks before any probe)."""
        held = _resume_targets.get(session_id)
        if held is not None and time.monotonic() - held[0] < _RESUME_TARGET_TTL_S:
            return held[1]
        target = self._resume_target(session_id, agent_name, username)
        _remember_resume_target(session_id, target)
        return target

    def resume_machine_reach(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> MachineReach:
        """Where the session's machine stands for an unanswered probe:
        connected, dropped within the grace (worth a wait), or away past it
        (refused at once). A session whose target cannot be named reads as
        away; a local target as the platform's."""
        target = self._resume_target_remembered(session_id, agent_name, username)
        if target is ResumeVerdict.FRESH:
            return MachineReach.LOCAL
        if isinstance(target, ResumeVerdict):
            return MachineReach.AWAY
        machine_id = target[0]
        if self._cm.is_connected(machine_id):
            return MachineReach.CONNECTED
        if self._cm.machine_dropped_recently(machine_id):
            return MachineReach.AWAY_RECENT
        return MachineReach.AWAY

    def _resume_adapter(self, session_id: str, agent_name: str, username: str):
        """``(adapter, machine_id)`` of the session's outstanding probe, or
        None when the target names no machine or engine."""
        from core.session.session_manager import get_layer_by_path
        target = self._resume_target_remembered(session_id, agent_name, username)
        if isinstance(target, ResumeVerdict):
            return None
        adapter = get_layer_by_path(target[1]).remote_adapter()
        return (adapter, target[0]) if adapter is not None else None

    def resume_probe_future(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> asyncio.Future | None:
        found = self._resume_adapter(session_id, agent_name, username)
        if found is None:
            return None
        adapter, machine_id = found
        return adapter.probe_future(self._cm, machine_id=machine_id, session_id=session_id)

    def forget_resume_probe(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> None:
        found = self._resume_adapter(session_id, agent_name, username)
        if found is not None:
            adapter, machine_id = found
            adapter.forget_probe(self._cm, machine_id=machine_id, session_id=session_id)

    def _resume_target(
        self, session_id: str, agent_name: str, username: str,
    ) -> "tuple[str, str, str, str] | ResumeVerdict":
        """``(machine_id, execution_path, resume_handle, agent_name)`` of a
        dead or lost session, or the verdict when there is nothing to ask.

        Two paths to find the machine and the engine:
        1. Fast: ``_sessions[session_id]`` holds both.
        2. Slow: when ``_sessions`` is empty (after ``prepare_resume``, after
           a reap, after a proxy restart), the chat ROW knows the pinned
           machine, the stored engine and the resume handle — and its
           ``execution_path`` column may be EMPTY (a delegate worker chat
           never stamps it), so the engine is resolved the way the spawn
           path resolves it, never compared raw. A chat with no pin falls
           back to the agent's current target via the same precedence the
           dashboard uses (``user_remote_targets`` → ``agent_remote_targets``
           → ``local``), which is what the post-abort / post-reap auto-resume
           paths in ``ws/dashboard.py`` need to reach the right satellite.
        A store error on the way is UNKNOWN (a blip must not empty a Codex
        handle into a digest); no agent, a local target or no remote engine
        is FRESH (nothing of the session exists on a machine); the offline
        sentinel is UNKNOWN (the machine is away).
        """
        from core.session.session_manager import resolve_execution_path
        machine_id = ""
        execution_path = ""
        resume_handle = ""
        info = self._sessions.get(session_id)
        if info:
            execution_path = info.execution_path
            resume_handle = info.resume_handle
            machine_id = info.machine_id
            agent_name = agent_name or info.agent_name
        else:
            # Fallback (proxy restart / reap / prepare_resume popped the
            # info): the chat ROW still knows what this session was.
            try:
                from storage.database import get_chat_by_session
                chat = get_chat_by_session(session_id)
            except ImportError:
                chat = None
            except psycopg.Error as e:
                logger.warning(
                    "can_resume_session: the chat of %s could not be read: %s",
                    session_id[:8], e,
                )
                return ResumeVerdict.UNKNOWN
            stored_path = ""
            if chat:
                agent_name = agent_name or chat.get("agent") or ""
                stored_path = chat.get("execution_path") or ""
                resume_handle = chat.get("codex_thread_id") or ""
                machine_id = chat.get("execution_target") or ""
            execution_path = resolve_execution_path(agent_name, stored_path)
            if not placement.machine_of(machine_id):
                # No chat row (or an unpinned/local one) — derive the target
                # from the agent's resolved execution target. Requires
                # agent_name + username so we can apply the per-user override
                # that ``resolve_execution_target`` checks first. Without
                # username we fall back to the agent's default — fine for
                # admin-paired agents but may pick the wrong machine for
                # user-paired sessions.
                if not agent_name:
                    return ResumeVerdict.FRESH
                try:
                    from services.remote import remote_status
                    from storage import database as _db
                    user_sub = (
                        _db.get_user_sub_by_username(username)
                        if username else None
                    )
                    target, _reason = remote_status.resolve_execution_target(
                        agent_name, user_sub,
                    )
                except ImportError:
                    return ResumeVerdict.FRESH
                except psycopg.Error as e:
                    logger.warning(
                        "can_resume_session: target resolve failed for agent %r "
                        "user %r: %s",
                        agent_name, username, e,
                    )
                    return ResumeVerdict.UNKNOWN
                if placement.is_local(target):
                    # Not a remote target — caller's layer routing should
                    # already have skipped this codepath, but be defensive.
                    return ResumeVerdict.FRESH
                if placement.is_offline_sentinel(target):
                    return ResumeVerdict.UNKNOWN
                machine_id = target
        return machine_id, execution_path, resume_handle, agent_name
