"""The MCP manifest store: the loaded manifests and every direct read of them.

``_manifests`` is rebound by ``scan_manifests`` on each scan, so everything that
reads it lives here and other modules ask through these functions (or read
``mcp_manifest_store._manifests`` at call time); a ``from`` import of the name
would keep the first scan's copy. ``mcp_registry`` re-exports the public names.
"""

import asyncio
import logging
from typing import Any

import config
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from services.mcp import mcp_manifest_types as _mt
from services.mcp.mcp_manifest_parse import _parse_manifest
from services.mcp.mcp_manifest_types import (
    _BUILDER_TOOL_RE,
    _HTTP_TRANSPORTS,
    AgentContextBlock,
    McpManifest,
)
from storage.mcp import mcp_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_manifests: dict[str, McpManifest] = {}

#: The suffix of a failed install's folder put aside by the installer's
#: rollback: never read as a tool, swept at boot.
FAILED_SUFFIX = ".failed"
#: The suffix of the copy an update keeps of the installed tool until it
#: ends: never read as a tool; one left at a start is put back.
BACKUP_SUFFIX = ".bak"


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

# The rescans run one at a time per loop: two scans never finish out of
# order, so an older scan never replaces the registry a newer one built.
_rescan_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


async def rescan() -> dict[str, McpManifest]:
    """``scan_manifests`` in a worker thread, under the loop's rescan lock:
    what the install, the delete and the update routes call after they
    changed the tree (the boot scan is synchronous, before the loop serves)."""
    global _rescan_lock
    loop = asyncio.get_running_loop()
    if _rescan_lock is None or _rescan_lock[0] is not loop:
        _rescan_lock = (loop, asyncio.Lock())
    async with _rescan_lock[1]:
        found = await asyncio.to_thread(scan_manifests)
    # An MCP installed, updated or removed changes what the relay should
    # forward here (a narrowing during the window is undone).
    from services.webhooks import relay_events
    relay_events.sync_all_soon()
    return found


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
            if not child.is_dir() or child.name.endswith((FAILED_SUFFIX, BACKUP_SUFFIX)):
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
    """Core MCPs every agent gets: the ``auto`` ones.

    ``explicit`` means an admin authorizes it per agent through an instance,
    ``platform`` that the platform attaches it itself (call-control on a
    call): neither is assigned. ``exclude_from`` does NOT disqualify: it
    filters at session-config time, not at assignment time.
    """
    return [
        name for name, m in _manifests.items()
        if m.category == "core" and m.assignment_mode == _mt.ASSIGNMENT_AUTO
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


def _default_text(value: Any) -> str:
    """A manifest config default as the text an environment carries: a
    string as written, a number as its text, a bool as ``true``/``false``;
    anything else is no default."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value if isinstance(value, str) else ""


def effective_config(manifest: McpManifest | None, saved: dict[str, str] | None = None) -> dict[str, str]:
    """The config values an MCP runs with: each declared ``config`` row's
    non-empty ``default`` (in effect before an admin saves the row, as the
    admin page shows it), the stored values over them (a saved ``""`` wins),
    and no ``_``-prefixed key (internal control state). An empty default is
    left unset: an absent variable and an empty one can differ to a server.
    ``saved`` is the store's read when the caller already holds it."""
    if manifest is None:
        return {}
    out: dict[str, str] = {}
    for field_def in manifest.config or []:
        key = getattr(field_def, "key", "")
        default = _default_text(getattr(field_def, "default", ""))
        if key and not key.startswith("_") and default:
            out[key] = default
    if saved is None:
        saved = mcp_store.get_mcp_config_values(manifest.name)
    out.update({k: v for k, v in saved.items() if not k.startswith("_")})
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


# Folder names earlier releases gave a session's OAuth token copies. No
# release writes them now; what they may have left on a disk stays refused
# by name until the sweeps remove it.
_LEGACY_TOKEN_FOLDERS = frozenset({"google-tokens", "google-analytics-tokens"})


def get_protected_credentials_subpaths() -> frozenset[str]:
    """Folder names refused to the agent's tools and the dashboard for
    every role (``auth/path_policy.py``, ``auth/path_shell.py``,
    ``api/agents/files.py``): a frozen list of what earlier releases wrote.
    The central store, ``sessions/{provider}-tokens/``, is outside every
    session's view."""
    return _LEGACY_TOKEN_FOLDERS


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
    if ((manifest.placement == "satellite_only" or manifest.device_capability)
            and not placement.offers_device_tools):
        return "Requires a remote machine (satellite)"
    # Reaching here with a device_capability set means the placement is a
    # person's or an admin's machine (gate 1 returned otherwise) — this is the
    # on-satellite owner-consent check.
    if manifest.device_capability and manifest.device_capability not in (placement.device_grants or set()):
        return f"Machine has not granted '{manifest.device_capability}' device control"
    if manifest.requires_display and placement.offers_device_tools and placement.has_display is False:
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
      - "platform": never from an ``agent_mcps`` row (a stray one is
        ignored); the sessions that need it get it through ``extra_mcps``
    Manager-enablement is the agent_mcps row presence. Platform-enabled is
    mcp_state.enabled. All three must be True for the manifest to be returned.
    """
    enabled_by_manager = set(mcp_store.get_manager_enabled_mcps(agent_name))
    if not enabled_by_manager:
        return []

    state_enabled = mcp_store.get_all_mcp_states()
    visible_explicit = mcp_store.get_visible_explicit_mcps(agent_name)

    result: list[McpManifest] = []
    # By name: a set's order follows the process's hash seed, and this order
    # reaches the prompt's skills and the session's MCP config, which must
    # read the same after every restart.
    for name in sorted(enabled_by_manager):
        if not state_enabled.get(name, False):
            continue
        manifest = _manifests.get(name)
        if not manifest:
            continue
        if manifest.assignment_mode == "explicit" and name not in visible_explicit:
            # admin revoked authorization; preserve agent_mcps row but skip at runtime
            continue
        if manifest.assignment_mode == _mt.ASSIGNMENT_PLATFORM:
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
    # lazy — avoid an import cycle at module load
    from services.media import audio_service
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


def get_visible_mcps_for_agent(agent_name: str) -> list[McpManifest]:
    """Return manifests visible to this agent regardless of manager-enablement.

    Used by the agent settings MCPs tab to render the unified list. Shows ALL
    MCPs the agent could enable (auto-mode + admin-authorized explicit-mode),
    platform-enabled; a platform-mode MCP is nobody's to enable. The API
    endpoint joins this with the manager-enabled set to compute the per-row
    `enabled` flag.
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
    appear here — they are always visible once platform-enabled — nor do
    platform-mode ones, which nobody assigns.
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
            if cred.app_kinds:
                result["app_kinds"] = cred.app_kinds
    if cred.ui_type:
        result["ui_type"] = cred.ui_type
    h = _hosted_schema(m)
    if h:
        result["hosted"] = h
    return result


def get_all_credential_schemas() -> dict[str, dict]:
    """Return all credential schemas keyed by MCP name."""
    result = {}
    for name, _m in _manifests.items():
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


def find_skill_provider(skill_id: str) -> McpManifest | None:
    """The manifest that declares ``skill_id`` (first match in registry
    order — collisions are rejected at install and warned at scan, so at
    most one live provider exists)."""
    for manifest in _manifests.values():
        for skill_def in manifest.skills:
            if skill_def.id == skill_id:
                return manifest
    return None
