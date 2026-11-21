"""The sandbox of an app server (APPS.md "The app sandbox").

An app's server runs on a compute machine, in the sandbox that machine
renders from the table built here: pasta outside, bwrap inside, no
capabilities, and its own mount table: the release copy read-only at
``/app``, the app's data directory read-write at ``/app/data``, the
machine's own Bun, the system directories, a private ``/tmp``. No
workspace, no users, no config, no MCP directories. The network is the
proxy port (``-T``), one inbound splice from a loopback port of the machine
to the server's port, DNS, and the addresses the approved egress hosts
resolved to at launch as ``/32`` carves. Private ranges and the rest of the
public internet are closed. A carve opens an address, not a name: any other
site served from the same address (a shared CDN edge) is reachable too, and
DNS answers any name. The egress deny keeps the LAN and the platform out of
an app's reach. It is not a boundary against the app's own code sending data
out (route-only HTTPS egress is a roadmap item).
"""

from __future__ import annotations

import os
from pathlib import Path

import config as app_config
from core import layout
from core.sandbox.sandbox import Mount, SandboxConfig

# Inside the namespace the server always listens here. The machine's side of
# the splice is a fresh port per start, which the machine picks.
APP_PORT = 3000
SERVER_ENTRIES = ("server/index.ts", "server/index.js")


class AppStartError(Exception):
    """The server cannot be launched as configured (no Bun, no entry)."""


def server_entry(release_dir: Path) -> str:
    """The server's entry file relative to the release, "" for a folder app
    without a server (client only)."""
    for rel in SERVER_ENTRIES:
        if (release_dir / rel).is_file():
            return rel
    return ""


def build_config(row: dict, release_dir: Path, data_dir: Path,
                 allow_hosts: list[str] | None = None, *, port: int = APP_PORT) -> SandboxConfig:
    """The table a compute machine renders. Its sources are resolved, so the
    description spells them below the agent's folder (the only root a
    machine binds from). The machine uses its own Bun and picks its side of
    the inbound splice."""
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
        app_mounts=[
            Mount(os.path.realpath(release_dir), "/app", False),
            Mount(os.path.realpath(data_dir), "/app/data", True),
        ],
        app_cwd="/app",
        net_inbound=f"0:{port}",
        net_egress_deny=True,
    )


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
        # Bun answers an uncaught error with a plain 500, never its
        # development page (the message, the paths and the source lines).
        "NODE_ENV": "production",
    }
    if preview:
        env["OTODOCK_PREVIEW"] = "1"
    for name, value in (secrets or {}).items():
        if name not in env:
            env[name] = value
    return env
