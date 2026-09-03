"""Satellite self-update + self-uninstall machinery.

The free functions that tear the satellite down or replace its own code on disk,
extracted from ws_client so the WS client stays focused on the connection/dispatch
loop. ws_client calls these; __main__ and the update tests import them directly.

  * ``_self_uninstall_and_exit`` runs the platform uninstaller then ``os._exit``
    (inbound ``uninstall`` message / WS close 4006).
  * ``_self_update_and_restart`` applies a tarball update, atomic-swaps the install
    dir, and relaunches (inbound ``update_required`` / WS close 4007).
  * ``_finalize_post_update_state`` resolves the in-flight update after a successful
    auth (the post-auth counterpart to ``__main__._check_post_update_state``).
"""
import asyncio
import contextlib
import logging
import os
import time

from ..config import otodock_dir, relaunch_self, venv_exe

logger = logging.getLogger("satellite")


def _windows_schedule_detached_uninstall(script, oto_dir) -> bool:
    """Register a one-shot Scheduled Task to run uninstall.ps1 ~15s from now,
    OUTSIDE the satellite's own logon-task Job Object.

    The per-user logon Scheduled Task runs the daemon inside a Task Scheduler
    job object. A child process (a plain ``Popen`` of uninstall.ps1) lives in
    that SAME job, so when the daemon exits — or the task is stopped — Task
    Scheduler kills the whole tree, including uninstall.ps1 mid-wipe, leaving
    the OtoDock dir half-removed (the tray disappears but the folder survives).
    A SEPARATE one-shot task launches in its own fresh job, independent of the
    dying daemon, so the wipe runs to completion.
    The ~15s delay lets the daemon exit + release its venv file locks first
    (``windows_schedule_oneshot`` is second-precision and locale-safe — the
    old 70s minute-floor padding is gone).

    Returns True if the task registered; False to fall back to inline cleanup.
    """
    from ..config import windows_schedule_oneshot

    argument = (
        f'-NoProfile -ExecutionPolicy Bypass '
        f'-WindowStyle Hidden -File "{script}" -Yes -OtoDir "{oto_dir}"'
    )
    ok, detail = windows_schedule_oneshot(
        "OtoDockSatelliteUninstall", "powershell.exe", argument, delay_s=15,
    )
    if ok:
        logger.info("Scheduled one-shot self-uninstall (+15s, locale-safe)")
        return True
    logger.warning("self-uninstall one-shot create failed: %s", detail)
    return False


