"""One decision of where a session of the platform's place runs, read once
by the builders and carried in the config's placement, and one refusal when
no compute machine can take it.

``remote_status.serving_compute_machine`` answers the machine or None;
``remote_status.compute_row_for`` is the builders' reader and raises
``placement.NoComputeMachine`` for the platform's place with none;
``remote_store.placement_of`` turns a ``local`` target into the serving
machine's compute placement; ``get_execution_layer`` answers the remote
layer for a built compute placement, never asks which machine serves, and
has no layer for a process engine with none; ``layer_for_chat`` asks the
holder of a live session before anything for the platform's place;
admission leaves a compute session to its machine.
"""

from __future__ import annotations

import asyncio

import pytest

from core import placement
from core.remote.remote_execution import RemoteExecutionLayer
from core.session import session_manager
from services.remote import remote_status
from storage import remote_store

_COMPUTE = {"id": "c-1", "name": "Compute one", "pairing_scope": "compute", "registered_by": "",
            "capabilities": "{}", "allow_full_fs": False, "device_grants": "[]"}


class _Cm:
    """The connection manager as the switch reads it: the connected compute
    machines (admitted by their rows at sign-in), their names, their links."""

    def __init__(self, connected=("c-1",), tunnel_ok=True, compute=("c-1",), homes=()):
        self._connected = set(connected)
        self._tunnel_ok = tunnel_ok
        self._compute = set(compute)
        self._homes = set(homes)

    def is_connected(self, mid):
        return mid in self._connected

    def tunnel_ok(self, mid):
        return self._tunnel_ok

    def exit_state(self, mid):
        return 0, False

    def is_compute(self, mid):
        return mid in self._compute and mid in self._connected

    def compute_machine_ids(self):
        return frozenset(self._compute & self._connected)

    def satellite_name(self, mid):
        return "Compute one" if mid == "c-1" else mid[:8]

    def satellite_version(self, mid):
        return "0.5.173" if mid in self._connected else ""

    def shares_folders(self, mid):
        return self.is_compute(mid)

    def owns_copy(self, mid):
        return False

    def compute_address(self, mid):
        return ""

    def satellite_session_home_dirs(self, mid):
        return frozenset({".claude", ".codex"}) if mid in self._homes else frozenset()

    def set_line_listener(self, listener):
        self.line_listener = listener


def _on(monkeypatch, *, connected=("c-1",), tunnel_ok=True, compute=("c-1",)):
    cm = _Cm(connected, tunnel_ok, compute)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: dict(_COMPUTE) if mid == "c-1" else None)
    # The process's one remote layer, for this test alone and on its fake: a
    # layer made here would otherwise outlive the test, bound to the fake.
    monkeypatch.setattr(session_manager, "_remote_layer", RemoteExecutionLayer(cm))
    return cm


# ---------------------------------------------------------------------------
# the serving machine
# ---------------------------------------------------------------------------

def test_no_key_decides_where_sessions_run(monkeypatch):
    """The setting that chose between a compute machine and the platform's
    own sandbox is gone with that sandbox: nothing reads it, and its word
    in an install's config changes no answer."""
    import config as app_config
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_SESSIONS", "local", raising=False)
    _on(monkeypatch)
    assert remote_status.serving_compute_machine("claude-code-cli")["id"] == "c-1"
    assert not hasattr(remote_status, "compute_sessions_on")
    assert not hasattr(app_config, "COMPUTE_SESSION_PLACES")


def test_the_decision_reads_no_store(monkeypatch):
    """The serving machine comes from the connections (a chat's layer asks on
    the event loop): the row's id and name as the sign-in registered them."""
    _on(monkeypatch)
    monkeypatch.setattr(remote_store, "list_compute_machines",
                        lambda: (_ for _ in ()).throw(AssertionError("the store was read")))
    row = remote_status.serving_compute_machine("claude-code-cli")
    assert row == {"id": "c-1", "name": "Compute one", "satellite_version": "0.5.173", "compute_address": "",
                   "pairing_scope": placement.PAIRING_COMPUTE, "capabilities": {"session_home_dirs": []}}
    built = placement.from_machine(placement.KIND_COMPUTE, row)
    # The build reads the version for the tools a satellite must be new enough for.
    assert built.label == "Compute one" and built.satellite_version == "0.5.173"
    assert built.session_homes == frozenset()


