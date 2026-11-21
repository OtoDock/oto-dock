"""One MCP server's runtime entry: the resolved config with its instance files,
the credential gateway's entry and the browser MCP's mode and origins.
"""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from auth.session_token import CALLBACK_JWT_SENTINEL_BEARER, SESSION_JWT_SENTINEL_BEARER
from core.config import deployment
from services.mcp import mcp_manifest_types as _mt
from services.mcp.mcp_manifest_store import effective_config
from services.mcp.mcp_manifest_types import McpManifest
from services.mcp.mcp_templating import _resolve_template
from services.oauth.credential_resolver import choice_kw
from storage.mcp import mcp_store

if TYPE_CHECKING:  # the gateway's types; the module is reached at call time
    from core.credentials.mcp_gateway import GatewayCredential, TokenRef

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime Config Generation
# ---------------------------------------------------------------------------


def _generate_instance_config_file(
    mcp_name: str, agent_name: str, config_dir: Path, manifest: McpManifest,
    *,
    username: str = "",
    user_role: str = "",
) -> Path | None:
    """Generate a per-agent config file from MCP instances.

    Queries mcp_instances for entries assigned to this agent, applies
    any manifest-declared transforms, and writes a JSON config file.

    Transforms get session scope (username, user_role) so they can inject
    scope-aware values (e.g. ssh-server's ``allowedLocalPaths``).

    Returns the path or None if no instances match.
    """
    instances = mcp_store.get_mcp_instances_for_agent(mcp_name, agent_name)
    if not instances:
        return None

    inst_cfg = manifest.instances
    if not inst_cfg:
        return None

    config_data = []
    for inst in instances:
        entry = dict(inst["field_values"])
        if inst_cfg.transform == "ssh_hosts":
            entry = _transform_ssh_host(
                entry, manifest, username=username, user_role=user_role,
            )
        config_data.append(entry)

    file_path = config_dir / f"{mcp_name}-{agent_name}.json"
    file_path.write_text(json.dumps(config_data, indent=2))
    return file_path


def _transform_ssh_host(
    entry: dict, manifest: McpManifest,
    *,
    username: str = "",
    user_role: str = "",
) -> dict:
    """Transform an SSH host instance to ssh-mcp-server's expected format.

    - Resolves ``key_name`` to an absolute path and renames to ``privateKey``.
    - Injects ``allowedLocalPaths`` from the session's accessible mount roots
      so SFTP upload/download from sandbox-style paths (``/users/{u}/...``,
      ``/workspace/...``, ``/config/...``) is permitted by ssh-mcp's
      validateLocalPath check.
    """
    from services import path_roles

    keys_dir = manifest.mcp_dir / "keys"
    result = dict(entry)
    if result.get("key_name"):
        result["privateKey"] = str(keys_dir / result.pop("key_name"))

    # Inject sandbox-style accessible mount roots so SFTP upload/download
    # accepts paths like /users/{u}/workspace/foo.txt, /workspace/bar.txt.
    # Mirrors what bwrap actually mounts for this session.
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": "|",  # ssh-mcp-server config splits allowedLocalPaths on `|`
    }
    joined = path_roles.resolve_path_env_entry(
        decl, username=username, user_role=user_role,
    )
    if joined:
        result["allowedLocalPaths"] = joined

    return result


