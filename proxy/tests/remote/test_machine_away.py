"""A machine that cannot serve a session yet: one its version keeps out, and
one still in its update (``services/remote/machine_away``). A start waits a
bounded time for a machine in its update, its restart included, and starts
as if nothing happened once it is done; past the bound it is refused with
the one sentence, and no session is built there with tools left out."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import placement
from core.remote import satellite_connection, satellite_link
from services.remote import machine_away
from storage import remote_store

FINISHING = "Laptop is finishing its update. Try again in a minute."


def _laptop(why: str = "", *, port: int = 41111, ok: bool = True) -> placement.PlacementCapabilities:
    return placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m-1", label="Laptop",
                                           satellite_version="0.5.196", exit_port=port, exit_ok=ok, exit_why=why)


def _installing() -> placement.PlacementCapabilities:
    return _laptop("installing", port=0, ok=False)


class _Machine:
    """The connection manager as machine_away reads it: the links, the
    connection and the exit's report, set by the test step by step."""

    def __init__(self, *, connected: bool = True, why: str = "") -> None:
        self.links = satellite_link.MachineLinks(None)
        self.connected = connected
        self.why = why

    def is_connected(self, mid: str) -> bool:
        return self.connected

    def exit_report(self, mid: str) -> dict:
        return {"why": self.why}


@pytest.fixture
def machine(monkeypatch):
    m = _Machine()
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: m)
    return m


def _reads(monkeypatch, *placements: placement.PlacementCapabilities) -> list[tuple]:
    """``placement_of`` answers ``placements`` in turn (the last one again)."""
    queue, asked = list(placements), []

    def read(target, user_sub, agent, **kw):
        asked.append((target, user_sub, agent, kw))
        return queue.pop(0) if len(queue) > 1 else queue[0]
    monkeypatch.setattr(remote_store, "placement_of", read)
    return asked


def test_a_placement_that_is_not_in_its_update_comes_back_at_once(machine, monkeypatch):
    for where in (placement.LOCAL_PLACEMENT, _laptop(), _laptop("packages", port=0, ok=False)):
        asked = _reads(monkeypatch, where)
        got = asyncio.run(machine_away.settled_placement("m-1", "u-1", "agent", compute={"id": "c"}, step=0))
        assert got is where and asked == [("m-1", "u-1", "agent", {"compute": {"id": "c"}})]


def test_a_start_holds_its_machine_so_a_waiting_update_waits(machine, monkeypatch):
    _reads(monkeypatch, _laptop())
    asyncio.run(machine_away.settled_placement("m-1", "u-1", "agent", step=0))
    assert machine.links.starting("m-1")
    _reads(monkeypatch, placement.LOCAL_PLACEMENT)
    asyncio.run(machine_away.settled_placement("local", "u-1", "agent", step=0))
    assert not machine.links.starting("local")


def test_the_start_waits_for_the_exit_and_builds_on_the_placement_read_again(machine, monkeypatch):
    machine.why = "installing"
    asked = _reads(monkeypatch, _installing(), _laptop())
    steps = []

    async def sleep(_s):
        steps.append(1)
        if len(steps) == 3:
            machine.why = ""
    monkeypatch.setattr(machine_away.asyncio, "sleep", sleep)
    got = asyncio.run(machine_away.settled_placement("m-1", "u-1", "agent", step=0))
    assert (got.exit_port, got.exit_ok, got.exit_why) == (41111, True, "") and len(asked) == 2


def test_the_start_waits_out_the_restart_of_an_update_sent_to_the_machine(machine, monkeypatch):
    """The update was sent: the machine still answers, drops off for its
    restart and signs in on the new version. The start goes on then."""
    machine.links.update_sent("m-1", "9.9.9")
    asked = _reads(monkeypatch, _laptop(), _laptop())
    steps = []

    async def sleep(_s):
        steps.append(1)
        if len(steps) == 1:
            machine.connected = False
        if len(steps) == 4:
            machine.connected = True
            machine.links.update_sent("m-1", "")
    monkeypatch.setattr(machine_away.asyncio, "sleep", sleep)
    got = asyncio.run(machine_away.settled_placement("m-1", "u-1", "agent", step=0))
    assert got.exit_ok and len(steps) == 4 and len(asked) == 2


def test_past_the_bound_the_start_is_refused_with_the_one_sentence(machine, monkeypatch):
    machine.why = "installing"
    _reads(monkeypatch, _installing())
    with pytest.raises(machine_away.MachineAway) as refused:
        asyncio.run(machine_away.settled_placement("m-1", "u-1", "agent", bound=0.05, step=0.01))
    assert str(refused.value) == FINISHING
    assert isinstance(refused.value, placement.PlacementRefused)


