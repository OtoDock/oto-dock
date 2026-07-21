"""Tests for the startup pass that ensures bundled MCP venvs are current.

Covers the freshness check semantics (missing/stale → rebuild, fresh → skip),
runtime dispatch (docker/python/node), graceful handling of bad manifests,
and the absence-of-MCPS_DIR case. ``install_mcp`` itself is mocked — its
behavior is covered separately in ``test_mcp_installer.py``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import config
from services.mcp import mcp_venv_bootstrap
from services.mcp.mcp_installer import InstallResult


def _make_mcp(root: Path, category: str, name: str, manifest: dict) -> Path:
    """Helper: drop a fake MCP folder under ``root/<category>/<name>/``."""
    mcp_dir = root / category / name
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "manifest.json").write_text(json.dumps(manifest))
    return mcp_dir


@pytest.fixture
def fake_mcps_root(tmp_path, monkeypatch):
    """Point ``config.MCPS_DIR`` at a temp dir for the duration of the test."""
    root = tmp_path / "mcps"
    root.mkdir()
    (root / "custom").mkdir()
    (root / "community").mkdir()
    monkeypatch.setattr(config, "MCPS_DIR", root)
    return root


# ─────────────────────── _venv_is_stale ────────────────────────────────


def test_stale_when_venv_missing(tmp_path):
    deps = tmp_path / "requirements.txt"
    deps.write_text("foo==1.0\n")
    venv = tmp_path / "venv"  # not created
    assert mcp_venv_bootstrap._venv_is_stale(deps, venv) is True


def test_fresh_when_venv_newer(tmp_path):
    deps = tmp_path / "requirements.txt"
    deps.write_text("foo==1.0\n")
    venv = tmp_path / "venv"
    venv.mkdir()
    # Bump venv mtime past deps' mtime
    future = time.time() + 60
    import os
    os.utime(venv, (future, future))
    assert mcp_venv_bootstrap._venv_is_stale(deps, venv) is False


def test_stale_when_deps_newer(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    deps = tmp_path / "requirements.txt"
    deps.write_text("foo==1.0\n")
    # Bump deps mtime past venv's mtime
    future = time.time() + 60
    import os
    os.utime(deps, (future, future))
    assert mcp_venv_bootstrap._venv_is_stale(deps, venv) is True


# ─────────────────── ensure_bundled_venvs_at_startup ──────────────────


@pytest.mark.asyncio
async def test_no_mcps_root_returns_empty(monkeypatch, tmp_path):
    """If MCPS_DIR doesn't exist, function returns empty dict + logs."""
    monkeypatch.setattr(config, "MCPS_DIR", tmp_path / "does-not-exist")
    results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {}


@pytest.mark.asyncio
async def test_skips_docker_runtime(fake_mcps_root):
    _make_mcp(fake_mcps_root, "custom", "docker-mcp", {
        "name": "docker-mcp",
        "server": {"runtime": "docker"},
    })
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"docker-mcp": "skipped-docker"}
        m.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_python_mcp_without_requirements(fake_mcps_root):
    _make_mcp(fake_mcps_root, "custom", "no-deps-mcp", {
        "name": "no-deps-mcp",
        "server": {"runtime": "python"},
    })
    # No requirements.txt
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"no-deps-mcp": "skipped-no-reqs"}
        m.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_bundled_node_runtime(fake_mcps_root):
    _make_mcp(fake_mcps_root, "custom", "node-mcp", {
        "name": "node-mcp",
        "server": {"runtime": "node"},
    })
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"node-mcp": "skipped-bundled-node"}
        m.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_runtime_labeled(fake_mcps_root):
    _make_mcp(fake_mcps_root, "custom", "weird-mcp", {
        "name": "weird-mcp",
        "server": {"runtime": "rust"},
    })
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"weird-mcp": "skipped-runtime-rust"}


@pytest.mark.asyncio
async def test_python_missing_venv_triggers_install(fake_mcps_root):
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp",
        "server": {"runtime": "python"},
    })
    (mcp_dir / "requirements.txt").write_text("requests==2.31\n")
    # No venv directory exists

    with patch(
        "services.mcp.mcp_installer.install_mcp",
        new_callable=AsyncMock,
        return_value=InstallResult(ok=True, log="installed", version_hash="abc"),
    ) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"py-mcp": "ok"}
        m.assert_awaited_once()
        args, kwargs = m.await_args
        assert args[0] == mcp_dir
        assert args[1] == "python"
        assert args[2] == ""  # empty source = bundled-python branch


