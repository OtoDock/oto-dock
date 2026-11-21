"""Characterization (golden-master) suite for the hook question path and the
credential hooks (``api/hooks/questions.py``, ``api/hooks/credentials.py``).

Every golden records what the code does today: the question card's items and
frames, ``ask_user_question``'s answers for attended and unattended sessions,
the deny reason of ``decide_tool_permission``, and each credential hook's
answers and refusals through the app. Names are imported from ``permission``,
which keeps them by re-export. A session's kind is set through the session
state's own setter, a chat row is written in the real database, a viewer is a
registered notify queue; nothing global of ``permission`` is patched.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from api.hooks import permission, routing
from app import app
from auth.path_policy import SecurityContext
from auth.session_token import CLI_TOKEN_ENV, attach_cli_token, create_session_token
from core.credentials import mcp_broker
from core.credentials.mcp_broker import SecretBundle
from core.credentials.mcp_gateway import CliCredential, TokenRef
from core.events import chat_writer, stream_pump
from core.session import interactive_session, session_state
from core.session.session_manager import get_layer_by_path
from tests.conftest import live_session_token
from tests.fixtures.characterization.golden_master import check_golden

client = TestClient(app)

QUESTIONS = [
    {"id": "colour", "header": "Colour", "question": "Which colour?",
     "options": [{"label": "Green", "description": "calm"}]},
    {"id": 7, "question": "A numeric id"},
    {"question": "No id at all"},
    "not a mapping",
]


# ---------------------------------------------------------------------------
# The session world: state written through the session state's own surface.
# ---------------------------------------------------------------------------

@pytest.fixture
def sessions():
    """Registers sessions (mode, context, kind) and forgets them after the test."""
    made: list[str] = []

    def _make(sid: str, client_type: str = "dashboard", *, context: bool = True) -> str:
        made.append(sid)
        if context:
            session_state.register_session_state(sid, "default", SecurityContext(
                role="manager", username="alice", agent="pa", is_admin_agent=False))
        if client_type:
            session_state._record_session_use(sid, client_type=client_type, agent="pa")
        return sid

    yield _make
    for sid in made:
        session_state.cleanup_session_permission_state(sid)
        session_state._sessions.pop(sid, None)
        session_state._permission_emitters.pop(sid, None)


@pytest.fixture
def surface(monkeypatch):
    """What a card reaches beyond its queue: a viewer's frames and the notice."""
    from services.notifications import notification_manager
    viewer: asyncio.Queue = asyncio.Queue()
    monkeypatch.setitem(session_state._dashboard_notify_queues, "golden-viewer", viewer)
    pings: list[dict] = []

    async def _ping(user_sub, title, body, chat_id=None, **kw):
        pings.append({"user_sub": user_sub, "title": title, "body": body, "chat_id": chat_id, **kw})

    monkeypatch.setattr(notification_manager, "fire_ephemeral", _ping)
    return SimpleNamespace(viewer=viewer, pings=pings)


def _bind_engine(monkeypatch, path: str | None) -> None:
    """The engine behind a headless session, at the session manager (the seam
    every caller of the authority reads)."""
    layer = get_layer_by_path(path) if path else None
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: layer)


def _drain_queue(queue) -> list:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _queued(sid: str) -> list:
    return _drain_queue(session_state.get_permission_queue(sid))


async def _settled(chat_id: str = "") -> None:
    """The card's row written and its notice sent."""
    from core.background import running
    if chat_id:
        await chat_writer.drain(chat_id, timeout=5)
    loop = asyncio.get_running_loop()
    pending = [t for t in running("engine_prompts") if t.get_loop() is loop]
    if pending:
        await asyncio.wait(pending, timeout=5)


def _chat_rows(chat_id: str) -> list[dict]:
    from storage import database as task_store
    return [{"role": m["role"], "content": m["content"], "event_type": m.get("event_type"),
             "event_data": json.loads(m["event_data"]) if m.get("event_data") else None}
            for m in task_store.get_chat_messages(chat_id)]


