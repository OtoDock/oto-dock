"""Phone server adapter.

Minimal adapter — phone (call) sessions have no display tools (excluded via
manifest exclude_from).

Call context prompts (LLM instructions for phone call behavior) are
centralized here so the phone server stays a pure audio pipeline.
"""

from pathlib import Path

from adapters.base import ClientAdapter


# A phone call's direction: a route's and a call log row's ``direction``, a
# phone session's ``call_type``. A missing direction is inbound.
INBOUND = "inbound"
OUTBOUND = "outbound"
DIRECTIONS = (INBOUND, OUTBOUND)


def is_outbound(direction: str | None) -> bool:
    """Whether a call direction is outbound; a missing one is inbound."""
    return (direction or INBOUND) == OUTBOUND


# Common call rules — injected into the system prompt for all phone sessions.
PHONE_CONTEXT = (
    "You are on a live phone call. This is voice — not chat.\n"
    "RULES:\n"
    "- ALWAYS respond in the SAME LANGUAGE the user speaks.\n"
    "- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n"
    "- Talk naturally like a real person on the phone — use casual, conversational language.\n"
    "- Avoid stiff/formal phrasing. Say things the way you'd say them out loud.\n"
    "- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n"
    "- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n"
    "- Don't spell out URLs, paths, or IPs unless asked.\n"
    "- Before ANY tool call, FIRST write one short spoken line saying what you're doing (e.g. 'Let me check that for you'). NEVER go silent into a tool — on a call, silence feels broken. During multi-step tool work, add a brief spoken progress line between steps, then summarize the result in 1-2 short sentences.\n"
    "- To end the call (e.g. user says goodbye or the conversation is clearly over): say your goodbye first, then call the end_call tool if you have it; without that tool, append [CALL_COMPLETE] at the end of your final message. Nothing you write after the tool call or the marker is heard.\n"
)

# Standalone call context for OUTBOUND calls — a full template (NOT appended to
# the inbound one): all the base call rules PLUS the task / [QUESTION:] rules.
PHONE_OUTBOUND = (
    "You are on a live phone call that YOU placed to complete a task. This is voice — not chat.\n"
    "RULES:\n"
    "- ALWAYS respond in the SAME LANGUAGE the other person speaks.\n"
    "- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n"
    "- Talk naturally like a real person on the phone — use casual, conversational language.\n"
    "- Avoid stiff/formal phrasing. Say things the way you'd say them out loud.\n"
    "- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n"
    "- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n"
    "- Don't spell out URLs, paths, or IPs unless asked.\n"
    "- Before ANY tool call, FIRST write one short spoken line saying what you're doing (e.g. 'Let me check that for you'). NEVER go silent into a tool — on a call, silence feels broken. During multi-step tool work, add a brief spoken progress line between steps, then summarize the result in 1-2 short sentences.\n"
    "- Complete the task you were given, politely and professionally.\n"
    "- If you need information from the agent that placed this call, call the ask_initiating_agent tool if you have it; without that tool, emit [QUESTION: your question here] in your response. "
    "The system relays the question while the call stays active.\n"
    "- When the task is complete or clearly cannot be completed: say your goodbye first, then call the end_call tool if you have it; without that tool, end your final message with [CALL_COMPLETE].\n"
    "- The [CALL_COMPLETE] and [QUESTION:] markers are stripped before speaking — they're signals for the system. Nothing you write after an end_call or a marker is heard.\n"
)

# The call tools block, appended after the call context when the session
# actually carries the tools (the call-control MCP attached, or the Direct
# builtins registered). The transfer targets are listed by label only.
_CALL_TOOLS_HEAD = (
    "CALL CONTROLS (tools):\n"
    "- end_call(reason): say your goodbye in one short sentence FIRST, then call it; "
    "the line closes right after. Nothing you write after the call is heard.\n"
)
_CALL_TOOLS_TRANSFER = (
    "- transfer_call(target): hands the caller to a colleague. Targets on this line: {targets}. "
    "Tell the caller in one short sentence first, then call it with the target's name.\n"
)
_CALL_TOOLS_NO_TRANSFER = (
    "- transfer_call: this line has no transfer targets; if asked, say you cannot transfer.\n"
)
_CALL_TOOLS_ASK = (
    "- ask_initiating_agent(question): relays a question to the agent that placed this call. "
    "Say that you are checking (one short sentence) and stop; the answer arrives as your next "
    "message within about 40 seconds.\n"
)
_CALL_TOOLS_TAIL = (
    "The text markers [CALL_COMPLETE] and [QUESTION: ...] still work as a fallback.\n"
)


def call_tools_block(*, ask_back: bool, transfer_targets: list[dict] | None) -> str:
    """The prompt block naming the call tools a session carries."""
    labels = [str(t.get("label") or t.get("number") or "").strip()
              for t in (transfer_targets or []) if isinstance(t, dict)]
    labels = [label for label in labels if label]
    block = _CALL_TOOLS_HEAD
    block += (_CALL_TOOLS_TRANSFER.format(targets=", ".join(labels))
              if labels else _CALL_TOOLS_NO_TRANSFER)
    if ask_back:
        block += _CALL_TOOLS_ASK
    return block + _CALL_TOOLS_TAIL


class PhoneAdapter(ClientAdapter):
    """Adapter for phone server sessions (WebSocket)."""

    # The kind's name: it selects this adapter (the registry keys on ``name``)
    # and must equal ``session_kind.PHONE.name`` — the one spelling every
    # builder passes (``core/session/session_kind.py``; the manifests'
    # ``exclude_from: ["phone"]`` is the same word).
    name = "phone"

    @staticmethod
    def get_phone_context(call_type: str = INBOUND) -> str:
        """Return the call-context system-prompt block for the call direction.

        Inbound and outbound are INDEPENDENT full templates (not base + extra):
        the outbound template carries all the base call rules plus the task /
        [QUESTION:] rules. Each falls back to its hardcoded default when the DB
        setting is empty (first run before seed). A per-route
        ``phone_context_override`` is appended on top by the config builder.
        """
        from storage.database import get_platform_setting

        if is_outbound(call_type):
            return get_platform_setting("phone_context_outbound") or PHONE_OUTBOUND
        return get_platform_setting("phone_context_inbound") or PHONE_CONTEXT

    def build_client_context(self, mcp_config_path: Path | None) -> str:
        # Phone sessions have no display tools and no extra client context.
        # Call-specific instructions are injected via get_phone_context()
        # during session warmup, not through the adapter pipeline.
        return ""

    async def handle_file_display(
        self,
        session_id: str,
        file_path: Path,
        filename: str,
        description: str,
        download_url: str = "",
    ) -> dict:
        # Phone calls can't display files — return a text-only fallback so the
        # agent at least knows the file exists.
        return {
            "event_type": "tool_result",
            "tool_name": "send_file",
            "summary": f"File available: {filename}",
        }
