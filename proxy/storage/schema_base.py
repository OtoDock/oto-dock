"""Helpers shared by ``storage.schema`` and the per-domain schema modules."""


def _index_exists(conn, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
        (index_name,),
    ).fetchone()
    return row is not None
