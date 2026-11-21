"""A machine's link state: registration with no lock, the reconnect steps'
windows, the alive report with its hooks and terminal claims, the exits a
machine owes across a drop, the heartbeat monitor's drop."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.remote import remote_pty, satellite_connection as sc, satellite_link
from tests._timing import bound
from tests.remote import link_fakes as lf


@pytest.fixture
def cm(monkeypatch):
    manager = sc.SatelliteConnectionManager()
    monkeypatch.setattr(sc, "_connection_manager", manager)
    monkeypatch.setattr(sc, "_persist_write", lambda *a, **k: None)
    from core.session import session_manager
    layer = SimpleNamespace(_sessions={}, _cm=manager,
                            sync_all_agents_on_reconnect=AsyncMock(),
                            moved_port_idle_sessions=lambda mid: [])
    layer.owns_session = lambda sid: sid in layer._sessions
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    from storage import remote_store
    reads: list[str] = []
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda mid: reads.append(mid) or {"name": f"name-{mid}", "max_sessions": 7})
    manager.machine_reads = reads
    manager.layer = layer
    satellite_link.reset_hooks()
    yield manager
    satellite_link.reset_hooks()
    remote_pty._remote_ptys.clear()


async def _settle(n: int = 40) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _report(machine: lf.FakeMachine, conn, *, terminals=(), sessions=(), others=(),
                  report_id: str | None = None, partial: bool = False) -> None:
    rid = machine.cm._links.get(machine.machine_id).assembly.report_id if report_id is None else report_id
    await machine.say({"type": "pty_alive", "session_ids": list(terminals), "report_id": rid})
    frame = {"type": "sessions_alive", "sessions": [{"session_id": s} for s in sessions],
             "report_id": rid}
    if others is not None:
        frame["other_sessions"] = [{"session_id": s} for s in others]
    if partial:
        frame["partial"] = True
    await machine.say(frame)


# -- registration ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("compute", [False, True])
async def test_every_sign_in_asks_the_fan_out_for_the_pushes_the_machine_missed(cm, monkeypatch, compute):
    """A paired computer and a compute machine alike, at each sign-in, and
    before the machine's own frames are read: the push is the first command
    the new connection carries."""
    from services.engines import token_fanout
    asked: list[str] = []

    async def _signed_in(machine_id):
        asked.append(machine_id)
    monkeypatch.setattr(token_fanout, "machine_signed_in", _signed_in)
    m = lf.FakeMachine(cm, "m1")

    async def _sign_in():
        if not compute:
            return await m.connect()
        return await cm.register("m1", lf._FakeSocket(m), dict(m.capabilities), satellite_version=m.version,
                                 report_id=satellite_link.new_report_id(), compute=True)
    await _sign_in()
    await _settle()
    assert asked == ["m1"]
    await _sign_in()
    await _settle()
    assert asked == ["m1", "m1"]



@pytest.mark.asyncio
async def test_a_machine_registers_while_another_ones_old_socket_is_still_closing(cm):
    first = lf.FakeMachine(cm, "m-dup")
    await first.connect()
    gate = asyncio.Event()

    async def close_that_waits(code=1000, reason=""):
        await gate.wait()

    first.ws.close = close_that_waits
    second = lf.FakeMachine(cm, "m-dup")
    replacing = asyncio.ensure_future(second.connect())
    await _settle()
    assert cm.get_connection("m-dup") is not None and not replacing.done()
    started = time.monotonic()
    await lf.FakeMachine(cm, "m-other").connect()
    assert time.monotonic() - started < bound(0.5)
    # The replaced machine is already served by its new connection.
    assert cm.get_connection("m-dup").ws is second.ws
    gate.set()
    await replacing


@pytest.mark.asyncio
async def test_a_replaced_connections_sessions_and_the_held_ones_move_to_the_new_one(cm):
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    queue = cm.create_session_queue("m1", "s-1", "claude-code-cli", maxsize=8)
    await m.drop()
    assert cm.is_session_in_grace("m1", "s-1")
    conn = await m.connect()
    assert conn.session_queues["s-1"] is queue
    again = lf.FakeMachine(cm, "m1")
    conn2 = await again.connect()
    assert conn2.session_queues["s-1"] is queue


@pytest.mark.asyncio
async def test_a_connect_and_a_leave_tell_the_link_callback_once_each(cm):
    heard: list[str] = []
    cm.set_link_callback(heard.append)
    first = lf.FakeMachine(cm, "m-live")
    await first.connect()
    assert heard == ["m-live"]
    # A duplicate replaces the connection: one more connect, and the old
    # socket's own deregister is stale, so no leave.
    old = first.conn
    second = lf.FakeMachine(cm, "m-live")
    await second.connect()
    await cm.deregister("m-live", expected=old)
    assert heard == ["m-live", "m-live"]
    await second.drop()
    assert heard == ["m-live", "m-live", "m-live"]
    assert cm.get_connection("m-live") is None


@pytest.mark.asyncio
async def test_a_link_callback_that_raises_undoes_neither_the_connect_nor_the_leave(cm):
    def broken(machine_id: str) -> None:
        raise RuntimeError("listener fault")
    cm.set_link_callback(broken)
    m = lf.FakeMachine(cm, "m-raise")
    await m.connect()
    assert cm.get_connection("m-raise") is not None
    await m.drop()
    assert cm.get_connection("m-raise") is None


# -- the reconnect steps ---------------------------------------------------


@pytest.mark.asyncio
async def test_ten_quick_reconnects_run_each_step_once_now_and_once_at_the_windows_end(cm, monkeypatch):
    monkeypatch.setattr(satellite_link, "STEP_WINDOW_S", 0.3)
    from services.remote import tools_ahead
    ahead: list[str] = []

    async def _ahead(machine_id):
        ahead.append(machine_id)
    monkeypatch.setattr(tools_ahead, "after_signin", _ahead)
    m = lf.FakeMachine(cm, "m-flap")
    for _ in range(10):
        await m.connect()
        await _settle()
        await m.drop()
    await m.connect()
    await _settle()
    # The tools' step is install ahead's sign-in run (its sync verifies).
    assert ahead == ["m-flap"]
    assert len(cm.machine_reads) == 1
    await asyncio.sleep(0.4)
    await _settle()
    assert ahead == ["m-flap", "m-flap"]
    assert len(cm.machine_reads) == 2


@pytest.mark.asyncio
async def test_a_step_due_at_the_windows_end_is_skipped_for_a_machine_that_left(cm, monkeypatch):
    monkeypatch.setattr(satellite_link, "STEP_WINDOW_S", 0.2)
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    await _settle()
    await m.drop()
    await m.connect()
    await m.drop()
    await asyncio.sleep(0.3)
    await _settle()
    assert len(cm.machine_reads) == 1


@pytest.mark.asyncio
async def test_the_machine_rows_cache_carries_over_to_the_next_connection(cm):
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    await cm.drain_persists()
    for _ in range(50):
        if cm.get_connection("m1").name:
            break
        await asyncio.sleep(0.01)
    assert cm.get_connection("m1").max_sessions == 7
    await m.drop()
    conn = await m.connect()
    assert (conn.name, conn.max_sessions) == ("name-m1", 7)


@pytest.mark.asyncio
async def test_a_broadcast_reaches_every_connected_machine(cm):
    a, b = lf.FakeMachine(cm, "m-a"), lf.FakeMachine(cm, "m-b")
    await a.connect()
    await b.connect()
    assert cm.broadcast_fire_and_forget({"type": "platform_stopping", "back_within_s": 60}) == 2
    await _settle()
    for m in (a, b):
        assert any(f.get("type") == "platform_stopping" for f in m.frames)


# -- the alive report --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hook_gets_one_complete_report_per_sign_in_after_the_adoption_callback(cm):
    order: list[str] = []
    reports: list[satellite_link.AliveReport] = []

    async def adoption(machine_id, sessions, others):
        await asyncio.sleep(0.05)
        order.append("adoption")

    async def hook(report):
        order.append("hook")
        reports.append(report)

    cm.set_sessions_alive_callback(adoption)
    satellite_link.add_alive_report_hook(hook, name="test")
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    await _report(m, conn, terminals=["t-1"], sessions=["s-1"], others=["s-2"])
    await asyncio.sleep(0.15)
    assert order == ["adoption", "hook"]
    (report,) = reports
    assert report.complete and report.is_current()
    assert report.headless == {"s-1", "s-2"} and report.terminals == {"t-1"}
    assert report.signed_in_at <= time.monotonic()
    await m.drop()
    conn = await m.connect()
    await _report(m, conn)
    await asyncio.sleep(0.15)
    assert len(reports) == 2 and not reports[0].is_current()


@pytest.mark.asyncio
async def test_the_held_lines_end_once_the_adoption_took_its_own(cm, monkeypatch):
    """The lines a machine sends before its report wait for the adoption;
    once the report of headless sessions has been handled the rest go, a
    later line waits for nothing, and a drop ends a new sign-in's window."""
    from auth.path_policy import SecurityContext
    from core import placement
    from core.remote import satellite_grace as sg
    from core.session import session_state
    for sid in ("s-1", "s-2"):
        monkeypatch.setitem(session_state._session_security, sid, SecurityContext(
            role="editor", username="", agent="a", is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m1")))
    taken: list = []

    async def adoption(machine_id, sessions, others):
        await asyncio.sleep(0.02)
        taken.extend(cm.take_held_lines(machine_id, "s-1"))

    cm.set_sessions_alive_callback(adoption)
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    for sid in ("s-1", "s-2"):
        await m.say({"type": "session_event", "session_id": sid, "event": {"type": "result", "of": sid}})
    held = cm._held_lines["m1"]
    assert set(held.lines) == {(sg.SESSION, "s-1"), (sg.SESSION, "s-2")}
    await _report(m, conn, sessions=["s-1", "s-2"])
    await asyncio.sleep(0.1)
    assert taken == [{"type": "result", "of": "s-1"}]
    assert held.ended and held.lines == {}
    await m.say({"type": "session_event", "session_id": "s-2", "event": {"type": "result"}})
    await m.say({"type": "transcript_lines", "session_id": "t-1", "lines": ["x"]})
    assert held.lines == {}
    # A sign-in that replaces the connection opens a new window.
    await m.connect()
    again = cm._held_lines["m1"]
    await m.say({"type": "session_event", "session_id": "s-2", "event": {"type": "result"}})
    assert again is not held and list(again.lines) == [(sg.SESSION, "s-2")]
    await m.drop()
    conn = await m.connect()
    fresh = cm._held_lines["m1"]
    assert fresh is not again and again.ended and not fresh.ended
    await m.drop()
    assert fresh.ended and "m1" not in cm._held_lines


@pytest.mark.asyncio
async def test_a_machines_sessions_are_back_after_its_first_complete_report(cm):
    """With no hook at all: a home start reads it to know the adoption ran."""
    adopted: list[str] = []

    async def adoption(machine_id, sessions, others):
        await asyncio.sleep(0.02)
        adopted.append(machine_id)

    cm.set_sessions_alive_callback(adoption)
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    assert not satellite_link.reported_since_boot("m1")
    await _report(m, conn, sessions=["s-1"], others=None)
    await asyncio.sleep(0.1)
    assert adopted == ["m1"] and not satellite_link.reported_since_boot("m1")
    await m.drop()
    conn = await m.connect()
    await _report(m, conn, sessions=["s-1"], others=[])
    await asyncio.sleep(0.1)
    assert satellite_link.reported_since_boot("m1")
    assert not satellite_link.reported_since_boot("m2")


@pytest.mark.asyncio
async def test_a_machines_sessions_are_not_back_while_an_adoption_is_on_its_way(cm):
    """A kept turn and a kept terminal bind in a task of their own, after
    the report: until each restored its binding the machine's sessions are
    not all in the pool's map."""
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    satellite_link.adoption_begins("m1", "s-turn")
    satellite_link.adoption_begins("m1", "s-terminal")
    await _report(m, conn, sessions=["s-turn"], others=[])
    await asyncio.sleep(0.05)
    assert not satellite_link.reported_since_boot("m1")
    satellite_link.adoption_ends("m1", "s-turn")
    satellite_link.adoption_ends("m1", "s-turn")          # its task's end after its binding: once is enough
    assert not satellite_link.reported_since_boot("m1")
    satellite_link.adoption_ends("m1", "s-terminal")
    assert satellite_link.reported_since_boot("m1")
    satellite_link.adoption_ends("m2", "s-none")          # a machine with none on its way
    satellite_link.adoption_begins("m1", "s-late")
    satellite_link.reset_hooks()
    assert not satellite_link._adopting


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["no other list", "partial", "no id"])
async def test_a_report_is_complete_only_for_this_sign_ins_own_whole_lists(cm, fault):
    reports: list = []
    satellite_link.add_alive_report_hook(AsyncMock(side_effect=reports.append), name="test")
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    kwargs: dict = {"sessions": ["s-1"], "others": ["s-2"]}
    if fault == "no other list":
        kwargs["others"] = None
    elif fault == "partial":
        kwargs["partial"] = True
    else:
        kwargs["report_id"] = ""
    await _report(m, conn, **kwargs)
    await _settle()
    (report,) = reports
    assert report.complete is False
    assert "s-1" in report.headless


@pytest.mark.asyncio
async def test_an_older_pair_sent_late_waits_for_this_sign_ins_own(cm):
    """The machine's control queue outlives a link: on one that dropped before
    its pair went out, the old pair goes first on the next and the fresh one
    follows. The old pair is no report and costs this sign-in nothing."""
    reports: list = []
    adopted: list = []

    async def adoption(machine_id, sessions, others):
        adopted.append([s["session_id"] for s in sessions])

    cm.set_sessions_alive_callback(adoption)
    satellite_link.add_alive_report_hook(AsyncMock(side_effect=reports.append), name="test")
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    await _report(m, conn, sessions=["s-then"], report_id="an-earlier-sign-in")
    await _settle()
    assert reports == [] and adopted == []
    await _report(m, conn, sessions=["s-now"])
    await _settle()
    (report,) = reports
    assert report.complete is True and report.headless == {"s-now"}
    assert adopted == [["s-now"]]


@pytest.mark.asyncio
async def test_a_hook_is_not_called_for_a_connection_gone_while_adoption_ran(cm):
    gate = asyncio.Event()
    seen: list = []

    async def adoption(machine_id, sessions, others):
        await gate.wait()

    cm.set_sessions_alive_callback(adoption)
    satellite_link.add_alive_report_hook(AsyncMock(side_effect=seen.append), name="test")
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    await _report(m, conn, sessions=["s-1"])
    await _settle()
    await m.drop()
    gate.set()
    await _settle()
    assert seen == []


@pytest.mark.asyncio
async def test_a_terminals_frame_alone_is_delivered_incomplete_after_the_wait(cm, monkeypatch):
    monkeypatch.setattr(satellite_link, "REPORT_WAIT_S", 0.1)
    reports: list = []
    satellite_link.add_alive_report_hook(AsyncMock(side_effect=reports.append), name="test")
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    await m.say({"type": "pty_alive", "session_ids": ["t-1"]})
    await _settle()
    assert reports == []
    await asyncio.sleep(0.2)
    (report,) = reports
    assert report.complete is False and report.terminals == {"t-1"}


@pytest.mark.asyncio
async def test_no_report_is_delivered_for_a_connection_that_dropped(cm, monkeypatch):
    monkeypatch.setattr(satellite_link, "REPORT_WAIT_S", 0.1)
    hook = AsyncMock()
    satellite_link.add_alive_report_hook(hook, name="test")
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    await m.say({"type": "pty_alive", "session_ids": []})
    await m.drop()
    await asyncio.sleep(0.2)
    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_slow_hook_delays_only_its_own_machine_and_the_newest_report_wins(cm):
    gate = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def hook(report):
        seen.append((report.machine_id, report.report_id))
        if report.machine_id == "m-slow":
            await gate.wait()

    satellite_link.add_alive_report_hook(hook, name="test")
    slow, other = lf.FakeMachine(cm, "m-slow"), lf.FakeMachine(cm, "m-other")
    conn = await slow.connect()
    await _report(slow, conn)
    await _settle()
    first = seen[0][1]
    conn = await other.connect()
    await _report(other, conn)
    await _settle()
    assert ("m-other", cm._links.get("m-other").assembly.report_id) in seen
    # Two more sign-ins of the slow machine while its hook still runs: the
    # older of the two is skipped.
    ids = []
    for _ in range(2):
        again = lf.FakeMachine(cm, "m-slow")
        conn = await again.connect()
        ids.append(cm._links.get("m-slow").assembly.report_id)
        await _report(again, conn)
        await _settle()
    assert [rid for mid, rid in seen if mid == "m-slow"] == [first]
    gate.set()
    await _settle()
    assert [rid for mid, rid in seen if mid == "m-slow"] == [first, ids[1]]


@pytest.mark.asyncio
async def test_a_hook_that_raises_stops_neither_the_next_hook_nor_the_adoption(cm):
    adoption, second = AsyncMock(side_effect=RuntimeError("boom")), AsyncMock()
    cm.set_sessions_alive_callback(adoption)
    satellite_link.add_alive_report_hook(AsyncMock(side_effect=RuntimeError("boom")), name="first")
    satellite_link.add_alive_report_hook(second, name="second")
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    await _report(m, conn)
    await _settle()
    adoption.assert_awaited_once()
    second.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_replaced_sockets_alive_frames_are_not_read_as_the_new_connections():
    from ws import satellite as ws_satellite

    class _Manager:
        def __init__(self):
            self.handled = []

        def get_connection(self, machine_id):
            return "the-new-connection"

        async def handle_message(self, machine_id, msg):
            self.handled.append(msg["type"])

    manager = _Manager()
    for kind in ("heartbeat", "pty_alive", "sessions_alive", "ack"):
        await ws_satellite._dispatch_frame(manager, "m1", SimpleNamespace(), f'{{"type": "{kind}"}}')
    assert manager.handled == ["ack"]


# -- terminals: claims and owed exits ------------------------------------------


def _terminal(machine_id: str, session_id: str) -> remote_pty.RemotePtyProcess:
    return remote_pty.RemotePtyProcess(
        machine_id=machine_id, session_id=session_id, rows=24, cols=80,
        on_output=lambda data: None, on_exit=None, scrollback_limit=1024)


@pytest.mark.asyncio
async def test_a_claimed_terminal_is_not_closed_as_an_orphan(cm):
    async def claim(machine_id, ids):
        assert ids == {"t-keep", "t-orphan"}
        _terminal(machine_id, "t-keep")
        return {"t-keep"}

    satellite_link.add_terminal_claim(claim, name="test")
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    await m.say({"type": "pty_alive", "session_ids": ["t-keep", "t-orphan"]})
    await _settle()
    closed = [f["session_id"] for f in m.frames if f.get("type") == "pty_close"]
    assert closed == ["t-orphan"]


@pytest.mark.asyncio
async def test_a_claim_that_raises_leaves_the_reconcile_as_it_was(cm):
    satellite_link.add_terminal_claim(AsyncMock(side_effect=RuntimeError("boom")), name="test")
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    await m.say({"type": "pty_alive", "session_ids": ["t-orphan"]})
    await _settle()
    assert [f["session_id"] for f in m.frames if f.get("type") == "pty_close"] == ["t-orphan"]


# -- the heartbeat monitor ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_monitor_drops_a_silent_machine_and_never_the_connection_that_replaced_it(cm, monkeypatch):
    monkeypatch.setattr(sc, "_MONITOR_TICK_S", 0.05)
    monkeypatch.setattr(sc, "_SILENT_DROP_S", 0.2)
    monkeypatch.setattr(cm, "_evaluate_admin_machine_alerts", AsyncMock())
    silent, fresh = lf.FakeMachine(cm, "m-silent", mode=lf.FROZEN), lf.FakeMachine(cm, "m-fresh")
    await silent.connect()
    await fresh.connect()
    monitor = asyncio.ensure_future(cm.heartbeat_monitor())
    try:
        for _ in range(30):
            await asyncio.sleep(0.05)
            cm.get_connection("m-fresh").last_heartbeat = time.monotonic()
            if not cm.is_connected("m-silent"):
                break
        assert not cm.is_connected("m-silent") and cm.is_connected("m-fresh")
        assert silent.ws.closed == (4003, "Heartbeat timeout")
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_deregister_of_a_judged_connection_leaves_its_successor(cm):
    m = lf.FakeMachine(cm, "m1")
    judged = await m.connect()
    newer = await lf.FakeMachine(cm, "m1").connect()
    await cm.deregister("m1", expected=judged)
    assert cm.get_connection("m1") is newer


# -- what counts as contact ------------------------------------------------


@pytest.mark.asyncio
async def test_every_frame_of_a_machine_is_contact_and_a_frozen_one_is_still_cut(cm, monkeypatch):
    """A machine's heartbeat waits its turn behind manifest pages, inline file
    changes and tunnel chunks on one lane, so one on a slow uplink is sending
    all along with no heartbeat through. A stopped process sends nothing."""
    from ws import satellite as ws_satellite
    monkeypatch.setattr(sc, "_MONITOR_TICK_S", 0.05)
    monkeypatch.setattr(sc, "_SILENT_DROP_S", 0.3)
    monkeypatch.setattr(cm, "_evaluate_admin_machine_alerts", AsyncMock())
    sending, frozen = lf.FakeMachine(cm, "m-sending"), lf.FakeMachine(cm, "m-frozen", mode=lf.FROZEN)
    conn = await sending.connect()
    await frozen.connect()
    monitor = asyncio.ensure_future(cm.heartbeat_monitor())
    frame = '{"type": "file_manifest", "command_id": "c1", "more": true, "files": []}'
    try:
        for _ in range(12):
            await ws_satellite._dispatch_frame(cm, "m-sending", conn, frame)
            await asyncio.sleep(0.05)
        assert cm.get_connection("m-sending") is conn and sending.ws.closed is None
        assert not cm.is_connected("m-frozen") and frozen.ws.closed == (4003, "Heartbeat timeout")
        # A machine that goes quiet is judged from its last frame.
        await asyncio.sleep(0.5)
        assert not cm.is_connected("m-sending") and sending.ws.closed == (4003, "Heartbeat timeout")
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_heartbeat_at_zero_sessions_hands_a_waiting_update_to_the_idle_hooks_once(cm):
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    idle: list[str] = []

    async def hook(machine_id):
        idle.append(machine_id)
    satellite_link.add_idle_hook(hook, name="t")
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 0})
    cm.links.update_waits("m1", "9.9.9")
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 1})
    await _settle()
    assert idle == []
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 0})
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 0})
    await _settle()
    assert idle == ["m1"] and cm.links.update_waiting("m1") == ""


