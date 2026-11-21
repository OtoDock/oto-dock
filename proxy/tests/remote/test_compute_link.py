"""A compute machine's link: the install's own admitted from its own address
alone, one on another server by the rule every machine link follows, no code
ever pushed to either.

``compute_link_refusal`` is the rule (the key, the claim, the file mode, the
listener, a forwarding header, the peer), judged before the secret;
``_admitted_machine`` applies it to a compute row and refuses a paired row
that claims the kind; the address of a machine on another server is judged
after its secret (``tests/remote/test_compute_pairing.py``).
``remote_store.mint_compute_secret`` writes the install's own machine's hash.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config as app_config
from core import placement
from core.config import deployment
from storage import remote_store
from ws import satellite

_COMPUTE = {"id": "c-1", "name": "Compute one", "pairing_scope": "compute", "registered_by": "",
            "capabilities": "{}", "allow_full_fs": False, "device_grants": "[]", "auto_update_enabled": False}
_ADMIN = {"id": "m-1", "name": "Rack", "pairing_scope": "admin", "registered_by": "sub-admin",
          "capabilities": "{}", "allow_full_fs": False, "device_grants": "[]"}


def _scope(peer="127.0.0.1", *, server_port=8400, headers=()):
    return {"client": (peer, 40000), "server": ("127.0.0.1", server_port),
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


class _Socket:
    def __init__(self, scope=None):
        self.scope = scope if scope is not None else _scope()
        self.sent: list = []
        self.closed: tuple = ()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code, reason=""):
        self.closed = (code, reason)


@pytest.fixture
def key_on(monkeypatch):
    monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 48400, raising=False)
    monkeypatch.setattr(deployment.config, "RUNNING_IN_DOCKER", False, raising=False)


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------

def test_the_rule_admits_a_loopback_link_that_claims_compute_under_the_key(key_on):
    assert satellite.compute_link_refusal(_scope(), {"compute": True}) == ""


def test_the_retired_word_local_no_longer_refuses_the_link(monkeypatch):
    """Every install runs its sessions on compute machines: the key that
    kept them in the platform is not obeyed, so its word refuses no link."""
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_SESSIONS", "local", raising=False)
    assert satellite.compute_link_refusal(_scope(), {"compute": True}) == ""


def test_the_rule_refuses_a_process_that_does_not_claim_compute(key_on):
    assert "claim" in satellite.compute_link_refusal(_scope(), {})
    assert "claim" in satellite.compute_link_refusal(_scope(), {"compute": "yes"})
    assert "claim" in satellite.compute_link_refusal(_scope(), None)


def test_the_rule_refuses_the_internal_listener_a_forwarder_and_another_address(key_on):
    # A sandbox's splice lands on the internal listener: never the compute machine's door.
    assert "internal listener" in satellite.compute_link_refusal(_scope(server_port=48400), {"compute": True})
    assert "forwarder" in satellite.compute_link_refusal(
        _scope(headers=[("x-forwarded-for", "127.0.0.1")]), {"compute": True})
    # Every header an edge adds, the one list lan_check keeps.
    from auth import lan_check
    for name in sorted(lan_check.EDGE_SIGNALS):
        assert "forwarder" in satellite.compute_link_refusal(
            _scope(headers=[(name.decode(), "x")]), {"compute": True}), name
    assert "does not link from 192.168.130.10" in satellite.compute_link_refusal(
        _scope("192.168.130.10"), {"compute": True})
    assert "does not link from 172.18.0.5" in satellite.compute_link_refusal(
        _scope("172.18.0.5"), {"compute": True})


def test_in_compose_the_compute_service_links_from_its_own_address_alone(key_on, monkeypatch):
    monkeypatch.setattr(deployment.config, "RUNNING_IN_DOCKER", True, raising=False)
    monkeypatch.setattr(deployment.config, "OTODOCK_COMPUTE_LINK_FROM", "10.205.0.10", raising=False)
    assert deployment.compute_link_allowed("10.205.0.10")
    assert satellite.compute_link_refusal(_scope("10.205.0.10"), {"compute": True}) == ""
    # Loopback, the install's other networks and a neighbour on the compute plane are not it.
    for peer in ("127.0.0.1", "10.200.0.7", "10.205.0.11", "172.18.0.5"):
        assert not deployment.compute_link_allowed(peer), peer
        assert "does not link from" in satellite.compute_link_refusal(_scope(peer), {"compute": True})


def test_in_compose_with_no_address_named_nothing_is_admitted(key_on, monkeypatch):
    monkeypatch.setattr(deployment.config, "RUNNING_IN_DOCKER", True, raising=False)
    monkeypatch.setattr(deployment.config, "OTODOCK_COMPUTE_LINK_FROM", "", raising=False)
    assert not deployment.compute_link_allowed("127.0.0.1")
    assert not deployment.compute_link_allowed("")


def test_the_compute_address_is_not_the_local_network_for_a_local_only_account(monkeypatch):
    from types import SimpleNamespace

    from auth import lan_check
    monkeypatch.setattr(app_config, "OTODOCK_CLOUD", False, raising=False)
    monkeypatch.setattr(app_config, "RUNNING_IN_DOCKER", False, raising=False)
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_FROM", "10.205.0.10", raising=False)
    at = lambda ip: SimpleNamespace(client=ip, peer=ip, internal=False, case="direct")
    conn = SimpleNamespace(scope={})
    monkeypatch.setattr(lan_check, "resolve", lambda _scope: at("10.205.0.10"))
    assert lan_check.check_local_auth_allowed(conn, {"local_only": 1}) is False
    monkeypatch.setattr(lan_check, "resolve", lambda _scope: at("10.205.0.9"))
    assert lan_check.check_local_auth_allowed(conn, {"local_only": 1}) is True


# ---------------------------------------------------------------------------
# the socket
# ---------------------------------------------------------------------------

def test_a_compute_row_is_admitted_with_its_secret_and_refused_without(key_on, monkeypatch):
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    monkeypatch.setattr(remote_store, "verify_machine_secret", lambda _mid, secret: secret == "right")
    ws = _Socket()
    row = asyncio.run(satellite._admitted_machine(ws, "c-1", "right", {"compute": True}))
    assert row is not None and row["id"] == "c-1" and ws.closed == ()
    ws = _Socket()
    assert asyncio.run(satellite._admitted_machine(ws, "c-1", "wrong", {"compute": True})) is None
    assert ws.closed == (4001, "Auth rejected")


def test_a_compute_row_is_refused_before_its_secret_when_the_rule_says_so(key_on, monkeypatch):
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    monkeypatch.setattr(remote_store, "verify_machine_secret",
                        lambda *_a: pytest.fail("refused before the secret"))
    ws = _Socket(_scope("192.168.130.10"))
    assert asyncio.run(satellite._admitted_machine(ws, "c-1", "right", {"compute": True})) is None
    assert ws.closed == (4009, "not_served") and "action" not in ws.sent[0]


def test_the_user_paired_switch_never_sees_a_compute_row(key_on, monkeypatch):
    """A compute row is not admin-paired, so with personal machines off it
    would have fallen into their 4005 refusal."""
    from storage import database as _db
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_COMPUTE))
    monkeypatch.setattr(remote_store, "verify_machine_secret", lambda *_a: True)
    monkeypatch.setattr(_db, "get_platform_setting", lambda key: "0")
    ws = _Socket()
    assert asyncio.run(satellite._admitted_machine(ws, "c-1", "right", {"compute": True})) is not None


def test_a_paired_row_that_claims_compute_is_refused(key_on, monkeypatch):
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda _mid: dict(_ADMIN))
    monkeypatch.setattr(remote_store, "verify_machine_secret",
                        lambda *_a: pytest.fail("refused before the secret"))
    ws = _Socket()
    assert asyncio.run(satellite._admitted_machine(ws, "m-1", "right", {"compute": True})) is None
    assert ws.closed == (4001, "Auth rejected")


def test_the_pre_auth_cap_keys_on_the_bucket():
    """Sockets from the internal listener share one key of their own, apart
    from a machine that links from loopback."""
    from auth import lan_check
    assert lan_check.resolve(_scope()).bucket_key == "127.0.0.1"
    app_config.INTERNAL_LISTENER_PORT = 48400
    try:
        assert lan_check.resolve(_scope(server_port=48400)).bucket_key == lan_check.INTERNAL_LISTENER_KEY
    finally:
        app_config.INTERNAL_LISTENER_PORT = 0


# ---------------------------------------------------------------------------
# the credential
# ---------------------------------------------------------------------------

def _compute_row() -> dict:
    return remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()), name=f"compute-{uuid.uuid4().hex[:6]}",
        registered_by=None, pairing_scope=placement.PAIRING_COMPUTE)


def test_a_minted_credential_signs_the_compute_machine_in_and_a_new_mint_replaces_it(temp_db):
    made = _compute_row()
    assert not remote_store.verify_machine_secret(made["id"], "anything")
    first = remote_store.mint_compute_secret(made["id"])
    assert remote_store.verify_machine_secret(made["id"], first)
    second = remote_store.mint_compute_secret(made["id"])
    assert second != first
    assert remote_store.verify_machine_secret(made["id"], second)
    assert not remote_store.verify_machine_secret(made["id"], first)
    assert "machine_secret_hash" not in remote_store.get_remote_machine(made["id"])


def test_only_a_compute_row_takes_a_minted_credential(temp_db):
    paired = remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()), name=f"rack-{uuid.uuid4().hex[:6]}", registered_by="sub-admin")
    with pytest.raises(ValueError):
        remote_store.mint_compute_secret(paired["id"])
    with pytest.raises(ValueError):
        remote_store.mint_compute_secret(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# no update
# ---------------------------------------------------------------------------

def test_nothing_is_pushed_to_a_compute_machine(monkeypatch):
    from api.remote import remote_machines

    class _Conn:
        satellite_version = "0.5.1"

        async def enqueue_send(self, msg):
            pytest.fail("an update was pushed to a compute machine")

    class _Cm:
        def is_connected(self, mid):
            return True

        def get_connection(self, mid):
            return _Conn()

    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", _Cm)
    assert asyncio.run(remote_machines.push_update_online("c-1", dict(_COMPUTE))) is False


def test_the_connection_manager_knows_its_compute_machines():
    from core.remote.satellite_connection import (
        SatelliteConnection,
        SatelliteConnectionManager,
    )
    cm = SatelliteConnectionManager()
    conn = SatelliteConnection(machine_id="c-1", ws=None)  # type: ignore[arg-type]
    conn.compute = True
    cm._connections["c-1"] = conn
    cm._connections["m-1"] = SatelliteConnection(machine_id="m-1", ws=None)  # type: ignore[arg-type]
    assert cm.is_compute("c-1") and not cm.is_compute("m-1") and not cm.is_compute("gone")
    assert cm.compute_machine_ids() == frozenset({"c-1"})


def _connected(**machines):
    """A connection manager with ``{id: (compute, own_copy, own_tools)}``."""
    from core.remote.satellite_connection import (
        SatelliteConnection,
        SatelliteConnectionManager,
    )
    cm = SatelliteConnectionManager()
    for machine_id, (compute, own_copy, own_tools) in machines.items():
        conn = SatelliteConnection(machine_id=machine_id, ws=None)  # type: ignore[arg-type]
        conn.compute, conn.own_copy, conn.own_tools = compute, own_copy, own_tools
        cm._connections[machine_id] = conn
    return cm


def test_which_machines_the_platform_installs_tools_on():
    """A paired computer, a compute machine on another host, and the one
    beside the platform once it claimed a tools folder of its own. One that
    shares the platform's folders and claims none is sent no tool."""
    cm = _connected(laptop=(False, False, False), own=(True, False, True),
                    shared=(True, False, False), rack=(True, True, False))
    assert [m for m in ("laptop", "own", "shared", "rack", "gone") if cm.takes_tools(m)] == ["laptop", "own", "rack"]
    assert [m for m in ("laptop", "own", "shared", "rack", "gone") if cm.builds_own_tools(m)] == ["own"]


