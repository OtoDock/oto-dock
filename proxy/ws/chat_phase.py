"""The chat's liveness vocabulary — the sidebar live-dot, the stop button
and the Active-now widget (core-seams phase 8).

Two spoken sets, every spelling frozen. The ``chat_status`` frame carries
``streaming`` (a turn opened: the pump's turn start, an interactive
turn-open transition) or ``ready`` (it closed: the pump's end of turn, an
interactive close / park / compaction / death) and nothing else —
``notification_manager.broadcast_chat_status`` refuses another word. The
``GET /v1/chats/active`` rows say ``streaming`` (an open turn right now),
``warming`` (a session registered as warming with no turn yet) or
``finished`` (a recent response nobody opened). The dashboard mirror
(``lib/status/chat.ts``) folds the client's own ``idle`` and ``failed``
and the widget's derived ``finished`` into ONE union; the lane vocabulary
a project board speaks (``generating | awaiting_user | idle``) lives in
``services/delegation/lane_status.py``.

Stdlib only — a leaf, used module-qualified (``from ws import chat_phase``).
"""

from __future__ import annotations

STREAMING = "streaming"
READY = "ready"
WARMING = "warming"
FINISHED = "finished"

#: What the ``chat_status`` frame carries.
WIRE_PHASES: frozenset[str] = frozenset({STREAMING, READY})

#: What a ``GET /v1/chats/active`` row carries.
ACTIVE_ROW_PHASES: frozenset[str] = frozenset({STREAMING, WARMING, FINISHED})
