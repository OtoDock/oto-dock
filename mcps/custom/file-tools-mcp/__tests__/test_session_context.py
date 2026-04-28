"""Tests for the per-request session binding (``shared.set_request_context`` /
``shared._current_session``).

file-tools is a SHARED container, so the old module-level ``_current_session_id``
raced two concurrent sessions onto one value. These tests prove the contextvar
replacement isolates each request and fails CLOSED — the property that lets
the master key be replaced by a per-session JWT forwarded on every callback.
"""

import asyncio
import contextvars
import sys
from pathlib import Path

# Make the parent dir importable as a top-level module
sys.path.insert(0, str(Path(__file__).parent.parent))

import shared


def test_set_and_get_round_trip():
    shared.set_request_context("sess-A", "Bearer jwtA")
    assert shared._current_session() == ("sess-A", "Bearer jwtA")


def test_none_args_coerced_to_empty():
    shared.set_request_context(None, None)  # type: ignore[arg-type]
    assert shared._current_session() == ("", "")


def test_isolated_across_concurrent_tasks():
    """Two concurrent requests must not bleed session_id/auth into each other.

    asyncio.create_task copies the context per task — the same guarantee the
    stateless streamable-HTTP manager gives each request's task group — so each
    task sees only its own binding even though they interleave. A module global
    would fail this; the contextvar passes.
    """
    async def worker(sid, auth, hold, out):
        shared.set_request_context(sid, auth)
        await asyncio.sleep(hold)  # let the sibling run + set its own context
        out[sid] = shared._current_session()

    async def main():
        out = {}
        await asyncio.gather(
            asyncio.create_task(worker("sess-A", "Bearer A", 0.03, out)),
            asyncio.create_task(worker("sess-B", "Bearer B", 0.01, out)),
        )
        return out

    out = asyncio.run(main())
    assert out["sess-A"] == ("sess-A", "Bearer A")
    assert out["sess-B"] == ("sess-B", "Bearer B")


def test_fails_closed_in_fresh_context():
    """A request whose context was never bound reads the empty default — the
    callbacks then report 'not session-bound' rather than acting on a stale or
    foreign session. Order-independent: a brand-new Context has vars at default.
    """
    ctx = contextvars.Context()
    assert ctx.run(shared._current_session) == ("", "")
