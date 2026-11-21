"""The pre-turn MCP warm gate of a Codex app-server session
(``app_server_client.wait_for_mcp_startup``, vendored into the satellite,
whose session picks the cap: ``satellite/tests/test_codex_warm_gate.py``).

Codex sends the model the servers that are CONNECTED when a turn starts, so a
gate that leaves while servers are still ``starting`` costs the first turn its
tools and changes the tool list between turns (every prefix cache misses —
minutes of re-prefill on a local model). The gate waits until every EXPECTED
server (the ``[mcp_servers.*]`` the session wrote) has a terminal status,
bounded by a cap, never waits for Codex's own hosted MCPs, and keeps the quick
exits for a session with no servers / a daemon that reports nothing.
"""

import asyncio
from types import SimpleNamespace

import pytest

from core.layers.codex.app_server_client import (
    MCP_STARTUP_METHOD,
    mcp_server_names_from_toml,
    wait_for_mcp_startup,
)


def _client():
    return SimpleNamespace(notif_queue=asyncio.Queue())


def _status(name, status):
    return (MCP_STARTUP_METHOD, {"threadId": "t1", "name": name, "status": status})


async def _push(q, items, delay=0.0):
    await asyncio.sleep(delay)
    for it in items:
        await q.put(it)


async def _gate(expected, feed, *, cap=1.0, poll=0.05, grace=0.3):
    """Run the shared gate with small constants; ``feed`` pushes notifications
    on its own schedule. Returns (result, elapsed)."""
    client = _client()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    feeder = asyncio.create_task(feed(client.notif_queue))
    try:
        res = await wait_for_mcp_startup(
            client, expected, cap_s=cap, poll_s=poll, no_status_grace_s=grace,
        )
    finally:
        feeder.cancel()
    return res, loop.time() - t0


@pytest.mark.asyncio
async def test_exits_as_soon_as_every_expected_server_is_terminal():
    async def feed(q):
        await _push(q, [_status("a", "starting"), _status("b", "starting"),
                        _status("c", "starting"), _status("a", "ready"),
                        _status("b", "failed"), _status("c", "cancelled")])

    res, elapsed = await _gate(["a", "b", "c"], feed)
    # Terminal statuses end the gate at once — no poll wait, no cap.
    assert elapsed < 0.05
    assert res.pending == [] and res.ready == ["a"] and res.failed == ["b", "c"]


@pytest.mark.asyncio
async def test_keeps_waiting_through_a_silence_while_a_server_is_starting():
    async def feed(q):
        await _push(q, [_status("fast", "starting"), _status("slow", "starting"),
                        _status("fast", "ready")])
        await asyncio.sleep(0.4)          # far longer than the poll interval
        await _push(q, [_status("slow", "ready")])

    res, elapsed = await _gate(["slow", "fast"], feed)
    assert 0.4 <= elapsed < 0.9
    assert res.pending == []


@pytest.mark.asyncio
async def test_waits_for_an_expected_server_that_has_not_reported_yet():
    # The daemon's `starting` burst can lag the gate's start: an expected
    # server with no status yet counts as pending (the old gate broke here).
    async def feed(q):
        await _push(q, [_status("late", "starting"), _status("late", "ready")],
                    delay=0.3)

    res, elapsed = await _gate(["late"], feed, grace=1.0)
    assert 0.3 <= elapsed < 0.8
    assert res.pending == []


@pytest.mark.asyncio
async def test_unexpected_reporter_is_recorded_not_waited_for():
    # Codex's own hosted MCPs (the ChatGPT apps connector) report too; they are
    # not ours to wait for — the gate ends when OUR servers are terminal.
    async def feed(q):
        await _push(q, [_status("codex_apps", "starting"), _status("a", "starting"),
                        _status("a", "ready")])
        await asyncio.sleep(5)
        await _push(q, [_status("codex_apps", "ready")])

    res, elapsed = await _gate(["a"], feed)
    assert elapsed < 0.1
    assert res.unexpected == ["codex_apps"] and res.pending == []


@pytest.mark.asyncio
async def test_no_servers_exits_on_the_first_silence():
    async def feed(q):
        await q.put(("thread/started", {}))  # pre-turn noise only

    res, elapsed = await _gate([], feed)
    assert elapsed < 0.2 and res.pending == []


@pytest.mark.asyncio
async def test_nothing_reported_for_our_servers_exits_after_the_grace_not_the_cap():
    # Expected servers but a daemon that never reports them (a renamed
    # notification on a Codex bump, or only Codex's own MCPs reporting): the
    # grace window bounds it, not the cap.
    async def feed(q):
        await _push(q, [_status("codex_apps", "starting"), _status("codex_apps", "ready")])
        await asyncio.sleep(10)

    res, elapsed = await _gate(["a", "b"], feed, cap=5.0, grace=0.3)
    assert 0.3 <= elapsed < 0.6
    assert res.pending == ["a", "b"] and res.unexpected == ["codex_apps"]


@pytest.mark.asyncio
async def test_cap_bounds_a_server_stuck_in_starting():
    async def feed(q):
        await _push(q, [_status("stuck", "starting")])
        await asyncio.sleep(10)

    res, elapsed = await _gate(["stuck"], feed, cap=0.4)
    assert 0.4 <= elapsed < 0.7
    assert res.pending == ["stuck"]


@pytest.mark.asyncio
async def test_daemon_exit_returns_at_once():
    async def feed(q):
        await _push(q, [_status("a", "starting"), ("__daemon_exit__", {})])

    res, elapsed = await _gate(["a"], feed)
    assert elapsed < 0.05 and res.daemon_exited is True


def test_mcp_server_names_from_toml():
    toml = (
        "[features]\nplugins = false\n\n"
        "[mcp_servers.task-mcp]\ncommand = \"python3\"\n"
        "env = { \"A\" = \"1\" }\n\n"
        "[mcp_servers.task-mcp.env]\nB = \"2\"\n\n"
        '[mcp_servers."file.tools"]\nurl = "http://x"\n\n'
        "[mcp_servers.github.http_headers]\nAuthorization = \"Bearer x\"\n\n"
        "[mcp_servers.github]\nurl = \"http://y\"\n"
        "[mcp_servers.task-mcp]\ncommand = \"again\"\n"
    )
    assert mcp_server_names_from_toml(toml) == ["task-mcp", "file.tools", "github"]
    assert mcp_server_names_from_toml("") == []
    assert mcp_server_names_from_toml("[memories]\nuse_memories = false\n") == []
