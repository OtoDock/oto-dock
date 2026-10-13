"""The hook files the platform writes for both CLIs (HOOKS.md "The hook
files" and "Trust"): the same four events and four scripts per scope, the
Codex object schema, the Stop hook's long timeout, and the stale-trust
assertions — nothing the platform writes can make a Codex hook silently
untrusted, and the TUI and app-server spawns carry their bypass."""

from __future__ import annotations

import json
from pathlib import Path

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
    py = scd.sandbox_python()
    assert py.startswith("/")  # the resolved path, never a name PATH could shadow
    for ev in EVENTS:
        h = s["hooks"][ev][0]["hooks"][0]
        assert h["type"] == "command"
        # isolated: <python> -I "<sandbox .claude>/<hook>.py"
        assert h["command"].startswith(f'{py} -I "/users/alice/.claude/')
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"].endswith('stop_tracker.py"')
    assert s["hooks"]["Stop"][0]["hooks"][0]["timeout"] == scd.STOP_HOOK_TIMEOUT_S
    # An hour past the prompt's own wait, so the proxy always answers first.
    from core.session.session_state import PROMPT_WAIT_S
    assert scd.LONG_HOOK_TIMEOUT_S == PROMPT_WAIT_S + 3600
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == scd.LONG_HOOK_TIMEOUT_S
    assert s["hooks"]["SubagentStop"][0]["hooks"][0]["command"].endswith('subagent_tracker.py"')


def test_codex_hooks_json_is_an_object_with_the_same_four_events():
    h = codex_cd.build_hooks("/workspace/.codex")
    assert isinstance(h, dict) and set(h) == {"hooks"}
    assert set(h["hooks"]) == EVENTS
    for ev in EVENTS:
        group = h["hooks"][ev]
        assert isinstance(group, list) and group[0]["matcher"] == ""
        cmd = group[0]["hooks"][0]["command"]
        assert cmd.startswith(f'{scd.sandbox_python()} -I "/workspace/.codex/')
    assert h["hooks"]["Stop"][0]["hooks"][0]["timeout"] == scd.LONG_HOOK_TIMEOUT_S
    assert h["hooks"]["Stop"][0]["hooks"][0]["command"].endswith('stop_tracker.py"')
    assert h["hooks"]["SubagentStop"][0]["hooks"][0]["command"].endswith('subagent_tracker.py"')
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
        # The command wrappers beside them, under bin/ (the Windows one CRLF).
        for relpath in scd.COMMAND_WRAPPERS:
            assert (d / relpath).is_file(), (d, relpath)
            assert (d / relpath).stat().st_mode & 0o111
        assert b"\r\n" in (d / "bin" / "gh.cmd").read_bytes()
        assert (d / "bin" / "gh").read_bytes().startswith(b"#!/bin/sh\n")
    settings = json.loads((claude / "settings.json").read_text())
    assert set(settings["hooks"]) == EVENTS
    hooks = json.loads((codex / "hooks.json").read_text())
    assert set(hooks["hooks"]) == EVENTS
    assert set(scd.HOOK_SCRIPTS) == {
        "permission_gate.py", "tool_result_forwarder.py",
        "subagent_tracker.py", "stop_tracker.py", "credential_helper.py",
    }


@pytest.mark.parametrize("scope,username", [("user", "alice"), ("agent", "")])
def test_a_second_start_of_the_scope_replaces_no_bound_file(tmp_path, monkeypatch, scope, username):
    """A replace detaches the read-only bind a running sandbox holds on the
    name, so the files the sandbox binds by name hold nothing a later start
    of the same scope could change (SANDBOX.md)."""
    _agents(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.sandbox.skills_materializer.materialize_skills_for_sandbox",
        lambda *a, **k: None,
    )

    def bound(d: Path) -> dict[str, int]:
        names = [scd.HOOK_FILE_OF_STATE_DIR[d.name], *scd.HOOK_SCRIPTS, *scd.COMMAND_WRAPPERS]
        return {n: (d / n).stat().st_ino for n in names if (d / n).exists()}

    starts = (
        lambda: cli_cd.ensure_persistent_claude_dir("pa", username=username, scope=scope),
        lambda: codex_cd.ensure_persistent_codex_dir("pa", username=username, scope=scope),
    )
    for start in starts:
        d = start()
        first = bound(d)
        assert scd.HOOK_FILE_OF_STATE_DIR[d.name] in first
        assert bound(start()) == first


