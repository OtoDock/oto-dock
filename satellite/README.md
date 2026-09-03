# Oto Dock Satellite Daemon

Standalone Python daemon that runs on remote Linux, macOS, and Windows machines to host AI agent sessions for the Oto Dock platform. It makes a single **outbound** WebSocket connection to the platform (`wss://<public-host>/v1/satellite`), receives commands, spawns Claude Code CLI and OpenAI Codex CLI sessions as direct subprocesses, and streams events back. Hook callbacks and Docker-MCP HTTP traffic are multiplexed back over that **same** WebSocket — there is no separate network tunnel.

## Architecture

```
Platform (proxy)                    Satellite (remote machine)
┌──────────────┐                   ┌──────────────────────────┐
│ RemoteExec   │◄══ single WSS ═══►│ SatelliteWSClient        │
│ Layer        │   (outbound only) │   ├── SessionManager     │
│              │                   │   │   ├── CLISession      │
│ ChatStream   │                   │   │   │   └── claude -p   │
│ Pump         │                   │   │   └── CodexSession    │
│ Hook + MCP   │◄ http_* frames ──│   ├── LocalTunnelServer   │
│ HTTP handlers│   (over the WS)   │   │   (127.0.0.1 aiohttp) │
│              │                   │   └── FileSync            │
└──────────────┘                   └──────────────────────────┘
```

- **No bwrap on satellite** -- agents run as direct subprocesses on the host filesystem
- **Dumb pipe** -- the satellite does not parse, filter, or interpret stdout. Every raw NDJSON/JSONL line is forwarded verbatim. All turn-end decisions (settle, JOB_DONE, bg-agent tracking) live on the proxy via `ClaudeCLIEventTranslator` + `SettleController`. The satellite exits its stdout read loop when the proxy sends `stop_turn` or the process EOFs.
- **Hooks shipped with each session** -- `permission_gate.py` and `tool_result_forwarder.py` scripts are bundled into every `start_session` payload and written into the session's `.claude/` or `.codex/` dir before the CLI is spawned. Satellites never need hooks pre-deployed and always run the current proxy version.
- **Hooks call platform over the WS tunnel** -- at runtime, hook scripts (and Docker MCPs) hit the satellite's local `aiohttp` server at `http://127.0.0.1:<port>` (the `PROXY_URL` value injected on the satellite), which multiplexes each request back to the proxy over the same WebSocket as `http_request` frames. No separate network path, no inbound ports.
- **File sync over WS** -- agent directories synced bidirectionally between platform and satellite. Generated/build dirs (cargo `target/`, gradle `build/`, NuGet `obj/`, CMake/Meson trees, ...) are excluded by the marker-confirmed sync-ignore rules the proxy ships in the auth handshake (0.5.110+; engine in `transport/file_sync.py`)
- **Process-per-turn for Codex** -- each message spawns a new `codex exec` process (thread ID enables resume)
- **Protocol-versioned** -- each satellite announces `satellite_version` on connect. The proxy rejects satellites below its `MIN_SATELLITE_VERSION` with a clear upgrade error.

### Package layout

Run with `python -m satellite`. Modules are grouped into subpackages by concern; the entry point + config stay at the root:

