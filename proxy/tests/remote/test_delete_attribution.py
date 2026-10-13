"""Out-of-turn satellite DELETE attribution in the 3-way merge.

When the satellite lacks a platform file it had CONVERGED on (base == platform
hash), its tree is still ALIVE (non-empty manifest), and the session may write the
path, the merge resolves `delete_platform` — propagate the satellite's out-of-turn
delete to the platform (B == P is the "the satellite HAD this file and removed it"
signal; no delete timestamp needed). Otherwise it RE-PUSHES: a new file (no
base), an edit-vs-delete (base != platform → edit wins), no write authority, or a
wiped/empty satellite (the wipe-guard — never mass-delete from absence; a partial
delete still leaves the tree non-empty, so it IS attributed — no volume heuristic).
"""

import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.file_sync import FileEntry, diff_manifests  # noqa: E402


def _act(plan, path):
    for a in plan.actions:
        if a.rel_path == path:
            return a
    return None


def _r(path, h, mtime=1.0):
    return {"path": path, "hash": h, "mtime": mtime}


# A second satellite file so the satellite tree is "alive" (non-empty manifest) —
# without it an empty manifest trips the wipe-guard.
_ALIVE = _r("workspace/keep.txt", "sha256:keep")


def test_converged_delete_with_live_tree_deletes_platform():
    local = [FileEntry("users/alice/workspace/gone.md", "sha256:v1", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    remote = [_ALIVE]  # gone.md absent on the satellite; tree alive
    base = {"users/alice/workspace/gone.md": ("sha256:v1", 1.0),
            "workspace/keep.txt": ("sha256:keep", 1.0)}
    plan = diff_manifests(local, remote, base=base,
                          target_role="editor", target_username="alice")
    a = _act(plan, "users/alice/workspace/gone.md")
    assert a is not None and a.op == "delete_platform"
    assert a.capture_side == "platform" and a.capture_reason == "deleted"
    assert a.clear_base is True


def test_partial_big_delete_still_attributed_no_volume_heuristic():
    # 3 of 4 converged files gone, tree still reports 1 → all 3 attributed as deletes
    # (a large partial delete, e.g. removing a pushed codebase, is NOT treated as a wipe).
    local = [FileEntry(f"workspace/repo/f{i}.py", f"sha256:{i}", 10, 1.0) for i in range(3)]
    local.append(FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0))
    base = {f"workspace/repo/f{i}.py": (f"sha256:{i}", 1.0) for i in range(3)}
    plan = diff_manifests(local, [_ALIVE], base=base, target_role="editor")
    for i in range(3):
        a = _act(plan, f"workspace/repo/f{i}.py")
        assert a is not None and a.op == "delete_platform"


def test_empty_manifest_is_wipe_guard_repush():
    # Converged delete BUT the satellite reports an EMPTY manifest (wipe /
    # delete-everything) → never mass-delete; re-push.
    local = [FileEntry("users/alice/workspace/gone.md", "sha256:v1", 10, 1.0)]
    base = {"users/alice/workspace/gone.md": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [], base=base,
                          target_role="editor", target_username="alice")
    a = _act(plan, "users/alice/workspace/gone.md")
    assert a is not None and a.op == "push"


def test_edit_vs_delete_keeps_platform_edit():
    # Platform changed since base (base != platform) + satellite lacks it → edit wins → push.
    local = [FileEntry("workspace/doc.md", "sha256:edited", 10, 2.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"workspace/doc.md": ("sha256:OLD", 1.0)}
    plan = diff_manifests(local, [_ALIVE], base=base, target_role="editor")
    a = _act(plan, "workspace/doc.md")
    assert a is not None and a.op == "push"


def test_new_platform_file_no_base_pushes():
    local = [FileEntry("workspace/new.md", "sha256:new", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    plan = diff_manifests(local, [_ALIVE], base={}, target_role="editor")
    a = _act(plan, "workspace/new.md")
    assert a is not None and a.op == "push"


def test_no_write_authority_repushes_instead_of_deleting():
    # A viewer "deleted" a SHARED workspace file (can't write it back) → re-push, not delete.
    local = [FileEntry("workspace/shared.md", "sha256:v1", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"workspace/shared.md": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [_ALIVE], base=base,
                          target_role="viewer", target_username="alice")
    a = _act(plan, "workspace/shared.md")
    assert a is not None and a.op == "push"


def test_viewer_can_delete_own_user_dir():
    # A viewer CAN write their own users/<self>/ dir → a converged delete there propagates.
    local = [FileEntry("users/alice/workspace/mine.md", "sha256:v1", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"users/alice/workspace/mine.md": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [_ALIVE], base=base,
                          target_role="viewer", target_username="alice")
    a = _act(plan, "users/alice/workspace/mine.md")
    assert a is not None and a.op == "delete_platform"


def test_config_never_delete_attributed_from_absence():
    # Even a MANAGER with full config write-back: a config/ file absent on the
    # satellite is RE-PUSHED, never delete-attributed — the blast radius of a
    # wrong inference is the agent's prompt (the Jul 2026 incident deleted
    # config/prompt.md this way). Deliberate config deletes travel as live
    # file_changed frames during a session, not absence at initial sync.
    local = [FileEntry("config/prompt.md", "sha256:v1", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"config/prompt.md": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [_ALIVE], base=base,
                          target_role="manager", target_username="alice")
    a = _act(plan, "config/prompt.md")
    assert a is not None and a.op == "push"


def test_knowledge_delete_attribution_unchanged():
    # The config/ guard is deliberately NARROW: knowledge/ (also owner-tier)
    # still delete-attributes on converged absence.
    local = [FileEntry("knowledge/old-doc.md", "sha256:v1", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"knowledge/old-doc.md": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [_ALIVE], base=base,
                          target_role="manager", target_username="alice")
    a = _act(plan, "knowledge/old-doc.md")
    assert a is not None and a.op == "delete_platform"


# --- What a 0.5.145 satellite could not read (``skipped`` entries) ---------

def _skip(path, why="unreadable", **extra):
    return {"path": path, "skipped": why, **extra}


def test_a_file_the_satellite_could_not_read_is_left_alone():
    # A locked .pst (B == P, write authority): without the skipped entry it
    # would read as deleted; with it, nothing is done to it either way.
    local = [FileEntry("workspace/mail.pst", "sha256:v1", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"workspace/mail.pst": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [_ALIVE, _skip("workspace/mail.pst")], base=base,
                          target_role="editor")
    assert _act(plan, "workspace/mail.pst") is None
    plan = diff_manifests(local, [_ALIVE, _skip("workspace/mail.pst", "too_large")],
                          base=base, target_role="editor")
    assert _act(plan, "workspace/mail.pst") is None


def test_files_under_a_folder_the_satellite_could_not_list_are_left_alone():
    local = [FileEntry("workspace/build/out.txt", "sha256:o", 10, 1.0),
             FileEntry("workspace/build/deep/x.txt", "sha256:x", 10, 1.0),
             FileEntry("workspace/buildings.txt", "sha256:b", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {e.path: (e.hash, 1.0) for e in local}
    remote = [_ALIVE, _skip("workspace/build", "unreadable_dir", dir=True)]
    plan = diff_manifests(local, remote, base=base, target_role="editor")
    assert _act(plan, "workspace/build/out.txt") is None
    assert _act(plan, "workspace/build/deep/x.txt") is None
    # A sibling sharing the prefix text is not under the folder.
    a = _act(plan, "workspace/buildings.txt")
    assert a is not None and a.op == "delete_platform"


def test_a_tree_the_satellite_could_not_list_at_all_gets_no_action():
    local = [FileEntry("workspace/a.txt", "sha256:a", 10, 1.0),
             FileEntry("config/prompt.md", "sha256:p", 10, 1.0)]
    base = {"workspace/a.txt": ("sha256:a", 1.0)}
    plan = diff_manifests(local, [_skip("", "unreadable_dir", dir=True)], base=base,
                          tombstones={"workspace/gone.txt": 5.0}, target_role="manager",
                          target_username="alice")
    assert plan.actions == [] and plan.to_scrub == []


def test_skipped_entries_alone_do_not_make_the_tree_alive():
    # Only unreadable files listed: still an "empty" tree for the wipe guard,
    # so a converged file missing from it is pushed, never deleted.
    local = [FileEntry("workspace/gone.md", "sha256:v1", 10, 1.0)]
    base = {"workspace/gone.md": ("sha256:v1", 1.0)}
    plan = diff_manifests(local, [_skip("workspace/locked.docx")], base=base,
                          target_role="editor")
    a = _act(plan, "workspace/gone.md")
    assert a is not None and a.op == "push"


def test_a_skipped_push_only_path_is_not_pushed_over():
    local = [FileEntry(".claude/settings.json", "sha256:p", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    plan = diff_manifests(local, [_ALIVE, _skip(".claude/settings.json")],
                          target_role="editor")
    assert _act(plan, ".claude/settings.json") is None


# --- An old Windows satellite (absence_unknown) ----------------------------

def test_an_old_windows_satellite_never_loses_a_file_to_its_absence():
    # B == P (a delete_platform before) and B None (the bug cleared the base
    # of what it deleted, the machine still has it): no action on either.
    local = [FileEntry("workspace/converged.md", "sha256:v1", 10, 1.0),
             FileEntry("workspace/rebased.md", "sha256:v2", 10, 1.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    base = {"workspace/converged.md": ("sha256:v1", 1.0),
            "workspace/keep.txt": ("sha256:keep", 1.0)}
    plan = diff_manifests(local, [_ALIVE], base=base, target_role="editor",
                          absence_unknown=True)
    assert _act(plan, "workspace/converged.md") is None
    assert _act(plan, "workspace/rebased.md") is None
    # What it did list merges as usual.
    new = _r("workspace/made-there.md", "sha256:n")
    plan = diff_manifests(local, [_ALIVE, new], base=base, target_role="editor",
                          absence_unknown=True)
    a = _act(plan, "workspace/made-there.md")
    assert a is not None and a.op == "pull"


# --- Holds: the Windows hash bug's deletes ----------------------------------

def test_a_held_file_is_restored_only_by_the_windows_machine():
    remote = [_ALIVE, _r("workspace/report.docx", "sha256:win-edit", mtime=3.0)]
    local = [FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    holds = {"workspace/report.docx": 2.0}
    windows = diff_manifests(local, remote, holds=holds, restore_holds=True,
                             target_role="editor")
    a = _act(windows, "workspace/report.docx")
    assert a is not None and a.op == "pull" and a.base_hash == "sha256:win-edit"
    assert a.drop_tombstone is True
    # The restored copy keeps the machine's own time (clock-adjusted), so a
    # newer copy elsewhere still wins the next divergence.
    timed = diff_manifests(local, remote, holds=holds, restore_holds=True,
                           clock_offset=10.0, target_role="editor")
    assert _act(timed, "workspace/report.docx").keep_mtime == 13.0
    assert a.keep_mtime is None             # no clock offset known: nothing to keep
    # Any other machine (an idle Linux one, or the Windows one before its
    # update): neither a pull nor a delete, newer or older than the hold.
    for mtime in (1.0, 9.0):
        remote = [_ALIVE, _r("workspace/report.docx", "sha256:old", mtime=mtime)]
        other = diff_manifests(local, remote, holds=holds, target_role="editor")
        assert _act(other, "workspace/report.docx") is None


def test_a_held_file_without_write_authority_is_not_restored():
    remote = [_ALIVE, _r("workspace/report.docx", "sha256:win-edit")]
    local = [FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    plan = diff_manifests(local, remote, holds={"workspace/report.docx": 2.0},
                          restore_holds=True, target_role="viewer",
                          target_username="alice")
    assert _act(plan, "workspace/report.docx") is None


def test_a_held_path_the_platform_has_again_merges_and_drops_the_hold():
    local = [FileEntry("workspace/report.docx", "sha256:v2", 10, 5.0),
             FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)]
    plan = diff_manifests(local, [_ALIVE, _r("workspace/report.docx", "sha256:v2")],
                          holds={"workspace/report.docx": 2.0}, target_role="editor")
    a = _act(plan, "workspace/report.docx")
    assert a is not None and a.op == "noop" and a.drop_tombstone is True


def test_the_restore_race_keeps_the_windows_edit():
    """The audit's race: an idle Linux machine still holds the old bytes
    (B == S there). It merges first and does nothing; the updated Windows
    machine restores its newer edit; the Linux machine then gets the
    restored copy pushed, its old bytes never reaching the platform."""
    path = "users/alice/workspace/notes.md"
    holds = {path: 2.0}
    keep = FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)
    linux_remote = [_ALIVE, _r(path, "sha256:old", mtime=1.0)]
    linux_base = {path: ("sha256:old", 1.0)}
    first = diff_manifests([keep], linux_remote, base=linux_base, holds=holds,
                           target_role="editor", target_username="alice")
    assert _act(first, path) is None
    win_remote = [_ALIVE, _r(path, "sha256:win-edit", mtime=1.5)]
    restore = diff_manifests([keep], win_remote, holds=holds, restore_holds=True,
                             target_role="editor", target_username="alice")
    assert _act(restore, path).op == "pull"
    after = [keep, FileEntry(path, "sha256:win-edit", 10, 9.0)]
    later = diff_manifests(after, linux_remote, base=linux_base,
                           target_role="editor", target_username="alice")
    a = _act(later, path)
    assert a is not None and a.op == "push" and a.base_hash == "sha256:win-edit"


def test_a_restore_never_beats_a_newer_copy_on_another_windows_machine():
    """Two Windows machines of one person: the desktop restores its older
    copy (keeping its mtime), the laptop holds the newer edit and no base
    (the bug's delete cleared it): the laptop's edit wins and is pulled."""
    path = "users/alice/workspace/notes.md"
    keep = FileEntry("workspace/keep.txt", "sha256:keep", 10, 1.0)
    desktop = diff_manifests([keep], [_ALIVE, _r(path, "sha256:old", mtime=100.0)],
                             holds={path: 150.0}, restore_holds=True, clock_offset=0.0,
                             target_role="editor", target_username="alice")
    restored = _act(desktop, path)
    assert restored.op == "pull" and restored.keep_mtime == 100.0
    after = [keep, FileEntry(path, "sha256:old", 10, restored.keep_mtime)]
    laptop = diff_manifests(after, [_ALIVE, _r(path, "sha256:newest", mtime=200.0)],
                            clock_offset=0.0, target_role="editor", target_username="alice")
    a = _act(laptop, path)
    assert a is not None and a.op == "pull" and a.base_hash == "sha256:newest"
