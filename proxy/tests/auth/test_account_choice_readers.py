"""Every reader of a session's account agrees on the chat's choice
(``credential_resolver.pick_account``): a person holds two accounts of
each tool, A (the default of the agent's set) and B, and a chat chose B.
On a Shared-only agent a manager lent the agent two accounts of their own
of each tool, C (the default of the agent's own set) and D: a Shared-only
chat that chose D answers D in every reader, its default C, and a task, a
call, a meeting or an outside person's build handed the choice answers C.
The session's MCP build, the gateway's bearer and its reach, the token files
(local and on a machine), the network targets of a per-person credential,
the prompt's account tokens and accounts block, and a session taken back
after a restart through its descriptor all answer B; without the choice,
all of them answer A.

Real rows in the test database (the person, the accounts, the agent's set),
the real resolver and token files in a temporary central store; only the
agent's MCP list is stubbed, with three manifests parsed from JSON: a
per-person account with a network target (Mail X), an OAuth tool that reads
its token file from a directory (Docs X), and a vendor-hosted OAuth tool the
gateway adds the bearer for (Hub X)."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from types import SimpleNamespace

import pytest

import config
from core import placement
from core.credentials import mcp_broker, mcp_gateway
from core.credentials.mcp_gateway import GatewayCredential
from core.remote import mcp_gateway_push
from core.session.session_manager import capabilities_for_path
from services.mcp import (
    account_context,
    builder_executor,
    dynamic_context,
    mcp_manifest_store,
    mcp_registry,
    mcp_session_config,
)
from services.mcp.mcp_manifest_types import AgentContextBlock, AgentContextBuilder
from services.oauth import credential_resolver, oauth_account_store
from services.scheduler import wake_session
from storage import database as task_store
from storage.agents import agent_store
from storage.identity import bearer_allowlist, credential_store, service_account_set
from storage.pg import get_conn

AGENT = "choice-agent"
PERSON = "user-admin"
MAIL, DOCS, HUB = "choice-mail", "choice-docs", "choice-hub"
DOCS_PROVIDER, HUB_PROVIDER = "choice-docs-prov", "choice-hub-prov"
CHOSEN = {MAIL: "B", DOCS: "B", HUB: "B"}
_HOSTS = {k: f"{k.lower()}.mail.test" for k in "ABCD"}
_PASSES = {k: f"pass-of-{k.lower()}" for k in "ABCD"}
_EMAILS = {k: f"{k.lower()}@mail.test" for k in "ABCD"}
_REPOS = {k: f"acme/{k.lower()}-only" for k in "ABCD"}
_OAUTH = {"flows": ["authorization_code"], "authorization_url": "https://auth.choice.test/authorize",
          "token_url": "https://auth.choice.test/token"}
_STDIO = {"runtime": "python", "transport": "stdio", "command": "venv/bin/python", "args": ["server.py"]}
_MANIFESTS = {
    MAIL: {"label": "Mail X", "server": _STDIO,
           "network_targets": [{"source": "per_user_credential", "host_key": "MAILX_HOST", "port_default": 993}],
           "credentials": {"type": "per_user", "label": "Mail X", "fields": [
               {"key": "MAILX_HOST", "label": "Host"},
               {"key": "MAILX_PASS", "label": "Password", "input_type": "password"}]}},
    DOCS: {"label": "Docs X", "server": _STDIO,
           "credentials": {"type": "per_user", "label": "Docs X",
                           "oauth": {**_OAUTH, "provider_id": DOCS_PROVIDER, "mcp_env_injection": ["DOCS_TOKEN"]}}},
    HUB: {"label": "Hub X",
          "server": {"runtime": "none", "transport": "streamable_http", "url_template": "https://hub.choice.test/mcp"},
          "credentials": {"type": "per_user", "label": "Hub X",
                          "oauth": {**_OAUTH, "provider_id": HUB_PROVIDER, "bearer_required": True,
                                    "proposed_hosts": ["hub.choice.test"]}}},
}


@pytest.fixture
def world(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    manifests = []
    for name, body in _MANIFESTS.items():
        folder = tmp_path / "mcps" / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(
            {"name": name, "version": "1.0.0", "description": f"{name} tools.", **body}))
        manifest = mcp_registry._parse_manifest(folder / "manifest.json")
        assert manifest is not None
        monkeypatch.setitem(mcp_manifest_store._manifests, name, manifest)
        manifests.append(manifest)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: list(manifests))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, **kw: list(manifests))
    monkeypatch.setattr(mcp_session_config, "_get_agent_mcps_with_device_exclusions",
                        lambda *a, **k: (list(manifests), {}))
    agent_store.create_agent(AGENT, "Choice Agent")
    username = "chooser"
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, PERSON))
        conn.commit()
    for label in ("A", "B"):
        credential_store.set_user_credentials(
            PERSON, MAIL, {"MAILX_HOST": _HOSTS[label], "MAILX_PASS": _PASSES[label]}, account_label=label)
        credential_store.set_account_display_email(PERSON, MAIL, label, _EMAILS[label])
        for name, provider in ((DOCS, DOCS_PROVIDER), (HUB, HUB_PROVIDER)):
            credential_store.set_user_credentials(PERSON, name, {}, account_label=label)
            folder = oauth_account_store.get_token_dir(username, provider_id=provider)
            (folder / f"{label}.json").write_text(json.dumps(
                {"access_token": f"{name}-token-of-{label}", "expires_at": ""}))
        for name in (MAIL, DOCS, HUB):
            assert credential_store.add_account_to_agent(
                PERSON, name, AGENT, label, repositories=[_REPOS[label]] if name == HUB else None)
    bearer_allowlist.add_allowed(HUB_PROVIDER, "hub.choice.test")
    assert [a.account_label for a in credential_store.list_agent_account_set(PERSON, MAIL, AGENT)] == ["A", "B"]
    return SimpleNamespace(username=username, manifests={m.name: m for m in manifests})


def _which(table: dict[str, str], value: str) -> str:
    return next((label for label, v in table.items() if v == value), f"neither: {value!r}")


def _delivered(bundle) -> str:
    """The account a tool's token is of: what the tool is told and what
    the value is read from."""
    assert bundle.env[mcp_gateway.TOKEN_ACCOUNT_ENV] == bundle.token.token_ref.account_label
    return bundle.token.token_ref.account_label


def _answers(world, choices: dict[str, str] | None) -> dict[str, str]:
    """Which account, A or B, every reader of the session's account answers."""
    kw = {"account_choices": choices} if choices else {}
    out: dict[str, str] = {}

    resolved = credential_resolver.resolve_credentials(AGENT, PERSON, task_scope="user", **kw)
    out["resolve_credentials"] = _which(_HOSTS, resolved.env_by_mcp[MAIL]["MAILX_HOST"])
    _path, _env, reasons, bundles, _keys = mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="user", **kw)
    assert not reasons
    out["session build: the secret"] = _which(_PASSES, bundles[MAIL].env["MAILX_PASS"])
    out["session build: the bearer"] = bundles[HUB].gateway.token_ref.account_label
    cred, reason = mcp_registry.gateway_entry(
        {"type": "http", "url": "https://hub.choice.test/mcp"}, world.manifests[HUB], srv_key=HUB,
        user_sub=PERSON, agent_name=AGENT, task_scope="user", bundle_env={}, **kw)
    assert reason is None
    out["gateway_entry: the token"] = cred.token_ref.account_label
    out["gateway_entry: the reach"] = _which(_REPOS, cred.reach["repositories"][0])

    out["session build: the tool's token"] = _delivered(bundles[DOCS])

    ((host, _port),) = mcp_registry.enumerate_mcp_network_targets(
        world.manifests[MAIL], AGENT, user_sub=PERSON, **kw)
    out["network targets"] = _which(_HOSTS, host)

    tokens = dynamic_context._build_token_map(MAIL, AGENT, PERSON, "", {}, None, choices)
    out["account.label"] = tokens["account.label"]
    out["account.email"] = _which(_EMAILS, tokens["account.email"])
    block = account_context.account_lines(
        AGENT, [account_context.AGENT_CONFIG_MCP, MAIL], user_sub=PERSON, account_choices=choices)
    out["the accounts block"] = re.search(r"Mail X \(`[^`]+`\): this chat uses (\w+) ", block).group(1)

    # A builder block dials the tool's server itself, with the bearer of the
    # account the session runs on.
    dialled: list[dict] = []

    async def _invoke(url, headers, tool_short, args, *, timeout, policy=None):
        dialled.append(dict(headers))
        return json.dumps({"ok": "yes"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builder_executor, "_invoke_quarantined", _invoke)
        mp.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)
        block_def = AgentContextBlock(
            template="hub says ${result.ok}",
            builder=AgentContextBuilder(tool=f"mcp__{HUB}__ping", args={}, timeout_seconds=5))
        rendered = asyncio.run(builder_executor.execute_builder(
            block=block_def, tokens={}, trigger_payload=None, mcp_name=HUB, agent_name=AGENT,
            user_sub=PERSON, **kw))
    assert rendered == "hub says yes"
    (headers,) = dialled
    (bearer,) = [v for v in headers.values() if f"{HUB}-token-of-" in v]
    out["a builder block's bearer"] = bearer.rsplit("-", 1)[-1]

    sid = str(uuid.uuid4())
    static = GatewayCredential(upstream="https://mail.choice.test", path="/mcp", allowlist_key=MAIL,
                               header="X-Mail-Key", prefix="", value="", manifest=MAIL, value_from="MAILX_PASS")
    # As the start records it: the account the build took.
    mcp_gateway.write_descriptor(sid, {MAIL: static}, machine_id="m-1", token_hash="h" * 64, agent=AGENT,
                                 user_sub=PERSON, task_scope="user", **kw,
                                 accounts={MAIL: mcp_gateway.account_text(resolved.accounts.get(MAIL))})
    stored = json.loads((mcp_gateway.descriptor_dir() / f"{sid}.json").read_text())
    assert stored.get("account_choices") == (choices or None)
    async def _offline(machine, msg, **kw):
        raise RuntimeError("not connected")
    try:
        asyncio.run(mcp_gateway_push.reprovision_adopted(
            sid, "m-1", cm=SimpleNamespace(get_connection=lambda machine: None, send_command=_offline,
                               is_compute=lambda machine: False)))
        out["a session taken back"] = _which(_PASSES, mcp_gateway.credentials_of(sid)[MAIL].value)
    finally:
        mcp_broker.purge_session(sid)
    return out


