"""Tests for `/v1/upload` save-path routing.

Chat-attached files (no
`target_dir`) now land in `users/<u>/workspace/uploads/files/<name>`
instead of workspace root. Workspace-page uploads (with explicit
`target_dir`) are unaffected.

We avoid the full app lifespan (DB schema init, MCP scan, scheduler)
by mounting just `uploads.router` on a minimal FastAPI app and
overriding the auth + helper deps so the test exercises the path
resolution branches without DB/auth ceremony — same approach as
`tests/media/test_collabora_proxy.py`.
"""

import asyncio
import os
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_router(tmp_path, monkeypatch):
    """Mount only the uploads router and stub out auth + agent existence."""
    import config
    from api.media import uploads
    from auth.providers import UserContext

    # Redirect AGENTS_DIR so we don't touch real workspace files
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()  # the root exists on every install; the code never creates it
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    # `config.get_agent_dir` reads AGENTS_DIR at call-time (it's a function);
    # the helper used by uploads.py:133 is `config.get_agent_dir`. Confirm
    # we're hitting the same function — it is monkeypatched implicitly via
    # the module attribute swap above.

    # Override the auth dependency to return a fixed manager user
    user = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="creator", agents=["test-agent"],
        agent_roles={"test-agent": "manager"},
    )

    async def _stub_user():
        return user

    # Stub agent_store + task_store calls inside the endpoint
    from storage import database as task_store
    from storage.agents import agent_store
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice" if sub == "user-test-sub" else None)
    # Skip the satellite-push side effect (no remote sessions in tests).
    async def _noop_push(*a, **kw):
        return None
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _noop_push)

    app = FastAPI()
    app.include_router(uploads.router)
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir


# ---------------------------------------------------------------------------
# Behavior — chat upload (no target_dir) lands in uploads/files
# ---------------------------------------------------------------------------


def test_chat_upload_lands_in_uploads_files_default(app_with_router):
    """No `target_dir` → file lands in `users/<u>/workspace/uploads/files/<name>`."""
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("doc.pdf", BytesIO(b"PDF data"), "application/pdf")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # `path` returned in response is relative to agent_dir (not AGENTS_DIR).
    assert body["path"] == "users/alice/workspace/uploads/files/doc.pdf"
    assert body["filename"] == "doc.pdf"
    # File actually exists on disk in the new subfolder
    expected = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files" / "doc.pdf"
    assert expected.is_file()
    assert expected.read_bytes() == b"PDF data"
    # AND nothing leaked into workspace root
    workspace_root = agents_dir / "test-agent" / "users" / "alice" / "workspace"
    root_files = [p for p in workspace_root.iterdir() if p.is_file()]
    assert root_files == [], f"workspace root should stay clean, found: {root_files}"


# ---------------------------------------------------------------------------
# No extension allowlist — any file type uploads (agents run full dev
# environments; the serving layer forces non-inert types to attachment)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,mime", [
    ("page.html", "text/html"),
    ("art.psd", "image/vnd.adobe.photoshop"),
    ("print.gcode", "text/x.gcode"),
    ("drawing.dwg", "application/acad"),
    ("tool.exe", "application/octet-stream"),
    ("Makefile", "application/octet-stream"),  # extensionless
])
def test_any_file_type_uploads(app_with_router, name, mime):
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": (name, BytesIO(b"payload"), mime)},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == f"users/alice/workspace/uploads/files/{name}"


# ---------------------------------------------------------------------------
# Workspace-page regression — explicit target_dir is honored exactly
# ---------------------------------------------------------------------------


