"""The admin OAuth-app credential kinds, named once (stdlib only).

An MCP's ``credentials.oauth.app_kinds`` block declares the kinds of
vendor application an admin may register for the provider (an OAuth App,
a GitHub App), each with its own admin fields; the bundle's marker key
(``app_kinds.key``) records the kind in use. Everything else that must
tell the kinds, the token kinds, the input types or the installation
vocabulary apart imports these names; nothing compares a literal.
"""

from __future__ import annotations

from typing import Any

#: ``app_kinds.kinds[].kind``: the vendor application an admin registered.
KIND_OAUTH_APP = "oauth_app"
KIND_GITHUB_APP = "github_app"
KINDS: frozenset[str] = frozenset({KIND_OAUTH_APP, KIND_GITHUB_APP})

#: A token file's ``extra.flow`` for an installation access token (beside
#: ``personal_access_token``, ``client_credentials``, ``mcp_authorization``).
INSTALLATION_FLOW = "installation"

#: A GitHub-App kind's ``mcp_token``: the token the MCP server receives for
#: an installation binding, the owner's user token or the installation's.
MCP_TOKEN_USER = "user"
MCP_TOKEN_INSTALLATION = "installation"
MCP_TOKENS: frozenset[str] = frozenset({MCP_TOKEN_USER, MCP_TOKEN_INSTALLATION})

#: A vendor subscription's ``target_kind`` on a GitHub-App account: the
#: target is an installation, delivered by the app's own webhook.
INSTALLATION_TARGET_KIND = "installation"

#: The effective registration mode of such a subscription (beside the
#: manifest's ``relay``, ``auto`` and ``manual``): no vendor call, no console
#: step, the app's webhook delivers.
REGISTRATION_MODE_APP = "app"

#: What an admin field of the GitHub-App kind holds, read off its key by
#: substring (the rule the engine reads the OAuth-App keys with).
FIELD_APP_ID = "app_id"
FIELD_CLIENT_ID = "client_id"
FIELD_CLIENT_SECRET = "client_secret"
FIELD_PRIVATE_KEY = "private_key_pem"
FIELD_WEBHOOK_SECRET = "webhook_secret"
#: The roles a GitHub-App kind's fields must cover (the webhook secret is
#: optional: an app with its webhook off receives no events).
GITHUB_APP_REQUIRED_FIELDS: frozenset[str] = frozenset({
    FIELD_APP_ID, FIELD_CLIENT_ID, FIELD_CLIENT_SECRET, FIELD_PRIVATE_KEY,
})

#: The admin fields' ``input_type`` values.
INPUT_TEXT = "text"
INPUT_PASSWORD = "password"
INPUT_MULTILINE_SECRET = "multiline_secret"
INPUT_TYPES: frozenset[str] = frozenset({INPUT_TEXT, INPUT_PASSWORD, INPUT_MULTILINE_SECRET})

#: The per-provider directory of installation token files, beside the
#: username directories (a username is ``[a-z0-9-]``, never this).
APP_TOKEN_DIR = "_app"

#: Why an installation binding cannot serve a session (the resolver's
#: reconnect wording and the dashboard mirror name each).
APP_KEY_REJECTED = "app_key_rejected"
INSTALLATION_GONE = "installation_gone"
INSTALLATION_SUSPENDED = "installation_suspended"
INSTALLATION_EXPIRED = "installation_expired"
APP_CHANGED = "app_changed"
INSTALLATION_NOT_OWNED = "installation_not_owned"
#: An installation binding left from before the GitHub tool signed in through
#: OtoDock's relay, which gives an agent no app identity.
INSTALLATION_HOSTED = "installation_hosted"
#: A narrowed binding names a repository the installation no longer has.
INSTALLATION_REACH_OUTDATED = "installation_reach_outdated"
INSTALLATION_REASONS: frozenset[str] = frozenset({
    APP_KEY_REJECTED, INSTALLATION_GONE, INSTALLATION_SUSPENDED,
    INSTALLATION_EXPIRED, APP_CHANGED, INSTALLATION_NOT_OWNED, INSTALLATION_HOSTED,
    INSTALLATION_REACH_OUTDATED,
})


