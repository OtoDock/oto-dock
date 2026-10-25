"""Regression tests for RemoteExecutionLayer.can_resume_session.

Bug being guarded against: clicking Stop on a remote CLI session set
``info.cli_dead = True``, the dashboard's auto-resume path then called
``prepare_resume`` (popping ``_sessions[session_id]``) BEFORE
``can_resume_session``. The old naive ``can_resume_session`` simply
returned ``True`` when ``info`` existed in the dict — and ``False`` when
it didn't. After ``prepare_resume`` popped the entry, ``can_resume_session``
always returned False, the dashboard fell through to the
fresh-session branch, and the user's chat memory was silently wiped.

Two-part fix being tested here:
1. Order in ``ws/dashboard.py``: ``can_resume_session`` is now called
   BEFORE ``prepare_resume`` so the in-memory info is still available
   for the fast path.
2. ``can_resume_session`` no longer trusts in-memory state alone — it
   RPCs the satellite's ``check_session_resumable`` handler to actually
   read the JSONL on disk. Also has a fallback path via
   ``resolve_execution_target`` for when ``_sessions`` is empty (idle
   reap / proxy restart) so the bug can't sneak in through any other
   call site.

Since 1.9.0 the answer is a ``ResumeVerdict``: a machine away, a probe
unanswered or a read the machine could not finish is UNKNOWN (the fresh
branch is irreversible, and the 2026-10-10 stall lost a chat to a timeout
read as "no"); the probe sent stays outstanding across the helper's rounds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from core.execution_layer import MachineReach, ResumeVerdict
from core.layers.cli import remote as cli_remote
from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo


def _make_info(
    session_id: str = "sess-1",
    machine_id: str = "m-1",
    execution_path: str = "claude-code-cli",
) -> RemoteSessionInfo:
    translator = ClaudeCLIEventTranslator(session_id)
    settle = SettleController(session_id, 0, translator)
    info = RemoteSessionInfo(
        session_id=session_id,
        machine_id=machine_id,
        agent_name="agent-1",
        execution_path=execution_path,
        event_queue=asyncio.Queue(),
    )
    info.cli_translator = translator
    info.cli_settle = settle
    return info


class _FakeCM:
    """The connection manager's probe surface: ``begin_command`` hands back
    an ack future the adapter waits on; ``answer`` resolves it at once,
    ``None`` leaves it pending (a silent machine)."""

    def __init__(self, *, connected: bool = True, answer: dict | None = None):
        self.connected = connected
        self.answer = answer
        self.conn = object()
        self.sent: list[dict] = []
        self.futures: list[asyncio.Future] = []
        self.forgotten: list[str] = []
        self.dropped_recently = False

    def is_connected(self, machine_id: str) -> bool:
        return self.connected

    def get_connection(self, machine_id: str):
        return self.conn if self.connected else None

    async def begin_command(self, machine_id: str, msg: dict):
        if not self.connected:
            raise RuntimeError("not connected")
        self.sent.append({"machine": machine_id, **msg})
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if self.answer is not None:
            fut.set_result(dict(self.answer))
        self.futures.append(fut)
        return f"cid-{len(self.sent)}", fut

    def forget_command(self, command_id: str) -> None:
        self.forgotten.append(command_id)

    def machine_dropped_recently(self, machine_id: str) -> bool:
        return self.dropped_recently


def _make_layer(cm: _FakeCM | None = None) -> RemoteExecutionLayer:
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = cm if cm is not None else _FakeCM(answer={"resumable": True})
    layer._sessions = {}
    return layer


@pytest.fixture(autouse=True)
def _no_outstanding_probes(monkeypatch):
    from core.remote import remote_resume
    cli_remote._probes.clear()
    remote_resume._resume_targets.clear()
    monkeypatch.setattr(cli_remote, "_PROBE_WAIT_S", 0.05)
    yield
    cli_remote._probes.clear()
    remote_resume._resume_targets.clear()


@pytest.mark.asyncio
async def test_can_resume_session_rpcs_satellite_when_info_present():
    """Fast path: info is in _sessions → use its machine_id, RPC the satellite."""
    layer = _make_layer()
    info = _make_info()
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )

    assert result is ResumeVerdict.RESUMABLE
    assert len(layer._cm.sent) == 1
    payload = layer._cm.sent[0]
    assert payload["machine"] == "m-1"
    assert payload["type"] == "check_session_resumable"
    assert payload["session_id"] == "sess-1"
    assert payload["agent_slug"] == "agent-1"
    assert payload["username"] == "alice"


@pytest.mark.asyncio
async def test_can_resume_session_is_fresh_when_satellite_says_no():
    """``resumable: false`` is the one definite no: the fresh-session branch."""
    layer = _make_layer(_FakeCM(answer={"resumable": False, "reason": "missing"}))
    info = _make_info()
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is ResumeVerdict.FRESH


@pytest.mark.asyncio
async def test_can_resume_session_falls_back_to_target_resolution():
    """Slow path: _sessions empty (e.g. after prepare_resume or idle reap)
    → derive machine_id from resolve_execution_target. The post-abort
    auto-resume path in ws/dashboard.py reaches this branch when the
    reorder + cache races; the idle-reap path always hits it because the
    reaper already popped _sessions.
    """
    layer = _make_layer()  # _sessions is empty

    with patch("storage.database.get_user_sub_by_username", return_value="user-sub-X"), \
         patch("services.remote.remote_status.resolve_execution_target",
               return_value=("m-2", None)):
        result = await layer.can_resume_session(
            "sess-missing", agent_name="agent-1", username="alice",
        )

    assert result is ResumeVerdict.RESUMABLE
    assert layer._cm.sent[0]["machine"] == "m-2"  # used the resolved target, not _sessions


@pytest.mark.asyncio
async def test_can_resume_session_is_fresh_when_target_is_local():
    """If target resolves to 'local', we're not on a satellite: nothing of
    the session is on a machine."""
    layer = _make_layer()
    with patch("storage.database.get_user_sub_by_username", return_value=None), \
         patch("services.remote.remote_status.resolve_execution_target",
               return_value=("local", None)):
        result = await layer.can_resume_session(
            "sess-x", agent_name="agent-1", username="",
        )
    assert result is ResumeVerdict.FRESH
    assert layer._cm.sent == []


@pytest.mark.asyncio
async def test_can_resume_session_is_unknown_when_satellite_offline():
    """is_connected=False → no RPC and UNKNOWN: the machine may be back in a
    moment, and a fresh session now would orphan its transcript for good."""
    layer = _make_layer(_FakeCM(connected=False))
    info = _make_info()
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is ResumeVerdict.UNKNOWN
    assert layer._cm.sent == []


@pytest.mark.asyncio
async def test_can_resume_session_is_unknown_when_the_send_fails():
    layer = _make_layer(_FakeCM())
    info = _make_info()
    layer._sessions[info.session_id] = info

    async def _refused(machine_id, msg):
        raise RuntimeError("Satellite m-1 not connected")

    layer._cm.begin_command = _refused
    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is ResumeVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_an_unanswered_probe_is_unknown_and_stays_out():
    """A machine connected but silent: UNKNOWN after the wait, and the probe
    stays outstanding, so the helper's next round sends no second frame
    (a slow machine is not asked to read the same transcript five times).
    The answer, when it comes, is read by the next call."""
    cm = _FakeCM(answer=None)
    layer = _make_layer(cm)
    info = _make_info()
    layer._sessions[info.session_id] = info

    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.UNKNOWN
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.UNKNOWN
    assert len(cm.sent) == 1
    assert ("m-1", "sess-1") in cli_remote._probes

    cm.futures[0].set_result({"resumable": True})
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.RESUMABLE
    assert len(cm.sent) == 1
    assert ("m-1", "sess-1") not in cli_remote._probes


@pytest.mark.asyncio
async def test_the_kept_probe_is_handed_to_a_watcher_and_forgotten_on_request():
    """A deferred turn's watcher gets the outstanding probe's future through
    the layer (the target the probe resolved, read again from no store);
    ``forget_resume_probe`` drops it and the connection manager's wait. A
    probe of a connection that was replaced is nobody's."""
    cm = _FakeCM(answer=None)
    layer = _make_layer(cm)
    info = _make_info()
    layer._sessions[info.session_id] = info
    assert layer.resume_probe_future(info.session_id, agent_name="agent-1") is None
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.UNKNOWN
    future = layer.resume_probe_future(info.session_id, agent_name="agent-1")
    assert future is cm.futures[0] and not future.done()
    layer.forget_resume_probe(info.session_id, agent_name="agent-1")
    assert cm.forgotten == ["cid-1"]
    assert ("m-1", "sess-1") not in cli_remote._probes
    assert layer.resume_probe_future(info.session_id, agent_name="agent-1") is None
    # Outstanding again, then the machine reconnects: the old probe is not
    # handed out (a new connection sends a new one).
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.UNKNOWN
    cm.conn = object()
    assert layer.resume_probe_future(info.session_id, agent_name="agent-1") is None


