"""One system prompt file per Claude Code session.

Every session of one person on one agent starts from the same ``.claude``
folder. Each writes its system prompt to a file named after its own session
id, so a second start never replaces what the first is about to read (the
close of a session removes the file its plan named and no other:
``tests/remote/test_compute_session_start.py``). The name has the shape the
never-sync rule of both trees matches, so the file stays on its host. A
start sweeps the prompt files older than a day (the terminal's builder
writes one and owns no close) and the shared file earlier releases wrote.
"""

import hashlib
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from core.layers.cli.spawn import ClaudeSpawn, _prompt_filename, remove_prompt_file
from core.remote import file_sync as proxy_fs
from tests.remote.test_file_sync_engine_contract import _satellite_constant

_DAY_S = 24 * 3600


@pytest.fixture(autouse=True)
def _agents_root(monkeypatch, tmp_path):
    # The prompt file is written beneath an agent's folder only; each test's
    # own folder is the agents root.
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)


def _mk(session_id: str, prompt: str, claude_dir: Path | None, **kw) -> ClaudeSpawn:
    return ClaudeSpawn(
        session_id=session_id,
        agent_prompt=prompt,
        mcp_config_path=None,
        model="claude-opus-5",
        effort="high",
        agent_name="agent",
        extra_env={"CLAUDE_CONFIG_DIR": str(claude_dir)} if claude_dir else None,
        **kw,
    )


def _engine_folder(tmp_path: Path) -> Path:
    """An engine folder in an agent's folder beneath the test's agents root."""
    d = tmp_path / "agent" / "workspace" / ".claude"
    d.mkdir(parents=True)
    return d


def _prompt_arg(cmd: list[str]) -> str:
    return cmd[cmd.index("--append-system-prompt-file") + 1]


def _aged(path: Path, age_s: float) -> Path:
    path.write_text("old")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def test_the_name_is_twelve_hex_of_the_session_ids_hash():
    name = _prompt_filename("sess-1")
    assert name == f"system-prompt-{hashlib.sha256(b'sess-1').hexdigest()[:12]}.json"
    assert name != _prompt_filename("sess-2")
    # No session id: one stable name of the same shape.
    assert _prompt_filename("") == _prompt_filename("")
    assert re.fullmatch(r"system-prompt-[0-9a-f]{12}\.json", _prompt_filename(""))


@pytest.mark.parametrize("session_id", ("sess-1", "0b1e6a52-8d0c-4c35-9a52-0f0a1c2d3e4f", ""))
def test_the_never_sync_rule_of_both_trees_matches_the_name(session_id):
    name = _prompt_filename(session_id)
    satellite_rule = re.compile(_satellite_constant("_CLAUDE_HOST_LOCAL_RE"))
    assert proxy_fs._CLAUDE_HOST_LOCAL_RE.match(name)
    assert satellite_rule.match(name)
    assert proxy_fs._is_claude_runtime_state(f"users/u/.claude/{name}")
    assert proxy_fs._is_claude_runtime_state(f"workspace/.claude/{name}")


def test_two_sessions_of_one_folder_write_their_own_files(tmp_path):
    d = _engine_folder(tmp_path)
    first = _mk("sess-first", "FIRST PROMPT", d)
    second = _mk("sess-second", "SECOND PROMPT", d)
    first_path = Path(_prompt_arg(first.build_spawn_command()[0]))
    second_path = Path(_prompt_arg(second.build_spawn_command()[0]))
    assert first_path != second_path
    assert first_path == d / _prompt_filename("sess-first")
    assert second_path == d / _prompt_filename("sess-second")
    assert "FIRST PROMPT" in first_path.read_text()
    assert "SECOND PROMPT" in second_path.read_text()


def test_a_start_sweeps_the_prompt_files_older_than_a_day(tmp_path):
    d = _engine_folder(tmp_path)
    stale = _aged(d / _prompt_filename("a-terminal-long-gone"), _DAY_S + 3600)
    recent = _aged(d / _prompt_filename("a-session-of-this-morning"), 3600)
    others = [_aged(d / name, _DAY_S + 3600)
              for name in ("notes.json", "agent-0123456789ab-1a2b3c4d-5e6.json",
                           "system-prompt-notes.json")]
    _mk("sess-new", "PROMPT", d).build_spawn_command()
    assert not stale.exists()
    assert recent.exists()
    assert all(p.exists() for p in others)
    assert (d / _prompt_filename("sess-new")).exists()


def test_a_start_removes_the_shared_file_of_earlier_releases(tmp_path):
    d = _engine_folder(tmp_path)
    legacy = d / "system-prompt.md"
    legacy.write_text("a prompt every session of the folder shared")
    _mk("sess-new", "PROMPT", d).build_spawn_command()
    assert not legacy.exists()
    assert "PROMPT" in (d / _prompt_filename("sess-new")).read_text()


