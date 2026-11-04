#!/usr/bin/env python3
"""Unit tests for the Docker-MCP streaming sidecar's pure session-lifecycle logic.

The module under test is container-side code that lives OUTSIDE the proxy:
``mcps/_shared/stream_sidecar.py`` (mounted into Docker-MCP images at build
time).

It covers the session map: open/refresh, the oto↔mcp mapping, and forget —
including the oto-reuse case where a new mcp-session-id supersedes the prior one
for an oto (a re-init), which must not let forgetting the old session clobber the
live mapping — the attached-stream refresh, the session count a health checker
reads, and one idle-GC pass. The aiohttp handlers are I/O, exercised live over a real MCP.

Standalone (no pytest / aiohttp / DB needed):  python3 proxy/tests/mcp/test_stream_sidecar.py
Via pytest:                                    pytest proxy/tests/mcp/test_stream_sidecar.py
"""
import asyncio
import sys
import time
from pathlib import Path

# The module under test lives in mcps/_shared (container-side, not the proxy).
# Self-computed (not tests._paths) so `python <file>` standalone runs still work;
# tests/<area>/<file>.py -> repo root is parents[3].
_SHARED = Path(__file__).resolve().parents[3] / "mcps" / "_shared"
sys.path.insert(0, str(_SHARED))

import stream_sidecar as ss


def _reset():
    ss._sessions.clear()
    ss._oto_to_mcp.clear()


def test_touch_opens_and_maps():
    _reset()
    ss._touch("mcp-A", "oto-1")
    assert ss._sessions["mcp-A"]["oto"] == "oto-1"
    assert ss._oto_to_mcp["oto-1"] == "mcp-A"


def test_touch_refreshes_last():
    _reset()
    ss._touch("mcp-A", "oto-1")
    first = ss._sessions["mcp-A"]["last"]
    ss._touch("mcp-A", "oto-1")
    assert ss._sessions["mcp-A"]["last"] >= first


def test_touch_empty_mcp_sid_is_noop():
    _reset()
    ss._touch("", "oto-1")
    assert ss._sessions == {} and ss._oto_to_mcp == {}


def test_forget_clears_both_maps():
    _reset()
    ss._touch("mcp-A", "oto-1")
    ss._forget("mcp-A")
    assert "mcp-A" not in ss._sessions
    assert "oto-1" not in ss._oto_to_mcp


def test_oto_reuse_remap_guard():
    # A re-init reuses the oto with a fresh mcp-session-id; the oto must point at
    # the newest, and forgetting the OLD session must not clear that mapping
    # (else the live session's active-close is orphaned → a leak).
    _reset()
    ss._touch("mcp-old", "oto-1")
    ss._touch("mcp-new", "oto-1")
    assert ss._oto_to_mcp["oto-1"] == "mcp-new"
    ss._forget("mcp-old")
    assert ss._oto_to_mcp["oto-1"] == "mcp-new"   # not clobbered
    assert "mcp-new" in ss._sessions


def test_forget_unknown_is_safe():
    _reset()
    ss._forget("nope")  # must not raise
    assert ss._sessions == {}


def test_refresh_keeps_an_attached_session_out_of_gc():
    # An attached client's stream refreshes its session on every write, so a
    # long think-gap never lets the GC reap a session whose client is there.
    _reset()
    ss._touch("mcp-A", "oto-1")
    ss._sessions["mcp-A"]["last"] = time.time() - ss.SESSION_IDLE_S - 60
    assert ss._stale_sids(time.time()) == ["mcp-A"]
    ss._refresh("mcp-A")
    assert ss._stale_sids(time.time()) == []


def test_refresh_never_resurrects_a_forgotten_session():
    _reset()
    ss._touch("mcp-A", "oto-1")
    ss._forget("mcp-A")
    ss._refresh("mcp-A")  # a straggling stream after a DELETE or the GC
    assert "mcp-A" not in ss._sessions and "oto-1" not in ss._oto_to_mcp


def test_open_sessions_leaves_out_a_prefix():
    _reset()
    ss._touch("mcp-held", "otodock-checker-held")
    ss._touch("mcp-fresh", "otodock-checker-fresh")
    ss._touch("mcp-agent", "6e5475d2-3492")
    ss._touch("mcp-anon", "")
    assert ss._open_sessions() == 4
    # A session with no OtoDock id still counts: unknown use is use.
    assert ss._open_sessions("otodock-checker") == 2
    ss._forget("mcp-agent")
    ss._forget("mcp-anon")
    assert ss._open_sessions("otodock-checker") == 0


def test_gc_pass_deletes_only_the_stale_sessions():
    _reset()
    deleted = []

    async def fake_delete(sid):
        deleted.append(sid)
        return 200

    real_delete, real_dir = ss._delete_upstream, ss.SCREENSHOTS_DIR
    ss._delete_upstream, ss.SCREENSHOTS_DIR = fake_delete, "/nonexistent-screenshots"
    try:
        ss._touch("mcp-old", "oto-old")
        ss._touch("mcp-new", "oto-new")
        ss._sessions["mcp-old"]["last"] = time.time() - ss.SESSION_IDLE_S - 1
        asyncio.run(ss._gc_pass())
    finally:
        ss._delete_upstream, ss.SCREENSHOTS_DIR = real_delete, real_dir
    assert deleted == ["mcp-old"]
    assert list(ss._sessions) == ["mcp-new"] and "oto-old" not in ss._oto_to_mcp


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001  # the script runner reports each failure and runs the rest
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
