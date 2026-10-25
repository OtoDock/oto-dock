"""Per-MCP-tool permission tiers — manifest declaration → mode decision.

A manifest's optional ``permissions`` block assigns each tool one of four
tiers (weakest gate first): ``open`` never prompts in any mode (including
plan), ``standard`` asks only in Ask, ``sensitive`` asks in Ask and goes to
the engine's own reviewer in Auto, ``critical`` always asks — and is denied
outright in no-human contexts (the unattended ``auto`` of tasks and phone
calls) rather than hanging. Tools without a manifest declaration fall back
to ``sensitive`` (asked in the asking modes, run silently in Don't Ask and
unattended).

A tool's own annotations decide its tier where no manifest rule names it
(the MCP standard's ``readOnlyHint`` / ``destructiveHint`` /
``openWorldHint``, with the standard's defaults for a missing one: not
read-only, destructive, open-world), read in Codex's order (``hint_tier``):
read-only → ``open`` first (a tool that only reads changes nothing, whatever
else it claims); then an explicit destructive hint → ``sensitive``;
non-destructive AND closed-world, both sent → ``standard``; anything else →
``sensitive``. Codex's own MCP approval rule reads the same hints the same
way. A tool that sent no hints, or whose
list the platform never read, takes the manifest's ``default_tier``. The
hints are the server's own word; the platform keeps what it read from the
tool lists it fetches (a Direct LLM session's, a local Codex session's) in a
cache persisted under the data dir (``record_tool_hints``).

Consulted from the ``mcp__`` branch of ``decide_tool_permission`` (all CLI
layers + the Codex approval bridge) and from the Direct-LLM inline gate. The
tool name arrives canonical: an engine that mangles the server key in its
hook names (Codex) restores it in its own ``canonical_tool_name``.

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

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path

from core.permission_modes import (
    AUTO_REVIEW_MODE,
    DONT_ASK_MODE,
    PLAN_MODE,
    UNATTENDED_MODE,
    current_mode,
)
from services.mcp.mcp_manifest_parse import ships_with_platform
from services.mcp.mcp_manifest_types import PERMISSION_TIERS, McpManifest

logger = logging.getLogger(__name__)

# Global fallback for tools with no manifest declaration — asked in Ask,
# reviewed in Auto, silent in Don't Ask and unattended.
DEFAULT_TIER = "sensitive"

# Modes with no human to answer a prompt: a ``critical`` tool is denied with
# an explanation instead of blocking forever.
_NO_HUMAN_MODES = {UNATTENDED_MODE}


def _is_glob(pattern: str) -> bool:
    """True if a permission rule's tool pattern is a glob (vs exact name)."""
    return any(ch in pattern for ch in "*?[")


def _is_bundled(manifest: McpManifest) -> bool:
    """True if the manifest ships with the platform (``mcps/custom/``).

    Directory-keyed on purpose: the ``category`` field is self-declared JSON
    a catalog manifest could spoof, while the install directory is
    operator-controlled (the installer writes catalog MCPs to
    ``mcps/community/``). The parser lowers ``critical`` by the same test.
    """
    return ships_with_platform(manifest.mcp_dir)


def _manifest_for(server_name: str) -> McpManifest | None:
    from services.mcp import mcp_registry
    for m in mcp_registry.get_all_manifests().values():
        if (m.server_name or m.name) == server_name:
            return m
    return None


def resolve_tool_tier(server_name: str, tool_name: str) -> str:
    """Resolve the permission tier for ``mcp__<server_name>__<tool_name>``.

    Manifest lookup matches the hook convention (``server_name`` is the
    mcpServers key — ``manifest.server_name`` falling back to the manifest
    name). First-match-wins over ``permissions.rules``; no match → the
    tool's own hints (``hint_tier``); no hints → ``default_tier``; no block
    / unknown server → ``DEFAULT_TIER``.
    """
    if not server_name or not tool_name:
        return DEFAULT_TIER
    manifest = _manifest_for(server_name)
    block = manifest.permissions if manifest is not None else None
    if block is None:
        return hint_tier(tool_hints(server_name, tool_name)) or DEFAULT_TIER

    bundled = manifest is not None and _is_bundled(manifest)
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
    hinted = hint_tier(tool_hints(server_name, tool_name))
    if hinted:
        return hinted
    if block.default_tier == "open" and not bundled:
        logger.info(
            "MCP permissions: %s/%s — community default_tier 'open' clamped "
            "to 'standard' (exact-name rail)",
            server_name, tool_name,
        )
        return "standard"
    return block.default_tier


# --- The tools' own hints ------------------------------------------------------

#: The MCP standard's tool annotations the tier reads.
HINT_KEYS: tuple[str, ...] = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
# A tier's strictness, to log a tier a server's new hints loosen.
_STRICTNESS = {"open": 0, "standard": 1, "sensitive": 2, "critical": 3}
_HINTS_FILE = "mcp-tool-hints.json"
_hints: dict[str, dict[str, dict]] = {}
_hints_loaded = False
_hints_lock = threading.Lock()


def hint_tier(hints: dict | None) -> str | None:
    """The tier a tool's annotations give it, None when it sent none. Read
    in Codex's order: read-only first (a tool that only reads changes
    nothing, whatever else it claims)."""
    if not hints or not any(k in hints for k in HINT_KEYS):
        return None
    if hints.get("readOnlyHint") is True:
        return "open"
    if hints.get("destructiveHint") is True:
        return "sensitive"
    if hints.get("destructiveHint") is False and hints.get("openWorldHint") is False:
        return "standard"
    return "sensitive"


def codex_asks(hints: dict | None) -> bool:
    """Whether Codex's own MCP approval rule asks for a tool with these
    annotations (an unannotated tool asks)."""
    tier = hint_tier(hints)
    return tier is None or tier == "sensitive"


