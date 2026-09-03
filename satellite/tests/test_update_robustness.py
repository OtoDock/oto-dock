"""Auto-update robustness.

`_reap_own_children_before_restart` (lifecycle_update) kills the satellite's child
tree before an update-restart exit, so no orphaned hook/CLI child keeps a
handle on ``satellite\\`` and blocks runner.ps1's swap (the silent "updates but
stays on the old version forever" loop). `_finalize_post_update_state`
(ws_client) is version-aware: it declares an update "finalized" ONLY when the
running version matches the marker's target, otherwise it surfaces the failed
swap instead of looping. `_check_post_update_state` (__main__) is the boot-time
crash-loop rollback guard.
"""

from pathlib import Path

import pytest

from satellite import __main__ as sat_main
from satellite.transport import lifecycle_update


# --- child reap (the actual swap-unlock fix) -------------------------------

def test_reap_never_raises_on_error(monkeypatch):
    def boom(pid):
        raise RuntimeError("psutil exploded")
    monkeypatch.setattr("satellite.config.snapshot_descendants", boom)
    # Runs right before os._exit — a raise must NEVER derail the exit path.
    lifecycle_update._reap_own_children_before_restart()


def test_reap_noop_when_no_children(monkeypatch):
    reaped = {}
    monkeypatch.setattr("satellite.config.snapshot_descendants", lambda pid: [])
    monkeypatch.setattr(
        "satellite.config.reap_descendants",
        lambda procs, timeout=5.0: reaped.setdefault("called", True),
    )
    lifecycle_update._reap_own_children_before_restart()
    assert "called" not in reaped  # empty snapshot → reap skipped


def test_reap_reaps_snapshotted_children(monkeypatch):
    sentinel = [object(), object()]
    seen = {}
    monkeypatch.setattr("satellite.config.snapshot_descendants", lambda pid: sentinel)
    monkeypatch.setattr(
        "satellite.config.reap_descendants",
        lambda procs, timeout=5.0: seen.update(procs=procs, timeout=timeout),
    )
    lifecycle_update._reap_own_children_before_restart()
    assert seen["procs"] is sentinel
    assert seen["timeout"] == 5.0


# --- version-aware finalize ------------------------------------------------

def _marker(oto: Path, *, target: str, prev: str = "0.5.27", attempts: int = 1):
    (oto / ".update_in_progress").write_text(f"{prev}\n{target}\n{attempts}\n")


def test_finalize_success_when_version_matches(tmp_path):
    _marker(tmp_path, target="0.5.31")
    (tmp_path / "satellite.previous").mkdir()
    (tmp_path / ".update-failed").write_text("stale crumb from a prior failure")

    lifecycle_update._finalize_post_update_state(tmp_path, "0.5.31")

    assert not (tmp_path / ".update_in_progress").exists()   # finalized
    assert not (tmp_path / "satellite.previous").exists()    # rollback copy dropped
    assert not (tmp_path / ".update-failed").exists()        # prior crumb cleared
    assert (tmp_path / ".last_successful_boot").exists()
    # The stamp records the running VERSION on line 1 — the boot guard's
    # clock-independent success signal.
    assert (tmp_path / ".last_successful_boot").read_text().splitlines()[0] == "0.5.31"


def test_finalize_surfaces_failed_swap_on_version_mismatch(tmp_path):
    _marker(tmp_path, target="0.5.31")                # aimed for 0.5.31 …
    (tmp_path / "satellite.previous").mkdir()

    lifecycle_update._finalize_post_update_state(tmp_path, "0.5.27")   # … but still 0.5.27

    assert not (tmp_path / ".update_in_progress").exists()   # marker cleared (no pointless rollback)
    assert (tmp_path / ".update-failed").exists()            # loud breadcrumb written
    crumb = (tmp_path / ".update-failed").read_text().splitlines()
    assert crumb[0] == "0.5.27" and crumb[1] == "0.5.31"
    assert (tmp_path / "satellite.previous").exists()        # rollback copy KEPT
    assert (tmp_path / ".last_successful_boot").exists()


def test_finalize_no_marker_is_clean_boot(tmp_path):
    (tmp_path / "satellite.previous").mkdir()  # a stale leftover from a past update
    lifecycle_update._finalize_post_update_state(tmp_path, "0.5.31")
    assert (tmp_path / ".last_successful_boot").exists()
    assert not (tmp_path / "satellite.previous").exists()   # cleaned on a normal boot
    assert not (tmp_path / ".update-failed").exists()


# --- boot-guard crash-loop rollback (__main__._check_post_update_state) -----

