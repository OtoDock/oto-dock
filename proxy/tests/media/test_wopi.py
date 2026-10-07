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

import asyncio
import base64
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _encode_file_id(rel: str) -> str:
    return base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _fresh_answers():
    # The answered LastModifiedTime and each document's base are remembered
    # per file id: tests share file ids, so each starts with none remembered.
    # So are the path locks, which a contended acquire binds to its test's
    # event loop.
    from api.hooks import preview
    from api.media import wopi
    from core.remote import remote_file_flow
    wopi._stable_times.clear()
    wopi._doc_bases.clear()
    wopi._bad_timestamp_warned.clear()
    wopi._refusals_logged.clear()
    preview._pushed_generations.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._fanout_locks.clear()
    yield
    wopi._stable_times.clear()
    wopi._doc_bases.clear()
    wopi._refusals_logged.clear()
    wopi._wopi_locks.clear()
    preview._pushed_generations.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._fanout_locks.clear()


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
async def test_put_file_answers_the_saved_files_last_modified_time(temp_db, tmp_path, monkeypatch):
    # Collabora keeps PutFile's LastModifiedTime and sends it back on its
    # next save, so it must be the text CheckFileInfo answers for the same
    # file; a save whose file is gone before the stat still answers 200
    # (the save happened).
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from services.notifications import notification_manager
    from services.remote import workspace_fanout

    monkeypatch.setattr(workspace_fanout, "propagate_write", AsyncMock())
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", AsyncMock())

    rel = "test-agent/workspace/x.docx"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"saved bytes")
    os.utime(target, (1700000000, 1700000000))
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "edit", "test-agent")
    file_id = _encode_file_id(rel)

    app = FastAPI()
    app.include_router(wopi.router)
    client = TestClient(app)
    resp = client.post(f"/wopi/files/{file_id}/contents?access_token={token}", content=b"saved bytes")
    assert resp.status_code == 200
    assert resp.json() == {"LastModifiedTime": "2023-11-14T22:13:20.000Z"}
    info = client.get(f"/wopi/files/{file_id}?access_token={token}").json()
    assert info["LastModifiedTime"] == resp.json()["LastModifiedTime"]

    target.unlink()
    resp = client.post(f"/wopi/files/{file_id}/contents?access_token={token}", content=b"again")
    assert resp.status_code == 200 and resp.content == b""


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

    async def _push(session_id, path):
        # A fixed mtime after the save's write: the answer must come from
        # the mirror's stat, which a clock-built answer cannot match.
        os.utime(path, (1_800_000_000, 1_800_000_000))
        return True

    push = AsyncMock(side_effect=_push)
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
    # The host-cache save answers the mirror's LastModifiedTime too.
    assert resp.json() == {"LastModifiedTime": "2027-01-15T08:00:00.000Z"}
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
    body = resp.json()
    assert "access_token" not in body["wopi_url"]
    assert wopi.validate_wopi_token(body["access_token"])["agent"] == "participant"
    assert body["access_token_ttl"] > 0


# ---------------------------------------------------------------------------
# The three mints read their stores and probe their paths off the loop
# ---------------------------------------------------------------------------


def _guard_loop(monkeypatch, owner, name):
    """Wrap ``owner.name`` to record, per call, whether it ran with an event
    loop running in its thread (on the loop) or not (a worker thread)."""
    import asyncio
    real = getattr(owner, name)
    on_loop: list[bool] = []

    def _guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(*a, **kw)
    monkeypatch.setattr(owner, name, _guarded)
    return on_loop


def _chat_mint_client(monkeypatch, tmp_path):
    """A manager of test-agent who may open the chat, with the chat reads
    stubbed: the client of the two chat-gated mints."""
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from auth.providers import UserContext, get_current_user
    from storage import database as db
    monkeypatch.setattr(db, "get_username_by_sub", lambda s: "alice")
    monkeypatch.setattr(
        db, "get_chat", lambda cid: {"id": cid, "agent": "test-agent", "session_id": ""},
    )
    monkeypatch.setattr("api.agents.chats.can_access_chat", lambda u, c: True)
    user = UserContext(sub="alice-sub", email="a@t.com", name="Alice", role="creator",
                       agents=["test-agent"], agent_roles={"test-agent": "manager"})

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(wopi.router)
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


def test_the_workspace_mint_reads_off_the_loop(temp_db, tmp_path, monkeypatch):
    from storage import database as db
    _seed_file(tmp_path, "workspace/x.docx")
    app = _make_url_app(monkeypatch, tmp_path, role="manager", username="mgr")
    on_loop = _guard_loop(monkeypatch, db, "get_username_by_sub")
    resp = _ask_url(app, "workspace/x.docx")
    assert resp.status_code == 200 and resp.json()["permissions"] == "edit"
    assert on_loop and not any(on_loop)


def test_the_snapshot_mint_reads_off_the_loop(temp_db, tmp_path, monkeypatch):
    from services.media import preview_snapshots
    from storage import database as db
    client = _chat_mint_client(monkeypatch, tmp_path)
    monkeypatch.setattr(db, "get_preview_event_by_snapshot",
                        lambda cid, sid: {"filename": "r.xlsx"})
    monkeypatch.setattr(preview_snapshots, "snapshot_path", lambda cid, sid: tmp_path / "s")
    reads = [_guard_loop(monkeypatch, db, "get_chat"),
             _guard_loop(monkeypatch, db, "get_preview_event_by_snapshot")]
    resp = client.get("/v1/documents/snapshot-wopi-url?chat_id=c1&snapshot_id=abc")
    assert resp.status_code == 200, resp.text
    for on_loop in reads:
        assert on_loop and not any(on_loop)


