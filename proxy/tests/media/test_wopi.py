"""Tests for Collabora WOPI propagation + role-gating (api/media/wopi.py).

Covers:
- ``generate_wopi_url`` role-clamp: the client ``edit`` bool is gated
  server-side by ``can_write_back(file_path, role, username)`` — a viewer cannot
  mint an edit token for a shared workspace file but CAN for their own
  ``users/{u}/`` dir; editor/manager/admin get edit on the shared workspace.
- ``wopi_put_file``: persists + propagates via
  ``workspace_fanout.propagate_write`` with the agent-tree path derived from the
  token, and broadcasts ``file_updated`` (source="collabora"); view tokens 403.
- ``wopi_check_file_info``: ``HideUserList`` / ``DisableInactiveMessages``
  are ``"false"`` (co-edit presence) and ``PostMessageOrigin`` is set.
"""

import base64
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _encode_file_id(rel: str) -> str:
    return base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")


def _wopi_config(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(config, "WOPI_SECRET", "test-wopi-secret", raising=False)
    monkeypatch.setattr(config, "COLLABORA_URL", "https://collabora.example", raising=False)
    monkeypatch.setattr(config, "WOPI_BASE_URL", "https://wopi.example", raising=False)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://app.example", raising=False)


# ---------------------------------------------------------------------------
# generate_wopi_url — role-clamp
# ---------------------------------------------------------------------------


def _make_url_app(monkeypatch, tmp_path, *, role, username, is_admin=False,
                  is_api_key=False, sub=None):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from auth.providers import UserContext, get_current_user
    from storage import database as db

    monkeypatch.setattr(db, "get_username_by_sub", lambda s: username)
    user = UserContext(
        sub=sub or f"{username}-sub", email=f"{username}@t.com", name=username.title(),
        role="admin" if is_admin else "creator",
        agents=["test-agent"], agent_roles={"test-agent": role},
        is_api_key=is_api_key,
    )

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(wopi.router)
    app.dependency_overrides[get_current_user] = _stub
    return app


def _seed_file(tmp_path, rel, content=b"doc"):
    p = tmp_path / "test-agent" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _ask_url(app, file_path, edit=True):
    client = TestClient(app)
    return client.post(
        "/v1/documents/wopi-url",
        json={"file_path": file_path, "agent": "test-agent", "edit": edit},
    )


def test_wopiurl_viewer_workspace_clamped_to_view(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="viewer", username="vic")
    resp = _ask_url(app, "workspace/x.docx", edit=True)
    assert resp.status_code == 200
    assert resp.json()["permissions"] == "view"  # viewer can't write shared workspace


def test_wopiurl_viewer_own_userdir_gets_edit(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "users/vic/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="viewer", username="vic")
    resp = _ask_url(app, "users/vic/x.docx", edit=True)
    assert resp.json()["permissions"] == "edit"  # own user dir, any role


def test_wopiurl_viewer_other_userdir_denied(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "users/alice/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="viewer", username="vic")
    resp = _ask_url(app, "users/alice/x.docx", edit=True)
    # A viewer cannot mint ANY token (not even view) for another user's dir —
    # cross-user read is denied at the read-scope gate.
    assert resp.status_code == 403


def test_wopiurl_editor_workspace_gets_edit(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="editor", username="ed")
    assert _ask_url(app, "workspace/x.docx", edit=True).json()["permissions"] == "edit"


def test_wopiurl_manager_workspace_gets_edit(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="manager", username="mgr")
    assert _ask_url(app, "workspace/x.docx", edit=True).json()["permissions"] == "edit"


def test_wopiurl_admin_workspace_gets_edit(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="admin", username="adm", is_admin=True)
    assert _ask_url(app, "workspace/x.docx", edit=True).json()["permissions"] == "edit"


def test_wopiurl_edit_false_is_view(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="manager", username="mgr")
    assert _ask_url(app, "workspace/x.docx", edit=False).json()["permissions"] == "view"


def test_wopiurl_masterkey_bypasses_roleclamp(temp_db, tmp_path, monkeypatch):
    # The trusted master key (sub="api-key" → SERVICE, acting_sub None) bypasses
    # the per-agent role clamp; edit is honored without a role check.
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(
        monkeypatch, tmp_path, role="viewer", username="svc",
        is_api_key=True, sub="api-key",
    )
    assert _ask_url(app, "workspace/x.docx", edit=True).json()["permissions"] == "edit"


def test_wopiurl_user_session_viewer_clamped(temp_db, tmp_path, monkeypatch):
    # A real-user session token (is_api_key + a real sub = USER_SESSION) is NO
    # longer trusted to bypass — a viewer is clamped to view-only.
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(
        monkeypatch, tmp_path, role="viewer", username="svc", is_api_key=True,
    )
    assert _ask_url(app, "workspace/x.docx", edit=True).json()["permissions"] == "view"


# ---------------------------------------------------------------------------
# wopi_put_file — persist + propagate + broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_file_propagates_and_broadcasts(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from services.notifications import notification_manager
    from services.remote import workspace_fanout

    pw = AsyncMock()
    bc = AsyncMock()
    monkeypatch.setattr(workspace_fanout, "propagate_write", pw)
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", bc)

    rel = "test-agent/workspace/x.docx"
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "edit", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app).post(
        f"/wopi/files/{file_id}/contents?access_token={token}", content=b"new bytes",
    )
    assert resp.status_code == 200

    pw.assert_awaited_once()
    assert pw.await_args.args[:3] == ("test-agent", "workspace/x.docx", b"new bytes")
    assert pw.await_args.kwargs.get("exclude_machine_id") is None

    bc.assert_awaited_once()
    assert bc.await_args.args[:2] == ("test-agent", "workspace/x.docx")
    assert bc.await_args.kwargs.get("source") == "collabora"
    assert bc.await_args.kwargs.get("exclude_user_sub") == "user-bob-sub"


@pytest.mark.asyncio
async def test_put_file_view_token_rejected(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from services.remote import workspace_fanout

    pw = AsyncMock()
    monkeypatch.setattr(workspace_fanout, "propagate_write", pw)

    rel = "test-agent/workspace/x.docx"
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "view", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app).post(
        f"/wopi/files/{file_id}/contents?access_token={token}", content=b"x",
    )
    assert resp.status_code == 403
    pw.assert_not_awaited()


def test_validate_rejects_purposeless_jwt(temp_db, tmp_path, monkeypatch):
    # WOPI_SECRET defaults to JWT_SECRET, so a non-WOPI platform JWT with a
    # coincidentally-fitting claim shape must NOT validate — only tokens
    # minted with the "wopi" purpose discriminator pass.
    _wopi_config(monkeypatch, tmp_path)
    import time as _time

    import config
    import jwt as _jwt
    from api.media import wopi

    rel = "test-agent/workspace/x.docx"
    forged = _jwt.encode(
        {
            "file_path": rel, "user_sub": "u", "user_name": "U",
            "permissions": "edit", "agent": "test-agent",
            "iat": int(_time.time()), "exp": int(_time.time()) + 60,
        },
        config.WOPI_SECRET, algorithm="HS256",
    )
    assert wopi.validate_wopi_token(forged) is None
    minted, _ = wopi.create_wopi_token(rel, "u", "U", "edit", "test-agent")
    assert wopi.validate_wopi_token(minted) is not None


# ---------------------------------------------------------------------------
# wopi_check_file_info — co-edit presence
# ---------------------------------------------------------------------------


def test_check_file_info_presence_fields(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi

    rel = "test-agent/workspace/x.docx"
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"hello")
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "edit", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app).get(f"/wopi/files/{file_id}?access_token={token}")
    assert resp.status_code == 200
    j = resp.json()
    assert j["HideUserList"] == "false"
    assert j["DisableInactiveMessages"] == "false"
    assert j["PostMessageOrigin"] == "https://app.example"
    assert j["UserCanWrite"] is True


# ---------------------------------------------------------------------------
# wopi_put_file — host-cache docs push back to the origin machine (2026-07-19)
# ---------------------------------------------------------------------------


def _seed_host_cache(tmp_path, session_id="sess-1", digest="abc123",
                     name="x.docx", content=b"old bytes"):
    d = tmp_path / ".remote-host-cache" / session_id / digest
    d.mkdir(parents=True)
    (d / "_meta.json").write_text(
        '{"machine_id": "m-1", "abs_path": "C:/Users/u/Desktop/x.docx"}'
    )
    p = d / name
    p.write_bytes(content)
    return p, f".remote-host-cache/{session_id}/{digest}/{name}"


@pytest.mark.asyncio
async def test_put_file_host_cache_pushes_back(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout

    pw = AsyncMock()
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(workspace_fanout, "propagate_write", pw)
    monkeypatch.setattr(remote_file_flow, "push_back_host_path", push)

    cache_file, rel = _seed_host_cache(tmp_path)
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "edit", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app).post(
        f"/wopi/files/{file_id}/contents?access_token={token}",
        content=b"edited bytes",
    )
    assert resp.status_code == 200
    assert cache_file.read_bytes() == b"edited bytes"
    push.assert_awaited_once_with("sess-1", str(cache_file))
    # Host files have no agent-tree fan-out.
    pw.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_file_host_cache_offline_fails_and_restores(
    temp_db, tmp_path, monkeypatch,
):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from core.remote import remote_file_flow

    push = AsyncMock(return_value=False)
    monkeypatch.setattr(remote_file_flow, "push_back_host_path", push)

    cache_file, rel = _seed_host_cache(tmp_path)
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "edit", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app).post(
        f"/wopi/files/{file_id}/contents?access_token={token}",
        content=b"edited bytes",
    )
    # Save FAILS loudly and the cache keeps mirroring the machine (old bytes) —
    # a diverged cache copy must never be served as truth by later reads.
    assert resp.status_code == 500
    assert cache_file.read_bytes() == b"old bytes"


