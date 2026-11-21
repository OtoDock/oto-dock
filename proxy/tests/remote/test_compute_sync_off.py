"""Nothing syncs to a compute machine, and its sessions are the platform's
own everywhere a reader asks where a session is.

Each choke point of the paired-machine file flows keys on the record's
``files_synced`` or the machine's kind: the steer barrier, the
``file_changed`` gate, the workspace fan-out, the gateway lease push, the
relocated outputs, the retention's machine pass, the admin's Sync Now. The
resume path reads the session's own place before the chat's pin and
adopts a compute session with its files unsynced and its login file local;
the lanes and the delivery agree a compute session serves the pin
``local``; the busy counter counts it once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config as app_config
from auth.path_policy import SecurityContext
from core import placement
from core.remote import satellite_file_transfer
from core.remote.remote_execution import RemoteExecutionLayer
from core.remote.remote_session_info import RemoteSessionInfo
from core.session import session_state

COMPUTE = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one")


class _Cm:
    def __init__(self, compute=("c-1",), connected=("c-1", "m-1"), own=()):
        self._compute = set(compute) | set(own)
        self._own = set(own)
        self._connected = list(connected)
        self.sent: list[dict] = []
        self.dropped: set[str] = set()

    def is_compute(self, mid):
        return mid in self._compute

    def shares_folders(self, mid):
        return mid in self._compute and mid not in self._own

    def owns_copy(self, mid):
        return mid in self._own

    def machine_dropped_recently(self, mid):
        return mid in self.dropped

    def satellite_version(self, mid):
        return "0.5.173"

    def take_held_lines(self, mid, sid):
        return []

    def compute_machine_ids(self):
        return frozenset(self._compute)

    def get_connected_machines(self):
        return list(self._connected)

    def tunnel_port(self, mid):
        return 43210

    def create_session_queue(self, mid, sid, path, *, maxsize=None):
        return asyncio.Queue()

    async def send_command(self, mid, msg, timeout=60.0):
        self.sent.append(msg)
        return {}

    async def send_fire_and_forget(self, mid, msg):
        self.sent.append(msg)


def _info(sid="s-1", machine="c-1", *, files_synced=False, alive=True,
          compute: bool | None = None) -> RemoteSessionInfo:
    info = RemoteSessionInfo(session_id=sid, machine_id=machine, agent_name="personal-assistant",
                             execution_path="claude-code-cli", event_queue=asyncio.Queue(),
                             files_synced=files_synced,
                             compute=(not files_synced) if compute is None else compute)
    info.alive = alive
    return info


def _layer(cm: _Cm, *infos: RemoteSessionInfo) -> RemoteExecutionLayer:
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = cm
    layer._sessions = {i.session_id: i for i in infos}
    layer._spawning = set()
    return layer


@pytest.fixture
def remote_layer(monkeypatch):
    cm = _Cm()
    layer = _layer(cm, _info("s-c"), _info("s-m", "m-1", files_synced=True))
    monkeypatch.setattr("core.session.session_manager._remote_layer", layer)
    monkeypatch.setattr("core.session.session_manager._get_remote_layer", lambda: layer)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    return layer


# ---------------------------------------------------------------------------
# the choke points
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_compute_turn_waits_for_no_upload(monkeypatch):
    """The turn's barrier on in-flight uploads is a paired machine's: a
    compute session reads the platform's folder, where the upload already
    landed. The turn is driven to the step after the barrier, which the
    fake connection manager ends."""
    from core.remote import upload_inflight
    waited: list[str] = []

    async def _wait(agent, *, machine_id, ceiling):
        waited.append(machine_id)
        return True
    monkeypatch.setattr(upload_inflight, "wait_settled", _wait)

    class _Ended(Exception):
        pass

    class _Cm2(_Cm):
        async def wait_abort_acked(self, mid, sid):
            raise _Ended()
    layer = _layer(_Cm2(), _info("s-c"), _info("s-m", "m-1", files_synced=True))
    monkeypatch.setattr(RemoteExecutionLayer, "_adapter", staticmethod(lambda info: object()))
    for sid in ("s-c", "s-m"):
        with pytest.raises(_Ended):
            async for _ev in layer._send_turn(sid, "hello"):
                pass
    assert waited == ["m-1"]


def test_a_file_changed_frame_from_a_compute_machine_is_dropped(monkeypatch):
    cm = _Cm()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    frame = {"agent_slug": "personal-assistant", "path": "workspace/a.txt", "action": "modified",
             "session_id": "s-c"}
    assert satellite_file_transfer._admit_file_changed("c-1", dict(frame)) is None
    # The same frame from a paired machine, with its session's context bound
    # to that machine, passes the gate.
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant", is_admin_agent=False,
                          placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE,
                                                                    machine_id="m-1", label="Rack"))
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr("core.session.session_manager.live_session", lambda sid: None)
    assert satellite_file_transfer._admit_file_changed("m-1", dict(frame)) is not None


def test_the_fan_out_never_targets_a_compute_machine(remote_layer, monkeypatch):
    from core.session import interactive_session
    from services.remote import workspace_fanout
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant", is_admin_agent=False)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr("core.remote.file_sync.should_sync_to_target", lambda *a, **k: True)
    monkeypatch.setattr(interactive_session, "_sessions", {
        "t-c": SimpleNamespace(agent_name="personal-assistant", alive=True, target="c-1", username="alice", role="manager"),
        "t-m": SimpleNamespace(agent_name="personal-assistant", alive=True, target="m-1", username="alice", role="manager"),
    })
    assert workspace_fanout.fanout_targets("personal-assistant", "workspace/a.txt", shared_only=False) == ["m-1"]
    assert workspace_fanout._active_machine_ids("personal-assistant") == {"m-1"}
    assert [m for _s, m, _u, _r in workspace_fanout._interactive_remote_sessions("personal-assistant")] == ["m-1"]


@pytest.mark.asyncio
async def test_no_gateway_lease_reaches_a_compute_machine(monkeypatch):
    from core.credentials import mcp_gateway
    from core.remote import mcp_gateway_push
    monkeypatch.setattr(mcp_gateway, "credentials_of", lambda sid: {"github": object()})
    monkeypatch.setattr(mcp_gateway_push, "register", lambda *a: pytest.fail("a lease was registered for compute"))
    assert await mcp_gateway_push.push_session("s-c", "c-1", "hash", cm=_Cm()) == 0


@pytest.mark.asyncio
async def test_relocated_outputs_are_not_pushed_to_a_compute_machine(remote_layer, monkeypatch, tmp_path):
    from core.remote import remote_file_flow
    from services.mcp import mcp_output_relocation
    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda sid: True)
    monkeypatch.setattr(remote_layer._cm, "push_file", AsyncMock(side_effect=AssertionError("pushed to compute")),
                        raising=False)
    await mcp_output_relocation.push_relocated_to_satellite("s-c", [tmp_path / "shot.png"])


@pytest.mark.asyncio
async def test_retention_keeps_a_compute_sessions_ids_live_and_asks_no_compute_machine(remote_layer, monkeypatch):
    from services.infra import retention
    all_ids, by_machine = retention._remote_live_ids()
    assert {"s-c", "s-m"} <= all_ids
    assert "c-1" not in by_machine and by_machine["m-1"] == {"s-m"}
    asked: list[str] = []

    async def _ask(cm, mid, keep, dry_run):
        asked.append(mid)
        return {"machine_id": mid, "name": mid, "reached": True, "bytes": 0, "deleted": 0,
                "deleted_bytes": 0, "candidates": []}
    monkeypatch.setattr(retention, "_ask_machine_retention", _ask)

    async def _run_db(fn, *a, **k):
        return {}
    monkeypatch.setattr("storage.pg.run_db", _run_db)
    stats = {"remote_machines": [], "remote_session_files_bytes": 0, "remote_session_files_deleted": 0,
             "remote_bytes_freed": 0, "chats_flagged": 0, "remote_chats_flagged": 0}
    live = SimpleNamespace(pump_chat_ids=set())
    await retention._pass_remote_sessions(30, False, live, stats, True)
    assert asked == ["m-1"]


def test_sync_now_refuses_a_compute_machine(temp_db, monkeypatch):
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.remote import remote_machines
    from auth.providers import UserContext, get_current_user
    from storage import remote_store
    row = remote_store.create_remote_machine(machine_id=str(uuid.uuid4()), name=f"c-{uuid.uuid4().hex[:6]}",
                                             registered_by=None, pairing_scope=placement.PAIRING_COMPUTE)
    user = UserContext(sub="sub-admin", email="a@test.com", name="A", role="admin", agents=[], agent_roles={})

    async def _stub():
        return user
    app = FastAPI()
    app.include_router(remote_machines.router)
    app.dependency_overrides[get_current_user] = _stub
    r = TestClient(app).post(f"/v1/admin/remote-machines/{row['id']}/sync-mcps")
    assert r.status_code == 400 and r.json()["detail"] == remote_store.COMPUTE_SETTING_REFUSED


# ---------------------------------------------------------------------------
# the resume and the readers
# ---------------------------------------------------------------------------

def test_the_resume_target_reads_the_sessions_own_place_before_the_pin(monkeypatch):
    from core.remote import remote_resume
    cm = _Cm()
    layer = _layer(cm)
    monkeypatch.setattr("storage.database.get_chat_by_session",
                        lambda sid: {"id": "chat-1", "agent": "personal-assistant", "execution_path": "claude-code-cli",
                                     "execution_target": "local", "codex_thread_id": ""})
    monkeypatch.setattr(session_state, "placed_on", lambda sid, chat_id="": "c-1")
    target = remote_resume.RemoteResumeMixin._resume_target(layer, "s-gone", "", "alice")
    assert target[0] == "c-1" and target[1] == "claude-code-cli"


@pytest.mark.asyncio
async def test_an_adopted_compute_session_is_unsynced_with_its_login_file_local(monkeypatch, tmp_path):
    from core.remote import remote_resume
    from services.engines import subscription_pool, token_fanout
    from storage.billing import subscription_store
    cm = _Cm()
    layer = _layer(cm)
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(remote_resume.RemoteResumeMixin, "_adoptable", lambda self, sid, path, idle=False: True)
    monkeypatch.setattr(remote_resume.RemoteResumeMixin, "_reprovision_gateway", AsyncMock())
    monkeypatch.setattr(subscription_pool, "restore_session_binding", lambda sid: "sub-1")
    monkeypatch.setattr(subscription_store, "get_credential_data", lambda sid: {"oauth_token": {"accessToken": "at"}})
    monkeypatch.setattr(subscription_pool, "fan_out_current_token", lambda sid: None)
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant", is_admin_agent=False,
                          placement=COMPUTE)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    targets: list = []
    monkeypatch.setattr(token_fanout, "register_session_target", lambda sid, t: targets.append(t))
    taken = await layer.adopt_idle_session(machine_id="c-1", session_id="s-c", agent_name="personal-assistant",
                                           execution_path="claude-code-cli")
    assert taken and layer._sessions["s-c"].files_synced is False
    assert targets and targets[0].machine_id == "" and targets[0].host_dir.endswith("users/alice/.claude")
    assert targets[0].host_dir.startswith(str(tmp_path / "agents" / "personal-assistant"))
    # A paired machine's adoption keeps the machine target.
    targets.clear()
    taken = await layer.adopt_idle_session(machine_id="m-1", session_id="s-m", agent_name="personal-assistant",
                                           execution_path="claude-code-cli")
    assert taken and layer._sessions["s-m"].files_synced is True
    assert targets and targets[0].machine_id == "m-1" and targets[0].dir_relative == "users/alice/.claude"


@pytest.mark.asyncio
async def test_a_lane_reuses_a_warm_compute_session_for_the_platforms_place(remote_layer, monkeypatch):
    from services.scheduler import lanes
    monkeypatch.setattr(session_state, "_sessions", {"s-c": {"is_task": True}, "s-m": {"is_task": True}})
    monkeypatch.setattr(remote_layer, "is_session_process_dead", AsyncMock(return_value=False), raising=False)
    cfg_local = SimpleNamespace(execution_target="local")
    cfg_machine = SimpleNamespace(execution_target="m-1")
    assert await lanes._try_reuse_warm_session(remote_layer, cfg_local, "s-c", "run-1") is True
    assert await lanes._try_reuse_warm_session(remote_layer, cfg_local, "s-m", "run-1") is False
    assert await lanes._try_reuse_warm_session(remote_layer, cfg_machine, "s-m", "run-1") is True


def test_a_compute_session_agrees_with_the_pin_local():
    assert placement.runs_on("local", "c-1", platform_machine=True)
    assert not placement.runs_on("local", "m-1", platform_machine=False)
    assert not placement.runs_on("m-2", "c-1", platform_machine=True)


def test_the_busy_counter_counts_a_compute_session_once(remote_layer, monkeypatch):
    import startup_drain
    from core import concurrency
    from core.session import interactive_session
    monkeypatch.setattr(concurrency, "_sessions", {"s-local": object()})
    monkeypatch.setattr(interactive_session, "_sessions", {
        "t-c": SimpleNamespace(alive=True, target="c-1"),
        "t-m": SimpleNamespace(alive=True, target="m-1"),
        "t-dead": SimpleNamespace(alive=False, target="c-1"),
    })
    compute_ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant",
                                  is_admin_agent=False, placement=COMPUTE)
    paired_ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant",
                                 is_admin_agent=False,
                                 placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE,
                                                                           machine_id="m-1", label="Rack"))
    contexts = {"t-c": compute_ctx, "t-dead": compute_ctx, "t-m": paired_ctx}
    monkeypatch.setattr(session_state, "get_session_security", contexts.get)
    # One local slot, one alive compute record (the paired one is its
    # machine's), one compute terminal (by its own placement, whatever its
    # machine's link does).
    assert startup_drain._sessions() == 3
    remote_layer._sessions["s-c"].alive = False
    assert startup_drain._sessions() == 2
    # A task's compute record while the run still holds its slot: once.
    remote_layer._sessions["s-c"].alive = True
    monkeypatch.setattr(concurrency, "_sessions", {"s-local": object(), "s-c": object()})
    assert startup_drain._sessions() == 3


@pytest.mark.asyncio
async def test_a_task_placed_on_compute_holds_its_slot_on_the_machines_box(monkeypatch):
    """The runner takes the slot where the builder will place the run (the
    compute machine that serves the platform's place included); when the
    build lands elsewhere (the machine signed in or left between them) the
    slot moves to the box the run runs on."""
    from core import concurrency
    from core.config import task_config_builder as tcb
    from core.config.task_config_builder import TaskIdentity
    from services.scheduler import task_run
    calls: list[tuple] = []
    monkeypatch.setattr(concurrency, "release", lambda sid: calls.append(("release", sid)))

    async def _acquire(sid, kind, *, target, **kw):
        calls.append(("acquire", sid, kind, target, kw.get("blocking")))
    monkeypatch.setattr(concurrency, "acquire", _acquire)

    async def _run_db(fn, *a, **k):
        return {}
    monkeypatch.setattr("storage.pg.run_db", _run_db)
    machine = str(COMPUTE.machine_id)
    for pl, slot_target, expect in (
            (COMPUTE, "local", [("release", "s-task"), ("acquire", "s-task", "task", machine, True)]),
            (COMPUTE, machine, []),
            (placement.LOCAL_PLACEMENT, "local", [])):
        calls.clear()
        ctx = SecurityContext(role="manager", username="", agent="personal-assistant",
                              is_admin_agent=False, placement=pl)

        async def _cfg(*a, _ctx=ctx, **kw):
            return SimpleNamespace(execution_target="local", security_context=_ctx,
                                   execution_path="claude-code-cli", mcp_config_path="")
        monkeypatch.setattr(tcb, "build_task_agent_config", _cfg)
        from services.scheduler import shared
        task = shared.TaskDefinition(id="dyn-1", name="t", agent="personal-assistant", prompt="go",
                                     notification_mode="none", scope="agent")
        run = task_run.TaskRun("run-1", "s-task", task, "go", "manual", None, 1, None,
                               TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None),
                               slot_target)
        await run._build_config()
        assert calls == expect, (pl.kind, slot_target)


def test_a_config_file_instance_tool_is_delivered_on_compute():
    """The builder's word for a machine with its own copy of the files is
    the placement's ``files_synced``, so a compute session is served the
    platform's own instance file like a local one."""
    import inspect

    from services.mcp import mcp_session_config
    src = inspect.getsource(mcp_session_config)
    assert "is_remote = placement.files_synced" in src
    assert COMPUTE.files_synced is False and placement.LOCAL_PLACEMENT.files_synced is False



def test_a_compute_machines_sign_in_kicks_no_tool_check_and_no_catch_up(monkeypatch):
    """The reconnect steps a sign-in starts: a paired machine gets its tool
    check and its workspace catch-up, a compute machine the row refresh
    alone (it would refuse the other two)."""
    from core.remote import satellite_connection as sc
    from core.remote.satellite_connection import (
        SatelliteConnection,
        SatelliteConnectionManager,
    )
    cm = SatelliteConnectionManager()
    compute = SatelliteConnection(machine_id="c-1", ws=None)  # type: ignore[arg-type]
    compute.compute = True
    cm._connections["c-1"] = compute
    cm._connections["m-1"] = SatelliteConnection(machine_id="m-1", ws=None)  # type: ignore[arg-type]
    kicked: list[tuple[str, str]] = []
    monkeypatch.setattr(cm._links, "kick", lambda mid, name, fn: kicked.append((mid, name)))
    spawned: list[str] = []

    def _spawn(coro, *, name="", owner=""):
        coro.close()
        spawned.append(name)
    monkeypatch.setattr(sc, "spawn", _spawn)
    cm._kick_reconnect_steps("c-1")
    assert kicked == [("c-1", "machine_row")] and spawned == []
    cm._kick_reconnect_steps("m-1")
    assert ("m-1", "verify") in kicked and "kick_workspace_sync" in spawned


def test_a_compute_connection_is_known_as_one_while_its_sign_in_steps_run(monkeypatch):
    """The kind rides the registration: the reconnect steps read it inside
    ``register``, before the socket handler could set anything after."""
    from types import SimpleNamespace

    from core.remote import satellite_connection as sc
    from core.remote.satellite_connection import SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    seen: list[bool] = []
    monkeypatch.setattr(cm, "_kick_reconnect_steps", lambda mid: seen.append(cm.is_compute(mid)))
    monkeypatch.setattr(sc, "spawn", lambda coro, **k: coro.close())
    ws = SimpleNamespace(client=("127.0.0.1", 1), scope={"client": ("127.0.0.1", 1), "headers": []})
    asyncio.run(cm.register("c-1", ws, {"compute": True}, compute=True))  # type: ignore[arg-type]
    asyncio.run(cm.register("m-1", ws, {}))  # type: ignore[arg-type]
    assert seen == [True, False]
    assert cm.is_compute("c-1") and not cm.is_compute("m-1")


def test_a_live_compute_session_is_the_platforms_own_to_the_file_flows(remote_layer):
    """``is_remote_session`` names a paired machine's session alone: a file
    tool's call from a compute session serves the platform's copy at once
    (the pull it would send is one the compute machine refuses), and the
    file-written hook takes the local branch."""
    from core.remote import remote_file_flow
    assert remote_file_flow.is_remote_session("s-c") is False
    assert remote_file_flow.remote_machine_id("s-c") == ""
    assert remote_file_flow.is_remote_session("s-m") is True
    assert remote_file_flow.remote_machine_id("s-m") == "m-1"



# ---------------------------------------------------------------------------
# a compute machine on another host: its own copy syncs as a paired machine's
# ---------------------------------------------------------------------------

OWN = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-2", label="Rack two",
                                      own_copy=True)