def test_workspace_target_dir_unchanged(app_with_router):
    """`target_dir=workspace/research` → file lands at `workspace/research/<name>` exactly.

    Confirms we did not auto-prefix `uploads/files/` onto explicit target_dirs
    — that would break the workspace-page upload UX where the user picks
    their own folder.
    """
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("note.md", BytesIO(b"# notes"), "text/markdown")},
        data={"agent": "test-agent", "target_dir": "workspace/research"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "workspace/research/note.md"
    assert "uploads/files" not in body["path"], "no auto-prefix injection"
    expected = agents_dir / "test-agent" / "workspace" / "research" / "note.md"
    assert expected.is_file()


def test_workspace_user_target_dir_unchanged(app_with_router):
    """`target_dir=users/alice/workspace/portraits` → respected exactly."""
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("avatar.png", BytesIO(b"\x89PNG"), "image/png")},
        data={"agent": "test-agent", "target_dir": "users/alice/workspace/portraits"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "users/alice/workspace/portraits/avatar.png"


# ---------------------------------------------------------------------------
# Filename sanitization + conflict resolution still work after the change
# ---------------------------------------------------------------------------


def test_chat_upload_filename_conflict_resolved(app_with_router):
    """Two uploads with the same name produce `<n>` and `<n>_1`."""
    app, agents_dir = app_with_router
    client = TestClient(app)

    for _ in range(2):
        resp = client.post(
            "/v1/upload",
            files={"file": ("notes.md", BytesIO(b"x"), "text/markdown")},
            data={"agent": "test-agent"},
        )
        assert resp.status_code == 200

    uploads_dir = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    names = sorted(p.name for p in uploads_dir.iterdir())
    assert names == ["notes.md", "notes_1.md"]


# ---------------------------------------------------------------------------
# Agent-scoped uploads (internal-agent chats)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_internal_agent(tmp_path, monkeypatch):
    """Same fixture as `app_with_router` but the agent is internal (agent-scoped chats)."""
    import config
    from api.media import uploads
    from auth.providers import UserContext

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()  # the root exists on every install; the code never creates it
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

    user = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="creator", agents=["internal-bot"],
        agent_roles={"internal-bot": "manager"},
    )

    async def _stub_user():
        return user

    from core.session import visibility as _vis
    from storage import database as task_store
    from storage.agents import agent_store
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "internal-bot")
    monkeypatch.setattr(_vis, "is_shared_only", lambda name: name == "internal-bot")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice" if sub == "user-test-sub" else None)
    async def _noop_push(*a, **kw):
        return None
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _noop_push)

    app = FastAPI()
    app.include_router(uploads.router)
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir, user


def test_agent_scoped_upload_lands_in_workspace(app_with_internal_agent):
    """Internal-agent chat → file lands in `workspace/uploads/files/<name>`,
    NOT under any user dir. Manager uploading to set up the bot."""
    app, agents_dir, _ = app_with_internal_agent
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("playbook.md", BytesIO(b"# steps"), "text/markdown")},
        data={"agent": "internal-bot"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "workspace/uploads/files/playbook.md"
    expected = agents_dir / "internal-bot" / "workspace" / "uploads" / "files" / "playbook.md"
    assert expected.is_file()
    assert expected.read_bytes() == b"# steps"
    # Nothing leaked into a per-user dir
    users_dir = agents_dir / "internal-bot" / "users"
    assert not users_dir.exists() or not any(users_dir.iterdir())


def test_agent_scoped_upload_rejects_viewer(app_with_internal_agent, monkeypatch):
    """Viewers cannot post into an internal agent's shared `/workspace/`.
    Path policy permits the write at the OS level (agent-scoped sessions
    can write `/workspace/`), but for the API caller we add a per-agent
    workspace-tier check — defense in depth."""
    app, _, user = app_with_internal_agent
    # Demote: per-agent viewer + platform member (defense in depth)
    user.agent_roles["internal-bot"] = "viewer"
    user.role = "member"
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("playbook.md", BytesIO(b"# steps"), "text/markdown")},
        data={"agent": "internal-bot"},
    )
    assert resp.status_code == 403, resp.text
    assert "contributor" in resp.text.lower()


@pytest.mark.parametrize("role", ["contributor", "editor"])
def test_agent_scoped_upload_accepts_the_workspace_tier(app_with_internal_agent, role):
    """An upload into a Shared-only chat is a shared-workspace write: the
    workspace tier, not the owner tier (editors used to be refused here)."""
    app, agents_dir, user = app_with_internal_agent
    user.agent_roles["internal-bot"] = role
    user.role = "member"
    resp = TestClient(app).post(
        "/v1/upload",
        files={"file": ("site.md", BytesIO(b"# day"), "text/markdown")},
        data={"agent": "internal-bot"},
    )
    assert resp.status_code == 200, resp.text
    assert (agents_dir / "internal-bot" / "workspace" / "uploads" / "files" / "site.md").is_file()


