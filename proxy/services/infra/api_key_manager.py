"""API key generation, hashing, and verification.

Key format: ``otok_<base64-32>`` where the prefix part (first 12 chars after
``otok_``) is stored plaintext for UI display + DB index. The full key is
bcrypt-hashed at rest. Plaintext is returned ONCE on creation.

Used by the webhook trigger fire endpoint (Bearer auth) and by the admin
CRUD endpoints. Master ``PROXY_API_KEY`` is REJECTED here — it never
authenticates webhook fires (security boundary between internal ops and
external webhooks).

This module reads the storage layer (``storage.identity.api_key_store``) and adds:

  - cryptographically random key generation
  - bcrypt password hashing
  - prefix-then-hash lookup pattern
  - scope+owner cross-check on verification
  - ``last_used_at`` async update (best-effort, doesn't block fire path)
"""

import asyncio
import logging
import secrets

import bcrypt

import config
from storage.identity import api_key_store
from storage.automation import notification_store

logger = logging.getLogger("claude-proxy.api-keys")


# Public prefix on raw keys — makes them grep-able in code dumps and
# scannable by secret detectors (think `git secrets`, GitGuardian).
KEY_PUBLIC_PREFIX = "otok_"

# How many chars after "otok_" we store + index for display. 12 chars of
# base64 = ~72 bits of entropy = effectively zero collision probability.
KEY_INDEX_PREFIX_LEN = 12


# All permission scopes that can appear on a user_api_keys row. v1 only
# wires up `triggers`; the rest are placeholders for future input layers.
ALL_USER_PERMISSIONS = ["triggers", "chat", "tasks", "notifications"]
ENABLED_USER_PERMISSIONS_V1 = {"triggers"}


# Permissions valid for agent_api_keys — same v1 scope.
ALL_AGENT_PERMISSIONS = ["triggers"]
ENABLED_AGENT_PERMISSIONS_V1 = {"triggers"}


# =====================================================================
# Key generation
# =====================================================================


def _generate_raw_key() -> tuple[str, str]:
    """Mint a new ``otok_<random32>`` key.

    Returns ``(raw_key, prefix)``. The raw key is shown to the user once and
    never stored. The prefix (12 chars after ``otok_``) is stored plaintext
    in the DB for indexed lookup + UI display.
    """
    # 32 bytes of entropy → 43 base64 chars (urlsafe, no padding).
    body = secrets.token_urlsafe(32)
    raw = f"{KEY_PUBLIC_PREFIX}{body}"
    prefix = body[:KEY_INDEX_PREFIX_LEN]
    return raw, prefix


