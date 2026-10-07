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
from pathlib import Path

from storage.identity import credential_store
from storage.mcp import mcp_store
import config
from storage import database as task_store
from services.mcp import mcp_manifest_types as _mt
from core import layout

logger = logging.getLogger("claude-proxy")


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
    # ``bash_env_keys`` = OAuth ``env_injection`` names (GH_TOKEN/GIT_CONFIG_*)
    # the agent's BASH needs (git/gh): they STAY in the flat env, but are listed
    # in OTO_STRIP_KEYS so wrapped MCP children drop them.
    secret_keys: set[str] = field(default_factory=set)
    bash_env_keys: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AccountRef:
    """Identity of a labeled account owned by a user.

    ``owner_sub`` is always the user_sub of the account's owner (non-empty for
    both scopes). For user scope it's the acting user; for agent (service)
    scope it's the user whose account a manager bound as the agent's service
    identity. Callers read from ``user_credential_accounts`` + the owner's
    ``{username}/`` token dir.
    """

    label: str
    owner_sub: str


class AccountNeedsReconnect(Exception):
    """The bound account's token file can no longer serve a session; the
    message is the reason (``expired``, ``revoked``, ``mechanism_changed``,
    ``registration_unavailable`` or ``issuer_changed``)."""


def account_unusable_reason(raw: dict, manifest) -> str:
    """Why a token file cannot serve a session on this manifest, or ``""``:
    the file's own verdict (``oauth_account_store.token_dead_reason``) and
    the token-origin invariant of a manifest that names its own
    authorization server (``mcp_authorization.token_origin_problem``)."""
    from services.oauth import mcp_authorization, oauth_account_store
    return (
        oauth_account_store.token_dead_reason(raw)
        or mcp_authorization.token_origin_problem(raw, manifest)
    )


_RECONNECT_WORDING = {
    "expired": "the account's access expired and the vendor issued no refresh token",
    "revoked": "the vendor ended the account's grant",
    "mechanism_changed": "this MCP now signs in through the vendor's own authorization server",
    "registration_unavailable": "the install's client registration at the vendor is gone",
    "issuer_changed": "the vendor's authorization server moved",
}


