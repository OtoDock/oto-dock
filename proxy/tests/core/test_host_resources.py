"""Unit tests for container-aware memory detection (core/sandbox/host_resources.py).

``reclaimable_bytes`` is a pure function tested exhaustively; ``live_available_bytes``
and ``detect_memory_limit_bytes`` are tested by monkeypatching the low-level readers
so the cgroup-v2 / v1 / proc ladder + the fail-closed path are pinned regardless of
where the suite runs.
"""

import core.sandbox.host_resources as H

_MIB = 1024 * 1024


# --- detect_memory_limit_bytes (real read) ---------------------------------

def test_detect_memory_positive():
    # A cgroup limit, or the /proc/meminfo host fallback — never <= 0.
    assert H.detect_memory_limit_bytes() > 0


# --- reclaimable_bytes (pure) ----------------------------------------------

def test_reclaimable_v2_subtracts_dirty_and_writeback():
    stat = {"inactive_file": 1000, "slab_reclaimable": 500,
            "file_dirty": 100, "file_writeback": 50, "active_file": 9999}
    # inactive_file + slab_reclaimable - dirty - writeback (active_file ignored)
    assert H.reclaimable_bytes(stat, v2=True) == 1000 + 500 - 100 - 50


def test_reclaimable_v1_has_no_slab_key():
    # cgroup v1 uses total_* hierarchical keys and has NO slab-reclaimable field;
    # a stray slab_reclaimable must be ignored on the v1 path.
    stat = {"total_inactive_file": 1000, "total_dirty": 100,
            "total_writeback": 50, "slab_reclaimable": 99999}
    assert H.reclaimable_bytes(stat, v2=False) == 1000 - 100 - 50


def test_reclaimable_clamps_to_zero():
    stat = {"inactive_file": 100, "file_dirty": 999, "file_writeback": 999}
    assert H.reclaimable_bytes(stat, v2=True) == 0


def test_reclaimable_missing_keys_are_zero():
    assert H.reclaimable_bytes({}, v2=True) == 0
    assert H.reclaimable_bytes({}, v2=False) == 0


# --- live_available_bytes (monkeypatched ladder) ---------------------------

def test_live_available_capped_v2(monkeypatch):
    monkeypatch.setattr(H, "_limited_groups", lambda: [(4000 * _MIB, "v2", "/sys/fs/cgroup")])
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)        # memory.current
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {"inactive_file": 300 * _MIB})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 64_000 * _MIB)  # host roomy
    # (4000 - 2500) + 300 reclaimable = 1800 MiB (< host ⇒ cgroup term wins)
    assert H.live_available_bytes() == 1800 * _MIB


def test_live_available_capped_missing_stat(monkeypatch):
    monkeypatch.setattr(H, "_limited_groups", lambda: [(4000 * _MIB, "v2", "/sys/fs/cgroup")])
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {})         # no reclaimable credit
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 64_000 * _MIB)
    assert H.live_available_bytes() == 1500 * _MIB


def test_live_available_capped_takes_host_min(monkeypatch):
    # A leaky SIBLING container eats host RAM without touching this cgroup:
    # cgroup headroom says 1800 MiB, the host has only 400 left. Gate 2 must
    # see 400 — a cgroup-only read admits straight into the host OOM killer
    # (the 2026-07-06 camoufox incident, with the proxy capped).
    monkeypatch.setattr(H, "_limited_groups", lambda: [(4000 * _MIB, "v2", "/sys/fs/cgroup")])
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {"inactive_file": 300 * _MIB})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 400 * _MIB)
    assert H.live_available_bytes() == 400 * _MIB


def test_live_available_capped_host_unreadable_falls_back_to_cgroup(monkeypatch):
    monkeypatch.setattr(H, "_limited_groups", lambda: [(4000 * _MIB, "v2", "/sys/fs/cgroup")])
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: None)
    assert H.live_available_bytes() == 1500 * _MIB


def test_live_available_uncapped_uses_meminfo(monkeypatch):
    monkeypatch.setattr(H, "_limited_groups", lambda: [])
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 7000 * _MIB)
    assert H.live_available_bytes() == 7000 * _MIB


def test_live_available_fail_closed(monkeypatch):
    # Nothing readable → 0 (⇒ GATE 2 admits nothing), never a guess.
    monkeypatch.setattr(H, "_limited_groups", lambda: [])
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: None)
    assert H.live_available_bytes() == 0


def test_live_available_real_read_nonnegative():
    assert H.live_available_bytes() >= 0


# --- the own cgroup's limits (a planted tree) ------------------------------

def _plant(tmp_path, monkeypatch, path: str, files: dict[str, dict[str, str]], host_mb: int = 16_000):
    """A cgroup v2 tree under ``tmp_path``: ``files`` maps a folder relative to
    the root to its files; this process sits at ``path``."""
    root = tmp_path / "cg"
    for folder, content in files.items():
        d = root / folder if folder else root
        d.mkdir(parents=True, exist_ok=True)
        for name, text in content.items():
            (d / name).write_text(text)
    proc = tmp_path / "cgroup"
    proc.write_text(f"0::{path}\n")
    monkeypatch.setattr(H, "_CGROUP_ROOT", str(root))
    monkeypatch.setattr(H, "_PROC_SELF_CGROUP", str(proc))
    monkeypatch.setattr(H, "_proc_mem_total_bytes", lambda: host_mb * _MIB)
    monkeypatch.setattr(H, "_read_int", lambda p: int(open(p).read()) if __import__("os").path.exists(p) else None)
    return root


