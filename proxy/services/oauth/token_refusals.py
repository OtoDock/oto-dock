"""An MCP server's refusal of the token the credential gateway added.

A token that cannot be renewed (a personal access token, an OAuth App token
with no expiry, a vendor that issues no refresh token) and that its vendor
ended leaves nothing to mark the account: no refresh fails, no notice comes.
The gateway route (``api/mcp/gateway.py``) sees the vendor's verdict as the
MCP server's 401, and tells this module:

* ``refused`` on a 401 to a POST, for a token-file credential the proxy adds
  to a vendor's server (never a static value, never a sidecar's, whose 401
  may be its own; never a challenge that asks for more scope). Per token
  file and MCP, in memory, the refusals are counted as events: a refusal
  from a session not counted yet, or from a counted one ten minutes after
  its last counted refusal, so one client's retries are one event. The
  first event of a token the worker can renew asks it for one renewal out
  of turn (``oauth_refresh_worker.renew_now``); a token it cannot renew, or
  the token a renewal issued within the hour, is marked at the second event
  (at the first when its expiry has passed): ``extra.refused_by[<manifest>]``
  in the file (``credential_resolver.REFUSED_BY``). The card then says
  "Reconnect needed" and new sessions leave that MCP out; a session that
  holds it keeps calling.
* ``served`` on a 2xx answer to a POST: an answer to a method the refusals
  carried ends the count, and an answer to a method the mark recorded clears
  the mark. An answered tool call does both whatever they recorded: new
  sessions are refused at ``initialize``, which a running session never sends
  again.

A reconnect writes a fresh file, without the mark. Counts older than a day
start again; the mark is compared with the file by a digest of the token,
never the token itself. The log names the MCP and the session, never the
account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from core import background
from core.credentials import mcp_gateway

if TYPE_CHECKING:
    from core.credentials.mcp_gateway import Prepared, TokenRef

logger = logging.getLogger("otodock.mcp-gateway")

# One session's refusals count as one event unless this far apart.
_SAME_SESSION_GAP_S = 600
# A count older than this starts again.
_WINDOW_S = 24 * 3600
# The token a renewal out of turn issued carries the count over this long.
_RENEWED_CARRY_S = 3600
_EVENTS_TO_MARK = 2
_TABLE_MAX = 256
_INSUFFICIENT_SCOPE = "insufficient_scope"
_TOOL_CALL = "tools/call"
_OWNER = "token-refusals"

# Seam for tests (patch this, not the global clock).
_monotonic = time.monotonic


@dataclass
class _Entry:
    digest: str
    first_at: float
    events: int = 0
    sessions: dict[str, float] = field(default_factory=dict)
    methods: set[str] = field(default_factory=set)
    last_session: str = ""


_Key = tuple[str, str, str]
_entries: dict[_Key, _Entry] = {}
_settling: set[_Key] = set()
_clearing: set[_Key] = set()
# When a renewal out of turn won, per key: a fact of the file and the MCP, so
# it outlives the entry of the token it replaced.
_renewed_at: dict[_Key, float] = {}


def forget() -> None:
    """Drop every count (tests)."""
    _entries.clear()
    _settling.clear()
    _clearing.clear()
    _renewed_at.clear()


def _key(prepared: Prepared) -> _Key | None:
    ref = prepared.token_ref
    if ref is None or prepared.proxy_local or not prepared.manifest or not prepared.digest:
        return None
    return (ref.token_dir, ref.account_label, prepared.manifest)


def _prune(now: float) -> None:
    if len(_entries) + len(_renewed_at) < _TABLE_MAX:
        return
    for key in [k for k, e in _entries.items() if now - e.first_at > _WINDOW_S and k not in _settling]:
        _entries.pop(key, None)
    for key in [k for k, at in _renewed_at.items() if now - at > _RENEWED_CARRY_S]:
        _renewed_at.pop(key, None)


def _renewed_lately(key: _Key, now: float) -> bool:
    at = _renewed_at.get(key)
    return at is not None and now - at <= _RENEWED_CARRY_S


def _drop(key: _Key, entry: _Entry) -> None:
    if _entries.get(key) is entry:
        _entries.pop(key, None)


def refused(prepared: Prepared, *, session_id: str, method: str, body: bytes, challenge: str) -> None:
    """The MCP server answered a forward of ``prepared`` with a 401. Cheap
    and never blocking: the file work runs in a spawned task."""
    if method != "POST" or prepared.marked or _INSUFFICIENT_SCOPE in (challenge or "").lower():
        return
    key = _key(prepared)
    if key is None or prepared.token_ref is None:
        return
    now = _monotonic()
    entry = _entries.get(key)
    if entry is not None and now - entry.first_at > _WINDOW_S:
        entry = None
    if entry is not None and entry.digest != prepared.digest:
        if _renewed_lately(key, now):
            entry.digest = prepared.digest
        else:
            entry = None
    if entry is None:
        _prune(now)
        entry = _entries[key] = _Entry(digest=prepared.digest, first_at=now)
    entry.methods.update(mcp_gateway.request_methods(body))
    last = entry.sessions.get(session_id)
    if last is not None and now - last < _SAME_SESSION_GAP_S:
        return
    entry.sessions[session_id] = now
    entry.last_session = session_id
    entry.events += 1
    if key not in _settling:
        _settling.add(key)
        background.spawn(_settle(key, prepared.token_ref, prepared.manifest),
                         name="settle", owner=_OWNER)


def served(prepared: Prepared, body: bytes) -> None:
    """The MCP server answered a POST forward of ``prepared`` with a 2xx."""
    key = _key(prepared)
    if key is None or prepared.token_ref is None:
        return
    entry = _entries.get(key)
    if entry is None and not prepared.marked:
        return
    methods = set(mcp_gateway.request_methods(body))
    tool_call = _TOOL_CALL in methods
    if entry is not None and (entry.digest != prepared.digest or entry.methods & methods or tool_call):
        _drop(key, entry)
    if prepared.marked and (tool_call or set(prepared.marked) & methods) and key not in _clearing:
        _clearing.add(key)
        background.spawn(_clear(key, prepared.token_ref, prepared.manifest, prepared.digest),
                         name="clear", owner=_OWNER)


def _token_path(ref: TokenRef) -> Path | None:
    from services.oauth import oauth_account_store
    try:
        return oauth_account_store.account_token_path(Path(ref.token_dir), ref.account_label)
    except ValueError:
        return None


def _expired(raw: dict) -> bool:
    from services.oauth import oauth_account_store
    expiry = oauth_account_store._parse_expiry(str(raw.get("expires_at") or ""))
    return expiry is not None and expiry <= datetime.now(timezone.utc)


async def _settle(key: _Key, ref: TokenRef, manifest_name: str) -> None:
    """Act on the count of ``key`` until no event lands meanwhile (a new
    entry, started by a rewritten file while this one ran, included)."""
    try:
        while True:
            entry = _entries.get(key)
            if entry is None:
                return
            seen = entry.events
            await _settle_once(key, entry, ref, manifest_name)
            after = _entries.get(key)
            if after is None or (after is entry and after.events == seen):
                return
    finally:
        _settling.discard(key)


def _still_refusable(ref: TokenRef, manifest_name: str, digest: str) -> dict | None:
    """The token file while it still holds the refused token, carries no
    mark for the MCP and has no other reason it cannot serve (a refresh
    failure the worker recorded meanwhile); else None. Blocking (the file,
    and the origin check may read the store)."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver, oauth_account_store
    raw = oauth_account_store.read_account_token(Path(ref.token_dir), ref.account_label)
    if not raw or mcp_gateway.token_digest(raw, ref) != digest:
        return None
    if credential_resolver.marked_methods(raw, manifest_name):
        return None
    manifest = mcp_registry.get_manifest(manifest_name)
    if manifest is not None and credential_resolver.account_unusable_reason(raw, manifest, refused=False):
        return None
    return raw


