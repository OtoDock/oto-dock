"""The public routes a person's API key reaches (``api/public/``) and the key
as a principal of its own kind (``auth/api_keys.py``): the mint's rules, the
checks at every call, the reach of each route, and send and wait."""

from __future__ import annotations

import asyncio
import functools
import json
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.params import Param
from fastapi.testclient import TestClient

from api.public import key_turns
from app import app
from auth import roles
from auth.providers import create_session_jwt
from core.session import session_kind
from services.infra import api_key_manager as akm
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import api_key_store
from storage.pg import get_conn
from tests._compute import serve, serve_none

from auth import rate_limiter as _limiter  # isort: skip

# The limiter's own functions, taken before any fixture stubs them.
_REAL_CHECK, _REAL_HIT = _limiter.check_rate_limit, _limiter.hit

A, B = "pub-a", "pub-b"
# Subs as long as a real install's (the notification route reads a short
# target as a username).
ME, OTHER = "0b7c4a8e-key-person-me-000000000001", "0b7c4a8e-key-person-other-0000000002"
ADMIN = "user-admin"
PASSWORD = "a long enough password 2026"


@pytest.fixture
def world(temp_db, monkeypatch):
    from auth import api_keys
    from auth.password import hash_password
    from services.scheduler import shared
    # ``with _by(key)`` runs the app's lifespan, whose end raises the
    # scheduler's shutdown flag: it comes down with this fixture.
    monkeypatch.setattr(shared, "_shutting_down", False)
    api_keys.forget()
    akm.clear_keys_off_cache()
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        for sub, name in ((ME, "viewer"), (OTHER, "viewer2")):
            conn.execute("INSERT INTO users (sub, email, name, role, created_at, last_login) "
                         "VALUES (%s, %s, %s, 'member', %s, %s)",
                         (sub, f"{name}@key.test", name, now, now))
    for slug in (A, B):
        agent_store.create_agent(slug, slug.upper(), created_by=ADMIN)
    _assign(ME, A, roles.VIEWER)
    _assign(ME, B, roles.VIEWER)
    _assign(OTHER, A, roles.VIEWER)
    for sub in (ME, OTHER, ADMIN):
        with get_conn() as conn:
            conn.execute("UPDATE users SET password_hash=%s, username=%s, "
                         "password_changed_at=%s WHERE sub=%s",
                         (hash_password(PASSWORD), task_store.get_user(sub)["name"].split()[0].lower(),
                          (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), sub))
    from services.engines import engine_resolution
    monkeypatch.setattr(engine_resolution, "resolve_session_engine",
                        lambda agent, sub: types.SimpleNamespace(execution_path="direct-llm", model="m"))
    from auth import rate_limiter
    monkeypatch.setattr(rate_limiter, "check_rate_limit", lambda *a, **k: (True, 0))
    monkeypatch.setattr(rate_limiter, "hit", lambda *a, **k: (True, 0))
    # A compute machine serves the platform's place, so a key's turn in a
    # chat of it has somewhere to run.
    serve(monkeypatch)
    yield
    api_keys.forget()


@pytest.fixture
def limits(world, monkeypatch):
    """The real limiter on the two key buckets, with small rules: three
    calls a key, three unknown keys an address."""
    import config
    from auth import rate_limiter
    monkeypatch.setattr(rate_limiter, "check_rate_limit", _REAL_CHECK)
    monkeypatch.setattr(rate_limiter, "hit", _REAL_HIT)
    for bucket in ("api_key_call", "api_key_fail"):
        monkeypatch.setitem(config.RATE_LIMIT_RULES, bucket,
                            {"max": 3, "window": 60, "base_block": 60, "max_block": 600})
    rate_limiter._attempts.clear()
    yield
    rate_limiter._attempts.clear()


def _assign(sub: str, agent: str, role: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
                     "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sub, agent) DO UPDATE "
                     "SET agent_role = EXCLUDED.agent_role",
                     (sub, agent, datetime.now(timezone.utc).isoformat(), ADMIN, role))


def _cookie(sub: str, role: str = roles.MEMBER) -> TestClient:
    user = task_store.get_user(sub)
    return TestClient(app, cookies={"session": create_session_jwt(sub, user["email"], user["name"], role)})


def _key(sub: str = ME, scopes=None, *, agents=(), unasked: bool = False, days: int = 7) -> str:
    """A key as the mint makes it: every scope it may hold (``tasks`` only
    on a key that runs without asking)."""
    if scopes is None:
        scopes = ("chat", "tasks", "files", "notifications") if unasked else (
            "chat", "files", "notifications")
    row, raw = akm.create_user_key(user_sub=sub, name="walk", permissions=list(scopes),
                                   agents=list(agents), expires_in_days=days,
                                   run_without_asking=unasked)
    return raw


def _stored_key(sub: str = ME, scopes=("chat", "tasks"), **row) -> str:
    """A key row written past the mint's rules (a row from before a rule,
    or one changed in the database)."""
    raw, prefix = akm._generate_raw_key()
    row.setdefault("expires_at", (datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    api_key_store.create_user_api_key(user_sub=sub, name="stored", key_hash=akm._hash_key(raw),
                                      prefix=prefix, permissions=list(scopes), **row)
    return raw


def _by(key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


# ── the mint ───────────────────────────────────────────────────────────────

def test_a_new_key_needs_an_expiry_and_past_triggers_the_password(world):
    c = _cookie(ME)
    assert c.post("/v1/user-api-keys", json={"name": "k"}).status_code == 422
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 400})
    assert r.status_code == 400 and "365" in r.json()["detail"]
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7,
                                          "permissions": ["triggers"]})
    assert r.status_code == 200 and r.json()["expires_at"]
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7,
                                          "permissions": ["chat"]})
    assert r.status_code == 428 and r.json()["detail"]["method"] == "password"
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7,
                                          "permissions": ["triggers"], "run_without_asking": True})
    assert r.status_code == 428
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7, "agents": ["pub-x"],
                                          "permissions": ["chat"], "password": PASSWORD})
    assert r.status_code == 400
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7, "agents": [A],
                                          "permissions": ["chat"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == [A] and r.json()["key"].startswith("otok_")
    # A key mints no key, nor lists or revokes them.
    key = r.json()["key"]
    assert _by(key).get("/v1/user-api-keys").status_code == 403


def test_the_tasks_permission_needs_a_key_that_runs_without_asking(world):
    c = _cookie(ME)
    # Refused before the password is asked: the pair is wrong whoever asks.
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7,
                                          "permissions": ["chat", "tasks"]})
    assert r.status_code == 400 and r.json()["detail"] == akm.TASKS_NEED_UNASKED
    with pytest.raises(ValueError, match="nobody to ask"):
        akm.create_user_key(user_sub=ME, name="k", permissions=["tasks"], expires_in_days=7)
    r = c.post("/v1/user-api-keys", json={"name": "k", "expires_in_days": 7, "password": PASSWORD,
                                          "permissions": ["tasks"], "run_without_asking": True})
    assert r.status_code == 200, r.text


# ── the checks at every call ───────────────────────────────────────────────

def test_a_key_reaches_its_list_only(world):
    key = _key()
    r = _by(key).get("/v1/api/me")
    assert r.status_code == 200, r.text
    assert r.json()["key"]["scopes"] == ["chat", "files", "notifications"]
    for path in ("/v1/chats", "/v1/agents", "/v1/admin/users", "/v1/settings/catalog",
                 "/v1/user-api-keys", "/v1/tasks"):
        assert _by(key).get(path).status_code == 403, path
    assert _by(key).put("/v1/users/me/password", json={}).status_code == 403
    assert _by("otok_" + "x" * 43).get("/v1/api/me").status_code == 401
    assert TestClient(app).get("/v1/api/me").status_code == 401


def test_a_scope_the_key_lacks_is_refused(world):
    key = _key(scopes=("notifications",))
    assert _by(key).get("/v1/api/chats").status_code == 403
    assert _by(key).get("/v1/api/notifications").status_code == 200


def test_a_cookie_reaches_only_the_description_and_the_command(world):
    c = _cookie(ME)
    assert c.get("/v1/api/chats").status_code == 401
    assert c.get("/v1/api/openapi.json").status_code in (200, 404)
    assert TestClient(app).get("/v1/api/openapi.json").status_code == 401


def test_a_key_beside_a_valid_cookie_is_still_only_a_key(world):
    """The bearer decides who is calling: an admin's cookie beside it lends
    the key nothing."""
    from auth import api_keys
    admin = task_store.get_user(ADMIN)
    both = TestClient(app, headers={"Authorization": f"Bearer {_key()}"}, cookies={
        "session": create_session_jwt(ADMIN, admin["email"], admin["name"], roles.ADMIN)})
    for path in ("/v1/chats", "/v1/admin/users", "/v1/user-api-keys", "/v1/admin/api-keys"):
        r = both.get(path)
        assert r.status_code == 403 and r.json()["detail"] == api_keys.NOT_LISTED, (path, r.text)
    assert both.get("/v1/api/me").json()["email"] == "viewer@key.test"


def test_another_method_or_a_doubled_slash_gets_a_key_past_no_gate(world):
    from auth import api_keys
    c = _by(_key())
    for method, path in (("HEAD", "/v1/api/me"), ("OPTIONS", "/v1/api/me"), ("DELETE", "/v1/api/me"),
                         ("GET", "/v1/api//me"), ("GET", "/v1//api/me"), ("GET", "/v1/api/me/")):
        r = c.request(method, path)
        assert r.status_code == 403, (method, path, r.status_code)
        if method != "HEAD":
            assert r.json()["detail"] == api_keys.NOT_LISTED, (method, path)


