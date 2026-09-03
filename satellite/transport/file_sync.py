"""File synchronization utilities for satellite daemon.

Provides file manifest computation, change detection, and file push/pull
for syncing agent directories between the platform and satellite.
"""

import base64
import fnmatch
import hashlib
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

from ..config import atomic_replace

logger = logging.getLogger("satellite")

# Directory names skipped during sync. Aligned with proxy/core/remote/file_sync.py
# (both sides must agree or the diff churns). Real Python venvs are skipped
# precisely via _is_venv_dir (pyvenv.cfg), NOT a blanket venv/bin/Scripts
# skip — so an agent workspace dir merely named `venv`/`bin` still syncs while
# an actual venv doesn't. Unreadable files (e.g. a Windows-locked .exe in a
# stray Scripts dir) are tolerated by the OSError catch in the walk below.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__",
    ".venv", ".cache", ".mypy_cache", ".pytest_cache",
}

# LEGACY per-file cap — the fallback when the proxy never sent a cap in the
# auth_result policy handshake (pre-1.4.0 proxies). A NEW satellite against an
# OLD proxy must stay at 100MB: its manifest would otherwise advertise files
# the old proxy's pull leg hard-rejects → permanent sync churn. Effective
# enforcement reads go through ``max_file_size()`` below. Kept as a plain
# module attribute (test monkeypatch seam).
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


