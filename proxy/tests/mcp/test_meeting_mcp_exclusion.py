"""Meeting-context MCP exclusion (`build_session_mcp_config(meeting_mode=True)`).

Meetings ride the task lane (task_mode=True), so "meeting" is matched as a
UNION with the base "task" context — never a replacement. Two invariants:

 - a manifest can opt out of meetings ALONE (exclude_from: ["meeting"]) while
   staying available to scheduled tasks;
 - manifests that exclude "task" remain absent from meetings, exactly as they
   were before the meeting context existed.

The prompt catalog and skills already filtered by client_type ("meeting"
there), so this is also the config/prompt-agreement seam: without the union a
meeting-excluded manifest would vanish from the prompt while its tools still
loaded.
"""

import sys

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from tests.mcp.test_mcp_broker_activation import (  # noqa: E402
    _FakeManifest, _stub_assembly,
)


def _manifest(name, exclude_from):
    fm = _FakeManifest(name)
    fm.exclude_from = list(exclude_from)
    return fm


def _build(monkeypatch, tmp_path, manifests, **kwargs):
    from services.mcp import mcp_registry
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path)
    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, **kwargs,
    )
    return excluded


def test_meeting_mode_excludes_meeting_listed_mcp(monkeypatch, tmp_path):
    excluded = _build(
        monkeypatch, tmp_path,
        [_manifest("config-clone", ["meeting"]), _manifest("neutral", [])],
        task_mode=True, meeting_mode=True,
    )
    assert excluded.get("config-clone") == "Excluded in meeting mode"
    assert "neutral" not in excluded


def test_task_mode_keeps_meeting_only_exclusion(monkeypatch, tmp_path):
    """The per-surface opt-out: ["meeting"] must NOT leak into plain tasks."""
    excluded = _build(
        monkeypatch, tmp_path,
        [_manifest("config-clone", ["meeting"])],
        task_mode=True,
    )
    assert "config-clone" not in excluded


def test_meeting_mode_is_a_union_not_a_replacement(monkeypatch, tmp_path):
    """Manifests excluding only "task" have ALWAYS been absent from meetings
    (meetings ride the task lane); the meeting context must not re-admit them."""
    excluded = _build(
        monkeypatch, tmp_path,
        [_manifest("task-excluded", ["task"])],
        task_mode=True, meeting_mode=True,
    )
    assert excluded.get("task-excluded") == "Excluded in task mode"


def test_dashboard_ignores_meeting_exclusion(monkeypatch, tmp_path):
    excluded = _build(
        monkeypatch, tmp_path,
        [_manifest("config-clone", ["meeting"])],
    )
    assert "config-clone" not in excluded


def test_shipped_manifests_opt_out_of_tasks_and_meetings():
    """agent-config-mcp + mcps-mcp: self-reconfiguration and marketplace
    browsing happen when a human is present and asking — never in a
    scheduled task or a meeting turn. agent-creator-mcp additionally
    excludes phone."""
    from services.mcp.mcp_manifest_parse import _parse_manifest

    root = PROXY_DIR.parent / "mcps" / "custom"
    assert _parse_manifest(root / "agent-config-mcp" / "manifest.json").exclude_from == ["task", "meeting"]
    assert _parse_manifest(root / "mcps-mcp" / "manifest.json").exclude_from == ["task", "meeting"]
    creator = _parse_manifest(root / "agent-creator-mcp" / "manifest.json")
    assert set(creator.exclude_from) == {"phone", "task", "meeting"}
