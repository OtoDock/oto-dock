"""Isolated parse workers: results round-trip; memory/timeout/kill failures
surface as clean, self-contained error messages (never crashes); large
results truncate child-side; the semaphore bounds concurrency.

These spawn real child processes — each case costs child startup (~1s).
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import isolation  # noqa: E402
import isolation_targets as targets  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_budgets(monkeypatch):
    """Each case recomputes budgets (env overrides must take effect) and
    runs REAL spawn children — conftest's suite-wide inline mode must never
    leak in here (inline would run selfkill_target's os.kill in the pytest
    process itself and hog_target with no rlimit)."""
    monkeypatch.delenv("FILETOOLS_ISOLATION_INLINE", raising=False)
    isolation.parse_permits.cache_clear()
    isolation.worker_rss_budget_bytes.cache_clear()
    yield
    isolation.parse_permits.cache_clear()
    isolation.worker_rss_budget_bytes.cache_clear()


def test_normal_roundtrip():
    out = asyncio.run(isolation.run_parse(targets.ok_target, "hello"))
    assert out == "parsed:hello"


def test_kwargs_roundtrip():
    out = asyncio.run(isolation.run_parse(targets.big_target, n=10))
    assert out == "x" * 10


def test_memory_error_is_clean_message(monkeypatch):
    monkeypatch.setenv("FILETOOLS_PARSE_MEM_MB", "300")
    isolation.worker_rss_budget_bytes.cache_clear()
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.hog_target))
    msg = str(ei.value)
    assert "300MB" in msg
    assert "pages" in msg  # actionable guidance rides along


def test_timeout_kills_child(monkeypatch):
    monkeypatch.setenv("FILETOOLS_PARSE_TIMEOUT_S", "2")
    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.sleep_target, 60))
    assert time.monotonic() - t0 < 20
    assert "longer than 2s" in str(ei.value)


def test_sigkilled_child_is_clean_message():
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.selfkill_target))
    assert "terminated" in str(ei.value)


def test_child_exception_becomes_message():
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.raise_target, "boom-xyz"))
    assert "boom-xyz" in str(ei.value)


def test_large_result_truncates_without_deadlock():
    out = asyncio.run(
        isolation.run_parse(targets.big_target, isolation._RESULT_CAP_CHARS + 100_000)
    )
    assert out.startswith("xxx")
    assert "truncated at 512KB" in out
    assert len(out) < isolation._RESULT_CAP_CHARS + len(isolation._TRUNCATION_NOTE) + 1


def test_semaphore_bounds_concurrency():
    permits = isolation.parse_permits()

    async def burst():
        await asyncio.gather(
            *(isolation.run_parse(targets.sleep_target, 1.0) for _ in range(permits + 1))
        )

    t0 = time.monotonic()
    asyncio.run(burst())
    # permits+1 one-second sleeps through `permits` slots need two batches;
    # spawn overhead only ever makes this larger.
    assert time.monotonic() - t0 >= 2.0


def test_structured_bytes_roundtrip():
    out = asyncio.run(isolation.run_parse(targets.bytes_target, 100_000))
    assert out["images"][0]["mime"] == "image/png"
    assert out["images"][0]["data"].startswith(b"\x89PNG")
    assert len(out["images"][0]["data"]) == 100_000 + 4
    assert out["rendered"] == [{"page": 1}]


def test_oversized_structured_payload_is_clean_error():
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(
            targets.big_struct_target,
            isolation._RESULT_CAP_STRUCT_BYTES + 1024 * 1024,
        ))
    assert "too large to return" in str(ei.value)


def test_timeout_override_kwarg():
    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.sleep_target, 60, _timeout_s=2))
    assert time.monotonic() - t0 < 20
    assert "longer than 2s" in str(ei.value)


def test_advice_overrides_error_and_truncation(monkeypatch):
    monkeypatch.setenv("FILETOOLS_PARSE_MEM_MB", "300")
    isolation.worker_rss_budget_bytes.cache_clear()
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(
            targets.hog_target, _advice="Lower `dpi` instead"))
    assert "Lower `dpi` instead" in str(ei.value)
    assert "pages" not in str(ei.value)

    out = asyncio.run(isolation.run_parse(
        targets.big_target, isolation._RESULT_CAP_CHARS + 100_000,
        _advice="Lower `dpi` instead",
    ))
    assert "truncated at 512KB. Lower `dpi` instead." in out


def test_inline_mode_parity(monkeypatch):
    """Inline mode: same caps and exception mapping, no spawn."""
    monkeypatch.setenv("FILETOOLS_ISOLATION_INLINE", "1")

    assert asyncio.run(isolation.run_parse(targets.ok_target, "hi")) == "parsed:hi"

    # Exception → RuntimeError(str), like the child path.
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.raise_target, "boom-inline"))
    assert "boom-inline" in str(ei.value)

    # MemoryError → the too-large message, like the child path.
    monkeypatch.setenv("FILETOOLS_PARSE_MEM_MB", "300")
    isolation.worker_rss_budget_bytes.cache_clear()
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(targets.memerr_target))
    assert "300MB" in str(ei.value)

    # String cap still applies.
    out = asyncio.run(isolation.run_parse(
        targets.big_target, isolation._RESULT_CAP_CHARS + 100_000))
    assert "truncated at 512KB" in out


def test_inline_mode_baseexception_propagates(monkeypatch):
    """Inline must NOT copy the child's BaseException catch-all — in-process
    a swallowed KeyboardInterrupt/CancelledError would break the loop."""
    monkeypatch.setenv("FILETOOLS_ISOLATION_INLINE", "1")
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(isolation.run_parse(targets.kbint_target))


def test_budget_derivation_env_override(monkeypatch):
    monkeypatch.setenv("FILETOOLS_PARSE_MEM_MB", "1234")
    isolation.worker_rss_budget_bytes.cache_clear()
    assert isolation.worker_rss_budget_bytes() == 1234 * 1024 ** 2


def test_budget_derivation_from_cgroup(monkeypatch, tmp_path):
    monkeypatch.delenv("FILETOOLS_PARSE_MEM_MB", raising=False)

    limit_file = tmp_path / "memory.max"
    limit_file.write_text(str(4 * 1024 ** 3))
    monkeypatch.setattr(
        isolation, "_cgroup_limit_bytes", lambda: 4 * 1024 ** 3
    )
    isolation.parse_permits.cache_clear()
    isolation.worker_rss_budget_bytes.cache_clear()
    assert isolation.parse_permits() == 2
    expected = (4 * 1024 ** 3 - isolation._HEADROOM_BYTES) // 2
    assert isolation.worker_rss_budget_bytes() == expected

    monkeypatch.setattr(
        isolation, "_cgroup_limit_bytes", lambda: 2 * 1024 ** 3
    )
    isolation.parse_permits.cache_clear()
    isolation.worker_rss_budget_bytes.cache_clear()
    assert isolation.parse_permits() == 1
    expected = 2 * 1024 ** 3 - isolation._HEADROOM_BYTES
    assert isolation.worker_rss_budget_bytes() == expected
