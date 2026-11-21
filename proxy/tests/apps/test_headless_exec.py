"""Headless mcp_tool executor: real stdio-MCP end-to-end (spawn → call →
result), synthetic-session registration + full teardown, pool keying by
scope identity (the credential boundary), the personal-owner fail-closed
guard, self-heal on unknown tools, and the idle reaper.

``_build_session_parts`` is stubbed (the blocking identity/sandbox build
composes ``resolve_task_identity``/``resolve_visibility``/
``resolve_sandbox_config``, each covered by its own suite); everything from
the manager up runs live here. The tool's process runs on a compute machine:
the relay's stand-in (``tests._compute.tools_run_here``) starts the REAL
fake MCP over stdio in the machine's place.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import config
from auth.path_policy import SecurityContext
from core.session.session_state import get_session_security
from services.apps import headless_exec as hx
from storage import database as task_store
from tests._compute import placed, tools_run_here

AGENT = "hx-agent"

FAKE_SERVER = """\
import json, os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def echo(text: str = "", flag: bool = False) -> str:
    return json.dumps({"text": text, "flag": flag,
                       "sid": os.environ.get("OTO_SESSION_ID", "")})


@mcp.tool()
def nap(seconds: float = 0) -> str:
    import time
    time.sleep(seconds)
    return "awake"


mcp.run()
"""


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    hx._pool.clear()
    hx._key_locks.clear()
    hx._inflight.clear()
    hx._selfheal_at.clear()
    hx._start_sem = asyncio.Semaphore(hx.START_CONCURRENCY)
    # No background sweeper in tests — reap is driven via _sweep_once.
    monkeypatch.setattr(hx, "_ensure_sweeper", lambda: None)
    yield
    # Every test must dispose what it started (close_all inside its loop) —
    # a leaked entry means leaked MCP subprocesses.
    assert not hx._pool


class _NoWrap:
    """Stand-in SandboxBuilder: the description a compute machine would
    render, which the relay's stand-in never reads."""
    class cfg:
        username = "alice"
        role = "manager"
        # Every builder's config names its host config dir: the credential
        # exit's attach reads it (the stand-in's machine runs no exit).
        host_claude_dir = "/nonexistent"

    def get_env_overrides(self):
        return {}

    def describe(self):
        return SimpleNamespace(to_dict=lambda: {})


@pytest.fixture
def fake_mcp(tmp_path, monkeypatch):
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER)
    cfg = tmp_path / "mcp.json"
    # Two servers in the agent's config; a manager starts only the ones the
    # app's manifest names (rows without a manifest resolve to the action's).
    cfg.write_text(json.dumps({"mcpServers": {
        "test-mcp": {"type": "stdio", "command": sys.executable, "args": [str(server)]},
        "other-mcp": {"type": "stdio", "command": sys.executable, "args": [str(server)]},
    }}))

    def fake_build(agent, row, mcps, session_id="", phone=False, reach=hx.FULL):
        assert session_id.startswith("appx-") and phone is False
        ctx = SecurityContext(role="manager", username=row.get("username") or "",
                              agent=agent, is_admin_agent=False, placement=placed())
        return cfg, {}, {}, _NoWrap(), ctx

    monkeypatch.setattr(hx, "_build_session_parts", fake_build)
    tools_run_here(monkeypatch)
    return cfg


def _shared_row() -> dict:
    return {"id": "hx-app-1", "agent": AGENT, "slug": "board",
            "username": "", "owner_sub": None}


def _personal_row(username: str = "hx-alice", sub: str = "hx-alice-sub") -> dict:
    return {"id": "hx-app-2", "agent": AGENT, "slug": "mine",
            "username": username, "owner_sub": sub}


ACTION = {"id": "run", "label": "Run", "type": "mcp_tool",
          "mcp": "test-mcp", "tool": "echo"}
OTHER_ACTION = {"id": "run2", "label": "Run 2", "type": "mcp_tool",
                "mcp": "other-mcp", "tool": "echo"}


def _with_manifest(row: dict, *actions: dict) -> dict:
    return dict(row, actions=json.dumps(list(actions)))


