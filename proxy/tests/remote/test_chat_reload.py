"""The reload of a chat's session at its agent's request
(``services/remote/chat_reload``, its route in ``api/agents/session_reload``):
who may ask, what the waiter waits for, the close under its hold, the note,
the account choice, the resume turn on a session built as the person's own
warm-up builds it, and the bounds."""

import asyncio
import json
import uuid

import pytest

from core.session import chat_rebuild, session_kind
from services.remote import chat_move, chat_reload
from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer,
    make_test_agent,
    stub_dashboard_seams,
)

SYSTEM = "system"
RELOADED = "session_reloaded"


def _user(sub, role="member"):
    from storage import database as db
    db.upsert_user(sub, f"{sub}@t.io", sub, role, display_name=sub)


def _chat(agent, owner, *, sid="s-live", pin="local", **fields):
    from storage import database as db
    cid = str(uuid.uuid4())
    db.create_chat(cid, owner, agent, "default", execution_path="claude-code-cli")
    db.update_chat(cid, execution_target=pin, session_id=sid, **fields)
    return cid


def _built(sid, agent, *labels, owner="u-ann", mcp="mail-x"):
    """The session ``sid`` was built on the agent's own accounts ``labels``
    of ``mcp``, lent by ``owner`` (what the build notes)."""
    from core.session import session_state
    session_state.note_built_agent_accounts(sid, agent, tuple((mcp, owner, label) for label in labels))


def _asker(sub="u-ann", role="manager"):
    return chat_move.Asker(sub=sub, role=role, platform_admin=False)


def _notes(cid) -> list[tuple[str, str]]:
    from storage import database as db
    out = []
    for m in db.get_chat_messages(cid):
        if m.get("event_type") == SYSTEM:
            block = json.loads(m.get("event_data") or "{}")
            if block.get("subtype") == RELOADED:
                out.append((block.get("message"), block.get("reason")))
    return out


@pytest.fixture
def world(temp_db, monkeypatch):
    """One person, one agent, a chat with a live headless session, the
    ladder recorded (nothing spawns), the poll short."""
    from core.session import session_delivery, session_state
    from storage import database as task_store
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    layer.alive.add("s-live")
    _user("u-ann")
    agent = make_test_agent()
    task_store.add_user_agent("u-ann", agent, "manager", "test")
    asked: list = []
    frames: list = []
    answer = {"path": "oneshot"}

    async def deliver_prompt(chat_id, text, **kw):
        asked.append({"chat_id": chat_id, "text": text, **kw})
        if kw.get("persist_event"):
            kw["persist_event"](chat_id)
        outcome = session_delivery.DeliveryOutcome(answer["path"], chat_id=chat_id)
        if kw.get("on_outcome"):
            await kw["on_outcome"](outcome)
        return outcome

    monkeypatch.setattr(session_delivery, "deliver_prompt", deliver_prompt)
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda cid, frame: frames.append(frame))
    monkeypatch.setattr(chat_move, "TURN_POLL_S", 0.01)
    chat_reload._recent.clear()
    chat_reload._waiters.clear()

    class World:
        pass

    w = World()
    w.layer, w.agent, w.asked, w.frames, w.answer = layer, agent, asked, frames, answer
    w.cid = _chat(agent, "u-ann")
    return w


def _reload(w, *, cid=None, asker=None, reason="the Notion tool was enabled", choices=None,
            mark=None, blocked=None):
    from storage import database as db
    cid = cid or w.cid

    async def go():
        m = mark if mark is not None else chat_reload._mark(db.get_chat(cid) or {})
        await chat_reload._wait_and_reload(cid, asker or _asker(), reason, choices or {}, m,
                                           blocked)
        for _ in range(20):             # the lane's rows land
            await asyncio.sleep(0.01)
    asyncio.run(go())


# -- the reload itself -------------------------------------------------------------

def test_a_reload_closes_the_session_notes_it_and_continues_the_chat(world):
    from storage import database as db
    db.update_chat(world.cid, permission_mode="autoReview")
    _reload(world)
    assert world.layer.closed_sessions == ["s-live"]
    assert _notes(world.cid) == [("The session was reloaded: the Notion tool was enabled.", "")]
    [call] = world.asked
    assert call["text"] == ("The session was reloaded: the Notion tool was enabled. "
                            "Continue where you were.")
    assert (call["source"], call["user_sub"], call["role"]) == ("reload", "u-ann", "manager")
    assert call["allow_ws"] is False and call["allow_pty"] is False
    # The session it starts is the one the person's own warm-up builds, in
    # the chat's own mode.
    assert call["oneshot_fn"].keywords == {
        "permission_mode": "autoReview", "client_type": session_kind.DASHBOARD.name,
        "build": chat_rebuild.AS_CHAT, "fresh_without_session": True, "driven_by": "reload"}
    wakes = [json.loads(m["event_data"]) for m in db.get_chat_messages(world.cid)
             if m.get("event_type") == "schedule_wake"]
    assert [w["source"] for w in wakes] == ["reload"]
    # Whoever views the chat got the note live.
    assert [f["subtype"] for f in world.frames] == [RELOADED]


def test_the_hold_is_given_back_before_the_chat_continues(world, monkeypatch):
    from core.events import input_queue
    from core.session import session_delivery, warmup_registry
    seen = {}

    async def deliver_prompt(chat_id, text, **kw):
        seen["claimed"] = input_queue.is_claimed(chat_id)
        seen["registered"] = warmup_registry.get(chat_id) is not None
        return session_delivery.DeliveryOutcome("oneshot", chat_id=chat_id)

    monkeypatch.setattr(session_delivery, "deliver_prompt", deliver_prompt)
    _reload(world)
    # The ladder's guards read a held chat as someone else's turn.
    assert seen == {"claimed": False, "registered": False}


def test_a_reload_waits_for_the_turn_to_end(world, monkeypatch):
    opens = {"left": 3}

    def _open(chat):
        opens["left"] -= 1
        return opens["left"] > 0

    monkeypatch.setattr(chat_move, "open_turn", _open)
    _reload(world)
    assert opens["left"] == 0 and world.layer.closed_sessions == ["s-live"]


def test_a_reload_that_comes_due_under_a_planned_stop_waits_and_closes_nothing(world, monkeypatch):
    """A close now would leave a session the hold starts nothing on, and the
    chat would read "did not continue". It waits with its row, and the boot or
    the resume takes it up."""
    from core import drain
    closed_while_held: list[list[str]] = []
    holds = {"left": 3}

    def _holding():
        holds["left"] -= 1
        if holds["left"] >= 0:
            closed_while_held.append(list(world.layer.closed_sessions))
        return holds["left"] >= 0
    monkeypatch.setattr(drain, "holding", _holding)
    monkeypatch.setattr(chat_move, "TURN_POLL_S", 0.0)
    _reload(world)
    assert closed_while_held == [[], [], []]
    assert world.layer.closed_sessions == ["s-live"]


def test_a_turn_that_opens_under_the_hold_sends_the_reload_back_to_waiting(world, monkeypatch):
    from core.events import stream_pump
    real = chat_rebuild.turn_open_while_held
    state = {"n": 0}

    def _busy_once(chat):
        state["n"] += 1
        return state["n"] == 1 or real(chat)

    monkeypatch.setattr(chat_rebuild, "turn_open_while_held", _busy_once)
    _reload(world)
    assert state["n"] >= 2 and world.layer.closed_sessions == ["s-live"]
    assert stream_pump._active_pumps.get(world.cid) is None


def test_a_session_started_since_the_call_satisfies_the_reload(world, monkeypatch):
    from core.session import session_state
    # The same id, a later life: its token's floor moved.
    monkeypatch.setitem(session_state._session_token_floor, "s-live", 5)
    _reload(world, mark=("s-live", 1))
    assert world.layer.closed_sessions == [] and world.asked == []
    assert _notes(world.cid) == [("The session was reloaded: the Notion tool was enabled.", "")]


def test_an_account_choice_is_never_satisfied_by_an_earlier_start(world, monkeypatch):
    from storage.identity import credential_store
    wrote: list = []
    monkeypatch.setattr(credential_store, "set_chat_account_choice",
                        lambda *a: wrote.append(a) or True)
    monkeypatch.setattr(credential_store, "clear_chat_account_choice",
                        lambda *a: wrote.append(("clear", *a)))
    from core.session import session_state
    monkeypatch.setitem(session_state._session_token_floor, "s-live", 5)
    _reload(world, mark=("s-live", 1), choices={"mail-x": "home", "drive-x": ""})
    assert world.layer.closed_sessions == ["s-live"]
    assert wrote == [(world.cid, "u-ann", world.agent, "mail-x", "home"),
                     ("clear", world.cid, "u-ann", "drive-x")]


def _tools(monkeypatch, **labels):
    """The agent's tools by name, each with its label (and nothing else)."""
    from types import SimpleNamespace

    from services.mcp import mcp_registry
    manifests = {name: SimpleNamespace(name=name, label=label) for name, label in labels.items()}
    monkeypatch.setattr(mcp_registry, "get_manifest", manifests.get)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements",
                        lambda agent: list(manifests.values()))


