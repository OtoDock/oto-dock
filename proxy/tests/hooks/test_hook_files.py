"""The hook files the platform writes for both CLIs (HOOKS.md "The hook
files" and "Trust"): the same four events and four scripts per scope, the
Codex object schema, the Stop hook's long timeout, and the stale-trust
assertions — nothing the platform writes can make a Codex hook silently
untrusted, and the TUI and app-server spawns carry their bypass."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

import config as app_config
from core.layers.cli import config_dir as cli_cd
from core.layers.codex import config_dir as codex_cd
from core.sandbox import session_config_dir as scd

EVENTS = {"PreToolUse", "PostToolUse", "SubagentStop", "Stop"}
#: Claude's settings also name the terminal's start (the start's riding part)
#: and a failed call's event, which Claude Code runs instead of PostToolUse.
CLAUDE_EVENTS = EVENTS | {"SessionStart", "PostToolUseFailure"}


def _agents(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents, raising=False)
    monkeypatch.setattr(app_config, "get_agent_dir", lambda name: agents / name, raising=False)
    return agents


def test_claude_settings_name_the_four_events_and_the_long_stop_timeout():
    s = cli_cd.build_settings("/users/alice/.claude")
    assert set(s["hooks"]) == CLAUDE_EVENTS
    py = scd.sandbox_python()
    assert py.startswith("/")  # the resolved path, never a name PATH could shadow
    for ev in CLAUDE_EVENTS:
        h = s["hooks"][ev][0]["hooks"][0]
        assert h["type"] == "command"
        # isolated: <python> -I "<sandbox .claude>/<hook>.py"
        assert h["command"].startswith(f'{py} -I "/users/alice/.claude/')
    # One start entry per part, each naming its part (Claude Code inlines a
    # hook's context only up to 10,000 characters).
    from core.session.start_context import HOOK_PARTS
    starts = [h["command"] for h in s["hooks"]["SessionStart"][0]["hooks"]]
    assert starts == [f'{py} -I "/users/alice/.claude/start_context.py" {i} {HOOK_PARTS}'
                      for i in range(1, HOOK_PARTS + 1)]
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"].endswith('stop_tracker.py"')
    assert s["hooks"]["Stop"][0]["hooks"][0]["timeout"] == scd.STOP_HOOK_TIMEOUT_S
    # An hour past the prompt's own wait, so the proxy always answers first.
    from core.session.session_state import PROMPT_WAIT_S
    assert scd.LONG_HOOK_TIMEOUT_S == PROMPT_WAIT_S + 3600
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == scd.LONG_HOOK_TIMEOUT_S
    assert s["hooks"]["SubagentStop"][0]["hooks"][0]["command"].endswith('subagent_tracker.py"')


def test_a_failed_call_runs_the_forwarder_too():
    """Claude Code runs PostToolUse for a call that succeeded and
    PostToolUseFailure for one that failed (a non-zero exit, a stopped
    command, a missing file, an MCP error): both name the forwarder, the
    card's one source on a headless chat, with the same timeout and no
    block on a forwarder failure."""
    s = cli_cd.build_settings("/users/alice/.claude")
    assert cli_cd.TOOL_RESULT_EVENTS == ("PostToolUse", "PostToolUseFailure")
    for ev in cli_cd.TOOL_RESULT_EVENTS:
        (group,) = s["hooks"][ev]
        (hook,) = group["hooks"]
        assert group["matcher"] == ""
        assert hook["command"].endswith('/users/alice/.claude/tool_result_forwarder.py"')
        assert hook["timeout"] == 10
        assert "onFailure" not in hook


def test_the_satellite_names_the_same_result_events():
    """A paired computer's satellite writes its own settings
    (``cli_session._write_cli_hooks``): its list of the result events is the
    proxy's, read from the file as a literal."""
    import ast

    from tests._paths import REPO_ROOT
    tree = ast.parse((REPO_ROOT / "satellite" / "sessions" / "cli_session.py").read_text(encoding="utf-8"))
    twin = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOL_RESULT_EVENTS" for t in node.targets):
            twin = ast.literal_eval(node.value)
    assert twin == cli_cd.TOOL_RESULT_EVENTS


