"""A machine nobody owns: the schema, the store, and each writer that refuses it.

A compute machine is registered with no owner (``registered_by`` NULL, scope
``compute``), no pairing token and no secret, and the platform serves nothing
on it yet. Real temp DB: the migration runs on a table shaped as a 1.9.1
install has it, with its paired machines.
"""

from __future__ import annotations

import uuid

import pytest

from core import placement
from storage import remote_schema, remote_store
from storage.pg import get_conn


def _compute(name: str = "") -> dict:
    return remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()), name=name or f"compute-{uuid.uuid4().hex[:6]}",
        registered_by=None, pairing_scope=placement.PAIRING_COMPUTE,
    )


def _paired(scope: str, owner: str = "sub-owner") -> dict:
    return remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()), name=f"{scope}-{uuid.uuid4().hex[:6]}",
        registered_by=owner, pairing_scope=scope,
    )


def _owner_column_not_null(conn) -> bool:
    row = conn.execute(
        "SELECT a.attnotnull FROM pg_attribute a JOIN pg_class t ON t.oid = a.attrelid "
        "WHERE t.relname = 'remote_machines' AND a.attname = 'registered_by' AND NOT a.attisdropped"
    ).fetchone()
    return bool(row["attnotnull"])


def _owner_check_present(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s", (remote_schema.OWNER_CHECK,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# the schema moves forward on a 1.9.1 table
# ---------------------------------------------------------------------------

def test_the_owner_rule_moves_forward_on_a_191_table_and_keeps_its_machines(temp_db):
    admin, person = _paired(placement.PAIRING_ADMIN, "sub-admin"), _paired(placement.PAIRING_USER, "sub-alice")
    with get_conn() as conn:
        # The table as 1.9.1 left it: the owner required, no rule by name.
        conn.execute(f"ALTER TABLE remote_machines DROP CONSTRAINT IF EXISTS {remote_schema.OWNER_CHECK}")
        conn.execute("ALTER TABLE remote_machines ALTER COLUMN registered_by SET NOT NULL")
        conn.commit()
        assert _owner_column_not_null(conn) and not _owner_check_present(conn)
        remote_schema.migrate_remote(conn)
        conn.commit()
        assert not _owner_column_not_null(conn) and _owner_check_present(conn)
        remote_schema.migrate_remote(conn)  # a second boot changes nothing and does not fail
        conn.commit()
    got = {m["id"]: m for m in remote_store.get_all_remote_machines()}
    assert got[admin["id"]]["registered_by"] == "sub-admin"
    assert got[admin["id"]]["pairing_scope"] == placement.PAIRING_ADMIN
    assert got[person["id"]]["registered_by"] == "sub-alice"
    assert got[person["id"]]["pairing_scope"] == placement.PAIRING_USER


def test_only_a_compute_machine_may_have_no_owner(temp_db):
    import psycopg
    with get_conn() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO remote_machines (id, name, status, registered_by, pairing_scope, capabilities, "
            "created_at) VALUES (%s, %s, 'offline', NULL, 'admin', '{}', '2026-11-07')",
            (str(uuid.uuid4()), f"ownerless-{uuid.uuid4().hex[:6]}"))


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def test_a_compute_machine_has_no_owner_no_token_and_no_update(temp_db):
    made = _compute()
    assert "pairing_token" not in made
    assert made["registered_by"] == "" and made["pairing_scope"] == placement.PAIRING_COMPUTE
    assert made["auto_update_enabled"] is False and made["allow_full_fs"] is False
    with get_conn() as conn:
        raw = conn.execute(
            "SELECT registered_by, pairing_token_hash, machine_secret_hash FROM remote_machines WHERE id = %s",
            (made["id"],)).fetchone()
    assert raw["registered_by"] is None
    assert raw["pairing_token_hash"] is None and raw["machine_secret_hash"] is None
    # nothing can sign in as it, and no token exchanges for a secret
    assert not remote_store.verify_machine_secret(made["id"], "anything")
    with pytest.raises(ValueError):
        remote_store.exchange_pairing_token(made["id"], "anything")


def test_every_row_reader_gives_nobody_as_an_empty_owner(temp_db):
    made = _compute()
    for row in (remote_store.get_remote_machine(made["id"]),
                next(m for m in remote_store.get_all_remote_machines() if m["id"] == made["id"])):
        assert row["registered_by"] == "" and row["owner_role"] == ""
    assert placement.owner_of(remote_store.get_remote_machine(made["id"])) == ""


def test_a_paired_machine_still_needs_the_person_who_paired_it(temp_db):
    with pytest.raises(ValueError):
        remote_store.create_remote_machine(machine_id=str(uuid.uuid4()), name="x", registered_by="",
                                           pairing_scope=placement.PAIRING_ADMIN)


def test_a_compute_rows_placement_is_its_own_kind_with_no_row_facts(temp_db):
    made = _compute("Compute one")
    with get_conn() as conn:
        conn.execute("UPDATE remote_machines SET capabilities = %s, device_grants = %s WHERE id = %s",
                     ('{"os": "linux", "home_dir": "/home/sat", "agents_dir": "/home/sat/a"}',
                      '["computer"]', made["id"]))
        conn.commit()
    p = remote_store.placement_of(made["id"], "sub-alice", "agent-a")
    assert p == placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id=made["id"],
                                                label="Compute one")


