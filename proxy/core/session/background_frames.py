"""What the lines read between turns tell about background work.

A headless Claude session keeps writing while its main agent is idle: a
background sub-agent's own lines, task frames of the commands it and the main
agent started, the CLI's snapshot of what still runs, its completions. Every
between-turns reader (the idle drains the chat monitors poll, the next turn's
stale drain, a steer's peek, the remote turn-end drain) hands each line to
``resolve_background_frame``, so a completion clears its badge however it is
read and a stop hook the proxy never received is healed from stdout. The
history frames ask ``running_background`` what a page rebuilt from rows
cannot know: which stored spawns still run.
"""

import logging

from core.session.session_state import (
    _push_to_chat,
    _subagent_registries,
    mark_subagent_done,
    reconcile_background_snapshot,
    resolve_bg_command_frame,
)
from ws import wire_events as wire

logger = logging.getLogger("otodock")


def _resolve_subagent_frame(session_id: str, data: dict) -> bool:
    """A tracked sub-agent's own completion read on stdout (its
    ``task_notification`` or terminal ``task_updated``): the backup for a
    stop hook that never arrived. Marks it done and clears its badge, the
    snapshot's way (the dashboard key is the spawning ``tool_use_id``)."""
    from core.layers.cli.translator import terminal_task_status
    reg = _subagent_registries.get(session_id)
    task_id = data.get("task_id", "")
    if reg is None or not task_id or terminal_task_status(data) is None:
        return False
    if not reg.mark_done(task_id):
        return False
    tuid = reg.tuid_for(task_id) or data.get("tool_use_id", "")
    if reg.chat_id and tuid:
        mark_subagent_done(reg.chat_id, tuid)
        _push_to_chat(reg.chat_id, {"type": wire.BG_AGENT_DONE, "tool_use_id": tuid})
    return True


def _note_background_alive(session_id: str, data: dict) -> None:
    """Stamp the pending agent a line belongs to: its own lines carry its
    spawn's ``parent_tool_use_id``, its task frames its id, its commands'
    frames a ``parent_task_id``."""
    reg = _subagent_registries.get(session_id)
    if reg is None or not reg.has_pending:
        return
    parent_tuid = data.get("parent_tool_use_id")
    if parent_tuid:
        reg.note_alive(reg.task_for_tuid(parent_tuid))
        return
    reg.note_alive(data.get("task_id", ""))
    reg.note_alive(data.get("parent_task_id", ""))


def resolve_background_frame(session_id: str, data: dict) -> str:
    """The one body for a line read between turns (the idle drains, the
    stale drains, a steer's peek, the remote turn-end drain): resolves the
    background commands and sub-agents it ends (badge clears and the
    registries), applies a snapshot, stamps the agent it belongs to, and
    says whose the line was (``FRAME_*``) for the readers' census."""
    # The CLI's frame vocabulary is the translator's; imported here, since
    # the CLI package's own modules import this one.
    from core.layers.cli.translator import FRAME_BACKGROUND, frame_kind
    kind = frame_kind(data)
    if kind != FRAME_BACKGROUND:
        return kind
    if data.get("parent_tool_use_id"):
        _note_background_alive(session_id, data)
    elif data.get("subtype") == "background_tasks_changed":
        reconcile_background_snapshot(session_id, data)
    else:
        _note_background_alive(session_id, data)
        resolve_bg_command_frame(session_id, data)
        _resolve_subagent_frame(session_id, data)
    return kind


def read_stale_line(session_id: str, data: dict) -> str:
    """One line a stale drain read before a turn: resolved, handed to the
    engine's own requests (``engine_prompts.note_frame``: a sub-agent's ask),
    and named for the census: ``background``, ``neutral`` (nobody's answer:
    a rate limit, the control channel, a status) or the line's own type (the
    main agent's output nobody read)."""
    from core.layers.cli.translator import FRAME_MAIN
    from core.session import engine_prompts
    kind = resolve_background_frame(session_id, data)
    engine_prompts.note_frame(session_id, data)
    return str(data.get("type") or "?") if kind == FRAME_MAIN else kind


def resolve_background_work(session_id: str, data: dict) -> bool:
    """``resolve_background_frame`` for a reader that counts progress: True
    when the line ended a background command or sub-agent."""
    from core.events.bg_command_state import peek_bg_command_registry
    regs = [r for r in (peek_bg_command_registry(session_id),
                        _subagent_registries.get(session_id)) if r is not None]
    before = [r.pending_count for r in regs]
    resolve_background_frame(session_id, data)
    return [r.pending_count for r in regs] != before


def log_between_turns_census(who: str, drained) -> None:
    """A between-turns drain's type census (never payloads: a frame can carry
    a file). A sub-agent's lines and task frames are its work going on while
    the main agent was idle (``background``); anything else read there is the
    main agent's own output nobody read, which is worth a warning."""
    counts = {k: v for k, v in drained.items() if v}
    if not counts:
        return
    background = counts.get("background", 0)
    rest = sum(v for k, v in counts.items() if k not in ("background", "neutral", "wake_captured"))
    if rest:
        logger.warning(
            "%s: drained %s stale lines from interrupted response (types=%s, results=%s)",
            who, sum(counts.values()), counts, counts.get("result", 0),
        )
    elif background:
        logger.info("%s: read %s lines of background work between turns (types=%s)",
                    who, background, counts)


def running_background(chat_ids) -> dict[str, list[dict]]:
    """The background sub-agents and commands still running for these
    chats, by dashboard id (the spawning ``tool_use_id``): what a page
    rebuilt from rows cannot know (every stored spawn reads finished).
    Background spawns only: a foreground agent ends inside its turn, and a
    terminal's registry is its tailer's, whose ends send no done frame.
    Empty lists when nothing runs."""
    from core.events.bg_command_state import _bg_command_registries
    from core.session import interactive_session
    wanted = {c for c in chat_ids if c}
    agents: list[dict] = []
    commands: list[dict] = []
    for session_id, reg in list(_subagent_registries.items()):
        if reg.chat_id not in wanted or interactive_session.get(session_id) is not None:
            continue
        for task_id in sorted((reg.spawned - reg.completed) & reg.background):
            tuid = reg.tuid_for(task_id)
            if tuid:
                agents.append({"id": tuid, "description": reg.description_for(task_id)})
    for bgreg in list(_bg_command_registries.values()):
        if bgreg.chat_id not in wanted:
            continue
        for task_id in sorted(bgreg.spawned - bgreg.completed):
            tuid = bgreg.tuid_for(task_id)
            if tuid:
                commands.append({"id": tuid, "description": bgreg.description_for(task_id)})
    return {"agents": agents, "commands": commands}
