"""Install ahead (``services/remote/tools_ahead.py``): when a machine gets its
tools, which machines a tool goes to, what a run sends, the sign-in spread,
and that a machine never gets more than a session there could."""

import asyncio
from types import SimpleNamespace

import pytest

from auth import roles
from core import placement
from services.mcp import mcp_sync
from services.remote import machine_tools, tools_ahead


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(tools_ahead, "_pending", {})
    monkeypatch.setattr(tools_ahead, "_locks", {})
    monkeypatch.setattr(machine_tools, "_machines", {})
    monkeypatch.setattr(tools_ahead, "_signin_gate", None)


def _machines(*, builds: bool = False):
    """A connection manager whose machines are all connected: paired
    computers, or with ``builds`` the compute machine beside the platform."""
    return SimpleNamespace(is_connected=lambda mid: True, takes_tools=lambda mid: True,
                           builds_own_tools=lambda mid: builds,
                           compute_machine_ids=lambda: frozenset({"m1"}) if builds else frozenset())


@pytest.fixture
def runs(monkeypatch):
    seen: list[str] = []

    async def _run(machine_id):
        seen.append(machine_id)
    monkeypatch.setattr(tools_ahead, "run", _run)
    return seen


class TestDelay:
    @pytest.mark.asyncio
    async def test_a_later_call_moves_the_run_and_it_fires_once(self, runs):
        tools_ahead.schedule("m1", 0.2)
        await asyncio.sleep(0.1)
        tools_ahead.schedule("m1", 0.3)
        await asyncio.sleep(0.25)
        assert runs == []
        await asyncio.sleep(0.4)
        assert runs == ["m1"]

    @pytest.mark.asyncio
    async def test_an_enable_shortens_a_pending_assignment(self, runs):
        tools_ahead.schedule("m1", 30)
        tools_ahead.schedule("m1", 0.05)
        await asyncio.sleep(0.4)
        assert runs == ["m1"]

    @pytest.mark.asyncio
    async def test_a_run_is_never_pushed_past_its_cap(self, runs, monkeypatch):
        monkeypatch.setattr(tools_ahead, "MAX_DELAY_S", 0.4)
        for _ in range(6):  # pushed later every 0.1 s, it fires at its cap all the same
            if runs:
                break
            tools_ahead.schedule("m1", 0.3)
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.2)
        assert runs == ["m1"]

    @pytest.mark.asyncio
    async def test_an_assignment_taken_back_within_the_delay_installs_nothing(self, monkeypatch):
        """The run reads what is wanted when it fires."""
        wanted = {"m1": {"github-mcp"}}
        synced: list[str] = []
        monkeypatch.setattr(tools_ahead, "desired_for", lambda mid, row=None: set(wanted[mid]))
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            _machines)

        async def _sync(machine_id, session_id, names, **kw):
            synced.extend(names)
            return SimpleNamespace(ok=True, failed={})
        monkeypatch.setattr(mcp_sync, "sync_mcps_for_session", _sync)
        tools_ahead.schedule("m1", 0.1)
        wanted["m1"] = set()
        await asyncio.sleep(0.4)
        assert synced == []


class TestWhoGetsATool:
    @pytest.fixture
    def machines(self, monkeypatch):
        rows = [("laptop", {"id": "laptop"}), ("desk", {"id": "desk"}),
                ("rack", {"id": "rack", "kind": "compute"})]
        monkeypatch.setattr(tools_ahead, "_connected_machines", lambda: rows)
        wants = {"laptop": {"github-mcp"}, "desk": {"linear"}, "rack": {"github-mcp", "linear"}}
        monkeypatch.setattr(tools_ahead, "desired_for", lambda mid, row=None: wants[mid])

    def test_an_update_goes_to_every_machine_whose_agents_use_the_tool(self, machines):
        """Not by the list a machine reported at its sign-in: a machine that
        got the tool since is never skipped."""
        assert tools_ahead.machines_for_tool("github-mcp", update=True) == ["laptop", "rack"]

    def test_an_install_goes_to_every_machine_whose_agents_use_it(self, machines):
        assert tools_ahead.machines_for_tool("linear", update=False) == ["desk", "rack"]

    def test_narrowing_updates_is_the_one_rule(self, machines, monkeypatch):
        monkeypatch.setattr(tools_ahead, "takes_updates", lambda row: row.get("kind") == "compute")
        assert tools_ahead.machines_for_tool("github-mcp", update=True) == ["rack"]
        assert tools_ahead.machines_for_tool("github-mcp", update=False) == ["laptop", "rack"]


