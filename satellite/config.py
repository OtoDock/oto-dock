"""Configuration + cross-platform helpers for the satellite daemon.

Loads ``satellite.conf`` (INI format) from:
  * ``~/.oto-dock/satellite.conf`` on Linux/macOS
  * ``~/OtoDock/satellite.conf`` on Windows

Cross-platform helpers used across the package:
  * ``OTODOCK_DIRNAME`` / ``otodock_dir()`` — install root location
  * ``venv_bin()`` / ``venv_exe()`` — Windows ``Scripts`` vs Unix ``bin``
  * ``kill_process_tree()`` — psutil-backed tree termination (kills
    ``cmd.exe`` → ``node.exe`` grandchildren on Windows)
  * ``atomic_replace()`` — ``os.replace`` with AV-retry on Windows
  * ``force_rmtree()`` — ``shutil.rmtree`` with AV-retry + cmd-rmdir
    fallback on Windows
  * ``relaunch_self()`` — per-user service-manager respawn after a
    clean exit (no-op on Linux; kickstart/schtasks on macOS/Windows)
  * ``hook_command()`` — settings.json hook-command string with
    quoted Python path for both platforms
  * ``WINDOWS_DETACHED_FLAGS`` — ``subprocess`` creationflags for
    detaching children from the satellite's process group
"""

import configparser
import contextlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("satellite")

# Per-user install root. Dotfile on Unix, visible folder on Windows
# (dotfiles are unusual on Windows and would render as hidden in Explorer).
OTODOCK_DIRNAME = "OtoDock" if sys.platform == "win32" else ".oto-dock"

# Executable suffix and venv binary subdir, platform-aware.
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# subprocess creationflags for spawning detached children on Windows
# (used by self-uninstall + self-relaunch so the child outlives the
# satellite's own exit — its own process group, not killed when we go).
# 0 on non-Windows, so the same constant can be used unconditionally.
WINDOWS_DETACHED_FLAGS = (
    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    if sys.platform == "win32"
    else 0
)


def otodock_dir() -> Path:
    """Absolute path to the per-user OtoDock root."""
    return Path.home() / OTODOCK_DIRNAME


def venv_bin(venv_dir: Path) -> Path:
    """Return the venv's binary directory (``Scripts``/Win, ``bin``/Unix)."""
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin")


def venv_exe(venv_dir: Path, name: str) -> Path:
    """Return path to a named venv executable. ``venv_exe(v, "pip")`` →
    ``<v>/bin/pip`` on Unix, ``<v>\\Scripts\\pip.exe`` on Windows.
    """
    return venv_bin(venv_dir) / f"{name}{EXE_SUFFIX}"


def hook_command(hook_path: Path) -> str:
    """Build the ``settings.json::hooks.command`` string for CLAUDE hooks.

    Form: ``"<python.exe>" "<hook.py>"`` — both quoted because user home
    dirs commonly contain spaces (``C:\\Users\\First Last\\…``) and Claude
    Code's Node hook runner splits the field quote-aware on every OS.
    Codex hooks must NOT use this form on Windows — see codex_hook_command.
    """
    return f'"{sys.executable}" "{hook_path}"'


