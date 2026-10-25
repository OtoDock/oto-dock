"""The authoring check of an MCP package: what the community installer
would refuse, drop or accept in a folder, plus the catalog's conventions,
without installing anything (``check_package``, which the installer
re-exports). One rule per concern runs over a ``_Package`` in a fixed
order; the installer's own refusals (the manifest validator, the incoming
tree's rules, the compose check) are read from the installer itself, bound
at the end of this module."""

import json
from pathlib import Path

from fastapi import HTTPException

from services.mcp import (
    manifest_floor,
    mcp_installer,
    mcp_manifest_parse as _mmp,
    mcp_manifest_types as _mt,
)

_MASTER_KEY_TOKENS = ("${platform.api_key}", "${proxy_api_key}")
_ICON_SIZE = 256
_ICON_MAX_BYTES = 256 * 1024


def _icon_problem(path: Path) -> str | None:
    """Why ``icon.png`` is not the 256×256 PNG the catalog asks for; ``None``
    when it is (the signature and the IHDR chunk, no imaging library)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(24)
    except OSError as exc:
        return f"cannot be read ({exc})"
    if size > _ICON_MAX_BYTES:
        return f"is {size} bytes (limit {_ICON_MAX_BYTES})"
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return "is not a PNG"
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if (width, height) != (_ICON_SIZE, _ICON_SIZE):
        return f"is {width}x{height}, must be {_ICON_SIZE}x{_ICON_SIZE}"
    return None


def _bundled_skill_warnings(mcp_root: Path, skills: list) -> list[str]:
    """Each bundled skill file's front matter against the skill schema
    (``mcps/skill.schema.json``). Warnings: the installer refuses none of
    it (the scrub keeps the declarative keys), but an engine may not load a
    skill whose name or description is wrong. A flat file without a front
    matter is legal (the platform writes one when it serves the skill)."""
    from services.mcp.skill_format import FrontMatterError, load_frontmatter
    from storage import catalog_schema
    try:
        schema = catalog_schema.load_schema(catalog_schema.SKILL_SCHEMA)
    except (OSError, ValueError) as exc:
        return [f"the skill schema could not be read ({exc}): the skills' front matter was not checked"]
    out: list[str] = []
    for sk in skills:
        rel = sk.get("file") if isinstance(sk, dict) else None
        path = _mmp.resolve_skill_file(mcp_root, rel) if isinstance(rel, str) else None
        if path is None:
            continue
        try:
            fm = load_frontmatter(path.read_text(encoding="utf-8"))
        except FrontMatterError as exc:
            out.append(f"{rel} front matter is {exc}: the install keeps only its name and description")
            continue
        except (OSError, UnicodeDecodeError):
            continue
        if fm is None:
            continue
        out.extend(catalog_schema.skill_problems(fm, schema, f"{rel} front matter"))
        if isinstance(fm.get("name"), str) and fm["name"] != sk.get("id"):
            out.append(f"{rel} front matter: name {fm['name']!r} is not the skill's id {sk.get('id')!r}")
    return out


def check_package(mcp_root: Path, *, skipped: list[str] | tuple[str, ...] = ()) -> dict:
    """What ``install_from_extracted_folder`` would refuse, drop or accept in
    ``mcp_root``, plus the catalog's conventions, without installing
    anything: ``{ok, errors, warnings, summary}``. The authoring check tool
    (``validate_mcp_package`` in mcps-mcp) runs it on a snapshot of the
    author's folder; ``skipped`` names the entries the snapshot left out
    (dotfiles, ``node_modules``, ``venv``), judged here since the copy never
    holds them. Synchronous: run it through ``asyncio.to_thread``."""
    from urllib.parse import urlsplit
    errors: list[str] = []
    warnings: list[str] = []

    _check_package_files(mcp_root, skipped, errors, warnings)
    manifest_path = mcp_root / "manifest.json"
    if not manifest_path.is_file():
        errors.append("manifest.json not found at the package root")
        return {"ok": False, "errors": errors, "warnings": warnings, "summary": {}}
    try:
        raw = manifest_path.read_text()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        errors.append(f"manifest.json is not valid JSON: {exc}")
        return {"ok": False, "errors": errors, "warnings": warnings, "summary": {}}
    if not isinstance(data, dict):
        errors.append("manifest.json must hold an object")
        return {"ok": False, "errors": errors, "warnings": warnings, "summary": {}}

    errors.extend(_validate_manifest(data))
    named = _mmp.agent_context_credential_tokens(data.get("agent_context"))
    if named:
        warnings.append(
            f"agent_context names {', '.join(named)}: no credential value enters "
            "a prompt, so each renders empty and a requires on one skips its block")
    pkg = _Package(mcp_root, manifest_path, raw, data, errors, warnings)
    for rule in _PACKAGE_RULES:
        rule(pkg)
    server, source, runtime, remote, name, skills = (
        pkg.server, pkg.source, pkg.runtime, pkg.remote, pkg.name, pkg.skills)
    floor_reasons: list[str]
    floor, floor_reasons = "", []
    try:
        schema = manifest_floor.load_schema()
        floor, floor_reasons = manifest_floor.platform_floor(data, schema)
        for problem in manifest_floor.schema_errors(data, schema):
            # The git+ ref has its own, plainer warning above.
            if not (problem.startswith("/server/source:") and source.startswith("git+")):
                warnings.append(f"catalog schema: {problem}")
    except (OSError, ValueError, ImportError) as exc:
        warnings.append(f"the catalog schema could not be read ({exc}): fields not checked against it")
    if floor and manifest_floor.version_key(manifest_floor.declared_floor(data)) < manifest_floor.version_key(floor):
        warnings.append(
            f"platform_min_version should be at least {floor}: older platforms refuse or "
            f"ignore {', '.join(floor_reasons)}",
        )

    skills = [sk.get("id") for sk in skills if isinstance(sk, dict)]
    raw_creds = data.get("credentials")
    creds = raw_creds if isinstance(raw_creds, dict) else {}
    summary = {
        "name": name, "label": data.get("label", ""),
        "runtime": "remote" if remote and not runtime else runtime,
        "transport": server.get("transport", ""),
        "source": source, "version": data.get("version", ""),
        "credentials": creds.get("type", "none"),
        "instances": isinstance(data.get("instances"), dict),
        "skills": [s for s in skills if isinstance(s, str)],
        "host": urlsplit(str(server.get("url_template") or "")).hostname or "" if remote else "",
        "files": sum(1 for p in mcp_root.rglob("*") if p.is_file()),
        "platform_floor": floor,
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def _check_package_files(mcp_root: Path, skipped: list[str] | tuple[str, ...],
                         errors: list[str], warnings: list[str]) -> None:
    """The entries the install refuses or drops whatever the manifest says:
    a ``.env``, a dependency folder, a lock file."""
    for name in skipped:
        base = name.rsplit("/", 1)[-1]
        if base == ".env":
            errors.append(
                f"{name}: a .env file is refused at install; declare environment "
                "values in the manifest and keep secrets in instances or credentials",
            )
        elif base in _REFUSED_INCOMING_DIRS:
            errors.append(
                f"{name}: a {base} entry is refused at install; the platform "
                "installs dependencies itself from server.source",
            )
    for p in sorted(mcp_root.rglob("*")):
        rel = str(p.relative_to(mcp_root))
        if p.name == ".env" and p.is_file():
            errors.append(f"{rel}: a .env file is refused at install")
        elif p.name in _REFUSED_INCOMING_DIRS:
            errors.append(f"{rel}: a {p.name} entry is refused at install")
    for name in sorted(_DROPPED_INCOMING_FILES):
        if (mcp_root / name).exists():
            warnings.append(f"{name} is dropped at install (the platform resolves its own lock)")


class _Package:
    """What a ``check_package`` run reads: the folder, the manifest text and
    object, the facts the rules share, and the two lists they fill."""

    def __init__(self, mcp_root: Path, manifest_path: Path, raw: str, data: dict,
                 errors: list[str], warnings: list[str]) -> None:
        self.mcp_root = mcp_root
        self.manifest_path = manifest_path
        self.raw = raw
        self.data = data
        self.errors = errors
        self.warnings = warnings
        raw_server = data.get("server")
        server = raw_server if isinstance(raw_server, dict) else {}
        source = server.get("source", "") if isinstance(server.get("source", ""), str) else ""
        runtime = server.get("runtime", "")
        rt = _mt.runtime_of(runtime) if isinstance(runtime, str) else None
        remote = source.startswith("remote:")
        name = data.get("name", "")
        raw_skills = data.get("skills")
        skills = raw_skills if isinstance(raw_skills, list) else []
        self.server, self.source, self.runtime, self.rt, self.remote, self.name, self.skills = (
            server, source, runtime, rt, remote, name, skills)


def _check_package_identity(pkg: _Package) -> None:
    """The name, the category and the server source."""
    data, errors, source, remote, name = pkg.data, pkg.errors, pkg.source, pkg.remote, pkg.name
    if isinstance(name, str) and name and not _is_safe_name(name):
        errors.append(f"name {name!r} must be lowercase letters, digits, dashes and underscores")
    if data.get("category") not in (None, "community"):
        errors.append("category must be \"community\" (core and custom ship with the platform)")
    if source and not remote and mcp_installer.parse_source(source) is None:
        errors.append(
            f"server.source {source!r} is not an accepted source (npm:<package>, "
            "pypi:<package>, git+<url>@<ref>, docker:<image>, remote:<host>)",
        )


def _check_package_skill_files(pkg: _Package) -> None:
    """Each skill's file, and its front matter (warnings)."""
    mcp_root, errors, warnings, skills = pkg.mcp_root, pkg.errors, pkg.warnings, pkg.skills
    for sk in skills:
        if isinstance(sk, dict) and isinstance(sk.get("file"), str) and \
                _mmp.skill_file_error(sk["file"]) is None and \
                _mmp.resolve_skill_file(mcp_root, sk["file"]) is None:
            errors.append(f"skill {sk.get('id')}: file {sk['file']!r} is not a regular file inside the package")
    warnings.extend(_bundled_skill_warnings(mcp_root, skills))


