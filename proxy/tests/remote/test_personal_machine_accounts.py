"""A shared chat on a person's own computer carries what is that person's
(``config_builder.personal_machine_bundles``): the machine's own-browser token
when the session runs as the machine's owner, and an agent's service
binding only when the account it lends is that owner's. Every other
binding, an admin's instance settings and a key stay on the platform.

Real rows in the test database (users, accounts, bindings), the real
resolver and the real session MCP build; only the agent's MCP list is
stubbed, with two manifests parsed from JSON."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core import placement
from core.config.config_builder import personal_machine_bundles, personal_owner_of
from core.session.visibility import SCOPE_AGENT, SCOPE_USER
from services.mcp import mcp_registry
from services.oauth import credential_resolver
from storage.identity import credential_store
from storage.pg import get_conn
from storage.remote_store import BrowserTargetSettings

AGENT = "pm-agent"
TOKEN = "T" * 43
_MAIL = {
    "label": "Mail X", "version": "1.0.0",
    "server": {"runtime": "python", "transport": "stdio", "command": "python3", "args": ["-c", "pass"]},
    "credentials": {"type": "per_user", "label": "Mail X", "service_account": True,
                    "fields": [{"key": "MAILX_USER", "label": "User", "input_type": "text"},
                               {"key": "MAILX_PASS", "label": "Password", "input_type": "password"}]},
}
_BROWSER = {
    "label": "Local Browser", "version": "1.0.0", "server_name": mcp_registry.BROWSER_SERVER_KEY,
    "server": {"runtime": "node", "transport": "stdio", "command": "node", "args": ["index.js"]},
    "credentials": {"type": "none"},
}


def _manifest(tmp_path, name: str, body: dict):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"name": name, **body}))
    m = mcp_registry._parse_manifest(folder / "manifest.json")
    assert m is not None
    return m


def _user(request, role: str = "admin") -> str:
    sub = f"pm-{uuid.uuid4().hex[:12]}"
    username = f"pm{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'Test', %s, %s, 'local', NOW()::text, NOW()::text)",
            (sub, f"{username}@example.test", username, role))
        conn.commit()

    def _gone():
        with get_conn() as conn:
            conn.execute("DELETE FROM service_agent_bindings WHERE account_owner_sub = %s", (sub,))
            conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
            conn.commit()
    request.addfinalizer(_gone)
    return sub


def _lend(owner: str, mcp: str, secret: str) -> None:
    """``owner`` connects an account for ``mcp`` and binds it as the agent's."""
    credential_store.set_user_credentials(owner, mcp, {"MAILX_USER": owner, "MAILX_PASS": secret},
                                          account_label="acct")
    assert credential_store.set_service_agent_binding(mcp, AGENT, account_label="acct",
                                                      owner_sub=owner, set_by=owner)


@pytest.fixture
def world(temp_db, tmp_path, monkeypatch, request):
    """The owner of the computer (an admin, so they manage the agent),
    another manager, and an MCP bound to each one's own account."""
    owner, other = _user(request), _user(request)
    mine, theirs = f"pm-mine-{uuid.uuid4().hex[:6]}", f"pm-theirs-{uuid.uuid4().hex[:6]}"
    manifests = [_manifest(tmp_path, mine, _MAIL), _manifest(tmp_path, theirs, _MAIL),
                 _manifest(tmp_path, "pm-browser", _BROWSER)]
    for m in manifests:
        monkeypatch.setitem(mcp_registry._manifests, m.name, m)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: list(manifests))
    monkeypatch.setattr(mcp_registry, "_get_agent_mcps_with_device_exclusions",
                        lambda *a, **k: (list(manifests), {}))
    monkeypatch.setattr(mcp_registry.config, "SESSIONS_DIR", tmp_path / "sessions")
    _lend(owner, mine, "owner-secret-1")
    _lend(other, theirs, "other-secret-2")
    return SimpleNamespace(owner=owner, other=other, mine=mine, theirs=theirs)


def _personal(owner: str):
    target = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m-personal")
    browser = BrowserTargetSettings(mode="own", extension_token=TOKEN, owner_sub=owner)
    return target, browser