def test_every_reader_answers_the_chosen_account_and_the_default_without_one(world):
    chosen = _answers(world, CHOSEN)
    assert chosen == dict.fromkeys(chosen, "B")
    assert _answers(world, None) == dict.fromkeys(chosen, "A")


# ---------------------------------------------------------------------------
# A Shared-only chat: the agent's own accounts
# ---------------------------------------------------------------------------

LENDER = "test-user-lender-choice"
SERVICE_CHOSEN = {MAIL: "D", DOCS: "D", HUB: "D"}


@pytest.fixture
def shared_world(world):
    """The agent made Shared-only, and a manager of it (not the person at
    the chat) lends it C and D of each tool."""
    agent_store.update_agent(AGENT, collaborative=False, default_scope="agent")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
            "VALUES (%s, 'lender@x.test', 'Lena', 'lender', 'member', 'local', NOW()::text, NOW()::text) "
            "ON CONFLICT (sub) DO NOTHING", (LENDER,))
        conn.commit()
    task_store.add_user_agent(LENDER, AGENT, "manager", "test")
    for label in ("C", "D"):
        credential_store.set_user_credentials(
            LENDER, MAIL, {"MAILX_HOST": _HOSTS[label], "MAILX_PASS": _PASSES[label]}, account_label=label)
        credential_store.set_account_display_email(LENDER, MAIL, label, _EMAILS[label])
        for name, provider in ((DOCS, DOCS_PROVIDER), (HUB, HUB_PROVIDER)):
            credential_store.set_user_credentials(LENDER, name, {}, account_label=label)
            folder = oauth_account_store.get_token_dir("lender", provider_id=provider)
            (folder / f"{label}.json").write_text(json.dumps(
                {"access_token": f"{name}-token-of-{label}", "expires_at": ""}))
        for name in (MAIL, DOCS, HUB):
            assert service_account_set.add_service_account_to_agent(
                name, AGENT, account_label=label, owner_sub=LENDER) == ""
        with get_conn() as conn:
            conn.execute("UPDATE service_agent_bindings SET repositories=%s "
                         "WHERE mcp_name=%s AND agent_name=%s AND account_label=%s",
                         (json.dumps([_REPOS[label]]), HUB, AGENT, label))
            conn.commit()
    yield world
    with get_conn() as conn:
        conn.execute("DELETE FROM service_agent_bindings WHERE account_owner_sub=%s", (LENDER,))
        conn.execute("DELETE FROM users WHERE sub=%s", (LENDER,))
        conn.commit()


