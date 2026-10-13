# OtoDock Satellite

The satellite is the daemon that runs on a remote Linux, macOS or Windows
machine and hosts OtoDock agent sessions there. It keeps one **outbound**
websocket to the platform (`wss://<public-host>/v1/satellite`), receives
commands, runs Claude Code and Codex sessions as direct subprocesses of its
own user, and streams their events back. Hook callbacks, MCP traffic and the
file sync ride that same websocket: the machine opens no port and needs no
VPN.

## How it works

```
Platform (proxy)                    Satellite (remote machine)
┌──────────────┐                   ┌──────────────────────────┐
│ remote       │◄══ single WSS ═══►│ SatelliteWSClient        │
│ execution    │   (outbound only) │   ├── SessionManager      │
│ layer        │                   │   │   ├── CLISession      │
│              │                   │   │   │   └── claude -p   │
│ stream pump  │                   │   │   └── CodexSession    │
│ hook and MCP │◄ http_* frames ──│   ├── LocalTunnelServer   │
│ handlers     │   (over the WS)   │   │   (127.0.0.1 aiohttp) │
│              │                   │   └── FileSync            │
└──────────────┘                   └──────────────────────────┘
```

- **A dumb pipe.** Engine output goes to the platform verbatim. Every
  decision about a turn is the platform's.
- **No sandbox here.** Sessions run on the host filesystem as the
  satellite's user. The platform's permission gate decides what they may
  touch, and the satellite checks host paths again itself.
- **Hooks ship with each session**, so the machine always runs the
  platform's current hook scripts.
- **Agent folders sync** both ways in real time, and build folders stay local.
- **Versioned.** The platform updates a satellite older than its own copy
  over the websocket. The update ends the sessions the machine runs.

The wire (every frame, the capability gates, the sessions, file sync, MCP
installs, the version history) is in [`PROTOCOL.md`](PROTOCOL.md).

### Package layout

Run with `python -m satellite`. The subpackages each own one concern.

| Path | Responsibility |
|---|---|
| `__main__.py`, `config.py`, `engines.py` | `__main__.py` is the entry point and boot guard. `config.py` holds the host OS table (`config.HOST`), `SATELLITE_VERSION` and the vendored hashes. `engines.py` holds the `ENGINES` table. |
| `transport/` | the wire: `ws_client`, `lifecycle_update` (auto-update), `http_tunnel`, `mcp_gateway`, `file_sync` |
| `sessions/` | engine sessions: `session_manager`, `cli_session`, `codex_session`, `alive_report`, `session_files`, `step_runner` (app steps), `mcp_install_support`, `mcp_interceptor` |
| `terminal/` | interactive PTYs and the local `otodock` command: `local_socket`, `otodock_cli`, `otodock_proto`, `pty_session_base`, `pty_session`, `codex_pty_session`, `pty_relay`, `winpty_relay` |
| `host/` | the machine: `host_probe`, `satellite_policy`, `auth_paths`, `safe_fs` (file opens beneath the agents root with no link followed), `env_hygiene`, `path_translator`, `cli_versions`, `tray`, `service_unit` |
| `_vendored/` | byte-for-byte copies of proxy modules. Never edit them (see "Development"). |

`__main__` imports only `config` at module top, so the boot guard can roll a
broken auto-update back before any subpackage import is attempted.

### Where sessions run

An admin pairs a machine and assigns agents to it (Admin, Remote Machines):
every user of those agents runs there. A user may also pair a machine of
their own (User Settings, Remote Machines) and make it their active target,
unless an admin turned personal pairing off. A user's target wins over the
agent's, which wins over the platform itself. When a user's machine is
offline the session falls back to the agent's target (the
`remote_fallback_user_override` setting, on by default).

### Updates

- The platform updates a satellite older than its own copy over the
  websocket when it reconnects, without waiting for a running turn: the
  update ends the sessions the machine runs, and they stop mid-turn.
- The Claude Code and Codex CLIs follow the platform's pins at every
  reconnect. Node, uv and pnpm move only when the install command runs
  again, and an update never changes the machine's Python.
- On an opted-in plaintext link (`allow_insecure_transport`) the satellite
  refuses updates and uninstalls from the platform: run the installer again
  to update it.
- A satellite below the platform's protocol floor (`MIN_SATELLITE_VERSION`)
  with auto-update turned off is refused with an upgrade error.
- A permission prompt raised by the engine itself is answered through a
  person only for a satellite at 0.5.140 or later. The platform refuses it at
  once for an older one.
