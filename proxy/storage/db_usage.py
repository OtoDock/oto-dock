"""Usage-record and usage-limit queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Usage records
# ---------------------------------------------------------------------------

def insert_usage_record(
    user_sub: str | None, agent: str, scope: str, source_type: str,
    source_id: str | None, cost_usd: float, input_tokens: int = 0,
    output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0,
    message_count: int = 0, provider: str = "anthropic",
    source_key: str = "default", model: str = "",
    audio_seconds: float | None = None, billing_unit: str | None = None,
) -> int:
    """Insert one usage row. ``audio_seconds`` / ``billing_unit`` are set by the
    chat-audio (TTS/STT) + transcribe endpoints — those are NOT MCP tool calls,
    so the cost engine never fires for them; they bill via this direct path
    (which, unlike usage_service, also records $0/free-tier seconds). Set
    ``provider`` explicitly for audio rows (don't default it to anthropic)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO usage_records
               (user_sub, agent, scope, source_type, source_id, cost_usd,
                input_tokens, output_tokens, cache_read, cache_write,
                message_count, provider, source_key, model,
                audio_seconds, billing_unit, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (user_sub, agent, scope, source_type, source_id, cost_usd,
             input_tokens, output_tokens, cache_read, cache_write,
             message_count, provider, source_key, model,
             audio_seconds, billing_unit, now),
        )
        row_id = cur.fetchone()["id"]
        conn.commit()
        return row_id


def insert_usage_records_batch(rows: list[dict]) -> list[int]:
    """Insert N usage rows in one transaction. Returns row IDs in input order.

    Each row is a dict matching insert_usage_record's kwargs (minus the
    timestamp, which is generated here). Used by the pump so the LLM row
    and per-MCP rows for one turn either all land or all roll back.
    """
    if not rows:
        return []
    now = datetime.now(timezone.utc).isoformat()
    ids: list[int] = []
    with get_conn() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT INTO usage_records
                   (user_sub, agent, scope, source_type, source_id, cost_usd,
                    input_tokens, output_tokens, cache_read, cache_write,
                    message_count, provider, source_key, model, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (r.get("user_sub"), r.get("agent", ""), r.get("scope", "user"),
                 r.get("source_type", "chat"), r.get("source_id"),
                 r.get("cost_usd", 0), r.get("input_tokens", 0),
                 r.get("output_tokens", 0), r.get("cache_read", 0),
                 r.get("cache_write", 0), r.get("message_count", 0),
                 r.get("provider", "anthropic"), r.get("source_key", "default"),
                 r.get("model", ""), now),
            )
            ids.append(cur.fetchone()["id"])
        conn.commit()
    return ids


def get_usage_totals_by_provider(start: str, end: str) -> list[dict]:
    """Platform-wide cost + message rollup grouped by provider only.

    Combines user-scope and agent-scope rows. Used for the top-level
    "Costs by Provider" admin view. Returns rows ordered by cost desc.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT provider,"
            " COALESCE(SUM(cost_usd),0) as cost,"
            " COALESCE(SUM(message_count),0) as message_count"
            " FROM usage_records"
            " WHERE created_at>=%s AND created_at<%s"
            " GROUP BY provider"
            " ORDER BY cost DESC",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def get_usage_totals_by_model(start: str, end: str) -> list[dict]:
    """Platform-wide cost + message rollup grouped by (provider, model).

    Combines user-scope and agent-scope rows. Used for the top-level
    "Costs by Model" admin view. Returns rows ordered by cost desc.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT provider, model,"
            " COALESCE(SUM(cost_usd),0) as cost,"
            " COALESCE(SUM(message_count),0) as message_count"
            " FROM usage_records"
            " WHERE created_at>=%s AND created_at<%s"
            " GROUP BY provider, model"
            " ORDER BY cost DESC",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def get_usage_provider_breakdown(scope: str, start: str, end: str) -> list[dict]:
    """Per-(target, provider, model) cost rollup for admin breakdown.

    Group key depends on scope so the user view doesn't over-split:
      - scope='user'  → GROUP BY user_sub, provider, model (one row per
        provider/model per user, summed across all the user's agents)
      - scope='agent' → GROUP BY agent, provider, model (one row per
        provider/model per agent)

    Returns rows ordered by cost desc, each with `user_sub`, `agent`,
    `provider`, `model`, `cost`. The unused key is NULL.
    """
    if scope == "user":
        sql = (
            "SELECT user_sub, NULL::text as agent, provider, model,"
            " COALESCE(SUM(cost_usd),0) as cost"
            " FROM usage_records"
            " WHERE scope='user' AND created_at>=%s AND created_at<%s"
            " GROUP BY user_sub, provider, model"
            " ORDER BY cost DESC"
        )
    elif scope == "agent":
        sql = (
            "SELECT NULL::text as user_sub, agent, provider, model,"
            " COALESCE(SUM(cost_usd),0) as cost"
            " FROM usage_records"
            " WHERE scope='agent' AND created_at>=%s AND created_at<%s"
            " GROUP BY agent, provider, model"
            " ORDER BY cost DESC"
        )
    else:
        raise ValueError(f"scope must be 'user' or 'agent', got {scope!r}")
    with get_conn() as conn:
        rows = conn.execute(sql, (start, end)).fetchall()
        return [dict(r) for r in rows]


# Classify a user-scoped usage row by who paid, via the serving subscription
# (usage_records.source_key -> execution_layer_subscriptions.id, both TEXT). Used
# by `basis=` below and the admin per-user split. Null-safe (IS DISTINCT FROM):
#   - platform: a borrowed platform credential (api_key/relay/local_endpoint owned
#     by someone other than the user; hosted relay owner_sub='' counts).
#   - self:     the user's OWN subscription (any auth_type incl. oauth/own api_key).
# Rows whose source_key matches no subscription (MCP/title/legacy 'default') are
# "unattributed" — counted by NEITHER predicate.
_PLATFORM_PAID_PRED = (
    "els.id IS NOT NULL AND els.owner_sub IS DISTINCT FROM ur.user_sub"
    " AND els.auth_type IN ('api_key','relay','local_endpoint')"
)
_SELF_PAID_PRED = "els.id IS NOT NULL AND els.owner_sub IS NOT DISTINCT FROM ur.user_sub"


def get_usage_aggregated(
    user_sub: str | None = None, agent: str | None = None,
    scope: str | None = None, start: str | None = None, end: str | None = None,
    basis: str | None = None,
) -> dict:
    """SUM cost/messages within filters. Returns {total_cost, total_messages, record_count}.

    ``basis`` classifies user-scoped rows by who paid (see the predicates above):
    ``'platform'`` = borrowed platform credentials only, ``'self'`` = the user's
    own subscription only, ``None`` = grand total (unchanged behaviour, no join).
    """
    use_join = basis in ("platform", "self")
    col = "ur." if use_join else ""
    conditions: list[str] = []
    params: list[Any] = []
    if user_sub is not None:
        conditions.append(f"{col}user_sub=%s")
        params.append(user_sub)
    if agent is not None:
        conditions.append(f"{col}agent=%s")
        params.append(agent)
    if scope is not None:
        conditions.append(f"{col}scope=%s")
        params.append(scope)
    if start:
        conditions.append(f"{col}created_at>=%s")
        params.append(start)
    if end:
        conditions.append(f"{col}created_at<%s")
        params.append(end)
    if use_join:
        conditions.append(_PLATFORM_PAID_PRED if basis == "platform" else _SELF_PAID_PRED)
        from_clause = (
            "usage_records ur"
            " LEFT JOIN execution_layer_subscriptions els ON ur.source_key = els.id"
        )
    else:
        from_clause = "usage_records"
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM({col}cost_usd),0) as total_cost,"
            f" COALESCE(SUM({col}message_count),0) as total_messages,"
            f" COUNT(*) as record_count FROM {from_clause} {where}",
            params,
        ).fetchone()
        return dict(row) if row else {"total_cost": 0, "total_messages": 0, "record_count": 0}


