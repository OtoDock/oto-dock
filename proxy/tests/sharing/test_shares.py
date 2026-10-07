"""Shares (SHARING.md): the internal half — who may share, the union in
``app_access``, the grantee's surfaces, the live-apps audience, hide-for-me
through the share row, suspend on soft unpin, expiry and revoke, the
directory switch, and the human-only gate on every share route.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.apps import apps as apps_api
from app import app
from auth.providers import UserContext, get_current_user
from services.apps import audience
from storage import database as task_store
from storage.sharing import share_store

client = TestClient(app)

AGENT = "share-agent"
OWNER = "owner-sub"
MEMBER = "member-sub"
OUTSIDER = "outsider-sub"
VIEWER = "viewer-sub"


def _user(sub: str = OWNER, role: str = "member", agents: tuple[str, ...] = (AGENT,),
          agent_roles: dict[str, str] | None = None, is_api_key: bool = False) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=list(agents),
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles,
                       is_api_key=is_api_key)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _people():
    """Four users: the owner (manager), a member (editor), an outsider with
    no agent at all, and a viewer of the agent."""
    for sub, name in ((OWNER, "Owner"), (MEMBER, "Member"),
                      (OUTSIDER, "Outsider"), (VIEWER, "Viewer")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    task_store.add_user_agent(MEMBER, AGENT, "editor", "test")
    task_store.add_user_agent(VIEWER, AGENT, "viewer", "test")
    from auth import rate_limiter
    rate_limiter._attempts.clear()
    audience._cache.clear()
    apps_api._fire_rate.clear()
    _as(_user())
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _username(sub: str) -> str:
    return task_store.get_username_by_sub(sub) or ""


def _personal(slug: str = "sched", owner: str = OWNER) -> dict:
    return task_store.upsert_app(AGENT, _username(owner), owner, slug, title=slug.title(),
                                 rel_path=f"users/{_username(owner)}/workspace/apps/{slug}.html")


def _shared(slug: str = "team") -> dict:
    return task_store.upsert_app(AGENT, "", None, slug, title=slug.title(),
                                 rel_path=f"workspace/apps/{slug}.html")


def _share(target_id: str, grantee: str, *, kind: str = "app", expires_in: str = "",
           expect: int = 200) -> dict:
    r = client.post("/v1/shares", json={"target_kind": kind, "target_id": target_id,
                                        "grantee": grantee, "expires_in": expires_in})
    assert r.status_code == expect, r.text
    return r.json()


def _notify_patch():
    return patch("services.notifications.notification_manager.fire_notification",
                 new=AsyncMock(return_value=[]))


# ───────────────────────── who may share ────────────────────────────────────


def test_owner_shares_personal_app_and_grantee_gains_access():
    row = _personal()
    outsider = _user(OUTSIDER, agents=(), agent_roles={})
    assert apps_api.app_access(row, outsider) is False
    with _notify_patch() as fired:
        body = _share(row["id"], OUTSIDER)
    assert body["share"]["grantee"]["sub"] == OUTSIDER
    assert body["share"]["scope"] == "internal" and body["share"]["state"] == "active"
    assert apps_api.app_access(row, outsider) is True
    # The grantee heard about it, with the page that opens the app.
    kwargs = fired.await_args.kwargs
    assert kwargs["target"] == OUTSIDER and kwargs["href"] == f"/apps/{row['id']}"
    assert kwargs["source"] == "share"


def test_viewer_cannot_share_a_shared_app_but_an_editor_can():
    row = _shared()
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    _share(row["id"], OUTSIDER, expect=403)
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    with _notify_patch():
        _share(row["id"], OUTSIDER)
    assert apps_api.app_access(row, _user(OUTSIDER, agents=(), agent_roles={})) is True


def test_only_the_owner_shares_a_personal_app():
    row = _personal()
    # An editor of the agent is not the owner of a colleague's personal app;
    # the row is the same 404 as missing to them.
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    _share(row["id"], OUTSIDER, expect=404)


def test_dock_pins_are_not_shareable():
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    row = task_store.upsert_scoped_app(AGENT, _username(OWNER), OWNER, "dock",
                                       scope_chat_id=chat_id, rel_path="x.html")
    body = _share(row["id"], OUTSIDER, expect=400)
    assert "through its chat" in body["detail"]


def test_self_owner_and_duplicate_grants_are_refused():
    row = _personal()
    _share(row["id"], OWNER, expect=400)
    with _notify_patch():
        _share(row["id"], OUTSIDER)
        r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                            "grantee": OUTSIDER})
    assert r.status_code == 409
    # A revoked grant may be re-created.
    share_id = share_store.list_target_shares("app", row["id"])[0]["id"]
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 200
    with _notify_patch():
        _share(row["id"], OUTSIDER)


def test_share_routes_are_human_only():
    row = _personal()
    for bearer in (_user(OWNER, is_api_key=True), _user("api-key", role="admin", is_api_key=True)):
        _as(bearer)
        assert client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                               "grantee": OUTSIDER}).status_code == 403
        assert client.get(f"/v1/shares?target_kind=app&target={row['id']}").status_code == 403
        assert client.patch("/v1/shares/x", json={"revoke": True}).status_code == 403
        assert client.get("/v1/users/directory").status_code == 403
    assert not share_store.list_target_shares("app", row["id"])


def test_links_need_the_confirm_and_a_chat_share_makes_a_snapshot(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    row = _personal()
    # No password on the account and no passkey: the link route says which
    # confirm it lacks instead of making the link.
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external"})
    assert r.status_code == 428 and r.json()["detail"]["method"] == "none"
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    with _notify_patch() as fired:
        r = client.post("/v1/shares", json={"target_kind": "chat", "target_id": chat_id,
                                            "grantee": OUTSIDER})
    assert r.status_code == 200, r.text
    share = share_store.get_share(r.json()["share"]["id"])
    assert share["target_kind"] == "chat" and share["chat_id"] == chat_id
    assert (tmp_path / share["snapshot_ref"] / "chat.json").is_file()
    assert fired.await_args.kwargs["href"] == f"/shared/{share['id']}"


def test_share_creation_is_rate_limited():
    row = _personal()
    with _notify_patch(), patch.dict("config.RATE_LIMIT_RULES", {
        "share_create": {"max": 1, "window": 3600, "base_block": 60, "max_block": 60}}):
        _share(row["id"], OUTSIDER)
        _share(row["id"], MEMBER, expect=429)


# ───────────────────────── the grantee's surfaces ───────────────────────────


def test_grantee_reads_serves_and_lists_the_app_without_agent_access(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    row = _personal()
    path = tmp_path / AGENT / row["rel_path"]
    path.parent.mkdir(parents=True)
    path.write_text("<p>hello</p>")
    task_store.write_app_state(row["id"], {"n": 1})
    with _notify_patch():
        _share(row["id"], OUTSIDER)
    _as(_user(OUTSIDER, agents=(), agent_roles={}))
    assert client.get(f"/v1/apps/{row['id']}").json()["can_manage"] is False
    assert client.get(f"/v1/apps/{row['id']}/html").status_code == 200
    assert client.get(f"/v1/apps/{row['id']}/state").json() == {"doc": {"n": 1}, "rev": 1}
    inbox = client.get("/v1/shares/inbox").json()
    assert [(i["target_id"], i["href"], i["shared_by"], i["actions"]) for i in inbox["items"]] == \
        [(row["id"], f"/apps/{row['id']}", OWNER, ["accept", "decline"])]
    assert inbox["pending"] == 1
    # Not a member: the per-agent list is closed to them, as before.
    assert client.get(f"/v1/apps?agent={AGENT}").status_code == 403


def test_member_sees_a_granted_personal_app_in_the_agent_list():
    row = _personal()
    with _notify_patch():
        _share(row["id"], MEMBER)
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    apps = client.get(f"/v1/apps?agent={AGENT}").json()["apps"]
    granted = [a for a in apps if a["granted"]]
    assert [a["id"] for a in granted] == [row["id"]]
    assert granted[0]["scope"] == "personal" and granted[0]["can_manage"] is False
    assert granted[0]["hidden_for_me"] is False


def test_grantee_hides_and_restores_through_the_share_row():
    row = _personal()
    with _notify_patch():
        _share(row["id"], MEMBER)
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    assert client.post(f"/v1/apps/{row['id']}/hide").status_code == 200
    assert share_store.grant_hidden("app", row["id"], MEMBER)
    apps = client.get(f"/v1/apps?agent={AGENT}").json()["apps"]
    assert [a["hidden_for_me"] for a in apps if a["id"] == row["id"]] == [True]
    assert client.get("/v1/shares/inbox").json()["items"] == []
    assert client.post(f"/v1/apps/{row['id']}/unhide").status_code == 200
    assert not share_store.grant_hidden("app", row["id"], MEMBER)
    # The section's x on their own share: the same hide, no body needed.
    share_id = client.get("/v1/shares/inbox").json()["items"][0]["id"]
    assert client.post(f"/v1/shares/{share_id}/hide").status_code == 200
    assert client.get("/v1/shares/inbox").json()["items"] == []
    assert client.post(f"/v1/shares/{share_id}/unhide").status_code == 200
    assert [i["id"] for i in client.get("/v1/shares/inbox").json()["items"]] == [share_id]
    # The owner's own hide request keeps today's answer.
    _as(_user())
    assert client.post(f"/v1/apps/{row['id']}/hide").status_code == 400


def test_audience_gains_the_grantee_and_drops_them_on_hide_and_revoke():
    row = _personal()
    assert audience.app_audience(row) == [OWNER]
    with _notify_patch():
        body = _share(row["id"], OUTSIDER)
    assert audience.app_audience(row) == sorted([OWNER, OUTSIDER])
    share_store.set_grant_hidden("app", row["id"], OUTSIDER, True)
    audience.forget(row["id"])
    assert audience.app_audience(row) == [OWNER]
    share_store.set_grant_hidden("app", row["id"], OUTSIDER, False)
    audience.forget(row["id"])
    assert OUTSIDER in audience.app_audience(row)
    assert client.patch(f"/v1/shares/{body['share']['id']}", json={"revoke": True}).status_code == 200
    # The revoke forgot the cache: no 2 s tail.
    assert audience.app_audience(row) == [OWNER]
    assert apps_api.app_access(row, _user(OUTSIDER, agents=(), agent_roles={})) is False


def test_grant_does_not_open_a_dock_pin_outside_its_chat():
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    row = task_store.upsert_scoped_app(AGENT, "", None, "dock", scope_chat_id=chat_id,
                                       rel_path="workspace/apps/dock.html")
    share_store.create_internal_share(target_kind="app", target_id=row["id"],
                                      grantee_sub=OUTSIDER, created_by=OWNER)
    # The grant is a union inside the scope gate, not around it.
    assert apps_api.app_access(row, _user(OUTSIDER, agents=(), agent_roles={})) is False


# ───────────────────────── lifecycle ────────────────────────────────────────


def test_soft_unpin_suspends_shares_and_resume_needs_the_row_back():
    row = _personal()
    with _notify_patch():
        share_id = _share(row["id"], OUTSIDER)["share"]["id"]
    outsider = _user(OUTSIDER, agents=(), agent_roles={})
    assert task_store.set_app_hidden(row["id"], True)
    assert share_store.get_share(share_id)["state"] == "suspended"
    assert share_store.internal_grant("app", row["id"], OUTSIDER) is None
    _as(outsider)
    assert client.get("/v1/shares/inbox").json() == {"items": [], "pending": 0}
    _as(_user())
    assert client.patch(f"/v1/shares/{share_id}", json={"resume": True}).status_code == 409
    # The re-pin restores the row, not the share; the sharer resumes it.
    task_store.upsert_app(AGENT, _username(OWNER), OWNER, "sched", rel_path=row["rel_path"])
    assert share_store.get_share(share_id)["state"] == "suspended"
    assert client.patch(f"/v1/shares/{share_id}", json={"resume": True}).json()["state"] == "active"
    assert apps_api.app_access(task_store.get_app(row["id"]), outsider) is True


def test_expiry_revoke_and_hard_unpin():
    row = _personal()
    with _notify_patch():
        share_id = _share(row["id"], OUTSIDER, expires_in="7d")["share"]["id"]
    share = share_store.get_share(share_id)
    until = datetime.fromisoformat(share["expires_at"])
    assert timedelta(days=6, hours=23) < until - datetime.now(timezone.utc) < timedelta(days=7, minutes=1)
    outsider = _user(OUTSIDER, agents=(), agent_roles={})
    share_store.set_share_expiry(share_id, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert apps_api.app_access(row, outsider) is False
    share_store.set_share_expiry(share_id, None)
    assert apps_api.app_access(row, outsider) is True
    # The hard unpin cascades the share with the row.
    assert task_store.delete_app(row["id"])
    assert share_store.get_share(share_id) is None


def test_expiry_cap_and_bad_specs():
    row = _personal()
    task_store.set_platform_setting("sharing_max_expiry_days", "30")
    _share(row["id"], OUTSIDER, expires_in="31d", expect=400)
    _share(row["id"], OUTSIDER, expires_in="soon", expect=400)
    with _notify_patch():
        body = _share(row["id"], OUTSIDER)
    # No expiry asked, a cap set: the cap applies.
    assert body["share"]["expires_at"] is not None


def test_never_is_no_expiry_and_is_refused_under_a_cap():
    """The explicit ``"never"`` means no expiry while no cap is set; under a
    cap it is refused, while an empty expiry (the form's "Never expires")
    is clamped to the cap as before."""
    row = _personal()
    with _notify_patch():
        share = _share(row["id"], OUTSIDER, expires_in="never")["share"]
    assert share["expires_at"] is None
    assert share_store.get_share(share["id"])["expires_at"] is None
    task_store.set_platform_setting("sharing_max_expiry_days", "30")
    _share(row["id"], MEMBER, expires_in="never", expect=400)
    with _notify_patch():
        clamped = _share(row["id"], MEMBER)["share"]
    until = datetime.fromisoformat(clamped["expires_at"])
    assert timedelta(days=29) < until - datetime.now(timezone.utc) <= timedelta(days=30)
    r = client.patch(f"/v1/shares/{clamped['id']}", json={"expires_in": "never"})
    assert r.status_code == 400 and r.json()["detail"] == "an admin capped share expiry at 30 days"
    r = client.patch(f"/v1/shares/{clamped['id']}", json={"expires_in": ""})
    assert r.status_code == 200 and r.json()["expires_at"] is not None


def test_sharing_settings_give_a_member_the_switches_and_nothing_else():
    """The share form needs the admin's longest expiry (whether a link may
    be offered with no expiry), the two target switches and whether the
    directory is open to the viewer; a member reads it, a bearer does not."""
    _as(_user(OUTSIDER, agents=(), agent_roles={}))
    open_all = {"max_expiry_days": None, "sharing_to_agents_enabled": True,
                "sharing_to_departments_enabled": True, "directory_open": True}
    assert client.get("/v1/sharing/settings").json() == open_all
    task_store.set_platform_setting("sharing_max_expiry_days", "30")
    task_store.set_platform_setting("sharing_to_agents_enabled", "0")
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    assert client.get("/v1/sharing/settings").json() == {
        **open_all, "max_expiry_days": 30, "sharing_to_agents_enabled": False,
        "directory_open": False}
    task_store.set_platform_setting("sharing_max_expiry_days", "soon")
    assert client.get("/v1/sharing/settings").json()["max_expiry_days"] is None
    _as(_user("root-sub", role="admin"))
    assert client.get("/v1/sharing/settings").json()["directory_open"] is True
    _as(_user(OUTSIDER, agents=(), agent_roles={}, is_api_key=True))
    assert client.get("/v1/sharing/settings").status_code == 403
    _as(None)
    assert client.get("/v1/sharing/settings").status_code == 401


def test_patch_authority_creator_or_target_authority():
    row = _shared()
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    with _notify_patch():
        share_id = _share(row["id"], OUTSIDER)["share"]["id"]
    # A viewer is neither the creator nor may share the target.
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 403
    # The owner-tier of the agent may revoke what an editor shared.
    _as(_user())
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 200
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 404


def test_a_demoted_creator_may_revoke_but_not_extend():
    row = _shared()
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    with _notify_patch():
        share_id = _share(row["id"], OUTSIDER, expires_in="7d")["share"]["id"]
    demoted = _user(MEMBER, agent_roles={AGENT: "viewer"})
    _as(demoted)
    assert client.patch(f"/v1/shares/{share_id}", json={"expires_in": ""}).status_code == 403
    assert client.patch(f"/v1/shares/{share_id}", json={"resume": True}).status_code == 403
    assert share_store.get_share(share_id)["expires_at"] is not None
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 200


def test_an_admin_revokes_a_link_whose_app_is_unpinned():
    row = _personal()
    with _notify_patch():
        share_id = _share(row["id"], OUTSIDER)["share"]["id"]
    assert task_store.set_app_hidden(row["id"], True)
    _as(_user("root-sub", role="admin"))
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 200
    assert share_store.get_share(share_id)["revoked_at"]


def test_list_shares_shows_grantee_names_and_needs_share_authority():
    row = _personal()
    with _notify_patch():
        _share(row["id"], MEMBER)
    shares = client.get(f"/v1/shares?target_kind=app&target={row['id']}").json()["shares"]
    assert [(s["grantee"]["sub"], s["grantee"]["name"]) for s in shares] == [(MEMBER, "Member")]
    # The grantee sees the app (403, not the 404 a stranger gets) but may
    # not read or change its shares.
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    assert client.get(f"/v1/shares?target_kind=app&target={row['id']}").status_code == 403
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    assert client.get(f"/v1/shares?target_kind=app&target={row['id']}").status_code == 404


# ───────────────────────── the directory ────────────────────────────────────


def test_directory_lists_others_and_closes_for_members_only():
    users = client.get("/v1/users/directory").json()["users"]
    assert OWNER not in {u["sub"] for u in users}
    assert {MEMBER, OUTSIDER, VIEWER} <= {u["sub"] for u in users}
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    assert client.get("/v1/users/directory").json()["users"] is None
    _as(_user("root-sub", role="admin"))
    assert client.get("/v1/users/directory").json()["users"]


def test_directory_off_gives_no_existence_oracle():
    row = _personal()
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    with _notify_patch():
        known = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                                "grantee": f"{OUTSIDER}@test.com"})
        unknown = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                                  "grantee": "nobody@test.com"})
        again = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                                "grantee": f"{OUTSIDER}@test.com"})
    assert known.status_code == unknown.status_code == again.status_code == 200
    assert known.json() == unknown.json() == again.json() == {"status": "ok"}
    # The grant landed all the same, visible in the sharer's list.
    assert [s["grantee"]["sub"] for s in
            client.get(f"/v1/shares?target_kind=app&target={row['id']}").json()["shares"]] == [OUTSIDER]
    # With the directory open, the unknown identifier is a plain 404.
    task_store.set_platform_setting("user_directory_visible_to_members", "1")
    assert client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                           "grantee": "nobody@test.com"}).status_code == 404


def test_admin_shares_list_is_admin_only():
    row = _personal()
    with _notify_patch():
        _share(row["id"], OUTSIDER)
    assert client.get("/v1/admin/shares").status_code == 403
    # A bearer is refused whatever its owner's role.
    _as(_user("root-sub", role="admin", is_api_key=True))
    assert client.get("/v1/admin/shares").status_code == 403
    _as(_user("root-sub", role="admin"))
    body = client.get("/v1/admin/shares").json()
    assert [(s["scope"], s["grantee"]["sub"], s["standing"]) for s in body["shares"]] == [
        ("internal", OUTSIDER, "waiting")]
    assert body["truncated"] == {"internal": False, "external": False}


# ───────────────────────── the store ────────────────────────────────────────


def test_scoped_repin_under_a_new_slug_keeps_the_row_and_its_shares():
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    first = task_store.upsert_scoped_app(AGENT, _username(OWNER), OWNER, "v1",
                                         scope_chat_id=chat_id, rel_path="a.html")
    task_store.write_app_state(first["id"], {"k": 1})
    share_store.create_internal_share(target_kind="app", target_id=first["id"],
                                      grantee_sub=OUTSIDER, created_by=OWNER)
    second = task_store.upsert_scoped_app(AGENT, _username(OWNER), OWNER, "v2",
                                          scope_chat_id=chat_id, rel_path="b.html")
    assert second["id"] == first["id"] and second["slug"] == "v2"
    assert task_store.get_app_state(first["id"]) == ({"k": 1}, 1)
    assert share_store.internal_grant("app", first["id"], OUTSIDER) is not None


def test_notification_row_carries_its_href():
    from storage.automation import notification_store
    row = notification_store.create_delivery(
        user_sub=OUTSIDER, title="t", body="b", severity="info", scope="user",
        source="share", href="/apps/abc",
    )
    assert row["href"] == "/apps/abc"
    from services.notifications import notification_manager
    assert notification_manager._build_delivery_payload(row)["href"] == "/apps/abc"


# ───────────────────────── agents, departments and placements ──────────────

RECEIVING = "share-target"
THIRD = "share-third"
NEWCOMER = "newcomer-sub"


def _agents():
    """Two more agents: the owner manages both, the member edits the first
    and views the second, the viewer views the first; a newcomer holds the
    first alone."""
    from storage.agents import agent_store
    for slug in (AGENT, RECEIVING, THIRD):
        if not agent_store.get_agent(slug):
            agent_store.create_agent(slug, slug.title(), created_by=OWNER)
    task_store.upsert_user(NEWCOMER, f"{NEWCOMER}@test.com", "Newcomer", "member")
    task_store.add_user_agent(OWNER, RECEIVING, "manager", "test")
    task_store.add_user_agent(OWNER, THIRD, "manager", "test")
    task_store.add_user_agent(MEMBER, RECEIVING, "editor", "test")
    task_store.add_user_agent(MEMBER, THIRD, "viewer", "test")
    task_store.add_user_agent(VIEWER, RECEIVING, "viewer", "test")
    task_store.add_user_agent(NEWCOMER, RECEIVING, "viewer", "test")


def _owner_everywhere() -> UserContext:
    return _user(OWNER, agents=(AGENT, RECEIVING, THIRD),
                 agent_roles={AGENT: "manager", RECEIVING: "manager", THIRD: "manager"})


def _member_everywhere() -> UserContext:
    return _user(MEMBER, agents=(AGENT, RECEIVING, THIRD),
                 agent_roles={AGENT: "editor", RECEIVING: "editor", THIRD: "viewer"})


def _newcomer() -> UserContext:
    return _user(NEWCOMER, agents=(RECEIVING,), agent_roles={RECEIVING: "viewer"})


def _share_to(target_id: str, kind: str, grantee: str, *, cap: str = "", expect: int = 200,
              target_kind: str = "app") -> dict:
    r = client.post("/v1/shares", json={"target_kind": target_kind, "target_id": target_id,
                                        "grantee_kind": kind, "grantee": grantee,
                                        "role_cap": cap})
    assert r.status_code == expect, r.text
    return r.json()


def _panel(agent: str) -> list[dict]:
    r = client.get(f"/v1/apps?agent={agent}")
    assert r.status_code == 200, r.text
    return r.json()["apps"]


def test_an_agent_share_lands_in_the_receiving_panel_and_its_editor_removes_it():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    body = _share_to(row["id"], "agent", RECEIVING, cap="editor")
    share = body["share"]
    assert share["grantee_kind"] == "agent" and share["to_agent"]["slug"] == RECEIVING
    assert share["decision"] == "accepted" and share["landed"] is True
    assert share["role_cap"] == "editor" and share["decided_by"] == OWNER
    # A member of the receiving agent alone sees it placed, with the
    # row's own agent, no management, and the capped role.
    newcomer = _newcomer()
    assert apps_api.app_access(row, newcomer) is True
    _as(newcomer)
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert [a["id"] for a in placed] == [row["id"]]
    p = placed[0]
    assert p["agent"] == AGENT and p["can_manage"] is False and p["can_approve"] is False
    assert p["placement"]["kind"] == "agent" and p["placement"]["share_id"] == share["id"]
    assert p["placement"]["from_agent"] == AGENT and p["placement"]["agent"] == RECEIVING
    assert p["placement"]["can_remove"] is False and p["hidden_for_me"] is False
    assert p["viewer_role"] == "viewer"   # capped by their row on the receiving agent
    # Never in the home agent's list twice, never for a bearer.
    _as(_member_everywhere())
    assert [a["id"] for a in _panel(AGENT) if a.get("placement")] == []
    me = [a for a in _panel(RECEIVING) if a["id"] == row["id"]][0]
    assert me["viewer_role"] == "editor" and me["placement"]["can_remove"] is True
    _as(_user(NEWCOMER, agents=(RECEIVING,), agent_roles={RECEIVING: "viewer"}, is_api_key=True))
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []
    # The viewer of the receiving agent may not remove it; its editor may
    # ("Remove from this agent" is the revoke).
    _as(_user(VIEWER, agents=(AGENT, RECEIVING), agent_roles={AGENT: "viewer", RECEIVING: "viewer"}))
    assert client.patch(f"/v1/shares/{share['id']}", json={"revoke": True}).status_code == 403
    _as(_member_everywhere())
    assert client.patch(f"/v1/shares/{share['id']}", json={"revoke": True}).status_code == 200
    _as(newcomer)
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []
    assert apps_api.app_access(row, newcomer) is False


def test_an_agent_share_takes_editor_or_above_on_the_receiving_agent_and_the_gates():
    _agents()
    row = _shared()
    personal = _personal()
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    # The member edits the source but only views the third agent.
    _as(_member_everywhere())
    _share_to(row["id"], "agent", THIRD, expect=403)
    _share_to(row["id"], "agent", RECEIVING, cap="manager", expect=400)   # above their role
    _share_to(row["id"], "agent", RECEIVING, cap="boss", expect=400)
    _as(_owner_everywhere())
    _share_to(row["id"], "agent", AGENT, expect=400)                      # the app's own agent
    _share_to(row["id"], "bogus", RECEIVING, expect=400)
    _share_to(personal["id"], "agent", RECEIVING, expect=400)             # personal: people only
    with _notify_patch():
        _share_to(chat_id, "agent", RECEIVING, target_kind="chat", expect=400)
    _share_to(row["id"], "agent", "no-such-agent", expect=404)
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    _as(_member_everywhere())
    assert _share_to(row["id"], "agent", "no-such-agent") == {"status": "ok"}
    task_store.set_platform_setting("user_directory_visible_to_members", "1")
    task_store.set_platform_setting("sharing_to_agents_enabled", "0")
    _as(_owner_everywhere())
    _share_to(row["id"], "agent", RECEIVING, expect=403)
    task_store.set_platform_setting("sharing_to_agents_enabled", "1")
    _share_to(row["id"], "agent", RECEIVING)
    _share_to(row["id"], "agent", RECEIVING, expect=409)


def test_a_department_share_lands_in_every_agent_and_follows_them():
    from services.departments import edge_compiler
    from storage.agents import agent_store, db_departments
    _agents()
    row = _shared()
    dept = db_departments.create_department("Engineering", "user-admin", level_names=["Head", "Staff"])
    head, staff = (lv["id"] for lv in dept["levels"])
    agent_store.update_agent(RECEIVING, department_id=dept["id"], department_level_id=head)
    agent_store.update_agent(THIRD, department_id=dept["id"], department_level_id=staff)
    edge_compiler.recompile()
    _as(_owner_everywhere())
    _share_to(row["id"], "department", dept["id"], expect=403)        # admins only
    _as(_user("user-admin", role="admin"))
    _share_to(row["id"], "department", "dept-nope", expect=404)
    task_store.set_platform_setting("sharing_to_departments_enabled", "0")
    _share_to(row["id"], "department", dept["id"], expect=403)
    task_store.set_platform_setting("sharing_to_departments_enabled", "1")
    share = _share_to(row["id"], "department", dept["id"])["share"]
    assert share["to_department"] == {"id": dept["id"], "name": "Engineering"}
    assert share_store.placement_for(row["id"], RECEIVING) == {
        "share_id": share["id"], "source": "department", "role_cap": "viewer"}
    assert share_store.placement_for(row["id"], THIRD)["share_id"] == share["id"]
    assert share_store.placement_for(row["id"], AGENT) is None
    _as(_member_everywhere())
    assert [a["placement"]["kind"] for a in _panel(THIRD) if a.get("placement")] == ["department"]
    # An agent moved out of the department loses the placement at the
    # next read; a department deleted ends the share.
    agent_store.update_agent(THIRD, department_id="", department_level_id="")
    assert share_store.placement_for(row["id"], THIRD) is None
    assert share_store.placement_for(row["id"], RECEIVING) is not None
    db_departments.delete_department(dept["id"])
    assert share_store.get_share(share["id"])["revoked_at"]
    assert share_store.placement_for(row["id"], RECEIVING) is None


def test_a_person_accepts_an_app_into_an_agent_of_theirs_and_leaving_it_unplaces():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch() as fired:
        share = _share_to(row["id"], "person", NEWCOMER, cap="editor")["share"]
    assert share["decision"] == "pending" and share["landed"] is False
    assert "Shared with you" in fired.await_args.kwargs["body"]
    assert fired.await_args.kwargs["href"] == f"/apps/{row['id']}"
    newcomer = _newcomer()
    # Pending opens, with the share's cap as the role; nothing is placed.
    assert apps_api.app_access(row, newcomer) is True
    from api.apps import manifest as _mf
    assert _mf.caller_role(row, newcomer) == "editor"
    _as(newcomer)
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []
    page = client.get(f"/v1/apps/{row['id']}").json()
    assert page["granted"] is True and "placement" not in page
    # The decision: an agent they do not hold, no agent at all, then one of theirs.
    assert client.post(f"/v1/shares/{share['id']}/accept", json={"agent": THIRD}).status_code == 403
    assert client.post(f"/v1/shares/{share['id']}/accept", json={}).status_code == 400
    r = client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING})
    assert r.status_code == 200 and r.json()["share"]["placed_agent"] == RECEIVING
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert [(a["id"], a["placement"]["kind"], a["viewer_role"]) for a in placed] == \
        [(row["id"], "person", "editor")]
    page = client.get(f"/v1/apps/{row['id']}").json()
    assert page["placement"]["agent"] == RECEIVING and page["placement"]["kind"] == "person"
    inbox = client.get("/v1/shares/inbox").json()
    assert inbox["pending"] == 0
    assert [(i["placed_agent"], i["actions"]) for i in inbox["items"]] == [(RECEIVING, ["open", "hide"])]
    # Another member of the receiving agent never sees a person's placement.
    _as(_user(VIEWER, agents=(AGENT, RECEIVING), agent_roles={AGENT: "viewer", RECEIVING: "viewer"}))
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []
    # The reads the app proxy's caller and the hooks take.
    assert share_store.placement_for(row["id"], RECEIVING) is None
    assert share_store.placement_for(row["id"], RECEIVING, NEWCOMER) == {
        "share_id": share["id"], "source": "person", "role_cap": "editor"}
    assert [a["id"] for a in share_store.placements_for_agent(RECEIVING, NEWCOMER)] == [row["id"]]
    # Removed from the agent: the placement ends and the share waits again.
    task_store.set_user_agents(NEWCOMER, [], "test")
    fresh = share_store.get_share(share["id"])
    assert fresh["decision"] == "pending" and fresh["placed_agent"] is None
    assert share_store.placements_for_agent(RECEIVING, NEWCOMER) == []


def test_a_decline_reads_declined_and_a_new_share_replaces_it():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    newcomer = _newcomer()
    _as(newcomer)
    assert client.post(f"/v1/shares/{share['id']}/decline").status_code == 200
    assert client.post(f"/v1/shares/{share['id']}/decline").status_code == 404
    assert apps_api.app_access(row, newcomer) is False
    assert client.get("/v1/shares/inbox").json() == {"items": [], "pending": 0}
    _as(_owner_everywhere())
    listed = client.get(f"/v1/shares?target_kind=app&target={row['id']}").json()["shares"]
    assert [(s["decision"], s["decided_by"]) for s in listed] == [("declined", NEWCOMER)]
    with _notify_patch():
        again = _share_to(row["id"], "person", NEWCOMER)["share"]
    assert again["id"] != share["id"] and share_store.get_share(share["id"])["revoked_at"]
    assert apps_api.app_access(row, newcomer) is True
    # No one else's share is theirs to decide; a member of the app's own
    # agent never places it there (they see it there already).
    _as(newcomer)
    assert client.post(f"/v1/shares/{again['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    _as(_owner_everywhere())
    with _notify_patch():
        to_member = _share_to(row["id"], "person", MEMBER)["share"]
    _as(_member_everywhere())
    assert client.post(f"/v1/shares/{to_member['id']}/accept", json={"agent": AGENT}).status_code == 400
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    assert client.post(f"/v1/shares/{again['id']}/decline").status_code == 404
    assert client.post(f"/v1/shares/{again['id']}/accept", json={"agent": RECEIVING}).status_code == 404


def _revoke(share_id: str):
    return client.patch(f"/v1/shares/{share_id}", json={"revoke": True})


def test_a_person_removes_a_share_they_accepted_and_may_be_shared_it_again():
    """"Remove for me" on a person's placement is the share's revoke by its
    own person: the placement, the section item, the access, the audience,
    the sharer's list and the admin list lose it at once; a new share
    works."""
    import asyncio as _asyncio
    from services.notifications import notification_manager as nm
    from tests.session.test_notification_fanout import _Conn, _frames
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    newcomer = _newcomer()
    _as(newcomer)
    assert client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    assert [a["placement"]["kind"] for a in _panel(RECEIVING) if a.get("placement")] == ["person"]
    # The audience is cached with them in it, so the revoke must forget it.
    assert NEWCOMER in audience.app_audience(row)
    nm._user_connections.clear()
    me = _Conn("c-newcomer")
    nm._user_connections[NEWCOMER] = [me]
    try:
        r = _revoke(share["id"])
        assert r.status_code == 200, r.text
        # The frame names the agent the app left.
        assert _frames(me) == [{"type": "share_inbox", "pending": 0, "agents": [RECEIVING]}]
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    finally:
        nm._user_connections.clear()
    fresh = share_store.get_share(share["id"])
    assert fresh["revoked_at"] and fresh["decision"] == "accepted"
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []
    assert client.get("/v1/shares/inbox").json() == {"items": [], "pending": 0}
    assert apps_api.app_access(row, newcomer) is False
    assert NEWCOMER not in audience.app_audience(row)
    assert share_store.placement_for(row["id"], RECEIVING, NEWCOMER) is None
    assert _revoke(share["id"]).status_code == 404
    assert client.post(f"/v1/shares/{share['id']}/decline").status_code == 404
    _as(_owner_everywhere())
    assert client.get(f"/v1/shares?target_kind=app&target={row['id']}").json()["shares"] == []
    _as(_user("root-sub", role="admin"))
    assert client.get("/v1/admin/shares").json()["shares"] == []
    _as(_owner_everywhere())
    with _notify_patch():
        again = _share_to(row["id"], "person", NEWCOMER)["share"]
    assert again["decision"] == "pending" and again["id"] != share["id"]
    assert apps_api.app_access(row, newcomer) is True


def test_a_person_removes_a_pending_share_too():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.get("/v1/shares/inbox").json()["pending"] == 1
    assert _revoke(share["id"]).status_code == 200
    assert client.get("/v1/shares/inbox").json() == {"items": [], "pending": 0}


def test_the_person_s_revoke_reaches_their_own_app_share_alone():
    """Another person, a member of the receiving agent, a team share, a
    declined row of theirs and a chat share of theirs are not theirs to
    revoke: someone who may see the app gets 403, someone who may not 404,
    and the row stays."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING)["share"]
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER)["share"]
        theirs = _share_to(row["id"], "person", OUTSIDER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    # Someone else's person share, and the agent share placing it in their
    # agent: the newcomer sees the app (both admit them), so 403.
    assert _revoke(theirs["id"]).status_code == 403
    assert _revoke(team["id"]).status_code == 403
    # A viewer of the app's own agent sees it and may not share it: 403.
    _as(_user(VIEWER, agents=(AGENT, RECEIVING), agent_roles={AGENT: "viewer", RECEIVING: "viewer"}))
    assert _revoke(mine["id"]).status_code == 403
    # Someone the app does not admit at all: the same 404 as a missing share.
    task_store.upsert_user("stranger-sub", "stranger@test.com", "Stranger", "member")
    _as(_user("stranger-sub", agents=(), agent_roles={}))
    assert _revoke(mine["id"]).status_code == 404
    for s in (mine, theirs, team):
        assert share_store.get_share(s["id"])["revoked_at"] is None
    # A declined row of theirs stays for the sharer to read (they may not
    # see the app any more: 404).
    with _notify_patch():
        other = _shared("other")
        _as(_owner_everywhere())
        declined = _share_to(other["id"], "person", OUTSIDER)["share"]
    _as(_user(OUTSIDER, agents=(), agent_roles={}))
    assert client.post(f"/v1/shares/{declined['id']}/decline").status_code == 200
    assert _revoke(declined["id"]).status_code == 404
    assert share_store.get_share(declined["id"])["revoked_at"] is None
    # A chat share of theirs is not a grant they revoke (it has Hide).
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    _as(_owner_everywhere())
    with _notify_patch(), patch("services.sharing.chat_snapshot.build", return_value="ref"):
        chat_share = _share_to(chat_id, "person", NEWCOMER, target_kind="chat")["share"]
    _as(_newcomer())
    assert _revoke(chat_share["id"]).status_code == 404
    assert share_store.get_share(chat_share["id"])["revoked_at"] is None


def test_the_decline_is_for_a_share_still_waiting():
    """An accepted share is removed with the revoke, never declined; a
    chat share needs no decision."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    r = client.post(f"/v1/shares/{mine['id']}/decline")
    assert r.status_code == 400 and "waiting for a decision" in r.json()["detail"]
    fresh = share_store.get_share(mine["id"])
    assert (fresh["decision"], fresh["placed_agent"]) == ("accepted", RECEIVING)
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    _as(_owner_everywhere())
    with _notify_patch(), patch("services.sharing.chat_snapshot.build", return_value="ref"):
        chat_share = _share_to(chat_id, "person", NEWCOMER, target_kind="chat")["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{chat_share['id']}/decline").status_code == 400


def test_the_decline_and_the_person_s_revoke_are_human_only():
    """The person's own session token and the master key are both refused:
    a prompt never declines or removes a share for its person."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        waiting = _share_to(row["id"], "person", NEWCOMER)["share"]
    session = _user(NEWCOMER, agents=(RECEIVING,), agent_roles={RECEIVING: "viewer"},
                    is_api_key=True)
    for bearer in (session, _user("api-key", role="admin", is_api_key=True)):
        _as(bearer)
        assert client.post(f"/v1/shares/{waiting['id']}/decline").status_code == 403
        assert _revoke(waiting["id"]).status_code == 403
    fresh = share_store.get_share(waiting["id"])
    assert (fresh["decision"], fresh["revoked_at"]) == ("pending", None)


def test_a_removal_beside_a_team_placement_leaves_the_team_row_at_its_own_cap():
    """A person's own share and an agent share place one app in one agent:
    the row is the agent share's; once the person removes their stronger
    share, the row stays and they act at the team cap again."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING, cap="viewer")["share"]
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER, cap="editor")["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert [(a["placement"]["kind"], a["placement"]["share_id"]) for a in placed] == [("agent", team["id"])]
    assert placed[0]["viewer_role"] == "editor"
    assert _revoke(mine["id"]).status_code == 200
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert [(a["placement"]["kind"], a["placement"]["share_id"]) for a in placed] == [("agent", team["id"])]
    assert placed[0]["viewer_role"] == "viewer"
    assert share_store.placements_for_agent(RECEIVING, NEWCOMER)[0]["placement"]["person_cap"] is None
    assert share_store.get_share(team["id"])["revoked_at"] is None
    # Shared again, the person's share waits while the team row stands.
    _as(_owner_everywhere())
    with _notify_patch():
        again = _share_to(row["id"], "person", NEWCOMER, cap="editor")["share"]
    assert again["decision"] == "pending"
    assert share_store.placement_for(row["id"], RECEIVING, NEWCOMER)["share_id"] == team["id"]


def test_a_demoted_admin_removes_a_placement_in_an_agent_they_no_longer_hold():
    import asyncio as _asyncio
    from services.notifications import notification_manager as nm
    from tests.session.test_notification_fanout import _Conn, _frames
    _agents()
    row = _shared()
    admin = "demoted-admin"
    task_store.upsert_user(admin, f"{admin}@test.com", "Demoted", "admin")
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", admin)["share"]
    _as(_user(admin, role="admin", agents=(), agent_roles={}))
    assert client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    # Demoted, before the reconcile: the share is still accepted there.
    task_store.update_user_role(admin, "member")
    assert share_store.get_share(share["id"])["decision"] == "accepted"
    nm._user_connections.clear()
    me = _Conn("c-demoted")
    nm._user_connections[admin] = [me]
    try:
        _as(_user(admin, agents=(), agent_roles={}))
        assert _revoke(share["id"]).status_code == 200
        assert _frames(me) == [{"type": "share_inbox", "pending": 0, "agents": [RECEIVING]}]
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    finally:
        nm._user_connections.clear()
    assert share_store.get_share(share["id"])["revoked_at"]


def test_the_person_s_revoke_never_takes_a_row_a_decline_just_answered():
    """The revoke read the share pending; a decline landed before its
    write. Their own right covers pending and accepted rows only, so the
    declined row stays for the sharer to read and the revoke answers 404."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    real = share_store.revoke_share

    def _declined_first(share_id, *args, **kwargs):
        share_store.set_decision(share_id, share_store.DECLINED, NEWCOMER)
        return real(share_id, *args, **kwargs)

    with patch.object(share_store, "revoke_share", side_effect=_declined_first):
        assert _revoke(share["id"]).status_code == 404
    fresh = share_store.get_share(share["id"])
    assert (fresh["decision"], fresh["revoked_at"]) == ("declined", None)


def test_an_accept_racing_a_revoke_answers_404():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    real = share_store.set_decision

    def _revoked_first(share_id, *args, **kwargs):
        share_store.revoke_share(share_id)
        return real(share_id, *args, **kwargs)

    with patch.object(share_store, "set_decision", side_effect=_revoked_first):
        r = client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING})
    assert r.status_code == 404
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []


