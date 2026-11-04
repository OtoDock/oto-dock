"""Settings-MCP: where a setting of the platform lives, what it does and
who changes it, the card a person changes it in, and the cards in the chat.

Two tools over the platform's settings catalog (``GET /v1/settings/catalog``,
the rows the session's person may read): ``find_settings`` scores the rows
by the words of a question, ``get_setting`` answers one row in full and,
for a row the chat can show, asks the platform to draw its card
(``POST /v1/settings/card``). The card reads and saves the value with each
viewer's own sign-in: everyone in the chat sees their own value, no value
ever comes through here, and no tool writes a setting.

Three tools over the cards routes (``POST /v1/cards``, ``POST /v1/cards/list``,
``GET /v1/cards/{id}``): ``show_card`` draws a card with up to three buttons
anyone in the chat may press (a ``say`` button sends its message as the
presser's own words, a ``do`` button runs one action of the platform's
catalog with the presser's own role), ``show_list`` draws a live list the
dashboard fetches for each viewer, ``check_card`` reads who pressed and what
happened. Nothing happens from this process: the platform renders every
card from what it stored and a person's click is the act.

Every call carries the session token the platform minted for this process
(``PROXY_API_KEY``); the platform resolves the person from it. The token
never appears in a tool's answer or a log line. A refusal reaches the agent
only as the routes' own sentence: a settings route's 403, 404 or 409, or a
cards route's 400, 403, 404 or 409, whose detail is a full sentence; any
other answer is "no answer", so no other text of the platform's is repeated
here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.parse
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool, ToolAnnotations

from _tool_args import ToolArgs

logger = logging.getLogger("settings-mcp")

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")
AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")

CATALOG_PATH = "/v1/settings/catalog"
CARD_PATH = "/v1/settings/card"
CARDS_PATH = "/v1/cards"
LISTS_PATH = "/v1/cards/list"

#: How long a fetched catalog serves before it is read again.
CATALOG_TTL_S = 300.0
#: How many rows a search answers.
MAX_MATCHES = 8

NOTHING_MATCHES = "Nothing matches. The platform skill's settings map has every page."
CARD_SHOWN = ("A card with this setting is in the chat. Each person whose role changes it sees their "
              "own value there and changes it. Anyone else sees only who changes it. You never see a value.")
NO_CARD = "Say where the setting is instead."
#: An older satellite refuses both routes, the catalog read first; each tool
#: adds what to do.
OLDER_SATELLITE = "This machine runs an older OtoDock satellite, which does not pass the settings on."
OLDER_SATELLITE_SEARCH = f"{OLDER_SATELLITE} Answer from the platform skill's settings map."
OLDER_SATELLITE_CARD = f"{OLDER_SATELLITE} Say where the setting is from the settings map."
NO_ANSWER = "The platform did not answer. Say where the setting is from the settings map."
#: The cards routes' sentences for the agent.
CARD_DRAWN = ("A card is in the chat. Anyone in the chat may press it, and nothing happens until "
              "someone does. The card's id is {card_id}: check_card reads who pressed it and what happened.")
LIST_DRAWN = ("The list is in the chat, read live for each person who opens it, with their own "
              "sign-in. You do not see its rows: say nothing of them.")
#: Who pressed, when an older platform's state does not say.
SOMEONE = "Someone"
CARDS_NO_ANSWER = ("The platform did not answer. Say what you wanted to show and ask the person "
                   "to do it in the dashboard.")
OLDER_SATELLITE_CARDS = (f"{OLDER_SATELLITE} Say what you wanted to show and ask the person to do it "
                         "in the dashboard, or to open a chat that runs on the platform.")
#: The satellite tunnel's own refusal of a path it does not know.
_NOT_ALLOWLISTED = "path-not-allowlisted"
#: The statuses the settings routes refuse with: a sentence for the person.
_REFUSALS = (403, 404, 409)
#: The cards routes refuse a malformed draw too (a 400 with a sentence).
_CARD_REFUSALS = (400, 403, 404, 409)
#: A button's kinds as the cards route spells them.
KIND_SAY = "say"
KIND_DO = "do"
#: An outcome still running and a say click, as the cards route spells them.
OUTCOME_RUNNING = "running"
CLICK_SAID = "said"
#: The kinds of list the platform draws (services/cards/lists.py).
LIST_KINDS = ("tasks", "triggers", "notifications", "machines", "mcps")

#: An httpx transport a test installs in place of the network.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class _Refused(Exception):
    """The platform refused with a sentence for the person."""

    def __init__(self, sentence: str, status: int = 0, *, older_satellite: bool = False):
        super().__init__(sentence)
        self.sentence = sentence
        self.status = status
        self.older_satellite = older_satellite


class _NoAnswer(Exception):
    """The platform could not be reached or answered no sentence."""


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if AGENT_NAME:
        headers["X-Agent-Name"] = AGENT_NAME
    return headers


def _refusal(resp: httpx.Response, statuses: tuple[int, ...] = _REFUSALS) -> _Refused | None:
    """The settings routes' own refusal (a sentence for the person, ending
    with a full stop), or the older satellite's tunnel refusal; None for
    any other answer, whose text is never repeated."""
    if resp.status_code not in statuses:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("error") == _NOT_ALLOWLISTED:
        return _Refused(OLDER_SATELLITE, resp.status_code, older_satellite=True)
    detail = body.get("detail")
    if isinstance(detail, str) and detail.endswith("."):
        return _Refused(detail, resp.status_code)
    return None


async def _request(method: str, path: str, body: dict | None = None, *,
                   statuses: tuple[int, ...] = _REFUSALS, no_answer: str = NO_ANSWER) -> Any:
    """One call to the platform. A refusal with a sentence raises ``_Refused``
    with that sentence; anything else that is not an answer raises
    ``_NoAnswer``. The error texts carry no part of the request's headers."""
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=TRANSPORT) as client:
            resp = await client.request(method, f"{PROXY_URL}{path}", json=body, headers=_headers())
    except httpx.HTTPError as exc:
        logger.warning("settings-mcp: %s %s failed: %s", method, path, type(exc).__name__)
        raise _NoAnswer(no_answer) from None
    if resp.status_code >= 400:
        logger.info("settings-mcp: %s %s answered %s", method, path, resp.status_code)
        refused = _refusal(resp, statuses)
        if refused is not None:
            raise refused
        raise _NoAnswer(no_answer)
    try:
        return resp.json()
    except ValueError:
        raise _NoAnswer(no_answer) from None


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

