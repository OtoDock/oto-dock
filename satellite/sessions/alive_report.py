"""What the satellite reports about its headless sessions at every connect.

The ``sessions_alive`` frame carries two lists (both keys FROZEN wire):

- ``sessions``: the live sessions of the engines whose in-flight turn a
  restarted platform re-adopts (``Engine.turn_replay``), with their turn
  state (``SessionManager.headless_sessions_alive``);
- ``other_sessions``: every other headless session the satellite holds — an
  engine without turn replay, alive or not (Codex revives a dead daemon on
  its next turn), and a dead turn-replay CLI — so the platform takes an idle
  one back or closes it, and none stays counted against the machine's
  capacity after a platform restart.

Each entry names its ``incarnation``: the object that holds the id now, a
string of this boot's nonce and a counter. A close that carries one ends only
that object, so a close decided on a report never ends a session a later
start put under the same id (or one from before a satellite restart).
"""

from __future__ import annotations

import itertools
import logging
import secrets
from typing import TYPE_CHECKING

from ..engines import ENGINES

if TYPE_CHECKING:
    from .session_manager import SessionManager

logger = logging.getLogger("satellite")

_BOOT = secrets.token_hex(4)
_COUNTER = itertools.count(1)


def next_incarnation() -> str:
    return f"{_BOOT}-{next(_COUNTER)}"


def other_sessions_held(sm: "SessionManager") -> list[dict]:
    out = []
    for sid, session in list(sm.sessions.items()):
        engine = ENGINES.get(getattr(session, "execution_path", ""))
        alive = bool(session.is_alive)
        if engine is not None and engine.turn_replay and alive:
            continue   # the turn-replay list names it
        config = getattr(session, "config", None)
        config = config if isinstance(config, dict) else {}
        if engine is not None and engine.turn_replay:
            turn_active = bool((sm.turn_state.get(sid) or {}).get("active"))
        else:
            turn_active = bool(getattr(session, "turn_active", False))
        out.append({
            "session_id": sid,
            "execution_path": getattr(session, "execution_path", ""),
            "agent_slug": getattr(session, "agent_slug", ""),
            "alive": alive,
            "turn_active": turn_active,
            "incarnation": getattr(session, "incarnation", "") or "",
            "resume_handle": getattr(session, "resume_handle", "") or "",
            "model": config.get("model", "") or "",
            "mcp_servers": list(getattr(session, "mcp_server_names", None) or []),
            "use_native_permissions": bool(config.get("use_native_permissions", False)),
        })
    return out


def sessions_alive_frame(sm: "SessionManager") -> dict | None:
    """The frame, or None when the turn-replay list cannot be built (no
    frame, as an older satellite sends on that failure). A failure of the
    other list costs only that key, never the turn-replay report."""
    try:
        frame = {"type": "sessions_alive", "sessions": sm.headless_sessions_alive()}
    except Exception:
        logger.warning("sessions_alive: the turn-replay list failed", exc_info=True)
        return None
    try:
        frame["other_sessions"] = other_sessions_held(sm)
    except Exception:
        logger.warning("sessions_alive: the other sessions' list failed", exc_info=True)
    return frame