@pytest.mark.asyncio
async def test_python_fresh_venv_is_skipped(fake_mcps_root):
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp",
        "server": {"runtime": "python"},
    })
    deps = mcp_dir / "requirements.txt"
    deps.write_text("requests==2.31\n")
    venv = mcp_dir / "venv"
    venv.mkdir()
    # Bump venv mtime past deps' mtime + success-mark it (an unmarked EMPTY
    # venv is the poisoned interrupted-install shape and rebuilds by design).
    import os
    future = time.time() + 60
    os.utime(venv, (future, future))
    mcp_venv_bootstrap._write_marker(
        mcp_dir,
        requirements_sha256=mcp_venv_bootstrap._requirements_hash(deps),
    )

    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"py-mcp": "fresh"}
        m.assert_not_awaited()


@pytest.mark.asyncio
async def test_python_stale_venv_triggers_install(fake_mcps_root):
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp",
        "server": {"runtime": "python"},
    })
    deps = mcp_dir / "requirements.txt"
    venv = mcp_dir / "venv"
    venv.mkdir()
    # Now write deps with a newer mtime
    deps.write_text("requests==2.31\nclick==8.1\n")
    import os
    future = time.time() + 60
    os.utime(deps, (future, future))

    with patch(
        "services.mcp.mcp_installer.install_mcp",
        new_callable=AsyncMock,
        return_value=InstallResult(ok=True, log="updated", version_hash="def"),
    ) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"py-mcp": "ok"}
        m.assert_awaited_once()


@pytest.mark.asyncio
async def test_install_failure_reported(fake_mcps_root):
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp",
        "server": {"runtime": "python"},
    })
    (mcp_dir / "requirements.txt").write_text("nonexistent-pkg==1.0\n")

    with patch(
        "services.mcp.mcp_installer.install_mcp",
        new_callable=AsyncMock,
        return_value=InstallResult(ok=False, log="pip error"),
    ):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"py-mcp": "failed"}


@pytest.mark.asyncio
async def test_install_exception_caught(fake_mcps_root):
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp",
        "server": {"runtime": "python"},
    })
    (mcp_dir / "requirements.txt").write_text("foo==1.0\n")

    with patch(
        "services.mcp.mcp_installer.install_mcp",
        new_callable=AsyncMock,
        side_effect=RuntimeError("disk full"),
    ):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {"py-mcp": "exception"}


