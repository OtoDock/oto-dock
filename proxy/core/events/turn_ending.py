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
# The person's own Stop: the run's word for a stopped turn, and the ending
# of a message sent to a session whose process the proxy's own abort ended
# (a review turn, a queued message) — that is a stop, not a lost machine.
STOPPED = "stopped"
# The platform restarted while the turn ran and nothing replayed it: written
# as the chat's closing row when the platform comes back
# (``services/scheduler/cut_turns.py``), never carried on an ERROR event.
RESTARTED = "restarted"
REASONS = (DECLINED, LIMIT, ERROR, EXITED, SILENT, LOST, STOPPED, RESTARTED)
# The endings the turn's owner ends the engine process on: a declined turn
# goes on by itself, a limit's process would hit the wall again, a silent
# one is the kill itself. An ``error`` keeps the process (it answered), an
# ``exited`` one is already gone, a ``lost`` one is not here.
KILLS_PROCESS = frozenset({DECLINED, LIMIT, SILENT})
# The endings whose card offers to send the message again verbatim: the
# same words would be declined again, a limit needs its reset, and a reply
# the restart cut has its work so far in the chat (sending the first message
# again would start it over).
RESEND = frozenset({ERROR, EXITED, SILENT, LOST, STOPPED})
# A decline's detail when Codex's Guardian stopped the turn after too many
# of its actions were denied (``codexErrorInfo: tooManyDenials``, Codex
# 0.160): not the model's classifier, so the words differ and no model
# change is offered.
DENIALS = "denials"
# An ``exited`` ending's detail when the machine's memory guard ended the
# engine (it was the largest part of a session that ate the machine's memory).
MEMORY = "memory"
# A ``lost`` ending's detail when the machine stayed away past the bound a
# held turn is kept for (``core/remote/remote_reaper.py``).
AWAY = "away"
# Two details of ``restarted``. The machine the chat runs on restarted, not
# the platform (a record of the cause ``machine`` that nothing continues).
MACHINE = "machine"
# The platform restarted before a delegated follow-up's round opened: the
# worker never read it.
NOT_STARTED = "not_started"

# The engines' own limit notices: Claude Code's "You've hit your Fable
# limit · resets …" / "You've hit your session limit · resets 3pm (UTC)", its
# usage-credits wording "You're out of usage credits. …" and the org's
# "Your org is out of usage · add funds to continue" (a Team or Enterprise
# login), the older "Claude AI usage limit reached|<epoch>"; Codex's
# "You’ve hit your usage limit. …" (a typographic apostrophe). A plan fact
# is not a limit: "Your seat type doesn't include usage credits" stays a
# plain error.
_LIMIT_RE = re.compile(
    r"\byou['\u2019]?ve (?:reached|hit) your\b[^.\n]{0,60}?\blimit\b|\busage limit reached\b"
    r"|\bout of usage\b",
    re.IGNORECASE,
)

# Claude Code's own sentence for the vendor's brief throttle, a 429 that is
# no limit of the account: "Server is temporarily limiting requests (not
# your usage limit)" (read in the 2.1.325 bundle). The transcript's row
# marks it ``apiErrorIsTransient``; the stream's frame carries the words
# alone, and both say ``error: "rate_limit"``.
_BRIEF_THROTTLE = "not your usage limit"

# A provider that is down, or refuses at its network edge, in the engine's
# error text: the edge's block page wins over everything, else an HTTP 5xx
# from the API or its "overloaded" answer, unless the text names a sign-in
# that really ended (that keeps the error's own line). A number alone is no
# status: "line 529" is not an outage.
_PROVIDER_EDGE_RE = re.compile(r"attention required!? \| cloudflare|just a moment\.\.\.", re.IGNORECASE)
_PROVIDER_DOWN_RE = re.compile(
    r"\b(?:api error|status(?: code)?|http)[: ]*5\d\d\b|\boverloaded(?:_error)?\b"
    r"|\binternal server error\b|\bbad gateway\b|\bservice unavailable\b|\bgateway time-?out\b",
    re.IGNORECASE,
)
_SIGN_IN_RE = re.compile(
    r"not logged in|/login\b|\b401\b|authentication_error|\bunauthorized\b"
    r"|invalid (?:api key|x-api-key|authentication)|token (?:has )?(?:expired|been revoked)",
    re.IGNORECASE,
)
#: How much of a provider's error text a line quotes (a block page is a whole page).
_PROVIDER_WORDS_MAX = 300
PROVIDER_DOWN = ("The AI provider did not answer this request, so the turn ended here. This is "
                 "usually an outage on the provider's side, not a problem with your sign-in or "
                 "with this OtoDock install.")


