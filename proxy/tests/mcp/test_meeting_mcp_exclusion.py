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

import json
import re
import sys
from types import SimpleNamespace

import pytest

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


# ---------------------------------------------------------------------------
# Forward direction of the agreement seam: the prompt may only advertise
# servers (catalog rows + inline skills) that the session config loads.
# ---------------------------------------------------------------------------


def _catalog_manifest(name, exclude_from=(), *, server_name=None, skill=None, tmp_path=None):
    """A manifest that renders in the prompt catalog: label/description for
    the row, an optional ``server_name`` (the mcpServers key when set) and an
    optional inline skill with its own ``exclude_from``."""
    fm = _manifest(name, exclude_from)
    fm.server_name = server_name
    fm.category = "core"
    fm.description = f"{name} tools."
    if skill is not None:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# skill\n")
        fm.mcp_dir = skill_dir
        fm.skills = [SimpleNamespace(
            id=f"{name}-skill", file="SKILL.md",
            default_exclude_from=list(skill), loading="always",
        )]
    return fm


_SESSION_CONTEXTS = [
    ("dashboard", {}),
    ("task", {"task_mode": True}),
    ("meeting", {"task_mode": True, "meeting_mode": True}),
    ("phone", {"phone_mode": True}),
    ("terminal", {"interactive_local": True}),
]


@pytest.mark.parametrize("context,kwargs", _SESSION_CONTEXTS)
def test_prompt_only_advertises_servers_the_config_loads(monkeypatch, tmp_path, context, kwargs):
    """Every server the prompt catalog names, and the owner of every inline
    skill, must be a key of the mcpServers config built for the same session
    — an advertised server the CLI cannot load is a tool the agent is told
    it has. The reverse also holds except for the deliberate catalog
    suppression of the meta MCP."""
    from services.mcp import mcp_registry

    manifests = [
        _catalog_manifest("always-on", [], skill=[], tmp_path=tmp_path),
        _catalog_manifest(
            "display-clone", ["phone", "terminal"], server_name="display",
            skill=["phone", "terminal"], tmp_path=tmp_path,
        ),
        _catalog_manifest("config-clone", ["task", "meeting"]),
        _catalog_manifest("creator-clone", ["phone", "task", "meeting"]),
        _catalog_manifest("meeting-shy", ["meeting"]),
        _catalog_manifest("mcps-mcp", []),
    ]
    by_name = {m.name: m for m in manifests}
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: list(manifests))

    path, _env, _excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, **kwargs,
    )
    loaded = set(json.loads(path.read_text())["mcpServers"])

    catalog = mcp_registry.build_available_mcps_section("agent", context=context)
    advertised = set(re.findall(r"\(`([^`]+)`\)", catalog))
    skill_owners = {
        skill_id.removesuffix("-skill")
        for skill_id, _content, _loading in mcp_registry.get_skills_for_agent("agent", context=context)
    }

    def _keys(names):
        return {by_name[n].server_name or n for n in names}

    assert advertised, catalog
    assert _keys(advertised | skill_owners) <= loaded
    assert loaded - _keys(advertised) == {"mcps-mcp"}