def test_an_accept_racing_a_decline_answers_404():
    """The accept read the share pending; a decline landed before its
    write. The write refuses a declined row, so nothing is placed and the
    sharer keeps "declined by X"."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    real = share_store.set_decision

    def _declined_first(share_id, *args, **kwargs):
        if kwargs.get("placed_agent") or (args and args[0] == share_store.ACCEPTED):
            real(share_id, share_store.DECLINED, NEWCOMER)
        return real(share_id, *args, **kwargs)

    with patch.object(share_store, "set_decision", side_effect=_declined_first):
        r = client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING})
    assert r.status_code == 404
    fresh = share_store.get_share(share["id"])
    assert (fresh["decision"], fresh["placed_agent"], fresh["revoked_at"]) == ("declined", None, None)
    assert [a for a in _panel(RECEIVING) if a.get("placement")] == []


def test_a_removed_share_stays_removed_through_every_reap():
    """The reaps of a placement (a membership removal, the demotion
    reconcile, an agent delete) set a share back to pending; a revoked row
    keeps its placed agent, and every reap leaves revoked rows alone."""
    import asyncio as _asyncio
    from services.agents import offboarding, offboarding_transfer
    from storage.agents import agent_store
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    assert _revoke(share["id"]).status_code == 200
    task_store.set_user_agents(NEWCOMER, [], "test")
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(offboarding_transfer.reconcile_person(
            NEWCOMER, actor=OWNER, reason=offboarding.DEMOTED))
    finally:
        loop.close()
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    assert agent_store.delete_agent(RECEIVING)
    fresh = share_store.get_share(share["id"])
    assert fresh["revoked_at"]
    assert (fresh["decision"], fresh["placed_agent"]) == ("accepted", RECEIVING)


def test_a_decline_that_changes_nothing_answers_404():
    """Revoked by the sharer between the route's read and its write."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    real = share_store.set_decision

    def _revoked_first(share_id, *args, **kwargs):
        share_store.revoke_share(share_id)
        return real(share_id, *args, **kwargs)

    with patch.object(share_store, "set_decision", side_effect=_revoked_first):
        assert client.post(f"/v1/shares/{share['id']}/decline").status_code == 404
    fresh = share_store.get_share(share["id"])
    assert fresh["revoked_at"] and fresh["decision"] == "pending"


