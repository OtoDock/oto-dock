"""OAuth bearer-allowlist restore-defaults (re-seed) tests.

Seeding is idempotent (``ON CONFLICT DO NOTHING``); ``restore_defaults``
re-adds any vendor-official defaults an admin deleted, without duplicating
rows or touching admin-added custom entries.
"""

from storage.identity import bearer_allowlist
from storage.pg import get_conn


def _delete(provider_id: str, host: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM oauth_bearer_allowlist "
            "WHERE provider_id=%s AND host_pattern=%s",
            (provider_id, host),
        )
        conn.commit()


def test_restore_readds_a_deleted_default():
    provider, host = bearer_allowlist.DEFAULT_ALLOWLIST[0]
    bearer_allowlist.restore_defaults()
    _delete(provider, host)
    assert not bearer_allowlist.is_host_allowed(provider, host)

    rows = bearer_allowlist.restore_defaults()
    assert bearer_allowlist.is_host_allowed(provider, host)
    pairs = {(r["provider_id"], r["host_pattern"]) for r in rows}
    for p, h in bearer_allowlist.DEFAULT_ALLOWLIST:
        assert (p, h) in pairs


def test_restore_is_idempotent_and_keeps_custom_entries():
    custom_id = bearer_allowlist.add_allowed("custom-prov", "custom.example.com")
    try:
        bearer_allowlist.restore_defaults()
        bearer_allowlist.restore_defaults()  # twice — must not duplicate
        after = bearer_allowlist.list_allowed()

        # Custom entry survives.
        assert any(r["id"] == custom_id for r in after)
        # No duplicate rows for any (provider, host) pair.
        pairs = [(r["provider_id"], r["host_pattern"]) for r in after]
        assert len(pairs) == len(set(pairs))
    finally:
        bearer_allowlist.delete_allowed(custom_id)
