"""Version-pinned preview snapshots: the store
(services/media/preview_snapshots.py), the WOPI snapshot namespace + the
view-only ``/v1/documents/snapshot-wopi-url`` mint (api/media/wopi.py), and
instance-scoped dismissal (storage/db_chats.py).

The frozen "previous version" block's whole trust story lives here: snapshots
are proxy-owned copies outside every agent tree, served only through
chat-access-gated view tokens minted at render time, and pruned by reference.
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from storage import database as task_store


@pytest.fixture
def snap_root(tmp_path, monkeypatch):
    import config
    root = tmp_path / "preview-snapshots"
    monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", root, raising=False)
    return root


def _seed_source(tmp_path, content=b"xlsx bytes"):
    src = tmp_path / "source.xlsx"
    src.write_bytes(content)
    return src


def _add_preview_row(chat_id, file_id, snapshot_id, filename="report.xlsx",
                     dismissed=False):
    data = {
        "type": "document_preview", "wopi_url": "/cool", "filename": filename,
        "file_id": file_id, "download_url": "/d", "snapshot_id": snapshot_id,
        "generation": 1,
    }
    if dismissed:
        data["dismissed"] = True
    return task_store.add_chat_message(
        chat_id, "event", "", event_type="document_preview",
        event_data=json.dumps(data),
    )


# ---------------------------------------------------------------------------
# Store: create / resolve / GC / sweep
# ---------------------------------------------------------------------------


def test_create_and_resolve_roundtrip(temp_db, tmp_path, snap_root):
    from services.media import preview_snapshots as ps
    src = _seed_source(tmp_path, b"as-delivered")
    sid = ps.create_snapshot("chat-1", src)
    assert sid
    # Later writes to the source never reach the snapshot.
    src.write_bytes(b"mutated afterwards")
    p = ps.snapshot_path("chat-1", sid)
    assert p is not None and p.read_bytes() == b"as-delivered"
    # No .tmp residue from the atomic copy.
    assert list((snap_root / "chat-1").glob("*.tmp")) == []


def test_malformed_ids_never_resolve(temp_db, tmp_path, snap_root):
    from services.media import preview_snapshots as ps
    src = _seed_source(tmp_path)
    for chat_id, sid in [("../etc", "x"), ("chat-1", "../../secret"),
                         ("chat/1", "abc"), ("", "abc"), ("chat-1", "")]:
        assert ps.snapshot_path(chat_id, sid) is None
    for bad_chat in ("../etc", "chat/1", "", "a" * 200):
        assert ps.create_snapshot(bad_chat, src) is None


def test_gc_keeps_referenced_drops_unreferenced(temp_db, tmp_path, snap_root):
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    src = _seed_source(tmp_path)
    kept = ps.create_snapshot("chat-1", src)
    dropped = ps.create_snapshot("chat-1", src)
    _add_preview_row("chat-1", "f1", kept)
    _add_preview_row("chat-1", "f1", dropped, dismissed=True)
    # Age both files past the in-flight grace window.
    import os
    old = time.time() - 600
    for sid in (kept, dropped):
        os.utime(snap_root / "chat-1" / sid, (old, old))
    removed = ps.gc_chat("chat-1")
    assert removed == 1
    assert ps.snapshot_path("chat-1", kept) is not None
    assert ps.snapshot_path("chat-1", dropped) is None


def test_gc_age_gate_spares_inflight_snapshot(temp_db, tmp_path, snap_root):
    # A hook-created snapshot whose row has not persisted yet (perm-queue
    # in flight) is unreferenced but FRESH — GC must not eat it.
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    sid = ps.create_snapshot("chat-1", _seed_source(tmp_path))
    assert ps.gc_chat("chat-1") == 0
    assert ps.snapshot_path("chat-1", sid) is not None


def test_sweep_orphans_reaps_deleted_chats_only(temp_db, tmp_path, snap_root, monkeypatch):
    from services.media import preview_snapshots as ps
    monkeypatch.setattr(ps, "_last_sweep", 0.0)
    task_store.create_chat("chat-live", "user-a", "test-agent")
    src = _seed_source(tmp_path)
    live_sid = ps.create_snapshot("chat-live", src)
    ps.create_snapshot("chat-gone", src)
    assert ps.sweep_orphans() == 1
    assert ps.snapshot_path("chat-live", live_sid) is not None
    assert not (snap_root / "chat-gone").exists()
    # Throttled: an immediate second call is a no-op even with new orphans.
    ps.create_snapshot("chat-gone2", src)
    assert ps.sweep_orphans() == 0


# ---------------------------------------------------------------------------
# WOPI: snapshot namespace serving + lock hardening
# ---------------------------------------------------------------------------


def _wopi_config(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents", raising=False)
    monkeypatch.setattr(config, "WOPI_SECRET", "test-wopi-secret", raising=False)
    monkeypatch.setattr(config, "COLLABORA_URL", "https://collabora.example", raising=False)
    monkeypatch.setattr(config, "WOPI_BASE_URL", "https://wopi.example", raising=False)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://app.example", raising=False)


def _wopi_client():
    from api.media import wopi
    app = FastAPI()
    app.include_router(wopi.router)
    return TestClient(app)


def _snapshot_token(chat_id, sid, permissions="view", display_name="report.xlsx"):
    from api.media import wopi
    rel = wopi.snapshot_rel_path(chat_id, sid)
    token, _ = wopi.create_wopi_token(
        rel, "user-a", "Alice", permissions, "test-agent", display_name=display_name,
    )
    return wopi.encode_file_id(rel), token


def test_snapshot_checkfileinfo_and_getfile(temp_db, tmp_path, snap_root, monkeypatch):
    from services.media import preview_snapshots as ps
    _wopi_config(monkeypatch, tmp_path)
    sid = ps.create_snapshot("chat-1", _seed_source(tmp_path, b"pinned bytes"))
    file_id, token = _snapshot_token("chat-1", sid)
    client = _wopi_client()
    info = client.get(f"/wopi/files/{file_id}?access_token={token}").json()
    # Collabora picks its renderer from BaseFileName — the opaque on-disk id
    # has no extension, so the token's display_name must win.
    assert info["BaseFileName"] == "report.xlsx"
    assert info["UserCanWrite"] is False
    body = client.get(f"/wopi/files/{file_id}/contents?access_token={token}")
    assert body.status_code == 200 and body.content == b"pinned bytes"


def test_snapshot_putfile_always_403(temp_db, tmp_path, snap_root, monkeypatch):
    # Defence in depth: even a (never-minted) edit-capable snapshot token
    # must not write into the snapshot cache.
    from services.media import preview_snapshots as ps
    _wopi_config(monkeypatch, tmp_path)
    sid = ps.create_snapshot("chat-1", _seed_source(tmp_path, b"pinned"))
    for perms in ("view", "edit"):
        file_id, token = _snapshot_token("chat-1", sid, permissions=perms)
        r = _wopi_client().post(
            f"/wopi/files/{file_id}/contents?access_token={token}", content=b"evil",
        )
        assert r.status_code == 403
    assert ps.snapshot_path("chat-1", sid).read_bytes() == b"pinned"


def test_lock_ops_require_edit(temp_db, tmp_path, monkeypatch):
    # A view-only session must not place/steal locks (it could 409 the real
    # editor's saves); GET_LOCK stays readable.
    from api.media import wopi
    _wopi_config(monkeypatch, tmp_path)
    monkeypatch.setattr(wopi, "_wopi_locks", {})  # isolate module lock state
    rel = "test-agent/workspace/x.docx"
    f = tmp_path / "agents" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"doc")
    file_id = wopi.encode_file_id(rel)
    client = _wopi_client()

    view_token, _ = wopi.create_wopi_token(rel, "u", "U", "view", "test-agent")
    edit_token, _ = wopi.create_wopi_token(rel, "u", "U", "edit", "test-agent")

    for op in ("LOCK", "UNLOCK", "REFRESH_LOCK"):
        r = client.post(
            f"/wopi/files/{file_id}?access_token={view_token}",
            headers={"X-WOPI-Override": op, "X-WOPI-Lock": "L1"},
        )
        assert r.status_code == 403, op
    r = client.post(
        f"/wopi/files/{file_id}?access_token={edit_token}",
        headers={"X-WOPI-Override": "LOCK", "X-WOPI-Lock": "L1"},
    )
    assert r.status_code == 200
    r = client.post(
        f"/wopi/files/{file_id}?access_token={view_token}",
        headers={"X-WOPI-Override": "GET_LOCK"},
    )
    assert r.status_code == 200 and r.headers.get("X-WOPI-Lock") == "L1"


# ---------------------------------------------------------------------------
# /v1/documents/snapshot-wopi-url — chat-access-gated view-only mint
# ---------------------------------------------------------------------------


def _mint_app(monkeypatch, tmp_path, *, sub="user-a", is_admin=False,
              agents=("test-agent",)):
    _wopi_config(monkeypatch, tmp_path)
    from api.media import wopi
    from auth.providers import UserContext, get_current_user
    user = UserContext(
        sub=sub, email=f"{sub}@t.com", name=sub.title(),
        role="admin" if is_admin else "creator",
        agents=list(agents), agent_roles={a: "manager" for a in agents},
    )

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(wopi.router)
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


def _mint(client, chat_id, sid):
    return client.get(
        f"/v1/documents/snapshot-wopi-url?chat_id={chat_id}&snapshot_id={sid}",
    )


def test_snapshot_url_happy_path_is_view_only(temp_db, tmp_path, snap_root, monkeypatch):
    import jwt as _jwt

    import config
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    client = _mint_app(monkeypatch, tmp_path)
    sid = ps.create_snapshot("chat-1", _seed_source(tmp_path))
    _add_preview_row("chat-1", "f1", sid, filename="budget.xlsx")
    r = _mint(client, "chat-1", sid)
    assert r.status_code == 200
    url = r.json()["wopi_url"]
    assert url.startswith("https://collabora.example/browser/dist/cool.html?WOPISrc=")
    token = url.split("access_token=")[1].split("&")[0]
    claims = _jwt.decode(token, config.WOPI_SECRET, algorithms=["HS256"])
    assert claims["permissions"] == "view"
    assert claims["display_name"] == "budget.xlsx"


def test_snapshot_url_requires_chat_access(temp_db, tmp_path, snap_root, monkeypatch):
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-owner", "test-agent")
    sid = ps.create_snapshot("chat-1", _seed_source(tmp_path))
    _add_preview_row("chat-1", "f1", sid)
    stranger = _mint_app(monkeypatch, tmp_path, sub="user-stranger")
    assert _mint(stranger, "chat-1", sid).status_code == 403
    admin = _mint_app(monkeypatch, tmp_path, sub="user-admin", is_admin=True)
    assert _mint(admin, "chat-1", sid).status_code == 200


def test_snapshot_url_404s(temp_db, tmp_path, snap_root, monkeypatch):
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    client = _mint_app(monkeypatch, tmp_path)
    # Unknown chat.
    assert _mint(client, "chat-none", "abc").status_code == 404
    # Snapshot with no referencing row (also: another chat's row can't serve it).
    orphan = ps.create_snapshot("chat-1", _seed_source(tmp_path))
    assert _mint(client, "chat-1", orphan).status_code == 404
    # Dismissed reference no longer serves.
    dismissed = ps.create_snapshot("chat-1", _seed_source(tmp_path))
    _add_preview_row("chat-1", "f1", dismissed, dismissed=True)
    assert _mint(client, "chat-1", dismissed).status_code == 404
    # Referenced but pruned file → 404 (dashboard degrades to chip).
    pruned = "a" * 32
    _add_preview_row("chat-1", "f1", pruned)
    assert _mint(client, "chat-1", pruned).status_code == 404


# ---------------------------------------------------------------------------
# Instance-scoped dismissal (storage layer)
# ---------------------------------------------------------------------------


def test_dismiss_scoped_by_snapshot_spares_live(temp_db):
    task_store.create_chat("chat-1", "user-a", "test-agent")
    _add_preview_row("chat-1", "f1", "snap-old")
    _add_preview_row("chat-1", "f1", "snap-live")
    count, freed = task_store.dismiss_document_previews(
        "chat-1", "f1", snapshot_id="snap-old",
    )
    assert count == 1 and freed == ["snap-old"]
    assert task_store.get_referenced_preview_snapshot_ids("chat-1") == {"snap-live"}
    assert task_store.get_preview_event_by_snapshot("chat-1", "snap-old") is None
    assert task_store.get_preview_event_by_snapshot("chat-1", "snap-live") is not None


def test_dismiss_scoped_by_message_id_for_presnapshot_rows(temp_db):
    task_store.create_chat("chat-1", "user-a", "test-agent")
    old_row = _add_preview_row("chat-1", "f1", "")
    _add_preview_row("chat-1", "f1", "snap-live")
    count, freed = task_store.dismiss_document_previews(
        "chat-1", "f1", db_message_id=old_row,
    )
    assert count == 1 and freed == []
    assert task_store.get_referenced_preview_snapshot_ids("chat-1") == {"snap-live"}


def test_dismiss_unscoped_takes_whole_trail(temp_db):
    task_store.create_chat("chat-1", "user-a", "test-agent")
    _add_preview_row("chat-1", "f1", "s1")
    _add_preview_row("chat-1", "f1", "s2")
    _add_preview_row("chat-1", "f2", "other")
    count, freed = task_store.dismiss_document_previews("chat-1", "f1")
    assert count == 2 and set(freed) == {"s1", "s2"}
    assert task_store.get_referenced_preview_snapshot_ids("chat-1") == {"other"}
