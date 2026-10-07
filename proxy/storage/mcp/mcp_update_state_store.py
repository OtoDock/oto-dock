"""PostgreSQL-backed state of the MCP update check and the pending source
changes (``mcp_update_checks``, ``mcp_source_changes``; created in
``storage/mcp/schema.py::init_mcp_autoupdate``).

The last check's ordinary results are replaced whole by every check, so the
MCP Servers page shows them again after a navigation or a restart without a
new check. A source change is one row per MCP that lives from its detection
to the admin's switch (or the catalog's revert), then as the switch's result
until dismissed. All functions are synchronous: call them through
``asyncio.to_thread`` or ``run_db``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from storage.pg import get_conn

STATUS_PENDING = "pending"
STATUS_SWITCHING = "switching"
STATUS_SWITCHED = "switched"

LAST_CHECKED_KEY = "mcp_update_last_checked_at"

# The pair that identifies one change: a different pair is a new change
# (notified again, its result cleared), the same pair keeps its row.
_PAIR_COLUMNS = ("from_kind", "from_identity", "to_kind", "to_identity")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# The last check's ordinary results
# ---------------------------------------------------------------------------

def replace_check_results(results: dict[str, dict]) -> str:
    """Replace the persisted check results with ``results`` (``{name: info}``)
    and stamp the check time. Returns the time written."""
    from storage.db_settings import set_platform_setting
    now = _now()
    with get_conn() as conn:
        conn.execute("DELETE FROM mcp_update_checks")
        for name, info in results.items():
            conn.execute(
                "INSERT INTO mcp_update_checks (mcp_name, info, checked_at) "
                "VALUES (%s, %s, %s)",
                (name, json.dumps(info), now),
            )
        conn.commit()
    set_platform_setting(LAST_CHECKED_KEY, now)
    return now


def delete_check_result(mcp_name: str) -> bool:
    """Drop one MCP's persisted result (an applied update retires its offer).
    The check time stays: the check itself still happened."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM mcp_update_checks WHERE mcp_name = %s", (mcp_name,))
        conn.commit()
        return cur.rowcount > 0


def get_check_results() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, info FROM mcp_update_checks",
        ).fetchall()
    return {r["mcp_name"]: _loads(r["info"]) for r in rows}


def last_checked_at() -> str:
    from storage.db_settings import get_platform_setting
    return get_platform_setting(LAST_CHECKED_KEY) or ""


# ---------------------------------------------------------------------------
# Source changes
# ---------------------------------------------------------------------------

def _row(d: dict) -> dict:
    d = dict(d)
    d["plan"] = _loads(d.get("plan"))
    d["result"] = _loads(d.get("result"))
    d["declared"] = bool(d.get("declared"))
    return d


def get_source_change(mcp_name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,),
        ).fetchone()
    return _row(row) if row else None


def list_source_changes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_source_changes ORDER BY mcp_name",
        ).fetchall()
    return [_row(r) for r in rows]


def upsert_pending_source_change(mcp_name: str, change: dict) -> dict:
    """Record a detected change. ``change`` carries the ``from_*`` and
    ``to_*`` columns, ``declared`` and ``plan``. A row in ``switching`` is
    left alone (an accept holds the install). The same pair keeps its row,
    its notification mark and its last result, with the catalog side
    refreshed (the version, the manifest hash, the plan); a different pair
    replaces the row as a fresh pending change. Returns the row."""
    now = _now()
    columns = (
        "from_kind", "from_identity", "from_url", "from_runtime",
        "to_kind", "to_identity", "to_url", "to_runtime", "to_version",
        "to_manifest_hash",
    )
    values = {c: str(change.get(c) or "") for c in columns}
    declared = bool(change.get("declared"))
    plan = json.dumps(change.get("plan") or {})
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,),
        ).fetchone()
        if existing is not None and existing["status"] == STATUS_SWITCHING:
            return _row(existing)
        same_pair = existing is not None and all(
            existing[c] == values[c] for c in _PAIR_COLUMNS
        ) and existing["status"] == STATUS_PENDING
        if same_pair:
            conn.execute(
                "UPDATE mcp_source_changes SET from_url=%s, from_runtime=%s, "
                "to_url=%s, to_runtime=%s, to_version=%s, to_manifest_hash=%s, "
                "declared=%s, plan=%s, updated_at=%s WHERE mcp_name=%s",
                (values["from_url"], values["from_runtime"], values["to_url"],
                 values["to_runtime"], values["to_version"],
                 values["to_manifest_hash"], declared, plan, now, mcp_name),
            )
        else:
            conn.execute(
                "DELETE FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,),
            )
            conn.execute(
                "INSERT INTO mcp_source_changes (mcp_name, status, from_kind, "
                "from_identity, from_url, from_runtime, to_kind, to_identity, "
                "to_url, to_runtime, to_version, to_manifest_hash, declared, "
                "plan, detected_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (mcp_name, STATUS_PENDING, values["from_kind"],
                 values["from_identity"], values["from_url"],
                 values["from_runtime"], values["to_kind"],
                 values["to_identity"], values["to_url"], values["to_runtime"],
                 values["to_version"], values["to_manifest_hash"], declared,
                 plan, now, now),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,),
        ).fetchone()
    return _row(row)


