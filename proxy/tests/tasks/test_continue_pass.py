"""The continue pass (``services/scheduler/continue_pass.py``): the rules a
record is judged by, the one call that continues a chat, the gate, the cap
and the claim. The store is real. The start itself is faked at
``chat_rebuild.rebuild_and_resume`` (its own tests cover the ladder) and at
``run_recovery.reopen_run``."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from auth import roles
from core import background
from core.events import chat_writer
from core.session import chat_rebuild, session_kind
from core.session.session_delivery import DeliveryOutcome
from services.infra import platform_marks
from services.scheduler import continue_pass, run_recovery
from storage import database as task_store
from storage.chat import cut_records, pending_rebuilds
from ws import wire_events as wire

AGENT = "pa"
PERSON = "user-viewer"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(continue_pass, "_PASS_SPACING_S", 0.0)
    monkeypatch.setattr(continue_pass, "_START_POLL_S", 0.01)
    monkeypatch.setattr(continue_pass, "_running", False)
    monkeypatch.setattr(continue_pass, "_again", False)
    monkeypatch.setattr(platform_marks, "_boot", platform_marks.Boot(
        stamped=True, down_since=datetime.now(timezone.utc) - timedelta(minutes=5)))


def _role(sub: str, role: str | None) -> None:
    """The person's row on the agent (None: they no longer hold it)."""
    with task_store.get_conn() as conn:
        conn.execute("DELETE FROM user_agents WHERE sub=%s AND agent=%s", (sub, AGENT))
        if role is not None:
            conn.execute(
                "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
                "VALUES (%s, %s, '2026-01-01T00:00:00+00:00', 'user-admin', %s)", (sub, AGENT, role))
        conn.commit()


def _queue(chat_id: str, queue_id: str, text: str) -> None:
    task_store.enqueue_chat_input(chat_id, queue_id, PERSON, text=text, cli_text=text,
                                  event_data="", images="", origin_conn="")


@pytest.fixture
def person(temp_db):
    """A member who holds the agent as an editor."""
    _role(PERSON, roles.EDITOR)
    return PERSON


def _chat(chat_id: str, owner: str = PERSON, **fields) -> None:
    task_store.create_chat(chat_id, owner, AGENT, "default")
    task_store.update_chat(chat_id, session_id=f"sid-{chat_id}", **fields)


def _record(chat_id: str, **facts) -> dict:
    facts = {"kind": session_kind.DASHBOARD.name, "person": PERSON, "session_id": f"sid-{chat_id}",
             "engine_open": True, "started_at": "2026-01-01T00:00:00+00:00", **facts}
    return cut_records.add(chat_id, cause=facts.pop("cause", cut_records.CAUSE_CRASH),
                           state=facts.pop("state", cut_records.PENDING), **facts)


async def _pass() -> int:
    """One pass, and what it let go on beside it (a run's report), to the end."""
    taken = await continue_pass.run_pending()
    for task in background.running("continue_pass"):
        await task
    return taken


def _state(record: dict) -> tuple[str, str]:
    row = cut_records.get(record["id"])
    return row["state"], row["reason"]


def _cards(chat_id: str) -> list[str]:
    return [json.loads(r["event_data"])["message"] for r in task_store.get_chat_messages(chat_id)
            if r.get("event_type") == wire.SYSTEM
            and json.loads(r["event_data"]).get("subtype") == wire.SUBTYPE_TURN_ENDED]


def _wakes(chat_id: str) -> list[dict]:
    return [json.loads(r["event_data"]) for r in task_store.get_chat_messages(chat_id)
            if r.get("event_type") == wire.PERSISTED_SCHEDULE_WAKE]


@pytest.fixture
def resumed(monkeypatch):
    """``rebuild_and_resume`` recorded, answering ``path`` (and writing the
    caller's row first, as the ladder does)."""
    calls: list[dict] = []
    answer = SimpleNamespace(path="oneshot", raises=None, during=None)

    async def _fake(chat_id, text, **kw):
        calls.append({"chat_id": chat_id, "text": text, **kw})
        if kw.get("persist_event") is not None:
            kw["persist_event"](chat_id)
        if answer.during is not None:
            await answer.during()
        if answer.raises is not None:
            raise answer.raises
        return DeliveryOutcome(answer.path, chat_id=chat_id)
    monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _fake)
    return calls, answer


# --- the rules ---------------------------------------------------------------------


def test_a_persons_chat_is_continued_at_the_role_read_now(person):
    _chat("cp-go")
    verdict = continue_pass.judge(_record("cp-go"))
    assert (verdict.action, verdict.person, verdict.role) == (
        continue_pass.CONTINUE, PERSON, roles.EDITOR)
    # The role is the store's at that moment, never the record's.
    _role(PERSON, roles.VIEWER)
    assert continue_pass.judge(_record("cp-go")).role == roles.VIEWER


def test_a_person_who_lost_the_agent_gets_no_session(person):
    _chat("cp-gone")
    record = _record("cp-gone")
    _role(PERSON, None)
    verdict = continue_pass.judge(record)
    assert (verdict.action, verdict.reason) == (
        continue_pass.CARD, "its person no longer holds the agent")


@pytest.mark.parametrize("facts, action, reason", [
    ({"kind": ""}, continue_pass.CARD, "the turn of an older release"),
    ({"kind": session_kind.PHONE.name}, continue_pass.DROP, "a phone turn has no resume"),
    ({"kind": session_kind.MEETING.name}, continue_pass.DROP, "a meeting turn has no resume"),
    ({"person": ""}, continue_pass.CARD, "nobody to continue it as"),
    ({"person": "user-viewer2"}, continue_pass.CARD, "the chat is another person's"),
    ({"kind": session_kind.TASK.name, "run_id": "run-none"}, continue_pass.CARD, "its run is over"),
])
def test_what_is_never_continued(person, facts, action, reason):
    _chat("cp-never")
    verdict = continue_pass.judge(_record("cp-never", **facts))
    assert (verdict.action, verdict.reason) == (action, reason)


