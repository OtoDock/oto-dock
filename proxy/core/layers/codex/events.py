"""What a Codex app-server stream hands the translator: the event record
and the item types that are a turn's work.
"""

from __future__ import annotations

from dataclasses import dataclass

# The item types that are the turn's life while the daemon streams nothing
# (a tool, a command, a patch, a search, a delegated agent): an open message
# or reasoning item is a stalled stream, not work.
_TOOL_ITEM_TYPES = frozenset({
    "commandExecution", "fileChange", "mcpToolCall", "webSearch",
    "collabAgentToolCall", "dynamicToolCall",
})


@dataclass
class CodexEvent:
    """One inbound app-server message routed to the translator.

    ``type`` is the JSON-RPC notification *method* (e.g. ``item/agentMessage/
    delta``, ``turn/completed``); ``data`` is its ``params``. (Under the old
    ``codex exec`` model this carried the JSONL event ``type`` + object — the
    translator was rewritten to key on the method instead.)
    """
    type: str
    data: dict
