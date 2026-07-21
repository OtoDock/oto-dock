"""skill_format helpers + SkillDef.loading manifest parsing.

The scrub whitelist is a security boundary: Claude Code honors
``allowed-tools`` from skill frontmatter, which would pre-authorize tools
past the platform's ask-tier on interactive sessions. These tests pin the
whitelist-not-blacklist behavior and the per-skill fail-closed parse rules
(bad id → skill dropped, manifest survives; bad loading → default + warn).
"""

import json

import pytest

from services.mcp.mcp_manifest_parse import _parse_manifest
from services.mcp.skill_format import (
    FRONTMATTER_ALLOWED_KEYS,
    parse_frontmatter,
    scrub_frontmatter,
    split_frontmatter,
    strip_frontmatter,
)

SKILL_WITH_ALLOWED_TOOLS = """---
name: test-skill
description: Does test things. Use when testing.
allowed-tools: Bash(git:*) Read
license: MIT
metadata:
  author: someone
  version: "1.0"
---

# Test skill

Body line one.
"""

NO_FRONTMATTER = "# Just a legacy skill doc\n\nInstructions here.\n"


# ── split / parse / strip ──────────────────────────────────────────────

def test_split_frontmatter_roundtrip():
    fm, body = split_frontmatter(SKILL_WITH_ALLOWED_TOOLS)
    assert "allowed-tools" in fm
    assert body.startswith("\n# Test skill")


def test_split_no_frontmatter():
    fm, body = split_frontmatter(NO_FRONTMATTER)
    assert fm is None
    assert body == NO_FRONTMATTER


def test_split_unterminated_fence_is_no_frontmatter():
    text = "---\nname: broken\nno closing fence\n"
    fm, body = split_frontmatter(text)
    assert fm is None
    assert body == text


def test_parse_frontmatter_values():
    data, body = parse_frontmatter(SKILL_WITH_ALLOWED_TOOLS)
    assert data["name"] == "test-skill"
    assert data["allowed-tools"] == "Bash(git:*) Read"
    assert data["metadata"]["version"] == "1.0"
    assert "# Test skill" in body


def test_parse_frontmatter_invalid_yaml():
    data, body = parse_frontmatter("---\n: : :\n---\nbody\n")
    assert data == {}
    assert "body" in body


def test_strip_frontmatter():
    body = strip_frontmatter(SKILL_WITH_ALLOWED_TOOLS)
    assert body.startswith("# Test skill")
    assert "allowed-tools" not in body


def test_strip_without_frontmatter_is_identity():
    assert strip_frontmatter(NO_FRONTMATTER) == NO_FRONTMATTER


# ── scrub (security boundary) ──────────────────────────────────────────

def test_scrub_drops_allowed_tools_keeps_whitelist():
    scrubbed = scrub_frontmatter(SKILL_WITH_ALLOWED_TOOLS)
    assert "allowed-tools" not in scrubbed
    data, body = parse_frontmatter(scrubbed)
    assert data["name"] == "test-skill"
    assert data["description"].startswith("Does test things")
    assert data["license"] == "MIT"
    assert data["metadata"] == {"author": "someone", "version": "1.0"}
    assert "# Test skill" in body
    assert "Body line one." in body


def test_scrub_is_whitelist_not_blacklist():
    text = "---\nname: x\ndescription: y\nfuture-dangerous-key: '!'\n---\nbody\n"
    scrubbed = scrub_frontmatter(text)
    assert "future-dangerous-key" not in scrubbed
    data, _ = parse_frontmatter(scrubbed)
    assert set(data) <= set(FRONTMATTER_ALLOWED_KEYS)


def test_scrub_without_frontmatter_is_identity():
    assert scrub_frontmatter(NO_FRONTMATTER) == NO_FRONTMATTER


def test_scrub_invalid_yaml_drops_fence_entirely():
    scrubbed = scrub_frontmatter("---\n: : :\n---\nbody text\n")
    assert "---" not in scrubbed
    assert "body text" in scrubbed


