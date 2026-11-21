"""Characterization of the memory admission gate (``core/concurrency.py``,
``concurrency_scan.py``):
scripted admission traces on a fixed box, each step's ``Admission`` (or
answer), ``get_stats()`` after it and the sessions it closed, compared with
golden files. A drift is a behaviour change, and the commit that records it says
why.

Only the boundaries are faked: the memory readings on ``host_resources``, the
idle sessions in their holders (Direct LLM's pool, the remote layer's records
of the compute machine, the terminals), the closes on ``session_manager`` and
``interactive_session``. ``init()`` is the real one. Grow-in debit is a
function of the clock, so every low-memory step keeps a margin far wider than
the few MB a run's timing moves it, and no golden holds a debit."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import config
import core.concurrency as C
from core.layers.direct import session as direct_session
from core.remote.remote_session_info import RemoteSessionInfo
from tests.fixtures.characterization.golden_master import check_golden

MIB = 1024 * 1024
COMPUTE = "compute-1"
OLD = 20_000      # idle past the background-work ceiling (3600 s here)
IDLE = 1_000      # past the eviction floor (300 s here), under the ceiling
FRESH = 10        # under the floor


def _never() -> bool:
    return False


class _Box:
    """The readings, the sessions' holders and the trace of one test."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.total_mb = 8000
        self.live_mb = 100_000
        self.swap_mb = 0
        self.satellites: list[dict] = []
        self.terminals: dict[str, object] = {}
        self.fail_close: set[str] = set()
        self.steps: list[dict] = []
        self.closes: list[dict] = []
        self.bg_sessions: list[str] = []
        self.compute: dict[str, object] = {}
        self.pools = {"direct": direct_session._direct_sessions, "compute": self.compute,
                      "interactive": self.terminals}

    def live(self, mb: int) -> None:
        """A new memory reading, seen at once."""
        self.live_mb = mb
        C._live_cache = None

    def step(self, name: str, result=None) -> None:
        entry: dict = {"step": name, "stats": C.get_stats(), "parked": C._parked_tasks}
        if isinstance(result, C.Admission):
            entry["admission"] = result._asdict() | {"admitted": bool(result)}
        elif result is not None:
            entry["result"] = result
        if self.closes:
            entry["closed"] = list(self.closes)
            self.closes.clear()
        self.steps.append(entry)

    def session(self, sid: str, pool: str, age: float, **attrs) -> None:
        """A live session of ``sid`` in one holder, last active ``age`` seconds
        ago: ``compute`` is the remote layer's own record of a session the
        compute machine runs."""
        held: object
        if pool == "compute":
            held = RemoteSessionInfo(session_id=sid, machine_id=COMPUTE, agent_name="char",
                                     execution_path=attrs.pop("execution_path", "claude-code-cli"),
                                     event_queue=asyncio.Queue(), compute=True, **attrs)
            held.last_activity = time.monotonic() - age
        else:
            held = SimpleNamespace(last_activity=time.monotonic() - age, **attrs)
        self.mp.setitem(self.pools[pool], sid, held)

    def background_work(self, sid: str) -> None:
        from core.session import session_state
        session_state.get_subagent_registry(sid).register_spawn("agent-1", "tool-1")
        self.bg_sessions.append(sid)

    def _drop(self, sid: str) -> None:
        for pool in self.pools.values():
            pool.pop(sid, None)

    def layer_close(self, path: str):
        async def close_session(sid: str) -> None:
            self.closes.append({"close": path, "session": sid})
            self._drop(sid)
            if sid in self.fail_close:
                raise RuntimeError("the close failed")
        return SimpleNamespace(close_session=close_session)

    async def terminal_close(self, sid: str, *, reason: str = "closed", **_kw) -> bool:
        self.closes.append({"close": "interactive", "session": sid, "reason": reason})
        self._drop(sid)
        return True


class _Remote:
    """The remote layer as the scan and the evictor ask it: the sessions the
    compute machine runs, none in its machine's reconnect grace, and a close
    the box records."""

    def __init__(self, rig: _Box):
        self._sessions = rig.compute
        self.close_session = rig.layer_close("remote").close_session

    def owns_session(self, sid: str) -> bool:
        return sid in self._sessions

    def machine_of_session(self, sid: str) -> str:
        return COMPUTE if sid in self._sessions else ""

    def is_session_grace_held(self, sid: str) -> bool:
        return False


