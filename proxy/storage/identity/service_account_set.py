"""An agent's own accounts of a tool as a set, and a chat's choice among it.

``service_agent_bindings`` holds, per tool and agent, the accounts the agent
itself uses (each a person's own connected account, lent by a manager of the
agent), one of them the set's default: the account an agent-scope session
that chose none runs on. A row is the account itself or, on a GitHub-App
account, one of its installations, with the agent's GitHub reach on it. The
removal and the per-row standing re-check stay in ``credential_store``; this
module holds the set's other writers and the chat's own choice, in the shape
of a person's set (``credential_store.add_account_to_agent`` and its
siblings). One account per label in a set: the label names the account in a
chat's choice, a subscription, the prompt and the agent's switch.

``chat_service_account_choices`` holds the choice per tool among the agent's
set of a chat whose sessions run as the agent, whoever made it: such a chat
is everyone's who drives it. A row goes with its set row (a foreign key) and
with its chat.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

from __future__ import annotations

import json

import psycopg

from storage.identity.credential_store import (
    _SERVICE_COLUMNS,
    _SERVICE_SET_KEY,
    ServiceBinding,
    _binding_of,
    _lock_service_set,
    _now,
    service_binding_stands,
)
from storage.pg import get_conn

#: Why ``add_service_account_to_agent`` or ``set_service_reach`` wrote nothing.
MISSING = "missing"      # the lender holds no such account, or the set no such row
TAKEN = "taken"          # another lender's account of the set has the label
IDENTITY = "identity"    # the lender's row of the label acts as another installation

_WHERE = "mcp_name=%s AND agent_name=%s"


def list_service_agent_set(
    mcp_name: str, agent_name: str, *, standing_only: bool = True,
) -> list[ServiceBinding]:
    """Every account of the agent's own set of a tool, the default first,
    then by ``set_at`` and ``id``: with ``standing_only`` only the rows whose
    lender still manages the agent (the rows a session may run on)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_SERVICE_COLUMNS} FROM service_agent_bindings WHERE {_WHERE} "
            "ORDER BY is_default DESC, set_at, id",
            (mcp_name, agent_name),
        ).fetchall()
    out = [_binding_of(r) for r in rows]
    if standing_only:
        out = [b for b in out if service_binding_stands(b, agent_name)]
    return out


def _repositories_json(repositories: list[str] | tuple[str, ...]) -> str:
    return json.dumps(list(dict.fromkeys(r.strip() for r in repositories if r.strip())))


def add_service_account_to_agent(
    mcp_name: str, agent_name: str, *, account_label: str, owner_sub: str,
    set_by: str = "", make_default: bool = False, installation_id: str = "",
    repositories: list[str] | tuple[str, ...] = (), read_only: bool = False,
) -> str:
    """Add one of ``owner_sub``'s connected accounts to the agent's own set:
    the account itself, or one of its installations (``installation_id``,
    whose standing the caller judged), with the agent's GitHub reach on it.
    The first row of a set is its default; ``make_default`` moves the
    default to it. A row the lender's account already has stays as it is
    (its reach kept: ``set_service_reach`` changes it), and is made the
    default when asked.

    Returns ``""`` when written, else why not: ``MISSING`` (the lender holds
    no such account), ``TAKEN`` (another lender's account of the set has the
    label), ``IDENTITY`` (the lender's row of the label acts as another
    installation: a row's identity is never changed in place).
    """
    if not owner_sub:
        return MISSING
    key = (mcp_name, agent_name)
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM user_credential_accounts "
            "WHERE user_sub=%s AND mcp_name=%s AND account_label=%s",
            (owner_sub, mcp_name, account_label),
        ).fetchone() is None:
            return MISSING
        _lock_service_set(conn, *key)
        rows = conn.execute(
            f"SELECT account_label, account_owner_sub, installation_id, is_default "
            f"FROM service_agent_bindings WHERE {_WHERE}",
            key,
        ).fetchall()
        mine = next((r for r in rows if r["account_label"] == account_label), None)
        if mine is not None and mine["account_owner_sub"] != owner_sub:
            conn.rollback()
            return TAKEN
        if mine is not None and (mine["installation_id"] or "") != installation_id:
            conn.rollback()
            return IDENTITY
        has_default = any(r["is_default"] for r in rows)
        if make_default:
            conn.execute(
                f"UPDATE service_agent_bindings SET is_default=FALSE "
                f"WHERE {_WHERE} AND is_default AND account_label<>%s",
                key + (account_label,),
            )
        conn.execute(
            f"""INSERT INTO service_agent_bindings
               (mcp_name, agent_name, account_label, account_owner_sub, set_by, set_at,
                installation_id, repositories, read_only, is_default)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT ON CONSTRAINT {_SERVICE_SET_KEY}
               DO UPDATE SET is_default=service_agent_bindings.is_default OR EXCLUDED.is_default""",
            key + (account_label, owner_sub, set_by, _now(), installation_id,
                   _repositories_json(repositories), bool(read_only),
                   make_default or not has_default),
        )
        conn.commit()
    return ""


