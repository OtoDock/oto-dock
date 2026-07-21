"""Codex event translator — CodexEvent notifications -> CommonEvent stream.

Converts the codex app-server's item/turn JSON-RPC notifications into the
platform's CommonEvent stream so the ChatStreamPump processes Codex turns
identically to CLI or Direct LLM events. Split out of layer.py; layer.py
re-exports CodexEventTranslator (and _codex_tool_summary) for back-compat.
"""

import logging

import config as app_config
from core.events.common_events import (
    CommonEvent, TEXT, THINKING, TOOL_USE, TOOL_INPUT, TOOL_RESULT,
    SUBAGENT_START, SUBAGENT_END, METADATA, DONE, ERROR, TODO_UPDATE,
    GOAL_UPDATE, CONTEXT_COMPACT,
)
from core.layers.codex.session import CodexEvent

logger = logging.getLogger("codex-layer")


def _codex_tool_summary(item_type: str, item: dict) -> str:
    """One-line summary for inline tool display in the dashboard."""
    if item_type == "commandExecution":
        cmd = item.get("command", "")
        return cmd[:100] + ("..." if len(cmd) > 100 else "")
    if item_type == "fileChange":
        changes = item.get("changes", [])
        names = [c.get("path", "").rsplit("/", 1)[-1] for c in changes[:3]]
        suffix = f" +{len(changes) - 3}" if len(changes) > 3 else ""
        return ", ".join(names) + suffix
    if item_type == "mcpToolCall":
        return f"{item.get('server', '')}/{item.get('tool', '')}"
    if item_type == "webSearch":
        q = item.get("query", "")
        return q if isinstance(q, str) else ""
    return ""


# Codex ThreadItem.type → platform tool display name (matches the CLI's names).
_TOOL_NAME_BY_ITEM_TYPE: dict[str, str] = {
    "commandExecution": "Bash",
    "fileChange": "apply_patch",
    "webSearch": "web_search",
}


def _completed_tool_output(item_type: str, item: dict) -> "tuple[str, bool] | None":
    """``(output_text, is_error)`` from a completed ThreadItem, or None when
    the type carries none. Field names verified against a live app-server
    probe (codex 0.142.5) + codex-rs app-server-protocol v2 ``item.rs``:
    commandExecution ``aggregatedOutput``/``exitCode``/``status``; fileChange
    ``changes[{path, kind.type, diff}]``/``status``; mcpToolCall
    ``result.content[]`` (MCP content blocks) or ``error.message``. webSearch
    and dynamicToolCall completions carry nothing to show (we register no
    dynamic tools). The raw text is capped pump-side (one truncation policy
    for headless + both interactive tailers)."""
    if item_type == "commandExecution":
        failed = item.get("status") == "failed"
        body = item.get("aggregatedOutput") or ""
        if not body and failed:
            exit_code = item.get("exitCode")
            body = f"(exit {exit_code})" if exit_code is not None else "(failed)"
        return (body, failed) if body else None
    if item_type == "fileChange":
        parts: list[str] = []
        for ch in item.get("changes") or []:
            kind = ((ch.get("kind") or {}).get("type") or "update")
            parts.append(f"{kind} {ch.get('path', '')}")
            diff = ch.get("diff") or ""
            if diff:
                parts.append(diff)
        body = "\n".join(parts)
        return (body, item.get("status") == "failed") if body else None
    if item_type == "mcpToolCall":
        err = item.get("error") or {}
        if err.get("message"):
            return err["message"], True
        result = item.get("result") or {}
        texts = [
            blk.get("text", "")
            for blk in (result.get("content") or [])
            if isinstance(blk, dict) and blk.get("type") == "text"
        ]
        body = "\n".join(t for t in texts if t)
        if not body and result.get("structuredContent") is not None:
            import json as _json
            body = _json.dumps(result["structuredContent"], indent=1)
        return (body, item.get("status") == "failed") if body else None
    return None

# turn.status values that terminate a turn without success.
_TURN_PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress",
                     "completed": "completed"}

# CollabAgentStatus buckets (codex 0.120.0). A collab sub-agent is "active"
# while pending/running and "terminal" once it finishes; the translator diffs
# ``agentsStates`` against these to emit SUBAGENT_START / SUBAGENT_END.
_COLLAB_ACTIVE = frozenset({"pendingInit", "running"})
_COLLAB_TERMINAL = frozenset(
    {"completed", "errored", "shutdown", "interrupted", "notFound"})

