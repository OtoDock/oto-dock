"""A compute machine on another server: added by an admin in person with one
command, paired through the bootstrap door every machine pairs through, its
address recorded and locked only on purpose, its image tag the platform's.

``api/remote/compute_machines.py`` holds the routes, ``services/remote/
compute_pairing.py`` the rules, ``storage/compute_store.py`` the address's
writes; ``remote_store`` keeps the one minting and the one exchange.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config as app_config
from auth.providers import UserContext, get_current_user
from core import placement
from core.config import deployment
from services.remote import compute_bootstrap, compute_pairing
from storage import compute_store, remote_store
from ws import satellite

_OWN = {"compute": True, "compute_files": "own"}
_SECRET = "a-secret-the-server-made"
_HASH = hashlib.sha256(_SECRET.encode()).hexdigest()


@pytest.fixture
def lan(monkeypatch):
    """An install served over plain HTTP on a LAN literal, as T1."""
    monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 48400, raising=False)
    monkeypatch.setattr(app_config, "DASHBOARD_PUBLIC_URL", "http://192.168.50.2:8400", raising=False)
    monkeypatch.setattr(app_config, "OTODOCK_REMOTE_COMPUTE_IMAGE", "", raising=False)
    # A released install in Docker: the compose file names its own image, and
    # its tools path is the image's.
    monkeypatch.setattr(app_config, "OTODOCK_PROXY_IMAGE",
                        f"{compute_pairing.PUBLIC_REPOSITORY}:{app_config.PINNED_OTODOCK_VERSION}", raising=False)
    monkeypatch.setattr(app_config, "MCPS_DIR", Path(compute_pairing.IMAGE_TOOLS_PATH), raising=False)
    monkeypatch.setattr(deployment.config, "RUNNING_IN_DOCKER", False, raising=False)
    compute_pairing._refused.clear()
    yield
    deployment.set_compute_addresses([])
    compute_pairing._refused.clear()


@pytest.fixture
def told(monkeypatch):
    from services.notifications import notification_manager
    calls: list[dict] = []

    async def _fire(**kw):
        calls.append(kw)
        return []
    monkeypatch.setattr(notification_manager, "fire_notification", _fire)
    return calls


def _app(*, role="admin", sub="sub-admin", api_key=False, peer="192.168.50.9") -> TestClient:
    from api.remote import remote_machines
    user = UserContext(sub=sub, email="a@test.com", name="A", role=role, agents=[], agent_roles={})
    if api_key:
        user.is_api_key = True  # type: ignore[attr-defined]

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(remote_machines.router)
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app, client=(peer, 40000))


def _anonymous(peer="192.168.50.9") -> TestClient:
    from api.remote import remote_machines

    async def _nobody():
        return None

    app = FastAPI()
    app.include_router(remote_machines.router)
    app.dependency_overrides[get_current_user] = _nobody
    return TestClient(app, client=(peer, 40000))


def _token(command: str) -> str:
    return command.split("' '", 1)[1].split("'", 1)[0]


def _add(name: str | None = None) -> dict:
    r = _app().post("/v1/admin/remote-machines/compute", json={"name": name or f"rack-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, r.text
    return r.json()


def _pair(added: dict, *, peer="192.168.50.9", secret_hash=_HASH) -> object:
    return _anonymous(peer).post("/v1/satellite/bootstrap", json={"secret_hash": secret_hash},
                                 headers={"X-Pairing-Token": _token(added["install_command"])})


def _values(script: bytes) -> dict:
    line = next(x for x in script.decode().splitlines() if x.startswith("VALUES_B64="))
    decoded = base64.b64decode(line.split("'")[1]).decode()
    return dict(entry.split("=", 1) for entry in decoded.splitlines())


# ---------------------------------------------------------------------------
# adding one
# ---------------------------------------------------------------------------

def test_an_admin_adds_a_machine_and_gets_one_command_and_no_secret(temp_db, lan, told):
    name = f"rack-{uuid.uuid4().hex[:6]}"
    body = _add(name)
    assert body["id"].startswith("compute-") and placement.log_id(body["id"]) == body["id"]
    command = body["install_command"]
    # The token reaches curl on its input: no command line on the server holds it.
    token = _token(command)
    assert command == (f"printf 'X-Pairing-Token: %s\\n' '{token}' | curl -fsSL -H @- "
                       '"http://192.168.50.2:8400/v1/satellite/bootstrap?os=linux" | sudo bash')
    assert body["expires_in_hours"] == remote_store.PAIRING_TOKEN_EXPIRY_HOURS
    assert body["image"] == f"ghcr.io/otodock/otodock-proxy:{app_config.PINNED_OTODOCK_VERSION}"
    assert body["image_public"] is True and body["plain"] is True
    assert "secret" not in json.dumps(body)
    row = remote_store.get_remote_machine(body["id"])
    assert row["pairing_scope"] == "compute" and not row["registered_by"] and not row["compute_address"]
    assert not placement.compute_own_copy(row)  # nothing signs in as it before its pairing
    assert compute_store.pairing_states()[body["id"]] == {"paired": False, "waiting": True}
    assert [c["title"] for c in told] == [f"Compute machine added: {name}"]


@pytest.mark.parametrize("who", [{"api_key": True}, {"role": "member", "sub": "sub-m"}])
def test_a_key_or_a_member_adds_no_compute_machine(temp_db, lan, told, who):
    r = _app(**who).post("/v1/admin/remote-machines/compute", json={"name": f"rack-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 403 and not told


@pytest.mark.parametrize("name", ["", "two\nlines", "tab\there", "x" * 101])
def test_a_name_of_more_than_one_line_is_refused(temp_db, lan, told, name):
    assert _app().post("/v1/admin/remote-machines/compute", json={"name": name}).status_code == 400 and not told


def test_without_the_public_address_nothing_is_added(temp_db, lan, told, monkeypatch):
    before = len(remote_store.get_all_remote_machines())
    monkeypatch.setattr(app_config, "DASHBOARD_PUBLIC_URL", "", raising=False)
    r = _app().post("/v1/admin/remote-machines/compute", json={"name": "rack-x"})
    assert r.status_code == 400 and "DASHBOARD_PUBLIC_URL" in r.json()["detail"]
    assert len(remote_store.get_all_remote_machines()) == before and not told


def test_the_card_reads_the_install_with_or_without_a_machine(temp_db, lan, monkeypatch):
    r = _app().get("/v1/admin/remote-machines/compute")
    assert r.status_code == 200
    assert r.json() == {"image_public": True, "plain": True, "add_refusal": "",
                        "image": f"ghcr.io/otodock/otodock-proxy:{app_config.PINNED_OTODOCK_VERSION}",
                        "own": bool(app_config.OTODOCK_COMPUTE_LINK_DIR), "docker": bool(app_config.RUNNING_IN_DOCKER)}
    # The card names the way out by these two: an install with no compute
    # machine of its own, and where the log of one is read.
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", "", raising=False)
    monkeypatch.setattr(app_config, "RUNNING_IN_DOCKER", False, raising=False)
    said = _app().get("/v1/admin/remote-machines/compute").json()
    assert (said["own"], said["docker"]) == (False, False)
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", "/opt/otodock/compute-link", raising=False)
    monkeypatch.setattr(app_config, "RUNNING_IN_DOCKER", True, raising=False)
    said = _app().get("/v1/admin/remote-machines/compute").json()
    assert (said["own"], said["docker"]) == (True, True)
    monkeypatch.setattr(app_config, "OTODOCK_REMOTE_COMPUTE_IMAGE", "otodock-compute-walk:a", raising=False)
    assert _app().get("/v1/admin/remote-machines/compute").json()["image"] == "otodock-compute-walk:a"
    assert _app().get("/v1/admin/remote-machines/compute").json()["image_public"] is False
    assert _app(role="member", sub="sub-m").get("/v1/admin/remote-machines/compute").status_code == 403


def test_an_install_outside_docker_adds_no_machine_and_says_why(temp_db, lan, told, monkeypatch):
    """A machine on another server runs the platform's image, whose tools sit
    at the image's path; a bare-metal install keeps them elsewhere, so every
    session there would be refused at its start."""
    machine = _paired()
    monkeypatch.setattr(app_config, "MCPS_DIR", Path("/home/someone/oto-dock/mcps"), raising=False)
    said = _app().get("/v1/admin/remote-machines/compute").json()["add_refusal"]
    assert said == compute_pairing.NOT_THIS_INSTALL
    before = len(told)
    r = _app().post("/v1/admin/remote-machines/compute", json={"name": "rack-z"})
    assert r.status_code == 400 and r.json()["detail"] == said
    again = _app().post(f"/v1/admin/remote-machines/compute/{machine['id']}/pair")
    assert again.status_code == 400 and again.json()["detail"] == said
    assert len(told) == before


# ---------------------------------------------------------------------------
# the command: the script, then the exchange
# ---------------------------------------------------------------------------

def test_the_bootstrap_door_serves_the_compute_script_and_exchanges_nothing(temp_db, lan):
    added = _add("Rack `two` $(id) 'q'")
    token = _token(added["install_command"])
    r = _anonymous().get("/v1/satellite/bootstrap?os=linux", headers={"X-Pairing-Token": token})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("text/x-shellscript")
    script = r.content
    assert script.startswith(b"#!/bin/bash\n")
    assert _values(script) == {
        "MACHINE_ID": added["id"], "PAIRING_TOKEN": token, "PLATFORM_URL": "http://192.168.50.2:8400",
        "IMAGE": added["image"], "IMAGE_PUBLIC": "1", "SATELLITE_VERSION": satellite.SATELLITE_VERSION_LATEST}
    # The name never reaches the server, and no secret exists yet.
    assert b"Rack" not in script and b"$(id)" not in script
    assert remote_store.pairing_row(token)["id"] == added["id"]
    assert compute_store.pairing_states()[added["id"]] == {"paired": False, "waiting": True}
    # A second read serves it again; Windows and macOS do not run one.
    assert _anonymous().get("/v1/satellite/bootstrap?os=linux", headers={"X-Pairing-Token": token}).status_code == 200
    for word in ("windows", "macos"):
        assert _anonymous().get(f"/v1/satellite/bootstrap?os={word}",
                                headers={"X-Pairing-Token": token}).status_code == 400


def test_a_value_outside_its_grammar_never_reaches_the_script():
    with pytest.raises(ValueError):
        compute_pairing.bootstrap_script("compute-x`id`", "t" * 43, "192.168.50.2:8400", "0.5.177")
    with pytest.raises(ValueError):
        compute_pairing.bootstrap_script("compute-x", "t" * 43, "host\n:8400", "0.5.177")
    with pytest.raises(ValueError):
        compute_pairing.bootstrap_script("compute-x", "t$(id)" * 8, "192.168.50.2:8400", "0.5.177")


def test_the_exchange_takes_the_servers_hash_records_its_address_and_tells_every_admin(temp_db, lan, told):
    added = _add()
    r = _pair(added)
    assert r.status_code == 200, r.text
    assert r.json() == {"machine_id": added["id"]}
    assert remote_store.verify_machine_secret(added["id"], _SECRET)
    row = remote_store.get_remote_machine(added["id"])
    assert row["compute_address"] == "192.168.50.9" and not row["compute_address_locked"]
    assert placement.compute_own_copy(row)
    assert deployment.is_compute_address("192.168.50.9")
    assert told[-1]["title"].startswith("Compute machine paired:") and "192.168.50.9" in told[-1]["body"]
    assert compute_store.pairing_states()[added["id"]] == {"paired": True, "waiting": False}
    # Once: the token is gone.
    assert _pair(added).status_code == 401


def test_the_exchange_refuses_what_it_does_not_take(temp_db, lan, told):
    added = _add()
    assert _pair(added, secret_hash="not-a-hash").status_code == 401
    assert _pair(added, peer="127.0.0.1").status_code == 403
    assert _pair(added, peer="169.254.3.3").status_code == 403
    # None of them used the token.
    assert _pair(added).status_code == 200
    # A paired machine's token is not exchanged here.
    paired = _app().post("/v1/admin/remote-machines/pair", json={"name": f"pc-{uuid.uuid4().hex[:6]}"})
    token = paired.json()["pairing_token"]
    r = _anonymous().post("/v1/satellite/bootstrap", json={"secret_hash": _HASH}, headers={"X-Pairing-Token": token})
    assert r.status_code == 400 and remote_store.pairing_row(token) is not None
    assert _anonymous().post("/v1/satellite/bootstrap", json={"secret_hash": _HASH}).status_code == 401


def test_an_expired_token_pairs_nothing(temp_db, lan):
    from storage.pg import get_conn
    added = _add()
    with get_conn() as conn:
        conn.execute("UPDATE remote_machines SET pairing_token_created_at = '2020-01-01T00:00:00+00:00' "
                     "WHERE id = %s", (added["id"],))
        conn.commit()
    token = _token(added["install_command"])
    assert _anonymous().get("/v1/satellite/bootstrap?os=linux", headers={"X-Pairing-Token": token}).status_code == 401
    assert _pair(added).status_code == 401
    assert not remote_store.verify_machine_secret(added["id"], _SECRET)


def test_the_exchange_takes_a_hash_only_for_a_compute_row_and_a_secret_only_for_a_paired_one(temp_db):
    pc = remote_store.create_remote_machine(machine_id=str(uuid.uuid4()), name=f"pc-{uuid.uuid4().hex[:6]}",
                                            registered_by="sub-admin")
    with pytest.raises(ValueError):
        remote_store.exchange_pairing_token(pc["id"], pc["pairing_token"], secret_hash=_HASH, address="10.0.0.5")
    rack = remote_store.create_remote_machine(machine_id=f"compute-{uuid.uuid4().hex[:16]}", name="rack-h",
                                              registered_by=None, pairing_scope=placement.PAIRING_COMPUTE, paired=True)
    with pytest.raises(ValueError):
        remote_store.exchange_pairing_token(rack["id"], rack["pairing_token"])
    # The install's own machine gets no token at all.
    own = remote_store.create_remote_machine(machine_id=f"compute-{uuid.uuid4().hex[:12]}", name="own",
                                             registered_by=None, pairing_scope=placement.PAIRING_COMPUTE)
    assert "pairing_token" not in own


# ---------------------------------------------------------------------------
# the address: recorded, locked only on purpose
# ---------------------------------------------------------------------------

def _scope(peer: str) -> dict:
    return {"client": (peer, 40000), "server": ("192.168.50.2", 8400), "headers": []}


class _Socket:
    def __init__(self, peer: str):
        self.scope = _scope(peer)
        self.sent: list = []
        self.closed: tuple = ()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code, reason=""):
        self.closed = (code, reason)


def _paired(peer="192.168.50.9") -> dict:
    added = _add()
    assert _pair(added, peer=peer).status_code == 200
    return remote_store.get_remote_machine(added["id"])


def _sign_in(machine_id: str, peer: str, secret: str = _SECRET):
    ws = _Socket(peer)
    return asyncio.run(satellite._admitted_machine(ws, machine_id, secret, dict(_OWN))), ws


def _seen(machine_id: str) -> None:
    remote_store.update_machine_status(machine_id, remote_store.STATUS_ONLINE, last_seen="2026-11-11T03:00:00+00:00")


def test_by_default_it_signs_in_from_any_address_and_the_last_one_is_recorded(temp_db, lan, told):
    machine = _paired()
    row, ws = _sign_in(machine["id"], "198.51.100.7")
    assert row is not None and row["compute_address"] == "198.51.100.7" and ws.closed == ()
    assert remote_store.get_remote_machine(machine["id"])["compute_address"] == "198.51.100.7"
    assert deployment.is_compute_address("198.51.100.7")
    assert not [c for c in told if c["title"].startswith("Sign-in refused")]


def test_a_bad_secret_records_nothing_and_tells_nobody(temp_db, lan, told):
    machine = _paired()
    before = len(told)
    row, ws = _sign_in(machine["id"], "203.0.113.50", secret="wrong")
    assert row is None and ws.closed == (4001, "Auth rejected")
    assert remote_store.get_remote_machine(machine["id"])["compute_address"] == "192.168.50.9"
    assert len(told) == before


def test_locked_another_address_is_refused_after_the_secret_and_every_admin_is_told_once(temp_db, lan, told,
                                                                                       monkeypatch):
    machine = _paired()
    _seen(machine["id"])
    lock = _app().put(f"/v1/admin/remote-machines/compute/{machine['id']}/address-lock",
                      json={"locked": True, "address": "192.168.50.9"})
    assert lock.status_code == 200, lock.text
    gave_up: list[str] = []
    monkeypatch.setattr(compute_bootstrap, "give_up", gave_up.append)
    row, ws = _sign_in(machine["id"], "203.0.113.50")
    assert row is None and ws.closed == (4009, "not_served")
    assert "locked to 192.168.50.9" in ws.sent[0]["reason"] and "203.0.113.50" in ws.sent[0]["reason"]
    assert gave_up == [machine["id"]]
    refused = [c for c in told if c["title"].startswith("Sign-in refused")]
    assert len(refused) == 1 and refused[0]["scope"] == "admin" and "203.0.113.50" in refused[0]["body"]
    # Within the bound: refused again, nobody told again, the address kept for the next notice.
    assert _sign_in(machine["id"], "203.0.113.51")[0] is None
    assert len([c for c in told if c["title"].startswith("Sign-in refused")]) == 1
    assert compute_pairing.refused_from(machine["id"]) == "203.0.113.51"
    later = compute_pairing._refused[machine["id"]][0] + compute_pairing.NOTICE_EVERY_S + 1
    monkeypatch.setattr(compute_pairing.time, "monotonic", lambda: later)
    assert _sign_in(machine["id"], "203.0.113.52")[0] is None
    refused = [c for c in told if c["title"].startswith("Sign-in refused")]
    assert len(refused) == 2 and "203.0.113.51" in refused[1]["body"]
    # The locked address itself is admitted, and nothing moved.
    assert _sign_in(machine["id"], "192.168.50.9")[0] is not None
    assert remote_store.get_remote_machine(machine["id"])["compute_address"] == "192.168.50.9"


def test_off_and_on_again_pins_the_new_address(temp_db, lan, told):
    machine = _paired()
    _seen(machine["id"])
    url = f"/v1/admin/remote-machines/compute/{machine['id']}/address-lock"
    assert _app().put(url, json={"locked": True, "address": "192.168.50.9"}).status_code == 200
    assert _sign_in(machine["id"], "203.0.113.60")[0] is None
    assert _app().put(url, json={"locked": False}).status_code == 200
    assert compute_pairing.refused_from(machine["id"]) == ""
    assert _sign_in(machine["id"], "203.0.113.60")[0] is not None
    assert _app().put(url, json={"locked": True, "address": "203.0.113.60"}).status_code == 200
    assert _sign_in(machine["id"], "192.168.50.9")[0] is None


def test_the_lock_takes_the_address_the_admin_saw_and_a_first_sign_in(temp_db, lan):
    machine = _paired()
    url = f"/v1/admin/remote-machines/compute/{machine['id']}/address-lock"
    early = _app().put(url, json={"locked": True, "address": "192.168.50.9"})
    assert early.status_code == 409 and "not signed in yet" in early.json()["detail"]
    _seen(machine["id"])
    stale = _app().put(url, json={"locked": True, "address": "10.0.0.1"})
    assert stale.status_code == 409 and "changed to 192.168.50.9" in stale.json()["detail"]
    assert _app(api_key=True).put(url, json={"locked": True, "address": "192.168.50.9"}).status_code == 403
    assert _app(role="member", sub="sub-m").put(url, json={"locked": False}).status_code == 403
    assert not remote_store.get_remote_machine(machine["id"])["compute_address_locked"]


def test_the_install_own_machine_has_no_lock_no_pairing_and_no_command(temp_db, lan, monkeypatch):
    own = remote_store.create_remote_machine(machine_id=f"compute-{uuid.uuid4().hex[:12]}", name="Compute",
                                             registered_by=None, pairing_scope=placement.PAIRING_COMPUTE)
    monkeypatch.setattr(compute_bootstrap, "own_machine_id", lambda: own["id"])
    assert _app().put(f"/v1/admin/remote-machines/compute/{own['id']}/address-lock",
                      json={"locked": True}).status_code == 404
    assert _app().post(f"/v1/admin/remote-machines/compute/{own['id']}/pair").status_code == 404


def test_a_shared_address_signs_in_unlocked_and_cannot_be_locked(temp_db, lan, told, monkeypatch):
    """Every client behind a hop or the container gateway reaches the
    platform from one address: it is recorded and shown, the lock refuses to
    pin it, and the machine still signs in."""
    from auth import lan_check
    real = lan_check.resolve

    def _shared(scope):
        got = real(scope)
        return lan_check.ClientAddress(got.client, got.peer, got.case, shared=True)
    machine = _paired()
    _seen(machine["id"])
    monkeypatch.setattr(lan_check, "resolve", _shared)
    monkeypatch.setattr(lan_check, "_is_hop", lambda addr: addr == "172.20.0.1")
    assert compute_pairing.link_address(_scope("172.20.0.1")) == ("172.20.0.1", "", True)
    row, _ws = _sign_in(machine["id"], "172.20.0.1")
    assert row is not None and row["compute_address"] == "172.20.0.1"
    listed = {m["id"]: m for m in _app().get("/v1/admin/remote-machines").json()["machines"]}
    assert listed[machine["id"]]["compute"]["shared"] is True
    lock = _app().put(f"/v1/admin/remote-machines/compute/{machine['id']}/address-lock",
                      json={"locked": True, "address": "172.20.0.1"})
    assert lock.status_code == 409 and lock.json()["detail"] == (
        "Other clients reach OtoDock from this address too, so it cannot be locked.")
    assert _sign_in(machine["id"], "172.20.0.1")[0] is not None
    assert not [c for c in told if c["title"].startswith("Sign-in refused")]


def test_a_lock_that_meets_a_shared_address_refuses_and_says_why(temp_db, lan, told, monkeypatch):
    """A lock set while the address was the server's own, met later from the
    same address now shared: refused, and the notice says the lock cannot
    hold, never "locked to A, tried from A"."""
    from auth import lan_check
    from storage.pg import get_conn
    real = lan_check.resolve
    machine = _paired()
    with get_conn() as conn:
        conn.execute("UPDATE remote_machines SET compute_address = '172.20.0.1', compute_address_locked = TRUE "
                     "WHERE id = %s", (machine["id"],))
        conn.commit()
    monkeypatch.setattr(lan_check, "resolve", lambda scope: lan_check.ClientAddress(
        real(scope).client, real(scope).peer, real(scope).case, shared=True))
    row, ws = _sign_in(machine["id"], "172.20.0.1")
    assert row is None and "other clients share" in ws.sent[0]["reason"]
    refused = [c for c in told if c["title"].startswith("Sign-in refused")]
    assert len(refused) == 1 and "which other clients share, so its lock cannot hold" in refused[0]["body"]


def test_the_shared_rule_reads_a_hop_or_the_containers_gateway(monkeypatch):
    from auth import lan_check
    monkeypatch.setattr(lan_check, "_is_hop", lambda addr: addr == "10.9.9.9")
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: "172.17.0.1")
    monkeypatch.setattr(lan_check.config, "RUNNING_IN_DOCKER", False, raising=False)
    assert lan_check.address_is_shared("10.9.9.9") and lan_check.address_is_shared("")
    assert not lan_check.address_is_shared("172.17.0.1") and not lan_check.address_is_shared("192.168.50.9")
    monkeypatch.setattr(lan_check.config, "RUNNING_IN_DOCKER", True, raising=False)
    assert lan_check.address_is_shared("172.17.0.1") and lan_check.address_is_shared("::ffff:172.17.0.1")


def test_an_address_that_cannot_be_another_servers_never_pairs_or_signs_in(lan):
    assert compute_pairing.link_address(_scope("192.168.50.9")) == ("192.168.50.9", "", False)
    for peer in ("127.0.0.1", "169.254.1.1", ""):
        address, why, _shared = compute_pairing.link_address(
            {"client": (peer, 0) if peer else None, "server": ("192.168.50.2", 8400), "headers": []})
        assert address == "" and "does not link from" in why


# ---------------------------------------------------------------------------
# pairing again, removing
# ---------------------------------------------------------------------------

def test_pairing_again_makes_a_new_command_and_the_old_sign_in_works_until_it_runs(temp_db, lan, told):
    machine = _paired()
    _seen(machine["id"])
    assert _app().put(f"/v1/admin/remote-machines/compute/{machine['id']}/address-lock",
                      json={"locked": True, "address": "192.168.50.9"}).status_code == 200
    r = _app().post(f"/v1/admin/remote-machines/compute/{machine['id']}/pair")
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["id"] == machine["id"] and "secret" not in json.dumps(again)
    assert told[-1]["title"].startswith("New pairing command:")
    assert remote_store.verify_machine_secret(machine["id"], _SECRET)
    new_secret = "another-secret"
    assert _pair(again, peer="198.51.100.20",
                 secret_hash=hashlib.sha256(new_secret.encode()).hexdigest()).status_code == 200
    assert not remote_store.verify_machine_secret(machine["id"], _SECRET)
    assert remote_store.verify_machine_secret(machine["id"], new_secret)
    row = remote_store.get_remote_machine(machine["id"])
    # A new pairing is a new proof: its address is the new one, unlocked.
    assert row["compute_address"] == "198.51.100.20" and not row["compute_address_locked"]
    assert _app(api_key=True).post(f"/v1/admin/remote-machines/compute/{machine['id']}/pair").status_code == 403
    assert _app().post("/v1/admin/remote-machines/compute/compute-nope/pair").status_code == 404


def test_removing_a_compute_machine_is_a_persons_act_and_drops_a_late_link(temp_db, lan, monkeypatch):
    machine = _paired()
    unlinked: list[str] = []

    async def _unlink(mid):
        unlinked.append(mid)
    monkeypatch.setattr(compute_bootstrap, "unlink", _unlink)
    assert _app(api_key=True).delete(f"/v1/admin/remote-machines/{machine['id']}").status_code == 403
    assert remote_store.get_remote_machine(machine["id"]) is not None and not unlinked
    assert _app().delete(f"/v1/admin/remote-machines/{machine['id']}").status_code == 200
    assert remote_store.get_remote_machine(machine["id"]) is None
    # Before the delete and after it: a link that signed in between is dropped.
    assert unlinked == [machine["id"], machine["id"]]
    assert not deployment.is_compute_address("192.168.50.9")


def test_the_list_says_what_the_card_shows(temp_db, lan):
    added = _add()
    machine = _paired()
    listed = {m["id"]: m for m in _app().get("/v1/admin/remote-machines").json()["machines"]}
    assert listed[added["id"]]["compute"]["waiting"] and not listed[added["id"]]["compute"]["paired"]
    assert listed[machine["id"]]["compute"]["paired"] and not listed[machine["id"]]["compute"]["waiting"]
    assert listed[machine["id"]]["compute_address"] == "192.168.50.9"
    assert listed[machine["id"]]["compute"]["refused_from"] == ""
    assert "machine_secret_hash" not in json.dumps(listed) and "pairing_token" not in json.dumps(listed)


# ---------------------------------------------------------------------------
# the image
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,parts", [
    ("ghcr.io/otodock/otodock-proxy:2.0.0", ("ghcr.io/otodock/otodock-proxy", "2.0.0")),
    ("localhost:5000/otodock:b", ("localhost:5000/otodock", "b")),
    ("otodock-compute-walk:a", ("otodock-compute-walk", "a")),
    ("otodock-compute-walk", None), ("ghcr.io/x@sha256:abc", None), ("Upper/case:1", None),
    ("x:a b", None), ("x:1\nIMAGE=y", None), ("", None),
])
def test_an_image_is_a_repository_and_a_tag(ref, parts):
    assert compute_pairing.split_image(ref) == parts


def test_the_platforms_word_names_a_tag_of_the_same_repository_only(lan, monkeypatch):
    monkeypatch.setattr(app_config, "OTODOCK_REMOTE_COMPUTE_IMAGE", "otodock-compute-walk:b", raising=False)
    latest = "0.5.177"
    same = {"compute_image": "otodock-compute-walk:b"}
    assert compute_pairing.image_refusal(same, latest, latest) is None
    assert compute_pairing.image_refusal({}, "0.5.100", latest) is None  # no claim: today's rule decides
    word = compute_pairing.image_refusal({"compute_image": "otodock-compute-walk:a"}, latest, latest)
    # A doorbell carries nothing: the host asks the platform for the tag.
    assert word["action"] == "update_image" and "image_tag" not in word
    # The same tag on another satellite would loop on the word: a sentence alone.
    stale = compute_pairing.image_refusal(same, "0.5.176", latest)
    assert "action" not in stale and "new tag" in stale["reason"]
    other = compute_pairing.image_refusal({"compute_image": "evil/otodock:b"}, latest, latest)
    assert "action" not in other and "Pair it again" in other["reason"]


def test_a_machine_on_another_tag_is_refused_with_the_word_at_its_sign_in(temp_db, lan, monkeypatch):
    """The handler's compute gate: the refusal carries the tag, and the
    boot's wait gives the machine up."""
    machine = _paired()
    monkeypatch.setattr(app_config, "OTODOCK_REMOTE_COMPUTE_IMAGE", "otodock-compute-walk:b", raising=False)
    gave_up: list[str] = []
    monkeypatch.setattr(compute_bootstrap, "give_up", gave_up.append)
    caps = {**_OWN, "compute_image": "otodock-compute-walk:a"}

    class _Ws(_Socket):
        async def accept(self):
            pass

        async def receive_text(self):
            return json.dumps({"type": "auth", "machine_id": machine["id"], "machine_secret": _SECRET,
                               "capabilities": caps, "satellite_version": satellite.SATELLITE_VERSION_LATEST})
    ws = _Ws("192.168.50.9")
    asyncio.run(satellite._ws_satellite_handler(ws, lambda: None))
    assert ws.closed == (4001, "version mismatch")
    assert ws.sent[-1]["action"] == "update_image" and "image_tag" not in ws.sent[-1]
    assert gave_up == [machine["id"]]


