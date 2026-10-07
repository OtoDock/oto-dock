"""OAuth token refresh worker — background asyncio task that proactively
refreshes tokens whose access lifetime is about to expire.

Without this, an agent that hasn't run for an hour pays a refresh
round-trip on its next tool call (lazy refresh inside ``provider.refresh``).
The worker keeps every connected account's token fresh in the background
so the user-perceived first-call latency stays low.

Lifecycle:
  * Created from the ``startup.py`` lifespan AFTER
    ``mcp_registry.scan_manifests()``.
  * Sleeps for ``_INTERVAL_SECONDS`` (default 60s).
  * Cancelled BEFORE ``_shutdown_sessions`` so it doesn't fight writeback
    for per-account locks during proxy shutdown; a refresh in flight is
    waited out (bounded) so a shutdown never lands between a vendor's
    rotation of the refresh token and its write.

Off the loop: the directory walk and every file read run in one worker
thread per tick (``_scan_token_files``), the ``users`` lookup and the store
reads ride ``run_db``, the re-read and the write-back of one account run in
threads, and only the vendor call is awaited on the loop. At most
``_CONCURRENCY`` accounts refresh at once so one slow vendor does not let
the others expire inside the tick.

Per-account safety:
  * Holds ``credential_locks.get_lock(user_sub, provider_id, account_label)``
    across the re-read, the vendor call and the write-back. The connect and
    the disconnect routes take the same lock around their own file work, so
    a refresh never recreates a file a disconnect deleted and never
    overwrites a fresh connect.
  * The re-read inside the lock is the source of every field the refresh
    uses; the scan's copy only picked the candidate.
  * Refresh always re-persists BOTH access AND refresh tokens — some
    vendors rotate the refresh token on every refresh; preserving the
    previous one only when the response omits it (handled by each
    provider's ``refresh()``) avoids silent token loss. A write that fails
    after the vendor rotated is kept in memory (``_pending_writes``) and
    retried at the next tick before any vendor call.

Lock scope: the lock key is **provider-scoped**, not MCP-scoped. Multiple
MCPs of the same provider share the OAuth grant + token file + lock, so
concurrent refresh attempts across MCPs of the same provider serialize
correctly.

Discovery: walks ``sessions/*-tokens/`` for any provider's token files. The
directory name IS the provider_id (``google-tokens`` → ``google``).

Failure mode:
  * A failed refresh leaves ``expires_at`` in the past, so without damping the
    file would be retried on EVERY tick — 1,440 vendor calls/day for a token
    that can never succeed. Failures therefore back off exponentially
    (``_BACKOFF_BASE_SECONDS`` doubling to ``_BACKOFF_CAP_SECONDS``), and an
    identified permanent error (``invalid_grant``: the grant was revoked at
    the vendor; ``invalid_client`` on a registered client) is written into
    the file as ``extra.refresh_failed`` so the card says reconnect and the
    resolver leaves the MCP out, and never retried until the file changes (a
    reconnect rewrites it). Backoff state is in-memory only: a proxy restart
    costs at most one extra attempt per failing token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("claude-proxy.oauth-refresh-worker")

# How often the loop wakes up.
_INTERVAL_SECONDS = 60
# Refresh threshold: tokens with less than this lifetime get refreshed.
# Per-provider manifests can RAISE it via credentials.oauth.refresh
# .min_remaining_seconds (see _refresh_threshold_for) — facebook's ~60-day
# tokens must re-exchange while still valid, and a 5-minute window at day 60
# dies on any proxy downtime.
_REFRESH_THRESHOLD_SECONDS = 300
# Failure backoff: first retry this long after a failure, doubling per
# consecutive failure up to the cap (a permanently-broken token then costs
# ≤24 attempts/day instead of 1,440).
_BACKOFF_BASE_SECONDS = 120
_BACKOFF_CAP_SECONDS = 3600
# Accounts refreshed at once within a tick.
_CONCURRENCY = 4
# How long a shutdown waits for refreshes in flight.
_SHUTDOWN_GRACE_SECONDS = 30

# Per-token-file failure state: str(path) → {failures, next_attempt, mtime,
# dead}. ``dead`` marks a permanent failure — never retried until the file's
# mtime changes. Cleared on success / file rewrite / file removal.
_failure_state: dict[str, dict] = {}

# Seam for tests (patch this, not the global clock).
_monotonic = time.monotonic

# Module-level handle to the running task so startup.py can cancel it cleanly.
_worker_task: asyncio.Task | None = None
# Refreshes in flight (shielded): a cancel of the loop never lands between a
# vendor's rotation and its write; stop_worker waits for them.
_inflight: set[asyncio.Task] = set()


@dataclass
class _PendingWrite:
    """A refreshed token set whose write failed: written at the next tick
    before any vendor call, as long as the file is still the one refreshed
    (same mtime)."""
    mtime: float
    payload: dict


_pending_writes: dict[str, _PendingWrite] = {}


@dataclass
class _Candidate:
    path: Path
    username: str
    provider_id: str
    raw: dict
    mtime: float
    remaining: float | None


@dataclass
class _TickState:
    """What one tick shares across its candidates: the resources already
    re-discovered (at most once per tick)."""
    rediscovered: set[str] = field(default_factory=set)


class _WriteAborted(Exception):
    """The file vanished or moved between the re-read and the write."""


def start_worker() -> asyncio.Task:
    """Spawn the background refresh task. Idempotent."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(_refresh_loop(), name="oauth-refresh-worker")
    logger.info("OAuth refresh worker started (interval=%ds, threshold=%ds)",
                _INTERVAL_SECONDS, _REFRESH_THRESHOLD_SECONDS)
    return _worker_task