def test_codex_hooks_json_is_an_object_with_the_same_four_events():
    h = codex_cd.build_hooks("/workspace/.codex")
    assert isinstance(h, dict) and set(h) == {"hooks"}
    # The four events, and Codex's own SessionStart (a resumed thread's text).
    assert set(h["hooks"]) == EVENTS | {"SessionStart"}
    for ev in (*EVENTS, "SessionStart"):
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
    assert set(settings["hooks"]) == CLAUDE_EVENTS
    # Codex's hooks file is the start's, in the folder the engine reads
    # (``write_hooks``: a session's home when it has one), never the funnel's.
    assert not (codex / "hooks.json").exists()
    codex_cd.write_hooks(codex, "/x/.codex")
    hooks = json.loads((codex / "hooks.json").read_text())
    assert set(hooks["hooks"]) == EVENTS | {"SessionStart"}
    assert '"/x/.codex/permission_gate.py' in (codex / "hooks.json").read_text()
    assert set(scd.HOOK_SCRIPTS) == {
        "permission_gate.py", "tool_result_forwarder.py",
        "subagent_tracker.py", "stop_tracker.py", "credential_helper.py",
        "session_text.py", "usage_report.py", "start_context.py",
    }
    # The status line names the usage reporter, in the file both kinds read.
    assert settings["statusLine"]["command"].endswith('/usage_report.py"')


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
        if d.name.endswith("codex"):
            codex_cd.write_hooks(d, "/x")
        first = bound(d)
        assert scd.HOOK_FILE_OF_STATE_DIR[d.name] in first
        if d.name.endswith("codex"):
            codex_cd.write_hooks(d, "/x")
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
        # The link's own target holds the same bytes and mode, with one link:
        # only the link at the name tells it from a file already right.
        link_target = tmp_path / "outside-for-the-link"
        link_target.write_bytes(b"x\n")
        link_target.chmod(0o644)
        d = tmp_path / ".claude"
        d.mkdir()
        (d / "a").symlink_to(link_target)
        os.link(target, d / "b")
        os.mkfifo(d / "c")
        (d / "e").mkdir()
        (d / "e" / "inner").write_text("i")
        for name in "abce":
            assert scd.write_if_changed(d / name, b"x\n", 0o644) is True, name
            st = os.lstat(d / name)
            assert stat_is_reg(st) and st.st_nlink == 1, name
        assert target.read_bytes() == b"x\n" and target.stat().st_nlink == 1
        assert link_target.read_bytes() == b"x\n" and link_target.stat().st_nlink == 1

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

    def test_many_starts_writing_one_folder_at_once_all_pass(self, tmp_path, monkeypatch):
        """Sibling starts of one scope write the same file at once: none of
        them fails, and the file holds the right bytes. A short pause where a
        writer looks at the file lets the writers interleave wherever they
        can; one writer's honest replace must never read to another as
        tampering."""
        import os
        import time
        from concurrent.futures import ThreadPoolExecutor
        real_fstat = os.fstat

        class _SlowFstat:
            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def fstat(fd):
                time.sleep(0.0002)
                return real_fstat(fd)

        monkeypatch.setattr(scd, "os", _SlowFstat())
        data = b"gate\n" * 50
        f = tmp_path / "permission_gate.py"
        for _ in range(10):
            f.write_text("an older gate\n")
            with ThreadPoolExecutor(8) as ex:
                written = list(ex.map(lambda _: scd.write_if_changed(f, data, 0o755), range(16)))
            assert written.count(True) >= 1
            assert f.read_bytes() == data and f.stat().st_mode & 0o777 == 0o755

    def test_a_temp_swapped_during_the_write_is_caught_and_written_again(self, tmp_path, monkeypatch):
        import os
        f = tmp_path / "permission_gate.py"
        real_replace = os.replace
        calls = []

        def swapping_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            calls.append(src)
            if len(calls) == 1:
                # A sibling sandbox renames its own file onto the temp name.
                planted = tmp_path / "planted"
                planted.write_bytes(b"evil\n")
                real_replace(planted, src, dst_dir_fd=src_dir_fd)
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        monkeypatch.setattr(scd.os, "replace", swapping_replace)
        assert scd.write_if_changed(f, b"gate\n", 0o755) is True
        assert len(calls) == 2 and f.read_bytes() == b"gate\n"

    def test_a_write_tampered_every_time_fails_the_start(self, tmp_path, monkeypatch):
        import os
        f = tmp_path / "permission_gate.py"
        real_replace = os.replace

        def swapping_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            planted = tmp_path / "planted"
            planted.write_bytes(b"evil\n")
            real_replace(planted, src, dst_dir_fd=src_dir_fd)
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

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
        root / "core" / "layers" / "codex" / "spawn.py",
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
    tui = (root / "core" / "layers" / "codex" / "spawn.py").read_text()
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
    sat = (root.parent / "satellite" / "sessions" / "codex_session.py").read_text()
    assert "if self._hooks_on:" in sat and 'thread_config["bypass_hook_trust"] = True' in sat


