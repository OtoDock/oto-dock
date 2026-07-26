"""Codex background terminals (unified_exec): translator detection/emission,
the session's idle OOB delivery + drain reconciliation, and teardown sweeps.

A ``commandExecution`` item may YIELD (unified_exec): the tool call returns to
the model, the item stays open (possibly past ``turn/completed``), and the
single ``item/completed`` arrives whenever the PTY exits — possibly while the
thread is idle or during a later turn. CRUCIALLY (observed live, 0.145.0): the
``item/started`` our wire carries for a REAL background terminal is the
approval-path SYNTHESIZED item with ``processId: null`` (the canonical item is
deduped by the app-server), so a null processId proves nothing — EVERY command
item is a candidate, the registry keys on the ITEM id throughout, and the
unified_exec processId is bound late (terminalInteraction) purely for
``thread/backgroundTerminals/list`` matching. The translator mirrors the CLI's
bg-bash contract (BG_COMMAND_START/END keyed by the itemId); the session adds
the between-turns delivery (router OOB hook) and the pull-API drain backstop.
"""
import asyncio

from core.events.bg_command_state import (
    _bg_command_registries, get_bg_command_registry,
)
from core.events.common_events import (
    BG_COMMAND_END, BG_COMMAND_START, DONE, TEXT, TOOL_RESULT, TOOL_USE,
)
from core.layers.codex.app_server_client import AppServerError
from core.layers.codex.layer import CodexEventTranslator
from core.layers.codex.session import CodexAppServerSession, CodexEvent

MAIN = "thread-MAIN"


def _tr(sid="", *, supervised=True):
    tr = CodexEventTranslator(
        model="gpt-5.6-sol", supervised_bg=True,
        session_id=sid, supervised_bg_commands=supervised,
    )
    tr._main_thread_id = MAIN
    return tr


def _cmd_item(item_id, *, process_id=None, command="sleep 40", **extra):
    # processId defaults to None — the approval-path synthesized item/started
    # (what this wire actually carries for a real background terminal).
    item = {"type": "commandExecution", "id": item_id, "command": command,
            "processId": process_id}
    item.update(extra)
    return item


def _feed(tr, method, **params):
    return tr.translate(CodexEvent(type=method, data={"threadId": MAIN, **params}))


def _types(events):
    return [e.type for e in events]


# ---------------------------------------------------------------------------
# 1. Translator: detection + BG_COMMAND_START/END emission
# ---------------------------------------------------------------------------

def test_short_command_no_bg_events():
    # PTY (or plain shell) exits before any yield evidence → today's plain
    # Bash card, no BG events, tracking dropped, registry never touched.
    sid = "s-cx-short"
    try:
        tr = _tr(sid)
        out = _feed(tr, "item/started", item=_cmd_item("i1"))
        assert TOOL_USE in _types(out)
        done = _feed(tr, "item/completed", item=_cmd_item(
            "i1", status="completed", exitCode=0, aggregatedOutput="ok"))
        assert _types(done) == [TOOL_RESULT]
        end = _feed(tr, "turn/completed", turn={"status": "completed"})
        assert BG_COMMAND_START not in _types(end)
        assert BG_COMMAND_END not in _types(end)
        assert tr.pending_bg_commands() == []
        assert get_bg_command_registry(sid).spawned == set()
    finally:
        _bg_command_registries.pop(sid, None)


def test_null_process_id_is_tracked_and_resolves_cross_turn():
    # The primary real-world case: item/started carries processId None (the
    # synthesized approval-path item wins) — it MUST still be a candidate.
    sid = "s-cx-nullpid"
    try:
        tr = _tr(sid)
        _feed(tr, "item/started", item=_cmd_item("i1", process_id=None))
        out = _feed(tr, "item/agentMessage/delta", itemId="m1", delta="on it")
        assert _types(out) == [BG_COMMAND_START, TEXT]
        reg = get_bg_command_registry(sid)
        assert reg.spawned == {"i1"}           # registry keys on the ITEM id
        assert reg.tuid_for("i1") == "i1"
        _feed(tr, "turn/completed", turn={"status": "completed"})
        late = _feed(tr, "item/completed", item=_cmd_item(
            "i1", status="completed", exitCode=0, aggregatedOutput="done\n"))
        assert _types(late) == [BG_COMMAND_END]     # swept → END only
        assert late[0].data == {"tool_use_id": "i1", "status": "completed"}
        assert reg.completed == {"i1"}
    finally:
        _bg_command_registries.pop(sid, None)


