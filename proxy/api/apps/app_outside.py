"""The app's door for an outside person's session: a caller on the agent's phone line, on an app its agent
opened to external identities on the External identities card. The same
door as every agent session (``app_proxy._agent_caller`` hands it the
token): the app's exported methods floored ``external`` and nothing else,
the picks re-read at every call, a claim that names the channel and an
opaque person and never the number."""

from __future__ import annotations

import time

from api.apps.app_proxy import Caller, _refuse
from auth import roles
from services.apps import app_tokens
from storage.pg import run_db

# The picks as they stand, read again for every outside call (a manager's
# removal bites at the next call) and kept a moment, so a burst of calls
# costs one read: ``agent -> (monotonic time, picked app ids)``.
_OUTSIDE_PICKS_TTL_S = 2.0
_outside_picks_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def _outside_apps_now(agent: str) -> tuple[str, ...]:
    """The apps ``agent`` opens to outside people now (``outside_picks.load``:
    each picked, approved, opening a method to them and reading no folder
    they may not see). Synchronous."""
    from services.mcp import outside_picks
    now = time.monotonic()
    cached = _outside_picks_cache.get(agent)
    if cached and now - cached[0] < _OUTSIDE_PICKS_TTL_S:
        return cached[1]
    apps = outside_picks.load(agent).apps
    _outside_picks_cache[agent] = (now, apps)
    return apps


def _person_of(agent: str, channel: str, ident: str) -> str:
    """The opaque stable id an app keys an outside person on: their
    identity row's id, "" for a caller with no identity of their own."""
    from core.session import external_identity
    from storage.identity import external_identity_store
    if not ident:
        return ""
    found = external_identity_store.get(agent, channel, external_identity.slug_for(ident))
    return str((found or {}).get("id") or "")


async def outside_caller(payload: dict, row: dict) -> Caller:
    """An outside person's session (a caller on the agent's phone line) on
    an app its agent opened to them on the External identities card: the
    app's exported methods floored ``external`` alone, never more than the
    app declares, through the same door as every agent session. The app
    receives an ``external`` claim naming the channel and an opaque person
    id, never the caller's number nor the session. An outside call is never
    the app's own identity, and its claim is refused when relayed."""
    from core.session import external_identity
    from core.session.session_state import get_session_security, session_token_refusal
    if session_token_refusal(payload):
        raise _refuse(401, "the session token is no longer valid")
    try:
        channel, ident, ephemeral = external_identity.parse_claim(str(payload.get("ext") or ""))
    except ValueError as exc:
        raise _refuse(401, "the session token is damaged") from exc
    sid = str(payload.get("sid") or "")
    agent = str(payload.get("agent") or "")
    ctx = get_session_security(sid)
    if ctx is None:
        raise _refuse(401, "the session token is no longer valid")
    if (row.get("agent") or "") != agent or row["id"] not in tuple(getattr(ctx, "outside_apps", ()) or ()):
        raise _refuse(404, "App not found")
    if row["id"] not in await run_db(_outside_apps_now, agent):
        raise _refuse(403, "This app is not available right now.")
    person = "" if ephemeral else await run_db(_person_of, agent, channel, ident)
    claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_CALLER, {
        "principal": app_tokens.PRINCIPAL_EXTERNAL, "sub": "", "username": "", "role": roles.EXTERNAL,
        "agent": agent, "external": True, "channel": channel, "person": person,
        "ephemeral": bool(ephemeral) or not person, "verified": bool(getattr(ctx, "external_verified", False)),
    }, app_tokens.CALLER_TTL_S)
    return Caller(basis="external", sub="", role=roles.EXTERNAL, claim=claim,
                  exp=int(time.time()) + app_tokens.CALLER_TTL_S,
                  extra={"outside": sid, "channel": channel, "person": person})


def check_outside_method(row: dict, path: str) -> None:
    """An outside person's session reaches the methods its app opens to
    them: the first path segment an exported method floored ``external``."""
    from services.apps import manifest as _mf
    entry = _mf.exported_method(row, path)
    if entry is None or entry.get("min_role") != _mf.OUTSIDE_FLOOR:
        raise _refuse(404, "not open to outside people")
