"""Tests for Docker MCP satellite-host path support.

Covers:
  * Satellite-side ``_validate_satellite_host_path`` defensive checks.
  * Proxy-side ``remote_file_flow`` cache-path helpers + host-cache
    detection.
  * Hook integration: ``/v1/hooks/resolve-path`` dispatches sandbox-virtual
    vs satellite-host; ``/v1/hooks/file-written`` dispatches host-cache
    paths to push-back-host vs the legacy push-back.
"""

import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
_REPO_ROOT = _PROXY_DIR.parent
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from auth.path_policy import SecurityContext  # noqa: E402
from core.remote import remote_file_flow as rff # noqa: E402
from satellite.sessions.session_manager import _validate_satellite_host_path  # noqa: E402


# ---------------------------------------------------------------------------
# Satellite-side defensive validation
# ---------------------------------------------------------------------------


class TestValidateSatelliteHostPath:
    def test_accepts_absolute_unix(self):
        _validate_satellite_host_path("/etc/hosts")
        _validate_satellite_host_path("/home/alice/Desktop/foo.png")

    def test_accepts_windows_drive_forward(self):
        _validate_satellite_host_path("c:/Users/erin/Desktop/foo.png")

    def test_accepts_windows_drive_backslash(self):
        _validate_satellite_host_path("c:\\Users\\erin\\Desktop\\foo.png")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_satellite_host_path("")

    def test_rejects_relative(self):
        with pytest.raises(ValueError, match="absolute"):
            _validate_satellite_host_path("foo.png")
        with pytest.raises(ValueError, match="absolute"):
            _validate_satellite_host_path("Desktop/foo.png")

    def test_rejects_nul(self):
        with pytest.raises(ValueError, match="NUL"):
            _validate_satellite_host_path("/foo\x00/bar")

    def test_rejects_traversal_segment(self):
        with pytest.raises(ValueError, match=r"\.\."):
            _validate_satellite_host_path("/home/erin/../etc/passwd")

    def test_rejects_traversal_only_segments(self):
        # `..` in the middle is detected
        with pytest.raises(ValueError, match=r"\.\."):
            _validate_satellite_host_path("/a/b/../c")

    def test_accepts_double_dot_inside_filename(self):
        # Files containing `..` in their name (e.g. `foo..bar.txt`)
        # are NOT path-traversal — only `..` as a full segment is.
        _validate_satellite_host_path("/home/erin/foo..bar.txt")


# ---------------------------------------------------------------------------
# Host-cache path helpers
# ---------------------------------------------------------------------------


class TestHostCachePaths:
    def test_is_host_cache_path_positive(self):
        # The cache lives under {AGENTS_DIR}/.remote-host-cache/
        root = str(rff._host_cache_root())
        assert rff.is_host_cache_path(f"{root}/sess1/abc/file.png")
        assert rff.is_host_cache_path(root)

    def test_is_host_cache_path_negative(self):
        # Plain agent-tree paths are NOT cache paths
        import config as _cfg
        assert not rff.is_host_cache_path(str(_cfg.AGENTS_DIR / "agent" / "workspace" / "f.png"))
        assert not rff.is_host_cache_path("/etc/hosts")
        assert not rff.is_host_cache_path("")

    def test_cache_paths_namespace_by_machine_and_abspath(self):
        cp1, _ = rff._host_cache_paths("sess", "m1", "/etc/hosts")
        cp2, _ = rff._host_cache_paths("sess", "m2", "/etc/hosts")
        cp3, _ = rff._host_cache_paths("sess", "m1", "/etc/passwd")
        # Different machine_id or abs_path → different cache dir.
        assert cp1.parent != cp2.parent
        assert cp1.parent != cp3.parent

    def test_cache_paths_basename_extracted(self):
        cp, _ = rff._host_cache_paths("sess", "m1", "/home/alice/Desktop/foo.png")
        assert cp.name == "foo.png"

    def test_cache_paths_sidecar_filename(self):
        cp, sidecar = rff._host_cache_paths("sess", "m1", "/x.png")
        assert sidecar.name == "_meta.json"
        assert cp.parent == sidecar.parent


# ---------------------------------------------------------------------------
# Per-(machine_id, abs_path) lock
# ---------------------------------------------------------------------------


