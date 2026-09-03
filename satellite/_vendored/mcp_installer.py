"""Shared MCP installer — runs on platform AND is vendored into satellite.

Single source of truth for:
- Parsing `server.source` fields — pinned (`npm:pkg@ver`, `pypi:pkg@ver`,
  `git+<url>@<ref>#subdirectory=<dir>`) or unpinned (`npm:pkg`, `pypi:pkg`,
  which install the latest published version).
- Installing Node/Python packages into a self-contained MCP directory
  (`node_modules/` or `venv/`), reading back the concrete installed version so
  the caller can pin it into the local manifest.
- Applying patch-package patches for Node MCPs.
- Checking and optionally installing system-level dependencies (libmagic,
  libreoffice, etc.) declared in the manifest's `system_requirements`.
- Computing a stable `version_hash` over install-relevant inputs so the
  satellite and proxy can agree on which version of an MCP is installed.

This module is **stdlib-only** (plus `asyncio`). No FastAPI, no psycopg.
The satellite imports its own vendored copy via
`scripts/sync-satellite-code.sh`; a hash check at satellite startup catches
drift. Changes here must be made in lockstep — if you edit this file, run
the sync script and update `SHARED_MCP_INSTALLER_HASH` in the satellite
config.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("mcp-installer")

# Default timeout for pip/npm install invocations. Can be overridden per call.
DEFAULT_INSTALL_TIMEOUT = 300  # seconds

# Cross-platform venv layout. Inlined here (instead of imported from
# satellite.config) so this module stays self-contained — it's vendored
# byte-for-byte into the satellite, and the satellite-side hash check
# would catch any external imports. On the platform side these always
# resolve to "bin"/"" because the proxy is Linux-only.
_VENV_BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# uv resolver naming a Python floor the current venv can't satisfy — both
# phrasings: pypi installs report "Requires Python>=3.13", requirements.txt
# resolves report "the current Python version (3.10.12) does not satisfy
# Python>=3.11". Captured spec feeds `uv venv --python <spec>`, which
# downloads a matching standalone CPython when the host lacks one.
_PY_FLOOR_RE = re.compile(
    r"Python(>=\s*\d+(?:\.\d+)?(?:\s*,\s*<\s*\d+(?:\.\d+)?)?)"
)


def _shell_argv(cmd: list[str]) -> list[str]:
    """Wrap an argv for cross-platform ``asyncio.create_subprocess_exec``.

    On Windows, common Node tooling (``npm``, ``npx``, ``yarn``) ships as
    ``*.cmd`` batch files, not ``*.exe``. The underlying ``CreateProcess``
    Win32 API only auto-appends ``.exe`` when resolving an executable name
    — it does NOT walk ``PATHEXT`` to find ``.cmd`` / ``.bat``. The result
    on Windows satellites is ``FileNotFoundError: [WinError 2]`` the moment
    we try to spawn ``npm install``. Routing through ``cmd /c`` lets the
    shell apply ``PATHEXT`` resolution and dispatch the batch file. On
    Unix this is a no-op pass-through.
    """
    if sys.platform == "win32":
        return ["cmd", "/c", *cmd]
    return cmd


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ParsedSource:
    """Decomposed `server.source` field."""
    registry: str   # "npm" | "pypi" | "docker" | ""
    package: str
    version: str


@dataclass
class SystemRequirementsInput:
    """Subset of an MCP's system_requirements declaration needed by the
    installer. Decoupled from the dataclass in services.mcp.mcp_registry so the
    installer can be used standalone (satellite doesn't import mcp_registry).
    """
    debian: list[str] = field(default_factory=list)
    ubuntu: list[str] = field(default_factory=list)
    rhel: list[str] = field(default_factory=list)
    arch: list[str] = field(default_factory=list)
    macos_brew: list[str] = field(default_factory=list)
    node_min: str = ""
    notes: str = ""


@dataclass
class MissingDep:
    """A required system package or interpreter that isn't available."""
    kind: str        # "package" | "interpreter"
    name: str        # package name or "python" / "node"
    required: str    # e.g. "libmagic1" or "3.10"
    actual: str = ""  # interpreter version found (or "")
    install_cmd: str = ""  # suggested install command


@dataclass
class InstallResult:
    """Outcome of an MCP install/update."""
    ok: bool
    log: str = ""
    version_hash: str = ""
    # Concrete version actually installed (read back from node_modules / the
    # venv). Set for npm/pypi installs; empty for docker/git+/no-op or when the
    # readback couldn't determine it. The caller pins this into the LOCAL
    # manifest (version + source) so detection, display, and satellite reinstall
    # stay deterministic. See pin_local_manifest in services/mcp/mcp_updater.py.
    resolved_version: str = ""
    missing_deps: list[MissingDep] = field(default_factory=list)


# Progress callback signature. Matches the shape `mcp_install_progress` WS
# events expect so the satellite can pipe straight through.
ProgressCb = Callable[[dict], Awaitable[None]] | Callable[[dict], None] | None


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------


def _spec_is_safe(s: str) -> bool:
    """Reject a package/version spec that could inject an argv flag into the
    npm/pip command (leading ``-``) or carry whitespace / shell / control chars.
    Allows the ordinary package-name + version-specifier charset."""
    if not s or s[0] == "-":
        return False
    if any(c.isspace() or ord(c) < 0x20 for c in s):
        return False
    return all(c.isalnum() or c in "@/._+-~^<>=!,*" for c in s)


def parse_source(source: str) -> ParsedSource | None:
    """Parse `server.source` into (registry, package, version).

    Community node/python MCPs are **unpinned** in the catalog — the source is a
    bare package pointer (`npm:pkg`, `pypi:pkg`) and the upstream registry is the
    version of record. A pinned form (`npm:pkg@1.2.3`, `pypi:pkg@1.2.3`) is also
    accepted: the proxy writes one into each install's LOCAL manifest after
    resolving the concrete version, so satellites reinstall deterministically.

    Handles npm scoped packages (`npm:@scope/name`, `npm:@scope/name@ver`).
    Returns ``version=""`` for an unpinned source. Returns ``None`` for Docker,
    empty, or empty-package sources.
    """
    if not source:
        return None
    if source.startswith("npm:"):
        pkg_ver = source[4:]
        # The leading '@' of a scoped name is part of the package; the version
        # separator is the first '@' AFTER it. find() (not index()) lets an
        # unpinned source fall through to version="" instead of raising.
        at_idx = pkg_ver.find("@", 1) if pkg_ver.startswith("@") else pkg_ver.find("@")
        package = pkg_ver if at_idx == -1 else pkg_ver[:at_idx]
        version = "" if at_idx == -1 else pkg_ver[at_idx + 1:]
        if not package:
            return None
        if not _spec_is_safe(package) or (version and not _spec_is_safe(version)):
            logger.warning("Rejecting unsafe npm source spec: %r", source)
            return None
        return ParsedSource(registry="npm", package=package, version=version)
    if source.startswith("pypi:"):
        pkg_ver = source[5:]
        at_idx = pkg_ver.find("@")
        package = pkg_ver if at_idx == -1 else pkg_ver[:at_idx]
        version = "" if at_idx == -1 else pkg_ver[at_idx + 1:]
        if not package:
            return None
        if not _spec_is_safe(package) or (version and not _spec_is_safe(version)):
            logger.warning("Rejecting unsafe pypi source spec: %r", source)
            return None
        return ParsedSource(registry="pypi", package=package, version=version)
    if source.startswith("git+"):
        # pip VCS source: git+https://host/repo.git@<ref>#subdirectory=<dir>
        # (the official Blender MCP, and any GitHub/Gitea-only MCP). install_mcp
        # hands the WHOLE string to pip/uv; this parse is for display/validation
        # only. The URL carries no credentials, so the last '@' before any '#'
        # is the git ref.
        url_part = source[4:].split("#", 1)[0]
        if "@" in url_part:
            url_no_ref, ref = url_part.rsplit("@", 1)
        else:
            url_no_ref, ref = url_part, ""
        return ParsedSource(registry="git", package=url_no_ref, version=ref)
    if source.startswith("docker:"):
        return ParsedSource(registry="docker", package=source[7:], version="")
    return None


# ---------------------------------------------------------------------------
# System-level checks
# ---------------------------------------------------------------------------


def _detect_os_keys() -> list[str]:
    """Return manifest-key(s) applicable to this host, most-specific first.

    E.g. on Ubuntu 22.04 returns `["ubuntu", "debian"]` so `ubuntu:` entries
    override `debian:` ones. On macOS returns `["macos_brew"]`.
    """
    system = platform.system().lower()
    if system == "darwin":
        return ["macos_brew"]
    if system == "windows":
        # Windows MCPs can declare `windows:` entries in
        # `system_requirements`. We don't probe winget/choco from here
        # (no reliable cross-version registry path), so install_system_
        # requirements is a no-op on Windows — users install deps via
        # winget/Chocolatey at install.ps1 time or out of band.
        return ["windows"]
    if system == "linux":
        # Read /etc/os-release (modern distros)
        try:
            data = Path("/etc/os-release").read_text(errors="replace")
        except OSError:
            return ["debian"]
        fields = {}
        for line in data.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip().strip('"')
        like = (fields.get("ID_LIKE", "") + " " + fields.get("ID", "")).lower().split()
        out: list[str] = []
        if "ubuntu" in like:
            out.append("ubuntu")
        if "debian" in like:
            out.append("debian")
        if "rhel" in like or "fedora" in like or "centos" in like:
            out.append("rhel")
        if "arch" in like:
            out.append("arch")
        return out or ["debian"]
    return []


def _packages_for_host(req: SystemRequirementsInput) -> tuple[str, list[str]]:
    """Return (os_family, package_list) applicable to this host."""
    for key in _detect_os_keys():
        pkgs = getattr(req, key, [])
        if pkgs:
            return key, list(pkgs)
    return ("", [])


def _is_package_installed(family: str, pkg: str) -> bool:
    """Best-effort check via the host's package DB."""
    if family == "windows":
        # Windows has no standardized cross-tool package DB (winget/choco/
        # MSI installers each have their own state). Assume installed
        # and let the MCP runtime fail loudly if a dep is actually
        # missing — baseline-tools.ps1 already covered the common cases.
        return True
    try:
        if family in ("debian", "ubuntu"):
            r = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0 and "install ok installed" in r.stdout
        if family == "rhel":
            r = subprocess.run(
                ["rpm", "-q", pkg],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        if family == "arch":
            r = subprocess.run(
                ["pacman", "-Q", pkg],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        if family == "macos_brew":
            r = subprocess.run(
                ["brew", "list", "--formula", pkg],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def _install_suggest(family: str, pkg: str) -> str:
    if family in ("debian", "ubuntu"):
        return f"sudo apt install -y {pkg}"
    if family == "rhel":
        return f"sudo dnf install -y {pkg}"
    if family == "arch":
        return f"sudo pacman -S --noconfirm {pkg}"
    if family == "macos_brew":
        return f"brew install {pkg}"
    if family == "windows":
        return f"winget install -e --id {pkg}  (or 'choco install {pkg}')"
    return ""


def _version_tuple(s: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def _node_version() -> str:
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip().lstrip("v")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def check_system_requirements(req: SystemRequirementsInput) -> list[MissingDep]:
    """Return the list of missing system deps + interpreter mismatches.

    Empty list → all requirements satisfied.
    """
    missing: list[MissingDep] = []

    # Interpreter floor. Only Node has one: a Python MCP's interpreter floor lives
    # in its upstream `requires-python`, which uv reads + auto-fetches at install
    # (mcp_installer recreates the venv on the right Python), so the platform never
    # gates on a separate `python_min`.
    if req.node_min:
        actual = _node_version()
        if not actual or _version_tuple(actual) < _version_tuple(req.node_min):
            missing.append(MissingDep(
                kind="interpreter", name="node",
                required=req.node_min, actual=actual or "not installed",
            ))

    # System packages (most-specific OS first)
    family, pkgs = _packages_for_host(req)
    if family:
        for pkg in pkgs:
            if not _is_package_installed(family, pkg):
                missing.append(MissingDep(
                    kind="package", name=pkg, required=pkg,
                    install_cmd=_install_suggest(family, pkg),
                ))
    return missing


def install_system_requirements(
    req: SystemRequirementsInput, *, dry_run: bool = False,
) -> tuple[bool, str]:
    """Run the host package manager to install missing system packages.

    Only called when the caller has explicitly opted in (e.g.
    MCP_AUTO_INSTALL_SYSTEM_DEPS=true on the proxy). Requires sudo;
    returns (False, error_msg) if sudo is unavailable or install fails.
    """
    family, pkgs = _packages_for_host(req)
    if not family or not pkgs:
        return True, ""
    missing = [p for p in pkgs if not _is_package_installed(family, p)]
    if not missing:
        return True, "All system requirements satisfied."

    if dry_run:
        return True, f"Would install {family}: {', '.join(missing)}"

    cmd: list[str]
    if family in ("debian", "ubuntu"):
        cmd = ["sudo", "apt", "install", "-y", *missing]
    elif family == "rhel":
        cmd = ["sudo", "dnf", "install", "-y", *missing]
    elif family == "arch":
        cmd = ["sudo", "pacman", "-S", "--noconfirm", *missing]
    elif family == "macos_brew":
        cmd = ["brew", "install", *missing]
    else:
        return False, f"Unknown package manager for family={family}"

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode == 0, (r.stdout + r.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Install (npm / pypi)
# ---------------------------------------------------------------------------


async def _emit(progress_cb: ProgressCb, event: dict) -> None:
    if progress_cb is None:
        return
    try:
        r = progress_cb(event)
        if asyncio.iscoroutine(r):
            await r
    except Exception:
        logger.exception("progress_cb raised")


# ---------------------------------------------------------------------------
# Installed-version readback — resolve the concrete version actually installed
# so the caller can pin it into the LOCAL manifest.
# ---------------------------------------------------------------------------


def _node_package_json(pkg_name: str, version_spec: str) -> bytes:
    """Canonical ``package.json`` bytes for a single-dependency node MCP.

    One serializer used for BOTH the pre-install write and the post-readback
    re-canonicalize, so the proxy (which resolved "latest") and a satellite
    (which got the pinned source) produce byte-identical bytes for the same
    resolved version — keeping ``compute_version_hash`` stable across hosts.
    Returns bytes (LF newlines) — callers must ``write_bytes``, never
    ``write_text``: text-mode writes translate ``\\n`` to ``\\r\\n`` on
    Windows, which drifts the hash and puts the MCP in a permanent
    reinstall-on-every-session loop against a Linux platform.
    """
    return json.dumps(
        {"private": True, "dependencies": {pkg_name: version_spec}}, indent=2,
    ).encode("utf-8")


def _node_installed_version(mcp_dir: Path, pkg_name: str) -> str:
    """Concrete version installed under ``node_modules/<pkg_name>``.

    Scoped packages live at ``node_modules/@scope/name`` — the embedded slash
    in ``pkg_name`` resolves correctly. Returns ``""`` on any error (never
    raises); a successful ``npm install`` always writes this file, so an empty
    result is anomalous and the node branch treats it as an install failure.
    """
    try:
        pj = mcp_dir / "node_modules" / pkg_name / "package.json"
        return str(json.loads(pj.read_text()).get("version", "")) if pj.is_file() else ""
    except (OSError, ValueError):
        return ""


async def _python_installed_version(
    venv_dir: Path, dist: str, *, timeout: int = 15,
) -> str:
    """Concrete version of distribution ``dist`` installed in ``venv_dir``.

    Queries the venv's own interpreter (the proxy's ``importlib.metadata`` can't
    see venv site-packages) via the cross-platform interpreter path
    (``Scripts/python.exe`` on Windows satellites). ``dist`` is the PyPI package
    name; ``importlib.metadata`` normalizes dash/underscore/case. Returns ``""``
    on any error (never raises) — the caller logs + skips pinning.
    """
    py = venv_dir / _VENV_BIN_DIR / f"python{_EXE_SUFFIX}"
    if not py.is_file():
        return ""
    code = (
        "import importlib.metadata as m, sys\n"
        f"sys.stdout.write(m.version({dist!r}))\n"
    )
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                str(py), "-c", code,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=timeout,
        )
        out, _ = await proc.communicate()
        return out.decode(errors="replace").strip() if proc.returncode == 0 else ""
    except (OSError, asyncio.TimeoutError):
        return ""


async def install_mcp(
    mcp_dir: Path,
    runtime: str,
    source: str,
    *,
    progress_cb: ProgressCb = None,
    uv_bin: str | None = None,
    python_bin: str = "python3",
    timeout: int = DEFAULT_INSTALL_TIMEOUT,
) -> InstallResult:
    """Install or update an MCP's Node/Python dependencies in-place.

    - Node: ensures `package.json` exists, runs `npm install --omit=dev`,
      applies `patch-package` if `patches/*.patch` present.
    - Python: creates `venv/` and installs via `uv pip install` (preferred)
      or `venv/bin/pip` (fallback). When uv needs to fetch a Python version
      that differs from system Python, it installs it under the platform's
      `mcps/.uv-python/` (via ``UV_PYTHON_INSTALL_DIR``) so the interpreter
      lives inside the already-sandbox-mounted `mcps/` tree — keeping every
      MCP self-contained and the sandbox free of any user-home leakage.
    - Docker: returns success with a note (build/run is a separate flow).

    `progress_cb` receives `{phase, pct, message}` dicts (both sync and
    async callables supported) so the satellite can stream them over WS.
    """
    mcp_dir = Path(mcp_dir)
    name = mcp_dir.name

    # Pythons that uv fetches land here — under mcps/, which the session
    # sandbox already RO-binds. Derived from mcp_dir (mcps/{custom,community}/<slug>/).
    uv_python_dir = mcp_dir.parent.parent / ".uv-python"
    # UV_LINK_MODE=copy forces uv to copy files into the venv instead of
    # hardlinking/symlinking the python.exe. On Windows, creating those
    # links requires either Developer Mode enabled OR admin rights —
    # without them, `uv venv` fails with "Failed to create Python
    # executable link ... .tmpXXX". Copy mode is slightly slower but
    # works on every OS without extra system privileges.
    uv_env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(uv_python_dir),
        "UV_LINK_MODE": "copy",
    }

    if runtime == "node" and source.startswith("npm:"):
        parsed = parse_source(source)
        if not parsed:
            return InstallResult(ok=False, log=f"Unparseable npm source: {source!r}")
        pkg_name = parsed.package
        # Unpinned source ("npm:pkg") → install the latest published version.
        pkg_ver = parsed.version or "latest"

        # The package.json `dependencies` spec is derived FROM the source, so the
        # source is authoritative: a pinned source installs exactly that version,
        # an unpinned one installs "latest". Always (re)write it — overwrites any
        # stale committed pin and guarantees the requested version regardless of
        # what was on disk.
        pj = mcp_dir / "package.json"
        pj.write_bytes(_node_package_json(pkg_name, pkg_ver))

        # Unpinned: drop a pre-existing lockfile so npm resolves the true latest
        # (a stale lock would otherwise pin an old version). Pinned: keep the lock
        # for reproducibility (the satellite syncs the proxy's resolved lock).
        if pkg_ver == "latest":
            (mcp_dir / "package-lock.json").unlink(missing_ok=True)

        await _emit(progress_cb, {"mcp": name, "phase": "npm", "pct": 10, "message": "npm install"})

        # ``--ignore-scripts``: a package's install lifecycle scripts
        # (preinstall/install/postinstall) are arbitrary code that would run in
        # the proxy's context the moment a community MCP is installed — the
        # classic npm supply-chain RCE. We refuse to run them. (An MCP that
        # genuinely needs a native build step should ship prebuilt artifacts or
        # run as a Docker MCP, which is isolated.)
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *_shell_argv(["npm", "install", "--omit=dev", "--ignore-scripts"]),
                cwd=str(mcp_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            ),
            timeout=timeout,
        )
        stdout, _ = await proc.communicate()
        log = stdout.decode(errors="replace")
        if proc.returncode != 0:
            await _emit(progress_cb, {"mcp": name, "phase": "failed", "pct": 100, "message": "npm install failed", "error": log})
            return InstallResult(ok=False, log=log)

        # Apply patches if present
        patches_dir = mcp_dir / "patches"
        if patches_dir.is_dir() and any(patches_dir.glob("*.patch")):
            await _emit(progress_cb, {"mcp": name, "phase": "npm", "pct": 80, "message": "patch-package"})
            npx = await asyncio.create_subprocess_exec(
                *_shell_argv(["npx", "patch-package"]),
                cwd=str(mcp_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            patch_out, _ = await npx.communicate()
            log += "\n" + patch_out.decode(errors="replace")

        # Read back the concrete installed version. A successful npm install
        # always writes node_modules/<pkg>/package.json, so an empty readback is
        # anomalous — fail rather than leave a non-deterministic "latest" pin.
        resolved = _node_installed_version(mcp_dir, pkg_name)
        if not resolved:
            log += f"\nCould not read installed version of {pkg_name} from node_modules."
            await _emit(progress_cb, {"mcp": name, "phase": "failed", "pct": 100, "message": "version readback failed", "error": log})
            return InstallResult(ok=False, log=log)

        # Re-canonicalize package.json to the concrete version. Both the proxy
        # (resolved "latest") and the satellite (pinned) land on the identical
        # bytes here, so the version_hash matches regardless of npm differences.
        pj.write_bytes(_node_package_json(pkg_name, resolved))

        vh = compute_version_hash(mcp_dir)
        await _emit(progress_cb, {"mcp": name, "phase": "done", "pct": 100, "message": "installed"})
        return InstallResult(ok=True, log=log, version_hash=vh, resolved_version=resolved)

    if runtime == "python" and (source.startswith("pypi:") or source.startswith("git+")):
        if source.startswith("git+"):
            # pip VCS source — pip/uv accept "git+https://…@<ref>#subdirectory=<dir>"
            # verbatim. NEVER rewrite the "@<ref>" to "==" (it is the git ref, not
            # a version pin). The pinned ref lives in the manifest, so bumping it
            # changes compute_version_hash → satellites resync + reinstall.
            pip_pkg = source
            upgrade = False
        else:
            pip_pkg = source[5:].replace("@", "==", 1)  # "ha-mcp@6.6.1" -> "ha-mcp==6.6.1"
            # Unpinned "pypi:pkg" → bare requirement (no "=="). Without --upgrade,
            # a re-install over a PRESERVED venv is a no-op against the satisfied
            # requirement and would keep + re-pin the STALE version. A pinned
            # "pkg==X.Y.Z" needs no flag (exact forces it) and must NOT float
            # transitive deps on the satellite.
            upgrade = "==" not in pip_pkg
        venv_dir = mcp_dir / "venv"

        async def _create_venv(python_spec: str | None) -> tuple[bool, str]:
            """Create venv. ``python_spec`` like ``>=3.13`` or ``None`` for default."""
            if uv_bin and os.path.isfile(uv_bin):
                msg = f"uv venv{' --python ' + python_spec if python_spec else ''}"
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 10, "message": msg})
                cmd = [uv_bin, "venv"]
                if python_spec:
                    cmd.extend(["--python", python_spec])
                cmd.append(str(venv_dir))
                env = uv_env
            else:
                if python_spec:
                    return False, f"python {python_spec} required but uv not available"
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 10, "message": "python -m venv"})
                cmd = [python_bin, "-m", "venv", str(venv_dir)]
                env = None
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd, cwd=str(mcp_dir),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=env,
                ),
                timeout=timeout,
            )
            out, _ = await proc.communicate()
            return proc.returncode == 0, out.decode(errors="replace")

        async def _pip_install() -> tuple[bool, str]:
            # Prefer uv (much faster). UV_PYTHON_INSTALL_DIR ensures any Python
            # uv fetches lands in the platform tree (under mcps/.uv-python/),
            # which the session sandbox already mounts — keeping every MCP
            # self-contained and the sandbox free of user-home leakage.
            upgrade_flag = ["--upgrade"] if upgrade else []
            if uv_bin and os.path.isfile(uv_bin):
                cmd = [uv_bin, "pip", "install",
                       "--python", str(venv_dir / _VENV_BIN_DIR / f"python{_EXE_SUFFIX}"),
                       *upgrade_flag, pip_pkg]
                env = uv_env
            else:
                cmd = [str(venv_dir / _VENV_BIN_DIR / f"pip{_EXE_SUFFIX}"),
                       "install", *upgrade_flag, pip_pkg]
                env = None
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd, cwd=str(mcp_dir),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=env,
                ),
                timeout=timeout,
            )
            out, _ = await proc.communicate()
            return proc.returncode == 0, out.decode(errors="replace")

        if not venv_dir.is_dir():
            ok, venv_log = await _create_venv(None)
            if not ok:
                log = f"venv creation failed:\n{venv_log}"
                await _emit(progress_cb, {"mcp": name, "phase": "failed", "pct": 100, "message": "venv failed", "error": log})
                return InstallResult(ok=False, log=log)

        await _emit(progress_cb, {"mcp": name, "phase": "pip", "pct": 50, "message": f"pip install {pip_pkg}"})
        ok, log = await _pip_install()

        # If the package requires a Python version this venv doesn't satisfy,
        # uv reports it as `Python>=3.13,<3.14`. Recreate the venv with that
        # constraint and retry once. Single retry — if it still fails, surface
        # the original error.
        if not ok and uv_bin:
            m = _PY_FLOOR_RE.search(log)
            if m:
                python_spec = m.group(1).replace(" ", "")
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 30, "message": f"retry with Python{python_spec}"})
                shutil.rmtree(venv_dir, ignore_errors=True)
                ok, venv_log = await _create_venv(python_spec)
                if ok:
                    ok, log = await _pip_install()
                else:
                    log = f"venv creation with Python{python_spec} failed:\n{venv_log}"
        # Read back the concrete version for pypi installs so the caller can pin
        # the LOCAL manifest. git+ has no PyPI dist name to query — skip it (its
        # version of record is the pinned git ref in the source). A readback miss
        # is non-fatal: pip install succeeded; the caller logs + leaves the
        # manifest unpinned (it self-heals on the next update).
        resolved = ""
        if ok and source.startswith("pypi:"):
            parsed = parse_source(source)
            if parsed:
                resolved = await _python_installed_version(venv_dir, parsed.package)
                if not resolved:
                    log += f"\nInstalled {parsed.package} but could not read its version back; leaving manifest unpinned."

        phase = "done" if ok else "failed"
        await _emit(progress_cb, {"mcp": name, "phase": phase, "pct": 100, "message": log[-200:] if not ok else "installed"})
        vh = compute_version_hash(mcp_dir) if ok else ""
        return InstallResult(ok=ok, log=log, version_hash=vh, resolved_version=resolved)

    if runtime == "docker":
        return InstallResult(ok=True, log="Docker MCP — use Start button to build and run.")

    # Source-bundled Python MCP (custom/core MCPs like schedules-mcp, memory-mcp,
    # mcps-mcp etc.). The runtime is "python" but the source is the bundled
    # server.py in the tarball, not a pypi package. Manifest convention is
    # `command: venv/bin/python` + `args: [server.py]`, so we create the
    # venv and pip install -r requirements.txt.
    if runtime == "python":
        req_file = mcp_dir / "requirements.txt"
        if not req_file.is_file():
            # No deps to install — bare server.py with stdlib-only imports.
            return InstallResult(
                ok=True, log="Python MCP with no requirements.txt",
                version_hash=compute_version_hash(mcp_dir),
            )
        venv_dir = mcp_dir / "venv"
        if not venv_dir.is_dir():
            # Prefer uv venv when available — the satellite-bundled `python -m
            # venv` triggers an internal `ensurepip --default-pip` step that
            # fails on Windows when Defender quarantines the freshly-copied
            # venv python.exe (returncode=1 with no actionable message). uv
            # builds a clean venv WITHOUT ensurepip — uv manages pip ops
            # natively, so the broken step is sidestepped. Falls back to
            # `python -m venv` only if uv isn't bundled (shouldn't happen on
            # any supported satellite, but defensive). The uv_env carries
            # UV_LINK_MODE=copy so the python.exe link step also works
            # without Windows Developer Mode / admin.
            if uv_bin and os.path.isfile(uv_bin):
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 10,
                                           "message": "uv venv"})
                cmd = [uv_bin, "venv", str(venv_dir)]
                env = uv_env
            else:
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 10,
                                           "message": "python -m venv"})
                cmd = [python_bin, "-m", "venv", str(venv_dir)]
                env = None
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd, cwd=str(mcp_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                ),
                timeout=timeout,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                log = "venv creation failed:\n" + out.decode(errors="replace")
                await _emit(progress_cb, {"mcp": name, "phase": "failed",
                                           "pct": 100, "message": "venv failed",
                                           "error": log})
                return InstallResult(ok=False, log=log)
        async def _req_install() -> tuple[bool, str]:
            # Prefer uv when available (much faster).
            if uv_bin and os.path.isfile(uv_bin):
                cmd = [uv_bin, "pip", "install",
                       "--python", str(venv_dir / _VENV_BIN_DIR / f"python{_EXE_SUFFIX}"),
                       "-r", str(req_file)]
                env = uv_env
            else:
                cmd = [str(venv_dir / _VENV_BIN_DIR / f"pip{_EXE_SUFFIX}"),
                       "install", "-r", str(req_file)]
                env = None
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd, cwd=str(mcp_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                ),
                timeout=timeout,
            )
            out, _ = await proc.communicate()
            return proc.returncode == 0, out.decode(errors="replace")

        await _emit(progress_cb, {"mcp": name, "phase": "pip", "pct": 50,
                                   "message": "pip install -r requirements.txt"})
        ok, log = await _req_install()
        # Python-floor retry — the pypi branch above has had this since the
        # start; bundled MCPs (server.py + requirements.txt, i.e. every
        # custom/core MCP) silently lacked it, so a satellite whose default
        # python is too old failed them forever (live-hit 2026-07-19:
        # music-gen-mcp needs >=3.11, satellite venv is 3.10 — uv can fetch a
        # matching standalone CPython, it just was never asked). Recreate the
        # venv with the reported constraint and retry once.
        if not ok and uv_bin and os.path.isfile(uv_bin):
            m = _PY_FLOOR_RE.search(log)
            if m:
                python_spec = m.group(1).replace(" ", "")
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 30,
                                           "message": f"retry with Python{python_spec}"})
                shutil.rmtree(venv_dir, ignore_errors=True)
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        uv_bin, "venv", "--python", python_spec, str(venv_dir),
                        cwd=str(mcp_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=uv_env,
                    ),
                    timeout=timeout,
                )
                out, _ = await proc.communicate()
                if proc.returncode == 0:
                    ok, log = await _req_install()
                else:
                    log = (f"venv creation with Python{python_spec} failed:\n"
                           + out.decode(errors="replace"))
        if not ok:
            await _emit(progress_cb, {"mcp": name, "phase": "failed",
                                       "pct": 100, "message": "pip install failed",
                                       "error": log})
            return InstallResult(ok=False, log=log)
        vh = compute_version_hash(mcp_dir)
        await _emit(progress_cb, {"mcp": name, "phase": "done", "pct": 100,
                                   "message": "installed"})
        return InstallResult(ok=True, log=log, version_hash=vh)

    return InstallResult(ok=True, log="No install step required.")


