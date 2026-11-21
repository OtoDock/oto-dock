"""A run's own switch of account (``chat_reload.schedule_for_run``) lands as
the run's next round (``TaskRun._switch_rounds``): after a turn that ended
clean the session is closed as a model change closes it and the
conversation resumed with the platform's nudge, in the same chat, the chat
told once; a turn that did not end clean keeps the choice for the run's
next start and drops the round. The harness is ``test_run_stream_terminal``'s:
the real ``_run_task`` on ``temp_db`` with a fake layer and the builders
patched."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.events.common_events import DONE, ERROR, TEXT, CommonEvent
from core.execution_layer import ResumeVerdict
from services.remote import chat_reload
from services.scheduler import shared
from storage import database as task_store
from storage.automation import run_status
from tests.tasks.test_run_stream_terminal import _drive, _Layer, _settle

pytestmark = pytest.mark.asyncio

SID = "55555555-5555-4555-8555-555555555555"
SAID = ["This run now uses d for Mail X."]


class _RoundLayer(_Layer):
    """Every prompt and every start recorded; the second turn ends clean
    too; a resume is possible. ``stop_first``: a person presses Stop during
    the first turn (the pump's flag and the chat's abort flags, a graceful
    abort); ``limit_first``: the first turn ends on a usage-limit notice."""

    def __init__(self, *, fail_first: bool = False, stop_first: bool = False,
                 limit_first: bool = False, run_id: str = "", repeat=None,
                 start_fails: bool = False, resumable: bool = True, pin_machine: str = ""):
        super().__init__(hold=False)
        self.prompts: list[str] = []
        self.starts: list[tuple[str, bool]] = []
        self.fail_first = fail_first
        self.stop_first = stop_first
        self.limit_first = limit_first
        self.run_id = run_id
        self.repeat = repeat          # the switch asked again at every turn's start
        self.start_fails = start_fails
        self.resumable = resumable
        self.pin_machine = pin_machine

    async def start_session(self, session_id, agent_cfg):
        if self.start_fails:
            raise RuntimeError("the engine did not start")
        self.starts.append((session_id, bool(agent_cfg.resume)))

    async def send_message(self, session_id, prompt, **kw):
        self.prompts.append(prompt)
        if self.repeat is not None:
            chat_reload._task_rounds[SID] = self.repeat
        if self.pin_machine and len(self.prompts) == 1:
            from core.session import session_kind
            task_store.update_chat(session_kind.task_chat_id(self.run_id), execution_target=self.pin_machine)
        if self.fail_first and len(self.prompts) == 1:
            yield CommonEvent(type=ERROR, data={"message": "the engine fell over"})
            return
        if self.limit_first and len(self.prompts) == 1:
            yield CommonEvent(type=TEXT, data={"content": "You've hit your session limit · resets 3pm"})
            yield CommonEvent(type=DONE, data={})
            return
        yield CommonEvent(type=TEXT, data={"content": f"turn {len(self.prompts)}\n"})
        if self.stop_first and len(self.prompts) == 1:
            from core.events.stream_pump import _active_pumps
            from core.session import session_kind
            chat_id = session_kind.task_chat_id(self.run_id)
            _active_pumps[chat_id].stopped = True
            task_store.update_chat(chat_id, last_turn_aborted=True, last_abort_graceful=True)
        yield CommonEvent(type=DONE, data={})

    async def can_resume_session(self, *a, **k):
        return ResumeVerdict.RESUMABLE if self.resumable else ResumeVerdict.FRESH


def _task(task_id: str) -> shared.TaskDefinition:
    return shared.TaskDefinition(id=task_id, name="count", agent="agent-x", prompt="count",
                                 scope="agent", notification_mode="none")


async def _run(monkeypatch, layer: _RoundLayer, run_id: str, pending, delivered: list | None = None,
               after_drive=None) -> str:
    """``delivered`` collects the status each run delivers (the row keeps
    completed for a person's Stop; the status is the result's);
    ``after_drive`` runs once the run is launched, for a patch over the
    builders the launch installs."""
    from core import concurrency
    from core.session import session_manager
    from services.scheduler import delivery
    acquired: list = []

    async def _acquire(sid, kind, **kw):
        acquired.append((sid, kind))

    async def _deliver(task, status, output, **kw):
        if delivered is not None:
            delivered.append(status)

    monkeypatch.setattr(concurrency, "acquire", _acquire)
    monkeypatch.setattr(delivery, "_deliver_task_result", _deliver)
    monkeypatch.setattr(session_manager, "find_layer_for_session",
                        lambda sid: layer if sid == SID else None)
    task = _task(f"t-{run_id}")
    t, _q = _drive(monkeypatch, layer, task, run_id, SID)
    if after_drive is not None:
        after_drive()
    # The run's own call lands while its first turn runs.
    chat_reload._task_rounds[SID] = pending
    await asyncio.wait_for(t, 30)
    chat_id = task_store.get_run(run_id)["chat_id"]
    await _settle(chat_id)
    return chat_id


def _notes(chat_id: str) -> list[str]:
    out = []
    for m in task_store.get_chat_messages(chat_id):
        if m.get("event_type") == "system":
            block = json.loads(m.get("event_data") or "{}")
            if block.get("subtype") == "session_reloaded":
                out.append(block["message"])
    return out


async def test_the_switch_lands_as_the_runs_next_round_in_the_same_chat(temp_db, monkeypatch):
    layer = _RoundLayer()
    chat_id = await _run(monkeypatch, layer, "run-round-1", ("the other mailbox", SAID))
    # The first turn, then the resumed one with the platform's nudge.
    assert layer.prompts == [
        "count",
        "The session was reloaded: the other mailbox. This run now uses d for Mail X. "
        "Continue where you were.",
    ]
    assert layer.starts == [(SID, False), (SID, True)]
    assert layer.closes == 1
    assert _notes(chat_id) == ["The session was reloaded: the other mailbox. "
                               "This run now uses d for Mail X."]
    row = task_store.get_run("run-round-1")
    assert row["status"] == run_status.COMPLETED and row["session_id"] == SID
    # One result for the whole run: both turns' words.
    assert "turn 1" in row["output_text"] and "turn 2" in row["output_text"]
    assert SID not in chat_reload._task_rounds


async def test_a_turn_that_did_not_end_clean_keeps_the_choice_and_drops_the_round(temp_db, monkeypatch):
    layer = _RoundLayer(fail_first=True)
    chat_id = await _run(monkeypatch, layer, "run-round-2", ("the other mailbox", SAID))
    assert layer.prompts == ["count"]
    assert layer.starts == [(SID, False)] and layer.closes == 0
    assert _notes(chat_id) == []
    assert SID not in chat_reload._task_rounds


async def test_a_persons_stop_ends_the_run_with_no_round(temp_db, monkeypatch):
    """A Stop pressed during the turn: the turn ends with DONE and no error,
    yet no round starts, the run reads as interrupted, and the choice stays
    for the run's next start."""
    layer = _RoundLayer(stop_first=True, run_id="run-round-3")
    delivered: list[str] = []
    chat_id = await _run(monkeypatch, layer, "run-round-3", ("the other mailbox", SAID), delivered)
    assert layer.prompts == ["count"]
    assert layer.starts == [(SID, False)] and layer.closes == 0
    assert _notes(chat_id) == []
    assert delivered == [run_status.USER_INTERRUPTED]
    assert SID not in chat_reload._task_rounds