class TestMachineHostLock:
    @pytest.mark.asyncio
    async def test_same_key_returns_same_lock(self):
        lock_a = await rff._acquire_machine_host_lock("m1", "/etc/hosts")
        lock_b = await rff._acquire_machine_host_lock("m1", "/etc/hosts")
        assert lock_a is lock_b

    @pytest.mark.asyncio
    async def test_different_machines_different_lock(self):
        lock_a = await rff._acquire_machine_host_lock("m1", "/etc/hosts")
        lock_b = await rff._acquire_machine_host_lock("m2", "/etc/hosts")
        assert lock_a is not lock_b

    @pytest.mark.asyncio
    async def test_normalizes_trailing_slash(self):
        lock_a = await rff._acquire_machine_host_lock("m1", "/etc/foo/")
        lock_b = await rff._acquire_machine_host_lock("m1", "/etc/foo")
        assert lock_a is lock_b


# ---------------------------------------------------------------------------
# pull_through_host_path + push_back_host_path
# ---------------------------------------------------------------------------


class _FakeInfo:
    def __init__(self, machine_id="m1", agent_name="my-agent"):
        self.machine_id = machine_id
        self.agent_name = agent_name


class TestPullThroughHostPath:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_remote(self, monkeypatch):
        monkeypatch.setattr(rff, "_get_remote_session_info", lambda s: None)
        result = await rff.pull_through_host_path("sess-x", "/etc/hosts")
        assert result is None

    @pytest.mark.asyncio
    async def test_writes_sidecar_with_metadata(self, monkeypatch, tmp_path):
        # Redirect the cache root into tmp_path for isolation.
        monkeypatch.setattr(rff, "_host_cache_root", lambda: tmp_path)
        monkeypatch.setattr(rff, "_get_remote_session_info", lambda s: _FakeInfo())

        fake_cm = AsyncMock()

        async def fake_pull_to_path(machine_id, ref, dest_path, *, agent_slug=""):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"file-bytes")
            return True

        fake_cm.pull_file_to_path.side_effect = fake_pull_to_path
        with patch(
            "core.remote.satellite_connection.get_connection_manager",
            return_value=fake_cm,
        ):
            result = await rff.pull_through_host_path(
                "sess-x", "/etc/hosts",
            )
        assert result is not None
        assert result.read_bytes() == b"file-bytes"

        sidecar = result.parent / "_meta.json"
        meta = json.loads(sidecar.read_text())
        assert meta["machine_id"] == "m1"
        assert meta["abs_path"] == "/etc/hosts"

    @pytest.mark.asyncio
    async def test_returns_none_on_pull_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rff, "_host_cache_root", lambda: tmp_path)
        monkeypatch.setattr(rff, "_get_remote_session_info", lambda s: _FakeInfo())

        fake_cm = AsyncMock()
        fake_cm.pull_file_to_path.return_value = False
        with patch(
            "core.remote.satellite_connection.get_connection_manager",
            return_value=fake_cm,
        ):
            result = await rff.pull_through_host_path("sess-x", "/etc/hosts")
        assert result is None


class TestPushBackHostPath:
    @pytest.mark.asyncio
    async def test_pushes_using_sidecar_metadata(self, monkeypatch, tmp_path):
        # Build a cache file + sidecar as if pull_through wrote them.
        cache_path = tmp_path / "sess1" / "abc" / "foo.png"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_bytes(b"new-bytes")
        sidecar = cache_path.parent / "_meta.json"
        sidecar.write_text(json.dumps({
            "machine_id": "m1", "abs_path": "/home/alice/Desktop/foo.png",
        }))

        fake_cm = AsyncMock()
        fake_cm.push_file.return_value = True
        with patch(
            "core.remote.satellite_connection.get_connection_manager",
            return_value=fake_cm,
        ):
            ok = await rff.push_back_host_path("sess1", str(cache_path))
        assert ok is True
        # cm.push_file received PathRef("satellite_host", "/home/alice/...")
        call = fake_cm.push_file.call_args
        assert call.args[0] == "m1"
        ref = call.args[1]
        assert ref.kind == "satellite_host"
        assert ref.value == "/home/alice/Desktop/foo.png"
        # Streaming refactor (1.4.0): push_file receives the cache PATH.
        from pathlib import Path as _P
        assert _P(call.args[2]).read_bytes() == b"new-bytes"

    @pytest.mark.asyncio
    async def test_missing_sidecar_returns_false(self, tmp_path):
        bogus = tmp_path / "nope" / "f.png"
        ok = await rff.push_back_host_path("sess1", str(bogus))
        assert ok is False

    @pytest.mark.asyncio
    async def test_missing_machine_id_in_sidecar(self, tmp_path):
        cp = tmp_path / "cache" / "f.png"
        cp.parent.mkdir(parents=True)
        cp.write_bytes(b"x")
        (cp.parent / "_meta.json").write_text(json.dumps({"abs_path": "/x"}))
        ok = await rff.push_back_host_path("sess1", str(cp))
        assert ok is False