def test_an_installs_build_runs_beside_the_platform_else_on_another_host():
    assert _connected(rack=(True, True, False), own=(True, False, True)).building_machine() == "own"
    assert _connected(rack=(True, True, False), laptop=(False, False, False)).building_machine() == "rack"
    assert _connected(shared=(True, False, False), laptop=(False, False, False)).building_machine() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("compute,address,claim,takes", [
    (True, "", "own", True),
    (True, "", None, False),            # a machine that claims nothing builds for no install
    (True, "", "shared", False),
    (True, "192.168.50.7", "own", False),   # one on another host keeps every folder: its tools are no claim
    (False, "", "own", False),          # a paired computer cannot claim it
])
async def test_the_sign_in_reads_the_tools_claim(compute, address, claim, takes):
    from unittest.mock import patch

    from core.remote.satellite_connection import SatelliteConnectionManager

    class _WS:
        async def close(self, code=1000, reason=""):
            pass

        async def send_text(self, text):
            pass
    cm = SatelliteConnectionManager()
    caps = {"compute": compute, **({"compute_tools": claim} if claim else {})}
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        conn = await cm.register("c-1", _WS(), caps, compute=compute, compute_address=address)
        assert conn.own_tools is takes and cm.builds_own_tools("c-1") is takes
        await cm.drain_persists()