# ---------------------------------------------------------------------------
# Behavior — satellite push is backgrounded, never awaited by the endpoint
# ---------------------------------------------------------------------------


def test_upload_schedules_push_without_awaiting(app_with_router, monkeypatch):
    """`/v1/upload` must return once the platform write lands: the satellite
    push is handed to the turn-start barrier registry as a background task
    (`core/remote/upload_inflight.track`) instead of being awaited in-request.
    The push coroutine here would hang forever if the endpoint awaited it."""
    app, agents_dir = app_with_router
    from api.media import uploads
    from core.remote import upload_inflight
    from services.remote import workspace_fanout

    monkeypatch.setattr(
        workspace_fanout, "has_fanout_candidates", lambda *a, **kw: True,
    )

    async def hang_forever(*a, **kw):  # would deadlock a synchronous await
        await asyncio.Event().wait()

    monkeypatch.setattr(
        uploads, "_push_upload_to_active_remote_sessions", hang_forever,
    )

    tracked: list[str] = []

    def fake_track(agent_slug, coro):
        tracked.append(agent_slug)
        coro.close()  # don't leave an un-awaited coroutine behind
        return None

    monkeypatch.setattr(upload_inflight, "track", fake_track)

    client = TestClient(app)
    resp = client.post(
        "/v1/upload",
        files={"file": ("big.bin", BytesIO(b"x" * 4096), "application/octet-stream")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    assert tracked == ["test-agent"]
    # The platform write landed before the response — that's what makes the
    # background push safe (the workspace listing reads the platform dir).
    saved = agents_dir / "test-agent" / resp.json()["path"]
    assert saved.is_file()


def test_upload_skips_push_scheduling_without_candidates(app_with_router, monkeypatch):
    """Local-only installs (no connected satellite that could receive the
    file) schedule nothing — no stray background task per upload."""
    app, _ = app_with_router
    from core.remote import upload_inflight
    from services.remote import workspace_fanout

    monkeypatch.setattr(
        workspace_fanout, "has_fanout_candidates", lambda *a, **kw: False,
    )
    tracked: list[str] = []
    monkeypatch.setattr(
        upload_inflight, "track",
        lambda slug, coro: (tracked.append(slug), coro.close()),
    )

    client = TestClient(app)
    resp = client.post(
        "/v1/upload",
        files={"file": ("note.txt", BytesIO(b"hello"), "text/plain")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    assert tracked == []


# ---------------------------------------------------------------------------
# Universal cap + atomic .partial staging (Feature D, 1.4.0)
# ---------------------------------------------------------------------------


def _no_partials(root):
    return [p for p in root.rglob("*.partial")]


def test_upload_cap_reads_config_live_and_413_leaves_no_partial(
    app_with_router, monkeypatch,
):
    import config
    app, agents_dir = app_with_router
    client = TestClient(app)
    monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_BYTES", 10)

    resp = client.post(
        "/v1/upload",
        files={"file": ("big.bin", BytesIO(b"x" * 11), "application/octet-stream")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 413
    # Neither the final file nor a torn .partial may exist after a 413.
    agent_root = agents_dir / "test-agent"
    assert not (agent_root / "users/alice/workspace/uploads/files/big.bin").exists()
    assert _no_partials(agents_dir) == []


def test_upload_success_leaves_no_partial(app_with_router):
    app, agents_dir = app_with_router
    client = TestClient(app)
    resp = client.post(
        "/v1/upload",
        files={"file": ("ok.bin", BytesIO(b"data!"), "application/octet-stream")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    assert _no_partials(agents_dir) == []
    saved = agents_dir / "test-agent" / "users/alice/workspace/uploads/files/ok.bin"
    assert saved.read_bytes() == b"data!"


def test_upload_at_cap_boundary_succeeds(app_with_router, monkeypatch):
    import config
    app, agents_dir = app_with_router
    client = TestClient(app)
    monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_BYTES", 10)
    resp = client.post(
        "/v1/upload",
        files={"file": ("edge.bin", BytesIO(b"x" * 10), "application/octet-stream")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Upload → transfer correlation (Feature E, 1.4.0)
# ---------------------------------------------------------------------------


def test_upload_response_carries_transfer_id_and_remote_push(app_with_router):
    """No connected satellite in the fixture → remote_push False; transfer_id
    is always minted so the client can correlate if phase 2 ever starts."""
    app, agents_dir = app_with_router
    client = TestClient(app)
    resp = client.post(
        "/v1/upload",
        files={"file": ("doc.pdf", BytesIO(b"PDF data"), "application/pdf")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transfer_id"]
    assert body["remote_push"] is False  # has_fanout_candidates → none in tests


# ---------------------------------------------------------------------------
# Landing-dir confinement — the RESOLVED destination must stay in the agent
# ---------------------------------------------------------------------------


def test_upload_refuses_landing_dir_symlinked_out_of_agent(app_with_router, tmp_path):
    """A symlink planted where the upload would land (``users/<u>/workspace/
    uploads`` → outside the agent tree) is refused, not followed: the
    destination check runs on the resolved path, before any mkdir."""
    app, agents_dir = app_with_router
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = agents_dir / "test-agent" / "users" / "alice" / "workspace"
    ws.mkdir(parents=True)
    os.symlink(outside, ws / "uploads")

    resp = TestClient(app).post(
        "/v1/upload",
        files={"file": ("doc.pdf", BytesIO(b"PDF data"), "application/pdf")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 403, resp.text
    assert list(outside.rglob("*")) == []


# ---------------------------------------------------------------------------
# The single-shot write: off the loop, floored, never through a planted link
# ---------------------------------------------------------------------------


def _post_file(client, name="note.txt", data=b"hello", agent="test-agent"):
    return client.post("/v1/upload", files={"file": (name, BytesIO(data))},
                       data={"agent": agent})


def test_single_shot_writes_and_fsyncs_off_the_loop(app_with_router, monkeypatch):
    import threading
    app, agents_dir = app_with_router
    seen = set()
    real_fsync = os.fsync

    def _fsync(fd):
        seen.add(threading.current_thread().name)
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", _fsync)
    resp = _post_file(TestClient(app), data=b"x" * (3 * 1024 * 1024))
    assert resp.status_code == 200, resp.text
    assert seen and all(n.startswith("file-commit") for n in seen)
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    assert [p.name for p in landed.iterdir()] == ["note.txt"]
    assert (landed / "note.txt").stat().st_size == 3 * 1024 * 1024


def test_single_shot_refuses_below_the_free_disk_floor(app_with_router, monkeypatch):
    import shutil
    from collections import namedtuple

    import config
    app, _agents_dir = app_with_router
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 5)
    monkeypatch.setattr(config, "MIN_FREE_DISK_PCT", 0)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(100 << 20, 99 << 20, 1 << 20))
    assert _post_file(TestClient(app)).status_code == 507


def test_single_shot_failure_leaves_no_partial(app_with_router, monkeypatch):
    from api.media import uploads
    app, agents_dir = app_with_router

    def _boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(uploads, "_flush_fsync_close", _boom)
    assert _post_file(TestClient(app)).status_code == 500
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    assert [p.name for p in landed.iterdir()] == []


def test_single_shot_refusals_answer_403_like_the_chunked_routes(app_with_router, monkeypatch):
    """A confinement refusal after the destination was resolved (a component
    swapped for a link in the window) is the caller's 403, never a 500, at
    the temp open and at the final rename alike; no temp is left behind."""
    from api.media import uploads
    from services.infra import safe_fs
    app, agents_dir = app_with_router
    real_open, real_rename = safe_fs.open_beneath, safe_fs.rename_beneath

    def _refuse_open(*a, **kw):
        raise safe_fs.SymlinkRefused("a component is a link")
    monkeypatch.setattr(uploads.safe_fs, "open_beneath", _refuse_open)
    resp = _post_file(TestClient(app))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Path outside agent directory"
    assert _no_partials(agents_dir) == []
    monkeypatch.setattr(uploads.safe_fs, "open_beneath", real_open)

    def _refuse_rename(*a, **kw):
        raise safe_fs.EscapeRefused("the path left the root")
    monkeypatch.setattr(uploads.safe_fs, "rename_beneath", _refuse_rename)
    resp = _post_file(TestClient(app))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Path outside agent directory"
    assert _no_partials(agents_dir) == []
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    assert [p.name for p in landed.iterdir()] == []
    monkeypatch.setattr(uploads.safe_fs, "rename_beneath", real_rename)

    # A plain filesystem error at the open is a logged 500, still no temp.
    def _not_a_dir(*a, **kw):
        raise NotADirectoryError("files is a file")
    monkeypatch.setattr(uploads.safe_fs, "mkdirs_beneath", _not_a_dir)
    resp = _post_file(TestClient(app))
    assert resp.status_code == 500 and resp.json()["detail"] == "Upload failed"
    assert _no_partials(agents_dir) == []


def test_single_shot_never_writes_through_a_planted_link(app_with_router, tmp_path):
    app, agents_dir = app_with_router
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    landed.mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    (landed / "note.txt").symlink_to(target)
    resp = _post_file(TestClient(app))
    assert resp.status_code == 200 and resp.json()["filename"] == "note_1.txt"
    assert target.read_bytes() == b"untouched"
    assert (landed / "note_1.txt").read_bytes() == b"hello"


# ---------------------------------------------------------------------------
# A folder attached in chat: a fresh folder in the chat's landing dir
# ---------------------------------------------------------------------------


def test_a_chat_folder_is_reserved_fresh_and_its_files_land_in_it(app_with_router):
    """The first folder takes its name, a second of the same name takes
    ``_1`` (never merged into the first), and an upload into the reserved
    folder's subfolder creates the subfolder."""
    app, agents_dir = app_with_router
    client = TestClient(app)
    first = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "mesitis"})
    assert first.status_code == 200, first.text
    assert first.json() == {"path": "users/alice/workspace/uploads/files/mesitis", "name": "mesitis"}
    second = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "mesitis"})
    assert second.json()["path"] == "users/alice/workspace/uploads/files/mesitis_1"
    resp = client.post(
        "/v1/upload",
        files={"file": ("agent.json", BytesIO(b"{}"), "application/json")},
        data={"agent": "test-agent", "target_dir": f"{second.json()['path']}/context"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "users/alice/workspace/uploads/files/mesitis_1/context/agent.json"
    assert (agents_dir / "test-agent" / resp.json()["path"]).read_bytes() == b"{}"


def test_a_landed_upload_and_a_reserved_folder_refresh_the_open_trees(app_with_router, monkeypatch):
    """The uploader's own workspace view (a folder attached in chat shows
    without a Refresh) and a teammate's hear ``file_updated``: once per
    landed file and once per reserved folder, the uploader never excluded."""
    from unittest.mock import AsyncMock

    from services.notifications import notification_manager
    told = AsyncMock()
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", told)
    app, _ = app_with_router
    client = TestClient(app)
    assert _post_file(client, name="note.txt").status_code == 200
    folder = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "pics"})
    assert folder.status_code == 200, folder.text
    assert told.await_args_list == [
        (("test-agent", "users/alice/workspace/uploads/files/note.txt"), {"source": "disk"}),
        (("test-agent", "users/alice/workspace/uploads/files/pics"), {"source": "disk"}),
    ]


def test_a_chat_folder_name_never_makes_a_hidden_or_nested_folder(app_with_router):
    app, _ = app_with_router
    client = TestClient(app)
    hidden = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": ".drafts"})
    assert hidden.json()["path"] == "users/alice/workspace/uploads/files/drafts"
    nested = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "../../config"})
    assert nested.status_code == 200
    assert nested.json()["path"].startswith("users/alice/workspace/uploads/files/")
    assert "/" not in nested.json()["name"]


def test_a_chat_folder_in_a_shared_chat_needs_the_workspace_tier(app_with_internal_agent):
    app, _, user = app_with_internal_agent
    user.agent_roles["internal-bot"] = "viewer"
    user.role = "member"
    resp = TestClient(app).post("/v1/upload/folder", json={"agent": "internal-bot", "name": "t"})
    assert resp.status_code == 403


def test_a_chat_folder_in_a_shared_chat_takes_a_contributor(app_with_internal_agent):
    app, agents_dir, user = app_with_internal_agent
    user.agent_roles["internal-bot"] = "contributor"
    user.role = "member"
    resp = TestClient(app).post("/v1/upload/folder", json={"agent": "internal-bot", "name": "site"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "workspace/uploads/files/site"
    assert (agents_dir / "internal-bot" / "workspace" / "uploads" / "files" / "site").is_dir()


def test_a_viewer_of_a_per_user_agent_uploads_into_their_own_workspace(app_with_router):
    """A viewer writes their own user directory and nothing else: a chat
    upload and a chat folder land; the shared workspace is refused."""
    app, agents_dir = app_with_router
    from auth.providers import UserContext, get_current_user
    viewer = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="member", agents=["test-agent"], agent_roles={"test-agent": "viewer"},
    )

    async def _viewer():
        return viewer
    app.dependency_overrides[get_current_user] = _viewer
    client = TestClient(app)
    assert _post_file(client).json()["path"] == "users/alice/workspace/uploads/files/note.txt"
    folder = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "mine"})
    assert folder.json()["path"] == "users/alice/workspace/uploads/files/mine"
    shared = client.post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "x", "target_dir": "workspace"})
    assert shared.status_code == 403
    assert not (agents_dir / "test-agent" / "workspace").exists()


def test_every_upload_route_refuses_an_anonymous_caller(app_with_router):
    app, _ = app_with_router
    from auth.providers import get_current_user

    async def _nobody():
        return None
    app.dependency_overrides[get_current_user] = _nobody
    client = TestClient(app)
    assert _post_file(client).status_code == 401
    assert client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "x"}).status_code == 401
    assert client.post("/v1/upload/chunked/init", json={
        "agent": "test-agent", "filename": "a", "size": 1}).status_code == 401


# ---------------------------------------------------------------------------
# Names: bytes, not characters; a name the file system refuses is a 400
# ---------------------------------------------------------------------------


def test_a_long_multibyte_name_lands_cut_to_bytes(app_with_router):
    """90 CJK characters are 270 bytes, past NAME_MAX: the stem is cut to
    200 bytes on a character boundary and the extension kept."""
    app, agents_dir = app_with_router
    resp = _post_file(TestClient(app), name="文" * 90 + ".txt")
    assert resp.status_code == 200, resp.text
    name = resp.json()["filename"]
    assert name == "文" * 66 + ".txt"
    assert (agents_dir / "test-agent" / resp.json()["path"]).read_bytes() == b"hello"


def test_sanitize_filename_caps_stem_and_extension_by_bytes():
    from api.media.uploads import _sanitize_filename
    assert _sanitize_filename("a" * 300 + ".md") == "a" * 200 + ".md"
    # A longer "extension" is part of the stem.
    long_ext = "report." + "x" * 30
    assert _sanitize_filename(long_ext) == long_ext
    assert len(_sanitize_filename("r." + "é" * 200).encode("utf-8")) <= 200
    assert _sanitize_filename("é" * 150 + ".pdf") == "é" * 100 + ".pdf"


def test_a_target_dir_component_over_name_max_is_a_400(app_with_router):
    app, agents_dir = app_with_router
    client = TestClient(app)
    long_dir = "users/alice/workspace/" + "d" * 256
    resp = client.post("/v1/upload", files={"file": ("a.txt", BytesIO(b"x"))},
                       data={"agent": "test-agent", "target_dir": long_dir})
    assert resp.status_code == 400 and "too long" in resp.json()["detail"]
    resp = client.post("/v1/upload/chunked/init", json={
        "agent": "test-agent", "filename": "a.txt", "size": 1, "target_dir": long_dir})
    assert resp.status_code == 400
    resp = client.post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "x", "target_dir": long_dir})
    assert resp.status_code == 400
    assert _no_partials(agents_dir) == []


