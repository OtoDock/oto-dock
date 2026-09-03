"""Entry point for the satellite daemon.

Usage: python -m satellite [--config /path/to/satellite.conf]

IMPORTANT — module-top imports MUST stay limited to stdlib + ``.config`` (a
stdlib-only leaf). The boot guard ``_check_post_update_state`` runs from
``main()`` BEFORE any subpackage is imported, so a broken auto-update — a
missing/renamed ``transport``/``sessions``/``terminal``/``host``/``_vendored``
module — rolls itself back instead of crash-looping on an ImportError that would
otherwise fire up here, before the guard ever runs. The heavy subpackage imports
therefore live INSIDE ``_main()`` (function-local), never at module top. Do NOT
add ``from .transport… import`` (or any other subpackage import) at module top —
``test_update_robustness.py`` asserts this and will fail.
"""

import argparse
import asyncio
import atexit
import contextlib
import faulthandler
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .config import (
    SATELLITE_VERSION,
    SHARED_APP_SERVER_CLIENT_HASH,
    SHARED_CODEX_APPROVALS_HASH,
    SHARED_MCP_INSTALLER_HASH,
    SHARED_STDIO_INTERCEPTOR_HASH,
    atomic_replace,
    force_rmtree,
    load_config,
    otodock_dir,
    relaunch_self,
)

# NOTE: the heavy subpackage imports (transport / sessions / terminal / host)
# are deliberately NOT here — they live inside _main() so the boot guard can run
# first. See the module docstring.

logger = logging.getLogger("satellite")


def _verify_installer_drift() -> None:
    """Fail-fast if the vendored shared mcp_installer has drifted from the
    hash baked into config at vendoring time. A drift means someone edited
    the satellite copy directly instead of running scripts/sync-satellite-code.sh
    on the proxy — behavior will silently diverge from the platform installer.
    """
    for mod_name, baked in (
        ("mcp_installer", SHARED_MCP_INSTALLER_HASH),
        ("app_server_client", SHARED_APP_SERVER_CLIENT_HASH),
        ("codex_approvals", SHARED_CODEX_APPROVALS_HASH),
        ("stdio_path_interceptor", SHARED_STDIO_INTERCEPTOR_HASH),
    ):
        try:
            mod = __import__(f"satellite._vendored.{mod_name}", fromlist=[mod_name])
        except ImportError:
            continue  # not vendored yet (older setup); skip silently
        actual = mod.self_hash()
        if actual and baked and actual != baked:
            logging.getLogger("satellite").error(
                "%s drift detected: expected=%s actual=%s — run "
                "scripts/sync-satellite-code.sh on the proxy and reinstall "
                "the satellite",
                mod_name, baked[:16], actual[:16],
            )
            sys.exit(2)