def test_execute_end_to_end_and_reuse(fake_mcp):
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION,
                                      {"text": "hi", "flag": True}, reach=hx.FULL)
        assert r["status"] == "done"
        payload = json.loads(r["result"])
        assert payload["text"] == "hi" and payload["flag"] is True
        # The subprocess ran under the SYNTHETIC session id…
        assert payload["sid"].startswith("appx-")
        # …whose security context is registered for hook callbacks.
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        assert entry.session_id == payload["sid"]
        assert get_session_security(entry.session_id) is not None

        # Warm reuse: second click, same manager (no respawn).
        first_manager = entry.manager
        r2 = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"}, reach=hx.FULL)
        assert json.loads(r2["result"])["text"] == "again"
        assert hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager is first_manager

        sid = entry.session_id
        await hx.close_all()
        assert hx._pool == {}
        # Full synthetic-session teardown — context gone, JWT replay denied.
        assert get_session_security(sid) is None

    asyncio.run(main())


def test_pool_keyed_by_scope_identity(fake_mcp):
    task_store.upsert_user("hx-alice-sub", "hx-alice@test.com", "Alice", "member")
    task_store.add_user_agent("hx-alice-sub", AGENT, "manager", "test")

    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        await hx.execute_app_tool(_personal_row(), ACTION, {}, reach=hx.FULL)
        # Personal and shared managers never share a key (credential boundary).
        assert set(hx._pool) == {(AGENT, "", "test-mcp", hx.FULL), (AGENT, "hx-alice-sub", "test-mcp", hx.FULL)}
        assert (hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager
                is not hx._pool[(AGENT, "hx-alice-sub", "test-mcp", hx.FULL)].manager)
        await hx.close_all()

    asyncio.run(main())


def test_personal_owner_must_still_hold_access(fake_mcp):
    task_store.upsert_user("hx-bob-sub", "hx-bob@test.com", "Bob", "member")
    # Bob holds NO role on the agent (unassigned since pin/approval).
    row = _personal_row(username="hx-bob", sub="hx-bob-sub")

    async def main():
        r = await hx.execute_app_tool(row, ACTION, {}, reach=hx.FULL)
        assert r["status"] == "error"
        assert "no longer has access" in r["reason"]
        assert hx._pool == {}  # denied BEFORE any manager was built

    asyncio.run(main())


def test_unknown_tool_on_fresh_build_never_rebuilds(fake_mcp):
    """A manager built by THIS call that lacks the tool means the MCP just
    failed to start — rebuilding immediately would double the cold-start
    cost for nothing."""
    async def main():
        assert hx._pool == {}
        bad = dict(ACTION, tool="nope")
        r = await hx.execute_app_tool(_shared_row(), bad, {}, reach=hx.FULL)
        assert r["status"] == "error" and "not available" in r["reason"]
        assert hx._selfheal_at == {}  # no self-heal was attempted
        await hx.close_all()

    asyncio.run(main())


def test_dead_server_self_heals_on_next_click(fake_mcp):
    """A server that died AFTER tool discovery (transport failure — the
    uptime-kuma case: startup auth failed against a blocked port, warm
    manager kept erroring forever) reports its tools missing via has_tool,
    so the next click rebuilds and succeeds."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager
        first.servers["test-mcp"].dead = True  # what a failed call_tool sets

        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"}, reach=hx.FULL)
        assert r["status"] == "done"
        assert json.loads(r["result"])["text"] == "again"
        assert hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager is not first  # rebuilt
        await hx.close_all()

    asyncio.run(main())


def test_a_call_that_finds_the_session_gone_rebuilds_once_and_presses_again(fake_mcp, monkeypatch):
    """The github sidecar evicts an idle session while the manager sits warm
    in the pool; the next press's call answers "Session terminated" and the
    connection marks itself dead. The press rebuilds once and presses again,
    so the hourly wake after a quiet stretch is not the one that fails."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager

        async def gone(calls):
            first.servers["test-mcp"].dead = True      # what call_tool sets on "Session terminated"
            return [{"id": "app-action", "content": "Error calling tool 'echo': Session terminated"}]
        monkeypatch.setattr(first, "execute_tools", gone)

        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"}, reach=hx.FULL)
        assert r["status"] == "done" and json.loads(r["result"])["text"] == "again"
        assert hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager is not first   # rebuilt within the press
        await hx.close_all()

    asyncio.run(main())


