"""The two manifest fields a source change reads: ``replaces`` (the sources an
entry supersedes, with its credential key renames) and ``skills[].audience``
(the tier a skill is listed for). The parser drops a malformed value with a
warning; the install gate refuses it."""

from __future__ import annotations

import json

from services.community import community_installer as ci
from services.mcp import mcp_manifest_parse as mmp


def _manifest(**extra) -> dict:
    data = {
        "name": "nextcloud", "label": "Nextcloud", "description": "d",
        "version": "", "category": "community",
        "server": {"runtime": "node", "transport": "stdio", "command": "node",
                   "source": "npm:nextcloud-mcp-server"},
    }
    data.update(extra)
    return data


# ── replaces ───────────────────────────────────────────────────────

def test_a_well_formed_replaces_parses():
    decls = mmp.parse_replaces([
        {"source": "npm:nextcloud-mcp-server",
         "credentials": {"NEXTCLOUD_USER": "NEXTCLOUD_USERNAME"}},
        {"source": "ghcr.io/other/nextcloud:1.0"},
    ], "nextcloud")
    assert [(d.source, d.credentials) for d in decls] == [
        ("npm:nextcloud-mcp-server", {"NEXTCLOUD_USER": "NEXTCLOUD_USERNAME"}),
        ("ghcr.io/other/nextcloud:1.0", {}),
    ]


def test_malformed_entries_are_dropped_and_the_rest_kept():
    decls = mmp.parse_replaces([
        "npm:x",                                   # not an object
        {"source": ""},                            # empty source
        {"source": "npm:a", "credentials": ["A"]},  # not a map
        {"source": "npm:b", "credentials": {"_hosted_service_mode": "X"}},  # control key
        {"source": "npm:c", "credentials": {"OLD": "new key"}},  # not an identifier
        {"source": "npm:d", "credentials": {"OLD": "NEW"}},
    ], "x")
    assert [d.source for d in decls] == ["npm:d"]


def test_replaces_that_is_not_a_list_is_dropped():
    assert mmp.parse_replaces({"source": "npm:a"}, "x") == []
    assert mmp.parse_replaces(None, "x") == []


def test_the_parsed_manifest_carries_replaces(tmp_path):
    folder = tmp_path / "nextcloud"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps(_manifest(replaces=[
        {"source": "npm:old-nextcloud", "credentials": {"USER": "USERNAME"}},
    ])))
    m = mmp._parse_manifest(folder / "manifest.json")
    assert [(d.source, d.credentials) for d in m.replaces] == [
        ("npm:old-nextcloud", {"USER": "USERNAME"}),
    ]


def test_the_gate_refuses_a_malformed_replaces():
    errors = ci._validate_manifest(_manifest(replaces=[{"source": "npm:a", "credentials": {"_X": "Y"}}]))
    assert any("replaces entry" in e for e in errors)
    errors = ci._validate_manifest(_manifest(replaces={"source": "npm:a"}))
    assert "replaces must be a list" in errors
    assert not any("replaces" in e for e in ci._validate_manifest(
        _manifest(replaces=[{"source": "npm:a", "credentials": {"A": "B"}}]),
    ))


# ── skills[].audience ─────────────────────────────────────────────

def _skill(audience):
    sk = {"id": "mcp-authoring", "file": "skills/mcp-authoring/SKILL.md"}
    if audience is not None:
        sk["audience"] = audience
    return sk


def test_a_skill_audience_parses_and_an_unknown_word_drops_the_skill(tmp_path):
    folder = tmp_path / "mcps-mcp"
    (folder / "skills" / "mcp-authoring").mkdir(parents=True)
    (folder / "skills" / "mcp-authoring" / "SKILL.md").write_text("---\nname: mcp-authoring\n---\n")
    for audience, expect in (("editor", ["editor"]), (None, [""]), ("", [""]), ("editors", [])):
        (folder / "manifest.json").write_text(json.dumps(_manifest(
            name="mcps-mcp", skills=[_skill(audience)],
        )))
        m = mmp._parse_manifest(folder / "manifest.json")
        assert [s.audience for s in m.skills] == expect, audience


def test_the_gate_refuses_an_unknown_audience():
    errors = ci._validate_manifest(_manifest(skills=[_skill("admins")]))
    assert any("audience" in e for e in errors)
    assert not any("audience" in e for e in ci._validate_manifest(
        _manifest(skills=[_skill("owner")]),
    ))
