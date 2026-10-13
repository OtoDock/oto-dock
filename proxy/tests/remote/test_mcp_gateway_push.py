"""The proxy's push registry for the satellite-local credential gateway
(core/remote/mcp_gateway_push.py): a session's vendor tokens are pushed to
its machine with an ack, never a sidecar's; the tick pushes again on a
changed value, a reconnect or a lease near its end and never after a purge;
the purge wipes; a re-adoption re-provisions from the descriptor.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from core.credentials import mcp_broker, mcp_gateway
from core.credentials.mcp_gateway import GatewayCredential, TokenRef
from core.remote import mcp_gateway_push as push


class _Conn:
    def __init__(self, connected_at: float):
        self.connected_at = connected_at


class _Cm:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.conn = _Conn(100.0)
        self.fail = False
        self.gateway = True

    def get_connection(self, machine_id):
        return self.conn

    def satellite_supports_mcp_gateway(self, machine_id):
        return self.gateway

    async def send_command(self, machine_id, msg, *, timeout=30.0, command_id=None):
        if self.fail:
            raise RuntimeError("not connected")
        self.sent.append((machine_id, msg))
        return {"status": "ok"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    push._registry.clear()
    mcp_gateway.forget_memos()
    yield
    push._registry.clear()
    mcp_gateway.forget_memos()


def _allow(host="mcp.example.com"):
    from storage.identity import bearer_allowlist
    key = f"gw-{uuid.uuid4().hex[:6]}"
    bearer_allowlist.add_allowed(key, host, "test")
    return key


def _static(key, value="tok-1"):
    return GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
                             value=value)


def test_the_capability_gate_needs_the_flag_and_the_version():
    from types import SimpleNamespace
    from core.remote.satellite_connection import SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    cm._connections["m"] = SimpleNamespace(capabilities={"mcp_gateway": True}, satellite_version="0.5.132")
    assert cm.satellite_supports_mcp_gateway("m") is True
    cm._connections["m"] = SimpleNamespace(capabilities={"mcp_gateway": True}, satellite_version="0.5.131")
    assert cm.satellite_supports_mcp_gateway("m") is False
    cm._connections["m"] = SimpleNamespace(capabilities={}, satellite_version="0.5.132")
    assert cm.satellite_supports_mcp_gateway("m") is False
    assert cm.satellite_supports_mcp_gateway("nobody") is False


def test_a_push_carries_the_header_the_lease_and_the_token_hash_and_skips_sidecars():
    sid = str(uuid.uuid4())
    key = _allow()
    mcp_broker.provision(sid, {
        "vendor": mcp_broker.SecretBundle(gateway=_static(key)),
        "github-mcp": mcp_broker.SecretBundle(gateway=GatewayCredential(
            upstream="http://localhost:8935", path="/mcp", allowlist_key=key, value="ghp",
            proxy_local=True)),
    })
    cm = _Cm()
    n = asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm))
    assert n == 1 and len(cm.sent) == 1
    machine, msg = cm.sent[0]
    assert machine == "m-1"
    assert msg == {"type": "mcp_gateway_token", "session_id": sid, "token_hash": "h" * 64,
                   "mcp": "vendor", "header": "Authorization", "value": "Bearer tok-1",
                   "expires_in": mcp_gateway.LEASE_S, "upstream": "https://mcp.example.com/mcp"}
    assert push.registered(sid)
    mcp_broker.purge_session(sid)


def test_a_token_file_credential_pushes_the_shorter_of_its_expiry_and_the_lease(tmp_path):
    sid = str(uuid.uuid4())
    key = _allow()
    (tmp_path / "acct.json").write_text(json.dumps({
        "access_token": "at-9", "refresh_token": "rt", "expires_at": "2099-01-01T00:00:00Z", "extra": {}}))
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=GatewayCredential(
        upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
        token_ref=TokenRef(str(tmp_path), "acct", "")))})
    cm = _Cm()
    asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm))
    msg = cm.sent[0][1]
    assert msg["value"] == "Bearer at-9" and msg["expires_in"] == mcp_gateway.LEASE_S
    mcp_broker.purge_session(sid)


def test_a_refused_credential_and_a_failed_ack_are_not_recorded():
    sid = str(uuid.uuid4())
    key = _allow("other.example.com")  # the host is not on the row: refused
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=_static(key))})
    cm = _Cm()
    assert asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm)) == 0
    assert cm.sent == [] and push._registry[sid].pushed == {}
    mcp_broker.purge_session(sid)
    sid = str(uuid.uuid4())
    key = _allow()
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=_static(key))})
    cm = _Cm()
    cm.fail = True
    assert asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm)) == 0
    assert push._registry[sid].pushed == {}
    mcp_broker.purge_session(sid)


def test_the_tick_pushes_again_on_a_changed_value_a_reconnect_or_a_near_lease(monkeypatch):
    sid = str(uuid.uuid4())
    key = _allow()
    cred = _static(key)
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=cred)})
    cm = _Cm()
    asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm))
    assert len(cm.sent) == 1
    # nothing changed: no push
    assert asyncio.run(push.tick(cm=cm)) == 0 and len(cm.sent) == 1
    # the value changed (the refresh worker rewrote the file)
    cred.value = "tok-2"
    assert asyncio.run(push.tick(cm=cm)) == 1 and cm.sent[-1][1]["value"] == "Bearer tok-2"
    # the machine reconnected
    cm.conn = _Conn(200.0)
    assert asyncio.run(push.tick(cm=cm)) == 1
    # the lease is near its end
    pushed = push._registry[sid].pushed["vendor"]
    late = pushed.pushed_at + pushed.lease_s - mcp_gateway.RENEW_BEFORE_S + 1
    assert asyncio.run(push.tick(cm=cm, now=late)) == 1
    assert len(cm.sent) == 4
    mcp_broker.purge_session(sid)


def test_a_credential_the_proxy_starts_refusing_is_revoked_on_the_machine(tmp_path):
    """A pushed token whose account is disconnected (the file is gone) leaves
    the machine at the next tick: a zero-lease frame built from the
    credential, the key forgotten only once acknowledged, so an offline
    machine is told after it reconnects; a never-pushed refusal sends nothing."""
    sid = str(uuid.uuid4())
    key = _allow()
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "at-1", "expires_at": "", "extra": {}}))
    cred = GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
                             token_ref=TokenRef(str(tmp_path), "acct", ""))
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=cred)})
    cm = _Cm()
    try:
        assert asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm)) == 1
        (tmp_path / "acct.json").unlink()   # the disconnect
        cm.fail = True                      # the machine is offline right now
        assert asyncio.run(push.tick(cm=cm)) == 0
        assert "vendor" in push._registry[sid].pushed and len(cm.sent) == 1
        cm.fail = False
        assert asyncio.run(push.tick(cm=cm)) == 0
        _, frame = cm.sent[-1]
        assert frame["type"] == "mcp_gateway_token" and frame["expires_in"] == 0
        assert frame["mcp"] == "vendor" and frame["token_hash"] == "h" * 64
        assert frame["header"] == "Authorization" and frame["upstream"] == "https://mcp.example.com/mcp"
        assert frame["value"] and "at-1" not in frame["value"]
        # The refusal travels with it: the machine answers the session's next
        # request with why, at once (0.5.140; an older one ignores both).
        assert frame["kind"] == "no_value" and "reconnect" in frame["reason"]
        assert "vendor" not in push._registry[sid].pushed
        # still refused, nothing pushed: no second revoke
        assert asyncio.run(push.tick(cm=cm)) == 0 and len(cm.sent) == 2
        # the account is connected again: pushed at the next tick
        (tmp_path / "acct.json").write_text(json.dumps({"access_token": "at-2", "expires_at": "", "extra": {}}))
        assert asyncio.run(push.tick(cm=cm)) == 1 and cm.sent[-1][1]["value"] == "Bearer at-2"
    finally:
        mcp_broker.purge_session(sid)


def test_tick_soon_runs_a_tick_on_the_loop_and_nothing_off_it(monkeypatch):
    ran: list = []

    async def _fake_tick():
        ran.append(1)
    monkeypatch.setattr(push, "tick", _fake_tick)
    push.tick_soon()   # no loop: a no-op
    assert ran == []

    async def _drive():
        push.tick_soon()
        await asyncio.sleep(0)
    asyncio.run(_drive())
    assert ran == [1]


def test_a_machine_tick_touches_only_that_machines_sessions():
    key = _allow()
    sids = {"m-1": str(uuid.uuid4()), "m-2": str(uuid.uuid4())}
    cm = _Cm()
    try:
        for machine, sid in sids.items():
            mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=_static(key))})
            asyncio.run(push.push_session(sid, machine, uuid.uuid4().hex * 2, cm=cm))
        cm.conn = _Conn(300.0)                   # both reconnected
        assert asyncio.run(push.tick(cm=cm, machine_id="m-2")) == 1
        assert [m for m, _ in cm.sent[2:]] == ["m-2"]
    finally:
        for sid in sids.values():
            mcp_broker.purge_session(sid)


def test_a_reconnecting_machine_runs_one_tick_at_a_time(monkeypatch):
    """A machine that reconnects every few seconds gets one tick running and
    at most one more queued, never a pile of them."""
    runs: list[str] = []
    gate = {}

    async def _slow_tick(*, machine_id=""):
        runs.append(machine_id)
        await gate["release"].wait()
    monkeypatch.setattr(push, "tick", _slow_tick)

    async def _drive():
        gate["release"] = asyncio.Event()
        for _ in range(10):
            push.tick_machine_soon("m-1")
            await asyncio.sleep(0)
        assert runs == ["m-1"]
        gate["release"].set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert runs == ["m-1", "m-1"]
        assert "m-1" not in push._machine_ticks
        push.tick_machine_soon("")      # no machine: nothing
    asyncio.run(_drive())
    assert runs == ["m-1", "m-1"]
    push.tick_machine_soon("m-1")       # no loop: a no-op


def test_a_push_is_recorded_on_the_connection_it_went_out_on():
    sid = str(uuid.uuid4())
    key = _allow()
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=_static(key))})
    cm = _Cm()
    real_send = cm.send_command

    async def _send_then_flap(machine_id, msg, **kw):
        out = await real_send(machine_id, msg, **kw)
        cm.conn = _Conn(999.0)          # the link dropped and came back before the ack
        return out
    cm.send_command = _send_then_flap
    try:
        asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm))
        assert push._registry[sid].pushed["vendor"].generation == 100.0
        cm.send_command = real_send
        assert asyncio.run(push.tick(cm=cm)) == 1   # pushed again on the new connection
    finally:
        mcp_broker.purge_session(sid)


def test_a_purge_forgets_the_session_and_wipes_it_on_the_machine():
    sid = str(uuid.uuid4())
    key = _allow()
    mcp_broker.provision(sid, {"vendor": mcp_broker.SecretBundle(gateway=_static(key))})
    cm = _Cm()
    asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm))
    entry = push.forget(sid)
    assert entry is not None and not push.registered(sid)
    asyncio.run(push.wipe_session(sid, entry, cm=cm))
    assert cm.sent[-1][1] == {"type": "mcp_gateway_wipe", "session_id": sid, "token_hash": "h" * 64}
    # the tick pushes nothing for a forgotten session
    assert asyncio.run(push.tick(cm=cm)) == 0
    mcp_broker.purge_session(sid)


def test_on_purge_runs_from_the_broker_and_never_raises():
    sid = str(uuid.uuid4())
    push.register(sid, "m-1", "h" * 64)
    push.on_purge(sid)  # no running loop: the wipe cannot be scheduled, nothing raises
    assert not push.registered(sid)
    push.on_purge(sid)  # idempotent


def test_a_re_adoption_re_provisions_exactly_the_descriptor_and_pushes(tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    key = _allow()
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "at-5", "expires_at": "", "extra": {}}))
    creds = {
        "vendor": GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
                                    token_ref=TokenRef(str(tmp_path), "acct", "")),
        "maps": GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
                                  header="X-Goog-Api-Key", prefix="", value="AIza-old",
                                  manifest="maps", value_from="GOOGLE_MAPS_API_KEY"),
    }
    mcp_gateway.write_descriptor(sid, creds, machine_id="m-1", token_hash="h" * 64, agent="pa",
                                 user_sub="u-1", task_scope="user")
    monkeypatch.setattr(mcp_gateway, "static_values_for",
                        lambda agent, user_sub, scope, c: {"maps": "AIza-new"} if (agent, user_sub, scope) == ("pa", "u-1", "user") else {})
    assert mcp_gateway.credentials_of(sid) == {}
    cm = _Cm()
    n = asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm))
    assert n == 2
    back = mcp_gateway.credentials_of(sid)
    assert set(back) == {"vendor", "maps"}
    assert back["maps"].value == "AIza-new" and back["vendor"].token_ref.account_label == "acct"
    values = {m["mcp"]: m["value"] for _, m in cm.sent}
    assert values == {"vendor": "Bearer at-5", "maps": "AIza-new"}
    assert all(m["token_hash"] == "h" * 64 for _, m in cm.sent)
    # no descriptor: nothing happens
    assert asyncio.run(push.reprovision_adopted("nobody", "m-1", cm=cm)) == 0
    mcp_broker.purge_session(sid)


def test_a_re_adoption_restores_the_command_line_credential_too(tmp_path):
    """The descriptor carries the git/gh credential by reference beside the
    gateway half; a session holding only that half is written and restored
    as well, and nothing is pushed for it (it is fetched per command)."""
    from core.credentials.mcp_gateway import CliCredential
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "at-7", "expires_at": "", "extra": {}}))
    cli = {"github-mcp": CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct", ""),
                                       env_names=("GH_TOKEN", "GITHUB_TOKEN"), manifest="github-mcp")}
    # only the command-line half
    sid = str(uuid.uuid4())
    mcp_gateway.write_descriptor(sid, {}, cli=cli, machine_id="m-1", token_hash="h" * 64, agent="pa",
                                 user_sub="u-1", task_scope="user")
    cm = _Cm()
    try:
        assert asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm)) == 0
        assert cm.sent == [] and not push.registered(sid)
        back = mcp_gateway.cli_credentials_of(sid)["github-mcp"]
        assert back.host == "github.com" and back.env_names == ("GH_TOKEN", "GITHUB_TOKEN")
        assert back.token_ref.account_label == "acct" and back.username == "x-access-token"
        assert mcp_broker.cli_credential(sid, "GitHub.com") is back
        res = mcp_gateway.resolve_cli_credential(back)
        assert res.secret == "at-7" and res.username == "x-access-token"
    finally:
        mcp_broker.purge_session(sid)
    assert mcp_gateway.read_descriptor(sid) is None
    # both halves on one bundle: the gateway half is pushed, the other restored
    sid = str(uuid.uuid4())
    key = _allow()
    gateway = GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
                                token_ref=TokenRef(str(tmp_path), "acct", ""))
    mcp_gateway.write_descriptor(sid, {"github-mcp": gateway}, cli=cli, machine_id="m-1",
                                 token_hash="h" * 64, agent="pa", user_sub="u-1", task_scope="user")
    try:
        assert asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm)) == 1
        bundle = mcp_broker.get(sid, "github-mcp")
        assert bundle.gateway is not None and bundle.cli.host == "github.com"
    finally:
        mcp_broker.purge_session(sid)
    # an empty descriptor of both kinds removes the file
    mcp_gateway.write_descriptor(sid, {}, cli={}, machine_id="m-1", token_hash="h" * 64)
    assert mcp_gateway.read_descriptor(sid) is None


def test_a_re_adoption_on_a_machine_without_the_gateway_provisions_and_pushes_nothing():
    """A session re-adopted on a satellite below 0.5.132 (or without the
    ``mcp_gateway`` capability): its config keeps the inline shape, so the
    credentials are provisioned for the tunnel's sidecar forward but nothing
    is pushed and the session is not registered, so no tick waits on acks the
    old satellite never sends."""
    sid = str(uuid.uuid4())
    key = _allow()
    creds = {
        "vendor": _static(key),
        "github-mcp": GatewayCredential(upstream="http://localhost:8935", path="/mcp",
                                        allowlist_key=key, value="", proxy_local=True,
                                        token_ref=TokenRef("/nonexistent", "acct", "")),
    }
    mcp_gateway.write_descriptor(sid, creds, machine_id="m-1", token_hash="h" * 64, agent="pa",
                                 user_sub="u-1", task_scope="user")
    cm = _Cm()
    cm.gateway = False
    try:
        assert asyncio.run(push.reprovision_adopted(sid, "m-1", cm=cm)) == 0
        assert cm.sent == [] and not push.registered(sid)
        assert set(mcp_gateway.credentials_of(sid)) == {"vendor", "github-mcp"}
    finally:
        mcp_broker.purge_session(sid)
