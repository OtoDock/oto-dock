"""MCP manifest data classes + their parse/validation enums.

The dataclasses describing a parsed ``manifest.json`` (``McpManifest`` and its
nested config / credential / hosted / instance / agent-context / companion-app /
network-target types), the small validation enums (``_VALID_*``), the public
``DEVICE_CAPABILITIES`` alias, and the ``companion_app`` block parser. Pure
data + stdlib only — no I/O, no ``config`` / ``logger`` / registry state — so
the manifest schema is importable without pulling in the registry engine.

``services.mcp.mcp_registry`` re-exports every name here, so the public surface
(``mcp_registry.McpManifest`` etc.) and all internal references are unchanged.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ConfigField:
    key: str
    label: str
    input_type: str = "text"
    default: str = ""
    required: bool = False
    user_overridable: bool = False


# Skill loading modes. "always" = full body inlined into the system prompt
# (the pre-2026-07 behavior; right for behavior-shaping skills the agent must
# know before deciding anything). "on_demand" = standard Agent Skills
# progressive disclosure: the body is materialized into the session's CLI
# skills dir and loaded only when a task matches the description. Absent
# field defaults to on_demand (agentskills.io semantics); bundled manifests
# stamp behavior-shaping skills "always" explicitly.
VALID_SKILL_LOADING = {"always", "on_demand"}
SKILL_LOADING_DEFAULT = "on_demand"

# Skill ids become filesystem path components at materialization
# (<config>/skills/<id>/) and folder names in skill packages, so they are
# constrained to the agentskills.io ``name`` grammar: lowercase alnum runs
# separated by single hyphens, max 64 chars. NEVER interpolate an id that
# fails this into a path.
SKILL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_ID_MAX_LEN = 64


@dataclass
class SkillDef:
    id: str
    file: str  # relative to MCP folder
    description: str = ""
    default_exclude_from: list[str] = field(default_factory=list)
    loading: str = SKILL_LOADING_DEFAULT  # "always" | "on_demand"


@dataclass
class ServerConfig:
    # "none"/"none" = CONTEXT-ONLY MCP: no server process at all. The MCP
    # contributes instances (admin UI + per-agent authorization), skills,
    # dynamic prompt context, and network_targets, but emits no mcpServers
    # entry and never installs on satellites (ssh-hosts is the archetype).
    runtime: str  # python, node, docker, none
    transport: str  # stdio, sse, none
    command: str = ""
    args: list[str] = field(default_factory=list)
    source: str = ""
    docker_compose: str = ""
    port: int = 0
    health_endpoint: str = ""
    url_template: str = ""
    # Opt-in for Docker/HTTP MCPs that call back to the proxy hooks
    # (resolve-path, document-preview, file-written, images). When true, the
    # framework injects a per-session JWT as the ``Authorization`` bearer on
    # this MCP's session config (the shared container can't hold a session-
    # scoped env token like stdio MCPs do). Default false; only file-tools sets
    # it today. See auth/session_token.SESSION_JWT_PLACEHOLDER.
    proxy_callbacks: bool = False
    # T2 (Docker-Compose) service-DNS name for this Docker MCP's container.
    # Absent ⇒ the manifest's canonical ``name``. Only consulted when the proxy
    # runs containerised (RUNNING_IN_DOCKER); on bare-metal the MCP is reached
    # on ``localhost`` and this is ignored. The T2 compose rewrite adds this as
    # a network alias so it resolves regardless of the compose service key.
    # See core/config/deployment.mcp_service_name + ${docker_mcp_host}.
    service_name: str = ""
    # T2/T3 pull target — the pre-built image a containerised proxy PULLS instead
    # of building (the docker-socket-proxy blocks `docker build`). Absent ⇒
    # build-from-context, which only works on a bare-metal proxy (T1) driving a
    # local daemon. The T2 compose rewrite swaps the catalog's `build:` for this
    # `image:`. See services/mcp/compose_rewrite.py.
    image: str = ""
    # Optional auto-update bound for unpinned node/python community MCPs — a PEP 440
    # specifier set (e.g. ">=2,<3"). Empty ⇒ unbounded (auto-update tracks the
    # absolute latest published version). When set, detection + install resolve the
    # latest version *within* the bound, so an upstream major can't auto-apply until
    # a contributor widens the bound in the catalog. Ignored for docker/git+.
    # See services/mcp/mcp_updater.resolve_latest_in_bound.
    version_constraint: str = ""


@dataclass
class CredentialConfig:
    type: str = "none"  # none, per_user, infra
    label: str = ""
    description: str = ""
    fields: list[dict[str, str]] = field(default_factory=list)
    server_config_fields: list[dict[str, str]] = field(default_factory=list)
    service_account: bool = False
    oauth: dict | None = None
    # Vendor webhook receiver declaration. Dict kept as-is so the
    # GenericWebhookProvider + dispatcher walk the manifest at runtime — same
    # pattern as `oauth`. None = MCP doesn't receive vendor webhooks.
    webhooks: dict | None = None
    ui_type: str = ""
    has_service_account: bool = False
    app_credential_fields: list[dict[str, str]] = field(default_factory=list)


@dataclass
class HostedOAuthApp:
    """Hosted OAuth: the OtoDock relay performs the OAuth
    code→token exchange and every refresh server-side with OtoDock's
    ``client_secret``. The install holds NO secret — it receives only the
    user's own access/refresh tokens. No credential slug: the relay maps
    provider→app internally.
    """
    available: bool = False
    default_mode: str = "hosted"  # "self_managed" | "hosted"


@dataclass
class HostedApiKeyRelay:
    """Hosted paid-API relay: the MCP calls the relay (not the
    vendor) with the user's OtoDock token; the relay injects its vendor key,
    meters, and returns the result. The vendor key never reaches the install.

    ``relay_path`` is appended to ``config.OTODOCK_RELAY_BASE`` at runtime.
    Only declarable by MCPs whose code supports relay mode (ours / forks);
    only valid for ``instances.delivery == "env"``.
    """
    available: bool = False
    default_mode: str = "hosted"  # "self_managed" | "hosted"
    relay_path: str = ""
    min_balance_to_enable_usd: float = 0.0
    billing_setup_url: str = ""


@dataclass
class HostedConfig:
    """OtoDock hosted-relay offering for an MCP.

    Two independent sub-blocks — a manifest may declare either or both. Both
    route through the relay, which holds every OtoDock-owned secret; no secret
    ever lives in any install (self-hosted or cloud). Replaces the legacy
    ``hosted_service`` block.
    """
    oauth_app: HostedOAuthApp | None = None
    api_key_relay: HostedApiKeyRelay | None = None


@dataclass
class SystemRequirements:
    """OS-level package dependencies for an MCP.

    Each OS key is a list of package names understood by that distro's package
    manager. The installer uses these for pre-install dependency checks and
    (when MCP_AUTO_INSTALL_SYSTEM_DEPS=true) invokes the local package manager
    to install missing ones. Otherwise it logs a clear warning so admins can
    install manually.

    node_min is an interpreter version floor — the installer rejects an MCP
    early if the local node is too old, avoiding cryptic npm errors later. (A
    Python MCP's floor lives in its upstream `requires-python`, which uv reads +
    auto-fetches at install, so no separate `python_min` gate is needed.)
    """
    debian: list[str] = field(default_factory=list)
    ubuntu: list[str] = field(default_factory=list)
    rhel: list[str] = field(default_factory=list)
    arch: list[str] = field(default_factory=list)
    macos_brew: list[str] = field(default_factory=list)
    node_min: str = ""    # e.g. "18"
    notes: str = ""       # free-text explanation shown to admin on soft-fail


@dataclass
class SandboxMountDef:
    """Conditional mount from an MCP manifest's sandbox config."""
    host: str       # template string (supports ${mcp_dir}, ${platform_root})
    sandbox: str    # mount point inside sandbox
    mode: str = "ro"  # "ro" or "rw"


