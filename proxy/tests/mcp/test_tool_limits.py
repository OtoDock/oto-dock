"""The tool limits as a session's build reads them (``services/mcp/tool_limits``):
the word a session takes, the grants per server key, what a Read only limit
keeps when the labels move, the exclusion of a server left with nothing, the
refusal sentence and the prompt's section."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import config
from services.mcp import (
    mcp_manifest_store,
    mcp_permissions,
    mcp_session_filter,
    outside_picks,
    tool_limits,
)
from services.mcp.mcp_manifest_types import CredentialConfig, McpManifest, ServerConfig
from storage.agents import agent_store, outside_store

_RO = {"readOnlyHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": True}


def _mk(name, *, server_name=""):
    return McpManifest(
        name=name, label=name.title(), description="", version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], server_name=server_name,
        mcp_dir=Path(config.MCPS_DIR) / "custom" / name,
    )


@pytest.fixture
def labels(monkeypatch):
    monkeypatch.setattr(mcp_permissions, "_hints", {
        "crm": {"find": _RO, "update": _WRITE, "bare": {}},
        "mail-srv": {"read_mail": _RO, "list_labels": _RO, "send_mail": _WRITE},
    })
    monkeypatch.setattr(mcp_permissions, "_hints_loaded", True)
    yield
    mcp_permissions.reset_cache()


def test_the_word_a_session_takes_is_by_its_identity():
    assert tool_limits.governs_of(external=True, person="u1") == tool_limits.OUTSIDE
    assert tool_limits.governs_of(external=False, person="u1") == tool_limits.CHATS
    assert tool_limits.governs_of(external=False, person=None) == tool_limits.RUNS
    assert tool_limits.governs_of(external=False, person="") == tool_limits.RUNS
    assert set(tool_limits.GOVERNS) == {"outside", "runs", "chats"}


def test_a_read_only_limit_keeps_the_set_it_was_saved_with(labels):
    crm = _mk("crm")
    saved = tool_limits.Limit("read_only", granted=("find", "bare"))
    # ``bare`` carries no label now, so it is off; ``find`` reads and stays.
    assert tool_limits.names_of(saved, "crm") == ("find",)
    # A function the maker labels read-only later is not in the saved set: off.
    mcp_permissions._hints["crm"]["update"] = _RO
    assert tool_limits.names_of(saved, "crm") == ("find",)
    assert tool_limits.grants({"crm": saved}, [crm, _mk("other")]) == (("crm", ("find",)),)
    custom = tool_limits.Limit("custom", functions=("update", "find", "find"))
    assert tool_limits.names_of(custom, "crm") == ("find", "update")


def test_a_server_left_with_nothing_is_left_out_by_the_one_rule(labels):
    unlisted = _mk("never-listed")
    reason = mcp_session_filter.session_exclusion_reason(
        unlisted, contexts={"dashboard"}, external=False,
        limits={"never-listed": tool_limits.Limit("read_only", granted=())})
    assert reason == tool_limits.LIMIT_UNLISTED_REASON
    crm = _mk("crm")
    reason = mcp_session_filter.session_exclusion_reason(
        crm, contexts={"dashboard"}, external=False,
        limits={"crm": tool_limits.Limit("read_only", granted=("update",))})
    assert reason == tool_limits.LIMIT_EMPTY_REASON
    assert mcp_session_filter.session_exclusion_reason(
        crm, contexts={"dashboard"}, external=False,
        limits={"crm": tool_limits.Limit("read_only", granted=("find",))}) is None
    # No limit named: admitted, as a reader that passes none.
    assert mcp_session_filter.session_exclusion_reason(crm, contexts={"dashboard"}, external=False) is None
    assert mcp_session_filter.session_exclusion_reason(
        crm, contexts={"dashboard"}, external=False, limits={"other": tool_limits.Limit("custom", ("x",))}) is None


def test_allows_refuses_only_a_limited_servers_other_functions(labels, monkeypatch):
    pairs = (("mail-srv", ("read_mail", "list_labels")),)
    assert tool_limits.allows(pairs, "mail-srv", "read_mail")
    assert not tool_limits.allows(pairs, "mail-srv", "send_mail")
    assert tool_limits.allows(pairs, "crm", "update")
    assert tool_limits.limited(pairs, "crm") is None and tool_limits.limited(pairs, "mail-srv") == ("read_mail", "list_labels")
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {"mail": _mk("mail", server_name="mail-srv")})
    assert tool_limits.refusal(pairs, "mail-srv", "read_mail") == ""
    sentence = tool_limits.refusal(pairs, "mail-srv", "send_mail")
    assert sentence == ("Mail is limited for this agent: it may not use send_mail. A manager can change this "
                        "in Agent Settings, Configuration, What the agent may use.")
    assert "—" not in sentence and ";" not in sentence
    section = tool_limits.prompt_section(pairs)
    assert section.startswith("# Tools you may use in part")
    assert "- Mail (`mail-srv`): you may use only read_mail, list_labels." in section
    assert "refused when called" in section
    assert tool_limits.prompt_section(()) == ""


def test_the_task_sessions_own_limit_names_the_two_tools():
    assert tool_limits.TASK_SESSION_LIMIT == (("agent-config-mcp", ("reload_session", "switch_account")),)
    assert tool_limits.allows(tool_limits.TASK_SESSION_LIMIT, "agent-config-mcp", "switch_account")
    assert not tool_limits.allows(tool_limits.TASK_SESSION_LIMIT, "agent-config-mcp", "update_persona")


def test_a_routes_refusal_is_the_hooks_for_the_tools_that_call_it(labels, monkeypatch):
    """A first-party tool's route is refused with the tool call's own
    sentence; a route several tools share stays open while one of them is
    allowed; the tools are named by the key the pairs carry."""
    from services.mcp import mcp_registry
    display = _mk("display-mcp", server_name="display")
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"display-mcp": display})
    pairs = (("mail-srv", ("read_mail",)), ("display", ("send_url",)))
    one = (("mail-srv", "send_mail"),)
    assert tool_limits.route_refusal(pairs, one) == tool_limits.refusal(pairs, "mail-srv", "send_mail")
    assert tool_limits.route_refusal(pairs, (("mail-srv", "read_mail"),)) == ""
    assert tool_limits.route_refusal(pairs, (("crm", "find"),)) == ""
    assert tool_limits.route_refusal(pairs, (("mail-srv", "send_mail"), ("crm", "find"))) == ""
    both_off = (("display", "display_images"), ("mail-srv", "send_mail"))
    assert tool_limits.route_refusal(pairs, both_off).startswith(
        "Display-Mcp is limited for this agent: it may not use display_images.")
    assert tool_limits.route_refusal((), one) == "" and tool_limits.route_refusal(pairs, ()) == ""


def test_a_forced_servers_limit_builds_no_pair(labels):
    """The meetings tool in a meeting and the call tools on a call are the
    channel's own: a row on them never reaches the session's pairs."""
    limits = {"meetings-mcp": tool_limits.Limit(tool_limits.CUSTOM, ("end_meeting",)),
              "mail-srv": tool_limits.Limit(tool_limits.CUSTOM, ("read_mail",))}
    attached = [_mk("meetings-mcp"), _mk("mail-srv")]
    assert tool_limits.grants(limits, attached) == (("meetings-mcp", ("end_meeting",)), ("mail-srv", ("read_mail",)))
    assert tool_limits.grants(limits, attached, forced=("meetings-mcp",)) == (("mail-srv", ("read_mail",)),)


def test_two_limits_on_one_server_give_what_both_allow():
    """A manager's row on the self-config tool and a run's own pair: one
    pair, their intersection, whichever came first; a second pair for a key
    never widens the first, combined or not."""
    manager = (("agent-config-mcp", ("get_agent_config", "update_persona", "reload_session", "switch_account")),
               ("mail-srv", ("read_mail",)))
    merged = tool_limits.merged(manager, tool_limits.TASK_SESSION_LIMIT)
    assert merged == (("agent-config-mcp", ("reload_session", "switch_account")), ("mail-srv", ("read_mail",)))
    assert tool_limits.merged(tool_limits.TASK_SESSION_LIMIT, manager) == (
        ("agent-config-mcp", ("reload_session", "switch_account")), ("mail-srv", ("read_mail",)))
    # A manager row of the tools that read: nothing a run may call, so none.
    read_only = (("agent-config-mcp", ("get_agent_config", "list_context_files")),)
    assert tool_limits.merged(read_only, tool_limits.TASK_SESSION_LIMIT) == (("agent-config-mcp", ()),)
    assert tool_limits.merged((), ()) == () and tool_limits.merged(None, manager) == manager
    # Pairs a caller did not combine: the readers still see what every one allows.
    raw = manager + tool_limits.TASK_SESSION_LIMIT
    assert tool_limits.limited(raw, "agent-config-mcp") == ("reload_session", "switch_account")
    assert not tool_limits.allows(raw, "agent-config-mcp", "update_persona")
    assert tool_limits.allows(raw, "agent-config-mcp", "switch_account")
    assert tool_limits.limited(raw, "other") is None


def test_a_persons_own_task_on_a_shared_only_agent_takes_the_chats_column(temp_db, monkeypatch, tmp_path):
    """On a Shared-only agent an editor's own task resolves the agent's
    credentials and runs with no person's sub, yet it is the person's run:
    its limits are those of people's chats (``TaskIdentity.person``), as the
    person's own chat there."""
    from types import SimpleNamespace

    from core.config.task_config_builder import (
        build_task_agent_config,
        resolve_task_identity,
    )
    from storage.identity import db_users
    from tests._compute import serve
    from tests.fixtures import loop_builder_rig as rig_mod
    rig_mod.install(monkeypatch, tmp_path)
    # A compute machine serves the platform's place: the tasks build on it.
    serve(monkeypatch)
    db_users.set_user_agent_role("user-viewer", rig_mod.SHARED, "editor")
    outside_store.update(rig_mod.SHARED, upsert_tools=[
        {"mcp_name": "char-files", "access": "custom", "functions": ["read"], "granted": [], "governs": "chats"}])
    ident = resolve_task_identity(rig_mod.SHARED, "user", "user-viewer")
    assert (ident.scope, ident.creds_user_sub, ident.person) == ("agent", None, "user-viewer")
    assert tool_limits.governs_of(external=False, person=ident.person) == tool_limits.CHATS
    assert resolve_task_identity(rig_mod.SHARED, "agent", "user-viewer").person is None
    assert resolve_task_identity(rig_mod.AGENT, "user", "user-viewer").person == "user-viewer"
    task = SimpleNamespace(scope="user", created_by="user-viewer", task_type="scheduled",
                           notification_mode="manual", name="t", id="dyn-shared-limit",
                           override_execution_path="claude-code-cli")
    cfg = asyncio.run(build_task_agent_config(rig_mod.SHARED, task, "s-shared-limit"))
    assert ("char-files", ("read",)) in cfg.security_context.tool_limits
    agent_task = SimpleNamespace(scope="agent", created_by="user-admin", task_type="scheduled",
                                 notification_mode="manual", name="t", id="dyn-shared-agent",
                                 override_execution_path="claude-code-cli")
    cfg = asyncio.run(build_task_agent_config(rig_mod.SHARED, agent_task, "s-shared-agent"))
    assert not any(key == "char-files" for key, _n in cfg.security_context.tool_limits)


