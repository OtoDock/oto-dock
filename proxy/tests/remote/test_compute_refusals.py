"""A compute machine is registered and refused at every door it is not yet served at.

One test per door: the session's guard, the
layer, the resolver, an agent's default place, a person's own place, the
places a chat is offered, the link, the machine's own settings and its
updates, the delete's uninstall, the ``otodock`` command, the sync of a
machine with no session, the personal-machines switch and a terminal's claim
at a reconnect. Real temp DB where a store decides, stubs where a route does.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import placement
from storage import remote_store

_COMPUTE = {"id": "c-1", "name": "Compute one", "pairing_scope": "compute", "registered_by": "",
            "capabilities": "{}", "allow_full_fs": False, "device_grants": "[]"}
_ADMIN = {"id": "m-1", "name": "Rack", "pairing_scope": "admin", "registered_by": "sub-admin",
          "capabilities": "{}", "allow_full_fs": False, "device_grants": "[]"}


def _compute_row() -> dict:
    return remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()), name=f"compute-{uuid.uuid4().hex[:6]}",
        registered_by=None, pairing_scope=placement.PAIRING_COMPUTE)


def _client(monkeypatch, *, role: str = "admin", sub: str = "sub-admin") -> TestClient:
    from api.remote import compute_machines, remote_machines
    from auth.providers import UserContext, get_current_user

    user = UserContext(sub=sub, email="a@test.com", name="A", role=role, agents=[], agent_roles={})

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(remote_machines.router)
    app.add_exception_handler(remote_store.ComputeMachineRefused,
                              compute_machines.compute_machine_refused)  # type: ignore[arg-type]
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


# ---------------------------------------------------------------------------
# registered
# ---------------------------------------------------------------------------

def test_an_admin_registers_a_compute_machine_and_it_is_listed_with_no_owner(temp_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "http://192.168.50.2:8400", raising=False)
    monkeypatch.setattr(config, "MCPS_DIR", __import__("pathlib").Path("/opt/otodock/mcps"), raising=False)
    monkeypatch.setattr(config, "OTODOCK_PROXY_IMAGE", "ghcr.io/otodock/otodock-proxy:2.0.0", raising=False)
    client = _client(monkeypatch)
    r = client.post("/v1/admin/remote-machines/compute", json={"name": "Compute one"})
    assert r.status_code == 200, r.text
    made = r.json()
    row = remote_store.get_remote_machine(made["id"])
    assert row["pairing_scope"] == "compute" and row["registered_by"] == ""
    # One command, and no secret: the server makes its own.
    assert made["install_command"].endswith("| sudo bash") and "machine_secret" not in made
    listed = next(m for m in client.get("/v1/admin/remote-machines").json()["machines"]
                  if m["id"] == made["id"])
    assert listed["registered_by"] == "" and listed["owner_display_name"] == ""
    # The Compute card's block: not signed in, so no memory report and no slot yet; its command waits.
    assert listed["compute"] == {"own": False, "sessions": 0, "memory": None, "holding": False,
                                 "paired": False, "waiting": True, "refused_from": "", "shared": False,
                                 "host_behind": False}
    assert client.post("/v1/admin/remote-machines/compute", json={"name": "Compute one"}).status_code == 409
    assert client.post("/v1/admin/remote-machines/compute", json={"name": "  "}).status_code == 400


def test_only_an_admin_registers_one(temp_db, monkeypatch):
    client = _client(monkeypatch, role="member", sub="sub-member")
    assert client.post("/v1/admin/remote-machines/compute", json={"name": "Mine"}).status_code == 403


# ---------------------------------------------------------------------------
# refused: a session, the layer, the resolver
# ---------------------------------------------------------------------------

def test_the_guards_sentence_names_a_compute_machine(monkeypatch):
    from core.config import config_builder
    monkeypatch.setattr(config_builder.remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    said = config_builder._placement_refusal("c-1", "sub-admin", "admin", "c-1")
    assert said == "This chat is on Compute one, a compute machine. Chats do not run there yet."


def test_a_spawn_on_a_compute_machine_is_refused(monkeypatch):
    from core.remote import remote_session_start
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))

    class _Ctx:
        role = "admin"

    class _Config:
        security_context = _Ctx()
        user_sub = "sub-admin"
        placement_sub = None
        agent_name = "agent-a"

    assert remote_session_start.placement_refuses("c-1", _Config())
    # a compute row may name the admin who registered it: still no place for them
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda _mid: {**_COMPUTE, "registered_by": "sub-admin"})
    assert remote_session_start.placement_refuses("c-1", _Config())


def test_the_layer_of_a_compute_target_is_the_remote_one(monkeypatch):
    """A compute machine's id is never a stored place: it reaches the layer
    only as a live record's target (a terminal the machine holds), whose
    layer is the remote one."""
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.session import session_manager
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    layer = session_manager.get_execution_layer("agent-a", execution_path="claude-code-cli",
                                                user_sub="sub-admin", role="admin", execution_target="c-1")
    assert isinstance(layer, RemoteExecutionLayer)


def test_the_resolver_never_answers_a_compute_machine(temp_db, monkeypatch):
    """A compute default or override exists only by a hand edit of the
    database: the resolver answers the platform's place."""
    from services.remote import remote_status
    from storage.agents import agent_store
    from storage.identity import db_users
    from storage.pg import get_conn
    db_users.upsert_user("sub-admin", "a@test.com", "A", "admin")
    agent_store.create_agent("resolve-agent", "Resolve agent")
    made = _compute_row()
    with get_conn() as conn:
        conn.execute("UPDATE agents SET execution_target = %s WHERE slug = 'resolve-agent'", (made["id"],))
        conn.execute("INSERT INTO user_remote_targets (user_sub, machine_id, agent_slug, added_at) "
                     "VALUES ('sub-admin', %s, 'resolve-agent', '2026-11-07')", (made["id"],))
        conn.commit()
    agent_store._invalidate_cache()
    monkeypatch.setattr(remote_status, "is_reachable", lambda _mid: True)
    assert remote_status.resolve_execution_target("resolve-agent", "sub-admin", "admin")[0] == placement.LOCAL
    assert remote_status.resolve_execution_target("resolve-agent", None, "manager")[0] == placement.LOCAL