def test_a_compute_machine_that_makes_homes_says_so_on_the_row_the_builders_read(monkeypatch):
    """The builders take a compute session's placement from this row, never
    the store: the machine's live report of ``session_home_dirs`` rides it."""
    cm = _on(monkeypatch)
    cm._homes = {"c-1"}
    row = remote_status.serving_compute_machine("claude-code-cli")
    assert placement.from_machine(placement.KIND_COMPUTE, row).session_homes == frozenset({".claude", ".codex"})


def test_a_connected_compute_machine_with_its_link_up_serves(monkeypatch):
    _on(monkeypatch)
    assert remote_status.serving_compute_machine("claude-code-cli")["id"] == "c-1"
    assert remote_status.serving_compute_machine("")["id"] == "c-1"


def test_a_machine_away_or_with_its_link_down_does_not_serve(monkeypatch):
    _on(monkeypatch, connected=())
    assert remote_status.serving_compute_machine("claude-code-cli") is None
    _on(monkeypatch, tunnel_ok=False)
    assert remote_status.serving_compute_machine("claude-code-cli") is None


def test_direct_llm_gets_the_machine_for_its_tool_processes(monkeypatch):
    """Its loop stays in the proxy (``get_execution_layer`` below) and its
    stdio tools go to the compute machine, so the builders place it there."""
    _on(monkeypatch)
    assert remote_status.serving_compute_machine("direct-llm")["id"] == "c-1"


def test_a_call_a_meeting_and_a_wrapup_ask_the_one_decision_on_every_engine(monkeypatch):
    """Their builders read ``serving_compute_machine`` with the platform's
    own folder asked for: the session of a Claude Code or Codex one, the
    tool processes of a Direct LLM one. No second reader decides for them."""
    _on(monkeypatch)
    for engine in ("claude-code-cli", "codex-cli", "direct-llm"):
        assert remote_status.serving_compute_machine(engine, shared_folder=True)["id"] == "c-1"
    assert not hasattr(remote_status, "serving_tools_machine")


@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli", "direct-llm"])
def test_with_none_connected_the_builders_reader_refuses_the_platforms_place(monkeypatch, engine):
    """The platform runs no session itself: with no compute machine signed
    in, or none whose link answers, a session of the platform's place is
    refused with the one sentence, on every engine (Direct LLM's tools are
    part of its session). A session on a paired computer asks for none."""
    for away in ({"connected": ()}, {"tunnel_ok": False}):
        _on(monkeypatch, **away)
        assert remote_status.serving_compute_machine(engine, shared_folder=True) is None
        for target in ("local", "", None):
            with pytest.raises(placement.NoComputeMachine) as refused:
                remote_status.compute_row_for(target, engine)
            assert str(refused.value) == placement.NO_COMPUTE_SENTENCE
            assert isinstance(refused.value, placement.PlacementRefused)
        assert remote_status.compute_row_for("m-paired", engine) is None
        assert remote_status.compute_row_for(placement.offline_sentinel("m-gone"), engine) is None
    _on(monkeypatch)
    assert remote_status.compute_row_for("local", engine, shared_folder=True)["id"] == "c-1"


def test_a_built_compute_placement_answers_the_remote_layer_and_none_has_no_layer(monkeypatch):
    built = placement.from_machine(placement.KIND_COMPUTE, dict(_COMPUTE))
    layer = session_manager.get_execution_layer("agent-a", execution_path="claude-code-cli",
                                                execution_target="local", placement_built=built)
    assert isinstance(layer, RemoteExecutionLayer)
    # The platform starts no session of a process engine itself: with a
    # local placement, or none, there is no layer to answer.
    for no_machine in ({"placement_built": placement.LOCAL_PLACEMENT}, {}):
        for engine in ("claude-code-cli", "codex-cli"):
            with pytest.raises(placement.NoComputeMachine):
                session_manager.get_execution_layer("agent-a", execution_path=engine,
                                                    execution_target="local", **no_machine)
    # Direct LLM's loop stays in the proxy whatever placement a caller passes.
    for placed in ({"placement_built": built}, {}):
        direct = session_manager.get_execution_layer("agent-a", execution_path="direct-llm",
                                                     execution_target="local", **placed)
        assert not isinstance(direct, RemoteExecutionLayer)
    # An engine nothing registers is still its own error, never the refusal.
    with pytest.raises(session_manager.UnknownExecutionPath):
        session_manager.get_execution_layer("agent-a", execution_path="no-such-engine",
                                            execution_target="local")