@dataclass
class PathEnvValueRef:
    """One entry inside a multi-value ``path_env`` list.

    Same shape as a shorthand ``PathEnvDecl`` (role + optional subpath); the
    multi-value resolver iterates these, drops empty resolutions, and joins
    the rest into a single env var value.
    """
    role: str
    subpath: str = ""


@dataclass
class PathEnvDecl:
    """Declares an env var that holds a sandbox-style workspace path for an MCP.

    Two equivalent shapes — pick whichever fits the use-case:

    - **Shorthand** — single role + optional subpath. The env var receives
      one resolved path as its value.
    - **Multi-value** — ``values`` is a non-empty list of ``PathEnvValueRef``.
      Each entry resolves independently; empty resolutions are dropped, the
      rest are joined with ``join`` (default ``":"``). Used for allowlist-style
      env vars (``ALLOWED_FILE_DIRS`` etc.) that mirror the bwrap mount roots.

    The framework resolves each role via ``services.path_roles.resolve_role``
    using the session's user/agent scope and access level (viewer/manager/
    admin). bwrap (local) or the satellite path translator (remote) maps the
    sandbox-style value(s) to real filesystem paths.

    Roles (see ``services/path_roles.py::ROLES``):
      - ``workspace``: `/users/{u}/workspace` (user-scoped) | `/workspace`
      - ``user_root``: `/users/{u}` (user-scoped) | empty (agent-scoped)
      - ``shared_workspace``: `/workspace` (manager+ user-scoped, agent-scoped) |
        empty (viewer)
      - ``config``: `/config` (manager+ user-scoped only) | empty
      - ``credentials_dir``: `/users/{u}/{subpath}` | `/workspace/{subpath}`

    Exactly one of ``role`` or ``values`` must be set; the parser enforces
    this and skips invalid entries.
    """
    role: str = ""
    subpath: str = ""
    values: list[PathEnvValueRef] = field(default_factory=list)
    join: str = ":"

    @property
    def is_multi(self) -> bool:
        """True if this entry is a multi-value list (non-empty ``values``)."""
        return bool(self.values)


