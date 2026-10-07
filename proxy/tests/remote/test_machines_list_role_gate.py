"""GET /v1/admin/remote-machines stays a hard 403 for non-admins.

The dashboard's useRemoteMachines() is role-gated client-side (it must never
poll this endpoint as a non-admin — repeated 403s can trip network IDS/IPS
signatures and get the whole flow blocked); this pins the server side so the
data itself keeps its admin gate and a client regression is a 403, not a leak.

The admin list reads its rows in a worker thread and hands each row to the
live-status overlay, so an offline machine costs no store read on the loop.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


def _off_loop(fn):
    """``fn`` raises when called with a running loop; a worker thread has none."""
    def guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fn(*a, **kw)
        raise AssertionError(f"{fn.__name__} ran on the event loop")
    return guarded


def _app(role: str) -> FastAPI:
    from api.remote import remote_machines as rm

    user = UserContext(
        sub="user-sub-self", email="u@test.com", name="U",
        role=role, agents=[], agent_roles={},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


@pytest.mark.parametrize("role", ["member", "creator", "manager"])
def test_list_machines_403_for_non_admin(role):
    resp = TestClient(_app(role)).get("/v1/admin/remote-machines")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin access required"


def test_list_machines_200_for_admin(temp_db):
    resp = TestClient(_app("admin")).get("/v1/admin/remote-machines")
    assert resp.status_code == 200
    assert resp.json() == {"machines": []}


def test_list_machines_reads_the_store_off_the_loop(temp_db, monkeypatch):
    from storage import remote_store

    machine = remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()),
        name=f"offline-{uuid.uuid4().hex[:6]}",
        registered_by="user-admin",
    )
    # get_remote_machine is the per-row re-read the status overlay would do
    # for an offline machine if it were not handed the row.
    for name in ("get_all_remote_machines", "get_remote_machine"):
        monkeypatch.setattr(remote_store, name, _off_loop(getattr(remote_store, name)))

    resp = TestClient(_app("admin")).get("/v1/admin/remote-machines")
    assert resp.status_code == 200
    (row,) = resp.json()["machines"]
    assert row["id"] == machine["id"]
    assert row["status"] == "never_connected"