def test_terminal_interaction_starts_badge_once_and_binds_pid():
    sid = "s-cx-interact"
    try:
        tr = _tr(sid)
        _feed(tr, "item/started", item=_cmd_item("i1", command="tail -f log"))
        out = _feed(tr, "item/commandExecution/terminalInteraction",
                    itemId="i1", processId="p-1", stdin="")
        assert len(out) == 1 and out[0].type == BG_COMMAND_START
        # Exact payload — the pump spreads **data into the WS frame.
        assert out[0].data == {"tool_use_id": "i1", "command": "tail -f log",
                               "description": "background terminal"}
        reg = get_bg_command_registry(sid)
        assert reg.spawned == {"i1"} and reg.tuid_for("i1") == "i1"
        # The interaction is the first place the pid reliably appears — it is
        # bound into the rec (drain matching) and reported by the accessor.
        assert tr.pending_bg_commands() == [
            {"process_id": "p-1", "item_id": "i1", "command": "tail -f log"}]
        # Idempotent on repeat (pid stays bound).
        assert _feed(tr, "item/commandExecution/terminalInteraction",
                     itemId="i1", processId="p-1", stdin="") == []
        assert tr.pending_bg_commands()[0]["process_id"] == "p-1"
    finally:
        _bg_command_registries.pop(sid, None)


def test_delta_while_open_starts_badge_once():
    tr = _tr()
    _feed(tr, "item/started", item=_cmd_item("i1"))
    out = _feed(tr, "item/agentMessage/delta", itemId="m1", delta="on it")
    assert _types(out) == [BG_COMMAND_START, TEXT]
    assert _types(_feed(tr, "item/agentMessage/delta", itemId="m1",
                        delta="…")) == [TEXT]
    # Reasoning deltas are equivalent yield evidence for a later candidate.
    _feed(tr, "item/started", item=_cmd_item("i2"))
    out2 = _feed(tr, "item/reasoning/textDelta", itemId="r1", delta="hm")
    assert _types(out2) == [BG_COMMAND_START, "thinking"]
    assert out2[0].data["tool_use_id"] == "i2"


def test_supervised_sweep_starts_but_never_ends():
    # Local layer: open candidate at turn/completed → START in the sweep, NO
    # END — the badge stays live until the real item/completed.
    sid = "s-cx-sweep"
    try:
        tr = _tr(sid)
        _feed(tr, "item/started", item=_cmd_item("i1", command="sleep 40"))
        end = _feed(tr, "turn/completed", turn={"status": "completed"})
        assert _types(end)[:1] == [BG_COMMAND_START]
        assert BG_COMMAND_END not in _types(end)
        assert tr.pending_bg_commands() == [
            {"process_id": None, "item_id": "i1", "command": "sleep 40"}]
        assert get_bg_command_registry(sid).spawned == {"i1"}
        assert get_bg_command_registry(sid).pending_count == 1
    finally:
        _bg_command_registries.pop(sid, None)


def test_cross_turn_completion_end_only_no_tool_result():
    # item/completed AFTER the sweep (cross-turn / idle arrival): BG_COMMAND_END
    # only — a TOOL_RESULT would attach the old transcript to a different live
    # Bash card via the pump's unknown-id name-match fallback (audit F2).
    sid = "s-cx-cross"
    try:
        tr = _tr(sid)
        _feed(tr, "item/started", item=_cmd_item("i1"))
        _feed(tr, "turn/completed", turn={"status": "completed"})
        late = _feed(tr, "item/completed", item=_cmd_item(
            "i1", status="failed", exitCode=3, aggregatedOutput="boom"))
        assert _types(late) == [BG_COMMAND_END]
        assert late[0].data == {"tool_use_id": "i1", "status": "failed"}
        reg = get_bg_command_registry(sid)
        assert reg.completed == {"i1"}
        assert reg.unsurfaced_count == 1   # the model never saw this completion
        assert tr.pending_bg_commands() == []
    finally:
        _bg_command_registries.pop(sid, None)


