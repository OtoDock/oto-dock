"""display_ui artifact pipeline: ``POST /v1/hooks/ui`` + ``GET /v1/ui/{token}``
+ the ``serve_media`` inline-allowlist hardening + the pump's ``ui`` block.

The security-load-bearing assertions live here: the save-path re-gate (a
denied/escaping target re-anchors into the caller's own scope), the CSP with
the request's CONCRETE origin (``'self'`` never matches the opaque sandboxed
origin), the CSP ``sandbox`` directive on EVERY branch including 404s, the
kind-mismatch rejection in both directions (a ``ui`` row must never render
inline same-origin via ``/v1/media``), full-document serve-verbatim, and the
cookie gate + per-token access rule (``api.media.access``) on both serve
routes — including the durable ``media_kind="file"`` download tokens.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from auth.session_token import create_session_token
from core.session import session_state
from storage import database as task_store
from pathlib import Path

client = TestClient(app)

SID = "sess-ui-1"
AGENT = "ui-agent"
# A session token, as the MCP sidecars send: a body past 64 KB with a bearer
# that does not verify is refused before the route runs.
_BEARER = {"Authorization": f"Bearer {create_session_token(SID, AGENT, '')}"}


def _user(sub: str = "user-viewer", role: str = "member",
          agents: tuple[str, ...] = (AGENT,)) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub,
                       role=role, agents=list(agents))


@pytest.fixture(autouse=True)
def authed():
    """Both serve routes are cookie-gated: default every test to a signed-in
    member of the test agent. The access-matrix tests swap the override."""
    app.dependency_overrides[get_current_user] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    """Temp agents tree with a manager session (alice) registered for SID."""
    agent = "ui-agent"
    agents_root = tmp_path / "agents"
    (agents_root / agent / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / agent / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    # path_policy caches AGENTS_DIR at import — same convention as
    # test_images_hook / test_oauth_token_protection.
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    ctx = SecurityContext(role="manager", username="alice", agent=agent,
                          is_admin_agent=False)
    session_state.set_session_security(SID, ctx)
    yield agents_root / agent
    session_state._session_security.pop(SID, None)
    q = session_state.get_permission_queue(SID)
    while not q.empty():
        q.get_nowait()


def _post_ui(payload: dict) -> object:
    payload.setdefault("session_id", SID)
    with patch("api.hooks.artifacts.verify_session_match_async"):
        return client.post("/v1/hooks/ui", json=payload,
                           headers=_BEARER)


def _queue_items() -> list[dict]:
    q = session_state.get_permission_queue(SID)
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ───────────────────────── POST /v1/hooks/ui ────────────────────────────────


def test_hook_ui_validation(agent_tree):
    assert _post_ui({"html": "   "}).status_code == 400
    assert _post_ui({"html": "x" * (2 * 1024 * 1024 + 1)}).status_code == 400
    assert _post_ui({"html": "<p>ok</p>", "title": "t" * 201}).status_code == 400
    r = _post_ui({"html": "<p>ok</p>", "session_id": "sess-does-not-exist"})
    assert r.status_code == 400 and "unknown session" in r.json()["detail"]


def test_hook_ui_default_path_token_and_queue_payload(agent_tree):
    r = _post_ui({"html": "<p>hello</p>", "title": "Hello", "height": 420})
    assert r.status_code == 200
    body = r.json()
    # The hook owns the default (the MCP's OTO_WORKSPACE_DIR is satellite-
    # absolute on remote sessions): the CALLER'S scope workspace + title slug.
    assert body["path"].startswith("/users/alice/workspace/generated-ui/hello-")
    assert body["path"].endswith(".html")

    saved = agent_tree / body["path"].lstrip("/")
    assert saved.read_text() == "<p>hello</p>"  # raw content, no wrapping

    items = _queue_items()
    assert len(items) == 1
    item = items[0]
    token = item["token"]
    assert item == {
        "event_type": "ui", "token": token, "ui_url": f"/v1/ui/{token}",
        "title": "Hello", "height": 420,
        "path": body["path"].lstrip("/"),
    }
    assert body["ui_url"] == f"/v1/ui/{token}"

    info = task_store.get_media_token(token)
    assert info["media_kind"] == "ui" and info["mime"] == "text/html"
    assert not info["cache_owned"]  # chat delete must NOT unlink the workspace file
    assert info["abs_path"] == str(saved)


def test_hook_ui_viewer_workspace_lands_in_own_scope(agent_tree, monkeypatch):
    sid = "sess-ui-viewer"
    ctx = SecurityContext(role="viewer", username="bob", agent="ui-agent",
                          is_admin_agent=False)
    session_state.set_session_security(sid, ctx)
    try:
        r = _post_ui({"html": "<p>v</p>", "save_path": "/workspace/mine.html",
                      "session_id": sid})
        assert r.status_code == 200
        # Viewer's /workspace/ redirects to THEIR user workspace, never shared.
        assert r.json()["path"] == "/users/bob/workspace/mine.html"
        assert (agent_tree / "users" / "bob" / "workspace" / "mine.html").is_file()
        assert not (agent_tree / "workspace" / "mine.html").exists()
    finally:
        session_state._session_security.pop(sid, None)


def test_hook_ui_escaping_save_path_reanchors(agent_tree):
    r = _post_ui({"html": "<p>x</p>", "display": False,
                  "save_path": "/users/alice/../../../../etc/evil.html"})
    assert r.status_code == 200
    assert r.json()["path"] == "/users/alice/workspace/generated-ui/evil.html"
    assert (agent_tree / "users" / "alice" / "workspace" / "generated-ui"
            / "evil.html").is_file()


def test_hook_ui_other_users_dir_reanchors(agent_tree):
    # Cross-user writes are denied by the role matrix even for managers.
    r = _post_ui({"html": "<p>x</p>", "display": False,
                  "save_path": "/users/bob/workspace/theirs.html"})
    assert r.status_code == 200
    assert r.json()["path"] == "/users/alice/workspace/generated-ui/theirs.html"
    assert not (agent_tree / "users" / "bob" / "workspace" / "theirs.html").exists()


def test_hook_ui_relative_save_path_is_workspace_relative(agent_tree):
    r = _post_ui({"html": "<p>x</p>", "display": False,
                  "save_path": "reports/q3.html"})
    assert r.status_code == 200
    assert r.json()["path"] == "/users/alice/workspace/reports/q3.html"
    assert (agent_tree / "users" / "alice" / "workspace" / "reports"
            / "q3.html").is_file()


def test_hook_ui_forces_html_extension(agent_tree):
    r = _post_ui({"html": "<p>x</p>", "display": False,
                  "save_path": "/workspace/evil.py"})
    assert r.status_code == 200
    assert r.json()["path"] == "/workspace/evil.html"
    assert not (agent_tree / "workspace" / "evil.py").exists()


def test_hook_ui_display_false_saves_without_event(agent_tree):
    r = _post_ui({"html": "<p>quiet</p>", "display": False,
                  "save_path": "/workspace/generated-ui/quiet.html"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/workspace/generated-ui/quiet.html"
    assert "ui_url" not in body
    assert _queue_items() == []
    assert (agent_tree / "workspace" / "generated-ui" / "quiet.html").is_file()


def test_hook_ui_broadcasts_file_updated_for_update_in_place(agent_tree, monkeypatch):
    """EVERY write broadcasts file_updated (display AND save-only): /v1/ui
    serves the file at request time, so a same-save_path overwrite changes
    what already-rendered instances show — the broadcast makes them reload
    NOW. display=False + same save_path = the silent standing-artifact
    update (no new chat block)."""
    calls = []

    async def fake_broadcast(agent_slug, rel_path, **kw):
        calls.append((agent_slug, rel_path))

    monkeypatch.setattr(
        "services.notifications.notification_manager.broadcast_file_updated",
        fake_broadcast,
    )
    r = _post_ui({"html": "<p>trip v1</p>", "save_path": "/workspace/trip.html"})
    assert r.status_code == 200
    assert calls == [(AGENT, "workspace/trip.html")]
    calls.clear()
    r = _post_ui({"html": "<p>flight booked</p>", "display": False,
                  "save_path": "/workspace/trip.html"})
    assert r.status_code == 200
    assert calls == [(AGENT, "workspace/trip.html")]
    assert len(_queue_items()) == 1  # only the first post queued a chat block
    assert (agent_tree / "workspace" / "trip.html").read_text() == "<p>flight booked</p>"


# ───────────────────────── GET /v1/ui/{token} ───────────────────────────────


def _mint_ui_token(abs_path: str, token: str = "") -> str:
    import secrets as _secrets
    token = token or _secrets.token_urlsafe(32)
    task_store.create_media_token(
        token, abs_path, mime="text/html", media_kind="ui",
        chat_id=None, session_id=SID, machine_id=None,
        cache_owned=False, expires_at="",
    )
    return token


def _assert_sandbox_headers(resp):
    csp = resp.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts;")
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["content-type"].startswith("text/html")
    return csp


def test_serve_ui_fragment_wrapped_with_concrete_origin_csp(agent_tree, tmp_path):
    f = agent_tree / "workspace" / "frag.html"
    f.write_text("<div class='card'>chart here</div>")
    token = _mint_ui_token(str(f))
    resp = client.get(f"/v1/ui/{token}?theme=dark")
    assert resp.status_code == 200
    csp = _assert_sandbox_headers(resp)
    # The document runs at an OPAQUE origin where 'self' matches nothing —
    # the kit only loads because the CSP names the request's concrete origin.
    assert "script-src http://testserver 'unsafe-inline'" in csp
    assert "'self'" not in csp.replace("frame-ancestors 'self'", "")
    assert "connect-src 'none'" in csp
    body = resp.text
    assert body.startswith("<!doctype html>")
    assert "/ui-kit/otodock-tokens.css" in body
    assert "otodock-artifact" in body  # the injected runtime
    assert "<div class='card'>chart here</div>" in body


def test_csp_origin_prefers_public_url_on_matching_host(agent_tree, tmp_path, monkeypatch):
    """Behind cloudflared→nginx a dropped X-Forwarded-Proto pins an http://
    CSP under an https:// page — the browser then blocks every /ui-kit
    subresource and artifacts render UNSTYLED (live trusted-VM find,
    2026-07-10). When Host matches DASHBOARD_PUBLIC_URL, its origin wins
    verbatim; any other host keeps the request-derived origin."""
    f = agent_tree / "workspace" / "frag.html"
    f.write_text("<p>x</p>")
    token = _mint_ui_token(str(f))

    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://otodock.example.com")
    # Host matches the public URL, no X-Forwarded-Proto → https CSP anyway.
    resp = client.get(f"/v1/ui/{token}", headers={"host": "otodock.example.com"})
    assert "script-src https://otodock.example.com 'unsafe-inline'" in \
        resp.headers["Content-Security-Policy"]
    # LAN-IP access of the same install → request-derived origin (unchanged).
    resp = client.get(f"/v1/ui/{token}", headers={"host": "192.168.1.10:8400"})
    assert "script-src http://192.168.1.10:8400 'unsafe-inline'" in \
        resp.headers["Content-Security-Policy"]
    # X-Forwarded-Proto still honoured on non-matching hosts.
    resp = client.get(f"/v1/ui/{token}", headers={
        "host": "alt.example.com", "x-forwarded-proto": "https"})
    assert "script-src https://alt.example.com 'unsafe-inline'" in \
        resp.headers["Content-Security-Policy"]


def test_serve_ui_full_document_gets_runtime_injected(agent_tree, tmp_path):
    doc = "<!DOCTYPE html><html><head></head><body>standalone</body></html>"
    f = agent_tree / "workspace" / "full.html"
    f.write_text(doc)
    token = _mint_ui_token(str(f))
    resp = client.get(f"/v1/ui/{token}")
    assert resp.status_code == 200
    body = resp.text
    # Full documents keep their own markup and head (no tokens CSS — the
    # only splice is the BYTE-STATIC runtime before </body>): without it a
    # full-document artifact silently loses theme sync, auto-height,
    # actions, feeds and links.
    assert body.startswith("<!DOCTYPE html>")
    assert "standalone" in body
    assert "otodock-artifact" in body  # the injected runtime
    assert "/ui-kit/otodock-tokens.css" not in body
    assert body.rstrip().endswith("</body></html>")
    assert body.index("otodock-artifact") < body.index("</body>")
    _assert_sandbox_headers(resp)


def test_inject_runtime_placement():
    from api.media.ui import UI_RUNTIME, inject_runtime
    out = inject_runtime("<html><body><p>a</p></body></html>")
    assert out.index(UI_RUNTIME) < out.index("</body>")
    # An author who omitted </body> still gets an executing trailing script.
    assert inject_runtime("<html><p>a</p></html>").endswith(UI_RUNTIME)


def test_serve_ui_runtime_carries_open_url_bridge(agent_tree, tmp_path):
    # The click bridge gates on the AUTHORED href attribute (a.href is
    # DOM-absolutized) and posts open_url up; the host answers open_url_ack.
    f = agent_tree / "workspace" / "frag.html"
    f.write_text("<a href='https://example.com'>x</a>")
    token = _mint_ui_token(str(f))
    body = client.get(f"/v1/ui/{token}").text
    assert "type:'open_url'" in body
    assert "open_url_ack" in body
    assert "getAttribute('href')" in body
    # In-page anchors and non-http schemes are left alone; PATH hrefs are
    # intercepted only to CANCEL them (a relative navigation can't load
    # under this CSP and replaced the artifact with the browser's
    # "refused to connect" page — verified live on T1 2026-07-26).
    assert "raw.charAt(0) === '#'" in body
    assert r"/^[a-z][a-z0-9+.\-]*:/i" in body


def test_serve_ui_unknown_token_is_sandboxed_404(agent_tree):
    resp = client.get("/v1/ui/no-such-token")
    assert resp.status_code == 404
    _assert_sandbox_headers(resp)  # the placeholder MUST stay sandboxed too


def test_serve_ui_missing_file_is_styled_escaped_404(agent_tree, tmp_path):
    gone = agent_tree / "workspace" / "<img src=x onerror=alert(1)>.html"
    token = _mint_ui_token(str(gone))
    resp = client.get(f"/v1/ui/{token}")
    assert resp.status_code == 404
    _assert_sandbox_headers(resp)
    assert "&lt;img src=x onerror=alert(1)&gt;" in resp.text
    assert "<img src=x" not in resp.text


def test_serve_ui_rejects_non_ui_tokens(agent_tree, tmp_path):
    import secrets as _secrets
    f = agent_tree / "workspace" / "clip.mp4"
    f.write_bytes(b"fake-mp4")
    token = _secrets.token_urlsafe(32)
    task_store.create_media_token(
        token, str(f), mime="video/mp4", media_kind="video",
        chat_id=None, session_id=SID, machine_id=None,
        cache_owned=False, expires_at="",
    )
    resp = client.get(f"/v1/ui/{token}")
    assert resp.status_code == 404
    _assert_sandbox_headers(resp)


def test_full_document_detector():
    from api.media.ui import is_full_document
    assert is_full_document("<!doctype html><html></html>")
    assert is_full_document("  \n<!DOCTYPE HTML>")
    assert is_full_document("<html lang='en'>")
    assert is_full_document("<!-- note --> <!-- more -->\n<html>")
    assert not is_full_document("<div>fragment</div>")
    assert not is_full_document("<!-- unterminated comment <html>")
    assert not is_full_document("hello <html> later")


# ───────────────────── serve_media hardening (allowlist) ────────────────────


def test_serve_media_rejects_ui_tokens(agent_tree, tmp_path):
    f = agent_tree / "workspace" / "artifact.html"
    f.write_text("<script>alert(1)</script>")
    token = _mint_ui_token(str(f))
    resp = client.get(f"/v1/media/{token}")
    assert resp.status_code == 404


def _mint_media_token(abs_path: str, mime: str, media_kind: str = "") -> str:
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    task_store.create_media_token(
        token, abs_path, mime=mime, media_kind=media_kind,
        chat_id=None, session_id=SID, machine_id=None,
        cache_owned=False, expires_at="",
    )
    return token


def test_serve_media_inline_allowlist(agent_tree):
    # text/html (any non-ui kind) → attachment, never inline same-origin.
    page = agent_tree / "workspace" / "page.html"
    page.write_text("<script>alert(1)</script>")
    resp = client.get(f"/v1/media/{_mint_media_token(str(page), 'text/html')}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")

    # SVG keeps its forced-download behavior under the allowlist.
    svg = agent_tree / "workspace" / "img.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    resp = client.get(f"/v1/media/{_mint_media_token(str(svg), 'image/svg+xml')}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")

    # Known-inert types still serve inline (no disposition header).
    mp4 = agent_tree / "workspace" / "clip.mp4"
    mp4.write_bytes(b"fake-mp4")
    resp = client.get(f"/v1/media/{_mint_media_token(str(mp4), 'video/mp4', 'video')}")
    assert resp.status_code == 200
    assert "content-disposition" not in resp.headers


# ──────────────── cookie gate + per-token access rule ───────────────────────


def test_serve_routes_require_auth(agent_tree, tmp_path):
    f = agent_tree / "workspace" / "frag.html"
    f.write_text("<p>x</p>")
    ui_token = _mint_ui_token(str(f))
    mp4 = agent_tree / "workspace" / "clip.mp4"
    mp4.write_bytes(b"fake-mp4")
    media_token = _mint_media_token(str(mp4), "video/mp4", "video")

    _as(None)  # signed out / a leaked link opened outside the platform
    resp = client.get(f"/v1/ui/{ui_token}")
    assert resp.status_code == 401
    assert "Sign in" in resp.text
    _assert_sandbox_headers(resp)  # the 401 placeholder is STILL sandboxed
    assert client.get(f"/v1/media/{media_token}").status_code == 401


def test_hook_minted_token_requires_agent_access(agent_tree):
    # Hook mints stamp the session's agent; chatless rows (task runs render
    # in agent History) are gated on agent access — same audience.
    r = _post_ui({"html": "<p>gated</p>"})
    assert r.status_code == 200
    token = _queue_items()[-1]["token"]

    assert client.get(f"/v1/ui/{token}").status_code == 200  # assigned member
    _as(_user(sub="user-viewer2", agents=()))  # signed in, different agents
    resp = client.get(f"/v1/ui/{token}")
    assert resp.status_code == 404  # uniform with missing — no liveness oracle
    _assert_sandbox_headers(resp)
    _as(_user(sub="user-admin", role="admin", agents=()))
    assert client.get(f"/v1/ui/{token}").status_code == 200


def _mint_chat_bound_ui_token(abs_path: str, chat_id: str) -> str:
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    task_store.create_media_token(
        token, abs_path, mime="text/html", media_kind="ui",
        chat_id=chat_id, session_id=SID, agent=AGENT,
    )
    return token


def test_chat_bound_token_uses_chat_access_rule(agent_tree, tmp_path):
    task_store.create_chat("chat-ui-acl", "user-viewer", AGENT)
    f = agent_tree / "workspace" / "c.html"
    f.write_text("<p>c</p>")
    token = _mint_chat_bound_ui_token(str(f), "chat-ui-acl")

    assert client.get(f"/v1/ui/{token}").status_code == 200  # chat owner
    _as(_user(sub="user-viewer2"))  # same agent, someone else's per-user chat
    assert client.get(f"/v1/ui/{token}").status_code == 404
    _as(_user(sub="user-admin", role="admin", agents=()))
    assert client.get(f"/v1/ui/{token}").status_code == 200  # admin


def test_shared_only_chat_token_serves_any_assigned_user(agent_tree, tmp_path):
    from core.session.visibility import SHARED_CHAT_OWNER_PREFIX
    task_store.create_chat("chat-ui-shared", f"{SHARED_CHAT_OWNER_PREFIX}{AGENT}", AGENT)
    f = agent_tree / "workspace" / "s.html"
    f.write_text("<p>s</p>")
    token = _mint_chat_bound_ui_token(str(f), "chat-ui-shared")

    _as(_user(sub="user-viewer2", agents=(AGENT,)))  # any assigned user
    assert client.get(f"/v1/ui/{token}").status_code == 200
    _as(_user(sub="user-viewer2", agents=()))  # not assigned
    assert client.get(f"/v1/ui/{token}").status_code == 404


def test_pre_stamp_rows_fall_back_to_any_authed_user(agent_tree, tmp_path):
    # _mint_ui_token writes neither chat_id nor agent — the shape of rows
    # minted before the access columns existed (restore-friendly, no backfill).
    f = agent_tree / "workspace" / "legacy.html"
    f.write_text("<p>old</p>")
    token = _mint_ui_token(str(f))
    _as(_user(sub="user-viewer2", agents=()))
    assert client.get(f"/v1/ui/{token}").status_code == 200


# ───────────── durable send_file / document-preview download tokens ─────────


def test_hook_file_mints_durable_media_token(agent_tree):
    # Dashboard sessions get a durable media_tokens download URL (the old
    # in-memory token died after 1h / any proxy restart).
    from adapters import register_adapter
    from adapters.dashboard import DashboardAdapter
    register_adapter(DashboardAdapter())  # app startup doesn't run under TestClient
    session_state._record_session_use(SID, client_type="dashboard", agent=AGENT)
    try:
        report = agent_tree / "users" / "alice" / "workspace" / "report.txt"
        report.write_text("hello")
        with patch("api.hooks.artifacts.verify_session_match_async"):
            r = client.post("/v1/hooks/file", json={
                "session_id": SID, "path": str(report), "description": "d",
            }, headers=_BEARER)
        assert r.status_code == 200
        url = r.json()["download_url"]
        assert url.startswith("/v1/media/")
        # No fn= server-side — the file components append it client-side.
        assert url.endswith("?download=1")

        token = url.removeprefix("/v1/media/").split("?")[0]
        info = task_store.get_media_token(token)
        assert info and info["media_kind"] == "file"
        assert info["agent"] == AGENT and info["expires_at"] == ""

        resp = client.get(url + "&fn=report.txt")  # as the frontend builds it
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith("attachment")
        assert "report.txt" in resp.headers["content-disposition"]
        _as(_user(sub="user-viewer2", agents=()))
        assert client.get(url).status_code == 404
    finally:
        session_state._sessions.pop(SID, None)


def test_file_token_dies_with_its_chat(agent_tree, tmp_path):
    task_store.create_chat("chat-file-life", "user-viewer", AGENT)
    doc = agent_tree / "workspace" / "r.txt"
    doc.write_text("x")
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    task_store.create_media_token(
        token, str(doc), media_kind="file",
        chat_id="chat-file-life", session_id=SID, agent=AGENT,
    )
    assert client.get(f"/v1/media/{token}?download=1&fn=r.txt").status_code == 200
    task_store.delete_chat("chat-file-life")
    assert client.get(f"/v1/media/{token}?download=1&fn=r.txt").status_code == 404


# ─────────────── backchannel primitives (ws/artifact_interactions) ──────────


def test_frame_text_is_fence_safe_and_provenance_marked():
    from ws.artifact_interactions import frame_text
    # A payload full of fences must not escape the framed code block.
    text = frame_text([{
        "token": "t", "title": 'Evil "quote" chart',
        "payload": {"x": "```</script>"},
        "payload_json": '{"x":"```</script>"}',
    }])
    assert '[interaction from artifact "Evil \'quote\' chart"]' in text
    assert "```json" in text
    assert "not the user typing" in text
    # The payload's own backtick run is broken.
    body = text.split("```json", 1)[1]
    assert "```" not in body.split("\n```", 1)[0].replace("`​``", "")


def test_check_rate_min_interval_and_window():
    from ws import artifact_interactions as ai
    ai._rate.clear()
    try:
        assert ai.check_rate("c", "t") is True
        assert ai.check_rate("c", "t") is False  # < 1s apart
        assert ai.check_rate("c", "OTHER") is True  # per-token keying
    finally:
        ai._rate.clear()


def test_pump_artifact_queue_cap_and_abort_clear():
    from ws.artifact_interactions import QUEUE_CAP
    from core.events.stream_pump import ChatStreamPump
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    try:
        producer = loop.create_task(_asyncio.sleep(3600))
        p = ChatStreamPump(chat_id="c1", session_id="s1", producer=producer,
                           event_queue=_asyncio.Queue(), perm_queue=None)
        for i in range(QUEUE_CAP):
            assert p.queue_artifact({"token": f"t{i}"}) is True
        assert p.queue_artifact({"token": "overflow"}) is False
        from core.events.common_events import TurnInput
        p.queue_message(TurnInput("user words"))
        p.cancel_all_queued()
        assert p.artifact_queue == [] and p.message_queue == []
        producer.cancel()
    finally:
        loop.close()


def test_serve_ui_runtime_carries_action_ack_bridge(agent_tree, tmp_path):
    f = agent_tree / "workspace" / "frag.html"
    f.write_text("<p>x</p>")
    token = _mint_ui_token(str(f))
    body = client.get(f"/v1/ui/{token}").text
    assert "action_ack" in body
    assert "otodock:action-ack" in body
    assert "otodock = { send:" in body.replace("window.", "")


# ───────────────────────── pump persistence ─────────────────────────────────


def _mk_pump(chat_id: str):
    from core.events.stream_pump import ChatStreamPump
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
        event_queue=asyncio.Queue(), perm_queue=None,
    )


@pytest.mark.asyncio
async def test_pump_persists_ui_block_and_roundtrips_fields(temp_db):
    temp_db.create_chat("ui-chat-1", "user-admin", "a1")
    pump = _mk_pump("ui-chat-1")
    try:
        q = pump.attach()
        await pump._handle_perm_event({
            "event_type": "ui", "token": "tok-1", "ui_url": "/v1/ui/tok-1",
            "title": "Tips", "height": 380, "path": "workspace/generated-ui/t.html",
        })
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
        events = [f["event"] for f in frames if f.get("pump_type") == "ws_event"]
        assert events and events[-1]["type"] == "ui"
        assert events[-1]["token"] == "tok-1" and events[-1]["height"] == 380

        blk = pump._turn_blocks[-1]
        assert blk["type"] == "ui" and blk["ui_url"] == "/v1/ui/tok-1"

        await pump._save_turn_blocks()  # an off-loop writer job
        rows = [m for m in task_store.get_chat_messages("ui-chat-1")
                if m.get("event_type") == "ui"]
        assert len(rows) == 1
        stored = json.loads(rows[0]["event_data"])
        # Reload renders from this row — every field must round-trip.
        assert stored == {"type": "ui", "token": "tok-1", "ui_url": "/v1/ui/tok-1",
                          "title": "Tips", "height": 380,
                          "path": "workspace/generated-ui/t.html"}
    finally:
        pump.producer.cancel()


# ───────────── serve from the checked descriptor (safe_fs) ─────────────


def _outside_secret(tmp_path) -> Path:
    secret = Path(tmp_path) / "config.env"
    secret.write_text("JWT_SECRET=proxy-host-secret\n")
    return secret


def test_serve_media_refuses_swapped_symlink(agent_tree, tmp_path):
    # A real web-safe file is minted, then swapped for a link after the mint:
    # out of the agents tree first, then to another user's file in the tree.
    clip = agent_tree / "users" / "alice" / "workspace" / "a.mp3"
    clip.write_bytes(b"ID3real-mp3")
    token = _mint_media_token(str(clip), "audio/mpeg", "audio")
    assert client.get(f"/v1/media/{token}").content == b"ID3real-mp3"

    secret = _outside_secret(tmp_path)
    clip.unlink()
    clip.symlink_to(secret)
    resp = client.get(f"/v1/media/{token}")
    assert resp.status_code == 404
    assert b"JWT_SECRET" not in resp.content

    bob = agent_tree / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    (bob / "private.mp3").write_bytes(b"BOB PRIVATE")
    clip.unlink()
    clip.symlink_to(bob / "private.mp3")
    resp = client.get(f"/v1/media/{token}")
    assert resp.status_code == 404
    assert b"BOB PRIVATE" not in resp.content
    # The download form takes the same path.
    resp = client.get(f"/v1/media/{token}?download=1&fn=a.mp3")
    assert resp.status_code == 404


def test_serve_media_binds_agent_segment(agent_tree, tmp_path):
    # A row stamped for one agent never serves a path under another agent's
    # tree, however it got there.
    other = config.AGENTS_DIR / "other-agent" / "workspace"
    other.mkdir(parents=True)
    (other / "clip.mp3").write_bytes(b"OTHER")
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    task_store.create_media_token(
        token, str(other / "clip.mp3"), mime="audio/mpeg", media_kind="audio",
        chat_id=None, session_id=SID, agent=AGENT, expires_at="",
    )
    resp = client.get(f"/v1/media/{token}")
    assert resp.status_code == 404
    assert resp.content != b"OTHER"


def test_serve_media_admits_the_caches(agent_tree, tmp_path, monkeypatch):
    # Transcodes live in the media cache and re-pulls in the host media
    # cache; both are proxy-owned roots and keep serving.
    from services.media import media_pipeline
    media_cache = tmp_path / "media-cache"
    host_cache = tmp_path / "host-media-cache"
    media_cache.mkdir()
    host_cache.mkdir()
    monkeypatch.setattr(media_pipeline, "_CACHE_DIR", media_cache)
    monkeypatch.setattr(media_pipeline, "_HOST_CACHE_DIR", host_cache)
    (media_cache / "abc.mp4").write_bytes(b"TRANSCODE")
    (host_cache / "repull-x.mp4").write_bytes(b"REPULL")
    t1 = _mint_media_token(str(media_cache / "abc.mp4"), "video/mp4", "video")
    t2 = _mint_media_token(str(host_cache / "repull-x.mp4"), "video/mp4", "video")
    assert client.get(f"/v1/media/{t1}").content == b"TRANSCODE"
    assert client.get(f"/v1/media/{t2}").content == b"REPULL"
    # A path under none of the roots is refused.
    stray = tmp_path / "stray.mp4"
    stray.write_bytes(b"STRAY")
    t3 = _mint_media_token(str(stray), "video/mp4", "video")
    assert client.get(f"/v1/media/{t3}").status_code == 404


def test_fd_response_serves_the_checked_inode_with_ranges(agent_tree, tmp_path):
    # The response streams the inode the check saw: a swap of the name after
    # the open changes nothing, and Range / HEAD keep working on it.
    import os
    from api.media.media import FdFileResponse
    from services.infra import safe_fs
    clip = agent_tree / "workspace" / "clip.mp3"
    clip.write_bytes(bytes(range(256)) * 4)
    fd, st = safe_fs.open_regular_for_read(config.AGENTS_DIR, f"{AGENT}/workspace/clip.mp3")
    clip.unlink()
    clip.symlink_to(_outside_secret(tmp_path))

    async def _run(headers: dict, method: str = "GET"):
        import httpx
        resp = FdFileResponse(fd, st, media_type="audio/mpeg")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=resp),
                                     base_url="http://t") as c:
            return await c.request(method, "/", headers=headers)

    r = asyncio.run(_run({"range": "bytes=256-259"}))
    assert r.status_code == 206 and r.content == bytes([0, 1, 2, 3])
    assert r.headers["content-range"] == "bytes 256-259/1024"
    # The descriptor is closed once the response has been sent.
    with pytest.raises(OSError):
        os.fstat(fd)
    # HEAD on a descriptor whose name was removed after the open.
    real = agent_tree / "workspace" / "real.mp3"
    real.write_bytes(bytes(range(256)) * 4)
    fd, st = safe_fs.open_regular_for_read(config.AGENTS_DIR, f"{AGENT}/workspace/real.mp3")
    real.unlink()
    r = asyncio.run(_run({}, "HEAD"))
    assert r.status_code == 200 and r.headers["content-length"] == "1024" and r.content == b""


def test_serve_media_never_leaks_a_descriptor(agent_tree, monkeypatch):
    import os
    from services.media import media_pipeline
    clip = agent_tree / "workspace" / "clip.mp3"
    clip.write_bytes(b"ID3x")
    token = _mint_media_token(str(clip), "", "audio")  # mime guessed after the open

    def _boom(path):
        raise RuntimeError("mime guess failed")

    monkeypatch.setattr(media_pipeline, "guess_media_mime", _boom)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(RuntimeError):
        client.get(f"/v1/media/{token}")
    assert len(os.listdir("/proc/self/fd")) == before


def test_serve_ui_refuses_swapped_symlink(agent_tree, tmp_path):
    f = agent_tree / "workspace" / "art.html"
    f.write_text("<p>real</p>")
    token = _mint_ui_token(str(f))
    assert "real" in client.get(f"/v1/ui/{token}").text
    f.unlink()
    f.symlink_to(_outside_secret(tmp_path))
    resp = client.get(f"/v1/ui/{token}")
    assert resp.status_code == 404
    assert "JWT_SECRET" not in resp.text
    _assert_sandbox_headers(resp)
