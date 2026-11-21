"""A turn a usage limit cut continues once by itself on another account.

The pump asks the pool (``pool_limits.limit_continues``) when a limit ends a
turn: another account that the vendor does not refuse can serve the session's
model. When one can, the chat shows no limit card and keeps its queue, and one
continuation of the platform's words follows through the reload's entrance
(``chat_reload.schedule_for_limit``): after the turn's end, on the moved
session (Claude) or on one started again on the pool's pick (Codex). A run
takes one next round instead (``TaskRun._limit_round``). A terminal gets the
same continuation typed through its queue, never over text waiting in its
input box. Never twice, and the person's own message wins.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.events import input_queue, stream_pump, turn_ending
from core.events.common_events import ERROR, CommonEvent, TurnInput
from core.session.interactive_queue import ACCOUNT_LIMIT_SOURCE, InteractiveQueueMixin
from services.engines import pool_limits, pool_moves, subscription_pool as sp
from services.remote import chat_reload
from tests._pool_store import pool_store

_REAL_RESUME = chat_reload._resume
_LIMIT = turn_ending.TurnEnding(reason=turn_ending.LIMIT, resets_at="2099-01-01T00:00:00+00:00",
                                detail="You've hit your limit", window="five_hour")


@pytest.fixture(autouse=True)
def _clean():
    chat_reload._limit_marks.clear()
    yield
    chat_reload._limit_marks.clear()
    chat_reload._fresh_marks.clear()
    chat_reload._limit_stopped.clear()
    input_queue._first_turns.clear()
    for d in (sp._session_subscriptions, sp._session_binding_ctx, sp._session_scope_keys,
              sp._throttled_until, sp._throttled_hard, sp._window_rests, sp._scoped_rests, sp._session_bound_at):
        d.clear()


# ---------------------------------------------------------------------------
# The pool's answer
# ---------------------------------------------------------------------------

def _row(sid, layer="codex-cli", owner="", auth="oauth"):
    return {"id": sid, "layer": layer, "provider": "openai" if layer == "codex-cli" else "anthropic",
            "auth_type": auth, "owner_sub": owner, "is_primary": 0, "status": "active", "active_sessions": 0}


def _bind(sid="s1", sub="sub-a", layer="codex-cli", scope_sub=""):
    sp._session_subscriptions[sid] = sub
    sp._session_binding_ctx[sid] = (layer, scope_sub)


@pool_store()
def test_another_account_the_vendor_does_not_refuse_takes_the_turn_on(mock_store):
    rows = {"sub-a": _row("sub-a"), "sub-b": _row("sub-b")}
    mock_store.list_platform_pool.return_value = list(rows.values())
    mock_store.get_subscription.side_effect = rows.get
    _bind()
    sp._throttled_until["sub-a"] = time.time() + 3600
    sp._throttled_hard.add("sub-a")
    with patch("storage.database.get_chat_by_session", return_value={"id": "c1", "model": ""}):
        assert pool_limits.limit_continues("s1")
        # Not resting, but the vendor's own reading says it refuses: no account to continue on.
        now = datetime.now(timezone.utc)
        full = {"observed_at": now.isoformat(), "source": "poll", "five_hour_pct": 100.0,
                "five_hour_resets_at": (now + timedelta(hours=2)).isoformat(), "seven_day_pct": 50.0,
                "seven_day_resets_at": (now + timedelta(days=2)).isoformat(),
                "data": {"scoped": [], "reached": "five_hour", "plan": ""}}
        mock_store.latest_window_samples.side_effect = lambda ids, source=None: {
            i: full for i in ids if i == "sub-b"}
        with patch("services.engines.subscription_windows.is_enabled", return_value=True):
            assert not pool_limits.limit_continues("s1")
        mock_store.latest_window_samples.side_effect = lambda ids, source=None: {}
        sp._throttled_until["sub-b"] = time.time() + 3600
        sp._throttled_hard.add("sub-b")
        assert not pool_limits.limit_continues("s1")        # every other account refused too
        assert not pool_limits.limit_continues("s-unbound")


@pool_store()
def test_a_persons_session_never_continues_on_an_admins_sign_in(mock_store):
    admin = _row("sub-admin", owner="admin")
    mock_store.list_personal.return_value = [_row("sub-own", owner="alice")]
    mock_store.list_platform_pool.return_value = [admin]
    mock_store.get_user_allow_platform_auth.return_value = True
    mock_store.get_subscription.side_effect = {"sub-own": _row("sub-own", owner="alice"), "sub-admin": admin}.get
    _bind(sub="sub-own", scope_sub="alice")
    sp._throttled_until["sub-own"] = time.time() + 3600
    sp._throttled_hard.add("sub-own")
    with patch("storage.database.get_chat_by_session", return_value={"id": "c1", "model": ""}):
        assert not pool_limits.limit_continues("s1")


# ---------------------------------------------------------------------------
# The pump's question (``pool_limits.after_limit``)
# ---------------------------------------------------------------------------

async def _pump_on_limit(temp_db, monkeypatch, chat_id, *, continues: bool, run_id: str = "",
                         person: str = "user-admin", layer: str = "codex-cli", taken: bool = False,
                         data: dict | None = None, queued: tuple[str, ...] = (),
                         driven: tuple[str, str] = ("", ""), **pump_kw):
    scheduled: list[dict] = []
    monkeypatch.setattr(sp, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "rest_after_limit", lambda sid, ending, msg: None)
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: continues)
    monkeypatch.setattr(chat_reload, "schedule_for_limit",
                        lambda cid, **kw: scheduled.append({"chat": cid, **kw}) or True)
    sp._session_binding_ctx[f"sess-{chat_id}"] = (layer, "")
    temp_db.create_chat(chat_id, "user-admin", "a1")
    for text in queued:
        q = await input_queue.loaded(chat_id)
        await q.add(f"q-{len(q.items)}", "user-admin", TurnInput(text=text))
    eq: asyncio.Queue = asyncio.Queue()
    await eq.put(CommonEvent(type=ERROR, data=data or {"message": _LIMIT.line(), "ending": _LIMIT.as_dict()}))
    pump = stream_pump.ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}",
        producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
        event_queue=eq, perm_queue=None, **pump_kw,
    )
    pump.wake_person = person
    pump.driven_for, pump.driven_by = driven
    pump.run_id = run_id
    pump.limit_round_taken = taken
    # Registered as the dashboard's turn registers it: the pump's end runs the queue's hand-off.
    stream_pump._active_pumps[chat_id] = pump
    await asyncio.wait_for(pump.start(), 10)
    for _ in range(20):    # the hand-off runs in a task of its own
        await asyncio.sleep(0.05)
    rows = temp_db.get_chat_messages(chat_id)
    cards = [r for r in rows if "turn_ended" in (r.get("event_data") or "")]
    return pump, scheduled, cards


@pytest.mark.asyncio
async def test_a_limit_with_another_account_shows_no_card_and_continues_once(temp_db, monkeypatch):
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-c1", continues=True)
    assert pump.limit_continues and cards == []
    assert scheduled == [{"chat": "lim-c1", "person": "user-admin", "session_id": "sess-lim-c1",
                          "old_sub": "sub-a", "in_place": False, "fresh": False, "holds_queue": True,
                          "unattended": False}]


@pytest.mark.asyncio
async def test_claude_continues_on_its_moved_session(temp_db, monkeypatch):
    _pump, scheduled, _cards = await _pump_on_limit(temp_db, monkeypatch, "lim-c2", continues=True,
                                                    layer="claude-code-cli")
    assert scheduled[0]["in_place"] is True


@pytest.mark.asyncio
async def test_with_no_other_account_the_limit_card_shows_as_before(temp_db, monkeypatch):
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-c3", continues=False)
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_the_continuations_own_limit_gets_the_card(temp_db, monkeypatch):
    chat_reload._limit_marks["lim-c4"] = time.monotonic()
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-c4", continues=True)
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1
    # Taken once: the limit after that card may continue again.
    assert not chat_reload.limit_marked("lim-c4")


@pytest.mark.asyncio
async def test_a_runs_turn_continues_in_its_own_round_once(temp_db, monkeypatch):
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-r1", continues=True,
                                                  run_id="run-1", person="")
    assert pump.limit_continues and scheduled == [] and cards == []
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-r2", continues=True,
                                                  run_id="run-1", person="", taken=True)
    assert not pump.limit_continues and len(cards) == 1


@pytest.mark.asyncio
async def test_a_call_in_a_chat_gets_the_card_and_no_text_turn(temp_db, monkeypatch):
    from core.session import session_kind
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-call", continues=True,
                                                  source_type=session_kind.PHONE.source_type)
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_a_voice_turn_gets_the_card_and_no_text_turn(temp_db, monkeypatch):
    from core.session import session_kind
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-voice", continues=True,
                                                  turn_kind=session_kind.DUPLEX_TURN)
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_a_chat_the_reload_refuses_keeps_its_card(temp_db, monkeypatch):
    """A person's own turn in a delegated worker chat: the reload's rule, the card and no continuation."""
    monkeypatch.setattr(chat_reload, "_refusal", lambda chat, asker: chat_reload.DELEGATED_WORK)
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-work", continues=True)
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_a_conversation_the_new_account_cannot_read_starts_fresh_once(temp_db, monkeypatch):
    data = {"message": "Your request was invalid: invalid_encrypted_content"}
    _pump, scheduled, _cards = await _pump_on_limit(temp_db, monkeypatch, "lim-f1", continues=False, data=data)
    assert scheduled and scheduled[0]["fresh"] is True and scheduled[0]["in_place"] is False
    chat_reload._fresh_marks["lim-f2"] = time.monotonic()
    _pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-f2", continues=False, data=data)
    assert scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_codex_starts_fresh_when_the_continuations_own_turn_is_refused(temp_db, monkeypatch):
    """The continuation's first turn on the new account is where Codex refuses the earlier reasoning."""
    chat_reload._limit_marks["lim-f3"] = time.monotonic()
    data = {"message": "Your request was invalid: invalid_encrypted_content"}
    _pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "lim-f3", continues=False, data=data)
    assert scheduled and scheduled[0]["fresh"] is True and cards == []


# ---------------------------------------------------------------------------
# The reload's one waiter, with a limit
# ---------------------------------------------------------------------------

class _Queue:
    def __init__(self):
        self.released = 0

    def release(self, claim, rearm=False):
        self.released += 1


def _waiter_world(monkeypatch, *, chat: dict, waiting_message=False, open_turn=False, bound=0, claims=None,
                  role="editor", since=0):
    from core.events import input_queue
    from core.session import chat_rebuild
    from storage import remote_store
    world = SimpleNamespace(notes=[], closed=[], continued=[], released=[], queue=_Queue())

    async def _claim(cid, timeout):
        return claims.pop(0) if claims else object()

    async def _loaded(cid):
        return world.queue

    class _Closed:
        def __init__(self, cid, **kw):
            if kw.get("hold") is not None and kw["hold"]():
                raise chat_rebuild.ChatBusy(cid)
            world.closed.append(kw["reason"])
            self.chat = chat

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _note(cid, message, reason="", **kw):
        world.notes.append((message, reason))

    async def _resume(chat_, asker, reason, switched=None, *, limit=None, terminal=False):
        world.continued.append((chat_["id"], asker.sub, terminal))

    monkeypatch.setattr(input_queue, "claim_when_free", _claim)
    monkeypatch.setattr(input_queue, "loaded", _loaded)
    monkeypatch.setattr(input_queue, "deleting", lambda cid: False)
    monkeypatch.setattr(chat_rebuild, "closed", _Closed)
    monkeypatch.setattr(chat_reload, "_note", _note)
    monkeypatch.setattr(chat_reload, "_account_note", lambda cid, m: _note(cid, m, "account_changed"))
    monkeypatch.setattr(pool_moves, "note", lambda cid, m, waits=False: world.notes.append((m, "waits")))
    monkeypatch.setattr(chat_reload, "_resume", _resume)
    monkeypatch.setattr(chat_reload, "_watch_terminal", lambda cid: None)
    monkeypatch.setattr(chat_reload, "_refusal", lambda c, a: "")
    monkeypatch.setattr(chat_reload, "_away_machine", lambda c: "")
    monkeypatch.setattr("storage.database.get_chat", lambda cid: chat)
    monkeypatch.setattr("auth.providers.effective_role_of", lambda sub, agent: role)
    monkeypatch.setattr(chat_reload.chat_move, "open_turn", lambda c: open_turn)
    monkeypatch.setattr(chat_reload.chat_move, "_waiting_message_goes_first", lambda c: waiting_message)
    # A continuation's bound counts the platform's own turns, never the agent's reloads.
    monkeypatch.setattr(remote_store, "reloads_since_person", lambda cid: 0)
    monkeypatch.setattr(remote_store, "resumes_since_person", lambda cid: bound)
    monkeypatch.setattr(remote_store, "newest_person_row", lambda cid: since)
    monkeypatch.setattr(chat_reload, "_is_terminal_chat", lambda c: bool(c.get("execution_mode")))
    # The rested account is let go by the pool's own piece (``pool_limits.leave_account``).
    monkeypatch.setattr(pool_limits, "get_session_subscription", lambda sid: "sub-a")
    monkeypatch.setattr(pool_limits, "release_subscription", world.released.append)
    return world


def _continue_after(cid, *, in_place=False, fresh=False, session="s", wait_s=60.0, told=False):
    asker = chat_reload.chat_move.Asker(sub="alice", role="", platform_admin=False)
    limit = chat_reload._Limit(session, "sub-a", in_place, fresh, told=told)

    async def _go():   # as the waiter runs it: begun, then the wait
        begun = await chat_reload._limit_begins(cid, limit)
        await chat_reload._wait_and_reload(cid, asker, "", {}, ("", 0), None, wait_s, limit=begun)
    asyncio.run(_go())


