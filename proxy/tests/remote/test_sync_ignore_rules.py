"""Proxy-side sync-ignore rules (1.5): marker-confirmed generated-dir
exclusion in ``compute_manifest``, the per-machine version gate, and the
config table's own validity. Mirrors satellite/tests/test_sync_ignore_rules.py
— the fixture trees are the shared symmetry contract between the two walks.
"""


import config as app_config
from core.remote.file_sync import (
    compute_manifest,
    validate_ignore_rules,
)

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


def _mk_cargo_project(ws):
    proj = ws / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "Cargo.toml").write_text("[package]")
    (proj / "src" / "main.rs").write_text("fn main() {}")
    target = proj / "target"
    (target / "debug").mkdir(parents=True)
    (target / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG + b"\n# cargo")
    (target / "debug" / "dep.o").write_text("obj")
    return proj


def _paths(agent_dir, **kw):
    return {e.path for e in compute_manifest(agent_dir, **kw)}


def test_rules_exclude_target_and_none_is_legacy(tmp_path):
    ws = tmp_path / "workspace"
    _mk_cargo_project(ws)
    with_rules = _paths(tmp_path, ignore_rules=RULES)
    legacy = _paths(tmp_path, ignore_rules=None)
    assert "workspace/proj/Cargo.toml" in with_rules
    assert "workspace/proj/src/main.rs" in with_rules
    assert not any(p.startswith("workspace/proj/target/") for p in with_rules)
    assert any(p.startswith("workspace/proj/target/") for p in legacy)


def test_dir_merely_named_target_still_syncs(tmp_path):
    ws = tmp_path / "workspace"
    tgt = ws / "target"
    tgt.mkdir(parents=True)
    (tgt / "README.md").write_text("real project dir")
    assert "workspace/target/README.md" in _paths(tmp_path, ignore_rules=RULES)


def test_in_tree_build_vetoed(tmp_path):
    ws = tmp_path / "workspace"
    proj = ws / "cmproj"
    proj.mkdir(parents=True)
    (proj / "CMakeLists.txt").write_text("project(x)")
    (proj / "CMakeCache.txt").write_text("# in-tree build")
    (proj / "main.c").write_text("int main(){}")
    got = _paths(tmp_path, ignore_rules=RULES)
    assert "workspace/cmproj/main.c" in got
    assert "workspace/cmproj/CMakeCache.txt" in got


def test_out_of_tree_build_and_unsigned_tag(tmp_path):
    ws = tmp_path / "workspace"
    build = ws / "proj" / "mybuild"
    build.mkdir(parents=True)
    (ws / "proj" / "CMakeLists.txt").write_text("project(x)")
    (build / "CMakeCache.txt").write_text("# build tree")
    (build / "gen.o").write_text("obj")
    unsigned = ws / "cacheB"
    unsigned.mkdir(parents=True)
    (unsigned / "CACHEDIR.TAG").write_bytes(b"not the signature")
    (unsigned / "blob.bin").write_text("x")
    got = _paths(tmp_path, ignore_rules=RULES)
    assert not any(p.startswith("workspace/proj/mybuild/") for p in got)
    assert "workspace/cacheB/blob.bin" in got  # unsigned → syncs


def test_protected_depth_and_structural_names(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG)
    (ws / "notes.md").write_text("keep me")
    deep_ws = tmp_path / "users" / "alice" / "workspace"
    deep_ws.mkdir(parents=True)
    (deep_ws / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIG)
    (deep_ws / "keep.md").write_text("keep me too")
    got = _paths(tmp_path, ignore_rules=RULES)
    assert "workspace/notes.md" in got
    assert "users/alice/workspace/keep.md" in got


def test_config_table_is_valid_and_normalizes_to_itself():
    # Dev-typo guard: the shipped table must pass the same validation the
    # satellite applies, or the handshake degrades every machine to legacy.
    validated = validate_ignore_rules(app_config.SYNC_IGNORE_RULES)
    assert validated is not None
    assert validated["version"] == 1
    assert validated["named"], "shipped table lost its named rules"
    # Veto evidence must be syncable files — never `.git` (SKIP_DIRS): the
    # platform copy lacks it and the two walks would disagree (churn).
    assert ".git" not in validated["veto_files"]
    # Every named rule must carry sibling evidence (no bare-name rules).
    for rule in validated["named"]:
        assert rule["siblings"] or rule["sibling_globs"]


def test_validation_rejects_bad_tables():
    assert validate_ignore_rules({"version": 2}) is None
    assert validate_ignore_rules(
        {"version": 1, "named": [{"dir": "target"}]}) is None
    assert validate_ignore_rules(
        {"version": 1, "in_dir_markers": [{"file": "a/b"}]}) is None
    big = {"version": 1, "named": [
        {"dir": f"d{i}", "siblings": ["x"]} for i in range(200)]}
    assert validate_ignore_rules(big) is None


def test_version_gate_selects_rules_per_machine():
    from core.remote.satellite_connection import get_connection_manager

    cm = get_connection_manager()

    class _Conn:
        def __init__(self, v):
            self.satellite_version = v

    mid_old, mid_new = "gate-old-mid", "gate-new-mid"
    cm._connections[mid_old] = _Conn("0.5.109")
    cm._connections[mid_new] = _Conn("0.5.110")
    try:
        assert cm.satellite_supports_sync_ignore_rules(mid_new)
        assert not cm.satellite_supports_sync_ignore_rules(mid_old)
        assert cm.effective_ignore_rules(mid_old) is None
        rules = cm.effective_ignore_rules(mid_new)
        assert rules is not None and rules["version"] == 1
    finally:
        cm._connections.pop(mid_old, None)
        cm._connections.pop(mid_new, None)