def test_a_refused_machine_stops_the_boots_wait_for_it():
    compute_bootstrap.reset_for_tests()
    compute_bootstrap._expected.update({"compute-a", "compute-b"})
    compute_bootstrap._back = asyncio.Event()
    compute_bootstrap.give_up("compute-a")
    assert not compute_bootstrap._back.is_set()
    compute_bootstrap.give_up("compute-b")
    assert compute_bootstrap._back.is_set()
    compute_bootstrap.give_up("compute-c")  # not expected: nothing
    compute_bootstrap.reset_for_tests()


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------

def test_the_boot_that_adds_the_lock_clears_a_never_used_sign_in_and_later_boots_do_not(temp_db):
    from storage import remote_schema
    from storage.pg import get_conn
    unused = _paired_row_raw("192.168.10.1", last_seen=None)
    used = _paired_row_raw("192.168.10.2", last_seen="2026-11-10T00:00:00+00:00")
    with get_conn() as conn:
        assert remote_schema.clear_unused_sign_ins(conn) == 1
        conn.commit()
    assert not remote_store.verify_machine_secret(unused, _SECRET)
    assert remote_store.verify_machine_secret(used, _SECRET)
    # The column is there: a later boot leaves a fresh pairing's sign-in alone.
    with get_conn() as conn:
        conn.execute("UPDATE remote_machines SET machine_secret_hash = %s WHERE id = %s", (_HASH, unused))
        remote_schema.migrate_remote(conn)
        conn.commit()
    assert remote_store.verify_machine_secret(unused, _SECRET)


