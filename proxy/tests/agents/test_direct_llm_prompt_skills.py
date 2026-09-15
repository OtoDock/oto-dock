"""The ``# Skills`` prompt section — the on-demand skill catalog a Direct-LLM
session needs because its ``Skill`` builtin (not a CLI skill index) is the
activation surface. CLI sessions never get it (their CLI indexes the
materialized skills dir itself)."""

from __future__ import annotations

from unittest.mock import patch

import config as app_config
from services.mcp import mcp_registry
from storage.agents import agent_store


def _seed_agent(slug: str) -> None:
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(slug, slug.title())
    agent_dir = app_config.AGENTS_DIR / slug
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("You are a test agent.")


_CATALOG = [
    ("photo-editing-guide", "Lightroom-style photo editing recipes"),
    ("task-scheduling-guide", ""),
]


def test_direct_llm_prompt_lists_on_demand_skills(temp_db):
    _seed_agent("skillcat")
    with patch.object(mcp_registry, "get_skill_catalog_for_agent", return_value=_CATALOG) as cat:
        p = app_config.build_agent_prompt(
            "skillcat", username="alice", role="manager",
            client_type="dashboard", execution_path="direct-llm",
        ) or ""
    assert "# Skills" in p
    assert "`Skill` tool" in p
    assert "- `photo-editing-guide` — Lightroom-style photo editing recipes" in p
    assert "- `task-scheduling-guide`\n" in p + "\n"
    # The catalog is context-filtered like the inline skills.
    assert cat.call_args.kwargs["context"] == "dashboard"


def test_cli_prompts_never_get_the_section(temp_db):
    _seed_agent("skillcat")
    with patch.object(mcp_registry, "get_skill_catalog_for_agent", return_value=_CATALOG):
        for path in ("claude-code-cli", "codex-cli", ""):
            p = app_config.build_agent_prompt(
                "skillcat", username="alice", role="manager", execution_path=path,
            ) or ""
            assert "# Skills" not in p


def test_empty_catalog_omits_the_section(temp_db):
    _seed_agent("skillcat")
    with patch.object(mcp_registry, "get_skill_catalog_for_agent", return_value=[]):
        p = app_config.build_agent_prompt(
            "skillcat", username="alice", role="manager", execution_path="direct-llm",
        ) or ""
    assert "# Skills" not in p
