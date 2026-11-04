"""settings-mcp: the search's scoring and sentences, the card call with
its session id, the platform's refusals passed on, an older satellite's
refusal named, and the session token never leaving the process.

Run with the proxy venv (has mcp + httpx + pytest):
    proxy/venv/bin/python -m pytest mcps/custom/settings-mcp/tests/ -q
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging

import httpx
import pytest

import server

SENTINEL = "SENTINEL-SESSION-TOKEN-NEVER-SHOWN"

ROWS = [
    {"id": "user.general.wake-word", "label": "Wake word", "names": ["Off", "On"], "group": "Wake word",
     "where": "User Settings, General", "help": "Say Hey OtoDock to start a voice conversation.",
     "who": "Any signed-in person, on their own account.", "card": True, "kind": "toggle"},
    {"id": "user.audio.dictation-language", "label": "Dictation language", "group": "Audio",
     "where": "User Settings, Audio", "help": "The language the mic recognizes.",
     "who": "Any signed-in person, on their own account.", "card": True, "kind": "choice",
     "options": [{"value": "auto", "label": "Automatic"}, {"value": "el-GR", "label": "Greek"}]},
    {"id": "user.general.clear-memory", "label": "Clear my memory across all agents", "group": "Memory",
     "where": "User Settings, General", "help": "Deletes the personal memories agents captured about you.",
     "who": "Any signed-in person, on their own account.", "card": False, "kind": "action"},
]


def _reload(monkeypatch, **env):
    for k in ("PROXY_URL", "PROXY_API_KEY", "OTO_SESSION_ID", "OTO_AGENT_NAME"):
        monkeypatch.delenv(k, raising=False)
    env.setdefault("PROXY_API_KEY", SENTINEL)
    env.setdefault("OTO_SESSION_ID", "sid-1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(server)


class _Proxy:
    """A fake platform: the catalog, and a card route whose answer the test sets."""

    def __init__(self, card=(200, {"status": "ok"}), catalog=(200, {"version": 1, "entries": ROWS})):
        self.card = card
        self.catalog = catalog
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            self.calls.append((request.method, request.url.path, body, dict(request.headers)))
            status, payload = self.catalog if request.url.path.endswith("/catalog") else self.card
            return httpx.Response(status, json=payload)
        return httpx.MockTransport(handle)


def _run(coro):
    return asyncio.run(coro)


def _call(srv, name: str, **args) -> str:
    out = _run(srv.call_tool(name, args))
    # A refusal of the arguments comes back as the SDK's result, an answer
    # as its content list.
    content = getattr(out, "content", out)
    return "".join(c.text for c in content)


def test_the_search_ranks_the_label_first_and_names_the_card(monkeypatch):
    srv = _reload(monkeypatch)
    proxy = _Proxy()
    srv.TRANSPORT = proxy.transport()
    text = _call(srv, "find_settings", query="wake word")
    assert text.startswith("- user.general.wake-word: Wake word. Where: User Settings, General.")
    assert "A card can show it in the chat." in text
    assert "Who changes it: Any signed-in person" in text
    greek = _call(srv, "find_settings", query="dictate in Greek")
    assert greek.startswith("- user.audio.dictation-language")
    memory = _call(srv, "find_settings", query="clear memory")
    assert memory.startswith("- user.general.clear-memory") and "No card" in memory
    assert _call(srv, "find_settings", query="xyzzy") == srv.NOTHING_MATCHES
    # One catalog read serves every search.
    assert [c[1] for c in proxy.calls] == ["/v1/settings/catalog"]


def test_get_setting_names_a_heading_that_says_more_than_the_label(monkeypatch):
    srv = _reload(monkeypatch, OTO_SESSION_ID="sid-42", OTO_AGENT_NAME="pa")
    srv.TRANSPORT = _Proxy().transport()
    text = _call(srv, "get_setting", id="user.audio.dictation-language")
    where = next(line for line in text.splitlines() if line.startswith("Where: "))
    assert where.endswith(", under Audio.")


def test_get_setting_draws_the_card_with_the_session_id(monkeypatch):
    srv = _reload(monkeypatch, OTO_SESSION_ID="sid-42", OTO_AGENT_NAME="pa")
    proxy = _Proxy()
    srv.TRANSPORT = proxy.transport()
    text = _call(srv, "get_setting", id="user.general.wake-word")
    assert text.startswith("Wake word (user.general.wake-word)")
    # A heading that only repeats the label is left out, as on the card.
    assert "Where: User Settings, General.\n" in text
    assert "Also named: Off, On." in text
    assert text.endswith(srv.CARD_SHOWN)
    method, path, body, headers = proxy.calls[-1]
    assert (method, path, body) == ("POST", "/v1/settings/card", {"session_id": "sid-42", "setting": "user.general.wake-word"})
    assert headers["authorization"] == f"Bearer {SENTINEL}" and headers["x-agent-name"] == "pa"


def test_a_row_with_no_card_asks_for_none(monkeypatch):
    srv = _reload(monkeypatch)
    proxy = _Proxy()
    srv.TRANSPORT = proxy.transport()
    text = _call(srv, "get_setting", id="user.general.clear-memory")
    assert "Clear my memory across all agents" in text and srv.CARD_SHOWN not in text
    assert [c[1] for c in proxy.calls] == ["/v1/settings/catalog"]
    assert _call(srv, "get_setting", id="user.nowhere.gone").startswith("No setting is called user.nowhere.gone.")


@pytest.mark.parametrize("status, payload, expected", [
    (403, {"detail": "An administrator changes this setting."}, "An administrator changes this setting."),
    (403, {"detail": "A card shows only in a chat with a person in it."}, "A card shows only in a chat with a person in it."),
    (409, {"detail": "This setting has no card in the chat."}, f"This setting has no card in the chat. {server.NO_CARD}"),
    # Only the settings routes' own sentences are repeated: another route's
    # refusal, a mechanism text and a failure are "no answer".
    (401, {"detail": "Invalid API key"}, server.NO_ANSWER),
    (403, {"detail": "Session token does not match request session_id"}, server.NO_ANSWER),
    (404, {"detail": "Not Found"}, server.NO_ANSWER),
    (422, {"detail": [{"loc": ["body", "setting"], "msg": "field required."}]}, server.NO_ANSWER),
    (500, {"nothing": True}, server.NO_ANSWER),
])
def test_a_refusal_is_passed_on_as_its_sentence(monkeypatch, status, payload, expected):
    srv = _reload(monkeypatch)
    proxy = _Proxy(card=(status, payload))
    srv.TRANSPORT = proxy.transport()
    text = _call(srv, "get_setting", id="user.general.wake-word")
    assert text.endswith(expected)


def test_an_older_satellite_is_named_by_each_tool(monkeypatch):
    """Both routes joined the tunnel in one satellite version, so an older
    one refuses the catalog read first: each tool says so and what to do."""
    srv = _reload(monkeypatch)
    srv.TRANSPORT = _Proxy(catalog=(403, {"error": "path-not-allowlisted"})).transport()
    assert _call(srv, "find_settings", query="wake") == srv.OLDER_SATELLITE_SEARCH
    assert _call(srv, "get_setting", id="user.general.wake-word") == srv.OLDER_SATELLITE_CARD
    assert srv.OLDER_SATELLITE_SEARCH.startswith(srv.OLDER_SATELLITE)
    assert srv.OLDER_SATELLITE_CARD.startswith(srv.OLDER_SATELLITE)


def test_a_catalog_refusal_and_a_dead_platform_answer_sentences(monkeypatch, caplog):
    srv = _reload(monkeypatch)
    srv.TRANSPORT = _Proxy(catalog=(403, {"detail": "No person is signed in here."})).transport()
    assert _call(srv, "find_settings", query="wake") == "No person is signed in here."
    srv.forget_catalog()
    srv.TRANSPORT = _Proxy(catalog=(401, {"detail": "Invalid API key"})).transport()
    assert _call(srv, "find_settings", query="wake") == srv.NO_ANSWER

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)
    srv.TRANSPORT = httpx.MockTransport(dead)
    srv.forget_catalog()
    with caplog.at_level(logging.DEBUG):
        assert _call(srv, "find_settings", query="wake") == srv.NO_ANSWER
    assert SENTINEL not in caplog.text


def test_the_token_never_leaves_the_process(monkeypatch, caplog):
    """Every answer, every refusal and every log line: the token is absent."""
    srv = _reload(monkeypatch)
    outputs = []
    for card in ((200, {"status": "ok"}), (403, {"detail": "Refused."}), (500, {}), (403, {"error": "path-not-allowlisted"})):
        srv.TRANSPORT = _Proxy(card=card).transport()
        srv.forget_catalog()
        with caplog.at_level(logging.DEBUG):
            outputs.append(_call(srv, "find_settings", query="wake word"))
            outputs.append(_call(srv, "get_setting", id="user.general.wake-word"))
    assert all(o for o in outputs)
    assert SENTINEL not in "\n".join(outputs)
    assert SENTINEL not in caplog.text
    # A wrong argument is refused with the argument named, the token absent
    # (the check learns the schemas at the listing, as the engine lists first).
    _run(srv.list_tools())
    refused = _call(srv, "get_setting")
    assert "id" in refused and SENTINEL not in refused


def test_the_tools_are_listed_read_only(monkeypatch):
    srv = _reload(monkeypatch)
    tools = _run(srv.list_tools())
    assert [t.name for t in tools] == ["find_settings", "get_setting", "show_card", "show_list", "check_card"]
    # Every tool reads, but the one that draws a card with buttons: it
    # changes nothing by itself and is never destructive.
    assert all(t.annotations.readOnlyHint for t in tools if t.name != "show_card")
    show = next(t for t in tools if t.name == "show_card")
    assert show.annotations.readOnlyHint is False and show.annotations.destructiveHint is False


# --- the cards tools ----------------------------------------------------------------

class _Cards:
    """A fake platform for the cards routes: the answer per path."""

    def __init__(self, draw=(200, {"status": "ok", "card_id": "card-1"}), lst=(200, {"status": "ok"}),
                 read=(200, {"card_id": "card-1", "clicks": [], "outcome": None, "expired": None})):
        self.by_path = {"/v1/cards": draw, "/v1/cards/list": lst}
        self.read = read
        self.calls: list[tuple[str, str, dict | None]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            self.calls.append((request.method, request.url.path, body))
            status, payload = self.by_path.get(request.url.path, self.read)
            return httpx.Response(status, json=payload)
        return httpx.MockTransport(handle)


def test_show_card_posts_the_words_and_the_buttons_with_their_ids(monkeypatch):
    srv = _reload(monkeypatch, OTO_SESSION_ID="sid-42")
    cards = _Cards()
    srv.TRANSPORT = cards.transport()
    text = _call(srv, "show_card", title="Hand over?", text="The report reads final.", note="My view.",
                 buttons=[{"kind": "do", "action": "hand_files", "params": {"paths": ["report.md"]}},
                          {"kind": "say", "label": "Not yet", "message": "Not yet, I am still writing."}])
    assert text == srv.CARD_DRAWN.format(card_id="card-1")
    (method, path, body), = cards.calls
    assert (method, path) == ("POST", "/v1/cards")
    assert body == {"session_id": "sid-42", "title": "Hand over?", "text": "The report reads final.",
                    "note": "My view.",
                    "buttons": [{"id": "b1", "kind": "do", "action": "hand_files", "params": {"paths": ["report.md"]}},
                                {"id": "b2", "kind": "say", "label": "Not yet", "message": "Not yet, I am still writing."}]}


def test_show_list_posts_the_kind_and_filters_and_says_nothing_of_the_rows(monkeypatch):
    srv = _reload(monkeypatch, OTO_SESSION_ID="sid-42")
    cards = _Cards()
    srv.TRANSPORT = cards.transport()
    assert _call(srv, "show_list", kind="tasks", filters={"scope": "mine"}) == srv.LIST_DRAWN
    assert cards.calls == [("POST", "/v1/cards/list", {"session_id": "sid-42", "kind": "tasks", "filters": {"scope": "mine"}})]
    srv.TRANSPORT = _Cards(lst=(404, {"detail": "There is no list of that kind."})).transport()
    assert _call(srv, "show_list", kind="inbox") == "No list was drawn: There is no list of that kind."


@pytest.mark.parametrize("status, payload, expected", [
    (400, {"detail": "A card needs one to three buttons."}, "No card was drawn: A card needs one to three buttons."),
    (403, {"detail": "A card shows only in a chat with a person in it."},
     "No card was drawn: A card shows only in a chat with a person in it."),
    (409, {"detail": "Only contributors, editors and managers hand files to the team."},
     "No card was drawn: Only contributors, editors and managers hand files to the team."),
    (403, {"error": "path-not-allowlisted"}, server.OLDER_SATELLITE_CARDS),
    (401, {"detail": "Invalid API key"}, server.CARDS_NO_ANSWER),
    (422, {"detail": [{"loc": ["body", "buttons"], "msg": "field required."}]}, server.CARDS_NO_ANSWER),
])
def test_a_cards_refusal_is_passed_on_as_its_sentence(monkeypatch, status, payload, expected):
    srv = _reload(monkeypatch)
    srv.TRANSPORT = _Cards(draw=(status, payload)).transport()
    assert _call(srv, "show_card", buttons=[{"kind": "say", "label": "Yes", "message": "Yes."}]) == expected


def test_check_card_reads_who_pressed_and_what_happened(monkeypatch):
    srv = _reload(monkeypatch)
    srv.TRANSPORT = _Cards().transport()
    assert _call(srv, "check_card", card_id="card-1") == "Nobody has pressed a button yet."
    srv.TRANSPORT = _Cards(read=(200, {"clicks": [{"button": "b2", "status": "said", "detail": "Not yet.", "by": "Maria"}],
                                      "outcome": None, "expired": None})).transport()
    assert _call(srv, "check_card", card_id="card-1") == "Maria answered: Not yet."
    srv.TRANSPORT = _Cards(read=(200, {"clicks": [{"button": "go", "status": "done", "detail": "", "by": "Nikos"}],
                                      "outcome": {"status": "done", "text": "2 files are in the team's workspace."},
                                      "expired": None})).transport()
    assert _call(srv, "check_card", card_id="card-1") == "Nikos pressed the button. 2 files are in the team's workspace."
    # An older platform's state names nobody.
    srv.TRANSPORT = _Cards(read=(200, {"clicks": [{"button": "b2", "status": "said", "detail": "Yes."}],
                                      "outcome": None, "expired": None})).transport()
    assert _call(srv, "check_card", card_id="card-1") == "Someone answered: Yes."
    srv.TRANSPORT = _Cards(read=(200, {"clicks": [], "outcome": None, "expired": "This card has expired. Ask again."})).transport()
    assert _call(srv, "check_card", card_id="card-1") == "The card expired: This card has expired. Ask again."
    srv.TRANSPORT = _Cards(read=(404, {"detail": "No such card."})).transport()
    assert _call(srv, "check_card", card_id="card-9") == "The card could not be read: No such card."


def test_the_cards_tools_never_show_the_token(monkeypatch, caplog):
    srv = _reload(monkeypatch)
    srv.TRANSPORT = _Cards(draw=(500, {"boom": SENTINEL})).transport()
    with caplog.at_level(logging.DEBUG, logger="settings-mcp"):
        text = _call(srv, "show_card", buttons=[{"kind": "say", "label": "Yes", "message": "Yes."}])
    assert text == srv.CARDS_NO_ANSWER and SENTINEL not in text and SENTINEL not in caplog.text


def test_check_card_quotes_the_id_so_it_reaches_no_other_route(monkeypatch):
    srv = _reload(monkeypatch)
    seen: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        return httpx.Response(200, json={"clicks": [], "outcome": None, "expired": None})

    srv.TRANSPORT = httpx.MockTransport(handle)
    _call(srv, "check_card", card_id="../settings/catalog")
    # The path on the wire: the id stays one segment under /v1/cards/.
    assert seen == [b"/v1/cards/..%2Fsettings%2Fcatalog"]


def test_the_tools_say_a_card_is_everyone_s_in_the_chat():
    """A card acts for whoever presses it: no tool text tells the agent a
    card is the asker's alone."""
    texts = [t.description for t in server.TOOLS] + [server.CARD_SHOWN, server.CARD_DRAWN, server.LIST_DRAWN]
    joined = " ".join(texts)
    assert "own chat" not in joined and "the person presses" not in joined
    assert "Each person whose role changes it sees their own value" in server.CARD_SHOWN
    # A person below the setting's role sees no value: the agent never says they do.
    assert "Anyone else sees only who changes it." in server.CARD_SHOWN
    show_card = next(t for t in server.TOOLS if t.name == "show_card")
    assert "anyone in the chat may press" in show_card.description
    assert "presser's own role" in show_card.description