def _materializer_off(monkeypatch):
    monkeypatch.setattr(
        "core.sandbox.skills_materializer.materialize_skills_for_sandbox",
        lambda *a, **k: None,
    )


def test_the_cli_memory_folders_go_and_no_link_leads_the_removal_out(tmp_path, monkeypatch):
    """The start removes Claude Code's own memory folders; a link in the
    projects folder, or at a memory name, removes nothing beyond itself."""
    agents = _agents(tmp_path, monkeypatch)
    _materializer_off(monkeypatch)
    projects = agents / "pa" / "users" / "alice" / ".claude" / "projects"
    (projects / "real" / "memory").mkdir(parents=True)
    (projects / "real" / "memory" / "MEMORY.md").write_text("cli memory")
    (projects / "real" / "s1.jsonl").write_text("{}\n")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "p" / "memory").mkdir(parents=True)
    (elsewhere / "p" / "memory" / "keep.md").write_text("keep")
    (elsewhere / "m").mkdir()
    (elsewhere / "m" / "keep.md").write_text("keep")
    (projects / "linked").symlink_to(elsewhere / "p")
    (projects / "other").mkdir()
    (projects / "other" / "memory").symlink_to(elsewhere / "m")

    cli_cd.ensure_persistent_claude_dir("pa", username="alice", scope="user")

    assert not (projects / "real" / "memory").exists()
    assert (projects / "real" / "s1.jsonl").is_file()
    assert (elsewhere / "p" / "memory" / "keep.md").read_text() == "keep"
    assert (elsewhere / "m" / "keep.md").read_text() == "keep"
    assert not (projects / "other" / "memory").is_symlink()


def test_a_linked_projects_folder_is_not_entered(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    _materializer_off(monkeypatch)
    claude = agents / "pa" / "users" / "alice" / ".claude"
    claude.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "p" / "memory").mkdir(parents=True)
    (elsewhere / "p" / "memory" / "keep.md").write_text("keep")
    (claude / "projects").symlink_to(elsewhere)

    cli_cd.ensure_persistent_claude_dir("pa", username="alice", scope="user")

    assert (elsewhere / "p" / "memory" / "keep.md").read_text() == "keep"


def test_the_folder_mode_is_set_on_the_folder_never_through_a_link(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    _materializer_off(monkeypatch)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    link = agents / "pa" / "users" / "alice" / ".claude"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    monkeypatch.setattr(scd, "scope_config_dir", lambda *a, **k: (link, "/users/alice/.claude"))
    monkeypatch.setattr(scd, "install_hook_scripts", lambda d: None)
    monkeypatch.setattr(scd, "write_if_changed", lambda *a, **k: False)

    with pytest.raises(OSError):
        cli_cd.ensure_persistent_claude_dir("pa", username="alice", scope="user")
    assert target.stat().st_mode & 0o777 == 0o755



def test_an_agent_whose_folder_is_a_link_gets_its_memory_wipe_and_mode(tmp_path, monkeypatch):
    """The operator may move an agent's folder and leave a link in its place:
    the start clears Claude Code's memory and sets the folder's mode beneath
    the real folder, and a link below it still removes nothing beyond
    itself."""
    agents = _agents(tmp_path, monkeypatch)
    _materializer_off(monkeypatch)
    real = tmp_path / "disk2" / "pa"
    projects = real / "users" / "alice" / ".claude" / "projects"
    (projects / "real" / "memory").mkdir(parents=True)
    (projects / "real" / "memory" / "MEMORY.md").write_text("cli memory")
    (projects / "real" / "s1.jsonl").write_text("{}\n")
    (real / "users" / "alice" / ".claude").chmod(0o755)
    (agents / "pa").symlink_to(real)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "m").mkdir(parents=True)
    (elsewhere / "m" / "keep.md").write_text("keep")
    (projects / "other").mkdir()
    (projects / "other" / "memory").symlink_to(elsewhere / "m")

    cli_cd.ensure_persistent_claude_dir("pa", username="alice", scope="user")

    assert not (projects / "real" / "memory").exists()
    assert (projects / "real" / "s1.jsonl").is_file()
    assert (real / "users" / "alice" / ".claude").stat().st_mode & 0o777 == 0o700
    assert not (projects / "other" / "memory").is_symlink()
    assert (elsewhere / "m" / "keep.md").read_text() == "keep"