async def stop_worker() -> None:
    """Cancel + await the worker, then wait (bounded) for refreshes in
    flight. Idempotent; safe during shutdown."""
    global _worker_task
    if not _worker_task:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _worker_task
    _worker_task = None
    if _inflight:
        with contextlib.suppress(Exception):
            await asyncio.wait(list(_inflight), timeout=_SHUTDOWN_GRACE_SECONDS)
    logger.info("OAuth refresh worker stopped")


async def _refresh_loop() -> None:
    """Main loop: scan every token file, refresh those near expiry."""
    while True:
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
            await _refresh_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OAuth refresh tick failed (continuing)")


# ---------------------------------------------------------------------------
# The scan (one thread per tick)
# ---------------------------------------------------------------------------

def _parse_remaining(raw: dict) -> float | None:
    """Seconds until the token expires, or None when the file never
    expires or its ``expires_at`` cannot be read."""
    expiry_str = raw.get("expires_at") or ""
    if not expiry_str:
        return None
    try:
        expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (expiry_dt - datetime.now(timezone.utc)).total_seconds()


def _scan_token_files(base: Path, known: list[str]) -> tuple[list[_Candidate], list[str]]:
    """Walk ``sessions/*-tokens/{username}/{account_label}.json`` and read
    every file; return the files with their expiry and mtime, and the
    ``known`` failure keys whose file is gone. Runs in a worker thread."""
    from services.oauth import oauth_account_store

    found: list[_Candidate] = []
    if base.is_dir():
        for prov_dir in base.glob("*-tokens"):
            if not prov_dir.is_dir():
                continue
            provider_id = prov_dir.name.removesuffix("-tokens")
            for user_dir in prov_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                for token_file in user_dir.glob("*.json"):
                    raw = oauth_account_store._read_oauth_token(token_file)
                    if raw is None:
                        continue
                    try:
                        mtime = token_file.stat().st_mtime
                    except OSError:
                        continue
                    found.append(_Candidate(
                        path=token_file, username=user_dir.name, provider_id=provider_id,
                        raw=raw, mtime=mtime, remaining=_parse_remaining(raw),
                    ))
    vanished = [k for k in known if not Path(k).exists()]
    return found, vanished


