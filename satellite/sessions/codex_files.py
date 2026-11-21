"""The files of a Codex session's home that this machine writes itself.

The ``config.toml`` pieces every Codex session of this machine shares (the
headless app-server session, ``codex_session``, and the terminal,
``terminal/codex_pty_session``): the tables and root keys, the one
``[features]`` table, a local endpoint's provider block, its model catalog,
``auth.json``, and the check that the generated file parses.
"""

import contextlib
import json
import logging
import os
import stat
from pathlib import Path

from .. import config

logger = logging.getLogger("satellite")


def _validate_config_toml(text: str, path: Path) -> None:
    """Best-effort TOML validation before handing a config to codex: the strict
    TUI hard-exits (code 1, blank terminal) on invalid TOML and the app-server
    silently "uses defaults" (drops every MCP) — both are hard to diagnose from
    the outside, so make the corruption LOUD at the write site. Warn-only: the
    proxy-side writer already fail-closes; here a visible ERROR beats killing
    the spawn. The host floor (3.10) has no tomllib and parses with the
    shipped tomli, the same parser."""
    try:
        import tomllib as toml
    except ModuleNotFoundError:
        try:
            import tomli as toml  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return
    try:
        toml.loads(text)
    except toml.TOMLDecodeError as e:
        logger.error("generated codex config.toml is INVALID (%s): %s", path, e)


# File name of the per-session model catalog inside CODEX_HOME (twin of the
# proxy's ``local_model_catalog.CATALOG_FILE_NAME``).
MODEL_CATALOG_FILE_NAME = "models.json"
# config.toml [tools] table every Codex session gets (twin of the proxy's
# ``layer._CODEX_TOOLS_TABLE``): Codex 0.152.0 made the update_plan tool
# opt-in (default off) — without it the daemon never emits turn/plan/updated
# and the dashboard's TODO checklist silently disappears. Valid on older
# Codex too (where on was the default).
CODEX_TOOLS_TABLE = "[tools]\nupdate_plan = { enabled = true }"
# Header every headless (app-server) remote Codex session gets — the twin of
# the local ``layer._write_config_toml`` header and of the interactive
# satellite writer (``codex_pty_session._build_codex_config_toml``). Before
# 0.5.118 the headless writer emitted none of it, so a paired machine ran
# Codex's defaults: the 32 KiB ``project_doc_max_bytes`` cap silently dropped
# the tail of a platform AGENTS.md (persona + memory + skills + knowledge),
# Codex's own memory subsystem ran next to otodock memory (the single source
# of memory truth; there is no ``.codex/memories/`` wipe on a satellite), and
# every daemon start cloned OpenAI's curated-plugins repo (``plugins`` below).
# Root keys must precede every ``[table]`` header.
CODEX_HEADLESS_ROOT_KEYS = "project_doc_max_bytes = 300000"
CODEX_MEMORIES_TABLE = "[memories]\nuse_memories = false\ngenerate_memories = false"


