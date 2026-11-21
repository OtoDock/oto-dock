"""Direct LLM's own client starts a tool at its first call
(``core/layers/direct/mcp.py``): a marked stdio entry whose session scope
has answers saved lists its tools from them with no program running, starts
the program at the first call (the session's box judging it), says in one
sentence why a start failed, takes the session's bundle as it stands at that
start, and keeps a program's answers when it starts with the session."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from auth.path_policy import SecurityContext
from core import concurrency
from core.credentials import mcp_broker
from core.layers.direct import mcp as direct_mcp
from core.session import session_state
from services.mcp import tool_listing
from tests._compute import COMPUTE, tools_run_here

SID = "late-start-session-0000000000000001"
MARKER = json.dumps({"v": 1, "stamp": "1", "account": "", "fetch": False, "label": "Echo"})

# A tool program that notes each start in a file and answers its tool with
# the value of FAKE_SECRET it was started with.
PROGRAM = """
import os
from mcp.server.fastmcp import FastMCP

with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(f"start {os.getpid()}\\n")
mcp = FastMCP("echo")


@mcp.tool()
def echo(q: str = "") -> str:
    return "secret=" + os.environ.get("FAKE_SECRET", "")


mcp.run()
"""

SAVED = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "echo", "version": "1"}},
         "lists": {"tools/list": [{"name": "echo", "description": "Echo it.",
                                   "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}}


class _NoWrap:
    class cfg:
        username = "alice"
        role = "editor"

    def get_env_overrides(self):
        return {}

    def describe(self):
        return SimpleNamespace(to_dict=lambda: {})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    program = tmp_path / "echo_server.py"
    program.write_text(PROGRAM)
    log = tmp_path / "starts.log"

    def config(*, die=False, warm=False):
        command = [sys.executable, "-c", "import sys; sys.exit(3)"] if die else [sys.executable, str(program)]
        cfg = tmp_path / "mcp.json"
        marker = json.dumps({**json.loads(MARKER), "warm": True}) if warm else MARKER
        cfg.write_text(json.dumps({"mcpServers": {"echo-mcp": {
            "type": "stdio", "command": command[0], "args": command[1:],
            "env": {"FAKE_LOG": str(log), direct_mcp.STANDIN_ENV: marker}}}}))
        return cfg

    ctx = SecurityContext(role="editor", username="alice", agent="a", is_admin_agent=False)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx if sid == SID else None)
    kept, saved = [], {"listing": SAVED}
    monkeypatch.setattr(tool_listing, "saved", lambda scope, server, fp: saved["listing"])
    monkeypatch.setattr(tool_listing, "keep", lambda *a: kept.append(a) or True)
    mcp_broker.provision(SID, {"echo-mcp": mcp_broker.SecretBundle(env={"FAKE_SECRET": "first"})})
    # The program runs on a compute machine: the relay's stand-in starts it here.
    tools_run_here(monkeypatch)

    def manager(*, lazy=True, die=False, warm=False):
        # A call's manager starts its programs at once: its bundles ride its
        # config, as the session's builder hands them.
        bundles = {"echo-mcp": mcp_broker.SecretBundle(env={"FAKE_SECRET": "first"})} if warm else None
        return direct_mcp.AgentMCPManager("a", session_id=SID, sandbox_builder=_NoWrap(),
                                          prebuilt_config=(config(die=die, warm=warm), bundles), lazy=lazy,
                                          machine_id=COMPUTE["id"])

    def starts():
        return log.read_text().count("start") if log.exists() else 0

    yield manager, starts, kept, saved
    mcp_broker.purge_session(SID)


def _run(coro):
    return asyncio.run(coro)


def test_a_saved_listing_lists_the_tools_with_no_program_and_the_first_call_starts_it(rig):
    manager, starts, kept, saved = rig

    async def main():
        # What the program answers, as a first session keeps it.
        saved["listing"] = None
        first = manager()
        await first.start()
        await first.close()
        saved["listing"] = kept.pop()[3]
        mgr = manager()
        await mgr.start()
        try:
            assert [t["name"] for t in mgr.get_tools()] == ["mcp__echo-mcp__echo"]
            assert mgr.has_tool("mcp__echo-mcp__echo")
            assert starts() == 1
            assert await direct_mcp.mcp_pool.active_mcp_names() == set()
            # The session's bundle as it stands at the start (a rebuild's
            # placeholders, EXIT-CREDS' hand-off), not as it stood at build.
            mcp_broker.provision(SID, {"echo-mcp": mcp_broker.SecretBundle(env={"FAKE_SECRET": "now"})})
            out = await mgr.execute_tool("mcp__echo-mcp__echo", {"q": "x"})
            assert out == "secret=now"
            assert await mgr.execute_tool("mcp__echo-mcp__echo", {}) == "secret=now"
            assert starts() == 2  # the first session's, then this one's at its first call
            # The program answered what was saved: nothing new kept.
            assert kept == []
        finally:
            await mgr.close()

    _run(main())


def test_a_program_that_cannot_start_says_so_and_keeps_its_tools(rig, admission):
    manager, starts, _kept, _saved = rig
    C, track = admission

    async def main():
        track(200)
        mgr = manager(die=True)
        await mgr.start()
        try:
            out = await mgr.execute_tool("mcp__echo-mcp__echo", {})
            assert out == "The Echo tool did not start. Its program stopped before it answered."
            assert [t["name"] for t in mgr.get_tools()] == ["mcp__echo-mcp__echo"]
            # The program its box counted for the start is given back.
            assert C._session_est[SID] == 200
        finally:
            await mgr.close()

    _run(main())


def test_a_program_that_fails_with_its_session_is_given_back(rig, admission):
    manager, starts, _kept, saved = rig
    saved["listing"] = None
    C, track = admission

    async def main():
        track(200)
        mgr = manager(die=True)
        await mgr.start()
        try:
            assert C._session_est[SID] == 200 and C._reserved_mb == 200
        finally:
            await mgr.close()

    _run(main())


def test_a_close_during_a_late_start_ends_the_program_it_opened(rig, monkeypatch, tmp_path):
    """The manager closes while a first call's start waits for its box: the
    close waits for that start and ends what it opened."""
    manager, starts, _kept, saved = rig

    async def slow(self, *, judged=True):
        await asyncio.sleep(1.0)
        return None

    monkeypatch.setattr(direct_mcp.MCPServerConnection, "_admit", slow)

    async def main():
        mgr = manager()
        await mgr.start()
        call = asyncio.create_task(mgr.execute_tools([{"id": "c1", "name": "mcp__echo-mcp__echo", "input": {}}]))
        await asyncio.sleep(0.3)
        await mgr.close()
        out = await call
        assert "did not start" in out[0]["content"], out
        return out

    _run(main())
    pids = [int(line.split()[1]) for line in (tmp_path / "starts.log").read_text().splitlines()
            if line.startswith("start ")]
    assert pids, "the start ran"
    deadline = time.monotonic() + 5
    while any(os.path.exists(f"/proc/{pid}") and "Z" not in open(f"/proc/{pid}/stat").read().split()[2]
              for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(os.path.exists(f"/proc/{pid}") and "Z" not in open(f"/proc/{pid}/stat").read().split()[2]
                   for pid in pids), "the program it opened outlived the close"


def test_a_full_box_refuses_the_start_in_one_sentence(rig, monkeypatch):
    manager, starts, _kept, _saved = rig

    async def refused(self):
        return concurrency._DENY_TOOL_MEMORY

    monkeypatch.setattr(direct_mcp.MCPServerConnection, "_admit", refused)

    async def main():
        mgr = manager()
        await mgr.start()
        try:
            out = await mgr.execute_tool("mcp__echo-mcp__echo", {})
            assert out == concurrency._DENY_TOOL_MEMORY.user_message
            assert starts() == 0
        finally:
            await mgr.close()

    _run(main())


@pytest.fixture
def admission(monkeypatch):
    """The admission's ledger for this test's loop, the walk's session
    tracked at the Direct floor (its marked tools counted late)."""
    from core import concurrency as C, concurrency_scan
    monkeypatch.setattr(C, "_cond", None)
    monkeypatch.setattr(C, "_reserved_mb", 0)
    for name in ("_sessions", "_session_est", "_session_added_at", "_session_owner", "_session_box"):
        monkeypatch.setattr(C, name, {})
    monkeypatch.setattr(concurrency_scan, "_grow_debits", type(concurrency_scan._grow_debits)())
    monkeypatch.setattr(C, "_grow_debits", concurrency_scan._grow_debits)
    monkeypatch.setattr(C, "_session_grown", {})
    monkeypatch.setattr(C, "_budget_mb", 100_000)
    monkeypatch.setattr(C, "_floor_mb", 0)
    monkeypatch.setattr(C, "_live_cache", None)
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)

    def track(est: int = 200) -> None:
        C._cond = asyncio.Condition()
        C._add(SID, "chat", est)
    return C, track


def test_with_nothing_saved_it_starts_with_the_session_and_its_answers_are_kept(rig, admission):
    """A program started only for its answers stops once they are kept, as
    the CLI engines' stand-in stops it: its tools stay listed, its room goes
    back, and its first call starts it again."""
    manager, starts, kept, saved = rig
    saved["listing"] = None
    C, track = admission

    async def main():
        track(200)
        mgr = manager()
        await mgr.start()
        try:
            assert starts() == 1
            assert len(kept) == 1
            scope, server, fp, listing = kept[0]
            assert scope == ("a", "alice") and server == "echo-mcp" and len(fp) == 64
            assert listing["lists"]["tools/list"][0]["name"] == "echo"
            conn = mgr.servers["echo-mcp"]
            assert conn.session is None and conn.pending
            assert [t["name"] for t in mgr.get_tools()] == ["mcp__echo-mcp__echo"]
            # Counted while it ran (grow, not judged), given back once stopped.
            assert C._session_est[SID] == 200
            # The session's bundle as it stands at the late start (the rig's
            # manager provisions none of its own).
            mcp_broker.provision(SID, {"echo-mcp": mcp_broker.SecretBundle(env={"FAKE_SECRET": "now"})})
            assert await mgr.execute_tool("mcp__echo-mcp__echo", {"q": "x"}) == "secret=now"
            assert starts() == 2
        finally:
            await mgr.close()

    _run(main())


def test_a_program_whose_answers_were_not_kept_runs_on(rig, admission, monkeypatch):
    """Nothing kept (a store that refused): the next session would start it
    again anyway, so it keeps running for this one."""
    manager, starts, _kept, saved = rig
    saved["listing"] = None
    monkeypatch.setattr(tool_listing, "keep", lambda *a: False)
    C, track = admission

    async def main():
        track(200)
        mgr = manager()
        await mgr.start()
        try:
            assert starts() == 1 and mgr.servers["echo-mcp"].session is not None
            assert C._session_est[SID] == 200 + 65
        finally:
            await mgr.close()

    _run(main())


def test_a_calls_tool_lists_at_once_and_starts_beside_the_model(rig, admission):
    """A call's tool (a warm marker) with answers saved: the model has its
    list at once, its program starts with no call, and a call meanwhile waits
    for that start. The call's estimate counted it: nothing is grown."""
    manager, starts, _kept, _saved = rig
    C, track = admission

    async def main():
        track(265)
        mgr = manager(warm=True)
        began = time.monotonic()
        await mgr.start()
        call = [{"id": "c1", "name": "mcp__echo-mcp__echo", "input": {}}]
        try:
            assert [t["name"] for t in mgr.get_tools()] == ["mcp__echo-mcp__echo"]
            # The model's calls run on the tools' own loop, as the start does.
            assert (await mgr.execute_tools(call))[0]["content"] == "secret=first"
            assert starts() == 1
            assert C._session_est[SID] == 265
            assert (await mgr.execute_tools(call))[0]["content"] == "secret=first"
            assert starts() == 1
        finally:
            await mgr.close()
        assert time.monotonic() - began < 30

    _run(main())


def test_a_calls_tool_starts_even_when_nothing_calls_it(rig, admission):
    manager, starts, _kept, _saved = rig
    C, track = admission

    async def main():
        track(265)
        mgr = manager(warm=True)
        await mgr.start()
        try:
            def started() -> bool:
                deadline = time.monotonic() + 20
                while starts() == 0 and time.monotonic() < deadline:
                    time.sleep(0.1)
                return starts() == 1

            assert await asyncio.to_thread(started) and C._session_est[SID] == 265
        finally:
            await mgr.close()

    _run(main())


def test_a_calls_tool_with_nothing_saved_is_not_counted_twice(rig, admission):
    manager, starts, kept, saved = rig
    saved["listing"] = None
    C, track = admission

    async def main():
        track(265)
        mgr = manager(warm=True)
        await mgr.start()
        try:
            assert starts() == 1 and C._session_est[SID] == 265 and len(kept) == 1
        finally:
            await mgr.close()

    _run(main())


def test_the_listing_job_and_an_app_button_start_what_they_call_at_once(rig):
    manager, starts, _kept, _saved = rig

    async def main():
        mgr = manager(lazy=False)
        await mgr.start()
        try:
            assert starts() == 1
        finally:
            await mgr.close()

    _run(main())