def _hash_key(raw_key: str) -> str:
    """bcrypt-hash a raw key. Cost factor 12 is plenty for this use case."""
    return bcrypt.hashpw(raw_key.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def _check_key(raw_key: str, hashed: str) -> bool:
    """Constant-time bcrypt compare. False on any error (bad hash format etc.)."""
    try:
        return bcrypt.checkpw(raw_key.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# =====================================================================
# Permission validation
# =====================================================================


def validate_user_permissions(perms: list[str]) -> list[str]:
    """Validate + canonicalise user-key permissions.

    Drops unknown values, deduplicates. Empty list is valid (key is useless
    until at least one permission is added). Raises ValueError if any
    requested permission isn't enabled in v1.
    """
    seen = set()
    out: list[str] = []
    for p in perms:
        if not isinstance(p, str):
            continue
        p = p.strip()
        if not p or p in seen:
            continue
        if p not in ALL_USER_PERMISSIONS:
            raise ValueError(f"Unknown user permission: {p!r}")
        if p not in ENABLED_USER_PERMISSIONS_V1:
            raise ValueError(
                f"Permission {p!r} not enabled yet — only "
                f"{sorted(ENABLED_USER_PERMISSIONS_V1)} are supported in v1"
            )
        seen.add(p)
        out.append(p)
    return out


def validate_agent_permissions(perms: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for p in perms:
        if not isinstance(p, str):
            continue
        p = p.strip()
        if not p or p in seen:
            continue
        if p not in ALL_AGENT_PERMISSIONS:
            raise ValueError(f"Unknown agent permission: {p!r}")
        if p not in ENABLED_AGENT_PERMISSIONS_V1:
            raise ValueError(
                f"Permission {p!r} not enabled yet — only "
                f"{sorted(ENABLED_AGENT_PERMISSIONS_V1)} are supported in v1"
            )
        seen.add(p)
        out.append(p)
    return out


# =====================================================================
# Creation
# =====================================================================


def create_agent_key(
    *, agent: str, name: str, permissions: list[str], created_by: str,
) -> tuple[dict, str]:
    """Mint a new agent API key. Returns ``(row_dict, raw_key)``.

    The raw_key MUST be shown to the user once and never persisted. The row
    contains the prefix (for display) + hashed key.
    """
    perms = validate_agent_permissions(permissions or ["triggers"])
    if not perms:
        raise ValueError("At least one permission required")
    if not name or not name.strip():
        raise ValueError("Key name required")
    raw_key, prefix = _generate_raw_key()
    key_hash = _hash_key(raw_key)
    row = api_key_store.create_agent_api_key(
        agent=agent, name=name.strip(),
        key_hash=key_hash, prefix=prefix,
        permissions=perms, created_by=created_by,
    )
    logger.info(f"Minted agent_api_key id={row['id'][:8]} agent={agent} by={created_by[:8]}")
    return row, raw_key


def create_user_key(
    *, user_sub: str, name: str, permissions: list[str],
) -> tuple[dict, str]:
    perms = validate_user_permissions(permissions or ["triggers"])
    if not perms:
        raise ValueError("At least one permission required")
    if not name or not name.strip():
        raise ValueError("Key name required")
    raw_key, prefix = _generate_raw_key()
    key_hash = _hash_key(raw_key)
    row = api_key_store.create_user_api_key(
        user_sub=user_sub, name=name.strip(),
        key_hash=key_hash, prefix=prefix,
        permissions=perms,
    )
    logger.info(f"Minted user_api_key id={row['id'][:8]} user={user_sub[:8]}")
    return row, raw_key


# =====================================================================
# Verification
# =====================================================================


class KeyMismatch(Exception):
    """Raised when bearer auth fails verification.

    ``code`` is one of:
      - ``"format"``    — token doesn't start with otok_ / wrong length
      - ``"master"``    — token is the master PROXY_API_KEY (rejected here)
      - ``"unknown"``   — no matching key by prefix
      - ``"hash"``      — prefix matched but bcrypt mismatch (or revoked)
      - ``"scope"``     — key found but scope/owner doesn't match the URL
      - ``"permission"``— key valid but lacks the required permission scope
    """

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def _strip_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def verify_bearer_for_agent(
    authorization: str | None,
    *,
    agent: str,
    required_permission: str = "triggers",
) -> dict:
    """Verify a Bearer token for an agent-scoped webhook URL.

    Raises KeyMismatch on any failure. Returns the matching agent_api_keys
    row on success.

    Master PROXY_API_KEY is explicitly rejected here — webhook surface is
    least-privilege only.
    """
    token = _strip_bearer(authorization)
    if not token:
        raise KeyMismatch("format", "Bearer token required")
    if config.is_master_key(token):
        raise KeyMismatch("master", "Master key not accepted on webhook endpoints")
    if not token.startswith(KEY_PUBLIC_PREFIX):
        raise KeyMismatch("format", f"Key must start with {KEY_PUBLIC_PREFIX}")
    body = token[len(KEY_PUBLIC_PREFIX):]
    if len(body) < KEY_INDEX_PREFIX_LEN:
        raise KeyMismatch("format", "Key too short")
    prefix = body[:KEY_INDEX_PREFIX_LEN]

    candidates = api_key_store.get_agent_keys_by_prefix(prefix)
    matched: dict | None = None
    for row in candidates:
        if _check_key(token, row["key_hash"]):
            matched = row
            break
    if matched is None:
        raise KeyMismatch("unknown", "Invalid key")

    # Scope + owner cross-check: agent key must be for this exact agent.
    if matched.get("agent") != agent:
        raise KeyMismatch("scope", "Key does not authorize this agent")

    if not api_key_store.has_permission(matched, required_permission):
        raise KeyMismatch(
            "permission",
            f"Key lacks {required_permission!r} permission",
        )

    # last_used_at update is best-effort; never block the fire path on it.
    _schedule_last_used_update("agent", matched["id"])
    return matched


def verify_bearer_for_user(
    authorization: str | None,
    *,
    username: str,
    required_permission: str = "triggers",
) -> dict:
    """Verify a Bearer token for a user-scoped webhook URL.

    The URL contains a username; we resolve it to the user_sub and require
    the key's user_sub matches.
    """
    token = _strip_bearer(authorization)
    if not token:
        raise KeyMismatch("format", "Bearer token required")
    if config.is_master_key(token):
        raise KeyMismatch("master", "Master key not accepted on webhook endpoints")
    if not token.startswith(KEY_PUBLIC_PREFIX):
        raise KeyMismatch("format", f"Key must start with {KEY_PUBLIC_PREFIX}")
    body = token[len(KEY_PUBLIC_PREFIX):]
    if len(body) < KEY_INDEX_PREFIX_LEN:
        raise KeyMismatch("format", "Key too short")
    prefix = body[:KEY_INDEX_PREFIX_LEN]

    target_sub = notification_store.resolve_username_to_sub(username)
    if not target_sub:
        # User not found → reject with scope mismatch (don't leak user existence).
        raise KeyMismatch("scope", "Key does not authorize this user")

    candidates = api_key_store.get_user_keys_by_prefix(prefix)
    matched: dict | None = None
    for row in candidates:
        if _check_key(token, row["key_hash"]):
            matched = row
            break
    if matched is None:
        raise KeyMismatch("unknown", "Invalid key")

    if matched.get("user_sub") != target_sub:
        raise KeyMismatch("scope", "Key does not authorize this user")

    if not api_key_store.has_permission(matched, required_permission):
        raise KeyMismatch(
            "permission",
            f"Key lacks {required_permission!r} permission",
        )

    _schedule_last_used_update("user", matched["id"])
    return matched


# =====================================================================
# Async last_used_at update (fire-and-forget)
# =====================================================================


def _schedule_last_used_update(kind: str, key_id: str) -> None:
    """Update last_used_at without blocking the request.

    If we're inside an asyncio loop, use asyncio.to_thread; otherwise just
    do it inline (test contexts).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        if kind == "agent":
            api_key_store.update_agent_key_last_used(key_id)
        else:
            api_key_store.update_user_key_last_used(key_id)
        return

    async def _do():
        try:
            if kind == "agent":
                await asyncio.to_thread(api_key_store.update_agent_key_last_used, key_id)
            else:
                await asyncio.to_thread(api_key_store.update_user_key_last_used, key_id)
        except Exception as e:
            logger.warning(f"Failed to update last_used_at for {kind} key {key_id[:8]}: {e}")

    loop.create_task(_do())


# =====================================================================
# Revocation
# =====================================================================


def revoke_agent_key(key_id: str) -> bool:
    return api_key_store.revoke_agent_api_key(key_id)


def revoke_user_key(key_id: str) -> bool:
    return api_key_store.revoke_user_api_key(key_id)