def features_table(hooks_floor: bool, mcp_toml: str) -> "tuple[str, str]":
    """The ONE ``[features]`` table of a headless config.toml, plus the MCP
    TOML with its own leading ``[features]`` block lifted out.

    Returns ``(features_block, mcp_toml_rest)``. The block always carries
    ``plugins = false`` (lean start, see ``CODEX_HEADLESS_ROOT_KEYS``), adds
    ``hooks = true`` when this session runs the PreToolUse permission floor
    (``codex_hooks_floor`` or ``codex_attended_floor`` in the start payload —
    the key is default-on upstream, explicit on purpose), and keeps every key of a ``[features]``
    block the proxy prepends to the MCP TOML (``default_mode_request_user_input``
    for headless dashboard chats). Merging is what keeps the file valid: two
    ``[features]`` tables are a duplicate key Codex rejects, after which the
    app-server "uses defaults" and drops every MCP. Comments and blank lines
    of the lifted block are dropped; a key already present is not repeated.
    """
    lines = mcp_toml.split("\n") if mcp_toml else []
    keys: list[str] = ["plugins = false"]
    rest = mcp_toml
    if lines and lines[0].strip() == "[features]":
        end = len(lines)
        for i in range(1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("[") and stripped != "[features]":
                end = i
                break
        for raw in lines[1:end]:
            line = raw.strip()
            if line and not line.startswith("#"):
                keys.append(line)
        rest = "\n".join(lines[end:]).strip() if end < len(lines) else ""
    if hooks_floor:
        keys.append("hooks = true")
    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        name = key.split("=", 1)[0].strip()
        if name in seen:
            continue
        seen.add(name)
        unique.append(key)
    return "[features]\n" + "\n".join(unique), rest


def _esc_toml(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


#: The two model providers a session's config.toml can name: Codex's built-in
#: one, and the local endpoint's table ``local_provider_toml`` writes.
BUILT_IN_PROVIDER = "openai"
LOCAL_PROVIDER = "oto_local"


def runs_on_local_model(config: dict) -> bool:
    """Whether a session's payload runs it on a local endpoint: an address
    ``local_provider_toml`` writes a provider for (none for a blank one), or a
    compute machine's ``local_model`` mark, whose config.toml is the
    platform's own."""
    address = str((config.get("local_model_provider") or {}).get("base_url") or "").strip()
    return bool(address) or bool(config.get("local_model"))


def configured_provider(local: bool) -> str:
    """The model provider a session's config.toml configures: the local
    endpoint's when the session runs on one, else Codex's built-in one. What a
    resume compares a thread's saved provider with."""
    return LOCAL_PROVIDER if local else BUILT_IN_PROVIDER


def local_provider_toml(local: "dict | None", codex_dir: "Path | None" = None) -> "tuple[str, str]":
    """Codex config.toml lines for a local OpenAI-compatible endpoint the proxy
    carried in the start payload (``local_model_provider``: ``base_url`` plus the
    optional ``env_key`` naming the child-env variable that holds its bearer,
    ``stream_idle_timeout_ms`` and the optional ``catalog_json``).

    Returns ``(root_lines, table)``: ``model_provider = "oto_local"`` (and, with
    a catalog, ``model_catalog_json = "<codex_dir>/models.json"`` — the file
    ``write_or_drop_model_catalog`` writes) are ROOT keys and must precede every
    ``[table]`` header; the ``[model_providers.oto_local]`` table may close the
    file. Twin of the proxy's ``_write_config_toml`` provider block —
    ``wire_api`` MUST be ``"responses"`` (codex-rs dropped the "chat" wire),
    ``env_key`` is emitted ONLY when the endpoint is keyed (codex-rs refuses a
    provider whose ``env_key`` names an unset variable) and the idle timeout
    only when the proxy sent one (a local model prefills at CPU speed; Codex's
    5-minute default killed a first turn before its first token). ``("", "")``
    without an endpoint, so a hosted session never inherits a provider.
    """
    base_url = str((local or {}).get("base_url") or "").strip()
    if not base_url:
        return "", ""

    root = ['model_provider = "oto_local"']
    if codex_dir is not None and str((local or {}).get("catalog_json") or "").strip():
        # Codex requires an ABSOLUTE path here (a relative agents_dir in
        # satellite.conf would otherwise reject the whole config.toml).
        catalog_path = os.path.abspath(str(Path(codex_dir) / MODEL_CATALOG_FILE_NAME))
        root.append(f'model_catalog_json = "{_esc_toml(catalog_path)}"')
    lines = [
        "[model_providers.oto_local]",
        'name = "Local"',
        f'base_url = "{_esc_toml(base_url)}"',
        'wire_api = "responses"',
    ]
    try:
        idle_ms = int((local or {}).get("stream_idle_timeout_ms") or 0)
    except (TypeError, ValueError):
        idle_ms = 0
    if idle_ms > 0:
        lines.append(f"stream_idle_timeout_ms = {idle_ms}")
    env_key = str((local or {}).get("env_key") or "").strip()
    if env_key:
        lines.append(f'env_key = "{_esc_toml(env_key)}"')
    return "\n".join(root), "\n".join(lines)


#: What a session in a home must not find in its store (twin of the platform's
#: ``core/layers/codex/config_dir.STORE_LEFTOVERS``): Codex 0.169 reads the
#: ``.codex`` folder of its working folder as the project's when that folder is
#: not ``CODEX_HOME``, so a store's ``config.toml`` and ``hooks.json`` would run
#: every hook twice; a sign-in or a model catalog there is an earlier copy.
STORE_LEFTOVERS = ("config.toml", "hooks.json", "auth.json", "models.json")


def drop_store_leftovers(agent_dir: Path, store_rel: str) -> None:
    """Remove ``STORE_LEFTOVERS`` from the store at ``store_rel`` in the
    agent's folder, where a session in a home starts (an earlier version's
    files, a session that ran from the store). Each name goes beneath the
    agent's folder (``host/safe_fs``): a link on the way to the store is
    refused and nothing is removed, a link at a name is removed, never
    followed. A folder there is left; an empty sign-in stays (a mount point
    a sandbox may hold, and it holds nothing)."""
    from ..host import safe_fs
    store_rel = Path(store_rel).as_posix()
    for name in STORE_LEFTOVERS:
        rel = f"{store_rel}/{name}"
        try:
            if name == "auth.json":
                st = safe_fs.lstat_beneath(agent_dir, rel)
                if stat.S_ISREG(st.st_mode) and st.st_size == 0:
                    continue
            safe_fs.unlink_beneath(agent_dir, rel, missing_ok=True)
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            continue
        except OSError as e:
            # A link on the way, or a path that is none: the same for every name.
            logger.warning("Codex store %s: its old files are left (%s)", store_rel, e)
            return


def write_or_drop_model_catalog(codex_dir: Path, local: "dict | None") -> None:
    """``models.json`` follows the start payload exactly: write the proxy-built
    per-session model catalog (``local_model_provider.catalog_json`` — the entry
    that makes Codex defer its MCP tools on an Ollama model) owner-only, or
    remove a previous session's file when this session carries none (CODEX_HOME
    persists per agent and user; config.toml no longer points at it, so the
    directory stays honest)."""
    path = codex_dir / MODEL_CATALOG_FILE_NAME
    text = str((local or {}).get("catalog_json") or "")
    if text.strip():
        path.write_text(text, encoding="utf-8")
        chmod_private(path)
        return
    with contextlib.suppress(OSError):
        path.unlink()


def write_or_drop_auth_json(codex_dir: Path, auth_json: "dict | None") -> None:
    """``auth.json`` follows the start payload exactly. With ``auth_json`` (a
    ChatGPT OAuth session) write it owner-only; WITHOUT it (an API key or a
    local endpoint) remove a previous session's file from this persistent
    CODEX_HOME — Codex would otherwise load the stale, refresh-neutralized
    token and loop on refreshing it every request ("Failed to refresh token:
    400 … refresh_token: empty string", verified 2026-09-07 on a local-endpoint
    session that followed a ChatGPT one)."""
    auth_path = codex_dir / "auth.json"
    if auth_json:
        auth_path.write_text(json.dumps(auth_json, indent=2))
        # Live OAuth tokens — keep them owner-only (the satellite host can be
        # multi-user; default umask would leave them world-readable).
        chmod_private(auth_path)
        return
    with contextlib.suppress(OSError):
        auth_path.unlink()


def chmod_private(path: Path) -> None:
    """Owner-only for a config file that carries per-session bearers (MCP
    cap-tokens, the session JWT, a local endpoint's URL) — the proxy locks its
    own config.toml 0600 the same way. No-op on Windows (no POSIX bits)."""
    if not config.HOST.posix:
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)