def test_the_note_says_in_the_platforms_words_which_account_the_chat_uses(world, monkeypatch):
    """The reason is the agent's text. The sentence that names the account
    is the platform's: one for each choice written, none for a label that
    left the set since the call."""
    from storage import database as db
    from storage.identity import credential_store
    _tools(monkeypatch, **{"mail-x": "Mail X", "drive-x": "Drive X", "cal-x": ""})
    monkeypatch.setattr(credential_store, "set_chat_account_choice",
                        lambda cid, sub, agent, tool, label: label != "gone")
    monkeypatch.setattr(credential_store, "clear_chat_account_choice", lambda *a: None)
    _reload(world, reason="refreshing tools",
            choices={"mail-x": "work", "drive-x": "", "cal-x": "gone"})
    said = ("The session was reloaded: refreshing tools. This chat now uses work for Mail X. "
            "This chat is back on the agent's default account for Drive X.")
    assert _notes(world.cid) == [(said, "")]
    # The agent reads the same sentences at the head of its resumed turn.
    [call] = world.asked
    assert call["text"] == f"{said} Continue where you were."
    wake = [json.loads(m["event_data"]) for m in db.get_chat_messages(world.cid)
            if m.get("event_type") == "schedule_wake"]
    assert wake[0]["prompt"] == call["text"]


def test_a_choice_is_written_on_a_machine_the_chat_moved_to(world, monkeypatch):
    """Between the call and the fire the chat moved to a machine: every
    tool's choice is written there, a tool that keeps its sign-in in files
    too (its session there has a folder of its own)."""
    from core.remote import satellite_connection
    from storage.identity import credential_store
    _tools(monkeypatch, **{"files-x": "Files X", "mail-x": "Mail X"})
    monkeypatch.setattr(satellite_connection.get_connection_manager(), "is_connected",
                        lambda mid: True)
    wrote: list = []
    monkeypatch.setattr(credential_store, "set_chat_account_choice",
                        lambda *a: wrote.append(a[3:]) or True)
    far = _chat(world.agent, "u-ann", sid="s-far", pin="m-ann")
    _reload(world, cid=far, choices={"files-x": "work", "mail-x": "work"})
    assert wrote == [("files-x", "work"), ("mail-x", "work")]
    assert _notes(far) == [("The session was reloaded: the Notion tool was enabled. "
                            "This chat now uses work for Files X. This chat now uses work for Mail X.", "")]


def test_a_message_that_waits_goes_first_and_a_stopped_turn_stays_stopped(world, monkeypatch):
    from storage import database as db
    monkeypatch.setattr(chat_move, "_waiting_message_goes_first", lambda chat: True)
    _reload(world)
    assert world.layer.closed_sessions == ["s-live"] and world.asked == []
    monkeypatch.setattr(chat_move, "_waiting_message_goes_first", lambda chat: False)
    other = _chat(world.agent, "u-ann", sid="s-two", last_turn_aborted=True)
    world.layer.alive.add("s-two")
    _reload(world, cid=other)
    assert "s-two" in world.layer.closed_sessions and world.asked == []
    assert db.get_chat(other)["last_turn_aborted"]


def test_a_nudge_no_rung_delivered_is_taken_back(world):
    from storage import database as db
    world.answer["path"] = "none"
    _reload(world)
    assert not [m for m in db.get_chat_messages(world.cid) if m.get("event_type") == "schedule_wake"]
    assert _notes(world.cid)[-1] == (
        "The chat did not continue on its own. Send a message to continue.", "resume_skipped")


def test_a_chat_surface_workers_round_takes_the_runs_own_choice(world):
    """``choice_scope(task=True)`` takes a task's chat, and a dashboard chat
    a worker's round works in (a chat-surface delegate); a plain dashboard
    chat is no run's."""
    from services.mcp import account_context
    worker = _chat(world.agent, "u-ann", sid="s-worker", parent_chat_id=world.cid)
    assert account_context.choice_scope(worker, mount_scope="user", role="manager", task=True) == "user"
    assert account_context.choice_scope(world.cid, mount_scope="user", role="manager", task=True) == ""
    # The person's own reload of the worker chat still leaves it to its rounds.
    assert account_context.choice_scope(worker, mount_scope="user", role="manager") == ""


def test_the_flip_drops_the_agents_idle_app_managers(world, monkeypatch):
    from services.apps import headless_exec
    dropped: list[str] = []

    async def drop_idle(agent):
        dropped.append(agent)
        return 0

    monkeypatch.setattr(headless_exec, "drop_idle", drop_idle)
    asyncio.run(chat_reload.team_account_changed(world.agent, lends=True))
    assert dropped == [world.agent]


def test_the_fourth_reload_in_a_row_stops(world):
    from storage import database as db
    for n in range(chat_reload.CHAIN_MAX):
        world.layer.alive.add("s-live")
        _reload(world, reason=f"round {n}")
    world.layer.closed_sessions.clear()
    world.asked.clear()
    world.layer.alive.add("s-live")
    _reload(world, reason="again")
    assert world.layer.closed_sessions == [] and world.asked == []
    assert _notes(world.cid)[-1] == (
        "The agent reloaded this chat 3 times in a row with no message from you, so it stops "
        "here. Send a message to continue.", "resume_stopped")
    # A person's message opens a new row of reloads.
    db.add_chat_message(world.cid, "user", "go on", author_sub="u-ann")
    _reload(world, reason="after the person")
    assert world.layer.closed_sessions == ["s-live"]


def test_the_hour_bound_counts_in_this_process(world):
    chat_reload._recent[world.cid] = [chat_reload.time.monotonic()] * chat_reload.HOUR_MAX
    _reload(world)
    assert world.layer.closed_sessions == []
    assert _notes(world.cid)[-1][1] == "resume_stopped"


def test_a_round_sent_back_to_waiting_costs_none_of_the_hours_count(world, monkeypatch):
    """A hold that finds the chat taken sends the reload back to waiting.
    Only a reload that happened is counted, and one that could not (an
    offline machine) is not."""
    from contextlib import asynccontextmanager

    from core.events import input_queue
    real, rounds = chat_rebuild.closed, []

    @asynccontextmanager
    async def busy_at_first(chat_id, *, reason, by, claim=None):
        rounds.append(1)
        if len(rounds) <= chat_reload.HOUR_MAX + 2:
            (await input_queue.loaded(chat_id)).release(claim, rearm=True)
            raise chat_rebuild.ChatBusy(chat_id)
        async with real(chat_id, reason=reason, by=by, claim=claim) as hold:
            yield hold

    monkeypatch.setattr(chat_rebuild, "closed", busy_at_first)
    _reload(world)
    assert len(rounds) == chat_reload.HOUR_MAX + 3
    assert world.layer.closed_sessions == ["s-live"] and len(world.asked) == 1
    assert _notes(world.cid) == [("The session was reloaded: the Notion tool was enabled.", "")]
    assert len(chat_reload._recent[world.cid]) == 1


def test_who_may_continue_the_chat_is_read_again_when_the_reload_fires(world):
    """An admin's turn in a member's chat asks for a reload, and the admin
    is a plain member by the time the turn ends: nothing is closed and the
    chat says why. The same for a person who lost the agent, on their own
    chat. The role the call captured decides nothing."""
    from storage import database as db
    _user("u-bob")
    db.add_user_agent("u-bob", world.agent, "viewer", "test")
    bobs = _chat(world.agent, "u-bob", sid="s-bob")
    world.layer.alive.update({"s-bob", "s-own"})
    stale = chat_move.Asker(sub="u-ann", role="admin", platform_admin=True)
    _reload(world, cid=bobs, asker=stale)
    refused = [(f"The session was not reloaded: {chat_reload.NOT_DRIVER}", "failed")]
    assert _notes(bobs) == refused
    _user("u-cat")
    own = _chat(world.agent, "u-cat", sid="s-own")
    _reload(world, cid=own, asker=_asker("u-cat", "manager"), choices={"mail-x": "home"})
    assert _notes(own) == refused
    assert world.layer.closed_sessions == [] and world.asked == []


def test_the_resume_runs_at_the_standing_of_the_fire(world):
    from storage import database as db
    db.set_user_agent_role("u-ann", world.agent, "editor")
    _reload(world, asker=_asker("u-ann", "manager"))
    assert [(a["user_sub"], a["role"]) for a in world.asked] == [("u-ann", "editor")]


def test_a_chat_that_is_gone_gets_no_note(world, monkeypatch):
    """The queue answers at once for a chat being deleted: the waiter ends
    there, with no line about a turn that did not end."""
    from core.events import input_queue
    notes: list = []

    async def _note(*a, **k):
        notes.append(a)

    monkeypatch.setattr(chat_reload, "_note", _note)
    monkeypatch.setattr(input_queue, "deleting", lambda cid: True)
    _reload(world)
    assert notes == [] and world.layer.closed_sessions == []


def test_a_machine_that_is_away_is_not_reloaded(world, monkeypatch):
    from core.remote import satellite_connection
    monkeypatch.setattr(satellite_connection.get_connection_manager(), "is_connected",
                        lambda mid: False)
    monkeypatch.setattr(chat_reload.chat_targets, "label_of", lambda pin: "Ann laptop")
    far = _chat(world.agent, "u-ann", sid="s-far", pin="m-ann")
    _reload(world, cid=far, choices={"mail-x": "home"})
    assert world.asked == [] and world.layer.closed_sessions == []
    assert _notes(far) == [("The session was not reloaded: Ann laptop is offline. "
                            "Ask again when it is back.", "failed")]


