"""OAuth bearer-token allowlist storage.

Hybrid trust model: MCP manifests declare INTENT
(``credentials.oauth.bearer_required=true`` + ``proposed_hosts``); this
table is the platform-controlled REALITY. Admins approve which
(provider_id, host_pattern) pairs may receive a user's OAuth token via
HTTP ``Authorization: Bearer`` injection.

Seeded at startup with vendor-official hosts (see ``schema.init_schema``
seed loop). Admins can extend via /v1/admin/oauth-bearer-allowlist.

Matcher supports two wildcard patterns:
  * Exact host: ``mcp.slack.com``
  * Subdomain wildcard: ``*.linear.app`` (matches ``mcp.linear.app``,
    ``api.linear.app``, etc. — NOT ``linear.app`` itself).
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timezone

from storage.pg import get_conn

logger = logging.getLogger("claude-proxy.bearer-allowlist")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Vendor-official (provider_id, host_pattern) pairs seeded on first startup so
# the framework works out of the box. github/microsoft use ``localhost``
# because those MCPs run as LOCAL Docker sidecars on the proxy: the bearer is
# forwarded to the sidecar (http://localhost:${port}/mcp), which then calls
# the vendor API itself. Single source of truth for both the boot seed
# (``schema.init_schema``) and the admin "Restore defaults" action.
DEFAULT_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("slack", "mcp.slack.com"),
    ("linear", "mcp.linear.app"),
    ("notion", "mcp.notion.com"),
    ("zoom", "mcp.zoom.us"),
    ("atlassian", "mcp.atlassian.com"),
    ("github", "localhost"),
    ("microsoft", "localhost"),
    # Postiz Cloud's hosted MCP (postiz-mcp, remote streamable_http) — the
    # user's Postiz API key is injected as the bearer toward api.postiz.com.
    ("postiz", "api.postiz.com"),
)


def seed_defaults(conn) -> None:
    """Insert the vendor-official defaults using an EXISTING connection.

    Idempotent (``ON CONFLICT DO NOTHING``) — keeps any admin-added entries
    intact. Caller owns the transaction (commit). Used by the boot seed.
    """
    for provider_id, host in DEFAULT_ALLOWLIST:
        conn.execute(
            "INSERT INTO oauth_bearer_allowlist "
            "(provider_id, host_pattern, added_by, added_at) "
            "VALUES (%s, %s, 'system', %s) "
            "ON CONFLICT (provider_id, host_pattern) DO NOTHING",
            (provider_id, host, _now()),
        )


def restore_defaults() -> list[dict]:
    """Re-insert any missing vendor-official defaults; return the full list.

    Idempotent — re-adds defaults an admin deleted without touching their
    own custom entries or duplicating existing rows.
    """
    with get_conn() as conn:
        seed_defaults(conn)
        conn.commit()
    return list_allowed()


def list_allowed() -> list[dict]:
    """Return [{id, provider_id, host_pattern, added_by, added_at}, ...]."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, provider_id, host_pattern, added_by, added_at "
            "FROM oauth_bearer_allowlist "
            "ORDER BY provider_id, host_pattern"
        ).fetchall()
        return [dict(r) for r in rows]


def add_allowed(provider_id: str, host_pattern: str, added_by: str = "admin") -> int:
    """Insert an allowlist entry. Returns the new row id (or existing on conflict)."""
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO oauth_bearer_allowlist
               (provider_id, host_pattern, added_by, added_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (provider_id, host_pattern)
               DO UPDATE SET added_at = oauth_bearer_allowlist.added_at
               RETURNING id""",
            (provider_id, host_pattern, added_by, _now()),
        ).fetchone()
        conn.commit()
        return row["id"]


def delete_allowed(row_id: int) -> bool:
    """Remove an allowlist entry by id. Returns True if deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM oauth_bearer_allowlist WHERE id = %s", (row_id,),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def is_host_allowed(provider_id: str, host: str) -> bool:
    """True iff (provider_id, host) is on the allowlist.

    Matches against ``host_pattern`` with fnmatch semantics — exact host
    or ``*.domain.com`` style wildcards. Case-insensitive on host.
    """
    if not provider_id or not host:
        return False
    host_lc = host.lower()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT host_pattern FROM oauth_bearer_allowlist "
            "WHERE provider_id = %s",
            (provider_id,),
        ).fetchall()
    for r in rows:
        pattern = (r["host_pattern"] or "").lower()
        if not pattern:
            continue
        if fnmatch.fnmatchcase(host_lc, pattern):
            return True
    return False
