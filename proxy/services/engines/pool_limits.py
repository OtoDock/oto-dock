"""A vendor's limit reaching the pool: the rest it starts, from a typed ending, an error text or a tailer, and
whether the turn it cut may continue on another account (``limit_continues``).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time

import psycopg

from services.engines import pool_moves, subscription_windows as _windows
from services.engines.pool_acquire import (
    _eligible_candidates,
    schedule_rebalance,
)
from services.engines.pool_bindings import (
    bound_attribution,
    get_session_subscription,
    release_subscription,
)
from services.engines.pool_rests import (
    _THROTTLE_COOLDOWN_S,
    _rest_account,
    _scoped_rests,
    throttle_cooldown_for,
)
from services.engines.pool_state import (
    _session_binding_ctx,
)
from services.engines.pool_writer import (
    _queue_write,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)

# How long the pump waits for the answer to "may the turn a limit cut go on
# on another account" before it shows the limit card as before.
LIMIT_ANSWER_S = 2.0
# ``after_limit``'s answers: the turn goes on, it may not, or it may and no
# account serves it now (a terminal's mark then waits for one).
_GOES_ON, _REFUSED, _NO_ACCOUNT = "goes_on", "refused", "no_account"


def mark_subscription_throttled(session_id: str, *, cooldown_s: int = _THROTTLE_COOLDOWN_S) -> None:
    """Temporarily remove the subscription bound to ``session_id`` from selection
    after it hit a provider rate/usage limit, so the next chat/turn fails over to a
    fresh account. Best-effort; the rest outlives a restart (``load_rests``).

    Read-through binding lookup (not just the live map): the interactive
    tailers report limits for sessions that may have outlived a proxy restart,
    where only the persisted row knows the account. A REAL limit (the full
    cooldown class) additionally fires the reactive scope rebalance — every
    scope pinned to this account gets re-homed onto a fresh one NOW, live
    sessions included, instead of erroring until the account's window resets.
    An overload nudge deliberately doesn't (see ``_throttled_hard``)."""
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return
    hard = cooldown_s >= _THROTTLE_COOLDOWN_S
    if _rest_account(sub_id, time.time() + cooldown_s, hard=hard):
        logger.info("Pool: throttled subscription %s for %ss (limit hit)", sub_id[:8], cooldown_s)
    if hard:
        _read_after_limit(sub_id)
        schedule_rebalance("provider limit")


def _read_after_limit(sub_id: str) -> None:
    """One reading of the account right after a limit error: the vendor's
    own windows and resets, which also end a rest the limit did not need
    (``clear_rests_with_headroom``). At most one per account in five minutes
    (``_LIMIT_POLL_FLOOR_S``, any poll counts), and a refusal's backoff holds."""
    _windows.request_poll(sub_id, floor_s=_windows._LIMIT_POLL_FLOOR_S, reason="a limit error")


def rest_after_limit(session_id: str, ending, err_msg: str = "") -> None:
    """Rest the subscription a failed turn ran on, by what ended it (the
    stream pump's ERROR branch):

    * a typed usage-limit ending that names its reached window and a reset
      still ahead: until that reset, bounded by the window's declared length.
      An account-wide window (the session or the weekly window) rests the
      account; a model family's window (``scoped:<scope key>``) rests it for
      spawns of that scope only (``usage_scope_key``), other models keep
      using it;
    * any other typed limit ending (no window, no reset, an unknown window):
      the usual cooldown (``_THROTTLE_COOLDOWN_S``);
    * no typed ending: the error text's class (``throttle_cooldown_for``: a
      limit the usual cooldown, a transient overload the brief nudge).

    Every eligible account resting still falls back to the one that frees
    first (``_select``), so a single-account install shows the vendor's own
    limit message rather than "no subscription"."""
    from core.events import turn_ending
    if ending is not None and ending.reason == turn_ending.LIMIT:
        if not _rest_until_reset(session_id, ending):
            mark_subscription_throttled(session_id)
        return
    cooldown = throttle_cooldown_for(err_msg)
    if cooldown:
        mark_subscription_throttled(session_id, cooldown_s=cooldown)


