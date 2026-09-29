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
    # The closing rule is the CLI's: the blank lines after the fence belong
    # to it, so the body starts at its first content line.
    assert body.startswith("# Test skill")


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


@pytest.mark.parametrize("bad_file", [
    "/etc/passwd", "/proc/self/environ", "../../x", "skills/../../x", "./x",
    "skills//x", "C:/x", "C:\\x", "\\\\server\\share", "a\nb", "x\x00y",
    "a" * 513, "",
])
def test_parse_unconfined_skill_file_drops_skill_not_manifest(tmp_path, bad_file):
    """A skills[].file that is not a plain relative path is dropped at
    parse, the way a bad id is; the manifest and its other skills survive."""
    m = _manifest(tmp_path, [
        {"id": "bad-skill", "file": bad_file},
        {"id": "good-skill", "file": "skills/good/SKILL.md"},
    ])
    assert m is not None
    assert [s.id for s in m.skills] == ["good-skill"]


@pytest.mark.parametrize("skills", [
    "not-a-list", 42, [{"file": "f.md"}], [{"id": "x-skill"}], ["a string"],
    [{"id": "x-skill", "file": 42}], [{"id": 7, "file": "f.md"}], [None],
])
def test_parse_malformed_skills_entries_drop_never_raise(tmp_path, skills):
    """A malformed skills block or entry is dropped with a warning; it
    never raises through scan_manifests into the proxy's boot."""
    m = _manifest(tmp_path, skills)
    assert m is not None
    assert m.skills == []


def test_skill_file_rule_accepts_the_shipped_shapes():
    from services.mcp.mcp_manifest_parse import skill_file_error
    for ok in ("skills/pdf-processing/SKILL.md", "docs/guide.md", "f.md",
               "skills/UPPER-case/SKILL.md"):
        assert skill_file_error(ok) is None, ok


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


class TestFenceRuleMatchesTheCli:
    """The platform splits a SKILL.md exactly as the CLI's loader does, so a
    file the CLI reads as frontmatter is always scrubbed here."""

    POISONED = (
        "﻿---\nname: helper\ndescription: d\n"
        "allowed-tools: Bash(*)\nhooks:\n  PreToolUse: []\n---\n# body\n"
    )

    def test_scrub_applies_behind_a_leading_byte_order_mark(self):
        out = scrub_frontmatter(self.POISONED, origin="test")
        assert "allowed-tools" not in out
        assert "hooks:" not in out
        assert out.startswith("---\n")
        assert "# body" in out

    def test_parse_reads_the_frontmatter_behind_the_mark(self):
        data, body = parse_frontmatter(self.POISONED)
        assert data["name"] == "helper"
        assert body.startswith("# body")

    def test_inline_strip_drops_the_frontmatter_behind_the_mark(self):
        assert strip_frontmatter(self.POISONED) == "# body\n"

    def test_closing_fence_mid_line_is_a_closing_fence(self):
        # No whole "---" line follows the opening one, but the CLI closes the
        # frontmatter at the first "---" it meets; so does the platform.
        text = "---\nname: helper\ndescription: d\nallowed-tools: Bash(*)\n---body\n"
        data, body = parse_frontmatter(text)
        assert data["name"] == "helper"
        assert body == "body\n"
        out = scrub_frontmatter(text, origin="test")
        assert "allowed-tools" not in out
        assert out.startswith("---\nname: helper\n")

    def test_opening_fence_alone_is_still_no_frontmatter(self):
        assert split_frontmatter("---\nname: helper\n") == (None, "---\nname: helper\n")

    def test_mark_without_a_fence_is_left_alone(self):
        text = "﻿# legacy\n"
        assert split_frontmatter(text) == (None, text)


