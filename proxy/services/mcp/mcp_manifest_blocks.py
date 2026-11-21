"""The parsers of a manifest's blocks: permissions, costs, the builder, agent
context, the tool filter, result files, path environment, tool argument paths,
the hosted block. ``mcp_manifest_parse`` assembles them into a manifest and
re-exports them.
"""

import fnmatch
import ipaddress
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import config
from services.mcp import mcp_validate_oauth as _mvo
from services.mcp.mcp_manifest_types import (
    _BUILDER_TOOL_RE,
    _ENV_VAR_NAME_RE,
    _VALID_TOOL_ARG_MODES,
    PERMISSION_TIERS,
    AgentContextBlock,
    AgentContextBuilder,
    CostRule,
    CostsBlock,
    HostedApiKeyRelay,
    HostedConfig,
    HostedOAuthApp,
    InstanceConfig,
    PathEnvDecl,
    PathEnvValueRef,
    PermissionRule,
    PermissionsBlock,
    ResultFilesDecl,
    ToolArgPathDeclaration,
    ToolFilterConfig,
)

logger = logging.getLogger(__name__)

# ``server.tool_timeout_sec``: whole seconds, at most an hour (a tool call
# an engine waits on longer than that is a hung server, not a slow one).
TOOL_TIMEOUT_SEC_MIN = 1
TOOL_TIMEOUT_SEC_MAX = 3600


def _tool_timeout_sec(srv_data: dict, name: str) -> int:
    """The ``server.tool_timeout_sec`` a manifest declares, or 0 (each
    engine's own) for an absent or malformed value, which is logged and dropped
    like a bad ``version_constraint``: a catalog entry's typo must not keep
    the MCP out of every session."""
    raw = srv_data.get("tool_timeout_sec")
    if raw is None:
        return 0
    if (isinstance(raw, int) and not isinstance(raw, bool)
            and TOOL_TIMEOUT_SEC_MIN <= raw <= TOOL_TIMEOUT_SEC_MAX):
        return raw
    logger.warning(
        "%s: server.tool_timeout_sec %r is not a whole number of seconds from "
        "%d to %d; Codex keeps its default", name, raw,
        TOOL_TIMEOUT_SEC_MIN, TOOL_TIMEOUT_SEC_MAX,
    )
    return 0
_BUSY_ENDPOINT_RE = re.compile(r"^/[A-Za-z0-9_\-/.]{0,199}$")


def _busy_endpoint(srv_data: dict, name: str) -> str:
    """The ``server.busy_endpoint`` a manifest declares (a path on the tool's
    own port), or "" for an absent or malformed value, which is logged and
    dropped: the tool then sleeps by its use alone."""
    raw = srv_data.get("busy_endpoint")
    if raw is None or raw == "":
        return ""
    from auth.request_path import has_traversal
    if isinstance(raw, str) and _BUSY_ENDPOINT_RE.match(raw) and not has_traversal(raw):
        return raw
    logger.warning("%s: server.busy_endpoint %r is not a path such as /busy; it is not asked", name, raw)
    return ""


#: ``server.starts``: when a session starts the server's program.
STARTS_FIRST_CALL = "first_call"
STARTS_WITH_CHAT = "with_chat"
#: A Docker tool's container that runs while the platform runs (never put to
#: sleep by ``services/containers/idle.py``): the platform's own tools only.
STARTS_WITH_PLATFORM = "with_platform"
_STARTS = (STARTS_FIRST_CALL, STARTS_WITH_CHAT, STARTS_WITH_PLATFORM)


def _server_starts(srv_data: dict, name: str, mcp_dir: Path) -> str:
    """The ``server.starts`` a manifest declares, the first call when absent;
    any other value is logged and read as the default, like a bad
    ``tool_timeout_sec``: a catalog entry's typo must not keep the MCP out
    of a session. ``with_platform`` from a package that does not ship with
    the platform reads as the default too, with one line: an admin keeps
    such a tool running instead."""
    raw = srv_data.get("starts")
    if raw is None:
        return STARTS_FIRST_CALL
    if raw == STARTS_WITH_PLATFORM and not ships_with_platform(mcp_dir):
        logger.warning("%s: server.starts with_platform is for the platform's own tools; "
                       "it starts at its first call", name)
        return STARTS_FIRST_CALL
    if raw in _STARTS:
        return raw
    logger.warning("%s: server.starts %r is neither %s; it starts at its first call", name, raw,
                   " nor ".join(repr(s) for s in _STARTS))
    return STARTS_FIRST_CALL


_CRITICAL, _SENSITIVE = "critical", "sensitive"


