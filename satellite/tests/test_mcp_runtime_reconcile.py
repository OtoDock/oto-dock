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

from satellite._vendored.mcp_installer import InstallResult
from satellite.sessions import mcp_install_support as mis


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
               return_value=InstallResult(ok=True, log="ok", version_hash="h", venv_python="3.x")) as inst:
        results = await mis.reconcile_mcp_runtimes(mcps, "/fake/uv")

    assert results == {"py-mcp": "ok-py-reconcile"}
    pin.assert_awaited_once()
    inst.assert_awaited_once()
    assert inst.await_args.kwargs["python_spec"] == mis.mcp_python_spec()
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


def test_reconcile_ends_with_the_interpreter_prune(tmp_path):
    """The unused uv-managed interpreters go once the venvs are settled; a
    venv's interpreter stays."""
    if sys.platform == "win32":
        pytest.skip("the prune is POSIX only")
    uvp = tmp_path / ".uv-python"
    for name in ("cpython-3.13.14-linux", "cpython-3.11.9-linux"):
        (uvp / name / "bin").mkdir(parents=True)
        (uvp / name / "bin" / "python3").write_text("")
    d = _make_mcp(tmp_path, "community", "pkg", {"name": "pkg", "server": {"runtime": "python"}})
    (d / "venv").mkdir()
    (d / "venv" / "pyvenv.cfg").write_text(f"home = {uvp / 'cpython-3.13.14-linux' / 'bin'}\n")
    asyncio.run(mis.reconcile_mcp_runtimes(tmp_path, None))
    assert (uvp / "cpython-3.13.14-linux").is_dir()
    assert not (uvp / "cpython-3.11.9-linux").exists()


# ── a venv newer than the satellite (a Mac's Homebrew moved ahead) ───────


def _custom_py(mcps: Path, minor_delta: int, marker: dict | None = None) -> Path:
    (mcps / "custom").mkdir(parents=True, exist_ok=True)
    mcp = _make_mcp(mcps, "custom", "py-mcp", {"name": "py-mcp", "server": {"runtime": "python"}})
    (mcp / "requirements.txt").write_text("x\n")
    _write_pyvenv(mcp / "venv", sys.version_info[0], sys.version_info[1] + minor_delta)
    (mcp / "venv" / "old-file").write_text("the old venv")
    if marker is not None:
        (mcp / mis._RUNTIME_MARKER).write_text(json.dumps(marker))
    return mcp


@pytest.mark.parametrize("host, rebuilt", [("darwin", True), ("linux", False)])
def test_a_newer_venv_is_rebuilt_on_a_mac_only(tmp_path, monkeypatch, host, rebuilt):
    monkeypatch.setattr(mis.config, "HOST", mis.config.of(host))
    mcp = _custom_py(tmp_path, +2)
    assert mis._needs_python_reconcile(mcp / "venv", sys.version_info[:2], mcp) is rebuilt


def test_a_newer_venv_the_package_floor_kept_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(mis.config, "HOST", mis.config.of("darwin"))
    target = f"{sys.version_info[0]}.{sys.version_info[1]}"
    mcp = _custom_py(tmp_path, +1, marker={"python": target})
    assert mis._needs_python_reconcile(mcp / "venv", sys.version_info[:2], mcp) is False


@pytest.mark.asyncio
async def test_a_failed_rebuild_puts_the_old_venv_back(tmp_path, monkeypatch):
    monkeypatch.setattr(mis.config, "HOST", mis.config.of("darwin"))
    mcps = tmp_path / "mcps"
    mcp = _custom_py(mcps, +2)

    async def half_made(uv_bin, venv_dir, *a, **kw):
        venv_dir.mkdir()  # uv made a venv; the package install then fails

    with patch.object(mis, "_uv_venv_pinned", side_effect=half_made), \
         patch("satellite._vendored.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=False, log="error: offline")):
        results = await mis.reconcile_mcp_runtimes(mcps, "/fake/uv")
    assert results == {"py-mcp": "failed"}
    assert (mcp / "venv" / "old-file").read_text() == "the old venv"
    assert not list(mcps.glob(".venv-aside-*"))
    assert not (mcp / mis._RUNTIME_MARKER).exists()


@pytest.mark.asyncio
async def test_a_rebuild_that_succeeds_drops_the_old_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(mis.config, "HOST", mis.config.of("darwin"))
    mcps = tmp_path / "mcps"
    mcp = _custom_py(mcps, +2)
    with patch.object(mis, "_uv_venv_pinned", new_callable=AsyncMock), \
         patch("satellite._vendored.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h")):
        results = await mis.reconcile_mcp_runtimes(mcps, "/fake/uv")
    assert results == {"py-mcp": "ok-py-reconcile"}
    assert not (mcp / "venv" / "old-file").exists()
    assert not list(mcps.glob(".venv-aside-*"))


def test_the_mcp_venv_pin_is_this_satellites_own_python(tmp_path, monkeypatch):
    minor = f"{sys.version_info[0]}.{sys.version_info[1]}"
    monkeypatch.setattr(mis.config, "HOST", mis.config.of("darwin"))
    monkeypatch.setattr(mis.sys, "base_prefix", str(tmp_path))
    assert mis.mcp_python_spec() == minor  # no interpreter under the prefix
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / f"python{minor}").write_text("")
    assert mis.mcp_python_spec() == str(tmp_path / "bin" / f"python{minor}")
    monkeypatch.setattr(mis.config, "HOST", mis.config.of("windows"))
    assert mis.mcp_python_spec() is None


# ── the cause of a failed install, as the session is told ───────────────

