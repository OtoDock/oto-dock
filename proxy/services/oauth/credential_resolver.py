"""Credential resolution engine for per-user MCP sessions.

Given a user context and agent name, resolves which MCPs are available
and what environment variables to inject for each.

Resolution priority for per-user MCPs:
  User-scoped session/task → pick_account(mcp, agent, user_sub=...):
      1. agent_account_bindings(user, mcp, agent) for explicit override
      2. user_credential_accounts default (is_default=TRUE)
      3. None → MCP excluded with "no account connected" reason
  Agent-scoped task / phone (no user_sub) → pick_account(mcp, agent):
      1. service_agent_bindings(mcp, agent) → the bound user's own account
      2. None → MCP excluded with "no account bound to this agent" reason

Infrastructure MCPs: infra_credentials(mcp_name) — read from DB only.

If required keys are missing OR no account bound → MCP excluded from
session with a reason string.

Credential schemas are read from MCP manifests via mcp_registry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import config
from auth import github_reach
from auth.github_reach import Reach
from core import layout
from services.mcp import mcp_manifest_types as _mt
from storage import database as task_store
from storage.identity import credential_store, oauth_app_kinds
from storage.mcp import mcp_store

if TYPE_CHECKING:  # the broker's type only; services import core lazily
    from core.credentials.mcp_gateway import CliCredential

logger = logging.getLogger("otodock")

# The installation token's life margins (services/oauth/github_apps.py).
MIN_REMAINING_S = 300
STEP_MIN_REMAINING_S = 1800


class CredentialUnavailable(Exception):
    """A passing failure of a credential (GitHub or the relay unreachable
    for an installation token): the MCP is left out of THIS session with
    the message, nothing is marked, the next session tries again."""


@dataclass
class ResolvedCredentials:
    """Result of credential resolution for a session."""

    env_vars: dict[str, str] = field(default_factory=dict)
    available_mcps: set[str] = field(default_factory=set)
    excluded_mcps: set[str] = field(default_factory=set)
    exclusion_reasons: dict[str, str] = field(default_factory=dict)
    # Per-MCP credential env, keyed by mcp_name — the SAME secrets as the flat
    # ``env_vars`` union, attributed to their owning MCP. The credential broker
    # needs per-MCP attribution to store each MCP's own secrets under a
    # per-(session, mcp) capability token instead of bleeding the flat union.
    env_by_mcp: dict[str, dict[str, str]] = field(default_factory=dict)
    # Secret classification. ``secret_keys`` = env-var names that are PURE
    # MCP secrets (infra + per-user creds) — stripped from the flat ``env_vars``
    # union and the config files, delivered ONLY via the broker bundle.
    # ``bash_env_keys`` = the git configuration entries (``GIT_CONFIG_*``) that
    # point the agent's shell at the platform's credential helper: they carry
    # no secret, STAY in the flat env, and are listed in OTO_STRIP_KEYS so
    # wrapped MCP children drop them.
    secret_keys: set[str] = field(default_factory=set)
    bash_env_keys: set[str] = field(default_factory=set)
    # Per-MCP command-line credentials (``git_credential_helper`` manifests):
    # the token by reference, fetched per command through the broker; the
    # config builder puts each on its MCP's bundle.
    cli_credentials: dict[str, CliCredential] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountRef:
    """Identity of a labeled account owned by a user.

    ``owner_sub`` is always the user_sub of the account's owner (non-empty for
    both scopes). For user scope it's the acting user; for agent (service)
    scope it's the user whose account a manager bound as the agent's service
    identity. Callers read from ``user_credential_accounts`` + the owner's
    ``{username}/`` token dir. ``installation_id`` names a GitHub App
    installation of the account bound as the identity (``""`` = the account
    itself): its own token then serves the session's command-line tools.
    """

    label: str
    owner_sub: str
    installation_id: str = ""


class AccountNeedsReconnect(Exception):
    """The bound account's token file can no longer serve a session; the
    message is the reason (``expired``, ``revoked``, ``mechanism_changed``,
    ``registration_unavailable``, ``issuer_changed``, or an installation
    binding's: ``storage/identity/oauth_app_kinds.INSTALLATION_REASONS``)."""


def account_unusable_reason(raw: dict, manifest) -> str:
    """Why a token file cannot serve a session on this manifest, or ``""``:
    the file's own verdict (``oauth_account_store.token_dead_reason``), an
    installation token past its hour (the worker re-mints it; a session
    never carries an expired one), and the token-origin invariant of a
    manifest that names its own authorization server
    (``mcp_authorization.token_origin_problem``)."""
    from services.oauth import mcp_authorization, oauth_account_store
    dead = oauth_account_store.token_dead_reason(raw)
    if dead:
        return dead
    extra = raw.get("extra") or {}
    if isinstance(extra, dict) and str(extra.get("flow") or "") == oauth_app_kinds.INSTALLATION_FLOW:
        expiry = oauth_account_store._parse_expiry(str(raw.get("expires_at") or ""))
        if expiry is None or expiry <= datetime.now(timezone.utc):
            return oauth_app_kinds.INSTALLATION_EXPIRED
    return mcp_authorization.token_origin_problem(raw, manifest)


_RECONNECT_WORDING = {
    "expired": "the account's access expired and the vendor issued no refresh token",
    "revoked": "the vendor ended the account's grant",
    "mechanism_changed": "this MCP now signs in through the vendor's own authorization server",
    "registration_unavailable": "the install's client registration at the vendor is gone",
    "issuer_changed": "the vendor's authorization server moved",
}
# An installation binding's reasons carry their own remedy.
_INSTALLATION_WORDING = {
    oauth_app_kinds.APP_KEY_REJECTED: (
        "GitHub refused this install's GitHub App credentials; an admin updates them "
        "in MCP Servers"),
    oauth_app_kinds.INSTALLATION_GONE: (
        "the app is no longer installed where the agent's GitHub identity pointed; "
        "a manager binds another installation in Agent Settings > MCPs"),
    oauth_app_kinds.INSTALLATION_SUSPENDED: (
        "GitHub suspended the installation the agent's GitHub identity uses"),
    oauth_app_kinds.INSTALLATION_EXPIRED: (
        "the agent's GitHub access lapsed; a new session renews it"),
    oauth_app_kinds.APP_CHANGED: (
        "this install's GitHub credentials are no longer the GitHub App the account "
        "signed in through; reconnect the account in Settings > Integrations"),
    oauth_app_kinds.INSTALLATION_NOT_OWNED: (
        "the account's owner no longer owns the installation the agent's GitHub "
        "identity uses; a manager binds another in Agent Settings > MCPs"),
    oauth_app_kinds.INSTALLATION_HOSTED: (
        "hosted sign-in gives an agent no app identity; a manager binds the account "
        "itself in Agent Settings, MCPs"),
    oauth_app_kinds.INSTALLATION_REACH_OUTDATED: (
        "a repository the agent may reach is no longer in the installation; a manager "
        "picks the repositories again in Agent Settings, MCPs"),
}


# The same reasons at the moment of choosing an installation (a bind, a
# subscription): nothing "pointed" anywhere yet.
_CHOICE_WORDING = {
    oauth_app_kinds.INSTALLATION_GONE: "the app is not installed there any more",
    oauth_app_kinds.INSTALLATION_SUSPENDED: "GitHub suspended this installation",
    oauth_app_kinds.INSTALLATION_NOT_OWNED: "this installation is not one you own on GitHub",
}


def reconnect_wording(reason: str) -> str:
    """The sentence a session reads for an account that needs a reconnect."""
    if reason in _INSTALLATION_WORDING:
        return _INSTALLATION_WORDING[reason]
    return (
        _RECONNECT_WORDING.get(reason, "the account can no longer be refreshed")
        + "; reconnect it in Settings > Integrations"
    )


def bind_wording(reason: str) -> str:
    """The sentence for an installation a person picks and cannot have."""
    return _CHOICE_WORDING.get(reason) or reconnect_wording(reason)


def pick_account(
    mcp_name: str, agent_name: str, *, user_sub: str = "",
) -> AccountRef | None:
    """Resolve which labeled account to use for (mcp_name, agent_name, scope).

    Scope is inferred from ``user_sub``:
      * Truthy → user scope: reads ``agent_account_bindings`` +
        ``user_credential_accounts.is_default=TRUE``. Returns
        ``AccountRef(label=…, owner_sub=user_sub)``.
      * Empty → service scope (agent-scoped session / phone / Shared-only
        agent): reads ``service_agent_bindings(mcp, agent)``. Returns
        ``AccountRef(label=…, owner_sub=<user_sub>)`` — the binding points
        at the user's own account a manager designated as the agent's
        service identity.

    Resolution order (service scope):
      1. Explicit ``service_agent_bindings(mcp, agent)`` row → bound user's
         account.
      2. None — no platform default; the binding must be explicit.

    User scope (unchanged):
      1. ``agent_account_bindings(user, mcp, agent)`` row.
      2. User's ⭐ default account for the MCP.
      3. None.

    Used by ``_resolve_per_user``, ``_resolve_oauth_mcp``, the bearer
    header injector, the token map (``dynamic_context._build_token_map``),
    and the end-of-session writeback to pick the correct credential blob
    to load / persist.
    """
    if user_sub:
        row = credential_store.get_account_agent_binding(
            user_sub, mcp_name, agent_name,
        )
        label = row.account_label if row is not None else None
        if not label:
            label = credential_store.get_default_account(user_sub, mcp_name)
        if not label:
            return None
        return AccountRef(label=label, owner_sub=user_sub)

    # Service scope: the agent's binding is the only source — it points at a
    # user's own account (owner_sub is always a real user_sub). No platform
    # default fallback.
    binding = credential_store.get_service_agent_binding(mcp_name, agent_name)
    if binding is not None:
        return AccountRef(
            label=binding.account_label, owner_sub=binding.account_owner_sub,
            installation_id=binding.installation_id,
        )
    return None


def binding_reach(
    mcp_name: str, agent_name: str, *, user_sub: str | None, task_scope: str,
) -> Reach | None:
    """The GitHub reach a session's credential for ``mcp_name`` carries,
    when it narrows (``None`` otherwise), by the scope the session resolves
    in (the same rule as ``_bound_token_source``): in user scope the
    person's own choice of account for the agent (a session on their
    default account, with no choice for the agent, reaches what the account
    reaches), else the agent's service binding."""
    from core.session.visibility import SCOPE_USER
    if user_sub and task_scope == SCOPE_USER:
        own = credential_store.get_account_agent_binding(user_sub, mcp_name, agent_name)
        if own is None:
            return None
        mine = github_reach.reach_of(own.repositories, own.read_only, personal=True)
        return mine if mine.narrows else None
    binding = credential_store.get_service_agent_binding(mcp_name, agent_name)
    if binding is None:
        return None
    reach = github_reach.reach_of(binding.repositories, binding.read_only)
    return reach if reach.narrows else None


def resolve_credentials(
    agent_name: str,
    user_sub: str | None,
    *,
    task_scope: str = "user",
) -> ResolvedCredentials:
    """Resolve all credentials for a session's MCPs. Synchronous — call via asyncio.to_thread.

    Reads credential schemas from MCP manifests.
    Mode-based exclusions (phone/task) are handled upstream by mcp_registry.build_session_mcp_config().

    Args:
        agent_name: Agent being used (determines which MCPs are in scope).
        user_sub: User sub ID, or None for agent-scoped tasks / phone.
        task_scope: "user" or "agent" — determines credential source for per-user MCPs.
    """
    from services.mcp import mcp_registry

    result = ResolvedCredentials()

    # Get MCPs assigned to this agent (enabled ones only). Configuration view:
    # resolve credentials for every assigned MCP regardless of device placement
    # — a credentialed MCP that ends up not attaching just leaves its creds
    # unused, whereas dropping it here would break it on a remote session.
    assigned = mcp_registry.get_agent_mcps_all_placements(agent_name)
    if not assigned:
        return result

    for manifest in assigned:
        mcp_name = manifest.name
        cred_schema = mcp_registry.get_credential_schema(mcp_name)
        if not cred_schema:
            cred_schema = {"type": "none"}

        mcp_type = cred_schema.get("type", "none")

        if mcp_type == "none":
            result.available_mcps.add(mcp_name)
            continue

        if mcp_type == "infra":
            env_vars = _resolve_infra(mcp_name, cred_schema)
            if env_vars is not None:
                result.env_vars.update(env_vars)
                result.env_by_mcp[mcp_name] = env_vars
                result.secret_keys.update(env_vars)  # infra creds are pure secrets
                result.available_mcps.add(mcp_name)
                # NOTE: IMAGE_SAVE_DIR (and any other workspace path) is now
                # injected via the manifest `path_env` field — see
                # `proxy/services/path_roles.py` and the path_env loop in
                # `config_builder.py` / `task_config_builder.py`.
            else:
                result.excluded_mcps.add(mcp_name)
                result.exclusion_reasons[mcp_name] = (
                    f"Infrastructure credentials not configured for {cred_schema.get('label', mcp_name)}. "
                    f"Admin must set these in the Integrations page."
                )
            continue

        if mcp_type == "per_user":
            # OAuth-based MCPs: check token file instead of DB credentials
            if cred_schema.get("oauth"):
                label = cred_schema.get("label", mcp_name)
                try:
                    oauth_env = _resolve_oauth_mcp(
                        mcp_name, cred_schema, user_sub, task_scope,
                        agent_name=agent_name, cli_credentials=result.cli_credentials,
                    )
                except AccountNeedsReconnect as needs:
                    result.excluded_mcps.add(mcp_name)
                    result.exclusion_reasons[mcp_name] = (
                        f"{label}: {reconnect_wording(str(needs))}."
                    )
                    continue
                except CredentialUnavailable as why:
                    result.excluded_mcps.add(mcp_name)
                    result.exclusion_reasons[mcp_name] = f"{label}: {why}."
                    continue
                if oauth_env is not None:
                    result.env_vars.update(oauth_env)
                    result.env_by_mcp[mcp_name] = oauth_env
                    # OAuth env = credentials_dir PATHS (kept in the flat env) +
                    # the git helper entries (GIT_CONFIG_*, bash-only, no
                    # secret) + mcp_env_injection (the MCP subprocess's own
                    # token env, e.g. notion NOTION_TOKEN). Paths and git
                    # entries aren't secrets; mcp_env_injection IS —
                    # secret_keys → broker bundle only, never config files or
                    # the bash env.
                    _path_keys = {
                        ev for ev, _ in mcp_registry.get_credentials_dirs(mcp_name)
                    }
                    # NB: cred_schema["oauth"] is a presence FLAG (bool) — the
                    # field list lives on the manifest's raw oauth dict.
                    _manifest_oauth = getattr(
                        getattr(manifest, "credentials", None), "oauth", None,
                    ) or {}
                    _mcp_secret_keys = (
                        set(_manifest_oauth.get("mcp_env_injection") or [])
                        & set(oauth_env)
                    )
                    result.secret_keys.update(_mcp_secret_keys)
                    result.bash_env_keys.update(
                        set(oauth_env) - _path_keys - _mcp_secret_keys
                    )
                    result.available_mcps.add(mcp_name)
                else:
                    result.excluded_mcps.add(mcp_name)
                    if user_sub and task_scope == "user":
                        result.exclusion_reasons[mcp_name] = (
                            f"{label} is not connected. "
                            f"Connect your {label} account in Settings > Integrations."
                        )
                    else:
                        result.exclusion_reasons[mcp_name] = (
                            f"No service account configured for {label}. "
                            f"This agent acts with its own service identity here "
                            f"(shared/agent scope) — a manager must bind one of "
                            f"their connected {label} accounts as this agent's "
                            f"service account in Agent Settings > MCPs."
                        )
                continue

            try:
                env_vars = _resolve_per_user(
                    mcp_name, cred_schema, user_sub, task_scope,
                    agent_name=agent_name,
                )
            except AccountMissingFields as missing:
                label = cred_schema.get("label", mcp_name)
                fields = ", ".join(missing.args[0])
                result.excluded_mcps.add(mcp_name)
                result.exclusion_reasons[mcp_name] = (
                    f"{label} needs {fields}, which your account does not hold yet. "
                    f"Update the account in Settings > Integrations."
                    if user_sub and task_scope == "user" else
                    f"The account bound to {label} for this agent lacks {fields}; "
                    f"its owner updates it in Settings > Integrations."
                )
                continue
            if env_vars is not None:
                result.env_vars.update(env_vars)
                result.env_by_mcp[mcp_name] = env_vars
                result.secret_keys.update(env_vars)  # per-user creds are pure secrets
                result.available_mcps.add(mcp_name)
            else:
                result.excluded_mcps.add(mcp_name)
                if user_sub and task_scope == "user":
                    result.exclusion_reasons[mcp_name] = (
                        f"{cred_schema.get('label', mcp_name)} is not configured. "
                        f"Set up your credentials in Settings > Integrations."
                    )
                else:
                    result.exclusion_reasons[mcp_name] = (
                        f"No service account configured for "
                        f"{cred_schema.get('label', mcp_name)}. "
                        f"This agent acts with its own service identity here "
                        f"(shared/agent scope) — a manager must bind one of "
                        f"their connected accounts as this agent's service "
                        f"account in Agent Settings > MCPs."
                    )
            continue

        # Unknown type — treat as available (no credentials needed)
        result.available_mcps.add(mcp_name)

    return result


def _resolve_infra(
    mcp_name: str, cred_type_info: dict
) -> dict[str, str] | None:
    """Resolve infrastructure credentials from DB.

    Returns ``{}`` for MCPs that declare zero credential fields (e.g.
    prometheus) so the caller can distinguish "no creds needed" from
    "creds missing".
    """
    db_creds = credential_store.get_infra_credentials(mcp_name)
    if db_creds:
        return db_creds
    if not required_field_keys(cred_type_info):
        return {}  # No credentials needed (none declared, or every one optional)
    return None


class AccountMissingFields(Exception):
    """A bound account holds no value for a field the manifest requires (a
    field added as required after the account was saved). ``args[0]`` is the
    list of the missing fields' labels."""


def required_field_keys(cred_type_info: dict) -> list[str]:
    """The ``credentials.fields`` keys a saved account must hold: every field
    but one declaring ``"required": false`` (JSON false only; a missing key
    or any other value reads required)."""
    return [
        f["key"] for f in cred_type_info.get("fields", [])
        if isinstance(f, dict) and f.get("key") and f.get("required") is not False
    ]


def missing_required_labels(cred_type_info: dict, creds: dict) -> list[str]:
    """The labels of the required fields ``creds`` holds no value for."""
    keys = set(required_field_keys(cred_type_info))
    return [
        str(f.get("label") or f["key"]) for f in cred_type_info.get("fields", [])
        if isinstance(f, dict) and f.get("key") in keys and f["key"] not in creds
    ]


def _resolve_per_user(
    mcp_name: str,
    cred_type_info: dict,
    user_sub: str | None,
    task_scope: str,
    agent_name: str = "",
) -> dict[str, str] | None:
    """Resolve per-user credentials. Returns merged env vars or None if missing.

    Multi-account: ``pick_account`` selects the bound account label for the
    session's scope (per-agent binding > none). User scope reads the caller's
    ``user_credentials``; agent scope reads the bound user's
    ``user_credentials`` (the binding's ``account_owner_sub``) — both keyed by
    ``account_label``. A field declaring ``"required": false`` may be left
    unset; an account lacking a required one raises ``AccountMissingFields``
    so the session says which.
    """

    if user_sub and task_scope == "user":
        if agent_name:
            ref = pick_account(mcp_name, agent_name, user_sub=user_sub)
        else:
            label = credential_store.get_default_account(user_sub, mcp_name)
            ref = AccountRef(label=label, owner_sub=user_sub) if label else None
        if ref is None:
            return None
        creds = credential_store.get_user_credentials(
            user_sub, mcp_name, ref.label,
        )
    else:
        # Agent scope. The binding points at a user's own account
        # (ref.owner_sub is always a real user_sub) — read their
        # user_credentials directly. No agent → no binding context → no creds.
        if not agent_name:
            return None
        ref = pick_account(mcp_name, agent_name)
        if ref is None:
            return None
        creds = credential_store.get_user_credentials(
            ref.owner_sub, mcp_name, ref.label,
        )

    missing = missing_required_labels(cred_type_info, creds)
    if missing:
        raise AccountMissingFields(missing)

    # Merge the server config (e.g. email SMTP/IMAP): the admin's saved value,
    # else the manifest's default. The person's own values override both.
    # `_`-prefixed keys are internal control state (`_hosted_service_mode`,
    # `_managed_instance_deleted`) and never reach the MCP subprocess env.
    from services.mcp import mcp_registry
    config_vals = mcp_registry.effective_config(
        mcp_registry.get_manifest(mcp_name), mcp_store.get_mcp_config_values(mcp_name))
    if config_vals:
        merged = dict(config_vals)
        merged.update(creds)
        return merged if merged else None

    return creds if creds else None


def _resolve_oauth_mcp(
    mcp_name: str,
    cred_type_info: dict,
    user_sub: str | None,
    task_scope: str,
    agent_name: str = "",
    cli_credentials: dict | None = None,
) -> dict[str, str] | None:
    """Resolve OAuth-based MCP credentials by checking token file existence.

    Multi-account (both scopes): ``pick_account`` selects the labeled account;
    only that one account's token file is copied into the per-session
    credentials_dir (so workspace-mcp's ``--single-user`` mode picks the
    right token without confusion).

    For every path_env entry on the MCP manifest with role 'credentials_dir'
    the answer carries the sandbox-virtual path of that directory; the
    bound account's token file reaches the MCP through the session's
    delivery route, never through the agent tree.

    A manifest that names a ``git_credential_helper`` host gets the git
    entries that point the shell at the platform's helper and, when
    ``cli_credentials`` is given, a ``CliCredential`` recorded under the MCP
    name: the token by reference, fetched per command, never in the answer.
    """
    from services.mcp import mcp_registry
    from services.oauth import oauth_account_store

    if not agent_name:
        # No agent context — can't compute a destination dir.
        return None

    # --- look up provider_id for this MCP (drives token dir name) ---
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.credentials.oauth:
        return None
    provider_id = manifest.credentials.oauth.get("provider_id", "")
    if not provider_id:
        return None

    # --- pick which account ---
    picked = _bound_token_source(
        mcp_name, provider_id,
        user_sub=user_sub, task_scope=task_scope, agent_name=agent_name,
    )
    if picked is None:
        logger.debug(
            "oauth resolve %s: no bound account (provider=%s) — declared "
            "credential delivery skipped", mcp_name, provider_id,
        )
        return None
    source_dir, account_label, username = picked

    source_file = source_dir / f"{account_label}.json"
    if not source_file.exists():
        logger.warning(
            "oauth resolve %s: bound account '%s' has no token file at %s — "
            "session spawns WITHOUT its declared credential env",
            mcp_name, account_label, source_file,
        )
        return None
    # A grant the vendor ended, an expired token nobody can refresh, or a
    # file the manifest's way of issuing tokens no longer covers: the MCP
    # is left out with the reason instead of failing on every call.
    dead = account_unusable_reason(
        oauth_account_store.read_account_token(source_dir, account_label) or {},
        manifest,
    )
    if dead:
        logger.warning(
            "oauth resolve %s: bound account '%s' needs a reconnect (%s)",
            mcp_name, account_label, dead,
        )
        raise AccountNeedsReconnect(dead)

    # The command-line delivery (manifest opt-in, github →
    # ``git_credential_helper: {host: "github.com", helper: …}`` beside
    # ``env_injection: ["GH_TOKEN", "GITHUB_TOKEN"]``): the shell's ``git``
    # is pointed at the platform's credential helper for the host through
    # git's env-config (``GIT_CONFIG_*``, git ≥ 2.31, session-scoped, no
    # ``~/.gitconfig`` mutation), and the ``gh`` wrapper sets the declared
    # names on the real command's own process. The token itself is fetched
    # per command from the broker (``core/credentials/mcp_gateway.CliCredential``,
    # ``/v1/hooks/cli-credential``) and is never placed in the session's
    # environment. The manifest's own ``helper`` string is accepted and not
    # read for a session: the platform supplies the helper for the host (an
    # app step script, which runs with no helper beside it, takes the
    # manifest's entries: ``services/apps/app_steps.credential_env_for``). The git
    # *identity* (user.name/email) is deliberately NOT injected — it stays a
    # plain, overridable `git config` the agent sets from its account per
    # the github-mcp skill, so the user can change it freely.
    env_injection_vars: dict[str, str] = {}
    _oauth = manifest.credentials.oauth
    injection_names = [str(n) for n in (_oauth.get("env_injection") or [])]
    # `mcp_env_injection` (manifest opt-in): expose the canonical
    # access_token via env vars destined for the MCP SERVER subprocess —
    # for stdio MCPs whose upstream reads its token from env (the official
    # notion server's NOTION_TOKEN). These are PURE secrets: the caller adds
    # them to `secret_keys` so they're stripped from config files and
    # delivered only via the broker bundle.
    mcp_env_names = _oauth.get("mcp_env_injection") or []
    mcp_env_vars: dict[str, str] = {}
    git_cred_helper = _oauth.get("git_credential_helper") or {}
    git_host = (str(git_cred_helper.get("host") or "").strip().lower()
                if isinstance(git_cred_helper, dict) else "")
    if git_host or mcp_env_names:
        token_data = oauth_account_store.read_account_token(
            source_dir, account_label,
        ) or {}
        access_token = oauth_account_store.get_canonical_access_token(token_data)
        if not access_token:
            logger.warning(
                "oauth resolve %s: bound account '%s' token file holds no "
                "canonical access_token — declared delivery (%s) NOT made",
                mcp_name, account_label,
                ",".join(injection_names + list(mcp_env_names)) or "git helper",
            )
        if mcp_env_names and access_token:
            for env_name in mcp_env_names:
                mcp_env_vars[env_name] = access_token
        if git_host and access_token:
            reach = binding_reach(mcp_name, agent_name, user_sub=user_sub, task_scope=task_scope)
            env_injection_vars.update(git_helper_env(git_host, reach=reach))
            if cli_credentials is not None:
                from core.credentials.mcp_gateway import CliCredential, TokenRef
                # The command-line tools of an installation binding get the
                # installation's own token whatever the MCP server gets.
                cli_dir, cli_label, _ = _bound_token_source(
                    mcp_name, provider_id, user_sub=user_sub, task_scope=task_scope,
                    agent_name=agent_name, purpose=PURPOSE_CLI,
                ) or (source_dir, account_label, username)
                cli_credentials[mcp_name] = CliCredential(
                    host=git_host,
                    token_ref=TokenRef(str(cli_dir), cli_label),
                    env_names=tuple(injection_names),
                    manifest=mcp_name,
                    reach=reach.to_dict() if reach is not None else None,
                )

    # Bearer-injected MCPs (slack, github, linear, notion) have no
    # `credentials_dir` path_env — their token reaches the MCP through the
    # credential gateway (`mcp_registry.gateway_entry` at config build; the
    # gateway adds the header per request). The token-file-exists check
    # above is enough to call them "connected"; we still emit any declared
    # env_injection so bash CLIs can use the token.
    if manifest.credentials.oauth.get("bearer_required", False):
        return {**env_injection_vars, **mcp_env_vars}

    cred_entries = mcp_registry.get_credentials_dirs(mcp_name)
    if not cred_entries:
        # Token-via-env stdio MCPs (notion): no credentials_dir, no bearer —
        # the resolved mcp_env_injection vars ARE the credential delivery.
        # The token-file-exists check above already proved "connected".
        if mcp_env_vars:
            return {**env_injection_vars, **mcp_env_vars}
        return None

    # The MCP's ``credentials_dir`` value is the sandbox-virtual path the
    # config builders write for the role; the file itself never lands in the
    # agent tree (no token directory is mounted into a sandbox): a local
    # session receives it in the MCP's secret bundle
    # (``core/credentials/credential_files.py``), a remote one over the
    # session-file channel.
    from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
    result: dict[str, str] = {}
    for env_var, subpath in cred_entries:
        try:
            sub = normalize_rel_path(str(subpath))
        except PathOutsideRoot:
            logger.error(
                "Refusing credential delivery for %s/%s: the manifest subpath is "
                "not a clean relative path", agent_name, mcp_name,
            )
            continue
        if username and task_scope == "user":
            result[env_var] = f"/{layout.USERS}/{username}/{layout.CREDENTIALS_DIR}/{sub}"
        else:
            result[env_var] = f"/{layout.KNOWLEDGE}/{layout.CREDENTIALS_DIR}/{sub}"

    result.update(env_injection_vars)
    result.update(mcp_env_vars)
    return result if result else None


def purge_agent_tree_credentials() -> int:
    """Remove every ``.credentials`` directory earlier releases materialised
    inside agent trees (``knowledge/.credentials`` and
    ``users/<u>/.credentials``); the central store is the only copy now. A
    link at that name is never legitimate and, left in place, refuses every
    sandbox build of the agent: it is removed and logged. The walk enters no
    linked directory. Runs once at boot, off the loop; returns the number
    removed."""
    import shutil
    root = config.AGENTS_DIR
    if not root or not root.is_dir():
        return 0

    def _plain_dirs(parent: Path) -> list[Path]:
        try:
            return [p for p in parent.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            return []

    removed = 0
    for agent in _plain_dirs(root):
        candidates = [agent / layout.KNOWLEDGE / layout.CREDENTIALS_DIR]
        users = agent / layout.USERS
        if users.is_dir() and not users.is_symlink():
            candidates += [u / layout.CREDENTIALS_DIR for u in _plain_dirs(users)]
        for d in candidates:
            if d.parent.is_symlink() or not d.parent.is_dir():
                continue
            if d.is_symlink():
                d.unlink(missing_ok=True)
                logger.warning("credentials: removed a symlink planted at %s", d)
                removed += 1
                continue
            if not d.is_dir():
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("credentials: removed %d agent-tree .credentials dir(s) left by "
                    "earlier releases", removed)
    return removed


# The helper every session's git asks for a credential: the two variables
# are set by each spawn path (the sandbox's env overrides, the satellite's
# session env) and expand through the shell git runs a ``!`` helper with.
# ``credential_helper.py`` is one of ``session_config_dir.HOOK_SCRIPTS``.
PLATFORM_GIT_HELPER = '!"$OTO_HOOK_PY" -I "$OTO_HOOKS_DIR/credential_helper.py" git'


def git_helper_env(
    host: str, helper: str = PLATFORM_GIT_HELPER, *, reach: Reach | None = None,
) -> dict[str, str]:
    """Git's env-config entries that make ``helper`` the only credential
    helper for ``https://<host>``: an empty value first resets the list
    read from every config file (Git Credential Manager on a Windows
    satellite, which would prompt; the host's own helper), then ours. A
    narrowed GitHub reach adds that git names the repository in each
    request (``useHttpPath``) and, under read-only, that a push asks for
    the marker path the broker refuses (``auth/github_reach.PUSH_MARKER``)."""
    key = f"credential.https://{host}.helper"
    pairs = [(key, ""), (key, helper)]
    if reach is not None and reach.narrows:
        pairs.append((f"credential.https://{host}.useHttpPath", "true"))
        if reach.read_only:
            base = f"https://{host}/"
            pairs.append((f"url.{base}{github_reach.PUSH_MARKER}/.pushInsteadOf", base))
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def cli_token_env(
    agent_name: str, user_sub: str | None, *, task_scope: str, providers: list[str],
) -> dict[str, str]:
    """The self-contained command-line delivery an app step's script gets
    for the providers the app requires: the manifest's ``env_injection``
    names set to the bound account's current token, and the git entries
    naming the manifest's own helper (``gh auth git-credential``, which
    reads that environment). A step has no broker helper beside it; the
    session path never produces this shape. Only the MCPs enabled on the
    local placement count, where a step runs: a provider whose MCP is
    enabled on a paired machine alone gives a step nothing. Synchronous."""
    from core import placement
    from services.mcp import mcp_registry
    from services.oauth import oauth_account_store
    out: dict[str, str] = {}
    for m in (mcp_registry.get_agent_mcps(agent_name, placement=placement.LOCAL_PLACEMENT) or []):
        oauth = (getattr(getattr(m, "credentials", None), "oauth", None) or {})
        provider_id = str(oauth.get("provider_id") or "")
        if not provider_id or provider_id not in providers:
            continue
        names = [str(n) for n in (oauth.get("env_injection") or [])]
        helper = oauth.get("git_credential_helper") or {}
        if not names and not helper:
            continue
        reach = binding_reach(m.name, agent_name, user_sub=user_sub, task_scope=task_scope)
        if reach is not None:
            # A person's own account is never an installation: their reach
            # leaves a step nothing, whatever the agent's service binding is.
            binding = None if reach.personal else credential_store.get_service_agent_binding(m.name, agent_name)
            if binding is None or not binding.installation_id:
                # A step holds its token for the whole run, unjudged: only an
                # installation's token, narrowed by GitHub itself, may go.
                logger.warning("app step: no %s credential (the agent's GitHub reach is narrowed and "
                               "a user token cannot be)", provider_id)
                continue
        try:
            picked = _bound_token_source(
                m.name, provider_id, user_sub=user_sub, task_scope=task_scope,
                agent_name=agent_name, purpose=PURPOSE_CLI, min_remaining_s=STEP_MIN_REMAINING_S,
            )
        except (AccountNeedsReconnect, CredentialUnavailable) as why:
            logger.warning("app step: no %s credential (%s)", provider_id, why)
            continue
        if picked is None:
            continue
        source_dir, label, _username = picked
        raw = oauth_account_store.read_account_token(source_dir, label) or {}
        if account_unusable_reason(raw, m):
            continue
        token = oauth_account_store.get_canonical_access_token(raw)
        if not token:
            continue
        for name in names:
            out[name] = token
        if isinstance(helper, dict) and helper.get("host") and helper.get("helper"):
            out.update(git_helper_env(str(helper["host"]).strip().lower(), str(helper["helper"])))
    return out


# What a token source is asked for: the MCP server's bearer, or the
# session's command-line tools (git, gh, an app step's script). An
# installation binding answers the two differently.
PURPOSE_MCP = "mcp"
PURPOSE_CLI = "cli"


def _bound_token_source(
    mcp_name: str,
    provider_id: str,
    *,
    user_sub: str | None,
    task_scope: str,
    agent_name: str,
    purpose: str = PURPOSE_MCP,
    min_remaining_s: float = MIN_REMAINING_S,
) -> tuple[Path, str, str] | None:
    """Resolve which central token file backs this ``(mcp, agent, scope)``.

    Returns ``(source_dir, account_label, username)`` — the central
    OAuth-store dir + the bound account's label, plus the username the
    per-session copy is keyed under (the session user for user-scope, the
    binding's owner otherwise; callers gate the user-vs-agent destination on
    ``task_scope`` exactly as before this was extracted). ``None`` when
    there is no usable binding or the user record is gone.

    An installation binding (``AccountRef.installation_id``): the
    command-line purpose always gets the installation's own token file
    (``_app/<id>.json``, minted or renewed on the way:
    ``github_apps.ensure_installation_token``, which raises
    ``AccountNeedsReconnect`` or ``CredentialUnavailable``); the MCP purpose
    gets it only when the kind's ``mcp_token`` says so, else the owner's
    user file.
    """
    from services.oauth import github_apps, oauth_account_store

    if user_sub and task_scope == "user":
        username = task_store.get_username_by_sub(user_sub)
        if not username:
            return None
        ref = pick_account(mcp_name, agent_name, user_sub=user_sub)
        if ref is None:
            return None
        source_dir = oauth_account_store.get_token_dir(
            username, provider_id=provider_id,
        )
        return source_dir, ref.label, username
    # Agent scope: the binding points at a user's own account (manager
    # designated it as the agent's service identity) — read from their
    # regular user token dir. ref.owner_sub is always a real user_sub.
    ref = pick_account(mcp_name, agent_name)
    if ref is None:
        return None
    username = task_store.get_username_by_sub(ref.owner_sub) or ""
    if not username:
        return None
    if ref.installation_id and (
        purpose == PURPOSE_CLI or github_apps.mcp_gets_installation_token(mcp_name)
    ):
        path = github_apps.ensure_installation_token(
            mcp_name, ref.owner_sub, ref.label, ref.installation_id,
            min_remaining_s=min_remaining_s,
            reach=binding_reach(mcp_name, agent_name, user_sub=user_sub, task_scope=task_scope),
        )
        return path.parent, path.stem, username
    if ref.installation_id:
        # The owner's user token serves the MCP server: the owner's standing
        # over the installation is still the binding's condition.
        standing = oauth_app_kinds.installation_standing(
            credential_store.get_account_meta(ref.owner_sub, mcp_name, ref.label), ref.installation_id,
        )
        if standing:
            raise AccountNeedsReconnect(standing)
    source_dir = oauth_account_store.get_token_dir(
        username, provider_id=provider_id,
    )
    return source_dir, ref.label, username


def collect_oauth_token_files_by_manifest(
    agent_name: str,
    *,
    user_sub: str | None = None,
    session_scope: str = "user",
) -> dict[str, dict[str, bytes]]:
    """Per-session OAuth token FILES of the agent's stdio OAuth MCPs,
    ``{manifest name: {virtual path: bytes}}``: each file under the manifest
    whose binding it was collected for, keyed by the sandbox-virtual
    ``credentials_dir`` path the MCP subprocess reads it from (e.g.
    ``/users/alice/.credentials/google-tokens/a@gmail.com.json``). The local
    broker delivers each MCP only its own entry; the remote channel takes
    the flat form (``collect_oauth_token_files``).

    ``.credentials`` is NOT part of the persistent satellite file sync —
    token files are delivered per-session over the session-file broker
    channel and wiped at session close, so a satellite disk never holds
    long-lived refresh tokens. This collector mirrors
    ``_resolve_oauth_mcp``'s account pick + source resolution but reads the
    bytes straight from the CENTRAL store (always fresh — the proxy-side
    refresh worker is authoritative), and like it skips a file that needs a
    reconnect (``account_unusable_reason``). Skips bearer-injected MCPs (token
    travels via the tunnel bearer swap, no file) and docker/none-runtime
    MCPs (never spawned on a satellite).
    """
    from services import path_roles
    from services.mcp import mcp_registry

    by_manifest: dict[str, dict[str, bytes]] = {}
    for manifest in mcp_registry.get_agent_mcps(agent_name):
        oauth = manifest.credentials.oauth
        if not oauth or oauth.get("bearer_required", False):
            continue
        if not _mt.installs_on_host(manifest.server):
            continue
        provider_id = oauth.get("provider_id", "")
        if not provider_id:
            continue
        cred_entries = mcp_registry.get_credentials_dirs(manifest.name)
        if not cred_entries:
            continue
        picked = _bound_token_source(
            manifest.name, provider_id,
            user_sub=user_sub, task_scope=session_scope,
            agent_name=agent_name,
        )
        if picked is None:
            continue
        source_dir, account_label, username = picked
        source_file = source_dir / f"{account_label}.json"
        try:
            content = source_file.read_bytes()
        except OSError:
            continue
        # A file that needs a reconnect is not delivered: the resolver
        # leaves its MCP out of the session (``AccountNeedsReconnect``).
        try:
            parsed = json.loads(content)
        except ValueError:
            parsed = {}
        dead = account_unusable_reason(parsed if isinstance(parsed, dict) else {}, manifest)
        if dead:
            logger.info(
                "token file of %s/%s not delivered: the account needs a reconnect (%s)",
                manifest.name, account_label, dead,
            )
            continue
        # Same destination rule as _resolve_oauth_mcp: user dir only for
        # user-scope sessions; agent-scope copies live under knowledge/.
        dest_username = username if session_scope == "user" else ""
        files = by_manifest.setdefault(manifest.name, {})
        for _env_var, subpath in cred_entries:
            virtual_dir = path_roles.resolve_role(
                "credentials_dir", username=dest_username, subpath=subpath,
            )
            files[f"{virtual_dir}/{source_file.name}"] = content
    return by_manifest


def collect_oauth_token_files(
    agent_name: str,
    *,
    user_sub: str | None = None,
    session_scope: str = "user",
) -> dict[str, bytes]:
    """The remote session's token files: every manifest's files of
    ``collect_oauth_token_files_by_manifest`` in one map keyed by virtual
    path. A satellite lands them at those paths, one directory per
    ``credentials_dir`` subpath, so two MCPs that declare the same subpath
    read one directory there."""
    flat: dict[str, bytes] = {}
    for files in collect_oauth_token_files_by_manifest(
        agent_name, user_sub=user_sub, session_scope=session_scope,
    ).values():
        flat.update(files)
    return flat
