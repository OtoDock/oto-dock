"""Helpers shared by ``storage.schema`` and the per-domain schema modules."""


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