def test_a_units_own_cap_is_read_not_the_roots(tmp_path, monkeypatch):
    # A bare-metal compute unit with MemoryMax=4G: its own group holds the cap,
    # the root holds none (the roadmap's "a unit's cap is not what it reports").
    _plant(tmp_path, monkeypatch, "/system.slice/otodock-compute.service", {
        "": {}, "system.slice": {"memory.max": "max", "memory.high": "max"},
        "system.slice/otodock-compute.service": {"memory.max": str(4096 * _MIB), "memory.high": "max",
                                                 "memory.current": str(1000 * _MIB), "memory.stat": ""},
    })
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 12_000 * _MIB)
    assert H.detect_memory_limit_bytes() == 4096 * _MIB
    assert H.live_available_bytes() == 3096 * _MIB
    assert H.report()["capped"] is True and H.report()["known"] is True


def test_the_tightest_limit_on_the_path_wins_and_memory_high_counts(tmp_path, monkeypatch):
    _plant(tmp_path, monkeypatch, "/user.slice/app.slice/walk.scope", {
        "user.slice": {"memory.max": str(8192 * _MIB), "memory.current": str(7000 * _MIB), "memory.stat": ""},
        "user.slice/app.slice": {"memory.max": "max"},
        "user.slice/app.slice/walk.scope": {"memory.max": "max", "memory.high": str(6000 * _MIB),
                                             "memory.current": str(2000 * _MIB), "memory.stat": ""},
    })
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 9_000 * _MIB)
    # The scope's memory.high is the tightest limit; the slice above it is the
    # shortest of room (8192 − 7000): every capped group on the path counts.
    assert H.detect_memory_limit_bytes() == 6000 * _MIB
    assert H.live_available_bytes() == 1192 * _MIB


def test_a_container_reads_its_namespaces_root(tmp_path, monkeypatch):
    _plant(tmp_path, monkeypatch, "/", {"": {"memory.max": str(2048 * _MIB), "memory.current": str(512 * _MIB),
                                             "memory.stat": "inactive_file 104857600\n"}})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 9_000 * _MIB)
    assert H.detect_memory_limit_bytes() == 2048 * _MIB
    assert H.live_available_bytes() == (2048 - 512 + 100) * _MIB


def test_a_path_named_from_outside_the_namespace_is_skipped(tmp_path, monkeypatch):
    # An older engine without a cgroup namespace names /docker/<id>: no such
    # folder inside, the mounted root holds the container's cap.
    _plant(tmp_path, monkeypatch, "/docker/abc", {"": {"memory.max": str(3000 * _MIB)}})
    assert H.detect_memory_limit_bytes() == 3000 * _MIB


def test_an_uncapped_host_reads_meminfo(tmp_path, monkeypatch):
    _plant(tmp_path, monkeypatch, "/user.slice", {"user.slice": {"memory.max": "max"}})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 7_000 * _MIB)
    assert H._capped_limit() is None
    assert H.live_available_bytes() == 7_000 * _MIB


def test_a_report_that_read_nothing_is_not_known(monkeypatch):
    monkeypatch.setattr(H, "_limited_groups", lambda: [])
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: None)
    r = H.report()
    assert r["known"] is False and r["available_bytes"] == 0


# --- the numbers both sides read --------------------------------------------

def test_the_guard_never_acts_on_more_than_half_the_machine():
    # The rig closes admission with a huge floor: the guard keeps half.
    huge = H.reserve_mb(4_096, floor_mb=100_000_000, share=H.COMPUTE_SHARE)
    assert H.guard_reserve_mb(4_096, huge) == 2_048
    assert H.guard_reserve_mb(20_480, 1_024) == 1_024


def test_a_big_computers_reserve_stops_at_4_gb():
    """10 % of a 64 GB workstation would refuse chats with 6.5 GB free: a
    personal or admin computer keeps at most 4 GB above its floor. A compute
    machine's share has no cap."""
    assert H.computer_reserve_mb(8_192) == 1_024
    assert H.computer_reserve_mb(16_384) == 1_638
    assert H.computer_reserve_mb(65_536) == 4_096
    assert H.reserve_mb(65_536, floor_mb=512, share=H.COMPUTE_SHARE) == 3_276
    assert H.reserve_mb(65_536, floor_mb=8_000, share=0.10, cap_mb=4_096) == 8_000   # a floor still wins


def test_a_tasks_headroom_is_a_heavy_session_until_the_machine_is_measured():
    """A thirty-second of the room only on a machine whose size is known: with
    no total (admission not initialised yet, a failed read) a task leaves one
    heavy session free, as before the small-machine rule."""
    assert H.task_headroom_mb(0, 512, 850) == 850
    assert H.task_headroom_mb(4_096, 512, 850) == 112
    assert H.task_headroom_mb(65_536, 3_276, 850) == 850


def test_min_stop_is_512_or_half_the_reserve():
    assert H.min_stop_mb(512) == 256 and H.min_stop_mb(1_024) == 512 and H.min_stop_mb(3_276) == 512


def test_the_ceiling_is_the_room_or_the_platforms_lower_one():
    assert H.ceiling_mb(20_480, 1_024) == 19_456
    assert H.ceiling_mb(20_480, 1_024, session_max_mb=8_192) == 8_192
    assert H.ceiling_mb(6_656, 512, session_max_mb=H.WORK_FLOOR_MB) == 6_144
    assert H.ceiling_mb(4_096, 512, session_max_mb=H.WORK_FLOOR_MB) == 3_584


def test_the_swap_credit_is_off_by_default_and_half_the_free_swap_when_set():
    assert H.swap_credit_mb(2_048, 0) == 0
    assert H.swap_credit_mb(400, 512) == 200 and H.swap_credit_mb(4_096, 512) == 512
