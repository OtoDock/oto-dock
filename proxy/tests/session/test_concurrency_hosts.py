"""Admission across hosts and the balancer (``core/concurrency.py``,
``remote_status.serving_compute_machine``).

A compute machine beside the platform shares its host: one budget, one count,
one grow-in debit. A compute machine on another host (its own copy) is a host
of its own: judged against its own report's budget, outside the platform's
count, with a debit and an admission line of its own, and eviction there frees
only its own sessions. The balancer gives a new session to the machine with
the most room, keeps a chat on the machine it started on, and never sends a
session that needs the platform's own folder to a machine with its own copy.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import config
from core import concurrency as C, concurrency_scan
from core.remote import satellite_connection
from core.session import session_state

SHARED, OWN = "c-1", "c-2"
GIB = 1 << 30


class _Manager:
    def __init__(self) -> None:
        self.memory = {
            SHARED: {"total_bytes": 16 * GIB, "available_bytes": 12 * GIB, "swap_free_bytes": 0, "capped": False},
            OWN: {"total_bytes": 8 * GIB, "available_bytes": 6 * GIB, "swap_free_bytes": 0, "capped": False},
        }
        self.connected = {SHARED, OWN}
        self.tunnels: dict[str, bool] = {}

    def is_compute(self, mid):
        return mid in self.connected

    def owns_copy(self, mid):
        return mid == OWN and mid in self.connected

    def shares_folders(self, mid):
        return mid == SHARED and mid in self.connected

    def compute_machine_ids(self):
        return frozenset(self.connected)

    def machine_memory_report(self, mid, compute=False):
        return (self.memory[mid], 1.0) if mid in self.connected else None

    def tunnel_ok(self, mid):
        return self.tunnels.get(mid, True)

    def satellite_name(self, mid):
        return mid

    def satellite_version(self, mid):
        return "0.5.176"

    def compute_address(self, mid):
        return "192.168.50.7" if mid == OWN else ""

    def satellite_session_home_dirs(self, mid):
        return frozenset()

    def exit_state(self, mid):
        return 0, False


@pytest_asyncio.fixture
async def hosts(monkeypatch):
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "SESSION_EST_LIGHT_MB", 200)
    monkeypatch.setattr(config, "SESSION_RESERVE_FLOOR_MB", 300)
    monkeypatch.setattr(config, "SESSION_SWAP_CREDIT_MB", 0)
    monkeypatch.setattr(config, "BUDGET_FRACTION", 0.9)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "OTODOCK_COMPUTE_SESSIONS", "compute", raising=False)
    tables = (C._sessions, C._session_est, C._session_added_at, C._session_owner, C._session_box,
              C._session_host, C._session_grown, C._evicted_credit, concurrency_scan._grow_debits)
    for table in tables:
        table.clear()
    C._line.clear()
    # The module's numbers and its condition are put back when the test ends,
    # whatever a test assigned to them: the next module starts from its own.
    for name, value in (("_reserved_mb", 0), ("_parked_tasks", 0), ("_cond", asyncio.Condition()),
                        ("_budget_mb", 4_000), ("_floor_mb", 300), ("_total_mb", 8_000)):
        monkeypatch.setattr(C, name, value)
    manager = _Manager()
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)
    monkeypatch.setattr(session_state, "get_session_security", lambda _sid: None)
    yield SimpleNamespace(manager=manager)
    for table in tables:
        table.clear()
    C._line.clear()


def _add(sid, est, box):
    C._add(sid, "chat", est, "", box)


@pytest.mark.asyncio
async def test_a_machine_on_another_host_is_judged_by_its_own_budget(hosts):
    # The platform's host is full; the machine on another host still admits.
    _add("p-1", 4_000, "")
    assert not C._gate1(500, is_task=False, box="") and not C._gate1(500, is_task=False, box=SHARED)
    assert C._gate1(500, is_task=False, box=OWN)
    _add("o-1", 500, OWN)
    # Its reservation is its own: the platform's sum did not move.
    assert C._reserved_mb == 4_000 and C._session_host == {"o-1": OWN}
    # Its own budget is its report's total times the fraction (8 GiB → 7372 MB).
    assert C._gate1(6_800, is_task=False, box=OWN)
    assert not C._gate1(6_900, is_task=False, box=OWN)
    C._remove("o-1")
    assert C._reserved_mb == 4_000 and not C._session_host


@pytest.mark.asyncio
async def test_the_count_caps_the_platforms_host_alone(hosts, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 2)
    _add("p-1", 100, "")
    _add("s-1", 100, SHARED)
    _add("o-1", 100, OWN)
    assert not C._gate1(100, is_task=False, box="")
    assert not C._gate1(100, is_task=False, box=SHARED)
    assert C._gate1(100, is_task=False, box=OWN)


@pytest.mark.asyncio
async def test_the_grow_in_debit_is_its_hosts(hosts):
    _add("o-1", 2_000, OWN)
    assert C._growin_debit_mb(OWN) > 1_900 and C._growin_debit_mb("") == 0


def test_each_host_has_a_line_of_its_own(hosts):
    a, b, c = object(), object(), object()
    C._line.extend([(a, ""), (b, OWN), (c, "")])
    assert C._is_head(a, "") and C._is_head(b, OWN)
    assert not C._is_head(c, "")


@pytest.mark.asyncio
async def test_eviction_frees_only_the_denied_hosts_sessions(hosts, monkeypatch):
    from core import concurrency_scan
    _add("p-1", 100, "")
    _add("o-1", 100, OWN)
    seen = []
    old = SimpleNamespace(last_activity=0.0, user_sub="")
    monkeypatch.setattr(concurrency_scan, "compute_sessions",
                        lambda: [("o-1", old, "remote", OWN)])
    monkeypatch.setattr("core.session.session_manager.live_session", lambda sid: None)
    scan = await concurrency_scan._oldest_evictable_local(0, host="")
    seen.append(scan.victim)
    scan = await concurrency_scan._oldest_evictable_local(0, host=OWN)
    seen.append(scan.victim)
    assert seen[0] is None or seen[0][0] != "o-1"
    assert seen[1] is not None and seen[1][0] == "o-1"


@pytest.mark.asyncio
async def test_a_reservation_moved_across_hosts_takes_its_sums_with_it(hosts):
    _add("s-1", 700, SHARED)
    assert C._reserved_mb == 700
    C._move_box("s-1", OWN)
    assert C._reserved_mb == 0 and C._session_host["s-1"] == OWN
    C._move_box("s-1", "")
    assert C._reserved_mb == 700 and "s-1" not in C._session_host


# ---------------------------------------------------------------------------
# the balancer
# ---------------------------------------------------------------------------

def _pick(**kw):
    from services.remote import remote_status
    row = remote_status.serving_compute_machine("claude-code-cli", **kw)
    return row["id"] if row else None


@pytest.mark.asyncio
async def test_the_machine_with_the_most_room_takes_a_new_chat(hosts):
    # The shared machine has 12 GiB free, the other 6: the shared one, until
    # its room is taken. Its room is also the platform host's budget's.
    C._budget_mb = 50_000
    assert _pick() == SHARED
    C._budget_mb = 4_000
    assert _pick() == OWN
    C._budget_mb = 50_000
    hosts.manager.memory[SHARED]["available_bytes"] = 2 * GIB
    assert _pick() == OWN


@pytest.mark.asyncio
async def test_a_chat_stays_where_it_started(hosts):
    assert _pick(prefer=OWN) == OWN
    hosts.manager.connected.discard(OWN)
    assert _pick(prefer=OWN) == SHARED


@pytest.mark.asyncio
async def test_what_needs_the_platforms_folder_never_goes_to_another_host(hosts):
    hosts.manager.memory[SHARED]["available_bytes"] = 1 * GIB
    assert _pick() == OWN
    assert _pick(shared_folder=True) == SHARED
    assert _pick(shared_folder=True, prefer=OWN) == SHARED
    from services.remote import remote_status
    # Direct LLM's tools follow the platform's folder by the engine itself.
    direct = remote_status.serving_compute_machine("direct-llm")
    assert direct is not None and direct["id"] == SHARED
    hosts.manager.connected.discard(SHARED)
    assert _pick(shared_folder=True) is None


@pytest.mark.asyncio
async def test_the_row_names_the_machine_on_another_host_as_its_own_copy(hosts):
    from core import placement
    from services.remote import remote_status
    hosts.manager.memory[SHARED]["available_bytes"] = 1 * GIB
    row = remote_status.serving_compute_machine("claude-code-cli")
    assert placement.from_machine(placement.KIND_COMPUTE, row).own_copy is True


@pytest.mark.asyncio
async def test_the_platforms_host_at_its_count_has_no_room(hosts, monkeypatch):
    C._budget_mb = 50_000
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 1)
    assert _pick() == SHARED
    _add("p-1", 100, "")
    assert C.box_room_mb(SHARED) == 0 and C.box_room_mb(OWN) > 0
    assert _pick() == OWN


@pytest.mark.asyncio
async def test_a_chat_whose_record_is_on_the_platforms_folder_stays_on_a_shared_machine(hosts):
    # The other host has more room: a new chat goes there, an existing one
    # whose engine record lives in the platform's folder does not, and one
    # that continued on the shared machine does not flip back.
    from services.remote import remote_status
    hosts.manager.memory[SHARED]["available_bytes"] = 1 * GIB

    def _for(chat):
        place = remote_status.chat_place(chat)
        return _pick(prefer=place.machine, shared_folder=place.shared)
    assert _for({}) == OWN
    assert _for({"session_id": "s-old", "compute_machine": ""}) == SHARED
    assert _for({"codex_thread_id": "t-old", "compute_machine": ""}) == SHARED
    assert _for({"session_id": "s-old", "compute_machine": OWN}) == OWN


# ---------------------------------------------------------------------------
# a tool's local address keeps its agent beside the platform
# ---------------------------------------------------------------------------

@pytest.fixture
def local_tools(monkeypatch):
    """The agents whose tools reach a local address, and every agent the
    rule's reader was asked about."""
    from services.mcp import mcp_egress
    state = SimpleNamespace(agents={"home-agent"}, asked=[])

    def _reaches(agent):
        state.asked.append(agent)
        return agent in state.agents
    monkeypatch.setattr(mcp_egress, "reaches_local_address", _reaches)
    return state


