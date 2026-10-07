"""The sign-in denylist: logout is this device only.

A dashboard session cookie is a JWT that lives to its expiry (and the
sliding refresh re-mints it while used), so deleting the cookie alone left
a copy valid. Every cookie carries the id of its SIGN-IN (``jti``, kept by
every re-mint of that sign-in); a logout revokes that id, and every reader
of the cookie (``auth.providers.validate_session_jwt``) refuses it from then
on, while the person's other devices stay signed in. Revoked ids are kept
in memory for the cookie's remaining life and in ``revoked_session_ids`` so
they survive a restart. A cookie minted before cookies carried ``jti``
takes a legacy id derived from its subject and issue time: every re-mint of
that sign-in shares it too.

The per-person epoch (``users.token_epoch_at``, ``auth.providers``) is the
other half: a password change or an admin's "sign out everywhere" ends
every sign-in and every agent session token of the person at once.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("claude-proxy.auth")

_revoked: dict[str, float] = {}    # sign-in id -> expiry (epoch seconds)
_lock = threading.Lock()           # read on the loop and on executor threads
_next_sweep = 0.0
_SWEEP_EVERY_S = 3600.0
# A revoked id is kept at least this long: a copy of the sign-in re-minted
# under a longer cookie lifetime than today's (an admin shortened it) must
# not outlive its revocation.
MIN_RETENTION_S = 30 * 86400.0


def session_cookie_id(payload: dict) -> str:
    """The sign-in id a cookie carries, or the legacy id of a cookie minted
    before cookies carried one (its subject and issue time: the re-mints of
    one sign-in share it, and so do two sign-ins of one person in the same
    second, the safe direction)."""
    jti = payload.get("jti")
    if isinstance(jti, str) and jti:
        return jti
    return f"legacy:{payload.get('sub') or ''}@{payload.get('iat') or 0}"


def _sweep(now: float) -> None:
    global _next_sweep
    if now < _next_sweep:
        return
    _next_sweep = now + _SWEEP_EVERY_S
    for cookie_id in [c for c, until in _revoked.items() if until < now]:
        _revoked.pop(cookie_id, None)


def is_revoked(cookie_id: str) -> bool:
    now = time.time()
    with _lock:
        _sweep(now)
        until = _revoked.get(cookie_id)
        return until is not None and until >= now


def revoke(payload: dict, *, lifetime_s: float) -> str:
    """Revoke the sign-in ``payload`` belongs to, in memory at once; the
    caller persists the row (``persist``) off the loop. ``lifetime_s`` is
    the cookie lifetime: a re-mint of the same sign-in may outlive the
    presented cookie's ``exp`` by up to that much (``MIN_RETENTION_S`` at
    least, for a copy minted under a longer lifetime). Returns the id."""
    cookie_id = session_cookie_id(payload)
    now = time.time()
    exp = payload.get("exp")
    expires_at = max(float(exp) if isinstance(exp, (int, float)) else now,
                     now + max(lifetime_s, MIN_RETENTION_S))
    with _lock:
        _revoked[cookie_id] = expires_at
    return cookie_id


def persist(cookie_id: str, user_sub: str) -> None:
    """Write the revoked id's row (synchronous: call it on the DB executor).
    Best-effort: a logout never fails on it; a lost row means the id is
    forgotten at the next restart, so it is logged as an error."""
    with _lock:
        expires_at = _revoked.get(cookie_id)
    if expires_at is None:
        return
    try:
        from storage.identity import db_session_revocations
        db_session_revocations.add(cookie_id, user_sub, expires_at)
    except Exception:
        logger.exception("revoked sign-in %s not persisted; it is forgotten at the next restart",
                         cookie_id[:8])


def load() -> None:
    """Reload the denylist at boot, after the schema is in place (synchronous:
    call it on the DB executor). Expired rows are dropped."""
    from storage.identity import db_session_revocations
    now = time.time()
    rows = db_session_revocations.load_live(now)
    with _lock:
        for cookie_id, expires_at in rows:
            _revoked[cookie_id] = expires_at
    if rows:
        logger.info("Reloaded %d revoked sign-in(s)", len(rows))


def reset_for_tests() -> None:
    global _next_sweep
    with _lock:
        _revoked.clear()
        _next_sweep = 0.0