def test_a_chat_that_is_gone_drops_its_record(person):
    _chat("cp-deleted")
    record = _record("cp-deleted")
    assert continue_pass.judge({**record, "chat_id": "cp-no-such"}).action == continue_pass.DROP


def test_a_task_runs_chat_with_no_live_run_is_never_continued_as_a_chat(person):
    """A person continuing a finished run's chat: the record names no run
    that is live, and a task's chat is not started at a role nobody holds."""
    chat_id = session_kind.task_chat_id("run-done")
    _chat(chat_id)
    verdict = continue_pass.judge(_record(chat_id))
    assert (verdict.action, verdict.reason) == (
        continue_pass.CARD, "a task's chat with no run of its own")


def test_a_run_that_is_live_is_reopened_whatever_pump_recorded_its_turn(person):
    chat_id = session_kind.task_chat_id("run-live")
    _chat(chat_id)
    task_store.create_run("run-live", "task-x", AGENT, "manual", None, "do it")
    task_store.update_run("run-live", status="running", chat_id=chat_id)
    for kind in (session_kind.TASK.name, session_kind.DASHBOARD.name):
        verdict = continue_pass.judge(_record(chat_id, kind=kind, run_id="run-live"))
        assert verdict.action == continue_pass.REOPEN and verdict.run["id"] == "run-live"
    # A judge's run and a run that had ended are never reopened.
    task_store.update_run("run-live", completed_at="2026-01-01T00:00:00+00:00")
    assert continue_pass.judge(
        _record(chat_id, kind=session_kind.TASK.name, run_id="run-live")).action == continue_pass.CARD


def test_a_person_who_wrote_first_is_given_way_to(person):
    _chat("cp-wrote")
    record = _record("cp-wrote")
    task_store.add_chat_message("cp-wrote", "user", "never mind, do this instead")
    verdict = continue_pass.judge(record)
    assert (verdict.action, verdict.queued) == (continue_pass.GIVE_WAY, False)


def test_a_message_that_waits_goes_first(person):
    _chat("cp-queued")
    record = _record("cp-queued")
    _queue("cp-queued", "q-1", "also check the totals")
    verdict = continue_pass.judge(record)
    assert (verdict.action, verdict.queued, verdict.reason) == (
        continue_pass.GIVE_WAY, True, "a person's message waits")


def test_the_cap_three_in_a_row_or_six_in_a_day(person):
    _chat("cp-cap")
    for _ in range(continue_pass.CHAIN_MAX):
        done = _record("cp-cap")
        cut_records.claim(done["id"])
        cut_records.settle(done["id"], cut_records.CONTINUED)
    verdict = continue_pass.judge(_record("cp-cap"))
    assert (verdict.action, verdict.reason) == (
        continue_pass.CARD, "continued too often with no message from a person")


# --- the words ---------------------------------------------------------------------


def test_the_words_are_one_text_for_every_engine_with_what_the_record_adds(person, monkeypatch):
    _chat("cp-words")
    chat = task_store.get_chat("cp-words")
    plain = continue_pass.words(_record("cp-words"), chat)
    assert plain == continue_pass.CONTINUE_WORDS
    for text in (plain, continue_pass.MACHINE_WORDS, continue_pass.GAVE_WAY_NOTE):
        # Never "did not finish": a call can finish with its answer lost.
        assert "may or may not have finished, so check what it did before you repeat it" in text
        assert "runs nothing" in text and ";" not in text and "—" not in text
    card = continue_pass.words(
        _record("cp-words", card=json.dumps({"tool": "Bash", "request_id": "r1"})), chat)
    assert card == plain + (" You were waiting for the person's answer about Bash. "
                            "Ask again if you still need it.")
    plan = continue_pass.words(_record("cp-words", card=json.dumps(
        {"tool": "ExitPlanMode", "event_type": wire.ITEM_PLAN_REVIEW})), chat)
    assert plan.endswith("You were waiting for the person to review your plan. Present it again.")
    machine = continue_pass.words(_record("cp-words", cause=cut_records.CAUSE_MACHINE), chat)
    assert machine.startswith("The machine this chat runs on restarted")


def test_an_engine_that_rebuilds_from_rows_is_told_its_tool_calls(person, monkeypatch):
    """Direct LLM sees no tool call of the cut turn in its rebuilt history,
    so the words name them, answered or not."""
    from core.session import session_manager
    _chat("cp-direct")
    task_store.add_chat_message("cp-direct", "event", "", event_type=wire.PERSISTED_TOOL,
                                event_data=json.dumps({"name": "Read", "active": False}))
    task_store.add_chat_message("cp-direct", "event", "", event_type=wire.PERSISTED_TOOL,
                                event_data=json.dumps({"name": "mcp__mail__send", "active": True}))
    monkeypatch.setattr(session_manager, "capabilities_for_path", lambda path: SimpleNamespace(
        behaviour=SimpleNamespace(rebuilds_history_from_db=True)))
    text = continue_pass.words(_record("cp-direct"), task_store.get_chat("cp-direct"))
    assert text.endswith(
        "Before the cut you had called: Read (answered), mcp__mail__send "
        "(no answer, it may have finished). This list may be short.")


# --- the pass ----------------------------------------------------------------------