def test_a_file_changed_frame_from_a_machine_with_its_own_copy_is_judged_by_its_session(monkeypatch):
    cm = _Cm(own=("c-2",))
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    frame = {"agent_slug": "personal-assistant", "path": "workspace/a.txt", "action": "modified",
             "session_id": "s-o"}
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant", is_admin_agent=False,
                          placement=OWN)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr("core.session.session_manager.live_session", lambda sid: None)
    assert satellite_file_transfer._admit_file_changed("c-2", dict(frame)) is not None
    # The machine beside the platform still writes nothing through this door,
    # and a frame naming a session bound elsewhere is dropped.
    assert satellite_file_transfer._admit_file_changed("c-1", dict(frame)) is None
    assert satellite_file_transfer._admit_file_changed("m-1", dict(frame)) is None


def test_the_fan_out_targets_a_terminal_on_a_machine_with_its_own_copy(monkeypatch):
    from core.session import interactive_session
    from services.remote import workspace_fanout
    cm = _Cm(own=("c-2",), connected=("c-1", "c-2", "m-1"))
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    monkeypatch.setattr(interactive_session, "_sessions", {
        "t-c": SimpleNamespace(agent_name="personal-assistant", alive=True, target="c-1", username="alice", role="manager"),
        "t-o": SimpleNamespace(agent_name="personal-assistant", alive=True, target="c-2", username="bob", role="viewer"),
    })
    assert workspace_fanout._interactive_remote_sessions("personal-assistant") == [
        ("t-o", "c-2", "bob", "viewer")]


