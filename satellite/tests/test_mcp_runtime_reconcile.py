"""Tests for satellite MCP runtime reconciliation (Python/Node platform bumps).

Mirrors proxy/tests/test_mcp_venv_bootstrap.py — the satellite self-reconciles MCP
venvs/addons that lag THIS host's interpreter/node after an update. ``install_mcp``
+ ``_uv_venv_pinned`` are mocked; the decision/marker logic is the unit under test.
"""

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
