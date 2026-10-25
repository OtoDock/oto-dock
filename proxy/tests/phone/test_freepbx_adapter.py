"""Unit tests for the FreePBX adapter — ``asterisk_freepbx.py``.

The adapter is exercised over a MOCKED AMI client (``asterisk_freepbx.AMIClient``
is patched), so these are pure + fast — no live PBX, no DB. They pin the AstDB
keying (``otodock`` / ``route_uuid/<n>``), the verify round-trip, the DID/UUID
guards, the config→AMI-param wiring, and that failures degrade (health/verify)
rather than 500.
"""

from __future__ import annotations

import asyncio

import pytest

from services.phone.phone_adapters import PhoneAdapterError, asterisk_freepbx
from services.phone.phone_adapters.asterisk_freepbx import AsteriskFreePBXAdapter

_UNSET = object()


def _adapter(*, config=_UNSET, secret="sek", register_secret="regsek",
             host="pbx.local", media="ph.local:9092",
             register="ph.local:9093/v1/calls/register"):
    server_row = {
        "id": 7, "name": "pbx", "host": host, "adapter_type": "asterisk_freepbx",
        "config": {"ami_host": "10.0.0.9", "ami_port": 5038, "ami_username": "otodock"}
        if config is _UNSET else config,
    }

    def resolver(suffix: str):
        if suffix == "register-secret":
            return {"REGISTER_SECRET": register_secret} if register_secret else {}
        assert suffix == "ami-secret"
        return {"AMI_SECRET": secret} if secret else {}

    return AsteriskFreePBXAdapter(
        server_row, credential_resolver=resolver, media_endpoint=media,
        register_endpoint=register,
    )


def _patch_ami(monkeypatch, *, login_fails=False, get_override=_UNSET):
    state = {"store": {}, "puts": [], "gets": [], "dels": [], "params": None}

    class _FakeAMI:
        def __init__(self, *, host, port, username, secret):
            state["params"] = {"host": host, "port": port,
                               "username": username, "secret": secret}

        async def __aenter__(self):
            if login_fails:
                raise PhoneAdapterError("AMI login failed: bad creds", status_code=502)
            return self

        async def __aexit__(self, *exc):
            return False

        async def db_put(self, family, key, val):
            state["puts"].append((family, key, val))
            state["store"][(family, key)] = val

        async def db_get(self, family, key):
            state["gets"].append((family, key))
            if get_override is not _UNSET:
                return get_override
            return state["store"].get((family, key))

        async def db_del(self, family, key):
            state["dels"].append((family, key))
            state["store"].pop((family, key), None)

    monkeypatch.setattr(asterisk_freepbx, "AMIClient", _FakeAMI)
    return state


# -- bootstrap / verify -----------------------------------------------------

def test_verify_bootstrap_round_trip(monkeypatch):
    state = _patch_ami(monkeypatch)
    res = asyncio.run(_adapter().verify_bootstrap())
    assert res.status == "verified"
    # wrote, read-back, and cleaned up a temp key under the otodock/_verify tree
    assert len(state["puts"]) == 1 and state["puts"][0][0] == "otodock"
    assert state["puts"][0][1].startswith("_verify/")
    assert state["dels"] == [("otodock", state["puts"][0][1])]


def test_verify_bootstrap_mismatch_is_failed(monkeypatch):
    _patch_ami(monkeypatch, get_override="tampered")
    res = asyncio.run(_adapter().verify_bootstrap())
    assert res.status == "failed" and "mismatch" in res.detail


def test_verify_bootstrap_ami_down_is_failed_not_raise(monkeypatch):
    _patch_ami(monkeypatch, login_fails=True)
    res = asyncio.run(_adapter().verify_bootstrap())
    assert res.status == "failed" and "login failed" in res.detail.lower()


def test_snippet_has_bridge_and_media_endpoint():
    snip = asyncio.run(_adapter(
        media="ph.local:9092", register="ph.local:9093/v1/calls/register",
        register_secret="regsek").get_bootstrap_snippet())
    assert "[oto-audiosocket-bridge]" in snip
    assert "exten => _.,1" in snip  # pattern match (not `s`) — the locked design
    assert "AudioSocket(${AS_UUID},ph.local:9092)" in snip
    # outbound context ships in the same snippet (one paste, both directions)
    assert "[oto-audiosocket-outbound]" in snip
    assert "AudioSocket(${OUTBOUND_UUID},ph.local:9092)" in snip
    # inbound also POSTs caller metadata to /v1/calls/register, Bearer the
    # server's own register secret, before AudioSocket connects
    assert "http://ph.local:9093/v1/calls/register" in snip
    assert "Authorization: Bearer regsek" in snip
    # outbound does NOT register (metadata is attached in-process by the daemon)
    assert snip.count("/v1/calls/register") == 1
    # every placeholder is substituted
    assert "__REGISTER_ENDPOINT__" not in snip and "__REGISTER_SECRET__" not in snip
    assert "__MEDIA_ENDPOINT__" not in snip