def test_self_heal_rebuilds_warm_manager_once_per_cooldown(fake_mcp):
    """A WARM manager missing the tool is dropped and rebuilt (the MCP may
    have been re-enabled since it was built) — but at most once per
    cooldown, so a permanently-flaky MCP can't turn every click into a
    full manager rebuild (found live on the trusted VM: uptime-kuma)."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager

        bad = dict(ACTION, tool="nope")
        r = await hx.execute_app_tool(_shared_row(), bad, {}, reach=hx.FULL)
        assert r["status"] == "error" and "not available" in r["reason"]
        second = hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager
        assert second is not first  # self-heal rebuilt the manager

        r = await hx.execute_app_tool(_shared_row(), bad, {}, reach=hx.FULL)
        assert r["status"] == "error"
        assert hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager is second  # cooldown: no rebuild

        # Cooldown elapsed → the self-heal path opens again.
        hx._selfheal_at[(AGENT, "", "test-mcp", hx.FULL)] -= hx._SELF_HEAL_COOLDOWN_S + 1
        r = await hx.execute_app_tool(_shared_row(), bad, {}, reach=hx.FULL)
        assert r["status"] == "error"
        assert hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager is not second
        await hx.close_all()

    asyncio.run(main())


def test_inflight_duplicate_rejected_but_distinct_args_pass(fake_mcp):
    """The flight key is args-aware: an IDENTICAL repeat is rejected while
    the first call runs, but the same declared action with different args
    (one parameterized action serving many widgets) is an independent call."""
    import hashlib as _hl
    import json as _json

    def _fp(args: dict) -> str:
        return _hl.sha256(_json.dumps(
            args, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:16]

    async def main():
        hx._inflight.add(("hx-app-1", f"run|{_fp({})}"))
        try:
            r = await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
            assert r["status"] == "error" and "already running" in r["reason"]
            # Same action id, different args → runs (real subprocess call).
            r2 = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "b"}, reach=hx.FULL)
            assert r2["status"] == "done"
        finally:
            hx._inflight.clear()
        await hx.close_all()

    asyncio.run(main())


def test_result_truncation(fake_mcp, monkeypatch):
    monkeypatch.setattr(hx, "RESULT_MAX_CHARS", 8)

    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION,
                                      {"text": "0123456789abcdef"}, reach=hx.FULL)
        assert r["status"] == "done"
        assert r["result"].endswith("(result truncated)")
        await hx.close_all()

    asyncio.run(main())


def test_idle_reap_full_teardown(fake_mcp, monkeypatch):
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        sid = entry.session_id
        monkeypatch.setattr(config, "get_idle_timeout", lambda: 1)
        entry.last_used -= 10
        entry.manager.last_activity -= 10
        await hx._sweep_once()
        assert hx._pool == {}
        assert get_session_security(sid) is None

    asyncio.run(main())


def test_manager_starts_only_the_manifest_mcps(fake_mcp):
    """The agent's config lists two servers; an app naming one starts one
    (a connector-heavy agent's dashboard must not spawn its whole roster),
    and the pool key carries the set."""
    row = _with_manifest(_shared_row(), ACTION)

    async def main():
        r = await hx.execute_app_tool(row, ACTION, {"text": "one"}, reach=hx.FULL)
        assert r["status"] == "done"
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        assert entry.mcps == frozenset({"test-mcp"})
        assert set(entry.manager.servers) == {"test-mcp"}
        assert entry.manager.included_mcps == {"test-mcp"}
        await hx.close_all()

    asyncio.run(main())


def test_covering_manager_is_reused_across_apps(fake_mcp):
    """Two apps of one identity share a manager when one set covers the
    other; a set nobody covers builds its own entry."""
    both = _with_manifest(dict(_shared_row(), id="hx-both"), ACTION, OTHER_ACTION)
    one = _with_manifest(dict(_shared_row(), id="hx-one"), ACTION)

    async def main():
        await hx.execute_app_tool(both, OTHER_ACTION, {"text": "b"}, reach=hx.FULL)
        assert set(hx._pool) == {(AGENT, "", "other-mcp,test-mcp", hx.FULL)}
        big = hx._pool[(AGENT, "", "other-mcp,test-mcp", hx.FULL)]
        assert set(big.manager.servers) == {"other-mcp", "test-mcp"}

        r = await hx.execute_app_tool(one, ACTION, {"text": "o"}, reach=hx.FULL)
        assert r["status"] == "done"
        assert set(hx._pool) == {(AGENT, "", "other-mcp,test-mcp", hx.FULL)}  # reused

        # The reverse order builds a second, exact entry: the small manager
        # cannot serve the bigger app.
        await hx.close_all()
        await hx.execute_app_tool(one, ACTION, {"text": "o"}, reach=hx.FULL)
        await hx.execute_app_tool(both, OTHER_ACTION, {"text": "b"}, reach=hx.FULL)
        assert set(hx._pool) == {(AGENT, "", "test-mcp", hx.FULL),
                                 (AGENT, "", "other-mcp,test-mcp", hx.FULL)}
        await hx.close_all()

    asyncio.run(main())


def test_self_heal_waits_for_the_entry_to_go_idle(fake_mcp):
    """A rebuild for a missing tool must not dispose the manager under a
    sibling call (a batch fans out on one manager): it waits for busy to
    drop, then rebuilds."""
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        entry.manager.servers["test-mcp"].dead = True
        entry.busy += 1  # a sibling still running

        async def release():
            await asyncio.sleep(0.3)
            entry.busy -= 1

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        releaser = loop.create_task(release())
        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"}, reach=hx.FULL)
        await releaser
        assert r["status"] == "done"
        assert loop.time() - t0 >= 0.3
        assert hx._pool[(AGENT, "", "test-mcp", hx.FULL)].manager is not entry.manager
        await hx.close_all()

    asyncio.run(main())


def test_the_idle_wait_reads_the_count_again_after_a_wake():
    """A call that starts between the last one's end and the waiter's wake
    keeps the entry busy: the self-heal waits for that call too."""
    class _Manager:
        tool_timeout = 5

    async def main():
        entry = hx._Entry(_Manager(), "sid", "", frozenset())
        assert entry.idle.is_set()
        entry.busy += 1
        waiter = asyncio.create_task(hx._wait_idle(entry))
        await asyncio.sleep(0)
        entry.busy -= 1          # the event is set ...
        entry.busy += 1          # ... and a sibling starts before the waiter runs
        await asyncio.sleep(0.05)
        assert not waiter.done()
        entry.busy -= 1
        await asyncio.wait_for(waiter, 1)

    asyncio.run(main())


def test_calls_on_one_manager_fan_out_up_to_the_cap(fake_mcp, monkeypatch):
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {}, reach=hx.FULL)
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        peak = {"now": 0, "max": 0}

        async def slow_execute(calls):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.05)
            peak["now"] -= 1
            return [{"tool_use_id": c["id"], "content": "ok"} for c in calls]

        monkeypatch.setattr(entry.manager, "execute_tools", slow_execute)
        results = await asyncio.gather(*[
            hx.execute_app_tool(_shared_row(), ACTION, {"text": str(i)}, reach=hx.FULL)
            for i in range(8)
        ])
        assert all(r["status"] == "done" for r in results)
        assert peak["max"] == hx.CALL_CONCURRENCY
        await hx.close_all()

    asyncio.run(main())


def test_warm_builds_pins_and_extends_idle(fake_mcp, monkeypatch):
    row = _with_manifest(_shared_row(), ACTION)

    async def main():
        # Nothing to build for an app with no tool buttons.
        assert await hx.warm(_shared_row()) is False
        assert hx._pool == {}

        assert await hx.warm(row) is True
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        assert entry.idle_s == hx.KEEP_WARM_S
        assert entry.pinned_until > 0
        # The platform idle timeout no longer reaps it…
        monkeypatch.setattr(config, "get_idle_timeout", lambda: 1)
        entry.last_used -= 10
        entry.manager.last_activity -= 10
        await hx._sweep_once()
        assert (AGENT, "", "test-mcp", hx.FULL) in hx._pool
        # …the keep-warm grace does.
        entry.idle_s = 1
        await hx._sweep_once()
        assert hx._pool == {}

    asyncio.run(main())


def test_pool_cap_evicts_unpinned_before_pinned(fake_mcp, monkeypatch):
    task_store.upsert_user("hx-alice-sub", "hx-alice@test.com", "Alice", "member")
    task_store.add_user_agent("hx-alice-sub", AGENT, "manager", "test")
    monkeypatch.setattr(hx, "POOL_MAX", 2)
    pinned_row = _with_manifest(_shared_row(), ACTION)
    other_row = _with_manifest(dict(_shared_row(), id="hx-o"), OTHER_ACTION)

    async def main():
        assert await hx.warm(pinned_row) is True
        await hx.execute_app_tool(_personal_row(), ACTION, {}, reach=hx.FULL)
        await hx.execute_app_tool(other_row, OTHER_ACTION, {}, reach=hx.FULL)
        # The personal (unpinned, older) entry went; the pinned one stayed.
        assert set(hx._pool) == {(AGENT, "", "test-mcp", hx.FULL), (AGENT, "", "other-mcp", hx.FULL)}
        await hx.close_all()

    asyncio.run(main())


# ── the account a call runs with (APPS.md "Account arguments") ──────────────


def test_account_email_fills_the_argument_for_the_identity_the_call_runs_with(monkeypatch):
    """`${account.email}` becomes the connected account of the identity the
    button runs with: the agent's service binding for a shared app, the
    owner's account for a personal one; nothing connected → the token stays
    (the tool's refusal then names it); other values are untouched."""
    from services.oauth import credential_resolver
    from storage.identity import credential_store
    asked: list = []

    class _Ref:
        def __init__(self, label, owner):
            self.label, self.owner_sub = label, owner

    def _pick(mcp, agent, *, user_sub=""):
        asked.append((mcp, agent, user_sub))
        if mcp != "google-workspace":
            return None
        return _Ref("svc", "owner-sub") if not user_sub else _Ref("mine", user_sub)

    def _accounts(owner, mcp):
        return [{"account_label": "svc", "display_email": "agent@example.com"},
                {"account_label": "mine", "display_email": ""}]
    monkeypatch.setattr(credential_resolver, "pick_account", _pick)
    monkeypatch.setattr(credential_store, "list_user_accounts", _accounts)
    action = {"id": "mail", "type": "mcp_tool", "mcp": "google-workspace", "tool": "search"}
    shared = {"id": "a", "agent": AGENT, "username": "", "owner_sub": None}
    personal = {"id": "b", "agent": AGENT, "username": "alice", "owner_sub": "alice-sub"}
    assert hx.runs_as(shared) == ("", "the agent") and hx.runs_as(personal) == ("alice-sub", "the owner")
    out = hx.fill_account_args(shared, action, {"user_google_email": "${account.email}", "query": "is:unread"})
    assert out == {"user_google_email": "agent@example.com", "query": "is:unread"}
    assert asked[-1] == ("google-workspace", AGENT, "")
    # The owner's account, by label when it has no email on file.
    out = hx.fill_account_args(personal, action, {"user_google_email": "${account.email}"})
    assert out == {"user_google_email": "mine"} and asked[-1][2] == "alice-sub"
    # No account: the token stays; no token: no lookup at all.
    other = {**action, "mcp": "other-mcp"}
    assert hx.fill_account_args(shared, other, {"x": "${account.email}"}) == {"x": "${account.email}"}
    n = len(asked)
    assert hx.fill_account_args(shared, action, {"query": "plain"}) == {"query": "plain"} and len(asked) == n
    assert hx.connected_account("google-workspace", AGENT, "") == "agent@example.com"


def test_the_pooled_session_is_held_while_live_and_closing_after(fake_mcp):
    from core.session import session_state

    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "hi"}, reach=hx.FULL)
        sid = json.loads(r["result"])["sid"]
        assert session_state.session_is_held(sid)
        await hx.close_all()
        assert not session_state.session_is_held(sid)
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()

    asyncio.run(main())


