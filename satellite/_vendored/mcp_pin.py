"""What a build leaves in the files a tool's version hash reads.

A community tool from npm or PyPI is unpinned in the catalog (``npm:pkg``,
``pypi:pkg``): its version of record is the registry's, read back from the
build. The build runs on a compute machine, never on the platform, so the
platform and the machine that built both call ``settle_built`` with that
version: both then hold the same pinned manifest and the same
``package.json``, and ``mcp_installer.compute_version_hash`` answers the same
for both without a second build.

Stdlib-only and vendored into the satellite beside ``mcp_installer``
(``scripts/sync-satellite-code.sh``; hashed against ``SHARED_MCP_PIN_HASH``).
"""

from __future__ import annotations

import json
from pathlib import Path

from .mcp_installer import ParsedSource, _node_package_json, parse_source, plain_version

#: The registries whose builds read a version back.
PINNED_REGISTRIES = ("npm", "pypi")


def pin_manifest(mcp_dir: Path, registry: str, package: str, resolved_version: str,
                 *, extras: tuple[str, ...] = ()) -> None:
    """Pin the tool's manifest to ``resolved_version``: its ``version`` and
    its ``server.source`` (``{registry}:{package}@{resolved_version}``, a PyPI
    source keeping its ``extras``: ``pypi:name[a,b]@1.2.3``). Detection and
    display are then deterministic, and every machine that gets the tool
    later installs exactly that version.

    Written as bytes: a text-mode write would turn each newline into two
    bytes on Windows and drift the hash."""
    manifest_path = Path(mcp_dir) / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["version"] = resolved_version
    data.setdefault("server", {})["source"] = ParsedSource(
        registry, package, "", tuple(extras)).pinned(resolved_version)
    manifest_path.write_bytes(json.dumps(data, indent=2).encode("utf-8"))


def settle_built(mcp_dir: Path, source: str, resolved_version: str) -> str:
    """Leave in ``mcp_dir`` what a build of ``source`` at ``resolved_version``
    leaves in its hashed files: the manifest pinned (``pin_manifest``) and,
    for a Node tool, the one-dependency ``package.json`` the installer
    writes. Answers the pinned source, ``""`` when nothing was pinned (a
    source of no registry, a version that is not a plain one)."""
    parsed = parse_source(source) if source else None
    version = plain_version(resolved_version)
    if parsed is None or parsed.registry not in PINNED_REGISTRIES or not version:
        return ""
    if parsed.registry == "npm":
        (Path(mcp_dir) / "package.json").write_bytes(_node_package_json(parsed.package, version))
    pin_manifest(Path(mcp_dir), parsed.registry, parsed.package, version, extras=parsed.extras)
    return parsed.pinned(version)