@pytest.mark.asyncio
async def test_a_frame_that_never_left_waits_for_no_ack():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.remote.satellite_connection import SatelliteConnectionManager
    mgr = SatelliteConnectionManager()
    mgr._connections["m1"] = SimpleNamespace(enqueue_send=AsyncMock(side_effect=RuntimeError("closed")))
    with pytest.raises(RuntimeError):
        await mgr.begin_command("m1", {"type": "x"})
    assert mgr._pending_acks == {}


@pytest.mark.asyncio
async def test_a_reconnect_sends_a_new_probe_and_forgets_the_old():
    cm = _FakeCM(answer=None)
    layer = _make_layer(cm)
    info = _make_info()
    layer._sessions[info.session_id] = info
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.UNKNOWN

    cm.conn = object()   # the machine reconnected
    cm.answer = {"resumable": False, "reason": "missing"}
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.FRESH
    assert len(cm.sent) == 2
    assert cm.forgotten == ["cid-1"]


@pytest.mark.asyncio
async def test_a_dropped_machine_fails_the_probe_into_unknown():
    """``deregister`` fails every pending ack: the wait ends UNKNOWN and the
    next call sends anew (on the new connection)."""
    cm = _FakeCM(answer=None)
    layer = _make_layer(cm)
    info = _make_info()
    layer._sessions[info.session_id] = info
    task = asyncio.ensure_future(layer.can_resume_session(info.session_id, agent_name="agent-1"))
    await asyncio.sleep(0)
    cm.futures[0].set_exception(RuntimeError("Satellite m-1 disconnected"))
    assert await task is ResumeVerdict.UNKNOWN
    assert ("m-1", "sess-1") not in cli_remote._probes


