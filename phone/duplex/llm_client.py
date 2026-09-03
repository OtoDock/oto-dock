"""LLM backend for a duplex session — utterances over the dial-back socket.

Implements the surface the pipeline's LLM streaming expects from
``proxy/client.py`` (``send_message`` yielding text chunks +
``TOOL_USE_SIGNAL``, spoken-chars barge-in bookkeeping, a local ``messages``
mirror), but the frames ride the SAME dial-back socket as the audio — the
proxy's attach layer owns the actual chat session and its per-layer turn
mechanics.

``supports_abort`` is True with INTERRUPT semantics
(``abort_erases_turn=False``): ``abort_turn`` sends an ``abort_turn``
frame on the dial-back socket and the proxy gracefully stops the in-flight
turn per layer (headless graceful-stop without killing the process;
interactive → ESC) — the user row and partial reply STAY in history and
the attach layer stamps an interruption note on the next prompt, so the
pipeline must NOT refold the aborted transcript into the next dispatch
(that is the direct-layer erase model). The NEXT utterance still carries
``barge_in_chars`` for spoken-portion fidelity.
"""

from __future__ import annotations

import logging

from proxy.client import TOOL_USE_SIGNAL

logger = logging.getLogger("duplex")


class DuplexLlmClient:
    """ProxyClient-shaped backend bound to a DuplexEngineConnection."""

    llm_mode = "duplex"
    supports_abort = True
    # Interrupt semantics: the aborted turn stays in the chat history (the
    # proxy notes the interruption) — the pipeline dispatches only NEW speech.
    abort_erases_turn = False

    def __init__(self, conn):
        self._conn = conn
        self.session_id: str | None = None
        self.messages: list[dict] = []
        self._turn_seq = 0
        self._last_spoken_chars = 0
        self._barge_in_pending = False
        # True while a send_message turn is open (no done/error/close seen).
        # _cancel_tts gates its upstream abort on this: a barge-in commit
        # landing after the engine finished must not fire a session-scoped
        # abort that could kill an unrelated queued turn.
        self.turn_in_flight = False

    # -- barge-in bookkeeping (same contract as ProxyClient) -----------------

    def mark_spoken(self, char_count: int) -> None:
        self._last_spoken_chars = char_count

    def annotate_interrupted_response(self) -> None:
        if self._last_spoken_chars > 0:
            self._barge_in_pending = True

    # -- turns ----------------------------------------------------------------

    async def send_message(self, text: str):
        """One utterance → one attached chat turn; yields streamed text."""
        barge_in_chars = None
        if self._barge_in_pending and self._last_spoken_chars > 0:
            barge_in_chars = self._last_spoken_chars
            self._barge_in_pending = False
            self._last_spoken_chars = 0

        self._turn_seq += 1
        turn = self._turn_seq
        queue = self._conn.new_turn(turn)
        self._conn.send_event({
            "type": "utterance",
            "text": text,
            "turn": turn,
            "barge_in_chars": barge_in_chars,
        })
        self.messages.append({"role": "user", "content": text})

        full_response: list[str] = []
        first_text_logged = False
        self.turn_in_flight = True
        while True:
            msg = await queue.get()
            mtype = msg.get("type", "")
            if not first_text_logged and mtype == "text":
                first_text_logged = True
                logger.info(
                    f"[{self._conn.peer_addr}] turn {turn}: first text frame"
                )
            elif mtype in ("tool_start", "tool_end"):
                logger.info(
                    f"[{self._conn.peer_addr}] turn {turn}: {mtype} frame"
                )
            if mtype == "_closed":
                logger.info(f"[{self._conn.peer_addr}] socket closed mid-turn")
                self.turn_in_flight = False
                break
            if mtype == "text":
                content = msg.get("data", {}).get("content", "")
                if content:
                    full_response.append(content)
                    yield content
            elif mtype in ("tool_start", "tool_end"):
                # EITHER boundary finalizes the current TTS segment: Codex
                # reports tool calls only on completion and interactive row
                # feeds land post-hoc, so tool_end may be the only boundary a
                # layer ever sends (live-test forensics: the weather turn
                # relayed three tool_ends and zero tool_starts, and the
                # pre-tool sentence sat unfinalized in the TTS context until
                # the turn ended). Repeats are safe: an empty text buffer
                # no-ops downstream.
                yield TOOL_USE_SIGNAL
            elif mtype == "session":
                self.session_id = msg.get("data", {}).get("session_id")
            elif mtype == "done":
                self.turn_in_flight = False
                break
            elif mtype == "error":
                reason = msg.get("data", {}).get("message", "unknown")
                logger.warning(
                    f"[{self._conn.peer_addr}] turn error: {reason}"
                )
                self.turn_in_flight = False
                yield "Sorry, I hit an error handling that."
                break

        self.turn_in_flight = False
        self._last_spoken_chars = 0
        self.messages.append(
            {"role": "assistant", "content": "".join(full_response)})

    async def abort_turn(self) -> None:
        """Gracefully stop the in-flight turn proxy-side (barge-in).

        Fire-and-forget frame: the proxy aborts per layer and the turn's
        ``done`` arrives on the per-turn queue as generation unwinds —
        which also unblocks a token loop parked on an empty queue (e.g. a
        tool running upstream). Safe to call more than once and after the
        turn finished: the proxy no-ops on a stale or absent turn."""
        if self._turn_seq <= 0:
            return
        self._conn.send_event({
            "type": "abort_turn", "turn": self._turn_seq,
        })

    async def close(self) -> None:
        return
