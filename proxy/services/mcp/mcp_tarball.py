"""Build and cache gzipped tarballs of MCP folders for satellite install.

When a remote session needs an MCP that isn't on its satellite yet (or is
a version behind), the proxy ships the MCP code as a tarball inside a
`sync_mcps` WS command. Building a tarball on every session start would
be wasteful — most sessions on the same satellite reuse the same set of
MCPs — so we cache tarballs by `version_hash` at
`sessions/mcp-tarball-cache/{version_hash}.tar.gz`.

- Cache is keyed by `mcp_installer.compute_version_hash()` so a change to
  manifest / requirements / patches / package.json invalidates it
  automatically. An MCP update that keeps install-relevant inputs
  unchanged (e.g. a README edit) reuses the cached tarball.
- Cache is bounded: on startup we GC entries not touched in 7 days; when
  total size exceeds `MAX_CACHE_BYTES` we evict oldest-mtime-first.
- Exclusions avoid shipping heavy, non-portable state (`venv/`,
  `node_modules/`, `__pycache__/`, `.git/`, `screenshots/`). Inclusions
  cover manifest + source + requirements + lockfiles + patches + skills.
  The satellite installs `venv/`/`node_modules/` itself from the
  manifest's `source` field.
- Manifest-declared ``data_dirs`` (machine-local state: ssh-server's
  `config/` + `keys/`) NEVER ship — and `keys/` is excluded even without a
  declaration. The keys dir holds raw private keys; a tarball is broadcast
  to every satellite that syncs the MCP (including user-paired laptops), so
  secret material must never ride it. The satellite mirrors this contract:
  its installer treats `keys/`/`config/`/`screenshots/` as machine-local
  (`preserve_subdirs`) and its `compute_version_hash` ignores non-source
  files — meaning a key upload never drifts the hash, so a poisoned cache
  entry would otherwise be served indefinitely. `_TARBALL_FORMAT` is baked
  into the cache filename so an exclusion-rule change invalidates all
  pre-fix cache entries at once.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from services.mcp import mcp_installer

logger = logging.getLogger("claude-proxy.mcp-tarball")

# --- Config ---

MAX_CACHE_BYTES = 500 * 1024 * 1024   # 500 MB
STALE_CACHE_AGE_S = 7 * 24 * 3600     # 7 days

_EXCLUDE_DIRS = {
    "venv", ".venv", "node_modules", "__pycache__", ".git",
    "screenshots", ".mypy_cache", ".pytest_cache", ".cache",
}
_EXCLUDE_FILES_SUFFIX = (".pyc", ".pyo", ".log", ".partial")

# Top-level dirs that hold SECRETS and must never ship regardless of what the
# manifest declares (defense in depth on top of the data_dirs exclusion).
_ALWAYS_EXCLUDE_TOP_DIRS = frozenset({"keys"})

# Bump when the exclusion rules change: the cache is keyed by version_hash,
# and data files (e.g. an uploaded SSH key) don't drift that hash — so without
# a format tag in the filename, a pre-fix cache entry (possibly containing
# secrets) would keep being served forever. Old-format entries are never
# served again and age out via gc().
_TARBALL_FORMAT = "d2"


# --- Result ---


@dataclass
class TarballResult:
    tarball_b64: str      # base64-encoded gzipped tar
    version_hash: str
    size_bytes: int


# --- Helpers ---


def _cache_dir() -> Path:
    import config
    d = config.BASE_DIR / "sessions" / "mcp-tarball-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path_for(version_hash: str) -> Path:
    return _cache_dir() / f"{version_hash}.{_TARBALL_FORMAT}.tar.gz"


def _data_dir_names(manifest) -> frozenset[str]:
    """Top-level dir names excluded from this MCP's tarball.

    Union of the manifest's declared ``data_dirs`` (machine-local state —
    each value like ``"keys/"`` reduces to its first path segment) and the
    hard :data:`_ALWAYS_EXCLUDE_TOP_DIRS` secrets floor.
    """
    names = set(_ALWAYS_EXCLUDE_TOP_DIRS)
    data_dirs = getattr(manifest, "data_dirs", None)
    if isinstance(data_dirs, dict):
        for v in data_dirs.values():
            seg = str(v).strip("/").split("/")[0]
            if seg:
                names.add(seg)
    return frozenset(names)


def _should_include(
    path: Path, mcp_root: Path, exclude_top: frozenset[str] = frozenset(),
) -> bool:
    """True if this path should be packed into the tarball."""
    rel = path.relative_to(mcp_root)
    parts = rel.parts
    if parts and parts[0] in exclude_top:
        return False
    if any(p in _EXCLUDE_DIRS for p in parts):
        return False
    if path.is_file() and path.suffix in _EXCLUDE_FILES_SUFFIX:
        return False
    return True


def _build_bytes(mcp_dir: Path, exclude_top: frozenset[str] = frozenset()) -> bytes:
    """Pack the MCP folder into a gzipped tar archive and return the bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for entry in sorted(mcp_dir.rglob("*")):
            if entry.is_symlink():
                continue  # follow-less: matches file_sync convention
            if not _should_include(entry, mcp_dir, exclude_top):
                continue
            if entry.is_file():
                # Name files relative to the MCP folder, stripping the
                # platform-absolute prefix so the satellite extracts into
                # {mcps_dir}/{category}/{name}/{relative-path}.
                arcname = str(entry.relative_to(mcp_dir))
                tar.add(str(entry), arcname=arcname, recursive=False)
    return buf.getvalue()