def _build(where, browser, *, scope=SCOPE_AGENT, person=None, **kw):
    return mcp_registry.build_session_mcp_config(
        AGENT, person, task_scope=scope, placement=where, target_browser=browser, **kw)


def _everything(path, flat_env, bundles) -> str:
    return json.dumps([path.read_text() if path else "", flat_env,
                       {k: [b.env, repr(b.gateway), repr(b.cli)] for k, b in bundles.items()}])


def test_the_owner_at_the_chat_carries_their_own_account_and_the_browser_token(world):
    target, browser = _personal(world.owner)
    path, flat, reasons, bundles, _ = _build(target, browser, person=world.owner,
                                             personal_owner=world.owner)
    kept = personal_machine_bundles(bundles, target, "", AGENT, who="t", owner=world.owner)
    assert kept[world.mine].env["MAILX_PASS"] == "owner-secret-1"
    assert kept[mcp_registry.BROWSER_SERVER_KEY].env == {mcp_registry.BROWSER_TOKEN_ENV: TOKEN}
    assert world.theirs not in kept and world.theirs not in bundles
    assert "another person connected" in reasons[world.theirs]
    # Nothing of the other person's account is even read into the build.
    assert "other-secret-2" not in _everything(path, flat, bundles)
    cfg = json.loads(path.read_text())["mcpServers"]
    assert world.mine in cfg and world.theirs not in cfg
    env = cfg[mcp_registry.BROWSER_SERVER_KEY]["env"]
    assert env["OTO_BROWSER_TOKEN_EXPECTED"] == "1" and "OTO_BROWSER_TOKEN_WITHHELD" not in env


def test_a_build_with_no_owner_at_the_chat_reads_nobodys_account(world):
    """An agent-scope build on a personal machine whose person is not its
    owner (or a builder with no person): the filter matches nobody, the
    token is withheld and the wrapper is told so."""
    target, browser = _personal(world.owner)
    path, flat, reasons, bundles, _ = _build(target, browser, person=world.other,
                                             personal_owner="")
    assert world.mine not in bundles and world.theirs not in bundles
    assert mcp_registry.BROWSER_SERVER_KEY not in bundles
    blob = _everything(path, flat, bundles)
    assert "owner-secret-1" not in blob and "other-secret-2" not in blob and TOKEN not in blob
    env = json.loads(path.read_text())["mcpServers"][mcp_registry.BROWSER_SERVER_KEY]["env"]
    assert env["OTO_BROWSER_TOKEN_WITHHELD"] == "1" and "OTO_BROWSER_TOKEN_EXPECTED" not in env
    assert personal_machine_bundles({"x": object()}, target, "", AGENT, who="t") == {}


