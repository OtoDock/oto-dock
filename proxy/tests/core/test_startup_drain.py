"""The planned stop's wiring (``startup_drain.py``): what counts as open
work, what a page is told, what runs when the stop is called off, and the
stop signal that says the platform is ending."""

import asyncio
import signal
from types import SimpleNamespace

import pytest

import startup
import startup_drain
from core import background, drain
from core.events import stream_pump
from core.session import session_kind


def _pump(sid: str, *, task: bool = False, done: bool = False):
    return SimpleNamespace(session_id=sid, is_done=done,
                           source_type=session_kind.TASK.source_type if task else "chat")


@pytest.fixture
def world(monkeypatch):
    from core.events import bg_command_state
    from core.session import interactive_session, session_state
    from services.scheduler import shared
    parked = {"s-card", "s-run-card", "t-card"}
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: sid in parked)
    monkeypatch.setattr(stream_pump, "_active_pumps", {
        "c1": _pump("s-works"), "c2": _pump("s-card"), "c3": _pump("s-done", done=True),
        "c4": _pump("s-run", task=True), "c5": _pump("s-run-card", task=True)})
    loop = asyncio.new_event_loop()
    live, ended = loop.create_future(), loop.create_future()
    ended.set_result(None)
    monkeypatch.setattr(shared, "_running_tasks", {"r1": live, "r2": loop.create_future(),
                                                   "r3": ended, "r-waits": loop.create_future()})
    # ``r-waits`` waits for its slot or at its lane's gate: its round has not opened.
    monkeypatch.setattr(shared, "_open_rounds", {"r1", "r2", "r3"})
    monkeypatch.setattr(interactive_session, "_sessions", {
        "t-works": SimpleNamespace(session_id="t-works", alive=True, turn_open=True,
                                   question_parked=False),
        "t-asks": SimpleNamespace(session_id="t-asks", alive=True, turn_open=True,
                                  question_parked=True),
        "t-card": SimpleNamespace(session_id="t-card", alive=True, turn_open=True,
                                  question_parked=False),
        "t-idle": SimpleNamespace(session_id="t-idle", alive=True, turn_open=False,
                                  question_parked=False)})
    monkeypatch.setattr(bg_command_state, "_bg_command_registries", {
        "s-idle-bg": SimpleNamespace(pending_count=2), "s-works": SimpleNamespace(pending_count=5)})
    monkeypatch.setattr(session_state, "_subagent_registries", {})
    yield
    loop.close()


def test_work_that_waits_on_a_person_is_not_working(world):
    assert startup_drain._turns() == 1              # the one with a card waits
    # Two live runs whose round opened, one parked on a card. The run that has
    # not opened its round started nothing: a stop leaves it pending.
    assert startup_drain._runs() == 1
    assert startup_drain._terminals() == 1
    assert startup_drain._waiting() == 4            # two pumps, a question, a terminal's card


def test_background_work_counts_only_beside_no_open_turn(world, monkeypatch):
    from core.session import background_leash
    monkeypatch.setattr(background_leash, "background_pending_count",
                        lambda sid: {"s-idle-bg": 2, "s-works": 5}[sid])
    assert startup_drain._background() == 2


def test_register_wires_every_kind_and_the_four_hooks():
    startup_drain.register()
    assert set(drain._counters) == set(drain.KINDS)
    assert drain._waiting is startup_drain._waiting and drain._queued is startup_drain._queued
    assert [len(drain._hooks[name]) for name in (
        "change", "settle", "held", "resume", "start_ended")] == [1, 1, 1, 1, 1]


def test_a_page_is_told_while_new_work_is_held():
    async def scene():
        before = startup_drain.notice_frame()
        await drain.drain(60)
        during = startup_drain.notice_frame()
        await drain.resume()
        return before, during, startup_drain.notice_frame()

    before, during, after = asyncio.run(scene())
    assert (before["type"], before["updating"]) == ("server_info", False)
    assert during["updating"] is True and after["updating"] is False
    assert set(before) == {"type", "build_id", "version", "updating"}


def test_a_resume_sends_out_what_was_held_in_order(monkeypatch):
    from services.infra import platform_marks
    from services.scheduler import continue_pass, run_recovery, scheduler
    order: list[str] = []

    def _step(name: str):
        async def step(*a, **kw):
            order.append(name)
            return 0
        return step
    monkeypatch.setattr(platform_marks, "note_resumed", lambda: order.append("the hold's mark"))
    monkeypatch.setattr(startup_drain, "_tell_machines", _step("the machines"))
    monkeypatch.setattr(continue_pass, "arm_queues", _step("the queues"))
    monkeypatch.setattr(run_recovery, "start_pending_runs", _step("the held runs"))
    monkeypatch.setattr(scheduler, "fire_held", _step("the held wakes"))
    monkeypatch.setattr(scheduler, "redeliver_pending_wakes", _step("the stored wakes"))
    monkeypatch.setattr(continue_pass, "run_pending", _step("the cut turns"))

    async def scene():
        await startup_drain._on_resume()
        # The route answers here, whatever a woken turn takes.
        assert order == ["the hold's mark"]
        for task in background.running("drain"):
            await task

    asyncio.run(scene())
    assert order == ["the hold's mark", "the machines", "the queues", "the cut turns", "the held runs",
                     "the held wakes", "the stored wakes"]