# -- provisioning -----------------------------------------------------------

def test_provision_inbound_dbput(monkeypatch):
    state = _patch_ami(monkeypatch)
    h = asyncio.run(_adapter().provision_route(
        {"direction": "inbound", "did": "200", "audiosocket_uuid": "u-1", "id": "r1"}))
    assert state["puts"] == [("otodock", "route_uuid/200", "u-1")]
    assert h.did == "200" and h.audiosocket_uuid == "u-1"
    assert h.adapter_data == {"mode": "ami", "astdb_key": "otodock/route_uuid/200"}
    assert "oto-audiosocket-bridge,200,1" in h.instructions


def test_provision_inbound_requires_did(monkeypatch):
    _patch_ami(monkeypatch)
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(_adapter().provision_route(
            {"direction": "inbound", "did": "", "audiosocket_uuid": "u", "id": "r"}))
    assert ei.value.status_code == 400 and "DID" in ei.value.message


def test_provision_inbound_requires_uuid(monkeypatch):
    _patch_ami(monkeypatch)
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(_adapter().provision_route(
            {"direction": "inbound", "did": "200", "id": "r"}))
    assert ei.value.status_code == 400 and "UUID" in ei.value.message


def test_provision_inbound_rejects_crlf_did(monkeypatch):
    _patch_ami(monkeypatch)
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(_adapter().provision_route(
            {"direction": "inbound", "did": "200\r\nInjected: x",
             "audiosocket_uuid": "u", "id": "r"}))
    assert ei.value.status_code == 400


def test_provision_outbound_is_noop(monkeypatch):
    state = _patch_ami(monkeypatch)
    h = asyncio.run(_adapter().provision_route({"direction": "outbound", "id": "r2"}))
    assert h.audiosocket_uuid is None and state["puts"] == []


def test_deprovision_inbound_dbdel(monkeypatch):
    state = _patch_ami(monkeypatch)
    asyncio.run(_adapter().deprovision_route(
        {"direction": "inbound", "did": "200", "id": "r1"}))
    assert state["dels"] == [("otodock", "route_uuid/200")]


def test_deprovision_outbound_is_noop(monkeypatch):
    state = _patch_ami(monkeypatch)
    asyncio.run(_adapter().deprovision_route({"direction": "outbound", "id": "r2"}))
    assert state["dels"] == []


# -- health + config wiring -------------------------------------------------

def test_health_check_ok(monkeypatch):
    _patch_ami(monkeypatch)
    hs = asyncio.run(_adapter().health_check())
    assert hs.healthy is True


def test_health_check_failure_is_unhealthy_not_500(monkeypatch):
    _patch_ami(monkeypatch, login_fails=True)
    hs = asyncio.run(_adapter().health_check())
    assert hs.healthy is False and "login failed" in hs.detail.lower()


def test_ami_params_from_config_and_secret(monkeypatch):
    state = _patch_ami(monkeypatch)
    asyncio.run(_adapter(secret="topsecret").provision_route(
        {"direction": "inbound", "did": "200", "audiosocket_uuid": "u", "id": "r"}))
    assert state["params"] == {
        "host": "10.0.0.9", "port": 5038, "username": "otodock", "secret": "topsecret"}


def test_ami_host_falls_back_to_row_host(monkeypatch):
    state = _patch_ami(monkeypatch)
    # config has no ami_host → use the server row's `host`
    a = _adapter(config={"ami_username": "otodock"}, host="fallback.pbx")
    asyncio.run(a.provision_route(
        {"direction": "inbound", "did": "200", "audiosocket_uuid": "u", "id": "r"}))
    assert state["params"]["host"] == "fallback.pbx"


def test_unconfigured_ami_raises_400(monkeypatch):
    _patch_ami(monkeypatch)
    a = _adapter(secret="")  # no AMI secret resolved
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(a.provision_route(
            {"direction": "inbound", "did": "200", "audiosocket_uuid": "u", "id": "r"}))
    assert ei.value.status_code == 400 and "Configure the AMI" in ei.value.message
    # and health degrades rather than raising
    assert asyncio.run(a.health_check()).healthy is False


def test_list_provisioned_routes_is_none():
    # AMI without the `command` priv can't enumerate AstDB → drift untracked.
    assert asyncio.run(_adapter().list_provisioned_routes()) is None