async def _refresh_tick() -> None:
    """One pass: scan every provider's token dir off the loop and refresh
    the near-expiry tokens, a few at a time."""
    found, vanished = await asyncio.to_thread(
        _scan_token_files, config.SESSIONS_DIR, list(_failure_state),
    )
    for key in vanished:
        _failure_state.pop(key, None)
        _pending_writes.pop(key, None)

    candidates = [
        c for c in found
        if str(c.path) in _pending_writes
        or (c.remaining is not None and c.remaining <= _refresh_threshold_for(c.provider_id))
    ]
    if not candidates:
        return
    state = _TickState()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(c: _Candidate) -> bool:
        async with sem:
            try:
                return await _refresh_candidate(c, state)
            except Exception as exc:
                _record_refresh_failure(c.path, exc, c.mtime)
                logger.exception(
                    "OAuth refresh failed for %s/%s", c.path.parent.parent.name, c.path.name,
                )
                return False

    results = await asyncio.gather(*(_one(c) for c in candidates))
    refreshed = sum(1 for r in results if r)
    if refreshed:
        logger.info("OAuth refresh worker refreshed %d token(s)", refreshed)


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

def _in_backoff(token_file: Path, mtime: float) -> bool:
    """True if ``token_file`` recently failed and its retry window hasn't
    elapsed (or it's marked dead). A changed mtime — reconnect or writeback by
    another path — clears the state: the old verdict no longer applies."""
    state = _failure_state.get(str(token_file))
    if state is None:
        return False
    if mtime != state["mtime"]:
        _failure_state.pop(str(token_file), None)
        return False
    return state["dead"] or _monotonic() < state["next_attempt"]


def _refresh_threshold_for(provider_id: str) -> int:
    """Per-provider refresh threshold: the LARGEST
    ``credentials.oauth.refresh.min_remaining_seconds`` any manifest declares
    for this provider, floored at the global default. This is the one place
    that knob is read — declared-but-ignored was fine for hourly-expiry
    vendors, but token-as-refresh providers (facebook) need a window of days,
    not minutes."""
    best = _REFRESH_THRESHOLD_SECONDS
    try:
        from services.mcp import mcp_registry

        for m in mcp_registry.get_mcps_by_provider(provider_id) or []:
            oauth = (m.credentials.oauth or {}) if m.credentials else {}
            try:
                v = int((oauth.get("refresh") or {}).get("min_remaining_seconds") or 0)
            except (TypeError, ValueError):
                continue
            best = max(best, v)
    except Exception:
        return _REFRESH_THRESHOLD_SECONDS
    return best


def _permanent_error_code(exc: Exception, arm: str) -> str:
    """The vendor's error code when the failure can never succeed on a
    retry, else ``""``: ``invalid_grant`` means the grant was revoked or
    expired at the vendor (a ``RelayError`` code on the relay arm, the typed
    ``OAuthTokenError`` code or the vendor's error string in a
    ``RuntimeError`` on the direct arms); ``invalid_client`` on the
    registered-client arm means the client the token was issued to is gone
    there. The error names produced by the worker itself (``_FILE_ERRORS``)
    are permanent too."""
    from auth.oauth_providers.base import OAuthTokenError
    from services.billing.relay_client import RelayError

    if isinstance(exc, RelayError):
        return "invalid_grant" if exc.code == "invalid_grant" else ""
    if isinstance(exc, _FileVerdict):
        return exc.code
    if isinstance(exc, OAuthTokenError):
        if exc.code == "invalid_grant":
            return exc.code
        if arm == "mcp_authorization" and exc.code == "invalid_client":
            return exc.code
        return ""
    if isinstance(exc, RuntimeError) and "invalid_grant" in str(exc):
        return "invalid_grant"
    return ""


