"""Rewrite a Docker MCP's ``docker-compose.yml`` for the T2 (Docker-Compose) topology.

In T2 the proxy is itself a container driving the host daemon through a
read-restricted ``docker-socket-proxy`` that **blocks ``docker build``** (BuildKit
→ 403, legacy build → 403). Community Docker MCPs ship a
``build: .`` compose (build-from-context), which therefore cannot be used as-is.
This module rewrites that compose to **pull a pre-built image** (``server.image``)
and run the container as a sibling on the platform's shared network, reachable by
service-DNS, the only shape a containerised proxy can actually launch.

T2 only: on bare-metal T1 the build-from-context compose is left byte-for-byte
untouched. The compose is first judged against an allowlist, on both paths
below:

* a top-level key other than ``services``, ``volumes``, ``networks``,
  ``version`` and the ``x-*`` extension fields is refused (``include``,
  ``configs``, ``secrets`` and ``name`` among them); the ``x-*`` fields are
  dropped from the written file, their anchors already resolved at load;
* a service may not read a file, a container or the daemon beyond the MCP
  folder (``_REFUSED_SERVICE_KEYS``), and its ``env_file`` may name only the
  ``.env`` the platform writes into the folder;
* a top-level volume is a plain per-MCP volume: no ``external``, ``name``,
  ``driver_opts`` or driver other than ``local``;
* no key or value may interpolate a variable (compose fills them from the
  proxy's own environment), apart from the agents-tree bind below and a whole
  size value such as ``mem_limit``;
* no name may claim a platform service (``_RESERVED_PLATFORM_NAMES``).

A build-from-context compose is then rewritten to pull form:

* drop ``build:`` (and any BuildKit ``additional_contexts``);
* set ``image:`` from the manifest's ``server.image``;
* ``container_name: otodock-<install_id>-mcp-<name>``, stable and collision-free.

Every compose, rewritten above or shipped in pull form, is hardened the same way:

* each service joins only the external platform network
  (``config.OTODOCK_NETWORK``), the target with a network **alias = the MCP's
  service-DNS name** so ``http://<service>:<port>`` resolves from the proxy (and
  from local agents that share the proxy's netns);
* published host ``ports:`` and the container-escape keys
  (``_DANGEROUS_COMPOSE_KEYS``) are stripped from every service;
* host **bind-mounts become named volumes**: a relative or absolute host path
  resolves on the *daemon host*, not inside the proxy container, so a bind-mount
  is meaningless in T2. The ``${HOST_AGENTS_DIR}`` agents-tree bind (the catalog
  contract for MCPs that work on agent files, such as video-tools) maps to the
  platform's **shared external agents volume** (``config.OTODOCK_AGENTS_VOLUME``),
  the same volume the proxy and file-tools mount, never to a fresh empty per-MCP
  volume;
* default **memory bounds and log rotation** go into services that declare none
  of their own (``_missing_default_bounds``; the same defaults reach T1 through
  the generated override), so a leaky community sidecar can't starve session
  admission or fill the disk.

The result is a fixed point: a hardened file is left unchanged by every later
call, so it is safe to call on every install and every start. Comments are not
preserved: the installed compose is a generated runtime artifact; the pristine
source lives in the community catalog repo.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

import yaml

import config
from core.config import deployment
from services.mcp import mcp_manifest_parse as _mmp
from services.mcp import mcp_manifest_types as _mt

logger = logging.getLogger("claude-proxy.compose-rewrite")

# The in-compose key for the platform's shared network. Mapped to the real
# (external) network name via ``{external: true, name: <OTODOCK_NETWORK>}``.
_NET_KEY = "otodock"

# Compose keys that grant a container host-level access or namespace escape. A
# community-MCP compose must never set these; they are stripped during the T2
# rewrite so a malicious catalog PR can't break out of the sandbox network.
_DANGEROUS_COMPOSE_KEYS = frozenset({
    "privileged", "cap_add", "devices", "device_cgroup_rules",
    "pid", "ipc", "uts", "userns_mode", "cgroup", "cgroup_parent",
    "network_mode", "security_opt", "sysctls", "group_add",
})

# The top-level keys a Docker MCP compose may carry besides the ``x-*``
# extension fields (inert data); compose ignores ``version``. Anything else is
# refused: ``include`` loads another file verbatim, ``configs`` and ``secrets``
# bind a path of the daemon host, ``name`` renames the project, and a key a
# later compose release adds has unknown reach.
_TOP_LEVEL_KEYS = frozenset({"services", "volumes", "networks", "version"})

# Service keys refused outright. Each reads a file, a container or the daemon
# beyond the MCP folder when the compose loads or starts (``configs``,
# ``secrets``, ``volumes_from``, ``extends``, ``include``, ``label_file``,
# ``use_api_socket``), runs a plugin on the compose client (``provider``), or
# runs a lifecycle hook that can ask for a privileged exec (``post_start``,
# ``pre_stop``).
_REFUSED_SERVICE_KEYS = frozenset({
    "configs", "secrets", "volumes_from", "extends", "include", "label_file",
    "use_api_socket", "provider", "post_start", "pre_stop",
})

# The env file ``docker_manager._inject_mcp_env`` writes into the MCP folder:
# the one ``env_file`` a service may load. The compose client reads any other
# path inside the proxy container, where the platform's ``config.env`` lives.
_OWN_ENV_FILES = frozenset({".env", "./.env"})
_ENV_FILE_ENTRY_KEYS = frozenset({"path", "required", "format"})

# Labels compose sets itself; the boot network self-check identifies the
# platform's containers by them.
_COMPOSE_LABEL_PREFIX = "com.docker.compose."

# The long-form mount types the rewrite understands.
_MOUNT_TYPES = frozenset({"bind", "volume", "tmpfs"})

# The one interpolation kept besides the agents-tree bind: a whole size value,
# as the catalog's per-install memory knobs use
# (``mem_limit: ${OTODOCK_CAMOUFOX_MEM_LIMIT:-3g}``). A size parses as a byte
# count or fails the load, so no string reaches the container through it.
_SIZE_KEYS = frozenset({"mem_limit", "memswap_limit", "mem_reservation", "shm_size"})
_SIZE_INTERPOLATION_RE = re.compile(
    r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-[0-9]+(?:\.[0-9]+)?[kKmMgG]?[bB]?)?\}$"
)

# The platform's compose services. Their names, the names compose gives their
# containers (``<project>-<service>-<n>``), the bare-metal stack's fixed
# container names and the hosts the proxy is configured to dial are reserved:
# a community MCP claiming one as a service key, container_name, service_name
# or network alias would join Docker's round-robin for a name the proxy dials
# (intercepting PROXY_API_KEY, the phone secret or a database login) or get a
# platform address carved into the sandbox. Matched case-insensitively (Docker
# DNS is case-insensitive). The carve-side refusal lives in mcp_registry.
_PLATFORM_SERVICES = (
    "otodock-proxy", "otodock-postgres", "docker-socket-proxy", "otodock-phone",
    "otodock-db-init", "otodock-agents-init", "file-tools", "collabora",
)
_RESERVED_PLATFORM_NAMES = frozenset({
    *_PLATFORM_SERVICES, "file-tools-mcp", "collabora-agent",
})
_PLATFORM_CONTAINER_RE = re.compile(
    r"^otodock[-_](?:"
    + "|".join(re.escape(s) for s in _PLATFORM_SERVICES)
    + r")[-_]\d+$"
)


def _configured_platform_hosts() -> set[str]:
    """The hosts the proxy dials by configuration, lower-cased."""
    hosts = {config.DOCKER_SOCKET_PROXY_HOST, config.PROXY_SERVICE_NAME}
    for url in (config.DATABASE_URL, config.PHONE_SERVER_URL, config.COLLABORA_BACKEND_URL):
        try:
            host = urlsplit(url or "").hostname
        except ValueError:
            host = None
        if host:
            hosts.add(host)
    return {h.strip().lower() for h in hosts if isinstance(h, str) and h.strip()}


def _is_reserved_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    name = value.strip().lower().rstrip(".")
    return (
        name in _RESERVED_PLATFORM_NAMES
        or _PLATFORM_CONTAINER_RE.match(name) is not None
        or name in _configured_platform_hosts()
    )


def _reject_reserved_name(kind: str, value: object, mcp_name: str) -> None:
    """Raise ValueError if ``value`` is a reserved platform name (case-insensitive)."""
    if _is_reserved_name(value):
        raise ValueError(
            f"{mcp_name}: MCP compose {kind} {value!r} collides with a reserved "
            f"platform service name, refusing (it would join the service-DNS of a "
            f"name the proxy dials, or get a platform address carved into the "
            f"sandbox)"
        )


# Named volumes the platform compose owns that no community MCP may mount:
# attaching one hands the sidecar the Postgres unix socket (which the image
# trusts: an instant superuser), the raw heap files, the SSH keys or the session
# state. The agents volume is the one legitimate reach (the ``${HOST_AGENTS_DIR}``
# contract maps onto it, and a healed pull-form file names it directly), so it
# is not listed. Case-insensitive, like the service names.
_PLATFORM_VOLUMES = frozenset({
    "otodock-pgdata", "otodock-pgsock", "otodock-mcps", "otodock-skills",
    "otodock-ssh-keys", "otodock-sessions",
})


def _reject_platform_volume(kind: str, value, mcp_name: str) -> None:
    """Raise ValueError if ``value`` names a platform-owned volume."""
    if isinstance(value, str) and value.strip().lower() in _PLATFORM_VOLUMES:
        raise ValueError(
            f"{mcp_name}: MCP compose {kind} {value!r} names a platform volume; "
            f"refusing (a community MCP may only declare per-MCP volumes; the "
            f"agents tree is reached through the ${{HOST_AGENTS_DIR}} bind)"
        )


def _reject_manifest_volume_declarations(data: dict, mcp_name: str) -> None:
    """Refuse a top-level ``volumes:`` entry that is not a plain per-MCP volume.

    ``external`` or ``name`` binds the service to a volume the manifest did not
    create, and ``driver_opts`` or a driver other than ``local`` can make the
    volume a bind of a daemon-host path, so each is refused whatever the name.
    The single exception is the agents mapping the rewrite declares itself
    (``_declare_volumes``), which a healed pull-form file carries on re-entry.
    """
    vols = data.get("volumes")
    if vols is None:
        return
    if not isinstance(vols, dict):
        raise ValueError(
            f"{mcp_name}: MCP compose top-level volumes is not a mapping, refusing"
        )
    agents_vol = config.OTODOCK_AGENTS_VOLUME
    for key, spec in vols.items():
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ValueError(
                f"{mcp_name}: MCP compose volume {key!r} is not a mapping, refusing"
            )
        if key == agents_vol and spec == {"external": True, "name": agents_vol}:
            continue
        if spec.get("external") or spec.get("name"):
            raise ValueError(
                f"{mcp_name}: MCP compose volume {key!r} attaches an external or "
                f"named volume; refusing (a community MCP may only declare "
                f"per-MCP volumes)"
            )
        if set(spec) - {"driver", "labels"} or spec.get("driver", "local") != "local":
            raise ValueError(
                f"{mcp_name}: MCP compose volume {key!r} declares driver options "
                f"or a driver other than local, refusing (either can bind a path "
                f"of the daemon host)"
            )

# Container-log rotation injected into services that set no `logging:` of their
# own — catalog composes rarely do, and an uncapped json-file log grows without
# bound over months of sidecar uptime. Matches the platform compose's x-logging
# anchor.
_LOG_DEFAULTS = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}


def _default_mem_limit() -> str:
    """The configured sidecar memory floor; '' when injection is disabled."""
    raw = str(getattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g")).strip().lower()
    return "" if raw in ("", "0", "none", "off") else raw


def _missing_default_bounds(svc: dict) -> dict:
    """Default resource-bound keys ``svc`` doesn't declare itself.

    An MCP's own limits always win: `mem_limit`, `memswap_limit`, or a
    `deploy.resources.limits.memory` in the catalog compose suppresses the
    memory injection; an explicit `logging:` suppresses the log-rotation
    injection. mem and memswap are set EQUAL — no swap growth, so a leaky
    sidecar OOM-restarts (`restart: always` self-heals) instead of dragging
    the host into thrash and vetoing session admission.
    """
    out: dict = {}
    mem = _default_mem_limit()
    deploy_mem = (
        ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    ).get("memory")
    if mem and "mem_limit" not in svc and "memswap_limit" not in svc and not deploy_mem:
        out["mem_limit"] = mem
        out["memswap_limit"] = mem
    if "logging" not in svc:
        out["logging"] = copy.deepcopy(_LOG_DEFAULTS)
    return out


def _slug(s: str) -> str:
    """Lowercase ``s`` to a ``[a-z0-9-]`` token (for volume/container names)."""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _interpolates(value: str) -> bool:
    """True when compose would substitute into ``value``: ``$$`` is a literal
    ``$``, and any other ``$`` starts a substitution or fails the load."""
    return "$" in value.replace("$$", "")


def _service_aliases(svc: dict) -> list[str]:
    """The network aliases a service declares, on any of its networks."""
    out: list[str] = []
    nets = svc.get("networks")
    if isinstance(nets, dict):
        for cfg in nets.values():
            if not isinstance(cfg, dict):
                continue
            aliases = cfg.get("aliases")
            if isinstance(aliases, str):
                out.append(aliases)
            elif isinstance(aliases, list):
                out.extend(a for a in aliases if isinstance(a, str))
    return out


def _is_own_env_file(entry: object) -> bool:
    if isinstance(entry, dict):
        path = entry.get("path")
        return (
            isinstance(path, str) and path in _OWN_ENV_FILES
            and not set(entry) - _ENV_FILE_ENTRY_KEYS
        )
    return isinstance(entry, str) and entry in _OWN_ENV_FILES


def _reject_foreign_env_file(key: str, value: object, mcp_name: str) -> None:
    """Refuse an ``env_file`` other than the MCP's own ``.env``: a string, or a
    list of strings and ``{path, required, format}`` entries."""
    if isinstance(value, str):
        own = _is_own_env_file(value)
    elif isinstance(value, list):
        own = all(_is_own_env_file(entry) for entry in value)
    else:
        own = False
    if not own:
        raise ValueError(
            f"{mcp_name}: MCP compose service {key!r} env_file {value!r} is not "
            f"the MCP's own .env, refusing (the compose client would read it "
            f"inside the proxy container)"
        )


def _reject_compose_labels(key: str, labels: object, mcp_name: str) -> None:
    """Refuse a service label in the namespace compose sets itself."""
    if isinstance(labels, dict):
        names = [str(k) for k in labels]
    elif isinstance(labels, list):
        names = [str(item).split("=", 1)[0] for item in labels]
    else:
        return
    owned = sorted(n for n in names if n.strip().lower().startswith(_COMPOSE_LABEL_PREFIX))
    if owned:
        raise ValueError(
            f"{mcp_name}: MCP compose service {key!r} sets the compose-owned "
            f"label {owned}, refusing (the platform identifies its own containers "
            f"by them)"
        )


def _harden_service(
    key: str, svc: dict, mcp_name: str,
    declared_volumes: set[str], external_volumes: set[str],
) -> None:
    """Apply the T2 sandbox rules to ONE service dict, in place.

    Runs for EVERY service of EVERY compose the T2 path touches, target or
    sibling, build-form or already-pull-form: a compose shipped pull-form (or a
    sibling service) gets the exact same treatment as the rewritten target, or
    a hijacked catalog entry sidesteps it by simply not declaring ``build:``.
    Refusals raise ValueError; the escape keys, published ports and host binds
    are stripped or converted.
    """
    _reject_reserved_name("service key", key, mcp_name)
    _reject_reserved_name("container_name", svc.get("container_name"), mcp_name)
    for alias in _service_aliases(svc):
        _reject_reserved_name("network alias", alias, mcp_name)
    refused = sorted(k for k in svc if k in _REFUSED_SERVICE_KEYS)
    if refused:
        raise ValueError(
            f"{mcp_name}: MCP compose service {key!r} sets {refused}, refusing "
            f"(each reads a file, a container or the daemon beyond the MCP folder)"
        )
    if "env_file" in svc:
        _reject_foreign_env_file(key, svc["env_file"], mcp_name)
    _reject_compose_labels(key, svc.get("labels"), mcp_name)
    dropped = [k for k in _DANGEROUS_COMPOSE_KEYS if k in svc]
    for k in dropped:
        svc.pop(k, None)
    if dropped:
        logger.warning(
            "compose_rewrite: stripped unsafe keys %s from service %r (%s)",
            sorted(dropped), key, mcp_name,
        )
    svc.pop("ports", None)
    if "volumes" in svc:
        if not isinstance(svc["volumes"], list):
            raise ValueError(
                f"{mcp_name}: MCP compose service {key!r} volumes is not a list, refusing"
            )
        svc["volumes"] = _rewrite_volumes(
            svc["volumes"], mcp_name, declared_volumes, external_volumes,
        )


def _is_host_bind_source(src: str) -> bool:
    """True if a short-form volume source is a host path (not a named volume).

    A named volume is a bare token (``mydata``); a host bind-mount source is a
    relative (``./x``, ``../x``), home (``~/x``) or absolute (``/x``) path.
    """
    return src.startswith((".", "/", "~"))


# The catalog contract for "this MCP works on the agents tree": a bind whose
# source is the ``${HOST_AGENTS_DIR}`` env var (optionally with a compose
# ``:-default``), e.g. ``${HOST_AGENTS_DIR:-../../../agents}:/agents:rw``
# (video-tools). On T1 the platform-written ``.env`` interpolates it to the
# real host path — a working bind. On T2 the value would be the *proxy
# container's* path, which on the daemon host is an empty (auto-created) dir —
# the real agents tree lives in the platform's shared named volume — so the
# rewrite maps this mount to that volume instead, declared ``external``
# because the platform compose owns its lifecycle.
_AGENTS_SRC_RE = re.compile(r"^\$\{HOST_AGENTS_DIR(?::?-[^}]*)?\}$")
_AGENTS_SHORT_RE = re.compile(
    r"^\$\{HOST_AGENTS_DIR(?::?-[^}]*)?\}:(?P<dst>[^:]+)(?::(?P<mode>[^:]+))?$"
)


def _rewrite_volumes(
    volumes: list, mcp_name: str, declared: set[str], external: set[str],
) -> list:
    """Convert host bind-mounts to per-MCP named volumes; keep named volumes.

    Mutates ``declared`` with any named volumes it creates and ``external``
    when an agents-tree mount is mapped onto the shared platform volume (the
    caller declares both at the compose top level). Handles both short-form
    (``"src:dst[:mode]"``) and long-form (``{type: bind, source, target}``)
    entries; a source compose would interpolate, other than the whole
    agents-tree form, is refused (it resolves as a daemon-host bind).
    """
    agents_vol = config.OTODOCK_AGENTS_VOLUME
    out: list = []
    for v in volumes:
        if isinstance(v, str):
            agents = _AGENTS_SHORT_RE.match(v)
            if agents:
                external.add(agents_vol)
                mode = agents.group("mode")
                out.append(
                    f"{agents_vol}:{agents.group('dst')}" + (f":{mode}" if mode else "")
                )
                continue
            parts = v.split(":")
            src = parts[0]
            if _interpolates(src):
                raise ValueError(
                    f"{mcp_name}: MCP compose volume {v!r} interpolates its source, "
                    f"refusing (it would resolve as a bind of the daemon host; only "
                    f"the whole ${{HOST_AGENTS_DIR}} form maps onto the agents volume)"
                )
            if _is_host_bind_source(src):
                dst = parts[1] if len(parts) > 1 else src
                mode = parts[2] if len(parts) > 2 else ""
                name = f"otodock-{config.INSTALL_ID}-mcp-{_slug(mcp_name)}-{_slug(dst)}"
                declared.add(name)
                out.append(f"{name}:{dst}" + (f":{mode}" if mode else ""))
            else:
                _reject_platform_volume("volume source", src, mcp_name)
                out.append(v)  # already a named volume
        elif isinstance(v, dict):
            mount_type = v.get("type")
            if mount_type not in _MOUNT_TYPES:
                raise ValueError(
                    f"{mcp_name}: MCP compose mount {v!r} has type {mount_type!r}, "
                    f"refusing (only bind, volume and tmpfs mounts are understood)"
                )
            src = str(v.get("source", ""))
            if mount_type == "bind" and _AGENTS_SRC_RE.match(src):
                external.add(agents_vol)
                ro = ":ro" if v.get("read_only") else ""
                out.append(f"{agents_vol}:{v.get('target', '')}{ro}")
            elif mount_type == "bind":
                dst = v.get("target", "")
                name = f"otodock-{config.INSTALL_ID}-mcp-{_slug(mcp_name)}-{_slug(dst)}"
                declared.add(name)
                out.append(f"{name}:{dst}")
            else:
                _reject_platform_volume("volume source", v.get("source"), mcp_name)
                out.append(v)  # named-volume long form or tmpfs
        else:
            raise ValueError(
                f"{mcp_name}: MCP compose volume entry {v!r} is not a mount, refusing"
            )
    return out


def _declare_volumes(data: dict, declared: set[str], external: set[str]) -> None:
    """Merge ``_rewrite_volumes``'s outputs into the compose top-level ``volumes``.

    Per-MCP names get a default-driver declaration; the shared agents volume is
    attached ``external`` (the platform compose owns it — attach, never create).
    """
    if not declared and not external:
        return
    vols = data.get("volumes")
    if not isinstance(vols, dict):
        vols = {}
    for name in sorted(declared):
        vols.setdefault(name, None)  # null ⇒ default-driver named volume
    for name in sorted(external):
        vols[name] = {"external": True, "name": name}
    data["volumes"] = vols


def _reject_interpolation(data: dict, mcp_name: str) -> None:
    """Refuse any key or value of the finished compose that compose would
    interpolate.

    The variables come from the compose client's environment, which is the
    proxy's own (``DATABASE_URL`` carries the application password), and from
    the MCP's ``.env``. Runs after the agents-tree bind is mapped onto the named
    volume; a whole size value (``_SIZE_INTERPOLATION_RE``) is the one form
    kept. The walk visits a node shared through a YAML alias once.
    """
    stack: list[tuple[tuple[str, ...], object]] = [((), data)]
    seen: set[int] = set()
    while stack:
        path, node = stack.pop()
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                continue
            seen.add(id(node))
            items = node.items() if isinstance(node, dict) else enumerate(node)
            for k, v in items:
                if isinstance(k, str) and _interpolates(k):
                    raise ValueError(
                        f"{mcp_name}: MCP compose key {k!r} interpolates a variable, "
                        f"refusing"
                    )
                stack.append(((*path, str(k)), v))
        elif isinstance(node, str) and _interpolates(node):
            if (
                len(path) == 3 and path[0] == "services" and path[2] in _SIZE_KEYS
                and _SIZE_INTERPOLATION_RE.match(node)
            ):
                continue
            raise ValueError(
                f"{mcp_name}: MCP compose value {'.'.join(path)} {node!r} "
                f"interpolates a variable, refusing (compose would fill it from the "
                f"proxy's own environment; write a literal $ as $$)"
            )


def _checked_services(data: object, mcp_name: str) -> dict:
    """The compose's ``services`` mapping, once the top level has passed the
    allowlist. Raises ValueError on any other shape."""
    if not isinstance(data, dict):
        raise ValueError("compose is not a mapping")
    unknown = sorted(
        str(k) for k in data
        if not (isinstance(k, str) and (k in _TOP_LEVEL_KEYS or k.startswith("x-")))
    )
    if unknown:
        raise ValueError(
            f"{mcp_name}: MCP compose top-level keys {unknown} are not allowed, "
            f"refusing (a Docker MCP compose carries only services, volumes, "
            f"networks, version and x-* fields)"
        )
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("compose has no `services` mapping")
    for key, svc in services.items():
        if not isinstance(svc, dict):
            raise ValueError(f"service {key!r} is not a mapping")
    return services


def _pick_target_service(services: dict, service_name: str) -> str:
    """Choose the service to turn into the pulled image.

    Prefer the key matching ``service_name``; else the sole service; else the
    single one declaring ``build``. Raise if ambiguous — an unexpected
    multi-service shape we won't silently mis-rewrite.
    """
    if service_name in services:
        return service_name
    if len(services) == 1:
        return next(iter(services))
    builds = [k for k, s in services.items() if isinstance(s, dict) and "build" in s]
    if len(builds) == 1:
        return builds[0]
    raise ValueError(
        f"cannot determine which service to rewrite among {sorted(services)} "
        f"(declare server.service_name to disambiguate)"
    )


def _pick_pull_target(services: dict, service_name: str) -> str:
    """The service a pull-form compose serves the MCP from: the key matching
    ``service_name``, else the sole service, else the one service whose network
    aliases carry ``service_name`` (the rewrite's own output). Raises when none
    or several qualify."""
    if service_name in services:
        return service_name
    if len(services) == 1:
        return next(iter(services))
    aliased = [k for k, s in services.items() if service_name in _service_aliases(s)]
    if len(aliased) == 1:
        return aliased[0]
    raise ValueError(
        f"cannot determine which service serves {service_name!r} among "
        f"{sorted(services)} (declare server.service_name to disambiguate)"
    )


def _harden_compose(
    data: dict, *, target: str, service_name: str, network_name: str, mcp_name: str,
) -> dict:
    """The hardening both paths share, on a deep copy of ``data``.

    Every service is judged and stripped (``_harden_service``), joins only the
    shared network (the target with the service-DNS alias, the siblings to
    reach it) and gets the default bounds; the top-level networks are replaced
    and the volumes the rewrite maps are declared. The ``x-*`` extension
    fields are dropped: compose reads them only as YAML anchor sources, which
    the load has already resolved into the services.
    """
    _reject_reserved_name("service_name", service_name, mcp_name)
    out = copy.deepcopy(data)
    for key in [k for k in out if isinstance(k, str) and k.startswith("x-")]:
        del out[key]
    _reject_manifest_volume_declarations(out, mcp_name)
    declared: set[str] = set()
    external: set[str] = set()
    for key, svc in out["services"].items():
        _harden_service(key, svc, mcp_name, declared, external)
    for key, svc in out["services"].items():
        svc["networks"] = (
            {_NET_KEY: {"aliases": [service_name]}} if key == target else {_NET_KEY: {}}
        )
        svc.update(_missing_default_bounds(svc))
    out["networks"] = {_NET_KEY: {"external": True, "name": network_name}}
    _declare_volumes(out, declared, external)
    return out


def transform_compose_dict(
    data: dict,
    *,
    image: str,
    service_name: str,
    container_name: str,
    network_name: str,
    mcp_name: str,
) -> dict:
    """Pure transform: build-from-context compose dict → pull-form compose dict.

    Returns a NEW dict (the input is not mutated). Raises ``ValueError`` on a
    shape the allowlist refuses or that can't be safely rewritten for T2 (no
    services, a non-mapping service, or a multi-service compose where a
    *non-target* service also needs to be built: we can't pull an image we
    don't know).
    """
    services = _checked_services(data, mcp_name)
    target = _pick_target_service(services, service_name)
    others_built = [
        k for k, s in services.items()
        if k != target and isinstance(s, dict) and "build" in s
    ]
    if others_built:
        raise ValueError(
            f"compose has additional build-from-context services {others_built} "
            f"with no pre-built image — unsupported in Docker-Compose mode"
        )
    out = _harden_compose(
        data, target=target, service_name=service_name,
        network_name=network_name, mcp_name=mcp_name,
    )
    svc = out["services"][target]
    svc.pop("build", None)
    svc["image"] = image
    svc["container_name"] = container_name
    _reject_interpolation(out, mcp_name)
    return out


_HEADER = (
    "# AUTO-GENERATED for the Docker-Compose (T2) topology — DO NOT EDIT.\n"
    "# The containerised proxy drives the daemon through a docker-socket-proxy\n"
    "# that blocks `docker build`, so the catalog's build-from-context compose\n"
    "# was rewritten to PULL the pre-built image and join the shared network.\n"
    "# Source of truth = the catalog repo + services/mcp/compose_rewrite.py.\n"
)


def _plan_pull_compose(manifest) -> tuple[Path, object, dict] | None:
    """The compose file, its current content and the content T2 runs.

    ``None`` where the rewrite does not apply (bare-metal T1, a non-docker
    MCP). Raises ``ValueError`` with an actionable message when the MCP cannot
    run in T2: a compose file missing or outside the MCP folder, no
    ``server.image`` for a build-form compose, or a shape the allowlist
    refuses.
    """
    if not deployment.in_docker_compose():
        return None  # T1 / T3: the build-from-context compose stays untouched
    srv = getattr(manifest, "server", None)
    if not _mt.is_container(srv):
        return None
    compose_path = _mmp.resolve_skill_file(manifest.mcp_dir, srv.docker_compose)
    if compose_path is None:
        raise ValueError(
            f"{manifest.name}: compose file {srv.docker_compose!r} not found in "
            f"{manifest.mcp_dir} (it must be a regular file inside the MCP folder)"
        )
    try:
        data = yaml.safe_load(compose_path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(
            f"{manifest.name}: compose file {srv.docker_compose!r} is not valid YAML: {e}"
        ) from e
    services = _checked_services(data, manifest.name)
    service_name = deployment.mcp_service_name(manifest)
    expected = f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}"

    if any("build" in s for s in services.values()):
        if not srv.image:
            raise ValueError(
                f"{manifest.name}: this Docker MCP has no pre-built image "
                f"(server.image is unset): it cannot be installed in Docker-Compose "
                f"mode because the docker-socket-proxy blocks `docker build`. Add "
                f"server.image to its manifest, or run it on a bare-metal proxy."
            )
        planned = transform_compose_dict(
            data,
            image=srv.image,
            service_name=service_name,
            container_name=expected,
            network_name=config.OTODOCK_NETWORK,
            mcp_name=manifest.name,
        )
        return compose_path, data, planned

    target = _pick_pull_target(services, service_name)
    planned = _harden_compose(
        data, target=target, service_name=service_name,
        network_name=config.OTODOCK_NETWORK, mcp_name=manifest.name,
    )
    # A container_name stamped by a PREVIOUS install identity is re-stamped.
    # The install-id lives in config.env, so a recreated config.env (install
    # moved to a new folder) rotates it, and a stale stamp would make every
    # `up` collide with the previous generation's container ("name already in
    # use", an opaque 500 on start, forever). A name that is not otodock-shaped
    # is the catalog's own and stays.
    svc = planned["services"][target]
    current = svc.get("container_name")
    if (
        isinstance(current, str)
        and current.startswith("otodock-")
        and "-mcp-" in current
        and current != expected
    ):
        svc["container_name"] = expected
    _reject_interpolation(planned, manifest.name)
    return compose_path, data, planned


def check_pull_compose(manifest) -> None:
    """Run every refusal of ``ensure_pull_compose`` and write nothing: the
    install gate runs it on the incoming folder before any file moves."""
    _plan_pull_compose(manifest)


def ensure_pull_compose(manifest) -> bool:
    """Idempotently bring a Docker MCP's compose to its hardened pull form, **T2 only**.

    Returns ``True`` if the file was rewritten, ``False`` when no change is
    needed (bare-metal T1, a non-docker MCP, or a compose already in its
    hardened pull form). Raises ``ValueError`` with an actionable message when
    the MCP cannot run in T2 (see ``_plan_pull_compose``).
    """
    plan = _plan_pull_compose(manifest)
    if plan is None:
        return False
    compose_path, current, planned = plan
    if planned == current:
        return False
    compose_path.write_text(
        _HEADER + yaml.safe_dump(planned, sort_keys=False, default_flow_style=False)
    )
    logger.info(
        "Rewrote %s compose for Docker-Compose mode (image=%s, net=%s)",
        manifest.name, manifest.server.image or "(as shipped)", config.OTODOCK_NETWORK,
    )
    return True


# ---------------------------------------------------------------------------
# T1 (bare-metal) override — subnet pin + namespaced container + image tag
# ---------------------------------------------------------------------------
#
# On T1 the proxy owns the local docker daemon and runs each Docker MCP's
# *pristine* build-from-context compose unchanged — EXCEPT for a generated
# ``docker-compose.override.yml`` beside it (merged via ``-f base -f override``)
# that adds exactly three things:
#   1. ``networks.default.ipam.config[].subnet`` — pins the per-MCP
#      ``<project>_default`` bridge to a unique /24 from OTODOCK_MCP_ADDRESS_POOL,
#      so a busy host (172.16/12 exhausted) never auto-grabs a 192.168.x bridge
#      that overlaps the operator's LAN.
#   2. ``container_name: otodock-<install_id>-mcp-<name>`` — collision-free across
#      OtoDock installs on the same daemon (T1 base composes carry a raw name).
#   3. ``image: <server.image>`` — so a fallback ``up --build`` tags the local
#      image with the canonical GHCR ref (and ``compose pull`` can resolve it),
#      which makes image-reclaim on delete/update work uniformly.
# This file is additive — it keeps the base ``build:`` so the build fallback
# still works; the base catalog compose is never mutated (unlike the T2 rewrite).

_OVERRIDE_NAME = "docker-compose.override.yml"

_T1_HEADER = (
    "# AUTO-GENERATED for bare-metal (T1) — DO NOT EDIT.\n"
    "# Pins this MCP's bridge to a private /24 (no LAN overlap), namespaces the\n"
    "# container, and tags the build/pull as server.image. Regenerated on every\n"
    "# start; the subnet is reused across restarts/updates. Source of truth =\n"
    "# the catalog compose + services/mcp/compose_rewrite.py.\n"
)

# Serializes the scan-live-networks + pick-free-/24 critical section. start paths
# run inside ``asyncio.to_thread`` (worker threads), so this is a threading.Lock,
# NOT an asyncio.Lock. Without it, two MCPs starting at once could pick the same
# free /24 from a stale scan.
_allocate_lock = threading.Lock()


def _collect_used_subnets(exclude_network: str | None = None) -> list[str]:
    """Return the IPv4 subnets currently allocated to docker networks.

    ``exclude_network`` (a network NAME) is skipped — used to ignore this MCP's
    own ``<project>_default`` bridge so a re-allocation can reuse its existing
    subnet rather than treat it as a conflict. Best-effort: any docker error
    returns ``[]`` (we then allocate without it; never block a start).
    """
    try:
        ls = subprocess.run(
            ["docker", "network", "ls", "-q"],
            capture_output=True, text=True, timeout=15,
        )
        ids = ls.stdout.split()
        if not ids:
            return []
        ins = subprocess.run(
            ["docker", "network", "inspect", *ids],
            capture_output=True, text=True, timeout=30,
        )
        nets = json.loads(ins.stdout or "[]")
    except Exception as e:
        logger.warning("docker subnet scan failed (%s) — allocating without it", e)
        return []
    out: list[str] = []
    for net in nets:
        if exclude_network and net.get("Name") == exclude_network:
            continue
        for cfg in (net.get("IPAM") or {}).get("Config") or []:
            sub = cfg.get("Subnet")
            if sub:
                out.append(sub)
    return out


def allocate_mcp_subnet(
    pool_cidr: str, used_subnets: list[str], recorded: str | None = None,
) -> str | None:
    """Pick a free ``/24`` from ``pool_cidr`` not overlapping any ``used_subnets``.

    Reuses ``recorded`` (the subnet a prior override already pinned) when it's a
    valid in-pool /24 that doesn't overlap a used subnet — keeping the MCP's
    subnet stable across restarts/updates. Returns ``None`` when the pool is
    invalid, not IPv4, or exhausted (the caller then omits ``ipam`` and lets
    docker auto-allocate). Overlap is checked with ``ipaddress.overlaps`` (NOT
    exact match) so a used network *larger* than /24 (e.g. a /16) is respected.
    """
    try:
        pool = ipaddress.ip_network(pool_cidr, strict=False)
    except ValueError:
        logger.warning("invalid OTODOCK_MCP_ADDRESS_POOL %r — no subnet pin", pool_cidr)
        return None
    if pool.version != 4:
        return None

    used: list[ipaddress.IPv4Network] = []
    for s in used_subnets:
        try:
            n = ipaddress.ip_network(s, strict=False)
        except ValueError:
            continue
        if n.version == 4:
            used.append(n)

    if recorded:
        try:
            rec = ipaddress.ip_network(recorded, strict=False)
            if (
                rec.version == 4
                and rec.subnet_of(pool)
                and not any(rec.overlaps(u) for u in used)
            ):
                return str(rec)
        except (ValueError, TypeError):
            pass

    # A pool that is itself a /24 (or smaller) can't be carved into /24s — the
    # whole pool is the single candidate (one MCP). ``subnets(new_prefix=24)``
    # raises "new prefix must be longer" for prefixlen >= 24, so guard explicitly.
    if pool.prefixlen >= 24:
        return str(pool) if not any(pool.overlaps(u) for u in used) else None

    for cand in pool.subnets(new_prefix=24):
        if not any(cand.overlaps(u) for u in used):
            return str(cand)

    logger.warning(
        "OTODOCK_MCP_ADDRESS_POOL %s exhausted — docker will auto-allocate", pool_cidr,
    )
    return None


def _read_recorded_subnet(override_path: Path) -> str | None:
    """Best-effort read of the subnet a prior override pinned (for reuse)."""
    if not override_path.is_file():
        return None
    try:
        old = yaml.safe_load(override_path.read_text()) or {}
        cfgs = (
            (((old.get("networks") or {}).get("default") or {}).get("ipam") or {})
            .get("config") or []
        )
        if isinstance(cfgs, list) and cfgs and isinstance(cfgs[0], dict):
            return cfgs[0].get("subnet")
    except Exception:
        return None
    return None


def t1_override_path(manifest) -> Path:
    """Path to the (possibly absent) T1 override beside the base compose."""
    return manifest.mcp_dir / _OVERRIDE_NAME


def ensure_t1_override(manifest, *, force_realloc: bool = False) -> Path | None:
    """Generate/refresh the T1 ``docker-compose.override.yml`` — **T1 only**.

    No-op (returns ``None``) on T2/T3, for non-docker MCPs, or when the base
    compose can't be parsed (graceful degradation — the start just proceeds
    without a pin). Always rewrites the override content so ``container_name`` and
    ``image`` track the current manifest, while reusing the recorded subnet for
    stability. ``force_realloc=True`` ignores the recorded subnet and picks a
    fresh free /24 (used by the start-time "pool overlaps" retry).
    """
    if deployment.current_mode() != deployment.MANAGED_LOCAL:
        return None
    srv = getattr(manifest, "server", None)
    if not _mt.is_container(srv):
        return None

    base_path = manifest.mcp_dir / srv.docker_compose
    if not base_path.is_file():
        return None
    try:
        base = yaml.safe_load(base_path.read_text()) or {}
        services = base.get("services")
        if not isinstance(services, dict) or not services:
            return None
        svc_key = _pick_target_service(services, srv.service_name or manifest.name)
    except Exception as e:
        logger.warning("T1 override: cannot parse %s base compose (%s)", manifest.name, e)
        return None

    override_path = manifest.mcp_dir / _OVERRIDE_NAME
    recorded = None if force_realloc else _read_recorded_subnet(override_path)

    project = f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}".lower()
    own_net = f"{project}_default"
    with _allocate_lock:
        used = _collect_used_subnets(exclude_network=own_net)
        subnet = allocate_mcp_subnet(config.OTODOCK_MCP_ADDRESS_POOL, used, recorded)

    svc: dict = {"container_name": project}
    if srv.image:
        svc["image"] = srv.image
    services_override: dict = {svc_key: svc}
    # Default resource bounds for every base service that declares none of its
    # own (compose overrides merge per-key, so an entry carrying only
    # mem_limit/logging never touches the base service's other keys).
    for key, base_svc in services.items():
        if not isinstance(base_svc, dict):
            continue
        extra = _missing_default_bounds(base_svc)
        if extra:
            services_override.setdefault(key, {}).update(extra)
    override: dict = {"services": services_override}
    if subnet:
        override["networks"] = {"default": {"ipam": {"config": [{"subnet": subnet}]}}}

    new_text = _T1_HEADER + yaml.safe_dump(
        override, sort_keys=False, default_flow_style=False,
    )
    old_text = override_path.read_text() if override_path.is_file() else None
    if old_text != new_text:
        override_path.write_text(new_text)
        logger.info(
            "T1 override %s: container=%s image=%s subnet=%s",
            manifest.name, project, srv.image or "(build)", subnet or "(auto)",
        )
    return override_path