# ---------------------------------------------------------------------------
# /v1/hooks/resolve-path dispatch
# ---------------------------------------------------------------------------


def _make_remote_ctx(*, allow_full_fs=False) -> SecurityContext:
    return SecurityContext(
        role="manager", username="alice", agent="my-agent",
        is_admin_agent=False,
        target_kind="user_remote",
        target_machine_id="m1",
        target_agents_dir="/home/dave/.oto-dock/agents",
        target_home_dir="/home/dave",
        target_allow_full_fs=allow_full_fs,
    )


class TestResolvePathHookSatelliteHost:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.hooks import hooks as hooks_mod

        app = FastAPI()
        app.include_router(hooks_mod.router)
        monkeypatch.setattr(
            hooks_mod, "verify_session_match", lambda *a, **kw: None,
        )
        return TestClient(app)

    def test_satellite_host_path_rejected_home_only(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=False),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1", "path": "/etc/hosts",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 403
        assert "home" in r.json()["detail"].lower()

    def test_satellite_host_path_allowed_with_full_fs(
        self, client, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=True),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        # Stub the host-cache pull to write a synthetic file.
        cache_file = tmp_path / "cache" / "f"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"x")

        async def fake_pull(session_id, abs_path):
            return cache_file

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through_host_path", fake_pull,
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1", "path": "/etc/hosts",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        body = r.json()
        assert body["host_path"] == str(cache_file)
        # agents_relative may be empty if the cache path isn't under
        # AGENTS_DIR (this test uses tmp_path); the production cache IS
        # under AGENTS_DIR so file-tools' MOUNT_AGENTS_DIR + agents_rel
        # works without code changes.
        assert "agents_relative" in body

    def test_in_tree_absolute_folds_back_and_pulls_by_slug(
        self, client, monkeypatch, tmp_path,
    ):
        """An in-tree satellite-host-ABSOLUTE path must fold back to its
        agent-tree slug and pull_through with THAT slug — not the whole raw
        absolute path. Regression for the "file inside the agent folder,
        addressed by its real /…absolute, not found on the first try" bug:
        previously the hook pulled with raw.lstrip('/') (the entire
        ``home/dave/.oto-dock/agents/my-agent/users/…`` string) and missed."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=False),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        cache_file = tmp_path / "cache" / "report.docx"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"x")
        captured: dict = {}

        async def fake_pull_through(session_id, rel):
            captured["rel"] = rel
            return cache_file

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through", fake_pull_through,
        )
        # The real absolute path of an in-workspace file on the satellite
        # (target_agents_dir/my-agent/users/alice/workspace/report.docx).
        abs_in_tree = (
            "/home/dave/.oto-dock/agents/my-agent/"
            "users/alice/workspace/report.docx"
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1", "path": abs_in_tree,
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        # Pulled by the agent-tree SLUG, not the whole absolute path.
        assert captured["rel"] == "users/alice/workspace/report.docx"

    def test_in_tree_absolute_folds_back_shared_only_layout(
        self, client, monkeypatch, tmp_path,
    ):
        """Shared-only agents have NO users/<u> segment — the in-tree fold
        must handle ``{sat_agents_dir}/{slug}/workspace/...`` the same way
        (pull by ``workspace/...``)."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=False),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        cache_file = tmp_path / "cache" / "report.docx"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"x")
        captured: dict = {}

        async def fake_pull_through(session_id, rel):
            captured["rel"] = rel
            return cache_file

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through", fake_pull_through,
        )
        abs_in_tree = (
            "/home/dave/.oto-dock/agents/my-agent/workspace/report.docx"
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1", "path": abs_in_tree,
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert captured["rel"] == "workspace/report.docx"

    def test_sandbox_virtual_pulls_by_slug_unchanged(
        self, client, monkeypatch, tmp_path,
    ):
        """The sandbox-virtual form pulls by the same slug — the fix is
        behavior-preserving for the path form that already worked."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=False),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        cache_file = tmp_path / "cache" / "report.docx"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"x")
        captured: dict = {}

        async def fake_pull_through(session_id, rel):
            captured["rel"] = rel
            return cache_file

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through", fake_pull_through,
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1",
            "path": "/users/alice/workspace/report.docx",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert captured["rel"] == "users/alice/workspace/report.docx"

    def test_write_target_missing_in_tree_resolves_to_creation_path(
        self, client, monkeypatch, tmp_path,
    ):
        """A WRITE target that doesn't exist on the satellite yet (fresh
        output_path) must resolve to the platform creation path — not fall
        through to the lexical translator. Regression for edit_image failing
        its first call with a new output_path on a remote session."""
        import config

        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=False),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )

        async def fake_pull_through(session_id, rel):
            return None  # not on the satellite — it's a new file

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through", fake_pull_through,
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1",
            "path": "/users/alice/workspace/photos/img_pro.png",
            "writing": True,
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        body = r.json()
        expected = str(
            (tmp_path / "my-agent" / "users" / "alice" / "workspace"
             / "photos" / "img_pro.png").resolve()
        )
        assert body["host_path"] == expected
        # _to_agents_relative keeps the leading "/" (file-tools lstrips it).
        assert body["agents_relative"] == (
            "/my-agent/users/alice/workspace/photos/img_pro.png"
        )

    def test_write_target_existing_file_still_pulls_current_bytes(
        self, client, monkeypatch, tmp_path,
    ):
        """writing=True must NOT skip the pull: a read-modify-write tool
        (write_docx on an existing doc) needs the satellite's current bytes
        materialized first."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=False),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        cache_file = tmp_path / "cache" / "report.docx"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"current")

        async def fake_pull_through(session_id, rel):
            return cache_file

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through", fake_pull_through,
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1",
            "path": "/users/alice/workspace/report.docx",
            "writing": True,
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json()["host_path"] == str(cache_file)

    def test_write_target_out_of_tree_gets_clear_policy_error(
        self, client, monkeypatch, tmp_path,
    ):
        """Creating a NEW file at an arbitrary satellite-host path is
        unsupported for Docker MCPs — the caller gets a clear 403, not a
        baffling 404."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(allow_full_fs=True),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )

        async def fake_pull_host(session_id, abs_path):
            return None

        monkeypatch.setattr(
            "core.remote.remote_file_flow.pull_through_host_path",
            fake_pull_host,
        )
        r = client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1",
            "path": "/home/dave/newfile.bin",
            "writing": True,
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 403
        assert "outside the synced agent tree" in r.json()["detail"]


class TestFileWrittenHookDispatch:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.hooks import hooks as hooks_mod

        app = FastAPI()
        app.include_router(hooks_mod.router)
        monkeypatch.setattr(
            hooks_mod, "verify_session_match", lambda *a, **kw: None,
        )
        return TestClient(app)

    def test_host_cache_path_routes_to_push_back_host(self, client, monkeypatch):
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_host_cache_path", lambda p: True,
        )
        called = {"count": 0}

        async def fake_push_host(session_id, cache_path):
            called["count"] += 1
            return True

        monkeypatch.setattr(
            "core.remote.remote_file_flow.push_back_host_path", fake_push_host,
        )
        r = client.post("/v1/hooks/file-written", json={
            "session_id": "s1",
            "path": "/proxy/agents/.remote-host-cache/s1/abc/f.png",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "kind": "satellite_host"}
        assert called["count"] == 1

    def test_agent_tree_path_routes_to_push_back(self, client, monkeypatch):
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_host_cache_path", lambda p: False,
        )
        called = {"count": 0}

        async def fake_push(session_id, rel_path):
            called["count"] += 1
            return True

        monkeypatch.setattr(
            "core.remote.remote_file_flow.push_back", fake_push,
        )
        r = client.post("/v1/hooks/file-written", json={
            "session_id": "s1",
            "path": "users/alice/workspace/foo.png",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert called["count"] == 1

    def test_local_session_short_circuits(self, client, monkeypatch):
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: False,
        )
        r = client.post("/v1/hooks/file-written", json={
            "session_id": "s1", "path": "/workspace/foo.png",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "local": True}

    def test_slug_prefixed_agents_relative_folds_to_canonical(
        self, client, monkeypatch,
    ):
        """file-tools posts the slug-PREFIXED agents-relative form (the same
        form the resolve-path hook hands back for reads); the hook must fold
        the session's own slug off so push_back's canonical gate passes.
        Regression for the silent write-back loss: the edit stayed
        platform-side and the next analyze re-pulled the satellite original
        over it."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_host_cache_path", lambda p: False,
        )
        captured: dict = {}

        async def fake_push(session_id, rel_path):
            captured["rel"] = rel_path
            return True

        monkeypatch.setattr(
            "core.remote.remote_file_flow.push_back", fake_push,
        )
        r = client.post("/v1/hooks/file-written", json={
            "session_id": "s1",
            "path": "my-agent/users/alice/workspace/foo.png",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert captured["rel"] == "users/alice/workspace/foo.png"

    def test_foreign_slug_is_never_stripped(self, client, monkeypatch):
        """Only the session's OWN agent slug folds off — a foreign slug is
        passed through untouched (and fails push_back's canonical gate)."""
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_host_cache_path", lambda p: False,
        )
        captured: dict = {}

        async def fake_push(session_id, rel_path):
            captured["rel"] = rel_path
            return False

        monkeypatch.setattr(
            "core.remote.remote_file_flow.push_back", fake_push,
        )
        r = client.post("/v1/hooks/file-written", json={
            "session_id": "s1",
            "path": "other-agent/users/alice/workspace/foo.png",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"ok": False}
        assert captured["rel"] == "other-agent/users/alice/workspace/foo.png"

    def test_slug_prefixed_write_back_reaches_satellite(
        self, client, monkeypatch, tmp_path,
    ):
        """End-to-end through the REAL push_back (no mock): the slug-prefixed
        post passes the canonical gate after the fold and the satellite push
        goes out with the canonical rel. This is the path the old mocked test
        never exercised."""
        import config
        from unittest.mock import MagicMock

        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_remote_ctx(),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: True,
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_host_cache_path", lambda p: False,
        )

        class _Info:
            machine_id = "m1"
            agent_name = "my-agent"

        monkeypatch.setattr(
            "core.remote.remote_file_flow._get_remote_session_info",
            lambda sid: _Info(),
        )
        edited = tmp_path / "my-agent" / "users" / "alice" / "workspace" / "foo.png"
        edited.parent.mkdir(parents=True)
        edited.write_bytes(b"edited-bytes")

        pushed: dict = {}

        async def fake_push_file(machine_id, ref, content, *, agent_slug=""):
            pushed["machine_id"] = machine_id
            pushed["rel"] = ref.value
            pushed["content"] = content
            return True

        mock_cm = MagicMock()
        mock_cm.push_file.side_effect = fake_push_file
        monkeypatch.setattr(
            "core.remote.satellite_connection.get_connection_manager",
            lambda: mock_cm,
        )

        async def fake_fan_out(*a, **kw):
            return None

        monkeypatch.setattr(
            "services.remote.workspace_fanout.fan_out_write", fake_fan_out,
        )
        r = client.post("/v1/hooks/file-written", json={
            "session_id": "s1",
            "path": "my-agent/users/alice/workspace/foo.png",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert pushed["machine_id"] == "m1"
        assert pushed["rel"] == "users/alice/workspace/foo.png"
        # Streaming refactor (1.4.0): fan-out receives the platform PATH.
        from pathlib import Path as _P
        assert _P(pushed["content"]).read_bytes() == b"edited-bytes"


# ---------------------------------------------------------------------------
# Write-RBAC through the endpoint (LOCAL sandbox-virtual paths)
#
# The role rules themselves are unit-covered in test_path_policy.py; this
# pins that a Docker MCP's resolve-path call with writing=True actually
# hits them (5f914e2) — the container writes with its own I/O outside the
# bwrap hook gate, so this 403 is the ONLY thing between an editor and an
# owner-only /knowledge write.
# ---------------------------------------------------------------------------


def _make_local_ctx(role: str) -> SecurityContext:
    return SecurityContext(
        role=role, username="bob", agent="my-agent", is_admin_agent=False,
    )


class TestResolvePathWriteRbac:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.hooks import hooks as hooks_mod

        app = FastAPI()
        app.include_router(hooks_mod.router)
        monkeypatch.setattr(
            hooks_mod, "verify_session_match", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: False,
        )
        return TestClient(app)

    def _post(self, client, monkeypatch, role, *, writing):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_local_ctx(role),
        )
        return client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1", "path": "/knowledge/notes.md",
            "writing": writing,
        }, headers={"Authorization": "Bearer test"})

    def test_editor_knowledge_write_403(self, client, monkeypatch):
        r = self._post(client, monkeypatch, "editor", writing=True)
        assert r.status_code == 403
        assert "knowledge" in r.json()["detail"].lower()

    def test_viewer_knowledge_write_403(self, client, monkeypatch):
        r = self._post(client, monkeypatch, "viewer", writing=True)
        assert r.status_code == 403

    def test_manager_knowledge_write_not_role_blocked(self, client, monkeypatch):
        # Managers own /knowledge — whatever else the resolver does with a
        # nonexistent file, it must NOT be a role denial.
        r = self._post(client, monkeypatch, "manager", writing=True)
        assert r.status_code != 403

    def test_editor_knowledge_read_not_role_blocked(self, client, monkeypatch):
        # Same path as a READ stays open to editors (owner-only is write-only).
        r = self._post(client, monkeypatch, "editor", writing=False)
        assert r.status_code != 403