def test_a_decline_racing_an_accept_never_declines_the_accepted_share():
    """The decline read the share pending; an accept landed before its
    write. The write asks for pending, so the accepted share stands and
    the decline answers 404."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    real = share_store.set_decision

    def _accepted_first(share_id, *args, **kwargs):
        real(share_id, share_store.ACCEPTED, NEWCOMER, placed_agent=RECEIVING)
        return real(share_id, *args, **kwargs)

    with patch.object(share_store, "set_decision", side_effect=_accepted_first):
        assert client.post(f"/v1/shares/{share['id']}/decline").status_code == 404
    fresh = share_store.get_share(share["id"])
    assert (fresh["decision"], fresh["placed_agent"]) == ("accepted", RECEIVING)


def test_the_focus_line_names_a_placed_app_the_viewer_looks_at():
    """The focus line reads the row through ``app_access``, so an agent
    placement and a person's own placement name the app like a home row."""
    import asyncio as _asyncio
    from services.apps import focus_context
    from services.notifications import notification_manager as nm
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    _share_to(row["id"], "agent", RECEIVING)
    focus_context._prefs_cache.clear()
    try:
        cid = str(uuid.uuid4())
        nm.register_user_connection(NEWCOMER, cid, _asyncio.Queue(), live_queue=nm.LiveQueue())
        nm.set_connection_focus(NEWCOMER, cid, {"surface": "app", "app_id": row["id"]})
        line = focus_context.focus_line(NEWCOMER, connection_id=cid)
        assert line == '[The user is looking at the app "Team" (team) right now.]'
        # An outsider who holds no agent it sits in gets nothing.
        other = str(uuid.uuid4())
        nm.register_user_connection(OUTSIDER, other, _asyncio.Queue(), live_queue=nm.LiveQueue())
        nm.set_connection_focus(OUTSIDER, other, {"surface": "app", "app_id": row["id"]})
        assert focus_context.focus_line(OUTSIDER, connection_id=other) == ""
        # A person's own placement names it too.
        with _notify_patch():
            mine = _share_to(row["id"], "person", OUTSIDER)["share"]
        task_store.add_user_agent(OUTSIDER, THIRD, "viewer", "test")
        _as(_user(OUTSIDER, agents=(THIRD,), agent_roles={THIRD: "viewer"}))
        assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": THIRD}).status_code == 200
        assert focus_context.focus_line(OUTSIDER, connection_id=other) == line
    finally:
        nm._user_connections.clear()
        focus_context._prefs_cache.clear()
        _asyncio.set_event_loop(_asyncio.new_event_loop())


