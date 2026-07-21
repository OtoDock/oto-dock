"""Unit tests for the per-MCP-tool permission tiers.

Covers the three pieces that make the tier system a security contract:
  * ``_parse_permissions_block`` — strict manifest validation (bad blocks
    reject the manifest at install/scan time, like the ``costs`` block).
  * ``mcp_permissions.resolve_tool_tier`` — first-match rules, glob support,
    fallbacks, and the community exact-name rail for ``open`` (keyed off the
    scan DIRECTORY, never the spoofable ``category`` field).
  * ``mcp_permissions.tier_decision`` — the full tier × mode outcome table.

DB-free by design: manifests are monkeypatched into the registry cache.
"""
from pathlib import Path

import pytest

import config
from services.mcp import mcp_permissions, mcp_registry as reg
from services.mcp.mcp_registry import (
    CredentialConfig,
    McpManifest,
    ServerConfig,
    _parse_permissions_block,
)
from services.mcp.mcp_manifest_types import PermissionRule, PermissionsBlock


# ---------------------------------------------------------------------------
# _parse_permissions_block — strict validation
# ---------------------------------------------------------------------------


class TestParsePermissionsBlock:
    def test_returns_none_when_omitted(self):
        assert _parse_permissions_block(None, "any") is None

    def test_minimal_block(self):
        block = _parse_permissions_block({"default_tier": "open"}, "any")
        assert isinstance(block, PermissionsBlock)
        assert block.default_tier == "open"
        assert block.rules == []

    def test_full_block(self):
        block = _parse_permissions_block({
            "default_tier": "sensitive",
            "rules": [
                {"tool": "list_*", "tier": "open"},
                {"tool": "fire_trigger", "tier": "standard"},
            ],
        }, "triggers-mcp")
        assert block.default_tier == "sensitive"
        assert block.rules == [
            PermissionRule(tool="list_*", tier="open"),
            PermissionRule(tool="fire_trigger", tier="standard"),
        ]

    def test_default_tier_defaults_to_standard(self):
        block = _parse_permissions_block({"rules": []}, "any")
        assert block.default_tier == "standard"

    @pytest.mark.parametrize("bad", [
        "not-a-dict",
        {"default_tier": "T0"},
        {"rules": "nope"},
        {"rules": ["nope"]},
        {"rules": [{"tool": "", "tier": "open"}]},
        {"rules": [{"tool": "x", "tier": "loud"}]},
        {"rules": [{"tool": "x"}]},
        {"rules": [{"tool": "mcp__srv__x", "tier": "open"}]},
        {"rules": [{"tool": "x", "tier": "open", "extra": 1}]},
        {"rules": [{"tool": "x", "tier": "open"}, {"tool": "x", "tier": "standard"}]},
    ])
    def test_structural_defects_raise(self, bad):
        with pytest.raises(ValueError):
            _parse_permissions_block(bad, "any")

    def test_duplicate_globs_are_legal_ordering(self):
        block = _parse_permissions_block({
            "rules": [
                {"tool": "get_*", "tier": "open"},
                {"tool": "get_*", "tier": "standard"},
            ],
        }, "any")
        assert len(block.rules) == 2


# ---------------------------------------------------------------------------
# resolve_tool_tier — rules, fallbacks, and the community exact-name rail
# ---------------------------------------------------------------------------


def _mk(name, *, permissions=None, server_name="", category="custom",
        bundled=True):
    subdir = "custom" if bundled else "community"
    return McpManifest(
        name=name, label=name.title(), description=f"{name} does things.",
        version="1.0.0", category=category,
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], server_name=server_name,
        permissions=permissions,
        mcp_dir=Path(config.MCPS_DIR) / subdir / name,
    )


@pytest.fixture
def manifests(monkeypatch):
    cache: dict[str, McpManifest] = {}
    monkeypatch.setattr(reg, "_manifests", cache)
    return cache