def codex_hook_command(hook_path: Path) -> str:
    """Build the ``hooks.json`` command string for CODEX hooks.

    Codex's Rust hook runner executes the string via ``sh -lc`` on POSIX
    (quote-aware — the Claude form works) but via ``cmd.exe /C <string>`` on
    Windows, where Rust's MSVC arg-escaping of a string with embedded quotes
    produces a line cmd cannot re-parse: the hook dies with exit code 1
    before Python ever starts, on EVERY tool call ("PreToolUse hook
    (failed)" spam in interactive Codex on Windows satellites; verified
    against codex-rs hooks/engine/command_runner.rs @ 0.144.x).

    Windows therefore gets a QUOTE-FREE command: the bare path of a
    pure-ASCII ``.cmd`` wrapper written next to the hook script. The wrapper
    resolves the script ``%~dp0``-relative and the interpreter through the
    ``OTO_HOOK_PY`` env var (set in the codex session env), so non-ASCII
    user paths never appear in the batch text — cmd reads batch files in
    the OEM codepage, not UTF-8. Residual limit: a ``&<>()@^|`` character
    in the .codex dir path would still defeat cmd's quote handling.
    """
    if sys.platform != "win32":
        return hook_command(hook_path)
    wrapper = hook_path.with_suffix(".cmd")
    wrapper.write_text(
        '@if "%OTO_HOOK_PY%"=="" set "OTO_HOOK_PY=python"\r\n'
        f'@"%OTO_HOOK_PY%" "%~dp0{hook_path.name}"\r\n'
        "@exit /b %errorlevel%\r\n",
        encoding="ascii",
        newline="",
    )
    return str(wrapper)


def kill_process_tree(pid: int, timeout: float = 5.0) -> None:
    """Terminate a process and all descendants, best-effort.

    On Windows, ``claude.CMD`` runs via ``cmd.exe`` which spawns
    ``node.exe`` — ``proc.terminate()`` only kills the shim and orphans
    node. psutil walks the tree so we get all of them.

    psutil is a hard runtime requirement (see requirements.txt). If
    import fails we raise rather than silently orphaning processes.
    """
    import psutil  # raises ImportError loudly if missing

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    try:
        descendants = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []

    # Terminate children first so they don't get reparented mid-cleanup.
    for proc in descendants:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        parent.terminate()

    _, alive = psutil.wait_procs([parent] + descendants, timeout=timeout)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()


def snapshot_descendants(pid: int) -> list:
    """Snapshot a live process's descendant processes for later reaping.

    MUST be called while ``pid`` is still alive: once a parent exits, its
    children are reparented (Unix) or orphaned (Windows) and can no longer be
    enumerated from the parent. Capturing the ``psutil.Process`` objects up
    front lets the caller gracefully exit the parent (e.g. let claude flush and
    persist its session for ``--resume``) and *then* reap any MCP children the
    parent left running. Returns ``[]`` if the process is already gone.
    """
    import psutil  # hard runtime requirement (see kill_process_tree)

    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []


def reap_descendants(procs: list, timeout: float = 5.0) -> None:
    """Terminate then kill a previously-snapshotted descendant list.

    Best-effort companion to :func:`snapshot_descendants`. Reaping leaked MCP
    children matters most on Windows, where a still-running ``.exe``/``.pyd``
    keeps its parent directory locked — which blocks the in-place MCP-update
    swap (``os.replace``/rename both fail with ``WinError 5``). No-op on an
    empty list.
    """
    if not procs:
        return
    import psutil

    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()


