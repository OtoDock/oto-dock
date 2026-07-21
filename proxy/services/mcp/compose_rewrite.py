"""Rewrite a Docker MCP's ``docker-compose.yml`` for the T2 (Docker-Compose) topology.

In T2 the proxy is itself a container driving the host daemon through a
read-restricted ``docker-socket-proxy`` that **blocks ``docker build``** (BuildKit
→ 403, legacy build → 403). Community Docker MCPs ship a
``build: .`` compose (build-from-context), which therefore cannot be used as-is.
This module rewrites that compose to **pull a pre-built image** (``server.image``)
and run the container as a sibling on the platform's shared network, reachable by
service-DNS — the only shape a containerised proxy can actually launch.

The rewrite (T2 only — on bare-metal T1 the build-from-context compose is left
byte-for-byte untouched, so the live install is unaffected):

* drop ``build:`` (and any BuildKit ``additional_contexts``);
* set ``image:`` from the manifest's ``server.image``;
* join the external platform network (``config.OTODOCK_NETWORK``) with a network
  **alias = the MCP's service-DNS name** so ``http://<service>:<port>`` resolves
  from the proxy (and from local agents that share the proxy's netns);
* ``container_name: otodock-<install_id>-mcp-<name>`` — stable + collision-free;
* strip published host ``ports:`` (the MCP is reached over the shared network,
  not a host port; publishing one is needless attack surface and can collide);
* convert host **bind-mounts → named volumes** — a relative/absolute host path
  resolves on the *daemon host*, not inside the proxy container, so a bind-mount
  is meaningless in T2; named volumes already declared are kept as-is. The
  ``${HOST_AGENTS_DIR}`` agents-tree bind (the catalog contract for MCPs that
  work on agent files, e.g. video-tools) maps to the platform's **shared
  external agents volume** (``config.OTODOCK_AGENTS_VOLUME``) — the same volume
  the proxy and file-tools mount — never to a fresh empty per-MCP volume;
* inject default **memory bounds + log rotation** into services that declare
  none of their own (``_missing_default_bounds`` — the same defaults reach T1
  through the generated override), so a leaky community sidecar can't starve
  session admission or fill the disk.

It is **idempotent**: a compose already in pull form (no ``build:``) is left
unchanged, so it's safe to call on every install and every start. Comments are
not preserved — the installed compose is a generated runtime artifact; the
pristine source lives in the community catalog repo.
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

import yaml

import config
from core.config import deployment

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
    volumes, mcp_name: str, declared: set[str], external: set[str],
) -> list:
    """Convert host bind-mounts to per-MCP named volumes; keep named volumes.

    Mutates ``declared`` with any named volumes it creates and ``external``
    when an agents-tree mount is mapped onto the shared platform volume (the
    caller declares both at the compose top level). Handles both short-form
    (``"src:dst[:mode]"``) and long-form (``{type: bind, source, target}``)
    entries.
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
            if _is_host_bind_source(src):
                dst = parts[1] if len(parts) > 1 else src
                mode = parts[2] if len(parts) > 2 else ""
                name = f"otodock-{config.INSTALL_ID}-mcp-{_slug(mcp_name)}-{_slug(dst)}"
                declared.add(name)
                out.append(f"{name}:{dst}" + (f":{mode}" if mode else ""))
            else:
                if "${" in src:
                    logger.warning(
                        "compose_rewrite: %s volume %r has an interpolated source "
                        "this rewrite doesn't understand — left as-is (it will "
                        "resolve as a host bind on the daemon host)",
                        mcp_name, v,
                    )
                out.append(v)  # already a named volume
        elif isinstance(v, dict):
            src = str(v.get("source", ""))
            if v.get("type") == "bind" and _AGENTS_SRC_RE.match(src):
                external.add(agents_vol)
                ro = ":ro" if v.get("read_only") else ""
                out.append(f"{agents_vol}:{v.get('target', '')}{ro}")
            elif v.get("type") == "bind":
                dst = v.get("target", "")
                name = f"otodock-{config.INSTALL_ID}-mcp-{_slug(mcp_name)}-{_slug(dst)}"
                declared.add(name)
                out.append(f"{name}:{dst}")
            else:
                out.append(v)  # named-volume long-form or tmpfs — leave as-is
        else:
            out.append(v)
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
    shape that can't be safely rewritten for T2 (no services, a non-mapping
    service, or a multi-service compose where a *non-target* service also needs
    to be built — we can't pull an image we don't know).
    """
    data = copy.deepcopy(data)
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("compose has no `services` mapping")

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

    declared_volumes: set[str] = set()
    external_volumes: set[str] = set()
    for key, svc in services.items():
        if not isinstance(svc, dict):
            raise ValueError(f"service {key!r} is not a mapping")
        # Strip container-escape / host-access keys a community compose must
        # never set — privileged mode, host namespaces, raw devices, security-opt
        # downgrades, etc. A malicious catalog compose can't request them.
        dropped = [k for k in _DANGEROUS_COMPOSE_KEYS if k in svc]
        for k in dropped:
            svc.pop(k, None)
        if dropped:
            logger.warning(
                "compose_rewrite: stripped unsafe keys %s from service %r (%s)",
                dropped, key, mcp_name,
            )
        if key == target:
            svc.pop("build", None)
            svc["image"] = image
            svc["container_name"] = container_name
            svc.pop("ports", None)
            if "volumes" in svc:
                svc["volumes"] = _rewrite_volumes(
                    svc["volumes"], mcp_name, declared_volumes, external_volumes,
                )
            svc["networks"] = {_NET_KEY: {"aliases": [service_name]}}
        else:
            # Sibling services (already image-based) join the same network so
            # they can still talk to the target; no alias (only the target is
            # addressed by the proxy via service-DNS).
            svc["networks"] = {_NET_KEY: {}}
        svc.update(_missing_default_bounds(svc))

    data["networks"] = {_NET_KEY: {"external": True, "name": network_name}}
    _declare_volumes(data, declared_volumes, external_volumes)
    return data


_HEADER = (
    "# AUTO-GENERATED for the Docker-Compose (T2) topology — DO NOT EDIT.\n"
    "# The containerised proxy drives the daemon through a docker-socket-proxy\n"
    "# that blocks `docker build`, so the catalog's build-from-context compose\n"
    "# was rewritten to PULL the pre-built image and join the shared network.\n"
    "# Source of truth = the catalog repo + services/mcp/compose_rewrite.py.\n"
)


def ensure_pull_compose(manifest) -> bool:
    """Idempotently rewrite a Docker MCP's compose to pull form — **T2 only**.

    Returns ``True`` if the file was rewritten, ``False`` when no change is
    needed (bare-metal T1, a non-docker MCP, or a compose already in pull form).
    Raises ``ValueError`` with an actionable message when the MCP genuinely
    cannot run in T2 — no ``server.image`` (can't pull, can't build), a missing
    compose file, or an un-rewritable shape.
    """
    if not deployment.in_docker_compose():
        return False  # T1 / T3 — build-from-context compose stays untouched
    srv = getattr(manifest, "server", None)
    if srv is None or getattr(srv, "runtime", "") != "docker":
        return False

    compose_path = manifest.mcp_dir / srv.docker_compose
    if not compose_path.is_file():
        raise ValueError(
            f"{manifest.name}: compose file {srv.docker_compose!r} not found "
            f"in {manifest.mcp_dir}"
        )

    data = yaml.safe_load(compose_path.read_text()) or {}
    services = data.get("services") or {}
    has_build = any(isinstance(s, dict) and "build" in s for s in services.values())
    if not has_build:
        # Already pull-form — heal what a stale generated artifact can carry:
        # (a) a container_name stamped by a PREVIOUS install identity. The
        #     install-id lives in config.env, so a recreated config.env
        #     (install moved to a new folder) rotates it and a stale stamp
        #     makes every `up` collide with the previous generation's
        #     container ("name already in use" → opaque 500 on start, forever).
        # (b) an agents-tree bind (`${HOST_AGENTS_DIR}` source) left in place
        #     by a rewrite that predates the shared-volume mapping — on T2 it
        #     binds an empty host dir, so the MCP sees no agent files.
        expected = f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}"
        try:
            target = _pick_target_service(
                services, deployment.mcp_service_name(manifest),
            )
        except ValueError:
            return False  # unrecognizable shape — leave it alone
        svc = services.get(target)
        if not isinstance(svc, dict):
            return False
        changes: list[str] = []
        current = svc.get("container_name")
        if (
            isinstance(current, str)
            and current.startswith("otodock-")
            and "-mcp-" in current
            and current != expected
        ):
            svc["container_name"] = expected
            changes.append(
                f"container_name {current} → {expected} (install identity changed)"
            )
        if isinstance(svc.get("volumes"), list):
            declared: set[str] = set()
            external: set[str] = set()
            healed = _rewrite_volumes(
                svc["volumes"], manifest.name, declared, external,
            )
            if healed != svc["volumes"]:
                svc["volumes"] = healed
                _declare_volumes(data, declared, external)
                changes.append("host binds → named volumes")
        if not changes:
            return False  # already pull-form and healthy — idempotent
        compose_path.write_text(
            _HEADER + yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
        )
        logger.info(
            "Healed %s pull-form compose: %s", manifest.name, "; ".join(changes),
        )
        return True

    if not srv.image:
        raise ValueError(
            f"{manifest.name}: this Docker MCP has no pre-built image "
            f"(server.image is unset) — it cannot be installed in Docker-Compose "
            f"mode because the docker-socket-proxy blocks `docker build`. Add "
            f"server.image to its manifest, or run it on a bare-metal proxy."
        )

    new_data = transform_compose_dict(
        data,
        image=srv.image,
        service_name=deployment.mcp_service_name(manifest),
        container_name=f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}",
        network_name=config.OTODOCK_NETWORK,
        mcp_name=manifest.name,
    )
    compose_path.write_text(
        _HEADER + yaml.safe_dump(new_data, sort_keys=False, default_flow_style=False)
    )
    logger.info(
        "Rewrote %s compose → pull form (image=%s, net=%s)",
        manifest.name, srv.image, config.OTODOCK_NETWORK,
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
    if srv is None or getattr(srv, "runtime", "") != "docker":
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