@pytest.mark.asyncio
async def test_retention_asks_a_machine_with_its_own_copy_and_keeps_its_live_ids_there(monkeypatch):
    from services.infra import retention
    cm = _Cm(own=("c-2",), connected=("c-1", "c-2", "m-1"))
    layer = _layer(cm, _info("s-c"), _info("s-o", "c-2", files_synced=True, compute=True))
    monkeypatch.setattr("core.session.session_manager._remote_layer", layer)
    monkeypatch.setattr("core.session.session_manager._get_remote_layer", lambda: layer)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    all_ids, by_machine = retention._remote_live_ids()
    assert by_machine["c-2"] == {"s-o"} and "c-1" not in by_machine
    asked: list[str] = []

    async def _ask(cm_, mid, keep, dry_run):
        asked.append(mid)
        return {"machine_id": mid, "name": mid, "reached": True, "bytes": 0, "deleted": 0,
                "deleted_bytes": 0, "candidates": []}
    monkeypatch.setattr(retention, "_ask_machine_retention", _ask)

    async def _run_db(fn, *a, **k):
        return {}
    monkeypatch.setattr("storage.pg.run_db", _run_db)
    stats = {"remote_machines": [], "remote_session_files_bytes": 0, "remote_session_files_deleted": 0,
             "remote_bytes_freed": 0, "chats_flagged": 0, "remote_chats_flagged": 0}
    await retention._pass_remote_sessions(30, False, SimpleNamespace(pump_chat_ids=set()), stats, True)
    assert sorted(asked) == ["c-2", "m-1"]