def _paired_row_raw(address: str, *, last_seen: str | None) -> str:
    made = remote_store.create_remote_machine(machine_id=f"compute-{uuid.uuid4().hex[:16]}",
                                              name=f"rack-{uuid.uuid4().hex[:6]}", registered_by=None,
                                              pairing_scope=placement.PAIRING_COMPUTE, paired=True)
    remote_store.exchange_pairing_token(made["id"], made["pairing_token"], secret_hash=_HASH, address=address)
    if last_seen:
        remote_store.update_machine_status(made["id"], remote_store.STATUS_ONLINE, last_seen=last_seen)
    return made["id"]


# ---------------------------------------------------------------------------
# the image this install runs, the host's questions, a removal
# ---------------------------------------------------------------------------

def test_the_image_is_the_one_this_install_runs_rc_tags_included(lan, monkeypatch):
    monkeypatch.setattr(app_config, "OTODOCK_PROXY_IMAGE", "ghcr.io/otodock/otodock-proxy:2.0.0-rc1", raising=False)
    image = compute_pairing.wanted_image()
    assert image is not None and image.ref == "ghcr.io/otodock/otodock-proxy:2.0.0-rc1" and image.public
    script = compute_pairing.bootstrap_script("compute-x", "t" * 43, "192.168.50.2:8400", "0.5.177")
    assert _values(script)["IMAGE"] == "ghcr.io/otodock/otodock-proxy:2.0.0-rc1"
    # The key wins, and a loaded image is never downloaded.
    monkeypatch.setattr(app_config, "OTODOCK_REMOTE_COMPUTE_IMAGE", "otodock-compute-walk:a", raising=False)
    image = compute_pairing.wanted_image()
    assert image is not None and image.ref == "otodock-compute-walk:a" and not image.public


