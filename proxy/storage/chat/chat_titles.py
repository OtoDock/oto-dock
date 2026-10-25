"""The deterministic chat title: the first words of a first prompt, its
injected preludes dropped. The task store stamps a scheduler-driven task
chat with it; the title generator strips the same preludes before it asks
a model.
"""

import re

# The dashboard injects a "[Current time: …]" prelude (and the viewer focus
# line, and a task run the parallel-activity line) ahead of interactive
# sends — strip them or they become the title (twin of
# ws/dashboard_chat_text.py's send-time recognizer; both must keep matching
# the injected shapes).
_TIME_PRELUDE_RE = re.compile(
    r"^\[(?:(?:Current time: |The user is looking at the app )[^\]\n]{1,200}"
    r"|Parallel activity — [^\n]{0,2000}?\(peek_session / get_task_result\)\."
    r'|Meeting "[^\n]{0,4000}? you have only this note\.)\]'
    r"(?:[ \t]*(?:\r?\n+|$)|[ \t]+)"
)


def _without_preludes(text: str) -> str:
    """``text`` with its leading injected prelude lines dropped."""
    stripped = text or ""
    while True:  # stacked preludes (the time stamp plus the focus line)
        once = _TIME_PRELUDE_RE.sub("", stripped, count=1)
        if once == stripped:
            return stripped
        stripped = once


def deterministic_title(text: str) -> str:
    """Stable chat title from a first user message — first ~6 words / 48 chars,
    whitespace-collapsed, ellipsis if truncated. The same rule the chat layer
    applies at send time (``ws/dashboard_chat_support.py::_deterministic_title``);
    exposed here so the storage layer can stamp scheduler-driven task chats
    without importing the WS controller."""
    cleaned = " ".join(_without_preludes(text).split())
    if not cleaned:
        return "New Chat"
    words = cleaned.split(" ")
    title = " ".join(words[:6])
    cut = len(words) > 6
    if len(title) > 48:
        title = title[:48].rstrip()
        cut = True
    return title + ("…" if cut else "")
