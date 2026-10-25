"""Remote machine store — CRUD for satellite daemon connections.

All functions are synchronous (called via asyncio.to_thread from async code).
Uses the same get_conn() pattern as all other storage modules.
"""

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.fernet import InvalidToken

# The placement vocabulary (the stored target value, the resolved kind, the
# pairing scope) and the descriptor this store builds from a machine row.
from core import placement
from storage.pg import get_conn
from ws import wire_events as wire

logger = logging.getLogger("otodock.remote-store")

# The persisted ``remote_machines.status`` column's three words, named once
# (core-seams phase 8): ``offline`` (the row a pairing mints), ``online``
# (the satellite authenticated or heartbeats), ``disconnected`` (its
# socket closed or its heartbeat lapsed). Every route overlays the LIVE
# state (``services/remote/remote_status.py``: online / stale / paused /
# disconnected / never_connected) — the column never reaches a client.
STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"
STATUS_DISCONNECTED = "disconnected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# Credential-bearing columns that must never leave the storage layer — API
# and WS callers forward these rows (or fields of them) to clients. The two
# verifiers (``exchange_pairing_token`` / ``verify_machine_secret``) read the
# hashes via their own targeted SELECTs, so every function that returns whole
# machine rows strips them first.
_SECRET_COLUMNS = (
    "pairing_token_hash",
    "pairing_token_created_at",
    "machine_secret_hash",
    # Own-browser mode's Playwright Extension token; read only by
    # get_target_browser_settings' targeted SELECT.
    "browser_extension_token_enc",
)


def _strip_secret_columns(row: dict) -> dict:
    for col in _SECRET_COLUMNS:
        row.pop(col, None)
    return row


# ---------------------------------------------------------------------------
# Machine CRUD
# ---------------------------------------------------------------------------

