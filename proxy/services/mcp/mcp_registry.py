"""MCP Framework: the registry's public surface.

The manifest store and its readers, the session filters, the egress rules, the
prompt's skill lists, one server's runtime entry and the per-session config
each live in a module of their own (``mcp_manifest_store``,
``mcp_session_filter``, ``mcp_egress``, ``mcp_agent_skills``,
``mcp_server_resolve``, ``mcp_session_config``), and this module re-exports what
importers take from here. ``_manifests`` is not re-exported: a scan rebinds it,
so a copy here would go stale. A test patches a name in the module whose code
reads it.
"""

import config  # noqa: F401
from services.mcp import (
    mcp_manifest_parse as _mmp,
    mcp_manifest_types as _mt,
    mcp_validate_oauth as _mvo,
    mcp_validate_webhooks as _mvw,
)
from services.mcp.mcp_agent_skills import (  # noqa: F401
    _first_sentence,
    _role_of_username,
    build_available_mcps_section,
    get_on_demand_skills_for_materialization,
    get_skill_catalog_for_agent,
    get_skills_for_agent,
)
from services.mcp.mcp_egress import (  # noqa: F401
    _is_local_host_ip,
    _resolve_to_ips,
    enumerate_mcp_network_targets,
    loopback_if_host_self,
    network_access_enabled,
    resolve_sandbox_egress,
)
from services.mcp.mcp_manifest_store import (  # noqa: F401
    _agent_base_manifests,
    _get_agent_mcps_with_device_exclusions,
    _resolve_tool_mcp,
    _validate_builder_block_transports,
    assign_core_mcps,
    authorization_server_conflict,
    build_oauth_scopes,
    core_mcp_names,
    device_capability_for_server,
    effective_config,
    find_skill_provider,
    get_agent_mcps,
    get_agent_mcps_all_placements,
    get_all_credential_schemas,
    get_all_manifests,
    get_credential_schema,
    get_manifest,
    get_manifest_by_config_key,
    get_mcps_by_provider,
    get_protected_credentials_subpaths,
    get_tool_filter,
    get_unauthorized_explicit_mcps_for_agent,
    get_visible_mcps_for_agent,
    manifest_capability_available,
    rescan,
    scan_manifests,
)
from services.mcp.mcp_manifest_types import (  # noqa: F401
    _HTTP_TRANSPORTS,
    _VALID_DEVICE_CAPABILITIES,
    AgentContextBlock,
    McpManifest,
    NetworkTargetDecl,
)
from services.mcp.mcp_server_resolve import (  # noqa: F401
    BROWSER_SERVER_KEY,
    BROWSER_TOKEN_ENV,
    _gateway_bearer_source,
    _generate_instance_config_file,
    env_instance_values,
    gateway_entry,
    resolve_server_config,
)
from services.mcp.mcp_session_config import (  # noqa: F401
    _servers_to_toml,
    _toml_escape,
    build_session_mcp_config,
    inject_credential_env_into_toml,
    inject_toml_credentials,
)
from services.mcp.mcp_session_filter import (  # noqa: F401
    EXTERNAL_CONTEXT,
    EXTERNAL_CONTEXT_REASON,
    EXTERNAL_DENIED_MCPS,
    EXTERNAL_DENIED_REASON,
    SHARED_CHAT_CONTEXT,
    audience_admits,
    audience_refusal,
    filter_manifests_for_session,
    preview_session_mcps,
    session_exclusion_reason,
    skill_audience_admits,
)
from services.mcp.mcp_templating import (  # noqa: F401
    _SECRET_TEMPLATE_TOKENS,
    _resolve_template,
)
from storage.mcp import mcp_store  # noqa: F401

# Public alias of the device-capability set — kept on the registry surface for
# external callers (api/remote/remote_machines.py). One source of truth.
DEVICE_CAPABILITIES = _VALID_DEVICE_CAPABILITIES

# Re-exports for the public surface (mcp_registry.X) used by callers + tests:
# manifest types still referenced via the registry, the oauth/webhook
# validators, and the manifest parsers — each now defined in its own module.
AgentContextBuilder = _mt.AgentContextBuilder
ConfigField = _mt.ConfigField
CostRule = _mt.CostRule
CostsBlock = _mt.CostsBlock
CredentialConfig = _mt.CredentialConfig
HostedApiKeyRelay = _mt.HostedApiKeyRelay
HostedConfig = _mt.HostedConfig
HostedOAuthApp = _mt.HostedOAuthApp
InstanceConfig = _mt.InstanceConfig
InstanceFieldDef = _mt.InstanceFieldDef
OutputRelocationDef = _mt.OutputRelocationDef
PathEnvDecl = _mt.PathEnvDecl
PathEnvValueRef = _mt.PathEnvValueRef
SandboxMountDef = _mt.SandboxMountDef
ServerConfig = _mt.ServerConfig
SkillDef = _mt.SkillDef
SystemRequirements = _mt.SystemRequirements
ToolArgPathDeclaration = _mt.ToolArgPathDeclaration
ToolFilterConfig = _mt.ToolFilterConfig
_VALID_PLACEMENTS = _mt._VALID_PLACEMENTS
_VALID_TOOL_ARG_MODES = _mt._VALID_TOOL_ARG_MODES
_ENV_VAR_NAME_RE = _mt._ENV_VAR_NAME_RE
_parse_companion_app = _mt._parse_companion_app
_validate_oauth_services = _mvo._validate_oauth_services
_validate_webhooks_block = _mvw._validate_webhooks_block
_parse_manifest = _mmp._parse_manifest
_parse_costs_block = _mmp._parse_costs_block
_parse_permissions_block = _mmp._parse_permissions_block
_parse_builder_block = _mmp._parse_builder_block
_parse_agent_context = _mmp._parse_agent_context
_parse_tool_filter = _mmp._parse_tool_filter
_parse_path_env = _mmp._parse_path_env
_validate_tool_arg_json_path = _mmp._validate_tool_arg_json_path
_parse_tool_arg_paths = _mmp._parse_tool_arg_paths
_parse_hosted_block = _mmp._parse_hosted_block
