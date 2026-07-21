"""Agent-name path-traversal guard for prompt building.

``build_agent_prompt`` / ``_read_agent_files`` join the agent name onto
``AGENTS_DIR``. Agent names are slugs at creation, but request endpoints
(e.g. ``GET /v1/agents/{name}/config``) pass the raw path segment through,
so the name is validated here as a barrier: anything that could escape the
agent tree is treated as an unknown agent.
"""

from __future__ import annotations

import config as app_config


class TestIsSafeAgentName:
    def test_accepts_real_slugs(self):
        for good in ("otodock-developer", "system-admin", "a", "agent1", "x_y.z"):
            assert app_config.is_safe_agent_name(good), good

    def test_rejects_traversal_and_separators(self):
        for bad in (
            "", "..", "../etc", "a/../b", "a/b", "a\\b",
            "..%2f", "foo/..", "/abs", ".hidden", "a\x00b", "-lead",
        ):
            assert not app_config.is_safe_agent_name(bad), bad


class TestPromptBuildRejectsTraversal:
    def test_read_agent_files_empty_for_unsafe_name(self, tmp_path, monkeypatch):
        # Even if a prompt.md exists at the traversal target, an unsafe name
        # must never read it.
        monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents")
        outside = tmp_path / "outside" / "config"
        outside.mkdir(parents=True)
        (outside / "prompt.md").write_text("SECRET PROMPT")
        assert app_config._read_agent_files("../outside") == []

    def test_build_agent_prompt_none_for_unsafe_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents")
        assert app_config.build_agent_prompt("../../etc") is None
        assert app_config.build_agent_prompt("a/b") is None