def resolve_server_config(
    manifest: McpManifest,
    agent_name: str,
    *,
    mcp_config_format: str | None = "json",
    session_ctx: dict[str, str] | None = None,
) -> dict:
    """Build a single mcpServers entry from a manifest.

    Resolves relative paths to absolute, substitutes template variables,
    merges env + agent_env + config values from DB.

    Args:
        mcp_config_format: "json" (Claude CLI) or "toml" (Codex CLI).
            For ``transport: "http"`` MCPs, this determines the URL path
            suffix: ``/sse`` for JSON (Claude CLI), ``/mcp`` for TOML (Codex).
        session_ctx: optional per-session context for ``${session.*}`` token
            resolution in ``agent_env`` declarations. Keys: ``task_owner``,
            ``task_username``, ``task_scope``, ``chat_id``. Pass None when
            building config outside a real session (e.g. admin UI rendering).
    """
    srv = manifest.server
    mcp_dir = manifest.mcp_dir

    if srv.transport == "stdio":
        # Resolve command relative to mcp_dir (only if it contains a path separator)
        command = srv.command
        resolved_cmd = _resolve_template(command, manifest, agent_name, session_ctx)
        if not Path(resolved_cmd).is_absolute() and "/" in resolved_cmd:
            resolved_cmd = str(mcp_dir / resolved_cmd)
        # Bare commands like "node", "python" stay as-is (found via PATH)

        # Resolve args
        args = []
        for arg in srv.args:
            resolved = _resolve_template(arg, manifest, agent_name, session_ctx)
            if arg.startswith("-"):
                pass  # flags stay as-is
            elif Path(resolved).is_absolute():
                pass  # already absolute (e.g. from template)
            elif "." in resolved and not resolved.startswith("-"):
                # Looks like a filename (has extension) — resolve relative to mcp_dir
                resolved = str(mcp_dir / resolved)
            # Plain values like "stdio", "gmail" stay as-is
            args.append(resolved)

        entry: dict[str, Any] = {"type": "stdio", "command": resolved_cmd, "args": args}

    elif srv.transport == "http":
        # Dual-transport HTTP MCPs: serve SSE at /sse + streamable HTTP at /mcp.
        # The url_template is a base URL; the framework appends the right path.
        #
        # Both Claude CLI and Codex CLI use streamable HTTP — session_id
        # rides in the ``Mcp-Session-Id`` HTTP header, so the transport
        # survives proxying through path-prefix tunnels (the satellite WS
        # tunnel). Legacy SSE encodes the session_id in URL paths the
        # server emits ("/messages/?session_id=X"); when the CLI resolves
        # that relative to the tunneled SSE base URL it drops the
        # ``/mcp/<name>/`` prefix and the tunnel allowlist 403s the
        # subsequent POST. Streamable HTTP avoids the issue entirely.
        # Trailing slash on the URL skips the FastAPI 307 → /mcp/.
        base_url = _resolve_template(srv.url_template, manifest, agent_name, session_ctx).rstrip("/")
        if mcp_config_format == "toml":
            # Codex TOML uses the canonical "streamable-http" type name.
            entry = {"type": "streamable-http", "url": f"{base_url}/mcp/"}
        else:
            # Claude CLI JSON accepts "http" (alias for streamable-http,
            # normalized internally; verified against cli binary).
            entry = {"type": "http", "url": f"{base_url}/mcp/"}

    elif srv.transport in ("sse", "streamable-http", "streamable_http"):
        # Explicit-transport HTTP MCPs (fixed URL — manifest's url_template
        # already includes the path). The type emitted depends on the
        # consumer's schema:
        #   - Claude CLI JSON: "sse" for SSE streams, "http" for streamable
        #     request/response.
        #   - Codex CLI TOML: "sse" or "streamable-http" (matched by
        #     `_servers_to_toml`).
        url = _resolve_template(srv.url_template, manifest, agent_name, session_ctx)
        if srv.transport == "sse":
            cfg_type = "sse"
        else:
            cfg_type = "streamable-http" if mcp_config_format == "toml" else "http"
        entry = {"type": cfg_type, "url": url}
    else:
        entry = {"type": srv.transport}

    # Build merged env (only for stdio MCPs — SSE MCPs are remote servers)
    if srv.transport == "stdio":
        env: dict[str, str] = {}

        # Static env from manifest
        for k, v in manifest.env.items():
            env[k] = _resolve_template(v, manifest, agent_name, session_ctx)

        # Per-agent templated env
        for k, v in manifest.agent_env.items():
            env[k] = _resolve_template(v, manifest, agent_name, session_ctx)

        # Config values (admin-configured operational settings, the manifest's
        # default for a row nobody saved). `_`-prefixed keys are internal
        # control state (e.g. `_hosted_service_mode`,
        # `_managed_instance_deleted`) and never reach the MCP subprocess env.
        env.update(effective_config(manifest))

        if env:
            entry["env"] = env

    # The per-call limit rides on the entry in every format: each writer
    # turns it into its engine's own key (``mcp_session_config.call_limit_s``).
    if srv.tool_timeout_sec:
        entry["tool_timeout_sec"] = srv.tool_timeout_sec

    return entry


