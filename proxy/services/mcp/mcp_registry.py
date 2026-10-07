"""MCP Framework — Manifest loader, in-memory registry, and runtime config generator.

Scans mcps/ on startup, loads manifest.json files, and provides the core engine
for generating per-session mcp-config.json from manifests + DB state + credentials.
"""

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import config
from core.config import deployment
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from storage.mcp import mcp_store
from auth.session_token import SESSION_JWT_SENTINEL_BEARER
# The manifest schema (data classes + validation enums), template resolution,
# the oauth/webhook validators, and manifest parsing now live in sibling
# modules. Names the registry engine uses directly are imported here; the rest
# are re-exported below so the public surface (mcp_registry.X) is unchanged.
from services.mcp.mcp_manifest_types import (
    AgentContextBlock,
    McpManifest,
    NetworkTargetDecl,
    _BUILDER_TOOL_RE,
    _HTTP_TRANSPORTS,
    _VALID_DEVICE_CAPABILITIES,
)
from services.mcp.mcp_templating import _SECRET_TEMPLATE_TOKENS, _resolve_template
from services.mcp import mcp_manifest_types as _mt
from services.mcp import mcp_manifest_parse as _mmp
from services.mcp import mcp_validate_oauth as _mvo
from services.mcp import mcp_validate_webhooks as _mvw

if TYPE_CHECKING:  # the gateway's types; the module is reached at call time
    from core.credentials.mcp_gateway import GatewayCredential, TokenRef

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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_manifests: dict[str, McpManifest] = {}


# ---------------------------------------------------------------------------
# Manifest Parsing
# ---------------------------------------------------------------------------


def _resolve_tool_mcp(tool_name: str) -> McpManifest | None:
    """Extract the MCP name from ``mcp__<server>__<tool>`` and look it up.

    Returns ``None`` if the tool name doesn't match the expected shape OR the
    referenced MCP isn't loaded. Used by ``_validate_builder_block_transports``
    post-load and by ``builder_executor`` at runtime (defense in depth).
    """
    if not _BUILDER_TOOL_RE.match(tool_name):
        return None
    # Already validated shape — split is safe.
    server_name = tool_name.split("__", 2)[1]
    return _manifests.get(server_name)


def _validate_builder_block_transports() -> None:
    """Post-load: enforce HTTP-only transport for every builder block's tool.

    Runs after ``scan_manifests`` populates ``_manifests`` so cross-MCP
    references resolve regardless of manifest load order. For each block
    that fails (unknown MCP OR stdio transport), logs WARN and **drops the
    block from the manifest's ``agent_context`` list** so it cannot fire at
    runtime. The MCP itself loads normally — only the bad block is excised.

    Stdio MCPs are rejected because they're per-session subprocesses spawned
    by CLI/Codex; the framework has no out-of-band channel to call their
    tools at session-build time. HTTP / SSE / streamable_http / Docker MCPs
    all expose a stable URL that can be hit pre-session.
    """
    for mcp_name, manifest in _manifests.items():
        if not manifest.agent_context:
            continue
        kept: list[AgentContextBlock] = []
        for idx, block in enumerate(manifest.agent_context):
            if block.builder is None:
                kept.append(block)
                continue
            tool_mcp = _resolve_tool_mcp(block.builder.tool)
            if tool_mcp is None:
                logger.warning(
                    "MCP %s agent_context[%d] builder.tool=%r references "
                    "unknown MCP — block removed",
                    mcp_name, idx, block.builder.tool,
                )
                continue
            if tool_mcp.server.transport not in _HTTP_TRANSPORTS:
                logger.warning(
                    "MCP %s agent_context[%d] builder.tool=%r references MCP "
                    "%s with stdio transport — only HTTP-class MCPs can be "
                    "invoked out-of-band; block removed",
                    mcp_name, idx, block.builder.tool, tool_mcp.name,
                )
                continue
            kept.append(block)
        manifest.agent_context = kept


# ---------------------------------------------------------------------------
# Scanning & Loading
# ---------------------------------------------------------------------------


def scan_manifests() -> dict[str, McpManifest]:
    """Discover and load all manifest.json files from mcps/ directories.

    Called once on proxy startup. Populates the module-level _manifests cache.
    Also ensures mcp_state rows exist for all discovered MCPs.
    """
    global _manifests
    found: dict[str, McpManifest] = {}

    search_dirs = [
        config.MCPS_DIR / "custom",
        config.MCPS_DIR / "community",
        config.MCPS_DIR / "skills",  # standalone skills
    ]

    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for child in sorted(search_dir.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = _parse_manifest(manifest_path)
            if not manifest:
                continue
            # Flat name registry — FIRST seen wins, deterministically (scan
            # order custom → community → skills). Last-wins would let a later
            # dir (e.g. a skill package) silently shadow a custom MCP; the
            # installers reject cross-catalog name collisions up front, so a
            # hit here is a local/manual layout problem worth shouting about.
            if manifest.name in found:
                logger.warning(
                    "MCP registry: manifest name collision on %r — keeping "
                    "%s, ignoring %s",
                    manifest.name, found[manifest.name].mcp_dir, child,
                )
                continue
            found[manifest.name] = manifest

    _manifests = found
    logger.info(
        "MCP registry: discovered %d manifests (%s)",
        len(found),
        ", ".join(sorted(found.keys())),
    )

    # Ensure mcp_state rows exist for all discovered MCPs.
    # Platform-bundled MCPs (``core`` + ``custom``) default to enabled so
    # they're usable out of the box on a fresh install — admin doesn't
    # have to hunt through MCP Servers and flip 12 toggles. Community
    # MCPs (installed from the marketplace) default to disabled so the
    # admin explicitly opts in. ``ensure_mcp_state`` is no-op for
    # already-existing rows, so flipping this default doesn't disturb
    # admin choices from previous installs.
    _DEFAULT_ENABLED_CATEGORIES = {"core", "custom"}
    for name, m in found.items():
        default_enabled = m.category in _DEFAULT_ENABLED_CATEGORIES
        mcp_store.ensure_mcp_state(name, default_enabled)

    # Now that _manifests is fully populated, validate every
    # builder block's tool references an HTTP-class MCP. Bad blocks are
    # dropped from the manifest (logged) so they can't fire at runtime.
    _validate_builder_block_transports()

    # Drop manifest-derived OAuth provider cache
    # so future manifest URL changes (e.g. admin edits a manifest's
    # authorization_url) take effect on the next get_provider() call
    # without needing a process restart. Cheap call, runs once per scan.
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()

    return found



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_manifest(name: str) -> McpManifest | None:
    return _manifests.get(name)


def get_manifest_by_config_key(key: str) -> McpManifest | None:
    """Resolve a manifest from an ``mcpServers``/``[mcp_servers.<key>]`` config
    key — the slug used in tunnel paths and remote-config rewrites.

    That key is the manifest's ``server_name`` when one is declared (e.g.
    camoufox registers as ``playwright``), otherwise its canonical ``name``.
    Tries the canonical name first, then falls back to a ``server_name`` scan —
    the same resolution the satellite tunnel's ``_resolve_upstream_url`` uses,
    so URL rewriting and upstream routing stay in lockstep.
    """
    m = _manifests.get(key)
    if m is not None:
        return m
    for manifest in _manifests.values():
        if getattr(manifest, "server_name", "") == key:
            return manifest
    return None


def get_all_manifests() -> dict[str, McpManifest]:
    return dict(_manifests)


def core_mcp_names() -> list[str]:
    """Core MCPs every agent gets.

    ``assignment_mode == "explicit"`` disqualifies an MCP — that mode means
    an admin authorizes it per-agent via an instance. ``exclude_from`` does
    NOT: it filters at session-config time, not at assignment time.
    """
    return [
        name for name, m in _manifests.items()
        if m.category == "core" and m.assignment_mode != "explicit"
    ]


def assign_core_mcps(agent_slug: str, names: list[str] | None = None) -> list[str]:
    """Assign every core MCP (and its skill rows) to ``agent_slug`` — or
    only ``names`` (a template update enabling the core MCPs the platform
    gained since the install, COMMUNITY-AGENTS-REGISTRY.md "Updates").

    Additive — safe to call on an agent that already has some of them, and
    safe to call after a template's own MCP cascade. Returns the names
    assigned. Callers treat failures as non-fatal: the startup backfill
    re-runs this for every agent on the next boot.
    """
    names = list(names) if names is not None else core_mcp_names()
    for name in names:
        mcp_store.add_agent_mcp(agent_slug, name)
        manifest = _manifests.get(name)
        if not manifest:
            continue
        for skill in manifest.skills:
            mcp_store.ensure_agent_skill(
                agent_slug, skill.id,
                default_enabled=True,
                default_exclude_from=skill.default_exclude_from,
            )
    return names


def get_mcps_by_provider(provider_id: str) -> list[McpManifest]:
    """Return manifests whose ``credentials.oauth.provider_id`` matches.

    Used by the OAuth refresh worker to look up alias maps for a token
    file (token files are keyed by provider_id, but the alias map lives
    on each MCP's manifest). Multiple MCPs sharing a provider_id share
    the OAuth grant + token dir + lock.
    """
    if not provider_id:
        return []
    out: list[McpManifest] = []
    for m in _manifests.values():
        if not m.credentials.oauth:
            continue
        if m.credentials.oauth.get("provider_id") == provider_id:
            out.append(m)
    return out


def get_tool_filter(mcp_name: str) -> tuple[str, str] | None:
    """Resolve the runtime tool filter for an MCP.

    Combines:
      1. Manifest's ``tool_filter.arg_name`` (advertises that the MCP
         supports runtime filtering).
      2. Admin-set ``mcp_state.tool_filter_regex`` (the actual regex).

    Returns ``(arg_name, regex)`` only when BOTH are non-empty. Returns
    ``None`` when the manifest omits ``tool_filter`` OR the admin hasn't
    set a regex — caller must apply no filter in either case.

    Used by ``services/docker_manager._inject_mcp_env`` (Docker MCPs) and
    by stdio MCP launch sites to append the flag to the spawn arglist.
    """
    manifest = _manifests.get(mcp_name)
    if manifest is None or manifest.tool_filter is None:
        return None
    from storage.mcp import mcp_store
    regex = mcp_store.get_tool_filter_regex(mcp_name) or ""
    if not regex:
        return None
    return (manifest.tool_filter.arg_name, regex)


def authorization_server_conflict(name: str, oauth: dict | None) -> str:
    """One provider id, one way of issuing tokens: the reason an incoming
    manifest ``name`` with ``oauth`` conflicts with an installed manifest of
    the same ``provider_id`` (one declares ``authorization_server`` and the
    other does not), or ``""``. Their token files would share a directory
    while being issued by different servers."""
    if not oauth:
        return ""
    provider_id = oauth.get("provider_id", "")
    if not provider_id:
        return ""
    incoming = bool(oauth.get("authorization_server"))
    for other in get_mcps_by_provider(provider_id):
        if other.name == name:
            continue
        installed = bool((other.credentials.oauth or {}).get("authorization_server"))
        if installed != incoming:
            return (
                f"provider_id {provider_id!r} is used by the installed MCP "
                f"{other.name!r}, which "
                + ("names its own authorization server" if installed
                   else "uses an admin's OAuth app")
                + "; one provider id issues its tokens one way"
            )
    return ""


def get_protected_credentials_subpaths() -> frozenset[str]:
    """Return directory names that hold OAuth-credential files.

    Walks every loaded manifest's ``path_env`` entries; collects subpath
    strings where ``role == "credentials_dir"`` (handles BOTH the
    shorthand single-role form AND the multi-value form). These
    directories are considered sensitive (raw OAuth tokens) and are:

      * Excluded from the dashboard file-tree at the API layer
        (``api/agents/agents.py::_build_tree``).
      * Refused by the agent's permission hook for ``Read``/``Write``/
        ``Edit`` (``auth/path_policy.py``) and by ``Bash`` when the
        command argument lands inside one (regardless of role — even
        admins are blocked, because the OAuth-connect UI is the
        intended management surface, not raw token editing).

    Manifest-driven so future MCPs that add a ``credentials_dir``
    ``path_env`` entry auto-inherit the protection — no code change
    needed per provider.

    Currently returns ``{"google-tokens"}`` (workspace-mcp). Bearer-
    injecting remote MCPs (Slack/Linear/Notion) don't copy
    tokens into the bwrap — their tokens stay in the central
    ``sessions/{provider}-tokens/`` store outside the agent's view —
    so they don't appear here.
    """
    out: set[str] = set()
    for m in _manifests.values():
        for decl in m.path_env.values():
            # Shorthand form: single role + subpath.
            if decl.role == "credentials_dir" and decl.subpath:
                out.add(decl.subpath)
            # Multi-value form: list of refs; check each.
            for ref in decl.values:
                if ref.role == "credentials_dir" and ref.subpath:
                    out.add(ref.subpath)
    return frozenset(out)


def _device_placement_reason(
    manifest: McpManifest,
    *,
    placement: PlacementCapabilities,
) -> str | None:
    """Return a human-readable exclusion reason if a device-local MCP must NOT
    attach to this session, else None. The single source of truth for the
    placement / consent / display gate, shared by the runtime filter AND the
    ``# Excluded MCPs`` surfacing so they can never diverge.

    Three gates, all fail-closed:
      1. PLACEMENT — a ``satellite_only`` MCP, OR any MCP declaring a
         ``device_capability``, runs ONLY on a satellite (device control on the
         proxy would drive the SERVER's screen/input). The placement defaults
         to the LOCAL one at every call site, so such an MCP attaches ONLY
         when the session is explicitly known to run on a satellite.
      2. CONSENT — a ``device_capability`` MCP attaches only when the
         target machine's owner has GRANTED that capability (the placement's
         ``device_grants``; the local placement carries none, so an ungranted
         machine blocks it).
      3. DISPLAY — a ``requires_display`` MCP is excluded only when the remote
         target is KNOWN to have no display (``has_display is False``);
         None = unknown → don't exclude (the tool reports "no display" at call
         time if it turns out to be missing).
    """
    if (manifest.placement == "satellite_only" or manifest.device_capability) and not placement.is_remote:
        return "Requires a remote machine (satellite)"
    # Reaching here with a device_capability set means the placement is remote
    # (gate 1 returned otherwise) — this is the on-satellite owner-consent check.
    if manifest.device_capability and manifest.device_capability not in (placement.device_grants or set()):
        return f"Machine has not granted '{manifest.device_capability}' device control"
    if manifest.requires_display and placement.is_remote and placement.has_display is False:
        return "Remote machine has no interactive display"
    return None


def _agent_base_manifests(agent_name: str) -> list[McpManifest]:
    """The agent's base runtime manifest set: visible AND manager-enabled AND
    platform-enabled — BEFORE any device-local placement / consent / display
    gate. Configuration-view callers (``get_agent_mcps_all_placements``) use
    this directly; session callers go through ``get_agent_mcps``, which then
    applies the device gate for the resolved target.

    Visibility comes from the manifest's assignment_mode:
      - "auto": always visible
      - "explicit": visible only if at least one mcp_instance authorizes the
        agent (agent in instance.agents OR instance.assigned_to_all=True)
    Manager-enablement is the agent_mcps row presence. Platform-enabled is
    mcp_state.enabled. All three must be True for the manifest to be returned.
    """
    enabled_by_manager = set(mcp_store.get_manager_enabled_mcps(agent_name))
    if not enabled_by_manager:
        return []

    state_enabled = mcp_store.get_all_mcp_states()
    visible_explicit = mcp_store.get_visible_explicit_mcps(agent_name)

    result: list[McpManifest] = []
    for name in enabled_by_manager:
        if not state_enabled.get(name, False):
            continue
        manifest = _manifests.get(name)
        if not manifest:
            continue
        if manifest.assignment_mode == "explicit" and name not in visible_explicit:
            # admin revoked authorization; preserve agent_mcps row but skip at runtime
            continue
        if not manifest_capability_available(manifest):
            # backing platform feature (STT / call providers) removed → skip at runtime
            continue
        result.append(manifest)
    return result


def _get_agent_mcps_with_device_exclusions(
    agent_name: str,
    *,
    placement: PlacementCapabilities,
) -> tuple[list[McpManifest], dict[str, str]]:
    """Core of ``get_agent_mcps`` — returns (kept manifests, {excluded: reason})
    where exclusions are ONLY the device-local placement / consent / display
    drops (so callers can surface them). Visibility/enablement drops are silent.
    """
    result: list[McpManifest] = []
    device_exclusions: dict[str, str] = {}
    for manifest in _agent_base_manifests(agent_name):
        reason = _device_placement_reason(manifest, placement=placement)
        if reason:
            device_exclusions[manifest.name] = reason
            continue
        result.append(manifest)
    return result, device_exclusions


def get_agent_mcps(
    agent_name: str,
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
) -> list[McpManifest]:
    """Return manifests for runtime: the base set (visible AND manager-enabled
    AND platform-enabled) minus device-local MCPs whose placement / consent /
    display gate excludes this session.

    This is the canonical "what MCPs does this agent have at runtime on THIS
    target" function used by skill loading, system prompt building, and runtime
    config generation.

    The ``placement`` (the session's resolved placement, ``core.placement``)
    gates device-local MCPs (computer / browser / app-connector control).
    **Fail-closed**: the default is the LOCAL placement (no grants), so
    callers that don't know the target NEVER leak a ``satellite_only`` /
    device-capability MCP onto a local session — every consumer that builds
    a local session (sandbox mounts, Direct-LLM in-process pool, system
    prompt) relies on this.
    """
    kept, _ = _get_agent_mcps_with_device_exclusions(agent_name, placement=placement)
    return kept


def _resolve_to_ips(host: str) -> list[str]:
    """Resolve a host (or pass through an IP literal) to its IP address(es).

    Runs in the proxy's namespace at sandbox-build time. In T2 this resolves a
    Docker service name (via the embedded DNS) and ``host.docker.internal`` (via
    the container's /etc/hosts). Returns [] on failure (fail-closed → no carve).
    """
    import ipaddress
    import socket
    host = (host or "").strip()
    if not host:
        return []
    # Not an IP literal → fall through to DNS resolution.
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return [host]  # already a literal
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos})


