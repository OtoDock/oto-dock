"""Whether a silent turn is alive.

A headless turn produces no stream line while its engine waits on a request
that stalled, and none while a long tool call runs, a person decides on a
card, or background work runs. The engine-side loops (the Claude session,
the Codex session, the remote adapters) ask here, on every silent slice,
whether the silence has passed its ceiling with nothing keeping the turn:
the answer ends the turn with the ``silent`` ending. The ceiling is
``TURN_SILENCE_S`` for a chat turn with no tool open, and ``CLAUDE_TIMEOUT``
while a tool is open or background work runs, and for a task run's turn
(a tool that never returns is the task watchdog's case).
"""
from __future__ import annotations

import time

import config


def ceiling_for(tools_open: bool, *, task: bool = False) -> float:
    """The silence a turn may keep before it ends: the turn ceiling while a
    tool call is open or for a task run's turn, the silence ceiling
    otherwise."""
    return float(config.CLAUDE_TIMEOUT if tools_open or task else config.TURN_SILENCE_S)


def spared(session_id: str, *, silent_for: float) -> str:
    """Why a turn silent for ``silent_for`` seconds is kept: a prompt waiting
    on a person, background work running for less than the turn ceiling, or
    hook activity younger than the silence ceiling. "" when nothing keeps
    it."""
    from core.session import background_leash
    from core.session.session_state import get_hook_activity, has_pending_prompt
    if has_pending_prompt(session_id):
        return "a prompt waiting on a person"
    work = background_leash.spare_reason(session_id, silent_for,
                                         ceiling_s=float(config.CLAUDE_TIMEOUT))
    if work:
        return work
    last_hook = get_hook_activity(session_id)
    if last_hook and time.monotonic() - last_hook < float(config.TURN_SILENCE_S):
        return "recent hook activity"
    return ""


def ends(session_id: str, *, tools_open: bool, silent_for: float, task: bool = False) -> bool:
    """Whether a turn silent for ``silent_for`` seconds ends now: past its
    ceiling with nothing keeping it."""
    if silent_for <= ceiling_for(tools_open, task=task):
        return False
    return not spared(session_id, silent_for=silent_for)
