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
    # The agents root exists on every install; the code never creates it.
    agents_dir.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(config, "UPLOAD_STAGING_DIR", staging_dir)
    uploads._chunk_locks.clear()

    user = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="creator", agents=["test-agent"],
        agent_roles={"test-agent": "manager"},
    )

    async def _stub_user():
        return user

    from storage.agents import agent_store
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
    assert _staged_files(staging_dir) == []  # staging + meta gone


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
    from api.media import uploads
    assert uploads._staging_paths(up["upload_id"], _user.sub)[0].exists()  # resumable


def test_foreign_owner_gets_403(app_with_router, monkeypatch):
    app, _agents, staging_dir, user = app_with_router
    client = TestClient(app)
    up = _init(client, 10).json()
    # Rewrite the meta's owner — the caller is no longer the initiator.
    from api.media import uploads
    _staging, meta_path = uploads._staging_paths(up["upload_id"], user.sub)
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
    from api.media import uploads
    app, _agents, staging_dir, user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()
    staging, _meta = uploads._staging_paths(up["upload_id"], user.sub)
    assert client.put(f"/v1/upload/chunked/{up['upload_id']}/1", content=b"BBBB").status_code == 200
    assert up["upload_id"] in uploads._chunk_locks
    staging.unlink()
    resp = client.put(f"/v1/upload/chunked/{up['upload_id']}/0", content=b"AAAA")
    assert resp.status_code == 410
    # The 410 also cleared the meta → subsequent calls are a plain 404, and
    # the upload's lock is gone with it.
    assert client.get(f"/v1/upload/chunked/{up['upload_id']}").status_code == 404
    assert up["upload_id"] not in uploads._chunk_locks


def _staged_files(staging_dir):
    return sorted(p for p in staging_dir.rglob("*") if p.is_file())


def test_delete_cleans_staging_and_is_idempotent(app_with_router):
    app, _agents, staging_dir, _user = app_with_router
    client = TestClient(app)
    up = _init(client, 10).json()
    assert client.delete(f"/v1/upload/chunked/{up['upload_id']}").json() == {"ok": True}
    assert _staged_files(staging_dir) == []
    assert client.delete(f"/v1/upload/chunked/{up['upload_id']}").json() == {"ok": True}


def _age(path, seconds):
    import os
    stale = time.time() - seconds
    os.utime(path, (stale, stale))


def test_init_never_sweeps(app_with_router, monkeypatch):
    """The sweep left the request path: init costs the caller's own admission
    and nothing that scales with what other users left behind."""
    from api.media import uploads
    app, _agents, _staging, _user = app_with_router
    called = []
    monkeypatch.setattr(uploads, "sweep_stale_staging", lambda: called.append(1) or [])
    assert _init(TestClient(app), 10).status_code == 200
    assert called == []


def test_sweep_reaps_stale_pairs_and_pops_only_idle_locks(app_with_router, monkeypatch):
    import asyncio
    import config
    from api.media import uploads
    app, _agents, staging_dir, user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    # (a) chunks received, idle past the 24 h TTL: reaped, lock popped
    old = _init(client, 8).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{old}/0", content=b"AAAA").status_code == 200
    for p in uploads._staging_paths(old, user.sub):
        _age(p, 25 * 3600)
    # (b) no chunk at all, older than the first-chunk window: reaped
    empty = _init(client, 8).json()["upload_id"]
    for p in uploads._staging_paths(empty, user.sub):
        _age(p, config.UPLOAD_FIRST_CHUNK_S + 60)
    # (c) no chunk yet, but young: kept
    young = _init(client, 8).json()["upload_id"]
    # (d) chunks received an hour ago: kept
    live = _init(client, 8).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{live}/0", content=b"AAAA").status_code == 200
    for p in uploads._staging_paths(live, user.sub):
        _age(p, 3600)
    # (e) a pre-upgrade flat pair at the root, past the TTL: reaped
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "oldflat01.partial").write_bytes(b"x")
    (staging_dir / "oldflat01.json").write_text("{}")
    _age(staging_dir / "oldflat01.partial", 25 * 3600)
    _age(staging_dir / "oldflat01.json", 25 * 3600)
    # (f) a swept id whose lock is held keeps the lock (the holder answers 410)
    uploads._chunk_locks[old].locked() is False
    held = asyncio.Lock()
    asyncio.run(held.acquire())
    uploads._chunk_locks[empty] = held

    swept = uploads.sweep_stale_staging()
    assert set(swept) == {old, empty, "oldflat01"}
    assert not any(p.exists() for p in uploads._staging_paths(old, user.sub))
    assert not any(p.exists() for p in uploads._staging_paths(empty, user.sub))
    assert not (staging_dir / "oldflat01.partial").exists()
    assert all(p.exists() for p in uploads._staging_paths(young, user.sub))
    assert all(p.exists() for p in uploads._staging_paths(live, user.sub))
    uploads.release_swept_locks(swept)
    assert old not in uploads._chunk_locks
    assert empty in uploads._chunk_locks  # held: left for its holder
    assert live in uploads._chunk_locks


