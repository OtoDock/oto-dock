"""A window sample records whether it is a session's own report
(``subscription_window_samples.hint``). A database from before the column
keeps every sample as it was, each read as the vendor's reading, and a second
boot changes nothing."""

from __future__ import annotations

from storage import pg, schema as pg_schema
from storage.billing import subscription_store
from storage.pg import get_conn


def _boot() -> None:
    pg.close_pool()
    with get_conn() as conn:
        pg_schema.init_schema(conn)
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()


def test_samples_from_before_the_column_stay_readings_after_the_upgrade():
    sub = subscription_store.add_subscription("claude-code-cli", "anthropic", "oauth", owner_sub="u-hint")["id"]
    try:
        pg.close_pool()
        with get_conn() as conn:
            conn.execute("ALTER TABLE subscription_window_samples DROP COLUMN IF EXISTS hint")
            for at, source, pct in (("2026-11-01T10:00:00+00:00", "poll", 40.0),
                                    ("2026-11-01T10:03:00+00:00", "codex_event", 41.0)):
                conn.execute(
                    "INSERT INTO subscription_window_samples (subscription_id, observed_at, source, "
                    "seven_day_pct, seven_day_resets_at, data) VALUES (%s,%s,%s,%s,%s,'{}')",
                    (sub, at, source, pct, "2026-11-06T10:00:00+00:00"))
            conn.commit()
        _boot()
        _boot()
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT source, seven_day_pct, hint FROM subscription_window_samples "
                "WHERE subscription_id = %s ORDER BY observed_at", (sub,)).fetchall()
        assert [tuple(r.values()) for r in rows] == [("poll", 40.0, False), ("codex_event", 41.0, False)]
        assert subscription_store.latest_window_samples([sub])[sub]["seven_day_pct"] == 41.0
    finally:
        subscription_store.delete_subscription(sub)
        pg.close_pool()