def test_an_action_on_a_compute_machine_hands_the_machine_to_its_tool_manager(monkeypatch):
    """The actions' tools run where a Direct LLM session's do, the machine
    read from the record the session's context carries."""
    from core import placement
    from core.session import session_state
    made: dict = {}

    class _Manager:
        def __init__(self, agent, **kw):
            made.update(kw)

        async def start(self):
            return None

    ctx = SimpleNamespace(placement=placement.from_machine(
        placement.KIND_COMPUTE, {"id": "compute-1", "pairing_scope": "compute"}))
    monkeypatch.setattr(hx, "AgentMCPManager", _Manager)
    monkeypatch.setattr(hx, "_build_session_parts", lambda *a, **k: ("cfg", {}, {}, _NoWrap(), ctx))
    monkeypatch.setattr(session_state, "set_session_security", lambda *a, **k: None)
    monkeypatch.setattr(session_state, "mark_headless_live", lambda *a, **k: None)
    monkeypatch.setattr(session_state, "_record_session_use", lambda *a, **k: None)
    asyncio.run(hx.open_manager(AGENT, {}, frozenset({"test-mcp"})))
    assert made["machine_id"] == "compute-1"


def _exit_compute(*, version="0.5.175", port=41112, ok=True):
    from core import placement
    return placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="compute-1", label="Compute one",
                                           satellite_version=version, exit_port=port, exit_ok=ok)


