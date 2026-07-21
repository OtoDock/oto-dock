"""Claude Code CLI raw-event translator.

Pure per-turn parser. Takes raw NDJSON dicts from `claude -p --output-format
stream-json`, yields `ClaudeStreamChunk` objects. No I/O, no subprocess
management, no settle decisions — those live in `session.py` (for local) and
`remote_execution.py` (for remote).

This is the shared piece that guarantees identical event semantics between
local-sandboxed and remote-unsandboxed Claude CLI sessions.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from core.layers.cli.helpers import (
    ClaudeStreamChunk,
    _extract_context_window,
    _extract_turn_context,
    _extract_tool_summary,
    _SKIP_INLINE_TOOLS,
)
from core.session.session_state import get_subagent_registry
from core.events.bg_command_state import get_bg_command_registry, TERMINAL_STATUSES

if TYPE_CHECKING:
    pass

logger = logging.getLogger("cli-translator")


# The CLI's fixed `--resume` handshake reply. String-matching CLI-synthesized
# text is tolerable because the fleet CLI version is pinned (VERSIONS.md);
# settle.is_foreign_result's content-count guard catches the shape even if
# this string drifts. Lives here (not settle.py) because _handle_result also
# suppresses its fallback emission and settle imports this module.
RESUME_HANDSHAKE_RESULT = "No response requested."


# CLI system subtypes we never surface to the client — pure heartbeat noise.
# `task_started` / `task_progress` are NOT suppressed: the former drives the
# deterministic subagent registry + workflow start, the latter carries the
# live workflow phase/agent tree. `thinking_tokens` is not silent either —
# it becomes a LIVE-ONLY thinking progress gauge (see `_handle_system`).
_SILENT_SYSTEM_SUBTYPES = frozenset({
    "status",           # CLI heartbeat / state ping
})

# Cap on per-agent preview strings inside a workflow_progress tree so a busy
# workflow can't blow the satellite WS frame/queue caps.
_WORKFLOW_PREVIEW_CAP = 500
_WORKFLOW_PREVIEW_KEYS = ("resultPreview", "promptPreview", "result_preview", "prompt_preview")


def _tool_result_text(block: dict) -> str:
    """Flatten a tool_result block's content (string or text-part list)."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            p.get("text", "") for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


# TaskCreate results carry the CLI-assigned id: "Task #7 created successfully: …"
_TASK_CREATED_RE = re.compile(r"Task #(\d+)")


def _truncate_workflow_previews(workflow_progress):
    """Cap long preview strings in a workflow_progress tree (size guard for
    the satellite WS). Returns a shallow-copied list — never mutates the CLI
    event in place. Non-list input is returned unchanged."""
    if not isinstance(workflow_progress, list):
        return workflow_progress
    out = []
    for entry in workflow_progress:
        if isinstance(entry, dict):
            e = dict(entry)
            for k in _WORKFLOW_PREVIEW_KEYS:
                v = e.get(k)
                if isinstance(v, str) and len(v) > _WORKFLOW_PREVIEW_CAP:
                    e[k] = v[:_WORKFLOW_PREVIEW_CAP] + "…"
            out.append(e)
        else:
            out.append(entry)
    return out