def _is_carveable_ip(ip: str) -> bool:
    """Whether an egress carve-out is needed for this IP.

    Only private / link-local IPs need un-blackholing (public IPs ride NAT'd
    outbound already, so carving them is redundant). The cloud-metadata
    endpoints are NEVER carved, even if a target resolves to one.
    """
    import ipaddress
    _METADATA = {"169.254.169.254", "169.254.170.2", "fd00:ec2::254"}
    if ip in _METADATA:
        return False
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return obj.is_private or obj.is_link_local


def _is_local_host_ip(ip: str) -> bool:
    """True if ``ip`` is one of the proxy HOST's own addresses (binding succeeds).

    Such an IP is *local* inside the agent's isolated netns (pasta copies the
    host's address, so it routes to loopback) — a route carve can't reach a
    service there. Instead the loopback ``-T`` splice + a ``127.0.0.1`` rewrite
    of the MCP's target (see ``_rewrite_host_self_targets_to_loopback``) reach a
    co-located service. T1 concern only — in T2 the proxy is a container.
    """
    import socket
    if not ip:
        # An empty host would bind all interfaces — never a "local host IP".
        # Callers only pass resolved literals (_resolve_to_ips), so this is
        # defensive; it also keeps the probe from ever binding wildcard.
        return False
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


class _PlatformTargets(NamedTuple):
    """What a sandbox may never reach through a carve or a splice."""
    compose: bool
    networks: tuple            # control-plane networks (compose only)
    addresses: frozenset[str]  # platform service addresses (compose only)
    ports: frozenset[int]      # local platform listeners (never spliced)
    # The document-sidecar plane (file-tools, Collabora): carved only for a
    # platform-shipped Docker MCP (compose only).
    sidecar_networks: tuple = ()
    # Platform hosts that start after the proxy and move when their container
    # is recreated (the phone daemon): resolved at every egress computation
    # (compose only).
    late_hosts: tuple[str, ...] = ()


# The phone daemon's AudioSocket listener has no config key: the phone
# overlay publishes it on the bind address next to the HTTP API.
_AUDIOSOCKET_PORT = 9092

_platform_targets_cache: dict[bool, _PlatformTargets] = {}


def _host_is_local(host: str) -> bool:
    import ipaddress
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    for ip in _resolve_to_ips(host):
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(ip).is_loopback:
                return True
        if _is_local_host_ip(ip):
            return True
    return False


def _subnet_setting(key: str, default: str):
    """A subnet config key as a network; an invalid value falls back to the
    default, which is then refused as well."""
    import ipaddress
    value = config._cfg(key, default)
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        logger.warning("%s=%r is not a network; refusing the default %s", key, value, default)
        return ipaddress.ip_network(default)


def _platform_targets() -> _PlatformTargets:
    """Computed once per process and mode (this runs on the loop at session
    start): the control-plane networks, the document-sidecar plane and the
    database and socket-proxy addresses in compose mode, the phone daemon's
    host to resolve per computation, and the ports of the platform listeners
    on this host and the ports that front the proxy, which the bare-metal
    loopback splice must never open."""
    from urllib.parse import urlsplit
    compose = deployment.in_docker_compose()
    cached = _platform_targets_cache.get(compose)
    if cached is not None:
        return cached
    networks: list = []
    sidecar_networks: list = []
    late_hosts: list[str] = []
    addresses: set[str] = set()
    ports: set[int] = set()
    try:
        from psycopg.conninfo import conninfo_to_dict
        db = conninfo_to_dict(config.DATABASE_URL)
    except Exception:
        db = {}
    db_hosts = [h.strip() for h in str(db.get("host") or "localhost").split(",") if h.strip()]
    try:
        db_port = int(db.get("port") or 5432)
    except (TypeError, ValueError):
        db_port = 5432
    phone = urlsplit(config.PHONE_SERVER_URL or "")
    if compose:
        # The compose interpolation variables never reach the container's
        # environment; ``config.env`` is the bind-mounted ``.env`` there.
        for key, default in (("OTODOCK_SOCKETPROXY_SUBNET", "10.202.0.0/24"),
                             ("OTODOCK_DATA_SUBNET", "10.203.0.0/24")):
            networks.append(_subnet_setting(key, default))
        sidecar_networks.append(_subnet_setting("OTODOCK_INTERNAL_SUBNET", "10.204.0.0/24"))
        for host in (*db_hosts, config.DOCKER_SOCKET_PROXY_HOST):
            addresses.update(_resolve_to_ips(host))
        if phone.hostname:
            late_hosts.append(phone.hostname)
        ports.add(int(config.DOCKER_SOCKET_PROXY_PORT))
    if any(_host_is_local(h) for h in db_hosts):
        ports.add(db_port)
    if phone.hostname and _host_is_local(phone.hostname):
        ports.add(phone.port or 9093)
        ports.add(_AUDIOSOCKET_PORT)
    if not compose:
        # The ports that front the proxy on this host: its own listener
        # (the hook forward already lands the sandbox's 127.0.0.1:PORT on
        # the internal listener) and, when the public URL's host is this
        # host, the public URL's port (an edge here forwards to the main
        # listener, where a loopback hop's X-Forwarded-For is walked).
        ports.add(int(config.PORT))
        pub = urlsplit(config.DASHBOARD_PUBLIC_URL or "")
        if pub.hostname and _host_is_local(pub.hostname):
            with contextlib.suppress(ValueError):
                ports.add(pub.port or (443 if pub.scheme == "https" else 80))
    targets = _PlatformTargets(
        compose, tuple(networks), frozenset(addresses), frozenset(ports),
        tuple(sidecar_networks), tuple(late_hosts),
    )
    _platform_targets_cache[compose] = targets
    return targets