def create_remote_machine(
    machine_id: str,
    name: str,
    registered_by: str,
    pairing_scope: str = "admin",
    allow_full_fs: bool = False,
) -> dict:
    """Create a machine record and generate a one-time pairing token.

    ``pairing_scope`` distinguishes how the row was created:
      * ``"admin"`` (default) — paired via ``/v1/admin/remote-machines/pair``.
        Treated as platform infrastructure: can be agent-scope default
        for any agent; visible to all admins.
      * ``"user"`` — paired via ``/v1/users/me/remote-machines/pair``.
        Personal machine: only the owner's user-scope chats/tasks run
        there; only the owner can delete it.

    ``allow_full_fs`` (per-machine FS policy): caller-decided
    default. Both admin- and user-pairing flows default to False;
    the flag is enabled only by an explicit opt-in.

    Returns the machine dict with ``pairing_token`` (plaintext, not stored).
    """
    if pairing_scope not in placement.PAIRING_SCOPES:
        raise ValueError(f"invalid pairing_scope: {pairing_scope!r}")

    now = _now()
    token = secrets.token_urlsafe(32)
    token_hash = _sha256(token)

    with get_conn() as conn:
        # Check name uniqueness
        existing = conn.execute(
            "SELECT id FROM remote_machines WHERE name = %s", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"Machine name already in use: {name}")

        conn.execute(
            """INSERT INTO remote_machines
               (id, name, status, registered_by, pairing_scope,
                pairing_token_hash, pairing_token_created_at, capabilities,
                allow_full_fs, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, '{}', %s, %s)""",
            (machine_id, name, STATUS_OFFLINE, registered_by, pairing_scope,
             token_hash, now, bool(allow_full_fs), now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM remote_machines WHERE id = %s", (machine_id,)
        ).fetchone()
        result = _strip_secret_columns(dict(row))

    result["pairing_token"] = token
    return result


def get_remote_machine(machine_id: str) -> dict | None:
    """Fetch a remote machine record, joining the owner's platform role.

    Adds `owner_role` to the returned dict (e.g. 'admin' | 'creator' |
    'member' | '' when the owner has been deleted) for display/diagnostics.
    **Per-user satellite isolation keys on the stable `pairing_scope` column
    (set once at pairing time), NOT `owner_role`** — the owner's platform
    role is mutable (a user promoted to admin must not retroactively turn
    their personal user-paired laptop into an admin-shared machine).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rm.*, COALESCE(u.role, '') AS owner_role, "
            "       (rm.browser_extension_token_enc IS NOT NULL) AS browser_extension_token_set "
            "FROM remote_machines rm "
            "LEFT JOIN users u ON u.sub = rm.registered_by "
            "WHERE rm.id = %s",
            (machine_id,),
        ).fetchone()
        return _strip_secret_columns(dict(row)) if row else None


def placement_of(
    target: str, user_sub: str | None, agent_slug: str,
) -> placement.PlacementCapabilities:
    """The resolved placement of one session — the ONE builder the session
    builders call, once, from what ``resolve_execution_target`` (or a
    chat's pin) answered: ``local`` | a machine id | the offline sentinel.

    The local placement for an empty, ``local`` or sentinel target (a
    sentinel's session never starts — every consumer refuses before the
    registration; see ``core.placement``); the user-paired kind for a
    person's own machine, and for an admin machine the person's
    ``user_remote_targets`` row names for this agent (the dual-scope flow),
    else the agent-level default (admin-paired); the machine row read once
    and its facts parsed by ``placement.from_machine`` (a missing row: the
    kind with every fact empty — the path gate fail-closes).
    """
    if placement.is_local(target) or placement.is_offline_sentinel(target):
        return placement.LOCAL_PLACEMENT
    machine = get_remote_machine(target)
    kind = placement.KIND_ADMIN_REMOTE
    if machine and not placement.machine_is_admin_paired(machine):
        # A person's own machine is user-remote whatever pinned the chat
        # there (their default, or a move to it).
        kind = placement.KIND_USER_REMOTE
    elif user_sub:
        ut = get_user_remote_target(user_sub, agent_slug)
        if ut and ut.get("machine_id") == target:
            kind = placement.KIND_USER_REMOTE
    return placement.from_machine(kind, machine)


def get_all_remote_machines() -> list[dict]:
    """Return all machines with assigned agents + owner identity.

    LEFT-JOIN against ``users`` so user-paired rows carry the owner's
    `owner_display_name` + `owner_email` for the admin Remote Machines
    page's User-Paired section. Admin-paired rows get the same fields
    populated when the registering admin still exists, but the UI
    ignores them for that section.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rm.*, "
            "       COALESCE(u.display_name, '') AS owner_display_name, "
            "       COALESCE(u.email, '')        AS owner_email, "
            "       COALESCE(u.role, '')         AS owner_role, "
            "       (rm.browser_extension_token_enc IS NOT NULL) AS browser_extension_token_set "
            "FROM remote_machines rm "
            "LEFT JOIN users u ON u.sub = rm.registered_by "
            "ORDER BY rm.name"
        ).fetchall()
        machines = [_strip_secret_columns(dict(r)) for r in rows]

        # Attach assigned agents
        for m in machines:
            targets = conn.execute(
                "SELECT agent_slug FROM agent_remote_targets WHERE machine_id = %s ORDER BY agent_slug",
                (m["id"],),
            ).fetchall()
            m["assigned_agents"] = [t["agent_slug"] for t in targets]

        return machines


def update_machine_status(
    machine_id: str,
    status: str,
    *,
    last_seen: str | None = None,
) -> None:
    with get_conn() as conn:
        if last_seen:
            conn.execute(
                "UPDATE remote_machines SET status = %s, last_seen = %s WHERE id = %s",
                (status, last_seen, machine_id),
            )
        else:
            conn.execute(
                "UPDATE remote_machines SET status = %s WHERE id = %s",
                (status, machine_id),
            )
        conn.commit()


def set_offline_alerted(machine_id: str, alerted: bool) -> None:
    """Set the persisted `offline_alerted` flag for a machine.

    Drives the edge-triggered admin offline/online notifications in
    `core/remote/satellite_connection.py` so they survive proxy restarts: TRUE
    means admins currently hold an outstanding "offline" alert for this
    machine; FALSE means it's considered healthy (or never alerted).
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET offline_alerted = %s WHERE id = %s",
            (alerted, machine_id),
        )
        conn.commit()


def set_paused(machine_id: str, paused: bool) -> None:
    """Set the persisted deliberate-pause flag for a machine.

    TRUE = the user hit Pause in the tray (offline by intent), so the
    sustained-outage evaluator in `core/remote/satellite_connection.py` skips it
    (no false admin alert). Persisted so the suppression survives a proxy
    restart; cleared on the next successful auth (tray Resume / reboot).
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET paused = %s WHERE id = %s",
            (paused, machine_id),
        )
        conn.commit()


def update_machine_capabilities(machine_id: str, capabilities: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET capabilities = %s WHERE id = %s",
            (json.dumps(capabilities), machine_id),
        )
        conn.commit()


def set_allow_full_fs(machine_id: str, enabled: bool) -> None:
    """Toggle the per-machine filesystem-access policy.

    When ``True``, agents running on this satellite can read/write any
    path the OS user can reach (system files, other directories, etc.).
    When ``False``, agents are limited to the agent tree plus the OS
    user's home directory as reported in ``capabilities.home_dir``.

    Default at pairing time is set by the pairing flow (admin pairing
    pre-checks ``True``; user pairing pre-checks ``False``); admins can
    override per-machine via the admin remote-machines page, users via
    Settings → Remote machines for their own paired machines.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET allow_full_fs = %s WHERE id = %s",
            (bool(enabled), machine_id),
        )
        conn.commit()


def set_remote_machine_max_sessions(machine_id: str, value: int | None) -> None:
    """Set the per-machine concurrent-session override (admin scope).

    ``value`` is the proxy-side soft cap on locally-tracked concurrent
    sessions for this satellite; ``None`` clears the override so the
    satellite's own reported recommendation (``capabilities.recommended_max_sessions``)
    is used instead. The satellite hard-caps at its physical max on its own —
    this column is only the proxy-side override honored by
    ``SatelliteConnectionManager.machine_at_capacity``. No push is needed.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET max_sessions = %s WHERE id = %s",
            (int(value) if value is not None else None, machine_id),
        )
        conn.commit()