def set_service_reach(
    mcp_name: str, agent_name: str, *, owner_sub: str, account_label: str,
    installation_id: str, repositories: list[str] | tuple[str, ...] | None,
    read_only: bool | None,
) -> str:
    """Write the agent's GitHub reach on ``owner_sub``'s row of the label,
    only while that row still acts as ``installation_id``: a page left open
    after a removal never puts the account back, nor a reach on another
    identity. A ``None`` keeps what the row holds, read under the set's lock.
    Returns ``""``, ``MISSING`` (no such row of that lender) or ``IDENTITY``."""
    key = (mcp_name, agent_name)
    with get_conn() as conn:
        _lock_service_set(conn, *key)
        row = conn.execute(
            f"SELECT installation_id, repositories, read_only FROM service_agent_bindings "
            f"WHERE {_WHERE} AND account_label=%s AND account_owner_sub=%s",
            key + (account_label, owner_sub),
        ).fetchone()
        if row is None:
            conn.rollback()
            return MISSING
        if (row["installation_id"] or "") != installation_id:
            conn.rollback()
            return IDENTITY
        stored = row["repositories"] if repositories is None else _repositories_json(repositories)
        conn.execute(
            f"UPDATE service_agent_bindings SET repositories=%s, read_only=%s "
            f"WHERE {_WHERE} AND account_label=%s AND account_owner_sub=%s",
            (stored, bool(row["read_only"] if read_only is None else read_only))
            + key + (account_label, owner_sub),
        )
        conn.commit()
    return ""


def set_service_default_account(mcp_name: str, agent_name: str, account_label: str) -> bool:
    """Make one account of the agent's own set its default. Returns False
    when the set does not hold it (no-op)."""
    key = (mcp_name, agent_name)
    with get_conn() as conn:
        _lock_service_set(conn, *key)
        if conn.execute(
            f"SELECT 1 FROM service_agent_bindings WHERE {_WHERE} AND account_label=%s",
            key + (account_label,),
        ).fetchone() is None:
            conn.rollback()
            return False
        conn.execute(
            f"UPDATE service_agent_bindings SET is_default=FALSE "
            f"WHERE {_WHERE} AND is_default AND account_label<>%s",
            key + (account_label,),
        )
        conn.execute(
            f"UPDATE service_agent_bindings SET is_default=TRUE WHERE {_WHERE} AND account_label=%s",
            key + (account_label,),
        )
        conn.commit()
    return True


def sets_of(agent_name: str | None = None) -> list[tuple[str, str]]:
    """``(mcp_name, agent_name)`` of every agent's own set that holds a row
    (of one agent with ``agent_name``)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT mcp_name, agent_name FROM service_agent_bindings "
            "WHERE (%s::text IS NULL OR agent_name = %s) ORDER BY agent_name, mcp_name",
            (agent_name, agent_name),
        ).fetchall()
    return [(r["mcp_name"], r["agent_name"]) for r in rows]


def chats_choosing(mcp_name: str, agent_name: str, account_labels: list[str]) -> list[str]:
    """The chats whose choice names one of these rows of the agent's own set
    (a label names one row of a set)."""
    if not account_labels:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM chat_service_account_choices "
            "WHERE mcp_name=%s AND agent_name=%s AND account_label = ANY(%s) ORDER BY chat_id",
            (mcp_name, agent_name, account_labels),
        ).fetchall()
    return [r["chat_id"] for r in rows]


def chats_without_a_choice(
    mcp_name: str, agent_name: str, *, chat_owner: str, any_owner: bool = False,
) -> list[str]:
    """The dashboard chats of ``chat_owner`` on the agent (with ``any_owner``,
    of every owner: an agent that runs a person's chat in its own space) that
    have started a session and hold no choice for this tool: they run on the
    set's default. A task's and a call's chats are none of them."""
    from core.session.session_kind import PHONE, TASK, TASK_CHAT_ID_PREFIX, source_types
    # ``session_kind.of_chat`` in SQL: no row kind but a dashboard's, no task id.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id FROM chats c WHERE c.agent=%s AND (c.user_sub=%s OR %s) "
            "AND NOT (COALESCE(c.source_type, '') = ANY(%s)) AND c.id NOT LIKE %s "
            "AND COALESCE(c.session_id, '') <> '' AND NOT EXISTS ("
            "  SELECT 1 FROM chat_service_account_choices s "
            "  WHERE s.chat_id = c.id AND s.mcp_name = %s"
            ") ORDER BY c.id",
            (agent_name, chat_owner, any_owner, source_types(PHONE, TASK),
             f"{TASK_CHAT_ID_PREFIX}%", mcp_name),
        ).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# A chat's choice among the agent's own set
# ---------------------------------------------------------------------------

def get_chat_service_choices(chat_id: str) -> dict[str, str]:
    """``{mcp_name: account_label}``: the chat's own choices among the
    agent's sets (a row a set lost went with it)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, account_label FROM chat_service_account_choices WHERE chat_id=%s",
            (chat_id,),
        ).fetchall()
    return {r["mcp_name"]: r["account_label"] for r in rows}


def set_chat_service_choice(
    chat_id: str, agent_name: str, mcp_name: str, account_label: str, *, set_by: str = "",
) -> bool:
    """Record the chat's choice of the agent's account for one tool. Returns
    False when the agent's set does not hold the account (or the chat is
    gone): the foreign keys refuse the row."""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO chat_service_account_choices
                   (chat_id, agent_name, mcp_name, account_label, set_by, set_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (chat_id, mcp_name)
                   DO UPDATE SET agent_name=EXCLUDED.agent_name,
                                 account_label=EXCLUDED.account_label,
                                 set_by=EXCLUDED.set_by,
                                 set_at=EXCLUDED.set_at""",
                (chat_id, agent_name, mcp_name, account_label, set_by, _now()),
            )
            conn.commit()
    except psycopg.errors.ForeignKeyViolation:
        return False
    return True


def clear_chat_service_choice(chat_id: str, mcp_name: str) -> None:
    """The chat runs on the agent's default account for this tool again."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM chat_service_account_choices WHERE chat_id=%s AND mcp_name=%s",
            (chat_id, mcp_name),
        )
        conn.commit()
