"""The remote machines' forward migrations that live outside
``storage/schema.py`` (which sits at its line cap): called from
``schema.run_migrations`` on every start, idempotent."""

from __future__ import annotations

import logging

logger = logging.getLogger("otodock.remote-schema")

#: The rule a row's owner keeps: only a compute machine (the platform's own)
#: has nobody in ``registered_by``.
OWNER_CHECK = "remote_machines_owner_check"


def migrate_remote(conn) -> None:
    """``browser_profile``: the machine's ``Browser profile`` for the
    own-browser mode, the profile that has the Playwright Extension as the
    browser names it (``""`` = the one profile that has it).
    ``all_roles_run_here``: an admin machine's "Every role of its agents runs
    here" (``placement.admits_every_role``), off on every existing row.
    ``compute_address``: the address a compute machine on another server last
    signed in from (``''`` for one beside the platform, which shares its
    folders, and for one not paired yet: ``placement.compute_own_copy``),
    written at its pairing and at every sign-in; two servers behind one
    network address are two machines, so it is not unique.
    ``compute_address_locked``: its "Lock to this address" (``compute_store``),
    off. The boot that adds it takes the secret hash off a compute machine on
    another server that never signed in: wave 11 showed that secret on a
    screen, and "Pair again" gives it a new one.
    Then a machine nobody owns (``_owner_may_be_nobody``)."""
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "browser_profile TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "compute_address TEXT NOT NULL DEFAULT ''"
    )
    conn.execute("DROP INDEX IF EXISTS remote_machines_compute_address_key")
    _address_lock(conn)
    # A compute machine on another server an admin removed, kept 30 days
    # (``compute_store.record_removal``): only a sign-in whose secret verifies
    # here is told to remove itself, never an id this database does not know.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS removed_compute_machines ("
        "id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, removed_at TEXT NOT NULL)"
    )
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "all_roles_run_here BOOLEAN NOT NULL DEFAULT FALSE"
    )
    _owner_may_be_nobody(conn)


def _address_lock(conn) -> None:
    """``compute_address_locked``, added once: the boot that adds it also
    clears the unused secret hash of every compute machine on another server
    that never signed in (the address set, no ``last_seen``, no pairing
    token), at that boot."""
    have = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'remote_machines' AND column_name = 'compute_address_locked'"
    ).fetchone()
    if have:
        return
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN compute_address_locked BOOLEAN NOT NULL DEFAULT FALSE"
    )
    clear_unused_sign_ins(conn)


def clear_unused_sign_ins(conn) -> int:
    """The secret hash off every compute machine on another server that
    never signed in (the address set, no ``last_seen``, no pairing token):
    run once, by the boot that adds the address lock."""
    cleared = conn.execute(
        "UPDATE remote_machines SET machine_secret_hash = NULL "
        "WHERE pairing_scope = 'compute' AND compute_address <> '' AND last_seen IS NULL "
        "AND pairing_token_hash IS NULL AND machine_secret_hash IS NOT NULL"
    ).rowcount
    if cleared:
        logger.info("remote_machines: %d compute machine(s) that never signed in wait to be paired again",
                    cleared)
    return cleared


def _owner_may_be_nobody(conn) -> None:
    """A compute machine is registered with no owner: ``registered_by`` takes
    NULL, and only on such a row (``OWNER_CHECK``). Each step runs once, read
    from the catalog first, under a savepoint: every row a 1.9.1 install holds
    has an owner, so the check holds on it, and a failure leaves the table as
    it was for the next boot to try again."""
    col = conn.execute(
        "SELECT a.attnotnull FROM pg_attribute a "
        "JOIN pg_class t ON t.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = 'remote_machines' AND n.nspname = current_schema() "
        "AND a.attname = 'registered_by' AND NOT a.attisdropped"
    ).fetchone()
    not_null = bool(col and col["attnotnull"])
    check = conn.execute(
        "SELECT 1 FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = 'remote_machines' AND n.nspname = current_schema() "
        "AND c.conname = %s",
        (OWNER_CHECK,),
    ).fetchone()
    if not not_null and check:
        return
    conn.execute("SAVEPOINT remote_machines_owner_may_be_nobody")
    try:
        if not_null:
            conn.execute("ALTER TABLE remote_machines ALTER COLUMN registered_by DROP NOT NULL")
        if not check:
            conn.execute(
                f"ALTER TABLE remote_machines ADD CONSTRAINT {OWNER_CHECK} "
                "CHECK (registered_by IS NOT NULL OR pairing_scope = 'compute')"
            )
        conn.execute("RELEASE SAVEPOINT remote_machines_owner_may_be_nobody")
        logger.info("remote_machines: a compute machine may have no owner")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT remote_machines_owner_may_be_nobody")
        logger.exception("remote_machines: the owner rule was not added; the next boot retries")