def test_a_chats_pinned_machine_in_its_restart_is_waited_for_then_read(machine, monkeypatch):
    from services.remote import chat_targets
    monkeypatch.setattr(chat_targets, "label_of", lambda target, machine=None: "Laptop")
    asyncio.run(machine_away.update_done("m-1", step=0))
    machine.links.update_sent("m-1", "9.9.9")
    machine.connected = False
    with pytest.raises(machine_away.MachineAway) as refused:
        asyncio.run(machine_away.update_done("m-1", bound=0.05, step=0.01))
    assert str(refused.value) == FINISHING and machine.links.starting("m-1")
    steps = []

    async def sleep(_s):
        steps.append(1)
        machine.connected = True
        machine.links.update_sent("m-1", "")
    monkeypatch.setattr(machine_away.asyncio, "sleep", sleep)
    asyncio.run(machine_away.update_done("m-1", step=0))
    assert steps == [1]


def test_a_machine_in_its_update_reads_the_one_sentence_at_every_door(machine):
    assert machine_away.away_sentence("m-1", "Laptop", "Laptop is offline.") == "Laptop is offline."
    machine.links.update_sent("m-1", "9.9.9")
    assert machine_away.away_sentence("m-1", "Laptop", "Laptop is offline.") == FINISHING
    assert machine_away.away_sentence("m-1", "", "") == "This computer is finishing its update. Try again in a minute."
    machine.links.update_sent("m-1", "")
    machine.why = "installing"
    assert machine_away.finishing_update("m-1") and machine_away.held_out("m-1")
    machine.connected = False
    assert not machine_away.finishing_update("m-1")
    assert machine_away.away_sentence("m-1", "Laptop", "Laptop is offline.") == "Laptop is offline."


def test_a_machine_with_no_name_reads_this_computer_inside_a_sentence():
    assert machine_away.label_at_start("") == "This computer" and machine_away.label_inside("") == "this computer"
    assert machine_away.label_at_start("Desk") == machine_away.label_inside("Desk") == "Desk"


def test_a_door_gives_the_update_sentence_for_a_machine_its_version_keeps_out():
    """One sentence for every door: an admin updates the machine, or on a
    plaintext link the installer does; any other machine keeps the door's
    own sentence."""
    try:
        assert machine_away.away_sentence("m-1", "Laptop", "Laptop is offline.") == "Laptop is offline."
        machine_away.note_refused("m-1", "0.5.180", plaintext=False)
        assert machine_away.refused_version("m-1") == ("0.5.180", False)
        assert machine_away.away_sentence("m-1", "Laptop", "Laptop is offline.") == (
            "Laptop needs an update. Ask an admin to update it, then try again.")
        machine_away.note_refused("m-1", "0.5.180", plaintext=True)
        assert machine_away.away_sentence("m-1", "", "") == (
            "This computer needs an update. Run the OtoDock installer on it again, then try again.")
        assert machine_away.away_sentence("m-2", "Desk", "Desk is offline.") == "Desk is offline."
    finally:
        machine_away.clear_refused("m-1")
    assert machine_away.refused_version("m-1") is None


def test_the_offline_doors_read_the_one_sentence(monkeypatch):
    """The chat's targets list says why it cannot run there, with the update
    sentence for a machine its version keeps out."""
    import inspect

    from core.remote import remote_session_start
    from services.apps import app_steps
    from services.remote import chat_move, chat_reload, chat_targets
    from services.scheduler import task_run
    from ws import (
        dashboard_chat_send,
        dashboard_warmup_spawn,
        dashboard_warmup_tail,
        duplex_attach,
    )
    for module in (chat_targets, chat_move, task_run, app_steps, dashboard_warmup_spawn, remote_session_start):
        assert "machine_away.away_sentence" in inspect.getsource(module), module.__name__
    # The doors that hold a chat's row ask for its machine's sentence.
    for module in (chat_reload, duplex_attach, dashboard_chat_send, dashboard_warmup_tail):
        assert "machine_away.sentence_for_chat" in inspect.getsource(module), module.__name__


def test_every_session_build_reads_its_placement_through_the_one_wait():
    """The chat's, the task's, the delivery's and the wake's builds: none
    reads a placement past the wait, so none builds on a machine in its
    update."""
    import inspect

    from core.config import config_builder, task_config_builder
    from services.scheduler import wake_session
    for module, count in ((config_builder, 1), (task_config_builder, 2), (wake_session, 1)):
        source = inspect.getsource(module)
        assert source.count("machine_away.settled_placement(") == count, module.__name__
        assert "remote_store.placement_of" not in source and "exit_settled" not in source, module.__name__
    assert "machine_away.update_done(" in inspect.getsource(config_builder)