class ClaudeCLIEventTranslator:
    """Stateful per-turn translator: raw NDJSON → ClaudeStreamChunk.

    One instance is created per CLI session and reused across turns.
    Callers call `reset_for_new_turn()` when a new turn begins, and
    `reset_for_settle()` when entering settle mode after a `result` event
    (preserves cross-turn counters but wipes parsing state).

    All event semantics match `PersistentSession.send_message` exactly —
    this class exists to let remote sessions reuse the same logic without
    duplicating code.
    """

    __slots__ = (
        "session_id",
        "actual_session_id",
        "block_types",
        "active_tool",
        "has_emitted_text",
        "_tool_inputs",
        "_tool_input_names",
        "agents_spawned",
        "last_turn_context",
        "_cc_tasks",
        "_pending_task_creates",
        "_bg_bash_commands",
        "_in_settle",
    )

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.actual_session_id = session_id

        # Per-turn parsing state
        self.block_types: dict[int, dict] = {}
        self.active_tool: dict | None = None
        self.has_emitted_text: bool = False
        self._tool_inputs: dict[int, list[str]] = {}
        self._tool_input_names: dict[int, str] = {}
        self.last_turn_context: int = 0

        # Spawn count — logging only. Authoritative subagent state (spawn /
        # finish gating) lives in the per-session SubagentRegistry.
        self.agents_spawned: int = 0

        # Newer Claude Code harnesses replaced TodoWrite with the
        # TaskCreate/TaskUpdate tool family. TaskUpdate only carries deltas,
        # so the full checklist is reconstructed HERE and emitted as the same
        # `todo_update` snapshots TodoWrite produces — the pump / TodoPanel /
        # remote path stay unchanged. Task ids are CLI-assigned and appear
        # only in the TaskCreate RESULT, so a create is staged by tool_use_id
        # until its result lands (_handle_user). SESSION-scoped: survives
        # turn resets (the checklist spans turns).
        self._cc_tasks: dict[str, dict] = {}            # task_id -> {content, status}
        self._pending_task_creates: dict[str, str] = {}  # tool_use_id -> subject

        # run_in_background Bash commands staged by tool_use_id at
        # content_block_stop, consumed by task_started{local_bash} so the
        # bg_command_spawn event carries the REAL command (the dashboard pill
        # expands to it) alongside the model-written description. Entries are
        # popped on spawn; stragglers (rejected commands) clear with the turn.
        self._bg_bash_commands: dict[str, str] = {}

        # True after reset_for_settle(): the model's final message is out, so
        # a bg-command completion observed from here on was never seen by the
        # model → mark_done(surfaced=False) (drives the task producer's
        # review-turn decision).
        self._in_settle: bool = False

    def reset_for_new_turn(self) -> None:
        """Full reset for a brand-new turn.

        Clears parsing state and the spawn counter. (The session's
        SubagentRegistry is reset separately by the caller — see
        session_state.reset_subagent_registry.)
        """
        self.block_types.clear()
        self.active_tool = None
        self.has_emitted_text = False
        self._tool_inputs.clear()
        self._tool_input_names.clear()
        self.last_turn_context = 0
        self.agents_spawned = 0
        # _cc_tasks deliberately survives (session-scoped checklist); only the
        # in-flight create staging is per-turn.
        self._pending_task_creates.clear()
        self._bg_bash_commands.clear()
        self._in_settle = False

    # -----------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------

    def feed(self, data: dict) -> list[ClaudeStreamChunk]:
        """Parse one raw NDJSON event into zero or more chunks.

        Mirrors the dispatch in PersistentSession.send_message lines 682-1056
        exactly. No I/O, no state external to this class (except the
        cross-turn counters).
        """
        if not isinstance(data, dict):
            return []

        msg_type = data.get("type", "")

        if "session_id" in data:
            self.actual_session_id = data["session_id"]

        if msg_type == "system":
            return self._handle_system(data)
        if msg_type == "control_request":
            return self._handle_control_request(data)
        if msg_type == "control_response":
            return []
        if msg_type == "assistant":
            return []
        if msg_type == "user":
            return self._handle_user(data)
        if msg_type == "stream_event":
            return self._handle_stream_event(data)
        if msg_type == "result":
            return self._handle_result(data)

        return []

    # -----------------------------------------------------------------
    # Per-event handlers
    # -----------------------------------------------------------------

    def _handle_system(self, data: dict) -> list[ClaudeStreamChunk]:
        """System events: init, task_started/_progress/_notification (subagents
        + dynamic workflows), compacting, compact_boundary, other."""
        subtype = data.get("subtype", "")
        if subtype == "init":
            # MCP init — log but don't yield
            mcp_servers = data.get("mcp_servers", [])
            connected = [s["name"] for s in mcp_servers if s.get("status") == "connected"]
            failed = [s["name"] for s in mcp_servers if s.get("status") == "failed"]
            if connected or failed:
                logger.info(
                    f"[{self.session_id[:8]}] MCP init — "
                    f"connected={connected}, failed={failed}"
                )
            return []

        if subtype in _SILENT_SYSTEM_SUBTYPES:
            return []

        if subtype == "thinking_tokens":
            # Adaptive-effort models (Opus 4.7+) don't stream thinking CONTENT
            # — the CLI sends this per-chunk token-estimate ping instead (one
            # per thinking chunk; ~hundreds per long turn). Surface it as a
            # LIVE-ONLY thinking progress event so the dashboard can show
            # "Thinking… ~N tokens" between the (empty) thinking block's
            # start/end. The pump never persists phase=progress — left as a
            # passthrough system event, these once flooded a chat with 693
            # junk rows and blew the chat-history row cap.
            est = data.get("estimated_tokens", 0)
            if not isinstance(est, (int, float)) or est <= 0:
                return []
            return [ClaudeStreamChunk(
                event_type="thinking",
                event_data={"phase": "progress", "estimated_tokens": int(est)},
                session_id=self.actual_session_id,
            )]

        if subtype == "task_started":
            return self._handle_task_started(data)
        if subtype == "task_progress":
            return self._handle_task_progress(data)
        if subtype == "task_notification":
            return self._handle_task_notification(data)
        if subtype == "task_updated":
            # A workflow tool can close via task_updated{patch.status=completed}
            # instead of a task_notification — treat it as the workflow end.
            tuid = data.get("tool_use_id", "")
            patch = data.get("patch") or {}
            reg = get_subagent_registry(self.session_id)
            if tuid in reg.workflow_tuids and patch.get("status") == "completed":
                reg.workflow_tuids.discard(tuid)
                return [ClaudeStreamChunk(
                    event_type="workflow_ended",
                    event_data={"tool_use_id": tuid},
                    session_id=self.actual_session_id,
                )]
            # A backgrounded shell command finished. This is the PRIMARY (and,
            # absent any completion hook, the only out-of-stdout) signal for
            # local_bash — matched by task_id (shell id), cleared on the
            # dashboard by the bound tool_use_id.
            status = patch.get("status", "")
            if status in TERMINAL_STATUSES:
                bgreg = get_bg_command_registry(self.session_id)
                if bgreg.mark_done(data.get("task_id", ""),
                                   surfaced=not self._in_settle):
                    return [ClaudeStreamChunk(
                        event_type="bg_command_end",
                        event_data={
                            "tool_use_id": bgreg.tuid_for(data.get("task_id", "")),
                            "status": status,
                        },
                        session_id=self.actual_session_id,
                    )]
            return []

        # All other subtypes (compacting, compact_boundary, etc.) — yield
        return [ClaudeStreamChunk(
            event_type="system",
            event_data={"subtype": subtype, **data},
            session_id=self.actual_session_id,
        )]

    def _handle_task_started(self, data: dict) -> list[ClaudeStreamChunk]:
        """Register a subagent spawn / start a dynamic workflow.

        Only ``local_agent`` enters the completion gate (it gets a
        SubagentStop hook). ``local_bash`` is ignored — it has no SubagentStop
        and would otherwise hang the wait. ``local_workflow`` is a dynamic
        workflow; its internal agents live in ``task_progress`` and never
        reach the gate.
        """
        task_type = data.get("task_type", "")
        tool_use_id = data.get("tool_use_id", "")
        reg = get_subagent_registry(self.session_id)

        if task_type == "local_workflow":
            reg.workflow_tuids.add(tool_use_id)
            return [ClaudeStreamChunk(
                event_type="workflow_started",
                event_data={
                    "tool_use_id": tool_use_id,
                    "workflow_name": data.get("workflow_name", "") or data.get("description", ""),
                },
                session_id=self.actual_session_id,
            )]

        if task_type == "local_agent":
            reg.register_spawn(data.get("task_id", ""), tool_use_id)
        elif task_type == "local_bash":
            # Backgrounded shell command: bind its shell id (task_id) to the
            # spawning Bash tool_use_id (gates the completion wait — task producer
            # + settle wait on this registry) AND emit the badge/inline block.
            # Emitted HERE, not at the Bash tool_use, so a REJECTED command —
            # which never starts and so never fires task_started — never strands a
            # badge. The pill's collapsed line is the description; it expands to
            # the real command (staged at the Bash tool_use's content_block_stop
            # — see _bg_bash_commands). Completion arrives via task_updated.
            get_bg_command_registry(self.session_id).register_spawn(
                data.get("task_id", ""), tool_use_id,
            )
            desc = data.get("description", "")
            # The real command was staged at the Bash tool_use's
            # content_block_stop — task_started only carries the description.
            # An empty miss is fine: the dashboard pill falls back to the
            # paired Bash tool card's input (pairBgCommandBlocks).
            return [ClaudeStreamChunk(
                event_type="bg_command_start",
                event_data={
                    "tool_use_id": tool_use_id,
                    "command": self._bg_bash_commands.pop(tool_use_id, ""),
                    "description": desc,
                },
                session_id=self.actual_session_id,
            )]
        # unknown task_type: not gated, no dashboard event.
        return []

    def _handle_task_progress(self, data: dict) -> list[ClaudeStreamChunk]:
        """Forward a dynamic-workflow phase/agent tree; drop plain heartbeats."""
        wp = data.get("workflow_progress")
        if wp is None:
            return []  # non-workflow progress ping — noise
        return [ClaudeStreamChunk(
            event_type="workflow_progress",
            event_data={
                "tool_use_id": data.get("tool_use_id", ""),
                "workflow_progress": _truncate_workflow_previews(wp),
            },
            session_id=self.actual_session_id,
        )]

    def _handle_task_notification(self, data: dict) -> list[ClaudeStreamChunk]:
        """Stdout completion signal — workflow end, or a backup for the
        idle-safe SubagentStop hook (deduped via the registry)."""
        tool_use_id = data.get("tool_use_id", "")
        task_id = data.get("task_id", "")
        reg = get_subagent_registry(self.session_id)

        if tool_use_id and tool_use_id in reg.workflow_tuids:
            reg.workflow_tuids.discard(tool_use_id)
            return [ClaudeStreamChunk(
                event_type="workflow_ended",
                event_data={"tool_use_id": tool_use_id},
                session_id=self.actual_session_id,
            )]

        # Backup completion for a backgrounded shell command (the primary signal
        # is task_updated above; this is the stdout backup, deduped via the
        # registry). Checked before the subagent path — a bg-bash id is never in
        # the subagent registry, so this only keeps the emitted event type right.
        bgreg = get_bg_command_registry(self.session_id)
        if task_id and bgreg.mark_done(task_id, surfaced=not self._in_settle):
            return [ClaudeStreamChunk(
                event_type="bg_command_end",
                event_data={"tool_use_id": bgreg.tuid_for(task_id), "status": "completed"},
                session_id=self.actual_session_id,
            )]

        # Backup for SubagentStop. Only fires the WS completion on the
        # newly-done transition (buffer=False ignores untracked local_bash ids).
        if task_id and reg.mark_done(task_id):
            return [ClaudeStreamChunk(
                event_type="subagent_end",
                event_data={"tool_use_id": tool_use_id or reg.tuid_for(task_id),
                            "agent_id": task_id},
                session_id=self.actual_session_id,
            )]
        return []

    def _handle_control_request(self, data: dict) -> list[ClaudeStreamChunk]:
        """control_request from CLI stdout — typically can_use_tool (native permissions)."""
        request = data.get("request", {})
        req_subtype = request.get("subtype", "")
        if req_subtype == "can_use_tool":
            return [ClaudeStreamChunk(
                event_type="permission_prompt",
                event_data={
                    "request_id": data.get("request_id", ""),
                    "tool_name": request.get("tool_name", ""),
                    "tool_input": request.get("input", {}),
                    "tool_use_id": request.get("tool_use_id", ""),
                    "description": request.get("description", ""),
                },
                session_id=self.actual_session_id,
            )]
        logger.info(
            f"[{self.session_id[:8]}] unhandled control_request subtype={req_subtype}"
        )
        return []

    def _handle_stream_event(self, data: dict) -> list[ClaudeStreamChunk]:
        """stream_event: content_block_start/delta/stop, message_start/delta/stop."""
        event = data.get("event", {})
        event_type = event.get("type", "")

        if event_type == "content_block_start":
            return self._handle_content_block_start(event)
        if event_type == "content_block_delta":
            return self._handle_content_block_delta(event)
        if event_type == "content_block_stop":
            return self._handle_content_block_stop(event)
        if event_type == "message_start":
            return self._handle_message_start(event)
        if event_type == "message_delta":
            return self._handle_message_delta(event)

        # message_stop, etc. — no yield
        return []

    def _handle_message_delta(self, event: dict) -> list[ClaudeStreamChunk]:
        """Surface safety refusals; everything else in message_delta stays silent.

        Fable 5's safety classifiers can decline a request (``stop_reason:
        "refusal"``, HTTP 200). In non-interactive stream-json Claude Code does
        NOT auto-fall-back to Opus 4.8 (only the interactive TUI switches
        models itself) — the turn just ends, which without this handler renders
        as a silently empty assistant reply. Emit a clear user-visible error
        instead, with the classifier category/explanation when present.
        """
        delta = event.get("delta", {}) or {}
        if delta.get("stop_reason") != "refusal":
            return []
        details = delta.get("stop_details") or event.get("stop_details") or {}
        category = details.get("category") or ""
        explanation = details.get("explanation") or ""
        parts = ["The model declined this request"]
        parts.append(f" (safety classifier: {category})" if category else " (safety classifier)")
        parts.append(".")
        if explanation:
            parts.append(f" {explanation}")
        parts.append(
            " You can rephrase the request, or switch this chat to another"
            " model (e.g. Opus 4.8) and retry."
        )
        return [ClaudeStreamChunk(
            text="".join(parts),
            session_id=self.actual_session_id,
            is_error=True,
        )]

    def _handle_content_block_start(self, event: dict) -> list[ClaudeStreamChunk]:
        chunks: list[ClaudeStreamChunk] = []

        # If a tool was active but its stop event was lost, close it now
        if self.active_tool:
            chunks.append(ClaudeStreamChunk(
                event_type="tool_end",
                event_data={"tool_id": self.active_tool["tool_id"], "name": self.active_tool["name"]},
                session_id=self.actual_session_id,
            ))
            self.active_tool = None

        cb = event.get("content_block", {})
        idx = event.get("index", 0)
        cb_type = cb.get("type", "")
        self.block_types[idx] = {
            "type": cb_type,
            "name": cb.get("name", ""),
            "tool_id": cb.get("id", ""),
        }

        if cb_type == "text":
            if self.has_emitted_text:
                chunks.append(ClaudeStreamChunk(text="\n\n", session_id=self.actual_session_id))
        elif cb_type == "thinking":
            chunks.append(ClaudeStreamChunk(
                event_type="thinking",
                event_data={"phase": "start"},
                session_id=self.actual_session_id,
            ))
        elif cb_type == "tool_use":
            tool_name = cb.get("name", "")
            self.active_tool = {"name": tool_name, "tool_id": cb.get("id", "")}
            chunks.append(ClaudeStreamChunk(
                event_type="tool_start",
                event_data={"name": tool_name, "tool_id": cb.get("id", "")},
                session_id=self.actual_session_id,
            ))
            self._tool_inputs[idx] = []
            self._tool_input_names[idx] = tool_name

        return chunks

    def _handle_content_block_delta(self, event: dict) -> list[ClaudeStreamChunk]:
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")

        if delta_type == "text_delta":
            text = delta.get("text", "")
            if not text:
                return []
            self.has_emitted_text = True
            return [ClaudeStreamChunk(text=text, session_id=self.actual_session_id)]

        if delta_type == "thinking_delta":
            text = delta.get("thinking", "")
            if text:
                return [ClaudeStreamChunk(
                    event_type="thinking",
                    event_data={"text": text},
                    session_id=self.actual_session_id,
                )]
            return []

        if delta_type == "input_json_delta":
            delta_idx = event.get("index", 0)
            if delta_idx in self._tool_inputs:
                partial = delta.get("partial_json", "")
                if partial:
                    self._tool_inputs[delta_idx].append(partial)
            return []

        return []

    def _handle_content_block_stop(self, event: dict) -> list[ClaudeStreamChunk]:
        idx = event.get("index", 0)
        block_info = self.block_types.get(idx, {})
        chunks: list[ClaudeStreamChunk] = []

        if block_info.get("type") == "thinking":
            chunks.append(ClaudeStreamChunk(
                event_type="thinking",
                event_data={"phase": "end"},
                session_id=self.actual_session_id,
            ))
            return chunks

        if block_info.get("type") != "tool_use" or idx not in self._tool_inputs:
            return chunks

        t_name = self._tool_input_names.get(idx, "")
        try:
            t_input = json.loads("".join(self._tool_inputs[idx]))
        except (json.JSONDecodeError, ValueError):
            t_input = {}

        if t_name in ("Task", "Agent"):
            self.agents_spawned += 1
            is_bg = bool(t_input.get("run_in_background", False))
            desc = t_input.get("description", "?")
            # tool_use_id is the dashboard's correlation key — the registry
            # binds it to the CLI task_id at `task_started`, and SUBAGENT_END
            # carries it back so the right widget clears on finish.
            tool_use_id = block_info.get("tool_id", "")
            logger.info(
                f"[{self.session_id[:8]}] subagent #{self.agents_spawned} spawned: "
                f"{desc} ({'bg' if is_bg else 'fg'})"
            )
            chunks.append(ClaudeStreamChunk(
                event_type="task_spawn",
                event_data={
                    "description": desc,
                    "subagent_type": t_input.get("subagent_type", ""),
                    "run_in_background": is_bg,
                    "tool_use_id": tool_use_id,
                    # Full Agent tool input (prompt, model, isolation, …) —
                    # the dashboard's subagent pill expands to it. Rides the
                    # event into the persisted turn block + live state, so
                    # history and reconnect render the same detail.
                    "tool_input": t_input,
                },
                session_id=self.actual_session_id,
            ))
        elif t_name == "mcp__delegation-mcp__delegate":
            # The delegate badge is emitted by the PROXY when the task is actually
            # created (the delegation spawn path → inject_pump_event delegate_spawn),
            # so a rejected delegation never strands a badge and spawn↔result
            # correlate by a stable task_id. Suppress the inline tool card here.
            pass
        elif t_name == "EnterPlanMode":
            chunks.append(ClaudeStreamChunk(
                event_type="plan_mode",
                event_data={"action": "enter"},
                session_id=self.actual_session_id,
            ))
        elif t_name == "ExitPlanMode":
            logger.info(
                f"[{self.session_id[:8]}] ExitPlanMode "
                f"has_plan={'plan' in t_input if t_input else False}"
            )
            chunks.append(ClaudeStreamChunk(
                event_type="plan_mode",
                event_data={"action": "exit", "tool_input": t_input},
                session_id=self.actual_session_id,
            ))
        elif t_name not in _SKIP_INLINE_TOOLS:
            chunks.append(ClaudeStreamChunk(
                event_type="tool_info",
                event_data={
                    "name": t_name,
                    "summary": _extract_tool_summary(t_name, t_input),
                    "tool_input": t_input,
                },
                session_id=self.actual_session_id,
            ))

        # NOTE: a backgrounded Bash's badge/inline block is emitted at
        # `task_started{local_bash}` (see _handle_task_started), NOT here — so a
        # permission-REJECTED command (which never starts → no task_started)
        # never strands a badge. Its normal tool card is still emitted above.
        # Stage the command by tool_use_id so that spawn event can carry it
        # (task_started itself only brings the model-written description).
        if t_name == "Bash" and t_input.get("run_in_background"):
            tuid = block_info.get("tool_id", "")
            if tuid:
                self._bg_bash_commands[tuid] = str(t_input.get("command") or "")

        # Task-tool checklist maintenance (the TodoWrite successor — see
        # __init__). Runs IN ADDITION to the tool_info card above.
        if t_name == "TaskCreate":
            tool_use_id = block_info.get("tool_id", "")
            subject = str(t_input.get("subject") or "").strip()
            if tool_use_id and subject:
                # The CLI-assigned id only appears in the tool RESULT —
                # stage the subject; _handle_user inserts the item.
                self._pending_task_creates[tool_use_id] = subject
        elif t_name == "TaskUpdate":
            update_chunk = self._apply_task_update(t_input)
            if update_chunk:
                chunks.append(update_chunk)

        self._tool_inputs.pop(idx, None)
        self._tool_input_names.pop(idx, None)
        return chunks

    def _apply_task_update(self, t_input: dict) -> ClaudeStreamChunk | None:
        """Apply a TaskUpdate input to the session checklist.

        TaskUpdate carries the task id directly ({taskId, status?, subject?}),
        so no result parsing is needed. `deleted` removes the item; an update
        for an id we never saw (created before a proxy restart) inserts a
        placeholder so the status still shows.
        """
        tid = str(t_input.get("taskId") or "").strip()
        if not tid:
            return None
        status = str(t_input.get("status") or "").strip()
        subject = str(t_input.get("subject") or "").strip()
        if status == "deleted":
            self._cc_tasks.pop(tid, None)
        else:
            item = self._cc_tasks.setdefault(
                tid, {"content": f"Task #{tid}", "status": "pending"},
            )
            if subject:
                item["content"] = subject
            if status in ("pending", "in_progress", "completed"):
                item["status"] = status
        return self._todo_snapshot_chunk()

    def _todo_snapshot_chunk(self) -> ClaudeStreamChunk:
        """Full-checklist snapshot in the TodoWrite `todos` shape (replace
        semantics — same contract the pump and TodoPanel already speak)."""
        items = list(self._cc_tasks.items())
        # Numeric creation order; non-numeric ids keep insertion order (stable sort).
        items.sort(key=lambda kv: (0, int(kv[0])) if kv[0].isdigit() else (1, 0))
        todos = [
            {"content": item["content"], "status": item["status"]}
            for _, item in items
        ]
        return ClaudeStreamChunk(
            event_type="todo_update",
            # persist_block: the TaskCreate/TaskUpdate family (unlike native TodoWrite)
            # has no tool-path block the panel can rehydrate from on an idle reload, so
            # ask the pump to persist a TodoWrite-shaped snapshot (same mechanism as
            # Codex update_plan). panel_only: the TaskCreate/TaskUpdate calls already
            # render their own inline cards, so the synthesized snapshot is restore-only
            # — the frontend suppresses it inline (no duplicate checklist card).
            event_data={"todos": todos, "persist_block": True, "panel_only": True},
            session_id=self.actual_session_id,
        )

    def _handle_user(self, data: dict) -> list[ClaudeStreamChunk]:
        """Tool results ride back as `user` messages. The only thing mined
        here is the CLI-assigned task id a TaskCreate result carries
        ("Task #N created successfully: …") — the tool INPUT has no id, so
        the checklist item can only be inserted once the result lands."""
        if not self._pending_task_creates:
            return []
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        inserted = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            subject = self._pending_task_creates.get(block.get("tool_use_id", ""))
            if not subject:
                continue
            m = _TASK_CREATED_RE.search(_tool_result_text(block))
            if not m:
                continue
            self._pending_task_creates.pop(block.get("tool_use_id", ""), None)
            self._cc_tasks[m.group(1)] = {"content": subject, "status": "pending"}
            inserted = True
        return [self._todo_snapshot_chunk()] if inserted else []

    def _handle_message_start(self, event: dict) -> list[ClaudeStreamChunk]:
        """A new API turn starting — capture per-turn context for the gauge.

        Subagent completion is driven by the SubagentStop hook (see the
        SubagentRegistry), not inferred from the next message_start.
        """
        ctx = _extract_turn_context(event)
        if ctx > 0:
            self.last_turn_context = ctx
        return []

    def _handle_result(self, data: dict) -> list[ClaudeStreamChunk]:
        """CLI result event — close active tool, emit text+error+metadata."""
        chunks: list[ClaudeStreamChunk] = []

        # Close any tool block that didn't get a stop event
        if self.active_tool:
            chunks.append(ClaudeStreamChunk(
                event_type="tool_end",
                event_data={"tool_id": self.active_tool["tool_id"], "name": self.active_tool["name"]},
                session_id=self.actual_session_id,
            ))
            self.active_tool = None

        result_text = data.get("result", "")
        is_error = bool(data.get("is_error", False))

        # Emit result text / error BEFORE metadata so dashboard ordering is right
        error_text = result_text
        if not error_text and is_error:
            errors_list = data.get("errors", [])
            if errors_list:
                error_text = "; ".join(str(e) for e in errors_list)
        if error_text and is_error:
            chunks.append(ClaudeStreamChunk(
                text=error_text,
                session_id=self.actual_session_id,
                is_error=True,
            ))
        elif (result_text and not self.has_emitted_text
                and result_text != RESUME_HANDSHAKE_RESULT):
            # Result text wasn't streamed via stream_events. The resume
            # handshake sentinel is never emitted: it is CLI plumbing, not an
            # answer — emitting it would both display junk and count as
            # content against the foreign-result gate.
            chunks.append(ClaudeStreamChunk(
                text=result_text,
                session_id=self.actual_session_id,
            ))

        # Metadata
        context_max = _extract_context_window(data)
        meta: dict = {
            "cost_usd": data.get("total_cost_usd", 0.0),
            "duration_ms": data.get("duration_ms", 0),
        }
        if self.last_turn_context > 0 and context_max > 0:
            meta["context_used"] = self.last_turn_context
            meta["context_max"] = context_max
        result_usage = data.get("usage", {}) or {}
        meta["cache_read"] = result_usage.get("cache_read_input_tokens", 0)
        meta["cache_write"] = result_usage.get("cache_creation_input_tokens", 0)
        meta["input_tokens"] = result_usage.get("input_tokens", 0)
        meta["output_tokens"] = result_usage.get("output_tokens", 0)
        chunks.append(ClaudeStreamChunk(
            event_type="metadata",
            event_data=meta,
            session_id=self.actual_session_id,
        ))

        return chunks

    def reset_for_settle(self) -> None:
        """Reset per-turn parsing state on settle entry (after result).

        Clears block_types, active_tool, has_emitted_text, and the tool-input
        buffers. Preserves the spawn counter (agents_spawned) and the
        session-scoped task checklist (_cc_tasks).
        """
        self.block_types.clear()
        self.active_tool = None
        self.has_emitted_text = False
        self._tool_inputs.clear()
        self._tool_input_names.clear()
        self._pending_task_creates.clear()
        self._in_settle = True

    def reset_for_foreign_skip(self) -> None:
        """Reset per-turn parsing state after a skipped foreign result.

        A resume-settlement mini-turn may have set has_emitted_text (or left
        half-open blocks); without this reset the DRIVEN turn's result-text
        fallback in _handle_result would be suppressed and the real answer
        silently dropped. Same clears as reset_for_settle but the turn has
        NOT entered settle — _in_settle stays off so background completions
        that land next still surface with their normal semantics.
        """
        self.block_types.clear()
        self.active_tool = None
        self.has_emitted_text = False
        self._tool_inputs.clear()
        self._tool_input_names.clear()
        self._pending_task_creates.clear()