def env_instance_values(manifest, agent_name: str) -> dict[str, str]:
    """The field values of the instance an env-delivered MCP hands
    ``agent_name`` (``mcp_store.get_instance_for_agent_env_delivery``, the
    choice ``build_session_mcp_config`` makes), or ``{}``: no instances,
    another delivery, none authorized, or a hosted instance (the relay holds
    its key). What a header-style key's ``value_from`` may name besides a
    resolver secret, read by the paths that resolve the key outside the
    session build (a re-adoption, a builder). Synchronous: a store read."""
    inst = getattr(manifest, "instances", None)
    if not inst or getattr(inst, "delivery", "") != "env":
        return {}
    chosen = mcp_store.get_instance_for_agent_env_delivery(manifest.name, agent_name)
    if not chosen:
        return {}
    hosted = getattr(manifest, "hosted", None)
    akr = hosted.api_key_relay if hosted else None
    if chosen.get("hosted_mode") == "hosted" and akr and akr.available:
        return {}
    return {str(k): "" if v is None else str(v)
            for k, v in (chosen.get("field_values") or {}).items()}


def _gateway_bearer_source(
    manifest: McpManifest, user_sub: str | None, agent_name: str, task_scope: str,
    only_owner: str | None = None, account_choices: Mapping[str, str] | None = None,
) -> tuple["TokenRef | None", str | None]:
    """The token-file reference a bearer MCP's account resolves to for this
    session, or the reason it cannot: the person's bound account in user
    scope (the chat's choice in ``account_choices`` when the agent's set
    holds it), the agent's service binding otherwise (the one rule the
    resolver applies). The file is read once here so a dead account is
    refused with its reconnect wording instead of failing on every call; the
    gateway re-reads it per request."""
    from core.credentials.mcp_gateway import TokenRef
    from services.oauth import credential_resolver, oauth_account_store

    provider_id = (manifest.credentials.oauth or {}).get("provider_id", "")
    try:
        picked = credential_resolver._bound_token_source(
            manifest.name, provider_id, user_sub=user_sub, task_scope=task_scope,
            agent_name=agent_name, purpose=credential_resolver.PURPOSE_MCP,
            only_owner=only_owner, **choice_kw(account_choices),
        )
    except credential_resolver.AccountNeedsReconnect as needs:
        return None, f"{manifest.label}: {credential_resolver.reconnect_wording(str(needs))}"
    except credential_resolver.CredentialUnavailable as why:
        return None, f"{manifest.label}: {why}"
    if picked is None:
        return None, f"{manifest.label}: no account is connected for this session"
    token_dir, label, _username = picked
    ref = credential_resolver.AccountRef(label=label, owner_sub="")
    raw = oauth_account_store.read_account_token(token_dir, ref.label)
    if not raw:
        return None, f"{manifest.label}: the connected account has no token file"
    dead = credential_resolver.account_unusable_reason(raw, manifest)
    if dead:
        return None, f"{manifest.label}: {credential_resolver.reconnect_wording(dead)}"
    extra = raw.get("extra") or {}
    preferred = str(extra.get("preferred_bearer") or "") if isinstance(extra, dict) else ""
    return TokenRef(str(token_dir), ref.label, preferred), None