def test_a_cached_verdict_ends_with_the_keys_standing(world):
    """A key checked a moment ago answers from the cache, and the cache
    holds no verdict: the row, the switch and the person are read again."""
    from auth import api_keys
    key = _key()
    c = _by(key)
    assert c.get("/v1/api/me").status_code == 200
    key_id = api_keys.cached_key_id(key)
    assert key_id

    def _ends(when: timedelta) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE user_api_keys SET expires_at=%s WHERE id=%s",
                         ((datetime.now(timezone.utc) + when).isoformat(), key_id))

    _ends(timedelta(seconds=-1))
    assert c.get("/v1/api/me").json()["detail"] == "This key has expired."
    _ends(timedelta(days=7))
    assert c.get("/v1/api/me").status_code == 200
    task_store.set_platform_setting(akm.KEYS_OFF_SETTING, "1")
    akm.clear_keys_off_cache()
    assert c.get("/v1/api/me").json()["detail"] == "API keys are switched off on this platform."
    task_store.set_platform_setting(akm.KEYS_OFF_SETTING, "0")
    akm.clear_keys_off_cache()
    assert c.get("/v1/api/me").status_code == 200
    # Signed out everywhere: the person's epoch moves past the key's making.
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET token_epoch_at=%s WHERE sub=%s", (later, ME))
    r = c.get("/v1/api/me")
    assert r.status_code == 401 and "signed out everywhere" in r.json()["detail"]
    assert api_keys.cached_key_id(key) == key_id


@pytest.mark.asyncio
async def test_a_session_token_mints_no_key(world):
    """An agent's session acts for its person and still makes no key."""
    from fastapi import HTTPException

    from api.auth import user_api_keys
    from auth.providers import PrincipalKind, UserContext
    session = UserContext(sub=ME, email="viewer@key.test", name="viewer", role=roles.MEMBER,
                          is_api_key=True)
    assert session.kind is PrincipalKind.USER_SESSION
    with pytest.raises(HTTPException) as e:
        await user_api_keys.create_user_key(user_api_keys.CreateUserKeyRequest(
            name="k", expires_in_days=7, permissions=["triggers"]), user=session)
    assert e.value.status_code == 403
    assert api_key_store.list_user_api_keys(user_sub=ME, include_revoked=True) == []


def test_an_expired_key_a_password_change_and_the_switch_end_the_key(world):
    key = _key()
    assert _by(key).get("/v1/api/me").status_code == 200
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_changed_at=%s, token_epoch_at=%s WHERE sub=%s",
                     (later, later, ME))
    r = _by(key).get("/v1/api/me")
    assert r.status_code == 401 and "password" in r.json()["detail"]

    key2 = _key(OTHER)
    with get_conn() as conn:
        conn.execute("UPDATE user_api_keys SET expires_at=%s WHERE user_sub=%s",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), OTHER))
    assert _by(key2).get("/v1/api/me").json()["detail"] == "This key has expired."

    key3 = _key(ADMIN)
    task_store.set_platform_setting(akm.KEYS_OFF_SETTING, "1")
    akm.clear_keys_off_cache()
    assert _by(key3).get("/v1/api/me").status_code == 401
    task_store.set_platform_setting(akm.KEYS_OFF_SETTING, "0")
    akm.clear_keys_off_cache()
    assert _by(key3).get("/v1/api/me").status_code == 200


def test_a_key_minted_before_2_0_keeps_its_triggers_and_gains_no_public_route(world, monkeypatch):
    """Such a key has no end date and a password change does not end it: it
    fires the person's webhooks as before, and reaches nothing new."""
    from auth import api_keys, rate_limiter
    raw, prefix = akm._generate_raw_key()
    api_key_store.create_user_api_key(user_sub=ME, name="old", key_hash=akm._hash_key(raw),
                                      prefix=prefix, permissions=["triggers"])
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_changed_at=%s, token_epoch_at=%s WHERE sub=%s",
                     (later, later, ME))
    row = api_key_store.get_user_keys_by_prefix(prefix)[0]
    assert akm.is_pre_2_0(row) and akm.key_row_refusal(row, "user", task_store.get_user(ME)) == ""
    failures: list[str] = []
    monkeypatch.setattr(rate_limiter, "record_attempt",
                        lambda bucket, key, tag="": failures.append(bucket))
    # The four routes any scope reaches, and one that needs a scope.
    for path in ("/v1/api/me", "/v1/api/agents", "/v1/api/openapi.json", "/v1/api/otodock.py",
                 "/v1/api/chats"):
        r = _by(raw).get(path)
        assert r.status_code == 403 and r.json()["detail"] == api_keys.TRIGGERS_ONLY, (path, r.text)
    # A real key, used where it does not belong, is no failed guess.
    assert failures == []


def test_junk_sent_under_a_keys_prefix_never_charges_the_key(limits):
    """A key's calls are counted under its id once it is checked, never
    under a prefix the caller picks: every key list shows the prefix."""
    key = _key()
    assert _by(key).get("/v1/api/me").status_code == 200
    junk = key[:17] + ("x" if key[17] != "x" else "y") * (len(key) - 17)
    assert junk[:17] == key[:17] and junk != key
    for _ in range(2):
        r = _by(junk).get("/v1/api/me")
        assert r.status_code == 401 and r.json()["detail"] == "This key is not valid.", r.text
    # The key's own second and third call, then its fourth.
    assert _by(key).get("/v1/api/me").status_code == 200
    assert _by(key).get("/v1/api/me").status_code == 200
    r = _by(key).get("/v1/api/me")
    assert r.status_code == 429 and "too many requests" in r.json()["detail"]
    assert int(r.headers["retry-after"]) > 0


def test_only_an_unknown_key_counts_against_its_address(limits):
    """A key that matched but ended is no failed guess, and a key with a
    live verdict never waits behind its address's failures."""
    good = _key()
    ended = _key(OTHER)
    with get_conn() as conn:
        conn.execute("UPDATE user_api_keys SET expires_at=%s WHERE user_sub=%s",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), OTHER))
    # Under the key's own cap of three: a forgotten script with an expired key.
    for _ in range(3):
        r = _by(ended).get("/v1/api/me")
        assert r.status_code == 401 and r.json()["detail"] == "This key has expired.", r.text
    # A key not seen before, from the same address, is still checked.
    assert _by(good).get("/v1/api/me").status_code == 200
    for n in range(3):
        assert _by("otok_" + f"{n}" * 43).get("/v1/api/me").status_code == 401
    r = _by("otok_" + "z" * 43).get("/v1/api/me")
    assert r.status_code == 429 and "failed keys" in r.json()["detail"], r.text
    # The address is blocked for a key nobody checked yet, never for one that was.
    assert _by(_key(ADMIN)).get("/v1/api/me").status_code == 429
    assert _by(good).get("/v1/api/me").status_code == 200


def test_a_write_to_the_verified_cache_is_never_split_by_a_drop(monkeypatch):
    """Worker threads read and write the cache and the loop drops it: a
    drop that lands inside a write used to raise, and the call answered 500."""
    import threading
    from collections import OrderedDict

    from auth import api_keys

    dropped = threading.Event()

    class _Racing(OrderedDict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            other = threading.Thread(target=lambda: (api_keys.forget(), dropped.set()))
            other.start()
            other.join(0.3)

    monkeypatch.setattr(api_keys, "_verified", _Racing())
    api_keys._remember("digest", {"id": "k1", "key_hash": "h"})
    assert dropped.wait(2) and not api_keys._verified


def test_the_key_is_never_a_person_at_the_keyboard():
    from fastapi import HTTPException

    from api.public.common import person_of
    from auth.providers import (
        PrincipalKind,
        UserContext,
        require_admin,
        require_creator,
        require_creator_interactive,
        require_human,
    )
    u = UserContext(sub=ADMIN, email="a@t", name="A", role=roles.ADMIN, is_api_key=True,
                    api_key_id="k1")
    copy = person_of(u)
    assert u.kind is PrincipalKind.API_KEY and copy.kind is PrincipalKind.API_KEY
    assert not copy.is_session and copy.acting_sub == ADMIN
    for gate in (require_admin, require_human, require_creator, require_creator_interactive):
        for who in (u, copy):
            with pytest.raises(HTTPException) as e:
                gate(who)
            assert e.value.status_code == 403


# ── reach ──────────────────────────────────────────────────────────────────

def _chat(owner: str, agent: str = A, **fields) -> str:
    import uuid
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, owner, agent, fields.pop("permission_mode", "ask"))
    if fields:
        task_store.update_chat(cid, **fields)
    return cid