def test_a_demoted_owner_lends_nothing(world):
    """The binding stands only while its owner manages the agent (the
    resolver's liveness re-check): demoted, their account stays behind."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = 'member' WHERE sub = %s", (world.owner,))
        conn.commit()
    target, browser = _personal(world.owner)
    path, flat, reasons, bundles, _ = _build(target, browser, person=world.owner,
                                             personal_owner=world.owner)
    assert world.mine not in bundles and "owner-secret-1" not in _everything(path, flat, bundles)
    assert world.mine in reasons


def test_an_admin_machine_and_the_platform_keep_todays_rule(world):
    admin = placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m-admin")
    for where in (admin, placement.LOCAL_PLACEMENT):
        _p, _f, reasons, bundles, _ = _build(where, None, person=world.owner)
        assert bundles[world.mine].env["MAILX_PASS"] == "owner-secret-1"
        assert bundles[world.theirs].env["MAILX_PASS"] == "other-secret-2"
        assert world.theirs not in reasons
    # A person's own chat on their computer: their own scope, unfiltered.
    target, browser = _personal(world.owner)
    _p, _f, reasons, bundles, _ = _build(target, browser, scope=SCOPE_USER, person=world.owner)
    assert world.theirs not in bundles and mcp_registry.BROWSER_SERVER_KEY in bundles


def test_a_rebind_between_two_reads_hands_over_nothing_of_the_new_account(world, monkeypatch):
    """Every read of the binding in the build passes the owner filter: a
    manager rebinding the owner's MCP to their own account while the build
    runs leaves the MCP out, never with the other account."""
    real = credential_store.get_service_agent_binding
    calls = {"n": 0}

    def rebound(mcp, agent):
        b = real(mcp, agent)
        if mcp == world.mine and b is not None:
            calls["n"] += 1
            if calls["n"] > 1:
                return b._replace(account_owner_sub=world.other)
        return b
    monkeypatch.setattr(credential_store, "get_service_agent_binding", rebound)
    result = credential_resolver.resolve_credentials(AGENT, world.owner, task_scope=SCOPE_AGENT,
                                                     only_owner=world.owner)
    assert calls["n"] >= 2 and world.mine not in result.env_by_mcp
    with pytest.raises(credential_resolver.CredentialUnavailable):
        credential_resolver.binding_reach(world.mine, AGENT, user_sub=world.owner,
                                          task_scope=SCOPE_AGENT, only_owner=world.owner)
    assert credential_resolver.pick_account(world.mine, AGENT, only_owner=world.owner) is None


def test_what_is_not_one_persons_account_stays_off_the_machine(monkeypatch):
    """The classification: an admin's instance, a sign-in file, a header
    key and an infra credential never go, whoever's binding it is."""
    def m(name, **creds):
        base = {"type": "per_user", "label": name, "api_key_header": None, "oauth": None}
        return SimpleNamespace(name=name, label=name, instances=None,
                               credentials=SimpleNamespace(**{**base, **creds}))
    files = {"filed"}
    monkeypatch.setattr(mcp_registry, "get_credentials_dirs",
                        lambda name: [("DIR", "x")] if name in files else [])
    assert credential_resolver.carries_to_personal_machine(m("plain"))
    instance = m("inst")
    instance.instances = SimpleNamespace(delivery="env")
    assert not credential_resolver.carries_to_personal_machine(instance)
    assert not credential_resolver.carries_to_personal_machine(m("keyed", api_key_header={"name": "X"}))
    assert not credential_resolver.carries_to_personal_machine(m("infra", type="infra"))
    assert not credential_resolver.carries_to_personal_machine(m("filed"))
    assert "file that stays" in credential_resolver.personal_withheld_reason(m("filed"), AGENT, "o")
    assert "key that stays" in credential_resolver.personal_withheld_reason(
        m("keyed", api_key_header={"name": "X"}), AGENT, "o")


_HUB = {
    "label": "Hub X", "version": "1.0.0",
    "server": {"transport": "streamable_http", "url_template": "https://hub.example.test/mcp/",
               "source": "remote:hub.example.test"},
    "credentials": {"type": "per_user", "label": "Hub X", "service_account": True, "oauth": {
        "provider_id": "hubx", "flows": ["personal_access_token"], "bearer_required": True,
        "proposed_hosts": ["hub.example.test"],
        "token_format": {"schema": "generic_oauth_v1", "filename_pattern": "{account_label}.json"},
        "env_injection": ["HUBX_TOKEN"],
        "git_credential_helper": {"host": "hub.example.test", "helper": "!x"}}},
}


