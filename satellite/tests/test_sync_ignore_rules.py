"""Sync-ignore rules: marker-confirmed generated-dir exclusion (0.5.110, 1.5).

The proxy ships a declarative rule table in the ``auth_result`` policy
handshake; every satellite sync surface (snapshot / fingerprint /
request-manifest / per-turn detect) excludes a dir iff a rule MATCHES it
(in-dir marker, or exact dir name + sibling manifest in the parent) and no
source-manifest VETO applies. Zero false positives is the design goal: every
ambiguity fails toward syncing. Mirrors proxy/core/remote/file_sync.py —
fixture trees here are the shared symmetry contract.
"""

import pytest

from satellite.host import satellite_policy
from satellite.transport.file_sync import (
    compute_fingerprint,
    compute_manifest,
    detect_changes,
    snapshot_agent_dir,
    validate_ignore_rules,
)

# A v1 table shaped like config.SYNC_IGNORE_RULES (subset — the mechanics,
# not the full catalog).
RULES = {
    "version": 1,
    "in_dir_markers": [
        {"file": "CACHEDIR.TAG", "signed": True},
        {"file": "CMakeCache.txt"},
        {"dir": "meson-info"},
    ],
    "named": [
        {"dir": "target", "siblings": ["Cargo.toml", "pom.xml"]},
        {"dir": "obj", "sibling_globs": ["*.csproj"]},
    ],
    "veto_files": ["CMakeLists.txt", "package.json"],
    "veto_globs": ["*.csproj"],
}

CACHEDIR_SIG = b"Signature: 8a477f597d28d172789f06886806bc55"


@pytest.fixture(autouse=True)
def _reset_policy():
    orig = dict(satellite_policy._state)
    yield
    with satellite_policy._lock:
        satellite_policy._state.clear()
        satellite_policy._state.update(orig)


def _apply_rules(rules=RULES):
    satellite_policy.set_policy({"sync_ignore_rules": rules})


def _mk_cargo_project(ws):
    """workspace/proj: Cargo.toml + src/main.rs + target/{CACHEDIR.TAG,dep.o}."""
    proj = ws / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "Cargo.toml").write_text("[package]")
    (proj / "src" / "main.rs").write_text("fn main() {}")
    target = proj / "target"
    (target / "debug").mkdir(parents=True)
    (target / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG + b"\n# cargo")
    (target / "debug" / "dep.o").write_text("obj")
    return proj


def test_named_rule_with_sibling_excludes_target(tmp_path):
    ws = tmp_path / "workspace"
    _mk_cargo_project(ws)
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert "workspace/proj/Cargo.toml" in snap
    assert "workspace/proj/src/main.rs" in snap
    assert not any(p.startswith("workspace/proj/target/") for p in snap)
    # All three surfaces agree (manifest + fingerprint share the walker).
    manifest_paths = {e["path"] for e in compute_manifest(tmp_path)}
    assert manifest_paths == set(snap)


def test_dir_merely_named_target_still_syncs(tmp_path):
    # The satellite/bin lesson: a SOURCE dir with a build-dir name and no
    # marker/sibling evidence must keep syncing (zero false positives).
    ws = tmp_path / "workspace"
    tgt = ws / "target"
    tgt.mkdir(parents=True)
    (tgt / "README.md").write_text("this is a real project dir")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert "workspace/target/README.md" in snap


def test_cachedir_tag_requires_signature(tmp_path):
    ws = tmp_path / "workspace"
    good = ws / "cacheA"
    bad = ws / "cacheB"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG)
    (good / "blob.bin").write_text("x")
    (bad / "CACHEDIR.TAG").write_bytes(b"not the signature")
    (bad / "blob.bin").write_text("x")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert not any(p.startswith("workspace/cacheA/") for p in snap)
    # Unsigned tag is NOT a marker — fail toward syncing (the tag file
    # itself syncs too).
    assert "workspace/cacheB/blob.bin" in snap
    assert "workspace/cacheB/CACHEDIR.TAG" in snap


