"""Version-pinned preview snapshots: the store
(services/media/preview_snapshots.py), the WOPI snapshot namespace + the
view-only ``/v1/documents/snapshot-wopi-url`` mint (api/media/wopi.py), and
instance-scoped dismissal (storage/chat/db_previews.py).

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
    # The pushed file lives in an agent tree (the hook resolved it there).
    import config
    ws = config.AGENTS_DIR / "test-agent" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    src = ws / "source.xlsx"
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
    # No temp residue from the atomic copy.
    assert [x.name for x in (snap_root / "chat-1").iterdir()] == [sid]


def test_snapshot_refuses_symlinked_source(temp_db, tmp_path, snap_root):
    """A file swapped for a link before the push copies it is not pinned:
    the preview degrades to no pinned version instead of a copy of
    whatever the link points at."""
    from services.media import preview_snapshots as ps
    src = _seed_source(tmp_path, b"real")
    secret = tmp_path / "config.env"
    secret.write_text("JWT_SECRET=1\n")
    src.unlink()
    src.symlink_to(secret)
    assert ps.create_snapshot("chat-1", src) is None
    assert not (snap_root / "chat-1").exists()
    # A source outside every agent tree is refused the same way.
    assert ps.create_snapshot("chat-1", secret) is None


def test_malformed_ids_never_resolve(temp_db, tmp_path, snap_root):
    from services.media import preview_snapshots as ps
    src = _seed_source(tmp_path)
    for chat_id, sid in [("../etc", "x"), ("chat-1", "../../secret"),
                         ("chat/1", "abc"), ("", "abc"), ("chat-1", "")]:
        assert ps.snapshot_path(chat_id, sid) is None
    for bad_chat in ("../etc", "chat/1", "", "a" * 200):
        assert ps.create_snapshot(bad_chat, src) is None


def test_oversized_source_skips_snapshot(temp_db, tmp_path, snap_root, monkeypatch):
    # Pinning copies the whole document — past the cap the preview degrades
    # to no pinned version instead of a giant disk copy (536MB incident).
    from services.media import preview_snapshots as ps
    monkeypatch.setattr(ps, "_MAX_SNAPSHOT_BYTES", 64)
    big = _seed_source(tmp_path, b"x" * 65)
    assert ps.create_snapshot("chat-1", big) is None
    assert not (snap_root / "chat-1").exists()  # no dir, no .tmp residue
    small = _seed_source(tmp_path, b"x" * 64)
    assert ps.create_snapshot("chat-1", small) is not None


def test_gc_keeps_referenced_drops_unreferenced(temp_db, tmp_path, snap_root, monkeypatch):
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    src = _seed_source(tmp_path)
    kept = ps.create_snapshot("chat-1", src)
    dropped = ps.create_snapshot("chat-1", src)
    _add_preview_row("chat-1", "f1", kept)
    _add_preview_row("chat-1", "f1", dropped, dismissed=True)
    # Ten minutes on: both copies are past the in-flight grace window.
    later = time.time() + 600
    monkeypatch.setattr(ps, "_clock", lambda: later)
    removed = ps.gc_chat("chat-1")
    assert removed == 1
    assert ps.snapshot_path("chat-1", kept) is not None
    assert ps.snapshot_path("chat-1", dropped) is None


def test_gc_age_gate_spares_inflight_snapshot(temp_db, tmp_path, snap_root):
    # A hook-created snapshot whose row has not persisted yet (perm-queue
    # in flight) is unreferenced but FRESH — GC must not eat it, even when
    # the source (whose times the copy keeps) was last written long ago.
    import os
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    src = _seed_source(tmp_path)
    old = time.time() - 3600
    os.utime(src, (old, old))
    sid = ps.create_snapshot("chat-1", src)
    assert (snap_root / "chat-1" / sid).stat().st_mtime < time.time() - 3000
    assert ps.gc_chat("chat-1") == 0
    assert ps.snapshot_path("chat-1", sid) is not None


def test_the_newest_row_of_a_file_is_the_one_a_mint_reads(temp_db, tmp_path, snap_root):
    task_store.create_chat("chat-1", "user-a", "test-agent")
    _add_preview_row("chat-1", "f1", "s1", filename="old.xlsx")
    _add_preview_row("chat-1", "f2", "s2", filename="other.xlsx")
    _add_preview_row("chat-1", "f1", "s3", filename="new.xlsx")
    assert task_store.get_preview_event_by_file("chat-1", "f1")["snapshot_id"] == "s3"
    assert task_store.count_preview_rows("chat-1", "f1") == 2


def test_a_push_numbers_itself_and_caps_the_files_versions(temp_db, tmp_path, snap_root, monkeypatch):
    from services.media import preview_snapshots as ps
    monkeypatch.setattr(ps, "MAX_VERSIONS_PER_FILE", 3)
    task_store.create_chat("chat-1", "user-a", "test-agent")
    src = _seed_source(tmp_path)
    assert ps.stamp_and_cap("chat-1", "f1") == 1
    sids = []
    for _ in range(4):
        sid = ps.create_snapshot("chat-1", src)
        sids.append(sid)
        _add_preview_row("chat-1", "f1", sid)
    other = ps.create_snapshot("chat-1", src)
    _add_preview_row("chat-1", "f2", other)
    # The fifth push: the newest two persisted keep their copies (the push
    # itself is the third), the older two lose theirs, the rows stay.
    assert ps.stamp_and_cap("chat-1", "f1") == 5
    assert [ps.snapshot_path("chat-1", s) is not None for s in sids] == [False, False, True, True]
    assert ps.snapshot_path("chat-1", other) is not None
    assert task_store.count_preview_rows("chat-1", "f1") == 4


def _refresh_rig(tmp_path, snap_root):
    import os
    from services.media import preview_snapshots as ps
    task_store.create_chat("chat-1", "user-a", "test-agent")
    src = _seed_source(tmp_path, b"pushed bytes")
    os.utime(src, (1_700_000_000, 1_700_000_000))
    sid = ps.create_snapshot("chat-1", src)
    _add_preview_row("chat-1", "f1", sid)
    st = src.stat()
    return ps, sid, (st.st_size, st.st_mtime_ns, None)


def test_a_pane_save_refreshes_the_version_that_mirrors_the_file(temp_db, tmp_path, snap_root):
    ps, sid, before = _refresh_rig(tmp_path, snap_root)
    saved_ns = 1_700_000_500 * 10**9
    assert ps.refresh_newest("chat-1", "f1", None, b"pushed bytes, edited", saved_ns, before)
    copy = snap_root / "chat-1" / sid
    assert copy.read_bytes() == b"pushed bytes, edited"
    # The copy takes the save's time: the next save's check compares it.
    assert copy.stat().st_mtime_ns == saved_ns
    st_before = (len(b"pushed bytes, edited"), saved_ns, None)
    assert ps.refresh_newest("chat-1", "f1", None, b"second save", saved_ns + 10**9, st_before)
    assert copy.read_bytes() == b"second save"


def test_a_version_the_file_moved_away_from_is_left_alone(temp_db, tmp_path, snap_root):
    # The agent changed the file without a push: the save's "before" no
    # longer matches the version, which keeps the push's bytes.
    ps, sid, (size, mtime_ns, _digest) = _refresh_rig(tmp_path, snap_root)
    assert not ps.refresh_newest("chat-1", "f1", None, b"x", 1, (size + 1, mtime_ns, None))
    assert not ps.refresh_newest("chat-1", "f1", None, b"x", 1, (size, mtime_ns + 5, None))
    assert not ps.refresh_newest("chat-1", "f1", None, b"x", 1, (size, mtime_ns + 5, "0" * 64))
    assert (snap_root / "chat-1" / sid).read_bytes() == b"pushed bytes"


def test_the_same_bytes_with_another_time_still_mirror(temp_db, tmp_path, snap_root):
    import hashlib
    ps, sid, (size, mtime_ns, _digest) = _refresh_rig(tmp_path, snap_root)
    digest = hashlib.sha256(b"pushed bytes").hexdigest()
    assert ps.refresh_newest("chat-1", "f1", None, b"edited", 1, (size, mtime_ns + 5, digest))


def test_the_pending_push_is_the_newest_version(temp_db, tmp_path, snap_root):
    # A push the pump still holds for the turn's flush is newer than the
    # persisted one: the save refreshes it, never the older version.
    import os
    ps, sid, before = _refresh_rig(tmp_path, snap_root)
    src = _seed_source(tmp_path, b"pushed bytes")
    os.utime(src, (1_700_000_000, 1_700_000_000))
    pending = ps.create_snapshot("chat-1", src)
    assert ps.refresh_newest("chat-1", "f1", pending, b"edited", 1, before)
    assert (snap_root / "chat-1" / pending).read_bytes() == b"edited"
    assert (snap_root / "chat-1" / sid).read_bytes() == b"pushed bytes"


def test_a_pruned_or_absent_version_is_never_recreated(temp_db, tmp_path, snap_root):
    ps, sid, before = _refresh_rig(tmp_path, snap_root)
    ps.delete_snapshot("chat-1", sid)
    assert not ps.refresh_newest("chat-1", "f1", None, b"edited", 1, before)
    assert not (snap_root / "chat-1" / sid).exists()


def test_a_newest_row_without_a_copy_never_falls_back_to_an_older_one(temp_db, tmp_path, snap_root):
    ps, sid, before = _refresh_rig(tmp_path, snap_root)
    _add_preview_row("chat-1", "f1", "")
    assert not ps.refresh_newest("chat-1", "f1", None, b"edited", 1, before)
    assert (snap_root / "chat-1" / sid).read_bytes() == b"pushed bytes"


def test_a_save_past_the_snapshot_cap_refreshes_nothing(temp_db, tmp_path, snap_root, monkeypatch):
    ps, sid, before = _refresh_rig(tmp_path, snap_root)
    monkeypatch.setattr(ps, "_MAX_SNAPSHOT_BYTES", 4)
    assert not ps.refresh_newest("chat-1", "f1", None, b"edited", 1, before)
    assert (snap_root / "chat-1" / sid).read_bytes() == b"pushed bytes"


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


def test_lock_ops_refuse_an_undecodable_or_foreign_file_id(temp_db, tmp_path, monkeypatch):
    # The read routes' answer: a file_id that is not base64, or not UTF-8
    # once decoded, or that names another path than the token, is 403.
    from api.media import wopi
    _wopi_config(monkeypatch, tmp_path)
    monkeypatch.setattr(wopi, "_wopi_locks", {})
    rel = "test-agent/workspace/x.docx"
    token, _ = wopi.create_wopi_token(rel, "u", "U", "edit", "test-agent")
    client = _wopi_client()
    foreign = wopi.encode_file_id("test-agent/workspace/other.docx")
    for file_id in ("not-base64!", "__4", foreign):
        for op in ("GET_LOCK", "LOCK", "UNLOCK", "REFRESH_LOCK"):
            r = client.post(
                f"/wopi/files/{file_id}?access_token={token}",
                headers={"X-WOPI-Override": op, "X-WOPI-Lock": "L1"},
            )
            assert r.status_code == 403, (file_id, op)
    assert wopi._wopi_locks == {}


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
    assert "access_token" not in url
    claims = _jwt.decode(r.json()["access_token"], config.WOPI_SECRET, algorithms=["HS256"])
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


# ---------------------------------------------------------------------------
# /v1/documents/preview-wopi-url — render-time re-mint for the LIVE block
# ---------------------------------------------------------------------------
# Preview events persist the push-time URL, whose token lasts 4h — reopening
# an older chat gave "session expired" with no recovery. The dashboard mints
# fresh at render through this endpoint; these tests pin its gates.


def _seed_workspace_file(tmp_path, rel="test-agent/workspace/report.xlsx"):
    import config
    from api.media.wopi import encode_file_id
    full = config.AGENTS_DIR / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"doc bytes")
    return encode_file_id(rel)


def _remint(client, chat_id, file_id):
    return client.get(
        f"/v1/documents/preview-wopi-url?chat_id={chat_id}&file_id={file_id}",
    )


def _claims(body):
    import jwt as _jwt

    import config
    assert "access_token" not in body["wopi_url"]
    return _jwt.decode(body["access_token"], config.WOPI_SECRET, algorithms=["HS256"])


def test_preview_remint_recomputes_requester_permission(temp_db, tmp_path, monkeypatch):
    task_store.create_chat("chat-1", "user-a", "test-agent")
    manager = _mint_app(monkeypatch, tmp_path)
    fid = _seed_workspace_file(tmp_path)
    _add_preview_row("chat-1", fid, "")
    r = _remint(manager, "chat-1", fid)
    assert r.status_code == 200
    body = r.json()
    assert body["wopi_url"].startswith(
        "https://collabora.example/browser/dist/cool.html?WOPISrc="
    )
    assert body["permissions"] == "edit"
    assert _claims(body)["permissions"] == "edit"
    # Same chat owner with NO per-agent role → viewer → view-only token.
    viewer = _mint_app(monkeypatch, tmp_path, agents=())
    body = _remint(viewer, "chat-1", fid).json()
    assert body["permissions"] == "view"
    assert _claims(body)["permissions"] == "view"


def test_preview_remint_requires_chat_access(temp_db, tmp_path, monkeypatch):
    task_store.create_chat("chat-1", "user-owner", "test-agent")
    stranger = _mint_app(monkeypatch, tmp_path, sub="user-stranger")
    fid = _seed_workspace_file(tmp_path)  # after _mint_app points AGENTS_DIR
    _add_preview_row("chat-1", fid, "")
    assert _remint(stranger, "chat-1", fid).status_code == 403
    admin = _mint_app(monkeypatch, tmp_path, sub="user-admin", is_admin=True)
    assert _remint(admin, "chat-1", fid).status_code == 200


def test_preview_remint_404s(temp_db, tmp_path, monkeypatch):
    from api.media.wopi import encode_file_id
    task_store.create_chat("chat-1", "user-a", "test-agent")
    client = _mint_app(monkeypatch, tmp_path)
    fid = _seed_workspace_file(tmp_path)
    # Unknown chat.
    assert _remint(client, "chat-none", fid).status_code == 404
    # No referencing event in this chat.
    assert _remint(client, "chat-1", fid).status_code == 404
    # Dismissed trail no longer re-mints.
    _add_preview_row("chat-1", fid, "", dismissed=True)
    assert _remint(client, "chat-1", fid).status_code == 404
    # Referenced but the file is gone → 404 (dashboard falls back).
    gone = encode_file_id("test-agent/workspace/deleted.xlsx")
    _add_preview_row("chat-1", gone, "")
    assert _remint(client, "chat-1", gone).status_code == 404
    # Snapshot-namespace ids have their own endpoint — never served here.
    snap_fid = encode_file_id(".preview-snapshots/chat-1/abc")
    _add_preview_row("chat-1", snap_fid, "")
    assert _remint(client, "chat-1", snap_fid).status_code == 404
    # Malformed file_id (bad base64) — but only with a matching event row,
    # which can't exist; the event gate 404s first.
    assert _remint(client, "chat-1", "%%bad%%").status_code == 404


def test_preview_remint_host_cache_is_session_scoped(temp_db, tmp_path, monkeypatch):
    import config
    from api.media.wopi import encode_file_id
    task_store.create_chat("chat-1", "user-a", "test-agent")
    task_store.update_chat("chat-1", session_id="sess-1")
    client = _mint_app(monkeypatch, tmp_path)  # manager role
    # A mirror in the CHAT'S OWN session cache → edit for write-capable roles.
    own_rel = ".remote-host-cache/sess-1/d1/notes.docx"
    (config.AGENTS_DIR / own_rel).parent.mkdir(parents=True, exist_ok=True)
    (config.AGENTS_DIR / own_rel).write_bytes(b"host bytes")
    own_fid = encode_file_id(own_rel)
    _add_preview_row("chat-1", own_fid, "")
    body = _remint(client, "chat-1", own_fid).json()
    assert body["permissions"] == "edit"
    # ANOTHER session's cache: still readable through the chat's live event,
    # but never editable — write-back is scoped to the chat's own session.
    other_rel = ".remote-host-cache/sess-other/d2/notes.docx"
    (config.AGENTS_DIR / other_rel).parent.mkdir(parents=True, exist_ok=True)
    (config.AGENTS_DIR / other_rel).write_bytes(b"host bytes")
    other_fid = encode_file_id(other_rel)
    _add_preview_row("chat-1", other_fid, "")
    body = _remint(client, "chat-1", other_fid).json()
    assert body["permissions"] == "view"