def test_a_placement_hide_is_per_agent_and_the_audience_follows_it():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    one = _share_to(row["id"], "agent", RECEIVING)["share"]
    _share_to(row["id"], "agent", THIRD)
    newcomer = _newcomer()
    assert NEWCOMER in audience.app_audience(row)
    _as(newcomer)
    # The hide names the agent; one the viewer does not hold is refused.
    assert client.post(f"/v1/shares/{one['id']}/hide", json={"agent": THIRD}).status_code == 404
    assert client.post(f"/v1/shares/{one['id']}/hide", json={"agent": RECEIVING}).status_code == 200
    assert [a["hidden_for_me"] for a in _panel(RECEIVING) if a["id"] == row["id"]] == [True]
    assert NEWCOMER not in audience.app_audience(row)
    assert apps_api.app_access(row, newcomer) is True   # a hide never removes access
    _as(_member_everywhere())
    assert [a["hidden_for_me"] for a in _panel(THIRD) if a["id"] == row["id"]] == [False]
    assert [a["hidden_for_me"] for a in _panel(AGENT) if a["id"] == row["id"]] == [False]
    _as(newcomer)
    assert client.post(f"/v1/shares/{one['id']}/unhide", json={"agent": RECEIVING}).status_code == 200
    assert [a["hidden_for_me"] for a in _panel(RECEIVING) if a["id"] == row["id"]] == [False]
    assert NEWCOMER in audience.app_audience(row)
    # The viewer token names the admitting share and the capped role.
    from unittest.mock import patch as _patch
    with _patch("api.apps.app_actions._check_fire_rate"):
        tok = client.post(f"/v1/apps/{row['id']}/viewer-token")
    assert tok.status_code == 200
    from services.apps import app_tokens
    claims = app_tokens.verify(tok.json()["token"], row["id"], app_tokens.PURPOSE_VIEWER)
    assert claims["grant"] == one["id"]
    assert claims["role"] == "viewer"