def test_the_continue_is_the_one_call_with_nothing_defaulted(person, resumed):
    calls, _answer = resumed
    _chat("cp-call", permission_mode="ask")
    record = _record("cp-call", plan="plan.md")

    async def scene():
        taken = await _pass()
        await chat_writer.drain("cp-call", timeout=5)
        return taken

    assert asyncio.run(scene()) == 1
    [call] = calls
    assert call["chat_id"] == "cp-call" and call["text"] == continue_pass.CONTINUE_WORDS
    assert {k: call[k] for k in ("source", "person", "role", "kind", "build", "allow_ws",
                                 "allow_pty", "allow_live_turn", "fresh_without_session",
                                 "session_id", "agent")} == {
        "source": "continue", "person": PERSON, "role": roles.EDITOR,
        "kind": session_kind.DASHBOARD.name, "build": chat_rebuild.AS_CHAT, "allow_ws": False,
        "allow_pty": False, "allow_live_turn": False, "fresh_without_session": True,
        "session_id": "sid-cp-call", "agent": AGENT}
    assert call["mode"] == chat_rebuild.chat_mode(task_store.get_chat("cp-call"))
    assert _state(record) == (cut_records.CONTINUED, "")
    # What the chat reads: the platform's words under a wake row, no card,
    # no abort flags (an engine that rebuilds from rows keeps the cut turn).
    assert _wakes("cp-call") == [{"prompt": continue_pass.CONTINUE_WORDS,
                                  "source": wire.WAKE_SOURCE_CONTINUE}]
    assert _cards("cp-call") == []
    assert not task_store.get_chat("cp-call")["last_turn_aborted"]


def test_a_start_that_did_not_happen_is_tried_again_then_gets_the_card(person, resumed):
    calls, answer = resumed
    answer.path = "none"
    _chat("cp-none")
    record = _record("cp-none")

    async def scene():
        for _ in range(continue_pass.ATTEMPTS_MAX + 1):
            await _pass()
            await chat_writer.drain("cp-none", timeout=5)

    asyncio.run(scene())
    assert len(calls) == continue_pass.ATTEMPTS_MAX
    assert _state(record) == (cut_records.DROPPED, "it could not be started")
    assert _wakes("cp-none") == []          # every wake row was taken back
    assert _cards("cp-none") == ["⚠ The reply was cut short when the platform restarted. "
                                 "Send a message to continue."]
    assert task_store.get_chat("cp-none")["last_turn_aborted"] is True


def test_a_start_that_raises_is_a_start_that_did_not_happen(person, resumed):
    calls, answer = resumed
    answer.raises = RuntimeError("the spawn's guard")
    _chat("cp-raise")
    record = _record("cp-raise")
    asyncio.run(_pass())
    assert len(calls) == 1 and _state(record)[0] == cut_records.PENDING
    assert cut_records.get(record["id"])["attempts"] == 1


def test_a_person_who_takes_the_chat_during_the_start_is_given_way_to(person, resumed, monkeypatch):
    calls, answer = resumed
    answer.path = "none"
    from services.remote import chat_move
    took = {"chat": False}
    monkeypatch.setattr(chat_move, "_person_took_chat", lambda cid: took["chat"])

    async def _opens_the_chat():
        took["chat"] = True
    answer.during = _opens_the_chat
    _chat("cp-took")
    record = _record("cp-took")

    async def scene():
        await _pass()
        await chat_writer.drain("cp-took", timeout=5)

    asyncio.run(scene())
    assert _state(record) == (cut_records.GAVE_WAY, "a person took the chat")
    assert _cards("cp-took") == [] and _wakes("cp-took") == []
    # The cut rides their next message as a note.
    notes = task_store.claim_pending_wake_records("cp-took", notes=True, wakes=False)
    assert [(n["prompt"], n["person"]) for n in notes] == [(continue_pass.GAVE_WAY_NOTE, PERSON)]


def test_a_turn_that_holds_the_chat_before_the_start_is_given_way_to(person, resumed, monkeypatch):
    calls, _answer = resumed
    from services.remote import chat_move
    monkeypatch.setattr(chat_move, "_person_took_chat", lambda cid: True)
    _chat("cp-held")
    record = _record("cp-held")
    asyncio.run(_pass())
    assert calls == [] and _state(record)[0] == cut_records.GAVE_WAY


def test_a_card_and_a_drop(person, resumed):
    calls, _answer = resumed
    _chat("cp-card")
    _chat("cp-drop")
    card = _record("cp-card", person="")
    drop = _record("cp-drop", kind=session_kind.PHONE.name)

    async def scene():
        await _pass()
        await chat_writer.drain("cp-card", timeout=5)

    asyncio.run(scene())
    assert calls == []
    assert _state(card) == (cut_records.DROPPED, "nobody to continue it as")
    assert len(_cards("cp-card")) == 1
    assert _state(drop) == (cut_records.DROPPED, "a phone turn has no resume")
    assert _cards("cp-drop") == []


def test_a_run_is_reopened_and_one_that_cannot_be_gets_the_card(person, monkeypatch):
    asked: list[str] = []

    async def _reopen(run, record):
        asked.append(run["id"])
        return run["id"] == "run-a"
    monkeypatch.setattr(run_recovery, "reopen_run", _reopen)
    records = {}
    for run_id in ("run-a", "run-b"):
        chat_id = session_kind.task_chat_id(run_id)
        _chat(chat_id)
        task_store.create_run(run_id, "task-x", AGENT, "manual", None, "do it")
        task_store.update_run(run_id, status="running", chat_id=chat_id)
        records[run_id] = _record(chat_id, kind=session_kind.TASK.name, run_id=run_id, person="")

    async def scene():
        await _pass()
        for run_id in records:
            await chat_writer.drain(session_kind.task_chat_id(run_id), timeout=5)

    asyncio.run(scene())
    assert sorted(asked) == ["run-a", "run-b"]
    assert _state(records["run-a"]) == (cut_records.CONTINUED, "its run was reopened")
    assert _state(records["run-b"]) == (cut_records.DROPPED, "its run could not be reopened")
    assert len(_cards(session_kind.task_chat_id("run-b"))) == 1


