"""Machine rows leaving the storage layer carry no credential columns.

``pairing_token_hash`` / ``pairing_token_created_at`` / ``machine_secret_hash``
are read only by the verifiers' own targeted SELECTs; every row-returning
store function strips them, so API/WS callers can forward rows (admin list,
per-machine GET, /v1/users/me/remote-machines, warmup/discovery events)
without re-sanitizing. Real temp DB — the whole pairing flow runs end-to-end
to prove the verifiers don't depend on the stripped functions.
"""

import uuid

import pytest

from storage import remote_store

SECRET_COLUMNS = (
    "pairing_token_hash",
    "pairing_token_created_at",
    "machine_secret_hash",
)


def _assert_clean(row: dict) -> None:
    leaked = [c for c in SECRET_COLUMNS if c in row]
    assert not leaked, f"secret columns leaked from storage layer: {leaked}"


@pytest.fixture
def machine(temp_db):
    result = remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()),
        name=f"sanitize-probe-{uuid.uuid4().hex[:6]}",
        registered_by="user-sub-admin",
    )
    return result


def test_create_result_is_clean_but_has_plaintext_token(machine):
    _assert_clean(machine)
    assert machine["pairing_token"]  # the one legitimate secret: shown once


def test_get_remote_machine_clean(machine):
    row = remote_store.get_remote_machine(machine["id"])
    assert row is not None
    _assert_clean(row)


def test_get_all_remote_machines_clean(machine):
    rows = remote_store.get_all_remote_machines()
    assert any(r["id"] == machine["id"] for r in rows)
    for r in rows:
        _assert_clean(r)


def test_user_visible_and_user_paired_lists_clean(temp_db):
    result = remote_store.create_remote_machine(
        machine_id=str(uuid.uuid4()),
        name=f"sanitize-user-{uuid.uuid4().hex[:6]}",
        registered_by="user-sub-owner",
        pairing_scope="user",
    )
    for rows in (
        remote_store.get_visible_machines_for_user("user-sub-owner"),
        remote_store.get_visible_machines_for_user(
            "user-sub-owner", include_admin_paired=True,
        ),
        remote_store.get_all_user_paired_machines(),
    ):
        assert any(r["id"] == result["id"] for r in rows)
        for r in rows:
            _assert_clean(r)


def test_default_machine_for_agent_clean(machine, temp_db):
    from storage import agent_store

    agent_store.create_agent("probe-agent", "Probe Agent")
    remote_store.set_agent_remote_target(
        "probe-agent", machine["id"], added_by="user-sub-admin",
    )
    row = remote_store.get_default_machine_for_agent("probe-agent")
    assert row is not None and row["id"] == machine["id"]
    _assert_clean(row)


def test_verifiers_still_work_after_stripping(machine):
    # exchange_pairing_token + verify_machine_secret use their own SELECTs —
    # the full pairing flow must survive the stripped public row shape.
    secret = remote_store.exchange_pairing_token(
        machine["id"], machine["pairing_token"],
    )
    assert remote_store.verify_machine_secret(machine["id"], secret)
    assert not remote_store.verify_machine_secret(machine["id"], "wrong-secret")
    _assert_clean(remote_store.get_remote_machine(machine["id"]))