def test_a_refusal_that_arose_while_it_waited_is_noted(world, monkeypatch):
    monkeypatch.setattr(chat_move, "may_drive", lambda chat, asker: False)
    _reload(world)
    assert world.layer.closed_sessions == []
    assert _notes(world.cid) == [(f"The session was not reloaded: {chat_reload.NOT_DRIVER}",
                                  "failed")]
    monkeypatch.setattr(chat_move, "may_drive", lambda chat, asker: True)
    _reload(world, blocked=lambda cid: "in use")
    assert _notes(world.cid)[-1] == (
        f"The session was not reloaded: {chat_reload.VOICE_IN_USE}", "failed")


def test_a_terminal_chats_viewers_are_told_when_the_new_terminal_is_up(world, monkeypatch):
    from types import SimpleNamespace

    from core.session import interactive_session
    from storage import database as db
    db.update_chat(world.cid, execution_mode="interactive")
    monkeypatch.setattr(chat_reload, "_is_terminal_chat", lambda chat: True)
    live = {"n": 0}

    def _find(chat_id):
        live["n"] += 1
        return SimpleNamespace(alive=True, turn_open=False) if live["n"] > 6 else None

    async def go():
        monkeypatch.setattr(interactive_session, "find_live_for_chat", _find)
        chat_reload._watch_terminal(world.cid)
        for _ in range(60):
            await asyncio.sleep(0.05)
            if world.frames:
                return

    asyncio.run(go())
    assert world.frames == [{"type": SYSTEM, "subtype": RELOADED, "terminal": True,
                             "chat_id": world.cid}]


# -- the call ----------------------------------------------------------------------

def _schedule(w, **kw):
    kw = {"asker": _asker(), "reason": "a tool was enabled", **kw}
    cid = kw.pop("cid", w.cid)

    async def go():
        try:
            return await chat_reload.schedule_after_turn(cid, **kw)
        finally:
            chat_reload.cancel(cid)
            await asyncio.sleep(0)
    return asyncio.run(go())


def test_the_call_refuses_what_is_not_a_persons_chat_to_reload(world, monkeypatch):
    from core.session.visibility import task_chat_owner
    from storage import database as db

    def refused(**kw) -> str:
        with pytest.raises(chat_reload.ReloadRefused) as e:
            _schedule(world, **kw)
        return str(e.value)

    assert refused(cid="no-such-chat") == chat_move.NOT_FOUND
    assert refused(asker=_asker(sub="")) == chat_reload.NOBODY
    assert refused(blocked=lambda cid: "in use") == chat_reload.VOICE_IN_USE
    _user("u-bob")
    assert refused(asker=_asker("u-bob", "member")) == chat_reload.NOT_DRIVER
    run_chat = session_kind.task_chat_id("run-1")
    db.create_chat(run_chat, task_chat_owner(world.agent), world.agent, "auto",
                   execution_path="claude-code-cli")
    assert refused(cid=run_chat) == chat_reload.ONLY_A_CHAT
    call = str(uuid.uuid4())
    db.create_chat(call, "u-ann", world.agent, "default", execution_path="claude-code-cli",
                   source_type="phone")
    assert refused(cid=call) == chat_reload.A_CALL
    lane = _chat(world.agent, "u-ann", parent_chat_id=world.cid)
    assert refused(cid=lane) == chat_reload.DELEGATED_WORK
    monkeypatch.setitem(chat_move._running, world.cid, object())
    assert refused() == chat_reload.MOVING


def _set(monkeypatch, *labels):
    from types import SimpleNamespace

    from storage.identity import credential_store
    monkeypatch.setattr(credential_store, "list_agent_account_set",
                        lambda sub, mcp, agent: [SimpleNamespace(account_label=x) for x in labels])


def test_the_call_checks_an_account_choice_against_the_persons_set(world, monkeypatch):
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "home")
    assert _schedule(world, accounts={"mail-x": "home"})["accounts"] == {"mail-x": "home"}
    assert _schedule(world, accounts={"mail-x": "default"})["accounts"] == {"mail-x": ""}
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(world, accounts={"mail-x": "boss"})
    assert str(e.value) == ("boss is not one of the accounts the person lets this agent use "
                            "for Mail X.")
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(world, accounts={"drive-x": "home"})
    assert str(e.value) == "drive-x is not one of this agent's tools."


def test_an_account_labelled_default_is_that_account(world, monkeypatch):
    """A person's first account of a plain-credential tool is labelled
    ``default``. In the set beside the set's default ``work`` it is an
    account like any other: the switch to it records the choice. The word
    clears the choice only when no account carries it."""
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "default")
    assert _schedule(world, accounts={"mail-x": "default"})["accounts"] == {"mail-x": "default"}
    _set(monkeypatch, "work", "home")
    assert _schedule(world, accounts={"mail-x": "default"})["accounts"] == {"mail-x": ""}


def test_a_label_reads_as_one_line_and_is_chosen_by_the_name_the_prompt_shows(
        world, monkeypatch):
    """A label stored before the one-line rule may hold a line break or a
    backtick. The prompt shows it as one line, the switch takes that name
    and records the label as stored, and the platform's sentences carry the
    one-line form. Two labels that read the same are not guessed between."""
    from storage.identity import credential_store
    _tools(monkeypatch, **{"mail-x": "Mail\nX `beta`"})
    stored = "home\n`office`"
    _set(monkeypatch, "work", stored)
    assert _schedule(world, accounts={"mail-x": "home office"})["accounts"] == {"mail-x": stored}
    assert _schedule(world, accounts={"mail-x": stored})["accounts"] == {"mail-x": stored}
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(world, accounts={"mail-x": "boss\n`x`"})
    assert str(e.value) == ("boss x is not one of the accounts the person lets this agent use "
                            "for Mail X beta.")
    _set(monkeypatch, "home  office", stored)
    with pytest.raises(chat_reload.ReloadRefused):
        _schedule(world, accounts={"mail-x": "home office"})
    monkeypatch.setattr(credential_store, "set_chat_account_choice", lambda *a: True)
    _reload(world, reason="asked", choices={"mail-x": stored})
    assert _notes(world.cid) == [
        ("The session was reloaded: asked. This chat now uses home office for Mail X beta.", "")]


def test_the_route_bounds_the_accounts_it_takes():
    """One call names a handful of tools, each with a label no longer than a
    sign-in's: more, or longer, is refused at the body."""
    from pydantic import ValidationError

    from api.agents.session_reload import _MAX_ACCOUNTS, ReloadSessionRequest
    ok = ReloadSessionRequest(reason="asked", accounts={"mail-x": "l" * 128})
    assert ok.accounts == {"mail-x": "l" * 128}
    assert ReloadSessionRequest(reason="asked").accounts is None
    for bad in ({"mail-x": "l" * 129}, {"t" * 129: "home"}, {"mail-x": ""}, {"": "home"},
                {f"tool-{i}": "home" for i in range(_MAX_ACCOUNTS + 1)}):
        with pytest.raises(ValidationError):
            ReloadSessionRequest(reason="asked", accounts=bad)


def test_a_direct_llm_chat_switches_its_account(world, monkeypatch):
    """A Direct LLM session is started from the builder's tool config, so a
    chat's choice reaches its tools as on the other engines."""
    from storage import database as db
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "home")
    direct = _chat(world.agent, "u-ann", sid="s-direct")
    db.update_chat(direct, execution_path="direct-llm")
    assert _schedule(world, cid=direct, accounts={"mail-x": "home"})["accounts"] == {"mail-x": "home"}


def _agents_set(monkeypatch, *labels):
    from types import SimpleNamespace

    from storage.identity import service_account_set
    monkeypatch.setattr(service_account_set, "list_service_agent_set",
                        lambda mcp, agent, **kw: [SimpleNamespace(account_label=x) for x in labels])


def test_a_chat_that_runs_as_the_agent_takes_none_of_the_persons_accounts(world, monkeypatch):
    """A person's own chat on an agent that runs every person's chat in its
    space (a Shared-only one, the chat from before the switch) runs on the
    agent's own accounts: one of the person's own is refused in words."""
    from core.session import visibility
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent: True)
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "home")
    _agents_set(monkeypatch, "agent-acct")
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(world, accounts={"mail-x": "home"})
    assert str(e.value) == "home is not one of this agent's accounts for Mail X."
    assert _schedule(world, accounts={"mail-x": "agent-acct"})["accounts"] == {"mail-x": "agent-acct"}


def test_a_chat_outside_the_agents_space_never_switches_to_the_agents_accounts(world, monkeypatch):
    """In every other mode a person's chat runs as that person, the agent's
    pool chat too (a person driving it mounts their own space): a switch
    names only the person's own accounts, never one of the agent's set."""
    from core.session import visibility
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "home")
    _agents_set(monkeypatch, "agent-acct")
    pool = _chat(world.agent, visibility.shared_chat_owner(world.agent), sid="s-pool")
    for cid in (world.cid, pool):
        with pytest.raises(chat_reload.ReloadRefused) as e:
            _schedule(world, cid=cid, accounts={"mail-x": "agent-acct"})
        assert str(e.value) == ("agent-acct is not one of the accounts the person lets this "
                                "agent use for Mail X.")
    assert _schedule(world, cid=pool, accounts={"mail-x": "home"})["accounts"] == {"mail-x": "home"}


