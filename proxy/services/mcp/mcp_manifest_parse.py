"""MCP manifest parsing — raw ``manifest.json`` dict → typed ``McpManifest``.

The top-level `_parse_manifest` orchestrator assembles a validated `McpManifest`
from the block parsers in ``mcp_manifest_blocks`` (re-exported here, with the
oauth and webhook validators). Strict-by-design: structural defects raise
`ValueError` so a bad manifest is rejected at install/scan time.

`services.mcp.mcp_registry` re-exports `_parse_manifest` (driven by `scan_manifests`)
and the sub-parsers (the parser test-suite). The in-memory `_manifests` cache,
`scan_manifests`, and the post-scan builder-transport validation live in
``mcp_manifest_store``.
"""

import json
import logging
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from core.config import deployment
from services.infra import path_confinement
from services.mcp.mcp_manifest_blocks import (  # noqa: F401
    RESULT_FILES_DEFAULT_SUBDIR,
    RESULT_FILES_SUBDIR_RE,
    TOOL_TIMEOUT_SEC_MAX,
    TOOL_TIMEOUT_SEC_MIN,
    _bounded_tiers,
    _busy_endpoint,
    _declared_env_names,
    _declares_token_folder,
    _parse_agent_context,
    _parse_builder_block,
    _parse_costs_block,
    _parse_hosted_block,
    _parse_path_env,
    _parse_permissions_block,
    _parse_result_files,
    _parse_tool_arg_paths,
    _parse_tool_filter,
    _server_starts,
    _tool_timeout_sec,
    _validate_api_key_header,
    _validate_exit,
    _validate_tool_arg_json_path,
    agent_context_credential_tokens,
    result_files_subdir_ok,
    ships_with_platform,
)
from services.mcp.mcp_manifest_types import (
    _VALID_DEVICE_CAPABILITIES,
    _VALID_PLACEMENTS,
    _VALID_REMOTE_POLICIES,
    REPLACES_KEY_RE,
    SKILL_AUDIENCES,
    SKILL_ID_MAX_LEN,
    SKILL_ID_RE,
    SKILL_LOADING_DEFAULT,
    VALID_SKILL_LOADING,
    ConfigField,
    CredentialConfig,
    InstanceConfig,
    InstanceFieldDef,
    McpManifest,
    NetworkTargetDecl,
    OutputRelocationDef,
    ReplacesDecl,
    SandboxMountDef,
    ServerConfig,
    SkillDef,
    SystemRequirements,
    _parse_companion_app,
)
from services.mcp.mcp_validate_oauth import _validate_oauth_services
from services.mcp.mcp_validate_webhooks import _validate_webhooks_block

# A ``skills[].file`` value names a file INSIDE its MCP folder and nothing
# else. The lexical rule below runs at parse, at the install gate and in front
# of both readers; the filesystem rule (``resolve_skill_file``) judges a
# symlink by its target. Windows forms are refused on every host because the
# same manifest is synced to satellites of every platform.
SKILL_FILE_MAX_LEN = 512
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# A PyPI distribution name (``server.source_build`` entries).
SOURCE_BUILD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def skill_file_error(value: Any) -> str | None:
    """Why ``value`` is not an acceptable ``skills[].file``, or ``None``."""
    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if len(value) > SKILL_FILE_MAX_LEN:
        return f"is longer than {SKILL_FILE_MAX_LEN} characters"
    if any(ord(c) < 0x20 for c in value):
        return "carries a control character"
    if "\\" in value:
        return "carries a backslash"
    if (value.startswith("/") or _DRIVE_RE.match(value)
            or PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()):
        return "is absolute"
    try:
        if path_confinement.normalize_rel_path(value) != value:
            return "is not a plain relative path"
    except path_confinement.PathOutsideRoot:
        return "is not a plain relative path"
    return None


# An absolute path inside a value: at its start or after a separator.
_ABS_PATH_IN_VALUE = re.compile(r"(?:^|(?<=[\s=:,;]))(/[^\s=:,;]*)")


