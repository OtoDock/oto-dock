"""Moving a live session to another account: why, onto which account, by what means, and the chat's one line.

The rebalance pass and the delisting rebind (``pool_acquire``, ``pool_rebind``) find a group of sessions whose
account they leave. A group is the sessions that share one sign-in file: one session when it has a folder of its
own, a folder's sessions otherwise. This module holds the rules they apply.

* **Why** (``Cause``): the account reached a limit (the vendor refused it: a rest), it is past its mark (a window
  at ``SPILL_PCT``), it left the selection, or the session's cache is cold and the pool would now pick another
  account for it (the return). Spending alone never moves a live session.
* **Onto which** (``accepts``): leaving a mark or returning goes only to an account that is under every mark for
  the group's models. A live session never moves between two accounts that are both past their marks. After a
  refusal or a delisting, any account that is not refused for the group's models will do: past its mark is still
  usable, and the selection puts the soonest reset first.
* **By what means**: an engine that takes another account's sign-in file under a live session
  (``AuthProfile.sign_in_in_place``) has the group's file rewritten over the fan-out, also while a turn runs (its
  next model call reads it). Any other engine has its chat's session closed once the turn has ended
  (``chat_reload.close_when_idle``), never while text typed into its terminal sits unsent there, and its next
  start takes the pool's pick. A session with no chat keeps its account to its end.
* **The line**: one "Account changed" note in the chat (``chat_reload.note_account_change``). It names an account
  only in a person's own session on an account they own, by the label they gave it, and never by an email: others
  may read the chat, so to them it is "another account".
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable

from services.engines import subscription_windows as _windows
from services.engines.pool_rests import (
    _is_throttled,
    _scope_resting,
    _scoped_rests,
    _throttled_hard,
    _window_rests,
)
from services.engines.pool_select import (
    _out_for,
    _scope_key,
    _window_readings,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)

#: How long a restart waits on text typed into a terminal before the chat is told once.
TERMINAL_HOLD_NOTICE_S = 15 * 60


class Cause(enum.Enum):
    LIMIT = "limit"          # the vendor refused the account (a rest): always eligible
    NEAR = "near"            # a window at its mark
    DELISTED = "delisted"    # the account left the selection: always eligible
    RETURN = "return"        # the cache is cold and the pool would pick another account

    @property
    def cooled(self) -> bool:
        """Whether the per-scope cooldown applies: to a return alone. A move off a mark never waits for it, since a
        target past its mark is never taken and two accounts cannot trade a group, and a refusal or a delisting
        never does."""
        return self is Cause.RETURN


def refused(sub_id: str, layer: str, models: frozenset[str]) -> bool:
    """The vendor refused the account for one of these models: an account-wide rest after a real limit, or the
    rest of one model's window."""
    if sub_id in _throttled_hard and _is_throttled(sub_id):
        return True
    return any(_scope_resting(sub_id, _scope_key(layer, m)) for m in models)


def reached(row: dict, models: frozenset[str]) -> bool:
    """The vendor's own reading says it refuses the account for one of these models: its ``reached`` names a
    whole-account window or the window of one of the models. A rest that lapsed leaves that word standing until
    the window resets (``effective``)."""
    reading = _window_readings([row]).get(row["id"])
    if reading is None or not reading.reached:
        return False
    if reading.reached in _windows.account_specs(reading.specs):
        return True
    layer = str(row.get("layer") or "")
    return any(reading.reached == f"scoped:{_scope_key(layer, m)}" for m in models if _scope_key(layer, m))


def past_mark(row: dict, models: frozenset[str]) -> bool:
    """A window the group's models count against is at its mark, or the vendor's own reading says it refuses."""
    return any(_out_for(row, m) for m in models)


