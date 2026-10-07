"""Logout is this device only, and the token epoch ends every sign-in.

``POST /auth/logout`` revokes the presented cookie's sign-in id (a denylist
kept until the cookie's expiry, surviving a restart): the same cookie replayed
is refused everywhere the cookie is read, and the sliding refresh never
re-mints it, while another browser's cookie of the same person stays valid.
The per-person epoch (``users.token_epoch_at``) refuses every cookie and
bearer session token minted before it: stamped by a password set and moved
by an admin's "sign out everywhere".

Run: cd proxy && venv/bin/pytest tests/auth/test_session_revocation.py -v
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth import session_revocation, token_holder
from auth.providers import create_session_jwt, validate_session_jwt
from storage import database as task_store
from tests.conftest import live_session_token

client = TestClient(app)


def _cookie(sub: str = "user-admin", **kw) -> str:
    u = task_store.get_user(sub)
    return create_session_jwt(u["sub"], u["email"], u["name"], u["role"], **kw)


def _legacy_cookie(sub: str = "user-admin", *, iat: int) -> str:
    """A cookie as minted before this release: no sign-in id."""
    u = task_store.get_user(sub)
    return jwt.encode({"purpose": "session", "sub": u["sub"], "email": u["email"],
                       "name": u["name"], "role": u["role"], "auth_provider": "local",
                       "iat": iat, "exp": iat + 7200}, config.JWT_SECRET, algorithm="HS256")


def _me(cookie: str):
    return client.get("/auth/me", headers={"Cookie": f"session={cookie}"})


def _backdate_row(sub: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET created_at=%s WHERE sub=%s",
                     ("2020-01-01T00:00:00+00:00", sub))
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    session_revocation.reset_for_tests()
    token_holder.reset_for_tests()
    yield
    session_revocation.reset_for_tests()
    token_holder.reset_for_tests()


class TestLogout:
    def test_the_logged_out_cookie_is_refused_and_another_device_stays(self):
        first, second = _cookie(), _cookie()
        assert _me(first).status_code == 200
        r = client.post("/auth/logout", headers={"Cookie": f"session={first}"})
        assert r.status_code == 200
        assert _me(first).status_code == 401
        assert _me(second).status_code == 200

    def test_the_refresh_keeps_the_sign_in_id_and_never_re_mints_a_revoked_cookie(self):
        _backdate_row("user-admin")
        # Past the halfway point of its life: the sliding refresh re-mints
        # it, with the same sign-in id.
        now = int(time.time())
        old = jwt.encode({**jwt.decode(_cookie(), config.JWT_SECRET, algorithms=["HS256"]),
                          "iat": now - 3600, "exp": now + 600}, config.JWT_SECRET, algorithm="HS256")
        r = _me(old)
        assert r.status_code == 200
        minted = [v for v in r.headers.get_list("set-cookie") if v.startswith("session=")]
        assert len(minted) == 1
        fresh = minted[0].split("session=", 1)[1].split(";", 1)[0]
        assert validate_session_jwt(fresh)["jti"] == validate_session_jwt(old)["jti"]
        # A logout of the re-mint ends the original too, and nothing is
        # minted for either again.
        client.post("/auth/logout", headers={"Cookie": f"session={fresh}"})
        r = _me(old)
        assert r.status_code == 401
        assert not any(v.startswith("session=") for v in r.headers.get_list("set-cookie"))
        assert _me(fresh).status_code == 401

    def test_a_legacy_cookie_revokes_its_own_lineage_only(self):
        _backdate_row("user-admin")
        now = int(time.time())
        legacy, other = _legacy_cookie(iat=now - 100), _legacy_cookie(iat=now - 200)
        assert _me(legacy).status_code == 200
        client.post("/auth/logout", headers={"Cookie": f"session={legacy}"})
        assert _me(legacy).status_code == 401
        assert _me(other).status_code == 200

    def test_the_denylist_survives_a_restart(self):
        first = _cookie()
        client.post("/auth/logout", headers={"Cookie": f"session={first}"})
        session_revocation.reset_for_tests()
        assert _me(first).status_code == 200
        session_revocation.load()
        assert _me(first).status_code == 401

    def test_a_revoked_id_outlives_a_copy_minted_under_a_longer_lifetime(self):
        # A lifetime an admin just shortened: a copy of the sign-in re-minted
        # under the earlier, longer one must stay refused past today's.
        now = time.time()
        payload = validate_session_jwt(_cookie())
        cookie_id = session_revocation.revoke(payload, lifetime_s=7 * 86400)
        kept_until = session_revocation._revoked[cookie_id]
        assert kept_until >= now + session_revocation.MIN_RETENTION_S - 5
        assert kept_until >= payload["exp"]

    def test_the_socket_refuses_a_revoked_cookie_at_connect(self):
        from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket, run_ws_scenario
        from ws import dashboard
        first = _cookie()
        client.post("/auth/logout", headers={"Cookie": f"session={first}"})

        async def scenario():
            ws = FakeDashboardWebSocket(cookie=first)
            await dashboard.ws_dashboard_handler(ws)
            assert ws.closed == (4001, "Invalid or expired session") and not ws.accepted
        run_ws_scenario(scenario)

    def test_logout_of_an_invalid_cookie_still_answers(self):
        r = client.post("/auth/logout", headers={"Cookie": "session=not-a-jwt"})
        assert r.status_code == 200
        assert "revoked" not in r.text


def _older_cookie(sub: str, age_s: int = 60) -> str:
    """A cookie minted ``age_s`` ago (the epoch has a 5 s grace)."""
    _backdate_row(sub)
    payload = jwt.decode(_cookie(sub), config.JWT_SECRET, algorithms=["HS256"])
    now = int(time.time())
    return jwt.encode({**payload, "iat": now - age_s, "exp": now + 7200},
                      config.JWT_SECRET, algorithm="HS256")


class TestEpoch:
    def test_a_bump_refuses_every_earlier_cookie(self):
        before = _older_cookie("user-admin")
        assert _me(before).status_code == 200
        task_store.bump_token_epoch("user-admin")
        assert _me(before).status_code == 401

    def test_a_cookie_minted_seconds_before_the_bump_is_refused(self):
        # The epoch has no grace beyond its own second: a sign-in made just
        # before an admin's "sign out everywhere" ends with the rest.
        before = _older_cookie("user-admin", age_s=3)
        assert _me(before).status_code == 200
        task_store.bump_token_epoch("user-admin")
        assert _me(before).status_code == 401
        token = live_session_token(str(uuid.uuid4()), "pa", "user-admin",
                                   issued_at=int(time.time()) - 3)
        token_holder.reset_for_tests()
        assert client.get("/v1/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_cookie_minted_after_the_bump_passes(self):
        task_store.bump_token_epoch("user-admin")
        assert _me(_cookie()).status_code == 200

    def test_a_password_set_stamps_the_epoch_with_the_timeline(self):
        from auth.password import hash_password
        task_store.set_user_password("user-admin", hash_password("Correct-horse-9"))
        row = task_store.get_user("user-admin")
        assert row["token_epoch_at"] and row["token_epoch_at"] == row["password_changed_at"]

    def test_the_migration_adds_the_column_to_an_older_table(self):
        from storage import schema as pg_schema
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute("ALTER TABLE users DROP COLUMN token_epoch_at")
            assert not pg_schema._column_exists(conn, "users", "token_epoch_at")
            pg_schema.run_migrations(conn)
            assert pg_schema._column_exists(conn, "users", "token_epoch_at")
            conn.rollback()


class TestSignOutEverywhere:
    ROUTE = "/v1/admin/users/user-viewer/sign-out-everywhere"

    def test_ends_the_cookies_and_the_agent_session_tokens(self):
        from storage.automation import notification_store
        viewer_cookie = _older_cookie("user-viewer")
        token = live_session_token(str(uuid.uuid4()), "pa", "user-viewer",
                                   issued_at=int(time.time()) - 60)
        notification_store.save_push_subscription("user-viewer", "android", "device-of-the-viewer")
        notification_store.save_push_subscription("user-admin", "android", "device-of-the-admin")
        assert _me(viewer_cookie).status_code == 200
        assert client.get("/v1/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        r = client.post(self.ROUTE, headers={"Cookie": f"session={_cookie()}"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"
        assert _me(viewer_cookie).status_code == 401
        # The devices the ended sign-ins registered for push are gone too; the
        # admin's stay.
        assert notification_store.get_push_subscriptions("user-viewer") == []
        assert [s["subscription_data"] for s in notification_store.get_push_subscriptions("user-admin")] \
            == ["device-of-the-admin"]
        notification_store.delete_push_subscriptions_of("user-admin")
        # The holder cache forgot the person: the token is judged again.
        assert client.get("/v1/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        # The admin who did it keeps their own sign-in.
        assert _me(_cookie()).status_code == 200

    def test_a_persons_decision_never_a_bearers(self):
        token = live_session_token(str(uuid.uuid4()), "pa", "user-admin")
        r = client.post(self.ROUTE, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
        r = client.post(self.ROUTE, headers={"Cookie": f"session={_cookie('user-viewer')}"})
        assert r.status_code == 403
        r = client.post("/v1/admin/users/nobody/sign-out-everywhere",
                        headers={"Cookie": f"session={_cookie()}"})
        assert r.status_code == 404


def _prime_holder_pass(token: str) -> tuple:
    """A pass the holder cache already holds for the token (a hook route
    judged it within the minute)."""
    from auth.session_token import validate_session_token
    payload = validate_session_token(token)
    key = token_holder._key(payload)
    token_holder._remember(key, True, payload, time.monotonic())
    return key


class TestPlatformRoleLowered:
    """A lowered platform role ends every sign-in and agent session token of
    the person (their token epoch moves, the holder cache forgets them);
    a raised one ends nothing; a per-agent change is left to the live roles,
    the closer and the socket re-check."""

    ROUTE = "/v1/admin/users/user-viewer/role"

    def _admin_put(self, role: str):
        return client.put(self.ROUTE, json={"role": role},
                          headers={"Cookie": f"session={_cookie()}"})

    def test_a_lowered_role_ends_every_earlier_sign_in_and_token(self):
        task_store.update_user_role("user-viewer", "creator")
        cookie = _older_cookie("user-viewer")
        token = live_session_token(str(uuid.uuid4()), "pa", "user-viewer",
                                   issued_at=int(time.time()) - 60)
        key = _prime_holder_pass(token)
        assert _me(cookie).status_code == 200
        r = self._admin_put("member")
        assert r.status_code == 200, r.text
        assert task_store.get_user("user-viewer")["token_epoch_at"]
        assert _me(cookie).status_code == 401
        assert client.get("/v1/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert key not in token_holder._answers
        # A sign-in after the change passes, and the admin keeps theirs.
        assert _me(_cookie("user-viewer")).status_code == 200
        assert _me(_cookie()).status_code == 200

    def test_a_raised_role_ends_nothing(self):
        cookie = _older_cookie("user-viewer")
        r = self._admin_put("creator")
        assert r.status_code == 200, r.text
        assert not task_store.get_user("user-viewer")["token_epoch_at"]
        assert _me(cookie).status_code == 200

    def test_a_sign_in_the_identity_provider_lowers_keeps_its_new_cookie(self):
        import asyncio
        from api.auth.admin_users import apply_platform_role_change
        task_store.update_user_role("user-viewer", "creator")
        before = task_store.get_user("user-viewer")
        cookie = _older_cookie("user-viewer")
        rows = task_store.get_user_agent_roles("user-viewer")
        task_store.update_user_role("user-viewer", "member")
        # The login path: the change applied with no actor, the login's
        # cookie minted right after.
        asyncio.run(apply_platform_role_change("user-viewer", before, "member", rows, ""))
        assert _me(cookie).status_code == 401
        assert _me(_cookie("user-viewer")).status_code == 200


class TestDeletion:
    def test_a_deleted_persons_cached_pass_is_forgotten(self):
        # The row's deletion refuses every cookie and token of the person
        # (a token epoch on a gone row is moot); what it leaves is the
        # holder cache's pass, which the delete route drops.
        token = live_session_token(str(uuid.uuid4()), "pa", "user-viewer2")
        key = _prime_holder_pass(token)
        r = client.delete("/v1/admin/users/user-viewer2",
                          headers={"Cookie": f"session={_cookie()}"})
        assert r.status_code == 200, r.text
        assert key not in token_holder._answers