def test_two_passes_never_take_one_record(person, resumed):
    calls, answer = resumed
    gate = asyncio.Event()

    async def _slow():
        await gate.wait()
    answer.during = _slow
    _chat("cp-once")
    _record("cp-once")

    async def scene():
        first = asyncio.ensure_future(continue_pass.run_pending())
        await asyncio.sleep(0.05)
        second = await continue_pass.run_pending()      # asks the first to look again
        gate.set()
        return await first, second

    first, second = asyncio.run(scene())
    assert (first, second) == (1, 0) and len(calls) == 1


def test_at_most_two_starts_at_once_and_the_gate_does_not_wait_for_a_turn(person, resumed,
                                                                           monkeypatch):
    """Forty cut chats never start at once: two are between their claim and
    their turn's start, the next goes when a turn is on its chat (here: the
    watch runs out), never when the turn ends."""
    calls, answer = resumed
    monkeypatch.setattr(continue_pass, "_START_WATCH_S", 0.2)
    started: list[float] = []
    release = asyncio.Event()

    async def _turn():
        started.append(asyncio.get_running_loop().time())
        await release.wait()
    answer.during = _turn
    for i in range(5):
        _chat(f"cp-many-{i}")
        _record(f"cp-many-{i}")

    async def scene():
        task = asyncio.ensure_future(continue_pass.run_pending())
        for _ in range(400):
            if len(started) == 5:
                break
            await asyncio.sleep(0.01)
        release.set()
        await task

    asyncio.run(scene())
    # Every turn started while the others still ran: the gate moved on.
    assert len(started) == 5
    # The third start waited out a watch, and so did the fifth.
    assert started[2] - started[0] >= 0.15 and started[4] - started[2] >= 0.15
    assert started[1] - started[0] < 0.15 and started[3] - started[2] < 0.15


def test_the_starts_are_spaced(person, resumed, monkeypatch):
    calls, _answer = resumed
    monkeypatch.setattr(continue_pass, "_PASS_SPACING_S", 5.0)
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(delay, *a, **k):
        slept.append(delay)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    for i in range(3):
        _chat(f"cp-space-{i}")
        _record(f"cp-space-{i}")
    asyncio.run(_pass())
    assert [d for d in slept if d >= 5.0] == [5.0, 10.0]


def test_a_session_this_process_still_holds_is_closed_first(person, resumed, monkeypatch):
    from contextlib import asynccontextmanager

    from core.session import session_manager
    calls, _answer = resumed
    order: list[str] = []
    monkeypatch.setattr(session_manager, "find_layer_for_session", lambda sid: object())

    @asynccontextmanager
    async def _closed(chat_id, *, reason, by, claim=None):
        order.append(f"closed {chat_id} {reason} {by}")
        yield None
    monkeypatch.setattr(chat_rebuild, "closed", _closed)
    _chat("cp-held-session")
    _record("cp-held-session", cause=cut_records.CAUSE_MACHINE)
    asyncio.run(_pass())
    assert order == [f"closed cp-held-session continue {PERSON}"] and len(calls) == 1

    # A turn open on the chat refuses the close: a person's turn has it.
    @asynccontextmanager
    async def _busy(chat_id, *, reason, by, claim=None):
        raise chat_rebuild.ChatBusy(chat_id)
        yield None
    monkeypatch.setattr(chat_rebuild, "closed", _busy)
    _chat("cp-busy")
    busy = _record("cp-busy")
    asyncio.run(_pass())
    assert len(calls) == 1 and _state(busy)[0] == cut_records.GAVE_WAY


def test_the_plan_a_cut_turn_implemented_is_marked_when_the_continue_ends(person, resumed):
    _chat("cp-plan")
    task_store.add_chat_plan("cp-plan", "plan.md", "the plan")
    _record("cp-plan", plan="plan.md")

    async def scene():
        await _pass()
        await chat_writer.drain("cp-plan", timeout=5)

    asyncio.run(scene())
    assert [p["status"] for p in task_store.get_chat_plans("cp-plan")] == ["implemented"]