def test_share_inbox_frames_reach_the_connected_recipients():
    import asyncio as _asyncio
    from services.notifications import notification_manager as nm
    from tests.session.test_notification_fanout import _Conn, _frames
    _agents()
    row = _shared()
    nm._user_connections.clear()
    me, member = _Conn("c-newcomer"), _Conn("c-member")
    nm._user_connections[NEWCOMER] = [me]
    nm._user_connections[MEMBER] = [member]
    try:
        _as(_owner_everywhere())
        with _notify_patch():
            share = _share_to(row["id"], "person", NEWCOMER)["share"]
        assert _frames(me) == [{"type": "share_inbox", "pending": 1, "agents": []}]
        assert _frames(member) == []
        _as(_newcomer())
        client.post(f"/v1/shares/{share['id']}/accept", json={"agent": RECEIVING})
        assert _frames(me) == [{"type": "share_inbox", "pending": 0, "agents": [RECEIVING]}]
        # An agent share tells every member of the receiving agent.
        _as(_owner_everywhere())
        _share_to(row["id"], "agent", RECEIVING)
        assert _frames(me) == [{"type": "share_inbox", "pending": 0, "agents": [RECEIVING]}]
        assert _frames(member) == [{"type": "share_inbox", "pending": 0, "agents": [RECEIVING]}]
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    finally:
        nm._user_connections.clear()


def test_a_moved_placement_names_the_agent_it_left_in_the_frame():
    import asyncio as _asyncio
    from services.notifications import notification_manager as nm
    from tests.session.test_notification_fanout import _Conn, _frames
    _agents()
    row = _shared()
    nm._user_connections.clear()
    me = _Conn("c-member")
    nm._user_connections[MEMBER] = [me]
    try:
        _as(_owner_everywhere())
        with _notify_patch():
            share = _share_to(row["id"], "person", MEMBER)["share"]
        _as(_member_everywhere())
        assert client.post(f"/v1/shares/{share['id']}/accept",
                           json={"agent": RECEIVING}).status_code == 200
        _frames(me)
        # A second accept moves the app: the panel it left changed too.
        assert client.post(f"/v1/shares/{share['id']}/accept",
                           json={"agent": THIRD}).status_code == 200
        assert _frames(me) == [{"type": "share_inbox", "pending": 0,
                                "agents": sorted([RECEIVING, THIRD])}]
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    finally:
        nm._user_connections.clear()