def max_file_size() -> int:
    """Effective per-file sync cap: the proxy's handshake value
    (``auth_result`` → ``policy.sync_max_file_bytes``, cached in
    ``satellite_policy``), else the legacy ``MAX_FILE_SIZE``. All post-auth
    enforcement sites (manifest / fingerprint / inbound writes / pull source)
    read this so proxy config is the single source of truth."""
    from ..host.satellite_policy import get_policy
    try:
        v = int(get_policy().get("sync_max_file_bytes") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else MAX_FILE_SIZE


# Max file size for inline base64 encoding (1MB). Larger files use chunked transfer.
CHUNK_THRESHOLD = 1_048_576
CHUNK_SIZE = 524_288  # 512KB

# Codex app-server keeps per-machine SQLite *runtime state* directly in
# CODEX_HOME — versioned DB families (``state_N``, ``logs_N``, ``goals_N``,
# ``memories_N``, …) plus ``-wal``/``-shm`` sidecars. Each host owns its own; a
# foreign one aborts daemon init with "migration N … has been modified"
# (``_reset_codex_state`` self-heals it, but only if its globs cover the
# poisoned file). New DB families appear with codex releases, so match EVERY
# SQLite file directly under ``.codex/`` — nothing legitimate to sync is ever
# SQLite there. These NEVER sync, in either direction — excluded from both the
# per-turn snapshot AND the request-manifest. Glob matches
# ``_reset_codex_state``'s own glob. Mirrors proxy/core/remote/file_sync.py.
_CODEX_RUNTIME_GLOBS = ("*.sqlite*",)

# Per-session REGENERATED Codex config — this satellite writes its own copies
# at session start from the ``start_session`` payload (rewritten paths,
# sentinel bearer tokens, broker fetch tokens, ``auth_json``). The platform's
# local-session copies carry REAL secrets and platform paths, so they must
# never be synced here, and ours must never flow back. Mirrors
# proxy/core/remote/file_sync.py — the two sets MUST stay identical.
_CODEX_HOST_LOCAL_FILES = frozenset({
    "config.toml", "AGENTS.md", "hooks.json", "auth.json",
})


def _is_codex_runtime_state(rel_path: str) -> bool:
    """True for host-local ``.codex`` files that must never sync in either
    direction: the app-server's per-machine SQLite runtime plus the
    per-session regenerated config files (``_CODEX_HOST_LOCAL_FILES``)."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[-2] != ".codex":
        return False
    if parts[-1] in _CODEX_HOST_LOCAL_FILES:
        return True
    return any(fnmatch.fnmatch(parts[-1], g) for g in _CODEX_RUNTIME_GLOBS)


# Per-session REGENERATED Claude Code config — this satellite writes its own
# settings.json + hook scripts at session start (``CLISession.start`` /
# ``_write_cli_hooks``), and the platform's local-session copies carry
# sandbox-internal hook paths invalid here. Host-local, never synced either
# direction. Mirrors proxy/core/remote/file_sync.py — the two sets MUST stay identical.
# (``projects/`` — the session ``.jsonl`` — is satellite-authoritative and STAYS
# synced.)
_CLAUDE_HOST_LOCAL_FILES = frozenset({
    "settings.json", "permission_gate.py", "tool_result_forwarder.py",
    "subagent_tracker.py", "stop_tracker.py", "stdio_path_interceptor.py",
    "mcp-config.json", "system-prompt.md",
    # Claude CLI OAuth credential file — written here from the start_session
    # ``credentials_json`` payload and rewritten by ``credentials_update``
    # pushes on rotation; never synced (would race the dedicated push).
    ".credentials.json",
    "auth.json",
})


def _is_claude_runtime_state(rel_path: str) -> bool:
    """True for host-local ``.claude`` config files that must never sync in
    either direction (``_CLAUDE_HOST_LOCAL_FILES``). Matches only DIRECT children
    of ``.claude/`` — so ``.claude/projects/<hash>/<sid>.jsonl`` is unaffected."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[-2] != ".claude":
        return False
    return parts[-1] in _CLAUDE_HOST_LOCAL_FILES


def _is_venv_dir(path: Path) -> bool:
    """True if ``path`` is a real Python venv root (contains pyvenv.cfg).
    Lets the manifest skip actual venvs precisely while still syncing a
    workspace dir merely named ``venv``/``bin``/``Scripts``."""
    try:
        return (path / "pyvenv.cfg").is_file()
    except (OSError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Sync-ignore rules: marker-confirmed generated-dir exclusion (1.5).
#
# The proxy ships a declarative rule table in the auth_result policy
# handshake (``policy.sync_ignore_rules``; proxies >= 1.5, satellites >=
# 0.5.110). A dir is excluded from every sync surface iff a rule MATCHES
# and no VETO applies:
#   * in-dir marker — the dir's own listing contains a marker entry
#     (``CACHEDIR.TAG`` only with the spec signature; ``CMakeCache.txt``;
#     ``project.assets.json``; dir marker ``meson-info``);
#   * named rule — dir name equals ``rule.dir`` exactly AND a sibling
#     manifest (e.g. ``Cargo.toml`` next to ``target/``) appears in the
#     PARENT's listing;
#   * veto — the dir's OWN top-level listing contains a source manifest
#     (``CMakeLists.txt``, ``package.json``, …) → NEVER excluded. Catches
#     in-tree builds (``cmake .`` drops CMakeCache.txt into the source
#     root). Veto evidence must be files that THEMSELVES SYNC — never
#     ``.git`` (SKIP_DIRS): the platform's copy lacks it, and both walks
#     MUST reach identical decisions or the diff churns.
#
# ALL evidence is evaluated from walk LISTINGS by exact string compare /
# ``fnmatchcase`` — never filesystem probes or case-folding ``fnmatch``:
# NTFS is case-insensitive, so a probe here would match ``cachedir.tag``
# while the Linux proxy's walk misses it → asymmetric manifests → mass
# delete-misattribution.
#
# Rules never fire on the walk root, its depth-1 children, or any dir
# named workspace/config/users (a stray CACHEDIR.TAG written into
# ``workspace/`` must not desync the whole agent).
#
# MUST stay semantically identical to proxy/core/remote/file_sync.py.
# Table evolution contract: ``version`` stays 1 permanently for proxy-only
# ADDITIVE data edits; any format/semantic change requires a new version
# AND a new SATELLITE_VERSION capability gate proxy-side (this satellite
# rejects ``version != 1`` → legacy full-sync, and the proxy must then
# also compute that machine's manifest legacy-style or the diff churns).
# ---------------------------------------------------------------------------

_CACHEDIR_SIG = b"Signature: 8a477f597d28d172789f06886806bc55"
_RULE_PROTECTED_DIR_NAMES = frozenset({"workspace", "config", "users"})
# Validation caps — a compromised/buggy proxy must not be able to ship a
# table that turns every per-turn walk into an O(#rules) CPU sink.
_RULES_MAX_LIST = 64
_RULES_MAX_MARKERS = 16
_RULES_MAX_STR = 128
_RULES_MAX_SERIALIZED = 32 * 1024


def validate_ignore_rules(raw) -> dict | None:
    """Shape-validate a handshake rule table → normalized copy, or None
    (caller unsets the key → legacy behavior). Never raises. Refuses:
    unknown version, oversized tables (caps above), path separators in any
    name, and named rules WITHOUT sibling evidence (a bare-name rule is
    exactly the false-positive class this design exists to avoid)."""
    try:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return None
        import json as _json
        if len(_json.dumps(raw)) > _RULES_MAX_SERIALIZED:
            return None

        def _name_ok(s) -> bool:
            return (isinstance(s, str) and 0 < len(s) <= _RULES_MAX_STR
                    and "/" not in s and "\\" not in s and s not in (".", ".."))

        def _strs(v) -> list[str]:
            if v is None:
                return []
            if not isinstance(v, list) or len(v) > _RULES_MAX_LIST:
                raise ValueError
            for s in v:
                if not _name_ok(s):
                    raise ValueError
            return list(v)

        markers_raw = raw.get("in_dir_markers") or []
        if not isinstance(markers_raw, list) or len(markers_raw) > _RULES_MAX_MARKERS:
            return None
        markers: list[dict] = []
        for m in markers_raw:
            if not isinstance(m, dict):
                return None
            kind = "file" if "file" in m else ("dir" if "dir" in m else None)
            if kind is None or not _name_ok(m.get(kind)):
                return None
            entry = {kind: m[kind]}
            if m.get("signed"):
                entry["signed"] = True
            markers.append(entry)

        named_raw = raw.get("named") or []
        if not isinstance(named_raw, list) or len(named_raw) > _RULES_MAX_LIST:
            return None
        named: list[dict] = []
        for n in named_raw:
            if not isinstance(n, dict) or not _name_ok(n.get("dir")):
                return None
            siblings = _strs(n.get("siblings"))
            sibling_globs = _strs(n.get("sibling_globs"))
            if not siblings and not sibling_globs:
                return None
            named.append({"dir": n["dir"], "siblings": siblings,
                          "sibling_globs": sibling_globs})

        return {
            "version": 1,
            "in_dir_markers": markers,
            "named": named,
            "veto_files": _strs(raw.get("veto_files")),
            "veto_globs": _strs(raw.get("veto_globs")),
        }
    except (ValueError, TypeError):
        return None


class _IgnoreMatcher:
    """Compiled rule table (see the block comment above). Immutable after
    construction — safe to share across walk threads."""

    __slots__ = ("file_markers", "signed_markers", "dir_markers", "named",
                 "veto_files", "veto_globs")

    def __init__(self, rules: dict):
        self.file_markers: frozenset[str]
        self.signed_markers: frozenset[str]
        file_m, signed_m, dir_m = set(), set(), set()
        for m in rules.get("in_dir_markers") or []:
            if "file" in m:
                (signed_m if m.get("signed") else file_m).add(m["file"])
            else:
                dir_m.add(m["dir"])
        self.file_markers = frozenset(file_m)
        self.signed_markers = frozenset(signed_m)
        self.dir_markers = frozenset(dir_m)
        named: dict[str, list] = {}
        for n in rules.get("named") or []:
            named.setdefault(n["dir"], []).append(
                (frozenset(n.get("siblings") or ()),
                 tuple(n.get("sibling_globs") or ())))
        self.named = named
        self.veto_files = frozenset(rules.get("veto_files") or ())
        self.veto_globs = tuple(rules.get("veto_globs") or ())

    def named_match(self, dir_name: str, parent_files: list[str]) -> bool:
        """True if ``dir_name`` matches a named rule with sibling evidence
        present in the parent's file listing (exact / fnmatchcase)."""
        rule_list = self.named.get(dir_name)
        if not rule_list:
            return False
        for siblings, globs in rule_list:
            for f in parent_files:
                if f in siblings or any(
                        fnmatch.fnmatchcase(f, g) for g in globs):
                    return True
        return False

    def has_in_dir_marker(self, dir_path: Path, files: list[str],
                          dirs: list[str]) -> bool:
        """True if the dir's own listing carries an in-dir marker. Signed
        markers (CACHEDIR.TAG) additionally require the spec signature —
        read errors fail toward syncing."""
        for f in files:
            if f in self.file_markers:
                return True
            if f in self.signed_markers:
                try:
                    with open(dir_path / f, "rb") as fh:
                        if fh.read(len(_CACHEDIR_SIG)) == _CACHEDIR_SIG:
                            return True
                except OSError:
                    pass
        return any(d in self.dir_markers for d in dirs)

    def is_vetoed(self, files: list[str]) -> bool:
        """True if the dir's own top-level listing contains source-manifest
        evidence — the dir is then NEVER excluded."""
        for f in files:
            if f in self.veto_files or any(
                    fnmatch.fnmatchcase(f, g) for g in self.veto_globs):
                return True
        return False


# Compiled-matcher cache: keyed by the canonical JSON of the policy table,
# swapped atomically — walks run in ``asyncio.to_thread`` workers while
# ``policy_update`` lands on the WS loop. Single entry (one proxy → one
# active table).
_matcher_lock = threading.Lock()
_matcher_cache: tuple[str, _IgnoreMatcher] | None = None
# Bounded once-per-dir logging of rule skips so marker-based data-hiding
# stays auditable without per-turn log spam.
_logged_rule_skips: set[str] = set()
_LOGGED_RULE_SKIPS_MAX = 500


def _effective_ignore_matcher() -> "_IgnoreMatcher | None":
    """Compiled matcher for the policy's current rule table, or None →
    legacy behavior (no rule exclusions). The table was already
    shape-validated at ``set_policy`` time."""
    global _matcher_cache
    from ..host.satellite_policy import get_policy
    rules = get_policy().get("sync_ignore_rules")
    if not rules:
        return None
    import json as _json
    try:
        key = _json.dumps(rules, sort_keys=True)
    except (TypeError, ValueError):
        return None
    with _matcher_lock:
        cached = _matcher_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        matcher = _IgnoreMatcher(rules)
    except (KeyError, TypeError):
        return None
    with _matcher_lock:
        _matcher_cache = (key, matcher)
    return matcher


def _walk_sync_tree(agent_dir: Path):
    """The ONE dir-walk for every sync surface (snapshot / fingerprint /
    request-manifest): yields ``(root_path, files)`` for each INCLUDED dir
    after all dir-level exclusions — SKIP_DIRS, real venvs, hidden dirs,
    CLI runtime children, and the sync-ignore rules. Per-FILE filters
    (symlinks, runtime state, cruft, ``.partial``, size cap) stay with the
    callers. Consumers MUST NOT hand-roll walks: the shared walker is what
    keeps the three satellite surfaces (and the proxy mirror) agreeing.

    Rule mechanics (two-step, zero extra IO beyond the CACHEDIR.TAG
    signature read): named matches are flagged as CANDIDATES at the
    parent's visit (sibling evidence = parent's ``files``); the exclusion
    decision happens at the dir's OWN visit, where its listing provides
    marker + veto evidence. Excluded → ``dirs[:] = []`` and nothing
    yielded, so the subtree below its top level is never even scanned."""
    matcher = _effective_ignore_matcher()
    candidates: set[str] = set()
    for root, dirs, files in os.walk(agent_dir):
        root_path = Path(root)
        rel = root_path.relative_to(agent_dir)
        rel_posix = rel.as_posix()
        if matcher is not None:
            was_candidate = rel_posix in candidates
            candidates.discard(rel_posix)
            if (len(rel.parts) >= 2
                    and root_path.name not in _RULE_PROTECTED_DIR_NAMES
                    and (was_candidate
                         or matcher.has_in_dir_marker(root_path, files, dirs))
                    and not matcher.is_vetoed(files)):
                if (rel_posix not in _logged_rule_skips
                        and len(_logged_rule_skips) < _LOGGED_RULE_SKIPS_MAX):
                    _logged_rule_skips.add(rel_posix)
                    logger.info(
                        "sync-ignore: excluding generated dir %s", rel_posix)
                dirs[:] = []
                continue
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS and not _is_venv_dir(root_path / d)
            and (not d.startswith(".") or d in INCLUDE_DOTTED_DIRS)
            and not _is_cli_runtime_child(root_path.name, d)
        ]
        if matcher is not None:
            for d in dirs:
                if matcher.named_match(d, files):
                    candidates.add((rel / d).as_posix())
        yield root_path, files


# Hidden dirs that ARE included despite starting with ``.`` — mirror of
# ``proxy/core/remote/file_sync.py::INCLUDE_DOTTED_DIRS``. Every OTHER hidden dir is
# pruned (so ``.codex/.tmp``, ``.codex/skills/.system``, a workspace ``.github``,
# etc. don't sync) — keeping the satellite manifest identical to the proxy's so
# the diff never churns on an asymmetric hidden dir. ``.credentials`` is
# deliberately absent: OAuth token files are per-session transients delivered
# over the session-file broker (see sessions/session_files.py), never synced.
INCLUDE_DOTTED_DIRS = {".claude", ".codex"}

# Runtime-cruft dirs that are immediate children of ``.claude``/``.codex`` — host-
# local session transcripts (``projects``/``sessions``), temp, caches, shell
# snapshots, backups — pruned from the manifest/snapshot so they never sync.
# MUST stay identical to ``proxy/core/remote/file_sync.py::_CLI_RUNTIME_CHILD_DIRS`` or
# the two manifests disagree and the diff churns. Real config + ``skills/`` sync.
_CLI_RUNTIME_CHILD_DIRS = frozenset({
    "projects", "sessions",
    # Claude Code task store — per-machine runtime state keyed by the
    # session id ``--resume`` keeps; syncing it let the platform-authoritative
    # scrub wipe the task list on every re-warm.
    "tasks",
    ".tmp", "tmp", "cache",
    "shell-snapshots", "shell_snapshots",
    "backups", "debug", "telemetry", "session-env", "plans",
    # Claude Code plugins dir (marketplaces clones = refetchable cache;
    # installs are per-machine) — pushing the tree flooded a satellite off
    # the socket mid-sync (2026-08-06 incident).
    "plugins",
    # Per-session ssh keys — broker-delivered + purged at close; never synced.
    "ssh",
})


def _is_cli_runtime_child(parent_name: str, child_name: str) -> bool:
    """True for a runtime-cruft dir that is an immediate child of ``.claude``/``.codex``."""
    return parent_name in (".claude", ".codex") and child_name in _CLI_RUNTIME_CHILD_DIRS


def _is_cli_runtime_cruft_file(parent_name: str, file_name: str) -> bool:
    """True for CLI backup/corrupted state files directly under ``.claude``/``.codex``."""
    if parent_name not in (".claude", ".codex"):
        return False
    return ".backup." in file_name or ".corrupted." in file_name


def _hash_file(path: Path) -> str:
    """Stream-hash a file → ``sha256:<hex>`` without loading it fully in memory.

    Used to verify a chunk-assembled ``.partial`` against the platform's
    full-file hash before the atomic commit — parity with the proxy pull path's
    incremental hashing (``_commit_pull``)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


# (path → (size, mtime_ns, hash)) LRU so the per-turn snapshot and the
# request-manifest never re-hash unchanged files — at the 1GB cap a single
# re-hash is seconds of CPU+IO, and snapshots run every turn. Thread-safe
# (snapshot/manifest calls can run off-thread). Mirrors the proxy-side cache
# in proxy/core/remote/file_sync.py.
_HASH_CACHE: OrderedDict[str, tuple[int, int, str]] = OrderedDict()
_HASH_CACHE_MAX = 50_000
_hash_cache_lock = threading.Lock()
# Git's "racily clean" rule: filesystem mtime granularity is the kernel tick
# (can be milliseconds), so two same-size writes inside one tick are
# indistinguishable by (size, mtime_ns). Never TRUST a cache hit for a file
# modified within this window — re-hash it; entries older than the window are
# provably keyed by a settled mtime.
_HASH_CACHE_RACY_WINDOW_S = 2.0


def _hash_file_cached(path: Path, st: os.stat_result) -> str:
    """``_hash_file`` with a (size, mtime_ns) validity check against the LRU."""
    import time as _time
    key = str(path)
    racy = (_time.time() - st.st_mtime) < _HASH_CACHE_RACY_WINDOW_S
    if not racy:
        with _hash_cache_lock:
            hit = _HASH_CACHE.get(key)
            if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                _HASH_CACHE.move_to_end(key)
                return hit[2]
    file_hash = _hash_file(path)
    try:
        st2 = os.stat(path)
    except OSError:
        return file_hash
    # TOCTOU guard: only cache when the file provably didn't change under the
    # streaming read. (Caching a racy-fresh entry is fine — reads inside the
    # racy window bypass the cache anyway.)
    if st2.st_size == st.st_size and st2.st_mtime_ns == st.st_mtime_ns:
        with _hash_cache_lock:
            _HASH_CACHE[key] = (st.st_size, st.st_mtime_ns, file_hash)
            _HASH_CACHE.move_to_end(key)
            while len(_HASH_CACHE) > _HASH_CACHE_MAX:
                _HASH_CACHE.popitem(last=False)
    return file_hash


def prime_hash_cache(path: Path, file_hash: str) -> None:
    """Record a KNOWN-good hash for a file just written (e.g. a committed
    platform push whose sha256 was verified) so the next snapshot never
    re-hashes it. Best-effort: a failed stat simply skips the prime."""
    try:
        st = os.stat(path)
    except OSError:
        return
    with _hash_cache_lock:
        _HASH_CACHE[str(path)] = (st.st_size, st.st_mtime_ns, file_hash)
        _HASH_CACHE.move_to_end(str(path))
        while len(_HASH_CACHE) > _HASH_CACHE_MAX:
            _HASH_CACHE.popitem(last=False)


def snapshot_agent_dir(agent_dir: Path) -> dict[str, str]:
    """Compute sha256 hashes for all files in agent dir.

    Returns {relative_path: "sha256:<hex>"}.
    """
    result = {}
    if not agent_dir.is_dir():
        return result

    # Dir-level pruning (skip-dirs, venvs, hidden, CLI runtime, sync-ignore
    # rules) lives in the shared walker — see _walk_sync_tree.
    for root_path, files in _walk_sync_tree(agent_dir):
        for fname in files:
            f = root_path / fname
            # Skip symlinks — they can't round-trip via base64 content and would
            # silently become regular files on the platform. Must match the
            # proxy's compute_manifest (proxy/core/remote/file_sync.py), which skips them
            # too; otherwise a symlink is satellite-only and the merge materializes
            # its target as a regular file on the platform.
            if f.is_symlink():
                continue
            # Use as_posix() so dict keys are forward-slash on both platforms
            # (proxy-side manifests use forward slashes too). Without this,
            # Windows backslash keys cause every file to look "new" relative
            # to the proxy snapshot.
            rel = f.relative_to(agent_dir).as_posix()
            # Codex per-machine SQLite runtime never syncs (see helper above) —
            # exclude so the per-turn detect_changes doesn't echo it to the platform.
            if _is_codex_runtime_state(rel):
                continue
            if _is_claude_runtime_state(rel):
                continue
            # CLI backup/corrupted state files (.claude.json.backup.* etc.) — cruft.
            if _is_cli_runtime_cruft_file(root_path.name, fname):
                continue
            # Transient staging files from an in-flight / aborted chunked push or
            # pull — never synced (else adopted as a satellite-only file and
            # propagated). Excluded symmetrically in proxy/core/remote/file_sync.py.
            if fname.endswith(".partial"):
                continue
            try:
                st = f.stat()
                cap = max_file_size()
                if st.st_size > cap:
                    logger.warning(
                        "Skipping oversized file (>%dMB) from manifest: %s",
                        cap // (1024 * 1024), f,
                    )
                    continue
                # Streamed + cached — never reads an unchanged file, never
                # holds a whole file in memory.
                result[rel] = _hash_file_cached(f, st)
            except (OSError, PermissionError):
                continue

    return result


def compute_fingerprint(agent_dir: Path) -> str:
    """A cheap STAT-ONLY fingerprint of an agent dir for the proxy's idle
    change-detection.

    A sha256 over the sorted ``(relpath, size, mtime_ns)`` of every sync-relevant
    file — the SAME exclusions as ``snapshot_agent_dir`` / ``compute_manifest`` (so
    it flips only when a file the MERGE would sync changes), but with NO content
    read. It is **opaque** to the proxy: the proxy only compares it to the LAST
    value from the SAME satellite ("did anything change since we last synced?"), so
    the satellite-local mtime clock never matters. Empty/missing dir → ``""``.
    Add / modify / delete / rename all flip it; the one miss is a content edit that
    preserves BOTH size and mtime — acceptable for a backstop (the authoritative
    merge, once triggered, uses content hashes).

    NOTE: the dir-prune + file-exclude logic below MUST stay identical to
    ``snapshot_agent_dir`` (and ``compute_manifest``) — a drift only degrades the
    backstop's trigger quality (spurious or missed sweeps), never correctness, since
    the merge re-validates with real hashes.
    """
    if not agent_dir.is_dir():
        return ""
    entries: list[tuple[str, int, int]] = []
    for root_path, files in _walk_sync_tree(agent_dir):
        for fname in files:
            f = root_path / fname
            if f.is_symlink():
                continue
            rel = f.relative_to(agent_dir).as_posix()
            if _is_codex_runtime_state(rel):
                continue
            if _is_claude_runtime_state(rel):
                continue
            if _is_cli_runtime_cruft_file(root_path.name, fname):
                continue
            if fname.endswith(".partial"):
                continue
            try:
                st = f.stat()
            except (OSError, PermissionError):
                continue
            if st.st_size > max_file_size():
                continue
            entries.append((rel, st.st_size, st.st_mtime_ns))
    if not entries:
        return ""  # missing dir, or no sync-relevant files → nothing to fingerprint
    h = hashlib.sha256()
    for rel, size, mtime_ns in sorted(entries):
        h.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def detect_changes(
    agent_dir: Path, snapshot: dict[str, str],
) -> list[dict]:
    """Compare current state against snapshot, return list of changes,
    AND refresh the snapshot in place so the next call only sees
    *new* changes since this point.

    Each change is a dict with:
      - path: relative path within agent_dir
      - action: "write" | "delete" | "mkdir"
      - content_b64: base64-encoded content (for writes, files <= CHUNK_THRESHOLD)
      - hash: current sha256 hash (for writes)

    **Snapshot refresh is load-bearing.** Without it, the snapshot stays
    pinned at session-start state forever and every end-of-turn scan
    re-reports every file modified at any point during the session —
    turn 1's writes flood again at end of turn 2, plus turn 2's, plus
    turn 3's, etc. A long Codex/CLI session would accumulate enough
    "stale" file_changed events to overflow the satellite's send queue
    (10K cap) and force the platform to apply the same writes over and
    over. We mutate the caller's snapshot dict in place — by Python
    convention the session's ``_file_snapshot`` is a live reference, so
    the next ``detect_file_changes()`` call automatically sees the new
    baseline.
    """
    current = snapshot_agent_dir(agent_dir)
    changes: list[dict] = []

    # New or modified files
    for rel_path, cur_hash in current.items():
        old_hash = snapshot.get(rel_path)
        if old_hash == cur_hash:
            continue
        full = agent_dir / rel_path
        try:
            # Stat first — only files small enough for inline delivery are ever
            # read into memory. Large files report hash+size and the platform
            # pulls them separately (streamed on both ends).
            size = full.stat().st_size
            if size <= CHUNK_THRESHOLD:
                content = full.read_bytes()
                changes.append({
                    "path": rel_path,
                    "action": "write",
                    "content_b64": base64.b64encode(content).decode(),
                    "hash": cur_hash,
                })
            else:
                changes.append({
                    "path": rel_path,
                    "action": "write",
                    "hash": cur_hash,
                    "size": size,
                })
        except (OSError, PermissionError):
            continue

    # Deleted files. A path can vanish from ``current`` for TWO reasons:
    # actually deleted, or newly EXCLUDED (a sync-ignore rule table arriving
    # mid-session via auth_result — live sessions survive proxy restarts, so
    # their baseline predates the rules). Only a path that is GONE from disk
    # is a delete; a still-present file was excluded → purge it from the
    # baseline silently and emit NOTHING. Emitting deletes here would let a
    # proxy-side config change delete the platform copies AND record
    # tombstones that could later delete REAL satellite files if the rule is
    # ever retracted (tombstone mtimes would outrank the files'). One stat
    # per candidate delete — rare outside rule-change turns.
    for rel_path in snapshot:
        if rel_path not in current:
            try:
                if (agent_dir / rel_path).is_file():
                    continue  # excluded, not deleted — drop from baseline only
            except OSError:
                pass  # unstatable → treat as deleted (pre-guard behavior)
            changes.append({"path": rel_path, "action": "delete"})

    # Refresh the snapshot so the next call's baseline is *this* moment,
    # not session start. Mutated in place — the session object's
    # ``_file_snapshot`` attribute is the same dict reference. See the
    # docstring above for why this is critical.
    snapshot.clear()
    snapshot.update(current)

    return changes


def prune_empty_parents(file_path: Path, root: Path) -> None:
    """After a file delete, remove now-empty parent directories bottom-up.

    Sync has no rmdir action (manifests/snapshots are file-based), so a tree
    delete propagates per-file deletes and leaves the empty dir skeleton
    behind. Mirror of the platform-side helper: never touches the sync root,
    its depth-1 children (workspace/ users/ config/ …), or per-user roots
    (users/<name>); the first non-empty directory stops the walk."""
    try:
        root_r = Path(root).resolve()
        cur = Path(file_path).parent.resolve()
    except OSError:
        return
    while True:
        try:
            rel = cur.relative_to(root_r)
        except ValueError:
            return
        depth = len(rel.parts)
        if depth < 2 or (depth == 2 and rel.parts[0] == "users"):
            return
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def apply_file_push(
    agent_dir: Path,
    msg: dict,
    snapshot: dict[str, str] | None = None,
    snapshots: list[dict[str, str]] | None = None,
) -> None:
    """Write a file pushed from the platform.

    Validates that the path stays within agent_dir (no traversal). Writes
    are atomic: `write` uses a .partial staging file + fsync + rename so
    a concurrent reader never sees a half-written file. `write_chunk`
    appends to .partial and atomically renames on the final chunk (detected
    via a non-empty `hash` field, which the platform sends only on the last
    chunk — matches `prepare_outgoing_files` convention).

    If ``snapshot`` is given (the satellite session's ``_file_snapshot`` dict),
    update it post-write so the next ``detect_changes`` doesn't emit a phantom
    ``file_changed`` event with the same hash. Without this, every push_back
    (file-tools edit on platform → push to satellite) round-trips back as a
    no-op file_changed at end-of-turn.
    """
    import os

    rel_path = msg.get("path", "")
    action = msg.get("action", "write")

    if not rel_path:
        return

    target = (agent_dir / rel_path).resolve()
    # Security: ensure target is within agent_dir. Use relative_to (not a
    # string startswith) so a sibling like ``<agent_dir>-evil`` can't pass the
    # prefix check.
    try:
        target.relative_to(agent_dir.resolve())
    except ValueError:
        logger.warning(f"Rejected file push outside agent dir: {rel_path}")
        return

    partial = target.with_suffix(target.suffix + ".partial")
    new_hash: str | None = None

    if action == "delete":
        if target.exists():
            target.unlink()
            logger.debug(f"Deleted: {rel_path}")
        prune_empty_parents(target, agent_dir)
    elif action == "mkdir":
        target.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Created dir: {rel_path}")
    elif action == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        # Key-present gate, NOT truthiness: "" is a real ZERO-BYTE write
        # (every platform write sender includes the key). The old truthy gate
        # silently skipped empty files while the handler still acked ok —
        # poisoning the platform's converged base.
        if "content_b64" in msg:
            data = base64.b64decode(msg.get("content_b64") or "")
            cap = max_file_size()
            if len(data) > cap:
                raise ValueError(
                    f"pushed file exceeds MAX_FILE_SIZE "
                    f"({len(data)} > {cap}): {rel_path}"
                )
            # Integrity check: the platform sends the full-file sha256 on the
            # inline write. Reject a corrupt payload rather than commit it.
            expected_hash = msg.get("hash") or ""
            if expected_hash:
                actual = "sha256:" + hashlib.sha256(data).hexdigest()
                if actual != expected_hash:
                    raise ValueError(
                        f"hash mismatch on write: got {actual} "
                        f"expected {expected_hash}: {rel_path}"
                    )
            with open(partial, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(partial, target)
            logger.debug(f"Wrote: {rel_path} ({len(data)} bytes)")
            # Snapshot refresh must not depend on the sender attaching a hash —
            # a stale snapshot echoes the push back as a phantom file_changed
            # (PTY sessions scan too now). Compute it from the payload if absent.
            new_hash = expected_hash or (
                "sha256:" + hashlib.sha256(data).hexdigest()
            )
    elif action == "write_chunk":
        target.parent.mkdir(parents=True, exist_ok=True)
        content_b64 = msg.get("content_b64", "")
        chunk_index = int(msg.get("chunk_index", 0) or 0)
        if content_b64:
            # The FIRST chunk truncates; later chunks append. A stale .partial
            # from an aborted prior transfer must never be appended to — it would
            # prepend garbage and silently corrupt the file. Mirrors the
            # already-correct _apply_satellite_host_push path.
            mode = "wb" if chunk_index == 0 else "ab"
            with open(partial, mode) as f:
                f.write(base64.b64decode(content_b64))
                f.flush()
                os.fsync(f.fileno())
            if partial.stat().st_size > max_file_size():
                partial.unlink(missing_ok=True)
                raise ValueError(
                    f"pushed file exceeds MAX_FILE_SIZE: {rel_path}"
                )
        # Non-empty hash → final chunk → verify the accumulated bytes against the
        # full-file sha256 BEFORE committing (parity with the pull path's
        # _commit_pull). A dropped/duplicated/reordered chunk is caught here and
        # never atomically published as a corrupt file.
        expected_hash = msg.get("hash") or ""
        if expected_hash and partial.exists():
            actual = _hash_file(partial)
            if actual != expected_hash:
                partial.unlink(missing_ok=True)
                raise ValueError(
                    f"chunked hash mismatch: got {actual} "
                    f"expected {expected_hash}: {rel_path}"
                )
            atomic_replace(partial, target)
            new_hash = expected_hash

    # Refresh snapshot(s) so the next detect_changes scan sees this file as
    # already-known with the same hash (no spurious file_changed event back
    # to the platform). Only update on completed writes — partial chunks
    # leave the snapshot as-is.
    targets: list[dict[str, str]] = []
    if snapshot is not None:
        targets.append(snapshot)
    if snapshots:
        targets.extend(snapshots)

    if action == "delete":
        for snap in targets:
            snap.pop(rel_path, None)
    elif new_hash:
        for snap in targets:
            snap[rel_path] = new_hash
        # The committed bytes' sha256 was just verified — prime the hash cache
        # so the next snapshot pass never re-hashes a big pushed file.
        prime_hash_cache(target, new_hash)


def compute_manifest(agent_dir: Path) -> list[dict]:
    """Compute full file manifest for manifest request.

    Returns [{path, hash, size, mtime}].
    """
    if not agent_dir.is_dir():
        return []

    entries = []
    # Dir-level pruning is the shared walker's (mirror snapshot_agent_dir) —
    # the request-manifest path never reports venv internals, rule-excluded
    # build dirs, or oversized files the snapshot/detect path excludes.
    for root_path, files in _walk_sync_tree(agent_dir):
        for fname in files:
            f = root_path / fname
            # Skip symlinks to match the proxy's compute_manifest (see
            # snapshot_agent_dir above) — keeps both manifests symmetric so the
            # 3-way merge never materializes a symlink target as a regular file.
            if f.is_symlink():
                continue
            # Codex per-machine SQLite runtime never syncs (see helper above).
            if _is_codex_runtime_state(f.relative_to(agent_dir).as_posix()):
                continue
            if _is_claude_runtime_state(f.relative_to(agent_dir).as_posix()):
                continue
            # CLI backup/corrupted state files (.claude.json.backup.* etc.) — cruft.
            if _is_cli_runtime_cruft_file(root_path.name, fname):
                continue
            # Transient staging files from an in-flight / aborted chunked push or
            # pull — never synced (mirror of snapshot_agent_dir + the proxy).
            if fname.endswith(".partial"):
                continue
            try:
                stat = f.stat()
                cap = max_file_size()
                if stat.st_size > cap:
                    logger.warning(
                        "Skipping oversized file (>%dMB) from manifest: %s",
                        cap // (1024 * 1024), f,
                    )
                    continue
                # Streamed + cached (see _hash_file_cached) — memory-bounded
                # and no re-hash of unchanged files.
                entries.append({
                    "path": f.relative_to(agent_dir).as_posix(),
                    "hash": _hash_file_cached(f, stat),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
            except (OSError, PermissionError):
                continue

    return entries
