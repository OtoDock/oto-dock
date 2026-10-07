"""Constants and pure helpers shared by the chat mixins (ws/dashboard_chat_*.py).

A leaf: imports nothing from ``ws.*`` so the sub-mixins can import it without
adding a second import cycle to the one ``ws/dashboard.py`` already resolves by
statement order.
"""

import re

import config

# Injected prelude line(s): the `[Current time: ...]` stamp and the viewer
# focus line — twin of ``transcript_tailer._TIME_PRELUDE_RE`` (start-anchored
# exact shapes only).
_TIME_PRELUDE_RE = re.compile(
    r"^\[(?:Current time: |The user is looking at the app )[^\]\n]{1,200}\][ \t]*(?:\r?\n+|$)"
)


def interactive_prelude(text: str, *, user_tz: str | None, focus_line: str = "") -> str:
    """The ONE shape every prompt typed into a live TUI carries: the time
    stamp (the `-p` pump's per-turn injection, ``config.format_current_time``),
    then the viewer-focus line when an app is on the user's screen, each on
    its own paragraph, then the text. Built here — beside the regex that
    recognises it — for the cold first prompt on both delivery paths (the
    launch argv and the PTY submit) and the live composer send, so the tailer's
    duplicate-skip, the title strip and the dashboard's display matchers
    meet one shape. A line the dashboard types straight into the PTY carries
    the browser's own stamp (``withInteractiveTime``), the same shape."""
    focus = f"{focus_line}\n\n" if focus_line else ""
    return f"[Current time: {config.format_current_time(user_tz)}]\n\n{focus}{text}"

# Bounded wait on an in-flight warmup during the dead-session re-check
# (single-flight revival). Covers a normal respawn (~2-10s) with slack for a
# satellite resume; past it the send falls back to synthesizing its own warmup
# rather than hanging the turn behind a stuck 90s MCP install.
_REVIVAL_WAIT_S = 30.0

# An app action's framed prompt header (``ws/artifact_interactions.
# frame_text`` — title/label have had '"' replaced with "'"). Recognized by
# the title chokepoints so an action-started chat names as "App — Label",
# never the raw framing brackets. Twin in ``transcript_tailer``. The older
# "mini-app" spelling still parses: transcripts written before the rename
# replay through the same chokepoint.
_APP_ACTION_HEADER_RE = re.compile(
    r'^\[action from (?:mini-)?app "(.{1,200}?)" — (.{1,80}?)\]'
)

# Stop-and-send: rides the ENGINE prompt of the interrupted-then-drained
# queued message only — the QUEUE_TURN event / DB user row keep the raw text
# (mirrors duplex ``_build_prompt``'s prompt-side-only interruption note).
_STOP_AND_SEND_NOTE = (
    "[The user sent the message below while you were still working — the "
    "previous turn was interrupted mid-step so you can respond now. "
    "Completed work and running background tasks are unaffected.]"
)


def _provider_switch_blocker(session_id: str, new_model: str, *, layer: str) -> str:
    """Non-empty user-facing reason when ``new_model`` belongs to a different
    provider than the subscription the live session is bound to, on an engine
    that pins its provider at spawn (``behaviour.provider_pinned_per_session``
    — Codex fixes ``model_provider`` in config.toml), so the switch cannot be
    applied to a running session; empty when nothing is bound (pool-external
    credentials) or the provider is the same."""
    from services.engines import subscription_pool
    from storage.billing import subscription_store
    sub_id = subscription_pool.get_session_subscription(session_id)
    if not sub_id or sub_id == "default":
        return ""
    bound = (subscription_store.get_subscription(sub_id) or {}).get("provider") or ""
    wanted = config.get_model_provider(new_model, layer=layer)
    if not bound or bound == wanted:
        return ""
    from core.execution_layer import provider_entry
    from core.session.session_manager import get_layer_capabilities
    _c = get_layer_capabilities(layer)
    engine = _c.display_name if _c else layer

    def _label(provider: str) -> str:
        entry = provider_entry(_c, provider) if _c is not None else None
        return entry["label"] if entry else provider
    return (
        f"{engine} keeps its model provider for the life of a chat: this chat runs on "
        f"{_label(bound)} and {new_model} needs "
        f"{_label(wanted)}. Start a new chat to use it."
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


# What ``_heal_viewed_session`` answers: the session is ready for the turn; the
# send was answered there (typed into a live terminal, refused read-only); or
# the session's machine is still reconnecting after the wait (nothing was
# touched, the send waits).
HEAL_READY = "ready"
HEAL_ANSWERED = "answered"
HEAL_RECONNECTING = "reconnecting"

# The line a turn gets that could not wait in the queue (its row is already
# written) while its chat's machine reconnects.
RECONNECTING_NOT_SENT = ("Not sent: the machine running this chat is reconnecting. "
                         "Send it again once it is back.")


class TurnDeferred(Exception):
    """A turn's session is still held in its machine's reconnect grace once
    the wait is over: the start is put off and nothing of the session is
    touched. Every caller of ``_start_new_stream`` catches it, except
    ``_run_server_turn``, which lets it reach its own callers."""


def grace_layer(session_id: str, fallback):
    """The layer to ask whether ``session_id``'s machine is reconnecting: the
    one that holds the session (a chat's resolved layer can be another, e.g.
    the local one for a chat with no pinned target), else ``fallback``."""
    from core.session.session_manager import find_layer_for_session
    return find_layer_for_session(session_id) or fallback
