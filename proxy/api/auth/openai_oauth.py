"""OpenAI OAuth REST API — device code flow via `codex login --device-auth`.

Flow:
1. POST /v1/oauth/openai/start → spawns `codex login --device-auth` on server
   in a private Codex home of its own, returns {url, user_code, login_id}
2. User opens the verification URL on any device and enters the code
3. GET /v1/oauth/openai/status → polls until codex login writes that home's
   auth.json
4. POST /v1/oauth/openai/finish → reads that auth.json, stores as encrypted
   subscription, removes the home

Works from everywhere (Android, desktop, remote) — no localhost redirect needed.
For per-user subscriptions: same flow but stored with owner_type="user".
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
import signal
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import get_current_user, require_human, UserContext, require_user
from services.engines import subscription_pool
from storage.billing import subscription_status, subscription_store
import config as app_config
import contextlib
from auth import rate_limiter, roles

logger = logging.getLogger(__name__)
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])

#: The vendor this login route belongs to — the engine's ``identity.vendor_id``
#: and the provider its OAuth rows carry, named once.
_VENDOR = "openai"

# Active login sessions: login_id → {proc, home, user_sub, owner_type, layer, started_at}.
# A login is registered BEFORE its process is spawned (``proc`` is None until
# the spawn returns), so no running login ever exists outside this registry.
_active_logins: dict[str, dict] = {}

# Every device login runs with its own ``CODEX_HOME`` under here (one
# directory per login id), so a login's ``auth.json`` can only ever hold the
# account that login produced. Never the host's own ``~/.codex``: the CLI
# refuses helper binaries under a temporary directory, so this sits beside it.
_LOGIN_HOME_BASE = Path.home() / ".codex-logins"

# Device codes expire after 15 minutes; a login is swept at 20.
_DEVICE_CODE_TTL_S = 900
_LOGIN_MAX_AGE_S = 1200
_SWEEP_EVERY_S = 60
# How long ``finish`` lets the CLI exit on its own after writing its file,
# and how long each signal gets before the next when a login is ended.
_FINISH_EXIT_GRACE_S = 2.0
_END_GRACE_S = 1.0

_last_sweep = 0.0

# Starts per person and window (``config.RATE_LIMIT_RULES``). Each start
# spawns a host process, so the cap is small; the poll and the finish are
# not limited.
_START_BUCKET = "oauth_start_openai"

# Strip ANSI escape codes from codex output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _require_chatgpt_login_engine(layer: str) -> None:
    """The engine a ChatGPT login is stored on must be one that takes an
    OpenAI OAuth login (``identity.vendor_id`` + ``oauth`` among its auth
    types) — the layer used to be stored verbatim from the request."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(layer)
    if (caps is None or "oauth" not in caps.auth.auth_types
            or caps.identity.vendor_id != _VENDOR):
        raise HTTPException(400, f"{layer} does not take a ChatGPT login")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class OAuthStartRequest(BaseModel):
    layer: str = "codex-cli"
    owner_type: str = "platform"  # 'platform' (admin) or 'user'


class OAuthFinishRequest(BaseModel):
    login_id: str
    layer: str = "codex-cli"
    label: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/v1/oauth/openai/start")