def _iso_epoch(value: str) -> float | None:
    from datetime import datetime, timezone
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _window_length_s(session_id: str, window: str) -> float | None:
    """The declared length of the window a limit ending names: the session's
    engine's declaration (its binding's acquisition context), else the
    longest any registered engine declares under that key. A model family's
    window settles by the declared window per model, else the quota window's
    length, as the readings do. None for a window nobody declares."""
    ctx = _session_binding_ctx.get(session_id)
    if ctx and ctx[0]:
        declared = [_windows.window_specs(ctx[0])]
    else:
        from core.session.session_manager import get_all_layers
        declared = [_windows.window_specs(name) for name in get_all_layers()]
    lengths: list[float] = []
    for specs in declared:
        if window.startswith("scoped:"):
            model = next((s for s in specs.values() if s.scope == "model"), None) or _windows.quota_spec(specs)
            if model is not None:
                lengths.append(float(model.length_s))
        elif window in specs:
            lengths.append(float(specs[window].length_s))
    return max(lengths) if lengths else None


def _rest_until_reset(session_id: str, ending) -> bool:
    """The window rest of ``rest_after_limit``; False when the ending cannot
    carry one (no window, no future reset, a window nobody declares) or the
    session has no account."""
    window = getattr(ending, "window", "") or ""
    reset = _iso_epoch(getattr(ending, "resets_at", "") or "")
    now = time.time()
    if not window or reset is None or reset <= now:
        return False
    length = _window_length_s(session_id, window)
    if length is None:
        return False
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return False
    until = min(reset, now + length)
    if window.startswith("scoped:"):
        scope = window[len("scoped:"):]
        if not scope:
            return False
        _rest_scope(sub_id, scope, until, now)
    elif _rest_account(sub_id, until, window=window, hard=True):
        logger.info("Pool: subscription %s rests until its %s window "
                    "resets (%ss, usage limit)",
                    sub_id[:8], window, int(until - now))
    _read_after_limit(sub_id)
    schedule_rebalance("provider limit")
    return True


def _rest_scope(sub_id: str, scope: str, until: float, now: float) -> None:
    """Rest the account for spawns of one model family's scope until ``until``."""
    held = _scoped_rests.get((sub_id, scope))
    if held is None or held[0] < until:
        _scoped_rests[(sub_id, scope)] = (until, now)
        _queue_write("saving a rest", subscription_store.save_rest,
                     sub_id, scope, until, now, "", False)
    logger.info("Pool: subscription %s rests for %s until its window "
                "resets (%ss, usage limit)",
                sub_id[:8], scope, int(until - now))


def nudge_from_cli_error(session_id: str, error_text: str) -> None:
    """An engine's API error in a terminal that is no limit (its limits are
    typed endings, ``terminal_limit``): a transient overload rests the account
    briefly, so an immediate retry is not blocked. Callers pass ONLY text the
    CLI itself marked as an error, never model prose. Safe from tailer
    threads."""
    cooldown = throttle_cooldown_for(error_text)
    if cooldown and cooldown < _THROTTLE_COOLDOWN_S:
        mark_subscription_throttled(session_id, cooldown_s=cooldown)


def _reading_of(sub_id: str):
    """The account's latest window reading, None when there is none (the
    setting off, no sample, a failed read). Synchronous: the DB executor."""
    from services.engines.pool_select import _window_readings
    row = subscription_store.get_subscription(sub_id) or {}
    return _window_readings([row]).get(sub_id) if row else None


def _account_window_full(reading) -> str:
    """The key of an account-wide window the reading shows full (at 100 % or
    the vendor's reached flag), "" for none."""
    for key, win in reading.windows.items():
        if key in reading.specs and (win.pct >= 100.0 or reading.reached == key):
            return key
    return reading.reached if reading.reached in reading.specs else ""


def _families_full(reading, family: str) -> list:
    """The model families a limit with no window reached, read from the
    account's reading: each family whose window is at 100 % or reached, when
    no account-wide window is; none when no family is, or when the session's
    own family is known and is not among them (the reading does not explain
    the refusal)."""
    if _account_window_full(reading):
        return []
    full = [s for s in reading.scoped
            if s.pct >= 100.0 or reading.reached == f"scoped:{s.key}"]
    if family and family not in {s.key for s in full}:
        return []
    return full