def test_a_name_the_file_system_refuses_is_a_400_never_a_500(app_with_router, monkeypatch):
    """ENAMETOOLONG at the temp open, at the final rename and at a folder
    reservation answers a plain 400; no temp is left behind."""
    import errno

    from api.media import uploads
    app, agents_dir = app_with_router
    client = TestClient(app)

    def _too_long(*a, **kw):
        raise OSError(errno.ENAMETOOLONG, "File name too long")
    real_open = uploads._open_landing_temp
    monkeypatch.setattr(uploads, "_open_landing_temp", _too_long)
    resp = _post_file(client)
    assert resp.status_code == 400 and "too long" in resp.json()["detail"]
    monkeypatch.setattr(uploads, "_open_landing_temp", real_open)

    monkeypatch.setattr(uploads, "_rename_to_free_name", _too_long)
    resp = _post_file(client)
    assert resp.status_code == 400 and "too long" in resp.json()["detail"]
    assert _no_partials(agents_dir) == []

    monkeypatch.setattr(uploads, "_reserve_folder", _too_long)
    resp = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "x"})
    assert resp.status_code == 400 and "too long" in resp.json()["detail"]


def test_a_chunked_complete_refused_for_its_name_is_a_400(app_with_router, tmp_path, monkeypatch):
    import errno

    import config
    from api.media import uploads
    app, _agents_dir = app_with_router
    monkeypatch.setattr(config, "UPLOAD_STAGING_DIR", tmp_path / "upload-staging")
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 8, raising=False)
    uploads._chunk_locks.clear()
    client = TestClient(app)
    init = client.post("/v1/upload/chunked/init", json={
        "agent": "test-agent", "filename": "big.bin", "size": 8}).json()
    assert client.put(f"/v1/upload/chunked/{init['upload_id']}/0", content=b"x" * 8).status_code == 200

    def _too_long(*a, **kw):
        raise OSError(errno.ENAMETOOLONG, "File name too long")
    monkeypatch.setattr(uploads, "_finalize_staged_file", _too_long)
    resp = client.post(f"/v1/upload/chunked/{init['upload_id']}/complete")
    assert resp.status_code == 400 and "too long" in resp.json()["detail"]


