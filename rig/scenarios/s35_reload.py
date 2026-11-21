"""An agent reloads its chat's session and gains a tool enabled after the chat began."""
from __future__ import annotations

import json
import uuid

from lib.chat import assistant_text, script, tool_rows
from lib.scenario import check, scenario
from run import ENGINES

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
# A server the chat starts without, and one of its tools that needs no account.
SERVER, TOOL = "triggers-mcp", "list_triggers"
RELOADED = "The session was reloaded"


def _enabled(owner, agent: str) -> list[str]:
    rows = owner.ok("GET", f"/v1/agents/{agent}/mcps")["mcps"]
    return [r["name"] for r in rows if r.get("enabled") and r.get("category") != "skill"]


def _tagged(ctx, tag: str) -> list[dict]:
    return [c for c in ctx.model_calls() if c.get("tag") == tag]


def _server_tools(calls: list[dict]) -> int:
    """How many tools of the server the calls carried or named."""
    loaded = {t for c in calls for t in c.get("tools") or [] if t.startswith(f"mcp__{SERVER}__")}
    return max([len(loaded), *[int((c.get("offered") or {}).get(SERVER, 0)) for c in calls]])


def _note(rows: list[dict]) -> tuple[int, dict]:
    """The chat's note about a reload, and where it sits among the rows."""
    for at, row in enumerate(rows):
        if row.get("event_type") != "system":
            continue
        data = row.get("event_data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                continue
        if isinstance(data, dict) and data.get("subtype") == "session_reloaded":
            return at, data
    return -1, {}


@scenario("reload", needs=("turn-claude",))
def reload(ctx, broken: bool = False):
    """A chat gains a tool by reload_session: none of the server's tools before, its
    tools after, and the note in the chat, which then continues by itself. Broken:
    the second turn does not reload, as a product whose reload did nothing."""
    owner = ctx.owner
    enabled = _enabled(owner, ctx.agent)
    check(SERVER in enabled, f"{SERVER} is not enabled for {ctx.agent}: {enabled}")
    owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": [m for m in enabled if m != SERVER]})
    run = uuid.uuid4().hex[:8]
    chat = None
    try:
        check(SERVER not in _enabled(owner, ctx.agent), f"{SERVER} stayed enabled after it was turned off")
        chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL,
                          text="Before. " + script({"tool": TOOL, "input": {}}, {"text": "Before."}, tag=f"before-{run}"))
        chat.wait_done()
        before = _tagged(ctx, f"before-{run}")
        check(before, "the first turn reached the model server")
        check(_server_tools(before) == 0, f"the first turn already had {_server_tools(before)} tools of {SERVER}")
        owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled})
        if broken:
            second = "Go on. " + script({"text": "Going on."}, tag=f"reload-{run}")
        else:
            second = "Reload. " + script({"tool": "reload_session", "input": {"reason": f"the {SERVER} tools were enabled"}},
                                         {"text": "Reloading."}, tag=f"reload-{run}")
        chat.send(second)
        chat.wait_done()
        rows = chat.wait_rows("the reload's note, and the chat going on after it",
                              lambda r: _note(r)[0] >= 0 and any(x.get("role") == "assistant" for x in r[_note(r)[0]:]),
                              timeout=60 if broken else 240)
        chat_id = chat.chat_id
        chat.close()
        # The dashboard's way back into a chat whose session was replaced.
        chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, chat_id=chat_id,
                          text="After. " + script({"tool": TOOL, "input": {}}, {"text": "Listed."}, tag=f"after-{run}"))
        rows = chat.wait_rows("the turn after the reload", lambda r: "Listed." in assistant_text(r), timeout=240)
    finally:
        if chat is not None:
            chat.close()
        owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled})
    _, note = _note(rows)
    check(str(note.get("message", "")).startswith(RELOADED), f"the note: {note}")
    after = _tagged(ctx, f"after-{run}")
    check(_server_tools(after) > 0, f"the turn after the reload has no tool of {SERVER}")
    listed = tool_rows(rows, TOOL)
    check(listed and not listed[-1].get("is_error"), f"{TOOL} after the reload: {str(listed)[:300]}")
    return f"{SERVER}: 0 tools before, {_server_tools(after)} after; the note: {note.get('message', '')[:120]}"