def test_open_uploads_are_capped_per_user_and_an_idle_pair_is_evicted_first(
        app_with_router, monkeypatch):
    import config
    from api.media import uploads
    app, _agents, _staging, user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_MAX_OPEN", 2)
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    first = _init(client, 10).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{first}/0", content=b"AAAA").status_code == 200
    assert first in uploads._chunk_locks
    assert _init(client, 10).status_code == 200
    r = _init(client, 10)
    assert r.status_code == 429, r.text
    assert "UPLOAD_MAX_OPEN" in r.text
    # An interrupted upload (no chunk in the idle window) gives its slot up,
    # and its idle lock goes with it.
    for p in uploads._staging_paths(first, user.sub):
        _age(p, uploads._IDLE_EVICT_S + 60)
    assert _init(client, 10).status_code == 200
    assert not any(p.exists() for p in uploads._staging_paths(first, user.sub))
    assert first not in uploads._chunk_locks
    assert _init(client, 10).status_code == 429
    monkeypatch.setattr(config, "UPLOAD_MAX_OPEN", 0)  # 0 = no cap
    assert _init(client, 10).status_code == 200


def test_an_admission_that_evicts_and_still_refuses_releases_the_evicted_lock(
        app_with_router, monkeypatch):
    import config
    from api.media import uploads
    app, _agents, _staging, user = app_with_router
    chunk = 64 * 1024
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", chunk, raising=False)
    monkeypatch.setattr(config, "UPLOAD_STAGING_USER_MB", 1)
    monkeypatch.setattr(config, "UPLOAD_MAX_OPEN", 0)
    client = TestClient(app)
    idle = _init(client, 2 * chunk).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{idle}/0", content=b"A" * chunk).status_code == 200
    for p in uploads._staging_paths(idle, user.sub):
        _age(p, uploads._IDLE_EVICT_S + 60)
    # The idle pair is evicted, and the request is over the cap on its own.
    r = _init(client, 2 * 1024 * 1024)
    assert r.status_code == 429 and "UPLOAD_STAGING_USER_MB" in r.text
    assert not any(p.exists() for p in uploads._staging_paths(idle, user.sub))
    assert idle not in uploads._chunk_locks


def test_a_chunk_whose_pair_is_evicted_in_flight_answers_410_and_leaves_no_meta(
        app_with_router, monkeypatch):
    """The eviction removes the pair while a chunk is written to the open
    staging file: the chunk's commit must not re-create the meta."""
    import config
    from api.media import uploads
    app, _agents, _staging, user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{up}/0", content=b"AAAA").status_code == 200
    staging, meta_path = uploads._staging_paths(up, user.sub)
    real_open = uploads._open_chunk_target

    def _open_then_evict(*a, **kw):
        f = real_open(*a, **kw)
        staging.unlink()
        meta_path.unlink()
        return f
    monkeypatch.setattr(uploads, "_open_chunk_target", _open_then_evict)
    r = client.put(f"/v1/upload/chunked/{up}/1", content=b"BBBB")
    assert r.status_code == 410, r.text
    assert not meta_path.exists() and not staging.exists()
    assert up not in uploads._chunk_locks