def test_a_team_accounts_chat_switches_among_the_agents_accounts_from_the_floor_up(world, monkeypatch):
    """The switch on: a person's own chat chooses among the agent's set, an
    editor or above; the owner below the floor is refused with the floor's
    sentence, and never offered their own."""
    from storage.agents import agent_store
    agent_store.update_agent(world.agent, team_account=True, team_account_floor="editor")
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "home")
    _agents_set(monkeypatch, "agent-acct")
    assert _schedule(world, accounts={"mail-x": "agent-acct"})["accounts"] == {"mail-x": "agent-acct"}
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(world, accounts={"mail-x": "home"})
    assert str(e.value) == "home is not one of this agent's accounts for Mail X."
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(world, asker=_asker("u-ann", "viewer"), accounts={"mail-x": "agent-acct"})
    assert str(e.value) == ("The agent's own accounts are for the editor role and above on this "
                            "agent. Ask a manager for that role.")
    agent_store.update_agent(world.agent, team_account=False)
    assert _schedule(world, accounts={"mail-x": "home"})["accounts"] == {"mail-x": "home"}


def _live(world, *sids):
    from services.agents import offboarding_sessions as closer
    return [closer.Live(sid, world.agent, closer.POOL, "manager", "u-ann", "u-ann") for sid in sids]


def test_the_team_account_switch_closes_the_idle_chats_and_tells_them(world, monkeypatch):
    """On, off, or the floor moved: every live session of a person's chat on
    the agent is closed once idle and the chat told one line; a worker's is
    left to its round."""
    from services.agents import offboarding_sessions as closer
    from ws import wire_events as wire
    world.layer.alive.add("s-worker")
    worker = _chat(world.agent, "u-ann", sid="s-worker", parent_chat_id=world.cid)
    monkeypatch.setattr(closer, "live_sessions", lambda: [
        *_live(world, "s-live"), closer.Live("s-worker", world.agent, closer.WORKER, "", "", "u-ann")])

    def flip(**kw):
        asyncio.run(chat_reload.team_account_changed(world.agent, **kw))
    flip(lends=True)
    assert world.layer.closed_sessions == ["s-live"]
    assert _notes(world.cid) == [(chat_reload.TEAM_ON, wire.MOVED_ACCOUNT_CHANGED)]
    assert _notes(worker) == []
    world.layer.alive.add("s-live")
    flip(lends=False)
    assert _notes(world.cid)[-1] == (chat_reload.TEAM_OFF, wire.MOVED_ACCOUNT_CHANGED)
    world.layer.alive.add("s-live")
    flip(lends=None)
    assert _notes(world.cid)[-1] == (chat_reload.TEAM_FLOOR_MOVED, wire.MOVED_ACCOUNT_CHANGED)
    assert world.layer.closed_sessions == ["s-live", "s-live", "s-live"]


def test_the_team_account_switch_waits_for_an_open_turn_and_stops_none(world, monkeypatch):
    from core.session import chat_rebuild
    from services.agents import offboarding_sessions as closer
    monkeypatch.setattr(closer, "live_sessions", lambda: _live(world, "s-live"))
    open_turns = {"n": 3}

    def turn_open(chat):
        return open_turns["n"] > 0

    def open_turn(chat):
        open_turns["n"] -= 1
        return open_turns["n"] > 0

    monkeypatch.setattr(chat_rebuild, "turn_open_while_held", turn_open)
    monkeypatch.setattr(chat_move, "open_turn", open_turn)

    async def go():
        await chat_reload.team_account_changed(world.agent, lends=True)
        assert world.layer.closed_sessions == []           # the turn runs on
        assert _notes(world.cid) == [(chat_reload.TEAM_ON, "account_changed")]
        for _ in range(40):
            await asyncio.sleep(0.01)
            if world.layer.closed_sessions:
                break
        assert world.layer.closed_sessions == ["s-live"]
        assert world.cid not in chat_reload._idle_closes
    asyncio.run(go())


# -- a run's own switch -----------------------------------------------------------------

def _run_chat(world, *, scope="agent", created_by=None, sid="s-run"):
    """A task's own chat with its run row and session, ``scope`` the run's."""
    from core.session import session_kind
    from storage import database as db
    run_id = f"run-{sid}"
    cid = session_kind.task_chat_id(run_id)
    db.create_chat(cid, created_by or f"agent::{world.agent}", world.agent, "default",
                   source_type="task", execution_path="claude-code-cli")
    db.update_chat(cid, session_id=sid, execution_target="local")
    db.create_run(run_id, f"task-{sid}", world.agent, "manual", None, "go", "scheduled", scope, created_by)
    db.update_run(run_id, session_id=sid, chat_id=cid)
    return cid


def _run_switch(world, cid, *, sid="s-run", asker=None, accounts=None, reason="the other mailbox"):
    from storage import database as db
    asker = asker or chat_move.run_asker(db.get_chat(cid), sid)
    assert asker is not None
    return asyncio.run(chat_reload.schedule_for_run(
        cid, asker=asker, session_id=sid, reason=reason, accounts=accounts))


def test_a_run_with_no_person_switches_among_the_agents_accounts_for_its_next_round(world, monkeypatch):
    """The switch is checked against the agent's own set, written at the
    call (nothing reads a run's choice before its next build) and the round
    recorded for the run to take after its turn; a label outside the set is
    refused in the platform's words; a person's own account is never one."""
    from storage.identity import credential_store, service_account_set
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    for label in ("c", "d"):
        credential_store.set_user_credentials("u-ann", "mail-x", {"k": label}, account_label=label)
        assert service_account_set.add_service_account_to_agent(
            "mail-x", world.agent, account_label=label, owner_sub="u-ann") == ""
    cid = _run_chat(world)
    asker = chat_move.run_asker(__import__("storage.database", fromlist=["x"]).get_chat(cid), "s-run")
    assert (asker.sub, asker.role, asker.session_id) == ("", "manager", "s-run")
    assert _run_switch(world, cid, accounts={"mail-x": "d"}) == {"scheduled": True, "accounts": {"mail-x": "d"}}
    assert service_account_set.get_chat_service_choices(cid) == {"mail-x": "d"}
    assert chat_reload.take_task_round("s-run") == ("the other mailbox", ["This run now uses d for Mail X."])
    assert chat_reload.take_task_round("s-run") is None
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _run_switch(world, cid, accounts={"mail-x": "own"})
    assert str(e.value) == "own is not one of this agent's accounts for Mail X."
    # Back to the default, and a plain reload with no switch.
    assert _run_switch(world, cid, accounts={"mail-x": "default"})["accounts"] == {"mail-x": ""}
    assert service_account_set.get_chat_service_choices(cid) == {}
    assert _run_switch(world, cid)["accounts"] == {}
    assert chat_reload.take_task_round("s-run") == ("the other mailbox", [])
    credential_store.delete_all_mcp_credentials("mail-x")


def test_a_persons_run_switches_among_the_accounts_they_lend_the_agent(world, monkeypatch):
    """A user-scope run is its creator: the creator's set, never the agent's;
    on an agent whose people use its accounts, the agent's set."""
    from storage.agents import agent_store
    from storage.identity import credential_store
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    _set(monkeypatch, "work", "home")
    _agents_set(monkeypatch, "agent-acct")
    cid = _run_chat(world, scope="user", created_by="u-ann", sid="s-user-run")
    asker = chat_move.run_asker(__import__("storage.database", fromlist=["x"]).get_chat(cid), "s-user-run")
    assert (asker.sub, asker.role) == ("u-ann", "manager")
    monkeypatch.setattr(credential_store, "set_chat_account_choice", lambda *a, **k: True)
    out = _run_switch(world, cid, sid="s-user-run", accounts={"mail-x": "home"})
    assert out["accounts"] == {"mail-x": "home"}
    assert chat_reload.take_task_round("s-user-run") == (
        "the other mailbox", ["This run now uses home for Mail X."])
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _run_switch(world, cid, sid="s-user-run", accounts={"mail-x": "agent-acct"})
    assert "is not one of the accounts the person lets this agent use" in str(e.value)
    agent_store.update_agent(world.agent, team_account=True)
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _run_switch(world, cid, sid="s-user-run", accounts={"mail-x": "home"})
    assert str(e.value) == "home is not one of this agent's accounts for Mail X."
    chat_reload.drop_task_round("s-user-run")


def test_a_run_driving_a_terminal_and_a_run_past_the_chain_are_refused(world, monkeypatch):
    from core.session import interactive_session
    from storage import remote_store
    cid = _run_chat(world)
    monkeypatch.setattr(interactive_session, "get", lambda sid: object() if sid == "s-run" else None)
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _run_switch(world, cid)
    assert str(e.value) == chat_reload.RUN_TERMINAL
    monkeypatch.setattr(interactive_session, "get", lambda sid: None)
    monkeypatch.setattr(remote_store, "reloads_since_person", lambda chat_id: chat_reload.CHAIN_MAX)
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _run_switch(world, cid)
    assert str(e.value) == "This run switched its account 3 times, so it stops here."
    assert chat_reload.take_task_round("s-run") is None


