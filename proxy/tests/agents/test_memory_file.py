"""Tests for the file-based memory primitives (index + topic files).

Covers:
  - virtual-path splitting + scope-relative validation (traversal, hidden
    segments, extensions, index write-deny)
  - the command set (view / create / str_replace / insert / delete / rename)
    including the verbatim contract error strings
  - the generated index: content, regen-on-write, staleness heal, budgets
  - caps: per-file hard/soft, per-scope total, topic-count
  - concurrency: parallel creates serialize cleanly
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from services.memory import memory_file
from services.memory.memory_file import MemoryOpError


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "memory"


def _create(root: Path, rel: str, text: str):
    return memory_file.op_create(root, rel, text)


# ---------------------------------------------------------------------------
# Virtual paths + validation
# ---------------------------------------------------------------------------

def test_split_virtual_path_scopes():
    assert memory_file.split_virtual_path("/memories/user/prefs.md") == ("user", "prefs.md")
    assert memory_file.split_virtual_path("/memories/agent/a/b.md") == ("agent", "a/b.md")
    assert memory_file.split_virtual_path("/memories/agent") == ("agent", "")
    assert memory_file.split_virtual_path("/memories") == ("", "")


def test_split_virtual_path_rejects_unknown():
    with pytest.raises(MemoryOpError, match="does not exist"):
        memory_file.split_virtual_path("/memories/global/x.md")
    with pytest.raises(MemoryOpError, match="does not exist"):
        memory_file.split_virtual_path("/etc/passwd")


def test_validate_rel_traversal_and_hidden():
    with pytest.raises(MemoryOpError, match="invalid segment"):
        memory_file.validate_rel("../escape.md", mutating=True)
    with pytest.raises(MemoryOpError, match="invalid segment"):
        memory_file.validate_rel("a/../../b.md", mutating=True)
    with pytest.raises(MemoryOpError, match="invalid segment"):
        memory_file.validate_rel(".hidden.md")


def test_validate_rel_extensions():
    assert memory_file.validate_rel("notes.md", mutating=True) == "notes.md"
    assert memory_file.validate_rel("sub/notes.txt", mutating=True) == "sub/notes.txt"
    with pytest.raises(MemoryOpError, match="unsupported extension"):
        memory_file.validate_rel("evil.py", mutating=True)
    # create demands an extension; delete/rename of directories must not.
    with pytest.raises(MemoryOpError, match="no extension"):
        memory_file.validate_rel("noext", mutating=True, require_ext=True)
    assert memory_file.validate_rel("subdir", mutating=True) == "subdir"


def test_validate_rel_denies_index_write():
    with pytest.raises(MemoryOpError, match="auto-generated"):
        memory_file.validate_rel("MEMORY.md", mutating=True)
    # Reads of the index are fine.
    assert memory_file.validate_rel("MEMORY.md") == "MEMORY.md"


def test_rename_file_to_bad_extension_denied(root):
    _create(root, "a.md", "x")
    with pytest.raises(MemoryOpError, match="unsupported extension"):
        memory_file.op_rename(root, "a.md", "a.py")


def test_resolve_strict_blocks_symlink_escape(root):
    root.mkdir(parents=True)
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("s")
    (root / "link").symlink_to(outside)
    with pytest.raises(MemoryOpError, match="does not exist"):
        memory_file._resolve_strict(root, "link/secret.md")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_and_view_file(root):
    r = _create(root, "prefs.md", "# User prefers metric units\n- since (2026-06-12)\n")
    assert r.output == "File created successfully at: prefs.md"
    assert "prefs.md" in r.changed and "MEMORY.md" in r.changed
    v = memory_file.op_view(root, "prefs.md")
    assert "Here's the content of prefs.md with line numbers:" in v.output
    assert "1\t# User prefers metric units" in v.output


def test_create_existing_errors_verbatim(root):
    _create(root, "a.md", "x")
    with pytest.raises(MemoryOpError, match=r"^Error: File a.md already exists$"):
        _create(root, "a.md", "y")


def test_create_in_subdir(root):
    r = _create(root, "projects/oto.md", "# OtoDock launch notes\n")
    assert (root / "projects" / "oto.md").exists()
    assert "projects/oto.md" in r.changed


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

def test_view_directory_listing(root):
    _create(root, "a.md", "# Alpha\n")
    _create(root, "sub/b.md", "# Beta\n")
    v = memory_file.op_view(root, "")
    assert "Directory:" in v.output
    assert "a.md" in v.output and "sub/" in v.output and "b.md" in v.output
    # Hidden lock file never appears.
    assert ".memlock" not in v.output


def test_view_range(root):
    _create(root, "a.md", "l1\nl2\nl3\nl4\n")
    v = memory_file.op_view(root, "a.md", view_range=[2, 3])
    assert "2\tl2" in v.output and "3\tl3" in v.output
    assert "l1" not in v.output and "l4" not in v.output
    with pytest.raises(MemoryOpError, match="Invalid `view_range`"):
        memory_file.op_view(root, "a.md", view_range=[0, 2])


def test_view_missing_path(root):
    root.mkdir(parents=True)
    with pytest.raises(MemoryOpError, match="does not exist"):
        memory_file.op_view(root, "nope.md")


def test_view_root_listing(root):
    _create(root, "a.md", "x")
    other = root.parent / "user-mem"
    listing = memory_file.view_root({"agent": root, "user": other})
    assert "agent/" in listing.output and "1 topic file" in listing.output
    assert "user/" in listing.output and "0 topic files" in listing.output


# ---------------------------------------------------------------------------
# str_replace
# ---------------------------------------------------------------------------

def test_str_replace_success_and_snippet(root):
    _create(root, "a.md", "# Topic\nfact: old value\nend\n")
    r = memory_file.op_str_replace(root, "a.md", "fact: old value", "fact: new value")
    assert r.output.startswith("The memory file has been edited.")
    assert "fact: new value" in (root / "a.md").read_text()


def test_str_replace_not_found_verbatim(root):
    _create(root, "a.md", "content\n")
    with pytest.raises(
        MemoryOpError,
        match=r"No replacement was performed, old_str `missing` did not appear verbatim in a.md\.",
    ):
        memory_file.op_str_replace(root, "a.md", "missing", "x")


def test_str_replace_multiple_matches_lists_lines(root):
    _create(root, "a.md", "dup\nother\ndup\n")
    with pytest.raises(MemoryOpError, match=r"Multiple occurrences of old_str `dup` in lines: \[1, 3\]"):
        memory_file.op_str_replace(root, "a.md", "dup", "x")


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------

def test_insert_at_line(root):
    _create(root, "a.md", "l1\nl3\n")
    r = memory_file.op_insert(root, "a.md", 1, "l2")
    assert r.output == "The file a.md has been edited."
    assert (root / "a.md").read_text() == "l1\nl2\nl3\n"


def test_insert_invalid_line_verbatim(root):
    _create(root, "a.md", "l1\n")
    with pytest.raises(MemoryOpError, match=r"Invalid `insert_line` parameter: 9.*\[0, 1\]"):
        memory_file.op_insert(root, "a.md", 9, "x")


# ---------------------------------------------------------------------------
# delete + rename
# ---------------------------------------------------------------------------

def test_delete_file(root):
    _create(root, "a.md", "x")
    r = memory_file.op_delete(root, "a.md")
    assert r.output == "Successfully deleted a.md"
    assert r.deleted == ["a.md"]
    assert not (root / "a.md").exists()


def test_delete_dir_recursive_enumerates(root):
    _create(root, "sub/a.md", "x")
    _create(root, "sub/deep/b.md", "y")
    r = memory_file.op_delete(root, "sub")
    assert sorted(r.deleted) == ["sub/a.md", "sub/deep/b.md"]
    assert not (root / "sub").exists()


def test_delete_scope_root_refused(root):
    root.mkdir(parents=True)
    with pytest.raises(MemoryOpError, match="cannot delete a memory scope root"):
        memory_file.op_delete(root, "")


def test_rename_file_and_no_overwrite(root):
    _create(root, "a.md", "x")
    _create(root, "b.md", "y")
    with pytest.raises(MemoryOpError, match=r"^Error: The destination b.md already exists$"):
        memory_file.op_rename(root, "a.md", "b.md")
    r = memory_file.op_rename(root, "a.md", "c.md")
    assert r.output == "Successfully renamed a.md to c.md"
    assert r.deleted == ["a.md"] and "c.md" in r.changed


def test_rename_dir_enumerates_files(root):
    _create(root, "old/a.md", "x")
    _create(root, "old/deep/b.md", "y")
    r = memory_file.op_rename(root, "old", "new")
    assert sorted(r.deleted) == ["old/a.md", "old/deep/b.md"]
    assert "new/a.md" in r.changed and "new/deep/b.md" in r.changed


# ---------------------------------------------------------------------------
# Index generation + healing
# ---------------------------------------------------------------------------

def test_index_content_and_summary(root):
    _create(root, "infra.md", "# Prod cluster is main-eu\ndetails...\n")
    _create(root, "prefs.md", "## User prefers metric units\n")
    index = (root / "MEMORY.md").read_text()
    assert index.startswith("# Memory index (auto-generated")
    assert "- infra.md — Prod cluster is main-eu (updated " in index
    assert "- prefs.md — User prefers metric units (updated " in index


def test_index_excluded_from_itself_and_updates_on_delete(root):
    _create(root, "a.md", "# Alpha\n")
    memory_file.op_delete(root, "a.md")
    index = (root / "MEMORY.md").read_text()
    assert "a.md" not in index
    assert "MEMORY.md —" not in index


def test_index_summary_truncated(root):
    long_line = "# " + "x" * 500
    _create(root, "long.md", long_line + "\n")
    index = (root / "MEMORY.md").read_text()
    entry = [l for l in index.splitlines() if l.startswith("- long.md")][0]
    assert len(entry) < 300 and "…" in entry


def test_index_stale_and_heal(root):
    _create(root, "a.md", "# Alpha\n")
    assert not memory_file.index_is_stale(root)
    # Simulate a human hand-edit: bump the topic mtime past the index.
    import os, time
    future = time.time() + 5
    (root / "a.md").write_text("# Renamed heading by hand\n")
    os.utime(root / "a.md", (future, future))
    assert memory_file.index_is_stale(root)
    assert memory_file.heal_index_if_stale(root) is True
    assert "Renamed heading by hand" in (root / "MEMORY.md").read_text()
    assert not memory_file.index_is_stale(root)


def test_index_missing_counts_as_stale(root):
    _create(root, "a.md", "# Alpha\n")
    (root / "MEMORY.md").unlink()
    assert memory_file.index_is_stale(root)


def test_index_line_budget_truncation(root):
    # Build content over the line budget without 200 real files: shrink the cap.
    orig = memory_file.INDEX_MAX_LINES
    memory_file.INDEX_MAX_LINES = 3
    try:
        for i in range(5):
            _create(root, f"t{i}.md", f"# Topic {i}\n")
        index = (root / "MEMORY.md").read_text()
        assert "more topics not indexed" in index
    finally:
        memory_file.INDEX_MAX_LINES = orig


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

def test_topic_hard_cap_rejected(root):
    big = "x" * (memory_file.TOPIC_HARD_CAP_BYTES + 1)
    with pytest.raises(MemoryOpError, match="per-file cap"):
        _create(root, "big.md", big)
    assert not (root / "big.md").exists()


def test_topic_soft_warn(root):
    soft = "x" * (memory_file.TOPIC_SOFT_WARN_BYTES + 1)
    r = _create(root, "warn.md", soft)
    assert any("Consider tightening" in w for w in r.warnings)


def test_topic_count_cap(root):
    orig = memory_file.MAX_TOPICS_PER_SCOPE
    memory_file.MAX_TOPICS_PER_SCOPE = 2
    try:
        _create(root, "a.md", "x")
        _create(root, "b.md", "y")
        with pytest.raises(MemoryOpError, match="Consolidate existing topics"):
            _create(root, "c.md", "z")
    finally:
        memory_file.MAX_TOPICS_PER_SCOPE = orig


def test_scope_total_cap(root):
    orig = memory_file.SCOPE_TOTAL_CAP_BYTES
    memory_file.SCOPE_TOTAL_CAP_BYTES = 1024
    try:
        _create(root, "a.md", "x" * 800)
        with pytest.raises(MemoryOpError, match="total cap"):
            _create(root, "b.md", "y" * 800)
        # Replacing within the SAME file is allowed when net total stays under.
        r = memory_file.op_str_replace(root, "a.md", "x" * 800, "z" * 100)
        assert "edited" in r.output
    finally:
        memory_file.SCOPE_TOTAL_CAP_BYTES = orig


# ---------------------------------------------------------------------------
# Sanitation + concurrency
# ---------------------------------------------------------------------------

def test_control_chars_stripped_newlines_kept(root):
    _create(root, "a.md", "line1\x00bad\nline2\twith tab\n")
    text = (root / "a.md").read_text()
    assert "\x00" not in text
    assert "line1bad\nline2\twith tab\n" == text


def test_concurrent_creates_serialize(root):
    errors: list[Exception] = []

    def worker(i: int):
        try:
            _create(root, f"t{i}.md", f"# Topic {i}\n")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(memory_file.iter_topic_files(root)) == 8
    index = (root / "MEMORY.md").read_text()
    # Index reflects all 8 (each create regenerates under the scope lock).
    assert all(f"t{i}.md" in index for i in range(8))


def test_scope_total_bytes_and_iter_skip_hidden(root):
    _create(root, "a.md", "12345")
    (root / ".hidden.md").write_text("nope")
    files = memory_file.iter_topic_files(root)
    assert [f.name for f in files] == ["a.md"]
    assert memory_file.scope_total_bytes(root) == 5


def test_git_repo_root_never_resolves_to_user_dir_root(tmp_path):
    # A bare pre-created users/{u}/.git (the codex inner-sandbox mountpoint)
    # must never be adopted as a repo by the memory-commit path: both scopes
    # resolve to dedicated subdirectory repos, not the user dir root.
    from services.memory.memory_file import git_repo_root
    agent_dir = tmp_path / "agents" / "a1"
    assert git_repo_root(agent_dir, "agent") == agent_dir / "knowledge"
    assert git_repo_root(agent_dir, "user", "alice") == \
        agent_dir / "users" / "alice" / "context"
