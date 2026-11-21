"""The Outside people picks as a session's build reads them: the grants per
server key, the store roundtrip, and the notice when a tool's maker changes
what Read only gives."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services.mcp import mcp_manifest_store, mcp_permissions, outside_picks
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
        "files-srv": {"read_document": _RO},
    })
    monkeypatch.setattr(mcp_permissions, "_hints_loaded", True)
    yield
    mcp_permissions.reset_cache()


def test_the_grants_per_server_key(labels):
    crm, files, calls, other = _mk("crm"), _mk("file-tools", server_name="files-srv"), _mk("call-control-mcp"), _mk("other")
    picks = outside_picks.OutsidePicks(
        tools={"crm": outside_picks.ToolPick("read_only"),
               "file-tools": outside_picks.ToolPick("custom", ("read_document", "list", "list")),
               "other": outside_picks.ToolPick("read_write")},
        channel=frozenset({"call-control-mcp"}),
    )
    rows = outside_picks.grants(picks, [crm, files, calls, other, _mk("unpicked")])
    assert rows == (("crm", ("find",)), ("files-srv", ("list", "read_document")),
                    ("call-control-mcp", None), ("other", None))
    assert outside_picks.allows(rows, "crm", "find") and not outside_picks.allows(rows, "crm", "update")
    assert not outside_picks.allows(rows, "crm", "bare")
    assert outside_picks.allows(rows, "other", "anything") and outside_picks.allows(rows, "call-control-mcp", "end_call")
    assert not outside_picks.allows(rows, "unpicked", "x") and outside_picks.granted(rows, "unpicked") == ()
    assert outside_picks.attached_keys(rows) == {"crm", "files-srv", "call-control-mcp", "other"}
    assert picks.admits("crm") and picks.admits("call-control-mcp") and not picks.admits("unpicked")
    # An unlisted server under Read only grants nothing.
    assert outside_picks.grants(picks, [_mk("other2")]) == ()
    picks2 = outside_picks.OutsidePicks(tools={"never-listed": outside_picks.ToolPick("read_only")})
    assert outside_picks.grants(picks2, [_mk("never-listed")]) == (("never-listed", ()),)


def test_nothing_admits_nothing():
    assert not outside_picks.NOTHING.admits("crm") and outside_picks.NOTHING.knowledge == ()
    assert outside_picks.grants(outside_picks.NOTHING, [_mk("crm")]) == ()


def test_load_reads_the_store(temp_db, labels):
    agent_store.create_agent("support", "Support", created_by="u")
    assert outside_picks.load("support") == outside_picks.NOTHING
    outside_store.update("support", upsert_tools=[
        {"mcp_name": "crm", "access": "read_only", "functions": [], "granted": ["find"]},
        {"mcp_name": "file-tools", "access": "custom", "functions": ["read_document"], "granted": []},
    ], files_access="read_write", web=True, add_knowledge=["faq"])
    picks = outside_picks.load("support", channel=["call-control-mcp"])
    assert picks.tools == {"crm": outside_picks.ToolPick("read_only"),
                           "file-tools": outside_picks.ToolPick("custom", ("read_document",))}
    assert picks.files_write and picks.web and picks.knowledge == ("faq",)
    assert picks.channel == {"call-control-mcp"}
    assert outside_store.read_only_picks_of("crm") == [("support", outside_store.OUTSIDE, ["find"], "")]


def test_a_label_change_marks_the_pick_and_tells_the_managers(temp_db, labels, monkeypatch):
    agent_store.create_agent("support", "Support", created_by="u")
    outside_store.update("support", upsert_tools=[
        {"mcp_name": "crm", "access": "read_only", "functions": [], "granted": ["find"]},
    ])
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {"crm": _mk("crm")})
    created: list[dict] = []
    monkeypatch.setattr("services.notifications.notification_manager.fire_from_thread",
                        lambda **kw: created.append(kw))
    # The same read-only set: nothing happens.
    outside_picks.labels_changed("crm", {"find": _RO}, {"find": _RO, "update": _WRITE})
    assert created == [] and outside_store.get_tool_picks("support")["crm"]["changed_at"] == ""
    # The maker marks update read-only and drops find: both sides are told.
    outside_picks.labels_changed("crm", {"find": _RO, "update": _WRITE}, {"find": _WRITE, "update": _RO})
    row = outside_store.get_tool_picks("support")["crm"]
    assert row["granted"] == ["update"] and row["changed_at"]
    assert len(created) == 1
    notice = created[0]
    assert notice["scope"] == "agent" and notice["target"] == "support" and notice["audience"] == "manager"
    assert "can use: update" in notice["body"] and "no longer use: find" in notice["body"]
    assert notice["title"] == outside_picks.OUTSIDE_CHANGE_NOTICE
    assert notice["agent_slug"] == "support" and notice["href"] == "/agents/support/config#external-identities"
    assert "Agent Settings, Configuration, External identities" in notice["body"]
    assert "outside people" not in (notice["title"] + notice["body"]).lower()
    # Recorded through record_tool_hints itself.
    mcp_permissions.record_tool_hints("crm", [("find", _RO), ("update", _RO)])
    assert outside_store.get_tool_picks("support")["crm"]["granted"] == ["find", "update"]
    assert len(created) == 2


def test_the_fake_manifest_shape_matches(labels):
    assert outside_picks.server_key(SimpleNamespace(name="a", server_name="")) == "a"
    assert outside_picks.server_key(SimpleNamespace(name="a", server_name="b")) == "b"


@pytest.mark.parametrize("pick,parts", [
    ("faq", ("faq",)),
    ("projects/alpha", ("projects", "alpha")),
    ("shared/brand/guides/2026", ("shared", "brand", "guides", "2026")),
    ("projects/memory-notes", ("projects", "memory-notes")),
])
def test_a_knowledge_pick_names_a_folder_inside_the_knowledge(pick, parts):
    assert outside_picks.knowledge_parts(pick) == parts


@pytest.mark.parametrize("pick", [
    "", "/", "/faq", "faq/", "a//b", "a/./b", "a/../b", "../outside", "..", ".",
    "memory", "memory/topic", "projects/memory", "shared/brand/memory",
    ".credentials", ".credentials/github", "projects/.hidden",
    "shared", "a\x00b", None, 3,
])
def test_a_knowledge_pick_never_names_the_memory_the_credentials_or_a_path_out(pick):
    assert outside_picks.knowledge_parts(pick) is None


def test_a_library_pick_lies_inside_an_attachment():
    attached = [{"source_agent": "brand", "subdir": "guides"}, ("ops", "", False)]
    assert outside_picks.library_of(("shared", "brand", "guides"), attached) == ("brand", "guides")
    assert outside_picks.library_of(("shared", "brand", "guides", "2026"), attached) == ("brand", "guides")
    assert outside_picks.library_of(("shared", "ops"), attached) == ("ops", "")
    assert outside_picks.library_of(("shared", "ops", "runbooks", "db"), attached) == ("ops", "")
    # The source's root when only a subfolder is attached, a sibling, a prefix
    # that is not a folder boundary, another source, and no source at all.
    assert outside_picks.library_of(("shared", "brand"), attached) is None
    assert outside_picks.library_of(("shared", "brand", "logos"), attached) is None
    assert outside_picks.library_of(("shared", "brand", "guides-old"), attached) is None
    assert outside_picks.library_of(("shared", "sales", "x"), attached) is None
    assert outside_picks.library_of(("shared",), attached) is None
    assert outside_picks.library_of(("faq",), attached) is None


def test_load_keeps_only_the_knowledge_picks_the_rule_and_the_attachments_admit(temp_db, monkeypatch):
    agent_store.create_agent("support", "Support", created_by="u")
    outside_store.update("support", add_knowledge=["faq", "projects/alpha", "shared/brand/guides/2026",
                                                    "shared/sales", "memory", ".credentials", "/faq"])
    monkeypatch.setattr("storage.knowledge.db_knowledge_libraries.attachments_for_consumer",
                        lambda agent: [{"source_agent": "brand", "subdir": "guides", "writable": False, "name": ""}])
    assert outside_picks.load("support").knowledge == ("faq", "projects/alpha", "shared/brand/guides/2026")


# ── apps opened to external identities ────────────────────────────────────

def _app(slug, *, agent="support", username="", kind="folder", methods=None, files=None, approve=True):
    """An app row of ``agent``: exported methods (name to floor) and a files
    block, approved by default."""
    import json

    from storage import database as task_store
    blocks = {}
    if methods is not None:
        blocks["exports"] = json.dumps({"methods": {
            m: ({"description": m, "min_role": f} if f else {"description": m}) for m, f in methods.items()}})
    if files is not None:
        blocks["files"] = json.dumps(files)
    row = task_store.upsert_app(agent, username, None, slug, title=slug.title(),
                                rel_path=f"workspace/apps/{slug}", kind=kind, blocks=blocks or None)
    if approve:
        assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "u")
    return task_store.get_app(row["id"])


def test_an_app_is_on_offer_only_while_it_serves_external_identities(temp_db):
    agent_store.create_agent("support", "Support", created_by="u")
    agent_store.create_agent("other", "Other", created_by="u")
    booking = _app("booking", methods={"book": "external", "admin": "editor"})
    assert outside_picks.app_methods(booking) == ["book"]
    assert outside_picks.app_problem(booking, "support", ()) == ""
    assert outside_picks.app_refusal(booking, "other") == "This app is not an app of this agent."
    mine = _app("mine", username="alice", methods={"book": "external"})
    assert "shared with the whole agent" in outside_picks.app_refusal(mine, "support")
    page = _app("page", kind="file")
    assert "no server" in outside_picks.app_refusal(page, "support")
    waiting = _app("waiting", methods={"book": "external"}, approve=False)
    assert outside_picks.app_problem(waiting, "support", ()) == "This app waits for approval."
    members = _app("members", methods={"list": None})
    assert outside_picks.app_problem(members, "support", ()) == "This app opens nothing to external identities yet."
    reads = _app("reads", methods={"book": "external"}, files={"read": ["knowledge/faq/prices"]})
    assert "knowledge/faq/prices" in outside_picks.app_problem(reads, "support", ())
    assert outside_picks.app_problem(reads, "support", ("faq",)) == ""
    assert outside_picks.app_refusal(None, "support") == "This app is not an app of this agent."


@pytest.mark.parametrize("files, knowledge, problem", [
    ({"read": ["workspace/reports"]}, ("faq",), "workspace/reports"),
    ({"read": ["knowledge/faq"]}, ("faq",), ""),
    ({"read": ["knowledge/faq/2026"]}, ("faq",), ""),
    ({"read": ["knowledge/faq"]}, ("faq/2026",), "knowledge/faq"),
    ({"read": ["knowledge/prices"]}, ("faq",), "knowledge/prices"),
    ({"read": ["knowledge/shared/lib-a/guides"]}, ("shared/lib-a",), ""),
    ({"read": ["knowledge/faq"], "write": ["workspace/out"]}, ("faq",), "workspace/out"),
    ({}, (), ""),
])
def test_the_folder_rule_admits_only_knowledge_the_picks_admit(files, knowledge, problem):
    import json

    from services.apps import app_folders
    assert app_folders.outside_folder_problem({"files": json.dumps(files)}, knowledge) == problem


def test_load_keeps_the_app_picks_that_serve_and_rides_the_apps_tool(temp_db):
    agent_store.create_agent("support", "Support", created_by="u")
    booking = _app("booking", methods={"book": "external"})
    members = _app("members", methods={"list": None})
    assert outside_picks.load("support").apps == () and outside_picks.APPS_MCP not in outside_picks.load("support").channel
    outside_store.update("support", add_apps=[{"app_id": booking["id"], "methods": ["book"]},
                                              {"app_id": members["id"], "methods": []}])
    picks = outside_picks.load("support")
    assert picks.apps == (booking["id"],)
    assert outside_picks.APPS_MCP in picks.channel and picks.admits(outside_picks.APPS_MCP)
    grants = outside_picks.grants(picks, [_mk(outside_picks.APPS_MCP, server_name="outside-apps")])
    assert grants == (("outside-apps", None),)
    outside_store.update("support", remove_apps=[booking["id"]])
    assert outside_picks.load("support").apps == ()


def test_a_release_that_changes_an_apps_methods_tells_the_managers_once(temp_db, monkeypatch):
    import json

    from storage import database as task_store
    agent_store.create_agent("support", "Support", created_by="u")
    booking = _app("booking", methods={"book": "external"})
    outside_store.update("support", add_apps=[{"app_id": booking["id"], "methods": ["book"]}])
    created: list[dict] = []
    monkeypatch.setattr("services.notifications.notification_manager.fire_from_thread",
                        lambda **kw: created.append(kw))
    outside_picks.app_released(booking)
    assert created == []
    row = task_store.upsert_app("support", "", None, "booking", blocks={"exports": json.dumps({"methods": {
        "book": {"description": "b", "min_role": "external"},
        "cancel": {"description": "c", "min_role": "external"}}})})
    outside_picks.app_released(row)
    outside_picks.app_released(row)
    assert len(created) == 1
    notice = created[0]
    assert notice["title"] == outside_picks.OUTSIDE_APP_CHANGE_NOTICE and notice["audience"] == "manager"
    assert "can use: cancel" in notice["body"] and notice["href"] == "/agents/support/config#external-identities"
    assert outside_store.get_app_picks("support")[booking["id"]]["changed_at"]


def test_an_app_open_to_external_identities_cannot_be_approved_reading_a_workspace_folder(temp_db):
    agent_store.create_agent("support", "Support", created_by="u")
    reads = _app("reads", methods={"book": "external"}, files={"read": ["workspace/reports"]}, approve=False)
    # Neither picked nor linked: the release is the app's own business.
    assert outside_picks.approval_refusal(reads) == ""
    outside_store.update("support", add_apps=[{"app_id": reads["id"], "methods": ["book"]}])
    refused = outside_picks.approval_refusal(reads)
    assert "cannot read workspace/reports" in refused and outside_picks.CARD_PLACE in refused
    assert refused.startswith("This app is open to external identities") and "take the app off" in refused
    # Open through a link alone: the words name the link and how to stop it.
    from storage.sharing import share_store
    linked = _app("linked", methods={"book": "external"}, files={"read": ["workspace/reports"]}, approve=False)
    share_store.create_external_share(target_kind="app", target_id=linked["id"], created_by="user-admin",
                                      token_hash="hash-linked", public=True)
    refused = outside_picks.approval_refusal(linked)
    assert refused.startswith("This app has a shared link, so it cannot read workspace/reports.")
    assert "Or stop the link first" in refused
    # A picked app soft-unpinned from the agent's apps says so.
    from storage import database as task_store
    task_store.set_app_hidden(reads["id"], True)
    assert outside_picks.app_refusal(task_store.get_app(reads["id"]), "support") == \
        "This app was removed from the agent's apps."
    # A file app is never judged: its link reaches no platform method.
    page = _app("page", kind="file", files={"read": ["workspace/reports"]}, approve=False)
    outside_store.update("support", add_apps=[{"app_id": page["id"], "methods": []}])
    assert outside_picks.approval_refusal(page) == ""