def test_an_install_built_from_source_that_names_no_image_adds_none(temp_db, lan, told, monkeypatch):
    """The build overlay empties the proxy's own image: with no key either,
    the add and Pair again refuse in one sentence, and a machine that still
    signs in is told so with no word to follow."""
    machine = _paired()
    monkeypatch.setattr(app_config, "OTODOCK_PROXY_IMAGE", "", raising=False)
    assert compute_pairing.wanted_image() is None
    assert compute_pairing.add_refusal() == compute_pairing.NO_IMAGE
    settings = _app().get("/v1/admin/remote-machines/compute").json()
    assert settings["add_refusal"] == compute_pairing.NO_IMAGE and settings["image"] == ""
    assert _app().post("/v1/admin/remote-machines/compute", json={"name": "rack-q"}).json()["detail"] == (
        "This install was built from source. Set OTODOCK_REMOTE_COMPUTE_IMAGE to the image you load on its servers.")
    assert _app().post(f"/v1/admin/remote-machines/compute/{machine['id']}/pair").status_code == 400
    refused = compute_pairing.image_refusal({"compute_image": "x/y:1"}, "0.5.177", "0.5.177")
    assert "action" not in refused and "OTODOCK_REMOTE_COMPUTE_IMAGE" in refused["reason"]
    monkeypatch.setattr(app_config, "OTODOCK_REMOTE_COMPUTE_IMAGE", "no tag here", raising=False)
    assert compute_pairing.add_refusal() == compute_pairing.BAD_IMAGE