def test_a_control_character_in_the_target_dir_is_a_400(app_with_router):
    app, agents_dir = app_with_router
    client = TestClient(app)
    resp = client.post("/v1/upload", files={"file": ("a.txt", BytesIO(b"x"))},
                       data={"agent": "test-agent", "target_dir": "users/alice/workspace/a\x01b"})
    assert resp.status_code == 400
    resp = client.post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "x", "target_dir": "users/alice/workspace/\nx"})
    assert resp.status_code == 400
    assert not (agents_dir / "test-agent" / "users").exists()


def test_409_once_a_hundred_names_are_taken(app_with_router):
    app, agents_dir = app_with_router
    landed = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    landed.mkdir(parents=True)
    (landed / "note.txt").write_bytes(b"0")
    (landed / "t").mkdir()
    for i in range(1, 100):
        (landed / f"note_{i}.txt").write_bytes(b"0")
        (landed / f"t_{i}").mkdir()
    client = TestClient(app)
    resp = _post_file(client)
    assert resp.status_code == 409, resp.text
    assert _no_partials(agents_dir) == []
    assert client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "t"}).status_code == 409


# ---------------------------------------------------------------------------
# A folder dropped or picked in the workspace: reserved where it goes
# ---------------------------------------------------------------------------


def test_a_workspace_folder_is_reserved_in_its_target_dir(app_with_router):
    """Two drops of one folder land as ``A`` and ``A_1`` in the folder on
    show; the name keeps its spaces and brackets and loses leading dots."""
    app, agents_dir = app_with_router
    client = TestClient(app)
    body = {"agent": "test-agent", "name": "My Folder (2)", "target_dir": "workspace/research"}
    first = client.post("/v1/upload/folder", json=body)
    second = client.post("/v1/upload/folder", json=body)
    assert first.status_code == 200, first.text
    assert first.json() == {"path": "workspace/research/My Folder (2)", "name": "My Folder (2)"}
    assert second.json()["path"] == "workspace/research/My Folder (2)_1"
    hidden = client.post("/v1/upload/folder", json={**body, "name": ".config"})
    assert hidden.json()["path"] == "workspace/research/config"
    assert (agents_dir / "test-agent" / "workspace" / "research" / "My Folder (2)_1").is_dir()