def test_a_codex_chat_is_closed_and_continues_once_on_the_pools_pick(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-x", "agent": "a1", "session_id": "s-x"})
    _continue_after("c-x", session="s-x")
    assert world.closed == ["account"] and world.released == ["s-x"]
    assert world.continued == [("c-x", "alice", False)]
    assert world.notes == [(chat_reload.LIMIT_LINE, "account_changed")]


def test_a_codex_chats_waiting_message_starts_it_on_the_pools_pick(monkeypatch):
    """The session is closed and the refused account let go first: the person's message is the turn on the
    new account, and no continuation follows it."""
    world = _waiter_world(monkeypatch, chat={"id": "c-y", "agent": "a1"}, waiting_message=True)
    _continue_after("c-y", session="s-y")
    assert world.closed == ["account"] and world.released == ["s-y"] and world.continued == []


def test_the_reloads_bounds_hold_the_continuation_in_its_own_words(monkeypatch):
    """Ten continuations in a row with no message from a person stop the chain, in the continuation's words."""
    world = _waiter_world(monkeypatch, chat={"id": "c-b", "agent": "a1"}, bound=chat_reload.LIMIT_CHAIN_MAX)
    _continue_after("c-b")
    assert world.continued == []
    assert world.notes[-1] == (chat_reload._bound("c-b", chat_reload.LIMIT_CHAIN_MAX, limit=True), "resume_stopped")
    assert world.notes[-1][0] == ("This chat changed account 10 times in a row with no message from you, "
                                  "so it stops here. Send a message to continue.")


def test_a_long_unattended_run_continues_past_the_agents_own_bound(monkeypatch):
    """Nine continuations in a row (one real limit per account through a night) still continue; the agent's own
    reload keeps its three."""
    world = _waiter_world(monkeypatch, chat={"id": "c-n", "agent": "a1"}, bound=chat_reload.LIMIT_CHAIN_MAX - 1)
    _continue_after("c-n")
    assert world.continued == [("c-n", "alice", False)]
    assert chat_reload._bound("c-n", chat_reload.CHAIN_MAX, limit=False).startswith(
        "The agent reloaded this chat 3 times in a row")


def test_the_reloads_refusal_holds_the_continuation(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-r", "agent": "a1"})
    monkeypatch.setattr(chat_reload, "_refusal", lambda c, a: chat_reload.A_CALL)
    _continue_after("c-r")
    assert world.continued == [] and world.closed == []
    assert world.notes[-1] == (chat_reload.LIMIT_FAILED, "failed")


def test_a_voice_conversation_holds_the_continuation(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-v", "agent": "a1"})
    monkeypatch.setattr(chat_reload, "voice_holds", [lambda cid: chat_reload.VOICE_IN_USE])
    asker = chat_reload.chat_move.Asker(sub="alice", role="", platform_admin=False)
    limit = chat_reload._Limit("s", "sub-a", False)
    asyncio.run(chat_reload._wait_and_reload("c-v", asker, "", {}, ("", 0), chat_reload._voice_holds, 60.0,
                                             limit=limit))
    assert world.continued == [] and world.notes[-1] == (chat_reload.LIMIT_FAILED, "failed")


def test_an_offline_machine_says_so(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-o", "agent": "a1"})
    monkeypatch.setattr(chat_reload, "_away_machine", lambda c: "the laptop")
    _continue_after("c-o")
    assert world.continued == []
    assert world.notes[-1][0].startswith("The chat did not continue on another account: the laptop is offline.")


def test_silent_ends_are_told(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-s", "agent": "a1"}, role="")
    _continue_after("c-s")
    assert world.notes[-1] == (chat_reload.LIMIT_FAILED, "failed")
    world = _waiter_world(monkeypatch, chat={"id": "c-t", "agent": "a1"}, claims=[None])
    _continue_after("c-t", wait_s=0.01)
    assert world.notes[-1] == (chat_reload.LIMIT_FAILED, "failed")


def test_claude_continues_on_its_session_once_the_move_landed(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-z", "agent": "a1"})
    bound = iter(["sub-a", "sub-a", "sub-b"])
    monkeypatch.setattr(pool_limits, "get_session_subscription", lambda sid: next(bound))
    monkeypatch.setattr(chat_reload.asyncio, "sleep", AsyncMock())
    _continue_after("c-z", in_place=True)
    assert world.closed == [] and world.continued == [("c-z", "alice", False)]
    assert world.notes == []                      # the move's own line says it (pool_moves.line)


def test_claude_keeps_the_claim_through_the_move_and_a_waiting_message_goes_first(monkeypatch):
    """In place: the claim is held while the move lands, so a person's message never starts on the account
    that still rests; once moved, the message that waits is the turn and nothing follows it."""
    world = _waiter_world(monkeypatch, chat={"id": "c-m", "agent": "a1"}, waiting_message=True)
    released_at_move: list[int] = []

    async def _moved(sid, old, within):
        released_at_move.append(world.queue.released)
        return True
    monkeypatch.setattr(pool_limits, "moved_off", _moved)
    _continue_after("c-m", in_place=True)
    assert released_at_move == [0]                  # the claim was still held during the wait
    assert world.continued == [] and world.queue.released == 1


def test_claude_with_no_move_does_not_continue(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "c-n", "agent": "a1"})
    asked: list[str] = []
    monkeypatch.setattr(pool_limits, "get_session_subscription", lambda sid: asked.append(sid) or "sub-a")
    monkeypatch.setattr(chat_reload, "LIMIT_MOVE_WAIT_S", 0.05)
    monkeypatch.setattr(chat_reload.asyncio, "sleep", AsyncMock())
    _continue_after("c-n", in_place=True)
    assert asked                                  # the binding was read, and never left sub-a
    assert world.continued == [] and world.notes[-1] == (chat_reload.LIMIT_NOT_MOVED, "resume_skipped")


def test_a_codex_terminal_is_never_closed_over_its_text_and_tells_once(monkeypatch):
    chat = {"id": "c-t", "agent": "a1", "execution_mode": "interactive"}
    world = _waiter_world(monkeypatch, chat=chat)
    term = SimpleNamespace(holds_unsent_text=True, submit_settling=False)
    polls = {"n": 0}

    async def _tick(_s):
        polls["n"] += 1
        if polls["n"] == 4:
            # The Enter: the box empties and its turn is not seen yet.
            term.holds_unsent_text, term.submit_settling = False, True
        if polls["n"] >= 6:
            term.submit_settling = False
    monkeypatch.setattr(chat_reload.asyncio, "sleep", _tick)
    monkeypatch.setattr(pool_moves, "TERMINAL_HOLD_NOTICE_S", 0.0)
    monkeypatch.setattr("core.session.interactive_session.find_live_for_chat", lambda cid: term)
    _continue_after("c-t")
    assert [n for n in world.notes if n[1] == "waits"] == [(chat_reload.LIMIT_HELD, "waits")]
    assert polls["n"] >= 6                       # still held through the submit's tails
    assert world.closed == ["account"] and world.continued == [("c-t", "alice", False)]


def test_a_fresh_start_drops_the_conversation_and_seeds_the_history(monkeypatch):
    chat = {"id": "c-f", "agent": "a1", "session_id": "s-f"}
    world = _waiter_world(monkeypatch, chat=chat)
    updates: list[dict] = []
    monkeypatch.setattr("storage.database.update_chat", lambda cid, **kw: updates.append(kw))
    _continue_after("c-f", session="s-f", fresh=True)
    assert updates == [{"session_id": None, "codex_thread_id": None, "pending_history_seed": "resume_failed"}]
    assert world.notes[0] == (chat_reload.LIMIT_FRESH_LINE, "account_changed")
    assert world.continued == [("c-f", "alice", False)]


def test_a_continuation_marks_its_turn_and_a_clean_end_or_the_persons_send_clears_it():
    chat_reload._limit_marks["c-m"] = time.monotonic()
    chat_reload._fresh_marks["c-m"] = time.monotonic()
    chat_reload.limit_turn_ended("c-m")
    assert not chat_reload.limit_marked("c-m") and not chat_reload.fresh_marked("c-m")
    chat_reload._limit_marks["c-m"] = time.monotonic()
    from core.session import interactive_queue
    assert chat_reload.limit_turn_ended in interactive_queue.person_sent_hooks
    for hook in interactive_queue.person_sent_hooks:
        hook("c-m")
    assert not chat_reload.limit_marked("c-m")
    chat_reload._limit_marks["c-m"] = time.monotonic() - chat_reload.LIMIT_MARK_S - 1
    assert not chat_reload.limit_marked("c-m")
    chat_reload._limit_marks["c-m"] = time.monotonic()
    assert chat_reload.limit_marked("c-m", take=False) and chat_reload.limit_marked("c-m", take=False)


def test_the_continuation_replaces_the_accounts_own_waiting_close(monkeypatch):
    """The rebalance may have asked the chat's account close first: the continuation closes and starts the
    session itself, so that closer goes. A close for another reason stays."""
    async def _never(*a, **kw):
        await asyncio.sleep(3600)

    async def _run():
        monkeypatch.setattr(chat_reload, "_wait_and_reload", _never)
        monkeypatch.setattr("storage.chat.pending_rebuilds.save", lambda *a, **kw: None)
        account = asyncio.ensure_future(asyncio.sleep(3600))
        chat_reload._idle_closes["c-r"], chat_reload._idle_close_reasons["c-r"] = account, "account"
        team = asyncio.ensure_future(asyncio.sleep(3600))
        chat_reload._idle_closes["c-s"], chat_reload._idle_close_reasons["c-s"] = team, "team-account"
        chat_reload.schedule_for_limit("c-r", person="alice", session_id="s-r", old_sub="sub-a", in_place=False)
        chat_reload.schedule_for_limit("c-s", person="alice", session_id="s-s", old_sub="sub-a", in_place=False)
        await asyncio.sleep(0)
        assert account.cancelled() and "c-r" not in chat_reload._idle_closes
        assert not team.cancelled() and chat_reload._idle_closes["c-s"] is team
        assert chat_reload.waiting("c-r") and chat_reload.waiting("c-s")
        for cid in ("c-r", "c-s"):
            chat_reload._waiters.pop(cid).cancel()
        chat_reload._idle_closes.pop("c-s").cancel()
        chat_reload._idle_close_reasons.clear()
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_a_limit_keeps_the_agents_waiting_reload():
    async def _run():
        reload_waiter = asyncio.ensure_future(asyncio.sleep(3600))
        chat_reload._waiters["c-rel"] = reload_waiter
        scheduled = chat_reload.schedule_for_limit("c-rel", person="alice", session_id="s", old_sub="sub-a",
                                                   in_place=True)
        await asyncio.sleep(0)
        kept = not reload_waiter.cancelled() and chat_reload._waiters["c-rel"] is reload_waiter
        chat_reload._waiters.pop("c-rel").cancel()
        await asyncio.sleep(0)
        return scheduled, kept
    assert asyncio.run(_run()) == (False, True)


def test_closed_keeps_a_session_its_caller_holds(monkeypatch):
    """The reload's hold reads its caller's own reason with the idle check, no await before the close."""
    from core.events import input_queue
    from core.session import chat_rebuild, warmup_registry
    closes: list[str] = []
    released: list[object] = []

    class _Q:
        def try_claim(self):
            return object()

        def release(self, claim, rearm=False):
            released.append(claim)

    async def _loaded(cid):
        return _Q()

    async def _register(cid, by, agent):
        return object()

    async def _unregister(cid):
        return None

    async def _close(chat, **kw):
        closes.append(chat["id"])
        return True
    monkeypatch.setattr(input_queue, "loaded", _loaded)
    monkeypatch.setattr(warmup_registry, "register_new", _register)
    monkeypatch.setattr(warmup_registry, "unregister", _unregister)
    monkeypatch.setattr("storage.database.get_chat", lambda cid: {"id": cid, "agent": "a1"})
    monkeypatch.setattr(chat_rebuild, "close_chat_session", _close)

    async def _run(hold):
        async with chat_rebuild.closed("c-h", reason="account", by="alice", hold=hold):
            pass
    with pytest.raises(chat_rebuild.ChatBusy):
        asyncio.run(_run(lambda: True))
    assert closes == [] and len(released) == 1
    asyncio.run(_run(lambda: False))
    assert closes == ["c-h"]


def test_a_waiting_continuation_keeps_the_accounts_own_close_away():
    closes: list[str] = []
    with patch("services.engines.token_fanout.on_loop", lambda cb: cb() or True), \
            patch.object(chat_reload, "waiting", lambda cid: cid == "c-w"), \
            patch.object(chat_reload, "close_when_idle", lambda cid, **kw: closes.append(cid)):
        pool_moves.restart_when_idle(["c-w", "c-o"], "line")
    assert closes == ["c-o"]


# ---------------------------------------------------------------------------
# The one resume, with a limit
# ---------------------------------------------------------------------------

def _resume_world(monkeypatch, *, chat=None, since_now=0):
    from core.session import chat_rebuild
    from storage import remote_store
    world = SimpleNamespace(started=[], rows=[])
    chat = chat or {"id": "c-res", "agent": "a1", "session_id": "s-res"}

    async def _rebuild(cid, text, **kw):
        world.started.append((cid, text, kw["source"], kw["allow_pty"]))
    monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _rebuild)
    monkeypatch.setattr(chat_rebuild, "chat_mode", lambda c: "auto")
    monkeypatch.setattr("storage.database.get_chat", lambda cid: chat)
    monkeypatch.setattr(remote_store, "newest_person_row", lambda cid: since_now)
    monkeypatch.setattr(chat_reload.chat_move, "_waiting_message_goes_first", lambda c: False)
    return world


def _asker():
    return chat_reload.chat_move.Asker(sub="alice", role="editor", platform_admin=False)


def test_the_continuation_waits_out_a_persons_turn(monkeypatch):
    from core.events.stream_pump import _active_pumps
    world = _resume_world(monkeypatch)
    _active_pumps["c-res"] = SimpleNamespace(is_done=False)   # the person's own turn runs now
    try:
        asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "",
                                        limit=chat_reload._Limit("s-res", "sub-a", False, since=0)))
    finally:
        _active_pumps.pop("c-res", None)
    assert world.started == [] and "c-res" not in chat_reload._limit_marks


def test_the_continuation_waits_out_the_terminals_open_turn(monkeypatch):
    """In place on a live terminal: the turn open in it (the person's) holds the chat, nothing is queued."""
    world = _resume_world(monkeypatch)
    live = SimpleNamespace(turn_open=True, submit_settling=False)
    monkeypatch.setattr("core.session.interactive_session.find_live_for_chat", lambda cid: live)
    limit = chat_reload._Limit("s-res", "sub-a", True, since=0)
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "", limit=limit, terminal=True))
    assert world.started == [] and "c-res" not in chat_reload._limit_marks
    live.turn_open = False
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "", limit=limit, terminal=True))
    assert world.started == [("c-res", chat_reload.LIMIT_NUDGE, chat_reload.LIMIT_SOURCE, True)]