# ---------------------------------------------------------------------------
# a compute machine on another host
# ---------------------------------------------------------------------------

_REMOTE = {**_COMPUTE, "id": "c-2", "name": "Rack two", "compute_address": "192.168.50.7"}
_OWN = {"compute": True, "compute_files": "own"}


@pytest.fixture
def plain_lan(key_on, monkeypatch):
    """An install served over plain HTTP on a LAN literal, as T1."""
    monkeypatch.setattr(app_config, "DASHBOARD_PUBLIC_URL", "http://192.168.50.2:8400", raising=False)


@pytest.fixture
def behind_tls(key_on, monkeypatch):
    from auth import lan_check
    monkeypatch.setattr(app_config, "DASHBOARD_PUBLIC_URL", "https://agents.example.com", raising=False)
    lan_check.reset_state()
    yield
    lan_check.reset_state()


def test_a_machine_on_another_server_links_straight_on_a_plain_lan_install(plain_lan):
    # Before its secret only the link's shape is judged: its address is
    # recorded, or refused by its lock, after the secret verified.
    assert satellite.compute_link_refusal(_scope("192.168.50.7"), _OWN, _REMOTE) == ""
    assert satellite.compute_link_refusal(_scope("192.168.50.8"), _OWN, _REMOTE) == ""
    assert satellite.compute_link_refusal(_scope("::ffff:192.168.50.7"), _OWN, _REMOTE) == ""


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "0.0.0.0", "169.254.1.1"])
def test_a_machine_on_another_server_never_links_from_this_hosts_own_addresses(plain_lan, peer):
    assert "does not link from" in satellite.compute_link_refusal(_scope(peer), _OWN, _REMOTE)