# ---------------------------------------------------------------------------
# Preview/display classify gate — host-cache carve-out (2026-07-19 live hit:
# the agent-tree confinement 400'd EVERY satellite-host preview because the
# lazy-pull cache lives beside, not inside, the agent trees)
# ---------------------------------------------------------------------------


class TestClassifyGateHostCache:
    def _ctx(self):
        return SecurityContext(role="manager", username="alice", agent="pa",
                               is_admin_agent=False)

    def _setup(self, tmp_path, monkeypatch, sid="sess-hc1"):
        import config
        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
        from core.session import session_state
        monkeypatch.setitem(session_state._session_security, sid, self._ctx())
        monkeypatch.setattr(rff, "is_remote_session", lambda s: True)
        return sid

    def test_own_session_cache_path_serves(self, tmp_path, monkeypatch):
        import asyncio
        from api.hooks import hooks as hooks_mod
        sid = self._setup(tmp_path, monkeypatch)
        cache = rff.host_cache_session_root(sid) / "abcd1234"
        cache.mkdir(parents=True)
        f = cache / "doc.xlsx"
        f.write_bytes(b"x")
        rel = str(f.relative_to(tmp_path))
        host, _res = asyncio.run(hooks_mod._classify_and_pull(sid, rel))
        assert host is not None and host.resolve() == f.resolve()

    def test_other_sessions_cache_denied(self, tmp_path, monkeypatch):
        import asyncio
        from api.hooks import hooks as hooks_mod
        sid = self._setup(tmp_path, monkeypatch)
        other = rff.host_cache_session_root("sess-OTHER") / "abcd1234"
        other.mkdir(parents=True)
        f = other / "doc.xlsx"
        f.write_bytes(b"x")
        rel = str(f.relative_to(tmp_path))
        host, _res = asyncio.run(hooks_mod._classify_and_pull(sid, rel))
        assert host is None

    def test_cache_rel_never_mints_write_capability(self):
        # The preview token's edit gate runs can_write_back on the agent-tree
        # rel; a host-cache rel is not a workspace path → always view-only.
        from core.remote.file_sync import can_write_back
        assert not can_write_back(
            "sess-hc1/abcd1234/doc.xlsx", "manager", "alice",
        )


