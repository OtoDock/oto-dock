"""The webhook subscription store's one-statement error note
(``note_last_error``): the receive path records a refused signature without
a status transition, and never on a row still being created."""

import uuid

from storage.automation import webhook_subscription_store as store


def _row(db, *, status: str) -> dict:
    row = store.create_subscription(
        scope="user", owner="user-admin", agent=None, mcp_name="m365-mcp",
        provider_id="microsoft", account_label="", vendor_target=f"t-{uuid.uuid4().hex[:6]}",
        selected_events=[], selected_subevents={}, signing_secret="s",
        created_by="user-admin")
    if status != store.CREATING:
        store.update_subscription_status(row["id"], store.ACTIVE)
    if status not in (store.CREATING, store.ACTIVE):
        store.update_subscription_status(row["id"], status)
    return store.get_subscription(row["id"])


def test_note_last_error_writes_the_note_and_leaves_the_status(temp_db):
    row = _row(temp_db, status=store.RENEW_FAILED)
    assert store.note_last_error(row["id"], "signature: client_state_mismatch (3 refused)")
    after = store.get_subscription(row["id"])
    assert after["last_error"] == "signature: client_state_mismatch (3 refused)"
    assert after["status"] == store.RENEW_FAILED
    assert after["updated_at"] > row["updated_at"]


def test_note_last_error_skips_a_creating_row_and_a_missing_id(temp_db):
    row = _row(temp_db, status=store.CREATING)
    assert store.note_last_error(row["id"], "signature: missing_header (1 refused)") is False
    assert store.get_subscription(row["id"])["last_error"] is None
    assert store.note_last_error(str(uuid.uuid4()), "x") is False