@pytest.mark.parametrize("target_dir", ["users/bob/workspace", "config/../users/bob"])
def test_a_workspace_folder_is_refused_outside_the_callers_scope(app_with_router, target_dir):
    app, agents_dir = app_with_router
    from auth.providers import UserContext, get_current_user
    editor = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="member", agents=["test-agent"], agent_roles={"test-agent": "editor"},
    )

    async def _editor():
        return editor
    app.dependency_overrides[get_current_user] = _editor
    resp = TestClient(app).post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "x", "target_dir": target_dir})
    assert resp.status_code in (400, 403), resp.text
    assert not (agents_dir / "test-agent" / "users" / "bob").exists()


def test_a_folder_reservation_never_lands_through_a_symlinked_folder(app_with_router, tmp_path):
    app, agents_dir = app_with_router
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = agents_dir / "test-agent" / "users" / "alice" / "workspace"
    ws.mkdir(parents=True)
    os.symlink(outside, ws / "uploads")
    os.symlink(outside, ws / "linked")
    client = TestClient(app)
    chat = client.post("/v1/upload/folder", json={"agent": "test-agent", "name": "x"})
    assert chat.status_code == 403, chat.text
    picked = client.post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "x", "target_dir": "users/alice/workspace/linked"})
    assert picked.status_code == 403, picked.text
    assert list(outside.iterdir()) == []