class _FileVerdict(RuntimeError):
    """A permanent verdict the worker reaches on its own (no vendor call):
    the registration a confidential file needs is gone, the metadata moved to
    another issuer, the manifest's way of issuing tokens changed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _record_refresh_failure(token_file: Path, exc: Exception, mtime: float, *, dead: bool = False) -> None:
    key = str(token_file)
    prev = _failure_state.get(key)
    failures = (prev["failures"] if prev else 0) + 1
    delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (failures - 1))
    _failure_state[key] = {
        "failures": failures,
        "next_attempt": _monotonic() + delay,
        "mtime": mtime,
        "dead": dead,
    }
    if dead:
        logger.warning(
            "OAuth refresh giving up on %s (%s) — will retry only after the "
            "account is reconnected", token_file.name, exc,
        )


# ---------------------------------------------------------------------------
# One account
# ---------------------------------------------------------------------------

def _user_sub_for(username: str) -> str:
    """The sub of a username (the token dir is keyed by username)."""
    from storage.pg import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sub FROM users WHERE username = %s", (username,),
        ).fetchone()
    return row["sub"] if row else ""


def _read_with_mtime(token_file: Path) -> tuple[dict | None, float]:
    from services.oauth import oauth_account_store
    raw = oauth_account_store._read_oauth_token(token_file)
    if raw is None:
        return None, 0.0
    try:
        return raw, token_file.stat().st_mtime
    except OSError:
        return None, 0.0


def _write_back(token_file: Path, expected_mtime: float, payload: dict) -> float:
    """Write the file atomically through a unique temporary name, only
    while the file is still the one re-read (same mtime); returns the new
    mtime. Runs in a worker thread."""
    try:
        current = token_file.stat().st_mtime
    except OSError:
        raise _WriteAborted("vanished")
    if current != expected_mtime:
        raise _WriteAborted("moved")
    fd, tmp_name = tempfile.mkstemp(prefix=token_file.name + ".", suffix=".partial",
                                    dir=str(token_file.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, token_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return token_file.stat().st_mtime


def _rendered(
    *, provider_id: str, raw: dict, access_token: str, refresh_token: str, expires_in: int,
    client_id: str, client_secret: str, token_url: str, extra: dict, aliases: dict | None,
) -> dict:
    """The file payload the account store's writer would produce."""
    from services.oauth import oauth_account_store
    return oauth_account_store.render_generic_oauth_v1_token(
        provider_id=provider_id, account_id=raw.get("account_id", ""),
        access_token=access_token, refresh_token=refresh_token, expires_in=expires_in,
        scopes=raw.get("scopes", []), client_id=client_id, client_secret=client_secret,
        token_url=token_url, extra=extra, aliases=aliases,
    )