def _check_post_update_state() -> None:
    """Boot guard: detect a crash-looping auto-update and roll back to the
    previous build. Called from ``main()`` BEFORE the heavy subpackage imports
    (which live in ``_main()``), so a broken update that can't even import rolls
    itself back instead of crash-looping forever — the failure mode that bricked
    satellites when the flat package was split into subpackages.

    State machine (marker = ``~/.oto-dock/.update_in_progress``, three lines:
    ``previous_version`` / ``target_version`` / ``attempt_count``):
      * No marker → no update in progress; return.
      * Marker present + ``.last_successful_boot`` records the marker's TARGET
        version → the new code already authed (a clock-independent check — see
        below). Marker is stale; clean it up + drop ``satellite.previous/``.
      * Marker present, NOT yet authed, attempts < 2 → first/second boot of the
        new code. Bump the counter and continue; it gets a chance to auth.
      * Marker present, attempts >= 2 → crash-looped twice without authing.
        Roll back: swap ``satellite.previous`` onto ``satellite`` and restart.

    Rollback is crash-safe and NEVER clears the marker unless it actually
    produced a runnable tree:
      * The reuse-venv update path MOVES the shared venv into the new build, so
        ``satellite.previous`` can be venv-less; we carry the venv back from the
        (crash-looping) current build first — else the rolled-back code has no
        interpreter and systemd's ExecStart dies 203/EXEC.
      * Any filesystem failure leaves the CURRENT tree untouched and KEEPS the
        marker so the next boot retries, instead of relaunching into a broken
        build with the marker cleared (which would masquerade as success).

    Uses only ``config`` helpers (a stdlib-only leaf imported at module top) +
    stdlib — no heavier satellite import, so a broken update can't poison its
    own rollback path.
    """
    oto_dir = otodock_dir()
    marker = oto_dir / ".update_in_progress"
    if not marker.exists():
        return

    sat_root = oto_dir / "satellite"
    sat_prev = oto_dir / "satellite.previous"

    body = marker.read_text().strip().split("\n")
    target_version = body[1] if len(body) > 1 else ""
    attempts = int(body[2]) if len(body) > 2 and body[2].isdigit() else 0

    # Has the update's TARGET version already authenticated on some boot since
    # the update started? ``_finalize_post_update_state`` records the running
    # version in ``.last_successful_boot`` on auth-success, so this is a
    # clock-INDEPENDENT check. Comparing file MTIMES here would misfire if the
    # host clock jumped backward (CMOS reset, NTP step-back, dual-boot RTC, VM
    # snapshot) — a fresh marker could land OLDER than a stale success stamp and
    # we'd wrongly drop a LIVE update's rollback copy. Version match can't be
    # fooled by the clock, and it also can't misfire on a failed swap (where
    # the stamp holds the OLD version, not the target).
    last_ok = oto_dir / ".last_successful_boot"
    last_ok_version = ""
    if last_ok.exists():
        try:
            head = last_ok.read_text().splitlines()
            last_ok_version = head[0].strip() if head else ""
        except OSError:
            pass

    if target_version and last_ok_version == target_version:
        # The new code already authed; this marker is stale (finalize's unlink
        # must have failed). Clean it up so we never false-rollback working code.
        with contextlib.suppress(OSError):
            marker.unlink()
        if sat_prev.exists():
            with contextlib.suppress(OSError):
                force_rmtree(sat_prev)
        return

    if attempts >= 2:
        logging.getLogger("satellite").warning(
            "Update crash-loop detected (attempts=%d) — rolling back to %s",
            attempts, body[0] if body else "?",
        )
        if sys.platform == "win32":
            # Can't swap in-process: the running venv (OtoDockSatellite.exe
            # + loaded .pyd) lives inside satellite\, so deleting it hits a
            # locked file. Hand the swap-back to runner.ps1 (pre-import, no
            # locks) via a marker, then relaunch via the logon task. The new
            # process can't auto-restart on a clean exit-0, hence relaunch.
            with contextlib.suppress(OSError):
                (oto_dir / ".rollback-pending").write_text(
                    f"{body[0] if body else ''}\n"
                )
            # Drop the update marker so the rolled-back code's boot guard
            # doesn't re-trigger (runner.ps1 also clears it during swap-back).
            with contextlib.suppress(OSError):
                marker.unlink()
            relaunch_self()
            sys.exit(0)

        # Unix: renaming a dir with open files is fine (inode-based), so swap
        # in-process, then relaunch (Linux: no-op + Restart=always; macOS:
        # launchctl kickstart). Every failure path below leaves the CURRENT tree
        # in place and KEEPS the marker, so a failed rollback retries on the next
        # boot instead of clearing the marker and relaunching into a still-broken
        # build (which would masquerade as a successful update).
        log = logging.getLogger("satellite")
        if not sat_prev.exists():
            # Nothing to roll back to (e.g. a prior successful update already
            # dropped it). Fail LOUD and keep the marker rather than silently
            # crash-looping the broken build.
            log.error(
                "Update crash-loop but no satellite.previous to roll back to "
                "(target %s) — manual reinstall required",
                body[1] if len(body) > 1 else "?",
            )
            return

        # The reuse-venv update path MOVES the shared venv into the new build,
        # leaving satellite.previous without an interpreter. Carry it back from
        # the crash-looping current build FIRST (before deleting anything), so
        # the rolled-back code can actually start. Bail on failure WITHOUT
        # touching the current tree.
        cur_venv = sat_root / "venv"
        prev_venv = sat_prev / "venv"
        if cur_venv.is_dir() and not prev_venv.exists():
            try:
                atomic_replace(cur_venv, prev_venv)
            except OSError:
                log.exception(
                    "rollback: could not carry the venv into satellite.previous "
                    "— leaving current tree intact, retrying next boot",
                )
                return

        # satellite.previous is now a complete, runnable tree. Swap it in.
        try:
            if sat_root.exists():
                force_rmtree(sat_root)
            atomic_replace(sat_prev, sat_root)
        except OSError:
            log.exception("rollback swap failed — retrying on next boot")
            return

        # Rolled back successfully. Now (and only now) clear the marker, tidy a
        # leftover staged build, and relaunch onto the restored code.
        with contextlib.suppress(OSError):
            marker.unlink()
        sat_new = oto_dir / "satellite.new"
        if sat_new.exists():
            with contextlib.suppress(OSError):
                force_rmtree(sat_new)
        log.warning("Rolled back to %s — relaunching", body[0] if body else "?")
        relaunch_self()
        sys.exit(0)
    else:
        # Increment the attempt counter and let the new code run. If
        # it auths, the auth-success path clears the marker; if it
        # crashes, we land here again on the next boot.
        prev_v = body[0] if body else ""
        target_v = body[1] if len(body) > 1 else ""
        marker.write_text(f"{prev_v}\n{target_v}\n{attempts + 1}\n")


