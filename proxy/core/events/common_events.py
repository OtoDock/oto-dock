"""CommonEvent schema — universal event format for all execution layers.

All execution layers (CLI, Direct LLM, Codex, etc.) translate their native
events into CommonEvent objects. The ChatStreamPump consumes CommonEvent
instead of ClaudeStreamChunk, making it execution-layer agnostic.

This is the contract between execution layers and the pump.
"""

from dataclasses import dataclass, field
import time


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

TEXT = "text"
THINKING = "thinking"
TOOL_USE = "tool_use"            # tool execution started (maps to CLI tool_start)
TOOL_INPUT = "tool_input"        # tool input details (maps to CLI tool_info)
TOOL_RESULT = "tool_result"      # tool execution completed (maps to CLI tool_end)
PERMISSION_REQUEST = "permission_request"
QUESTION = "question"
SUBAGENT_START = "subagent_start"  # Agent tool spawned (maps to CLI task_spawn)
SUBAGENT_END = "subagent_end"      # agent completed (SubagentStop hook / task_notification)
BG_COMMAND_START = "bg_command_start"  # run_in_background Bash spawned (CLI task_started:local_bash)
BG_COMMAND_END = "bg_command_end"      # background command finished (CLI task_updated{completed}/task_notification)
DELEGATE_SPAWN = "delegate_spawn"  # delegate_task MCP tool
DELEGATE_RESULT = "delegate_result"
CHECK_VERDICT = "check_verdict"    # a check's verdict on the turn (CHECKS.md), injected by the evaluator
WORKFLOW_START = "workflow_start"      # dynamic-workflow tool spawned (CLI task_started:local_workflow)
WORKFLOW_PROGRESS = "workflow_progress"  # live phase/agent tree snapshot (CLI task_progress.workflow_progress)
WORKFLOW_END = "workflow_end"          # workflow finished (CLI task_notification / task_updated)
PLAN_MODE = "plan_mode"
SYSTEM = "system"
METADATA = "metadata"
TODO_UPDATE = "todo_update"      # todo/checklist state change
GOAL_UPDATE = "goal_update"      # codex per-thread long-running goal change
CONTEXT_COMPACT = "context_compact"  # context compression event
DONE = "done"                    # turn boundary
ERROR = "error"
QUEUE_TURN = "queue_turn"        # queued user message boundary
ARTIFACT_TURN = "artifact_turn"  # queued artifact-interaction delivery boundary
PRODUCER_DONE = "producer_done"  # producer finished (all turns complete)


# ---------------------------------------------------------------------------
# CommonEvent dataclass
# ---------------------------------------------------------------------------

