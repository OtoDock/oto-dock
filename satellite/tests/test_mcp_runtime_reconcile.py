"""Tests for satellite MCP runtime reconciliation (Python/Node platform bumps).

Mirrors proxy/tests/test_mcp_venv_bootstrap.py — the satellite self-reconciles MCP
venvs/addons that lag THIS host's interpreter/node after an update. ``install_mcp``
+ ``_uv_venv_pinned`` are mocked; the decision/marker logic is the unit under test.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from satellite.sessions import mcp_install_support as mis
from satellite._vendored.mcp_installer import InstallResult


def _make_mcp(root: Path, category: str, name: str, manifest: dict) -> Path:
    d = root / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


def _write_pyvenv(venv_dir: Path, major: int, minor: int, micro: int = 0) -> None:
    venv_dir.mkdir(parents=True, exist_ok=True)
    (venv_dir / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {major}.{minor}.{micro}\n")


# ── helper units ────────────────────────────────────────────────────────


def test_venv_python_minor_parses(tmp_path):
    v = tmp_path / "venv"
    _write_pyvenv(v, 3, 10, 5)
    assert mis._venv_python_minor(v) == (3, 10)


def test_venv_python_minor_missing(tmp_path):
    v = tmp_path / "venv"
    v.mkdir()
    assert mis._venv_python_minor(v) is None


def test_needs_python_reconcile_logic(tmp_path):
    v = tmp_path / "venv"
    _write_pyvenv(v, 3, 10)
    assert mis._needs_python_reconcile(v, (3, 13), tmp_path) is True
    _write_pyvenv(v, 3, 13)
    assert mis._needs_python_reconcile(v, (3, 13), tmp_path) is False
    # below target but already reconciled to it → ceiling, skip (churn-free)
    _write_pyvenv(v, 3, 13)
    (tmp_path / mis._RUNTIME_MARKER).write_text(json.dumps({"python": "3.14"}))
    assert mis._needs_python_reconcile(v, (3, 14), tmp_path) is False


# ── reconcile_mcp_runtimes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_rebuilds_stale_interpreter(tmp_path):
    mcps = tmp_path / "mcps"
    (mcps / "custom").mkdir(parents=True)
    mcp = _make_mcp(mcps, "custom", "py-mcp", {"name": "py-mcp", "server": {"runtime": "python"}})
    (mcp / "requirements.txt").write_text("requests==2.31\n")
    venv = mcp / "venv"
    _write_pyvenv(venv, sys.version_info[0], sys.version_info[1] - 1)  # one below host

    with patch.object(mis, "_uv_venv_pinned", new_callable=AsyncMock) as pin, \
         patch("satellite._vendored.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h")) as inst:
        results = await mis.reconcile_mcp_runtimes(mcps, "/fake/uv")

    assert results == {"py-mcp": "ok-py-reconcile"}
    pin.assert_awaited_once()
    inst.assert_awaited_once()
    assert not venv.exists()  # old venv removed; pin + install are mocked
    marker = json.loads((mcp / mis._RUNTIME_MARKER).read_text())
    assert marker["python"] == f"{sys.version_info[0]}.{sys.version_info[1]}"


@pytest.mark.asyncio
async def test_reconcile_skips_current_interpreter(tmp_path):
    mcps = tmp_path / "mcps"
    (mcps / "custom").mkdir(parents=True)
    mcp = _make_mcp(mcps, "custom", "py-mcp", {"name": "py-mcp", "server": {"runtime": "python"}})
    (mcp / "requirements.txt").write_text("x\n")
    _write_pyvenv(mcp / "venv", sys.version_info[0], sys.version_info[1])

    with patch("satellite._vendored.mcp_installer.install_mcp", new_callable=AsyncMock) as inst:
        results = await mis.reconcile_mcp_runtimes(mcps, "/fake/uv")
    assert results == {}
    inst.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_node_major_change(tmp_path):
    mcps = tmp_path / "mcps"
    (mcps / "custom").mkdir(parents=True)
    mcp = _make_mcp(mcps, "custom", "node-mcp", {"name": "node-mcp", "server": {"runtime": "node"}})
    (mcp / "node_modules").mkdir()
    (mcp / mis._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))

    fake = AsyncMock()
    fake.communicate = AsyncMock(return_value=(b"ok", None))
    fake.returncode = 0
    with patch.object(mis, "_node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake):
        results = await mis.reconcile_mcp_runtimes(mcps, None)
    assert results == {"node-mcp": "ok-node-rebuild"}
    assert json.loads((mcp / mis._RUNTIME_MARKER).read_text())["node_major"] == 24


@pytest.mark.asyncio
async def test_reconcile_node_absent_marker_records_only(tmp_path):
    mcps = tmp_path / "mcps"
    (mcps / "custom").mkdir(parents=True)
    mcp = _make_mcp(mcps, "custom", "node-mcp", {"name": "node-mcp", "server": {"runtime": "node"}})
    (mcp / "node_modules").mkdir()
    with patch.object(mis, "_node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        results = await mis.reconcile_mcp_runtimes(mcps, None)
    assert results == {}                 # nothing rebuilt on first encounter
    spawn.assert_not_awaited()
    assert json.loads((mcp / mis._RUNTIME_MARKER).read_text())["node_major"] == 24


@pytest.mark.asyncio
async def test_reconcile_empty_when_nothing_present(tmp_path):
    assert await mis.reconcile_mcp_runtimes(tmp_path / "nope", None) == {}


@pytest.mark.asyncio
async def test_uv_venv_pinned_runs_under_the_installers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OTO_SATELLITE_PRIVATE", "1")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("UV_PYTHON_INSTALL_MIRROR", "https://mirror.example/python")
    fake = AsyncMock()
    fake.communicate = AsyncMock(return_value=(b"", None))
    fake.returncode = 0
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake) as spawn:
        await mis._uv_venv_pinned("/usr/bin/uv", tmp_path / "m" / "venv", (3, 12), tmp_path / "m")
    env = spawn.await_args.kwargs["env"]
    assert "OTO_SATELLITE_PRIVATE" not in env
    assert env["UV_NO_CONFIG"] == "1" and env["UV_LINK_MODE"] == "copy"
    assert env["UV_PYTHON_INSTALL_DIR"].endswith(".uv-python")
    assert env["UV_PYTHON_INSTALL_MIRROR"] == "https://mirror.example/python"


@pytest.mark.asyncio
async def test_reconcile_never_rebuilds_a_community_node_mcp(tmp_path):
    mcps = tmp_path / "mcps"
    (mcps / "community").mkdir(parents=True)
    mcp = _make_mcp(mcps, "community", "node-mcp", {"name": "node-mcp", "server": {"runtime": "node"}})
    (mcp / "node_modules").mkdir()
    (mcp / mis._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))
    with patch.object(mis, "_node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        results = await mis.reconcile_mcp_runtimes(mcps, None)
    assert results == {"node-mcp": "skipped-bundled-node"}
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_never_builds_a_catalog_python_folder(tmp_path):
    """A requirements build runs the file's own sources as this account; a
    community folder is reported and left alone, a custom one is rebuilt."""
    mcps = tmp_path / "mcps"
    for cat in ("custom", "community"):
        (mcps / cat).mkdir(parents=True)
        mcp = _make_mcp(mcps, cat, f"{cat}-py", {"name": f"{cat}-py", "server": {"runtime": "python"}})
        (mcp / "requirements.txt").write_text("requests==2.31\n")
        _write_pyvenv(mcp / "venv", sys.version_info[0], sys.version_info[1] - 1)

    with patch.object(mis, "_uv_venv_pinned", new_callable=AsyncMock), \
         patch("satellite._vendored.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h")) as inst:
        results = await mis.reconcile_mcp_runtimes(mcps, "/fake/uv")

    assert results == {"custom-py": "ok-py-reconcile", "community-py": "skipped-community-python"}
    inst.assert_awaited_once()
    assert (mcps / "community" / "community-py" / "venv").exists()


# ── the npm rebuild and the pinned venv run as every install subprocess does ─


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def _node_mcp_due_for_rebuild(tmp_path: Path) -> tuple[Path, Path]:
    mcps = tmp_path / "mcps"
    (mcps / "custom").mkdir(parents=True)
    mcp = _make_mcp(mcps, "custom", "node-mcp", {"name": "node-mcp", "server": {"runtime": "node"}})
    (mcp / "node_modules").mkdir()
    (mcp / mis._RUNTIME_MARKER).write_text(json.dumps({"node_major": 22}))
    return mcps, mcp


@pytest.mark.asyncio
async def test_npm_rebuild_runs_under_the_installers_environment(tmp_path, monkeypatch):
    """``npm rebuild`` runs the packages' lifecycle scripts: they see the
    installer's allowlisted environment, never the satellite's own."""
    monkeypatch.setenv("OTO_SATELLITE_PRIVATE", "1")
    mcps, _ = _node_mcp_due_for_rebuild(tmp_path)
    fake = AsyncMock()
    fake.communicate = AsyncMock(return_value=(b"ok", None))
    fake.returncode = 0
    with patch.object(mis, "_node_major", return_value=24), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake) as spawn:
        results = await mis.reconcile_mcp_runtimes(mcps, None)
    assert results == {"node-mcp": "ok-node-rebuild"}
    env = spawn.await_args.kwargs["env"]
    assert env is not None and "OTO_SATELLITE_PRIVATE" not in env
    assert env["UV_NO_CONFIG"] == "1"


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
async def test_a_hung_npm_rebuild_is_killed_and_retried_next_start(tmp_path, monkeypatch):
    from satellite._vendored import mcp_installer
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "npm.pids"
    npm = bin_dir / "npm"
    npm.write_text(f'#!/bin/sh\necho $$ >> "{pids}"\nsleep 20 & echo $! >> "{pids}"\nsleep 20\n')
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(mcp_installer, "DEFAULT_INSTALL_TIMEOUT", 1)
    mcps, mcp = _node_mcp_due_for_rebuild(tmp_path)
    with patch.object(mis, "_node_major", return_value=24):
        results = await asyncio.wait_for(mis.reconcile_mcp_runtimes(mcps, None), 10)
    assert results == {"node-mcp": "skipped-node-rebuild-fail"}
    assert json.loads((mcp / mis._RUNTIME_MARKER).read_text())["node_major"] == 22
    for _ in range(40):
        if not any(_alive(int(p)) for p in pids.read_text().split()):
            break
        await asyncio.sleep(0.05)
    assert not [p for p in pids.read_text().split() if _alive(int(p))]