def test_a_start_built_before_the_update_was_sent_is_refused_with_the_one_sentence(machine):
    """An update pushed by hand (Update now) while a start's session was
    being built: the remote start refuses before it asks the machine
    anything, with the update's sentence, and counts no start left over."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from auth.path_policy import SecurityContext
    from core.execution_layer import AgentConfig
    from core.remote.remote_execution import RemoteExecutionLayer
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    cm = MagicMock()
    cm.links = machine.links
    cm.is_connected.return_value = True
    cm.satellite_name.return_value = "Laptop"
    cm.send_command = AsyncMock()
    layer._cm, layer._sessions, layer._spawning = cm, {}, set()
    layer._evict_idle_on_machine = AsyncMock()
    machine.links.update_sent("m-1", "9.9.9")
    config = AgentConfig(agent_name="agent-1", execution_target="m-1", execution_path="claude-code-cli",
                         security_context=SecurityContext(role="manager", username="", agent="agent-1",
                                                          is_admin_agent=False, session_scope="agent"))
    with patch("core.remote.remote_session_start.placement_refuses", return_value=False), \
         pytest.raises(machine_away.MachineAway) as refused:
        asyncio.run(layer.start_session("s-1", config))
    assert str(refused.value) == FINISHING
    cm.send_command.assert_not_awaited()
    assert machine.links.get("m-1").starts == 0


def test_a_chats_sentence_names_its_machine_only_when_its_version_keeps_it_out(monkeypatch):
    from services.remote import chat_targets
    monkeypatch.setattr(chat_targets, "label_of", lambda target, machine=None: "Laptop")
    row = {"execution_target": "m-refused"}
    assert machine_away.sentence_for_chat(row, "Its own words.") == "Its own words."
    machine_away.note_refused("m-refused", "0.5.168", plaintext=False)
    try:
        assert machine_away.sentence_for_chat(row, "Its own words.") == (
            "Laptop needs an update. Ask an admin to update it, then try again.")
        assert machine_away.sentence_for_chat(row, "x", label="Desk") == (
            "Desk needs an update. Ask an admin to update it, then try again.")
        assert machine_away.sentence_for_chat({"execution_target": "local"}, "Its own words.") == "Its own words."
        assert machine_away.sentence_for_chat(None, "Its own words.") == "Its own words."
    finally:
        machine_away.clear_refused("m-refused")


def test_a_removed_machine_leaves_no_refusal_behind(temp_db):
    """The admin's removal and the machine's own uninstall both clear the
    version record, so nothing of a removed machine stays in memory."""
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.remote import remote_machines
    from storage import remote_store
    made = remote_store.create_remote_machine(
        machine_id=f"m-{uuid.uuid4().hex[:12]}", name="laptop", registered_by="sub-admin",
        pairing_scope=placement.PAIRING_ADMIN, paired=True)
    secret = remote_store.exchange_pairing_token(made["id"], made["pairing_token"])
    machine_away.note_refused(made["id"], "0.5.168", plaintext=False)
    app = FastAPI()
    app.include_router(remote_machines.router)
    r = TestClient(app).delete(f"/v1/satellite/{made['id']}/self-uninstall", headers={"X-Machine-Secret": secret})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert machine_away.refused_version(made["id"]) is None


@pytest.mark.parametrize("error", [RuntimeError("Satellite m-1 not connected"),
                                   machine_away.MachineAway(FINISHING)], ids=["dropped-off", "refused"])
def test_a_chat_whose_machine_is_in_its_update_ends_its_start_with_the_one_sentence(machine, monkeypatch, error):
    """The warm-up's card for a start that met the machine in its update:
    the machine dropped off for its restart, or the remote start refused it.
    The card reads the update's sentence, as a machine that is away."""
    from unittest.mock import AsyncMock

    import ws.dashboard  # noqa: F401  (the tail's modules load through it)
    from core.session import warmup_registry
    from services.remote import chat_targets
    from ws import dashboard_warmup_tail as tail
    emitted: list[dict] = []
    monkeypatch.setattr(warmup_registry, "emit", AsyncMock(side_effect=lambda cid, frame: emitted.append(frame)))
    monkeypatch.setattr(chat_targets, "label_of", lambda target, machine=None: "Laptop")
    machine.links.update_sent("m-1", "9.9.9")
    w = SimpleNamespace(wcid="c-1", w_pinned="m-1", w_place="", msg={"type": "warmup"})
    asyncio.run(tail.WarmupTail._tail_failed(None, w, tail._TailRun(), "spawn", error))
    assert emitted == [{"type": "warmup_failed", "chat_id": "c-1", "error": FINISHING,
                        "reason": "target_unavailable"}]