def test_a_link_named_like_the_folder_is_passed_over_never_followed(app_with_router):
    """``workspace/A`` is a link into the caller's own folder: the dropped
    ``A`` is reserved as ``workspace/A_1``, never where the link points."""
    app, agents_dir = app_with_router
    deep = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "deep" / "B"
    deep.mkdir(parents=True)
    (agents_dir / "test-agent" / "workspace").mkdir()
    os.symlink(deep, agents_dir / "test-agent" / "workspace" / "A")
    resp = TestClient(app).post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "A", "target_dir": "workspace"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "workspace/A_1"
    assert list(deep.iterdir()) == []


def test_a_reservation_judges_the_folder_it_made(app_with_router):
    """A viewer may write its own ``users/alice`` folder, but asking for a
    folder named ``alice`` in ``users/`` would make ``users/alice_1``, which
    it may not write: refused, and nothing is left behind."""
    app, agents_dir = app_with_router
    from auth.providers import UserContext, get_current_user
    viewer = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="member", agents=["test-agent"], agent_roles={"test-agent": "viewer"},
    )

    async def _viewer():
        return viewer
    app.dependency_overrides[get_current_user] = _viewer
    (agents_dir / "test-agent" / "users" / "alice").mkdir(parents=True)
    resp = TestClient(app).post("/v1/upload/folder", json={
        "agent": "test-agent", "name": "alice", "target_dir": "users"})
    assert resp.status_code == 403, resp.text
    assert sorted(p.name for p in (agents_dir / "test-agent" / "users").iterdir()) == ["alice"]


