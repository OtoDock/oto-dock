"""The folders the proxy and the compute service must share, compared.

``scripts/upgrade.sh`` feeds it ``docker compose config --format json`` of
the files the update runs (the base, the phone file and a kept override) and
runs it with the Python of the install's own server image, before anything
changes (the pulled image's when no server container is there). It prints
each shared folder whose source differs between ``otodock-proxy`` and
``otodock-compute``, each mount the proxy has inside a shared folder that
the compute service lacks (a disk bound into the agents folder by an
override file), and the mount of an engine's binary .env names
(``CLAUDE_BIN``, ``CODEX_BIN``) that the proxy has and the compute service,
which runs the sessions, lacks, one per line. Then, as ``proxy: <item>``,
each thing that hands ``otodock-proxy`` a privilege of the sandbox, item by
item (``SANDBOX_OPTIONS``, the tun device, ``cap_add``, ``privileged``): the
proxy starts no sandbox, so only an override sets one. An option of the
admin's own (a seccomp file, another AppArmor profile,
``no-new-privileges``, an SELinux label, another device) is never named:
the update tells the person the named lines can go. Nothing printed: the
two services see the same folders and the proxy holds no such item. Exit 2
when the input is not a compose configuration with both services. Standard
library only: the update hands this text to another release's Python.
"""

from __future__ import annotations

import json
import sys

#: The community tools are not here: the proxy keeps their sources in its
#: own volume and the compute service builds them in another, at one path.
SHARED = ("/opt/otodock/agents", "/opt/otodock/mcps/skills")
#: The engines' binaries, as the compose file passes them to the compute service.
ENGINE_BINARIES = ("CLAUDE_BIN", "CODEX_BIN")
#: The security options the releases before 2.0 gave ``otodock-proxy`` for
#: the sandbox, the scoped AppArmor profile in place of ``unconfined`` among them.
SANDBOX_OPTIONS = ("seccomp=unconfined", "apparmor=unconfined", "apparmor=otodock_userns", "systempaths=unconfined")
#: The device pasta opens its tap through.
TUN = "/dev/net/tun"


def _sources(service: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for mount in service.get("volumes") or []:
        if isinstance(mount, dict) and mount.get("target"):
            out[str(mount["target"]).rstrip("/")] = str(mount.get("source") or "").rstrip("/")
    return out


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env if isinstance(item, str) and "=" in item)
    return {str(k): str(v or "") for k, v in env.items()}


def differences(config: dict) -> list[str]:
    services = config.get("services") or {}
    proxy, compute = _sources(services["otodock-proxy"]), _sources(services["otodock-compute"])
    out = [target for target in SHARED if proxy.get(target) != compute.get(target)]
    for target, source in sorted(proxy.items()):
        nested = any(target.startswith(shared + "/") for shared in SHARED)
        if nested and compute.get(target) != source:
            out.append(target)
    env = _environment(services["otodock-compute"])
    for name in ENGINE_BINARIES:
        path = env.get(name, "").strip().rstrip("/")
        holders = [t for t in proxy if path.startswith("/") and (path == t or path.startswith(t + "/"))]
        # A binary in the image is the same for both services.
        target = max(holders, key=len) if holders else ""
        if target and compute.get(target) != proxy[target] and target not in out:
            out.append(target)
    return out


def privileges(config: dict) -> list[str]:
    proxy = config["services"]["otodock-proxy"]
    # ``seccomp:unconfined`` is Docker's older spelling of ``seccomp=unconfined``.
    options = {opt if "=" in opt else opt.replace(":", "=", 1)
               for opt in (str(o).strip() for o in proxy.get("security_opt") or [])}
    held = [opt for opt in SANDBOX_OPTIONS if opt in options]
    sources = [d.get("source") if isinstance(d, dict) else str(d).split(":")[0] for d in proxy.get("devices") or []]
    if TUN in sources:
        held.append(TUN)
    if proxy.get("cap_add"):
        held.append("cap_add")
    if proxy.get("privileged") is True:
        held.append("privileged")
    return [f"proxy: {item}" for item in held]


def main() -> int:
    try:
        config = json.load(sys.stdin)
        found = differences(config) + privileges(config)
    except (ValueError, KeyError, TypeError, AttributeError):
        return 2
    for target in found:
        print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