- git and `gh` on a machine fetch the connected GitHub account's token per
  command from 0.5.139. On an older satellite git refuses with a message that
  names the update and `gh` runs as the plain command. A credential the
  platform revokes stops on the machine within seconds, and from 0.5.140 the
  next call says why.

## Prerequisites

- Python 3.10 or newer (`PYTHON_MIN_VERSION` in `VERSIONS.md`). The pairing
  installer provisions one where it is missing: the distro's `python3` on
  Linux, brew `python` on macOS, Python 3.12 through winget on Windows.
  Auto-update keeps the interpreter the venv was built on.
- Network egress to the platform's public `wss://` endpoint.
- Claude Code and/or Codex: the pairing installer installs them, with the
  rest of the developer toolchain.

## Install and pair

Pair the machine in the dashboard (Admin, Remote Machines, or User Settings,
Remote Machines). The dialog shows an install command to run on the machine:

```bash
# Linux / macOS: the token rides a header, not the URL
bash <(curl -sL -H "X-Pairing-Token: <token>" "https://<public-host>/v1/satellite/bootstrap?os=linux")
```

`/v1/satellite/bootstrap` returns a self-extracting script (bash, or
PowerShell for Windows). It installs the developer toolchain through
`scripts/install-baseline-tools.sh` (git, gh, Python with uv, Node with npm
and pnpm, jq, ripgrep, poppler-utils, sqlite3, ...) and the Claude and Codex
CLIs, exchanges the pairing token for the machine secret, writes the config
and registers a **per-user** service (a systemd user unit with
`loginctl enable-linger`, a launchd LaunchAgent, or a Windows logon
Scheduled Task). Nothing runs as root or SYSTEM.

> **Windows: Python "App execution aliases".** On a fresh Windows 10 or 11
> without a real Python, `python.exe` and `python3.exe` under
> `%LOCALAPPDATA%\Microsoft\WindowsApps\` are Microsoft Store redirect
> stubs. The installer probes for a real interpreter, so winget still
> installs Python 3.12. If a run cannot find Python anyway (a real install
> shadowed by the alias), turn the aliases off: **Settings, Apps, Advanced
> app settings, App execution aliases**, switch off `python.exe` and
> `python3.exe`, then run the installer again.

### Manual install

```bash
mkdir -p ~/.oto-dock
cp -r satellite/ ~/.oto-dock/satellite/          # or the tarball from the platform
cd ~/.oto-dock/satellite
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # the lockfile

cat > ~/.oto-dock/satellite.conf << 'EOF'
[satellite]
machine_id = <from pairing>
machine_secret = <from the pairing exchange>
platform_url = wss://<public-host>/v1/satellite
agents_dir = ~/.oto-dock/agents
mcps_dir = ~/.oto-dock/mcps

[cli]
claude_bin = claude

[codex]
codex_bin = codex
EOF

venv/bin/python -m satellite
```

## The `otodock` command

On the machine, the bundled `otodock` command (`bin/otodock` linked onto the
`PATH`, `otodock.cmd` on Windows) starts an interactive session as one of the
machine's agents in the current folder. The chat syncs to the dashboard,
which can take it over:

```bash
otodock claude <agent>                  # an interactive Claude session as <agent>, here
otodock codex  <agent> --folder /path   # Codex, in another folder
otodock claude <agent> --resume         # pick a chat to resume first
```

## Configuration

`~/.oto-dock/satellite.conf` (INI):

| Section | Key | Description |
|---------|-----|-------------|
| `[satellite]` | `machine_id` | the id assigned at pairing |
| | `machine_secret` | the secret from the token exchange |
| | `platform_url` | the platform's websocket URL |
| | `agents_dir` | the local agent folders (default `~/.oto-dock/agents`) |
| | `mcps_dir` | the local MCP servers (default `~/.oto-dock/mcps`) |
| | `allow_insecure_transport` | `true` lets a plain `ws://` URL reach a public host (a split-horizon self-host). Such a link refuses updates and uninstalls (`PROTOCOL.md`, "Versions"). |
| `[cli]` | `claude_bin` | a Claude Code CLI hint (default `claude`). When the platform ships CLI pins, the binary that satisfies the pin is what spawns. With no pin the hint decides. |
| `[codex]` | `codex_bin` | a Codex CLI hint (default `codex`), resolved the same way |

The hint keys are the `conf_section` / `conf_key` of each engine's row in
`engines.py`: a new engine adds a row, not a field.

## What is on the machine

