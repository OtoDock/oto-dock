"""MCP manifest ``${...}`` template resolution.

``_resolve_template`` expands the manifest-static / ``platform.*`` / ``session.*``
/ cross-MCP ``${config:mcp:key}`` namespaces in a manifest string, and
``_SECRET_TEMPLATE_TOKENS`` flags the tokens whose resolved values must be
broker-delivered (never written to the session config file). Extracted from the
registry engine; ``services.mcp.mcp_registry`` re-exports both names so all call
sites (incl. ``docker_manager`` and the tests) are unchanged.
"""

import logging
import re

import config
from core.config import deployment
from storage import mcp_store
from services.mcp.mcp_manifest_types import McpManifest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Template Resolution
# ---------------------------------------------------------------------------

# agent_env templates that resolve to SECRETS. Their resolved values are
# broker-delivered (moved into the MCP's SecretBundle in
# build_session_mcp_config) — never left in the session config
# file/TOML. Add any future secret-bearing ${...} token here.
# No secret-valued template tokens today (phone-mcp's PHONE_API_SECRET —
# the last one — stays proxy-side since the /v1/phone/calls relay). The
# broker plumbing stays: any future secret token just gets listed here.
_SECRET_TEMPLATE_TOKENS: tuple[str, ...] = ()


def _resolve_template(
    value: str,
    manifest: McpManifest,
    agent_name: str = "",
    session_ctx: dict[str, str] | None = None,
) -> str:
    """Resolve ${...} template variables in a string.

    Supports three namespaces:

    **Manifest-static tokens** (resolved at any time, no session/agent context
    required):
      ``${platform_root}``, ``${proxy_url}``, ``${agent_name}``,
      ``${mcp_dir}``, ``${port}``, ``${docker_mcp_host}``

    ``${docker_mcp_host}`` is the service host a Docker MCP's ``url_template``
    dials: ``localhost`` on bare-metal (T1) vs the service-DNS name in
    Docker-Compose (T2). Docker MCP manifests use ``http://${docker_mcp_host}:
    ${port}`` so the same manifest works in both topologies.

    **Platform tokens** (``${platform.*}`` — resolved from ``proxy/config.py``,
    used for Docker MCP ``.env`` injection):
      ``${platform.proxy_url_for_docker}``  — how a Docker MCP calls back to the
        proxy: ``http://host.docker.internal:PORT`` (T1) vs the proxy's
        service-DNS name (T2)
      ``${platform.wopi_base_url}``         — current ``config.WOPI_BASE_URL``
      ``${platform.collabora_frame_ancestors}``, ``${platform.collabora_service_root}``
      ``${platform.host_agents_dir}``       — ``str(config.AGENTS_DIR)``
      ``${platform.mcp_port}``              — current MCP's ``server.port``
      ``${platform.oauth_insecure_transport}`` — ``"1"`` if dashboard uses
                                              ``http://`` (dev only), else ``""``

    **Session.* tokens** (``${session.*}`` — resolved from a per-session
    context dict passed to ``resolve_server_config``/``build_session_mcp_config``;
    intended for stdio MCP ``agent_env`` blocks; resolves to "" when no context
    is supplied, e.g. when admin UI renders MCP descriptors):
      ``${session.task_owner}``, ``${session.task_username}``,
      ``${session.task_scope}``, ``${session.chat_id}``

    **Cross-MCP config references**:
      ``${config:mcp_name:key}`` — reads config value from another MCP's DB config

    Security: NO MCP ever receives the master ``PROXY_API_KEY``. Stdio MCPs get
    a session-scoped JWT auto-injected as ``PROXY_API_KEY`` by ``env_builder``;
    Docker/HTTP MCPs that call back to the proxy declare ``server.proxy_callbacks``
    and receive a per-session JWT as the ``Authorization`` bearer on their
    session config (see ``build_session_mcp_config`` +
    ``auth/session_token.SESSION_JWT_PLACEHOLDER``). Neither ``${proxy_api_key}``
    nor ``${platform.api_key}`` is a valid template variable — both are rejected
    so a manifest can never pull the master key into a config file or container.
    """
    if "${" not in value:
        return value

    sctx = session_ctx or {}
    replacements = {
        # Manifest-static
        "${platform_root}":               str(config.PLATFORM_ROOT),
        "${proxy_url}":                   f"http://localhost:{config.PORT}",
        "${agent_name}":                  agent_name,
        "${mcp_dir}":                     str(manifest.mcp_dir),
        "${port}":                        str(manifest.server.port),
        # Docker-MCP service host: ``localhost`` on bare-metal (T1, published
        # loopback port) vs the service-DNS name in Docker-Compose (T2, sibling
        # container on the shared network). Docker MCP ``url_template``s use
        # this instead of a hardcoded ``localhost`` so the agent/proxy dials the
        # right host per topology. Resolves to ``localhost`` on the live native
        # install — no change there.
        "${docker_mcp_host}":             deployment.docker_mcp_host(manifest),

        # Platform.* tokens (mainly for Docker MCP `.env` injection)
        # proxy_url_for_docker = how a Docker MCP calls BACK to the proxy:
        # ``host.docker.internal`` on bare-metal (host-gateway) vs the proxy's
        # service-DNS name in Docker-Compose (sibling container).
        "${platform.proxy_url_for_docker}": f"http://{deployment.proxy_callback_host()}:{config.PORT}",
        "${platform.wopi_base_url}":      config.WOPI_BASE_URL,
        "${platform.collabora_frame_ancestors}": config.COLLABORA_FRAME_ANCESTORS,
        "${platform.collabora_service_root}":    config.COLLABORA_SERVICE_ROOT,
        "${platform.collabora_ssl_termination}": config.COLLABORA_SSL_TERMINATION,
        "${platform.host_agents_dir}":    str(config.AGENTS_DIR),
        "${platform.mcp_port}":           str(manifest.server.port),
        "${platform.oauth_insecure_transport}": "1" if config.DASHBOARD_PUBLIC_URL.startswith("http://") else "",

        # Session.* tokens (per-session stdio MCP context — empty when no ctx)
        "${session.task_owner}":          sctx.get("task_owner", ""),
        "${session.task_username}":       sctx.get("task_username", ""),
        "${session.task_scope}":          sctx.get("task_scope", ""),
        "${session.chat_id}":             sctx.get("chat_id", ""),
    }
    # NOTE: tokens in _SECRET_TEMPLATE_TOKENS resolve here as usual, but
    # build_session_mcp_config moves the resolved agent_env value into the
    # MCP's broker bundle so it never lands in the session config file.

    # Security: the master key is never templatable. Both ${proxy_api_key} and
    # the retired ${platform.api_key} are rejected (no replacement → left literal
    # so the misuse is loud, not silently authenticated). Stdio MCPs get a
    # session JWT auto-injected as PROXY_API_KEY; Docker MCPs use
    # server.proxy_callbacks (per-session JWT header).
    if "${proxy_api_key}" in value or "${platform.api_key}" in value:
        logger.warning(
            f"MCP '{manifest.name}' references the master key via a template "
            f"(${{proxy_api_key}}/${{platform.api_key}}) — rejected for "
            f"security. Stdio MCPs receive a session JWT automatically; Docker "
            f"MCPs that call back must declare server.proxy_callbacks."
        )

    result = value
    for key, val in replacements.items():
        result = result.replace(key, val)

    # Cross-MCP config references: ${config:mcp_name:key}
    for match in re.finditer(r'\$\{config:([^:}]+):([^}]+)\}', result):
        ref_mcp, ref_key = match.group(1), match.group(2)
        ref_val = mcp_store.get_mcp_config_values(ref_mcp).get(ref_key, "")
        result = result.replace(match.group(0), ref_val)

    return result