def test_a_token_minted_earlier_gets_no_script_once_the_install_names_no_image(temp_db, lan, monkeypatch):
    added = _add()
    monkeypatch.setattr(app_config, "OTODOCK_PROXY_IMAGE", "", raising=False)
    r = _anonymous().get("/v1/satellite/bootstrap?os=linux", headers={"X-Pairing-Token": _token(added["install_command"])})
    assert r.status_code == 400 and "OTODOCK_REMOTE_COMPUTE_IMAGE" in r.json()["detail"]


def test_an_https_install_pins_the_command_to_https(lan, monkeypatch):
    assert "--proto" not in compute_pairing.install_command("192.168.50.2:8400", "t" * 43)
    monkeypatch.setattr(app_config, "DASHBOARD_PUBLIC_URL", "https://agents.example.com", raising=False)
    command = compute_pairing.install_command("agents.example.com", "t" * 43)
    assert "curl -fsSL --proto '=https' -H @- \"https://agents.example.com/v1/satellite/bootstrap?os=linux\"" in command


def _host_answer(machine_id: str, secret: str):
    return _anonymous().get("/v1/satellite/compute-host", headers={"X-Machine-Id": machine_id,
                                                                   "X-Machine-Secret": secret})


def test_the_host_asks_the_platform_with_the_machines_sign_in(temp_db, lan):
    machine = _paired()
    r = _host_answer(machine["id"], _SECRET)
    assert r.status_code == 200 and r.json() == {
        "image": f"{compute_pairing.PUBLIC_REPOSITORY}:{app_config.PINNED_OTODOCK_VERSION}",
        "satellite_version": satellite.SATELLITE_VERSION_LATEST}
    assert _host_answer(machine["id"], "wrong").status_code == 401
    assert _host_answer("compute-nope", _SECRET).status_code == 401
    assert _anonymous().get("/v1/satellite/compute-host").status_code == 401