# ---------------------------------------------------------------------------
# refused: places
# ---------------------------------------------------------------------------

def test_a_compute_machine_is_no_agents_default_on_the_machine_card(temp_db, monkeypatch):
    from storage.agents import agent_store
    agent_store.create_agent("card-agent", "Card agent")
    made = _compute_row()
    r = _client(monkeypatch).post(f"/v1/admin/remote-machines/{made['id']}/agents", json={"agent_slug": "card-agent"})
    assert r.status_code == 400 and r.json()["detail"] == remote_store.COMPUTE_PLACE_REFUSED


def test_a_compute_machine_is_no_agents_default_in_agent_settings(monkeypatch):
    from api.agents import agents as agents_api
    from auth.providers import UserContext
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))

    class _Req:
        execution_target = "c-1"
        execution_paths: list = []

    admin = UserContext(sub="sub-admin", email="a@t", name="A", role="admin", agents=[], agent_roles={})
    with pytest.raises(Exception) as refused:
        asyncio.run(agents_api._check_execution_target("agent-a", _Req(), admin))
    assert getattr(refused.value, "status_code", 0) == 400
    assert getattr(refused.value, "detail", "") == remote_store.COMPUTE_PLACE_REFUSED


def test_an_admin_cannot_take_a_compute_machine_as_their_own_place(temp_db, monkeypatch):
    from storage.agents import agent_store
    agent_store.create_agent("own-agent", "Own agent")
    made = _compute_row()
    r = _client(monkeypatch).put("/v1/users/me/remote-targets/own-agent", json={"machine_id": made["id"]})
    assert r.status_code == 400 and r.json()["detail"] == remote_store.COMPUTE_PLACE_REFUSED
    assert remote_store.get_user_remote_targets("sub-admin") == []