def _rest_by_reading(session_id: str, ending, *, from_row: bool):
    """The rest of a limit the engine did not fully say, by the account's
    latest reading (``follow_ending``'s off-loop step), and the ending with
    the reset the reading knows when the engine named none:

    * no window: every model family the reading shows full rests until its
      own window resets, when no account-wide window is full; an account-wide
      window the reading shows full rests the account until it resets; else
      the account's usual cooldown;
    * a window named by a terminal's row (``from_row``: a file the session can
      write) rests until its reset only when the reading backs it, else the
      usual cooldown, so a forged row costs a shared account no more than
      before.

    Synchronous: the DB executor."""
    from dataclasses import replace
    from datetime import datetime, timezone

    from services.engines.pool_select import _scope_facts, _scope_key
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return ending
    reading = _reading_of(sub_id)
    ctx = _session_binding_ctx.get(session_id)
    models = _scope_facts([session_id]).models
    family = _scope_key(ctx[0] if ctx else None, next(iter(models))) if len(models) == 1 else ""
    window = getattr(ending, "window", "") or ""
    now = datetime.now(timezone.utc)
    if reading is not None and not window:
        full = _families_full(reading, family)
        if full:
            for s in full:
                reset = (s.resets_at.timestamp() if s.resets_at is not None
                         else _iso_epoch(ending.resets_at or ""))
                if reset is not None and reset > now.timestamp():
                    _rest_scope(sub_id, s.key, reset, now.timestamp())
            _read_after_limit(sub_id)
            schedule_rebalance("provider limit")
            first = min((s.resets_at for s in full if s.resets_at is not None), default=None)
            return replace(ending, window=f"scoped:{full[0].key}",
                           resets_at=ending.resets_at or (first.isoformat() if first else ""))
        key = _account_window_full(reading)
        win = reading.windows.get(key) if key else None
        if win is not None and win.resets_at is not None:
            ending = replace(ending, window=key, resets_at=ending.resets_at or win.resets_at.isoformat())
            if _rest_until_reset(session_id, ending):
                return ending
    elif window and from_row and not (reading is not None and (
            (window.startswith("scoped:") and not _windows.has_headroom(reading, window)
             and reading.scoped_for(window[len("scoped:"):]) is not None)
            or (not window.startswith("scoped:") and _account_window_full(reading) == window))):
        mark_subscription_throttled(session_id)
        return ending
    elif window and _rest_until_reset(session_id, ending):
        return ending
    mark_subscription_throttled(session_id)
    if not ending.resets_at and reading is not None:
        frees = _windows.frees_at(reading, family)
        if frees is not None:
            ending = replace(ending, resets_at=frees.isoformat())
    return ending


async def follow_ending(side, ending, typed, data: dict, err_msg: str) -> bool:
    """What follows an error that ended a turn, the pump's and a terminal's
    alike (one owner): the account rested by what ended it, the side's turn
    closed (``side.close_turn``: the pump's queue, a terminal's own), the
    turn's continuation on another account (``after_limit``), else the card
    (``side.show_card``, which the side writes its own way). A limit with no
    window or no reset, and any limit a terminal's row raised, rests by the
    account's reading, read off the loop after the turn closed and before
    the answer, under one deadline (``LIMIT_ANSWER_S``); past it the account
    rests and the card shows. Every other rest is taken here and now, before
    any await, as the pump always did. Whether the turn goes on."""
    from core.background import spawn
    from core.events import turn_ending
    from core.events.stream_pump import _card_frame
    from storage.pg import run_db
    sid = side.session_id
    if typed is not None and typed.sign_in:
        from services.engines import pool_tokens
        pool_tokens.repair_sign_in_soon(sid)
    old_sub = bound_attribution(sid)[0]
    limited = typed is not None and typed.reason == turn_ending.LIMIT
    from_row = bool(getattr(side, "terminal", False))
    by_reading = limited and (from_row or not typed.window or not typed.resets_at)
    if not by_reading:
        if old_sub:
            rest_after_limit(sid, typed, err_msg)
        else:
            spawn(run_db(rest_after_limit, sid, typed, err_msg),
                  name="rest_after_limit", owner="pool_limits")
    side.close_turn()
    deadline = time.monotonic() + LIMIT_ANSWER_S
    if by_reading:
        try:
            ending = typed = await asyncio.wait_for(
                run_db(functools.partial(_rest_by_reading, sid, typed, from_row=from_row)), LIMIT_ANSWER_S)
        except (TimeoutError, psycopg.Error):
            logger.info("Pool: session %s: its account's reading came too late, the account rests", sid[:8])
            await run_db(mark_subscription_throttled, sid)
            within = 0.0
        else:
            within = max(0.0, deadline - time.monotonic())
        side.last_ending = ending
    else:
        within = LIMIT_ANSWER_S
    if within > 0 and await after_limit(side, typed, data, old_sub, within=within):
        return True
    await side.show_card(ending, _card_frame(ending, err_msg, typed))
    return False