def reconnect_wording(reason: str) -> str:
    """The sentence a session reads for an account that needs a reconnect."""
    return (
        _RECONNECT_WORDING.get(reason, "the account can no longer be refreshed")
        + "; reconnect it in Settings > Integrations"
    )


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
        label = credential_store.get_account_agent_binding(
            user_sub, mcp_name, agent_name,
        )
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
        label, owner_sub = binding
        return AccountRef(label=label, owner_sub=owner_sub)
    return None


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
                        agent_name=agent_name,
                    )
                except AccountNeedsReconnect as needs:
                    result.excluded_mcps.add(mcp_name)
                    result.exclusion_reasons[mcp_name] = (
                        f"{label}: {reconnect_wording(str(needs))}."
                    )
                    continue
                if oauth_env is not None:
                    result.env_vars.update(oauth_env)
                    result.env_by_mcp[mcp_name] = oauth_env
                    # OAuth env = credentials_dir PATHS (kept in the flat env) +
                    # env_injection (GH_TOKEN/GIT_CONFIG_*, bash-only) +
                    # mcp_env_injection (the MCP subprocess's own token env,
                    # e.g. notion NOTION_TOKEN). Paths/bash-injection aren't
                    # pure secrets; mcp_env_injection IS — secret_keys → broker
                    # bundle only, never config files or the bash env.
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

            env_vars = _resolve_per_user(
                mcp_name, cred_schema, user_sub, task_scope,
                agent_name=agent_name,
            )
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
    if not cred_type_info.get("fields"):
        return {}  # No credentials needed
    return None


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
    ``account_label``.
    """
    required_keys = [f["key"] for f in cred_type_info.get("fields", [])]

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

    if required_keys and not all(k in creds for k in required_keys):
        return None

    # Merge server config defaults from mcp_config_values (e.g. email SMTP/IMAP).
    # User credentials override these defaults. `_`-prefixed keys are internal
    # control state (`_hosted_service_mode`, `_managed_instance_deleted`) and
    # MUST NOT leak into the MCP subprocess env.
    config_vals = mcp_store.get_mcp_config_values(mcp_name)
    if config_vals:
        merged = {k: v for k, v in config_vals.items() if not k.startswith("_")}
        merged.update(creds)
        return merged if merged else None

    return creds if creds else None


def _resolve_oauth_mcp(
    mcp_name: str,
    cred_type_info: dict,
    user_sub: str | None,
    task_scope: str,
    agent_name: str = "",
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

    # `env_injection` (manifest opt-in): expose the bound account's
    # canonical access_token via the declared env var names so CLIs
    # inside the agent's bash sandbox (`git`, `gh`, `aws`, ...) can
    # authenticate without the user logging in a second time. Works for
    # bearer-required AND stdio MCPs — the env vars sit alongside any
    # credentials_dir paths the manifest also declares.
    env_injection_vars: dict[str, str] = {}
    _oauth = manifest.credentials.oauth
    injection_names = _oauth.get("env_injection") or []
    # `mcp_env_injection` (manifest opt-in): expose the same canonical
    # access_token via env vars destined for the MCP SERVER subprocess —
    # for stdio MCPs whose upstream reads its token from env (the official
    # notion server's NOTION_TOKEN). Unlike env_injection (bash env), these
    # are PURE secrets: the caller adds them to `secret_keys` so they're
    # stripped from config files and delivered only via the broker bundle.
    mcp_env_names = _oauth.get("mcp_env_injection") or []
    mcp_env_vars: dict[str, str] = {}
    # `git_credential_helper` (manifest opt-in, github →
    # ``{host:"github.com", helper:"!gh auth git-credential"}``): wire `git
    # push`/`clone` to authenticate NON-INTERACTIVELY from the injected
    # ``GH_TOKEN`` via git's env-config (``GIT_CONFIG_*``, git ≥2.31) —
    # session-scoped, NO token in config (the CLI supplies it live), NO
    # ``~/.gitconfig`` mutation. EXACTLY what ``gh auth setup-git`` writes: an
    # empty ``helper`` first to RESET the list, then ``!gh auth git-credential``.
    # The reset is LOAD-BEARING on Windows satellites: Git for Windows ships
    # ``credential.helper=manager`` (Git Credential Manager) in its SYSTEM
    # gitconfig, tried FIRST, which pops an INTERACTIVE browser/app auth — fine
    # on a desktop but HANGS on a headless sandbox. Clearing it makes our token
    # helper the only one. (It also clears the proxy-host
    # ``oto-git-credential-helper`` — harmless: ``gh`` is at /usr/bin/gh, mounted
    # into the bwrap, and authed by the same ``GH_TOKEN``.) The git *identity*
    # (user.name/email) is deliberately NOT injected — it stays a plain,
    # overridable `git config` the agent sets from its account per the github-mcp
    # skill, so the user can change it freely.
    git_cred_helper = _oauth.get("git_credential_helper") or {}
    if injection_names or git_cred_helper or mcp_env_names:
        token_data = oauth_account_store.read_account_token(
            source_dir, account_label,
        ) or {}
        access_token = oauth_account_store.get_canonical_access_token(token_data)
        if not access_token:
            logger.warning(
                "oauth resolve %s: bound account '%s' token file holds no "
                "canonical access_token — declared env (%s) NOT injected",
                mcp_name, account_label,
                ",".join(injection_names + mcp_env_names) or "git helper",
            )
        if injection_names and access_token:
            for env_name in injection_names:
                env_injection_vars[env_name] = access_token
        if mcp_env_names and access_token:
            for env_name in mcp_env_names:
                mcp_env_vars[env_name] = access_token
        if (
            isinstance(git_cred_helper, dict)
            and git_cred_helper.get("host")
            and git_cred_helper.get("helper")
        ):
            _ch_key = f"credential.https://{git_cred_helper['host']}.helper"
            env_injection_vars["GIT_CONFIG_COUNT"] = "2"
            # Entry 0: empty value RESETS the helper list — drops interactive
            # helpers (Windows GCM) read from earlier/lower-precedence config.
            env_injection_vars["GIT_CONFIG_KEY_0"] = _ch_key
            env_injection_vars["GIT_CONFIG_VALUE_0"] = ""
            # Entry 1: our non-interactive token helper, now the ONLY one.
            env_injection_vars["GIT_CONFIG_KEY_1"] = _ch_key
            env_injection_vars["GIT_CONFIG_VALUE_1"] = str(git_cred_helper["helper"])

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


def _bound_token_source(
    mcp_name: str,
    provider_id: str,
    *,
    user_sub: str | None,
    task_scope: str,
    agent_name: str,
) -> "tuple[Path, str, str] | None":
    """Resolve which central token file backs this ``(mcp, agent, scope)``.

    Returns ``(source_dir, account_label, username)`` — the central
    OAuth-store dir + the bound account's label, plus the username the
    per-session copy is keyed under (the session user for user-scope, the
    binding's owner otherwise; callers gate the user-vs-agent destination on
    ``task_scope`` exactly as before this was extracted). ``None`` when
    there is no usable binding or the user record is gone.
    """
    from services.oauth import oauth_account_store

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
    from services.mcp import mcp_registry
    from services import path_roles

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
