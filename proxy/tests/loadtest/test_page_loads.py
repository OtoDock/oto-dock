"""Check 2: thirty people open the dashboard at once, against a box with 20
agents, 400 chats and 100 of them streaming. Two phases, each measured on
its own: the three reads a page makes (the agent list, the sidebar's chat
list and the "Active now" seed), then the whole page load, those reads plus
the dashboard socket's connect."""

import json

import pytest

pytestmark = [pytest.mark.loadtest, pytest.mark.timeout(400, method="thread")]

AGENTS = 20
CHATS = 400
STREAMING = 100
TABS = 30
ROUNDS = 3
CLIENTS = 3          # client processes, 10 tabs each: one process for 30 would be the bottleneck


def _seed() -> tuple[list[str], list[str]]:
    import config
    from services.mcp import mcp_registry
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.mcp import mcp_store

    mcp_registry.scan_manifests()
    mcps = sorted(mcp_registry.get_all_manifests())[:12]
    slugs = []
    for i in range(AGENTS):
        slug = f"lt-page-{i}"
        agent_store.create_agent(slug, f"Page {i}", created_by="user-admin")
        (config.AGENTS_DIR / slug / "workspace").mkdir(parents=True, exist_ok=True)
        mcp_store.set_manager_enabled_mcps(slug, mcps)
        slugs.append(slug)
    chats = []
    for i in range(CHATS):
        cid = f"lt-page-chat-{i}"
        task_store.create_chat(cid, "user-admin", slugs[i % AGENTS], execution_path="claude-code-cli",
                               title=f"A chat about item {i}")
        chats.append(cid)
    return slugs, chats


def test_thirty_page_loads_at_once_hold_the_loop(tmp_path, monkeypatch):
    import core.session.interactive_session as interactive
    import core.session.session_state as session_state
    from auth.providers import create_session_jwt
    from tests.loadtest import _harness as h

    slugs, chats = _seed()
    streaming = chats[:STREAMING]
    monkeypatch.setattr(session_state, "streaming_chat_ids", lambda: list(streaming))
    monkeypatch.setattr(interactive, "streaming_chat_ids", set)
    cookie = create_session_jwt("user-admin", "admin@test.com", "Admin User", "admin")

    async def phase(host, port, sockets: bool):
        spec = tmp_path / f"pages-{int(sockets)}.json"
        spec.write_text(json.dumps({
            "base": f"http://{host}:{port}", "ws": f"ws://{host}:{port}/ws/dashboard",
            "origin": f"http://{host}:{port}", "cookie": cookie, "agents": slugs,
            "tabs": TABS // CLIENTS, "rounds": ROUNDS, "sockets": sockets}))
        clients = []
        try:
            for _ in range(CLIENTS):
                clients.append(await h.spawn(h.CLIENT, "pages", str(spec)))
            for c in clients:
                await h.read_line(c, 120)
            async with h.Window() as window:
                for c in clients:
                    await h.tell(c, "go")
                results = [await h.read_line(c, 180) for c in clients]
        finally:
            for c in clients:
                await h.end(c)
        statuses: dict[str, dict[str, int]] = {}
        latencies: dict[str, list[float]] = {}
        for r in results:
            for route, counts in r["statuses"].items():
                for code, n in counts.items():
                    statuses.setdefault(route, {})[code] = statuses.setdefault(route, {}).get(code, 0) + n
            for route, values in r["latencies"].items():
                latencies.setdefault(route, []).extend(values)
        merged = {"statuses": statuses, "routes": {k: h.dist(v) for k, v in latencies.items()},
                  "retries": sum(r["retries"] for r in results), "clients": [r["client"] for r in results]}
        return merged, window

    async def main():
        async with h.production_loop(tmp_path):
            async with h.serve_app() as (host, port, _router):
                return await phase(host, port, False), await phase(host, port, True)

    (reads, reads_window), (full, full_window) = h.run_loop(main, 380)
    for name, result, window in (("reads", reads, reads_window), ("full", full, full_window)):
        h.record(f"page-loads-{name}", agents=AGENTS, chats=CHATS, streaming=STREAMING, tabs=TABS,
                 rounds=ROUNDS, statuses=result["statuses"], routes=result["routes"],
                 retries=result["retries"], clients=result["clients"], **window.summary())

    for result, window in ((reads, reads_window), (full, full_window)):
        window.ticker.assert_covered()
        for route in ("agents", "chats", "active"):
            assert result["statuses"][route] == {"200": TABS * ROUNDS}, result["statuses"]
        for client in result["clients"]:
            assert client["lag_max_ms"] < h.HELPER_LAG_MAX_S * 1000, (
                "a client fell behind: this run measured the client, not the proxy", client)
    assert full["statuses"]["socket"] == {"101": TABS * ROUNDS}, full["statuses"]
    assert reads_window.ticker.stats()["max_ms"] < h.PAGE_LOAD_MAX_S * 1000, reads_window.ticker.stats()
    assert full_window.ticker.stats()["max_ms"] < h.PAGE_LOAD_MAX_S * 1000, full_window.ticker.stats()