@pytest.mark.asyncio
async def test_bad_manifest_skipped(fake_mcps_root, caplog):
    mcp_dir = fake_mcps_root / "custom" / "broken-mcp"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text("{ not valid json")

    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as m:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {}  # bad manifest never enters the dict
        m.assert_not_awaited()
    # Warning was logged
    assert any("bad manifest" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_walks_both_categories(fake_mcps_root):
    """Both custom/ AND community/ subtrees are scanned."""
    _make_mcp(fake_mcps_root, "custom", "from-custom", {
        "name": "from-custom",
        "server": {"runtime": "docker"},
    })
    _make_mcp(fake_mcps_root, "community", "from-community", {
        "name": "from-community",
        "server": {"runtime": "docker"},
    })
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert results == {
            "from-custom": "skipped-docker",
            "from-community": "skipped-docker",
        }


@pytest.mark.asyncio
async def test_manifest_name_overrides_dir_name(fake_mcps_root):
    """Canonical name from manifest is used, even when dir name differs."""
    mcp_dir = fake_mcps_root / "community" / "workspace-mcp"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text(json.dumps({
        "name": "google-workspace",  # canonical slug, not folder name
        "server": {"runtime": "docker"},
    }))
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        assert "google-workspace" in results
        assert "workspace-mcp" not in results


# ──────────────────── python-interpreter reconciliation ────────────────


def _write_pyvenv(venv_dir: Path, major: int, minor: int, micro: int = 0) -> None:
    venv_dir.mkdir(parents=True, exist_ok=True)
    (venv_dir / "pyvenv.cfg").write_text(
        f"home = /usr/bin\nversion = {major}.{minor}.{micro}\n"
    )


def test_venv_python_minor_parses(tmp_path):
    venv = tmp_path / "venv"
    _write_pyvenv(venv, 3, 10, 12)
    assert mcp_venv_bootstrap._venv_python_minor(venv) == (3, 10)


def test_venv_python_minor_missing_or_garbled(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    assert mcp_venv_bootstrap._venv_python_minor(venv) is None  # no pyvenv.cfg
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")  # no version line
    assert mcp_venv_bootstrap._venv_python_minor(venv) is None


def test_needs_python_reconcile_missing_venv(tmp_path):
    assert mcp_venv_bootstrap._needs_python_reconcile(tmp_path / "venv", (3, 13), tmp_path) is False


def test_needs_python_reconcile_older_interpreter(tmp_path):
    venv = tmp_path / "venv"
    _write_pyvenv(venv, 3, 10)
    assert mcp_venv_bootstrap._needs_python_reconcile(venv, (3, 13), tmp_path) is True


def test_needs_python_reconcile_current_or_newer(tmp_path):
    venv = tmp_path / "venv"
    _write_pyvenv(venv, 3, 13)
    assert mcp_venv_bootstrap._needs_python_reconcile(venv, (3, 13), tmp_path) is False
    _write_pyvenv(venv, 3, 14)
    assert mcp_venv_bootstrap._needs_python_reconcile(venv, (3, 13), tmp_path) is False


def test_needs_python_reconcile_ceiling_marker_skips(tmp_path):
    """venv below target but already reconciled to it (real requires-python ceiling)."""
    venv = tmp_path / "venv"
    _write_pyvenv(venv, 3, 13)
    (tmp_path / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"python": "3.14"}))
    assert mcp_venv_bootstrap._needs_python_reconcile(venv, (3, 14), tmp_path) is False


def test_needs_python_reconcile_stale_marker_rebuilds(tmp_path):
    """A marker from an OLD target doesn't suppress a new reconcile."""
    venv = tmp_path / "venv"
    _write_pyvenv(venv, 3, 10)
    (tmp_path / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"python": "3.12"}))
    assert mcp_venv_bootstrap._needs_python_reconcile(venv, (3, 13), tmp_path) is True


@pytest.mark.asyncio
async def test_interpreter_stale_rebuilds_pinned_and_marks(fake_mcps_root):
    """A venv on an older interpreter is rmtree'd, rebuilt pinned to target, marked."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp", "server": {"runtime": "python"},
    })
    (mcp_dir / "requirements.txt").write_text("requests==2.31\n")
    venv = mcp_dir / "venv"
    # Built on one minor BELOW the test runner → interpreter-stale...
    _write_pyvenv(venv, sys.version_info[0], sys.version_info[1] - 1)
    # ...but venv mtime newer than reqs, so this is NOT the mtime path.
    import os
    future = time.time() + 60
    os.utime(venv, (future, future))

    with patch("services.mcp.mcp_venv_bootstrap._uv_bin_if_present", return_value="/fake/uv"), \
         patch("services.mcp.mcp_venv_bootstrap._uv_venv_pinned", new_callable=AsyncMock) as pin, \
         patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h")) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()

    assert results == {"py-mcp": "ok-py-reconcile"}
    pin.assert_awaited_once()            # rebuild was pinned to the proxy interpreter
    inst.assert_awaited_once()
    assert not venv.exists()             # old venv removed (pin + install are mocked)
    marker = json.loads((mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).read_text())
    assert marker["python"] == f"{sys.version_info[0]}.{sys.version_info[1]}"


@pytest.mark.asyncio
async def test_interpreter_current_is_fresh(fake_mcps_root):
    """A venv on the proxy's own interpreter is not reconciled."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "py-mcp", {
        "name": "py-mcp", "server": {"runtime": "python"},
    })
    (mcp_dir / "requirements.txt").write_text("requests==2.31\n")
    venv = mcp_dir / "venv"
    _write_pyvenv(venv, sys.version_info[0], sys.version_info[1])
    import os
    future = time.time() + 60
    os.utime(venv, (future, future))
    mcp_venv_bootstrap._write_marker(
        mcp_dir,
        requirements_sha256=mcp_venv_bootstrap._requirements_hash(
            mcp_dir / "requirements.txt"),
    )

    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"py-mcp": "fresh"}
    inst.assert_not_awaited()


