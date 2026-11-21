"""The phone WebSocket warmup and the call's identity: identity resolved
first, stamped for the call log, reuse requests validated, ephemeral caller
trees pruned at hangup.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import ws.phone as ws_phone
from core.session import external_identity
from services.phone.phone_identity import RouteIdentity
from tests.phone import test_ws_phone_turns as turns

SID = "11111111-2222-4333-8444-555555555555"
_run_handler = turns._run_handler
phone_ws_env = turns.phone_ws_env      # re-registers the fixture in this module


async def _warmup_frame(ws, **fields):
    msg = {"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True}
    msg.update(fields)
    ws.push(msg)
    return await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))


@pytest.mark.asyncio
async def test_identity_is_stamped_for_the_call_log(phone_ws_env, monkeypatch):
    ws, layer = phone_ws_env
    seen = {}
    ident = external_identity.resolve("phone", "+302101234567", session_id=SID)
    monkeypatch.setattr(
        ws_phone, "resolve_route_identity",
        lambda route, **kw: seen.update(kw) or RouteIdentity(mode="caller", role="viewer", external=ident),
    )
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="+30 210 1234567", pin_verified=True,
                                phone_route_id="r1")
    assert frame["type"] == "warmup_ready"
    sid = frame["data"]["session_id"]
    assert seen["caller_phone"] == "+30 210 1234567" and seen["pin_verified"] is True
    assert seen["session_id"] == sid
    assert ws_phone.pop_call_identity(sid) == "caller:+302101234567"
    assert ws_phone.pop_call_identity(sid) == ""     # consumed once
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_reuse_request_is_validated(phone_ws_env, monkeypatch):
    """A non-UUID / non-phone / other-agent session id is never attached."""
    ws, layer = phone_ws_env
    checked = {}
    monkeypatch.setattr(
        ws_phone, "_validated_reuse_sid",
        lambda sid, agent: checked.update(sid=sid, agent=agent) or "",
    )
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, session_id="not-a-phone-session")
    assert frame["type"] == "warmup_ready"
    assert checked == {"sid": "not-a-phone-session", "agent": "unified"}
    assert frame["data"]["session_id"] != "not-a-phone-session"
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)


def test_validated_reuse_sid_rules(temp_db, monkeypatch):
    from storage import database as task_store
    assert ws_phone._validated_reuse_sid("", "a") == ""
    assert ws_phone._validated_reuse_sid("nope", "a") == ""
    sid = str(uuid.uuid4())
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"source_type": "dashboard", "agent": "a"})
    assert ws_phone._validated_reuse_sid(sid, "a") == ""
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"source_type": "phone", "agent": "b"})
    assert ws_phone._validated_reuse_sid(sid, "a") == ""
    # A person's own call (a route tied to them) is theirs: no other call
    # speaks into it.
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"id": "c-mine", "source_type": "phone", "agent": "a", "user_sub": "user-me"})
    assert ws_phone._validated_reuse_sid(sid, "a") == ""
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"id": "c-call", "source_type": "phone", "agent": "a", "user_sub": "phone"})
    assert ws_phone._validated_reuse_sid(sid, "a") == sid
    # A conversation whose wrap-up began is over: no call speaks into it again.
    from storage.chat import conversation_store
    monkeypatch.setattr(conversation_store, "get",
                        lambda cid: {"wrapup": conversation_store.WRAPUP_RUNNING})
    assert ws_phone._validated_reuse_sid(sid, "a") == ""


@pytest.mark.asyncio
async def test_ephemeral_tree_is_pruned_at_hangup(phone_ws_env, monkeypatch, tmp_path):
    ws, layer = phone_ws_env
    agents = tmp_path / "agents"
    monkeypatch.setattr(ws_phone.config, "AGENTS_DIR", agents)
    monkeypatch.setattr(ws_phone.config, "get_agent_dir", lambda a: agents / a)
    pruned = []

    def _resolve(route, **kw):
        ident = external_identity.resolve("phone", "anonymous", session_id=kw["session_id"])
        home = external_identity.external_home(agents / "unified", ident)
        (home / "workspace").mkdir(parents=True)
        return RouteIdentity(mode="caller", role="viewer", external=ident)
    monkeypatch.setattr(ws_phone, "resolve_route_identity", _resolve)
    monkeypatch.setattr(ws_phone.external_identity, "prune_ephemeral",
                        lambda home: pruned.append(str(home)) or True)
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="anonymous")
    sid = frame["data"]["session_id"]
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    assert pruned == [str(agents / "unified" / "externals" / "phone" / "_ephemeral" / sid)]


@pytest.mark.asyncio
async def test_a_callers_conversation_is_recorded_and_owned_by_the_agent(phone_ws_env, monkeypatch):
    """An outside caller's call is a conversation of the agent's tab: the
    ``phone`` owner and its record, the caller line and the direction."""
    ws, layer = phone_ws_env
    made = []
    monkeypatch.setattr(ws_phone.task_store, "create_chat", lambda *a, **kw: made.append(a))
    ident = external_identity.resolve("phone", "+302101234567", session_id=SID)
    monkeypatch.setattr(ws_phone, "resolve_route_identity",
                        lambda route, **kw: RouteIdentity(mode="caller", role="external", external=ident))
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="+302101234567", call_type="outbound")
    assert frame["type"] == "warmup_ready"
    (chat_id, owner, *_), = made
    assert owner == ws_phone.PHONE_CHAT_OWNER
    assert layer.records[chat_id]["line"] == "caller"
    assert layer.records[chat_id]["direction"] == "outbound"
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_a_call_on_a_persons_route_is_their_own_chat(phone_ws_env, monkeypatch):
    """A route tied to a person makes the call that person's own chat: their
    history on the agent, no record, never the agent's Conversations tab."""
    ws, layer = phone_ws_env
    made = []
    monkeypatch.setattr(ws_phone.task_store, "create_chat", lambda *a, **kw: made.append(a))
    from core.session import visibility
    monkeypatch.setattr(visibility, "chat_history_owner", lambda agent, sub: sub)
    monkeypatch.setattr(ws_phone, "resolve_route_identity", lambda route, **kw: RouteIdentity(
        mode="user", role="external", external=None,
        user={"sub": "user-me", "username": "me"}, user_role="editor"))
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="+302101234567")
    assert frame["type"] == "warmup_ready"
    (chat_id, owner, *_), = made
    assert owner == "user-me"
    assert chat_id not in layer.records
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True], ids=["closed", "close_failed"])
async def test_a_persons_call_holds_their_chat_until_its_session_is_closed(
        phone_ws_env, monkeypatch, close_fails):
    """The call holds the person's chat from its warmup, so nobody else
    writes there. At the hang-up the hold goes only after the call's session
    is closed (a close that fails lets it go too), and the person's pages hear
    the chat's ready status, on which a locked page opens."""
    from services.notifications import notification_manager
    from services.phone import call_chats
    ws, layer = phone_ws_env
    call_chats._held.clear()
    monkeypatch.setattr(ws_phone.task_store, "create_chat",
                        lambda chat_id, owner, *a, **kw: {"id": chat_id, "user_sub": owner})
    from core.session import visibility
    monkeypatch.setattr(visibility, "chat_history_owner", lambda agent, sub: sub)
    monkeypatch.setattr(ws_phone, "resolve_route_identity", lambda route, **kw: RouteIdentity(
        mode="user", role="external", external=None,
        user={"sub": "user-me", "username": "me"}, user_role="editor"))
    told, at_close = [], []
    monkeypatch.setattr(notification_manager, "broadcast_chat_status",
                        lambda owner, cid, status, agent="": told.append((owner, cid, status)))

    async def _close(sid):
        at_close.append(dict(call_chats._held))
        if close_fails:
            raise RuntimeError("the machine went away")
    monkeypatch.setattr(layer, "close_session", _close)
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="+302101234567")
    sid = frame["data"]["session_id"]
    (chat_id,) = call_chats._held
    assert call_chats._held == {chat_id: sid} and told == []
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    assert at_close == [{chat_id: sid}]
    assert not call_chats.holds(chat_id)
    assert told == [("user-me", chat_id, "ready")]