def test_in_tree_build_vetoed_by_source_manifest(tmp_path):
    # `cmake .` in a source root drops CMakeCache.txt NEXT TO CMakeLists.txt.
    # Without the veto the whole project would silently stop syncing.
    ws = tmp_path / "workspace"
    proj = ws / "cmproj"
    proj.mkdir(parents=True)
    (proj / "CMakeLists.txt").write_text("project(x)")
    (proj / "CMakeCache.txt").write_text("# in-tree build")
    (proj / "main.c").write_text("int main(){}")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert "workspace/cmproj/main.c" in snap
    assert "workspace/cmproj/CMakeCache.txt" in snap  # syncs — mixed dir


def test_out_of_tree_cmake_build_excluded(tmp_path):
    ws = tmp_path / "workspace"
    build = ws / "proj" / "mybuild"
    build.mkdir(parents=True)
    (ws / "proj" / "CMakeLists.txt").write_text("project(x)")
    (build / "CMakeCache.txt").write_text("# build tree")
    (build / "gen.o").write_text("obj")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert "workspace/proj/CMakeLists.txt" in snap
    assert not any(p.startswith("workspace/proj/mybuild/") for p in snap)


def test_meson_info_dir_marker(tmp_path):
    ws = tmp_path / "workspace"
    bld = ws / "proj" / "builddir"
    (bld / "meson-info").mkdir(parents=True)
    (bld / "build.ninja").write_text("ninja")
    (ws / "proj" / "meson.build").write_text("project('x')")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert not any(p.startswith("workspace/proj/builddir/") for p in snap)
    assert "workspace/proj/meson.build" in snap


def test_obj_glob_sibling_and_csproj_veto(tmp_path):
    ws = tmp_path / "workspace"
    proj = ws / "app"
    obj = proj / "obj"
    obj.mkdir(parents=True)
    (proj / "App.csproj").write_text("<Project/>")
    (obj / "App.dll").write_text("bin")
    # A dir that MATCHES but itself contains a project file is vetoed:
    weird = ws / "tools" / "obj"
    weird.mkdir(parents=True)
    (ws / "tools" / "Tools.csproj").write_text("<Project/>")
    (weird / "Inner.csproj").write_text("<Project/>")  # veto_globs *.csproj
    (weird / "src.cs").write_text("class C {}")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert not any(p.startswith("workspace/app/obj/") for p in snap)
    assert "workspace/tools/obj/src.cs" in snap  # vetoed → syncs


def test_protected_depth_and_names(tmp_path):
    # Depth ≤ 1 and structural names never match — a stray CACHEDIR.TAG in
    # workspace/ must not desync the whole agent.
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG)
    (ws / "notes.md").write_text("keep me")
    deep_ws = tmp_path / "users" / "alice" / "workspace"
    deep_ws.mkdir(parents=True)
    (deep_ws / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG)
    (deep_ws / "keep.md").write_text("keep me too")
    _apply_rules()
    snap = snapshot_agent_dir(tmp_path)
    assert "workspace/notes.md" in snap
    assert "users/alice/workspace/keep.md" in snap


def test_no_policy_means_legacy_walk(tmp_path):
    ws = tmp_path / "workspace"
    _mk_cargo_project(ws)
    # No sync_ignore_rules in policy → the tree syncs exactly as before.
    snap = snapshot_agent_dir(tmp_path)
    assert any(p.startswith("workspace/proj/target/") for p in snap)


def test_fingerprint_flips_with_rules(tmp_path):
    ws = tmp_path / "workspace"
    _mk_cargo_project(ws)
    fp_legacy = compute_fingerprint(tmp_path)
    _apply_rules()
    fp_rules = compute_fingerprint(tmp_path)
    assert fp_legacy != fp_rules  # excluded tree left the fingerprint
    # Stable under rules: writes INSIDE the excluded tree don't flip it.
    (ws / "proj" / "target" / "debug" / "new.o").write_text("obj")
    assert compute_fingerprint(tmp_path) == fp_rules


