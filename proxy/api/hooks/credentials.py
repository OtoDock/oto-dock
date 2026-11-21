"""The hooks a session's tools fetch what they run on: an MCP's credentials, a
CLI's bearer and the session's SSH agent.
"""

import asyncio
import hmac
import logging
import re

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

import config
from api.sessions.sessions import verify_session_match_async
from core.session.session_state import session_token_refusal

logger = logging.getLogger("otodock")

router = APIRouter()

#: A host name as a sentence or a log line may name it: letters, digits,
#: dots and dashes, and a port.
_HOST_NAME = re.compile(r"[a-z0-9.-]+(:[0-9]{1,5})?")


def _named_host(host: str) -> str:
    """The request's host as an answer or a log line names it: lower case,
    cut to 253, and only when it is a host name; ``""`` otherwise."""
    name = (host or "").strip().lower()[:253]
    return name if _HOST_NAME.fullmatch(name) else ""


_RENEWAL_ENDED = (
    "This tool has been running for more than a day, so its account token cannot be renewed. "
    "Start a new chat, or close the app and open it again later."
)


def capability_token(authorization: str | None) -> str:
    """The capability token of a tool's hook call (``Bearer <token>``), 401
    when there is none or the header has another shape."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing capability token")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    return parts[1]


@router.post("/v1/hooks/mcp-credentials")
async def hook_mcp_credentials(authorization: str | None = Header(None)):
    """Credential broker: return an stdio MCP's secret env at spawn, or, to a
    renewal token, its account token alone when the hour ends.

    Auth is the per-(session, mcp) CAPABILITY TOKEN ONLY — NOT the session JWT
    and NOT the master key. The agent's bash holds the session JWT + PROXY_URL +
    curl, so accepting it would let the agent harvest every MCP's (and every
    co-resident user's) secrets; the capability token is instead injected only
    into the MCP child's env and stripped by the wrapper before exec. The ``mcp``
    is derived from the token, so a token for one MCP can't fetch another's.
    Answered on the internal listener only (a sandbox's forward, the satellite
    tunnel: ``middleware._session_refusal``) — the token binding is the
    boundary; the listener and the tunnel allowlist are defense-in-depth.

    A renewal token (``mcp_broker.mint_renewal``, the tool keeps it in its own
    environment) names the same pair and reaches only the token's names, read
    fresh: 404 with no bundle or no token half, 409 when the account needs a
    reconnect or the renewal is past its life, 503 while a token that is
    running out cannot be refreshed yet.
    """
    from core.credentials import mcp_broker
    token = capability_token(authorization)
    renewal = mcp_broker.verify_renewal(token)
    if renewal is not None:
        return {"env": await _renewed_token(*renewal)}
    if mcp_broker.renewal_ended(token):
        raise HTTPException(status_code=409, detail=_RENEWAL_ENDED)
    live = mcp_broker.live_token(token)
    if live == 401:
        raise HTTPException(status_code=401, detail="Invalid or expired capability token")
    if isinstance(live, int):
        # Session closed / purged / proxy restarted mid-session → fail fast so
        # the spawn-time wrapper errors cleanly instead of hanging on its timeout.
        raise HTTPException(status_code=404, detail="No credentials for this session")
    env = await _spawn_env(live[2])
    from core.credentials import credential_exit
    return {"env": {**env, **credential_exit.own_direct(live[0], getattr(live[2], "exit", ()) or (), env)}}


async def _spawn_env(bundle) -> dict[str, str]:
    """The env a tool starts with: the bundle, its account token read fresh
    over the value the build held. A token that cannot be read now stays
    as built: the tool's own ask at the hour's end says why. A name the
    credential exit covers keeps its placeholder, and no expiry rides with
    it: the exit reads the token file per request."""
    from core.credentials import mcp_gateway
    if bundle.token is None:
        return bundle.env
    res = await asyncio.to_thread(mcp_gateway.resolve_token_credential, bundle.token)
    if isinstance(res, mcp_gateway.Refusal):
        return bundle.env
    fresh = mcp_gateway.token_env(bundle.token, res)
    covered = {c.env_name for c in (getattr(bundle, "exit", ()) or ()) if c.env_name}
    if covered & set(bundle.token.env_names):
        for name in bundle.token.env_names:
            fresh.pop(name, None)
        fresh.pop(mcp_gateway.TOKEN_EXPIRES_ENV, None)
    return {**bundle.env, **fresh}


async def _renewed_token(sid: str, mcp: str) -> dict[str, str]:
    """The answer to a renewal: the token's names alone, after asking the
    refresh worker, which refreshes it if it is due."""
    from pathlib import Path

    from core.credentials import mcp_broker, mcp_gateway
    from services.oauth import oauth_account_store, oauth_refresh_worker
    bundle = mcp_broker.get(sid, mcp)
    cred = bundle.token if bundle is not None else None
    if cred is None or not cred.renews:
        # A token the credential exit carries never renews in the session:
        # the tool holds a placeholder, and a renewal it kept from before a
        # rebuild answers nothing.
        raise HTTPException(status_code=404, detail="No credentials for this session")
    try:
        token_file = oauth_account_store.account_token_path(
            Path(cred.token_ref.token_dir), cred.token_ref.account_label)
    except ValueError:
        raise HTTPException(status_code=404, detail="No credentials for this session") from None
    await oauth_refresh_worker.refresh_if_due(token_file)
    res = await asyncio.to_thread(mcp_gateway.resolve_token_credential, cred)
    if isinstance(res, mcp_gateway.Refusal):
        logger.info("mcp-credentials: the token of %s for %s is refused (%s)", mcp, sid[:8], res.reason)
        raise HTTPException(status_code=409, detail=res.detail)
    if res.expires_in is not None and res.expires_in < mcp_gateway.TOKEN_MIN_LIFE_S:
        raise HTTPException(status_code=503, detail="The account's token is being renewed. Try again in a moment.")
    return mcp_gateway.token_env(cred, res)


class ExitRequest(BaseModel):
    """One request a session's credential exit is about to send with the
    command-line credential's placeholder (a machine's exit asks): the
    placeholder, the request's method, host and target, its body's type and
    encoding, and the body where the rule asked for it."""

    placeholder: str = Field(max_length=64)
    method: str = Field(max_length=16)
    host: str = Field(max_length=255)
    target: str = Field(max_length=8192)
    content_type: str = Field(default="", max_length=255)
    content_encoding: str = Field(default="", max_length=64)
    # The rule's bound (``github_reach.REQUEST_BODY_MAX``, 32 KiB) encoded,
    # with the padding: the whole ask stays under the body the middleware
    # lets a command-line token send.
    body_b64: str | None = Field(default=None, max_length=44_000)


class HookCliCredentialRequest(BaseModel):
    session_id: str
    host: str
    # What the command names, for a narrowed GitHub reach
    # (``auth/github_reach``): git's repository path, or gh's arguments, the
    # working directory's remote addresses and gh's GH_REPO and GH_HOST. An
    # older helper sends none.
    path: str = Field(default="", max_length=1024)
    command: list[str] = Field(default_factory=list, max_length=256)
    remotes: list[str] = Field(default_factory=list, max_length=32)
    gh_repo: str = Field(default="", max_length=512)
    gh_host: str = Field(default="", max_length=255)
    # The credential exit's ask for one request: answered with a verdict,
    # never with a credential (``_exit_verdict``).
    request: ExitRequest | None = None


@router.post("/v1/hooks/cli-credential")
async def hook_cli_credential(
    req: HookCliCredentialRequest, authorization: str | None = Header(None),
):
    """The credential a session's command-line tools get for ``host`` right
    now: the git credential helper and the ``gh`` wrapper
    (``proxy/hooks/credential_helper.py``) ask here at each command, so the
    token is in no environment and no file and the current token file is
    read every time. Auth is the session's COMMAND-LINE token
    (``OTO_CLI_TOKEN``, ``session_token.attach_cli_token``): the shell holds
    it, no MCP child does, so an MCP holding the session token cannot read
    the account's token here. The holder is judged as a session token's is,
    its session against the body's. A session token is refused (403) unless
    it was minted before 1.9: a session started before the upgrade has no
    command-line token, and keeps its git and gh until its token expires.
    The master key is refused outright (it names no session). Answers: the
    username git presents, the session's placeholder as the password (the
    token itself only for a credential built per command, on a paired macOS
    or Windows computer) and the names the wrapper sets; 404 when the
    session holds no credential for the host (not connected, left out on a
    machine whose exit does not run, or the store emptied by a restart and
    not re-provisioned); 409 when the account needs a reconnect, or when the
    credential holds neither a placeholder nor the per-command mark. Answered on the internal listener only: from
    a sandbox and over the satellite tunnel (``middleware._session_refusal``).

    With ``request`` the caller is a machine's credential exit, about to
    send one request with the placeholder: the answer is a verdict under
    ``verdict`` and never a credential (``_exit_verdict``), so the check of
    each command and the check of each request share this route, its
    authorization and the session's one reach.
    """
    from core.credentials import mcp_broker, mcp_gateway
    if req.request is not None:
        return await _exit_verdict(req, authorization)
    await _authorize_cli_bearer(authorization, req.session_id)
    cred = mcp_broker.cli_credential(req.session_id, req.host)
    if cred is None:
        host = _named_host(req.host)
        raise HTTPException(
            status_code=404,
            detail=(f"No {host} account is connected for this session" if host
                    else "No account for this host is connected for this session"),
        )
    refusal = _reach_refusal(cred, req)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    res = await asyncio.to_thread(mcp_gateway.resolve_cli_credential, cred)
    if isinstance(res, mcp_gateway.Refusal):
        raise HTTPException(status_code=409, detail=res.detail)
    # The session holds a placeholder for the host
    # (``mcp_session_config._exit_placeholders``): git and gh send it, and
    # the exit writes the token into the request on the way out, so the
    # token itself never enters the session. On a paired computer gh's
    # process also gets the machine's exit variables (``process_env``,
    # which the helper sets on the real command). The token itself goes only
    # to a credential built per command (a paired macOS or Windows computer,
    # ``mcp_session_config.cli_per_command``); anything else holds no key.
    from core.credentials import credential_exit
    placeholder = mcp_broker.exit_placeholder_for_cli(req.session_id, cred)
    if placeholder:
        return {"username": res.username, "password": placeholder, "env": list(res.env_names),
                "process_env": credential_exit.cli_process_env(req.session_id)}
    if cred.per_command:
        return {"username": res.username, "password": res.secret, "env": list(res.env_names),
                "process_env": {}}
    host = _named_host(req.host)
    logger.warning("cli-credential: %s holds no placeholder for %s", req.session_id[:8], host or "a host it named")
    raise HTTPException(status_code=409,
                        detail=f"This session holds no key for {host or 'this host'}. Start a new chat.")


async def _exit_verdict(req: HookCliCredentialRequest, authorization: str | None) -> dict:
    """The word on one request a session's exit is about to send
    (``credential_exit.judge_request``: the provider's rule under the reach
    the session was built with). The bearer is the session's command-line
    token alone, with no grace for a session token from before 1.9: such a
    session holds no placeholder. The answer is always the one object
    ``verdict``: an exit takes nothing else for a "go". Neither the target
    nor the body is logged."""
    import base64
    import binascii

    from core.credentials import credential_exit
    await _authorize_cli_bearer(authorization, req.session_id, grace=False)
    ask = req.request
    assert ask is not None
    body = None
    if ask.body_b64 is not None:
        try:
            body = base64.b64decode(ask.body_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="The body is not base64") from None
    word = credential_exit.judge_request(
        req.session_id, ask.placeholder, ask.method, ask.host, ask.target,
        {"content_type": ask.content_type, "content_encoding": ask.content_encoding}, body)
    return {"verdict": {
        "status": word.status, "detail": word.detail, "content_type": word.content_type,
        "wants_body": word.wants_body,
        "body_b64": base64.b64encode(word.body).decode("ascii") if word.body is not None else None,
        "link": list(word.link) if word.link is not None else None,
    }}


def _reach_refusal(cred, req: HookCliCredentialRequest) -> str:
    """A narrowed GitHub reach judges what the command names: gh by its
    arguments, git by the repository path it asks for."""
    from auth import github_reach
    reach = github_reach.Reach.from_dict(cred.reach)
    if reach is None or not reach.narrows:
        return ""
    if req.command:
        return github_reach.judge_gh(reach, [str(a)[:1024] for a in req.command], list(req.remotes),
                                     gh_repo=req.gh_repo, gh_host=req.gh_host)
    return github_reach.judge_git(reach, req.path)


# The sessions whose pre-1.9 session token was taken on the command-line
# route, logged once each.
_CLI_GRACE_LOGGED: set[str] = set()


async def _authorize_cli_bearer(authorization: str | None, session_id: str, *, grace: bool = True) -> None:
    """The command-line route's bearer: the session's command-line token,
    or a session token minted before 1.9 (its session has none). 401 for no
    or an unreadable bearer and for a dead session or holder, 403 for the
    master key, a token of another session, and a session token minted by
    this release. Without ``grace`` (the exit's ask) every session token is
    refused, one from before 1.9 too."""
    from auth import token_holder
    from auth.external_endpoints import EXTERNAL_BLOCKED_DETAIL
    from auth.session_token import validate_cli_token, validate_session_token
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    if config.is_master_key(token):
        raise HTTPException(status_code=403, detail="A session token is required")
    claims = validate_cli_token(token)
    if claims is None:
        if validate_session_token(token, legacy=False) is not None:
            raise HTTPException(
                status_code=403, detail="This session's command-line token is required")
        if validate_session_token(token) is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if not grace:
            raise HTTPException(
                status_code=403, detail="This session's command-line token is required")
        await verify_session_match_async(authorization, session_id)
        if session_id not in _CLI_GRACE_LOGGED:
            _CLI_GRACE_LOGGED.add(session_id)
            logger.info("cli-credential: session %s started before 1.9 presents its "
                        "session token (taken until it expires)", session_id[:8])
        return
    sid = claims.get("sid")
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")
    if not isinstance(sid, str) or not sid or not hmac.compare_digest(
            sid.encode("utf-8", "surrogatepass"), session_id.encode("utf-8", "surrogatepass")):
        raise HTTPException(status_code=403, detail="Session token does not match request session_id")
    if session_token_refusal(claims) or not await token_holder.holder_ok(claims):
        raise HTTPException(status_code=401, detail="Invalid API key")
    # An external caller (a phone caller who is no platform user) reaches
    # only its tools' endpoints, as the middleware rules for its session
    # token; this route is none of them.
    if claims.get("ext") and not claims.get("user_sub"):
        raise HTTPException(status_code=403, detail=EXTERNAL_BLOCKED_DETAIL)


class HookSshAgentRequest(BaseModel):
    session_id: str
    # One SSH agent protocol message (the payload after the length prefix),
    # base64: the signer's 256 KB cap encoded, with the padding.
    message_b64: str = Field(max_length=350_000)


@router.post("/v1/hooks/ssh-agent")
async def hook_ssh_agent(req: HookSshAgentRequest, authorization: str | None = Header(None)):
    """The SSH agent protocol for a session on a machine: its satellite
    opens a socket for the session and posts each message a client writes
    here, with the session's token as the bearer; the answer is one message.
    The proxy signs: the one signer (``core/credentials/ssh_signer.answer``)
    judges the entitlement per message from the session's registered
    context and logs every signature, so the agent is the session's, never
    the body's. The master key is refused outright, as on the command-line
    credential's route: a signature is a session's act. Answered on the
    internal listener only (a machine's tunnel), on the session token's
    route list."""
    import base64
    import binascii

    from core.credentials import ssh_signer
    if authorization and config.is_master_key(authorization.partition(" ")[2]):
        raise HTTPException(status_code=403, detail="A session token is required")
    await verify_session_match_async(authorization, req.session_id)
    try:
        message = base64.b64decode(req.message_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="The message is not base64") from None
    if not message or len(message) > ssh_signer.MAX_MESSAGE_BYTES:
        raise HTTPException(status_code=400, detail="The message is empty or too large")
    answer = await asyncio.to_thread(ssh_signer.answer, req.session_id, message)
    return {"message_b64": base64.b64encode(answer).decode("ascii")}
