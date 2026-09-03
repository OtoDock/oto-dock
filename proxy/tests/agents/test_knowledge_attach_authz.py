"""Writable knowledge attach requires EDITOR+ on the source agent.

A writable attachment is a write channel into the source's knowledge tree
(the projector adopts mirror edits and fans them out to every consumer,
bulletin included) — viewer-tier read access on the source must not grant
it. Read-only attach keeps the viewer bar.
"""

import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext, get_current_user
from storage import agent_store, db_knowledge_libraries

SRC = "kaa-src"
CON = "kaa-con"
CREATOR_SUB = "user-kaa-creator"


def _app(source_role: str) -> FastAPI:
    from api.agents import knowledge_libraries as kl

    user = UserContext(
        sub=CREATOR_SUB, email="c@test.com", name="C", role="creator",
        agents=[SRC, CON],
        agent_roles={SRC: source_role, CON: "manager"},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(kl.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


@pytest.fixture
def attach_env(temp_db):
    for slug in (SRC, CON):
        shutil.rmtree(config.get_agent_dir(slug), ignore_errors=True)
    agent_store.create_agent(SRC, "Src", collaborative=True, default_scope="agent")
    agent_store.create_agent(CON, "Con", collaborative=True, default_scope="agent")
    (config.get_agent_dir(SRC) / "knowledge").mkdir(parents=True, exist_ok=True)
    db_knowledge_libraries.promote(SRC, created_by="user-admin")
    return temp_db


def _attach(app, writable: bool):
    return TestClient(app).put(
        f"/v1/agents/{CON}/knowledge-attachments",
        json={"source_agent": SRC, "writable": writable},
    )


def test_viewer_on_source_cannot_attach_writable(attach_env):
    resp = _attach(_app("viewer"), writable=True)
    assert resp.status_code == 403
    assert "editor" in resp.json()["detail"].lower()
    assert db_knowledge_libraries.writable_pairs_for(CON) == frozenset()


def test_viewer_on_source_can_attach_read_only(attach_env):
    resp = _attach(_app("viewer"), writable=False)
    assert resp.status_code == 200
    assert resp.json()["writable"] is False


def test_editor_on_source_can_attach_writable(attach_env):
    resp = _attach(_app("editor"), writable=True)
    assert resp.status_code == 200
    assert db_knowledge_libraries.writable_pairs_for(CON) == {(SRC, "")}