@pytest.mark.asyncio
async def test_an_agent_whose_tool_reaches_a_local_address_runs_beside_the_platform(hosts, local_tools):
    # The other host has more room: every other agent goes there, the one
    # with a tool's local address stays beside the platform, also when its
    # chat's record lives on the other host.
    hosts.manager.memory[SHARED]["available_bytes"] = 1 * GIB
    assert _pick(agent="other-agent") == OWN
    assert _pick(agent="home-agent") == SHARED
    assert _pick(agent="home-agent", prefer=OWN) == SHARED
    from services.remote import remote_status
    assert remote_status.compute_row_for("local", "claude-code-cli", agent="home-agent")["id"] == SHARED
    local_tools.agents.clear()
    assert _pick(agent="home-agent") == OWN


@pytest.mark.asyncio
async def test_with_the_machine_beside_the_platform_away_it_never_falls_to_another_host(hosts, local_tools):
    from core import placement
    from services.remote import remote_status
    hosts.manager.connected.discard(SHARED)
    assert _pick(agent="home-agent") is None
    with pytest.raises(placement.NoComputeMachine):
        remote_status.compute_row_for("local", "claude-code-cli", agent="home-agent")
    assert _pick(agent="other-agent") == OWN


@pytest.mark.asyncio
async def test_the_rule_reads_nothing_it_does_not_need(hosts, local_tools):
    from services.remote import remote_status
    # A paired computer: asked nothing.
    assert remote_status.compute_row_for("m-paired", "claude-code-cli", agent="home-agent") is None
    # Narrowed already (an outside person, a call, Direct LLM's tools): asked nothing.
    assert _pick(agent="home-agent", shared_folder=True) == SHARED
    assert remote_status.serving_compute_machine("direct-llm", agent="home-agent")["id"] == SHARED
    # No agent named: asked nothing.
    assert _pick() in (SHARED, OWN)
    # No machine on another host connected: asked nothing.
    hosts.manager.connected.discard(OWN)
    assert _pick(agent="home-agent") == SHARED
    assert local_tools.asked == []
    assert remote_status.keeps_beside("home-agent") is False
    hosts.manager.connected.add(OWN)
    assert remote_status.keeps_beside("home-agent") is True
    assert remote_status.keeps_beside("home-agent", (SHARED,)) is False
    assert remote_status.keeps_beside("") is False


