"""GET /v1/chats/{chat_id}/project — the Projects overlay's lane graph.

Anchor authz = the anchor chat's own access rule; sibling lanes are filtered
per row, so cross-owner lanes of a shared project never leak to a viewer who
couldn't open them directly.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from storage.agents import agent_store
from storage import database as task_store


AGENT = "pa"


def _user(sub="user-alice", role="member", agents=(AGENT,)):
    return UserContext(
        sub=sub, email=f"{sub}@test.com", name=sub, role=role,
        agents=list(agents), agent_roles={a: "editor" for a in agents},
    )


def _mk_chat(owner: str, *, project: str = "", role: str = "", agent: str = AGENT,
             parent: str = "", origin: str = "dashboard") -> str:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, owner, agent, origin=origin,
                           parent_chat_id=parent)
    if project or role:
        task_store.update_chat(cid, project_id=project, delegate_role=role)
    return cid


@pytest.fixture
def client(temp_db):
    from api.agents import chats as chats_api

    agent_store.create_agent(AGENT, "PA", collaborative=True, default_scope="user")

    app = FastAPI()
    app.include_router(chats_api.router)
    app.state.user = _user()

    async def _current_user():
        return app.state.user

    app.dependency_overrides[get_current_user] = _current_user
    c = TestClient(app)
    c.app_ref = app
    return c


class TestChatProjectEndpoint:
    def test_404_for_missing_chat(self, client):
        r = client.get(f"/v1/chats/{uuid.uuid4()}/project")
        assert r.status_code == 404

    def test_404_for_chat_without_delegation_markers(self, client):
        cid = _mk_chat("user-alice")
        r = client.get(f"/v1/chats/{cid}/project")
        assert r.status_code == 404

    def test_lineage_fallback_for_projectless_orchestrator(self, client):
        # A plain delegation (no project_id) still gets the dock: the graph
        # falls back to the anchor's lineage — itself plus its spawned workers.
        orch = _mk_chat("user-alice", role="orchestrator")
        w1 = _mk_chat("user-alice", parent=orch, origin="delegated")
        _mk_chat("user-alice")  # unrelated chat stays out
        r = client.get(f"/v1/chats/{orch}/project")
        assert r.status_code == 200
        data = r.json()
        assert data["project_id"] == ""
        assert {c["id"] for c in data["chats"]} == {orch, w1}

    def test_lineage_fallback_from_worker_anchor(self, client):
        orch = _mk_chat("user-alice", role="orchestrator")
        w1 = _mk_chat("user-alice", parent=orch, origin="delegated")
        w2 = _mk_chat("user-alice", parent=orch, origin="delegated")
        r = client.get(f"/v1/chats/{w1}/project")
        assert r.status_code == 200
        assert {c["id"] for c in r.json()["chats"]} == {orch, w1, w2}

    def test_lineage_fallback_lone_delegated_worker(self, client):
        # A delegated chat with no parent row (e.g. task-surface worker):
        # graceful single-lane graph, never a 404.
        w = _mk_chat("user-alice", origin="delegated")
        r = client.get(f"/v1/chats/{w}/project")
        assert r.status_code == 200
        assert {c["id"] for c in r.json()["chats"]} == {w}

    def test_403_for_foreign_anchor(self, client):
        cid = _mk_chat("user-bob", project="p1", role="orchestrator")
        r = client.get(f"/v1/chats/{cid}/project")
        assert r.status_code == 403

    def test_lists_own_project_lanes_with_roles_and_status(self, client):
        orch = _mk_chat("user-alice", project="p1", role="orchestrator")
        w1 = _mk_chat("user-alice", project="p1", role="worker")
        w2 = _mk_chat("user-alice", project="p1", role="worker")
        _mk_chat("user-alice", project="other-project", role="worker")

        r = client.get(f"/v1/chats/{orch}/project")
        assert r.status_code == 200
        data = r.json()
        assert data["project_id"] == "p1"
        ids = {c["id"] for c in data["chats"]}
        assert ids == {orch, w1, w2}
        by_id = {c["id"]: c for c in data["chats"]}
        assert by_id[orch]["delegate_role"] == "orchestrator"
        assert by_id[w1]["status"] == "idle"
        assert set(by_id[w1]) == {"id", "title", "agent", "delegate_role",
                                  "parent_chat_id", "status", "updated_at"}

    def test_project_rows_carry_lineage_for_round_scoping(self, client):
        # Two delegation rounds under one reused slug: every row exposes its
        # parent_chat_id so the dock can scope live cards to the anchor's round.
        orch1 = _mk_chat("user-alice", project="p1", role="orchestrator")
        w1 = _mk_chat("user-alice", project="p1", role="worker", parent=orch1)
        orch2 = _mk_chat("user-alice", project="p1", role="orchestrator")
        w2 = _mk_chat("user-alice", project="p1", role="worker", parent=orch2)

        r = client.get(f"/v1/chats/{orch2}/project")
        assert r.status_code == 200
        by_id = {c["id"]: c for c in r.json()["chats"]}
        assert by_id[w1]["parent_chat_id"] == orch1
        assert by_id[w2]["parent_chat_id"] == orch2
        assert by_id[orch2]["parent_chat_id"] == ""

    def test_foreign_lanes_filtered_not_leaked(self, client):
        anchor = _mk_chat("user-alice", project="p2", role="orchestrator")
        _mk_chat("user-bob", project="p2", role="worker")

        r = client.get(f"/v1/chats/{anchor}/project")
        assert r.status_code == 200
        assert {c["id"] for c in r.json()["chats"]} == {anchor}

    def test_worker_anchor_sees_the_same_graph(self, client):
        orch = _mk_chat("user-alice", project="p3", role="orchestrator")
        w1 = _mk_chat("user-alice", project="p3", role="worker")
        r = client.get(f"/v1/chats/{w1}/project")
        assert r.status_code == 200
        assert {c["id"] for c in r.json()["chats"]} == {orch, w1}

    def test_admin_sees_all_lanes(self, client):
        client.app_ref.state.user = _user(sub="root", role="admin")
        anchor = _mk_chat("user-alice", project="p4", role="orchestrator")
        foreign = _mk_chat("user-bob", project="p4", role="worker")
        r = client.get(f"/v1/chats/{anchor}/project")
        assert r.status_code == 200
        assert {c["id"] for c in r.json()["chats"]} == {anchor, foreign}