@pytest.mark.parametrize("connected,dropped,expect", [
    (True, False, "machine"), (False, True, "fresh"), (False, False, "fresh"),
])
def test_the_resume_asks_the_machine_that_holds_the_chats_engine_record(monkeypatch, connected, dropped, expect):
    from core.remote import remote_resume
    from core.remote.remote_resume import ResumeVerdict
    cm = _Cm(own=("c-2",) if connected else ())
    if dropped:
        cm.dropped.add("c-2")
    layer = _layer(cm)
    monkeypatch.setattr("storage.database.get_chat_by_session",
                        lambda sid: {"id": "chat-1", "agent": "personal-assistant", "execution_path": "claude-code-cli",
                                     "execution_target": "local", "codex_thread_id": "", "compute_machine": "c-2"})
    # Its closed session's record is gone; the chat's column remembers.
    monkeypatch.setattr(session_state, "placed_on", lambda sid, chat_id="": None)
    target = remote_resume.RemoteResumeMixin._resume_target(layer, "s-gone", "", "alice")
    if expect == "machine":
        assert target[0] == "c-2"
    else:
        assert target is ResumeVerdict.FRESH


@pytest.mark.asyncio
async def test_an_adopted_session_on_a_machine_with_its_own_copy_rotates_its_login_there(monkeypatch, tmp_path):
    from core.remote import remote_resume
    from services.engines import subscription_pool, token_fanout
    from storage.billing import subscription_store
    cm = _Cm(own=("c-2",))
    layer = _layer(cm)
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(remote_resume.RemoteResumeMixin, "_adoptable", lambda self, sid, path, idle=False: True)
    monkeypatch.setattr(remote_resume.RemoteResumeMixin, "_reprovision_gateway", AsyncMock())
    monkeypatch.setattr(subscription_pool, "restore_session_binding", lambda sid: "sub-1")
    monkeypatch.setattr(subscription_store, "get_credential_data", lambda sid: {"oauth_token": {"accessToken": "at"}})
    monkeypatch.setattr(subscription_pool, "fan_out_current_token", lambda sid: None)
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant", is_admin_agent=False,
                          placement=OWN)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    targets: list = []
    monkeypatch.setattr(token_fanout, "register_session_target", lambda sid, t: targets.append(t))
    taken = await layer.adopt_idle_session(machine_id="c-2", session_id="s-o", agent_name="personal-assistant",
                                           execution_path="claude-code-cli")
    info = layer._sessions["s-o"]
    assert taken and info.files_synced is True and info.compute is True
    assert targets and targets[0].machine_id == "c-2"