def _drop_meta_after_next_read(monkeypatch, meta_path):
    """The next read of ``meta_path`` returns the meta and then removes it,
    as a DELETE that ran between a route's first read and its lock would."""
    from pathlib import Path
    real_read = Path.read_text
    armed = {"on": True}

    def _read(self, *a, **kw):
        text = real_read(self, *a, **kw)
        if armed["on"] and self == meta_path:
            armed["on"] = False
            meta_path.unlink()
        return text
    monkeypatch.setattr(Path, "read_text", _read)


def test_a_meta_gone_under_the_lock_leaves_no_lock(app_with_router, monkeypatch):
    import config
    from api.media import uploads
    app, _agents, _staging, user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    # PUT: the upload already holds a chunk, so its lock outlives a failed PUT
    # unless the upload itself is gone.
    up = _init(client, 8).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{up}/0", content=b"AAAA").status_code == 200
    assert up in uploads._chunk_locks
    _drop_meta_after_next_read(monkeypatch, uploads._staging_paths(up, user.sub)[1])
    assert client.put(f"/v1/upload/chunked/{up}/1", content=b"BBBB").status_code == 404
    assert up not in uploads._chunk_locks
    # Complete.
    done = _init(client, 8).json()["upload_id"]
    _upload_all(client, done, b"ABCDEFGH", 4)
    assert done in uploads._chunk_locks
    _drop_meta_after_next_read(monkeypatch, uploads._staging_paths(done, user.sub)[1])
    assert client.post(f"/v1/upload/chunked/{done}/complete").status_code == 404
    assert done not in uploads._chunk_locks


def test_staged_bytes_are_capped_per_user_by_real_occupancy(app_with_router, monkeypatch):
    import config
    app, _agents, _staging, _user = app_with_router
    chunk = 64 * 1024
    mb = 1024 * 1024
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", chunk, raising=False)
    monkeypatch.setattr(config, "UPLOAD_STAGING_USER_MB", 2)
    monkeypatch.setattr(config, "UPLOAD_MAX_OPEN", 0)
    client = TestClient(app)
    # An open upload counts what it holds on disk, not what it declared: a
    # 1.5 MB declaration with one chunk landed is 64 KB, so a second 1.5 MB
    # declaration still fits under 2 MB (declared sizes would sum to 3 MB).
    big = _init(client, 3 * mb // 2).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{big}/0", content=b"A" * chunk).status_code == 200
    assert _init(client, 3 * mb // 2).status_code == 200
    # Seven more chunks on the big one (512 KB staged): a 1.6 MB request is
    # refused, a 100 KB one is not, and the new upload's own size counts.
    for i in range(1, 8):
        assert client.put(f"/v1/upload/chunked/{big}/{i}", content=b"A" * chunk).status_code == 200
    r = _init(client, 16 * mb // 10)
    assert r.status_code == 429 and "UPLOAD_STAGING_USER_MB" in r.text
    assert _init(client, 100 * 1024).status_code == 200
    assert _init(client, 3 * mb).status_code == 429


def test_the_global_open_cap_answers_503(app_with_router, monkeypatch):
    import config
    app, _agents, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_MAX_OPEN_TOTAL", 1)
    client = TestClient(app)
    assert _init(client, 10).status_code == 200
    assert _init(client, 10).status_code == 503


def _no_disk(monkeypatch):
    import shutil
    import config
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 5)
    monkeypatch.setattr(config, "MIN_FREE_DISK_PCT", 0)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(100 << 20, 99 << 20, 1 << 20))


def test_the_free_disk_floor_refuses_every_write(app_with_router, monkeypatch):
    import config
    from api.media import uploads
    app, _agents, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()["upload_id"]
    _no_disk(monkeypatch)
    assert _init(client, 8).status_code == 507
    assert client.put(f"/v1/upload/chunked/{up}/0", content=b"AAAA").status_code == 507
    # The floor is checked in the thread that writes, never on the loop.
    import threading
    seen = []
    real = uploads._free_disk_ok

    def _spy(path, incoming):
        seen.append(threading.current_thread().name)
        return real(path, incoming)
    monkeypatch.setattr(uploads, "_free_disk_ok", _spy)
    assert _init(client, 8).status_code == 507
    assert seen and all(not n.startswith("MainThread") and "anyio" not in n for n in seen)


def test_a_same_filesystem_complete_ignores_the_floor_and_the_copy_path_honours_it(
        app_with_router, monkeypatch):
    import errno
    import config
    from services.infra import safe_fs
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    payload = b"ABCDEFGH"
    up = _init(client, 8).json()["upload_id"]
    _upload_all(client, up, payload, 4)
    _no_disk(monkeypatch)
    assert client.post(f"/v1/upload/chunked/{up}/complete").status_code == 200
    up2 = _init(client, 8, filename="two.bin")
    # (init is refused under the floor; lift it for the init, then re-arm it)
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 0)
    up2 = _init(client, 8, filename="two.bin").json()["upload_id"]
    _upload_all(client, up2, payload, 4)
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 5)
    real = safe_fs.rename_beneath

    def _exdev(*a, **kw):
        raise OSError(errno.EXDEV, "cross-device")
    monkeypatch.setattr(safe_fs, "rename_beneath", _exdev)
    assert client.post(f"/v1/upload/chunked/{up2}/complete").status_code == 507
    monkeypatch.setattr(safe_fs, "rename_beneath", real)