def _service_answers(world, choices: dict[str, str] | None) -> dict[str, str]:
    """Which account, C or D, every reader of a Shared-only session's
    account answers (the session runs as the person, at the agent scope)."""
    kw = {"account_choices": choices} if choices else {}
    out: dict[str, str] = {}
    resolved = credential_resolver.resolve_credentials(AGENT, PERSON, task_scope="agent", **kw)
    out["resolve_credentials"] = _which(_HOSTS, resolved.env_by_mcp[MAIL]["MAILX_HOST"])
    _path, _env, reasons, bundles, _keys = mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="agent", **kw)
    assert not reasons
    out["session build: the secret"] = _which(_PASSES, bundles[MAIL].env["MAILX_PASS"])
    out["session build: the bearer"] = bundles[HUB].gateway.token_ref.account_label
    cred, reason = mcp_registry.gateway_entry(
        {"type": "http", "url": "https://hub.choice.test/mcp"}, world.manifests[HUB], srv_key=HUB,
        user_sub=PERSON, agent_name=AGENT, task_scope="agent", bundle_env={}, **kw)
    assert reason is None
    out["gateway_entry: the token"] = cred.token_ref.account_label
    out["gateway_entry: the reach"] = _which(_REPOS, cred.reach["repositories"][0])
    out["session build: the tool's token"] = _delivered(bundles[DOCS])
    ((host, _port),) = mcp_registry.enumerate_mcp_network_targets(
        world.manifests[MAIL], AGENT, user_sub=PERSON, **kw)
    out["network targets"] = _which(_HOSTS, host)
    tokens = dynamic_context._build_token_map(MAIL, AGENT, PERSON, "", {}, None, choices)
    out["account.label"] = tokens["account.label"]
    out["account.email"] = _which(_EMAILS, tokens["account.email"])
    block = account_context.account_lines(
        AGENT, [account_context.AGENT_CONFIG_MCP, MAIL], user_sub=PERSON, account_choices=choices, scope="agent")
    out["the accounts block"] = re.search(r"Mail X \(`[^`]+`\): this chat uses (\w+) ", block).group(1)
    dialled: list[dict] = []

    async def _invoke(url, headers, tool_short, args, *, timeout, policy=None):
        dialled.append(dict(headers))
        return json.dumps({"ok": "yes"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builder_executor, "_invoke_quarantined", _invoke)
        mp.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)
        block_def = AgentContextBlock(
            template="hub says ${result.ok}",
            builder=AgentContextBuilder(tool=f"mcp__{HUB}__ping", args={}, timeout_seconds=5))
        assert asyncio.run(builder_executor.execute_builder(
            block=block_def, tokens={}, trigger_payload=None, mcp_name=HUB, agent_name=AGENT,
            user_sub=PERSON, **kw)) == "hub says yes"
    (headers,) = dialled
    (bearer,) = [v for v in headers.values() if f"{HUB}-token-of-" in v]
    out["a builder block's bearer"] = bearer.rsplit("-", 1)[-1]
    # A session taken back after a restart, through its descriptor.
    sid = str(uuid.uuid4())
    static = GatewayCredential(upstream="https://mail.choice.test", path="/mcp", allowlist_key=MAIL,
                               header="X-Mail-Key", prefix="", value="", manifest=MAIL, value_from="MAILX_PASS")
    # As the start records it: the account the build took.
    mcp_gateway.write_descriptor(sid, {MAIL: static}, machine_id="m-1", token_hash="h" * 64, agent=AGENT,
                                 user_sub=PERSON, task_scope="agent", **kw,
                                 accounts={MAIL: mcp_gateway.account_text(resolved.accounts.get(MAIL))})
    stored = json.loads((mcp_gateway.descriptor_dir() / f"{sid}.json").read_text())
    assert stored.get("account_choices") == (choices or None)

    async def _offline(machine, msg, **kw):
        raise RuntimeError("not connected")
    try:
        asyncio.run(mcp_gateway_push.reprovision_adopted(
            sid, "m-1", cm=SimpleNamespace(get_connection=lambda machine: None, send_command=_offline,
                               is_compute=lambda machine: False)))
        out["a session taken back"] = _which(_PASSES, mcp_gateway.credentials_of(sid)[MAIL].value)
    finally:
        mcp_broker.purge_session(sid)
    return out