def test_a_forwarded_link_is_refused_on_a_plain_lan_install(plain_lan):
    why = satellite.compute_link_refusal(
        _scope("192.168.50.7", headers=(("x-forwarded-for", "192.168.50.7"),)), _OWN, _REMOTE)
    assert "straight to this server" in why


def test_behind_tls_the_link_comes_through_the_edge_over_https(behind_tls):
    from services.remote import compute_pairing
    edge = (("x-forwarded-for", "203.0.113.9"), ("x-forwarded-proto", "https"))
    assert satellite.compute_link_refusal(_scope("127.0.0.1", headers=edge), _OWN, _REMOTE) == ""
    assert compute_pairing.link_address(_scope("127.0.0.1", headers=edge)) == ("203.0.113.9", "", False)
    # Plaintext through the edge, or straight to the port: refused.
    plain = (("x-forwarded-for", "203.0.113.9"), ("x-forwarded-proto", "http"))
    assert "https address" in satellite.compute_link_refusal(_scope("127.0.0.1", headers=plain), _OWN, _REMOTE)
    assert "https address" in satellite.compute_link_refusal(_scope("203.0.113.9"), _OWN, _REMOTE)
    # The internal listener never.
    assert "internal listener" in satellite.compute_link_refusal(
        _scope("127.0.0.1", server_port=48400, headers=edge), _OWN, _REMOTE)


def test_the_file_mode_is_the_rows(plain_lan):
    # A machine on another server that claims the shared folder, and one
    # beside the platform that claims its own copy, are refused first.
    assert "its own copy" in satellite.compute_link_refusal(
        _scope("192.168.50.7"), {"compute": True}, _REMOTE)
    assert "share the platform's folders" in satellite.compute_link_refusal(_scope(), _OWN, _COMPUTE)
    # An older compute satellite claims nothing: the shared folder, as before.
    assert satellite.compute_link_refusal(_scope(), {"compute": True}, _COMPUTE) == ""


def test_a_recorded_address_is_a_link_address_never_the_local_network(monkeypatch):
    from auth import lan_check
    deployment.set_compute_addresses(["192.168.50.7"])
    try:
        class _Conn:
            scope = _scope("192.168.50.7")
        monkeypatch.setattr(app_config, "OTODOCK_CLOUD", False, raising=False)
        assert lan_check.check_local_auth_allowed(_Conn(), {"local_only": 1}) is False

        class _Other:
            scope = _scope("192.168.50.8")
        assert lan_check.check_local_auth_allowed(_Other(), {"local_only": 1}) is True
    finally:
        deployment.set_compute_addresses([])


@pytest.mark.parametrize("addr,ok", [
    ("192.168.50.7", True), ("2001:db8::7", True), ("127.0.0.1", False), ("0.0.0.0", False),
    ("::1", False), ("224.0.0.1", False), ("169.254.1.1", False), ("rack.lan", False), ("", False),
])
def test_what_a_machine_on_another_server_may_sign_in_from(addr, ok):
    assert (deployment.compute_address_problem(addr) == "") is ok


