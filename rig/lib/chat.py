"""A chat as the dashboard sees it: one socket, its frames, the chat's rows.

The waits keep reading the socket, so a held turn's pings are answered. A
headless turn has ended at a ``done`` frame with its chat id, which means its
rows are written. ``turn_complete`` goes only to a connection not viewing the
chat, so nothing waits on it. A terminal's rows arrive on a tailer tick: its
wait polls the rows.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING

from .websocket import WebSocketClosed

if TYPE_CHECKING:
    from .person import Person

# Frames that end a wait with a failure.
FAILURE_FRAMES = ("warmup_failed", "error", "limit_reached")
# A permission card's frame, and the kind a live state's held prompt carries for it.
CARD = "permission_prompt"


class TurnFailed(Exception):
    def __init__(self, frame: dict):
        super().__init__(json.dumps(frame)[:400])
        self.frame = frame


def script(*steps: dict, tag: str = "", after_cut: list[dict] | None = None) -> str:
    """The marker that makes the scripted model follow ``steps``. ``tag`` is written
    with each of its calls in the model server's log, so a scenario can wait for one.
    ``after_cut``: what it does once the platform has told it its turn was cut."""
    return "[[rig " + json.dumps({"steps": list(steps), **({"tag": tag} if tag else {}),
                                  **({"after_cut": after_cut} if after_cut else {})}) + "]]"


def tool_said(row: dict, what: str = "the call") -> str:
    """A tool row's words for a failure: its answer first, then whether it was marked
    an error, its input last and short, so a cut message never loses the reason."""
    answer = str(row.get("tool_result") or "").strip()
    head = f"{what} answered {answer[:600]!r}" if answer else f"{what} left no answer in the chat"
    return f"{head} (an error: {bool(row.get('is_error'))}, its input {json.dumps(row.get('tool_input'))[:200]})"


def tool_rows(rows: list[dict], name: str) -> list[dict]:
    """The tool rows of a tool, by the end of its name: what it ran and answered."""
    out = []
    for row in rows:
        if row.get("event_type") != "tool":
            continue
        data = row.get("event_data")
        data = json.loads(data) if isinstance(data, str) else (data or {})
        if str(data.get("name", "")).endswith(name):
            out.append(data)
    return out


def assistant_text(rows: list[dict]) -> str:
    return "\n".join(r.get("content") or "" for r in rows if r.get("role") == "assistant")


class ChatSession:
    def __init__(self, person: Person, *, agent: str):
        self.person = person
        self.agent = agent
        self.chat_id = ""
        self.session_id = ""
        self.frames: list[dict] = []
        self.ws = None

    def open(self, *, engine: str = "", model: str = "", text: str = "", mode: str = "dontAsk",
             terminal: bool = False, target: str = "", chat_id: str = "", timeout: float = 240.0) -> dict:
        self.ws = self.person.socket()
        msg: dict = {"type": "warmup", "agent": self.agent, "permission_mode": mode, "model": model,
                     "execution_path": engine, "execution_mode": "interactive" if terminal else "headless",
                     "theme": "dark"}
        if chat_id:
            msg["chat_id"] = chat_id
            self.chat_id = chat_id
        if target:
            msg["target"] = target
        if text:
            msg["text"] = text
            msg["queue_id"] = uuid.uuid4().hex
        self.ws.send_json(msg)
        ready = self.wait_for("warmup_ready", timeout=timeout)
        self.chat_id = ready.get("chat_id") or self.chat_id
        self.session_id = ready.get("session_id") or self.session_id
        return ready

    def pre_warm(self, *, engine: str = "", model: str = "", mode: str = "dontAsk") -> dict:
        """A new chat page's pre-warm, as the dashboard asks it before the first word."""
        self.ws = self.person.socket()
        self.ws.send_json({"type": "pre_warmup", "agent": self.agent, "permission_mode": mode,
                           "model": model, "execution_path": engine, "theme": "dark"})
        return self.wait_for("pre_warmup_ready", timeout=240)

    def send(self, text: str, *, queue_only: bool = False) -> None:
        msg = {"type": "chat", "text": text, "chat_id": self.chat_id, "queue_id": uuid.uuid4().hex}
        if queue_only:
            msg["queue_only"] = True
        self.ws.send_json(msg)

    def next_frame(self, timeout: float = 5.0) -> dict | None:
        frame = self.ws.recv_json(timeout=timeout)
        if frame is not None:
            self.frames.append(frame)
            if not self.chat_id and frame.get("chat_id"):
                self.chat_id = frame["chat_id"]
        return frame

    def wait_for(self, kind: str, *, timeout: float = 180.0, where=None) -> dict:
        """The next frame of ``kind`` for this chat. A failure frame raises."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.next_frame(timeout=min(5.0, max(0.1, deadline - time.monotonic())))
            if frame is None:
                continue
            frame_type = frame.get("type")
            other_chat = self.chat_id and frame.get("chat_id") and frame["chat_id"] != self.chat_id
            if other_chat:
                continue
            if frame_type == kind and (where is None or where(frame)):
                return frame
            if frame_type in FAILURE_FRAMES and kind not in FAILURE_FRAMES:
                raise TurnFailed(frame)
        seen = [f.get("type") for f in self.frames][-30:]
        raise TimeoutError(f"no {kind} frame within {timeout:.0f} s; the last frames: {seen}")

    def wait_for_card(self, timeout: float = 180.0) -> dict:
        """A permission card as the chat page gets it: its own frame, or, for a socket that
        attached to the turn after the card was shown, the held prompt its live state
        carries (``live_state.pending_permission``, which the page re-presents)."""
        def held(frame: dict) -> bool:
            pending = frame.get("pending_permission")
            return isinstance(pending, dict) and pending.get("event_type", CARD) == CARD and bool(
                pending.get("request_id"))
        frame = self.wait_for_any((CARD, "live_state"), timeout=timeout,
                                  where=lambda f: f.get("type") == CARD or held(f))
        if frame.get("type") == CARD:
            return frame
        return {**frame["pending_permission"], "type": CARD, "chat_id": frame.get("chat_id", self.chat_id)}

    def wait_for_any(self, kinds: tuple[str, ...], *, timeout: float = 180.0, where=None) -> dict:
        """The next frame of one of ``kinds`` for this chat. A failure frame raises."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.next_frame(timeout=min(5.0, max(0.1, deadline - time.monotonic())))
            if frame is None:
                continue
            if self.chat_id and frame.get("chat_id") and frame["chat_id"] != self.chat_id:
                continue
            if frame.get("type") in kinds and (where is None or where(frame)):
                return frame
            if frame.get("type") in FAILURE_FRAMES and not set(kinds) & set(FAILURE_FRAMES):
                raise TurnFailed(frame)
        seen = [f.get("type") for f in self.frames][-30:]
        raise TimeoutError(f"no {' or '.join(kinds)} frame within {timeout:.0f} s; the last frames: {seen}")

    def wait_done(self, timeout: float = 180.0) -> dict:
        return self.wait_for("done", timeout=timeout)

    def wait_rows(self, what: str, probe, *, timeout: float = 120.0) -> list[dict]:
        """Poll the rows until ``probe(rows)`` holds, reading the socket meanwhile."""
        deadline = time.monotonic() + timeout
        while True:
            rows = self.person.rows(self.chat_id)
            if probe(rows):
                return rows
            if time.monotonic() >= deadline:
                shape = [(r.get("role"), r.get("event_type")) for r in rows][-12:]
                raise TimeoutError(f"{what}: not within {timeout:.0f} s; the rows: {shape}")
            until = time.monotonic() + 2.0
            try:
                while time.monotonic() < until:
                    if self.ws is None:
                        time.sleep(2.0)
                        break
                    self.next_frame(timeout=0.5)
            except WebSocketClosed:
                self.ws = None

    def answer_card(self, frame: dict, *, approved: bool) -> None:
        self.ws.send_json({"type": "permission_response", "request_id": frame.get("request_id"), "approved": approved})

    def attach_terminal(self) -> None:
        self.ws.send_json({"type": "pty_attach", "chat_id": self.chat_id})

    def rows(self) -> list[dict]:
        return self.person.rows(self.chat_id)

    def frame_types(self) -> list[str]:
        return [f.get("type", "") for f in self.frames]

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None