def test_a_link_planted_at_the_wrapper_directory_is_replaced(tmp_path):
    """The config dir is agent-writable: a link at ``bin`` is dropped and a
    real directory written, so the sandbox's read-only bind lands on it."""
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (cfg / "bin").symlink_to(elsewhere)
    scd.write_command_wrappers(cfg)
    assert (cfg / "bin").is_dir() and not (cfg / "bin").is_symlink()
    assert (cfg / "bin" / "gh").is_file() and not list(elsewhere.iterdir())


def test_the_wrapper_directory_holds_exactly_the_wrappers_after_a_spawn(tmp_path):
    """The dir is agent-writable while nothing is bound (before the first
    spawn of this release, or of this scope) and the sandbox then binds
    ``bin/`` read-only first on PATH: a file, a link or a subdirectory left
    there would run as the next session's command. The spawn removes
    everything that is not a wrapper, a directory planted at a wrapper's
    name included, and never follows a link out of the dir."""
    cfg = tmp_path / ".claude"
    (cfg / "bin" / "sub").mkdir(parents=True)
    (cfg / "bin" / "x").write_text("#!/bin/sh\necho planted\n")
    (cfg / "bin" / "sub" / "y").write_text("y")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("k")
    (cfg / "bin" / "lnk").symlink_to(elsewhere)
    (cfg / "bin" / "gh").mkdir()
    (cfg / "bin" / "gh" / "inner").write_text("i")
    scd.install_hook_scripts(cfg)
    assert sorted(p.name for p in (cfg / "bin").iterdir()) == sorted(Path(r).name for r in scd.COMMAND_WRAPPERS)
    for relpath, content in scd.COMMAND_WRAPPERS.items():
        assert (cfg / relpath).is_file() and (cfg / relpath).read_bytes() == content.encode()
        assert (cfg / relpath).stat().st_mode & 0o111
    assert (elsewhere / "keep").read_text() == "k"   # the link was unlinked, not followed


