"""GitHub App: the app's own half of the ``github`` provider.

The person's half (the consent page, the user token, its refresh) is the
manifest-built generic provider. This module is what the APP does with its
private key: the app JWT, installation access tokens, the installations a
person can reach and what they may do with each, and the confirmations the
app webhook's lifecycle events need. Synchronous over ``httpx.Client``: a
caller on the event loop runs it in a thread.

The three token kinds (docs.github.com, "Authenticating with a GitHub App"):
the app JWT (RS256, ten minutes at most, the issuer is the client id or the
app id), an installation access token (``POST
/app/installations/{id}/access_tokens``, about one hour, narrowed by the
installation's repositories and permissions), a user access token (the web
flow, eight hours with a six-month refresh token).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.oauth_providers.base import INVALID_GRANT, OAuthTokenError, TokenSet
from storage.identity import oauth_app_kinds as kinds

logger = logging.getLogger("otodock.oauth-providers.github-app")

PROVIDER_ID = "github"
API_BASE = "https://api.github.com"
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_TIMEOUT_S = 15.0
# The JWT: issued a minute in the past against clock drift (GitHub's own
# advice), good for nine minutes, renewed a minute before its end.
_JWT_BACKDATE_S = 60
_JWT_LIFE_S = 540
_JWT_RENEW_BEFORE_S = 60
_PAGE = 100
_MAX_INSTALLATION_PAGES = 5
_MAX_REPOSITORY_PAGES = 2

# The bundle keys the slug and the name are written under after a save.
APP_SLUG_KEY = "GITHUB_APP_SLUG"
APP_NAME_KEY = "GITHUB_APP_NAME"
# GitHub's organization membership vocabulary: the role that owns the
# organization and the state of a membership in force.
_ORG_OWNER_ROLE = "admin"
_MEMBERSHIP_ACTIVE = "active"

_PEM_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z ]+)-----(?P<body>.*?)-----END (?P=label)-----", re.S,
)


@dataclass
class GitHubApp:
    """An admin's GitHub App as the bundle holds it."""

    app_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    private_key_pem: str = ""
    webhook_secret: str = ""
    slug: str = ""
    name: str = ""

    @property
    def issuer(self) -> str:
        return self.client_id or self.app_id


@dataclass
class UserInstallations:
    """What ``GET /user/installations`` says for one person: who they are
    on GitHub and the installations of this app they can reach."""

    login: str = ""
    user_id: int = 0
    installations: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------

def app_from_bundle(creds: dict[str, str], fields: list[dict[str, Any]]) -> GitHubApp:
    """The app the admin's bundle holds, read through the GitHub-App kind's
    declared fields only (the OAuth-App kind's keys share the bundle)."""
    app = GitHubApp(slug=str(creds.get(APP_SLUG_KEY) or ""), name=str(creds.get(APP_NAME_KEY) or ""))
    for f in fields:
        key = str(f.get("key") or "")
        attr = kinds.app_field_role(key)
        if attr and key in creds:
            value = str(creds.get(key) or "")
            setattr(app, attr, value if attr == kinds.FIELD_PRIVATE_KEY else value.strip())
    return app


def normalize_pem(text: str) -> str:
    """The private key as a well-formed PEM: a key pasted into a single-line
    input loses its newlines, so the header, the base64 body and the footer
    are found by regex and the body re-wrapped at 64 columns. The result must
    be an RSA key (PKCS#1 or PKCS#8), else ``ValueError``."""
    m = _PEM_RE.search(text or "")
    if m is None:
        raise ValueError("not an RSA private key in PEM form")
    label = m.group("label").strip()
    body = re.sub(r"\s+", "", m.group("body"))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    pem = f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise ValueError("not an RSA private key in PEM form") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("not an RSA private key in PEM form")
    return pem


# ---------------------------------------------------------------------------
# The app JWT
# ---------------------------------------------------------------------------

_jwt_lock = threading.Lock()
_jwt_cache: dict[tuple[str, str, str], tuple[str, float]] = {}


def _key_fingerprint(pem: str) -> str:
    return hashlib.sha256(pem.encode("utf-8")).hexdigest()


