"""Unit tests for the per-MCP-tool permission tiers.

Covers the three pieces that make the tier system a security contract:
  * ``_parse_permissions_block`` — strict manifest validation (bad blocks
    reject the manifest at install/scan time, like the ``costs`` block).
  * ``mcp_permissions.resolve_tool_tier`` — first-match rules, the tools' own
    hints, glob support, fallbacks, and the community exact-name rail for
    ``open`` (keyed off the scan DIRECTORY, never the spoofable ``category``
    field).
  * ``mcp_permissions.tier_decision`` — the full tier × mode outcome table.

DB-free by design: manifests are monkeypatched into the registry cache.
"""
from pathlib import Path

import pytest

import config
from services.mcp import mcp_permissions, mcp_registry as reg
from services.mcp.mcp_manifest_types import PermissionRule, PermissionsBlock
from services.mcp.mcp_registry import (
    CredentialConfig,
    McpManifest,
    ServerConfig,
    _parse_permissions_block,
)

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
    # No hints a server sent elsewhere in the run.
    monkeypatch.setattr(mcp_permissions, "_hints", {})
    monkeypatch.setattr(mcp_permissions, "_hints_loaded", True)
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
# The tools' own hints: the rule after a manifest rule, before default_tier
# ---------------------------------------------------------------------------

_RO = {"readOnlyHint": True, "openWorldHint": True}
_LOCAL_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


