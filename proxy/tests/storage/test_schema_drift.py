"""Boot-time schema-drift guard (storage.schema.check_schema_drift).

The guard builds the expected schema in a scratch pg schema inside a
rolled-back transaction and diffs information_schema against the live DB —
catching the "column added to CREATE TABLE without a run_migrations ALTER"
class of breakage (the friend's-install incident) at boot instead of as
UndefinedColumn 500s at query time.

Run standalone (one file at a time — concurrent pytest deadlocks on schema-init).
"""

import logging

from storage import schema as pg_schema
from storage.database import get_conn


class TestSchemaDrift:
    def test_no_drift_on_current_schema(self, caplog):
        # The test DB was initialized from the same init_schema — zero drift.
        with get_conn() as conn:
            with caplog.at_level(logging.ERROR):
                drift = pg_schema.check_schema_drift(conn)
        assert drift == []
        assert "SCHEMA DRIFT" not in caplog.text

    def test_detects_missing_column(self, caplog):
        # Simulate a pre-migration DB: drop a column INSIDE the same
        # never-committed transaction the probe runs in — visible to the
        # information_schema diff, discarded by the guard's own rollback.
        with get_conn() as conn:
            conn.execute("ALTER TABLE pinned_apps DROP COLUMN hidden")
            with caplog.at_level(logging.ERROR):
                drift = pg_schema.check_schema_drift(conn)
        assert ("pinned_apps", "hidden") in drift
        assert "SCHEMA DRIFT" in caplog.text
        assert "pinned_apps.hidden" in caplog.text
        # The guard's rollback also discarded our DROP — the live table is intact.
        with get_conn() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name='pinned_apps' AND column_name='hidden'"
            ).fetchone()
        assert row["n"] == 1

    def test_never_raises_on_db_error(self, caplog):
        class _BrokenConn:
            def execute(self, *a, **k):
                raise RuntimeError("db is gone")

            def rollback(self):
                pass

        drift = pg_schema.check_schema_drift(_BrokenConn())  # must not raise
        assert drift == []
