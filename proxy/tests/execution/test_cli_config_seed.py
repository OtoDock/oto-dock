"""seed_interactive_cli_config — the .claude.json pre-seed for interactive TUIs.

The seed must skip the first-run wizard (onboarding/trust), silence the
per-session auto-mode entry banner (fresh config dir per spawn → the CLI's
once-per-install notices would re-trigger every session), and pick the theme
matching the dashboard mode — while preserving whatever else is already in
the file (userID etc. from headless)."""
import json
import os
import threading
from pathlib import Path

import pytest

import config
from services.engines import cli_settings_manager
from services.engines.cli_settings_manager import seed_interactive_cli_config


@pytest.fixture(autouse=True)
def _agents_root(monkeypatch, tmp_path):
    # The seed reads and writes beneath an agent's folder only.
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)


def _read(dir_: Path) -> dict:
    return json.loads((dir_ / ".claude.json").read_text())


def test_seed_writes_wizard_and_banner_flags(tmp_path):
    store = _store(tmp_path)
    seed_interactive_cli_config(str(store), "/sandbox/cwd", theme="light")
    data = _read(store)
    assert data["hasCompletedOnboarding"] is True
    assert data["hasSeenAutoModeEntryWarning"] is True
    # Resume-from-summary picker suppression (would eat cold-flushed prompts).
    assert data["resumeReturnDismissed"] is True
    assert data["theme"] == "light"
    proj = data["projects"]["/sandbox/cwd"]
    assert proj["hasTrustDialogAccepted"] is True
    assert proj["hasCompletedProjectOnboarding"] is True


def test_seed_defaults_dark_and_merges_existing(tmp_path):
    store = _store(tmp_path)
    (store / ".claude.json").write_text(json.dumps({"userID": "u-1"}))
    seed_interactive_cli_config(str(store), "", theme="")
    data = _read(store)
    assert data["userID"] == "u-1"  # merge, not replace
    assert data["theme"] == "dark"
    assert data["hasSeenAutoModeEntryWarning"] is True
    assert "projects" not in data  # no cwd → no trust entry


def _store(tmp_path) -> Path:
    store = tmp_path / "ag" / "users" / "u" / ".claude"
    store.mkdir(parents=True)
    return store


def test_a_link_at_the_file_is_replaced_never_read_or_written_through(tmp_path):
    store = _store(tmp_path)
    victim = tmp_path / "elsewhere.json"
    victim.write_text(json.dumps({"secret": "stand-in"}))
    (store / ".claude.json").symlink_to(victim)
    seed_interactive_cli_config(str(store), "/c", theme="dark")
    assert json.loads(victim.read_text()) == {"secret": "stand-in"}
    assert not (store / ".claude.json").is_symlink()
    data = _read(store)
    assert data["hasCompletedOnboarding"] is True and "secret" not in data


def test_a_pipe_at_the_file_is_replaced_at_once(tmp_path):
    store = _store(tmp_path)
    os.mkfifo(store / ".claude.json")
    th = threading.Thread(target=seed_interactive_cli_config, args=(str(store), "/c"), daemon=True)
    th.start()
    th.join(5)
    assert not th.is_alive()
    assert _read(store)["hasCompletedOnboarding"] is True


def test_a_file_over_the_cap_is_left_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_settings_manager, "_SEED_MAX_BYTES", 64)
    store = _store(tmp_path)
    big = json.dumps({"projects": {"x": "y" * 200}})
    (store / ".claude.json").write_text(big)
    seed_interactive_cli_config(str(store), "/c")
    assert (store / ".claude.json").read_text() == big


def test_a_linked_folder_on_the_way_gets_nothing(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    store = tmp_path / "ag" / "users" / "u" / ".claude"
    store.parent.mkdir(parents=True)
    store.symlink_to(target)
    seed_interactive_cli_config(str(store), "/c")
    assert list(target.iterdir()) == []


def test_an_agent_whose_folder_is_a_link_is_seeded(tmp_path, monkeypatch):
    """The operator may move an agent's folder and leave a link in its place:
    the seed is read and written beneath the real folder, and a link below
    it still gets nothing."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    real = tmp_path / "disk2" / "ag"
    (real / "users" / "u" / ".claude").mkdir(parents=True)
    (agents / "ag").symlink_to(real)
    seed_interactive_cli_config(str(agents / "ag" / "users" / "u" / ".claude"), "/c")
    assert _read(real / "users" / "u" / ".claude")["hasCompletedOnboarding"] is True
    target = tmp_path / "elsewhere"
    target.mkdir()
    (real / "users" / "v").mkdir()
    (real / "users" / "v" / ".claude").symlink_to(target)
    seed_interactive_cli_config(str(agents / "ag" / "users" / "v" / ".claude"), "/c")
    assert list(target.iterdir()) == []