def test_a_failed_first_put_leaves_no_lock(app_with_router, monkeypatch):
    import config
    from api.media import uploads
    app, _agents, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{up}/0", content=b"").status_code == 400
    assert up not in uploads._chunk_locks
    assert client.put(f"/v1/upload/chunked/{up}/0", content=b"AAAA").status_code == 200
    assert up in uploads._chunk_locks


def test_a_duplicate_complete_answers_404_with_one_file_landed(app_with_router, monkeypatch):
    import config
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    up = _init(client, 8).json()["upload_id"]
    _upload_all(client, up, b"ABCDEFGH", 4)
    assert client.post(f"/v1/upload/chunked/{up}/complete").status_code == 200
    assert client.post(f"/v1/upload/chunked/{up}/complete").status_code == 404
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    assert sorted(p.name for p in landed.iterdir()) == ["video.bin"]


def test_two_completes_of_one_name_land_two_files(app_with_router, monkeypatch):
    import config
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    a = _init(client, 4).json()["upload_id"]
    b = _init(client, 4).json()["upload_id"]
    _upload_all(client, a, b"AAAA", 4)
    _upload_all(client, b, b"BBBB", 4)
    assert client.post(f"/v1/upload/chunked/{a}/complete").json()["filename"] == "video.bin"
    assert client.post(f"/v1/upload/chunked/{b}/complete").json()["filename"] == "video_1.bin"
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    assert (landed / "video.bin").read_bytes() == b"AAAA"
    assert (landed / "video_1.bin").read_bytes() == b"BBBB"


def test_a_planted_link_at_the_landing_name_is_never_written_through(
        app_with_router, monkeypatch, tmp_path):
    import config
    app, agents_dir, _staging, _user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    landed.mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    (landed / "video.bin").symlink_to(target)
    up = _init(client, 4).json()["upload_id"]
    _upload_all(client, up, b"AAAA", 4)
    done = client.post(f"/v1/upload/chunked/{up}/complete")
    assert done.status_code == 200 and done.json()["filename"] == "video_1.bin"
    assert target.read_bytes() == b"untouched"
    assert (landed / "video_1.bin").read_bytes() == b"AAAA"