class _LivePump:
    """A registered pump reading a permission queue."""

    def __init__(self, queue, chat_id, session_id):
        self.perm_queue = queue
        self.session_id = session_id
        self.chat_id = chat_id
        self.implementing_plan = ""
        self.is_done = False


class _Terminal:
    """A live terminal in the registry: its engine and the dialog it parks on."""

    def __init__(self, session_id, chat_id, execution_path):
        self.session_id = session_id
        self.chat_id = chat_id
        self.execution_path = execution_path
        self.parked: list[list] = []

    def park_on_native_dialog(self, tool_name, *, sent_at=None):
        self.parked.append([tool_name, sent_at])


# ---------------------------------------------------------------------------
# The question path.
# ---------------------------------------------------------------------------

def test_question_shown_reason_words():
    check_golden("questions/shown-reason", {
        tool: permission.question_shown_reason(tool)
        for tool in ("AskUserQuestion", "request_user_input", "request_user_input_async", "")
    })


def test_ask_user_question_by_session_kind(sessions, surface):
    """The answers map and the card, for each kind of session (no chat row:
    the card goes onto the queue)."""
    out = {}

    async def scenario():
        for kind in ("dashboard", "", "unnamed-kind", "phone", "task", "meeting",
                     "trigger", "internal", "app"):
            sid = sessions(f"sess-q-kind-{kind or 'none'}", kind)
            answers = await permission.ask_user_question(sid, QUESTIONS, tool_name="request_user_input")
            out[kind or "<unrecorded>"] = {"answers": answers, "queued": _queued(sid)}
    asyncio.run(scenario())
    out["viewer_frames"] = _drain_queue(surface.viewer)
    out["pings"] = surface.pings
    check_golden("questions/ask-by-kind", out)


def test_ask_user_question_routes_its_card(temp_db, sessions, surface, monkeypatch):
    """Where the card goes: a chat between turns (frame, row, notice), a live
    pump's queue, a meeting's queue and parent chat, an unreadable chat row."""
    out = {}
    agent = "questions-agent"
    temp_db.create_chat("chat-q-idle", "user-admin", agent, "default", model="m")
    temp_db.update_chat("chat-q-idle", session_id="sess-q-idle")
    temp_db.create_chat("chat-q-pump", "user-admin", agent, "default", model="m")
    temp_db.update_chat("chat-q-pump", session_id="sess-q-pump")
    temp_db.create_chat("chat-q-meeting", "user-admin", agent, "default", model="m")
    temp_db.create_chat("chat-q-unattended", "user-admin", agent, "default", model="m")
    temp_db.update_chat("chat-q-unattended", session_id="sess-q-unattended")

    async def scenario():
        one = [{"id": "q1", "question": "Which one?"}]

        sid = sessions("sess-q-idle")
        answers = await permission.ask_user_question(sid, one, tool_name="request_user_input")
        await _settled("chat-q-idle")
        out["between_turns"] = {
            "answers": answers, "queued": _queued(sid), "viewer": _drain_queue(surface.viewer),
            "rows": _chat_rows("chat-q-idle"), "pings": list(surface.pings)}
        surface.pings.clear()

        sid = sessions("sess-q-pump")
        queue = session_state.get_permission_queue(sid)
        monkeypatch.setitem(stream_pump._active_pumps, "chat-q-pump",
                            _LivePump(queue, "chat-q-pump", sid))
        answers = await permission.ask_user_question(sid, one, tool_name="AskUserQuestion")
        out["live_pump"] = {"answers": answers, "queued": _queued(sid),
                            "viewer": _drain_queue(surface.viewer),
                            "rows": _chat_rows("chat-q-pump"), "pings": list(surface.pings)}

        sid = sessions("sess-q-participant")
        session_state.set_meeting_session_info(sid, "sess-q-parent", "meeting-pump-q",
                                               "pa", "chat-q-meeting")
        try:
            answers = await permission.ask_user_question(sid, one, tool_name="AskUserQuestion")
            await _settled("chat-q-meeting")
            out["meeting_participant"] = {
                "answers": answers, "own_queue": _queued(sid),
                "pump_queue": _queued("meeting-pump-q"),
                "viewer": _drain_queue(surface.viewer),
                "rows": _chat_rows("chat-q-meeting"), "pings": list(surface.pings)}
            surface.pings.clear()
        finally:
            session_state._meeting_session_info.pop(sid, None)
            session_state._permission_emitters.pop("meeting-pump-q", None)

        # The chat row cannot be read now: the card goes onto the queue as before.
        from storage import database as task_store
        sid = sessions("sess-q-unreadable")

        def _boom(session_id):
            raise RuntimeError("the row is away")

        monkeypatch.setattr(task_store, "get_chat_by_session", _boom)
        answers = await permission.ask_user_question(sid, one, tool_name="request_user_input")
        out["unreadable_chat"] = {"answers": answers, "queued": _queued(sid),
                                  "viewer": _drain_queue(surface.viewer)}

    asyncio.run(scenario())
    check_golden("questions/ask-routes", out)