@pytest.mark.asyncio
async def test_put_file_host_cache_view_token_still_403(
    temp_db, tmp_path, monkeypatch,
):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from core.remote import remote_file_flow

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(remote_file_flow, "push_back_host_path", push)

    _cache_file, rel = _seed_host_cache(tmp_path)
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "view", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app).post(
        f"/wopi/files/{file_id}/contents?access_token={token}", content=b"x",
    )
    assert resp.status_code == 403
    push.assert_not_awaited()


# ---------------------------------------------------------------------------
# generate_wopi_url — the request's agent AND file_path are confined
# ---------------------------------------------------------------------------


def test_wopiurl_agent_segment_cannot_leave_agents_tree(temp_db, tmp_path, monkeypatch):
    """``can_access_agent`` says yes to ANY name for an admin, so the agent
    field is confined to the agents tree before the file path is confined to
    the agent: a traversal agent answers 403, never a token."""
    import config
    app = _make_url_app(monkeypatch, tmp_path, role="admin", username="root", is_admin=True)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    stray = tmp_path / "stray" / "workspace" / "doc.docx"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"doc")
    r = TestClient(app).post(
        "/v1/documents/wopi-url",
        json={"file_path": "workspace/doc.docx", "agent": "../stray", "edit": False},
    )
    assert r.status_code == 403


