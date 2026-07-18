"""Tests for ``mcp_registry.get_skills_for_agent`` — the per-MCP skill loader
behind the ``# MCP Tool Skills`` prompt section.

Pins the 2026-07 skills behavior:
- records are (skill_id, content, loading) triples;
- SKILL.md frontmatter never reaches prompt content (stripped on read);
- ``exclude_from`` filters against the REAL session context — this was dead
  until the call site stopped hardcoding ``context="system_prompt"``;
- the ``""`` context (unset client_type) excludes nothing;
- DB rows override manifest defaults (enabled gate + exclude_from copy).
"""

from __future__ import annotations

from unittest.mock import patch

from services.mcp import mcp_registry
from storage import mcp_store


def _manifest_with_skills(tmp_path, name, skills):
    """Manifest whose ``skills`` live as real files under tmp_path/name."""
    mcp_dir = tmp_path / name
    (mcp_dir / "skills").mkdir(parents=True, exist_ok=True)
    defs = []
    for sk in skills:
        (mcp_dir / sk["file"]).write_text(sk.pop("content"))
        defs.append(mcp_registry.SkillDef(**sk))
    return mcp_registry.McpManifest(
        name=name,
        label=name,
        description="",
        version="1.0.0",
        category="custom",
        server=mcp_registry.ServerConfig(
            runtime="none", transport="none", command="", args=[]),
        credentials=mcp_registry.CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=defs,
        mcp_dir=mcp_dir,
    )


FRONTMATTERED = """---
name: narrated-video
description: Produce voice-overs. Use when narrating.
allowed-tools: Bash(ffmpeg:*)
---

# Voice-over production

Pick a voice, then generate segments.
"""


def _voice_manifest(tmp_path):
    return _manifest_with_skills(tmp_path, "tts-test", [{
        "id": "narrated-video", "file": "skills/narrated-video.md",
        "content": FRONTMATTERED,
        "default_exclude_from": ["phone"], "loading": "on_demand",
    }])


def test_returns_id_content_loading_triples(temp_db, tmp_path):
    m = _manifest_with_skills(tmp_path, "m1", [
        {"id": "alpha-skill", "file": "skills/a.md", "content": "Alpha body.",
         "loading": "always"},
    ])
    with patch.object(mcp_registry, "get_agent_mcps", return_value=[m]):
        skills = mcp_registry.get_skills_for_agent("pa", context="dashboard")
    assert skills == [("alpha-skill", "Alpha body.", "always")]


def test_frontmatter_never_reaches_prompt_content(temp_db, tmp_path):
    with patch.object(mcp_registry, "get_agent_mcps",
                      return_value=[_voice_manifest(tmp_path)]):
        skills = mcp_registry.get_skills_for_agent("pa", context="dashboard")
    (_sid, content, loading) = skills[0]
    assert content.startswith("# Voice-over production")
    assert "allowed-tools" not in content
    assert "description:" not in content
    assert loading == "on_demand"


def test_exclude_from_matches_real_context(temp_db, tmp_path):
    with patch.object(mcp_registry, "get_agent_mcps",
                      return_value=[_voice_manifest(tmp_path)]):
        on_phone = mcp_registry.get_skills_for_agent("pa", context="phone")
        on_dash = mcp_registry.get_skills_for_agent("pa", context="dashboard")
    assert on_phone == []
    assert [s[0] for s in on_dash] == ["narrated-video"]


def test_empty_context_excludes_nothing(temp_db, tmp_path):
    with patch.object(mcp_registry, "get_agent_mcps",
                      return_value=[_voice_manifest(tmp_path)]):
        skills = mcp_registry.get_skills_for_agent("pa", context="")
    assert [s[0] for s in skills] == ["narrated-video"]


def test_db_disable_wins_over_manifest(temp_db, tmp_path):
    mcp_store.set_agent_skill("pa", "narrated-video", enabled=False,
                              exclude_from=[])
    with patch.object(mcp_registry, "get_agent_mcps",
                      return_value=[_voice_manifest(tmp_path)]):
        skills = mcp_registry.get_skills_for_agent("pa", context="dashboard")
    assert skills == []


def test_db_exclude_from_overrides_manifest_default(temp_db, tmp_path):
    # Row clears the phone exclusion → skill appears on phone again.
    mcp_store.set_agent_skill("pa", "narrated-video", enabled=True,
                              exclude_from=[])
    with patch.object(mcp_registry, "get_agent_mcps",
                      return_value=[_voice_manifest(tmp_path)]):
        skills = mcp_registry.get_skills_for_agent("pa", context="phone")
    assert [s[0] for s in skills] == ["narrated-video"]


def test_missing_skill_file_is_skipped(temp_db, tmp_path):
    m = _manifest_with_skills(tmp_path, "m2", [
        {"id": "ok-skill", "file": "skills/ok.md", "content": "OK body."},
    ])
    m.skills.append(mcp_registry.SkillDef(id="ghost", file="skills/none.md"))
    with patch.object(mcp_registry, "get_agent_mcps", return_value=[m]):
        skills = mcp_registry.get_skills_for_agent("pa", context="dashboard")
    assert [s[0] for s in skills] == ["ok-skill"]