async def oauth_start(
    req: OAuthStartRequest,
    user: UserContext = Depends(get_current_user),
):
    """Start OpenAI device code auth. Returns verification URL + user code.

    Spawns `codex login --device-auth` on the server, parses the one-time
    code and verification URL from stdout.
    """
    user = require_human(user)
    if req.owner_type == "platform" and not roles.is_admin(user.role):
        raise HTTPException(403, "Admin required for platform subscriptions")
    _require_chatgpt_login_engine(req.layer)
    limit_connect_start(_START_BUCKET, user.sub)

    await _sweep_logins()
    # The caller's own earlier login gives way to this one; every other
    # person's login stays registered, running, with its home intact.
    for lid, meta in [(lid, m) for lid, m in _active_logins.items() if m["user_sub"] == user.sub]:
        await _end_login(lid, meta)

    login_id = secrets.token_urlsafe(16)
    home = _LOGIN_HOME_BASE / login_id
    meta = {
        "proc": None,
        "home": home,
        "user_sub": user.sub,
        "owner_type": req.owner_type,
        "layer": req.layer,
        "started_at": time.monotonic(),
    }
    # Registered before the home is made (off the loop): a newer start by
    # the same person during that wait finds this login and ends it.
    _active_logins[login_id] = meta
    try:
        await asyncio.to_thread(_make_login_home, home)
    except Exception:
        await _end_login(login_id, meta)
        raise
    if _active_logins.get(login_id) is not meta:
        await _end_login(login_id, meta)
        raise HTTPException(409, "Replaced by a newer login")

    # Spawn codex login --device-auth via node directly (the codex binary
    # is a Node.js script and systemd services have minimal PATH)
    codex_bin = getattr(app_config, "CODEX_BIN", "codex")
    codex_resolved = os.path.realpath(codex_bin)
    # Resolve node from PATH — its location varies by install (bare-metal apt
    # = /usr/bin/node, the container image = /usr/local/bin/node); keep the
    # apt path as the fallback for systemd's minimal PATH.
    node_bin = shutil.which("node") or "/usr/bin/node"
    logger.info(
        f"codex device-auth: binary={codex_bin}, resolved={codex_resolved}, "
        f"node={node_bin}"
    )
    spawn_env = {**os.environ, "BROWSER": "echo", "CODEX_HOME": str(home)}
    try:
        # Its own session, so the node wrapper and the native codex child
        # share a process group that one signal reaches (see _end_process).
        proc = await asyncio.create_subprocess_exec(
            node_bin, codex_resolved, "login", "--device-auth",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=spawn_env,
            start_new_session=True,
        )
    except Exception:
        await _end_login(login_id, meta)
        raise
    meta["proc"] = proc
    if _active_logins.get(login_id) is not meta:
        await _end_login(login_id, meta)
        raise HTTPException(409, "Replaced by a newer login")

    # Read stdout to extract verification URL and user code
    verification_url = ""
    user_code = ""
    all_output = []
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=3,
                )
            except asyncio.TimeoutError:
                # If we already have both values, stop reading
                if verification_url and user_code:
                    break
                continue
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            all_output.append(raw)
            # Strip ANSI codes for parsing
            clean = _ANSI_RE.sub("", raw)
            logger.info(f"codex device-auth: {clean[:200]}")

            # Extract verification URL (e.g., https://auth.openai.com/codex/device)
            if not verification_url:
                url_match = re.search(r"(https://\S+)", clean)
                if url_match:
                    verification_url = url_match.group(1)

            # Extract user code — alphanumeric with dash, on its own line
            # Format observed: KI02-SGF8G (4-5 chars, dash, 4-5 chars)
            if not user_code and verification_url:
                code_match = re.match(r"^\s*([A-Z0-9]{3,6}-[A-Z0-9]{3,6})\s*$", clean)
                if code_match:
                    user_code = code_match.group(1)

            if verification_url and user_code:
                break
    except Exception as e:
        logger.error(f"Error reading codex device-auth output: {e}")

    if _active_logins.get(login_id) is not meta:
        await _end_login(login_id, meta)
        raise HTTPException(409, "Replaced by a newer login")
    if not verification_url or not user_code:
        clean_output = [_ANSI_RE.sub("", l) for l in all_output]
        logger.error(
            f"codex device-auth: missing url={bool(verification_url)} "
            f"code={bool(user_code)}. Output: {clean_output[:10]}"
        )
        await _end_login(login_id, meta)
        raise HTTPException(
            500,
            "Failed to start device code auth — could not extract code. "
            "Make sure device code login is enabled in your ChatGPT Security Settings.",
        )

    logger.info(f"OpenAI device-auth started (id={login_id[:8]}, code_received={bool(user_code)})")
    return {"url": verification_url, "user_code": user_code, "login_id": login_id}