# ---------------------------------------------------------------------------
# A session's config folder is written beneath the agent's real folder: no
# link at any part below it is followed (stand-ins inside the test's own
# agents root).
# ---------------------------------------------------------------------------


def _store(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    store = agents / "pa" / "workspace" / ".claude"
    store.mkdir(parents=True)
    return agents, store


def test_the_in_place_writer_keeps_the_files_inode_beneath_the_agent(tmp_path, monkeypatch):
    """The store's sign-in is a running home session's mountpoint: the
    write reaches it in place, so the mount over it holds."""
    _agents_dir, store = _store(tmp_path, monkeypatch)
    sign_in = store / ".credentials.json"
    sign_in.write_bytes(b"old account")
    inode = sign_in.stat().st_ino
    scd.write_no_follow(sign_in, b"new", mode=0o600)
    assert sign_in.read_bytes() == b"new" and sign_in.stat().st_ino == inode
    assert sign_in.stat().st_mode & 0o777 == 0o600


def test_the_in_place_writer_refuses_a_folder_link_above_the_name(tmp_path, monkeypatch):
    agents, _store_dir = _store(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".claude").mkdir(parents=True)
    (agents / "pa" / "users").mkdir()
    (agents / "pa" / "users" / "alice").symlink_to(elsewhere)
    with pytest.raises(OSError):
        scd.write_no_follow(agents / "pa" / "users" / "alice" / ".claude" / ".credentials.json", b"x", mode=0o600)
    assert list((elsewhere / ".claude").iterdir()) == []


_HOME_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def _second_name(agents, folder):
    """A sign-in file that has a second name beside it on the disk, as an
    operator's copy made with hard links leaves it."""
    import os
    folder.mkdir(parents=True, exist_ok=True)
    other = agents / "pa" / "workspace" / "stand-in.txt"
    other.write_bytes(b"stand-in")
    os.link(other, folder / ".credentials.json")
    return other


def test_a_sign_in_with_a_second_name_and_no_home_gets_a_file_of_its_own(tmp_path, monkeypatch):
    """The file is never written into: the name gets a file of its own, and
    the second name keeps the old bytes and no token."""
    agents, store = _store(tmp_path, monkeypatch)
    other = _second_name(agents, store)
    scd.write_no_follow(store / ".credentials.json", b"new", mode=0o600)
    assert other.read_bytes() == b"stand-in" and other.stat().st_nlink == 1
    assert (store / ".credentials.json").read_bytes() == b"new"
    assert (store / ".credentials.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("where", ["workspace/.claude", "workspace/.codex", "users/alice/.claude"])
def test_a_sign_in_with_a_second_name_is_held_back_while_a_home_exists(tmp_path, monkeypatch, where):
    """A home anywhere in the agent's folder may hold a mask over the name,
    which a new file there would drop: the write is held back, both names
    stay, and the person reads why on the failed start's card."""
    agents = _agents(tmp_path, monkeypatch)
    store = agents / "pa" / "workspace" / ".codex"
    other = _second_name(agents, store)
    (agents / "pa" / where / "own" / _HOME_ID).mkdir(parents=True)
    with pytest.raises(scd.SignInHasAnotherName) as refused:
        scd.write_no_follow(store / ".credentials.json", b"new", mode=0o600)
    assert str(refused.value) == (
        "This chat's sign-in file has a second name on the server's disk, so it was not written. "
        "It is written once the agent's other chats have ended, or when an admin removes the second name.")
    assert isinstance(refused.value, OSError)
    assert other.read_bytes() == b"stand-in" and other.stat().st_nlink == 2


def test_a_second_name_made_again_before_the_new_file_is_never_written_into(tmp_path, monkeypatch):
    """The name's own file is made exclusively: a file of two names put back
    between the removal and the make ends the write, and neither name gets
    the token."""
    import os

    from services.infra import safe_fs
    agents, store = _store(tmp_path, monkeypatch)
    other = _second_name(agents, store)
    real_unlink = safe_fs.unlink_beneath

    def unlink_then_link_again(*a, **kw):
        real_unlink(*a, **kw)
        os.link(other, store / ".credentials.json")

    monkeypatch.setattr(safe_fs, "unlink_beneath", unlink_then_link_again)
    with pytest.raises(FileExistsError):
        scd.write_no_follow(store / ".credentials.json", b"new", mode=0o600)
    assert other.read_bytes() == b"stand-in"


def test_a_homes_own_sign_in_with_a_second_name_gets_a_file_of_its_own(tmp_path, monkeypatch):
    """No session masks a home's own file: it gets a file of its own even
    while that home exists."""
    agents = _agents(tmp_path, monkeypatch)
    home = agents / "pa" / "workspace" / ".claude" / "own" / _HOME_ID
    other = _second_name(agents, home)
    scd.write_no_follow(home / ".credentials.json", b"new", mode=0o600)
    assert other.read_bytes() == b"stand-in" and other.stat().st_nlink == 1
    assert (home / ".credentials.json").read_bytes() == b"new"


def test_the_in_place_writer_refuses_a_pipe_at_the_name_at_once(tmp_path, monkeypatch):
    import os
    import threading
    _agents_dir, store = _store(tmp_path, monkeypatch)
    os.mkfifo(store / ".credentials.json")
    raised = []

    def write():
        try:
            scd.write_no_follow(store / ".credentials.json", b"x", mode=0o600)
        except OSError as exc:
            raised.append(exc)

    worker = threading.Thread(target=write, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and raised


def test_the_in_place_writer_replaces_a_link_at_the_name(tmp_path, monkeypatch):
    _agents_dir, store = _store(tmp_path, monkeypatch)
    target = tmp_path / "stand-in"
    target.write_bytes(b"stand-in")
    (store / ".credentials.json").symlink_to(target)
    scd.write_no_follow(store / ".credentials.json", b"x", mode=0o600)
    assert not (store / ".credentials.json").is_symlink() and target.read_bytes() == b"stand-in"


def test_the_wrappers_never_list_or_remove_through_a_folder_link_above(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".claude" / "bin").mkdir(parents=True)
    (elsewhere / ".claude" / "bin" / "keep").write_text("k")
    (agents / "pa" / "users").mkdir(parents=True)
    (agents / "pa" / "users" / "alice").symlink_to(elsewhere)
    with pytest.raises(OSError):
        scd.write_command_wrappers(agents / "pa" / "users" / "alice" / ".claude")
    assert sorted(p.name for p in (elsewhere / ".claude" / "bin").iterdir()) == ["keep"]


def test_the_wrappers_beneath_the_agent_are_written_and_a_link_at_bin_replaced(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    cfg = agents / "pa" / "workspace" / ".claude"
    cfg.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("k")
    (cfg / "bin").symlink_to(elsewhere)
    scd.install_hook_scripts(cfg)
    assert (cfg / "bin").is_dir() and not (cfg / "bin").is_symlink()
    assert sorted(p.name for p in (cfg / "bin").iterdir()) == sorted(Path(r).name for r in scd.COMMAND_WRAPPERS)
    assert sorted(p.name for p in elsewhere.iterdir()) == ["keep"]


def test_the_wrappers_beneath_the_agent_hold_exactly_the_wrappers(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    cfg = agents / "pa" / "workspace" / ".claude"
    (cfg / "bin" / "sub").mkdir(parents=True)
    (cfg / "bin" / "x").write_text("#!/bin/sh\necho planted\n")
    (cfg / "bin" / "sub" / "y").write_text("y")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("k")
    (cfg / "bin" / "lnk").symlink_to(elsewhere)
    (cfg / "bin" / "gh").mkdir()
    scd.install_hook_scripts(cfg)
    assert sorted(p.name for p in (cfg / "bin").iterdir()) == sorted(Path(r).name for r in scd.COMMAND_WRAPPERS)
    assert (elsewhere / "keep").read_text() == "k"


def test_the_wrappers_are_removed_through_the_folder_the_listing_opened(tmp_path, monkeypatch):
    """``bin`` made a link after its listing: the entries go from the folder
    that was listed, and the folder the link names loses nothing."""
    agents = _agents(tmp_path, monkeypatch)
    cfg = agents / "pa" / "workspace" / ".claude"
    (cfg / "bin").mkdir(parents=True)
    (cfg / "bin" / "x").write_text("planted")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x").write_text("stand-in")
    real_keeps = scd._keeps
    swapped = []

    def keeps(entry):
        if not swapped:
            (cfg / "bin").rename(cfg / "bin-listed")
            (cfg / "bin").symlink_to(elsewhere)
            swapped.append(True)
        return real_keeps(entry)

    monkeypatch.setattr(scd, "_keeps", keeps)
    with contextlib.suppress(OSError):
        scd.write_command_wrappers(cfg)
    assert swapped and (elsewhere / "x").read_text() == "stand-in"
    assert not (cfg / "bin-listed" / "x").exists()


def test_a_file_at_bin_through_a_folder_link_above_is_kept(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".claude").mkdir(parents=True)
    (elsewhere / ".claude" / "bin").write_text("stand-in")
    (agents / "pa" / "users").mkdir(parents=True)
    (agents / "pa" / "users" / "alice").symlink_to(elsewhere)
    with pytest.raises(OSError):
        scd.write_command_wrappers(agents / "pa" / "users" / "alice" / ".claude")
    assert (elsewhere / ".claude" / "bin").read_text() == "stand-in"


def test_the_in_place_writer_finds_an_agent_folder_that_is_itself_a_link(tmp_path, monkeypatch):
    """The operator's layout: the agent's folder is a link to its real
    place, and the session's config folder is named by that real place. The
    in-place rule still holds there: a folder link below it is refused."""
    agents = _agents(tmp_path, monkeypatch)
    real = tmp_path / "disk2" / "pa"
    (real / "users").mkdir(parents=True)
    (agents / "pa").symlink_to(real)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".claude").mkdir(parents=True)
    (real / "users" / "alice").symlink_to(elsewhere)
    with pytest.raises(OSError):
        scd.write_no_follow(real / "users" / "alice" / ".claude" / ".credentials.json", b"x", mode=0o600)
    assert list((elsewhere / ".claude").iterdir()) == []
    (real / "workspace" / ".claude").mkdir(parents=True)
    scd.write_no_follow(real / "workspace" / ".claude" / ".credentials.json", b"y", mode=0o600)
    assert (real / "workspace" / ".claude" / ".credentials.json").read_bytes() == b"y"


# The writer of a session's config folder opens the folder at each write
# beneath the agent's real folder, with no link at any part: the folder
# checked at the start is the folder written, whatever replaced it since.


@pytest.mark.parametrize("swapped", [".claude", "users"])
def test_a_folder_swapped_for_a_link_after_the_check_gets_nothing(tmp_path, monkeypatch, swapped):
    agents = _agents(tmp_path, monkeypatch)
    scope = agents / "pa" / "users" / "alice" / ".claude"
    scope.mkdir(parents=True)
    stand_in = tmp_path / "stand-in"
    (stand_in / "alice" / ".claude").mkdir(parents=True)
    moved = scope if swapped == ".claude" else agents / "pa" / "users"
    moved.rename(tmp_path / "aside")
    moved.symlink_to(stand_in / "alice" / ".claude" if swapped == ".claude" else stand_in)
    with pytest.raises(OSError):
        scd.write_if_changed(scope / "settings.json", b"{}\n")
    assert not (stand_in / "alice" / ".claude" / "settings.json").exists()


def test_two_names_of_one_folder_share_its_lock(tmp_path, monkeypatch):
    """An agent behind the operator's link: its folder as named and at its
    real place are one folder, so its writes take one lock."""
    agents = _agents(tmp_path, monkeypatch)
    real = tmp_path / "disk2" / "pa"
    (real / "workspace" / ".claude").mkdir(parents=True)
    (agents / "pa").symlink_to(real)
    taken: list[int] = []

    class _Recording:
        def __init__(self, i):
            self.i = i

        def __enter__(self):
            taken.append(self.i)

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(scd, "_FOLDER_LOCKS", tuple(_Recording(i) for i in range(64)))
    scd.write_if_changed(agents / "pa" / "workspace" / ".claude" / "a.json", b"1")
    scd.write_if_changed(real / "workspace" / ".claude" / "b.json", b"2")
    assert len(taken) == 2 and taken[0] == taken[1]
    assert (real / "workspace" / ".claude" / "a.json").read_bytes() == b"1"


def test_the_codex_folder_mode_is_set_on_the_folder_never_through_a_link(tmp_path, monkeypatch):
    agents = _agents(tmp_path, monkeypatch)
    _materializer_off(monkeypatch)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    link = agents / "pa" / "users" / "alice" / ".codex"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    monkeypatch.setattr(scd, "scope_config_dir", lambda *a, **k: (link, "/users/alice/.codex"))
    monkeypatch.setattr(scd, "install_hook_scripts", lambda d: None)
    monkeypatch.setattr(scd, "write_if_changed", lambda *a, **k: False)
    with pytest.raises(OSError):
        codex_cd.ensure_persistent_codex_dir("pa", username="alice", scope="user")
    assert target.stat().st_mode & 0o777 == 0o755
