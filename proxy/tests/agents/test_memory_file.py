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
from datetime import date
from pathlib import Path

import pytest

from services.memory import memory_file, memory_index
from services.memory.memory_file import Caps, MemoryCapError, MemoryOpError


@pytest.fixture
def root(monkeypatch):
    """A scope root under the agents root (every memory write opens beneath
    ``config.AGENTS_DIR``; a root outside it is refused)."""
    import config
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(config, "AGENTS_DIR", Path(d))
        yield Path(d) / "memory"


def _create(root: Path, rel: str, text: str):
    return memory_file.op_create(root, rel, text)


def _seed(root: Path, rel: str, text: str | bytes) -> None:
    """A topic written by hand (the Files UI, a satellite): the tool's caps
    never saw it, so it can be any size."""
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        (root / rel).write_bytes(text)
    else:
        (root / rel).write_text(text)


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


def test_an_empty_insert_is_refused_and_writes_nothing(root):
    _create(root, "a.md", "l1\n")
    before = (root / "a.md").stat().st_mtime_ns
    with pytest.raises(MemoryOpError, match="`insert_text` must be non-empty"):
        memory_file.op_insert(root, "a.md", 1, "")
    assert (root / "a.md").read_text() == "l1\n"
    assert (root / "a.md").stat().st_mtime_ns == before


def test_an_insert_of_control_characters_alone_is_refused(root):
    _create(root, "a.md", "l1\n")
    with pytest.raises(MemoryOpError, match="`insert_text` must be non-empty"):
        memory_file.op_insert(root, "a.md", 1, "\x01\x02")
    assert (root / "a.md").read_text() == "l1\n"


def test_a_blank_line_still_inserts(root):
    _create(root, "a.md", "l1\nl2\n")
    memory_file.op_insert(root, "a.md", 1, "\n")
    assert (root / "a.md").read_text() == "l1\n\nl2\n"


def test_an_insert_past_the_soft_limit_lands_with_the_warning(root):
    big = "\n".join(f"- line {i} " + "x" * 60 for i in range(300)) + "\n"
    assert len(big) > memory_file.TOPIC_SOFT_WARN_BYTES
    _create(root, "big.md", big)
    r = memory_file.op_insert(root, "big.md", 150, "- the new line")
    assert r.output == "The file big.md has been edited."
    assert any("over the 16,384 byte target" in w for w in r.warnings)
    assert r.over_target == (root / "big.md").stat().st_size
    assert (root / "big.md").read_text().splitlines()[150] == "- the new line"


def test_an_insert_past_the_hard_cap_is_refused_and_writes_nothing(root):
    near = "x" * (memory_file.TOPIC_HARD_CAP_BYTES - 10) + "\n"
    _seed(root, "full.md", near)
    with pytest.raises(MemoryOpError, match="per-file cap"):
        memory_file.op_insert(root, "full.md", 1, "y" * 64)
    assert (root / "full.md").read_text() == near


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
    assert index.startswith("# Memory index v2 (generated from the topic files")
    assert "- infra.md: Prod cluster is main-eu (" in index
    assert "- prefs.md: User prefers metric units (" in index


def test_index_excluded_from_itself_and_updates_on_delete(root):
    _create(root, "a.md", "# Alpha\n")
    memory_file.op_delete(root, "a.md")
    index = (root / "MEMORY.md").read_text()
    assert "a.md" not in index
    assert "MEMORY.md:" not in index


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
    import os
    import time
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
    orig = memory_index.INDEX_MAX_LINES
    memory_index.INDEX_MAX_LINES = 3
    try:
        for i in range(5):
            _create(root, f"t{i}.md", f"# Topic {i}\n")
        index = (root / "MEMORY.md").read_text()
        assert "more topics not indexed" in index
    finally:
        memory_index.INDEX_MAX_LINES = orig


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
    assert r.warnings == [
        "warn.md is 16,385 bytes, over the 16,384 byte target. Bring it under "
        "the target now: fold duplicates, drop superseded trails, move logs out."
    ]
    assert r.over_target == 16_385


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
        except Exception as e:  # noqa: BLE001  # pragma: no cover, a worker thread's error is collected for the test
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


