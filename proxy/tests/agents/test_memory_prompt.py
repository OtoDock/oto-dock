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
from storage.agents import agent_store, memory_store
from tests.fixtures.start_text import prompt_at_start


def _seed_agent(slug: str, *, default_scope: str = "user") -> Path:
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(
            slug, slug.title(), default_scope=default_scope,
        )
    agent_dir = app_config.AGENTS_DIR / slug
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("You are a test agent.")
    return agent_dir


def _write_topic(agent_dir: Path, scope: str, name: str, text: str,
                 username: str | None = None) -> None:
    root = memory_file.scope_root(agent_dir, scope, username)
    memory_file.op_create(root, name, text)


def _prompt(slug: str, **kwargs) -> str:
    # The memory rides on the start: read with the text, as the model reads it.
    return prompt_at_start(slug, **kwargs)


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
    # The directive, once: the tool, the default, the scope rule, the sizes.
    assert "`memory` tool" in p
    assert "Default for new memories: `/memories/user/`. About a person" in p
    assert ("the fact goes shared and the preference personal. A process the team "
            "follows is knowledge, not memory.") in p
    assert "Keep it under 16 KB: the tool refuses growth past 32 KB and an edit that adds over 2 KB." in p
    assert "the engine's own memory is off" in p


def test_agent_scoped_session_gets_agent_only(temp_db):
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "infra.md", "# Shared fact\n")
    _write_topic(agent_dir, "user", "prefs.md", "# Private fact\n",
                 username="alice")
    p = _prompt("acme")  # no username
    assert "## Agent memory (shared)" in p
    assert "## User memory" not in p
    assert "Private fact" not in p
    assert "Default for new memories: `/memories/agent/`." in p


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

def test_over_budget_scope_lists_the_topic_with_its_entry(temp_db):
    agent_dir = _seed_agent("acme")
    memory_store.update_settings(inline_budget_bytes=64)
    _write_topic(agent_dir, "agent", "big.md",
                 "# Big topic\nWhat it holds.\n\n## Part\n- " + "x" * 200 + " (2026-10-01)\n")
    p = _prompt("acme", username="alice", role="manager")
    # Listed: the entry with its facts and sections, never the raw body.
    assert "### Topics listed, not loaded" in p
    assert "- big.md: Big topic (" in p and "newest 2026-10-01" in p
    assert "  What it holds." in p and "  · L4 Part" in p
    assert "x" * 200 not in p
    assert "or a section with `view_range`" in p


def test_index_mode_heals_stale_index(temp_db):
    agent_dir = _seed_agent("acme")
    memory_store.update_settings(inline_budget_bytes=64)
    _write_topic(agent_dir, "agent", "big.md",
                 "# Old heading\n" + "x" * 200 + "\n")
    # Hand-edit the topic (simulates dashboard/satellite edit): index stale.
    import os
    import time
    root = memory_file.scope_root(agent_dir, "agent")
    (root / "big.md").write_text("# Hand-edited heading\n" + "y" * 200 + "\n")
    future = time.time() + 5
    os.utime(root / "big.md", (future, future))
    p = _prompt("acme", username="alice", role="manager")
    assert "Hand-edited heading" in p  # the listing reads the file
    # The on-disk index is healed in the same pass, so `view` agrees.
    index = (root / "MEMORY.md").read_text()
    assert index.startswith(memory_file.INDEX_HEADER) and "Hand-edited heading" in index


def test_an_old_index_is_healed_at_the_prompt_build_even_when_the_scope_inlines(temp_db):
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "a.md", "# Alpha\n- a (2026-10-01)\n")
    root = memory_file.scope_root(agent_dir, "agent")
    (root / "MEMORY.md").write_text("# Memory index (auto-generated)\n- a.md — Alpha (updated 2026-10-01)\n")
    p = _prompt("acme", username="alice", role="manager")
    assert "### `a.md`" in p and "Topics listed" not in p
    assert (root / "MEMORY.md").read_text().startswith(memory_file.INDEX_HEADER)


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
    assert "`memory` tool" in p
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
    assert "# Memory index v2" not in p


def test_contributor_gets_readonly_note(temp_db):
    """The shared memory is the editor tier's to write: a contributor reads
    it and saves user-scope memories, the same note a viewer gets."""
    _seed_agent("acme")
    p = _prompt("acme", username="bob", role="contributor")
    assert "Agent memory is read-only for your role: save user-scope memories only." in p
    assert "About a person" not in p  # no scope choice to make
    assert "## Agent memory (shared)" in p


# ---------------------------------------------------------------------------
# The directive's variants and the graded fill
# ---------------------------------------------------------------------------

def test_the_directive_names_the_caps_from_the_settings(temp_db):
    _seed_agent("acme")
    memory_store.update_settings(topic_grow_cap_bytes=40960, entry_cap_bytes=3000)
    p = _prompt("acme", username="alice", role="manager")
    assert "refuses growth past 40 KB and an edit that adds over 3,000 bytes." in p


