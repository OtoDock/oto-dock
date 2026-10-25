"""Tests for ``mcp_registry.build_available_mcps_section`` — the
top-down MCP catalog injected near the top of every agent prompt.

Generated content rules:
- Each enabled MCP renders as ``- **{label}** (`{slug}`) — {first sentence}``.
- Manifests matching the session ``context`` via ``exclude_from`` are filtered.
- Sorted alphabetically by label for deterministic output.
- Empty result → empty string (caller skips the section header).
"""

from __future__ import annotations

from unittest.mock import patch

from services.mcp import mcp_registry


def _fake_manifest(name, label="", description="", exclude_from=None):
    """Build a minimal McpManifest-like object for tests.

    Only the fields ``build_available_mcps_section`` reads need to be set.
    """
    return mcp_registry.McpManifest(
        name=name,
        label=label or name,
        description=description,
        version="1.0.0",
        category="custom",
        server=mcp_registry.ServerConfig(runtime="python", transport="stdio", command="", args=[]),
        credentials=mcp_registry.CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=exclude_from or [],
        skills=[],
    )


def test_renders_each_assigned_mcp(temp_db):
    manifests = [
        _fake_manifest("schedules-mcp", "Task Management", "Task delegation, scheduling, and execution"),
        _fake_manifest("file-tools-mcp", "File Tools", "Document reading and editing"),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("pa")
    assert "# Available Tools (MCPs)" in text
    assert "**Task Management** (`schedules-mcp`) — Task delegation, scheduling, and execution" in text
    assert "**File Tools** (`file-tools-mcp`) — Document reading and editing" in text


def test_first_sentence_trims_noise(temp_db):
    """Operational metadata after the first sentence is stripped."""
    manifests = [
        _fake_manifest(
            "mcps-mcp-clone",
            label="Clone",
            description="Useful tool. Permission-aware: viewers and internal agents see zero tools.",
        ),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("pa")
    assert "**Clone** (`mcps-mcp-clone`) — Useful tool" in text
    # The operational note must NOT appear.
    assert "Permission-aware" not in text


def test_lists_mcps_mcp(temp_db):
    """The meta MCP is listed like any other enabled MCP."""
    manifests = [
        _fake_manifest("mcps-mcp", "MCPs Manager", "Lets agents browse the catalog"),
        _fake_manifest("schedules-mcp", "Tasks", "Task scheduling"),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("pa")
    assert "schedules-mcp" in text
    assert "**MCPs Manager** (`mcps-mcp`)" in text


def test_filters_by_exclude_from(temp_db):
    """Manifests with exclude_from matching the context are dropped."""
    manifests = [
        _fake_manifest("display-mcp", "Display", "Show images", exclude_from=["phone"]),
        _fake_manifest("schedules-mcp", "Tasks", "Task scheduling"),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        phone_text = mcp_registry.build_available_mcps_section("pa", context="phone")
        dash_text = mcp_registry.build_available_mcps_section("pa", context="dashboard")
    # Phone excludes display-mcp; dashboard keeps it.
    assert "Display" not in phone_text
    assert "Tasks" in phone_text
    assert "Display" in dash_text
    assert "Tasks" in dash_text


def test_empty_when_no_mcps(temp_db):
    with patch.object(mcp_registry, "get_agent_mcps", return_value=[]):
        text = mcp_registry.build_available_mcps_section("pa")
    assert text == ""


def test_lists_mcps_mcp_alone(temp_db):
    """If only mcps-mcp is enabled, the catalog lists it."""
    manifests = [
        _fake_manifest("mcps-mcp", "MCPs Manager", "Meta MCP"),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("pa")
    assert text != ""
    assert "mcps-mcp" in text


def test_sorted_alphabetically_by_label(temp_db):
    manifests = [
        _fake_manifest("zoom-mcp", "Zoom", "Video calls"),
        _fake_manifest("aa-mcp", "Apple", "First"),
        _fake_manifest("nn-mcp", "Notion", "Middle"),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("pa")
    apple_idx = text.find("Apple")
    notion_idx = text.find("Notion")
    zoom_idx = text.find("Zoom")
    assert 0 < apple_idx < notion_idx < zoom_idx


def test_handles_no_description(temp_db):
    """An MCP with no description renders the label/slug only."""
    manifests = [
        _fake_manifest("bare-mcp", "Bare", description=""),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("pa")
    assert "**Bare** (`bare-mcp`)" in text
    # No trailing em-dash + nothing
    assert "**Bare** (`bare-mcp`) —" not in text
