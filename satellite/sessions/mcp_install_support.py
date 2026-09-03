"""MCP install/swap filesystem helpers + post-install warm-up.

Free functions supporting the SessionManager's MCP sync path: robust dir
clear-or-quarantine and atomic-swap fixups (Windows AV-lock tolerant), venv
shebang / .exe-wrapper rewrites after a swap, the installed-MCP scan, and the
post-install warm-up that boots each freshly-installed stdio MCP once
(``initialize`` round-trip) to compile .pyc + warm the page cache so the first
real session spawn isn't slow. Pure helpers; the SessionManager imports the ones
it calls. Kept whole alongside (not inside) sync_mcps, which stays in
session_manager as a cohesive hot path.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..host import env_hygiene
from ..config import atomic_replace, force_rmtree, kill_process_tree, venv_exe
import contextlib

logger = logging.getLogger("satellite")


# Markers in an installer's output that indicate a TRANSIENT Windows file lock
# (a real-time AV scanner / Search Indexer holding a just-written file open
# without share-delete) rather than a real install failure. uv/pip then abort
# with one of these on a rename/replace. Retrying past the scan window clears it.
_TRANSIENT_LOCK_MARKERS = (
    "access is denied",
    "os error 5",
    "winerror 5",
    "winerror 32",
    "failed to rename",
    "being used by another process",
)


def _is_transient_lock(log: str) -> bool:
    """True if installer output looks like a transient AV/scanner file lock."""
    low = (log or "").lower()
    return any(m in low for m in _TRANSIENT_LOCK_MARKERS)


def _force_remove_or_quarantine(path: Path, quarantine: Path, *, label: str) -> None:
    """Robustly clear ``path`` so an atomic ``os.replace`` onto it can succeed.

    First tries ``force_rmtree``. If that can't delete it — typically because a
    live/zombie process (or AV) on Windows holds a handle on a file inside it,
    classically a venv ``python.exe`` — RENAME the directory aside to
    ``quarantine`` instead. Windows allows renaming a directory containing open
    files even when deleting it is blocked, which frees the original path for the
    swap; the quarantined dir is GC'd by the satellite startup sweep (a reboot
    releases the handles).

    Raises only if BOTH the forced delete AND the rename fail — at which point the
    caller cannot safely swap and should fail the install.

    Use this instead of ``shutil.rmtree(..., ignore_errors=True)`` anywhere a
    subsequent ``os.replace`` onto ``path`` must succeed: a silent partial rmtree
    leaves the dir present, and ``os.replace`` cannot rename onto an existing
    non-empty directory on Windows (``WinError 5``)."""
    try:
        force_rmtree(path)
    except OSError as rm_err:
        try:
            path.rename(quarantine)
            logger.warning(
                "%s: rmtree blocked, quarantined %s → %s (%s)",
                label, path.name, quarantine.name, rm_err,
            )
        except OSError as mv_err:
            # Even rename failed — bubble up the original rmtree error; nothing
            # else we can do without a reboot.
            raise rm_err from mv_err


def _fix_shebangs_after_swap(old_root: Path, new_root: Path) -> None:
    """Rewrite venv entry-point shebangs after an os.replace swap.

    pip + uv bake the absolute path to the venv's Python interpreter into
    every entry-point script in ``venv/bin/``. When ``sync_mcps`` installs
    into ``<X>.new/`` and atomically renames to ``<X>/``, those shebangs
    still reference the deleted ``<X>.new/venv/bin/python`` — the script
    immediately fails with ``bad interpreter`` on every invocation.

    This walks ``new_root/venv/bin/*`` and replaces any shebang pointing
    at ``old_root`` with the equivalent path under ``new_root``. Safe to
    run on dirs without a venv (returns early) or on already-correct
    shebangs (no-op).
    """
    venv_bin = new_root / "venv" / "bin"
    if not venv_bin.is_dir():
        return
    old_str = str(old_root)
    new_str = str(new_root)
    if old_str == new_str:
        return
    for entry in venv_bin.iterdir():
        if not entry.is_file():
            continue
        try:
            # Read just the first chunk; shebangs are always the first line.
            with open(entry, "rb") as f:
                head = f.read(4096)
            if not head.startswith(b"#!"):
                continue
            head_text = head.decode("utf-8", errors="replace")
            if old_str not in head_text:
                continue
            # Read the rest, rewrite first line, write back atomically.
            with open(entry, "rb") as f:
                content = f.read()
            text = content.decode("utf-8", errors="replace")
            nl = text.find("\n")
            if nl == -1:
                continue
            first_line = text[:nl].replace(old_str, new_str)
            fixed = first_line + text[nl:]
            tmp = entry.with_suffix(entry.suffix + ".shebang-tmp")
            tmp.write_text(fixed)
            # Mode bits only matter on Unix (entry scripts need +x).
            if sys.platform != "win32":
                tmp.chmod(entry.stat().st_mode)
            atomic_replace(tmp, entry)
        except (OSError, UnicodeDecodeError):
            # Binary file or read failure — leave it alone.
            continue


def _fix_windows_exe_paths_after_swap(old_root: Path, new_root: Path) -> None:
    """Rewrite embedded python.exe path inside pip/distlib .exe wrappers.

    On Windows, pip + uv install console-script entry-points as
    ``venv/Scripts/<name>.exe`` wrappers built by distlib's ScriptMaker.
    Layout: ``[launcher PE][shebang line][zip payload]``. The shebang
    embeds the *absolute* path to the venv's python.exe — baked at
    install time. Because we install into ``<X>.new/`` and atomically
    rename to ``<X>/``, those wrappers still reference
    ``<X>.new/venv/Scripts/python.exe`` (deleted by the swap) and every
    entry-point fails with WinError 2 the moment Claude/Codex tries to
    spawn the MCP. ``_fix_shebangs_after_swap`` can't help — these are
    PE binaries with appended zip data whose byte offsets must remain
    stable (the launcher reads the zip EOCD record from the end of the
    file).

    Strategy: substitute ``<old_root>\\venv\\Scripts\\python.exe`` →
    ``<new_root>\\venv\\Scripts\\python.exe`` PADDED with trailing
    spaces inside the shebang line so the file's byte length stays
    constant. The trailing whitespace is harmless — the launcher
    strips whitespace from the parsed shebang path before exec.

    No-op on every platform except Windows.
    """
    if sys.platform != "win32":
        return
    venv_scripts = new_root / "venv" / "Scripts"
    if not venv_scripts.is_dir():
        return
    old_python = str(old_root / "venv" / "Scripts" / "python.exe")
    new_python = str(new_root / "venv" / "Scripts" / "python.exe")
    if old_python == new_python:
        return
    # distlib + pip always write backslashes on Windows; cover the
    # forward-slash variant defensively in case a wheel installer normalized.
    variants: list[tuple[bytes, bytes]] = []
    for old_str, new_str in (
        (old_python, new_python),
        (old_python.replace("\\", "/"), new_python.replace("\\", "/")),
    ):
        old_b = old_str.encode("utf-8")
        new_b = new_str.encode("utf-8")
        if len(new_b) > len(old_b):
            # New path longer than old (shouldn't happen — <X>.new is
            # always 4 chars longer than <X>). Can't safely pad shorter;
            # skip this variant.
            continue
        pad = b" " * (len(old_b) - len(new_b))
        variants.append((old_b, new_b + pad))

    for entry in venv_scripts.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".exe":
            continue
        try:
            content = entry.read_bytes()
            changed = False
            for old_b, new_b_padded in variants:
                if old_b in content:
                    content = content.replace(old_b, new_b_padded)
                    changed = True
            if not changed:
                continue
            tmp = entry.with_suffix(entry.suffix + ".rewrap-tmp")
            tmp.write_bytes(content)
            atomic_replace(tmp, entry)
        except OSError as e:
            logger.warning(
                "Failed to rewrite .exe wrapper %s: %s", entry.name, e,
            )
            continue


def _uv_bin_if_present() -> str | None:
    """Return path to ``uv`` on this satellite host, or None if not found.

    The satellite typically runs under a systemd user unit (Linux) /
    Task Scheduler (Windows) with a minimal PATH that doesn't include
    ``~/.local/bin``, so we
    check explicit locations. Required for Python MCPs that pin a
    non-system Python version (e.g. ``unifi-network`` declaring
    ``python>=3.13`` while the platform's default is 3.10). Without uv,
    ``install_mcp`` falls back to system Python and pip fails with a
    version-mismatch error.

    The ``os.access(X_OK)`` check is load-bearing on Unix:
    ``/usr/local/bin/uv`` is often a symlink to ``/root/.local/bin/uv``
    which is unreadable as a non-root user. On Windows, ``os.access(...,
    X_OK)`` is essentially ``os.access(..., F_OK)`` (existence only),
    which is fine since we look at named ``.exe`` paths.
    """
    if sys.platform == "win32":
        # Astral's PowerShell installer (used by install.ps1's
        # baseline-tools step) drops uv.exe in %USERPROFILE%\.local\bin\.
        # %LOCALAPPDATA%\uv\ is a fallback for some package managers.
        candidates = [
            os.path.expanduser(r"~\.local\bin\uv.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\uv\uv.exe"),
            # Last-ditch: bare name on PATH (winget-installed uv ends up there).
        ]
    else:
        candidates = [
            os.path.expanduser("~/.local/bin/uv"),
            "/usr/local/bin/uv",
            "/usr/bin/uv",
        ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Fallback to PATH lookup — covers winget installs (Windows) and
    # any custom install location on Unix.
    fallback = shutil.which("uv")
    return fallback or None


def _scan_installed_mcps(mcps_dir: Path) -> list[str]:
    """Scan mcps directory for installed MCP server names.

    Reads the `name` field from each manifest.json (matches the key
    used in mcpServers config). Handles both flat layout
    (mcps/{name}/manifest.json) and nested layout
    (mcps/{category}/{name}/manifest.json).
    """
    if not mcps_dir.is_dir():
        return []
    names = []
    manifests = []
    for d in mcps_dir.iterdir():
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        if mf.exists():
            manifests.append(mf)
        else:
            for sub in d.iterdir():
                if sub.is_dir() and (sub / "manifest.json").exists():
                    manifests.append(sub / "manifest.json")
    for mf in manifests:
        try:
            data = json.loads(mf.read_text())
            # Include both manifest name and server_name (used as mcpServers key)
            names.append(data.get("name", mf.parent.name))
            server_name = data.get("server_name", "")
            if server_name and server_name != data.get("name"):
                names.append(server_name)
        except (json.JSONDecodeError, OSError):
            names.append(mf.parent.name)
    return names


# --- MCP pre-warm (cold-start mitigation) -----------------------------------
#
# A freshly-installed MCP has no ``__pycache__`` and a cold OS page cache, so
# its first spawn is slow — slow enough that the CLI's initial ``tools/list``
# poll can miss it on the very first session ("still connecting" retries;
# worst on Windows). Booting each MCP once right after install compiles the
# ``.pyc``, warms the page cache, and runs module init so the real session
# spawn is fast. The ``initialize`` round-trip also confirms the server boots
# — a GENERIC MCP-protocol check (every compliant stdio server answers
# ``initialize``; no per-MCP knowledge needed). Advisory only: a warm-up
# failure NEVER excludes the MCP (a missing credential at startup is not a
# real failure — the real session env supplies it). Even on failure the
# imports already ran, so the cold-start mitigation holds.

_WARMUP_TIMEOUT_S = 10.0
_WARMUP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "otodock-warmup", "version": "1.0"},
    },
}


def _resolve_warmup_command(target_root: Path, command: str, args: list) -> list[str]:
    """Resolve a manifest stdio ``command``/``args`` to a concrete argv.

    Mirrors ``proxy/services/mcp/mcp_registry.py::resolve_server_config``'s stdio
    rules, but resolves the venv binary via the satellite's own layout
    (``venv_exe`` → ``venv/Scripts/<bin>.exe`` on Windows) so no proxy-side
    path translation is needed.
    """
    # command
    if command.startswith("venv/bin/") or command.startswith("venv/Scripts/"):
        binary = command.rsplit("/", 1)[-1]
        if binary.lower().endswith(".exe"):
            binary = binary[:-4]
        if binary == "python3":
            binary = "python"
        resolved_cmd = str(venv_exe(target_root / "venv", binary))
    elif "/" in command and not os.path.isabs(command):
        resolved_cmd = str(target_root / command)
    else:
        # bare command (e.g. "node") resolved via PATH, or already absolute.
        resolved_cmd = command

    # args
    resolved_args: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            continue
        a = arg.replace("${mcp_dir}", str(target_root))
        if a.startswith("-"):
            pass  # flag — keep
        elif os.path.isabs(a):
            pass  # already absolute (template-resolved)
        elif "." in a:
            a = str(target_root / a)  # bare filename like "server.py"
        # else plain token (e.g. "stdio") — keep as-is
        resolved_args.append(a)

    return [resolved_cmd, *resolved_args]


def _warmup_env() -> dict:
    """Inherit the (curated) satellite env but guarantee ``.pyc`` writes by
    dropping ``PYTHONDONTWRITEBYTECODE``. No session credentials — ``initialize``
    doesn't need them and warm-up is credential-agnostic. Curated for the same
    reason as the spawn sites (operator's ambient secrets out of the child env);
    warm-up has no config["env"] overlay so curation is the only filter here."""
    env = env_hygiene.curate_satellite_env(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


async def _warm_handshake(proc: "asyncio.subprocess.Process") -> None:
    """Send an MCP ``initialize`` and wait for the matching reply.

    Returns on a JSON-RPC message with ``id == 0`` (result or error — either
    proves the server finished importing and read stdin). Raises on EOF
    (process died before replying) or non-reply.
    """
    proc.stdin.write((json.dumps(_WARMUP_INITIALIZE) + "\n").encode("utf-8"))
    await proc.stdin.drain()
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError("process exited before initialize reply")
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            # Non-JSON stdout line (shouldn't happen for a compliant MCP);
            # keep reading until the protocol reply or EOF/timeout.
            continue
        if isinstance(msg, dict) and msg.get("id") == 0:
            return


async def _kill_warmup_proc(proc: "asyncio.subprocess.Process | None") -> None:
    """Tree-kill a warm-up subprocess and reap it (no zombies / leaked pipes).
    ``kill_process_tree`` is psutil-backed + synchronous, so run it off-loop."""
    if proc is None:
        return
    if proc.returncode is None:
        try:
            await asyncio.to_thread(kill_process_tree, proc.pid, 5.0)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


async def _warm_one_mcp(
    target_root: Path, name: str, sem: asyncio.Semaphore,
) -> tuple[str, str]:
    """Pre-warm one freshly-installed stdio MCP. Returns ``(name, status)``
    where status is ``"ok"`` (got an ``initialize`` reply), ``"skip:<why>"``
    (HTTP/SSE/remote MCP — nothing to spawn), or ``"warn:<reason>"``.
    Never raises — warm-up is advisory."""
    manifest_path = target_root / "manifest.json"
    try:
        server = (json.loads(manifest_path.read_text(encoding="utf-8")).get("server") or {})
    except Exception as e:
        logger.info("MCP %s warm-up skipped: manifest unreadable (%s)", name, e)
        return (name, f"warn:manifest unreadable ({e})")

    if server.get("transport") != "stdio":
        return (name, "skip:not-stdio")
    raw_cmd = server.get("command") or ""
    if not raw_cmd:
        return (name, "skip:no-command")

    cmd = _resolve_warmup_command(target_root, raw_cmd, server.get("args") or [])

    async with sem:
        proc: "asyncio.subprocess.Process | None" = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(target_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_warmup_env(),
                limit=10 * 1024 * 1024,  # a chatty banner shouldn't overrun readline
            )
            await asyncio.wait_for(_warm_handshake(proc), timeout=_WARMUP_TIMEOUT_S)
            logger.info("MCP %s warm-up: ok", name)
            return (name, "ok")
        except asyncio.TimeoutError:
            logger.warning("MCP %s warm-up: timeout (imports still ran)", name)
            return (name, "warn:timeout")
        except Exception as e:
            stderr_tail = ""
            if proc is not None and proc.stderr is not None:
                try:
                    data = await asyncio.wait_for(proc.stderr.read(4096), timeout=1.0)
                    stderr_tail = data.decode("utf-8", errors="replace").strip()[-500:]
                except Exception:
                    pass
            reason = f"{type(e).__name__}: {e}"
            logger.warning(
                "MCP %s failed to start during warm-up: %s%s",
                name, reason, f" | stderr: {stderr_tail}" if stderr_tail else "",
            )
            return (name, f"warn:{reason}")
        finally:
            await _kill_warmup_proc(proc)


# --- Runtime reconciliation (platform Python/Node bump) ---------------------
#
# Mirrors proxy/services/mcp/mcp_venv_bootstrap.py for the satellite: when THIS host's
# interpreter advances (e.g. install.sh bumped Python 3.10→3.13), MCP venvs built
# on the old interpreter must be rebuilt; on a node MAJOR bump (22→24) native
# addons must be rebuilt. The proxy can't drive this — it doesn't know the
# satellite's local runtime changed (version_hash covers manifest+lockfiles, not
# the interpreter). So the satellite self-reconciles at startup. Kept HERE (not in
# the vendored installer) so it never bumps SHARED_MCP_INSTALLER_HASH. A
# ``.oto-runtime.json`` marker (next to manifest.json, SURVIVES a venv-only delete)
# makes it idempotent and never re-churns a ceiling-pinned MCP.

_RUNTIME_MARKER = ".oto-runtime.json"


def _venv_python_minor(venv_dir: Path) -> tuple[int, int] | None:
    """``(major, minor)`` from ``venv/pyvenv.cfg``, or None if missing/garbled."""
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
    """Major version of the system ``node`` (cached), or None if absent."""
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


def _read_runtime_marker(mcp_dir: Path) -> dict:
    try:
        return json.loads((mcp_dir / _RUNTIME_MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_runtime_marker(mcp_dir: Path, **updates: object) -> None:
    data = _read_runtime_marker(mcp_dir)
    data.update(updates)
    try:
        (mcp_dir / _RUNTIME_MARKER).write_text(json.dumps(data))
    except OSError as e:
        logger.warning("could not write runtime marker for %s: %s", mcp_dir.name, e)


def _needs_python_reconcile(venv_dir: Path, target: tuple[int, int], mcp_dir: Path) -> bool:
    """``True`` when the venv interpreter lags this host and isn't already
    reconciled to it (a marker == target means a real upstream requires-python
    ceiling kept it lower — don't re-install it every boot)."""
    if not venv_dir.is_dir():
        return False
    vm = _venv_python_minor(venv_dir)
    if vm is None or vm >= target:
        return False
    return _read_runtime_marker(mcp_dir).get("python") != f"{target[0]}.{target[1]}"


async def _uv_venv_pinned(uv_bin: str, venv_dir: Path, target: tuple[int, int], mcp_dir: Path) -> None:
    """Pre-create ``venv_dir`` on this host's interpreter so the subsequent
    install pip-installs into a venv that matches the platform. Best-effort."""
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
    """``npm rebuild`` native addons when the system node MAJOR changed since
    ``node_modules`` was built. Advisory; absent marker → record, don't rebuild."""
    current = _node_major()
    if current is None:
        return None
    recorded = _read_runtime_marker(mcp_dir).get("node_major")
    if recorded is None:
        _write_runtime_marker(mcp_dir, node_major=current)
        return None
    if recorded == current:
        return None
    logger.info("reconcile: %s node %s→%s — npm rebuild", name, recorded, current)
    try:
        from .._vendored.mcp_installer import _shell_argv
        proc = await asyncio.create_subprocess_exec(
            *_shell_argv(["npm", "rebuild"]), cwd=str(mcp_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            _write_runtime_marker(mcp_dir, node_major=current)
            return "ok-node-rebuild"
        logger.warning(
            "npm rebuild for %s failed: %s", name, out.decode(errors="replace")[-300:],
        )
        return "skipped-node-rebuild-fail"
    except Exception:
        logger.exception("npm rebuild for %s errored", name)
        return "skipped-node-rebuild-fail"


async def reconcile_mcp_runtimes(mcps_dir: Path, uv_bin: str | None) -> dict[str, str]:
    """Rebuild MCP venvs / native addons that lag this host's interpreter / node.

    Best-effort + idempotent; never raises. For a Python MCP whose venv interpreter
    is older than this host's: delete ONLY the venv (the marker survives) and
    rebuild it pinned to the host interpreter via the vendored ``install_mcp``. For
    a Node MCP after a major bump: ``npm rebuild``. Returns ``name → outcome`` for
    logging. Returns ``{}`` when there's nothing to do (the common case).
    """
    from .._vendored import mcp_installer

    results: dict[str, str] = {}
    if not mcps_dir.is_dir():
        return results
    target = sys.version_info[:2]

    for cat_dir in sorted(mcps_dir.iterdir()):
        if not cat_dir.is_dir():
            continue
        for mcp_dir in sorted(cat_dir.iterdir()):
            mf = mcp_dir / "manifest.json"
            if not mcp_dir.is_dir() or not mf.is_file():
                continue
            try:
                manifest = json.loads(mf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            name = manifest.get("name") or mcp_dir.name
            runtime = (manifest.get("server") or {}).get("runtime", "")

            if runtime == "python":
                venv_dir = mcp_dir / "venv"
                if not (mcp_dir / "requirements.txt").is_file():
                    continue
                if not _needs_python_reconcile(venv_dir, target, mcp_dir):
                    continue
                try:
                    force_rmtree(venv_dir)  # Windows AV-lock safe
                except OSError:
                    logger.warning("reconcile: could not remove stale venv for %s", name)
                    continue
                if uv_bin:
                    await _uv_venv_pinned(uv_bin, venv_dir, target, mcp_dir)
                logger.info("reconcile: rebuilding %s venv on Python %s.%s", name, *target)
                try:
                    r = await mcp_installer.install_mcp(mcp_dir, "python", "", uv_bin=uv_bin)
                    if r.ok:
                        _write_runtime_marker(mcp_dir, python=f"{target[0]}.{target[1]}")
                        results[name] = "ok-py-reconcile"
                    else:
                        results[name] = "failed"
                        logger.warning("reconcile: %s rebuild failed:\n%s", name, r.log[-300:])
                except Exception:
                    logger.exception("reconcile: %s rebuild errored", name)
                    results[name] = "exception"

            elif runtime == "node" and (mcp_dir / "node_modules").is_dir():
                outcome = await _reconcile_node_addons(mcp_dir, name)
                if outcome:
                    results[name] = outcome

    return results