class TestWriteIfChanged:
    """The hook files are written at every session start; a sibling session
    of the scope may launch a hook from them at that moment. An unchanged
    file is not touched, a changed one replaced whole (HOOKS.md "The hook
    files")."""

    def test_an_unchanged_file_keeps_its_inode_and_mtime(self, tmp_path):
        f = tmp_path / "permission_gate.py"
        assert scd.write_if_changed(f, b"gate\n", 0o755) is True
        before = f.stat()
        assert scd.write_if_changed(f, b"gate\n", 0o755) is False
        after = f.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_a_changed_byte_or_mode_is_a_new_inode_and_an_old_reader_keeps_its_bytes(self, tmp_path):
        f = tmp_path / "settings.json"
        scd.write_if_changed(f, b"{}\n", 0o644)
        reader = f.open("rb")
        ino = f.stat().st_ino
        assert scd.write_if_changed(f, b'{"a": 1}\n', 0o644) is True
        assert f.stat().st_ino != ino and f.read_bytes() == b'{"a": 1}\n'
        assert reader.read() == b"{}\n"   # a reader that opened the old file
        reader.close()
        assert scd.write_if_changed(f, b'{"a": 1}\n', 0o600) is True
        assert f.stat().st_mode & 0o777 == 0o600

    def test_a_link_a_hardlink_a_fifo_and_a_dir_at_the_name_are_replaced(self, tmp_path):
        import os
        target = tmp_path / "outside"
        target.write_bytes(b"x\n")
        d = tmp_path / ".claude"
        d.mkdir()
        (d / "a").symlink_to(target)
        os.link(target, d / "b")
        os.mkfifo(d / "c")
        (d / "e").mkdir()
        (d / "e" / "inner").write_text("i")
        for name in "abce":
            assert scd.write_if_changed(d / name, b"x\n", 0o644) is True, name
            st = os.lstat(d / name)
            assert stat_is_reg(st) and st.st_nlink == 1, name
        assert target.read_bytes() == b"x\n" and target.stat().st_nlink == 1

    def test_a_truncated_file_from_a_crashed_start_is_rewritten(self, tmp_path):
        f = tmp_path / "stop_tracker.py"
        f.write_bytes(b"#!/usr/bin/env pyth")
        f.chmod(0o755)
        assert scd.write_if_changed(f, b"#!/usr/bin/env python3\n", 0o755) is True
        assert f.read_bytes() == b"#!/usr/bin/env python3\n"

    def test_a_stale_temp_is_swept_and_a_fresh_one_kept(self, tmp_path):
        import os
        import time
        stale = tmp_path / f".settings.json{scd.WRITE_TEMP_MARKER}aaaa"
        fresh = tmp_path / f".settings.json{scd.WRITE_TEMP_MARKER}bbbb"
        stale.write_text("old")
        fresh.write_text("new")
        old = time.time() - scd.STALE_TEMP_S - 5
        os.utime(stale, (old, old))
        scd.write_if_changed(tmp_path / "settings.json", b"{}\n", 0o644)
        assert not stale.exists() and fresh.exists()
        assert not [p for p in tmp_path.iterdir()
                    if scd.WRITE_TEMP_MARKER in p.name and p != fresh]

    def test_the_wrapper_sweep_keeps_a_concurrent_starts_temp(self, tmp_path):
        cfg = tmp_path / ".claude"
        (cfg / "bin").mkdir(parents=True)
        inflight = cfg / "bin" / f".gh{scd.WRITE_TEMP_MARKER}cccc"
        inflight.write_text("in flight")
        scd.write_command_wrappers(cfg)
        assert inflight.exists()

    def test_two_concurrent_writers_of_one_dir_both_succeed(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor
        cfg = tmp_path / ".claude"
        cfg.mkdir()
        (cfg / "permission_gate.py").write_text("an older gate\n")
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda _: scd.install_hook_scripts(cfg), range(16)))
        names = sorted(p.name for p in cfg.iterdir())
        assert not [n for n in names if scd.WRITE_TEMP_MARKER in n]
        gate = (scd._HOOKS_DIR / "permission_gate.py").read_bytes().replace(b"\r\n", b"\n")
        assert (cfg / "permission_gate.py").read_bytes() == gate

    def test_a_temp_swapped_during_the_write_is_caught_and_written_again(self, tmp_path, monkeypatch):
        import os
        f = tmp_path / "permission_gate.py"
        real_replace = os.replace
        calls = []

        def swapping_replace(src, dst):
            calls.append(src)
            if len(calls) == 1:
                # A sibling sandbox renames its own file onto the temp name.
                planted = tmp_path / "planted"
                planted.write_bytes(b"evil\n")
                real_replace(planted, src)
            return real_replace(src, dst)

        monkeypatch.setattr(scd.os, "replace", swapping_replace)
        assert scd.write_if_changed(f, b"gate\n", 0o755) is True
        assert len(calls) == 2 and f.read_bytes() == b"gate\n"

    def test_a_write_tampered_every_time_fails_the_start(self, tmp_path, monkeypatch):
        import os
        f = tmp_path / "permission_gate.py"
        real_replace = os.replace

        def swapping_replace(src, dst):
            planted = tmp_path / "planted"
            planted.write_bytes(b"evil\n")
            real_replace(planted, src)
            return real_replace(src, dst)

        monkeypatch.setattr(scd.os, "replace", swapping_replace)
        with pytest.raises(RuntimeError, match="possible tampering"):
            scd.write_if_changed(f, b"gate\n", 0o755)
        assert not f.exists()


def stat_is_reg(st) -> bool:
    import stat
    return stat.S_ISREG(st.st_mode)


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
    # Codex 0.157 auto-starts a shared background server (its own Codex
    # install, outside the pin) for eligible TUIs; the trust flag already keeps
    # both TUIs out of it, and both opt out explicitly, which also drops the
    # line it printed at start (the flag exists on the previous pin 0.156.1 too).
    assert '"--no-daemon"' in tui and '"--no-daemon"' in sat_tui
    # Every app-server thread trusts its hook, an attended chat's included
    # (its gate stands aside while the bridge asks).
    from core.layers.codex.session import CodexAppServerSession
    for floor in (True, False):
        session = CodexAppServerSession(
            session_id="11111111-2222-4333-8444-555555555555", agent_name="pa",
            model="gpt-6", sandbox_mode="danger-full-access", working_dir="",
            config_dir="/tmp/x", hooks_floor=floor,
        )
        assert session._thread_overrides()["config"] == {"bypass_hook_trust": True}
    sat = (root.parent / "satellite" / "sessions" / "codex_session.py").read_text()
    assert "if self._hooks_on:" in sat and '{"bypass_hook_trust": True}' in sat
