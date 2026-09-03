"""Tests for the process-tree reap helpers used by session teardown.

These guard the fix for the Windows MCP-update lock: a session's MCP children
must be reaped on ``close()`` so a still-running ``.exe``/``.pyd`` doesn't keep
its venv directory locked and block the next MCP-update swap (``WinError 5``).

The mechanism — snapshot the child tree while the parent is alive, then reap the
survivors after the parent exits (orphaning them) — is validated here with real
processes, so it exercises the actual cross-platform behavior, not a mock.
"""

import contextlib
import subprocess
import sys
import time

import psutil

from satellite.config import reap_descendants, snapshot_descendants

# A parent that spawns a long-lived child, prints the child's PID, then sleeps.
# Mirrors a CLI / app-server daemon (parent) that spawned an MCP subprocess
# (child) which Windows does NOT auto-kill when the parent exits.
_PARENT_SRC = (
    "import subprocess, sys, time; "
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
    "print(c.pid, flush=True); "
    "time.sleep(120)"
)


def _alive(pid: int) -> bool:
    """True only if ``pid`` is a live, non-zombie process."""
    if not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_snapshot_then_reap_kills_orphaned_child():
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT_SRC],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline().strip())

        # Snapshot the descendant tree WHILE the parent is alive — this is what
        # close() does before letting the CLI/daemon exit.
        descendants = snapshot_descendants(parent.pid)
        assert child_pid in {p.pid for p in descendants}

        # Parent exits → the child is orphaned. This is exactly the leak the fix
        # targets: terminating only the parent leaves the MCP child running.
        parent.terminate()
        parent.wait(timeout=10)
        assert _alive(child_pid), "child should outlive the parent (the leak)"

        # Reap the snapshot → the orphaned child is killed.
        reap_descendants(descendants, timeout=10.0)
        deadline = time.time() + 5
        while time.time() < deadline and _alive(child_pid):
            time.sleep(0.05)
        assert not _alive(child_pid), "reap_descendants must kill the orphaned child"
    finally:
        for pid in (child_pid, parent.pid):
            if pid is None:
                continue
            with contextlib.suppress(psutil.Error):
                psutil.Process(pid).kill()
        with contextlib.suppress(Exception):
            parent.wait(timeout=5)


def test_snapshot_descendants_missing_pid_returns_empty():
    # A pid past the max → psutil.NoSuchProcess → [] (never raises).
    assert snapshot_descendants(2_000_000_000) == []


def test_reap_descendants_empty_is_noop():
    # Must not raise and must not even import psutil work on an empty list.
    reap_descendants([], timeout=1.0)


def test_is_transient_lock_detection():
    """The install-retry gate: recognize AV/scanner file-lock output, not real
    failures."""
    from satellite.sessions.mcp_install_support import _is_transient_lock

    # Real Windows AV-lock signatures seen from uv/pip:
    assert _is_transient_lock(
        "error: failed to rename file from .tmpX/foo.py to foo.py: "
        "Access is denied. (os error 5)"
    )
    assert _is_transient_lock("foo.exe - The process cannot access the file "
                              "because it is being used by another process.")
    assert _is_transient_lock("PermissionError: [WinError 32] ...")
    # NOT transient — genuine install failures must still fail (no retry):
    assert not _is_transient_lock("ModuleNotFoundError: No module named 'x'")
    assert not _is_transient_lock("ERROR: Could not find a version that satisfies")
    assert not _is_transient_lock("")