def gateway_entry(
    entry: dict[str, Any],
    manifest: McpManifest,
    *,
    srv_key: str,
    user_sub: str | None,
    agent_name: str,
    task_scope: str,
    bundle_env: dict[str, str],
    only_owner: str | None = None,
    account_choices: Mapping[str, str] | None = None,
) -> tuple["GatewayCredential | None", str | None]:
    """The one writer of a credentialed remote-HTTP entry, for every builder
    and both config formats: the entry's URL becomes the credential gateway
    route for this MCP (``mcp_gateway.entry_url``) and its bearer the
    session-token sentinel every layer swaps; the credential itself is
    returned for the session's bundle and never written. Returns
    ``(credential, exclusion reason)``: ``(None, None)`` for an entry that
    needs no credential (stdio, an uncredentialed HTTP MCP), a reason when
    the MCP cannot be part of the session (no account, a dead account, an
    empty key, a host the admin has not approved, a hosted MCP naming a
    loopback host).

    The credential's source: ``credentials.oauth.bearer_required`` reads the
    bound account's token file per request; ``credentials.api_key_header``
    takes the declared key's value from this MCP's resolved secrets (an
    infra value, the person's own, an env-delivered instance field). A
    sidecar (a container the proxy reaches itself) is judged under the
    ``localhost`` allowlist row; a vendor host under its own name. The
    allowlist is checked again at forward time: this check only keeps an
    unapproved MCP out of the session with an actionable sentence.
    ``only_owner``: the owner filter of an agent-scope session on a personal
    machine (``credential_resolver.pick_account``), for every binding read.
    ``account_choices``: the chat's choice of account per MCP, user scope
    only; the bearer's token and its reach are read for the same account.
    """
    from urllib.parse import urlsplit

    from core.credentials.mcp_gateway import GatewayCredential, entry_url
    from storage.identity import bearer_allowlist

    url = entry.get("url", "")
    if not url:
        return None, None
    oauth = manifest.credentials.oauth or {}
    header_block = manifest.credentials.api_key_header
    bearer = bool(oauth.get("bearer_required", False))
    parts = urlsplit(url)
    host = parts.hostname or ""
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    proxy_local = deployment.is_proxy_local_mcp_host(host, manifest)
    if not bearer and not header_block:
        if not (proxy_local and _mt.is_container(manifest.server)):
            return None, None
        # A Docker tool with no credential: its entry is the gateway's too,
        # so every call passes its door (``services/containers/door.py``).
        cred = GatewayCredential(
            upstream=origin, path=path, allowlist_key="", header="", prefix="", proxy_local=True,
            manifest=manifest.name, **_door_kind(manifest, True),
        )
    else:
        if proxy_local and not _mt.is_container(manifest.server):
            return None, (
                f"{manifest.label} names a loopback host; a hosted MCP must name its "
                "vendor's host"
            )

        if bearer:
            allowlist_key = oauth.get("provider_id", "")
            token_ref, reason = _gateway_bearer_source(manifest, user_sub, agent_name, task_scope,
                                                       only_owner, **choice_kw(account_choices))
            if reason:
                return None, reason
            from services.oauth.credential_resolver import (
                CredentialUnavailable,
                binding_reach,
            )
            try:
                reach = binding_reach(manifest.name, agent_name, user_sub=user_sub,
                                      task_scope=task_scope, only_owner=only_owner,
                                      **choice_kw(account_choices))
            except CredentialUnavailable as why:
                return None, f"{manifest.label}: {why}"
            cred = GatewayCredential(
                upstream=origin, path=path, allowlist_key=allowlist_key,
                token_ref=token_ref, proxy_local=proxy_local, manifest=manifest.name,
                reach=reach.to_dict() if reach is not None else None, **_door_kind(manifest, proxy_local),
            )
        else:
            allowlist_key = manifest.name
            key = str(cast("dict", header_block).get("value_from") or "")
            value = bundle_env.get(key, "")
            if not value:
                return None, (
                    f"{manifest.label}: no {key} value is set for this session; enter "
                    "it in Settings > Integrations or ask an admin"
                )
            cred = GatewayCredential(
                upstream=origin, path=path, allowlist_key=allowlist_key,
                header=str(cast("dict", header_block).get("name") or ""), prefix="", value=value,
                proxy_local=proxy_local, manifest=manifest.name, value_from=key,
                **_door_kind(manifest, proxy_local),
            )

        allow_host = "localhost" if proxy_local else host
        if not bearer_allowlist.is_host_allowed(allowlist_key, allow_host):
            logger.warning(
                "MCP %s left out of the session: host %s is not on the bearer "
                "allowlist for %s", manifest.name, host, allowlist_key,
            )
            return None, (
                f"{manifest.label}: the host {host} is not approved for {allowlist_key}; "
                "an admin can approve it in Admin > Security"
            )

    entry["url"] = entry_url(srv_key, path)
    headers = dict(entry.get("headers") or {})
    headers["Authorization"] = SESSION_JWT_SENTINEL_BEARER
    entry["headers"] = headers
    return cred, None


def _door_kind(manifest: McpManifest, proxy_local: bool) -> dict[str, Any]:
    """The gateway kind of an entry: every Docker tool the platform runs is
    its door's, a sign-in or not (``mcp_gateway.CONTAINER``)."""
    from core.credentials.mcp_gateway import CONTAINER
    if not (proxy_local and _mt.is_container(manifest.server)):
        return {}
    return {"kind": CONTAINER, "callbacks": bool(manifest.server.proxy_callbacks)}


