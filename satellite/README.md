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
| `__main__.py`, `config.py`, `engines.py`, `background.py` | `__main__.py` is the entry point and boot guard. `config.py` holds the host OS table (`config.HOST`), `SATELLITE_VERSION` and the vendored hashes. `engines.py` holds the `ENGINES` table. `background.py` holds `spawn(coro, name=, owner=)`, the one way to start a task nobody keeps: held until it ends, a death handed to the loop's exception handler under `owner:name`. |
| `transport/` | the wire: `ws_client`, `lifecycle_update` (auto-update), `http_tunnel`, `mcp_gateway`, `file_sync` |
| `sessions/` | engine sessions: `session_manager`, `cli_session`, `codex_session`, `alive_report`, `session_files`, `session_move` (a chat's session files moved on and off this machine, the `session_move` frame), `step_runner` (app steps and script checks, the `step_run` frame), `mcp_install_support` (MCP venvs on the satellite's own Python, the boot's reconcile, a failed install's cause), `mcp_interceptor` |
| `terminal/` | interactive PTYs and the local `otodock` command: `local_socket`, `otodock_cli`, `otodock_proto`, `pty_session_base`, `pty_session`, `codex_pty_session`, `pty_relay`, `winpty_relay` |
| `host/` | the machine: `host_probe`, `satellite_policy`, `auth_paths`, `safe_fs` (file opens beneath the agents root with no link followed), `env_hygiene`, `path_translator`, `cli_versions` (the CLI pins, and the npm and Mac tool folders put on `PATH` at start), `session_retention` (the retention sweep's keep list applied to the transcripts here), `interpreters` (Git Bash, its `env.exe` and the install's Python on Windows, for a script's first line), `tray`, `service_unit`, `macos_app` (`OtoDock Satellite.app`, the Mac's login item, which `install.sh` builds by running it as a script) |
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
- A Mac paired before 1.9.1 finds its CLIs again after the update alone:
  from 0.5.160 the satellite puts Homebrew's and your own tool folders in
  front of its `PATH` at start. Homebrew's `python@3.13`, the login item's
  `PATH` and `OtoDock Satellite.app` reach it only when you pair it again.
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
  Linux, Python 3.12 through winget on Windows. On macOS it takes the
  platform's own minor, Homebrew's `python@3.13`, and builds the satellite's
  venv on `$(brew --prefix)/opt/python@3.13/bin/python3.13`. It installs
  Homebrew first when it finds none (on `PATH`, at `/opt/homebrew/bin/brew`
  or at `/usr/local/bin/brew`): that needs an administrator account, whose
  password it asks for once, and brings Apple's Command Line Tools. With
  Homebrew there and no Command Line Tools, the installer stops and asks
  you to run `xcode-select --install`. Without `python@3.13` it warns and
  builds on Homebrew's newest Python, where tools written in Python may not
  install. From 0.5.160 the satellite builds every MCP's venv on its own
  Python, since Homebrew's plain `python3` moves to a newer minor that no
  compiled package serves yet. Auto-update keeps the interpreter the
  venv was built on.
- macOS, for browser control: Google Chrome (or Edge, Brave, Chromium):
  `brew install --cask google-chrome`, then open it once by hand, since the
  first launch of a downloaded app asks whether to open it.
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

