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
- **Hooks shipped with each session** -- the four hook scripts (`permission_gate.py`, `tool_result_forwarder.py`, `subagent_tracker.py`, `stop_tracker.py`) are bundled into every `start_session` payload and written into the session's `.claude/` or `.codex/` dir before the CLI is spawned; the satellite's own writers name the same four events for both engines (0.5.121). Satellites never need hooks pre-deployed and always run the current proxy version. Claude sessions always run them; a Codex session runs them when trusted -- the interactive TUI by CLI flag, and (0.5.118) an unattended app-server session (task / phone / meeting / trigger) when the payload's `codex_hooks_floor` is set: `[features] hooks = true`, thread-level `bypass_hook_trust`, deny-only hook output. Attended Codex dashboard chats gate through the JSON-RPC approval bridge instead (asking for every command in the prompting modes). The matrix per engine and placement is the proxy's `docs/architecture/HOOKS.md`.
- **Hooks call platform over the WS tunnel** -- at runtime, hook scripts (and Docker MCPs) hit the satellite's local `aiohttp` server at `http://127.0.0.1:<port>` (the `PROXY_URL` value injected on the satellite), which multiplexes each request back to the proxy over the same WebSocket as `http_request` frames. No separate network path, no inbound ports. A tunneled path with a dot segment, an encoded separator, a backslash or a NUL is refused before the allowlist runs (`_has_traversal`, a byte twin of the proxy's `auth/request_path.py`).
- **File sync over WS** -- agent directories synced bidirectionally between platform and satellite. Generated/build dirs (cargo `target/`, gradle `build/`, NuGet `obj/`, CMake/Meson trees, ...) are excluded by the marker-confirmed sync-ignore rules the proxy ships in the auth handshake (0.5.110+; engine in `transport/file_sync.py`)
- **Persistent `codex app-server` daemon for Codex** -- one JSON-RPC process per session: `thread/start` (or `thread/resume` for a persisted thread id) once, `turn/start` per message; the process-per-turn `codex exec` model is gone (see Session Types)
- **Protocol-versioned** -- each satellite announces `satellite_version` on connect. The proxy rejects satellites below its `MIN_SATELLITE_VERSION` with a clear upgrade error.

### Package layout

Run with `python -m satellite`. Modules are grouped into subpackages by concern; the entry point + config stay at the root:

