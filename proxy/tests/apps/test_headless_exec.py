"""Headless mcp_tool executor: real stdio-MCP end-to-end (spawn → call →
result), synthetic-session registration + full teardown, pool keying by
scope identity (the credential boundary), the personal-owner fail-closed
guard, self-heal on unknown tools, and the idle reaper.

``_build_session_parts`` is stubbed (the blocking identity/sandbox build
composes ``resolve_task_identity``/``resolve_visibility``/
``resolve_sandbox_config``, each covered by its own suite); everything from
the manager up — including the REAL fake MCP subprocess over stdio — runs
live here.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

import config
from auth.path_policy import SecurityContext
from core.session.session_state import get_session_security
from services.apps import headless_exec as hx
from storage import database as task_store

AGENT = "hx-agent"

FAKE_SERVER = """\
import json, os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def echo(text: str = "", flag: bool = False) -> str:
    return json.dumps({"text": text, "flag": flag,
                       "sid": os.environ.get("OTO_SESSION_ID", "")})


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

    class _NoWrap:
        """Stand-in SandboxBuilder (bwrap needs the full platform mount tree)."""
        class cfg:
            username = "alice"
            role = "manager"

        def build_command_prefix(self, cmd):
            return list(cmd)

        def get_env_overrides(self):
            return {}

    def fake_build(agent, row, mcps, session_id=""):
        assert session_id.startswith("appx-")
        ctx = SecurityContext(role="manager", username=row.get("username") or "",
                              agent=agent, is_admin_agent=False)
        return cfg, {}, {}, _NoWrap(), ctx

    monkeypatch.setattr(hx, "_build_session_parts", fake_build)
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
                                      {"text": "hi", "flag": True})
        assert r["status"] == "done"
        payload = json.loads(r["result"])
        assert payload["text"] == "hi" and payload["flag"] is True
        # The subprocess ran under the SYNTHETIC session id…
        assert payload["sid"].startswith("appx-")
        # …whose security context is registered for hook callbacks.
        entry = hx._pool[(AGENT, "", "test-mcp")]
        assert entry.session_id == payload["sid"]
        assert get_session_security(entry.session_id) is not None

        # Warm reuse: second click, same manager (no respawn).
        first_manager = entry.manager
        r2 = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"})
        assert json.loads(r2["result"])["text"] == "again"
        assert hx._pool[(AGENT, "", "test-mcp")].manager is first_manager

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
        await hx.execute_app_tool(_shared_row(), ACTION, {})
        await hx.execute_app_tool(_personal_row(), ACTION, {})
        # Personal and shared managers never share a key (credential boundary).
        assert set(hx._pool) == {(AGENT, "", "test-mcp"), (AGENT, "hx-alice-sub", "test-mcp")}
        assert (hx._pool[(AGENT, "", "test-mcp")].manager
                is not hx._pool[(AGENT, "hx-alice-sub", "test-mcp")].manager)
        await hx.close_all()

    asyncio.run(main())


def test_personal_owner_must_still_hold_access(fake_mcp):
    task_store.upsert_user("hx-bob-sub", "hx-bob@test.com", "Bob", "member")
    # Bob holds NO role on the agent (unassigned since pin/approval).
    row = _personal_row(username="hx-bob", sub="hx-bob-sub")

    async def main():
        r = await hx.execute_app_tool(row, ACTION, {})
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
        r = await hx.execute_app_tool(_shared_row(), bad, {})
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
        r = await hx.execute_app_tool(_shared_row(), ACTION, {})
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "", "test-mcp")].manager
        first.servers["test-mcp"].dead = True  # what a failed call_tool sets

        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"})
        assert r["status"] == "done"
        assert json.loads(r["result"])["text"] == "again"
        assert hx._pool[(AGENT, "", "test-mcp")].manager is not first  # rebuilt
        await hx.close_all()

    asyncio.run(main())


def test_a_call_that_finds_the_session_gone_rebuilds_once_and_presses_again(fake_mcp, monkeypatch):
    """The github sidecar evicts an idle session while the manager sits warm
    in the pool; the next press's call answers "Session terminated" and the
    connection marks itself dead. The press rebuilds once and presses again,
    so the hourly wake after a quiet stretch is not the one that fails."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {})
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "", "test-mcp")].manager

        async def gone(calls):
            first.servers["test-mcp"].dead = True      # what call_tool sets on "Session terminated"
            return [{"id": "app-action", "content": "Error calling tool 'echo': Session terminated"}]
        monkeypatch.setattr(first, "execute_tools", gone)

        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"})
        assert r["status"] == "done" and json.loads(r["result"])["text"] == "again"
        assert hx._pool[(AGENT, "", "test-mcp")].manager is not first   # rebuilt within the press
        await hx.close_all()

    asyncio.run(main())


def test_self_heal_rebuilds_warm_manager_once_per_cooldown(fake_mcp):
    """A WARM manager missing the tool is dropped and rebuilt (the MCP may
    have been re-enabled since it was built) — but at most once per
    cooldown, so a permanently-flaky MCP can't turn every click into a
    full manager rebuild (found live on the trusted VM: uptime-kuma)."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {})
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "", "test-mcp")].manager

        bad = dict(ACTION, tool="nope")
        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error" and "not available" in r["reason"]
        second = hx._pool[(AGENT, "", "test-mcp")].manager
        assert second is not first  # self-heal rebuilt the manager

        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error"
        assert hx._pool[(AGENT, "", "test-mcp")].manager is second  # cooldown: no rebuild

        # Cooldown elapsed → the self-heal path opens again.
        hx._selfheal_at[(AGENT, "", "test-mcp")] -= hx._SELF_HEAL_COOLDOWN_S + 1
        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error"
        assert hx._pool[(AGENT, "", "test-mcp")].manager is not second
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
            r = await hx.execute_app_tool(_shared_row(), ACTION, {})
            assert r["status"] == "error" and "already running" in r["reason"]
            # Same action id, different args → runs (real subprocess call).
            r2 = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "b"})
            assert r2["status"] == "done"
        finally:
            hx._inflight.clear()
        await hx.close_all()

    asyncio.run(main())


