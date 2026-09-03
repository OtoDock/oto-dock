"""Tests for host/cli_versions.py — pin-verified CLI resolution + reconcile.

Two load-bearing behaviors:
- Verify-first reconcile: a binary already at the pin is recorded with NO
  install — and out-resolves a stale configured/PATH-shadowing copy (the
  frozen-satellite.conf incident class).
- The per-user npm-prefix fallback: a sudo-less satellite on Linux/NodeSource
  cannot write the global prefix (/usr), so without the fallback every
  platform pin bump would silently drift the machine (fail-open logs only).
"""

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from satellite.host import cli_versions


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Fresh module state per test — resolution state is process-global."""
    monkeypatch.setattr(cli_versions, "_pins", {})
    monkeypatch.setattr(cli_versions, "_config_bins", {})
    monkeypatch.setattr(cli_versions, "_verified", {})
    monkeypatch.setattr(cli_versions, "_last_reconciled", {})
    monkeypatch.setattr(cli_versions, "_fast_verify_attempted", {})
    monkeypatch.setattr(cli_versions, "_unverified_probe", {})


def _mk(dirpath: Path, name: str = "claude") -> str:
    """Create an executable stub and return its path."""
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / name
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    return str(f)


@pytest.fixture
def fake_npm(monkeypatch):
    """Stub shutil.which → a fake npm; record subprocess.run invocations and
    drive their return codes from a queue."""
    calls = []
    codes = []

    monkeypatch.setattr(
        cli_versions.shutil, "which",
        lambda name: "/usr/bin/npm" if name == "npm" else None,
    )

    def _run(cmd, **kwargs):
        calls.append(cmd)
        rc = codes.pop(0) if codes else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="EACCES: permission denied")

    monkeypatch.setattr(cli_versions.subprocess, "run", _run)
    return SimpleNamespace(calls=calls, codes=codes)


# --- _npm_install / PATH plumbing (unchanged behavior) ---------------------

def test_npm_install_global_success_skips_fallback(fake_npm):
    fake_npm.codes[:] = [0]
    assert cli_versions._npm_install("@anthropic-ai/claude-code", "2.1.201") is True
    assert len(fake_npm.calls) == 1
    assert "--prefix" not in fake_npm.calls[0]


def test_npm_install_falls_back_to_user_prefix(fake_npm, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_versions, "_USER_NPM_PREFIX", tmp_path / ".npm-global")
    fake_npm.codes[:] = [243, 0]  # global EACCES → user prefix succeeds
    assert cli_versions._npm_install("@openai/codex", "0.142.5") is True
    assert len(fake_npm.calls) == 2
    assert "--prefix" in fake_npm.calls[1]
    assert str(tmp_path / ".npm-global") in fake_npm.calls[1]


def test_npm_install_both_attempts_fail(fake_npm):
    fake_npm.codes[:] = [243, 243]
    assert cli_versions._npm_install("@openai/codex", "0.142.5") is False
    assert len(fake_npm.calls) == 2


def test_npm_install_missing_npm(monkeypatch):
    monkeypatch.setattr(cli_versions.shutil, "which", lambda name: None)
    assert cli_versions._npm_install("@openai/codex", "0.142.5") is False


def test_ensure_user_npm_bin_on_path(monkeypatch, tmp_path):
    prefix = tmp_path / ".npm-global"
    (prefix / "bin").mkdir(parents=True)
    monkeypatch.setattr(cli_versions, "_USER_NPM_PREFIX", prefix)
    monkeypatch.setenv("PATH", "/usr/bin")

    cli_versions.ensure_user_npm_bin_on_path()
    assert os.environ["PATH"].startswith(str(prefix / "bin") + os.pathsep)

    # Idempotent — a second call doesn't duplicate the entry.
    cli_versions.ensure_user_npm_bin_on_path()
    assert os.environ["PATH"].count(str(prefix / "bin")) == 1


def test_ensure_user_npm_bin_noop_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_versions, "_USER_NPM_PREFIX", tmp_path / "nope")
    monkeypatch.setenv("PATH", "/usr/bin")
    cli_versions.ensure_user_npm_bin_on_path()
    assert os.environ["PATH"] == "/usr/bin"


# --- set_pins ---------------------------------------------------------------

def test_set_pins_normalizes_and_drops_unpinned_verified():
    cli_versions._verified["claude"] = {"path": "/x/claude", "version": "1.0.0"}
    cli_versions._verified["codex"] = {"path": "/x/codex", "version": "1.0.0"}
    cli_versions.set_pins({"claude_code": " 2.1.220 "}, {"claude": "claude"})
    assert cli_versions._pins == {"claude": "2.1.220"}
    # codex pin gone → authority back to config; claude entry survives.
    assert "codex" not in cli_versions._verified
    assert "claude" in cli_versions._verified
    assert cli_versions._config_bins == {"claude": "claude"}


# --- resolve_spawn_bin priority chain ---------------------------------------

def test_resolve_verified_wins_over_config(tmp_path):
    verified = _mk(tmp_path / "v")
    configured = _mk(tmp_path / "c")
    cli_versions._verified["claude"] = {"path": verified, "version": "2.1.220"}
    assert cli_versions.resolve_spawn_bin("claude", configured) == verified


def test_resolve_vanished_verified_falls_through(tmp_path):
    configured = _mk(tmp_path / "c")
    cli_versions._verified["claude"] = {
        "path": str(tmp_path / "gone" / "claude"), "version": "2.1.220",
    }
    assert cli_versions.resolve_spawn_bin("claude", configured) == configured


def test_resolve_bare_name_resolves_via_which(monkeypatch, tmp_path):
    on_path = _mk(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert cli_versions.resolve_spawn_bin("claude", "claude") == on_path


def test_resolve_unresolvable_returns_bare(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cli_versions.resolve_spawn_bin("claude", "") == "claude"


def test_pty_neutrality_windows_cmd_shim(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_versions, "_is_windows", lambda: True)
    shim = _mk(tmp_path / "npm", "claude.cmd")
    cli_versions._verified["claude"] = {"path": shim, "version": "2.1.220"}
    # PTY spawns get the bare name (pywinpty can't launch .cmd shims);
    # headless spawns take the shim verbatim (proven CreateProcess path).
    assert cli_versions.resolve_spawn_bin("claude", "", for_pty=True) == "claude"
    assert cli_versions.resolve_spawn_bin("claude", "") == shim


# --- reconcile: verify-first ------------------------------------------------

def _wire_probe(monkeypatch, versions: dict):
    monkeypatch.setattr(
        cli_versions, "_probe_version_at", lambda p: versions.get(p, ""),
    )


def test_reconcile_verify_first_heals_stale_config_no_install(
    tmp_path, monkeypatch,
):
    """The frozen-conf incident: stale absolute hint, pin elsewhere on PATH —
    heals by verification alone, zero installs."""
    stale = _mk(tmp_path / "localbin")
    pinned = _mk(tmp_path / "usrbin")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(tmp_path / "localbin"), str(tmp_path / "usrbin")]),
    )
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {stale: "2.1.168", pinned: "2.1.220"})
    installs = []
    monkeypatch.setattr(
        cli_versions, "_npm_install",
        lambda pkg, v: installs.append((pkg, v)) or True,
    )
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {"claude": stale})
    cli_versions.reconcile_cli_versions(pins)

    assert installs == []
    assert cli_versions._verified["claude"] == {
        "path": pinned, "version": "2.1.220",
    }
    assert cli_versions.resolve_spawn_bin("claude", stale) == pinned


def test_reconcile_config_hint_wins_when_it_satisfies_pin(tmp_path, monkeypatch):
    configured = _mk(tmp_path / "custom")
    also_pinned = _mk(tmp_path / "usrbin")
    monkeypatch.setenv("PATH", str(tmp_path / "usrbin"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {configured: "2.1.220", also_pinned: "2.1.220"})
    monkeypatch.setattr(cli_versions, "_npm_install", lambda *a: False)
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {"claude": configured})
    cli_versions.reconcile_cli_versions(pins)
    assert cli_versions._verified["claude"]["path"] == configured


def test_reconcile_installs_when_nothing_matches(tmp_path, monkeypatch):
    npmbin = tmp_path / "npmbin"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(
        cli_versions, "_npm_global_bin_dirs", lambda: [str(npmbin)],
    )
    versions: dict = {}
    _wire_probe(monkeypatch, versions)

    def fake_install(pkg, want):
        bin_name = "claude" if "claude" in pkg else "codex"
        versions[_mk(npmbin, bin_name)] = want
        return True

    monkeypatch.setattr(cli_versions, "_npm_install", fake_install)
    pins = {"claude_code": "2.1.220", "codex": "0.145.0"}
    cli_versions.set_pins(pins, {})
    cli_versions.reconcile_cli_versions(pins)

    assert cli_versions._verified["claude"]["version"] == "2.1.220"
    assert cli_versions._verified["codex"]["version"] == "0.145.0"
    assert cli_versions._last_reconciled == pins


def test_reconcile_per_cli_independence(tmp_path, monkeypatch):
    """A healed claude survives a failing codex — and the pass retries later
    (_last_reconciled only set on a clean pass)."""
    pinned = _mk(tmp_path / "usrbin")
    monkeypatch.setenv("PATH", str(tmp_path / "usrbin"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {pinned: "2.1.220"})
    monkeypatch.setattr(cli_versions, "_npm_install", lambda *a: False)
    pins = {"claude_code": "2.1.220", "codex": "0.145.0"}
    cli_versions.set_pins(pins, {})
    cli_versions.reconcile_cli_versions(pins)

    assert cli_versions._verified["claude"]["path"] == pinned
    assert "codex" not in cli_versions._verified
    assert cli_versions._last_reconciled == {}


def test_reconcile_fastpath_reprobes_recorded_paths(tmp_path, monkeypatch):
    """Unchanged pins short-circuit ONLY while the recorded binaries still
    probe at the pin — in-place drift forces a full pass."""
    old = _mk(tmp_path / "usrbin")
    fresh = _mk(tmp_path / "fresh")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(tmp_path / "usrbin"), str(tmp_path / "fresh")]),
    )
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    versions = {old: "2.1.220", fresh: "2.1.220"}
    _wire_probe(monkeypatch, versions)
    monkeypatch.setattr(cli_versions, "_npm_install", lambda *a: False)
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {})
    cli_versions.reconcile_cli_versions(pins)
    assert cli_versions._verified["claude"]["path"] == old
    assert cli_versions._last_reconciled == pins

    # In-place replacement drifts the recorded binary → full pass re-records.
    versions[old] = "9.9.9"
    cli_versions.reconcile_cli_versions(pins)
    assert cli_versions._verified["claude"]["path"] == fresh


def test_reconcile_failure_warns_with_candidates(tmp_path, monkeypatch, caplog):
    wrong = _mk(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {wrong: "2.1.168"})
    monkeypatch.setattr(cli_versions, "_npm_install", lambda *a: False)
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {})
    with caplog.at_level(logging.WARNING, logger="satellite"):
        cli_versions.reconcile_cli_versions(pins)
    warning = "\n".join(r.message for r in caplog.records)
    assert wrong in warning
    assert "2.1.168" in warning
    assert "codex" not in cli_versions._verified
    assert "claude" not in cli_versions._verified


# --- spawn-time lazy fast verify ---------------------------------------------

@pytest.mark.asyncio
async def test_async_lazy_fast_verify_records(tmp_path, monkeypatch):
    pinned = _mk(tmp_path / "usrbin")
    monkeypatch.setenv("PATH", str(tmp_path / "usrbin"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {pinned: "2.1.220"})
    cli_versions.set_pins({"claude_code": "2.1.220"}, {})

    resolved = await cli_versions.resolve_spawn_bin_async("claude", "claude")
    assert resolved == pinned
    assert cli_versions._verified["claude"]["path"] == pinned


@pytest.mark.asyncio
async def test_async_failed_verify_is_throttled(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    calls = []

    def counting_verify(bin_name, configured):
        calls.append(bin_name)
        return ""

    monkeypatch.setattr(
        cli_versions, "_fast_verify_and_record", counting_verify,
    )
    cli_versions.set_pins({"claude_code": "2.1.220"}, {})

    await cli_versions.resolve_spawn_bin_async("claude", "")
    await cli_versions.resolve_spawn_bin_async("claude", "")
    assert calls == ["claude"]  # second spawn inside the retry window skips


@pytest.mark.asyncio
async def test_async_no_pin_never_probes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        cli_versions, "_fast_verify_and_record",
        lambda *a: calls.append(a) or "",
    )
    configured = _mk(tmp_path / "c")
    resolved = await cli_versions.resolve_spawn_bin_async("claude", configured)
    assert resolved == configured
    assert calls == []


# --- cli_status reporting ----------------------------------------------------

def test_report_verified_shape_and_fast_path(tmp_path, monkeypatch):
    pinned = _mk(tmp_path / "usrbin")
    monkeypatch.setenv("PATH", str(tmp_path / "usrbin"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {pinned: "2.1.220"})
    monkeypatch.setattr(cli_versions, "_npm_install", lambda *a: False)
    reports = []
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {})
    cli_versions.reconcile_cli_versions(pins, reports.append)
    assert reports == [{"claude": {"version": "2.1.220", "path": pinned}}]
    # The unchanged-pins fast path re-probes and STILL reports (capabilities
    # were just wiped at auth — a silent fast path would leave them blank).
    cli_versions.reconcile_cli_versions(pins, reports.append)
    assert len(reports) == 2
    assert reports[1] == reports[0]


def test_report_unverified_carries_first_probed(tmp_path, monkeypatch):
    """A pinned-but-unverifiable CLI reports the REAL version spawn
    resolution would pick — not a blank."""
    wrong = _mk(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(cli_versions, "_npm_global_bin_dirs", lambda: [])
    _wire_probe(monkeypatch, {wrong: "2.1.168"})
    monkeypatch.setattr(cli_versions, "_npm_install", lambda *a: False)
    reports = []
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {})
    cli_versions.reconcile_cli_versions(pins, reports.append)
    assert reports == [{"claude": {"version": "2.1.168", "path": wrong}}]


def test_report_skipped_when_lock_held_or_no_pins():
    reports = []
    cli_versions.reconcile_cli_versions({}, reports.append)  # no pins
    cli_versions.set_pins({"claude_code": "2.1.220"}, {})
    assert cli_versions._reconcile_lock.acquire(blocking=False)
    try:
        # Concurrent pass: the holder owns the report; this one stays silent.
        cli_versions.reconcile_cli_versions(
            {"claude_code": "2.1.220"}, reports.append,
        )
    finally:
        cli_versions._reconcile_lock.release()
    assert reports == []


def test_report_fires_even_when_pass_crashes(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "none"))

    def boom(*a):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cli_versions, "_scan_for_pin", boom)
    reports = []
    pins = {"claude_code": "2.1.220"}
    cli_versions.set_pins(pins, {})
    with pytest.raises(RuntimeError):
        cli_versions.reconcile_cli_versions(pins, reports.append)
    assert reports == [{"claude": {"version": None, "path": None}}]