def test_boot_guard_rollback_swaps_and_relaunches(tmp_path, monkeypatch):
    """attempts>=2 without ever authing → roll back: swap satellite.previous
    onto satellite, drop the marker, exit(0). Exercises the REAL config helpers
    (force_rmtree / atomic_replace / relaunch_self) deliberately — they were
    called here but never imported into __main__, so every rollback branch
    NameError'd. Forces the Unix path (relaunch_self is a no-op on Linux)."""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    prev = tmp_path / "satellite.previous"
    prev.mkdir()
    (prev / "VERSION").write_text("rolled-back")             # proves the swap ran
    (tmp_path / ".update_in_progress").write_text("0.5.27\n0.5.64\n2\n")  # attempts=2
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    with pytest.raises(SystemExit) as exc:
        sat_main._check_post_update_state()
    assert exc.value.code == 0

    assert not prev.exists()                                 # previous swapped away
    assert (tmp_path / "satellite" / "VERSION").read_text() == "rolled-back"
    assert not (tmp_path / ".update_in_progress").exists()   # marker cleared


def test_boot_guard_increments_attempts_before_giving_up(tmp_path, monkeypatch):
    """attempts<2 → no rollback yet; just bump the counter and let the new code
    try to auth. (Guards the other side of the >=2 branch.)"""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    (tmp_path / "satellite.previous").mkdir()
    (tmp_path / ".update_in_progress").write_text("0.5.27\n0.5.64\n0\n")  # attempts=0
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    sat_main._check_post_update_state()                      # returns, no exit

    assert (tmp_path / "satellite.previous").exists()        # NOT rolled back
    body = (tmp_path / ".update_in_progress").read_text().split("\n")
    assert body[2] == "1"                                    # counter incremented


# --- Fix A: the guard must be reachable on a broken update -------------------

def test_main_top_level_imports_stay_leaf_only():
    """The boot guard runs from main() BEFORE the heavy subpackage imports
    (which live in _main). That only protects against an import-crash if
    __main__ imports nothing heavier than `.config` at MODULE TOP — a
    `from .transport… import` up there would crash before the guard runs and
    re-brick the exact way the subpackage split did. Enforce it via AST so the
    invariant can't silently rot on a future 'tidy the imports' refactor."""
    import ast
    from pathlib import Path as _P

    tree = ast.parse(_P(sat_main.__file__).read_text())
    heavy = {"transport", "sessions", "terminal", "host", "_vendored"}
    offenders = [
        ast.dump(n)
        for n in tree.body  # module level only — function-local imports are nested
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in heavy
    ]
    assert not offenders, f"heavy subpackage import at __main__ module top: {offenders}"


def test_main_does_not_bind_heavy_classes_at_module_scope():
    """Companion to the AST check: the deferred classes must not leak back as
    __main__ module attributes (which would mean a module-top import sneaked
    in)."""
    for name in (
        "LocalTunnelServer", "LocalControlServer", "SessionManager", "SatelliteWSClient",
    ):
        assert not hasattr(sat_main, name), f"{name} leaked to __main__ module scope"


# --- Fix B: rollback must restore a RUNNABLE tree (carry the venv) -----------

def test_boot_guard_rollback_carries_venv_into_previous(tmp_path, monkeypatch):
    """The reuse-venv update path MOVES the venv into the new build, leaving
    satellite.previous venv-less. On rollback the guard must carry the venv back
    from the crash-looping build, else the restored code has no interpreter and
    systemd's ExecStart dies 203/EXEC (the second bug that re-bricked the box)."""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    sat = tmp_path / "satellite"
    sat.mkdir()
    (sat / "VERSION").write_text("broken-new")
    venv = sat / "venv"                       # the ONLY venv lives in the broken build
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    prev = tmp_path / "satellite.previous"
    prev.mkdir()
    (prev / "VERSION").write_text("old-good")  # ...and prev has NO venv
    (tmp_path / ".update_in_progress").write_text("0.5.64\n0.5.67\n2\n")  # attempts=2
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    with pytest.raises(SystemExit) as exc:
        sat_main._check_post_update_state()
    assert exc.value.code == 0

    restored = tmp_path / "satellite"
    assert (restored / "VERSION").read_text() == "old-good"          # prev swapped in
    assert (restored / "venv" / "pyvenv.cfg").exists()               # venv CARRIED back
    assert not prev.exists()                                         # previous consumed
    assert not (tmp_path / ".update_in_progress").exists()           # marker cleared