async def terminal_limit(isess, ending, *, closes: bool = True) -> None:
    """A terminal's turn ended at a vendor's limit (its tailer's typed ending,
    folded on the loop, which already closed the turn and set the mark): the
    rest, the move and the card through ``follow_ending``, as the pump's. With
    no continuation the turn's waiters end with the limit (a task's run, a
    borrowed round), and the mark keeps whether only an account was
    missing, so a later re-home continues the turn (``account_freed``).
    ``closes`` false: the person typed after the limit's row, so no turn
    waits to be continued and the rest and the card follow alone."""
    from core.session.interactive_turn import TerminalLimitSide
    side = TerminalLimitSide(isess, closes=closes)
    goes_on = False
    try:
        goes_on = await follow_ending(side, ending, ending, {}, ending.detail)
    finally:
        # A step that failed still ends what waits on the turn with the limit
        # (a task's run, a borrowed round), never at the backstop.
        mark = isess.limit_mark
        if mark is not None:
            mark.renew(side.last_ending or ending, waits_for_account=side.waits_for_account and not goes_on)
        if not goes_on:
            isess.end_waiters_with(side.last_ending or ending)


def account_freed(session_id: str, old_sub: str) -> None:
    """A session left the account it was bound to for another (the
    rebalance's re-home): a terminal marked at a limit is free of it. When
    the limit refused its continuation only for want of an account
    (``LimitMark.waits_for_account``), the turn continues once now, in
    place, through the reload's entrance, its person allowed again
    (``may_continue_after_limit``). From any thread: on the loop."""
    from services.engines import token_fanout
    token_fanout.on_loop(functools.partial(_spawn_freed, session_id, old_sub))


def _spawn_freed(session_id: str, old_sub: str) -> None:
    from core.background import spawn
    spawn(_account_freed(session_id, old_sub), name=session_id[:8], owner="pool_limits")


async def _account_freed(session_id: str, old_sub: str) -> None:
    from core.session import interactive_session
    from services.remote import chat_reload
    from storage.pg import run_db
    isess = interactive_session.get(session_id)
    if isess is None or isess.limit_mark is None:
        return
    mark = isess.limit_mark
    isess.clear_limit_mark("its account changed")
    person = isess.user_sub or ""
    if (mark.waits_for_account and isess.alive and isess.chat_id
            and await run_db(chat_reload.may_continue_after_limit, isess.chat_id, person)):
        chat_reload.schedule_for_limit(isess.chat_id, person=person, session_id=session_id,
                                       old_sub=old_sub, in_place=True)