def test_an_existing_chat_is_refused_with_the_machine_beside_the_platform_away(hosts, local_tools, monkeypatch):
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "live_session", lambda sid: None)
    from core.session import interactive_registry
    monkeypatch.setattr(interactive_registry, "find_live_for_chat", lambda cid: None)
    hosts.manager.connected.discard(SHARED)
    chat = {"id": "c-1", "agent": "home-agent", "execution_target": "local", "execution_path": "claude-code-cli",
            "session_id": "s-1", "compute_machine": OWN}
    assert session_manager.no_compute_for(chat) is True
    assert session_manager.no_compute_for({**chat, "agent": "other-agent"}) is False
    # A lane on the other host stays there: it is not refused.
    assert session_manager.no_compute_for({**chat, "parent_chat_id": "c-0"}) is False


def test_a_prewarm_that_needs_the_platforms_folder_asks_room_beside_it(hosts):
    C._budget_mb = 50_000
    assert C.prewarm_allowed("claude-code-cli") is True
    assert C.prewarm_allowed("claude-code-cli", shared_folder=True) is True
    hosts.manager.connected.discard(SHARED)
    assert C.prewarm_allowed("claude-code-cli") is True
    assert C.prewarm_allowed("claude-code-cli", shared_folder=True) is False


def test_a_chat_that_continues_work_stays_on_its_records_machine():
    # Delegated or task work resumes by id and never reads a digest: it stays
    # on the machine its record lives on, as a move keeps such a lane.
    from services.remote import remote_status
    person = remote_status.chat_place({"id": "c-1", "session_id": "s", "compute_machine": OWN})
    lane = remote_status.chat_place({"id": "c-2", "session_id": "s", "compute_machine": OWN,
                                     "parent_chat_id": "c-1"})
    run = remote_status.chat_place({"id": "task-r1", "session_id": "s", "compute_machine": OWN,
                                    "source_type": "task"})
    beside = remote_status.chat_place({"id": "c-3", "session_id": "s", "compute_machine": "",
                                       "parent_chat_id": "c-1"})
    assert (person.stays, lane.stays, run.stays, beside.stays) == (False, True, True, False)
    assert person.agent_for("home-agent") == "home-agent"
    assert lane.agent_for("home-agent") == "" and run.agent_for("home-agent") == ""


