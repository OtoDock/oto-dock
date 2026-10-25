"""How a config value is written, and which keys config.env may hold.

The yes/no and number rules every ``config.py`` key is parsed by, and the key
registry the unknown-key warning at the end of ``config.py`` checks the file
against. ``config`` re-exports the public names; the reads themselves
(``_cfg`` and the file it reads) stay there, beside the code that writes the
file.
"""

import os
from collections.abc import Mapping, MutableMapping

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")
# A tri-state key (COOKIE_SECURE, RUNNING_IN_DOCKER, OTODOCK_LOCAL_PBX_ENABLED)
# keeps its derived default on "auto", as every other word did before 1.8.0.
_AUTO_WORDS = ("auto",)


def _word(raw: str) -> str:
    """A yes/no or numeric value as written, less an inline comment: dotenv
    drops one, systemd's EnvironmentFile keeps it (``X=false   # why`` arrives
    whole, ``X="true" # why`` as ``true# why``), and no accepted word or
    number holds a ``#``."""
    return raw.split("#", 1)[0].strip()


def _parse_bool(key: str, raw: str, default: bool | None) -> bool | None:
    """The yes/no rule, one for every key: 1 true yes on / 0 false no off, case
    and surrounding whitespace ignored; empty is the default; a tri-state key
    (``default`` None) also takes ``auto``. Any other word stops the boot
    naming the key: a wrong value read silently as "off" is the bug this
    replaces (``OIDC_ENABLED`` written as 1 and ``OTODOCK_AIR_GAPPED`` written
    as yes were both off)."""
    word = _word(raw).lower()
    if word == "" or (default is None and word in _AUTO_WORDS):
        return default
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise SystemExit(
        f"FATAL: {key}={word!r} is not a yes/no value: use true or false "
        f"(1/0, yes/no, on/off)."
    )


def _parse_int(key: str, raw: str, default: int, minimum: int | None = None) -> int:
    """A whole number; empty is the default; ``minimum`` clamps (the floors
    the limits keep, ``max(1, …)`` before). Anything else stops the boot
    naming the key, where ``int()`` used to raise a bare ValueError."""
    word = _word(raw)
    if word == "":
        value = default
    else:
        try:
            value = int(word, 10)
        except ValueError:
            raise SystemExit(f"FATAL: {key}={word!r} is not a whole number.") from None
    return value if minimum is None else max(minimum, value)


def _parse_float(key: str, raw: str, default: float, minimum: float | None = None) -> float:
    """A number (nan and inf refused); empty is the default; ``minimum`` clamps."""
    import math
    word = _word(raw)
    if word == "":
        value = float(default)
    else:
        try:
            value = float(word)
        except ValueError:
            raise SystemExit(f"FATAL: {key}={word!r} is not a number.") from None
        if not math.isfinite(value):
            raise SystemExit(f"FATAL: {key}={word!r} is not a number.")
    return value if minimum is None else max(minimum, value)


# ---------------------------------------------------------------------------
# The keys config.env may hold
# ---------------------------------------------------------------------------
# Keys config.py reads that config.env.example does not document, with the
# reason (tests/config/test_config_keys.py holds every other _cfg key to the
# example): generated and persisted by the proxy itself, set by the compose
# files for the container, or a developer's knob.
INTERNAL_CONFIG_KEYS: frozenset[str] = frozenset({
    "OTODOCK_INSTALL_ID",       # generated once and persisted (_persist_secret)
    "PHONE_API_SECRET",         # generated once, shared with the phone daemon
    "WOPI_SECRET",              # defaults to JWT_SECRET; a split is not a documented setup
    "RUNNING_IN_DOCKER",        # set by the compose file; /.dockerenv otherwise
    "PROXY_SERVICE_NAME",       # the compose service name, for Docker MCP URLs
    "OTODOCK_NETWORK",          # the compose network name, for Docker MCP attachment
    "OTODOCK_AGENTS_VOLUME",    # the compose volume name, for Docker MCP mounts
    "DOCKER_SOCKET_PROXY_HOST", # the compose socket-proxy service
    "DOCKER_SOCKET_PROXY_PORT",
    "PROXY_INTERNAL_URL",       # the standalone scheduler's address of the proxy
    "SCHEDULER_MODE",           # embedded | standalone (the commercial scheduler)
    "SCHEDULER_SYNC_INTERVAL",
    "SCHEDULER_TIMEZONE",       # set by the compose file; the platform setting rules
    "ALLOWED_TOOLS",            # a developer's narrowing of the CLI's tool set
})
# Keys read after import, through a function, so _CFG_KEYS_READ misses them.
LAZY_CONFIG_KEYS: frozenset[str] = frozenset({"CREDENTIAL_ENCRYPTION_KEY"})
# The egress proxy config.env may name for the proxy's own outbound calls:
# exported into the process environment, where httpx, the checked transport
# and the programs the proxy starts read it (``export_egress_proxy``). The
# plain-http and socks names stay environment-only: an ``HTTP_PROXY`` would
# send the internal plain-http calls (the Docker socket proxy, 127.0.0.1)
# through it, and a socks ``ALL_PROXY`` needs a package the proxy does not
# ship. Pairs: either spelling in the environment keeps the pair as it is.
EGRESS_PROXY_KEYS: tuple[tuple[str, str], ...] = (("HTTPS_PROXY", "https_proxy"), ("NO_PROXY", "no_proxy"))
# What an https proxy never takes when no NO_PROXY is set anywhere: this
# machine and the names the server dials on its own Docker networks (the
# database, the socket proxy, the document editor, the phone service, a model
# server on the host).
DEFAULT_NO_PROXY = ("localhost,127.0.0.1,::1,otodock-postgres,docker-socket-proxy,"
                    "collabora,file-tools,otodock-phone,host.docker.internal")