@dataclass
class ToolArgPathDeclaration:
    """Declares that a specific tool argument carries a filesystem path
    so the satellite stdio interceptor can translate it
    before forwarding the JSON-RPC ``tools/call`` to the MCP.

    Fields:
      * ``tool`` — MCP tool name (e.g. ``display_images``).
      * ``json_path`` — JSONPath-flavored expression into the tool's
        ``arguments`` object. Subset supported (see
        ``_validate_tool_arg_json_path``):
          - ``name`` (top-level field)
          - ``a.b`` (nested object field)
          - ``name[*]`` (every element in an array)
          - ``a[*].b`` / ``a.b[*].c[*].d`` (nested combinations)
        Predicates, recursive descent, bracket-string and numeric
        indices are rejected at parse time.
      * ``mode`` — ``"read"`` (default) or ``"write"``. Drives the
        ``writing`` flag passed to ``path_policy_v2`` and triggers
        push-back semantics for Docker MCPs on writes.
      * ``optional`` — when ``True``, a missing field in the tool args
        is silently skipped instead of being a parse error.
      * ``relative_anchor`` — sandbox-virtual prefix that relative
        paths inside this arg anchor to. Almost never needed; the
        framework defaults to ``OTO_WORKSPACE_DIR`` which matches the
        existing community contract.
    """
    tool: str
    json_path: str
    mode: str = "read"
    optional: bool = False
    relative_anchor: str = ""


_VALID_TOOL_ARG_MODES = {"read", "write"}


@dataclass
class OutputRelocationDef:
    """Declares that an MCP writes files to a shared directory that should
    be *relocated* into the session's workspace after each tool call. Primary
    use: camoufox's shared screenshot dir → the user's hidden ``.screenshots``.

    ``source``: template path where the MCP writes (e.g. ``${mcp_dir}/screenshots``)
    ``destination_template``: template for the (flat) target dir, e.g.
        ``${workspace_dir}/.screenshots`` — kept HIDDEN so the bounded copy is
        excluded from the platform↔satellite file-sync.
    ``after_tools``: tool names that trigger relocation. Use ``["*"]`` for all.
    ``keep_recent``: keep only the N newest files in the dest dir (0/None = keep
        all). The dest dir is relocation-dedicated, so this caps it directly.
    ``gc_after``: legacy per-session GC (``"session_close"``). ``None`` (the
        default) → no GC; prefer ``keep_recent``.
    """
    source: str
    destination_template: str
    after_tools: list[str] = field(default_factory=lambda: ["*"])
    keep_recent: int | None = None
    gc_after: str | None = None


@dataclass
class InstanceFieldDef:
    """Declares a field for an MCP instance (e.g., host, port, API key)."""
    key: str
    label: str
    input_type: str = "text"  # text, url, number, password, ssh_key_select, phone_route_outbound_select
    default: str = ""
    required: bool = False
    secret: bool = False