@pytest.mark.asyncio
async def test_the_agents_conversation_is_let_go_without_a_status(phone_ws_env, monkeypatch):
    """An outside caller's conversation stays read-only after its call: the
    hold goes at the hang-up and nobody's pages are told to open it."""
    from services.notifications import notification_manager
    from services.phone import call_chats
    ws, layer = phone_ws_env
    call_chats._held.clear()
    monkeypatch.setattr(ws_phone.task_store, "create_chat",
                        lambda chat_id, owner, *a, **kw: {"id": chat_id, "user_sub": owner})
    told = []
    monkeypatch.setattr(notification_manager, "broadcast_chat_status",
                        lambda *a, **kw: told.append(a))
    task = await _run_handler(ws)
    await _warmup_frame(ws, caller_phone="+302101234567")
    (chat_id,) = call_chats._held
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    assert not call_chats.holds(chat_id) and told == []


class _PersonsCall:
    """A socket on a route tied to a person, as the phone service speaks it:
    every chat it mints and every session it starts, closes and binds, and
    what the person's pages hear, in one ordered list."""

    def __init__(self, ws, layer, monkeypatch):
        from core.session import visibility
        from services.notifications import notification_manager
        from services.phone import call_chats, call_control
        self.ws, self.layer, self.events = ws, layer, []
        self.chats: dict[str, dict] = {}
        call_chats._held.clear()

        def _create(chat_id, owner, *a, **kw):
            self.chats[chat_id] = {"id": chat_id, "user_sub": owner}
            return self.chats[chat_id]
        monkeypatch.setattr(ws_phone.task_store, "create_chat", _create)
        monkeypatch.setattr(ws_phone.task_store, "update_chat",
                            lambda chat_id, **kw: self.chats.get(chat_id, {}).update(kw))
        monkeypatch.setattr(ws_phone.task_store, "get_chat_by_session", lambda sid: next(
            (c for c in self.chats.values() if c.get("session_id") == sid), None))
        monkeypatch.setattr(visibility, "chat_history_owner", lambda agent, sub: sub)
        monkeypatch.setattr(ws_phone, "resolve_route_identity", lambda route, **kw: RouteIdentity(
            mode="user", role="external", external=None,
            user={"sub": "user-me", "username": "me"}, user_role="editor"))
        # The production answer for a person's own call: never reused.
        monkeypatch.setattr(ws_phone, "_validated_reuse_sid", lambda sid, agent: "")
        monkeypatch.setattr(ws_phone, "live_session", lambda sid: (
            SimpleNamespace(layer=layer) if sid in {s for s, _ in layer.started} else None))
        monkeypatch.setattr(notification_manager, "broadcast_chat_status",
                            lambda owner, cid, status, agent="": self.events.append(("told", cid, status)))

        async def _close(sid):
            self.events.append(("closed", sid))
        monkeypatch.setattr(layer, "close_session", _close)
        self.binding_for = call_control.binding_for
        self.held = call_chats._held

    async def warm(self, **extra) -> str:
        n = len(self.ws.sent)
        self.ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True,
                      "call_type": "outbound", "call_control": True, **extra})
        frame = await self.ws.wait_for_frame(
            lambda f: f["type"] in ("warmup_ready", "error") and f in self.ws.sent[n:])
        return frame["data"]["session_id"] if frame["type"] == "warmup_ready" else ""

    def started(self) -> list[str]:
        return [s for s, _ in self.layer.started]