def test_show_question_card_items(temp_db, sessions, surface):
    """The card's item, journaled (a terminal's blocking question: the queue
    whatever the chat) and shown (no chat, a chat between turns)."""
    out = {}
    temp_db.create_chat("chat-q-card", "user-admin", "questions-agent", "default", model="m")
    temp_db.update_chat("chat-q-card", session_id="sess-q-card")
    tool_input = {"questions": [{"question": "Pick one", "options": ["a", "b"]}]}

    async def scenario():
        sid = sessions("sess-q-card")
        route = routing.resolve_hook_route(sid)
        await permission.show_question_card(route, sid, "AskUserQuestion", tool_input, journaled=True)
        out["journaled"] = {"queued": _queued(sid), "viewer": _drain_queue(surface.viewer),
                            "rows": _chat_rows("chat-q-card")}
        await permission.show_question_card(route, sid, "AskUserQuestion", tool_input)
        await _settled("chat-q-card")
        out["shown_between_turns"] = {"queued": _queued(sid), "viewer": _drain_queue(surface.viewer),
                                      "rows": _chat_rows("chat-q-card"), "pings": list(surface.pings)}
        surface.pings.clear()

        other = sessions("sess-q-card-no-chat")
        route = routing.HookRoute(queue_session_id="queue-of-another")
        await permission.show_question_card(route, other, "request_user_input_async", tool_input)
        out["no_chat_other_queue"] = {"own_queue": _queued(other), "route_queue": _queued("queue-of-another"),
                                      "viewer": _drain_queue(surface.viewer), "pings": list(surface.pings)}
        session_state._permission_emitters.pop("queue-of-another", None)

    asyncio.run(scenario())
    check_golden("questions/card-items", out)


def test_decide_tool_permission_on_a_question(sessions, surface, monkeypatch):
    """The authority's answer to the engine's question tools: the deny reason
    for a headless session, the run-it answer for a person's terminal."""
    out = {}
    q_input = {"questions": [{"id": "q", "question": "Which?"}]}

    async def _decide(sid, tool):
        decision = await permission.decide_tool_permission(sid, tool, q_input)
        return {"decision": decision, "queued": _queued(sid)}

    async def scenario():
        with monkeypatch.context() as bound:
            for path, tools in (("claude-code-cli", ("AskUserQuestion",)),
                                ("codex-cli", ("request_user_input", "request_user_input_async"))):
                _bind_engine(bound, path)
                for tool in tools:
                    sid = sessions(f"sess-q-headless-{tool}")
                    out[f"headless/{path}/{tool}"] = await _decide(sid, tool)
            _bind_engine(bound, None)
            sid = sessions("sess-q-no-engine")
            out["headless/no-engine/AskUserQuestion"] = await _decide(sid, "AskUserQuestion")
            sid = sessions("sess-q-no-context", context=False)
            out["no-context"] = await _decide(sid, "AskUserQuestion")

        for path, tool, kind in (("claude-code-cli", "AskUserQuestion", "dashboard"),
                                 ("codex-cli", "request_user_input", "dashboard"),
                                 ("codex-cli", "request_user_input_async", "dashboard"),
                                 ("claude-code-cli", "AskUserQuestion", "task"),
                                 ("codex-cli", "request_user_input_async", "task")):
            sid = sessions(f"sess-q-term-{path}-{tool}-{kind}", kind)
            term = _Terminal(sid, f"chat-q-term-{tool}-{kind}", path)
            interactive_session._sessions[sid] = term
            try:
                answer = await _decide(sid, tool)
            finally:
                interactive_session._sessions.pop(sid, None)
            out[f"terminal/{path}/{tool}/{kind}"] = {**answer, "parked": term.parked}
    asyncio.run(scenario())
    check_golden("questions/decide", out)