```
~/.oto-dock/
├── satellite.conf            # the daemon's configuration
├── satellite/                # the daemon's code and venv
├── steps/                    # app steps' scratch: one 0700 folder per running step, never synced
├── run/                      # the local control socket of the otodock command
├── agents/                   # agent folders, synced with the platform
│   └── {agent-slug}/
│       ├── config/           # agent config (the platform's, pushed)
│       ├── workspace/        # the agent's shared workspace (both ways)
│       └── users/
│           └── {username}/
│               ├── workspace/    # the person's workspace (both ways)
│               ├── context/      # the person's context documents (pushed)
│               ├── .claude/      # Claude Code session data
│               └── .codex/       # Codex session data
└── mcps/                     # installed stdio MCP servers
    ├── custom/{mcp-name}/    #   manifest.json and venv/ or node_modules/
    └── community/{mcp-name}/
```

On Windows the install folder is `%USERPROFILE%\OtoDock\`.

## Running as a service

The installer registers the service. The definitions below are for
reference.

- **Linux:** `~/.config/systemd/user/oto-dock-satellite.service`, started
  with `systemctl --user enable --now oto-dock-satellite`. Booting without a
  login needs `loginctl enable-linger <user>` once. `OOMPolicy=continue`
  keeps the unit and its sessions alive when the kernel's OOM killer ends
  one child (a headless browser, say). An older unit gets the line at
  daemon start as the drop-in `oto-dock-satellite.service.d/oom-policy.conf`,
  only when the unit is this install's own (`host/service_unit.is_own_unit`
  compares its `WorkingDirectory`: one OS user may run several satellites).
- **macOS:** the LaunchAgent `~/Library/LaunchAgents/com.otodock.satellite.plist`
  (`KeepAlive`, `RunAtLoad`).
- **Windows:** the per-user logon task `OtoDockSatellite` (no admin rights)
  and an HKCU Add/Remove Programs entry.
  `Get-ScheduledTask OtoDockSatellite | Get-ScheduledTaskInfo` inspects it.

```ini
[Unit]
Description=Oto Dock Satellite Daemon

[Service]
Type=simple
ExecStart=/home/<user>/.oto-dock/satellite/venv/bin/python -m satellite
WorkingDirectory=/home/<user>/.oto-dock/satellite
Restart=always
RestartSec=5
OOMPolicy=continue
Environment=HOME=/home/<user>

[Install]
WantedBy=default.target
```

## Uninstall

- **From the dashboard:** deleting the machine (Remote Machines) sends the
  satellite `uninstall`. It runs `uninstall.sh` or `uninstall.ps1` and exits,
  and the platform drops the machine's records. Without the script the
  daemon cleans up itself, removing the systemd unit only when the unit
  runs this install.
- **On the machine:** `bash uninstall.sh` (Linux, macOS) or
  `powershell -ExecutionPolicy Bypass -File uninstall.ps1` (Windows). The
  script also tells the platform, so the dashboard entry disappears. On
  Linux it removes the `oto-dock-satellite` unit only when the unit's
  `WorkingDirectory` is this install's `satellite/` folder, or the unit is
  absent.

The uninstall removes the service registration and the whole install folder
(code, venv, agents, MCPs, sessions, config and the credentials synced down).
The toolchain the pairing installed (git, gh, Python, Node, pnpm, uv, jq,
ripgrep, poppler-utils, sqlite3, the Claude Code and Codex npm globals)
**stays**: the installer skipped what was already there and keeps no record
of what it added, and removing it blindly could break other software. The
scripts print the `winget`, `apt` or `brew` commands for a full cleanup.

## Development

```bash
python3 -m venv satellite/venv && satellite/venv/bin/pip install -r satellite/requirements.txt -r proxy/requirements-test.txt   # once
cd satellite && venv/bin/python -m pytest
```

- The suite also runs at the host floor (Python 3.10, CI's `satellite-floor`
  job): `requirements.txt` carries `tomli` below 3.11, and the tests that
  parse a composed `config.toml` go through `tests/_toml.py`.
  `tests/test_python_floor.py` pins every site that states the floor, this
  README's Prerequisites included.
- `_vendored/` holds copies of proxy modules, pinned by hash in
  `config.py`: edit the proxy source and run `scripts/sync-satellite-code.sh`,
  never the copy (`PROTOCOL.md`, "Vendored modules").
- Every change to the satellite's code bumps `SATELLITE_VERSION` in
  `config.py`: that is what makes paired machines update.
- On a machine that also runs the platform, link the platform's MCPs to skip
  the install round trip:
  `ln -sfn /path/to/oto-dock/mcps/custom ~/.oto-dock/mcps/custom` (and the
  same for `community`).

User documentation for remote machines is at
[docs.otodock.io](https://docs.otodock.io).