def test_the_busy_counter_counts_a_session_on_a_machine_with_its_own_copy(monkeypatch):
    import startup_drain
    from core import concurrency
    from core.session import interactive_session
    cm = _Cm(own=("c-2",))
    layer = _layer(cm, _info("s-o", "c-2", files_synced=True, compute=True),
                   _info("s-m", "m-1", files_synced=True, compute=False))
    monkeypatch.setattr("core.session.session_manager._remote_layer", layer)
    monkeypatch.setattr(concurrency, "_sessions", {})
    monkeypatch.setattr(interactive_session, "_sessions", {})
    assert startup_drain._sessions() == 1


def test_a_shared_compute_machine_writes_nothing_back_even_for_its_own_session(monkeypatch):
    """The gate alone: the frame names a session whose context is bound to
    the very compute machine that sends it, which every later rule admits."""
    cm = _Cm(own=("c-2",))
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    ctx = SecurityContext(role="manager", username="alice", agent="personal-assistant", is_admin_agent=False,
                          placement=COMPUTE)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr("core.session.session_manager.live_session", lambda sid: None)
    frame = {"agent_slug": "personal-assistant", "path": "workspace/a.txt", "action": "modified",
             "session_id": "s-c"}
    assert satellite_file_transfer._admit_file_changed("c-1", dict(frame)) is None


# A compute machine with its own copy is shared by many people: each person's
# merge there leaves the other people's folders alone. The single-owner rule
# of a paired machine scrubbed them, and their owner's next merge then read the
# file as deleted on the machine and deleted it on the platform.

def _shared_merge(local: list, machine: dict, base: dict, user: str, *, role: str = "editor"):
    from core.remote.file_sync import FileEntry, diff_manifests
    entries = [FileEntry(p, h, 10, m) for p, (h, m) in local]
    remote = [{"path": p, "hash": h, "mtime": m, "size": 10} for p, (h, m) in machine.items()]
    return diff_manifests(entries, remote, base=base, clock_offset=0.0, target_username=user,
                          target_role=role, session_username=user, satellite_user=user,
                          shared_by_many=True)


def _apply(plan, platform: dict, machine: dict, base: dict) -> None:
    """What the apply does with a plan, enough for a merge sequence."""
    for a in plan.actions:
        if a.op == "push":
            machine[a.rel_path] = platform[a.rel_path]
            base[a.rel_path] = platform[a.rel_path]
        elif a.op == "pull":
            platform[a.rel_path] = machine[a.rel_path]
            base[a.rel_path] = machine[a.rel_path]
        elif a.op == "delete_platform":
            platform.pop(a.rel_path, None)
            base.pop(a.rel_path, None)
    for p in plan.to_scrub:
        machine.pop(p, None)


def _mine(platform: dict, user: str) -> list:
    """A person's manifest of the platform: their own folder and the shared ones."""
    return [(p, v) for p, v in platform.items()
            if not p.startswith("users/") or p.startswith(f"users/{user}/")]


@pytest.mark.parametrize("bob_role", ["editor", "viewer"])
def test_two_people_on_one_machine_never_lose_each_others_files(bob_role):
    platform = {"users/alice/notes.md": ("sha256:a1", 1.0), "users/bob/todo.md": ("sha256:b1", 1.0),
                "workspace/shared.md": ("sha256:w1", 1.0)}
    machine: dict = {}
    base: dict = {}
    # Alice's first session there: her folder and the shared one go over.
    plan = _shared_merge(_mine(platform, "alice"), machine, base, "alice")
    _apply(plan, platform, machine, base)
    assert "users/alice/notes.md" in machine
    # Bob's session there: his folder goes over, Alice's is left alone.
    plan = _shared_merge(_mine(platform, "bob"), machine, base, "bob", role=bob_role)
    assert not plan.to_scrub
    assert all(not a.rel_path.startswith("users/alice/") for a in plan.actions)
    _apply(plan, platform, machine, base)
    # Alice edits her note on the platform; her next merge pushes it and
    # deletes nothing.
    platform["users/alice/notes.md"] = ("sha256:a2", 2.0)
    plan = _shared_merge(_mine(platform, "alice"), machine, base, "alice")
    ops = {a.rel_path: a.op for a in plan.actions}
    assert ops == {"users/alice/notes.md": "push"} and not plan.to_scrub
    _apply(plan, platform, machine, base)
    assert set(platform) == {"users/alice/notes.md", "users/bob/todo.md", "workspace/shared.md"}
    assert machine["users/bob/todo.md"] == ("sha256:b1", 1.0)