@pytest.mark.asyncio
async def test_a_read_the_machine_could_not_finish_is_unknown():
    layer = _make_layer(_FakeCM(answer={"status": "error", "error": "OSError: Cannot allocate memory"}))
    info = _make_info()
    layer._sessions[info.session_id] = info
    assert await layer.can_resume_session(info.session_id, agent_name="agent-1") is ResumeVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_a_store_error_reading_the_chat_is_unknown():
    """A DB blip must not empty the resume handle into a digest."""
    layer = _make_layer()

    def _boom(sid):
        raise psycopg.OperationalError("connection lost")

    with patch("storage.database.get_chat_by_session", _boom):
        result = await layer.can_resume_session("sess-gone", agent_name="agent-1", username="alice")
    assert result is ResumeVerdict.UNKNOWN
    assert layer._cm.sent == []


@pytest.mark.asyncio
async def test_an_offline_target_is_unknown():
    layer = _make_layer()
    with patch("storage.database.get_user_sub_by_username", return_value=None), \
         patch("services.remote.remote_status.resolve_execution_target",
               return_value=("__offline__:m-9", None)):
        result = await layer.can_resume_session("sess-x", agent_name="agent-1", username="")
    assert result is ResumeVerdict.UNKNOWN
    assert layer._cm.sent == []


@pytest.mark.asyncio
async def test_can_resume_session_codex_uses_thread_id_no_rpc():
    """Codex execution path: resume keys on codex_thread_id (the Codex CLI
    itself reads .codex/sessions/<thread>.jsonl at spawn time). No RPC."""
    layer = _make_layer()
    info = _make_info(execution_path="codex-cli")
    info.resume_handle = "thr-123"
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is ResumeVerdict.RESUMABLE
    assert layer._cm.sent == []


