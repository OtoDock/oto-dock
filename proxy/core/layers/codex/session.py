"""CodexAppServerSession — persistent ``codex app-server`` session management.

Codex runs as a long-lived JSON-RPC daemon (``codex app-server``, NDJSON over
stdio) — structurally identical to the Claude CLI's persistent subprocess.
A session spawns the daemon once, opens (or resumes) a thread, and runs each
turn via ``turn/start`` → stream notifications → ``turn/completed``, keeping the
daemon (and its warm MCP servers) alive across turns. Abort is ``turn/interrupt``
(daemon stays warm — no rollout-truncation hack).

Replaces the previous process-per-turn ``codex exec`` model. The pool API
(``create_codex_session`` / ``get_codex_session`` / ``close_codex_session`` /
``reap_idle_codex_sessions``) and the ``_codex_sessions`` registry are unchanged
so ``app.py`` / ``concurrency.py`` touchpoints keep working.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator

import config as app_config
from core.layers.codex.app_server_client import AppServerClient, AppServerError
from core.layers.codex.codex_approvals import (
    approval_for_sandbox, build_sandbox_policy, make_server_request_handler,
)
from core.session.session_state import (
    clear_session_liveness, get_session_user_tz, resolve_session_permissions,
)

logger = logging.getLogger("codex-session")

# Codex idle reaping is unified across all session kinds via
# config.get_idle_timeout() (the admin `session_idle_timeout` setting); the reaper
# reads it per-sweep. See core/concurrency.py.

# Bounded warm-gate for MCP startup before the first turn (the analog of the
# CLI's _wait_for_init). app-server emits mcpServer/startupStatus/updated per
# configured server; we wait for startup to go quiet, capped, then proceed.
# Lean-start: quiescence trimmed 1.5s→0.5s. The daemon stays warm for
# the whole session and `thread/start` returns before MCPs finish, so the gate
# is only a first-turn nicety; a shorter silence threshold shaves ~1s off cold
# first-token. Keep in lock-step with the satellite twin
# (satellite/codex_session.py).
_WARM_QUIESCENCE_S = 0.5
_WARM_CAP_S = 15.0


@dataclass
class CodexEvent:
    """One inbound app-server message routed to the translator.

    ``type`` is the JSON-RPC notification *method* (e.g. ``item/agentMessage/
    delta``, ``turn/completed``); ``data`` is its ``params``. (Under the old
    ``codex exec`` model this carried the JSONL event ``type`` + object — the
    translator was rewritten to key on the method instead.)
    """
    type: str
    data: dict


class CodexAppServerSession:
    """One persistent Codex conversation over a ``codex app-server`` daemon."""

    def __init__(
        self,
        session_id: str,
        agent_name: str,
        model: str,
        sandbox_mode: str,
        working_dir: str,
        config_dir: str,
        extra_env: dict[str, str] | None = None,
        sandbox_cmd_prefix: list[str] | None = None,
        effort: str = "",
        thread_id: str | None = None,
        system_prompt: str = "",
        user_role: str = "",
    ):
        self.session_id = session_id
        self.agent_name = agent_name
        self.model = model
        self.sandbox_mode = sandbox_mode          # SandboxMode enum string
        self.working_dir = working_dir
        self.config_dir = config_dir              # host path to .codex/ (CODEX_HOME)
        self.extra_env = extra_env or {}
        self.sandbox_cmd_prefix = sandbox_cmd_prefix or []
        self.effort = effort
        self.system_prompt = system_prompt
        self.user_role = user_role

        # Thread persistence: pre-populated for resume; captured on thread/start.
        self.thread_id: str | None = thread_id
        # Approval policy is derived from the sandbox mode (the gating matrix):
        # danger-full-access (dontAsk/auto) → never; everything else → on-request,
        # so a sandbox escape fires an approval we route through decide_tool_permission.
        self.approval_policy: str = approval_for_sandbox(sandbox_mode)

        self.last_activity: float = time.monotonic()
        self.lock = asyncio.Lock()
        self.translator = None  # set by the layer; persists across turns

        self._client: AppServerClient | None = None
        self._started = False
        self._closed = False
        self._current_turn_id: str | None = None  # for turn/interrupt
        # itemId → [paths] from fileChange item/started, so the lean v2
        # item/fileChange/requestApproval can recover what it's writing.
        self._item_paths: dict[str, list[str]] = {}

        # --- Background sub-agent demux (multi-agent concurrency) ---
        # A single router task is the SOLE consumer of the daemon's notif_queue.
        # It demuxes by threadId: the MAIN thread (and any untagged notification)
        # → the active turn's _default_consumer; each spawned sub-agent thread →
        # a per-thread buffer. A BACKGROUND sub-agent (spawned without wait_agent)
        # keeps streaming on its own thread after the main turn ends — a thin
        # per-thread supervisor drains its buffer to the terminal, then marks the
        # SubagentRegistry done + clears its badge so the shared _bg_agent_monitor
        # nudges.
        self._router_task: asyncio.Task | None = None
        self._default_consumer: asyncio.Queue | None = None      # active main turn
        self._thread_consumers: dict[str, asyncio.Queue] = {}    # sub_tid → buffer
        self._bg_supervisors: dict[str, asyncio.Task] = {}       # sub_tid → supervisor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        return (
            not self._closed
            and self._client is not None
            and self._client.is_alive
        )

    async def start(self) -> None:
        """Spawn the daemon, initialize, open/resume the thread, warm MCPs."""
        if self._started:
            return
        self._started = True

        env = self._build_env()
        # Under bwrap (local) the prefix sets the cwd via --chdir, so pass
        # cwd=None; otherwise use the resolved working dir if any.
        cwd = None if self.sandbox_cmd_prefix else (self.working_dir or None)

        await self._connect_with_retry(env, cwd)

        # Wire native-tool approvals: the daemon fires an approval
        # ServerRequest on a sandbox escape (write outside the writable root,
        # network, escalation); route it through the one decision authority
        # in-process — decide_tool_permission — reusing the same path-policy +
        # mode logic as the CLI hook. (Configured MCP tools are gated natively
        # too, via mcpServer/elicitation/request → the same bridge → the same
        # decide_tool_permission; there is NO transport/interceptor gate for
        # Codex MCP permissions.) Non-approval ServerRequests are answered
        # safely by the bridge so the daemon never hangs.
        self._client.set_server_request_handler(
            make_server_request_handler(
                self._decide_permission,
                ask_question=self._ask_question,
                get_item_paths=lambda: self._item_paths,
                log=lambda m: logger.info(f"Codex [{self.session_id[:8]}] {m}"),
            )
        )

        overrides = self._thread_overrides()
        if self.thread_id:
            # Resume an existing thread (survives proxy restart via codex_thread_id).
            try:
                await self._client.request("thread/resume", {
                    "threadId": self.thread_id, **overrides,
                })
                logger.info(f"Codex [{self.session_id[:8]}] resumed thread {self.thread_id}")
            except AppServerError as e:
                # Rollout gone (CODEX_HOME wiped / fresh machine) → start fresh.
                logger.warning(
                    f"Codex [{self.session_id[:8]}] resume failed ({e}); starting new thread"
                )
                self.thread_id = None
        if not self.thread_id:
            res = await self._client.request("thread/start", overrides)
            self.thread_id = (res.get("thread") or {}).get("id") or ""
            logger.info(f"Codex [{self.session_id[:8]}] started thread {self.thread_id}")

        await self._warm_mcps()

        # The router now becomes the SOLE consumer of notif_queue (warm-up drained
        # the startup notifications itself, above). On a re-warm (daemon death →
        # fresh client + queue) tear down the stale router + any orphaned bg
        # supervisors first, so the new router owns the new queue cleanly.
        await self._teardown_bg(reason="re-warm")
        self._router_task = asyncio.create_task(self._route_notifications())

        self.last_activity = time.monotonic()

    async def send_message(
        self, prompt: str, *, inject_time: bool = False,
    ) -> AsyncIterator[CodexEvent]:
        """Run one turn: ``turn/start`` → stream notifications → ``turn/completed``.

        Yields a :class:`CodexEvent` per inbound notification (method + params).
        Keeps the daemon alive afterwards.
        """
        if self._closed or self._client is None:
            raise RuntimeError(f"CodexAppServerSession {self.session_id} is closed")
        if not self.is_alive:
            # Daemon died between turns — re-warm + resume (mirror CLI cli_dead).
            logger.info(f"Codex [{self.session_id[:8]}] daemon dead; re-warming")
            self._started = False
            await self.start()

        if inject_time:
            user_tz = get_session_user_tz(self.session_id)
            time_str = app_config.format_current_time(user_tz)
            prompt = f"[Current time: {time_str}]\n\n{prompt}"
            from core.session import sibling_awareness
            sibling_line = await sibling_awareness.prelude_line(self.session_id)
            if sibling_line:
                prompt = f"{sibling_line}\n\n{prompt}"

        # Register this turn's MAIN-thread consumer; the router (the sole
        # notif_queue consumer) feeds it. No pre-turn drain is needed — the router
        # routes each sub-agent thread to its own buffer, so a prior turn leaves no
        # main-thread stragglers to discard.
        consumer: asyncio.Queue = asyncio.Queue()
        self._default_consumer = consumer

        turn_params: dict = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
            # Per-turn permission knobs — make every turn authoritative for
            # the session's CURRENT mode, so a mid-session set_permission_mode
            # takes effect next turn. turn/start takes the *structured*
            # sandboxPolicy (thread/start takes the simple SandboxMode enum).
            "approvalPolicy": self.approval_policy,
            "approvalsReviewer": "user",
            "sandboxPolicy": build_sandbox_policy(self.sandbox_mode, self.working_dir),
            # Plan collaboration mode, re-asserted per turn (like sandboxPolicy) so a
            # plan→build switch clears it on the next turn — self-contained, not
            # relying on thread persistence.
            "settings": {"collaborationMode": self._collaboration_mode()},
        }
        # Pick up mid-session model/effort changes as per-turn overrides too.
        if self.model:
            turn_params["model"] = self.model
        if self.effort:
            turn_params["effort"] = self.effort
        # Fresh turn → drop the prior turn's fileChange path correlations.
        self._item_paths.clear()
        res = await self._client.request("turn/start", turn_params)
        self._current_turn_id = (res.get("turn") or {}).get("id")
        self.last_activity = time.monotonic()

        try:
            while True:
                method, params = await consumer.get()
                self.last_activity = time.monotonic()
                if method == "__daemon_exit__":
                    # Daemon died mid-turn → it will never send serverRequest/
                    # resolved, so release any pending in-process approval /
                    # MCP-gate waiter for this session (deny) before we bail.
                    resolve_session_permissions(self.session_id, approved=False)
                    yield CodexEvent(type="error", data={
                        "message": "Codex app-server exited unexpectedly",
                    })
                    return
                if method == "item/started":
                    self._track_item_paths(params)
                yield CodexEvent(type=method, data=params)
                # The router only feeds this consumer the MAIN thread's (and any
                # untagged) notifications — a spawned sub-agent's events go to its
                # own buffer — so a sub-agent's ``turn/completed`` can no longer
                # reach this loop and truncate the turn (the 0.5.17 truncation bug
                # is now structurally impossible). Keep the main-thread gate as a
                # defensive belt-and-braces (untagged → treat as main → never hang).
                ev_tid = params.get("threadId") if isinstance(params, dict) else None
                is_main_thread = (not ev_tid) or (not self.thread_id) or (ev_tid == self.thread_id)
                if method == "turn/completed" and is_main_thread:
                    return
                if (method == "error" and is_main_thread
                        and not params.get("error", {}).get("willRetry")):
                    # Stream-level failure on the MAIN thread that won't retry.
                    return
        finally:
            self._current_turn_id = None
            # Hand off any still-running background sub-agents to supervisors and
            # clear this turn's consumer. Synchronous (no await) so the router
            # can't observe a partial hand-off.
            self._handoff_bg_subagents()

    async def abort(self) -> None:
        """Interrupt the in-flight turn; keep the daemon warm (no trim hack)."""
        if self._client is None or not self._client.is_alive:
            return
        if not self._current_turn_id and self._default_consumer is not None:
            # A turn is mid turn/start (consumer registered, id not yet
            # assigned) — wait out the RPC so the interrupt can't miss the
            # window and leave the turn streaming past an "aborted" UI.
            for _ in range(20):
                await asyncio.sleep(0.1)
                if self._current_turn_id or self._default_consumer is None:
                    break
        if not self._current_turn_id:
            return
        try:
            await self._client.request("turn/interrupt", {
                "threadId": self.thread_id, "turnId": self._current_turn_id,
            }, timeout=10.0)
            logger.info(f"Codex [{self.session_id[:8]}] interrupted turn {self._current_turn_id}")
        except AppServerError as e:
            logger.warning(f"Codex [{self.session_id[:8]}] interrupt failed: {e}")

    async def steer(self, text: str) -> bool:
        """Inject user input into the RUNNING turn via ``turn/steer``.

        Codex drains pending input at the sampling-round boundary and EXTENDS
        the turn to consume it, persisting the input to the rollout at accept
        time — an accepted steer is therefore delivered exactly-once (verified
        live, 0.142.5; see the session-N plan's pre-build probe). The steer
        window opens at the ``turn/started`` notification: between ``turn/start``
        and that notification (``_current_turn_id`` unset) and for review/
        compaction turns the request is rejected — return False and let the
        caller fall back to the post-turn queue.
        """
        if (self._closed or self._client is None or not self._client.is_alive
                or not self._current_turn_id):
            return False
        try:
            await self._client.request("turn/steer", {
                "threadId": self.thread_id,
                "expectedTurnId": self._current_turn_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            }, timeout=10.0)
        except AppServerError as e:
            logger.info(f"Codex [{self.session_id[:8]}] steer rejected: {e}")
            return False
        self.last_activity = time.monotonic()
        logger.info(f"Codex [{self.session_id[:8]}] steered turn {self._current_turn_id}")
        return True

    async def compact(self) -> dict | None:
        """Manual thread compaction between turns (``thread/compact/start``).

        The router drops main-thread notifications when no turn consumer is
        registered, so a temporary consumer is installed for the compaction's
        stream (progress arrives as normal turn/item notifications, then
        ``thread/compacted``). Refuses while a turn is active — compaction
        turns are non-steerable daemon-side and the active turn owns the
        consumer slot. Returns ``{"post_tokens": int | None}`` on success
        (the post-compaction prompt size from the stream's last
        ``tokenUsage/updated``, same figure the gauge uses), None on failure.
        """
        if self._closed or self._client is None or not self.is_alive:
            return None
        if self._current_turn_id or self._default_consumer is not None:
            return None
        consumer: asyncio.Queue = asyncio.Queue()
        self._default_consumer = consumer
        try:
            await self._client.request("thread/compact/start", {
                "threadId": self.thread_id,
            }, timeout=30.0)
            post_tokens: int | None = None
            compacted = False
            deadline = time.monotonic() + 120.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if compacted:
                        # Grace window closed — the item is authoritative.
                        logger.info(
                            f"Codex [{self.session_id[:8]}] compacted thread "
                            f"(post_tokens={post_tokens})"
                        )
                        return {"post_tokens": post_tokens}
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] compaction timed out"
                    )
                    return None
                try:
                    method, params = await asyncio.wait_for(
                        consumer.get(), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    continue     # loop re-checks the deadline / grace window
                self.last_activity = time.monotonic()
                if method == "__daemon_exit__":
                    return None
                if method == "thread/tokenUsage/updated":
                    usage = (params.get("tokenUsage") or {})
                    last = usage.get("last") or {}
                    post_tokens = int(last.get("inputTokens", 0) or 0) or post_tokens
                    # Keep the translator's gauge bookkeeping consistent for
                    # the NEXT turn's metadata.
                    if self.translator is not None:
                        self.translator._last_usage = last
                        self.translator._ctx_window = (
                            usage.get("modelContextWindow")
                            or self.translator._ctx_window
                        )
                    continue
                # Canonical v2 completion: the contextCompaction ITEM (the
                # app-server swallows the deprecated thread/compacted for v2
                # clients — verified against 0.142.5 source). Core recomputes
                # token usage AFTER replacing the history, so keep a short
                # grace window for the post-compaction size / turn close.
                if ((method == "item/completed"
                     and (params.get("item") or {}).get("type")
                     == "contextCompaction")
                        or method == "thread/compacted"):
                    compacted = True
                    deadline = min(deadline, time.monotonic() + 5.0)
                    continue
                if method == "turn/completed":
                    if compacted:
                        logger.info(
                            f"Codex [{self.session_id[:8]}] compacted thread "
                            f"(post_tokens={post_tokens})"
                        )
                        return {"post_tokens": post_tokens}
                    # The compaction turn closed without the item — failed.
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] compaction turn ended "
                        f"without a contextCompaction item"
                    )
                    return None
                if method == "error" and not (
                        (params.get("error") or {}).get("willRetry")):
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] compaction failed: "
                        f"{(params.get('error') or {}).get('message', '?')}"
                    )
                    return None
                # turn/started, item/* progress — noise.
        except AppServerError as e:
            logger.warning(f"Codex [{self.session_id[:8]}] compact rejected: {e}")
            return None
        finally:
            self._default_consumer = None

    async def close(self) -> None:
        """Terminate the daemon (it is long-lived, not ephemeral)."""
        self._closed = True
        await self._teardown_bg(reason="close")
        if self._client is not None:
            await self._client.close()

    # ------------------------------------------------------------------
    # Mid-session overrides (applied as per-turn args on the next turn/start;
    # the daemon stays warm so these are real now — capabilities flipped).
    # ------------------------------------------------------------------

    def set_model(self, model: str) -> None:
        self.model = model
        if self.translator is not None:
            self.translator._model = model

    def set_sandbox_mode(self, sandbox_mode: str) -> None:
        self.sandbox_mode = sandbox_mode
        # Keep the approval policy in lock-step with the sandbox mode.
        self.approval_policy = approval_for_sandbox(sandbox_mode)

    def _collaboration_mode(self) -> dict:
        """The Codex collaboration mode for this turn, derived from the platform
        mode. ``plan`` is the ONLY platform mode that maps codex to a read-only
        sandbox, so read-only ⟺ plan. Returns the MINIMAL ``{"mode": ...}`` — NOT
        the full ``collaborationMode/list`` preset, whose ``reasoning_effort``
        would override the user's selected effort."""
        return {"mode": "plan"} if self.sandbox_mode == "read-only" else {"mode": "default"}

    async def _decide_permission(self, tool_name: str, tool_input: dict) -> dict:
        """The injected decision authority for the approval bridge — runs the
        platform permission decision in-process (local Codex)."""
        from api.hooks.hooks import decide_tool_permission
        return await decide_tool_permission(self.session_id, tool_name, tool_input)

    async def _ask_question(self, questions: list) -> dict:
        """The injected question authority for request_user_input — surfaces the
        dashboard card and blocks for the human answer in-process (local Codex)."""
        from api.hooks.hooks import ask_user_question
        return await ask_user_question(self.session_id, questions)

    def _track_item_paths(self, params: dict) -> None:
        """Record a fileChange item's target paths (from ``item/started``) so a
        following lean ``item/fileChange/requestApproval`` can name what it edits."""
        item = params.get("item") or {}
        if item.get("type") != "fileChange":
            return
        item_id = item.get("id")
        paths = [
            c.get("path") for c in (item.get("changes") or [])
            if isinstance(c, dict) and c.get("path")
        ]
        if item_id and paths:
            self._item_paths[item_id] = paths

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _thread_overrides(self) -> dict:
        """Thread-level config sent on thread/start | thread/resume.

        ``sandbox`` is the simple SandboxMode enum (``permission_to_sandbox``
        output); ``approvalPolicy`` is mode-derived — ``never`` for
        danger-full-access, ``on-request`` otherwise, so a sandbox escape fires
        an approval we route through ``decide_tool_permission``. turn/start
        re-asserts these per turn (with the structured sandboxPolicy) so a
        mid-session mode change takes effect.
        """
        overrides: dict = {
            "approvalPolicy": self.approval_policy,
            "approvalsReviewer": "user",
            "sandbox": self.sandbox_mode,
        }
        if self.model:
            overrides["model"] = self.model
        if self.effort:
            overrides["effort"] = self.effort
        if self.working_dir:
            overrides["cwd"] = self.working_dir
        return overrides

    async def _connect_with_retry(self, env: dict, cwd: str | None) -> None:
        """Spawn the daemon + initialize, resetting a stale state runtime once.

        codex app-server keeps SQLite runtime DBs directly under CODEX_HOME;
        a copy written by a different codex version aborts init ("migration N
        was previously applied but has been modified"). Reset the runtime DBs
        (rollouts in ``sessions/`` survive) and retry once.
        """
        last_err: Exception | None = None
        for attempt in (1, 2):
            self._client = AppServerClient(
                env=env, cwd=cwd,
                sandbox_cmd_prefix=self.sandbox_cmd_prefix,
                codex_bin=getattr(app_config, "CODEX_BIN", "codex"),
                label=f"codex[{self.session_id[:8]}]",
            )
            try:
                await self._client.start({
                    "clientInfo": {"name": "otodock", "title": "OtoDock", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                return
            except AppServerError as e:
                last_err = e
                await self._client.close()
                if attempt == 1:
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] app-server init failed ({e}); "
                        f"resetting state runtime + retrying"
                    )
                    self._reset_codex_state()
        raise last_err if last_err else RuntimeError("codex app-server init failed")

    def _reset_codex_state(self) -> None:
        """Delete codex's SQLite runtime DBs in CODEX_HOME (regenerable).

        Every DB family the daemon keeps directly under CODEX_HOME (state,
        logs, goals, memories, and whatever future releases add) can carry the
        "migration … has been modified" poison, so wipe them all — thread
        rollouts in ``sessions/`` survive. Matches ``_CODEX_RUNTIME_GLOBS`` in
        core/remote/file_sync.py so the reset covers exactly what is excluded
        from sync.
        """
        from pathlib import Path
        if not self.config_dir:
            return
        home = Path(self.config_dir)
        for path in home.glob("*.sqlite*"):
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"Codex state reset: couldn't delete {path.name}: {e}")

    # ------------------------------------------------------------------
    # Background sub-agent demux — router + supervisors
    # ------------------------------------------------------------------

    async def _route_notifications(self) -> None:
        """Sole consumer of the daemon's ``notif_queue``; demultiplexes by
        ``threadId``. MAIN-thread (and untagged) notifications → the active turn's
        ``_default_consumer``; each spawned sub-agent thread → its own buffer. This
        lets a background sub-agent keep streaming on its own thread after the main
        turn ends without its events being lost. Runs for one client's lifetime;
        cancelled on close / re-warm (a fresh client gets a fresh router)."""
        client = self._client
        if client is None:
            return
        q = client.notif_queue
        while True:
            try:
                method, params = await q.get()
            except asyncio.CancelledError:
                return
            # Any daemon traffic (incl. a still-running bg sub-agent) is activity —
            # keep the session from being reaped while bg work is in flight.
            self.last_activity = time.monotonic()
            if method == "__daemon_exit__":
                # Fan the death out to the active turn + every supervisor so none
                # hang waiting for a terminal that will never come.
                if self._default_consumer is not None:
                    self._default_consumer.put_nowait((method, params))
                for cq in list(self._thread_consumers.values()):
                    cq.put_nowait((method, params))
                return
            tid = params.get("threadId") if isinstance(params, dict) else None
            if tid and self.thread_id and tid != self.thread_id:
                # A spawned sub-agent thread → its own buffer (created lazily so a
                # sub that appears mid-turn is captured even before hand-off).
                cq = self._thread_consumers.get(tid)
                if cq is None:
                    cq = asyncio.Queue()
                    self._thread_consumers[tid] = cq
                cq.put_nowait((method, params))
            elif self._default_consumer is not None:
                # MAIN thread (or untagged) → the active turn's consumer.
                self._default_consumer.put_nowait((method, params))
            elif method in ("thread/goal/updated", "thread/goal/cleared"):
                # Codex accounts goal progress AT TURN STOP, so the final goal
                # update (often the completion) lands after turn/completed —
                # between turns, with no consumer. Goal state is chat-durable:
                # apply it out-of-band instead of dropping it.
                self._apply_goal_oob(method, params)
            # else: between turns with no active main turn — drop main-thread
            # stragglers (the daemon is idle on the main thread between turns).

    def _apply_goal_oob(self, method: str, params: dict) -> None:
        """Translate + apply a between-turns goal notification (persist to
        chats.thread_goal + broadcast to the owner's connections). Guarded —
        a failure here must never kill the router."""
        if self.translator is None:
            return
        try:
            from core.layers.codex.goals import apply_goal_events_oob
            events = self.translator.translate(CodexEvent(type=method, data=params))
            apply_goal_events_oob(self.session_id, events)
        except Exception:
            logger.exception(
                f"Codex [{self.session_id[:8]}] out-of-band goal apply failed")

    def _handoff_bg_subagents(self) -> None:
        """At main-turn end (from ``send_message``'s ``finally``, synchronously so
        the router can't observe a partial hand-off): arm a supervisor for each
        background sub-agent still active, discard buffers for foreground subs that
        already terminated, and clear the main-turn consumer. Feeds the
        SubagentRegistry so the shared _bg_agent_monitor fires the review nudge."""
        # Stop feeding the (now-finished) main turn first.
        self._default_consumer = None

        pending: list[dict] = []
        if self.translator is not None and not self._closed and self.is_alive:
            try:
                pending = self.translator.pending_bg_subagents()
            except Exception:
                logger.exception(
                    f"Codex [{self.session_id[:8]}] pending_bg_subagents failed"
                )
        pending_ids = {p["agent_id"] for p in pending}

        # Drop buffers for sub-agent threads we won't supervise (foreground subs
        # that already reached terminal — their buffered events are dead weight).
        for tid in list(self._thread_consumers.keys()):
            if tid not in pending_ids and tid not in self._bg_supervisors:
                self._thread_consumers.pop(tid, None)

        if not pending:
            return

        from core.session.session_state import get_subagent_registry
        reg = get_subagent_registry(self.session_id)
        for p in pending:
            aid = p["agent_id"]
            if aid in self._bg_supervisors:
                continue  # already supervised (carried over from a prior turn)
            # Ensure a buffer exists even if the sub emitted nothing post-spawn.
            self._thread_consumers.setdefault(aid, asyncio.Queue())
            reg.register_spawn(aid, aid)
            self._bg_supervisors[aid] = asyncio.create_task(
                self._supervise_bg_subagent(aid)
            )
        logger.info(
            f"Codex [{self.session_id[:8]}] {len(pending)} background sub-agent(s) "
            f"running past turn end → supervising {sorted(pending_ids)}"
        )

    async def _supervise_bg_subagent(self, sub_tid: str) -> None:
        """Drain ONE background sub-agent's thread buffer until it terminates, then
        resolve it. NEVER acquires ``self.lock`` — it runs concurrently with the
        user's / nudge's main turns. The shared _bg_agent_monitor (awaiting the
        registry) fires the cohort review nudge; this supervisor resolves only its
        own sub-agent."""
        CEILING = 600.0   # mirror _bg_agent_monitor's lost-terminal backstop
        start = time.monotonic()
        q = self._thread_consumers.get(sub_tid)
        try:
            while q is not None and (time.monotonic() - start) < CEILING:
                try:
                    method, params = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if self._closed or not self.is_alive:
                        break
                    continue
                self.last_activity = time.monotonic()
                if method == "__daemon_exit__":
                    break
                if method == "turn/completed":
                    break
                if (method == "error"
                        and not (params or {}).get("error", {}).get("willRetry")):
                    break
            else:
                if q is not None:
                    logger.warning(
                        f"Codex [{self.session_id[:8]}] bg sub-agent {sub_tid} hit "
                        f"the {CEILING:.0f}s ceiling without a terminal — resolving"
                    )
        finally:
            self._resolve_bg_subagent(sub_tid)

    def _resolve_bg_subagent(self, sub_tid: str) -> None:
        """Mark a background sub-agent done + clear its live badge — mirrors the
        CLI SubagentStop hook's between-turn delivery. Sync + idempotent. The
        completion side-effects (registry + badge + translator tombstone) live in
        the shared ``resolve_bg_subagent`` so local + remote Codex behave
        identically; we only pop our own per-thread bookkeeping here."""
        from core.session.session_state import resolve_bg_subagent
        self._thread_consumers.pop(sub_tid, None)
        self._bg_supervisors.pop(sub_tid, None)
        resolve_bg_subagent(self.session_id, sub_tid, self.translator)

    async def _teardown_bg(self, *, reason: str) -> None:
        """Cancel the router + every bg supervisor and resolve their registry
        entries (so a re-warm / close can't leave the _bg_agent_monitor waiting on
        a dead sub-agent). Idempotent — safe to call on a fresh session."""
        if self._router_task is not None:
            self._router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._router_task
            self._router_task = None
        sups = list(self._bg_supervisors.items())
        for _sub_tid, task in sups:
            task.cancel()
        for sub_tid, task in sups:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            # A cancelled supervisor's own ``finally`` already resolved it; this
            # is the idempotent backstop for one that never reached its finally.
            self._resolve_bg_subagent(sub_tid)
        self._thread_consumers.clear()
        self._default_consumer = None
        if sups:
            logger.info(
                f"Codex [{self.session_id[:8]}] tore down {len(sups)} bg "
                f"supervisor(s) ({reason})"
            )

    async def _warm_mcps(self) -> None:
        """Best-effort wait for configured MCP servers to finish starting.

        app-server emits ``mcpServer/startupStatus/updated {name, status}``.
        We drain pre-turn notifications until startup goes quiet (no update for
        ``_WARM_QUIESCENCE_S``) or the cap, logging readiness. Anything else
        seen here is pre-turn noise (thread/started, status changes) and dropped.
        """
        if self._client is None:
            return
        q = self._client.notif_queue
        statuses: dict[str, str] = {}
        deadline = time.monotonic() + _WARM_CAP_S
        while time.monotonic() < deadline:
            try:
                method, params = await asyncio.wait_for(q.get(), timeout=_WARM_QUIESCENCE_S)
            except asyncio.TimeoutError:
                break  # quiet → assume warm
            if method == "__daemon_exit__":
                logger.warning(f"Codex [{self.session_id[:8]}] daemon exited during warm-up")
                return
            if method == "mcpServer/startupStatus/updated":
                name = params.get("name", "?")
                statuses[name] = params.get("status", "?")
        ready = [n for n, s in statuses.items() if s not in ("starting", "failed")]
        failed = [n for n, s in statuses.items() if s == "failed"]
        logger.info(
            f"Codex [{self.session_id[:8]}] MCP warm-up: ready={ready} failed={failed}"
        )

    def _build_env(self) -> dict[str, str]:
        """Environment for the app-server daemon (mirrors the old exec path)."""
        from core.sandbox.env_builder import build_session_env
        username = ""
        if self.working_dir.startswith("/users/"):
            parts = self.working_dir.split("/")
            username = parts[2] if len(parts) >= 3 else ""
        env = build_session_env(
            self.session_id, self.agent_name,
            username=username, user_role=self.user_role,
        )
        # Ensure the codex binary's dir is on PATH (systemd has a minimal PATH).
        codex_bin = getattr(app_config, "CODEX_BIN", "codex")
        codex_dir = os.path.dirname(os.path.realpath(codex_bin))
        if codex_dir and codex_dir not in env.get("PATH", ""):
            env["PATH"] = codex_dir + ":" + env.get("PATH", "/usr/bin:/bin")
        env.update(self.extra_env)  # subscription auth (CODEX_HOME, etc.)
        return env


# ---------------------------------------------------------------------------
# Session pool (unchanged API — app.py / concurrency.py touchpoints stable)
# ---------------------------------------------------------------------------

_codex_sessions: dict[str, CodexAppServerSession] = {}
_codex_sessions_lock = asyncio.Lock()


async def active_agent_names() -> set[str]:
    """Agent slugs of every live Codex session.

    The auto-update in-use guard (services/mcp_updater.mcp_in_use) maps these to
    each agent's runtime MCP set to decide whether a docker MCP it's about to
    recreate is currently connected by a Codex session.
    """
    async with _codex_sessions_lock:
        return {s.agent_name for s in _codex_sessions.values()
                if getattr(s, "agent_name", "") and s.is_alive}


async def create_codex_session(
    session_id: str,
    agent_name: str,
    model: str,
    sandbox_mode: str = "workspace-write",
    working_dir: str = "",
    config_dir: str = "",
    extra_env: dict[str, str] | None = None,
    sandbox_cmd_prefix: list[str] | None = None,
    effort: str = "",
    thread_id: str | None = None,
    user_role: str = "",
    system_prompt: str = "",
) -> CodexAppServerSession:
    """Create, register, and **start** a Codex app-server session."""
    session = CodexAppServerSession(
        session_id=session_id,
        agent_name=agent_name,
        model=model,
        sandbox_mode=sandbox_mode,
        working_dir=working_dir,
        config_dir=config_dir,
        extra_env=extra_env,
        sandbox_cmd_prefix=sandbox_cmd_prefix,
        effort=effort,
        thread_id=thread_id,
        user_role=user_role,
        system_prompt=system_prompt,
    )
    async with _codex_sessions_lock:
        _codex_sessions[session_id] = session
    # Spawn the daemon outside the pool lock (slow — MCP init). On failure,
    # don't leave a dead entry behind for warmup to trip over.
    try:
        await session.start()
    except Exception:
        async with _codex_sessions_lock:
            _codex_sessions.pop(session_id, None)
        await session.close()
        raise
    return session


async def get_codex_session(session_id: str) -> CodexAppServerSession | None:
    """Look up a live session by id. Drops + returns None if dead/closed."""
    async with _codex_sessions_lock:
        session = _codex_sessions.get(session_id)
        if session and session.is_alive:
            return session
        if session and session._closed:
            _codex_sessions.pop(session_id, None)
        return session if session else None


async def close_codex_session(session_id: str) -> bool:
    """Close and remove a session from the pool."""
    async with _codex_sessions_lock:
        session = _codex_sessions.pop(session_id, None)
    if not session:
        return False
    await session.close()
    # Any background sub-agent threads died with the daemon — clear their
    # badges (the dead daemon's supervisor can never emit the clears itself).
    clear_session_liveness(session_id, reason="codex_close")
    # Wipe Codex's optional ``.codex/memories/`` (the platform's memory-mcp is
    # the single source of memory truth — see _write_config_toml [memories]).
    try:
        import shutil
        from pathlib import Path
        mem_dir = Path(session.config_dir) / "memories"
        if mem_dir.exists():
            shutil.rmtree(mem_dir, ignore_errors=True)
    except Exception:
        pass
    return True


async def reap_idle_codex_sessions() -> None:
    """Background task: reap idle Codex sessions every 60 s."""
    while True:
        await asyncio.sleep(60)
        try:
            now = time.monotonic()
            idle_timeout = app_config.get_idle_timeout()
            to_reap = [
                sid for sid, s in list(_codex_sessions.items())
                if now - s.last_activity > idle_timeout or not s.is_alive
            ]
            for sid in to_reap:
                logger.info(f"Reaping idle Codex session {sid[:8]}")
                await close_codex_session(sid)
                from core.concurrency import release_chat_slot
                release_chat_slot(sid)
                from services.engines.subscription_pool import release_subscription
                release_subscription(sid)
        except Exception as e:
            logger.error(f"Codex session reaper error: {e}")
