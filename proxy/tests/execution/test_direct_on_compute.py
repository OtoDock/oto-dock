"""Direct LLM on a compute machine: the proxy keeps its loop, the machine runs
its stdio tools.

The session's record names the machine (its tools' callbacks come through its
link), the direct layer reads its tool machine from that record, and every
reader of "where a session is" answers for a session whose only presence on
the machine is its tools: nothing there to wait for or take back. With no
machine or under ``local``, nothing changes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import placement
from core.execution_layer import AgentConfig
from core.layers.direct import layer as direct_layer
from core.layers.direct.session import DirectSession

COMPUTE = placement.from_machine(placement.KIND_COMPUTE, {"id": "compute-1", "name": "Compute", "pairing_scope": "compute"})


def _ctx(pl=None):
    return SimpleNamespace(placement=pl or placement.LOCAL_PLACEMENT, role="editor", mount_username="alice",
                           is_admin_agent=False, config_visible=None, mount_shared=True, knowledge_rw=False,
                           external=False, read_only=False, outside_files_write=False, outside_knowledge=(),
                           wrapup="", session_scope="user")


def _start_world(monkeypatch, tmp_path):
    from core.credentials import credential_exit
    from core.sandbox import sandbox as sb
    from services.mcp import mcp_registry
    seen: dict = {}
    attached: list = []

    async def fake_create(session_id, **kw):
        seen.update(kw)
        return SimpleNamespace(session_id=session_id, model="m", effort="", user_sub="", sandbox_cfg=None)

    monkeypatch.setattr(direct_layer, "create_direct_session", fake_create)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
    monkeypatch.setattr(sb, "resolve_sandbox_config", lambda **k: SimpleNamespace())
    monkeypatch.setattr(sb, "SandboxBuilder", lambda cfg: SimpleNamespace(
        cfg=cfg, get_env_overrides=dict))
    monkeypatch.setattr(direct_layer, "_record_session_use", lambda *a, **k: None)
    async def attach(*a, **k):
        attached.append((a, k))
    monkeypatch.setattr(credential_exit, "attach_sandbox", attach)
    return seen, attached


@pytest.mark.asyncio
async def test_the_layer_reads_its_tool_machine_from_the_sessions_record(monkeypatch, tmp_path):
    seen, _attached = _start_world(monkeypatch, tmp_path)
    await direct_layer.DirectLLMExecutionLayer()._start_session_impl("s-c", AgentConfig(
        agent_name="ag", sandbox_host_claude_dir=str(tmp_path), security_context=_ctx(COMPUTE)))
    assert seen["machine_id"] == "compute-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", [None, _ctx()])
async def test_on_this_host_there_is_no_machine(monkeypatch, tmp_path, ctx):
    seen, _attached = _start_world(monkeypatch, tmp_path)
    await direct_layer.DirectLLMExecutionLayer()._start_session_impl("s-l", AgentConfig(
        agent_name="ag", sandbox_host_claude_dir=str(tmp_path), security_context=ctx))
    assert seen["machine_id"] == ""


@pytest.mark.asyncio
async def test_the_credential_exit_is_attached_here_and_with_the_compute_machine(monkeypatch, tmp_path):
    """The exit is attached for a session on this host and for one whose
    tools run on the compute machine, which is told so: that machine's own
    exit swaps the placeholders the build minted (the attach decides from
    the machine what it does)."""
    _seen, attached = _start_world(monkeypatch, tmp_path)
    layer = direct_layer.DirectLLMExecutionLayer()
    await layer._start_session_impl("s-local", AgentConfig(
        agent_name="ag", sandbox_host_claude_dir=str(tmp_path), security_context=_ctx()))
    assert len(attached) == 1 and attached[0][1]["machine"] is None
    await layer._start_session_impl("s-compute", AgentConfig(
        agent_name="ag", sandbox_host_claude_dir=str(tmp_path), security_context=_ctx(COMPUTE)))
    assert len(attached) == 2 and attached[1][1]["machine"].machine_id == "compute-1"


# -- a session whose tools were lost ------------------------------------------


def _pooled(monkeypatch, *, lost=True, running=False):
    from core.layers.direct import session as sess
    closed: list = []
    monkeypatch.setattr("core.concurrency.release_chat_slot", lambda sid: closed.append(("slot", sid)))
    session = DirectSession.__new__(DirectSession)
    session.mcp_manager = SimpleNamespace(tools_lost=lambda: lost)
    monkeypatch.setitem(sess._direct_sessions, "s-lost", session)
    layer = direct_layer.DirectLLMExecutionLayer()
    monkeypatch.setattr(direct_layer.DirectLLMExecutionLayer, "_active_streams",
                        {"s-lost": object()} if running else {})

    async def close(sid):
        closed.append(sid)
    monkeypatch.setattr(direct_layer, "close_direct_session", close)
    monkeypatch.setattr(direct_layer, "cleanup_session_permission_state", lambda _sid: None)
    monkeypatch.setattr("services.engines.subscription_pool.release_subscription", lambda _sid: None)
    return layer, closed


@pytest.mark.asyncio
async def test_a_session_whose_tools_were_lost_reads_dead_and_is_closed_at_its_resume(monkeypatch):
    layer, closed = _pooled(monkeypatch)
    assert await layer.is_session_alive("s-lost") is False
    assert await layer.is_session_process_dead("s-lost") is True
    await layer.prepare_resume("s-lost")
    # The session goes; its slot stays the caller's, as on the other engines:
    # a call healed in place keeps the one it holds.
    assert closed == ["s-lost"]
    await layer.close_session("s-lost")
    assert closed == ["s-lost", "s-lost", ("slot", "s-lost")]


@pytest.mark.asyncio
async def test_a_running_turn_finishes_before_the_loss_counts(monkeypatch):
    layer, closed = _pooled(monkeypatch, running=True)
    assert await layer.is_session_alive("s-lost") is True
    assert await layer.is_session_process_dead("s-lost") is False
    await layer.prepare_resume("s-lost")
    assert closed == []


@pytest.mark.asyncio
async def test_a_session_with_its_tools_is_alive(monkeypatch):
    layer, closed = _pooled(monkeypatch, lost=False)
    assert await layer.is_session_alive("s-lost") is True
    await layer.prepare_resume("s-lost")
    assert closed == []


@pytest.mark.asyncio
async def test_the_reaper_closes_a_session_whose_tools_were_lost_once_no_turn_holds_it(monkeypatch):
    import asyncio
    import time

    from core import concurrency
    from core.layers.direct import session as sess
    from core.session import session_state
    reaped: list = []

    async def reap(sid, _close):
        reaped.append(sid)

    async def idle():
        return 100000

    def session_of(lost):
        s = DirectSession.__new__(DirectSession)
        s.last_activity = time.monotonic()
        s.lock = asyncio.Lock()
        s.mcp_manager = SimpleNamespace(tools_lost=lambda: lost)
        return s

    busy = session_of(True)
    monkeypatch.setattr(sess, "_direct_sessions", {"s-gone": session_of(True), "s-fine": session_of(False), "s-busy": busy})
    monkeypatch.setattr(concurrency, "reap_session", reap)
    monkeypatch.setattr(session_state, "cached_idle_timeout", idle)
    monkeypatch.setattr(sess, "has_pending_prompt", lambda _s: False)
    monkeypatch.setattr(sess, "mark_closing", lambda _s: None)

    kept: list = []

    async def no_reap(timeout=0, *, keep=frozenset()):
        kept.append(keep)
    monkeypatch.setattr(sess.mcp_pool, "reap_idle", no_reap)
    async with busy.lock:
        await sess._reap_idle_direct_pass()
    assert reaped == ["s-gone"]
    # The pool's own reaper is told which sessions live: their managers stay.
    assert kept == [frozenset({"s-fine", "s-busy"})]


# -- nothing of the session is on the machine ----------------------------------


def test_a_direct_chat_runs_no_process(monkeypatch):
    from services.scheduler import run_recovery
    assert run_recovery.chat_runs_no_process({"agent": "a", "execution_path": "direct-llm"}) is True
    assert run_recovery.chat_runs_no_process({"agent": "a", "execution_path": "claude-code-cli"}) is False


def test_the_boots_hold_waits_for_no_direct_session(monkeypatch):
    from services.remote import compute_bootstrap as cb
    from storage import database as task_store
    chats = {"s-direct": {"agent": "a", "execution_path": "direct-llm"},
             "s-claude": {"agent": "a", "execution_path": "claude-code-cli"}}
    monkeypatch.setattr(task_store, "get_chat_by_session", chats.get)
    assert cb._may_come_back("s-direct") is False
    assert cb._may_come_back("s-claude") is True
    # A participant, a wrap-up, a button's or a pre-warm's session: no chat row
    # for the hold to adopt it into.
    assert cb._may_come_back("s-with-no-chat") is False


def test_a_cut_direct_turn_is_not_parked_on_its_tools_machine(monkeypatch):
    """Nothing of a Direct turn is on the machine: it takes the path of a
    turn on the platform and is continued, never left waiting."""
    from services.scheduler import cut_turns
    from storage import database as task_store
    from storage.chat import cut_records as records, open_turns

    monkeypatch.setattr(records, "tidy_at_boot", lambda: None)
    monkeypatch.setattr(records, "in_state", lambda _s: [])
    monkeypatch.setattr(records, "supersede_older", lambda: False)
    rows = [{"chat_id": "c-direct", "turn_token": "t1", "session_id": "s-d", "engine_open": True,
             "run_id": "", "machine_id": "compute-1"},
            {"chat_id": "c-claude", "turn_token": "t2", "session_id": "s-c", "engine_open": True,
             "run_id": "", "machine_id": "compute-1"}]
    monkeypatch.setattr(open_turns, "list_open", lambda: rows)
    chats = {"c-direct": {"agent": "a", "execution_path": "direct-llm"},
             "c-claude": {"agent": "a", "execution_path": "claude-code-cli"}}
    monkeypatch.setattr(task_store, "get_chat", chats.get)
    moved: list = []
    waited: list = []
    monkeypatch.setattr(records, "move_open", lambda chat_id, token, **kw: moved.append(chat_id) or True)

    def copy_open(row, **kw):
        waited.append(row["chat_id"])
        return {"id": 1, "chat_id": row["chat_id"], "session_id": row["session_id"], "machine_id": row["machine_id"]}

    monkeypatch.setattr(records, "copy_open", copy_open)
    monkeypatch.setattr(cut_turns, "_park", lambda *_a: None)
    counts = cut_turns.record_at_boot(clean_stop=False, catches_up=True)
    assert moved == ["c-direct"] and waited == ["c-claude"]
    assert counts["pending"] == 1 and counts["waiting"] == 1


def test_a_direct_turn_is_not_open_work_on_the_machine(monkeypatch):
    import startup_drain
    from core import concurrency_scan
    from core.events import stream_pump
    from core.session import interactive_session, session_manager
    monkeypatch.setattr(stream_pump, "_active_pumps", {
        "c1": SimpleNamespace(is_done=False, session_id="s-direct"),
        "c2": SimpleNamespace(is_done=False, session_id="s-claude"),
    })
    monkeypatch.setattr(interactive_session, "_sessions", {})
    monkeypatch.setattr(concurrency_scan, "placed_on_compute", lambda _sid: "compute-1")

    def holder_of(process: bool):
        return SimpleNamespace(capabilities_for=lambda _sid: SimpleNamespace(
            runtime=SimpleNamespace(has_os_process=process)))

    holders = {"s-direct": holder_of(False), "s-claude": holder_of(True)}
    monkeypatch.setattr(session_manager, "find_layer_for_session", holders.get)
    assert startup_drain.compute_open() == 1


def test_a_direct_prewarm_is_judged_by_its_tools_machine(monkeypatch):
    """A Direct pre-warm is held in the proxy, and it was built for the
    machine its record names: without that it would never be reused."""
    from core import concurrency_scan
    from core.session import session_manager
    from ws import dashboard_warmup_tail as tail
    monkeypatch.setattr(session_manager, "compute_placement_for", lambda _path, **_kw: COMPUTE)
    monkeypatch.setattr(session_manager, "live_session", lambda _sid: SimpleNamespace(machine_id=""))
    monkeypatch.setattr(concurrency_scan, "placed_on_compute", lambda _sid: "compute-1")
    assert tail._built_where_it_starts("pw-1", "direct-llm") is True
    monkeypatch.setattr(concurrency_scan, "placed_on_compute", lambda _sid: "")
    assert tail._built_where_it_starts("pw-1", "direct-llm") is False