@pytest.mark.asyncio
async def test_can_resume_session_codex_without_thread_id_is_fresh():
    """Codex with no thread_id captured yet → not resumable."""
    layer = _make_layer()
    info = _make_info(execution_path="codex-cli")
    info.resume_handle = ""
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(info.session_id)
    assert result is ResumeVerdict.FRESH
    assert layer._cm.sent == []


def test_a_verdict_is_never_a_bool():
    """A call site that still reads the answer as a bool fails at its first
    run instead of resuming fresh on a slow machine."""
    for verdict in ResumeVerdict:
        with pytest.raises(TypeError):
            bool(verdict)


class TestResumeMachineReach:
    def test_connected(self):
        layer = _make_layer(_FakeCM())
        info = _make_info()
        layer._sessions[info.session_id] = info
        assert layer.resume_machine_reach(info.session_id) is MachineReach.CONNECTED

    def test_dropped_a_moment_ago_is_worth_a_wait(self):
        cm = _FakeCM(connected=False)
        cm.dropped_recently = True
        layer = _make_layer(cm)
        info = _make_info()
        layer._sessions[info.session_id] = info
        assert layer.resume_machine_reach(info.session_id) is MachineReach.AWAY_RECENT

    def test_away_past_the_grace(self):
        layer = _make_layer(_FakeCM(connected=False))
        info = _make_info()
        layer._sessions[info.session_id] = info
        assert layer.resume_machine_reach(info.session_id) is MachineReach.AWAY

    def test_a_local_target_is_the_platforms(self):
        layer = _make_layer()
        with patch("storage.database.get_user_sub_by_username", return_value=None), \
             patch("services.remote.remote_status.resolve_execution_target",
                   return_value=("local", None)):
            assert layer.resume_machine_reach("sess-x", agent_name="agent-1") is MachineReach.LOCAL

    def test_a_store_error_reads_as_away(self):
        layer = _make_layer()

        def _boom(sid):
            raise psycopg.OperationalError("connection lost")

        with patch("storage.database.get_chat_by_session", _boom):
            assert layer.resume_machine_reach("sess-x", agent_name="agent-1") is MachineReach.AWAY


# --- _resume_username_for_chat (ws/dashboard.py) -----------------------------
#
# The username handed to can_resume_session decides WHICH .claude/ dir the
# check probes (users/<u>/ vs the shared workspace/). A Shared-only agent's
# human chats mount the agent scope — the JSONL lives under workspace/.claude —
# so passing the viewer's username made the check probe a dir that doesn't
# exist and refuse EVERY resume: each re-warm (satellite restart, proxy
# restart, idle reap) silently rotated the chat to a fresh, context-less
# session. Task chats had the same bug fixed earlier via resolve_task_identity;
# these tests pin down all four identity branches.


def test_resume_username_regular_chat_per_user_agent(monkeypatch):
    """Per-user agent: the viewer's dir holds the session file."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(dashboard._vis, "is_shared_only", lambda name: False)
    assert dashboard._resume_username_for_chat("chat-1", "agent-1", "alice") == "alice"


def test_resume_username_regular_chat_shared_only_agent(monkeypatch):
    """Shared-only agent: human chats mount the agent scope → ''."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(
        dashboard._vis, "is_shared_only", lambda name: name == "shared-bot",
    )
    assert dashboard._resume_username_for_chat("chat-1", "shared-bot", "alice") == ""


def test_resume_username_task_chat_agent_scope(monkeypatch):
    """Agent-scope task: the run's identity (no user dir) wins over the viewer."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(
        dashboard.task_store, "get_run",
        lambda run_id: {"scope": "agent", "created_by": "sub-1"},
    )
    monkeypatch.setattr(
        dashboard, "resolve_task_identity",
        lambda agent, scope, created_by, **kw: SimpleNamespace(username=""),
    )
    assert dashboard._resume_username_for_chat("task-r1", "agent-1", "alice") == ""


def test_resume_username_task_chat_user_scope(monkeypatch):
    """User-scope task: the CREATOR's dir, not the viewer's."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(
        dashboard.task_store, "get_run",
        lambda run_id: {"scope": "user", "created_by": "sub-bob"},
    )
    monkeypatch.setattr(
        dashboard, "resolve_task_identity",
        lambda agent, scope, created_by, **kw: SimpleNamespace(username="bob"),
    )
    assert dashboard._resume_username_for_chat("task-r1", "agent-1", "alice") == "bob"