def _is_platform_ip(
    ip: str, targets: _PlatformTargets, *,
    late_addresses: set[str] | frozenset[str] = frozenset(), sidecar_ok: bool = False,
) -> bool:
    """Loopback in every mode; in compose mode also the control-plane
    networks, the document-sidecar plane (unless ``sidecar_ok``), the service
    addresses, the late hosts' current addresses and the proxy container's
    own addresses. A mapped IPv4 form is judged as its IPv4 address."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_loopback or str(addr) in targets.addresses or str(addr) in late_addresses:
        return True
    if any(addr in net for net in targets.networks):
        return True
    if not sidecar_ok and any(addr in net for net in targets.sidecar_networks):
        return True
    return targets.compose and _is_local_host_ip(str(addr))


def _is_platform_shipped(manifest) -> bool:
    """The MCP's folder is in the platform's own ``custom`` tree (the catalog
    installs into ``community``, whatever category its manifest declares)."""
    mcp_dir = getattr(manifest, "mcp_dir", None)
    return mcp_dir is not None and Path(mcp_dir).parent == Path(config.MCPS_DIR) / "custom"


def resolve_sandbox_egress(
    agent_name: str, *, user_sub: str = "", extra_targets: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Egress allow-set for a local session's isolated sandbox netns.

    Returns ``(forwards, allow_hosts)`` for ``scripts/oto-sandbox-net``:
      * ``forwards`` — loopback ports spliced via pasta ``-T``: the proxy hook
        port (always — the permission gate POSTs to ``127.0.0.1:{PORT}``) plus
        each **T1** Docker-MCP's host-loopback-published port.
      * ``allow_hosts`` — routable IPs carved back out of the blackholes (the
        host's own subnet is now blocked): **T2** Docker-MCP container IPs
        (service-DNS resolved), and every homelab MCP's configured target host
        (when its ``_network_access`` toggle is on). Public targets are skipped
        (already reachable); Postgres / other siblings / the rest of the LAN
        stay blocked.

    The local placement (fail-closed): a ``satellite_only`` MCP never runs
    locally, so it contributes nothing here. Deterministic order for stable
    argv + golden tests.
    """
    from urllib.parse import urlparse

    forwards: list[str] = [str(config.PORT)]
    fseen: set[str] = {str(config.PORT)}
    allow_hosts: list[str] = []
    aseen: set[str] = set()
    targets = _platform_targets()

    def _add_forward(port) -> None:
        # Judged before the dedup: the hook forward of config.PORT sits in
        # ``forwards`` already, so a target on it is refused aloud instead of
        # passing silently (the hook forward itself stays).
        s = str(port)
        try:
            refused = int(port) in targets.ports
        except (TypeError, ValueError):
            refused = True
        if refused:
            logger.warning(
                "egress for %s: port %s is a platform listener on this host; "
                "not spliced", agent_name, s,
            )
            return
        if s in fseen:
            return
        fseen.add(s)
        forwards.append(s)

    late: set[str] | None = None

    def _late_addresses() -> set[str]:
        nonlocal late
        if late is None:
            late = {ip for h in targets.late_hosts for ip in _resolve_to_ips(h)}
        return late

    def _add_allow(host: str, *, sidecar_ok: bool = False) -> None:
        for ip in _resolve_to_ips(host):
            if ip in aseen or not _is_carveable_ip(ip):
                continue
            if _is_platform_ip(
                ip, targets, late_addresses=_late_addresses(), sidecar_ok=sidecar_ok,
            ):
                logger.warning(
                    "egress for %s: %s resolves to %s, a platform control-plane "
                    "address; not carved", agent_name, host, ip,
                )
                continue
            aseen.add(ip)
            allow_hosts.append(ip)

    mcps = get_agent_mcps(agent_name, placement=LOCAL_PLACEMENT) or []
    # The person's tier decides which audience-gated MCPs carve anything:
    # a viewer's sandbox gets no route to the SSH hosts an editor's does.
    if user_sub:
        from auth import providers
        role = providers.effective_role_of(user_sub, agent_name)
        mcps = [m for m in mcps if audience_admits(getattr(m, 'audience', ''), role)]

    # 1. Docker MCPs the agent dials.
    for manifest in mcps:
        srv = manifest.server
        if not _mt.is_container(srv) or not srv.port:
            continue
        if deployment.in_docker_compose():
            # T2: a sibling container reached by service-DNS on the shared
            # network. The on-link subnet is now blackholed, so resolve the
            # service name to its container IP and carve exactly that.
            # Only the platform's own Docker MCP (file-tools) lives on the
            # document-sidecar plane; nothing else is carved into it.
            host = (deployment.docker_mcp_host(manifest)
                    or getattr(srv, "service_name", "") or manifest.name)
            _add_allow(host, sidecar_ok=_is_platform_shipped(manifest))
        else:
            resolved = _resolve_template(srv.url_template, manifest, agent_name)
            host = (urlparse(resolved).hostname or "localhost") if resolved else "localhost"
            if host in ("localhost", "127.0.0.1", "::1", ""):
                _add_forward(srv.port)  # T1: published on host loopback
            else:
                _add_allow(host)

    # (The phone daemon needs no carve here anymore: phone-mcp calls the
    # proxy's /v1/phone/calls relay over the standard PROXY_URL loopback
    # forward, and the proxy dials the daemon from outside the sandbox.)

    # 2. Homelab MCP network targets — only for MCPs that declare them, are
    #    placement=any, capability-available, and have the admin toggle on.
    for manifest in mcps:
        if not manifest.network_targets or manifest.placement != "any":
            continue
        if not manifest_capability_available(manifest):
            continue
        if not network_access_enabled(manifest):
            continue
        is_t2 = deployment.in_docker_compose()
        for host, port in enumerate_mcp_network_targets(
            manifest, agent_name, user_sub=user_sub,
        ):
            if host in ("localhost", "127.0.0.1", "::1"):
                if is_t2:
                    # localhost inside the container ≠ the Docker host — nothing
                    # to reach. The admin must use a LAN IP or host.docker.internal.
                    logger.warning(
                        "network target for MCP %r is %r in a containerized "
                        "install — unreachable; configure a LAN IP or "
                        "host.docker.internal", manifest.name, host,
                    )
                elif port:
                    _add_forward(port)  # T1 same-host service (loopback splice)
                continue
            ips = _resolve_to_ips(host)
            # A service on the proxy host itself (T1), addressed by one of the
            # host's own IPs, is local inside the netns → reached via the loopback
            # -T splice (the MCP's target URL is rewritten to 127.0.0.1 by
            # _rewrite_host_self_targets_to_loopback). In T2 the proxy is a
            # container, so a homelab IP is never the host itself.
            if (not is_t2) and port and any(_is_local_host_ip(ip) for ip in ips):
                _add_forward(port)
                continue
            _add_allow(host)

    # 3. Layer-supplied targets (e.g. a Codex local-LLM endpoint URL the agent
    #    dials from the sandbox). Same handling as a homelab target: loopback →
    #    forward the port (T1 same-host), routable → carve as an allow-host.
    for t in (extra_targets or []):
        if not t:
            continue
        parsed = urlparse(t if "://" in t else f"//{t}")
        host = parsed.hostname or ""
        if not host:
            continue
        if host in ("localhost", "127.0.0.1", "::1"):
            if deployment.in_docker_compose():
                logger.warning(
                    "local endpoint %r is localhost in a containerized install — "
                    "unreachable; use a LAN IP or host.docker.internal", t)
                continue
            if parsed.port:
                _add_forward(parsed.port)
        else:
            _add_allow(host)

    forwards = [forwards[0], *sorted(forwards[1:])]
    return forwards, sorted(allow_hosts)


def get_agent_mcps_all_placements(agent_name: str) -> list[McpManifest]:
    """Configuration view: every manager-enabled + platform-enabled + visible
    manifest for the agent, INCLUDING device-local MCPs regardless of placement,
    consent, or display.

    For surfaces that describe what the agent is CONFIGURED with rather than
    what attaches to one specific session: settings listings (skills tab,
    MCP-count badge) and credential provisioning / writeback. A device-local
    MCP must still appear here (annotated "needs a remote machine / capability
    grant") instead of vanishing when no target is in context. Session builders
    MUST use ``get_agent_mcps`` with the resolved target instead, so the
    placement / consent gate actually fires.
    """
    return _agent_base_manifests(agent_name)


# ---------------------------------------------------------------------------
# External routes (phone callers who are not platform users)
# ---------------------------------------------------------------------------

#: Exclusion context matched against a manifest's ``exclude_from`` for every
#: EXTERNAL session (a phone call in the per-caller or shared identity mode —
#: anyone who is not a platform user). Community manifests may opt out of
#: external sessions with ``"exclude_from": ["external"]``.
EXTERNAL_CONTEXT = "external"

#: MCPs that never attach to an external session regardless of what any
#: manifest declares: they manage the platform, schedule deferred execution
#: that would later run with the agent's full rights, or expose infrastructure
#: inventory. A manifest can add itself to the exclusion (above) but nothing
#: can remove a name listed here.
EXTERNAL_DENIED_MCPS = frozenset({
    "delegation-mcp", "schedules-mcp", "triggers-mcp", "notifications-mcp",
    "meetings-mcp", "agent-config-mcp", "agent-creator-mcp", "mcps-mcp",
    "ssh-hosts", "phone-mcp",
})

#: Exclusion reasons that describe the EXTERNAL rules themselves. The
#: prompt's "Unavailable Tools" list omits them on external sessions — a
#: caller-facing prompt must not enumerate the platform's management surface.
EXTERNAL_DENIED_REASON = "Not available on external routes"
EXTERNAL_CONTEXT_REASON = f"Excluded in {EXTERNAL_CONTEXT} mode"


def session_exclusion_reason(
    manifest, *, contexts: set[str] | frozenset[str], external: bool,
) -> str | None:
    """Why ``manifest`` is NOT part of a session with these ``contexts``
    (``{"phone"}``, ``{"task", "meeting"}``, …) — or None when it is.

    THE one rule every consumer applies: the session MCP config
    (``build_session_mcp_config``), the prompt catalog, the inline skills
    and the Direct-LLM skill catalog, the route preview, and the layers'
    per-MCP dir binds. ``external`` adds the ``"external"`` context AND the
    :data:`EXTERNAL_DENIED_MCPS` hard set, so prompt, config, key material
    and processes can never disagree about what a caller gets.
    """
    if external and manifest.name in EXTERNAL_DENIED_MCPS:
        return EXTERNAL_DENIED_REASON
    effective = set(contexts) | ({EXTERNAL_CONTEXT} if external else set())
    exclude_from = manifest.exclude_from or []
    # "meeting" outranks the base context (a manifest may opt out of
    # meetings alone); the external context is checked last so a manifest
    # listing both "phone" and "external" reports the base reason.
    ordered = (
        [c for c in ("meeting",) if c in effective]
        + sorted(effective - {"meeting", EXTERNAL_CONTEXT})
        + ([EXTERNAL_CONTEXT] if EXTERNAL_CONTEXT in effective else [])
    )
    for c in ordered:
        if c and c in exclude_from:
            return f"Excluded in {c} mode"
    return None


def filter_manifests_for_session(
    manifests, *, contexts: set[str] | frozenset[str], external: bool,
) -> list:
    """``manifests`` minus those :func:`session_exclusion_reason` drops."""
    return [
        m for m in manifests
        if session_exclusion_reason(m, contexts=contexts, external=external) is None
    ]


def preview_session_mcps(
    agent_name: str, *, contexts: set[str], external: bool,
) -> tuple[list[McpManifest], dict[str, str]]:
    """Manifest-only view of what a session with ``contexts`` would attach:
    the agent's configured MCPs minus what :func:`session_exclusion_reason`
    drops. Returns ``(attached, excluded_reasons)``.

    No processes, no credential resolution — the admin route form uses it to
    show "available to callers on this line" before saving; the session
    builder applies the same rule (plus credential availability).
    """
    attached: list[McpManifest] = []
    excluded: dict[str, str] = {}
    for manifest in get_agent_mcps_all_placements(agent_name):
        reason = session_exclusion_reason(manifest, contexts=contexts, external=external)
        if reason:
            excluded[manifest.name] = reason
            continue
        attached.append(manifest)
    return attached, excluded


def device_capability_for_server(server_name: str) -> str | None:
    """Return the ``device_capability`` of the loaded MCP whose mcpServers key
    (its ``server_name``, defaulting to the manifest name) equals ``server_name``,
    else None.

    Used by the permission hook to recognise a device-control tool call
    (``mcp__<server_name>__<tool>``) and auto-approve it when the owner has
    granted that capability — the machine-grant IS the consent, so per-click
    prompts are redundant.
    """
    if not server_name:
        return None
    for manifest in _manifests.values():
        if (manifest.server_name or manifest.name) == server_name:
            return manifest.device_capability
    return None


