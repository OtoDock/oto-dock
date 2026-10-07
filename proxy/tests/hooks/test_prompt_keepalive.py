"""A prompt that waits on a person answers with keepalives.

``/v1/hooks/permission`` and ``/v1/hooks/codex-question`` answer a quick
decision as plain JSON. One that waits on a person streams whitespace every
keepalive interval and the JSON last (JSON readers skip leading whitespace,
and the content type stays ``application/json`` for aiohttp's ``json()``),
so the stream is never idle through a satellite's tunnel; a caller that went
away cancels the decision, which releases its wait. A prompt waits up to
``PROMPT_WAIT_S`` (three days), cut to its session token's remaining life.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from api.hooks import permission
from core.session import session_state


async def _collect(response) -> list[bytes]:
    return [chunk async for chunk in response.body_iterator]


@pytest.mark.asyncio
async def test_a_quick_decision_is_plain_json():
    async def decide():
        return {"decision": "allow"}

    assert await permission._answer_with_keepalive(decide(), {"decision": "deny"}) == {
        "decision": "allow"}


@pytest.mark.asyncio
async def test_a_decision_that_waits_streams_keepalives_then_the_json(monkeypatch):
    monkeypatch.setattr(permission, "_QUICK_S", 0.01)
    monkeypatch.setattr(permission, "_KEEPALIVE_S", 0.02)

    async def decide():
        await asyncio.sleep(0.12)
        return {"decision": "allow"}

    resp = await permission._answer_with_keepalive(decide(), {"decision": "deny"})
    assert resp.media_type == "application/json"
    chunks = await _collect(resp)
    assert chunks[-1] == b'{"decision": "allow"}'
    assert set(chunks[:-1]) == {b" "} and len(chunks) >= 3
    assert json.loads(b"".join(chunks)) == {"decision": "allow"}


@pytest.mark.asyncio
async def test_a_caller_that_goes_away_releases_the_wait(monkeypatch):
    monkeypatch.setattr(permission, "_QUICK_S", 0.01)
    monkeypatch.setattr(permission, "_KEEPALIVE_S", 0.02)
    released = asyncio.Event()

    async def decide():
        try:
            await asyncio.sleep(3600)
        finally:
            released.set()

    resp = await permission._answer_with_keepalive(decide(), {"decision": "deny"})
    body = resp.body_iterator
    assert await body.__anext__() == b" "
    await body.aclose()  # the client disconnected
    await asyncio.wait_for(released.wait(), 1)


@pytest.mark.asyncio
async def test_a_caller_gone_before_the_stream_releases_the_wait():
    """The request is cancelled during the quick wait (the client left
    before any byte): the decision is cancelled too, never left parked."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def decide():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    handler = asyncio.ensure_future(
        permission._answer_with_keepalive(decide(), {"decision": "deny"}))
    await started.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.asyncio
async def test_a_decision_that_fails_mid_stream_ends_with_the_deny(monkeypatch):
    monkeypatch.setattr(permission, "_QUICK_S", 0.01)
    monkeypatch.setattr(permission, "_KEEPALIVE_S", 0.02)

    async def decide():
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    resp = await permission._answer_with_keepalive(decide(), {"decision": "deny", "reason": "x"})
    chunks = await _collect(resp)
    assert json.loads(chunks[-1]) == {"decision": "deny", "reason": "x"}


def test_a_prompt_waits_three_days_or_until_its_token_ends(monkeypatch):
    assert session_state.PROMPT_WAIT_S == 3 * 24 * 3600
    monkeypatch.setattr(permission, "_session_payload", lambda auth, sid: None)
    assert permission._prompt_wait_s("Bearer master", "s") == session_state.PROMPT_WAIT_S
    soon = int(time.time()) + 3600
    monkeypatch.setattr(permission, "_session_payload", lambda auth, sid: {"exp": soon})
    assert 3400 < permission._prompt_wait_s("Bearer t", "s") <= 3600 - permission._TOKEN_MARGIN_S
    later = int(time.time()) + 10 * 24 * 3600
    monkeypatch.setattr(permission, "_session_payload", lambda auth, sid: {"exp": later})
    assert permission._prompt_wait_s("Bearer t", "s") == session_state.PROMPT_WAIT_S


def test_a_pending_prompt_is_seen_only_while_its_waiter_lives():
    session_state._session_permission_requests["s-p"] = {"r-p"}
    try:
        assert session_state.has_pending_prompt("s-p") is False  # no waiter
        session_state._permission_events["r-p"] = asyncio.Event()
        assert session_state.has_pending_prompt("s-p") is True
    finally:
        session_state._permission_events.pop("r-p", None)
        session_state._session_permission_requests.pop("s-p", None)


# A prompt whose wait ends with no answer (its caller went away, it ran out
# of time, an abort or a close released it) retires its card: a
# ``prompt_retired`` item for its request_id follows the prompt on the queue
# it was raised on, marked ``caller_gone`` when a retried call may follow.

def _retired(request_id: str, *, gone: bool = False) -> dict:
    item = {"event_type": "prompt_retired", "request_id": request_id}
    if gone:
        item["caller_gone"] = True
    return item


