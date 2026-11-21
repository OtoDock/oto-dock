"""``services.infra.file_bookkeeping`` — the tree-level tombstone / publish
helpers confine the subtree they walk to the agent dir before touching it,
so a path that resolves outside the agent (a planted symlink, a stray
absolute path) records and pushes nothing. And the delete's owner removes
the file a session's sandbox judged, or none: with the values the sandbox
saw, the look, the capture and the removal are made on that one file, in one
held folder.
"""

import asyncio
import os
import time

import pytest

from services.infra import file_bookkeeping as fb


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "pa"
    (d / "workspace" / "docs").mkdir(parents=True)
    (d / "workspace" / "docs" / "a.md").write_text("a")
    (d / "workspace" / "docs" / "b.md").write_text("b")
    return d


@pytest.fixture
def outside(tmp_path, agent_dir):
    """A dir outside the agent tree, also reachable through a symlink
    planted inside the workspace."""
    out = tmp_path / "outside"
    out.mkdir()
    (out / "x.md").write_text("x")
    os.symlink(out, agent_dir / "workspace" / "link")
    return out


def _capture(monkeypatch, name):
    seen = []

    async def _rec(*a, **k):
        seen.append(a)
    monkeypatch.setattr(fb, name, _rec)
    return seen


def test_tombstone_subtree_walks_only_inside_the_agent(agent_dir, outside, monkeypatch):
    seen = _capture(monkeypatch, "tombstone_path")
    asyncio.run(fb.tombstone_subtree("pa", agent_dir, agent_dir / "workspace" / "docs"))
    assert sorted(s[1] for s in seen) == ["workspace/docs/a.md", "workspace/docs/b.md"]

    seen.clear()
    asyncio.run(fb.tombstone_subtree("pa", agent_dir, agent_dir / "workspace" / "link"))
    asyncio.run(fb.tombstone_subtree("pa", agent_dir, outside))
    assert seen == []


def test_push_tree_write_records_only_inside_the_agent(agent_dir, outside, monkeypatch):
    seen = _capture(monkeypatch, "record_platform_write")
    from services.remote import workspace_fanout
    monkeypatch.setattr(workspace_fanout, "has_fanout_candidates", lambda *a, **k: False)
    asyncio.run(fb.push_tree_write(
        "pa", agent_dir / "workspace" / "docs", agent_dir, writer="alice"))
    assert sorted(s[1] for s in seen) == ["workspace/docs/a.md", "workspace/docs/b.md"]
    assert {s[2] for s in seen} == {"alice"}

    seen.clear()
    asyncio.run(fb.push_tree_write("pa", agent_dir / "workspace" / "link", agent_dir))
    asyncio.run(fb.push_tree_write("pa", outside, agent_dir))
    assert seen == []


# -- the delete's owner, with what a session's sandbox saw of the file ---------

@pytest.fixture
def owner(monkeypatch):
    """The owner's database steps stood in: what was captured and tombstoned."""
    from storage.files import recover_bin_store
    seen = {"captured": [], "tombstoned": []}
    monkeypatch.setattr(recover_bin_store, "capture",
                        lambda agent, rel, content, reason, **_k: seen["captured"].append((rel, content)))
    monkeypatch.setattr(fb, "tombstone_path_sync", lambda agent, rel, **_k: seen["tombstoned"].append(rel))
    return seen


def _saw(path):
    """What a session's sandbox would answer for ``path``."""
    st = os.lstat(path)
    return (st.st_size, st.st_ino, st.st_ctime_ns)


REL = "workspace/docs/a.md"


def test_the_owner_removes_the_file_the_sandbox_saw(agent_dir, owner):
    target = agent_dir / REL
    assert fb._delete_file_sync("pa", agent_dir, REL, _saw(target)) is False
    assert not target.exists() and (agent_dir / "workspace" / "docs" / "b.md").exists()
    assert owner == {"captured": [(REL, b"a")], "tombstoned": [REL]}


def test_the_owner_without_values_removes_the_name_as_before(agent_dir, owner):
    assert fb._delete_file_sync("pa", agent_dir, REL) is False
    assert not (agent_dir / REL).exists() and owner == {"captured": [(REL, b"a")], "tombstoned": [REL]}


@pytest.mark.parametrize("change", ["written in place", "moved over", "touched", "another size"])
def test_a_file_that_is_not_the_one_the_sandbox_saw_stays(agent_dir, owner, change):
    target = agent_dir / REL
    saw = _saw(target)
    # A file's change time moves in steps of the kernel's clock tick, a few
    # milliseconds: a change in the same tick as the look shows the same time.
    time.sleep(0.05)
    if change == "written in place":
        target.write_text("b")                          # the same size and inode, a later change time
    elif change == "moved over":
        (target.parent / "new").write_text("a")
        os.replace(target.parent / "new", target)       # another inode under the name
    elif change == "touched":
        os.chmod(target, 0o600)
    else:
        saw = (saw[0] + 1, saw[1], saw[2])
    with pytest.raises(FileNotFoundError):
        fb._delete_file_sync("pa", agent_dir, REL, saw)
    assert target.exists() and owner == {"captured": [], "tombstoned": []}