| Path | Responsibility |
|---|---|
| `__main__.py`, `config.py` | entry point + boot guard; cross-platform helpers, `SATELLITE_VERSION`, vendored hashes |
| `transport/` | the wire — `ws_client`, `lifecycle_update` (auto-update), `http_tunnel`, `file_sync` |
| `sessions/` | agent sessions — `session_manager`, `cli_session`, `codex_session`, `mcp_install_support`, `mcp_interceptor` |
| `terminal/` | interactive PTY + the local `otodock` CLI — `local_socket`, `otodock_cli`, `otodock_proto`, `pty_session`/`pty_session_base`, `codex_pty_session`, `pty_relay`/`winpty_relay` |
| `host/` | host integration — `host_probe`, `satellite_policy`, `auth_paths`, `env_hygiene`, `path_translator`, `cli_versions`, `tray` |
| `_vendored/` | byte-for-byte copies of proxy source (never edit — see [Vendored Shared Modules](#vendored-shared-modules)) |

`__main__` imports only `config` (a stdlib-only leaf) at module top so the boot guard can roll back a broken auto-update **before** any subpackage import is attempted — a broken update self-heals instead of crash-looping.

## Targeting Model

Two levels of remote execution targeting:

- **Admin (per-agent)**: Admin pairs a machine and assigns agents to it. All users of that agent run on the same machine. Configured in Admin → Remote Machines page and per-agent Config page.
- **User (per-user override)**: Any authenticated user can pair their own machine in User Settings → Remote Machines and set it as their active target (a platform-wide admin toggle can disable personal pairing). This overrides agent-level defaults for that user only.

Resolution priority: **user target > agent target > local**. If a user's machine is offline, falls back to the agent default (per the `remote_fallback_user_override` setting, default on — when off, the session fails with an offline error instead).

## Prerequisites

- Python 3.10+ (3.13 recommended; the installer provisions it where missing)
- Network egress to the platform's public `wss://` endpoint (one outbound WebSocket — no inbound ports, no VPN)
- Claude Code CLI and/or Codex CLI — the pairing installer installs these automatically, along with the rest of the dev toolchain

## Installation

### Quick Install (from pairing UI)

After pairing a machine in the dashboard (Admin → Remote Machines or User Settings → Remote Machines), the UI shows an install command. Run it on the remote machine:

```bash
# Linux / macOS — the pairing modal shows this; the token rides a header, not the URL:
bash <(curl -sL -H "X-Pairing-Token: <token>" "https://<public-host>/v1/satellite/bootstrap?os=linux")
```

The `/v1/satellite/bootstrap` endpoint returns a self-extracting script (bash for Linux/macOS, PowerShell for Windows). It calls `scripts/install-baseline-tools.sh` to install Tier 1 + Tier 2 dev tooling (git, gh, python+uv, node+npm+pnpm, jq, ripgrep, poppler-utils, sqlite3, etc.) and the Claude / Codex CLIs, exchanges the pairing token for the machine secret, writes config, and registers a **per-user** service (systemd user unit + `loginctl enable-linger` / launchd LaunchAgent / Windows logon Scheduled Task) — no root or SYSTEM.

> **Windows gotcha — Python "App execution aliases".** On a fresh Windows 10/11 with no real Python, `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` / `python3.exe` are 0-byte Microsoft Store redirect stubs. They satisfy `Get-Command python` but, when run, print `Python was not found…` and exit `9009`. The installer is hardened against this: `install-baseline-tools.ps1` uses a real-interpreter probe (`Get-RealPythonExe`, not a bare name check) so winget actually installs Python 3.13, and `install.ps1`'s `Test-PythonVersion` runs the probe under a function-local `SilentlyContinue` so a stub degrades to "not found" instead of aborting the install under the bootstrap's `$ErrorActionPreference='Stop'`. If a run still can't find Python (e.g. a real install shadowed by the alias), the installer prints the fix: **Settings → Apps → Advanced app settings → App execution aliases → turn OFF `python.exe` and `python3.exe`**, then re-run.

### Manual Install

```bash
# 1. Create the base directory
mkdir -p ~/.oto-dock

# 2. Copy satellite code (or install from platform)
cp -r satellite/ ~/.oto-dock/satellite/

# 3. Install dependencies (lockfile — exact pinned versions)
cd ~/.oto-dock/satellite
pip install -r requirements.txt

# 4. Create config (after pairing via platform admin/user UI)
cat > ~/.oto-dock/satellite.conf << 'EOF'
[satellite]
machine_id = <from pairing>
machine_secret = <from pairing exchange>
platform_url = wss://<public-host>/v1/satellite
agents_dir = ~/.oto-dock/agents
mcps_dir = ~/.oto-dock/mcps

[cli]
claude_bin = claude

[codex]
codex_bin = codex
EOF

# 5. Run
python -m satellite
```

## Uninstallation

Two paths, both equivalent:

- **From the dashboard** — `Remote Machines → delete` triggers the satellite's `_self_uninstall_and_exit`, which schedules `uninstall.{ps1,sh}` and exits. The deletion endpoint cleans up the platform-side record (machine row, OAuth bearer allowlist, `agent_remote_targets`, `user_remote_targets`).
- **From the satellite host** — run `bash uninstall.sh` (Linux/macOS) or `powershell -ExecutionPolicy Bypass -File uninstall.ps1` (Windows) interactively. The script also best-effort notifies the platform (`DELETE /v1/satellite/{id}/self-uninstall` with `X-Machine-Secret`) so the dashboard entry disappears without admin intervention.

### What gets removed

- The platform-side machine row + targeting tables
- The per-user service registration (`~/.config/systemd/user/oto-dock-satellite.service`, `~/Library/LaunchAgents/com.otodock.satellite.plist`, or the Windows logon Scheduled Task `OtoDockSatellite` + its HKCU Add/Remove-Programs entry)
- The entire install dir: `~/.oto-dock/` on Linux/macOS, `%USERPROFILE%\OtoDock\` on Windows — includes code, venv, agents, mcps, sessions, config, OAuth tokens that were synced down

### What stays — by design

Baseline tooling installed during pairing (`git`, `gh`, `python`, `node`/`pnpm`, `uv`, `jq`, `ripgrep`, `poppler-utils`, `sqlite3`, plus the `@anthropic-ai/claude-code` and `@openai/codex` npm globals) is **left in place**. Two reasons:

1. **No provenance record.** `winget install` / `brew install` / `apt install` are idempotent — if the user already had `git` before pairing, the installer just skipped it. We have no marker distinguishing "we installed this" from "this was already here". Removing them blindly could break unrelated tooling the user depends on.
2. **Convention.** Removing a Python venv tool doesn't uninstall Python; removing VS Code doesn't uninstall git. OtoDock follows the same expectation — uninstall removes OtoDock, not its system-level dependencies.

The uninstall scripts print a copy-paste-ready `winget uninstall …` / `apt remove …` / `brew uninstall …` snippet at the end for users who want a full cleanup. Don't add a `--purge` flag — the OS-level uninstaller (Programs and Features, Homebrew, apt) is the right tool for that job.

## Configuration

Config file: `~/.oto-dock/satellite.conf` (INI format)

| Section | Key | Description |
|---------|-----|-------------|
| `[satellite]` | `machine_id` | UUID assigned during pairing |
| | `machine_secret` | Secret from token exchange |
| | `platform_url` | WebSocket URL to platform |
| | `agents_dir` | Local agent directory base (default: `~/.oto-dock/agents`) |
| | `mcps_dir` | Local MCP servers directory (default: `~/.oto-dock/mcps`) |
| `[cli]` | `claude_bin` | Claude Code CLI *hint* (default: `claude`). When the platform ships CLI pins, runtime pin-verified resolution (`host/cli_versions.py`) decides what spawns — a hint that doesn't satisfy the pin loses to a binary that does. With no pin, the hint is authoritative. |
| `[codex]` | `codex_bin` | Codex CLI hint (default: `codex`) — same resolution rules as `claude_bin` |

## Directory Structure

```
~/.oto-dock/
├── satellite.conf            # Daemon configuration
├── agents/                   # Agent directories (synced from platform)
│   └── {agent-slug}/
│       ├── config/           # Agent config (platform-authoritative, push only)
│       ├── workspace/        # Agent-scoped workspace (bidirectional sync)
│       └── users/
│           └── {username}/
│               ├── workspace/    # Per-user workspace (bidirectional sync)
│               ├── context/      # Per-user context docs (push only)
│               ├── .claude/      # CLI session data (settings, hooks, plans, projects, tasks)
│               └── .codex/       # Codex session data (config.toml, AGENTS.md, auth.json)
└── mcps/                     # Installed MCP servers (stdio only)
    ├── custom/               # Custom MCPs (or symlinked from platform)
    │   └── {mcp-name}/
    │       ├── manifest.json
    │       └── venv/ or node_modules/
    └── community/            # Community MCPs
        └── {mcp-name}/
            ├── manifest.json
            └── venv/ or node_modules/
```

### MCP Discovery + Automated Install

The satellite scans `mcps/` for `manifest.json` files (one or two levels deep, supporting both flat and `custom/`/`community/` layouts). From each manifest, it reports both the `name` field and the `server_name` field (if different) in its capabilities. This is necessary because `mcpServers` keys in the config use `server_name` (e.g., `display`) while manifests use `name` (e.g., `display-mcp`).

**Automated install via `sync_mcps`** (protocol 0.3.0+): before every remote session, the proxy diffs the satellite's installed MCPs against what the session needs (union over all active sessions on the same satellite) and ships missing/outdated ones as gzipped tarballs. Each install runs inside an atomic `{name}.new/` staging dir protected by a `.install-in-progress-{name}` marker — a mid-install disconnect is detected on reconnect by `sync_mcps_verify` and re-queued. The shared `mcp_installer` handles pip (uv-backed when available) / npm / patch-package, preserves per-MCP user data (`keys/`, `config/`, `screenshots/`), and reports per-step progress via `mcp_install_progress` events. Docker MCPs (file-tools, camoufox) stay on the platform; satellites reach them via HTTP multiplexed over the WebSocket tunnel.

The proxy filters out stdio MCPs not present in the satellite's `installed_mcps` capability list (with a 5-minute staleness fallback; freshly verified via `sync_mcps_verify` before every session's diff). HTTP/SSE MCPs (Docker containers on the platform) are always included, with URLs rewritten to the satellite's local tunnel (`http://127.0.0.1:<port>/mcp/<slug>/mcp/`), which forwards each request to the proxy over the WS.

**Same-host development**: you can still symlink the platform's MCPs to skip the install round-trip during local dev:
```bash
ln -sfn /path/to/oto-dock/mcps/custom ~/.oto-dock/mcps/custom
ln -sfn /path/to/oto-dock/mcps/community ~/.oto-dock/mcps/community
```

## WebSocket Protocol

The satellite communicates with the platform via JSON messages over WebSocket.

### Satellite -> Platform

| Type | Purpose |
|------|---------|
| `auth` | Authentication (first message, 5s timeout). Includes `satellite_version`. |
| `heartbeat` | System load + active sessions + per-agent stat fingerprints (every 20s) |
| `ack` | Command acknowledgment. Carries `results` / `installed_mcps` for `sync_mcps`; `mcps` for `sync_mcps_verify`. `file_push` acks when the proxy supplied a `command_id`. |
| `session_event` | Raw CLI/Codex event (forwarded verbatim to ChatStreamPump) |
| `session_started` | Process spawned successfully |
| `session_ended` | Process exited |
| `turn_ended` | Stdout read loop for the current turn exited (stop_turn or EOF). Proxy yields `DONE` on receipt. |
| `codex_thread_id` | Thread ID from Codex first turn |
| `file_changed` | File modified during session |
| `file_manifest` | Response to manifest request |
| `file_content` | Response to file pull |
| `mcp_install_progress` | Streaming progress during `sync_mcps`: `{command_id, mcp, phase, pct, message, error?}`. |
| `pty_output` / `pty_exit` | Interactive-PTY stdout bytes (base64) / child-exit, for the dashboard terminal mirror |
| `pty_alive` | On reconnect: the live interactive-PTY `session_ids` so the proxy can reconcile (re-adopt / exit / orphan) |
| `transcript_lines` | New CLI/Codex session-file (JSONL) lines from an interactive session → the dashboard chat |
| `local_session_open` / `local_session_list` / `local_session_detached` | `otodock` CLI brokering: open/list/detach a local interactive session (keyed by `request_id`) |
| `session_aborted` | Abort acknowledged |
| `pause` | Deliberate pause (tray) — suppresses the admin-offline alert before the WS closes |
| `http_request` / `http_request_chunk` | Hook/MCP HTTP request multiplexed to the proxy (the loopback tunnel) |

### Platform -> Satellite

| Type | Purpose |
|------|---------|
| `auth_result` | Authentication response |
| `start_session` | Spawn CLI or Codex session. Includes `hook_scripts`, `use_native_permissions` (CLI), `multi_value_envs` (`{env_var: separator}` map for joined sandbox-path-list env vars like `OTO_ALLOWED_ROOTS=":"` and `ALLOWED_FILE_DIRS=":"`), and the session's MCP `env` (manifest-declared `path_env` values + standard `OTO_*` set, all sandbox-style virtual paths). `path_translator.translate_env` rewrites virtual paths to satellite-absolute paths before subprocess spawn; for env vars listed in `multi_value_envs` it splits on the separator, translates each segment, drops empties, and rejoins. Mirror of bwrap on local. Codex payloads add `sandbox_mode`, already-mapped `effort`, `auth_json`. |
| `send_message` | Send user message to session |
| `stop_turn` | Exit the current stdout read loop — turn is over according to proxy's `SettleController`. Triggers `turn_ended` reply. |
| `abort` | Interrupt running session: tree-kill for CLI (hard fallback since 0.5.89 — see `interrupt_turn`); Codex soft-interrupts its daemon turn (`turn/interrupt`, daemon + MCPs survive) — since 2026-07-09 the proxy treats that as its GRACEFUL codex path (producer stays alive for the terminal turn event; the `session_aborted` ack only triggers a proxy-side queue drain when a hard abort armed it). |
| `interrupt_turn` | Soft abort for headless Claude CLI (≥ 0.5.89): writes `control_request {interrupt}` to CLI stdin; turn closes with a normal `result`, process + MCPs survive. Handler is CLI-only (requires a `proc`) — codex's graceful path rides `abort` instead. Proxy escalates to `abort` via a 12s watchdog if the turn doesn't close. |
| `close_session` | Clean shutdown |
| `control_request` | Model/mode change — written to CLI stdin as `{"type":"control_request",...}`. |
| `control_response` | Answer to a native `can_use_tool` permission prompt — written to CLI stdin as `{"type":"control_response",...}`. |
| `request_manifest` | Request file manifest for sync |
| `file_push` | Push file to satellite. Atomic `.partial` → fsync → rename (or append + rename on final chunk). When `command_id` is present, satellite replies with an `ack` so the proxy's `push_file()` helper can wait for the flush (write-barrier for the remote file flow). |
| `file_pull` | Pull file from satellite (path-clamped to agent_dir). |
| `sync_mcps` | Batched MCP install/update/remove. Includes per-MCP tarballs (gzipped base64), manifest data, source, version_hash, system_requirements. Satellite streams `mcp_install_progress` as it works. |
| `sync_mcps_verify` | Compute a fresh `version_hash` for every installed MCP and report in ack. Used on reconnect + before every `sync_mcps` diff. |
| `pty_open` | Spawn an interactive TUI under a PTY (the no-`-p` remote analogue of `start_session`); ack carries the satellite `pid` |
| `pty_input` / `pty_resize` / `pty_close` | Interactive-PTY keystroke bytes / resize / close |
| `pty_local_detach` | Dashboard took over a session — detach the local `otodock` terminal but keep the PTY (+ proxy mirror) alive |
| `local_session_opened` / `local_session_listed` / `local_session_error` | Responses to the `otodock` CLI's open/list requests (keyed by `request_id`) |
| `policy_update` | Live refresh of the satellite-host path policy (`allow_full_fs` / `device_grants`) |
| `check_session_resumable` | Ask whether a chat's CLI/Codex session can be resumed on this host |
| `uninstall` | Self-uninstall + exit (machine deleted from the dashboard) |
| `update_required` | Auto-update: apply the pushed tarball, atomic-swap the install dir, restart |
| `http_response` / `http_response_chunk` | Tunneled HTTP response back to the waiting subprocess |
| `pong` | Heartbeat pong (no-op) |

The `auth_result` also carries **`cli_pins`** (`{claude_code, codex}` versions the satellite reconciles its installed CLIs to) and the satellite advertises an **`interactive_pty`** capability so the proxy only drives a remote PTY on hosts that can spawn one (else it falls back to headless `-p`).

## Session Types

### Claude Code CLI (`execution_path: "claude-code-cli"`)

Persistent subprocess. The satellite:
1. Writes hook scripts (from `start_session` payload) into `.claude/`, then system prompt, MCP config (`~` expanded to absolute paths), and settings.json.
2. Spawns `claude -p --session-id <id> --output-format stream-json --input-format stream-json`.
3. Returns immediately (acks to proxy) — does NOT wait for `system.init` event.
4. On each `send_message`: writes prompt to stdin, forwards every NDJSON line from stdout verbatim as a `session_event`.
5. Exits the read loop on `stop_turn` (proxy decision) or process EOF, then runs the file-change scan and sends `turn_ended`.

The satellite has no turn-end intelligence of its own — it does not inspect events for `result`, `[JOB_DONE]`, or background agents. Those decisions live on the proxy.

**Why no init wait**: The CLI with `--input-format stream-json` does not emit `system.init` until it receives the first message on stdin. Waiting for init before sending anything creates a deadlock. The local proxy follows the same pattern — `PersistentSession.start()` returns immediately without waiting for init.

**MCP config handling**: If the agent has no MCPs assigned, the satellite writes `{"mcpServers": {}}` and omits the `--mcp-config` flag entirely (an empty `{}` would be rejected as invalid schema). When MCPs are present, tilde paths (`~/.oto-dock/mcps/...`) are expanded to absolute paths since `subprocess.exec` does not perform shell expansion.

**Native CLI permissions**: When `use_native_permissions=true`, the CLI emits `control_request.can_use_tool` on stdout. The satellite forwards it unchanged (parsed by the proxy's `ClaudeCLIEventTranslator`). When the user answers in the dashboard, the proxy sends a `control_response` back over the WS; the satellite writes the matching `{"type":"control_response",...}` frame to CLI stdin using `send_permission_response()`.

### Codex CLI (`execution_path: "codex-cli"`)

Process-per-turn. The satellite:
1. Writes hook scripts (from payload), AGENTS.md, config.toml (with `~` expanded), and auth.json into `.codex/`.
2. On each `send_message`: spawns `codex exec --json <prompt>` (or `codex exec resume <thread_id>`), **using the `sandbox_mode` and `effort` values already mapped by the proxy** (effort: `max` → `xhigh`; permission_mode → `workspace-write`/`workspace-write-auto`/`danger-full-access`).
3. Captures `thread_id` from `thread.started`, reports to platform.
4. Forwards JSONL events from stdout verbatim to platform via WS.

No duplicate effort mapping or sandbox defaulting happens on the satellite — the proxy is the single source of truth.

**Execution path selection**: The proxy sends `execution_path` in the `start_session` command. The satellite dispatches to `CLISession` or `CodexSession` based on this field. The `execution_path` comes from `AgentConfig.execution_path` (set by the dashboard's layer selection), not from the agent's DB default.

### Interactive PTY (`pty_open`)

Both Claude and Codex can also run as a **full interactive TUI** under a real PTY (no `-p`), driven from the dashboard's terminal or a local `otodock` command. `pty_open` spawns the CLI on a pseudo-terminal (`pty_relay` on Unix, `winpty_relay`/ConPTY on Windows; dispatched via `pty_session.py` / `codex_pty_session.py` on the shared `pty_session_base` spine). Raw stdout bytes stream back as base64 `pty_output` on a dedicated **lossless, backpressured lane** — a full lane pauses the PTY read instead of dropping bytes that would corrupt the xterm stream — and keystrokes arrive as `pty_input`. The CLI's own transcript file is tailed and forwarded as `transcript_lines` so the dashboard chat reflects the interactive work. On reconnect the satellite reports its live `session_ids` via `pty_alive` and the proxy reconciles (re-adopt survivors, exit the dead, reap orphans).

## The `otodock` CLI (local sessions)

On the satellite host, the bundled `otodock` command (`bin/otodock` symlinked onto the PATH; `otodock.cmd` on Windows) starts an interactive Claude/Codex session **as the machine's agent, in the current folder** — synced back to and controllable from the dashboard:

```bash
otodock claude <agent>                  # interactive Claude TUI as <agent>, here
otodock codex  <agent> --folder /path   # in another folder
otodock claude <agent> --resume         # pick a resumable chat first
```

The client connects to a local control socket (`~/.oto-dock/run/otodock.sock`, mode `0600` in a `0700` dir; a per-install named pipe on Windows) whose wire format is defined in `otodock_proto.py`. The daemon (`local_socket.py`) brokers the session through the existing WS (`local_session_*` frames) — **identity is re-derived proxy-side from the machine owner; the client's request fields are untrusted input**. Dual control: if the dashboard takes over a session the proxy sends `pty_local_detach`, and the local terminal detaches while the PTY (and proxy mirror) keep running. CLI-launched chats are marked `origin = 'otodock'` with their `work_cwd` recorded, so a dashboard resume re-spawns in the same folder.

## File Sync

Agent directories are synced between platform and satellite continuously, in real time during a turn:

| Directory | Sync Direction | Authority |
|-----------|---------------|-----------|
| `config/` | Platform -> Satellite | Platform (admin-managed; pushed at session start) |
| `workspace/` | Bidirectional | Last writer wins |
| `users/{name}/` | Bidirectional | Last writer wins |
| `.claude/`, `.codex/` (anywhere in the path) | Platform -> Satellite | Platform (regenerated each session, includes per-user `users/{u}/.claude/`) |

**Initial sync at session start**: the proxy requests a manifest from the satellite, diffs it against the platform's, and pushes missing/changed files before the CLI/Codex process spawns.

**During-turn sync**: Hook callbacks (`/v1/hooks/file`, `/v1/hooks/file-written`) trigger immediate pull or push for individual files. `mcp_output_relocation` per-write push lands camoufox screenshots on the satellite right after the tool call. Uploads from the dashboard push to active remote sessions.

**End-of-turn sync**: The satellite compares file hashes against a session-start snapshot and reports changes via `file_changed` messages. The proxy applies them (small files inline, large files via explicit `pull_file`) into the platform's actual workspace. Interactive (PTY) sessions have no turn boundary, so they run the same scan on transcript QUIESCENCE — the first quiet tail-loop poll after a burst of forwarded transcript lines — plus a final scan at close (0.5.77).

**Phantom-event suppression**: Satellite's `apply_file_push` updates `_file_snapshot[rel_path]` post-write so the next end-of-turn `detect_changes` doesn't echo the same content back as a phantom event.

**Path translation for stdio MCPs**: `path_translator.translate_env` mirrors bwrap's mapping rules — sandbox-style virtual paths in the proxy-supplied env (`/users/{u}/workspace`, `/workspace`, `/.claude`, etc.) get rewritten to satellite-absolute paths (`{agent_dir}/users/{u}/workspace`, etc.) before subprocess spawn. The literal `{session_id}` token (used for session-scoped roles like screenshots) is also expanded here.

For env vars listed in the `start_session` payload's `multi_value_envs` map (built proxy-side from manifest `path_env` decls + `OTO_ALLOWED_ROOTS`), the translator splits each value on its declared separator, translates each segment independently, drops empties, and rejoins. This is what makes `ALLOWED_FILE_DIRS=/users/alice:/workspace:/config` translate correctly to `{agent_dir}/users/alice:{agent_dir}/workspace:{agent_dir}/config`. Same convention as bwrap on local — MCPs see the same env values, no per-target branching needed.

## Resilience

- **Auto-reconnect**: Jittered exponential backoff (1s -> 30s cap) on disconnect
- **Send buffer**: Up to 10,000 messages buffered during disconnect, replayed on reconnect (interactive-PTY output rides a separate lossless, backpressured lane)
- **Heartbeat**: 20s interval with CPU/memory load reporting
- **Graceful shutdown**: SIGTERM/SIGINT closes all active sessions before exit
- **Orphan cleanup**: On startup, kills processes from a previous crashed run

## Protocol Versioning

The satellite sends `satellite_version` (from `satellite/config.py::SATELLITE_VERSION`) in its auth message. The proxy reads two values, both single-sourced so they can't drift:

- **`MIN_SATELLITE_VERSION`** (`ws/satellite.py`) — reserved for hard wire-protocol breaks. A satellite below this with auto-update disabled is rejected with a clear upgrade error.
- **`SATELLITE_VERSION_LATEST`** — derived at import from `satellite/config.py::SATELLITE_VERSION`. A connecting satellite below `LATEST` is **auto-updated** over the WS (tarball push), not rejected.

Bump `SATELLITE_VERSION` for every satellite-side change (it drives auto-update). Only bump `MIN_SATELLITE_VERSION` for an incompatible wire-protocol break.

## Vendored Shared Modules

Four modules are byte-for-byte copies of proxy source, vendored into the satellite's `_vendored/` package so both sides share one implementation. `scripts/sync-satellite-code.sh` copies each verbatim and bakes its sha256 into a `SHARED_*_HASH` constant in `satellite/config.py`:

| Vendored copy (never edit) | Authoritative proxy source |
|---|---|
| `_vendored/mcp_installer.py` | `proxy/services/mcp/mcp_installer.py` |
| `_vendored/stdio_path_interceptor.py` | `proxy/core/stdio_path_interceptor.py` |
| `_vendored/app_server_client.py` | `proxy/core/layers/codex/app_server_client.py` |
| `_vendored/codex_approvals.py` | `proxy/core/layers/codex/codex_approvals.py` |

```bash
# On the proxy machine, after touching any of the four proxy sources above:
./scripts/sync-satellite-code.sh
# …then redeploy/restart the satellite to pick up the new modules.
```

At startup (`__main__._verify_installer_drift`) the satellite computes the sha256 of each vendored module and compares it to its baked `SHARED_*_HASH`. A mismatch exits with a clear error — prevents silent divergence between platform and satellite. **Never edit a vendored copy in place**; edit the proxy source and re-run the sync script.

## Running Tests

```bash
cd satellite
python -m pytest tests/ -v
```

## Running as a Service

The installer registers a **per-user** service automatically (no root / SYSTEM). The unit definitions below are reference only.

### systemd user unit (Linux)

Written to `~/.config/systemd/user/oto-dock-satellite.service`, started with
`systemctl --user enable --now oto-dock-satellite`. Boot-without-login needs
`loginctl enable-linger <your-user>` (one-time). No `User=` line — a user
unit always runs as the user; `WantedBy=default.target` (the user target,
not the system `multi-user.target`).

```ini
[Unit]
Description=Oto Dock Satellite Daemon

[Service]
Type=simple
ExecStart=/home/<your-user>/.oto-dock/satellite/venv/bin/python -m satellite
WorkingDirectory=/home/<your-user>/.oto-dock/satellite
Restart=always
RestartSec=5
Environment=HOME=/home/<your-user>

[Install]
WantedBy=default.target
```

### launchd (macOS)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.otodock.satellite</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-m</string>
        <string>satellite</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/<your-user>/.oto-dock/satellite</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
```

### Windows (logon Scheduled Task)

Registered automatically by `install.ps1` — a per-user logon task named
`OtoDockSatellite` (non-admin), plus an HKCU Add/Remove-Programs entry. There
is no manual unit file; inspect or manage it with:

```powershell
Get-ScheduledTask OtoDockSatellite | Get-ScheduledTaskInfo
```