def test_chats_reach_the_persons_own_on_the_keys_agents(world):
    mine, theirs = _chat(ME), _chat(OTHER)
    on_b = _chat(ME, B)
    key = _key(agents=(A,))
    listed = {c["id"] for c in _by(key).get("/v1/api/chats").json()["chats"]}
    assert mine in listed and theirs not in listed and on_b not in listed
    assert _by(key).get(f"/v1/api/chats/{mine}").status_code == 200
    assert _by(key).get(f"/v1/api/chats/{theirs}").status_code == 404
    assert _by(key).get(f"/v1/api/chats/{on_b}").status_code == 404
    assert _by(key).get("/v1/api/chats", params={"agent": B}).status_code == 403
    # An admin's key gets no pass on a colleague's chat.
    admin_key = _key(ADMIN)
    assert _by(admin_key).get(f"/v1/api/chats/{mine}").status_code == 404
    # A task's chat is never a key's.
    task_store.create_chat("task-run-x", ME, A, source_type=session_kind.TASK.source_type)
    assert _by(key).get("/v1/api/chats/task-run-x").status_code == 404


def test_a_new_chat_takes_the_agent_and_the_keys_mode_rule(world):
    key = _key(agents=(A,))
    r = _by(key).post("/v1/api/chats", json={"agent": A})
    assert r.status_code == 200, r.text
    chat = task_store.get_chat(r.json()["id"])
    assert chat["user_sub"] == ME and chat["permission_mode"] == "ask"
    assert _by(key).post("/v1/api/chats", json={"agent": B}).status_code == 403
    for word in ("dontAsk", "autoReview"):
        r = _by(key).post("/v1/api/chats", json={"agent": A, "mode": word})
        assert r.status_code == 403 and "Ask or Plan" in r.json()["detail"], (word, r.text)
    r = _by(key).post("/v1/api/chats", json={"agent": A, "mode": "plan"})
    assert r.status_code == 200 and task_store.get_chat(r.json()["id"])["permission_mode"] == "plan"
    unasked = _key(agents=(A,), unasked=True)
    r = _by(unasked).post("/v1/api/chats", json={"agent": A, "mode": "autoReview"})
    assert r.status_code == 200, r.text
    created, total = api_key_store.key_created(api_key_store.get_user_keys_by_prefix(
        key[5:17])[0]["id"])
    assert total == 2 and {c["kind"] for c in created} == {"chat"}


def test_a_new_chat_needs_an_engine_the_person_can_use(world, monkeypatch):
    from services.engines import engine_resolution
    monkeypatch.setattr(engine_resolution, "resolve_session_engine",
                        lambda agent, sub: types.SimpleNamespace(execution_path="", model=""))
    r = _by(_key(agents=(A,))).post("/v1/api/chats", json={"agent": A})
    assert r.status_code == 409 and "no AI engine" in r.json()["detail"]
    assert task_store.list_chats(ME, agent=A) == []


# ── send and wait ──────────────────────────────────────────────────────────

class _Ladder:
    """``deliver_prompt`` as the ladder runs it: the row first, then the
    turn's answer row, through the arguments it was given. It answers the
    session id it was given, as the real ladder does: a chat with no session
    starts one under a new id (``fresh``) that only the chat's row names, as
    ``wake_fresh.run_echo`` writes it."""

    def __init__(self, path: str = "oneshot", answer: str = "Hello from the agent.", fresh: str = "sid-x"):
        self.calls: list[dict] = []
        self.path, self.answer, self.fresh = path, answer, fresh

    async def __call__(self, chat_id, text, **kw):
        from core.session.session_delivery import DeliveryOutcome
        self.calls.append({"chat_id": chat_id, "text": text, **kw})
        from core.events import chat_writer
        kw["persist_event"](chat_id)
        if self.path != "none":
            # The pump's rows ride the chat's lane, after the person's row.
            await chat_writer.submit(chat_id, functools.partial(
                task_store.add_chat_message, chat_id, "assistant", self.answer))
        given = kw.get("session_id") or ""
        if self.path != "none" and not given:
            await asyncio.to_thread(task_store.update_chat, chat_id, session_id=self.fresh)
        outcome = DeliveryOutcome(self.path, chat_id=chat_id, session_id=given)
        # As the ladder ends every rung: the caller's hook gets the outcome.
        if kw.get("on_outcome") is not None:
            await kw["on_outcome"](outcome)
        return outcome


@pytest.fixture
def ladder(monkeypatch):
    from core.session import session_delivery
    fake = _Ladder()
    monkeypatch.setattr(session_delivery, "deliver_prompt", fake)
    return fake


def test_send_and_wait_runs_the_persons_turn_in_the_chats_mode(world, ladder):
    from services.scheduler import delivery
    cid = _chat(ME)
    key = _key(agents=(A,))
    with _by(key) as c:
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "done" and body["answer"] == "Hello from the agent."
        again = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert again.json()["message_id"] == body["message_id"]
        read = c.get(f"/v1/api/chats/{cid}/messages/{body['message_id']}")
        assert read.json()["state"] == "done"
    assert len(ladder.calls) == 1
    call = ladder.calls[0]
    assert call["allow_ws"] is False and call["allow_pty"] is False
    assert call["allow_live_turn"] is False
    assert call["user_sub"] == ME and call["role"] == roles.VIEWER
    oneshot = call["oneshot_fn"]
    assert isinstance(oneshot, functools.partial)
    # The rung, told what started the turn (a key's turn keeps a limit's card).
    assert call["persistent_fn"].func is delivery._deliver_via_persistent
    assert call["persistent_fn"].keywords == {"driven_by": "api_key"}
    assert oneshot.keywords["permission_mode"] == "ask"
    assert oneshot.keywords["client_type"] == session_kind.DASHBOARD.name
    # The person's own chat: the session their own warm-up builds.
    assert oneshot.keywords["build"] == "chat"
    row = next(m for m in task_store.get_chat_messages(cid) if m["role"] == "user")
    assert row["author_sub"] == ME and row["content"] == "hi"
    assert json.loads(row["event_data"])["api_key"]["name"] == "walk"


def test_a_keys_turn_builds_the_chat_as_its_persons_warm_up_in_every_chat_it_reaches(world, ladder):
    """A key's message is its person's typed turn: in every chat a key
    reaches it starts the session the person's own warm-up builds, never the
    delivery's builder for a turn nobody typed. A shared chat left after its
    agent quit Shared-only and a person's own delegated chat took that
    builder before."""
    from core.session.visibility import shared_chat_owner
    _assign(ME, A, roles.EDITOR)
    left_over = _chat(shared_chat_owner(A))
    import uuid
    delegated = str(uuid.uuid4())
    task_store.create_chat(delegated, ME, A, "ask", origin="delegated")
    key = _key(agents=(A,))
    for i, cid in enumerate((left_over, delegated)):
        with _by(key) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": f"r-{i}"})
            assert r.status_code == 200, r.text
    builds = [call["oneshot_fn"].keywords["build"] for call in ladder.calls]
    assert builds == ["chat", "chat"]
    assert {call["user_sub"] for call in ladder.calls} == {ME}
    assert {call["role"] for call in ladder.calls} == {roles.EDITOR}


def test_the_turns_log_line_names_the_role_in_force(world, ladder, caplog):
    """The rung that starts the session reads the person's row on the agent
    again, so the session may run below the role read at send: the line
    names the session's own role, and says so when the session is gone."""
    import logging

    from auth.path_policy import SecurityContext
    from core.session import session_state
    cid = _chat(ME)
    ended = "ended via oneshot"
    try:
        # The ladder's fake starts the session "sid-x", named by the chat's row.
        session_state.register_session_state("sid-x", "ask", SecurityContext(
            role=roles.CONTRIBUTOR, username="viewer", agent=A, is_admin_agent=False))
        with caplog.at_level(logging.INFO, logger="otodock.api-keys"), _by(_key()) as c:
            assert c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"}).status_code == 200
            line = next(r.getMessage() for r in caplog.records if ended in r.getMessage())
            assert f"role {roles.CONTRIBUTOR}," in line and roles.VIEWER not in line, line
            caplog.clear()
            session_state.cleanup_session_permission_state("sid-x")
            assert c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"}).status_code == 200
            line = next(r.getMessage() for r in caplog.records if ended in r.getMessage())
            assert f"role {roles.VIEWER} at send," in line, line
    finally:
        session_state.cleanup_session_permission_state("sid-x")


def test_a_turn_nobody_took_takes_its_row_back(world, monkeypatch):
    from core.session import session_delivery
    monkeypatch.setattr(session_delivery, "deliver_prompt", _Ladder(path="none"))
    cid = _chat(ME)
    with _by(_key()) as c:
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi"})
    assert r.json()["state"] == "not_sent"
    assert [m for m in task_store.get_chat_messages(cid) if m["role"] == "user"] == []


def test_a_message_while_the_platform_is_updating_is_told_to_come_again(world, ladder,
                                                                        monkeypatch):
    """No turn starts while new work is held for an update: the request gets
    503 with ``Retry-After`` and writes nothing. One sent before the hold is
    still answered, and the same request goes through once the platform is
    back."""
    from core import drain
    cid = _chat(ME)
    with _by(_key(agents=(A,))) as c:
        sent = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert sent.status_code == 200
        monkeypatch.setattr(drain, "_state", drain.DRAINING)
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "more", "request_id": "r2"})
        assert r.status_code == 503 and r.json()["detail"] == key_turns.UPDATING
        assert r.headers["retry-after"] == str(key_turns.UPDATING_RETRY_S)
        again = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert again.status_code == 200 and again.json()["message_id"] == sent.json()["message_id"]
        assert len(ladder.calls) == 1
        monkeypatch.setattr(drain, "_state", drain.RUNNING)
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "more", "request_id": "r2"})
        assert r.status_code == 200 and r.json()["state"] == "done"
    assert len(ladder.calls) == 2