def _check_package_container(pkg: _Package) -> None:
    """A container runtime's compose file, image, address and version."""
    mcp_root, data, errors, warnings, server, rt = (
        pkg.mcp_root, pkg.data, pkg.errors, pkg.warnings, pkg.server, pkg.rt)
    compose_file = server.get("docker_compose") or ""
    if rt is not None and rt.container:
        if not compose_file:
            errors.append("server.docker_compose must name the compose file of a docker MCP")
        elif isinstance(compose_file, str) and _mmp.skill_file_error(compose_file) is None and \
                _mmp.resolve_skill_file(mcp_root, compose_file) is None:
            errors.append(f"server.docker_compose {compose_file!r} is not a regular file inside the package")
        if not server.get("image"):
            warnings.append(
                "server.image is absent: the containerised deployment cannot build "
                "an image, so the package installs on bare metal only",
            )
        url_template = str(server.get("url_template") or "")
        if "${docker_mcp_host}" not in url_template:
            warnings.append(
                "server.url_template should be http://${docker_mcp_host}:${port}: a fixed "
                "host breaks the containerised deployment",
            )
        if not str(data.get("version") or ""):
            errors.append("version must be set for a docker MCP (the image tag)")


def _check_package_git_source(pkg: _Package) -> None:
    """A git+ source's version and its pinned commit."""
    data, errors, warnings, source = pkg.data, pkg.errors, pkg.warnings, pkg.source
    if source.startswith("git+") and not str(data.get("version") or ""):
        errors.append("version must be set for a git+ source (the pinned ref)")
    if source.startswith("git+") and not git_ref_is_commit(source):
        warnings.append(
            "server.source pins a tag or a branch: the community catalog takes a git+ "
            "source only at a full 40-character commit (a tag can move upstream), "
            "while an admin may still install this package from a ZIP",
        )