def test_mid_turn_completion_after_start_ends_with_tool_result():
    # STARTed via yield evidence, PTY exits later in the SAME turn → the card
    # completes normally AND the badge clears.
    sid = "s-cx-mid"
    try:
        tr = _tr(sid)
        _feed(tr, "item/started", item=_cmd_item("i1"))
        _feed(tr, "item/agentMessage/delta", itemId="m1", delta="waiting")
        done = _feed(tr, "item/completed", item=_cmd_item(
            "i1", status="completed", exitCode=0, aggregatedOutput="fine"))
        assert _types(done) == [TOOL_RESULT, BG_COMMAND_END]
        assert done[1].data == {"tool_use_id": "i1", "status": "completed"}
        reg = get_bg_command_registry(sid)
        assert reg.completed == {"i1"} and reg.unsurfaced_count == 0
    finally:
        _bg_command_registries.pop(sid, None)


def test_legacy_remote_sweep_resolves_at_turn_end():
    # supervised_bg_commands=False (LEGACY remote satellite < 0.5.18 — a
    # ≥0.5.18 satellite gets True and the remote router's OOB hook, see
    # tests/remote/test_remote_codex_bg.py): open candidate at turn end gets
    # START and immediately END(completed) — the badge never dangles.
    sid = "s-cx-remote"
    try:
        tr = _tr(sid, supervised=False)
        _feed(tr, "item/started", item=_cmd_item("i1"))
        end = _feed(tr, "turn/completed", turn={"status": "completed"})
        assert _types(end) == [BG_COMMAND_START, BG_COMMAND_END, "metadata", DONE]
        assert end[1].data == {"tool_use_id": "i1", "status": "completed"}
        assert get_bg_command_registry(sid).completed == {"i1"}
        assert tr.pending_bg_commands() == []
    finally:
        _bg_command_registries.pop(sid, None)


def test_interrupted_turn_still_sweeps():
    # turn/interrupt does NOT kill background terminals — the sweep runs on
    # every turn status, before the status branch.
    tr = _tr()
    _feed(tr, "item/started", item=_cmd_item("i1"))
    end = _feed(tr, "turn/completed", turn={"status": "interrupted"})
    assert _types(end) == [BG_COMMAND_START, DONE]
    assert tr.pending_bg_commands() != []


def test_untracked_completion_is_plain_tool_result():
    tr = _tr()
    out = _feed(tr, "item/completed", item=_cmd_item(
        "ghost", status="completed", exitCode=0, aggregatedOutput="x"))
    assert _types(out) == [TOOL_RESULT]


def test_turn_start_prunes_drain_resolved_candidates():
    # The drain resolved a candidate out-of-band (registry no longer pending) →
    # the next turn/started drops its tracking (keyed by itemId) so it can't
    # leak or re-END.
    sid = "s-cx-prune"
    try:
        tr = _tr(sid)
        _feed(tr, "item/started", item=_cmd_item("i1"))
        _feed(tr, "turn/completed", turn={"status": "completed"})
        reg = get_bg_command_registry(sid)
        reg.mark_done("i1", surfaced=False)    # what resolve_bg_command does
        reg.reset()                            # next turn's registry prune
        _feed(tr, "turn/started")
        assert tr.pending_bg_commands() == []
        assert not tr.tracks_bg_command({"item": _cmd_item("i1")})
    finally:
        _bg_command_registries.pop(sid, None)


# ---------------------------------------------------------------------------
# 2. Session: idle OOB delivery, drain reconciliation, teardown sweep
# ---------------------------------------------------------------------------

class _FakeClient:
    """Minimal AppServerClient: a notif_queue + a scriptable request()."""

    def __init__(self, terminals=None, *, fail_list=False):
        self.notif_queue: asyncio.Queue = asyncio.Queue()
        self.is_alive = True
        self.terminals = terminals if terminals is not None else []
        self.fail_list = fail_list
        self.list_calls = 0

    async def request(self, method, params=None, *, timeout=None):
        if method == "thread/backgroundTerminals/list":
            self.list_calls += 1
            if self.fail_list:
                raise AppServerError("daemon busy")
            return {"data": self.terminals}
        return {}


def _make_session(sid, client, translator):
    s = CodexAppServerSession(
        session_id=sid, agent_name="a", model="gpt-5.6-sol",
        sandbox_mode="workspace-write", working_dir="/tmp",
        config_dir="/tmp/.codex", thread_id=MAIN,
    )
    s._started = True
    s._client = client
    s.translator = translator
    return s