def test_a_persons_message_since_the_limit_wins(monkeypatch):
    world = _resume_world(monkeypatch, since_now=42)
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "",
                                    limit=chat_reload._Limit("s-res", "sub-a", False, since=41)))
    assert world.started == []


def test_a_stopped_turn_stays_stopped(monkeypatch):
    world = _resume_world(monkeypatch, chat={"id": "c-res", "agent": "a1", "last_turn_aborted": 1})
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "",
                                    limit=chat_reload._Limit("s-res", "sub-a", False, since=0)))
    assert world.started == []


@pytest.mark.asyncio
async def test_the_real_continuation_marks_its_turn_and_a_limit_in_it_gets_the_card(monkeypatch):
    """Through the path: the resume starts the platform's turn and marks it, and a limit in that turn is
    answered with the card, never a second continuation."""
    world = _resume_world(monkeypatch)
    await chat_reload._resume({"id": "c-res"}, _asker(), "",
                              limit=chat_reload._Limit("s-res", "sub-a", False, since=0))
    assert world.started == [("c-res", chat_reload.LIMIT_NUDGE, chat_reload.LIMIT_SOURCE, False)]
    assert chat_reload.limit_marked("c-res", take=False)
    scheduled: list[str] = []
    monkeypatch.setattr(chat_reload, "schedule_for_limit", lambda cid, **kw: scheduled.append(cid))
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: True)
    # Another account serves and the reload would accept the chat: only the mark answers.
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    from core.session import session_kind
    pump = SimpleNamespace(cut_by_platform=False, run_id="", wake_person="alice", chat_id="c-res",
                           session_id="s-res", source_type=session_kind.DASHBOARD.source_type, turn_kind="",
                           limit_round_taken=False)
    assert await pool_limits.after_limit(pump, _LIMIT, {}, "sub-b") is False
    assert scheduled == []


def test_the_fresh_start_says_its_own_words(monkeypatch):
    world = _resume_world(monkeypatch)
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "",
                                    limit=chat_reload._Limit("s-res", "sub-a", False, fresh=True, since=0)))
    assert world.started[0][1] == chat_reload.FRESH_NUDGE and chat_reload.fresh_marked("c-res")


# ---------------------------------------------------------------------------
# The restart (``chat_pending_rebuilds``, ``LIMIT``)
# ---------------------------------------------------------------------------

def test_the_waiting_continuation_writes_its_row_and_never_over_a_reloads(monkeypatch):
    saved: list[tuple] = []

    async def _never(*a, **kw):
        await asyncio.sleep(3600)

    async def _run():
        monkeypatch.setattr(chat_reload, "_wait_and_reload", _never)
        monkeypatch.setattr("storage.chat.pending_rebuilds.save",
                            lambda cid, what, **kw: saved.append((cid, what, kw)))
        monkeypatch.setattr("storage.remote_store.newest_person_row", lambda cid: 7)
        chat_reload.schedule_for_limit("c-row", person="alice", session_id="s-row", old_sub="sub-a",
                                       in_place=True)
        await asyncio.sleep(0.05)
        chat_reload._waiters.pop("c-row").cancel()
        await asyncio.sleep(0)

    asyncio.run(_run())
    cid, what, kw = saved[0]
    assert (cid, what, kw["person"], kw["replace"]) == ("c-row", "limit", "alice", False)
    # The row carries when the limit came (the newest person row), so a restart measures from it.
    assert chat_reload._Limit.from_target(kw["target"]) == chat_reload._Limit(
        "s-row", "sub-a", True, since=7, told=True)


def test_a_limit_row_never_replaces_another_kinds_row(temp_db):
    from storage.chat import pending_rebuilds
    temp_db.create_chat("c-pr", "user-admin", "a1")
    pending_rebuilds.save("c-pr", pending_rebuilds.RELOAD, person="u", wait_s=60, reason="r")
    pending_rebuilds.save("c-pr", pending_rebuilds.LIMIT, person="u", wait_s=60, target="{}", replace=False)
    assert pending_rebuilds.take("c-pr")["what"] == "reload"
    pending_rebuilds.save("c-pr", pending_rebuilds.LIMIT, person="u", wait_s=60, target="{}", replace=False)
    assert pending_rebuilds.take("c-pr")["what"] == "limit"


def _limit_row(left_s: float = 600) -> dict:
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=left_s)).isoformat()
    target = chat_reload._Limit("s-old", "sub-a", False, fresh=False, since=7).target()
    return {"chat_id": "c-rs", "what": "limit", "person": "alice", "target": target, "choices": {},
            "deadline": deadline}


def _settle(monkeypatch, row, **kw):
    notes: list[str] = []
    armed: list[dict] = []

    async def _note(cid, message, reason="", **k):
        notes.append(message)
    monkeypatch.setattr(chat_reload, "_note", _note)
    monkeypatch.setattr("storage.database.get_chat", lambda cid: {"id": cid, "agent": "a1"})
    monkeypatch.setattr("auth.providers.effective_role_of", lambda sub, agent: "editor")
    monkeypatch.setattr(chat_reload, "schedule_for_limit", lambda cid, **k: armed.append({"chat": cid, **k}) or True)
    asyncio.run(chat_reload.settle_after_restart(row, **kw))
    return notes, armed


def test_a_continuation_waits_again_when_its_chat_lives_on(monkeypatch):
    notes, armed = _settle(monkeypatch, _limit_row(), continued=False, lives_on=True)
    assert notes == [] and armed[0]["chat"] == "c-rs" and armed[0]["told"] is True
    assert armed[0]["session_id"] == "s-old" and armed[0]["in_place"] is False and armed[0]["wait_s"] > 500
    # When the limit came is kept, and a headless chat's queue is held again for the waiter.
    assert armed[0]["since"] == 7 and armed[0]["holds_queue"] is True


def test_a_continuation_the_restart_ended_is_told(monkeypatch):
    notes, armed = _settle(monkeypatch, _limit_row(), continued=False, lives_on=False)
    assert notes == [chat_reload.LIMIT_RESTARTED] and armed == []
    notes, armed = _settle(monkeypatch, _limit_row(left_s=-1), continued=False, lives_on=True)
    assert notes == [chat_reload.LIMIT_RESTARTED] and armed == []


def test_a_chat_the_platform_continues_needs_no_continuation(monkeypatch):
    notes, armed = _settle(monkeypatch, _limit_row(), continued=True)
    assert notes == [] and armed == []


# ---------------------------------------------------------------------------
# A terminal
# ---------------------------------------------------------------------------

def _terminal(dirty: bool, dirty_at: float):
    term = type("_T", (InteractiveQueueMixin,), {})()
    from collections import deque
    term.alive, term._closing, term._closed, term._ready = True, False, False, True
    term._submit_settle_handle = term._submit_max_handle = term._submit_recheck_handle = None
    term._turn_open = False
    term._composer_dirty, term._composer_dirty_at = dirty, dirty_at
    term.created_at = 0.0
    term._question_parked = False
    term.session_id = "s-term"
    term.chat_id = "c-term"
    term._prompt_queue = deque()
    term._satellite_inject = None
    term.limit_mark, term._limit_menu, term._served_at = None, False, 0.0
    type(term).quiet_seconds = property(lambda self: 999.0)
    return term


def _driven(term, written: list):
    """The terminal's own drain, with only its writer and the steps after a
    write stood in."""
    term._draining_prompts = False
    term.otodock_attached = False
    type(term).is_remote = property(lambda self: True)   # no transcript tail before the write
    term._last_held_reason = None
    term._arm_inject_backstop = lambda: None
    term._set_turn_open = lambda v: None
    term._note_injected = lambda item: None
    term.submit_prompt = lambda text, settle=False: written.append(text) or True
    term.write_input = lambda raw: written.append(raw) or True
    return term


def test_the_drain_never_types_the_continuation_over_an_old_draft():
    """Through the queue's own drain: an hour-old draft no longer holds a delegated result, and it still
    holds the continuation."""
    written: list = []
    term = _driven(_terminal(True, time.monotonic() - 3600), written)
    term._prompt_queue.append({"text": "the continuation", "source": ACCOUNT_LIMIT_SOURCE, "steer": True})
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        asyncio.run(term._try_drain_prompt_queue())
        assert written == []
        term._prompt_queue.clear()
        term._prompt_queue.append({"text": "a result", "source": "delegate_result", "steer": True})
        asyncio.run(term._try_drain_prompt_queue())
    assert written == ["a result"]


def test_the_drain_holds_the_continuation_that_heads_the_queue_after_the_tail():
    """A local terminal re-reads its gates after the transcript's tail: a delegated result that an old draft
    no longer holds is cancelled during the tail, and the continuation now at the head is held by the draft."""
    written: list = []
    term = _driven(_terminal(True, time.monotonic() - 3600), written)
    type(term).is_remote = property(lambda self: False)
    result = {"text": "a result", "source": "delegate_result"}
    term._prompt_queue.extend([result, {"text": "the continuation", "source": ACCOUNT_LIMIT_SOURCE,
                                        "steer": True}])
    term._tailer = lambda: SimpleNamespace(resolve_and_tail=lambda sid, cid: term.cancel_prompt(result))
    term._fold_tail_result = lambda r: None
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        asyncio.run(term._try_drain_prompt_queue())
    assert written == [] and [i["source"] for i in term._prompt_queue] == [ACCOUNT_LIMIT_SOURCE]


def test_the_drain_never_steers_the_continuation_into_a_persons_turn():
    written: list = []
    term = _driven(_terminal(False, 0.0), written)
    term._turn_open = True                     # the person's own turn, started after the limit
    term._prompt_queue.append({"text": "the continuation", "source": ACCOUNT_LIMIT_SOURCE, "steer": True})
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        asyncio.run(term._try_drain_prompt_queue())
        assert written == []
        term._prompt_queue[0]["source"] = "delegate_result"
        asyncio.run(term._try_drain_prompt_queue())
    assert written == ["the continuation"]   # any other steer goes into the open turn


def test_an_enter_in_the_terminal_drops_the_waiting_continuation():
    """Through the input path: the person's Enter drops the continuation that waits and clears the chat's mark."""
    from core.session.interactive_input import InteractiveInputMixin
    term = type("_In", (InteractiveInputMixin,), {})()
    from collections import deque
    term.alive, term.otodock_attached, term._ready = True, False, False
    term._question_parked = False
    term._composer_dirty, term._composer_dirty_at = True, time.monotonic()
    term._input_buffer = []
    term._note_activity = lambda: None
    term._schedule_submit_tails = lambda: None
    term.session_id, term.chat_id = "s-in", "c-in"
    told: list[str] = []
    term._prompt_queue = deque([{"text": "x", "source": ACCOUNT_LIMIT_SOURCE, "chat_id": "c-in",
                                 "on_outcome": lambda o: told.append(o.path)}])
    term._satellite_inject = None
    chat_reload._limit_marks["c-in"] = time.monotonic()
    assert term.write_input(b"my own words\r")
    assert list(term._prompt_queue) == [] and told == ["none"]
    assert not chat_reload.limit_marked("c-in", take=False)


def test_the_continuation_is_never_typed_over_text_in_the_terminals_input_box():
    old_draft = time.monotonic() - 3600          # past the gate's lapse for any other prompt
    term = _terminal(True, old_draft)
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        assert term._prompt_gates_blocked() is None                       # a delegated result types now
        assert term._prompt_gates_blocked(hold_draft=True) == "composer_dirty"
        term._composer_dirty = False
        assert term._prompt_gates_blocked(hold_draft=True) is None


def test_the_persons_own_send_drops_the_waiting_continuation():
    term = _terminal(True, time.monotonic())
    told: list[str] = []
    item = {"text": "x", "source": ACCOUNT_LIMIT_SOURCE, "chat_id": "c-term",
            "on_outcome": lambda outcome: told.append(outcome.path)}
    other = {"text": "y", "source": "delegate_result"}
    term._prompt_queue.extend([item, other])
    assert term.drop_limit_continuation() == 1
    assert list(term._prompt_queue) == [other] and told == ["none"]


def _side(chat_id="c-t1", sid="s-t1", *, attached=False):
    """A terminal's side of what follows its limit, over a stand-in terminal."""
    from core.session.interactive_turn import TerminalLimitSide
    sp._session_binding_ctx[sid] = ("claude-code-cli", "")
    return TerminalLimitSide(SimpleNamespace(
        chat_id=chat_id, session_id=sid, user_sub="alice", otodock_attached=attached, last_ending=None,
        _a_tasks_terminal=lambda: False))


def test_the_turn_ended_card_writer_writes_a_row_and_a_frame_and_warns_on_failure(monkeypatch, caplog):
    """One writer for a terminal chat's turn-ended card: the block as one row of the chat and the same block
    as a frame to its open tabs. A row that fails is a warning, no frame, and the caller is told."""
    import json
    import logging

    from core.events import chat_writer
    from core.session import interactive_turn, session_state
    submitted: list = []
    frames: list = []

    async def _submit(chat_id, fn, *, label):
        if chat_id == "c-fail":
            raise RuntimeError("the store is away")
        submitted.append((chat_id, label, json.loads(fn.keywords["event_data"])))
    monkeypatch.setattr(chat_writer, "submit", _submit)
    monkeypatch.setattr(session_state, "broadcast_chat_frame", lambda cid, frame: frames.append((cid, frame)))
    assert asyncio.run(interactive_turn.write_turn_ended_card("c-w", _LIMIT, label="limit_card"))
    (cid, label, block), = submitted
    assert (cid, label, block["subtype"], block["reason"]) == ("c-w", "limit_card", "turn_ended", "limit")
    assert frames == [("c-w", {**block, "chat_id": "c-w"})]
    with caplog.at_level(logging.WARNING):
        assert not asyncio.run(interactive_turn.write_turn_ended_card("c-fail", _LIMIT, label="limit_card"))
    assert len(frames) == 1 and "card was not written" in caplog.text