def is_high_risk_device_tool(server_name: str, tool_name: str) -> bool:
    """True if ``tool_name`` (bare, e.g. "execute_blender_code") is declared
    high-risk by the device MCP whose server_name matches — EXCLUDED from the
    device auto-approve so it still prompts even when the capability is granted.
    RCE-class app-connector tools bypass the bash-tier system, so they must
    never be silently auto-approved.
    """
    if not server_name or not tool_name:
        return False
    for manifest in _manifests.values():
        if (manifest.server_name or manifest.name) == server_name:
            return tool_name in manifest.device_high_risk_tools
    return False


def manifest_capability_available(manifest: McpManifest) -> bool:
    """Whether the platform feature an MCP requires is currently configured.

    Gates capability-dependent built-ins out of the assignable + runtime sets
    until their backing feature exists: the transcribe MCP needs a usable STT
    provider; the phone MCP needs usable call STT+TTS providers.
    ``requires_capability`` is None for almost every MCP (→ always available, no
    DB hit). An unknown token fails closed (the misconfigured MCP stays hidden).
    """
    cap = manifest.requires_capability
    if not cap:
        return True
    from services.media import audio_service  # lazy — avoid an import cycle at module load
    if cap == "audio_transcribe":
        return audio_service.transcribe_capability_available()
    if cap == "audio_tts":
        return audio_service.tts_capability_available()
    if cap == "phone_calls":
        return audio_service.phone_calls_available()
    logger.warning(
        "MCP %s declares unknown requires_capability=%r — hiding it", manifest.name, cap
    )
    return False


def network_access_enabled(manifest: McpManifest) -> bool:
    """Whether this MCP's per-MCP internal-network access toggle is on.

    Only meaningful for MCPs that declare ``network_targets``. The admin
    override lives in ``mcp_config_values`` under the ``_network_access``
    control key (mirrors ``_hosted_service_mode``); absent → the manifest
    default (``network_access_default``, on by default).
    """
    if not manifest.network_targets:
        return False
    val = mcp_store.get_mcp_config_values(manifest.name).get("_network_access")
    if val is None:
        return manifest.network_access_default
    return val == "true"


def _extract_host_port(
    value: str, port_key_val: str | None, port_default: int | None,
) -> tuple[str, int | None] | None:
    """Parse a config value (URL or bare ``host`` / ``host:port``) → (host, port).

    Schemeless bare hosts (UNIFI_HOST, ssh ``host``) parse via a ``//`` prefix
    so ``urlparse`` populates netloc; a value with a scheme is parsed as-is.
    Port falls back to ``port_key_val`` then ``port_default``.
    """
    from urllib.parse import urlparse
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = parsed.hostname or ""
    port = parsed.port
    if not host:
        return None
    if port is None:
        if port_key_val:
            try:
                port = int(str(port_key_val).strip())
            except (TypeError, ValueError):
                port = None
        if port is None:
            port = port_default
    return host, port


def enumerate_mcp_network_targets(
    manifest: McpManifest, agent_name: str,
    *, user_sub: str = "", task_scope: str = "user",
) -> list[tuple[str, int | None]]:
    """Resolve the (host, port) internal-network targets an MCP is wired to dial.

    Reuses the same stores the session env-build reads (admin config, per-agent
    instances, the session user's credential, infra credential) so the carved
    hosts are exactly what the MCP connects to. Returns [] when nothing is
    configured (→ no carve-out).
    """
    if not manifest.network_targets:
        return []
    from services.oauth import credential_resolver
    from storage.identity import credential_store

    out: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()

    def _add(values: dict | None, decl: NetworkTargetDecl) -> None:
        raw = (values or {}).get(decl.host_key)
        if not raw:
            return
        port_val = (values or {}).get(decl.port_key) if decl.port_key else None
        hp = _extract_host_port(raw, port_val, decl.port_default)
        if hp and hp not in seen:
            seen.add(hp)
            out.append(hp)

    for decl in manifest.network_targets:
        try:
            if decl.source == "config":
                _add(mcp_store.get_mcp_config_values(manifest.name), decl)
            elif decl.source == "instance":
                # All authorized instances — superset of the env-delivery pick,
                # and the exact set written for config_file MCPs (ssh hosts.json).
                for inst in mcp_store.get_mcp_instances_for_agent(manifest.name, agent_name):
                    _add(inst.get("field_values"), decl)
            elif decl.source == "per_user_credential":
                if user_sub:
                    ref = credential_resolver.pick_account(
                        manifest.name, agent_name, user_sub=user_sub,
                    )
                    if ref:
                        _add(credential_store.get_user_credentials(
                            user_sub, manifest.name, ref.label,
                        ), decl)
            elif decl.source == "infra_credential":
                _add(credential_store.get_infra_credentials(manifest.name), decl)
        except Exception as e:  # noqa: BLE001 — a bad target must never break spawn
            logger.warning(
                "network_targets: resolving %s for MCP %s failed: %s",
                decl.host_key, manifest.name, e,
            )
    return out


def _rewrite_host_self_targets_to_loopback(
    manifest: McpManifest, env: dict, agent_name: str,
    *, user_sub: str = "", task_scope: str = "user",
) -> None:
    """Rewrite a homelab MCP's configured target host → 127.0.0.1 (in place) when
    it points at one of the proxy HOST's own IPs — a service co-located with the
    proxy (T1). That IP is local inside the agent's isolated netns, so it's
    reached via the loopback ``-T`` splice (``resolve_sandbox_egress`` forwards
    the port), and the agent must dial 127.0.0.1. No-op in T2 (the proxy is a
    container — homelab IPs route via the NAT carve), when the per-MCP toggle is
    off, or for non-host targets.
    """
    if deployment.in_docker_compose():
        return
    if not manifest.network_targets or manifest.placement != "any":
        return
    if not manifest_capability_available(manifest) or not network_access_enabled(manifest):
        return
    for decl in manifest.network_targets:
        val = env.get(decl.host_key)
        if not val:
            continue
        hp = _extract_host_port(val, None, None)
        if not hp:
            continue
        host = hp[0]
        if host == "::1":
            env[decl.host_key] = _ipv4_loopback(val)
            continue
        if host in ("localhost", "127.0.0.1"):
            continue
        try:
            ips = _resolve_to_ips(host)
            if ips and any(_is_local_host_ip(ip) for ip in ips):
                env[decl.host_key] = val.replace(host, "127.0.0.1", 1)
        except Exception as e:  # noqa: BLE001 — never break spawn on a bad target
            logger.warning(
                "network_targets: host-self rewrite of %s for MCP %s failed: %s",
                decl.host_key, manifest.name, e,
            )


def _ipv4_loopback(value: str) -> str:
    """``value`` with its ``::1`` host written ``127.0.0.1``: the sandbox has
    no IPv6 loopback (``scripts/oto-sandbox-net``), so a host service reached
    through the loopback splice is dialed over IPv4."""
    if value.strip() == "::1":
        return "127.0.0.1"
    return value.replace("[::1]", "127.0.0.1", 1)


def loopback_if_host_self(url: str) -> str:
    """Rewrite a URL's host → 127.0.0.1 when it is one of the proxy HOST's own
    IPs (T1) — a service co-located with the proxy, reached via the loopback
    splice. Used for the Codex local-LLM endpoint (dialed from the sandbox like
    the homelab MCPs). No-op in T2, for loopback, or for remote hosts. See
    resolve_sandbox_egress (which forwards the port).
    """
    if not url or deployment.in_docker_compose():
        return url
    from urllib.parse import urlparse
    host = urlparse(url if "://" in url else f"//{url}").hostname or ""
    if host == "::1":
        return _ipv4_loopback(url)
    if not host or host in ("localhost", "127.0.0.1"):
        return url
    with contextlib.suppress(Exception):
        if any(_is_local_host_ip(ip) for ip in _resolve_to_ips(host)):
            return url.replace(host, "127.0.0.1", 1)
    return url


def get_visible_mcps_for_agent(agent_name: str) -> list[McpManifest]:
    """Return manifests visible to this agent regardless of manager-enablement.

    Used by the agent settings MCPs tab to render the unified list. Shows ALL
    MCPs the agent could enable (auto-mode + admin-authorized explicit-mode),
    platform-enabled. The API endpoint joins this with the manager-enabled set
    to compute the per-row `enabled` flag.
    """
    state_enabled = mcp_store.get_all_mcp_states()
    visible_explicit = mcp_store.get_visible_explicit_mcps(agent_name)

    result = []
    for name, manifest in _manifests.items():
        if not state_enabled.get(name, False):
            continue
        if not manifest_capability_available(manifest):
            continue
        if manifest.assignment_mode == "auto":
            result.append(manifest)
        elif manifest.assignment_mode == "explicit" and name in visible_explicit:
            result.append(manifest)
    return result


def get_unauthorized_explicit_mcps_for_agent(agent_name: str) -> list[McpManifest]:
    """The complement of :func:`get_visible_mcps_for_agent` within the
    platform-enabled set: explicit-mode MCPs installed and enabled on the
    platform where NO instance authorizes this agent yet.

    These are the MCPs a manager can *request* (the admin attaches the agent
    to an instance on approval) but cannot self-enable. Auto-mode MCPs never
    appear here — they are always visible once platform-enabled.
    Platform-disabled MCPs are excluded on purpose: the admin turned them
    off deliberately, so they are not offered for request either.
    """
    state_enabled = mcp_store.get_all_mcp_states()
    visible_explicit = mcp_store.get_visible_explicit_mcps(agent_name)

    result = []
    for name, manifest in _manifests.items():
        if not state_enabled.get(name, False):
            continue
        if not manifest_capability_available(manifest):
            continue
        if manifest.assignment_mode == "explicit" and name not in visible_explicit:
            result.append(manifest)
    return result


def _hosted_schema(m) -> dict | None:
    """Serialize the manifest's hosted block (oauth_app / api_key_relay) for the
    dashboard. Independent of credential type — an MCP can be credentials.type
    'none' (API keys via instances, e.g. image-search / image-gen) and still
    offer api_key_relay, so this MUST be attached on the type='none' path too."""
    if not m.hosted:
        return None
    out: dict = {}
    if m.hosted.oauth_app:
        out["oauth_app"] = {
            "available": m.hosted.oauth_app.available,
            "default_mode": m.hosted.oauth_app.default_mode,
        }
    if m.hosted.api_key_relay:
        out["api_key_relay"] = {
            "available": m.hosted.api_key_relay.available,
            "default_mode": m.hosted.api_key_relay.default_mode,
            "relay_path": m.hosted.api_key_relay.relay_path,
            "min_balance_to_enable_usd": m.hosted.api_key_relay.min_balance_to_enable_usd,
            "billing_setup_url": m.hosted.api_key_relay.billing_setup_url,
        }
    return out or None


