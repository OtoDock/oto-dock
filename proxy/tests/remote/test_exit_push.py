"""The push registry carries the credential exit's values to a machine
(``core/remote/mcp_gateway_push.py``): each entry pushed under its placeholder
with the tool's prefix stripped, a compute machine taking the exit's values
and no gateway lease, the tick pushing a changed value again and revoking a
refused one, and a re-adoption rebuilding the exit half from the descriptor
so git and gh keep their placeholder after a proxy restart.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from types import SimpleNamespace

import pytest

import config
from api.hooks import credentials as hooks
from core import placement
from core.credentials import credential_exit, mcp_broker, mcp_gateway
from core.credentials.mcp_broker import SecretBundle
from core.credentials.mcp_gateway import CliCredential, ExitCredential, TokenRef
from core.remote import mcp_gateway_push as push
from tests.remote.test_mcp_gateway_push import _allow, _Cm, _static

HASH = "h" * 64


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    push._registry.clear()
    mcp_broker._store.clear()
    mcp_gateway.forget_memos()
    yield
    push._registry.clear()
    mcp_broker._store.clear()
    mcp_gateway.forget_memos()


def _exit(key: str, *, value="tok-exit", prefix="Bearer ", env_name="PROBE_KEY",
          hosts=("mcp.example.com",), cli=None, token_ref=None) -> ExitCredential:
    return ExitCredential(placeholder=credential_exit.new_placeholder(), header="Authorization", hosts=tuple(hosts),
                          allowlist_key=key, manifest="probe", env_name=env_name, prefix=prefix,
                          value=value if cli is None and token_ref is None else "", cli=cli, token_ref=token_ref)


def _frames(cm: _Cm, kind: str = "mcp_gateway_token") -> list[dict]:
    return [m for _mid, m in cm.sent if m["type"] == kind]


def test_an_exit_entry_is_pushed_under_its_placeholder_with_the_prefix_stripped():
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(key)
    mcp_broker.provision(sid, {"probe": SecretBundle(env={"PROBE_KEY": cred.placeholder}, exit=(cred,)),
                               "gw": SecretBundle(gateway=_static(key))})
    assert asyncio.run(push.push_session(sid, "m-1", HASH, cm=cm)) == 2
    by_key = {m["mcp"]: m for m in _frames(cm)}
    frame = by_key[cred.placeholder]
    # The tool writes "Bearer " itself: the value alone is pushed, toward the
    # declared host, under the session's token hash.
    assert frame["value"] == "tok-exit" and frame["header"] == "Authorization"
    assert frame["upstream"] == "https://mcp.example.com" and frame["token_hash"] == HASH
    assert 0 < frame["expires_in"] <= mcp_gateway.LEASE_S
    assert by_key["gw"]["value"] == "Bearer tok-1"
    assert cred.placeholder in push._registry[sid].pushed and "gw" in push._registry[sid].pushed


def test_a_compute_machine_takes_the_exits_values_and_no_gateway_lease():
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cm.is_compute = lambda mid: True  # type: ignore[method-assign]
    cred = _exit(key)
    mcp_broker.provision(sid, {"probe": SecretBundle(env={}, exit=(cred,)), "gw": SecretBundle(gateway=_static(key))})
    assert asyncio.run(push.push_session(sid, "c-1", HASH, cm=cm)) == 1
    assert [m["mcp"] for m in _frames(cm)] == [cred.placeholder]
    # Nothing at all: no registration either.
    push._registry.clear()
    cm.sent.clear()
    mcp_broker.provision(sid, {"gw": SecretBundle(gateway=_static(key))})
    assert asyncio.run(push.push_session(sid, "c-1", HASH, cm=cm)) == 0 and sid not in push._registry


def test_an_entry_no_allowlist_row_admits_is_not_pushed(monkeypatch):
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(f"gw-{uuid.uuid4().hex[:6]}")  # no row for this key
    mcp_broker.provision(sid, {"probe": SecretBundle(env={}, exit=(cred,))})
    assert asyncio.run(push.push_session(sid, "m-1", HASH, cm=cm)) == 0
    assert _frames(cm) == []


def test_the_tick_pushes_a_changed_value_again_and_revokes_a_refused_one(monkeypatch):
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(key)
    mcp_broker.provision(sid, {"probe": SecretBundle(env={}, exit=(cred,))})
    asyncio.run(push.push_session(sid, "m-1", HASH, cm=cm))
    cm.sent.clear()
    # The push stamped the real clock, so the ticks read it too: a fixed
    # number is past the renewal point on a machine that started minutes ago.
    now = time.monotonic()
    # The same value: nothing due.
    assert asyncio.run(push.tick(cm=cm, now=now)) == 0 and _frames(cm) == []
    # The key rotated (the admin re-entered it): the new value lands.
    mcp_broker.provision(sid, {"probe": SecretBundle(env={}, exit=(dataclasses.replace(cred, value="tok-2"),))})
    assert asyncio.run(push.tick(cm=cm, now=now)) == 1
    assert _frames(cm)[-1]["value"] == "tok-2" and _frames(cm)[-1]["mcp"] == cred.placeholder
    cm.sent.clear()
    # The row removed: the machine is told at once, with the reason's kind.
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda k, h: False)
    assert asyncio.run(push.tick(cm=cm, now=now)) == 0
    revoke = _frames(cm)[-1]
    assert revoke["mcp"] == cred.placeholder and revoke["expires_in"] == 0 and revoke["value"] == "-"
    assert revoke["kind"] == "host_not_allowed"
    assert cred.placeholder not in push._registry[sid].pushed


def test_a_token_file_entry_takes_the_shorter_of_its_expiry_and_the_lease(tmp_path, monkeypatch):
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(key, token_ref=TokenRef(str(tmp_path), "alice"))
    monkeypatch.setattr(mcp_gateway, "_token_secret", lambda ref, manifest: ("ya29.fresh", 120.0, ()))
    mcp_broker.provision(sid, {"ws": SecretBundle(env={}, exit=(cred,))})
    assert asyncio.run(push.push_session(sid, "m-1", HASH, cm=cm)) == 1
    frame = _frames(cm)[-1]
    assert frame["value"] == "ya29.fresh" and frame["expires_in"] == 120.0


def test_a_readoption_rebuilds_the_exit_half_and_git_keeps_its_placeholder(tmp_path, monkeypatch):
    """After a proxy restart the descriptor gives the entries back: a static
    value resolved again under its own key, the command-line entry bound to
    the bundle's credential by value, so the helper hands git the placeholder
    and never the token."""
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cli = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"), env_names=("GH_TOKEN",),
                        manifest="github-mcp")
    git_entry = _exit("github-mcp", hosts=("github.com", "*.github.com"), prefix="", env_name="", cli=cli)
    runway = _exit(key, env_name="RUNWAY_API_KEY", value="r-1")
    fal = _exit(_allow("rest.fal.ai"), env_name="FAL_API_KEY", value="f-1", prefix="Key ", hosts=("rest.fal.ai",))
    mcp_gateway.write_descriptor(sid, {}, cli={"github-mcp": cli}, exits={"github-mcp": (git_entry,), "video": (runway, fal)},
                                 machine_id="m-1", token_hash=HASH, agent="pa", user_sub="u-1", task_scope="user")
    doc = mcp_gateway.read_descriptor(sid)
    assert set(doc["exits"]) == {"github-mcp", "video"} and len(doc["exits"]["video"]) == 2
    assert all(d["value"] == "" for v in doc["exits"].values() for d in v)
    assert doc["exits"]["github-mcp"][0]["cli"] is True and doc["exits"]["video"][0]["cli"] is False
    # Each static entry resolves under its own key: an MCP with two.
    monkeypatch.setattr(mcp_gateway, "static_values_for",
                        lambda *a, **k: {"video\0RUNWAY_API_KEY": "r-1", "video\0FAL_API_KEY": "f-1"})
    n = asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
    assert n == 3
    bundles = mcp_broker.bundles_of(sid)
    assert bundles["github-mcp"].cli == cli
    (back,) = bundles["github-mcp"].exit
    assert back.placeholder == git_entry.placeholder and back.cli == cli and back.cli is bundles["github-mcp"].cli
    assert mcp_broker.exit_placeholder_for_cli(sid, cli) == git_entry.placeholder
    values = {c.env_name: c.value for c in bundles["video"].exit}
    assert values == {"RUNWAY_API_KEY": "r-1", "FAL_API_KEY": "f-1"}
    pushed = {m["mcp"]: m["value"] for m in _frames(cm)}
    assert pushed[runway.placeholder] == "r-1" and pushed[fal.placeholder] == "f-1"
    assert pushed[git_entry.placeholder] == "ghp_live"
    # A static entry whose value cannot be had now is left out, the rest kept.
    monkeypatch.setattr(mcp_gateway, "static_values_for", lambda *a, **k: {"video\0FAL_API_KEY": "f-1"})
    mcp_broker._store.clear()
    asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
    assert [c.env_name for c in mcp_broker.bundles_of(sid)["video"].exit] == ["FAL_API_KEY"]


def test_a_readoption_mints_the_relay_token_again(tmp_path, monkeypatch):
    from services.billing import relay_client
    from services.mcp.mcp_session_config import RELAY_TOKEN_ENV
    key = _allow("relay.example.test")
    sid, cm = str(uuid.uuid4()), _Cm()
    relay = _exit(key, env_name=RELAY_TOKEN_ENV, hosts=("relay.example.test",), value="old")
    mcp_gateway.write_descriptor(sid, {}, exits={"image-gen-mcp": (relay,)}, machine_id="m-1", token_hash=HASH,
                                 agent="pa", user_sub="u-1")
    minted: list[str] = []
    monkeypatch.setattr(relay_client, "mint_session_token", lambda sub: minted.append(sub) or "relay-new")
    assert asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm)) == 1
    assert minted == ["u-1"] and mcp_broker.bundles_of(sid)["image-gen-mcp"].exit[0].value == "relay-new"
    assert _frames(cm)[-1]["value"] == "relay-new"
    # The relay refusing ends the entry, never the re-adoption.
    def refuse(sub):
        raise relay_client.RelayNotConfigured("no")
    monkeypatch.setattr(relay_client, "mint_session_token", refuse)
    mcp_broker._store.clear()
    assert asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm)) == 0
    assert mcp_broker.bundles_of(sid) == {}


def test_a_readoption_mints_the_relay_token_for_the_person_not_the_accounts_owner(tmp_path, monkeypatch):
    """The hosted relay's token was minted for the person in the chat. The
    descriptor's ``user_sub`` is whose accounts the chat used, which is no
    one for a chat on the agent's own accounts: the person is recorded
    beside it, and the re-adoption mints for the person."""
    from services.billing import relay_client
    from services.mcp.mcp_session_config import RELAY_TOKEN_ENV
    key = _allow("relay.example.test")
    sid, cm = str(uuid.uuid4()), _Cm()
    relay = _exit(key, env_name=RELAY_TOKEN_ENV, hosts=("relay.example.test",), value="old")
    mcp_gateway.write_descriptor(sid, {}, exits={"image-gen-mcp": (relay,)}, machine_id="m-1", token_hash=HASH,
                                 agent="pa", user_sub="", person_sub="u-7")
    assert mcp_gateway.read_descriptor(sid)["person_sub"] == "u-7"
    minted: list[str] = []
    monkeypatch.setattr(relay_client, "mint_session_token", lambda sub: minted.append(sub) or "relay-new")
    assert asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm)) == 1
    assert minted == ["u-7"]


def test_a_readoption_looks_the_machine_up_for_the_person(tmp_path, monkeypatch):
    """The machine record a re-adoption rebuilds is the placement the start
    used: resolved for the person in the chat, who decides whether an admin
    computer is theirs for this agent, not for whose accounts it used."""
    from storage import remote_store
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(key, value="tok-exit")
    mcp_gateway.write_descriptor(sid, {}, exits={"probe": (cred,)}, machine_id="m-1", token_hash=HASH,
                                 agent="pa", user_sub="", person_sub="u-7")
    monkeypatch.setattr(mcp_gateway, "static_values_for", lambda *a, **k: {"probe\0PROBE_KEY": "tok-exit"})
    asked: list[tuple] = []
    monkeypatch.setattr(remote_store, "placement_of",
                        lambda target, sub, agent, **_kw: asked.append((target, sub, agent)) or placement.LOCAL_PLACEMENT)
    try:
        asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
        assert asked == [("m-1", "u-7", "pa")]
    finally:
        credential_exit.forget(sid)


def test_a_descriptor_of_exit_entries_alone_is_written_and_read(tmp_path):
    sid = str(uuid.uuid4())
    cred = _exit("k")
    mcp_gateway.write_descriptor(sid, {}, exits={"probe": (cred,)}, machine_id="m-1", token_hash=HASH, agent="pa")
    doc = mcp_gateway.read_descriptor(sid)
    assert doc is not None and doc["exits"]["probe"][0]["placeholder"] == cred.placeholder
    back = ExitCredential.from_descriptor(doc["exits"]["probe"][0])
    assert back == dataclasses.replace(cred, value="")
    assert ExitCredential.from_descriptor({"placeholder": ""}) is None
    mcp_gateway.write_descriptor(sid, {}, exits={}, machine_id="m-1", token_hash=HASH, agent="pa")
    assert mcp_gateway.read_descriptor(sid) is None


def test_the_spawn_route_keeps_a_covered_tokens_placeholder_and_a_renewal_is_refused(tmp_path, monkeypatch):
    """A tool whose token the exit carries starts with the placeholder under
    its names and no expiry, whatever the fresh read says; a renewal it kept
    from before a rebuild answers nothing."""
    sid = str(uuid.uuid4())
    ref = TokenRef(str(tmp_path), "alice@example.com")
    token = mcp_gateway.TokenCredential(env_names=("GOOGLE_OAUTH_ACCESS_TOKEN",), token_ref=ref, manifest="ws",
                                        renews=False)
    cred = _exit("ws", env_name="GOOGLE_OAUTH_ACCESS_TOKEN", hosts=("gmail.googleapis.com",), token_ref=ref)
    bundle = SecretBundle(env={"GOOGLE_OAUTH_ACCESS_TOKEN": cred.placeholder, mcp_gateway.TOKEN_ACCOUNT_ENV: "alice@example.com"},
                          exit=(cred,), token=token)
    monkeypatch.setattr(mcp_gateway, "resolve_token_credential", lambda c: mcp_gateway.ResolvedToken(
        secret="ya29.fresh", expires_at="2030-01-01T00:00:00Z", expires_in=3600.0, scopes=("s",), account="alice@example.com"))
    env = asyncio.run(hooks._spawn_env(bundle))
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == cred.placeholder and "ya29.fresh" not in str(env)
    assert mcp_gateway.TOKEN_EXPIRES_ENV not in env and env[mcp_gateway.TOKEN_SCOPES_ENV] == "s"
    # An uncovered token still gets the fresh value and its expiry.
    plain = SecretBundle(env={"GOOGLE_OAUTH_ACCESS_TOKEN": "ya29.old"}, token=dataclasses.replace(token, renews=True))
    env2 = asyncio.run(hooks._spawn_env(plain))
    assert env2["GOOGLE_OAUTH_ACCESS_TOKEN"] == "ya29.fresh" and env2[mcp_gateway.TOKEN_EXPIRES_ENV] == "2030-01-01T00:00:00Z"
    # The renewal route refuses a token credential that does not renew.
    mcp_broker.provision(sid, {"ws": bundle})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(hooks.hook_mcp_credentials(authorization=f"Bearer {mcp_broker.mint_renewal(sid, 'ws')}"))
    assert e.value.status_code == 404
    # The reprovisioned shape (an empty env) keeps the placeholder out of the way too.
    rebuilt = SecretBundle(env={}, exit=(cred,), token=token)
    env3 = asyncio.run(hooks._spawn_env(rebuilt))
    assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in env3 and mcp_gateway.TOKEN_EXPIRES_ENV not in env3


def test_after_a_restart_gh_still_reaches_github_through_the_machines_exit(tmp_path, monkeypatch):
    """The proxy's memory of a machine session (its door's secret, the
    variables gh's process gets) dies with the process. The machine kept the
    door, git kept its spawn-time entries and the tools their environments,
    all naming the old secret: the re-adoption rebuilds the record with the
    same secret, so the credential hook hands gh the variables that reach the
    door again, not the placeholder alone (which gh would then send straight
    to GitHub)."""
    from core import placement
    from storage import remote_store
    paired = placement.PlacementCapabilities(
        kind=placement.KIND_USER_REMOTE, machine_id="m-1", label="Laptop", satellite_version="0.5.175",
        exit_port=41111, exit_ok=True, os="linux", reported_otodock_dir="/home/alice/.oto-dock")
    monkeypatch.setattr(remote_store, "placement_of", lambda target, sub, agent, **_kw: paired)
    sid, cm = str(uuid.uuid4()), _Cm()
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cli = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"), env_names=("GH_TOKEN",),
                        manifest="github-mcp")
    git_entry = _exit("github-mcp", hosts=("github.com", "*.github.com"), prefix="", env_name="", cli=cli)
    before = credential_exit.machine_env(sid, paired)
    secret = credential_exit.machine_secret(sid)
    assert before and secret and credential_exit.cli_process_env(sid) == before
    mcp_gateway.write_descriptor(sid, {}, cli={"github-mcp": cli}, exits={"github-mcp": (git_entry,)},
                                 machine_id="m-1", token_hash=HASH, agent="pa", user_sub="u-1", task_scope="user")
    try:
        # The restart: the proxy's records gone, the machine's door kept.
        credential_exit.forget(sid)
        mcp_broker._store.clear()
        assert credential_exit.cli_process_env(sid) == {}
        asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
        assert credential_exit.machine_secret(sid) == secret
        assert credential_exit.cli_process_env(sid) == before
    finally:
        credential_exit.forget(sid)


def test_a_machine_sessions_secret_is_the_same_after_the_record_is_gone(monkeypatch):
    """The door's secret is derived from the platform's own key and the
    session id: recomputable after a restart, written nowhere, and another
    session's or another install's is another secret."""
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        first = credential_exit._machine_session(sid).secret
        credential_exit.forget(sid)
        assert credential_exit._machine_session(sid).secret == first and len(first) == 32
        assert credential_exit._machine_session(other).secret != first
        credential_exit.forget(sid)
        monkeypatch.setattr(config, "JWT_SECRET", "another-install-" + "x" * 40)
        assert credential_exit._machine_session(sid).secret != first
    finally:
        credential_exit.forget(sid)
        credential_exit.forget(other)


def _laptop(**kw) -> placement.PlacementCapabilities:
    fields = dict(kind=placement.KIND_USER_REMOTE, machine_id="m-1", label="Laptop", satellite_version="0.5.175",
                  exit_port=41111, exit_ok=True, os="linux", reported_otodock_dir="/home/alice/.oto-dock")
    fields.update(kw)
    return placement.PlacementCapabilities(**fields)


@pytest.mark.parametrize("now", ["the same laptop", "a laptop the rule refuses now", "a compute machine"])
def test_a_tool_that_has_not_started_gets_its_exit_back_after_a_restart(tmp_path, monkeypatch, now):
    """On a paired computer a tool that holds a placeholder also holds the
    variables that reach the machine's exit, in its bundle. After a proxy
    restart a tool that has not started yet fetches that bundle at its first
    call: the re-adoption makes the variables again (the door's secret is
    derived, so they are the ones the machine kept), beside the recorded
    placeholder. A tool with no exit entry keeps its own names as resolved,
    a machine the rule refuses now gives the tool nothing (no placeholder
    without its route), and a compute machine's tools never held them."""
    from services.mcp import mcp_permissions
    from storage import remote_store
    compute = now == "a compute machine"
    at_start = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="m-1", label="Compute",
                                               satellite_version="0.5.175", exit_port=41112, exit_ok=True) \
        if compute else _laptop()
    after = _laptop(exit_ok=False) if now == "a laptop the rule refuses now" else at_start
    monkeypatch.setattr(remote_store, "placement_of", lambda target, sub, agent, **_kw: after)
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(key, value="tok-exit")
    start_env = {"PROBE_KEY": cred.placeholder, **credential_exit.machine_env(sid, at_start)}
    assert bool(start_env.get("HTTPS_PROXY")) is not compute
    own = {"SSL_CERT_FILE": "/etc/own.pem", "OWN_KEY": "own-1"}
    mcp_broker.provision(sid, {"probe": SecretBundle(env=dict(start_env), exit=(cred,)),
                               "plain": SecretBundle(env=dict(own))})
    mcp_gateway.write_descriptor(sid, {}, exits={"probe": (cred,)}, envs=mcp_gateway.env_names_of(sid),
                                 machine_id="m-1", token_hash=HASH, agent="pa", user_sub="u-1", task_scope="user")
    # The restart: the proxy's records gone, the machine's door kept.
    credential_exit.forget(sid)
    mcp_broker._store.clear()
    monkeypatch.setattr(mcp_gateway, "static_values_for", lambda *a, **k: {"probe\0PROBE_KEY": "tok-exit"})
    monkeypatch.setattr(mcp_permissions, "manifest_for_server", lambda key: SimpleNamespace(name=key))
    asked: list[set] = []
    monkeypatch.setattr(mcp_gateway, "env_values_for",
                        lambda agent, sub, scope, wanted, *a, **k: asked.append(set(wanted))
                        or ({"plain": dict(own)} if "plain" in wanted else {}))
    try:
        asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
        bundles = mcp_broker.bundles_of(sid)
        assert bundles["plain"].env == own
        if now == "a laptop the rule refuses now":
            assert bundles["probe"].env == {}
        else:
            assert bundles["probe"].env == start_env
            # The exit's names are made, never asked of the stored values.
            assert asked == [{"plain"}]
        # A session whose exit entries have no value now gets no exit record.
        credential_exit.forget(sid)
        mcp_broker._store.clear()
        monkeypatch.setattr(mcp_gateway, "static_values_for", lambda *a, **k: {})
        asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
        assert credential_exit.machine_secret(sid) == ""
    finally:
        credential_exit.forget(sid)