def set_source_change_status(
    mcp_name: str, status: str, *, accepted_by: str | None = None,
    result: dict | None = None,
) -> dict | None:
    """Move a row to ``status``; ``accepted_by`` stamps the accept, ``result``
    replaces the result document. Returns the row, ``None`` when absent."""
    now = _now()
    sets = ["status=%s", "updated_at=%s"]
    params: list = [status, now]
    if accepted_by is not None:
        sets += ["accepted_at=%s", "accepted_by=%s"]
        params += [now, accepted_by]
    if result is not None:
        sets.append("result=%s")
        params.append(json.dumps(result))
    params.append(mcp_name)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE mcp_source_changes SET {', '.join(sets)} WHERE mcp_name=%s",
            tuple(params),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,),
        ).fetchone()
    return _row(row) if row else None


def mark_source_change_notified(mcp_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mcp_source_changes SET notified_at=%s WHERE mcp_name=%s "
            "AND notified_at IS NULL",
            (_now(), mcp_name),
        )
        conn.commit()


def delete_source_change(mcp_name: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,),
        )
        conn.commit()
        return bool(cur.rowcount)


def delete_pending_source_change(mcp_name: str) -> bool:
    """Drop a ``pending`` row (the catalog reverted); a switching or switched
    row is kept."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM mcp_source_changes WHERE mcp_name = %s AND status = %s",
            (mcp_name, STATUS_PENDING),
        )
        conn.commit()
        return bool(cur.rowcount)


def interrupted_switches() -> list[str]:
    """The MCPs whose row says ``switching``: after a restart nothing holds
    the in-process install lock, so each is an interrupted switch."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name FROM mcp_source_changes WHERE status = %s",
            (STATUS_SWITCHING,),
        ).fetchall()
    return [r["mcp_name"] for r in rows]


def delete_mcp_rows(mcp_name: str) -> None:
    """Both tables' rows of a deleted MCP."""
    with get_conn() as conn:
        conn.execute("DELETE FROM mcp_update_checks WHERE mcp_name = %s", (mcp_name,))
        conn.execute("DELETE FROM mcp_source_changes WHERE mcp_name = %s", (mcp_name,))
        conn.commit()


# ---------------------------------------------------------------------------
# The declared renames, applied where the new key holds nothing yet
# ---------------------------------------------------------------------------

def rename_credential_key(mcp_name: str, old: str, new: str) -> dict:
    """Rename ``old`` to ``new`` in the MCP's infra rows and in every user's
    rows (per user and account). A row whose target already holds a value
    is left under its old key: the stored new value wins. Returns
    ``{"renamed": n, "kept": n}`` (rows moved, rows left)."""
    now = _now()
    with get_conn() as conn:
        infra = conn.execute(
            "UPDATE infra_credentials SET credential_key=%s, updated_at=%s "
            "WHERE mcp_name=%s AND credential_key=%s AND NOT EXISTS ("
            "SELECT 1 FROM infra_credentials i2 WHERE i2.mcp_name=%s AND i2.credential_key=%s)",
            (new, now, mcp_name, old, mcp_name, new),
        ).rowcount
        user = conn.execute(
            "UPDATE user_credentials u SET credential_key=%s, updated_at=%s "
            "WHERE u.mcp_name=%s AND u.credential_key=%s AND NOT EXISTS ("
            "SELECT 1 FROM user_credentials u2 WHERE u2.user_sub=u.user_sub "
            "AND u2.mcp_name=u.mcp_name AND u2.account_label=u.account_label "
            "AND u2.credential_key=%s)",
            (new, now, mcp_name, old, new),
        ).rowcount
        kept = conn.execute(
            "SELECT (SELECT COUNT(*) FROM infra_credentials WHERE mcp_name=%s AND credential_key=%s)"
            " + (SELECT COUNT(*) FROM user_credentials WHERE mcp_name=%s AND credential_key=%s) AS n",
            (mcp_name, old, mcp_name, old),
        ).fetchone()["n"]
        conn.commit()
    return {"renamed": int(infra or 0) + int(user or 0), "kept": int(kept or 0)}