# ───────────────────── node native-addon reconciliation ────────────────


@pytest.mark.asyncio
async def test_node_absent_marker_records_no_rebuild(fake_mcps_root):
    """A freshly-built node_modules (no marker) is recorded, not rebuilt."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "node-mcp", {
        "name": "node-mcp", "server": {"runtime": "node"},
    })
    (mcp_dir / "node_modules").mkdir()
    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"node-mcp": "skipped-bundled-node"}
    spawn.assert_not_awaited()  # no npm rebuild on first encounter
    marker = json.loads((mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).read_text())
    assert marker["node_major"] == 24


@pytest.mark.asyncio
async def test_node_major_change_triggers_rebuild(fake_mcps_root):
    """A node MAJOR change since node_modules was built → npm rebuild + remark."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "node-mcp", {
        "name": "node-mcp", "server": {"runtime": "node"},
    })
    (mcp_dir / "node_modules").mkdir()
    (mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"rebuilt", None))
    fake_proc.returncode = 0
    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"node-mcp": "ok-node-rebuild"}
    marker = json.loads((mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).read_text())
    assert marker["node_major"] == 24


@pytest.mark.asyncio
async def test_node_rebuild_failure_is_advisory(fake_mcps_root):
    """A failed npm rebuild is reported but never fatal; marker is NOT advanced."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "node-mcp", {
        "name": "node-mcp", "server": {"runtime": "node"},
    })
    (mcp_dir / "node_modules").mkdir()
    (mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"gyp error", None))
    fake_proc.returncode = 1
    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc):
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"node-mcp": "skipped-node-rebuild-fail"}
    marker = json.loads((mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).read_text())
    assert marker["node_major"] == 22  # unchanged — retried next boot


class TestSuccessMarkerStaleness:
    """Interrupted installs poison the mtime heuristic (live-hit 2026-07-11):
    the success marker (requirements sha256, written only after a dep sync
    exits 0) is what the staleness check trusts."""

    def _mcp(self, tmp_path, with_pkg):
        from services.mcp.mcp_venv_bootstrap import _requirements_hash
        mcp = tmp_path / "m"
        mcp.mkdir()
        req = mcp / "requirements.txt"
        req.write_text("fastmcp\n")
        venv = mcp / "venv"
        venv.mkdir()  # created after reqs → mtime-fresh
        if with_pkg:
            sp = venv / "lib" / "python3.13" / "site-packages" / "pkg-1.0.dist-info"
            sp.mkdir(parents=True)
        return mcp, req, venv, _requirements_hash(req)

    def test_interrupted_install_is_stale(self, tmp_path):
        from services.mcp.mcp_venv_bootstrap import _venv_is_stale
        mcp, req, venv, _ = self._mcp(tmp_path, with_pkg=False)
        assert _venv_is_stale(req, venv, mcp) is True

    def test_healthy_unmarked_venv_backfills_without_rebuild(self, tmp_path):
        from services.mcp.mcp_venv_bootstrap import _venv_is_stale, _read_marker
        mcp, req, venv, h = self._mcp(tmp_path, with_pkg=True)
        assert _venv_is_stale(req, venv, mcp) is False
        assert _read_marker(mcp).get("requirements_sha256") == h

    def test_marker_match_is_fresh_even_when_venv_empty(self, tmp_path):
        from services.mcp.mcp_venv_bootstrap import _venv_is_stale, _write_marker
        mcp, req, venv, h = self._mcp(tmp_path, with_pkg=False)
        _write_marker(mcp, requirements_sha256=h)
        assert _venv_is_stale(req, venv, mcp) is False

    def test_marker_mismatch_is_stale(self, tmp_path):
        from services.mcp.mcp_venv_bootstrap import _venv_is_stale, _write_marker
        mcp, req, venv, _ = self._mcp(tmp_path, with_pkg=True)
        _write_marker(mcp, requirements_sha256="deadbeef")
        assert _venv_is_stale(req, venv, mcp) is True

    def test_no_mcp_dir_keeps_pure_mtime_behavior(self, tmp_path):
        from services.mcp.mcp_venv_bootstrap import _venv_is_stale
        _, req, venv, _ = self._mcp(tmp_path, with_pkg=False)
        assert _venv_is_stale(req, venv) is False