def test_result_truncation(fake_mcp, monkeypatch):
    monkeypatch.setattr(hx, "RESULT_MAX_CHARS", 8)

    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION,
                                      {"text": "0123456789abcdef"})
        assert r["status"] == "done"
        assert r["result"].endswith("(result truncated)")
        await hx.close_all()

    asyncio.run(main())


def test_idle_reap_full_teardown(fake_mcp, monkeypatch):
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {})
        entry = hx._pool[(AGENT, "", "test-mcp")]
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
        r = await hx.execute_app_tool(row, ACTION, {"text": "one"})
        assert r["status"] == "done"
        entry = hx._pool[(AGENT, "", "test-mcp")]
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
        await hx.execute_app_tool(both, OTHER_ACTION, {"text": "b"})
        assert set(hx._pool) == {(AGENT, "", "other-mcp,test-mcp")}
        big = hx._pool[(AGENT, "", "other-mcp,test-mcp")]
        assert set(big.manager.servers) == {"other-mcp", "test-mcp"}

        r = await hx.execute_app_tool(one, ACTION, {"text": "o"})
        assert r["status"] == "done"
        assert set(hx._pool) == {(AGENT, "", "other-mcp,test-mcp")}  # reused

        # The reverse order builds a second, exact entry: the small manager
        # cannot serve the bigger app.
        await hx.close_all()
        await hx.execute_app_tool(one, ACTION, {"text": "o"})
        await hx.execute_app_tool(both, OTHER_ACTION, {"text": "b"})
        assert set(hx._pool) == {(AGENT, "", "test-mcp"),
                                 (AGENT, "", "other-mcp,test-mcp")}
        await hx.close_all()

    asyncio.run(main())


def test_self_heal_waits_for_the_entry_to_go_idle(fake_mcp):
    """A rebuild for a missing tool must not dispose the manager under a
    sibling call (a batch fans out on one manager): it waits for busy to
    drop, then rebuilds."""
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {})
        entry = hx._pool[(AGENT, "", "test-mcp")]
        entry.manager.servers["test-mcp"].dead = True
        entry.busy += 1  # a sibling still running

        async def release():
            await asyncio.sleep(0.3)
            entry.busy -= 1

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        loop.create_task(release())
        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"})
        assert r["status"] == "done"
        assert loop.time() - t0 >= 0.3
        assert hx._pool[(AGENT, "", "test-mcp")].manager is not entry.manager
        await hx.close_all()

    asyncio.run(main())


def test_calls_on_one_manager_fan_out_up_to_the_cap(fake_mcp, monkeypatch):
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {})
        entry = hx._pool[(AGENT, "", "test-mcp")]
        peak = {"now": 0, "max": 0}

        async def slow_execute(calls):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.05)
            peak["now"] -= 1
            return [{"tool_use_id": c["id"], "content": "ok"} for c in calls]

        monkeypatch.setattr(entry.manager, "execute_tools", slow_execute)
        results = await asyncio.gather(*[
            hx.execute_app_tool(_shared_row(), ACTION, {"text": str(i)})
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
        entry = hx._pool[(AGENT, "", "test-mcp")]
        assert entry.idle_s == hx.KEEP_WARM_S
        assert entry.pinned_until > 0
        # The platform idle timeout no longer reaps it…
        monkeypatch.setattr(config, "get_idle_timeout", lambda: 1)
        entry.last_used -= 10
        entry.manager.last_activity -= 10
        await hx._sweep_once()
        assert (AGENT, "", "test-mcp") in hx._pool
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
        await hx.execute_app_tool(_personal_row(), ACTION, {})
        await hx.execute_app_tool(other_row, OTHER_ACTION, {})
        # The personal (unpinned, older) entry went; the pinned one stayed.
        assert set(hx._pool) == {(AGENT, "", "test-mcp"), (AGENT, "", "other-mcp")}
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


def test_the_app_identity_token_files_ride_the_bundles_of_the_started_mcps(monkeypatch):
    """The app-button builder delivers the identity's OAuth token files in
    the bundles, for the MCPs this manager starts only."""
    from types import SimpleNamespace
    from core.credentials import credential_files as cf
    seen: list = []
    monkeypatch.setattr(cf, "token_file_env",
                        lambda agent, **kw: seen.append((agent, kw)) or {
                            "test-mcp": {cf.CREDENTIAL_FILES_ENV: "{}"},
                            "other-mcp": {cf.CREDENTIAL_FILES_ENV: "{}"}})
    identity = SimpleNamespace(creds_user_sub="hx-alice-sub", scope="user")
    out = hx._bundles_with_token_files(AGENT, identity, {}, frozenset({"test-mcp"}))
    assert seen == [(AGENT, {"user_sub": "hx-alice-sub", "session_scope": "user"})]
    assert set(out) == {"test-mcp"}


def test_the_pooled_session_is_held_while_live_and_closing_after(fake_mcp):
    from core.session import session_state

    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "hi"})
        sid = json.loads(r["result"])["sid"]
        assert session_state.session_is_held(sid)
        await hx.close_all()
        assert not session_state.session_is_held(sid)
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()

    asyncio.run(main())
