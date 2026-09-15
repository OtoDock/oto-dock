"""Prompt blocks for external sessions: identity, scope, folders, memory
and caller context — the caller's tree, never the shared memory."""

from __future__ import annotations

import pytest

from auth.path_policy import SecurityContext
from auth.path_prompt import build_permission_context


def _ctx(home: str, *, role="viewer", verified=False, ident="+302101234567") -> SecurityContext:
    return SecurityContext(
        role=role, username="", agent="ext-prompt", is_admin_agent=False,
        session_scope="agent", config_visible=False, knowledge_rw=(role == "manager"),
        principal="external", external_channel="phone", external_id=ident,
        external_home=home, external_verified=verified, external_claim=f"phone:{ident}",
    )


class TestPermissionContext:
    def test_with_tree(self, temp_db):
        text = build_permission_context(_ctx("/x/externals/phone/1"), execution_path="direct-llm")
        assert "external caller" in text and "+302101234567" in text
        assert "caller-ID" in text            # low assurance
        assert "/caller/workspace/" in text and "/caller/context/" in text
        assert "`/workspace/` (RO)" in text
        assert "/config" not in text
        assert "not available here" in text  # schedules / delegation etc.
        assert "Default writes for this session go to `/caller/workspace/`" in text

    def test_verified_manager_without_tree(self, temp_db):
        text = build_permission_context(_ctx("", role="manager", verified=True),
                                        execution_path="claude-code-cli")
        assert "verified by this line's PIN" in text
        assert "keeps no per-caller memory" in text
        assert "`/workspace/` (RW)" in text and "`/knowledge/` (RW)" in text
        assert "/caller" not in text
        assert "Default writes for this session go to `/workspace/`" in text

    def test_withheld_number(self, temp_db):
        text = build_permission_context(_ctx("/x", ident=""), execution_path="direct-llm")
        assert "withheld number" in text

    def test_codex_caller_is_told_it_has_no_shell(self, temp_db):
        # An external Codex session has no exec_command (config.toml
        # `shell_tool = false`); the scope block says so and points at the
        # file tools. Other engines get no such line.
        text = build_permission_context(_ctx("/x/externals/phone/1"), execution_path="codex-cli")
        assert "You have no shell on this line" in text and "file tools" in text
        for other in ("claude-code-cli", "direct-llm"):
            assert "no shell on this line" not in build_permission_context(
                _ctx("/x/externals/phone/1"), execution_path=other,
            )


class TestSystemPrompt:
    @pytest.fixture
    def agent(self, temp_db, tmp_path):
        import config
        from storage.agents import agent_store
        if not agent_store.agent_exists("ext-prompt"):
            agent_store.create_agent("ext-prompt", "Ext Prompt")
        agent_dir = config.get_agent_dir("ext-prompt")
        (agent_dir / "config").mkdir(parents=True, exist_ok=True)
        (agent_dir / "config" / "agent.md").write_text("# Ext Prompt\n\nSupport.\n")
        shared = agent_dir / "knowledge" / "memory"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "ops.md").write_text("# Ops\nSHARED-SECRET-FACT\n")
        home = agent_dir / "externals" / "phone" / "302101234567"
        (home / "context" / "memory").mkdir(parents=True)
        (home / "context" / "memory" / "prefs.md").write_text("# Prefs\nCALLER-FACT\n")
        (home / "context" / "notes.md").write_text("Prefers mornings.\n")
        return agent_dir, home

    def test_caller_memory_and_context_only(self, agent):
        import config
        _agent_dir, home = agent
        prompt = config.build_agent_prompt(
            "ext-prompt", client_type="phone", external=True, external_home=str(home),
        ) or ""
        assert "Caller memory (private to this caller)" in prompt
        assert "CALLER-FACT" in prompt
        assert "SHARED-SECRET-FACT" not in prompt and "Agent memory (shared)" not in prompt
        assert "# Caller Context" in prompt and "Prefers mornings." in prompt
        assert "/caller/workspace/" in prompt
        assert "Files for this caller go to `/caller/workspace/`" in prompt

    def test_external_without_tree_has_no_memory(self, agent):
        import config
        prompt = config.build_agent_prompt(
            "ext-prompt", client_type="phone", role="viewer", external=True,
        ) or ""
        assert "SHARED-SECRET-FACT" not in prompt and "CALLER-FACT" not in prompt
        assert "# Memory" not in prompt
        assert "This session has no writable folder." in prompt

    def test_non_external_sessions_still_see_shared_memory(self, agent):
        import config
        prompt = config.build_agent_prompt("ext-prompt", client_type="phone") or ""
        assert "SHARED-SECRET-FACT" in prompt
