"""Shared MCP installer, vendored into the satellite: run where a tool is built and by ``mcp_venv_bootstrap``.

Single source of truth for:
- Parsing `server.source` fields — pinned (`npm:pkg@ver`, `pypi:pkg@ver`,
  `git+<url>@<ref>#subdirectory=<dir>`) or unpinned (`npm:pkg`, `pypi:pkg`,
  which install the latest published version).
- Installing Node/Python packages into a self-contained MCP directory
  (`node_modules/` or `venv/`), reading back the concrete installed version so
  the caller can pin it into the local manifest.
- Applying the patches a Node MCP ships (``patches/*.patch``) with git.
- Checking and optionally installing system-level dependencies (libmagic,
  libreoffice, etc.) declared in the manifest's `system_requirements`
  (``mcp_system_deps``, vendored beside it and re-exported here).
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
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .mcp_system_deps import (  # noqa: F401  # re-exported for the installer's importers
    MissingDep,
    SystemRequirementsInput,
    check_system_requirements,
    install_system_requirements,
)

logger = logging.getLogger("mcp-installer")

# Default timeout for one install subprocess (its whole run, not its spawn).
# Can be overridden per call.
DEFAULT_INSTALL_TIMEOUT = 300  # seconds

# Cross-platform venv layout. Inlined here (instead of imported from
# satellite.config) so this module stays self-contained — it's vendored
# byte-for-byte into the satellite, and the satellite-side hash check
# would catch any external imports. On the platform side these always
# resolve to "bin"/"" because the proxy is Linux-only.
_VENV_BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# uv resolver naming a Python floor the current venv can't satisfy, in every
# phrasing seen: uv 0.12 reports "dummy==1.0 requires Python >=3.14" (a
# space), older ones "Requires Python>=3.13" and "the current Python version
# (3.10.12) does not satisfy Python>=3.11". The captured spec, spaces removed,
# feeds `uv venv --python <spec>`, which downloads a matching standalone
# CPython when the host lacks one. uv ignores an upper bound, so only a floor
# ever fails a resolve.
_PY_FLOOR_RE = re.compile(
    r"Python\s*(>=\s*\d+(?:\.\d+)*(?:\s*,\s*<=?\s*\d+(?:\.\d+)*)?)"
)

# A distribution name as it appears in a resolver's message.
_DIST_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: ``mask_sign_ins(text)``: every address's sign-in masked (``scheme://user:password@host``, a token alone
#: too, reads ``scheme://***@host``). What a build said, before it leaves the machine or is shown.
mask_sign_ins = functools.partial(re.compile(r"(?<=://)[^/\s@]+(?=@)").sub, "***")


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _source_builds_named(log: str, allowed: list[str]) -> list[str]:
    """The ``allowed`` (``source_build``) packages a failed resolve's output
    names, in list order, names compared in their PEP 503 form. Read from the
    names alone, so neither pip's nor uv's wording is relied on."""
    named = {_pep503(t.strip("._-")) for t in _DIST_TOKEN_RE.findall(log)}
    return [p for p in allowed if _pep503(p) in named]


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


def _label(cmd: list[str]) -> str:
    """``npm install``, ``uv pip``, ``git apply``: the program and its first
    argument, for a timeout message (a leading ``cmd /c`` skipped)."""
    words = list(cmd[2:] if cmd[:2] == ["cmd", "/c"] else cmd)
    return " ".join([Path(words[0]).name, *words[1:2]]) if words else "subprocess"


async def _reap_group(pgid: int) -> None:
    """Reap what is left of a killed process group among this process's own
    children: the killed child's children become ours once it dies when this
    process is their reaper of last resort (PID 1 of a container with no init).
    Anywhere else the first wait answers that no such child exists."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while True:
        try:
            pid, _ = os.waitpid(-pgid, os.WNOHANG)  # noqa: ASYNC222  # WNOHANG: the wait never blocks
        except ChildProcessError:
            return
        if pid == 0:
            if loop.time() >= deadline:
                return
            await asyncio.sleep(0.05)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and every process it started, then reap it.

    POSIX: the child leads its own process group (``start_new_session``), so
    the group is killed whole. Windows: ``taskkill /T`` walks the tree while
    the child is still there to walk from (killing ``cmd /c npm`` alone leaves
    npm running), then the child itself is killed. The second ``communicate``
    drains what the cancelled one left, so the pipes reach EOF; it is bounded
    because a process that left the group can still hold them."""
    windows = sys.platform == "win32"
    if windows:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            await asyncio.to_thread(
                subprocess.run, ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=30,
            )
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.communicate(), 10)
    # Only once asyncio's own watcher has reaped the child: the group wait
    # must never take the child from it.
    if not windows and proc.returncode is not None:
        await _reap_group(proc.pid)
        await _drain_group(proc.pid)


#: How long a killed step's group may take to empty. In a build's sandbox the
#: pid namespace's first process is a member, and it is reaped only once every
#: process of the namespace is gone, so an empty group means the build is over.
_GROUP_DRAIN_S = 10.0


async def _drain_group(pgid: int) -> None:
    """Wait, bounded, until no process of the group is left, zombies included
    (a zombie still answers ``killpg``), reaping the ones that became this
    process's own."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _GROUP_DRAIN_S
    while True:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        if loop.time() >= deadline:
            logger.warning("process group %s still has members after %.0f s", pgid, _GROUP_DRAIN_S)
            return
        with contextlib.suppress(ChildProcessError):
            os.waitpid(-pgid, os.WNOHANG)  # noqa: ASYNC222  # WNOHANG: the wait never blocks
        await asyncio.sleep(0.05)


#: What a step's output keeps: its first and its last bytes, and every line the
#: installer's readers look for in what was cut (a Python floor).
_OUT_HEAD = 64 * 1024
_OUT_TAIL = 192 * 1024


