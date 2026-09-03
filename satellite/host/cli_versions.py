"""Pin-and-freeze: make every spawn use the platform-pinned Claude/Codex CLI.

The proxy ships its pinned CLI versions (VERSIONS.md) in the WS ``auth_result``
(``cli_pins``). This module is the authority on WHICH binary satisfies a pin:

- ``set_pins`` records the pins synchronously at auth parse — before the WS
  message loop starts, so spawn commands (which only arrive through that loop)
  can always consult them.
- ``reconcile_cli_versions`` (off-thread, fail-open) scans candidate binaries
  and verifies ``--version`` against the pin; only when NO candidate matches
  does it npm-install. The verified binary's absolute path is recorded
  in-process.
- ``resolve_spawn_bin``/``resolve_spawn_bin_async`` are what session spawns
  call: verified path → configured hint (``satellite.conf``) → bare name. The
  async form additionally runs a fast candidate-verify on first spawn when the
  reconcile hasn't landed yet, so a session never starts on a stale binary
  merely because npm is slow.

PATH is deliberately NOT the authority: the incident class this prevents is a
stale native-installer copy shadowing the pinned one (installer shells and the
service see different PATHs, and ``satellite.conf`` used to freeze whatever
``command -v`` hit at install time). With no pin shipped, the configured hint
keeps full authority — private setups behave exactly as before.

The CLIs' own auto-update is disabled (Claude: ``autoUpdates: false`` in
settings.json + ``DISABLE_AUTOUPDATER=1`` env; Codex has no silent updater), so
the reconcile is the ONLY thing that ever changes the installed version.

Fail-open by design: any error (npm missing, registry unreachable, version
unparseable, install non-zero) is logged and swallowed — it must NEVER block
the WS connection or the session loop.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("satellite")

_VER_RE = re.compile(r"(\d+\.\d+\.\d+)")

# Per-user npm prefix the reconcile falls back to when the GLOBAL prefix
# isn't writable by the service user — the normal state on Linux with the
# NodeSource packages (prefix ``/usr``, root-owned; the satellite runs
# sudo-less). Windows/macOS npm prefixes are user-writable, so the fallback
# never fires there. ``ensure_user_npm_bin_on_path`` (called at satellite
# startup) makes binaries installed here resolvable by every spawn.
_USER_NPM_PREFIX = Path.home() / ".npm-global"

# cli_pins key -> (npm package, bin name on PATH)
_PACKAGES = {
    "claude_code": ("@anthropic-ai/claude-code", "claude"),
    "codex": ("@openai/codex", "codex"),
}
_PIN_KEY_TO_BIN = {key: bin_name for key, (_pkg, bin_name) in _PACKAGES.items()}

# Module state. Writes go through _state_lock; readers are lock-free — every
# mutation replaces a whole dict value (single item assignment, atomic under
# the GIL), never edits an entry in place.
_pins: dict[str, str] = {}          # bin name -> pinned version
_config_bins: dict[str, str] = {}   # bin name -> satellite.conf hint
_verified: dict[str, dict] = {}     # bin name -> {"path": ..., "version": ...}
_state_lock = threading.Lock()
_reconcile_lock = threading.Lock()  # single-flight reconcile passes

# Last pin map we fully reconciled — skips redundant full passes on reconnect
# storms. Only set after a clean pass so a failed install retries.
_last_reconciled: dict = {}

# Throttle for the spawn-time fast verify: when it fails (binary genuinely
# absent), don't re-pay the probe cost on every spawn.
_FAST_VERIFY_RETRY_S = 30.0
_fast_verify_attempted: dict[str, float] = {}

# What the last failed scan actually found (first candidate = what spawn
# resolution would pick): feeds the cli_status report for a pinned-but-
# unverified CLI so the platform sees the REAL version running, not a blank.
_unverified_probe: dict[str, dict] = {}


def ensure_user_npm_bin_on_path() -> None:
    """Prepend ``~/.npm-global/bin`` to this process's PATH when it exists.

    Called once at satellite startup, BEFORE any session/MCP spawn: children
    inherit the augmented PATH, so CLIs installed via the per-user fallback
    below resolve exactly like globally-installed ones. No-op when the dir
    doesn't exist or is already on PATH.
    """
    bin_dir = str(_USER_NPM_PREFIX / "bin")
    if not os.path.isdir(bin_dir):
        return
    path = os.environ.get("PATH", "")
    if bin_dir in path.split(os.pathsep):
        return
    os.environ["PATH"] = bin_dir + os.pathsep + path


def _normalize_pins(cli_pins: dict) -> dict[str, str]:
    """``{"claude_code": "x.y.z", ...}`` → ``{"claude": "x.y.z", ...}``, empties dropped."""
    out: dict[str, str] = {}
    for key, bin_name in _PIN_KEY_TO_BIN.items():
        want = str((cli_pins or {}).get(key) or "").strip()
        if want:
            out[bin_name] = want
    return out


def set_pins(cli_pins: dict, config_bins: dict[str, str] | None = None) -> None:
    """Record the platform pins (and the satellite.conf bin hints).

    Called synchronously while ``auth_result`` is parsed — the WS message loop
    (the only source of spawn commands) starts after, so resolution provably
    sees the pins from the first session of every connection.
    """
    global _pins, _config_bins
    pins = _normalize_pins(cli_pins)
    with _state_lock:
        _pins = pins
        if config_bins is not None:
            _config_bins = {k: str(v or "") for k, v in config_bins.items()}
        # A pin that disappeared hands authority back to the config hint.
        for bin_name in list(_verified):
            if bin_name not in pins:
                _verified.pop(bin_name, None)
        for bin_name in list(_unverified_probe):
            if bin_name not in pins:
                _unverified_probe.pop(bin_name, None)


def _is_windows() -> bool:
    return os.name == "nt"


def _probe_version_at(path: str) -> str:
    """Return the bare x.y.z reported by an explicit binary path, or ""."""
    if not path:
        return ""
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = _VER_RE.search(out or "")
    return m.group(1) if m else ""


def _npm_global_bin_dirs() -> list[str]:
    """Directories npm-installed shims may live in, most likely first."""
    dirs: list[str] = []
    npm = shutil.which("npm")
    if npm:
        try:
            prefix = subprocess.run(
                [npm, "prefix", "-g"], capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            prefix = ""
        if prefix:
            # POSIX puts shims under <prefix>/bin; Windows npm puts them
            # directly in the prefix dir.
            dirs.append(prefix if _is_windows() else os.path.join(prefix, "bin"))
    if _is_windows():
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            dirs.append(os.path.join(appdata, "npm"))
    else:
        dirs.append(str(_USER_NPM_PREFIX / "bin"))
    return dirs


def _candidates(bin_name: str, configured: str) -> list[str]:
    """Ordered candidate binaries for ``bin_name``, realpath-deduped, capped.

    Order encodes authority: previously verified path, then the operator/
    installer hint from satellite.conf, then the process PATH, then npm's
    global bin dirs (which may not be on the service PATH at all). Dedupe key
    is the REALPATH (usrmerge /bin↔/usr/bin, PATH duplicates) but the
    CANDIDATE path is what gets recorded — npm shim symlinks resolve into a
    version-specific package dir that breaks on the next upgrade.
    """
    ordered: list[str] = []

    entry = _verified.get(bin_name)
    if entry and entry.get("path"):
        ordered.append(entry["path"])

    cand = (configured or "").strip()
    if cand:
        if os.path.dirname(cand):
            ordered.append(cand)
        else:
            w = shutil.which(cand)
            if w:
                ordered.append(w)

    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        w = shutil.which(bin_name, path=d)
        if w:
            ordered.append(w)

    for d in _npm_global_bin_dirs():
        if _is_windows():
            # .exe before .cmd: pywinpty's CreateProcess can't launch .cmd
            # shims, so when both verify at the pin the .exe must win.
            for ext in (".exe", ".cmd"):
                p = os.path.join(d, bin_name + ext)
                if os.path.exists(p):
                    ordered.append(p)
        else:
            p = os.path.join(d, bin_name)
            if os.path.exists(p):
                ordered.append(p)

    seen: set[str] = set()
    out: list[str] = []
    for p in ordered:
        if not p or not os.path.exists(p):
            continue
        try:
            key = os.path.realpath(p)
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= 6:
            break
    return out


def _scan_for_pin(
    bin_name: str, configured: str, want: str,
) -> tuple[str, list[tuple[str, str]]]:
    """Probe candidates in authority order; return (first match, all probed)."""
    probed: list[tuple[str, str]] = []
    for path in _candidates(bin_name, configured):
        have = _probe_version_at(path)
        probed.append((path, have))
        if have == want:
            return path, probed
    return "", probed


def _record(bin_name: str, path: str, version: str) -> None:
    with _state_lock:
        _verified[bin_name] = {"path": path, "version": version}
        _unverified_probe.pop(bin_name, None)


def _note_unverified(bin_name: str, probed: list[tuple[str, str]]) -> None:
    """Remember what a failed scan found so the status report shows the real
    running version instead of a blank."""
    with _state_lock:
        _unverified_probe[bin_name] = {
            "path": probed[0][0] if probed else None,
            "version": (probed[0][1] or None) if probed else None,
        }


def _build_status_report() -> dict:
    """Per-pinned-CLI ``{"version", "path"}`` from CURRENT module state.

    Data only — never a pin-match verdict; the proxy owns the pins and
    computes mismatch itself. Built at report time (not from a reconcile
    invocation's arguments) so a long install finishing after a reconnect
    can't report superseded pins.
    """
    report: dict = {}
    for bin_name in _pins:
        entry = _verified.get(bin_name) or _unverified_probe.get(bin_name) or {}
        report[bin_name] = {
            "version": entry.get("version"),
            "path": entry.get("path"),
        }
    return report


def _fast_verify_and_record(bin_name: str, configured: str) -> str:
    """Spawn-time verify: candidate scan only, NO install. Returns path or ""."""
    want = _pins.get(bin_name, "")
    if not want:
        return ""
    path, _probed = _scan_for_pin(bin_name, configured, want)
    if path:
        _record(bin_name, path, want)
        logger.info(
            "CLI resolve: %s verified at pinned %s (%s)", bin_name, want, path,
        )
    return path


def _finalize_spawn_path(bin_name: str, path: str, for_pty: bool) -> str:
    """Windows PTY neutrality: pywinpty can't launch .cmd/.bat/.ps1 shims —
    hand the PTY layer the bare name and let its own PATHEXT resolution run
    (it prefers .exe). POSIX and headless paths take the path verbatim."""
    if (
        for_pty
        and _is_windows()
        and path.lower().endswith((".cmd", ".bat", ".ps1"))
    ):
        return bin_name
    return path


def resolve_spawn_bin(bin_name: str, configured: str, for_pty: bool = False) -> str:
    """Resolve the binary a spawn should exec, synchronously (no probing).

    Verified pin → configured hint → bare name. Bare names pass through
    ``shutil.which`` so Windows resolves the PATHEXT shim; when nothing
    resolves, the bare/configured value is returned as-is and the spawn fails
    with the same clear error as today.
    """
    entry = _verified.get(bin_name)
    if entry:
        p = entry.get("path", "")
        if p and os.path.exists(p):
            return _finalize_spawn_path(bin_name, p, for_pty)
    cand = (configured or "").strip() or bin_name
    if os.path.dirname(cand):
        return _finalize_spawn_path(bin_name, cand, for_pty)
    w = shutil.which(cand)
    if w:
        return _finalize_spawn_path(bin_name, w, for_pty)
    return cand


async def resolve_spawn_bin_async(
    bin_name: str, configured: str, for_pty: bool = False,
) -> str:
    """`resolve_spawn_bin` + a spawn-time fast verify when the pin is known
    but the reconcile hasn't recorded a binary yet (first spawn racing a slow
    ``npm install``). The verify is the candidate scan only — bounded by the
    probe timeouts, never an install — and failures are throttled so repeated
    spawns on a genuinely pinless machine don't re-pay it every time.
    """
    entry = _verified.get(bin_name)
    if (not entry) and _pins.get(bin_name):
        now = time.monotonic()
        last = _fast_verify_attempted.get(bin_name, 0.0)
        if now - last >= _FAST_VERIFY_RETRY_S:
            _fast_verify_attempted[bin_name] = now
            try:
                await asyncio.to_thread(_fast_verify_and_record, bin_name, configured)
            except Exception:
                logger.debug("spawn-time CLI verify failed", exc_info=True)
    return resolve_spawn_bin(bin_name, configured, for_pty)


def _npm_install(pkg: str, version: str) -> bool:
    """Install/upgrade an npm-global package to the EXACT version. Fail-open.

    Tries the global prefix first (works when it's user-writable: Windows,
    macOS, a preconfigured user prefix). On failure — the sudo-less service
    can't write NodeSource's root-owned ``/usr`` prefix — retries into the
    per-user ``~/.npm-global`` prefix, whose ``bin/`` is on PATH via
    ``ensure_user_npm_bin_on_path``.
    """
    npm = shutil.which("npm")
    if not npm:
        logger.warning("CLI reconcile: npm not found; cannot pin %s@%s", pkg, version)
        return False
    attempts = (
        [npm, "install", "-g", f"{pkg}@{version}"],
        [npm, "install", "-g", "--prefix", str(_USER_NPM_PREFIX),
         f"{pkg}@{version}"],
    )
    for i, cmd in enumerate(attempts):
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "CLI reconcile: npm install %s@%s failed: %s", pkg, version, e,
            )
            return False
        if r.returncode == 0:
            if i == 1:
                # PATH must cover the fallback bin dir for THIS process too —
                # the dir may not have existed at startup.
                ensure_user_npm_bin_on_path()
                logger.info(
                    "CLI reconcile: %s@%s installed to user prefix %s "
                    "(global npm prefix not writable)",
                    pkg, version, _USER_NPM_PREFIX,
                )
            return True
        logger.warning(
            "CLI reconcile: npm install %s@%s (%s) exited %d: %s",
            pkg, version,
            "global" if i == 0 else "user prefix",
            r.returncode, (r.stderr or "")[-300:],
        )
    return False


def _warn_unpinned(bin_name: str, want: str, probed: list[tuple[str, str]]) -> None:
    """One loud, self-diagnosing warning: every candidate and its version."""
    listing = "; ".join(
        f"{path} ({ver or 'unknown'})" for path, ver in probed
    ) or "none found"
    logger.warning(
        "CLI reconcile: no %s binary satisfies the pin %s — sessions will "
        "spawn the configured/PATH binary instead. Candidates probed: %s",
        bin_name, want, listing,
    )


def reconcile_cli_versions(cli_pins: dict, on_status=None) -> None:
    """Bring the spawn binaries in line with the platform pins.

    Verify-first: a candidate already at the pin is recorded with NO install
    (the common case, and the self-heal for machines whose configured path
    went stale). Installs only when nothing satisfies the pin. Single-flight —
    a pass that finds another in progress returns; the holder does the work
    (including the status report, so no-pins and lock-held returns stay
    silent).

    ``on_status`` (optional) receives ``_build_status_report()`` from the
    lock-held ``finally`` — so the unchanged-pins fast path AND a crashed
    pass still report (capabilities were just wiped at auth; a missing
    report would leave the platform blank until the next connect). Its
    exceptions are swallowed here on purpose: the trampoline raises when the
    event loop is already closed at shutdown.
    """
    global _last_reconciled
    pins = _normalize_pins(cli_pins)
    if not pins:
        return
    if not _reconcile_lock.acquire(blocking=False):
        return
    try:
        if cli_pins == _last_reconciled:
            # Fast path — but re-probe the recorded paths first: an in-place
            # replacement (manual npm install) must not keep spawning
            # unnoticed behind the short-circuit.
            drifted = False
            for bin_name, want in pins.items():
                entry = _verified.get(bin_name)
                if not entry or _probe_version_at(entry.get("path", "")) != want:
                    drifted = True
                    break
            if not drifted:
                return
        all_ok = True
        for key, (pkg, bin_name) in _PACKAGES.items():
            want = pins.get(bin_name, "")
            if not want:
                continue  # platform didn't pin this CLI → config keeps authority
            configured = _config_bins.get(bin_name, "")
            path, probed = _scan_for_pin(bin_name, configured, want)
            if path:
                _record(bin_name, path, want)
                logger.info(
                    "CLI reconcile: %s verified at pinned %s (%s)",
                    bin_name, want, path,
                )
                continue
            have = probed[0][1] if probed else "absent"
            logger.info(
                "CLI reconcile: %s %s → %s (pinning to platform version)",
                bin_name, have or "absent", want,
            )
            if _npm_install(pkg, want):
                path2, probed2 = _scan_for_pin(bin_name, configured, want)
                if path2:
                    _record(bin_name, path2, want)
                    logger.info(
                        "CLI reconcile: %s now pinned at %s (%s)",
                        bin_name, want, path2,
                    )
                    continue
                _warn_unpinned(bin_name, want, probed2)
                _note_unverified(bin_name, probed2)
            else:
                _warn_unpinned(bin_name, want, probed)
                _note_unverified(bin_name, probed)
            all_ok = False
            # A previously verified binary that no longer exists must not
            # keep winning resolution.
            entry = _verified.get(bin_name)
            if entry and not os.path.exists(entry.get("path", "")):
                with _state_lock:
                    _verified.pop(bin_name, None)
        if all_ok:
            _last_reconciled = dict(cli_pins)
    finally:
        if on_status is not None:
            try:
                on_status(_build_status_report())
            except Exception:
                logger.debug("cli_status report failed", exc_info=True)
        _reconcile_lock.release()