def test_the_layer_never_asks_which_machine_serves(monkeypatch):
    """With a machine serving, a local target with no built placement still
    has no layer: the builders decide once, and a caller passes what they
    built."""
    _on(monkeypatch)
    monkeypatch.setattr(remote_status, "serving_compute_machine",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the layer asked")))
    with pytest.raises(placement.NoComputeMachine):
        session_manager.get_execution_layer("agent-a", execution_path="claude-code-cli",
                                            execution_target="local")


def test_a_chats_local_pin_asks_the_holder_of_its_live_session_first(monkeypatch):
    _on(monkeypatch)
    held = object()

    class _Holder:
        pass

    holder = _Holder()
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(holder) if sid == "s-1" else None)  # type: ignore[arg-type]
    chat = {"session_id": "s-1", "execution_target": "local", "execution_path": "claude-code-cli"}
    assert session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager") is holder
    del held


def test_a_compute_machine_that_is_reconnecting_keeps_its_chats_layer(monkeypatch):
    """A compute machine in its reconnect grace is not linked, so the
    connection no longer says it is one: the session's own record does. Its
    chat keeps the holder's layer (the turn waits for the link there), and a
    paired computer's holder in its grace never takes a ``local`` pin."""
    from types import SimpleNamespace

    from core.session import session_state
    _on(monkeypatch, connected=())
    holder = object()
    held = session_manager.LiveSession(holder, "c-1", linked=False, grace=True)  # type: ignore[arg-type]
    monkeypatch.setattr(session_manager, "live_session", lambda sid: held)
    chat = {"session_id": "s-1", "execution_target": "local", "execution_path": "claude-code-cli"}
    placed_on = {"c-1": placement.from_machine(placement.KIND_COMPUTE, dict(_COMPUTE)),
                 "m-1": placement.from_machine(placement.KIND_ADMIN_REMOTE, {"id": "m-1", "pairing_scope": "admin"})}
    where = {"machine": "c-1"}
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda sid: SimpleNamespace(placement=placed_on[where["machine"]]))
    assert session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager") is holder
    # Its grace over and no machine back: nothing holds it, nothing serves.
    # The chat still reads as the remote layer's, where its session is not
    # alive, never as the holder's.
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(holder, "c-1", linked=False, grace=False))  # type: ignore[arg-type]
    layer = session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager")
    assert layer is not holder and isinstance(layer, RemoteExecutionLayer)
    # A paired computer in its grace holding a session of a chat pinned local.
    where["machine"] = "m-1"
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(holder, "m-1", linked=False, grace=True))  # type: ignore[arg-type]
    layer = session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager")
    assert layer is not holder and isinstance(layer, RemoteExecutionLayer)


def test_a_paired_machines_holder_never_takes_a_local_pin(monkeypatch):
    """The holder is taken only when the proxy or a compute machine holds
    the session: one a paired machine holds leaves the pin to decide (the
    builders' answer, here the serving compute machine)."""
    _on(monkeypatch)
    paired = object()
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(paired, "m-1"))  # type: ignore[arg-type]
    chat = {"session_id": "s-1", "execution_target": "local", "execution_path": "claude-code-cli"}
    layer = session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager")
    assert layer is not paired and isinstance(layer, RemoteExecutionLayer)
    compute_holder = object()
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(compute_holder, "c-1"))  # type: ignore[arg-type]
    assert session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager") is compute_holder


def test_a_chats_local_pin_with_no_live_session_builds_as_the_builders_would(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: None)
    chat = {"session_id": "s-gone", "execution_target": "local", "execution_path": "claude-code-cli"}
    assert isinstance(session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager"),
                      RemoteExecutionLayer)
    # With none connected the chat is still opened and probed: it reads as
    # the remote layer's, where nothing of it is alive. A start is what is
    # refused, by the builders and by the layer's own resolver.
    _on(monkeypatch, connected=())
    layer = session_manager.layer_for_chat("agent-a", chat, user_sub="sub-a", role="manager")
    assert isinstance(layer, RemoteExecutionLayer)
    assert session_manager.no_compute_for({**chat, "agent": "agent-a"}) is True
    with pytest.raises(placement.NoComputeMachine):
        session_manager.get_execution_layer("agent-a", execution_path="claude-code-cli",
                                            user_sub="sub-a", role="manager", execution_target="local")
    # A chat on a paired computer answers for itself, and so does one whose
    # session is live.
    assert session_manager.no_compute_for({**chat, "agent": "agent-a", "execution_target": "m-1"}) is False
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(object(), "c-1"))  # type: ignore[arg-type]
    assert session_manager.no_compute_for({**chat, "agent": "agent-a"}) is False


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------