@pytest.fixture
def prompt_session(monkeypatch):
    from unittest.mock import AsyncMock
    from auth.path_policy import SecurityContext
    sid = "sess-retire-test"
    monkeypatch.setattr(permission, "verify_session_match_async", AsyncMock(return_value=None))
    session_state._sessions[sid] = {"client_type": "dashboard"}
    session_state._session_security[sid] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True)
    session_state.set_session_mode(sid, "default")
    yield sid
    for table in (session_state._sessions, session_state._session_modes,
                  session_state._session_security, session_state._session_tool_allows,
                  session_state._permission_emitters):
        table.pop(sid, None)


async def _raise(queue, coro):
    task = asyncio.ensure_future(coro)
    prompt = await asyncio.wait_for(queue.get(), 2)
    return task, prompt


@pytest.mark.asyncio
async def test_a_permission_prompt_whose_caller_goes_away_retires_its_card(prompt_session, monkeypatch):
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")
    queue = session_state.get_permission_queue(prompt_session)
    task, prompt = await _raise(queue, permission.decide_tool_permission(
        prompt_session, "mcp__demo__do_thing", {}))
    assert prompt["event_type"] == "permission_prompt"
    task.cancel()  # the hook's stream dropped
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(queue.get(), 1) == _retired(prompt["request_id"], gone=True)


@pytest.mark.asyncio
async def test_a_permission_prompt_that_times_out_retires_its_card(prompt_session, monkeypatch):
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")
    queue = session_state.get_permission_queue(prompt_session)
    task, prompt = await _raise(queue, permission.decide_tool_permission(
        prompt_session, "mcp__demo__do_thing", {}, wait_s=0.05))
    assert (await task)["decision"] == "deny"
    assert await asyncio.wait_for(queue.get(), 1) == _retired(prompt["request_id"])


@pytest.mark.asyncio
async def test_an_answered_permission_prompt_retires_nothing(prompt_session, monkeypatch):
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")
    queue = session_state.get_permission_queue(prompt_session)
    for approved in (False, True):
        task, prompt = await _raise(queue, permission.decide_tool_permission(
            prompt_session, "mcp__demo__do_thing", {}))
        assert session_state.resolve_permission(prompt["request_id"], approved)
        assert (await task)["decision"] == ("allow" if approved else "deny")
        await asyncio.sleep(0)
        assert queue.empty()


@pytest.mark.asyncio
async def test_a_plan_review_whose_caller_goes_away_retires_its_card(prompt_session, monkeypatch):
    session_state.set_session_mode(prompt_session, "plan")
    queue = session_state.get_permission_queue(prompt_session)
    task, prompt = await _raise(queue, permission.decide_tool_permission(
        prompt_session, "ExitPlanMode", {"plan": "the plan"}))
    assert prompt["event_type"] == "plan_review"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(queue.get(), 1) == _retired(prompt["request_id"], gone=True)


@pytest.mark.asyncio
async def test_a_question_whose_wait_ends_unanswered_retires_its_card():
    sid = "sess-retire-question"
    queue = session_state.get_permission_queue(sid)
    questions = [{"id": "q1", "question": "Which one?"}]
    try:
        task, prompt = await _raise(queue, permission.ask_user_question(sid, questions))
        assert prompt["event_type"] == "question_prompt"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(queue.get(), 1) == _retired(prompt["request_id"], gone=True)
        task, prompt = await _raise(queue, permission.ask_user_question(sid, questions, timeout=0.05))
        assert await task == {}
        assert await asyncio.wait_for(queue.get(), 1) == _retired(prompt["request_id"])
        task, prompt = await _raise(queue, permission.ask_user_question(sid, questions))
        assert session_state.resolve_question(prompt["request_id"], {"q1": {"answers": ["a"]}})
        assert await task == {"q1": {"answers": ["a"]}}
        await asyncio.sleep(0)
        assert queue.empty()
    finally:
        session_state._permission_emitters.pop(sid, None)


@pytest.mark.asyncio
async def test_a_prompt_released_by_an_abort_retires_its_card(prompt_session, monkeypatch):
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")
    queue = session_state.get_permission_queue(prompt_session)
    task, prompt = await _raise(queue, permission.decide_tool_permission(
        prompt_session, "mcp__demo__do_thing", {}))
    assert session_state.resolve_session_permissions(prompt_session) == 1
    assert (await task)["decision"] == "deny"
    assert await asyncio.wait_for(queue.get(), 1) == _retired(prompt["request_id"])
    assert session_state._prompt_watch == {}


@pytest.mark.asyncio
async def test_a_wait_no_hook_site_watches_records_nothing():
    """The Direct layer's own waits (no watch) leave no verdict behind."""
    waiter = asyncio.ensure_future(session_state.wait_for_permission("r-direct", "s-direct", 0.01))
    assert await waiter is False
    released = asyncio.ensure_future(session_state.wait_for_permission("r-direct2", "s-direct", 5))
    await asyncio.sleep(0)
    session_state.resolve_session_permissions("s-direct")
    assert await released is False
    assert "r-direct" not in session_state._prompt_watch
    assert "r-direct2" not in session_state._prompt_watch