def sessions_folder_errors(data: dict) -> list[str]:
    """A package's command, arguments and environment never name the
    platform's sessions folder (its tokens and session builds). Judged with
    the path tokens filled in as the install would (``${mcp_dir}`` as the
    folder it installs into) and the path normalised. Tokens a session's
    start fills in are not judged here: the session's config build copies
    nothing from that folder but the tool's own instance file."""
    raw_server = data.get("server")
    server: dict = raw_server if isinstance(raw_server, dict) else {}
    values: list[tuple[str, object]] = [("server.command", server.get("command"))]
    args = server.get("args")
    values += [(f"server.args entry {i + 1}", a) for i, a in enumerate(args)] if isinstance(args, list) else []
    for block in ("env", "agent_env"):
        env = data.get(block)
        values += [(f"{block}.{k}", v) for k, v in env.items()] if isinstance(env, dict) else []
    import config
    tokens = {
        "${platform_root}": str(config.PLATFORM_ROOT),
        "${mcp_dir}": str(config.MCPS_DIR / str(data.get("category") or "community") / str(data.get("name", ""))),
        "${platform.host_agents_dir}": str(config.AGENTS_DIR),
    }
    sessions = {os.path.normpath(str(config.SESSIONS_DIR)), os.path.realpath(config.SESSIONS_DIR)}
    errors = []
    for field, value in values:
        if not isinstance(value, str):
            continue
        for token, path in tokens.items():
            value = value.replace(token, path)
        for found in _ABS_PATH_IN_VALUE.findall(value):
            path = os.path.normpath(found)
            if any(path == s or path.startswith(s.rstrip("/") + "/") for s in sessions):
                errors.append(f"{field} must not name the platform's sessions folder")
                break
    return errors


def sandbox_mount_errors(data: dict) -> list[str]:
    """A ``sandbox.mounts`` host is the package's own folder or a folder
    inside it, never another place on the host."""
    sandbox = data.get("sandbox")
    if sandbox is None:
        return []
    mounts = sandbox.get("mounts", []) if isinstance(sandbox, dict) else None
    if not isinstance(mounts, list):
        return ["sandbox.mounts must be a list"]
    errors = []
    for mount in mounts:
        host = mount.get("host") if isinstance(mount, dict) else None
        rest = host[len("${mcp_dir}"):] if isinstance(host, str) and host.startswith("${mcp_dir}") else None
        if rest == "" or (rest is not None and rest.startswith("/") and skill_file_error(rest[1:]) is None):
            continue
        errors.append(f"sandbox.mounts host {host!r} must be the tool's own folder (${{mcp_dir}}) "
                      "or a folder inside it")
    return errors


