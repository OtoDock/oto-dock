"""delegation-mcp and where work runs: ``delegate``'s ``machine`` (forwarded in
the spawn body for the worker's runs) and the ``move_chat`` tool, which posts
``/v1/chats/agent-move`` and prints the answer: the places (200), the move
scheduled for the turn's end (202), the refusal's sentence (4xx) or, with no
sentence, what happened and the status."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "delegation-mcp"
_REAL_CLIENT = httpx.AsyncClient


def _mod():
    return load_mcp_server(_MCP_DIR)


def _call(mod, name: str, args: dict) -> str:
    result = asyncio.run(mod.call_tool(name, args))
    return "\n".join(c.text for c in result)


def _fake_proxy(mod, monkeypatch, status: int, payload) -> list[tuple[str, dict]]:
    """The proxy behind the MCP's own HTTP client: every request is recorded
    and answered with ``status`` and ``payload``."""
    seen: list[tuple[str, dict]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(mod.httpx, "AsyncClient",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(_handler), **kw))
    return seen


_PLACES = {
    "current": {"target": "m-1", "label": "laptop"},
    "default_target": "local",
    "targets": [
        {"target": "local", "label": "OtoDock platform", "pairing": "", "state": "",
         "reachable": True, "is_default": True, "pickable": True, "why_not": ""},
        {"target": "m-1", "label": "laptop", "pairing": "user", "state": "online",
         "reachable": True, "is_default": False, "pickable": True, "why_not": ""},
        {"target": "m-2", "label": "desktop", "pairing": "user", "state": "disconnected",
         "reachable": False, "is_default": False, "pickable": False,
         "why_not": "desktop is offline."},
        {"target": "m-3", "label": "rack-1", "pairing": "admin", "state": "online",
         "reachable": True, "is_default": False, "pickable": False,
         "why_not": "rack-1 can't run this chat's engine."},
    ],
}


def test_delegate_takes_a_machine_for_the_workers_runs(monkeypatch):
    mod = _mod()
    tools = {t.name: t for t in asyncio.run(mod.list_tools())}
    prop = tools["delegate"].inputSchema["properties"]["machine"]
    assert prop["type"] == "string" and "worker's runs" in prop["description"]
    assert "continue_id" in prop["description"]
    assert "machine" not in tools["delegate"].inputSchema["required"]
    posted: list[dict] = []

    async def _post(path, body, *, timeout=30.0):
        posted.append(body)
        return {"task_id": "dyn-1", "run_id": "run-1", "agent": mod.AGENT}

    monkeypatch.setattr(mod, "_post", _post)
    _call(mod, "delegate", {"name": "n", "prompt": "p", "surface": "task", "machine": "laptop"})
    assert posted[-1]["machine"] == "laptop"
    _call(mod, "delegate", {"name": "n", "prompt": "p", "surface": "task"})
    assert posted[-1]["machine"] is None


def test_move_chat_is_advertised_with_two_optional_arguments():
    mod = _mod()
    tool = next(t for t in asyncio.run(mod.list_tools()) if t.name == "move_chat")
    assert set(tool.inputSchema["properties"]) == {"machine", "chat_id"}
    assert not tool.inputSchema.get("required")
    assert "End your turn after calling it." in tool.description
    assert "when this turn ends" in tool.description
    # It labels itself a write that changes nothing outside the platform.
    assert tool.annotations is not None and tool.annotations.readOnlyHint is False


def test_without_a_machine_it_lists_the_places_the_current_one_marked(monkeypatch):
    mod = _mod()
    seen = _fake_proxy(mod, monkeypatch, 200, _PLACES)
    text = _call(mod, "move_chat", {})
    assert seen == [("/v1/chats/agent-move", {})]
    lines = text.splitlines()
    assert "  OtoDock platform (id: local) (default)" in lines
    assert "* laptop (id: m-1)" in lines
    assert "  desktop (id: m-2) (offline): desktop is offline." in lines
    assert "  rack-1 (id: m-3): rack-1 can't run this chat's engine." in lines
    # Absent keys are omitted, a present one is forwarded.
    _call(mod, "move_chat", {"chat_id": "chat-2"})
    assert seen[-1] == ("/v1/chats/agent-move", {"chat_id": "chat-2"})


def test_a_scheduled_move_says_it_happens_when_the_turn_ends(monkeypatch):
    mod = _mod()
    seen = _fake_proxy(mod, monkeypatch, 202, {"scheduled": True, "to_label": "laptop",
                                               "memory": "files"})
    text = _call(mod, "move_chat", {"machine": "laptop", "chat_id": "chat-1"})
    assert seen == [("/v1/chats/agent-move", {"machine": "laptop", "chat_id": "chat-1"})]
    assert text == ("The chat moves to laptop when this turn ends, and you continue there on "
                    "your own. End your turn now.")
    _fake_proxy(mod, monkeypatch, 202, {"scheduled": True, "to_label": "OtoDock platform",
                                        "to_phrase": "the OtoDock platform",
                                        "memory": "summary"})
    text = _call(mod, "move_chat", {"machine": "default"})
    assert text == (
        "The chat moves to the OtoDock platform when this turn ends and continues there from "
        "a summary of the conversation. You continue there on your own. End your turn now.")
    _fake_proxy(mod, monkeypatch, 202, {"scheduled": True, "to_label": "laptop",
                                        "memory": "files", "at_turn_end": False})
    assert _call(mod, "move_chat", {"machine": "laptop", "chat_id": "chat-2"}) == (
        "The chat is moving to laptop.")


def test_a_refusal_is_its_sentence(monkeypatch):
    mod = _mod()
    _fake_proxy(mod, monkeypatch, 409, {"detail": "This chat is moving to laptop."})
    assert _call(mod, "move_chat", {"machine": "desktop"}) == "This chat is moving to laptop."
    _fake_proxy(mod, monkeypatch, 422, {"detail": [{"msg": "machine must be a string"}]})
    assert _call(mod, "move_chat", {"machine": "x"}) == "machine must be a string"
    _fake_proxy(mod, monkeypatch, 500, {"detail": "boom"})
    assert _call(mod, "move_chat", {"machine": "x"}).startswith("API error 500")


def test_the_machines_own_refusal_says_why(monkeypatch):
    """On a machine PROXY_URL is the satellite's tunnel, which answers some
    calls itself, ``{"error": <code>}`` with no ``detail``: an older satellite
    refuses the route, a lost link answers 503. Each reads as what happened,
    never a bare "refused", and a listing never reads as a move."""
    mod = _mod()
    _fake_proxy(mod, monkeypatch, 403, {"error": "path-not-allowlisted"})
    assert _call(mod, "move_chat", {"machine": "default"}) == (
        "This machine runs an older satellite, which cannot move the chat from here. Ask the "
        "machine's owner or an admin to update it, or move the chat with the Runs on menu on "
        "its page.")
    assert _call(mod, "move_chat", {}) == (
        "This machine runs an older satellite, which cannot list the places from here. Ask "
        "the machine's owner or an admin to update it, or see them in the Runs on menu on the "
        "chat's page.")
    _fake_proxy(mod, monkeypatch, 503, {"error": "tunnel-not-connected"})
    assert _call(mod, "move_chat", {"machine": "default"}) == (
        "This machine has lost its link to the platform, so the chat did not move. Try again "
        "once the machine is back online.")
    assert _call(mod, "move_chat", {}) == (
        "This machine has lost its link to the platform, so the places could not be listed. "
        "Try again once the machine is back online.")
    _fake_proxy(mod, monkeypatch, 503, {"error": "tunnel-shutting-down"})
    assert _call(mod, "move_chat", {"machine": "default"}) == (
        "This machine's satellite is restarting. Ask again in a moment.")
    for text in (_call(mod, "move_chat", {"machine": "default"}), _call(mod, "move_chat", {})):
        assert "HTTP" not in text and "tunnel" not in text


def test_an_answer_that_may_have_moved_the_chat_says_so(monkeypatch):
    """A call the link dropped after it left, or that got no answer in time,
    may have been accepted: the move then happens when the turn ends, so the
    tool never says it did not."""
    mod = _mod()
    dropped = ("The link to the platform dropped before an answer came back. If the move was "
               "accepted, the chat moves when this turn ends. Otherwise ask again.")
    for status, code in ((502, "tunnel-disconnected"), (502, "upstream-ReadTimeout"),
                         (502, "upstream-ConnectError"), (504, "upstream-timeout"),
                         (500, "dispatch-exception")):
        _fake_proxy(mod, monkeypatch, status, {"error": code})
        assert _call(mod, "move_chat", {"machine": "laptop"}) == dropped, code
        assert _call(mod, "move_chat", {}) == (
            "The link to the platform dropped before an answer came back, so the places could "
            "not be listed. Ask again."), code
    _fake_proxy(mod, monkeypatch, 504, {"error": "tunnel-no-response"})
    assert _call(mod, "move_chat", {"machine": "laptop"}) == (
        "The platform did not answer in time. If the move was accepted, the chat moves when "
        "this turn ends. Otherwise ask again.")
    assert _call(mod, "move_chat", {}) == (
        "The platform did not answer in time, so the places could not be listed. Ask again.")


def test_an_answer_without_a_sentence_names_its_status(monkeypatch, caplog):
    """Any other answer with no sentence names the HTTP status; its code goes
    to the tool's log, never into the words."""
    mod = _mod()
    _fake_proxy(mod, monkeypatch, 503, {"error": "too-many-streams"})
    with caplog.at_level("WARNING", logger="delegation-mcp"):
        assert _call(mod, "move_chat", {"machine": "laptop"}) == (
            "The chat did not move, and no reason came back (HTTP 503).")
    assert "too-many-streams" in caplog.text
    assert _call(mod, "move_chat", {}) == (
        "The places could not be listed, and no reason came back (HTTP 503).")
    for payload in ({}, {"detail": ""}, {"detail": []}, {"detail": None}):
        _fake_proxy(mod, monkeypatch, 404, payload)
        assert _call(mod, "move_chat", {"machine": "laptop"}) == (
            "The chat did not move, and no reason came back (HTTP 404)."), payload
    # A body that is not JSON is the sentence as it came.
    seen: list = []

    def _text(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(403, text="Forbidden")

    monkeypatch.setattr(mod.httpx, "AsyncClient",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(_text), **kw))
    assert _call(mod, "move_chat", {"machine": "laptop"}) == "Forbidden"
    assert seen == ["/v1/chats/agent-move"]


