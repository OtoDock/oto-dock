"""The first-user setup (``POST /auth/setup``) makes one owner, and only on
an install with no user at all.

The strength check and the bcrypt run in worker threads for hundreds of
milliseconds between the first count and the create, so the store counts
again and inserts in one transaction under a table lock
(``create_first_owner``): a second setup, or a person who signed in
meanwhile (a first SSO login, committed or still in flight), ends the setup
with the same 403 as a finished one.

Run: cd proxy && venv/bin/pytest tests/auth/test_setup_first_user.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.auth import setup
from auth.password import hash_password
from storage import database as db
from storage.pg import get_conn

_PW = "a-Long-and-Strong-pass-9713"
_HASH = hash_password(_PW)


@pytest.fixture
def fresh_install(monkeypatch):
    """No users, a quick stand-in hash, and none of the catalog work."""
    with get_conn() as conn:
        conn.execute("SET LOCAL session_replication_role = 'replica'")
        conn.execute("DELETE FROM users")
        conn.commit()

    async def slow_hash(_plain: str) -> str:
        await asyncio.sleep(0.2)
        return _HASH

    monkeypatch.setattr(setup, "hash_password_async", slow_hash)
    from services.community import community_agent_installer, default_agent_assigner

    async def no_install(**_kw):
        return {}

    monkeypatch.setattr(community_agent_installer, "install_from_catalog", no_install)
    monkeypatch.setattr(default_agent_assigner, "assign_default_agents", lambda _sub: None)
    return slow_hash


async def _setup(email: str) -> int:
    try:
        r = await setup.setup_first_user(setup.SetupRequest(
            email=email, password=_PW, display_name=email.split("@")[0]))
        return r.status_code
    except HTTPException as e:
        return e.status_code


def _owners() -> list[dict]:
    return [u for u in db.list_users() if u.get("is_owner")]


def test_two_setups_at_once_make_one_owner(fresh_install):
    async def scenario():
        return await asyncio.gather(_setup("first@t.com"), _setup("second@t.com"))

    codes = asyncio.run(scenario())
    assert sorted(codes) == [200, 403]
    assert len(db.list_users()) == 1 and len(_owners()) == 1


def test_a_user_created_during_the_hash_ends_the_setup(fresh_install, monkeypatch):
    async def hash_while_someone_signs_in(plain: str) -> str:
        await asyncio.to_thread(db.upsert_user, "oidc:someone", "someone@t.com",
                                "Someone", "member")
        return await fresh_install(plain)

    monkeypatch.setattr(setup, "hash_password_async", hash_while_someone_signs_in)
    assert asyncio.run(_setup("late@t.com")) == 403
    assert _owners() == []
    assert db.get_user_by_email("late@t.com") is None


def test_a_finished_setup_answers_403_before_any_hash(fresh_install, monkeypatch):
    assert asyncio.run(_setup("owner@t.com")) == 200
    ran: list[str] = []

    async def never(plain: str) -> str:
        ran.append(plain)
        return _HASH

    monkeypatch.setattr(setup, "hash_password_async", never)
    assert asyncio.run(_setup("again@t.com")) == 403
    assert ran == []


def test_the_store_creates_the_first_owner_only_while_no_user_exists(fresh_install):
    sub = db.create_first_owner("owner@t.com", "Owner", _HASH)
    row = db.get_user(sub)
    assert row["is_owner"] and row["role"] == "admin" and row["auth_provider"] == "local"
    assert db.create_first_owner("second@t.com", "Second", _HASH) is None
    assert len(db.list_users()) == 1


def test_two_store_creates_at_once_make_one_owner(fresh_install):
    async def scenario():
        return await asyncio.gather(
            asyncio.to_thread(db.create_first_owner, "a@t.com", "A", _HASH),
            asyncio.to_thread(db.create_first_owner, "b@t.com", "B", _HASH))

    subs = asyncio.run(scenario())
    assert sum(1 for s in subs if s) == 1 and len(_owners()) == 1


def test_a_first_sso_login_in_flight_at_the_insert_ends_the_setup(fresh_install):
    """A first SSO login whose insert has not committed when the setup
    creates the owner: the create waits for it, counts it, and the setup
    ends with the 403 of a finished one."""
    async def scenario():
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (sub, email, name, role, created_at, last_login, username) "
                "VALUES (%s, %s, %s, %s, now()::text, now()::text, %s)",
                ("oidc:inflight", "inflight@t.com", "In Flight", "member", "in-flight"))
            task = asyncio.create_task(_setup("late@t.com"))
            await asyncio.sleep(0.8)
            conn.commit()
        return await task

    assert asyncio.run(scenario()) == 403
    assert _owners() == []
    assert db.get_user_by_email("late@t.com") is None


def test_a_create_that_waits_past_the_lock_timeout_answers_503(fresh_install, monkeypatch):
    from psycopg.errors import LockNotAvailable

    def stuck(*_a):
        raise LockNotAvailable("canceling statement due to lock timeout")

    monkeypatch.setattr(db, "create_first_owner", stuck)
    with pytest.raises(HTTPException) as e:
        asyncio.run(setup.setup_first_user(setup.SetupRequest(
            email="owner@t.com", password=_PW, display_name="Owner")))
    assert e.value.status_code == 503 and e.value.headers["Retry-After"] == "5"


def test_the_setup_reads_the_session_length_once_off_the_loop(fresh_install, monkeypatch):
    import config
    calls: list[str] = []

    def hours() -> int:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("worker")
        else:
            calls.append("loop")
        return 7

    monkeypatch.setattr(config, "get_jwt_expiry_hours", hours)

    async def scenario():
        return await setup.setup_first_user(setup.SetupRequest(
            email="owner@t.com", password=_PW, display_name="Owner"))

    r = asyncio.run(scenario())
    assert calls == ["worker"]
    assert f"Max-Age={7 * 3600}" in r.headers["set-cookie"]