def test_the_limit_card_and_the_memory_card_share_the_one_writer(monkeypatch):
    """The terminal's limit card and the memory guard's card are written by ``write_turn_ended_card``; the
    limit's side then nudges the chat's rows, only after a card that was written."""
    from core.events import turn_ending
    from core.session import interactive_session, interactive_turn
    from core.session.interactive_turn import TerminalLimitSide
    from services.notifications import notification_manager
    written: list = []
    nudged: list = []
    ok = {"is": True}

    async def _write(chat_id, ending, *, label):
        written.append((chat_id, ending.reason, label))
        return ok["is"]
    monkeypatch.setattr(interactive_turn, "write_turn_ended_card", _write)
    monkeypatch.setattr(notification_manager, "broadcast_chat_rows", lambda owner, cid, **kw: nudged.append(cid))
    sp._session_binding_ctx["s-t1"] = ("claude-code-cli", "")
    side = TerminalLimitSide(SimpleNamespace(
        chat_id="c-t1", session_id="s-t1", user_sub="alice", otodock_attached=False, last_ending=None,
        _a_tasks_terminal=lambda: False, _chat_owner=lambda: "alice", agent_name="a1"))
    asyncio.run(side.show_card(_LIMIT, {}))
    asyncio.run(interactive_session._note_memory_end("c-mem"))
    ok["is"] = False
    asyncio.run(side.show_card(_LIMIT, {}))
    assert written == [("c-t1", turn_ending.LIMIT, "limit_card"), ("c-mem", turn_ending.EXITED, "memory_end"),
                       ("c-t1", turn_ending.LIMIT, "limit_card")]
    assert nudged == ["c-t1"]


def test_a_terminals_limit_continues_once_through_the_reload():
    """The terminal's limit takes the pump's own decision (``after_limit``), in place on Claude, never holding
    the chat's headless queue; refused for a terminal driven through the ``otodock`` command, a continuation's
    own limit and a chat the reload refuses."""
    scheduled: list[dict] = []
    with patch.object(pool_limits, "limit_continues", return_value=True), \
            patch.object(chat_reload, "may_continue_after_limit", return_value=True), \
            patch.object(chat_reload, "schedule_for_limit",
                         lambda cid, **kw: scheduled.append({"chat": cid, **kw}) or True):
        assert asyncio.run(pool_limits.after_limit(_side(), _LIMIT, {}, "sub-a"))
        assert not asyncio.run(pool_limits.after_limit(_side(attached=True), _LIMIT, {}, "sub-a"))
        chat_reload._limit_marks["c-t1"] = time.monotonic()
        assert not asyncio.run(pool_limits.after_limit(_side(), _LIMIT, {}, "sub-a"))
        assert chat_reload.limit_marked("c-t1", take=False)       # only read: its row is the session's file
        with patch.object(chat_reload, "may_continue_after_limit", return_value=False):
            chat_reload._limit_marks.pop("c-t1")
            assert not asyncio.run(pool_limits.after_limit(_side(), _LIMIT, {}, "sub-a"))
    assert scheduled == [{"chat": "c-t1", "person": "alice", "session_id": "s-t1", "old_sub": "sub-a",
                          "in_place": True, "fresh": False, "holds_queue": False, "unattended": False}]


def test_a_task_runs_terminal_gets_no_continuation():
    from core.session import session_kind
    side = _side(chat_id=session_kind.TASK_CHAT_ID_PREFIX + "run-1", sid="s-task")
    scheduled: list[str] = []
    asked = AssertionError("a task's terminal is refused before anything is asked")
    with patch.object(pool_limits, "limit_continues", side_effect=asked), \
            patch.object(chat_reload, "may_continue_after_limit", side_effect=asked), \
            patch.object(chat_reload, "schedule_for_limit", lambda cid, **kw: scheduled.append(cid)):
        assert not asyncio.run(pool_limits.after_limit(side, _LIMIT, {}, "sub-a"))
    assert scheduled == []


def test_a_task_chat_never_continues_by_the_reloads_rule(monkeypatch):
    """The predicate itself refuses a task's chat, whatever the person's standing and the reload's refusals."""
    from core.session import session_kind
    task_chat = session_kind.TASK_CHAT_ID_PREFIX + "run-1"
    monkeypatch.setattr("storage.database.get_chat", lambda cid: {"id": cid, "agent": "a1"})
    monkeypatch.setattr("auth.providers.effective_role_of", lambda sub, agent: "editor")
    monkeypatch.setattr(chat_reload, "_refusal", lambda chat, asker: "")
    assert not chat_reload.may_continue_after_limit(task_chat, "alice")
    assert chat_reload.may_continue_after_limit("c-own", "alice")
    assert not chat_reload.may_continue_after_limit("c-own", "")


def test_a_forged_pair_of_limit_lines_never_continues_twice():
    """The terminal's limit row is a file the session could write: the mark is only read on it, and the
    terminal's own turn end clears it."""
    scheduled: list[str] = []

    def _sched(cid, **kw):
        scheduled.append(cid)
        chat_reload._limit_marks[cid] = time.monotonic()      # as the resume marks it
        return True
    with patch.object(pool_limits, "limit_continues", return_value=True), \
            patch.object(chat_reload, "may_continue_after_limit", return_value=True), \
            patch.object(chat_reload, "schedule_for_limit", _sched):
        for _ in range(3):
            asyncio.run(pool_limits.after_limit(_side("c-forge", "s-forge"), _LIMIT, {}, "sub-a"))
    assert scheduled == ["c-forge"]


def test_the_terminals_turn_end_after_the_continuation_clears_its_mark(monkeypatch):
    waiters: list = []
    isess = SimpleNamespace(add_turn_end_waiter=waiters.append)
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: isess)
    monkeypatch.setattr("core.background.spawn", lambda coro, **kw: coro.close())
    chat_reload._limit_marks["c-te"] = time.monotonic()
    chat_reload._after_terminal_queue("c-te", "s-te")
    assert chat_reload.limit_marked("c-te", take=False)
    waiters[0]("s-te")
    assert not chat_reload.limit_marked("c-te", take=False)


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------

def _run(pump, *, rounds=0):
    from services.scheduler.task_run import TaskRun
    run = TaskRun.__new__(TaskRun)
    run.pump, run.limit_rounds, run.run_id, run.chat_id = pump, rounds, "run-1", "c-run"
    run._round_on = AsyncMock()
    return run


def test_a_runs_limit_takes_one_round_with_the_platforms_words(monkeypatch):
    from storage import remote_store
    monkeypatch.setattr(remote_store, "reloads_since_person", lambda cid: 0)
    monkeypatch.setattr(chat_reload, "note_limit_round", AsyncMock())
    run = _run(SimpleNamespace(limit_continues=True))
    asyncio.run(run._limit_round())
    run._round_on.assert_awaited_once_with(chat_reload.LIMIT_NUDGE)
    assert run.limit_rounds == 1
    asyncio.run(run._limit_round())                       # never a second
    assert run._round_on.await_count == 1
    quiet = _run(SimpleNamespace(limit_continues=False))
    asyncio.run(quiet._limit_round())
    quiet._round_on.assert_not_awaited()


def test_a_run_past_its_chain_bound_fails_with_its_card(monkeypatch):
    from core.events import chat_writer
    from storage import remote_store
    monkeypatch.setattr(remote_store, "reloads_since_person", lambda cid: chat_reload.CHAIN_MAX)
    rows: list = []

    async def _submit(cid, job, **kw):
        rows.append((cid, job.keywords))
    monkeypatch.setattr(chat_writer, "submit", _submit)
    pump = SimpleNamespace(limit_continues=True, last_ending=_LIMIT)
    run = _run(pump)
    asyncio.run(run._limit_round())
    run._round_on.assert_not_awaited()
    assert pump.limit_continues is False
    assert rows and rows[0][0] == "c-run" and '"turn_ended"' in rows[0][1]["event_data"]


# ---------------------------------------------------------------------------
# A person's own message, and the chat's queue held for the waiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_continued_limit_keeps_the_persons_waiting_message(temp_db, monkeypatch):
    """The pump's end hands nothing back: the person's message stays queued, held for the waiter (it goes
    first, on the new account), and no undelivered card is written. The schedule itself holds the queue."""
    armed: list[str] = []
    monkeypatch.setattr(chat_reload, "_arm", lambda cid, *a, **kw: armed.append(cid))
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    monkeypatch.setattr(sp, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "rest_after_limit", lambda sid, ending, msg: None)
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: True)
    sp._session_binding_ctx["sess-q-1"] = ("codex-cli", "")
    temp_db.create_chat("q-1", "user-admin", "a1")
    q = await input_queue.loaded("q-1")
    await q.add("q-a", "user-admin", TurnInput(text="my own message"))
    eq: asyncio.Queue = asyncio.Queue()
    await eq.put(CommonEvent(type=ERROR, data={"message": _LIMIT.line(), "ending": _LIMIT.as_dict()}))
    pump = stream_pump.ChatStreamPump(
        chat_id="q-1", session_id="sess-q-1",
        producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
        event_queue=eq, perm_queue=None)
    pump.wake_person = "user-admin"
    stream_pump._active_pumps["q-1"] = pump        # as the dashboard's turn registers it
    await asyncio.wait_for(pump.start(), 10)
    for _ in range(20):
        await asyncio.sleep(0.05)
    assert pump.limit_continues and armed == ["q-1"]
    assert [qi.item.text for qi in input_queue.get("q-1").items] == ["my own message"]
    assert [r for r in temp_db.get_chat_messages("q-1") if "undelivered" in (r.get("event_data") or "")] == []
    assert input_queue.getting_ready("q-1") and not await input_queue.deliver("q-1")


@pytest.mark.asyncio
async def test_a_runs_limit_still_hands_its_queue_back(temp_db, monkeypatch):
    """A run keeps today's hand-back: its round is its own, never a person's message."""
    pump, _scheduled, _cards = await _pump_on_limit(temp_db, monkeypatch, "q-run", continues=True, run_id="run-q",
                                                    queued=("a message",))
    assert pump.limit_continues and input_queue.get("q-run").items == []


def test_the_schedule_holds_the_queue_and_the_waiters_end_lets_go(monkeypatch):
    """The hold lasts from the schedule into the waiter, and the waiter's end lets go of it whatever way it
    ends."""
    seen: list[bool] = []

    async def _wait(cid, *a, limit=None, **kw):
        seen.append(input_queue.getting_ready(cid))     # ends without a claim (a refusal, a deadline)
    monkeypatch.setattr(chat_reload, "_wait_and_reload", _wait)
    monkeypatch.setattr(chat_reload, "_limit_begins", AsyncMock(side_effect=lambda cid, lim: lim))
    monkeypatch.setattr("storage.chat.pending_rebuilds.save", lambda *a, **kw: None)
    monkeypatch.setattr("storage.chat.pending_rebuilds.clear", lambda *a, **kw: None)

    async def _go():
        assert chat_reload.schedule_for_limit("q-h", person="alice", session_id="s", old_sub="sub-a",
                                              in_place=False, holds_queue=True)
        for _ in range(10):
            await asyncio.sleep(0.01)
    asyncio.run(_go())
    assert seen == [True] and not input_queue.getting_ready("q-h")


def test_the_queues_hold_is_kept_while_a_turn_is_open_and_ends_at_the_claim(monkeypatch):
    world = _waiter_world(monkeypatch, chat={"id": "q-h2", "agent": "a1"})
    input_queue.hold_first_turn("q-h2")
    turns = iter([True, False])
    monkeypatch.setattr(chat_reload.chat_move, "open_turn", lambda c: next(turns))
    monkeypatch.setattr(chat_reload.asyncio, "sleep", AsyncMock())
    held_while_open: list[bool] = []
    real_release = world.queue.release

    def _release(claim, rearm=False):
        held_while_open.append(input_queue.getting_ready("q-h2"))
        real_release(claim, rearm)
    world.queue.release = _release
    asker = chat_reload.chat_move.Asker(sub="alice", role="", platform_admin=False)
    limit = chat_reload._Limit("s", "sub-a", False, since=0, told=True, holds_queue=True)
    asyncio.run(chat_reload._wait_and_reload("q-h2", asker, "", {}, ("", 0), None, 60.0, limit=limit))
    assert held_while_open[0] is True                  # the open turn's end delivers nothing yet
    assert not input_queue.getting_ready("q-h2") and world.continued == [("q-h2", "alice", False)]


@pytest.mark.asyncio
async def test_a_second_limit_while_a_continuation_waits_shows_its_card(temp_db, monkeypatch):
    """A person's turn that meets the limit while the first continuation still waits keeps its card:
    nothing would continue it. A waiting reload keeps today's answer (it continues the chat itself)."""
    async def _forever(*a, **kw):
        await asyncio.sleep(3600)
    monkeypatch.setattr(chat_reload, "_wait_and_reload", _forever)
    monkeypatch.setattr(chat_reload, "_limit_begins", AsyncMock(side_effect=lambda cid, lim: lim))
    monkeypatch.setattr("storage.chat.pending_rebuilds.save", lambda *a, **kw: None)
    monkeypatch.setattr("storage.chat.pending_rebuilds.clear", lambda *a, **kw: None)
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    monkeypatch.setattr(sp, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "rest_after_limit", lambda sid, ending, msg: None)
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: True)

    async def _limit_in(cid):
        sp._session_binding_ctx[f"sess-{cid}"] = ("claude-code-cli", "")
        temp_db.create_chat(cid, "user-admin", "a1")
        eq: asyncio.Queue = asyncio.Queue()
        await eq.put(CommonEvent(type=ERROR, data={"message": _LIMIT.line(), "ending": _LIMIT.as_dict()}))
        pump = stream_pump.ChatStreamPump(
            chat_id=cid, session_id=f"sess-{cid}",
            producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
            event_queue=eq, perm_queue=None)
        pump.wake_person = "user-admin"
        stream_pump._active_pumps[cid] = pump
        await asyncio.wait_for(pump.start(), 10)
        return pump, [r for r in temp_db.get_chat_messages(cid) if "turn_ended" in (r.get("event_data") or "")]

    assert chat_reload.schedule_for_limit("w-1", person="user-admin", session_id="s", old_sub="sub-a",
                                          in_place=True)
    reload_waits = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    chat_reload._waiters["w-2"] = reload_waits          # the agent's own reload, not a continuation
    try:
        first, first_cards = await _limit_in("w-1")
        second, second_cards = await _limit_in("w-2")
    finally:
        for cid in ("w-1", "w-2"):
            task = chat_reload._waiters.pop(cid, None)
            if task is not None:
                task.cancel()
        reload_waits.cancel()
    assert chat_reload.continuation_waits("w-1") is False   # gone with its task
    assert len(first_cards) == 1 and not first.limit_continues
    assert second_cards == [] and second.limit_continues