def test_scope_roots_refuse_a_username_that_leaves_the_agent(tmp_path):
    from services.memory.memory_file import git_repo_root, scope_root
    agent_dir = tmp_path / "agents" / "pa"
    assert scope_root(agent_dir, "agent") == agent_dir / "knowledge" / "memory"
    assert scope_root(agent_dir, "user", "alice") == (
        agent_dir / "users" / "alice" / "context" / "memory")
    # ``users/../context`` stays below the agent dir (a wrong place, not an
    # escape); these two would leave it.
    for bad in ("../../other", "/etc"):
        with pytest.raises(ValueError):
            scope_root(agent_dir, "user", bad)
        with pytest.raises(ValueError):
            git_repo_root(agent_dir, "user", bad)


# ---------------------------------------------------------------------------
# The memory writes open beneath the agents root
# ---------------------------------------------------------------------------


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    return tmp_path


def _swap_on_open(monkeypatch, root, victim):
    import contextlib
    import os

    from services.infra import safe_fs
    real = safe_fs.open_root
    state = {"done": False}

    @contextlib.contextmanager
    def _patched(r, rel=""):
        if not state["done"]:
            state["done"] = True
            d = root / "topics"
            for child in d.iterdir():
                child.unlink()
            d.rmdir()
            os.symlink(victim, d)
        with real(r, rel) as fd:
            yield fd

    monkeypatch.setattr(safe_fs, "open_root", _patched)


def test_memory_write_refuses_a_swapped_component(agents_root, monkeypatch):
    root = agents_root / "memory"
    memory_file.op_create(root, "topics/a.md", "# a\nfirst")
    victim = agents_root.parent / f"victim-{agents_root.name}"
    victim.mkdir(exist_ok=True)
    (victim / "a.md").write_text("ORIGINAL")
    _swap_on_open(monkeypatch, root, victim)
    with pytest.raises((MemoryOpError, OSError)):
        memory_file.op_str_replace(root, "topics/a.md", "first", "second")
    assert (victim / "a.md").read_text() == "ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["a.md"]


