"""Generic OIDC authentication provider.

Replaces the Authentik-specific code with a generic OIDC implementation
that works with any OIDC provider (Authentik, Authelia, Keycloak, Okta, etc.).
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import urlsplit

import httpx
import jwt

import config
from auth.providers.base import AuthProvider, AuthResult
from auth import roles

logger = logging.getLogger("claude-proxy")

# Lazy re-discovery guard — module-level so it is shared across all callers
# and provider instances. `at` is the monotonic time of the LAST attempt
# (success or failure); attempts are spaced at least the interval apart so a
# down IdP is neither hammered nor log-spammed on every login click.
_DISCOVERY_RETRY_INTERVAL_S = 30
_discovery_guard = {"at": 0.0}
_discovery_lock = asyncio.Lock()


def _discovery_needed() -> bool:
    if not (config.OIDC_ENABLED and config.OIDC_DISCOVERY_URL):
        return False
    # LOGOUT_URL is deliberately excluded: many IdPs omit end_session_endpoint
    # and get_logout_url degrades gracefully — requiring it here would retry
    # forever against such providers. The JWKS and the issuer are needed:
    # no ID token is trusted without them.
    return not (config.OIDC_AUTHORIZE_URL and config.OIDC_TOKEN_URL
                and config.OIDC_USERINFO_URL and config.OIDC_JWKS_URL
                and config.OIDC_ISSUER)


async def ensure_oidc_discovery() -> None:
    """Re-attempt OIDC endpoint discovery when the boot-time fetch failed
    (e.g. proxy and a co-hosted IdP racing up after a power cut). Fast path
    is a bare attribute check; on success the config globals are populated
    via config.apply_oidc_discovery (explicit env vars still win)."""
    if not _discovery_needed():
        return
    async with _discovery_lock:
        if not _discovery_needed():  # a concurrent caller just recovered it
            return
        now = time.monotonic()
        if now - _discovery_guard["at"] < _DISCOVERY_RETRY_INTERVAL_S:
            return
        _discovery_guard["at"] = now
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    config.OIDC_DISCOVERY_URL,
                    headers={"User-Agent": config.OIDC_DISCOVERY_USER_AGENT},
                )
                resp.raise_for_status()
                meta = resp.json()
        except Exception as e:
            # The guard timestamp already advanced — a down IdP logs one line
            # per retry window, not one per click.
            logger.warning(
                f"OIDC discovery retry failed for {config.OIDC_DISCOVERY_URL}: {e}"
            )
            return
        config.apply_oidc_discovery(meta)
        logger.info(f"OIDC discovery recovered: authorize={config.OIDC_AUTHORIZE_URL}")


# --- the ID token -------------------------------------------------------------

#: The signing algorithms accepted when the provider announces none.
_DEFAULT_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                 "ES256", "ES384", "ES512", "HS256")
_JWKS_TTL_S = 600
_JWKS_REFETCH_S = 30
_LEEWAY_S = 60
_jwks: dict = {"url": "", "keys": [], "at": 0.0}
_derived: dict = {"iss": "", "jwks": ""}


class OIDCRefused(Exception):
    """A sign-in the ID token's checks refuse: ``message`` is for the
    person, ``code`` for the log and the callback's status."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


async def _get_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(url, headers={"User-Agent": config.OIDC_DISCOVERY_USER_AGENT})
        resp.raise_for_status()
        doc = resp.json()
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