@pytest.mark.asyncio
async def test_a_runs_and_a_calls_refused_conversation_keep_their_card(temp_db, monkeypatch):
    """The fresh start is a person's dashboard turn's alone: a run's or a call's turn that fails with the
    vendor's code for an unreadable conversation keeps its card and schedules nothing."""
    from core.session import session_kind
    refused = {"message": "invalid_encrypted_content: the reasoning cannot be read"}
    for cid, kw in (("f-run", {"run_id": "run-f"}), ("f-call", {"source_type": session_kind.PHONE.source_type})):
        pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, cid, continues=True, data=refused,
                                                      **kw)
        assert scheduled == [] and not pump.limit_continues and len(cards) == 1, cid


@pytest.mark.asyncio
async def test_the_continuations_own_turn_takes_the_mark(temp_db, monkeypatch):
    """The continuation's own headless turn has no person on its pump: its limit shows the card and takes
    the mark, so the person's next turn that meets a limit may continue."""
    chat_reload._limit_marks["m-own"] = time.monotonic()
    pump, scheduled, cards = await _pump_on_limit(temp_db, monkeypatch, "m-own", continues=True, person="")
    assert len(cards) == 1 and scheduled == []
    assert not chat_reload.limit_marked("m-own", take=False)


def test_a_typed_enter_drops_the_waiting_continuation():
    """A person types in the terminal one key at a time, then a bare return: their own message, so the
    continuation that waits is dropped and the hook told."""
    from collections import deque

    from core.session.interactive_input import InteractiveInputMixin
    term = type("_Keys", (InteractiveInputMixin,), {})()
    term.alive, term.otodock_attached, term._ready = True, False, False
    term._question_parked = False
    term._composer_dirty, term._composer_dirty_at = False, 0.0
    term._input_buffer = []
    term._note_activity = lambda: None
    term._schedule_submit_tails = lambda: None
    term.session_id, term.chat_id = "s-k", "c-k"
    told: list[str] = []
    term._prompt_queue = deque([{"text": "x", "source": ACCOUNT_LIMIT_SOURCE, "chat_id": "c-k",
                                 "on_outcome": lambda o: told.append(o.path)}])
    term._satellite_inject = None
    for key in (b"h", b"i"):
        assert term.write_input(key)
    assert list(term._prompt_queue) != [] and told == []
    assert term.write_input(b"\r")
    assert list(term._prompt_queue) == [] and told == ["none"]


def test_a_local_terminal_never_gets_the_continuation():
    """A terminal the person attached through the ``otodock`` command: a continuation queued before the
    attach is dropped, never handed to the machine (which never tells of the person's own Enter)."""
    written: list = []
    term = _driven(_terminal(True, time.monotonic()), written)
    term.otodock_attached = True
    told: list[str] = []
    term._prompt_queue.append({"text": "the continuation", "source": ACCOUNT_LIMIT_SOURCE, "steer": True,
                               "on_outcome": lambda o: told.append(o.path)})
    term._prompt_queue.append({"text": "a result", "source": "delegate_result"})
    handed: list = []

    async def _hand():
        handed.append([i.get("source") for i in term._prompt_queue])
    term._try_satellite_inject = _hand
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        asyncio.run(term._try_drain_prompt_queue())
    assert told == ["none"]
    assert handed == [["delegate_result"]]          # what is left still goes to the machine, at once


def test_the_resume_holds_while_an_enter_settles(monkeypatch):
    """An Enter empties the box before its turn is seen: the resume into the live terminal waits for it."""
    world = _resume_world(monkeypatch)
    live = SimpleNamespace(turn_open=False, submit_settling=True)
    monkeypatch.setattr("core.session.interactive_session.find_live_for_chat", lambda cid: live)
    limit = chat_reload._Limit("s-res", "sub-a", True, since=0)
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "", limit=limit, terminal=True))
    assert world.started == []


@pytest.mark.asyncio
async def test_continuations_in_a_row_reach_the_chain_bound(temp_db):
    """What a continuation leaves in the chat (its wake row) counts toward its bound in a row; a person's
    message starts the count again, and a delegated result's wake never counts."""
    import json

    from storage import remote_store
    from ws import wire_events as wire
    cid = "b-1"
    temp_db.create_chat(cid, "user-admin", "a1")
    temp_db.add_chat_message(cid, "user", "go on", author_sub="user-admin")

    def _wake(source):
        temp_db.add_chat_message(cid, "event", "", event_type=wire.PERSISTED_SCHEDULE_WAKE,
                                 event_data=json.dumps({"prompt": chat_reload.LIMIT_NUDGE, "source": source}))
    _wake("delegate_result")
    for _ in range(chat_reload.LIMIT_CHAIN_MAX):
        await chat_reload._account_note(cid, chat_reload.LIMIT_LINE)
        _wake(wire.WAKE_SOURCE_RELOAD)
    await asyncio.sleep(0.2)
    assert remote_store.resumes_since_person(cid) == chat_reload.LIMIT_CHAIN_MAX
    assert chat_reload._bound(cid, remote_store.resumes_since_person(cid), limit=True)
    assert remote_store.reloads_since_person(cid) == 0          # the agent's own reloads are another count
    temp_db.add_chat_message(cid, "user", "and now?", author_sub="user-admin")
    assert remote_store.resumes_since_person(cid) == 0


def test_a_continuation_leaves_another_reasons_close_as_it_is():
    """Only the account's own waiting close gives way; a close for another reason keeps its task and reason."""
    other = MagicMock()
    chat_reload._idle_closes["c-oc"], chat_reload._idle_close_reasons["c-oc"] = other, "model"
    try:
        with patch.object(chat_reload, "_arm"):
            assert chat_reload.schedule_for_limit("c-oc", person="alice", session_id="s", old_sub="sub-a",
                                                  in_place=True)
        assert chat_reload._idle_closes["c-oc"] is other and chat_reload._idle_close_reasons["c-oc"] == "model"
        other.cancel.assert_not_called()
    finally:
        chat_reload._idle_closes.pop("c-oc", None)
        chat_reload._idle_close_reasons.pop("c-oc", None)


# ---------------------------------------------------------------------------
# A turn nobody watches (a delegated result, a wake): the continue pass's rules
# ---------------------------------------------------------------------------

def _session_for(chat_id: str, sub: str = "user-admin", *, kind: str = "dashboard",
                 principal: str = "") -> None:
    """The session the platform built for the turn: its own context and kind."""
    from auth.path_policy import SecurityContext
    from core.session import session_state
    from storage import database as task_store
    sid = f"sess-{chat_id}"
    if not task_store.get_username_by_sub(sub):
        # The seeded admin has no username: a context names its person by one.
        task_store.upsert_user(sub, f"{sub}@x.test", "Admin", "admin")
    assert task_store.get_username_by_sub(sub), "the person needs a username"
    more = {"principal": principal} if principal else {}
    session_state._session_security[sid] = SecurityContext(
        role="manager", username=task_store.get_username_by_sub(sub) or "", agent="a1",
        is_admin_agent=False, **more)
    session_state._session_security_ts[sid] = time.time()
    session_state._record_session_use(sid, client_type=kind, agent="a1")


@pytest.fixture
def _sessions_cleared():
    from core.session import session_state
    yield
    for sid in [s for s in session_state._session_security if s.startswith("sess-una-")]:
        session_state._session_security.pop(sid, None)
        session_state._session_security_ts.pop(sid, None)
        session_state._sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_a_delegated_results_turn_nobody_watches_continues_once(
        temp_db, monkeypatch, _sessions_cleared):
    """The operator's rule: unattended work continues after a limit too. No
    person is on the pump: the session's own person, judged by the continue
    pass's rules, is who it continues as."""
    _session_for("una-1")
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-1", continues=True, person="",
        driven=("user-admin", "delegate_result"))
    assert pump.limit_continues and cards == []
    assert scheduled == [{"chat": "una-1", "person": "user-admin", "session_id": "sess-una-1",
                          "old_sub": "sub-a", "in_place": False, "fresh": False, "holds_queue": True,
                          "unattended": True}]
    # Nothing is counted yet: the continuation has not gone out.
    from storage.chat import cut_records
    assert cut_records.continued_in_a_row("una-1") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["schedule_wake", "delegate_result_replay", "continue"])
async def test_a_wake_and_the_pass_own_turn_continue_too(temp_db, monkeypatch, _sessions_cleared, source):
    chat_id = f"una-s-{source[:4]}"
    _session_for(chat_id)
    pump, scheduled, _cards = await _pump_on_limit(
        temp_db, monkeypatch, chat_id, continues=True, person="", driven=("user-admin", source))
    assert pump.limit_continues and len(scheduled) == 1 and scheduled[0]["unattended"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["api_key", "move_resume", "", "bg_review", "account_limit"])
async def test_a_keys_turn_a_move_and_a_turn_with_no_source_keep_the_card(
        temp_db, monkeypatch, _sessions_cleared, source):
    chat_id = f"una-k-{source[:4] or 'none'}"
    _session_for(chat_id)
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, chat_id, continues=True, person="", driven=("user-admin", source))
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_a_persons_waiting_message_lets_an_unattended_turn_go_on_and_goes_first(
        temp_db, monkeypatch, _sessions_cleared):
    """The pass's rule gives way to a person's message that waits. Here that
    is no reason for the card: the continuation is scheduled with the queue
    held, and the waiter sends the person's message first, on the new account
    (``test_a_codex_chats_waiting_message_starts_it_on_the_pools_pick``)."""
    _session_for("una-q1")
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-q1", continues=True, person="",
        driven=("user-admin", "delegate_result"), queued=("go on with the tests",))
    assert pump.limit_continues and cards == []
    assert len(scheduled) == 1 and scheduled[0]["holds_queue"] is True
    # The message was not handed back to its author: it waits for the waiter.
    assert [i.item.text for i in input_queue.get("una-q1").items] == ["go on with the tests"]


@pytest.mark.asyncio
async def test_a_session_built_for_nobody_keeps_the_card(temp_db, monkeypatch, _sessions_cleared):
    _session_for("una-n1")
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-n1", continues=True, person="", driven=("", "delegate_result"))
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1
    # A session with no kind (a wake built as nobody's) keeps it too.
    _session_for("una-n2", kind="")
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-n2", continues=True, person="",
        driven=("user-admin", "delegate_result"))
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_an_outside_persons_session_keeps_the_card(temp_db, monkeypatch, _sessions_cleared):
    _session_for("una-x1", principal="external")
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-x1", continues=True, person="",
        driven=("user-admin", "delegate_result"))
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_it_continues_only_as_the_person_the_session_runs_as(
        temp_db, monkeypatch, _sessions_cleared):
    """The caller's word is not enough: the session's own context names who
    it runs as, and the turn must have been started for that person."""
    temp_db.upsert_user("sub-other", "other@x.test", "Other", "member")
    _session_for("una-p1", sub="sub-other")
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-p1", continues=True, person="",
        driven=("user-admin", "delegate_result"))
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1
    # A session with no context at all keeps the card.
    pump, scheduled, cards = await _pump_on_limit(
        temp_db, monkeypatch, "una-p2", continues=True, person="",
        driven=("user-admin", "delegate_result"))
    assert not pump.limit_continues and scheduled == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_the_bounds_of_the_continue_pass_stop_the_fourth_in_a_row(
        temp_db, monkeypatch, _sessions_cleared):
    """Restarts and limits count together: three continues in a row with no
    message from a person, then the card."""
    from storage.chat import cut_records
    _session_for("una-b1")
    temp_db.create_chat("una-b1", "user-admin", "a1")
    for _ in range(3):
        assert cut_records.add_settled(
            "una-b1", cause=cut_records.CAUSE_LIMIT, state=cut_records.CONTINUED,
            reason="continued after a usage limit", session_id="sess-una-b1",
            person="user-admin", kind="dashboard") is not None
    assert cut_records.continued_in_a_row("una-b1") == 3
    assert cut_records.unsettled_of("una-b1") is None
    assert not pool_limits.continues_unattended("una-b1", "sess-una-b1", "user-admin")
    # A person's message starts the count again.
    await asyncio.sleep(0.01)
    temp_db.add_chat_message("una-b1", "user", "go on")
    assert pool_limits.continues_unattended("una-b1", "sess-una-b1", "user-admin")