def _open_with_fakes(monkeypatch, where, *, start=None) -> dict:
    """``open_manager`` with the build, the session's records and the
    manager faked: what the manager was handed, its builder and its id."""
    from core.session import session_state
    made: dict = {}
    builder = SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=("10.0.0.5",), host_claude_dir="/x"),
                              exit_port=0, ssh_agent="", get_env_overrides=lambda: {})

    class _Manager:
        def __init__(self, agent, **kw):
            made.update(kw)

        async def start(self):
            if start is not None:
                await start()

        async def close(self):
            made["closed"] = True

    ctx = SimpleNamespace(placement=where)
    monkeypatch.setattr(hx, "AgentMCPManager", _Manager)
    monkeypatch.setattr(hx, "_build_session_parts", lambda *a, **k: ("cfg", {}, {}, builder, ctx))
    monkeypatch.setattr(session_state, "set_session_security", lambda *a, **k: None)
    monkeypatch.setattr(session_state, "mark_headless_live", lambda *a, **k: None)
    monkeypatch.setattr(session_state, "_record_session_use", lambda *a, **k: None)
    made["builder"] = builder
    return made


@pytest.fixture
def no_proxy_set(monkeypatch):
    from core.credentials import credential_exit
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
    for sid in list(credential_exit._sessions):
        credential_exit.forget(sid)