def get_usage_daily(
    user_sub: str | None = None, agent: str | None = None,
    scope: str | None = None, days: int = 30,
) -> list[dict]:
    """Per-day cost breakdown. Returns [{date, cost, messages}, ...]."""
    conditions: list[str] = []
    params: list[Any] = []
    if user_sub is not None:
        conditions.append("user_sub=%s")
        params.append(user_sub)
    if agent is not None:
        conditions.append("agent=%s")
        params.append(agent)
    if scope is not None:
        conditions.append("scope=%s")
        params.append(scope)
    conditions.append("created_at >= (CURRENT_DATE - INTERVAL '1 day' * %s)::text")
    params.append(days)
    where = f"WHERE {' AND '.join(conditions)}"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT LEFT(created_at, 10) as date,"
            f" COALESCE(SUM(cost_usd),0) as cost,"
            f" COALESCE(SUM(message_count),0) as messages"
            f" FROM usage_records {where}"
            f" GROUP BY LEFT(created_at, 10) ORDER BY LEFT(created_at, 10)",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_usage_by_agent(user_sub: str, start: str, end: str) -> list[dict]:
    """Per-agent cost breakdown for a user. Returns [{agent, cost, messages}, ...]."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COALESCE(SUM(cost_usd),0) as cost,"
            " COALESCE(SUM(message_count),0) as messages"
            " FROM usage_records WHERE user_sub=%s AND scope='user'"
            " AND created_at>=%s AND created_at<%s"
            " GROUP BY agent ORDER BY cost DESC",
            (user_sub, start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_users_usage(start: str, end: str) -> list[dict]:
    """All users' usage for admin table. Joins users + usage_records, and the
    serving subscription to split each user's cost into platform-paid (borrowed)
    vs self-paid (own subscription). ``total_cost`` stays the grand total for
    display; ``platform_cost`` drives the limit percentage. The els join is on a
    PK (id) so it's 1:1 — no fan-out / double-counting."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT u.sub, u.email, u.name, u.role,"
            " COALESCE(SUM(ur.cost_usd),0) as total_cost,"
            f" COALESCE(SUM(CASE WHEN {_PLATFORM_PAID_PRED} THEN ur.cost_usd ELSE 0 END),0) as platform_cost,"
            f" COALESCE(SUM(CASE WHEN {_SELF_PAID_PRED} THEN ur.cost_usd ELSE 0 END),0) as self_cost,"
            " COALESCE(SUM(ur.message_count),0) as message_count"
            " FROM users u"
            " LEFT JOIN usage_records ur"
            "   ON ur.user_sub=u.sub AND ur.scope='user'"
            "   AND ur.created_at>=%s AND ur.created_at<%s"
            " LEFT JOIN execution_layer_subscriptions els ON ur.source_key = els.id"
            " GROUP BY u.sub, u.email, u.name, u.role"
            " ORDER BY platform_cost DESC, total_cost DESC",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def get_agent_activity_counts(start: str, end: str) -> dict[str, dict]:
    """Per-agent activity volume across ALL scopes for the agents-map heat:
    {agent: {messages, records}}. usage_records is the deliberate heat
    source — it is small (rollup rows) and indexed by (agent, scope,
    created_at); chat_messages is the highest-cardinality table in the
    schema and has no timestamp index, so it must NOT back this."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COALESCE(SUM(message_count),0) AS messages,"
            " COUNT(*) AS records"
            " FROM usage_records WHERE created_at>=%s AND created_at<%s"
            " AND agent IS NOT NULL AND agent<>''"
            " GROUP BY agent",
            (start, end),
        ).fetchall()
        return {r["agent"]: {"messages": r["messages"], "records": r["records"]}
                for r in rows}


def get_all_agents_usage(start: str, end: str) -> list[dict]:
    """Agent-scoped usage aggregated per agent."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COALESCE(SUM(cost_usd),0) as total_cost,"
            " COUNT(*) as record_count"
            " FROM usage_records WHERE scope='agent'"
            " AND created_at>=%s AND created_at<%s"
            " GROUP BY agent ORDER BY total_cost DESC",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Usage limits
