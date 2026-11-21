"""The prompt's block of the accounts a chat may switch between
(``services/mcp/account_context.py``): one line for each tool whose set of
accounts for the person and the agent holds more than one, the account the
chat uses named first, and no block at all for a set of one, a session
without the ``switch_account`` tool, a Shared-only agent, a session with no
person or an engine a choice does not reach. What a session takes of a
chat's choices (``session_choices``), and which sessions the builders show
the block to. The store and the resolver are stubbed at their boundary;
every reader agreeing on one account against a real database is
``tests/auth/test_account_choice_readers.py``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.session import visibility
from services.mcp import account_context, dynamic_context, mcp_manifest_store
from services.mcp.mcp_registry import CredentialConfig, McpManifest, ServerConfig
from services.oauth import credential_resolver, oauth_account_store
from storage.identity import credential_store

AGENT = "acct-agent"
USER = "u-acct"
MAIL = "acct-mail"
DRIVE = "acct-drive"
KEYS = "acct-keys"
CONFIG = account_context.AGENT_CONFIG_MCP
SECRET = "the-password-of-the-test"
TOKEN = "the-bearer-of-the-test"
_TAIL = "To switch this chat, ask the person, then call `switch_account`."


def _manifest(name: str, label: str, cred_type: str = "per_user") -> McpManifest:
    return McpManifest(
        name=name, label=label, description="", version="1.0.0", category="community",
        server=ServerConfig(runtime="python", transport="stdio", command="", args=[]),
        credentials=CredentialConfig(type=cred_type), config=[], env={}, agent_env={},
        exclude_from=[], skills=[],
    )


@pytest.fixture
def world(monkeypatch):
    """Mail X: a set of two (``work``, the default, and ``home@x.example``,
    whose label is its address). Drive X: a set of one. Keys X: an infra
    tool with rows that are no person's account. Every credential read
    answers a secret, so a block that read one would show it."""
    sets = {MAIL: ["work", "home@x.example"], DRIVE: ["solo"], KEYS: ["k1", "k2"]}
    addresses = {MAIL: {"work": "me@work.example", "home@x.example": "home@x.example"},
                 DRIVE: {"solo": "solo@x.example"}}
    for m in (_manifest(MAIL, "Mail X"), _manifest(DRIVE, "Drive X"), _manifest(KEYS, "Keys X", "infra")):
        monkeypatch.setitem(mcp_manifest_store._manifests, m.name, m)

    def account_set(user_sub, mcp_name, agent_name):
        if (user_sub, agent_name) != (USER, AGENT):
            return []
        return [credential_store.AccountAgentBinding(label, (), False, i == 0)
                for i, label in enumerate(sets.get(mcp_name, []))]

    def pick(mcp_name, agent_name, *, user_sub="", only_owner=None, account_choices=None):
        labels = sets.get(mcp_name, [])
        chosen = (account_choices or {}).get(mcp_name)
        label = chosen if chosen in labels else (labels[0] if labels else None)
        return credential_resolver.AccountRef(label=label, owner_sub=user_sub) if label else None

    monkeypatch.setattr(credential_store, "list_agent_account_set", account_set)
    monkeypatch.setattr(credential_store, "list_user_accounts", lambda user_sub, mcp_name: [
        {"account_label": label, "display_email": email, "is_default": False, "created_at": "", "meta": {}}
        for label, email in addresses.get(mcp_name, {}).items()])
    monkeypatch.setattr(credential_store, "get_user_credentials", lambda *a, **k: {"MAILX_PASS": SECRET})
    monkeypatch.setattr(oauth_account_store, "read_account_token", lambda *a, **k: {"access_token": TOKEN})
    monkeypatch.setattr(credential_resolver, "pick_account", pick)
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent_name: False)
    from storage import database as task_store
    task_store.upsert_user(USER, "ann@x.example", "ann", "member", display_name="Ann")
    return SimpleNamespace(sets=sets)


CLI = "claude-code-cli"


def _lines(mcps, *, user_sub=USER, choices=None) -> str:
    return account_context.account_lines(AGENT, mcps, user_sub=user_sub, account_choices=choices)


WHOSE = ("The accounts in this chat are Ann's own: Mail X on work (me@work.example), "
         "Drive X on solo (solo@x.example). What you send through them goes out as Ann.")


def test_a_set_of_two_gets_one_line_with_the_default_in_use(world):
    """The block opens with whose accounts the chat runs on (every tool
    that is a person's account with one picked, an infra tool never), then
    one switch line per set of two."""
    assert _lines([CONFIG, MAIL, DRIVE, KEYS]) == "\n".join([
        "## Accounts you may switch to", "", WHOSE, "",
        f"- Mail X (`{MAIL}`): this chat uses work (me@work.example). You may also use: home@x.example. " + _TAIL,
    ])


def test_whose_accounts_stands_alone_when_the_chat_cannot_switch(world):
    """No switch tool, or no set of two: the sentence under its own heading;
    in a run it names the run. Nothing with no tool that has an account."""
    assert _lines([MAIL, DRIVE, KEYS]) == "\n".join([account_context.WHOSE_HEADING, "", WHOSE])
    assert _lines([CONFIG, DRIVE]) == "\n".join([
        account_context.WHOSE_HEADING, "",
        "The accounts in this chat are Ann's own: Drive X on solo (solo@x.example). "
        "What you send through them goes out as Ann."])
    assert account_context.account_lines(AGENT, [MAIL], user_sub=USER, scope="", run=True).startswith(
        f"{account_context.WHOSE_HEADING}\n\nThe accounts in this run are Ann's own: Mail X on work")
    assert _lines([CONFIG, KEYS]) == ""


def test_the_chosen_account_is_the_one_in_use(world):
    text = _lines([CONFIG, MAIL], choices={MAIL: "home@x.example"})
    assert text.endswith(
        f"- Mail X (`{MAIL}`): this chat uses home@x.example. You may also use: work (me@work.example). " + _TAIL)
    # A choice the set does not hold leaves the default in use.
    assert "this chat uses work (me@work.example)" in _lines([CONFIG, MAIL], choices={MAIL: "gone"})


@pytest.mark.parametrize("mcps, user_sub, shared_only", [
    ([CONFIG, KEYS], USER, False),         # no tool that is a person's account
    ([CONFIG, MAIL], USER, True),          # a Shared-only agent: the agent's own accounts
    ([CONFIG, MAIL], "", False),           # no person
])
def test_no_block_at_all(world, monkeypatch, mcps, user_sub, shared_only):
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent_name: shared_only)
    assert _lines(mcps, user_sub=user_sub) == ""


def test_a_team_account_runs_the_chat_on_the_agents_accounts(world, monkeypatch):
    """A collaborative agent whose team account is on: the person's own
    block says nothing (their accounts are not in use) and no sentence
    names them; below the floor the identity carries the floor's sentence."""
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Acct")
    agent_store.update_agent(AGENT, team_account=True, team_account_floor="editor")
    assert account_context.people_use_agents_accounts(AGENT)
    assert _lines([CONFIG, MAIL, DRIVE]) == ""
    assert account_context.reader_scope(AGENT, USER) == ("agent", "")
    editor = account_context.credential_identity(AGENT, USER, mount_scope="user", role="editor")
    viewer = account_context.credential_identity(AGENT, USER, mount_scope="user", role="viewer")
    assert (editor.sub, editor.scope, editor.refusal) == (None, "agent", "")
    assert (viewer.sub, viewer.scope) == (None, "agent")
    assert viewer.refusal == ("The agent's own accounts are for the editor role and above on this "
                              "agent. Ask a manager for that role.")
    assert account_context.credential_identity(AGENT, USER, mount_scope="user", role="").refusal
    agent_store.update_agent(AGENT, team_account=False)
    own = account_context.credential_identity(AGENT, USER, mount_scope="user", role="viewer")
    assert (own.sub, own.scope, own.refusal) == (USER, "user", "")
    assert account_context.credential_identity(AGENT, "", mount_scope="user", role="viewer").scope == "agent"
    assert account_context.credential_identity(AGENT, USER, mount_scope="agent", role="viewer").scope == "agent"


def test_never_a_token_or_a_secret(world):
    for choices in (None, {MAIL: "home@x.example"}):
        text = _lines([CONFIG, MAIL, DRIVE, KEYS], choices=choices)
        assert text and SECRET not in text and TOKEN not in text


def test_a_store_failure_leaves_the_block_out(world, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(credential_store, "list_agent_account_set", broken)
    assert _lines([CONFIG, MAIL]) == ""


def test_a_session_takes_the_choices_its_engine_can_honour(world, monkeypatch):
    from storage import database as task_store
    task_store.create_chat("c-1", USER, AGENT, "default")
    stored = {MAIL: "home@x.example", DRIVE: "solo"}
    monkeypatch.setattr(credential_store, "get_chat_account_choices",
                        lambda chat_id, user_sub: dict(stored) if (chat_id, user_sub) == ("c-1", USER) else {})

    def takes(chat="c-1", person=USER):
        return account_context.session_choices(chat, person)

    assert takes() == stored
    # None on an engine that builds its own tool config, for nobody, for no chat.
    assert takes(person="") == {} and takes(chat="") == {}


def test_the_dynamic_contexts_take_the_choice_and_carry_no_block(world, monkeypatch):
    """``get_dynamic_contexts`` hands the choice to the tokens, the builder
    blocks and the reach lines, never to the Python providers. The block is
    the builders' to append, under their own condition."""
    seen = {}

    def provider(agent_name, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setitem(dynamic_context._providers, CONFIG, provider)
    out = asyncio.run(dynamic_context.get_dynamic_contexts(
        AGENT, [CONFIG, MAIL], user_sub=USER, account_choices={MAIL: "home@x.example"}))
    assert out == []
    assert "account_choices" not in seen and seen["user_sub"] == USER


def test_the_chat_builder_shows_the_block_to_a_persons_own_dashboard_chat_only(world):
    """A signed-in person's call and a task's chat reopened in the dashboard
    cannot switch (the reload refuses both), so they are told of no other
    account. The same on an engine a choice does not reach."""
    from core.config import config_builder

    def block(**over):
        kw = {"switchable": True, **over}
        out = asyncio.run(config_builder._with_accounts_block(
            [("other", "text")], AGENT, [CONFIG, MAIL], USER, {}, **kw))
        return [name for name, _text in out]

    assert block() == ["other", CONFIG]
    # Not switchable: the block still names whose accounts the chat runs on.
    out = asyncio.run(config_builder._with_accounts_block(
        [("other", "text")], AGENT, [CONFIG, MAIL], USER, {}, switchable=False))
    assert [name for name, _t in out] == ["other", CONFIG]
    assert out[1][1].startswith(account_context.WHOSE_HEADING)


def test_the_builders_show_the_block_where_the_chat_can_switch(temp_db, monkeypatch, tmp_path):
    """Through the real chat builder: a person's own dashboard chat gets the
    block, with the choices its session takes. A task's chat reopened in the
    dashboard, a signed-in person's call and a build with no chat get none
    (the reload refuses a switch in each)."""
    from core.config.config_builder import build_agent_config
    from core.config.phone_config_builder import build_phone_agent_config
    from core.config.task_config_builder import TaskIdentity
    from services.phone.phone_identity import EXTERNAL_ROUTE_ROLE, RouteIdentity
    from storage import database as task_store
    from tests._compute import serve
    from tests.fixtures import loop_builder_rig as rig_mod
    rig = rig_mod.install(monkeypatch, tmp_path)
    # A compute machine serves the platform's place: every build here lands on it.
    serve(monkeypatch)
    asked: list = []

    def lines(agent_name, assigned, **kw):
        asked.append(kw)
        return "BLOCK"

    monkeypatch.setattr(account_context, "account_lines", lines)
    monkeypatch.setattr(account_context, "session_choices",
                        lambda chat_id, person, **kw: {"char-files": "home"} if chat_id == "c-own" else {})
    task_store.create_chat("c-own", "user-viewer", rig_mod.AGENT, "default")
    person = {"username": "vic", "display_name": "Vic", "email": "v@x", "role": "member"}

    def blocks(build) -> list:
        rig.calls.clear()
        asyncio.run(build())
        [prompt] = [c for c in rig.calls if c["call"] == "build_agent_prompt"]
        return [text for name, text in (prompt["kwargs"].get("dynamic_contexts") or [])
                if name == CONFIG]

    def chat(**kw):
        return lambda: build_agent_config(
            rig_mod.AGENT, person, "user-viewer", "editor", client_type="dashboard",
            session_id="s-1", **kw)

    assert blocks(chat(chat_id="c-own")) == ["BLOCK"]
    assert asked == [{"user_sub": "user-viewer", "account_choices": {"char-files": "home"},
                      "scope": "user", "run": False}]
    asked.clear()
    # Every other build asks for the block with no scope: whose accounts the
    # session runs on is still said, a switch is offered to none of them.
    assert blocks(chat()) == ["BLOCK"]
    ident = TaskIdentity(username="vic", role="editor", scope="user", creds_user_sub="user-viewer")
    assert blocks(chat(chat_id="c-own", task_identity=ident)) == ["BLOCK"]
    route = RouteIdentity(mode="user", role=EXTERNAL_ROUTE_ROLE, external=None, user=person | {
        "sub": "user-viewer"}, user_role="editor", caller_claim="phone:char")
    assert blocks(lambda: build_phone_agent_config(
        rig_mod.AGENT, call_type="inbound", route_identity=route, session_id="s-call")) == ["BLOCK"]
    assert [kw["scope"] for kw in asked] == ["", "", ""]
    assert all(kw["account_choices"] == {} for kw in asked)


def test_the_delivery_builder_shows_the_block_to_a_dashboard_turn_only(monkeypatch):
    """A wake finds nobody at the chat and can switch nothing. The move's
    resume and a key's turn are turns of the person's own dashboard chat."""
    from core import placement
    from services.mcp import mcp_registry
    from services.scheduler import wake_session

    async def no_contexts(*a, **k):
        return [("other", "text")]

    monkeypatch.setattr(dynamic_context, "get_dynamic_contexts", no_contexts)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
    monkeypatch.setattr(dynamic_context, "build_delegation_roster", lambda *a: None)
    monkeypatch.setattr(dynamic_context, "build_meetings_access", lambda *a: None)
    asked: list = []

    def lines(agent_name, assigned, **kw):
        asked.append(kw)
        return "BLOCK"

    monkeypatch.setattr(account_context, "account_lines", lines)

    def contexts(**kw):
        return asyncio.run(wake_session._wake_dynamic_contexts(
            AGENT, {}, user_sub=USER, role="editor", targets=[],
            where=placement.LOCAL_PLACEMENT, **kw))

    # The block is asked for every wake (whose accounts the session runs
    # on), with the chat's scope only where the chat can switch.
    assert contexts() == [("other", "text"), (CONFIG, "BLOCK")]
    assert contexts(switchable=True, exec_path=CLI) == [("other", "text"), (CONFIG, "BLOCK")]
    assert contexts(kind="task") == [("other", "text"), (CONFIG, "BLOCK")]
    assert [(kw["scope"], kw["run"]) for kw in asked] == [("", False), ("user", False), ("", True)]



# ---------------------------------------------------------------------------
# A Shared-only chat: which chat chooses, and the agent's own lines
# ---------------------------------------------------------------------------


def test_which_chat_chooses_and_whose_choices_a_session_takes(temp_db, monkeypatch):
    """A person's own chat chooses among their accounts; a session in the
    agent's space of the agent's pool chat chooses among the agent's own, in
    every mode (a collaborative agent's pool chat woken with no person
    included), and so does a person's chat on a Shared-only agent; a
    worker's chat, a task's, a call's, a person's chat on a collaborative
    agent at the agent's mount and no chat at all take none."""
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.identity import service_account_set
    agent_store.create_agent("pool-ac", "Pool")
    agent_store.update_agent("pool-ac", collaborative=False, default_scope="agent")
    agent_store.create_agent("team-ac", "Team")
    pool = visibility.shared_chat_owner("pool-ac")
    task_store.create_chat("c-pool", pool, "pool-ac")
    task_store.create_chat("c-mine", "user-viewer", "team-ac")
    task_store.create_chat("c-worker", pool, "pool-ac", parent_chat_id="c-pool")
    task_store.create_chat("c-task-src", pool, "pool-ac", source_type="task")
    task_store.create_chat("c-call", visibility.PHONE_CHAT_OWNER, "pool-ac")
    task_store.create_chat("c-team-pool", visibility.shared_chat_owner("team-ac"), "team-ac")
    task_store.create_chat("c-before", "user-viewer", "pool-ac")
    scope = account_context.choice_scope
    assert scope("c-pool", mount_scope="agent") == "agent"
    assert scope("c-team-pool", mount_scope="agent") == "agent"
    assert scope("c-before", mount_scope="agent") == "agent"
    assert scope("c-mine", mount_scope="user") == "user"
    assert scope("c-team-pool", mount_scope="user") == "user"
    for chat in ("c-worker", "c-task-src", "c-call", "c-mine", "task-run-1", "c-gone", ""):
        assert scope(chat, mount_scope="agent") == "", chat

    monkeypatch.setattr(service_account_set, "get_chat_service_choices",
                        lambda chat_id: {MAIL: "d"} if chat_id == "c-pool" else {})
    monkeypatch.setattr(credential_store, "get_chat_account_choices",
                        lambda chat_id, user_sub: {MAIL: "mine"})

    def takes(chat, mount, person=USER):
        return account_context.session_choices(chat, person, mount_scope=mount)

    assert takes("c-pool", "agent") == {MAIL: "d"}
    # The chat's choice, whoever drives it, and with no person at all.
    assert takes("c-pool", "agent", person="") == {MAIL: "d"}
    assert takes("c-mine", "user") == {MAIL: "mine"}
    assert takes("c-worker", "agent") == {} and takes("c-call", "agent") == {}


def test_a_shared_only_chat_is_told_the_agents_own_accounts(world, monkeypatch):
    """The agent's own set of two, lent by two people: the line names the
    account the chat uses, the other, that the switch is for everyone in
    the chat and that tasks stay on the default. A set of one gets no line,
    a person's own set is never read, and a choice whose account left the
    set names none in use."""
    from storage.identity import service_account_set
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent_name: True)
    own = {MAIL: [("c@x.example", "lender-1"), ("d-label", "lender-2")], DRIVE: [("solo", "lender-1")]}
    monkeypatch.setattr(service_account_set, "list_service_agent_set", lambda mcp, agent: [
        credential_store.ServiceBinding(label, owner, is_default=i == 0)
        for i, (label, owner) in enumerate(own.get(mcp, []))])
    monkeypatch.setattr(credential_store, "list_user_accounts", lambda user_sub, mcp_name: [
        {"account_label": label, "display_email": {"d-label": "d@x.example"}.get(label, label)}
        for label, owner in own.get(mcp_name, []) if owner == user_sub])

    def pick(mcp_name, agent_name, *, user_sub="", only_owner=None, account_choices=None):
        assert not user_sub
        labels = [label for label, _o in own.get(mcp_name, [])]
        chosen = (account_choices or {}).get(mcp_name)
        if chosen:
            return credential_resolver.AccountRef(label=chosen, owner_sub="x") if chosen in labels else None
        return credential_resolver.AccountRef(label=labels[0], owner_sub="x") if labels else None

    monkeypatch.setattr(credential_resolver, "pick_account", pick)

    def lines(choices=None, scope="agent"):
        return account_context.account_lines(
            AGENT, [CONFIG, MAIL, DRIVE], user_sub=USER, account_choices=choices,
            scope=scope)

    block = lines()
    assert block.startswith(account_context.HEADING)
    assert ("Mail X (`acct-mail`): this chat uses c@x.example. You may also use: "
            "d-label (d@x.example). These are the agent's own accounts, and a switch is for "
            "everyone in this chat.") in block
    assert "Drive X" not in block and SECRET not in block
    # A run's line: its switch lands in its next turn, no person to ask.
    run = account_context.account_lines(
        AGENT, [CONFIG, MAIL], user_sub="", scope="agent", run=True)
    assert ("Mail X (`acct-mail`): this run uses c@x.example. You may also use: d-label (d@x.example). "
            "These are the agent's own accounts. To switch this run to another account, call "
            "`switch_account` and finish your turn: the run goes on with it from your next round, "
            "in the same chat. Only these accounts are offered, and a run that drives a terminal "
            "cannot switch while it runs.") in run
    assert "ask the person" not in run
    assert "this chat uses d-label (d@x.example)" in lines({MAIL: "d-label"})
    assert "the account this chat chose is no longer one of the agent's" in lines({MAIL: "gone"})
    # A person's own block on a Shared-only agent, and no scope: nothing.
    assert lines(scope="user") == "" and lines(scope="") == ""