On a Mac run the install command as yourself: it refuses root, as Homebrew
does, and the toolchain comes from Homebrew. npm and the git credential
helper need no `sudo` in Homebrew's folder. What an older installer's
`sudo npm` left owned by root (`~/.npm`, the CLI packages and their links
in Homebrew's folder) is given back to you.

> **Windows: Python "App execution aliases".** On a fresh Windows 10 or 11
> without a real Python, `python.exe` and `python3.exe` under
> `%LOCALAPPDATA%\Microsoft\WindowsApps\` are Microsoft Store redirect
> stubs. The installer probes for a real interpreter, so winget still
> installs Python 3.12. If a run cannot find Python anyway (a real install
> shadowed by the alias), turn the aliases off: **Settings, Apps, Advanced
> app settings, App execution aliases**, switch off `python.exe` and
> `python3.exe`, then run the installer again.

### macOS permissions

macOS asks before the satellite may see the screen, use the mouse and
keyboard, or read some folders. Each question names OtoDock Satellite and
comes once. Your answer stays for the app.

- The first time an agent uses computer control, macOS asks for
  Accessibility and for Screen & System Audio Recording. On macOS 27 the
  first list is called Device Control and Data Access. Before macOS 15 the
  second is called Screen Recording.
- On macOS 27 the first screenshot also asks whether OtoDock Satellite may
  bypass the system private window picker. Screenshots work while it waits.
- The first time a session reads your Documents, Desktop or Downloads
  folder, macOS asks for that folder. A network or external drive asks the
  same way. The command waits until you answer.
- To skip the folder questions, turn on OtoDock Satellite once under Full
  Disk Access in System Settings, Privacy & Security.
- When a tool controls another app on the Mac, macOS asks once for each app,
  under Automation. Browser control does not ask.
- A switch you turn on reaches the next chat you start on this computer. A
  chat that is already open keeps the old answer. If macOS offers Quit &
  Reopen, choose Later, because quitting the satellite ends its chats.

Windows and Linux ask none of these.

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
otodock claude <agent> --mode ask       # start in Ask (ask|auto|dontAsk|plan, default auto)
```

`--mode` picks the session's permission mode: `ask`, `auto`, `dontAsk` or
`plan`, `auto` when it is left out. The older words `default` and
`acceptEdits` are still taken, as Ask and Auto.

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
├── OtoDock Satellite.app/    # macOS: the login item's launcher (host/macos_app.py)
├── .satellite-app.sha256     # macOS: the app's stamp. A match keeps the app and its permissions
├── steps/                    # app steps' scratch: one 0700 folder per running step, never synced
├── run/                      # the local control socket of the otodock command
├── agents/                   # agent folders, synced with the platform
│   └── {agent-slug}/
│       ├── config/           # agent config (the platform's, pushed)
│       ├── workspace/        # the agent's shared workspace (both ways)
│       ├── .session-move/    # a chat move's staged session files, never synced
│       └── users/
│           └── {username}/
│               ├── workspace/    # the person's workspace (both ways)
│               ├── context/      # the person's context documents (pushed)
│               ├── .claude/      # Claude Code session data (moves with a chat)
│               └── .codex/       # Codex session data (moves with a chat)
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
  (`KeepAlive`, `RunAtLoad`), started in your desktop session (`launchctl
  bootstrap gui/<uid>`, also when you pair over SSH). Its `PATH` names
  Homebrew's and your own tool folders, since launchd gives a login item
  only the system ones. It runs `~/.oto-dock/OtoDock Satellite.app`, a small
  app the installer builds and signs on the Mac (ad hoc, as
  `com.otodock.satellite`), which starts the satellite and stays its
  parent. macOS therefore lists the satellite as **OtoDock Satellite** in
  System Settings, Privacy & Security: turn it on under Accessibility
  (named Device Control and Data Access from macOS 27) and Screen & System
  Audio Recording for computer control. Computer control asks for both on
  its first use. After turning them on, start a new chat on the machine.
  With no one signed in at the Mac's desktop the installer starts the
  satellite in the background (`launchctl load`): computer control and the
  permission questions wait for a desktop sign-in. A Mac without the
  Command Line Tools or `codesign` runs the satellite without the app, and
  macOS lists its permissions under Python. Running the install command
  again over an existing install keeps the app and its permissions.
  Removing the machine and pairing it again clears both: macOS asks again.
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
(code, venv, agents, MCPs, sessions, config and the credentials synced down),
and the `~/.local/bin/otodock` link when it points into that folder. On
macOS it also clears the two permissions macOS gave OtoDock Satellite.
The toolchain the pairing installed (git, gh, Python, Node, pnpm, uv, jq,
ripgrep, poppler-utils, sqlite3, the Claude Code and Codex npm globals,
and on macOS Homebrew itself, `python@3.13` and the git credential helper
in Homebrew's `bin`) **stays**: the installer skipped what was already
there and keeps no record of what it added, and removing it blindly could
break other software. The scripts print the `winget`, `apt` or `brew`
commands for a full cleanup.

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
