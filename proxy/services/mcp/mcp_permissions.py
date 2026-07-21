"""Per-MCP-tool permission tiers — manifest declaration → mode decision.

A manifest's optional ``permissions`` block assigns each tool one of four
tiers (weakest gate first): ``open`` never prompts in any mode (including
plan), ``standard`` prompts only in ``default``, ``sensitive`` prompts in
``default`` AND ``acceptEdits``, ``critical`` always prompts — and is denied
outright in no-human contexts (``auto`` tasks/phone) rather than hanging.
Tools without a manifest declaration fall back to ``sensitive``, which is
byte-identical to the pre-tier MCP behavior (prompt in the prompting modes,
run silently in ``dontAsk``/``auto``).

Consulted from the ``mcp__`` branch of ``decide_tool_permission`` (all CLI
layers + the Codex approval bridge) and from the Direct-LLM inline gate.

Trust rail: a manifest that ships with the platform (``mcps/custom/``) may
declare any tier through any rule form. A catalog-installed manifest
(``mcps/community/``, ``mcps/skills/``) gets ``open`` honored ONLY from an
EXACT tool-name rule — a glob or ``default_tier`` resolving to ``open``
clamps to ``standard``. The catalog review process is the trust gate, and
requiring each silent tool to be named individually makes a T0 grant
impossible to hide in a wildcard and trivially auditable in a catalog PR
diff. The rail keys off the SCAN DIRECTORY (``manifest.mcp_dir``), never the
self-declared ``category`` field.
"""

import logging
from fnmatch import fnmatchcase

import config
from services.mcp.mcp_manifest_types import McpManifest, PERMISSION_TIERS

logger = logging.getLogger(__name__)

# Global fallback for tools with no manifest declaration — the pre-tier
# status quo (prompt in default/acceptEdits, silent in dontAsk/auto).
DEFAULT_TIER = "sensitive"

# Modes with no human to answer a prompt: a ``critical`` tool is denied with
# an explanation instead of blocking forever.
_NO_HUMAN_MODES = {"auto"}


def _is_glob(pattern: str) -> bool:
    """True if a permission rule's tool pattern is a glob (vs exact name)."""
    return any(ch in pattern for ch in "*?[")


def _is_bundled(manifest: McpManifest) -> bool:
    """True if the manifest ships with the platform (``mcps/custom/``).

    Directory-keyed on purpose: the ``category`` field is self-declared JSON
    a catalog manifest could spoof, while the install directory is
    operator-controlled (the installer writes catalog MCPs to
    ``mcps/community/``).
    """
    try:
        return manifest.mcp_dir.resolve().parent == (config.MCPS_DIR / "custom").resolve()
    except OSError:
        return False


def resolve_tool_tier(server_name: str, tool_name: str) -> str:
    """Resolve the permission tier for ``mcp__<server_name>__<tool_name>``.

    Manifest lookup matches the hook convention (``server_name`` is the
    mcpServers key — ``manifest.server_name`` falling back to the manifest
    name). First-match-wins over ``permissions.rules``; no match →
    ``default_tier``; no block / unknown server → ``DEFAULT_TIER``.
    """
    if not server_name or not tool_name:
        return DEFAULT_TIER
    from services.mcp import mcp_registry
    manifest = None
    for m in mcp_registry.get_all_manifests().values():
        if (m.server_name or m.name) == server_name:
            manifest = m
            break
    if manifest is None or manifest.permissions is None:
        return DEFAULT_TIER

    block = manifest.permissions
    bundled = _is_bundled(manifest)
    for rule in block.rules:
        if rule.tool == tool_name or (
            _is_glob(rule.tool) and fnmatchcase(tool_name, rule.tool)
        ):
            if (
                rule.tier == "open"
                and not bundled
                and rule.tool != tool_name  # matched via glob, not exact name
            ):
                logger.info(
                    "MCP permissions: %s/%s — community glob rule %r grants "
                    "'open'; clamped to 'standard' (exact-name rail)",
                    server_name, tool_name, rule.tool,
                )
                return "standard"
            return rule.tier
    if block.default_tier == "open" and not bundled:
        logger.info(
            "MCP permissions: %s/%s — community default_tier 'open' clamped "
            "to 'standard' (exact-name rail)",
            server_name, tool_name,
        )
        return "standard"
    return block.default_tier


def tier_decision(tier: str, mode: str) -> str:
    """The tier × mode outcome: ``"allow"`` | ``"prompt"`` | ``"deny"``.

    ================  =======  ===========  ====  =======  =====
    tier              default  acceptEdits  plan  dontAsk  auto
    ================  =======  ===========  ====  =======  =====
    open              allow    allow        allow allow    allow
    standard          prompt   allow        deny  allow    allow
    sensitive         prompt   prompt       deny  allow    allow
    critical          prompt   prompt       deny  prompt   deny
    ================  =======  ===========  ====  =======  =====

    ``plan`` allows only ``open`` (reads/recoverable writes that support
    planning) — every other tier keeps today's plan-mode deny. ``critical``
    in a no-human mode returns ``deny`` (deny-and-inform at the call site).
    Callers translate ``"prompt"`` into their surface's mechanism (dashboard
    block-and-wait, interactive ``"ask"``, Codex bridge answer).
    """
    if tier not in PERMISSION_TIERS:  # unknown → safest prompt-capable tier
        tier = DEFAULT_TIER
    if tier == "open":
        return "allow"
    if mode == "plan":
        return "deny"
    if tier == "critical":
        return "deny" if mode in _NO_HUMAN_MODES else "prompt"
    if mode in ("dontAsk", "auto"):
        return "allow"
    if tier == "standard":
        return "allow" if mode == "acceptEdits" else "prompt"
    return "prompt"  # sensitive in default/acceptEdits