def test_a_session_gone_while_the_platform_ran_gets_a_record_the_pass_takes(person, monkeypatch):
    """The entry for whoever reads a machine's complete report: the record
    is built from what the platform holds for the session."""
    from core.session import session_state
    kicked: list[int] = []
    monkeypatch.setattr(continue_pass, "kick", lambda: kicked.append(1))
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda sid: SimpleNamespace(username="viewer-name"))
    monkeypatch.setattr(session_state, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(session_state, "recorded_session_mode", lambda sid: "ask")
    monkeypatch.setattr(task_store, "get_user_sub_by_username",
                        lambda name: PERSON if name == "viewer-name" else None)
    _chat("cp-lost")
    assert asyncio.run(continue_pass.session_gone("cp-lost", "sid-cp-lost", "machine-1")) is True
    [record] = cut_records.in_state(cut_records.PENDING)
    assert (record["cause"], record["person"], record["kind"], record["mode"],
            record["machine_id"], record["session_id"]) == (
        cut_records.CAUSE_MACHINE, PERSON, "dashboard", "ask", "machine-1", "sid-cp-lost")
    assert kicked == [1]
    assert continue_pass.judge(record).action == continue_pass.CONTINUE
    assert asyncio.run(continue_pass.session_gone("cp-no-chat", "sid", "machine-1")) is False


def test_a_session_gone_is_recorded_from_the_facts_its_caller_read_before_the_close(person, monkeypatch):
    """The close forgets the session's person, kind and mode: facts read
    before it are written as given, whatever the session state says now."""
    from core.session import session_state
    monkeypatch.setattr(continue_pass, "kick", lambda: None)
    monkeypatch.setattr(task_store, "get_user_sub_by_username",
                        lambda name: PERSON if name == "viewer-name" else None)
    assert session_state.get_session_security("sid-cp-closed") is None
    _chat("cp-closed")
    facts = session_state.SessionFacts(username="viewer-name", kind="dashboard", mode="plan")
    assert asyncio.run(continue_pass.session_gone("cp-closed", "sid-cp-closed", "machine-1", facts=facts))
    [record] = cut_records.in_state(cut_records.PENDING)
    assert (record["person"], record["kind"], record["mode"]) == (PERSON, "dashboard", "plan")
    assert continue_pass.judge(record).action == continue_pass.CONTINUE


def test_the_boot_arms_every_chat_with_a_waiting_message(person, monkeypatch):
    from core.events import input_queue
    armed: list[str] = []
    monkeypatch.setattr(input_queue, "arm", lambda chat_id, **kw: armed.append(chat_id))
    _chat("cp-q1")
    _chat("cp-q2")
    _queue("cp-q1", "q-a", "one")
    _queue("cp-q2", "q-b", "two")
    assert asyncio.run(continue_pass.arm_queues()) == 2
    assert set(armed) == {"cp-q1", "cp-q2"}


# --- the waiting reload and move ---------------------------------------------------


def _notes(chat_id: str) -> list[tuple[str, str]]:
    out = []
    for r in task_store.get_chat_messages(chat_id):
        if r.get("event_type") != wire.SYSTEM:
            continue
        block = json.loads(r["event_data"])
        if block.get("subtype") in (wire.SUBTYPE_SESSION_RELOADED, wire.SUBTYPE_CHAT_MOVED):
            out.append((block["message"], block.get("reason") or ""))
    return out


def test_a_reload_that_waited_is_done_by_the_continue_itself(person, resumed, monkeypatch):
    from services.remote import chat_reload
    written: list[tuple] = []
    monkeypatch.setattr(chat_reload, "_write_choices",
                        lambda chat, who, choices: written.append((who, choices)) or
                        ["This chat now uses work for Mail."])
    _chat("cp-reload")
    pending_rebuilds.save("cp-reload", pending_rebuilds.RELOAD, person=PERSON, wait_s=1800,
                          reason="a tool was enabled", choices={"mail": "work"})
    _record("cp-reload")

    async def scene():
        await _pass()
        await chat_writer.drain("cp-reload", timeout=5)

    asyncio.run(scene())
    assert written == [(PERSON, {"mail": "work"})]
    assert _notes("cp-reload") == [
        ("The session was reloaded: a tool was enabled. This chat now uses work for Mail.", "")]
    assert pending_rebuilds.listed() == []


def test_a_reload_or_a_move_whose_chat_is_not_continued_is_told_so(person, resumed):
    _chat("cp-not-reloaded")
    _chat("cp-not-moved")
    pending_rebuilds.save("cp-not-reloaded", pending_rebuilds.RELOAD, person=PERSON, wait_s=1800,
                          reason="x", choices={"mail": "work"})
    pending_rebuilds.save("cp-not-moved", pending_rebuilds.MOVE, person=PERSON, wait_s=1800,
                          target="machine-1")
    _record("cp-not-reloaded", person="")
    _record("cp-not-moved", person="")

    async def scene():
        await _pass()
        for chat_id in ("cp-not-reloaded", "cp-not-moved"):
            await chat_writer.drain(chat_id, timeout=5)

    asyncio.run(scene())
    assert _notes("cp-not-reloaded") == [
        ("The session was not reloaded: the platform restarted. Ask again.", wire.MOVED_FAILED)]
    assert _notes("cp-not-moved") == [
        ("The chat did not move: the platform restarted. Ask again.", wire.MOVED_FAILED)]
    assert pending_rebuilds.listed() == []


def test_a_reload_whose_session_lives_on_waits_again_and_one_past_its_wait_is_told(
        person, monkeypatch):
    from services.remote import chat_reload
    armed: list[tuple] = []
    monkeypatch.setattr(chat_reload, "_arm", lambda chat_id, asker, reason, choices, mark,
                        blocked, wait_s: armed.append((chat_id, asker.sub, asker.role, reason,
                                                       choices, blocked, round(wait_s, -2))))
    _chat("cp-lives")
    _chat("cp-late")
    _chat("cp-unsettled")
    pending_rebuilds.save("cp-lives", pending_rebuilds.RELOAD, person=PERSON, wait_s=1800,
                          reason="an account was connected", choices={"mail": "work"})
    pending_rebuilds.save("cp-late", pending_rebuilds.RELOAD, person=PERSON, wait_s=-1)
    pending_rebuilds.save("cp-unsettled", pending_rebuilds.RELOAD, person=PERSON, wait_s=1800)
    _record("cp-unsettled", state=cut_records.WAITING, machine_id="machine-1")

    async def scene():
        await continue_pass.settle_waiters()
        await chat_writer.drain("cp-late", timeout=5)

    asyncio.run(scene())
    assert armed == [("cp-lives", PERSON, roles.EDITOR, "an account was connected",
                      {"mail": "work"}, None, 1800.0)]
    assert _notes("cp-late") == [
        ("The session was not reloaded: the turn did not end within 30 minutes.",
         wire.MOVED_FAILED)]
    # A chat whose record is not settled keeps its row for that record.
    assert sorted(r["chat_id"] for r in pending_rebuilds.listed()) == ["cp-lives", "cp-unsettled"]


def test_the_first_boot_on_a_191_database_writes_todays_card_and_continues_nothing(
        person, resumed):
    """A row 1.9.1 left (a crash) has no facts. The first 2.0 boot writes
    today's card for it and continues nothing. The database carries no mark
    that a server kept records on it: ``test_run_reopen`` and
    ``test_downtime_fires`` prove what that means for its runs and tasks."""
    from services.scheduler import cut_turns
    from storage.chat.schema import migrate_chat
    calls, _ = resumed
    _chat("cp-191")
    with task_store.get_conn() as conn:
        conn.execute("DROP TABLE chat_open_turns")
        conn.execute("""
            CREATE TABLE chat_open_turns (
                chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                turn_token TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                engine_open BOOLEAN NOT NULL DEFAULT TRUE,
                early_ranges TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("INSERT INTO chat_open_turns (chat_id, turn_token, session_id, started_at) "
                     "VALUES ('cp-191', 'tok', 'sid-cp-191', '2026-01-01T00:00:00+00:00')")
        migrate_chat(conn)
        conn.commit()

    boot = platform_marks.read_at_boot()
    assert boot.stamped is False and boot.catches_up is False
    assert cut_turns.record_at_boot(clean_stop=boot.clean_stop, catches_up=boot.catches_up)["pending"] == 1
    record = cut_records.unsettled_of("cp-191")
    assert record["kind"] == "" and record["person"] == ""

    asyncio.run(_pass())
    assert calls == [] and _wakes("cp-191") == []
    assert _state(record)[0] == cut_records.DROPPED
    assert len(_cards("cp-191")) == 1


# --- what follows the pass, the hold, a record that cannot start -----------------------


def test_the_pass_returns_once_a_turn_is_on_the_chat_and_settles_it_at_its_end(
        person, monkeypatch):
    """What follows the pass at boot never waits for a continued turn, which
    may park on a person for days."""
    from core.events import stream_pump
    _chat("cp-long")
    record = _record("cp-long")
    ends = {}

    async def _long(chat_id, text, **kw):
        ends["turn"] = asyncio.Event()
        monkeypatch.setitem(stream_pump._active_pumps, chat_id, object())
        await ends["turn"].wait()
        return DeliveryOutcome("oneshot", chat_id=chat_id)
    monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _long)

    async def scene():
        assert await asyncio.wait_for(continue_pass.run_pending(), 5) == 1
        assert cut_records.get(record["id"])["state"] == cut_records.RUNNING
        ends["turn"].set()
        for _ in range(100):
            if cut_records.get(record["id"])["state"] != cut_records.RUNNING:
                break
            await asyncio.sleep(0.02)

    asyncio.run(scene())
    assert _state(record)[0] == cut_records.CONTINUED


def test_the_pass_takes_nothing_while_new_work_is_held(person, resumed, monkeypatch):
    """A start under a hold answers none and would spend the record's
    attempts. The resume passes it."""
    from core import drain
    calls, _ = resumed
    _chat("cp-held")
    record = _record("cp-held")
    monkeypatch.setattr(drain, "_state", drain.DRAINING)
    assert asyncio.run(_pass()) == 0
    asyncio.run(continue_pass.sweep())
    row = cut_records.get(record["id"])
    assert (row["state"], row["attempts"]) == (cut_records.PENDING, 0) and calls == []
    monkeypatch.setattr(drain, "_state", drain.RUNNING)
    assert asyncio.run(_pass()) == 1
    assert _state(record)[0] == cut_records.CONTINUED and len(calls) == 1


def test_a_record_out_of_attempts_gets_the_card_and_never_strands_its_chat(person, resumed):
    """A crash loop puts a claimed record back with its attempts spent: it is
    settled with the card, or the chat's stored wakes would wait for ever."""
    calls, _ = resumed
    _chat("cp-spent")
    record = _record("cp-spent")
    with task_store.get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET attempts=%s WHERE id=%s",
                     (continue_pass.ATTEMPTS_MAX, record["id"]))
        conn.commit()
    asyncio.run(_pass())
    assert calls == [] and _state(record) == (cut_records.DROPPED, "it could not be started")
    assert len(_cards("cp-spent")) == 1
    assert cut_records.unsettled_of("cp-spent") is None


def test_a_third_attempt_that_raises_gets_the_card(person, monkeypatch):
    _chat("cp-raises")
    record = _record("cp-raises")
    with task_store.get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET attempts=%s WHERE id=%s",
                     (continue_pass.ATTEMPTS_MAX - 1, record["id"]))
        conn.commit()

    def _broken(_record):
        raise RuntimeError("the store went away")
    monkeypatch.setattr(continue_pass, "judge", _broken)
    asyncio.run(_pass())
    assert _state(record) == (cut_records.DROPPED, "it could not be started")
    assert len(_cards("cp-raises")) == 1