def test_an_agent_scope_merge_on_a_shared_machine_leaves_every_persons_folder_alone():
    machine = {"users/alice/notes.md": ("sha256:a1", 1.0), "users/bob/todo.md": ("sha256:b1", 1.0),
               "workspace/shared.md": ("sha256:w1", 1.0)}
    base = dict(machine)
    # The agent's own task: its manifest holds nobody's folder.
    plan = _shared_merge([("workspace/shared.md", ("sha256:w1", 1.0))], machine, base, "")
    assert not plan.to_scrub
    assert all(not a.rel_path.startswith("users/") for a in plan.actions)


def test_a_members_merge_on_a_shared_machine_leaves_the_managers_config_there():
    # The member's manifest has no config/ and config/ is push-only for them:
    # on a machine many people share it is the manager's, merged by theirs.
    machine = {"config/context/notes.md": ("sha256:c1", 1.0)}
    plan = _shared_merge([], machine, {}, "bob", role="member")
    assert not plan.to_scrub and not plan.actions


def test_what_never_leaves_the_platform_is_still_scrubbed_from_a_shared_machine():
    machine = {"service-accounts/google.json": ("sha256:s1", 1.0),
               "users/bob/.credentials/github-tokens/t.json": ("sha256:t1", 1.0)}
    plan = _shared_merge([], machine, {}, "bob")
    assert sorted(plan.to_scrub) == ["service-accounts/google.json",
                                     "users/bob/.credentials/github-tokens/t.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("row, built, prefer, shared, recorded, stays", [
    ({"session_id": "s-1", "compute_machine": "c-2"}, "c-2", "c-2", False, "c-2", False),
    ({"session_id": "s-1", "compute_machine": "c-2", "parent_chat_id": "c-0"}, "c-2", "c-2", False, "c-2", True),
    ({"session_id": "s-1", "compute_machine": ""}, "c-1", "", True, "", False),
    ({}, "c-2", "", False, "c-2", False),
])
async def test_a_workers_rounds_stay_where_its_record_is(monkeypatch, row, built, prefer, shared, recorded, stays):
    from core.config import task_config_builder as tcb
    from core.config.task_config_builder import TaskIdentity
    from services.scheduler import shared as sched_shared, task_run
    from storage import database, remote_store
    writes: list = []
    monkeypatch.setattr(database, "get_chat", lambda cid: row)
    monkeypatch.setattr(remote_store, "set_chat_compute_machine", lambda c, m: writes.append((c, m)))

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr("storage.pg.run_db", _run_db)
    own = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id=built, label="m",
                                          own_copy=built == "c-2")
    asked: dict = {}

    async def _cfg(*a, **kw):
        asked.update(kw)
        ctx = SecurityContext(role="manager", username="", agent="personal-assistant",
                              is_admin_agent=False, placement=own)
        return SimpleNamespace(execution_target="local", security_context=ctx,
                               execution_path="claude-code-cli", mcp_config_path="")
    monkeypatch.setattr(tcb, "build_task_agent_config", _cfg)
    task = sched_shared.TaskDefinition(id="dyn-1", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent")
    run = task_run.TaskRun("run-1", "s-task", task, "go", "manual", None, 1, None,
                           TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None),
                           built)
    run.chat_id = "worker-chat"
    await run._read_chat_place()
    await run._build_config()
    assert asked["prefer"] == (prefer or built) and asked["stay_shared"] is shared
    # A lane whose record lives on another host stays there: its next round
    # resumes by id, whatever tool of its agent reaches a local address.
    assert asked["stay_on"] is stays
    assert writes == [("worker-chat", recorded)]


@pytest.mark.asyncio
async def test_a_wrapups_slot_is_taken_where_its_build_goes(monkeypatch):
    # A wrap-up runs on a machine that shares the platform's folders (its
    # builder asks for one): its slot is asked of the same machines, so the
    # run never reserves on another host and starts beside the platform.
    from core.config import task_config_builder as tcb
    from core.session import session_manager
    from services.scheduler import runner, shared as sched_shared
    seen: dict = {}

    def _placed(path, **kw):
        seen.update(kw)
        return COMPUTE
    monkeypatch.setattr(session_manager, "compute_placement_for", _placed)
    monkeypatch.setattr(tcb, "task_target", lambda task, ident: SimpleNamespace(target="local"))
    monkeypatch.setattr(runner, "_task_layer", lambda task: "claude-code-cli")
    task = sched_shared.TaskDefinition(id="wrapup-1", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent",
                                       wrapup={"chat_id": "chat-1", "identity_id": "", "home": ""})
    assert await runner._slot_target_of(task, None) == str(COMPUTE.machine_id)
    assert seen["shared_folder"] is True