def test_an_actions_tools_get_the_compute_machines_exit(no_proxy_set, monkeypatch):
    """A button's tools and the listing's run on the compute machine as a
    Direct LLM session's do, and they get its exit the same way: the
    door's secret their open carries, the variables their processes take,
    the port the description splices. A placeholder then always has an
    exit to fill it."""
    from core.credentials import credential_exit
    made = _open_with_fakes(monkeypatch, _exit_compute())
    entry = asyncio.run(hx.open_manager(AGENT, {}, frozenset({"test-mcp"})))
    sid = made["session_id"]
    assert entry.session_id == sid
    secret = credential_exit.machine_secret(sid)
    assert secret and credential_exit.env_of(sid)["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:41443"
    assert "10.0.0.5" in credential_exit.env_of(sid)["NO_PROXY"]
    assert made["builder"].exit_port == 41443


@pytest.mark.parametrize("where", [_exit_compute(port=0), _exit_compute(ok=False)])
def test_a_machine_the_rule_refuses_gives_an_actions_tools_no_exit(no_proxy_set, monkeypatch, where):
    """Where the one machine rule refuses the machine the attach gives no
    exit, reading the same answer the build's leave-out reads."""
    from core.credentials import credential_exit
    made = _open_with_fakes(monkeypatch, where)
    asyncio.run(hx.open_manager(AGENT, {}, frozenset({"test-mcp"})))
    assert credential_exit.machine_secret(made["session_id"]) == ""
    assert credential_exit.env_of(made["session_id"]) == {} and made["builder"].exit_port == 0


def test_a_placement_that_is_no_compute_machine_attaches_no_exit(no_proxy_set, monkeypatch):
    """The platform starts no tool itself, and the exit is a machine's: with
    no compute machine in the record nothing of the exit is attached."""
    from core import placement
    from core.credentials import credential_exit
    made = _open_with_fakes(monkeypatch, placement.LOCAL_PLACEMENT)
    asyncio.run(hx.open_manager(AGENT, {}, frozenset({"test-mcp"})))
    assert made["session_id"] not in credential_exit._sessions and made["builder"].exit_port == 0


def test_a_cancelled_start_closes_the_session_and_its_exit(no_proxy_set, monkeypatch):
    """The listing bounds the start at 30 s and cancels it: the session, its
    bundles and its exit go as a failed start's do, never left to a tick
    that would renew its values on the machine."""
    from core.credentials import credential_exit
    closed: list[str] = []

    async def hang():
        await asyncio.sleep(3600)

    made = _open_with_fakes(monkeypatch, _exit_compute(), start=hang)

    async def close(entry):
        closed.append(entry.session_id)
        credential_exit.forget(entry.session_id)

    monkeypatch.setattr(hx, "close_manager", close)

    async def main():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(hx.open_manager(AGENT, {}, frozenset({"test-mcp"})), timeout=0.2)

    asyncio.run(main())
    assert closed == [made["session_id"]] and made["session_id"] not in credential_exit._sessions


def test_an_action_with_no_compute_machine_is_refused_before_its_tools_are_built(temp_db, monkeypatch):
    """The platform starts no tool itself. With no compute machine the
    button's build (and the tool listing's, the same builder) is refused
    with the sentence before the tool config is built: no seat, no secret,
    no process, on a shared app and on a personal one."""
    from core import placement
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    from tests._compute import serve_none
    if not agent_store.agent_exists(AGENT):
        agent_store.create_agent(AGENT, "Exec Agent")
    built: list = []
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config",
                        lambda *a, **kw: built.append(kw) or (None, {}, {}, {}, set()))
    serve_none(monkeypatch)
    for row in (_shared_row(), {}):
        for phone in (False, True):
            with pytest.raises(placement.NoComputeMachine) as refused:
                hx._build_session_parts(AGENT, row, frozenset({"test-mcp"}), "appx-none", phone=phone)
            assert str(refused.value) == placement.NO_COMPUTE_SENTENCE
    assert built == []