_catalog: list[dict] = []
_catalog_read_at = 0.0


async def catalog() -> list[dict]:
    """The rows the session's person may read, fetched once per TTL."""
    global _catalog, _catalog_read_at
    if _catalog and time.monotonic() - _catalog_read_at < CATALOG_TTL_S:
        return _catalog
    data = await _request("GET", CATALOG_PATH)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise _NoAnswer(NO_ANSWER)
    _catalog = [e for e in entries if isinstance(e, dict) and e.get("id")]
    _catalog_read_at = time.monotonic()
    return _catalog


def forget_catalog() -> None:
    global _catalog, _catalog_read_at
    _catalog = []
    _catalog_read_at = 0.0


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 1]


def score(entry: dict, query: str) -> int:
    """How well a row answers the words of ``query``: its label counts most,
    its other names and its place next, its help least; the whole question
    inside the label counts on top."""
    words = _words(query)
    if not words:
        return 0
    label = _words(entry.get("label", ""))
    names = _words(" ".join(entry.get("names") or []))
    place = _words(f"{entry.get('where', '')} {entry.get('group', '')} {entry.get('id', '')}")
    options = _words(" ".join(o.get("label", "") for o in entry.get("options") or []))
    help_words = set(_words(entry.get("help", "")))
    total = 0
    for w in words:
        if w in label:
            total += 3
        if w in names or w in options:
            total += 2
        if w in place:
            total += 2
        if w in help_words:
            total += 1
    if query.strip().lower() in entry.get("label", "").lower():
        total += 5
    return total