def test_a_chat_is_never_offered_a_compute_machine(monkeypatch):
    """Even as the run person's default: the places come from the scope, and
    the default row is dropped if it is one."""
    from services.remote import chat_targets, remote_status
    from storage.agents import agent_store
    monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *_a, **_k: ("c-1", None))
    monkeypatch.setattr(remote_store, "get_visible_machines_for_user", lambda *_a, **_k: [])
    monkeypatch.setattr(remote_store, "get_all_remote_machines", lambda: [dict(_COMPUTE), dict(_ADMIN)])
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda mid: dict(_COMPUTE) if mid == "c-1" else dict(_ADMIN))
    monkeypatch.setattr(agent_store, "get_agent", lambda _a: {"execution_target": "local"})
    monkeypatch.setattr(remote_status, "get_live_machine_status",
                        lambda *_a, **_k: {"state": "online", "reachable": True})
    _default, targets = chat_targets.offered(
        "agent-a", chat_targets.RunPerson("sub-admin", "admin"), engine_path="claude-code-cli",
        admin_asker=True)
    assert "c-1" not in {t.target for t in targets}
    assert "m-1" in {t.target for t in targets}


# ---------------------------------------------------------------------------
# refused: the link, the machine's own settings, its updates, its removal
# ---------------------------------------------------------------------------

def test_the_link_refuses_a_compute_machine_with_its_own_close(monkeypatch):
    from ws import satellite
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    monkeypatch.setattr(remote_store, "verify_machine_secret",
                        lambda *_a: pytest.fail("a compute machine is refused before its secret"))

    class _Socket:
        def __init__(self):
            self.sent: list = []
            self.closed: tuple = ()

        async def send_text(self, text):
            self.sent.append(json.loads(text))

        async def close(self, code, reason=""):
            self.closed = (code, reason)

    ws = _Socket()
    assert asyncio.run(satellite._admitted_machine(ws, "c-1", "secret")) is None
    assert ws.closed == (4009, "not_served")
    assert ws.sent[0]["status"] == "rejected" and "action" not in ws.sent[0]


@pytest.mark.parametrize("method, path, body", [
    ("put", "/v1/admin/remote-machines/{id}/allow-full-fs", {"enabled": True}),
    ("put", "/v1/admin/remote-machines/{id}/device-grants", {"grants": ["computer"]}),
    ("put", "/v1/admin/remote-machines/{id}/all-roles", {"enabled": True}),
    ("put", "/v1/admin/remote-machines/{id}/max-sessions", {"max_sessions": 3}),
    ("put", "/v1/admin/remote-machines/{id}/auto-update", {"enabled": True}),
    ("post", "/v1/admin/remote-machines/{id}/update-now", None),
    ("put", "/v1/admin/remote-machines/{id}/browser-mode", {"mode": "own"}),
    ("put", "/v1/admin/remote-machines/{id}/browser-profile", {"profile": "Work"}),
    ("put", "/v1/admin/remote-machines/{id}/browser-unattended", {"enabled": True}),
    # an admin passes the owner check of these two on any machine
    ("put", "/v1/users/me/remote-machines/{id}/auto-update", {"enabled": True}),
    ("post", "/v1/users/me/remote-machines/{id}/update-now", None),
])
def test_a_compute_machine_takes_no_setting_and_no_update(temp_db, monkeypatch, method, path, body):
    made = _compute_row()
    client = _client(monkeypatch)
    url = path.format(id=made["id"])
    r = client.request(method.upper(), url, json=body) if body is not None else client.request(method.upper(), url)
    assert r.status_code == 400, (url, r.status_code, r.text)
    assert r.json()["detail"] == remote_store.COMPUTE_SETTING_REFUSED
    after = remote_store.get_remote_machine(made["id"])
    assert not after["allow_full_fs"] and not after["auto_update_enabled"] and not after["pending_update"]
    assert after["device_grants"] in ("[]", None, "") and not after["all_roles_run_here"]


@pytest.mark.parametrize("role, sub", [("admin", "sub-admin"), ("member", "sub-member")])
@pytest.mark.parametrize("method, path, body", [
    ("PUT", "/v1/users/me/remote-machines/{id}/allow-full-fs", {"enabled": True}),
    ("PUT", "/v1/users/me/remote-machines/{id}/device-grants", {"grants": ["computer"]}),
    ("PUT", "/v1/users/me/remote-machines/{id}/browser-mode", {"mode": "own"}),
])
def test_a_persons_routes_answer_a_compute_machines_own_sentence(temp_db, monkeypatch, role, sub, method, path, body):
    """Never "This is an admin-paired (platform) machine", whose toggle a
    compute machine does not have."""
    made = _compute_row()
    r = _client(monkeypatch, role=role, sub=sub).request(method, path.format(id=made["id"]), json=body)
    assert r.status_code == 400 and r.json()["detail"] == remote_store.COMPUTE_SETTING_REFUSED