def test_a_settled_record_is_pruned_with_the_others(temp_db):
    from storage.chat import cut_records
    from storage.pg import get_conn
    temp_db.create_chat("una-b2", "user-admin", "a1")
    made = cut_records.add_settled("una-b2", cause=cut_records.CAUSE_LIMIT,
                                   state=cut_records.CONTINUED, session_id="s", kind="dashboard")
    assert cut_records.get(made["id"])["settled_at"]
    old = (datetime.now(timezone.utc) - timedelta(days=cut_records.KEEP_DAYS + 1)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET settled_at=%s WHERE id=%s", (old, made["id"]))
        conn.commit()
    cut_records.tidy_at_boot()
    assert cut_records.get(made["id"]) is None
# A Stop while a continuation waits
# ---------------------------------------------------------------------------

def _stop_world(monkeypatch):
    from tests.fixtures.ws_dashboard_harness import (
        FakeExecutionLayer,
        make_test_agent,
        stub_dashboard_seams,
    )
    from tests.session.test_rebuild_characterization import _chat, _connection
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    agent = make_test_agent()
    cid = _chat(agent)
    notes: list[str] = []

    async def _note(chat_id, message, reason="", **kw):
        notes.append(message)

    async def _forever(*a, **kw):
        await asyncio.sleep(3600)
    monkeypatch.setattr(chat_reload, "_note", _note)
    monkeypatch.setattr(chat_reload, "_wait_and_reload", _forever)
    monkeypatch.setattr(chat_reload, "_limit_begins", AsyncMock(side_effect=lambda c, lim: lim))
    def _conn(sub="user-admin"):
        conn = _connection(cid, None, agent=agent, layer=layer, user_sub=sub)
        conn._warmup_chat_id, conn.implement_queue = None, []
        conn._cancel_artifacts = lambda chat_id: None
        return conn
    return cid, notes, _conn


def test_a_stop_ends_the_waiting_continuation_and_takes_back_only_the_stoppers_message(temp_db, monkeypatch):
    """Stop means stop: the continuation that waits does not run and the chat says so. The stopper's own
    waiting message comes back; a teammate's still goes out once the waiter has let go of the chat."""
    cid, notes, connection = _stop_world(monkeypatch)

    async def _go():
        q = await input_queue.loaded(cid)
        await q.add("q-mine", "user-admin", TurnInput(text="mine"))
        await q.add("q-team", "user-viewer", TurnInput(text="a teammate's"))
        assert chat_reload.schedule_for_limit(cid, person="user-admin", session_id="s", old_sub="sub-a",
                                              in_place=False, holds_queue=True)
        await asyncio.sleep(0.05)
        await connection()._on_abort({})
        for _ in range(10):
            await asyncio.sleep(0.01)
        # Read inside the loop: its close would cancel a waiter the Stop left.
        assert not chat_reload.continuation_waits(cid) and not input_queue.getting_ready(cid)
        return [qi.queue_id for qi in input_queue.peek(cid)]
    assert asyncio.run(_go()) == ["q-team"] and notes == [chat_reload.LIMIT_STOPPED]


def test_a_stop_leaves_the_agents_waiting_reload(temp_db, monkeypatch):
    """A reload the agent asked for is not a continuation: a Stop leaves it, as it always did."""
    cid, notes, connection = _stop_world(monkeypatch)

    async def _go():
        reload_waits = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        chat_reload._waiters[cid] = reload_waits
        try:
            await connection()._on_abort({})
            return chat_reload.waiting(cid)
        finally:
            chat_reload._waiters.pop(cid, None)
            reload_waits.cancel()
    assert asyncio.run(_go()) is True and notes == []


def test_only_someone_who_may_drive_the_chat_stops_its_continuation(temp_db, monkeypatch):
    """An editor of the agent may read another person's own chat, never drive it: their Stop leaves it."""
    from storage import database as task_store
    cid, notes, _connection = _stop_world(monkeypatch)
    task_store.add_user_agent("user-viewer", task_store.get_chat(cid)["agent"], "editor", "test")

    async def _go():
        assert chat_reload.schedule_for_limit(cid, person="user-admin", session_id="s", old_sub="sub-a",
                                              in_place=True)
        try:
            stopped = await pool_limits.stop_continuation(cid, "user-viewer")
            return stopped, chat_reload.continuation_waits(cid)
        finally:
            chat_reload._waiters.pop(cid).cancel()
    assert asyncio.run(_go()) == (False, True) and notes == []


def test_a_stop_drops_the_continuation_queued_in_the_terminal(monkeypatch):
    """In a terminal the continuation waits in the terminal's queue: the dashboard's Stop presses Esc and,
    by the same rule, drops it as the person's own Enter does (whose outcome tells the chat)."""
    dropped: list[str] = []
    live = SimpleNamespace(_prompt_queue=[{"source": ACCOUNT_LIMIT_SOURCE}], may_drive=lambda sub: sub == "alice",
                           drop_limit_continuation=lambda: dropped.append("x"), limit_mark=None)
    monkeypatch.setattr("core.session.interactive_session.find_live_for_chat", lambda cid: live)
    monkeypatch.setattr("storage.database.get_chat", lambda cid: {"id": cid, "agent": "a1", "user_sub": "alice"})
    monkeypatch.setattr("auth.providers.effective_role_of", lambda sub, agent: "editor")
    monkeypatch.setattr(chat_reload.chat_move, "may_drive", lambda chat, asker: True)
    # The resume marks the turn before it queues it; a mark past its hour still says one is queued.
    chat_reload._limit_marks["c-term"] = time.monotonic() - 2 * chat_reload.LIMIT_MARK_S
    assert asyncio.run(pool_limits.stop_continuation("c-term", "bob")) is False and dropped == []
    assert asyncio.run(pool_limits.stop_continuation("c-term", "alice")) is True and dropped == ["x"]
    assert "c-term" in chat_reload._limit_stopped          # its outcome writes the Stop's line
    chat_reload._limit_stopped.discard("c-term")


def _started_world(monkeypatch):
    """A continuation's waiter through the real ``_wait_and_reload``, for a Stop at each of its steps: the
    waiter world, a Stop from the chat's own person (the drive rule is the test above's)."""
    world = _waiter_world(monkeypatch, chat={"id": "c-st", "agent": "a1", "session_id": "s-st"})
    monkeypatch.setattr("storage.chat.pending_rebuilds.save", lambda *a, **kw: None)
    monkeypatch.setattr("storage.chat.pending_rebuilds.clear", lambda *a, **kw: None)
    monkeypatch.setattr(chat_reload.chat_move, "may_drive", lambda chat, asker: True)
    monkeypatch.setattr("core.session.interactive_session.find_live_for_chat", lambda cid: None)
    return world


def _arm_limit(*, in_place=False, **kw):
    chat_reload._arm("c-st", chat_reload.chat_move.Asker(sub="alice", role="", platform_admin=False), "", {},
                     ("", 0), None, 60.0,
                     limit=chat_reload._Limit("s-st", "sub-a", in_place, told=True, since=0, **kw))


@pytest.mark.parametrize("in_place", [False, True])
def test_a_stop_while_the_continuations_turn_runs_keeps_the_waiter_and_writes_no_line(monkeypatch, in_place):
    """The waiter lives through the continuation's own turn (the delivery awaits its pump): a Stop then is
    the turn's own Stop, never a cancel of the waiter and never "did not continue" under a reply that did.
    In place (Claude Code) the session is not closed: the resume rides it once the sign-in moved."""
    world = _started_world(monkeypatch)
    monkeypatch.setattr(pool_limits, "moved_off", AsyncMock(return_value=True))
    running, release = asyncio.Event(), asyncio.Event()

    async def _turn(chat_, asker, reason, switched=None, *, limit=None, terminal=False):
        running.set()               # the pump runs inside the delivery
        await release.wait()
        world.continued.append(chat_["id"])
    monkeypatch.setattr(chat_reload, "_resume", _turn)

    async def _go():
        _arm_limit(in_place=in_place)
        await asyncio.wait_for(running.wait(), 5)
        assert await pool_limits.stop_continuation("c-st", "alice") is True
        await asyncio.sleep(0.01)
        alive = chat_reload.continuation_waits("c-st")
        release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
        return alive
    assert asyncio.run(_go()) is True and world.continued == ["c-st"]
    assert (world.closed, world.released) == (([], []) if in_place else (["account"], ["s-st"]))
    assert chat_reload.LIMIT_STOPPED not in [n for n, _r in world.notes] and "c-st" not in chat_reload._limit_stopped


def test_a_stop_inside_the_close_lets_the_account_go_then_the_resume_reads_it(monkeypatch):
    """A Stop that lands while the session closes is not a cancel half way: the rested account is still let go,
    and the Stop is left for the resume's last check."""
    world = _started_world(monkeypatch)
    inside, release = asyncio.Event(), asyncio.Event()
    from core.session import chat_rebuild

    class _SlowClose:
        def __init__(self, cid, **kw):
            world.closed.append(kw["reason"])
            self.chat = {"id": "c-st", "agent": "a1", "session_id": "s-st"}

        async def __aenter__(self):
            inside.set()
            await release.wait()
            return self

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(chat_rebuild, "closed", _SlowClose)
    seen: list[bool] = []

    async def _resume(chat_, asker, reason, switched=None, *, limit=None, terminal=False):
        seen.append("c-st" in chat_reload._limit_stopped)
    monkeypatch.setattr(chat_reload, "_resume", _resume)

    async def _go():
        _arm_limit()
        await asyncio.wait_for(inside.wait(), 5)
        assert await pool_limits.stop_continuation("c-st", "alice") is True
        release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
    asyncio.run(_go())
    assert world.released == ["s-st"] and seen == [True]
    assert chat_reload.LIMIT_STOPPED not in [n for n, _r in world.notes]


def test_a_continuation_that_begins_forgets_an_older_stop(monkeypatch):
    """A Stop is about the continuation of its moment. One the terminal was already typing is not dropped and
    tells no outcome, so the Stop's record outlives its waiter: the chat's next limit must not read it and stay
    stopped under a Stop nobody pressed."""
    _started_world(monkeypatch)
    seen: list[bool] = []

    async def _resume(chat_, asker, reason, switched=None, *, limit=None, terminal=False):
        seen.append("c-st" in chat_reload._limit_stopped)
    monkeypatch.setattr(chat_reload, "_resume", _resume)

    async def _go():
        chat_reload._limit_stopped.add("c-st")     # left by a Stop whose continuation was already typed
        _arm_limit()
        for _ in range(20):
            await asyncio.sleep(0.01)
    asyncio.run(_go())
    assert seen == [False]


def test_the_resume_reads_a_stop_last_and_starts_nothing(monkeypatch):
    world = _resume_world(monkeypatch)
    notes: list[str] = []

    async def _note(cid, message, reason="", **kw):
        notes.append(message)
    monkeypatch.setattr(chat_reload, "_note", _note)
    chat_reload._limit_stopped.add("c-res")
    asyncio.run(chat_reload._resume({"id": "c-res"}, _asker(), "",
                                    limit=chat_reload._Limit("s-res", "sub-a", False, since=0)))
    assert world.started == [] and notes == [chat_reload.LIMIT_STOPPED] and "c-res" not in chat_reload._limit_stopped


def test_a_stop_between_the_new_session_and_its_pump_leaves_the_delivery_alone(monkeypatch):
    """Past the resume's last check the delivery starts the session and then its pump: a Stop there cancels
    nothing half way (no session left without its pump), and the turn is then the Stop's own."""
    world = _started_world(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    delivered: list[str] = []

    async def _deliver(cid, text, **kw):
        started.set()               # the new session is up, its pump not yet
        await release.wait()
        delivered.append(cid)
    monkeypatch.setattr("core.session.chat_rebuild.rebuild_and_resume", _deliver)
    monkeypatch.setattr("core.session.chat_rebuild.chat_mode", lambda c: "auto")
    monkeypatch.setattr(chat_reload, "_resume", _REAL_RESUME)
    monkeypatch.setattr(chat_reload, "_turn_holds", lambda cid, terminal: False)

    async def _go():
        _arm_limit()
        await asyncio.wait_for(started.wait(), 5)
        assert await pool_limits.stop_continuation("c-st", "alice") is True
        await asyncio.sleep(0.01)
        alive = chat_reload.continuation_waits("c-st")
        release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
        return alive
    assert asyncio.run(_go()) is True and delivered == ["c-st"]
    assert chat_reload.LIMIT_STOPPED not in [n for n, _r in world.notes]


class TestTheCountOfAnUnattendedContinuation:
    """Counted with the continue pass's own (three in a row, six a day), at
    the moment the continuation goes out (``chat_reload._resume``'s outcome).
    One that never went out counts for nothing."""

    @staticmethod
    def _world(monkeypatch, temp_db, cid: str, *, path: str):
        from core.session import chat_rebuild
        from core.session.session_delivery import DeliveryOutcome
        from storage import remote_store
        temp_db.create_chat(cid, "user-admin", "a1")
        chat = {"id": cid, "agent": "a1", "session_id": f"s-{cid}"}

        async def _rebuild(chat_id, text, **kw):
            await kw["on_outcome"](DeliveryOutcome(path, chat_id=chat_id, session_id=chat["session_id"]))
        monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _rebuild)
        monkeypatch.setattr(chat_rebuild, "chat_mode", lambda c: "auto")
        monkeypatch.setattr("storage.database.get_chat", lambda chat_id: chat)
        monkeypatch.setattr(remote_store, "newest_person_row", lambda chat_id: 0)
        monkeypatch.setattr(chat_reload.chat_move, "_waiting_message_goes_first", lambda c: False)
        return chat

    @pytest.mark.asyncio
    async def test_it_is_counted_when_it_goes_out(self, temp_db, monkeypatch):
        from storage.chat import cut_records
        chat = self._world(monkeypatch, temp_db, "cnt-1", path="oneshot")
        limit = chat_reload._Limit("s-cnt-1", "sub-a", False, since=0, unattended=True)
        await chat_reload._resume(chat, _asker(), "", limit=limit)
        assert cut_records.continued_in_a_row("cnt-1") == 1
        assert cut_records.unsettled_of("cnt-1") is None
        # A turn its person watched is never counted with the pass's own.
        chat = self._world(monkeypatch, temp_db, "cnt-2", path="oneshot")
        await chat_reload._resume(chat, _asker(), "",
                                  limit=chat_reload._Limit("s-cnt-2", "sub-a", False, since=0))
        assert cut_records.continued_in_a_row("cnt-2") == 0

    @pytest.mark.asyncio
    async def test_one_that_never_went_out_counts_for_nothing(self, temp_db, monkeypatch):
        from storage.chat import cut_records
        # No rung delivered it.
        chat = self._world(monkeypatch, temp_db, "cnt-3", path="none")
        monkeypatch.setattr(chat_reload, "_note", AsyncMock())
        limit = chat_reload._Limit("s-cnt-3", "sub-a", False, since=0, unattended=True)
        await chat_reload._resume(chat, _asker(), "", limit=limit)
        assert cut_records.continued_in_a_row("cnt-3") == 0
        # A person's message went first: the resume starts nothing.
        chat = self._world(monkeypatch, temp_db, "cnt-4", path="oneshot")
        monkeypatch.setattr(chat_reload.chat_move, "_waiting_message_goes_first", lambda c: True)
        await chat_reload._resume(chat, _asker(), "", limit=limit)
        assert cut_records.continued_in_a_row("cnt-4") == 0

    def test_the_waiters_row_keeps_that_nobody_watched_across_a_restart(self):
        limit = chat_reload._Limit("s1", "sub-a", False, unattended=True)
        assert chat_reload._Limit.from_target(limit.target()).unattended is True
        # A row an earlier build wrote has no such key.
        assert chat_reload._Limit.from_target(
            '{"session_id": "s1", "old_sub": "a", "in_place": false}').unattended is False


# ---------------------------------------------------------------------------
# A terminal at a limit: the fold, the hold, the card, the run, a re-home
# ---------------------------------------------------------------------------

_FABLE_LIMIT = turn_ending.TurnEnding(
    reason=turn_ending.LIMIT, resets_at="2099-01-01T06:00:00+00:00",
    detail="You're out of usage credits. Run /usage-credits to keep using Fable 5.2 or /model to switch models.")


def _folding_terminal(monkeypatch):
    """A terminal whose fold runs for real, its signal fold and batch effects recorded."""
    term = _terminal(False, 0.0)
    term.last_ending, term._turn_end_waiters, term._turn_complete_fired = None, [], False
    term.on_turn_complete = None
    term._loop = MagicMock()
    seen: list = []
    spawned: list = []
    term._apply_turn_signal = lambda sig, **kw: seen.append((sig, term.limit_mark is not None))
    term._post_batch_effects = lambda r: None
    term._note_model_chosen = lambda m: None
    term._maybe_fire_turn_complete = lambda *a, **kw: seen.append(("complete",))
    term._arm_inject_backstop = lambda: None
    monkeypatch.setattr("core.session.interactive_turn.spawn",
                        lambda coro, **kw: (spawned.append(kw["name"]), coro.close()))
    return term, seen, spawned


def test_the_fold_marks_a_limit_before_its_signal_closes_the_turn(monkeypatch):
    """The close drains the queue and the gate reads the mark: the mark is set first, in the fold itself.
    What follows (the rest, the move, the card) runs off the fold, and no completion is reported."""
    term, seen, spawned = _folding_terminal(monkeypatch)
    term._fold_tail_result({"last_signal": "end_turn", "ending": _FABLE_LIMIT.as_dict(), "limit_menu": True,
                            "turn_complete": False})
    assert seen == [("end_turn", True)] and spawned == ["terminal_limit"]
    assert term.limit_mark.ending == _FABLE_LIMIT and term.last_ending == _FABLE_LIMIT and term._limit_menu


def test_a_person_typing_after_the_limit_row_keeps_no_hold(monkeypatch):
    term, seen, spawned = _folding_terminal(monkeypatch)
    term._fold_tail_result({"last_signal": "user", "ending": _FABLE_LIMIT.as_dict()})
    assert term.limit_mark is None and spawned == ["terminal_limit"]     # the rest and the card still follow


def test_a_clean_turn_end_ends_the_hold(monkeypatch):
    from core.session.interactive_turn import LimitMark
    term, seen, _spawned = _folding_terminal(monkeypatch)
    term.limit_mark = LimitMark.of(_FABLE_LIMIT)
    term._fold_tail_result({"last_signal": "end_turn", "turn_complete": True})
    assert term.limit_mark is None and ("complete",) in seen


def test_an_api_error_ends_what_waits_on_the_turn_with_it(monkeypatch):
    """A borrowed round's waiter and a task's completion end at once with the error, past the gates of a
    clean end; nothing is held."""
    term, _seen, spawned = _folding_terminal(monkeypatch)
    waited, completed = [], []
    term._turn_end_waiters.append(waited.append)
    term.on_turn_complete = completed.append
    error = turn_ending.TurnEnding(reason=turn_ending.ERROR, detail="API Error: 500")
    term._fold_tail_result({"last_signal": "end_turn", "ending": error.as_dict()})
    assert waited == [""] and completed == [""] and spawned == [] and term.limit_mark is None
    assert term.last_ending == error


def test_server_prompts_wait_at_the_limit_and_a_persons_send_and_the_continuation_pass(monkeypatch):
    from core.session.interactive_turn import LimitMark
    term = _terminal(False, 0.0)
    term._arm_inject_backstop = lambda: None
    monkeypatch.setattr(sp, "rest_ending_for", lambda sid, since=0.0: None)
    term._prompt_queue.append({"text": "a result", "source": "delegate_result"})
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        assert term._prompt_gates_blocked() is None
        term.limit_mark = LimitMark.of(_FABLE_LIMIT)
        assert term._prompt_gates_blocked() == "at_limit"
        term._prompt_queue[0] = {"raw": b"x", "source": "user"}
        assert term._prompt_gates_blocked() is None                       # a person's held send
        term._prompt_queue[0] = {"text": "go on", "source": ACCOUNT_LIMIT_SOURCE}
        assert term._prompt_gates_blocked() is None                       # the limit's own continuation
        term._prompt_queue[0] = {"text": "a result", "source": "delegate_result"}
        term.limit_mark.until = time.time() - 1                           # its reset passed
        assert term._prompt_gates_blocked() is None and term.limit_mark is None
        monkeypatch.setattr(sp, "rest_ending_for", lambda sid, since=0.0: _FABLE_LIMIT)     # its account rests for it
        assert term._prompt_gates_blocked() == "at_limit"


def test_a_server_prompt_that_may_steer_and_one_for_the_otodock_command_wait_at_the_limit(monkeypatch):
    """Every prompt the platform delivers is queued steer-eligible: it waits at the limit like any other, into
    an open turn too. A terminal on the ``otodock`` command (the machine types the head item) waits the same."""
    from core.session.interactive_turn import LimitMark
    term = _terminal(False, 0.0)
    monkeypatch.setattr(sp, "rest_ending_for", lambda sid, since=0.0: None)
    term.limit_mark = LimitMark.of(_FABLE_LIMIT)
    term._prompt_queue.append({"text": "a result", "source": "delegate_result", "steer": True})
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        term._turn_open = True
        assert term._prompt_gates_blocked(steering=True) == "at_limit"
        term._turn_open = False
        assert term._prompt_gates_blocked(steering=True) == "at_limit"
        assert term._prompt_gates_blocked(for_satellite=True) == "at_limit"


@pytest.mark.asyncio
async def test_the_mark_takes_the_reset_the_pool_completed(temp_db, monkeypatch):
    """A Codex row names no reset, so its mark first holds a quarter of an hour. The pool completes the ending
    with the reset the account's reading knows, and the mark holds to that reset."""
    from dataclasses import replace

    from core.session.interactive_turn import LimitMark
    bare = turn_ending.TurnEnding(reason=turn_ending.LIMIT, detail="You've hit your usage limit.")
    isess, ended = _limited_isess("c-reset-t", "s-reset-t")
    isess.limit_mark, isess.last_ending = LimitMark.of(bare), bare
    temp_db.create_chat(isess.chat_id, "user-admin", "a1")
    sp._session_binding_ctx[isess.session_id] = ("codex-cli", "")
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))
    completed = replace(bare, resets_at="2099-01-02T06:00:00+00:00")
    monkeypatch.setattr(pool_limits, "_rest_by_reading", lambda sid, ending, from_row: completed)
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: False)
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    monkeypatch.setattr("core.session.session_state.broadcast_chat_frame", lambda cid, f: None)
    await pool_limits.terminal_limit(isess, bare)
    assert isess.limit_mark.until == pool_limits._iso_epoch(completed.resets_at)
    assert isess.limit_mark.ending == completed and ended == [completed]