def test_memory_root_outside_the_agents_tree_is_refused(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    with pytest.raises(MemoryOpError):
        memory_file.op_create(tmp_path / "elsewhere" / "memory", "a.md", "# a")
    assert not (tmp_path / "elsewhere").exists()


def test_memory_delete_and_rename_open_beneath_the_root(agents_root):
    root = agents_root / "memory"
    memory_file.op_create(root, "topics/a.md", "# a\nx")
    memory_file.op_rename(root, "topics/a.md", "moved/b.md")
    assert (root / "moved" / "b.md").read_text().startswith("# a")
    memory_file.op_delete(root, "moved")
    assert not (root / "moved").exists()
    # The index was written beneath the root too.
    assert (root / memory_file.INDEX_FILENAME).exists()


# ---------------------------------------------------------------------------
# The growth cap, the entry cap and the shrink rule
# ---------------------------------------------------------------------------

def _dated_lines(n: int) -> str:
    return "# Big topic\nWhat this holds.\n\n## Facts\n" + "".join(
        f"- fact {i} (2026-10-01)\n" for i in range(n)
    )


def test_a_write_over_the_growth_cap_is_refused_with_the_target(root):
    big = _dated_lines(1800)
    assert memory_file.TOPIC_HARD_CAP_BYTES > len(big) > memory_file.DEFAULT_TOPIC_GROW_CAP_BYTES
    _seed(root, "big.md", big)
    with pytest.raises(MemoryCapError) as exc:
        memory_file.op_insert(root, "big.md", 4, "- one more (2026-10-28)")
    text = str(exc.value)
    assert text.startswith("Error: big.md would be ")
    assert "over the 32,768 byte growth cap. Bring it under 16,384 bytes first:" in text
    assert text.endswith("Or split it into a second topic. Then retry.")
    assert (root / "big.md").read_text() == big


def test_a_write_that_shrinks_an_over_cap_topic_passes(root):
    big = _dated_lines(1800)
    _seed(root, "big.md", big)
    r = memory_file.op_str_replace(root, "big.md", "- fact 7 (2026-10-01)\n", "")
    assert r.output.startswith("The memory file has been edited.")
    assert (root / "big.md").stat().st_size == len(big) - len("- fact 7 (2026-10-01)\n")
    # Still over the target: the warning stays, the date reminder does not
    # fire on a deletion.
    assert [w for w in r.warnings if "target" in w] and memory_file.DATE_REMINDER not in r.warnings


def test_a_shrink_passes_even_over_the_hard_cap_and_a_grow_names_it(root):
    huge = _dated_lines(3400)
    assert len(huge) > memory_file.TOPIC_HARD_CAP_BYTES
    _seed(root, "huge.md", huge)
    r = memory_file.op_str_replace(root, "huge.md", "- fact 3 (2026-10-01)\n", "")
    assert "edited" in r.output
    with pytest.raises(MemoryCapError, match="per-file cap"):
        memory_file.op_insert(root, "huge.md", 4, "- more (2026-10-28)")


def test_the_scope_total_cap_lets_a_shrink_through(root):
    orig = memory_file.SCOPE_TOTAL_CAP_BYTES
    memory_file.SCOPE_TOTAL_CAP_BYTES = 1024
    try:
        _seed(root, "a.md", "# A\n" + "- x (2026-10-01)\n" * 60)
        _seed(root, "b.md", "# B\n" + "- y (2026-10-01)\n" * 60)
        r = memory_file.op_str_replace(root, "a.md", "# A\n- x (2026-10-01)\n", "# A\n")
        assert "edited" in r.output
        with pytest.raises(MemoryCapError, match="total cap"):
            memory_file.op_insert(root, "b.md", 1, "- z (2026-10-01)")
    finally:
        memory_file.SCOPE_TOTAL_CAP_BYTES = orig


def test_the_entry_cap_measures_growth_not_length(root):
    _create(root, "a.md", "# A\n- old " + "x" * 3000 + " (2026-10-01)\n")
    r = memory_file.op_str_replace(root, "a.md", "x" * 3000, "y" * 4500)
    assert "edited" in r.output
    with pytest.raises(MemoryCapError) as exc:
        memory_file.op_str_replace(root, "a.md", "y" * 4500, "z" * 7000)
    assert str(exc.value) == (
        "Error: this edit adds 2,500 bytes, over the 2,048 byte entry cap. A "
        "memory entry is a fact, a rule or a pointer, not a log. Write the "
        "detail to a file outside memory and keep one line here."
    )
    with pytest.raises(MemoryCapError, match="entry cap"):
        memory_file.op_insert(root, "a.md", 1, "- " + "w" * 2100)


def test_create_meets_the_growth_cap_and_not_the_entry_cap(root):
    r = _create(root, "twenty.md", _dated_lines(1100))
    assert any("over the 16,384 byte target" in w for w in r.warnings)
    with pytest.raises(MemoryCapError, match="growth cap"):
        _create(root, "forty.md", _dated_lines(2200))
    assert not (root / "forty.md").exists()


def test_the_caps_come_from_the_settings_with_a_fallback():
    assert Caps.from_settings({}) == Caps()
    assert Caps.from_settings(
        {"topic_grow_cap_bytes": 40960, "entry_cap_bytes": 4096}
    ) == Caps(grow_cap=40960, entry_cap=4096)
    for bad in ({"topic_grow_cap_bytes": 100}, {"topic_grow_cap_bytes": True},
                {"topic_grow_cap_bytes": "32768"}, {"entry_cap_bytes": 10 ** 6}):
        assert Caps.from_settings(bad) == Caps()


def test_the_caps_argument_governs_the_write(root):
    text = _dated_lines(1100)
    with pytest.raises(MemoryCapError, match="growth cap"):
        memory_file.op_create(root, "a.md", text, caps=Caps(grow_cap=16384))
    r = memory_file.op_create(root, "a.md", text)
    assert "created" in r.output
    with pytest.raises(MemoryCapError, match="over the 512 byte entry cap"):
        memory_file.op_insert(root, "a.md", 1, "- " + "x" * 600, caps=Caps(entry_cap=512))


def test_the_current_size_is_measured_on_the_sanitised_text(root):
    # Each stray byte decodes to a three-byte replacement character: with the
    # size on disk as "current", a pure deletion would read as growth past
    # the hard cap.
    raw = b"# T\nabout it\n" + b"\xff" * 30_000 + b"\nEND\n"
    _seed(root, "raw.md", raw)
    r = memory_file.op_str_replace(root, "raw.md", "END\n", "")
    assert "edited" in r.output


# ---------------------------------------------------------------------------
# The date reminder
# ---------------------------------------------------------------------------

def test_an_undated_insert_gets_the_reminder_and_a_dated_one_does_not(root):
    _create(root, "a.md", "# A\n- first (2026-10-01)\n")
    r = memory_file.op_insert(root, "a.md", 2, "- second, no date")
    assert r.warnings == [memory_file.DATE_REMINDER]
    r = memory_file.op_insert(root, "a.md", 2, "- third (2026-10-28)")
    assert r.warnings == []
    # A heading says nothing dated.
    r = memory_file.op_insert(root, "a.md", 1, "## Section")
    assert r.warnings == []


def test_a_replace_reads_the_whole_edited_line_for_its_date(root):
    _create(root, "a.md", "# A\n- port 8080 (2026-10-01)\n- undated thing\n")
    r = memory_file.op_str_replace(root, "a.md", "port 8080", "port 8081")
    assert r.warnings == []
    r = memory_file.op_str_replace(root, "a.md", "undated thing", "still undated")
    assert r.warnings == [memory_file.DATE_REMINDER]
    r = memory_file.op_str_replace(root, "a.md", "- still undated\n", "")
    assert r.warnings == []
    # A date that is not a real day does not count.
    r = memory_file.op_str_replace(root, "a.md", "port 8081", "port 8082 (2026-99-99)")
    assert r.warnings == []  # the line keeps its real date


# ---------------------------------------------------------------------------
# The index: facts, marks, sections, the fallback and the header
# ---------------------------------------------------------------------------

OPS = (
    "# Ops traps\n"
    "Where the deploys bite.\n"
    "\n"
    "## Deploys\n"
    "- a (2026-01-02)\n"
    "- b (2026-03-04)\n"
    "\n"
    "## Shared checkout\n"
    "- c\n"
    "```\n"
    "## not a section\n"
    "- not an entry\n"
    "```\n"
    "### a third level, not listed\n"
)


def test_index_entry_names_size_entries_newest_summary_and_sections(root):
    _seed(root, "ops.md", OPS)
    index = memory_index.build_index_content(root, today=date(2026, 3, 10))
    size = len(OPS.encode())
    assert index.splitlines()[1:] == [
        f"- ops.md: Ops traps ({size} B, 3 entries, newest 2026-03-04)",
        "  Where the deploys bite.",
        "  · L4 Deploys · L8 Shared checkout",
    ]
    facts = memory_index.topic_facts(root / "ops.md", root, today=date(2026, 3, 10))
    assert memory_file.op_view(root, "ops.md", [facts.sections[1][0], -1]).output.splitlines()[1].endswith(
        "## Shared checkout"
    )


def test_index_marks_stale_undated_and_ignores_a_future_date(root):
    _seed(root, "ops.md", OPS)
    _seed(root, "plain.md", "# Plain\n- one line\n")
    _seed(root, "due.md", "# Due\n- renew the cert by 2027-01-01\n- noted (2026-10-20)\n")
    index = memory_index.build_index_content(root, today=date(2026, 10, 28))
    lines = index.splitlines()
    ops = next(line for line in lines if line.startswith("- ops.md: Ops traps ("))
    assert ops.endswith("newest 2026-03-04, stale)")
    assert any(line.endswith("(19 B, 1 entry, undated)") for line in lines)
    assert any("- due.md: Due (" in line and line.endswith("newest 2026-10-20)") for line in lines)


def test_the_summary_is_a_plain_line_and_the_sections_stop_at_eight(root):
    for name, second in (("b", "- a bullet"), ("n", "1. numbered"), ("q", "> quoted"),
                         ("t", "| a | b |"), ("h", "## heading")):
        _seed(root, f"{name}.md", f"# {name.upper()}\n{second}\n")
    _seed(root, "s.md", "## Starts with a section\n" + "".join(f"## S{i}\n" for i in range(10)))
    today = date(2026, 10, 28)
    for name in "bnqth":
        assert memory_index.topic_facts(root / f"{name}.md", root, today=today).summary == ""
    s = memory_index.topic_facts(root / "s.md", root, today=today)
    assert s.heading == "Starts with a section"
    assert [t for _n, t in s.sections] == [f"S{i}" for i in range(8)]
    long = "x" * 400
    _seed(root, "l.md", f"# L\n{long}\n")
    assert memory_index.topic_facts(root / "l.md", root, today=today).summary == ""


def test_the_index_falls_back_to_heading_lines_before_it_cuts(root):
    for i in range(5):
        _seed(root, f"t{i}.md", f"# Topic {i}\nSummary {i}.\n\n## One\n- a\n\n## Two\n- b\n")
    orig = memory_index.INDEX_MAX_LINES
    try:
        memory_index.INDEX_MAX_LINES = 7
        index = memory_index.build_index_content(root)
        lines = index.splitlines()
        assert len(lines) == 6 and not any("·" in line for line in lines)
        assert all(f"- t{i}.md: Topic {i} (" in index for i in range(5))
        memory_index.INDEX_MAX_LINES = 4
        index = memory_index.build_index_content(root)
        assert index.splitlines()[-1] == (
            "… (2 more topics not indexed. The index is over its budget: consolidate topic files.)"
        )
    finally:
        memory_index.INDEX_MAX_LINES = orig


def test_an_index_from_the_old_builder_is_healed_once(root):
    import os
    import time
    _create(root, "a.md", "# Alpha\n- a (2026-10-01)\n")
    old = "# Memory index (auto-generated — edit topic files, not this file)\n- a.md — Alpha (updated 2026-10-01)\n"
    (root / "MEMORY.md").write_text(old)
    future = time.time() + 5
    os.utime(root / "MEMORY.md", (future, future))
    assert memory_file.index_is_stale(root)
    assert memory_file.heal_index_if_stale(root) is True
    assert (root / "MEMORY.md").read_text().startswith(memory_index.INDEX_HEADER)
    assert not memory_file.index_is_stale(root)
    assert memory_file.heal_index_if_stale(root) is False


# ---------------------------------------------------------------------------
# promote: a topic moved from a personal root into the shared one
# ---------------------------------------------------------------------------

@pytest.fixture
def two_roots(root):
    shared = root.parent / "shared-memory"
    _create(root, "suppliers.md", "# Suppliers\n- Acme ships in two days (2026-10-01)\n")
    _create(shared, "ops.md", "# Ops\n- a (2026-10-01)\n")
    return root, shared


def test_promote_moves_the_topic_and_regenerates_both_indexes(two_roots):
    personal, shared = two_roots
    src, dst = memory_file.op_promote(personal, shared, "suppliers.md", "suppliers.md")
    assert not (personal / "suppliers.md").exists()
    assert (shared / "suppliers.md").read_text().startswith("# Suppliers")
    assert src.deleted == ["suppliers.md"] and src.changed == ["MEMORY.md"]
    assert dst.changed == ["suppliers.md", "MEMORY.md"] and dst.deleted == []
    assert dst.output == "Successfully renamed /memories/user/suppliers.md to /memories/agent/suppliers.md"
    assert "suppliers.md" not in (personal / "MEMORY.md").read_text()
    assert "- suppliers.md: Suppliers (" in (shared / "MEMORY.md").read_text()


def test_promote_takes_a_new_name_and_refuses_a_taken_one(two_roots):
    personal, shared = two_roots
    with pytest.raises(MemoryOpError, match="The destination ops.md already exists"):
        memory_file.op_promote(personal, shared, "suppliers.md", "ops.md")
    assert (personal / "suppliers.md").exists()
    _src, dst = memory_file.op_promote(personal, shared, "suppliers.md", "vendors.md")
    assert dst.output.endswith("to /memories/agent/vendors.md")
    assert (shared / "vendors.md").exists()


def test_promote_refuses_a_missing_topic_a_folder_and_the_create_caps(two_roots):
    personal, shared = two_roots
    with pytest.raises(MemoryOpError, match="does not exist"):
        memory_file.op_promote(personal, shared, "nope.md", "nope.md")
    (personal / "folder").mkdir()
    with pytest.raises(MemoryOpError, match="Promote one topic file at a time"):
        memory_file.op_promote(personal, shared, "folder", "folder")
    _seed(personal, "big.md", _dated_lines(1800))
    with pytest.raises(MemoryCapError, match="growth cap"):
        memory_file.op_promote(personal, shared, "big.md", "big.md")
    assert (personal / "big.md").exists() and not (shared / "big.md").exists()


def test_promote_rolls_back_when_the_source_cannot_be_removed(two_roots, monkeypatch):
    from services.infra import safe_fs
    personal, shared = two_roots
    real = safe_fs.unlink_beneath
    calls = []

    def failing(root_dir, rel, **kw):
        calls.append(rel)
        if len(calls) == 1:
            raise OSError("busy")
        return real(root_dir, rel, **kw)
    monkeypatch.setattr(safe_fs, "unlink_beneath", failing)
    with pytest.raises(MemoryOpError, match="stays there. Nothing changed in /memories/agent/"):
        memory_file.op_promote(personal, shared, "suppliers.md", "suppliers.md")
    assert (personal / "suppliers.md").exists() and not (shared / "suppliers.md").exists()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# The edge cases: the reminder's span, the snippet, the sanitised insert,
# invalid dates, the rollback that fails
# ---------------------------------------------------------------------------

def test_a_newline_around_the_new_text_touches_no_neighbouring_line(root):
    _create(root, "m.md", "# M\n- a\n- b\n- d (2026-01-01)\n")
    r = memory_file.op_str_replace(root, "m.md", "- b\n", "- c, undated\n")
    assert r.warnings == [memory_file.DATE_REMINDER]
    _create(root, "n.md", "# N\n- a (2026-01-01)\n- b\n")
    r = memory_file.op_str_replace(root, "n.md", "\n- b", "\n- c undated")
    assert r.warnings == [memory_file.DATE_REMINDER]
    r = memory_file.op_str_replace(root, "n.md", "\n- c undated", "\n")
    assert r.warnings == []


def test_the_replace_snippet_never_numbers_a_line_past_the_end(root):
    _create(root, "t.md", "# T\n\na\nb\n")
    r = memory_file.op_str_replace(root, "t.md", "b", "c")
    numbered = [line.split("\t")[0].strip() for line in r.output.splitlines()[1:]]
    assert numbered == ["2", "3", "4"]  # two lines of context, never a fifth line


def test_an_insert_measures_growth_on_the_sanitised_text(root):
    _seed(root, "esc.md", "# E\n" + "\x1b[31mred\x1b[0m\n" * 1500)
    r = memory_file.op_insert(root, "esc.md", 1, "- note (2026-10-28)")
    assert "edited" in r.output
    assert "\x1b" not in (root / "esc.md").read_text()


def test_has_date_refuses_an_impossible_day_and_foreign_digits():
    assert memory_index.has_date("done 2026-10-28")
    assert not memory_index.has_date("2026-99-99")
    assert not memory_index.has_date("٢٠٢٦-١٠-٢٨")
    assert not memory_index.has_date("20261028")


def test_the_index_skips_rules_as_summary_and_entries_and_keeps_fence_markers_apart(root):
    _seed(root, "r.md", "# R\n---\n* * *\n```\n~~~\n- inside\n```\n- outside\n")
    f = memory_index.topic_facts(root / "r.md", root, today=date(2026, 10, 28))
    assert f.summary == "" and f.entries == 1


def test_promote_whose_source_vanished_keeps_the_copy(two_roots):
    from services.infra import safe_fs
    personal, shared = two_roots
    real = safe_fs.unlink_beneath

    def vanish(root_dir, rel, **kw):
        (personal / "suppliers.md").unlink()
        return real(root_dir, rel, **kw)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safe_fs, "unlink_beneath", vanish)
        _src, dst = memory_file.op_promote(personal, shared, "suppliers.md", "suppliers.md")
    assert dst.changed[0] == "suppliers.md" and (shared / "suppliers.md").exists()


