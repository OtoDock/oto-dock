"""``skip_http_mcps`` — a phone Direct-LLM session never connects the
sidecar (HTTP-transport) MCPs, so its prompt must not describe them: the
MCP catalog, the inline skills and the ``# Skills`` catalog all drop those
manifests when the flag is set, and ``build_agent_prompt`` forwards it."""

from __future__ import annotations

from unittest.mock import patch

import config as app_config
from services.mcp import mcp_registry
from storage.agents import agent_store


def _manifest(tmp_path, name, transport, skills):
    mcp_dir = tmp_path / name
    (mcp_dir / "skills").mkdir(parents=True, exist_ok=True)
    defs = []
    for sk in skills:
        (mcp_dir / sk["file"]).write_text(sk.pop("content"))
        defs.append(mcp_registry.SkillDef(**sk))
    return mcp_registry.McpManifest(
        name=name, label=name.title(), description=f"{name} tools.", version="1.0.0",
        category="custom",
        server=mcp_registry.ServerConfig(runtime="docker" if transport == "http" else "python",
                                         transport=transport, command="", args=[]),
        credentials=mcp_registry.CredentialConfig(type="none"),
        config=[], env={}, agent_env={}, exclude_from=[], skills=defs, mcp_dir=mcp_dir,
    )


def _pair(tmp_path):
    sidecar = _manifest(tmp_path, "file-tools", "http", [
        {"id": "file-tools-usage", "file": "skills/ft.md", "content": "FT card", "loading": "always"},
        {"id": "photo-editing-guide", "file": "skills/pe.md", "content": "PE", "loading": "on_demand"},
    ])
    stdio = _manifest(tmp_path, "memory-mcp", "stdio", [
        {"id": "memory-usage", "file": "skills/m.md", "content": "MEM card", "loading": "always"},
        {"id": "memory-guide", "file": "skills/mg.md", "content": "MG", "loading": "on_demand"},
    ])
    return [sidecar, stdio]


def test_catalog_skills_and_skill_catalog_drop_http_manifests(temp_db, tmp_path):
    with patch.object(mcp_registry, "get_agent_mcps", return_value=_pair(tmp_path)):
        full = mcp_registry.build_available_mcps_section("pa", context="phone")
        lean = mcp_registry.build_available_mcps_section("pa", context="phone", skip_http_mcps=True)
        assert "`file-tools`" in full and "`file-tools`" not in lean and "`memory-mcp`" in lean

        ids = [s[0] for s in mcp_registry.get_skills_for_agent("pa", context="phone", skip_http_mcps=True)]
        assert ids == ["memory-usage", "memory-guide"]
        assert mcp_registry.get_skill_catalog_for_agent("pa", context="phone", skip_http_mcps=True) == [
            ("memory-guide", ""),
        ]
        # sse / stdio / none are NOT sidecars — only "http" is dropped.
        assert not mcp_registry._is_http_transport(_pair(tmp_path)[1])
        assert mcp_registry._is_http_transport(_pair(tmp_path)[0])


def test_build_agent_prompt_forwards_the_flag(temp_db):
    if not agent_store.agent_exists("phonebot"):
        agent_store.create_agent("phonebot", "Phonebot")
    agent_dir = app_config.AGENTS_DIR / "phonebot"
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("You answer calls.")
    with patch.object(mcp_registry, "build_available_mcps_section", return_value="") as cat, \
         patch.object(mcp_registry, "get_skills_for_agent", return_value=[]) as sk, \
         patch.object(mcp_registry, "get_skill_catalog_for_agent", return_value=[]) as skc:
        app_config.build_agent_prompt(
            "phonebot", username=None, role="viewer", client_type="phone",
            execution_path="direct-llm", skip_http_mcps=True,
        )
    assert cat.call_args.kwargs["skip_http_mcps"] is True
    assert sk.call_args.kwargs["skip_http_mcps"] is True
    assert skc.call_args.kwargs["skip_http_mcps"] is True