def atomic_replace(src: Path, dst: Path, *, attempts: int = 3, delay: float = 0.2) -> None:
    """``os.replace(src, dst)`` with retry on Windows.

    Antivirus and Windows Defender briefly hold handles on freshly-
    written executables, raising ``PermissionError`` from ``os.replace``.
    Three 200ms retries clears the typical scan window. On Unix the
    first attempt succeeds and we never sleep.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except (PermissionError, OSError):
            if attempt == attempts - 1 or sys.platform != "win32":
                raise
            time.sleep(delay)


def force_rmtree(path: Path, *, attempts: int = 12, delay: float = 0.5) -> None:
    """``shutil.rmtree`` with aggressive retry + ``rmdir /s /q`` fallback.

    Unlike ``shutil.rmtree(..., ignore_errors=True)`` which silently
    leaves partial state behind, this raises on persistent failure so
    the caller knows the directory wasn't fully cleared. Critical for
    the satellite MCP install path: a stale ``<name>.new/`` dir from a
    prior failed install causes the next attempt to extract over a
    broken venv, manifesting as cryptic uv "Failed to inspect Python
    interpreter" errors with no obvious cause.

    On Windows, Defender / Search-Indexer / explorer.exe / antivirus
    can hold handles on freshly-written Python venv files for
    several seconds — especially right after MCP install creates a
    venv with newly-written ``python.exe``. Real-world testing showed
    5×200ms was not enough; 12×500ms (~6s total) covers the typical
    Defender scan window.

    If ``shutil.rmtree`` still fails after all Python-side retries,
    on Windows we fall back to ``cmd.exe /c rmdir /s /q`` which uses
    the Win32 file APIs directly (no Python file-handle layer). This
    sometimes succeeds when ``shutil.rmtree`` gives up because cmd's
    rmdir handles read-only attributes and some lock types better.

    On Unix the first attempt succeeds and we never sleep.

    Returns silently if ``path`` doesn't exist (idempotent).
    """
    import shutil
    import subprocess

    if not path.exists():
        return
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except (PermissionError, OSError) as e:
            last_err = e
            if attempt == attempts - 1 or sys.platform != "win32":
                break
            time.sleep(delay)

    # On Windows: last-ditch ``cmd /c rmdir /s /q``. cmd's rmdir
    # uses the Win32 file APIs directly and sometimes succeeds when
    # ``shutil.rmtree`` (which goes through Python's path-handling
    # layer) gives up — particularly for read-only attributes and
    # some lock types. Synchronous; ~5s timeout for large trees.
    if sys.platform == "win32" and path.exists():
        try:
            r = subprocess.run(
                ["cmd.exe", "/c", "rmdir", "/s", "/q", str(path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if not path.exists():
                return
            # rmdir failed too — surface its stderr in the raise so the
            # caller's log has both Python's and cmd's diagnostics.
            cmd_err = r.stderr.decode("utf-8", errors="replace").strip()
            if cmd_err:
                last_err = OSError(
                    f"{type(last_err).__name__}: {last_err}; "
                    f"cmd.exe rmdir fallback: {cmd_err}"
                )
        except (subprocess.TimeoutExpired, OSError) as e:
            last_err = OSError(f"{last_err}; rmdir fallback raised: {e}")

    if last_err is not None:
        raise last_err


def _ps_squote(s: str) -> str:
    """Single-quote a value for embedding in a PowerShell ``-Command`` snippet."""
    return "'" + str(s).replace("'", "''") + "'"


def windows_schedule_oneshot(
    task_name: str, execute: str, argument: str, delay_s: int,
) -> tuple[bool, str]:
    """Register a one-shot per-user Scheduled Task (Windows), locale-safe.

    Replaces the old ``schtasks /Create /SC ONCE /SD <date> /ST <time>`` form,
    which had TWO landmines:

      * ``/SD`` is parsed in the SYSTEM LOCALE's short-date format. The
        hardcoded ``%m/%d/%Y`` failed on any non-US locale (``dd/MM/yyyy``)
        whenever the day was > 12 ("ERROR: Invalid Start Date.") — the task
        was never created, so the satellite silently stayed DOWN after an
        auto-update (the Win11/Greek-locale "doesn't autostart" bug; en-US
        machines were immune).
      * ``/ST`` is minute-granularity (floors to ``HH:MM:00``), which forced
        the 70 s delay workaround.

    ``New-ScheduledTaskTrigger -Once -At`` takes a real DateTime — no locale
    string parsing, second precision. Default principal = current user,
    interactive token, RunLevel Limited (same as the old ``/RU … /IT
    /RL LIMITED``). Synchronous and fast — safe to call from a dying daemon
    before its ``os._exit``.

    Returns ``(ok, detail)`` — ``detail`` carries rc/stdout/stderr on failure.
    """
    ps = (
        "$ErrorActionPreference='Stop';"
        f"$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds({int(delay_s)});"
        f"$a=New-ScheduledTaskAction -Execute {_ps_squote(execute)}"
        f" -Argument {_ps_squote(argument)};"
        f"Register-ScheduledTask -TaskName {_ps_squote(task_name)}"
        " -Trigger $t -Action $a -Force | Out-Null"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=60, check=False, text=True,
        )
    except Exception as e:
        return False, f"powershell spawn failed: {e}"
    if r.returncode != 0:
        return False, (
            f"rc={r.returncode} out={(r.stdout or '').strip()!r} "
            f"err={(r.stderr or '').strip()!r}"
        )
    return True, ""


def relaunch_self() -> None:
    """(Re)start the satellite via the per-user service manager.

    Called after a clean ``exit(0)`` that still needs a respawn the service
    manager won't do on its own — an auto-update apply or a boot-guard
    rollback. The caller performs the actual ``sys.exit(0)`` AFTER this
    returns, so the exit stays visible at the call site.

      * Linux:   no-op. The systemd *user* unit's ``Restart=always``
                 respawns us the instant we exit.
      * macOS:   ``launchctl kickstart`` the per-user LaunchAgent.
      * Windows: a logon Scheduled Task auto-restarts only on *failure*
                 (non-zero exit), NOT on a clean ``exit(0)``, AND it runs the
                 daemon inside a kill-on-close Job Object — so a plain detached
                 child (``DETACHED_PROCESS`` is NOT breakaway) is killed the
                 instant we exit and the relaunch never fires. We instead
                 register a **one-shot Scheduled Task in its own fresh job**
                 (``windows_schedule_oneshot`` — locale-safe, second-precision)
                 that ``schtasks /Run``s the logon task ~30 s after we're gone —
                 by then our instance has exited, so ``IgnoreNew`` lets a fresh
                 one start.

                 BOUNDARY GOTCHA: a fix here only takes effect from the update
                 where the ALREADY-running code has it. The update that first
                 installs it is relaunched by the OLD code, so THAT one may
                 still need a one-time manual start; every update after
                 self-restarts.
    """
    if sys.platform == "win32":
        ok, detail = windows_schedule_oneshot(
            "OtoDockSatelliteRelaunch",
            "schtasks.exe", "/Run /TN OtoDockSatellite",
            delay_s=30,
        )
        if ok:
            logger.info(
                "relaunch: scheduled OtoDockSatelliteRelaunch (+30s, locale-safe)"
            )
        else:
            # Make the failure VISIBLE, then use the last-ditch fallback
            # (kill-prone — the detached child shares our Job Object — but
            # better than nothing).
            logger.error(
                "relaunch: one-shot task create failed (%s) — falling back "
                "to detached PowerShell /Run", detail,
            )
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command",
                 "Start-Sleep -Seconds 3; schtasks /Run /TN OtoDockSatellite"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=WINDOWS_DETACHED_FLAGS,
            )
    elif sys.platform == "darwin":
        subprocess.Popen(
            ["launchctl", "kickstart", "-k",
             f"gui/{os.getuid()}/com.otodock.satellite"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    # Linux: intentionally nothing — Restart=always handles the respawn.


_WINDOWS_TASK_NAME = "OtoDockSatellite"

# Canonical start-tray.vbs — MUST match install.ps1's heredoc so the heal
# below and the installer converge on identical content.
_START_TRAY_VBS = (
    "' OtoDock Satellite -- silent tray launcher (no console window). Starts the\r\n"
    "' daemon via its logon Scheduled Task: launches it if it was Quit, no-ops if\r\n"
    "' already running. Invoked by the Start Menu shortcut.\r\n"
    'CreateObject("WScript.Shell").Run "schtasks /Run /TN OtoDockSatellite", 0, False\r\n'
)


def ensure_windows_install_artifacts() -> None:
    """Converge install-time Windows artifacts that auto-update never touches.

    The logon-task definition, the silent ``start-tray.vbs`` launcher and the
    Start-Menu shortcut are written by ``install.ps1`` at INSTALL time only —
    an auto-update swaps ``satellite\\`` but re-registers none of them. A
    machine whose task action predates the ``conhost --headless`` form hosts
    the daemon in a VISIBLE terminal window (Windows Terminal — Win11's
    default console host — ignores ``-WindowStyle Hidden`` for scheduled
    tasks; closing the window kills the satellite). Runs at every daemon
    start, best-effort, each piece independent:

      1. Task action not the canonical ``conhost.exe --headless powershell …``
         → ``Set-ScheduledTask`` with the canonical action (triggers/settings/
         principal preserved; the RUNNING instance is unaffected — takes
         effect next start).
      2. ``start-tray.vbs`` missing or stale → rewrite.
      3. Start-Menu ``OtoDock Satellite.lnk`` missing or mis-targeted →
         (re)write.
    """
    if sys.platform != "win32":
        return
    oto = otodock_dir()
    runner = oto / "satellite" / "runner.ps1"

    # 1. Logon-task action — converge on the canonical headless form:
    #    `conhost.exe --headless powershell.exe … runner.ps1`. Win11 delegates
    #    console apps to Windows Terminal by default, and WT IGNORES the
    #    powershell -WindowStyle Hidden for scheduled tasks — the daemon ran
    #    inside a VISIBLE terminal window that killed it when closed (Win10's
    #    legacy conhost honors the hide, hence the machine split). A headless
    #    classic console shows no window under either terminal; conhost
    #    propagates the child's exit code, preserving restart-on-failure.
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", _WINDOWS_TASK_NAME, "/XML"],
            capture_output=True, timeout=20, check=False, text=True,
        )
        xml = r.stdout or ""
        if r.returncode == 0 and (
            "conhost" not in xml.lower() or "--headless" not in xml
        ):
            canonical = (
                f'--headless powershell.exe -NoProfile -ExecutionPolicy Bypass '
                f'-WindowStyle Hidden -File "{runner}"'
            )
            ps = (
                "$ErrorActionPreference='Stop';"
                "$a=New-ScheduledTaskAction -Execute 'conhost.exe'"
                f" -Argument {_ps_squote(canonical)}"
                f" -WorkingDirectory {_ps_squote(str(oto))};"
                f"Set-ScheduledTask -TaskName {_ps_squote(_WINDOWS_TASK_NAME)}"
                " -Action $a | Out-Null"
            )
            rc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=60, check=False, text=True,
            )
            if rc.returncode == 0:
                logger.info(
                    "install-heal: task action converged to conhost --headless "
                    "(next start runs windowless)"
                )
            else:
                logger.warning(
                    "install-heal: task action update failed rc=%s err=%r",
                    rc.returncode, (rc.stderr or "").strip(),
                )
    except Exception:
        logger.debug("install-heal: task check skipped", exc_info=True)

    # 2. Silent launcher script.
    vbs = oto / "start-tray.vbs"
    try:
        current = ""
        if vbs.exists():
            current = vbs.read_text(encoding="utf-8", errors="replace")
        if current.replace("\r\n", "\n") != _START_TRAY_VBS.replace("\r\n", "\n"):
            vbs.write_text(_START_TRAY_VBS, encoding="ascii", newline="")
            logger.info("install-heal: start-tray.vbs (re)written")
    except Exception:
        logger.debug("install-heal: vbs write skipped", exc_info=True)

    # 3. Start-Menu shortcut — create if missing (pre-0.5.4 installs never
    #    had one) AND validate an existing one (rewrite unless it points at
    #    wscript + start-tray.vbs — a stale/manual shortcut targeting
    #    runner.ps1 directly bypasses the task entirely).
    try:
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            lnk = (
                Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "OtoDock Satellite.lnk"
            )
            ico = oto / "satellite" / "bin" / "otodock.ico"
            icon_line = (
                f"$l.IconLocation={_ps_squote(str(ico))};" if ico.exists() else ""
            )
            # One PS pass: load-or-create, check canonical target, fix if off.
            ps = (
                "$ErrorActionPreference='Stop';"
                "$w=New-Object -ComObject WScript.Shell;"
                f"$l=$w.CreateShortcut({_ps_squote(str(lnk))});"
                "$base=[System.IO.Path]::GetFileName($l.TargetPath);"
                f"$ok=($base -ieq 'wscript.exe') -and "
                "($l.Arguments -like '*start-tray.vbs*');"
                "if (-not $ok) {"
                "$l.TargetPath='wscript.exe';"
                f"$l.Arguments={_ps_squote(chr(34) + str(vbs) + chr(34))};"
                f"$l.WorkingDirectory={_ps_squote(str(oto))};"
                + icon_line +
                "$l.Description='Start the OtoDock Satellite "
                "(connects in the background; no window)';"
                "$l.Save();"
                "Write-Output 'FIXED'"
                "}"
            )
            rc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=60, check=False, text=True,
            )
            if rc.returncode == 0:
                if "FIXED" in (rc.stdout or ""):
                    logger.info("install-heal: Start-Menu shortcut (re)written")
            else:
                logger.warning(
                    "install-heal: shortcut check failed rc=%s err=%r",
                    rc.returncode, (rc.stderr or "").strip(),
                )
    except Exception:
        logger.debug("install-heal: shortcut skipped", exc_info=True)


_DEFAULT_CONFIG_PATH = otodock_dir() / "satellite.conf"

# Wire-protocol version (satellite <-> proxy). The proxy refuses to drive a
# satellite older than its MIN_SATELLITE_VERSION (proxy/ws/satellite.py). Bump
# this on any change to the satellite<->proxy contract; a purely additive frame
# an older proxy can safely ignore needs a bump only when the proxy must gate
# behaviour on it (e.g. satellite_supports_pty). Per-change history is in git.
SATELLITE_VERSION = "0.5.114"
SHARED_STDIO_INTERCEPTOR_HASH = "7afe65d06ada641e89c9901c261417dbf3d0786f043ff415b7a580248b831449"
SHARED_CODEX_APPROVALS_HASH = "118ad311166465a2ab4990177cefc056af3ca236af38a112556dfc0a3e362391"
SHARED_APP_SERVER_CLIENT_HASH = "8afc272b0568ff5da018ed5be03fcf3b91c830c58f6d074ca3f13e56537bbb8b"
SHARED_MCP_INSTALLER_HASH = "61b99c692d36c17760d6de93abd2a269d4a3ed9a8da861fa9c829fb351221419"


@dataclass
class SatelliteConfig:
    machine_id: str
    machine_secret: str
    platform_url: str
    agents_dir: Path
    mcps_dir: Path
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    # Opt-in for plaintext ws:// to a publicly-resolving host (split-horizon
    # DNS / VPN overlays) — without it the daemon refuses that transport
    # (transport/ws_client.assert_transport_secure).
    allow_insecure_transport: bool = False


def load_config(path: Path | None = None) -> SatelliteConfig:
    """Load configuration from INI file."""
    config_path = path or _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            f"Run the installer or create {config_path} manually."
        )

    cp = configparser.ConfigParser()
    # ``utf-8-sig`` transparently strips a UTF-8 BOM if present. PS 5.1's
    # ``Set-Content -Encoding UTF8`` writes BOM by default; the Windows
    # installer now uses BOM-less UTF-8, but tolerating BOM here means
    # a hand-edited conf doesn't break the satellite at startup.
    cp.read(config_path, encoding="utf-8-sig")

    sat = cp["satellite"]
    agents_str = sat.get("agents_dir") or ""
    mcps_str = sat.get("mcps_dir") or ""
    return SatelliteConfig(
        machine_id=sat["machine_id"],
        machine_secret=sat["machine_secret"],
        platform_url=sat["platform_url"],
        agents_dir=(
            Path(agents_str).expanduser() if agents_str
            else otodock_dir() / "agents"
        ),
        mcps_dir=(
            Path(mcps_str).expanduser() if mcps_str
            else otodock_dir() / "mcps"
        ),
        claude_bin=cp.get("cli", "claude_bin", fallback="claude"),
        codex_bin=cp.get("codex", "codex_bin", fallback="codex"),
        allow_insecure_transport=sat.getboolean(
            "allow_insecure_transport", fallback=False,
        ),
    )