def test_the_host_question_never_answers_a_paired_computers_sign_in(temp_db, lan):
    pc = remote_store.create_remote_machine(machine_id=str(uuid.uuid4()), name=f"pc-{uuid.uuid4().hex[:6]}",
                                            registered_by="sub-admin")
    secret = remote_store.exchange_pairing_token(pc["id"], pc["pairing_token"])
    assert secret and remote_store.verify_machine_secret(pc["id"], secret)
    r = _host_answer(pc["id"], secret)
    assert r.status_code == 401 and r.json() == {"detail": "Invalid credentials"}


@pytest.mark.parametrize("claim", ["x", "1", [1], {"a": 1}, "9" * 5000, True, -1, 0, 10 ** 30],
                         ids=["word", "digits", "list", "object", "long", "bool", "negative", "zero", "huge"])
def test_a_host_claim_that_is_not_a_small_number_never_breaks_the_list(temp_db, lan, claim):
    """The claim is the machine's own word: whatever it holds, every admin
    still sees the list (and can remove the machine)."""
    machine = _paired()
    remote_store.update_machine_capabilities(machine["id"], {**_OWN, "compute_host": claim})
    r = _app().get("/v1/admin/remote-machines")
    assert r.status_code == 200, r.text
    listed = {m["id"]: m for m in r.json()["machines"]}
    assert listed[machine["id"]]["compute"]["host_behind"] is False