def rest_ending_for(session_id: str, *, since: float = 0.0):
    """The limit the account bound to the session rests on for it now, as a
    typed ending with the rest's end, or None: a hard rest of the whole
    account a window backs (the vendor's or the reading's, not the plain
    cooldown a refusal the reading cannot explain gets, nor an overload's
    brief nudge), else a rest of its model family. Only a rest that began
    after ``since`` counts: the engine answered the session since an older
    one. The pool's memory only (a terminal's one reading of its limit,
    ``limit_ending``, on the loop)."""
    from datetime import datetime, timezone

    from core.events import turn_ending
    from services.engines.pool_rests import (
        _scope_resting,
        _scoped_rests,
        _throttled_hard,
        _throttled_until,
        _window_rests,
    )
    from services.engines.pool_select import _scope_key
    from services.engines.pool_state import _session_models, _session_subscriptions

    def _ending(until: float, window: str):
        resets_at = datetime.fromtimestamp(until, timezone.utc).isoformat()
        return turn_ending.TurnEnding(reason=turn_ending.LIMIT, resets_at=resets_at, window=window)
    sub_id = _session_subscriptions.get(session_id)
    if not sub_id:
        return None
    until = _throttled_until.get(sub_id, 0.0)
    held = _window_rests.get(sub_id)
    if sub_id in _throttled_hard and held is not None and until > time.time() and held[1] > since:
        return _ending(until, held[0])
    ctx = _session_binding_ctx.get(session_id)
    family = _scope_key(ctx[0] if ctx else None, _session_models.get(session_id, ""))
    if family and _scope_resting(sub_id, family):
        family_until, rested_at = _scoped_rests[(sub_id, family)]
        if rested_at > since:
            return _ending(family_until, f"scoped:{family}")
    return None


def leave_account(chat_id: str, session_id: str, old_sub: str, *, fresh: bool) -> None:
    """After a limit, under the reload's hold: the closed session lets the rested account go, and a fresh start
    drops the conversation and seeds the chat's history (as ``wake_fresh`` does). Synchronous."""
    from storage import database as task_store
    from ws import wire_events as wire
    if get_session_subscription(session_id) == old_sub:
        release_subscription(session_id)
    if fresh:
        task_store.update_chat(chat_id, session_id=None, codex_thread_id=None,
                               pending_history_seed=wire.SEED_RESUME_FAILED)


async def stop_continuation(chat_id: str, person: str) -> bool:
    """A person's Stop (the dashboard's): the chat's continuation after a limit does not go on. Only someone who
    may drive the chat (its drive rule, and a live terminal's own). A continuation that waits is cancelled (its end
    clears the row and lets go of the queue, held until then so the Stop takes back the stopper's own messages only)
    and the chat is told. One whose waiter started the chat again is not cancelled half way: the Stop is recorded,
    the resume reads it last and writes the line, and a turn already running is the Stop's own. One queued in the
    chat's terminal is dropped as a person's send drops it. A reload the agent asked for is left. Whether a
    continuation was stopped."""
    from auth import roles
    from auth.providers import effective_role_of
    from core import background
    from core.events import input_queue
    from core.session import interactive_session
    from services.remote import chat_move, chat_reload
    from storage import database as task_store
    from storage.pg import run_db
    from ws import wire_events as wire
    waits = chat_reload.continuation_waits(chat_id)
    isess = interactive_session.find_live_for_chat(chat_id)
    queued = isess is not None and any(i.get("source") == chat_reload.LIMIT_SOURCE for i in list(isess._prompt_queue))
    # A terminal whose limit waits for an account to continue its turn.
    for_account = isess is not None and isess.limit_mark is not None and isess.limit_mark.waits_for_account
    chat = await run_db(task_store.get_chat, chat_id) if person and (waits or queued or for_account) else None
    role = await run_db(effective_role_of, person, chat.get("agent") or "") if chat else roles.NO_ACCESS
    if (role == roles.NO_ACCESS or (isess is not None and not isess.may_drive(person))
            or not chat_move.may_drive(chat or {}, chat_move.Asker(sub=person, role=role,
                                                                   platform_admin=roles.is_admin(role)))):
        return False
    # No await from here: the waiter's state is read with the act on it.
    task = chat_reload._waiters.get(chat_id)
    if task is not None and not task.done() and task in chat_reload._limit_waiters:
        if task in chat_reload._limit_started:
            chat_reload._limit_stopped.add(chat_id)
        else:
            task.cancel()
            if chat_reload._limit_waiters[task]:
                input_queue.hold_first_turn(chat_id)
            chat_reload.limit_turn_ended(chat_id)
            background.spawn(chat_reload._note(chat_id, chat_reload.LIMIT_STOPPED, wire.MOVED_RESUME_SKIPPED),
                             name="chat-limit-stopped", owner="pool_limits")
    if queued and isess is not None:
        chat_reload._limit_stopped.add(chat_id)   # its outcome writes the Stop's line
        isess.drop_limit_continuation()
    if for_account and isess is not None and isess.limit_mark is not None:
        isess.limit_mark.waits_for_account = False
    logger.info("Pool: chat=%s: a Stop ends its continuation after a limit", chat_id[:8])
    return True


