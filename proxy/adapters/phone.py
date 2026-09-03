"""Phone server adapter.

Minimal adapter — phone (call) sessions have no display tools (excluded via
manifest exclude_from).

Call context prompts (LLM instructions for phone call behavior) are
centralized here so the phone server stays a pure audio pipeline.
"""

from pathlib import Path

from adapters.base import ClientAdapter


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
    "- To end the call (e.g. user says goodbye or the conversation is clearly over), append [CALL_COMPLETE] at the end of your final message. The system will strip it before speaking.\n"
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
    "- If you need information from your manager during the call, emit [QUESTION: your question here] in your response. "
    "The system will relay the question while the call stays active.\n"
    "- When the task is complete or clearly cannot be completed, end your final message with [CALL_COMPLETE].\n"
    "- The [CALL_COMPLETE] and [QUESTION:] markers are stripped before speaking — they're signals for the system.\n"
)


class PhoneAdapter(ClientAdapter):
    """Adapter for phone server sessions (WebSocket)."""

    # The client-type discriminator: it selects this adapter (registry keys on
    # ``name``) and is the token matched by ``client_type=="phone"`` /
    # ``source_type`` / the manifests' ``exclude_from: ["phone"]``. Must stay in
    # lockstep with the producer in core/config/phone_config_builder.py.
    name = "phone"

    @staticmethod
    def get_phone_context(call_type: str = "inbound") -> str:
        """Return the call-context system-prompt block for the call direction.

        Inbound and outbound are INDEPENDENT full templates (not base + extra):
        the outbound template carries all the base call rules plus the task /
        [QUESTION:] rules. Each falls back to its hardcoded default when the DB
        setting is empty (first run before seed). A per-route
        ``phone_context_override`` is appended on top by the config builder.
        """
        from storage.database import get_platform_setting

        if call_type == "outbound":
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