def test_admission_judges_a_compute_session_by_its_machine():
    built = placement.from_machine(placement.KIND_COMPUTE, dict(_COMPUTE))
    assert placement.admission_target("local", built) == "c-1"
    assert placement.admission_target("m-1", built) == "c-1"
    # The platform's place with no compute placement: admission's own word,
    # which no box and no paired computer answers to.
    assert placement.admission_target("local", placement.LOCAL_PLACEMENT) == placement.NO_COMPUTE
    assert placement.admission_target("", None) == placement.NO_COMPUTE
    assert placement.admission_target("m-1", None) == "m-1"


def test_a_registered_compute_session_reserves_on_its_machines_box(monkeypatch):
    """A resume passes the chat's ``local`` pin: the session's own placement
    names the box it reserves on while that machine is connected."""
    from types import SimpleNamespace

    from core import concurrency, concurrency_scan
    from core.remote import satellite_connection
    from core.session import session_state
    built = placement.from_machine(placement.KIND_COMPUTE, dict(_COMPUTE))

    class _Ctx:
        placement = built

    monkeypatch.setattr(session_state, "get_session_security", lambda sid: _Ctx() if sid == "s-c" else None)
    monkeypatch.setattr(satellite_connection, "get_connection_manager",
                        lambda: SimpleNamespace(is_compute=lambda m: m == "c-1", machine_memory_report=lambda m, compute=False: None,
                                                owns_copy=lambda m: False))
    reading = concurrency_scan.Reading(16000, 12000, False, True)
    monkeypatch.setattr(concurrency, "compute_reading", lambda _box: reading)
    concurrency.init()
    try:
        adm = asyncio.run(concurrency.acquire("s-c", "chat", target="local"))
        assert adm and concurrency._session_box.get("s-c") == "c-1"
    finally:
        concurrency.release("s-c")


# ---------------------------------------------------------------------------
# the remote start reads the machine from the config
# ---------------------------------------------------------------------------

def test_the_machine_of_a_config_is_its_compute_placements():
    from core.execution_layer import AgentConfig
    from core.remote.remote_session_start import compute_placement_of, machine_of_config

    class _Ctx:
        placement = placement.from_machine(placement.KIND_COMPUTE, dict(_COMPUTE))

    cfg = AgentConfig("agent-a", execution_target="local", security_context=_Ctx())  # type: ignore[arg-type]
    assert compute_placement_of(cfg) is not None and machine_of_config(cfg) == "c-1"

    class _Local:
        placement = placement.LOCAL_PLACEMENT

    cfg = AgentConfig("agent-a", execution_target="m-1", security_context=_Local())  # type: ignore[arg-type]
    assert compute_placement_of(cfg) is None and machine_of_config(cfg) == "m-1"


# ---------------------------------------------------------------------------
# A wake: the machine its delivery took the slot on is the one its build lands on
# ---------------------------------------------------------------------------

def test_a_wakes_build_prefers_the_machine_its_slot_was_taken_on(monkeypatch):
    """The delivery picks the compute machine once and takes the slot there.
    The chat's builder is told that machine, so with two machines connected
    the build never lands on the other one. Without it the chat's own place
    decides, as for a warm-up."""
    import asyncio

    from core.config import config_builder
    from services.scheduler import wake_session
    from storage import database as task_store
    asked: list = []

    def _row_for(target, execution_path="", *, prefer="", shared_folder=False, agent=""):
        asked.append((target, execution_path, prefer, shared_folder))
        return {"id": prefer or "c-1", "pairing_scope": "compute"}
    monkeypatch.setattr(remote_status, "compute_row_for", _row_for)
    monkeypatch.setattr(remote_status, "any_own_copy", lambda: False)

    async def _rows():
        taken = await config_builder._compute_row("local", "claude-code-cli", "chat-1", "manager", False,
                                                  prefer="c-2")
        plain = await config_builder._compute_row("local", "claude-code-cli", "chat-1", "manager", False)
        paired = await config_builder._compute_row("m-1", "claude-code-cli", "chat-1", "manager", False,
                                                   prefer="c-2")
        return taken, plain, paired
    taken, plain, paired = asyncio.run(_rows())
    assert taken["id"] == "c-2" and plain["id"] == "c-1" and paired is None
    assert asked == [("local", "claude-code-cli", "c-2", False), ("local", "claude-code-cli", "", False)]

    built: list = []

    async def _build(**kw):
        built.append(kw)
        return "cfg"
    monkeypatch.setattr(config_builder, "build_agent_config", _build)
    monkeypatch.setattr(task_store, "get_user", lambda sub: {"sub": sub, "username": "alice"})

    async def _configs():
        common = dict(user_sub="sub-a", role="editor", chat_id="chat-1", chat_row={"id": "chat-1"},
                      exec_path="claude-code-cli", target="local", fresh=False,
                      permission_mode="autoReview", client_type="dashboard")
        await wake_session._chat_built_config("s-1", "agent-a", **common, compute={"id": "c-2"})
        await wake_session._chat_built_config("s-1", "agent-a", **common)
    asyncio.run(_configs())
    assert [kw["compute_prefer"] for kw in built] == ["c-2", ""]
    assert all(kw["pinned_target"] == "local" for kw in built)