def get_credential_schema(name: str) -> dict | None:
    """Return credential config dict from the manifest's credentials section."""
    m = _manifests.get(name)
    if not m:
        return None
    cred = m.credentials
    if cred.type == "none":
        # `hosted` is independent of credential type — surface it even here
        # (image-gen is type 'none' but offers api_key_relay).
        out: dict[str, Any] = {"type": "none"}
        h = _hosted_schema(m)
        if h:
            out["hosted"] = h
        return out

    result: dict[str, Any] = {
        "type": cred.type,
        "label": cred.label,
        "description": cred.description,
        "fields": cred.fields,
    }
    if cred.server_config_fields:
        result["server_config_fields"] = cred.server_config_fields
    if cred.service_account:
        result["has_service_account"] = True
    if cred.oauth:
        result["oauth"] = True
        result["oauth_services"] = cred.oauth.get("services", [])
        # Metadata exposed for the dashboard's multi-account UX +
        # provider-aware affordances (capabilities consent UI, bearer info).
        # `flows` is always a list (validator enforces non-empty). For
        # single-flow MCPs the list has one element; multi-flow MCPs like
        # github-mcp declare `["authorization_code", "personal_access_token"]`.
        flows = list(cred.oauth.get("flows", []))
        result["oauth_meta"] = {
            "provider_id": cred.oauth.get("provider_id", ""),
            "supports_multi_account": cred.oauth.get(
                "supports_multi_account", True,
            ),
            "registered_app_required": cred.oauth.get(
                "registered_app_required", False,
            ),
            "bearer_required": cred.oauth.get("bearer_required", False),
            "proposed_hosts": cred.oauth.get("proposed_hosts", []),
            "flows": flows,
            "pat_instructions_url": cred.oauth.get("pat_instructions_url", ""),
            # Optional manifest-supplied placeholder for the PAT/API-key
            # textarea (e.g. github's "ghp_…"). Empty = dashboard's neutral
            # fallback — without this, every PAT provider showed GitHub's.
            "pat_placeholder": cred.oauth.get("pat_placeholder", ""),
            # Provider-specific copy for the PAT flow-picker option
            # (github: "…in your GitHub account settings (Developer
            # settings)…"). Empty = dashboard's generic fallback.
            "pat_description": cred.oauth.get("pat_description", ""),
        }
        # Manifest values only (the route is open to every role and runs
        # on the loop): the install's registrations are an admin route.
        block = cred.oauth.get("authorization_server")
        if isinstance(block, dict):
            from urllib.parse import urlsplit
            result["oauth_meta"]["authorization_server"] = {
                "registration": block.get("registration", "dynamic"),
                "issuer": block.get("issuer", "") or "",
                "resource_host": (
                    urlsplit(m.server.url_template or "").hostname or ""
                ).lower(),
                "confidential": bool(block.get("confidential", False)),
                "accepts_app_tokens": bool(block.get("accepts_app_tokens", False)),
            }
        if cred.app_credential_fields:
            result["app_credential"] = cred.oauth.get("app_credential", "")
            result["app_credential_fields"] = cred.app_credential_fields
    if cred.ui_type:
        result["ui_type"] = cred.ui_type
    h = _hosted_schema(m)
    if h:
        result["hosted"] = h
    return result


def get_all_credential_schemas() -> dict[str, dict]:
    """Return all credential schemas keyed by MCP name."""
    result = {}
    for name, m in _manifests.items():
        schema = get_credential_schema(name)
        if schema:
            result[name] = schema
    return result


def build_oauth_scopes(mcp_name: str, services: list[str]) -> list[str]:
    """Combine the manifest's ``base_scopes`` with the scopes for each
    requested service. Returns a deduplicated list preserving declaration
    order. Unknown services are silently dropped.

    Reads from any manifest's ``credentials.oauth`` block. Used by
    ``api/auth/oauth.py::oauth_start`` and by the test suite.
    """
    m = get_manifest(mcp_name)
    if m is None or not m.credentials.oauth:
        return []
    oauth = m.credentials.oauth
    base = list(oauth.get("base_scopes", []))
    by_key = {s["key"]: s.get("scopes", []) for s in oauth.get("services", [])}

    seen: set[str] = set()
    result: list[str] = []
    for s in base:
        if s not in seen:
            seen.add(s)
            result.append(s)
    for svc in services:
        for s in by_key.get(svc, []):
            if s not in seen:
                seen.add(s)
                result.append(s)
    return result


def get_credentials_dirs(mcp_name: str) -> list[tuple[str, str]]:
    """Return [(env_var, subpath)] for every path_env entry that resolves to a
    credentials_dir role — including multi-value entries that contain such an
    item.

    Used by the OAuth flow (``services/oauth/credential_resolver.py``) and the
    token-file delivery (``core/credentials/credential_files.py``) to know:
      - which env var carries the credentials path (so the MCP gets a value)
      - which subpath the delivered directory carries

    For shorthand entries with role ``credentials_dir``, we return
    ``(env_var, decl.subpath)``. For multi-value entries that include one or
    more ``credentials_dir`` items, we return one tuple per item; the env
    var is shared (callers should be aware that copying tokens to multiple
    subpaths under the same env var would be unusual but supported).
    """
    m = _manifests.get(mcp_name)
    if m is None:
        return []
    out: list[tuple[str, str]] = []
    for env_var, decl in m.path_env.items():
        if decl.is_multi:
            for item in decl.values:
                if item.role == "credentials_dir":
                    out.append((env_var, item.subpath))
        elif decl.role == "credentials_dir":
            out.append((env_var, decl.subpath))
    return out


# Manifests where the agent-facing description is operational metadata that
# would just be noise inside the agent's MCP catalog. mcps-mcp is meta (it
# talks about other MCPs and shouldn't self-advertise); add to this set if
# more emerge.
_SUPPRESS_FROM_CATALOG: set[str] = {"mcps-mcp"}


def _first_sentence(text: str) -> str:
    """Return the first sentence of a description (period-delimited).

    Falls back to the whole text if there's no period. Used to trim
    manifest descriptions to a one-line catalog entry — keeps operational
    notes ("Permission-aware: viewers see zero tools" and similar) out
    of the agent's view while preserving the meaningful lede.
    """
    text = (text or "").strip()
    if not text:
        return ""
    idx = text.find(". ")
    if idx == -1:
        # No multi-sentence — strip a single trailing period if present
        return text.rstrip(".")
    return text[:idx]


def _is_http_transport(manifest) -> bool:
    """A sidecar (streamable-HTTP) MCP — the transport the phone Direct-LLM
    manager does not connect. ``sse`` / ``stdio`` / ``none`` are not."""
    return getattr(getattr(manifest, "server", None), "transport", "") == "http"


def build_available_mcps_section(
    agent_name: str,
    *,
    context: str = "",
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    skip_http_mcps: bool = False,
    external: bool = False,
    user_role: str | None = None,
) -> str:
    """Build the ``# Available Tools (MCPs)`` prompt section.

    Lists each enabled MCP as one bullet — ``**{label}** (`{slug}`) — {desc}``.
    Sourced from ``get_agent_mcps`` (visible + manager-enabled + platform-
    enabled), filtered to skip:
      - ``mcps-mcp`` (meta MCP — self-reference adds no value).
      - Manifests whose ``exclude_from`` matches the current session
        context (e.g. ``display-mcp`` is phone-excluded).

    Returns an empty string when no MCPs would be listed (the caller
    omits the section heading entirely in that case).

    Args:
        agent_name: agent slug.
        context: session context string — ``"dashboard"`` / ``"phone"`` /
            ``"task"`` / ``"terminal"``. Empty string skips context filtering
            (defense-only — upstream usually filters).
        placement: the session's resolved placement, forwarded to
            ``get_agent_mcps`` so the prompt catalog only lists device-local
            MCPs the session can actually use. Fail-closed defaults.
        skip_http_mcps: drop manifests with ``server.transport == "http"`` —
            the sidecar MCPs a phone Direct-LLM session never connects
            (``core/layers/direct/mcp.py`` skips them), so the prompt does
            not advertise tools the session cannot reach.
        user_role: the session's person's role on the agent; None lists
            every audience (no person known).
    """
    manifests = get_agent_mcps(agent_name, placement=placement) or []
    if skip_http_mcps:
        manifests = [m for m in manifests if not _is_http_transport(m)]
    # ``user_role`` (the session's person) drops the manifests whose
    # ``audience`` the person is below, as the session config does.
    manifests = [m for m in manifests if audience_admits(getattr(m, 'audience', ''), user_role)]
    # Sort alphabetically by label for deterministic output — registry scan
    # order is otherwise filesystem-dependent and can drift across reinstalls.
    manifests_sorted = sorted(
        manifests, key=lambda m: (m.label or m.name).lower()
    )
    rows: list[str] = []
    for m in manifests_sorted:
        if m.name in _SUPPRESS_FROM_CATALOG:
            continue
        # Standalone skill packages are context-only carriers of skills —
        # advertising them as "MCP servers ... tools you can call" would put
        # a tool-less server in front of the agent. Their skills surface via
        # the skills pipeline (inline or the CLI's own index) instead.
        if m.category == "skill":
            continue
        if session_exclusion_reason(
            m, contexts={context} if context else set(), external=external,
        ):
            continue
        label = (m.label or m.name).strip()
        desc = _first_sentence(m.description)
        if desc:
            rows.append(f"- **{label}** (`{m.name}`) — {desc}")
        else:
            rows.append(f"- **{label}** (`{m.name}`)")
    if not rows:
        return ""
    header = (
        "# Available Tools (MCPs)\n\n"
        "You have these MCP servers enabled in this session — each "
        "provides a set of tools you can call:\n\n"
    )
    footer = (
        "\n\nDetailed usage instructions for each MCP follow in "
        "`# MCP Tool Skills` below."
    )
    return header + "\n".join(rows) + footer


def audience_admits(audience: str, user_role: str | None) -> bool:
    """Whether a session at ``user_role`` is in ``audience`` (the tier names
    of ``auth/roles.py``; an empty audience admits everyone). ``None`` is
    "no person known": the caller did not say, nothing is filtered."""
    if user_role is None or not audience:
        return True
    from auth import roles
    gate = {
        "owner": roles.can_manage,
        "editor": roles.can_edit,
        "workspace": roles.can_write_workspace,
    }.get(audience)
    return bool(gate and gate(user_role))


def skill_audience_admits(skill_def, user_role: str | None) -> bool:
    """:func:`audience_admits` for a skill's ``audience``."""
    return audience_admits(getattr(skill_def, "audience", ""), user_role)


def audience_refusal(manifest, *, user_role: str | None, task_scope: str) -> str | None:
    """Why a manifest with an ``audience`` is not part of this session, or
    None: a person below the tier in a user-scope session. An agent-scope
    session is the agent acting as itself and is admitted; a builder that
    names no role (an empty string) is "no person known" and admitted, as
    the skills' gate reads it."""
    if not getattr(manifest, "audience", "") or task_scope != "user":
        return None
    if audience_admits(manifest.audience, user_role or None):
        return None
    return f"{manifest.label} is for the {manifest.audience} tier of this agent and above"


def get_skills_for_agent(
    agent_name: str,
    context: str = "dashboard",
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    skip_http_mcps: bool = False,
    external: bool = False,
    user_role: str | None = None,
) -> list[tuple[str, str, str]]:
    """Return (skill_id, prompt_content, loading) for an agent, context-filtered.

    Reads agent_skills DB table, checks exclude_from, loads skill files from
    disk with any SKILL.md frontmatter stripped (frontmatter is index
    metadata for the CLI skills dir, never prompt text). Context is the
    session kind's name (``core/session/session_kind.py``) or the ``terminal``
    placement — matched against the skill's ``exclude_from``; ``""`` skips
    the exclusion filter.

    ``loading`` is the manifest stamp (``"always"`` | ``"on_demand"``) — the
    caller decides what to inline vs. hand to the CLI's own progressive
    disclosure.

    ``placement`` forwards to ``get_agent_mcps`` so a device-local MCP's
    skill text is dropped on sessions that can't run it (fail-closed: the
    local placement by default). ``skip_http_mcps`` drops the
    skills of sidecar (HTTP) MCPs — see ``build_available_mcps_section``.
    ``user_role`` (the session's role) drops the skills whose ``audience``
    the role fails (``skill_audience_admits``); ``None`` filters nothing.
    """
    from services.mcp.skill_format import strip_frontmatter

    result: list[tuple[str, str, str]] = []
    for manifest, skill_def in _iter_agent_skills(
        agent_name, context, placement=placement,
        skip_http_mcps=skip_http_mcps,
        external=external, user_role=user_role,
    ):
        # The file must be a regular file inside this MCP's own folder; a
        # value that escapes it is never read.
        skill_path = _mmp.resolve_skill_file(manifest.mcp_dir, skill_def.file)
        if skill_path is None:
            logger.warning(
                "Skill %s of %s names %r, which is not a file inside its "
                "folder; skipped", skill_def.id, manifest.name, skill_def.file,
            )
            continue
        try:
            content = strip_frontmatter(skill_path.read_text())
            result.append((skill_def.id, content, skill_def.loading))
        except Exception as e:
            logger.warning("Failed to read skill %s: %s", skill_path, e)

    return result