def _hints_path() -> Path:
    import config
    return Path(config.PLATFORM_DATA_DIR) / _HINTS_FILE


def _load_hints_locked() -> None:
    global _hints_loaded
    if _hints_loaded:
        return
    _hints_loaded = True
    try:
        data = json.loads(_hints_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning("MCP permissions: the tool hints file is unreadable; starting empty")
        return
    if isinstance(data, dict):
        for server, tools in data.items():
            if isinstance(server, str) and isinstance(tools, dict):
                _hints[server] = {t: h for t, h in tools.items()
                                  if isinstance(t, str) and isinstance(h, dict)}


def _save_hints_locked() -> None:
    path = _hints_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mcp-tool-hints-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_hints, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        logger.warning("MCP permissions: the tool hints file could not be written", exc_info=True)


def tool_hints(server_name: str, tool_name: str) -> dict | None:
    """The annotations a server last sent for a tool, None when unknown."""
    with _hints_lock:
        _load_hints_locked()
        return _hints.get(server_name, {}).get(tool_name)


def known_tools(server_name: str) -> list[str]:
    """The tools of a server whose list the platform has read."""
    with _hints_lock:
        _load_hints_locked()
        return sorted(_hints.get(server_name, {}))


def record_tool_hints(server_name: str, tools: Iterable[tuple[str, dict | None]]) -> None:
    """Keep the annotations a server's tool list carried (a Direct LLM
    session's ``list_tools``, a Codex session's server status), written
    through to the data dir when they change. A tier a new hint loosens is
    logged: a server can change its own word between versions."""
    if not server_name:
        return
    fresh = {name: {k: hints[k] for k in HINT_KEYS if isinstance(hints.get(k), bool)}
             for name, hints in ((n, h or {}) for n, h in tools) if name}
    with _hints_lock:
        _load_hints_locked()
        old = _hints.get(server_name, {})
        if old == fresh:
            return
        for name, hints in fresh.items():
            before, after = hint_tier(old.get(name)), hint_tier(hints)
            if before and after and _STRICTNESS[after] < _STRICTNESS[before]:
                logger.info("MCP permissions: %s/%s now labels itself %s (was %s)",
                            server_name, name, after, before)
        _hints[server_name] = fresh
        _save_hints_locked()


def codex_server_default(server_name: str) -> str | None:
    """``default_tools_approval_mode`` for a Codex session's server:
    ``prompt`` while the platform has not read the server's tool list and a
    glob rule names some of its tools sensitive or critical (the per-tool
    tables need exact names, unknown until a session lists them, and such a
    tool's own label could have Codex run it unasked); None otherwise."""
    if known_tools(server_name):
        return None
    manifest = _manifest_for(server_name)
    if manifest is None or manifest.permissions is None:
        return None
    if any(_is_glob(r.tool) and r.tier in ("sensitive", "critical")
           for r in manifest.permissions.rules):
        return "prompt"
    return None


def codex_tool_approvals(server_name: str) -> dict[str, str]:
    """``approval_mode`` per tool for a Codex session's ``[mcp_servers.<k>.
    tools.<t>]`` tables, where the platform's tier and Codex's own rule
    disagree: ``approve`` for a critical tool (the person's card is its one
    judge) and for an open or standard tool Codex would ask about,
    ``prompt`` for a sensitive tool it would not (a manifest rule beats a
    hint on Codex too). Over the tools whose list was read and the
    manifest's exact rule names."""
    manifest = _manifest_for(server_name)
    names = set(known_tools(server_name))
    if manifest is not None and manifest.permissions is not None:
        names |= {r.tool for r in manifest.permissions.rules if not _is_glob(r.tool)}
    out: dict[str, str] = {}
    for name in sorted(names):
        tier = resolve_tool_tier(server_name, name)
        asks = codex_asks(tool_hints(server_name, name))
        if tier == "critical" or (tier in ("open", "standard") and asks):
            out[name] = "approve"
        elif tier == "sensitive" and not asks:
            out[name] = "prompt"
    return out


def tier_decision(tier: str, mode: str) -> str:
    """The tier × mode outcome: ``"allow"`` | ``"prompt"`` | ``"deny"``.

    ================  ===  ==========  ====  =======  =====
    tier              ask  autoReview  plan  dontAsk  auto
    ================  ===  ==========  ====  =======  =====
    open              allow allow      allow allow    allow
    standard          prompt allow     deny  allow    allow
    sensitive         prompt prompt    deny  allow    allow
    critical          prompt prompt    deny  prompt   deny
    ================  ===  ==========  ====  =======  =====

    ``plan`` allows only ``open`` (reads/recoverable writes that support
    planning). ``critical`` in a no-human mode returns ``deny``
    (deny-and-inform at the call site). Sensitive's ``prompt`` in Auto is
    the engine's own reviewer when it runs one for the call (the hook gives
    no decision), the card otherwise. Callers translate ``"prompt"`` into
    their surface's mechanism (dashboard block-and-wait, interactive
    ``"ask"``, Codex bridge answer).
    """
    if tier not in PERMISSION_TIERS:  # unknown → safest prompt-capable tier
        tier = DEFAULT_TIER
    mode = current_mode(mode)
    if tier == "open":
        return "allow"
    if mode == PLAN_MODE:
        return "deny"
    if tier == "critical":
        return "deny" if mode in _NO_HUMAN_MODES else "prompt"
    if mode in (DONT_ASK_MODE, UNATTENDED_MODE):
        return "allow"
    if tier == "standard":
        return "allow" if mode == AUTO_REVIEW_MODE else "prompt"
    return "prompt"  # sensitive in Ask / Auto