async def moved_off(session_id: str, old_sub: str, within: float) -> bool:
    """Whether the session's binding left ``old_sub`` within ``within`` seconds (the rebalance's move after a
    limit, which takes the sign-in in place)."""
    from storage.pg import run_db
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        now = await run_db(get_session_subscription, session_id)
        if now and now != old_sub:
            return True
        await asyncio.sleep(1.0)
    return False


def limit_continues(session_id: str) -> bool:
    """Whether the turn a vendor's limit cut may continue on another account: an account the session's own
    acquisition would consider (the caller's own, the borrowable pool, the agent's pool) that the vendor does not
    refuse for the session's models, past its mark included (the rule after a refusal). False for a session the
    pool did not bind here (an outlived restart, a key frozen into its environment) and when nothing else serves.
    The move itself is the rebalance's, which the limit's rest already scheduled. Synchronous: the DB executor."""
    from services.engines.pool_select import _scope_facts
    sub_id = get_session_subscription(session_id)
    ctx = _session_binding_ctx.get(session_id)
    if not sub_id or sub_id == "default" or ctx is None:
        return False
    layer, scope_sub = ctx
    row = subscription_store.get_subscription(sub_id) or {}
    models = _scope_facts([session_id]).models
    candidates = _eligible_candidates(layer, scope_sub, str(row.get("provider") or ""))
    out = pool_moves.excluded(pool_moves.Cause.LIMIT, sub_id, candidates, layer, models)
    return any(c.get("id") not in out and pool_moves.accepts(pool_moves.Cause.LIMIT, c, models)
               for c in candidates)


def resume_refused(session_id: str, data: dict) -> bool:
    """The turn failed with a code the session's engine declares for a
    conversation another account cannot read (``AuthProfile.resume_refusal_codes``)."""
    from core.session.session_manager import get_layer_capabilities
    ctx = _session_binding_ctx.get(session_id)
    caps = get_layer_capabilities(ctx[0]) if ctx else None
    codes = caps.auth.resume_refusal_codes if caps else ()
    if not codes:
        return False
    try:
        said = json.dumps(data, default=str)
    except (TypeError, ValueError):
        said = str(data.get("message", ""))
    return any(code in said for code in codes)


#: What started a turn nobody watches that may continue after a limit
#: (``chat_rebuild.rebuild_and_resume``'s source): a delegated result, a
#: scheduled wake, the continue pass's own turn. Not a key's turn, the
#: agent's own move, a review or a monitor.
UNATTENDED_SOURCES = frozenset({"delegate_result", "delegate_result_replay", "schedule_wake", "continue"})


def continues_unattended(chat_id: str, session_id: str, driven_for: str) -> bool:
    """Whether a turn nobody watched may continue by itself, by the rules
    that decide it after a restart (``continue_pass.judge``), asked of a
    record made of the SESSION's own facts, never a caller's word: the
    person it runs as, their standing and role read now, the chat's kind,
    the one drive rule, the bounds in a row and per day. A session built for
    nobody or with no kind, an outside person's, and one whose person is not
    the one the turn was started for keep the card. A person's message that
    waits goes on too: the waiter lets it go first. Synchronous: the DB
    executor."""
    from datetime import datetime, timezone

    from core.session import session_state
    from core.session.external_identity import is_external_ctx
    from services.scheduler import continue_pass
    from storage import database as task_store
    from storage.chat import cut_records
    ctx = session_state.get_session_security(session_id)
    if ctx is None or is_external_ctx(ctx):
        return False
    facts = session_state.session_facts(session_id)
    person = (task_store.get_user_sub_by_username(facts.username) or "") if facts.username else ""
    if not person or person != driven_for:
        return False
    verdict = continue_pass.judge({
        "chat_id": chat_id, "kind": facts.kind, "person": person, "run_id": "",
        "cause": cut_records.CAUSE_LIMIT, "cut_at": datetime.now(timezone.utc).isoformat(),
    })
    return verdict.action == continue_pass.CONTINUE or (
        verdict.action == continue_pass.GIVE_WAY and verdict.queued)