def test_the_route_takes_a_runs_session_as_the_runs_identity(world, monkeypatch):
    """A task session (its own kind, whatever its chat's row) reaches the
    route with or without a person and gets its next round; a task session
    no run owns is refused."""
    from fastapi import HTTPException

    from api.agents import session_reload as routes
    from core.session import session_kind, session_state
    from storage import database as db
    cid = _run_chat(world)
    for sid in ("s-run", "s-orphan"):
        session_state._record_session_use(sid, client_type=session_kind.TASK.name, agent=world.agent)
    monkeypatch.setattr(db, "get_chat_by_session",
                        lambda s: db.get_chat(cid) if s in ("s-run", "s-orphan") else None)
    body = routes.ReloadSessionRequest(reason="a tool was enabled")
    resp = asyncio.run(routes.reload_session(world.agent, body, user=_principal("session:s-run", "s-run", world.agent)))
    assert resp.status_code == 202 and json.loads(resp.body) == {"scheduled": True, "accounts": {}, "round": True}
    assert chat_reload.take_task_round("s-run") == ("a tool was enabled", [])
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes.reload_session(world.agent, body, user=_principal("session:s-orphan", "s-orphan", world.agent)))
    assert (e.value.status_code, e.value.detail) == (409, chat_reload.NO_RUN)
    for sid in ("s-run", "s-orphan"):
        session_state._sessions.pop(sid, None)


# -- a Shared-only chat: the agent's own accounts ------------------------------------

@pytest.fixture
def pool(world, monkeypatch):
    """The agent made Shared-only, its shared history's chat with a live
    session, and two of the agent's own accounts of Mail X (c, the default,
    and d), lent by its manager u-ann."""
    from core.session import session_state, visibility
    from storage.agents import agent_store
    from storage.identity import credential_store, service_account_set
    agent_store.update_agent(world.agent, collaborative=False, default_scope="agent")
    _tools(monkeypatch, **{"mail-x": "Mail X"})
    for label in ("c", "d", "own"):
        credential_store.set_user_credentials("u-ann", "mail-x", {"k": label}, account_label=label)
    for label in ("c", "d"):
        assert service_account_set.add_service_account_to_agent(
            "mail-x", world.agent, account_label=label, owner_sub="u-ann") == ""
    world.layer.alive.add("s-pool")
    world.pool = _chat(world.agent, visibility.shared_chat_owner(world.agent), sid="s-pool")
    _built("s-pool", world.agent, "c")
    yield world
    credential_store.delete_all_mcp_credentials("mail-x")
    session_state._built_accounts.clear()


def test_a_shared_only_chat_switches_among_the_agents_own_accounts(pool):
    """The choice is checked against the agent's own set, never the asker's
    own accounts, and its line names who switched, for everyone in the chat."""
    from storage.identity import service_account_set
    assert _schedule(pool, cid=pool.pool, accounts={"mail-x": "d"})["accounts"] == {"mail-x": "d"}
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(pool, cid=pool.pool, accounts={"mail-x": "own"})
    assert str(e.value) == "own is not one of this agent's accounts for Mail X."
    _reload(pool, cid=pool.pool, reason="reading the other mailbox", choices={"mail-x": "d"})
    assert pool.layer.closed_sessions == ["s-pool"]
    assert service_account_set.get_chat_service_choices(pool.pool) == {"mail-x": "d"}
    assert _notes(pool.pool) == [("The session was reloaded: reading the other mailbox. "
                                  "u-ann switched this chat to d for Mail X.", "")]
    _reload(pool, cid=pool.pool, reason="back", choices={"mail-x": ""})
    assert service_account_set.get_chat_service_choices(pool.pool) == {}
    assert _notes(pool.pool)[-1][0].endswith(
        "u-ann put this chat back on the agent's default account for Mail X.")


def test_only_an_editor_or_a_manager_switches_a_shared_only_chat(pool):
    """Who drives a Shared-only chat is an editor or above already; the
    switch asks it again itself, at the call and when the choice is
    written (an editor demoted meanwhile, or a restart settling the row)."""
    from storage import database as db
    from storage.identity import service_account_set
    chat = db.get_chat(pool.pool)
    for role in ("contributor", "viewer", "external"):
        with pytest.raises(chat_reload.ReloadRefused) as e:
            chat_reload._choices(chat, _asker(role=role), {"mail-x": "d"})
        assert str(e.value) == chat_reload.NOT_EDITOR
    assert chat_reload._choices(chat, _asker(role="editor"), {"mail-x": "d"}) == {"mail-x": "d"}
    with pytest.raises(chat_reload.ReloadRefused) as e:
        _schedule(pool, cid=pool.pool, asker=_asker(role="contributor"), accounts={"mail-x": "d"})
    assert str(e.value) == chat_reload.NOT_DRIVER
    db.set_user_agent_role("u-ann", pool.agent, "contributor")
    assert chat_reload._write_choices(chat, "u-ann", {"mail-x": "d"}) == []
    assert service_account_set.get_chat_service_choices(pool.pool) == {}
    # A restart settles the row of the waiting switch: nothing is written.
    row = {"chat_id": pool.pool, "what": "reload", "person": "u-ann", "reason": "x",
           "choices": {"mail-x": "d"}, "deadline": "2999-01-01T00:00:00+00:00"}
    asyncio.run(chat_reload.settle_after_restart(row, continued=True))
    assert service_account_set.get_chat_service_choices(pool.pool) == {}


ACCOUNT_CHANGED = "account_changed"
MOVES = "This chat moves to it at its next start."
NONE_LEFT = "It has no other account for Mail X now."


def _removed(account_after: str) -> list[tuple[str, str]]:
    return [(f"The account this chat used for Mail X was removed from the agent. {account_after}",
             ACCOUNT_CHANGED)]


def _moved(now: str, after: str = MOVES) -> list[tuple[str, str]]:
    return [(f"The default account for Mail X is now {now}. {after}", ACCOUNT_CHANGED)]


GONE_DEFAULT = [(f"The default account for Mail X was removed from the agent. {NONE_LEFT}",
                 ACCOUNT_CHANGED)]


async def _settled() -> None:
    """The closes of a removal run in offboarding's bounded batch, each
    chat's line after its own close: wait for this loop's batches."""
    from services.agents import offboarding_sessions
    loop = asyncio.get_running_loop()
    while batches := [t for t in offboarding_sessions._background
                      if t.get_loop() is loop and not t.done()]:
        await asyncio.gather(*batches, return_exceptions=True)
    for _ in range(5):
        await asyncio.sleep(0.01)


def _remove(agent, **filters) -> bool:
    async def go():
        gone = await chat_reload.remove_service_account("mail-x", agent, **filters)
        await _settled()
        return gone
    return asyncio.run(go())


def test_an_account_that_leaves_the_set_closes_the_sessions_built_on_it(pool):
    """Its lender leaves, or a manager removes it: the session built on it is
    closed and its chat told which account it uses now; a session built on
    another account is left as it is."""
    from core.session import visibility
    from storage.identity import service_account_set
    assert service_account_set.set_chat_service_choice(pool.pool, pool.agent, "mail-x", "d")
    _built("s-pool", pool.agent, "d")
    pool.layer.alive.add("s-other")
    other = _chat(pool.agent, visibility.shared_chat_owner(pool.agent), sid="s-other")
    _built("s-other", pool.agent, "c")
    assert _remove(pool.agent, owner_sub="u-ann", account_label="d") is True
    assert pool.layer.closed_sessions == ["s-pool"]
    assert service_account_set.get_chat_service_choices(pool.pool) == {}
    assert _notes(pool.pool) == _removed("The chat now uses c.")
    assert _notes(other) == []
    assert _remove(pool.agent, owner_sub="u-ann", account_label="d") is False


@pytest.fixture
def bob(pool):
    """A second lender, Bob, with an account in the agent's set."""
    from storage import database as db
    from storage.identity import credential_store, service_account_set
    _user("u-bob")
    db.add_user_agent("u-bob", pool.agent, "manager", "test")
    credential_store.set_user_credentials("u-bob", "mail-x", {"k": "bob"}, account_label="bob-box")
    assert service_account_set.add_service_account_to_agent(
        "mail-x", pool.agent, account_label="bob-box", owner_sub="u-bob") == ""
    return pool


def test_a_session_on_an_old_default_is_closed_when_that_account_is_removed(bob):
    """Make default tells a live chat it moves at its next start and stops
    nothing. Removing the old account then closes the session built on it,
    whatever the default is now, and tells the chat (a lender's offboarding
    removes the same way, by lender and name)."""
    from storage.identity import service_account_set
    assert service_account_set.set_service_default_account("mail-x", bob.agent, "bob-box")
    asyncio.run(chat_reload.default_moved("mail-x", bob.agent))
    assert _notes(bob.pool) == _moved("bob-box")
    assert bob.layer.closed_sessions == []
    assert _remove(bob.agent, owner_sub="u-ann", account_label="c") is True
    assert bob.layer.closed_sessions == ["s-pool"]
    assert _notes(bob.pool) == _moved("bob-box") + _removed("The chat now uses bob-box.")


