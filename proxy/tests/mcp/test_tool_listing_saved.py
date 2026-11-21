"""What a tool's program answered, kept for the next session
(``tool_listing.keep`` / ``saved`` / ``forget``): served only to the agent and
person it was saved for, never a source of labels, bounded in size, count and
age, gone with its agent, its tool's update and its person."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from auth.path_policy import SecurityContext
from services.mcp import mcp_permissions, tool_listing
from storage.pg import get_conn

AGENT = "saved-agent"
SCOPE = (AGENT, "alice")
TOOLS = [
    {"name": "look", "description": "Look.", "inputSchema": {"type": "object"},
     "annotations": {"title": "Look around", "readOnlyHint": False, "destructiveHint": True}},
    {"name": "unlabelled", "description": "?", "inputSchema": {"type": "object"},
     "annotations": {"readOnlyHint": True}},
]
LISTING = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "x", "version": "1"}, "instructions": "Hi.",
                          "extra": "dropped"},
           "lists": {"tools/list": TOOLS}}


@pytest.fixture
def agent(temp_db, monkeypatch):
    with get_conn() as conn:
        conn.execute("INSERT INTO agents (slug, display_name, created_at, updated_at) "
                     "VALUES (%s, 'S', '2026-11-10', '2026-11-10')", (AGENT,))
        conn.commit()
    mcp_permissions.reset_cache()
    monkeypatch.setattr(tool_listing, "schedule", lambda *a: None)
    yield
    mcp_permissions.reset_cache()


def test_a_kept_listing_is_served_to_its_scope_alone(agent):
    assert tool_listing.keep(SCOPE, "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    got = tool_listing.saved(SCOPE, "x-mcp", "fp1")
    assert got["initialize"] == {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                 "serverInfo": {"name": "x", "version": "1"}, "instructions": "Hi."}
    assert [t["name"] for t in got["lists"]["tools/list"]] == ["look", "unlabelled"]
    assert tool_listing.saved((AGENT, "bob"), "x-mcp", "fp1") is None
    assert tool_listing.saved((AGENT, ""), "x-mcp", "fp1") is None
    assert tool_listing.saved(SCOPE, "x-mcp", "fp2") is None
    # The other direction: the agent's own runs' answer, and another
    # person's, never reach this person.
    tool_listing.keep((AGENT, ""), "x-mcp", "fp3", json.loads(json.dumps(LISTING)))
    tool_listing.keep((AGENT, "bob"), "x-mcp", "fp4", json.loads(json.dumps(LISTING)))
    assert tool_listing.saved(SCOPE, "x-mcp", "fp3") is None
    assert tool_listing.saved(SCOPE, "x-mcp", "fp4") is None


def test_the_hints_served_are_the_platforms_labels_never_the_programs(agent):
    mcp_permissions.record_tool_hints("x-mcp", [("look", {"readOnlyHint": True})])
    tool_listing.keep(SCOPE, "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    tools = {t["name"]: t for t in tool_listing.saved(SCOPE, "x-mcp", "fp1")["lists"]["tools/list"]}
    # The platform's own label wins; the title the program sent stays.
    assert tools["look"]["annotations"] == {"title": "Look around", "readOnlyHint": True}
    # No label known: the program's own hint is not served.
    assert "annotations" not in tools["unlabelled"]
    # And keeping the answer labelled nothing.
    assert mcp_permissions.tool_hints("x-mcp", "unlabelled") is None


def test_an_unlabelled_tool_the_listing_job_reaches_is_listed_once_by_it(agent, monkeypatch):
    from services.mcp import mcp_registry
    scheduled = []
    monkeypatch.setattr(tool_listing, "schedule", lambda agent, name: scheduled.append((agent, name)))

    class _M:
        name, placement = "x-mcp", "any"
    monkeypatch.setattr(mcp_permissions, "manifest_for_server", lambda server: _M())
    tool_listing.keep(SCOPE, "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    assert scheduled == [(AGENT, "x-mcp")]
    del mcp_registry


@pytest.mark.parametrize("bad", [
    None, {}, {"initialize": "x"}, {"initialize": {}, "lists": {"tools/list": "x"}},
    {"initialize": {}, "lists": {"tools/list": [{"name": 3}]}},
    {"initialize": {}, "lists": {"tools/list": [{"name": "a", "description": "x" * (2 * 1024 * 1024)}]}},
    # A server that declares tools and lists none: saved, its stand-in would
    # refuse every call by name and never start it again.
    {"initialize": {"capabilities": {"tools": {}}}, "lists": {"tools/list": []}},
    {"initialize": {"capabilities": {"tools": {}}}, "lists": {}},
])
def test_a_malformed_or_oversized_answer_is_not_kept(agent, bad):
    assert tool_listing.keep(SCOPE, "x-mcp", "fp1", bad) is False
    assert tool_listing.saved(SCOPE, "x-mcp", "fp1") is None


def _age(fingerprint: str, days: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE mcp_tool_listings SET used_at=%s WHERE fingerprint=%s", (old, fingerprint))
        conn.commit()


def test_at_most_eight_fingerprints_per_person_and_tool(agent):
    for i in range(10):
        assert tool_listing.keep(SCOPE, "x-mcp", f"fp{i}", json.loads(json.dumps(LISTING)))
    with get_conn() as conn:
        rows = conn.execute("SELECT fingerprint FROM mcp_tool_listings").fetchall()
    assert len(rows) == tool_listing.KEEP_PER_TOOL


def test_none_unused_for_a_month_at_the_next_save_or_the_daily_pass(agent):
    for fp in ("old", "kept", "other"):
        tool_listing.keep(SCOPE, "x-mcp" if fp != "other" else "y-mcp", fp, json.loads(json.dumps(LISTING)))
    _age("old", 31)
    tool_listing.keep(SCOPE, "x-mcp", "fresh", json.loads(json.dumps(LISTING)))
    assert tool_listing.saved(SCOPE, "x-mcp", "old") is None
    assert tool_listing.saved(SCOPE, "x-mcp", "kept") is not None
    # The daily pass takes what no save of that scope and tool ever reaches.
    _age("other", 31)
    assert tool_listing.sweep_unused(dry_run=True) == 1
    assert tool_listing.saved(SCOPE, "y-mcp", "other") is not None
    _age("other", 31)
    assert tool_listing.sweep_unused() == 1
    assert tool_listing.saved(SCOPE, "y-mcp", "other") is None


def test_the_retention_sweep_runs_the_pass(agent):
    from services.infra import retention
    tool_listing.keep(SCOPE, "x-mcp", "old", json.loads(json.dumps(LISTING)))
    _age("old", 31)
    stats = retention._run_sweep_sync(180, False, retention._build_live_snapshot(), False)
    assert stats["tool_listings_deleted"] == 1


def test_a_full_keep_in_a_day_is_named_in_the_log(agent, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="otodock.mcp"):
        for i in range(tool_listing.KEEP_PER_TOOL - 1):
            tool_listing.keep(SCOPE, "x-mcp", f"fp{i}", json.loads(json.dumps(LISTING)))
    # A person on several engines and machine kinds holds several by design.
    assert not any("fingerprints" in r.getMessage() for r in caplog.records)
    with caplog.at_level(logging.WARNING, logger="otodock.mcp"):
        tool_listing.keep(SCOPE, "x-mcp", "fp-last", json.loads(json.dumps(LISTING)))
    assert any(f"gathered {tool_listing.KEEP_PER_TOOL} fingerprints" in r.getMessage() for r in caplog.records)


def test_saved_answers_go_with_their_tool_their_person_and_their_agent(agent):
    tool_listing.keep(SCOPE, "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    tool_listing.keep((AGENT, "bob"), "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    tool_listing.keep(SCOPE, "y-mcp", "fp1", json.loads(json.dumps(LISTING)))
    outside = tool_listing.outside_person("phone", "+302101111111")
    tool_listing.keep((AGENT, outside), "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    # One outside person on one agent: that agent's rows alone.
    assert tool_listing.forget(person=outside, agent="another-agent") == 0
    assert tool_listing.forget(person=outside, agent=AGENT) == 1
    assert tool_listing.forget(person="alice") == 2
    assert tool_listing.saved((AGENT, "bob"), "x-mcp", "fp1") is not None
    assert tool_listing.forget(server="x-mcp") == 1
    tool_listing.keep(SCOPE, "x-mcp", "fp1", json.loads(json.dumps(LISTING)))
    from storage.agents import agent_store
    agent_store.delete_agent(AGENT)
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM mcp_tool_listings").fetchone()["n"] == 0


def test_the_scope_is_the_agent_and_the_username_or_one_outside_person_or_none():
    person = SecurityContext(role="editor", username="alice", agent="a", is_admin_agent=False)
    run = SecurityContext(role="manager", username="", agent="a", is_admin_agent=False)
    outside = SecurityContext(role="external", username="", agent="a", is_admin_agent=False,
                              principal="external", external_channel="phone", external_id="+302101111111")
    other = SecurityContext(role="external", username="", agent="a", is_admin_agent=False,
                            principal="external", external_channel="phone", external_id="+302102222222")
    assert tool_listing.scope_of(person) == ("a", "alice")
    assert tool_listing.scope_of(run) == ("a", "")
    assert tool_listing.scope_of(outside) == ("a", "outside:phone:+302101111111")
    assert tool_listing.scope_of(other) != tool_listing.scope_of(outside)


def test_the_stamp_carries_the_platforms_version_for_a_bundled_tool(monkeypatch):
    from pathlib import Path

    import config

    class _M:
        version, label, name = "1.2.0", "X", "x-mcp"
        mcp_dir = Path(config.MCPS_DIR) / "custom" / "x-mcp"
    monkeypatch.setattr(config, "PINNED_OTODOCK_VERSION", "2.0.0")
    assert tool_listing.stamp_of(_M()) == "1.2.0+2.0.0"
    _M.mcp_dir = Path(config.MCPS_DIR) / "community" / "x-mcp"
    assert tool_listing.stamp_of(_M()) == "1.2.0"


def test_a_docker_tools_record_on_the_real_store(agent, tmp_path):
    """The platform's record of a Docker tool and a person's record of one
    with a sign-in, on the store itself: the platform's reads back, neither is
    read as the other, the account is part of the fingerprint, and the
    admin's "Keep running" is stored and cleared."""
    from types import SimpleNamespace

    from storage.mcp import mcp_store
    # The platform's record reads back, its labels the platform's.
    assert tool_listing.keep_platform("dock-mcp", "fpA", json.loads(json.dumps(LISTING)), [AGENT])
    got = tool_listing.saved_platform("dock-mcp", "fpA")
    assert got is not None and [t["name"] for t in got["lists"]["tools/list"]] == ["look", "unlabelled"]
    assert tool_listing.saved_platform("dock-mcp", "fpB") is None
    # A person's record is never the platform's, nor the platform's a person's.
    assert tool_listing.keep(SCOPE, "dock-mcp", "fpP", json.loads(json.dumps(LISTING)))
    assert tool_listing.saved_platform("dock-mcp", "fpP") is None
    assert tool_listing.saved(SCOPE, "dock-mcp", "fpA") is None
    assert tool_listing.saved((AGENT, tool_listing.PLATFORM_PERSON), "dock-mcp", "fpP") is None
    # The account is folded into the fingerprint: none, and two accounts, give three.
    (tmp_path / "docker-compose.yml").write_text("services:\n  dock:\n    image: x\n")
    manifest = SimpleNamespace(name="dock-mcp", version="1.0.0", mcp_dir=tmp_path,
                               server=SimpleNamespace(docker_compose="docker-compose.yml"))
    fps = {tool_listing.container_fingerprint(manifest, account)
           for account in ("", "user-a|work", "user-a|home")}
    assert len(fps) == 3
    assert tool_listing.container_fingerprint(manifest, "user-a|work") == \
        tool_listing.container_fingerprint(manifest, "user-a|work")
    # The admin's switch, stored and cleared.
    mcp_store.ensure_mcp_state("dock-mcp", True)
    mcp_store.set_keep_running("dock-mcp", True)
    assert "dock-mcp" in mcp_store.get_keep_running()
    mcp_store.set_keep_running("dock-mcp", False)
    assert "dock-mcp" not in mcp_store.get_keep_running()