def test_a_mark_with_no_reset_holds_a_quarter_of_an_hour():
    from core.session.interactive_turn import _LIMIT_MARK_S, LimitMark
    mark = LimitMark.of(turn_ending.TurnEnding(reason=turn_ending.LIMIT))
    assert _LIMIT_MARK_S - 5 < mark.until - time.time() <= _LIMIT_MARK_S and mark.holds()


def test_the_account_rests_for_a_session_by_its_model_family():
    from services.engines import pool_state
    sid = "s-rests"
    sp._session_subscriptions[sid] = "sub-r"
    sp._session_binding_ctx[sid] = ("claude-code-cli", "")
    pool_state._session_models[sid] = "claude-opus-5-5"
    try:
        sp._scoped_rests[("sub-r", "fable")] = (time.time() + 600, time.time())
        assert pool_limits.rest_ending_for(sid) is None                     # Fable rests, the session runs Opus
        sp._scoped_rests[("sub-r", "opus")] = (time.time() + 600, time.time())
        assert pool_limits.rest_ending_for(sid) is not None
    finally:
        for k in (("sub-r", "fable"), ("sub-r", "opus")):
            sp._scoped_rests.pop(k, None)
        sp._session_subscriptions.pop(sid, None)
        sp._session_binding_ctx.pop(sid, None)
        pool_state._session_models.pop(sid, None)


def test_only_a_rest_a_window_backs_holds_another_terminal_on_the_account():
    """A plain quarter-hour cooldown (a refusal the reading cannot explain, a row the session wrote) holds
    nothing for another terminal on the account: the pool falls back to it as on 1.9.1. A rest an
    account-wide window backs holds every terminal on it until its reset."""
    from services.engines import pool_rests, pool_state
    sid = "s-cool"
    sp._session_subscriptions[sid] = "sub-c"
    sp._session_binding_ctx[sid] = ("claude-code-cli", "")
    pool_state._session_models[sid] = "claude-opus-5-5"
    try:
        pool_rests._throttled_until["sub-c"] = time.time() + 900
        pool_rests._throttled_hard.add("sub-c")
        assert pool_limits.rest_ending_for(sid) is None                     # the plain cooldown
        pool_rests._window_rests["sub-c"] = ("seven_day", time.time())
        assert pool_limits.rest_ending_for(sid) is not None                         # the week's window backs it
        pool_rests._throttled_until["sub-c"] = time.time() - 1
        assert pool_limits.rest_ending_for(sid) is None                     # its reset passed
    finally:
        pool_rests._throttled_until.pop("sub-c", None)
        pool_rests._throttled_hard.discard("sub-c")
        pool_rests._window_rests.pop("sub-c", None)
        sp._session_subscriptions.pop(sid, None)
        sp._session_binding_ctx.pop(sid, None)
        pool_state._session_models.pop(sid, None)


@pytest.fixture
def resting_account():
    """The pool's memory for terminal ``s-term`` on account ``sub-w``, cleared after the test; the test rests
    the account as a limit leaves it (``rest(window=…)``, a plain cooldown with ``window=""``)."""
    from services.engines import pool_rests

    def rest(*, window="seven_day", rested_at=None, until_in=3600.0):
        pool_rests._throttled_until["sub-w"] = time.time() + until_in
        pool_rests._throttled_hard.add("sub-w")
        if window:
            pool_rests._window_rests["sub-w"] = (window, time.time() if rested_at is None else rested_at)
        else:
            pool_rests._window_rests.pop("sub-w", None)
        return pool_rests._throttled_until["sub-w"]
    sp._session_subscriptions["s-term"] = "sub-w"
    try:
        yield rest
    finally:
        pool_rests._throttled_until.pop("sub-w", None)
        pool_rests._throttled_hard.discard("sub-w")
        pool_rests._window_rests.pop("sub-w", None)
        sp._session_subscriptions.pop("s-term", None)


def test_a_terminal_with_no_mark_reads_its_accounts_window_rest_as_its_limit(resting_account):
    """One reading of "at its limit" on the session (``limit_ending``), which the queue, the voice door and the
    borrowed round ask: the terminal's own mark while it stands, else its account's rest for it, as a limit with
    the rest's end. Another session filled the account, or the server restarted: no mark, the rest stands. A
    plain cooldown is no limit."""
    term = _terminal(False, 0.0)
    assert term.limit_ending() is None
    resting_account(window="")
    assert term.limit_ending() is None                                 # a plain cooldown
    until = resting_account()
    ending = term.limit_ending()
    assert ending.reason == turn_ending.LIMIT and ending.window == "seven_day"
    assert pool_limits._iso_epoch(ending.resets_at) == pytest.approx(until, abs=1)
    term._prompt_queue.append({"text": "a result", "source": "delegate_result"})
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        assert term._prompt_gates_blocked() == "at_limit"


def test_a_turn_the_engine_answered_ends_the_accounts_hold_on_that_terminal(monkeypatch, resting_account):
    """A person left Fable by ``/model`` after Fable's limit: the pool still reads the terminal's family as Fable
    (``_session_models``), which rests for days. Their next turn the engine answers ends both halves of the hold
    for that terminal: the mark and the account's rest, as that terminal reads it. A rest that began after the
    answered turn holds again, and an interrupt's marker (no answer) releases nothing. Another terminal on the
    account keeps what it had."""
    from services.engines import pool_rests, pool_state
    term, _seen, _spawned = _folding_terminal(monkeypatch)
    sp._session_binding_ctx["s-term"] = ("claude-code-cli", "")
    pool_state._session_models["s-term"] = "claude-fable-5-2"
    sp._session_subscriptions["s-other"] = "sub-w"
    sp._session_binding_ctx["s-other"] = ("claude-code-cli", "")
    pool_state._session_models["s-other"] = "claude-fable-5-2"
    other = _terminal(False, 0.0)
    other.session_id = "s-other"
    pool_rests._scoped_rests[("sub-w", "fable")] = (time.time() + 86400, time.time() - 60)
    try:
        assert term.limit_ending().window == "scoped:fable"
        term._fold_tail_result({"last_signal": "end_turn", "turn_complete": False})   # an interrupt's marker
        assert term.limit_ending() is not None
        term._fold_tail_result({"last_signal": "end_turn", "turn_complete": True})    # the engine answered
        assert term.limit_ending() is None
        assert other.limit_ending().window == "scoped:fable"                         # its own reading stands
        pool_rests._scoped_rests[("sub-w", "fable")] = (time.time() + 86400, time.time() + 1)
        assert term.limit_ending().window == "scoped:fable"                          # a later rest holds again
        resting_account(rested_at=time.time() - 60)
        pool_rests._scoped_rests.pop(("sub-w", "fable"))
        assert term.limit_ending() is None                                           # the account's window too
        assert other.limit_ending().window == "seven_day"
    finally:
        pool_rests._scoped_rests.pop(("sub-w", "fable"), None)
        for sid in ("s-term", "s-other"):
            sp._session_binding_ctx.pop(sid, None)
            pool_state._session_models.pop(sid, None)
        sp._session_subscriptions.pop("s-other", None)


def test_a_round_borrowed_onto_a_terminal_whose_account_rests_ends_at_once_with_the_limit(resting_account):
    """A follow-up round through a terminal with no mark whose account rests on a window is not typed and does
    not wait for the backstop: it returns at once, and the run reads the limit with the rest's end."""
    from services.scheduler import interactive
    term = _terminal(False, 0.0)
    term.queue_prompt = lambda *a, **kw: pytest.fail("nothing is typed into a terminal whose account rests")
    until = resting_account()
    assert asyncio.run(asyncio.wait_for(interactive._run_borrowed_terminal_turn(term, "follow up"), 5)) == 0
    held = term.limit_ending()
    assert held.reason == turn_ending.LIMIT and pool_limits._iso_epoch(held.resets_at) == pytest.approx(until, abs=1)


def test_an_open_limit_menu_gets_one_escape_before_the_continuation_is_typed():
    written: list = []
    term = _driven(_terminal(False, 0.0), written)
    term._limit_menu = True
    term._prompt_queue.append({"text": "go on", "source": ACCOUNT_LIMIT_SOURCE})
    with patch("core.session.session_state.get_subagent_registry", return_value=MagicMock(has_pending=False)), \
            patch("core.session.session_state._session_permission_requests", {}):
        asyncio.run(term._try_drain_prompt_queue())
        assert written == [b"\x1b"] and len(term._prompt_queue) == 1
        asyncio.run(term._try_drain_prompt_queue())
    assert written == [b"\x1b", "go on"]