# ---------------------------------------------------------------------------
# Version hash
# ---------------------------------------------------------------------------


_HASH_INPUT_FILES = (
    "manifest.json",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Dockerfile",
    "docker-compose.yml",
)

# Source-code extensions whose contents drift the version hash. Catches the
# "I fixed a bug in server.py but didn't bump manifest.version" footgun —
# without this the satellite would keep running the stale code indefinitely
# because the manifest+lockfile-only hash stays identical.
_HASH_SOURCE_EXTS = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs", ".java", ".kt", ".rb",
})

# Directory names skipped at any depth during the source-walk. Build
# artifacts, virtual envs, vendored deps, VCS metadata, runtime screenshots,
# install backups — none of these should make the hash drift.
_HASH_EXCLUDE_DIRS = frozenset({
    "__pycache__", "node_modules", "venv", ".venv", ".uv-python",
    ".git", "screenshots", ".backups", "dist", "build", "out", "target",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

# Compiled / binary artifacts. Excluded even when they accidentally land in
# the tree (e.g. a stray .so from a local rebuild).
_HASH_EXCLUDE_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".class",
})


def _walk_source_files(mcp_dir: Path) -> list[Path]:
    """Return source files under ``mcp_dir`` sorted by relative path.

    Skips :data:`_HASH_EXCLUDE_DIRS` at any depth, :data:`_HASH_EXCLUDE_SUFFIXES`,
    and the manifest/lockfile/Dockerfile names already covered by
    :data:`_HASH_INPUT_FILES` so we don't double-hash them. Sorted output
    keeps :func:`compute_version_hash` deterministic across filesystems.
    """
    out: list[Path] = []
    for root, dirs, files in os.walk(mcp_dir):
        # In-place prune so os.walk doesn't descend into excluded subtrees.
        dirs[:] = [d for d in dirs if d not in _HASH_EXCLUDE_DIRS]
        for fname in files:
            if fname in _HASH_INPUT_FILES:
                continue
            p = Path(root) / fname
            if p.suffix.lower() in _HASH_EXCLUDE_SUFFIXES:
                continue
            if p.suffix.lower() in _HASH_SOURCE_EXTS:
                out.append(p)
    out.sort(key=lambda p: p.relative_to(mcp_dir).as_posix())
    return out