@pytest.mark.parametrize("claim,kept", [("x", False), ([1], False), ({"a": 1}, False), ("9" * 5000, False),
                                        (True, False), (10 ** 30, False), (1, True)],
                         ids=["word", "list", "object", "long", "bool", "huge", "one"])
def test_a_sign_in_keeps_a_host_claim_only_as_a_small_number(temp_db, lan, monkeypatch, claim, kept):
    """What the sign-in keeps is what the row gets (register persists the
    same capabilities), so a bad claim never reaches it."""
    machine = _paired()
    seen: list[dict] = []

    def _refuse_here(capabilities, _version, _latest):
        seen.append(dict(capabilities))
        return {"reason": "stop here"}
    monkeypatch.setattr(compute_pairing, "image_refusal", _refuse_here)
    caps = {**_OWN, "compute_image": "otodock-compute-walk:a", "compute_host": claim}

    class _Ws(_Socket):
        async def accept(self):
            pass

        async def receive_text(self):
            return json.dumps({"type": "auth", "machine_id": machine["id"], "machine_secret": _SECRET,
                               "capabilities": caps, "satellite_version": satellite.SATELLITE_VERSION_LATEST})
    asyncio.run(satellite._ws_satellite_handler(_Ws("192.168.50.9"), lambda: None))
    assert len(seen) == 1 and ("compute_host" in seen[0]) is kept
    assert seen[0]["compute_image"] == "otodock-compute-walk:a"