def test_mid_session_rules_never_emit_deletes(tmp_path):
    # THE audit must-fix: a live session's baseline predates the rules
    # (proxy restart re-auths the WS mid-session). Newly-excluded paths must
    # be purged from the baseline SILENTLY — an emitted delete would remove
    # platform copies AND record tombstones that could later delete real
    # satellite files if the rule is retracted.
    ws = tmp_path / "workspace"
    proj = _mk_cargo_project(ws)
    snapshot = snapshot_agent_dir(tmp_path)  # legacy baseline incl. target/
    assert any(p.startswith("workspace/proj/target/") for p in snapshot)
    _apply_rules()  # rules arrive mid-session
    (proj / "src" / "lib.rs").write_text("pub fn f() {}")  # normal turn work
    changes = detect_changes(tmp_path, snapshot)
    actions = {(c["path"], c["action"]) for c in changes}
    assert ("workspace/proj/src/lib.rs", "write") in actions
    assert not any(a == "delete" for _, a in actions)
    # Baseline purged — the next turn reports nothing for the tree either.
    assert not any(p.startswith("workspace/proj/target/") for p in snapshot)
    assert detect_changes(tmp_path, snapshot) == []


def test_real_delete_still_reported_under_rules(tmp_path):
    ws = tmp_path / "workspace"
    proj = _mk_cargo_project(ws)
    _apply_rules()
    snapshot = snapshot_agent_dir(tmp_path)
    (proj / "src" / "main.rs").unlink()
    changes = detect_changes(tmp_path, snapshot)
    assert {("workspace/proj/src/main.rs", "delete")} == {
        (c["path"], c["action"]) for c in changes}


def test_validation_rejects_bad_tables():
    assert validate_ignore_rules(None) is None
    assert validate_ignore_rules({"version": 2}) is None
    assert validate_ignore_rules({"version": 1, "named": [
        {"dir": "target"}]}) is None  # bare-name rule (no sibling) refused
    assert validate_ignore_rules({"version": 1, "named": [
        {"dir": "a/b", "siblings": ["x"]}]}) is None  # path separators
    big = {"version": 1, "named": [
        {"dir": f"d{i}", "siblings": ["x"]} for i in range(200)]}
    assert validate_ignore_rules(big) is None  # over cap
    ok = validate_ignore_rules(RULES)
    assert ok is not None and len(ok["named"]) == 2


def test_policy_none_clears_and_malformed_clears():
    satellite_policy.set_policy({"sync_ignore_rules": RULES})
    assert "sync_ignore_rules" in satellite_policy.get_policy()
    # Auth from a downgraded proxy passes None explicitly → cleared.
    satellite_policy.set_policy({"sync_ignore_rules": None})
    assert "sync_ignore_rules" not in satellite_policy.get_policy()
    satellite_policy.set_policy({"sync_ignore_rules": RULES})
    satellite_policy.set_policy({"sync_ignore_rules": {"version": 99}})
    assert "sync_ignore_rules" not in satellite_policy.get_policy()
    # A partial policy_update WITHOUT the key retains the table.
    satellite_policy.set_policy({"sync_ignore_rules": RULES})
    satellite_policy.set_policy({"allow_full_fs": True})
    assert "sync_ignore_rules" in satellite_policy.get_policy()


def test_matcher_cache_swaps_on_table_change(tmp_path):
    ws = tmp_path / "workspace"
    _mk_cargo_project(ws)
    _apply_rules()
    assert not any(p.startswith("workspace/proj/target/")
                   for p in snapshot_agent_dir(tmp_path))
    # New table without the target rule → tree syncs again (cache swapped).
    slim = {"version": 1, "in_dir_markers": [], "named": [
        {"dir": "obj", "sibling_globs": ["*.csproj"]}], "veto_files": [],
        "veto_globs": []}
    _apply_rules(slim)
    snap2 = snapshot_agent_dir(tmp_path)
    # CACHEDIR.TAG marker gone from the slim table → target/ visible again.
    assert any(p.startswith("workspace/proj/target/") for p in snap2)