_NO_RUST = """Using Python 3.15.2 environment at: venv
  × Failed to build `rpds-py==0.30.0`
  ├─▶ The build backend returned an error
  ╰─▶ Call to `maturin.build_wheel` failed (exit status: 1)
      [stderr]
      error: rustc not found
        Caused by: `cargo metadata` exited with an error: cargo not found
      hint: This usually indicates a problem with the package or the build environment."""
_NO_WHEEL = """  × No solution found when resolving dependencies:
  ╰─▶ Because pyyaml>=6.0.2 has no usable wheels and workspace-mcp==2.0.1 depends on pyyaml>=6.0.2, we can conclude that workspace-mcp==2.0.1 cannot be used."""
_NO_HEADERS = """  File "<string>", line 294, in <module>
RequiredDependencyException: The headers or library files could not be found for libheif, a required dependency."""


@pytest.mark.parametrize("log, cause", [
    (_NO_RUST, "Failed to build `rpds-py==0.30.0` / error: rustc not found / "
               "Caused by: `cargo metadata` exited with an error: cargo not found"),
    (_NO_WHEEL, "No solution found when resolving dependencies: / Because pyyaml>=6.0.2 has no usable wheels"),
    (_NO_HEADERS, "RequiredDependencyException: The headers or library files could not be found for libheif"),
])
def test_the_cause_of_a_failed_install_comes_first(log, cause):
    out = mis.install_failure_cause(log)
    assert out.startswith(cause)
    assert not any(g in out for g in ("×", "├", "╰", "▶", "hint:"))


def test_a_log_with_no_cause_line_keeps_its_end():
    assert mis.install_failure_cause("x" * 900 + " the last words").endswith("the last words")
    assert len(mis.install_failure_cause("x" * 900)) == 500
    long_cause = mis.install_failure_cause("error: " + "y " * 500)
    assert len(long_cause) <= 600 and long_cause.endswith("...")


def test_a_network_line_leads_the_cause_and_survives_the_cut():
    log = "\n".join([
        "  × Failed to build `rpds-py==0.30.0`",
        *(f"      error: failed to run custom build command for `pyo3-build-config v0.25.{i}`" for i in range(12)),
        "        failed to download from `https://index.crates.io/config.json`",
    ])
    out = mis.install_failure_cause(log)
    assert out.startswith("failed to download from `https://index.crates.io/config.json` / Failed to build")
    assert len(out) <= 600 and out.endswith("...")


# ── the rebuild's marker and the venvs a stopped rebuild set aside ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("venv_python, made_minor, marked", [
    ("/opt/py/bin/python3.x", None, True),   # made on the pin by the install
    ("", "target", True),                    # made on the pin beforehand
    ("", "newer", False),                    # uv's own default: tried again
])
async def test_a_rebuild_is_marked_reconciled_only_on_the_pin(tmp_path, monkeypatch, venv_python, made_minor, marked):
    monkeypatch.setattr(mis.config, "HOST", mis.config.of("darwin"))
    mcps = tmp_path / "mcps"
    mcp = _custom_py(mcps, +2)
    delta = {"target": 0, "newer": 2}

    async def made(uv_bin, venv_dir, *a, **kw):
        if made_minor:
            _write_pyvenv(venv_dir, sys.version_info[0], sys.version_info[1] + delta[made_minor])

    with patch.object(mis, "_uv_venv_pinned", side_effect=made), \
         patch("satellite._vendored.mcp_installer.install_mcp", new_callable=AsyncMock,
               return_value=InstallResult(ok=True, log="ok", version_hash="h", venv_python=venv_python)):
        assert await mis.reconcile_mcp_runtimes(mcps, "/fake/uv") == {"py-mcp": "ok-py-reconcile"}
    assert (mcp / mis._RUNTIME_MARKER).exists() is marked


def _stopped_rebuild(mcps: Path, *, marker: dict | None, half_built: bool) -> tuple[Path, Path]:
    """An MCP whose rebuild a stopped satellite left: its old venv aside,
    maybe a half-built new one, maybe the marker a finished rebuild writes."""
    mcp = _custom_py(mcps, +2, marker=marker)
    aside = mcps / ".venv-aside-custom-py-mcp"
    (mcp / "venv").rename(aside)
    if half_built:
        _write_pyvenv(mcp / "venv", sys.version_info[0], sys.version_info[1])
    return mcp, aside


@pytest.mark.parametrize("half_built", [False, True])
def test_a_rebuild_a_stopped_satellite_left_gets_its_old_venv_back(tmp_path, half_built):
    mcps = tmp_path / "mcps"
    mcp, aside = _stopped_rebuild(mcps, marker=None, half_built=half_built)
    mis._settle_venv_asides(mcps, sys.version_info[:2])
    assert (mcp / "venv" / "old-file").read_text() == "the old venv"
    assert not aside.exists()


def test_a_rebuild_that_finished_before_the_stop_keeps_its_new_venv(tmp_path):
    mcps = tmp_path / "mcps"
    target = f"{sys.version_info[0]}.{sys.version_info[1]}"
    mcp, aside = _stopped_rebuild(mcps, marker={"python": target}, half_built=True)
    mis._settle_venv_asides(mcps, sys.version_info[:2])
    assert (mcp / "venv" / "pyvenv.cfg").exists() and not (mcp / "venv" / "old-file").exists()
    assert not aside.exists()


def test_an_aside_whose_mcp_is_gone_is_dropped(tmp_path):
    mcps = tmp_path / "mcps"
    (mcps / ".venv-aside-custom-gone-mcp").mkdir(parents=True)
    mis._settle_venv_asides(mcps, sys.version_info[:2])
    assert not list(mcps.glob(".venv-aside-*"))