async def _settle_once(key: _Key, entry: _Entry, ref: TokenRef, manifest_name: str) -> None:
    from services.oauth import (
        credential_resolver,
        oauth_account_store,
        oauth_refresh_worker,
    )

    path = _token_path(ref)
    digest = entry.digest
    raw = await asyncio.to_thread(_still_refusable, ref, manifest_name, digest) if path else None
    if path is None or raw is None:
        # A late refusal of the token a renewal just replaced: the count
        # stands, and the renewed token's refusal is its next event.
        if not _renewed_lately(key, _monotonic()):
            _drop(key, entry)
        return

    def still(r: dict) -> bool:
        return mcp_gateway.token_digest(r, ref) == digest

    if oauth_refresh_worker.renewable(raw) and not _renewed_lately(key, _monotonic()):
        outcome = await oauth_refresh_worker.renew_now(path, still=still)
        if outcome == oauth_refresh_worker.RENEWED:
            _renewed_at[key] = _monotonic()
            return
        if outcome != oauth_refresh_worker.NOT_RENEWABLE:
            return
    if entry.events < _EVENTS_TO_MARK and not _expired(raw):
        return
    methods = sorted(entry.methods) or [_TOOL_CALL]
    at = oauth_account_store.now_iso()

    def change(r: dict) -> dict | None:
        if not still(r) or credential_resolver.marked_methods(r, manifest_name):
            return None
        extra = r.get("extra")
        extra = dict(extra) if isinstance(extra, dict) else {}
        marks = extra.get(credential_resolver.REFUSED_BY)
        marks = dict(marks) if isinstance(marks, dict) else {}
        marks[manifest_name] = {"at": at, "methods": methods}
        extra[credential_resolver.REFUSED_BY] = marks
        return {**r, "extra": extra}

    if await oauth_refresh_worker.rewrite(path, change):
        logger.info("mcp-gateway: the server of mcp %r refused the token again (session %s): "
                    "the account needs a reconnect", manifest_name, entry.last_session[:8])
        _drop(key, entry)


async def _clear(key: _Key, ref: TokenRef, manifest_name: str, digest: str) -> None:
    """Take ``manifest_name``'s mark off the file while it still holds the
    token that was answered."""
    from services.oauth import credential_resolver, oauth_refresh_worker
    try:
        path = _token_path(ref)
        if path is None:
            return

        def change(r: dict) -> dict | None:
            if mcp_gateway.token_digest(r, ref) != digest:
                return None
            extra = r.get("extra")
            marks = extra.get(credential_resolver.REFUSED_BY) if isinstance(extra, dict) else None
            if not isinstance(extra, dict) or not isinstance(marks, dict) or manifest_name not in marks:
                return None
            left = {k: v for k, v in marks.items() if k != manifest_name}
            extra = dict(extra)
            if left:
                extra[credential_resolver.REFUSED_BY] = left
            else:
                extra.pop(credential_resolver.REFUSED_BY, None)
            return {**r, "extra": extra}

        if await oauth_refresh_worker.rewrite(path, change):
            logger.info("mcp-gateway: the server of mcp %r answered the token again: the "
                        "reconnect mark is cleared", manifest_name)
    finally:
        _clearing.discard(key)