def _check_package_install_source(pkg: _Package) -> None:
    """A node or python package's pin and its requirements file."""
    mcp_root, warnings, source, runtime, rt = (
        pkg.mcp_root, pkg.warnings, pkg.source, pkg.runtime, pkg.rt)
    if rt is not None and rt.installed and source and not source.startswith("git+"):
        parsed = mcp_installer.parse_source(source)
        if parsed is not None and parsed.version:
            warnings.append(
                f"server.source pins {parsed.version}: the catalog leaves node and "
                "python packages unpinned and the platform pins the installed version",
            )
    if rt is not None and rt.installed and runtime == _mt.RUNTIME_PYTHON and (mcp_root / "requirements.txt").is_file():
        warnings.append("requirements.txt is dropped at install: a python package installs from server.source")


def _check_package_platform_env(pkg: _Package) -> None:
    """The environment names the platform injects itself."""
    data, warnings = pkg.data, pkg.warnings
    for block in ("env", "agent_env"):
        values = data.get(block)
        if isinstance(values, dict):
            for key in values:
                if str(key).startswith("OTO_") or str(key) in ("PROXY_URL", "PROXY_API_KEY"):
                    warnings.append(
                        f"{block}.{key}: the OTO_* set, PROXY_URL and PROXY_API_KEY are "
                        "injected by the platform and must not be declared",
                    )


