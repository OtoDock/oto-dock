"""Cloudflare Turnstile bot-protection for the login page.

Resolves the Turnstile keys (env-managed > DB > disabled), verifies a login token
against Cloudflare's ``siteverify``, and stores the admin-supplied secret encrypted.
Mirrors ``services/notifications/smtp.py`` (config resolution + a save helper + Fernet at rest).
"""

import logging
from dataclasses import dataclass

import config
from storage import database as db
from storage.identity.credential_store import _decrypt, _encrypt

logger = logging.getLogger("claude-proxy")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@dataclass
class TurnstileConfig:
    site_key: str
    secret_key: str
    managed: bool  # keys came from OTODOCK_TURNSTILE_* env (OtoDock infra)

    @property
    def enabled(self) -> bool:
        return bool(self.site_key and self.secret_key)


def is_managed() -> bool:
    """True when both keys come from env (OtoDock-managed / cloud). Cheap — no DB."""
    return bool(config.TURNSTILE_SITE_KEY and config.TURNSTILE_SECRET_KEY)


def load_config(settings: dict | None = None) -> TurnstileConfig:
    """Resolve the active Turnstile config. Precedence: env (both keys) > DB > none.

    ``settings`` may be a pre-loaded ``get_all_platform_settings()`` dict to avoid a
    second DB read; ignored when env-managed.
    """
    if is_managed():
        return TurnstileConfig(config.TURNSTILE_SITE_KEY, config.TURNSTILE_SECRET_KEY, managed=True)
    if settings is None:
        settings = db.get_all_platform_settings()
    secret = ""
    enc = settings.get("turnstile_secret_key_enc", "")
    if enc:
        try:
            secret = _decrypt(enc)
        except Exception:
            # A mismatched encryption key leaves the secret undecryptable → treat as
            # not configured (enabled=False) rather than crash; the admin GET reports
            # turnstile_secret_key_set=False so the state isn't falsely "configured".
            logger.error("Failed to decrypt Turnstile secret key")
    return TurnstileConfig(settings.get("turnstile_site_key", ""), secret, managed=False)


async def _post_siteverify(data: dict) -> dict:
    """POST to Cloudflare siteverify and return the parsed JSON. Factored out so tests
    can monkeypatch it (no real network)."""
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(SITEVERIFY_URL, data=data)
        return resp.json()


async def verify_token(cfg: TurnstileConfig, token: str, remote_ip: str | None = None) -> bool:
    """Verify a Turnstile token. Returns True to allow the login, False to reject.

    - Not configured → allow (no challenge).
    - Configured but no token → reject (the enforcement the reCAPTCHA stub never did).
    - ``success: true`` → allow; ``success: false`` (incl. replayed/expired/non-dict
      body) → reject.
    - siteverify unreachable / non-JSON body → allow (fail-open: Cloudflare being down
      must not lock everyone out). ``remote_ip`` is intentionally NOT sent — it risks
      false rejects behind reverse proxies/CDNs whose observed IP differs from the edge
      that issued the token.
    """
    if not cfg.enabled:
        return True
    if not token:
        return False
    try:
        result = await _post_siteverify({"secret": cfg.secret_key, "response": token})
    except Exception:
        logger.warning("Turnstile siteverify unreachable/bad response — allowing login (fail-open)")
        return True
    if not isinstance(result, dict) or not result.get("success"):
        logger.info(
            "Turnstile rejected token: %s",
            result.get("error-codes") if isinstance(result, dict) else result,
        )
        return False
    return True


def save_keys(site_key: str | None, secret_key: str | None) -> None:
    """Persist admin-supplied Turnstile keys (secret Fernet-encrypted). No-op when
    env-managed — the operator owns the keys then."""
    if is_managed():
        return
    if site_key is not None:
        db.set_platform_setting("turnstile_site_key", site_key.strip())
    if secret_key:  # only overwrite when a new non-empty secret is provided
        db.set_platform_setting("turnstile_secret_key_enc", _encrypt(secret_key.strip()))