def set_device_grants(machine_id: str, grants: list[str]) -> None:
    """Set the per-machine device-control consent set — the capabilities
    (``computer`` / ``browser`` / ``app``) the owner
    permits device-local MCPs to use on this satellite.

    Defaults to ``[]`` at pairing time for BOTH admin- and user-paired machines
    (device control is strictly more powerful than allow_full_fs). Admins toggle
    per-capability on the admin remote-machines page; owners via Settings →
    Remote machines for their own machines. The CALLER (the endpoint) validates
    the grant keys against the known capability set; the store persists the
    de-duplicated, sorted list verbatim.
    """
    cleaned = sorted({str(g) for g in (grants or [])})
    with get_conn() as conn:
        if "browser" in cleaned:
            conn.execute(
                "UPDATE remote_machines SET device_grants = %s WHERE id = %s",
                (json.dumps(cleaned), machine_id),
            )
        else:
            # Revoking browser control revokes the whole own-browser consent
            # unit (mode + token): a later re-grant starts from the dedicated
            # default instead of silently reviving the user's-browser choice.
            conn.execute(
                "UPDATE remote_machines SET device_grants = %s, "
                "browser_mode = 'dedicated', browser_extension_token_enc = NULL, "
                "browser_unattended = FALSE "
                "WHERE id = %s",
                (json.dumps(cleaned), machine_id),
            )
        conn.commit()


# --- Own-browser mode (browser-control) ---

BROWSER_MODES = ("dedicated", "own")


@dataclass(frozen=True)
class BrowserTargetSettings:
    """Which browser the browser-control MCP drives on a target machine —
    ``dedicated`` (the per-agent profile) or ``own`` (the OS user's signed-in
    browser through the Playwright Extension) — and, for ``own``, the
    decrypted extension token. ``extension_token`` None = every session asks
    the user to click Allow in the browser."""

    mode: str = "dedicated"
    extension_token: str | None = None
    #: Whether an unattended session (a task, a call, a meeting) may use the
    #: token; off, only a session a person drives gets it.
    unattended: bool = False


def _parse_browser_mode(raw) -> str:
    """Fail-closed: anything but the literal ``own`` is the dedicated profile."""
    return "own" if raw == "own" else "dedicated"


def set_browser_mode(machine_id: str, mode: str) -> None:
    """Switch the machine's browser-control mode. The extension token stays
    stored across switches (it belongs to the machine, and it is only ever
    delivered while the mode is ``own``) — an explicit clear or revoking the
    ``browser`` grant removes it, a round trip through ``dedicated`` does not."""
    if mode not in BROWSER_MODES:
        raise ValueError(f"unknown browser mode {mode!r}")
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET browser_mode = %s WHERE id = %s",
            (mode, machine_id),
        )
        conn.commit()


def set_browser_extension_token(machine_id: str, token: str | None) -> None:
    """Store (Fernet, credential-store key) or clear (None) the machine's
    Playwright Extension token. The endpoint validates the token shape and
    the mode; the store persists verbatim. Clearing it clears the
    unattended permission too, so a new token starts attended-only."""
    from storage.identity import credential_store
    enc = credential_store.encrypt_secret(token) if token else None
    with get_conn() as conn:
        if enc is None:
            conn.execute(
                "UPDATE remote_machines SET browser_extension_token_enc = NULL, "
                "browser_unattended = FALSE WHERE id = %s", (machine_id,))
        else:
            conn.execute(
                "UPDATE remote_machines SET browser_extension_token_enc = %s WHERE id = %s",
                (enc, machine_id),
            )
        conn.commit()


def set_browser_unattended(machine_id: str, enabled: bool) -> None:
    """Whether unattended sessions get the machine's own-browser token."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET browser_unattended = %s WHERE id = %s",
            (bool(enabled), machine_id),
        )
        conn.commit()


def get_target_browser_settings(target: placement.PlacementCapabilities) -> BrowserTargetSettings:
    """Browser-control mode + decrypted extension token for a remote target.
    Dedicated (no token) for a local placement, an unknown machine, or one
    on the dedicated profile — the token is decrypted only when the mode is
    ``own``, through this targeted SELECT (the column never rides a machine
    row, and never the placement the session persists; see
    ``_SECRET_COLUMNS``). A decrypt failure (key mismatch) is logged and
    degrades to "no token" — the session then asks in the browser."""
    if not target.is_remote or not target.machine_id:
        return BrowserTargetSettings()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT browser_mode, browser_extension_token_enc, browser_unattended "
            "FROM remote_machines WHERE id = %s",
            (target.machine_id,),
        ).fetchone()
    if not row or _parse_browser_mode(row["browser_mode"]) != "own":
        return BrowserTargetSettings()
    token = None
    enc = row["browser_extension_token_enc"]
    if enc:
        from storage.identity import credential_store
        try:
            token = credential_store.decrypt_secret(enc)
        except (InvalidToken, UnicodeDecodeError):
            logger.warning(
                "Failed to decrypt the browser extension token of machine %s "
                "(key mismatch? see the CREDENTIAL KEY MISMATCH boot check)",
                target.machine_id[:8],
            )
    return BrowserTargetSettings(mode="own", extension_token=token,
                                 unattended=bool(row["browser_unattended"]))


# ---------------------------------------------------------------------------
# Auto-update helpers (auto_update_enabled, satellite_version, etc.)
# ---------------------------------------------------------------------------


def set_auto_update_enabled(machine_id: str, enabled: bool) -> None:
    """Toggle the per-machine auto-update policy. When False, the proxy
    rejects version-mismatched satellites instead of pushing the new
    tarball; an admin must click "Update now" to trigger the push.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET auto_update_enabled = %s WHERE id = %s",
            (enabled, machine_id),
        )
        conn.commit()