def test_the_question_routes_through_the_app(sessions, surface):
    """``/v1/hooks/permission`` and ``/v1/hooks/codex-question`` with a live
    session's token: a quick plain JSON answer on each."""
    out = {}
    sid = sessions("sess-q-app")
    token = live_session_token(sid, "pa", "user-admin")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/v1/hooks/permission", headers=headers, json={
        "session_id": sid, "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Which?"}]}})
    out["permission"] = {"status": r.status_code, "body": r.json(), "queued": _queued(sid)}
    r = client.post("/v1/hooks/codex-question", headers=headers, json={
        "session_id": sid, "questions": [{"id": "q1", "question": "Which?"}, {"question": "no id"}]})
    out["codex_question"] = {"status": r.status_code, "body": r.json(), "queued": _queued(sid)}
    r = client.post("/v1/hooks/codex-question", json={"session_id": sid, "questions": []})
    out["codex_question_no_auth"] = {"status": r.status_code, "body": r.json()}
    check_golden("questions/routes", out)


# ---------------------------------------------------------------------------
# The credential hooks, through the app.
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_store():
    mcp_broker._store.clear()
    yield
    mcp_broker._store.clear()


def _answer(response) -> dict:
    return {"status": response.status_code, "body": response.json()}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_mcp_credentials_answers_and_refusals(clean_store):
    from core.credentials.mcp_gateway import GatewayCredential
    mcp_broker.provision("sess-cred-m", {
        "github": SecretBundle(env={"GH_TOKEN": "gh"}, gateway=GatewayCredential(
            upstream="http://localhost:8935", path="/mcp", allowlist_key="github",
            value="ghp_secret", proxy_local=True)),
        "slack": SecretBundle(env={"SLACK_TOKEN": "sk"}),
    })
    url = "/v1/hooks/mcp-credentials"
    expired = jwt.encode({"type": "mcp_cred", "sid": "sess-cred-m", "mcp": "github", "exp": 1},
                         config.JWT_SECRET, algorithm="HS256")
    check_golden("questions/credentials-mcp", {
        "own_mcp": _answer(client.post(url, headers=_bearer(mcp_broker.mint_token("sess-cred-m", "github")))),
        "other_mcp": _answer(client.post(url, headers=_bearer(mcp_broker.mint_token("sess-cred-m", "slack")))),
        "mcp_not_provisioned": _answer(client.post(url, headers=_bearer(mcp_broker.mint_token("sess-cred-m", "nope")))),
        "session_gone": _answer(client.post(url, headers=_bearer(mcp_broker.mint_token("sess-cred-gone", "github")))),
        "no_header": _answer(client.post(url)),
        "not_bearer": _answer(client.post(url, headers={"Authorization": "Basic xyz"})),
        "one_word": _answer(client.post(url, headers={"Authorization": "garbage"})),
        "garbage_token": _answer(client.post(url, headers=_bearer("not-a-token"))),
        "expired_token": _answer(client.post(url, headers=_bearer(expired))),
        "session_token": _answer(client.post(url, headers=_bearer(create_session_token("sess-cred-m", "agent")))),
        "master_key": _answer(client.post(url, headers=_bearer(config.API_KEY))),
    })


