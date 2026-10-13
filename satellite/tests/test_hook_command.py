"""hook_command / codex_hook_command — the hook command strings.

Claude's Node runner shell-splits quote-aware on every OS → the quoted
two-token form works everywhere, with spaces (``C:\\Users\\First Last``)
and non-ASCII home dirs (``C:\\Users\\Δημήτρης``) preserved.

Codex's Rust runner executes via ``cmd.exe /C`` on Windows, which cannot
re-parse Rust's escaping of an embedded-quote string — the hook exits 1
before Python starts (the interactive-Codex "PreToolUse hook (failed)"
spam). codex_hook_command therefore emits a QUOTE-FREE .cmd wrapper path
on Windows and the Claude form elsewhere.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

from satellite import config as sat_config
from satellite.config import codex_hook_command, hook_command


def test_both_parts_quoted():
    cmd = hook_command(Path("/tmp/hooks/permission_gate.py"))
    assert cmd == f'"{sys.executable}" "/tmp/hooks/permission_gate.py"'


def test_path_with_spaces_splits_to_two_argv_items():
    cmd = hook_command(Path("/home/first last/.codex/permission_gate.py"))
    argv = shlex.split(cmd)
    assert argv == [sys.executable, "/home/first last/.codex/permission_gate.py"]


def test_non_ascii_path_preserved():
    p = Path("/home/Δημήτρης/.codex/permission_gate.py")
    cmd = hook_command(p)
    argv = shlex.split(cmd)
    assert argv[1] == str(p)


def test_codex_hook_command_posix_matches_claude_form(tmp_path):
    p = tmp_path / "permission_gate.py"
    assert codex_hook_command(p) == hook_command(p)


def test_codex_hook_command_windows_emits_quote_free_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(sat_config, "HOST", sat_config.ROWS[sat_config.WINDOWS])
    hook = tmp_path / "Δημήτρης dir" / "permission_gate.py"
    hook.parent.mkdir()
    cmd = codex_hook_command(hook)
    # The command string itself must carry NO quotes — anything quoted dies
    # in Rust-escaping → cmd.exe /C re-parsing.
    assert '"' not in cmd
    assert cmd.endswith("permission_gate.cmd")
    wrapper = Path(cmd)
    assert wrapper.exists()
    content = wrapper.read_bytes()
    # Pure ASCII: cmd reads batch files in the OEM codepage, so the
    # non-ASCII directory name must never appear in the batch text.
    content.decode("ascii")
    text = content.decode("ascii")
    assert "%~dp0permission_gate.py" in text
    assert "%OTO_HOOK_PY%" in text
    assert "exit /b %errorlevel%" in text
    assert "\r\n" in text  # batch files want CRLF


def test_write_command_wrappers_creates_the_dir_and_keeps_bytes(tmp_path):
    """The payload's wrappers land under the config dir's bin/ as sent (the
    Windows one CRLF), executable on POSIX; a name that escapes the dir, an
    empty name and an empty body are dropped."""
    sat_config.write_command_wrappers(tmp_path, {
        "bin/gh": "#!/bin/sh\nexec x \"$@\"\n",
        "bin/gh.cmd": "@echo off\r\nx %*\r\n",
        "../evil": "no", "/abs/evil": "no", "": "no", "bin/empty": "", 7: "no",
    })
    assert (tmp_path / "bin" / "gh").read_bytes() == b"#!/bin/sh\nexec x \"$@\"\n"
    assert (tmp_path / "bin" / "gh.cmd").read_bytes() == b"@echo off\r\nx %*\r\n"
    if sat_config.HOST.posix:
        assert (tmp_path / "bin" / "gh").stat().st_mode & 0o111
    assert not (tmp_path / "bin" / "empty").exists() and not (tmp_path.parent / "evil").exists()
    assert not Path("/abs/evil").exists()
    sat_config.write_command_wrappers(tmp_path, {})   # nothing: a no-op


def test_write_command_wrappers_leaves_exactly_the_wrappers(tmp_path):
    """The dir holds the payload's wrappers and nothing else afterwards (the
    twin of the proxy's rule for a sandbox): a file, a link and a
    subdirectory left there are removed, a directory at a wrapper's name
    replaced, a link never followed. A name without a directory is dropped,
    so the config dir itself is never cleared."""
    (tmp_path / "bin" / "sub").mkdir(parents=True)
    (tmp_path / "bin" / "x").write_text("planted")
    (tmp_path / "bin" / "sub" / "y").write_text("y")
    (tmp_path / "bin" / "gh").mkdir()
    (tmp_path / "keep.txt").write_text("k")
    if sat_config.HOST.posix:
        (tmp_path / "bin" / "lnk").symlink_to(tmp_path / "keep.txt")
    sat_config.write_command_wrappers(tmp_path, {
        "bin/gh": "#!/bin/sh\nx\n", "bin/gh.cmd": "@x\r\n", "gh": "no",
    })
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["gh", "gh.cmd"]
    assert (tmp_path / "bin" / "gh").read_bytes() == b"#!/bin/sh\nx\n"
    assert (tmp_path / "keep.txt").read_text() == "k" and not (tmp_path / "gh").exists()


def test_write_if_changed_leaves_an_unchanged_file_and_replaces_a_changed_one(tmp_path):
    """The hook files are written at every session start while a sibling
    session may launch a hook from them: an unchanged file keeps its inode
    and mtime, a changed one is a new file written whole, and an old reader
    keeps the bytes it opened."""
    f = tmp_path / "permission_gate.py"
    assert sat_config.write_if_changed(f, b"gate\n", 0o755) is True
    before = f.stat()
    assert sat_config.write_if_changed(f, b"gate\n", 0o755) is False
    assert (f.stat().st_ino, f.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    reader = f.open("rb")
    assert sat_config.write_if_changed(f, b"gate 2\n", 0o755) is True
    assert f.read_bytes() == b"gate 2\n" and reader.read() == b"gate\n"
    reader.close()
    assert not [p for p in tmp_path.iterdir() if sat_config.WRITE_TEMP_MARKER in p.name]


def test_write_if_changed_replaces_a_link_and_a_dir_and_keeps_lf(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("keep")
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.json").symlink_to(outside)
    (d / "hooks.json").mkdir()
    for name in ("settings.json", "hooks.json"):
        assert sat_config.write_if_changed(d / name, b'{"a": 1}\n') is True
        assert not (d / name).is_symlink() and (d / name).read_bytes() == b'{"a": 1}\n'
    assert outside.read_text() == "keep"


def test_write_if_changed_on_windows_retries_a_locked_name_then_writes_in_place(tmp_path, monkeypatch):
    """Windows refuses a rename onto a file another process holds open (a
    hook running): three tries, then the bytes are written in place, as
    before; no O_NOFOLLOW there and no mode bits to judge."""
    monkeypatch.setattr(sat_config, "HOST", sat_config.ROWS[sat_config.WINDOWS])
    monkeypatch.delattr(sat_config.os, "O_NOFOLLOW", raising=False)
    tries = []

    def locked(src, dst):
        tries.append(src)
        raise PermissionError(13, "in use")

    monkeypatch.setattr(sat_config.os, "replace", locked)
    monkeypatch.setattr(sat_config.time, "sleep", lambda s: None)
    f = tmp_path / "permission_gate.py"
    f.write_bytes(b"old\r\n")
    assert sat_config.write_if_changed(f, b"new\n", 0o755) is True
    assert len(tries) == 3 and f.read_bytes() == b"new\n"
    assert not [p for p in tmp_path.iterdir() if sat_config.WRITE_TEMP_MARKER in p.name]
    # An unchanged file is judged by its bytes alone there.
    monkeypatch.setattr(sat_config.os, "replace", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert sat_config.write_if_changed(f, b"new\n", 0o600) is False


def test_the_wrapper_sweep_keeps_a_concurrent_starts_temp(tmp_path):
    (tmp_path / "bin").mkdir()
    inflight = tmp_path / "bin" / f".gh{sat_config.WRITE_TEMP_MARKER}abcd"
    inflight.write_text("in flight")
    sat_config.write_command_wrappers(tmp_path, {"bin/gh": "#!/bin/sh\nx\n"})
    assert inflight.exists()


def test_the_hook_writers_leave_unchanged_files_alone(tmp_path):
    from satellite.sessions import cli_session, codex_session
    claude, codex = tmp_path / ".claude", tmp_path / ".codex"
    claude.mkdir()
    codex.mkdir()
    scripts = {"permission_gate.py": "# gate\n", "stop_tracker.py": "# stop\n"}
    cli_session._write_hook_scripts(claude, scripts)
    cli_session._write_cli_hooks(claude, ["CronCreate"])
    codex_session._write_codex_hook_scripts(codex, scripts)
    codex_session._write_codex_hooks(codex)
    files = [claude / "permission_gate.py", claude / "settings.json",
             codex / "stop_tracker.py", codex / "hooks.json"]
    before = {f: (f.stat().st_ino, f.stat().st_mtime_ns) for f in files}
    cli_session._write_hook_scripts(claude, scripts)
    cli_session._write_cli_hooks(claude, ["CronCreate"])
    codex_session._write_codex_hook_scripts(codex, scripts)
    codex_session._write_codex_hooks(codex)
    assert {f: (f.stat().st_ino, f.stat().st_mtime_ns) for f in files} == before
    cli_session._write_hook_scripts(claude, {"permission_gate.py": "# gate 2\n"})
    assert (claude / "permission_gate.py").read_text() == "# gate 2\n"


def test_session_tools_env_names_the_dir_the_interpreter_and_the_path(tmp_path):
    import os
    env = sat_config.session_tools_env({"PATH": "/usr/bin", "OTO_HOOKS_DIR": "/users/a/.claude",
                                        "OTO_HOOK_PY": "python3"}, tmp_path / ".codex")
    assert env["OTO_HOOKS_DIR"] == str(tmp_path / ".codex")
    assert env["OTO_HOOK_PY"] == sys.executable
    assert env["PATH"] == str(tmp_path / ".codex" / "bin") + os.pathsep + "/usr/bin"
    assert sat_config.session_tools_env({}, tmp_path)["PATH"].startswith(str(tmp_path / "bin"))