class _Machines:
    """A connection manager with one machine connected, its lane in hand."""

    def __init__(self, monkeypatch):
        from core.remote import machine_push_lane
        self.lane = machine_push_lane._Lane(machine_id="m-1")
        monkeypatch.setattr(machine_push_lane, "_lanes", {"m-1": self.lane})
        monkeypatch.setattr(machine_push_lane, "_manager", lambda: self)

    def is_connected(self, machine_id):
        return machine_id == "m-1"

    def broadcast_fire_and_forget(self, msg):
        return 1


def _drain_to_held(monkeypatch, machines, during=None) -> dict:
    from core.remote import satellite_connection
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: machines)
    states: list[str] = []
    drain.reset_for_tests()
    drain.on_settle(startup_drain._settle_pushes)

    async def scene():
        reached = asyncio.Event()
        drain.on_held(lambda: states.append("held") or reached.set())
        started = asyncio.get_running_loop().time()
        await drain.drain(0)
        if during:
            await during(states)
        await asyncio.wait_for(reached.wait(), 8)
        return asyncio.get_running_loop().time() - started
    try:
        return {"took": asyncio.run(scene()), "states": states}
    finally:
        drain.reset_for_tests()


def test_a_planned_stop_waits_for_what_a_healthy_machine_is_taking(monkeypatch):
    """The names live in this process: the stop comes after the push, not
    after the bound."""
    machines = _Machines(monkeypatch)
    machines.lane.inflight[("pa", "a.txt")] = (1, False, False)

    async def during(states):
        await asyncio.sleep(0.05)
        assert drain.state() == drain.DRAINING and not states
        machines.lane.inflight.clear()
        machines.lane.wake()
    done = _drain_to_held(monkeypatch, machines, during)
    assert done["took"] < startup_drain.PUSH_WAIT_S / 2


def test_a_frozen_machine_does_not_hold_a_planned_stop(monkeypatch, caplog):
    machines = _Machines(monkeypatch)
    machines.lane.inflight[("pa", "a.txt")] = (1, False, False)
    monkeypatch.setattr(startup_drain, "PUSH_WAIT_S", 0.05)
    done = _drain_to_held(monkeypatch, machines)
    assert done["took"] >= 0.05 and done["took"] < 1.0
    assert "its next merge settles them" in caplog.text


def test_a_machine_that_is_away_does_not_slow_a_planned_stop(monkeypatch):
    """It keeps its names for its return: nothing can reach it now."""
    machines = _Machines(monkeypatch)
    machines.lane.machine_id = "m-away"
    machines.lane.names[("pa", "a.txt")] = object()
    done = _drain_to_held(monkeypatch, machines)
    assert done["took"] < startup_drain.PUSH_WAIT_S / 2


def test_the_machines_are_told_how_long_the_platform_is_away(monkeypatch):
    from core.remote import satellite_connection
    sent: list[dict] = []

    class _Manager:
        def broadcast_fire_and_forget(self, msg):
            sent.append(msg)
            return 1
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Manager)
    asyncio.run(startup_drain._tell_machines(900))
    assert sent == [{"type": "platform_stopping", "back_within_s": 900}]


def test_the_stop_signal_says_the_platform_is_ending_before_the_servers_own_handler():
    seen: list[tuple] = []
    earlier_int = signal.getsignal(signal.SIGINT)
    earlier = signal.signal(signal.SIGTERM, lambda sig, frame: seen.append(
        (sig, stream_pump._platform_ending, drain.stopping())))
    try:
        startup._note_stop_signal()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, earlier)
        signal.signal(signal.SIGINT, earlier_int)
    assert seen == [(signal.SIGTERM, True, True)]


@pytest.fixture
def clean_drain():
    drain.reset_for_tests()
    yield
    drain.reset_for_tests()


@pytest.fixture
def prewarm_reaps(monkeypatch):
    """Every close of the pre-warms the start's end asks for."""
    from core.session import prewarm_session_registry
    asked: list = []

    async def reap(ttl=None):
        asked.append(ttl)
        return 1
    monkeypatch.setattr(prewarm_session_registry, "reap_stale", reap)
    return asked