def test_a_button_with_no_compute_machine_answers_the_sentence(monkeypatch, caplog):
    """The page's button gets the sentence as its answer, an error it can
    show. Nothing is logged as a crash: a refusal is no failure of the
    tool."""
    import logging

    from core import placement

    def _refused(agent, row, mcps, session_id="", phone=False, reach=hx.FULL):
        raise placement.NoComputeMachine()
    monkeypatch.setattr(hx, "_build_session_parts", _refused)
    with caplog.at_level(logging.INFO):
        answer = asyncio.run(hx.execute_app_tool(_shared_row(), ACTION, {"text": "hi"}, reach=hx.FULL))
    assert answer == {"status": "error", "reason": placement.NO_COMPUTE_SENTENCE}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert hx._pool == {}, "no manager is kept for a refused build"


def test_a_limited_button_is_refused_before_its_tool_runs(fake_mcp, monkeypatch):
    """An app button's call reads the agent's tool limits from the run's
    registered context (``tool_limits``), as the hook reads a session's."""
    def limited_build(agent, row, mcps, session_id="", phone=False, reach=hx.FULL):
        ctx = SecurityContext(role="manager", username="", agent=agent, is_admin_agent=False,
                              tool_limits=(("test-mcp", ("other",)),), placement=placed())
        return fake_mcp, {}, {}, _NoWrap(), ctx

    monkeypatch.setattr(hx, "_build_session_parts", limited_build)

    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "hi"}, reach=hx.FULL)
        assert r["status"] == "error"
        assert r["reason"].startswith("test-mcp is limited for this agent: it may not use echo.")
        await hx.close_all()

    asyncio.run(main())


def test_a_buttons_tools_see_no_sessions_home(temp_db, tmp_path, monkeypatch):
    """An app button's tool processes run in a sandbox over the identity's
    stores: the agent scope's engine state, the sessions' homes (their
    sign-ins) with it, is masked empty."""
    from core.sandbox import sandbox_render
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    from tests._compute import serve
    if not agent_store.agent_exists(AGENT):
        agent_store.create_agent(AGENT, "Exec Agent")
    agents = tmp_path / "agents"
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    (agents / AGENT / "workspace" / ".claude" / "own" / sid).mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", lambda *a, **kw: ({}, {}, {}, {}, set()))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **kw: [])
    serve(monkeypatch)
    _cfg, _b, _env, builder, _ctx = hx._build_session_parts(AGENT, _shared_row(), frozenset({"test-mcp"}),
                                                            "appx-homes")
    desc = builder.describe()
    assert "/workspace/.claude" in {b.sandbox for b in desc.binds
                                    if b.root == sandbox_render.ROOT_EMPTY and not b.rw}
    assert desc.home is None