class TestResolveToolTier:
    def test_no_manifest_falls_back_to_sensitive(self, manifests):
        assert mcp_permissions.resolve_tool_tier("ghost", "anything") == "sensitive"

    def test_no_block_falls_back_to_sensitive(self, manifests):
        manifests["plain"] = _mk("plain")
        assert mcp_permissions.resolve_tool_tier("plain", "tool") == "sensitive"

    def test_empty_names_fall_back(self, manifests):
        assert mcp_permissions.resolve_tool_tier("", "tool") == "sensitive"
        assert mcp_permissions.resolve_tool_tier("srv", "") == "sensitive"

    def test_exact_rule_and_default_tier(self, manifests):
        manifests["sched"] = _mk("sched", permissions=PermissionsBlock(
            default_tier="standard",
            rules=[PermissionRule(tool="list_tasks", tier="open")],
        ))
        assert mcp_permissions.resolve_tool_tier("sched", "list_tasks") == "open"
        assert mcp_permissions.resolve_tool_tier("sched", "delete_task") == "standard"

    def test_glob_rule_first_match_wins(self, manifests):
        manifests["m"] = _mk("m", permissions=PermissionsBlock(
            default_tier="sensitive",
            rules=[
                PermissionRule(tool="get_special", tier="sensitive"),
                PermissionRule(tool="get_*", tier="open"),
            ],
        ))
        assert mcp_permissions.resolve_tool_tier("m", "get_special") == "sensitive"
        assert mcp_permissions.resolve_tool_tier("m", "get_other") == "open"

    def test_server_name_keying(self, manifests):
        manifests["phone-mcp"] = _mk(
            "phone-mcp", server_name="make-call",
            permissions=PermissionsBlock(
                default_tier="sensitive",
                rules=[PermissionRule(tool="get_call_status", tier="open")],
            ),
        )
        # Lookup is by the mcpServers key (server_name), not the manifest name.
        assert mcp_permissions.resolve_tool_tier("make-call", "get_call_status") == "open"
        assert mcp_permissions.resolve_tool_tier("phone-mcp", "get_call_status") == "sensitive"

    # --- the community exact-name rail ---

    def test_community_exact_open_honored(self, manifests):
        manifests["vt"] = _mk("vt", bundled=False, permissions=PermissionsBlock(
            default_tier="standard",
            rules=[PermissionRule(tool="probe_media", tier="open")],
        ))
        assert mcp_permissions.resolve_tool_tier("vt", "probe_media") == "open"

    def test_community_glob_open_clamps_to_standard(self, manifests):
        manifests["c"] = _mk("c", bundled=False, permissions=PermissionsBlock(
            default_tier="sensitive",
            rules=[PermissionRule(tool="get_*", tier="open")],
        ))
        assert mcp_permissions.resolve_tool_tier("c", "get_thing") == "standard"

    def test_community_default_open_clamps_to_standard(self, manifests):
        manifests["c"] = _mk("c", bundled=False,
                             permissions=PermissionsBlock(default_tier="open"))
        assert mcp_permissions.resolve_tool_tier("c", "anything") == "standard"

    def test_community_non_open_tiers_honored(self, manifests):
        manifests["c"] = _mk("c", bundled=False, permissions=PermissionsBlock(
            default_tier="standard",
            rules=[PermissionRule(tool="send_*", tier="critical")],
        ))
        assert mcp_permissions.resolve_tool_tier("c", "send_all") == "critical"
        assert mcp_permissions.resolve_tool_tier("c", "other") == "standard"

    def test_category_spoof_does_not_lift_rail(self, manifests):
        # A community-installed manifest claiming category "custom" still gets
        # the rail — trust keys off the scan directory, not the JSON field.
        manifests["spoof"] = _mk("spoof", bundled=False, category="custom",
                                 permissions=PermissionsBlock(default_tier="open"))
        assert mcp_permissions.resolve_tool_tier("spoof", "x") == "standard"

    def test_bundled_glob_and_default_open_honored(self, manifests):
        manifests["b"] = _mk("b", permissions=PermissionsBlock(
            default_tier="open",
            rules=[PermissionRule(tool="display_*", tier="open")],
        ))
        assert mcp_permissions.resolve_tool_tier("b", "display_images") == "open"
        assert mcp_permissions.resolve_tool_tier("b", "whatever") == "open"


# ---------------------------------------------------------------------------
# tier_decision — the full tier × mode outcome table
# ---------------------------------------------------------------------------


class TestTierDecision:
    @pytest.mark.parametrize("tier,mode,expected", [
        # open: never prompts, any mode — including plan
        ("open", "default", "allow"),
        ("open", "acceptEdits", "allow"),
        ("open", "plan", "allow"),
        ("open", "dontAsk", "allow"),
        ("open", "auto", "allow"),
        # standard: prompts only in default
        ("standard", "default", "prompt"),
        ("standard", "acceptEdits", "allow"),
        ("standard", "plan", "deny"),
        ("standard", "dontAsk", "allow"),
        ("standard", "auto", "allow"),
        # sensitive: prompts in both prompting modes (the pre-tier status quo)
        ("sensitive", "default", "prompt"),
        ("sensitive", "acceptEdits", "prompt"),
        ("sensitive", "plan", "deny"),
        ("sensitive", "dontAsk", "allow"),
        ("sensitive", "auto", "allow"),
        # critical: always prompts; denied where nobody can answer
        ("critical", "default", "prompt"),
        ("critical", "acceptEdits", "prompt"),
        ("critical", "plan", "deny"),
        ("critical", "dontAsk", "prompt"),
        ("critical", "auto", "deny"),
    ])
    def test_grid(self, tier, mode, expected):
        assert mcp_permissions.tier_decision(tier, mode) == expected

    def test_unknown_tier_treated_as_sensitive(self):
        assert mcp_permissions.tier_decision("bogus", "acceptEdits") == "prompt"
        assert mcp_permissions.tier_decision("bogus", "dontAsk") == "allow"
