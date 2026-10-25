"""The stream pump's event handlers: one method per CommonEvent kind and
the dispatcher that names them (``_process_event``, a table). ``ChatStreamPump``
(``stream_pump.py``) inherits them; the loop, the hook items, the viewers,
the saves and the turn's end stay there with the module-level state the
tests patch. Two handlers live there too, beside the globals they read:
``_on_context_compact`` (the status-surface test pins its compare to that
file) and ``_on_workflow_progress`` (its clock window).
"""

import json
import logging
from typing import TYPE_CHECKING

from core.background import spawn
from core.events import tool_roles
from core.events.common_events import (
    BG_COMMAND_END,
    BG_COMMAND_START,
    CHECK_VERDICT,
    CONTEXT_COMPACT,
    DELEGATE_SPAWN,
    DONE,
    ERROR,
    GOAL_UPDATE,
    METADATA,
    NOTE_MARK,
    PERMISSION_REQUEST,
    PLAN_MODE,
    SUBAGENT_END,
    SUBAGENT_START,
    SYSTEM,
    TEXT,
    THINKING,
    TODO_UPDATE,
    TOOL_INPUT,
    TOOL_RESULT,
    TOOL_USE,
    WORKFLOW_END,
    WORKFLOW_PROGRESS,
    WORKFLOW_START,
    CommonEvent,
    goal_payload_to_state,
    is_note_mark,
)
from core.session.session_state import _chat_streaming_state, set_session_mode
from core.session.transcript_tool_events import result_summary, truncate_result
from services.title_generator import (
    TITLE_CHAR_THRESHOLD as _TITLE_CHAR_THRESHOLD,
    TITLE_TOOL_THRESHOLD as _TITLE_TOOL_THRESHOLD,
)
from storage.automation import run_status
from ws import wire_events as wire

logger = logging.getLogger("otodock")

# Tracks CLI's cumulative cost per session across pump instances.
# CLI's total_cost_usd is cumulative per process — new pumps need the last
# reported value to compute per-turn deltas correctly.
_session_cumulative_cost: dict[str, float] = {}  # session_id -> last CLI cost

# Initial _current_goal marker, distinct from None ("goal known cleared") so
# the change-gate can't skip NULLing chats.thread_goal on a cleared event in a
# pump that never saw the goal being set.
_GOAL_UNSET = object()


if TYPE_CHECKING:
    from core.events.pump_host import PumpHost as _Host
else:
    _Host = object