def test_every_tool_is_annotated_and_move_chat_stays_sensitive():
    """The tools' own annotations give their tiers; ``move_chat`` labels
    itself standard, and the manifest's rule keeps it sensitive (a rule beats
    a hint)."""
    from pathlib import Path

    from services.mcp.mcp_permissions import hint_tier
    tools = {t.name: t for t in asyncio.run(_mod().list_tools())}
    tiers = {name: hint_tier(t.annotations.model_dump(exclude_none=True) if t.annotations else None)
             for name, t in tools.items()}
    assert tiers == {
        "delegate": "standard", "send_files": "sensitive", "attach_result_files": "standard",
        "list_sessions": "open", "peek_session": "open", "move_chat": "standard",
        "adopt_project": "standard",
    }
    rules = json.loads(Path(_MCP_DIR, "manifest.json").read_text())["permissions"]["rules"]
    assert {"tool": "move_chat", "tier": "sensitive"} in rules


# Every other tool reads the tunnel's own answer in the same sentences, with
# its own words for what did not happen (and, for a call that may have
# landed, what to do before trying again).
_TOOL_CALLS = {
    "send_files": {"target_agent": "helper", "paths": ["report.md"]},
    "attach_result_files": {"paths": ["report.md"]},
    "delegate": {"name": "n", "prompt": "p", "surface": "task"},
    "list_sessions": {},
    "peek_session": {"chat_id": "chat-1"},
    "adopt_project": {"project_id": "proj-1"},
}
_OUTCOME = {
    "send_files": "the files did not go",
    "attach_result_files": "the files were not attached",
    "delegate": "the work was not handed over",
    "list_sessions": "the sessions could not be listed",
    "peek_session": "the session could not be read",
    "adopt_project": "the project was not adopted",
}
_ACTION = {
    "send_files": "send files", "attach_result_files": "attach files",
    "delegate": "hand work over", "list_sessions": "list the sessions",
    "peek_session": "read a session", "adopt_project": "adopt a project",
}
_MAYBE = {
    "send_files": "The files may still have arrived. Send them again only if the target does "
                  "not have them.",
    "attach_result_files": "Files may still have landed. Name the paths in your report and do "
                           "not attach them again.",
    "delegate": "The work may still have been handed over. Look at list_sessions before you "
                "delegate it again.",
    "adopt_project": "The project may still have been adopted. Ask again to be sure.",
}