@dataclass
class CommonEvent:
    """Universal event emitted by all execution layers.

    The pump processes these instead of ClaudeStreamChunk. Each execution
    layer has a translator that converts native events to CommonEvent.

    Type-specific data payloads:

    TEXT:
        {"content": str}

    THINKING:
        {"phase": "start"|"delta"|"end", "text": str}

    TOOL_USE:
        {"name": str, "tool_id": str}

    TOOL_INPUT:
        {"name": str, "summary": str, "tool_input": dict|None,
         "file_path": str}  # optional, for Write/Edit

    TOOL_RESULT:
        {"name": str, "tool_id": str}

    PERMISSION_REQUEST:
        {"request_id": str, "tool_name": str, "tool_input": dict}

    QUESTION:
        {"tool_name": str, "tool_input": dict}

    SUBAGENT_START:
        {"description": str, "subagent_type": str,
         "run_in_background": bool, "tool_use_id": str}
        tool_use_id is the Agent/Task tool_use id — the dashboard keys the
        subagent widget by it so completion (SUBAGENT_END) clears the right
        block regardless of finish order. (CLI agent_id/task_id stays
        proxy-internal — see core/session_state.SubagentRegistry.)

    SUBAGENT_END:
        {"tool_use_id": str}
        Emitted when a subagent finishes. Driven by the SubagentStop hook
        (idle-safe, out-of-band) with the CLI task_notification as a stdout
        backup; both dedup through the per-session SubagentRegistry.

    BG_COMMAND_START:
        {"tool_use_id": str, "command": str, "description": str}
        Emitted for a run_in_background Bash tool. tool_use_id is the Bash
        tool_use id — the dashboard keys the command badge/block by it so
        BG_COMMAND_END clears the right one regardless of finish order. The
        background shell id (task_id) stays proxy-internal — see
        core/bg_command_state.BackgroundCommandRegistry.

    BG_COMMAND_END:
        {"tool_use_id": str, "status": str}
        Emitted when a background command finishes. Driven by the CLI
        task_updated{patch.status} on stdout (task_notification as a backup);
        both dedup through the per-session BackgroundCommandRegistry. Unlike
        subagents there is NO completion hook, so completion is only observed
        while stdout is read (live turn, task settle, or the post-turn monitor).

    WORKFLOW_START:
        {"tool_use_id": str, "workflow_name": str}

    WORKFLOW_PROGRESS:
        {"tool_use_id": str, "workflow_progress": list}
        Full phase/agent tree snapshot (replace semantics) from the CLI's
        task_progress event. Previews are truncated by the translator.

    WORKFLOW_END:
        {"tool_use_id": str}

    DELEGATE_SPAWN:
        {"task_name": str, "agent": str}

    CHECK_VERDICT:
        The card event of services/checks/render.card_event: {"check": str,
        "status": "pass"|"fail"|"error"|"skipped", "summary": str,
        "findings": list, "round": int, "rounds": int, "ran_on": str, ...}.
        Forwarded as-is, kept in the live state and persisted as an event
        row, in order with the turn's blocks.

    PLAN_MODE:
        {"action": "enter"|"exit"}
        On exit with plan content (from ExitPlanMode tool_input):
        {"action": "exit", "plan": str, "filename": str}

    TODO_UPDATE:
        {"todos": [{"content": str, "status": "pending"|"in_progress"|"completed"}]}
        Emitted by CLI (TodoWrite), Codex (update_plan), etc.

    GOAL_UPDATE:
        {"objective": str, "status": str, "token_budget": int|None,
         "tokens_used": int, "time_used_seconds": int, "cleared": bool}
        Codex per-thread long-running goal (thread/goal/updated|cleared).
        status: "active" | "complete" | "paused" | "usageLimited" |
        "budgetLimited" — a model "mark complete" is an update with
        status "complete", not a cleared event. cleared=True carries no
        goal fields — the chat's goal was removed (thread/goal/clear RPC).

    CONTEXT_COMPACT:
        {"phase": "started"|"completed",
         "trigger": "auto"|"manual",
         "pre_tokens": int|None,
         "post_tokens": int|None,
         "messages_summarized": int|None}

    SYSTEM:
        {"subtype": str, ...}

    METADATA:
        {"cost_usd": float, "context_used": int, "context_max": int,
         "cache_read": int, "cache_write": int,
         "input_tokens": int, "output_tokens": int,
         "cost_is_delta": bool}
        cost_is_delta: True if cost_usd is already a per-turn delta (Direct LLM,
        Codex). False/absent if cost_usd is cumulative session total (CLI).
        The pump computes delta for cumulative, uses directly for delta.
        The pump then stamps ``cost_billed`` (bool) on the forwarded/persisted
        metadata block — False when the session's credential is a
        subscription (oauth) or a local model (local_endpoint), so the chat
        hides the cost; the producers never set it. A meeting cost event
        (``_meeting_cost``) may carry ``session_id`` = the speaker's session
        so the flag is resolved per participant.

    DONE:
        {}

    ERROR:
        {"message": str}

    QUEUE_TURN:
        {"text": str, "event_data": dict}
        text = the raw user text of the drained batch (the user row);
        event_data = the row's attachment meta ({"images": [{name, path}],
        "files": [{path, name}]}), empty when the batch carried none.

    ARTIFACT_TURN:
        {"interactions": [{"token": str, "title": str, "payload": Any,
                           "payload_json": str}], "text": str}
        text = the framed prompt already sent to the engine; the pump
        persists one artifact_interaction event row per entry.
    """

    type: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class TurnInput:
    """One user message as the engine and the transcript each see it.

    ``text`` is the raw typed text (the ``chat_messages`` row); ``cli_text``
    is the engine prompt: the same text plus the sandbox-virtual paths of
    the attached photos and files for engines that open them with their
    Read tool. ``images`` are the vision blocks of an engine that takes
    photos inline (Direct LLM) — empty otherwise. ``image_meta`` and
    ``files`` are the row's attachment meta (``{name, path}`` per photo,
    ``{path, name}`` per file); they ride the ``queued`` / ``steered`` /
    ``queue_sent`` frames so the dashboard renders the chips and can hand
    the attachments back to the composer on a cancel.

    The shape is what the idle send builds, what a steer writes into the
    live turn, and what the pump's message queue holds — so a message sent
    while the agent works loses nothing an idle send would carry.
    """

    text: str
    cli_text: str = ""
    images: list[dict] = field(default_factory=list)
    image_meta: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.cli_text:
            self.cli_text = self.text

    @property
    def event_meta(self) -> dict:
        """The row's ``event_data`` dict: only the keys with content."""
        meta: dict = {}
        if self.image_meta:
            meta["images"] = list(self.image_meta)
        if self.files:
            meta["files"] = list(self.files)
        return meta

    def frame_fields(self) -> dict:
        """The attachment keys a queue/steer frame carries — absent when the
        message has none, so text-only frames keep their exact shape."""
        return self.event_meta

    @classmethod
    def combine(cls, items: "list[TurnInput]") -> "TurnInput":
        """A drained batch as ONE turn: texts joined with a blank line (the
        user row and the engine prompt alike), attachments concatenated."""
        return cls(
            text="\n\n".join(i.text for i in items),
            cli_text="\n\n".join(i.cli_text for i in items),
            images=[b for i in items for b in i.images],
            image_meta=[m for i in items for m in i.image_meta],
            files=[f for i in items for f in i.files],
        )


def goal_payload_to_state(data: dict) -> dict | None:
    """GOAL_UPDATE payload → the chat-goal state dict persisted in
    ``chats.thread_goal`` and shipped to the GoalPanel; None = cleared.
    Shared by the pump handler and the out-of-band (between-turns) path so
    the two can never disagree on the stored shape."""
    if data.get("cleared"):
        return None
    return {k: data.get(k) for k in (
        "objective", "status", "token_budget", "tokens_used",
        "time_used_seconds")}