async def _read_bounded(stream: asyncio.StreamReader) -> str:
    """The stream to its end, its head and tail kept and the cut named."""
    head, tail, seen = bytearray(), bytearray(), 0
    kept: list[str] = []
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        seen += len(chunk)
        room = _OUT_HEAD - len(head)
        head += chunk[:room]
        tail += chunk[room:] if room > 0 else chunk
        if len(tail) > _OUT_TAIL:
            cut = bytes(tail[:-_OUT_TAIL])
            kept.extend(m.group(0) for m in _PY_FLOOR_RE.finditer(cut.decode(errors="replace")))
            del tail[:-_OUT_TAIL]
    text = head.decode(errors="replace")
    if seen > len(head) + len(tail):
        text += (f"\n[... {seen - len(head) - len(tail)} bytes cut ...]\n"
                 + "".join(f"{k}\n" for k in kept[:5]))
    return text + tail.decode(errors="replace")


async def _run_bounded(
    cmd: list[str], *, cwd: str | None, env: dict[str, str], timeout: float,
) -> tuple[int, str]:
    """Run an install subprocess to its end within ``timeout`` seconds and
    return its exit code and its combined output.

    The bound covers the whole run, not only the spawn. When it passes, or the
    caller is cancelled, the child and every process it started are killed
    and reaped (``_kill_tree``); the timeout is raised as
    ``asyncio.TimeoutError`` naming the command, for the caller's rollback."""
    kwargs: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )
    async def _whole() -> str:
        text = await _read_bounded(cast("asyncio.StreamReader", proc.stdout))
        await proc.wait()
        return text

    try:
        out = await asyncio.wait_for(_whole(), timeout)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        raise asyncio.TimeoutError(
            f"{_label(cmd)} did not finish within {timeout} s") from None
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    # _whole waits for the exit, so the code is set.
    return cast("int", proc.returncode), out


#: A step's kind, named to the ``run`` that starts it: ``venv`` makes a venv and
#: may fetch an interpreter (no package code runs), ``pip`` installs into a venv
#: (a package's build may run), ``npm`` installs ``node_modules`` and writes the
#: lock, ``patch`` applies a shipped patch, ``rebuild`` rebuilds Node addons.
STEPS = frozenset({"venv", "pip", "npm", "patch", "rebuild"})

#: ``run(cmd, cwd=, env=, timeout=, step=, mcp_dir=)``: starts one step and
#: answers its exit code and output (``_run_bounded``'s contract). The platform
#: and a compute machine pass one that runs the step in a sandbox.
Runner = Callable[..., Awaitable[tuple[int, str]]]


async def run_unconfined(cmd: list[str], *, cwd: str | None, env: dict[str, str], timeout: float,
                         step: str, mcp_dir: Path) -> tuple[int, str]:
    """The ``run`` of a step as this process's own child: a paired computer,
    which has no sandbox, and the image build's stage."""
    return await _run_bounded(cmd, cwd=cwd, env=env, timeout=timeout)


# ---------------------------------------------------------------------------
# The environment every install subprocess gets
# ---------------------------------------------------------------------------

# An allowlist, not a denylist: the process that runs the installer holds the
# platform's secrets (the proxy) or a machine's (a satellite), and a package
# manager or a build backend must see none of them. Names are compared
# case-insensitively (Windows). Package-manager names are listed one by one:
# a prefix would admit index credentials and build-steering knobs unreviewed.
_INSTALL_ENV_NAMES = frozenset(name.upper() for name in (
    # runtime
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL",
    "LC_CTYPE", "TERM", "TZ", "TMPDIR", "TEMP", "TMP", "PWD",
    "SystemRoot", "windir", "SystemDrive", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
    "ProgramW6432", "ComSpec", "PATHEXT", "HOMEDRIVE", "HOMEPATH", "USERNAME",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "PYTHONIOENCODING", "PYTHONUTF8",
    # network and trust
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO",
    # package managers, by exact name; the uv mirror names say where an
    # interpreter or a package comes from, never what runs
    "UV_CACHE_DIR", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX",
    "UV_INDEX", "UV_OFFLINE", "UV_NATIVE_TLS", "UV_HTTP_TIMEOUT",
    "UV_PYTHON_INSTALL_MIRROR", "UV_PYPY_INSTALL_MIRROR", "UV_PYTHON_DOWNLOADS",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CACHE_DIR",
    "NPM_CONFIG_REGISTRY", "NPM_CONFIG_CACHE", "NPM_CONFIG_CAFILE",
    "NPM_CONFIG_STRICT_SSL", "XDG_CACHE_HOME",
))


