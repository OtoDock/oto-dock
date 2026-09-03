"""Chunked upload endpoints (`/v1/upload/chunked/*`).

Same minimal-router harness as `test_uploads.py` (no app lifespan; auth +
store deps stubbed) plus a redirected `UPLOAD_STAGING_DIR`. Covers: init cap /
destination validation, offset-correct assembly, exact per-chunk size
enforcement, owner + traversal guards, 410 for reaped staging, complete's
verification + conflict rename + push scheduling, idempotent re-PUT, DELETE
cleanup, and the stale-staging sweep.
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_router(tmp_path, monkeypatch):
    """Mount only the uploads router; stub auth, stores, and the push."""
    import config
    from api.media import uploads
    from auth.providers import UserContext

    agents_dir = tmp_path / "agents"
    staging_dir = tmp_path / "upload-staging"
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(config, "UPLOAD_STAGING_DIR", staging_dir)

    user = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="creator", agents=["test-agent"],
        agent_roles={"test-agent": "manager"},
    )

    async def _stub_user():
        return user

    from storage import agent_store
    from storage import database as task_store
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(
        task_store, "get_username_by_sub",
        lambda sub: "alice" if sub == "user-test-sub" else None,
    )

    async def _noop_push(*a, **kw):
        return None
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _noop_push)

    app = FastAPI()
    app.include_router(uploads.router)
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir, staging_dir, user


def _init(client, size, filename="video.bin", agent="test-agent", target_dir=""):
    return client.post("/v1/upload/chunked/init", json={
        "agent": agent, "filename": filename, "size": size,
        "target_dir": target_dir,
    })


def _upload_all(client, upload_id, payload, chunk_size):
    for i in range(0, len(payload), chunk_size):
        r = client.put(
            f"/v1/upload/chunked/{upload_id}/{i // chunk_size}",
            content=payload[i:i + chunk_size],
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_chunked_roundtrip_assembles_and_lands_like_single_shot(
    app_with_router, monkeypatch,
):
    """init → chunks (out of order) → complete: byte-identical file in the
    default chat landing dir, single-shot response shape, staging cleaned."""
    import config
    app, agents_dir, staging_dir, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 8, raising=False)
    client = TestClient(app)

    payload = bytes(range(256)) * 8  # 2048 bytes → 256 chunks of 8
    resp = _init(client, len(payload))
    assert resp.status_code == 200, resp.text
    up = resp.json()
    assert up["chunk_size"] == 8
    upload_id = up["upload_id"]

    # Push chunks in a shuffled order — offsets, not arrival order, decide.
    n = (len(payload) + 7) // 8
    order = list(range(n))
    order = order[1::2] + order[0::2]
    for i in order:
        r = client.put(
            f"/v1/upload/chunked/{upload_id}/{i}",
            content=payload[i * 8:(i + 1) * 8],
        )
        assert r.status_code == 200, r.text

    done = client.post(f"/v1/upload/chunked/{upload_id}/complete")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["path"] == "users/alice/workspace/uploads/files/video.bin"
    assert body["filename"] == "video.bin"
    assert body["size"] == len(payload)
    assert body["transfer_id"]
    assert body["remote_push"] is False

    final = agents_dir / "test-agent" / "users" / "alice" / "workspace" / \
        "uploads" / "files" / "video.bin"
    assert final.read_bytes() == payload
    assert list(staging_dir.iterdir()) == []  # staging + meta gone


def test_chunked_conflict_rename_matches_single_shot(app_with_router, monkeypatch):
    import config
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 16, raising=False)
    client = TestClient(app)

    dest = agents_dir / "test-agent" / "users" / "alice" / "workspace" / \
        "uploads" / "files"
    dest.mkdir(parents=True)
    (dest / "video.bin").write_bytes(b"already here")

    payload = b"x" * 20
    up = _init(client, len(payload)).json()
    _upload_all(client, up["upload_id"], payload, up["chunk_size"])
    done = client.post(f"/v1/upload/chunked/{up['upload_id']}/complete").json()
    assert done["filename"] == "video_1.bin"
    assert (dest / "video_1.bin").read_bytes() == payload


def test_chunked_explicit_target_dir_lands_like_workspace_upload(
    app_with_router, monkeypatch,
):
    """The workspace tab passes target_dir — chunked must honor it exactly
    like the single-shot route (role-checked resolve + mkdir at complete)."""
    import config
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 16, raising=False)
    client = TestClient(app)
    payload = b"z" * 24
    up = _init(client, len(payload), filename="notes.txt",
               target_dir="users/alice/workspace/projects").json()
    _upload_all(client, up["upload_id"], payload, up["chunk_size"])
    done = client.post(f"/v1/upload/chunked/{up['upload_id']}/complete").json()
    assert done["path"] == "users/alice/workspace/projects/notes.txt"
    final = agents_dir / "test-agent" / "users" / "alice" / "workspace" / \
        "projects" / "notes.txt"
    assert final.read_bytes() == payload


def test_chunked_status_reports_received(app_with_router, monkeypatch):
    import config
    app, _agents, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 10).json()
    client.put(f"/v1/upload/chunked/{up['upload_id']}/1", content=b"9999")
    st = client.get(f"/v1/upload/chunked/{up['upload_id']}").json()
    assert st == {"received": [1], "chunk_size": 4, "size": 10}


def test_chunked_reput_is_idempotent(app_with_router, monkeypatch):
    import config
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()
    uid = up["upload_id"]
    client.put(f"/v1/upload/chunked/{uid}/0", content=b"AAAA")
    client.put(f"/v1/upload/chunked/{uid}/1", content=b"BBBB")
    # Retry chunk 0 with corrected bytes (the client's blip-retry path).
    client.put(f"/v1/upload/chunked/{uid}/0", content=b"CCCC")
    done = client.post(f"/v1/upload/chunked/{uid}/complete")
    assert done.status_code == 200
    final = agents_dir / "test-agent" / "users" / "alice" / "workspace" / \
        "uploads" / "files" / "video.bin"
    assert final.read_bytes() == b"CCCCBBBB"


# ---------------------------------------------------------------------------
# Validation / guards
# ---------------------------------------------------------------------------


def test_init_rejects_over_cap(app_with_router, monkeypatch):
    import config
    app, _agents, staging_dir, _user = app_with_router
    monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_BYTES", 100)
    client = TestClient(app)
    resp = _init(client, 101)
    assert resp.status_code == 413
    assert "File too large" in resp.json()["detail"]
    assert not staging_dir.exists() or list(staging_dir.iterdir()) == []


def test_init_rejects_unknown_agent_and_bad_size(app_with_router):
    app, _agents, _staging, _user = app_with_router
    client = TestClient(app)
    # An agent outside the caller's grants 403s at require_agent_access —
    # same order as the single-shot route (access before existence).
    assert _init(client, 10, agent="nope").status_code == 403
    assert _init(client, 0).status_code == 400


def test_init_does_not_create_landing_dir(app_with_router):
    """An aborted upload must not leave an empty target dir behind."""
    app, agents_dir, _staging, _user = app_with_router
    client = TestClient(app)
    assert _init(client, 10).status_code == 200
    assert not (agents_dir / "test-agent" / "users" / "alice" / "workspace"
                / "uploads" / "files").exists()


def test_chunk_rejects_wrong_sizes_and_range(app_with_router, monkeypatch):
    import config
    app, _agents, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 10).json()  # chunks: 4, 4, 2
    uid = up["upload_id"]
    assert client.put(f"/v1/upload/chunked/{uid}/3", content=b"zz").status_code == 400
    assert client.put(f"/v1/upload/chunked/{uid}/-1", content=b"zz").status_code == 400
    # Short non-final chunk
    assert client.put(f"/v1/upload/chunked/{uid}/0", content=b"abc").status_code == 400
    # Oversize final chunk (expected 2)
    assert client.put(f"/v1/upload/chunked/{uid}/2", content=b"abc").status_code == 400
    # A rejected chunk is NOT recorded
    st = client.get(f"/v1/upload/chunked/{uid}").json()
    assert st["received"] == []


def test_complete_rejects_missing_chunks_and_keeps_staging(app_with_router, monkeypatch):
    import config
    app, _agents, staging_dir, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()
    client.put(f"/v1/upload/chunked/{up['upload_id']}/0", content=b"AAAA")
    resp = client.post(f"/v1/upload/chunked/{up['upload_id']}/complete")
    assert resp.status_code == 409
    assert "1 chunks missing" in resp.json()["detail"]
    assert (staging_dir / f"{up['upload_id']}.partial").exists()  # resumable


def test_foreign_owner_gets_403(app_with_router, monkeypatch):
    app, _agents, staging_dir, user = app_with_router
    client = TestClient(app)
    up = _init(client, 10).json()
    # Rewrite the meta's owner — the caller is no longer the initiator.
    meta_path = staging_dir / f"{up['upload_id']}.json"
    meta = json.loads(meta_path.read_text())
    meta["sub"] = "someone-else"
    meta_path.write_text(json.dumps(meta))
    assert client.put(
        f"/v1/upload/chunked/{up['upload_id']}/0", content=b"x" * 10,
    ).status_code == 403
    assert client.post(
        f"/v1/upload/chunked/{up['upload_id']}/complete",
    ).status_code == 403
    assert client.delete(f"/v1/upload/chunked/{up['upload_id']}").status_code == 403


def test_hostile_upload_id_is_404_not_traversal(app_with_router):
    app, _agents, staging_dir, _user = app_with_router
    client = TestClient(app)
    # Plant a file OUTSIDE staging that a traversal would reach.
    victim = staging_dir.parent / "victim.json"
    staging_dir.mkdir(parents=True, exist_ok=True)
    victim.write_text(json.dumps({"sub": "user-test-sub", "agent": "test-agent"}))
    # Single-segment hostile id → our regex guard answers 404 before any
    # filesystem path is built. (`..` itself never reaches the handler — URL
    # dot-segment normalization rewrites the path before routing — so use a
    # literal-preserved hostile value.)
    resp = client.get("/v1/upload/chunked/...")
    assert resp.status_code == 404
    # Encoded-slash id never reaches the handler — the router rejects the
    # multi-segment path itself (404/405 depending on sibling routes).
    resp2 = client.get("/v1/upload/chunked/..%2Fvictim")
    assert resp2.status_code in (404, 405)
    assert victim.exists()


def test_reaped_staging_is_410_gone(app_with_router, monkeypatch):
    import config
    app, _agents, staging_dir, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()
    (staging_dir / f"{up['upload_id']}.partial").unlink()
    resp = client.put(f"/v1/upload/chunked/{up['upload_id']}/0", content=b"AAAA")
    assert resp.status_code == 410
    # The 410 also cleared the meta → subsequent calls are a plain 404.
    assert client.get(f"/v1/upload/chunked/{up['upload_id']}").status_code == 404


def test_delete_cleans_staging_and_is_idempotent(app_with_router):
    app, _agents, staging_dir, _user = app_with_router
    client = TestClient(app)
    up = _init(client, 10).json()
    assert client.delete(f"/v1/upload/chunked/{up['upload_id']}").json() == {"ok": True}
    assert list(staging_dir.iterdir()) == []
    assert client.delete(f"/v1/upload/chunked/{up['upload_id']}").json() == {"ok": True}


def test_stale_staging_swept_on_init(app_with_router):
    app, _agents, staging_dir, _user = app_with_router
    client = TestClient(app)
    staging_dir.mkdir(parents=True, exist_ok=True)
    old_partial = staging_dir / "oldid123.partial"
    old_meta = staging_dir / "oldid123.json"
    old_partial.write_bytes(b"x")
    old_meta.write_text("{}")
    stale = time.time() - 25 * 3600
    import os
    os.utime(old_partial, (stale, stale))
    os.utime(old_meta, (stale, stale))
    assert _init(client, 10).status_code == 200
    assert not old_partial.exists()
    assert not old_meta.exists()


def test_complete_schedules_push_with_transfer_id(app_with_router, monkeypatch):
    """Mirror of test_upload_schedules_push_without_awaiting for the chunked
    path: complete mints a transfer id and reports remote_push=True when
    fan-out candidates exist."""
    import config
    from api.media import uploads
    app, _agents, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 8, raising=False)
    scheduled = {}

    def _fake_schedule(agent, rel_path, target, *, transfer_id=None, origin_user_sub=""):
        scheduled.update(agent=agent, rel_path=rel_path, transfer_id=transfer_id)
        return True
    monkeypatch.setattr(uploads, "_schedule_upload_push", _fake_schedule)

    client = TestClient(app)
    payload = b"y" * 20
    up = _init(client, len(payload)).json()
    _upload_all(client, up["upload_id"], payload, up["chunk_size"])
    done = client.post(f"/v1/upload/chunked/{up['upload_id']}/complete").json()
    assert done["remote_push"] is True
    assert done["transfer_id"] == scheduled["transfer_id"]
    assert scheduled["rel_path"] == done["path"]


def test_complete_survives_cross_device_staging(app_with_router, monkeypatch):
    """Docker layout: the agents dir is a named volume while the staging dir
    lives in the container overlay — ``os.replace`` raises EXDEV. Finalize
    must copy into a same-directory temp file and rename atomically (found
    live 2026-09-02: every chunked finalize 500'd on the internal install)."""
    import errno
    import os

    import config
    app, agents_dir, staging_dir, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 8, raising=False)
    client = TestClient(app)

    payload = bytes(range(40))
    up = _init(client, len(payload)).json()
    _upload_all(client, up["upload_id"], payload, 8)

    real_replace = os.replace

    def _exdev_from_staging(src, dst, *a, **kw):
        if str(src).startswith(str(staging_dir)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", _exdev_from_staging)
    done = client.post(f"/v1/upload/chunked/{up['upload_id']}/complete")
    assert done.status_code == 200, done.text

    final = agents_dir / "test-agent" / "users" / "alice" / "workspace" / \
        "uploads" / "files" / "video.bin"
    assert final.read_bytes() == payload
    assert not [p for p in final.parent.iterdir() if p.name.endswith(".part")]
    assert list(staging_dir.iterdir()) == []  # staging + meta gone