def test_load_reads_one_column_and_never_the_outside_one(temp_db, labels):
    agent_store.create_agent("limits-a", "Limits", created_by="u")
    outside_store.update("limits-a", upsert_tools=[
        {"mcp_name": "crm", "access": "read_only", "functions": [], "granted": ["find"]},
        {"mcp_name": "crm", "access": "custom", "functions": ["find"], "granted": [], "governs": "runs"},
        {"mcp_name": "mail", "access": "read_only", "functions": [], "granted": ["read_mail"], "governs": "chats"},
    ])
    assert tool_limits.load("limits-a", tool_limits.RUNS) == {"crm": tool_limits.Limit("custom", ("find",), ())}
    assert tool_limits.load("limits-a", tool_limits.CHATS) == {"mail": tool_limits.Limit("read_only", (), ("read_mail",))}
    assert tool_limits.load("limits-a", tool_limits.OUTSIDE) == {}
    assert outside_picks.load("limits-a").tools == {"crm": outside_picks.ToolPick("read_only")}
    outside_store.delete_agent_picks("limits-a")


def test_a_label_change_stamps_a_limit_but_keeps_its_set_and_tells_per_column(temp_db, labels, monkeypatch):
    agent_store.create_agent("limits-b", "Limits", created_by="u")
    outside_store.update("limits-b", upsert_tools=[
        {"mcp_name": "crm", "access": "read_only", "functions": [], "granted": ["find"]},
        {"mcp_name": "crm", "access": "read_only", "functions": [], "granted": ["find"], "governs": "runs"},
        {"mcp_name": "crm", "access": "read_only", "functions": [], "granted": [], "governs": "chats"},
    ])
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {"crm": _mk("crm")})
    created: list[dict] = []
    monkeypatch.setattr("services.notifications.notification_manager.fire_from_thread",
                        lambda **kw: created.append(kw))
    # The first listing fills the chats row saved while the server was unlisted.
    outside_picks.labels_changed("crm", {}, {"find": _RO, "update": _WRITE})
    rows = {g: (gr, st) for _a, g, gr, st in outside_store.read_only_picks_of("crm")}
    assert rows["chats"] == (["find"], "") and created == []
    # The maker marks update read-only and drops find.
    outside_picks.labels_changed("crm", {"find": _RO, "update": _WRITE}, {"find": _WRITE, "update": _RO})
    rows = {g: (gr, st) for _a, g, gr, st in outside_store.read_only_picks_of("crm")}
    assert rows["outside"][0] == ["update"] and rows["outside"][1]
    assert rows["runs"][0] == ["find"] and rows["runs"][1]
    assert rows["chats"][0] == ["find"] and rows["chats"][1]
    assert len(created) == 1
    body = created[0]["body"]
    assert "external identities can use: update" in body
    assert "For the agent's own runs, these now read too and stay off until you save the limit again: update." in body
    assert "For the agent's own runs, these no longer read and are off from the next session: find." in body
    assert "people's chats" in body
    assert created[0]["href"] == "/agents/limits-b/config#tool-limits"
    # With those labels in force the limit grants nothing: the server is
    # left out of a run (record_tool_hints writes the labels, then tells).
    mcp_permissions._hints["crm"] = {"find": _WRITE, "update": _RO}
    limit = tool_limits.load("limits-b", tool_limits.RUNS)["crm"]
    assert tool_limits.names_of(limit, "crm") == ()
    assert tool_limits.exclusion_reason(limit, "crm") == tool_limits.LIMIT_EMPTY_REASON
    outside_store.delete_agent_picks("limits-b")
