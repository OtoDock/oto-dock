"""DELETE /v1/admin/remote-machines/{id} on a USER-paired machine.

The endpoint never scoped itself to admin-paired rows; the dashboard hid
the control. Admin → Remote machines shows it now, so this pins what the
call does for a user's machine: the same self-uninstall + cascade as the
owner's own Remove, plus a notification to the owner — unless the admin is
the owner, or the machine is admin-paired.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


def _app(role: str, sub: str = "admin-sub") -> FastAPI:
    from api.remote import remote_machines as rm

    user = UserContext(
        sub=sub, email="u@test.com", name="U",
        role=role, agents=[], agent_roles={},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


def _machine(scope: str, owner: str) -> dict:
    return {
        "id": "m-user", "name": "Alice laptop",
        "pairing_scope": scope, "registered_by": owner,
    }


@pytest.fixture
def stubs(monkeypatch):
    from api.remote import remote_machines as rm
    from storage import remote_store as rs
    from services.notifications import notification_manager as nm

    deleted: list[str] = []
    uninstalled: list[str] = []
    monkeypatch.setattr(rs, "delete_remote_machine", deleted.append)
    monkeypatch.setattr(
        rm, "_trigger_self_uninstall", AsyncMock(side_effect=uninstalled.append),
    )
    fired = AsyncMock(return_value=[])
    monkeypatch.setattr(nm, "fire_notification", fired)
    return {"deleted": deleted, "uninstalled": uninstalled, "fired": fired, "rs": rs}


def test_admin_removes_a_user_paired_machine_and_the_owner_is_told(stubs, monkeypatch):
    monkeypatch.setattr(stubs["rs"], "get_remote_machine", lambda mid: _machine("user", "owner-sub"))
    resp = TestClient(_app("admin")).delete("/v1/admin/remote-machines/m-user")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert stubs["uninstalled"] == ["m-user"]
    assert stubs["deleted"] == ["m-user"]
    stubs["fired"].assert_awaited_once()
    kw = stubs["fired"].await_args.kwargs
    assert kw["scope"] == "user"
    assert kw["target"] == "owner-sub"
    assert kw["source"] == "satellite"
    assert kw["source_id"] == "m-user"
    assert "Alice laptop" in kw["title"]
    assert "uninstall" in kw["body"]


def test_removing_your_own_user_paired_machine_sends_no_notification(stubs, monkeypatch):
    monkeypatch.setattr(stubs["rs"], "get_remote_machine", lambda mid: _machine("user", "admin-sub"))
    resp = TestClient(_app("admin", sub="admin-sub")).delete("/v1/admin/remote-machines/m-user")
    assert resp.status_code == 200
    assert stubs["deleted"] == ["m-user"]
    stubs["fired"].assert_not_awaited()


def test_an_admin_paired_machine_is_deleted_without_a_notification(stubs, monkeypatch):
    monkeypatch.setattr(stubs["rs"], "get_remote_machine", lambda mid: _machine("admin", "other-admin"))
    resp = TestClient(_app("admin")).delete("/v1/admin/remote-machines/m-user")
    assert resp.status_code == 200
    assert stubs["uninstalled"] == ["m-user"]
    assert stubs["deleted"] == ["m-user"]
    stubs["fired"].assert_not_awaited()


@pytest.mark.parametrize("role", ["member", "creator", "manager"])
def test_non_admins_get_403_and_nothing_happens(stubs, monkeypatch, role):
    monkeypatch.setattr(stubs["rs"], "get_remote_machine", lambda mid: _machine("user", "owner-sub"))
    resp = TestClient(_app(role)).delete("/v1/admin/remote-machines/m-user")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin required"
    assert stubs["deleted"] == []
    assert stubs["uninstalled"] == []
    stubs["fired"].assert_not_awaited()


def test_unknown_machine_is_404(stubs, monkeypatch):
    monkeypatch.setattr(stubs["rs"], "get_remote_machine", lambda mid: None)
    resp = TestClient(_app("admin")).delete("/v1/admin/remote-machines/nope")
    assert resp.status_code == 404
    assert stubs["deleted"] == []
    stubs["fired"].assert_not_awaited()