def test_viewer_me_reports_the_role_the_share_gives():
    from api.apps import manifest as _mf
    _agents()
    me = {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}
    canonical, err = _mf.validate_actions([me], AGENT, True)
    assert err == "", err
    row = task_store.upsert_app(AGENT, "", None, "board", title="Board",
                                rel_path="workspace/apps/board.html", actions_json=canonical)
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), OWNER)
    _as(_owner_everywhere())
    with _notify_patch():
        _share_to(row["id"], "person", NEWCOMER, cap="editor")
    # The newcomer holds no row on the app's agent: the floors judge them at
    # the share's cap, and viewer.me must say the same.
    newcomer = _newcomer()
    assert _mf.caller_role(task_store.get_app(row["id"]), newcomer) == "editor"
    _as(newcomer)
    out = client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={}).json()
    assert out["ok"] and out["result"]["role"] == "editor"


def test_the_single_app_read_names_who_shared_the_placement():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    _share_to(row["id"], "agent", RECEIVING)
    # A member of the receiving agent alone, through the agent's placement.
    _as(_newcomer())
    page = client.get(f"/v1/apps/{row['id']}").json()
    assert page["placement"]["kind"] == "agent"
    assert page["placement"]["shared_by"] == OWNER
    assert page["placement"]["shared_by_name"] == "Owner"
    # A person's own placement names its sharer too.
    _as(_owner_everywhere())
    other = _shared("other")
    with _notify_patch():
        mine = _share_to(other["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept",
                       json={"agent": RECEIVING}).status_code == 200
    page = client.get(f"/v1/apps/{other['id']}").json()
    assert page["placement"]["kind"] == "person"
    assert (page["placement"]["shared_by"], page["placement"]["shared_by_name"]) == (OWNER, "Owner")


def test_the_single_app_read_follows_the_panel_s_row_beside_a_team_placement():
    """A person's own share and an agent share place one app in one agent:
    the full-screen page reads the row the Apps panel shows there (the agent
    share's), so it offers that row's menu; alone, their own placement."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING)["share"]
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER, cap="editor")["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    page = client.get(f"/v1/apps/{row['id']}").json()
    assert (page["placement"]["kind"], page["placement"]["share_id"], page["placement"]["agent"]) == (
        "agent", team["id"], RECEIVING)
    assert page["granted"] is True and page["share_id"] == mine["id"]
    _as(_owner_everywhere())
    assert client.patch(f"/v1/shares/{team['id']}", json={"revoke": True}).status_code == 200
    _as(_newcomer())
    page = client.get(f"/v1/apps/{row['id']}").json()
    assert (page["placement"]["kind"], page["placement"]["share_id"]) == ("person", mine["id"])


def test_a_member_of_the_home_agent_with_a_person_share_sees_the_home_row_once():
    """A person share to a member of the app's own agent adds no row there:
    pending, and accepted into another agent they hold, the home panel
    lists the app once with no placement and no granted mark, and the page
    reads it as a member's."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        mine = _share_to(row["id"], "person", MEMBER)["share"]
    _as(_member_everywhere())

    def _home_row_once_unmarked():
        rows = [a for a in _panel(AGENT) if a["id"] == row["id"]]
        assert len(rows) == 1
        assert not rows[0].get("placement") and not rows[0].get("granted")
        page = client.get(f"/v1/apps/{row['id']}").json()
        assert not page.get("placement") and not page.get("granted") and "share_id" not in page

    _home_row_once_unmarked()
    assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    _home_row_once_unmarked()
    assert [a["placement"]["kind"] for a in _panel(RECEIVING)
            if a["id"] == row["id"]] == ["person"]


def test_the_directory_lists_agents_and_departments_by_the_switches():
    from storage.agents import agent_store, db_departments
    _agents()
    agent_store.create_agent("admins-only", "Admins Only", admin_only=True, created_by=OWNER)
    dept = db_departments.create_department("Ops", "user-admin")
    _as(_member_everywhere())
    d = client.get("/v1/users/directory").json()
    assert {a["slug"] for a in d["agents"]} >= {AGENT, RECEIVING, THIRD}
    assert "admins-only" not in {a["slug"] for a in d["agents"]}
    assert d["departments"] == []
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    d = client.get("/v1/users/directory").json()
    assert d["users"] is None and {a["slug"] for a in d["agents"]} == {AGENT, RECEIVING, THIRD}
    task_store.set_platform_setting("sharing_to_agents_enabled", "0")
    assert client.get("/v1/users/directory").json()["agents"] == []
    _as(_user("root-sub", role="admin"))
    d = client.get("/v1/users/directory").json()
    assert d["users"] and d["agents"] == [] and d["departments"] == [{"id": dept["id"], "name": "Ops"}]
    task_store.set_platform_setting("sharing_to_departments_enabled", "0")
    assert client.get("/v1/users/directory").json()["departments"] == []


def test_the_chat_cap_counts_chat_shares_only():
    from services.sharing import chat_snapshot
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    _share_to(row["id"], "agent", RECEIVING)
    with _notify_patch():
        _share_to(row["id"], "person", NEWCOMER)
    assert share_store.count_live_shares_by(OWNER, target_kind="chat") == 0
    assert share_store.count_live_shares_by(OWNER) == 2
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    with patch.object(chat_snapshot, "MAX_CHAT_SHARES_PER_USER", 0), _notify_patch():
        _share_to(chat_id, "person", NEWCOMER, target_kind="chat", expect=400)


def test_an_agent_delete_reaps_its_placements():
    from storage.agents import agent_store
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    agent_share = _share_to(row["id"], "agent", RECEIVING)["share"]
    with _notify_patch():
        person_share = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    client.post(f"/v1/shares/{person_share['id']}/accept", json={"agent": RECEIVING})
    client.post(f"/v1/shares/{agent_share['id']}/hide", json={"agent": RECEIVING})
    assert agent_store.delete_agent(RECEIVING)
    assert share_store.get_share(agent_share["id"]) is None
    fresh = share_store.get_share(person_share["id"])
    assert fresh["decision"] == "pending" and fresh["placed_agent"] is None
    from storage.pg import get_conn
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM share_placement_hides").fetchone()["n"] == 0


def test_a_sharer_s_deletion_hands_team_shares_to_the_owner_and_takes_their_own():
    _agents()
    row = _shared()
    task_store.upsert_user("owner-of-install", "root@test.com", "Root", "admin")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_owner = TRUE WHERE sub = %s", ("owner-of-install",))
        conn.commit()
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING)["share"]
    with _notify_patch():
        person = _share_to(row["id"], "person", NEWCOMER)["share"]
    assert task_store.delete_user(OWNER)
    kept = share_store.get_share(team["id"])
    assert kept and kept["created_by"] == "owner-of-install" and kept["revoked_at"] is None
    assert share_store.get_share(person["id"]) is None
    assert share_store.placement_for(row["id"], RECEIVING)["share_id"] == team["id"]


def test_two_shares_in_one_agent_make_one_row_the_agent_share_owns_at_the_stronger_role():
    from services.departments import edge_compiler
    from storage.agents import agent_store, db_departments
    _agents()
    row = _shared()
    dept = db_departments.create_department("Ops", "user-admin", level_names=["Head"])
    agent_store.update_agent(RECEIVING, department_id=dept["id"],
                             department_level_id=dept["levels"][0]["id"])
    edge_compiler.recompile()
    _as(_user("user-admin", role="admin"))
    _share_to(row["id"], "department", dept["id"], cap="manager")
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING, cap="viewer")["share"]
    # The member edits RECEIVING: one row, the agent share's (theirs to
    # remove), at the strongest cap, acting at their own capped role.
    _as(_member_everywhere())
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert [(a["placement"]["kind"], a["placement"]["share_id"], a["placement"]["can_remove"])
            for a in placed] == [("agent", team["id"], True)]
    assert placed[0]["placement"]["role_cap"] == "manager"
    assert placed[0]["viewer_role"] == "editor"
    assert placed[0]["placement"]["shared_by_name"] == "Owner"
    # Removing the agent share leaves the department's placement.
    assert client.patch(f"/v1/shares/{team['id']}", json={"revoke": True}).status_code == 200
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert [(a["placement"]["kind"], a["placement"]["can_remove"]) for a in placed] == [
        ("department", False)]
    # A person's own accepted share beside the team's placement: its cap is
    # theirs uncapped, so a viewer of RECEIVING acts at it when it is stronger.
    _as(_owner_everywhere())
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER, cap="editor")["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept",
                       json={"agent": RECEIVING}).status_code == 200
    placed = [a for a in _panel(RECEIVING) if a.get("placement")]
    assert placed[0]["placement"]["kind"] == "department"
    assert placed[0]["viewer_role"] == "editor"