@pytest.mark.asyncio
async def test_a_runs_slot_keeps_an_agent_whose_tool_reaches_a_local_address_beside(monkeypatch):
    # The rule's gate is read once, off the loop, before the wait: its answer
    # narrows the slot's machines as the build's own read narrows the build.
    import psycopg

    from core.config import task_config_builder as tcb
    from core.session import session_manager
    from services.remote import remote_status
    from services.scheduler import runner, shared as sched_shared
    seen: list = []

    def _placed(path, **kw):
        seen.append(kw)
        return COMPUTE
    monkeypatch.setattr(session_manager, "compute_placement_for", _placed)
    monkeypatch.setattr(tcb, "task_target", lambda task, ident: SimpleNamespace(target="local"))
    monkeypatch.setattr(runner, "_task_layer", lambda task: "claude-code-cli")
    asked: list = []

    def _beside(agent, machines=None):
        asked.append(agent)
        return agent == "home-agent"
    monkeypatch.setattr(remote_status, "keeps_beside", _beside)
    task = sched_shared.TaskDefinition(id="dyn-h", name="t", agent="home-agent", prompt="go",
                                       notification_mode="none", scope="agent")
    assert await runner._slot_target_of(task, None) == str(COMPUTE.machine_id)
    assert asked == ["home-agent"] and seen and all(kw["shared_folder"] is True for kw in seen)
    seen.clear()
    task = sched_shared.TaskDefinition(id="dyn-o", name="t", agent="other-agent", prompt="go",
                                       notification_mode="none", scope="agent")
    assert await runner._slot_target_of(task, None) == str(COMPUTE.machine_id)
    assert all(kw["shared_folder"] is False for kw in seen)

    def _store_down(agent, machines=None):
        raise psycopg.OperationalError("down")
    monkeypatch.setattr(remote_status, "keeps_beside", _store_down)
    assert await runner._slot_target_of(task, None) == placement.NO_COMPUTE