def set_satellite_version(machine_id: str, version: str) -> None:
    """Record the satellite version observed at auth time. Surfaced in the
    admin dashboard alongside "Update available" badges.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET satellite_version = %s WHERE id = %s",
            (version, machine_id),
        )
        conn.commit()


def record_update_result(
    machine_id: str, *, target_version: str = "", error: str | None = None,
) -> None:
    """Record the outcome of an update attempt. Success: clears the error
    column, stamps last_update_at, sets satellite_version to target and
    resets the rollback budget (``update_rollback_count/target``). Failure:
    keeps the previous satellite_version and stores the error text (a push
    that never left the proxy — for a rolled-back push use
    ``record_update_rollback``, which also counts it).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if error is None:
            conn.execute(
                "UPDATE remote_machines SET last_update_at = %s, "
                "satellite_version = %s, last_update_error = NULL, "
                "pending_update = FALSE, "
                "update_rollback_count = 0, update_rollback_target = NULL "
                "WHERE id = %s",
                (now, target_version, machine_id),
            )
        else:
            conn.execute(
                "UPDATE remote_machines SET last_update_error = %s, "
                "last_update_at = %s WHERE id = %s",
                (error, now, machine_id),
            )
        conn.commit()


def record_update_rollback(
    machine_id: str, *, target_version: str, error: str,
) -> int:
    """Record that a pushed update to ``target_version`` rolled back (the
    satellite reconnected BELOW the target). Counts consecutive rollbacks
    PER TARGET: the same target increments, a different target starts a
    fresh budget at 1. Stores the error, stamps ``last_update_at`` and
    CLEARS ``pending_update`` — the explicit "Update now" attempt was
    consumed, so an offline-queued push cannot loop on every reconnect.
    Returns the new count; ``ws/satellite.py`` stops automatic pushes of
    that target at ``MAX_AUTO_UPDATE_ROLLBACKS``.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE remote_machines SET "
            "update_rollback_count = CASE WHEN update_rollback_target = %s "
            "THEN update_rollback_count + 1 ELSE 1 END, "
            "update_rollback_target = %s, last_update_error = %s, "
            "last_update_at = %s, pending_update = FALSE "
            "WHERE id = %s RETURNING update_rollback_count",
            (target_version, target_version, error, now, machine_id),
        ).fetchone()
        conn.commit()
    return int(row["update_rollback_count"]) if row else 0


def set_pending_update(machine_id: str, pending: bool) -> None:
    """Flag a machine for forced-update on next reconnect, even when
    ``auto_update_enabled=False``. Set by the admin "Update now" endpoint
    when the machine is currently offline; cleared on successful update.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE remote_machines SET pending_update = %s WHERE id = %s",
            (pending, machine_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Admin lock-out helpers (allow_user_paired_machines)
# ---------------------------------------------------------------------------


def get_all_user_paired_machines() -> list[dict]:
    """Return every user-paired machine (``pairing_scope = 'user'``).

    Used by the ``allow_user_paired_machines`` admin toggle when flipped
    off: we iterate this list, deregister the live WS, and clear targets.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM remote_machines WHERE pairing_scope = 'user'"
        ).fetchall()
        return [_strip_secret_columns(dict(r)) for r in rows]


def clear_user_remote_targets_for_machine(machine_id: str) -> int:
    """Delete every ``user_remote_targets`` row pointing at this machine.

    Returns the row count deleted. Called when the admin disables
    user-paired machines so stale targets don't keep routing at a
    disconnected satellite.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_remote_targets WHERE machine_id = %s",
            (machine_id,),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# Pairing token exchange
# ---------------------------------------------------------------------------

PAIRING_TOKEN_EXPIRY_HOURS = 1


def exchange_pairing_token(
    machine_id: str,
    pairing_token: str,
) -> str:
    """Exchange a pairing token for a machine secret.

    Validates the token hash and expiry, then:
    - Clears the pairing token (one-time use)
    - Generates and stores a machine secret hash
    - Returns the plaintext secret

    Raises ValueError on invalid/expired token.
    """
    token_hash = _sha256(pairing_token)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT pairing_token_hash, pairing_token_created_at FROM remote_machines WHERE id = %s",
            (machine_id,),
        ).fetchone()
        if not row:
            raise ValueError("Machine not found")
        if not row["pairing_token_hash"]:
            raise ValueError("Pairing token already exchanged")
        if not hmac.compare_digest(row["pairing_token_hash"], token_hash):
            raise ValueError("Invalid pairing token")

        # Check expiry
        created_at = datetime.fromisoformat(row["pairing_token_created_at"])
        if datetime.now(timezone.utc) - created_at > timedelta(hours=PAIRING_TOKEN_EXPIRY_HOURS):
            raise ValueError("Pairing token expired")

        # Generate machine secret
        machine_secret = secrets.token_urlsafe(48)
        secret_hash = _sha256(machine_secret)

        # Conditional on the hash still being present: two racing exchanges
        # (a token thief vs the legitimate installer) must produce ONE winner
        # and one loud "already exchanged" error — an unconditional UPDATE
        # would let the loser silently overwrite the winner's secret.
        cur = conn.execute(
            "UPDATE remote_machines SET pairing_token_hash = NULL, "
            "pairing_token_created_at = NULL, machine_secret_hash = %s "
            "WHERE id = %s AND pairing_token_hash = %s",
            (secret_hash, machine_id, token_hash),
        )
        conn.commit()
        if cur.rowcount != 1:
            raise ValueError("Pairing token already exchanged")

    return machine_secret