def test_one_scope_alone_gets_the_default_line_without_the_scope_rule(temp_db):
    _seed_agent("acme")
    memory_store.set_agent_toggle("acme", "agent_memory_enabled", False)
    p = _prompt("acme", username="alice", role="manager")
    assert "Default for new memories: `/memories/user/`.\n" in p
    assert "About a person" not in p and "read-only" not in p


def _fill_agent(days: dict[str, tuple[int, str]]) -> Path:
    """Topics named by their newest date: ``{name: (bytes, date)}``."""
    agent_dir = _seed_agent("acme")
    for name, (size, when) in days.items():
        body = f"# {name}\nAbout {name}.\n\n## Part\n- noted ({when})\n"
        body += "- " + "x" * max(0, size - len(body) - 3) + "\n"
        _write_topic(agent_dir, "agent", f"{name}.md", body)
    return agent_dir


def test_the_graded_fill_loads_the_freshest_topics_that_fit_and_lists_the_rest(temp_db):
    memory_store.update_settings(inline_budget_bytes=1500)
    _fill_agent({
        "fresh-small": (600, "2026-10-27"),
        "fresh-big": (1200, "2026-10-26"),
        "older-small": (500, "2026-09-01"),
        "undated": (300, "2026-99-99"),
    })
    p = _prompt("acme", username="alice", role="manager")
    body = p.split("## Agent memory (shared)")[1]
    inlined = [line for line in body.splitlines() if line.startswith("### `")]
    # First fit by freshness: the 600 and the 500 fit (1,100 of 1,500), the
    # 1,200 does not and the undated 300 lands last and fits too.
    assert inlined == ["### `fresh-small.md`", "### `older-small.md`", "### `undated.md`"]
    listed = body.split("### Topics listed, not loaded")[1]
    assert "- fresh-big.md: fresh-big (" in listed and "newest 2026-10-26" in listed
    assert "· L4 Part" in listed
    assert "- fresh-small.md:" not in listed


def test_a_topic_over_the_hard_cap_is_listed_whatever_the_budget(temp_db):
    memory_store.update_settings(inline_budget_bytes=200_000)
    agent_dir = _seed_agent("acme")
    root = memory_file.scope_root(agent_dir, "agent")
    root.mkdir(parents=True, exist_ok=True)
    (root / "huge.md").write_text("# Huge\n" + "- y (2026-10-01)\n" * 5000)
    (root / "small.md").write_text("# Small\n- z (2026-10-02)\n")
    p = _prompt("acme", username="alice", role="manager")
    assert "### `small.md`" in p and "### `huge.md`" not in p
    assert "- huge.md: Huge (" in p


def test_a_topic_whose_read_fails_is_listed_not_dropped(temp_db, monkeypatch):
    memory_store.update_settings(inline_budget_bytes=200_000)
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "locked.md", "# Locked\n- l (2026-10-03)\n")
    _write_topic(agent_dir, "agent", "open.md", "# Open\n- o (2026-10-02)\n")
    from services.infra import safe_fs
    real_read = safe_fs.read_bytes_beneath

    def read_bytes_beneath(root, rel, **kwargs):
        if str(rel).endswith("locked.md"):
            raise PermissionError(13, "Permission denied", str(rel))
        return real_read(root, rel, **kwargs)
    monkeypatch.setattr(safe_fs, "read_bytes_beneath", read_bytes_beneath)
    p = _prompt("acme", username="alice", role="manager")
    assert "### `open.md`" in p and "### `locked.md`" not in p
    listed = p.split("### Topics listed, not loaded")[1]
    assert "- locked.md: Locked (" in listed


def test_a_topic_swapped_for_a_link_after_the_listing_is_listed_not_read(temp_db, monkeypatch, tmp_path):
    """The topics are listed first and read after: a topic that became a link
    in between is never read through it, and stays listed."""
    from services.memory import memory_index
    memory_store.update_settings(inline_budget_bytes=200_000)
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "swapped.md", "# Swapped\n- s (2026-10-03)\n")
    _write_topic(agent_dir, "agent", "open.md", "# Open\n- o (2026-10-02)\n")
    stand_in = tmp_path / "stand-in.md"
    stand_in.write_text("# Swapped\n- STAND-IN TEXT (2026-10-03)\n")
    real_facts = memory_index.scope_facts

    def facts_then_swap(root, **kwargs):
        out = real_facts(root, **kwargs)
        topic = root / "swapped.md"
        if topic.is_file() and not topic.is_symlink():
            topic.unlink()
            topic.symlink_to(stand_in)
        return out
    monkeypatch.setattr(memory_index, "scope_facts", facts_then_swap)
    p = _prompt("acme", username="alice", role="manager")
    assert "### `open.md`" in p and "### `swapped.md`" not in p
    assert "STAND-IN TEXT" not in p
    assert "- swapped.md: Swapped (" in p.split("### Topics listed, not loaded")[1]