def _line(entry: dict) -> str:
    if entry.get("card"):
        card = "A card can show it in the chat."
    elif entry.get("kind") == "readonly":
        card = "No card: the page shows it."
    else:
        card = "No card: the person changes it on the page."
    return (f"- {entry['id']}: {entry.get('label', '')}. Where: {entry.get('where', '')}. "
            f"{entry.get('help', '')} Who changes it: {entry.get('who', '')} {card}")


def _full(entry: dict) -> str:
    # The heading only when it says more than the label, as the card's own
    # line does (the dashboard's settings/where.ts, whereLine).
    group = entry.get("group", "")
    under = f", under {group}" if group and group != entry.get("label", "") else ""
    lines = [
        f"{entry.get('label', '')} ({entry['id']})",
        f"Where: {entry.get('where', '')}{under}.",
        f"What it does: {entry.get('help', '')}",
        f"Who changes it: {entry.get('who', '')}",
    ]
    if entry.get("note"):
        lines.append(f"Note: {entry['note']}")
    if entry.get("options"):
        lines.append("Choices: " + ", ".join(o.get("label", "") for o in entry["options"]) + ".")
    if entry.get("item"):
        lines.append(f"One row per {entry['item']}.")
    if entry.get("names"):
        lines.append("Also named: " + ", ".join(entry["names"]) + ".")
    return "\n".join(lines)


async def find_settings(query: str) -> str:
    try:
        rows = await catalog()
    except _Refused as exc:
        raise _Refused(OLDER_SATELLITE_SEARCH) if exc.older_satellite else exc from None
    scored = sorted(((score(e, query), e) for e in rows), key=lambda t: (-t[0], t[1]["id"]))
    hits = [e for s, e in scored if s > 0][:MAX_MATCHES]
    if not hits:
        return NOTHING_MATCHES
    return "\n".join(_line(e) for e in hits)


async def get_setting(setting_id: str) -> str:
    try:
        rows = await catalog()
    except _Refused as exc:
        raise _Refused(OLDER_SATELLITE_CARD) if exc.older_satellite else exc from None
    entry = next((e for e in rows if e["id"] == setting_id), None)
    if entry is None:
        return f"No setting is called {setting_id}. Search with find_settings first."
    text = _full(entry)
    if not entry.get("card"):
        return text
    try:
        await _request("POST", CARD_PATH, {"session_id": SESSION_ID, "setting": setting_id})
    except _Refused as exc:
        if exc.older_satellite:
            return f"{text}\n{OLDER_SATELLITE_CARD}"
        # A row the catalog said has a card and the route says has none
        # (a catalog read older than the route's): the agent says where.
        tail = f" {NO_CARD}" if exc.status == 409 else ""
        return f"{text}\n{exc.sentence}{tail}"
    except _NoAnswer as exc:
        return f"{text}\n{exc}"
    return f"{text}\n{CARD_SHOWN}"


# ---------------------------------------------------------------------------
# The cards
# ---------------------------------------------------------------------------

def _buttons(raw: Any) -> list[dict]:
    """The buttons as the cards route takes them, each with its own short
    id (the tool's callers never mint one)."""
    out: list[dict] = []
    for i, b in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(b, dict):
            continue
        button: dict[str, Any] = {"id": str(b.get("id") or f"b{i + 1}"), "kind": str(b.get("kind") or "")}
        if button["kind"] == KIND_SAY:
            button["label"] = str(b.get("label") or "").strip()
            button["message"] = str(b.get("message") or "").strip()
        else:
            button["action"] = str(b.get("action") or "")
            button["params"] = b.get("params") if isinstance(b.get("params"), dict) else {}
        out.append(button)
    return out