@pytest.mark.parametrize("flip_after,reason", [
    ("the resolver's reads", "no account is connected for this session"),
    ("the gateway's token read", "changed while the session started"),
])
def test_a_rebind_while_the_session_builds_leaves_the_mcp_out(world, tmp_path, monkeypatch,
                                                              flip_after, reason):
    """The race end to end: the resolver reads the owner's binding, then a
    manager rebinds the agent to their own account before the gateway's
    reads. The session build leaves the MCP out with no gateway credential
    and nothing of either account in a bundle."""
    from core.config import deployment
    from services.oauth import oauth_account_store
    from storage import database as task_store
    from storage.identity import bearer_allowlist
    hub = _manifest(tmp_path, "pm-hub", _HUB)
    monkeypatch.setitem(mcp_registry._manifests, hub.name, hub)
    tokens = tmp_path / "tokens"
    monkeypatch.setattr(oauth_account_store, "get_token_dir",
                        lambda username, *, provider_id: (tokens / username).mkdir(parents=True, exist_ok=True)
                        or tokens / username)
    monkeypatch.setattr(bearer_allowlist, "is_host_allowed", lambda key, host: True)
    monkeypatch.setattr(deployment, "is_proxy_local_mcp_host", lambda host, manifest: False)
    for who in (world.owner, world.other):
        credential_store.set_user_credentials(who, hub.name, {"x": "y"}, account_label="acct")
        name = task_store.get_username_by_sub(who)
        (tokens / name).mkdir(parents=True, exist_ok=True)
        (tokens / name / "acct.json").write_text(json.dumps({
            "provider": "hubx", "account_id": "acct", "access_token": f"tok-of-{name}",
            "expires_at": "2099-01-01T00:00:00Z"}))
    assert credential_store.set_service_agent_binding(hub.name, AGENT, account_label="acct",
                                                      owner_sub=world.owner, set_by=world.owner)
    monkeypatch.setattr(mcp_registry, "_get_agent_mcps_with_device_exclusions",
                        lambda *a, **k: ([hub], {}))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: [hub])

    def rebind():
        credential_store.set_service_agent_binding(hub.name, AGENT, account_label="acct",
                                                   owner_sub=world.other, set_by=world.other)
    if flip_after == "the resolver's reads":
        real = credential_resolver.resolve_credentials

        def resolve_then_rebind(*a, **k):
            out = real(*a, **k)
            assert hub.name in out.available_mcps and hub.name in out.cli_credentials
            rebind()
            return out
        monkeypatch.setattr(credential_resolver, "resolve_credentials", resolve_then_rebind)
    else:
        real_source = mcp_registry._gateway_bearer_source

        def read_then_rebind(*a, **k):
            out = real_source(*a, **k)
            assert out[0] is not None
            rebind()
            return out
        monkeypatch.setattr(mcp_registry, "_gateway_bearer_source", read_then_rebind)
    target, browser = _personal(world.owner)
    path, flat, reasons, bundles, _ = _build(target, browser, person=world.owner,
                                             personal_owner=world.owner)
    assert hub.name not in bundles and hub.name in reasons
    assert reason in reasons[hub.name]
    assert hub.name not in (json.loads(path.read_text())["mcpServers"] if path else {})
    other_name = task_store.get_username_by_sub(world.other)
    assert f"tok-of-{other_name}" not in _everything(path, flat, bundles)


def test_git_keeps_the_platform_helper_for_a_withheld_account(world, tmp_path, monkeypatch):
    """A GitHub-like MCP whose binding is another person's: the MCP stays
    out, and git still asks the platform's helper (which refuses with the
    platform's reason) instead of the machine's own credential manager."""
    hub = _manifest(tmp_path, "pm-hub", {
        "label": "Hub X", "version": "1.0.0",
        "server": {"transport": "streamable_http", "url_template": "https://hub.example.test/mcp/",
                   "source": "remote:hub.example.test"},
        "credentials": {"type": "per_user", "label": "Hub X", "service_account": True, "oauth": {
            "provider_id": "hubx", "flows": ["personal_access_token"], "bearer_required": True,
            "proposed_hosts": ["hub.example.test"],
            "token_format": {"schema": "generic_oauth_v1", "filename_pattern": "{account_label}.json"},
            "env_injection": ["HUBX_TOKEN"],
            "git_credential_helper": {"host": "hub.example.test", "helper": "!x"}}}})
    monkeypatch.setitem(mcp_registry._manifests, hub.name, hub)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: [hub])
    credential_store.set_user_credentials(world.other, hub.name, {"x": "y"}, account_label="acct")
    assert credential_store.set_service_agent_binding(hub.name, AGENT, account_label="acct",
                                                      owner_sub=world.other, set_by=world.other)
    result = credential_resolver.resolve_credentials(AGENT, world.owner, task_scope=SCOPE_AGENT,
                                                     only_owner=world.owner)
    assert hub.name in result.excluded_mcps and hub.name not in result.cli_credentials
    assert result.env_vars["GIT_CONFIG_KEY_1"] == "credential.https://hub.example.test.helper"
    assert result.env_vars["GIT_CONFIG_VALUE_1"] == credential_resolver.PLATFORM_GIT_HELPER
    assert set(result.env_vars) <= result.bash_env_keys
    token_ref, reason = mcp_registry._gateway_bearer_source(hub, world.owner, AGENT, SCOPE_AGENT,
                                                            only_owner=world.owner)
    assert token_ref is None and reason