# ---------------------------------------------------------------------------
# Every door that places a session names its agent
# ---------------------------------------------------------------------------

#: The three readers of where a session of the platform's place runs.
_PLACERS = frozenset({"serving_compute_machine", "compute_row_for", "compute_placement_for"})

#: The calls that name no agent and narrow by nothing, each with its reason.
_NO_AGENT = {
    ("core/session/session_manager.py", "layer_for_chat"):
        "the layer is the remote layer on every compute machine, and it runs on the loop",
    ("ws/dashboard_warmup_tail.py", "_built_where_it_starts"):
        "a pre-warm is built by the chat builder, which names the agent; its machine opens no tool address",
    ("services/scheduler/runner.py", "_serves"):
        "the run's slot reads remote_status.keeps_beside once off the loop and passes it as shared_folder",
    ("services/scheduler/runner.py", "_slot_target_of"):
        "the same: shared holds the gate's answer",
    ("core/concurrency.py", "prewarm_allowed"):
        "a pre-warm site reads remote_status.keeps_beside in its own thread job and passes shared_folder",
}


def _placing_calls():
    """``(path, function, call)`` for every reference in the proxy's code to
    one of ``_PLACERS``, by the call that runs it: a direct call, or the
    call that takes it as an argument (``functools.partial``,
    ``asyncio.to_thread``)."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", "venv/")) or "/tests/" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""
            if name not in _PLACERS or not isinstance(getattr(node, "ctx", None), ast.Load):
                continue
            call = parents.get(node)
            if not isinstance(call, ast.Call):
                out.append((rel, "", None))
                continue
            func = parents.get(call)
            while func is not None and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = parents.get(func)
            out.append((rel, func.name if func is not None else "", call))
    return out


def test_every_door_that_places_a_session_names_its_agent():
    """A tool's local address keeps its agent's sessions beside the platform
    (``remote_status.keeps_beside``, asked by ``serving_compute_machine``):
    a door that places a session without naming its agent would send such a
    session to another host. Every call names ``agent=``, passes
    ``shared_folder=True``, or is listed with its reason."""
    import ast
    missing = []
    seen_allowed = set()
    for rel, func, call in _placing_calls():
        if call is None:
            missing.append((rel, func, "a reference outside a call"))
            continue
        kws = {k.arg: k.value for k in call.keywords}
        shared = kws.get("shared_folder")
        # An owner's pass-through hands on its caller's agent as ``**{"agent": ...}``.
        handed = any(k.arg is None and "'agent'" in ast.unparse(k.value) for k in call.keywords)
        if "agent" in kws or handed or (isinstance(shared, ast.Constant) and shared.value is True):
            continue
        if (rel, func) in _NO_AGENT:
            seen_allowed.add((rel, func))
            continue
        missing.append((rel, func, ast.unparse(call)[:120]))
    assert not missing, missing
    assert seen_allowed == set(_NO_AGENT), set(_NO_AGENT) - seen_allowed


def test_the_prewarm_sites_pass_the_gates_answer():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    calls = []
    for rel in ("ws/dashboard_warmup.py", "ws/dashboard_prewarm.py"):
        for node in ast.walk(ast.parse((root / rel).read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "prewarm_allowed"):
                calls.append((rel, {k.arg for k in node.keywords}))
    assert calls and all("shared_folder" in kws for _rel, kws in calls), calls