@pytest.mark.asyncio
async def test_a_run_waits_for_a_compute_machine_and_is_refused_when_none_comes(monkeypatch):
    """A task, a lane, a judge and a wrap-up take their slot before they
    build. With no compute machine the run waits for one, bounded: its slot
    is taken on the machine that signed in meanwhile, or refused with
    admission's own word, never taken on the platform's box. A run of a
    paired computer waits for nothing, and a store error refuses."""
    import psycopg

    from core import concurrency
    from core.config import task_config_builder as tcb
    from core.session import session_manager
    from services.scheduler import runner, shared as sched_shared
    serving = {"on": False}
    monkeypatch.setattr(session_manager, "compute_placement_for",
                        lambda path, **kw: COMPUTE if serving["on"] else None)
    where = {"target": "local"}
    monkeypatch.setattr(tcb, "task_target", lambda task, ident: SimpleNamespace(target=where["target"]))
    monkeypatch.setattr(runner, "_task_layer", lambda task: "claude-code-cli")
    waits: list = []

    async def _wait(serves, **kw):
        waits.append(serves())
        serving["on"] = arrives["in_time"]
        return serves()
    monkeypatch.setattr(concurrency, "wait_for_compute", _wait)
    task = sched_shared.TaskDefinition(id="dyn-w", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent")
    arrives = {"in_time": True}
    assert await runner._slot_target_of(task, None) == str(COMPUTE.machine_id)
    assert waits == [False]
    serving["on"], arrives["in_time"] = False, False
    assert await runner._slot_target_of(task, None) == placement.NO_COMPUTE
    assert placement.NO_COMPUTE != placement.LOCAL and waits == [False, False]
    # One already serving: no wait at all.
    serving["on"] = True
    assert await runner._slot_target_of(task, None) == str(COMPUTE.machine_id) and len(waits) == 2
    # A paired computer's run never waits for a compute machine.
    serving["on"], where["target"] = False, "m-1"
    assert await runner._slot_target_of(task, None) == "m-1" and len(waits) == 2

    def _store_down(task, ident):
        raise psycopg.OperationalError("down")
    monkeypatch.setattr(tcb, "task_target", _store_down)
    assert await runner._slot_target_of(task, None) == placement.NO_COMPUTE


@pytest.mark.asyncio
@pytest.mark.parametrize("judge", [True, False])
async def test_a_judges_slot_is_taken_where_its_build_goes(monkeypatch, judge):
    # A judge needs the platform's own folder: its slot is asked of the
    # machines that share it, as its build is.
    from core.config import task_config_builder as tcb
    from core.session import session_manager
    from services.scheduler import runner, shared as sched_shared
    seen: dict = {}

    def _placed(path, **kw):
        seen.update(kw)
        return COMPUTE
    monkeypatch.setattr(session_manager, "compute_placement_for", _placed)
    monkeypatch.setattr(tcb, "task_target", lambda task, ident: SimpleNamespace(target="local"))
    monkeypatch.setattr(runner, "_task_layer", lambda task: "claude-code-cli")
    task = sched_shared.TaskDefinition(id="dyn-1", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent",
                                       judge={"check": "c"} if judge else None)
    assert await runner._slot_target_of(task, None) == str(COMPUTE.machine_id)
    assert seen["shared_folder"] is judge


@pytest.mark.asyncio
@pytest.mark.parametrize("slot, cfg_target, pl, expect", [
    # The slot was taken on one compute machine, the build landed on another: it moves.
    ("c-2", "local", COMPUTE, [("release", "s-task"), ("acquire", "s-task", "task", "c-1", True)]),
    # No machine served when the slot was asked (it was refused), one signed
    # in before the build: the slot is taken where the build landed.
    (placement.NO_COMPUTE, "local", COMPUTE, [("release", "s-task"), ("acquire", "s-task", "task", "c-1", True)]),
    # A config with no compute placement moves nothing: no slot is ever taken
    # on the platform's own box (the real build of such a run is refused).
    ("c-2", "local", placement.LOCAL_PLACEMENT, []),
    # A paired machine's run keeps the slot its machine holds.
    ("m-1", "m-1", placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1"), []),
    # The slot was taken on the compute machine the build then names (a
    # call's wrap-up, a judge, a task): nothing moves.
    ("c-1", "local", COMPUTE, []),
])
async def test_a_slot_follows_a_build_that_lands_on_the_platform(monkeypatch, slot, cfg_target, pl, expect):
    from core import concurrency
    from core.config import task_config_builder as tcb
    from core.config.task_config_builder import TaskIdentity
    from services.scheduler import shared as sched_shared, task_run
    calls: list[tuple] = []
    monkeypatch.setattr(concurrency, "release", lambda sid: calls.append(("release", sid)))

    async def _acquire(sid, kind, *, target, **kw):
        calls.append(("acquire", sid, kind, target, kw.get("blocking")))
    monkeypatch.setattr(concurrency, "acquire", _acquire)

    async def _run_db(fn, *a, **k):
        return {}
    monkeypatch.setattr("storage.pg.run_db", _run_db)

    async def _cfg(*a, **kw):
        ctx = SecurityContext(role="manager", username="", agent="personal-assistant",
                              is_admin_agent=False, placement=pl)
        return SimpleNamespace(execution_target=cfg_target, security_context=ctx,
                               execution_path="claude-code-cli", mcp_config_path="")
    monkeypatch.setattr(tcb, "build_task_agent_config", _cfg)
    task = sched_shared.TaskDefinition(id="dyn-1", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent")
    run = task_run.TaskRun("run-1", "s-task", task, "go", "manual", None, 1, None,
                           TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None), slot)
    await run._build_config()
    assert calls == expect


@pytest.mark.asyncio
async def test_a_runs_own_session_on_its_chat_is_no_record_of_a_place(monkeypatch):
    # The round names its own session on the chat row before the build: a
    # fresh run's chat is read before that, so its run goes where the room
    # is, never kept on a shared machine by its own new id.
    from core.config import task_config_builder as tcb
    from core.config.task_config_builder import TaskIdentity
    from core.events import chat_writer
    from services.scheduler import shared as sched_shared, task_run
    from storage import database, remote_store
    row: dict = {"id": "task-run-1"}
    order: list[str] = []
    monkeypatch.setattr(database, "get_chat", lambda cid: dict(row))
    monkeypatch.setattr(remote_store, "set_chat_compute_machine", lambda c, m: None)

    async def _run_db(fn, *a, **k):
        return fn(*a, **k) if fn is database.get_chat or fn is remote_store.set_chat_compute_machine else None
    monkeypatch.setattr("storage.pg.run_db", _run_db)

    async def _submit(cid, job, label=""):
        order.append(label)
        if label == "run_start":
            row["session_id"] = "s-task"
        return 0
    monkeypatch.setattr(chat_writer, "submit", _submit)
    asked: dict = {}

    async def _cfg(*a, **kw):
        asked.update(kw)
        ctx = SecurityContext(role="manager", username="", agent="personal-assistant",
                              is_admin_agent=False, placement=COMPUTE)
        return SimpleNamespace(execution_target="local", security_context=ctx,
                               execution_path="claude-code-cli", mcp_config_path="")
    monkeypatch.setattr(tcb, "build_task_agent_config", _cfg)
    task = sched_shared.TaskDefinition(id="dyn-1", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent")
    run = task_run.TaskRun("run-1", "s-task", task, "go", "manual", None, 1, None,
                           TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None),
                           str(COMPUTE.machine_id))
    run.chat_id = "task-run-1"
    await run._open_round()
    assert "run_start" in order and row["session_id"] == "s-task"
    await run._build_config()
    assert asked["stay_shared"] is False


def _bare_run(chat_id: str = "worker-chat"):
    from core.config.task_config_builder import TaskIdentity
    from services.scheduler import shared as sched_shared, task_run
    task = sched_shared.TaskDefinition(id="dyn-1", name="t", agent="personal-assistant", prompt="go",
                                       notification_mode="none", scope="agent")
    run = task_run.TaskRun("run-1", "s-task", task, "go", "manual", None, 1, None,
                           TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None), "local")
    run.chat_id = chat_id
    return run


@pytest.mark.asyncio
async def test_a_runs_next_round_builds_with_the_place_round_one_left(monkeypatch):
    # Round one recorded the machine with its own copy it ran on; the account
    # switch's round two resumes there, not where the balancer would send a
    # chat with no record.
    from services.remote import chat_reload
    from storage import database
    row = {"id": "worker-chat", "session_id": "s-task", "compute_machine": "c-2", "execution_target": "local"}
    monkeypatch.setattr(database, "get_chat", lambda cid: dict(row))

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr("storage.pg.run_db", _run_db)

    async def _note(*a, **k):
        return None
    monkeypatch.setattr(chat_reload, "note_round", _note)
    monkeypatch.setattr(chat_reload, "resume_nudge", lambda reason, said: "go on")
    run = _bare_run()
    from services.remote import remote_status
    run.chat_place = remote_status.ChatPlace("", False)   # what round one read
    seen: list = []

    async def _drive():
        seen.append(run.chat_place)
    monkeypatch.setattr(run, "_spawn_and_drive", _drive)
    await run._next_round("account", [])
    assert seen == [remote_status.ChatPlace("c-2", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("held_on", ["c-2", ""])
async def test_a_reused_warm_session_records_where_it_runs(monkeypatch, held_on):
    # The build may have named another machine (the recorded one's tunnel
    # read down): the turn rides the warm session, so the chat records where
    # that one runs.
    from core.config import config_builder
    from core.events import stream_pump
    from core.session import session_manager
    from services.scheduler import lanes
    from storage import remote_store
    monkeypatch.setattr(session_manager, "own_copy_machine_of", lambda sid: held_on if sid == "s-task" else "")
    written: list = []
    monkeypatch.setattr(remote_store, "set_chat_compute_machine", lambda c, m: written.append((c, m)))

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr("storage.pg.run_db", _run_db)

    async def _reuse(*a, **k):
        return True
    monkeypatch.setattr(lanes, "_try_reuse_warm_session", _reuse)
    monkeypatch.setattr(config_builder, "release_config_seat", lambda *a, **k: None)
    monkeypatch.setattr(stream_pump, "_active_pumps", {})
    run = _bare_run()
    run.agent_cfg = SimpleNamespace(interactive=False)
    run.layer = object()
    await run._continue_round()
    assert run.reused_warm and written == [("worker-chat", held_on)]