def test_an_eviction_at_the_cap_never_holds_the_press_on_its_close(monkeypatch):
    """The evicted manager's close (its tools ended, its record's removal on
    disk when it ran on a machine) runs behind the press: a person's button
    never waits on it, nor holds the pool's lock while it runs."""
    monkeypatch.setattr(hx, "POOL_MAX", 1)
    closed: list[str] = []

    class _Mgr:
        async def close(self):
            pass

    async def main():
        release = asyncio.Event()

        async def slow_close(entry):
            await release.wait()
            closed.append(entry.session_id)

        async def create(agent, row, scope_key, mcps, reach=hx.FULL):
            return hx._Entry(_Mgr(), "appx-new", scope_key, mcps, reach)

        monkeypatch.setattr(hx, "close_manager", slow_close)
        monkeypatch.setattr(hx, "_create_entry", create)
        hx._pool[(AGENT, "", "old-mcp", hx.FULL)] = hx._Entry(_Mgr(), "appx-old", "", frozenset({"old-mcp"}))
        try:
            entry, fresh = await asyncio.wait_for(
                hx._get_entry(AGENT, _shared_row(), frozenset({"new-mcp"})), 1)
            assert fresh and entry.session_id == "appx-new" and closed == []
            release.set()
            for _ in range(50):
                if closed:
                    break
                await asyncio.sleep(0.01)
            assert closed == ["appx-old"]
        finally:
            hx._pool.clear()

    asyncio.run(main())


def test_a_call_the_platform_stopped_waiting_for_is_a_failure(fake_mcp):
    """A tool that runs past the manager's cap answers the page as a
    failure with a sentence, never as a success whose text is an error."""
    nap = {"id": "nap", "label": "Nap", "type": "mcp_tool", "mcp": "test-mcp", "tool": "nap"}

    async def main():
        warm = await hx.execute_app_tool(_shared_row(), nap, {"seconds": 0}, reach=hx.FULL)
        assert warm == {"status": "done", "result": "awake"}
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        entry.manager.tool_timeout = 1
        for conn in entry.manager.servers.values():
            conn.tool_timeout = 1
        r = await hx.execute_app_tool(_shared_row(), nap, {"seconds": 3}, reach=hx.FULL)
        assert r == {"status": "error", "reason": hx.timed_out_sentence(1)}
        assert r["reason"] == "The tool did not finish within 1 seconds. It may still be working."
        await hx.close_all()

    asyncio.run(main())


def test_a_wedged_dispatch_says_the_same_sentence(fake_mcp):
    """The outer margin, past the call's own cap, answers with the one
    timeout sentence too."""
    nap = {"id": "nap", "label": "Nap", "type": "mcp_tool", "mcp": "test-mcp", "tool": "nap"}

    async def main():
        await hx.execute_app_tool(_shared_row(), nap, {"seconds": 0}, reach=hx.FULL)
        entry = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]

        async def wedged(calls):
            await asyncio.sleep(5)
        entry.manager.tool_timeout = -29
        entry.manager.execute_tools = wedged
        r = await hx.execute_app_tool(_shared_row(), nap, {"seconds": 0}, reach=hx.FULL)
        assert r == {"status": "error", "reason": hx.timed_out_sentence(1)}
        await hx.close_all()

    asyncio.run(main())


def test_a_confined_press_takes_a_manager_of_its_own(fake_mcp):
    """A press below the identity never runs on a warm manager of a wider
    reach, and the other way round."""
    from auth.path_policy import PRESS_MEMBER
    viewer = (PRESS_MEMBER, "viewer")

    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {"text": "a"}, reach=hx.FULL)
        await hx.execute_app_tool(_shared_row(), ACTION, {"text": "b"}, reach=viewer)
        assert set(hx._pool) == {(AGENT, "", "test-mcp", hx.FULL), (AGENT, "", "test-mcp", viewer)}
        full = hx._pool[(AGENT, "", "test-mcp", hx.FULL)]
        assert hx._covering_entry(AGENT, "", frozenset({"test-mcp"}), viewer) is not full
        assert hx._pool[(AGENT, "", "test-mcp", viewer)].reach == viewer
        await hx.close_all()

    asyncio.run(main())
