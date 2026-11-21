"""A Docker tool's compose files as the platform reads and writes them: the
single-quoted ``.env`` value Compose hands a container literally, and
whether a compose mounts one file of a volume."""

import yaml

# A volume ``subpath`` mount (one file of a volume) needs Docker Engine 26,
# API 1.45: an older engine mounts the whole volume, and the service misses
# the file it expects (the file tools' Collabora runs without its WOPI key).
_SUBPATH_MIN_API = (1, 45)


def _compose_mounts_subpath(manifest) -> bool:
    """Whether the MCP's compose mounts one file of a volume."""
    try:
        data = yaml.safe_load((manifest.mcp_dir / manifest.server.docker_compose).read_text()) or {}
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return False
    services = data.get("services") if isinstance(data, dict) else None
    for svc in (services.values() if isinstance(services, dict) else ()):
        for vol in ((svc.get("volumes") or []) if isinstance(svc, dict) else ()):
            spec = vol.get("volume") if isinstance(vol, dict) else None
            if isinstance(spec, dict) and spec.get("subpath"):
                return True
    return False


def _odd_backslash_run_before_quote(value: str) -> bool:
    """Whether an odd run of backslashes ends ``value`` or precedes a ``'``
    in it (one pass; a regex over a long run of backslashes backtracks)."""
    run = 0
    for ch in value:
        if ch == "\\":
            run += 1
            continue
        if ch == "'" and run % 2:
            return True
        run = 0
    return bool(run % 2)


def dotenv_quote(value: str) -> str | None:
    """``value`` as a single-quoted ``.env`` value Compose's dotenv reader
    hands the container literally: no ``$`` interpolation, backslashes kept,
    each ``'`` written ``\\'``. ``None`` for a value that form cannot carry: a
    control character (a newline ends the line), or an odd run of
    backslashes right before a ``'`` or at the end (Compose reads the quote
    after an odd run as escaped)."""
    if any(ord(c) < 0x20 or c == "\x7f" for c in value):
        return None
    if _odd_backslash_run_before_quote(value):
        return None
    return "'" + value.replace("'", "\\'") + "'"
