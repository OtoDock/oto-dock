"""The call tools' proxy half: the registry (services/phone/call_control.py),
``POST /v1/phone/call-control``, the external allowlist entry, the phone
builder's tools block and MCP, the phone socket's binding and result
routing, and the Direct builtins."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from adapters.phone import call_tools_block
from api.phone import call_control as api
from auth.external_endpoints import is_external_endpoint_allowed
from core.layers.direct import builtins as direct_builtins
from services.phone import call_control
from tests.conftest import live_session_token


class Sender:
    def __init__(self, *, answer=None, sid="sid-1"):
        self.frames = []
        self.answer = answer   # (ok, message) answered as the daemon would
        self.sid = sid

    async def __call__(self, frame):
        self.frames.append(frame)
        if self.answer is not None:
            data = frame["data"]
            call_control.resolve(
                self.sid, data["request_id"],
                {"ok": self.answer[0], "message": self.answer[1]})


@pytest.fixture(autouse=True)
def clean_registry():
    call_control._bindings.clear()
    yield
    call_control._bindings.clear()


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_request_sends_the_frame_with_the_turn_and_returns_the_daemons_answer():
    sender = Sender(answer=(True, "closing"))
    call_control.bind("sid-1", sender, ask_back=False)
    call_control.set_turn("sid-1", 4)
    result = asyncio.run(call_control.request("sid-1", "end", {"reason": "bye"}))
    assert result == {"ok": True, "message": "closing"}
    frame = sender.frames[0]
    assert frame["type"] == "call_control" and frame["turn"] == 4
    assert frame["data"]["action"] == "end" and frame["data"]["reason"] == "bye"
    assert len(frame["data"]["request_id"]) == 32


def test_request_without_a_binding_or_with_a_silent_line_fails_soft():
    assert asyncio.run(call_control.request("nobody", "end", {}))["ok"] is False
    sender = Sender()
    call_control.bind("sid-1", sender)
    result = asyncio.run(call_control.request("sid-1", "end", {}, timeout=0.05))
    assert result["ok"] is False and "did not answer" in result["message"]


def test_ask_needs_a_binding_that_waits_for_questions():
    call_control.bind("sid-1", Sender(answer=(True, "relayed")), ask_back=False)
    result = asyncio.run(call_control.request("sid-1", "ask", {"question": "q"}))
    assert result["ok"] is False and "not placed by another agent" in result["message"]
    call_control.bind("sid-1", Sender(answer=(True, "relayed")), ask_back=True)
    assert asyncio.run(call_control.request("sid-1", "ask", {"question": "q"}))["ok"] is True


def test_unbind_is_a_compare_and_delete_on_the_sender():
    old, new = Sender(), Sender()
    call_control.bind("sid-1", old)
    call_control.bind("sid-1", new)
    call_control.unbind("sid-1", old)
    assert call_control.binding_for("sid-1").sender is new
    call_control.unbind("sid-1", new)
    assert call_control.binding_for("sid-1") is None
    assert call_control.resolve("sid-1", "x", {}) is False


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def _body(**kw):
    return api.CallControlBody(**kw)


def test_endpoint_requires_a_live_session_token(temp_db):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.call_control_endpoint(_body(action="end"), None))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.call_control_endpoint(_body(action="end"), "Bearer nope"))
    assert e.value.status_code == 401


def test_endpoint_refuses_without_a_binding_and_relays_with_one(temp_db):
    from storage import database as task_store
    task_store.upsert_user("user-1", "user-1@test.com", "User 1", "member")
    token = live_session_token("sid-1", "agent-a", "user-1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.call_control_endpoint(_body(action="end"), f"Bearer {token}"))
    assert e.value.status_code == 409 and "No live call" in e.value.detail

    sender = Sender(answer=(True, "closing after your words"))
    call_control.bind("sid-1", sender)
    out = asyncio.run(api.call_control_endpoint(_body(action="end", reason="x" * 300), f"Bearer {token}"))
    assert out == {"ok": True, "message": "closing after your words"}
    assert len(sender.frames[0]["data"]["reason"]) == 200

    with pytest.raises(HTTPException) as e:
        asyncio.run(api.call_control_endpoint(_body(action="transfer"), f"Bearer {token}"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.call_control_endpoint(_body(action="dance"), f"Bearer {token}"))
    assert e.value.status_code == 400

    sender.answer = (False, "No transfer target named Kitchen")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.call_control_endpoint(_body(action="transfer", target="Kitchen"), f"Bearer {token}"))
    assert e.value.status_code == 409 and "Kitchen" in e.value.detail


def test_an_external_callers_token_reaches_the_endpoint(temp_db):
    """An inbound caller who is not a platform user ends their own call."""
    token = live_session_token("sid-ext", "agent-a", "", external="phone:+30210")
    call_control.bind("sid-ext", Sender(answer=(True, "closing"), sid="sid-ext"))
    out = asyncio.run(api.call_control_endpoint(_body(action="end"), f"Bearer {token}"))
    assert out["ok"] is True
    assert is_external_endpoint_allowed("POST", "/v1/phone/call-control")
    assert not is_external_endpoint_allowed("GET", "/v1/phone/call-control")


# ---------------------------------------------------------------------------
# The prompt block
# ---------------------------------------------------------------------------

def test_call_tools_block_names_the_tools_and_the_targets_by_label():
    block = call_tools_block(ask_back=True, transfer_targets=[
        {"label": "Reception", "number": "200"}, {"number": "+30699"}])
    assert "end_call(reason)" in block and "Reception, +30699" in block
    assert "ask_initiating_agent" in block and "200" not in block.split("Targets")[1].split(".")[0]
    assert "still work as a fallback" in block
    inbound = call_tools_block(ask_back=False, transfer_targets=[])
    assert "ask_initiating_agent" not in inbound and "no transfer targets" in inbound


# ---------------------------------------------------------------------------
# The Direct builtins
# ---------------------------------------------------------------------------

def test_direct_call_tools_are_registered_and_offered_per_call():
    names = [t["name"] for t in direct_builtins.call_tool_defs(ask_back=False)]
    assert names == ["end_call", "transfer_call"]
    names = [t["name"] for t in direct_builtins.call_tool_defs(ask_back=True)]
    assert names == ["end_call", "transfer_call", "ask_initiating_agent"]
    assert "end_call" not in direct_builtins.CLIENT_TOOL_NAMES
    for n in names:
        assert direct_builtins.get(n).tier == direct_builtins.TIER_OPEN


def test_direct_registers_the_call_tools_only_on_the_builders_word():
    """A phone session gets the builtins when the builder set the flag (the
    daemon advertised call_control); an older daemon's session keeps the
    markers; a chat of a phone-mcp agent carries them for in-session calls."""
    from core.layers.direct.session import call_tools_wanted
    assert call_tools_wanted(True, True, has_phone_mcp=False) is True
    assert call_tools_wanted(True, False, has_phone_mcp=False) is False
    assert call_tools_wanted(True, False, has_phone_mcp=True) is False
    assert call_tools_wanted(False, False, has_phone_mcp=True) is True
    assert call_tools_wanted(False, False, has_phone_mcp=False) is False
    assert call_control.CALL_TOOLS_ENV == "OTO_CALL_TOOLS"


def test_the_no_binding_refusal_points_at_the_marker():
    out = asyncio.run(call_control.request("no-such-sid", "end", {}))
    assert out["ok"] is False and "[CALL_COMPLETE]" in out["message"]


def test_direct_end_call_marks_the_session_closing_only_when_accepted():
    session = SimpleNamespace(session_id="sid-d", line_closing=False)
    call_control.bind("sid-d", Sender(answer=(True, "closing"), sid="sid-d"))
    text = asyncio.run(direct_builtins.execute(session, "end_call", {"reason": "bye"}))
    assert text == "closing" and session.line_closing is True

    session.line_closing = False
    call_control.bind("sid-d", Sender(answer=(False, "An end is already pending"), sid="sid-d"))
    text = asyncio.run(direct_builtins.execute(session, "transfer_call", {"target": "x"}))
    assert text.startswith("Not done:") and session.line_closing is False
    text = asyncio.run(direct_builtins.execute(session, "transfer_call", {}))
    assert text.startswith("Error: target is required")


# ---------------------------------------------------------------------------
# The phone socket: binding at warmup, the result frame, the builder flag
# ---------------------------------------------------------------------------

from tests.phone.test_ws_phone_turns import phone_ws_env, _run_handler  # noqa: E402,F401


def test_phone_socket_binds_a_tool_daemon_and_routes_its_results(phone_ws_env, monkeypatch):  # noqa: F811
    import ws.phone as ws_phone
    ws, layer = phone_ws_env
    seen = {}
    orig = ws_phone.build_phone_agent_config

    async def _build(**kw):
        seen.update(kw)
        return await orig(**kw)
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _build)

    async def run():
        handler = await _run_handler(ws)
        ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct",
                 "phone_mode": True, "call_type": "outbound", "call_control": True})
        frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert frame["type"] == "warmup_ready", frame
        sid = frame["data"]["session_id"]
        binding = call_control.binding_for(sid)
        assert binding is not None and binding.ask_back is True
        # The daemon's answer reaches a waiting request through the socket.
        req = asyncio.create_task(call_control.request(sid, "end", {}))
        sent = await ws.wait_for_frame(lambda f: f["type"] == "call_control")
        ws.push({"type": "call_control_result",
                 "data": {"request_id": sent["data"]["request_id"], "ok": True, "message": "closing"}})
        assert (await req) == {"ok": True, "message": "closing"}
        ws.disconnect()
        await handler
        assert call_control.binding_for(sid) is None
    asyncio.run(run())
    assert seen["call_tools"] is True and seen["transfer_targets"] == []


def test_a_line_that_cannot_transfer_lists_no_targets(phone_ws_env, monkeypatch):  # noqa: F811
    """The daemon's warmup says whether the call can hand the caller over (an
    outbound AMI call names no channel): the prompt lists the route's
    targets only then; an older daemon says nothing and keeps today's shape."""
    import ws.phone as ws_phone
    ws, layer = phone_ws_env
    seen = []
    targets = [{"label": "Reception", "number": "200"}]
    monkeypatch.setattr(ws_phone.phone_route_store, "get_route",
                        lambda rid: {"id": rid, "transfer_targets": targets})

    async def _build(**kw):
        seen.append(kw["transfer_targets"])
        return SimpleNamespace(execution_path="direct-llm", mcp_config_path="")
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _build)

    async def run():
        handler = await _run_handler(ws)
        for flag in ({"transfer": False}, {}):
            ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True,
                     "call_type": "outbound", "call_control": True, "phone_route_id": "r-1", **flag})
            await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready" and len(ws.sent) >= 1)
            ws.sent.clear()
        ws.disconnect()
        await handler
    asyncio.run(run())
    assert seen == [[], targets]


def test_phone_socket_keeps_the_markers_for_an_older_daemon(phone_ws_env, monkeypatch):  # noqa: F811
    import ws.phone as ws_phone
    ws, layer = phone_ws_env
    seen = {}

    async def _build(**kw):
        seen.update(kw)
        return SimpleNamespace(execution_path="direct-llm", mcp_config_path="")
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _build)

    async def run():
        handler = await _run_handler(ws)
        ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True})
        frame = await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        assert call_control.binding_for(frame["data"]["session_id"]) is None
        ws.disconnect()
        await handler
    asyncio.run(run())
    assert seen["call_tools"] is False