#: What a caller hears when the engine stopped and the next turn starts it again.
SPOKEN_STOPPED = "Sorry, I had to stop for a moment. Could you say that again?"
#: What a caller hears when the call could not start (the server is full, no
#: compute machine is connected, the start failed): the platform's reason
#: goes to the phone service's log, never to the caller, who is outside the
#: company.
SPOKEN_REFUSED = "Sorry, I cannot take the call right now. Please call again in a few minutes."


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
    # An ``error`` the vendor gave because it refused the session's sign-in
    # (Codex's ``unauthorized``, a request answered 401): the pool repairs
    # the account (``pool_tokens.repair_sign_in``).
    sign_in: bool = False

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
        if self.reason == ERROR and self.sign_in:
            return ("⚠ The AI account's sign-in was refused, so the turn ended here. "
                    "Send the message again in a minute.")
        if self.reason == ERROR and is_provider_down_text(self.detail):
            return f"⚠ {PROVIDER_DOWN}{_said(self.detail)} Send the message again in a few minutes."
        if self.reason == ERROR:
            what = f": {self.detail}" if self.detail else ""
            return f"⚠ The engine reported an error and the turn ended here{what}. Send the message again."
        if self.reason == EXITED and self.detail == MEMORY:
            return ("⚠ This chat used too much memory, so its engine was stopped. Send the "
                    "message again, the conversation continues where it left off.")
        if self.reason == EXITED:
            code = f" (exit code {self.exit_code})" if self.exit_code is not None else ""
            return (f"⚠ The engine's process exited before it answered{code}. Send the "
                    "message again, the conversation continues where it left off.")
        if self.reason == SILENT:
            how = f" ({self.detail})" if self.detail else ""
            return (f"⚠ The engine went silent with nothing running{how}, so the turn was "
                    "ended. Send the message again.")
        if self.reason == LOST and self.detail == AWAY:
            return ("⚠ The machine running this chat has been away too long, so the turn "
                    "was ended. Send the message again once the machine is back.")
        if self.reason == LOST:
            return ("⚠ The connection to the machine running this chat was lost mid-turn. "
                    "Send the message again once the machine is back.")
        if self.reason == RESTARTED and self.detail == MACHINE:
            return ("⚠ The reply was cut short when the machine this chat runs on restarted. "
                    "Send a message to continue.")
        if self.reason == RESTARTED and self.detail == NOT_STARTED:
            return "⚠ The platform restarted before this message was read. Send it again."
        if self.reason == RESTARTED:
            return ("⚠ The reply was cut short when the platform restarted. "
                    "Send a message to continue.")
        return "⚠ The engine was stopped before this message ran. Send it again."

    def spoken(self) -> str:
        """What a caller on the phone hears for this ending, "" when the
        phone service's own apology is the right one. An engine that
        stopped (the machine's memory guard, an exit, a lost link) is
        started again by the call's next turn, so the caller is asked for
        that turn. It names no internals: a caller is outside the company."""
        if self.reason in (EXITED, LOST):
            return SPOKEN_STOPPED
        return ""

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
        if self.reason == ERROR and is_provider_down_text(self.detail):
            return f"The AI provider did not answer, usually an outage on its side: {_clip(self.detail)}"
        if self.reason == ERROR:
            return f"Engine error: {self.detail}" if self.detail else "Engine error."
        if self.reason == EXITED:
            return f"The engine's process exited before its result ({self.detail})." \
                if self.detail else "The engine's process exited before its result."
        if self.reason == SILENT:
            return f"The engine went silent ({self.detail})." if self.detail \
                else "The engine went silent."
        if self.reason == LOST and self.detail == AWAY:
            return "The machine stayed away too long, so the turn was ended."
        if self.reason == LOST:
            return f"The machine's stream was lost mid-turn ({self.detail})." if self.detail \
                else "The machine's stream was lost mid-turn."
        if self.reason == RESTARTED and self.detail == MACHINE:
            return "Cut short when its machine restarted."
        if self.reason == RESTARTED and self.detail == NOT_STARTED:
            return "Not started. The platform restarted first."
        if self.reason == RESTARTED:
            return "Cut short when the platform restarted."
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
        if self.reason == ERROR and is_provider_down_text(self.detail):
            return ("The worker's turn ended because the AI provider did not answer, usually an "
                    "outage on the provider's side and not a sign-in problem: "
                    f"{_clip(self.detail)}. Its session is still there: continue it with {plain} "
                    "in a few minutes.")
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
        if self.reason == LOST and self.detail == AWAY:
            return ("The worker's machine stayed away too long, so its turn was ended. "
                    f"Continue it with {plain} once the machine is back.")
        if self.reason == LOST:
            return ("The connection to the worker's machine was lost mid-turn. Continue it "
                    f"with {plain} once the machine is back.")
        if self.reason == RESTARTED and self.detail == MACHINE:
            return ("The worker's turn was cut short when its machine restarted. Its work "
                    f"so far is in its chat: continue it with {plain}.")
        if self.reason == RESTARTED and self.detail == NOT_STARTED:
            return ("The platform restarted before the worker read this follow-up, so "
                    f"nothing of it ran. Send it again with {plain}.")
        if self.reason == RESTARTED:
            return ("The worker's turn was cut short when the platform restarted. Its work "
                    f"so far is in its chat: continue it with {plain}.")
        return f"The worker's turn was stopped by a person. Continue it with {plain}."

    def abort_stamps(self) -> dict | None:
        """The chat flags the pump writes with the ending's row, or None: a
        process that went (``exited``, ``silent``, ``lost``) lost its partial
        turn, so the next turn re-injects the cancelled context, unless the
        engine's own history kept it (``graceful``: a Codex interrupt). A
        ``stopped`` message met a process the proxy's abort ended: the send
        consumed the Stop's own flags while it built its batch, so the next
        turn needs them again. The engine answered on an ``error``, and a
        decline or a limit kept its history. A ``restarted`` turn's process
        ended with the platform, but its engine's own record holds the turn
        (the CLI's or Codex's transcript, Direct LLM's chat rows, which the
        checkpoints wrote): graceful, so the next turn repeats none of it."""
        if self.reason == RESTARTED:
            return {"last_turn_aborted": True, "last_abort_graceful": True}
        if self.reason in (EXITED, STOPPED):
            return {"last_turn_aborted": True, "last_abort_graceful": False}
        if self.reason in (SILENT, LOST):
            return {"last_turn_aborted": True, "last_abort_graceful": self.graceful}
        return None

    def as_dict(self) -> dict:
        out: dict = {"reason": self.reason, "resets_at": self.resets_at, "detail": self.detail,
                     "window": self.window}
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.graceful:
            out["graceful"] = True
        if self.sign_in:
            out["sign_in"] = True
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
        sign_in=data.get("sign_in") is True,
    )


def is_limit_text(text: str) -> bool:
    """Whether an engine's error text is its usage-limit notice."""
    return bool(text) and _LIMIT_RE.search(text) is not None


def is_brief_throttle_text(text: str) -> bool:
    """Whether an engine's error text is the vendor's brief throttle: the
    provider is busy for a moment and the account has room."""
    return _BRIEF_THROTTLE in (text or "").lower()


def is_provider_down_text(text: str) -> bool:
    """Whether an engine's error text says its AI provider is down or
    refused the request at its network edge, not that a sign-in ended."""
    if not text:
        return False
    if _PROVIDER_EDGE_RE.search(text):
        return True
    return _SIGN_IN_RE.search(text) is None and _PROVIDER_DOWN_RE.search(text) is not None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _PROVIDER_WORDS_MAX else text[:_PROVIDER_WORDS_MAX].rstrip() + "…"


def _said(detail: str) -> str:
    """The engine's own words after the plain sentences, as a sentence."""
    words = _clip(detail).rstrip()
    if not words:
        return ""
    return f" The engine said: {words}{'' if words.endswith(('.', '!', '?', '…')) else '.'}"


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
