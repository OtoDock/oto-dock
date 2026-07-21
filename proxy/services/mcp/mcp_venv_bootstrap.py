"""Startup pass that keeps every bundled MCP's runtime current.

Runs once at proxy startup after ``mcp_registry.scan_manifests()``. Walks
``mcps/custom/`` and ``mcps/community/`` (the latter is gitignored — only
locally-present community MCPs are touched) and reconciles each MCP's runtime
to the platform:

- **Python deps** — if a Python MCP's ``venv/`` is missing OR ``requirements.txt``
  is newer than the venv, rebuild via the shared ``mcp_installer.install_mcp``.
- **Python interpreter** — if a venv was built on an interpreter OLDER than the
  proxy's own (e.g. a 3.10 venv after the platform bumped to 3.13), delete it and
  rebuild pinned to the proxy interpreter. Upstream ``requires-python`` ceilings
  are respected: an MCP already reconciled to the current target is left alone (a
  ``.oto-runtime.json`` marker prevents re-installing a ceiling-pinned MCP — e.g.
  ``ha-mcp <3.14`` — on every boot).
- **Node native addons** — if the system ``node`` MAJOR changed since an MCP's
  ``node_modules/`` was built, ``npm rebuild`` its native bindings (advisory).

This closes the gap where a fresh clone, a ``git pull`` that bumps
``requirements.txt``, or a platform runtime bump leaves an MCP's venv/addons stale
and the MCP silently fails to launch.

The satellite has its own equivalent (it builds venvs locally during
``sync_mcps`` on the satellite host's interpreter); this module is proxy-only and
lives in a separate file so additions don't bump ``SHARED_MCP_INSTALLER_HASH``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config
from services.mcp import mcp_installer

logger = logging.getLogger("claude-proxy.venv-bootstrap")

# Per-MCP runtime marker (next to manifest.json, SURVIVES a venv rmtree). Records
# the platform runtime an MCP was last reconciled against so the sweep is
# idempotent and never churns a ceiling-pinned MCP.
_RUNTIME_MARKER = ".oto-runtime.json"


def _uv_bin_if_present() -> str | None:
    """First ``uv`` binary found in the standard locations, or ``None``.

    Mirrors ``satellite/sessions/mcp_install_support.py::_uv_bin_if_present`` so
    platform and satellite agree on where to look. ``uv`` is preferred over plain
    ``python -m venv`` + ``pip install`` because it's an order of magnitude faster
    and auto-fetches the right Python when an MCP pins one.
    """
    for candidate in (
        os.path.expanduser("~/.local/bin/uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK | os.R_OK):
            return candidate
    return None


def _venv_is_stale(
    deps_file: Path, target_dir: Path, mcp_dir: Path | None = None,
) -> bool:
    """``True`` when ``target_dir`` is missing, older than ``deps_file``, or —
    when ``mcp_dir`` is given — not success-marked for the current
    requirements. The marker hash (written only after a dep sync exits 0)
    catches an INTERRUPTED install: mtime alone called a created-but-
    never-synced venv fresh forever (live-hit 2026-07-11). Marker-less but
    populated venvs (pre-marker installs) are backfilled without a rebuild;
    marker-less EMPTY venvs rebuild."""
    if not target_dir.is_dir():
        return True
    try:
        if deps_file.stat().st_mtime > target_dir.stat().st_mtime:
            return True
    except OSError:
        return True
    if mcp_dir is None:
        return False
    current = _requirements_hash(deps_file)
    marked = _read_marker(mcp_dir).get("requirements_sha256")
    if marked == current:
        return False
    if marked is None and _venv_has_packages(target_dir):
        # Pre-marker healthy venv: adopt it (no fleet-wide rebuild storm on
        # upgrade) — a later requirements edit re-checks via the hash.
        _write_marker(mcp_dir, requirements_sha256=current)
        return False
    return True


def _venv_has_packages(venv_dir: Path) -> bool:
    """At least one installed distribution in the venv's site-packages."""
    try:
        for sp in venv_dir.glob("lib/python*/site-packages"):
            if any(sp.glob("*.dist-info")):
                return True
    except OSError:
        pass
    return False


def _requirements_hash(deps_file: Path) -> str:
    try:
        return hashlib.sha256(deps_file.read_bytes()).hexdigest()
    except OSError:
        return ""


