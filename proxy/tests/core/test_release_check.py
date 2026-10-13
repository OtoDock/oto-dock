"""The daily release check (services.infra.release_check): the switches, the
fetch through the checked client, what is stored, the admin notification once
per version, the failure path, the two admin routes.

The GitHub answer is a ``httpx.MockTransport``; ``pinned_http.checked_client``
is replaced by a function that records the policy it was handed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from services.infra import pinned_http, release_check
from storage.pg import get_conn


@pytest.fixture
def fresh(monkeypatch):
    """A connected self-hosted 1.8.0 install with the default URL and no state."""
    monkeypatch.setattr(config, "PINNED_OTODOCK_VERSION", "1.8.0")
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "RELEASE_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "RELEASE_CHECK_URL", config.RELEASE_CHECK_DEFAULT_URL)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(release_check, "_last_run_monotonic", None)
    with get_conn() as conn:
        conn.execute("DELETE FROM platform_settings WHERE key = ANY(%s)", (list(release_check._KEYS),))
        conn.commit()


def _answer(tag="v1.9.0", status=200, body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.requests.append(request)
        if body is not None:
            return httpx.Response(status, content=body)
        return httpx.Response(status, json={
            "tag_name": tag, "html_url": f"https://github.com/OtoDock/oto-dock/releases/tag/{tag}",
            "published_at": "2026-10-20T10:00:00Z", "body": "notes",
        })
    handler.requests = []
    return handler


@pytest.fixture
def github(monkeypatch):
    """Point the checked client at a fake GitHub; returns the handler and the policies seen."""
    state = {"handler": _answer(), "policies": []}

    def client(url, policy=pinned_http.public_only, **kw):
        state["policies"].append(policy)
        return httpx.AsyncClient(transport=httpx.MockTransport(state["handler"]), **kw)

    monkeypatch.setattr(release_check.pinned_http, "checked_client", client)
    return state


@pytest.fixture
def notified(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return [{"id": "d1"}] if not getattr(fake, "no_admins", False) else []

    import services.notifications.notification_manager as nm
    monkeypatch.setattr(nm, "fire_notification", fake)
    fake.calls = calls
    return fake


def _state():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM platform_settings WHERE key = ANY(%s)",
                            (list(release_check._KEYS),)).fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── the switches ─────────────────────────────────────────────────────────────

def test_the_check_is_off_when_air_gapped_on_the_cloud_by_its_key_or_without_a_version(fresh, monkeypatch):
    assert release_check.enabled()
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", True)
    assert release_check.disabled_reason() == "OTODOCK_AIR_GAPPED is set"
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    assert "cloud" in release_check.disabled_reason()
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "RELEASE_CHECK_ENABLED", False)
    assert release_check.disabled_reason() == "OTODOCK_RELEASE_CHECK is off"
    monkeypatch.setattr(config, "RELEASE_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "PINNED_OTODOCK_VERSION", "1.8.0-laneu")
    assert "not a release number" in release_check.disabled_reason()


def test_the_default_url_dials_public_addresses_only_and_an_operator_url_any(fresh, monkeypatch):
    assert release_check._policy(config.RELEASE_CHECK_DEFAULT_URL) is pinned_http.public_only
    monkeypatch.setattr(config, "RELEASE_CHECK_URL", "http://127.0.0.1:8768/latest")
    assert release_check._policy(config.RELEASE_CHECK_URL)("127.0.0.1") is False


# ── a check ──────────────────────────────────────────────────────────────────

def test_a_newer_release_is_stored_and_announced_to_admins_once(fresh, github, notified):
    asyncio.run(release_check.check_now())
    state = _state()
    assert state[release_check.LATEST] == "1.9.0"
    assert state[release_check.LATEST_URL].endswith("/tag/v1.9.0")
    assert state[release_check.URL_KEY] == config.RELEASE_CHECK_DEFAULT_URL
    assert state[release_check.ERROR] == ""
    assert state[release_check.NOTIFIED] == "1.9.0"
    assert github["policies"] == [pinned_http.public_only]
    req = github["handler"].requests[0]
    assert req.headers["user-agent"] == "OtoDock/1.8.0"
    assert req.headers["accept"] == "application/vnd.github+json"
    (call,) = notified.calls
    assert call["scope"] == "admin" and call["source"] == "release"
    assert call["title"] == "OtoDock 1.9.0 is available"
    assert "runs OtoDock 1.8.0" in call["body"]
    assert "Release notes: https://github.com/OtoDock/oto-dock/releases/tag/v1.9.0" in call["body"]
    assert f"How to upgrade: {release_check.UPGRADE_GUIDE_URL}" in call["body"]
    assert "upgrade.sh" not in call["body"] and "git pull" not in call["body"]
    assert call["href"] == "/admin/platform?tab=otodock"
    # A forced second check (the 24-hour rule would skip it) does not announce again.
    release_check._last_run_monotonic = None
    asyncio.run(release_check.check_now(force=True))
    assert len(notified.calls) == 1
    assert release_check.notice()["newer"] is True


def test_no_admin_yet_means_the_next_check_announces_again(fresh, github, notified):
    notified.no_admins = True
    asyncio.run(release_check.check_now())
    assert release_check.NOTIFIED not in _state()
    notified.no_admins = False
    release_check._last_run_monotonic = None
    asyncio.run(release_check.check_now(force=True))
    assert _state()[release_check.NOTIFIED] == "1.9.0"
    assert len(notified.calls) == 2


def test_an_older_or_equal_release_is_stored_but_not_announced(fresh, github, notified):
    github["handler"] = _answer("v1.8.0")
    asyncio.run(release_check.check_now())
    assert _state()[release_check.LATEST] == "1.8.0"
    assert notified.calls == []
    assert release_check.notice()["newer"] is False


@pytest.mark.parametrize("status, body, text", [
    (403, None, "HTTP 403"),
    (200, b"not json", "not JSON"),
    (200, json.dumps({"tag_name": "nightly", "html_url": "https://x"}).encode(), "not a release number"),
    (200, json.dumps({"tag_name": "v1.9.0", "html_url": "http://x"}).encode(), "not an https address"),
    (200, b"x" * (release_check.BODY_CAP + 1), "larger than 256 KB"),
])
def test_a_bad_answer_is_recorded_and_the_last_facts_stay(fresh, github, notified, status, body, text):
    asyncio.run(release_check.check_now())
    github["handler"] = _answer(status=status, body=body)
    release_check._last_run_monotonic = None
    asyncio.run(release_check.check_now(force=True))
    state = _state()
    assert text in state[release_check.ERROR]
    assert state[release_check.LATEST] == "1.9.0"
    assert release_check.notice()["error"] == state[release_check.ERROR]


@pytest.mark.parametrize("url, shown", [
    ("http://[::1", "http://[::1"),
    ("https://mirror.invalid:99999/latest", "https://mirror.invalid"),
    ("https://user:pw@mirror.invalid:99999/latest", "https://mirror.invalid"),
    ("latest", "latest"),
])
def test_a_malformed_url_is_recorded_as_an_error_and_never_shown_with_a_password(fresh, github, notified, monkeypatch, url, shown):
    monkeypatch.setattr(config, "RELEASE_CHECK_URL", url)
    asyncio.run(release_check.check_now(force=True))
    err = _state()[release_check.ERROR]
    assert "is not valid" in err and shown in err and "pw" not in err
    assert github["handler"].requests == []
    assert release_check.notice()["error"] == err


def test_an_unsupported_scheme_is_the_clients_error_recorded(fresh, monkeypatch, notified):
    """A real client (no fake transport): the scheme is judged by httpx and
    the error is recorded, never raised out of the check."""
    monkeypatch.setattr(config, "RELEASE_CHECK_URL", "ftp://mirror.invalid/latest")
    asyncio.run(release_check.check_now(force=True))
    err = _state()[release_check.ERROR]
    assert "UnsupportedProtocol" in err and "ftp://" in err


def test_an_invalid_url_raised_by_the_client_is_recorded(fresh, github, notified):
    def boom(request):
        raise httpx.InvalidURL("bad")
    github["handler"] = boom
    asyncio.run(release_check.check_now())
    assert "InvalidURL" in _state()[release_check.ERROR]


def test_a_network_error_is_recorded(fresh, github, notified):
    def boom(request):
        raise httpx.ConnectError("refused")
    github["handler"] = boom
    asyncio.run(release_check.check_now())
    assert "ConnectError" in _state()[release_check.ERROR]
    assert release_check.LAST_AT not in _state()


def test_the_daily_rule(fresh):
    now = datetime.now(timezone.utc)
    url = config.RELEASE_CHECK_URL
    assert release_check._due({}, now)
    recent = {release_check.LAST_AT: (now - timedelta(hours=1)).isoformat(), release_check.URL_KEY: url}
    assert not release_check._due(recent, now)
    old = {release_check.LAST_AT: (now - timedelta(hours=25)).isoformat(), release_check.URL_KEY: url}
    assert release_check._due(old, now)
    other = {release_check.LAST_AT: now.isoformat(), release_check.URL_KEY: "http://127.0.0.1:1/x"}
    assert release_check._due(other, now)


def test_facts_from_another_url_are_discarded(fresh, github, notified, monkeypatch):
    asyncio.run(release_check.check_now())
    assert release_check.notice()["latest"] == "1.9.0"
    monkeypatch.setattr(config, "RELEASE_CHECK_URL", "http://127.0.0.1:8768/latest")
    assert release_check.notice()["latest"] == "" and release_check.notice()["newer"] is False


def test_a_switch_flipped_after_a_check_hides_the_facts(fresh, github, notified, monkeypatch):
    asyncio.run(release_check.check_now())
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", True)
    n = release_check.notice()
    assert n["enabled"] is False and n["newer"] is False and n["latest"] == ""
    assert n["running"] == "1.8.0"
    assert release_check.start_worker() is None


def test_the_cooldown_answers_the_stored_state(fresh, github, notified):
    asyncio.run(release_check.check_now(force=True))
    asyncio.run(release_check.check_now(force=True))
    assert len(github["handler"].requests) == 1


def test_a_check_not_due_fetches_nothing(fresh, github, notified):
    asyncio.run(release_check.check_now())
    asyncio.run(release_check.check_now())
    assert len(github["handler"].requests) == 1


# ── the routes ───────────────────────────────────────────────────────────────

def _client(role: str) -> TestClient:
    from app import app
    from auth.providers import create_session_jwt
    sub = "user-admin" if role == "admin" else "user-viewer"
    email = "admin@test.com" if role == "admin" else "viewer@test.com"
    return TestClient(app, cookies={"session": create_session_jwt(sub, email, "U", role)})


def test_the_notice_route_is_the_admins_and_the_check_route_runs_a_check(fresh, github, notified):
    admin = _client("admin")
    r = admin.get("/v1/admin/release-notice")
    assert r.status_code == 200, r.text
    assert r.json()["running"] == "1.8.0" and r.json()["latest"] == "" and r.json()["enabled"] is True
    r = admin.post("/v1/admin/release-check")
    assert r.status_code == 200, r.text
    assert r.json()["latest"] == "1.9.0" and r.json()["newer"] is True
    member = _client("member")
    assert member.get("/v1/admin/release-notice").status_code == 403
    assert member.post("/v1/admin/release-check").status_code == 403