def _release_all() -> None:
    for sid in list(C._sessions):
        C.release(sid)


@pytest.fixture
def box(monkeypatch):
    from core.remote import satellite_connection
    from core.sandbox import host_resources
    from core.session import interactive_session, session_manager, session_state
    # A pre-warm starts on the compute machine that serves, here one beside
    # the platform whose box is the host's own: the host's gates are read.
    monkeypatch.setattr(session_manager, "compute_placement_for",
                        lambda *_a, **_k: SimpleNamespace(machine_id=""))
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "SESSION_EST_LIGHT_MB", 200)
    monkeypatch.setattr(config, "BUDGET_FRACTION", 0.625)        # 5000 of the 8000: five HEAVY
    monkeypatch.setattr(config, "SESSION_RESERVE_FLOOR_MB", 300)
    monkeypatch.setattr(config, "SESSION_SWAP_CREDIT_MB", 512)
    monkeypatch.setattr(config, "SESSION_EVICT_FLOOR_S", 300)
    monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 3600)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "get_idle_timeout", lambda: 900)

    rig = _Box(monkeypatch)
    monkeypatch.setattr(host_resources, "detect_memory_limit_bytes", lambda: rig.total_mb * MIB)
    monkeypatch.setattr(host_resources, "live_available_bytes", lambda: rig.live_mb * MIB)
    monkeypatch.setattr(host_resources, "swap_free_bytes", lambda: rig.swap_mb * MIB)
    monkeypatch.setattr(satellite_connection, "get_connection_manager",
                        lambda: SimpleNamespace(concurrency_stats=lambda: list(rig.satellites),
                                                compute_machine_ids=frozenset,
                                                is_compute=lambda _mid: False,
                                                is_connected=lambda _mid: True,
                                                machine_memory_report=lambda _mid, compute=False: None))
    # The layer that holds a victim closes it: a recorder answers for Direct
    # LLM's layer (by its engine id) and for the remote layer.
    held = session_manager.holding_engines()
    monkeypatch.setattr(session_manager, "holding_engines",
                        lambda: SimpleNamespace(get=rig.layer_close, values=held.values))
    monkeypatch.setattr(session_manager, "_remote_layer", _Remote(rig))
    # No hook ever ran: its 0 would floor a fake's age at this box's uptime.
    monkeypatch.setattr(session_state, "get_hook_activity", lambda _sid: float("-inf"))
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda local_only=False: set(rig.terminals))
    monkeypatch.setattr(interactive_session, "get", rig.terminals.get)
    monkeypatch.setattr(interactive_session, "close_session", rig.terminal_close)

    _release_all()
    for name, value in (("_cond", C._cond), ("_budget_mb", C._budget_mb), ("_floor_mb", C._floor_mb),
                        ("_total_mb", C._total_mb), ("_reserved_mb", 0), ("_parked_tasks", 0),
                        ("_live_cache", None)):
        monkeypatch.setattr(C, name, value)
    C.init()
    yield rig
    _release_all()
    for sid in rig.bg_sessions:
        session_state._subagent_registries.pop(sid, None)


async def _until(condition, what: str) -> None:
    for _ in range(5000):
        if condition():
            return
        await asyncio.sleep(0.001)
    pytest.fail(f"never: {what}")