def accepts(cause: Cause, row: dict, models: frozenset[str], *, old: dict | None = None) -> bool:
    """Whether a live group may move onto ``row`` for ``cause``. Leaving a mark or returning needs a reading of
    ``row`` under every mark (an account the pool has not read is not known to be under one), and a return needs
    readings of both accounts and a strictly sooner reset of ``row``'s quota than of ``old``'s: the pool's pick
    falls to spending when they tie or are unknown, and spending alone never moves a live session. After a refusal
    or a delisting any account the vendor does not refuse will do."""
    if cause not in (Cause.NEAR, Cause.RETURN):
        return not refused(row["id"], str(row.get("layer") or ""), models) and not reached(row, models)
    if _is_throttled(row["id"]) or past_mark(row, models):
        return False
    new_reading = _window_readings([row]).get(row["id"])
    if new_reading is None:
        return False
    if cause is Cause.NEAR:
        return True
    old_reading = _window_readings([old]).get(old["id"]) if old else None
    if old_reading is None:
        return False
    new_at, old_at = _windows.quota_reset(new_reading), _windows.quota_reset(old_reading)
    return new_at is not None and old_at is not None and new_at < old_at


def excluded(cause: Cause, old_sub: str, candidates: list[dict], layer: str,
             models: frozenset[str]) -> frozenset[str]:
    """The accounts the replacement's acquisition leaves out: the old one, and after a refusal or a delisting every
    account refused for the group's models, so the selection's fallback (past the mark, the one that frees first)
    never lands on one the vendor refuses."""
    out = {old_sub}
    if cause in (Cause.LIMIT, Cause.DELISTED):
        out |= {c["id"] for c in candidates if refused(c["id"], layer, models)}
    return frozenset(out)


def cold(last_active: float | None, cache_s: int, now: float | None = None) -> bool:
    """A session idle past its engine's cache lifetime starts cold on any account. Unknown is never cold."""
    if not cache_s or last_active is None:
        return False
    return (now if now is not None else time.time()) - last_active > cache_s


# ---------------------------------------------------------------------------
# The chat's line
# ---------------------------------------------------------------------------

def _window_name(spec) -> str:
    hours = round(spec.length_s / 3600)
    if hours <= 24:
        return {5: "five-hour"}.get(hours, f"{hours}-hour")
    days = round(spec.length_s / 86400)
    if days == 7:
        return "weekly"
    if 28 <= days <= 31:
        return "monthly"
    return f"{days}-day"


def window_words(old_sub: str, layer: str, models: frozenset[str], cause: Cause) -> str:
    """Which limit, in words: "weekly", "five-hour", or a model's own ("Fable"). "usage" when the pool cannot say."""
    specs = _windows.window_specs(layer)
    if cause is Cause.LIMIT:
        rest = _window_rests.get(old_sub)
        if rest and rest[0] in specs:
            return _window_name(specs[rest[0]])
        for (sub, family), _ in list(_scoped_rests.items()):
            if sub == old_sub and family in {_scope_key(layer, m) for m in models}:
                return family.capitalize()
        return "usage"
    reading = _window_readings([{"id": old_sub, "layer": layer, "auth_type": "oauth"}]).get(old_sub)
    if reading is None:
        return "usage"
    for key, spec in reading.specs.items():
        win = reading.windows.get(key)
        if win is not None and win.pct is not None and win.pct >= _windows.spill_pct(spec):
            return _window_name(spec)
    families = {_scope_key(layer, m) for m in models} - {""}
    for scoped in reading.scoped:
        if scoped.key in families and _windows.exhausted(reading, scoped.key):
            return scoped.label or scoped.key.capitalize()
    return "usage"


def line(cause: Cause, old_name: str, new_name: str, words: str, *, in_place: bool) -> str:
    """The chat's sentence about a move. A name is "" for an account the chat may not name
    (``account_label``), and ``new_name`` is "" for a restart: its next start picks the account."""
    old = old_name or "The account it used"
    new = new_name or "another account"
    why = {
        Cause.LIMIT: f"{old} reached its {words} limit.",
        Cause.NEAR: f"{old} is close to its {words} limit.",
        Cause.DELISTED: f"{old} is no longer offered to this chat.",
        Cause.RETURN: "",
    }[cause]
    if in_place:
        if cause is Cause.RETURN:
            return f"This chat now runs on {new}, whose limits reset sooner."
        return f"This chat now runs on {new}. {why}"
    if cause is Cause.RETURN:
        return "This chat starts again on another account at its next message."
    return f"{why} This chat starts again on another account at its next message."