_SETTINGS = (
    ("set_allow_full_fs", (True,)),
    ("set_remote_machine_max_sessions", (4,)),
    ("set_device_grants", (["computer"],)),
    ("set_browser_mode", ("own",)),
    ("set_browser_extension_token", ("tok",)),
    ("set_browser_profile", ("Work",)),
    ("set_browser_unattended", (True,)),
    ("set_all_roles_run_here", (True,)),
    ("set_auto_update_enabled", (True,)),
    ("set_pending_update", (True,)),
)


@pytest.mark.parametrize("writer, args", _SETTINGS)
def test_a_compute_machine_takes_no_setting_of_a_paired_machine(temp_db, writer, args):
    made = _compute()
    with pytest.raises(remote_store.ComputeMachineRefused) as refused:
        getattr(remote_store, writer)(made["id"], *args)
    assert str(refused.value) == remote_store.COMPUTE_SETTING_REFUSED
    after = remote_store.get_remote_machine(made["id"])
    assert not after["allow_full_fs"] and not after["all_roles_run_here"]
    assert not after["auto_update_enabled"] and not after["pending_update"]


@pytest.mark.parametrize("writer, args", _SETTINGS)
def test_an_admins_machine_still_takes_its_settings(temp_db, writer, args):
    made = _paired(placement.PAIRING_ADMIN)
    getattr(remote_store, writer)(made["id"], *args)


def test_a_compute_machine_is_no_agents_default_place(temp_db):
    from storage.agents import agent_store
    agent_store.create_agent("nobody-agent", "Nobody agent")
    made = _compute()
    with pytest.raises(remote_store.ComputeMachineRefused) as refused:
        remote_store.set_agent_remote_target("nobody-agent", made["id"], "sub-admin")
    assert str(refused.value) == remote_store.COMPUTE_PLACE_REFUSED
    assert remote_store.get_agents_for_machine(made["id"]) == []
    assert placement.is_local((agent_store.get_agent("nobody-agent") or {}).get("execution_target"))


def test_a_compute_machine_is_no_persons_own_place(temp_db):
    made = _compute()
    with pytest.raises(remote_store.ComputeMachineRefused):
        remote_store.set_user_remote_target("sub-admin", made["id"], "any-agent")
    assert remote_store.get_user_remote_targets("sub-admin") == []


def test_a_person_never_sees_a_compute_machine_among_their_own(temp_db):
    made = _compute()
    for admin_view in (False, True):
        ids = {m["id"] for m in remote_store.get_visible_machines_for_user("sub-admin",
                                                                          include_admin_paired=admin_view)}
        assert made["id"] not in ids