def test_a_promote_whose_rollback_also_fails_says_both_paths(two_roots, monkeypatch, caplog):
    import logging

    from services.infra import safe_fs
    personal, shared = two_roots

    def failing(root_dir, rel, **kw):
        raise OSError("busy")
    monkeypatch.setattr(safe_fs, "unlink_beneath", failing)
    with caplog.at_level(logging.WARNING, logger="otodock.memory"), pytest.raises(
        MemoryOpError, match="a copy stayed at /memories/agent/suppliers.md. Delete one of the two.",
    ):
        memory_file.op_promote(personal, shared, "suppliers.md", "suppliers.md")
    assert "stayed in both scopes" in caplog.text


def test_a_giant_hand_dropped_topic_is_read_capped_and_sized_by_stat(root):
    body = ("# Giant\nA log dropped by hand.\n\n## Early\n- a (2026-10-01)\n"
            + ("- filler " + "x" * 80 + "\n") * 3000 + "## Late\n- b (2026-10-27)\n")
    _seed(root, "giant.md", body)
    assert len(body.encode()) > memory_index.TOPIC_READ_CAP_BYTES
    f = memory_index.topic_facts(root / "giant.md", root, today=date(2026, 10, 28))
    assert f.size == len(body.encode())
    assert f.heading == "Giant" and f.summary == "A log dropped by hand."
    assert [t for _n, t in f.sections] == ["Early"]
    assert f.newest == date(2026, 10, 1)
    index = memory_index.build_index_content(root, today=date(2026, 10, 28))
    assert "- giant.md: Giant (" in index and "Late" not in index