def resolve_skill_file(mcp_dir: Path, rel: str) -> Path | None:
    """The regular file ``rel`` names inside ``mcp_dir`` (symlinks followed
    and judged by where they point), or ``None`` when the value fails the
    lexical rule, escapes the folder or is not a regular file."""
    if skill_file_error(rel) is not None:
        return None
    try:
        resolved = path_confinement.resolve_under(Path(mcp_dir) / rel, mcp_dir)
    except (path_confinement.PathOutsideRoot, OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def read_skill_file(mcp_dir: Path, rel: str) -> str | None:
    """The text of the skill file ``rel`` names (:func:`resolve_skill_file`'s
    answer, where an honest link inside the folder lands), read beneath the
    tool's own folder with no link followed and under the skills' cap: the
    file authorized is the file read. None when it is refused, unreadable
    or not text."""
    from core.sandbox.skills_materializer import _MAX_SKILL_BYTES
    from services.infra import safe_fs
    path = resolve_skill_file(mcp_dir, rel)
    if path is None:
        return None
    try:
        data = safe_fs.read_bytes_beneath(mcp_dir, safe_fs.rel_under(path, mcp_dir), max_size=_MAX_SKILL_BYTES)
        return data.decode("utf-8")
    except (OSError, ValueError):
        return None

logger = logging.getLogger(__name__)

# A Docker MCP's service name is the host the platform dials it at on the
# install's own network: one DNS label, never a dotted name or an address.
_SERVICE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


def _service_name(value: object, mcp: str) -> str:
    """``server.service_name`` when it is one DNS label the resolver does
    not read as an address or as this machine, else "" (the platform then
    dials the MCP by its name), with a warning."""
    name = value if isinstance(value, str) else ""
    if name and (not _SERVICE_LABEL.match(name) or deployment.address_like(name)):
        logger.warning("MCP %s: service_name %r is not one DNS label a tool may take; ignored",
                       mcp, name)
        return ""
    return name


def _parse_manifest(manifest_path: Path) -> McpManifest | None:
    """Parse a manifest.json file into an McpManifest."""
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as e:
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
        except (ImportError, ValueError, TypeError):
            logger.warning(
                "%s: invalid server.version_constraint %r (expected a PEP 440 "
                "specifier like '>=2,<3') — treating as unbounded",
                data.get("name", manifest_path.parent.name), version_constraint,
            )
            version_constraint = ""
    source_build = []
    for entry in srv_data.get("source_build") or []:
        if isinstance(entry, str) and SOURCE_BUILD_RE.fullmatch(entry):
            source_build.append(entry)
        else:
            logger.warning(
                "%s: server.source_build entry %r is not a package name; dropped",
                data.get("name", manifest_path.parent.name), entry,
            )
    # ``server.docker_compose`` names a file inside the MCP folder, under the
    # same lexical rule as ``skills[].file``: compose runs it under this MCP's
    # project and the T2 rewrite rewrites it in place.
    docker_compose = srv_data.get("docker_compose", "")
    compose_error = skill_file_error(docker_compose) if docker_compose != "" else None
    if compose_error is not None:
        logger.warning(
            "%s: server.docker_compose %r %s, dropped",
            data.get("name", manifest_path.parent.name), docker_compose, compose_error,
        )
        docker_compose = ""
    server = ServerConfig(
        runtime=srv_data.get("runtime", "python"),
        transport=srv_data.get("transport", "stdio"),
        command=srv_data.get("command", ""),
        args=srv_data.get("args", []),
        source=srv_data.get("source", ""),
        docker_compose=docker_compose,
        port=srv_data.get("port", 0),
        health_endpoint=srv_data.get("health_endpoint", ""),
        busy_endpoint=_busy_endpoint(srv_data, data.get("name", manifest_path.parent.name)),
        url_template=srv_data.get("url_template", ""),
        proxy_callbacks=bool(srv_data.get("proxy_callbacks", False)),
        service_name=_service_name(srv_data.get("service_name", ""), data.get("name", "")),
        image=srv_data.get("image", ""),
        version_constraint=version_constraint,
        source_build=source_build,
        tool_timeout_sec=_tool_timeout_sec(srv_data, data.get("name", manifest_path.parent.name)),
        starts=_server_starts(srv_data, data.get("name", manifest_path.parent.name), mcp_dir),
    )

    # Credential config.
    cred_data = data.get("credentials", {})
    oauth_data = cred_data.get("oauth")
    webhooks_data = cred_data.get("webhooks")
    api_key_header = cred_data.get("api_key_header")
    exit_data = cred_data.get("exit")
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
        api_key_header=api_key_header,
        exit=exit_data,
        ui_type=cred_data.get("ui_type", ""),
        app_credential_fields=app_cred_fields,
        app_kinds=oauth_data.get("app_kinds") if oauth_data else None,
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
    raw_skills = data.get("skills", [])
    if raw_skills is None:
        raw_skills = []
    if not isinstance(raw_skills, list):
        logger.warning(
            "%s: skills is not a list; skills dropped",
            data.get("name", mcp_dir.name),
        )
        raw_skills = []
    for sk in raw_skills:
        if (not isinstance(sk, dict) or not isinstance(sk.get("id"), str)
                or not isinstance(sk.get("file"), str)):
            logger.warning(
                "%s: skill entry %r needs a string id and file; skill dropped",
                data.get("name", mcp_dir.name), sk,
            )
            continue
        skill_id = sk["id"]
        if not SKILL_ID_RE.fullmatch(skill_id) or len(skill_id) > SKILL_ID_MAX_LEN:
            logger.warning(
                "%s: skill id %r is not a valid skill name (lowercase alnum "
                "+ single hyphens, max %d chars) — skill dropped",
                data.get("name", mcp_dir.name), skill_id, SKILL_ID_MAX_LEN,
            )
            continue
        file_error = skill_file_error(sk["file"])
        if file_error is not None:
            logger.warning(
                "%s: skill %r file %r %s; skill dropped",
                data.get("name", mcp_dir.name), skill_id, sk["file"], file_error,
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
        audience = sk.get("audience", "") or ""
        if skill_audience_error(audience) is not None:
            # Fail closed: a skill meant for a tier must not reach everyone
            # because its word is misspelt.
            logger.warning(
                "%s: skill %r has an invalid audience %r (expected one of %s); "
                "skill dropped",
                data.get("name", mcp_dir.name), skill_id, audience,
                list(SKILL_AUDIENCES),
            )
            continue
        skills.append(SkillDef(
            id=skill_id,
            file=sk["file"],
            description=sk.get("description", ""),
            default_exclude_from=sk.get("default_exclude_from", []),
            loading=loading,
            audience=audience,
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

    try:
        _validate_api_key_header(
            api_key_header, cred_data, data.get("server"), data.get("instances"),
            data.get("hosted"),
        )
    except ValueError as e:
        raise ValueError(f"{name}: invalid credentials.api_key_header block — {e}") from e

    try:
        _validate_exit(exit_data, cred_data, data.get("instances"), data.get("hosted"))
    except ValueError as e:
        raise ValueError(f"{name}: invalid credentials.exit block — {e}") from e

    audience = data.get("audience", "")
    if audience not in ("", *SKILL_AUDIENCES):
        raise ValueError(
            f"{name}: audience must be one of {list(SKILL_AUDIENCES)} when declared, "
            f"got {audience!r}"
        )

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

    # Per-tool permission tiers (optional). Same strict-validation contract.
    try:
        permissions = _parse_permissions_block(data.get("permissions"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid permissions block — {e}") from e
    permissions = _bounded_tiers(permissions, name, mcp_dir)

    # Per-session prompt blocks (optional). Same strict-validation contract.
    try:
        agent_context_blocks = _parse_agent_context(data.get("agent_context"), name)
    except ValueError as e:
        raise ValueError(f"{name}: invalid agent_context block — {e}") from e

    # Runtime tool filter declaration (optional). Its two variables are
    # written into the MCP's env beside every declared key, so a name that is
    # also one of them would replace that value silently: refused here.
    path_env = _parse_path_env(data.get("path_env") or {}, name)
    legacy_token_dir = _declares_token_folder(data.get("path_env") or {})
    try:
        tool_filter = _parse_tool_filter(data.get("tool_filter"), name)
        if tool_filter is not None:
            taken = _declared_env_names(data, config_fields, credentials, instances, path_env)
            for var in (tool_filter.env_var_name, tool_filter.regex_env_var_name):
                if var in taken:
                    raise ValueError(
                        f"{var} is also a declared env, config, credential, "
                        "instance or path_env key")
    except ValueError as e:
        raise ValueError(f"{name}: invalid tool_filter block — {e}") from e

    # The tools whose pictures and file blobs the platform keeps (optional).
    # A tool output relocation already moves would be saved twice.
    try:
        result_files = _parse_result_files(data.get("result_files"), name)
        relocated = {t for o in (data.get("outputs") or []) if isinstance(o, dict)
                     for t in (o.get("after_tools") or ["*"])}
        if result_files and relocated & ({"*"} | set(result_files.tools)):
            raise ValueError("a tool both outputs and result_files keep")
    except ValueError as e:
        raise ValueError(f"{name}: invalid result_files block — {e}") from e

    # Direct-LLM deferred tool loading opt-out (optional, strict bool).
    always_load = data.get("always_load", False)
    if not isinstance(always_load, bool):
        raise ValueError(
            f"{name}: always_load must be true or false, got {always_load!r}"
        )

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
    remote_policy = data.get("remote_policy", "any")
    if remote_policy not in _VALID_REMOTE_POLICIES:
        raise ValueError(
            f"{name}: remote_policy must be one of "
            f"{sorted(_VALID_REMOTE_POLICIES)}, got {remote_policy!r}"
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
        author=str(data.get("author") or ""),
        author_url=str(data.get("author_url") or ""),
        assignment_mode=data.get("assignment_mode", "auto"),
        audience=audience,
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
        network_exit=data.get("network_exit") is True,
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
        result_files=result_files,
        path_env=path_env,
        legacy_token_dir=legacy_token_dir,
        tool_arg_paths=tool_arg_paths,
        costs=costs,
        permissions=permissions,
        agent_context=agent_context_blocks,
        tool_filter=tool_filter,
        always_load=always_load,
        placement=placement,
        remote_policy=remote_policy,
        requires_display=bool(data.get("requires_display", False)),
        device_capability=device_capability,
        companion_app=companion_app,
        replaces=parse_replaces(data.get("replaces"), name),
        patched=data.get("patched", False),
        patch_note=data.get("patch_note"),
        manifest_path=manifest_path,
        mcp_dir=mcp_dir,
    )


def skill_audience_error(audience) -> str | None:
    """Why a ``skills[].audience`` value is not one of the three tiers (or
    empty); ``None`` when it is."""
    if audience in ("", None):
        return None
    if not isinstance(audience, str) or audience not in SKILL_AUDIENCES:
        return f"must be one of {', '.join(SKILL_AUDIENCES)}"
    return None


def replaces_entry_error(entry) -> str | None:
    """Why one ``replaces[]`` entry is malformed; ``None`` when it is well
    formed: an object with a non-empty string ``source`` and an optional
    ``credentials`` map of identifiers (``REPLACES_KEY_RE``) to identifiers,
    no key starting with ``_`` on either side. Shared by the parser (which
    drops a bad entry), the install gate and the package check (which refuse
    it) and mirrored by the catalogs' registry generators."""
    if not isinstance(entry, dict):
        return "must be an object"
    source = entry.get("source")
    if not isinstance(source, str) or not source.strip():
        return "needs a non-empty string source"
    creds = entry.get("credentials", {})
    if creds is None:
        creds = {}
    if not isinstance(creds, dict):
        return "credentials must be an object of old key to new key"
    for old, new in creds.items():
        if not isinstance(old, str) or not REPLACES_KEY_RE.fullmatch(old):
            return f"credential key {old!r} is not a plain identifier"
        if not isinstance(new, str) or not REPLACES_KEY_RE.fullmatch(new):
            return f"credential key {new!r} is not a plain identifier"
    return None


def parse_replaces(raw, label: str) -> list[ReplacesDecl]:
    """The ``replaces`` list of a manifest, malformed entries dropped with a
    warning (the installers refuse them instead)."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        logger.warning("%s: replaces is not a list; dropped", label)
        return []
    out: list[ReplacesDecl] = []
    for entry in raw:
        error = replaces_entry_error(entry)
        if error is not None:
            logger.warning("%s: replaces entry %r %s; dropped", label, entry, error)
            continue
        out.append(ReplacesDecl(
            source=entry["source"].strip(),
            credentials={str(k): str(v) for k, v in (entry.get("credentials") or {}).items()},
        ))
    return out