class TestToolHints:
    @pytest.mark.parametrize("hints,tier", [
        (None, None),
        ({}, None),
        ({"title": "x"}, None),
        ({"readOnlyHint": True}, "open"),
        (_RO, "open"),                                            # a web search is open
        (_LOCAL_WRITE, "standard"),
        ({"destructiveHint": False}, "sensitive"),                # open world by default
        ({"destructiveHint": False, "openWorldHint": True}, "sensitive"),  # sends outward
        ({"idempotentHint": True}, "sensitive"),                  # destructive by default
        (_DESTRUCTIVE, "sensitive"),
        ({"readOnlyHint": True, "destructiveHint": True}, "open"),  # Codex's order: read-only first
    ])
    def test_the_hint_rule(self, hints, tier):
        assert mcp_permissions.hint_tier(hints) == tier
        # Codex's own MCP rule asks exactly where the tier is sensitive or unlabelled.
        assert mcp_permissions.codex_asks(hints) is (tier in (None, "sensitive"))

    def test_a_rule_beats_a_hint_and_a_hint_beats_default_tier(self, manifests):
        manifests["srv"] = _mk("srv", permissions=PermissionsBlock(
            default_tier="standard",
            rules=[PermissionRule(tool="pinned", tier="open")],
        ))
        mcp_permissions._hints["srv"] = {"pinned": _DESTRUCTIVE, "reader": _RO,
                                         "writer": _LOCAL_WRITE, "sender": _DESTRUCTIVE}
        assert mcp_permissions.resolve_tool_tier("srv", "pinned") == "open"
        assert mcp_permissions.resolve_tool_tier("srv", "reader") == "open"
        assert mcp_permissions.resolve_tool_tier("srv", "writer") == "standard"
        assert mcp_permissions.resolve_tool_tier("srv", "sender") == "sensitive"
        # A tool that sent no hints (or whose list was never read) keeps the default.
        assert mcp_permissions.resolve_tool_tier("srv", "unlisted") == "standard"

    def test_a_catalog_hint_is_honoured_like_a_first_party_one(self, manifests):
        manifests["cat"] = _mk("cat", bundled=False, category="community",
                               permissions=PermissionsBlock(default_tier="sensitive", rules=[]))
        mcp_permissions._hints["cat"] = {"search": _RO}
        assert mcp_permissions.resolve_tool_tier("cat", "search") == "open"
        # No manifest block at all: the hints, else sensitive.
        mcp_permissions._hints["bare"] = {"read": _RO}
        assert mcp_permissions.resolve_tool_tier("bare", "read") == "open"
        assert mcp_permissions.resolve_tool_tier("bare", "other") == "sensitive"

    def test_hints_are_kept_across_a_restart(self, manifests, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(config, "PLATFORM_DATA_DIR", str(tmp_path))
        mcp_permissions.record_tool_hints("srv", [("read", {**_RO, "title": "Read"}),
                                                  ("write", _DESTRUCTIVE), ("plain", None)])
        on_disk = (tmp_path / "mcp-tool-hints.json").read_text()
        assert '"title"' not in on_disk
        monkeypatch.setattr(mcp_permissions, "_hints", {})
        monkeypatch.setattr(mcp_permissions, "_hints_loaded", False)
        assert mcp_permissions.tool_hints("srv", "read") == _RO
        assert mcp_permissions.tool_hints("srv", "plain") == {}
        assert mcp_permissions.known_tools("srv") == ["plain", "read", "write"]
        # A server that loosens its own word is logged.
        with caplog.at_level("INFO", logger="services.mcp.mcp_permissions"):
            mcp_permissions.record_tool_hints("srv", [("write", _RO)])
        assert "srv/write now labels itself open (was sensitive)" in caplog.text
        assert mcp_permissions.known_tools("srv") == ["write"]

    def test_an_unreadable_hints_file_is_an_empty_cache(self, manifests, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(config, "PLATFORM_DATA_DIR", str(tmp_path))
        (tmp_path / "mcp-tool-hints.json").write_text("{not json")
        monkeypatch.setattr(mcp_permissions, "_hints_loaded", False)
        with caplog.at_level("WARNING", logger="services.mcp.mcp_permissions"):
            assert mcp_permissions.tool_hints("srv", "read") is None
        assert "unreadable" in caplog.text

    def test_codex_prompts_a_globbed_sensitive_server_until_its_list_is_read(self, manifests):
        manifests["glob"] = _mk("glob", permissions=PermissionsBlock(
            default_tier="standard", rules=[PermissionRule(tool="send_*", tier="sensitive")]))
        manifests["exact"] = _mk("exact", permissions=PermissionsBlock(
            default_tier="sensitive", rules=[PermissionRule(tool="send_mail", tier="sensitive")]))
        assert mcp_permissions.codex_server_default("glob") == "prompt"
        assert mcp_permissions.codex_server_default("exact") is None   # named: its own table
        assert mcp_permissions.codex_server_default("ghost") is None
        # Once a session lists the tools, each gets its own table instead.
        mcp_permissions._hints["glob"] = {"send_note": _RO}
        assert mcp_permissions.codex_server_default("glob") is None
        assert mcp_permissions.codex_tool_approvals("glob") == {"send_note": "prompt"}

    def test_codex_gets_a_table_where_its_rule_and_the_tier_disagree(self, manifests):
        manifests["srv"] = _mk("srv", permissions=PermissionsBlock(
            default_tier="standard",
            rules=[PermissionRule(tool="share", tier="critical"),
                   PermissionRule(tool="peek", tier="sensitive"),
                   PermissionRule(tool="note", tier="open")],
        ))
        mcp_permissions._hints["srv"] = {"peek": _RO, "local": _LOCAL_WRITE, "send": _DESTRUCTIVE}
        assert mcp_permissions.codex_tool_approvals("srv") == {
            "share": "approve",   # critical: the person's card is its one judge
            "note": "approve",    # open by rule, unlabelled: Codex would ask
            "peek": "prompt",     # sensitive by rule, labelled read-only: Codex would not
        }


# ---------------------------------------------------------------------------
# critical is the platform's own tier: bounded at parse, by scan directory
# ---------------------------------------------------------------------------


class TestCriticalIsThePlatformsOwn:
    PERMS = {"default_tier": "critical", "rules": [
        {"tool": "read", "tier": "open"}, {"tool": "ban_user", "tier": "critical"}]}

    def _parse(self, monkeypatch, root, kind, name, category="community"):
        import json

        from services.mcp import mcp_manifest_parse as mmp
        monkeypatch.setattr(config, "MCPS_DIR", root)
        folder = root / kind / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "name": name, "label": name, "description": "d", "version": "",
            "category": category, "permissions": self.PERMS,
            "server": {"runtime": "node", "transport": "stdio", "command": "node",
                       "source": f"npm:{name}"}}))
        return mmp._parse_manifest(folder / "manifest.json")

    def test_a_catalog_manifest_runs_critical_as_sensitive(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            m = self._parse(monkeypatch, tmp_path, "community", "chat")
        assert m is not None and m.permissions.default_tier == "sensitive"
        assert m.permissions.rules == [PermissionRule(tool="read", tier="open"),
                                       PermissionRule(tool="ban_user", tier="sensitive")]
        assert "chat: critical is the platform's own tier; default_tier, ban_user run as sensitive" \
            in caplog.text
        # A self-declared category does not make a catalog folder the platform's.
        spoof = self._parse(monkeypatch, tmp_path, "community", "spoof", category="custom")
        assert spoof.permissions.default_tier == "sensitive"

    def test_the_platforms_own_manifest_keeps_it(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            m = self._parse(monkeypatch, tmp_path, "custom", "checks", category="custom")
        assert m.permissions.default_tier == "critical"
        assert m.permissions.rules[1] == PermissionRule(tool="ban_user", tier="critical")
        assert "platform's own tier" not in caplog.text

    @pytest.mark.parametrize(("kind", "category"), [
        ("custom", "custom"), ("community", "community"), ("community", "custom")])
    def test_the_parser_and_the_open_rail_share_one_directory_test(
            self, monkeypatch, tmp_path, kind, category):
        m = self._parse(monkeypatch, tmp_path, kind, "both", category=category)
        bundled = kind == "custom"
        assert mcp_permissions._is_bundled(m) is bundled
        assert (m.permissions.default_tier == "critical") is bundled

    def test_the_bundled_manifests_that_use_it_are_all_under_custom(self):
        import json
        users = sorted(
            p.parent.name for p in (config.MCPS_DIR / "custom").glob("*/manifest.json")
            if '"critical"' in p.read_text())
        assert users == ["agent-config-mcp", "checks-mcp"]
        for name in users:
            perms = json.loads((config.MCPS_DIR / "custom" / name / "manifest.json").read_text())
            assert "critical" in {r["tier"] for r in perms["permissions"]["rules"]}


# ---------------------------------------------------------------------------
# tier_decision — the full tier × mode outcome table
# ---------------------------------------------------------------------------


class TestTierDecision:
    @pytest.mark.parametrize("tier,mode,expected", [
        # open: never prompts, any mode — including plan
        ("open", "ask", "allow"),
        ("open", "autoReview", "allow"),
        ("open", "plan", "allow"),
        ("open", "dontAsk", "allow"),
        ("open", "auto", "allow"),
        # standard: prompts only in Ask (a local, reversible write runs in Auto)
        ("standard", "ask", "prompt"),
        ("standard", "autoReview", "allow"),
        ("standard", "plan", "deny"),
        ("standard", "dontAsk", "allow"),
        ("standard", "auto", "allow"),
        # sensitive: the card in Ask, the engine's reviewer (else the card) in Auto
        ("sensitive", "ask", "prompt"),
        ("sensitive", "autoReview", "prompt"),
        ("sensitive", "plan", "deny"),
        ("sensitive", "dontAsk", "allow"),
        ("sensitive", "auto", "allow"),
        # critical: always prompts; denied where nobody can answer
        ("critical", "ask", "prompt"),
        ("critical", "autoReview", "prompt"),
        ("critical", "plan", "deny"),
        ("critical", "dontAsk", "prompt"),
        ("critical", "auto", "deny"),
        # a word from before 1.9 reads as its current one
        ("standard", "default", "prompt"),
        ("standard", "acceptEdits", "allow"),
    ])
    def test_grid(self, tier, mode, expected):
        assert mcp_permissions.tier_decision(tier, mode) == expected

    def test_unknown_tier_treated_as_sensitive(self):
        assert mcp_permissions.tier_decision("bogus", "autoReview") == "prompt"
        assert mcp_permissions.tier_decision("bogus", "dontAsk") == "allow"


# ---------------------------------------------------------------------------
# The Codex layer's canonical_tool_name — its sanitized server keys map back
# to the manifest, and its native tool names to the platform's
# ---------------------------------------------------------------------------
#
# Codex names MCP tools for its PreToolUse hook with the server key sanitized
# (``meetings-mcp`` → ``meetings_mcp``). Observed live 2026-09-09: a Codex
# meeting participant's ``mcp__meetings_mcp__direct_to`` missed the manifest's
# ``open`` rule, fell to the default tier and parked the meeting on a
# permission card in acceptEdits mode.


def _canonical(name: str, session_id: str = "") -> str:
    from core.layers.codex.layer import CodexCLIExecutionLayer
    return CodexCLIExecutionLayer().canonical_tool_name(session_id, name)


class TestCanonicalToolName:
    def test_sanitized_server_key_maps_to_the_manifest(self, manifests):
        manifests["meetings-mcp"] = _mk("meetings-mcp")
        assert _canonical("mcp__meetings_mcp__direct_to") == "mcp__meetings-mcp__direct_to"

    def test_exact_server_name_and_non_mcp_tools_are_untouched(self, manifests):
        manifests["meetings-mcp"] = _mk("meetings-mcp")
        manifests["my_tools"] = _mk("my_tools")
        assert _canonical("mcp__meetings-mcp__direct_to") == "mcp__meetings-mcp__direct_to"
        assert _canonical("mcp__my_tools__go") == "mcp__my_tools__go"
        assert _canonical("Bash") == "Bash"
        assert _canonical("mcp__meetings_mcp") == "mcp__meetings_mcp"

    def test_unknown_server_stays_as_is(self, manifests):
        assert _canonical("mcp__ghost_x__y") == "mcp__ghost_x__y"

    def test_server_name_override_is_the_key(self, manifests):
        manifests["gh"] = _mk("gh", server_name="github-mcp")
        assert _canonical("mcp__github_mcp__list_issues") == "mcp__github-mcp__list_issues"

    def test_a_known_session_restores_against_its_own_servers(self, manifests, monkeypatch):
        """Two manifests whose names sanitise alike: a session that declared
        one of them resolves to that one, never to whichever the registry
        lists first."""
        manifests["a-b"] = _mk("a-b")
        manifests["a_b"] = _mk("a_b", server_name="a.b")
        from core.layers.codex import session as codex_session
        sess = codex_session.CodexAppServerSession(
            session_id="11111111-2222-4333-8444-555555555555", agent_name="pa",
            model="gpt-6", sandbox_mode="workspace-write", working_dir="",
            config_dir="/tmp/x", mcp_server_names=["a.b"],
        )
        monkeypatch.setitem(codex_session._codex_sessions, sess.session_id, sess)
        assert _canonical("mcp__a_b__go", sess.session_id) == "mcp__a.b__go"
        assert _canonical("mcp__a_b__go") == "mcp__a-b__go"

    def test_native_names_map_to_the_platforms(self):
        assert _canonical("commandExecution") == "Bash"
        assert _canonical("exec_command") == "Bash"
        assert _canonical("fileChange") == "apply_patch"
        assert _canonical("update_plan") == "TodoWrite"
        assert _canonical("webSearch") == "web_search"
        assert _canonical("request_user_input") == "request_user_input"
