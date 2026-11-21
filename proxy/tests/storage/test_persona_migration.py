"""Startup persona-filename migration (config/prompt.md → config/agent.md).

Covers the one-shot sweep (rename / duplicate-drop / divergent-keep, with
sync tombstones + config-repo commits) and the reader's name preference in
``configuration.prompts._read_agent_files``.

Run: cd proxy && python -m pytest tests/storage/test_persona_migration.py -v
"""

from __future__ import annotations

import sys

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config as app_config
from configuration import prompts
from services.agents import agent_provisioning
from storage.agents import agent_store
from storage.files import file_tombstones_store


def _mk_agent(slug: str):
    """Create an agent row + folder tree (no persona file — the API layer
    writes that; the store only makes dirs and inits the config repo)."""
    agent_store.create_agent(slug, slug.replace("-", " ").title(),
                             default_scope="user")
    return app_config.AGENTS_DIR / slug


def _config_git_log(config_dir) -> str:
    from services.infra import git_writer
    return "\n".join(row["subject"] for row in git_writer.log(config_dir))


class TestMigratePersonaFilenames:
    def test_renames_tombstones_and_commits(self, temp_db):
        cfg = _mk_agent("mig-rename") / "config"
        (cfg / "prompt.md").write_text("persona v1")

        assert agent_provisioning.migrate_persona_filenames() == 1

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

        assert agent_provisioning.migrate_persona_filenames() == 1

        assert (cfg / "agent.md").read_text() == "same"
        assert not (cfg / "prompt.md").exists()
        assert file_tombstones_store.get("mig-dup", "config/prompt.md") is not None

    def test_divergent_copies_are_both_kept(self, temp_db):
        cfg = _mk_agent("mig-div") / "config"
        (cfg / "agent.md").write_text("new persona")
        (cfg / "prompt.md").write_text("old persona")

        assert agent_provisioning.migrate_persona_filenames() == 0

        assert (cfg / "agent.md").read_text() == "new persona"
        assert (cfg / "prompt.md").read_text() == "old persona"
        assert file_tombstones_store.get("mig-div", "config/prompt.md") is None

    def test_already_converged_is_noop(self, temp_db):
        cfg = _mk_agent("mig-done") / "config"
        (cfg / "agent.md").write_text("persona")

        assert agent_provisioning.migrate_persona_filenames() == 0
        assert (cfg / "agent.md").read_text() == "persona"


class TestReaderNamePreference:
    def test_reader_prefers_agent_md(self, temp_db):
        cfg = _mk_agent("read-pref") / "config"
        (cfg / "agent.md").write_text("current")
        (cfg / "prompt.md").write_text("stale")

        files = prompts._read_agent_files("read-pref")
        assert files[0] == ("agent.md", "current")

    def test_reader_falls_back_to_legacy_name(self, temp_db):
        """A restored pre-1.4 backup keeps working before the next startup
        sweep converges it."""
        cfg = _mk_agent("read-legacy") / "config"
        (cfg / "prompt.md").write_text("legacy persona")

        files = prompts._read_agent_files("read-legacy")
        # Content is served under the canonical label regardless of source.
        assert files[0] == ("agent.md", "legacy persona")

    def test_reader_missing_persona_returns_empty(self, temp_db):
        _mk_agent("read-none")
        assert prompts._read_agent_files("read-none") == []


class TestMigratePersonaFollowsNoLink:
    def test_a_config_folder_replaced_by_a_link_is_left_as_it_is(self, temp_db, tmp_path):
        agent = _mk_agent("mig-link")
        stand_in = tmp_path / "stand-in"
        stand_in.mkdir()
        (stand_in / "prompt.md").write_text("elsewhere")
        import shutil
        shutil.rmtree(agent / "config")
        (agent / "config").symlink_to(stand_in)
        assert agent_provisioning.migrate_persona_filenames() == 0
        assert sorted(p.name for p in stand_in.iterdir()) == ["prompt.md"]