async def _settled() -> None:
    """The notifies a release handed to the loop have run."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _fill(count: int, prefix: str = "fill") -> list[str]:
    sids = [f"{prefix}-{i}" for i in range(count)]
    for sid in sids:
        assert await C.acquire(sid, "chat")
    return sids


# --- init and the readings -------------------------------------------------

def _empty_stats(rig: _Box, *, total: int = 8000, live: int = 100_000, swap: int = 0) -> dict:
    rig.total_mb, rig.swap_mb = total, swap
    C.init()
    rig.live(live)
    return C.get_stats()["sessions"]


def test_init_and_the_readings(box, monkeypatch):
    got = {
        "the box": _empty_stats(box),
        "small box": _empty_stats(box, total=2000),
        "large box, three percent floor": _empty_stats(box, total=64_000, live=1_920),
        "unknown limit": _empty_stats(box, total=0),
        "swap credit, half the free swap": _empty_stats(box, live=1000, swap=400),
        "swap credit, capped": _empty_stats(box, live=1000, swap=4096),
        "no reading gets no credit": _empty_stats(box, live=0, swap=4096),
    }
    monkeypatch.setattr(config, "SESSION_SWAP_CREDIT_MB", 0)
    got["swap credit off"] = _empty_stats(box, live=1000, swap=4096)
    monkeypatch.setattr(config, "BUDGET_FRACTION", 0.9)
    monkeypatch.setattr(config, "SESSION_RESERVE_FLOOR_MB", 256)
    got["other fractions"] = _empty_stats(box)

    from core.sandbox import host_resources

    def _unreadable():
        raise OSError("no such file")
    monkeypatch.setattr(host_resources, "live_available_bytes", _unreadable)
    C._live_cache = None
    got["unreadable reads as zero"] = C.get_stats()["sessions"]
    got["Admission"] = {"fields": list(C.Admission._fields), "defaults": C.Admission(True)._asdict(),
                        "truthy": bool(C.Admission(True)), "falsy": bool(C.Admission(False, "busy", "x"))}
    check_golden("concurrency/init", got)


def test_get_stats_shows_the_satellites_it_is_given(box):
    box.satellites.append({"machine_id": "m1", "sessions": 2})
    check_golden("concurrency/stats-satellites", C.get_stats())


# --- the traces ------------------------------------------------------------

@pytest.mark.asyncio
async def test_chats_on_a_box_with_plenty_of_memory(box):
    s = box.step
    s("empty")
    s("a chat", await C.acquire("chat-a", "chat", user_sub="u1"))
    s("the same id again", await C.acquire("chat-a", "chat", user_sub="u1"))
    s("a chat on a machine", await C.acquire("chat-remote", "chat", target="machine-x"))
    s("an in-process engine's chat", await C.acquire_chat_slot("chat-light", execution_path="direct-llm"))
    s("a phone call", await C.acquire_chat_slot("phone-1", kind="phone"))
    s("may a pre-warm start", C.prewarm_allowed())
    s("a pre-warm with room for two", await C.acquire_chat_slot("pw-1", user_sub="u2", speculative=True))
    s("may another start", C.prewarm_allowed())
    s("a pre-warm without", await C.acquire_chat_slot("pw-2", user_sub="u2", speculative=True))
    s("the pre-warm's id again", await C.acquire_chat_slot("pw-1", speculative=True))
    s("a chat that fits", await C.acquire("chat-b", "chat"))
    s("one that does not", await C.acquire("chat-c", "chat"))
    s("a light one still fits", await C.acquire_chat_slot("chat-light-2", execution_path="direct-llm"))
    C.release("chat-a")
    s("released")
    C.release("chat-a")
    s("released again")
    C.release("chat-remote")
    s("a release of what was never tracked")
    C.release_chat_slot("chat-b")
    s("the chat slot's release")
    await _settled()
    check_golden("concurrency/trace-chats", box.steps)


@pytest.mark.asyncio
async def test_estimates_as_the_reservations_show_them(box, tmp_path):
    def config_of(stdio: int, http: int = 0):
        path = tmp_path / f"mcp-{stdio}-{http}.json"
        servers = {f"s{i}": {"type": "stdio", "command": "x"} for i in range(stdio)}
        servers |= {f"h{i}": {"type": "http", "url": "http://h"} for i in range(http)}
        path.write_text(json.dumps({"mcpServers": servers}))
        return str(path)

    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    huge = tmp_path / "huge.json"
    huge.write_text(" " * (1024 * 1024 + 1))
    cases = {
        "no path": {},
        "claude": {"execution_path": "claude-code-cli"},
        "codex": {"execution_path": "codex-cli"},
        "direct": {"execution_path": "direct-llm"},
        "unknown engine": {"execution_path": "no-such-engine"},
        "direct, two stdio": {"execution_path": "direct-llm", "mcp_config_path": config_of(2)},
        "direct, eight stdio and two http": {"execution_path": "direct-llm", "mcp_config_path": config_of(8, 2)},
        "direct, past HEAVY": {"execution_path": "direct-llm", "mcp_config_path": config_of(20)},
        "direct, not JSON": {"execution_path": "direct-llm", "mcp_config_path": str(bad)},
        "direct, too large to be one": {"execution_path": "direct-llm", "mcp_config_path": str(huge)},
        "direct, missing": {"execution_path": "direct-llm", "mcp_config_path": str(tmp_path / "none")},
        "a subprocess engine's config": {"execution_path": "claude-code-cli", "mcp_config_path": config_of(2)},
    }
    got = {}
    for name, kwargs in cases.items():
        assert await C.acquire(f"est-{name}", "chat", **kwargs)
        got[name] = C.get_stats()["sessions"]["reserved_mb"]
        C.release(f"est-{name}")
    await _settled()
    check_golden("concurrency/estimates", got)


@pytest.mark.asyncio
async def test_background_wakes_and_tasks(box):
    s = box.step
    wake = {"timeout_s": 5, "superseded": _never}
    s("a wake", await C.reserve_background("wake-1", **wake))
    s("the same id", await C.reserve_background("wake-1", **wake))
    s("a person took the chat", await C.reserve_background("wake-2", timeout_s=5, superseded=lambda: True))

    def _raises() -> bool:
        raise RuntimeError("no answer")
    s("the check raises", await C.reserve_background("wake-3", timeout_s=5, superseded=_raises))
    s("on a machine", await C.reserve_background("wake-4", target="machine-x", **wake))
    s("an in-process engine", await C.reserve_background("wake-5", execution_path="direct-llm", **wake))
    C.release("wake-5")
    async with C.task_slot("task-1"):
        s("inside a task slot")
    s("after a task slot")
    async with C.task_slot("task-2", target="machine-x"):
        s("a task slot on a machine")
    await _fill(3)                                   # 4000 of 5000: no task headroom
    s("no headroom", await C.reserve_background("wake-6", timeout_s=0.02, superseded=_never))
    parked = asyncio.create_task(C.reserve_background("wake-7", timeout_s=30, superseded=_never))
    await _until(lambda: C._parked_tasks == 1, "the wake parks")
    s("a wake parked")
    s("a person still gets the last one", await C.acquire("person", "chat"))
    C.release("person")
    C.release("fill-0")
    s("a slot freed", await asyncio.wait_for(parked, 10))
    await _settled()
    check_golden("concurrency/trace-background", box.steps)


@pytest.mark.asyncio
async def test_parked_tasks_take_turns_by_creator(box):
    s = box.step
    chats = await _fill(4)                           # 4000 of 5000: every task parks
    alice_1 = asyncio.create_task(C.acquire("alice-1", "task", blocking=True, ring_key="alice"))
    alice_2 = asyncio.create_task(C.acquire("alice-2", "task", blocking=True, ring_key="alice"))
    bob_1 = asyncio.create_task(C.acquire("bob-1", "task", blocking=True, ring_key="bob"))
    tasks = {"alice-1": alice_1, "alice-2": alice_2, "bob-1": bob_1}

    def done() -> list[str]:
        return sorted(name for name, task in tasks.items() if task.done())

    await _until(lambda: C._parked_tasks == 3, "three tasks park")
    s("three parked", done())
    s("a chat still fits", await C.acquire("person", "chat"))
    C.release("person")
    for i, due in enumerate((alice_1, bob_1, alice_2)):
        C.release(chats[i])
        assert await asyncio.wait_for(due, 10)
        s(f"chat {i} released", done())
    await _settled()
    check_golden("concurrency/trace-task-ring", box.steps)


@pytest.mark.asyncio
async def test_meetings(box, tmp_path, monkeypatch):
    s = box.step
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}))
    s("two local and one on a machine", await C.acquire_meeting_slots(
        ["m1", "m2", "m3"], targets={"m1": "local", "m2": "machine-x", "m3": "local"}))
    s("the same people again", await C.acquire_meeting_slots(["m1", "m3"]))
    s("in-process participants", await C.acquire_meeting_slots(
        ["d1", "d2"], exec_paths={"d1": "direct-llm", "d2": "direct-llm"}, mcp_paths={"d2": str(mcp)}))
    s("too many for the budget", await C.acquire_meeting_slots(["over-1", "over-2", "over-3", "over-4"]))
    box.live(500)
    s("too little memory", await C.acquire_meeting_slots(["low-1"]))
    box.live(100_000)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 5)
    s("past the session cap", await C.acquire_meeting_slots(["cap-1", "cap-2"]))
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    C.release_meeting_slots(["m1", "m2", "m3", "d1", "d2"])
    s("released, the machine's id a no-op")
    await _settled()
    check_golden("concurrency/trace-meeting", box.steps)


@pytest.mark.asyncio
async def test_the_cap_of_one_person(box, monkeypatch):
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 2)
    s = box.step
    s("first", await C.acquire("u1-a", "chat", user_sub="u1"))
    s("second", await C.acquire("u1-b", "chat", user_sub="u1"))
    s("third, nothing of theirs idle", await C.acquire("u1-c", "chat", user_sub="u1"))
    s("a task is not counted", await C.acquire("u1-t", "task", blocking=True, user_sub="u1"))
    C.release("u1-t")
    s("a phone call is not", await C.acquire("u1-p", "phone", user_sub="u1"))
    C.release("u1-p")
    s("someone else", await C.acquire("u2-a", "chat", user_sub="u2"))
    s("a re-attach is not", await C.acquire("u1-d", "chat", user_sub="u1", per_user_cap=False))
    C.release("u1-d")
    s("may they pre-warm", C.prewarm_allowed(user_sub="u1"))
    s("may another pre-warm", C.prewarm_allowed(user_sub="u2"))
    s("their pre-warm is refused quietly", await C.acquire_chat_slot("u1-pw", user_sub="u1", speculative=True))
    box.session("u1-a", "compute", OLD)
    box.session("u1-b", "compute", IDLE)
    box.session("u2-a", "compute", 2 * OLD)
    s("third again: their most idle closes, not another's", await C.acquire("u1-e", "chat", user_sub="u1"))
    s("a chat of no known owner", await C.acquire("no-owner", "chat"))
    s("the same id, now with an owner", await C.acquire("no-owner", "chat", user_sub="u2"))
    s("that owner is at the cap: their idle one closes", await C.acquire("u2-b", "chat", user_sub="u2"))
    box.fail_close.add("u1-b")
    s("a close that fails still frees the seat", await C.acquire("u1-f", "chat", user_sub="u1"))
    await _settled()
    check_golden("concurrency/trace-user-cap", box.steps)


@pytest.mark.asyncio
async def test_eviction_over_every_holder(box):
    s = box.step
    for sid, pool, age, more in (("e-claude", "compute", 5000, {}), ("e-direct", "direct", 6000, {}),
                                 ("e-codex", "compute", 7000, {"execution_path": "codex-cli"}),
                                 ("e-term", "interactive", 8000, {}), ("e-fresh", "compute", FRESH, {})):
        assert await C.acquire(sid, "chat")
        box.session(sid, pool, age, **more)
    s("the budget is full")
    s("evicts the most idle: a terminal", await C.acquire("n1", "chat", user_sub="u9"))
    s("then the compute machine's Codex session", await C.acquire("n2", "chat", user_sub="u9"))
    s("then direct", await C.acquire("n3", "chat", user_sub="u9"))
    s("then its Claude Code session", await C.acquire("n4", "chat", user_sub="u9"))
    s("only a fresh one left", await C.acquire("n5", "chat", user_sub="u9"))
    await _settled()
    check_golden("concurrency/trace-eviction-pools", box.steps)


@pytest.mark.asyncio
async def test_eviction_order_and_what_is_spared(box):
    from core.session import prewarm_session_registry
    s = box.step
    # A machine session's record names no person: its owner is the ledger's.
    for sid, owner in (("a-other", "u2"), ("a-mine", "u9"), ("a-prewarm", None), ("a-turn", None),
                       ("a-work", None)):
        assert await C.acquire(sid, "chat", user_sub=owner)
    box.session("a-other", "compute", 9000)
    box.session("a-mine", "compute", 4000)
    box.session("a-prewarm", "compute", 1)
    await prewarm_session_registry.register("a-prewarm", agent="char", role="admin")
    box.session("a-turn", "compute", IDLE, turn_active=True)
    box.session("a-work", "compute", IDLE)
    box.background_work("a-work")
    s("a pre-warm goes first, at any age", await C.acquire("n1", "chat", user_sub="u9"))
    s("then the person's own", await C.acquire("n2", "chat", user_sub="u9"))
    s("then the most idle", await C.acquire("n3", "chat", user_sub="u9"))
    s("a turn and background work are spared", await C.acquire("n4", "chat", user_sub="u9"))
    box.session("a-work", "compute", OLD)
    s("background work past the ceiling goes", await C.acquire("n5", "chat", user_sub="u9"))
    s("a turn alone is spared without a word", await C.acquire("n6", "chat", user_sub="u9"))
    box.session("a-turn", "compute", OLD, turn_active=True)
    box.fail_close.add("a-turn")
    s("a turn past the ceiling goes, a failed close too", await C.acquire("n7", "chat", user_sub="u9"))
    await _settled()
    check_golden("concurrency/trace-eviction-order", box.steps)


@pytest.mark.asyncio
async def test_host_memory_and_the_session_cap(box, monkeypatch):
    s = box.step
    box.live(500)
    s("the only session, low memory", await C.acquire("only", "chat"))
    s("a second is host memory", await C.acquire("second", "chat"))
    s("a pre-warm is refused quietly", await C.acquire_chat_slot("pw", speculative=True))
    box.live(100_000)
    s("memory back", await C.acquire("third", "chat"))
    box.live(1_500)
    s("the sessions account for the shortfall", await C.acquire("fourth", "chat"))
    box.live(100_000)
    C.release("third")
    await _settled()
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 2)
    s("under the cap", await C.acquire("fifth", "chat"))
    s("past the cap", await C.acquire("sixth", "chat"))
    s("may a pre-warm start", C.prewarm_allowed())
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 3)
    s("a task keeps a slot back", await C.reserve_background("wake", timeout_s=0.02, superseded=_never))
    s("a person does not", await C.acquire("seventh", "chat"))
    await _settled()
    check_golden("concurrency/trace-host-memory", box.steps)


@pytest.mark.asyncio
async def test_the_admission_queue(box, monkeypatch):
    s = box.step
    chats = await _fill(5)
    dave = asyncio.create_task(C.acquire("dave", "chat", queue_wait_s=30))
    await asyncio.sleep(0)
    frank = asyncio.create_task(C.acquire("frank", "chat", queue_wait_s=30))
    await asyncio.sleep(0.05)
    s("two in line", {"dave": dave.done(), "frank": frank.done()})
    C.release(chats[0])
    s("first in line, first served", await asyncio.wait_for(dave, 10))
    s("the second still waits", {"frank": frank.done()})
    s("a walk-in never waits", await C.acquire("walk-in", "chat"))
    C.release(chats[1])
    s("then the second", await asyncio.wait_for(frank, 10))
    s("past its deadline", await C.acquire("late", "chat", queue_wait_s=0.02))
    for sid in (*chats[2:], "dave"):
        C.release(sid)
    await _settled()
    box.live(500)
    s("host memory never waits", await C.acquire("low", "chat", queue_wait_s=30))
    box.live(100_000)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 1)
    s("a person's seat", await C.acquire("mine", "chat", user_sub="u1"))
    s("nor does the person's cap", await C.acquire("mine-2", "chat", user_sub="u1", queue_wait_s=30))
    await _settled()
    check_golden("concurrency/trace-queue", box.steps)


@pytest.mark.asyncio
async def test_reconcile_and_the_release_paths(box, monkeypatch):
    from core.session import session_manager
    from services.engines import subscription_pool
    s = box.step
    seats: list[str] = []
    monkeypatch.setattr(session_manager, "holding_engines",
                        lambda: {"char": SimpleNamespace(local_session_ids=lambda: ["live"])})
    monkeypatch.setattr(subscription_pool, "release_subscription", seats.append)
    await C.acquire("live", "chat")
    await C.acquire("orphan", "chat")
    await C.acquire("job", "task", blocking=True)
    s("a fresh slot is spared, a task is never touched", await C.reconcile_chat_slots())
    s("a live session keeps its slot", C.release_unless_live("live"))
    s("an orphan gives it up", C.release_unless_live("orphan"))
    s("nothing left to give up", C.release_unless_live("orphan"))
    closed: list[str] = []

    async def _close(sid: str) -> None:
        closed.append(sid)

    async def _close_raises(sid: str) -> None:
        closed.append(sid)
        raise RuntimeError("the close failed")

    await C.reap_session("live", _close)
    s("a reap frees slot and seat", {"closed": list(closed), "seats": list(seats)})
    await C.reap_session("job", _close_raises)
    s("a failed close frees them too", {"closed": list(closed), "seats": list(seats)})
    await _settled()
    check_golden("concurrency/trace-release-paths", box.steps)