def names(old_sub: str, new_sub: str, scope_sub: str) -> tuple[str, str]:
    """The two accounts' names for a move's line (``account_label``). Two accounts with one label (the plan's
    name at connect, "Claude Max" twice) are never named: the line says "another account"."""
    old, new = account_label(old_sub, scope_sub), account_label(new_sub, scope_sub) if new_sub else ""
    if old and new and old.casefold() == new.casefold():
        return "", ""
    return old, new


def account_label(sub_id: str, scope_sub: str) -> str:
    """How the line names an account: the label its owner gave it, in that person's own sessions (``scope_sub``),
    else "" (the line then says "another account"). Never the account's email: an agent's chats are read by others
    than the account's owner."""
    if not scope_sub:
        return ""
    row = subscription_store.get_subscription(sub_id) or {}
    if str(row.get("owner_sub") or "") != scope_sub:
        return ""
    return str(row.get("label") or "").strip()


def note(chat_id: str, message: str, *, waits: bool = False) -> None:
    """Write the chat's line, from a pool thread or the loop. ``waits``: the line says the change waits."""
    from services.engines import token_fanout
    from services.remote import chat_reload

    def _go() -> None:
        from core.background import spawn
        spawn(chat_reload.note_account_change(chat_id, message, waits=waits), name="account_move_line",
              owner="pool_moves")

    if not token_fanout.on_loop(_go):
        logger.debug("pool move: no loop for the line of chat %s", chat_id[:8])


# ---------------------------------------------------------------------------
# The restart of an engine that does not take a sign-in in place
# ---------------------------------------------------------------------------

def terminal_hold(chat_id: str, notice: str, *,
                  clock: Callable[[], float] = time.monotonic) -> Callable[[], bool]:
    """``close_when_idle``'s hold for one chat: True while the chat's terminal holds text typed into it and not
    sent. After ``TERMINAL_HOLD_NOTICE_S`` of that, the chat is told once (``notice``) and the hold goes on: the
    platform never restarts a terminal over the person's text. A draft in the dashboard's own input is not read."""
    state: dict[str, float | bool] = {"since": 0.0, "told": False}

    def _hold() -> bool:
        from core.session import interactive_session
        isess = interactive_session.find_live_for_chat(chat_id)
        # A submit whose turn is not known yet holds too: the box is empty at the Enter, and the turn it opens is
        # seen a tail later (``submit_settling``).
        if isess is None or not (isess.holds_unsent_text or isess.submit_settling):
            state["since"] = 0.0
            return False
        now = clock()
        if not state["since"]:
            state["since"] = now
        if not state["told"] and now - float(state["since"]) >= TERMINAL_HOLD_NOTICE_S:
            state["told"] = True
            note(chat_id, notice, waits=True)
        return True

    return _hold


def restart_when_idle(chat_ids: list[str], message: str, *, reason: str = "account") -> None:
    """Close each chat's session once its turn has ended and its terminal's input box is empty, then write the
    move's line. Its next message starts it again, and that start takes the pool's pick. A chat whose close
    already waits keeps it, so the terminal's notice comes once however many passes find the chat again."""
    from services.engines import token_fanout
    from services.remote import chat_reload

    def _go() -> None:
        for cid in chat_ids:
            # A close already waiting keeps its hold; a reload or a continuation after a limit that waits
            # closes the session itself and starts it on the pool's pick.
            if chat_reload.close_waiting(cid, reason) or chat_reload.waiting(cid):
                continue
            async def _after(cid: str = cid) -> None:
                await chat_reload.note_account_change(cid, message)
            chat_reload.close_when_idle(
                cid, reason=reason,
                hold=terminal_hold(cid, "This chat moves to another account when the text in its "
                                        "terminal is sent or cleared."),
                on_close=_after)

    if not token_fanout.on_loop(_go):
        logger.debug("pool move: no loop to close %d chat(s)", len(chat_ids))