def _install_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a package manager, a build backend or git run by
    the installer: the allowlisted names of the parent, no configuration
    file of any of them, and ``extra`` on top."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _INSTALL_ENV_NAMES}
    env.update({
        "UV_NO_CONFIG": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    if extra:
        env.update(extra)
    return env


async def _apply_patches(
    mcp_dir: Path, patches: list[Path], env: dict[str, str], timeout: int, *, run: Runner,
) -> tuple[bool, str]:
    """Apply ``patches`` (git-format diffs rooted at ``mcp_dir``) with the
    platform's own git; never an executable the package or a registry
    provides. Git may not discover a repository above the MCP folder (a
    bare-metal install keeps ``mcps/`` under the platform checkout, where an
    in-tree run skips every path outside its prefix with exit 0), reads no
    configuration (see ``_install_env``), and a patch already present in
    ``node_modules`` (a re-install) is skipped rather than failed."""
    git = shutil.which("git")
    if not git:
        return False, "this MCP ships patches, which need git on this machine"
    git_env = {**env, "GIT_CEILING_DIRECTORIES": str(mcp_dir.parent)}
    log: list[str] = []

    async def _git(*args: str) -> tuple[int, str]:
        return await run(
            [git, "apply", *args], cwd=str(mcp_dir), env=git_env, timeout=timeout,
            step="patch", mcp_dir=mcp_dir,
        )

    for patch in patches:
        rel = f"patches/{patch.name}"
        rc, _out = await _git("-R", "--check", rel)
        if rc == 0:
            log.append(f"{patch.name}: already applied")
            continue
        rc, out = await _git("--verbose", "--whitespace=nowarn", rel)
        log.append(out.strip())
        if rc != 0 or "Skipped patch" in out:
            log.append(f"{patch.name}: not applied")
            return False, "\n".join(log)
    return True, "\n".join(log)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ParsedSource:
    """Decomposed `server.source` field."""
    registry: str   # "npm" | "pypi" | "docker" | ""
    package: str
    version: str
    # The extras of a ``pypi:`` source (``pypi:name[a,b]``), as written.
    extras: tuple[str, ...] = ()

    @property
    def requirement(self) -> str:
        """The package with its extras, as pip and uv name it: ``name[a,b]``."""
        return f"{self.package}[{','.join(self.extras)}]" if self.extras else self.package

    def pinned(self, version: str) -> str:
        """This source pinned to ``version``, the extras kept:
        ``pypi:name[a]@1.2.3``."""
        return f"{self.registry}:{self.requirement}@{version}"


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
    # stay deterministic. See mcp_pin.settle_built.
    resolved_version: str = ""
    missing_deps: list[MissingDep] = field(default_factory=list)
    # The ``--python`` spec of the venv this install created: the caller's
    # ``python_spec`` or a floor uv asked for; "" for uv's default (a pin
    # that failed, none asked) or when no venv was created.
    venv_python: str = ""


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


# One extra of a PyPI requirement: PEP 508's identifier (letters and digits at
# both ends, ``-``, ``_`` and ``.`` between), bounded in length and number.
_EXTRA_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_EXTRA_MAX_LEN = 64
_EXTRAS_MAX = 16


def _split_extras(spec: str) -> tuple[str, tuple[str, ...]] | None:
    """``name[a,b]`` → ``("name", ("a", "b"))``; ``name`` → ``("name", ())``.
    One bracket group right after the name and closing the spec, extras
    comma-separated and nothing else inside; ``None`` for any other shape
    (an empty group, a second group, a space, an empty or flag-like extra)."""
    if "[" not in spec and "]" not in spec:
        return spec, ()
    start = spec.find("[")
    if start < 1 or spec.count("[") != 1 or spec.count("]") != 1 or not spec.endswith("]"):
        return None
    extras = tuple(spec[start + 1:-1].split(","))
    if len(extras) > _EXTRAS_MAX:
        return None
    if any(len(e) > _EXTRA_MAX_LEN or not _EXTRA_RE.fullmatch(e) for e in extras):
        return None
    return spec[:start], extras


def normalized_extras(parsed: ParsedSource | None) -> frozenset[str]:
    """The extras of a parsed source as PEP 685 compares them: lower case,
    runs of ``-``, ``_`` and ``.`` as one ``-``, order ignored."""
    if parsed is None:
        return frozenset()
    return frozenset(_pep503(e) for e in parsed.extras)


def parse_source(source: str) -> ParsedSource | None:
    """Parse `server.source` into (registry, package, version).

    Community node/python MCPs are **unpinned** in the catalog — the source is a
    bare package pointer (`npm:pkg`, `pypi:pkg`) and the upstream registry is the
    version of record. A pinned form (`npm:pkg@1.2.3`, `pypi:pkg@1.2.3`) is also
    accepted: the proxy writes one into each install's LOCAL manifest after
    resolving the concrete version, so satellites reinstall deterministically.

    Handles npm scoped packages (`npm:@scope/name`, `npm:@scope/name@ver`) and
    PyPI extras (`pypi:name[a,b]`, `pypi:name[a,b]@ver`: ``package`` is the
    bare name, ``extras`` the group). Returns ``version=""`` for an unpinned
    source. Returns ``None`` for empty or empty-package sources and for a
    spec that fails the charset or the extras rule.
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
        spec = pkg_ver if at_idx == -1 else pkg_ver[:at_idx]
        version = "" if at_idx == -1 else pkg_ver[at_idx + 1:]
        split = _split_extras(spec)
        if split is None:
            logger.warning("Rejecting malformed pypi extras: %r", source)
            return None
        package, extras = split
        if not package:
            return None
        if not _spec_is_safe(package) or (version and not _spec_is_safe(version)):
            logger.warning("Rejecting unsafe pypi source spec: %r", source)
            return None
        return ParsedSource(registry="pypi", package=package, version=version, extras=extras)
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
# Installing an npm or PyPI package
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
        raw = read_built(pj, _PACKAGE_JSON_LIMIT) if _stays_in(pj, mcp_dir) else None
        return plain_version(json.loads(raw).get("version", "")) if raw is not None else ""
    except (ValueError, AttributeError, RecursionError):
        return ""


#: A top package's ``package.json`` is read to here (the largest seen is a
#: few KB; a long description must not fail an install).
_PACKAGE_JSON_LIMIT = 1 << 20


#: The most of a file a build wrote that the platform reads: a venv's
#: config, a package's metadata, an entry point's text.
BUILT_READ_LIMIT = 1 << 16

_PLAIN_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+!_-]{0,63}")


def read_built(path: Path, limit: int = BUILT_READ_LIMIT) -> bytes | None:
    """At most ``limit`` bytes of a regular file a build may have written, or
    None for anything else (a link, a pipe, a device, a folder, nothing). It
    never follows a link at the file itself, never waits on a pipe and never
    reads past ``limit``: every reader of a file in a build's outputs goes
    through it, so nothing a build leaves holds the platform."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and os.path.islink(path):  # a host with no O_NOFOLLOW
        return None
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        left = limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def plain_version(value: object) -> str:
    """``value`` when it reads as a package version (digits first, then
    letters, digits and ``.+!_-``, 64 characters at most), else "": a version
    a build wrote is pinned into the manifest only when it is one."""
    text = str(value or "").strip()
    return text if _PLAIN_VERSION_RE.fullmatch(text) else ""


def _metadata_version(raw: bytes) -> str:
    """The ``Version`` header of a distribution's METADATA."""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            break
        key, _, val = line.partition(":")
        if key.strip().lower() == "version":
            return val.strip()
    return ""


def venv_python_minor(venv_dir: Path) -> tuple[int, int] | None:
    """``(major, minor)`` of the interpreter a venv's ``pyvenv.cfg`` records,
    or None when the file is missing, garbled or not a regular file."""
    raw = read_built(Path(venv_dir) / "pyvenv.cfg")
    for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
        key, _, val = line.partition("=")
        if key.strip().lower() in ("version", "version_info"):
            parts = val.strip().split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    return None


def _stays_in(path: Path, folder: Path) -> bool:
    """Whether ``path``, its links followed, lies in ``folder``: what a build
    wrote is read only when no link of it leads out of the tool's folder."""
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except (OSError, RuntimeError):
        return False


def _python_installed_version(venv_dir: Path, dist: str) -> str:
    """Concrete version of distribution ``dist`` installed in ``venv_dir``.

    Read from the venv's ``*.dist-info`` metadata in THIS process: the venv's
    own interpreter is never started by the installer (its site hooks would
    be third-party code running outside any sandbox). Both venv layouts are
    searched (``lib/pythonX.Y/site-packages``, Windows ``Lib/site-packages``);
    ``importlib.metadata`` normalizes dash, underscore and case and finds
    the folders; each METADATA is read through ``read_built``. Returns ``""``
    on any miss or a version that is not plain (never raises); the caller
    logs + skips pinning.
    """
    import importlib.metadata as metadata
    paths = [
        str(p) for pattern in ("lib/python*/site-packages", "Lib/site-packages")
        for p in venv_dir.glob(pattern) if p.is_dir() and _stays_in(p, venv_dir)
    ]
    if not paths:
        return ""
    try:
        for found in metadata.Distribution.discover(name=dist, path=paths):
            meta = getattr(found, "_path", None)
            if meta is None or not _stays_in(Path(str(meta)), venv_dir):
                return ""
            raw = read_built(Path(str(meta)) / "METADATA")
            if raw is not None:
                return plain_version(_metadata_version(raw))
    except (OSError, ValueError):
        return ""
    return ""


# ---------------------------------------------------------------------------
# Venv interpreters — where they live, how a venv is created, the prune
# ---------------------------------------------------------------------------

# A uv-managed download that failed is not tried again for this long: offline,
# every MCP would otherwise wait out its own failed download.
_MANAGED_RETRY_AFTER_S = 3600.0
_MANAGED_TIMEOUT_S = 120
_managed_failed_at: float | None = None


def _pyvenv_home(venv_dir: Path) -> str:
    """The ``home`` a venv's ``pyvenv.cfg`` names ("" when it names none).
    Raises ``OSError`` when the file is missing or not a regular file."""
    cfg = venv_dir / "pyvenv.cfg"
    raw = read_built(cfg)
    if raw is None:
        raise OSError(f"{cfg} is not a regular file")
    for line in raw.decode("utf-8", errors="replace").splitlines():
        key, _, val = line.partition("=")
        if key.strip().lower() == "home":
            return val.strip()
    return ""


def venv_interpreter_paths(venv_dir: Path) -> list[Path]:
    """Where a venv's interpreter really lives: its ``bin/python`` resolved
    and the ``home`` of its ``pyvenv.cfg`` resolved (on Windows the first is
    a copy inside the venv, the second the real one). Empty when neither can
    be read."""
    venv_dir = Path(venv_dir)
    out: list[Path] = []
    python = venv_dir / _VENV_BIN_DIR / f"python{_EXE_SUFFIX}"
    # A build wrote the config: a ``home`` it names may be anything, a NUL
    # included (``ValueError``), and reads as no path.
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        if os.path.lexists(python):
            out.append(python.resolve())
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        home = _pyvenv_home(venv_dir)
        if home:
            out.append(Path(home).resolve())
    return out


def venv_outside(venv_dir: Path, roots: list[Path]) -> bool:
    """Whether an interpreter path of the venv lies under none of ``roots``
    (resolved paths both sides), or does not exist (a recreate lost it). A
    venv with no readable interpreter path is not judged (``False``)."""
    paths = venv_interpreter_paths(venv_dir)
    return any(not p.exists() or not any(p == r or p.is_relative_to(r) for r in roots)
               for p in paths)


async def _create_venv(
    venv_dir: Path,
    *,
    uv_bin: str | None,
    python_bin: str,
    floor: str | None,
    pin: str | None,
    roots: list[Path] | None,
    env: dict[str, str],
    cwd: str,
    timeout: int,
    run: Runner,
) -> tuple[bool, str, str]:
    """Create ``venv_dir``; returns ``(ok, log, spec)`` with ``spec`` the
    ``--python`` the venv was made with ("" for uv's default).

    ``floor`` is a Python floor uv asked for: it must hold. ``pin`` is the
    caller's preferred interpreter: a pin uv cannot satisfy falls back to
    uv's default, never failing the install. With ``roots``, a venv whose
    interpreter lies outside them (an interpreter the session sandbox does
    not bind) is made again with ``--managed-python``, which fetches one into
    ``UV_PYTHON_INSTALL_DIR``; when that fails too the first venv is made
    again and the log says so."""
    global _managed_failed_at
    uv = uv_bin if uv_bin and os.path.isfile(uv_bin) else None  # noqa: ASYNC240  # one stat of the uv binary before a minutes-long install
    if uv is None:
        if floor:
            return False, f"python {floor} required but uv not available", ""
        rc, out = await run(
            [python_bin, "-m", "venv", str(venv_dir)], cwd=cwd, env=env, timeout=timeout,
            step="venv", mcp_dir=venv_dir.parent,
        )
        return rc == 0, out, ""

    async def _uv_venv(spec: str, managed: bool, bound: int) -> tuple[int, str]:
        cmd = [uv, "venv"]
        if spec:
            cmd.extend(["--python", spec])
        if managed:
            cmd.append("--managed-python")
        cmd.append(str(venv_dir))
        return await run(cmd, cwd=cwd, env=env, timeout=bound, step="venv", mcp_dir=venv_dir.parent)

    spec = floor or pin or ""
    try:
        await asyncio.to_thread(seed_interpreter, venv_dir.parent, spec)
    except OSError as exc:
        logger.warning("venv %s: the bundled interpreter was not copied (%s); uv fetches one", venv_dir, exc)
    rc, log = await _uv_venv(spec, False, timeout)
    if rc != 0 and spec and not floor:
        await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
        log += f"\nuv venv --python {spec} failed; created on uv's default interpreter"
        spec = ""
        rc, out = await _uv_venv("", False, timeout)
        log += "\n" + out
    if rc != 0:
        return False, log, spec
    if roots and venv_outside(venv_dir, roots):
        now = asyncio.get_running_loop().time()
        if _managed_failed_at is not None and now - _managed_failed_at < _MANAGED_RETRY_AFTER_S:
            logger.warning("venv %s: its interpreter lies outside the platform tree "
                           "(a managed download failed recently)", venv_dir)
            return True, log, spec
        await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
        rc, out = await _uv_venv(spec, True, min(timeout, _MANAGED_TIMEOUT_S))
        log += "\n" + out
        if rc == 0:
            return True, log, spec
        _managed_failed_at = now
        await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
        rc, out = await _uv_venv(spec, False, timeout)
        log += "\n" + out
        logger.warning("venv %s: uv venv --managed-python failed; its interpreter "
                       "stays outside the platform tree", venv_dir)
        return rc == 0, log, spec
    return True, log, spec


def remove_tree(path: Path) -> None:
    """Remove ``path`` even when a build left folders with no permission
    bits in it: each folder gets its owner's bits back and the removal runs
    again. Follows no link. Raises the last ``OSError``; a missing path is
    not one."""
    try:
        shutil.rmtree(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    stack = [str(path)]
    while stack:
        folder = stack.pop()
        try:
            if os.path.islink(folder) or not os.path.isdir(folder):
                continue
            os.chmod(folder, stat.S_IRWXU)
            stack.extend(e.path for e in os.scandir(folder) if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
    shutil.rmtree(path)


def sweep_folders(root: Path) -> int:
    """Remove every folder in ``root`` (what a stopped build left: its
    scratch), links left alone. Answers how many went; one that will not go
    is logged and left for the next sweep."""
    if not Path(root).is_dir():
        return 0
    gone = 0
    for path in Path(root).iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            remove_tree(path)
            gone += 1
        except OSError as exc:
            logger.warning("could not remove %s: %s", path, exc)
    return gone


#: The category folder of the community MCPs under the MCPs root.
COMMUNITY = "community"


def is_community(mcp_dir: Path) -> bool:
    """True for an MCP of the community category."""
    return Path(mcp_dir).parent.name == COMMUNITY


def uv_python_dir_for(mcp_dir: Path) -> Path:
    """Where uv puts an interpreter it fetches for this MCP: a community MCP's
    in its category's own folder, which travels with the community MCPs (a
    volume of their own in a container), every other MCP's beside the
    categories. ``interpreter_dirs`` names both; nothing else spells them."""
    mcp_dir = Path(mcp_dir)
    if is_community(mcp_dir):
        return mcp_dir.parent / ".uv-python"
    return mcp_dir.parent.parent / ".uv-python"


def _managed_of(folder: Path, minor: str) -> list[Path]:
    """The uv-managed CPython folders of ``minor`` in ``folder``, newest
    last; links (uv's minor-version name) left out."""
    if not folder.is_dir():
        return []
    found = [p for p in folder.glob(f"cpython-{minor}.*") if p.is_dir() and not p.is_symlink()]

    def _patch(p: Path) -> int:
        part = p.name.split("-")[1].split(".")
        return int(part[2]) if len(part) > 2 and part[2].isdigit() else -1
    return sorted(found, key=_patch)


def seed_interpreter(mcp_dir: Path, minor: str) -> bool:
    """Copy (never link) the newest managed interpreter of ``minor`` (``3.13``)
    the bundled tools hold into a community tool's interpreter folder when
    that holds none, so the tool's venv step downloads nothing: a bare-metal
    install's community tools use the interpreter they always did, and an
    install whose only index is private still gets the pinned Python. The
    community folder travels on its own (a volume of its own), hence a copy.
    Answers whether one was copied. Blocking (a copy of about 100 MB)."""
    mcp_dir = Path(mcp_dir)
    if not is_community(mcp_dir) or not re.fullmatch(r"\d+\.\d+", minor or ""):
        return False
    own = uv_python_dir_for(mcp_dir)
    if _managed_of(own, minor):
        return False
    found = _managed_of(mcp_dir.parent.parent / ".uv-python", minor)
    if not found:
        return False
    src = found[-1]
    own.mkdir(exist_ok=True)
    staged = own / f".seed-{src.name}"
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(src, staged, symlinks=True)
    os.replace(staged, own / src.name)
    logger.info("seeded %s with %s from the bundled tools' interpreters", own, src.name)
    return True


def interpreter_dirs(mcps_root: Path) -> list[Path]:
    """Every folder uv-managed interpreters live in under ``mcps_root``."""
    root = Path(mcps_root)
    return [root / ".uv-python", root / COMMUNITY / ".uv-python"]


def _pyvenv_cfgs(mcps_root: Path, max_depth: int = 4) -> list[Path]:
    """Every ``pyvenv.cfg`` under ``mcps_root`` (install backups and swap
    folders included), not descending into a venv, ``node_modules`` or
    ``.uv-python``. Only a regular file is a venv's config: a pipe or a link
    a build left under that name is none. A walk error raises."""
    def _raise(exc: OSError) -> None:
        raise exc

    found: list[Path] = []
    base = len(Path(mcps_root).parts)
    for root, dirs, files in os.walk(mcps_root, onerror=_raise):
        if "pyvenv.cfg" in files and stat.S_ISREG(os.lstat(Path(root) / "pyvenv.cfg").st_mode):
            found.append(Path(root) / "pyvenv.cfg")
            dirs[:] = []
            continue
        depth = len(Path(root).parts) - base
        dirs[:] = [
            d for d in dirs
            if depth < max_depth and d not in (".uv-python", "node_modules", "__pycache__", ".git")
        ]
    return found


def prune_uv_pythons(mcps_root: Path) -> list[str]:
    """Remove the interpreters in each folder of ``interpreter_dirs`` that no MCP
    venv uses (about 50 MB each, left by an uninstall or a platform that
    moved to a newer Python). Returns the names removed.

    A venv uses the ``cpython-*`` entry its interpreter resolves into
    (``bin/python`` and the ``home`` of its ``pyvenv.cfg``); a minor-version
    link is kept with its target and unlinked, never walked, when not. Any
    unreadable ``pyvenv.cfg`` or walk error removes nothing. Run only while
    nothing installs (before the platform serves, before a satellite
    connects). POSIX only: on Windows a venv's ``home`` names a junction this
    prune does not follow, so it removes nothing there."""
    if sys.platform == "win32":
        return []
    removed: list[str] = []
    for uv_dir in interpreter_dirs(mcps_root):
        if uv_dir.is_dir():
            removed += _prune_one(Path(mcps_root), uv_dir)
    return removed


def _prune_one(mcps_root: Path, uv_dir: Path) -> list[str]:
    try:
        uv_real = uv_dir.resolve()
        used: set[str] = set()
        for cfg in _pyvenv_cfgs(mcps_root):
            _pyvenv_home(cfg.parent)
            for path in venv_interpreter_paths(cfg.parent):
                if path.is_relative_to(uv_real) and path != uv_real:
                    used.add(path.relative_to(uv_real).parts[0])
        removed: list[str] = []
        for entry in sorted(uv_dir.iterdir()):
            if not entry.name.startswith("cpython-"):
                continue
            if entry.is_symlink():
                target = entry.resolve()
                kept = (target.is_relative_to(uv_real) and target != uv_real
                        and target.relative_to(uv_real).parts[0] in used)
                if not kept and entry.name not in used:
                    entry.unlink()
                    removed.append(entry.name)
            elif entry.is_dir() and entry.name not in used:
                shutil.rmtree(entry)
                removed.append(entry.name)
        return removed
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("uv interpreter prune skipped: %s", exc)
        return []


async def install_mcp(
    mcp_dir: Path,
    runtime: str,
    source: str,
    *,
    progress_cb: ProgressCb = None,
    uv_bin: str | None = None,
    python_bin: str = "python3",
    timeout: int = DEFAULT_INSTALL_TIMEOUT,
    source_build: list[str] | None = None,
    python_spec: str | None = None,
    sandbox_roots: list[Path] | None = None,
    run: Runner,
) -> InstallResult:
    """Install or update an MCP's Node/Python dependencies in-place.

    - Node: ensures `package.json` exists, runs `npm install --omit=dev`,
      applies the shipped `patches/*.patch` with git.
    - Python: recreates `venv/` and installs via `uv pip install` (preferred)
      or `venv/bin/pip` (fallback). A `pypi:` source installs wheels only;
      `source_build` names the packages the manifest allows to build from
      source, and only when a wheels-only resolve fails naming one is it
      retried with `--no-binary` for the named ones (a listed package with a
      wheel for this machine installs from the wheel). When uv needs to fetch
      a Python version that differs from system Python, it installs it under
      the platform's `mcps/.uv-python/` (via ``UV_PYTHON_INSTALL_DIR``) so the
      interpreter lives inside the already-sandbox-mounted `mcps/` tree,
      keeping every MCP self-contained and the sandbox free of any user-home
      leakage.
    - Docker: returns success with a note (build/run is a separate flow).

    Every subprocess runs under ``_install_env``: the parent's secrets never
    reach a package manager or a build backend. ``timeout`` bounds each
    subprocess's whole run; past it the subprocess and everything it started
    are killed and ``asyncio.TimeoutError`` reaches the caller (the community
    installer rolls back, a satellite keeps the old version).

    `progress_cb` receives `{phase, pct, message}` dicts (both sync and
    async callables supported) so the satellite can stream them over WS.

    ``python_spec`` is the interpreter a venv this install creates should run
    on (the platform passes its own minor; a floor uv asks for replaces it,
    and a pin uv cannot satisfy falls back to uv's default). With
    ``sandbox_roots``, a created venv whose interpreter lies outside them is
    made again on a uv-managed interpreter (see ``_create_venv``). A caller
    passing neither gets uv's default interpreter, as before.

    ``run`` starts each step (``STEPS``) and has no default, so a caller
    that forgets it cannot build at all: the platform and a compute machine
    pass one that runs the step in a sandbox (``tool_build``, the satellite's
    ``mcp_install_support.build_runner``), a paired computer and the image
    build ``run_unconfined``.
    """
    mcp_dir = Path(mcp_dir)
    name = mcp_dir.name

    # Pythons that uv fetches land in a folder the session sandbox binds
    # read-only (``uv_python_dir_for``).
    uv_python_dir = uv_python_dir_for(mcp_dir)
    # UV_LINK_MODE=copy forces uv to copy files into the venv instead of
    # hardlinking/symlinking the python.exe. On Windows, creating those
    # links requires either Developer Mode enabled OR admin rights —
    # without them, `uv venv` fails with "Failed to create Python
    # executable link ... .tmpXXX". Copy mode is slightly slower but
    # works on every OS without extra system privileges.
    env = _install_env({
        "UV_PYTHON_INSTALL_DIR": str(uv_python_dir),
        "UV_LINK_MODE": "copy",
    })

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
        rc, log = await run(
            _shell_argv(["npm", "install", "--omit=dev", "--ignore-scripts"]),
            cwd=str(mcp_dir), env=env, timeout=timeout, step="npm", mcp_dir=mcp_dir,
        )
        if rc != 0:
            await _emit(progress_cb, {"mcp": name, "phase": "failed", "pct": 100, "message": "npm install failed", "error": log})
            return InstallResult(ok=False, log=log)

        # Shipped patches, applied by git (never by an executable the package
        # or a registry provides).
        patches_dir = mcp_dir / "patches"
        patches = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []
        if patches:
            await _emit(progress_cb, {"mcp": name, "phase": "npm", "pct": 80, "message": "patches"})
            patched, patch_log = await _apply_patches(mcp_dir, patches, env, timeout, run=run)
            log += "\n" + patch_log
            if not patched:
                await _emit(progress_cb, {"mcp": name, "phase": "failed", "pct": 100, "message": "patches failed", "error": log})
                return InstallResult(ok=False, log=log)

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
        parsed_pypi: ParsedSource | None = None
        if source.startswith("git+"):
            # pip VCS source — pip/uv accept "git+https://…@<ref>#subdirectory=<dir>"
            # verbatim. NEVER rewrite the "@<ref>" to "==" (it is the git ref, not
            # a version pin). The pinned ref lives in the manifest, so bumping it
            # changes compute_version_hash → satellites resync + reinstall.
            pip_pkg = source
            upgrade = False
        else:
            parsed_pypi = parse_source(source)
            if parsed_pypi is None:
                return InstallResult(ok=False, log=f"Unparseable pypi source: {source!r}")
            # "pypi:ha-mcp[x]@6.6.1" -> "ha-mcp[x]==6.6.1": pip and uv resolve
            # the extras.
            pip_pkg = parsed_pypi.requirement + (
                f"=={parsed_pypi.version}" if parsed_pypi.version else "")
            # Unpinned "pypi:pkg" → bare requirement (no "=="); the venv is
            # fresh, so --upgrade only keeps the resolver honest about
            # cached wheels. A pinned "pkg==X.Y.Z" needs no flag (exact
            # forces it) and must NOT float transitive deps on the satellite.
            upgrade = not parsed_pypi.version
        venv_dir = mcp_dir / "venv"
        # A catalog package installs from wheels only: no build backend of a
        # source distribution ever runs here, except for a package the
        # manifest names in ``source_build`` once a wheels-only resolve has
        # failed on it (``--no-binary`` forces the source build, so it is
        # never passed up front). A git source builds by definition (an
        # explicit admin install; never the weekly job).
        wheels_only = source.startswith("pypi:")

        async def _make_venv(floor: str | None) -> tuple[bool, str, str]:
            """Create the venv: on ``floor`` when uv asked for one, else on
            the caller's pin."""
            want = floor or python_spec
            uv_here = bool(uv_bin and os.path.isfile(uv_bin))  # noqa: ASYNC240  # one stat of the uv binary before a minutes-long install
            msg = (f"uv venv{' --python ' + want if want else ''}" if uv_here
                   else "python -m venv")
            await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 10, "message": msg})
            return await _create_venv(
                venv_dir, uv_bin=uv_bin, python_bin=python_bin, floor=floor,
                pin=python_spec, roots=sandbox_roots, env=env, cwd=str(mcp_dir),
                timeout=timeout, run=run,
            )

        async def _pip_install(no_binary: list[str]) -> tuple[bool, str]:
            # Prefer uv (much faster). UV_PYTHON_INSTALL_DIR ensures any Python
            # uv fetches lands in the platform tree (under mcps/.uv-python/),
            # which the session sandbox already mounts — keeping every MCP
            # self-contained and the sandbox free of user-home leakage.
            upgrade_flag = ["--upgrade"] if upgrade else []
            binary_flags = ["--only-binary=:all:"] if wheels_only else []
            for pkg in no_binary:
                binary_flags.extend(["--no-binary", pkg])
            if uv_bin and os.path.isfile(uv_bin):  # noqa: ASYNC240  # one stat of the uv binary before a minutes-long install
                cmd = [uv_bin, "pip", "install", "--compile-bytecode",
                       "--python", str(venv_dir / _VENV_BIN_DIR / f"python{_EXE_SUFFIX}"),
                       *binary_flags, *upgrade_flag, pip_pkg]
            else:
                cmd = [str(venv_dir / _VENV_BIN_DIR / f"pip{_EXE_SUFFIX}"),
                       "install", "--isolated", *binary_flags, *upgrade_flag, pip_pkg]
            rc, out = await run(cmd, cwd=str(mcp_dir), env=env, timeout=timeout, step="pip", mcp_dir=mcp_dir)
            return rc == 0, out

        async def _install_package() -> tuple[bool, str]:
            # Wheels only first. A resolve that fails naming a source_build
            # package (and no Python floor, which the venv retry below owns)
            # is tried again with that package from source; each listed
            # package is added once at most.
            built: list[str] = []
            ok, log = await _pip_install(built)
            while not ok and wheels_only and not _PY_FLOOR_RE.search(log):
                more = [p for p in _source_builds_named(log, source_build or [])
                        if p not in built]
                if not more:
                    break
                built.extend(more)
                await _emit(progress_cb, {"mcp": name, "phase": "pip", "pct": 60,
                                          "message": f"build from source: {', '.join(more)}"})
                ok, log = await _pip_install(built)
            return ok, log

        # Always a fresh venv: the package manager probes the venv's
        # interpreter, and a preserved venv would run the previous install's
        # site hooks on the host. The caller's whole-dir backup restores the
        # old venv when the install fails. A venv holds thousands of files,
        # so it is removed off the event loop.
        await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
        ok, venv_log, venv_spec = await _make_venv(None)
        if not ok:
            log = f"venv creation failed:\n{venv_log}"
            await _emit(progress_cb, {"mcp": name, "phase": "failed", "pct": 100, "message": "venv failed", "error": log})
            return InstallResult(ok=False, log=log)

        await _emit(progress_cb, {"mcp": name, "phase": "pip", "pct": 50, "message": f"pip install {pip_pkg}"})
        ok, log = await _install_package()

        # If the package requires a Python version this venv doesn't satisfy,
        # uv names the floor (`requires Python >=3.14`). Recreate the venv with
        # that constraint and retry once. Single retry — if it still fails,
        # surface the original error.
        if not ok and uv_bin:
            m = _PY_FLOOR_RE.search(log)
            if m:
                floor = m.group(1).replace(" ", "")
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 30, "message": f"retry with Python{floor}"})
                await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
                ok, venv_log, venv_spec = await _make_venv(floor)
                if ok:
                    ok, log = await _install_package()
                else:
                    log = f"venv creation with Python{floor} failed:\n{venv_log}"
        # Read back the concrete version for pypi installs so the caller can pin
        # the LOCAL manifest, by the bare name (the extras are not part of it).
        # git+ has no PyPI dist name to query — skip it (its version of record
        # is the pinned git ref in the source). A readback miss is non-fatal:
        # pip install succeeded; the caller logs + leaves the manifest unpinned
        # (it self-heals on the next update).
        resolved = ""
        if ok and parsed_pypi is not None:
            resolved = _python_installed_version(venv_dir, parsed_pypi.package)
            if not resolved:
                log += f"\nInstalled {parsed_pypi.package} but could not read its version back; leaving manifest unpinned."

        phase = "done" if ok else "failed"
        await _emit(progress_cb, {"mcp": name, "phase": phase, "pct": 100, "message": log[-200:] if not ok else "installed"})
        vh = compute_version_hash(mcp_dir) if ok else ""
        return InstallResult(ok=ok, log=log, version_hash=vh, resolved_version=resolved,
                             venv_python=venv_spec if ok else "")

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
        venv_spec = ""
        if not venv_dir.is_dir():
            # Prefer uv venv when available — the satellite-bundled `python -m
            # venv` triggers an internal `ensurepip --default-pip` step that
            # fails on Windows when Defender quarantines the freshly-copied
            # venv python.exe (returncode=1 with no actionable message). uv
            # builds a clean venv WITHOUT ensurepip — uv manages pip ops
            # natively, so the broken step is sidestepped. Falls back to
            # `python -m venv` only if uv isn't bundled (shouldn't happen on
            # any supported satellite, but defensive). The install env carries
            # UV_LINK_MODE=copy so the python.exe link step also works
            # without Windows Developer Mode / admin.
            uv_here = bool(uv_bin and os.path.isfile(uv_bin))  # noqa: ASYNC240  # one stat of the uv binary before a minutes-long install
            msg = ((f"uv venv --python {python_spec}" if python_spec else "uv venv")
                   if uv_here else "python -m venv")
            await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 10,
                                       "message": msg})
            ok, out, venv_spec = await _create_venv(
                venv_dir, uv_bin=uv_bin, python_bin=python_bin, floor=None,
                pin=python_spec, roots=sandbox_roots, env=env, cwd=str(mcp_dir),
                timeout=timeout, run=run,
            )
            if not ok:
                log = "venv creation failed:\n" + out
                await _emit(progress_cb, {"mcp": name, "phase": "failed",
                                           "pct": 100, "message": "venv failed",
                                           "error": log})
                return InstallResult(ok=False, log=log)
        async def _req_install() -> tuple[bool, str]:
            # Prefer uv when available (much faster).
            if uv_bin and os.path.isfile(uv_bin):  # noqa: ASYNC240  # one stat of the uv binary before a minutes-long install
                cmd = [uv_bin, "pip", "install", "--compile-bytecode",
                       "--python", str(venv_dir / _VENV_BIN_DIR / f"python{_EXE_SUFFIX}"),
                       "-r", str(req_file)]
            else:
                cmd = [str(venv_dir / _VENV_BIN_DIR / f"pip{_EXE_SUFFIX}"),
                       "install", "--isolated", "-r", str(req_file)]
            rc, out = await run(cmd, cwd=str(mcp_dir), env=env, timeout=timeout, step="pip", mcp_dir=mcp_dir)
            return rc == 0, out

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
        if not ok and uv_bin and os.path.isfile(uv_bin):  # noqa: ASYNC240  # one stat of the uv binary before a minutes-long install
            m = _PY_FLOOR_RE.search(log)
            if m:
                floor = m.group(1).replace(" ", "")
                await _emit(progress_cb, {"mcp": name, "phase": "deps", "pct": 30,
                                           "message": f"retry with Python{floor}"})
                await asyncio.to_thread(shutil.rmtree, venv_dir, ignore_errors=True)
                created, out, venv_spec = await _create_venv(
                    venv_dir, uv_bin=uv_bin, python_bin=python_bin, floor=floor,
                    pin=python_spec, roots=sandbox_roots, env=env, cwd=str(mcp_dir),
                    timeout=timeout, run=run,
                )
                if created:
                    ok, log = await _req_install()
                else:
                    log = f"venv creation with Python{floor} failed:\n" + out
        if not ok:
            await _emit(progress_cb, {"mcp": name, "phase": "failed",
                                       "pct": 100, "message": "pip install failed",
                                       "error": log})
            return InstallResult(ok=False, log=log)
        vh = compute_version_hash(mcp_dir)
        await _emit(progress_cb, {"mcp": name, "phase": "done", "pct": 100,
                                   "message": "installed"})
        return InstallResult(ok=True, log=log, version_hash=vh, venv_python=venv_spec)

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
