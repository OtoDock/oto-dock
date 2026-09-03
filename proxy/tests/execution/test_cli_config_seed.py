"""seed_interactive_cli_config — the .claude.json pre-seed for interactive TUIs.

The seed must skip the first-run wizard (onboarding/trust), silence the
per-session auto-mode entry banner (fresh config dir per spawn → the CLI's
once-per-install notices would re-trigger every session), and pick the theme
matching the dashboard mode — while preserving whatever else is already in
the file (userID etc. from headless)."""
import json
from pathlib import Path

from services.engines.cli_settings_manager import seed_interactive_cli_config


def _read(dir_: Path) -> dict:
    return json.loads((dir_ / ".claude.json").read_text())


def test_seed_writes_wizard_and_banner_flags(tmp_path):
    seed_interactive_cli_config(str(tmp_path), "/sandbox/cwd", theme="light")
    data = _read(tmp_path)
    assert data["hasCompletedOnboarding"] is True
    assert data["hasSeenAutoModeEntryWarning"] is True
    # Resume-from-summary picker suppression (would eat cold-flushed prompts).
    assert data["resumeReturnDismissed"] is True
    assert data["theme"] == "light"
    proj = data["projects"]["/sandbox/cwd"]
    assert proj["hasTrustDialogAccepted"] is True
    assert proj["hasCompletedProjectOnboarding"] is True


def test_seed_defaults_dark_and_merges_existing(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"userID": "u-1"}))
    seed_interactive_cli_config(str(tmp_path), "", theme="")
    data = _read(tmp_path)
    assert data["userID"] == "u-1"  # merge, not replace
    assert data["theme"] == "dark"
    assert data["hasSeenAutoModeEntryWarning"] is True
    assert "projects" not in data  # no cwd → no trust entry
