"""Startup pass that keeps every bundled MCP's runtime current.

Runs once at proxy startup after ``mcp_registry.scan_manifests()``. Walks
``mcps/custom/`` and ``mcps/community/`` (the latter is gitignored — only
locally-present community MCPs are touched) and reconciles each MCP's runtime
to the platform:

- **Python deps** — if a Python MCP's ``venv/`` is missing OR ``requirements.txt``
  is newer than the venv, rebuild via the shared ``mcp_installer.install_mcp``.
  Platform-shipped (``custom``) folders only: a community MCP installs from its
  ``server.source`` and its folder's ``requirements.txt`` is never built.
- **Python interpreter** — every venv the pass creates is pinned to the proxy's
  own minor (``install_mcp``'s ``python_spec``), the first one included. A venv
  built on an interpreter OLDER than the proxy's (e.g. a 3.10 venv after the
  platform bumped to 3.13), or one whose interpreter lies outside the folders
  the session sandbox binds, is deleted and rebuilt the same way. A floor the
  package asks for is respected and recorded in the ``.oto-runtime.json``
  marker; a pin that fails is recorded too and retried after a day, never at
  every boot (the pass runs before the proxy serves).
- **Node native addons** — if the system ``node`` MAJOR changed since an MCP's
  ``node_modules/`` was built, ``npm rebuild`` its native bindings (advisory).

This closes the gap where a fresh clone, a ``git pull`` that bumps
``requirements.txt``, or a platform runtime bump leaves an MCP's venv/addons stale
and the MCP silently fails to launch.

The pass ends by removing the uv-managed interpreters no venv uses
(``mcp_installer.prune_uv_pythons``); nothing installs while it runs.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from services.mcp import mcp_installer, mcp_manifest_types as _mt

logger = logging.getLogger("otodock.venv-bootstrap")

# Per-MCP runtime marker (next to manifest.json, SURVIVES a venv rmtree). Records
# the platform runtime an MCP was last reconciled against so the sweep is
# idempotent and never churns a ceiling-pinned MCP.
_RUNTIME_MARKER = ".oto-runtime.json"


# A failed interpreter pin is tried again after this long, not at every boot.
PIN_RETRY_AFTER = timedelta(hours=24)

# The system folders the session sandbox binds read-only that an interpreter
# can live in (``core/sandbox/sandbox.py``'s system binds; ``/snap/bin`` holds
# no interpreter). ``mcps/.uv-python`` and the MCP's own folder join them.
_SYSTEM_INTERPRETER_ROOTS = ("/usr", "/bin", "/lib", "/lib64", "/lib32", "/sbin")


def find_uv() -> str | None:
    """The ``uv`` binary the platform installs with, or ``None``: the standard
    locations first (a readable, executable file), then ``PATH``.

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
    return shutil.which("uv")


def proxy_python_spec() -> str:
    """The proxy's own Python minor, the interpreter MCP venvs are pinned to."""
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def sandbox_interpreter_roots(mcp_dir: Path | None = None) -> list[Path]:
    """The resolved folders a venv's interpreter must live in for a session
    sandbox to run it: the system binds, ``mcps/.uv-python`` and, given, the
    MCP's own folder."""
    roots = [Path(p).resolve() for p in _SYSTEM_INTERPRETER_ROOTS]
    roots.append(Path(config.MCPS_DIR).resolve() / ".uv-python")
    if mcp_dir is not None:
        roots.append(Path(mcp_dir).resolve())
    return roots


def venv_outside_sandbox(mcp_dir: Path) -> bool:
    """Whether the MCP's ``venv/`` runs on an interpreter the session sandbox
    does not bind (a hand-built venv on ``~/.local/share/uv``, say)."""
    venv_dir = Path(mcp_dir) / "venv"
    return venv_dir.is_dir() and mcp_installer.venv_outside(
        venv_dir, sandbox_interpreter_roots(mcp_dir))


def _pin_failed_recently(mcp_dir: Path, spec: str, now: datetime | None = None) -> str:
    """The time of a failed pin to ``spec`` within ``PIN_RETRY_AFTER`` ("" when
    none): the venv stays as it is until then."""
    failed = _read_marker(mcp_dir).get("python_pin_failed") or {}
    if not isinstance(failed, dict) or failed.get("target") != spec:
        return ""
    try:
        at = datetime.fromisoformat(str(failed.get("at", "")))
    except ValueError:
        return ""
    now = now or datetime.now(timezone.utc)
    return failed["at"] if now - at < PIN_RETRY_AFTER else ""


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
        # The packages' lifecycle scripts run here: the installer's
        # environment and time bound, as for every install subprocess. A
        # timeout keeps the old marker, so the next boot tries again.
        rc, out = await mcp_installer._run_bounded(
            ["npm", "rebuild"], cwd=str(mcp_dir), env=mcp_installer._install_env(),
            timeout=mcp_installer.DEFAULT_INSTALL_TIMEOUT,
        )
        if rc == 0:
            _write_marker(mcp_dir, node_major=current)
            return "ok-node-rebuild"
        logger.warning("npm rebuild for %s failed: %s", name, out[-300:])
        return "skipped-node-rebuild-fail"
    except Exception:
        logger.exception("npm rebuild for %s errored", name)
        return "skipped-node-rebuild-fail"