@router.get("/v1/oauth/openai/status")
async def oauth_status(
    login_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Poll whether the codex login process has completed."""
    user = require_human(user)
    if time.monotonic() - _last_sweep > _SWEEP_EVERY_S:
        await _sweep_logins()
    meta = _active_logins.get(login_id)
    if not meta:
        raise HTTPException(404, "Login session not found")
    if meta["user_sub"] != user.sub:
        raise HTTPException(403, "Not your login session")

    # Only this login's own file counts, and only once it is whole: the CLI
    # writes it, then exits. The exit is noted BEFORE the file is read (off
    # the loop), so an exit judged a failure is one whose file the read
    # would have found; a login finished or replaced during the read is
    # gone, as for a later poll.
    proc = meta["proc"]
    exited = proc is not None and proc.returncode is not None
    if await asyncio.to_thread(_whole_login_file, meta["home"]) is not None:
        return {"status": "completed"}
    if _active_logins.get(login_id) is not meta:
        raise HTTPException(404, "Login session not found")

    if exited:
        await _end_login(login_id, meta)
        return {"status": "failed", "message": "Login process exited without writing credentials"}

    if time.monotonic() - meta["started_at"] > _DEVICE_CODE_TTL_S:
        await _end_login(login_id, meta)
        return {"status": "failed", "message": "Device code expired (15 minutes). Please try again."}

    return {"status": "pending"}


@router.post("/v1/oauth/openai/finish")
async def oauth_finish(
    req: OAuthFinishRequest,
    user: UserContext = Depends(get_current_user),
):
    """Read auth.json and store as subscription. Call after status=completed."""
    user = require_human(user)
    meta = _active_logins.get(req.login_id)
    if not meta:
        raise HTTPException(404, "Login session not found or already finished")
    if meta["user_sub"] != user.sub:
        raise HTTPException(403, "Not your login session")
    # From here the login is the caller's own and ends on every path: its
    # process is settled first, its home removed last.
    _active_logins.pop(req.login_id, None)
    try:
        await _settle_process(meta["proc"])
        return await _store_login(req, meta, user)
    finally:
        await asyncio.to_thread(shutil.rmtree, meta["home"], ignore_errors=True)


async def _store_login(req: OAuthFinishRequest, meta: dict, user: UserContext) -> dict:
    if meta["owner_type"] == "platform" and not roles.is_admin(user.role):
        raise HTTPException(403, "Admin required for platform subscriptions")
    _require_chatgpt_login_engine(meta["layer"])

    auth_data = await asyncio.to_thread(_read_login_blob, meta["home"])

    access_token = _token_field(auth_data, "access_token")
    refresh_token = _token_field(auth_data, "refresh_token")
    if not access_token:
        raise HTTPException(400, "No access token found in credentials file")

    # The account this login produced. A blob that names none is refused,
    # never stored under a guessed row: the stored identity is what a
    # reconnect matches on.
    identity = _login_identity(auth_data)
    if not identity:
        logger.warning("Codex auth blob carried no account identity: login not stored")
        raise HTTPException(
            400, "This login did not identify a ChatGPT account, so it was not stored.",
        )

    # Build credential data in our standard format
    credential_data = {
        "oauth_token": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int((time.time() + 28800) * 1000),  # 8 hour default
        },
        # Store full Codex auth.json structure for session auth.json reconstruction.
        # Codex CLI requires id_token, account_id, etc. alongside access_token.
        "codex_auth_blob": auth_data,
    }

    label = req.label or "ChatGPT (subscription)"
    # The connector owns the account (admin for 'platform', the user for 'user');
    # a 'platform' connect also contributes it to the agent pool. The admin gate
    # above ensures only an admin can request owner_type='platform'. Flags are set
    # on CREATE only — reconnect just refreshes tokens.
    is_platform = meta["owner_type"] == "platform"
    owner_sub = meta["user_sub"]
    from storage import database as _db

    def _match_or_create() -> tuple[dict | None, dict | None]:
        """``(match, None)`` for the row already holding this account,
        ``(None, row)`` for the row created for it."""
        # Reconnecting the SAME account (matched by the auth blob's identity)
        # refreshes tokens on the existing row; a DIFFERENT account creates a
        # second subscription (users can pool several plans). Matching on mere
        # (owner, layer, provider) silently clobbered the first account's
        # credential when a second one was connected — see the Anthropic twin
        # in claude_oauth.py.
        # include_disabled: a reconnect on an admin-disabled row must MATCH it
        # (and keep it disabled, below) — excluding it would fork a second ACTIVE
        # row for the same account, silently routing around the admin.
        existing = subscription_store.list_subscriptions(
            layer=meta["layer"],
            owner_sub=owner_sub,
            include_disabled=True,
        )
        existing_oauth = [s for s in existing if s["auth_type"] == "oauth" and s["provider"] == _VENDOR]
        # Only a row proven to be the same account is refreshed; legacy rows
        # (oauth_email == "") are never adopted by guesswork.
        match = next((s for s in existing_oauth if s.get("oauth_email") == identity), None)
        if match:
            return match, None
        # Admins' personal connects ALSO contribute to the shared agent pool by
        # default (so agent-scoped tasks work without the admin knowing to tick it).
        connector_is_admin = roles.is_admin((_db.get_user(owner_sub) or {}).get("role"))
        return None, subscription_store.add_subscription(
            layer=meta["layer"],
            provider=_VENDOR,
            auth_type="oauth",
            owner_sub=owner_sub,
            use_personal=True,
            contribute_platform=is_platform or connector_is_admin,
            label=label,
            credential_data=credential_data,
            oauth_email=identity,
        )

    match, sub = await asyncio.to_thread(_match_or_create)
    if match:
        # Under the sub's refresh lock — an in-flight refresh of the old
        # token must not land its failure verdict on the fresh grant. An
        # admin-DISABLED row keeps its status (see the Anthropic twin).
        sub_id = match["id"]
        new_status = (subscription_status.DISABLED if match.get("status") == subscription_status.DISABLED
                      else subscription_status.ACTIVE)

        def _apply_reconnect() -> None:
            with subscription_pool._refresh_lock(sub_id):
                subscription_store.update_credential_data(sub_id, credential_data)
                subscription_store.update_subscription(
                    sub_id, status=new_status, label=label, oauth_email=identity,
                )
                subscription_pool.clear_refresh_backoff(sub_id)

        await asyncio.to_thread(_apply_reconnect)
        sub = await asyncio.to_thread(subscription_store.get_subscription, sub_id)
        logger.info(f"Updated existing OpenAI OAuth subscription {sub_id[:8]} with fresh tokens")
        # The exchange rotated the grant OUTSIDE the rotation chokepoint —
        # push the fresh token into live bound sessions' auth.json files
        # (see the Anthropic twin in claude_oauth.py).
        await asyncio.to_thread(subscription_pool.fan_out_current_token, sub_id)
    else:
        logger.info(f"Created new OpenAI OAuth subscription {sub['id'][:8]}")

    # A freshly (re)connected account may be the replacement that sessions
    # stuck on a delisted/removed subscription are waiting for.
    subscription_pool.schedule_rebind("openai oauth connect")
    # The account's window bars show right after the connect.
    from services.engines import subscription_windows
    subscription_windows.schedule_poll(str(sub.get("id") or ""))
    return {"subscription": sub}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def limit_connect_start(bucket: str, user_sub: str) -> None:
    """Refuse a start past the person's cap for the window (429 with
    ``Retry-After``). Taken after the role and engine checks and before any
    state is created, so a refused bearer never counts against its owner."""
    allowed, retry_after = rate_limiter.hit(bucket, user_sub)
    if not allowed:
        wait = max(1, retry_after)
        raise HTTPException(
            429, f"Too many connect attempts: try again in {wait} s",
            headers={"Retry-After": str(wait)},
        )


def _make_login_home(home: Path) -> None:
    """Make a login's private ``CODEX_HOME``, owner-only like the files the
    CLI writes into it. A pre-existing base gets its mode corrected too."""
    _LOGIN_HOME_BASE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(_LOGIN_HOME_BASE, 0o700)
    home.mkdir(mode=0o700)


def _read_login_blob(home: Path) -> dict:
    """The finished login's ``auth.json``, or the finish's 400."""
    auth_path = Path(home) / "auth.json"
    if not auth_path.exists():
        raise HTTPException(400, "No credentials found — login may have failed")
    try:
        auth_data = json.loads(auth_path.read_text())
    except Exception as e:
        raise HTTPException(400, f"Failed to read credentials: {e}")
    if not isinstance(auth_data, dict):
        raise HTTPException(400, "Failed to read credentials: not a JSON object")
    return auth_data


def _token_field(auth_data: dict, name: str) -> str:
    """``access_token`` / ``refresh_token`` at the top level or under
    ``tokens`` (the layout Codex writes)."""
    tokens = auth_data.get("tokens")
    nested = tokens.get(name) if isinstance(tokens, dict) else None
    return str(auth_data.get(name) or nested or "")


def _whole_login_file(home: Path) -> dict | None:
    """The login's ``auth.json`` once it is whole (parses, carries an access
    token); None while it is absent or still being written."""
    try:
        data = json.loads((Path(home) / "auth.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _token_field(data, "access_token"):
        return None
    return data


def _login_identity(auth_data: dict) -> str:
    """The account a blob belongs to, the key a reconnect matches rows on:
    the top-level ``email`` (never written by Codex, kept first for rows
    stored before the id-token fallback), then ``tokens.account_id`` (what
    Codex writes), then the ``email`` or ``chatgpt_account_id`` claim of
    the id token."""
    tokens = auth_data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    identity = auth_data.get("email") or tokens.get("account_id")
    if identity:
        return str(identity)
    claims = _jwt_claims(tokens.get("id_token"))
    auth_claims = claims.get("https://api.openai.com/auth")
    auth_claims = auth_claims if isinstance(auth_claims, dict) else {}
    return str(claims.get("email") or auth_claims.get("chatgpt_account_id") or "")


def _jwt_claims(token: object) -> dict:
    """A JWT's payload, decoded without verification: the token came from
    the login this proxy ran, and the value is a label to match rows on,
    never a credential."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _signal_login(proc, sig: int) -> None:
    """Signal the login's whole process group (the spawn made the wrapper a
    session leader). SIGTERM to the node wrapper alone is forwarded to the
    native codex child; SIGKILL alone orphans it, so both go to the group.
    A process without a group of its own gets the signal directly."""
    try:
        os.killpg(proc.pid, sig)
    except (OSError, TypeError):
        with contextlib.suppress(Exception):
            proc.send_signal(sig)