def test_removing_the_default_closes_the_sessions_on_it_and_tells_every_chat_on_it(bob):
    """Nobody moves to another person's mailbox with no word: a session built
    on the default is closed, a delegated worker's between its rounds too,
    and its chat told what it uses now; an idle chat on the default is told
    the account it moves to. A chat on a kept choice and a chat that never
    started are left alone."""
    from core.session import visibility
    from storage import database as db
    from storage.identity import credential_store, service_account_set
    pool_owner = visibility.shared_chat_owner(bob.agent)
    bob.layer.alive.update({"s-kept", "s-worker"})
    kept = _chat(bob.agent, pool_owner, sid="s-kept")
    assert service_account_set.set_chat_service_choice(kept, bob.agent, "mail-x", "bob-box")
    _built("s-kept", bob.agent, "bob-box", owner="u-bob")
    idle = _chat(bob.agent, pool_owner, sid="s-idle")
    never = _chat(bob.agent, pool_owner, sid="")
    worker = _chat(bob.agent, pool_owner, sid="s-worker", parent_chat_id=bob.pool)
    _built("s-worker", bob.agent, "c")
    idle_at = db.get_chat(idle)["updated_at"]

    assert _remove(bob.agent, owner_sub="u-ann") is True

    assert credential_store.get_service_agent_binding("mail-x", bob.agent).account_label == "bob-box"
    assert sorted(bob.layer.closed_sessions) == ["s-pool", "s-worker"]
    told = _removed("The chat now uses bob-box.")
    assert _notes(bob.pool) == told and _notes(worker) == told
    assert _notes(idle) == _moved("bob-box")
    assert _notes(kept) == [] and _notes(never) == []
    # A line about an account moves no chat up the list.
    assert db.get_chat(idle)["updated_at"] == idle_at


def test_a_delegated_worker_whose_round_runs_keeps_the_account_to_its_end(bob, monkeypatch):
    """A worker's round is a task: it keeps its account to its end, and its
    chat is told the account its next round starts on."""
    from core.session import chat_rebuild, visibility
    bob.layer.alive.add("s-worker")
    worker = _chat(bob.agent, visibility.shared_chat_owner(bob.agent), sid="s-worker",
                   parent_chat_id=bob.pool)
    _built("s-worker", bob.agent, "c")
    monkeypatch.setattr(chat_rebuild, "turn_open_while_held", lambda chat: chat["id"] == worker)
    assert _remove(bob.agent, owner_sub="u-ann") is True
    assert bob.layer.closed_sessions == ["s-pool"]
    assert _notes(worker) == _moved("bob-box")


def test_the_sentence_names_the_address_of_the_account_the_chat_uses_now(bob):
    from storage.identity import credential_store
    credential_store.set_account_display_email("u-bob", "mail-x", "bob-box", "bob@team.example")
    _remove(bob.agent, owner_sub="u-ann")
    assert _notes(bob.pool) == _removed("The chat now uses bob-box (bob@team.example).")


def test_one_chat_that_cannot_be_told_leaves_the_others_told(pool, monkeypatch):
    """A removal reaches every chat on the default: a line that fails stops
    none of the others, nor the removal's answer, and a chat deleted between
    the read and its line is passed over."""
    from core.session import visibility
    from storage import database as db
    pool_owner = visibility.shared_chat_owner(pool.agent)
    first, last = sorted([pool.pool, _chat(pool.agent, pool_owner, sid="s-idle")])
    real = chat_reload._account_note

    async def _note_fails_for_the_first(chat_id, message):
        if chat_id == first:
            raise RuntimeError("the row was refused")
        await real(chat_id, message)

    monkeypatch.setattr(chat_reload, "_account_note", _note_fails_for_the_first)
    assert _remove(pool.agent) is True
    assert _notes(last) == (_removed(NONE_LEFT) if last == pool.pool else GONE_DEFAULT)
    db.delete_chat(first)
    lines = chat_reload._lines("mail-x", pool.agent, [first, last], removed={first, last},
                               default_went=True)
    assert list(lines) == [last]


def test_one_chat_that_cannot_be_told_of_a_new_default_leaves_the_rest_told(bob, monkeypatch):
    """Make default tells each chat on its own: one refused line stops
    neither the others nor the route's answer."""
    from core.session import visibility
    from storage.identity import service_account_set
    other = _chat(bob.agent, visibility.shared_chat_owner(bob.agent), sid="s-other")
    first, last = sorted([bob.pool, other])
    real = chat_reload._account_note

    async def _note_fails_for_the_first(chat_id, message):
        if chat_id == first:
            raise RuntimeError("the row was refused")
        await real(chat_id, message)

    monkeypatch.setattr(chat_reload, "_account_note", _note_fails_for_the_first)
    assert service_account_set.set_service_default_account("mail-x", bob.agent, "bob-box")
    asyncio.run(chat_reload.default_moved("mail-x", bob.agent))
    assert _notes(last) == _moved("bob-box")


def test_the_last_account_out_names_no_default_that_does_not_exist(pool):
    """A chat that chose an account and a chat on the default: neither reads
    a default, since none is left."""
    from core.session import visibility
    from storage.identity import service_account_set
    assert service_account_set.set_chat_service_choice(pool.pool, pool.agent, "mail-x", "d")
    _built("s-pool", pool.agent, "d")
    pool.layer.alive.add("s-other")
    other = _chat(pool.agent, visibility.shared_chat_owner(pool.agent), sid="s-other")
    _built("s-other", pool.agent, "c")
    assert _remove(pool.agent) is True
    told = _removed(NONE_LEFT)
    assert _notes(pool.pool) == told and _notes(other) == told
    assert sorted(pool.layer.closed_sessions) == ["s-other", "s-pool"]


def test_a_default_left_with_no_standing_lender_is_no_account_to_name(bob):
    """The promoted row's lender no longer manages the agent: the chat has no
    account, and the sentence says so."""
    from storage import database as db
    db.set_user_agent_role("u-bob", bob.agent, "editor")
    _remove(bob.agent, owner_sub="u-ann")
    assert _notes(bob.pool) == _removed(NONE_LEFT)


def test_an_account_line_is_no_reload_of_the_chain(pool):
    """The agent's reloads in a row count the lines of its own reloads."""
    from storage import remote_store
    _remove(pool.agent)
    assert len(_notes(pool.pool)) == 1
    assert remote_store.reloads_since_person(pool.pool) == 0


def test_a_default_that_moves_tells_the_chats_on_it_and_stops_no_turn(bob):
    """The old default stays one of the agent's: a chat on it is told when it
    moves, a delegated worker's included, and its running session is not
    stopped for it."""
    from core.session import visibility
    from storage import database as db
    from storage.identity import service_account_set
    pool_owner = visibility.shared_chat_owner(bob.agent)
    bob.layer.alive.add("s-kept")
    kept = _chat(bob.agent, pool_owner, sid="s-kept")
    assert service_account_set.set_chat_service_choice(kept, bob.agent, "mail-x", "d")
    idle = _chat(bob.agent, pool_owner, sid="s-idle")
    worker = _chat(bob.agent, pool_owner, sid="s-worker", parent_chat_id=bob.pool)
    idle_at = db.get_chat(idle)["updated_at"]
    assert service_account_set.set_service_default_account("mail-x", bob.agent, "bob-box")
    asyncio.run(chat_reload.default_moved("mail-x", bob.agent))
    told = _moved("bob-box")
    assert _notes(bob.pool) == told and _notes(idle) == told and _notes(worker) == told
    assert _notes(kept) == []
    assert bob.layer.closed_sessions == []
    assert db.get_chat(idle)["updated_at"] == idle_at


def test_a_pre_warm_built_on_a_removed_account_is_closed_and_never_claimed(pool):
    """A pre-warmed session belongs to no chat yet: it is closed with no line,
    so the next new chat never starts on the removed account."""
    from core.session import prewarm_session_registry as prewarm

    async def go():
        await prewarm.register("s-prewarm", agent=pool.agent, role="editor", user_sub="u-ann",
                               exec_path="claude-code-cli", model="m")
        await chat_reload.remove_service_account("mail-x", pool.agent)
        await _settled()
        return await prewarm.claim_by_key(user_sub="u-ann", agent=pool.agent, model="m",
                                          role="editor", exec_path="claude-code-cli")

    pool.layer.alive.add("s-prewarm")
    _built("s-prewarm", pool.agent, "c")
    assert asyncio.run(go()) is None
    assert "s-prewarm" in pool.layer.closed_sessions


def test_a_chat_on_a_persons_own_computer_is_never_told_an_account_it_cannot_use(bob):
    """A Shared-only chat on Ann's own computer takes only Ann's accounts:
    told of Bob's new default, it reads that it cannot use it there, and when
    Ann's accounts go it reads that it has none there."""
    from storage import database as db, remote_store
    from storage.identity import service_account_set
    remote_store.create_remote_machine("m-ann", "Ann's laptop", "u-ann", pairing_scope="user")
    db.update_chat(bob.pool, execution_target="m-ann")
    assert service_account_set.set_service_default_account("mail-x", bob.agent, "bob-box")
    asyncio.run(chat_reload.default_moved("mail-x", bob.agent))
    assert _notes(bob.pool) == _moved("bob-box", "This chat cannot use it on this computer.")
    assert _remove(bob.agent, owner_sub="u-ann") is True
    assert _notes(bob.pool)[-1][0].endswith("It has no other account for Mail X on this computer.")


def _placed_on(sid: str, machine: str, agent: str) -> None:
    from auth.path_policy import SecurityContext
    from core import placement
    from core.session import session_state
    session_state.set_session_security(sid, SecurityContext(
        role="editor", username="", agent=agent, is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id=machine)))


def test_the_build_record_rides_the_sessions_context_across_a_restart(pool):
    """The build's note is folded into the session's context when it
    registers, and the index keeps it, so a session re-adopted after a
    restart is still found by what it was built on."""
    from core.session import session_state
    _built("s-pool", pool.agent, "d")
    _placed_on("s-pool", "m-1", pool.agent)
    context = session_state.get_session_security("s-pool")
    assert context.agent_accounts == (("mail-x", "u-ann", "d"),)
    again = session_state._deserialize_security_ctx(
        json.loads(json.dumps(session_state._serialize_security_ctx(context))))
    assert again == context
    assert session_state.sessions_built_on(pool.agent, "mail-x", {("u-ann", "d")}) == ["s-pool"]
    assert session_state.sessions_built_on(pool.agent, "mail-x", {("u-ann", "c")}) == []
    session_state.cleanup_session_permission_state("s-pool")
    assert session_state.sessions_built_on(pool.agent, "mail-x", {("u-ann", "d")}) == []


def test_an_account_removed_while_the_machine_is_away_ends_the_session_there(pool, monkeypatch):
    """The machine is away: the remote layer's close takes the record, its
    queue and its context, so the machine's report at its return meets a
    refusal and the process ends (``run_recovery._readopt_state``), never a
    session back on the account."""
    from unittest.mock import AsyncMock, MagicMock

    from core.remote import satellite_connection
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.session import session_manager, session_state
    from services.scheduler import run_recovery
    from storage.identity import service_account_set
    cm = MagicMock()
    cm.is_connected = lambda machine: False
    cm.send_command = AsyncMock(side_effect=RuntimeError("not connected"))
    remote = RemoteExecutionLayer(cm)
    info = MagicMock(machine_id="m-away", execution_path="claude-code-cli", alive=True)
    remote._sessions["s-pool"] = info
    monkeypatch.setattr(RemoteExecutionLayer, "_adapter", staticmethod(
        lambda i: MagicMock(close_state=AsyncMock())))
    monkeypatch.setattr(session_manager, "_remote_layer", remote)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: cm)
    _built("s-pool", pool.agent, "d")
    _placed_on("s-pool", "m-away", pool.agent)
    assert run_recovery._readopt_state("m-away", "s-pool", None) is not None
    assert service_account_set.set_chat_service_choice(pool.pool, pool.agent, "mail-x", "d")
    assert _remove(pool.agent, owner_sub="u-ann", account_label="d") is True
    assert "s-pool" not in remote._sessions
    cm.remove_session_queue.assert_called_once_with("m-away", "s-pool")
    assert session_state.get_session_security("s-pool") is None
    assert run_recovery._readopt_state("m-away", "s-pool", None) is None
    assert _notes(pool.pool) == _removed("The chat now uses c.")


def test_an_account_removed_after_a_restart_with_the_machine_away_ends_the_session_too(
        pool, monkeypatch):
    """The platform restarted while the machine was away: no record holds the
    session, only its context does. The context goes all the same, so the
    machine's report at its return does not bring the session back."""
    from unittest.mock import MagicMock

    from core.remote import satellite_connection
    from core.session import session_state
    from services.scheduler import run_recovery
    from storage.identity import service_account_set
    monkeypatch.setattr(satellite_connection, "get_connection_manager",
                        lambda: MagicMock(is_connected=lambda machine: False))
    pool.layer.alive.discard("s-pool")
    _built("s-pool", pool.agent, "d")
    _placed_on("s-pool", "m-away", pool.agent)
    assert service_account_set.set_chat_service_choice(pool.pool, pool.agent, "mail-x", "d")
    _remove(pool.agent, owner_sub="u-ann", account_label="d")
    assert session_state.get_session_security("s-pool") is None
    assert run_recovery._readopt_state("m-away", "s-pool", None) is None


def test_an_account_removed_on_a_connected_machine_leaves_a_later_session_alone(pool, monkeypatch):
    """No holder and a machine that is here: a session starting there under
    the id keeps its context."""
    from unittest.mock import MagicMock

    from core.remote import satellite_connection
    from core.session import session_state
    from storage.identity import service_account_set
    monkeypatch.setattr(satellite_connection, "get_connection_manager",
                        lambda: MagicMock(is_connected=lambda machine: True))
    pool.layer.alive.discard("s-pool")
    _built("s-pool", pool.agent, "d")
    _placed_on("s-pool", "m-here", pool.agent)
    assert service_account_set.set_chat_service_choice(pool.pool, pool.agent, "mail-x", "d")
    _remove(pool.agent, owner_sub="u-ann", account_label="d")
    assert session_state.get_session_security("s-pool") is not None
    session_state.cleanup_session_permission_state("s-pool")


def test_a_running_turn_does_not_hold_up_a_removal(pool):
    """A turn holds the session's lock to its end: the account leaves now,
    the turn with it, and the removal does not wait for it."""
    from storage.identity import service_account_set
    assert service_account_set.set_chat_service_choice(pool.pool, pool.agent, "mail-x", "d")
    _built("s-pool", pool.agent, "d")

    async def go():
        async with pool.layer.session_lock("s-pool"):
            gone = await asyncio.wait_for(chat_reload.remove_service_account(
                "mail-x", pool.agent, owner_sub="u-ann", account_label="d"), timeout=5)
        await _settled()
        return gone

    assert asyncio.run(go()) is True
    assert pool.layer.closed_sessions == ["s-pool"]


# -- a removal during a running reply, through the real layers ------------------------
#
# The chat's own producer streams the layer's turn into a real pump; the
# removal stops the turn the way the Stop button's hard path does
# (``offboarding_sessions.stop_turn``) before the layer's own close, so the
# reply ends at once and the chat never reads the close as a lost machine or
# an engine that exited.

RUN = "s-run"


async def _running_reply(pool, monkeypatch, layer, stream):
    """A real pump over ``stream``, registered for the pool's chat on
    ``RUN`` (built on c); the closer finds ``layer``."""
    from core.events import stream_pump
    from core.session import session_manager
    from storage import database as db
    db.update_chat(pool.pool, session_id=RUN)
    _built(RUN, pool.agent, "c")
    events: asyncio.Queue = asyncio.Queue()

    async def produce():
        async for event in stream:
            await events.put(event)

    pump = stream_pump.ChatStreamPump(pool.pool, RUN, asyncio.ensure_future(produce()), events, None)
    monkeypatch.setitem(stream_pump._active_pumps, pool.pool, pump)
    monkeypatch.setattr(session_manager, "find_layer_for_session",
                        lambda sid: layer if sid == RUN else None)
    await asyncio.sleep(0.1)
    assert not pump.producer.done(), "the turn ended before the removal"
    return pump, events


async def _remove_during(pool, pump, events) -> None:
    from core.events.common_events import ERROR
    assert await chat_reload.remove_service_account(
        "mail-x", pool.agent, owner_sub="u-ann", account_label="c") is True
    done, _ = await asyncio.wait({pump.producer}, timeout=2.0)
    assert done, "the reply is still running after the removal"
    await _settled()
    seen = [events.get_nowait() for _ in range(events.qsize())]
    # A typed ending reaches the pump as an ERROR that carries it.
    assert [e for e in seen if e.type == ERROR] == []
    assert _notes(pool.pool) == _removed("The chat now uses d.")


def _remote(send_command):
    from unittest.mock import AsyncMock, MagicMock

    from core.remote.remote_execution import RemoteExecutionLayer
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    cm = MagicMock()
    cm.send_fire_and_forget = AsyncMock()
    cm.send_command = send_command
    cm.is_session_in_grace = MagicMock(return_value=False)
    cm.is_session_stream_attached = MagicMock(return_value=False)
    layer._cm = cm
    layer._sessions = {}
    layer._spawning = set()
    return layer, cm


def _machine(connected: bool, box: list):
    """The machine's side of the commands: a close ends the session's
    stream there (``session_ended``); one that is away answers nothing."""
    async def send_command(_machine_id, frame, **_kw):
        if not connected:
            raise RuntimeError("Satellite m-1 not connected")
        if frame.get("type") == "close_session" and box:
            box[0].event_queue.put_nowait(None)
        return {}
    return send_command


@pytest.mark.parametrize("connected", [True, False], ids=["connected", "away"])
def test_a_removal_stops_a_running_claude_code_reply_on_a_machine(pool, monkeypatch, connected):
    from core.layers.cli.layer import CLIExecutionLayer
    from core.layers.cli.remote import ClaudeRemoteState
    from core.layers.cli.settle import SettleController
    from core.layers.cli.translator import ClaudeCLIEventTranslator
    from core.remote.remote_execution import RemoteSessionInfo
    from core.session import session_manager
    # The harness puts a fake in the engine's place: the turn here is the
    # engine's own remote adapter.
    monkeypatch.setitem(session_manager._ENGINES, "claude-code-cli",
                        CLIExecutionLayer.__new__(CLIExecutionLayer))

    class _ShortSlice(SettleController):
        __slots__ = ()

        def effective_timeout(self) -> float:
            return 0.05

    async def go():
        box: list = []
        layer, cm = _remote(_machine(connected, box))
        info = RemoteSessionInfo(session_id=RUN, machine_id="m-1", agent_name=pool.agent,
                                 execution_path="claude-code-cli", event_queue=asyncio.Queue())
        translator = ClaudeCLIEventTranslator(RUN)
        info.engine_state = ClaudeRemoteState(translator=translator,
                                              settle=_ShortSlice(RUN, 0, translator))
        info.current_send_command_id = "cmd-1"
        info.turn_active = True
        layer._sessions[RUN] = info
        box.append(info)
        pump, events = await _running_reply(pool, monkeypatch, layer,
                                            layer._adapter(info).stream_turn(info, cm))
        await _remove_during(pool, pump, events)
        assert RUN not in layer._sessions

    asyncio.run(go())