def test_a_compute_machine_is_not_removed_from_a_persons_page(temp_db, monkeypatch):
    made = _compute_row()
    r = _client(monkeypatch).delete(f"/v1/users/me/remote-machines/{made['id']}")
    assert r.status_code == 403 and "admin-paired" not in r.json()["detail"]
    assert r.json()["detail"] == "A compute machine is removed from the admin Remote Machines page."
    assert remote_store.get_remote_machine(made["id"]) is not None


def test_the_app_answers_the_stores_refusal(temp_db):
    """The real app, its real handler: without it every refusal is a 500, which
    nothing a route's own test sees, since that test registers the handler."""
    from fastapi.testclient import TestClient as Client

    from app import app
    from auth.providers import UserContext, get_current_user

    assert remote_store.ComputeMachineRefused in app.exception_handlers
    made = _compute_row()
    admin = UserContext(sub="sub-admin", email="a@test.com", name="A", role="admin", agents=[], agent_roles={})

    async def _stub():
        return admin

    app.dependency_overrides[get_current_user] = _stub
    try:
        r = Client(app, raise_server_exceptions=False).put(
            f"/v1/admin/remote-machines/{made['id']}/max-sessions", json={"max_sessions": 3})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 400 and r.json()["detail"] == remote_store.COMPUTE_SETTING_REFUSED


def test_removing_a_compute_machine_sends_no_uninstall(temp_db, monkeypatch):
    from api.remote import remote_machines
    made = _compute_row()

    async def _no_uninstall(_mid):
        pytest.fail("a compute machine never removes itself on the platform's word")

    monkeypatch.setattr(remote_machines, "_trigger_self_uninstall", _no_uninstall)
    assert _client(monkeypatch).delete(f"/v1/admin/remote-machines/{made['id']}").status_code == 200
    assert remote_store.get_remote_machine(made["id"]) is None


# ---------------------------------------------------------------------------
# refused: what a machine asks for, the sync, the switch, a claim
# ---------------------------------------------------------------------------

def test_a_compute_machine_opens_no_session_on_its_own_request(monkeypatch):
    from core.session import otodock_session
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    with pytest.raises(otodock_session.OtodockSessionError, match="compute machine"):
        asyncio.run(otodock_session._owner_for_machine("c-1"))


def test_nothing_syncs_to_a_compute_machine(monkeypatch):
    from core.remote import remote_workspace_sync
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    got = asyncio.run(remote_workspace_sync.RemoteWorkspaceSyncMixin.resolve_machine_sync_identity(
        None, "c-1", "agent-a"))  # type: ignore[arg-type]  # the method reads nothing of its layer
    assert got is None


def test_turning_personal_machines_off_leaves_a_compute_machine_alone(monkeypatch):
    from api.auth import platform

    cleared: list = []

    class _Cm:
        def get_connected_machines(self):
            return ["c-1"]

    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", _Cm)
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    monkeypatch.setattr(remote_store, "clear_user_remote_targets_for_machine",
                        lambda mid: cleared.append(mid) or 0)
    monkeypatch.setattr(remote_store, "get_all_user_paired_machines", lambda: [])
    asyncio.run(platform._enforce_user_paired_disabled())
    assert cleared == []


_USER_KIND, _COMPUTE_KIND = placement.KIND_USER_REMOTE, placement.KIND_COMPUTE


