"""A session with tracked background work is not idle.

The Bash tool's ``run_in_background`` and a background subagent outlive the
turn that spawned them, and their only completion signals are a stdout
frame (commands) and a Stop hook (subagents). The idle reapers and the RAM
evictor read the two registries through here so a running job keeps its
session, up to ``BACKGROUND_WORK_CEILING_S``: past the ceiling the session
goes anyway, because a job that never ends must not pin a slot forever.
Reads never create a registry entry.
"""

import config


def pending_background(session_id: str) -> tuple[int, int]:
    """``(background commands, background subagents)`` still running."""
    from core.events.bg_command_state import peek_bg_command_registry
    from core.session.session_state import peek_subagent_registry
    cmds = peek_bg_command_registry(session_id)
    subs = peek_subagent_registry(session_id)
    return (
        cmds.pending_count if cmds is not None else 0,
        subs.pending_count if subs is not None else 0,
    )


def background_pending_count(session_id: str) -> int:
    cmds, subs = pending_background(session_id)
    return cmds + subs


def background_work_ceiling() -> float:
    return float(config.BACKGROUND_WORK_CEILING_S)


def pending_summary(session_id: str) -> str:
    """"2 background command(s) + 1 background subagent(s)", or "" when
    nothing is running."""
    cmds, subs = pending_background(session_id)
    parts = []
    if cmds:
        parts.append(f"{cmds} background command(s)")
    if subs:
        parts.append(f"{subs} background subagent(s)")
    return " + ".join(parts)


def spare_reason(session_id: str, idle_s: float, *, ceiling_s: float | None = None) -> str:
    """Why an over-idle session must be kept: the running work it holds, or
    "" when nothing runs or the ceiling has passed (reap it)."""
    limit = background_work_ceiling() if ceiling_s is None else ceiling_s
    if idle_s > limit:
        return ""
    return pending_summary(session_id)