def test_the_starts_end_clears_the_chips_tells_the_pages_and_closes_no_prewarm(
        monkeypatch, clean_drain, prewarm_reaps):
    """The start holds new work for the compute machine alone. A pre-warm
    made before its end has every tool, since the boot builds none: it
    stays for the page's Send. The held messages go out with the boot's
    continue, not here."""
    from core.events import input_queue
    seen: list = []
    monkeypatch.setattr(input_queue, "clear_updating", lambda: seen.append("chips"))
    monkeypatch.setattr(startup_drain, "_tell_pages", lambda: seen.append("pages"))
    startup_drain.register()
    drain.hold_for_start()
    asyncio.run(drain.end_start_hold())
    assert seen == ["chips", "pages"] and prewarm_reaps == []


def test_a_drain_on_at_the_starts_end_keeps_its_chips(monkeypatch, clean_drain, prewarm_reaps):
    from core.events import input_queue
    seen: list = []
    monkeypatch.setattr(input_queue, "clear_updating", lambda: seen.append("chips"))
    monkeypatch.setattr(startup_drain, "_tell_pages", lambda: seen.append("pages"))

    async def scene():
        drain.on_start_ended(startup_drain._on_start_ended)
        drain.hold_for_start()
        await drain.drain(30)
        await drain.end_start_hold()
        await drain.resume()
    monkeypatch.setattr(startup_drain, "_on_resume", lambda: None)
    asyncio.run(scene())
    assert seen == [] and prewarm_reaps == []


def test_the_compute_machines_open_work_and_its_block_in_work(world, monkeypatch):
    """There is one drain, and it counts the compute machine's turns among
    the others; ``work()["compute"]`` says what that machine holds now, for
    the scripts, the page and a controller."""
    from core import concurrency, concurrency_scan
    from core.remote import satellite_connection
    on_compute = {"s-works", "t-works"}
    monkeypatch.setattr(concurrency_scan, "placed_on_compute",
                        lambda sid: "c-1" if sid in on_compute else "")
    assert startup_drain.compute_open() == 2
    monkeypatch.setattr(satellite_connection, "get_connection_manager",
                        lambda: SimpleNamespace(compute_machine_ids=lambda: frozenset({"c-1"})))
    monkeypatch.setattr(concurrency, "_session_box", {"s-works": "c-1", "s-local": ""})
    from services.remote import compute_bootstrap
    monkeypatch.setattr(compute_bootstrap, "runs_own", lambda: True)
    from services.apps import app_compute
    shares = {"row": {"id": "c-1"}}
    monkeypatch.setattr(app_compute, "place", lambda: shares["row"])
    drain.reset_for_tests()
    try:
        drain.register_compute(startup_drain._compute)
        assert drain.work()["compute"] == {"own": True, "connected": True, "shared": True, "machines": ["c-1"],
                                           "open": 2, "sessions": 1}
        # The one signed in is on another server: none shares the platform's folders.
        shares["row"] = None
        block = drain.work()["compute"]
        assert block["connected"] is True and block["shared"] is False
    finally:
        drain.reset_for_tests()


def test_the_clean_stop_is_written_only_once_the_compute_machine_drained(monkeypatch):
    """A turn the compute machine still runs outlives the proxy's stop: the
    mark that would read it as a stop is not written."""
    import startup_shutdown
    from services.infra import platform_marks
    marks: list[str] = []
    monkeypatch.setattr(platform_marks, "note_clean_stop", lambda: marks.append("clean"))
    assert "_note_the_clean_stop" in startup_shutdown._shutdown_cleanup.__code__.co_names
    for still, expect in ((1, []), (0, ["clean"])):
        marks.clear()
        monkeypatch.setattr(startup_drain, "compute_open", lambda still=still: still)
        startup_shutdown._note_the_clean_stop(startup_shutdown.logger)
        assert marks == expect


def test_on_bare_metal_the_server_takes_itself_out_of_reach_of_proc(monkeypatch):
    """The compute unit runs as the platform's own OS user there: the server
    is made not dumpable (prctl PR_SET_DUMPABLE 0). In a container nothing is
    asked: the container is the boundary. Nor on an install that runs no
    compute machine of its own: core dumps and an inspector stay as they were."""
    import ctypes

    import config
    from core import host_os
    from services.remote import compute_bootstrap
    asked: list[tuple] = []

    class _Libc:
        def prctl(self, *args):
            asked.append(args)
            return 0

    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_k: _Libc())
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(host_os, "host_os", lambda: SimpleNamespace(name=host_os.LINUX))
    monkeypatch.setattr(compute_bootstrap, "runs_own", lambda: True)
    assert startup.not_dumpable() is True and asked == [(4, 0, 0, 0, 0)]
    asked.clear()
    monkeypatch.setattr(compute_bootstrap, "runs_own", lambda: False)
    assert startup.not_dumpable() is False and asked == []
    monkeypatch.setattr(compute_bootstrap, "runs_own", lambda: True)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    assert startup.not_dumpable() is False and asked == []
