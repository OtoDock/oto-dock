"""The file-sync constant blocks against the engine descriptors — and against
each other across the two trees (engine-contract lane, phase 4).

Both ``core/remote/file_sync.py`` and ``satellite/transport/file_sync.py``
hand-keep the engine dirs the sync whitelists, the host-local files that
never sync and the runtime dirs that are pruned. They are deliberately NOT
derived from the descriptors at runtime: the table decides delete
attribution, and a proxy walk that includes a dir an un-updated satellite
prunes converges once, records the base, and then deletes the platform copy
on the satellite's next manifest. So the constants stay the walk for every
satellite, and THIS test pins them to the descriptors and to each other — a
fourth engine fails here, in both trees, until it extends the blocks.
"""

import ast
import sys
from pathlib import Path

from core.remote import file_sync as proxy_fs
from core.session.session_manager import get_all_layers

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SATELLITE_FILE_SYNC = _REPO_ROOT / "satellite" / "transport" / "file_sync.py"


def _satellite_constant(name: str):
    """A module-level constant of the satellite's file_sync, read by AST: a
    set / tuple display, or ``frozenset({...})``."""
    tree = ast.parse(_SATELLITE_FILE_SYNC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Call):        # frozenset({...})
                value = value.args[0]
            return ast.literal_eval(value)
    raise AssertionError(f"satellite file_sync has no {name}")


def _engine_dirs() -> set[str]:
    return {
        layer.capabilities.runtime.config_dir_name
        for layer in get_all_layers().values()
        if layer.capabilities.runtime.config_dir_name
    }


def test_the_synced_dotted_dirs_are_exactly_the_engines_config_dirs():
    assert set(proxy_fs.INCLUDE_DOTTED_DIRS) == _engine_dirs()


def test_every_engine_config_dir_is_push_only_and_may_own_satellite_state():
    # A dir the proxy walks but does not route push-only reaches the 3-way
    # merge, where a converged-then-absent file delete-attributes the
    # platform copy — the data-loss shape the constants exist to prevent.
    assert set(proxy_fs.DEFAULT_PUSH_ONLY_SEGMENTS) >= set(proxy_fs.INCLUDE_DOTTED_DIRS)
    assert {s[0] for s in proxy_fs.SATELLITE_OWNED_SUBPATHS} <= set(proxy_fs.INCLUDE_DOTTED_DIRS)


def test_each_engines_credential_file_is_host_local_under_its_dir():
    # The credential file is written by the platform on each host and
    # rewritten on rotation — never synced. Each engine's set is its own.
    host_local = {
        ".claude": proxy_fs._CLAUDE_HOST_LOCAL_FILES,
        ".codex": proxy_fs._CODEX_HOST_LOCAL_FILES,
    }
    for path, layer in get_all_layers().items():
        spec = layer.capabilities.auth.credential_file
        if spec is None:
            continue
        assert spec.dirname in host_local, path
        assert spec.filename in host_local[spec.dirname], (path, spec.filename)
        assert proxy_fs._is_claude_runtime_state(f"users/u/{spec.dirname}/{spec.filename}") or \
            proxy_fs._is_codex_runtime_state(f"users/u/{spec.dirname}/{spec.filename}"), path


def test_the_two_trees_walk_with_identical_blocks():
    # The satellite manifest and the proxy manifest must agree or the diff
    # misattributes whole trees as deletes.
    assert _satellite_constant("INCLUDE_DOTTED_DIRS") == set(proxy_fs.INCLUDE_DOTTED_DIRS)
    assert _satellite_constant("_CLI_RUNTIME_CHILD_DIRS") == set(proxy_fs._CLI_RUNTIME_CHILD_DIRS)
    assert _satellite_constant("_CLAUDE_HOST_LOCAL_FILES") == set(proxy_fs._CLAUDE_HOST_LOCAL_FILES)
    assert _satellite_constant("_CODEX_HOST_LOCAL_FILES") == set(proxy_fs._CODEX_HOST_LOCAL_FILES)
    assert tuple(_satellite_constant("_CODEX_RUNTIME_GLOBS")) == tuple(proxy_fs._CODEX_RUNTIME_GLOBS)
    # The per-session model catalog joined the Codex set with satellite 0.5.117.
    assert "models.json" in proxy_fs._CODEX_HOST_LOCAL_FILES
    assert proxy_fs._is_codex_runtime_state("workspace/.codex/models.json")
    assert not proxy_fs._is_codex_runtime_state("workspace/models.json")


def test_the_per_session_mcp_config_copies_are_host_local_in_both_trees():
    # A session's MCP config copy carries its own session token and broker
    # fetch tokens: the satellite's ``mcp-config-<sid12>.json`` (0.5.132+),
    # the proxy's ``<agent>-<sha256(sub)[:12]>-<sid12>.json``
    # (``session_config_dir._session_copy_path``) and the shared names an
    # earlier release wrote never sync, either direction.
    for name in ("mcp-config.json", "mcp-config-1a2b3c4d-5e6.json",
                 "personal-assistant-0123456789ab-1a2b3c4d-5e6.json",
                 "personal-assistant-0123456789ab.json"):
        assert proxy_fs._is_claude_runtime_state(f"users/u/.claude/{name}"), name
        assert proxy_fs._is_claude_runtime_state(f"workspace/.claude/{name}"), name
    for name in ("notes.json", "settings.local.json", "mcp-config.txt"):
        assert not proxy_fs._is_claude_runtime_state(f"users/u/.claude/{name}"), name
    assert not proxy_fs._is_claude_runtime_state(
        "users/u/.claude/projects/h/mcp-config-1a2b3c4d-5e6.json")
    assert _satellite_constant("_CLAUDE_HOST_LOCAL_RE") == proxy_fs._CLAUDE_HOST_LOCAL_RE.pattern


def test_the_runtime_helpers_key_on_the_one_whitelist():
    # The cruft-dir and cruft-file helpers answer for every synced engine dir
    # (they used to spell the pair themselves).
    for d in proxy_fs.INCLUDE_DOTTED_DIRS:
        assert proxy_fs._is_cli_runtime_child(d, "projects")
        assert proxy_fs._is_cli_runtime_cruft_file(d, "x.backup.1")
        assert proxy_fs.is_engine_machinery_path(f"users/u/{d}/settings.json")
    assert not proxy_fs._is_cli_runtime_child("workspace", "projects")
    assert proxy_fs.is_engine_machinery_path("users/u/.credentials/x.json")
    assert not proxy_fs.is_engine_machinery_path("workspace/notes.md")