def test_a_message_with_no_compute_machine_is_told_to_come_again(world, ladder, monkeypatch):
    """The platform runs no turn itself: while no compute machine can take
    the chat's session the request gets 503 with ``Retry-After`` and the
    sentence every door shows, before any row is written or the request is
    claimed, so the same request goes through once a machine serves."""
    from core import placement
    cid = _chat(ME)
    with _by(_key(agents=(A,))) as c:
        serve_none(monkeypatch)
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert r.status_code == 503 and r.json()["detail"] == placement.NO_COMPUTE_SENTENCE, r.text
        assert r.headers["retry-after"] == str(key_turns.UPDATING_RETRY_S)
        assert ladder.calls == []
        assert [m for m in task_store.get_chat_messages(cid) if m["role"] == "user"] == []
        # In the first minute after a start the compute machine has not
        # signed in yet: the caller reads that OtoDock is starting, never
        # that its compute machine is not connected.
        from core import drain
        drain.hold_for_start()
        try:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
            assert r.status_code == 503 and r.json()["detail"] == drain.STARTING_LINE, r.text
            assert r.headers["retry-after"] == str(key_turns.UPDATING_RETRY_S)
        finally:
            drain.drop_start_hold()
        assert ladder.calls == []
        serve(monkeypatch)
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert r.status_code == 200 and r.json()["state"] == "done", r.text
    assert len(ladder.calls) == 1
    assert [m["content"] for m in task_store.get_chat_messages(cid) if m["role"] == "user"] == ["hi"]


def test_a_busy_chat_a_dont_ask_chat_and_a_terminal_chat_are_refused(world, ladder, monkeypatch):
    from core.events import stream_pump

    class _Live:
        is_done = False

    busy = _chat(ME)
    monkeypatch.setitem(stream_pump._active_pumps, busy, _Live())
    key = _key()
    with _by(key) as c:
        assert c.post(f"/v1/api/chats/{busy}/messages", json={"text": "x"}).status_code == 409
        unasked = _chat(ME, permission_mode="dontAsk")
        r = c.post(f"/v1/api/chats/{unasked}/messages", json={"text": "x"})
        assert r.status_code == 403 and "without asking" in r.json()["detail"]
        term = _chat(ME, execution_mode="interactive")
        assert c.post(f"/v1/api/chats/{term}/messages", json={"text": "x"}).status_code == 409
    assert ladder.calls == []
    with _by(_key(unasked=True)) as c:
        r = c.post(f"/v1/api/chats/{unasked}/messages", json={"text": "x"})
        assert r.status_code == 200 and r.json()["state"] == "done"


@pytest.mark.asyncio
async def test_a_repeated_request_never_waits_on_a_send_that_was_dropped(world, ladder, monkeypatch):
    """A second request with the same id waits for the first one's row. A
    first send dropped before its row used to leave it waiting for ever,
    holding one of the key's eight waits."""
    import asyncio
    import threading

    from auth import api_keys
    cid = _chat(ME)
    u = await api_keys.verify(_key(), "chat")
    chat = task_store.get_chat(cid)
    release = threading.Event()
    real, calls = api_key_store.claim_key_request, []

    def _claim(key_id, request_id, chat_id):
        calls.append(request_id)
        if len(calls) == 1:
            release.wait(5)
            raise RuntimeError("the database went away")
        return real(key_id, request_id, chat_id)

    monkeypatch.setattr(api_key_store, "claim_key_request", _claim)
    first = asyncio.create_task(key_turns.send(u, chat, "x", request_id="r1", wait_s=5))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(key_turns.send(u, chat, "x", request_id="r1", wait_s=5))
    await asyncio.sleep(0.2)
    release.set()
    with pytest.raises(RuntimeError):
        await first
    answer = await asyncio.wait_for(second, 5)
    assert answer["state"] == "done" and len(ladder.calls) == 1


def test_a_claim_a_dead_process_left_is_taken_over(world, ladder):
    """A restart between the claim and the row left the request id refused
    for a week. A claim with no message that nobody here holds is replaced
    once it is two minutes old."""
    cid = _chat(ME)
    key = _key()
    key_id = api_key_store.get_user_keys_by_prefix(key[5:17])[0]["id"]

    def _left_behind(request_id: str, age_s: int) -> None:
        with get_conn() as conn:
            conn.execute("INSERT INTO user_api_key_requests (key_id, request_id, chat_id, created_at) "
                         "VALUES (%s, %s, %s, %s)",
                         (key_id, request_id, cid,
                          (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()))

    _left_behind("fresh", 5)
    _left_behind("stale", 300)
    with _by(key) as c:
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "fresh"})
        assert r.status_code == 409 and "already being sent" in r.json()["detail"]
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "stale"})
        assert r.status_code == 200 and r.json()["state"] == "done", r.text
    assert len(ladder.calls) == 1


def test_a_request_id_answers_only_in_its_own_chat(world, ladder):
    """A reused id used to answer the first chat's turn, whatever chat the
    address named and whether the key still reached the first one."""
    one, two = _chat(ME), _chat(ME)
    with _by(_key()) as c:
        assert c.post(f"/v1/api/chats/{one}/messages",
                      json={"text": "x", "request_id": "r1"}).status_code == 200
        r = c.post(f"/v1/api/chats/{two}/messages", json={"text": "x", "request_id": "r1"})
        assert r.status_code == 409 and r.json()["detail"] == key_turns.OTHER_CHAT, r.text
        # After a restart the claim row answers the same.
        key_turns._turns.clear()
        r = c.post(f"/v1/api/chats/{two}/messages", json={"text": "x", "request_id": "r1"})
        assert r.status_code == 409 and r.json()["detail"] == key_turns.OTHER_CHAT, r.text
    assert len(ladder.calls) == 1


def test_a_rung_that_raised_after_an_answer_leaves_the_question_with_it(world, monkeypatch):
    """The row is taken back only while it is the chat's last row. A rung
    that raised mid-turn left an answer without its question, and a retry
    with the same id ran the turn twice."""
    from core.events import chat_writer
    from core.session import session_delivery
    sends: list[str] = []

    async def _raises_mid_turn(chat_id, text, **kw):
        sends.append(text)
        kw["persist_event"](chat_id)
        await chat_writer.submit(chat_id, functools.partial(
            task_store.add_chat_message, chat_id, "assistant", "Half an answer."))
        raise RuntimeError("the engine went away")

    monkeypatch.setattr(session_delivery, "deliver_prompt", _raises_mid_turn)
    cid = _chat(ME)
    with _by(_key()) as c:
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert r.status_code == 200 and r.json()["state"] == "done", r.text
        assert r.json()["answer"] == "Half an answer."
        again = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "hi", "request_id": "r1"})
        assert again.json()["message_id"] == r.json()["message_id"]
    assert [m["content"] for m in task_store.get_chat_messages(cid)] == ["hi", "Half an answer."]
    assert sends == ["hi"]


def test_a_turn_that_starts_while_the_key_reads_the_session_is_never_cut(world, ladder, monkeypatch):
    """A session that does not run as the sender is closed for one that
    does. The liveness answer before that close takes a moment, and a
    teammate's turn may start in it: the key answers busy and closes nothing."""
    from core.events import stream_pump
    from core.session import session_delivery

    class _Live:
        is_done = False

    pumps: dict = {}
    monkeypatch.setattr(stream_pump, "_active_pumps", pumps)
    closed: list[str] = []
    races: list[bool] = [True]

    class _Layer:
        async def is_session_alive(self, sid):
            if races[0]:
                pumps[cid] = _Live()
            return True

        async def close_session(self, sid):
            closed.append(sid)

    async def _layer(chat):
        return _Layer()

    monkeypatch.setattr(session_delivery, "chat_layer", _layer)
    cid = _chat(ME, session_id="sid-of-someone-else")
    with _by(_key()) as c:
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "r1"})
        assert r.status_code == 409 and r.json()["detail"] == key_turns.BUSY, r.text
        assert closed == [] and ladder.calls == []
        assert [m for m in task_store.get_chat_messages(cid) if m["role"] == "user"] == []
        # Nothing was claimed: the same request id sends once the chat is free.
        pumps.clear()
        races[0] = False
        r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "r1"})
        assert r.status_code == 200 and r.json()["state"] == "done", r.text
    assert closed == ["sid-of-someone-else"] and len(ladder.calls) == 1