def _tool_mod(monkeypatch):
    mod = _mod()
    monkeypatch.setattr(mod, "DELEGATION_TARGETS", ["helper"])
    return mod


@pytest.mark.parametrize("tool", sorted(_TOOL_CALLS))
def test_every_tool_reads_the_machines_own_refusal_in_its_words(monkeypatch, tool):
    mod = _tool_mod(monkeypatch)
    args = _TOOL_CALLS[tool]
    _fake_proxy(mod, monkeypatch, 403, {"error": "path-not-allowlisted"})
    assert _call(mod, tool, args) == (
        f"This machine runs an older satellite, which cannot {_ACTION[tool]} from here. Ask the "
        "machine's owner or an admin to update it.")
    _fake_proxy(mod, monkeypatch, 503, {"error": "tunnel-not-connected"})
    assert _call(mod, tool, args) == (
        f"This machine has lost its link to the platform, so {_OUTCOME[tool]}. Try again once "
        "the machine is back online.")
    _fake_proxy(mod, monkeypatch, 503, {"error": "tunnel-shutting-down"})
    assert _call(mod, tool, args) == "This machine's satellite is restarting. Ask again in a moment."
    _fake_proxy(mod, monkeypatch, 503, {"error": "too-many-streams"})
    outcome = _OUTCOME[tool]
    assert _call(mod, tool, args) == (
        f"{outcome[0].upper()}{outcome[1:]}, and no reason came back (HTTP 503).")