def app_jwt(app: GitHubApp) -> str:
    """The app's JWT, cached per app, issuer and key until a minute before
    its end."""
    cache_key = (app.app_id, app.issuer, _key_fingerprint(app.private_key_pem))
    now = time.time()
    with _jwt_lock:
        hit = _jwt_cache.get(cache_key)
        if hit is not None and hit[1] - _JWT_RENEW_BEFORE_S > now:
            return hit[0]
    expires = int(now) + _JWT_LIFE_S
    claims = {"iat": int(now) - _JWT_BACKDATE_S, "exp": expires, "iss": app.issuer}
    try:
        token = jwt.encode(claims, app.private_key_pem, algorithm="RS256")
    except (ValueError, TypeError, jwt.exceptions.PyJWTError) as exc:
        raise ValueError("not an RSA private key in PEM form") from exc
    with _jwt_lock:
        _jwt_cache[cache_key] = (token, float(expires))
    return token


def forget_jwts() -> None:
    """Drop every cached JWT (an admin saved new credentials)."""
    with _jwt_lock:
        _jwt_cache.clear()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _client() -> httpx.Client:
    """The client every call here uses (tests replace it)."""
    return httpx.Client(timeout=_TIMEOUT_S, headers=_API_HEADERS)


def _request(action: str, method: str, path: str, *, token: str, **kw: Any) -> httpx.Response:
    try:
        with _client() as client:
            return client.request(
                method, f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, **kw,
            )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise OAuthTokenError(PROVIDER_ID, action, "unreachable", str(exc)) from exc


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _message(resp: httpx.Response) -> str:
    payload = _json(resp)
    return str(payload.get("message") or "") if isinstance(payload, dict) else ""


def _refused(action: str, resp: httpx.Response) -> OAuthTokenError:
    """The error of an answer that is not the one expected."""
    message = _message(resp)
    if resp.status_code == 401:
        code = kinds.APP_KEY_REJECTED
    elif resp.status_code == 404:
        code = kinds.INSTALLATION_GONE
    elif resp.status_code == 403 and "suspend" in message.lower():
        code = kinds.INSTALLATION_SUSPENDED
    else:
        code = f"http_{resp.status_code}"
    return OAuthTokenError(PROVIDER_ID, action, code, message, resp.status_code)


# ---------------------------------------------------------------------------
# As the app (the JWT)
# ---------------------------------------------------------------------------

def get_app(app: GitHubApp) -> dict[str, Any]:
    """``GET /app``: the app the key signs for."""
    resp = _request("app", "GET", "/app", token=app_jwt(app))
    payload = _json(resp)
    if resp.status_code != 200 or not isinstance(payload, dict):
        raise _refused("app", resp)
    return payload


def get_installation(app: GitHubApp, installation_id: int) -> dict[str, Any] | None:
    """``GET /app/installations/{id}``: the installation as GitHub holds it
    now, ``None`` when it is gone (the lifecycle's confirmation)."""
    resp = _request("installation", "GET", f"/app/installations/{int(installation_id)}", token=app_jwt(app))
    if resp.status_code == 404:
        return None
    payload = _json(resp)
    if resp.status_code != 200 or not isinstance(payload, dict):
        raise _refused("installation", resp)
    return payload


def verify_app_credentials(creds: dict[str, str], fields: list[dict[str, Any]]) -> dict[str, str]:
    """An admin's GitHub-App bundle as it is about to be stored: the PEM
    normalized, the JWT minted, ``GET /app`` answered by the app whose id
    the admin entered. Returns the bundle with the normalized key, the slug
    and the name; ``ValueError`` with a plain sentence otherwise."""
    app = app_from_bundle(creds, fields)
    if not app.app_id or not app.private_key_pem:
        raise ValueError("A GitHub App needs its App ID and its private key")
    pem = normalize_pem(app.private_key_pem)
    app.private_key_pem = pem
    try:
        info = get_app(app)
    except OAuthTokenError as exc:
        if exc.code == kinds.APP_KEY_REJECTED:
            raise ValueError(
                "GitHub refused the app's key: check the App ID and the private key",
            ) from exc
        if exc.code == "unreachable":
            raise ValueError("GitHub could not be reached to verify the app") from exc
        raise ValueError(f"GitHub answered {exc.status} to the app's own request") from exc
    if str(info.get("id") or "") != app.app_id:
        raise ValueError(
            f"The key belongs to another app (GitHub says {info.get('id')}, the form says {app.app_id})",
        )
    out = dict(creds)
    for f in fields:
        key = str(f.get("key") or "")
        if kinds.app_field_role(key) == kinds.FIELD_PRIVATE_KEY and key in out:
            out[key] = pem
    out[APP_SLUG_KEY] = str(info.get("slug") or "")
    out[APP_NAME_KEY] = str(info.get("name") or "")
    return out