def _venv_python_minor(venv_dir: Path) -> tuple[int, int] | None:
    """``(major, minor)`` of the interpreter recorded in ``venv/pyvenv.cfg``.

    Returns ``None`` when the cfg is missing or unparseable — in which case the
    interpreter check is skipped (never a blind rebuild).
    """
    try:
        text = (venv_dir / "pyvenv.cfg").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip().lower() in ("version", "version_info"):
            parts = val.strip().split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    return None


_node_major_cache: int | None = None
_node_major_probed = False


def _node_major() -> int | None:
    """Major version of the system ``node`` (cached), or ``None`` if absent."""
    global _node_major_cache, _node_major_probed
    if _node_major_probed:
        return _node_major_cache
    _node_major_probed = True
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            head = r.stdout.strip().lstrip("v").split(".")
            if head and head[0].isdigit():
                _node_major_cache = int(head[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return _node_major_cache


def _read_marker(mcp_dir: Path) -> dict:
    try:
        return json.loads((mcp_dir / _RUNTIME_MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_marker(mcp_dir: Path, **updates: object) -> None:
    data = _read_marker(mcp_dir)
    data.update(updates)
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


def _needs_python_reconcile(venv_dir: Path, target: tuple[int, int], mcp_dir: Path) -> bool:
    """``True`` when the venv interpreter lags the proxy and hasn't been reconciled.

    A venv whose interpreter is older than the proxy's is rebuilt — UNLESS the
    marker shows it's already been reconciled to this exact target, which means a
    real upstream ``requires-python`` ceiling (e.g. ``ha-mcp <3.14`` while the
    platform is 3.14) kept it on its highest allowed interpreter. Re-installing it
    every boot would just land on the same interpreter, so skip it.
    """
    if not venv_dir.is_dir():
        return False  # a missing venv is a full (re)build via the mtime path
    vm = _venv_python_minor(venv_dir)
    if vm is None or vm >= target:
        return False
    return _read_marker(mcp_dir).get("python") != f"{target[0]}.{target[1]}"


async def _uv_venv_pinned(uv_bin: str, venv_dir: Path, target: tuple[int, int], mcp_dir: Path) -> None:
    """Pre-create ``venv_dir`` on the proxy's interpreter so the subsequent
    ``install_mcp`` pip-installs into a venv that MATCHES the platform.

    Any uv-fetched Python lands in ``mcps/.uv-python/`` (already sandbox-mounted),
    keeping every MCP self-contained. Best-effort: on failure ``venv_dir`` is left
    absent so ``install_mcp`` falls back to creating it on uv's default Python.
    """
    spec = f"{target[0]}.{target[1]}"
    env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(mcp_dir.parent.parent / ".uv-python"),
        "UV_LINK_MODE": "copy",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            uv_bin, "venv", "--python", spec, str(venv_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "uv venv --python %s for %s failed: %s",
                spec, mcp_dir.name, out.decode(errors="replace")[-300:],
            )
    except Exception:
        logger.exception("uv venv --python %s for %s errored", spec, mcp_dir.name)


async def _reconcile_node_addons(mcp_dir: Path, name: str) -> str | None:
    """``npm rebuild`` a Node MCP's native addons when the system node MAJOR
    changed since ``node_modules`` was built.

    Returns an outcome string, or ``None`` to fall through to the default node
    outcome. Advisory: a rebuild failure is logged, never fatal. An absent marker
    means ``node_modules`` was just built on the current node — record it, don't
    rebuild (avoids a spurious first-run rebuild).
    """
    current = _node_major()
    if current is None:
        return None  # no node on this host — nothing to rebuild
    recorded = _read_marker(mcp_dir).get("node_major")
    if recorded is None:
        _write_marker(mcp_dir, node_major=current)
        return None
    if recorded == current:
        return None
    logger.info(
        "ensure_bundled_venvs: %s node %s→%s — npm rebuild",
        name, recorded, current,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "npm", "rebuild", cwd=str(mcp_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            _write_marker(mcp_dir, node_major=current)
            return "ok-node-rebuild"
        logger.warning(
            "npm rebuild for %s failed: %s", name, out.decode(errors="replace")[-300:],
        )
        return "skipped-node-rebuild-fail"
    except Exception:
        logger.exception("npm rebuild for %s errored", name)
        return "skipped-node-rebuild-fail"


async def ensure_bundled_venvs_at_startup() -> dict[str, str]:
    """Build, refresh, or runtime-reconcile every bundled MCP.

    Returns a map of ``mcp_name`` → outcome string for logging. Outcomes:

    - ``"ok"`` — venv was (re)built (missing or deps changed)
    - ``"ok-py-reconcile"`` — venv rebuilt because its interpreter lagged the proxy
    - ``"ok-node-rebuild"`` — node addons rebuilt after a node MAJOR bump
    - ``"failed"`` — install_mcp returned ``ok=False``
    - ``"exception"`` — install_mcp raised
    - ``"fresh"`` — venv exists, current deps + interpreter; skipped
    - ``"skipped-no-reqs"`` — Python MCP with no requirements.txt
    - ``"skipped-docker"`` — Docker MCP; handled by ``docker_manager``
    - ``"skipped-bundled-node"`` — Node MCP without ``node_modules`` to rebuild
    - ``"skipped-node-rebuild-fail"`` — npm rebuild failed (advisory)
    - ``"skipped-runtime-<x>"`` — unknown runtime

    Idempotent and cheap when everything is up to date.
    """
    results: dict[str, str] = {}
    mcps_root = Path(config.MCPS_DIR)
    if not mcps_root.is_dir():
        logger.warning(
            "ensure_bundled_venvs: MCPS_DIR %s not found; skipping", mcps_root,
        )
        return results

    uv = _uv_bin_if_present()
    target = sys.version_info[:2]  # the proxy's interpreter — venvs must not lag it

    for category in ("custom", "community"):
        cat_dir = mcps_root / category
        if not cat_dir.is_dir():
            continue
        for mcp_dir in sorted(cat_dir.iterdir()):
            if not mcp_dir.is_dir():
                continue
            manifest_file = mcp_dir / "manifest.json"
            if not manifest_file.is_file():
                continue
            try:
                manifest = json.loads(manifest_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "ensure_bundled_venvs: bad manifest at %s: %s", mcp_dir, e,
                )
                continue

            name = manifest.get("name") or mcp_dir.name
            runtime = manifest.get("server", {}).get("runtime", "")

            if runtime == "docker":
                results[name] = "skipped-docker"
                continue

            if runtime == "python":
                req_file = mcp_dir / "requirements.txt"
                venv_dir = mcp_dir / "venv"
                if not req_file.is_file():
                    results[name] = "skipped-no-reqs"
                    continue
                reqs_stale = _venv_is_stale(req_file, venv_dir, mcp_dir)
                interp_stale = _needs_python_reconcile(venv_dir, target, mcp_dir)
                if not reqs_stale and not interp_stale:
                    results[name] = "fresh"
                    continue
                if interp_stale:
                    # Swapping the interpreter REQUIRES deleting the old venv —
                    # install_mcp reuses an existing one. Pre-create it on the
                    # proxy interpreter so the rebuild matches the platform; on
                    # failure install_mcp recreates it on uv's default.
                    shutil.rmtree(venv_dir, ignore_errors=True)
                    if uv:
                        await _uv_venv_pinned(uv, venv_dir, target, mcp_dir)
                logger.info(
                    "ensure_bundled_venvs: %s venv for %s at %s",
                    "reconciling" if interp_stale else "building", name, mcp_dir,
                )
                try:
                    result = await mcp_installer.install_mcp(
                        mcp_dir, "python", "", uv_bin=uv,
                    )
                    if result.ok:
                        results[name] = "ok-py-reconcile" if interp_stale else "ok"
                        # Success mark: the dep sync exited 0 for THESE
                        # requirements — the staleness check trusts only this.
                        _write_marker(
                            mcp_dir,
                            requirements_sha256=_requirements_hash(req_file),
                        )
                        if interp_stale:
                            _write_marker(mcp_dir, python=f"{target[0]}.{target[1]}")
                    else:
                        results[name] = "failed"
                        logger.warning(
                            "ensure_bundled_venvs: %s install failed:\n%s",
                            name, result.log[-500:],
                        )
                except Exception:
                    logger.exception(
                        "ensure_bundled_venvs: %s unexpected error", name,
                    )
                    results[name] = "exception"
                continue

            if runtime == "node":
                if (mcp_dir / "node_modules").is_dir():
                    outcome = await _reconcile_node_addons(mcp_dir, name)
                    if outcome:
                        results[name] = outcome
                        continue
                results[name] = "skipped-bundled-node"
                continue

            results[name] = f"skipped-runtime-{runtime}"

    return results
