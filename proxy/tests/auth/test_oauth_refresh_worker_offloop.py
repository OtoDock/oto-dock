"""The refresh worker off the event loop and its registered-client arm.

A loop-thread guard on the store and on the filesystem proves the tick
never reads, writes, walks or queries on the loop; the registered-client
arm refreshes with the resource indicator and no secret, persists a
rotated refresh token, marks the file (never the registration) on
``invalid_client`` and ``invalid_grant``, re-discovers a moved token
endpoint once and refuses another issuer; the account lock is held across
the vendor call and the write; a failed write is retried from memory; a
file deleted during the vendor call is not recreated; a disconnect holding
the lock makes the worker wait.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import config
from auth.oauth_providers.base import TokenSet, UserInfo
from core.credentials import credential_locks
from services.mcp import mcp_manifest_parse, mcp_registry
from services.oauth import mcp_authorization as ma, oauth_account_store, oauth_refresh_worker as w
from storage.identity import oauth_client_registrations as regs
from storage.pg import get_conn
from tests.auth.test_mcp_authorization_discovery import AS_META, ISSUER, PRM_PATH, RESOURCE, Vendor

MCP = "notion-hosted-mcp"
PROVIDER = "notion-hosted"
TOKEN = f"{ISSUER}/token"


def _expiry(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, *, provider="slack", refresh="RT", expires_in=60, client_id="ci",
           client_secret="cs", token_url="https://slack.com/api/oauth.v2.access", extra=None):
    body = {
        "provider": provider, "account_id": "ACC", "access_token": "old-AT",
        "refresh_token": refresh, "expires_at": _expiry(expires_in), "scopes": ["s"],
        "client_id": client_id, "client_secret": client_secret, "token_url": token_url,
        "extra": extra or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return path


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    w._failure_state.clear()
    w._pending_writes.clear()
    w._inflight.clear()
    ma.clear_caches()
    yield
    w._failure_state.clear()
    w._pending_writes.clear()
    ma.clear_caches()


@pytest.fixture
def person():
    sub = f"test-user-{uuid.uuid4().hex[:12]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'T', %s, 'creator', 'local', NOW()::text, NOW()::text)",
            (sub, f"{username}@t.example", username),
        )
        conn.commit()
    yield sub, username
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
        conn.commit()


@pytest.fixture
def registered(monkeypatch, person, tmp_path):
    """A manifest with the block in the registry, a live registration row
    and a near-expiry token file written through ``persist_oauth_account``
    (the real shape: ``client_secret`` empty, the flow and resource in
    ``extra``). Returns the file, the row and the fake vendor."""
    sub, username = person
    from tests.auth.test_oauth_mcp_authorization_flow import _manifest_data
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()
    path = tmp_path / "m" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_manifest_data()))
    m = mcp_manifest_parse._parse_manifest(path)
    mcp_registry._manifests[m.name] = m
    row = regs.insert(issuer=ISSUER, redirect_uri="https://dash.example/cb",
                      registration_endpoint=f"{ISSUER}/register", client_id="cid-live")
    ts = TokenSet(access_token="old-AT", refresh_token="RT-1", expires_in=60,
                  raw={"flow": "mcp_authorization", "registration_id": row["id"], "issuer": ISSUER,
                       "resource": RESOURCE, "revocation_endpoint": TOKEN,
                       "token_endpoint_auth_method": "none", "workspace_id": "ws-1"})
    oauth_account_store.persist_oauth_account(
        user_sub=sub, mcp_name=MCP, provider_id=PROVIDER, account_label="ws-1", services=["read"],
        token_set=ts, userinfo=UserInfo(email="acme.com", account_id="u-1"),
        client_id="cid-live", client_secret="", token_url=TOKEN,
    )
    token_file = config.SESSIONS_DIR / f"{PROVIDER}-tokens" / username / "ws-1.json"
    assert token_file.exists()
    vendor = Vendor()
    monkeypatch.setattr(ma, "_client", vendor.client)
    monkeypatch.setattr(ma, "_resolve_addresses", lambda host: ["8.8.8.8"])
    calls: list[dict] = []

    def token(request):
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "new-AT", "refresh_token": "RT-2", "expires_in": 3600})
    vendor.routes[("POST", TOKEN)] = token

    class R:
        pass
    r = R()
    r.file, r.row, r.vendor, r.calls, r.sub, r.username, r.manifest = token_file, row, vendor, calls, sub, username, m
    r.manifest_path = path
    yield r
    oauth_providers.clear_manifest_cache()


async def _refresh(r):
    return await w._maybe_refresh_token_file(token_file=r.file, username=r.username, provider_id=PROVIDER)


def _raw(path):
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Off the loop
# ---------------------------------------------------------------------------

class _FsGuard:
    """Records every file-system call made on the loop thread."""

    def __init__(self, monkeypatch, loop_ident):
        self.hits: list[str] = []

        def wrap(owner, name):
            original = getattr(owner, name)

            def guarded(*a, **k):
                if threading.get_ident() == loop_ident:
                    self.hits.append(name)
                return original(*a, **k)
            monkeypatch.setattr(owner, name, guarded)
        for name in ("read_text", "write_text", "stat", "exists", "is_dir", "glob", "iterdir", "replace"):
            wrap(pathlib.Path, name)
        for name in ("replace", "stat", "chmod"):
            wrap(os, name)


class TestOffTheLoop:
    @pytest.mark.asyncio
    async def test_a_tick_touches_neither_the_store_nor_the_files_on_the_loop(
        self, monkeypatch, registered, loop_db_guard,
    ):
        sessions = config.SESSIONS_DIR
        _write(sessions / "slack-tokens" / "alice" / "a.json", provider="slack")
        _write(sessions / "google-tokens" / "bob" / "b.json", provider="google", extra={"via_relay": True},
               client_id="", client_secret="")
        prov = MagicMock()
        prov.refresh = AsyncMock(return_value=TokenSet(access_token="n", refresh_token="r", expires_in=3600))
        prov.normalize_token_response = lambda raw: TokenSet(access_token="n2", refresh_token="r2", expires_in=3600, raw=raw)
        prov.token_url = "https://slack.com/api/oauth.v2.access"
        monkeypatch.setattr("auth.oauth_providers.get_provider", lambda pid: prov)
        from services.billing import relay_client
        monkeypatch.setattr(relay_client, "is_available", lambda: True)
        monkeypatch.setattr(relay_client, "oauth_refresh", AsyncMock(
            return_value=TokenSet(access_token="n2", refresh_token="r2", expires_in=3600, raw={"via_relay": True})))
        guard = _FsGuard(monkeypatch, threading.get_ident())
        with loop_db_guard.active():
            await w._refresh_tick()
        assert guard.hits == []
        assert _raw(sessions / "slack-tokens" / "alice" / "a.json")["access_token"] == "n"
        assert _raw(sessions / "google-tokens" / "bob" / "b.json")["access_token"] == "n2"
        assert _raw(registered.file)["access_token"] == "new-AT"
        assert prov.refresh.call_count == 1 and len(registered.calls) == 1


# ---------------------------------------------------------------------------
# The registered-client arm
# ---------------------------------------------------------------------------

class TestRegisteredClientArm:
    @pytest.mark.asyncio
    async def test_refreshes_with_the_resource_and_no_secret(self, registered):
        assert await _refresh(registered) is True
        form = registered.calls[0]
        assert form == {"grant_type": "refresh_token", "refresh_token": "RT-1", "resource": RESOURCE,
                        "client_id": "cid-live"}
        raw = _raw(registered.file)
        assert raw["access_token"] == "new-AT" and raw["refresh_token"] == "RT-2"
        assert raw["token_url"] == TOKEN and raw["client_secret"] == ""
        assert raw["extra"]["flow"] == "mcp_authorization" and raw["extra"]["workspace_id"] == "ws-1"
        assert "refresh_failed" not in raw["extra"]

    @pytest.mark.asyncio
    async def test_the_registration_row_is_not_consulted_for_a_public_client(self, registered):
        regs.revoke(registered.row["id"], "forgotten")
        assert await _refresh(registered) is True
        assert registered.calls[0]["client_id"] == "cid-live"

    @pytest.mark.asyncio
    async def test_a_confidential_file_without_its_row_is_marked(self, registered):
        raw = _raw(registered.file)
        raw["extra"]["token_endpoint_auth_method"] = "client_secret_post"
        registered.file.write_text(json.dumps(raw))
        regs.revoke(registered.row["id"], "forgotten")
        assert await _refresh(registered) is False
        assert registered.calls == []
        assert _raw(registered.file)["extra"]["refresh_failed"] == "registration_unavailable"

    @pytest.mark.asyncio
    async def test_a_confidential_file_uses_the_rows_secret(self, registered):
        regs.revoke(registered.row["id"], "x")
        row = regs.insert(issuer=ISSUER, redirect_uri="https://dash.example/cb2",
                          registration_endpoint=f"{ISSUER}/register", client_id="cid-live",
                          client_secret="sec", token_endpoint_auth_method="client_secret_post")
        raw = _raw(registered.file)
        raw["extra"]["token_endpoint_auth_method"] = "client_secret_post"
        raw["extra"]["registration_id"] = row["id"]
        registered.file.write_text(json.dumps(raw))
        assert await _refresh(registered) is True
        assert registered.calls[0]["client_secret"] == "sec"
        assert _raw(registered.file)["client_secret"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["invalid_client", "invalid_grant"])
    async def test_permanent_errors_mark_the_file_not_the_row(self, registered, code):
        registered.vendor.routes[("POST", TOKEN)] = (400, {"error": code}, {})
        assert await _refresh(registered) is False
        raw = _raw(registered.file)
        assert raw["extra"]["refresh_failed"] == code and raw["extra"]["refresh_failed_at"]
        assert raw["refresh_token"] == "RT-1"
        assert regs.get(registered.row["id"])["revoked_at"] == ""
        assert w._failure_state[str(registered.file)]["dead"] is True
        # dead: no further vendor call, even past every backoff
        n = len(registered.vendor.requests)
        assert await _refresh(registered) is False
        assert len(registered.vendor.requests) == n

    @pytest.mark.asyncio
    async def test_a_passing_error_backs_off_without_a_mark(self, registered):
        registered.vendor.routes[("POST", TOKEN)] = (503, "down", {})
        with pytest.raises(Exception):
            await _refresh(registered)
        assert "refresh_failed" not in _raw(registered.file)["extra"]

    @pytest.mark.asyncio
    async def test_a_moved_token_endpoint_is_rediscovered_once(self, registered):
        new_token = f"{ISSUER}/v2/token"
        registered.vendor.routes[("POST", TOKEN)] = (404, "<html>gone</html>", {})
        registered.vendor.routes[("POST", new_token)] = registered.vendor.routes.pop(("POST", TOKEN)) and (
            lambda r: (registered.calls.append(dict(httpx.QueryParams(r.content.decode()))),
                       httpx.Response(200, json={"access_token": "moved-AT", "refresh_token": "RT-3", "expires_in": 60}))[1])
        registered.vendor.routes[("POST", TOKEN)] = (404, "<html>gone</html>", {})
        registered.vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER]}, {})
        registered.vendor.routes[AS_META] = (200, {
            "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": new_token,
            "registration_endpoint": f"{ISSUER}/register", "code_challenge_methods_supported": ["S256"]}, {})
        assert await _refresh(registered) is True
        raw = _raw(registered.file)
        assert raw["access_token"] == "moved-AT" and raw["token_url"] == new_token

    @pytest.mark.asyncio
    async def test_rediscovery_keeps_the_manifests_issuer_choice(self, registered, monkeypatch):
        """A manifest that picks the second listed server: the re-discovery
        picks it again instead of calling the first a changed issuer."""
        other = "https://auth.other.example"
        data = json.loads((registered.manifest_path).read_text())
        data["credentials"]["oauth"]["authorization_server"]["issuer"] = other
        registered.manifest_path.write_text(json.dumps(data))
        m = mcp_manifest_parse._parse_manifest(registered.manifest_path)
        mcp_registry._manifests[m.name] = m
        raw = _raw(registered.file)
        raw["extra"]["issuer"] = other
        registered.file.write_text(json.dumps(raw))
        registered.vendor.routes[("POST", TOKEN)] = (404, "gone", {})
        registered.vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER, other]}, {})
        registered.vendor.routes[f"{other}/.well-known/oauth-authorization-server"] = (200, {
            "issuer": other, "authorization_endpoint": f"{other}/a", "token_endpoint": f"{other}/t",
            "code_challenge_methods_supported": ["S256"]}, {})
        registered.vendor.routes[("POST", f"{other}/t")] = lambda r: httpx.Response(
            200, json={"access_token": "other-AT", "refresh_token": "RT-5", "expires_in": 60})
        assert await _refresh(registered) is True
        assert _raw(registered.file)["token_url"] == f"{other}/t"

    @pytest.mark.asyncio
    async def test_rediscovery_runs_once_per_resource_per_tick(self, registered, person):
        """Two files on the same resource, both facing a dead endpoint: the
        resource metadata is fetched once in the tick."""
        sub, username = person
        second = registered.file.with_name("ws-2.json")
        second.write_text(registered.file.read_text())
        registered.vendor.routes[("POST", TOKEN)] = (404, "gone", {})
        registered.vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [ISSUER]}, {})
        registered.vendor.routes[AS_META] = (200, {
            "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": TOKEN,
            "code_challenge_methods_supported": ["S256"]}, {})
        await w._refresh_tick()
        assert sum(1 for r in registered.vendor.requests if str(r.url) == PRM_PATH) == 1

    @pytest.mark.asyncio
    async def test_a_moved_endpoint_on_another_issuer_is_a_reconnect(self, registered):
        other = "https://auth.other.example"
        registered.vendor.routes[("POST", TOKEN)] = (404, "gone", {})
        registered.vendor.routes[PRM_PATH] = (200, {"resource": RESOURCE, "authorization_servers": [other]}, {})
        registered.vendor.routes[f"{other}/.well-known/oauth-authorization-server"] = (200, {
            "issuer": other, "authorization_endpoint": f"{other}/a", "token_endpoint": f"{other}/t",
            "code_challenge_methods_supported": ["S256"]}, {})
        assert await _refresh(registered) is False
        assert _raw(registered.file)["extra"]["refresh_failed"] == "issuer_changed"
        assert not any(str(r.url).startswith(other) and r.method == "POST" for r in registered.vendor.requests)

    @pytest.mark.asyncio
    async def test_an_app_token_on_a_registered_client_manifest_is_left_alone(self, registered, monkeypatch):
        raw = _raw(registered.file)
        raw["extra"] = {"via_relay": True}
        raw["client_secret"] = ""
        registered.file.write_text(json.dumps(raw))
        from services.billing import relay_client
        monkeypatch.setattr(relay_client, "is_available", lambda: True)
        monkeypatch.setattr(relay_client, "oauth_refresh", AsyncMock(side_effect=AssertionError("never")))
        assert await _refresh(registered) is False
        assert _raw(registered.file)["extra"]["refresh_failed"] == "mechanism_changed"

    @pytest.mark.asyncio
    async def test_a_reconnect_revives_a_dead_file(self, registered):
        registered.vendor.routes[("POST", TOKEN)] = (400, {"error": "invalid_grant"}, {})
        await _refresh(registered)
        assert _raw(registered.file)["extra"]["refresh_failed"] == "invalid_grant"
        # the reconnect writes a fresh file (no verdict, new tokens)
        registered.vendor.routes[("POST", TOKEN)] = lambda r: httpx.Response(
            200, json={"access_token": "after-AT", "refresh_token": "RT-9", "expires_in": 3600})
        ts = TokenSet(access_token="fresh", refresh_token="RT-8", expires_in=60,
                      raw={"flow": "mcp_authorization", "registration_id": registered.row["id"], "issuer": ISSUER,
                           "resource": RESOURCE, "token_endpoint_auth_method": "none"})
        oauth_account_store.persist_oauth_account(
            user_sub=registered.sub, mcp_name=MCP, provider_id=PROVIDER, account_label="ws-1", services=["read"],
            token_set=ts, userinfo=UserInfo(email="acme.com"), client_id="cid-live", client_secret="", token_url=TOKEN)
        st = registered.file.stat()
        os.utime(registered.file, (st.st_atime, st.st_mtime + 10))
        assert await _refresh(registered) is True
        assert _raw(registered.file)["access_token"] == "after-AT"


# ---------------------------------------------------------------------------
# Locks, pending writes, vanished files
# ---------------------------------------------------------------------------

class TestLockAndWrites:
    @pytest.mark.asyncio
    async def test_the_lock_is_held_across_the_vendor_call_and_the_write(self, registered, monkeypatch):
        lock = credential_locks.get_lock(registered.sub, PROVIDER, "ws-1")
        seen = {}

        def token(request):
            seen["locked_at_vendor"] = lock.locked()
            return httpx.Response(200, json={"access_token": "n", "refresh_token": "r", "expires_in": 60})
        registered.vendor.routes[("POST", TOKEN)] = token
        real_write = w._write_back

        def write(*a, **k):
            seen["locked_at_write"] = lock.locked()
            return real_write(*a, **k)
        monkeypatch.setattr(w, "_write_back", write)
        assert await _refresh(registered) is True
        assert seen == {"locked_at_vendor": True, "locked_at_write": True}
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_a_disconnect_holding_the_lock_makes_the_worker_wait(self, registered):
        lock = credential_locks.get_lock(registered.sub, PROVIDER, "ws-1")
        async with lock:
            task = asyncio.create_task(_refresh(registered))
            await asyncio.sleep(0.2)
            assert not task.done() and registered.calls == []
        assert await task is True

    @pytest.mark.asyncio
    async def test_a_failed_write_is_retried_from_memory_without_a_vendor_call(self, registered, monkeypatch):
        real_write = w._write_back
        state = {"fail": True}

        def write(*a, **k):
            if state["fail"]:
                state["fail"] = False
                raise OSError("disk full")
            return real_write(*a, **k)
        monkeypatch.setattr(w, "_write_back", write)
        assert await _refresh(registered) is False
        assert str(registered.file) in w._pending_writes
        assert _raw(registered.file)["access_token"] == "old-AT"
        assert await _refresh(registered) is True
        assert len(registered.calls) == 1
        assert _raw(registered.file)["access_token"] == "new-AT" and _raw(registered.file)["refresh_token"] == "RT-2"
        assert str(registered.file) not in w._pending_writes

    @pytest.mark.asyncio
    async def test_a_file_deleted_during_the_vendor_call_is_not_recreated(self, registered):
        """A confidential client: the revoke of the rotated token carries
        the registration's secret."""
        regs.revoke(registered.row["id"], "x")
        row = regs.insert(issuer=ISSUER, redirect_uri="https://dash.example/cb3",
                          registration_endpoint=f"{ISSUER}/register", client_id="cid-live",
                          client_secret="sec", token_endpoint_auth_method="client_secret_post")
        raw = _raw(registered.file)
        raw["extra"]["token_endpoint_auth_method"] = "client_secret_post"
        raw["extra"]["registration_id"] = row["id"]
        registered.file.write_text(json.dumps(raw))
        revoked = []

        def token(request):
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("grant_type") == "refresh_token":
                registered.file.unlink()
                return httpx.Response(200, json={"access_token": "n", "refresh_token": "RT-new", "expires_in": 60})
            revoked.append(form)
            return httpx.Response(200)
        registered.vendor.routes[("POST", TOKEN)] = token
        assert await _refresh(registered) is False
        assert not registered.file.exists()
        assert revoked and revoked[0]["token"] == "RT-new" and revoked[0]["client_secret"] == "sec"

    @pytest.mark.asyncio
    async def test_a_cancelled_tick_keeps_the_lock_and_stop_waits_for_the_write(self, registered):
        """The tick awaiting the shielded refresh is cancelled while the
        vendor call is in flight: the lock stays held, the refresh finishes
        its write, and stop_worker waits for it."""
        gate = asyncio.Event()
        lock = credential_locks.get_lock(registered.sub, PROVIDER, "ws-1")
        seen = {}

        def token(request):
            return httpx.Response(200, json={"access_token": "n", "refresh_token": "r", "expires_in": 60})
        registered.vendor.routes[("POST", TOKEN)] = token
        real = ma.refresh

        async def slow(*a, **k):
            await gate.wait()
            seen["locked_during_vendor_call"] = lock.locked()
            return await real(*a, **k)
        ma.refresh = slow
        try:
            tick = asyncio.create_task(_refresh(registered))
            await asyncio.sleep(0.05)
            assert w._inflight and lock.locked()
            tick.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tick
            # the shielded refresh is still running under the lock
            assert w._inflight and lock.locked()
            w._worker_task = asyncio.create_task(asyncio.sleep(3600))
            stopper = asyncio.create_task(w.stop_worker())
            await asyncio.sleep(0.05)
            assert not stopper.done()
            gate.set()
            await stopper
        finally:
            ma.refresh = real
        assert seen["locked_during_vendor_call"] is True
        assert not lock.locked() and not w._inflight
        assert _raw(registered.file)["access_token"] == "n"