def verify_machine_secret(machine_id: str, machine_secret: str) -> bool:
    """Verify a machine secret using constant-time comparison."""
    import hmac
    secret_hash = _sha256(machine_secret)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT machine_secret_hash FROM remote_machines WHERE id = %s",
            (machine_id,),
        ).fetchone()
        if not row or not row["machine_secret_hash"]:
            return False
        return hmac.compare_digest(secret_hash, row["machine_secret_hash"])


# ---------------------------------------------------------------------------
# Machine deletion (cascade)
# ---------------------------------------------------------------------------

def delete_remote_machine(machine_id: str) -> bool:
    """Delete a machine and reset agents that target it to 'local'.

    Chats pinned to the machine transition to "auto-continue" (resilience
    #11): the pin + session ids are cleared so the next turn fresh-resolves
    the agent's current target and spawns a new session, and
    ``pending_history_seed`` marks the chat for a DB-history digest injection
    on that turn (core/session/history_seed.py). The on-disk CLI/Codex sessions lived
    only on the deleted machine — they are unrecoverable by design.
    """
    with get_conn() as conn:
        machine_row = conn.execute(
            "SELECT name FROM remote_machines WHERE id = %s", (machine_id,)
        ).fetchone()
        machine_name = (machine_row or {}).get("name") or machine_id[:8]
        # A chat moved TO this machine whose source still holds its session
        # files (the move waits for proof) goes back there with its ids; the
        # move rows naming the machine settle; a task's machine goes.
        from storage import move_schema as _mv
        _now_s = _now()
        for r in conn.execute(
            """SELECT DISTINCT ON (m.chat_id) m.id, m.chat_id, m.source, m.target,
                      m.state, m.memory, m.created_at
                 FROM chat_moves m JOIN chats c ON c.id = m.chat_id
                WHERE c.execution_target = %s
                ORDER BY m.chat_id, m.created_at DESC""",
            (machine_id,),
        ).fetchall():
            if (r["state"] == _mv.AWAITING_RESUME and r["memory"] == _mv.MEMORY_FILES
                    and r["target"] == machine_id and r["source"] != machine_id):
                # The source's copy misses any turn the chat ran on this
                # machine since the move: those come back as a digest of
                # just those turns on the next one (``returned:<move id>``).
                conn.execute(
                    """UPDATE chats SET execution_target = %s,
                              pending_history_seed = CASE WHEN EXISTS (
                                  SELECT 1 FROM chat_messages cm
                                   WHERE cm.chat_id = chats.id
                                     AND cm.role IN ('user', 'assistant')
                                     AND cm.created_at > %s)
                                THEN %s ELSE pending_history_seed END
                        WHERE id = %s AND execution_target = %s""",
                    (r["source"], r["created_at"], f"returned:{r['id']}", r["chat_id"],
                     machine_id),
                )
                conn.execute(
                    "UPDATE chat_moves SET state = %s, settled_at = %s WHERE id = %s",
                    (_mv.RETURNED, _now_s, r["id"]),
                )
        for col, state in (("source", _mv.SOURCE_GONE), ("target", _mv.TARGET_GONE)):
            conn.execute(
                f"UPDATE chat_moves SET state = %s, settled_at = %s "
                f"WHERE {col} = %s AND state = ANY(%s)",
                (state, _now_s, machine_id, [_mv.MOVING, _mv.AWAITING_RESUME]),
            )
        conn.execute(
            "UPDATE dynamic_tasks SET target_machine = '' WHERE target_machine = %s",
            (machine_id,),
        )
        # Transition pinned chats to auto-continue. last_turn_aborted is
        # cleared too: the history digest already contains the aborted turn,
        # so the cancelled-context injection must not fire on top of it.
        # context_used is zeroed so the meter doesn't show the dead session's
        # fill. Direct-LLM/phone chats are structurally 'local' — never match.
        conn.execute(
            """UPDATE chats
                  SET execution_target = '', session_id = NULL,
                      codex_thread_id = NULL, last_turn_aborted = FALSE,
                      last_abort_graceful = FALSE, context_used = 0,
                      pending_history_seed = %s
                WHERE execution_target = %s""",
            (f"machine_removed:{machine_name}", machine_id),
        )
        # Reset agents that default to this machine
        conn.execute(
            "UPDATE agents SET execution_target = 'local' WHERE execution_target = %s",
            (machine_id,),
        )
        # Delete agent targets (FK cascade would do this, but be explicit)
        conn.execute(
            "DELETE FROM agent_remote_targets WHERE machine_id = %s",
            (machine_id,),
        )
        # Delete user targets
        conn.execute(
            "DELETE FROM user_remote_targets WHERE machine_id = %s",
            (machine_id,),
        )
        cur = conn.execute(
            "DELETE FROM remote_machines WHERE id = %s", (machine_id,)
        )
        conn.commit()
        deleted = cur.rowcount > 0

    if deleted:
        # Invalidate agent cache since execution_target may have changed
        from storage.agents.agent_store import _invalidate_cache
        _invalidate_cache()

    return deleted