@pytest.mark.asyncio
async def test_the_builders_and_the_layer_read_the_chats_place(hosts, monkeypatch):
    from core.config import config_builder
    from core.session import session_manager
    from storage import database
    hosts.manager.memory[SHARED]["available_bytes"] = 1 * GIB
    row = {"id": "chat-1", "session_id": "s-old", "compute_machine": "", "execution_target": "local",
           "execution_path": "claude-code-cli"}
    monkeypatch.setattr(database, "get_chat", lambda cid: row)
    got = await config_builder._compute_row("local", "claude-code-cli", "chat-1", "editor", False)
    assert got is not None and got["id"] == SHARED
    # The chat's agent names the rule of a tool's local address: a chat whose
    # record is on the other host starts beside the platform for such an agent.
    from services.mcp import mcp_egress
    monkeypatch.setattr(mcp_egress, "reaches_local_address", lambda agent: agent == "home-agent")
    row["compute_machine"] = OWN
    assert (await config_builder._compute_row("local", "claude-code-cli", "chat-1", "editor", False,
                                              agent="other-agent"))["id"] == OWN
    assert (await config_builder._compute_row("local", "claude-code-cli", "chat-1", "editor", False,
                                              agent="home-agent"))["id"] == SHARED
    row["parent_chat_id"] = "chat-0"
    assert (await config_builder._compute_row("local", "claude-code-cli", "chat-1", "editor", False,
                                              agent="home-agent"))["id"] == OWN
    row["compute_machine"], row["parent_chat_id"] = "", ""
    # A session on a paired computer is built on no compute machine, and the
    # machine a caller took its slot on is preferred while it serves.
    assert await config_builder._compute_row("m-paired", "claude-code-cli", "chat-1", "editor", False) is None
    row["session_id"] = ""
    assert (await config_builder._compute_row("local", "claude-code-cli", "chat-1", "editor", False, OWN))["id"] == OWN
    row["session_id"] = "s-old"
    seen: dict = {}
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(session_manager, "live_session", lambda sid: None)
    session_manager.layer_for_chat("personal-assistant", row, user_sub="sub-1", role="editor")
    assert seen["placement_built"].machine_id == SHARED