def _inject_session_jwt_sentinel(
    entry: dict[str, Any], manifest: McpManifest
) -> dict[str, Any]:
    """Mark a Docker/HTTP MCP that calls back to the proxy hooks (manifest
    ``server.proxy_callbacks``; today only file-tools) with a sentinel
    ``Authorization`` bearer.

    A shared Docker MCP container can't hold a session-scoped credential in its
    env the way a per-session stdio MCP does, so instead it receives a
    per-session JWT as a request header. The session_id isn't known at config-
    build time (it's bound per-layer for pre-warmed sessions), so we set a
    sentinel here that each per-layer ``?session_id=`` injection site
    (cli/codex/direct/remote) swaps for the callback kind once the
    session_id is known. A credentialed entry carries the session sentinel
    from ``gateway_entry`` instead: the gateway route takes the session's
    own token.

    No-op for: MCPs that don't opt in, stdio entries (no ``url``), or entries
    that already carry an ``Authorization`` header. Returns the
    (possibly-mutated) entry.
    """
    if (
        manifest.server.proxy_callbacks
        and entry.get("url")
        and not entry.get("headers", {}).get("Authorization")
    ):
        entry.setdefault("headers", {})["Authorization"] = CALLBACK_JWT_SENTINEL_BEARER
    return entry


def _origin_host(origin: str) -> str:
    """The lowercased host of an origin (or origin pattern like
    ``http://localhost:*``): scheme, port/wildcard and any path stripped;
    a bracketed IPv6 host keeps its brackets."""
    host = origin.strip().lower()
    host = host.split("://", 1)[-1]
    host = host.split("/", 1)[0]
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def _apply_browser_allowed_origins(entry: dict[str, Any], allowed_origins: list[str]) -> None:
    """Inject a per-agent allow-list into the browser-control MCP entry.

    Empty list ⇒ no allow-list: the manifest ships no blocked origins, so the
    agent reaches what its machine reaches. @playwright/mcp reads
    ``PLAYWRIGHT_MCP_ALLOWED_ORIGINS`` directly; its value is a
    semicolon-separated list of origins.

    @playwright/mcp blocks an origin matching BOTH lists (deny wins), so when
    an admin config value sets ``PLAYWRIGHT_MCP_BLOCKED_ORIGINS``, the hosts
    the allow-list names are SUBTRACTED from it — otherwise an explicit allow
    (the agent's own dev install at ``localhost``) could never open. Safe
    because a non-empty allow-list flips @playwright/mcp to allowlist mode:
    everything outside ``allowed_origins`` is unreachable regardless of the
    blocked list. This is a network-request scope, NOT a hard security
    boundary — the dedicated profile + per-machine device grant are the real
    boundary.
    """
    if not allowed_origins:
        return
    env = entry.setdefault("env", {})
    env["PLAYWRIGHT_MCP_ALLOWED_ORIGINS"] = ";".join(allowed_origins)
    blocked = [
        p for p in (env.get("PLAYWRIGHT_MCP_BLOCKED_ORIGINS") or "").split(";")
        if p.strip()
    ]
    if not blocked:
        return
    allowed_hosts = {_origin_host(o) for o in allowed_origins} - {""}
    kept = [p for p in blocked if _origin_host(p) not in allowed_hosts]
    if kept != blocked:
        env["PLAYWRIGHT_MCP_BLOCKED_ORIGINS"] = ";".join(kept)


# Browser-control env keys only the framework may set. Admin MCP config values
# merge into the entry env unvalidated (resolve_server_config), so any of
# these arriving from the manifest / DB is dropped before the target
# machine's own setting is applied — otherwise an admin config value could
# flip every machine (a user-paired laptop included) into the owner's real
# browser, or point the dedicated mode at the real profile dir.
_BROWSER_MODE_ENV_KEYS = (
    "OTO_BROWSER_MODE",
    "OTO_BROWSER_UNATTENDED",
    "OTO_BROWSER_TOKEN_EXPECTED",
    "OTO_BROWSER_TOKEN_WITHHELD",
    "OTO_BROWSER_PROFILE",
    "OTO_BROWSER_PROFILE_DIR",
    "PLAYWRIGHT_MCP_PROFILE_DIR_NAME",
    "PLAYWRIGHT_MCP_EXTENSION",
    "PLAYWRIGHT_MCP_EXTENSION_TOKEN",
    "PLAYWRIGHT_MCP_USER_DATA_DIR",
    "PLAYWRIGHT_MCP_EXECUTABLE_PATH",
    "PLAYWRIGHT_MCP_BROWSER",
    "PLAYWRIGHT_MCP_CDP_ENDPOINT",
)