def test_the_exits_variables_fill_only_the_names_the_descriptor_recorded(tmp_path, monkeypatch):
    """A session an older proxy started recorded one name fewer than the
    machine's variables hold now: the tool's bundle comes back with the
    names it was started with, never one more."""
    from services.mcp import mcp_permissions
    from storage import remote_store
    laptop = _laptop()
    monkeypatch.setattr(remote_store, "placement_of", lambda target, sub, agent, **_kw: laptop)
    key = _allow()
    sid, cm = str(uuid.uuid4()), _Cm()
    cred = _exit(key, value="tok-exit")
    start_env = {"PROBE_KEY": cred.placeholder, **credential_exit.machine_env(sid, laptop)}
    start_env.pop("NODE_USE_ENV_PROXY")
    mcp_broker.provision(sid, {"probe": SecretBundle(env=dict(start_env), exit=(cred,))})
    mcp_gateway.write_descriptor(sid, {}, exits={"probe": (cred,)}, envs=mcp_gateway.env_names_of(sid),
                                 machine_id="m-1", token_hash=HASH, agent="pa", user_sub="u-1", task_scope="user")
    credential_exit.forget(sid)
    mcp_broker._store.clear()
    monkeypatch.setattr(mcp_gateway, "static_values_for", lambda *a, **k: {"probe\0PROBE_KEY": "tok-exit"})
    monkeypatch.setattr(mcp_permissions, "manifest_for_server", lambda key: SimpleNamespace(name=key))
    monkeypatch.setattr(mcp_gateway, "env_values_for", lambda *a, **k: {})
    try:
        asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
        assert mcp_broker.bundles_of(sid)["probe"].env == start_env
    finally:
        credential_exit.forget(sid)