# ---------------------------------------------------------------------------

def upsert_usage_limit(limit_type: str, target: str, period: str,
                       cost_limit_usd: float | None, updated_by: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO usage_limits (limit_type, target, period, cost_limit_usd, updated_at, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT(limit_type, target, period)
               DO UPDATE SET cost_limit_usd=EXCLUDED.cost_limit_usd,
                             updated_at=EXCLUDED.updated_at,
                             updated_by=EXCLUDED.updated_by""",
            (limit_type, target, period, cost_limit_usd, now, updated_by),
        )
        conn.commit()


def delete_usage_limit(limit_type: str, target: str, period: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM usage_limits WHERE limit_type=%s AND target=%s AND period=%s",
            (limit_type, target, period),
        )
        conn.commit()
        return cur.rowcount > 0


def get_usage_limit(limit_type: str, target: str, period: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM usage_limits WHERE limit_type=%s AND target=%s AND period=%s",
            (limit_type, target, period),
        ).fetchone()
        return dict(row) if row else None


def get_usage_limits_all() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM usage_limits ORDER BY limit_type, target, period"
        ).fetchall()
        return [dict(r) for r in rows]


def get_usage_limits_for_target(limit_type: str, target: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM usage_limits WHERE limit_type=%s AND target=%s",
            (limit_type, target),
        ).fetchall()
        return [dict(r) for r in rows]