def test_wopiurl_symlink_out_of_agent_is_403(temp_db, tmp_path, monkeypatch):
    import config
    app = _make_url_app(monkeypatch, tmp_path, role="manager", username="alice")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    outside = tmp_path / "outside-doc.docx"
    outside.write_bytes(b"doc")
    ws = tmp_path / "agents" / "test-agent" / "workspace"
    ws.mkdir(parents=True)
    os.symlink(outside, ws / "link.docx")
    assert _ask_url(app, "workspace/link.docx").status_code == 403


def test_wopiurl_contributor_workspace_gets_edit(temp_db, tmp_path, monkeypatch):
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="contributor", username="con")
    assert _ask_url(app, "workspace/x.docx", edit=True).json()["permissions"] == "edit"


# ---------------------------------------------------------------------------
# GetFile / CheckFileInfo serve the checked descriptor
# ---------------------------------------------------------------------------


def _serve_client():
    from api.media import wopi
    app = FastAPI()
    app.include_router(wopi.router)
    return TestClient(app)


def test_wopi_get_file_refuses_post_mint_symlink_swap(temp_db, tmp_path, monkeypatch):
    """A view token for the PM's own file keeps serving the PM's file only:
    swapped for a link into another project's credentials, into another
    user's folder of the same project, or out of the agents tree, both
    GetFile and CheckFileInfo answer 404 and leak nothing."""
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    doc = _seed_file(tmp_path, "users/pm/workspace/r.docx", b"PK real docx")
    rel = "test-agent/users/pm/workspace/r.docx"
    token, _ = wopi.create_wopi_token(rel, "pm-sub", "PM", "view", "test-agent")
    file_id = _encode_file_id(rel)
    client = _serve_client()
    assert client.get(f"/wopi/files/{file_id}/contents",
                      params={"access_token": token}).content == b"PK real docx"

    victim = tmp_path / "proj-b" / "knowledge" / ".credentials" / "google-tokens" / "t.json"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b'{"refresh_token":"SECRET"}')
    bob = tmp_path / "test-agent" / "users" / "bob" / "workspace" / "private.docx"
    bob.parent.mkdir(parents=True)
    bob.write_bytes(b"BOB SECRET")
    outside = tmp_path.parent / f"{tmp_path.name}-config.env"
    outside.write_text("JWT_SECRET=SECRET\n")
    for target in (victim, bob, outside):
        doc.unlink()
        doc.symlink_to(os.path.relpath(target, doc.parent))
        r = client.get(f"/wopi/files/{file_id}/contents", params={"access_token": token})
        assert r.status_code == 404, target
        assert b"SECRET" not in r.content
        info = client.get(f"/wopi/files/{file_id}", params={"access_token": token})
        assert info.status_code == 404, target
        assert b"SECRET" not in info.content and b"Size" not in info.content
    outside.unlink()
    # A directory link in the middle of the path is refused too.
    doc.unlink()
    doc.write_bytes(b"PK real docx")
    ws = doc.parent
    real_ws = ws.with_name("real-ws")
    ws.rename(real_ws)
    ws.symlink_to("real-ws")
    assert client.get(f"/wopi/files/{file_id}/contents",
                      params={"access_token": token}).status_code == 404