def test_writes_fsync_and_finalize_run_off_the_loop(app_with_router, monkeypatch):
    import os
    import threading
    from pathlib import Path
    import config
    from api.media import uploads
    app, _agents, staging_dir, user = app_with_router
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 4, raising=False)
    client = TestClient(app)
    threads = {"fsync": set(), "finalize": set(), "meta_read": set(), "unlink": set()}
    real_fsync = os.fsync

    def _fsync(fd):
        threads["fsync"].add(threading.current_thread().name)
        return real_fsync(fd)
    real_finalize = uploads._finalize_staged_file

    def _finalize(*a, **kw):
        threads["finalize"].add(threading.current_thread().name)
        return real_finalize(*a, **kw)
    real_read, real_unlink = Path.read_text, Path.unlink

    def _read(self, *a, **kw):
        if self.suffix == ".json" and self.is_relative_to(staging_dir):
            threads["meta_read"].add(threading.current_thread().name)
        return real_read(self, *a, **kw)

    def _unlink(self, *a, **kw):
        if self.is_relative_to(staging_dir):
            threads["unlink"].add(threading.current_thread().name)
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(uploads, "_finalize_staged_file", _finalize)
    monkeypatch.setattr(Path, "read_text", _read)
    monkeypatch.setattr(Path, "unlink", _unlink)
    up = _init(client, 8).json()["upload_id"]
    _upload_all(client, up, b"ABCDEFGH", 4)
    assert client.get(f"/v1/upload/chunked/{up}").status_code == 200
    assert client.post(f"/v1/upload/chunked/{up}/complete").status_code == 200
    # A torn staging file: complete's size probe and its unlinks.
    torn = _init(client, 8).json()["upload_id"]
    _upload_all(client, torn, b"ABCDEFGH", 4)
    os.truncate(uploads._staging_paths(torn, user.sub)[0], 3)
    assert client.post(f"/v1/upload/chunked/{torn}/complete").status_code == 409
    # A reaped staging file: the 410's unlink.
    gone = _init(client, 8).json()["upload_id"]
    os.remove(uploads._staging_paths(gone, user.sub)[0])
    assert client.put(f"/v1/upload/chunked/{gone}/0", content=b"AAAA").status_code == 410
    # An abort: its read and its unlinks.
    aborted = _init(client, 8).json()["upload_id"]
    assert client.delete(f"/v1/upload/chunked/{aborted}").json() == {"ok": True}
    assert _staged_files(staging_dir) == []
    for kind, names in threads.items():
        assert names and all(n.startswith("file-commit") for n in names), (kind, names)


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

    from services.infra import safe_fs
    real_rename = safe_fs.rename_beneath
    calls = []

    def _exdev_from_staging(root, src_rel, dst_rel, **kw):
        calls.append(str(src_rel))
        if str(src_rel).startswith("upload-staging/"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(root, src_rel, dst_rel, **kw)

    monkeypatch.setattr(safe_fs, "rename_beneath", _exdev_from_staging)
    done = client.post(f"/v1/upload/chunked/{up['upload_id']}/complete")
    assert done.status_code == 200, done.text

    final = agents_dir / "test-agent" / "users" / "alice" / "workspace" / \
        "uploads" / "files" / "video.bin"
    assert final.read_bytes() == payload
    assert os.stat(final).st_mode & 0o777 == 0o644
    assert [p.name for p in final.parent.iterdir()] == ["video.bin"]  # no temp survives
    assert _staged_files(staging_dir) == []  # staging + meta gone


def test_staged_bytes_for_one_user(app_with_router, monkeypatch):
    """The quota monitor reads one user's staged bytes without walking the
    other users' staging directories."""
    import config
    from api.media import uploads
    app, _agents, _staging, user = app_with_router
    chunk = 64 * 1024
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", chunk, raising=False)
    monkeypatch.setattr(config, "UPLOAD_MAX_OPEN", 0)
    client = TestClient(app)
    assert uploads.staged_bytes_for(user.sub) == 0
    up = _init(client, 4 * chunk).json()["upload_id"]
    assert client.put(f"/v1/upload/chunked/{up}/0", content=b"A" * chunk).status_code == 200
    assert uploads.staged_bytes_for(user.sub) == chunk
    assert uploads.staged_bytes_for("local:someone-else") == 0
    assert uploads.staged_bytes_total() == chunk