def _parse_permissions_block(raw: Any, mcp_name: str) -> PermissionsBlock | None:
    """Parse + validate a manifest's ``permissions`` block.

    Returns ``None`` if no block is declared. Raises ``ValueError`` on any
    structural defect — strict-by-design like ``_parse_costs_block``, so a
    bad manifest is rejected at install/scan time rather than degrading a
    security-relevant declaration at runtime. The community exact-name rail
    for ``open`` is NOT enforced here (rules stay as declared); it applies at
    resolve time in ``mcp_permissions.resolve_tool_tier``, keyed off the
    manifest's scan directory.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"permissions must be an object, got {type(raw).__name__}")

    default_tier = raw.get("default_tier", "standard")
    if default_tier not in PERMISSION_TIERS:
        raise ValueError(
            f"permissions.default_tier must be one of {list(PERMISSION_TIERS)}, "
            f"got {default_tier!r}"
        )

    raw_rules = raw.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError(f"permissions.rules must be a list, got {type(raw_rules).__name__}")

    rules: list[PermissionRule] = []
    seen_exact: set[str] = set()
    for idx, rraw in enumerate(raw_rules):
        if not isinstance(rraw, dict):
            raise ValueError(
                f"permissions.rules[{idx}] must be an object, got {type(rraw).__name__}"
            )
        tool = rraw.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"permissions.rules[{idx}].tool must be a non-empty string")
        tool = tool.strip()
        if tool.startswith("mcp__"):
            raise ValueError(
                f"permissions.rules[{idx}].tool must be the bare tool name "
                f"(no 'mcp__' prefix), got {tool!r}"
            )
        tier = rraw.get("tier")
        if tier not in PERMISSION_TIERS:
            raise ValueError(
                f"permissions.rules[{idx}].tier must be one of "
                f"{list(PERMISSION_TIERS)}, got {tier!r}"
            )
        unknown = set(rraw) - {"tool", "tier"}
        if unknown:
            raise ValueError(
                f"permissions.rules[{idx}] has unknown keys: {sorted(unknown)}"
            )
        # Duplicate EXACT rules are author error (the second can never win);
        # repeated globs are legal ordering tools.
        if not _is_permission_glob(tool):
            if tool in seen_exact:
                raise ValueError(
                    f"permissions.rules[{idx}]: duplicate rule for tool {tool!r}"
                )
            seen_exact.add(tool)
        rules.append(PermissionRule(tool=tool, tier=tier))

    return PermissionsBlock(default_tier=default_tier, rules=rules)


def ships_with_platform(mcp_dir: Path) -> bool:
    """True if the MCP at ``mcp_dir`` was scanned from ``mcps/custom/``: the directory, never ``category``."""
    try:
        return Path(mcp_dir).resolve().parent == (config.MCPS_DIR / "custom").resolve()
    except OSError:
        return False


def _bounded_tiers(block: PermissionsBlock | None, mcp_name: str, mcp_dir: Path) -> PermissionsBlock | None:
    """``critical`` (a card in every mode, refused unattended) is the platform's own tier: a manifest
    that does not ship with the platform loads those tiers as ``sensitive`` with one warning. Lowered,
    never refused: an install keeps a catalog version from before the rule until its update arrives."""
    lowered = ["default_tier"] if block and block.default_tier == _CRITICAL else []
    lowered += [r.tool for r in block.rules if r.tier == _CRITICAL] if block else []
    if block is None or not lowered or ships_with_platform(mcp_dir):
        return block
    logger.warning("MCP %s: critical is the platform's own tier; %s run as sensitive", mcp_name, ", ".join(lowered))
    return PermissionsBlock(default_tier=_SENSITIVE if block.default_tier == _CRITICAL else block.default_tier,
                            rules=[PermissionRule(tool=r.tool, tier=_SENSITIVE if r.tier == _CRITICAL else r.tier)
                                   for r in block.rules])


def _is_permission_glob(tool: str) -> bool:
    """True if a permission rule's tool pattern is a glob (vs exact name)."""
    return any(ch in tool for ch in "*?[")