def test_the_owner_predicate():
    target, browser = _personal("owner-1")
    assert personal_owner_of(target, "", "owner-1", browser) == "owner-1"
    assert personal_owner_of(target, "", "someone", browser) == ""
    assert personal_owner_of(target, "ann", "owner-1", browser) == ""
    assert personal_owner_of(target, "", "", browser) == ""
    assert personal_owner_of(target, "", "owner-1", None) == ""
    admin = placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m")
    assert personal_owner_of(admin, "", "owner-1", browser) == ""


def test_the_wake_that_resumes_a_moved_shared_chat_carries_the_owners_own(temp_db, monkeypatch):
    """The agent's own ``move_chat`` resumes the chat through the wake
    builder, and the dashboard warm-up then reuses that session: the wake
    asks the same rule, so the token goes there and the filter names the
    owner."""
    import asyncio
    from datetime import datetime, timezone

    from core.execution_layer import ResumeVerdict
    from core.session import session_kind, session_manager, visibility
    from services.engines import subscription_pool
    from services.remote import remote_status
    from services.scheduler import scheduler
    from storage import database as task_store, remote_store
    from storage.agents import agent_store
    from storage.identity import db_users
    agent, owner, machine = "pm-wake", "pm-wake-owner", "m-pm-wake"
    agent_store.create_agent(agent, "PM", collaborative=False, default_scope=SCOPE_AGENT)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("INSERT INTO users (sub, email, name, username, role, created_at, last_login) "
                     "VALUES (%s, 'w@test.com', 'W', 'pmwake', 'member', %s, %s)", (owner, now, now))
        conn.execute("INSERT INTO remote_machines (id, name, registered_by, pairing_scope, capabilities,"
                     " created_at, device_grants) VALUES (%s, 'Desk', %s, 'user', '{}', %s, %s)",
                     (machine, owner, now, json.dumps(["browser"])))
        conn.commit()
    db_users.add_user_agent(owner, agent, "editor", "user-admin")
    remote_store.set_browser_mode(machine, "own")
    remote_store.set_browser_extension_token(machine, TOKEN)
    cid = "pm-wake-chat"
    task_store.create_chat(cid, visibility.SHARED_CHAT_OWNER_PREFIX + agent, agent, "auto",
                           execution_path="claude-code-cli")
    task_store.update_chat(cid, execution_target=machine)
    monkeypatch.setattr(remote_status, "is_reachable", lambda mid: mid == machine)
    built: dict = {}

    def _build(*a, **k):
        built.update(k)
        from core.credentials.mcp_broker import SecretBundle
        return None, {}, {}, {mcp_registry.BROWSER_SERVER_KEY: SecretBundle(
            env={mcp_registry.BROWSER_TOKEN_ENV: TOKEN, "OTHER": "x"})}, set()
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _build)
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env", lambda *a, **k: ("sub-1", {}))
    seen: dict = {}

    class _Layer:
        async def can_resume_session(self, sid, agent_name="", username="", external_home=""):
            return ResumeVerdict.RESUMABLE

        async def start_session(self, sid, cfg):
            seen["cfg"] = cfg
            raise RuntimeError("stop-after-cfg")
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda agent, **kw: _Layer())
    with pytest.raises(RuntimeError, match="stop-after-cfg"):
        asyncio.run(scheduler._deliver_via_oneshot(
            "sess-pm-wake", agent, "resume", user_sub=owner, role="editor", chat_id=cid,
            client_type=session_kind.DASHBOARD.name))
    assert built["personal_owner"] == owner
    assert built["target_browser"].owner_sub == owner
    assert seen["cfg"].mcp_secret_bundles[mcp_registry.BROWSER_SERVER_KEY].env == {
        mcp_registry.BROWSER_TOKEN_ENV: TOKEN}


