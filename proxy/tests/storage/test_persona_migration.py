"""Startup persona-filename migration (config/prompt.md → config/agent.md).

Covers the one-shot sweep (rename / duplicate-drop / divergent-keep, with
sync tombstones + config-repo commits) and the reader's name preference in
``config._read_agent_files``.

Run: cd proxy && python -m pytest tests/storage/test_persona_migration.py -v
"""

from __future__ import annotations

import subprocess
import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config as app_config
from storage import agent_store, file_tombstones_store


def _mk_agent(slug: str):
    """Create an agent row + folder tree (no persona file — the API layer
    writes that; the store only makes dirs and inits the config repo)."""
    agent_store.create_agent(slug, slug.replace("-", " ").title(),
                             default_scope="user")
    return app_config.AGENTS_DIR / slug


def _config_git_log(config_dir) -> str:
    out = subprocess.run(
        ["git", "log", "--oneline"], cwd=config_dir,
        capture_output=True, text=True,
    )
    return out.stdout


class TestMigratePersonaFilenames:
    def test_renames_tombstones_and_commits(self, temp_db):
        cfg = _mk_agent("mig-rename") / "config"
        (cfg / "prompt.md").write_text("persona v1")

        assert agent_store.migrate_persona_filenames() == 1

        assert (cfg / "agent.md").read_text() == "persona v1"
        assert not (cfg / "prompt.md").exists()
        # Sync tombstone: an idle satellite must APPLY the delete, not
        # resurrect its stale prompt.md at next merge.
        assert file_tombstones_store.get("mig-rename", "config/prompt.md") is not None
        # Audit repo tracks the rename so revert-from-history stays coherent.
        assert "agent.md" in _config_git_log(cfg)

    def test_identical_duplicate_is_dropped(self, temp_db):
        cfg = _mk_agent("mig-dup") / "config"
        (cfg / "agent.md").write_text("same")
        (cfg / "prompt.md").write_text("same")

        assert agent_store.migrate_persona_filenames() == 1

        assert (cfg / "agent.md").read_text() == "same"
        assert not (cfg / "prompt.md").exists()
        assert file_tombstones_store.get("mig-dup", "config/prompt.md") is not None

    def test_divergent_copies_are_both_kept(self, temp_db):
        cfg = _mk_agent("mig-div") / "config"
        (cfg / "agent.md").write_text("new persona")
        (cfg / "prompt.md").write_text("old persona")

        assert agent_store.migrate_persona_filenames() == 0

        assert (cfg / "agent.md").read_text() == "new persona"
        assert (cfg / "prompt.md").read_text() == "old persona"
        assert file_tombstones_store.get("mig-div", "config/prompt.md") is None

    def test_already_converged_is_noop(self, temp_db):
        cfg = _mk_agent("mig-done") / "config"
        (cfg / "agent.md").write_text("persona")

        assert agent_store.migrate_persona_filenames() == 0
        assert (cfg / "agent.md").read_text() == "persona"


class TestReaderNamePreference:
    def test_reader_prefers_agent_md(self, temp_db):
        cfg = _mk_agent("read-pref") / "config"
        (cfg / "agent.md").write_text("current")
        (cfg / "prompt.md").write_text("stale")

        files = app_config._read_agent_files("read-pref")
        assert files[0] == ("agent.md", "current")

    def test_reader_falls_back_to_legacy_name(self, temp_db):
        """A restored pre-1.4 backup keeps working before the next startup
        sweep converges it."""
        cfg = _mk_agent("read-legacy") / "config"
        (cfg / "prompt.md").write_text("legacy persona")

        files = app_config._read_agent_files("read-legacy")
        # Content is served under the canonical label regardless of source.
        assert files[0] == ("agent.md", "legacy persona")

    def test_reader_missing_persona_returns_empty(self, temp_db):
        _mk_agent("read-none")
        assert app_config._read_agent_files("read-none") == []
