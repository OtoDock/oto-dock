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
import logging
import time
from typing import AsyncIterator

from core.events.common_events import CommonEvent, DONE, TEXT
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.layers.cli.settle import SettleController
from core.remote.remote_session_info import RemoteSessionInfo
from core.session.session_state import (
    _record_session_use,
    reset_subagent_registry,
)
from core.events.bg_command_state import reset_bg_command_registry

logger = logging.getLogger("remote-layer")


class RemoteResumeMixin:
    # --- Adopt, resume, liveness ---

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
        from storage.billing import subscription_store
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

    async def adopt_idle_session(
        self, *, machine_id: str, session_id: str, agent_name: str,
        use_native_permissions: bool = False,
    ) -> None:
        """Registry-only twin of ``adopt_session`` for sessions the satellite
        kept alive across a proxy restart with NO turn in flight. Nothing
        streams — the point is that the session EXISTS proxy-side again:
        the idle reaper can leash it, a later chat resume finds it, and
        token rotations reach it. Without this, restart-orphaned idle
        sessions lived forever satellite-side and ate the machine's
        session capacity (the operator's laptop hit its at-capacity wall
        after an evening of proxy redeploys). ``last_activity`` starts
        fresh: one more standard idle leash beats yanking a session a
        user is about to resume."""
        if session_id in self._sessions:
            return
        queue = self._cm.create_session_queue(
            machine_id, session_id, "claude-code-cli",
        )
        translator = ClaudeCLIEventTranslator(session_id)
        info = RemoteSessionInfo(
            session_id=session_id,
            machine_id=machine_id,
            agent_name=agent_name,
            execution_path="claude-code-cli",
            event_queue=queue,
            cli_translator=translator,
            cli_settle=SettleController(session_id, 0, translator),
            use_native_permissions=use_native_permissions,
        )
        self._sessions[session_id] = info
        reset_subagent_registry(session_id)
        reset_bg_command_registry(session_id)
        _record_session_use(session_id, client_type="", agent=agent_name)
        await asyncio.to_thread(
            self._restore_adopted_credentials, session_id, machine_id,
            agent_name,
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
