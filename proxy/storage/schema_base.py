"""Helpers shared by ``storage.schema`` and the per-domain schema modules."""

import re


def _index_exists(conn, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s AND schemaname = current_schema()",
        (index_name,),
    ).fetchone()
    return row is not None


def _drop_invalid_indexes(conn, *names: str) -> None:
    """Drop any of ``names`` an interrupted ``CREATE INDEX CONCURRENTLY``
    left INVALID in the current schema: ``CREATE INDEX IF NOT EXISTS`` would
    skip it forever while every query ignores it. The caller creates it next."""
    rows = conn.execute(
        "SELECT c.relname AS name FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT i.indisvalid AND n.nspname = current_schema() AND c.relname = ANY(%s)",
        (list(names),),
    ).fetchall()
    for row in rows:
        name = row["name"] if isinstance(row, dict) else row[0]
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')


def _check_constraint_words(conn, table: str, name: str, schema: str | None = None) -> set[str] | None:
    """The quoted words of a CHECK constraint as the catalog renders it
    (``current_schema()`` unless ``schema`` names another); None when the
    constraint does not exist."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE c.conname = %s AND t.relname = %s "
        "AND n.nspname = COALESCE(%s, current_schema())",
        (name, table, schema),
    ).fetchone()
    return set(re.findall(r"'([^']*)'", row["d"])) if row else None


def _column_exists(conn, table: str, column: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "AND column_name = %s",
        (table, column),
    ).fetchone() is not None
