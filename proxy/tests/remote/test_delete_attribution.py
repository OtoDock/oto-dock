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