async def ensure_bundled_venvs_at_startup() -> dict[str, str]:
    """Build, refresh, or runtime-reconcile every bundled MCP.

    Returns a map of ``mcp_name`` → outcome string for logging. Outcomes:

    - ``"ok"`` — venv was (re)built (missing or deps changed)
    - ``"ok-py-reconcile"`` — venv rebuilt because its interpreter lagged the proxy
    - ``"ok-py-relocate"`` — venv rebuilt because its interpreter lay outside the
      folders the session sandbox binds
    - ``"ok-node-rebuild"`` — node addons rebuilt after a node MAJOR bump
    - ``"failed"`` — install_mcp returned ``ok=False``
    - ``"exception"`` — install_mcp raised
    - ``"fresh"`` — venv exists, current deps + interpreter; skipped
    - ``"skipped-no-reqs"`` — Python MCP with no requirements.txt
    - ``"skipped-community-python"`` - a catalog Python MCP (installed from its
      ``server.source``, never from a folder ``requirements.txt``; one outside
      the sandbox's folders is offered as an update by the check)
    - ``"skipped-docker"`` — Docker MCP; handled by ``docker_manager``
    - ``"skipped-bundled-node"`` — Node MCP without ``node_modules`` to rebuild
    - ``"skipped-node-rebuild-fail"`` — npm rebuild failed (advisory)
    - ``"skipped-runtime-<x>"`` — unknown runtime

    Idempotent and cheap when everything is up to date.
    """
    results: dict[str, str] = {}
    mcps_root = Path(config.MCPS_DIR)
    if not mcps_root.is_dir():  # noqa: ASYNC240  # once at boot, before the minutes-long venv checks
        logger.warning(
            "ensure_bundled_venvs: MCPS_DIR %s not found; skipping", mcps_root,
        )
        return results

    uv = find_uv()
    target = sys.version_info[:2]  # the proxy's interpreter — venvs must not lag it
    spec = proxy_python_spec()

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

            if runtime == _mt.RUNTIME_DOCKER:
                results[name] = "skipped-docker"
                continue

            if runtime == _mt.RUNTIME_PYTHON:
                # A requirements build runs any index, VCS or sdist line the
                # file names, outside the wheels-only rule: platform-shipped
                # folders only (a community MCP installs from server.source).
                if category != "custom":
                    if venv_outside_sandbox(mcp_dir):
                        logger.warning(
                            "ensure_bundled_venvs: %s runs on an interpreter outside "
                            "the folders the session sandbox binds; Check updates "
                            "offers its rebuild", name,
                        )
                    results[name] = "skipped-community-python"
                    continue
                req_file = mcp_dir / "requirements.txt"
                venv_dir = mcp_dir / "venv"
                if not req_file.is_file():
                    results[name] = "skipped-no-reqs"
                    continue
                reqs_stale = _venv_is_stale(req_file, venv_dir, mcp_dir)
                interp_stale = _needs_python_reconcile(venv_dir, target, mcp_dir)
                relocate = venv_outside_sandbox(mcp_dir)
                rebuild = interp_stale or relocate
                failed_at = _pin_failed_recently(mcp_dir, spec) if rebuild else ""
                if failed_at:
                    logger.warning(
                        "ensure_bundled_venvs: the venv of %s stays as it is: its "
                        "rebuild on Python %s failed at %s, tried again a day later",
                        name, spec, failed_at,
                    )
                    rebuild = interp_stale = relocate = False
                if not reqs_stale and not rebuild:
                    results[name] = "fresh"
                    continue
                created = rebuild or not venv_dir.is_dir()
                if rebuild:
                    # Swapping the interpreter REQUIRES deleting the old venv —
                    # install_mcp reuses an existing one and creates a missing
                    # one on the pin. The success mark goes first: a rebuild
                    # that fails must not leave an empty venv reading fresh.
                    _forget_success(mcp_dir)
                    await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
                logger.info(
                    "ensure_bundled_venvs: %s venv for %s at %s",
                    "relocating" if relocate else "reconciling" if interp_stale else "building",
                    name, mcp_dir,
                )
                try:
                    result = await mcp_installer.install_mcp(
                        mcp_dir, "python", "", uv_bin=uv,
                        python_spec=spec, sandbox_roots=sandbox_interpreter_roots(mcp_dir),
                    )
                    if result.ok:
                        results[name] = ("ok-py-relocate" if relocate
                                         else "ok-py-reconcile" if interp_stale else "ok")
                        # Success mark: the dep sync exited 0 for THESE
                        # requirements — the staleness check trusts only this.
                        _write_marker(
                            mcp_dir,
                            requirements_sha256=_requirements_hash(req_file),
                        )
                        if created:
                            _mark_interpreter(mcp_dir, name, spec, result.venv_python,
                                              pinnable=uv is not None)
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

            if runtime == _mt.RUNTIME_NODE:
                # A rebuild runs the packages' own lifecycle scripts, which the
                # install refuses for catalog content: platform-shipped
                # folders only (a community MCP with a native addon already
                # depends on prebuilt binaries under --ignore-scripts).
                if category == "custom" and (mcp_dir / "node_modules").is_dir():
                    outcome = await _reconcile_node_addons(mcp_dir, name)
                    if outcome:
                        results[name] = outcome
                        continue
                results[name] = "skipped-bundled-node"
                continue

            results[name] = f"skipped-runtime-{runtime}"

    removed = await asyncio.to_thread(mcp_installer.prune_uv_pythons, mcps_root)
    if removed:
        logger.info("ensure_bundled_venvs: removed unused uv interpreters: %s", removed)
    return results