# The browser-control MCP's server key and its extension-token env name: the
# token is the one secret of that bundle an agent-scope session carries to
# its owner's computer (``config_builder.personal_machine_bundles``).
BROWSER_SERVER_KEY = "local"
BROWSER_TOKEN_ENV = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"


def token_reaches(target_browser, *, unattended: bool) -> bool:
    """Whether a session gets the machine owner's own-browser token: a token
    is saved, and an unattended run (a task, a call, a meeting, a key's turn)
    only when the owner ticked "Also let unattended runs like scheduled tasks,
    triggers, calls, meetings and API keys use this browser"."""
    return (target_browser is not None and getattr(target_browser, "mode", "") == "own"
            and bool(getattr(target_browser, "extension_token", None))
            and (not unattended or bool(getattr(target_browser, "unattended", False))))


def session_holds_browser_token(session_id: str) -> bool:
    """Whether a live session was handed its machine owner's browser token
    (the token's one home is the session's browser bundle)."""
    from core.credentials import mcp_broker
    bundle = mcp_broker.get(session_id, BROWSER_SERVER_KEY)
    return bool(bundle is not None and bundle.env.get(BROWSER_TOKEN_ENV))


def _apply_browser_mode(
    entry: dict[str, Any], secret_bundles: dict, target_browser, *, unattended: bool,
    withheld: bool = False,
) -> None:
    """Apply the target machine's browser-control mode to the ``local`` entry.

    ``own`` → ``OTO_BROWSER_MODE=own`` in the (non-secret) config env; the
    extension token rides the MCP's secret bundle (broker-fetched at spawn,
    never in the config file) with ``OTO_BROWSER_TOKEN_EXPECTED=1`` beside it
    so the wrapper can tell "no token saved" from "the fetch failed". Without
    a token, an unattended session (task / phone / meeting / a key's turn) gets
    ``OTO_BROWSER_UNATTENDED=1`` so the wrapper fails the first browser call
    at once instead of waiting for a click nobody will give.

    An unattended session gets the saved token only when the machine row
    opts in (``BrowserTargetSettings.unattended``, F47): a token left for an
    attended person is not spent by a task, a call or a meeting that happens
    to target the machine unless its owner chose to.

    ``withheld``: an agent-scope session on a personal machine that does not
    run as the machine's owner (``_finish_session_servers``). It gets no
    token and ``OTO_BROWSER_TOKEN_WITHHELD=1``, so the wrapper refuses the
    owner's browser instead of asking for a click there. The machine's
    ``Browser profile`` rides the plain env as ``OTO_BROWSER_PROFILE``.
    """
    env = entry.get("env") or {}
    for key in _BROWSER_MODE_ENV_KEYS:
        env.pop(key, None)
    if target_browser is not None and target_browser.mode == "own":
        env["OTO_BROWSER_MODE"] = "own"
        profile = getattr(target_browser, "profile", "")
        if profile:
            env["OTO_BROWSER_PROFILE"] = profile
        token_allowed = token_reaches(target_browser, unattended=unattended)
        if withheld:
            logger.info("browser token withheld: an agent-scope session on a personal "
                        "machine runs as someone else than its owner")
            env["OTO_BROWSER_TOKEN_WITHHELD"] = "1"
        elif token_allowed:
            from core.credentials.mcp_broker import SecretBundle
            env["OTO_BROWSER_TOKEN_EXPECTED"] = "1"
            bundle = secret_bundles.get(BROWSER_SERVER_KEY) or SecretBundle()
            bundle.env[BROWSER_TOKEN_ENV] = target_browser.extension_token
            secret_bundles[BROWSER_SERVER_KEY] = bundle
        elif unattended:
            env["OTO_BROWSER_UNATTENDED"] = "1"
    if env:
        entry["env"] = env
    else:
        entry.pop("env", None)