async def show_card(title: str, text: str, buttons: Any, note: str) -> str:
    body = {"session_id": SESSION_ID, "title": title.strip(), "text": text.strip(),
            "buttons": _buttons(buttons), "note": note.strip()[:1000]}
    try:
        data = await _request("POST", CARDS_PATH, body, statuses=_CARD_REFUSALS, no_answer=CARDS_NO_ANSWER)
    except _Refused as exc:
        if exc.older_satellite:
            return OLDER_SATELLITE_CARDS
        return f"No card was drawn: {exc.sentence}"
    card_id = str((data or {}).get("card_id") or "") if isinstance(data, dict) else ""
    return CARD_DRAWN.format(card_id=card_id)


async def show_list(kind: str, filters: Any) -> str:
    body = {"session_id": SESSION_ID, "kind": kind.strip(),
            "filters": filters if isinstance(filters, dict) else {}}
    try:
        await _request("POST", LISTS_PATH, body, statuses=_CARD_REFUSALS, no_answer=CARDS_NO_ANSWER)
    except _Refused as exc:
        if exc.older_satellite:
            return OLDER_SATELLITE_CARDS
        return f"No list was drawn: {exc.sentence}"
    return LIST_DRAWN


async def check_card(card_id: str) -> str:
    try:
        # The id is the agent's text: quoted, it names one card and no other route.
        s = await _request("GET", f"{CARDS_PATH}/{urllib.parse.quote(card_id.strip(), safe='')}",
                           statuses=_CARD_REFUSALS,
                           no_answer=CARDS_NO_ANSWER)
    except _Refused as exc:
        if exc.older_satellite:
            return OLDER_SATELLITE_CARDS
        return f"The card could not be read: {exc.sentence}"
    if not isinstance(s, dict):
        return CARDS_NO_ANSWER
    if s.get("expired"):
        return f"The card expired: {s['expired']}"
    clicks = [c for c in s.get("clicks") or [] if isinstance(c, dict)]
    by = str(clicks[0].get("by") or SOMEONE) if clicks else SOMEONE
    outcome = s.get("outcome") or {}
    if outcome:
        lines = [f"{by} pressed the button. {outcome.get('text') or ''}".strip()]
        if outcome.get("status") == OUTCOME_RUNNING:
            lines.append("It is still running. Read the card again in a moment.")
        lines.extend(f"- {extra}" for extra in outcome.get("lines") or [])
        return "\n".join(lines)
    said = [c for c in clicks if c.get("status") == CLICK_SAID]
    if said:
        return f"{by} answered: " + ", ".join(str(c.get("detail") or c.get("button")) for c in said)
    return "Nobody has pressed a button yet."


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("settings-mcp")
TOOL_ARGS = ToolArgs()

