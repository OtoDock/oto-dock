"""External links (SHARING.md "External links"): the confirm, the
token that is never stored, the one 404 for unknown, expired and revoked,
the password cookie bound to the share and its password, the failure
buckets, what a link may and may not do, the daily cap, the creator
re-check, and the routes that never read the dashboard session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import apps as apps_api
from api.apps import manifest as _mf
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from storage import database as task_store
from storage.sharing import share_store

client = TestClient(app)

AGENT = "link-agent"
OWNER = "link-owner"
VIEWER = "link-viewer"
ACCOUNT_PW = "owner-pass-123"
ORIGIN = {"Origin": "http://testserver"}


def _user(sub: str = OWNER, role: str = "member", agent_roles: dict[str, str] | None = None,
          is_api_key: bool = False) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role, agents=[AGENT],
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles,
                       is_api_key=is_api_key)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _people(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    for sub, name in ((OWNER, "Owner"), (VIEWER, "Viewer")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=%s, username=%s WHERE sub=%s",
                     (hash_password(ACCOUNT_PW), "owner", OWNER))
        conn.commit()
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    task_store.add_user_agent(VIEWER, AGENT, "viewer", "test")
    from auth import rate_limiter
    rate_limiter._attempts.clear()
    apps_api._fire_rate.clear()
    client.cookies.clear()
    _as(_user())
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _app(html: str = "<p>hello link</p>", actions_json: str | None = None,
         shared: bool = False) -> dict:
    row = task_store.upsert_app(AGENT, "" if shared else "owner", None if shared else OWNER,
                                "sched", title="Schedule",
                                rel_path=("workspace/apps/sched.html" if shared
                                          else "users/owner/workspace/apps/sched.html"),
                                actions_json=actions_json)
    path = config.AGENTS_DIR / AGENT / row["rel_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)
    return row


def _link(app_id: str, **extra) -> dict:
    body = {"target_kind": "app", "target_id": app_id, "scope": "external",
            "password": ACCOUNT_PW, **extra}
    r = client.post("/v1/shares", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _token(link: str) -> str:
    return link.rsplit("/s/", 1)[1]


def _unlock(token: str, password: str, **headers):
    return client.post(f"/s/{token}/unlock", json={"password": password},
                       headers={**ORIGIN, **headers})


# ───────────────────────── making a link ────────────────────────────────────


def test_link_needs_the_confirm_and_is_human_only():
    row = _app()
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external"})
    assert r.status_code == 428 and r.json()["detail"]["method"] == "password"
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external", "password": "wrong"})
    assert r.status_code == 401
    _as(_user(is_api_key=True))
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external", "password": ACCOUNT_PW})
    assert r.status_code == 403


def test_password_link_returns_the_secret_once_and_stores_only_hashes():
    row = _app()
    out = _link(row["id"])
    token = _token(out["link"])
    assert len(out["password"]) == 8 and out["share"]["scope"] == "external"
    share = share_store.get_share(out["share"]["id"])
    assert token not in str(share) and share["token_hash"] != token
    assert share["password_hash"] and out["password"] not in share["password_hash"]
    # The default expiry is 30 days; the list never repeats the secrets.
    until = datetime.fromisoformat(share["expires_at"])
    assert timedelta(days=29) < until - datetime.now(timezone.utc) <= timedelta(days=30)
    listed = client.get(f"/v1/shares?target_kind=app&target={row['id']}").json()["shares"]
    assert listed[0]["scope"] == "external" and "token" not in listed[0]


def test_a_link_never_expires_only_while_no_admin_cap_is_set():
    """``"never"`` is the explicit no-expiry value; an empty expiry keeps
    the 30-day link default, so a client that omits it gets no forever link."""
    row = _app()
    forever = _link(row["id"], expires_in="never", public=True)
    assert share_store.get_share(forever["share"]["id"])["expires_at"] is None
    assert forever["share"]["expires_at"] is None
    assert client.get(f"/s/{_token(forever['link'])}").status_code == 200
    default = share_store.get_share(_link(row["id"], expires_in="")["share"]["id"])
    until = datetime.fromisoformat(default["expires_at"])
    assert timedelta(days=29) < until - datetime.now(timezone.utc) <= timedelta(days=30)
    # A dated link may be turned into a forever one later, under the same guard.
    dated = _link(row["id"], expires_in="7d")["share"]["id"]
    r = client.patch(f"/v1/shares/{dated}", json={"expires_in": "never"})
    assert r.status_code == 200 and r.json()["expires_at"] is None
    # An admin cap refuses never on every write path, like an over-cap duration.
    task_store.set_platform_setting("sharing_max_expiry_days", "30")
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external", "password": ACCOUNT_PW,
                                        "expires_in": "never"})
    assert r.status_code == 400 and r.json()["detail"] == "an admin capped share expiry at 30 days"
    capped = _link(row["id"], expires_in="7d")["share"]["id"]
    r = client.patch(f"/v1/shares/{capped}", json={"expires_in": "never"})
    assert r.status_code == 400 and r.json()["detail"] == "an admin capped share expiry at 30 days"
    assert share_store.get_share(capped)["expires_at"] is not None


def test_platform_switches_gate_links_and_public_links():
    """Each switch refuses new links of its kind and stops the existing
    ones from serving (the same 404 as an unknown token)."""
    row = _app()
    locked = _token(_link(row["id"])["link"])
    public = _token(_link(row["id"], public=True)["link"])
    assert client.get(f"/s/{locked}").status_code == 200
    assert client.get(f"/s/{public}").status_code == 200
    task_store.set_platform_setting("sharing_public_links_enabled", "0")
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external", "public": True, "password": ACCOUNT_PW})
    assert r.status_code == 403
    assert client.get(f"/s/{public}").status_code == 404
    assert client.get(f"/s/{locked}").status_code == 200
    task_store.set_platform_setting("sharing_external_enabled", "0")
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": row["id"],
                                        "scope": "external", "password": ACCOUNT_PW})
    assert r.status_code == 403
    assert client.get(f"/s/{locked}").status_code == 404
    assert client.get(f"/s/{locked}").text == client.get("/s/nope").text


# ───────────────────────── the link's pages ─────────────────────────────────


def test_unknown_expired_and_revoked_links_render_the_same_404():
    row = _app()
    out = _link(row["id"])
    token = _token(out["link"])
    unknown = client.get("/s/not-a-token")
    assert unknown.status_code == 404
    live = client.get(f"/s/{token}")
    assert live.status_code == 200 and '"needs_password": true' in live.text
    assert "/ui-kit/share-host.js" in live.text and "Schedule" in live.text
    client.patch(f"/v1/shares/{out['share']['id']}", json={"revoke": True})
    revoked = client.get(f"/s/{token}")
    assert revoked.status_code == 404 and revoked.text == unknown.text
    out2 = _link(row["id"])
    share_store.set_share_expiry(out2["share"]["id"],
                                 (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    expired = client.get(f"/s/{_token(out2['link'])}")
    assert expired.status_code == 404 and expired.text == unknown.text
    # Nothing under a link is the dashboard shell.
    assert client.get(f"/s/{token}/anything/else").status_code == 404
    assert client.get("/s/").status_code == 404


def test_password_cookie_is_bound_to_the_share_and_its_password():
    row = _app()
    out = _link(row["id"])
    token, pw = _token(out["link"]), out["password"]
    # Locked: the app is not served, the dashboard session is not consulted.
    r = client.get(f"/s/{token}/html", cookies={"session": "whatever"})
    assert r.status_code == 401 and "password first" in r.text
    assert client.get(f"/s/{token}/state").status_code == 401
    # A wrong password, then the right one; the cookie is scoped to the link.
    assert _unlock(token, "nope").status_code == 403
    ok = _unlock(token, pw)
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"]
    assert f"share_{out['share']['id']}=" in cookie and f"Path=/s/{token}" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")
    served = client.get(f"/s/{token}/html")
    assert served.status_code == 200 and "<p>hello link</p>" in served.text
    assert "sandbox allow-scripts" in served.headers["content-security-policy"]
    assert client.get(f"/s/{token}/state").json() == {"doc": {}, "rev": 0}
    # Counted once per document fetch.
    assert share_store.get_share(out["share"]["id"])["access_count"] == 1
    # The same cookie does not open another link of the same app.
    other = _link(row["id"])
    assert client.get(f"/s/{_token(other['link'])}/html").status_code == 401
    # A password change logs everyone out.
    client.patch(f"/v1/shares/{out['share']['id']}", json={"link_password": "brand-new-1"})
    assert client.get(f"/s/{token}/html").status_code == 401
    assert _unlock(token, "brand-new-1").status_code == 200
    assert client.get(f"/s/{token}/html").status_code == 200


def test_public_link_serves_without_a_password():
    row = _app()
    out = _link(row["id"], public=True)
    assert "password" not in out
    token = _token(out["link"])
    page = client.get(f"/s/{token}")
    assert '"needs_password": false' in page.text
    assert client.get(f"/s/{token}/html").status_code == 200
    assert _unlock(token, "").json()["status"] == "ok"


def test_unlock_refuses_cross_site_posts_and_counts_failures():
    row = _app()
    out = _link(row["id"])
    token = _token(out["link"])
    # No Origin and no Sec-Fetch-Site: a third-party form post.
    r = client.post(f"/s/{token}/unlock", json={"password": out["password"]})
    assert r.status_code == 403 and "set-cookie" not in r.headers
    r = client.post(f"/s/{token}/unlock", data={"password": out["password"]}, headers=ORIGIN)
    assert r.status_code in (403, 422)
    with patch.dict("config.RATE_LIMIT_RULES", {
        "share_unlock_share": {"max": 2, "window": 300, "base_block": 60, "max_block": 60}}):
        assert _unlock(token, "bad-1").status_code == 403
        assert _unlock(token, "bad-2").status_code == 403
        assert _unlock(token, out["password"]).status_code == 429
    # Successful unlocks are never counted: the bucket sees failures only.


def test_the_page_pins_its_frames_and_its_config_carries_no_markup():
    """The page's frames stay on this origin (an artifact navigating its own
    frame away would carry the link token in its URL) and its scripts are
    its own; the config block holds no `<` a title could use."""
    row = task_store.upsert_app(AGENT, "owner", OWNER, "tricky",
                                title="a<!--<script>b</script>",
                                rel_path="users/owner/workspace/apps/tricky.html")
    path = config.AGENTS_DIR / AGENT / row["rel_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<p>x</p>")
    token = _token(_link(row["id"], public=True)["link"])
    page = client.get(f"/s/{token}")
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "frame-src 'self' https://challenges.cloudflare.com" in csp
    assert "script-src 'self' https://challenges.cloudflare.com" in csp
    block = page.text.split('id="share-config">', 1)[1].split("</script>", 1)[0]
    assert "<" not in block
    import json
    assert json.loads(block)["title"] == "a<!--<script>b</script>"


def test_a_burst_of_wrong_passwords_is_counted_before_any_passes():
    """Parallel attempts on one link run one at a time: a burst larger than
    the cap never gets more guesses than the cap."""
    import asyncio
    import httpx
    row = _app()
    out = _link(row["id"])
    token = _token(out["link"])

    async def burst():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            return await asyncio.gather(*[
                ac.post(f"/s/{token}/unlock", json={"password": f"bad-{i}"}, headers=ORIGIN)
                for i in range(6)])

    with patch.dict("config.RATE_LIMIT_RULES", {
        "share_unlock_share": {"max": 2, "window": 300, "base_block": 60, "max_block": 60}}):
        codes = sorted(r.status_code for r in asyncio.run(burst()))
    assert codes.count(403) == 2 and codes.count(429) == 4, codes


def test_a_demoted_creator_kills_the_link_at_click_time():
    row = _app(shared=True)
    out = _link(row["id"], public=True)
    token = _token(out["link"])
    assert client.get(f"/s/{token}/html").status_code == 200
    task_store.set_user_agents(OWNER, [], "test")
    assert client.get(f"/s/{token}").status_code == 404
    assert client.get(f"/s/{token}/html").status_code == 404


# ───────────────────────── what a link may do ───────────────────────────────


def _mk_task() -> str:
    tid = str(uuid.uuid4())
    task_store.create_dynamic_task(
        tid, AGENT, "task-trigger", "do the thing", "auto", "trigger", None,
        None, None, 300, OWNER, scope="agent", notification_mode="none",
    )
    return tid


def _approved_shared_app() -> dict:
    tid = _mk_task()
    canonical, err = _mf.validate_actions([
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": tid},
        {"id": "ask", "label": "Ask", "type": "send_prompt", "prompt": "hi"},
    ], AGENT, True)
    assert err == "", err
    row = _app(actions_json=canonical, shared=True)
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), OWNER)
    return task_store.get_app(row["id"])


def _batch(token: str, calls: list[dict]) -> dict:
    r = client.post(f"/s/{token}/actions/batch", json={"calls": calls}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    import json
    return {json.loads(line)["call_id"]: json.loads(line) for line in r.text.strip().splitlines()}


def test_actions_are_off_until_the_switch_and_never_prompts(monkeypatch):
    row = _approved_shared_app()
    out = _link(row["id"], public=True)
    token = _token(out["link"])
    res = _batch(token, [{"call_id": "1", "action_id": "go"}])
    assert res["1"]["status"] == "denied" and "shared links" in res["1"]["reason"]
    r = client.post(f"/s/{token}/actions/go", json={}, headers=ORIGIN)
    assert r.status_code == 403
    # Turning Buttons on needs the confirm; then fire_task runs on the app's
    # identity with the share as its trigger source, send_prompt never.
    r = client.patch(f"/v1/shares/{out['share']['id']}", json={"allow_actions": True})
    assert r.status_code == 428
    r = client.patch(f"/v1/shares/{out['share']['id']}",
                     json={"allow_actions": True, "password": ACCOUNT_PW})
    assert r.status_code == 200 and r.json()["allow_actions"] is True
    fired: dict = {}

    async def _fake_trigger(task_def, **kw):
        fired.update(kw)
        return "run-1"
    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_task_now", _fake_trigger)
    apps_api._fire_rate.clear()  # the batch pace, one per second per link
    res = _batch(token, [{"call_id": "1", "action_id": "go"}, {"call_id": "2", "action_id": "ask"}])
    assert res["1"]["status"] == "ok" and res["1"]["run_id"] == "run-1"
    assert fired["trigger_source"] == f"share:{out['share']['id']}:go"
    assert res["2"]["status"] == "denied" and "shared links" in res["2"]["reason"]
    # A link is a viewer: an editor floor refuses it.
    canonical, _ = _mf.validate_actions([
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": _mk_task(), "min_role": "editor"},
    ], AGENT, True)
    task_store.upsert_app(AGENT, "", None, "sched", actions_json=canonical)
    fresh = task_store.get_app(row["id"])
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(fresh), OWNER)
    apps_api._fire_rate.clear()
    res = _batch(token, [{"call_id": "1", "action_id": "go"}])
    assert res["1"]["status"] == "denied" and res["1"]["code"] == 403


def test_daily_cap_and_cross_site_actions(monkeypatch):
    row = _approved_shared_app()
    out = _link(row["id"], public=True, allow_actions=True)
    token = _token(out["link"])
    from api.sharing import external
    monkeypatch.setattr(external, "ACTIONS_PER_DAY", 1)

    async def _fake_trigger(task_def, **kw):
        return "run-x"
    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_task_now", _fake_trigger)
    assert _batch(token, [{"call_id": "1", "action_id": "go"}])["1"]["status"] == "ok"
    apps_api._fire_rate.clear()
    res = _batch(token, [{"call_id": "1", "action_id": "go"}])
    assert res["1"]["status"] == "denied" and "daily limit" in res["1"]["reason"]
    assert share_store.get_share(out["share"]["id"])["actions_today"] == 1
    # Cross-site: no Origin header.
    r = client.post(f"/s/{token}/actions/batch", json={"calls": [{"call_id": "1", "action_id": "go"}]})
    assert r.status_code == 403


def test_soft_unpin_and_hard_unpin_close_the_link():
    row = _app()
    out = _link(row["id"], public=True)
    token = _token(out["link"])
    assert client.get(f"/s/{token}").status_code == 200
    assert task_store.set_app_hidden(row["id"], True)
    assert client.get(f"/s/{token}").status_code == 404
    assert task_store.delete_app(row["id"])
    assert share_store.get_share(out["share"]["id"]) is None


# ───────────────────── the password gate, the share side ──────────────


def test_a_full_hash_gate_answers_the_unlock_with_503():
    from auth.password import HashBusy
    from api.sharing import external

    async def busy(*_a, **_k):
        raise HashBusy()

    row = _app()
    out = _link(row["id"])
    with patch.object(external, "verify_password_async", busy):
        r = _unlock(_token(out["link"]), out["password"])
    assert r.status_code == 503 and r.headers.get("Retry-After")


def test_a_link_password_over_72_bytes_is_refused_with_400():
    """bcrypt hashes at most 72 bytes; 40 two-byte characters pass the
    character bounds and must never reach the hasher as an unhandled error."""
    row = _app()
    long_pw = "\u00ff" * 40
    body = {"target_kind": "app", "target_id": row["id"], "scope": "external",
            "password": ACCOUNT_PW, "link_password": long_pw}
    r = client.post("/v1/shares", json=body)
    assert r.status_code == 400 and "72 bytes" in r.json()["detail"]
    out = _link(row["id"])
    r = client.patch(f"/v1/shares/{out['share']['id']}", json={"link_password": long_pw})
    assert r.status_code == 400 and "72 bytes" in r.json()["detail"]
    assert _unlock(_token(out["link"]), out["password"]).status_code == 200