async def _mark_dead(token_file: Path, raw: dict, mtime: float, code: str, exc: Exception) -> None:
    """Record a permanent failure in the file (the card and the resolver
    read ``extra.refresh_failed``) and in the backoff state, keyed on the
    mtime after the write so the verdict holds until a reconnect."""
    payload = dict(raw)
    extra = dict(payload.get("extra") or {})
    extra["refresh_failed"] = code
    extra["refresh_failed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload["extra"] = extra
    try:
        new_mtime = await asyncio.to_thread(_write_back, token_file, mtime, payload)
    except (_WriteAborted, OSError):
        new_mtime = mtime
    _record_refresh_failure(token_file, exc, new_mtime, dead=True)


async def _maybe_refresh_token_file(
    *, token_file: Path, username: str, provider_id: str,
) -> bool:
    """Refresh the token in ``token_file`` IF it's near expiry (or a write is
    pending for it). Returns True if a refresh was performed. The entry point
    of one account, used by the tick and by tests."""
    raw, mtime = await asyncio.to_thread(_read_with_mtime, token_file)
    if raw is None:
        return False
    cand = _Candidate(path=token_file, username=username, provider_id=provider_id,
                      raw=raw, mtime=mtime, remaining=_parse_remaining(raw))
    if str(token_file) not in _pending_writes:
        if cand.remaining is None or cand.remaining > _refresh_threshold_for(provider_id):
            return False
    return await _refresh_candidate(cand, _TickState())


async def _refresh_candidate(cand: _Candidate, state: _TickState) -> bool:
    from core.credentials import credential_locks
    from storage.pg import run_db

    key = str(cand.path)
    pending = _pending_writes.get(key)
    if pending is None and _in_backoff(cand.path, cand.mtime):
        return False
    user_sub = await run_db(_user_sub_for, cand.username) if cand.username else ""

    async def _under_lock() -> bool:
        # Lock key: provider-scoped (multiple MCPs sharing this provider
        # share the OAuth grant + token file). The lock is taken inside the
        # shielded task, so a cancelled tick never releases it while the
        # refresh still runs.
        async with credential_locks.get_lock(
            user_sub or "_service", cand.provider_id, cand.path.stem,
        ):
            return await _refresh_locked(cand, user_sub, state)

    task = asyncio.ensure_future(_under_lock())
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return await asyncio.shield(task)


async def _refresh_locked(cand: _Candidate, user_sub: str, state: _TickState) -> bool:
    """Under the account lock: the re-read is the source of every field, a
    pending write is retried first, the arms dispatch, the write-back."""
    from services.billing import relay_client
    from services.mcp import mcp_registry
    from services.oauth import mcp_authorization, oauth_account_store
    from auth.oauth_providers import get_provider
    from storage.pg import run_db

    token_file, provider_id = cand.path, cand.provider_id
    key = str(token_file)
    raw2, mtime2 = await asyncio.to_thread(_read_with_mtime, token_file)
    if raw2 is None:
        _pending_writes.pop(key, None)
        return False

    pending = _pending_writes.get(key)
    if pending is not None:
        if pending.mtime == mtime2:
            try:
                await asyncio.to_thread(_write_back, token_file, mtime2, pending.payload)
            except (_WriteAborted, OSError) as exc:
                logger.error("Pending token write for %s failed again: %s", token_file.name, exc)
                return False
            _pending_writes.pop(key, None)
            _failure_state.pop(key, None)
            logger.info("Wrote the pending refreshed token for %s", token_file.name)
            return True
        _pending_writes.pop(key, None)

    if oauth_account_store.token_dead_reason(raw2):
        return False
    remaining = _parse_remaining(raw2)
    if remaining is None or remaining > _refresh_threshold_for(provider_id):
        return False
    if _in_backoff(token_file, mtime2):
        return False

    # Five-arm dispatch:
    #   * HOSTED (extra.via_relay) → relay refreshes server-side with OtoDock's
    #     secret. No local client_secret exists. Checked FIRST.
    #   * PAT (extra.flow == "personal_access_token") → never refresh (zero-expiry).
    #   * S2S (extra.flow == "client_credentials") → re-exchange via
    #     provider.exchange_client_credentials. Zoom S2S tokens last
    #     1 hour and have NO refresh_token — they must be re-minted.
    #   * REGISTERED CLIENT (extra.flow == "mcp_authorization") → the MCP
    #     server's own authorization server, with the resource indicator.
    #   * Standard OAuth → provider.refresh with the stored refresh_token.
    file_extra = raw2.get("extra", {}) or {}
    flow_marker = file_extra.get("flow", "")
    is_relay = bool(file_extra.get("via_relay"))
    if is_relay and not await run_db(relay_client.is_available):
        # Hosted token but the relay isn't reachable yet — can't refresh
        # without OtoDock's secret. Skip quietly; retry once the relay is live.
        return False
    if flow_marker == "personal_access_token":
        return False
    is_s2s = flow_marker == "client_credentials"
    is_registered = flow_marker == oauth_account_store.MCP_AUTHORIZATION_FLOW
    refresh_token = raw2.get("refresh_token") or ""
    if not is_s2s and not refresh_token:
        # Nothing to refresh with (vendor never returned a refresh_token, or
        # it was lost). Skip — the card says reconnect once it expires.
        return False
    client_id = raw2.get("client_id", "")
    client_secret = raw2.get("client_secret", "")
    if not is_relay and not is_registered and (not client_id or not client_secret):
        return False

    manifests = mcp_registry.get_mcps_by_provider(provider_id)
    arm = "mcp_authorization" if is_registered else "relay" if is_relay else "s2s" if is_s2s else "standard"
    token_url = raw2.get("token_url") or ""
    registered_secret = ""
    try:
        if not is_registered and manifests and (
            manifests[0].credentials.oauth or {}
        ).get("authorization_server"):
            # A file the manifest's way of issuing tokens no longer covers
            # (an older app or relay token on a registered-client manifest).
            problem = await run_db(mcp_authorization.token_origin_problem, raw2, manifests[0])
            if problem:
                raise _FileVerdict(problem)
        provider = get_provider(provider_id)
        if is_relay:
            # HOSTED: the relay refreshes with OtoDock's secret and returns the
            # user's new tokens (its TokenSet.raw keeps {"via_relay": True}, so
            # the marker survives the writeback below).
            new_ts = await relay_client.oauth_refresh(
                provider_id=provider_id, refresh_token=refresh_token,
            )
            # Re-run the provider's normalizer over the vendor's verbatim
            # response (mirrors do_oauth_exchange) so provider-specific
            # flattening lands in extra. The relay envelope keeps the old
            # refresh_token when the vendor omits one — raw doesn't, so
            # carry it across the rebuild.
            if new_ts.raw:
                _kept_refresh = new_ts.refresh_token
                new_ts = provider.normalize_token_response(new_ts.raw)
                if not new_ts.refresh_token:
                    new_ts.refresh_token = _kept_refresh
        elif is_s2s:
            # S2S re-exchange. Pass account_id from file extra (vendor
            # response doesn't echo it — caller persisted it at first
            # exchange; we pass it back so the re-exchange call targets
            # the same Zoom account).
            new_ts = await provider.exchange_client_credentials(
                client_id=client_id,
                client_secret=client_secret,
                scopes=[],
                extra={"account_id": file_extra.get("account_id", "")} or None,
            )
        elif is_registered:
            block = ((manifests[0].credentials.oauth or {}).get("authorization_server") or {}) if manifests else {}
            new_ts, token_url, registered_secret = await _refresh_registered(
                raw2, provider_id=provider_id, refresh_token=refresh_token, state=state,
                issuer_override=str(block.get("issuer") or ""),
            )
        else:
            new_ts = await provider.refresh(
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
        token_url = token_url or provider.token_url
    except Exception as exc:
        code = _permanent_error_code(exc, arm)
        if code:
            await _mark_dead(token_file, raw2, mtime2, code, exc)
            return False
        raise

    # Aliases come from any MCP using this provider — all MCPs
    # sharing a provider share the alias declaration (provider-level
    # concern, not per-MCP).
    aliases = None
    if manifests:
        tf = (manifests[0].credentials.oauth or {}).get("token_format", {}) or {}
        aliases = tf.get("aliases") or None
    # Preserve previously-captured vendor metadata (team_id,
    # tenant_id, account_id, flow, preferred_bearer, …). Merge in
    # any new fields from the refresh / re-exchange response.
    extra = dict(file_extra)
    if new_ts.raw:
        for k, v in new_ts.raw.items():
            if k not in (
                "access_token", "refresh_token", "id_token", "expires_in",
                "scope", "token_type",
            ):
                extra[k] = v
    # For S2S, the re-exchange response doesn't carry `flow` or
    # `account_id` — guarantee they survive by re-asserting.
    if is_s2s:
        extra["flow"] = "client_credentials"
        if file_extra.get("account_id"):
            extra["account_id"] = file_extra["account_id"]
    extra.pop("refresh_failed", None)
    extra.pop("refresh_failed_at", None)

    payload = _rendered(
        provider_id=provider_id, raw=raw2,
        access_token=new_ts.access_token, refresh_token=new_ts.refresh_token,
        expires_in=new_ts.expires_in, client_id=client_id, client_secret=client_secret,
        token_url=token_url, extra=extra, aliases=aliases,
    )
    try:
        await asyncio.to_thread(_write_back, token_file, mtime2, payload)
    except _WriteAborted as aborted:
        logger.warning(
            "Refreshed token for %s not written: the file %s during the refresh",
            token_file.name, aborted,
        )
        if is_registered and str(aborted) == "vanished" and new_ts.refresh_token:
            await mcp_authorization.revoke(
                str(extra.get("revocation_endpoint") or ""), issuer=str(extra.get("issuer") or ""),
                token=new_ts.refresh_token, method=str(extra.get("token_endpoint_auth_method") or "none"),
                client_id=client_id, client_secret=registered_secret,
            )
        return False
    except OSError as exc:
        _pending_writes[key] = _PendingWrite(mtime=mtime2, payload=payload)
        logger.error(
            "Refreshed token for %s/%s could not be written (%s); kept in memory "
            "for the next tick", provider_id, token_file.name, exc,
        )
        return False

    logger.debug(
        "Refreshed token: provider=%s user=%s account=%s flow=%s remaining_before=%.0fs",
        provider_id, (user_sub or "_service")[:8], token_file.stem, arm, remaining,
    )
    _failure_state.pop(key, None)
    return True


async def _refresh_registered(
    raw: dict, *, provider_id: str, refresh_token: str, state: _TickState, issuer_override: str = "",
):
    """Refresh a token the MCP server's own authorization server issued to
    the client this install registered. The registration row is used only
    when it matches the file; a public client refreshes with the file's
    client id regardless, a confidential file without its row is marked
    for reconnect. A token endpoint that moved (404, unreachable) is
    re-discovered once per resource per tick (with the manifest's issuer
    choice); another issuer is a reconnect. Returns the token set, the
    token endpoint to persist and the registration's secret (``""`` for a
    public client)."""
    from auth.oauth_providers.base import OAuthTokenError
    from services.oauth import mcp_authorization
    from storage.identity import oauth_client_registrations as regs
    from storage.pg import run_db

    extra = raw.get("extra") or {}
    issuer = str(extra.get("issuer") or "")
    resource = str(extra.get("resource") or "")
    client_id = str(raw.get("client_id") or "")
    method = str(extra.get("token_endpoint_auth_method") or "none")
    token_url = str(raw.get("token_url") or "")
    secret = ""
    if method != "none":
        row = await run_db(regs.get, int(extra.get("registration_id") or 0))
        usable = (
            row is not None and not row.get("revoked_at")
            and row.get("client_id") == client_id and row.get("issuer") == issuer
        )
        if not usable:
            raise _FileVerdict("registration_unavailable")
        secret = await run_db(regs.client_secret, row["id"])
        if secret is None:
            raise _FileVerdict("registration_unavailable")

    async def _call(endpoint: str):
        return await mcp_authorization.refresh(
            provider_id, token_endpoint=endpoint, refresh_token=refresh_token,
            resource=resource, method=method, client_id=client_id, client_secret=secret,
        )

    try:
        return await _call(token_url), token_url, secret
    except OAuthTokenError as exc:
        if exc.code not in ("http_404", "unreachable") or resource in state.rediscovered:
            raise
        state.rediscovered.add(resource)
        mcp_authorization.forget(resource)
        server = await mcp_authorization.discover(resource, issuer_override=issuer_override)
        if server.issuer.rstrip("/") != issuer.rstrip("/"):
            raise _FileVerdict("issuer_changed")
        return await _call(server.token_endpoint), server.token_endpoint, secret