@pytest.mark.parametrize("row, kind, username, external, claimed", [
    # A compute machine takes back what the platform placed on it: a compute
    # placement naming it, for a person.
    (dict(_COMPUTE), _COMPUTE_KIND, "alice", False, True),
    (dict(_COMPUTE), _USER_KIND, "alice", False, False),     # a context the platform did not place there
    (dict(_COMPUTE), _COMPUTE_KIND, "", False, False),       # nobody: no standing to read
    (dict(_COMPUTE), _COMPUTE_KIND, "alice", True, False),   # an outside person
    ({**_COMPUTE, "registered_by": "sub-alice"}, _USER_KIND, "alice", False, False),  # a registrar owns nothing
    (None, _COMPUTE_KIND, "alice", False, False),            # the machine's row is gone
    (None, _USER_KIND, "", False, False),
    ({**_COMPUTE, "pairing_scope": "user", "registered_by": "sub-alice"}, _USER_KIND, "", False, False),  # an empty sub owns nothing
    ({**_COMPUTE, "pairing_scope": "user", "registered_by": "sub-alice"}, _USER_KIND, "alice", False, True),
    ({**_COMPUTE, "pairing_scope": "user", "registered_by": "sub-alice"}, _USER_KIND, "alice", True, False),
    (dict(_ADMIN, id="c-1"), _USER_KIND, "alice", False, True),
    (dict(_ADMIN, id="c-1"), _USER_KIND, "alice", True, False),
])
def test_a_terminal_is_taken_back_only_on_a_machine_it_may_be(monkeypatch, caplog, row, kind, username, external,
                                                              claimed):
    from core.session import session_state
    from services.remote import machine_sessions

    ctx = type("Ctx", (), {"placement": placement.PlacementCapabilities(kind=kind, machine_id="c-1"),
                           "username": username, "role": "editor", "agent": "agent-a",
                           "is_external": external})()
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: ctx)
    monkeypatch.setattr(session_state, "security_context_expired", lambda _sid: False)
    monkeypatch.setattr(machine_sessions.task_store, "get_chat_by_session",
                        lambda _sid: {"id": "chat-1", "execution_target": "c-1", "agent": "agent-a"})
    monkeypatch.setattr(machine_sessions.task_store, "get_user_sub_by_username",
                        lambda name: {"alice": "sub-alice"}.get(name, ""))
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: row)
    from services.scheduler import run_recovery
    monkeypatch.setattr(run_recovery, "lost_standing", lambda *_a, **_k: set())
    monkeypatch.setattr(session_state, "reloaded_state", lambda _sid: ("ask", 0))
    from storage.chat import open_turns
    monkeypatch.setattr(open_turns, "get_open", lambda _cid: {})
    with caplog.at_level("INFO", logger="otodock.machine-sessions"):
        got = machine_sessions._claim_facts("c-1", "s-1")
    assert (got is not None) is claimed
    # A compute machine has no owner: its refusal names what it lacked.
    if not claimed and not external and placement.machine_is_compute(row):
        assert "refused: no person, or not placed there as compute" in caplog.text
        assert "owner" not in caplog.text
    elif not claimed and not external:
        assert "refused: not the machine's owner" in caplog.text


def test_a_compute_terminal_is_taken_back_only_for_its_chat_on_its_place(monkeypatch):
    """The other checks stand on a compute machine: a chat pinned to another
    machine, a task's chat, an expired token and a lowered person are refused,
    and a chat pinned ``local`` (the place compute serves) is taken back."""
    from core.session import session_state
    from services.remote import machine_sessions
    from services.scheduler import run_recovery
    from storage.chat import open_turns

    ctx = type("Ctx", (), {"placement": placement.PlacementCapabilities(kind=_COMPUTE_KIND, machine_id="c-1"),
                           "username": "alice", "role": "editor", "agent": "agent-a", "is_external": False})()
    chat = {"id": "chat-1", "execution_target": "local", "agent": "agent-a"}
    lost: set = set()
    expired = [False]
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: ctx)
    monkeypatch.setattr(session_state, "security_context_expired", lambda _sid: expired[0])
    monkeypatch.setattr(machine_sessions.task_store, "get_chat_by_session", lambda _sid: dict(chat))
    monkeypatch.setattr(machine_sessions.task_store, "get_user_sub_by_username", lambda name: "sub-alice")
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    monkeypatch.setattr(run_recovery, "lost_standing", lambda *_a, **_k: lost)
    monkeypatch.setattr(session_state, "reloaded_state", lambda _sid: ("ask", 0))
    monkeypatch.setattr(open_turns, "get_open", lambda _cid: {})
    assert machine_sessions._claim_facts("c-1", "s-1") is not None
    chat["execution_target"] = "m-other"
    assert machine_sessions._claim_facts("c-1", "s-1") is None
    chat.update(execution_target="local", id="task-run-1")
    assert machine_sessions._claim_facts("c-1", "s-1") is None
    chat["id"] = "chat-1"
    expired[0] = True
    assert machine_sessions._claim_facts("c-1", "s-1") is None
    expired[0] = False
    lost.add("s-1")
    assert machine_sessions._claim_facts("c-1", "s-1") is None