@pytest.mark.parametrize("interactive", (False, True), ids=("headless", "terminal"))
def test_a_sandboxed_session_passes_the_sandbox_path_of_its_own_file(tmp_path, interactive):
    d = _engine_folder(tmp_path)
    builder = SimpleNamespace(
        cfg=SimpleNamespace(host_claude_dir=d),
        get_env_overrides=lambda: {"CLAUDE_CONFIG_DIR": "/users/alice/.claude"},
    )
    session = _mk("sess-boxed", "BOXED PROMPT", None, sandbox_builder=builder,
                  interactive=interactive)
    name = _prompt_filename("sess-boxed")
    assert _prompt_arg(session._build_persistent_cmd()) == f"/users/alice/.claude/{name}"
    assert "BOXED PROMPT" in (d / name).read_text()
    assert session.prompt_file == d / name


def test_a_session_with_no_config_dir_writes_its_own_file_beside_the_sessions(
        tmp_path, monkeypatch):
    # The sessions folder sits beside the agents folder, as on an install.
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    session = _mk("sess-bare", "BARE PROMPT", None)
    path = tmp_path / "sessions" / "prompt-files" / _prompt_filename("sess-bare")
    assert _prompt_arg(session._build_persistent_cmd()) == str(path)
    assert "BARE PROMPT" in path.read_text()


def test_a_link_at_the_prompt_name_is_replaced_never_written_through(tmp_path):
    claude = tmp_path / "ag" / "users" / "u" / ".claude"
    claude.mkdir(parents=True)
    victim = tmp_path / "elsewhere.json"
    victim.write_text("untouched")
    (claude / _prompt_filename("sess-l")).symlink_to(victim)
    path = Path(_prompt_arg(_mk("sess-l", "THE PROMPT", claude).build_spawn_command()[0]))
    assert victim.read_text() == "untouched"
    assert not path.is_symlink() and path.read_text() == "THE PROMPT"


def test_a_linked_engine_folder_refuses_the_start(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    claude = tmp_path / "ag" / "users" / "u" / ".claude"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(target)
    with pytest.raises(OSError):
        _mk("sess-l", "THE PROMPT", claude).build_spawn_command()
    assert list(target.iterdir()) == []


def test_a_folder_outside_the_roots_refuses_the_start(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    with pytest.raises(OSError):
        _mk("sess-o", "THE PROMPT", tmp_path / "outside").build_spawn_command()
    assert not (tmp_path / "outside").exists()


def test_the_close_removes_a_link_at_the_prompt_name_and_never_its_target(tmp_path):
    claude = tmp_path / "ag" / "users" / "u" / ".claude"
    claude.mkdir(parents=True)
    victim = tmp_path / "elsewhere.json"
    victim.write_text("untouched")
    name = claude / _prompt_filename("sess-c")
    name.symlink_to(victim)
    remove_prompt_file(str(name))
    assert not name.is_symlink() and victim.read_text() == "untouched"
    remove_prompt_file(str(name))   # already gone: no error


def test_the_close_removes_nothing_through_a_linked_folder(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / _prompt_filename("sess-c")).write_text("untouched")
    claude = tmp_path / "ag" / "users" / "u" / ".claude"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(target)
    with pytest.raises(OSError):
        remove_prompt_file(str(claude / _prompt_filename("sess-c")))
    assert (target / _prompt_filename("sess-c")).read_text() == "untouched"


def test_an_agent_whose_folder_is_a_link_writes_and_removes_its_prompt_file(tmp_path, monkeypatch):
    """The operator may move an agent's folder and leave a link in its place:
    the prompt file is written and removed beneath the real folder, and a
    link below it still refuses the start."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    real = tmp_path / "disk2" / "ag"
    (real / "users" / "u" / ".claude").mkdir(parents=True)
    (tmp_path / "agents" / "ag").symlink_to(real)
    for claude in (tmp_path / "agents" / "ag" / "users" / "u" / ".claude", real / "users" / "u" / ".claude"):
        path = Path(_prompt_arg(_mk("sess-m", "THE PROMPT", claude).build_spawn_command()[0]))
        assert (real / "users" / "u" / ".claude" / path.name).read_text() == "THE PROMPT"
        remove_prompt_file(str(path))
        assert not (real / "users" / "u" / ".claude" / path.name).exists()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (real / "users" / "v").mkdir()
    (real / "users" / "v" / ".claude").symlink_to(target)
    with pytest.raises(OSError):
        _mk("sess-l", "THE PROMPT", tmp_path / "agents" / "ag" / "users" / "v" / ".claude").build_spawn_command()
    assert list(target.iterdir()) == []