# ServerNotification methods that carry no pump-visible signal (suppressed).
_SUPPRESSED_METHODS = frozenset({
    "thread/started", "turn/started", "thread/status/changed",
    "mcpServer/startupStatus/updated", "mcpServer/oauthLogin/completed",
    "item/mcpToolCall/progress", "item/commandExecution/outputDelta",
    "command/exec/outputDelta", "item/fileChange/outputDelta",
    "item/commandExecution/terminalInteraction", "turn/diff/updated",
    "serverRequest/resolved", "hook/started", "hook/completed",
    "model/rerouted", "account/updated", "account/rateLimits/updated",
    "app/list/updated", "fs/changed", "item/plan/delta",
    "item/reasoning/summaryPartAdded", "deprecationNotice", "configWarning",
})


class CodexEventTranslator:
    """Translate ``codex app-server`` notifications into CommonEvents.

    The daemon streams JSON-RPC notifications (``item/agentMessage/delta``,
    ``item/started``/``item/completed`` carrying a ``ThreadItem``, ``turn/
    plan/updated``, ``thread/tokenUsage/updated``, ``turn/completed``). One
    instance per session persists across turns (held on the session) so
    ``codex_thread_id`` is emitted once and token state is available at
    turn end. ``event.type`` is the notification *method*; ``event.data`` its
    ``params`` (see :class:`CodexEvent`).
    """

    def __init__(self, model: str = "", *, supervised_bg: bool = False) -> None:
        self._model = model
        # When True (the LOCAL Codex layer, whose session runs a per-thread bg
        # supervisor), background sub-agents still active at main-turn end are NOT
        # swept — the supervisor emits each one's SUBAGENT_END on real completion.
        # When False (the REMOTE path — the satellite has no bg supervisor yet),
        # keep the original behavior: sweep still-active sub-agents at turn end so
        # their badges can't hang. Remote bg-subagent supervision is a follow-up.
        self._supervised_bg = supervised_bg
        # Per-turn latest token breakdown (``tokenUsage.last`` is already
        # per-turn — no cumulative diffing needed, unlike the exec model).
        self._last_usage: dict = {}
        self._ctx_window: int | None = None
        # Armed by a compaction (auto or manual, item or legacy notification):
        # the NEXT tokenUsage carries the post-compaction prompt size, which
        # the context gauge must show without waiting for the next turn to
        # complete — a compaction landing at/after turn end otherwise leaves
        # the gauge (and chats.context_used) at the pre-compaction number.
        self._await_post_compact_usage = False
        # itemIds whose text/thinking streamed via delta notifications, so the
        # item/completed fallback only fires when nothing streamed.
        self._streamed_text: set[str] = set()
        self._streamed_reasoning: set[str] = set()
        self._emitted_thread_id = False
        # Collab sub-agents seen this turn: agentId -> {started, ended, desc}.
        # Diffed from collabAgentToolCall ``agentsStates`` (0.5).
        self._subagents: dict[str, dict] = {}
        # Tombstone of agent ids that have already finished (this session). A
        # later turn can reference an already-completed sub-agent (e.g. the agent
        # ``wait_agent``s / "closes" it in the review-nudge turn) — its agentsStates
        # reappears in a collabAgentToolCall, and since finished agents are dropped
        # from _subagents, _handle_collab would treat it as NEW and re-emit a
        # spurious SUBAGENT_START (a phantom badge). The tombstone makes a finished
        # agent stay finished. Agent ids are UUIDs → bounded growth.
        self._resolved_subagents: set[str] = set()
        # One warning per session for a malformed thread/goal/updated shape —
        # goal notifications can repeat every turn and must never spam the log.
        self._goal_warned = False
        # Main thread id, captured from its first turn/started. The daemon
        # multiplexes spawned sub-agent threads onto the same connection; we
        # suppress every notification tagged with a different threadId so a
        # sub-agent's internal stream never leaks into the main agent's output
        # (or ends the main turn). See translate().
        self._main_thread_id: str | None = None

    def translate(self, event: CodexEvent) -> list[CommonEvent]:
        method = event.type
        params = event.data

        # Multi-agent thread demux. The daemon streams the MAIN thread's
        # notifications AND each spawned sub-agent thread's notifications over the
        # one connection — every per-turn/per-item notification carries a
        # ``threadId`` (verified vs 0.120.0: items, deltas, reasoning, turn/*,
        # tokenUsage, error all do). Capture the main thread id from its first
        # ``turn/started`` (sub-agents are spawned later, mid-turn), then SUPPRESS
        # any notification tagged with a different threadId: a sub-agent's own
        # reasoning / messages / token usage / turn-completion must NOT surface as
        # the main agent's text or end the main turn. The sub-agent LIFECYCLE is
        # tracked instead from the MAIN thread's ``collabAgentToolCall`` items
        # (whose ``agentsStates`` is the authoritative per-agent snapshot).
        tid = params.get("threadId") if isinstance(params, dict) else None
        if tid:
            if not self._main_thread_id and method == "turn/started":
                self._main_thread_id = tid
            if self._main_thread_id and tid != self._main_thread_id:
                return []

        if method == "item/agentMessage/delta":
            item_id = params.get("itemId", "")
            delta = params.get("delta", "")
            if delta:
                self._streamed_text.add(item_id)
                return [CommonEvent(type=TEXT, data={"content": delta})]
            return []

        if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            item_id = params.get("itemId", "")
            delta = params.get("delta", "")
            if delta:
                self._streamed_reasoning.add(item_id)
                return [CommonEvent(type=THINKING, data={"phase": "delta", "text": delta})]
            return []

        if method == "item/started":
            return self._on_item_started(params.get("item", {}))

        if method == "item/completed":
            return self._on_item_completed(params.get("item", {}))

        if method == "turn/plan/updated":
            todos = [
                {"content": s.get("step", ""),
                 "status": _TURN_PLAN_STATUS.get(s.get("status", ""), "pending")}
                for s in params.get("plan", [])
            ]
            # persist_block: codex's update_plan has no tool-path block (unlike
            # claude's TodoWrite), so the pump persists a TodoWrite-shaped block
            # for history rehydration of the checklist panel.
            return [CommonEvent(type=TODO_UPDATE, data={"todos": todos, "persist_block": True})]

        if method == "thread/goal/updated":
            return self._on_goal_updated(params)

        if method == "thread/goal/cleared":
            return [CommonEvent(type=GOAL_UPDATE, data={"cleared": True})]

        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            self._last_usage = usage.get("last", {}) or {}
            self._ctx_window = usage.get("modelContextWindow")
            if self._await_post_compact_usage:
                self._await_post_compact_usage = False
                post = int(self._last_usage.get("inputTokens", 0) or 0)
                if post > 0:
                    # Gauge-only phase: moves the live counter (and the
                    # persisted chat context) to the compacted size — no chip.
                    return [CommonEvent(type=CONTEXT_COMPACT, data={
                        "phase": "usage", "post_tokens": post,
                        "context_max": int(self._ctx_window or 0),
                    })]
            return []

        if method == "thread/compacted":
            # Legacy compaction notification (pre-0.142 daemons; the v2
            # app-server now swallows it — the canonical signal is the
            # contextCompaction ITEM handled in _on_item_completed). Same
            # first-class contract as the Claude layer's compact lift.
            self._await_post_compact_usage = True
            return [CommonEvent(type=CONTEXT_COMPACT, data={
                "phase": "completed", "trigger": "auto",
            })]

        if method == "turn/completed":
            return self._on_turn_completed(params.get("turn", {}))

        if method == "error":
            err = params.get("error", {}) or {}
            return [CommonEvent(type=ERROR, data={
                "message": err.get("message", "Codex error"),
            })]

        if method in _SUPPRESSED_METHODS:
            return []

        logger.debug(f"Codex translator: unhandled notification {method}")
        return []

    def _on_goal_updated(self, params) -> list[CommonEvent]:
        """``thread/goal/updated`` → GOAL_UPDATE. Wire shape verified live vs
        0.142.5: params ``{threadId, turnId, goal}`` where the ThreadGoal is
        ``{threadId, objective, status, tokenBudget (nullable), tokensUsed,
        timeUsedSeconds, createdAt, updatedAt}``. ``status`` observed values:
        ``active`` / ``complete`` (binary also defines paused/usageLimited/
        budgetLimited) — a model "mark complete" arrives as an update with
        ``status: complete``, NOT as thread/goal/cleared. A flat params shape
        is tolerated; a malformed one is dropped (logged once per session) —
        goals must never break the event stream.
        """
        try:
            goal = params.get("goal")
            if not isinstance(goal, dict):
                goal = params
            objective = goal.get("objective")
            if not isinstance(objective, str) or not objective:
                raise ValueError("missing objective")
            budget = goal.get("tokenBudget")
            status = goal.get("status")
            data = {
                "objective": objective,
                "status": status if isinstance(status, str) and status else "active",
                "token_budget": int(budget) if budget is not None else None,
                "tokens_used": int(goal.get("tokensUsed") or 0),
                "time_used_seconds": int(goal.get("timeUsedSeconds") or 0),
                "cleared": False,
            }
        except (TypeError, ValueError, AttributeError) as e:
            if not self._goal_warned:
                self._goal_warned = True
                logger.warning(
                    f"Codex translator: unrecognized thread/goal/updated shape ({e}); ignoring")
            return []
        return [CommonEvent(type=GOAL_UPDATE, data=data)]

    # ------------------------------------------------------------------
    # Item lifecycle (item.type discriminates — verified ThreadItem union)
    # ------------------------------------------------------------------

    def _on_item_started(self, item: dict) -> list[CommonEvent]:
        item_id = item.get("id", "")
        item_type = item.get("type", "")

        # The user's own message echoes back as a userMessage item — suppress
        # (we persist the user turn ourselves; verified live).
        if item_type == "userMessage":
            return []

        if item_type == "agentMessage":
            return []  # text streams via item/agentMessage/delta

        if item_type == "reasoning":
            return [CommonEvent(type=THINKING, data={"phase": "start"})]

        if item_type in ("commandExecution", "fileChange", "webSearch"):
            tool_name = _TOOL_NAME_BY_ITEM_TYPE[item_type]
            events = [CommonEvent(type=TOOL_USE, data={"name": tool_name, "tool_id": item_id})]
            summary = _codex_tool_summary(item_type, item)
            if summary:
                events.append(CommonEvent(type=TOOL_INPUT, data={
                    "name": tool_name, "summary": summary, "tool_input": item,
                }))
            return events

        if item_type == "mcpToolCall":
            server, tool = item.get("server", ""), item.get("tool", "")
            tool_name = f"mcp__{server}__{tool}" if server and tool else "mcp_tool"
            events = [CommonEvent(type=TOOL_USE, data={"name": tool_name, "tool_id": item_id})]
            summary = _codex_tool_summary("mcpToolCall", item)
            if summary:
                events.append(CommonEvent(type=TOOL_INPUT, data={
                    "name": tool_name, "summary": summary,
                    "tool_input": item.get("arguments"),
                }))
            return events

        # Collab sub-agents → rich SUBAGENT_START/END (0.5), diffed from the
        # item's ``agentsStates`` (the spawn id/prompt appears on item/completed,
        # so most events fire there — but the handler is safe on both).
        if item_type == "collabAgentToolCall":
            return self._handle_collab(item)

        # Multi-agent v2 lifecycle (ultra / proactive orchestration, 0.144).
        if item_type == "subAgentActivity":
            return self._handle_subagent_activity(item)

        # dynamicToolCall = a client-registered dynamic tool (we register none);
        # render as a generic tool card if one ever appears.
        if item_type == "dynamicToolCall":
            return [CommonEvent(type=TOOL_USE, data={
                "name": item.get("tool", item_type), "tool_id": item_id,
            })]

        if item_type == "plan":
            return []  # prose plan; the checklist arrives via turn/plan/updated

        logger.debug(f"Codex translator: unhandled item.started type={item_type!r}")
        return []

    def _on_item_completed(self, item: dict) -> list[CommonEvent]:
        item_id = item.get("id", "")
        item_type = item.get("type", "")

        if item_type == "userMessage":
            return []

        if item_type == "contextCompaction":
            # The canonical v2 compaction signal (0.142+: the app-server
            # swallows the deprecated thread/compacted for v2 clients) —
            # auto AND manual compaction both arrive as this item.
            self._await_post_compact_usage = True
            return [CommonEvent(type=CONTEXT_COMPACT, data={
                "phase": "completed", "trigger": "auto",
            })]

        if item_type == "agentMessage":
            # Fallback: emit the full text only if nothing streamed via deltas.
            if item_id not in self._streamed_text:
                text = item.get("text", "")
                if text:
                    return [CommonEvent(type=TEXT, data={"content": text})]
            self._streamed_text.discard(item_id)
            return []

        if item_type == "reasoning":
            self._streamed_reasoning.discard(item_id)
            return [CommonEvent(type=THINKING, data={"phase": "end", "text": ""})]

        if item_type in ("commandExecution", "fileChange", "webSearch"):
            data = {"name": _TOOL_NAME_BY_ITEM_TYPE[item_type], "tool_id": item_id}
            output = _completed_tool_output(item_type, item)
            if output is not None:
                data["result_content"], data["is_error"] = output
            return [CommonEvent(type=TOOL_RESULT, data=data)]

        if item_type == "mcpToolCall":
            server, tool = item.get("server", ""), item.get("tool", "")
            tool_name = f"mcp__{server}__{tool}" if server and tool else "mcp_tool"
            data = {"name": tool_name, "tool_id": item_id}
            output = _completed_tool_output("mcpToolCall", item)
            if output is not None:
                data["result_content"], data["is_error"] = output
            return [CommonEvent(type=TOOL_RESULT, data=data)]

        # Collab sub-agents: the spawnAgent/completed item carries the new
        # agent id (receiverThreadIds) + its task (prompt) + agentsStates, and
        # wait/completed carries the terminal agentsStates — both diffed here.
        if item_type == "collabAgentToolCall":
            return self._handle_collab(item)

        # Multi-agent v2 lifecycle (ultra / proactive orchestration, 0.144).
        if item_type == "subAgentActivity":
            return self._handle_subagent_activity(item)

        if item_type == "dynamicToolCall":
            return [CommonEvent(type=TOOL_RESULT, data={
                "name": item.get("tool", item_type), "tool_id": item_id,
            })]

        if item_type == "plan":
            return []

        logger.debug(f"Codex translator: unhandled item.completed type={item_type!r}")
        return []

    def _handle_collab(self, item: dict) -> list[CommonEvent]:
        """Map a ``collabAgentToolCall`` item to SUBAGENT_START / SUBAGENT_END.

        Codex multi-agent collaboration surfaces as ``collabAgentToolCall`` items
        whose ``agentsStates: {agentId: {status, message}}`` is the authoritative
        per-agent snapshot (verified live, 0.5). We diff it, keyed by the agent's
        thread id (stable — also the ``receiverThreadIds`` of the spawn): first
        sighting active (``pendingInit``/``running``) → SUBAGENT_START (description
        from the spawn ``prompt``); first sighting terminal (``completed`` /
        ``errored`` / ``shutdown`` / ``interrupted`` / ``notFound``) →
        SUBAGENT_END. The snapshot is full on every collab item, so this converges
        regardless of which item (spawn vs wait) carries which transition.
        ``tool_use_id`` is the agent id, so the dashboard's existing per-id
        subagent widgets light up + clear individually — no ``fg_agents_complete``.
        """
        events: list[CommonEvent] = []
        # spawnAgent carries the new agent's task in ``prompt`` + its id in
        # ``receiverThreadIds`` — stash the description for the START below.
        prompt = item.get("prompt")
        if prompt:
            for aid in item.get("receiverThreadIds") or []:
                if aid and aid not in self._resolved_subagents:
                    self._subagents.setdefault(aid, {})["desc"] = prompt
        states = item.get("agentsStates")
        if not isinstance(states, dict):
            return events
        for aid, state in states.items():
            if not aid:
                continue
            if aid in self._resolved_subagents:
                continue  # already finished earlier — never re-open its badge
            status = (state or {}).get("status", "")
            rec = self._subagents.setdefault(aid, {})
            if not rec.get("started") and (
                status in _COLLAB_ACTIVE or status in _COLLAB_TERMINAL
            ):
                rec["started"] = True
                events.append(CommonEvent(type=SUBAGENT_START, data={
                    "description": rec.get("desc") or (state or {}).get("message") or "sub-agent",
                    "subagent_type": "general-purpose",
                    # The spawn prompt as tool_input — CLI task_spawn parity, so
                    # the dashboard pill EXPANDS to the full prompt (the collapsed
                    # description line clips it to one line). Absent when the
                    # spawn item didn't carry a prompt (state-message fallback).
                    **({"tool_input": {"prompt": rec["desc"]}} if rec.get("desc") else {}),
                    # Mark every Codex sub-agent as background: a spawned agent runs
                    # on its OWN thread and MAY outlive the main turn, so the badge
                    # must keep spinning past turn end (the dashboard's onDone only
                    # auto-clears NON-background sub-agents). Each one clears on its
                    # own per-agent SUBAGENT_END — which fires DURING the turn for a
                    # foreground (waited) sub, or at REAL completion for a background
                    # one (the supervisor, local) / the turn-end sweep (remote).
                    # Without this the dashboard treated bg subs as foreground and
                    # flipped the badge to "finished" at main-turn end.
                    "run_in_background": True,
                    "tool_use_id": aid,
                }))
            if status in _COLLAB_TERMINAL and not rec.get("ended"):
                rec["ended"] = True
                self._resolved_subagents.add(aid)
                events.append(CommonEvent(type=SUBAGENT_END, data={"tool_use_id": aid}))
        return events

    def _handle_subagent_activity(self, item: dict) -> list[CommonEvent]:
        """Map a multi-agent v2 ``subAgentActivity`` item (0.144 — the shape
        ultra's proactive orchestration emits on SPAWN) to SUBAGENT_START/END.

        The v2 spawn handler emits ``{kind: started, agentThreadId, agentPath}``
        the moment a sub-agent thread starts — BEFORE any ``wait_agent``
        produces a ``collabAgentToolCall`` snapshot — so mapping it here makes
        the badge light up at spawn time. Terminal state still arrives via the
        collab items' ``agentsStates`` (handled in ``_handle_collab``; both
        paths share the ``_subagents``/``_resolved_subagents`` records keyed by
        the agent's thread id, so double STARTs/ENDs can't happen).
        ``interrupted`` ends the badge; ``interacted`` (send_message/followup)
        is a lifecycle no-op. Description falls back to the ``agentPath``'s
        task segment — the richer spawn prompt only rides collab items.
        """
        aid = item.get("agentThreadId") or ""
        if not aid or aid in self._resolved_subagents:
            return []
        kind = item.get("kind", "")
        rec = self._subagents.setdefault(aid, {})
        if kind == "started" and not rec.get("started"):
            rec["started"] = True
            path = str(item.get("agentPath") or "")
            desc = rec.get("desc") or (path.split("/")[-1] if path else "") or "sub-agent"
            rec["desc"] = desc
            return [CommonEvent(type=SUBAGENT_START, data={
                "description": desc,
                "subagent_type": "general-purpose",
                # Background semantics — same contract as _handle_collab's
                # START (the badge clears on this agent's own SUBAGENT_END).
                "run_in_background": True,
                "tool_use_id": aid,
            })]
        if kind == "interrupted" and rec.get("started") and not rec.get("ended"):
            rec["ended"] = True
            self._resolved_subagents.add(aid)
            return [CommonEvent(type=SUBAGENT_END, data={"tool_use_id": aid})]
        return []

    def _on_turn_completed(self, turn: dict) -> list[CommonEvent]:
        # Sub-agents still active (pendingInit/running) at the MAIN turn's
        # completion are BACKGROUND sub-agents (spawned without wait_agent) — the
        # daemon keeps running them on their own threads past this turn
        # (live-verified). With a bg supervisor (``supervised_bg`` — local layer)
        # do NOT sweep them: the session's per-thread supervisor emits each one's
        # SUBAGENT_END on real completion, so sweeping here would clear the badge
        # prematurely; keep still-active entries tracked (drop only resolved ones).
        # Without one (remote path) sweep them as before so a badge can't hang.
        # Foreground (waited) sub-agents are terminal by main-turn end either way
        # (``ended`` set in _handle_collab), so they are never in the sweep.
        sweep: list[CommonEvent] = []
        if self._supervised_bg:
            self._subagents = {
                aid: rec for aid, rec in self._subagents.items()
                if rec.get("started") and not rec.get("ended")
            }
        else:
            for aid, rec in self._subagents.items():
                if rec.get("started") and not rec.get("ended"):
                    self._resolved_subagents.add(aid)
                    sweep.append(CommonEvent(type=SUBAGENT_END, data={"tool_use_id": aid}))
            self._subagents = {}

        status = turn.get("status", "completed")
        if status == "failed":
            err = turn.get("error", {}) or {}
            return sweep + [
                CommonEvent(type=ERROR, data={"message": err.get("message", "Turn failed")}),
                CommonEvent(type=DONE),
            ]
        if status == "interrupted":
            # Abort path — the turn was interrupted; just close it out.
            return sweep + [CommonEvent(type=DONE)]

        # Success: per-turn token breakdown is tokenUsage.last (already per-turn).
        # cache_write stays 0 BY NECESSITY, not choice: the OpenAI API reports
        # cache-write tokens on gpt-5.6+ (usage.input_tokens_details.
        # cache_write_tokens, billed at 1.25x input for implicit AND explicit
        # caching), but codex-rs 0.144.1 drops the field at deserialization —
        # its TokenUsage carries only input/cached/output/reasoning/total
        # (codex-api/src/sse/responses.rs). Until the pinned CLI surfaces it,
        # gpt-5.6 codex costs slightly UNDERCOUNT the true bill (by 0.25x the
        # input rate on however many non-cached tokens were written — bounded
        # by the non-cached input line, usually the smallest turn component).
        # We record what the provider-side CLI reports rather than fabricate
        # counts. Re-check on every codex pin bump (VERSIONS.md runbook).
        u = self._last_usage
        input_tokens = int(u.get("inputTokens", 0) or 0)
        cached = int(u.get("cachedInputTokens", 0) or 0)
        output_tokens = int(u.get("outputTokens", 0) or 0)
        non_cached = max(0, input_tokens - cached)

        cost = 0.0
        if self._model:
            provider = app_config.get_model_provider(self._model)
            p_in, p_out, _p_cw, p_cr = app_config.get_model_pricing(self._model, provider)
            cost = (
                non_cached * p_in / 1_000_000
                + cached * p_cr / 1_000_000
                + output_tokens * p_out / 1_000_000
            )

        # Context gauge: Codex app-server reports the last turn's prompt size +
        # the model context window, so (unlike exec mode) we CAN show it.
        ctx_used = input_tokens if self._ctx_window else 0
        ctx_max = int(self._ctx_window or 0)

        meta = {
            "cost_usd": cost,
            "cost_is_delta": True,
            "input_tokens": non_cached,
            "output_tokens": output_tokens,
            "cache_read": cached,
            "cache_write": 0,
            "context_used": ctx_used,
            "context_max": ctx_max,
        }
        # Native wall-clock from the daemon (the layer also injects its own).
        if turn.get("durationMs") is not None:
            meta["duration_ms"] = int(turn["durationMs"])
        return sweep + [CommonEvent(type=METADATA, data=meta), CommonEvent(type=DONE)]

    def pending_bg_subagents(self) -> list[dict]:
        """Background sub-agents still active at main-turn end (started, not yet
        terminal). The session arms a supervisor per entry to await each one's
        real completion. Returns ``[{"agent_id", "description"}]`` — ``agent_id``
        is the sub-agent's thread id (also the SUBAGENT_START / live-badge key).
        """
        return [
            {"agent_id": aid, "description": rec.get("desc") or "sub-agent"}
            for aid, rec in self._subagents.items()
            if rec.get("started") and not rec.get("ended")
        ]

    def subagent_end_event(self, agent_id: str) -> list[CommonEvent]:
        """SUBAGENT_END for a background sub-agent whose supervisor saw it
        terminate. Idempotent: marks the agent ``ended`` so a later turn's stray
        collab snapshot can't re-open its badge. Returns [] if already ended.
        """
        if agent_id in self._resolved_subagents:
            return []
        rec = self._subagents.get(agent_id)
        if rec is not None:
            rec["ended"] = True
        self._resolved_subagents.add(agent_id)
        return [CommonEvent(type=SUBAGENT_END, data={"tool_use_id": agent_id})]

    def thread_id_metadata(self, thread_id: str) -> list[CommonEvent]:
        """Emit codex_thread_id once for DB persistence (called by the session)."""
        if thread_id and not self._emitted_thread_id:
            self._emitted_thread_id = True
            return [CommonEvent(type=METADATA, data={"codex_thread_id": thread_id})]
        return []
