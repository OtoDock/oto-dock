"""System requirements of an MCP: the packages and the interpreter floor a
manifest declares, checked on this host and, when the caller opts in,
installed with its package manager. Runs on the platform AND is vendored into
the satellite beside ``mcp_installer``, which re-exports what its importers
read.

Stdlib only, Python 3.10: the satellite imports its own vendored copy via
``scripts/sync-satellite-code.sh``; the satellite's start hashes the file
against ``SHARED_MCP_SYSTEM_DEPS_HASH`` to catch drift.
"""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# A distro package name (dpkg, rpm, pacman and brew all fit).
_SYSTEM_PKG_RE = re.compile(r"[a-z0-9][a-z0-9+.@_-]*")


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

    Only called when a caller explicitly asks for it; no build path of the
    platform does. Requires sudo; returns (False, error_msg) if sudo is
    unavailable or install fails.
    """
    family, pkgs = _packages_for_host(req)
    if not family or not pkgs:
        return True, ""
    # Manifest content reaches a privileged package manager here: a name is a
    # name, never an option or a list.
    bad = [p for p in pkgs if not isinstance(p, str) or not _SYSTEM_PKG_RE.fullmatch(p)]
    if bad:
        return False, f"Invalid system package name(s): {bad}"
    missing = [p for p in pkgs if not _is_package_installed(family, p)]
    if not missing:
        return True, "All system requirements satisfied."

    if dry_run:
        return True, f"Would install {family}: {', '.join(missing)}"

    cmd: list[str]
    if family in ("debian", "ubuntu"):
        cmd = ["sudo", "apt", "install", "-y", "--", *missing]
    elif family == "rhel":
        cmd = ["sudo", "dnf", "install", "-y", "--", *missing]
    elif family == "arch":
        cmd = ["sudo", "pacman", "-S", "--noconfirm", "--", *missing]
    elif family == "macos_brew":
        cmd = ["brew", "install", *missing]
    else:
        return False, f"Unknown package manager for family={family}"

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode == 0, (r.stdout + r.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