def test_placement_reads_the_mode_from_the_row():
    assert placement.compute_own_copy(_REMOTE) is True
    assert placement.compute_own_copy(_COMPUTE) is False
    assert placement.compute_own_copy({**_ADMIN, "compute_address": "192.168.50.7"}) is False
    pl = placement.from_machine(placement.KIND_COMPUTE, _REMOTE)
    assert pl.own_copy and pl.files_synced and pl.site == placement.SITE_MACHINE
    shared = placement.from_machine(placement.KIND_COMPUTE, _COMPUTE)
    assert not shared.own_copy and not shared.files_synced and shared.site == placement.SITE_LOCAL
    assert placement.log_id("compute-0a1b2c3d4e5f") == "compute-0a1b2c3d4e5f"
    assert placement.log_id("compute-0a1b2c3d4e5f6a7b") == "compute-0a1b2c3d4e5f6a7b"
    assert placement.log_id("3f0c9d1e-aaaa") == "3f0c9d1e"


def test_an_id_a_client_sent_is_logged_bounded():
    """A sign-in may claim any id before anything verifies it: only an id
    of the install's own shape is logged whole, nothing else carries a line
    break or more than eight characters into the log."""
    assert placement.log_id("compute-" + "a" * 300_000) == "compute-"
    assert placement.log_id("compute-0a1b2c3d4e5f\nfake line") == "compute-"
    assert placement.log_id("ab\ncd\r\x1b[1m") == "ab?cd???"


# ---------------------------------------------------------------------------
# removing one
# ---------------------------------------------------------------------------

def _paired_row(address: str = "192.168.50.35") -> dict:
    """A compute machine on another server, paired from ``address``."""
    import hashlib
    made = remote_store.create_remote_machine(
        machine_id=f"compute-{uuid.uuid4().hex[:16]}", name=f"rack-{uuid.uuid4().hex[:6]}",
        registered_by=None, pairing_scope=placement.PAIRING_COMPUTE, paired=True)
    remote_store.exchange_pairing_token(made["id"], made["pairing_token"],
                                        secret_hash=hashlib.sha256(b"s").hexdigest(), address=address)
    return remote_store.get_remote_machine(made["id"])


def test_removing_it_sends_its_chats_on_with_the_digest(temp_db):
    from storage import database
    machine = _paired_row()
    chat_id, other = f"c-{uuid.uuid4().hex[:8]}", f"c-{uuid.uuid4().hex[:8]}"
    database.create_chat(chat_id, "sub-admin", "agent-a")
    database.create_chat(other, "sub-admin", "agent-a")
    remote_store.set_chat_compute_machine(chat_id, machine["id"])
    assert remote_store.delete_remote_machine(machine["id"])
    row, untouched = database.get_chat(chat_id), database.get_chat(other)
    assert row["compute_machine"] == "" and row["session_id"] is None
    assert row["pending_history_seed"] == f"machine_removed:{machine['name']}"
    assert not untouched.get("pending_history_seed")


def test_a_compute_machine_never_removes_its_own_row(temp_db):
    from api.remote import remote_machines
    made = _compute_row()
    secret = remote_store.mint_compute_secret(made["id"])
    app = FastAPI()
    app.include_router(remote_machines.router)
    r = TestClient(app).delete(f"/v1/satellite/{made['id']}/self-uninstall",
                               headers={"X-Machine-Secret": secret})
    assert r.status_code == 403
    assert remote_store.get_remote_machine(made["id"]) is not None


def test_a_chat_that_starts_elsewhere_lets_go_of_its_machine(temp_db):
    from storage import database
    chat_id = f"c-{uuid.uuid4().hex[:8]}"
    database.create_chat(chat_id, "sub-admin", "agent-a")
    remote_store.set_chat_compute_machine(chat_id, "rack-two")
    assert database.get_chat(chat_id)["compute_machine"] == "rack-two"
    remote_store.set_chat_compute_machine(chat_id, "")
    assert database.get_chat(chat_id)["compute_machine"] == ""
    remote_store.set_chat_compute_machine("", "rack-two")  # no chat, nothing written


def test_two_servers_behind_one_address_are_two_machines(temp_db):
    first, second = _paired_row("192.168.50.38"), _paired_row("192.168.50.38")
    assert first["compute_address"] == second["compute_address"] == "192.168.50.38"
    assert remote_store.delete_remote_machine(first["id"]) and remote_store.delete_remote_machine(second["id"])
