"""Codex fixes ``model_provider`` in config.toml at session start, so the
dashboard refuses a mid-chat model change that would cross providers (local
endpoint ↔ OpenAI) and tells the user to start a new chat."""

import json
import os
import sys

from tests._paths import PROXY_DIR as _PROXY_DIR

if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import pytest

import config
import ws.dashboard  # noqa: F401  — the chat module is imported through the WS package
from services.engines import subscription_pool
from storage.billing import subscription_store
from ws.dashboard_chat import _provider_switch_blocker


def _wire(monkeypatch, bound_provider, sub_id="sub-1"):
    monkeypatch.setattr(subscription_pool, "get_session_subscription", lambda sid: sub_id)
    monkeypatch.setattr(subscription_store, "get_subscription",
                        lambda sid: {"id": sid, "provider": bound_provider} if sid else None)
    monkeypatch.setattr(config, "get_model_provider", lambda model, layer="": {
        "gpt-5.6-sol": "openai",
        "qwen3.6-35b-a3b": "openai_compatible",
    }[model])


def test_cross_provider_switch_is_refused(monkeypatch):
    _wire(monkeypatch, "openai")
    reason = _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli")
    assert "Start a new chat" in reason
    assert "OpenAI" in reason and "OpenAI-compatible server" in reason  # the descriptor's labels


def test_same_provider_switch_is_allowed(monkeypatch):
    _wire(monkeypatch, "openai_compatible")
    assert _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli") == ""


def test_unbound_session_is_not_guarded(monkeypatch):
    _wire(monkeypatch, "openai", sub_id=None)
    assert _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli") == ""
    _wire(monkeypatch, "openai", sub_id="default")
    assert _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli") == ""


@pytest.mark.parametrize("recorded,local,changed", [
    ("openai", False, False), ("oto_local", True, False),
    ("openai", True, True), ("oto_local", False, True), ("", True, False),
])
def test_a_terminal_resume_across_a_provider_change_is_spotted(tmp_path, monkeypatch, recorded, local, changed):
    """From Codex 0.168 a terminal resumes a thread on the provider its
    rollout recorded, whatever ``config.toml`` names: the spawn reads the
    rollout's provider and starts fresh instead (reseeded from the chat's
    history)."""
    from core.layers.codex.layer import _resume_provider_changed
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)   # found and read beneath an agent's folder only
    home = tmp_path / "ag" / "workspace" / ".codex"
    day = home / "sessions" / "2026" / "10" / "30"
    day.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"model_provider": recorded}} if recorded else {}
    (day / "rollout-2026-10-30T01-00-00-tid-1.jsonl").write_text(json.dumps(meta) + "\n")
    assert _resume_provider_changed(str(home), "tid-1", local) is changed
    assert _resume_provider_changed(str(home), "tid-gone", local) is False