def _rebind(chat_id: str, sets: str, set_args: tuple, *, expect_target: str,
            expect_session_id: str | None, expect_thread: str | None) -> bool:
    """One chat's place changed by a move, as a compare-and-set on the place
    and the ids the move read: a second move, or a spawn that re-bound the
    row in between, makes it a no-op (False)."""
    with get_conn() as conn:
        cur = conn.execute(
            f"""UPDATE chats SET {sets}
                 WHERE id = %s AND COALESCE(execution_target, '') = %s
                   AND session_id IS NOT DISTINCT FROM %s
                   AND codex_thread_id IS NOT DISTINCT FROM %s""",
            (*set_args, chat_id, expect_target or "", expect_session_id or None,
             expect_thread or None),
        )
        conn.commit()
        return cur.rowcount > 0


def rebind_chat_with_files(chat_id: str, target: str, *, expect_target: str,
                           expect_session_id: str | None, expect_thread: str | None) -> bool:
    """The move WITH the session files: the chat's place becomes ``target``
    and it keeps its ids and its context fill (the engine's files are there
    now, so the next turn resumes the same session); any pending seed goes."""
    return _rebind(chat_id, "execution_target = %s, pending_history_seed = ''", (target,),
                   expect_target=expect_target, expect_session_id=expect_session_id,
                   expect_thread=expect_thread)


def rebind_chat_to_target(chat_id: str, target: str, move_id: str, *, expect_target: str,
                          expect_session_id: str | None, expect_thread: str | None) -> bool:
    """The move WITHOUT the files (the source offline, the transfer failed):
    the ``delete_remote_machine`` per-chat shape scoped to one row, pinned to
    the CHOSEN ``target``: the session ids are dropped so the next turn
    spawns fresh there, and ``pending_history_seed='moved:<move id>'`` marks
    the chat for the DB-history digest (the move row names both places and
    why). The old session stays where it lived."""
    return _rebind(
        chat_id,
        """execution_target = %s, session_id = NULL, codex_thread_id = NULL,
           last_turn_aborted = FALSE, last_abort_graceful = FALSE,
           context_used = 0, pending_history_seed = %s""",
        (target, f"moved:{move_id}"),
        expect_target=expect_target, expect_session_id=expect_session_id,
        expect_thread=expect_thread,
    )


# ---------------------------------------------------------------------------
# Chat moves (storage/move_schema.py)
# ---------------------------------------------------------------------------

_MOVE_COLUMNS = (
    "id", "chat_id", "agent", "engine", "session_id", "resume_handle", "home_user",
    "source", "target", "from_label", "to_label", "memory", "why", "state",
    "files", "bytes", "main_rel", "main_size", "actor", "origin", "created_at", "settled_at",
)


def create_chat_move(row: dict) -> None:
    """Write a move's row (its ``state`` and ids set by the caller)."""
    cols = [c for c in _MOVE_COLUMNS if c in row]
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO chat_moves ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            tuple(row[c] for c in cols),
        )
        conn.commit()


def update_chat_move(move_id: str, **fields) -> None:
    """Update a move's row; a settling state stamps ``settled_at``."""
    from storage import move_schema
    fields = {k: v for k, v in fields.items() if k in _MOVE_COLUMNS and k != "id"}
    if fields.get("state") and fields["state"] not in (move_schema.MOVING,
                                                       move_schema.AWAITING_RESUME):
        fields.setdefault("settled_at", _now())
    if not fields:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE chat_moves SET {', '.join(f'{k} = %s' for k in fields)} WHERE id = %s",
            (*fields.values(), move_id),
        )
        conn.commit()


def get_chat_move(move_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_moves WHERE id = %s", (move_id,)).fetchone()
        return dict(row) if row else None


def latest_chat_move(chat_id: str) -> dict | None:
    """The chat's newest move row, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_moves WHERE chat_id = %s ORDER BY created_at DESC, id LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def tool_moves_since_person(chat_id: str) -> int:
    """The agent's own moves of the chat (``origin`` tool, any that did not
    fail) since the chat's last message from a person (a ``user`` row with an
    author: the platform's own prompts carry none)."""
    from storage import move_schema
    with get_conn() as conn:
        row = conn.execute(
            """SELECT count(*) AS n FROM chat_moves
                WHERE chat_id = %s AND origin = %s AND state <> %s
                  AND created_at > COALESCE(
                      (SELECT max(created_at) FROM chat_messages
                        WHERE chat_id = %s AND role = 'user'
                          AND COALESCE(author_sub, '') <> ''), '')""",
            (chat_id, move_schema.ORIGIN_TOOL, move_schema.FAILED, chat_id),
        ).fetchone()
        return int((row or {}).get("n") or 0)


