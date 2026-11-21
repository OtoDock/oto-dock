"""A model a person picks inside a terminal reaches the chat's row
(``interactive_turn._note_model_chosen``): one write in the terminal's pump,
read from the engine's own tailer by its capability, only for a person's
terminal and only for a model the agent may run there, so the chat's next
start comes back on it and never on a model a warm-up refuses."""

import asyncio
import dataclasses
import json

import pytest

import config as app_config
from core.events import chat_writer
from core.session import interactive_turn, session_state
from core.session.interactive_session import InteractiveSession
from core.session.session_manager import capabilities_for_path
from storage import database as task_store
from storage.agents import agent_store
from storage.billing import subscription_store
from ws import wire_events as wire

pytestmark = pytest.mark.asyncio

CLAUDE, CODEX = "claude-code-cli", "codex-cli"


def _terminal(chat_id: str, path: str = CLAUDE, sid: str = "sid-model") -> InteractiveSession:
    s = InteractiveSession.__new__(InteractiveSession)
    s.session_id, s.chat_id, s.execution_path = sid, chat_id, path
    s._engine_caps = capabilities_for_path(path)
    s.on_turn_complete = None
    return s


@pytest.fixture
def rows(temp_db, monkeypatch):
    """The engines' model tables as a boot writes them, a local model on
    Codex, the agent's row, and the frames the chat's viewers get."""
    for path in (CLAUDE, CODEX):
        subscription_store.sync_builtin_models(path, app_config.get_layer_models(path))
    subscription_store.add_model(CODEX, "qwen-local", "Qwen local", provider="ollama")
    agent = {"slug": "pa", "execution_path": CLAUDE, "execution_paths": json.dumps([CODEX]),
             "default_model": "", "local_only_engines": ""}
    monkeypatch.setattr(agent_store, "get_agent", lambda slug: dict(agent) if slug == "pa" else None)
    frames: list[tuple[str, dict]] = []
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda chat_id, frame, **_k: frames.append((chat_id, frame)))
    return agent, frames


async def _pick(s: InteractiveSession, model: str) -> None:
    s._note_model_chosen(model)
    await chat_writer.drain(s.chat_id, timeout=5)
    await asyncio.sleep(0)


def _model(chat_id: str) -> str:
    return (task_store.get_chat(chat_id) or {}).get("model") or ""


async def test_a_persons_pick_becomes_the_chats_model_and_the_menu_follows(rows):
    _agent, frames = rows
    task_store.create_chat("tm-1", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE)
    await _pick(_terminal("tm-1"), "claude-opus-5-5")
    assert _model("tm-1") == "claude-opus-5-5"
    assert frames == [("tm-1", {"type": wire.MODEL_CHANGED, "model": "claude-opus-5-5", "chat_id": "tm-1"})]


async def test_the_rows_own_model_writes_nothing_and_tells_nobody(rows):
    _agent, frames = rows
    task_store.create_chat("tm-2", "user-1", "pa", model="claude-opus-5-5", execution_path=CLAUDE)
    await _pick(_terminal("tm-2"), "claude-opus-5-5")
    assert frames == []


async def test_a_tasks_terminal_and_a_delegated_workers_chat_write_nothing(rows, monkeypatch):
    _agent, frames = rows
    task_store.create_chat("tm-3", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE)
    by_callback = _terminal("tm-3")
    by_callback.on_turn_complete = lambda msg: None
    await _pick(by_callback, "claude-opus-5-5")
    by_kind = _terminal("tm-3", sid="sid-task")
    monkeypatch.setattr(session_state, "get_session_client_type",
                        lambda sid: "task" if sid == "sid-task" else "")
    await _pick(by_kind, "claude-opus-5-5")
    task_store.create_chat("task-run-3", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE,
                           source_type="task")
    await _pick(_terminal("task-run-3", sid="sid-run"), "claude-opus-5-5")
    task_store.create_chat("tm-worker", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE,
                           origin="delegated", delegate_role="worker")
    await _pick(_terminal("tm-worker", sid="sid-worker"), "claude-opus-5-5")
    # A phone chat's row: only the row's kind refuses it (its terminal is no task's).
    task_store.create_chat("tm-phone", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE,
                           source_type="phone")
    await _pick(_terminal("tm-phone", sid="sid-phone"), "claude-opus-5-5")
    assert [_model(c) for c in ("tm-3", "task-run-3", "tm-worker", "tm-phone")] == ["claude-fable-5-2"] * 4
    assert frames == []


async def test_an_engine_that_reports_no_pick_writes_nothing(rows):
    task_store.create_chat("tm-4", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE)
    s = _terminal("tm-4")
    s._engine_caps = dataclasses.replace(
        s._engine_caps, runtime=dataclasses.replace(s._engine_caps.runtime, interactive_reports_model=False))
    await _pick(s, "claude-opus-5-5")
    assert _model("tm-4") == "claude-fable-5-2"


async def test_a_model_the_agent_does_not_run_there_is_never_written(rows):
    """The row must not hold a model a later start refuses: not offered on
    the chat's engine, another engine's, or a vendor model where the agent
    allows only local ones."""
    agent, frames = rows
    task_store.create_chat("tm-5", "user-1", "pa", model="claude-fable-5-2", execution_path=CLAUDE)
    s = _terminal("tm-5")
    await _pick(s, "claude-opus-4-1-20250805")   # a model the engine does not offer
    await _pick(s, "gpt-6.2-sol")                # another engine's
    await _pick(s, "<synthetic>")
    assert _model("tm-5") == "claude-fable-5-2"
    agent["local_only_engines"] = json.dumps([CODEX])
    task_store.create_chat("tm-6", "user-1", "pa", model="qwen-local", execution_path=CODEX)
    codex = _terminal("tm-6", CODEX, sid="sid-codex")
    await _pick(codex, "gpt-6.2-astra")
    assert _model("tm-6") == "qwen-local" and frames == []
    agent["local_only_engines"] = ""
    await _pick(codex, "gpt-6.2-astra")
    assert _model("tm-6") == "gpt-6.2-astra"


async def test_a_chat_switched_to_another_engine_keeps_its_model(rows):
    task_store.create_chat("tm-7", "user-1", "pa", model="gpt-6.1-sol", execution_path=CODEX)
    await _pick(_terminal("tm-7", CLAUDE), "claude-opus-5-5")
    assert _model("tm-7") == "gpt-6.1-sol"


async def test_the_pump_reads_the_pick_off_every_batch():
    """``_fold_tail_result`` is the one place both tail paths (the local
    file and a machine's forwarded lines) fold a batch."""
    import inspect
    fold = inspect.getsource(interactive_turn.InteractiveTurnMixin._fold_tail_result)
    assert 'self._note_model_chosen(result.get("model_chosen") or "")' in fold
