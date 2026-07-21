"""Prompt-injection tests for the `# Memory` sections.

Covers the scope loading matrix:
user-scoped sessions → agent + user memory; agent-scoped (no username) →
agent only; internal agents → read-only reference (no directive, no section
when empty); per-scope inline vs index-only vs empty-state priming; toggle
gating; and the no-double-load guarantee (the generic context loaders never
see the memory dirs).
"""

from __future__ import annotations

from pathlib import Path


import config as app_config
from services.memory import memory_file
from storage import agent_store, memory_store


def _seed_agent(slug: str, *, default_scope: str = "user") -> Path:
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(
            slug, slug.title(), default_scope=default_scope,
        )
    agent_dir = app_config.AGENTS_DIR / slug
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "prompt.md").write_text("You are a test agent.")
    return agent_dir


def _write_topic(agent_dir: Path, scope: str, name: str, text: str,
                 username: str | None = None) -> None:
    root = memory_file.scope_root(agent_dir, scope, username)
    memory_file.op_create(root, name, text)


def _prompt(slug: str, **kwargs) -> str:
    return app_config.build_agent_prompt(slug, **kwargs) or ""


# ---------------------------------------------------------------------------
# Loading matrix
# ---------------------------------------------------------------------------

def test_user_session_gets_both_scopes_inline(temp_db):
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "infra.md", "# Prod cluster is main-eu\n")
    _write_topic(agent_dir, "user", "prefs.md", "# Prefers metric\n",
                 username="alice")
    p = _prompt("acme", username="alice", role="manager")
    assert "# Memory" in p
    assert "## Agent memory (shared)" in p
    assert "## User memory (alice)" in p
    assert "Prod cluster is main-eu" in p
    assert "Prefers metric" in p
    # Directive present with the trained tool name + default scope.
    assert "`memory` tool" in p
    assert "Default scope for new memories: `/memories/user/`." in p
    assert "Maintain, don't accumulate" in p


def test_agent_scoped_session_gets_agent_only(temp_db):
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "infra.md", "# Shared fact\n")
    _write_topic(agent_dir, "user", "prefs.md", "# Private fact\n",
                 username="alice")
    p = _prompt("acme")  # no username
    assert "## Agent memory (shared)" in p
    assert "## User memory" not in p
    assert "Private fact" not in p
    assert "Default scope for new memories: `/memories/agent/`." in p


def test_empty_scopes_render_priming(temp_db):
    _seed_agent("acme")
    p = _prompt("acme", username="alice", role="manager")
    # Both subsections render the writable empty-state (priming matters).
    assert p.count("_No memories saved yet. Create the first topic") == 2


def test_viewer_gets_readonly_note_and_neutral_empty_state(temp_db):
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "user", "mine.md", "# Mine\n", username="bob")
    p = _prompt("acme", username="bob", role="viewer")
    assert "read-only for your role" in p
    # Agent scope is empty → neutral empty state (no "create" instruction).
    assert "_No memories saved yet._" in p
    # User scope still renders their content.
    assert "# Mine" in p


# ---------------------------------------------------------------------------
# Inline vs index mode
# ---------------------------------------------------------------------------

def test_over_budget_scope_degrades_to_index(temp_db):
    agent_dir = _seed_agent("acme")
    memory_store.update_settings(inline_budget_bytes=64)
    _write_topic(agent_dir, "agent", "big.md",
                 "# Big topic\n" + "x" * 200 + "\n")
    p = _prompt("acme", username="alice", role="manager")
    # Index mode: the entry line appears, the raw body does not.
    assert "- big.md — Big topic (updated " in p
    assert "x" * 200 not in p
    assert "index only" in p


def test_index_mode_heals_stale_index(temp_db):
    agent_dir = _seed_agent("acme")
    memory_store.update_settings(inline_budget_bytes=64)
    _write_topic(agent_dir, "agent", "big.md",
                 "# Old heading\n" + "x" * 200 + "\n")
    # Hand-edit the topic (simulates dashboard/satellite edit): index stale.
    import os, time
    root = memory_file.scope_root(agent_dir, "agent")
    (root / "big.md").write_text("# Hand-edited heading\n" + "y" * 200 + "\n")
    future = time.time() + 5
    os.utime(root / "big.md", (future, future))
    p = _prompt("acme", username="alice", role="manager")
    assert "Hand-edited heading" in p  # healed at injection time


# ---------------------------------------------------------------------------
# Toggles + internal agents
# ---------------------------------------------------------------------------

def test_toggles_gate_sections(temp_db):
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "a.md", "# Shared\n")
    _write_topic(agent_dir, "user", "u.md", "# Private\n", username="alice")
    memory_store.set_agent_toggle("acme", "agent_memory_enabled", False)
    p = _prompt("acme", username="alice", role="manager")
    assert "## Agent memory" not in p
    assert "## User memory (alice)" in p
    memory_store.update_settings(user_memory_enabled=False)
    p = _prompt("acme", username="alice", role="manager")
    assert "# Memory" not in p


def test_agent_scope_session_renders_writable_agent_memory(temp_db):
    # A service session (no human user) renders the agent-scope memory directive
    # + the `memory` tool — agent memory is writable (the memory MCP is active in
    # agent scope). Replaces the old read-only "internal agent" reference, which
    # was dead code (client_type="internal" was never set).
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "ops.md", "# Op fact\n")
    p = _prompt("acme", username=None, role="manager")
    assert "Op fact" in p
    assert "`memory` tool:" in p
    assert "/memories/agent/" in p
    assert "## User memory" not in p  # no human user → no per-user scope


# ---------------------------------------------------------------------------
# No double-load: the generic context loaders must not see memory dirs
# ---------------------------------------------------------------------------

def test_memory_content_appears_exactly_once(temp_db):
    agent_dir = _seed_agent("acme")
    sentinel_agent = "UNIQUE-AGENT-FACT-93b1"
    sentinel_user = "UNIQUE-USER-FACT-7c42"
    _write_topic(agent_dir, "agent", "facts.md", f"# {sentinel_agent}\n")
    _write_topic(agent_dir, "user", "facts.md", f"# {sentinel_user}\n",
                 username="alice")
    # A REGULAR user-context doc still loads via the generic loader.
    ctx_dir = agent_dir / "users" / "alice" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "personal-info.md").write_text("REGULAR-CONTEXT-DOC")
    p = _prompt("acme", username="alice", role="manager")
    assert p.count(sentinel_agent) == 1
    assert p.count(sentinel_user) == 1
    assert "REGULAR-CONTEXT-DOC" in p
    # The generated index files never inject as context docs.
    assert "# Memory index (auto-generated" not in p