def test_a_topic_that_grew_past_its_cap_after_the_listing_is_listed_not_read(temp_db, monkeypatch):
    """The topics are listed first and read after: a topic that grew past
    the hard cap in between is not loaded, and stays listed."""
    from services.memory import memory_index
    memory_store.update_settings(inline_budget_bytes=200_000)
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "grown.md", "# Grown\n- g (2026-10-03)\n")
    _write_topic(agent_dir, "agent", "open.md", "# Open\n- o (2026-10-02)\n")
    real_facts = memory_index.scope_facts

    def facts_then_grow(root, **kwargs):
        out = real_facts(root, **kwargs)
        topic = root / "grown.md"
        if topic.is_file() and topic.stat().st_size <= memory_file.TOPIC_HARD_CAP_BYTES:
            topic.write_text("# Grown\n" + "x" * (memory_file.TOPIC_HARD_CAP_BYTES + 1))
        return out
    monkeypatch.setattr(memory_index, "scope_facts", facts_then_grow)
    p = _prompt("acme", username="alice", role="manager")
    assert "### `open.md`" in p and "### `grown.md`" not in p
    assert "- grown.md: Grown (" in p.split("### Topics listed, not loaded")[1]


def test_an_agent_whose_folder_is_a_link_loads_its_memory(temp_db, tmp_path):
    """The operator may move an agent's folder and leave a link in its place:
    its topics are read beneath the real folder."""
    real = tmp_path / "disk2" / "moved"
    real.mkdir(parents=True)
    (app_config.AGENTS_DIR / "moved").symlink_to(real)
    _seed_agent("moved")
    memory_store.update_settings(inline_budget_bytes=200_000)
    (real / "knowledge" / "memory").mkdir(parents=True)
    (real / "knowledge" / "memory" / "infra.md").write_text("# Prod cluster is main-eu\n")
    p = _prompt("moved", username="alice", role="manager")
    assert "### `infra.md`" in p and "Prod cluster is main-eu" in p

# ---------------------------------------------------------------------------
# The index rewrite at a prompt build never drops the section
# ---------------------------------------------------------------------------

OLD_INDEX = "# Memory index (auto-generated)\n- a.md — Alpha (updated 2026-10-01)\n"


def test_a_folder_the_proxy_cannot_write_keeps_its_index_and_the_section(temp_db, caplog):
    import logging
    import os
    agent_dir = _seed_agent("acme")
    _write_topic(agent_dir, "agent", "a.md", "# Alpha\n- a (2026-10-01)\n")
    root = memory_file.scope_root(agent_dir, "agent")
    (root / "MEMORY.md").write_text(OLD_INDEX)
    os.chmod(root, 0o555)
    try:
        with caplog.at_level(logging.WARNING, logger="otodock.prompts"):
            p = _prompt("acme", username="alice", role="manager")
    finally:
        os.chmod(root, 0o755)
    assert "# Memory" in p and "You have persistent memory" in p
    assert "### `a.md`" in p and "- a (2026-10-01)" in p
    assert (root / "MEMORY.md").read_text() == OLD_INDEX
    assert "the index of acme (agent scope) was not rewritten" in caplog.text


def test_a_rewrite_that_raises_still_renders_the_rules_and_the_listing(temp_db, monkeypatch, caplog):
    import logging
    agent_dir = _seed_agent("acme")
    memory_store.update_settings(inline_budget_bytes=64)
    _write_topic(agent_dir, "agent", "big.md", "# Big\nWhat it holds.\n\n## Part\n- " + "x" * 200 + " (2026-10-01)\n")
    _write_topic(agent_dir, "user", "mine.md", "# Mine\n- m (2026-10-02)\n", username="alice")

    def boom(_root):
        raise memory_file.MemoryOpError("Error: the memory scope is outside the agents tree.")
    monkeypatch.setattr(memory_file, "heal_index_if_stale", boom)
    with caplog.at_level(logging.WARNING, logger="otodock.prompts"):
        p = _prompt("acme", username="alice", role="manager")
    assert "the fact goes shared and the preference personal" in p
    assert "### Topics listed, not loaded" in p and "- big.md: Big (" in p and "· L4 Part" in p
    assert "### `mine.md`" in p
    assert caplog.text.count("was not rewritten") == 2
    assert "acme (user scope, alice)" in caplog.text


def test_a_viewer_on_a_shared_first_agent_defaults_to_their_own_scope(temp_db):
    _seed_agent("acme-shared", default_scope="agent")
    p = _prompt("acme-shared", username="bob", role="viewer")
    assert ("Default for new memories: `/memories/user/`. Agent memory is read-only "
            "for your role: save user-scope memories only.") in p
    assert "`/memories/agent/`." not in p.split("# Memory")[1].split("\n\n")[0]