@pytest.mark.asyncio
async def test_a_start_on_its_way_to_the_machine_keeps_its_waiting_update_back(cm):
    """A start counts as a session from its build on: the waiting update is
    pushed at the first report of no session once no start is on its way,
    never under one (the push ends that session as it begins)."""
    m = lf.FakeMachine(cm, "m1")
    await m.connect()
    idle: list[str] = []

    async def hook(machine_id):
        idle.append(machine_id)
    satellite_link.add_idle_hook(hook, name="t")
    cm.links.update_waits("m1", "9.9.9")
    cm.links.hold_for_start("m1", 60.0)
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 0})
    await _settle()
    assert idle == [] and cm.links.update_waiting("m1") == "9.9.9"
    # The remote layer begins the start, and the build's hold runs out.
    cm.links.start_begins("m1")
    cm.links.get("m1").start_held_until = 0.0
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 0})
    await _settle()
    assert idle == [] and cm.links.starting("m1")
    cm.links.start_ends("m1")
    cm.links.start_ends("m1")
    assert not cm.links.starting("m1") and cm.links.get("m1").starts == 0
    await cm._frame_heartbeat("m1", {"type": "heartbeat", "active_sessions": 0})
    await _settle()
    assert idle == ["m1"] and cm.links.update_waiting("m1") == ""
