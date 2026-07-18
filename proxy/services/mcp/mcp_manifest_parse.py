"""MCP manifest parsing — raw ``manifest.json`` dict → typed ``McpManifest``.

All the per-block parsers (`costs`, `agent_context` + `builder`, `tool_filter`,
`path_env`, `tool_arg_paths`, `hosted`) and the top-level `_parse_manifest`
orchestrator that assembles a validated `McpManifest` (invoking the oauth +
webhook validators). Strict-by-design: structural defects raise `ValueError`
so a bad manifest is rejected at install/scan time.

`services.mcp.mcp_registry` re-exports `_parse_manifest` (driven by `scan_manifests`)
and the sub-parsers (the parser test-suite). The in-memory `_manifests` cache,
`scan_manifests`, and the post-scan builder-transport validation stay in the
registry engine.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from services.mcp.mcp_manifest_types import (
    AgentContextBlock,
    AgentContextBuilder,
    ConfigField,
    CostRule,
    CostsBlock,
    CredentialConfig,
    HostedApiKeyRelay,
    HostedConfig,
    HostedOAuthApp,
    InstanceConfig,
    InstanceFieldDef,
    McpManifest,
    NetworkTargetDecl,
    OutputRelocationDef,
    PathEnvDecl,
    PathEnvValueRef,
    SKILL_ID_MAX_LEN,
    SKILL_ID_RE,
    SKILL_LOADING_DEFAULT,
    VALID_SKILL_LOADING,
    SandboxMountDef,
    ServerConfig,
    SkillDef,
    SystemRequirements,
    ToolArgPathDeclaration,
    ToolFilterConfig,
    _BUILDER_TOOL_RE,
    _ENV_VAR_NAME_RE,
    _VALID_DEVICE_CAPABILITIES,
    _VALID_PLACEMENTS,
    _VALID_TOOL_ARG_MODES,
    _parse_companion_app,
)
from services.mcp.mcp_validate_oauth import _validate_oauth_services
from services.mcp.mcp_validate_webhooks import _validate_webhooks_block

logger = logging.getLogger(__name__)


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
    seen: set[tuple[str, tuple]] = set()
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

        # Duplicate detection: same tool + same match = ambiguous rule order. List
        # values normalize to tuples so the dedup key stays hashable.
        key = (tool, tuple(sorted(
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
    return ToolFilterConfig(arg_name=arg_name, env_var_name=env_var_name)


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
            if role == "credentials_dir" and not subpath:
                logger.warning(
                    "path_env %s for %s uses credentials_dir without subpath; ignoring",
                    env_var, mcp_name,
                )
                continue
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
            if item_role == "credentials_dir" and not item_subpath:
                logger.warning(
                    "path_env %s for %s values[%d] uses credentials_dir without subpath; skipping entry",
                    env_var, mcp_name, idx,
                )
                skip_entry = True
                break
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


def _parse_manifest(manifest_path: Path) -> McpManifest | None:
    """Parse a manifest.json file into an McpManifest."""
    try:
        data = json.loads(manifest_path.read_text())
    except Exception as e:
        logger.warning("Failed to read manifest %s: %s", manifest_path, e)
        return None

    mcp_dir = manifest_path.parent

    # Server config
    srv_data = data.get("server", {})
    # Validate the optional auto-update bound (PEP 440). A malformed specifier
    # (e.g. an npm-style "^2"/"2.x" range) must not break the registry load — log
    # it and fall back to unbounded so the MCP simply tracks the latest version.
    version_constraint = srv_data.get("version_constraint", "") or ""
    if version_constraint:
        try:
            from packaging.specifiers import SpecifierSet
            SpecifierSet(version_constraint)
        except Exception:
            logger.warning(
                "%s: invalid server.version_constraint %r (expected a PEP 440 "
                "specifier like '>=2,<3') — treating as unbounded",
                data.get("name", manifest_path.parent.name), version_constraint,
            )
            version_constraint = ""
    server = ServerConfig(
        runtime=srv_data.get("runtime", "python"),
        transport=srv_data.get("transport", "stdio"),
        command=srv_data.get("command", ""),
        args=srv_data.get("args", []),
        source=srv_data.get("source", ""),
        docker_compose=srv_data.get("docker_compose", ""),
        port=srv_data.get("port", 0),
        health_endpoint=srv_data.get("health_endpoint", ""),
        url_template=srv_data.get("url_template", ""),
        proxy_callbacks=bool(srv_data.get("proxy_callbacks", False)),
        service_name=srv_data.get("service_name", ""),
        image=srv_data.get("image", ""),
        version_constraint=version_constraint,
    )

    # Credential config. Credential directories (OAuth tokens etc.) are
    # declared via `path_env` with role `credentials_dir` — never on the
    # `credentials` block. See proxy/services/path_roles.py.
    cred_data = data.get("credentials", {})
    oauth_data = cred_data.get("oauth")
    webhooks_data = cred_data.get("webhooks")
    app_cred_fields = oauth_data.get("app_credential_fields", []) if oauth_data else []

    credentials = CredentialConfig(
        type=cred_data.get("type", "none"),
        label=cred_data.get("label", ""),
        description=cred_data.get("description", ""),
        fields=cred_data.get("fields", []),
        server_config_fields=cred_data.get("server_config_fields", []),
        service_account=cred_data.get("service_account", False),
        has_service_account=cred_data.get("service_account", False),
        oauth=oauth_data,
        webhooks=webhooks_data,
        ui_type=cred_data.get("ui_type", ""),
        app_credential_fields=app_cred_fields,
    )

    # Config fields
    config_fields = []
    for cf in data.get("config", []):
        config_fields.append(ConfigField(
            key=cf["key"],
            label=cf.get("label", cf["key"]),
            input_type=cf.get("input_type", "text"),
            default=cf.get("default", ""),
            required=cf.get("required", False),
            user_overridable=cf.get("user_overridable", False),
        ))

    # Skills. Fail closed per-skill, not per-manifest: a bad skill entry is
    # dropped with a warning (its id would become a filesystem path component
    # at materialization) while the MCP's tools keep working. The community
    # installers additionally hard-reject packages with invalid skill entries
    # at install time, where a human sees the error.
    skills = []
    for sk in data.get("skills", []):
        skill_id = sk["id"]
        if not SKILL_ID_RE.fullmatch(skill_id) or len(skill_id) > SKILL_ID_MAX_LEN:
            logger.warning(
                "%s: skill id %r is not a valid skill name (lowercase alnum "
                "+ single hyphens, max %d chars) — skill dropped",
                data.get("name", mcp_dir.name), skill_id, SKILL_ID_MAX_LEN,
            )
            continue
        loading = sk.get("loading", SKILL_LOADING_DEFAULT)
        if loading not in VALID_SKILL_LOADING:
            logger.warning(
                "%s: skill %r has invalid loading %r (expected one of %s) — "
                "using %r",
                data.get("name", mcp_dir.name), skill_id, loading,
                sorted(VALID_SKILL_LOADING), SKILL_LOADING_DEFAULT,
            )
            loading = SKILL_LOADING_DEFAULT
        skills.append(SkillDef(
            id=skill_id,
            file=sk["file"],
            description=sk.get("description", ""),
            default_exclude_from=sk.get("default_exclude_from", []),
            loading=loading,
        ))

    # System-level package dependencies (optional). Used by the installer
    # (both platform and satellite via shared mcp_installer module) for
    # pre-install dependency checks.
    sr_data = data.get("system_requirements") or {}
    system_requirements = SystemRequirements(
        debian=list(sr_data.get("debian", [])),
        ubuntu=list(sr_data.get("ubuntu", [])),
        rhel=list(sr_data.get("rhel", [])),
        arch=list(sr_data.get("arch", [])),
        macos_brew=list(sr_data.get("macos_brew", [])),
        node_min=str(sr_data.get("node_min", "")),
        notes=str(sr_data.get("notes", "")),
    )

    # Instance config (generalized per-instance, per-agent configuration)
    inst_data = data.get("instances")
    instances = None
    if inst_data:
        inst_fields = [
            InstanceFieldDef(
                key=f["key"],
                label=f.get("label", f["key"]),
                input_type=f.get("input_type", "text"),
                default=f.get("default", ""),
                required=f.get("required", False),
                secret=f.get("secret", False),
            )
            for f in inst_data.get("fields", [])
        ]
        instances = InstanceConfig(
            delivery=inst_data.get("delivery", "env"),
            fields=inst_fields,
            config_file_arg=inst_data.get("config_file_arg", ""),
            config_file_name=inst_data.get("config_file_name", "config.json"),
            transform=inst_data.get("transform", ""),
            max_instances=inst_data.get("max_instances", 0),
        )

    name = data["name"]

    # OAuth services validation (optional block, but strict when present).
    # Pass the raw server block so the validator can cross-check
    # `bearer_required=true` against `server.transport` being HTTP-class.
    try:
        _validate_oauth_services(oauth_data, name, data.get("server"))
    except ValueError as e:
        raise ValueError(f"{name}: invalid credentials.oauth block — {e}") from e

    # Webhook receiver validation (optional block). Strict — bad
    # blocks raise ValueError so install rejects with a clear error before
    # the dispatcher ever sees a malformed manifest at runtime.
    try:
        _validate_webhooks_block(webhooks_data, name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid credentials.webhooks block — {e}") from e
    # Cross-check: when both oauth and webhooks are declared, their
    # provider_id must match. Webhook dispatcher relies on this to find the
    # bound OAuth account at receive time.
    if (
        webhooks_data
        and webhooks_data.get("available", False)
        and oauth_data
    ):
        oauth_provider = oauth_data.get("provider_id", "")
        webhook_provider = webhooks_data.get("provider_id", "")
        if oauth_provider != webhook_provider:
            raise ValueError(
                f"{name}: credentials.webhooks.provider_id={webhook_provider!r} must "
                f"match credentials.oauth.provider_id={oauth_provider!r} (webhook "
                f"dispatcher uses provider_id to look up the OAuth account for vendor "
                f"API calls)"
            )

    # Per-tool cost rules (optional). Strict validation — bad blocks raise
    # ValueError so the install pipeline can reject the upload with a 400.
    try:
        costs = _parse_costs_block(data.get("costs"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid costs block — {e}") from e

    # Per-session prompt blocks (optional). Same strict-validation contract.
    try:
        agent_context_blocks = _parse_agent_context(data.get("agent_context"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid agent_context block — {e}") from e

    # Runtime tool filter declaration (optional).
    try:
        tool_filter = _parse_tool_filter(data.get("tool_filter"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid tool_filter block — {e}") from e

    # Tool-arg path declarations (optional). Strict — bad
    # JSONPath syntax fails the install with a clear error.
    try:
        tool_arg_paths = _parse_tool_arg_paths(data.get("tool_arg_paths"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid tool_arg_paths — {e}") from e

    # Hosted-relay config (optional). Strict — validates default_mode
    # and the api_key_relay ↔ delivery=="env" requirement. Needs `instances`
    # (parsed above) for that cross-check.
    try:
        hosted = _parse_hosted_block(data.get("hosted"), name, instances)
    except ValueError as e:
        raise ValueError(f"{name}: invalid hosted block — {e}") from e

    # Device-local MCP class fields (placement / display / capability / companion app).
    # Strict validation: bad values fail the manifest load with a clear error.
    placement = data.get("placement", "any")
    if placement not in _VALID_PLACEMENTS:
        raise ValueError(
            f"{name}: placement must be one of {sorted(_VALID_PLACEMENTS)}, got {placement!r}"
        )
    device_capability = data.get("device_capability")
    if device_capability is not None and device_capability not in _VALID_DEVICE_CAPABILITIES:
        raise ValueError(
            f"{name}: device_capability must be one of "
            f"{sorted(_VALID_DEVICE_CAPABILITIES)} or null, got {device_capability!r}"
        )
    try:
        companion_app = _parse_companion_app(data.get("companion_app"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid companion_app block — {e}") from e
    device_high_risk_tools_raw = data.get("device_high_risk_tools") or []
    if not isinstance(device_high_risk_tools_raw, list):
        raise ValueError(f"{name}: device_high_risk_tools must be a list of tool names")
    device_high_risk_tools = [str(t) for t in device_high_risk_tools_raw]

    return McpManifest(
        name=name,
        label=data.get("label", name),
        description=data.get("description", ""),
        version=data.get("version", "0.0.0"),
        category=data.get("category", "community"),
        server=server,
        credentials=credentials,
        config=config_fields,
        env=data.get("env", {}),
        agent_env=data.get("agent_env", {}),
        exclude_from=data.get("exclude_from", []),
        skills=skills,
        server_name=data.get("server_name", name),
        assignment_mode=data.get("assignment_mode", "auto"),
        requires_capability=data.get("requires_capability"),
        network_targets=[
            NetworkTargetDecl(
                source=nt.get("source", "config"),
                host_key=nt.get("host_key", ""),
                port_key=nt.get("port_key"),
                port_default=nt.get("port_default"),
            )
            for nt in (data.get("network_targets") or [])
            if nt.get("host_key")
        ],
        network_access_default=bool(data.get("network_access_default", True)),
        instances=instances,
        data_dirs=data.get("data_dirs", {}),
        sandbox_mounts=[
            SandboxMountDef(
                host=m.get("host", ""),
                sandbox=m.get("sandbox", ""),
                mode=m.get("mode", "ro"),
            )
            for m in data.get("sandbox", {}).get("mounts", [])
        ],
        hosted=hosted,
        system_requirements=system_requirements,
        outputs=[
            OutputRelocationDef(
                source=o.get("source", ""),
                destination_template=o.get("destination_template", ""),
                after_tools=list(o.get("after_tools", ["*"])),
                keep_recent=o.get("keep_recent"),
                gc_after=o.get("gc_after"),
            )
            for o in (data.get("outputs") or [])
            if o.get("source") and o.get("destination_template")
        ],
        path_env=_parse_path_env(data.get("path_env") or {}, name),
        tool_arg_paths=tool_arg_paths,
        costs=costs,
        agent_context=agent_context_blocks,
        tool_filter=tool_filter,
        placement=placement,
        requires_display=bool(data.get("requires_display", False)),
        device_capability=device_capability,
        companion_app=companion_app,
        device_high_risk_tools=device_high_risk_tools,
        patched=data.get("patched", False),
        patch_note=data.get("patch_note"),
        manifest_path=manifest_path,
        mcp_dir=mcp_dir,
    )