class TestRun:
    @pytest.fixture
    def sync(self, monkeypatch):
        calls: list[dict] = []
        answers: dict[str, SimpleNamespace] = {}
        held: set[str] = set()
        monkeypatch.setattr(tools_ahead, "desired_for", lambda mid, row=None: {"a-mcp", "b-mcp", "c-mcp"})
        monkeypatch.setattr("storage.remote_store.get_remote_machine", lambda mid: {"id": mid})

        async def _pending(machine_id, names, *, install_only=False):
            calls.append({"pending_install_only": install_only})
            return set(names) - held
        monkeypatch.setattr(mcp_sync, "pending_for", _pending)
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            _machines)

        async def _sync(machine_id, session_id, names, **kw):
            calls.append({"names": names, "card": tools_ahead.card(machine_id), **kw})
            await asyncio.sleep(0.01)
            answer = answers.get(names[0], SimpleNamespace(ok=True, failed={}))
            if answer.ok:
                held.add(names[0])
            return answer
        monkeypatch.setattr(mcp_sync, "sync_mcps_for_session", _sync)
        return calls, answers, held

    @pytest.mark.asyncio
    async def test_one_tool_at_a_time_never_removing_and_the_card_shows_what_is_left(self, sync, monkeypatch):
        calls, _, _ = sync
        monkeypatch.setattr(tools_ahead, "_label", lambda name: name)
        await tools_ahead.run("m1")
        calls = [c for c in calls if "names" in c]
        assert [c["names"] for c in calls] == [["a-mcp"], ["b-mcp"], ["c-mcp"]]
        assert all(c["remove_unwanted"] is False and c["install_only"] is False for c in calls)
        assert [c["card"]["installing"] for c in calls] == [["a-mcp", "b-mcp", "c-mcp"], ["b-mcp", "c-mcp"],
                                                            ["c-mcp"]]
        assert tools_ahead.card("m1") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("takes", [True, False])
    async def test_a_run_takes_updates_as_the_one_rule_says(self, sync, monkeypatch, takes):
        """A sign-in after a release installs what a machine holds outdated
        too, under the same gate (the operator, 2026-11-11)."""
        calls, _, _ = sync
        monkeypatch.setattr(tools_ahead, "SIGNIN_SPREAD_S", 0)
        monkeypatch.setattr(tools_ahead, "takes_updates", lambda row: takes)
        await tools_ahead.after_signin("m1")
        assert calls[0] == {"pending_install_only": not takes}
        assert all(c["install_only"] is (not takes) for c in calls[1:])

    @pytest.mark.asyncio
    async def test_two_runs_on_one_machine_take_turns_and_the_second_plans_what_is_left(
            self, sync, monkeypatch):
        """A tool enabled while a run installs: its run waits for the first
        one and installs it, and the card names what is left throughout."""
        calls, _, _ = sync
        monkeypatch.setattr(tools_ahead, "_label", lambda name: name)
        first = asyncio.create_task(tools_ahead.run("m1"))
        await asyncio.sleep(0.005)
        monkeypatch.setattr(tools_ahead, "desired_for", lambda mid, row=None: {"a-mcp", "b-mcp", "c-mcp", "d-mcp"})
        second = asyncio.create_task(tools_ahead.run("m1"))
        assert await asyncio.gather(first, second, return_exceptions=True) == [None, None]
        syncs = [c for c in calls if "names" in c]
        assert [c["names"] for c in syncs] == [["a-mcp"], ["b-mcp"], ["c-mcp"], ["d-mcp"]]
        assert syncs[3]["card"]["installing"] == ["d-mcp"] and tools_ahead.card("m1") is None

    @pytest.mark.asyncio
    async def test_a_run_installs_and_lists_only_what_the_machine_lacks(self, sync):
        calls, _, held = sync
        held |= {"a-mcp", "c-mcp"}
        await tools_ahead.run("m1")
        syncs = [c for c in calls if "names" in c]
        assert [c["names"] for c in syncs] == [["b-mcp"]] and syncs[0]["card"]["installing"] == ["b-mcp"]
        calls.clear()
        await tools_ahead.run("m1")
        assert [c for c in calls if "names" in c] == [] and tools_ahead.card("m1") is None

    @pytest.mark.asyncio
    async def test_a_failure_shows_on_the_card_until_an_install_works(self, sync, monkeypatch):
        calls, answers, _ = sync
        monkeypatch.setattr(tools_ahead, "_label", lambda name: name.upper())
        answers["b-mcp"] = SimpleNamespace(ok=False, failed={"b-mcp": "no wheel for this Python"})
        await tools_ahead.run("m1")
        assert tools_ahead.card("m1") == {"installing": [], "failed": {"B-MCP": "no wheel for this Python"}}
        # The run notes when: a chat's start leaves a fresh failure alone.
        assert machine_tools.just_failed("m1", "b-mcp") and not machine_tools.just_failed("m1", "a-mcp")
        answers.pop("b-mcp")
        await tools_ahead.run("m1")
        assert tools_ahead.card("m1") is None and not machine_tools.just_failed("m1", "b-mcp")

    @pytest.mark.asyncio
    async def test_the_card_shows_one_bounded_line_of_what_a_machine_said(self, sync):
        """The card is the machine's words on an admin's screen: one line of
        bounded length, whatever the sync handed over."""
        _, answers, _ = sync
        answers["b-mcp"] = SimpleNamespace(ok=False, failed={"b-mcp": "error:\n" + "x" * 5000})
        await tools_ahead.run("m1")
        shown, = tools_ahead.card("m1")["failed"].values()
        assert len(shown) == machine_tools.SAID_LIMIT and "\n" not in shown and shown.startswith("error: x")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("left", [{"a-mcp"}, set()])
    async def test_a_failure_leaves_the_card_once_the_machine_no_longer_gets_the_tool(
            self, sync, monkeypatch, left):
        _, answers, _ = sync
        answers["b-mcp"] = SimpleNamespace(ok=False, failed={"b-mcp": "no wheel for this Python"})
        await tools_ahead.run("m1")
        assert tools_ahead.card("m1") is not None
        monkeypatch.setattr(tools_ahead, "desired_for", lambda mid, row=None: left)
        await tools_ahead.run("m1")
        assert tools_ahead.card("m1") is None