@pytest.mark.asyncio
async def test_an_outbound_calls_second_warmup_is_the_same_call(phone_ws_env, monkeypatch):
    """The phone service warms an outbound call twice on one socket: the
    pre-warm, then the live pipeline naming the same session. It is one
    call: one session, one chat held from the first warm-up to the hang-up,
    the call tools bound again, and nothing told before the teardown."""
    ws, layer = phone_ws_env
    call = _PersonsCall(ws, layer, monkeypatch)
    task = await _run_handler(ws)
    s1 = await call.warm()
    (c1,) = call.held
    assert await call.warm(session_id=s1) == s1
    assert call.started() == [s1] and call.held == {c1: s1} and call.events == []
    assert call.binding_for(s1) is not None
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    assert call.events == [("closed", s1), ("told", c1, "ready")]
    assert call.held == {} and call.binding_for(s1) is None


@pytest.mark.asyncio
async def test_a_second_chat_on_one_socket_keeps_the_first_held_until_the_teardown(
        phone_ws_env, monkeypatch):
    """A second warm-up that names no session starts a second session and
    chat. The first stays held, its session alive and bound, until the
    teardown closes every session the socket started; only then does any
    chat open."""
    ws, layer = phone_ws_env
    call = _PersonsCall(ws, layer, monkeypatch)
    task = await _run_handler(ws)
    s1 = await call.warm()
    (c1,) = call.held
    s2 = await call.warm()
    (c2,) = set(call.held) - {c1}
    assert call.started() == [s1, s2] and call.held == {c1: s1, c2: s2} and call.events == []
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    closes = [e for e in call.events if e[0] == "closed"]
    tolds = [e for e in call.events if e[0] == "told"]
    assert sorted(closes) == sorted([("closed", s1), ("closed", s2)])
    assert sorted(tolds) == sorted([("told", c1, "ready"), ("told", c2, "ready")])
    assert call.events[:2] == closes[:2] and call.held == {}
    assert call.binding_for(s1) is None and call.binding_for(s2) is None