def _provision_cli(sid: str, tmp_path, raw: dict | None = None, *, per_command: bool = True) -> CliCredential:
    """A GitHub credential with no placeholder, built per command (a paired
    macOS or Windows computer's) unless ``per_command`` is False."""
    (tmp_path / "acct.json").write_text(json.dumps(
        raw if raw is not None else {"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cred = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"),
                         env_names=("GH_TOKEN", "GITHUB_TOKEN"), manifest="", per_command=per_command)
    mcp_broker.provision(sid, {"github-mcp": SecretBundle(cli=cred)})
    return cred


def _cli_token(sid: str, user_sub: str = "user-admin", *, live: bool = True) -> str:
    """The command-line token a spawn sets beside a (live) session token."""
    token = (live_session_token(sid, "agent", user_sub) if live
             else create_session_token(sid, "agent", user_sub))
    env = {"PROXY_API_KEY": token}
    attach_cli_token(env)
    return env[CLI_TOKEN_ENV]


def _pre_upgrade_session_token(sid: str, user_sub: str = "user-admin") -> str:
    """A session token as 1.8 signed it (the raw secret, no audience)."""
    live_session_token(sid, "agent", user_sub)   # marks the session live
    now = int(time.time())
    return jwt.encode({"type": "session", "sid": sid, "agent": "agent", "user_sub": user_sub,
                       "iat": now, "exp": now + 3600}, config.JWT_SECRET, algorithm="HS256")


def _cli_post(sid: str, token: str | None, host: str = "github.com", **names) -> dict:
    headers = _bearer(token) if token else {}
    return _answer(client.post("/v1/hooks/cli-credential", headers=headers,
                               json={"session_id": sid, "host": host, **names}))


def test_cli_credential_bearers_and_answers(clean_store, tmp_path):
    from auth import tokens
    sid, other, dead = (str(uuid.uuid4()) for _ in range(3))
    _provision_cli(sid, tmp_path)
    cli = _cli_token(sid)
    external = live_session_token(sid, "agent", "", external="phone:+300000000")
    external_claims = {k: v for k, v in tokens.decode(external, tokens.SESSION).items() if k != "aud"}
    grace_sid = str(uuid.uuid4())
    _provision_cli(grace_sid, tmp_path)
    keyless = str(uuid.uuid4())
    _provision_cli(keyless, tmp_path, per_command=False)
    check_golden("questions/credentials-cli-bearers", {
        "neither_placeholder_nor_per_command": _cli_post(keyless, _cli_token(keyless)),
        "cli_token": _cli_post(sid, cli),
        "cli_token_host_any_case": _cli_post(sid, cli, host=" GitHub.com "),
        "agent_scope_session": _cli_post(sid, _cli_token(sid, "")),
        "host_not_connected": _cli_post(sid, cli, host="gitlab.com"),
        "long_host_cut": _cli_post(sid, cli, host="X" * 300),
        "session_with_nothing": _cli_post(other, _cli_token(other)),
        "session_token_of_this_release": _cli_post(sid, live_session_token(sid, "agent", "user-admin")),
        "pre_upgrade_session_token": _cli_post(grace_sid, _pre_upgrade_session_token(grace_sid)),
        "pre_upgrade_token_of_another_session": _cli_post(sid, _pre_upgrade_session_token(other)),
        "another_sessions_cli_token": _cli_post(sid, _cli_token(other)),
        "no_session_id": _cli_post("", cli),
        "master_key": _cli_post(sid, config.API_KEY),
        "capability_token": _cli_post(sid, mcp_broker.mint_token(sid, "github-mcp")),
        "garbage_token": _cli_post(sid, "not-a-token"),
        "no_header": _cli_post(sid, None),
        "not_bearer": _answer(client.post("/v1/hooks/cli-credential",
                                          headers={"Authorization": "Basic xyz"},
                                          json={"session_id": sid, "host": "github.com"})),
        "bearer_no_token": _answer(client.post("/v1/hooks/cli-credential",
                                               headers={"Authorization": "Bearer "},
                                               json={"session_id": sid, "host": "github.com"})),
        "session_not_live": _cli_post(dead, _cli_token(dead, live=False)),
        "holder_gone": _cli_post(sid, _cli_token(sid, "user-nobody-has")),
        "external_caller": _cli_post(sid, tokens.mint(tokens.CLI, external_claims)),
        "body_without_host": _answer(client.post("/v1/hooks/cli-credential", headers=_bearer(cli),
                                                 json={"session_id": sid})),
    })


def test_cli_credential_reach_and_account_refusals(clean_store, tmp_path):
    """A narrowed GitHub reach judges what each command names (``_reach_refusal``);
    an account that needs a reconnect answers 409 with the resolver's words."""
    sid = str(uuid.uuid4())
    cred = _provision_cli(sid, tmp_path)
    cli = _cli_token(sid)
    remotes = ["git@github.com:octo/hello.git"]
    out = {}
    cred.reach = {"repositories": ["octo/hello"], "read_only": True}
    out["read_only_reach"] = {
        "git_own_repo": _cli_post(sid, cli, path="octo/hello.git"),
        "git_other_repo": _cli_post(sid, cli, path="octo/secret.git"),
        "older_helper_names_nothing": _cli_post(sid, cli),
        "git_push_path": _cli_post(sid, cli, path="otodock-read-only/octo/hello.git"),
        "gh_read": _cli_post(sid, cli, command=["pr", "list"], remotes=remotes),
        "gh_write": _cli_post(sid, cli, command=["pr", "create"], remotes=remotes),
        "gh_repo_elsewhere": _cli_post(sid, cli, command=["pr", "list"], remotes=remotes, gh_repo="octo/secret"),
        "gh_host_elsewhere": _cli_post(sid, cli, command=["pr", "list"], gh_repo="octo/hello", gh_host="ghe.example"),
    }
    cred.reach = {"repositories": ["octo/hello"], "read_only": False}
    out["writable_reach"] = {
        "git_own_repo": _cli_post(sid, cli, path="octo/hello.git"),
        "gh_write": _cli_post(sid, cli, command=["pr", "create"], remotes=remotes),
    }
    cred.reach = None
    out["no_reach"] = {"git_anything": _cli_post(sid, cli, path="anyone/anything")}
    (tmp_path / "acct.json").write_text(json.dumps({
        "access_token": "ghp_old", "expires_at": "", "extra": {"refresh_failed": "invalid_grant"}}))
    out["account_needs_reconnect"] = _cli_post(sid, cli)
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_rotated", "expires_at": "", "extra": {}}))
    out["token_file_read_at_the_call"] = _cli_post(sid, cli)
    mcp_broker.purge_session(sid)
    out["purged"] = _cli_post(sid, cli)
    check_golden("questions/credentials-cli-reach", out)


def test_the_hooks_own_refusals_behind_the_middleware(clean_store):
    """The refusals the middleware answers first through the app (the master
    key, a dead session's token): the hooks' own words, by the routes' endpoints
    and with a live session's token."""
    def _endpoint(path, routes):
        for route in routes:
            nested = getattr(route, "original_router", None)
            found = (_endpoint(path, nested.routes) if nested is not None
                     else route.endpoint if getattr(route, "path", "") == path else None)
            if found is not None:
                return found
        return None

    def _refusal(path, *args):
        try:
            return {"answered": asyncio.run(
                _endpoint(path, app.routes)(*args, authorization=f"Bearer {config.API_KEY}"))}
        except HTTPException as e:
            return {"status": e.status_code, "detail": e.detail}

    live = _bearer(live_session_token("sess-cred-live", "agent", "user-admin"))
    check_golden("questions/credentials-own-refusals", {
        "master_key": {
            "mcp": _refusal("/v1/hooks/mcp-credentials"),
            "cli": _refusal("/v1/hooks/cli-credential",
                            SimpleNamespace(session_id="sess-cred-live", host="github.com", request=None)),
        },
        "live_session_token": {
            "mcp": _answer(client.post("/v1/hooks/mcp-credentials", headers=live)),
        },
    })