@dataclass
class InstanceConfig:
    """Declares that an MCP uses per-instance, per-agent configuration.

    delivery modes:
      "env"         — inject first matching instance's fields as env vars (single-instance MCPs)
      "config_file" — generate JSON with all matching instances, pass via arg/env (multi-instance MCPs)
      "none"        — no runtime delivery; instances exist for admin UI +
                      per-agent authorization + network_targets only
                      (context-only MCPs — ssh-hosts consumes its instances
                      via a dynamic-context provider instead)
    """
    delivery: str  # "env" | "config_file" | "none"
    fields: list[InstanceFieldDef] = field(default_factory=list)
    config_file_arg: str = ""       # for config_file: CLI arg to override (e.g. "--config-file")
    config_file_name: str = "config.json"  # output filename
    transform: str = ""             # optional transform ID (e.g. "ssh_hosts" for key path resolution)
    max_instances: int = 0          # 0 = unlimited, 1 = single-instance


@dataclass
class CostRule:
    """A single cost rule under a manifest's `costs.rules[]`.

    First-match-wins evaluation: the engine scans rules in order and applies
    the first whose `tool` matches AND whose `match` dict is a subset of the
    actual tool args. Authors must order specific rules before catch-alls.
    """
    tool: str                                # tool name (no `mcp__` prefix); "*" matches any
    amount: float                            # base price (USD)
    match: dict[str, Any] = field(default_factory=dict)  # arg-equality conditions; {} = catch-all
    multiply_by: str = ""                    # optional integer arg name; missing/garbage → multiplier 1


@dataclass
class CostsBlock:
    """Per-MCP cost declaration — evaluated by ``mcp_cost_engine``."""
    currency: str                # "USD" only for v1
    provider: str                # tag written to usage_records.provider for every row this MCP produces
    rules: list[CostRule] = field(default_factory=list)


@dataclass
class ToolFilterConfig:
    """Manifest-declared support for runtime tool filtering.

    When an MCP's upstream binary accepts a CLI flag (or env var) that
    restricts which tools are exposed in ``tools/list`` — e.g. softeria's
    ``ms-365-mcp-server --enabled-tools <regex>`` or taylorwilsdon's
    ``workspace-mcp --tools <names>`` — declaring this block tells the
    framework which flag to emit. The admin sets the actual regex via
    ``mcp_state.tool_filter_regex`` (dashboard UI).

    Currently wired for **Docker MCPs only**: the framework writes the filter
    into the container's ``.env`` via the env var named by ``env_var_name``
    (default ``ENABLED_TOOLS_FLAG``) and the Dockerfile's ENTRYPOINT expands it
    into the binary's CLI args. The stdio spawn path does NOT yet apply this —
    declaring the block on a stdio MCP is a no-op until that delivery is wired.
    Semantics are whatever the upstream flag does — every supported flag
    (m365 ``--enabled-tools``, workspace ``--tools``) is an allowlist / include.

    MCPs that omit this block silently get no filter — the admin's
    ``mcp_state.tool_filter_regex`` is ignored (admin UI greys out the
    field). MCPs MUST declare this block to opt into runtime filtering.
    """
    arg_name: str                # e.g. "--enabled-tools" or "--tools"
    env_var_name: str = "ENABLED_TOOLS_FLAG"  # Docker MCPs only; ignored for stdio


@dataclass
class AgentContextBuilder:
    """Out-of-band MCP tool invocation for an ``agent_context`` block.

    When a block declares ``builder``, the framework invokes ``tool`` at
    session-build time with ``${...}``-substituted ``args``, captures the
    JSON-or-text result, exposes it via the ``${result.*}`` namespace, and
    renders the block's ``template`` with both source-token namespaces AND
    the result tokens.

    Fields:
      tool:           ``mcp__<server>__<tool>`` — the canonical MCP tool
                      name. Validator enforces shape AND post-load checks
                      that the referenced MCP is HTTP-class (stdio MCPs
                      cannot be called out-of-band — they're per-session
                      subprocesses).
      args:           Arbitrary JSON dict passed to the tool. ``${ns.key}``
                      tokens inside string leaves are substituted before
                      invocation (e.g. ``"phone": "${trigger.phone}"``).
      timeout_seconds: Hard wall-clock limit for the tool call. Default 5s,
                      max 30s. Multiple builder blocks in one session evaluate
                      in parallel so total latency is ``max(timeouts)`` not
                      ``sum(timeouts)``.
      account_label:  Optional override for ``pick_account(mcp, agent)``.
                      Empty string defers to the resolver — usually correct.
    """
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 5
    account_label: str = ""