def add_switch_leftover(row: dict) -> None:
    """Record the session files an engine switch left (``row``: the
    ``engine_switch_leftovers`` columns)."""
    cols = ("id", "chat_id", "agent", "engine", "session_id", "resume_handle",
            "home_user", "place", "created_at")
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO engine_switch_leftovers ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))})",
            tuple(row[c] for c in cols),
        )
        conn.commit()


def switch_leftovers() -> list[dict]:
    """Every engine switch's leftover, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM engine_switch_leftovers ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]


def drop_switch_leftover(leftover_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM engine_switch_leftovers WHERE id = %s", (leftover_id,))
        conn.commit()


def last_answer_since(chat_id: str, since: str) -> tuple[int, list[str]] | None:
    """The chat's last answer of its agent written after ``since`` (an ISO
    stamp, as the rows store theirs), as ``(row id, the event_data of every
    system event row after it)``; None when the agent wrote none."""
    with get_conn() as conn:
        last = conn.execute(
            "SELECT max(id) AS id FROM chat_messages WHERE chat_id = %s AND role = 'assistant' "
            "AND created_at > %s",
            (chat_id, since),
        ).fetchone()
        if not last or last["id"] is None:
            return None
        rows = conn.execute(
            "SELECT event_data FROM chat_messages WHERE chat_id = %s AND id > %s "
            "AND role = 'event' AND event_type = %s ORDER BY id",
            (chat_id, last["id"], wire.SYSTEM),
        ).fetchall()
        return int(last["id"]), [str(r["event_data"] or "") for r in rows]


def chat_moves_in(states: tuple[str, ...]) -> list[dict]:
    """Every move row in one of ``states``, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_moves WHERE state = ANY(%s) ORDER BY created_at",
            (list(states),),
        ).fetchall()
        return [dict(r) for r in rows]


def session_refs_shared(chat_id: str, refs: list[str]) -> bool:
    """Whether anything besides the chat ``chat_id`` still names one of
    ``refs``: another chat row's session or thread id, or a task that
    continues the session (a delegation's ``continue_session``). Such a
    session stays where it is: its next round resumes it there."""
    refs = [r for r in refs if r]
    if not refs:
        return False
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 WHERE EXISTS (
                   SELECT 1 FROM chats
                    WHERE id != %s AND (session_id = ANY(%s) OR codex_thread_id = ANY(%s)))
                OR EXISTS (
                   SELECT 1 FROM dynamic_tasks WHERE continue_session = ANY(%s))""",
            (chat_id, refs, refs, refs),
        ).fetchone()
        return row is not None


def chat_continued_by_task(chat: dict) -> bool:
    """Whether a task continues the chat's session (a delegation's
    ``continue_session`` names its session or thread id)."""
    refs = [r for r in (chat.get("session_id"), chat.get("codex_thread_id")) if r]
    if not refs:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM dynamic_tasks WHERE continue_session = ANY(%s) LIMIT 1", (refs,),
        ).fetchone()
        return row is not None


def fail_running_moves() -> int:
    """A move a proxy restart cut short: its job is gone, so it failed (the
    chat's place is whatever its compare-and-set left). At startup."""
    from storage import move_schema
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_moves SET state = %s, settled_at = %s WHERE state = %s",
            (move_schema.FAILED, _now(), move_schema.MOVING),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# ---------------------------------------------------------------------------
# Agent-machine targeting
# ---------------------------------------------------------------------------

def get_agent_remote_targets(agent_slug: str) -> list[dict]:
    """Return remote machine targets for an agent."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT art.*, rm.name, rm.status, rm.capabilities "
            "FROM agent_remote_targets art "
            "JOIN remote_machines rm ON rm.id = art.machine_id "
            "WHERE art.agent_slug = %s ORDER BY rm.name",
            (agent_slug,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_agent_remote_target(
    agent_slug: str,
    machine_id: str,
    added_by: str,
) -> None:
    """Set a machine as the remote target for an agent (v1: one target per agent).

    Replaces any existing target and updates the agents.execution_target column.
    """
    now = _now()
    with get_conn() as conn:
        # Remove existing targets (v1: one target per agent)
        conn.execute(
            "DELETE FROM agent_remote_targets WHERE agent_slug = %s",
            (agent_slug,),
        )
        conn.execute(
            """INSERT INTO agent_remote_targets
               (agent_slug, machine_id, added_by, is_default, created_at)
               VALUES (%s, %s, %s, TRUE, %s)""",
            (agent_slug, machine_id, added_by, now),
        )
        # Update the agent's execution_target
        conn.execute(
            "UPDATE agents SET execution_target = %s, updated_at = %s WHERE slug = %s",
            (machine_id, now, agent_slug),
        )
        conn.commit()

    from storage.agents.agent_store import _invalidate_cache
    _invalidate_cache()


def remove_agent_remote_target(agent_slug: str) -> None:
    """Remove the remote target for an agent, resetting to local."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM agent_remote_targets WHERE agent_slug = %s",
            (agent_slug,),
        )
        conn.execute(
            "UPDATE agents SET execution_target = 'local', updated_at = %s WHERE slug = %s",
            (now, agent_slug),
        )
        conn.commit()

    from storage.agents.agent_store import _invalidate_cache
    _invalidate_cache()