def compute_version_hash(mcp_dir: Path) -> str:
    """Stable sha256 digest over the install-relevant inputs of an MCP.

    Inputs:

    1. Well-known top-level files in :data:`_HASH_INPUT_FILES` (manifest,
       requirements, lockfiles, Dockerfile) — the original contract.
    2. Every file under ``patches/`` sorted by name.
    3. Every source file matching :data:`_HASH_SOURCE_EXTS` recursively,
       skipping :data:`_HASH_EXCLUDE_DIRS` and :data:`_HASH_EXCLUDE_SUFFIXES`.

    (3) is the late addition: edits to ``server.py`` (or any other source
    file) now drift the hash, so satellites pick up the change on next sync
    without needing a manifest-version bump as a workaround. Applied
    uniformly to ``mcps/custom/*`` and ``mcps/community/*`` — both flavors
    extract source onto local disk that the satellite vendors.

    The output is a short hex prefix (first 16 chars) suitable for tagging
    installed state on the satellite.
    """
    mcp_dir = Path(mcp_dir)
    h = hashlib.sha256()
    for fname in _HASH_INPUT_FILES:
        p = mcp_dir / fname
        if p.is_file():
            try:
                h.update(p.relative_to(mcp_dir).as_posix().encode())
                h.update(b"\x00")
                h.update(p.read_bytes())
                h.update(b"\x00")
            except OSError:
                continue
    patches = sorted((mcp_dir / "patches").glob("*.patch")) if (mcp_dir / "patches").is_dir() else []
    for p in patches:
        try:
            h.update(p.relative_to(mcp_dir).as_posix().encode())
            h.update(b"\x00")
            h.update(p.read_bytes())
            h.update(b"\x00")
        except OSError:
            continue
    for p in _walk_source_files(mcp_dir):
        try:
            h.update(p.relative_to(mcp_dir).as_posix().encode())
            h.update(b"\x00")
            h.update(p.read_bytes())
            h.update(b"\x00")
        except OSError:
            continue
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sanity helpers
# ---------------------------------------------------------------------------


def self_hash() -> str:
    """SHA256 of this file's source. Used by the satellite drift check so a
    divergent vendored copy fails loudly at import time instead of producing
    subtly different install behavior."""
    try:
        p = Path(__file__)
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""