@dataclass
class AgentContextBlock:
    """One per-session prompt block injected by an MCP.

    Template string with ``${ns.key}`` tokens resolved at session-build time
    against built-in source namespaces (``account.*``, ``credential.*``,
    ``agent.*``, ``user.*``, ``session.*``, ``trigger.*``) plus the optional
    ``result.*`` namespace populated by a ``builder`` block. See
    ``services/mcp/dynamic_context.py`` for the resolver.

    Fields:
      template: Markdown text with ``${ns.key}`` placeholders.
      requires: Token names that must resolve non-empty for the block to
                render. If any required token is empty, the block is
                skipped silently — no broken half-prompts. Gates BOTH the
                template render AND the builder invocation (skipping the
                builder saves an MCP call).
      scope:    ``["user"]`` to skip agent-scope sessions,
                ``["agent"]`` to skip user-scope, ``[]`` for both
                (the default).
      builder:  Optional out-of-band tool invocation. When present,
                fills the ``${result.*}`` token namespace before the
                template renders.
    """
    template: str
    requires: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    builder: AgentContextBuilder | None = None


# ---------------------------------------------------------------------------
# Device-local MCP class (computer / browser / app-connector control)
# These manifest fields gate MCPs that control a specific machine's physical
# resources (screen/input, a real browser, a running desktop app). Defaults
# preserve pre-existing behavior.
# ---------------------------------------------------------------------------

_VALID_PLACEMENTS = {"any", "satellite_only"}
_VALID_DEVICE_CAPABILITIES = {"computer", "browser", "app"}
# Public alias: the canonical device-control capability keys, for callers
# outside the registry (e.g. the per-machine device-grants endpoint validating
# user input). One source of truth.
DEVICE_CAPABILITIES = _VALID_DEVICE_CAPABILITIES


@dataclass
class CompanionAppConnect:
    host: str = "127.0.0.1"
    transport: str = "tcp_json"
    port: int = 0
    port_discovery: str = ""  # "" (static port) | "plugin_announce"


@dataclass
class CompanionAppPlugin:
    asset: str = ""  # path under the MCP dir, shipped in the sync_mcps tarball
    min_plugin_version: str = ""
    supported_host_versions: list[str] = field(default_factory=list)


@dataclass
class CompanionAppConfig:
    """Descriptor for an app-connector MCP that bridges to a running local
    program (Blender, Photoshop, ...)."""

    program: str = ""
    connect: CompanionAppConnect = field(default_factory=CompanionAppConnect)
    plugin: CompanionAppPlugin = field(default_factory=CompanionAppPlugin)
    status_tool: str = ""
    auth: str = "per_connection_token"  # "none" | "per_connection_token"