class TestPullThroughMirrorFallback:
    def test_failed_satellite_pull_serves_platform_mirror(self, tmp_path, monkeypatch):
        import asyncio
        from types import SimpleNamespace
        import config
        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

        mirror = tmp_path / "pa" / "users" / "alice" / "workspace" / "out.pdf"
        mirror.parent.mkdir(parents=True)
        mirror.write_bytes(b"fresh platform bytes")

        info = SimpleNamespace(machine_id="mach-1", agent_name="pa")
        monkeypatch.setattr(rff, "_get_remote_session_info", lambda sid: info)

        class _St:
            pending_push: dict = {}

        async def _state(sid):
            return _St()

        monkeypatch.setattr(rff, "_state", _state)

        async def _probe(cm, mid, ref, agent_slug=""):
            return None

        monkeypatch.setattr(rff, "_probe_stat", _probe)

        class _CM:
            async def pull_file_to_path(self, mid, ref, dest, agent_slug=""):
                return False  # satellite can't serve it (flush not landed)

        from core.remote import satellite_connection
        monkeypatch.setattr(satellite_connection, "get_connection_manager", _CM)

        got = asyncio.run(rff.pull_through(
            "sess-pt1", "users/alice/workspace/out.pdf",
        ))
        assert got is not None and got.resolve() == mirror.resolve()

    def test_failed_pull_no_mirror_still_none(self, tmp_path, monkeypatch):
        import asyncio
        from types import SimpleNamespace
        import config
        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
        (tmp_path / "pa").mkdir()
        info = SimpleNamespace(machine_id="mach-1", agent_name="pa")
        monkeypatch.setattr(rff, "_get_remote_session_info", lambda sid: info)

        class _St:
            pending_push: dict = {}

        async def _state(sid):
            return _St()

        monkeypatch.setattr(rff, "_state", _state)

        async def _probe(cm, mid, ref, agent_slug=""):
            return None

        monkeypatch.setattr(rff, "_probe_stat", _probe)

        class _CM:
            async def pull_file_to_path(self, mid, ref, dest, agent_slug=""):
                return False

        from core.remote import satellite_connection
        monkeypatch.setattr(satellite_connection, "get_connection_manager", _CM)

        got = asyncio.run(rff.pull_through(
            "sess-pt2", "users/alice/workspace/never-existed.pdf",
        ))
        assert got is None


