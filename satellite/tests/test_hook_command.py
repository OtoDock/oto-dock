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
    monkeypatch.setattr(sys, "platform", "win32")
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
