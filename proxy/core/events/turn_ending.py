"""A turn that ended other than by its own result: the model's safety
classifier declined it, the subscription reached its usage limit, the engine
reported an error, its process exited before it answered, it went silent
past the ceiling, the machine's stream was lost, or the person stopped it.

The engines report these as an error, a dead pipe or silence; the
translators, the session loops and the reaps turn them into one typed ending
carried on the ERROR event (``data["ending"]``), so the chat's card, the
task run's result and the delegator's callback name the same reason, the
pump persists it as the chat's ``turn_ended`` row, and the turn's owner can
end the engine process before anything else reads it (a declined Claude
turn goes on by itself otherwise).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

DECLINED = "declined"
LIMIT = "limit"
# The engine's own error result (Claude's ``is_error`` result, a Codex turn
# failure): the process is fine and the next message runs on it.
ERROR = "error"
# The engine's process gone before its result: the exit code and the stderr
# tail are the detail, the next message resumes the session.
EXITED = "exited"
# No stream line and no life (an open tool, a pending prompt, background
# work, hook activity) past the silence ceiling: a Claude process was
# ended, a Codex turn interrupted.
SILENT = "silent"
# The machine's stream severed or the satellite's process gone mid-turn.
LOST = "lost"
# The person's own Stop: stamped on the pump only, never an ERROR event.
STOPPED = "stopped"
REASONS = (DECLINED, LIMIT, ERROR, EXITED, SILENT, LOST, STOPPED)
# The endings the turn's owner ends the engine process on: a declined turn
# goes on by itself, a limit's process would hit the wall again, a silent
# one is the kill itself. An ``error`` keeps the process (it answered), an
# ``exited`` one is already gone, a ``lost`` one is not here.
KILLS_PROCESS = frozenset({DECLINED, LIMIT, SILENT})
# The endings whose card offers to send the message again verbatim: the
# same words would be declined again, and a limit needs its reset.
RESEND = frozenset({ERROR, EXITED, SILENT, LOST})
# A decline's detail when Codex's Guardian stopped the turn after too many
# of its actions were denied (``codexErrorInfo: tooManyDenials``, Codex
# 0.160): not the model's classifier, so the words differ and no model
# change is offered.
DENIALS = "denials"

# The engines' own limit notices: Claude Code's "You've reached your Fable
# limit. …" / "You've hit your session limit · resets 3pm (UTC)", its
# usage-credits wording "You're out of usage credits. …" and the older
# "Claude AI usage limit reached|<epoch>"; Codex's
# "You've hit your usage limit. …".
_LIMIT_RE = re.compile(
    r"\byou'?ve (?:reached|hit) your\b[^.\n]{0,60}?\blimit\b|\busage limit reached\b"
    r"|\bout of usage credits\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TurnEnding:
    reason: str
    # ISO-8601 UTC instant the limit resets; "" when the engine did not say.
    resets_at: str = ""
    # The engine's own words: the classifier's category for a decline, the
    # limit notice for a limit, the error text for an error, the exit and
    # the stderr tail for an exit, the silence for a silent one, the reap's
    # reason for a lost one.
    detail: str = ""
    # The window a limit reached, in the windows' own keys
    # (``Windows.reached``): a declared window key (``five_hour``,
    # ``seven_day``: account-wide) or ``scoped:<scope key>`` (one model
    # family's window); "" when the engine did not say. The pool rests the
    # account by it (``subscription_pool.rest_after_limit``).
    window: str = ""
    # The process's exit code for ``exited``, None otherwise.
    exit_code: int | None = None
    # The engine's own history kept the turn: a Codex ``silent`` (the
    # interrupt keeps the rollout), so the next turn needs no cancelled
    # context.
    graceful: bool = False

    def line(self) -> str:
        """The one user-facing line the chat, the run and the callback show."""
        if self.reason == DECLINED and self.detail == DENIALS:
            return ("⚠ Codex stopped this turn after too many of its actions were denied, "
                    "so it ended here. Change the permission mode or rephrase the request.")
        if self.reason == DECLINED:
            cat = f" ({self.detail})" if self.detail else ""
            return (f"⚠ The model's safety classifier declined this turn{cat}, so it "
                    "ended here. Rephrase the request, or continue on another model.")
        if self.reason == LIMIT:
            when = format_reset(self.resets_at)
            head = f"⚠ Usage limit reached{', resets ' + when if when else ''}."
            tail = self.detail or "Continue after the reset, or on another model."
            return f"{head} {tail}"
        if self.reason == ERROR:
            what = f": {self.detail}" if self.detail else ""
            return f"⚠ The engine reported an error and the turn ended here{what}. Send the message again."
        if self.reason == EXITED:
            code = f" (exit code {self.exit_code})" if self.exit_code is not None else ""
            return (f"⚠ The engine's process exited before it answered{code}. Send the "
                    "message again, the conversation continues where it left off.")
        if self.reason == SILENT:
            how = f" ({self.detail})" if self.detail else ""
            return (f"⚠ The engine went silent with nothing running{how}, so the turn was "
                    "ended. Send the message again.")
        if self.reason == LOST:
            return ("⚠ The connection to the machine running this chat was lost mid-turn. "
                    "Send the message again once the machine is back.")
        return "Stopped."

    def summary(self) -> str:
        """The run's error message: the reason first, then the engine's words."""
        if self.reason == DECLINED and self.detail == DENIALS:
            return "Stopped after too many of its actions were denied."
        if self.reason == DECLINED:
            cat = f" ({self.detail})" if self.detail else ""
            return f"Declined by the model's safety classifier{cat}."
        if self.reason == LIMIT:
            when = format_reset(self.resets_at)
            head = f"Usage limit reached{', resets ' + when if when else ''}"
            return f"{head}: {self.detail}" if self.detail else f"{head}."
        if self.reason == ERROR:
            return f"Engine error: {self.detail}" if self.detail else "Engine error."
        if self.reason == EXITED:
            return f"The engine's process exited before its result ({self.detail})." \
                if self.detail else "The engine's process exited before its result."
        if self.reason == SILENT:
            return f"The engine went silent ({self.detail})." if self.detail \
                else "The engine went silent."
        if self.reason == LOST:
            return f"The machine's stream was lost mid-turn ({self.detail})." if self.detail \
                else "The machine's stream was lost mid-turn."
        return "Stopped by the person."

    def callback_note(self, worker_chat_id: str) -> str:
        """What the delegating agent is told besides the worker's output."""
        cont = (f'delegate(continue_id="{worker_chat_id}", model=…)' if worker_chat_id
                else "delegate(continue_id=…, model=…)")
        plain = (f'delegate(continue_id="{worker_chat_id}")' if worker_chat_id
                 else "delegate(continue_id=…)")
        if self.reason == DECLINED and self.detail == DENIALS:
            return ("The worker's turn stopped after too many of its actions were denied. "
                    f"Continue it with {cont} after changing its permission mode, or rephrase "
                    "the request.")
        if self.reason == DECLINED:
            cat = f" ({self.detail})" if self.detail else ""
            return (f"The worker's turn was declined by the model's safety classifier{cat} "
                    f"and its engine was stopped. Continue it on another model with {cont}, "
                    "or rephrase the request.")
        if self.reason == LIMIT:
            when = format_reset(self.resets_at)
            reset = f", which resets {when}" if when else ""
            return (f"The worker's turn stopped on the subscription's usage limit{reset}. "
                    f"Continue it after the reset, or on another model with {cont}.")
        if self.reason == ERROR:
            what = f": {self.detail}" if self.detail else ""
            return (f"The worker's turn ended on an engine error{what}. Its session is "
                    f"still there: continue it with {plain}.")
        if self.reason == EXITED:
            return ("The worker's engine process exited before it answered. Its session "
                    f"resumes where it left off: continue it with {plain}.")
        if self.reason == SILENT:
            return ("The worker's engine went silent past the ceiling and was ended. "
                    f"Its session resumes where it left off: continue it with {plain}.")
        if self.reason == LOST:
            return ("The connection to the worker's machine was lost mid-turn. Continue it "
                    f"with {plain} once the machine is back.")
        return f"The worker's turn was stopped by a person. Continue it with {plain}."

    def abort_stamps(self) -> dict | None:
        """The chat flags the pump writes with the ending's row, or None: a
        process that went (``exited``, ``silent``, ``lost``) lost its partial
        turn, so the next turn re-injects the cancelled context, unless the
        engine's own history kept it (``graceful``: a Codex interrupt). The
        engine answered on an ``error``, and a decline or a limit kept its
        history."""
        if self.reason == EXITED:
            return {"last_turn_aborted": True, "last_abort_graceful": False}
        if self.reason in (SILENT, LOST):
            return {"last_turn_aborted": True, "last_abort_graceful": self.graceful}
        return None

    def as_dict(self) -> dict:
        out = {"reason": self.reason, "resets_at": self.resets_at, "detail": self.detail,
               "window": self.window}
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.graceful:
            out["graceful"] = True
        return out


def from_dict(data) -> TurnEnding | None:
    """The ending an event or a payload carries; None for anything else."""
    if not isinstance(data, dict) or data.get("reason") not in REASONS:
        return None
    code = data.get("exit_code")
    return TurnEnding(
        reason=data["reason"],
        resets_at=str(data.get("resets_at") or ""),
        detail=str(data.get("detail") or ""),
        window=str(data.get("window") or ""),
        exit_code=int(code) if isinstance(code, int) and not isinstance(code, bool) else None,
        graceful=bool(data.get("graceful")),
    )


def is_limit_text(text: str) -> bool:
    """Whether an engine's error text is its usage-limit notice."""
    return bool(text) and _LIMIT_RE.search(text) is not None


def epoch_to_iso(value) -> str:
    """An epoch instant (seconds) as ISO-8601 UTC; "" when it is not one."""
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()


def format_reset(resets_at: str) -> str:
    """``2026-10-01 15:00 UTC`` for an ISO instant; "" when there is none."""
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