class TestHostCachePreviewEditMint:
    """Entry-10 write-back (2026-07-19): a host-cache doc (Desktop/Downloads
    pulled through THIS session's cache) mints an EDIT token for write-capable
    roles — PutFile then pushes back to the real file. Session-scoped: another
    session's cache stays view-only; viewer role stays view-only."""

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.hooks import hooks as hooks_mod

        app = FastAPI()
        app.include_router(hooks_mod.router)
        monkeypatch.setattr(
            hooks_mod, "verify_session_match", lambda *a, **kw: None,
        )
        return TestClient(app)

    def _mint(self, client, monkeypatch, tmp_path, *, role,
              cache_session="sess-1", req_session="sess-1"):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import config
        from api.hooks import hooks as hooks_mod

        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
        monkeypatch.setattr(config, "COLLABORA_URL", "https://c.example", raising=False)
        monkeypatch.setattr(config, "WOPI_BASE_URL", "https://w.example", raising=False)
        monkeypatch.setattr(config, "WOPI_SECRET", "test-secret", raising=False)

        cache = tmp_path / ".remote-host-cache" / cache_session / "digest" / "x.docx"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"doc")

        async def _cap(session_id, path):
            return (cache, None)

        monkeypatch.setattr(hooks_mod, "_classify_and_pull", _cap)
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: SimpleNamespace(
                role=role, username="u", mount_username="", agent="test-agent",
            ),
        )
        monkeypatch.setattr(
            hooks_mod, "resolve_hook_chat_id", AsyncMock(return_value=None),
        )
        hooks_mod._wopi_url_cache.clear()
        minted = {}

        def _fake_mint(rel, sub, name, permissions, agent):
            minted["permissions"] = permissions
            return "tok", 3600

        monkeypatch.setattr("api.media.wopi.create_wopi_token", _fake_mint)
        r = client.post("/v1/hooks/document-preview", json={
            "session_id": req_session, "file_path": "C:/Users/u/Desktop/x.docx",
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text
        return minted["permissions"]

    def test_editor_gets_edit_on_own_session_cache(
        self, client, monkeypatch, tmp_path, temp_db,
    ):
        assert self._mint(client, monkeypatch, tmp_path, role="editor") == "edit"

    def test_viewer_stays_view(self, client, monkeypatch, tmp_path, temp_db):
        assert self._mint(client, monkeypatch, tmp_path, role="viewer") == "view"

    def test_other_sessions_cache_stays_view(
        self, client, monkeypatch, tmp_path, temp_db,
    ):
        # The file lives in ANOTHER session's cache → the session-scoped
        # carve-out doesn't apply, and the agent-tree matrix rejects cache
        # rels → view-only even for a manager.
        assert self._mint(
            client, monkeypatch, tmp_path, role="manager",
            cache_session="sess-OTHER", req_session="sess-1",
        ) == "view"


# ---------------------------------------------------------------------------
# Model-echoed display-path carve-out (/v1/hooks/resolve-path, reads only)
# ---------------------------------------------------------------------------


def _make_display_ctx(**over) -> SecurityContext:
    kw = dict(role="manager", username="alice", agent="my-agent",
              is_admin_agent=False)
    kw.update(over)
    return SecurityContext(**kw)


class TestResolvePathDisplayPathCarveOut:
    """Tool results print agents-relative DISPLAY paths
    ("<slug>/users/<u>/workspace/x.png") and models echo them back as
    inputs. Reads of an existing file in the session's OWN tree resolve;
    every widening variant falls through or 403s, and writes never take
    the carve-out (a write would create an agents/<slug>/<slug>/ tree)."""

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.hooks import hooks as hooks_mod

        app = FastAPI()
        app.include_router(hooks_mod.router)
        monkeypatch.setattr(
            hooks_mod, "verify_session_match", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: _make_display_ctx(),
        )
        monkeypatch.setattr(
            "core.remote.remote_file_flow.is_remote_session", lambda sid: False,
        )
        return TestClient(app)

    @pytest.fixture
    def tree(self):
        import config as _cfg
        own = _cfg.AGENTS_DIR / "my-agent" / "users" / "alice" / "workspace" / "shots"
        own.mkdir(parents=True, exist_ok=True)
        (own / "page_001.png").write_bytes(b"png")
        other_user = _cfg.AGENTS_DIR / "my-agent" / "users" / "bob" / "workspace"
        other_user.mkdir(parents=True, exist_ok=True)
        (other_user / "secret.png").write_bytes(b"x")
        other_agent = _cfg.AGENTS_DIR / "other-agent" / "users" / "alice" / "workspace"
        other_agent.mkdir(parents=True, exist_ok=True)
        (other_agent / "file.png").write_bytes(b"x")
        return _cfg.AGENTS_DIR

    def _resolve(self, client, path, writing=False):
        return client.post("/v1/hooks/resolve-path", json={
            "session_id": "s1", "path": path, "writing": writing,
        }, headers={"Authorization": "Bearer test"})

    def test_own_display_path_resolves_for_read(self, client, tree):
        r = self._resolve(
            client, "my-agent/users/alice/workspace/shots/page_001.png")
        assert r.status_code == 200, r.text
        expected = tree / "my-agent" / "users" / "alice" / "workspace" / "shots" / "page_001.png"
        assert r.json()["host_path"] == str(expected)
        assert r.json()["agents_relative"].lstrip("/") == \
            "my-agent/users/alice/workspace/shots/page_001.png"

    def test_other_agent_display_path_gains_no_cross_agent_reach(self, client, tree):
        r = self._resolve(client, "other-agent/users/alice/workspace/file.png")
        if r.status_code == 200:
            assert str(tree / "other-agent") not in r.json()["host_path"]

    def test_dotdot_to_other_agent_gains_no_cross_agent_reach(self, client, tree):
        r = self._resolve(
            client, "my-agent/../other-agent/users/alice/workspace/file.png")
        if r.status_code == 200:
            assert str(tree / "other-agent") not in r.json()["host_path"]

    def test_dotdot_escape_denied(self, client, tree):
        r = self._resolve(client, "my-agent/../../etc/passwd")
        assert r.status_code == 403

    def test_other_users_dir_denied(self, client, tree):
        r = self._resolve(client, "my-agent/users/bob/workspace/secret.png")
        assert r.status_code == 403

    def test_write_target_skips_the_carve_out(self, client, tree):
        direct = tree / "my-agent" / "users" / "alice" / "workspace" / "shots" / "page_001.png"
        r = self._resolve(
            client, "my-agent/users/alice/workspace/shots/page_001.png",
            writing=True)
        # Fall-through behavior for writes (today's): whatever happens, the
        # carve-out must not hand back the direct file as a WRITE target.
        assert r.status_code != 200 or r.json()["host_path"] != str(direct)