async def after_limit(pump, ending, data: dict, old_sub: str | None, *,
                      within: float = LIMIT_ANSWER_S) -> bool:
    """The pump's question when an error ends a turn
    (``stream_pump._on_error_event``): does the turn go on, on another
    account, so that no card shows? A run's turn takes one next round of the
    run's own (``TaskRun._limit_round``). A person's own dashboard turn (never
    a call's, a voice conversation's, a worker's or a task's) continues once
    through the reload's wait (``chat_reload.schedule_for_limit``), its queue
    held for the waiter, and a turn whose new account cannot read the
    conversation starts fresh from the chat's history once. A turn the
    platform itself started in a person's chat with nobody on its pump (a
    delegated result, a wake: ``UNATTENDED_SOURCES``) continues through the
    same wait when the continue pass's rules allow it
    (``continues_unattended``), and is counted toward that pass's bounds
    when it goes out. A terminal's own (``TerminalLimitSide``) reads the limit
    mark without taking it (its row is a file the session can write) and holds
    no headless queue; ``waits_for_account`` tells it whether only an account
    was missing. Off the loop and bounded (``within``): no answer in time
    shows the card."""
    from core.events import turn_ending
    from core.session import session_kind
    from core.session.session_manager import get_layer_capabilities
    from services.remote import chat_reload
    from storage.pg import run_db
    terminal = bool(getattr(pump, "terminal", False))
    if not old_sub or pump.cut_by_platform:
        return False
    a_chats = (not pump.run_id and pump.source_type == session_kind.DASHBOARD.source_type
               and pump.turn_kind != session_kind.DUPLEX_TURN)
    chat_turn = a_chats and bool(pump.wake_person)
    # Nobody on the pump: the platform started the turn for a person.
    unattended = (a_chats and not pump.wake_person and bool(getattr(pump, "driven_for", ""))
                  and getattr(pump, "driven_by", "") in UNATTENDED_SOURCES)
    limited = ending is not None and ending.reason == turn_ending.LIMIT
    if limited:
        if pump.run_id and pump.limit_round_taken:
            return False
        # The mark first: the continuation's own turn (no person on its pump)
        # takes it, so the person's next turn may continue.
        if not pump.run_id and (chat_reload.limit_marked(pump.chat_id, take=not terminal)
                                or not (chat_turn or unattended)):
            return False
    elif not (chat_turn and not chat_reload.fresh_marked(pump.chat_id) and resume_refused(pump.session_id, data)):
        return False
    person = pump.wake_person or (pump.driven_for if unattended else "")

    def _answer() -> str:
        if pump.run_id:
            return _GOES_ON if not limited or limit_continues(pump.session_id) else _REFUSED
        if unattended and not continues_unattended(pump.chat_id, pump.session_id, pump.driven_for):
            return _REFUSED
        if not chat_reload.may_continue_after_limit(pump.chat_id, person):
            return _REFUSED
        return _NO_ACCOUNT if limited and not limit_continues(pump.session_id) else _GOES_ON

    try:
        answer = await asyncio.wait_for(run_db(_answer), within)
    except (TimeoutError, psycopg.Error):
        logger.info("Pool: chat=%s: no answer on another account in time, the limit card shows",
                    pump.chat_id[:8])
        return False
    pump.waits_for_account = answer == _NO_ACCOUNT
    goes_on = answer == _GOES_ON
    if goes_on and not pump.run_id:
        ctx = _session_binding_ctx.get(pump.session_id)
        caps = get_layer_capabilities(ctx[0]) if ctx else None
        # The chat's queue is held for the waiter from here to its claim: a
        # message that waited goes first, on the new account. A limit while a
        # continuation already waits keeps its card; a waiting reload
        # continues the chat itself.
        # A turn nobody watched is counted with the continue pass's own
        # when its continuation goes out (``chat_reload._resume``).
        if (not chat_reload.schedule_for_limit(
                pump.chat_id, person=person, session_id=pump.session_id, old_sub=old_sub,
                in_place=limited and bool(caps and caps.auth.sign_in_in_place), fresh=not limited,
                holds_queue=not terminal, unattended=unattended)
                and chat_reload.continuation_waits(pump.chat_id)):
            return False
    return goes_on