def _iter_agent_skills(
    agent_name: str,
    context: str,
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    skip_http_mcps: bool = False,
    external: bool = False,
    user_role: str | None = None,
):
    """``(manifest, skill_def)`` for every ENABLED skill of the agent's
    assigned MCPs that is not excluded from ``context`` — the one filter
    behind the inline skills and the Direct-LLM skill catalog. DB rows
    override the manifest defaults (enabled gate + exclude_from copy);
    ``""`` as context excludes nothing; ``skip_http_mcps`` drops sidecar
    MCPs (phone Direct-LLM sessions never connect them); ``external`` drops
    the MCPs an external session never attaches (their skills with them) and
    honours a skill-level ``"external"`` exclusion; ``user_role`` drops the
    skills whose audience the role fails."""
    db_skills = mcp_store.get_agent_skills(agent_name)
    skill_map: dict[str, dict] = {s["skill_id"]: s for s in db_skills}

    assigned_mcps = get_agent_mcps(agent_name, placement=placement)
    for manifest in assigned_mcps:
        if skip_http_mcps and _is_http_transport(manifest):
            continue
        if session_exclusion_reason(
            manifest, contexts={context} if context else set(), external=external,
        ):
            continue
        for skill_def in manifest.skills:
            if not skill_audience_admits(skill_def, user_role):
                continue
            db_entry = skill_map.get(skill_def.id)
            if db_entry:
                if not db_entry["enabled"]:
                    continue
                exclude_from = db_entry["exclude_from"]
            else:
                exclude_from = skill_def.default_exclude_from
            if context in exclude_from:
                continue
            if external and EXTERNAL_CONTEXT in exclude_from:
                continue
            yield manifest, skill_def


def get_skill_catalog_for_agent(
    agent_name: str,
    context: str = "dashboard",
    *,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    skip_http_mcps: bool = False,
    external: bool = False,
    user_role: str | None = None,
) -> list[tuple[str, str]]:
    """``(skill_id, description)`` of the agent's enabled ON-DEMAND skills in
    this context, sorted by id — the ``# Skills`` catalog of a Direct-LLM
    prompt. The CLI engines index their materialized skills dir themselves;
    the direct layer has a client-side ``Skill`` builtin instead and needs
    the names in the prompt to know what it can load. Same filters as the
    inline path (``_iter_agent_skills``), the audience included."""
    rows = [
        (skill_def.id, skill_def.description or "")
        for _m, skill_def in _iter_agent_skills(
            agent_name, context, placement=placement,
            skip_http_mcps=skip_http_mcps,
            external=external, user_role=user_role,
        )
        if skill_def.loading == "on_demand"
    ]
    return sorted(rows)


def find_skill_provider(skill_id: str) -> McpManifest | None:
    """The manifest that declares ``skill_id`` (first match in registry
    order — collisions are rejected at install and warned at scan, so at
    most one live provider exists)."""
    for manifest in _manifests.values():
        for skill_def in manifest.skills:
            if skill_def.id == skill_id:
                return manifest
    return None


def _role_of_username(username: str, agent_name: str) -> str | None:
    """The person's effective role on the agent, read from the users table;
    ``None`` when the username names nobody (an unknown person gets the
    unfiltered set, as every caller that passes no username does)."""
    from auth import providers
    from storage.identity import db_users
    sub = db_users.get_user_sub_by_username(username)
    if not sub:
        return None
    return providers.effective_role_of(sub, agent_name)