@pytest.mark.parametrize("cap", [1 << 20, 0])
def test_a_link_and_a_folder_at_the_name_are_never_removed(agent_dir, outside, owner, monkeypatch, cap):
    """Also when the Recover bin would take no copy of it: a file over the
    bin's cap is removed unread, and a link is never such a file."""
    import config
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", cap)
    link = agent_dir / "workspace" / "docs" / "ln.md"
    os.symlink(outside / "x.md", link)
    for rel in ("workspace/docs/ln.md", "workspace/docs", "workspace/link/x.md"):
        with pytest.raises(OSError):
            fb._delete_file_sync("pa", agent_dir, rel)
    assert link.is_symlink() and (outside / "x.md").read_text() == "x"
    assert owner == {"captured": [], "tombstoned": []}


def test_a_file_moved_over_the_name_after_the_look_is_neither_captured_nor_removed(agent_dir, owner, monkeypatch):
    from services.infra import safe_fs
    target = agent_dir / REL
    saw = _saw(target)
    real = safe_fs.open_regular_for_read

    def swapped(root, rel, **kwargs):
        (target.parent / "theirs").write_text("theirs")
        os.replace(target.parent / "theirs", target)
        return real(root, rel, **kwargs)

    monkeypatch.setattr(safe_fs, "open_regular_for_read", swapped)
    with pytest.raises(FileNotFoundError):
        fb._delete_file_sync("pa", agent_dir, REL, saw)
    assert target.read_text() == "theirs" and owner == {"captured": [], "tombstoned": []}


def test_a_file_moved_over_the_name_after_the_capture_is_not_removed(agent_dir, owner, monkeypatch):
    from storage.files import recover_bin_store
    target = agent_dir / REL
    saw = _saw(target)

    def captured_then_swapped(agent, rel, content, reason, **_k):
        owner["captured"].append((rel, content))
        (target.parent / "theirs").write_text("theirs")
        os.replace(target.parent / "theirs", target)

    monkeypatch.setattr(recover_bin_store, "capture", captured_then_swapped)
    with pytest.raises(FileNotFoundError):
        fb._delete_file_sync("pa", agent_dir, REL, saw)
    assert target.read_text() == "theirs" and owner["tombstoned"] == []


def test_the_look_the_capture_and_the_removal_are_made_in_one_held_folder(agent_dir, owner, monkeypatch):
    import config
    from services.infra import safe_fs
    # The file under the same path is over the bin's cap, the judged one is not: its copy is kept.
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 8)
    docs = agent_dir / "workspace" / "docs"
    saw = _saw(docs / "a.md")
    real = safe_fs.lstat_beneath
    moved: list = []

    def folder_swapped(root, rel):
        if not moved:
            moved.append(True)
            docs.rename(docs.parent / "aside")          # the folder is moved away under the owner
            docs.mkdir()
            (docs / "a.md").write_text("another file under the same path")
        return real(root, rel)

    monkeypatch.setattr(safe_fs, "lstat_beneath", folder_swapped)
    assert fb._delete_file_sync("pa", agent_dir, REL, saw) is False
    # The file that was judged is gone, from the folder that was opened. The other one stays.
    assert not (docs.parent / "aside" / "a.md").exists()
    assert (docs / "a.md").read_text() == "another file under the same path"
    assert owner == {"captured": [(REL, b"a")], "tombstoned": [REL]}


def test_a_file_over_the_bins_cap_is_removed_and_not_read(agent_dir, owner, monkeypatch):
    import config
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 0)
    target = agent_dir / REL
    assert fb._delete_file_sync("pa", agent_dir, REL, _saw(target)) is True
    assert not target.exists() and owner == {"captured": [], "tombstoned": [REL]}


def test_the_platform_delete_hands_the_values_on_and_finishes_its_record(agent_dir, owner, monkeypatch):
    done: list = []
    monkeypatch.setattr(fb, "schedule_library_projection", lambda agent, rel, *, deleted: done.append(("library", rel, deleted)))

    async def pushed(agent, rel):
        done.append(("push", rel))

    monkeypatch.setattr(fb, "push_file_delete", pushed)
    target = agent_dir / REL
    stale = (0, 0, 0)
    with pytest.raises(FileNotFoundError):
        asyncio.run(fb.delete_platform_file("pa", agent_dir, target, expect=stale))
    assert target.exists() and done == []
    assert asyncio.run(fb.delete_platform_file("pa", agent_dir, target, expect=_saw(target))) is False
    assert not target.exists() and done == [("library", REL, True), ("push", REL)]