def test_placement_role_is_the_role_a_session_acts_at_and_describe_placements_lists_them():
    """The two reads the app proxy's caller and the audience take (APPS.md
    "Agents call apps", "Platform catalog"): a session's role through a
    placement, and every receiving agent with its members at the capped
    role."""
    from auth import roles
    from services.departments import edge_compiler
    from storage.agents import agent_store, db_departments
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING, cap="manager")["share"]
    # An agent placement: the row on the receiving agent capped by the cap;
    # a session with no person carries the no-user word, which ranks below
    # every floor; the home agent and an agent with no share get nothing.
    assert share_store.placement_role(row["id"], RECEIVING, None, "editor") == {
        "share_id": team["id"], "source": "agent", "role_cap": "manager", "role": "editor"}
    assert share_store.placement_role(row["id"], RECEIVING, None, "viewer")["role"] == "viewer"
    assert share_store.placement_role(row["id"], RECEIVING, None, roles.SERVICE)["role"] == roles.SERVICE
    assert share_store.placement_role(row["id"], AGENT, None, "manager") is None
    assert share_store.placement_role(row["id"], THIRD, None, "manager") is None
    # The person's own accepted share beside it: its cap uncapped, so a
    # viewer of the agent acts at editor where the cap alone would give viewer.
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER, cap="editor")["share"]
    share_store.set_decision(mine["id"], share_store.ACCEPTED, NEWCOMER, placed_agent=RECEIVING)
    assert share_store.placement_role(row["id"], RECEIVING, NEWCOMER, "viewer") == {
        "share_id": mine["id"], "source": "person", "role_cap": "editor", "role": "editor"}
    assert share_store.placement_role(row["id"], RECEIVING, VIEWER, "viewer")["share_id"] == team["id"]
    assert share_store.placement_role(row["id"], THIRD, NEWCOMER, "viewer") is None
    assert share_store.placement_for(row["id"], RECEIVING, NEWCOMER)["share_id"] == team["id"]
    assert share_store.effective_placement_role("viewer", "manager", "editor") == "editor"
    assert share_store.effective_placement_role("manager", "editor", None) == "editor"
    # A department placement across two agents, one of them holding the
    # agent share too: one entry per receiving agent, the agent share's
    # identity and the strongest cap, every member at their capped row.
    dept = db_departments.create_department("Ops", "user-admin", level_names=["Head", "Staff"])
    head, staff = (lv["id"] for lv in dept["levels"])
    agent_store.update_agent(RECEIVING, department_id=dept["id"], department_level_id=head)
    agent_store.update_agent(THIRD, department_id=dept["id"], department_level_id=staff)
    edge_compiler.recompile()
    _as(_user("user-admin", role="admin"))
    dep = _share_to(row["id"], "department", dept["id"], cap="editor")["share"]
    assert share_store.placement_role(row["id"], THIRD, None, "viewer") == {
        "share_id": dep["id"], "source": "department", "role_cap": "editor", "role": "viewer"}
    placements = share_store.describe_placements(row["id"])
    assert [(p["agent"], p["kind"], p["share_id"], p["role_cap"], p["department"])
            for p in placements] == [
        (RECEIVING, "agent", team["id"], "manager", None),
        (THIRD, "department", dep["id"], "editor", {"id": dept["id"], "name": "Ops"})]
    assert {m["sub"]: m["role"] for m in placements[0]["members"]} == {
        OWNER: "manager", MEMBER: "editor", VIEWER: "viewer", NEWCOMER: "viewer"}
    assert {m["sub"]: m["role"] for m in placements[1]["members"]} == {OWNER: "editor", MEMBER: "viewer"}
    assert all(set(m) == {"sub", "username", "display_name", "name", "role"}
               for p in placements for m in p["members"])
    assert "email" not in str(placements)
    # A revoked share leaves at once; an agent of no share lists nothing.
    assert client.patch(f"/v1/shares/{dep['id']}", json={"revoke": True}).status_code == 200
    assert [p["agent"] for p in share_store.describe_placements(row["id"])] == [RECEIVING]
    assert share_store.describe_placements(_shared("other")["id"]) == []


def test_a_closed_directory_hides_agents_the_sharer_does_not_hold():
    _agents()
    row = _shared()
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    try:
        _as(_user(MEMBER, agents=(AGENT,), agent_roles={AGENT: "editor"}))
        # RECEIVING exists, but the sharer holds no row on it: the unknown
        # agent's answer, and nothing written.
        assert _share_to(row["id"], "agent", RECEIVING) == {"status": "ok"}
        assert _share_to(row["id"], "agent", "no-such-agent") == {"status": "ok"}
        assert share_store.placement_for(row["id"], RECEIVING) is None
    finally:
        task_store.set_platform_setting("user_directory_visible_to_members", "1")


def test_the_pending_count_follows_the_section_s_rule_for_a_dormant_app():
    row = _personal()
    with _notify_patch():
        share = _share(row["id"], MEMBER)["share"]
    assert share_store.pending_count_for([MEMBER]) == {MEMBER: 1}
    # The owner loses the agent: the personal app is dormant, so the section
    # lists nothing and the badge counts nothing; the row waits unchanged.
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM user_agents WHERE sub = %s AND agent = %s", (OWNER, AGENT))
        conn.commit()
    _as(_user(MEMBER, agent_roles={AGENT: "editor"}))
    body = client.get("/v1/shares/inbox").json()
    assert body["items"] == [] and body["pending"] == 0
    assert share_store.pending_count_for([MEMBER]) == {MEMBER: 0}
    assert share_store.get_share(share["id"])["decision"] == share_store.PENDING


def test_an_expired_share_takes_no_decision():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", NEWCOMER)["share"]
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE shares SET expires_at = %s WHERE id = %s",
                     ("2000-01-01T00:00:00+00:00", share["id"]))
        conn.commit()
    _as(_newcomer())
    assert client.post(f"/v1/shares/{share['id']}/accept",
                       json={"agent": RECEIVING}).status_code == 404
    assert client.post(f"/v1/shares/{share['id']}/decline").status_code == 404


def test_the_list_s_viewer_role_is_the_role_the_floors_judge():
    from api.apps import manifest as _mf
    from services.departments import edge_compiler
    from storage.agents import agent_store, db_departments
    _agents()
    me = {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}
    canonical, err = _mf.validate_actions([me], AGENT, True)
    assert err == "", err
    row = task_store.upsert_app(AGENT, "", None, "board", title="Board",
                                rel_path="workspace/apps/board.html", actions_json=canonical)
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), OWNER)
    dept = db_departments.create_department("Floors", "user-admin", level_names=["Head", "Staff"])
    head, staff = (lv["id"] for lv in dept["levels"])
    agent_store.update_agent(RECEIVING, department_id=dept["id"], department_level_id=head)
    agent_store.update_agent(THIRD, department_id=dept["id"], department_level_id=staff)
    edge_compiler.recompile()
    _as(_user("user-admin", role="admin"))
    _share_to(row["id"], "department", dept["id"], cap="editor")
    # A viewer of RECEIVING and an editor of THIRD, not on the app's agent:
    # the floors take the strongest placement (editor) on every panel.
    dual = "dual-sub"
    task_store.upsert_user(dual, f"{dual}@test.com", "Dual", "member")
    task_store.add_user_agent(dual, RECEIVING, "viewer", "test")
    task_store.add_user_agent(dual, THIRD, "editor", "test")
    person = _user(dual, agents=(RECEIVING, THIRD),
                   agent_roles={RECEIVING: "viewer", THIRD: "editor"})
    assert _mf.caller_role(task_store.get_app(row["id"]), person) == "editor"
    _as(person)
    for panel in (RECEIVING, THIRD):
        placed = [a for a in _panel(panel) if a["id"] == row["id"]]
        assert placed and placed[0]["viewer_role"] == "editor", (panel, placed)
    out = client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={}).json()
    assert out["ok"] and out["result"]["role"] == "editor"


def test_a_demoted_admin_s_placement_returns_to_their_section():
    import asyncio as _asyncio
    from services.agents import offboarding, offboarding_transfer
    _agents()
    row = _shared()
    admin = "demoted-admin"
    task_store.upsert_user(admin, f"{admin}@test.com", "Demoted", "admin")
    _as(_owner_everywhere())
    with _notify_patch():
        share = _share_to(row["id"], "person", admin)["share"]
    # An admin holds every agent: they place it in one they hold no row on.
    _as(_user(admin, role="admin", agents=(), agent_roles={}))
    assert client.post(f"/v1/shares/{share['id']}/accept",
                       json={"agent": RECEIVING}).status_code == 200
    assert share_store.get_share(share["id"])["placed_agent"] == RECEIVING
    # Demoted to a member of no agent: they no longer hold it, so the share
    # waits again in their section, to be placed elsewhere.
    task_store.update_user_role(admin, "member")
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(offboarding_transfer.reconcile_person(
            admin, actor=OWNER, reason=offboarding.DEMOTED))
    finally:
        loop.close()
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    fresh = share_store.get_share(share["id"])
    assert fresh["decision"] == "pending" and fresh["placed_agent"] is None
    _as(_user(admin, agents=(), agent_roles={}))
    inbox = client.get("/v1/shares/inbox").json()
    assert inbox["pending"] == 1
    assert [i["actions"] for i in inbox["items"] if i["id"] == share["id"]] == [["accept", "decline"]]


# ───────────────────────── the admin list ───────────────────────────────────


