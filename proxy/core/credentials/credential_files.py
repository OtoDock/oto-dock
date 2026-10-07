"""Per-session OAuth token files for stdio MCPs, delivered through the broker.

A stdio OAuth MCP that declares a ``credentials_dir`` path env reads its
account's token file from a directory. No token file is mounted into a
sandbox (``core/sandbox/sandbox.py`` masks the tree's ``.credentials``), so a
local session hands the file over the credential broker instead: the MCP's
secret bundle carries ``OTO_CREDENTIAL_FILES``, a JSON string of
``{<env var>: {"subpath": <manifest subpath>, "files": {<name>: <text>}}}``
read fresh from the central store at session start; the stdio interceptor
merges the bundle into the MCP child's env at spawn, and the entry's
launcher writes the files 0600 into a private directory under the session's
temporary space and points the env var there. The copy dies with the session
and is never written back; the refresh worker keeps the central file current.
A file reaches only the MCP whose binding it was collected for, so two MCPs
that declare the same subpath each receive their own account's file.

Remote sessions keep the session-file channel
(``core/remote/remote_session_start.py``): their bundles carry no files and
the launchers take the directory the satellite materialised.
"""

from __future__ import annotations

import json
import logging
import posixpath

from core import layout
from core.credentials.mcp_broker import SecretBundle

logger = logging.getLogger("claude-proxy")

CREDENTIAL_FILES_ENV = "OTO_CREDENTIAL_FILES"


def token_file_env(
    agent_name: str, *, user_sub: str = "", session_scope: str = "user",
) -> dict[str, dict[str, str]]:
    """``{<config key>: {OTO_CREDENTIAL_FILES: <json>}}`` for every stdio MCP
    of the agent whose bound account has a token file, keyed the way the
    secret bundles are (``server_name`` or the manifest name). Each MCP
    takes only the files collected for its own binding (the collector keys
    them by manifest, then by the sandbox-virtual ``credentials_dir`` path),
    split by the manifest's ``(env var, subpath)`` entries."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver

    by_manifest = credential_resolver.collect_oauth_token_files_by_manifest(
        agent_name, user_sub=user_sub or None, session_scope=session_scope,
    )
    if not by_manifest:
        return {}
    out: dict[str, dict[str, str]] = {}
    for manifest in mcp_registry.get_agent_mcps(agent_name):
        files = by_manifest.get(manifest.name)
        if not files:
            continue
        entries = mcp_registry.get_credentials_dirs(manifest.name)
        if not entries:
            continue
        spec: dict[str, dict] = {}
        for env_var, subpath in entries:
            sub = subpath.strip("/")
            suffix = f"/{layout.CREDENTIALS_DIR}/{sub}"
            found: dict[str, str] = {}
            for vpath, content in files.items():
                if not posixpath.dirname(vpath).endswith(suffix):
                    continue
                try:
                    found[posixpath.basename(vpath)] = content.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "token file for %s/%s is not text; not delivered",
                        agent_name, manifest.name,
                    )
            if found:
                spec[env_var] = {"subpath": sub, "files": found}
        if spec:
            out[manifest.server_name or manifest.name] = {
                CREDENTIAL_FILES_ENV: json.dumps(spec),
            }
    return out


def merge_token_files(bundles: dict, env_by_key: dict[str, dict[str, str]]) -> None:
    """Add each MCP's files env to its bundle, creating the bundle for an
    MCP that had none: the bundle's presence is what gives the MCP its fetch
    token and the interceptor wrap."""
    for key, env in env_by_key.items():
        bundle = bundles.get(key) or SecretBundle()
        bundle.env.update(env)
        bundles[key] = bundle


def deliver_token_files(
    bundles: dict, agent_name: str, *, user_sub: str = "", session_scope: str = "user",
) -> None:
    """Merge the token files of ``agent_name``'s OAuth stdio MCPs into
    ``bundles`` (in place). A failure delivers nothing: the MCP starts
    unauthenticated and says so, the session still starts."""
    try:
        env_by_key = token_file_env(
            agent_name, user_sub=user_sub or "", session_scope=session_scope or "user",
        )
    except Exception:
        logger.exception(
            "token files for %s not delivered; its OAuth stdio MCPs start "
            "without a credential", agent_name,
        )
        return
    if env_by_key:
        merge_token_files(bundles, env_by_key)


def attach_token_files(config) -> None:
    """Complete a session's ``mcp_secret_bundles`` with its token files; the
    local layers call it before their MCP config preparation reads the
    bundle keys."""
    ctx = getattr(config, "security_context", None)
    scope = getattr(ctx, "session_scope", "") or "user"
    try:
        env_by_key = token_file_env(
            config.agent_name, user_sub=getattr(config, "user_sub", "") or "",
            session_scope=scope,
        )
    except Exception:
        logger.exception(
            "token files for %s not delivered; its OAuth stdio MCPs start "
            "without a credential", config.agent_name,
        )
        return
    if not env_by_key:
        return
    if config.mcp_secret_bundles is None:
        config.mcp_secret_bundles = {}
    merge_token_files(config.mcp_secret_bundles, env_by_key)
