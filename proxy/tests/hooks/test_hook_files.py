"""The hook files the platform writes for both CLIs (HOOKS.md "The hook
files" and "Trust"): the same four events and four scripts per scope, the
Codex object schema, the Stop hook's long timeout, and the stale-trust
assertions — nothing the platform writes can make a Codex hook silently
untrusted, and the TUI and app-server spawns carry their bypass."""

from __future__ import annotations

import json

import pytest

import config as app_config
from core.layers.cli import config_dir as cli_cd
from core.layers.codex import config_dir as codex_cd
from core.sandbox import session_config_dir as scd

EVENTS = {"PreToolUse", "PostToolUse", "SubagentStop", "Stop"}


def _agents(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents, raising=False)
    monkeypatch.setattr(app_config, "get_agent_dir", lambda name: agents / name, raising=False)
    return agents


def test_claude_settings_name_the_four_events_and_the_long_stop_timeout():
    s = cli_cd.build_settings("/users/alice/.claude")
    assert set(s["hooks"]) == EVENTS
    for ev in EVENTS:
        h = s["hooks"][ev][0]["hooks"][0]
        assert h["type"] == "command" and h["command"].startswith("/users/alice/.claude/")
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("stop_tracker.py")
    assert s["hooks"]["Stop"][0]["hooks"][0]["timeout"] == scd.STOP_HOOK_TIMEOUT_S == 604800
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == scd.LONG_HOOK_TIMEOUT_S
    assert s["hooks"]["SubagentStop"][0]["hooks"][0]["command"].endswith("subagent_tracker.py")


def test_codex_hooks_json_is_an_object_with_the_same_four_events():
    h = codex_cd.build_hooks("/workspace/.codex")
    assert isinstance(h, dict) and set(h) == {"hooks"}
    assert set(h["hooks"]) == EVENTS
    for ev in EVENTS:
        group = h["hooks"][ev]
        assert isinstance(group, list) and group[0]["matcher"] == ""
        cmd = group[0]["hooks"][0]["command"]
        assert cmd.startswith("python3 /workspace/.codex/")
    assert h["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 604800
    assert h["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("stop_tracker.py")
    assert h["hooks"]["SubagentStop"][0]["hooks"][0]["command"].endswith("subagent_tracker.py")
    # The list shape Codex rejects must never come back.
    assert not isinstance(h.get("hooks"), list)


@pytest.mark.parametrize("scope,username,expect", [
    ("user", "alice", "users/alice"),
    ("agent", "", "workspace"),
])
def test_both_dirs_carry_the_four_scripts_per_scope(tmp_path, monkeypatch, scope, username, expect):
    agents = _agents(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.sandbox.skills_materializer.materialize_skills_for_sandbox",
        lambda *a, **k: None,
    )
    claude = cli_cd.ensure_persistent_claude_dir("pa", username=username, scope=scope)
    codex = codex_cd.ensure_persistent_codex_dir("pa", username=username, scope=scope)
    assert claude == agents / "pa" / expect / ".claude"
    assert codex == agents / "pa" / expect / ".codex"
    for d in (claude, codex):
        for script in scd.HOOK_SCRIPTS:
            assert (d / script).is_file(), (d, script)
            assert (d / script).stat().st_mode & 0o111
            assert b"\r\n" not in (d / script).read_bytes()
    settings = json.loads((claude / "settings.json").read_text())
    assert set(settings["hooks"]) == EVENTS
    hooks = json.loads((codex / "hooks.json").read_text())
    assert set(hooks["hooks"]) == EVENTS
    assert set(scd.HOOK_SCRIPTS) == {
        "permission_gate.py", "tool_result_forwarder.py",
        "subagent_tracker.py", "stop_tracker.py",
    }


def test_no_writer_emits_a_codex_trust_table():
    """Trust is the per-invocation bypass, never a ``[hooks.state]`` hash a
    later edit would silently stale (HOOKS.md "Trust")."""
    from pathlib import Path
    root = Path(app_config.BASE_DIR)
    writers = [
        root / "core" / "layers" / "codex" / "layer.py",
        root / "core" / "layers" / "codex" / "config_dir.py",
        root.parent / "satellite" / "sessions" / "codex_session.py",
        root.parent / "satellite" / "terminal" / "codex_pty_session.py",
    ]
    for path in writers:
        text = path.read_text()
        assert "hooks.state" not in text and "trusted_hash" not in text, path


def test_the_two_codex_spawns_carry_their_bypass():
    from pathlib import Path
    root = Path(app_config.BASE_DIR)
    tui = (root / "core" / "layers" / "codex" / "layer.py").read_text()
    assert '"--dangerously-bypass-hook-trust"' in tui
    sat_tui = (root.parent / "satellite" / "terminal" / "codex_pty_session.py").read_text()
    assert '"--dangerously-bypass-hook-trust"' in sat_tui
    from core.layers.codex.session import CodexAppServerSession
    floored = CodexAppServerSession(
        session_id="11111111-2222-4333-8444-555555555555", agent_name="pa",
        model="gpt-6", sandbox_mode="danger-full-access", working_dir="",
        config_dir="/tmp/x", hooks_floor=True,
    )
    assert floored._thread_overrides()["config"] == {"bypass_hook_trust": True}
