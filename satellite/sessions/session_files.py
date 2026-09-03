"""Per-session secret FILES from the platform's session-file broker.

A start_session payload may carry a ``session_files_token`` — a one-shot
capability token for the proxy's ``POST /v1/hooks/session-files`` endpoint
(reached over the local tunnel, like the stdio interceptor's credential
fetch). Two kinds of relpath keys arrive in the files map:

- plain relpaths (``ssh/<key_name>`` — SSH private keys for the ssh-hosts
  MCP, admin-paired machines only): materialized under
  ``<otodock_dir>/session-secrets/<session_id>/`` and exposed via the
  payload's ``session_files_env`` map (``OTO_SSH_KEY_DIR``).
- sandbox-virtual absolute paths (``/users/{u}/.credentials/google-tokens/
  a@gmail.com.json`` — OAuth token files for credentials_dir MCPs):
  path-translated and written INSIDE the agent tree, exactly where the MCP
  subprocess reads them (its env carries the same virtual dir).
  ``.credentials`` is not part of the persistent file sync — this
  per-session delivery is the only way tokens reach a satellite disk, and
  every written file is recorded in a per-session manifest so wipe() can
  remove it at session close (refcounted against concurrent sessions that
  share the same token-file target).

Everything is written 0600 under 0700 dirs. A satellite restart wipes the
entire ``session-secrets/`` tree and sweeps ``.credentials/`` dirs out of
the agent tree — its sessions died with it, so nothing in there is live.

The token never enters the spawned CLI/agent env, so agent bash cannot
replay it; the fetch happens exactly once, before the CLI spawns.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from ..config import otodock_dir
from ..host import path_translator
import contextlib

logger = logging.getLogger("satellite.session_files")

_FETCH_TIMEOUT_S = 20

# Per-session record of the agent-tree files materialize() wrote (absolute
# paths, JSON list). Lives inside the session-secrets dir so it dies with
# the session; wipe() consults the OTHER sessions' manifests before
# deleting a shared target.
_AGENT_TREE_MANIFEST = "agent-tree-files.json"


def _secrets_root() -> Path:
    return otodock_dir() / "session-secrets"


def _session_dir(session_id: str) -> Path:
    return _secrets_root() / session_id


def _fetch(proxy_url: str, token: str) -> dict[str, dict] | None:
    """POST the capability token to the session-files endpoint → files map."""
    req = urllib.request.Request(
        f"{proxy_url.rstrip('/')}/v1/hooks/session-files",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("files") or {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("session-files fetch failed: %s", e)
        return None


def materialize(
    config: dict, session_id: str, agent_dir: Path | None = None,
) -> dict[str, str]:
    """Fetch + write this session's secret files; return env vars to inject.

    ``config`` is the start_session payload; ``agent_dir`` the session's
    agent root (required for virtual-path entries — without it they are
    skipped). Returns ``{}`` when the payload carries no token, the fetch
    fails, or nothing lands — the session then starts without the files
    (the agent sees file-not-found on use, never a hang). Plain relpaths
    are constrained to the session-secrets dir; virtual paths must
    translate to a location inside ``agent_dir`` (traversal-safe both
    ways) and are recorded in the per-session manifest for wipe().
    """
    token = config.get("session_files_token") or ""
    if not token:
        return {}
    proxy_url = (config.get("env") or {}).get("PROXY_URL", "")
    if not proxy_url:
        logger.warning("session-files: payload has token but no PROXY_URL")
        return {}

    files = _fetch(proxy_url, token)
    if not files:
        return {}

    username = path_translator.derive_username_from_cwd_relative(
        config.get("cwd_relative", ""),
    )
    base = _session_dir(session_id)
    wipe(session_id)  # never merge onto a stale tree
    written = 0
    agent_tree_files: list[str] = []
    for relpath, entry in files.items():
        if relpath.startswith("/"):
            # Sandbox-virtual target (OAuth credentials_dir token file):
            # translate into the agent tree. An unknown prefix passes
            # through translate_path unchanged and then fails the
            # inside-agent-dir check — refused, like traversal.
            if agent_dir is None:
                logger.warning(
                    "session-files: no agent_dir, skipping %r", relpath,
                )
                continue
            dest = Path(path_translator.translate_path(
                relpath, agent_dir, username,
            )).resolve()
            root = agent_dir.resolve()
        else:
            dest = (base / relpath).resolve()
            root = base.resolve()
        if not str(dest).startswith(str(root) + os.sep):
            logger.warning("session-files: refusing path %r", relpath)
            continue
        try:
            content = base64.b64decode(entry.get("content_b64", ""))
        except (ValueError, TypeError):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _chmod_quiet(dest.parent, 0o700)
        if relpath.startswith("/"):
            # Drop stale sibling token files a crashed pre-wipe session left
            # behind — single-user-mode MCPs pick the first file in the dir,
            # so it must contain only this session's bound account's token.
            # (Mirrors the proxy-side resolver's local per-session copy.)
            for existing in dest.parent.glob("*.json"):
                if existing.name != dest.name:
                    with contextlib.suppress(OSError):
                        existing.unlink()
            agent_tree_files.append(str(dest))
        dest.write_bytes(content)
        _chmod_quiet(dest, int(entry.get("mode") or 0o600))
        written += 1
    if not written:
        wipe(session_id)
        return {}
    base.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(base, 0o700)
    if agent_tree_files:
        manifest = base / _AGENT_TREE_MANIFEST
        manifest.write_text(json.dumps(agent_tree_files))
        _chmod_quiet(manifest, 0o600)

    env: dict[str, str] = {}
    for var, subdir in (config.get("session_files_env") or {}).items():
        env[var] = str(base / subdir)
    logger.info(
        "session-files: materialized %d file(s) for %s", written, session_id[:8],
    )
    return env


def wipe(session_id: str) -> None:
    """Remove a session's secret files (session close / pre-materialize).

    Agent-tree token files from the session's manifest are removed too —
    unless another live session's manifest still references the same file
    (concurrent sessions of the same agent+user share the token target) —
    and their now-empty ``.credentials`` parent dirs are pruned.
    """
    base = _session_dir(session_id)
    for path in _agent_tree_files(base):
        if _referenced_elsewhere(path, session_id):
            continue
        with contextlib.suppress(OSError):
            os.unlink(path)
        _prune_credentials_dirs(Path(path).parent)
    shutil.rmtree(base, ignore_errors=True)


def wipe_all() -> None:
    """Startup sweep: a satellite restart killed every session it was
    running, so the whole tree is dead secret material — remove it.
    (Agent-tree token files are handled by ``purge_agent_tree_credentials``,
    which the startup path calls right after this.)"""
    shutil.rmtree(_secrets_root(), ignore_errors=True)


def purge_agent_tree_credentials(agents_dir: Path | None) -> None:
    """Startup sweep: remove every ``.credentials/`` dir from the agent tree.

    Token files are per-session transients written by materialize(); after
    a restart none of them belong to a live session. This also clears the
    persistent copies older satellites accumulated back when
    ``.credentials`` was part of the file-sync manifest (one-time cleanup,
    free every boot thereafter)."""
    if not agents_dir or not agents_dir.is_dir():
        return
    removed = 0
    for pattern in ("*/knowledge/.credentials", "*/users/*/.credentials"):
        for d in agents_dir.glob(pattern):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        logger.info(
            "session-files: purged %d agent-tree .credentials dir(s)", removed,
        )


def _agent_tree_files(session_dir: Path) -> list[str]:
    """The absolute agent-tree paths this session's materialize() wrote."""
    try:
        paths = json.loads((session_dir / _AGENT_TREE_MANIFEST).read_text())
        return [p for p in paths if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def _referenced_elsewhere(path: str, session_id: str) -> bool:
    """True when another session's manifest also claims ``path``."""
    try:
        siblings = [
            d for d in _secrets_root().iterdir()
            if d.is_dir() and d.name != session_id
        ]
    except OSError:
        return False
    return any(path in _agent_tree_files(d) for d in siblings)


def _prune_credentials_dirs(start: Path) -> None:
    """Remove now-empty dirs upward from ``start``, but only within a
    ``.credentials`` subtree — never touches the surrounding agent tree."""
    d = start
    while ".credentials" in d.parts:
        try:
            d.rmdir()  # OSError unless empty — that's the stop condition
        except OSError:
            return
        d = d.parent


def _chmod_quiet(path: Path, mode: int) -> None:
    """chmod that tolerates platforms where it's a no-op (Windows ACLs)."""
    with contextlib.suppress(OSError):
        os.chmod(path, mode)