# Keys other components read from the same file: the compose files' ${VAR}
# names (tests/config/test_config_keys.py keeps this list a superset of them),
# the phone daemon's, the operator scripts' and the environment-only knobs.
FOREIGN_CONFIG_KEYS: frozenset[str] = frozenset({
    # docker-compose.yml, docker-compose.t1.yml, docker-compose.phone.yml, docker-compose.build.yml
    "DB_HOST", "DB_PORT", "OTODOCK_APPARMOR_PROFILE", "OTODOCK_DATA_SUBNET",
    "OTODOCK_DB_ADMIN_PASSWORD", "OTODOCK_DB_APP_PASSWORD", "OTODOCK_DB_INIT_WAIT_S",
    "OTODOCK_ENV_FILE", "OTODOCK_FILETOOLS_MEM_LIMIT", "OTODOCK_INTERNAL_SUBNET",
    "OTODOCK_NETWORK_SUBNET", "OTODOCK_PROXY_MEM_LIMIT", "OTODOCK_PROXY_PIDS_LIMIT",
    "OTODOCK_SOCKETPROXY_SUBNET", "OTODOCK_VERSION", "OTO_PHONE_BIND", "POSTGRES_DB",
    "POSTGRES_IMAGE", "POSTGRES_PASSWORD", "POSTGRES_USER", "PROXY_BIND_IP", "TZ", "WAIT",
    "PYTHON_IMAGE", "NODE_IMAGE",
    # phone/config.py
    "PROXY_URL", "OTO_AUDIO_LOG_TRANSCRIPTS",
    # scripts/*.sh
    "OTODOCK_BACKUP_DIR", "OTODOCK_BACKUP_RETAIN", "OTODOCK_BACKUP_RETAIN_TREES",
    "OTODOCK_AGENTS_DIR", "OTODOCK_SSH_KEYS_DIR", "OTODOCK_SESSIONS_DIR", "OTODOCK_UPGRADE_WAIT_S",
    "OTODOCK_MIN_FREE_GB",
    "OTODOCK_COMPOSE_PROJECT", "OTODOCK_POSTGRES_CONTAINER", "OTODOCK_PHONE", "OTODOCK_DIR", "OTODOCK_REF",
    "OTODOCK_VERSIONS_MD",
    # read from the process environment before this module parses the file
    "PLATFORM_DATA_DIR", "PLATFORM_CONFIG_DIR",
    # developer and test knobs read straight from the environment
    "OTODOCK_SAFE_FS_NO_OPENAT2", "OTODOCK_DB_LOOP_GUARD", "OTODOCK_DB_LOOP_GUARD_OUT",
    "OTODOCK_LOADTEST", "OTODOCK_LOADTEST_REPORT",
    # exported into the proxy's own environment (export_egress_proxy)
    *(key for pair in EGRESS_PROXY_KEYS for key in pair),
})
# Key families read elsewhere or spelled with a variable part.
FOREIGN_CONFIG_PREFIXES: tuple[str, ...] = (
    "COMPOSE_", "DOCKER_", "RATE_LIMIT_",
    "OTODOCK_MCPS_CATALOG_", "OTODOCK_AGENTS_CATALOG_", "OTODOCK_SKILLS_CATALOG_",
)


def unknown_config_keys(present, known, prefixes=FOREIGN_CONFIG_PREFIXES) -> list[str]:
    """The keys of ``present`` that neither ``known`` lists nor a prefix
    covers, sorted: the keys config.env sets that nothing reads, most often
    a misspelt one."""
    return sorted(
        k for k in present
        if k not in known and not any(k.startswith(p) for p in prefixes)
    )


def export_egress_proxy(file_cfg: Mapping[str, str | None],
                        environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Put the egress proxy ``file_cfg`` names into ``environ`` (a Docker
    install's ``.env`` reaches no process otherwise; a systemd unit's
    ``EnvironmentFile`` already did it on a source install). A pair either of
    whose spellings the environment holds stays the environment's, and an
    empty value sets nothing. An https proxy, from the file or the
    environment, with no ``NO_PROXY`` anywhere gets ``DEFAULT_NO_PROXY``, so a
    call to this machine or a name of its own network never goes out through
    it. Returns the names set."""
    exported: list[str] = []
    for pair in EGRESS_PROXY_KEYS:
        if any(key in environ for key in pair):
            continue
        for key in pair:
            value = (file_cfg.get(key) or "").strip()
            if value:
                environ[key] = value
                exported.append(key)
    https, no_proxy = EGRESS_PROXY_KEYS
    if any(environ.get(key) for key in https) and not any(key in environ for key in no_proxy):
        environ["NO_PROXY"] = DEFAULT_NO_PROXY
        exported.append("NO_PROXY")
    return exported
