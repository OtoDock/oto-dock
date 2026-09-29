"""A link password is hashed only for a caller who may change it.

The hash is a cost-12 bcrypt through the shared password gate (the one the
sign-ins use), so the share routes judge the share, the caller's authority
over its target and the link's kind before any hash runs, and a password
change counts against the person's ``share_create`` bucket.

Run: cd proxy && venv/bin/pytest tests/api/test_share_link_password_order.py -v
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import config
from api.sharing import shares
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from storage import database as task_store
from storage.sharing import share_store

client = TestClient(app)

AGENT = "pw-order-agent"
OWNER = "pw-order-owner"
VIEWER = "pw-order-viewer"
ACCOUNT_PW = "owner-pass-123"


def _user(sub: str = OWNER, agent_roles: dict[str, str] | None = None) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=[AGENT],
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles)


def _as(user: UserContext) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture
def hashes(temp_db, tmp_path, monkeypatch):
    """The two people, the owner's account password, and a count of the
    link-password hashes the share routes run."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    for sub, name in ((OWNER, "Owner"), (VIEWER, "Viewer")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=%s, username=%s WHERE sub=%s",
                     (hash_password(ACCOUNT_PW), "pwowner", OWNER))
        conn.commit()
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    task_store.add_user_agent(VIEWER, AGENT, "viewer", "test")
    from auth import rate_limiter
    rate_limiter._attempts.clear()
    client.cookies.clear()
    seen: list[str] = []
    real = shares.hash_password_async

    async def counting(plain: str) -> str:
        seen.append(plain)
        return await real(plain)

    monkeypatch.setattr(shares, "hash_password_async", counting)
    _as(_user())
    yield seen
    app.dependency_overrides.pop(get_current_user, None)
    rate_limiter._attempts.clear()


def _app(shared: bool = True) -> dict:
    row = task_store.upsert_app(AGENT, "" if shared else "pwowner", None if shared else OWNER,
                                "board", title="Board",
                                rel_path=("workspace/apps/board.html" if shared
                                          else "users/pwowner/workspace/apps/board.html"))
    path = config.AGENTS_DIR / AGENT / row["rel_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<p>board</p>")
    return row


def _link(app_id: str, **extra) -> dict:
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": app_id,
                                        "scope": "external", "password": ACCOUNT_PW, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_password_for_an_unknown_share_runs_no_hash(hashes):
    r = client.patch(f"/v1/shares/{uuid.uuid4()}", json={"link_password": "new-secret-1"})
    assert r.status_code == 404
    assert hashes == []


def test_a_password_from_a_caller_who_may_not_share_runs_no_hash(hashes):
    out = _link(_app()["id"])
    hashes.clear()
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    r = client.patch(f"/v1/shares/{out['share']['id']}", json={"link_password": "new-secret-1"})
    assert r.status_code in (403, 404)
    assert hashes == []


def test_a_password_on_a_public_link_or_an_internal_share_runs_no_hash(hashes):
    row = _app()
    public = _link(row["id"], public=True)
    r = client.patch(f"/v1/shares/{public['share']['id']}", json={"link_password": "new-secret-1"})
    assert r.status_code == 400 and r.json()["detail"] == "Only a password link has a password"
    internal = share_store.create_internal_share(
        target_kind="app", target_id=row["id"], grantee_sub=VIEWER, created_by=OWNER,
        expires_at=None)
    r = client.patch(f"/v1/shares/{internal['id']}", json={"link_password": "new-secret-1"})
    assert r.status_code == 400
    assert hashes == []


def test_a_malformed_password_is_refused_before_anything_else(hashes):
    out = _link(_app()["id"])
    hashes.clear()
    r = client.patch(f"/v1/shares/{out['share']['id']}", json={"link_password": "short"})
    assert r.status_code == 400
    assert hashes == []


def test_a_password_change_by_the_sharer_lands(hashes):
    out = _link(_app()["id"])
    hashes.clear()
    r = client.patch(f"/v1/shares/{out['share']['id']}", json={"link_password": "new-secret-1"})
    assert r.status_code == 200, r.text
    assert hashes == ["new-secret-1"]
    from auth.password import verify_password
    row = share_store.get_share(out["share"]["id"])
    assert verify_password("new-secret-1", row["password_hash"])


def test_password_changes_count_against_the_persons_bucket(hashes, monkeypatch):
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "share_create",
                        {"max": 3, "window": 3600, "base_block": 1800, "max_block": 7200})
    out = _link(_app()["id"])
    hashes.clear()
    codes = [client.patch(f"/v1/shares/{out['share']['id']}",
                          json={"link_password": f"new-secret-{i}"}).status_code
             for i in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429
    assert len(hashes) == codes.count(200)


def test_a_link_for_a_target_the_caller_may_not_share_runs_no_hash(hashes, monkeypatch):
    row = _app()
    monkeypatch.setattr(shares.confirm, "confirm_human", AsyncMock(return_value=None))
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external", "link_password": "own-secret-1"})
    assert r.status_code == 403
    assert hashes == []
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": str(uuid.uuid4()),
                                        "scope": "external"})
    assert r.status_code == 404
    assert hashes == []