@pytest.mark.asyncio
async def test_a_warmup_that_fails_after_its_hold_keeps_it_until_the_teardown(phone_ws_env, monkeypatch):
    """The session started and the chat is held when the warm-up fails: the
    chat stays the call's until the teardown has closed the session."""
    from services.apps import catalog
    ws, layer = phone_ws_env
    call = _PersonsCall(ws, layer, monkeypatch)

    def _fails(row):
        raise RuntimeError("the catalog went away")
    monkeypatch.setattr(catalog, "announce_new_chat", _fails)
    task = await _run_handler(ws)
    assert await call.warm() == ""
    (s1,) = call.started()
    (c1,) = call.held
    assert call.held == {c1: s1} and call.events == []
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    assert call.events == [("closed", s1), ("told", c1, "ready")] and call.held == {}


@pytest.mark.parametrize("mode,owner,role,stored", [
    ("caller", "phone", "", "auto"),                  # the agent's conversation: the call's for good
    ("user", "user-me", "editor", "autoReview"),      # a person's own chat: a new chat of theirs
    ("user", "agent::unified", "editor", "autoReview"),
    ("user", "agent::unified", "viewer", "ask"),      # a shared chat below the editor role
])
def test_a_calls_chat_is_stored_in_the_mode_of_its_owner(temp_db, monkeypatch, mode, owner, role, stored):
    """The call's own session runs unattended from its config. Its chat row
    is what the person's chat starts from after the call: the mode a new
    chat of theirs is stored with (``chat_rebuild.new_chat_mode``)."""
    from core.session import visibility
    from storage import database as task_store
    monkeypatch.setattr(ws_phone.config, "get_cli_model", lambda agent: "model-x")
    monkeypatch.setattr(visibility, "chat_history_owner", lambda agent, sub: owner)
    identity = (RouteIdentity(mode="user", role="external", external=None,
                              user={"sub": "user-me", "username": "me"}, user_role=role)
                if mode == "user" else None)
    cid = str(uuid.uuid4())
    ws_phone._create_call_chat(cid, "unified", identity, execution_path="claude-code-cli",
                               session_id=str(uuid.uuid4()), identity_id="", call_type="inbound")
    row = task_store.get_chat(cid)
    assert (row["user_sub"], row["permission_mode"]) == (owner, stored)
