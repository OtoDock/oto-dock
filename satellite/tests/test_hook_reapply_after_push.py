"""Standalone harness for the reconnect hook-path clobber fix.

Run directly (no pytest needed):

    cd oto-dock && python3 satellite/tests/test_hook_reapply_after_push.py

Simulates a platform push of a bwrap-VIRTUAL-path hooks file over a LIVE
session's spawn-time REAL-path version and asserts
``SessionManager._reapply_hooks_after_push`` restores real (sys.executable)
paths + refreshes the session snapshot — for Claude (settings.json) and Codex
(hooks.json), while leaving non-hooks pushes + non-live agents untouched.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from satellite.sessions.session_manager import SessionManager
from satellite.transport import file_sync
from satellite.config import hook_command


def _virtual_settings() -> str:
    """What the proxy ships for .claude/settings.json — bwrap-virtual paths."""
    return json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "", "hooks": [{
            "type": "command",
            "command": '"python3" "/users/alice/.claude/permission_gate.py"',
            "timeout": 604800,
        }]}]}
    }, indent=2) + "\n"


def _virtual_codex_hooks() -> str:
    return json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "", "hooks": [{
            "type": "command",
            "command": '"python3" "/users/alice/.codex/permission_gate.py"',
            "timeout": 604800,
        }]}]}
    }, indent=2) + "\n"


def _mgr(tmp: Path):
    m = SessionManager.__new__(SessionManager)
    m.config = SimpleNamespace(agents_dir=tmp)
    m.sessions = {}
    m.pty_sessions = {}
    return m


def _claude_session(agent_dir: Path, *, user="alice"):
    claude_dir = agent_dir / "users" / user / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        agent_slug=agent_dir.name,
        _claude_dir=claude_dir,
        _file_snapshot={},
    )


def _codex_session(agent_dir: Path, *, user="alice"):
    codex_dir = agent_dir / "users" / user / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        agent_slug=agent_dir.name,
        _codex_dir=codex_dir,
        _file_snapshot={},
    )


def test_claude_clobber_reapplied():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent_dir = tmp / "my-agent"
        sess = _claude_session(agent_dir)
        rel = "users/alice/.claude/settings.json"
        (agent_dir / rel).write_text(_virtual_settings())  # the clobber

        m = _mgr(tmp)
        m.pty_sessions["s1"] = sess  # live interactive session
        m._reapply_hooks_after_push("my-agent", rel)

        data = json.loads((agent_dir / rel).read_text())
        cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        # Exactly what the satellite's own writer would produce at spawn — real
        # interpreter + real claude_dir path (NOT the virtual /users/<u> root).
        assert cmd == hook_command(sess._claude_dir / "permission_gate.py"), cmd
        assert sys.executable in cmd, cmd
        # snapshot refreshed to the on-disk (real) hash
        snap_hash = sess._file_snapshot.get(rel)
        assert snap_hash == file_sync._hash_file(agent_dir / rel), sess._file_snapshot
        print("ok: Claude live-session clobber re-applied to real paths + snapshot refreshed")


def test_codex_clobber_reapplied():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent_dir = tmp / "cx-agent"
        sess = _codex_session(agent_dir)
        rel = "users/alice/.codex/hooks.json"
        (agent_dir / rel).write_text(_virtual_codex_hooks())

        m = _mgr(tmp)
        m.sessions["s1"] = sess  # live -p codex session
        m._reapply_hooks_after_push("cx-agent", rel)

        data = json.loads((agent_dir / rel).read_text())
        cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert cmd == hook_command(sess._codex_dir / "permission_gate.py"), cmd
        print("ok: Codex live-session hooks.json clobber re-applied to real paths")


def test_no_live_session_left_untouched():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent_dir = tmp / "idle-agent"
        claude_dir = agent_dir / "users" / "alice" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        rel = "users/alice/.claude/settings.json"
        virtual = _virtual_settings()
        (agent_dir / rel).write_text(virtual)

        m = _mgr(tmp)  # no sessions registered
        m._reapply_hooks_after_push("idle-agent", rel)

        # Untouched (next spawn rewrites): still the virtual version.
        assert (agent_dir / rel).read_text() == virtual
        print("ok: non-live agent push left untouched (next spawn rewrites)")


def test_non_hooks_file_early_out():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent_dir = tmp / "my-agent"
        sess = _claude_session(agent_dir)
        rel = "users/alice/.claude/CLAUDE.md"
        (agent_dir / rel).write_text("# memory")
        # write a clobbered settings.json too — must stay clobbered (not our push)
        settings_rel = "users/alice/.claude/settings.json"
        (agent_dir / settings_rel).write_text(_virtual_settings())

        m = _mgr(tmp)
        m.pty_sessions["s1"] = sess
        m._reapply_hooks_after_push("my-agent", rel)  # push was CLAUDE.md

        # settings.json NOT touched by a non-hooks push.
        assert "/users/alice/" in (agent_dir / settings_rel).read_text()
        print("ok: non-hooks push (CLAUDE.md) early-outs (no spurious rewrite)")


def test_wrong_user_session_not_matched():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent_dir = tmp / "my-agent"
        # live session for bob; the push is for alice
        bob = _claude_session(agent_dir, user="bob")
        alice_rel = "users/alice/.claude/settings.json"
        (agent_dir / alice_rel).parent.mkdir(parents=True, exist_ok=True)
        virtual = _virtual_settings()
        (agent_dir / alice_rel).write_text(virtual)

        m = _mgr(tmp)
        m.pty_sessions["s1"] = bob
        m._reapply_hooks_after_push("my-agent", alice_rel)

        # bob's session must not have rewritten alice's file.
        assert (agent_dir / alice_rel).read_text() == virtual
        assert bob._file_snapshot == {}
        print("ok: a different user's live session does not match the push")


def main():
    test_claude_clobber_reapplied()
    test_codex_clobber_reapplied()
    test_no_live_session_left_untouched()
    test_non_hooks_file_early_out()
    test_wrong_user_session_not_matched()
    print("\nALL PASS (5/5)")


if __name__ == "__main__":
    main()