def get_on_demand_skills_for_materialization(
    agent_name: str,
    *,
    username: str | None = None,
) -> list[tuple[str, Path, str, str, str]]:
    """The on-demand skills to materialize into a session CLI skills dir.

    Returns ``(skill_id, source_path, package_name, package_version,
    description)`` for every ENABLED ``loading: on_demand`` skill of the
    agent's base manifest set.

    Deliberately **context-free and placement-free** (all-placements set, no
    ``exclude_from`` filtering): the materialized ``skills/`` dir is shared
    by every session context that uses the same config dir (chat + task +
    phone on the workspace scope), and concurrent config-dir ensures MUST
    compute the identical set or reconciliation ping-pongs — one session
    start deleting the folders another just wrote (plan §2.1). Per-context
    precision lives on the INLINE path, where the token cost is; an
    on-demand skill's residual footprint is a one-line CLI index entry.

    ``username`` names the person whose own config dir is being
    materialized: a skill with an ``audience`` their role on the agent
    fails is left out. One person has one role, so every session that
    shares that dir computes the same set; the agent-level dir (no
    username) keeps the role-free set.
    """
    user_role = _role_of_username(username, agent_name) if username else None
    db_skills = {s["skill_id"]: s for s in mcp_store.get_agent_skills(agent_name)}
    out: list[tuple[str, Path, str, str, str]] = []
    for manifest in get_agent_mcps_all_placements(agent_name):
        for skill_def in manifest.skills:
            if skill_def.loading != "on_demand":
                continue
            if not skill_audience_admits(skill_def, user_role):
                continue
            row = db_skills.get(skill_def.id)
            if row and not row["enabled"]:
                continue
            source = _mmp.resolve_skill_file(manifest.mcp_dir, skill_def.file)
            if source is None:
                logger.warning(
                    "Skill %s of %s names %r, which is not a file inside its "
                    "folder; not materialized", skill_def.id, manifest.name,
                    skill_def.file,
                )
                continue
            out.append((
                skill_def.id,
                source,
                manifest.name,
                manifest.version,
                skill_def.description,
            ))
    return out


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

        # Config values from DB (admin-configured operational settings).
        # `_`-prefixed keys are internal control state (e.g.
        # `_hosted_service_mode`, `_managed_instance_deleted`) and MUST NOT
        # leak into the MCP subprocess env.
        config_vals = mcp_store.get_mcp_config_values(manifest.name)
        env.update({k: v for k, v in config_vals.items() if not k.startswith("_")})

        if env:
            entry["env"] = env

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
) -> tuple["TokenRef | None", str | None]:
    """The token-file reference a bearer MCP's account resolves to for this
    session, or the reason it cannot: the person's bound account in user
    scope, the agent's service binding otherwise (the one rule the resolver
    applies). The file is read once here so a dead account is refused with
    its reconnect wording instead of failing on every call; the gateway
    re-reads it per request."""
    from core.credentials.mcp_gateway import TokenRef
    from services.oauth import credential_resolver, oauth_account_store
    from storage import database as _db

    provider_id = (manifest.credentials.oauth or {}).get("provider_id", "")
    if user_sub and task_scope == "user":
        ref = credential_resolver.pick_account(manifest.name, agent_name, user_sub=user_sub)
        owner_sub = user_sub
    else:
        ref = credential_resolver.pick_account(manifest.name, agent_name)
        owner_sub = ref.owner_sub if ref else ""
    username = _db.get_username_by_sub(owner_sub) if owner_sub else ""
    if ref is None or not username:
        return None, f"{manifest.label}: no account is connected for this session"
    token_dir = oauth_account_store.get_token_dir(username, provider_id=provider_id)
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
    if not bearer and not header_block:
        return None, None

    parts = urlsplit(url)
    host = parts.hostname or ""
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    proxy_local = deployment.is_proxy_local_mcp_host(host, manifest)
    if proxy_local and not _mt.is_container(manifest.server):
        return None, (
            f"{manifest.label} names a loopback host; a hosted MCP must name its "
            "vendor's host"
        )

    if bearer:
        allowlist_key = oauth.get("provider_id", "")
        token_ref, reason = _gateway_bearer_source(manifest, user_sub, agent_name, task_scope)
        if reason:
            return None, reason
        cred = GatewayCredential(
            upstream=origin, path=path, allowlist_key=allowlist_key,
            token_ref=token_ref, proxy_local=proxy_local, manifest=manifest.name,
        )
    else:
        allowlist_key = manifest.name
        key = str(header_block.get("value_from") or "")
        value = bundle_env.get(key, "")
        if not value:
            return None, (
                f"{manifest.label}: no {key} value is set for this session; enter "
                "it in Settings > Integrations or ask an admin"
            )
        cred = GatewayCredential(
            upstream=origin, path=path, allowlist_key=allowlist_key,
            header=str(header_block.get("name") or ""), prefix="", value=value,
            proxy_local=False, manifest=manifest.name, value_from=key,
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
    (cli/codex/direct/remote) swaps for a real ``create_session_token`` once the
    session_id is known. A credentialed entry already carries the same
    sentinel from ``gateway_entry``.

    No-op for: MCPs that don't opt in, stdio entries (no ``url``), or entries
    that already carry an ``Authorization`` header. Returns the
    (possibly-mutated) entry.
    """
    if (
        manifest.server.proxy_callbacks
        and entry.get("url")
        and not entry.get("headers", {}).get("Authorization")
    ):
        entry.setdefault("headers", {})["Authorization"] = SESSION_JWT_SENTINEL_BEARER
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
    "PLAYWRIGHT_MCP_EXTENSION",
    "PLAYWRIGHT_MCP_EXTENSION_TOKEN",
    "PLAYWRIGHT_MCP_USER_DATA_DIR",
    "PLAYWRIGHT_MCP_EXECUTABLE_PATH",
    "PLAYWRIGHT_MCP_BROWSER",
    "PLAYWRIGHT_MCP_CDP_ENDPOINT",
)


def _apply_browser_mode(
    entry: dict[str, Any], secret_bundles: dict, target_browser, *, unattended: bool,
) -> None:
    """Apply the target machine's browser-control mode to the ``local`` entry.

    ``own`` → ``OTO_BROWSER_MODE=own`` in the (non-secret) config env; the
    extension token rides the MCP's secret bundle (broker-fetched at spawn,
    never in the config file) with ``OTO_BROWSER_TOKEN_EXPECTED=1`` beside it
    so the wrapper can tell "no token saved" from "the fetch failed". Without
    a token, an unattended session (task / phone / meeting) gets
    ``OTO_BROWSER_UNATTENDED=1`` so the wrapper fails the first browser call
    at once instead of waiting for a click nobody will give.

    An unattended session gets the saved token only when the machine row
    opts in (``BrowserTargetSettings.unattended``, F47): a token left for an
    attended person is not spent by a task, a call or a meeting that happens
    to target the machine unless its owner chose to.
    """
    env = entry.get("env") or {}
    for key in _BROWSER_MODE_ENV_KEYS:
        env.pop(key, None)
    if target_browser is not None and target_browser.mode == "own":
        env["OTO_BROWSER_MODE"] = "own"
        token_allowed = bool(target_browser.extension_token) and (
            not unattended or getattr(target_browser, "unattended", False)
        )
        if token_allowed:
            from core.credentials.mcp_broker import SecretBundle
            env["OTO_BROWSER_TOKEN_EXPECTED"] = "1"
            bundle = secret_bundles.get("local") or SecretBundle()
            bundle.env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = target_browser.extension_token
            secret_bundles["local"] = bundle
        elif unattended:
            env["OTO_BROWSER_UNATTENDED"] = "1"
    if env:
        entry["env"] = env
    else:
        entry.pop("env", None)


def build_session_mcp_config(
    agent_name: str,
    user_sub: str | None,
    *,
    phone_mode: bool = False,
    task_mode: bool = False,
    meeting_mode: bool = False,
    interactive_local: bool = False,
    task_scope: str = "user",
    delegation_targets: list[str] | None = None,
    extra_mcps: list[str] | None = None,
    mcp_config_format: str | None = "json",   # None = JSON consumed in-process (Direct LLM)
    username: str = "",
    user_role: str = "",
    chat_id: str = "",
    task_owner: str = "",
    task_username: str = "",
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    target_browser=None,
    external: bool = False,
    only_mcps: list[str] | None = None,
    session_id: str = "",
) -> tuple[Path | None, dict[str, str], dict[str, str], dict, set]:
    """Main entry point: build a complete MCP config for a session.

    ``session_id`` writes the build into the session's own directory (the
    file keeps the identity's name), so a concurrent build of the same
    identity never rewrites it between this build and its start; without
    one the identity's shared file is written (a pooled build).

    ``only_mcps`` (CHECKS.md, the judge profile) keeps ONLY the named MCPs
    of what the agent's assignment and the gates above resolved — an
    intersection, never an addition (``[]`` = no MCP at all).

    Args:
        target_browser: the target machine's ``remote_store.BrowserTargetSettings``
            (browser-control mode + extension token) — None / dedicated
            leaves the browser entry untouched; ``own`` is applied by
            ``_apply_browser_mode``. Local sessions never carry it (the MCP
            is satellite-only).
        meeting_mode: meeting-participant session. Meetings RIDE the task lane
            (callers also pass ``task_mode=True``), so their base exclusion
            context stays ``"task"`` — this flag ADDITIONALLY matches
            ``"meeting"`` in ``exclude_from``, letting a manifest opt out of
            meetings without leaving scheduled tasks. Needed for config/prompt
            agreement: the prompt catalog and skills filter by ``client_type``
            (which IS ``"meeting"`` there), so without this a
            meeting-excluded manifest would vanish from the prompt while its
            tools still loaded.
        extra_mcps: MCP names to force-include even if not assigned to the agent
                    (e.g. meetings-mcp for meeting participants).
        mcp_config_format: "json" (Claude CLI) or "toml" (Codex CLI).
        username: session's username — passed to instance-config transforms
            so they can inject scope-aware values (e.g. ssh-server's
            ``allowedLocalPaths``).
        user_role: session's access level (viewer/manager/admin/"") —
            same purpose as username; lets transforms compute mount sets.
        chat_id, task_owner, task_username: per-session context exposed to
            stdio MCP ``agent_env`` blocks via ``${session.*}`` token
            resolution. ``task_owner``/``task_username`` are populated for
            scheduled-task and meeting sessions; empty for interactive chats.

    Returns: (config_path, credential_env_vars, exclusion_reasons, secret_bundles,
        bash_env_keys) — a 5-tuple. ``credential_env_vars`` is the NON-secret flat
        env (secrets are stripped to the per-MCP ``secret_bundles``);
        ``bash_env_keys`` are the OAuth ``env_injection`` names (GH_TOKEN/
        GIT_CONFIG_*) that stay in the CLI parent env but are listed in
        ``OTO_STRIP_KEYS`` so the interceptor drops them from MCP children.

    ``secret_bundles`` maps each included MCP's mcpServers key (server_name or
    mcp name) to a ``core.credentials.mcp_broker.SecretBundle`` carrying that one MCP's
    secret material (resolver creds + relay token + instance field_values +
    HTTP bearer). The execution layer provisions these into the broker and the
    stdio interceptor fetches them at spawn. The PURE secrets ARE stripped from
    the returned flat env + the per-server config env here (see below); the
    interceptor re-injects identical values at spawn.
    """
    from services.oauth import credential_resolver
    from core.credentials.mcp_broker import SecretBundle

    # Determine context for exclusion checks
    context = "dashboard"
    if phone_mode:
        context = "phone"
    elif task_mode:
        context = "task"
    elif interactive_local:
        # Interactive terminal: a local CLI session (Claude Code / Codex on the
        # user's own machine) — exclude device/UI MCPs that make no sense there
        # (location GPS, display image-viewer; see their manifests' exclude_from).
        context = "terminal"
    # Meeting sessions match "meeting" IN ADDITION to their base context
    # ("task" — meetings ride the task lane), so a manifest can opt out of
    # meetings alone. Kept as a union, never a replacement: manifests that
    # exclude only "task" have always been absent from meetings, and swapping
    # the context would silently re-admit them all.
    exclusion_contexts = {context, "meeting"} if meeting_mode else {context}
    # External sessions (a phone caller who is not a platform user) add the
    # "external" context AND the hard-denied management set — see
    # ``session_exclusion_reason``; the prompt builders apply the same rule.
    if external:
        exclusion_contexts.add(EXTERNAL_CONTEXT)

    # Get assigned + enabled MCPs for this agent. Device-local placement /
    # consent / display filtering happens here; the dropped device MCPs
    # come back as exclusion reasons so the prompt can surface them.
    assigned, exclusion_reasons = _get_agent_mcps_with_device_exclusions(
        agent_name, placement=placement,
    )
    is_remote = placement.is_remote

    # Force-include extra MCPs (e.g. meetings-mcp for meeting participants).
    # NOTE: extra_mcps is an engine-level escape hatch — it BYPASSES both
    # visibility (admin authorization) AND enablement (manager toggle). Only
    # use it for system-essential MCPs that must always be available regardless
    # of agent config (currently: meetings-mcp during a meeting). Adding new
    # entries here amounts to giving an MCP "always-on" status. It does NOT,
    # however, bypass the device-local placement gate — a satellite_only MCP
    # must never be force-injected onto a local session.
    if extra_mcps:
        assigned_names = {m.name for m in assigned}
        for mcp_name in extra_mcps:
            if mcp_name in assigned_names:
                continue
            manifest = get_manifest(mcp_name)
            if not manifest:
                continue
            reason = _device_placement_reason(manifest, placement=placement)
            if reason:
                exclusion_reasons[mcp_name] = reason
                continue
            assigned.append(manifest)

    if only_mcps is not None:
        keep = {str(n) for n in only_mcps}
        assigned = [m for m in assigned if m.name in keep]

    if not assigned:
        # Preserve device-placement exclusion reasons even when nothing remains.
        # MUST return the full 5-tuple (callers unpack mcp_config, credential_env,
        # excluded_mcps, secret_bundles, bash_env_keys) — a 4-tuple here crashes any
        # session that resolves to zero MCPs ("not enough values to unpack").
        return None, {}, exclusion_reasons, {}, set()

    # Resolve credentials for all assigned MCPs
    cred_result = credential_resolver.resolve_credentials(
        agent_name, user_sub,
        task_scope=task_scope,
    )

    # The flat env that reaches the CLI/daemon process + the codex TOML is
    # stripped of PURE MCP secrets — each MCP gets those via its broker bundle
    # instead. env_injection (GH_TOKEN) + paths + OTO_* STAY (bash + the MCP need
    # them in-process).
    flat_env = {
        k: v for k, v in cred_result.env_vars.items()
        if k not in cred_result.secret_keys
    }

    # Build mcpServers dict
    servers: dict[str, dict] = {}
    # Credential broker: per-MCP secret bundles, keyed by srv_key.
    secret_bundles: dict[str, SecretBundle] = {}

    for manifest in assigned:
        mcp_name = manifest.name

        # Context exclusion (manifest exclude_from + the external hard set) —
        # the ONE rule shared with the prompt builders and the route preview.
        context_reason = session_exclusion_reason(
            manifest, contexts=exclusion_contexts, external=external,
        )
        if context_reason:
            exclusion_reasons[mcp_name] = context_reason
            continue

        # The manifest's audience: a person below the tier it names is not
        # handed the MCP (the prompt, the egress carve and the sandbox agree).
        audience_reason = audience_refusal(manifest, user_role=user_role, task_scope=task_scope)
        if audience_reason:
            exclusion_reasons[mcp_name] = audience_reason
            continue

        # Check credential exclusion
        if mcp_name in cred_result.excluded_mcps:
            exclusion_reasons[mcp_name] = cred_result.exclusion_reasons.get(
                mcp_name, "Credentials not configured"
            )
            continue

        # Admin-paired-only MCPs (ssh-hosts): the tool surface exists exactly
        # where the MCP's session data (SSH keys) is delivered — locally and
        # on admin-paired satellites. A user-paired machine gets a visible
        # exclusion instead (mirrors the agent-scope-credentials rule).
        if (manifest.remote_policy == "admin_paired_only"
                and is_remote and not placement.admin_paired):
            exclusion_reasons[mcp_name] = (
                f"{manifest.label} is unavailable on this machine — "
                "it is delivered only to admin-paired machines"
            )
            continue

        # Context-only MCPs (transport "none"): no server process, so nothing
        # to emit into mcpServers. Skills, dynamic prompt context, instance
        # authorization and network_targets all apply through their own
        # paths. On REMOTE sessions they are allowed only on ADMIN-PAIRED
        # targets — their session data rides the session-file broker there;
        # key material never reaches a user-paired satellite.
        if manifest.server.transport == "none":
            if is_remote and not placement.admin_paired:
                exclusion_reasons[mcp_name] = (
                    f"{manifest.label} is unavailable on this machine — "
                    "its key material is delivered only to admin-paired "
                    "machines"
                )
            continue

        # Build server config (session ctx populated for ${session.*} token
        # resolution in manifest agent_env declarations)
        session_ctx = {
            "chat_id": chat_id,
            "task_owner": task_owner,
            "task_username": task_username,
            "task_scope": task_scope if (task_owner or task_mode) else "",
        }
        server_entry = resolve_server_config(
            manifest, agent_name,
            mcp_config_format=mcp_config_format,
            session_ctx=session_ctx,
        )

        # Credential broker: collect THIS MCP's secret material into a
        # bundle the stdio interceptor fetches at spawn. Resolver creds come
        # from the per-MCP attribution; the relay token + instance
        # field_values are added at their injection points below; an HTTP
        # MCP's credential is the bundle's gateway half (``gateway_entry``).
        # The PURE secrets are stripped from the flat env + config below;
        # the interceptor re-fetches them from the broker at spawn.
        # Bundle carries ONLY this MCP's PURE secrets — NOT the
        # credentials_dir paths or env_injection in env_by_mcp, which stay in the
        # flat env (the broker fetch would otherwise overwrite the sandbox-virtual
        # path with a host path, breaking credentials_dir MCPs).
        bundle_env: dict[str, str] = {
            k: v for k, v in cred_result.env_by_mcp.get(mcp_name, {}).items()
            if k in cred_result.secret_keys
        }
        # Agent_env secrets: a manifest agent_env value resolved from a
        # SECRET template (see _SECRET_TEMPLATE_TOKENS — empty today) must
        # not stay in the session config file either — the resolver strip
        # above only covers credential-store secrets. Move the resolved value
        # into the bundle; the interceptor re-injects it at spawn.
        for _env_key, _template in (manifest.agent_env or {}).items():
            if any(tok in str(_template) for tok in _SECRET_TEMPLATE_TOKENS):
                _val = (server_entry.get("env") or {}).pop(_env_key, "")
                if _val:
                    bundle_env[_env_key] = _val
        # Per-session JWT for Docker/HTTP MCPs that call back to the proxy hooks
        # (manifest ``server.proxy_callbacks``; today only file-tools). See
        # _inject_session_jwt_sentinel.
        server_entry = _inject_session_jwt_sentinel(server_entry, manifest)

        # Instance-based MCP config (generalized — replaces SSH-specific block)
        if manifest.instances:
            inst_cfg = manifest.instances
            if inst_cfg.delivery == "config_file":
                # config_file delivery is platform-host-only for now: the
                # generated instance file (and, for ssh-server, the private
                # keys its ``privateKey`` entries point at) lives on the proxy
                # filesystem and is NOT delivered to satellites — the path
                # would ship verbatim and dangle there, so the MCP spawns
                # broken with no visible reason. Exclude with an explicit
                # reason instead (surfaced in the prompt's "# Excluded MCPs").
                # Secure remote delivery (admin-paired machines only) is a
                # planned framework extension.
                if is_remote:
                    exclusion_reasons[mcp_name] = (
                        f"{manifest.label} is unavailable on remote machines — "
                        "its instance config and key files stay on the "
                        "platform host"
                    )
                    continue
                inst_config_dir = config.SESSIONS_DIR / "user-mcp-configs"
                inst_config_dir.mkdir(parents=True, exist_ok=True)
                inst_path = _generate_instance_config_file(
                    mcp_name, agent_name, inst_config_dir, manifest,
                    username=username, user_role=user_role,
                )
                if not inst_path:
                    exclusion_reasons[mcp_name] = (
                        f"No {manifest.label} instances assigned to this agent"
                    )
                    continue
                # Override config file arg if declared in manifest
                if inst_cfg.config_file_arg and "args" in server_entry:
                    new_args = []
                    skip_next = False
                    for arg in server_entry["args"]:
                        if skip_next:
                            skip_next = False
                            continue
                        if arg == inst_cfg.config_file_arg:
                            new_args.append(arg)
                            new_args.append(str(inst_path))
                            skip_next = True
                        else:
                            new_args.append(arg)
                    server_entry["args"] = new_args
            elif inst_cfg.delivery == "env":
                # Env delivery picks ONE instance via deterministic precedence:
                # explicit-agent assignment beats catch-all (assigned_to_all).
                # See get_instance_for_agent_env_delivery for full ordering.
                chosen = mcp_store.get_instance_for_agent_env_delivery(
                    mcp_name, agent_name,
                )
                if not chosen:
                    exclusion_reasons[mcp_name] = (
                        f"No {manifest.label} instance authorized for this agent"
                    )
                    continue
                akr = manifest.hosted.api_key_relay if manifest.hosted else None
                if chosen.get("hosted_mode") == "hosted" and akr and akr.available:
                    # HOSTED api_key_relay: point the MCP at the relay (NOT the
                    # vendor) with a per-USER token (the session user, even
                    # though the system instance is assigned_to_all) — this is
                    # the seam per-user metering plugs into. The relay
                    # holds the vendor key; field_values are NOT injected.
                    from services.billing import relay_client
                    env = server_entry.setdefault("env", {})
                    if relay_client.is_available():
                        try:
                            token = relay_client.mint_session_token(user_sub or "")
                            env["OTODOCK_RELAY_BASE"] = (
                                config.OTODOCK_RELAY_BASE + akr.relay_path
                            )
                            # RELAY_BASE is a URL (non-secret) and stays in the
                            # file; the per-user relay TOKEN is secret → broker
                            # only, never written to the config file.
                            bundle_env["OTODOCK_RELAY_TOKEN"] = token
                        except relay_client.RelayNotConfigured as e:
                            env["OTODOCK_RELAY_ERROR"] = str(e)
                    else:
                        env["OTODOCK_RELAY_ERROR"] = (
                            "OtoDock hosted relay not available yet — set up "
                            f"billing at {akr.billing_setup_url}"
                            if akr.billing_setup_url
                            else "OtoDock hosted relay not available yet."
                        )
                else:
                    # Instance field_values are secret → broker bundle only,
                    # never the config file.
                    bundle_env.update(chosen["field_values"])

        # Use server_name (backward-compatible key) for the mcpServers dict
        srv_key = manifest.server_name or mcp_name

        # The credential gateway: a remote-HTTP MCP with a credential dials
        # the gateway route with the session token; the credential rides the
        # bundle and is added per request on the way out. After the instance
        # block, so an env-delivered key is in ``bundle_env``.
        gateway_cred, gateway_reason = gateway_entry(
            server_entry, manifest, srv_key=srv_key, user_sub=user_sub,
            agent_name=agent_name, task_scope=task_scope, bundle_env=bundle_env,
        )
        if gateway_reason:
            exclusion_reasons[mcp_name] = gateway_reason
            continue
        if gateway_cred is not None:
            # An HTTP MCP has no process to fetch an env: the key's value
            # lives in the credential alone.
            bundle_env = {}

        # Host-self target rewrite (T1): a homelab MCP configured with one of the
        # proxy host's OWN IPs (a co-located service, e.g. Prometheus on the
        # bare-metal box) must dial 127.0.0.1 — that IP is local inside the
        # agent's netns; resolve_sandbox_egress forwards the port to the host
        # loopback. Applied to both the broker bundle (instance/cred secrets) and
        # the plain env (config source).
        _rewrite_host_self_targets_to_loopback(
            manifest, bundle_env, agent_name, user_sub=user_sub, task_scope=task_scope,
        )
        if server_entry.get("env"):
            _rewrite_host_self_targets_to_loopback(
                manifest, server_entry["env"], agent_name,
                user_sub=user_sub, task_scope=task_scope,
            )

        servers[srv_key] = server_entry
        if bundle_env or gateway_cred is not None:
            secret_bundles[srv_key] = SecretBundle(env=bundle_env, gateway=gateway_cred)
            # A credentialed (wrapped) stdio MCP drops the bash-only
            # env_injection creds (GH_TOKEN/GIT_CONFIG_*) it would inherit from
            # the CLI env — the interceptor strips OTO_STRIP_KEYS-named vars at
            # spawn. (No-op on HTTP MCPs — they aren't stdio-wrapped.)
            if cred_result.bash_env_keys and "command" in server_entry:
                server_entry.setdefault("env", {})["OTO_STRIP_KEYS"] = ",".join(
                    sorted(cred_result.bash_env_keys)
                )

    if not servers:
        return None, flat_env, exclusion_reasons, {}, cred_result.bash_env_keys

    # Inject delegation targets into delegation-mcp env
    if delegation_targets is not None and "delegation-mcp" in servers:
        servers["delegation-mcp"].setdefault("env", {})["DELEGATION_MCP_TARGETS"] = ",".join(delegation_targets)

    # Inject the per-agent allowed browser origins into the browser-control
    # entry (server_name "local"). Empty list = no limit (the manifest ships
    # no blocked origins). Only hit the DB when the browser MCP actually
    # attached to this session.
    if "local" in servers:
        from storage.agents import agent_store as _agent_store
        _apply_browser_allowed_origins(
            servers["local"], _agent_store.get_browser_allowed_origins(agent_name)
        )
        _apply_browser_mode(
            servers["local"], secret_bundles, target_browser,
            unattended=bool(task_mode or phone_mode or meeting_mode),
        )
        # A bundle created here (token) gets the same bash-cred stripping the
        # loop applies to credentialed stdio MCPs.
        if "local" in secret_bundles and cred_result.bash_env_keys:
            servers["local"].setdefault("env", {})["OTO_STRIP_KEYS"] = ",".join(
                sorted(cred_result.bash_env_keys)
            )

    # Write config file
    sub_hash = hashlib.sha256((user_sub or "agent").encode()).hexdigest()[:12]
    config_out_dir = config.SESSIONS_DIR / "user-mcp-configs"
    if session_id:
        _sweep_session_builds(config_out_dir / _SESSION_BUILDS)
        config_out_dir = (config_out_dir / _SESSION_BUILDS
                          / hashlib.sha256(session_id.encode()).hexdigest()[:16])
    config_out_dir.mkdir(parents=True, exist_ok=True)

    if mcp_config_format == "toml":
        # Codex CLI: generate config.toml with [mcp_servers.*] sections
        # Note: credential env injection happens separately via
        # inject_credential_env_into_toml() after sandbox path rewriting.
        config_path = config_out_dir / f"{agent_name}-{sub_hash}-mcp.toml"
        config_path.write_text(_servers_to_toml(servers))
        # Save servers dict as JSON for re-generation after credential injection
        config_path.with_suffix(".servers.json").write_text(
            json.dumps(servers, indent=2)
        )
    else:
        # Claude CLI: generate mcp-config.json with mcpServers dict
        mcp_config = {"mcpServers": servers}
        config_path = config_out_dir / f"{agent_name}-{sub_hash}.json"
        config_path.write_text(json.dumps(mcp_config, indent=2))
    if session_id:
        # A rewrite in place leaves the directory's time alone: touched, so
        # the day-old sweep never takes a resumed session's fresh build.
        with contextlib.suppress(OSError):
            os.utime(config_out_dir)

    return config_path, flat_env, exclusion_reasons, secret_bundles, cred_result.bash_env_keys


# The per-session build directories under ``user-mcp-configs/``: one per
# session that built, swept once they outlive a session token's day.
_SESSION_BUILDS = "sessions"
_SESSION_BUILD_TTL_S = 24 * 3600
_BUILD_SWEEP_EVERY_S = 3600.0
_build_sweep_at: float | None = None


def _sweep_session_builds(root: Path) -> None:
    """Remove the session build directories older than a day, at most once
    an hour (the boot empties the whole tree anyway)."""
    import shutil
    import time
    global _build_sweep_at
    now = time.monotonic()
    if _build_sweep_at is not None and now - _build_sweep_at < _BUILD_SWEEP_EVERY_S:
        return
    _build_sweep_at = now
    cutoff = time.time() - _SESSION_BUILD_TTL_S
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)


def inject_credential_env_into_toml(
    toml_path: Path, credential_env: dict[str, str],
    *, exclude_keys: set[str] | None = None,
) -> Path:
    """Re-generate TOML MCP config with credential env vars injected.

    Called AFTER sandbox path rewriting so env vars have correct
    sandbox-internal paths.  Reads the saved servers dict (JSON sidecar),
    injects credential vars into each stdio MCP's env section, and
    regenerates the TOML file.

    ``exclude_keys`` are dropped from what lands in the TOML — used for the
    bash-only ``env_injection`` creds (GH_TOKEN/GIT_CONFIG_*): Codex MCPs don't
    need them, and writing them to the config FILE would put the user's token on
    disk. They stay in the Codex DAEMON env (the parent process) so
    bash ``git``/``gh`` still authenticate.

    Returns the same path (modified in-place).
    """
    json_sidecar = toml_path.with_suffix(".servers.json")
    if not json_sidecar.exists() or not credential_env:
        return toml_path

    try:
        servers = json.loads(json_sidecar.read_text())
    except Exception:
        return toml_path

    _exclude = exclude_keys or set()
    toml_env = {k: v for k, v in credential_env.items() if k not in _exclude}

    # Inject credential env into stdio MCPs only
    for srv_entry in servers.values():
        if srv_entry.get("type") == "stdio":
            srv_entry.setdefault("env", {}).update(toml_env)

    toml_path.write_text(_servers_to_toml(servers))
    json_sidecar.unlink(missing_ok=True)
    return toml_path


def _servers_to_toml(servers: dict[str, dict]) -> str:
    """Convert mcpServers dict to Codex-compatible TOML string.

    Generates [mcp_servers.<name>] sections.  Handles command, args, env,
    and url fields.  Uses manual string building to avoid a tomli_w dependency.
    """
    lines: list[str] = []
    for name, entry in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        # Lean-start: bound a single MCP's startup so one slow/hung
        # server can't drag the OtoDock warm-gate to its 15s cap. Confirmed a
        # recognized `McpServerConfig` field on the installed codex-cli 0.136
        # (the field literal is present in the native binary alongside
        # `startup_timeout_ms`/`tool_timeout_sec`), so it can't be rejected as an
        # unknown key. 10s sits below the warm-gate cap and well above the
        # slowest measured warm MCP (~3s); MCPs are pre-warmed before the session
        # A valid server isn't dropped. This 10s is the LOCAL value;
        # `_rewrite_mcp_toml_for_remote` raises it to a remote floor (with
        # per-MCP overrides) because remote cold starts pay the tunnel broker
        # fetch + on-satellite import.
        lines.append("startup_timeout_sec = 10")
        srv_type = entry.get("type", "stdio")

        if srv_type == "stdio":
            lines.append(f'command = "{_toml_escape(entry.get("command", ""))}"')
            args = entry.get("args", [])
            if args:
                args_str = ", ".join(f'"{_toml_escape(a)}"' for a in args)
                lines.append(f"args = [{args_str}]")
            env = entry.get("env", {})
            if env:
                env_pairs = ", ".join(
                    f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
                    for k, v in env.items()
                )
                lines.append(f"env = {{ {env_pairs} }}")
        elif srv_type in ("sse", "streamable-http"):
            url = entry.get("url", "")
            lines.append(f'url = "{_toml_escape(url)}"')
            # CRITICAL: emit headers for bearer-injecting remote HTTP MCPs
            # (github-mcp, Slack, Linear, Notion, Zoom). Codex's MCP server
            # config field for custom headers is ``http_headers`` (an inline
            # table) — NOT ``headers``. An unknown ``[mcp_servers.X.headers]``
            # sub-table is silently ignored by Codex (verified on 0.120.0:
            # ``codex mcp get`` reports Auth "Unsupported"), so the request
            # goes out with no Authorization header and the vendor/sidecar
            # returns 401. Must be ``http_headers`` for the header to be sent.
            headers = entry.get("headers", {}) or {}
            if headers:
                lines.append(f"[mcp_servers.{name}.http_headers]")
                for hk, hv in headers.items():
                    lines.append(f'"{_toml_escape(hk)}" = "{_toml_escape(hv)}"')

        lines.append("")  # blank line between sections

    return "\n".join(lines)


def _toml_escape(s: str) -> str:
    """Escape a string for TOML double-quoted values."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