def _setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    # On Windows the logon Scheduled Task can't capture the daemon's
    # stdout/stderr to a file the way systemd/launchd do — and a PowerShell
    # stream redirect in runner.ps1 kills the process on its first stderr
    # line (NativeCommandError under -ErrorAction Stop). So when runner.ps1
    # sets OTO_SATELLITE_LOG, the daemon owns its own rotating log file.
    # (Linux/macOS leave OTO_SATELLITE_LOG unset → journald/launchd capture +
    # rotate stdout themselves; only the Windows file needs our own rotation.)
    log_path = os.environ.get("OTO_SATELLITE_LOG")
    if log_path:
        try:
            from logging.handlers import RotatingFileHandler
            # Bounded by rotation so the file can NEVER grow without limit: each
            # segment is capped at max_mb, and only `backups` rotated segments are
            # kept (older ones are auto-deleted on rollover). Defaults cap the
            # total at ~20 MB (1 active + 3 × 5 MB); tunable via env for low-disk
            # hosts (OTO_SATELLITE_LOG_MAX_MB / OTO_SATELLITE_LOG_BACKUPS).
            try:
                max_mb = float(os.environ.get("OTO_SATELLITE_LOG_MAX_MB") or 5.0)
            except ValueError:
                max_mb = 5.0
            try:
                backups = int(os.environ.get("OTO_SATELLITE_LOG_BACKUPS") or 3)
            except ValueError:
                backups = 3
            handlers.append(
                RotatingFileHandler(
                    log_path,
                    maxBytes=max(1, int(max_mb * 1024 * 1024)),
                    backupCount=max(0, backups),
                    encoding="utf-8",
                )
            )
        except (OSError, ValueError):
            # Bad path / perms — fall back to console-only, never crash boot.
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# Module-global so the faulthandler fault-log file object isn't garbage-
# collected — that would close its fd and silence native-crash reports.
_FAULT_LOG_FH = None


