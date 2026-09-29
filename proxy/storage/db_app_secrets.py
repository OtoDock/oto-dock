"""The values behind an app's declared secrets (APPS.md "Secrets"): one row
per (app, name) in ``app_secrets``, Fernet-encrypted with the credential
store's key. A listing never decrypts — it reads names, who set them and
when; a value is decrypted only where it is used (the server's launch for
an ``env`` secret, the egress route for a ``sends_to`` one, an inbound
hook's verifier) and never returned by a route.

Synchronous psycopg calls, run through ``run_db`` / ``asyncio.to_thread``
like the other app stores.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage.identity.credential_store import decrypt_secret, encrypt_secret
from storage.pg import get_conn

logger = logging.getLogger("claude-proxy.apps")

VALUE_MAX_BYTES = 8 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_set(app_id: str) -> dict[str, dict]:
    """``{name: {set_by, updated_at}}`` for every stored value of the app
    — names only, nothing decrypted."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, set_by, updated_at FROM app_secrets WHERE app_id=%s ORDER BY name",
            (app_id,),
        ).fetchall()
    return {r["name"]: {"set_by": r["set_by"] or "", "updated_at": r["updated_at"] or ""}
            for r in rows}


def set_value(app_id: str, name: str, value: str, set_by: str) -> None:
    """Store (or replace) one value; the caller validated the name against
    the declared block and the value's size."""
    enc = encrypt_secret(value)
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO app_secrets (app_id, name, value_enc, set_by, updated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (app_id, name) DO UPDATE
               SET value_enc = EXCLUDED.value_enc, set_by = EXCLUDED.set_by,
                   updated_at = EXCLUDED.updated_at""",
            (app_id, name, enc, set_by, now),
        )
        conn.commit()


def delete_value(app_id: str, name: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM app_secrets WHERE app_id=%s AND name=%s", (app_id, name))
        conn.commit()
        return bool(cur.rowcount)


def values(app_id: str, names: list[str]) -> dict[str, str]:
    """The decrypted values of ``names`` (those that are set). For the
    launch and the egress route only — never a route's answer. A value the
    key cannot decrypt is left out with a warning (the boot canary names
    the store)."""
    if not names:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, value_enc FROM app_secrets WHERE app_id=%s AND name = ANY(%s)",
            (app_id, list(names)),
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        try:
            out[r["name"]] = decrypt_secret(r["value_enc"])
        except Exception:
            logger.warning("App %s: the secret %s did not decrypt (key mismatch?)", app_id, r["name"])
    return out