async def _end_process(proc) -> None:
    """SIGTERM, a bounded wait, then SIGKILL; returns once the process is
    reaped or the second wait lapsed."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.returncode is not None:
            return
        _signal_login(proc, sig)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), _END_GRACE_S)


async def _settle_process(proc) -> None:
    """Let the CLI exit on its own after writing its file; end it if it
    does not."""
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), _FINISH_EXIT_GRACE_S)
    if proc.returncode is None:
        await _end_process(proc)


async def _end_login(login_id: str, meta: dict) -> None:
    """Drop the login's registry entry (when it is still this one), end its
    process, and remove its home once the process has been waited for."""
    if _active_logins.get(login_id) is meta:
        del _active_logins[login_id]
    proc = meta.get("proc")
    if proc is not None and proc.returncode is None:
        await _end_process(proc)
    await asyncio.to_thread(shutil.rmtree, meta["home"], ignore_errors=True)


async def _sweep_logins() -> None:
    """End registered logins older than 20 minutes, and remove homes of that
    age that belong to no registered login (left by a restart or a crash).
    A young unregistered home is never touched."""
    global _last_sweep
    _last_sweep = time.monotonic()
    now = time.monotonic()
    for lid, meta in [(lid, m) for lid, m in _active_logins.items() if now - m["started_at"] > _LOGIN_MAX_AGE_S]:
        await _end_login(lid, meta)
    # The registry is read here, on the loop; a login registered after it
    # has a home younger than the cutoff, which the sweep never touches.
    await asyncio.to_thread(_remove_orphan_homes, frozenset(_active_logins),
                            time.time() - _LOGIN_MAX_AGE_S)


def _remove_orphan_homes(registered: frozenset[str], cutoff: float) -> None:
    """Remove the login homes older than ``cutoff`` that belong to no
    registered login. Synchronous: call it on a worker thread."""
    try:
        entries = list(os.scandir(_LOGIN_HOME_BASE))
    except OSError:
        return
    for entry in entries:
        if entry.name in registered:
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue
