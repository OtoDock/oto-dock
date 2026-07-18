"""skills_materializer — projection of on-demand skills into CLI config dirs.

Pins the audit-hardened reconciliation protocol:
identical-set computation is covered in test_skills_for_agent; here we
pin the filesystem behavior — staging+swap, digest-based tamper repair,
quarantine-not-delete, dot-entry immunity (Codex .system), legacy flat-file
synthesis, frontmatter scrub on the materialization path, and fail-soft.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

from core.sandbox import skills_materializer as sm
from services.mcp import mcp_registry


def _wire(tmp_path, skills):
    """Create skill sources on disk and patch the registry set-provider.

    ``skills`` entries: (skill_id, relfile, content, description).
    Returns the wanted-list the materializer will see.
    """
    src_root = tmp_path / "mcps" / "pkg"
    wanted = []
    for skill_id, relfile, content, desc in skills:
        f = src_root / relfile
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        wanted.append((skill_id, f, "pkg", "1.0.0", desc))
    return wanted


FOLDER_SKILL = """---
name: pdf-tricks
description: PDF manipulation tricks. Use for PDFs.
allowed-tools: Bash(qpdf:*)
---

# PDF tricks

Do the tricks.
"""


def test_folder_skill_materializes_scrubbed(tmp_path):
    wanted = _wire(tmp_path, [
        ("pdf-tricks", "skills/pdf-tricks/SKILL.md", FOLDER_SKILL, "PDF."),
    ])
    # A bundled reference file rides along.
    ref = tmp_path / "mcps/pkg/skills/pdf-tricks/references/REF.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("reference body")

    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)

    out = cfg / "skills" / "pdf-tricks"
    text = (out / "SKILL.md").read_text()
    assert "allowed-tools" not in text          # scrub is a security boundary
    assert "name: pdf-tricks" in text
    assert "# PDF tricks" in text
    assert (out / "references/REF.md").read_text() == "reference body"
    marker = json.loads((out / ".oto-skill.json").read_text())
    assert marker["package"] == "pkg" and marker["version"] == "1.0.0"


def test_legacy_flat_file_synthesizes_frontmatter(tmp_path):
    wanted = _wire(tmp_path, [
        ("voiceover", "skills/voiceover.md", "# Voice-overs\n\nPick a voice.\n",
         "Produce narrated voice-overs."),
    ])
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)

    text = (cfg / "skills/voiceover/SKILL.md").read_text()
    assert text.startswith("---\n")
    assert "name: voiceover" in text
    assert "description: Produce narrated voice-overs." in text
    assert "# Voice-overs" in text


def test_tamper_is_repaired_next_ensure(tmp_path):
    wanted = _wire(tmp_path, [
        ("pdf-tricks", "skills/pdf-tricks/SKILL.md", FOLDER_SKILL, "PDF."),
    ])
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)
        skill_md = cfg / "skills/pdf-tricks/SKILL.md"
        skill_md.write_text("HIJACKED INSTRUCTIONS")
        # Marker untouched — digest of the tree is what must catch this.
        sm.materialize_skills_for_sandbox("pa", cfg)
    assert "HIJACKED" not in skill_md.read_text()
    assert "# PDF tricks" in skill_md.read_text()


def test_source_edit_rematerializes(tmp_path):
    wanted = _wire(tmp_path, [
        ("voiceover", "skills/voiceover.md", "old body", "d"),
    ])
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)
        wanted[0][1].write_text("new body")
        sm.materialize_skills_for_sandbox("pa", cfg)
    assert "new body" in (cfg / "skills/voiceover/SKILL.md").read_text()


def test_disabled_skill_quarantined_not_deleted(tmp_path):
    wanted = _wire(tmp_path, [
        ("voiceover", "skills/voiceover.md", "body", "d"),
    ])
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=[]):
        sm.materialize_skills_for_sandbox("pa", cfg)
    assert not (cfg / "skills/voiceover").exists()
    q = list((cfg / "skills/.quarantine").iterdir())
    assert [p.name for p in q] == ["voiceover"]


def test_agent_written_stray_quarantined(tmp_path):
    cfg = tmp_path / ".claude"
    stray = cfg / "skills" / "self-made"
    stray.mkdir(parents=True)
    (stray / "SKILL.md").write_text("agent parallel memory")
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=[]):
        sm.materialize_skills_for_sandbox("pa", cfg)
    assert not stray.exists()
    assert (cfg / "skills/.quarantine/self-made/SKILL.md").exists()


def test_dot_entries_never_touched(tmp_path):
    cfg = tmp_path / ".codex"
    system = cfg / "skills" / ".system" / "imagegen"
    system.mkdir(parents=True)
    (system / "SKILL.md").write_text("codex builtin")
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=[]):
        sm.materialize_skills_for_sandbox("pa", cfg)
    assert (system / "SKILL.md").read_text() == "codex builtin"


def test_no_skills_no_dir_creates_nothing(tmp_path):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=[]):
        sm.materialize_skills_for_sandbox("pa", cfg)
    assert not (cfg / "skills").exists()


def test_missing_source_fail_soft(tmp_path):
    cfg = tmp_path / ".claude"
    wanted = [("ghost", tmp_path / "nope.md", "pkg", "1", "d")]
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)   # must not raise
    assert not (cfg / "skills/ghost").exists()


def test_registry_error_fail_soft(tmp_path):
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      side_effect=RuntimeError("db down")):
        sm.materialize_skills_for_sandbox("pa", cfg)   # must not raise


def test_symlink_in_source_not_followed_into_output(tmp_path):
    wanted = _wire(tmp_path, [
        ("pdf-tricks", "skills/pdf-tricks/SKILL.md", FOLDER_SKILL, "d"),
    ])
    secret = tmp_path / "secret.txt"
    secret.write_text("credential")
    (tmp_path / "mcps/pkg/skills/pdf-tricks/link.txt").symlink_to(secret)
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)
    out_link = cfg / "skills/pdf-tricks/link.txt"
    assert not out_link.is_symlink()


def test_concurrent_ensures_converge(tmp_path):
    wanted = _wire(tmp_path, [
        ("voiceover", "skills/voiceover.md", "body", "d"),
        ("pdf-tricks", "skills/pdf-tricks/SKILL.md", FOLDER_SKILL, "d"),
    ])
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        threads = [threading.Thread(
            target=sm.materialize_skills_for_sandbox, args=("pa", cfg))
            for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    names = sorted(p.name for p in (cfg / "skills").iterdir()
                   if not p.name.startswith("."))
    assert names == ["pdf-tricks", "voiceover"]
    assert not list((cfg / "skills").glob(".staging-*"))
    assert "# PDF tricks" in (cfg / "skills/pdf-tricks/SKILL.md").read_text()


def test_idempotent_second_run_no_rewrite(tmp_path):
    wanted = _wire(tmp_path, [
        ("voiceover", "skills/voiceover.md", "body", "d"),
    ])
    cfg = tmp_path / ".claude"
    with patch.object(mcp_registry, "get_on_demand_skills_for_materialization",
                      return_value=wanted):
        sm.materialize_skills_for_sandbox("pa", cfg)
        target = cfg / "skills/voiceover/SKILL.md"
        ino_before = target.stat().st_ino
        sm.materialize_skills_for_sandbox("pa", cfg)
        assert target.stat().st_ino == ino_before  # unchanged tree kept in place
