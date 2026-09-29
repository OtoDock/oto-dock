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
    mine = client.get("/v1/shares/mine").json()["shares"]
    assert [(s["target_id"], s["href"], s["shared_by"]) for s in mine] == \
        [(row["id"], f"/apps/{row['id']}", OWNER)]
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
    assert client.get("/v1/shares/mine").json()["shares"][0]["hidden"] is True
    assert client.post(f"/v1/apps/{row['id']}/unhide").status_code == 200
    assert not share_store.grant_hidden("app", row["id"], MEMBER)
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
    assert client.get("/v1/shares/mine").json()["shares"] == []
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


def test_sharing_settings_give_a_member_the_cap_and_nothing_else():
    """The share form needs the admin's longest expiry to know whether a
    link may be offered with no expiry; a member reads it, a bearer does not."""
    _as(_user(OUTSIDER, agents=(), agent_roles={}))
    assert client.get("/v1/sharing/settings").json() == {"max_expiry_days": None}
    task_store.set_platform_setting("sharing_max_expiry_days", "30")
    assert client.get("/v1/sharing/settings").json() == {"max_expiry_days": 30}
    task_store.set_platform_setting("sharing_max_expiry_days", "soon")
    assert client.get("/v1/sharing/settings").json() == {"max_expiry_days": None}
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
    assert client.get("/v1/users/directory").status_code == 404
    _as(_user("root-sub", role="admin"))
    assert client.get("/v1/users/directory").status_code == 200


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


def test_admin_shares_list_is_admin_only_and_external_only():
    row = _personal()
    with _notify_patch():
        _share(row["id"], OUTSIDER)
    assert client.get("/v1/admin/shares").status_code == 403
    _as(_user("root-sub", role="admin"))
    assert client.get("/v1/admin/shares").json() == {"shares": []}


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
