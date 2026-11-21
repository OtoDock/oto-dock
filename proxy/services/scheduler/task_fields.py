"""The fields a template or a blueprint may set on a task (its permission
mode and timeout), judged for the agent that takes it: one rule for the
task routes, the installer and the blueprint seed.

``session_manager`` is imported inside the function: this package loads in
the standalone scheduler, which never builds a session.
"""

from __future__ import annotations

from core.permission_modes import AUTO_REVIEW_MODE

MIN_TIMEOUT_S = 60
MAX_TIMEOUT_S = 7200
DEFAULT_TIMEOUT_S = 600


def mode_problem(agent: str, layer: str | None, mode: str | None) -> str:
    """Why ``agent`` cannot run a task in ``mode`` on ``layer`` (its default
    engine when none is pinned): Auto needs an engine with a reviewer. ''
    when it can, or when the mode is not Auto."""
    if mode != AUTO_REVIEW_MODE:
        return ""
    from core.session.session_manager import (
        get_layer_capabilities,
        resolve_execution_path,
    )
    path = layer or resolve_execution_path(agent)
    caps = get_layer_capabilities(path)
    if caps is None or AUTO_REVIEW_MODE not in caps.permission_modes:
        return (f"Auto needs an engine with its own review; {path} has none. "
                "Use Don't Ask for this task, or pin an engine that offers Auto.")
    return ""


def clean_timeout(raw: object) -> int | None:
    """A task's timeout in seconds, ``None`` when it is not a whole number
    within the bounds; an absent one is the default."""
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT_S
    if isinstance(raw, bool) or not isinstance(raw, int) or not MIN_TIMEOUT_S <= raw <= MAX_TIMEOUT_S:
        return None
    return raw