def test_a_key_that_asks_never_rides_a_tool_the_person_allowed_before(world, ladder, monkeypatch):
    """One Allow covers its tool for the rest of the session, with no card.
    A key made to ask starts its turn in a session that remembers none: the
    live one is closed first, and the close forgets its allows. A key made
    to run without asking leaves the session alone."""
    from auth.path_policy import SecurityContext
    from core.session import session_delivery, session_manager, session_state
    tool = "mcp__schedules-mcp__create_scheduled_task"
    layer = session_manager.holding_engines()["direct-llm"]
    live: set[str] = set()
    closed: list[str] = []
    real_close = layer.close_session

    async def _alive(sid):
        return sid in live

    async def _close(sid):
        closed.append(sid)
        live.discard(sid)
        await real_close(sid)

    async def _layer(chat):
        return layer

    monkeypatch.setattr(layer, "is_session_alive", _alive)
    monkeypatch.setattr(layer, "close_session", _close)
    monkeypatch.setattr(session_delivery, "chat_layer", _layer)

    def _session_of_me(allowed: bool) -> tuple[str, str]:
        """A chat of the person with a live session that serves them, in
        the chat's mode, as a chat turn."""
        import uuid
        sid = str(uuid.uuid4())
        live.add(sid)
        session_state.register_session_state(sid, "ask", SecurityContext(
            role=roles.VIEWER, username="viewer", agent=A, is_admin_agent=False))
        session_state._record_session_use(sid, client_type=session_kind.DASHBOARD.name, agent=A)
        if allowed:
            session_state.remember_session_tool_allow(sid, tool)
        return _chat(ME, session_id=sid), sid

    remembered, sid = _session_of_me(allowed=True)
    clean, clean_sid = _session_of_me(allowed=False)
    unasked, unasked_sid = _session_of_me(allowed=True)
    try:
        assert session_state.remembers_tool_allows(sid)
        assert not session_state.remembers_tool_allows(clean_sid)
        with _by(_key()) as c:
            r = c.post(f"/v1/api/chats/{remembered}/messages", json={"text": "x"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
            # Recycled, and the turn still ran: on a session with no memory.
            assert closed == [sid]
            assert not session_state.is_session_tool_allowed(sid, tool)
            assert not session_state.remembers_tool_allows(sid)
            # A session that remembers nothing is the key's to use as it is.
            r = c.post(f"/v1/api/chats/{clean}/messages", json={"text": "x"})
            assert r.status_code == 200 and closed == [sid]
        with _by(_key(unasked=True)) as c:
            r = c.post(f"/v1/api/chats/{unasked}/messages", json={"text": "x"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
        assert closed == [sid]
        assert session_state.is_session_tool_allowed(unasked_sid, tool)
        assert [call["chat_id"] for call in ladder.calls] == [remembered, clean, unasked]
    finally:
        for each in (sid, clean_sid, unasked_sid):
            session_state.cleanup_session_permission_state(each)
            session_state._sessions.pop(each, None)


def test_an_allow_given_inside_an_asking_keys_turn_is_never_remembered(world, monkeypatch):
    """The person answers a card inside the turn of a key that asks: that
    Allow covers the one call. The hook is asked at the turn's last tool
    call and after its end, so a change to how long the turn holds its chat
    shows here. A key made to run without asking leaves the memory alone."""
    import asyncio

    from api.hooks import permission
    from api.hooks.permission import HookPermissionRequest, hook_permission
    from api.public import key_turns
    from auth.path_policy import SecurityContext
    from core.session import session_delivery, session_state
    tool = "mcp__schedules-mcp__create_scheduled_task"
    sid = "sid-asking-turn"
    cid = _chat(ME, session_id=sid)
    other = _chat(ME)
    session_state.register_session_state(sid, "ask", SecurityContext(
        role=roles.VIEWER, username="viewer", agent=A, is_admin_agent=False))
    session_state._record_session_use(sid, client_type=session_kind.DASHBOARD.name, agent=A)

    async def _no_match(authorization, session_id):
        return None
    monkeypatch.setattr(permission, "verify_session_match_async", _no_match)
    # The card goes to the session's queue, as with a turn's pump reading it.
    from core.session import engine_prompts
    monkeypatch.setattr(engine_prompts, "queue_has_reader", lambda queue: True)

    async def _allowed_on_a_card() -> bool:
        """One call of the tool, the card answered Allow; whether the
        session remembers it after."""
        task = asyncio.create_task(hook_permission(
            HookPermissionRequest(session_id=sid, tool_name=tool, tool_input={}), authorization=None))
        prompt = await asyncio.wait_for(session_state.get_permission_queue(sid).get(), timeout=5)
        session_state.resolve_permission(prompt["request_id"], True)
        assert (await asyncio.wait_for(task, timeout=5))["decision"] == "allow"
        return session_state.is_session_tool_allowed(sid, tool)

    seen: dict = {}

    class _TurnWithACard(_Ladder):
        async def __call__(self, chat_id, text, **kw):
            seen["feeds"] = (key_turns.feeds_allow_memory(chat_id), key_turns.feeds_allow_memory(""),
                             key_turns.feeds_allow_memory(other))
            seen["remembered"] = await _allowed_on_a_card()
            return await super().__call__(chat_id, text, **kw)

    monkeypatch.setattr(session_delivery, "deliver_prompt", _TurnWithACard())
    try:
        with _by(_key()) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
        assert seen == {"feeds": (False, False, True), "remembered": False}
        # The turn ended: the chat is the person's again, and so is the memory.
        assert key_turns.feeds_allow_memory(cid) and key_turns.feeds_allow_memory("")
        assert not session_state.remembers_tool_allows(sid)
        # (the next client runs its own loop: the session's queue is made again)
        session_state._permission_emitters.pop(sid, None)
        with _by(_key(unasked=True)) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
        assert seen == {"feeds": (True, True, True), "remembered": True}
    finally:
        session_state.cleanup_session_permission_state(sid)
        session_state._sessions.pop(sid, None)
        session_state._permission_emitters.pop(sid, None)


def test_a_key_that_asks_sends_only_into_an_ask_or_a_plan_chat(world, ladder):
    """Auto runs standard tools with no card, and a chat with no stored word
    reads as Auto: a key made to ask reaches neither."""
    asks, plans = _chat(ME), _chat(ME, permission_mode="plan")
    refused = [_chat(ME, permission_mode=word) for word in ("autoReview", "dontAsk", "")]
    with _by(_key()) as c:
        for cid in (asks, plans):
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
        for cid in refused:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"})
            assert r.status_code == 403 and r.json()["detail"] == key_turns.UNASKED_CHAT, r.text
    assert [call["chat_id"] for call in ladder.calls] == [asks, plans]
    with _by(_key(unasked=True)) as c:
        for cid in refused:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text


def test_a_judge_chat_and_a_chat_another_key_holds_take_no_message(world, ladder, monkeypatch):
    judge, held = _chat(ME, permission_mode="judge"), _chat(ME)
    monkeypatch.setattr(key_turns, "_held_chats", {held})
    with _by(_key(unasked=True)) as c:
        r = c.post(f"/v1/api/chats/{judge}/messages", json={"text": "x"})
        assert r.status_code == 409 and "cannot take a message" in r.json()["detail"]
        r = c.post(f"/v1/api/chats/{held}/messages", json={"text": "x"})
        assert r.status_code == 409 and r.json()["detail"] == key_turns.BUSY
    assert ladder.calls == []


@pytest.mark.asyncio
async def test_the_ladder_never_folds_a_keys_message_into_a_running_turn(world, monkeypatch):
    """``allow_live_turn=False``: a turn streaming on the chat is neither
    steered nor queued on, and no second producer starts beside it."""
    from core.events import stream_pump
    from core.session import session_delivery, session_state
    cid = _chat(ME)
    steered: list[str] = []
    queued: list[str] = []
    started: list[str] = []

    class _Pump:
        is_done = False
        session_id = "sid-live"
        source_type = session_kind.DASHBOARD.source_type

    class _Layer:
        async def steer(self, sid, text):
            steered.append(text)
            return True

    async def _layer(chat):
        return _Layer()

    async def _rung(*a, **kw):
        started.append("rung")
        return ""

    monkeypatch.setattr(stream_pump, "_active_pumps", {cid: _Pump()})
    monkeypatch.setattr(session_delivery, "chat_layer", _layer)
    monkeypatch.setattr(session_state, "queue_pump_prompt",
                        lambda chat_id, text, **kw: queued.append(text) or True)
    send = functools.partial(session_delivery.deliver_prompt, cid, "x", source="api_key",
                             agent=A, user_sub=ME, role=roles.VIEWER, allow_ws=False,
                             allow_pty=False, persistent_fn=_rung, oneshot_fn=_rung)
    outcome = await send(allow_live_turn=False)
    assert outcome.path == "none" and steered == [] and queued == [] and started == []
    # The ladder's own default does steer: the flag is what keeps a key out.
    outcome = await send()
    assert outcome.path != "none" and len(steered) == 1 and started == []


def test_the_row_being_delivered_is_held_out_of_a_history_rebuild():
    from core.session import key_rows

    def _row(request_id: str) -> dict:
        return {"role": "user", "event_data": json.dumps({"api_key": {"request_id": request_id}})}

    key_rows.hold("c1", "r1")
    try:
        assert key_rows.held_row("c1", _row("r1"))
        assert not key_rows.held_row("c1", _row("r0"))
        assert not key_rows.held_row("c1", {"role": "user", "event_data": ""})
        assert not key_rows.held_row("c1", {**_row("r1"), "role": "assistant"})
        assert not key_rows.held_row("c2", _row("r1"))
        # Another request's release leaves this one held.
        key_rows.release("c1", "r0")
        assert key_rows.held_row("c1", _row("r1"))
    finally:
        key_rows.release("c1", "r1")
    assert not key_rows.held_row("c1", _row("r1"))


def test_a_shared_pool_below_the_editor_tier_is_refused(world, ladder):
    agent_store.update_agent(A, collaborative=False, default_scope="agent")
    from core.session.visibility import shared_chat_owner
    pool = _chat(shared_chat_owner(A))
    with _by(_key()) as c:
        r = c.post(f"/v1/api/chats/{pool}/messages", json={"text": "x"})
    assert r.status_code == 409
    assert ladder.calls == []


def _worker_chat(agent: str = A, *, by: str = ADMIN, kind: str = "delegate",
                 scope: str = "agent", session_id: str = "") -> str:
    """A chat-surface worker of a delegation: a chat of the agent's own
    (the ``agent::`` owner, the dashboard kind) with a run row, in the
    agent scope unless named."""
    import uuid

    from core.session.visibility import shared_chat_owner
    cid = _chat(shared_chat_owner(agent), agent, permission_mode="auto")
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    task_store.create_run(run_id, "dyn-x", agent, kind, None, "go", kind, scope, by)
    task_store.update_run(run_id, status="completed", chat_id=cid, session_id=session_id or None)
    if session_id:
        task_store.update_chat(cid, session_id=session_id)
    return cid


def test_an_editors_key_never_drives_a_managers_chat_worker_that_writes_knowledge(
        world, ladder, monkeypatch):
    """A chat-surface worker a manager delegated runs as the agent, in a
    chat of the agent's own that an editor may open and read. While its run
    holds the write on the agent's knowledge the editor's key sends nothing
    into it and its live session stays as it is: the rule the dashboard's
    socket and the delegation's own door apply. The manager's key drives it,
    and a pool chat with no run row keeps the editor tier alone."""
    from core.session import session_delivery
    from core.session.visibility import KNOWLEDGE_RUN_REFUSED, shared_chat_owner
    closed: list[str] = []

    class _Layer:
        async def is_session_alive(self, sid):
            return True

        async def close_session(self, sid):
            closed.append(sid)

    async def _layer(chat):
        return _Layer()

    monkeypatch.setattr(session_delivery, "chat_layer", _layer)
    _assign(ME, A, roles.EDITOR)
    _assign(OTHER, A, roles.MANAGER)
    worker = _worker_chat(session_id="sid-worker")
    pool = _chat(shared_chat_owner(A), A, permission_mode="auto")
    with _by(_key(unasked=True)) as c:
        # The editor may read the worker's chat by its id: reading acts on
        # no session. The key's list names the person's own chats only.
        assert c.get(f"/v1/api/chats/{worker}").status_code == 200
        assert worker not in [row["id"] for row in c.get("/v1/api/chats").json()["chats"]]
        r = c.post(f"/v1/api/chats/{worker}/messages", json={"text": "do this", "request_id": "r1"})
        assert r.status_code == 409 and r.json()["detail"] == KNOWLEDGE_RUN_REFUSED, r.text
        assert closed == [] and ladder.calls == []
        assert [m for m in task_store.get_chat_messages(worker) if m["role"] == "user"] == []
        # Nothing was claimed for the refused request.
        assert c.get(f"/v1/api/chats/{worker}/messages/1").status_code == 404
        ok = c.post(f"/v1/api/chats/{pool}/messages", json={"text": "hi", "request_id": "r2"})
        assert ok.status_code == 200 and ok.json()["state"] == "done", ok.text
    with _by(_key(OTHER, unasked=True)) as c:
        r = c.post(f"/v1/api/chats/{worker}/messages", json={"text": "go on", "request_id": "r3"})
        assert r.status_code == 200 and r.json()["state"] == "done", r.text
    assert [call["chat_id"] for call in ladder.calls] == [pool, worker]
    assert ladder.calls[1]["user_sub"] == OTHER and ladder.calls[1]["role"] == roles.MANAGER


def test_a_worker_whose_run_holds_no_knowledge_write_is_an_editors_to_drive_by_key(
        world, ladder):
    """The run's gate is the one the dashboard asks: an agent-scope worker a
    person below the manager tier delegated holds no write on the agent's
    knowledge, so an editor's key drives it as their socket does."""
    _assign(ME, A, roles.EDITOR)
    _assign(OTHER, A, roles.EDITOR)
    worker = _worker_chat(by=OTHER)
    with _by(_key(unasked=True)) as c:
        r = c.post(f"/v1/api/chats/{worker}/messages", json={"text": "go on"})
    assert r.status_code == 200 and r.json()["state"] == "done", r.text
    assert len(ladder.calls) == 1


def test_the_doors_to_a_workers_chat_answer_as_one(world):
    """The dashboard's socket, the delegation's ``continue_id``, a key's
    turn, the voice door and the move ask one rule of a person on a
    delegation's worker chat (``visibility.drive_refusal``): the same
    sentence for the same person and run, or none, and the move's yes or no
    with it."""
    import asyncio
    import uuid

    from auth.providers import UserContext
    from core.session import visibility as _vis
    from services.media import duplex_service
    from services.remote import chat_move
    from tests.session.test_ws_dashboard_resume_controls import _bare_connection
    _assign(ME, A, roles.EDITOR)
    _assign(OTHER, A, roles.MANAGER)
    writes = _worker_chat()                      # a manager's: its run writes the knowledge
    plain = _worker_chat(by=ME)                  # an editor's own agent-scope worker
    theirs = _worker_chat(by=OTHER, scope="user")   # a colleague's, in the user scope
    no_run = _chat(_vis.shared_chat_owner(A), A)

    def doors(sub: str, role: str, cid: str) -> set[str]:
        chat = task_store.get_chat(cid)
        u = UserContext(sub=sub, email="x@key.test", name="x", role=roles.MEMBER,
                        agents=[A], agent_roles={A: role}, is_api_key=True,
                        api_key_id=uuid.uuid4().hex)
        conn = _bare_connection(cid, None, user_sub=sub, agent=A)
        voice = duplex_service.chat_access_denied_reason(cid, u) or ""
        moved = chat_move.may_drive(chat, chat_move.Asker(sub, role, False))
        answers = {key_turns.drive_refusal(u, chat), _vis.drive_refusal_for(u, chat),
                   asyncio.run(conn._deny_task_continue(cid, quiet=True)), voice}
        assert moved is (answers == {""}), (cid, answers)
        return answers

    assert doors(ME, roles.EDITOR, writes) == {_vis.KNOWLEDGE_RUN_REFUSED}
    assert doors(OTHER, roles.MANAGER, writes) == {""}
    assert doors(ME, roles.EDITOR, plain) == {""}
    assert doors(ME, roles.EDITOR, theirs) == {_vis.TASK_CONTINUE_REFUSED}
    assert doors(OTHER, roles.MANAGER, theirs) == {""}
    assert doors(ME, roles.EDITOR, no_run) == {""}


# ── tasks, files, notifications ────────────────────────────────────────────

def test_tasks_the_key_creates_and_runs(world, monkeypatch):
    from services.scheduler import scheduler
    runs: list[str] = []

    async def _now(task, **kw):
        runs.append(task.id)
        return "run-1"

    monkeypatch.setattr(scheduler, "trigger_task_now", _now)
    key = _key(agents=(A,), unasked=True)
    with _by(key) as c:
        r = c.post("/v1/api/tasks", json={"agent": A, "name": "n", "prompt": "p"})
        assert r.status_code == 200, r.text
        assert r.json()["run_id"] == "run-1"
        task_id = r.json()["task_id"]
        assert [t["id"] for t in c.get("/v1/api/tasks").json()["tasks"]] == [task_id]
        assert c.post(f"/v1/api/tasks/{task_id}/run").json()["run_id"] == "run-1"
        assert c.post("/v1/api/tasks", json={"agent": A, "name": "n", "prompt": "p",
                                             "scope": "agent"}).status_code == 403
    row = task_store.get_dynamic_task(task_id)
    assert row["created_by"] == ME and row["scope"] == "user" and row["task_type"] == "trigger"
    # A colleague's task and a continuation.
    other = _key(OTHER, agents=(A,), unasked=True)
    assert _by(other).post(f"/v1/api/tasks/{task_id}/run").status_code == 404
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET task_type='continuation' WHERE id=%s", (task_id,))
    assert _by(key).post(f"/v1/api/tasks/{task_id}/run").status_code == 409
    assert runs == [task_id, task_id]


def test_a_key_that_asks_reaches_no_task_route(world, monkeypatch):
    """A task's run has nobody to ask. The mint refuses the pair, and the
    routes check a stored row again."""
    from api.public import tasks as public_tasks
    from services.scheduler import scheduler
    runs: list[str] = []

    async def _now(task, **kw):
        runs.append(task.id)
        return "run-1"

    monkeypatch.setattr(scheduler, "trigger_task_now", _now)
    with _by(_key(agents=(A,), unasked=True)) as c:
        task_id = c.post("/v1/api/tasks", json={"agent": A, "name": "n", "prompt": "p",
                                                "run_now": False}).json()["task_id"]
    # Auto is no way round it: the task's own mode changes nothing.
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET permission_mode='autoReview' WHERE id=%s", (task_id,))
    with _by(_stored_key()) as c:
        for r in (c.post("/v1/api/tasks", json={"agent": A, "name": "n", "prompt": "p"}),
                  c.post(f"/v1/api/tasks/{task_id}/run"), c.get("/v1/api/tasks"),
                  c.get("/v1/api/runs/run-1"), c.get("/v1/api/runs/run-1/stream")):
            assert r.status_code == 403 and r.json()["detail"] == public_tasks.NOBODY_TO_ASK, r.text
    assert runs == [] and len(task_store.list_dynamic_tasks(A)) == 1


def test_a_task_whose_run_is_no_session_of_its_own_is_not_a_keys_to_run(world, monkeypatch):
    from services.scheduler import scheduler
    runs: list[str] = []

    async def _now(task, **kw):
        runs.append(task.id)
        return "run-1"

    monkeypatch.setattr(scheduler, "trigger_task_now", _now)
    with _by(_key(agents=(A,), unasked=True)) as c:
        task_id = c.post("/v1/api/tasks", json={"agent": A, "name": "n", "prompt": "p",
                                                "run_now": False}).json()["task_id"]
        for column, value, reset in (("use_persistent", True, False), ("on_complete_agent", B, None),
                                     ("on_complete_session_id", "sid", None),
                                     ("target_chat_id", "c1", None)):
            with get_conn() as conn:
                conn.execute(f"UPDATE dynamic_tasks SET {column}=%s WHERE id=%s", (value, task_id))
            r = c.post(f"/v1/api/tasks/{task_id}/run")
            assert r.status_code == 409 and "cannot run this kind" in r.json()["detail"], column
            assert c.get("/v1/api/tasks").json()["tasks"] == [], column
            with get_conn() as conn:
                conn.execute(f"UPDATE dynamic_tasks SET {column}=%s WHERE id=%s", (reset, task_id))
        assert c.post(f"/v1/api/tasks/{task_id}/run").status_code == 200
    assert runs == [task_id]


def test_an_agents_task_is_a_managers_to_run_and_its_makers(world, monkeypatch):
    """The key's run asks what the dashboard's Run now asks: an editor
    fires the agent-scope tasks they made, a manager any. The list is the
    tasks the key may run, so it follows."""
    from api.public import tasks as public_tasks
    from services.scheduler import scheduler
    runs: list[str] = []

    async def _now(task, **kw):
        runs.append(task.id)
        return "run-1"

    monkeypatch.setattr(scheduler, "trigger_task_now", _now)
    _assign(ME, A, roles.EDITOR)
    _assign(OTHER, A, roles.MANAGER)
    new = {"agent": A, "name": "n", "prompt": "p", "scope": "agent", "run_now": False}
    manager, editor = _key(OTHER, agents=(A,), unasked=True), _key(agents=(A,), unasked=True)
    with _by(manager) as c:
        theirs = c.post("/v1/api/tasks", json=new).json()["task_id"]
    with _by(editor) as c:
        mine = c.post("/v1/api/tasks", json=new).json()["task_id"]
        r = c.post(f"/v1/api/tasks/{theirs}/run")
        assert r.status_code == 403 and r.json()["detail"] == public_tasks.MANAGERS_TASK
        assert [t["id"] for t in c.get("/v1/api/tasks").json()["tasks"]] == [mine]
        assert runs == []
        assert c.post(f"/v1/api/tasks/{mine}/run").status_code == 200
    with _by(manager) as c:
        assert c.post(f"/v1/api/tasks/{mine}/run").status_code == 200
        assert c.post(f"/v1/api/tasks/{theirs}/run").status_code == 200
        assert {t["id"] for t in c.get("/v1/api/tasks").json()["tasks"]} == {mine, theirs}
    assert runs == [mine, mine, theirs]
    # The dashboard's own answers for the same two people and the same task.
    assert _cookie(ME).post(f"/v1/tasks/{theirs}/run").status_code == 403
    assert _cookie(OTHER).post(f"/v1/tasks/{theirs}/run").status_code == 200


def test_runs_a_colleague_made_stay_theirs(world):
    with get_conn() as conn:
        conn.execute("INSERT INTO task_runs (id, task_id, agent, trigger_type, status, scope, "
                     "created_by) VALUES ('run-other', 't', %s, 'manual', 'completed', 'user', %s) "
                     "ON CONFLICT DO NOTHING", (A, OTHER))
    assert _by(_key(ADMIN, unasked=True)).get("/v1/api/runs/run-other").status_code == 404
    assert _by(_key(OTHER, unasked=True)).get("/v1/api/runs/run-other").status_code == 200


def test_files_reach_the_own_folder_and_the_workspace(world, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    for rel in ("workspace/w.txt", "knowledge/k.txt", "config/c.txt", "users/viewer/m.txt",
                "users/viewer2/o.txt"):
        (tmp_path / A / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / A / rel).write_text(rel)
    _assign(ME, A, roles.EDITOR)
    key = _key(agents=(A,))
    with _by(key) as c:
        tree = json.dumps(c.get(f"/v1/api/agents/{A}/files").json())
        assert "w.txt" in tree and "k.txt" in tree and "m.txt" in tree
        assert "c.txt" not in tree and "o.txt" not in tree
        assert c.get(f"/v1/api/agents/{A}/files/workspace/w.txt").status_code == 200
        assert c.get(f"/v1/api/agents/{A}/files/config/c.txt").status_code == 403
        assert c.get(f"/v1/api/agents/{A}/files/users/viewer2/o.txt").status_code == 403
        assert c.put(f"/v1/api/agents/{A}/files/knowledge/k.txt",
                     json={"content": "x"}).status_code == 403
        r = c.put(f"/v1/api/agents/{A}/files/workspace/new.txt", json={"content": "x"})
        assert r.status_code == 200, r.text
        # Past the narrow cap of a request with no proven credential.
        big = "y" * (128 * 1024)
        r = c.put(f"/v1/api/agents/{A}/files/workspace/big.txt", json={"content": big})
        assert r.status_code == 200, r.text
    assert (tmp_path / A / "workspace/new.txt").read_text() == "x"
    assert (tmp_path / A / "workspace/big.txt").stat().st_size == 128 * 1024
    admin = _key(ADMIN, agents=(A,))
    assert _by(admin).get(f"/v1/api/agents/{A}/files/users/viewer2/o.txt").status_code == 403
    assert _by(admin).get(f"/v1/api/agents/{A}/files/config/c.txt").status_code == 403


def test_the_file_routes_follow_no_climb_and_no_link_out(world, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    for rel in ("workspace/w.txt", "config/c.txt", "users/viewer2/o.txt"):
        (tmp_path / A / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / A / rel).write_text("kept apart")
    outside = tmp_path / "outside.txt"
    outside.write_text("kept apart")
    workspace = tmp_path / A / "workspace"
    (workspace / "to-config").symlink_to(tmp_path / A / "config")
    (workspace / "to-colleague").symlink_to(tmp_path / A / "users" / "viewer2")
    (workspace / "to-file").symlink_to(outside)
    _assign(ME, A, roles.EDITOR)
    with _by(_key(agents=(A,))) as c:
        assert c.get(f"/v1/api/agents/{A}/files/workspace/w.txt").status_code == 200
        for path in ("workspace/to-config/c.txt", "workspace/to-colleague/o.txt", "workspace/to-file",
                     "workspace/%2e%2e/config/c.txt", "workspace/..%2fconfig/c.txt",
                     "workspace/%2e%2e%2f%2e%2e%2foutside.txt"):
            r = c.get(f"/v1/api/agents/{A}/files/{path}")
            assert r.status_code in (400, 403, 404) and "kept apart" not in r.text, (path, r.text)
        r = c.put(f"/v1/api/agents/{A}/files/workspace/to-config/new.txt", json={"content": "x"})
        assert r.status_code in (400, 403, 404), r.text
    assert not (tmp_path / A / "config" / "new.txt").exists()


def test_a_notification_goes_to_the_keys_person(world):
    key = _key(scopes=("notifications",))
    with _by(key) as c:
        r = c.post("/v1/api/notifications", json={"title": "Built", "body": "ok"})
        assert r.status_code == 200, r.text
        inbox = c.get("/v1/api/notifications").json()["notifications"]
    assert [n["title"] for n in inbox] == ["Built"]
    from storage.automation import notification_store
    assert [d["title"] for d in notification_store.list_deliveries(ME)] == ["Built"]
    assert notification_store.list_deliveries(OTHER) == []


def test_a_key_that_names_its_agents_reads_and_sends_only_their_notifications(world):
    every = _by(_key(scopes=("notifications",)))
    for title, agent in (("plain", ""), ("from a", A), ("from b", B)):
        assert every.post("/v1/api/notifications",
                          json={"title": title, "agent": agent}).status_code == 200
    only_a = _by(_key(scopes=("notifications",), agents=(A,)))
    titles = [n["title"] for n in only_a.get("/v1/api/notifications").json()["notifications"]]
    assert sorted(titles) == ["from a", "plain"]
    r = only_a.post("/v1/api/notifications", json={"title": "t", "agent": B})
    assert r.status_code == 403 and "does not reach" in r.json()["detail"]
    assert len(every.get("/v1/api/notifications").json()["notifications"]) == 3


def test_the_handlers_get_values_never_parameter_objects(world, monkeypatch, ladder):
    """A FastAPI ``Query``/``Header`` default is truthy: every handler a
    public route calls gets each argument as a value."""
    from api.agents import files
    from api.notifications import notifications
    from api.tasks import tasks
    seen: list[str] = []

    def _guard(mod, name):
        real = getattr(mod, name)

        @functools.wraps(real)
        async def wrapped(*a, **kw):
            bad = [k for k, v in kw.items() if isinstance(v, Param)]
            bad += [str(i) for i, v in enumerate(a) if isinstance(v, Param)]
            assert not bad, f"{name} got parameter objects for {bad}"
            seen.append(name)
            return await real(*a, **kw)
        monkeypatch.setattr(mod, name, wrapped)

    for mod, name in ((tasks, "create_one_time_task"), (notifications, "create_notification"),
                      (files, "list_agent_files"), (files, "read_agent_file")):
        _guard(mod, name)
    from services.scheduler import scheduler

    async def _now(task, **kw):
        return "run-1"
    monkeypatch.setattr(scheduler, "trigger_task_now", _now)
    key = _key(agents=(A,), unasked=True)
    with _by(key) as c:
        assert c.post("/v1/api/tasks", json={"agent": A, "name": "n", "prompt": "p"}).status_code == 200
        assert c.post("/v1/api/notifications", json={"title": "t"}).status_code == 200
        c.get(f"/v1/api/agents/{A}/files")
        c.get(f"/v1/api/agents/{A}/files/workspace/none.txt")
    assert set(seen) == {"create_one_time_task", "create_notification", "list_agent_files",
                         "read_agent_file"}


def test_what_a_key_does_not_reach_is_refused_in_a_sentence(world):
    asks, runs = _by(_key()), _by(_key(unasked=True))
    answers = {
        asks.get("/v1/api/chats/none").json()["detail"]: "This key reaches no such chat.",
        runs.post("/v1/api/tasks/none/run").json()["detail"]: "This key reaches no such task.",
        runs.get("/v1/api/runs/none").json()["detail"]: "This key reaches no such run.",
    }
    admin = _cookie(ADMIN, roles.ADMIN)
    for path in ("/v1/admin/api-keys/other/none", "/v1/admin/api-keys/user/none"):
        r = admin.delete(path)
        assert r.status_code == 404
        answers[r.json()["detail"]] = "There is no such key."
    assert all(said == meant for said, meant in answers.items()), answers


# ── the admin's list ───────────────────────────────────────────────────────

def test_an_admin_sees_and_revokes_every_key_and_the_revoke_lists_what_it_made(world):
    key = _key(agents=(A,))
    _by(key).post("/v1/api/chats", json={"agent": A})
    admin = _cookie(ADMIN, roles.ADMIN)
    listed = admin.get("/v1/admin/api-keys").json()
    assert listed["enabled"] is True
    mine = [k for k in listed["keys"] if k["kind"] == "user" and k["owner_email"] == "viewer@key.test"]
    assert len(mine) == 1 and mine[0]["agents"] == [A]
    r = admin.delete(f"/v1/admin/api-keys/user/{mine[0]['id']}")
    assert r.status_code == 200 and r.json()["created_total"] == 1
    assert _by(key).get("/v1/api/me").status_code == 401
    assert _cookie(ME).get("/v1/admin/api-keys").status_code == 403
    assert admin.put("/v1/admin/api-keys/settings", json={"enabled": False}).json() == {"enabled": False}
    akm.clear_keys_off_cache()
    assert akm.keys_switched_off()


# ── a key's turn and a paired computer's device tools ─────────────────────

def _on_my_computer(sid: str, *, grants=("browser",)) -> None:
    from auth.path_policy import SecurityContext
    from core import placement
    from core.session import session_state
    session_state.register_session_state(sid, "ask", SecurityContext(
        role=roles.VIEWER, username="viewer", agent=A, is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m-1",
                                                  device_grants=set(grants))))
    session_state._record_session_use(sid, client_type=session_kind.DASHBOARD.name, agent=A)


@pytest.mark.parametrize("tick", [False, True])
def test_a_key_never_rides_a_session_that_holds_the_owners_browser_token(world, ladder, monkeypatch, tick):
    """The person's warm session holds their own browser's token: a key's
    turn, an unattended run, closes it first unless the owner let unattended
    runs use the browser."""
    import uuid

    from core.credentials import mcp_broker
    from core.session import session_delivery, session_state
    from storage import remote_store
    from storage.remote_store import BrowserTargetSettings
    closed: list[str] = []
    sid = str(uuid.uuid4())

    class _Layer:
        async def is_session_alive(self, s):
            return s == sid and s not in closed

        async def close_session(self, s):
            closed.append(s)

    async def _layer(chat):
        return _Layer()
    monkeypatch.setattr(session_delivery, "chat_layer", _layer)
    monkeypatch.setattr(remote_store, "get_target_browser_settings",
                        lambda where: BrowserTargetSettings(mode="own", extension_token="T" * 43, unattended=tick))
    _on_my_computer(sid)
    mcp_broker.provision(sid, {"local": mcp_broker.SecretBundle(env={"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "T" * 43})})
    cid = _chat(ME, session_id=sid)
    try:
        with _by(_key()) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "r1"})
            assert r.status_code == 200, r.text
        assert (sid in closed) is not tick
    finally:
        mcp_broker.purge_session(sid)
        session_state.cleanup_session_permission_state(sid)


def test_a_keys_session_stays_under_a_person_who_took_the_chat_before_its_end(world, monkeypatch):
    """The key's turn ran on a place with a device grant, and a person took
    the chat after the ladder returned (their turn's pump is live): the end
    of the key's turn leaves their session alone."""
    from core.events import stream_pump
    from core.session import (
        background_leash,
        key_rows,
        session_delivery,
        session_manager,
        session_state,
    )
    closed: list[str] = []

    class _Layer:
        async def close_session(self, s):
            closed.append(s)

    class _Live:
        is_done = False

    class _TakenAfter(_Ladder):
        async def __call__(self, chat_id, text, **kw):
            outcome = await super().__call__(chat_id, text, **kw)
            stream_pump._active_pumps[chat_id] = _Live()
            return outcome

    monkeypatch.setattr(session_delivery, "deliver_prompt", _TakenAfter())
    monkeypatch.setattr(session_manager, "find_layer_for_session", lambda s: _Layer())
    monkeypatch.setattr(background_leash, "background_pending_count", lambda s: 0)
    _on_my_computer("sid-x")
    cid = _chat(ME)
    try:
        with _by(_key()) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "r1"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
        assert closed == []
    finally:
        stream_pump._active_pumps.pop(cid, None)
        key_rows.forget_session("sid-x")
        session_state.cleanup_session_permission_state("sid-x")


@pytest.mark.parametrize("reason", ["browser_token", "mode"])
def test_a_key_never_closes_a_session_whose_last_turn_left_work_running(world, monkeypatch, reason):
    """A session the key's turn would close (it holds the owner's browser
    token, or it runs in another mode) is left alone while work its last turn
    started still runs: the key is told to send again, the work goes on."""
    import uuid

    from api.public import key_turns
    from core.credentials import mcp_broker
    from core.session import background_leash, session_delivery, session_state
    from storage import remote_store
    from storage.remote_store import BrowserTargetSettings
    closed: list[str] = []
    sid = str(uuid.uuid4())

    class _Layer:
        async def is_session_alive(self, s):
            return s == sid

        async def close_session(self, s):
            closed.append(s)

    async def _layer(chat):
        return _Layer()
    monkeypatch.setattr(session_delivery, "chat_layer", _layer)
    monkeypatch.setattr(session_delivery, "deliver_prompt", _Ladder())
    monkeypatch.setattr(remote_store, "get_target_browser_settings",
                        lambda where: BrowserTargetSettings(mode="own", extension_token="T" * 43, unattended=False))
    monkeypatch.setattr(background_leash, "background_pending_count", lambda s: 1 if s == sid else 0)
    _on_my_computer(sid)
    if reason == "browser_token":
        mcp_broker.provision(sid, {"local": mcp_broker.SecretBundle(env={"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "T" * 43})})
    else:
        session_state.register_session_state(sid, "plan", session_state.get_session_security(sid))
    cid = _chat(ME, session_id=sid)
    try:
        with _by(_key()) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "r1"})
        assert (r.status_code, r.json().get("detail")) == (409, key_turns.BACKGROUND_BUSY), r.text
        assert key_turns.BACKGROUND_BUSY == ("The chat's last turn started work that is still running. "
                                             "Send the message again when it ends.")
        assert closed == []
    finally:
        mcp_broker.purge_session(sid)
        session_state.cleanup_session_permission_state(sid)


@pytest.mark.parametrize(("grants", "background", "closes"), [
    (("browser",), 0, True), (("browser",), 1, False), ((), 0, False)])
def test_a_keys_session_on_a_device_place_closes_at_the_turns_end(world, monkeypatch, grants, background, closes):
    """The person's next turn builds their own session, with the grant and the
    token a key's turn went without. Background work the turn left running
    keeps the session, still the key's for the device tools."""
    from core.session import (
        background_leash,
        key_rows,
        session_delivery,
        session_manager,
        session_state,
    )
    closed: list[str] = []

    class _Layer:
        async def close_session(self, s):
            closed.append(s)

    monkeypatch.setattr(session_delivery, "deliver_prompt", _Ladder())
    monkeypatch.setattr(session_manager, "find_layer_for_session", lambda s: _Layer())
    monkeypatch.setattr(background_leash, "background_pending_count", lambda s: background)
    _on_my_computer("sid-x", grants=grants)
    cid = _chat(ME)
    try:
        with _by(_key()) as c:
            r = c.post(f"/v1/api/chats/{cid}/messages", json={"text": "x", "request_id": "r1"})
            assert r.status_code == 200 and r.json()["state"] == "done", r.text
        assert (closed == ["sid-x"]) is closes
        assert key_rows.drives("", "sid-x") is bool(grants)
    finally:
        key_rows.forget_session("sid-x")
        session_state.cleanup_session_permission_state("sid-x")