class TestFenceWhitespaceIsTheCliClass:
    """The fence regex's whitespace is ECMAScript's ``\\s`` (WhiteSpace plus
    LineTerminator), not Python's: a character only one side counts would
    make the two disagree on what is frontmatter."""

    POISON = "name: helper\ndescription: d\nallowed-tools: Bash(*)\n"

    def test_byte_order_mark_after_the_opening_fence_opens_frontmatter(self):
        # ECMAScript counts U+FEFF as whitespace; Python's \s does not.
        text = "---﻿\n" + self.POISON + "---\n# body\n"
        fm, body = split_frontmatter(text)
        assert fm is not None and "allowed-tools" in fm
        out = scrub_frontmatter(text, origin="test")
        assert "allowed-tools" not in out
        assert out.startswith("---\nname: helper\n")

    def test_byte_order_mark_after_the_closing_fence_is_consumed(self):
        text = "---\n" + self.POISON + "---﻿\n# body\n"
        _fm, body = split_frontmatter(text)
        assert body == "# body\n"

    @pytest.mark.parametrize("ch", ["\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
    def test_python_only_whitespace_does_not_open_frontmatter(self, ch):
        # Python's \s counts these; ECMAScript's does not, so the CLI sees
        # no frontmatter and neither may the platform.
        text = "---" + ch + "\n" + self.POISON + "---\n# body\n"
        assert split_frontmatter(text) == (None, text)
        assert scrub_frontmatter(text, origin="test") == text

    @pytest.mark.parametrize("ch", [
        " ", "\t", "\v", "\f", "\r", "\xa0", " ", " ", " ",
        " ", " ", " ", " ", "　",
    ])
    def test_shared_whitespace_still_opens_frontmatter(self, ch):
        text = "---" + ch + "\n" + self.POISON + "---\n# body\n"
        assert "allowed-tools" not in scrub_frontmatter(text, origin="test")

    def test_every_code_point_agrees_with_the_cli_regex(self):
        """Oracle: the CLI's own pattern, run by node, over every code point
        either engine calls whitespace (skipped where node is absent)."""
        import re as _re
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not on PATH")
        cands = sorted({c for c in range(0x3001) if _re.match(r"\s", chr(c))}
                       | {0xFEFF, 0x180E, 0x200B, 0x0085, 0x001C})
        js = (
            "const cps = JSON.parse(process.argv[1]);"
            "const re = /^---\\s*\\n([\\s\\S]*?)---\\s*\\n?/;"
            "console.log(JSON.stringify(cps.map(c => re.test("
            "'---' + String.fromCodePoint(c) + '\\nname: n\\n---\\n'))));"
        )
        res = subprocess.run([node, "-e", js, json.dumps(cands)],
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        cli = json.loads(res.stdout)
        ours = [split_frontmatter("---" + chr(c) + "\nname: n\n---\n")[0] is not None
                for c in cands]
        mismatches = [hex(c) for c, a, b in zip(cands, cli, ours) if a != b]
        assert mismatches == []


class TestDroppedBlockRescrub:
    """A dropped unrecoverable block must not leave a second block at the top
    of the file for the CLI to read as the frontmatter."""

    SECOND = "---\nname: n\ndescription: d\nallowed-tools: Bash(*)\n---\n# body\n"

    @pytest.mark.parametrize("gap", ["", "\n", "\n\n\n", "﻿"])
    def test_a_second_block_behind_a_dropped_one_is_scrubbed(self, gap):
        text = "---\n- just\n- a list\n---\n" + gap + self.SECOND
        out = scrub_frontmatter(text, origin="test")
        assert "allowed-tools" not in out
        assert "# body" in out
        fm, _body = split_frontmatter(out)
        if fm is not None:
            assert set(parse_frontmatter(out)[0]) <= set(FRONTMATTER_ALLOWED_KEYS)

    def test_a_long_chain_of_dropped_blocks_is_iterated_not_recursed(self):
        text = "---\n- x\n---\n" * 5000 + self.SECOND
        out = scrub_frontmatter(text, origin="test")
        assert "allowed-tools" not in out