@pytest.mark.asyncio
async def test_sign_ins_run_a_few_machines_at_a_time(monkeypatch):
    monkeypatch.setattr(tools_ahead, "SIGNIN_SPREAD_S", 0)
    monkeypatch.setattr(tools_ahead, "SIGNIN_AT_ONCE", 2)
    live = {"now": 0, "peak": 0}

    async def _run(machine_id):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
    monkeypatch.setattr(tools_ahead, "run", _run)
    await asyncio.gather(*(tools_ahead.after_signin(f"m{i}") for i in range(7)))
    assert live["peak"] == 2


class TestNeverMoreThanASession:
    def _m(self, name, **kw):
        return SimpleNamespace(name=name, label=name, audience=kw.get("audience", ""),
                               remote_policy=kw.get("remote_policy", ""), instances=None,
                               credentials=SimpleNamespace(type=kw.get("cred", "none")))

    def test_a_viewers_laptop_gets_no_editor_tool_and_no_admin_machine_tool(self, monkeypatch):
        laptop = placement.from_machine(placement.KIND_USER_REMOTE, {"id": "l", "name": "Laptop"})
        tools = [self._m("plain"), self._m("editors", audience="editor"),
                 self._m("admin-box", remote_policy="admin_paired_only", cred="infra"),
                 self._m("mine", cred="per_user")]
        got = mcp_sync._admitted("agent", tools, role=roles.VIEWER, placement=laptop, per_user=True)
        assert got == {"plain", "mine"}
        lifted = mcp_sync._admitted("agent", tools, role=roles.VIEWER, placement=laptop, per_user=False)
        assert lifted == {"plain"}

    def test_a_personal_machine_installs_with_its_owners_role_and_an_admin_machine_with_a_managers(
            self, monkeypatch):
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_agents_for_machine", lambda mid: ["helper"])
        monkeypatch.setattr(remote_store, "get_user_targets_on_machine", lambda mid: [("bob", "helper")])
        monkeypatch.setattr("auth.providers.effective_role_of",
                            lambda sub, agent, **kw: {"alice": roles.VIEWER}.get(sub) or 1 / 0)
        mine = tools_ahead._agents_on({"id": "l", "registered_by": "alice", "pairing_scope": placement.PAIRING_USER})
        assert mine[0] == ("helper", placement.KIND_USER_REMOTE, roles.VIEWER)
        assert mine[1] == ("helper", placement.KIND_USER_REMOTE, roles.VIEWER)  # bob's role unreadable
        admin = tools_ahead._agents_on({"id": "a", "registered_by": "root", "pairing_scope": placement.PAIRING_ADMIN})
        assert admin[0] == ("helper", placement.KIND_ADMIN_REMOTE, roles.MANAGER)