def _forget_success(mcp_dir: Path) -> None:
    """Drop the success mark (and the reconciled interpreter) before a venv is
    deleted for a rebuild: until the rebuild succeeds the venv is stale."""
    data = _read_marker(mcp_dir)
    dropped = [data.pop(key, None) for key in ("requirements_sha256", "python")]
    if dropped == [None, None]:
        return
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


def _mark_interpreter(mcp_dir: Path, name: str, spec: str, venv_python: str,
                      *, pinnable: bool = True) -> None:
    """Record what the venv just created runs on. Made on the pin or on a
    floor the package asked for, inside the sandbox's folders: reconciled.
    On uv's default (the pin failed) or still outside: the failure, so the
    next start leaves it alone for a day instead of reading it reconciled.
    With no uv on the host nothing can be pinned: the venv reads reconciled
    as before, never a daily rebuild."""
    data = _read_marker(mcp_dir)
    if not pinnable or (venv_python and not venv_outside_sandbox(mcp_dir)):
        data.pop("python_pin_failed", None)
        data["python"] = spec
    else:
        data.pop("python", None)
        data["python_pin_failed"] = {
            "target": spec, "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning(
            "ensure_bundled_venvs: %s could not be built on Python %s inside the "
            "folders the session sandbox binds; tried again a day later", name, spec,
        )
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


# A cause is logged once per MCP and process, not at every session start.
_causes_logged: set[tuple[str, str]] = set()


def start_failure_cause(manifest) -> str:
    """Why a stdio MCP's process may not have started, read from its folder
    without running anything: a missing venv or command, an interpreter the
    session sandbox does not bind, a Node MCP with no ``node_modules``. ""
    when the folder shows nothing wrong."""
    mcp_dir = Path(getattr(manifest, "mcp_dir", "") or "")
    server = getattr(manifest, "server", None)
    if not str(mcp_dir) or server is None or not mcp_dir.is_dir():
        return ""
    runtime = getattr(server, "runtime", "")
    command = str(getattr(server, "command", "") or "")
    if runtime == _mt.RUNTIME_PYTHON:
        if not (mcp_dir / "venv" / "pyvenv.cfg").is_file():
            return "its venv/ is missing"
        if venv_outside_sandbox(mcp_dir):
            paths = mcp_installer.venv_interpreter_paths(mcp_dir / "venv")
            return (f"its interpreter ({paths[-1] if paths else 'unknown'}) lies outside "
                    "the folders the session sandbox binds")
    if runtime == _mt.RUNTIME_NODE and (mcp_dir / "package.json").is_file() \
            and not (mcp_dir / "node_modules").is_dir():
        return "its node_modules/ is missing"
    if command and not os.path.isabs(command) and "/" in command \
            and not (mcp_dir / command).exists():
        return f"its command {command} is missing"
    return ""


def log_start_failures(manifests: list) -> None:
    """Log, once per MCP and cause, why each of ``manifests`` (MCPs a session
    reported failed) may not have started. Blocking file reads: call off the
    event loop."""
    for manifest in manifests:
        cause = start_failure_cause(manifest) or "no cause visible to the platform"
        key = (getattr(manifest, "name", ""), cause)
        if key in _causes_logged:
            continue
        _causes_logged.add(key)
        logger.warning("MCP %s failed to start: %s", key[0], cause)
