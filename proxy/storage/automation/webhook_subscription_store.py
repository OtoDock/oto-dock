"""Webhook subscription CRUD.

Storage layer for the ``webhook_subscriptions`` table. Each row represents a
single vendor-side webhook registration (one repo on GitHub, one channel on
Slack, one drive on MS Graph, etc.) that we've bound to a user's OAuth
account. Triggers reference these via ``triggers.subscription_id`` to fan
out on incoming events.

Service-layer orchestration (vendor API calls, OAuth token lookup, secret
generation) lives in ``services/webhooks/subscription_manager.py`` — this module is
pure storage, mirrors ``trigger_store.py`` patterns.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from storage.identity.credential_store import _decrypt, _encrypt
from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The status vocabulary (``webhook_subscriptions.status``, named once —
# core-seams phase 8; the dashboard mirror is ``lib/status/webhookSubscription.ts``)
# and the allowed transitions, enforced by ``update_subscription_status``.
CREATING = "creating"          # vendor API call in flight
ACTIVE = "active"              # vendor confirmed registration; receiving events
FAILED = "failed"              # vendor create call failed; user must delete + recreate
RENEW_FAILED = "renew_failed"  # renew vendor call failed; vendor may still send events
EXPIRED = "expired"            # vendor TTL passed without successful renew
DISABLED = "disabled"          # user paused

STATUSES: frozenset[str] = frozenset({CREATING, ACTIVE, FAILED, RENEW_FAILED, EXPIRED, DISABLED})
#: The dispatcher accepts an inbound event (a row still being created
#: may already receive its first delivery).
RECEIVING: frozenset[str] = frozenset({ACTIVE, RENEW_FAILED, CREATING})
#: The relay fan-out delivers to these rows.
DELIVERING: frozenset[str] = frozenset({ACTIVE, RENEW_FAILED})

_VALID_STATUSES = STATUSES
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CREATING:     {ACTIVE, FAILED},
    ACTIVE:       {RENEW_FAILED, DISABLED},
    RENEW_FAILED: {ACTIVE, EXPIRED, DISABLED},
    EXPIRED:      {ACTIVE, DISABLED},
    DISABLED:     {ACTIVE},
    FAILED:       set(),  # terminal — caller must DELETE + recreate
}


def _coerce_json(value: Any) -> Any:
    """psycopg may return JSONB as already-parsed Python; tolerate both."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _row_to_dict(row: dict | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["selected_events"] = _coerce_json(d.get("selected_events")) or []
    d["selected_subevents"] = _coerce_json(d.get("selected_subevents")) or {}
    # signing_secret_enc is NEVER returned in the dict; use get_signing_secret().
    d.pop("signing_secret_enc", None)
    return d


def create_subscription(
    *,
    scope: str,
    owner: str,
    agent: str | None,
    mcp_name: str,
    provider_id: str,
    account_label: str,
    vendor_target: str,
    selected_events: list[str],
    selected_subevents: dict[str, list[str]],
    signing_secret: str,
    created_by: str,
    expires_at: str | None = None,
    subscription_id: str | None = None,
    delivery_mode: str = "vendor",
    target_kind: str = "",
) -> dict:
    """Insert a new subscription row in 'creating' state.

    ``target_kind`` is the manifest's ``vendor_target_spec.target_kinds``
    key the row registers as; ``""`` is the default kind (every row created
    before kinds existed).

    ``signing_secret`` is the plaintext secret; this function Fernet-encrypts
    it before storage. The plaintext is never returned again — call
    ``get_signing_secret(subscription_id)`` to retrieve it for vendor API
    template substitution.

    Empty ``signing_secret`` (``""``) is valid: vendors that share a single
    platform-wide secret (Slack signing secret, MS Graph clientState) store
    the secret in ``infra_credentials`` instead — the row's ``signing_secret_enc``
    is then the encryption of the empty string. ``get_signing_secret`` returns
    ``""`` for such rows so the dispatcher resolves from infra at verify time.
    """
    if scope not in ("user", "service"):
        raise ValueError(f"invalid scope: {scope!r}")
    if scope == "user" and not owner:
        raise ValueError("user-scope subscription requires owner (user_sub)")
    if scope == "service" and not agent:
        raise ValueError("service-scope subscription requires agent slug")

    if delivery_mode not in ("vendor", "relay"):
        raise ValueError(f"invalid delivery_mode: {delivery_mode!r}")

    sid = subscription_id or str(uuid.uuid4())
    now = _now()
    enc = _encrypt(signing_secret or "")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO webhook_subscriptions
               (id, scope, owner, agent, mcp_name, provider_id, account_label,
                vendor_target, selected_events, selected_subevents,
                signing_secret_enc, status, expires_at,
                created_by, created_at, updated_at, delivery_mode, target_kind)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (sid, scope, owner, agent, mcp_name, provider_id, account_label,
             vendor_target,
             json.dumps(list(selected_events or [])),
             json.dumps(dict(selected_subevents or {})),
             enc, CREATING, expires_at, created_by, now, now, delivery_mode, target_kind or ""),
        )
        row = conn.execute(
            "SELECT * FROM webhook_subscriptions WHERE id=%s", (sid,)
        ).fetchone()
        return _row_to_dict(row)  # type: ignore[return-value]