def test_wopi_check_file_info_and_get_file_from_the_descriptor(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    _seed_file(tmp_path, "workspace/x.docx", b"hello world")
    rel = "test-agent/workspace/x.docx"
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "view", "test-agent")
    file_id = _encode_file_id(rel)
    client = _serve_client()
    j = client.get(f"/wopi/files/{file_id}", params={"access_token": token}).json()
    assert j["BaseFileName"] == "x.docx" and j["Size"] == 11 and j["UserCanWrite"] is False
    r = client.get(f"/wopi/files/{file_id}/contents", params={"access_token": token})
    assert r.status_code == 200 and r.content == b"hello world"
    assert r.headers["content-type"] == "application/octet-stream"
    assert "x-wopi-itemversion" in r.headers
    # Range requests (Collabora fetches ranges of large documents).
    r = client.get(f"/wopi/files/{file_id}/contents", params={"access_token": token},
                   headers={"range": "bytes=0-4"})
    assert r.status_code == 206 and r.content == b"hello"


def test_wopi_agent_claim_may_differ_from_path(temp_db, tmp_path, monkeypatch):
    """A meeting participant's preview is re-minted with the parent chat's
    agent; the signed path is the binding, so it still serves."""
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    _seed_file(tmp_path, "workspace/p.docx", b"participant doc")
    rel = "test-agent/workspace/p.docx"
    token, _ = wopi.create_wopi_token(rel, "u", "U", "view", "parent-agent")
    file_id = _encode_file_id(rel)
    r = _serve_client().get(f"/wopi/files/{file_id}/contents", params={"access_token": token})
    assert r.status_code == 200 and r.content == b"participant doc"