def _parse_expiry(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def mint_installation_token(
    app: GitHubApp, installation_id: int, *,
    repositories: list[str] | None = None, permissions: dict[str, str] | None = None,
) -> TokenSet:
    """An installation access token (``POST
    /app/installations/{id}/access_tokens``), narrowed to ``repositories``
    and ``permissions`` when given, else to the whole installation. The
    token set carries GitHub's own ``expires_at`` in ``raw``."""
    body: dict[str, Any] = {}
    if repositories:
        body["repositories"] = list(repositories)
    if permissions:
        body["permissions"] = dict(permissions)
    resp = _request(
        "mint", "POST", f"/app/installations/{int(installation_id)}/access_tokens",
        token=app_jwt(app), **({"json": body} if body else {}),
    )
    payload = _json(resp)
    if resp.status_code != 201 or not isinstance(payload, dict) or not payload.get("token"):
        raise _refused("mint", resp)
    expires_at = str(payload.get("expires_at") or "")
    expiry = _parse_expiry(expires_at)
    expires_in = 0
    if expiry is not None:
        expires_in = max(0, int((expiry - datetime.now(timezone.utc)).total_seconds()))
    repos = payload.get("repositories")
    raw: dict[str, Any] = {
        "flow": kinds.INSTALLATION_FLOW,
        "installation_id": int(installation_id),
        "app_id": app.app_id,
        "expires_at": expires_at,
        "permissions": payload.get("permissions") or {},
        "repository_selection": str(payload.get("repository_selection") or ""),
    }
    if isinstance(repos, list):
        raw["repositories"] = [
            str(r.get("full_name") or "") for r in repos if isinstance(r, dict) and r.get("full_name")
        ]
    return TokenSet(
        access_token=str(payload["token"]), refresh_token="", expires_in=expires_in,
        scope="", token_type="Bearer", raw=raw,
    )


def list_hook_deliveries(app: GitHubApp, *, per_page: int = 30) -> list[dict[str, Any]]:
    """``GET /app/hook/deliveries``: the latest deliveries of the app's
    webhook (a walk replays the ones GitHub could not deliver)."""
    resp = _request("deliveries", "GET", "/app/hook/deliveries", token=app_jwt(app),
                    params={"per_page": per_page})
    payload = _json(resp)
    if resp.status_code != 200 or not isinstance(payload, list):
        raise _refused("deliveries", resp)
    return [d for d in payload if isinstance(d, dict)]


def get_hook_delivery(app: GitHubApp, delivery_id: int) -> dict[str, Any]:
    """``GET /app/hook/deliveries/{id}``: one delivery with its request."""
    resp = _request("deliveries", "GET", f"/app/hook/deliveries/{int(delivery_id)}", token=app_jwt(app))
    payload = _json(resp)
    if resp.status_code != 200 or not isinstance(payload, dict):
        raise _refused("deliveries", resp)
    return payload


# ---------------------------------------------------------------------------
# As the person (the user token)
# ---------------------------------------------------------------------------

def _user_refused(action: str, resp: httpx.Response) -> OAuthTokenError:
    code = INVALID_GRANT if resp.status_code == 401 else f"http_{resp.status_code}"
    return OAuthTokenError(PROVIDER_ID, action, code, _message(resp), resp.status_code)


def _paged(action: str, path: str, *, token: str, key: str, pages: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        resp = _request(action, "GET", path, token=token, params={"per_page": _PAGE, "page": page})
        payload = _json(resp)
        if resp.status_code != 200 or not isinstance(payload, dict):
            raise _user_refused(action, resp)
        items = payload.get(key)
        rows = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        out.extend(rows)
        if len(rows) < _PAGE:
            break
    return out


def _account_of(inst: dict[str, Any]) -> dict[str, Any]:
    account = inst.get("account")
    return account if isinstance(account, dict) else {}


def _is_admin(user_token: str, inst: dict[str, Any], user_id: int) -> bool:
    """Whether the person owns the installation's target: for a User target
    the account is theirs; for an Organization they are an admin of it
    (``GET /user/memberships/orgs/{org}``, which needs the app's Members
    permission: a refusal there is "not an admin"). GitHub lists every
    installation where the person can read one repository, which is not
    the standing to hand out the installation's token."""
    account = _account_of(inst)
    if str(inst.get("target_type") or "") != "Organization":
        return bool(user_id) and account.get("id") == user_id
    login = str(account.get("login") or "")
    if not login:
        return False
    resp = _request("membership", "GET", f"/user/memberships/orgs/{login}", token=user_token)
    payload = _json(resp)
    if resp.status_code != 200 or not isinstance(payload, dict):
        return False
    return payload.get("role") == _ORG_OWNER_ROLE and payload.get("state") == _MEMBERSHIP_ACTIVE


def installation_record(
    inst: dict[str, Any], repositories: list[str], *, admin: bool, repositories_truncated: bool = False,
) -> dict[str, Any]:
    """The shape an account's meta keeps for one installation
    (``repositories_truncated``: the listing stopped at its page cap)."""
    account = _account_of(inst)
    return {
        "id": int(inst.get("id") or 0),
        "account": str(account.get("login") or ""),
        "account_id": int(account.get("id") or 0),
        "target_type": str(inst.get("target_type") or ""),
        "repository_selection": str(inst.get("repository_selection") or ""),
        "repositories": list(repositories),
        "repositories_truncated": bool(repositories_truncated),
        "permissions": inst.get("permissions") if isinstance(inst.get("permissions"), dict) else {},
        "suspended_at": inst.get("suspended_at") or None,
        "admin": bool(admin),
    }


def list_installation_repositories(user_token: str, installation_id: int) -> tuple[list[str], str, bool]:
    """The repositories of an installation the person can reach (``GET
    /user/installations/{id}/repositories``), at most two pages (the third
    value says the list was cut there), and GitHub's
    ``repository_selection`` (``all`` means the list is what the person
    sees, not the installation's bound)."""
    names: list[str] = []
    selection = ""
    truncated = False
    for page in range(1, _MAX_REPOSITORY_PAGES + 1):
        resp = _request(
            "repositories", "GET", f"/user/installations/{int(installation_id)}/repositories",
            token=user_token, params={"per_page": _PAGE, "page": page},
        )
        payload = _json(resp)
        if resp.status_code != 200 or not isinstance(payload, dict):
            raise _user_refused("repositories", resp)
        selection = str(payload.get("repository_selection") or selection)
        rows = payload.get("repositories")
        page_names = [
            str(r.get("full_name") or "") for r in rows if isinstance(r, dict) and r.get("full_name")
        ] if isinstance(rows, list) else []
        names.extend(page_names)
        if len(page_names) < _PAGE:
            break
    else:
        truncated = len(page_names) == _PAGE
    return names, selection, truncated


def user_installations(user_token: str, *, app_id: str) -> UserInstallations:
    """Who the person is on GitHub and the installations of THIS app they
    can reach, each as an account-meta record with its repositories (the
    selected ones listed) and the owner standing."""
    resp = _request("user", "GET", "/user", token=user_token)
    me = _json(resp)
    if resp.status_code != 200 or not isinstance(me, dict):
        raise _user_refused("user", resp)
    user_id = int(me.get("id") or 0)
    login = str(me.get("login") or "")
    rows = _paged("installations", "/user/installations", token=user_token,
                  key="installations", pages=_MAX_INSTALLATION_PAGES)
    out: list[dict[str, Any]] = []
    for inst in rows:
        if str(inst.get("app_id") or "") != str(app_id):
            continue
        repositories: list[str] = []
        truncated = False
        if str(inst.get("repository_selection") or "") == "selected":
            try:
                repositories, _selection, truncated = list_installation_repositories(
                    user_token, int(inst.get("id") or 0),
                )
            except OAuthTokenError as exc:
                logger.warning("installation %s: its repositories could not be listed (%s)",
                               inst.get("id"), exc.code)
        out.append(installation_record(
            inst, repositories, admin=_is_admin(user_token, inst, user_id), repositories_truncated=truncated,
        ))
    return UserInstallations(login=login, user_id=user_id, installations=out)
