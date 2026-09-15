"""Constants and pure helpers shared by the chat mixins (ws/dashboard_chat_*.py).

A leaf: imports nothing from ``ws.*`` so the sub-mixins can import it without
adding a second import cycle to the one ``ws/dashboard.py`` already resolves by
statement order.
"""

import re

import config

# Injected `[Current time: ...]` stamp line(s) — twin of
# ``transcript_tailer._TIME_PRELUDE_RE`` (start-anchored exact shape only).
_TIME_PRELUDE_RE = re.compile(r"^\[Current time: [^\]\n]{1,160}\][ \t]*(?:\r?\n+|$)")

# Bounded wait on an in-flight warmup during the dead-session re-check
# (single-flight revival). Covers a normal respawn (~2-10s) with slack for a
# satellite resume; past it the send falls back to synthesizing its own warmup
# rather than hanging the turn behind a stuck 90s MCP install.
_REVIVAL_WAIT_S = 30.0

# A mini-app action's framed prompt header (``ws/artifact_interactions.
# frame_text`` — title/label have had '"' replaced with "'"). Recognized by
# the title chokepoints so an action-started chat names as "App — Label",
# never the raw framing brackets. Twin in ``transcript_tailer``.
_APP_ACTION_HEADER_RE = re.compile(
    r'^\[action from mini-app "(.{1,200}?)" — (.{1,80}?)\]'
)

# Stop-and-send: rides the ENGINE prompt of the interrupted-then-drained
# queued message only — the QUEUE_TURN event / DB user row keep the raw text
# (mirrors duplex ``_build_prompt``'s prompt-side-only interruption note).
_STOP_AND_SEND_NOTE = (
    "[The user sent the message below while you were still working — the "
    "previous turn was interrupted mid-step so you can respond now. "
    "Completed work and running background tasks are unaffected.]"
)


_PROVIDER_WORDS = {
    "openai": "OpenAI",
    "ollama": "the Ollama endpoint",
    "openai_compatible": "the local OpenAI-compatible endpoint",
}


def _codex_provider_switch_blocker(session_id: str, new_model: str) -> str:
    """Non-empty user-facing reason when ``new_model`` belongs to a different
    provider than the subscription the live Codex session is bound to. Codex
    fixes ``model_provider`` in config.toml at session start, so the switch
    cannot be applied to a running session; empty when nothing is bound
    (pool-external credentials) or the provider is the same."""
    from services.engines import subscription_pool
    from storage.billing import subscription_store
    sub_id = subscription_pool.get_session_subscription(session_id)
    if not sub_id or sub_id == "default":
        return ""
    bound = (subscription_store.get_subscription(sub_id) or {}).get("provider") or ""
    wanted = config.get_model_provider(new_model, layer="codex-cli")
    if not bound or bound == wanted:
        return ""
    return (
        "Codex keeps its model provider for the life of a chat: this chat runs on "
        f"{_PROVIDER_WORDS.get(bound, bound)} and {new_model} needs "
        f"{_PROVIDER_WORDS.get(wanted, wanted)}. Start a new chat to use it."
    )


def _queued_outgoing(stop_flags: dict, combined: str) -> str:
    """Engine text for a drained queue batch; consumes the stop-and-send
    flags. When a graceful interrupt landed for this boundary the note is
    prepended to the ENGINE prompt only — the caller's QUEUE_TURN event
    (and thus the DB user row) keeps ``combined`` raw."""
    outgoing = combined
    if stop_flags["note"]:
        outgoing = _STOP_AND_SEND_NOTE + "\n\n" + combined
    stop_flags["note"] = False
    stop_flags["fired"] = False
    return outgoing
