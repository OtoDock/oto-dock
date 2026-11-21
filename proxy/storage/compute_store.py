"""A compute machine on another server's own records: the address its
sign-in records, its "Lock to this address", and its removal.

The pairing records the address the machine paired from
(``remote_store.exchange_pairing_token``); every sign-in after its secret
verified records the address it came from (``note_sign_in``) unless its lock
pins another one. The lock is an admin's act in person (``set_address_lock``).
Both read and write the row under its row lock, so a sign-in and a lock never
interleave. A removed machine's id and secret hash are kept for
``REMOVED_KEPT_DAYS`` (``record_removal``): only a sign-in that verifies
against them is told to remove itself (``removed_verifies``), so a restored
database or another install at the same address removes nothing. All
functions are synchronous (``run_db``).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core import placement
from storage.pg import get_conn

#: How long a removed machine's record answers its sign-in with its removal.
REMOVED_KEPT_DAYS = 30


@dataclass(frozen=True)
class SignInAddress:
    """What a sign-in's address did: ``admitted`` (recorded, or the address
    the lock pins), ``address`` the one recorded now (the locked one when
    refused), ``changed`` when the recorded address moved."""

    admitted: bool
    address: str
    changed: bool = False


def note_sign_in(machine_id: str, seen: str, *, refuse_locked: bool = False) -> SignInAddress:
    """A compute machine on another server signed in from ``seen`` with a
    secret that verified: recorded unless its lock pins another address.
    ``refuse_locked``: ``seen`` is shared by other clients, so a lock refuses
    it even when it is the pinned one."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT compute_address, compute_address_locked FROM remote_machines "
            "WHERE id = %s AND pairing_scope = %s FOR UPDATE",
            (machine_id, placement.PAIRING_COMPUTE),
        ).fetchone()
        if not row:
            conn.rollback()
            return SignInAddress(False, "")
        held = row["compute_address"] or ""
        if row["compute_address_locked"] and (held != seen or refuse_locked):
            conn.rollback()
            return SignInAddress(False, held)
        if held != seen:
            conn.execute("UPDATE remote_machines SET compute_address = %s WHERE id = %s", (seen, machine_id))
        conn.commit()
    return SignInAddress(True, seen, held != seen)


def set_address_lock(machine_id: str, locked: bool, address: str = "") -> str:
    """Turn a compute machine's address lock on or off; ``""`` when done,
    else why not. Locking pins the address it last signed in from, which
    must still be ``address`` (the one the admin saw), and needs a first
    sign-in."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT compute_address, last_seen FROM remote_machines "
            "WHERE id = %s AND pairing_scope = %s FOR UPDATE",
            (machine_id, placement.PAIRING_COMPUTE),
        ).fetchone()
        if not row:
            conn.rollback()
            return "Machine not found"
        if locked and not (row["last_seen"] and row["compute_address"]):
            conn.rollback()
            return "It has not signed in yet. Lock it once it is online."
        if locked and row["compute_address"] != address:
            conn.rollback()
            return f"Its address changed to {row['compute_address']}. Look again before you lock it."
        conn.execute("UPDATE remote_machines SET compute_address_locked = %s WHERE id = %s",
                     (bool(locked), machine_id))
        conn.commit()
    return ""


def pairing_states() -> dict[str, dict]:
    """Per compute row: whether it holds a sign-in (``paired``) and whether
    a pairing command waits for it (``waiting``), for the Compute card. The
    hashes themselves never leave this query."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, machine_secret_hash IS NOT NULL AS paired, pairing_token_hash IS NOT NULL AS waiting "
            "FROM remote_machines WHERE pairing_scope = %s",
            (placement.PAIRING_COMPUTE,),
        ).fetchall()
    return {r["id"]: {"paired": bool(r["paired"]), "waiting": bool(r["waiting"])} for r in rows}


def _prune_removed(conn) -> None:
    cut = (datetime.now(timezone.utc) - timedelta(days=REMOVED_KEPT_DAYS)).isoformat()
    conn.execute("DELETE FROM removed_compute_machines WHERE removed_at < %s", (cut,))


def record_removal(machine_id: str) -> bool:
    """Keep an added compute machine's id and secret hash before its row is
    deleted; nothing for another kind of row or one never paired. True when
    kept."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT machine_secret_hash FROM remote_machines WHERE id = %s AND pairing_scope = %s",
            (machine_id, placement.PAIRING_COMPUTE),
        ).fetchone()
        _prune_removed(conn)
        if not row or not row["machine_secret_hash"]:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO removed_compute_machines (id, secret_hash, removed_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET secret_hash = EXCLUDED.secret_hash, removed_at = EXCLUDED.removed_at",
            (machine_id, row["machine_secret_hash"], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return True


def removed_verifies(machine_id: str, machine_secret: str) -> bool:
    """Whether a sign-in is that of a compute machine an admin removed in
    the last ``REMOVED_KEPT_DAYS``: its id kept and its secret the one it had."""
    if not machine_id or not machine_secret:
        return False
    with get_conn() as conn:
        _prune_removed(conn)
        row = conn.execute(
            "SELECT secret_hash FROM removed_compute_machines WHERE id = %s", (machine_id,),
        ).fetchone()
        conn.commit()
    if not row:
        return False
    return hmac.compare_digest(hashlib.sha256(machine_secret.encode()).hexdigest(), row["secret_hash"])