def test_boot_guard_keeps_marker_when_rollback_swap_fails(tmp_path, monkeypatch):
    """A failed rollback swap must NOT clear the marker or relaunch — otherwise
    the next boot sees no marker and silently crash-loops the broken build. The
    failure has to retry, not give up quietly."""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    (tmp_path / "satellite.previous").mkdir()
    (tmp_path / ".update_in_progress").write_text("0.5.64\n0.5.67\n2\n")
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    def boom(*a, **k):
        raise OSError("swap blocked")
    monkeypatch.setattr(sat_main, "atomic_replace", boom)

    sat_main._check_post_update_state()                      # returns, NO SystemExit

    assert (tmp_path / ".update_in_progress").exists()       # marker PRESERVED for retry
    assert (tmp_path / ".update_in_progress").read_text().split("\n")[2] == "2"


def test_boot_guard_loud_when_no_previous_to_roll_back_to(tmp_path, monkeypatch):
    """attempts>=2 but satellite.previous is gone (e.g. a prior successful update
    dropped it) → fail loud + KEEP the marker; do not relaunch into the broken
    build pretending success."""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    # NO satellite.previous on disk
    (tmp_path / ".update_in_progress").write_text("0.5.64\n0.5.67\n2\n")
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    sat_main._check_post_update_state()                      # returns, NO exit/relaunch

    assert (tmp_path / ".update_in_progress").exists()       # marker kept (loud, not silent)


# --- stale-marker detection is VERSION-based, not clock-based ----------------

def test_boot_guard_stale_marker_cleared_when_target_already_authed(tmp_path, monkeypatch):
    """If .last_successful_boot records the marker's TARGET version, the new code
    already authed → the marker is stale (finalize's unlink must have failed).
    Clean it up + drop satellite.previous; never roll back working code — even
    with a high attempt count (the version signal wins over the counter)."""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    prev = tmp_path / "satellite.previous"
    prev.mkdir()
    (tmp_path / ".update_in_progress").write_text("0.5.66\n0.5.68\n2\n")  # attempts=2
    (tmp_path / ".last_successful_boot").write_text("0.5.68\n1750000000.0\n")  # target authed
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    sat_main._check_post_update_state()                      # returns, NO rollback

    assert not (tmp_path / ".update_in_progress").exists()   # stale marker cleared
    assert not prev.exists()                                 # rollback copy dropped
    assert (tmp_path / "satellite").exists()                 # current tree untouched


def test_boot_guard_stale_check_is_clock_independent(tmp_path, monkeypatch):
    """The whole point of the version check: a .last_successful_boot with a
    NEWER mtime than the marker must NOT look stale on its own — only a VERSION
    match does. This is the backward-clock-jump shape (CMOS reset / NTP step /
    VM snapshot) that the old mtime comparison misread, dropping a LIVE update's
    rollback copy. Here the stamp is a DIFFERENT version but far-future mtime."""
    import os
    import time as _t
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    (tmp_path / "satellite.previous").mkdir()
    marker = tmp_path / ".update_in_progress"
    marker.write_text("0.5.66\n0.5.68\n0\n")                 # in-flight update to 0.5.68
    last_ok = tmp_path / ".last_successful_boot"
    last_ok.write_text("0.5.66\n1750000000.0\n")             # OLD version on line 1...
    future = _t.time() + 100_000
    os.utime(last_ok, (future, future))                      # ...but a far-FUTURE mtime
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    sat_main._check_post_update_state()                      # must NOT treat as stale

    assert marker.exists()                                   # rollback path preserved
    assert (tmp_path / "satellite.previous").exists()        # copy NOT dropped
    assert marker.read_text().split("\n")[2] == "1"          # fell through to increment


def test_boot_guard_legacy_timestamp_stamp_is_not_stale(tmp_path, monkeypatch):
    """A pre-0.5.68 .last_successful_boot is a bare timestamp (no version on
    line 1), so it never matches a target version → the guard safely falls
    through to the attempts path. Backward compatible across the upgrade."""
    monkeypatch.setattr(sat_main.sys, "platform", "linux")
    (tmp_path / "satellite").mkdir()
    (tmp_path / "satellite.previous").mkdir()
    (tmp_path / ".update_in_progress").write_text("0.5.66\n0.5.68\n0\n")
    (tmp_path / ".last_successful_boot").write_text("1750000000.123")  # legacy bare ts
    monkeypatch.setattr(sat_main, "otodock_dir", lambda: tmp_path)

    sat_main._check_post_update_state()                      # not stale → increment

    assert (tmp_path / ".update_in_progress").exists()
    assert (tmp_path / ".update_in_progress").read_text().split("\n")[2] == "1"