def get_agents_for_machine(machine_id: str) -> list[str]:
    """Return agent slugs assigned to a machine."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_slug FROM agent_remote_targets WHERE machine_id = %s ORDER BY agent_slug",
            (machine_id,),
        ).fetchall()
        return [r["agent_slug"] for r in rows]


def get_default_machine_for_agent(agent_slug: str) -> dict | None:
    """Return the default remote machine for an agent, or None if local."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rm.* FROM remote_machines rm "
            "JOIN agent_remote_targets art ON art.machine_id = rm.id "
            "WHERE art.agent_slug = %s AND art.is_default = TRUE",
            (agent_slug,),
        ).fetchone()
        return _strip_secret_columns(dict(row)) if row else None


# ---------------------------------------------------------------------------
# User-level remote targeting
# ---------------------------------------------------------------------------

def get_user_remote_target(user_sub: str, agent_slug: str) -> dict | None:
    """Get the user's machine for one specific agent.

    Legacy global override (``agent_slug=''``) is gone — users
    select per-agent in the UI. Returns None if no row matches.
    """
    if not agent_slug:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT urt.*, rm.name, rm.status, rm.capabilities "
            "FROM user_remote_targets urt "
            "JOIN remote_machines rm ON rm.id = urt.machine_id "
            "WHERE urt.user_sub = %s AND urt.agent_slug = %s",
            (user_sub, agent_slug),
        ).fetchone()
        return dict(row) if row else None


def get_user_remote_targets(user_sub: str) -> list[dict]:
    """All targets for a user (for settings page)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT urt.*, rm.name, rm.status, rm.capabilities "
            "FROM user_remote_targets urt "
            "JOIN remote_machines rm ON rm.id = urt.machine_id "
            "WHERE urt.user_sub = %s ORDER BY urt.agent_slug",
            (user_sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_visible_machines_for_user(
    user_sub: str, *, include_admin_paired: bool = False,
) -> list[dict]:
    """Machines a user should see in their personal "Remote Machines" view.

    Always includes user-paired machines registered by ``user_sub``.
    When ``include_admin_paired=True`` (typically for admins viewing
    their own UserSettings), also includes every admin-paired machine —
    they can attach user-scope agent targets to those too.

    The frontend uses the ``pairing_scope`` field to badge admin-paired
    rows distinctly and hide the "Remove" button (deletion of those
    stays in the admin dashboard).
    """
    with get_conn() as conn:
        if include_admin_paired:
            rows = conn.execute(
                "SELECT rm.*, (rm.browser_extension_token_enc IS NOT NULL) AS browser_extension_token_set "
                "FROM remote_machines rm "
                "WHERE (registered_by = %s AND pairing_scope = 'user') "
                "   OR pairing_scope = 'admin' "
                "ORDER BY pairing_scope DESC, name",  # user-paired first, then admin
                (user_sub,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT rm.*, (rm.browser_extension_token_enc IS NOT NULL) AS browser_extension_token_set "
                "FROM remote_machines rm "
                "WHERE registered_by = %s AND pairing_scope = 'user' "
                "ORDER BY name",
                (user_sub,),
            ).fetchall()
        return [_strip_secret_columns(dict(r)) for r in rows]


def set_user_remote_target(
    user_sub: str, machine_id: str, agent_slug: str = "",
) -> None:
    """Set user's remote target (upsert). Validates machine exists."""
    with get_conn() as conn:
        # Verify machine exists
        machine = conn.execute(
            "SELECT id FROM remote_machines WHERE id = %s", (machine_id,)
        ).fetchone()
        if not machine:
            raise ValueError("Machine not found")

        now = _now()
        conn.execute(
            """INSERT INTO user_remote_targets (user_sub, machine_id, agent_slug, added_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_sub, agent_slug)
               DO UPDATE SET machine_id = EXCLUDED.machine_id, added_at = EXCLUDED.added_at""",
            (user_sub, machine_id, agent_slug, now),
        )
        conn.commit()


def remove_user_remote_target(user_sub: str, agent_slug: str = "") -> None:
    """Remove user's target override (revert to agent default)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_remote_targets WHERE user_sub = %s AND agent_slug = %s",
            (user_sub, agent_slug),
        )
        conn.commit()


def clear_user_remote_targets_for_agent(agent_slug: str) -> int:
    """Remove EVERY user's personal-machine override pinning ``agent_slug``.

    Used when an agent switches to Shared-only (agent-scoped) mode — it may no
    longer run on user-paired machines (its one shared chat history can't live on
    a personal box). Admin defaults (``agent_remote_targets`` → admin machines)
    are untouched. Returns the number of overrides removed."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_remote_targets WHERE agent_slug = %s", (agent_slug,)
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