TOOLS = [
    Tool(
        name="find_settings",
        description=(
            "Finds settings of the OtoDock platform by words: where each lives, what it does, "
            "who changes it and whether the chat can show it as a card. Never a value. Use it "
            "when a person asks where a setting is or how to change something."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The words of the question, like 'wake word' or 'dictation language'."},
            },
            "required": ["query"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    Tool(
        name="get_setting",
        description=(
            "One setting in full by its id (from find_settings). When the chat can show it, "
            "a card with the setting appears in the chat. Each person whose role changes it sees "
            "their own value there and changes it. Anyone else sees only who changes it. You never "
            "see or set a value."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The setting's id, like user.general.wake-word."},
            },
            "required": ["id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    Tool(
        name="show_card",
        description=(
            "Draw a card in the chat: a title, a short text and one to three buttons anyone in the "
            "chat may press. A say button sends its message to you as the words of whoever pressed "
            "it. A do button runs one action of the platform with the presser's own role. The "
            "actions and their params, exactly these keys and no other: hand_files {paths: [..], "
            "dest_dir?, copy?} (files from the person's space to the team's workspace); "
            "promote_memory {topic, new_name?} (a topic of their memory shared with the team); "
            "share_app {app_slug, target_agent, source_visibility?, visibility?} (an app copied to "
            "another agent); update_followed_app {app_slug, mode, visibility?} (an app that follows "
            "another agent's app: mode update puts the source's newer release here, held for approval, "
            "and mode merge unpacks it beside the app for you to merge); send_release_to_source "
            "{app_slug, visibility?} (this app's release onto the app it follows, held for approval "
            "there); attach_library {source_agent, target_agent, subdir?, writable?} (a "
            "library attached to an agent; subdir names a library that is one folder of the source's "
            "knowledge); create_department {name, levels?, mode?, reach?}; set_department "
            "{department_id, mode?, reach?}; move_agent {agent, department_id, level_id}. The "
            "platform writes a do button's words from its "
            "params; your own words go in the text and the note. A refusal names the parameter that "
            "did not fit. Nothing happens by this call, and the card may be ignored. One card "
            "per question, one press per card."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The card's title, a few words. A do button's action writes its own."},
                "text": {"type": "string", "description": "One or two sentences on what the person decides."},
                "buttons": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": [KIND_SAY, KIND_DO]},
                            "label": {"type": "string", "description": "A say button's label, a word or two."},
                            "message": {"type": "string", "description": "A say button's message, sent as the person's own words."},
                            "action": {"type": "string", "description": "A do button's action name."},
                            "params": {"type": "object", "description": "The action's parameters."},
                        },
                        "required": ["kind"],
                    },
                },
                "note": {"type": "string", "description": "Your own note, drawn apart from the platform's words."},
            },
            "required": ["buttons"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="show_list",
        description=(
            "Draw a live list in the chat instead of a paragraph when a person asks what their "
            "scheduled tasks, triggers, notifications, remote machines or MCP servers are. The "
            "dashboard fetches the list with each viewer's own sign-in when the card renders, so "
            "everyone in the chat sees their own rows, you never see them and copy nothing into "
            "text. The row actions (pause, run now, open) are the page's own."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(LIST_KINDS)},
                "filters": {
                    "type": "object",
                    "description": "scope: mine, agent or all; status: active or paused. Tasks, triggers and notifications take them.",
                    "properties": {
                        "scope": {"type": "string", "enum": ["mine", "agent", "all"]},
                        "status": {"type": "string", "enum": ["active", "paused"]},
                    },
                },
            },
            "required": ["kind"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    Tool(
        name="check_card",
        description=(
            "What happened to a card you drew: not yet pressed, who answered a say button and "
            "with what, or who pressed a do button and what the action did (one still running "
            "says so). Read it when someone says they pressed it, never in a loop."
        ),
        inputSchema={
            "type": "object",
            "properties": {"card_id": {"type": "string", "description": "The card's id the draw answered with."}},
            "required": ["card_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_ARGS.listed(TOOLS)


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments, refusal = TOOL_ARGS.check(name, arguments)
    if refusal is not None:
        return refusal
    try:
        if name == "find_settings":
            text = await find_settings(str(arguments.get("query", "")))
        elif name == "get_setting":
            text = await get_setting(str(arguments.get("id", "")))
        elif name == "show_card":
            text = await show_card(str(arguments.get("title", "")), str(arguments.get("text", "")),
                                   arguments.get("buttons"), str(arguments.get("note", "")))
        elif name == "show_list":
            text = await show_list(str(arguments.get("kind", "")), arguments.get("filters"))
        elif name == "check_card":
            text = await check_card(str(arguments.get("card_id", "")))
        else:
            text = f"Unknown tool {name}."
    except (_Refused, _NoAnswer) as exc:
        text = str(exc)
    return [TextContent(type="text", text=text)]


async def main() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