class TestTheBuildingMachine:
    """The compute machine beside the platform, once it keeps a tools folder
    of its own: it gets its tools ahead like any machine, at once at its
    sign-in, and a run also takes away what the platform no longer has."""

    @pytest.fixture
    def rig(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(tools_ahead, "desired_for", lambda mid, row=None: {"a-mcp"})
        monkeypatch.setattr("storage.remote_store.get_remote_machine", lambda mid: {"id": mid})

        async def _pending(machine_id, names, *, install_only=False):
            return set()
        monkeypatch.setattr(mcp_sync, "pending_for", _pending)

        async def _remove_gone(machine_id):
            calls.append(("remove_gone", machine_id))
            return []
        monkeypatch.setattr(mcp_sync, "remove_gone", _remove_gone)
        return calls

    @pytest.fixture
    def agents(self, monkeypatch):
        """Two agents and what a session of each could get on a compute
        machine: ``coder`` runs its sessions on machines, ``answers`` is a
        Direct LLM agent (its loop stays in the proxy, its tools' processes
        run on the machine beside the platform)."""
        from storage.agents import agent_store
        rows = {"coder": {"slug": "coder", "execution_path": "claude-code-cli"},
                "answers": {"slug": "answers", "execution_path": "direct-llm"}}
        monkeypatch.setattr(agent_store, "get_all_agents", lambda: list(rows.values()))
        monkeypatch.setattr(agent_store, "get_agent", rows.get)
        monkeypatch.setattr(mcp_sync, "machine_admitted",
                            lambda agent, *, role, placement: {f"{agent}-tool", "shared-tool"})
        monkeypatch.setattr(placement, "from_machine", lambda kind, row: (kind, row["id"]))
        return {"id": "m1", "name": "Compute", "pairing_scope": placement.PAIRING_COMPUTE}

    def test_it_gets_the_tools_of_every_agent_whatever_its_engine(self, agents, monkeypatch):
        """A Direct LLM agent's tools run there too, and a chat may run an
        agent on another engine than its default: no session's sync ever
        installs a tool on this machine, so the run is its one way in."""
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            lambda: _machines(builds=True))
        assert tools_ahead.desired_for("m1", row=agents) == {"coder-tool", "answers-tool", "shared-tool"}

    def test_a_compute_machine_on_another_server_gets_no_direct_llm_agents_tools(self, agents, monkeypatch):
        """Direct LLM's tools never run there: its file tools read the
        platform's own folders."""
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", _machines)
        assert tools_ahead.desired_for("m1", row=agents) == {"coder-tool", "shared-tool"}

    @pytest.mark.asyncio
    async def test_its_run_takes_away_what_the_platform_no_longer_has(self, rig, monkeypatch):
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            lambda: _machines(builds=True))
        await tools_ahead.run("m1")
        assert rig == [("remove_gone", "m1")]

    @pytest.mark.asyncio
    async def test_a_paired_computers_run_removes_nothing(self, rig, monkeypatch):
        """A session's start on that computer removes what is gone."""
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", _machines)
        await tools_ahead.run("m1")
        assert rig == []

    @pytest.mark.asyncio
    async def test_its_sign_in_runs_at_once(self, runs, monkeypatch):
        """One machine, and every session of the platform's place waits on
        its tools: no spread, no turn behind other machines."""
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            lambda: _machines(builds=True))
        monkeypatch.setattr(tools_ahead, "SIGNIN_SPREAD_S", 3600)
        await asyncio.wait_for(tools_ahead.after_signin("m1"), timeout=1.0)
        assert runs == ["m1"]

    def test_the_machines_an_ahead_run_serves_are_the_ones_that_take_tools(self, monkeypatch):
        rows = [{"id": "laptop"}, {"id": "claims"}, {"id": "claims-nothing"}, {"id": "offline"}]
        monkeypatch.setattr("storage.remote_store.get_all_remote_machines", lambda: rows)
        monkeypatch.setattr(
            "core.remote.satellite_connection.get_connection_manager",
            lambda: SimpleNamespace(takes_tools=lambda mid: mid in ("laptop", "claims")))
        assert [mid for mid, _row in tools_ahead._connected_machines()] == ["laptop", "claims"]

    @pytest.mark.asyncio
    async def test_an_uninstall_starts_its_run_and_no_other_machines(self, runs, monkeypatch):
        monkeypatch.setattr(tools_ahead, "ENABLE_DELAY_S", 0.01)
        monkeypatch.setattr(
            "core.remote.satellite_connection.get_connection_manager",
            lambda: SimpleNamespace(compute_machine_ids=lambda: frozenset({"own", "rack"}),
                                    builds_own_tools=lambda mid: mid == "own"))
        tools_ahead.after_uninstall()
        await asyncio.sleep(0.2)
        assert runs == ["own"]
