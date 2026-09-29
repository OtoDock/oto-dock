"""The sandbox of an app server (APPS.md "The app sandbox").

An app's server runs on the platform host like an agent's tools do — pasta
outside, bwrap inside, no capabilities — but with its own mount table: the
release copy read-only at ``/app``, the app's data directory read-write at
``/app/data``, the Bun binary, the system directories, a private ``/tmp``.
No workspace, no users, no config, no MCP directories. The network is the
proxy port (``-T``), one inbound splice from a host loopback port to the
server's port, DNS, and the addresses the approved egress hosts resolved to
at launch as ``/32`` carves; private ranges and the rest of the public
internet are closed. A carve opens an address, not a name: any other site
served from the same address (a shared CDN edge) is reachable too, and DNS
answers any name. The egress deny keeps the LAN and the platform out of an
app's reach; it is not a boundary against the app's own code sending data
out (route-only HTTPS egress is a roadmap item).
"""

from __future__ import annotations

import os
from pathlib import Path

import config as app_config
from core.sandbox.sandbox import Mount, SandboxBuilder, SandboxConfig
from core import layout

# Inside the namespace the server always listens here; the host side of the
# splice is a fresh port per start (the supervisor picks it).
APP_PORT = 3000
# Where a Bun binary that is not under /usr is bound (bwrap's root is its
# own tmpfs, so a new path there costs nothing; a path under the read-only
# /usr bind could not be created).
BUN_SANDBOX_PATH = "/opt/otodock/bin/bun"
SERVER_ENTRIES = ("server/index.ts", "server/index.js")


class AppStartError(Exception):
    """The server cannot be launched as configured (no Bun, no entry)."""


def bun_binary() -> str:
    """The Bun binary on the host, or "" when the install has none."""
    path = (app_config.BUN_BIN or "").strip()
    return path if path and os.access(path, os.X_OK) else ""


def server_entry(release_dir: Path) -> str:
    """The server's entry file relative to the release, "" for a folder app
    without a server (client only)."""
    for rel in SERVER_ENTRIES:
        if (release_dir / rel).is_file():
            return rel
    return ""


def sandbox_bun_path(host_bun: str) -> str:
    return host_bun if host_bun.startswith("/usr/") else BUN_SANDBOX_PATH


def build_config(row: dict, release_dir: Path, data_dir: Path, host_port: int,
                 allow_hosts: list[str] | None = None, *, port: int = APP_PORT) -> SandboxConfig:
    host_bun = bun_binary()
    if not host_bun:
        raise AppStartError("Bun is not installed on the platform host (BUN_BIN)")
    mounts = [
        Mount(str(release_dir), "/app", False),
        Mount(str(data_dir), "/app/data", True),
    ]
    if sandbox_bun_path(host_bun) != host_bun:
        mounts.append(Mount(host_bun, BUN_SANDBOX_PATH, False))
    agents_dir = Path(app_config.AGENTS_DIR).resolve()
    return SandboxConfig(
        role="manager",
        username="",
        agent_name=row["agent"],
        is_admin_agent=False,
        host_agents_dir=agents_dir,
        host_mcps_dir=Path(app_config.MCPS_DIR).resolve(),
        host_claude_dir=agents_dir / row["agent"] / layout.WORKSPACE / ".claude",
        config_visible=False,
        mount_shared=False,
        net_forwards=[str(app_config.PORT)],
        net_allow_hosts=list(allow_hosts or []),
        app_mounts=mounts,
        app_cwd="/app",
        net_inbound=f"{host_port}:{port}",
        net_egress_deny=True,
    )


def build_command(row: dict, release_dir: Path, data_dir: Path, host_port: int,
                  entry: str, allow_hosts: list[str] | None = None, *,
                  port: int = APP_PORT) -> list[str]:
    """The full argv: the netns launcher, bwrap with the app table, Bun
    running the entry from ``/app``."""
    cfg = build_config(row, release_dir, data_dir, host_port, allow_hosts, port=port)
    bun = sandbox_bun_path(bun_binary())
    return SandboxBuilder(cfg).build_command_prefix([bun, "run", entry])


def build_env(row: dict, token: str, public_key: str, *, port: int = APP_PORT,
              preview: bool = False, secrets: dict[str, str] | None = None) -> dict[str, str]:
    """The child's environment: nothing of the proxy's own, only what the
    server needs (APPS.md lists these for authors). ``secrets`` are the
    app's ``env`` secrets (APPS.md "Secrets"), merged for the live instance
    only (the caller never passes them for a preview or a check), and
    never a name the runtime owns (the validator refuses those)."""
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/otodock/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "PORT": str(port),
        "OTODOCK_APP_ID": row["id"],
        "OTODOCK_APP_SLUG": row["slug"],
        "OTODOCK_APP_TOKEN": token,
        "OTODOCK_APP_PUBLIC_KEY": public_key,
        "OTODOCK_PROXY_URL": f"http://127.0.0.1:{app_config.PORT}",
        "OTODOCK_DATA_DIR": "/app/data",
    }
    if preview:
        env["OTODOCK_PREVIEW"] = "1"
    for name, value in (secrets or {}).items():
        if name not in env:
            env[name] = value
    return env