# ---------------------------------------------------------------------------
# Resume affinity: the layer used for liveness/resume checks must be resolved
# from the chat's PINNED execution_target, never re-resolved from the agent's
# current default. Regression: an agent retargeted local↔remote between
# sessions made the dashboard consult the wrong machine about the old session
# — a live local session read dead, an on-disk resume was refused, and the
# chat lost its context to a fresh reseeded spawn.
# ---------------------------------------------------------------------------


def test_get_execution_layer_pinned_local_wins_over_remote_agent_default(monkeypatch):
    """Pin 'local' → CLI layer even when the agent's default target is remote."""
    from core.session import session_manager as sm
    monkeypatch.setattr(
        sm.agent_store, "get_agent",
        lambda name: {"execution_path": "claude-code-cli"},
    )
    from services.remote import remote_status
    def _boom(*a, **k):
        raise AssertionError("pinned call must not re-resolve the target")
    monkeypatch.setattr(remote_status, "resolve_execution_target", _boom)
    layer = sm.get_execution_layer(
        "agent-1", user_sub="sub-1", execution_target="local",
    )
    assert layer is sm._cli_layer


def test_get_execution_layer_pinned_remote_wins_over_local_agent_default(monkeypatch):
    """Pin a machine id → remote layer even when the agent now resolves local."""
    from core.session import session_manager as sm
    monkeypatch.setattr(
        sm.agent_store, "get_agent",
        lambda name: {"execution_path": "claude-code-cli"},
    )
    from services.remote import remote_status
    from storage import remote_store
    monkeypatch.setattr(
        remote_status, "resolve_execution_target",
        lambda *a, **k: ("local", ""),
    )
    monkeypatch.setattr(
        remote_store, "get_remote_machine",
        lambda mid: {"pairing_scope": "admin", "registered_by": "sub-a"},
    )
    sentinel = MagicMock()
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: sentinel)
    layer = sm.get_execution_layer(
        "agent-1", user_sub="sub-1", execution_target="m-remote-1",
    )
    assert layer is sentinel


# ---------------------------------------------------------------------------
# Restart fallback must consult the CHAT ROW: a remote CODEX chat resumes by
# thread id and must never be probed through check_session_resumable (a
# .claude JSONL read that always fails for codex). Regression: after a proxy
# restart, every remote codex resume was refused and the chat silently
# reseeded from DB history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_resume_codex_after_restart_uses_chat_row_thread_id():
    layer = _make_layer()  # _sessions empty (proxy restarted)
    with patch("storage.database.get_chat_by_session", return_value={
        "execution_path": "codex-cli",
        "codex_thread_id": "thread-123",
        "execution_target": "m-win",
    }):
        result = await layer.can_resume_session(
            "sess-codex", agent_name="pa", username="alice",
        )
    assert result is ResumeVerdict.RESUMABLE
    assert layer._cm.sent == []  # codex never RPCs the JSONL read


@pytest.mark.asyncio
async def test_can_resume_codex_after_restart_without_thread_id_refuses():
    layer = _make_layer()
    with patch("storage.database.get_chat_by_session", return_value={
        "execution_path": "codex-cli",
        "codex_thread_id": "",
        "execution_target": "m-win",
    }):
        result = await layer.can_resume_session(
            "sess-codex", agent_name="pa", username="alice",
        )
    assert result is ResumeVerdict.FRESH
    assert layer._cm.sent == []


@pytest.mark.asyncio
async def test_can_resume_claude_after_restart_rpcs_the_pinned_machine():
    """A Claude chat row with a pinned machine RPCs THAT machine — not
    whatever the agent's target resolves to today."""
    layer = _make_layer()
    with patch("storage.database.get_chat_by_session", return_value={
        "execution_path": "claude-code-cli",
        "codex_thread_id": "",
        "execution_target": "m-pinned",
    }), patch("services.remote.remote_status.resolve_execution_target",
              side_effect=AssertionError("must not re-resolve when pinned")):
        result = await layer.can_resume_session(
            "sess-claude", agent_name="pa", username="alice",
        )
    assert result is ResumeVerdict.RESUMABLE
    assert layer._cm.sent[0]["machine"] == "m-pinned"