def rename_instance_field_keys(mcp_name: str, renames: dict[str, str]) -> dict:
    """Rename field keys inside every instance of the MCP, one row at a
    time: decrypt, move the value when the new key holds nothing, write the
    row back as one UPDATE of its values and ``updated_at`` (``hosted_mode``
    and ``managed_by`` untouched). A row that does not decrypt is skipped
    and reported, never overwritten. Returns ``{"renamed": {old: new},
    "kept": [old], "unreadable": [instance ids]}``."""
    from storage.identity.credential_store import _decrypt, _encrypt
    renamed: dict[str, str] = {}
    kept: set[str] = set()
    unreadable: list[int] = []
    now = _now()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, field_values_enc FROM mcp_instances WHERE mcp_name=%s ORDER BY id",
            (mcp_name,),
        ).fetchall()
        for r in rows:
            try:
                values = json.loads(_decrypt(r["field_values_enc"]))
            except Exception:
                unreadable.append(int(r["id"]))
                continue
            if not isinstance(values, dict):
                unreadable.append(int(r["id"]))
                continue
            changed = False
            for old, new in renames.items():
                if values.get(old) in (None, ""):
                    continue
                if values.get(new) not in (None, ""):
                    kept.add(old)
                    continue
                values[new] = values.pop(old)
                renamed[old] = new
                changed = True
            if changed:
                conn.execute(
                    "UPDATE mcp_instances SET field_values_enc=%s, updated_at=%s WHERE id=%s",
                    (_encrypt(json.dumps(values)), now, r["id"]),
                )
        conn.commit()
    return {"renamed": renamed, "kept": sorted(kept), "unreadable": unreadable}


def rename_config_key(mcp_name: str, old: str, new: str) -> dict:
    """Rename a config value's key when the new key holds nothing."""
    with get_conn() as conn:
        moved = conn.execute(
            "UPDATE mcp_config_values SET config_key=%s WHERE mcp_name=%s AND config_key=%s "
            "AND NOT EXISTS (SELECT 1 FROM mcp_config_values c2 WHERE c2.mcp_name=%s AND c2.config_key=%s)",
            (new, mcp_name, old, mcp_name, new),
        ).rowcount
        kept = conn.execute(
            "SELECT COUNT(*) AS n FROM mcp_config_values WHERE mcp_name=%s AND config_key=%s",
            (mcp_name, old),
        ).fetchone()["n"]
        conn.commit()
    return {"renamed": int(moved or 0), "kept": int(kept or 0)}


# ---------------------------------------------------------------------------
# What is stored under an MCP's keys (the switch plan's input)
# ---------------------------------------------------------------------------

def stored_credential_keys(mcp_name: str) -> set[str]:
    """The credential keys with a stored value: the MCP's infra rows and
    every user's rows (no decryption, keys only)."""
    with get_conn() as conn:
        infra = conn.execute(
            "SELECT DISTINCT credential_key FROM infra_credentials WHERE mcp_name=%s",
            (mcp_name,),
        ).fetchall()
        user = conn.execute(
            "SELECT DISTINCT credential_key FROM user_credentials WHERE mcp_name=%s",
            (mcp_name,),
        ).fetchall()
    return {r["credential_key"] for r in infra} | {r["credential_key"] for r in user}


def stored_instance_keys(mcp_name: str) -> set[str]:
    """The instance field keys that hold a value in any instance of the MCP."""
    from storage.mcp import mcp_store
    keys: set[str] = set()
    for inst in mcp_store.get_mcp_instances(mcp_name):
        for key, value in (inst.get("field_values") or {}).items():
            if value not in (None, ""):
                keys.add(str(key))
    return keys


def stored_config_keys(mcp_name: str) -> set[str]:
    """The config keys with a stored value, the platform's control keys
    (``_``-prefixed) left out."""
    from storage.mcp import mcp_store
    return {
        k for k in mcp_store.get_mcp_config_values(mcp_name) if not k.startswith("_")
    }