def test_a_removed_machine_is_told_so_only_with_its_own_secret_and_for_thirty_days(temp_db, lan, monkeypatch):
    from storage import compute_store
    from storage.pg import get_conn

    async def _unlink(_mid):
        return None
    monkeypatch.setattr(compute_bootstrap, "unlink", _unlink)
    machine = _paired()
    assert _app().delete(f"/v1/admin/remote-machines/{machine['id']}").status_code == 200
    r = _host_answer(machine["id"], _SECRET)
    assert r.status_code == 410 and r.json() == {"removed": True}
    assert _host_answer(machine["id"], "wrong").status_code == 401
    # Its sign-in hears its removal; a wrong secret is refused as any wrong secret is.
    row, ws = _sign_in(machine["id"], "192.168.50.9")
    assert row is None and ws.closed == (4006, "machine_deleted") and ws.sent[0]["action"] == "uninstall"
    row, ws = _sign_in(machine["id"], "192.168.50.9", secret="wrong")
    assert row is None and ws.closed == (4001, "Auth rejected") and "action" not in ws.sent[0]
    with get_conn() as conn:
        conn.execute("UPDATE removed_compute_machines SET removed_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
    assert not compute_store.removed_verifies(machine["id"], _SECRET)
    assert _host_answer(machine["id"], _SECRET).status_code == 401


def test_an_id_this_database_does_not_know_is_never_told_to_remove_itself(temp_db, lan):
    """A restored database or another install at the same address: a
    compute claim with an unknown id gets a plain refusal and waits; a paired
    computer's unknown id is still told to uninstall, as before."""
    row, ws = _sign_in(f"compute-{uuid.uuid4().hex[:16]}", "192.168.50.9")
    assert row is None and ws.closed == (4001, "Auth rejected")
    assert ws.sent[0]["reason"] == "Invalid credentials" and "action" not in ws.sent[0]
    paired = _Socket("192.168.50.9")
    asyncio.run(satellite._admitted_machine(paired, str(uuid.uuid4()), "s", {}))
    assert paired.closed == (4006, "machine_deleted") and paired.sent[0]["action"] == "uninstall"


def test_one_maker_mints_every_pairing_token(temp_db, lan, monkeypatch):
    made: list[str] = []
    real = remote_store._new_pairing_token

    def _counting():
        out = real()
        made.append(out[0])
        return out
    monkeypatch.setattr(remote_store, "_new_pairing_token", _counting)
    added = _add()
    again = _app().post(f"/v1/admin/remote-machines/compute/{added['id']}/pair").json()
    pc = _app().post("/v1/admin/remote-machines/pair", json={"name": f"pc-{uuid.uuid4().hex[:6]}"}).json()
    assert made == [_token(added["install_command"]), _token(again["install_command"]), pc["pairing_token"]]


def test_the_card_learns_a_host_older_than_this_platform(temp_db, lan, monkeypatch):
    machine = _paired()
    remote_store.update_machine_capabilities(machine["id"], {"compute": True, "compute_host": 1})
    listed = {m["id"]: m for m in _app().get("/v1/admin/remote-machines").json()["machines"]}
    assert listed[machine["id"]]["compute"]["host_behind"] is False
    monkeypatch.setattr(compute_pairing, "HOST_SHAPE", 2)
    listed = {m["id"]: m for m in _app().get("/v1/admin/remote-machines").json()["machines"]}
    assert listed[machine["id"]]["compute"]["host_behind"] is True