def get_subscription(subscription_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM webhook_subscriptions WHERE id=%s", (subscription_id,)
        ).fetchone()
        return _row_to_dict(row)


def get_signing_secret(subscription_id: str) -> str | None:
    """Decrypt + return the per-subscription signing secret.

    Returns ``""`` if the row stores an empty secret (vendor uses
    infra-credentials-level platform-wide secret). Returns ``None`` if the
    row doesn't exist.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT signing_secret_enc FROM webhook_subscriptions WHERE id=%s",
            (subscription_id,),
        ).fetchone()
        if not row:
            return None
        enc = row["signing_secret_enc"]
        return _decrypt(enc) if enc else ""


def update_signing_secret(subscription_id: str, signing_secret: str) -> None:
    """Set the per-subscription signing secret (Fernet at rest). Used by the
    in-band verification-token capture (Notion-class vendors dictate the
    secret instead of OtoDock minting one)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE webhook_subscriptions SET signing_secret_enc=%s, updated_at=%s "
            "WHERE id=%s",
            (_encrypt(signing_secret or ""), _now(), subscription_id),
        )


def list_subscriptions(
    *,
    scope: str | None = None,
    owner: str | None = None,
    agent: str | None = None,
    mcp_name: str | None = None,
    provider_id: str | None = None,
    status: str | None = None,
    account_label: str | None = None,
    vendor_target: str | None = None,
    delivery_mode: str | None = None,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []
    if scope:
        conditions.append("scope=%s")
        params.append(scope)
    if owner is not None:
        conditions.append("owner=%s")
        params.append(owner)
    if agent is not None:
        conditions.append("agent=%s")
        params.append(agent)
    if mcp_name:
        conditions.append("mcp_name=%s")
        params.append(mcp_name)
    if provider_id:
        conditions.append("provider_id=%s")
        params.append(provider_id)
    if status:
        conditions.append("status=%s")
        params.append(status)
    if account_label is not None:
        conditions.append("account_label=%s")
        params.append(account_label)
    if vendor_target is not None:
        conditions.append("vendor_target=%s")
        params.append(vendor_target)
    if delivery_mode is not None:
        conditions.append("delivery_mode=%s")
        params.append(delivery_mode)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM webhook_subscriptions {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def list_subscriptions_for_user_view(
    *, user_sub: str, agent: str | None = None,
) -> list[dict]:
    """Subscriptions visible to a specific user: own user-scope + all
    service-scope on agents they can access (caller post-filters)."""
    conditions: list[str] = [
        "(scope='service' OR (scope='user' AND owner=%s))"
    ]
    params: list[Any] = [user_sub]
    if agent:
        conditions.append("(agent IS NULL OR agent=%s)")
        params.append(agent)
    where = f"WHERE {' AND '.join(conditions)}"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM webhook_subscriptions {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def update_subscription_status(
    subscription_id: str,
    new_status: str,
    *,
    vendor_subscription_id: str | None = None,
    last_error: str | None = None,
    expires_at: str | None = None,
    clear_last_error: bool = False,
) -> bool:
    """Transition status atomically; reject illegal transitions.

    Optional fields are written when non-None. ``clear_last_error=True``
    explicitly sets ``last_error=NULL`` (used when a renew_failed row
    flips back to active).
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM webhook_subscriptions WHERE id=%s",
            (subscription_id,),
        ).fetchone()
        if not row:
            return False
        current = row["status"]
        if current != new_status and new_status not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(
                f"illegal transition {current!r} → {new_status!r} for subscription {subscription_id}"
            )
        sets = ["status=%s", "updated_at=%s"]
        vals: list[Any] = [new_status, _now()]
        if vendor_subscription_id is not None:
            sets.append("vendor_subscription_id=%s")
            vals.append(vendor_subscription_id)
        if last_error is not None:
            sets.append("last_error=%s")
            vals.append(last_error)
        elif clear_last_error:
            sets.append("last_error=NULL")
        if expires_at is not None:
            sets.append("expires_at=%s")
            vals.append(expires_at)
        vals.append(subscription_id)
        cur = conn.execute(
            f"UPDATE webhook_subscriptions SET {', '.join(sets)} WHERE id=%s",
            vals,
        )
        return cur.rowcount > 0


def note_last_error(subscription_id: str, text: str) -> bool:
    """Record ``text`` as the row's ``last_error`` without touching its
    status: one statement, so a status transition committed meanwhile is
    never reverted. A row still ``creating`` (its first delivery may land
    before the vendor confirms) and a missing id take no note. The receive
    path's signature note goes through here."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE webhook_subscriptions SET last_error=%s, updated_at=%s "
            "WHERE id=%s AND status <> %s",
            (text, _now(), subscription_id, CREATING),
        )
        return cur.rowcount > 0


def record_event_received(subscription_id: str) -> None:
    """Increment event_count + update last_event_at. Best-effort, no return."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """UPDATE webhook_subscriptions SET
                event_count = event_count + 1,
                last_event_at = %s,
                updated_at = %s
               WHERE id = %s""",
            (now, now, subscription_id),
        )


def delete_subscription(subscription_id: str) -> bool:
    """Hard delete. Returns True if the row existed.

    Triggers referencing this subscription have their ``subscription_id``
    set to NULL automatically via FK ``ON DELETE SET NULL``.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM webhook_subscriptions WHERE id=%s", (subscription_id,)
        )
        return cur.rowcount > 0


def list_due_for_renewal(now_iso: str, lead_seconds: int) -> list[dict]:
    """Return active subscriptions whose ``expires_at`` is within lead_seconds.

    Used by the subscription_renewer worker. NULL ``expires_at`` rows are
    skipped (those vendors don't expire).
    """
    with get_conn() as conn:
        # expires_at is TEXT (ISO-8601, "T" separator) while a timestamptz
        # rendered ::text uses a space separator — a string comparison between
        # the two collapses the renewal lead for same-day expiries. Compare as
        # timestamps on both sides.
        rows = conn.execute(
            """SELECT * FROM webhook_subscriptions
               WHERE status = %s
                 AND expires_at IS NOT NULL
                 AND expires_at::timestamptz
                     < (%s::timestamptz + (%s || ' seconds')::interval)
               ORDER BY expires_at ASC""",
            (ACTIVE, now_iso, str(lead_seconds)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cleanup hooks (called by user-delete / agent-delete / account-disconnect)
# ---------------------------------------------------------------------------

def cleanup_user_subscriptions(user_sub: str) -> list[dict]:
    """Return user-scope subscription rows BEFORE delete.

    Caller (subscription_manager.cleanup_user_subscriptions) iterates the
    returned rows, attempts the vendor DELETE for each (best-effort using
    the OAuth token that's still on disk), then calls ``delete_subscription``
    per row. Returning the rows first lets the service layer log + telemeter
    each vendor call.
    """
    return list_subscriptions(scope="user", owner=user_sub)


def cleanup_agent_subscriptions(agent: str) -> list[dict]:
    """Return service-scope subscription rows BEFORE delete for an agent."""
    return list_subscriptions(scope="service", agent=agent)


def cleanup_account_subscriptions(
    *, scope: str, owner: str, mcp_name: str, account_label: str,
    agent: str | None = None,
) -> list[dict]:
    """Return rows tied to a specific OAuth account BEFORE delete.

    For user scope: ``owner`` is the user_sub. For service scope: the rows are
    keyed by the bound ``agent`` + ``account_label`` + ``mcp_name`` — pass
    ``agent`` to target a single agent's bound-account subscriptions (used by
    the account-disconnect cascade); omit it to match every agent on the
    account.
    """
    if scope == "user":
        return list_subscriptions(
            scope="user", owner=owner, mcp_name=mcp_name, account_label=account_label,
        )
    return list_subscriptions(
        scope="service", agent=agent, mcp_name=mcp_name, account_label=account_label,
    )