async def _issuer_and_jwks(token_iss: str) -> tuple[str, str]:
    """The issuer the ID token must name and the JWKS its keys come from:
    configured or discovered; for an install with explicit endpoint URLs
    and no JWKS URL, the issuer's own discovery document when it names the
    same issuer: the configured ``OIDC_ISSUER``, or the token's issuer when
    it is on the token endpoint's origin (a provider whose issuer sits on
    another origin needs ``OIDC_ISSUER``)."""
    await ensure_oidc_discovery()
    issuer, jwks = config.OIDC_ISSUER, config.OIDC_JWKS_URL
    if issuer and jwks:
        return issuer, jwks
    if issuer and token_iss != issuer:
        raise OIDCRefused("The sign-in came from another identity provider.", "wrong_issuer")
    if _derived["iss"] and _derived["iss"] == token_iss:
        return token_iss, jwks or _derived["jwks"]
    if not token_iss or (not issuer and _origin(token_iss) != _origin(config.OIDC_TOKEN_URL)):
        raise OIDCRefused(
            "The identity provider's issuer is unknown here: set OIDC_DISCOVERY_URL "
            "(or OIDC_ISSUER and OIDC_JWKS_URL) in config.env.", "issuer_unknown")
    try:
        meta = await _get_json(token_iss.rstrip("/") + "/.well-known/openid-configuration")
    except Exception as e:
        logger.warning(f"OIDC: discovery at the token's issuer failed: {e}")
        raise OIDCRefused("The identity provider's signing keys could not be read.",
                          "jwks_unavailable") from None
    if meta.get("issuer") != token_iss or not (jwks or meta.get("jwks_uri")):
        raise OIDCRefused("The identity provider's issuer is unknown here: set OIDC_ISSUER "
                          "and OIDC_JWKS_URL in config.env.", "issuer_unknown")
    _derived.update(iss=token_iss, jwks=jwks or meta["jwks_uri"])
    return token_iss, _derived["jwks"]


async def _jwks_keys(url: str, *, refresh: bool) -> list[dict]:
    now = time.monotonic()
    fresh = _jwks["url"] == url and now - _jwks["at"] < _JWKS_TTL_S
    if fresh and not (refresh and now - _jwks["at"] >= _JWKS_REFETCH_S):
        return _jwks["keys"]
    try:
        doc = await _get_json(url)
    except Exception as e:
        logger.warning(f"OIDC: the JWKS at {url} could not be read: {e}")
        if _jwks["url"] == url and _jwks["keys"]:
            return _jwks["keys"]
        raise OIDCRefused("The identity provider's signing keys could not be read.",
                          "jwks_unavailable") from None
    keys = [k for k in doc.get("keys") or [] if isinstance(k, dict)]
    _jwks.update(url=url, keys=keys, at=now)
    return keys


async def _signing_key(url: str, kid: str | None, alg: str):
    """The public key that signed the token: by ``kid``, or the only key
    for the algorithm; an unknown ``kid`` fetches the set again once (a
    rotation), at most every ``_JWKS_REFETCH_S``."""
    for refresh in (False, True):
        keys = await _jwks_keys(url, refresh=refresh)
        if kid:
            match = [k for k in keys if k.get("kid") == kid]
        else:
            match = [k for k in keys if k.get("alg") in (None, alg) and k.get("use") in (None, "sig")]
            match = match if len(match) == 1 else []
        if match:
            try:
                return jwt.PyJWK(match[0], algorithm=alg).key
            except jwt.PyJWTError as e:
                logger.warning(f"OIDC: an unusable signing key ({e})")
                break
    raise OIDCRefused("The sign-in was signed with a key the identity provider does not publish.",
                      "bad_signature")


