"""The release that migrated the database (``schema_meta``), the one version
rule and the boot preflight (storage.schema.migrate_database).

The autouse fixture wipes every table before a test, the row included, so
each test migrates for itself.
"""

from __future__ import annotations

import logging

import pytest

from storage import schema
from storage.database import get_conn


def _row():
    with get_conn() as conn:
        return schema.read_platform_version(conn)


def _migrate(running, **kw):
    with get_conn() as conn:
        schema.migrate_database(conn, running, **kw)
        conn.commit()


def _no_users():
    # The autouse fixture seeds users; a fresh database has none.
    with get_conn() as conn:
        conn.execute("DELETE FROM users")
        conn.commit()


def _add_user():
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login) "
            "VALUES ('u-meta', 'meta@test.com', 'Meta', 'admin', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')")
        conn.commit()


# ── the version rule ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, want", [
    ("1.8.0", (1, 8, 0, (0, 0))),
    ("v1.8.0", (1, 8, 0, (0, 0))),
    (" 1.8.0 ", (1, 8, 0, (0, 0))),
    ("1.8.0-rc2", (1, 8, 0, (-1, 2))),
    ("1.8.0-dev", (1, 8, 0, (-2, 0))),
    ("1.8.0-laneu", None),
    ("latest", None),
    ("", None),
    (None, None),
    ("1.8", None),
])
def test_the_version_rule(text, want):
    assert schema.parse_version(text) == want


def test_a_dev_build_is_older_than_an_rc_which_is_older_than_the_release():
    dev, rc1, rc2, rel = (schema.parse_version(v) for v in ("1.8.0-dev", "1.8.0-rc1", "1.8.0-rc2", "1.8.0"))
    assert dev < rc1 < rc2 < rel
    assert rel < schema.parse_version("1.8.1-dev")


# ── the row ──────────────────────────────────────────────────────────────────

def test_a_fresh_database_remembers_the_release_that_created_it():
    _no_users()
    _migrate("1.8.0")
    row = _row()
    assert row["created_by"] == "1.8.0"
    assert row["last_migrated_by"] == "1.8.0"
    assert row["upgraded_from"] == ""
    assert row["last_migrated_at"] is not None


def test_a_database_from_before_the_row_records_an_upgrade_of_unknown_origin():
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS schema_meta")
        conn.commit()
    _add_user()
    _migrate("1.8.0")
    row = _row()
    assert row["created_by"] == ""
    assert row["last_migrated_by"] == "1.8.0"
    assert row["upgraded_from"] == ""


def test_a_newer_release_raises_the_row_and_keeps_where_it_came_from():
    _no_users()
    _migrate("1.8.0")
    _migrate("1.8.0")  # the same release: nothing changes
    before = _row()
    _migrate("1.9.0")
    row = _row()
    assert row["last_migrated_by"] == "1.9.0"
    assert row["upgraded_from"] == "1.8.0"
    assert row["created_by"] == "1.8.0"
    assert row["last_migrated_at"] > before["last_migrated_at"]


def test_a_newer_database_refuses_the_boot_naming_both_versions_and_the_override(caplog):
    _migrate("1.9.0")
    with get_conn() as conn, caplog.at_level(logging.CRITICAL), pytest.raises(RuntimeError) as exc:
        schema.migrate_database(conn, "1.8.0")
    msg = str(exc.value)
    assert "last migrated by OtoDock 1.9.0" in msg and "runs 1.8.0" in msg
    assert "OTODOCK_ALLOW_NEWER_DATABASE=true" in msg and "scripts/restore.sh" in msg
    assert msg in caplog.text
    assert _row()["last_migrated_by"] == "1.9.0"


def test_the_override_lets_an_older_release_through_and_never_lowers_the_row(caplog):
    _migrate("1.9.0")
    with caplog.at_level(logging.WARNING):
        _migrate("1.8.0", allow_newer=True)
    assert "OTODOCK_ALLOW_NEWER_DATABASE is set" in caplog.text
    assert _row()["last_migrated_by"] == "1.9.0"


def test_a_dev_build_is_never_refused_and_never_records(caplog):
    _migrate("1.9.0")
    with caplog.at_level(logging.WARNING):
        _migrate("1.8.0-dev")
    assert "a -dev build: the boot goes on" in caplog.text
    assert _row()["last_migrated_by"] == "1.9.0"
    with get_conn() as conn:
        conn.execute("DELETE FROM schema_meta")
        conn.commit()
    _migrate("1.8.0-dev")
    assert _row() is None


def test_an_rc_is_refused_by_the_release_with_the_same_numbers():
    _migrate("1.8.0")
    with get_conn() as conn, pytest.raises(RuntimeError):
        schema.migrate_database(conn, "1.8.0-rc1")


@pytest.mark.parametrize("running", ["", "1.8.0-laneu", "latest"])
def test_an_unknown_running_version_is_not_checked_and_not_recorded(running, caplog):
    _migrate("1.9.0")
    with caplog.at_level(logging.WARNING):
        _migrate(running)
    assert "not a release number" in caplog.text
    assert _row()["last_migrated_by"] == "1.9.0"


def test_an_unreadable_row_is_neither_checked_nor_touched(caplog):
    _migrate("1.8.0")
    with get_conn() as conn:
        conn.execute("UPDATE schema_meta SET last_migrated_by = '2.0.0-beta1' WHERE id = 1")
        conn.commit()
    with caplog.at_level(logging.WARNING):
        _migrate("1.8.1")
    assert "not a release number" in caplog.text
    assert _row()["last_migrated_by"] == "2.0.0-beta1"


def test_no_table_means_no_check():
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS schema_meta")
        assert schema.platform_version_preflight(conn, "1.8.0") == ""
        conn.rollback()