class PumpEventHandlers(_Host):
    """One method per event kind, named by the table; ``self`` is the
    ``ChatStreamPump`` that mixes them in."""

    # The pump state these handlers assign, declared where they assign it:
    # the type checker reads an attribute a mixin assigns from the mixin
    # first. Annotations only, no class attributes.
    _current_goal: object
    _last_session_cost: float
    _pending_note: bool
    _title_armed: bool
    _todo_block: dict | None

    #: The event kinds and the methods that handle them. Every kind is
    #: exclusive (an event has one type); a kind not named here is ignored.
    _EVENT_HANDLERS: dict[str, str] = {
        TEXT: "_on_text",
        THINKING: "_on_thinking",
        TOOL_USE: "_on_tool_use",
        TOOL_INPUT: "_on_tool_input",
        TOOL_RESULT: "_on_tool_result",
        SUBAGENT_START: "_on_subagent_start",
        SUBAGENT_END: "_on_subagent_end",
        BG_COMMAND_START: "_on_bg_command_start",
        BG_COMMAND_END: "_on_bg_command_end",
        WORKFLOW_START: "_on_workflow_start",
        WORKFLOW_PROGRESS: "_on_workflow_progress",
        WORKFLOW_END: "_on_workflow_end",
        DELEGATE_SPAWN: "_on_delegate_spawn",
        CHECK_VERDICT: "_on_check_verdict",
        PERMISSION_REQUEST: "_on_permission_request",
        PLAN_MODE: "_on_plan_mode",
        TODO_UPDATE: "_on_todo_update",
        GOAL_UPDATE: "_on_goal_update",
        CONTEXT_COMPACT: "_on_context_compact",
        SYSTEM: "_on_system",
        METADATA: "_on_metadata",
        ERROR: "_on_error",
        DONE: "_on_done",
    }

    async def _process_event(self, event: CommonEvent):
        """Process a single CommonEvent — update live state, save events, forward.

        Text is accumulated in _pending_text. Before any non-text event,
        pending text is flushed to _turn_blocks. This preserves interleaving
        so widgets appear in correct position after page refresh.
        live_blocks mirrors this interleaving for reconnect rendering.

        Execution-layer agnostic: operates on CommonEvent, not ClaudeStreamChunk.
        """
        live = _chat_streaming_state.get(self.chat_id)
        ed = event.data
        handler = self._EVENT_HANDLERS.get(event.type)
        if handler is not None:
            await getattr(self, handler)(ed, live)

    async def _on_text(self, ed: dict, live: dict | None) -> None:
        content = ed.get("content", "")
        if content:
            await self._clear_orphan_thinking(live)
            # A progress note (TEXT ``note``) is text kept in a segment of its
            # own: a kind change ends the pending segment, and the live view
            # starts a fresh block, so an answer never lands inside a note.
            note = bool(ed.get("note"))
            if self._pending_text and note != self._pending_note:
                self._flush_pending_text()
            self._pending_note = note
            await self._forward_delta(TEXT, {"content": content, **({"note": True} if note else {})})
            self._pending_text.append(content)
            if live:
                self._live_append_text(live, content, note)
            # LLM chat-title: once the first response crosses the length
            # threshold on the first turn, fire the title upgrade early (feels
            # instant) from the response so far. Disarmed immediately; the
            # PRODUCER_DONE fallback covers shorter turns; the atomic claim
            # guarantees exactly-once.
            if self._title_armed and sum(
                len(t) for t in self._pending_text
            ) >= _TITLE_CHAR_THRESHOLD:
                self._title_armed = False
                from services import title_generator
                spawn(title_generator.request_chat_title(
                    self.chat_id, assistant_excerpt="".join(self._pending_text),
                ), name="request_chat_title", owner="stream_pump")
        elif is_note_mark(ed):
            await self._note_open_text(live)

    async def _note_open_text(self, live: dict | None) -> None:
        """The note mark: the plain text still pending is a progress note (the
        agent's words right before a tool call). A checkpoint never takes
        pending words, so the whole segment is still here however long it
        streamed: it is saved as a note row, its live block becomes a note
        (split off any earlier words the block holds), and the page turns its
        last text block into one. Nothing when no plain text is pending."""
        if not self._pending_text or self._pending_note:
            return
        self._pending_note = True
        words = "".join(self._pending_text)
        blocks = live["live_blocks"] if live else []
        last = blocks[-1] if blocks else None
        if (last is not None and last.get("type") == wire.LIVE_TEXT and not last.get("note")
                and last["content"].endswith(words)):
            if last["content"] == words:
                last["note"] = True
            else:
                last["content"] = last["content"][:-len(words)]
                blocks.append({"type": wire.TEXT, "content": words, "note": True})
        await self._forward_event(TEXT, dict(NOTE_MARK))

    async def _on_thinking(self, ed: dict, live: dict | None) -> None:
        await self._forward_delta(THINKING, ed)
        live = _chat_streaming_state.get(self.chat_id)
        phase = ed.get("phase")
        if phase == "start":
            # The page starts a thinking block here, so the words before it
            # end their segment here too: a note mark later folds only the
            # words the page holds in its last text block.
            self._flush_pending_text()
            # A block left open (no end came) is saved, not overwritten.
            self._save_thinking(live)
            self._thinking_text = ""
            if live:
                live["thinking_active"] = True
                live["thinking_text"] = ""
                live["thinking_tokens"] = 0
        elif phase == "progress":
            # Live-only token-estimate gauge (adaptive-effort models hide
            # the thinking CONTENT — the CLI sends `thinking_tokens` pings
            # instead; see the translator). Never accumulated, never
            # persisted — only the live dict + the forward above.
            if live:
                live["thinking_active"] = True
                live["thinking_tokens"] = ed.get("estimated_tokens", 0)
        elif phase == "end":
            # The phase before the text: an end may carry a last piece.
            self._thinking_text += ed.get("text") or ""
            if ed.get("note"):
                # Re-sent as note text right after (a Claude narration
                # block): no thinking row for it.
                self._thinking_text = ""
            self._save_thinking(live)
            self._thinking_text = ""
            if live:
                live["thinking_active"] = False
                live["thinking_text"] = ""
                live["thinking_tokens"] = 0
        elif ed.get("text"):
            self._thinking_text += ed["text"]
            if live:
                live["thinking_text"] = self._thinking_text

    def _save_thinking(self, live: dict | None) -> None:
        """The thinking streamed so far as one ``thinking`` block, after the
        text before it; nothing when no text streamed (no empty rows)."""
        if self._thinking_text:
            self._flush_pending_text()
            thinking_block = self._stamp_speaker(
                {"type": wire.THINKING, "content": self._thinking_text})
            self._turn_blocks.append(thinking_block)
            if live:
                live["live_blocks"].append(thinking_block)
            self._thinking_text = ""

    async def _on_tool_use(self, ed: dict, live: dict | None) -> None:
        await self._clear_orphan_thinking(live)
        await self._forward_event(TOOL_USE, ed)
        tool_name = ed.get("name", "")
        # LLM chat-title: tool-count early trigger. Counted at branch top —
        # before _skips_tool_persist — so subagent/delegate spawns count.
        self._tool_seen += 1
        if self._title_armed and self._tool_seen >= _TITLE_TOOL_THRESHOLD:
            self._title_armed = False
            from services import title_generator
            spawn(title_generator.request_chat_title(
                self.chat_id, assistant_excerpt="".join(self._pending_text),
            ), name="request_chat_title", owner="stream_pump")
        # A memory tool call resets this session's capture-nudge counter
        # (services/memory_nudge). Name shapes: mcp__memory-mcp__memory
        # (CLI/Codex) or bare "memory" (Direct LLM).
        if tool_name == "memory" or tool_name.startswith("mcp__memory-mcp__"):
            try:
                from services.memory import memory_nudge
                memory_nudge.record_memory_call(self.session_id)
            except ImportError:
                pass
        # Record tool-start time for any MCP with `outputs` declared so
        # mcp_output_relocation can diff source dir by mtime on
        # TOOL_RESULT and move just the newly-written files.
        if tool_name.startswith("mcp__"):
            try:
                # mcp__{server_name}__{tool} → resolve server_name to MCP name
                parts = tool_name.split("__", 2)
                if len(parts) >= 2:
                    from services.mcp import mcp_output_relocation, mcp_registry
                    server_name = parts[1]
                    for n, m in mcp_registry.get_all_manifests().items():
                        if (m.server_name or m.name) == server_name and m.outputs:
                            mcp_output_relocation.record_tool_start(
                                self.session_id, n,
                            )
                            break
            except ImportError:
                logger.debug("record_tool_start failed", exc_info=True)
        # Skip tool tracking for Agent/Task/PlanMode (they have dedicated events)
        if not self._skips_tool_persist(tool_name):
            tool_id = ed.get("tool_id") or tool_name
            self._flush_pending_text()
            tool_block = self._stamp_speaker({
                "type": wire.PERSISTED_TOOL, "name": tool_name,
                "tool_id": tool_id, "summary": "", "active": True,
                "tool_input": None,
                "_insert_idx": len(self._turn_blocks),  # track position for correct DB ordering
            })
            self._active_tools[tool_id] = tool_block
            if live:
                live["active_tools"] = list(self._active_tools.values())
                live["live_blocks"].append(tool_block)

    async def _on_tool_input(self, ed: dict, live: dict | None) -> None:
        await self._forward_event(TOOL_INPUT, ed)
        name = ed.get("name", "")
        summary = ed.get("summary", "")
        tool_input = ed.get("tool_input")
        for t in self._active_tools.values():
            if t["name"] == name:
                t["summary"] = summary
                t["tool_input"] = tool_input
                break
        # Capture plan filename from file-writing tools. Normalize
        # separators first: a Windows-satellite session reports the
        # host-absolute path with backslashes (C:\Users\...\.claude\plans\x.md).
        if tool_roles.role_of(name) == tool_roles.WRITE:
            fp = (ed.get("file_path", "") or "").replace("\\", "/")
            if ("/.claude/plans/" in fp or "/.codex/plans/" in fp) and fp.endswith(".md"):
                self._plan_filename = fp.rsplit("/", 1)[-1]

    async def _on_tool_result(self, ed: dict, live: dict | None) -> None:
        tool_id = ed.get("tool_id") or ed.get("name", "")
        tool_name = ed.get("name", "")
        # Codex-style completions carry the output inline (the CLI path
        # gets it via the PostToolUse hook instead): attach it to the
        # still-active block + emit the hook path's `tool_result` frame
        # BEFORE tool_end, and keep the body out of the tool_end frame.
        result_content = ed.pop("result_content", None)
        result_is_error = bool(ed.pop("is_error", False))
        # A Codex MCP result's pictures and blobs: kept and shown for a
        # tool its MCP opts in (``result_files``), never forwarded or
        # recorded with the tool block.
        result_blocks = ed.pop("result_blocks", None)
        result_owner = ed.pop("result_session_id", None) or self.session_id
        if result_blocks:
            from services.mcp import result_files
            await result_files.keep_blocks(result_owner, tool_name, result_blocks,
                                           result_content or "", is_error=result_is_error)
        if result_content:
            result_content = truncate_result(result_content)
            summary = result_summary(tool_name, result_content)
            target = self._active_tools.get(tool_id)
            if target is None:
                for t in self._active_tools.values():
                    if t["name"] == tool_name:
                        target = t
                        break
            if target is not None:
                target["tool_result"] = result_content
                target["result_summary"] = summary
                target["is_error"] = result_is_error
            await self._forward({"pump_type": wire.PUMP_WS_EVENT,
                "event": {"type": wire.TOOL_RESULT,
                           "tool_name": tool_name,
                           "tool_use_id": tool_id,
                           "summary": summary,
                           "result_content": result_content}})
        await self._forward_event(TOOL_RESULT, ed)
        # A foreground subagent's call ended: its spawn block stops no
        # checkpoint any more, report or not.
        self._spawns_done.add(tool_id)
        tool_evt = self._active_tools.pop(tool_id, None)
        if tool_evt:
            tool_evt["active"] = False  # Also updates live_blocks (same dict ref)
            self._flush_pending_text()
            # Insert at the position where the tool STARTED (not at end).
            # This ensures hook-pushed events (images, previews) that arrived
            # during tool execution appear AFTER the tool block in DB order.
            insert_idx = tool_evt.pop("_insert_idx", len(self._turn_blocks))
            insert_idx = min(insert_idx, len(self._turn_blocks))
            self._turn_blocks.insert(insert_idx, tool_evt)
        if live:
            live["active_tools"] = list(self._active_tools.values())

        await self._relocate_mcp_outputs(tool_name, tool_evt)
        await self._charge_mcp_tool(tool_name, tool_evt)

    async def _relocate_mcp_outputs(self, tool_name: str, tool_evt: dict | None) -> None:
        """The result landed: the files an MCP with declared outputs just
        wrote move to the session's folder (TOOL_RESULT's second step)."""
        # Relocate any files an MCP with `outputs` just produced into
        # the session-scoped workspace subdir. Camoufox screenshots
        # flow through here (privacy + cleanup). For remote sessions
        # the relocated files are also pushed to the satellite so the
        # agent CLI on the satellite can read them at the same path.
        if tool_name.startswith("mcp__"):
            try:
                parts = tool_name.split("__", 2)
                if len(parts) >= 2:
                    from services.mcp import mcp_output_relocation, mcp_registry
                    server_name = parts[1]
                    tool_only = parts[2] if len(parts) >= 3 else ""
                    for n, m in mcp_registry.get_all_manifests().items():
                        if (m.server_name or m.name) == server_name and m.outputs:
                            # tool_evt carries the tool's result text (set by
                            # the PostToolUse hook) → precise move-by-filename;
                            # None on the Codex/interactive paths → mtime scan.
                            await mcp_output_relocation.relocate_and_push_for_tool(
                                self.session_id, n, tool_only,
                                result_text=(tool_evt or {}).get("tool_result"),
                            )
                            break
            except Exception:  # noqa: BLE001  # output relocation spans fs, docker and satellite: the turn goes on
                logger.debug("output relocation failed", exc_info=True)

    async def _charge_mcp_tool(self, tool_name: str, tool_evt: dict | None) -> None:
        """The result landed: the manifest's fee for the call, when one is
        declared and the call did not fail (TOOL_RESULT's last step)."""
        # Generic per-tool cost evaluation. Looks up the manifest's
        # `costs` block (if any) and applies the first matching rule
        # against the args we stashed when we saw the TOOL_INPUT event.
        # See services/mcp/mcp_cost_engine.py.
        #
        # Caveats:
        #   - Failed tool calls are NOT charged: the PostToolUse hook
        #     reports `is_error` (the structured MCP flag, or an "Error…"
        #     result for MCPs that return failures as plain text), which we
        #     stashed on the tool block above. The direct/codex paths have
        #     no such hook, so a failed tool there can still be charged
        #     (secondary follow-up).
        #   - Aborts between TOOL_USE and TOOL_RESULT are NOT charged
        #     (TOOL_RESULT never fires).
        if (
            tool_evt is not None
            and not tool_evt.get("is_error")
            and tool_name.startswith("mcp__")
        ):
            try:
                from services.mcp import mcp_cost_engine
                found = mcp_cost_engine.find_costs_block_for_tool(tool_name)
                if found is not None:
                    mcp_name, plain_tool, costs_block = found
                    hit = mcp_cost_engine.evaluate(
                        mcp_name, plain_tool, tool_evt.get("tool_input"), costs_block,
                    )
                    if hit is not None and hit.amount > 0:
                        self._total_cost_delta += hit.amount
                        key = (hit.provider, hit.model)
                        self._mcp_cost_by_key[key] = round(
                            self._mcp_cost_by_key.get(key, 0.0) + hit.amount, 6,
                        )
                        fee_event = {
                            "type": wire.MCP_COST,
                            "cost_usd": hit.amount,
                            # A tool fee is money whatever credential the
                            # LLM runs on: the cost icon shows it even on a
                            # subscription / local-model chat.
                            "cost_billed": True,
                            "provider": hit.provider,
                            "model": hit.model,
                            "tool": plain_tool,
                            "mcp": mcp_name,
                        }
                        await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": fee_event})
                        # Its own row, so a reload adds the fee back up
                        # (the metadata row carries the model's cost only).
                        self._flush_pending_text()
                        self._turn_blocks.append(fee_event)
            except Exception:
                logger.exception("mcp cost evaluation failed for %s", tool_name)

    async def _on_subagent_start(self, ed: dict, live: dict | None) -> None:
        await self._forward_event(SUBAGENT_START, ed)
        self._flush_pending_text()
        spawn_block = {"type": wire.TASK_SPAWN, **ed}
        self._turn_blocks.append(spawn_block)
        is_bg = ed.get("run_in_background", False)
        if live:
            agent_block = {
                "type": wire.LIVE_AGENT,
                "description": ed.get("description", ""),
                "subagent_type": ed.get("subagent_type", ""),
                "background": is_bg,
                "tool_use_id": ed.get("tool_use_id", ""),  # completion key (SUBAGENT_END)
                "active": True,
                # Full Agent tool input — reconnect renders the same
                # expandable detail as the live pill.
                "tool_input": ed.get("tool_input"),
            }
            live["active_agents"].append(agent_block)
            live["live_blocks"].append(agent_block)  # Same dict ref — updates propagate
            self._live_twins[id(spawn_block)] = (spawn_block, agent_block)

    async def _on_subagent_end(self, ed: dict, live: dict | None) -> None:
        # Deterministic per-agent completion (stdout task_notification
        # backup path; the SubagentStop hook delivers the same WS event
        # out-of-band via push_pump_event). Clears the widget by id —
        # order-independent, no FIFO.
        tuid = ed.get("tool_use_id", "")
        self._spawns_done.add(tuid)
        await self._forward_event(SUBAGENT_END, {"tool_use_id": tuid})
        if live and tuid:
            for a in live["active_agents"]:
                if a.get("tool_use_id") == tuid:
                    a["active"] = False
                    break

    async def _on_bg_command_start(self, ed: dict, live: dict | None) -> None:
        # Backgrounded bash command — mirror SUBAGENT_START: a live badge +
        # inline block keyed by the Bash tool_use_id. The dashboard FOLDS
        # the command's normal tool card into this block by that id
        # (pairBgCommandBlocks) — one expandable pill per command.
        # Completion (BG_COMMAND_END) clears it by id, order-independent
        # (multiple bg commands run concurrently).
        await self._forward_event(BG_COMMAND_START, ed)
        self._flush_pending_text()
        spawn_block = {"type": wire.BG_COMMAND_SPAWN, **ed}
        self._turn_blocks.append(spawn_block)
        if live:
            command_block = {
                "type": wire.LIVE_COMMAND,
                "command": ed.get("command", ""),
                "description": ed.get("description", ""),
                "tool_use_id": ed.get("tool_use_id", ""),  # completion key (BG_COMMAND_END)
                "active": True,
            }
            live["active_commands"].append(command_block)
            live["live_blocks"].append(command_block)  # same dict ref — updates propagate
            self._live_twins[id(spawn_block)] = (spawn_block, command_block)

    async def _on_bg_command_end(self, ed: dict, live: dict | None) -> None:
        # Deterministic per-command completion — driven by the CLI
        # task_updated frame (no completion hook exists for bg bash). Clears
        # the widget by tool_use_id, order-independent (no FIFO guessing).
        tuid = ed.get("tool_use_id", "")
        await self._forward_event(BG_COMMAND_END, {"tool_use_id": tuid,
                                                   "status": ed.get("status", "")})
        if live and tuid:
            for c in live["active_commands"]:
                if c.get("tool_use_id") == tuid:
                    c["active"] = False
                    break

    async def _on_workflow_start(self, ed: dict, live: dict | None) -> None:
        # Dynamic workflow (Opus 4.8 Workflow tool) — one orchestration
        # spawning many agents, surfaced as a live phase/agent tree in the
        # floating WorkflowPanel. Like the TodoPanel checklist, this is
        # live-state only (not persisted as a turn block) — the agents'
        # actual output streams as normal text/tool blocks.
        tuid = ed.get("tool_use_id", "")
        await self._forward_event(WORKFLOW_START, {
            "tool_use_id": tuid, "workflow_name": ed.get("workflow_name", ""),
        })
        if live:
            live["workflows"][tuid] = {
                "tool_use_id": tuid,
                "workflow_name": ed.get("workflow_name", ""),
                "progress": [], "active": True,
            }

    async def _on_workflow_end(self, ed: dict, live: dict | None) -> None:
        tuid = ed.get("tool_use_id", "")
        self._wf_last_forward.pop(tuid, None)
        await self._forward_event(WORKFLOW_END, {"tool_use_id": tuid})
        if live and tuid in live.get("workflows", {}):
            live["workflows"][tuid]["active"] = False

    async def _on_delegate_spawn(self, ed: dict, live: dict | None) -> None:
        evt = {"type": wire.DELEGATE_SPAWN, **ed}
        await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
        self._flush_pending_text()
        self._turn_blocks.append(evt)
        if live:
            delegate_block = {
                "type": wire.LIVE_DELEGATE,
                "task_id": ed.get("task_id", ""),
                "task_name": ed.get("task_name", ""),
                "agent": ed.get("agent", ""),
                # Full prompt for the expandable pill (reconnect parity).
                "prompt": ed.get("prompt", ""),
                "active": True,
                "status": run_status.RUNNING,  # a DELEGATE_RESULTS word on terminal
            }
            live["active_delegates"].append(delegate_block)
            live["live_blocks"].append(delegate_block)
            self._live_twins[id(evt)] = (evt, delegate_block)

    async def _on_check_verdict(self, ed: dict, live: dict | None) -> None:
        # A check's verdict (CHECKS.md "Rendering"): the card rides the
        # turn in order — forwarded live, kept in the live state for a
        # reconnect, persisted as an event row with the turn's blocks.
        evt = {**ed, "type": wire.CHECK_VERDICT}
        await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
        self._flush_pending_text()
        self._turn_blocks.append(evt)
        if live:
            live["live_blocks"].append(evt)

    async def _on_permission_request(self, ed: dict, live: dict | None) -> None:
        # The engine's own request (Claude's stdio channel): a prompt
        # waiting on a person, answered through the raising session's
        # layer (core/session/engine_prompts).
        from core.session import engine_prompts
        if engine_prompts.withdrawn_before_registered(str(ed.get("request_id") or "")):
            return   # the engine withdrew it before the card could show
        engine_prompts.watch(ed.get("session_id") or self.session_id, ed,
                             chat_id=self.chat_id)
        self._flush_pending_text()
        self._turn_blocks.append({"type": wire.PERMISSION_PROMPT, **ed})
        # Gate: use the same queue as perm_queue permissions
        perm_data = {**ed, "event_type": wire.ITEM_PERMISSION_PROMPT}
        await self._queue_or_show_permission(perm_data)

    async def _on_plan_mode(self, ed: dict, live: dict | None) -> None:
        evt = {"type": wire.PLAN_MODE, **ed}
        await self._forward(
            {"pump_type": wire.PUMP_WS_EVENT, "event": evt},
        )
        self._flush_pending_text()
        self._turn_blocks.append(evt)
        if live:
            live["live_blocks"].append(evt)
        # Update hook mode + DB so plan mode is enforced even without WS
        action = ed.get("action", "")
        if action == "enter":
            set_session_mode(self.session_id, "plan")
            if self.chat_id:
                self._submit_chat_update("plan_mode", permission_mode="plan")

    async def _on_todo_update(self, ed: dict, live: dict | None) -> None:
        # Cross-layer todo/checklist update (TodoWrite for CLI, update_plan for Codex)
        todos = ed.get("todos", [])
        self._current_todos = todos
        await self._forward_event(TODO_UPDATE, {"todos": todos})
        if live:
            live["todos"] = todos
        # Some checklist sources have no tool-path block the panel can rehydrate
        # from on reload — Codex's update_plan (no block at all) and the CLI
        # TaskCreate/TaskUpdate family (its own inline cards, but no TodoWrite
        # snapshot). Persist a TodoWrite-shaped block so the checklist survives
        # turn end + refresh — one block per turn-segment, updated in place (the
        # frontend + get_last_todo_snapshot restore the panel from the last one).
        # panel_only marks the Task-tool case so the frontend suppresses it inline
        # (the TaskCreate/TaskUpdate cards already render); codex omits it (the
        # synthesized block IS its only inline representation).
        if ed.get("persist_block"):
            if self._todo_block is None:
                self._flush_pending_text()
                self._todo_block = self._stamp_speaker({
                    "type": wire.PERSISTED_TOOL, "name": tool_roles.TODO_SNAPSHOT,
                    "tool_input": {"todos": todos},
                })
                if ed.get("panel_only"):
                    self._todo_block["panel_only"] = True
                self._turn_blocks.append(self._todo_block)
            else:
                self._todo_block["tool_input"]["todos"] = todos
                if self._todo_ref is not None:
                    # A checkpoint saved the block: its row follows.
                    self._rewrite_row(self._todo_ref, self._todo_block)

    async def _on_goal_update(self, ed: dict, live: dict | None) -> None:
        # Codex thread goal — chat-durable panel state, NOT a turn block
        # (like workflows). Reloads restore it from chats.thread_goal via
        # restore.goal; live.goal only covers a mid-turn reconnect. The DB
        # write is change-gated: goal notifications can repeat every turn
        # and each update_chat bumps updated_at.
        goal = goal_payload_to_state(ed)
        changed = goal != self._current_goal
        self._current_goal = goal
        await self._forward_event(GOAL_UPDATE, {"goal": goal})
        if live:
            live["goal"] = goal
        if changed and self.chat_id:
            self._submit_chat_update(
                "thread_goal", thread_goal=json.dumps(goal) if goal else None)

    async def _on_system(self, ed: dict, live: dict | None) -> None:
        subtype = ed.get("subtype", "")
        # Subagent completion (fg + bg) is a first-class SUBAGENT_END
        # event keyed by tool_use_id (the SubagentStop hook + the
        # task_notification backup) — no FIFO matching. No layer mints
        # a `fg_agents_complete` subtype; that frame's one minter is the
        # liveness clear (ws/dashboard_server_events).
        if subtype == wire.SUBTYPE_MEETING_STARTED:
            evt = {"type": wire.SYSTEM, **ed}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            # Save immediately so it appears before any QUEUE_TURN user messages
            self._save_turn_blocks()
            if live:
                live["meeting_participants"] = ed.get("participants", [])
                # Don't re-add to live_blocks — it's now in DB and will come
                # via chat_history on reconnect.  Re-adding caused duplicates.
        elif subtype == wire.SUBTYPE_MEETING_TURN_START:
            self._meeting_agent = ed.get("agent", "")
            evt = {"type": wire.SYSTEM, **ed}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            if live:
                live["meeting_agent"] = self._meeting_agent
                live["live_blocks"].append(evt)
        elif subtype == wire.SUBTYPE_MEETING_FAILED:
            # Pre-turn meeting failure (admission denial, spawn failure…)
            # surfaced by the orchestrator: persist immediately so the
            # attach-time chat_history re-send carries the banner, and
            # clear live meeting state so the pill goes away. Not re-added
            # to live_blocks after the save (meeting_started precedent —
            # it would duplicate on reconnect).
            self._meeting_agent = None
            evt = {"type": wire.SYSTEM, **ed}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            self._save_turn_blocks()
            if live:
                live["meeting_agent"] = None
                live["meeting_participants"] = []
        elif subtype in (wire.SUBTYPE_MEETING_TURN_END, wire.SUBTYPE_MEETING_CONCLUDED, wire.SUBTYPE_MEETING_AGENT_FAILED):
            if subtype in (wire.SUBTYPE_MEETING_TURN_END, wire.SUBTYPE_MEETING_CONCLUDED):
                self._meeting_agent = None
            evt = {"type": wire.SYSTEM, **ed}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            # Meeting costs now arrive as separate METADATA events with
            # _meeting_cost=True (emitted by meeting_produce after computing
            # per-turn delta). No cost extraction from turn_end/concluded.
            # Save each meeting turn's blocks immediately so DB order
            # matches chronological order (user QUEUE_TURN messages also
            # save immediately, and would otherwise appear before all
            # meeting content which is deferred to PRODUCER_DONE).
            self._save_turn_blocks()
            if live:
                if subtype in (wire.SUBTYPE_MEETING_TURN_END, wire.SUBTYPE_MEETING_CONCLUDED):
                    live["meeting_agent"] = None
                if subtype == wire.SUBTYPE_MEETING_CONCLUDED:
                    live["meeting_participants"] = []
                live["live_blocks"].append(evt)
        elif subtype not in wire.SYSTEM_SUBTYPES_TRANSIENT:
            evt = {"type": wire.SYSTEM, **ed}
            await self._forward(
                {"pump_type": wire.PUMP_WS_EVENT, "event": evt},
            )
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            if live:
                live["live_blocks"].append(evt)

    async def _on_metadata(self, ed: dict, live: dict | None) -> None:
        # Persist Codex thread_id for resume after proxy restart
        codex_tid = ed.get("codex_thread_id")
        if codex_tid and self.chat_id:
            self._submit_chat_update("codex_thread_id", codex_thread_id=codex_tid)
            return  # internal metadata — don't forward to WS or save as turn block

        # Display-only flag: the chat hides the cost line + gauge for a
        # subscription / local-model turn. Stamped here, the one place
        # every emitter (CLI, Codex, direct, satellite, meetings) passes
        # through, so it rides the WS frame AND the persisted row. A
        # meeting turn names the speaker's session (each participant
        # runs on its own account); accounting below is untouched.
        cost_billed = await self._cost_billed_for(ed.get("session_id") or self.session_id)
        is_delta = ed.get("cost_is_delta") or ed.get("_meeting_cost")
        if is_delta:
            # Cost is already a per-turn delta (Direct LLM, Codex, meetings).
            # Use directly — don't touch cumulative tracking.
            turn_cost = ed.get("cost_usd", 0)
            meta_event = {"type": wire.METADATA, "cost_usd": turn_cost,
                          "cost_billed": cost_billed}
            # Preserve context/cache/duration fields if present
            for k in ("context_used", "context_max", "cache_read",
                      "cache_write", "input_tokens", "output_tokens",
                      "duration_ms"):
                if k in ed:
                    meta_event[k] = ed[k]
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": meta_event})
            self._flush_pending_text()
            self._turn_blocks.append(meta_event)
            self._total_cost_delta += turn_cost
            self._llm_cost_delta += turn_cost
        else:
            # Cost is CUMULATIVE session total (CLI). Compute per-turn delta.
            session_cost = ed.get("cost_usd", 0)
            if session_cost < self._last_session_cost:
                # CLI process restarted — cost counter reset
                self._last_session_cost = 0.0
            turn_cost = max(0, session_cost - self._last_session_cost)
            self._last_session_cost = session_cost
            _session_cumulative_cost[self.session_id] = session_cost
            # The baseline made durable for a resume after a proxy restart
            # (the turn-start callback seeds from it: _seed_cost_baseline).
            if self.chat_id:
                self._submit_chat_update(
                    "engine_cost", engine_cost_total=session_cost,
                    engine_cost_session_id=self.session_id,
                )
            # Forward per-turn cost (not cumulative)
            meta_event = {**ed, "cost_usd": turn_cost, "cost_billed": cost_billed}
            await self._forward_event(METADATA, meta_event)
            self._flush_pending_text()
            self._turn_blocks.append({"type": wire.METADATA, **meta_event})
            self._total_cost_delta += turn_cost
            self._llm_cost_delta += turn_cost
        # Track context + cache stats for persistence
        ctx_used = ed.get("context_used", 0)
        ctx_max = ed.get("context_max", 0)
        if ctx_used > 0 and ctx_max > 0:
            self._context_used = ctx_used
            self._context_max = ctx_max
        it = ed.get("input_tokens", 0)
        cr = ed.get("cache_read", 0)
        cw = ed.get("cache_write", 0)
        ot = ed.get("output_tokens", 0)
        if it > 0:
            self._input_tokens = it
        if ot > 0:
            self._output_tokens = ot
        if cr > 0 or cw > 0:
            self._cache_read = cr
            self._cache_write = cw

    async def _on_error(self, ed: dict, live: dict | None) -> None:
        # NOTE: the main `_run` loop intercepts ERROR (forward + break) before
        # this dispatcher is reached, so a grace-expiry's durable marker is
        # persisted there (see the ERROR branch in `_run`). This path remains
        # for any future caller that routes an ERROR through `_process_event`.
        await self._forward_event(ERROR, {"message": ed.get("message", "")})

    async def _on_done(self, ed: dict, live: dict | None) -> None:
        # Turn boundary — flush all pending data to DB in order
        self._flush_pending_text()
        await self._flush_pending_previews()
        for tool_evt in self._active_tools.values():
            self._turn_blocks.append(tool_evt)
        self._active_tools.clear()
        self._save_turn_blocks()
        self._engine_turn_ended()
        self._pending_text.clear()
        if live:
            # Don't reset live_blocks — keep accumulating for live_state
            # reconnect (DB messages are truncated at _db_msg_cutoff_id)
            self._live_append_text(live, "\n\n")  # paragraph break between turns
            live["active_tools"] = []
        await self._forward({"pump_type": wire.PUMP_IS_DONE})