@pytest.fixture
def team_world(shared_world):
    """The same lending, on a collaborative agent whose team account is on:
    the person keeps their own mount (user scope) and an editor's role."""
    agent_store.update_agent(AGENT, collaborative=True, default_scope="user", team_account=True)
    task_store.add_user_agent(PERSON, AGENT, "editor", "test")
    return shared_world


def _team_answers(world, choices: dict[str, str] | None, *, role: str = "editor") -> dict[str, str]:
    """Which account, C or D, every reader of a team account's session answers:
    the person at their own mount, the credentials the agent's own."""
    kw = {"account_choices": choices} if choices else {}
    out: dict[str, str] = {}
    _path, _env, reasons, bundles, _keys = mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="user", user_role=role, **kw)
    assert not reasons
    out["session build: the secret"] = _which(_PASSES, bundles[MAIL].env["MAILX_PASS"])
    out["session build: the bearer"] = bundles[HUB].gateway.token_ref.account_label
    out["session build: the tool's token"] = _delivered(bundles[DOCS])
    ((host, _port),) = mcp_registry.enumerate_mcp_network_targets(
        world.manifests[MAIL], AGENT, user_sub=PERSON, **kw)
    out["network targets"] = _which(_HOSTS, host)
    tokens = dynamic_context._build_token_map(MAIL, AGENT, PERSON, "", {}, None, choices)
    out["account.label"] = tokens["account.label"]
    block = account_context.account_lines(
        AGENT, [account_context.AGENT_CONFIG_MCP, MAIL], user_sub=PERSON, account_choices=choices, scope="agent")
    out["the accounts block"] = re.search(r"Mail X \(`[^`]+`\): this chat uses (\w+) ", block).group(1)
    assert "goes out as" not in block
    return out


