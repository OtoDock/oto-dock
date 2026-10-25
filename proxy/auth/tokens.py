"""Every token the platform signs for itself: one kind per purpose, a key
per kind, an audience per kind.

A kind (``Kind``) is one purpose: the agent session's bearer, the session's
command-line token, the dashboard sign-in cookie, the 2FA step, a reset
link, a WOPI token, ... Each kind signs with its own key, derived from the
platform secret with HKDF (``_key``), and names itself in ``aud``
(``otodock:<kind>``): a token of one kind never verifies as another,
whatever its caller checks afterwards, and a decode anywhere that forgets
the audience fails on these tokens too (PyJWT refuses an ``aud`` nobody
asked for).

Tokens in flight at the upgrade to 1.9 were signed with the raw secret and
carry no ``aud``. ``decode`` accepts such a token for the kind whose claim
it carries (``Kind.legacy``: the ``type`` or ``purpose`` the kind's 1.8
verifier checked, exactly that check), until its own expiry: a sign-in, a
remote session re-adopted after the restart, an open document, an invite
keep working. A token minted here never passes that branch (it carries
``aud``), and a legacy token of one kind never passes as another (its
claim is checked). ``legacy=False`` answers "minted by this release".

``services/apps/app_tokens.py`` (an app's own Ed25519 key) and the
vendors' tokens (an OAuth ID token, GitHub's app JWT) are not platform
tokens of this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import config

_ALGORITHM = "HS256"
_KEY_SALT = b"otodock-token-keys"


@dataclass(frozen=True)
class Kind:
    """One purpose. ``legacy`` is the claim and value a 1.8 verifier of this
    purpose checked (None for a kind 1.8 never minted); ``secret`` names the
    setting the kind's key derives from."""

    name: str
    legacy: tuple[str, str] | None
    secret: str = "JWT_SECRET"

    @property
    def audience(self) -> str:
        return f"otodock:{self.name}"


SESSION = Kind("session", ("type", "session"))
CLI = Kind("cli", None)
SIGN_IN = Kind("sign_in", ("purpose", "session"))
TWO_FACTOR = Kind("two_factor", ("purpose", "2fa"))
PASSWORD_RESET = Kind("password_reset", ("purpose", "password_reset"))
INVITE = Kind("invite", ("purpose", "invite"))
TRUSTED_DEVICE = Kind("trusted_device", ("purpose", "trusted_device"))
SHARE_UNLOCK = Kind("share_unlock", ("purpose", "share_unlock"))
SHARE_APP_SESSION = Kind("share_app_session", ("purpose", "app_session"))
ZIP_DOWNLOAD = Kind("zip_download", ("purpose", "zip_download"))
WOPI = Kind("wopi", ("purpose", "wopi"), secret="WOPI_SECRET")
AUDIO_STT = Kind("audio_stt", ("purpose", "audio_stt"))
AUDIO_TTS = Kind("audio_tts", ("purpose", "audio_tts"))
DUPLEX = Kind("duplex", ("purpose", "duplex_session"))
MCP_CREDENTIALS = Kind("mcp_credentials", ("type", "mcp_cred"))
SESSION_FILES = Kind("session_files", ("type", "session_files"))
RENDER = Kind("render", ("purpose", "app_render"))

KINDS: tuple[Kind, ...] = (
    SESSION, CLI, SIGN_IN, TWO_FACTOR, PASSWORD_RESET, INVITE, TRUSTED_DEVICE,
    SHARE_UNLOCK, SHARE_APP_SESSION, ZIP_DOWNLOAD, WOPI, AUDIO_STT, AUDIO_TTS,
    DUPLEX, MCP_CREDENTIALS, SESSION_FILES, RENDER,
)


class TokenInvalid(Exception):
    """Not a live token of the kind asked for."""


class TokenExpired(TokenInvalid):
    """A token of the kind asked for, past its ``exp``."""


_keys: dict[tuple[str, str], bytes] = {}


def _secret(kind: Kind) -> str:
    return str(getattr(config, kind.secret))


def _key(kind: Kind) -> bytes:
    secret = _secret(kind)
    cache_key = (kind.name, secret)
    key = _keys.get(cache_key)
    if key is None:
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=_KEY_SALT,
            info=f"token:{kind.name}".encode(),
        ).derive(secret.encode("utf-8"))
        _keys[cache_key] = key
    return key


def mint(kind: Kind, claims: dict[str, Any], *, ttl_s: int | None = None,
         now: int | None = None) -> str:
    """A token of ``kind`` carrying ``claims``. With ``ttl_s`` it is issued
    at ``now`` (``iat``, unless the claims give one) and expires ``ttl_s``
    later; without, the claims carry ``exp`` themselves. Every platform
    token expires: a payload with no ``exp`` is refused."""
    payload = dict(claims)
    if ttl_s is not None:
        issued = int(time.time()) if now is None else int(now)
        payload.setdefault("iat", issued)
        payload["exp"] = issued + int(ttl_s)
    if "exp" not in payload:
        raise ValueError(f"a {kind.name} token must expire")
    payload["aud"] = kind.audience
    return jwt.encode(payload, _key(kind), algorithm=_ALGORITHM)


def decode(token: object, kind: Kind, *, verify_exp: bool = True,
           legacy: bool = True) -> dict[str, Any]:
    """The claims of a token of ``kind``. Raises ``TokenExpired`` for one
    past its ``exp`` (when ``verify_exp``), ``TokenInvalid`` for anything
    else: another kind, another key, a malformed string. ``legacy`` also
    takes a token signed before 1.9 for this kind (the module's note)."""
    if not isinstance(token, str) or not token:
        raise TokenInvalid("no token")
    try:
        return dict(jwt.decode(
            token, _key(kind), algorithms=[_ALGORITHM], audience=kind.audience,
            options={"verify_exp": verify_exp},
        ))
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired(kind.name) from exc
    except jwt.InvalidTokenError as exc:
        if not legacy or kind.legacy is None:
            raise TokenInvalid(kind.name) from exc
    return _decode_legacy(token, kind, verify_exp=verify_exp)


def _decode_legacy(token: str, kind: Kind, *, verify_exp: bool) -> dict[str, Any]:
    """The 1.8 shape: the raw secret, no ``aud``, the kind's claim and an
    ``exp`` (every 1.8 mint set one: a token without is refused, never taken
    for ever). The claim is judged before the expiry, so an expired token of
    another kind reads as invalid, never as this kind's expired one."""
    if kind.legacy is None:
        raise TokenInvalid(kind.name)
    claim, value = kind.legacy
    try:
        payload = dict(jwt.decode(
            token, _secret(kind), algorithms=[_ALGORITHM],
            options={"verify_exp": False, "verify_aud": False, "require": ["exp"]},
        ))
    except jwt.InvalidTokenError as exc:
        raise TokenInvalid(kind.name) from exc
    if "aud" in payload or payload.get(claim) != value:
        raise TokenInvalid(kind.name)
    if verify_exp:
        try:
            jwt.decode(token, _secret(kind), algorithms=[_ALGORITHM],
                       options={"verify_aud": False})
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpired(kind.name) from exc
        except jwt.InvalidTokenError as exc:
            raise TokenInvalid(kind.name) from exc
    return payload


def verify(token: object, kind: Kind, *, verify_exp: bool = True,
           legacy: bool = True) -> dict[str, Any] | None:
    """``decode`` that answers None instead of raising."""
    try:
        return decode(token, kind, verify_exp=verify_exp, legacy=legacy)
    except TokenInvalid:
        return None