def _swept_candidate(tr, *, pid=None):
    """One background terminal past its turn-end sweep (badge live, pending).
    ``pid`` optionally binds the unified_exec processId via terminalInteraction
    first (the only place it reliably appears on this wire)."""
    _feed(tr, "item/started", item=_cmd_item("i1", command="sleep 40"))
    if pid:
        _feed(tr, "item/commandExecution/terminalInteraction",
              itemId="i1", processId=pid, stdin="")
    _feed(tr, "turn/completed", turn={"status": "completed"})


def test_oob_completion_between_turns_marks_registry_done():
    sid = "s-cx-oob"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr)
        session = _make_session(sid, _FakeClient(), tr)
        session._router_task = asyncio.create_task(session._route_notifications())
        try:
            # Idle (no _default_consumer): the exit's item/completed lands on
            # the router and must resolve via the OOB hook, not be dropped.
            session._client.notif_queue.put_nowait(("item/completed", {
                "threadId": MAIN,
                "item": _cmd_item("i1", status="completed", exitCode=0,
                                  aggregatedOutput="done"),
            }))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if not get_bg_command_registry(sid).has_pending:
                    break
            assert get_bg_command_registry(sid).completed == {"i1"}
            assert tr.pending_bg_commands() == []
        finally:
            await session._teardown_bg(reason="test")

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_resolves_item_missing_from_list():
    sid = "s-cx-drain"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr)
        client = _FakeClient(terminals=[])   # PTY gone — exit event was lost
        session = _make_session(sid, client, tr)
        assert await session.drain_bg_commands() is True
        assert get_bg_command_registry(sid).completed == {"i1"}
        assert tr.pending_bg_commands() == []
        # Self-paced: an immediate re-poll (monitor retries every 0.3s) makes
        # no second RPC. (No pending would fast-path anyway — re-arm one.)
        get_bg_command_registry(sid).register_spawn("i9", "i9")
        assert await session.drain_bg_commands() is False
        assert client.list_calls == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_keeps_item_present_in_list():
    # The list row names our tracked itemId → still running, NOT resolved
    # (works even when the processId was never learned).
    sid = "s-cx-drain-item"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr)
        client = _FakeClient(terminals=[
            {"itemId": "i1", "processId": "p-1", "command": "sleep 40"}])
        session = _make_session(sid, client, tr)
        assert await session.drain_bg_commands() is False
        assert get_bg_command_registry(sid).pending_count == 1
        assert tr.pending_bg_commands() != []

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_keeps_item_matched_by_process_id_only():
    # The list may name the CANONICAL itemId (ours is the synthesized
    # approval-path one) — the pid learned via terminalInteraction must still
    # match the row, keeping the entry pending.
    sid = "s-cx-drain-pid"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr, pid="p-1")
        client = _FakeClient(terminals=[
            {"itemId": "item-canonical-9", "processId": "p-1",
             "command": "sleep 40"}])
        session = _make_session(sid, client, tr)
        assert await session.drain_bg_commands() is False
        assert get_bg_command_registry(sid).pending_count == 1
        assert tr.pending_bg_commands() != []

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_rpc_failure_is_no_progress():
    sid = "s-cx-drain-fail"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr)
        session = _make_session(sid, _FakeClient(fail_list=True), tr)
        assert await session.drain_bg_commands() is False
        # Never resolve-on-error: the entry stays pending for the next poll.
        assert get_bg_command_registry(sid).pending_count == 1
        assert tr.pending_bg_commands() != []
        assert not session.lock.locked()

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_drain_backs_off_when_turn_in_flight():
    sid = "s-cx-drain-lock"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr)
        client = _FakeClient(terminals=[])
        session = _make_session(sid, client, tr)
        async with session.lock:
            assert await session.drain_bg_commands() is False
        assert client.list_calls == 0
        assert get_bg_command_registry(sid).pending_count == 1

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)


def test_teardown_bg_resolves_pending_commands_as_killed():
    sid = "s-cx-teardown"

    async def run():
        tr = _tr(sid)
        _swept_candidate(tr)
        session = _make_session(sid, _FakeClient(), tr)
        await session._teardown_bg(reason="test")
        assert get_bg_command_registry(sid).completed == {"i1"}
        assert tr.pending_bg_commands() == []

    try:
        asyncio.run(run())
    finally:
        _bg_command_registries.pop(sid, None)