def test_a_team_accounts_chat_runs_every_reader_on_the_agents_account(team_world):
    """The person's own A and B are never read: the agent's C by default, D
    when the chat chose it; and ``whose_accounts_line`` names nobody."""
    assert account_context.people_use_agents_accounts(AGENT)
    chosen = _team_answers(team_world, SERVICE_CHOSEN)
    assert chosen == dict.fromkeys(chosen, "D")
    assert _team_answers(team_world, None) == dict.fromkeys(chosen, "C")
    assert account_context.whose_accounts_line(AGENT, PERSON, [MAIL, DOCS, HUB]) == ""


def test_below_the_team_accounts_floor_every_per_user_tool_is_left_out(team_world):
    """A viewer on an agent whose floor is editor: no per-user tool loads,
    each left out with the floor's sentence, and none of their own accounts
    is read either."""
    _path, _env, reasons, bundles, _keys = mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="user", user_role="viewer")
    floor = ("The agent's own accounts are for the editor role and above on this agent. "
             "Ask a manager for that role.")
    assert reasons == {MAIL: floor, DOCS: floor, HUB: floor}
    assert not any(name in bundles for name in (MAIL, DOCS, HUB))
    # The switch is off again: the person's own A, at any role.
    agent_store.update_agent(AGENT, team_account=False)
    _path, _env, reasons, bundles, _keys = mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="user", user_role="viewer")
    assert not reasons and _which(_PASSES, bundles[MAIL].env["MAILX_PASS"]) == "A"
    assert "goes out as" in account_context.whose_accounts_line(AGENT, PERSON, [MAIL])


def test_a_shared_only_chat_runs_every_reader_on_the_agents_account_it_chose(shared_world):
    chosen = _service_answers(shared_world, SERVICE_CHOSEN)
    assert chosen == dict.fromkeys(chosen, "D")
    assert _service_answers(shared_world, None) == dict.fromkeys(chosen, "C")


def test_a_session_build_notes_the_agents_own_accounts_it_took(shared_world):
    """What a removal from the agent's set reads: each tool's account of the
    agent's own set the session was built on, the chosen one or the default;
    on a Shared-only agent a build handed a person at their own mount takes
    the agent's too (``account_context.credential_identity``)."""
    from core.session import session_state
    chosen, plain, own = (str(uuid.uuid4()) for _ in range(3))
    mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="agent", account_choices=SERVICE_CHOSEN, session_id=chosen)
    mcp_registry.build_session_mcp_config(AGENT, PERSON, task_scope="agent", session_id=plain)
    mcp_registry.build_session_mcp_config(AGENT, PERSON, session_id=own)
    try:
        assert session_state._built_accounts[chosen] == (
            AGENT, tuple(sorted((name, LENDER, "D") for name in (MAIL, DOCS, HUB))))
        assert session_state._built_accounts[own] == session_state._built_accounts[plain] == (
            AGENT, tuple(sorted((name, LENDER, "C") for name in (MAIL, DOCS, HUB))))
        assert session_state.sessions_built_on(AGENT, MAIL, {(LENDER, "D")}) == [chosen]
        assert session_state.sessions_built_on(AGENT, MAIL, {(LENDER, "C")}) == sorted([plain, own])
    finally:
        for sid in (chosen, plain, own):
            session_state._built_accounts.pop(sid, None)