def _parse_companion_app(raw: Any, mcp_name: str) -> CompanionAppConfig | None:
    """Parse + validate a manifest ``companion_app`` block. None when absent;
    raises ValueError on structural defects (strict-by-design, like the other
    manifest parsers)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"companion_app must be an object, got {type(raw).__name__}")
    connect_raw = raw.get("connect") or {}
    if not isinstance(connect_raw, dict):
        raise ValueError("companion_app.connect must be an object")
    plugin_raw = raw.get("plugin") or {}
    if not isinstance(plugin_raw, dict):
        raise ValueError("companion_app.plugin must be an object")
    auth = raw.get("auth", "per_connection_token")
    if auth not in ("none", "per_connection_token"):
        raise ValueError(
            f'companion_app.auth must be "none" or "per_connection_token", got {auth!r}'
        )
    return CompanionAppConfig(
        program=raw.get("program", ""),
        connect=CompanionAppConnect(
            host=connect_raw.get("host", "127.0.0.1"),
            transport=connect_raw.get("transport", "tcp_json"),
            port=int(connect_raw.get("port", 0) or 0),
            port_discovery=connect_raw.get("port_discovery", ""),
        ),
        plugin=CompanionAppPlugin(
            asset=plugin_raw.get("asset", ""),
            min_plugin_version=plugin_raw.get("min_plugin_version", ""),
            supported_host_versions=list(plugin_raw.get("supported_host_versions") or []),
        ),
        status_tool=raw.get("status_tool", ""),
        auth=auth,
    )


@dataclass
class NetworkTargetDecl:
    """Declares where an MCP's internal-network target address is configured.

    Drives the sandbox egress carve-out: the platform resolves the live value
    of ``host_key`` (per agent/user/session) and punches a hole to exactly that
    host — nothing else on the LAN. ``source`` says which store holds the value:

      * ``config``             — admin per-MCP config (``mcp_config_values``)
      * ``instance``           — per-agent instance ``field_values``
      * ``per_user_credential``— the session user's own credential
      * ``infra_credential``   — shared infra credential

    The value may be a bare host/IP or a URL; the port falls back to
    ``port_key`` (a sibling key) then ``port_default``.
    """
    source: str
    host_key: str
    port_key: str | None = None
    port_default: int | None = None


@dataclass
class McpManifest:
    name: str
    label: str
    description: str
    version: str
    category: str  # core, custom, community
    server: ServerConfig
    credentials: CredentialConfig
    config: list[ConfigField]
    env: dict[str, str]
    agent_env: dict[str, str]
    exclude_from: list[str]
    skills: list[SkillDef]
    server_name: str = ""  # key used in mcpServers (defaults to name)
    assignment_mode: str = "auto"  # "auto" (managers can add) or "explicit" (admin assigns)
    # Platform feature this MCP needs before it is assignable / loadable. None for
    # almost every MCP (→ always available, no cost). Known tokens: "audio_transcribe"
    # (a usable STT provider) and "phone_calls" (usable call STT+TTS). Resolved by
    # manifest_capability_available().
    requires_capability: str | None = None
    # Internal-network targets this MCP dials (homelab MCPs: prometheus, ha, …).
    # When declared, the per-MCP ``_network_access`` admin toggle appears and,
    # when on, the sandbox carves egress to exactly these resolved hosts. Empty
    # for the vast majority of MCPs.
    network_targets: list[NetworkTargetDecl] = field(default_factory=list)
    network_access_default: bool = True  # default state of the _network_access toggle
    instances: InstanceConfig | None = None  # per-instance, per-agent config
    data_dirs: dict[str, str] = field(default_factory=dict)
    sandbox_mounts: list[SandboxMountDef] = field(default_factory=list)
    hosted: HostedConfig | None = None
    system_requirements: SystemRequirements = field(default_factory=SystemRequirements)
    outputs: list[OutputRelocationDef] = field(default_factory=list)
    path_env: dict[str, PathEnvDecl] = field(default_factory=dict)
    # Tool-arg path declarations. Flat list keyed by tool +
    # JSONPath for direct iteration by the satellite stdio interceptor;
    # use ``tool_arg_paths_for(tool)`` to get the per-tool slice.
    tool_arg_paths: list[ToolArgPathDeclaration] = field(default_factory=list)
    costs: CostsBlock | None = None  # per-tool cost rules (see mcp_cost_engine)
    agent_context: list[AgentContextBlock] = field(default_factory=list)  # per-session prompt blocks
    tool_filter: ToolFilterConfig | None = None  # Runtime tool restriction support
    # Device-local MCP class (computer / browser / app-connector control)
    placement: str = "any"  # "any" | "satellite_only"
    requires_display: bool = False
    device_capability: str | None = None  # None | "computer" | "browser" | "app"
    companion_app: CompanionAppConfig | None = None
    # Tools EXCLUDED from the device auto-approve (still prompt even when the
    # capability is granted) — RCE-class app-connector tools.
    device_high_risk_tools: list[str] = field(default_factory=list)
    patched: bool = False
    patch_note: str | None = None
    manifest_path: Path = field(default_factory=lambda: Path("."))
    mcp_dir: Path = field(default_factory=lambda: Path("."))


# ---------------------------------------------------------------------------
# Shared manifest-validation patterns (used by the parse + oauth validators)
# ---------------------------------------------------------------------------

# Streamable-HTTP transport names accepted for bearer-injection.
_HTTP_TRANSPORTS = {"http", "sse", "streamable_http", "streamable-http"}

# POSIX env-var name: leading letter or underscore, rest alphanumerics/underscore.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Canonical MCP tool name shape: ``mcp__<server>__<tool>`` with lowercase
# slugs (letters / digits / hyphens / underscores). Server and tool names
# may not be empty. The leading ``mcp__`` prefix matches the convention
# used everywhere else (cost rules, hook routing, MCP SDK).
_BUILDER_TOOL_RE = re.compile(r"^mcp__[a-z0-9_-]+__[a-z0-9_-]+$")