def declared_kinds(oauth_block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ``app_kinds.kinds`` list of an oauth block, ``[]`` when none."""
    block = (oauth_block or {}).get("app_kinds")
    if not isinstance(block, dict):
        return []
    kinds = block.get("kinds")
    return [k for k in kinds if isinstance(k, dict)] if isinstance(kinds, list) else []


def kind_key(oauth_block: dict[str, Any] | None) -> str:
    """The bundle key that records the kind in use, ``""`` without kinds."""
    block = (oauth_block or {}).get("app_kinds")
    return str(block.get("key") or "") if isinstance(block, dict) else ""


def kind_block(oauth_block: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    """The declared entry of ``kind``, ``{}`` when the block does not name it."""
    for entry in declared_kinds(oauth_block):
        if entry.get("kind") == kind:
            return entry
    return {}


def kind_fields(oauth_block: dict[str, Any] | None, kind: str) -> list[dict[str, Any]]:
    """The admin fields of ``kind``; the legacy ``app_credential_fields``
    when the block declares no kinds (the OAuth App is the only kind then)."""
    entry = kind_block(oauth_block, kind)
    if entry:
        fields = entry.get("fields")
        return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []
    if kind == KIND_OAUTH_APP:
        fields = (oauth_block or {}).get("app_credential_fields")
        return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []
    return []


def active_kind(oauth_block: dict[str, Any] | None, creds: dict[str, str] | None) -> str:
    """The kind an admin's bundle is in: the marker value when it names a
    declared kind, else the first declared kind, else the OAuth App."""
    kinds = declared_kinds(oauth_block)
    if not kinds:
        return KIND_OAUTH_APP
    marker = str((creds or {}).get(kind_key(oauth_block)) or "")
    if marker and any(k.get("kind") == marker for k in kinds):
        return marker
    return str(kinds[0].get("kind") or KIND_OAUTH_APP)


def mcp_token_of(oauth_block: dict[str, Any] | None, kind: str) -> str:
    """Which token the MCP server gets for an installation binding of
    ``kind``: the kind's ``mcp_token``, the user token by default."""
    value = str(kind_block(oauth_block, kind).get("mcp_token") or "")
    return value if value in MCP_TOKENS else MCP_TOKEN_USER


# ---------------------------------------------------------------------------
# An account's meta (user_credential_accounts.meta): on a GitHub-App account
# the person's GitHub identity and the installations they can reach.
# ---------------------------------------------------------------------------

def is_github_app_meta(meta: dict[str, Any] | None) -> bool:
    return bool(meta) and (meta or {}).get("kind") == KIND_GITHUB_APP


def is_hosted_app_meta(meta: dict[str, Any] | None) -> bool:
    """A GitHub-App account signed in through OtoDock's relay (``hosted``,
    written at the connect): user tokens only, no installation identity."""
    return is_github_app_meta(meta) and bool((meta or {}).get("hosted"))


def installations_of(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (meta or {}).get("installations")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def installation_in(meta: dict[str, Any] | None, installation_id: str | int) -> dict[str, Any] | None:
    """The meta's record of ``installation_id``, or ``None``."""
    wanted = str(installation_id)
    for row in installations_of(meta):
        if str(row.get("id") or "") == wanted:
            return row
    return None


def installation_standing(meta: dict[str, Any] | None, installation_id: str | int) -> str:
    """Why ``installation_id`` cannot serve as the account's service
    identity, or ``""`` when it stands: the account is not a GitHub-App one,
    the installation is not among the person's, they do not own its target,
    or GitHub suspended it."""
    if not is_github_app_meta(meta):
        return APP_CHANGED
    row = installation_in(meta, installation_id)
    if row is None:
        return INSTALLATION_GONE
    if not row.get("admin"):
        return INSTALLATION_NOT_OWNED
    if row.get("suspended_at"):
        return INSTALLATION_SUSPENDED
    return ""


def binding_covers(bound_installation_id: str, meta: dict[str, Any] | None,
                   installation_id: str | int) -> bool:
    """Whether an agent's binding lets its subscriptions take
    ``installation_id``: the binding pins that installation, or it pins none
    on a hosted GitHub-App account (hosted sign-in has no installation
    identity, so the account itself is bound) that owns it now. A
    self-managed account bound without an installation covers none."""
    if bound_installation_id:
        return bound_installation_id == str(installation_id)
    return is_hosted_app_meta(meta) and installation_standing(meta, installation_id) == ""


def app_field_role(key: str) -> str:
    """Which app field a bundle key is (``FIELD_*``), ``""`` for none."""
    upper = key.upper()
    if "WEBHOOK_SECRET" in upper:
        return FIELD_WEBHOOK_SECRET
    if "PRIVATE_KEY" in upper:
        return FIELD_PRIVATE_KEY
    if "CLIENT_SECRET" in upper:
        return FIELD_CLIENT_SECRET
    if "CLIENT_ID" in upper:
        return FIELD_CLIENT_ID
    if "APP_ID" in upper:
        return FIELD_APP_ID
    return ""