def test_wopi_first_segment_must_be_an_agent_name(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    stray = tmp_path / ".hidden-dir" / "x.docx"
    stray.parent.mkdir()
    stray.write_bytes(b"stray")
    rel = ".hidden-dir/x.docx"
    token, _ = wopi.create_wopi_token(rel, "u", "U", "view", "test-agent")
    file_id = _encode_file_id(rel)
    client = _serve_client()
    assert client.get(f"/wopi/files/{file_id}/contents",
                      params={"access_token": token}).status_code == 404
    assert client.get(f"/wopi/files/{file_id}", params={"access_token": token}).status_code == 404


def test_wopi_snapshot_and_host_cache_still_serve(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    import config
    from api.media import wopi
    client = _serve_client()
    # A lazy-pull host cache document (a remote-machine preview).
    cache_file, rel = _seed_host_cache(tmp_path, content=b"desktop doc")
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "view", "test-agent")
    r = client.get(f"/wopi/files/{_encode_file_id(rel)}/contents", params={"access_token": token})
    assert r.status_code == 200 and r.content == b"desktop doc"
    # A version-pinned preview snapshot.
    snap_root = tmp_path / "preview-snapshots"
    monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", snap_root, raising=False)
    (snap_root / "chat-1").mkdir(parents=True)
    (snap_root / "chat-1" / "abc123").write_bytes(b"pinned")
    rel = wopi.snapshot_rel_path("chat-1", "abc123")
    token, _ = wopi.create_wopi_token(rel, "u", "U", "view", "test-agent",
                                      display_name="report.xlsx")
    fid = _encode_file_id(rel)
    assert client.get(f"/wopi/files/{fid}/contents",
                      params={"access_token": token}).content == b"pinned"
    j = client.get(f"/wopi/files/{fid}", params={"access_token": token}).json()
    assert j["BaseFileName"] == "report.xlsx" and j["Size"] == 6
    # A swapped snapshot file is refused like any other.
    (snap_root / "chat-1" / "abc123").unlink()
    (snap_root / "chat-1" / "abc123").symlink_to(cache_file)
    assert client.get(f"/wopi/files/{fid}/contents",
                      params={"access_token": token}).status_code == 404


# ---------------------------------------------------------------------------
# PutFile writes beneath the agents root; a malformed token path is
# refused instead of written
# ---------------------------------------------------------------------------


def _swap_on_open(monkeypatch, tree, victim):
    """``tree/workspace/sub`` becomes a link to the victim as the helper opens
    the root, after every check: the strict open must refuse it."""
    import contextlib
    from services.infra import safe_fs
    real = safe_fs.open_root
    state = {"done": False}

    @contextlib.contextmanager
    def _patched(root, rel=""):
        if not state["done"]:
            state["done"] = True
            d = tree / "workspace" / "sub"
            d.rmdir()
            os.symlink(victim, d)
        with real(root, rel) as fd:
            yield fd

    monkeypatch.setattr(safe_fs, "open_root", _patched)


@pytest.mark.asyncio
async def test_put_file_refuses_a_component_swapped_after_the_check(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from services.notifications import notification_manager
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", AsyncMock())
    (tmp_path / "test-agent" / "workspace" / "sub").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "x.docx").write_bytes(b"ORIGINAL")
    _swap_on_open(monkeypatch, tmp_path / "test-agent", victim)

    rel = "test-agent/workspace/sub/x.docx"
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "edit", "test-agent")
    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app, raise_server_exceptions=False).post(
        f"/wopi/files/{_encode_file_id(rel)}/contents?access_token={token}", content=b"NEW",
    )
    assert resp.status_code >= 400
    assert (victim / "x.docx").read_bytes() == b"ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["x.docx"]


