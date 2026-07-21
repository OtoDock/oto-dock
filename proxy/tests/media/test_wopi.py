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