# --- Public API ---


def build_tarball(mcp_name: str) -> TarballResult:
    """Build (or reuse) the tarball for an MCP in the registry.

    Returns a `TarballResult` with the base64-encoded tarball and the
    `version_hash`. Call sites are responsible for shipping this to
    satellites via `sync_mcps`.

    Raises `FileNotFoundError` if the MCP doesn't exist in the registry.
    """
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None:
        raise FileNotFoundError(f"MCP not found in registry: {mcp_name}")

    mcp_dir = manifest.mcp_dir
    version_hash = mcp_installer.compute_version_hash(mcp_dir)

    cache_path = _cache_path_for(version_hash)
    if cache_path.is_file():
        content = cache_path.read_bytes()
        # Touch the cache entry so GC considers it recently used.
        with contextlib.suppress(OSError):
            cache_path.touch()
        return TarballResult(
            tarball_b64=base64.b64encode(content).decode(),
            version_hash=version_hash,
            size_bytes=len(content),
        )

    content = _build_bytes(mcp_dir, _data_dir_names(manifest))
    try:
        # Atomic via .partial → rename to avoid half-written cache entries
        # visible to concurrent readers.
        import os
        partial = cache_path.with_suffix(cache_path.suffix + ".partial")
        partial.write_bytes(content)
        os.replace(partial, cache_path)
    except OSError as e:
        logger.warning("tarball cache write failed: %s", e)
    return TarballResult(
        tarball_b64=base64.b64encode(content).decode(),
        version_hash=version_hash,
        size_bytes=len(content),
    )


def invalidate(mcp_name: str) -> None:
    """Drop any cached tarballs for an MCP (after install/update/delete).

    We key by version_hash, not mcp_name, so we can't target a specific
    cache entry. Instead, recompute the current hash from disk and drop
    any entry that doesn't match. Safer to GC aggressively: just drop
    entries whose hash doesn't match any currently-registered MCP.
    """
    from services.mcp import mcp_registry
    try:
        valid_hashes = set()
        for name, m in mcp_registry.get_all_manifests().items():
            valid_hashes.add(mcp_installer.compute_version_hash(m.mcp_dir))
        fmt_suffix = f".{_TARBALL_FORMAT}"
        for entry in _cache_dir().iterdir():
            if not (entry.is_file() and entry.name.endswith(".tar.gz")):
                continue
            base = entry.name[: -len(".tar.gz")]
            if base.endswith(fmt_suffix):
                keep = base[: -len(fmt_suffix)] in valid_hashes
            else:
                # Pre-format (or unknown-format) entry — never served again
                # (the cache path always carries the current format tag), so
                # always drop it.
                keep = False
            if not keep:
                with contextlib.suppress(OSError):
                    entry.unlink()
    except Exception:
        logger.exception("tarball cache invalidate failed")


def gc() -> int:
    """Evict stale and over-quota tarballs. Returns bytes freed."""
    freed = 0
    now = time.time()
    entries: list[tuple[float, Path, int]] = []
    for entry in _cache_dir().iterdir():
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        # Age-based: drop anything not touched for STALE_CACHE_AGE_S.
        if now - st.st_mtime > STALE_CACHE_AGE_S:
            try:
                freed += st.st_size
                entry.unlink()
                continue
            except OSError:
                pass
        entries.append((st.st_mtime, entry, st.st_size))

    # Quota-based: oldest first until under MAX_CACHE_BYTES.
    total = sum(e[2] for e in entries)
    if total > MAX_CACHE_BYTES:
        entries.sort(key=lambda x: x[0])  # oldest first
        for mtime, path, size in entries:
            if total <= MAX_CACHE_BYTES:
                break
            try:
                path.unlink()
                freed += size
                total -= size
            except OSError:
                pass
    return freed