# ---------------------------------------------------------------------------
# the platform's own readers count the platform's host alone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_meeting_counts_the_platforms_sessions_alone(hosts, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 2)
    _add("o-1", 100, OWN)

    async def _est(*_a):
        return 100
    monkeypatch.setattr(C, "_session_estimate", _est)
    assert await C.acquire_meeting_slots(["m-1", "m-2"])
    assert {"m-1", "m-2"} <= set(C._sessions)


@pytest.mark.asyncio
async def test_a_parked_task_evicts_on_the_platforms_host_alone(hosts, monkeypatch):
    # An idle chat on another host frees nothing on the platform's.
    C._parked_tasks = 1
    _add("p-1", 4_000, "")
    seen: dict = {}

    async def _scan(*_a, **kw):
        seen.update(kw)
        return SimpleNamespace(victim=None)
    monkeypatch.setattr(C, "_oldest_evictable_local", _scan)

    async def _idle():
        return 600
    monkeypatch.setattr(C, "_idle_timeout", _idle)
    await C._maintenance_pass()
    assert seen["host"] == ""


@pytest.mark.asyncio
async def test_the_platforms_only_session_is_let_in_beside_another_hosts(hosts, monkeypatch):
    # Low memory on the platform's host with nothing of its own running: its
    # one session goes in, whatever runs on another host.
    monkeypatch.setattr(C, "_live_available_mb", lambda: 500)
    _add("o-1", 1_000, OWN)
    adm = await C.acquire("only", "chat", target="local")
    assert adm and "only" in C._sessions


def test_the_balancers_reading_writes_nothing(hosts, monkeypatch):
    # The balancer runs in a worker thread beside the loop that changes the
    # tables: its reading of a box changes none of them. A stale credit is
    # dropped when the next one is written, on the loop.
    import time as _time
    old = _time.monotonic() - 100
    C._evicted_credit[OWN] = [(old, 500)]
    before = {k: list(v) for k, v in C._evicted_credit.items()}
    C.box_room_mb(OWN)
    C.box_room_mb(SHARED)
    assert C._evicted_credit == before
    C._note_evicted_credit(OWN, 300)
    assert [mb for _t, mb in C._evicted_credit[OWN]] == [300]


@pytest.mark.asyncio
async def test_a_tool_started_on_another_host_grows_that_host_alone(hosts):
    """A tool's program started late on a machine on another host (lane
    TOOLS-ON-DEMAND's ``grow``) grows its session there: the platform's sum
    stays, the debit is that host's, its own budget judges it, and the
    release and a failed start's give-back bring everything back."""
    from core import concurrency_scan
    concurrency_scan._grow_debits.clear()
    concurrency_scan._session_grown.clear()
    _add("p-1", 500, "")
    _add("o-1", 400, OWN)
    platform_debit, own_debit = C._growin_debit_mb(""), C._growin_debit_mb(OWN)
    assert await C.grow("o-1", judged=True, server="mail")
    assert C._reserved_mb == 500 and C._session_est["o-1"] == 465
    assert C._growin_debit_mb("") <= platform_debit and C._growin_debit_mb(OWN) > own_debit
    await C.shrink("o-1", "mail")
    assert C._session_est["o-1"] == 400 and C._reserved_mb == 500
    assert await C.grow("o-1", judged=True, server="mail")
    # The machine's own budget (8 GiB x 0.9 = 7372 MB) refuses past it,
    # whatever room the platform's host has.
    _add("o-2", 6_900, OWN)
    assert not await C.grow("o-1", judged=True, server="docs")
    C._remove("o-2")
    C._remove("o-1")
    assert C._reserved_mb == 500 and "o-1" not in concurrency_scan._session_grown
    # On the platform's host a grow moves the platform's sum.
    assert await C.grow("p-1", judged=True, server="mail")
    assert C._reserved_mb == 565