@pytest.mark.asyncio
async def test_put_file_malformed_token_path_is_refused_not_written(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    for rel in ("loose.docx", "../escape.docx", "test-agent/../x.docx"):
        token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "edit", "test-agent")
        app = FastAPI()
        app.include_router(wopi.router)
        resp = TestClient(app, raise_server_exceptions=False).post(
            f"/wopi/files/{_encode_file_id(rel)}/contents?access_token={token}", content=b"NEW",
        )
        assert resp.status_code == 403, rel
    assert not (tmp_path / "loose.docx").exists()
    assert not (tmp_path.parent / "escape.docx").exists()


@pytest.mark.asyncio
async def test_put_file_host_cache_refuses_a_linked_cache_file(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from core.remote import remote_file_flow
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(remote_file_flow, "push_back_host_path", push)
    d = tmp_path / ".remote-host-cache" / "sess-1" / "abc123"
    d.mkdir(parents=True)
    (d / "_meta.json").write_text('{"machine_id": "m-1", "abs_path": "C:/Users/u/Desktop/x.docx"}')
    victim = tmp_path / "victim.docx"
    victim.write_bytes(b"ORIGINAL")
    (d / "x.docx").symlink_to(victim)
    rel = ".remote-host-cache/sess-1/abc123/x.docx"
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "edit", "test-agent")
    app = FastAPI()
    app.include_router(wopi.router)
    resp = TestClient(app, raise_server_exceptions=False).post(
        f"/wopi/files/{_encode_file_id(rel)}/contents?access_token={token}", content=b"NEW",
    )
    assert resp.status_code == 403
    assert victim.read_bytes() == b"ORIGINAL"
    push.assert_not_awaited()


def test_preview_mint_stamps_the_agent_of_the_path(temp_db, tmp_path, monkeypatch):
    """A meeting participant's preview names the participant's tree; the
    token's agent claim follows the path, not the parent chat's agent."""
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from auth.providers import UserContext, get_current_user
    from storage import database as db
    (tmp_path / "participant" / "workspace").mkdir(parents=True)
    (tmp_path / "participant" / "workspace" / "r.docx").write_bytes(b"doc")
    monkeypatch.setattr(db, "get_username_by_sub", lambda s: "alice")
    monkeypatch.setattr(db, "get_chat", lambda cid: {"id": cid, "agent": "parent", "session_id": ""})
    monkeypatch.setattr(db, "get_preview_event_by_file", lambda cid, fid: {"id": 1})
    monkeypatch.setattr("api.agents.chats.can_access_chat", lambda u, c: True)
    user = UserContext(sub="alice-sub", email="a@t.com", name="Alice", role="creator",
                       agents=["parent", "participant"],
                       agent_roles={"parent": "manager", "participant": "manager"})

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(wopi.router)
    app.dependency_overrides[get_current_user] = _stub
    rel = "participant/workspace/r.docx"
    resp = TestClient(app).get(
        f"/v1/documents/preview-wopi-url?chat_id=c1&file_id={_encode_file_id(rel)}",
    )
    assert resp.status_code == 200, resp.text
    from urllib.parse import parse_qs, urlparse
    token = parse_qs(urlparse(resp.json()["wopi_url"]).query)["access_token"][0]
    assert wopi.validate_wopi_token(token)["agent"] == "participant"
