"""The remote machines' forward migrations that live outside
``storage/schema.py`` (which sits at its line cap): called from
``schema.run_migrations`` on every start, idempotent."""

from __future__ import annotations


def migrate_remote(conn) -> None:
    """``browser_profile``: the machine's ``Browser profile`` for the
    own-browser mode, the profile that has the Playwright Extension as the
    browser names it (``""`` = the one profile that has it).
    ``all_roles_run_here``: an admin machine's "Every role of its agents runs
    here" (``placement.admits_every_role``), off on every existing row."""
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "browser_profile TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE remote_machines ADD COLUMN IF NOT EXISTS "
        "all_roles_run_here BOOLEAN NOT NULL DEFAULT FALSE"
    )
