"""Chat turns + streaming: user sends, resume/history replay, the producer/pump
plumbing, permission gates, attachments, and the mode/model/execution-
mode/implement-plan controls.

ChatController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.

The methods live in four sub-mixins, one file each, assembled here:

* ``dashboard_chat_send.py``    — the user's send, stop-and-send, live interactive
                                  delivery, artifact interactions, app actions, reads
* ``dashboard_chat_resume.py``  — resume_chat (history replay, restore, lazy warmup)
* ``dashboard_chat_stream.py``  — the producer/pump plumbing and background monitors
* ``dashboard_chat_support.py`` — titles, first prompt, cancelled context, attachments,
                                  permission resolution, the mode/model/execution
                                  controls, implement-plan

``dashboard_chat_text.py`` holds the constants and pure helpers they share. The
names below are re-exported for their existing importers.
"""

import config  # noqa: F401  (tests patch ``dashboard_chat.config.get_model_provider``)
from ws.dashboard_chat_text import (  # noqa: F401
    _STOP_AND_SEND_NOTE,
    _codex_provider_switch_blocker,
    _queued_outgoing,
)
from ws.dashboard_chat_send import ChatSendMixin
from ws.dashboard_chat_resume import ChatResumeMixin
from ws.dashboard_chat_stream import ChatStreamMixin
from ws.dashboard_chat_support import ChatSupportMixin


class ChatController(ChatSendMixin, ChatResumeMixin, ChatStreamMixin, ChatSupportMixin):
    """Chat turns + streaming: user sends, resume/history replay, the
    producer/pump plumbing, permission gates, attachments, and the
    mode/model/execution-mode/implement-plan controls."""