def _limited_isess(chat_id="c-lim-t", sid="s-lim-t"):
    from core.session.interactive_turn import LimitMark
    ended: list = []
    isess = SimpleNamespace(chat_id=chat_id, session_id=sid, user_sub="user-admin", otodock_attached=False,
                            last_ending=_FABLE_LIMIT, limit_mark=LimitMark.of(_FABLE_LIMIT), agent_name="a1",
                            alive=True, _a_tasks_terminal=lambda: False, _chat_owner=lambda: "user-admin",
                            end_waiters_with=ended.append)
    isess.clear_limit_mark = lambda why: setattr(isess, "limit_mark", None)
    isess.limit_ending = lambda: isess.limit_mark.ending if isess.limit_mark and isess.limit_mark.holds() else None
    return isess, ended


@pytest.mark.asyncio
async def test_a_terminal_with_no_account_to_move_to_gets_the_card_and_waits_for_one(temp_db, monkeypatch):
    """The terminal's limit through the pump's own owner (``follow_ending``): no other account serves it, so
    the chat gets the card (a row and a frame), what waits on the turn ends with the limit, and its mark keeps
    that only an account was missing."""
    isess, ended = _limited_isess()
    temp_db.create_chat(isess.chat_id, "user-admin", "a1")
    sp._session_binding_ctx[isess.session_id] = ("claude-code-cli", "")
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "_rest_by_reading", lambda sid, ending, from_row: ending)
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: False)
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    frames: list = []
    monkeypatch.setattr("core.session.session_state.broadcast_chat_frame", lambda cid, f: frames.append(f))
    await pool_limits.terminal_limit(isess, _FABLE_LIMIT)
    rows = [r for r in temp_db.get_chat_messages(isess.chat_id) if "turn_ended" in (r.get("event_data") or "")]
    assert len(rows) == 1 and frames and frames[0]["reason"] == "limit"
    assert ended == [_FABLE_LIMIT] and isess.limit_mark.waits_for_account


@pytest.mark.asyncio
@pytest.mark.parametrize("closes", [True, False])
async def test_a_limits_row_a_person_typed_after_schedules_no_continuation(temp_db, monkeypatch, closes):
    """One batch holds the limit's row and then the person's next message (``closes`` false): no turn waits to
    be continued, so another account serving schedules nothing and the card shows. A row that closed the turn
    continues as before."""
    isess, ended = _limited_isess("c-typed", "s-typed")
    temp_db.create_chat(isess.chat_id, "user-admin", "a1")
    sp._session_binding_ctx[isess.session_id] = ("claude-code-cli", "")
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))
    monkeypatch.setattr(pool_limits, "_rest_by_reading", lambda sid, ending, from_row: ending)
    monkeypatch.setattr(pool_limits, "limit_continues", lambda sid: True)
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    scheduled: list = []
    monkeypatch.setattr(chat_reload, "schedule_for_limit", lambda cid, **kw: scheduled.append(cid) or True)
    monkeypatch.setattr("core.session.session_state.broadcast_chat_frame", lambda cid, f: None)
    await pool_limits.terminal_limit(isess, _FABLE_LIMIT, closes=closes)
    rows = [r for r in temp_db.get_chat_messages(isess.chat_id) if "turn_ended" in (r.get("event_data") or "")]
    if closes:
        assert scheduled == ["c-typed"] and rows == [] and ended == []
    else:
        assert scheduled == [] and len(rows) == 1 and ended == [_FABLE_LIMIT]


@pytest.mark.asyncio
async def test_a_failure_in_what_follows_a_limit_still_ends_what_waits_on_the_turn(temp_db, monkeypatch):
    """A step of what follows the limit raises (here the reading's): a task's run or a borrowed round still ends
    with the limit at once, never after the two-hour backstop, and the mark still takes the ending."""
    isess, ended = _limited_isess("c-fail-t", "s-fail-t")
    temp_db.create_chat(isess.chat_id, "user-admin", "a1")
    sp._session_binding_ctx[isess.session_id] = ("claude-code-cli", "")
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-a", ""))

    def _broken(sid, ending, from_row):
        raise KeyError("a reading's window")
    monkeypatch.setattr(pool_limits, "_rest_by_reading", _broken)
    with pytest.raises(KeyError):
        await pool_limits.terminal_limit(isess, _FABLE_LIMIT)
    assert ended == [_FABLE_LIMIT]
    assert isess.limit_mark.ending == _FABLE_LIMIT and not isess.limit_mark.waits_for_account


def test_the_fold_tells_what_follows_a_limit_whether_its_row_closed_the_turn(monkeypatch):
    term, _seen, _spawned = _folding_terminal(monkeypatch)
    calls: list = []
    monkeypatch.setattr(sp, "terminal_limit", lambda isess, ending, **kw: calls.append(kw) or asyncio.sleep(0))
    term._fold_tail_result({"last_signal": "user", "ending": _FABLE_LIMIT.as_dict()})
    term._fold_tail_result({"last_signal": "end_turn", "ending": _FABLE_LIMIT.as_dict()})
    assert calls == [{"closes": False}, {"closes": True}]


@pytest.mark.asyncio
async def test_a_re_home_continues_a_turn_that_waited_for_an_account_once(monkeypatch):
    isess, _ended = _limited_isess()
    isess.limit_mark.waits_for_account = True
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: isess)
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    scheduled: list = []
    monkeypatch.setattr(chat_reload, "schedule_for_limit",
                        lambda cid, **kw: scheduled.append({"chat": cid, **kw}) or True)
    await pool_limits._account_freed(isess.session_id, "sub-old")
    await pool_limits._account_freed(isess.session_id, "sub-old")      # a second re-home: nothing more
    assert scheduled == [{"chat": "c-lim-t", "person": "user-admin", "session_id": "s-lim-t",
                          "old_sub": "sub-old", "in_place": True}]
    assert isess.limit_mark is None


@pytest.mark.asyncio
async def test_a_re_home_of_a_limit_that_refused_its_continuation_only_ends_the_hold(monkeypatch):
    isess, _ended = _limited_isess()          # waits_for_account False: refused for another reason
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: isess)
    monkeypatch.setattr(chat_reload, "may_continue_after_limit", lambda cid, person: True)
    monkeypatch.setattr(chat_reload, "schedule_for_limit", lambda cid, **kw: pytest.fail("no continuation"))
    await pool_limits._account_freed(isess.session_id, "sub-old")
    assert isess.limit_mark is None


def test_a_stop_ends_a_terminals_wait_for_an_account(monkeypatch):
    isess, _ended = _limited_isess("c-stop-t")
    isess.limit_mark.waits_for_account = True
    isess._prompt_queue, isess.may_drive = [], (lambda sub: sub == "user-admin")
    monkeypatch.setattr("core.session.interactive_session.find_live_for_chat", lambda cid: isess)
    monkeypatch.setattr("storage.database.get_chat", lambda cid: {"id": cid, "agent": "a1", "user_sub": "user-admin"})
    monkeypatch.setattr("auth.providers.effective_role_of", lambda sub, agent: "editor")
    monkeypatch.setattr(chat_reload.chat_move, "may_drive", lambda chat, asker: True)
    assert asyncio.run(pool_limits.stop_continuation("c-stop-t", "user-admin")) is True
    assert not isess.limit_mark.waits_for_account


def test_a_run_reads_its_terminals_limit_and_a_round_queued_onto_one_ends_at_once():
    from services.scheduler import interactive
    from services.scheduler.task_run import TaskRun
    isess, _ended = _limited_isess()
    assert interactive.terminal_ending(isess) == _FABLE_LIMIT
    isess.queue_prompt = lambda *a, **kw: pytest.fail("nothing is typed into a terminal at its limit")
    assert asyncio.run(interactive._run_borrowed_terminal_turn(isess, "follow up")) == 0
    run = SimpleNamespace(pump=None, terminal_ending=_FABLE_LIMIT, final_status="completed", final_output="",
                          run_id="run-t")
    TaskRun._engine_failure(run)
    assert run.final_status == "failed" and run.engine_ending == _FABLE_LIMIT and run.cut_short == "limit"


# ---------------------------------------------------------------------------
# One model's limit rests that model only (by the account's reading)
# ---------------------------------------------------------------------------

# Ahead of the run's own clock: a rest ends at its reset, so a fixed date stops holding the day it passes.
_RESET = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)


def _reading(week: float, fable: float, *, opus: float | None = None):
    from core.engine_records import Scoped, Window, Windows
    from services.engines import subscription_windows
    reset = _RESET
    scoped = [Scoped(key="fable", label="Fable", pct=fable, resets_at=reset)]
    if opus is not None:
        scoped.append(Scoped(key="opus", label="Opus", pct=opus, resets_at=reset))
    return Windows(specs=subscription_windows.window_specs("claude-code-cli"),
                   windows={"five_hour": Window(pct=10.0, resets_at=reset - timedelta(days=4)),
                            "seven_day": Window(pct=week, resets_at=reset)},
                   scoped=scoped)


@pytest.fixture
def _bound(monkeypatch):
    from services.engines import pool_rests, pool_state
    monkeypatch.setattr(pool_limits, "_read_after_limit", lambda sub: None)
    monkeypatch.setattr(pool_limits, "schedule_rebalance", lambda why: None)
    monkeypatch.setattr(pool_limits, "_queue_write", lambda *a, **kw: None)
    monkeypatch.setattr(pool_rests, "_queue_write", lambda *a, **kw: None, raising=False)

    def bind(sid, model):
        sp._session_subscriptions[sid] = "sub-m"
        sp._session_binding_ctx[sid] = ("claude-code-cli", "")
        pool_state._session_models[sid] = model
    yield bind
    for d in (sp._session_subscriptions, sp._session_binding_ctx, pool_state._session_models):
        for k in [k for k in d if str(k).startswith("s-m")]:
            d.pop(k, None)
    for k in [k for k in sp._scoped_rests if k[0] == "sub-m"]:
        sp._scoped_rests.pop(k, None)
    for d in (pool_rests._throttled_until, pool_rests._window_rests):
        d.pop("sub-m", None)
    pool_rests._throttled_hard.discard("sub-m")


_OVERAGE = turn_ending.TurnEnding(reason=turn_ending.LIMIT, resets_at=_RESET.isoformat(),
                                  detail="You're out of usage credits.", window="")


def test_the_saved_event_rests_fable_only_and_an_opus_start_still_gets_the_account(_bound, monkeypatch):
    """The week at 82 %, Fable at 100 %, a type that names no family: Fable rests, nothing else."""
    from services.engines import pool_select
    _bound("s-m-fable", "claude-fable-5-2")
    monkeypatch.setattr(pool_limits, "_reading_of", lambda sub: _reading(82.0, 100.0, opus=40.0))
    out = pool_limits._rest_by_reading("s-m-fable", _OVERAGE, from_row=True)
    assert out.window == "scoped:fable"
    assert pool_select._scope_resting("sub-m", "fable") and not pool_select._scope_resting("sub-m", "opus")
    assert not sp._is_throttled("sub-m")


def test_a_full_week_rests_the_account(_bound, monkeypatch):
    _bound("s-m-week", "claude-fable-5-2")
    monkeypatch.setattr(pool_limits, "_reading_of", lambda sub: _reading(100.0, 100.0))
    pool_limits._rest_by_reading("s-m-week", _OVERAGE, from_row=False)
    assert sp._is_throttled("sub-m")


def test_a_reading_that_does_not_explain_the_refusal_rests_the_account(_bound, monkeypatch):
    """An Opus session refused while only Fable is full: the reading does not explain it."""
    _bound("s-m-opus", "claude-opus-5-5")
    monkeypatch.setattr(pool_limits, "_reading_of", lambda sub: _reading(82.0, 100.0, opus=40.0))
    pool_limits._rest_by_reading("s-m-opus", _OVERAGE, from_row=True)
    assert sp._is_throttled("sub-m") and not sp._scoped_rests.get(("sub-m", "fable"))


def test_a_row_naming_a_window_its_reading_does_not_back_rests_no_longer_than_before(_bound, monkeypatch):
    """A terminal's row is a file the session can write: a five-hour window the reading shows at 10 % costs
    the account the usual quarter hour, never five hours."""
    from services.engines import pool_rests
    _bound("s-m-forged", "claude-fable-5-2")
    monkeypatch.setattr(pool_limits, "_reading_of", lambda sub: _reading(30.0, 30.0))
    forged = turn_ending.TurnEnding(reason=turn_ending.LIMIT, resets_at="2099-01-01T00:00:00+00:00",
                                    window="five_hour")
    pool_limits._rest_by_reading("s-m-forged", forged, from_row=True)
    assert 0 < pool_rests._throttled_until["sub-m"] - time.time() <= pool_rests._THROTTLE_COOLDOWN_S + 1


@pytest.mark.asyncio
async def test_both_doors_repair_a_refused_sign_in(monkeypatch):
    """What follows a turn refused for its sign-in, the pump's
    (``follow_ending``) and a terminal's (``_after_ending``) alike: the one
    repair (``pool_tokens.repair_sign_in_soon``)."""
    from types import SimpleNamespace

    from core.events import turn_ending
    from core.session.interactive_turn import InteractiveTurnMixin
    from services.engines import pool_limits, pool_tokens
    asked: list[str] = []
    monkeypatch.setattr(pool_tokens, "repair_sign_in_soon", asked.append)

    class _Closed(Exception):
        pass

    def _close():
        raise _Closed
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: ("sub-x", None))
    monkeypatch.setattr(pool_limits, "rest_after_limit", lambda *a: None)
    refused = turn_ending.TurnEnding(reason=turn_ending.ERROR, detail="refused", sign_in=True)
    with pytest.raises(_Closed):
        await pool_limits.follow_ending(SimpleNamespace(session_id="s-pump", terminal=False, close_turn=_close),
                                        refused, refused, {}, "refused")
    ended: list = []
    terminal = SimpleNamespace(session_id="s-term", end_waiters_with=ended.append)
    InteractiveTurnMixin._after_ending(terminal, refused, closes=True)
    assert asked == ["s-pump", "s-term"] and ended == [refused]
    # Any other error repairs nothing.
    other = turn_ending.TurnEnding(reason=turn_ending.ERROR, detail="boom")
    InteractiveTurnMixin._after_ending(terminal, other, closes=True)
    assert asked == ["s-pump", "s-term"]

