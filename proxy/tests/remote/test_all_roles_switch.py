"""An admin machine's "Every role of its agents runs here": the column and
its forward migration, the store, and the admin route. The placement it
decides is tested beside the resolver
(``tests/execution/test_resolve_execution_target_role.py``) and the guard
(``tests/remote/test_placement.py``)."""

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from storage import remote_store
from storage.pg import get_conn


def test_the_column_is_added_forward_and_an_existing_machine_is_off(temp_db):
    """An install upgraded from 1.9.0 has no ``all_roles_run_here``: the
    start's migration adds it off on every existing machine, and a run on a
    database that has it keeps an admin's choice."""
    from storage import pg
    from storage.remote_schema import migrate_remote
    machine = remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()), name=f"rack-{uuid.uuid4().hex[:6]}",
        registered_by="user-sub-admin")
    try:
        with get_conn() as conn:
            conn.execute("ALTER TABLE remote_machines DROP COLUMN IF EXISTS all_roles_run_here")
            migrate_remote(conn)
            conn.commit()
        # The DROP and the ADD moved the column to the row's end: every pooled
        # session's cached plan of a SELECT * is stale, so the pools close.
        pg.close_pool(timeout=2.0)
        assert remote_store.get_remote_machine(machine["id"])["all_roles_run_here"] is False
        remote_store.set_all_roles_run_here(machine["id"], True)
        with get_conn() as conn:
            migrate_remote(conn)
            conn.commit()
        assert remote_store.get_remote_machine(machine["id"])["all_roles_run_here"] is True
        assert next(m for m in remote_store.get_all_remote_machines()
                    if m["id"] == machine["id"])["all_roles_run_here"] is True
        remote_store.set_all_roles_run_here(machine["id"], False)
        assert remote_store.get_remote_machine(machine["id"])["all_roles_run_here"] is False
    finally:
        pg.close_pool(timeout=2.0)


def _app(monkeypatch, *, role, machine):
    from api.remote import remote_machines as rm
    from auth.providers import UserContext, get_current_user

    user = UserContext(sub="user-sub-x", email="x@test.com", name="X", role=role,
                       agents=[], agent_roles={})

    async def _stub_user():
        return user

    writes: list = []
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda mid: dict(machine) if mid == machine["id"] else None)
    monkeypatch.setattr(remote_store, "set_all_roles_run_here",
                        lambda mid, on: writes.append((mid, on)))
    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return TestClient(app), writes


_RACK = {"id": "m-1", "name": "rack", "pairing_scope": "admin", "registered_by": "user-sub-x"}
_URL = "/v1/admin/remote-machines/{}/all-roles"


def test_an_admin_turns_it_on_and_off(monkeypatch):
    client, writes = _app(monkeypatch, role="admin", machine=_RACK)
    r = client.put(_URL.format("m-1"), json={"enabled": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "all_roles_run_here": True}
    r = client.put(_URL.format("m-1"), json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"ok": True, "all_roles_run_here": False}
    assert writes == [("m-1", True), ("m-1", False)]


def test_a_personal_machine_a_missing_one_and_a_non_admin_are_refused(monkeypatch):
    mine = dict(_RACK, pairing_scope="user")
    client, writes = _app(monkeypatch, role="admin", machine=mine)
    r = client.put(_URL.format("m-1"), json={"enabled": True})
    assert r.status_code == 403
    assert r.json()["detail"] == ("This setting is for the machines an admin paired for the "
                                  "platform. A person's own computer runs only their chats.")
    assert client.put(_URL.format("m-gone"), json={"enabled": True}).status_code == 404
    creator, creator_writes = _app(monkeypatch, role="creator", machine=_RACK)
    assert creator.put(_URL.format("m-1"), json={"enabled": True}).status_code == 403
    assert writes == [] and creator_writes == []