def test_a_lone_surrogate_is_dropped_from_a_name_and_refused_in_a_path(app_with_router):
    """A name from a Windows machine can carry a lone surrogate, which JSON
    passes as an escape: dropped from a folder name, a 400 in a path."""
    app, _agents_dir = app_with_router
    client = TestClient(app)

    def post(raw: str):
        return client.post("/v1/upload/folder", content=raw,
                           headers={"Content-Type": "application/json"})
    named = post('{"agent": "test-agent", "name": "a\\ud800b", "target_dir": "workspace"}')
    assert named.status_code == 200, named.text
    assert named.json()["name"] == "ab"
    pathed = post('{"agent": "test-agent", "name": "x", "target_dir": "workspace/a\\ud800b"}')
    assert pathed.status_code == 400, pathed.text


@pytest.mark.parametrize("raw,expected", [
    ("My Folder (2)", "My Folder (2)"),
    ("a/b\\c", "a_b_c"),
    ("tab\there\x7f", "tabhere"),
    ("  . .hidden  ", "hidden"),
    ("..", None),
    (".", None),
    ("   ", None),
    ("\x00\x01", None),
    ("文" * 100, "文" * 66),
])
def test_folder_name_rules(raw, expected):
    from fastapi import HTTPException

    from api.media.uploads import _folder_name
    if expected is None:
        with pytest.raises(HTTPException) as exc:
            _folder_name(raw)
        assert exc.value.status_code == 400
    else:
        assert _folder_name(raw) == expected
