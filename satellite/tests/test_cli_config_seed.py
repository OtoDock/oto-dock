"""_seed_interactive_cli_config — the satellite twin of the proxy's
.claude.json pre-seed (wizard skip + auto-mode banner silence + theme)."""
from __future__ import annotations

import json
from pathlib import Path

from satellite.terminal.pty_session import _seed_interactive_cli_config


def _read(dir_: Path) -> dict:
    return json.loads((dir_ / ".claude.json").read_text())


def test_seed_writes_wizard_and_banner_flags(tmp_path):
    _seed_interactive_cli_config(tmp_path, "/remote/cwd", theme="light")
    data = _read(tmp_path)
    assert data["hasCompletedOnboarding"] is True
    assert data["hasSeenAutoModeEntryWarning"] is True
    # Resume-from-summary picker suppression (would eat cold-flushed prompts).
    assert data["resumeReturnDismissed"] is True
    assert data["theme"] == "light"
    assert data["projects"]["/remote/cwd"]["hasTrustDialogAccepted"] is True


def test_seed_keys_trust_under_both_slash_forms(tmp_path):
    # Windows cwd: Claude Code matches the forward-slash form internally.
    _seed_interactive_cli_config(tmp_path, "C:\\Users\\dev\\ws", theme="dark")
    projects = _read(tmp_path)["projects"]
    assert "C:\\Users\\dev\\ws" in projects
    assert "C:/Users/dev/ws" in projects