@pytest.mark.parametrize("connected", [True, False], ids=["connected", "away"])
def test_a_removal_stops_a_running_codex_reply_on_a_machine(pool, monkeypatch, connected):
    from core.layers.codex import remote as codex_remote
    from core.layers.codex.remote import CodexRemoteState
    from core.layers.codex.translator import CodexEventTranslator
    from core.remote.remote_execution import RemoteSessionInfo
    monkeypatch.setattr(codex_remote, "_SILENCE_SLICE_S", 0.05)

    async def go():
        box: list = []
        layer, cm = _remote(_machine(connected, box))
        info = RemoteSessionInfo(session_id=RUN, machine_id="m-1", agent_name=pool.agent,
                                 execution_path="codex-cli", event_queue=asyncio.Queue())
        layer._sessions[RUN] = info
        box.append(info)
        info.current_send_command_id = "cmd-1"
        info.turn_active = True
        state = CodexRemoteState(translator=CodexEventTranslator(model="m", supervised_bg=True))
        info.engine_state = state
        adapter = layer._adapter(info)
        state.router_task = asyncio.create_task(adapter._route_notifications(info))
        await adapter.begin_turn(info, 0)
        pump, events = await _running_reply(pool, monkeypatch, layer, adapter.stream_turn(info, cm))
        await _remove_during(pool, pump, events)
        assert RUN not in layer._sessions

    asyncio.run(go())


def test_a_later_call_replaces_an_earlier_one_and_a_move_drops_it(world, monkeypatch):
    started: list = []

    async def _wait(chat_id, asker, reason, choices, mark, blocked, wait_s):
        started.append(reason)
        await asyncio.sleep(30)

    monkeypatch.setattr(chat_reload, "_wait_and_reload", _wait)

    async def go():
        await chat_reload.schedule_after_turn(world.cid, asker=_asker(), reason="first.")
        first = chat_reload._waiters[world.cid]
        await chat_reload.schedule_after_turn(world.cid, asker=_asker(), reason="second")
        await asyncio.sleep(0.01)
        second = chat_reload._waiters[world.cid]
        assert first.cancelled() and chat_reload.waiting(world.cid)
        chat_reload.cancel(world.cid)
        await asyncio.sleep(0.01)
        return second.cancelled()

    assert asyncio.run(go()) is True
    assert started == ["second"] or started == ["first", "second"]
    assert not chat_reload.waiting(world.cid)


# -- the row of a waiter, across a stop --------------------------------------------

def _scheduled_and_waiting(world):
    return chat_reload.schedule_after_turn(world.cid, asker=_asker(), reason="the Notion tool was enabled",
                                           accounts=None)


async def _the_stop_cuts_the_wait() -> None:
    """The graceful shutdown: the stop is noted first, then the tracked tasks
    are cancelled while the store is still open."""
    from core import background, drain
    drain.note_stopping()
    await background.cancel_all(grace=0.0, timeout=1.0)
    await asyncio.sleep(0.05)


def test_a_clean_stop_leaves_the_row_and_the_next_boot_waits_again_and_reloads(world, monkeypatch):
    """A drained stop finds a reload waiting with no turn left to cut: no record
    settles its chat, so the boot arms it again with what is left of its wait."""
    from core import drain
    from services.scheduler import continue_pass
    from storage.chat import pending_rebuilds
    monkeypatch.setattr(chat_move, "open_turn", lambda chat: True)

    async def go():
        await _scheduled_and_waiting(world)
        await asyncio.sleep(0.05)
        await _the_stop_cuts_the_wait()
        [row] = pending_rebuilds.listed()
        assert (row["chat_id"], row["what"], row["person"]) == (world.cid, pending_rebuilds.RELOAD, "u-ann")
        assert world.layer.closed_sessions == []
        drain.reset_for_tests()
        chat_reload._waiters.clear()
        monkeypatch.setattr(chat_move, "open_turn", lambda chat: False)
        await continue_pass.settle_waiters()
        await chat_reload._waiters[world.cid]
        for _ in range(20):
            await asyncio.sleep(0.01)
    asyncio.run(go())
    assert world.layer.closed_sessions == ["s-live"]
    assert _notes(world.cid) == [("The session was reloaded: the Notion tool was enabled.", "")]
    assert pending_rebuilds.listed() == []


def test_a_row_the_stop_left_tells_the_chat_when_its_cut_turn_is_not_continued(world, monkeypatch):
    from services.scheduler import continue_pass
    from storage.chat import pending_rebuilds
    monkeypatch.setattr(chat_move, "open_turn", lambda chat: True)

    async def go():
        await _scheduled_and_waiting(world)
        await asyncio.sleep(0.05)
        await _the_stop_cuts_the_wait()
        await continue_pass._settle_waiter({"chat_id": world.cid}, continued=False)
        for _ in range(20):
            await asyncio.sleep(0.01)
    asyncio.run(go())
    assert _notes(world.cid)[-1][0] == "The session was not reloaded: the platform restarted. Ask again."
    assert pending_rebuilds.listed() == []


def test_a_reload_that_ended_during_the_stop_takes_its_row(world, monkeypatch):
    """Inside the shutdown's grace a waiter can still finish: a row left then
    would reload the chat again at the next boot."""
    from core import drain
    from storage.chat import pending_rebuilds

    async def _wait(chat_id, asker, reason, choices, mark, blocked, wait_s):
        drain.note_stopping()
    monkeypatch.setattr(chat_reload, "_wait_and_reload", _wait)

    async def go():
        await _scheduled_and_waiting(world)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert pending_rebuilds.listed() == []


def test_a_cancel_that_is_not_the_stop_takes_its_row(world, monkeypatch):
    from storage.chat import pending_rebuilds
    monkeypatch.setattr(chat_move, "open_turn", lambda chat: True)

    async def go():
        await _scheduled_and_waiting(world)
        await asyncio.sleep(0.05)
        assert len(pending_rebuilds.listed()) == 1
        chat_reload._waiters[world.cid].cancel()
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert pending_rebuilds.listed() == []


# -- the route ---------------------------------------------------------------------

def _principal(sub, sid, agent, **kw):
    """A session's principal as the resolver builds it: its rows read from the store."""
    from auth.providers import UserContext
    from storage import database as db
    kw.setdefault("agent_roles", db.get_user_agent_roles(sub))
    return UserContext(sub=sub, email=f"{sub}@t.io", name=sub, role="member", agents=[agent],
                       session_id=sid, agent=agent, is_api_key=True, **kw)


def test_the_route_reloads_only_the_callers_own_chat(world, monkeypatch):
    from fastapi import HTTPException

    from api.agents import session_reload as routes
    from auth.providers import UserContext
    from storage import database as db
    monkeypatch.setattr(db, "get_chat_by_session",
                        lambda s: db.get_chat(world.cid) if s == "s-live" else None)
    scheduled: list = []

    async def _schedule_after_turn(chat_id, **kw):
        scheduled.append((chat_id, kw["asker"].sub, kw["asker"].role, kw["reason"], kw["accounts"]))
        return {"scheduled": True, "accounts": kw["accounts"] or {}}

    monkeypatch.setattr(chat_reload, "schedule_after_turn", _schedule_after_turn)
    body = routes.ReloadSessionRequest(reason="a tool was enabled", accounts={"mail-x": "home"})

    def call(user, name=None):
        return asyncio.run(routes.reload_session(name or world.agent, body, user=user))

    resp = call(_principal("u-ann", "s-live", world.agent))
    assert resp.status_code == 202
    # The asker's own role on the agent, the principal's: the switch's rule asks it.
    assert scheduled == [(world.cid, "u-ann", "manager", "a tool was enabled", {"mail-x": "home"})]
    # A signed-in person or a key is not a session: the tool reloads the
    # chat it runs in.
    with pytest.raises(HTTPException) as e:
        call(UserContext(sub="u-ann", email="a@t.io", name="a", role="member",
                         agents=[world.agent]))
    assert e.value.status_code == 403
    # A session that runs as nobody has no person to rebuild for.
    with pytest.raises(HTTPException) as e:
        call(_principal("session:s-live", "s-live", world.agent))
    assert (e.value.status_code, e.value.detail) == (409, chat_reload.NOBODY)
    # A session whose chat is gone, or is another agent's.
    with pytest.raises(HTTPException) as e:
        call(_principal("u-ann", "s-gone", world.agent))
    assert e.value.status_code == 404
    assert len(scheduled) == 1


def test_a_refusal_reaches_the_agent_as_its_sentence(world, monkeypatch):
    from fastapi import HTTPException

    from api.agents import session_reload as routes
    from storage import database as db
    monkeypatch.setattr(db, "get_chat_by_session", lambda s: db.get_chat(world.cid))
    monkeypatch.setitem(chat_move._running, world.cid, object())
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes.reload_session(
            world.agent, routes.ReloadSessionRequest(reason="x"),
            user=_principal("u-ann", "s-live", world.agent)))
    assert (e.value.status_code, e.value.detail) == (409, chat_reload.MOVING)
