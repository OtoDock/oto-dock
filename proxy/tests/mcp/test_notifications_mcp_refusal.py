"""notifications-mcp: a call the platform refuses reads as plain sentences.

The agent reads what was refused, then the platform's own sentence for why
and what to do, never the raw answer (``{"detail": ...}``), a status code or
the URL it called. Each tool is called through ``call_tool`` with the
platform's answer faked on the module's own client.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "notifications-mcp"

# The platform's sentence for a press that may not use the tool, as the walk
# of lane APPS-TOOLS met it.
_PRESS = ("This button cannot use that tool when you press it. On a personal app, ask its owner. "
          "On a shared app, ask an editor of the agent or an admin.")


def _call(name: str, args: dict, answer: httpx.Response) -> str:
    mod = load_mcp_server(_MCP_DIR)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/session/current":
            return httpx.Response(200, json={})
        return answer

    async def run() -> str:
        mod._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await mod.call_tool(name, args)
        finally:
            await mod._client.aclose()
        return "\n".join(c.text for c in result)

    return asyncio.run(run())


def _refused(status: int, detail) -> httpx.Response:
    return httpx.Response(status, content=json.dumps({"detail": detail}).encode(),
                          headers={"content-type": "application/json"})


_CALLS = [
    ("create_notification", {"title": "Stand-up", "body": "In five minutes."},
     "The notification was not created."),
    ("list_notifications", {}, "The notifications could not be listed."),
    ("cancel_notification", {"id": "7"}, "The notification was not deleted."),
    ("pause_notification", {"id": "7"}, "The notification was not paused."),
    ("resume_notification", {"id": "7"}, "The notification was not resumed."),
    ("edit_notification", {"id": "7", "title": "Stand-up"}, "The notification was not changed."),
]


def _plain(text: str) -> None:
    for code in ("{", "}", "detail", "403", "HTTP", "http://", "Error", "Failed", ";", "—"):
        assert code not in text, f"{code!r} in {text!r}"


@pytest.mark.parametrize(("name", "args", "refused"), _CALLS)
def test_a_refusal_reads_what_was_refused_then_the_platforms_sentence(name, args, refused):
    text = _call(name, args, _refused(403, _PRESS))
    assert text == f"{refused} {_PRESS}"
    _plain(text)


@pytest.mark.parametrize(("name", "args", "refused"), _CALLS)
def test_a_reason_without_its_full_stop_gets_one(name, args, refused):
    text = _call(name, args, _refused(
        403, "Agent-scoped notifications require editor, manager, or admin role for this agent"))
    assert text == (f"{refused} Agent-scoped notifications require editor, manager, "
                    "or admin role for this agent.")


@pytest.mark.parametrize(("name", "args", "refused"), _CALLS)
def test_a_failure_with_no_words_says_to_try_again(name, args, refused):
    text = _call(name, args, httpx.Response(502, text="<html>Bad Gateway</html>"))
    assert text == f"{refused} The platform could not finish it. Try again in a moment."
    _plain(text)


def test_a_refusal_with_no_reason_says_so():
    text = _call("create_notification", {"title": "Stand-up", "body": "Soon."},
                 httpx.Response(400, text="nope"))
    assert text == ("The notification was not created. The platform gave no reason. "
                    "Tell the person it was refused.")


def test_a_value_the_platform_did_not_accept_names_no_schema():
    text = _call("create_notification", {"title": "Stand-up", "body": "Soon."},
                 _refused(422, [{"loc": ["body", "run_at"], "msg": "Input should be a valid string",
                                 "type": "string_type"}]))
    assert text == ("The notification was not created. One of the values was not accepted. "
                    "Check them and call again.")
    _plain(text)


@pytest.mark.parametrize("name", ["cancel_notification", "pause_notification",
                                  "resume_notification", "edit_notification"])
def test_an_unknown_notification_is_still_named_by_its_id(name):
    args = {"id": "7", "title": "x"} if name == "edit_notification" else {"id": "7"}
    assert _call(name, args, _refused(404, "Notification not found")) == "Notification 7 not found."