def _install_crash_diagnostics() -> None:
    """Make a SILENT death leave a trace.

    The daemon's clean-shutdown, auto-update, self-uninstall, and rollback
    paths each log a reason before they exit. But three failure modes leave
    NOTHING in satellite.log — exactly the "it just closed right after
    connecting" symptom:

      * a NATIVE crash (access violation / segfault) in a C extension —
        commonly the pystray/Pillow tray message pump or an SSL/socket
        binding — kills the process with no Python traceback;
      * an UNCAUGHT exception on a daemon thread (e.g. the tray thread),
        which CPython prints to a now-unwatched stderr and then lets the
        thread die;
      * an external TerminateProcess (AV/Defender quarantine of the freshly
        written, unsigned OtoDockSatellite.exe; or a Task-Scheduler stop).

    All best-effort (diagnostics must never break a working daemon):
      * faulthandler → a dedicated ``satellite-fault.log`` (all threads), so a
        native crash dumps a C-level traceback we can read on the next boot;
      * ``threading.excepthook`` / ``sys.excepthook`` → route uncaught
        exceptions on ANY thread into the satellite log (flushed) before the
        default reporter swallows them;
      * an ``atexit`` breadcrumb so a Python-level teardown is timestamped —
        its PRESENCE next time means the exit was intentional/Python-level,
        its ABSENCE (with an empty fault log) means an external hard kill.

    On boot, a non-empty fault log from the PREVIOUS run is surfaced into the
    main log as a WARNING (then rotated aside), so reading satellite.log alone
    tells you the last run crashed natively and where.
    """
    global _FAULT_LOG_FH
    log = logging.getLogger("satellite")
    try:
        oto_dir = otodock_dir()
        fault_path = oto_dir / "satellite-fault.log"

        # Surface a PRIOR run's native crash into the main log, then rotate it
        # aside so this run starts with a clean fault file.
        try:
            if fault_path.exists() and fault_path.stat().st_size > 0:
                prior = fault_path.read_text(errors="replace").strip()
                log.warning(
                    "Previous run left a native-fault report (likely the cause "
                    "of an unexplained exit) at %s:\n%s",
                    fault_path, prior[:4000],
                )
                fault_path.replace(fault_path.with_suffix(".log.prev"))
        except OSError:
            pass

        try:
            _FAULT_LOG_FH = open(fault_path, "w", encoding="utf-8")
            faulthandler.enable(file=_FAULT_LOG_FH, all_threads=True)
        except (OSError, RuntimeError, ValueError):
            # No writable path / no real stderr fd — fall back to default.
            with contextlib.suppress(Exception):
                faulthandler.enable(all_threads=True)

        # Uncaught exceptions on background threads (the tray pump is the
        # usual suspect) otherwise die silently. Route them to the log.
        def _thread_excepthook(args):
            if args.exc_type is SystemExit:
                return
            log.error(
                "Uncaught exception on thread %r — this can take down the daemon",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        with contextlib.suppress(Exception):
            threading.excepthook = _thread_excepthook

        _prev_excepthook = sys.excepthook

        def _main_excepthook(exc_type, exc_value, exc_tb):
            if not issubclass(exc_type, KeyboardInterrupt):
                log.critical(
                    "Uncaught exception on the main thread — daemon exiting",
                    exc_info=(exc_type, exc_value, exc_tb),
                )
            _prev_excepthook(exc_type, exc_value, exc_tb)

        sys.excepthook = _main_excepthook

        def _exit_breadcrumb():
            with contextlib.suppress(Exception):
                log.info("Satellite process exiting (pid=%d)", os.getpid())

        atexit.register(_exit_breadcrumb)

        log.info(
            "Crash diagnostics active (pid=%d, v%s); native faults → %s",
            os.getpid(), SATELLITE_VERSION, fault_path,
        )
    except Exception:
        # Strictly additive observability — never let it break boot.
        log.debug("crash-diagnostics setup skipped", exc_info=True)


def _otodock_wrapper_src(name: str) -> "Path | None":
    """Locate a ``bin/<name>`` wrapper across BOTH install layouts (mirrors
    ``tray._load_base_image``): the flat dev checkout (``<pkg>/bin/<name>``) and
    the nested tarball install where ``bin/`` is a sibling of the package
    (``<pkg>.parent/bin/<name>`` = ``<OtoDir>/satellite/bin``). Returns the first
    existing path, or ``None``."""
    here = Path(__file__).resolve().parent
    for cand in (here / "bin" / name, here.parent / "bin" / name):
        if cand.exists():
            return cand
    return None


def _ensure_otodock_on_path() -> None:
    """Best-effort, idempotent install so the `otodock` command is
    on the user's PATH. Run on EVERY startup, so both a fresh install and an
    auto-update get it without the user touching PATH.

    Unix/macOS: a symlink ``~/.local/bin/otodock`` → ``<pkg>/bin/otodock`` (never
    clobbers a real file the user placed there). Windows: copy
    ``bin/otodock.cmd`` into ``<otodock_dir>\\bin`` and add that dir to the user
    PATH (see :func:`_ensure_otodock_on_path_windows`)."""
    if sys.platform == "win32":
        _ensure_otodock_on_path_windows()
        return
    log = logging.getLogger("satellite")
    try:
        wrapper = _otodock_wrapper_src("otodock")
        if wrapper is None:
            return
        with contextlib.suppress(OSError):
            os.chmod(wrapper, 0o755)
        bindir = Path.home() / ".local" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        link = bindir / "otodock"
        if link.is_symlink():
            if os.path.realpath(link) == str(wrapper):
                return  # already correct
            link.unlink()
        elif link.exists():
            return  # a real file the user owns — leave it alone
        link.symlink_to(wrapper)
        log.info("otodock CLI linked onto PATH: %s → %s", link, wrapper)
    except Exception:
        log.debug("otodock PATH symlink (best-effort) failed", exc_info=True)


def _ensure_otodock_on_path_windows() -> None:
    """Windows twin of the POSIX symlink: put the REAL ``bin/`` dir that holds
    ``otodock.cmd`` on the user PATH. The ``.cmd`` resolves the venv + the
    ``satellite`` package RELATIVE to its own location (``%~dp0``), so it must run
    from its install dir — copying it elsewhere breaks that math. The real dir
    (``<root>/bin``) is recreated by every auto-update (the tarball ships it), so
    the PATH entry stays valid across updates. Best-effort + idempotent; never
    blocks startup."""
    log = logging.getLogger("satellite")
    try:
        src = _otodock_wrapper_src("otodock.cmd")
        if src is None:
            return
        real_bindir = src.parent  # <root>/bin — where the .cmd's %~dp0 math is correct

        # Self-heal an earlier 0.5.56 mistake that COPIED otodock.cmd into
        # <otodock_dir>/bin and PATH-added THAT dir: the copy's %~dp0-relative
        # lookups then pointed at <otodock_dir>/venv + <otodock_dir> (neither
        # holds the venv/package) → "No module named satellite.otodock_cli".
        # Drop the stray copy + its PATH entry so the real wrapper isn't shadowed.
        stale_bindir = otodock_dir() / "bin"
        if stale_bindir.resolve() != real_bindir.resolve():
            try:
                stale_cmd = stale_bindir / "otodock.cmd"
                if stale_cmd.exists():
                    stale_cmd.unlink()
            except OSError:
                pass
            _remove_from_user_path_windows(str(stale_bindir), log)

        _add_to_user_path_windows(str(real_bindir), log)
    except Exception:
        log.debug("otodock PATH shim (best-effort) failed", exc_info=True)


def _remove_from_user_path_windows(target_dir: str, log) -> None:
    """Remove ``target_dir`` from the user PATH (``HKCU\\Environment``), preserving
    the value's registry type. Best-effort; no-op if absent."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                cur, regtype = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return
            if regtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                regtype = winreg.REG_EXPAND_SZ
            target = target_dir.rstrip("\\").lower()
            parts = [p for p in (cur or "").split(";") if p]
            kept = [p for p in parts if p.rstrip("\\").lower() != target]
            if len(kept) == len(parts):
                return  # not present
            winreg.SetValueEx(key, "Path", 0, regtype, ";".join(kept))
        log.info("otodock CLI removed stale PATH entry: %s", target_dir)
        _broadcast_env_change()
    except OSError:
        pass


def _add_to_user_path_windows(new_dir: str, log) -> None:
    """Append ``new_dir`` to the user PATH via ``HKCU\\Environment`` (user scope
    only — never the system PATH), preserving the value's registry type. Writing
    REG_SZ over a REG_EXPAND_SZ Path would break every ``%VAR%`` expansion in it,
    so we re-use the queried type. Idempotent (case-insensitive membership check)
    so reboots/auto-updates don't grow the PATH. On failure, log a manual hint and
    continue — never fatal."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                cur, regtype = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                cur, regtype = "", winreg.REG_EXPAND_SZ
            if regtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                regtype = winreg.REG_EXPAND_SZ
            parts = [p for p in (cur or "").split(";") if p]
            target = new_dir.rstrip("\\").lower()
            if any(p.rstrip("\\").lower() == target for p in parts):
                return  # already present — idempotent
            parts.append(new_dir)
            winreg.SetValueEx(key, "Path", 0, regtype, ";".join(parts))
        log.info("otodock CLI added to user PATH: %s", new_dir)
        _broadcast_env_change()
    except OSError:
        log.warning(
            "otodock: could not add %s to your PATH automatically — add it to "
            "your user PATH to use the `otodock` command from any folder.", new_dir,
        )


def _broadcast_env_change() -> None:
    """Broadcast WM_SETTINGCHANGE('Environment') so NEW shells pick up the PATH
    edit without a logout. The shell that launched the satellite won't see it
    regardless; best-effort (failure just means the change lands on next login)."""
    try:
        import ctypes.wintypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageTimeoutW.argtypes = [
            ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.UINT, ctypes.wintypes.UINT, ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.wintypes.LPARAM
        result = ctypes.wintypes.DWORD(0)
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
        )
    except Exception:
        pass


async def _main(config_path: Path | None = None) -> None:
    # Heavy subpackage imports are deferred to here (NOT module top) so the boot
    # guard in main() can roll back a broken update before these would crash on a
    # missing/renamed module. See the module docstring.
    from .transport.http_tunnel import LocalTunnelServer
    from .terminal.local_socket import LocalControlServer
    from .sessions.session_manager import SessionManager
    from .transport.ws_client import SatelliteWSClient

    _verify_installer_drift()
    config = load_config(config_path)

    # CLIs installed via the reconcile's per-user npm-prefix fallback
    # (~/.npm-global/bin — sudo-less Linux can't write NodeSource's /usr
    # prefix) must resolve for every child this process spawns; augment
    # PATH once, before any session/MCP spawn.
    from .host.cli_versions import ensure_user_npm_bin_on_path
    ensure_user_npm_bin_on_path()

    # Ensure directories exist
    config.agents_dir.mkdir(parents=True, exist_ok=True)
    config.mcps_dir.mkdir(parents=True, exist_ok=True)

    sm = SessionManager(config)
    ws = SatelliteWSClient(config, sm)

    # Start the local HTTP tunnel server BEFORE the WS client connects.
    # The chosen port goes into the satellite's capabilities at auth time;
    # the platform uses it as PROXY_URL when injecting env for new
    # subprocesses. Hook scripts call http://127.0.0.1:<port>/v1/hooks/...
    # which the tunnel forwards over the WS.
    tunnel = LocalTunnelServer(ws)
    tunnel_port = await tunnel.start()
    ws.tunnel = tunnel
    sm.local_tunnel_port = tunnel_port

    # The local control socket — lets `otodock claude|codex` on
    # THIS machine open an interactive session as the agent. Brokers via the
    # existing WS (identity re-derived proxy-side); best-effort (a bind failure
    # logs + degrades, never blocks the satellite).
    local_ctl = LocalControlServer(ws, sm)
    await local_ctl.start()
    # Self-heal the `otodock` PATH symlink (fresh install + every auto-update).
    _ensure_otodock_on_path()

    # Kill orphan processes from previous run
    sm.cleanup_orphans()

    # Wipe leftover per-session secret files (SSH keys, OAuth token files) —
    # every session died with the previous satellite process, so anything
    # under session-secrets/ (and any .credentials/ token dir in the agent
    # tree) is dead material a crash prevented from being wiped at close.
    # The .credentials purge also clears the persistent copies accumulated
    # before token files became per-session transients.
    try:
        from .sessions import session_files
        session_files.wipe_all()
        session_files.purge_agent_tree_credentials(sm.config.agents_dir)
    except Exception:
        logger.exception("session-secrets sweep failed (non-fatal)")

    # Reconcile MCP runtimes that lag this host's interpreter/node (e.g. after a
    # Python 3.10→3.13 or Node 22→24 bump from a satellite update). Best-effort —
    # never blocks startup; the common case (nothing stale) returns immediately.
    try:
        from .sessions.mcp_install_support import (
            reconcile_mcp_runtimes, _uv_bin_if_present,
        )
        reconciled = await reconcile_mcp_runtimes(sm.config.mcps_dir, _uv_bin_if_present())
        if reconciled:
            logger.info("MCP runtime reconcile: %s", reconciled)
    except Exception:
        logger.exception("MCP runtime reconcile failed (non-fatal)")

    # Converge install-time Windows artifacts old installers left stale —
    # the visible-console task action, missing/stale start-tray.vbs and the
    # Start-Menu shortcut (no-op off Windows, best-effort on it).
    from .config import ensure_windows_install_artifacts
    ensure_windows_install_artifacts()

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    # Suppress benign Windows asyncio shutdown noise. When the WS
    # connection drops (e.g. proxy closes after machine deletion) or
    # the proactor event loop tears down sockets, the ProactorBase
    # transport tries to call ``socket.shutdown(SHUT_RDWR)`` on an
    # already-closed socket and raises ConnectionResetError. asyncio
    # then logs it via the default exception handler, polluting the
    # uninstall log with a multi-line stack trace that doesn't reflect
    # any real problem. Install a handler that filters these specific
    # close-time errors but lets every other exception through to the
    # default reporter.
    if sys.platform == "win32":
        def _benign_close_filter(loop_, context):
            exc = context.get("exception")
            msg = context.get("message", "")
            if isinstance(exc, ConnectionResetError):
                return  # silent — socket was already closed
            if "WinError 10054" in msg:
                return
            loop_.default_exception_handler(context)
        loop.set_exception_handler(_benign_close_filter)

    def _signal_handler():
        logging.getLogger("satellite").info("Shutdown signal received")
        shutdown_event.set()

    # `loop.add_signal_handler` is Unix-only. On Windows, asyncio doesn't
    # support signal handlers; we rely on `KeyboardInterrupt` propagating out
    # of `asyncio.run`, the tray Quit setting `shutdown_event`, and the
    # Scheduled-Task stop terminating the process from outside — each lands in
    # clean shutdown or the `except KeyboardInterrupt` block in `main()`.
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

    # Windows system-tray icon (headless-safe: None off-Windows / no pystray).
    # asyncio keeps THIS main thread; the tray runs on its own daemon thread.
    # Centralizing the tray start here keeps the "who owns the main thread"
    # decision in one place for any future macOS menu-bar backend.
    from .host import tray as tray_mod
    cmd_queue: asyncio.Queue = asyncio.Queue()
    tray = tray_mod.maybe_start_tray(loop, cmd_queue, config)
    if tray is not None:
        # ws_client pushes connection-state transitions to the tray icon.
        ws.set_status_callback(tray.set_status)

    async def _consume_tray_commands() -> None:
        """Drain tray commands on the loop thread (the tray enqueues them
        cross-thread via call_soon_threadsafe)."""
        from .transport.lifecycle_update import _self_uninstall_and_exit
        log = logging.getLogger("satellite")
        while True:
            cmd = await cmd_queue.get()
            try:
                if cmd == tray_mod.CMD_PAUSE:
                    await ws.pause()
                elif cmd == tray_mod.CMD_RESUME:
                    await ws.resume()
                elif cmd == tray_mod.CMD_QUIT:
                    log.info("Tray: quit requested")
                    if tray is not None:
                        tray.stop()
                    shutdown_event.set()
                    return
                elif cmd == tray_mod.CMD_UNINSTALL:
                    log.info("Tray: remove-this-machine requested")
                    if tray is not None:
                        tray.stop()
                    # Notifies the platform, runs uninstall.ps1, os._exit(0).
                    await _self_uninstall_and_exit()
            except Exception:
                log.exception("tray command %r failed", cmd)

    consumer_task = (
        asyncio.create_task(_consume_tray_commands()) if tray is not None
        else None
    )

    # Run until shutdown
    connect_task = asyncio.create_task(ws.connect_forever())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [connect_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Stop the tray + its command consumer on the way down.
    if tray is not None:
        tray.stop()
    if consumer_task is not None:
        consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consumer_task

    # Shutdown: close all sessions
    logger.info(f"Closing {len(sm.sessions)} active session(s)...")
    for sid, session in list(sm.sessions.items()):
        try:
            await session.close()
        except Exception:
            logger.exception(f"Error closing session {sid}")
    sm.sessions.clear()

    # Also reap interactive PTY sessions (the -p `sessions` dict above
    # never held them) so their child processes don't survive a satellite stop.
    if getattr(sm, "pty_sessions", None):
        logger.info(f"Closing {len(sm.pty_sessions)} interactive PTY session(s)...")
        for sid, session in list(sm.pty_sessions.items()):
            try:
                await session.close()
            except Exception:
                logger.exception(f"Error closing PTY session {sid}")
        sm.pty_sessions.clear()

    # Shut down the tunnel server before cancelling tasks so subprocesses
    # holding HTTP connections see a clean close.
    try:
        await tunnel.stop()
    except Exception:
        logger.exception("Tunnel shutdown failed (continuing)")

    # Stop the otodock local control socket (PTY sessions already closed
    # above → their _on_exit fed EXIT to any attached local terminals).
    try:
        await local_ctl.stop()
    except Exception:
        logger.exception("Local control socket shutdown failed (continuing)")

    # Cancel remaining tasks
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    logger.info("Satellite shutdown complete")


def main() -> None:
    _setup_logging()
    # Wire crash diagnostics BEFORE asyncio.run so the tray thread + every
    # exit path is covered for the whole run (see _install_crash_diagnostics).
    # It runs before the boot guard too, so a crash-looping build's native-fault
    # report is surfaced/rotated on the boot it dies (both are stdlib-only and
    # cannot crash on a broken update).
    _install_crash_diagnostics()

    # Boot guard: roll back a crash-looping auto-update BEFORE importing any
    # heavy subpackage (those imports live in _main). May relaunch_self() +
    # sys.exit(0). Must run here, not inside _main, or a broken update crashes at
    # the subpackage imports before the guard ever runs.
    _check_post_update_state()

    # Process title: ``oto-dock-satellite`` instead of ``python``.
    # Writes to /proc/<pid>/comm on Linux and uses sysctl on macOS — picked
    # up by top/htop/ps. Wrapped in try/except so a missing wheel (rare —
    # all supported Pythons have prebuilt wheels) degrades gracefully to
    # the default "python" title rather than crashing startup.
    # No-op on Windows (process name there comes from the executable
    # filename — handled by runner.ps1 copying python.exe → OtoDockSatellite.exe).
    if sys.platform != "win32":
        try:
            import setproctitle  # type: ignore[import-not-found]
            setproctitle.setproctitle("oto-dock-satellite")
        except ImportError:
            pass

    parser = argparse.ArgumentParser(description="Oto Dock Satellite Daemon")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to satellite.conf (default: ~/.oto-dock/satellite.conf)",
    )
    args = parser.parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(args.config))


if __name__ == "__main__":
    main()