def _check_package_master_key(pkg: _Package) -> None:
    """The master key, which no MCP is given."""
    raw, errors = pkg.raw, pkg.errors
    for token in _MASTER_KEY_TOKENS:
        if token in raw:
            errors.append(f"the manifest references the master key ({token}), which is never given to an MCP")


def _check_package_readme_icon(pkg: _Package) -> None:
    """The README and the icon the catalog shows."""
    mcp_root, warnings = pkg.mcp_root, pkg.warnings
    if not (mcp_root / "README.md").is_file():
        warnings.append("README.md is missing: the catalog shows it in the install dialog")
    icon = mcp_root / "icon.png"
    if icon.is_file():
        problem = _icon_problem(icon)
        if problem:
            warnings.append(f"icon.png {problem}")


def _check_package_author(pkg: _Package) -> None:
    """The upstream project's author and repository."""
    from urllib.parse import urlsplit
    data, warnings = pkg.data, pkg.warnings
    if not str(data.get("author") or "").strip():
        warnings.append("author is missing: name the project whose server code the package wraps")
    author_url = str(data.get("author_url") or "")
    author_parts = urlsplit(author_url)
    if author_parts.scheme != "https" or not author_parts.netloc:
        warnings.append("author_url should be the https:// repository of the upstream project")


def _check_package_tiers(pkg: _Package) -> None:
    """critical is the platform's own tier: a package's tools run as
    sensitive at most (the parse lowers them, so this reads the raw block)."""
    perms = pkg.data.get("permissions")
    if not isinstance(perms, dict):
        return
    named = ["default_tier"] if perms.get("default_tier") == "critical" else []
    rules = perms.get("rules")
    named += [str(r.get("tool")) for r in (rules if isinstance(rules, list) else [])
              if isinstance(r, dict) and r.get("tier") == "critical"]
    if named:
        pkg.warnings.append(
            f"permissions: critical is kept for OtoDock's own tools, so {', '.join(named)} "
            "will run as sensitive")


def _check_package_parse(pkg: _Package) -> None:
    """The full manifest parse, once nothing else is wrong."""
    manifest_path, errors = pkg.manifest_path, pkg.errors
    if not errors:
        try:
            parsed_manifest = _mmp._parse_manifest(manifest_path)
        except ValueError as exc:
            errors.append(f"manifest block rejected: {exc}")
            parsed_manifest = None
        if parsed_manifest is None and not errors:
            errors.append("the manifest could not be parsed")


def _check_package_compose(pkg: _Package) -> None:
    """A container's compose file against the install's own rules, once
    nothing else is wrong."""
    mcp_root, errors, rt, name = pkg.mcp_root, pkg.errors, pkg.rt, pkg.name
    if not errors and rt is not None and rt.container:
        try:
            _check_incoming_compose(mcp_root, str(name))
        except HTTPException as exc:
            errors.append(str(exc.detail))


# check_package's rules in the order they run; the last two read the
# errors the others found.
_PACKAGE_RULES = (
    _check_package_identity,
    _check_package_skill_files,
    _check_package_container,
    _check_package_git_source,
    _check_package_install_source,
    _check_package_platform_env,
    _check_package_master_key,
    _check_package_readme_icon,
    _check_package_author,
    _check_package_tiers,
    _check_package_parse,
    _check_package_compose,
)


# The installer's rules the check applies, bound after this module's own
# definitions: the installer imports this module at its end, so either one
# may be imported first.
from services.community.community_installer import (
    _DROPPED_INCOMING_FILES,
    _REFUSED_INCOMING_DIRS,
    _check_incoming_compose,
    _is_safe_name,
    _validate_manifest,
    git_ref_is_commit,
)
