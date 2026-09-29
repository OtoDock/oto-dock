"""Signed claims for apps (APPS.md "Viewer identity"): the proxy mints, an
app may verify, nobody else can mint.

One Ed25519 key per app row, derived from the platform's JWT secret with
HKDF: no key store, stable across restarts (a viewer token outlives a
restart), and never shared — an app receives only its own PUBLIC key
(``OTODOCK_APP_PUBLIC_KEY``, the raw 32 bytes, base64url). Every claim
carries ``purpose`` (``app_viewer`` | ``app_launch`` | ``app_caller``),
``aud: app:<row id>`` and ``exp``; the session validators reject these
tokens by construction (a different algorithm, no ``type`` / ``purpose:
session``), and ``verify`` rejects any session token in return.
"""

from __future__ import annotations

import base64
import time

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import config

PURPOSE_VIEWER = "app_viewer"
PURPOSE_LAUNCH = "app_launch"
PURPOSE_CALLER = "app_caller"
PURPOSES = frozenset({PURPOSE_VIEWER, PURPOSE_LAUNCH, PURPOSE_CALLER})

# A caller claim's ``principal`` for an agent's session calling an app
# (APPS.md "Agents call apps"), and for a handler's wake in flight — a word
# of the claim, not the agent scope it shares a spelling with.
PRINCIPAL_AGENT = "agent"
PRINCIPAL_PLATFORM = "platform"

VIEWER_TTL_S = 10 * 60
CALLER_TTL_S = 60
LAUNCH_TTL_S = 30 * 24 * 3600

_keys: dict[str, ed25519.Ed25519PrivateKey] = {}


def _private_key(app_id: str) -> ed25519.Ed25519PrivateKey:
    key = _keys.get(app_id)
    if key is None:
        seed = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"otodock-app-keys",
            info=f"app-ed25519:{app_id}".encode("utf-8"),
        ).derive(config.JWT_SECRET.encode("utf-8"))
        key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        _keys[app_id] = key
    return key


def _private_pem(app_id: str) -> str:
    return _private_key(app_id).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _public_pem(app_id: str) -> str:
    return _private_key(app_id).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def public_key_b64(app_id: str) -> str:
    """The raw public key, base64url without padding — what the app's env
    carries and what WebCrypto's ``importKey("raw", …, "Ed25519")`` takes."""
    raw = _private_key(app_id).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def audience(app_id: str) -> str:
    return f"app:{app_id}"


def mint(app_id: str, purpose: str, claims: dict, ttl_s: int) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    now = int(time.time())
    payload = {**claims, "purpose": purpose, "aud": audience(app_id),
               "iat": now, "exp": now + int(ttl_s)}
    return jwt.encode(payload, _private_pem(app_id), algorithm="EdDSA")


def verify(token: str, app_id: str, purpose: str) -> dict | None:
    """The claims iff ``token`` was minted for this app and this purpose and
    has not expired; None otherwise (never raises on a bad token)."""
    if not token or purpose not in PURPOSES:
        return None
    try:
        payload = jwt.decode(
            token, _public_pem(app_id), algorithms=["EdDSA"], audience=audience(app_id),
        )
    except jwt.InvalidTokenError:
        return None
    if payload.get("purpose") != purpose:
        return None
    return payload


def peek_app_id(token: str) -> str:
    """The app id a token names in its audience, unverified — the routes
    already know the app from the path; this is for the caller resolver's
    early refusal of a token minted for another app."""
    try:
        # Unverified on purpose: the name only routes the token to the app
        # whose key then verifies it (``verify`` below). Nothing is granted
        # here, and a forged audience only picks the key that refuses it.
        aud = jwt.decode(
            token,
            options={"verify_signature": False},  # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        ).get("aud") or ""
    except jwt.InvalidTokenError:
        return ""
    return aud[4:] if isinstance(aud, str) and aud.startswith("app:") else ""