def _parse_costs_block(raw: Any, mcp_name: str) -> CostsBlock | None:
    """Parse + validate a manifest's ``costs`` block.

    Returns ``None`` if no block is declared. Raises ``ValueError`` on any
    structural defect — strict-by-design so the install pipeline rejects bad
    manifests with a clear error before they reach runtime.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"costs must be an object, got {type(raw).__name__}")

    currency = raw.get("currency")
    if currency != "USD":
        raise ValueError(
            f"costs.currency must be \"USD\" (v1 only), got {currency!r}"
        )

    provider = raw.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("costs.provider must be a non-empty string")

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("costs.rules must be a non-empty list")

    rules: list[CostRule] = []
    seen: set[tuple[str, str, tuple]] = set()
    for idx, rraw in enumerate(raw_rules):
        if not isinstance(rraw, dict):
            raise ValueError(
                f"costs.rules[{idx}] must be an object, got {type(rraw).__name__}"
            )
        tool = rraw.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"costs.rules[{idx}].tool must be a non-empty string")
        amount_raw = rraw.get("amount")
        if not isinstance(amount_raw, (int, float)) or isinstance(amount_raw, bool):
            raise ValueError(
                f"costs.rules[{idx}].amount must be a number, got {type(amount_raw).__name__}"
            )
        amount = float(amount_raw)
        if amount < 0:
            raise ValueError(f"costs.rules[{idx}].amount must be >= 0, got {amount}")
        match_raw = rraw.get("match", {})
        if not isinstance(match_raw, dict):
            raise ValueError(
                f"costs.rules[{idx}].match must be an object, got {type(match_raw).__name__}"
            )
        # Each match value is a SCALAR (exact equality) or a non-empty LIST of
        # scalars (membership; see mcp_cost_engine._matches). Reject anything else
        # so a malformed manifest fails loudly at load.
        for mk, mv in match_raw.items():
            if isinstance(mv, list):
                if not mv or any(isinstance(x, (dict, list)) for x in mv):
                    raise ValueError(
                        f"costs.rules[{idx}].match[{mk!r}] must be a non-empty list of scalars"
                    )
            elif isinstance(mv, dict):
                raise ValueError(
                    f"costs.rules[{idx}].match[{mk!r}] must be a scalar or a list of scalars"
                )
        multiply_by = rraw.get("multiply_by", "")
        if not isinstance(multiply_by, str):
            raise ValueError(
                f"costs.rules[{idx}].multiply_by must be a string (arg name), got {type(multiply_by).__name__}"
            )

        result_has = rraw.get("result_has", "")
        if not isinstance(result_has, str) or ("result_has" in rraw and not result_has):
            raise ValueError(f"costs.rules[{idx}].result_has must be non-empty words")

        # Duplicate detection: same tool + same match = ambiguous rule order. List
        # values normalize to tuples so the dedup key stays hashable.
        key = (tool, result_has, tuple(sorted(
            (k, tuple(v) if isinstance(v, list) else v)
            for k, v in match_raw.items()
        )))
        if key in seen:
            raise ValueError(
                f"costs.rules[{idx}] duplicates an earlier rule for tool={tool!r} match={match_raw!r}"
            )
        seen.add(key)

        rules.append(CostRule(
            tool=tool, amount=amount, match=dict(match_raw), multiply_by=multiply_by,
            result_has=result_has,
        ))

    return CostsBlock(currency=currency, provider=provider.strip(), rules=rules)


_AGENT_CONTEXT_VALID_SCOPES = frozenset({"user", "agent"})
# Block-level keys. ``builder`` opts the block into out-of-band
# tool invocation; all others are template-only fields.
# Any other key raises ValueError so typos fail loud.
_AGENT_CONTEXT_BLOCK_KEYS = frozenset({"template", "requires", "scope", "builder"})

# Sub-keys allowed inside an ``agent_context[*].builder`` block.
_BUILDER_KEYS = frozenset({"tool", "args", "timeout_seconds", "account_label"})

# Per-block builder timeout bounds. Phone calls greet within ~3-4s of
# answer, so even one 30s builder block would push past acceptable latency;
# the upper bound exists to fail-loud on accidental "300s" typos.
_BUILDER_TIMEOUT_MIN = 1
_BUILDER_TIMEOUT_MAX = 30


def _parse_builder_block(raw: dict, idx: int) -> AgentContextBuilder:
    """Parse + validate the ``builder`` sub-block of an ``agent_context`` entry.

    Caller is ``_parse_agent_context``. ``idx`` is the parent block index for
    error messages. The cross-MCP transport check (HTTP-only) runs separately
    in ``_validate_builder_block_transports`` after all manifests are loaded —
    here we only validate the structural shape and ranges.
    """
    unknown = set(raw.keys()) - _BUILDER_KEYS
    if unknown:
        raise ValueError(
            f"agent_context[{idx}].builder has unknown keys: {sorted(unknown)}"
        )

    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(
            f"agent_context[{idx}].builder.tool must be a non-empty string"
        )
    if not _BUILDER_TOOL_RE.match(tool):
        raise ValueError(
            f"agent_context[{idx}].builder.tool={tool!r} must match "
            f"'mcp__<server>__<tool>' with lowercase slugs"
        )

    args_raw = raw.get("args", {})
    if not isinstance(args_raw, dict):
        raise ValueError(
            f"agent_context[{idx}].builder.args must be an object, "
            f"got {type(args_raw).__name__}"
        )

    timeout = raw.get("timeout_seconds", 5)
    # bool is a subclass of int; reject it explicitly so True/False can't
    # silently pass as "1s" / "0s" timeouts.
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ValueError(
            f"agent_context[{idx}].builder.timeout_seconds must be an integer"
        )
    if timeout < _BUILDER_TIMEOUT_MIN or timeout > _BUILDER_TIMEOUT_MAX:
        raise ValueError(
            f"agent_context[{idx}].builder.timeout_seconds must be between "
            f"{_BUILDER_TIMEOUT_MIN} and {_BUILDER_TIMEOUT_MAX}, got {timeout}"
        )

    account_label = raw.get("account_label", "")
    if not isinstance(account_label, str):
        raise ValueError(
            f"agent_context[{idx}].builder.account_label must be a string"
        )

    return AgentContextBuilder(
        tool=tool,
        args=dict(args_raw),
        timeout_seconds=timeout,
        account_label=account_label,
    )


_CREDENTIAL_TOKEN_RE = re.compile(r"\$\{(credential\.[A-Za-z0-9_.]*)\}")


def agent_context_credential_tokens(raw: Any) -> list[str]:
    """The ``credential.*`` tokens an ``agent_context`` list names (a
    template, a ``requires`` entry, a builder's args). No credential value
    enters the token map (a block renders into the system prompt), so each
    renders empty and a ``requires`` on one skips its block."""
    if not isinstance(raw, list):
        return []
    found: set[str] = set()
    for blk in raw:
        if not isinstance(blk, dict):
            continue
        found.update(_CREDENTIAL_TOKEN_RE.findall(json.dumps(blk)))
        found.update(r for r in blk.get("requires") or []
                     if isinstance(r, str) and r.startswith("credential."))
    return sorted(found)


def _parse_agent_context(raw: Any, mcp_name: str) -> list[AgentContextBlock]:
    """Parse + validate a manifest's ``agent_context`` list.

    Returns an empty list if no block is declared. Raises ``ValueError`` on
    any structural defect so the install pipeline rejects bad manifests
    with a clear error before they reach runtime.

    Cross-MCP transport validation for ``builder.tool`` runs in a post-load
    pass — see ``_validate_builder_block_transports``.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"agent_context must be a list, got {type(raw).__name__}"
        )

    blocks: list[AgentContextBlock] = []
    for idx, blk in enumerate(raw):
        if not isinstance(blk, dict):
            raise ValueError(
                f"agent_context[{idx}] must be an object, got {type(blk).__name__}"
            )

        unknown = set(blk.keys()) - _AGENT_CONTEXT_BLOCK_KEYS
        if unknown:
            raise ValueError(
                f"agent_context[{idx}] has unknown keys: {sorted(unknown)}"
            )

        template = blk.get("template")
        if not isinstance(template, str) or not template.strip():
            raise ValueError(
                f"agent_context[{idx}].template must be a non-empty string"
            )

        requires_raw = blk.get("requires", [])
        if not isinstance(requires_raw, list) or not all(
            isinstance(r, str) and r.strip() for r in requires_raw
        ):
            raise ValueError(
                f"agent_context[{idx}].requires must be a list of non-empty strings"
            )

        scope_raw = blk.get("scope", [])
        if not isinstance(scope_raw, list) or not all(
            isinstance(s, str) for s in scope_raw
        ):
            raise ValueError(
                f"agent_context[{idx}].scope must be a list of strings"
            )
        bad_scopes = [s for s in scope_raw if s not in _AGENT_CONTEXT_VALID_SCOPES]
        if bad_scopes:
            raise ValueError(
                f"agent_context[{idx}].scope contains invalid values "
                f"{bad_scopes!r}; allowed: {sorted(_AGENT_CONTEXT_VALID_SCOPES)}"
            )

        builder = None
        builder_raw = blk.get("builder")
        if builder_raw is not None:
            if not isinstance(builder_raw, dict):
                raise ValueError(
                    f"agent_context[{idx}].builder must be an object, "
                    f"got {type(builder_raw).__name__}"
                )
            builder = _parse_builder_block(builder_raw, idx)

        blocks.append(AgentContextBlock(
            template=template,
            requires=list(requires_raw),
            scope=list(scope_raw),
            builder=builder,
        ))

    named = agent_context_credential_tokens(raw)
    if named:
        logger.warning(
            "%s: agent_context names %s: no credential value enters a prompt, "
            "so each renders empty and a requires on one skips its block",
            mcp_name, ", ".join(named))
    return blocks