@pytest.mark.parametrize("tool", sorted(_TOOL_CALLS))
def test_every_tool_says_what_a_call_that_may_have_landed_did(monkeypatch, tool):
    """A call the link dropped after it left, or that got no answer in time:
    a write may have landed (its words say what to check), a read is asked
    again."""
    mod = _tool_mod(monkeypatch)
    args = _TOOL_CALLS[tool]
    for status, code, lead in (
            (504, "tunnel-no-response", "The platform did not answer in time"),
            (502, "tunnel-disconnected",
             "The link to the platform dropped before an answer came back"),
            (502, "upstream-ReadTimeout",
             "The link to the platform dropped before an answer came back"),
            (500, "dispatch-exception",
             "The link to the platform dropped before an answer came back")):
        _fake_proxy(mod, monkeypatch, status, {"error": code})
        text = _call(mod, tool, args)
        if tool in _MAYBE:
            assert text == f"{lead}. {_MAYBE[tool]}", (tool, code)
        else:
            assert text == f"{lead}, so {_OUTCOME[tool]}. Ask again.", (tool, code)
        assert "HTTP" not in text and "tunnel" not in text and "upstream" not in text


def test_a_platform_answer_keeps_each_tools_own_words(monkeypatch):
    """The tunnel's sentences read only an answer with no ``detail``: the
    platform's own refusal keeps each tool's wording."""
    mod = _tool_mod(monkeypatch)
    _fake_proxy(mod, monkeypatch, 404, {"detail": "No such file: report.md"})
    assert _call(mod, "send_files", _TOOL_CALLS["send_files"]) == (
        "send_files FAILED — 0 files delivered to 'helper'. No such file: report.md")
    _fake_proxy(mod, monkeypatch, 400, {"detail": "Not a delegated run."})
    assert _call(mod, "attach_result_files", _TOOL_CALLS["attach_result_files"]) == (
        "Nothing attached: Not a delegated run.")
    _fake_proxy(mod, monkeypatch, 403, {"detail": "Not yours."})
    assert _call(mod, "list_sessions", {}) == 'API error 403: {"detail":"Not yours."}'