def test_an_earlier_attempt_that_raises_is_left_for_the_next_sweep(person, monkeypatch):
    _chat("cp-again")
    record = _record("cp-again")

    def _broken(_record):
        raise RuntimeError("the store went away")
    monkeypatch.setattr(continue_pass, "judge", _broken)
    asyncio.run(_pass())
    row = cut_records.get(record["id"])
    assert (row["state"], row["attempts"]) == (cut_records.PENDING, 1)


def test_a_spoken_conversations_turn_has_no_resume(person):
    _chat("cp-voice")
    verdict = continue_pass.judge(_record("cp-voice", kind=session_kind.DUPLEX_TURN))
    assert verdict.action == continue_pass.DROP


def test_a_run_whose_chat_a_person_took_ends_with_its_one_report(person, resumed, monkeypatch):
    """The person's turn is not the run's round. The run ends here, and its
    delegator hears it once."""
    calls, _ = resumed
    reported: list[tuple] = []

    async def _fail(run_id, chat_id, reason, **_kw):
        reported.append((run_id, chat_id, reason))
    monkeypatch.setattr(run_recovery, "_fail_and_report", _fail)
    _chat("cp-worker")
    record = _record("cp-worker", run_id="run-cp", cause=cut_records.CAUSE_STOP)
    with task_store.get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET cut_at=%s WHERE id=%s",
                     ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), record["id"]))
        conn.commit()
    task_store.add_chat_message("cp-worker", "user", "I write first.")
    asyncio.run(_pass())
    assert calls == [] and _state(record)[0] == cut_records.GAVE_WAY
    assert reported == [("run-cp", "cp-worker", run_recovery.RUN_CUT)]