# CLI flag shape — long-form ``--foo`` / ``--foo-bar``. Conservative on
# purpose: short flags like ``-t`` are rare for tool filtering and admins
# regularly typo them.
_CLI_FLAG_RE = re.compile(r"^--[a-z0-9][a-z0-9-]*$")


def _parse_tool_filter(raw: Any, mcp_name: str) -> ToolFilterConfig | None:
    """Parse + validate the optional ``tool_filter`` manifest block.

    Returns None when the manifest omits the block (the admin's
    ``mcp_state.tool_filter_regex`` is then ignored — UI greys out the
    field for this MCP). Raises ``ValueError`` on structural defects so
    the install pipeline rejects malformed manifests with a clear error.

    Required:
      * ``arg_name``: CLI flag the MCP accepts (e.g. ``--enabled-tools``).

    Optional:
      * ``env_var_name``: name of the Docker env var to write the flag
        into (default ``ENABLED_TOOLS_FLAG``). Ignored for stdio MCPs.
      * ``regex_env_var_name``: name of the Docker env var to write the bare
        regex into (default ``ENABLED_TOOLS_REGEX``); must differ from
        ``env_var_name``. Ignored for stdio MCPs.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"tool_filter must be an object, got {type(raw).__name__}",
        )
    arg_name = raw.get("arg_name", "")
    if not isinstance(arg_name, str) or not arg_name:
        raise ValueError("tool_filter.arg_name is required (non-empty string)")
    if not _CLI_FLAG_RE.match(arg_name):
        raise ValueError(
            f"tool_filter.arg_name {arg_name!r} must be a long-form CLI flag "
            f"matching {_CLI_FLAG_RE.pattern}",
        )
    env_var_name = raw.get("env_var_name", "ENABLED_TOOLS_FLAG")
    if not isinstance(env_var_name, str) or not env_var_name:
        raise ValueError(
            "tool_filter.env_var_name must be a non-empty string when declared",
        )
    if not _ENV_VAR_NAME_RE.match(env_var_name):
        raise ValueError(
            f"tool_filter.env_var_name {env_var_name!r} must match "
            f"POSIX env var name pattern {_ENV_VAR_NAME_RE.pattern}",
        )
    regex_env_var_name = raw.get("regex_env_var_name", "ENABLED_TOOLS_REGEX")
    if not isinstance(regex_env_var_name, str) or not _ENV_VAR_NAME_RE.match(regex_env_var_name):
        raise ValueError(
            f"tool_filter.regex_env_var_name {regex_env_var_name!r} must match "
            f"POSIX env var name pattern {_ENV_VAR_NAME_RE.pattern}",
        )
    if regex_env_var_name == env_var_name:
        raise ValueError("tool_filter.regex_env_var_name must differ from env_var_name")
    return ToolFilterConfig(
        arg_name=arg_name, env_var_name=env_var_name, regex_env_var_name=regex_env_var_name,
    )


def _declared_env_names(data: dict, config_fields, credentials, instances, path_env) -> set[str]:
    """Every key the MCP's env may carry besides the tool filter's two: the
    ``env`` and ``agent_env`` declarations, the config keys, the credential
    fields (an infra MCP's secrets and its server config), the instance
    fields and the ``path_env`` names."""
    names: set[str] = set()
    for block in ("env", "agent_env"):
        raw = data.get(block)
        if isinstance(raw, dict):
            names.update(str(k) for k in raw)
    names.update(cf.key for cf in config_fields)
    for fields in (credentials.fields, credentials.server_config_fields):
        names.update(str(f["key"]) for f in (fields or []) if isinstance(f, dict) and f.get("key"))
    if instances is not None:
        names.update(f.key for f in instances.fields)
    names.update(path_env)
    return names


# ``result_files``: a tool is named exactly (an MCP tool name's characters);
# the folder is ONE plain name under the workspace: no dot anywhere (no
# ``..``, no hidden folder the file sync skips), no separator, no drive.
_RESULT_FILES_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
RESULT_FILES_SUBDIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RESULT_FILES_DEFAULT_SUBDIR = "mcp-files"
_RESULT_FILES_MAX_TOOLS = 64
# Names a folder may not take: Windows device names (a paired Windows
# machine could not hold the folder) and the folders the platform itself
# writes in a workspace (``services/agents/repository_instructions.py``).
_RESULT_FILES_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "inbox", "apps", "uploads"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)


def result_files_subdir_ok(subdir) -> bool:
    """Whether ``subdir`` is a folder name ``result_files`` may use."""
    return (isinstance(subdir, str) and bool(RESULT_FILES_SUBDIR_RE.match(subdir))
            and subdir.lower() not in _RESULT_FILES_RESERVED)


def _parse_result_files(raw: Any, mcp_name: str) -> ResultFilesDecl | None:
    """Parse + validate the optional ``result_files`` block:
    ``{"tools": ["<tool>", …], "subdir": "<folder>"}``.

    ``tools`` names the tools whose results the platform keeps (1 to 64
    exact names, no glob). ``subdir`` is the folder under the session's
    workspace; absent, the MCP's own name when that is a plain folder name,
    else ``mcp-files``. Raises ``ValueError`` on any defect, so a manifest
    naming ``..``, a path or a hidden folder never installs."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"result_files must be an object, got {type(raw).__name__}")
    tools = raw.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError("result_files.tools must be a non-empty list of tool names")
    if len(tools) > _RESULT_FILES_MAX_TOOLS:
        raise ValueError(f"result_files.tools lists more than {_RESULT_FILES_MAX_TOOLS} tools")
    for tool in tools:
        if not isinstance(tool, str) or not _RESULT_FILES_TOOL_RE.match(tool):
            raise ValueError(
                f"result_files.tools entry {tool!r} is not a tool name "
                f"(letters, digits, '_', '-', '.'; no glob)",
            )
    if len(set(tools)) != len(tools):
        raise ValueError("result_files.tools names a tool twice")
    if "subdir" in raw:
        subdir = raw["subdir"]
        if not result_files_subdir_ok(subdir):
            raise ValueError(
                f"result_files.subdir {subdir!r} must be one plain folder name "
                f"matching {RESULT_FILES_SUBDIR_RE.pattern}, not a device or platform folder",
            )
    else:
        subdir = mcp_name if result_files_subdir_ok(mcp_name) else RESULT_FILES_DEFAULT_SUBDIR
    return ResultFilesDecl(tools=list(tools), subdir=subdir)


# The path role an earlier release delivered a token folder through.
_LEGACY_TOKEN_ROLE = "credentials_dir"


def _declares_token_folder(raw: dict) -> bool:
    """Whether a manifest's raw ``path_env`` still names the token folder
    role: its tool's launcher reads a file that no longer comes, so it is
    left out of sessions with a sentence until it is updated."""
    for decl in raw.values() if isinstance(raw, dict) else ():
        if not isinstance(decl, dict):
            continue
        if decl.get("role") == _LEGACY_TOKEN_ROLE:
            return True
        values = decl.get("values")
        if isinstance(values, list) and any(
            isinstance(v, dict) and v.get("role") == _LEGACY_TOKEN_ROLE for v in values
        ):
            return True
    return False


def _parse_path_env(raw: dict, mcp_name: str) -> dict[str, PathEnvDecl]:
    """Parse the ``path_env`` field of a manifest into ``PathEnvDecl`` entries.

    Each entry is one of:
      - shorthand: ``{"role": <role>, "subpath": <opt>}``
      - multi-value: ``{"values": [{"role": <role>, "subpath": <opt>}, ...],
        "join": ":"}``

    Exactly one of ``role`` and ``values`` must be set. Invalid entries are
    logged and skipped (not fatal — keeps a single bad MCP from breaking
    the whole manifest scan).
    """
    from services import path_roles

    result: dict[str, PathEnvDecl] = {}
    if not isinstance(raw, dict):
        return result
    for env_var, decl_raw in raw.items():
        if not isinstance(decl_raw, dict):
            logger.warning(
                "path_env %s for %s must be an object, got %s",
                env_var, mcp_name, type(decl_raw).__name__,
            )
            continue
        has_role = bool(decl_raw.get("role"))
        has_values = bool(decl_raw.get("values"))
        if has_role == has_values:
            # Both or neither — invalid.
            logger.warning(
                "path_env %s for %s must set exactly one of 'role' or "
                "'values' (got role=%r, values=%r); skipping",
                env_var, mcp_name, decl_raw.get("role"), decl_raw.get("values"),
            )
            continue

        if has_role:
            role = decl_raw["role"]
            if role not in path_roles.ROLES:
                logger.warning(
                    "path_env %s for %s declares unknown role %r (valid: %s)",
                    env_var, mcp_name, role, path_roles.ROLES,
                )
                continue
            subpath = decl_raw.get("subpath", "")
            result[env_var] = PathEnvDecl(role=role, subpath=subpath)
            continue

        # Multi-value entry.
        raw_values = decl_raw.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            logger.warning(
                "path_env %s for %s 'values' must be a non-empty list; skipping",
                env_var, mcp_name,
            )
            continue
        join = decl_raw.get("join")
        if not isinstance(join, str) or not join:
            join = ":"
        parsed_values: list[PathEnvValueRef] = []
        skip_entry = False
        for idx, item_raw in enumerate(raw_values):
            if not isinstance(item_raw, dict):
                logger.warning(
                    "path_env %s for %s values[%d] must be an object; skipping entry",
                    env_var, mcp_name, idx,
                )
                skip_entry = True
                break
            item_role = item_raw.get("role", "")
            if item_role not in path_roles.ROLES:
                logger.warning(
                    "path_env %s for %s values[%d] declares unknown role %r (valid: %s); skipping entry",
                    env_var, mcp_name, idx, item_role, path_roles.ROLES,
                )
                skip_entry = True
                break
            item_subpath = item_raw.get("subpath", "")
            parsed_values.append(
                PathEnvValueRef(role=item_role, subpath=item_subpath),
            )
        if skip_entry or not parsed_values:
            continue
        result[env_var] = PathEnvDecl(values=parsed_values, join=join)
    return result


# JSONPath subset validator regex. Accepts:
#   * a leading identifier (Python-like: letter/underscore + word chars)
#   * any number of `.identifier` or `[*]` segments
# Anything else (predicates [?(...)], numeric indices [3], bracket strings
# ["key"], recursive descent .., wildcard **) is rejected.
_JSONPATH_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_TOOL_ARG_JSONPATH_RE = re.compile(
    rf"^{_JSONPATH_IDENT}(?:\.{_JSONPATH_IDENT}|\[\*\])*$",
)


def _validate_tool_arg_json_path(path: str) -> str:
    """Validate a ``tool_arg_paths`` JSONPath expression against the
    supported subset. Returns an empty string when valid, otherwise a
    short rejection reason suitable for an install-time error message.
    """
    if not isinstance(path, str) or not path:
        return "json_path must be a non-empty string"
    if ".." in path:
        return "recursive descent ('..') is not supported"
    if "**" in path:
        return "wildcard '**' is not supported"
    if "[?" in path or "?(" in path:
        return "filter predicates ('[?(...)]') are not supported"
    if not _TOOL_ARG_JSONPATH_RE.match(path):
        # Surface a more specific reason when we can.
        if path.startswith(".") or path.endswith("."):
            return "leading or trailing '.' is not allowed"
        if '"' in path or "'" in path:
            return "bracket-string subscripts (e.g. [\"key\"]) are not supported"
        if re.search(r"\[\d+\]", path):
            return "numeric indices (e.g. [3]) are not supported"
        return "unsupported JSONPath syntax (allowed: name, a.b, name[*])"
    return ""


def _parse_tool_arg_paths(
    raw: Any, mcp_name: str,
) -> list[ToolArgPathDeclaration]:
    """Parse the ``tool_arg_paths`` manifest field into a flat list of
    declarations.

    Manifest shape::

        "tool_arg_paths": {
          "<tool_name>": {
            "<json_path>": { "mode": "read"|"write",
                             "optional": false,
                             "relative_anchor": "" }
          }
        }

    Strict-by-design: structural defects raise ``ValueError`` so the
    install pipeline rejects bad manifests with a clear message before
    they reach the satellite interceptor. The validator does NOT verify
    that ``<tool_name>`` exists in the MCP's runtime tool list (those
    schemas aren't available at parse time); that warning fires at
    interceptor time on the satellite.
    """
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError(
            f"tool_arg_paths must be an object, got "
            f"{type(raw).__name__}"
        )
    declarations: list[ToolArgPathDeclaration] = []
    for tool_name, per_tool_raw in raw.items():
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(
                "tool_arg_paths keys must be non-empty tool name strings"
            )
        if not isinstance(per_tool_raw, dict):
            raise ValueError(
                f"tool_arg_paths['{tool_name}'] must be an object, got "
                f"{type(per_tool_raw).__name__}"
            )
        for json_path, decl_raw in per_tool_raw.items():
            err = _validate_tool_arg_json_path(json_path)
            if err:
                raise ValueError(
                    f"tool_arg_paths['{tool_name}']['{json_path}']: {err}"
                )
            if not isinstance(decl_raw, dict):
                raise ValueError(
                    f"tool_arg_paths['{tool_name}']['{json_path}'] must "
                    f"be an object, got {type(decl_raw).__name__}"
                )
            mode = decl_raw.get("mode", "read")
            if mode not in _VALID_TOOL_ARG_MODES:
                raise ValueError(
                    f"tool_arg_paths['{tool_name}']['{json_path}']: "
                    f"mode must be one of {sorted(_VALID_TOOL_ARG_MODES)}, "
                    f"got {mode!r}"
                )
            optional = bool(decl_raw.get("optional", False))
            relative_anchor = decl_raw.get("relative_anchor", "") or ""
            if relative_anchor and not relative_anchor.startswith("/"):
                raise ValueError(
                    f"tool_arg_paths['{tool_name}']['{json_path}']: "
                    f"relative_anchor must start with '/', got "
                    f"{relative_anchor!r}"
                )
            declarations.append(
                ToolArgPathDeclaration(
                    tool=tool_name,
                    json_path=json_path,
                    mode=mode,
                    optional=optional,
                    relative_anchor=relative_anchor,
                )
            )
    return declarations


def _parse_hosted_block(
    raw: Any, mcp_name: str, instances: "InstanceConfig | None",
) -> "HostedConfig | None":
    """Parse + validate the manifest ``hosted`` block.

    Two independent sub-blocks — ``oauth_app`` and ``api_key_relay`` — either
    or both may be declared. Both route through the OtoDock relay; no OtoDock
    secret ever lives in the install. Strict validation (raises ``ValueError``,
    mirroring ``_parse_costs_block`` / ``_validate_oauth_services``):

      * ``default_mode`` ∈ {``self_managed``, ``hosted``} (no ``disabled``).
      * ``api_key_relay.available`` requires a non-empty ``relay_path`` AND the
        MCP's instance ``delivery == "env"`` (the ``config_file`` arm writes
        raw ``field_values`` to disk and would bypass the relay).
      * ``oauth_app`` needs no install-side credential — just ``available`` +
        ``default_mode``.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"hosted must be an object, got {type(raw).__name__}")

    def _check_mode(mode: str, where: str) -> str:
        if mode not in ("self_managed", "hosted"):
            raise ValueError(
                f"hosted.{where}.default_mode must be 'self_managed' or "
                f"'hosted', got {mode!r}"
            )
        return mode

    oauth_app = None
    oa = raw.get("oauth_app")
    if oa is not None:
        if not isinstance(oa, dict):
            raise ValueError("hosted.oauth_app must be an object")
        oauth_app = HostedOAuthApp(
            available=bool(oa.get("available", False)),
            default_mode=_check_mode(oa.get("default_mode", "hosted"), "oauth_app"),
        )

    api_key_relay = None
    akr = raw.get("api_key_relay")
    if akr is not None:
        if not isinstance(akr, dict):
            raise ValueError("hosted.api_key_relay must be an object")
        available = bool(akr.get("available", False))
        relay_path = akr.get("relay_path", "")
        if available:
            if not relay_path:
                raise ValueError(
                    "hosted.api_key_relay.available requires a non-empty relay_path"
                )
            if not (instances and instances.delivery == "env"):
                raise ValueError(
                    "hosted.api_key_relay is only supported for MCPs whose "
                    "instances use delivery='env' (config_file would write raw "
                    "field_values to disk and bypass the relay)"
                )
        api_key_relay = HostedApiKeyRelay(
            available=available,
            default_mode=_check_mode(
                akr.get("default_mode", "hosted"), "api_key_relay",
            ),
            relay_path=relay_path,
            min_balance_to_enable_usd=float(akr.get("min_balance_to_enable_usd", 0.0)),
            billing_setup_url=akr.get("billing_setup_url", ""),
        )

    if oauth_app is None and api_key_relay is None:
        return None
    return HostedConfig(oauth_app=oauth_app, api_key_relay=api_key_relay)


# Header names the gateway owns or strips: a header-style key may not take
# one of these, so a manifest can never move the session token, a cookie or
# a transport header into the vendor's hands.
_API_KEY_HEADER_DENIED = re.compile(
    r"^(authorization|cookie|host|content-.*|accept.*|mcp-.*|forwarded|"
    r"x-forwarded-.*|x-real-ip|connection|transfer-encoding|upgrade|te|"
    r"trailer|keep-alive|proxy-.*)$", re.IGNORECASE,
)
_HEADER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


def _validate_api_key_header(
    raw: Any, cred_raw: Any, server_raw: Any, instances_raw: Any, hosted_raw: Any,
) -> None:
    """``credentials.api_key_header``: a vendor-hosted MCP server that takes
    an API key in a header of its own (Google Maps Grounding Lite takes
    ``X-Goog-Api-Key``). The credential gateway adds the header; the value
    comes from a declared ``credentials.fields`` password key or an
    env-delivered instance field; the manifest proposes the hosts and the
    admin approves them under the manifest's name. Streamable HTTP only (an
    SSE endpoint event would escape the gateway's path), a literal host,
    never beside a bearer or the hosted key relay."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError("must be an object when declared")
    for key in ("name", "value_from", "proposed_hosts"):
        if key not in raw:
            raise ValueError(f"{key} is required")
    unknown = set(raw) - {"name", "value_from", "proposed_hosts"}
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _HEADER_TOKEN_RE.match(name):
        raise ValueError("name must be an HTTP header name")
    if _API_KEY_HEADER_DENIED.match(name):
        raise ValueError(f"name {name!r} is a header the platform owns")
    value_from = raw.get("value_from")
    if not isinstance(value_from, str) or not _ENV_VAR_NAME_RE.match(value_from):
        raise ValueError("value_from must name a credential key")
    cred = cred_raw if isinstance(cred_raw, dict) else {}
    oauth_raw = cred.get("oauth")
    oauth = oauth_raw if isinstance(oauth_raw, dict) else {}
    if oauth.get("bearer_required"):
        raise ValueError("cannot be declared beside credentials.oauth.bearer_required")
    hosted = hosted_raw if isinstance(hosted_raw, dict) else {}
    if hosted.get("api_key_relay"):
        raise ValueError("cannot be declared beside hosted.api_key_relay")
    declared: set[str] = set()
    if cred.get("type") in ("per_user", "infra"):
        for f in cred.get("fields") or []:
            if isinstance(f, dict) and f.get("input_type") == "password" and f.get("key"):
                declared.add(str(f["key"]))
    inst = instances_raw if isinstance(instances_raw, dict) else {}
    if inst.get("delivery", "env") == "env":
        for f in inst.get("fields") or []:
            if isinstance(f, dict) and f.get("key"):
                declared.add(str(f["key"]))
    if value_from not in declared:
        raise ValueError(
            f"value_from {value_from!r} must be a credentials.fields key with "
            "input_type password or an env-delivered instances field"
        )
    hosts = raw.get("proposed_hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("proposed_hosts must be a non-empty list")
    for idx, host in enumerate(hosts):
        if not isinstance(host, str) or not host.strip() or not _mvo._HOSTNAME_RE.match(host):
            raise ValueError(f"proposed_hosts[{idx}]={host!r} is not a valid hostname")
    server = server_raw if isinstance(server_raw, dict) else {}
    transport = server.get("transport", "")
    if transport not in ("streamable_http", "streamable-http", "http"):
        raise ValueError(
            f"requires a streamable-HTTP server.transport; got {transport!r}"
        )
    template = str(server.get("url_template") or "")
    if "${" in template:
        raise ValueError("server.url_template must be a literal URL")
    try:
        parts = urlsplit(template)
    except ValueError:
        parts = None
    host = (parts.hostname or "") if parts else ""
    if not parts or parts.scheme not in ("http", "https") or not host:
        raise ValueError("server.url_template must be an http(s) URL with a host")
    if not any(fnmatch.fnmatchcase(host.lower(), h.lower()) for h in hosts):
        raise ValueError(
            f"server.url_template host {host!r} is not among proposed_hosts"
        )


# The headers a credential-exit entry may not name: the ones the gateway
# owns or strips, ``authorization`` excepted (the header most vendors read a
# key from, which the exit rewrites by the placeholder it carries).
_EXIT_HEADER_DENIED = re.compile(
    r"^(cookie|host|content-.*|accept.*|mcp-.*|forwarded|"
    r"x-forwarded-.*|x-real-ip|connection|transfer-encoding|upgrade|te|"
    r"trailer|keep-alive|proxy-.*)$", re.IGNORECASE,
)
_EXIT_ENTRY_KEYS = frozenset({"key", "header", "prefix", "hosts", "streams"})
_EXIT_PREFIX_MAX = 32
# The hosted key relay's token in the tool's environment
# (``mcp_session_config``); its host is the install's relay base.
_EXIT_RELAY_TOKEN = "OTODOCK_RELAY_TOKEN"


def _exit_key_sources(cred_raw: Any, instances_raw: Any, hosted_raw: Any) -> set[str]:
    """The env names the platform delivers as secrets, which an exit entry
    may name: an env-delivered instance field marked secret, a
    ``credentials.fields`` password key, an ``mcp_env_injection`` name, the
    relay token when the hosted key relay is declared."""
    out: set[str] = set()
    cred = cred_raw if isinstance(cred_raw, dict) else {}
    if cred.get("type") in ("per_user", "infra"):
        for f in cred.get("fields") or []:
            if isinstance(f, dict) and f.get("input_type") == "password" and f.get("key"):
                out.add(str(f["key"]))
    oauth = cred.get("oauth")
    injected = oauth.get("mcp_env_injection") if isinstance(oauth, dict) else None
    if isinstance(injected, list):
        out.update(n for n in injected if isinstance(n, str))
    inst = instances_raw if isinstance(instances_raw, dict) else {}
    if inst.get("delivery", "env") == "env":
        for f in inst.get("fields") or []:
            if isinstance(f, dict) and f.get("secret") and f.get("key"):
                out.add(str(f["key"]))
    hosted = hosted_raw if isinstance(hosted_raw, dict) else {}
    if hosted.get("api_key_relay"):
        out.add(_EXIT_RELAY_TOKEN)
    return out


#: Engines' domains with no API key product (a sign-in's own), each with
#: whose it is: no name under one is declared.
_ENGINE_SIGN_IN = {"chatgpt.com": "ChatGPT's", "claude.com": "Claude's"}


def _engine_host_refusal(low: str) -> str:
    """Why a declared host under an engine's domain cannot be one, or "".
    The exit reads a declared host's connections for the whole chat, so an
    engine's domain or a wildcard over one would take in the engine's
    sign-in hosts for every program that passes the exit, and a sign-in's
    own domain takes no API key. A literal API host below one
    (``api.openai.com``) is a key's own host: the tool reaches it through
    its own list of direct names (``credential_exit.own_direct``)."""
    from core.credentials.credential_exit import ENGINE_HOSTS
    name = low.removeprefix("*.")
    for engine in ENGINE_HOSTS:
        if name == engine or name.endswith("." + engine):
            if low.startswith("*.") or name == engine:
                return "is an engine's domain or a wildcard over one: declare the API host itself"
            if engine in _ENGINE_SIGN_IN:
                return f"is under {_ENGINE_SIGN_IN[engine]} own domain, which takes no API key"
    return ""


def _check_exit_host(host: Any, where: str) -> str:
    """A literal hostname, or ``*.`` before one of at least two labels;
    never an address, ``localhost``, a ``.localhost`` name or a name with a
    trailing dot. Returns the host lowered for the duplicate check."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"{where} must be a non-empty string")
    wild = host.startswith("*.")
    name = host[2:] if wild else host
    if name.endswith("."):
        # The exit drops a connection's trailing dot before it matches, never
        # a pattern's: a declared name means one thing everywhere.
        raise ValueError(f"{where}={host!r} ends with a dot: declare the name without it")
    if "*" in name or not _mvo._HOSTNAME_RE.match(name):
        raise ValueError(f"{where}={host!r} is not a hostname or a *.domain pattern")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError(f"{where}={host!r} is an address, not a hostname")
    low = name.lower().rstrip(".")
    if low == "localhost" or low.endswith(".localhost"):
        raise ValueError(f"{where}={host!r} is a local name")
    if wild and len([lbl for lbl in name.split(".") if lbl]) < 2:
        raise ValueError(f"{where}={host!r} is too broad: a wildcard needs a domain of two labels")
    return host.lower()


def _validate_exit(raw: Any, cred_raw: Any, instances_raw: Any, hosted_raw: Any) -> None:
    """``credentials.exit``: the secrets the tool sends in a header of its
    own toward declared hosts, ``[{key, header, prefix, hosts, streams}]``.
    The credential exit may hand the tool a placeholder for ``key`` and write
    ``prefix`` + the value in ``header`` on the way out, so ``key`` must be
    a secret the platform delivers (``_exit_key_sources``), ``header`` one
    the platform does not own (``authorization`` allowed: vendors read keys
    from it) and ``hosts`` literal names or ``*.domain`` patterns that join
    the MCP's allowlist rows under the manifest's name. ``streams`` (true or
    false) asks that a request body past the exit's cap passes as it arrives.
    Never beside a bearer: its rows are keyed by the provider id, and an MCP
    owns one key."""
    if raw is None:
        return
    if not isinstance(raw, list) or not raw:
        raise ValueError("must be a non-empty list of objects when declared")
    cred = cred_raw if isinstance(cred_raw, dict) else {}
    oauth = cred.get("oauth")
    if isinstance(oauth, dict) and oauth.get("bearer_required"):
        raise ValueError("cannot be declared beside credentials.oauth.bearer_required")
    sources = _exit_key_sources(cred_raw, instances_raw, hosted_raw)
    keys: set[str] = set()
    for idx, entry in enumerate(raw):
        where = f"exit[{idx}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        for k in ("key", "header", "hosts"):
            if k not in entry:
                raise ValueError(f"{where}.{k} is required")
        unknown = set(entry) - _EXIT_ENTRY_KEYS
        if unknown:
            raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")
        key = entry["key"]
        if not isinstance(key, str) or not _ENV_VAR_NAME_RE.match(key):
            raise ValueError(f"{where}.key must be an env var name")
        if key not in sources:
            raise ValueError(
                f"{where}.key {key!r} must name a secret env-delivered instances field, a "
                "credentials.fields key with input_type password, a credentials.oauth."
                f"mcp_env_injection name or {_EXIT_RELAY_TOKEN} under hosted.api_key_relay"
            )
        if key in keys:
            raise ValueError(f"{where}.key {key!r} is declared by an earlier entry")
        keys.add(key)
        header = entry["header"]
        if not isinstance(header, str) or not _HEADER_TOKEN_RE.match(header):
            raise ValueError(f"{where}.header must be an HTTP header name")
        if _EXIT_HEADER_DENIED.match(header):
            raise ValueError(f"{where}.header {header!r} is a header the platform owns")
        prefix = entry.get("prefix", "")
        if (not isinstance(prefix, str) or len(prefix) > _EXIT_PREFIX_MAX
                or not prefix.isascii() or not prefix.isprintable()):
            raise ValueError(
                f"{where}.prefix must be a string of at most {_EXIT_PREFIX_MAX} printable "
                "ASCII characters without CR or LF"
            )
        if not isinstance(entry.get("streams", False), bool):
            raise ValueError(f"{where}.streams must be true or false")
        hosts = entry["hosts"]
        if not isinstance(hosts, list) or not hosts:
            raise ValueError(f"{where}.hosts must be a non-empty list")
        seen: set[str] = set()
        for hidx, host in enumerate(hosts):
            low = _check_exit_host(host, f"{where}.hosts[{hidx}]")
            if low in seen:
                raise ValueError(f"{where}.hosts[{hidx}]={host!r} is declared twice")
            refusal = _engine_host_refusal(low)
            if refusal:
                raise ValueError(f"{where}.hosts[{hidx}]={host!r} {refusal}")
            seen.add(low)