@pytest.mark.parametrize("build, taken", [
    ({"task_mode": True}, True),
    ({"task_mode": True, "meeting_mode": True}, False),
    ({"phone_mode": True, "shared_chat": True}, False),
    ({"phone_mode": True, "external": True, "shared_chat": True}, False),
    ({"task_mode": True, "chat_worker": True, "shared_chat": True}, True),
], ids=["a task", "a meeting", "a call", "an outside person", "a delegated worker"])
def test_a_call_a_meeting_and_an_outside_person_stay_on_the_default(
        shared_world, monkeypatch, build, taken):
    """Whatever a caller hands in, the MCP build of a call, a meeting or an
    outside person's session resolves with no chat's choice; a chat's build
    takes it, and so do a task's and a delegated worker's (the run's own
    switch, ``chat_reload.schedule_for_run``)."""
    seen: list[dict] = []
    real = credential_resolver.resolve_credentials

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(credential_resolver, "resolve_credentials", spy)
    mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="agent", account_choices=SERVICE_CHOSEN, **build)
    assert ("account_choices" in seen[-1]) is taken
    mcp_registry.build_session_mcp_config(
        AGENT, PERSON, task_scope="agent", account_choices=SERVICE_CHOSEN)
    assert seen[-1]["account_choices"] == SERVICE_CHOSEN


def test_a_choice_whose_lender_left_gives_no_account_never_the_default(shared_world):
    """The chosen account's lender stops managing the agent: the session
    gets nothing for the tool, with its own sentence, not another person's
    mailbox in its place."""
    task_store.set_user_agent_role(LENDER, AGENT, "editor")
    assert credential_resolver.pick_account(MAIL, AGENT, account_choices=SERVICE_CHOSEN) is None
    resolved = credential_resolver.resolve_credentials(
        AGENT, PERSON, task_scope="agent", account_choices=SERVICE_CHOSEN)
    assert MAIL in resolved.excluded_mcps
    assert "the account this chat chose is no longer one of the agent's" in resolved.exclusion_reasons[MAIL]
    assert credential_resolver.pick_account(MAIL, AGENT, account_choices={MAIL: "nowhere"}) is None


# ---------------------------------------------------------------------------
# A wake: a chat continuing with nobody at it (a schedule_continuation, a
# delegate's result), in a session it starts itself
# ---------------------------------------------------------------------------


def _wake_build(scope: str, choices: dict[str, str], kind: str):
    """``wake_session._wake_mcp_build`` as ``_wake_built_config`` calls it: a
    continuation and a delegate's result pass ``kind=""`` through the ladder."""
    return wake_session._wake_mcp_build(
        AGENT, PERSON, username="chooser", role="editor",
        vis=SimpleNamespace(mount_scope=scope), chat_id="", where=placement.LOCAL_PLACEMENT,
        browser=None, mcp_format=capabilities_for_path("claude-code-cli").mcp_config_format,
        session_id=str(uuid.uuid4()), targets=[], kind=kind, account_choices=choices,
    )


@pytest.mark.parametrize("kind", ["", "dashboard"], ids=["a wake", "a resume as the person's turn"])
def test_a_wake_of_a_persons_own_chat_keeps_its_choice(world, kind):
    *_, bundles, _keys = _wake_build("user", CHOSEN, kind)
    assert _which(_PASSES, bundles[MAIL].env["MAILX_PASS"]) == "B"
    assert bundles[HUB].gateway.token_ref.account_label == "B"