def _admin_list(**params) -> dict:
    r = client.get("/v1/admin/shares", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(body: dict) -> set[str]:
    return {s["id"] for s in body["shares"]}


def _department_of(*slugs: str) -> dict:
    from services.departments import edge_compiler
    from storage.agents import agent_store, db_departments
    task_store.upsert_user("user-admin", "user-admin@test.com", "Root", "admin")
    dept = db_departments.create_department("Ops", "user-admin", level_names=["Head"])
    for slug in slugs:
        agent_store.update_agent(slug, department_id=dept["id"],
                                 department_level_id=dept["levels"][0]["id"])
    edge_compiler.recompile()
    return dept


def test_the_admin_list_holds_every_share_with_its_standing_and_filters_in_the_query():
    _agents()
    row = _shared()
    other = _shared("other")
    personal = _personal()
    dept = _department_of(THIRD)
    _as(_owner_everywhere())
    with _notify_patch():
        waiting = _share_to(row["id"], "person", NEWCOMER)["share"]
        declined = _share_to(other["id"], "person", NEWCOMER)["share"]
    team = _share_to(row["id"], "agent", RECEIVING)["share"]
    paused = _share_to(other["id"], "agent", RECEIVING)["share"]
    share_store.set_share_state(paused["id"], "suspended")
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    with _notify_patch(), patch("services.sharing.chat_snapshot.build", return_value="ref"):
        chat = _share_to(chat_id, "person", NEWCOMER, target_kind="chat")["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{declined['id']}/decline").status_code == 200
    _as(_user("user-admin", role="admin"))
    to_dept = _share_to(row["id"], "department", dept["id"])["share"]
    link = share_store.create_external_share(target_kind="app", target_id=personal["id"],
                                             created_by=OWNER, token_hash="a" * 64,
                                             password_hash="x")
    # Expired and paused at once: the first word that holds is expired.
    stale = share_store.create_external_share(target_kind="app", target_id=personal["id"],
                                              created_by=OWNER, token_hash="b" * 64,
                                              public=True, expires_at="2000-01-01T00:00:00+00:00")
    share_store.set_share_state(stale["id"], "suspended")

    body = _admin_list()
    by_id = {s["id"]: s for s in body["shares"]}
    assert set(by_id) == {waiting["id"], declined["id"], team["id"], paused["id"], chat["id"],
                          to_dept["id"], link["id"], stale["id"]}
    assert {i: s["standing"] for i, s in by_id.items()} == {
        waiting["id"]: "waiting", declined["id"]: "declined", team["id"]: "live",
        paused["id"]: "paused", chat["id"]: "live", to_dept["id"]: "live",
        link["id"]: "live", stale["id"]: "expired"}
    # An internal row names its recipient by kind; a link row keeps its keys.
    assert by_id[team["id"]]["to_agent"] == {"slug": RECEIVING, "name": "Share-Target"}
    assert by_id[to_dept["id"]]["to_department"] == {"id": dept["id"], "name": "Ops"}
    assert by_id[declined["id"]]["decided_by_name"] == "Newcomer"
    assert by_id[waiting["id"]]["grantee"]["sub"] == NEWCOMER
    assert by_id[waiting["id"]]["placed_agent"] == ""
    assert (by_id[chat["id"]]["target_kind"], by_id[chat["id"]]["agent"]) == ("chat", AGENT)
    assert (by_id[link["id"]]["title"], by_id[link["id"]]["agent"]) == ("Sched", AGENT)
    assert by_id[link["id"]]["created_by_name"] == "Owner"
    assert "token_hash" not in by_id[link["id"]] and "password_hash" not in by_id[link["id"]]
    # Newest first, internal rows before the links.
    assert [s["scope"] for s in body["shares"]] == ["internal"] * 6 + ["external"] * 2
    assert [s["id"] for s in body["shares"]][-2:] == [stale["id"], link["id"]]
    # The kinds.
    assert _ids(_admin_list(kind="internal")) == set(by_id) - {link["id"], stale["id"]}
    assert _ids(_admin_list(kind="external")) == {link["id"], stale["id"]}
    assert _admin_list(kind="external")["truncated"] == {"external": False}
    # The standings, one word each.
    assert _ids(_admin_list(standing="waiting")) == {waiting["id"]}
    assert _ids(_admin_list(standing="declined")) == {declined["id"]}
    assert _ids(_admin_list(standing="paused")) == {paused["id"]}
    assert _ids(_admin_list(standing="expired")) == {stale["id"]}
    assert _ids(_admin_list(standing="live")) == {team["id"], chat["id"], to_dept["id"], link["id"]}
    assert _ids(_admin_list(kind="external", standing="waiting")) == set()
    # The agent: its apps' and chats' shares, the shares to it, and a
    # department share whose department holds it.
    assert _ids(_admin_list(agent=RECEIVING)) == {team["id"], paused["id"]}
    assert _ids(_admin_list(agent=THIRD)) == {to_dept["id"]}
    assert _ids(_admin_list(agent=AGENT)) == set(by_id)
    assert _ids(_admin_list(agent=RECEIVING, standing="paused")) == {paused["id"]}
    assert client.get("/v1/admin/shares", params={"kind": "links"}).status_code == 400
    assert client.get("/v1/admin/shares", params={"standing": "pending"}).status_code == 400


def test_the_admin_list_keeps_the_newest_of_each_kind_and_says_when_it_cut(monkeypatch):
    from api.sharing import shares as shares_api
    _agents()
    rows = [_shared(f"app{i}") for i in range(3)]
    _as(_owner_everywhere())
    made = [_share_to(r["id"], "agent", RECEIVING)["share"] for r in rows]
    share_store.create_external_share(target_kind="app", target_id=rows[0]["id"],
                                      created_by=OWNER, token_hash="c" * 64, password_hash="x")
    monkeypatch.setattr(shares_api, "ADMIN_LIST_MAX", 2)
    _as(_user("root-sub", role="admin"))
    body = _admin_list()
    assert [s["id"] for s in body["shares"] if s["scope"] == "internal"] == [made[2]["id"], made[1]["id"]]
    assert body["truncated"] == {"internal": True, "external": False}


def test_an_admin_revoke_from_the_list_ends_a_share_everywhere_and_tells_nobody():
    import asyncio as _asyncio
    from services.notifications import notification_manager as nm
    from tests.session.test_notification_fanout import _Conn, _frames
    _agents()
    row = _shared()
    dept = _department_of(RECEIVING, THIRD)
    _as(_user("user-admin", role="admin"))
    to_dept = _share_to(row["id"], "department", dept["id"])["share"]
    assert {a["id"] for a in share_store.placements_for_agent(RECEIVING)} == {row["id"]}
    assert {a["id"] for a in share_store.placements_for_agent(THIRD)} == {row["id"]}
    _as(_owner_everywhere())
    with _notify_patch():
        person = _share_to(row["id"], "person", NEWCOMER)["share"]
    paused = _share_to(_shared("paused")["id"], "agent", RECEIVING)["share"]
    share_store.set_share_state(paused["id"], "suspended")
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    with _notify_patch(), patch("services.sharing.chat_snapshot.build", return_value="ref"):
        chat = _share_to(chat_id, "person", NEWCOMER, target_kind="chat")["share"]
    nm._user_connections.clear()
    member, root = _Conn("c-member"), _Conn("c-root")
    nm._user_connections[MEMBER] = [member]
    nm._user_connections["user-admin"] = [root]
    _as(_user("user-admin", role="admin"))
    try:
        with _notify_patch() as fired:
            assert client.patch(f"/v1/shares/{to_dept['id']}", json={"revoke": True}).status_code == 200
            for s in (person, paused, chat):
                assert client.patch(f"/v1/shares/{s['id']}", json={"revoke": True}).status_code == 200
        fired.assert_not_awaited()
        # The members of both agents of the department, and every admin.
        assert _frames(member)[0] == {"type": "share_inbox", "pending": 0,
                                      "agents": sorted([RECEIVING, THIRD])}
        assert _frames(root)[0]["agents"] == sorted([RECEIVING, THIRD])
        _asyncio.set_event_loop(_asyncio.new_event_loop())
    finally:
        nm._user_connections.clear()
    assert share_store.placements_for_agent(RECEIVING) == []
    assert share_store.placements_for_agent(THIRD) == []
    for s in (to_dept, person, paused, chat):
        assert share_store.get_share(s["id"])["revoked_at"]
    assert _admin_list()["shares"] == []


def test_the_admin_list_s_agent_filter_never_reads_a_person_s_placed_agent():
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        mine = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{mine['id']}/accept", json={"agent": RECEIVING}).status_code == 200
    _as(_user("root-sub", role="admin"))
    assert _ids(_admin_list(agent=RECEIVING)) == set()
    assert _ids(_admin_list(agent=AGENT)) == {mine["id"]}
    assert _admin_list()["shares"][0]["standing"] == "live"


# ───────────────────────── the 30-day sweep of stale rows ───────────────────


def _backdate(share_id: str, column: str, days: int) -> None:
    from storage.pg import get_conn
    iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(f"UPDATE shares SET {column} = %s WHERE id = %s", (iso, share_id))
        conn.commit()


def test_revoked_and_declined_rows_go_thirty_days_after_and_nothing_else():
    _agents()
    row = _shared()
    other = _shared("other")
    personal = _personal()
    _as(_owner_everywhere())
    with _notify_patch():
        old_revoked = _share_to(row["id"], "person", NEWCOMER)["share"]
        old_declined = _share_to(other["id"], "person", NEWCOMER)["share"]
        young_declined = _share_to(other["id"], "person", OUTSIDER)["share"]
        live = _share_to(row["id"], "person", OUTSIDER)["share"]
    young_revoked = _share_to(row["id"], "agent", RECEIVING)["share"]
    expired = _share_to(other["id"], "agent", RECEIVING)["share"]
    old_link = share_store.create_external_share(target_kind="app", target_id=personal["id"],
                                                 created_by=OWNER, token_hash="d" * 64,
                                                 password_hash="x")
    for s in (old_revoked, young_revoked, old_link):
        share_store.revoke_share(s["id"])
    _as(_newcomer())
    assert client.post(f"/v1/shares/{old_declined['id']}/decline").status_code == 200
    _as(_user(OUTSIDER, agents=(), agent_roles={}))
    assert client.post(f"/v1/shares/{young_declined['id']}/decline").status_code == 200
    _backdate(old_revoked["id"], "revoked_at", 31)
    _backdate(old_link["id"], "revoked_at", 31)
    _backdate(young_revoked["id"], "revoked_at", 29)
    _backdate(old_declined["id"], "decided_at", 31)
    _backdate(young_declined["id"], "decided_at", 29)
    # Expired long ago but never revoked: the sharer still reads it.
    _backdate(expired["id"], "expires_at", 60)
    # Accepted long ago: only a declined row goes by its decision date.
    _as(_owner_everywhere())
    with _notify_patch():
        old_accepted = _share_to(personal["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{old_accepted['id']}/accept",
                       json={"agent": RECEIVING}).status_code == 200
    _backdate(old_accepted["id"], "decided_at", 60)
    assert share_store.delete_stale_shares(dry_run=True) == 3
    assert share_store.get_share(old_revoked["id"]) is not None
    assert share_store.delete_stale_shares() == 3
    gone = {old_revoked["id"], old_declined["id"], old_link["id"]}
    kept = {young_revoked["id"], young_declined["id"], live["id"], expired["id"],
            old_accepted["id"]}
    assert all(share_store.get_share(i) is None for i in gone)
    assert all(share_store.get_share(i) is not None for i in kept)
    assert share_store.delete_stale_shares() == 0


def test_a_declined_row_replaced_by_a_new_share_goes_by_its_revoke():
    """A new share to someone who declined revokes the declined row in the
    same transaction: from then on its revoke date counts, not its
    decision."""
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    with _notify_patch():
        first = _share_to(row["id"], "person", NEWCOMER)["share"]
    _as(_newcomer())
    assert client.post(f"/v1/shares/{first['id']}/decline").status_code == 200
    _backdate(first["id"], "decided_at", 40)
    _as(_owner_everywhere())
    with _notify_patch():
        again = _share_to(row["id"], "person", NEWCOMER)["share"]
    assert share_store.get_share(first["id"])["revoked_at"]
    assert share_store.delete_stale_shares() == 0
    _backdate(first["id"], "revoked_at", 31)
    assert share_store.delete_stale_shares() == 1
    assert share_store.get_share(again["id"]) is not None


def test_the_daily_sweep_runs_the_stale_share_pass():
    """Registered in the sweep whatever the session-retention switch says."""
    from services.infra import retention
    from services.infra.retention import LiveSnapshot
    _agents()
    row = _shared()
    _as(_owner_everywhere())
    team = _share_to(row["id"], "agent", RECEIVING)["share"]
    share_store.revoke_share(team["id"])
    _backdate(team["id"], "revoked_at", 31)
    dry = retention._run_sweep_sync(30, False, LiveSnapshot(), True)
    assert dry["shares_deleted"] == 1 and share_store.get_share(team["id"]) is not None
    assert dry["share_snapshots_deleted"] == 0
    stats = retention._run_sweep_sync(30, False, LiveSnapshot(), False)
    assert stats["shares_deleted"] == 1 and stats["errors"] == 0
    assert share_store.get_share(team["id"]) is None