def test_the_browser_profile_column_is_added_forward(temp_db):
    """An install upgraded from 1.9.0 has no ``browser_profile``: the start's
    migration adds it, and a second run changes nothing."""
    from storage import pg
    from storage.remote_schema import migrate_remote
    pg.close_pool(timeout=2.0)
    try:
        with get_conn() as conn:
            conn.execute("ALTER TABLE remote_machines DROP COLUMN IF EXISTS browser_profile")
            migrate_remote(conn)
            migrate_remote(conn)
            row = conn.execute(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'remote_machines' AND column_name = 'browser_profile'").fetchone()
            conn.commit()
    finally:
        # The DROP and the ADD moved the column to the row's end: every pooled
        # session's cached plan of a SELECT * is stale, so the pools close.
        pg.close_pool(timeout=2.0)
    assert row is not None and row["is_nullable"] == "NO" and "''" in row["column_default"]


# ── the two other builders ask the same rule ───────────────────────────────

def _token_build(rig, monkeypatch):
    """The recording rig's MCP build, answering a browser bundle that holds
    the machine's token and one more key (only the token may go)."""
    from core.credentials.mcp_broker import SecretBundle

    def _mcp_build(*args, **kwargs):
        rig.calls.append({"call": "build_session_mcp_config", "args": list(args), "kwargs": dict(kwargs)})
        return None, {}, {}, {mcp_registry.BROWSER_SERVER_KEY: SecretBundle(
            env={mcp_registry.BROWSER_TOKEN_ENV: TOKEN, "OTHER": "x"})}, set()
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _mcp_build)
    return lambda: [c for c in rig.calls if c["call"] == "build_session_mcp_config"][-1]["kwargs"]


def test_the_chat_builder_names_the_machines_owner(temp_db, monkeypatch, tmp_path):
    """A Shared-only chat pinned to the computer of the person at it: the
    build reads the bindings through that person's filter and carries the
    token alone; the same chat on the platform passes no keyword."""
    import asyncio

    from core.config.config_builder import build_agent_config
    from services.remote import remote_status
    from storage import database as db, remote_store
    from storage.identity import db_users
    from tests.fixtures.loop_builder_rig import SHARED, install
    rig = install(monkeypatch, tmp_path)
    last = _token_build(rig, monkeypatch)
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    remote_store.create_remote_machine("m-max", "max-desk", "user-manager", pairing_scope="user")
    monkeypatch.setattr(remote_status, "is_reachable", lambda mid: mid == "m-max")

    def build(pin):
        return asyncio.run(build_agent_config(
            SHARED, db.get_user("user-manager"), "user-manager", "editor", client_type="dashboard",
            session_id=f"s-pm-{pin}", pinned_target=pin))
    cfg = build("m-max")
    assert last()["personal_owner"] == "user-manager"
    assert set(cfg.mcp_secret_bundles) == {mcp_registry.BROWSER_SERVER_KEY}
    assert cfg.mcp_secret_bundles[mcp_registry.BROWSER_SERVER_KEY].env == {mcp_registry.BROWSER_TOKEN_ENV: TOKEN}
    build("local")
    assert "personal_owner" not in last()


def test_the_chat_worker_builder_names_its_delegator(temp_db, monkeypatch, tmp_path):
    """A chat lane delegated from a person's own chat, running on that
    person's computer: the build's owner is the delegator."""
    from core.config import task_config_builder as tcb
    from services.scheduler import task_kinds
    from storage import remote_store
    from tests.fixtures.loop_builder_rig import AGENT, install, run
    rig = install(monkeypatch, tmp_path)
    last = _token_build(rig, monkeypatch)
    remote_store.create_remote_machine("m-vic", "vic-laptop", "user-viewer", pairing_scope="user")
    monkeypatch.setattr(tcb, "task_target", lambda task, identity, agent="": tcb.TaskTarget("m-vic", None, ""))
    task = SimpleNamespace(scope="agent", created_by="user-viewer", task_type=task_kinds.DELEGATE,
                           target_chat_id="chat-pm-lane", agent=AGENT, notification_mode="none",
                           name="lane", id="dyn-pm-lane", override_execution_path="claude-code-cli")
    cfg = run(tcb.build_task_agent_config(AGENT, task, "s-pm-lane"))
    assert last()["personal_owner"] == "user-viewer"
    assert cfg.mcp_secret_bundles[mcp_registry.BROWSER_SERVER_KEY].env == {mcp_registry.BROWSER_TOKEN_ENV: TOKEN}