def test_a_crash_loop_reaches_the_card_on_the_fourth_start(person, resumed):
    """A turn that brings the platform down is continued three times, then the
    chat gets the card. Each continue's own open-turn row becomes the next
    record, and the record it replaced counts as a start."""
    from services.scheduler import cut_turns
    from storage.chat import open_turns
    calls, _ = resumed
    _chat("cp-loop")
    verdicts = []
    for i in range(5):
        open_turns.open_turn("cp-loop", f"tok{i}", "sid-cp-loop", person=PERSON, kind="dashboard", mode="ask")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        [pending] = [r for r in cut_records.in_state(cut_records.PENDING) if r["chat_id"] == "cp-loop"]
        verdicts.append(continue_pass.judge(cut_records.claim(pending["id"])).action)
    assert verdicts[:3] == [continue_pass.CONTINUE] * 3
    assert verdicts[3:] == [continue_pass.CARD] * 2
    with task_store.get_conn() as conn:
        states = [r["state"] for r in conn.execute(
            "SELECT state FROM chat_cut_turns WHERE chat_id='cp-loop' ORDER BY id").fetchall()]
    assert states[:3] == [cut_records.CONTINUED] * 3 and calls == []


# --- past a long downtime, or after a restore, nothing is taken up -------------------


def _old_stamp(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.mark.parametrize("stamp", ["", "old"], ids=["restored", "down-for-90-days"])
def test_after_a_restore_or_a_long_downtime_a_chat_gets_the_card_and_nothing_starts(
        person, resumed, stamp):
    """A dump holds the open-turn rows of its day. A restore removes the
    marks, and a long downtime reads as old ones: neither continues a chat."""
    from services.scheduler import cut_turns
    from storage.chat import open_turns
    calls, _ = resumed
    task_store.set_platform_setting(platform_marks.ALIVE_AT, _old_stamp(90) if stamp else "")
    _chat("cp-old")
    open_turns.open_turn("cp-old", "tok", "sid-cp-old", person=PERSON, kind="dashboard", mode="ask")
    boot = platform_marks.read_at_boot()
    assert boot.catches_up is False and boot.stamped is bool(stamp)
    cut_turns.record_at_boot(clean_stop=boot.clean_stop, catches_up=boot.catches_up)
    record = cut_records.unsettled_of("cp-old")
    assert record["cause"] == cut_records.CAUSE_NOT_TAKEN_UP
    assert continue_pass.judge(record).action == continue_pass.CARD
    asyncio.run(_pass())
    assert calls == [] and _wakes("cp-old") == [] and len(_cards("cp-old")) == 1
    assert _state(record) == (cut_records.DROPPED, "the platform was down too long or was restored")


def test_a_day_of_downtime_is_still_taken_up(person):
    """Anything under the bound continues: a stamp with a turn of its own."""
    from services.scheduler import cut_turns
    from storage.chat import open_turns
    task_store.set_platform_setting(platform_marks.ALIVE_AT, (
        datetime.now(timezone.utc) - timedelta(hours=20)).isoformat())
    _chat("cp-day")
    open_turns.open_turn("cp-day", "tok", "sid-cp-day", person=PERSON, kind="dashboard", mode="ask")
    boot = platform_marks.read_at_boot()
    assert boot.catches_up is True
    cut_turns.record_at_boot(clean_stop=False, catches_up=boot.catches_up)
    assert continue_pass.judge(cut_records.unsettled_of("cp-day")).action == continue_pass.CONTINUE


def test_a_boot_after_a_refused_boot_continues_only_what_was_cut_since(person):
    """A process that ends inside the boot's settle leaves a stamp that reads
    as caught up. The dump's turn keeps its answer, and a turn cut after the
    refusal is a turn of its own."""
    from services.scheduler import cut_turns
    from storage.chat import open_turns
    _chat("cp-dump")
    open_turns.open_turn("cp-dump", "tok", "sid-cp-dump", person=PERSON, kind="dashboard", mode="ask")
    first = platform_marks.read_at_boot()
    assert first.catches_up is False
    cut_turns.record_at_boot(clean_stop=first.clean_stop, catches_up=first.catches_up)
    platform_marks.stamp_at_boot()
    _chat("cp-new")
    open_turns.open_turn("cp-new", "tok", "sid-cp-new", person=PERSON, kind="dashboard", mode="ask")
    second = platform_marks.read_at_boot()
    assert second.catches_up is True
    cut_turns.record_at_boot(clean_stop=second.clean_stop, catches_up=second.catches_up)
    assert continue_pass.judge(cut_records.unsettled_of("cp-dump")).action == continue_pass.CARD
    assert continue_pass.judge(cut_records.unsettled_of("cp-new")).action == continue_pass.CONTINUE


def test_a_restored_turn_on_a_machine_gets_the_card_after_a_routine_restart(person):
    """The machine is away at the restore, so its record waits. A later
    restart reads as caught up, and the machine's report does not continue a
    chat of the dump. Its line never promises that it will."""
    from services.scheduler import cut_turns
    from storage.chat import open_turns
    _chat("cp-mach")
    open_turns.open_turn("cp-mach", "tok", "sid-cp-mach", person=PERSON, kind="dashboard", machine_id="m-1")
    first = platform_marks.read_at_boot()
    cut_turns.record_at_boot(clean_stop=first.clean_stop, catches_up=first.catches_up)
    platform_marks.stamp_at_boot()
    [waiting] = cut_records.in_state(cut_records.WAITING)
    assert waiting["cause"] == cut_records.CAUSE_NOT_TAKEN_UP
    second = platform_marks.read_at_boot()
    cut_turns.record_at_boot(clean_stop=second.clean_stop, catches_up=second.catches_up)
    assert second.catches_up is True and cut_records.make_pending(waiting["id"])
    assert continue_pass.judge(cut_records.get(waiting["id"])).action == continue_pass.CARD

    asyncio.run(cut_turns._write_waiting(waiting))
    [line] = [json.loads(r["event_data"])["message"] for r in task_store.get_chat_messages("cp-mach")
              if r.get("event_type") == wire.SYSTEM]
    assert line == cut_turns.WAITING_LINE_NOT_TAKEN_UP and "does not continue by itself" in line


def test_a_session_a_machine_lost_is_not_judged_by_the_boots_marks(person, monkeypatch):
    """The platform stayed up: the marks of its last boot say nothing of it."""
    monkeypatch.setattr(platform_marks, "_boot", platform_marks.Boot())
    _chat("cp-machine")
    verdict = continue_pass.judge(_record("cp-machine", cause=cut_records.CAUSE_MACHINE))
    assert verdict.action == continue_pass.CONTINUE


def test_a_machine_away_past_the_bound_gets_the_card(person, resumed):
    """A record that waited a week is not continued by itself on the
    machine's return."""
    calls, _ = resumed
    _chat("cp-away")
    record = _record("cp-away")
    with task_store.get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET cut_at=%s WHERE id=%s", (_old_stamp(7), record["id"]))
        conn.commit()
    assert continue_pass.judge(cut_records.get(record["id"])).reason == "its machine was away too long"
    asyncio.run(_pass())
    assert calls == [] and _state(record)[0] == cut_records.DROPPED and len(_cards("cp-away")) == 1


# --- a record that gets the card ends its run, with the one report -------------------


def _live_run(chat_id: str, run_id: str, *, status: str = "running") -> None:
    task_store.create_run(run_id, "task-x", AGENT, "manual", None, "do it", "delegate", "agent", None)
    task_store.update_run(run_id, status=status, chat_id=chat_id, session_id=f"sid-{chat_id}")


@pytest.fixture
def told(monkeypatch):
    """What the recovery tells a delegator: the run ids it reported."""
    from services.scheduler import scheduler
    said: list[str] = []

    async def _deliver(task, final, output, **kw):
        said.append(kw.get("run_id"))
    monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
    monkeypatch.setattr(task_store, "get_dynamic_task", lambda task_id: {"id": task_id})
    monkeypatch.setattr(scheduler, "_row_to_task", lambda row: SimpleNamespace(id=row["id"]))
    return said


def test_a_capped_run_fails_and_its_delegator_is_told_once(person, told):
    """A lane reopened three times and cut a fourth does not stay live: every
    later boot would give it another record and another card."""
    _chat("task-run-cap")
    _live_run("task-run-cap", "run-cap")
    for _ in range(continue_pass.CHAIN_MAX):
        done = cut_records.add("task-run-cap", cause="stop", state=cut_records.PENDING, kind="task",
                               run_id="run-cap", session_id="sid-task-run-cap")
        cut_records.claim(done["id"])
        cut_records.settle(done["id"], cut_records.CONTINUED)
    record = cut_records.add("task-run-cap", cause="stop", state=cut_records.PENDING, kind="task",
                             run_id="run-cap", session_id="sid-task-run-cap")
    asyncio.run(_pass())
    assert _state(record)[0] == cut_records.DROPPED and len(_cards("task-run-cap")) == 1
    run = task_store.get_run("run-cap")
    assert run["status"] == "failed" and run["error_message"] == run_recovery.RUN_CUT
    assert told == ["run-cap"]


def test_a_card_for_a_run_already_over_tells_nobody_again(person, told):
    _chat("task-run-over")
    _live_run("task-run-over", "run-over", status="completed")
    record = _record("task-run-over", kind="task", run_id="run-over")
    asyncio.run(_pass())
    assert _state(record) == (cut_records.DROPPED, "its run is over") and told == []
    assert task_store.get_run("run-over")["status"] == "completed"


def test_the_report_of_a_run_does_not_hold_the_pass(person, monkeypatch):
    """It goes on beside the record's place in the gate: the boot's next steps and
    the next records go on, and the record settles when the report is done."""
    ends = {}

    async def _report(run_id, chat_id, reason, **_kw):
        ends["report"] = asyncio.Event()
        await ends["report"].wait()
    monkeypatch.setattr(run_recovery, "_fail_and_report", _report)
    _chat("cp-slow")
    record = _record("cp-slow", kind="task", run_id="run-gone")

    async def scene():
        assert await asyncio.wait_for(continue_pass.run_pending(), 5) == 1
        assert cut_records.get(record["id"])["state"] == cut_records.RUNNING
        ends["report"].set()
        for _ in range(100):
            if cut_records.get(record["id"])["state"] != cut_records.RUNNING:
                break
            await asyncio.sleep(0.02)

    asyncio.run(scene())
    assert _state(record)[0] == cut_records.DROPPED


def test_a_chat_with_a_run_takes_the_door_of_any_continue(person):
    """A worker's chat a person drove: the run's gate, as for their own send."""
    _chat("cp-gate")
    task_store.create_run("run-gate", "task-x", AGENT, "manual", None, "do it", "delegate", "agent", None)
    task_store.update_run("run-gate", status="completed", chat_id="cp-gate", completed_at=_old_stamp(0))
    assert continue_pass.judge(_record("cp-gate")).action == continue_pass.CONTINUE
    _role(PERSON, roles.VIEWER)
    verdict = continue_pass.judge(_record("cp-gate"))
    assert (verdict.action, verdict.reason) == (continue_pass.CARD, "its person may not continue this chat")
