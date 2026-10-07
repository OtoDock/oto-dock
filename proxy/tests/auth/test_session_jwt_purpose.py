"""Regression tests for the session-cookie JWT `purpose` discriminator.

Guards against the 2FA / password-reset bypass: the 2FA step token and the
password-reset token are signed with the same JWT_SECRET as the session cookie,
so without a `purpose` check either could be replayed as the `session` cookie
for a full authenticated session. See auth/providers.validate_session_jwt.
"""

import time

import jwt as pyjwt

import config
from auth.providers import create_session_jwt, validate_session_jwt
from auth.totp import create_2fa_session_token


def test_characters_after_a_valid_signature_are_refused():
    # A token is valid only in its exact compact form: PyJWT before 2.14
    # dropped characters outside the base64url alphabet from the signature
    # segment, so a token with junk appended still verified. The floor in
    # requirements.in keeps that closed for both session schemes.
    from auth.session_token import create_session_token, validate_session_token
    cookie = create_session_jwt("local:u1", "u@x.com", "User", "member")
    bearer = create_session_token("sess-1", "agent-1")
    assert validate_session_jwt(cookie) is not None
    assert validate_session_token(bearer) is not None
    for junk in ("!!", "**", "%%%"):
        assert validate_session_jwt(cookie + junk) is None, junk
        assert validate_session_token(bearer + junk) is None, junk


def test_real_session_cookie_validates():
    tok = create_session_jwt("local:u1", "u@x.com", "User", "member")
    payload = validate_session_jwt(tok)
    assert payload is not None
    assert payload["sub"] == "local:u1"
    assert payload["purpose"] == "session"


def test_2fa_token_rejected_as_session_cookie():
    # Handed to the client after ONLY the password — must never authenticate.
    tok = create_2fa_session_token("local:victim")
    assert validate_session_jwt(tok) is None


def test_password_reset_token_rejected_as_session_cookie():
    tok = pyjwt.encode(
        {"sub": "local:victim", "purpose": "password_reset",
         "exp": int(time.time()) + 3600},
        config.JWT_SECRET, algorithm="HS256",
    )
    assert validate_session_jwt(tok) is None


def test_purposeless_token_rejected_as_session_cookie():
    # A legacy/forged token with no purpose must not pass.
    tok = pyjwt.encode(
        {"sub": "local:victim", "exp": int(time.time()) + 3600},
        config.JWT_SECRET, algorithm="HS256",
    )
    assert validate_session_jwt(tok) is None