def _home(tmp_path, monkeypatch):
    """A Codex home under the test's own agents root, and a folder beside the
    agent holding a stand-in rollout of the thread."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    home = tmp_path / "ag" / "users" / "u" / ".codex"
    (home / "sessions").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    meta = {"type": "session_meta", "payload": {"model_provider": "openai"}}
    (elsewhere / "rollout-2026-11-13T00-00-00-tid-9.jsonl").write_text(json.dumps(meta) + "\n")
    return home, elsewhere


def _within(seconds, fn):
    import threading
    out: list = []
    th = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    th.start()
    th.join(seconds)
    return out


def test_a_folder_loop_under_sessions_answers_at_once(tmp_path, monkeypatch):
    from core.layers.codex.layer import _resume_provider_changed
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    for name in ("a", "b", "c"):
        (home / "sessions" / name).symlink_to(home / "sessions")
    assert _within(10, lambda: rollouts.find_rollout(str(home), "tid-9")) == [(rollouts.MISSING, None)]
    assert _within(10, lambda: _resume_provider_changed(str(home), "tid-9", True)) == [False]


def test_a_linked_folder_hides_no_rollout_from_elsewhere(tmp_path, monkeypatch):
    from core.layers.codex.layer import _resume_provider_changed
    from core.session import codex_rollout_tailer as rollouts
    home, elsewhere = _home(tmp_path, monkeypatch)
    (home / "sessions" / "2026").symlink_to(elsewhere)
    assert rollouts.find_rollout(str(home), "tid-9") == (rollouts.MISSING, None)
    assert _resume_provider_changed(str(home), "tid-9", True) is False


def test_a_pipe_at_the_rollout_name_is_never_opened(tmp_path, monkeypatch):
    from core.layers.codex.layer import _resume_provider_changed
    home, _ = _home(tmp_path, monkeypatch)
    from core.session import codex_rollout_tailer as rollouts
    os.mkfifo(home / "sessions" / "rollout-2026-11-13T00-00-00-tid-9.jsonl")
    assert rollouts.find_rollout(str(home), "tid-9") == (rollouts.MISSING, None)
    assert _within(10, lambda: _resume_provider_changed(str(home), "tid-9", True)) == [False]


def test_a_first_record_over_the_cap_reads_as_unknown(tmp_path, monkeypatch):
    from core.layers.codex.layer import _resume_provider_changed
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    meta = {"type": "session_meta", "payload": {"model_provider": "openai", "pad": "x" * 200}}
    (home / "sessions" / "rollout-2026-11-13T00-00-00-tid-9.jsonl").write_text(json.dumps(meta) + "\n")
    assert _resume_provider_changed(str(home), "tid-9", True) is True
    monkeypatch.setattr(rollouts, "_META_MAX_BYTES", 64)
    assert _resume_provider_changed(str(home), "tid-9", True) is False


def test_the_newest_folder_is_searched_first_and_a_search_past_its_bound_is_undecided(tmp_path, monkeypatch):
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    for day in ("2026/01/01", "2026/11/13"):
        folder = home / "sessions" / day
        folder.mkdir(parents=True)
        (folder / f"rollout-{day.replace('/', '-')}T00-00-00-tid-9.jsonl").write_text("{}\n")
    state, path = rollouts.find_rollout(str(home), "tid-9")
    assert state == rollouts.FOUND and "/2026/11/13/" in path
    for i in range(6):
        (home / "sessions" / "2026" / "11" / f"x{i}").mkdir()
    (home / "sessions" / "2026" / "11" / "13" / "rollout-2026-11-13T00-00-00-tid-9.jsonl").unlink()
    (home / "sessions" / "2026" / "01" / "01" / "rollout-2026-01-01T00-00-00-tid-9.jsonl").unlink()
    monkeypatch.setattr(rollouts, "_FIND_MAX_ENTRIES", 3)
    assert rollouts.find_rollout(str(home), "tid-9") == (rollouts.UNDECIDED, None)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder with no permission bits")
def test_a_sessions_folder_it_cannot_open_is_undecided(tmp_path, monkeypatch):
    """The rollout search could not look: undecided, never a missing thread."""
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    sessions = home / "sessions"
    sessions.chmod(0)
    try:
        found = rollouts.find_rollout(str(home), "tid-9")
    finally:
        sessions.chmod(0o755)
    assert found == (rollouts.UNDECIDED, None)


def test_a_rollout_below_the_depth_bound_is_not_searched(tmp_path, monkeypatch):
    """Codex files a rollout three folders below ``sessions``: one there is
    found, and the search enters no folder more than the bound below it."""
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    day = home / "sessions" / "2026" / "11" / "13"
    day.mkdir(parents=True)
    (day / "rollout-2026-11-13T00-00-00-tid-1.jsonl").write_text("{}\n")
    assert rollouts.find_rollout(str(home), "tid-1")[0] == rollouts.FOUND
    folder = home / "sessions"
    for depth in range(1, rollouts._FIND_MAX_DEPTH + 2):
        folder = folder / f"d{depth}"
        folder.mkdir()
        (folder / f"rollout-2026-11-13T00-00-00-tid-at{depth}.jsonl").write_text("{}\n")
    assert rollouts.find_rollout(str(home), f"tid-at{rollouts._FIND_MAX_DEPTH}")[0] == rollouts.FOUND
    assert rollouts.find_rollout(str(home), f"tid-at{rollouts._FIND_MAX_DEPTH + 1}") == (rollouts.MISSING, None)


def test_a_search_past_its_bound_in_all_is_undecided_with_every_folder_under_it(tmp_path, monkeypatch):
    """Each folder lists under the cap, the search as a whole passes it: the
    whole-walk count answers undecided, not missing."""
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    for day in ("a", "b"):
        for leaf in ("x", "y"):
            (home / "sessions" / day / leaf).mkdir(parents=True)
    monkeypatch.setattr(rollouts, "_FIND_MAX_ENTRIES", 3)
    assert rollouts.find_rollout(str(home), "tid-9") == (rollouts.UNDECIDED, None)
    monkeypatch.setattr(rollouts, "_FIND_MAX_ENTRIES", 6)
    assert rollouts.find_rollout(str(home), "tid-9") == (rollouts.MISSING, None)


def test_a_sessions_folder_past_the_listing_cap_is_undecided_unread(tmp_path, monkeypatch):
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    for i in range(3):
        (home / "sessions" / f"x{i}").mkdir()
    (home / "sessions" / "rollout-2026-11-13T00-00-00-tid-9.jsonl").write_text("{}\n")
    monkeypatch.setattr(rollouts, "_FIND_MAX_ENTRIES", 3)
    assert rollouts.find_rollout(str(home), "tid-9") == (rollouts.UNDECIDED, None)
    monkeypatch.setattr(rollouts, "_FIND_MAX_ENTRIES", 4)
    assert rollouts.find_rollout(str(home), "tid-9")[0] == rollouts.FOUND


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder with no permission bits")
@pytest.mark.asyncio
async def test_a_day_folder_it_cannot_read_is_undecided_and_the_resume_unknown(tmp_path, monkeypatch, temp_db):
    from core.execution_layer import ResumeVerdict
    from core.layers.codex.layer import CodexCLIExecutionLayer
    from core.session import codex_rollout_tailer as rollouts
    home, _ = _home(tmp_path, monkeypatch)
    day = home / "sessions" / "2026" / "11" / "13"
    day.mkdir(parents=True)
    (day / "rollout-2026-11-13T00-00-00-tid-9.jsonl").write_text("{}\n")
    temp_db.create_chat("ud1", "user-admin", "ag")
    temp_db.update_chat("ud1", session_id="sess-ud1", codex_thread_id="tid-9")
    day.chmod(0)
    try:
        found = rollouts.find_rollout(str(home), "tid-9")
        verdict = await CodexCLIExecutionLayer().can_resume_session("sess-ud1", agent_name="ag", username="u")
    finally:
        day.chmod(0o755)
    assert found == (rollouts.UNDECIDED, None)
    assert verdict is ResumeVerdict.UNKNOWN


def test_the_terminal_reads_the_rollout_off_the_loop(monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace

    from core.execution_layer import AgentConfig
    from core.layers.codex import spawn
    from core.layers.codex.layer import CodexCLIExecutionLayer
    threads: list = []
    monkeypatch.setattr(spawn, "_resume_provider_changed",
                        lambda home, tid, local, agent="": threads.append(threading.current_thread()) or False)
    cfg = AgentConfig(agent_name="a", interactive=True, resume=True, resume_handle="tid-9",
                      sandbox_host_claude_dir="/agents/a/users/u/.codex", permission_mode="auto")
    ready = SimpleNamespace(sandbox_mode="workspace-write", codex_effort="", local_endpoint="")
    argv, _ = asyncio.run(CodexCLIExecutionLayer()._tui_argv("sid-1", cfg, ready, "/users/u"))
    assert argv[:2] == ["resume", "tid-9"]
    assert threads and threads[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_an_agent_whose_folder_is_a_link_finds_its_rollout_for_a_resume(tmp_path, monkeypatch, temp_db):
    """The operator may move an agent's folder and leave a link in its place:
    the rollout is found and read beneath the real folder for a resume, and
    a link below it still hides nothing from elsewhere."""
    from core.execution_layer import ResumeVerdict
    from core.layers.codex.layer import CodexCLIExecutionLayer, _resume_provider_changed
    from core.session import codex_rollout_tailer as rollouts
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    real = tmp_path / "disk2" / "ag"
    day = real / "users" / "u" / ".codex" / "sessions" / "2026" / "11" / "13"
    day.mkdir(parents=True)
    (agents / "ag").symlink_to(real)
    meta = {"type": "session_meta", "payload": {"model_provider": "openai"}}
    (day / "rollout-2026-11-13T00-00-00-tid-9.jsonl").write_text(json.dumps(meta) + "\n")
    for home in (agents / "ag" / "users" / "u" / ".codex", real / "users" / "u" / ".codex"):
        state, path = rollouts.find_rollout(str(home), "tid-9")
        assert state == rollouts.FOUND and path.startswith(str(home))
        assert _resume_provider_changed(str(home), "tid-9", True) is True
    temp_db.create_chat("ln1", "user-admin", "ag")
    temp_db.update_chat("ln1", session_id="sess-ln1", codex_thread_id="tid-9")
    verdict = await CodexCLIExecutionLayer().can_resume_session("sess-ln1", agent_name="ag", username="u")
    assert verdict is ResumeVerdict.RESUMABLE
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "rollout-2026-11-13T00-00-00-tid-7.jsonl").write_text(json.dumps(meta) + "\n")
    (real / "users" / "u" / ".codex" / "sessions" / "2025").symlink_to(elsewhere)
    assert rollouts.find_rollout(str(agents / "ag" / "users" / "u" / ".codex"), "tid-7") == (rollouts.MISSING, None)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a folder with no read bit")
@pytest.mark.asyncio
async def test_the_resume_gates_name_the_agent_so_an_unlistable_agents_folder_loses_nothing(
        tmp_path, monkeypatch, temp_db):
    """An agent behind the operator's link, its home recorded at the real
    place: the gates name the agent, so the owner needs no listing of the
    agents folder, and a listing that fails costs no conversation."""
    from core.execution_layer import AgentConfig, ResumeVerdict
    from core.layers.codex.layer import CodexCLIExecutionLayer, _resume_provider_changed
    from core.session import codex_rollout_tailer as rollouts
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    real = tmp_path / "disk2" / "ag"
    home = real / "users" / "u" / ".codex"
    day = home / "sessions" / "2026" / "11" / "13"
    day.mkdir(parents=True)
    (agents / "ag").symlink_to(real)
    meta = {"type": "session_meta", "payload": {"model_provider": "openai"}}
    (day / "rollout-2026-11-13T00-00-00-tid-9.jsonl").write_text(json.dumps(meta) + "\n")
    temp_db.create_chat("ul1", "user-admin", "ag")
    temp_db.update_chat("ul1", session_id="sess-ul1", codex_thread_id="tid-9")
    monkeypatch.setattr("core.session.session_state.get_session_codex_dir",
                        lambda sid: {"home": str(home)} if sid == "sess-ul1" else None)
    cfg = AgentConfig(agent_name="ag", interactive=True, resume=False, resume_handle="tid-9",
                      sandbox_host_claude_dir=str(home), permission_mode="auto")
    layer = CodexCLIExecutionLayer()
    agents.chmod(0o311)
    try:
        resumes = layer.resumes_interactive(cfg)
        verdict = await layer.can_resume_session("sess-ul1", agent_name="ag", username="u")
        changed = _resume_provider_changed(str(home), "tid-9", True, "ag")
    finally:
        agents.chmod(0o755)
    assert resumes is True
    assert verdict is ResumeVerdict.RESUMABLE
    assert changed is True
    assert rollouts.find_rollout(str(home), "tid-9", "ag")[0] == rollouts.FOUND
