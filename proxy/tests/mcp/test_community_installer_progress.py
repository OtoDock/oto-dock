"""Unit tests for the community_installer progress + off-loop FS helpers.

These cover the pieces added/refactored for async install progress without
needing the DB or a real npm/pip/docker install:

- ``_emit`` forwards job-level progress events and never propagates a bad sink.
- ``_apply_extracted_files`` copies a fresh install and, on update, preserves
  ``_PRESERVE_DIRS`` (node_modules/venv/...) while replacing everything else and
  taking a backup.
- ``_rollback_extracted_files`` restores the backup over the target.
"""

import asyncio

import pytest

from services.community import community_installer as ci


# --------------------------------------------------------------------------- #
# _emit
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_emit_forwards_event_to_async_cb():
    seen: list[dict] = []

    async def cb(ev: dict):
        seen.append(ev)

    await ci._emit(cb, "install", 50, "installing")
    assert seen == [{"phase": "install", "pct": 50, "message": "installing"}]


@pytest.mark.asyncio
async def test_emit_forwards_to_sync_cb():
    seen: list[dict] = []
    await ci._emit(seen.append, "fetch", 5, "downloading")
    assert seen[0]["phase"] == "fetch" and seen[0]["pct"] == 5


@pytest.mark.asyncio
async def test_emit_none_is_noop():
    # Must not raise — the default caller (zip-upload / approve) passes None.
    await ci._emit(None, "prepare", 15, "x")


@pytest.mark.asyncio
async def test_emit_swallows_callback_errors():
    async def bad(_ev):
        raise RuntimeError("sink exploded")

    # A failing progress sink must never fail an install.
    await ci._emit(bad, "install", 30, "x")


# --------------------------------------------------------------------------- #
# _apply_extracted_files / _rollback_extracted_files
# --------------------------------------------------------------------------- #

def test_apply_fresh_copy(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "manifest.json").write_text("new")
    (src / "sub" / "server.js").write_text("code")
    target = tmp_path / "dest"

    ci._apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert (target / "manifest.json").read_text() == "new"
    assert (target / "sub" / "server.js").read_text() == "code"


def test_apply_update_preserves_node_modules_and_backs_up(tmp_path):
    # Existing install: old manifest + old code + a preserved node_modules.
    target = tmp_path / "mcp"
    (target / "node_modules").mkdir(parents=True)
    (target / "manifest.json").write_text("old")
    (target / "server.js").write_text("old-code")
    (target / "node_modules" / "dep.js").write_text("dep")

    # New extracted folder: bumped manifest + code, NO node_modules.
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text("new")
    (src / "server.js").write_text("new-code")

    backup_dir = target.with_suffix(".bak")
    ci._apply_extracted_files(src, target, is_update=True, backup_dir=backup_dir)

    # Replaced files updated; preserved dir kept; backup holds the old tree.
    assert (target / "manifest.json").read_text() == "new"
    assert (target / "server.js").read_text() == "new-code"
    assert (target / "node_modules" / "dep.js").read_text() == "dep"
    assert (backup_dir / "manifest.json").read_text() == "old"
    assert (backup_dir / "node_modules" / "dep.js").read_text() == "dep"


def test_rollback_restores_backup(tmp_path):
    target = tmp_path / "mcp"
    target.mkdir()
    (target / "broken.txt").write_text("half-installed")
    backup_dir = target.with_suffix(".bak")
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text("good")

    ci._rollback_extracted_files(target, backup_dir)

    assert (target / "manifest.json").read_text() == "good"
    assert not (target / "broken.txt").exists()   # the failed install is gone
    assert not backup_dir.exists()                # backup moved into place


def test_apply_runs_cleanly_via_to_thread(tmp_path):
    """The install path calls _apply_extracted_files via asyncio.to_thread — make
    sure it works off the event loop (no loop-bound state)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text("x")
    target = tmp_path / "dest"

    async def run():
        await asyncio.to_thread(ci._apply_extracted_files, src, target, False, None)

    asyncio.run(run())
    assert (target / "manifest.json").read_text() == "x"
