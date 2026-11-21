"""services/apps/releases: an app's release and data folders are reached
from the agents root with the agent's name first and no link followed, so a
folder swapped for a link is never read, written or removed through. Every
link here sits in the test's own folder."""

import json

import pytest

import config
from services.apps import releases

ROW = {"agent": "relagent", "username": "", "id": "app-9", "slug": "board"}


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / ROW["agent"]).mkdir(parents=True)
    victim = tmp_path / "agents" / "other" / "victim"
    victim.mkdir(parents=True)
    return victim


def test_a_linked_bucket_is_never_removed_through(agents):
    (agents / "shared" / ROW["id"] / "1").mkdir(parents=True)
    (agents / "shared" / ROW["slug"]).mkdir(parents=True)
    agent_dir = config.AGENTS_DIR / ROW["agent"]
    (agent_dir / releases.RELEASES_DIRNAME).symlink_to(agents)
    (agent_dir / releases.DATA_DIRNAME).symlink_to(agents)
    releases.remove_release_dir(ROW)
    releases.remove_data_dir(ROW)
    assert (agents / "shared" / ROW["id"] / "1").is_dir()
    assert (agents / "shared" / ROW["slug"]).is_dir()


def test_a_linked_manifest_is_never_read(agents):
    (agents / "manifest.json").write_text(json.dumps({"files": {"a": {"sha256": "x"}}}))
    release = releases.app_release_dir(ROW) / "1"
    release.mkdir(parents=True)
    (release / releases.MANIFEST_NAME).symlink_to(agents / "manifest.json")
    assert releases.read_manifest(release) is None
    assert releases.tree_sha(release) == ""
    assert not releases.is_plain_file(release, releases.MANIFEST_NAME)


def test_the_stale_sweep_and_the_listing_never_enter_a_link(agents, caplog):
    base = releases.app_release_dir(ROW)
    base.mkdir(parents=True)
    (agents / "kept.txt").write_text("keep")
    (base / f"{releases.SCRATCH_DATA_PREFIX}old").symlink_to(agents)
    with caplog.at_level("WARNING", logger="otodock.apps"):
        releases.sweep_stale(base, releases.SCRATCH_DATA_PREFIX, 0)
    assert (agents / "kept.txt").read_text() == "keep"
    # A link is not one of the sweep's folders at all: nothing was tried.
    assert not [r for r in caplog.records if "not removed" in r.getMessage()]
    assert releases.entries(base) == ([], [])