def test_scrub_idempotent():
    once = scrub_frontmatter(SKILL_WITH_ALLOWED_TOOLS)
    assert scrub_frontmatter(once) == once


# ── manifest parsing of skills[] entries ───────────────────────────────

def _manifest(tmp_path, skills):
    data = {
        "name": "test-mcp", "label": "Test", "description": "d",
        "version": "1.0.0", "category": "custom",
        "server": {"runtime": "none", "transport": "none"},
        "skills": skills,
    }
    p = tmp_path / "test-mcp"
    p.mkdir()
    (p / "manifest.json").write_text(json.dumps(data))
    return _parse_manifest(p / "manifest.json")


def test_parse_loading_default_is_on_demand(tmp_path):
    m = _manifest(tmp_path, [{"id": "my-skill", "file": "skills/my-skill/SKILL.md"}])
    assert m.skills[0].loading == "on_demand"


@pytest.mark.parametrize("mode", ["always", "on_demand"])
def test_parse_loading_explicit(tmp_path, mode):
    m = _manifest(tmp_path, [
        {"id": "my-skill", "file": "f.md", "loading": mode}])
    assert m.skills[0].loading == mode


def test_parse_loading_invalid_falls_back_to_default(tmp_path):
    m = _manifest(tmp_path, [
        {"id": "my-skill", "file": "f.md", "loading": "sometimes"}])
    assert m.skills[0].loading == "on_demand"


@pytest.mark.parametrize("bad_id", [
    "../evil", "UPPER", "has_underscore", "-leading", "trailing-",
    "double--hyphen", "path/sep", "a" * 65,
])
def test_parse_unsafe_skill_id_drops_skill_not_manifest(tmp_path, bad_id):
    m = _manifest(tmp_path, [
        {"id": bad_id, "file": "f.md"},
        {"id": "good-skill", "file": "g.md"},
    ])
    assert m is not None, "manifest must survive a bad skill entry"
    assert [s.id for s in m.skills] == ["good-skill"]


def test_parse_existing_real_ids_all_pass():
    from services.mcp.mcp_manifest_types import SKILL_ID_RE
    for sid in ["memory-usage", "photo-editing-guide", "github-git-usage",
                "display-tools", "voiceover", "web-browsing",
                "notification-instructions", "trigger-instructions"]:
        assert SKILL_ID_RE.fullmatch(sid), sid


def test_parse_unknown_skill_keys_tolerated(tmp_path):
    m = _manifest(tmp_path, [
        {"id": "my-skill", "file": "f.md", "some_future_key": 42}])
    assert m.skills[0].id == "my-skill"


class TestScrubSalvage:
    """Invalid-YAML frontmatter (the unquoted-colon description footgun,
    found live 2026-07-19: the tts-mcp voiceover skill was silently
    frontmatter-stripped at scrub → codex rejected the whole skill)."""

    def test_unquoted_colon_description_salvaged(self):
        from services.mcp.skill_format import parse_frontmatter, scrub_frontmatter
        text = (
            "---\n"
            "name: voiceover\n"
            "description: Produce voice-overs: choose the voice, generate.\n"
            "allowed-tools: Bash\n"
            "---\n\nBody here.\n"
        )
        out = scrub_frontmatter(text, origin="tts-mcp/voiceover")
        data, body = parse_frontmatter(out)
        # Valid YAML now, descriptive keys preserved, body intact...
        assert data["name"] == "voiceover"
        assert data["description"].startswith("Produce voice-overs:")
        assert "Body here." in body
        # ...and the authorization-bearing key did NOT survive the salvage.
        assert "allowed-tools" not in out

    def test_unrecoverable_frontmatter_still_dropped(self):
        from services.mcp.skill_format import scrub_frontmatter, split_frontmatter
        text = "---\n- just\n- a\n- list\n---\n\nBody.\n"
        out = scrub_frontmatter(text, origin="x")
        fm, body = split_frontmatter(out)
        assert fm is None and "Body." in out