@pytest.mark.parametrize("kind", ["", "dashboard"], ids=["a wake", "a resume as the person's turn"])
def test_a_wake_of_a_shared_only_chat_keeps_its_choice(shared_world, kind):
    *_, bundles, _keys = _wake_build("agent", SERVICE_CHOSEN, kind)
    assert _which(_PASSES, bundles[MAIL].env["MAILX_PASS"]) == "D"
    assert bundles[HUB].gateway.token_ref.account_label == "D"


def _wake_session_config(chat_id: str, *, person: str, username: str):
    """The whole config of a session a wake starts for this chat, nothing
    stubbed but the seat it draws."""
    return asyncio.run(wake_session._wake_built_config(
        str(uuid.uuid4()), AGENT, user_sub=person, role="editor", chat_id=chat_id,
        chat_row=task_store.get_chat(chat_id), exec_path="claude-code-cli",
        target=placement.LOCAL, username=username, fresh=True, permission_mode="ask",
        client_type="", woken=True,
    ))


@pytest.fixture
def named_in_the_prompt(monkeypatch):
    """Mail X's prompt block names its account, a seat is at hand, and the
    agent's prompt (no agent folder here) is its dynamic context alone."""
    from services.engines import subscription_pool
    block = AgentContextBlock(template="The mailbox is ${account.email}.", requires=["account.email"])
    monkeypatch.setattr(mcp_manifest_store._manifests[MAIL], "agent_context", [block])
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("sub-walk", {}))
    monkeypatch.setattr(config, "build_agent_prompt", lambda agent, **kw: "\n".join(
        text for _name, text in kw.get("dynamic_contexts") or []))


def _names_one_account(config, account: str) -> None:
    """The session's tools, its prompt and its token files name ``account``."""
    assert _which(_PASSES, config.mcp_secret_bundles[MAIL].env["MAILX_PASS"]) == account
    assert config.mcp_secret_bundles[HUB].gateway.token_ref.account_label == account
    assert f"The mailbox is {_EMAILS[account]}." in config.system_prompt
    assert _delivered(config.mcp_secret_bundles[DOCS]) == account


def test_a_wakes_tools_prompt_and_token_files_name_the_shared_only_chats_account(
        shared_world, named_in_the_prompt):
    from core.session.visibility import shared_chat_owner
    chat = f"chat-{uuid.uuid4().hex[:12]}"
    task_store.create_chat(chat, shared_chat_owner(AGENT), AGENT)
    assert service_account_set.set_chat_service_choice(chat, AGENT, MAIL, "D")
    assert service_account_set.set_chat_service_choice(chat, AGENT, DOCS, "D")
    assert service_account_set.set_chat_service_choice(chat, AGENT, HUB, "D")
    _names_one_account(_wake_session_config(chat, person=PERSON, username="chooser"), "D")
    # With nobody on the delivery, too: the chat's choice needs no person.
    _names_one_account(_wake_session_config(chat, person="", username=""), "D")
    other = f"chat-{uuid.uuid4().hex[:12]}"
    task_store.create_chat(other, shared_chat_owner(AGENT), AGENT)
    _names_one_account(_wake_session_config(other, person=PERSON, username="chooser"), "C")


def test_a_wakes_tools_prompt_and_token_files_name_a_persons_own_chats_account(
        world, named_in_the_prompt):
    chat = f"chat-{uuid.uuid4().hex[:12]}"
    task_store.create_chat(chat, PERSON, AGENT)
    for tool in (MAIL, DOCS, HUB):
        assert credential_store.set_chat_account_choice(chat, PERSON, AGENT, tool, "B")
    _names_one_account(_wake_session_config(chat, person=PERSON, username=world.username), "B")


def test_a_chat_that_chose_nothing_is_not_told_it_chose(shared_world):
    """The default's lender stops managing the agent while other accounts
    stand: a chat with no choice reads that the default is not in use. Only a
    chat that chose reads about what it chose."""
    for label in ("A", "B"):
        assert service_account_set.add_service_account_to_agent(
            MAIL, AGENT, account_label=label, owner_sub=PERSON) == ""
    task_store.set_user_agent_role(LENDER, AGENT, "editor")

    def block(choices):
        return account_context.account_lines(
            AGENT, [account_context.AGENT_CONFIG_MCP, MAIL], user_sub=PERSON, account_choices=choices, scope="agent")

    assert "the agent's default account for it is not in use" in block(None)
    assert "this chat chose" not in block(None)
    assert "the account this chat chose is no longer one of the agent's" in block({MAIL: "D"})
    assert "this chat uses A" in block({MAIL: "A"})