async def _self_uninstall_and_exit() -> None:
    """Run the platform-appropriate uninstaller and exit.

    Triggered by inbound ``{type: "uninstall"}`` WS message or close code
    4006 (``machine_deleted``).

    Layout (created by install.sh / install.ps1):
      * Linux/macOS: ``<OtoDock>/satellite/uninstall.sh``
      * Windows:     ``<OtoDock>/satellite/uninstall.ps1``

    The uninstaller is spawned **detached** so it outlives our exit and can
    delete the install dir (incl. our own running code) without racing us.
    On Unix that's ``start_new_session=True``; on Windows it's a separate
    one-shot Scheduled Task (``_windows_schedule_detached_uninstall``). The
    per-user logon task does not kill it when we exit, so the teardown survives. We
    exit cleanly (0), which the logon task does NOT auto-restart, so nothing
    respawns to race the wipe; uninstall.ps1 stops + unregisters the task
    itself. If the script is missing, falls back to a sudo-free inline
    cleanup.
    """
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    # Guard against double-invocation. BOTH the inbound `uninstall` WS message
    # AND the 4006 (machine_deleted) close fire this within the same second —
    # each would spawn its own detached uninstaller, and two uninstallers race:
    # each kills the other's process tree mid-wipe, leaving the OtoDock dir only
    # partially removed. Run the teardown once.
    if getattr(_self_uninstall_and_exit, "_started", False):
        return
    _self_uninstall_and_exit._started = True

    oto_dir = otodock_dir()
    is_windows = sys.platform == "win32"
    uninstall_script = (
        oto_dir / "satellite" / ("uninstall.ps1" if is_windows else "uninstall.sh")
    )

    script_spawned = False
    try:
        if uninstall_script.exists():
            if is_windows:
                # Register a one-shot Scheduled Task instead of spawning a
                # child: a child lives inside the daemon's logon-task Job
                # Object and gets killed when the daemon exits, leaving the
                # OtoDock dir half-wiped. The one-shot task runs in its own
                # fresh job ~15s later, after we've exited + released locks.
                logger.info("Scheduling %s -Yes via Task Scheduler",
                            uninstall_script)
                script_spawned = _windows_schedule_detached_uninstall(
                    uninstall_script, oto_dir,
                )
            else:
                logger.info("Running %s --yes", uninstall_script)
                # Spawn in a SEPARATE cgroup via systemd-run so the uninstaller
                # survives `systemctl --user stop oto-dock-satellite` (which the
                # script itself runs). start_new_session alone only makes a new
                # session — the child stays in the satellite unit's cgroup, so
                # stopping the unit (KillMode=control-group) kills the
                # uninstaller mid-`rm -rf`, leaving ~/.oto-dock half-removed (the
                # Linux twin of the Windows self-kill race). `--collect` GCs the
                # transient unit when it exits. Falls back to plain detached
                # bash on non-systemd hosts + macOS (no cgroup, so no race).
                env = {
                    **os.environ,
                    "XDG_RUNTIME_DIR": os.environ.get(
                        "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
                    ),
                }
                spawned = False
                if sys.platform == "linux" and shutil.which("systemd-run"):
                    try:
                        subprocess.Popen(
                            ["systemd-run", "--user", "--collect",
                             "bash", str(uninstall_script), "--yes"],
                            cwd="/tmp", env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        spawned = True
                    except Exception:
                        logger.warning(
                            "systemd-run spawn failed; falling back to bash",
                            exc_info=True,
                        )
                if not spawned:
                    # start_new_session detaches from our cwd so the script can
                    # remove the OtoDock dir without rmdir failing.
                    subprocess.Popen(
                        ["bash", str(uninstall_script), "--yes"],
                        cwd="/tmp",
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                script_spawned = True
        if not script_spawned:
            if is_windows:
                # Inline fallback (uninstall.ps1 missing). Best-effort: delete
                # the logon task so it won't restart, then wipe what we can.
                # The running venv is locked, so the dir may not fully clear;
                # the rare missing-script case accepts that.
                logger.info("uninstall.ps1 missing, doing inline cleanup")
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["schtasks", "/Delete", "/TN", "OtoDockSatellite", "/F"],
                        timeout=15, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False,
                    )
                if oto_dir.exists():
                    shutil.rmtree(oto_dir, ignore_errors=True)
            else:
                # Inline fallback — sudo-free, user scope. Set XDG_RUNTIME_DIR
                # defensively so `systemctl --user` finds the session bus from
                # this detached context.
                logger.info("uninstall.sh missing, doing inline cleanup")
                env = {
                    **os.environ,
                    "XDG_RUNTIME_DIR": os.environ.get(
                        "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
                    ),
                }
                unit = (Path.home() / ".config/systemd/user"
                        / "oto-dock-satellite.service")
                for cmd in (
                    ["systemctl", "--user", "stop", "oto-dock-satellite"],
                    ["systemctl", "--user", "disable", "oto-dock-satellite"],
                ):
                    with contextlib.suppress(Exception):
                        subprocess.run(
                            cmd, env=env, timeout=15,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False,
                        )
                with contextlib.suppress(OSError):
                    unit.unlink()
                if oto_dir.exists():
                    shutil.rmtree(oto_dir, ignore_errors=True)
    except Exception:
        logger.exception("Self-uninstall failed (continuing to exit)")

    finally:
        # Give the spawned uninstall a moment to start, then exit. Use
        # ``os._exit`` (not ``sys.exit``) so asyncio doesn't catch the
        # SystemExit at the task layer and log "Task exception was never
        # retrieved" noise — this code path INTENDS to terminate the
        # process; cooperative asyncio shutdown isn't useful here.
        await asyncio.sleep(1.0)
        os._exit(0)


def _resolve_base_python() -> str:
    """Path to the BASE (non-venv) Python interpreter.

    Used to REBUILD the satellite venv during a Unix update AFTER the dir
    swap. The swap moves our running venv out of ``sat_root/venv`` (to
    ``satellite.previous/``), so ``sys.executable`` — which points INTO that
    venv (``…/satellite/venv/bin/python``) — no longer exists on disk and
    ``subprocess.run([sys.executable, "-m", "venv", …])`` dies with
    ``FileNotFoundError``, leaving ``sat_root`` with no venv (a hard brick that
    only a reinstall recovers). The base interpreter lives OUTSIDE the venv and
    survives the swap.

    Resolution prefers ``sys.base_prefix`` — which, inside a venv, is
    DEFINITIONALLY the base install (``≠ sys.prefix``), so the interpreter
    under it is never the venv we're replacing. We deliberately do NOT trust
    ``sys._base_executable`` first: for copied / venv-of-venv interpreters it
    can point right back at the venv python (observed locally). The last
    resort is the bare name (resolved via PATH at exec time) — NEVER
    ``sys.executable``, which is the moved-away path itself.
    """
    import os
    import shutil
    import sys

    if sys.platform == "win32":
        # Base python sits at the prefix root on Windows (Scripts/ is venv-only).
        cand = os.path.join(sys.base_prefix, "python.exe")
        name = "python.exe"
    else:
        cand = os.path.join(sys.base_prefix, "bin", "python3")
        name = "python3"
    if os.path.exists(cand):
        return cand
    for nm in ("python3", "python"):
        w = shutil.which(nm)
        if w:
            return w
    # _base_executable only if it's genuinely a DIFFERENT interpreter than the
    # (about-to-be-moved) venv one.
    be = getattr(sys, "_base_executable", "") or ""
    if be and os.path.exists(be) and os.path.realpath(be) != os.path.realpath(sys.executable):
        return be
    return name


def _reap_own_children_before_restart() -> None:
    """Kill the satellite's entire child tree before an update-restart exit.

    ``os._exit``/``sys.exit`` do NOT reap children, so the CLI/Codex sessions,
    their MCP subprocesses, and hook processes would be ORPHANED across the
    restart — they keep running AND, on Windows, keep a handle on ``satellite\\``
    (hooks run as ``OtoDockSatellite.exe`` from ``satellite\\venv\\Scripts``).
    That handle blocks runner.ps1's swap (``Move-Item satellite ->
    satellite.previous`` fails the whole retry window) so the OLD code silently
    keeps running and the update never applies. Reaping here releases the lock
    so the swap succeeds. Same orphan-lock class as the 0.5.28 MCP-update fix,
    here in the update-restart path. Best-effort + bounded; never raises into
    the exit path."""
    try:
        from ..config import reap_descendants, snapshot_descendants
        procs = snapshot_descendants(os.getpid())
        if procs:
            logger.info(
                "Reaping %d child process(es) before update-restart", len(procs),
            )
            reap_descendants(procs, timeout=5.0)
    except Exception:
        logger.warning(
            "pre-restart child reap failed (continuing to exit)", exc_info=True,
        )


def _finalize_post_update_state(oto_dir, running_version: str) -> None:
    """Resolve any in-flight update after a successful auth. Stamps
    ``.last_successful_boot`` (the boot guard's success signal), then:

      * No ``.update_in_progress`` marker → a normal boot; nothing to finalize.
      * Marker target == ``running_version`` → the swap applied and the new code
        is on the wire → finalize: drop the marker + the rollback copy.
      * Marker target != ``running_version`` → the runner.ps1 swap SILENTLY
        FAILED (we are still the OLD code). Log LOUD + leave a ``.update-failed``
        breadcrumb + KEEP ``satellite.previous``; clear the in-progress marker so
        the boot guard doesn't pointlessly "roll back" identical old code. The
        proxy re-offers the update on the next reconnect (the child-reap fix
        should let the swap succeed next time). This replaces the old behavior
        that logged "Update finalized" on ANY auth — which masked a silent
        stuck-update loop (the exact failure seen on a slow Windows laptop).

    Best-effort; never raises."""
    import shutil
    import time
    with contextlib.suppress(OSError):
        # Record the running VERSION (line 1) + a timestamp (line 2). The boot
        # guard compares the VERSION (not the file mtime) to decide whether an
        # update's target already authed — a clock-independent signal, immune to
        # a backward clock jump (CMOS reset / NTP step-back / dual-boot RTC / VM
        # snapshot) that an mtime comparison would misread and could then drop a
        # live update's rollback copy.
        (oto_dir / ".last_successful_boot").write_text(
            f"{running_version}\n{time.time()}\n"
        )
    marker = oto_dir / ".update_in_progress"
    sat_prev = oto_dir / "satellite.previous"
    if marker.exists():
        target_v = ""
        try:
            parts = marker.read_text().strip().split("\n")
            target_v = parts[1] if len(parts) > 1 else ""
        except OSError:
            pass
        if target_v and target_v != running_version:
            logger.error(
                "Update to %s did NOT apply — still running %s. The staged "
                "runner.ps1 swap was blocked (see runner.log); clearing the "
                "in-progress marker — the proxy will re-offer the update.",
                target_v, running_version,
            )
            with contextlib.suppress(OSError):
                (oto_dir / ".update-failed").write_text(
                    f"{running_version}\n{target_v}\n"
                )
            with contextlib.suppress(OSError):
                marker.unlink()
            return  # KEEP satellite.previous (the rollback copy) on a failed swap
        # Target matches (or unknown/legacy) → genuine finalize.
        with contextlib.suppress(OSError):
            marker.unlink()
        with contextlib.suppress(OSError):
            (oto_dir / ".update-failed").unlink()  # clear any prior failure crumb
        logger.info("Update finalized — now running %s", running_version)
    # No marker, or a real success → clean any stale rollback copy.
    if sat_prev.exists():
        shutil.rmtree(sat_prev, ignore_errors=True)


async def _self_update_and_restart(payload: dict) -> None:
    """Apply a tarball update from the proxy and restart via systemd.

    Mirrors the atomic-swap pattern used by session_manager.sync_mcps for
    MCPs: extract to a `.new/` dir, swap with the live dir, then exit and
    let `Restart=always` in the systemd unit respawn us with the new code.

    Rollback safety:
      * The previous satellite dir is kept at ``satellite.previous/`` until
        the new code authenticates with the proxy successfully.
      * A marker file ``~/.oto-dock/.update_in_progress`` is written before
        the swap. On boot, ``__main__.py::_check_post_update_state`` reads
        it: if the new code crashes before authenticating, we swap back.
      * The proxy sends ``expected_sha256``; we verify before applying.

    Restart trigger is platform-specific (centralized in
    ``config.relaunch_self``): Linux simply exits and lets the systemd
    *user* unit's ``Restart=always`` respawn us (no sudo, no privileged
    call); macOS uses ``launchctl kickstart``; Windows stages the swap for
    ``runner.ps1`` and relaunches via the logon task (``schtasks /Run``).
    """
    import base64 as _b64
    import hashlib
    import io
    import os
    import shutil
    import subprocess
    import sys
    import tarfile

    target_version = payload.get("target_version", "")
    tarball_b64 = payload.get("tarball_b64", "")
    expected_sha256 = payload.get("expected_sha256", "")
    previous_version = payload.get("previous_version", "")

    oto_dir = otodock_dir()
    sat_root = oto_dir / "satellite"
    sat_new = oto_dir / "satellite.new"
    sat_prev = oto_dir / "satellite.previous"
    marker = oto_dir / ".update_in_progress"

    try:
        if not tarball_b64 or not expected_sha256:
            raise ValueError("missing tarball_b64 or expected_sha256")

        tarball_bytes = _b64.b64decode(tarball_b64)
        actual_sha = hashlib.sha256(tarball_bytes).hexdigest()
        if actual_sha != expected_sha256:
            raise ValueError(
                f"hash mismatch: expected={expected_sha256} actual={actual_sha}"
            )

        # 1. Marker — boot guard reads this on next start.
        #    Format: <previous_version>\n<target_version>\n<attempt_count>\n
        marker.write_text(f"{previous_version}\n{target_version}\n0\n")

        # 2. Clean & extract to .new/
        if sat_new.exists():
            shutil.rmtree(sat_new, ignore_errors=True)
        sat_new.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            for member in tar.getmembers():
                mpath = (sat_new / member.name).resolve()
                if not str(mpath).startswith(str(sat_new.resolve())):
                    raise ValueError(f"tarball escaped: {member.name}")
            tar.extractall(sat_new, filter="data")

        # 3. Stage the venv into satellite.new/. The "reuse when reqs match"
        #    optimization is Unix-only — Windows can't safely `shutil.move`
        #    the venv because the running OtoDockSatellite.exe lives inside
        #    venv/Scripts/ and Windows refuses to rename a dir whose files
        #    are open. On Windows we COPY instead, which works because
        #    Python's open() reads the running .exe with FILE_SHARE_READ.
        old_venv = sat_root / "venv"
        new_venv_target = sat_new / "venv"
        old_req_path = sat_root / "requirements.txt"
        new_req_path = sat_new / "requirements.txt"
        old_req = old_req_path.read_text() if old_req_path.is_file() else ""
        new_req = new_req_path.read_text() if new_req_path.is_file() else ""
        reuse_venv = old_venv.is_dir() and old_req == new_req

        if sys.platform == "win32":
            # --- Windows path: stage everything into sat_new/, let runner.ps1
            # do the rename on the next logon-task start (pre-import, so no
            # files are open yet). The in-process os.replace below would fail
            # with WinError 32 the moment a single .pyd/.exe inside the dir is
            # held by the running process. ---
            if reuse_venv:
                # Copy (not move) so the running interpreter keeps working.
                # The pip/distlib .exe wrappers in venv/Scripts/ have their
                # python.exe path embedded as `<sat_root>/venv/Scripts/...` —
                # after runner.ps1 moves sat_new → sat_root, that path
                # resolves back to the same final location, so the wrappers
                # remain correct. Retry the copytree: Defender can briefly
                # lock a freshly-written venv file mid-copy, and a bare
                # copytree would abort the whole update into the silent
                # error-return path below (the 0.4.27 brick class).
                for _attempt in range(12):
                    try:
                        shutil.copytree(str(old_venv), str(new_venv_target))
                        break
                    except (PermissionError, OSError):
                        if _attempt == 11:
                            raise
                        # copytree refuses a pre-existing dest, so clear any
                        # partial copy before retrying.
                        if new_venv_target.exists():
                            shutil.rmtree(new_venv_target, ignore_errors=True)
                        time.sleep(0.5)
            else:
                # Fresh venv in sat_new/. Creating + installing in a side
                # dir never touches the running interpreter's files.
                new_py = str(venv_exe(new_venv_target, "python"))
                try:
                    subprocess.run(
                        [sys.executable, "-m", "venv", str(new_venv_target)],
                        check=True,
                    )
                    # Upgrade pip via `python -m pip`, NOT `pip.exe`: on Windows
                    # `pip.exe install --upgrade pip` can't overwrite its own
                    # running executable (Access is denied → exit 1), which used
                    # to ABORT the whole update. Best-effort — the venv's bundled
                    # ensurepip pip already installs requirements fine, so a
                    # failed self-upgrade must NOT fail the update.
                    _r = subprocess.run(
                        [new_py, "-m", "pip", "install", "-q", "--upgrade", "pip"],
                    )
                    if _r.returncode != 0:
                        logger.warning(
                            "Update: pip self-upgrade failed (rc=%s) — "
                            "continuing with the bundled pip", _r.returncode,
                        )
                    subprocess.run(
                        [new_py, "-m", "pip", "install",
                         "-q", "-r", str(new_req_path)],
                        check=True,
                    )
                except subprocess.CalledProcessError as e:
                    logger.error("Update venv install failed: %s — aborting", e)
                    shutil.rmtree(sat_new, ignore_errors=True)
                    with contextlib.suppress(OSError):
                        marker.unlink()
                    return

            # Signal runner.ps1 to do the swap on next start. The name +
            # format here must match runner.ps1's marker contract.
            pending_marker = oto_dir / ".update-pending"
            pending_marker.write_text(f"{previous_version}\n{target_version}\n")

            logger.info(
                "Update staged for runner.ps1 swap — relaunching via logon task"
            )
            # A clean exit-0 is NOT auto-restarted by the logon task, so
            # relaunch explicitly (one-shot Scheduled Task fires after we exit).
            relaunch_self()
            await asyncio.sleep(1.0)
            # Reap our child tree FIRST so no orphaned CLI/MCP/hook process keeps
            # a handle on satellite\ and blocks runner.ps1's swap (the orphan-lock
            # that silently left the OLD code running). See
            # _reap_own_children_before_restart.
            _reap_own_children_before_restart()
            # os._exit (not sys.exit) so the intentional update-restart doesn't
            # surface as a noisy "Task exception was never retrieved: SystemExit"
            # through the asyncio task — the relaunch task respawns us cleanly.
            os._exit(0)

        # --- Unix path: in-process swap. Renaming a dir whose files are
        # open is fine on Linux/macOS because the kernel tracks files by
        # inode, not path — the running process keeps reading via its open
        # FD even after the dir moves. ---
        if reuse_venv:
            shutil.move(str(old_venv), str(new_venv_target))

        # 4. Atomic swap.
        if sat_prev.exists():
            shutil.rmtree(sat_prev, ignore_errors=True)
        os.replace(str(sat_root), str(sat_prev))
        os.replace(str(sat_new), str(sat_root))

        # 5. (Re)install deps when requirements changed. Build with the BASE
        #    interpreter, NOT sys.executable: the swap above moved our running
        #    venv to satellite.previous/, so sys.executable's path
        #    (sat_root/venv/bin/python) no longer exists — `python -m venv`
        #    would die with FileNotFoundError and leave sat_root with NO venv
        #    (the satellite then can't restart; systemd's ExecStart is that
        #    same dead path → a hard brick only a reinstall recovers). The base
        #    interpreter lives outside ~/.oto-dock and survives the swap. Fail =
        #    roll back here (still in this process), don't restart with a broken
        #    venv that systemd would then crash-loop on.
        if not reuse_venv:
            base_python = _resolve_base_python()
            new_py = str(venv_exe(sat_root / "venv", "python"))
            try:
                subprocess.run(
                    [base_python, "-m", "venv", str(sat_root / "venv")],
                    check=True,
                )
                # Best-effort pip self-upgrade via `python -m pip` (see the
                # Windows path) — a failure here must not abort the update; the
                # venv's bundled pip installs requirements fine.
                _r = subprocess.run(
                    [new_py, "-m", "pip", "install", "-q", "--upgrade", "pip"],
                )
                if _r.returncode != 0:
                    logger.warning(
                        "Update: pip self-upgrade failed (rc=%s) — "
                        "continuing with the bundled pip", _r.returncode,
                    )
                subprocess.run(
                    [new_py, "-m", "pip", "install",
                     "-q", "-r", str(sat_root / "requirements.txt")],
                    check=True,
                )
            except (subprocess.CalledProcessError, OSError) as e:
                # Roll back in-process — the swap already happened. OSError
                # (covers FileNotFoundError — a missing/again-wrong interpreter)
                # is caught too, so a failed rebuild RESTORES the old working
                # venv instead of leaving the satellite bricked.
                logger.error("Update venv install failed: %s — rolling back", e)
                if sat_prev.exists():
                    if sat_root.exists():
                        shutil.rmtree(sat_root, ignore_errors=True)
                    os.replace(str(sat_prev), str(sat_root))
                with contextlib.suppress(OSError):
                    marker.unlink()
                return

        # 6. Trigger service-manager restart and exit. The boot guard in
        #    the new code will verify auth within 60s and finalize (or
        #    roll back). relaunch_self() is a no-op on Linux (the user
        #    unit's Restart=always respawns us the instant we exit) and
        #    launchctl-kickstarts the LaunchAgent on macOS.
        logger.info(
            "Update extracted, swap done — triggering service-manager restart"
        )
        relaunch_self()
        await asyncio.sleep(1.0)
        # Reap orphaned children before exit (see Windows path). On Unix the
        # rename swap already succeeded so this isn't a lock fix, but it still
        # prevents leaking the old session's CLI/MCP children as orphans.
        _reap_own_children_before_restart()
        sys.exit(0)
    except Exception as e:
        logger.exception("self-update failed: %s", e)
        # Best-effort cleanup of partial state. Don't restart — let the
        # current (old) version keep running. The proxy will retry the
        # update push on the next reconnect.
        try:
            if sat_new.exists():
                shutil.rmtree(sat_new, ignore_errors=True)
        except Exception:
            pass
        with contextlib.suppress(OSError):
            marker.unlink()