| Path | Responsibility |
|---|---|
| `__main__.py`, `config.py` | entry point + boot guard; the host OS table (`HostOS`, `ROWS`, `HOST` bound once at import — the twin of `proxy/core/host_os.py`) and the cross-platform helpers that ask it, `SATELLITE_VERSION`, vendored hashes |
| `transport/` | the wire — `ws_client`, `lifecycle_update` (auto-update), `http_tunnel`, `file_sync` |
| `sessions/` | agent sessions — `session_manager`, `cli_session`, `codex_session`, `alive_report` (the `sessions_alive` report), `session_files`, `step_runner` (app steps), `mcp_install_support`, `mcp_interceptor` |
| `terminal/` | interactive PTY + the local `otodock` CLI — `local_socket`, `otodock_cli`, `otodock_proto`, `pty_session`/`pty_session_base`, `codex_pty_session`, `pty_relay`/`winpty_relay` |
| `host/` | host integration — `host_probe`, `satellite_policy`, `auth_paths`, `safe_fs` (the file sync's opens beneath the agents root with no link followed; the satellite's own twin of the proxy's `safe_fs`, not vendored, see proxy SAFE-FS.md "The satellite twin"), `env_hygiene`, `path_translator`, `cli_versions`, `tray`, `service_unit` (the systemd user unit: whether it runs this install, `is_own_unit`, and its `OOMPolicy=continue` drop-in, `ensure_oom_policy`) |
| `_vendored/` | byte-for-byte copies of proxy source — `mcp_installer`, `stdio_path_interceptor`, `app_server_client`, `codex_approvals`, `terminal_queries` (the terminal query/reply strips + `MOUSE_RE` the PTY mirror and the local CLI use; 0.5.125), `layout` (the agent tree: the folder names, the per-user tree, the virtual-root translation `host_of_virtual`, and the questions the session modules ask of a relative path — `user_of`, and the scope's engine-config dir `state_dir`; 0.5.126) — never edit, see [Vendored Shared Modules](#vendored-shared-modules) |

`__main__` imports only `config` (a leaf that reaches no further than the stdlib and `engines.py`) at module top so the boot guard can roll back a broken auto-update **before** any subpackage import is attempted — a broken update self-heals instead of crash-looping.

Everything platform-specific is a FACT on one row — `config.HOST` (this machine's row of the table in `config.py`, the twin of `proxy/core/host_os.py`): `config.HOST.posix`, `.conpty`, `.locks_running_files`, `.service_manager`, `.case_insensitive`, `.name` … No module outside the table compares `sys.platform` (the two exceptions are named below); see [Vendored Shared Modules](#vendored-shared-modules) for why this one shared body is the one that is not vendored.

## Targeting Model

Two levels of remote execution targeting:

- **Admin (per-agent)**: Admin pairs a machine and assigns agents to it. All users of that agent run on the same machine. Configured in Admin → Remote Machines page and per-agent Config page.
- **User (per-user override)**: Any authenticated user can pair their own machine in User Settings → Remote Machines and set it as their active target (a platform-wide admin toggle can disable personal pairing). This overrides agent-level defaults for that user only.

Resolution priority: **user target > agent target > local**. If a user's machine is offline, falls back to the agent default (per the `remote_fallback_user_override` setting, default on — when off, the session fails with an offline error instead).

On the proxy this vocabulary is `proxy/core/placement.py` (core-seams phase 7): the two remote kinds (`KIND_ADMIN_REMOTE` / `KIND_USER_REMOTE`), the pairing scopes (`PAIRING_ADMIN` / `PAIRING_USER`, `machine_is_admin_paired`) and the offline sentinel. `remote_store.resolve_execution_target` picks the target; `remote_store.placement_of` reads the machine row once into the `PlacementCapabilities` descriptor (the machine's `os`, `home_dir`, `agents_dir`, `os_user`, `allow_full_fs`, `user_dirs`, `device_grants`, `has_display` — the facts this satellite reports at auth) that every session builder carries as `SecurityContext.placement` and hands to the MCP set and the prompt as `placement=`.

## Prerequisites

- Python 3.10 or newer (`PYTHON_MIN_VERSION` in VERSIONS.md). The pairing installer provisions one where missing: the distro's `python3` on Linux, brew `python` on macOS, Python 3.12 via winget on Windows. Auto-update keeps whatever interpreter the venv was built on.
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

> **Windows gotcha — Python "App execution aliases".** On a fresh Windows 10/11 with no real Python, `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` / `python3.exe` are 0-byte Microsoft Store redirect stubs. They satisfy `Get-Command python` but, when run, print `Python was not found…` and exit `9009`. The installer is hardened against this: `install-baseline-tools.ps1` uses a real-interpreter probe (`Get-RealPythonExe`, not a bare name check) so winget actually installs Python 3.12 (`Python.Python.3.12`) instead of skipping the step, and `install.ps1`'s `Test-PythonVersion` runs the probe under a function-local `SilentlyContinue` so a stub degrades to "not found" instead of aborting the install under the bootstrap's `$ErrorActionPreference='Stop'`. If a run still can't find Python (e.g. a real install shadowed by the alias), the installer prints the fix: **Settings → Apps → Advanced app settings → App execution aliases → turn OFF `python.exe` and `python3.exe`**, then re-run.

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

- **From the dashboard** — `Remote Machines → delete` triggers the satellite's `_self_uninstall_and_exit`, which schedules `uninstall.{ps1,sh}` and exits. When the script is missing, the daemon cleans up inline; on Linux it stops, disables and removes the systemd unit (its file and drop-in dir) only when the unit runs this install (`host/service_unit.is_own_unit`), and otherwise leaves it to the install it belongs to. The deletion endpoint cleans up the platform-side record (machine row, OAuth bearer allowlist, `agent_remote_targets`, `user_remote_targets`).
- **From the satellite host** — run `bash uninstall.sh` (Linux/macOS) or `powershell -ExecutionPolicy Bypass -File uninstall.ps1` (Windows) interactively. The script also best-effort notifies the platform (`DELETE /v1/satellite/{id}/self-uninstall` with `X-Machine-Secret`) so the dashboard entry disappears without admin intervention. On Linux it stops and removes the `oto-dock-satellite` unit (and its drop-in dir) only when the unit's `WorkingDirectory` is this install's `satellite/` dir, or the unit is absent; another install's unit is left alone.

### What gets removed

- The platform-side machine row + targeting tables
- The per-user service registration (`~/.config/systemd/user/oto-dock-satellite.service` and its `.service.d/` drop-in dir, `~/Library/LaunchAgents/com.otodock.satellite.plist`, or the Windows logon Scheduled Task `OtoDockSatellite` + its HKCU Add/Remove-Programs entry)
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

The two hint rows are the `conf_section` / `conf_key` of the engine's row in `satellite/engines.py` (0.5.124: `SatelliteConfig.cli_bins`, read through `bin_hint(binary)`) — the keys are unchanged, and a new engine adds a row, not a field.

## Directory Structure

```
~/.oto-dock/
├── satellite.conf            # Daemon configuration
├── steps/                    # App steps' scratch (0.5.122): one 0700 dir per running delivery — the script and its payload — removed with the run, wiped at start; never synced
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

**Automated install via `sync_mcps`** (protocol 0.3.0+): before every remote session, the proxy diffs the satellite's installed MCPs against what the session needs (union over all active sessions on the same satellite, plus the agent's MCPs the session leaves out by its context alone) and ships missing/outdated ones as gzipped tarballs. A session start removes only an MCP the platform no longer ships, and from 0.5.137 a frame carrying `remove_any_category` removes it from `core` too (without the flag only `custom` and `community`, as before). Every removal is logged. Each install runs inside an atomic `{name}.new/` staging dir protected by a `.install-in-progress-{name}` marker — a mid-install disconnect is detected on reconnect by `sync_mcps_verify` and re-queued. The shared `mcp_installer` handles pip (uv-backed when available) and npm (the shipped `patches/*.patch` applied with git), bounds every install subprocess over its whole run (300 s; past it the subprocess and everything it started are killed and the MCP is reported failed, the old version kept), preserves per-MCP user data (`keys/`, `config/`, `screenshots/`), and reports per-step progress via `mcp_install_progress` events. Docker MCPs (file-tools, camoufox) stay on the platform; satellites reach them via HTTP multiplexed over the WebSocket tunnel.

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
| `auth` | Authentication (first message, 5s timeout). Includes `satellite_version` and the `capabilities` object; among them `home_dir`, `otodock_dir` and `mcps_dir` resolved, and `home_dir_unresolved` and `otodock_dir_unresolved` as the OS names them (a link or a Windows short name left as it is). The proxy refuses the OtoDock folder under each spelling (`otodock_dir`, `otodock_dir_unresolved`, and the OtoDock folder name under either home) and the MCP folder to every session; the home folder itself is not refused. |
| `heartbeat` | System load + active sessions + per-agent stat fingerprints (every 20s) |
| `ack` | Command acknowledgment. Carries `results` / `installed_mcps` for `sync_mcps`; `mcps` for `sync_mcps_verify`. `file_push` acks when the proxy supplied a `command_id`. |
| `session_event` | Raw CLI/Codex event (forwarded verbatim to ChatStreamPump). (0.5.138) A Codex revive whose `thread/resume` was refused also sends `{type: "_resume_handle", handle}` as the event before the turn; the platform records it as the session's thread. |
| `session_started` | Process spawned successfully |
| `session_ended` | Process exited |
| `turn_ended` | Stdout read loop for the current turn exited (stop_turn or EOF). Proxy yields `DONE` on receipt. |
| `codex_thread_id` | Thread ID from Codex first turn |
| `file_changed` | File modified during session |
| `file_manifest` | Response to manifest request: one frame, or, when the request carried `page_size` (0.5.130), pages in order with `page` and `more` (at most the clamped `page_size`, 256 to 8192 entries, and about 2 MiB of entry text each; the walk and the page sizing run off the loop). The proxy fails the wait on a page out of order, so a lost page never reads as a whole manifest. |
| `file_content` | Response to file pull, in 512 KB chunks. From 0.5.137 the chunks ride the bounded bulk lane (see Resilience). |
| `mcp_install_progress` | Streaming progress during `sync_mcps`: `{command_id, mcp, phase, pct, message, error?}`. |
| `step_output` | (0.5.122) What a running app step prints, as it prints it: `{command_id, delivery_id, text}`, coalesced per 4 KB / 300 ms; the verdict rides the ack (`sessions/step_runner.py`). |
| `pty_output` / `pty_exit` | Interactive-PTY stdout bytes (base64) / child-exit, for the dashboard terminal mirror |
| `pty_alive` | On reconnect: the live interactive-PTY `session_ids` so the proxy can reconcile (re-adopt / exit / orphan) |
| `sessions_alive` | After auth, at every connect: the headless sessions this satellite holds, so a restarted or reconnected platform takes them back or closes them (`sessions/alive_report.py`). `sessions`: the live sessions of an engine with turn replay (Claude Code), each `{session_id, execution_path, agent_slug, turn_active, incarnation, command_id, buffered_events, use_native_permissions}`. `other_sessions` (0.5.138): every other headless session, an engine without turn replay (Codex) alive or not and a Claude Code process that died, each `{session_id, execution_path, agent_slug, alive, turn_active, incarnation, resume_handle, model, mcp_servers, use_native_permissions}` (`resume_handle` is the Codex thread id, `mcp_servers` the session's MCP server names). `incarnation` names the session object that holds the id now: this boot's random nonce and a counter, so an entry from before a restart never matches a new object. When `sessions` cannot be built no frame is sent; when only `other_sessions` fails the frame goes without that key. |
| `transcript_lines` | New CLI/Codex session-file (JSONL) lines from an interactive session → the dashboard chat |
| `local_session_open` / `local_session_list` / `local_session_detached` | `otodock` CLI brokering: open/list/detach a local interactive session (keyed by `request_id`) |
| `session_aborted` | Abort acknowledged |
| `pause` | Deliberate pause (tray) — suppresses the admin-offline alert before the WS closes |
| `http_request` / `http_request_chunk` | Hook/MCP HTTP request multiplexed to the proxy (the loopback tunnel) |
| `http_abort` | (0.5.115+) The local client of a tunneled stream went away (disconnect, first-frame/mid-stream timeout, write reset) — the proxy closes its upstream at once instead of waiting for its idle sweep. Older proxies ignore it. |

### Platform -> Satellite

| Type | Purpose |
|------|---------|
| `auth_result` | Authentication response |
| `start_session` | Spawn CLI or Codex session. Includes `hook_scripts`, `use_native_permissions` (CLI), `multi_value_envs` (`{env_var: separator}` map for joined sandbox-path-list env vars like `OTO_ALLOWED_ROOTS=":"` and `ALLOWED_FILE_DIRS=":"`), and the session's MCP `env` (manifest-declared `path_env` values + standard `OTO_*` set, all sandbox-style virtual paths). `path_translator.translate_env` rewrites virtual paths to satellite-absolute paths before subprocess spawn; for env vars listed in `multi_value_envs` it splits on the separator, translates each segment, drops empties, and rejoins. Mirror of bwrap on local. Claude payloads add `disallowed_tools`, the platform-wide deny list the satellite writes into the scope's shared `settings.json` (a session's own denials never ride it; the permission gate refuses them over the tunnel). Codex payloads add `sandbox_mode`, already-mapped `effort`, `auth_json`, `permission_mode` (`judge` for a check's judge, `""` for every other session), and (0.5.116+) `local_model_provider` — `{base_url, env_key}` for a local OpenAI-compatible endpoint, written into `config.toml` as `model_provider = "oto_local"` + `[model_providers.oto_local]` by both Codex writers; the key rides the payload `env` under the `env_key` name and the satellite host dials `base_url` as configured. 0.5.117+ adds `stream_idle_timeout_ms` (into the provider table) and `catalog_json` (written as `<CODEX_HOME>/models.json`, referenced by the root `model_catalog_json` key with its absolute path; removed when absent) so Codex defers its MCP tools on an Ollama model; the Codex MCP warm gate then waits for every configured server (90 s cap on a local model) before the `start_session` ack. |
| `send_message` | Send user message to session |
| `stop_turn` | Exit the current stdout read loop — turn is over according to proxy's `SettleController`. Triggers `turn_ended` reply. |
| `abort` | Interrupt running session: tree-kill for CLI (hard fallback since 0.5.89 — see `interrupt_turn`); Codex soft-interrupts its daemon turn (`turn/interrupt`, daemon + MCPs survive) — since 2026-07-09 the proxy treats that as its GRACEFUL codex path (producer stays alive for the terminal turn event; the `session_aborted` ack only triggers a proxy-side queue drain when a hard abort armed it). |
| `interrupt_turn` | Soft abort for headless Claude CLI (≥ 0.5.89): writes `control_request {interrupt}` to CLI stdin; turn closes with a normal `result`, process + MCPs survive. Handler is CLI-only (requires a `proc`) — codex's graceful path rides `abort` instead. Proxy escalates to `abort` via a 12s watchdog if the turn doesn't close. |
| `close_session` | Clean shutdown. An optional `incarnation` (0.5.138) closes only the session object that carries it: a close the platform decided on a `sessions_alive` report never ends a session a later `start_session` put under the same id (a mismatch is acked and nothing closes). Without the key the session holding the id closes. |
| `control_request` | Model/mode change — written to CLI stdin as `{"type":"control_request",...}`. |
| `control_response` | Answer to a native `can_use_tool` permission prompt — written to CLI stdin as `{"type":"control_response",...}`. |
| `credentials_update` | The platform's token-rotation fan-out: `{agent_slug, dir_relative, kind, content, command_id}` — rewrite one scope config dir's CLI credential file with a freshly rotated token. `kind` names the engine's row in `satellite/engines.py` (`claude` → `.claude/.credentials.json`, `codex` → `.codex/auth.json`); the target must be that engine's config dir inside the agent tree — relative, no traversal, nothing else — or the frame is refused with an ack error. These files are excluded from the file sync, so this push is their only delivery channel; the live CLIs pick the rewrite up on their own (Claude's mtime-watch / 401-recovery, Codex's guarded `auth.json` reload). No process is touched. |
| `mcp_gateway_token` | (0.5.132) Push one vendor-MCP credential for the satellite-local credential gateway: `{session_id, token_hash (the hash of the session's own token), mcp, header, value, expires_in (a lease in seconds), upstream}`. Held in memory only, keyed by `(token_hash, mcp)`; renewed under the lease by the proxy's 60 s tick, swept when the lease ends. The session's config names the loopback gateway route and the gateway adds this credential on the way out, so the value never lands on disk. Acknowledged with an `ack` (`status: error`, "gateway unavailable or frame incomplete", for a frame missing a field or with no tunnel server); the proxy treats an unacknowledged push as not pushed and sends it again at its next tick. A request whose token has not arrived yet waits up to 3 s for a push in flight. |
| `mcp_gateway_wipe` | (0.5.132) Remove a session's pushed credentials — by its `token_hash`, or by `session_id` when the hash is absent. Sent at the session's close or purge. |
| `request_manifest` | Request file manifest for sync; an optional `page_size` asks for the answer in pages |
| `file_push` | Push file to satellite, written beneath the agent's folder through `host/safe_fs.py` with no component followed: atomic temp → fsync → rename in the parent (or append to `<path>.partial` + rename on final chunk); a link on the way is an error. When `command_id` is present, satellite replies with an `ack` so the proxy's `push_file()` helper can wait for the flush (write-barrier for the remote file flow). |
| `file_pull` | Pull file from satellite (path-clamped to agent_dir, then streamed from a descriptor opened beneath the agents root with no link followed). From 0.5.137 each block is read off the loop and the pull is registered by its `request_id` as its frame is read, until it ends. |
| `file_pull_cancel` | (0.5.137) `{request_id}`: the proxy gave up on a pull, stop producing it and drop its chunks still queued on the bulk lane. An unknown id is ignored. |
| `sync_mcps` | Batched MCP install/update/remove. Includes per-MCP tarballs (gzipped base64), manifest data, source, `source_build` (the packages allowed to build from source, each checked against a plain distribution name), version_hash, system_requirements, and from a 1.7.1 platform `remove_any_category` (removals search `core` too). Satellite streams `mcp_install_progress` as it works. |
| `sync_mcps_verify` | Compute a fresh `version_hash` for every installed MCP and report in ack. Used on reconnect + before every `sync_mcps` diff. |
| `step_run` | (0.5.122) An app step (proxy APPS.md "Steps"): the script's text and sha256, the folder to run in (relative to the synced agent folder), the environment, the payload, the timeout. `sessions/step_runner.py` writes the script and the payload into `~/.oto-dock/steps/<delivery id>/` (0700 — outside `agents/`, so the sync never sees it; wiped at start), checks the hash, runs the interpreter the script's first line names (no mode bit needed) in that folder with the curated environment plus `OTODOCK_PROXY_URL` (the loopback tunnel), `OTODOCK_STEP_PAYLOAD`, `OTODOCK_STEP_SCRIPT`, `OTODOCK_WORKSPACE_DIR` and `OTODOCK_KNOWLEDGE_DIR`, streams `step_output`, kills the tree at the timeout and when the link drops, removes the directory, and acks `{exit_code, output (first 32 KB), timed_out, seconds}` — or an error for a bad id, slug, hash, folder, bound or a duplicate delivery, and (0.5.129) for any failure before the script runs, so every step is answered. (0.5.129) The exit code is the script's own return code, not the pipe closing: its output drains for up to 5 s after it exits, then its process group is killed (POSIX), so a child it left behind neither holds the run to its timeout nor outlives it. The script runs as the satellite's own user, like every session here; only the script travels. (0.5.123) A check's script (proxy CHECKS.md) adds `payload_env` (up to four names set to the payload file's path — `OTODOCK_CHECK_INPUT`) and `cwd_absolute` (the judged session's working directory; it must exist and is refused if it is the filesystem root, inside `~/.oto-dock` — the synced agent tree included, that is the no-field branch — or an ancestor of the home directory). The same version admits the checks tool's routes `/v1/checks/{attached,attach,detach,run}` on the tunnel allowlist and makes Codex's plan collaboration mode follow a `permission_mode` the start payload names, so a judge's read-only sandbox is not plan mode; with no mode named (the proxy names one for a judge only) plan follows the live sandbox, read-only ⟺ plan, both at start and after a mode change, which carries the sandbox alone. |
| `pty_open` | Spawn an interactive TUI under a PTY (the no-`-p` remote analogue of `start_session`); ack carries the satellite `pid` |
| `pty_input` / `pty_resize` / `pty_close` | Interactive-PTY keystroke bytes / resize / close |
| `pty_local_detach` | Dashboard took over a session — detach the local `otodock` terminal but keep the PTY (+ proxy mirror) alive |
| `local_session_opened` / `local_session_listed` / `local_session_error` | Responses to the `otodock` CLI's open/list requests (keyed by `request_id`) |
| `policy_update` | Live refresh of the satellite-host path policy (`allow_full_fs` / `device_grants`) |
| `check_session_resumable` | Ask whether a chat's CLI/Codex session can be resumed on this host |
| `uninstall` | Self-uninstall + exit (machine deleted from the dashboard) |
| `update_required` | Auto-update: apply the pushed tarball, atomic-swap the install dir, restart. A 1.7.1 platform sends it after `auth_result: ok` (with empty `cli_pins`), the message loop applies it, and the platform then closes with 4007. |
| `http_response` / `http_response_chunk` | Tunneled HTTP response back to the waiting subprocess |
| `pong` | Heartbeat pong (no-op) |

**Satellite-host paths.** A `file_push`, `file_pull` or `file_stat` with `path_kind: "satellite_host"` names an absolute path on the machine, and the satellite re-checks it itself (`session_manager._check_satellite_host_policy`, defense in depth against a compromised proxy): on every pairing the machine's own OtoDock folder, its MCP folder and its agents root are refused (the frame's own agent tree excepted), on Windows `<home>/.oto-dock` too, each compared by realpath with the Win32 device prefix (`\\?\`) dropped; then `allow_full_fs`, or else the home and the Claude CLI runtime tree, decide. On Windows a network or device path (a leading `//` or `\\`) is refused before any of this (0.5.134); a root-relative path names the current drive and meets the same checks.

The `auth_result` also carries **`cli_pins`** (`{claude_code, codex}` versions the satellite reconciles its installed CLIs to) and the satellite advertises an **`interactive_pty`** capability so the proxy only drives a remote PTY on hosts that can spawn one (else it falls back to headless `-p`), and a **`steps`** capability (0.5.122) so the proxy sends `step_run` only where it is handled. It also advertises **`paced_transfers`** (0.5.137), so the proxy sends `file_pull_cancel` only where it is handled (the flag and the version). Since 0.5.124 the satellite also advertises **`engines`** — the wire ids of the engines it can run (`sorted(ENGINES)` from `satellite/engines.py`); the proxy refuses to start an engine the satellite does not advertise, and reads an absent value as `{claude-code-cli, codex-cli}` (every satellite before 0.5.124). It also advertises **`mcp_gateway`** (0.5.132) so the proxy routes a session's vendor MCP credentials through the loopback gateway below; an older satellite keeps a vendor entry's inline shape instead.

## Credential gateway (0.5.132)

A machine session's **vendor** HTTP MCP leaves the machine with its credential added here, from memory — the proxy is never in that request path. The loopback tunnel server mounts a handler under `/v1/mcp-gateway/` (`transport/mcp_gateway.py`), and the session's MCP config names `http://127.0.0.1:<port>/v1/mcp-gateway/<mcp>/<path>` with the session's **own token** as the bearer. The proxy pushes each resolved token before the session spawns (`mcp_gateway_token`) and renews it under a lease; the table is keyed by the **hash of that bearer** and the MCP, so no session index is needed and a stale close or a respawn of the same session id can never wipe a replacement's push. A request is matched to its pushed token by the hash of its bearer, confined to the pushed upstream's path, forwarded with the session's own headers dropped (Authorization/Cookie/forwarding), follows no redirect, and a refusal of a request that carries its session token is a JSON-RPC error the MCP client reads, never a 401 (a request with no single bearer gets 401 `session-token-required`, an oversized body 413); the response streams through with the client's transport polled so a vanished reader ends the upstream read. **Memory only**: nothing here is written to disk, an entry past its lease is swept on a 60 s loop, and a satellite restart starts empty (the proxy pushes again at the next session start). A proxy-local **sidecar**'s credential is NOT pushed — that traffic the proxy's own gateway adds in-process when it forwards the tunneled request.

The MCP config copy the satellite writes is **named per session** (`mcp-config-<session_id[:12]>.json`; the legacy `mcp-config.json` is unlinked), so two concurrent sessions never share one, and a **startup sweep** (`session_files.purge_agent_tree_mcp_configs`) removes every session MCP config copy left in an agent tree by an earlier run — the copies an earlier satellite wrote carried vendor bearers inline, which the gateway retired. Since 0.5.133 the copies are host-local in the file sync (`transport/file_sync.py::_CLAUDE_HOST_LOCAL_RE`, identical to the proxy's): neither this satellite's copies nor the proxy's local-session copies (`<agent>-<sha256(user_sub)[:12]>-<session_id[:12]>.json`) ever enter a manifest, so none is pushed here or scrubbed.

## Session Types

### Claude Code CLI (`execution_path: "claude-code-cli"`)

Persistent subprocess. The satellite:
1. Writes hook scripts (from `start_session` payload) into `.claude/`, then system prompt, MCP config (`~` expanded to absolute paths), and settings.json (the hooks, the payload's `permissions.deny` list with the claude.ai Artifact tools among them, auto-update and auto-memory off, and since 0.5.135 `enabledPlugins` with every Claude Code built-in plugin but the four the platform keeps on set to `false` by name: `DISABLED_BUILTIN_PLUGINS`, held equal to the proxy's list).
2. Spawns `claude -p [--model …] … --append-system-prompt-file <prompt> --system-prompt-snapshot off --setting-sources user --output-format stream-json --input-format stream-json --verbose --include-partial-messages --session-id <id>` (`--resume <id>` on a resume). `--setting-sources user` (0.5.135) makes the CLI read the platform's settings.json alone, so a plugin a terminal enabled at the local scope (`settings.local.json`) never loads.
3. Returns immediately (acks to proxy) — does NOT wait for `system.init` event.
4. On each `send_message`: writes prompt to stdin, forwards every NDJSON line from stdout verbatim as a `session_event`. One loop reads stdout at a time (a per-session reader lock): a previous turn's loop still reading (its `stop_turn` lost or late) is told to stop and awaited, up to 10 s, before the new turn's write; one still reading after that fails the new send with "the previous turn is still reading this session", and a post-turn drainer the previous turn's handler started as its reader let go is stopped before the write.
5. Exits the read loop on `stop_turn` (proxy decision) or process EOF, then runs the file-change scan and sends `turn_ended`.

The satellite has no turn-end intelligence of its own — it does not inspect events for `result`, `[JOB_DONE]`, or background agents. Those decisions live on the proxy.

**Why no init wait**: The CLI with `--input-format stream-json` does not emit `system.init` until it receives the first message on stdin. Waiting for init before sending anything creates a deadlock. The local proxy follows the same pattern — `PersistentSession.start()` returns immediately without waiting for init.

**MCP config handling**: If the agent has no MCPs assigned, the satellite writes `{"mcpServers": {}}` and omits the `--mcp-config` flag entirely (an empty `{}` would be rejected as invalid schema). When MCPs are present, tilde paths (`~/.oto-dock/mcps/...`) are expanded to absolute paths since `subprocess.exec` does not perform shell expansion.

**Native CLI permissions**: When `use_native_permissions=true`, the CLI emits `control_request.can_use_tool` on stdout. The satellite forwards it unchanged (parsed by the proxy's `ClaudeCLIEventTranslator`). When the user answers in the dashboard, the proxy sends a `control_response` back over the WS; the satellite writes the matching `{"type":"control_response",...}` frame to CLI stdin using `send_permission_response()`.

### Codex CLI (`execution_path: "codex-cli"`)

Persistent `codex app-server` JSON-RPC daemon (`sessions/codex_session.py`; the old process-per-turn `codex exec` model is gone). The satellite:
1. Writes hook scripts (from payload), AGENTS.md, `hooks.json`, config.toml and auth.json (or removes a stale one) into `.codex/`. The config.toml is composed here: the app-server header (`project_doc_max_bytes`, `[memories]` off, `[tools]` plan tool on, one `[features]` table -- `plugins = false`, `hooks = true` for unattended sessions, merged with any block the proxy prepends), the proxy's `[mcp_servers.*]` sections (`~` expanded, MCP env paths translated, `DISPLAY` injected, interceptor-wrapped) and, on a local endpoint, the `oto_local` provider block.
2. Spawns the daemon once, then `thread/start` (or `thread/resume` for a persisted `thread_id`) with the proxy-mapped `sandbox_mode` / `effort` and, for unattended sessions, `config = {"bypass_hook_trust": true}`; waits for the configured MCP servers to finish starting.
3. Reports the thread id (`codex_thread_id`) so the proxy persists it for resume after a restart. A turn that finds the daemon dead re-warms it first; when `thread/resume` is refused and a new thread starts, the new id goes to the platform (a `_resume_handle` event on the session's `session_event` stream, 0.5.138) before `turn/start`.
4. On each `send_message`: `turn/start` with the per-turn approval policy + sandbox policy; a persistent forwarder ships every daemon notification verbatim to the platform as a `session_event`, including a background sub-agent's events after the main turn ends. Approval and question server-requests go to the proxy over the loopback tunnel (`/v1/hooks/permission`, `/v1/hooks/codex-question`), each approval named as a tool by the vendored `codex_approvals` bridge (a command as `Bash` or `PowerShell`, a file change as `Write`, a sandbox escalation as `CodexEscalation`, and since 0.5.135 terminal input to an elevated command, `kind: "writeStdin"`, as `CodexTerminalInput`); while the platform cannot be reached (a connection error, the tunnel's 502 / 503 / 504, a body that ends before its JSON) a request is held and retried for up to 120 s, a 401 gets a 2, 4, 8 s ladder, and then the approval is declined (a question gets empty answers), as the permission gate does (`_post_to_platform`).

No duplicate effort mapping or sandbox defaulting happens on the satellite — the proxy is the single source of truth (it also decides which sessions run the hook floor: `codex_hooks_floor`).

**Execution path selection**: The proxy sends `execution_path` in the `start_session` command. The satellite resolves it in the **`ENGINES` table** (`satellite/engines.py`, 0.5.124 — one row per engine: the wire id, its binary and `installed_clis` name, `cli_pins` key, config dir, `credentials_update` kind and file, `satellite.conf` hint key, the headless and PTY session classes by dotted path, and two behaviour facts — `turn_replay`, the Mode C per-turn tagging and retention buffer that is Claude's, and which list of the `sessions_alive` report names a live session (`sessions` with it, `other_sessions` without), and `revives_dead_process`, Codex's `run_turn` re-warming a dead daemon, so a dead daemon is not an ack error there) and instantiates the row's headless class. An id the table lacks is refused with an ack error before anything spawns — both here and in `pty_open` (which used to fall through to the Claude PTY). The same table drives the `credentials_update` clamp, the CLI pin reconcile (`host/cli_versions._PACKAGES`) and the `installed_clis` / `cli_status` / `engines` capabilities; the local `otodock` client (`terminal/otodock_cli.py`) keeps its own two-row subcommand → wire-id map, `_EXEC_PATHS`, because it runs standalone and cannot import the table. The proxy's `tests/remote/test_satellite_engine_table.py` checks every shared fact against its engine descriptors. The `execution_path` comes from `AgentConfig.execution_path` (set by the dashboard's layer selection), not from the agent's DB default.

### Interactive PTY (`pty_open`)

Both Claude and Codex can also run as a **full interactive TUI** under a real PTY (no `-p`), driven from the dashboard's terminal or a local `otodock` command. `pty_open` resolves the frame's `execution_path` in the same `ENGINES` table and spawns the row's PTY class on a pseudo-terminal (`pty_relay` on Unix, `winpty_relay`/ConPTY on Windows; `pty_session.py` / `codex_pty_session.py` on the shared `pty_session_base` spine; an unknown id is refused with an ack error, 0.5.124). The Claude TUI takes the headless argv minus `-p` and the stream flags, so it passes `--setting-sources user` too; the Codex TUI passes `--no-daemon` on the fresh and the resume form (0.5.135: Codex 0.157 turned the shared background server's auto-start on; `--dangerously-bypass-hook-trust` already keeps the platform's TUI out of it, but the TUI printed a "Running without the shared background server" line at every start, and `--no-daemon` turns the auto-start off with that line). Raw stdout bytes stream back as base64 `pty_output` on a dedicated **lossless, backpressured lane** — a full lane pauses the PTY read instead of dropping bytes that would corrupt the xterm stream — and keystrokes arrive as `pty_input`. The CLI's own transcript file is tailed and forwarded as `transcript_lines` so the dashboard chat reflects the interactive work. On reconnect the satellite reports its live `session_ids` via `pty_alive` and the proxy reconciles (re-adopt survivors, exit the dead, reap orphans).

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

**Path translation for stdio MCPs**: `path_translator.translate_env` applies the agent tree's rule — `_vendored/layout.py::host_of_virtual`, the byte copy of `proxy/core/layout.py`, edited there and re-synced — to every value: sandbox-style virtual paths in the proxy-supplied env (`/users/{u}/workspace`, `/workspace`, `/.claude`, etc.) get rewritten to satellite-absolute paths (`{agent_dir}/users/{u}/workspace`, etc.) before subprocess spawn. The literal `{session_id}` token (used for session-scoped roles like screenshots) is also expanded here.

For env vars listed in the `start_session` payload's `multi_value_envs` map (built proxy-side from manifest `path_env` decls + `OTO_ALLOWED_ROOTS`), the translator splits each value on its declared separator, translates each segment independently, drops empties, and rejoins. This is what makes `ALLOWED_FILE_DIRS=/users/alice:/workspace:/config` translate correctly to `{agent_dir}/users/alice:{agent_dir}/workspace:{agent_dir}/config`. Same convention as bwrap on local — MCPs see the same env values, no per-target branching needed.

## Resilience

- **Auto-reconnect**: Jittered exponential backoff (1s -> 30s cap) on disconnect
- **Send buffer**: Up to 10,000 messages buffered during disconnect, replayed on reconnect (interactive-PTY output rides a separate lossless, backpressured lane)
- **Bulk lane (0.5.137)**: a file pull's chunks ride a third lane of 3 frames, drained after control and PTY (one bulk frame after every 8 PTY frames while both wait). The pull reads each block off the loop and waits for room, so heartbeats, acks and hook calls go out between its chunks instead of behind the whole file on a slow uplink. A disconnect stops the running pulls and purges the lane (the platform failed those pulls), and `TCP_NOTSENT_LOWAT` (128 KB, Linux and macOS) keeps the kernel's unsent bytes small, so the WebSocket's own pong never waits long behind them. Capability `paced_transfers`.
- **Heartbeat**: 20s interval with CPU/memory load reporting
- **Graceful shutdown**: SIGTERM/SIGINT closes all active sessions before exit
- **Orphan cleanup**: On startup, kills processes from a previous crashed run

## Protocol Versioning

The satellite sends `satellite_version` (from `satellite/config.py::SATELLITE_VERSION`) in its auth message. The proxy reads two values, both single-sourced so they can't drift:

- **`MIN_SATELLITE_VERSION`** (`ws/satellite.py`) — reserved for hard wire-protocol breaks. A satellite below this with auto-update disabled is rejected with a clear upgrade error, and so is one below it on an opted-in plaintext link (`insecure_transport`), since its update is never pushed (re-run the installer there).
- **`SATELLITE_VERSION_LATEST`** — derived at import from `satellite/config.py::SATELLITE_VERSION`. A connecting satellite below `LATEST` is **auto-updated** over the WS (tarball push), not rejected, except on such a plaintext link, where it connects on its own version. The update ends the sessions the machine runs: the proxy pushes it at the reconnect auth without waiting for a turn in flight, and the satellite reaps its child tree, the Claude Code and Codex sessions included, before it restarts (`transport/lifecycle_update.py`), so a turn running there stops, and the remote sessions a graceful proxy restart keeps open do not outlive an upgrade that ships a new satellite. The update replaces the satellite's own code (and rebuilds its venv when `requirements.txt` changed); the Claude Code and Codex CLIs follow the platform's pins on every reconnect, and the other baseline tools (Node, uv, pnpm) move only when the install command runs again or their owner updates them.

Bump `SATELLITE_VERSION` for every satellite-side change (it drives auto-update). Only bump `MIN_SATELLITE_VERSION` for an incompatible wire-protocol break.

**Plaintext links refuse code pushes (F65).** `assert_transport_secure` (`transport/ws_client.py`) runs before the first connect: `wss://` is accepted, a plain `ws://` to a loopback / RFC1918 / link-local host is allowed with a warning, and `ws://` to a public host is a hard error unless the operator opts in with `allow_insecure_transport = true` under `[satellite]` (a split-horizon self-host whose public name resolves privately). On an opted-in plaintext link the satellite marks the connection insecure (the `insecure_transport` capability) and **refuses the frames that carry code or end the install** — `update_required`, `uninstall`, and the 4006 (machine-deleted) close — logging a one-line refusal instead of acting; the proxy correspondingly **withholds the update push** to such a machine. An insecure-link machine therefore updates only by re-running the installer by hand.

## Vendored Shared Modules

Six modules are byte-for-byte copies of proxy source, vendored into the satellite's `_vendored/` package so both sides share one implementation. `scripts/sync-satellite-code.sh` copies each verbatim and bakes its sha256 into a `SHARED_*_HASH` constant in `satellite/config.py`:

| Vendored copy (never edit) | Authoritative proxy source |
|---|---|
| `_vendored/mcp_installer.py` | `proxy/services/mcp/mcp_installer.py` |
| `_vendored/stdio_path_interceptor.py` | `proxy/core/stdio_path_interceptor.py` |
| `_vendored/app_server_client.py` | `proxy/core/layers/codex/app_server_client.py` |
| `_vendored/codex_approvals.py` | `proxy/core/layers/codex/codex_approvals.py` |
| `_vendored/terminal_queries.py` | `proxy/core/terminal_queries.py` (the terminal query / reply strips and the mouse-report pattern the PTY mirror boundaries share; 0.5.125) |
| `_vendored/layout.py` | `proxy/core/layout.py` (the agent tree: the folder names, the per-user tree, the sandbox-virtual roots and the rule that maps them under the satellite's agent dir — `host_of_virtual`, `user_of`, `state_dir`; 0.5.126, core-seams phase 10) |

`mcp_installer.py` keeps its own `runtime` compares (`python` / `node` / `docker`): the satellite cannot import the proxy's runtime authority (`proxy/services/mcp/mcp_manifest_types.py`, core-seams phase 9), so the vendored copy carries the words, and the vocabulary gate lists both it and `sessions/mcp_install_support.py` as allowed sites. A change there is a satellite release.

The host OS table is NOT vendored: `config.py` is the boot guard's leaf (`__main__.py` imports it before `_check_post_update_state` runs, so it may import nothing but the standard library and the stdlib-only sibling leaf `engines.py` (`proxy/tests/core/test_host_os.py::test_the_satellite_config_stays_a_leaf` pins that import set) — a broken vendored module would raise before the rollback could run). The table (`HostOS`, the words `LINUX` / `DARWIN` / `WINDOWS`, the rows `ROWS` / `OTHER`, `family_of`, `of`, `host_os`) is defined in `config.py` and again in `proxy/core/host_os.py`; the vocabulary gate's twin rule pins `_table`, `family_of` and `of`, and `proxy/tests/core/test_host_os.py` imports both and compares every row. Every satellite module asks a fact on `config.HOST` at call time (`config.HOST.posix`, `.conpty`, `.locks_running_files`, `.service_manager`, …) — never `from .config import HOST` (the tests patch the global with a row: `monkeypatch.setattr(config, "HOST", config.ROWS[config.WINDOWS])`). The one exception among the satellite's own modules is `terminal/otodock_cli.py`, the standalone terminal client, whose fallback import branch has no package to reach `config` from; the vendored `mcp_installer.py` keeps its own `sys.platform` reads for the reason it keeps its `runtime` words — one file serving two trees can import a leaf from neither. A module whose function binds its own `config` (the loaded `SatelliteConfig` — `__main__._main`, `host/tray.py`) imports the module as `satconfig`; the proxy's acceptance test refuses a shadowed read.

```bash
# On the proxy machine, after touching any of the six proxy sources above:
./scripts/sync-satellite-code.sh
# …then redeploy/restart the satellite to pick up the new modules.
```

At startup (`__main__._verify_installer_drift`) the satellite computes the sha256 of each vendored module and compares it to its baked `SHARED_*_HASH`. A mismatch exits with a clear error — prevents silent divergence between platform and satellite. **Never edit a vendored copy in place**; edit the proxy source and re-run the sync script.

## Running Tests

```bash
cd satellite
python -m pytest tests/ -v
```

The suite runs on the host floor too (Python 3.10; CI's `satellite-floor` job). `requirements.txt` carries `tomli` there (a `python_version < "3.11"` marker in `requirements.in`: `sessions/codex_session._validate_config_toml` checks the generated `config.toml` with it), so a venv built from it has the parser; the tests that parse a composed `config.toml` go through `tests/_toml.py`, which takes `tomllib`, else `tomli`, else skips. `tests/test_python_floor.py` pins every site that states the floor (`VERSIONS.md`, the two installers, the baseline probe, this README's Prerequisites).

## Running as a Service

The installer registers a **per-user** service automatically (no root / SYSTEM). The unit definitions below are reference only.

### systemd user unit (Linux)

Written to `~/.config/systemd/user/oto-dock-satellite.service`, started with
`systemctl --user enable --now oto-dock-satellite`. Boot-without-login needs
`loginctl enable-linger <your-user>` (one-time). No `User=` line — a user
unit always runs as the user; `WantedBy=default.target` (the user target,
not the system `multi-user.target`). `OOMPolicy=continue`: a child the
kernel's OOM killer ends (a headless browser, say) must not stop the unit
and every session it parents (systemd's default policy is `stop`). An
install whose unit was written before the installer carried the line gets
it at daemon start as a drop-in,
`~/.config/systemd/user/oto-dock-satellite.service.d/oom-policy.conf`,
followed by `systemctl --user daemon-reload`, which applies it to the
running unit (`host/service_unit.ensure_oom_policy`). Like the self-uninstall
and `uninstall.sh` (the installer writes the unit unconditionally), it does so
only when the unit is this install's own: one
OS user may run more than one satellite, `systemctl --user` reaches that
user's one manager, and `host/service_unit.is_own_unit` compares the unit's
`WorkingDirectory` with this install's `satellite/` dir.

```ini
[Unit]
Description=Oto Dock Satellite Daemon

[Service]
Type=simple
ExecStart=/home/<your-user>/.oto-dock/satellite/venv/bin/python -m satellite
WorkingDirectory=/home/<your-user>/.oto-dock/satellite
Restart=always
RestartSec=5
OOMPolicy=continue
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
        <string>/Users/<your-user>/.oto-dock/satellite/venv/bin/python</string>
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
