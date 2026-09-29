"""Own-browser mode for browser-control — the per-machine opt-in + token.

Store rules (real temp DB): the mode parses fail-closed, the token is
encrypted at rest and decrypted only by the targeted settings read, leaving
``own`` or revoking the ``browser`` grant clears the token, a local target
never reads the DB. Endpoint rules (router + stubbed store): the consent
owner of the machine (owner for user-paired, admin for admin-paired) is the
only role that may flip the mode or store a token; ``own`` needs the grant;
a token needs ``own``; the extension's ``NAME=value`` paste is accepted.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import placement
from storage import remote_store


def _p(kind: str, machine_id: str):
    return placement.PlacementCapabilities(kind=kind, machine_id=machine_id)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@pytest.fixture
def machine(temp_db):
    return remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()),
        name=f"own-browser-{uuid.uuid4().hex[:6]}",
        registered_by="user-sub-admin",
    )


def test_parse_browser_mode_fails_closed():
    assert remote_store._parse_browser_mode("own") == "own"
    for raw in (None, "", "dedicated", "OWN", "extension", 1):
        assert remote_store._parse_browser_mode(raw) == "dedicated"


def test_defaults_are_dedicated_without_token(machine):
    row = remote_store.get_remote_machine(machine["id"])
    assert row["browser_mode"] == "dedicated"
    assert row["browser_extension_token_set"] is False
    s = remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, machine["id"]))
    assert (s.mode, s.extension_token) == ("dedicated", None)


def test_token_is_encrypted_at_rest_and_decrypted_only_when_own(machine):
    from storage.pg import get_conn
    remote_store.set_device_grants(machine["id"], ["browser"])
    remote_store.set_browser_mode(machine["id"], "own")
    remote_store.set_browser_extension_token(machine["id"], "A" * 43)
    with get_conn() as conn:
        enc = conn.execute(
            "SELECT browser_extension_token_enc FROM remote_machines WHERE id = %s",
            (machine["id"],),
        ).fetchone()["browser_extension_token_enc"]
    assert enc and "A" * 43 not in enc  # ciphertext, not the value
    assert remote_store.get_target_browser_settings(_p(placement.KIND_USER_REMOTE, machine["id"])).extension_token == "A" * 43
    # Back to dedicated: the token STAYS stored (it belongs to the machine)
    # but is not delivered — only own mode reads it; switching back to own
    # needs no re-paste.
    remote_store.set_browser_mode(machine["id"], "dedicated")
    assert remote_store.get_remote_machine(machine["id"])["browser_extension_token_set"] is True
    assert remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, machine["id"])).extension_token is None
    remote_store.set_browser_mode(machine["id"], "own")
    assert remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, machine["id"])).extension_token == "A" * 43


def test_revoking_browser_grant_resets_mode_and_token(machine):
    remote_store.set_device_grants(machine["id"], ["browser", "computer"])
    remote_store.set_browser_mode(machine["id"], "own")
    remote_store.set_browser_extension_token(machine["id"], "B" * 43)
    # Keeping the grant keeps the consent unit.
    remote_store.set_device_grants(machine["id"], ["browser"])
    row = remote_store.get_remote_machine(machine["id"])
    assert row["browser_mode"] == "own" and row["browser_extension_token_set"] is True
    # Dropping it resets both — a later re-grant starts dedicated.
    remote_store.set_device_grants(machine["id"], ["computer"])
    row = remote_store.get_remote_machine(machine["id"])
    assert row["browser_mode"] == "dedicated" and row["browser_extension_token_set"] is False
    assert remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, machine["id"])).mode == "dedicated"


def test_clear_token_keeps_mode(machine):
    remote_store.set_device_grants(machine["id"], ["browser"])
    remote_store.set_browser_mode(machine["id"], "own")
    remote_store.set_browser_extension_token(machine["id"], "C" * 43)
    remote_store.set_browser_extension_token(machine["id"], None)
    s = remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, machine["id"]))
    assert (s.mode, s.extension_token) == ("own", None)


def test_set_browser_mode_rejects_unknown(machine):
    with pytest.raises(ValueError):
        remote_store.set_browser_mode(machine["id"], "extension")


def test_local_or_unknown_target_is_dedicated(machine, monkeypatch):
    from storage import pg as _pg

    def _boom(*a, **k):
        raise AssertionError("local target must not read the DB")
    monkeypatch.setattr(_pg, "get_conn", _boom)
    assert remote_store.get_target_browser_settings(placement.LOCAL_PLACEMENT) == remote_store.BrowserTargetSettings()
    assert remote_store.get_target_browser_settings(_p(placement.KIND_LOCAL, "m")) == remote_store.BrowserTargetSettings()
    monkeypatch.undo()
    assert remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, str(uuid.uuid4()))).mode == "dedicated"


def test_undecryptable_token_degrades_to_no_token(machine, monkeypatch):
    from storage.identity import credential_store
    remote_store.set_device_grants(machine["id"], ["browser"])
    remote_store.set_browser_mode(machine["id"], "own")
    remote_store.set_browser_extension_token(machine["id"], "D" * 43)

    def _bad(_enc):
        raise ValueError("key mismatch")
    monkeypatch.setattr(credential_store, "decrypt_secret", _bad)
    s = remote_store.get_target_browser_settings(_p(placement.KIND_ADMIN_REMOTE, machine["id"]))
    assert (s.mode, s.extension_token) == ("own", None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _make_app(monkeypatch, *, role, sub, machine):
    """Mount remote_machines.router with the caller identity + a stubbed
    machine row; record the store writes."""
    from api.remote import remote_machines as rm
    from auth.providers import UserContext, get_current_user

    user = UserContext(sub=sub, email="u@test.com", name="U", role=role,
                       agents=[], agent_roles={})

    async def _stub_user():
        return user

    writes: list = []
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: dict(machine) if mid == machine["id"] else None)
    monkeypatch.setattr(remote_store, "set_browser_mode", lambda mid, mode: writes.append(("mode", mid, mode)))
    monkeypatch.setattr(remote_store, "set_browser_extension_token", lambda mid, tok: writes.append(("token", mid, tok)))

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return TestClient(app), writes


def _machine(**over):
    base = {
        "id": "machine-1", "name": "box", "pairing_scope": "admin",
        "registered_by": "user-sub-admin", "device_grants": '["browser"]',
        "browser_mode": "own", "browser_extension_token_set": False,
    }
    base.update(over)
    return base


ADMIN = "/v1/admin/remote-machines/machine-1"
MINE = "/v1/users/me/remote-machines/machine-1"


def test_admin_sets_mode_on_admin_paired_machine(monkeypatch):
    client, writes = _make_app(monkeypatch, role="admin", sub="user-sub-admin",
                               machine=_machine(browser_mode="dedicated"))
    r = client.put(f"{ADMIN}/browser-mode", json={"mode": "own"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "browser_mode": "own", "browser_extension_token_set": False}
    assert writes == [("mode", "machine-1", "own")]


def test_own_requires_browser_grant(monkeypatch):
    client, writes = _make_app(monkeypatch, role="admin", sub="user-sub-admin",
                               machine=_machine(device_grants='["computer"]', browser_mode="dedicated"))
    r = client.put(f"{ADMIN}/browser-mode", json={"mode": "own"})
    assert r.status_code == 422
    assert "Grant browser control" in r.json()["detail"]
    assert writes == []


def test_unknown_mode_rejected(monkeypatch):
    client, writes = _make_app(monkeypatch, role="admin", sub="user-sub-admin", machine=_machine())
    assert client.put(f"{ADMIN}/browser-mode", json={"mode": "extension"}).status_code == 422
    assert writes == []


def test_admin_cannot_touch_user_paired_machine(monkeypatch):
    client, writes = _make_app(monkeypatch, role="admin", sub="user-sub-admin",
                               machine=_machine(pairing_scope="user", registered_by="user-sub-owner"))
    assert client.put(f"{ADMIN}/browser-mode", json={"mode": "dedicated"}).status_code == 403
    assert client.put(f"{ADMIN}/browser-token", json={"token": "E" * 43}).status_code == 403
    assert client.delete(f"{ADMIN}/browser-token").status_code == 403
    assert writes == []


def test_non_admin_gets_403_on_admin_routes(monkeypatch):
    client, writes = _make_app(monkeypatch, role="creator", sub="user-sub-owner", machine=_machine())
    assert client.put(f"{ADMIN}/browser-mode", json={"mode": "own"}).status_code == 403
    assert writes == []


def test_owner_sets_mode_and_token_on_own_machine(monkeypatch):
    client, writes = _make_app(monkeypatch, role="creator", sub="user-sub-owner",
                               machine=_machine(pairing_scope="user", registered_by="user-sub-owner"))
    r = client.put(f"{MINE}/browser-token", json={"token": "F" * 43})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "browser_extension_token_set": True}
    r = client.delete(f"{MINE}/browser-token")
    assert r.status_code == 200 and r.json()["browser_extension_token_set"] is False
    r = client.put(f"{MINE}/browser-mode", json={"mode": "dedicated"})
    assert r.status_code == 200 and r.json()["browser_mode"] == "dedicated"
    assert writes == [
        ("token", "machine-1", "F" * 43),
        ("token", "machine-1", None),
        ("mode", "machine-1", "dedicated"),
    ]


def test_owner_routes_reject_admin_paired_and_foreign_machines(monkeypatch):
    client, writes = _make_app(monkeypatch, role="creator", sub="user-sub-owner", machine=_machine())
    r = client.put(f"{MINE}/browser-mode", json={"mode": "own"})
    assert r.status_code == 403 and "admin-paired" in r.json()["detail"]
    client, writes2 = _make_app(monkeypatch, role="creator", sub="user-sub-other",
                                machine=_machine(pairing_scope="user", registered_by="user-sub-owner"))
    assert client.put(f"{MINE}/browser-token", json={"token": "G" * 43}).status_code == 403
    assert writes == [] and writes2 == []


def test_token_requires_own_mode(monkeypatch):
    client, writes = _make_app(monkeypatch, role="admin", sub="user-sub-admin",
                               machine=_machine(browser_mode="dedicated"))
    r = client.put(f"{ADMIN}/browser-token", json={"token": "H" * 43})
    assert r.status_code == 422 and "own browser" in r.json()["detail"]
    # Clearing is always allowed (idempotent tidy-up).
    assert client.delete(f"{ADMIN}/browser-token").status_code == 200
    assert writes == [("token", "machine-1", None)]


def test_token_paste_normalisation(monkeypatch):
    client, writes = _make_app(monkeypatch, role="admin", sub="user-sub-admin", machine=_machine())
    tok = "Aa0-_" + "b" * 38  # 43 chars, base64url alphabet — the extension's shape
    for pasted in (tok, f"PLAYWRIGHT_MCP_EXTENSION_TOKEN={tok}", f'  "PLAYWRIGHT_MCP_EXTENSION_TOKEN={tok}"\n', f"'{tok}'"):
        assert client.put(f"{ADMIN}/browser-token", json={"token": pasted}).status_code == 200, pasted
    assert [w[2] for w in writes] == [tok] * 4
    for bad in ("", "short", "has space " + tok, tok + "!", "x" * 300):
        assert client.put(f"{ADMIN}/browser-token", json={"token": bad}).status_code == 422, bad
    assert len(writes) == 4


def test_machine_rows_expose_flag_not_ciphertext(monkeypatch):
    # The list/get endpoints normalise the two fields; the ciphertext column
    # is already stripped by the store (see test_machine_row_sanitization).
    from api.remote import remote_machines as rm
    m = _machine(browser_mode="bogus", browser_extension_token_set=None)
    rm._shape_browser_fields(m)
    assert m["browser_mode"] == "dedicated" and m["browser_extension_token_set"] is False
    m = _machine(browser_extension_token_set=True)
    rm._shape_browser_fields(m)
    assert m["browser_mode"] == "own" and m["browser_extension_token_set"] is True