def test_the_preview_mint_reads_off_the_loop(temp_db, tmp_path, monkeypatch):
    from storage import database as db
    client = _chat_mint_client(monkeypatch, tmp_path)
    _seed_file(tmp_path, "workspace/r.docx")
    monkeypatch.setattr(db, "get_preview_event_by_file", lambda cid, fid: {"id": 1})
    reads = [_guard_loop(monkeypatch, db, "get_chat"),
             _guard_loop(monkeypatch, db, "get_preview_event_by_file"),
             _guard_loop(monkeypatch, db, "get_username_by_sub")]
    fid = _encode_file_id("test-agent/workspace/r.docx")
    resp = client.get(f"/v1/documents/preview-wopi-url?chat_id=c1&file_id={fid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["permissions"] == "edit"
    for on_loop in reads:
        assert on_loop and not any(on_loop)


# ---------------------------------------------------------------------------
# The save check: X-COOL-WOPI-Timestamp against the file's current answer
# ---------------------------------------------------------------------------


def _save_rig(monkeypatch, tmp_path, content=b"agent bytes", mtime=1_700_000_000):
    """An agent-tree docx with a fixed mtime, an edit token for it and a
    client. The real propagate_write runs (its path lock, the save check
    inside it); ``pw`` is its disk write and the fan-out is a no-op."""
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from services.notifications import notification_manager
    from services.remote import workspace_fanout

    rel = "test-agent/workspace/x.docx"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    os.utime(target, (mtime, mtime))

    async def _write(agent, tree_rel, body):
        (tmp_path / agent / tree_rel).write_bytes(body)

    pw = AsyncMock(side_effect=_write)
    bc = AsyncMock()
    monkeypatch.setattr(workspace_fanout, "_atomic_write_agent_file", pw)
    monkeypatch.setattr(workspace_fanout, "fan_out_write", AsyncMock())
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", bc)
    token, _ = wopi.create_wopi_token(rel, "user-bob-sub", "Bob", "edit", "test-agent")
    file_id = _encode_file_id(rel)
    app = FastAPI()
    app.include_router(wopi.router)
    return TestClient(app), target, file_id, token, pw, bc


def _loaded_time(client, file_id, token):
    return client.get(f"/wopi/files/{file_id}?access_token={token}").json()["LastModifiedTime"]


def _save(client, file_id, token, body, timestamp=None, **headers):
    if timestamp is not None:
        headers["X-COOL-WOPI-Timestamp"] = timestamp
    return client.post(f"/wopi/files/{file_id}/contents?access_token={token}",
                       content=body, headers=headers)


def test_a_save_naming_the_loaded_time_lands(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    resp = _save(client, file_id, token, b"person bytes", loaded)
    assert resp.status_code == 200, resp.text
    assert target.read_bytes() == b"person bytes"
    pw.assert_awaited_once()


def test_a_save_after_another_write_is_refused_and_writes_nothing(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    # The agent writes the file after the person loaded it.
    target.write_bytes(b"the agent's rewrite")
    os.utime(target, (1_700_000_100, 1_700_000_100))
    resp = _save(client, file_id, token, b"person bytes", loaded)
    assert resp.status_code == 409
    assert resp.json() == {"COOLStatusCode": 1010}
    assert target.read_bytes() == b"the agent's rewrite"
    pw.assert_not_awaited()
    bc.assert_not_awaited()


def test_a_changed_file_of_the_same_size_is_a_conflict(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path, content=b"aaaa")
    loaded = _loaded_time(client, file_id, token)
    target.write_bytes(b"bbbb")
    os.utime(target, (1_700_000_100, 1_700_000_100))
    assert _save(client, file_id, token, b"cccc", loaded).status_code == 409
    pw.assert_not_awaited()


def test_the_same_bytes_rewritten_keep_their_time_and_the_save_lands(temp_db, tmp_path, monkeypatch):
    # A satellite pull or a fan-out echo rewrites identical bytes with a new
    # mtime: not a change, neither for CheckFileInfo nor for the check.
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    target.write_bytes(b"agent bytes")
    os.utime(target, (1_700_000_100, 1_700_000_100))
    assert _loaded_time(client, file_id, token) == loaded
    assert _save(client, file_id, token, b"person bytes", loaded).status_code == 200
    pw.assert_awaited_once()


def test_a_save_answer_is_the_next_saves_timestamp(temp_db, tmp_path, monkeypatch):
    client, _target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    first = _save(client, file_id, token, b"one", _loaded_time(client, file_id, token))
    assert first.status_code == 200
    second = _save(client, file_id, token, b"two", first.json()["LastModifiedTime"])
    assert second.status_code == 200
    assert pw.await_count == 2


def test_a_save_without_the_header_lands(temp_db, tmp_path, monkeypatch):
    # Collabora's Overwrite after a refusal sends no timestamp.
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    target.write_bytes(b"the agent's rewrite")
    os.utime(target, (1_700_000_100, 1_700_000_100))
    assert _save(client, file_id, token, b"person bytes").status_code == 200
    assert target.read_bytes() == b"person bytes"


def test_an_unreadable_header_saves_and_warns_once(temp_db, tmp_path, monkeypatch, caplog):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    caplog.set_level("WARNING", logger="claude-proxy")
    assert _save(client, file_id, token, b"one", "not a time").status_code == 200
    assert _save(client, file_id, token, b"two", "still not").status_code == 200
    assert pw.await_count == 2
    assert sum("X-COOL-WOPI-Timestamp" in r.getMessage() for r in caplog.records) == 1


def test_the_file_gone_since_the_load_is_a_conflict(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    target.unlink()
    assert _save(client, file_id, token, b"person bytes", loaded).status_code == 409
    assert not target.exists()
    pw.assert_not_awaited()


def test_a_refused_closing_save_is_logged(temp_db, tmp_path, monkeypatch, caplog):
    client, target, file_id, token, _pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    target.write_bytes(b"the agent's rewrite")
    os.utime(target, (1_700_000_100, 1_700_000_100))
    caplog.set_level("WARNING", logger="claude-proxy")
    resp = _save(client, file_id, token, b"person bytes", loaded, **{"X-COOL-WOPI-IsExitSave": "true"})
    assert resp.status_code == 409
    assert any("closing editor" in r.getMessage() for r in caplog.records)


def test_the_lock_refusal_still_comes_first(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    from api.media import wopi
    loaded = _loaded_time(client, file_id, token)
    wopi._set_lock(file_id, "someone-else")
    try:
        target.write_bytes(b"the agent's rewrite")
        resp = _save(client, file_id, token, b"x", loaded, **{"X-WOPI-Lock": "mine"})
        assert resp.status_code == 409
        assert resp.headers.get("X-WOPI-Lock") == "someone-else"
    finally:
        wopi._wopi_locks.pop(file_id, None)
    pw.assert_not_awaited()


@pytest.mark.parametrize("text, seconds", [
    ("2023-11-14T22:13:20.000Z", 1_700_000_000),
    ("2023-11-14T22:13:20Z", 1_700_000_000),
    ("2023-11-14T22:13:20.999999Z", 1_700_000_000),
    ("2023-11-15T01:13:20.000+03:00", 1_700_000_000),
    ("2023-11-14T22:13:20", 1_700_000_000),
    ("yesterday", None),
    ("", None),
])
def test_timestamp_texts_read_as_whole_utc_seconds(text, seconds):
    from api.media import wopi
    assert wopi._answer_seconds(text) == seconds


@pytest.mark.asyncio
async def test_a_host_cache_save_after_a_pull_is_refused_and_not_pushed(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from core.remote import remote_file_flow

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(remote_file_flow, "push_back_host_path", push)
    cache_file, rel = _seed_host_cache(tmp_path)
    os.utime(cache_file, (1_700_000_000, 1_700_000_000))
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "edit", "test-agent")
    file_id = _encode_file_id(rel)
    app = FastAPI()
    app.include_router(wopi.router)
    client = TestClient(app)
    loaded = _loaded_time(client, file_id, token)
    cache_file.write_bytes(b"pulled again, changed")
    os.utime(cache_file, (1_700_000_100, 1_700_000_100))
    resp = _save(client, file_id, token, b"edited bytes", loaded)
    assert resp.status_code == 409
    assert cache_file.read_bytes() == b"pulled again, changed"
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_comparison_waits_for_a_write_in_flight(temp_db, tmp_path, monkeypatch):
    # A platform write holding the path lock lands before the save is
    # compared, so the save sees it and is refused.
    import httpx
    from core.remote import remote_file_flow

    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    app = client.app
    lock = await remote_file_flow._acquire_global_path_lock("test-agent", "workspace/x.docx")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as ac:
        async with lock:
            task = asyncio.create_task(ac.post(
                f"/wopi/files/{file_id}/contents?access_token={token}",
                content=b"person bytes", headers={"X-COOL-WOPI-Timestamp": loaded},
            ))
            await asyncio.sleep(0.2)
            assert not task.done()
            target.write_bytes(b"written while the lock was held")
            os.utime(target, (1_700_000_100, 1_700_000_100))
        resp = await task
    assert resp.status_code == 409
    assert target.read_bytes() == b"written while the lock was held"
    pw.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_write_queued_during_the_comparison_lands_after_the_save(temp_db, tmp_path, monkeypatch):
    # A satellite's applier asks for the path lock while the save is being
    # compared: it lands after the person's save, never between the check
    # and the write, so the save cannot overwrite it unseen.
    import httpx
    from api.media import wopi
    from core.remote import remote_file_flow

    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    order = []
    loop = asyncio.get_running_loop()
    queued = []

    async def _agent_write():
        lock = await remote_file_flow._acquire_global_path_lock("test-agent", "workspace/x.docx")
        async with lock:
            order.append("agent write")
            target.write_bytes(b"the agent's write")

    async def _queue_agent_write():
        queued.append(asyncio.create_task(_agent_write()))
        for _ in range(5):
            await asyncio.sleep(0)

    real_answer = wopi._current_answer

    def _answer_while_a_write_queues(fid, claims):
        order.append("check")
        if not queued:
            asyncio.run_coroutine_threadsafe(_queue_agent_write(), loop).result(timeout=5)
        return real_answer(fid, claims)

    monkeypatch.setattr(wopi, "_current_answer", _answer_while_a_write_queues)
    real_write = pw.side_effect

    async def _person_write(agent, tree_rel, body):
        order.append("person write")
        await real_write(agent, tree_rel, body)
    pw.side_effect = _person_write

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://t") as ac:
        resp = await ac.post(
            f"/wopi/files/{file_id}/contents?access_token={token}",
            content=b"person bytes", headers={"X-COOL-WOPI-Timestamp": loaded},
        )
    assert resp.status_code == 200
    await queued[0]
    assert order[:3] == ["check", "person write", "agent write"]
    assert target.read_bytes() == b"the agent's write"


@pytest.mark.asyncio
async def test_a_refused_precheck_writes_and_fans_out_nothing(temp_db, tmp_path, monkeypatch):
    import config
    from services.remote import workspace_fanout as wf
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    fo = AsyncMock()
    monkeypatch.setattr(wf, "fan_out_write", fo)

    async def _no():
        return False
    assert await wf.propagate_write("agent-1", "workspace/x.md", b"hello", precheck=_no) is False
    assert not (tmp_path / "agent-1" / "workspace" / "x.md").exists()
    fo.assert_not_awaited()

    async def _yes():
        return True
    assert await wf.propagate_write("agent-1", "workspace/x.md", b"hello", precheck=_yes) is True
    assert (tmp_path / "agent-1" / "workspace" / "x.md").read_bytes() == b"hello"
    fo.assert_awaited_once()


def _fetch(client, file_id, token):
    resp = client.get(f"/wopi/files/{file_id}/contents?access_token={token}")
    assert resp.status_code == 200
    return resp.content


def _agent_rewrite(target, body=b"the agent's rewrite", mtime=1_700_000_100):
    target.write_bytes(body)
    os.utime(target, (mtime, mtime))


def test_a_view_joining_after_a_write_does_not_let_the_old_save_through(temp_db, tmp_path, monkeypatch):
    # A second view of the open document (another device, a teammate) is
    # answered the file's new time, and Collabora then sends that time back
    # on the first view's save: the bytes the document was built from still
    # tell the write apart.
    client, target, file_id, token, pw, bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    assert _fetch(client, file_id, token) == b"agent bytes"
    _agent_rewrite(target)
    joined = _loaded_time(client, file_id, token)
    assert joined != loaded
    resp = _save(client, file_id, token, b"person bytes", joined)
    assert resp.status_code == 409
    assert resp.json() == {"COOLStatusCode": 1010}
    assert target.read_bytes() == b"the agent's rewrite"
    pw.assert_not_awaited()
    bc.assert_not_awaited()


def test_a_landed_save_is_the_documents_base(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    _fetch(client, file_id, token)
    first = _save(client, file_id, token, b"one", _loaded_time(client, file_id, token))
    assert first.status_code == 200
    second = _save(client, file_id, token, b"two", first.json()["LastModifiedTime"])
    assert second.status_code == 200
    assert target.read_bytes() == b"two"
    assert pw.await_count == 2


def test_an_overwrite_after_a_refusal_is_the_new_base(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    _fetch(client, file_id, token)
    _agent_rewrite(target)
    joined = _loaded_time(client, file_id, token)
    assert _save(client, file_id, token, b"person bytes", joined).status_code == 409
    forced = _save(client, file_id, token, b"person bytes")
    assert forced.status_code == 200
    assert target.read_bytes() == b"person bytes"
    after = _save(client, file_id, token, b"person bytes, more", forced.json()["LastModifiedTime"])
    assert after.status_code == 200


def test_the_same_bytes_rewritten_after_the_fetch_still_save(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    _fetch(client, file_id, token)
    _agent_rewrite(target, b"agent bytes")
    assert _save(client, file_id, token, b"person bytes", loaded).status_code == 200
    pw.assert_awaited_once()


def test_a_file_past_the_hash_cap_keeps_the_timestamp_check_only(temp_db, tmp_path, monkeypatch):
    from api.media import wopi
    monkeypatch.setattr(wopi, "_HASH_MAX_BYTES", 4)
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    _fetch(client, file_id, token)
    assert file_id not in wopi._doc_bases
    _agent_rewrite(target)
    assert _save(client, file_id, token, b"person bytes", _loaded_time(client, file_id, token)).status_code == 200


@pytest.mark.asyncio
async def test_a_host_cache_save_after_a_pull_and_a_joining_view_is_refused(temp_db, tmp_path, monkeypatch):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from core.remote import remote_file_flow

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(remote_file_flow, "push_back_host_path", push)
    cache_file, rel = _seed_host_cache(tmp_path)
    os.utime(cache_file, (1_700_000_000, 1_700_000_000))
    token, _ = wopi.create_wopi_token(rel, "agent", "Agent", "edit", "test-agent")
    file_id = _encode_file_id(rel)
    app = FastAPI()
    app.include_router(wopi.router)
    client = TestClient(app)
    _fetch(client, file_id, token)
    _agent_rewrite(cache_file, b"pulled again, changed")
    resp = _save(client, file_id, token, b"edited bytes", _loaded_time(client, file_id, token))
    assert resp.status_code == 409
    assert cache_file.read_bytes() == b"pulled again, changed"
    push.assert_not_awaited()


# ---------------------------------------------------------------------------
# A save from a chat's document pane refreshes the file's newest version
# ---------------------------------------------------------------------------


def _pane_save_rig(monkeypatch, tmp_path):
    """The save rig plus a chat whose newest push of the file has a version
    copy, an edit token carrying the chat, and the writer lane run inline."""
    import json
    import config
    from api.media import wopi
    from core.events import chat_writer
    from services.media import preview_snapshots
    from storage import database as db

    client, target, file_id, _token, pw, bc = _save_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", tmp_path / "snaps", raising=False)
    db.create_chat("chat-p", "user-bob-sub", "test-agent")
    sid = preview_snapshots.create_snapshot("chat-p", target)
    db.add_chat_message("chat-p", "event", "", event_type="document_preview",
                        event_data=json.dumps({"type": "document_preview", "file_id": file_id,
                                               "filename": "x.docx", "snapshot_id": sid}))
    jobs = []

    def _run_now(chat_id, job, *, label=""):
        jobs.append((chat_id, label, job()))
    monkeypatch.setattr(chat_writer, "submit", _run_now)
    token, _ = wopi.create_wopi_token("test-agent/workspace/x.docx", "user-bob-sub", "Bob",
                                      "edit", "test-agent", chat_id="chat-p")
    return client, target, file_id, token, sid, jobs, tmp_path / "snaps" / "chat-p" / sid


def test_a_pane_save_refreshes_its_chats_newest_version(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, _sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    resp = _save(client, file_id, token, b"edited by the person", loaded)
    assert resp.status_code == 200
    assert jobs == [("chat-p", "preview_version_refresh", True)]
    assert copy.read_bytes() == b"edited by the person"
    # A second save from the same pane keeps folding into that version.
    resp = _save(client, file_id, token, b"edited twice", resp.json()["LastModifiedTime"])
    assert resp.status_code == 200 and copy.read_bytes() == b"edited twice"


def test_an_agent_change_without_a_push_keeps_the_version(temp_db, tmp_path, monkeypatch):
    client, target, file_id, token, _sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    target.write_bytes(b"the agent's unpushed edit")
    os.utime(target, (1_700_000_200, 1_700_000_200))
    loaded = _loaded_time(client, file_id, token)
    assert _save(client, file_id, token, b"person on top", loaded).status_code == 200
    assert jobs == [("chat-p", "preview_version_refresh", False)]
    assert copy.read_bytes() == b"agent bytes"


def test_saves_that_refresh_nothing(temp_db, tmp_path, monkeypatch):
    from api.media import wopi
    client, target, file_id, token, _sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)
    # A forced overwrite (no timestamp): the agent's delivery stays the version.
    assert _save(client, file_id, token, b"forced").status_code == 200
    # A workspace tab's token carries no chat.
    plain, _ = wopi.create_wopi_token("test-agent/workspace/x.docx", "user-bob-sub", "Bob",
                                      "edit", "test-agent")
    assert _save(client, file_id, plain, b"from the tab", _loaded_time(client, file_id, plain)).status_code == 200
    # A refused save.
    assert _save(client, file_id, token, b"stale", loaded).status_code == 409
    assert jobs == [] and copy.read_bytes() == b"agent bytes"


def test_a_pending_push_is_the_version_a_save_refreshes(temp_db, tmp_path, monkeypatch):
    from core.events import stream_pump
    from services.media import preview_snapshots
    client, target, file_id, token, sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    pending = preview_snapshots.create_snapshot("chat-p", target)
    monkeypatch.setattr(stream_pump, "pending_preview_snapshot",
                        lambda chat_id, fid: pending if (chat_id, fid) == ("chat-p", file_id) else None)
    assert _save(client, file_id, token, b"edited", _loaded_time(client, file_id, token)).status_code == 200
    assert (tmp_path / "snaps" / "chat-p" / pending).read_bytes() == b"edited"
    assert copy.read_bytes() == b"agent bytes"


def test_a_save_replaced_before_the_stat_refreshes_nothing(temp_db, tmp_path, monkeypatch):
    # Another write of the same size lands between the save and its stat:
    # the version keeps the push's bytes rather than taking the other write's.
    from services.remote import workspace_fanout
    client, target, file_id, token, _sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)

    async def _write_then_replaced(agent, tree_rel, body, **_kw):
        (tmp_path / agent / tree_rel).write_bytes(b"x" * len(body))
    monkeypatch.setattr(workspace_fanout, "propagate_write", AsyncMock(side_effect=_write_then_replaced))
    assert _save(client, file_id, token, b"edited by the person", loaded).status_code == 200
    assert jobs == [] and copy.read_bytes() == b"agent bytes"


def test_another_chats_token_refreshes_only_its_own_chat(temp_db, tmp_path, monkeypatch):
    from api.media import wopi
    client, target, file_id, _token, _sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    other, _ = wopi.create_wopi_token("test-agent/workspace/x.docx", "user-bob-sub", "Bob",
                                      "edit", "test-agent", chat_id="chat-q")
    assert _save(client, file_id, other, b"from chat q", _loaded_time(client, file_id, other)).status_code == 200
    assert jobs == [("chat-q", "preview_version_refresh", False)]
    assert copy.read_bytes() == b"agent bytes"


def test_a_file_the_check_cannot_read_saves_without_it(temp_db, tmp_path, monkeypatch):
    # Only a file gone since the load is a conflict; one that is there but
    # cannot be read (descriptors exhausted, an I/O error) fails open.
    import errno
    from api.media import wopi
    client, target, file_id, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    loaded = _loaded_time(client, file_id, token)

    def _emfile(root, rel):
        raise OSError(errno.EMFILE, "Too many open files")
    monkeypatch.setattr(wopi.safe_fs, "open_regular_for_read", _emfile)
    assert _save(client, file_id, token, b"person bytes", loaded).status_code == 200
    pw.assert_awaited_once()


def test_the_live_remint_names_its_chat(temp_db, tmp_path, monkeypatch):
    import jwt as _jwt
    import config
    from storage import database as db
    client = _chat_mint_client(monkeypatch, tmp_path)
    _seed_file(tmp_path, "workspace/r.docx")
    monkeypatch.setattr(db, "get_preview_event_by_file", lambda cid, fid: {"id": 1})
    fid = _encode_file_id("test-agent/workspace/r.docx")
    body = client.get(f"/v1/documents/preview-wopi-url?chat_id=c1&file_id={fid}").json()
    claims = _jwt.decode(body["access_token"], config.WOPI_SECRET, algorithms=["HS256"])
    assert claims["chat_id"] == "c1"


# ---------------------------------------------------------------------------
# GET /v1/documents/chat-documents — the pane's documents and versions
# ---------------------------------------------------------------------------


def _listing_rig(monkeypatch, tmp_path, *, access=True):
    import json
    import config
    from storage import database as db
    from services.media import preview_snapshots
    client = _chat_mint_client(monkeypatch, tmp_path)
    monkeypatch.setattr("api.agents.chats.can_access_chat", lambda u, c: access)
    monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", tmp_path / "snaps", raising=False)
    db.create_chat("c1", "alice-sub", "test-agent")
    src = _seed_file(tmp_path, "workspace/a.docx")

    def push(file_id, filename, generation, snapshot=True, dismissed=False):
        sid = preview_snapshots.create_snapshot("c1", src) if snapshot else ""
        data = {"type": "document_preview", "file_id": file_id, "filename": filename,
                "download_url": f"/v1/media/{file_id}", "snapshot_id": sid,
                "generation": generation}
        if dismissed:
            data["dismissed"] = True
        db.add_chat_message("c1", "event", "", event_type="document_preview", event_data=json.dumps(data))
        return sid
    return client, push


def test_the_listing_numbers_versions_and_orders_documents(temp_db, tmp_path, monkeypatch):
    from services.media import preview_snapshots
    from storage import database as db
    client, push = _listing_rig(monkeypatch, tmp_path)
    a1 = push("fa", "a.docx", 1000)  # before any message of the person: turn 1
    db.add_chat_message("c1", "user", "write the sheet")
    push("fb", "b.xlsx", 2000, snapshot=False)
    db.add_chat_message("c1", "user", "again")
    push("fa", "a.docx", 2500, dismissed=True)
    a2 = push("fa", "a-renamed.docx", 3000)
    preview_snapshots.delete_snapshot("c1", a1)

    resp = client.get("/v1/documents/chat-documents?chat_id=c1")
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    assert [d["file_id"] for d in docs] == ["fa", "fb"]
    fa = docs[0]
    assert fa["filename"] == "a-renamed.docx" and fa["download_url"] == "/v1/media/fa"
    assert fa["generation"] == 3000
    assert [(v["version"], v["turn"], v["available"], v["snapshot_id"]) for v in fa["versions"]] == [
        (2, 2, True, a2), (1, 1, False, a1)]
    fb = docs[1]["versions"]
    assert [(v["version"], v["turn"], v["available"]) for v in fb] == [(1, 1, False)]
    assert "wopi_url" not in resp.text and "access_token" not in resp.text


def test_the_listing_is_gated_by_the_chat(temp_db, tmp_path, monkeypatch):
    from storage import database as db
    client, _push = _listing_rig(monkeypatch, tmp_path, access=False)
    assert client.get("/v1/documents/chat-documents?chat_id=c1").status_code == 403
    monkeypatch.setattr(db, "get_chat", lambda cid: None)
    assert client.get("/v1/documents/chat-documents?chat_id=nope").status_code == 404


def test_the_listing_reads_off_the_loop(temp_db, tmp_path, monkeypatch):
    from storage import database as db
    client, push = _listing_rig(monkeypatch, tmp_path)
    push("fa", "a.docx", 1000)
    reads = [_guard_loop(monkeypatch, db, "get_chat"),
             _guard_loop(monkeypatch, db, "get_chat_preview_listing_rows")]
    assert client.get("/v1/documents/chat-documents?chat_id=c1").status_code == 200
    for on_loop in reads:
        assert on_loop and not any(on_loop)


def test_an_earlier_rounds_document_opens_from_the_runs_page(temp_db, tmp_path, monkeypatch):
    # Each round of a multi-turn task run has its own task- chat on one
    # session, and the page shows them all: a card from round 1 opens
    # (listing, live mint, version mint) from round 2's page.
    import json
    import jwt as _jwt
    import config
    from api.media import wopi
    from services.media import preview_snapshots
    from storage import database as db
    client = _chat_mint_client(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", tmp_path / "snaps", raising=False)
    runs = {"r1": {"id": "r1", "chat_id": "task-r1", "session_id": "s", "started_at": "1"},
            "r2": {"id": "r2", "chat_id": "task-r2", "session_id": "s", "started_at": "2"},
            "rx": {"id": "rx", "chat_id": "task-rx", "session_id": "other", "started_at": "1"}}
    monkeypatch.setattr(db, "get_run", runs.get)
    monkeypatch.setattr(db, "list_runs", lambda limit=50, session_id=None, **_kw: [
        r for r in runs.values() if r["session_id"] == session_id])
    rel = "test-agent/workspace/a.docx"
    src = _seed_file(tmp_path, "workspace/a.docx")
    fid = _encode_file_id(rel)
    for cid in ("task-r1", "task-r2", "task-rx"):
        db.create_chat(cid, "alice-sub", "test-agent")
    sid = preview_snapshots.create_snapshot("task-r1", src)
    db.add_chat_message("task-r1", "user", "round one")
    db.add_chat_message("task-r1", "event", "", event_type="document_preview", event_data=json.dumps({
        "type": "document_preview", "file_id": fid, "filename": "a.docx",
        "download_url": "/v1/media/a", "snapshot_id": sid, "generation": 1000}))
    db.add_chat_message("task-r2", "user", "round two")

    docs = client.get("/v1/documents/chat-documents?chat_id=task-r2").json()["documents"]
    assert [(d["file_id"], [(v["snapshot_id"], v["turn"], v["available"]) for v in d["versions"]])
            for d in docs] == [(fid, [(sid, 1, True)])]

    live = client.get(f"/v1/documents/preview-wopi-url?chat_id=task-r2&file_id={fid}")
    assert live.status_code == 200, live.text
    claims = _jwt.decode(live.json()["access_token"], config.WOPI_SECRET, algorithms=["HS256"])
    assert claims["chat_id"] == "task-r1"

    # Round 2 pushes the same file: the listing numbers the versions and
    # counts the turns across the rounds, and the live token names the
    # newest round holding a push.
    sid2 = preview_snapshots.create_snapshot("task-r2", src)
    db.add_chat_message("task-r2", "event", "", event_type="document_preview", event_data=json.dumps({
        "type": "document_preview", "file_id": fid, "filename": "a.docx",
        "download_url": "/v1/media/a", "snapshot_id": sid2, "generation": 1001}))
    docs = client.get("/v1/documents/chat-documents?chat_id=task-r2").json()["documents"]
    assert [(v["snapshot_id"], v["version"], v["turn"], v["available"]) for v in docs[0]["versions"]] \
        == [(sid2, 2, 2, True), (sid, 1, 1, True)]
    live = client.get(f"/v1/documents/preview-wopi-url?chat_id=task-r2&file_id={fid}")
    claims = _jwt.decode(live.json()["access_token"], config.WOPI_SECRET, algorithms=["HS256"])
    assert claims["chat_id"] == "task-r2"

    version = client.get(f"/v1/documents/snapshot-wopi-url?chat_id=task-r2&snapshot_id={sid}")
    assert version.status_code == 200, version.text
    claims = _jwt.decode(version.json()["access_token"], config.WOPI_SECRET, algorithms=["HS256"])
    assert claims["file_path"] == wopi.snapshot_rel_path("task-r1", sid)

    # A run on another session is not on the page.
    assert client.get("/v1/documents/chat-documents?chat_id=task-rx").json()["documents"] == []
    assert client.get(
        f"/v1/documents/preview-wopi-url?chat_id=task-rx&file_id={fid}").status_code == 404
    assert client.get(
        f"/v1/documents/snapshot-wopi-url?chat_id=task-rx&snapshot_id={sid}").status_code == 404


# ---------------------------------------------------------------------------
# Generation-keyed ids: the pane's live loads open the newest push's document
# ---------------------------------------------------------------------------


def _keyed(rel: str, generation: int) -> str:
    return _encode_file_id(f"{rel}\n{generation}")


def test_a_wopi_id_is_bare_or_keyed_by_digits_after_its_last_newline():
    from api.media import wopi
    rel = "test-agent/workspace/x.docx"
    assert wopi.decode_wopi_id(_encode_file_id(rel)) == (rel, 0)
    assert wopi.decode_wopi_id(_keyed(rel, 1791234640123)) == (rel, 1791234640123)
    assert wopi.encode_wopi_id(rel, 7) == _keyed(rel, 7)
    assert wopi.encode_wopi_id(rel) == wopi.encode_wopi_id(rel, 0) == _encode_file_id(rel)
    assert wopi.decode_wopi_id(_encode_file_id("a\nb\n5")) == ("a\nb", 5)
    for bad in (f"{rel}\nabc", f"{rel}\n", f"{rel}\n12x", f"{rel}\n{'9' * 21}", f"{rel}\n٣"):
        with pytest.raises(ValueError):
            wopi.decode_wopi_id(_encode_file_id(bad))
    with pytest.raises(ValueError):
        wopi.decode_wopi_id(base64.urlsafe_b64encode(b"\xff\xfe").decode())


def test_a_keyed_id_answers_every_route_with_the_bare_paths_token(temp_db, tmp_path, monkeypatch):
    client, target, _bare, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    fid = _keyed("test-agent/workspace/x.docx", 42)
    info = client.get(f"/wopi/files/{fid}?access_token={token}")
    assert info.status_code == 200
    assert info.json()["BaseFileName"] == "x.docx"
    assert _fetch(client, fid, token) == b"agent bytes"
    lock = client.post(f"/wopi/files/{fid}?access_token={token}",
                       headers={"X-WOPI-Override": "LOCK", "X-WOPI-Lock": "L1"})
    assert lock.status_code == 200
    resp = _save(client, fid, token, b"person bytes", info.json()["LastModifiedTime"], **{"X-WOPI-Lock": "L1"})
    assert resp.status_code == 200
    assert target.read_bytes() == b"person bytes"


def test_a_keyed_id_never_opens_a_path_beyond_the_tokens(temp_db, tmp_path, monkeypatch):
    from api.media import wopi
    client, _target, _bare, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    (tmp_path / "test-agent" / "workspace" / "x.docx.d").mkdir()
    (tmp_path / "test-agent" / "workspace" / "x.docx.d" / "secret.docx").write_bytes(b"secret")
    dir_token, _ = wopi.create_wopi_token("test-agent/workspace", "user-bob-sub", "Bob", "edit", "test-agent")
    cases = [
        (token, _keyed("test-agent/workspace/x.docx.d/secret.docx", 1)),
        (dir_token, _keyed("test-agent/workspace/x.docx", 1)),
        (token, _encode_file_id("test-agent/workspace/x.docx\nabc")),
        (token, _keyed(".preview-snapshots/chat-1/abc", 1)),
    ]
    for tok, fid in cases:
        assert client.get(f"/wopi/files/{fid}?access_token={tok}").status_code == 403
        assert client.get(f"/wopi/files/{fid}/contents?access_token={tok}").status_code == 403
        assert _save(client, fid, tok, b"x").status_code == 403
        lock = client.post(f"/wopi/files/{fid}?access_token={tok}",
                           headers={"X-WOPI-Override": "LOCK", "X-WOPI-Lock": "L"})
        assert lock.status_code == 403
    pw.assert_not_awaited()


def test_a_lingering_old_generation_cannot_save_over_a_new_push(temp_db, tmp_path, monkeypatch):
    # The document of an earlier push still open on another device: its save
    # after the agent's next push is refused, the new push's view saves.
    client, target, _bare, token, pw, _bc = _save_rig(monkeypatch, tmp_path)
    rel = "test-agent/workspace/x.docx"
    old, new = _keyed(rel, 1), _keyed(rel, 2)
    old_loaded = _loaded_time(client, old, token)
    assert _fetch(client, old, token) == b"agent bytes"
    _agent_rewrite(target)
    new_loaded = _loaded_time(client, new, token)
    assert _fetch(client, new, token) == b"the agent's rewrite"
    for stamp in (old_loaded, new_loaded):
        resp = _save(client, old, token, b"stale edits", stamp)
        assert resp.status_code == 409
        assert resp.json() == {"COOLStatusCode": 1010}
    assert target.read_bytes() == b"the agent's rewrite"
    resp = _save(client, new, token, b"fresh edits", new_loaded)
    assert resp.status_code == 200
    assert target.read_bytes() == b"fresh edits"


def test_a_save_through_a_keyed_id_refreshes_the_chats_version(temp_db, tmp_path, monkeypatch):
    client, _target, _bare, token, _sid, jobs, copy = _pane_save_rig(monkeypatch, tmp_path)
    fid = _keyed("test-agent/workspace/x.docx", 9)
    resp = _save(client, fid, token, b"edited by the person", _loaded_time(client, fid, token))
    assert resp.status_code == 200
    assert jobs == [("chat-p", "preview_version_refresh", True)]
    assert copy.read_bytes() == b"edited by the person"


def test_the_preview_mint_opens_the_newest_push_of_the_file(temp_db, tmp_path, monkeypatch):
    import urllib.parse
    from collections import OrderedDict
    from api.hooks import preview
    from storage import database as db
    monkeypatch.setattr(preview, "_pushed_generations", OrderedDict())
    client = _chat_mint_client(monkeypatch, tmp_path)
    _seed_file(tmp_path, "workspace/r.docx")
    rel = "test-agent/workspace/r.docx"
    fid = _encode_file_id(rel)
    row = {"id": 1, "generation": 5}
    monkeypatch.setattr(db, "get_preview_event_by_file", lambda cid, f: dict(row))

    def opened():
        url = client.get(f"/v1/documents/preview-wopi-url?chat_id=c1&file_id={fid}").json()["wopi_url"]
        src = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["WOPISrc"][0]
        return src.rsplit("/", 1)[-1]

    # After a restart: the row's generation, which seeds the noted map.
    assert opened() == _keyed(rel, 5)
    assert preview.pushed_generation(fid) == 5
    # Another chat's newer row replaces an older seed (chats mint in any
    # order after a restart), and the document its push opened is the one
    # its stored URL names, not its push time.
    row.update(generation=8, wopi_url=(
        "https://c/browser/dist/cool.html?WOPISrc=" + urllib.parse.quote(
            f"https://w/wopi/files/{_keyed(rel, 7)}", safe="") + "&closebutton=0"))
    assert opened() == _keyed(rel, 7)
    row.update(generation=6, wopi_url="")
    assert opened() == _keyed(rel, 7)
    # A push this process made later, in this chat or another: its row may
    # not be stored yet. An older note never wins.
    preview.note_pushed_generation(fid, 9, "abc")
    assert opened() == _keyed(rel, 9)
    preview.note_pushed_generation(fid, 3)
    assert opened() == _keyed(rel, 9)
    row.update(generation=0)
    preview._pushed_generations.clear()
    assert opened() == fid


def test_the_workspace_tab_opens_the_panes_document(temp_db, tmp_path, monkeypatch):
    import urllib.parse
    from api.hooks import preview
    app = _make_url_app(monkeypatch, tmp_path, role="manager", username="alice")
    _seed_file(tmp_path, "workspace/x.docx")
    fid = _encode_file_id("test-agent/workspace/x.docx")

    def opened():
        body = _ask_url(app, "workspace/x.docx").json()
        assert body["file_id"] == fid
        src = urllib.parse.parse_qs(urllib.parse.urlparse(body["wopi_url"]).query)["WOPISrc"][0]
        return src.rsplit("/", 1)[-1]

    assert opened() == fid
    preview.note_pushed_generation(fid, 77, "abc")
    assert opened() == _keyed("test-agent/workspace/x.docx", 77)


def test_a_lingering_views_refused_saves_log_once_a_minute(temp_db, tmp_path, monkeypatch, caplog):
    client, target, _bare, token, _pw, _bc = _save_rig(monkeypatch, tmp_path)
    fid = _keyed("test-agent/workspace/x.docx", 1)
    loaded = _loaded_time(client, fid, token)
    _fetch(client, fid, token)
    _agent_rewrite(target)
    with caplog.at_level("INFO", logger="claude-proxy"):
        for _ in range(3):
            assert _save(client, fid, token, b"stale", loaded).status_code == 409
    assert caplog.text.count("changed since it was loaded") == 1