async def test_the_chain_bound_ends_the_rounds_and_the_run_drops_what_is_left(temp_db, monkeypatch):
    """A run whose every turn asks for a switch again gets ``CHAIN_MAX``
    rounds, then stops; the round left pending is dropped with the run."""
    pending = ("the other mailbox", SAID)
    layer = _RoundLayer(repeat=pending)
    await _run(monkeypatch, layer, "run-round-5", pending)
    assert len(layer.prompts) == 1 + chat_reload.CHAIN_MAX
    assert layer.closes == chat_reload.CHAIN_MAX
    assert SID not in chat_reload._task_rounds


async def test_a_round_runs_where_the_first_round_ran(temp_db, monkeypatch):
    """The round's task is pinned to the chat's machine, so the switch
    resumes the session where it ran."""
    from core.config import task_config_builder
    layer = _RoundLayer(run_id="run-round-6", pin_machine="m-pinned")
    seen: list[str | None] = []

    def after_drive():
        stub = task_config_builder.build_task_agent_config

        async def recording(agent, task_def, sid, **kw):
            seen.append(task_def.target_machine)
            return await stub(agent, task_def, sid, **kw)
        monkeypatch.setattr(task_config_builder, "build_task_agent_config", recording)

    await _run(monkeypatch, layer, "run-round-6", ("the other mailbox", SAID), after_drive=after_drive)
    # The first build is the task's own (no pin); the round's is pinned.
    assert len(layer.prompts) == 2 and seen == ["", "m-pinned"]


async def test_a_round_that_cannot_resume_runs_the_prompt_again_with_the_nudge(temp_db, monkeypatch):
    layer = _RoundLayer(resumable=False)
    await _run(monkeypatch, layer, "run-round-7", ("the other mailbox", SAID))
    assert len(layer.prompts) == 2 and layer.starts[1][1] is False
    assert layer.prompts[1].startswith("count\n\nThe session was reloaded: the other mailbox.")
    assert layer.prompts[1].endswith("Continue where you were.")


async def test_a_run_that_never_started_drops_its_round(temp_db, monkeypatch):
    layer = _RoundLayer(start_fails=True)
    await _run(monkeypatch, layer, "run-round-8", ("the other mailbox", SAID))
    assert layer.prompts == [] and SID not in chat_reload._task_rounds
    assert task_store.get_run("run-round-8")["status"] == run_status.FAILED


async def test_a_turn_that_ended_on_a_usage_limit_runs_no_round(temp_db, monkeypatch):
    layer = _RoundLayer(limit_first=True)
    chat_id = await _run(monkeypatch, layer, "run-round-4", ("the other mailbox", SAID))
    assert layer.prompts == ["count"]
    assert layer.closes == 0 and _notes(chat_id) == []
    row = task_store.get_run("run-round-4")
    assert row["status"] == run_status.FAILED and "usage limit" in (row.get("error_message") or "")