async def verify_id_token(id_token: str, *, nonce: str) -> dict:
    """The ID token's claims, after its signature (the provider's JWKS, or
    the client secret for HS256), issuer, audience, expiry and nonce are
    checked. Raises ``OIDCRefused`` naming what failed."""
    try:
        header = jwt.get_unverified_header(id_token)
        # A pre-read of the claims (the issuer picks the JWKS) before the
        # verified decode below, which checks the signature, issuer,
        # audience and expiry; nothing is trusted from this one (reviewed
        # 2026-10-07).
        unverified = jwt.decode(id_token, options={"verify_signature": False})  # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
    except jwt.PyJWTError:
        raise OIDCRefused("The identity provider sent an unreadable ID token.", "bad_id_token") from None
    alg = str(header.get("alg") or "")
    allowed = config.OIDC_ID_TOKEN_ALGS or list(_DEFAULT_ALGS)
    if alg.lower() == "none" or alg not in allowed:
        raise OIDCRefused("The ID token's signing algorithm is not accepted.", "bad_signature")
    issuer, jwks_url = await _issuer_and_jwks(str(unverified.get("iss") or ""))
    if alg.startswith("HS"):
        key = config.OIDC_CLIENT_SECRET
        if not key:
            raise OIDCRefused("The ID token is signed with the client secret, which is not set.",
                              "bad_signature")
    else:
        key = await _signing_key(jwks_url, header.get("kid"), alg)
    try:
        claims = jwt.decode(
            id_token, key, algorithms=[alg], audience=config.OIDC_CLIENT_ID, issuer=issuer,
            leeway=_LEEWAY_S, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.InvalidSignatureError:
        raise OIDCRefused("The sign-in's signature did not verify.", "bad_signature") from None
    except jwt.ExpiredSignatureError:
        raise OIDCRefused("The sign-in had expired; try again.", "expired") from None
    except jwt.InvalidAudienceError:
        raise OIDCRefused("The sign-in was not for this platform.", "wrong_audience") from None
    except jwt.InvalidIssuerError:
        raise OIDCRefused("The sign-in came from another identity provider.", "wrong_issuer") from None
    except jwt.PyJWTError as e:
        raise OIDCRefused(f"The ID token was refused ({e}).", "bad_id_token") from None
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != config.OIDC_CLIENT_ID:
        raise OIDCRefused("The sign-in was not for this platform.", "wrong_audience")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise OIDCRefused("The sign-in did not answer this request; start it again.", "bad_nonce")
    return claims


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _truthy(value) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


class OIDCAuthProvider(AuthProvider):
    """OpenID Connect authentication provider."""

    async def authenticate(self, request_data: dict) -> AuthResult:
        """Authenticate via OIDC code exchange.

        request_data: {"code": str, "redirect_uri": str | None, "nonce": str,
        "code_verifier": str} (the last two from the server-side state).
        Nothing the provider says is trusted before its ID token is
        verified (``verify_id_token``) and names the userinfo's account.
        """
        code = request_data.get("code", "")
        redirect_uri = request_data.get("redirect_uri")

        # Covers the token/userinfo leg when boot-time discovery failed but a
        # callback still arrives (authorize URL set explicitly, or recovery
        # happened between the login click and the callback).
        await ensure_oidc_discovery()

        # Exchange code for tokens
        try:
            tokens = await self._exchange_code(code, redirect_uri,
                                               request_data.get("code_verifier", ""))
        except httpx.HTTPStatusError as e:
            logger.error(f"OIDC token exchange failed: {e}")
            reason = ""
            try:
                body = e.response.json()
                reason = str(body.get("error_description") or body.get("error") or "")[:200]
            except Exception:
                pass
            return AuthResult(success=False, error_code="token_exchange_failed",
                              error=(f"The identity provider refused the sign-in ({reason})."
                                     if reason else "Authentication failed"))
        except Exception as e:
            logger.error(f"OIDC token exchange error: {e}")
            return AuthResult(success=False, error="Authentication failed",
                              error_code="token_exchange_failed")

        access_token = tokens.get("access_token")
        if not access_token:
            return AuthResult(success=False, error="No access token received",
                              error_code="no_access_token")
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            logger.warning("OIDC sign-in refused: the provider sent no ID token")
            return AuthResult(success=False, error_code="no_id_token",
                              error="The identity provider sent no ID token (is the openid scope allowed?).")
        try:
            claims = await verify_id_token(id_token, nonce=request_data.get("nonce", ""))
        except OIDCRefused as e:
            logger.warning(f"OIDC sign-in refused ({e.code}): {e.message}")
            return AuthResult(success=False, error=e.message, error_code=e.code)

        # Fetch user info
        try:
            userinfo = await self._fetch_userinfo(access_token)
        except Exception as e:
            logger.error(f"OIDC userinfo fetch failed: {e}")
            return AuthResult(success=False, error="Failed to fetch user info",
                              error_code="userinfo_failed")

        sub = userinfo.get("sub", "")
        if not sub or sub != claims.get("sub"):
            logger.warning("OIDC sign-in refused (sub_mismatch): userinfo names another account")
            return AuthResult(success=False, error_code="sub_mismatch",
                              error="The identity provider answered for another account.")
        email = userinfo.get("email", "")
        name = userinfo.get("preferred_username") or userinfo.get("name", email)
        display_name = userinfo.get("name") or ""
        if not display_name:
            given = userinfo.get("given_name", "")
            family = userinfo.get("family_name", "")
            display_name = f"{given} {family}".strip()

        groups = userinfo.get("groups", [])
        role = self._extract_role(groups)
        if not role:
            return AuthResult(
                success=False,
                error=f"Access denied: not a member of any configured group. "
                      f"Expected one of: {', '.join(config.OIDC_ROLE_GROUPS.keys())}",
                error_code="no_group",
            )

        provider_slug = config.OIDC_PROVIDER_NAME.lower().replace(" ", "-")
        return AuthResult(
            success=True,
            sub=sub,
            email=email,
            name=name,
            display_name=display_name,
            role=role,
            auth_provider=f"oidc:{provider_slug}",
            id_claims=claims,
            email_verified=_truthy(userinfo.get("email_verified", claims.get("email_verified"))),
        )

    def get_login_url(self, *, redirect_uri: str | None = None,
                      mobile: bool = False, prompt_login: bool = False,
                      purpose: str = "login", sub: str = "",
                      return_to: str = "", client: str = "",
                      client_capped: bool = True) -> str | None:
        """Build OIDC authorization URL. ``prompt_login`` asks the provider
        for a new login (the strict confirm, ``OIDC_CONFIRM_FRESH_LOGIN``):
        ``prompt=login`` requests it and ``max_age=0`` requires it per the
        spec, which then makes ``auth_time`` mandatory in the ID token.
        ``client`` and ``client_capped``: the state store's per-client bound
        (``create_oauth_state``)."""
        if not config.OIDC_AUTHORIZE_URL or not config.OIDC_CLIENT_ID:
            return None

        from urllib.parse import urlencode

        from auth.providers import create_oauth_state
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        state = create_oauth_state(redirect_uri=redirect_uri, purpose=purpose, sub=sub,
                                   return_to=return_to, nonce=nonce, code_verifier=verifier,
                                   client=client, client_capped=client_capped)

        actual_redirect = redirect_uri or config.OIDC_REDIRECT_URI
        params = {
            "response_type": "code",
            "client_id": config.OIDC_CLIENT_ID,
            "redirect_uri": actual_redirect,
            "scope": config.OIDC_SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if prompt_login:
            params["prompt"] = "login"
            params["max_age"] = "0"
        return f"{config.OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    def get_logout_url(self, post_redirect: str | None = None) -> str | None:
        """Return OIDC provider logout URL."""
        url = config.OIDC_LOGOUT_URL
        if not url:
            return None
        if post_redirect:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}post_logout_redirect_uri={post_redirect}"
        return url

    async def _exchange_code(self, code: str, redirect_uri: str | None = None,
                             code_verifier: str = "") -> dict:
        """Exchange authorization code for tokens (with the PKCE verifier
        the authorization request's challenge was made from)."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                config.OIDC_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri or config.OIDC_REDIRECT_URI,
                    "client_id": config.OIDC_CLIENT_ID,
                    "client_secret": config.OIDC_CLIENT_SECRET,
                    "code_verifier": code_verifier,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def _fetch_userinfo(self, access_token: str) -> dict:
        """Fetch user profile from OIDC userinfo endpoint."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                config.OIDC_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _extract_role(groups: list[str]) -> str | None:
        """Map OIDC groups to role. The strongest platform role wins."""
        return roles.highest_platform_role(config.OIDC_ROLE_GROUPS.get(group) for group in groups)
