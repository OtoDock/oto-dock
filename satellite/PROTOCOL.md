# Satellite protocol

How the OtoDock platform (the proxy) and a satellite talk: the one websocket
between them, the frames each side sends, the capability gates, the sessions
the satellite runs, file sync, MCP installs, and the version history. The
daemon itself, its install and its configuration are in
[`README.md`](README.md).

## The connection

The satellite makes a single **outbound** websocket connection to
`wss://<public-host>/v1/satellite` and keeps it. Everything rides it: commands
and session events, the file sync, and every HTTP request a hook script or an
MCP on the machine makes to the platform (the loopback tunnel below). The
machine opens no port and needs no VPN.

- **Auth.** The first frame is `auth` (5 s timeout) with the machine id and
  secret from pairing, `satellite_version` and the `capabilities` object.
  The platform answers `auth_result`. A version below its
  `MIN_SATELLITE_VERSION` is refused with an upgrade error.
- **A dumb pipe.** The satellite does not parse, filter or interpret an
  engine's output: every NDJSON / JSONL line goes to the platform verbatim
  as a `session_event`. Every turn-end decision (settle, background agents,
  the end marker) is the platform's. The satellite stops reading a turn when
  told (`stop_turn`) or at EOF.
- **No sandbox on the machine.** Engines run as direct subprocesses of the
  satellite's own user on the host filesystem. The path policy is enforced
  by the platform's permission gate and re-checked here for host paths.
- **Hooks ship with every session.** The four hook scripts
  (`permission_gate.py`, `tool_result_forwarder.py`, `subagent_tracker.py`,
  `stop_tracker.py`) and the credential helper (`credential_helper.py`)
  arrive in each `start_session` payload and are written
  into the session's `.claude/` or `.codex/` folder before the engine
  starts, so a machine never needs hooks deployed and always runs the
  platform's current ones. They are written only when they change, whole
  (0.5.140).

### The loopback tunnel

Hook scripts and MCPs on the machine reach the platform at
`http://127.0.0.1:<port>` (the `PROXY_URL` the satellite injects), served by
the satellite's local `aiohttp` server (`transport/http_tunnel.py`). Each
request is multiplexed to the platform over the websocket as `http_request`
frames and answered with `http_response` frames. The path is checked
against an allowlist, and a path with a dot segment, an encoded separator, a
backslash or a NUL is refused before the allowlist runs (`_has_traversal`,
the byte twin of the proxy's `auth/request_path.py`). Docker MCPs stay on the
platform. An HTTP MCP's URL is rewritten to the tunnel
(`http://127.0.0.1:<port>/mcp/<slug>/mcp/`).

### Capabilities

The `capabilities` object in `auth` tells the platform what this satellite
can do. The platform sends a frame only where it is handled.

| Capability | Since | What it gates |
|---|---|---|
| `home_dir`, `otodock_dir`, `mcps_dir` (and `*_unresolved`) | | the platform refuses the OtoDock folder under each spelling, and the MCP folder, to every session on the machine. The home folder itself is not refused. |
| `installed_mcps`, `installed_clis`, `cli_status` | | the MCP set a session gets (stdio MCPs the machine lacks are filtered out, with a 5-minute staleness fallback) and the CLI versions on the machine card |
| `interactive_pty` | | the platform drives a remote terminal only where one can be spawned, else it falls back to headless |
| `steps` | 0.5.122 | `step_run` (app steps and check scripts) |
| `engines` | 0.5.124 | the wire ids of the engines this satellite runs. The platform refuses any other engine, and reads an absent value as `claude-code-cli` and `codex-cli`. |
| `mcp_gateway` | 0.5.132 | vendor MCP credentials go through the loopback gateway. An older satellite keeps the inline shape. |
| `paced_transfers` | 0.5.137 | `file_pull_cancel` |
| `insecure_transport` | | set on an opted-in plaintext link: the platform withholds the update push |

`auth_result` carries `cli_pins` (`{claude_code, codex}`): the satellite
reconciles its installed CLIs to those versions and spawns only a binary
that satisfies the pin (`host/cli_versions.py`).

## Satellite to platform

| Type | Purpose |
|------|---------|
| `auth` | Authentication (first message, 5 s timeout): `satellite_version` and the `capabilities` object, with `home_dir`, `otodock_dir` and `mcps_dir` resolved and `home_dir_unresolved` and `otodock_dir_unresolved` as the OS names them (a link or a Windows short name left as it is). |
| `heartbeat` | System load, active sessions and per-agent stat fingerprints, every 20 s. |
| `ack` | Command acknowledgment. Carries `results` / `installed_mcps` for `sync_mcps`, and `mcps` for `sync_mcps_verify`. `file_push` is acked when the platform supplied a `command_id`. |
| `session_event` | A raw CLI / Codex event, forwarded verbatim. A Codex revive whose `thread/resume` was refused first sends `{type: "_resume_handle", handle}`, and the platform records it as the session's thread (0.5.138). |
| `session_started` | The process spawned. |
| `session_ended` | The process exited. |
| `turn_ended` | The read loop of the current turn exited (`stop_turn` or EOF). The platform ends the turn on receipt. |
| `codex_thread_id` | The thread id of a Codex session's first turn, persisted for a resume after a restart. |
| `file_changed` | A file changed during the session. |
| `file_manifest` | The answer to `request_manifest`: one frame, or, when the request carried `page_size` (0.5.130), pages in order with `page` and `more` (at most the clamped `page_size`, 256 to 8192 entries, and about 2 MiB of entry text each, with the walk and the sizing run off the event loop). A page out of order fails the wait, so a lost page never reads as a whole manifest. Each entry is `{path, hash, size, mtime}`. When the request carried `report_skipped: true` (0.5.145) the reply also lists what the walk could not read, with no `hash`: a file as `{path, skipped: "unreadable" \| "too_large", size, mtime}` (size and mtime 0 when even the stat failed), a folder it could not list or a Windows junction it did not follow as `{path, skipped: "unreadable_dir" \| "link", dir: true}` (`""` for the agent folder). A file that vanished under the walk, or is not a regular file, is never listed. |
| `file_content` | The answer to `file_pull`, in 512 KB chunks, on the bounded bulk lane (0.5.137). |
| `mcp_install_progress` | Progress during `sync_mcps`: `{command_id, mcp, phase, pct, message, error?}`. |
| `step_output` | What a running app step prints, as it prints it: `{command_id, delivery_id, text}`, coalesced per 4 KB / 300 ms. The verdict rides the ack (0.5.122). |
| `pty_output` / `pty_exit` | Terminal output bytes (base64) / the child's exit, for the dashboard terminal. |
| `pty_alive` | On reconnect: the live terminal `session_ids`, so the platform re-adopts, exits or reaps them. |
| `sessions_alive` | After auth, at every connect: the headless sessions this satellite holds, so a restarted or reconnected platform takes them back or closes them (`sessions/alive_report.py`). `sessions`: the live sessions of an engine with turn replay (Claude Code), each `{session_id, execution_path, agent_slug, turn_active, incarnation, command_id, buffered_events, use_native_permissions}`. `other_sessions` (0.5.138): every other headless session, an engine without turn replay (Codex) alive or not and a Claude Code process that died, each `{session_id, execution_path, agent_slug, alive, turn_active, incarnation, resume_handle, model, mcp_servers, use_native_permissions}`. `incarnation` names the session object that holds the id now (this boot's random nonce and a counter), so an entry from before a restart never matches a new object. When `sessions` cannot be built no frame is sent. When only `other_sessions` fails the frame goes without that key. |
| `transcript_lines` | New lines of an interactive session's own transcript file, for the dashboard chat. |
| `local_session_open` / `local_session_list` / `local_session_detached` | The local `otodock` command: open, list or detach a local interactive session (keyed by `request_id`). |
| `pty_inject_result` | The answer to every `pty_inject`. |
| `session_aborted` | An abort was acknowledged. |
| `update_received` | `{target_version}`: an `update_required` frame was read, sent before the update starts (0.5.140). The platform counts the push for its rollback check on it (from an older satellite, on its own close going out on a live socket), so an update lost to a dying socket is offered again instead of taken for a rollback. |
| `pause` | A deliberate pause (the tray): suppresses the offline alert before the socket closes. |
| `http_request` / `http_request_chunk` | A hook or MCP HTTP request through the loopback tunnel. |
| `http_abort` | The local client of a tunneled stream went away (disconnect, timeout, write reset): the platform closes its upstream at once (0.5.115). |

## Platform to satellite

| Type | Purpose |
|------|---------|
| `auth_result` | The authentication answer, with `cli_pins`. |
| `start_session` | Spawn a CLI or Codex session. Carries `hook_scripts`, `use_native_permissions` (Claude), `multi_value_envs` (`{env_var: separator}` for joined path lists such as `OTO_ALLOWED_ROOTS=":"`) and the session's MCP `env` (manifest-declared `path_env` values and the standard `OTO_*` set, all sandbox-style virtual paths, translated before the spawn, see "Path translation"). From 0.5.139 `command_wrappers` (`bin/gh`, `bin/gh.cmd`) are written beside the hook scripts, `OTO_HOOKS_DIR` and `OTO_HOOK_PY` are set and `bin` leads the session's `PATH`, so `git` and `gh` ask the platform for the connected GitHub account's token per command (`POST /v1/hooks/cli-credential`) and no environment or file holds it. An older satellite ignores the key. Claude payloads add `disallowed_tools`, the platform-wide deny list written into the scope's shared `settings.json` (a session's own denials never ride it: the permission gate refuses them). Codex payloads add `sandbox_mode`, the mapped `effort`, `auth_json`, `permission_mode` (the live platform mode for a 0.5.143 satellite, and below it `judge` for a check's judge and `""` otherwise), `codex_hooks_floor` (0.5.118), `codex_attended_floor` (0.5.143), `local_model_provider` (0.5.116: `{base_url, env_key}` for a local OpenAI-compatible endpoint, written into `config.toml` as `model_provider = "oto_local"`, and the key rides the payload `env` under the `env_key` name), and from 0.5.117 `stream_idle_timeout_ms` and `catalog_json` (written as `<CODEX_HOME>/models.json` so Codex defers its MCP tools on a local model, and the warm gate then waits for every configured server, 90 s at most on a local model, before the ack). |
| `send_message` | A user message for the session. |
| `steer_turn` / `codex_steer` | A message written into a running headless turn (Claude from 0.5.128). The platform asks the version first, and an older satellite keeps the message queued for the next turn. One handler serves both and acks a strict `{steered}`. |
| `resume_session_stream` | Replay a turn's retained buffer to a platform that re-adopts the session after a restart: a begin marker, the replay, then the live stream. |
| `check_session_process` | Whether a headless session's process is alive, asked before the platform ends a turn that went silent. |
| `codex_compact` | Compact a headless Codex thread by hand. Always acked. |
| `codex_bg_terminals` | The background terminals a Codex session holds, for the platform's background-command drain. Always acked. |
| `stop_turn` | Leave the current read loop: the platform decided the turn is over. Answered with `turn_ended`. |
| `abort` | Interrupt a running session. For the Claude CLI it is a tree kill (the hard fallback since 0.5.89, see `interrupt_turn`). Codex interrupts its daemon's turn (`turn/interrupt`, the daemon and its MCPs survive), the platform's graceful Codex path. |
| `interrupt_turn` | The soft abort for a headless Claude CLI (0.5.89): writes `control_request {interrupt}` to its stdin. The turn closes with a normal `result`, and the process and its MCPs survive. Claude only. The platform escalates to `abort` after 12 s if the turn does not close. |
| `close_session` | A clean shutdown. An optional `incarnation` (0.5.138) closes only the session object that carries it, so a close decided on a `sessions_alive` report never ends a session a later `start_session` put under the same id. Without the key the session holding the id closes. |
| `control_request` | A model or mode change, written to the CLI's stdin as `{"type":"control_request",...}`. A Codex session applies `set_model` and `set_permission_mode` (`sandbox_mode`, and from 0.5.143 `permission_mode`) on its next `turn/start`. |
| `control_response` | The answer to a native `can_use_tool` prompt, written to the CLI's stdin. From 0.5.140 in the CLI's own shape: `response.response` with `behavior` (`allow` with the `updatedInput` the CLI printed, or `deny` with its message), the decision taken from the platform's `response` when its `request_id` matches, else from `approved`. |
| `credentials_update` | The token-rotation fan-out: `{agent_slug, dir_relative, kind, content, command_id}` rewrites one scope's CLI credential file with a freshly rotated token. `kind` names the engine's row in `satellite/engines.py` (`claude`: `.claude/.credentials.json`, `codex`: `.codex/auth.json`). The target must be that engine's config folder inside the agent tree, relative, with no traversal, or the frame is refused. These files never sync, so this push is their only delivery. The running CLIs pick the rewrite up on their own. |
| `mcp_gateway_token` | (0.5.132) One vendor MCP credential for the satellite-local gateway: `{session_id, token_hash, mcp, header, value, expires_in, upstream}`. See "The credential gateway". `expires_in: 0` is a revoke. From 0.5.140 it carries the refusal's `kind` and `reason` and leaves a tombstone, so the session's next request is refused at once with why (reconnect the account in Settings, Integrations). The platform sends a machine's revokes again right after it reconnects. |
| `mcp_gateway_wipe` | (0.5.132) Remove a session's pushed credentials, by `token_hash`, or by `session_id` when the hash is absent. Sent at the session's close or purge. |
| `request_manifest` | Request the file manifest for a sync. An optional `page_size` asks for pages, and `report_skipped: true` (0.5.145) asks for the `skipped` entries too. The platform asks `report_skipped` only in its merge and only of a 0.5.145 satellite. Without it the reply never holds an entry without a hash. |
| `file_push` | Push a file, written beneath the agent's folder through `host/safe_fs.py` with no link followed: a temp file, fsync, rename in the parent (or append to `<path>.partial` and rename on the last chunk). A link on the way is an error. With a `command_id` the satellite acks after the write, a write barrier for the platform. |
| `file_pull` | Pull a file (clamped to the agent folder, streamed from a descriptor opened beneath the agents root with no link followed). From 0.5.137 each block is read off the event loop and the pull is registered by its `request_id` until it ends. |
| `file_pull_cancel` | (0.5.137) `{request_id}`: stop a pull the platform gave up on and drop its queued chunks. An unknown id is ignored. |
| `file_stat` | A cheap revalidation probe for a cached file, answered as an ack and checked as `file_pull` is. |
| `sync_mcps` | Batched MCP install, update and removal: per-MCP gzipped base64 tarballs, the manifest, `source`, `source_build`, `version_hash`, `system_requirements`, and from a 1.7.1 platform `remove_any_category`. Answered with `mcp_install_progress` while it works. See "MCP installs". |
| `sync_mcps_verify` | Compute a fresh `version_hash` for every installed MCP and report it in the ack. Sent on reconnect and before every `sync_mcps` diff. |
| `step_run` | (0.5.122) An app step or a check's script. See "App steps". |
| `pty_open` | Spawn an interactive engine under a PTY (the terminal form of `start_session`). The ack carries the `pid`. |
| `pty_input` / `pty_resize` / `pty_close` | Terminal keystrokes, a resize, a close. |
| `pty_local_detach` | The dashboard took a session over: detach the local `otodock` terminal and keep the PTY alive. |
| `pty_inject` | A platform prompt written into the stdin of a CLI attached through the `otodock` command (how a delegated result reaches it). Answered with `pty_inject_result`. |
| `local_session_opened` / `local_session_listed` / `local_session_error` | The answers to the `otodock` command's requests (keyed by `request_id`). |
| `policy_update` | A live refresh of the machine's path policy (`allow_full_fs`, `device_grants`). |
| `check_session_resumable` | Whether a chat's CLI or Codex session can be resumed on this machine. |
| `uninstall` | Self-uninstall and exit (the machine was deleted in the dashboard). |
| `update_required` | Auto-update: apply the pushed tarball, swap the install folder atomically, restart. At the auth the platform sends it after `auth_result: ok` (with empty `cli_pins`). In its first 10 s of serving (`UPDATE_HOLD_S`) it first reads the socket for the rest of that window, and a disconnect there drops the push. It then waits up to 20 s for a 0.5.140 satellite's `update_received` and closes with 4007. It does not wait for the update to be applied. From 0.5.140 the satellite answers `update_received` as soon as it reads the frame, before it applies it. |
| `http_response` / `http_response_chunk` | A tunneled HTTP response for the waiting subprocess. |
| `pong` | The heartbeat answer. |

**Satellite-host paths.** A `file_push`, `file_pull` or `file_stat` with
`path_kind: "satellite_host"` names an absolute path on the machine, and the
satellite checks it again itself (`session_manager._check_satellite_host_policy`,
defence in depth against a compromised platform): on every pairing the
machine's own OtoDock folder, its MCP folder and its agents root are refused
(the frame's own agent tree excepted), on Windows `<home>/.oto-dock` too, each
compared by real path with the Win32 device prefix (`\\?\`) dropped. Then
`allow_full_fs`, or else the home folder and the Claude CLI's runtime tree,
decide. On Windows a network or device path (a leading `//` or `\\`) is
refused before any of this (0.5.134). A root-relative path names the current
drive and meets the same checks.

## The credential gateway

(0.5.132) A machine session's **vendor** HTTP MCP leaves the machine with its
credential added here, from memory. The platform is not in that request's
path. The tunnel server mounts a handler under `/v1/mcp-gateway/`
(`transport/mcp_gateway.py`), and the session's MCP config names
`http://127.0.0.1:<port>/v1/mcp-gateway/<mcp>/<path>` with the session's
**own token** as the bearer.

- The platform pushes each resolved token before the session spawns
  (`mcp_gateway_token`) and renews it under a lease on a 60 s tick. An
  unacknowledged push counts as not pushed and is sent again. A request
  whose token has not arrived waits up to 3 s for a push in flight.
- The table is keyed by the **hash of the bearer** and the MCP, so a stale
  close or a respawn under the same session id never wipes a replacement's
  push.
- A request is confined to the pushed upstream's path, forwarded with the
  session's own `Authorization`, `Cookie` and forwarding headers dropped,
  follows no redirect, and streams back with the client polled so a
  vanished reader ends the upstream read. A refusal of a request that
  carries its session token is a JSON-RPC error the MCP client reads, never
  a 401 (a request with no single bearer gets 401 `session-token-required`,
  and an oversized body gets 413).
- **Memory only.** Nothing is written to disk. An entry past its lease is
  swept on a 60 s loop, and a restart starts empty. A revoke leaves a tombstone
  for the pair (no secret, at most 1024, cleared by a new push or a wipe).
- A platform sidecar's credential is not pushed: the platform's own gateway
  adds it when it forwards the tunneled request.

The MCP config copy the satellite writes is named per session
(`mcp-config-<session_id[:12]>.json`), so two sessions never share one. A
startup sweep (`session_files.purge_agent_tree_mcp_configs`) removes copies
an earlier run left in an agent tree, and since 0.5.133 the copies are
host-local in the file sync (`transport/file_sync.py::_CLAUDE_HOST_LOCAL_RE`,
identical to the proxy's), so neither side's copies ever enter a manifest.

## Sessions

The platform sends `execution_path` in `start_session` and `pty_open`. The
satellite resolves it in the **`ENGINES` table** (`satellite/engines.py`, 0.5.124):
one row per engine with its wire id, binary and `installed_clis` name, its
`cli_pins` key, config folder, `credentials_update` kind and file, the
`satellite.conf` hint key, the headless and PTY session classes, and two
behaviour facts. The first, `turn_replay`, is Claude's per-turn tagging and
retention buffer, which also decides the `sessions_alive` list a live session
is named in. The second, `revives_dead_process`, is Codex re-warming a dead
daemon. An id the table lacks is refused with an ack error before anything
spawns. The same table drives the credential clamp, the CLI pin reconcile and the
`installed_clis`, `cli_status` and `engines` capabilities. The proxy's
`tests/remote/test_satellite_engine_table.py` checks every shared fact against
its engine descriptors.

### Claude Code CLI (`claude-code-cli`)

A persistent subprocess. The satellite:
1. Writes the hook scripts into `.claude/`, then the system prompt, the MCP
   config (`~` expanded, since a subprocess performs no shell expansion,
   and with no MCPs `{"mcpServers": {}}` is written and `--mcp-config` left out)
   and `settings.json`: the hooks, the payload's `permissions.deny` list,
   auto-update and auto-memory off, and since 0.5.135 `enabledPlugins` with
   every built-in plugin but the four the platform keeps set to `false` by
   name (`DISABLED_BUILTIN_PLUGINS`, held equal to the proxy's list).
2. Spawns `claude -p [--model ...] --append-system-prompt-file <prompt>
   --system-prompt-snapshot off --setting-sources user --output-format
   stream-json --input-format stream-json --verbose --include-partial-messages
   --session-id <id>` (`--resume <id>` on a resume). `--setting-sources user`
   (0.5.135) makes the CLI read the platform's `settings.json` alone.
3. Acks at once, without waiting for `system.init`: the CLI emits it only
   after the first message on stdin, so waiting would deadlock.
4. On each `send_message`, writes the prompt to stdin and forwards every
   stdout line as a `session_event`. One loop reads stdout at a time: a
   previous turn's loop still reading is told to stop and awaited up to
   10 s before the new write. One still reading after that fails the send
   with "the previous turn is still reading this session".
5. Leaves the loop on `stop_turn` or EOF, runs the file-change scan and
   sends `turn_ended`.

The CLI prints `control_request.can_use_tool` under `use_native_permissions`,
and on its own under `--dangerously-skip-permissions` for some calls (a
background subagent's call, a dangerous `rm` inside `bash -c`). The spawn
passes `--permission-prompt-tool stdio`. The satellite forwards the request
unchanged and writes the platform's
`control_response` to stdin (`send_permission_response()`), in the CLI's own
shape from 0.5.140 (it remembers the last 64 requests' inputs, so an allow
carries the input the CLI printed). A platform answers an engine's own
request through a person only for 0.5.140 and later, and refuses it at once
for an older satellite.

### Codex CLI (`codex-cli`)

A persistent `codex app-server` JSON-RPC daemon (`sessions/codex_session.py`).
The satellite:
1. Writes the hook scripts, `AGENTS.md`, `hooks.json`, `config.toml` and
   `auth.json` (or removes a stale one) into `.codex/`. The `config.toml`
   is composed here: the app-server header (`project_doc_max_bytes`,
   `[memories]` off, the plan tool on, one `[features]` table with
   `plugins = false` and `hooks = true` for unattended sessions and, from
   0.5.143, attended chats), the platform's `[mcp_servers.*]` sections
   (`~` expanded, env paths translated, `DISPLAY` injected,
   interceptor-wrapped) and, on a local endpoint, the `oto_local` provider.
2. Spawns the daemon once, then `thread/start` (or `thread/resume` for a
   persisted thread, with `excludeTurns` from 0.5.140) with the mapped
   `sandbox_mode` and `effort`, the approval policy
   (`codex_approvals.approval_policy`: an attended chat by the platform mode
   from 0.5.143, `never` in Don't Ask and auto and `untrusted` in every
   other mode, and an unattended session by the sandbox) and, for unattended
   sessions and from 0.5.143 attended chats, `config = {"bypass_hook_trust":
   true}`. It then waits for the configured MCP servers to start.
3. Reports the thread id (`codex_thread_id`). A turn that finds the daemon
   dead re-warms it. When `thread/resume` is refused and a new thread
   starts, the new id goes to the platform before `turn/start`.
4. On each `send_message`, `turn/start` with the turn's approval and sandbox
   policy. A forwarder ships every daemon notification verbatim, a
   background sub-agent's events after the main turn included. Approvals
   and questions go to the platform over the tunnel (`/v1/hooks/permission`,
   `/v1/hooks/codex-question`), each approval named as a tool by the
   vendored `codex_approvals` bridge (a command as `Bash` or `PowerShell`, a
   file change as `Write`, a sandbox escalation as `CodexEscalation`, and
   since 0.5.135 terminal input to an elevated command as
   `CodexTerminalInput`). While the platform cannot be reached a request is
   held and retried for up to 120 s (a 401 gets a 2, 4, 8 s ladder), then
   declined, as the permission gate does.

No effort mapping or sandbox default is decided here: the platform is the
single source, including which sessions run the hook floor
(`codex_hooks_floor` for unattended sessions, judged in every mode, and
`codex_attended_floor` from 0.5.143 for attended chats, whose daemon env adds
`OTO_HOOK_BRIDGED`: the gate judges the turns Codex runs under `never` and
stands aside while the bridge asks).

### Interactive terminals (`pty_open`)

Both engines can also run as a full interactive TUI under a real PTY,
driven from the dashboard's terminal or a local `otodock` command
(`pty_relay` on Unix, `winpty_relay` / ConPTY on Windows, and `pty_session.py`
and `codex_pty_session.py` on the shared `pty_session_base`). The Claude TUI takes
the headless argv without `-p` and the stream flags. The Codex TUI passes
`--no-daemon` on the fresh and the resume form (0.5.135). Output streams back
as base64 `pty_output` on a **lossless, backpressured lane** (a full lane
pauses the PTY read instead of dropping bytes), keystrokes arrive as
`pty_input`, and the engine's own transcript file is tailed into
`transcript_lines` so the dashboard chat follows the work. On reconnect
`pty_alive` lets the platform re-adopt survivors and reap the rest.

### The `otodock` command

`otodock claude <agent>` (or `codex`) starts an interactive session as the
machine's agent in the current folder. The client talks to a local control
socket (`~/.oto-dock/run/otodock.sock`, mode `0600` in a `0700` folder, or a
per-install named pipe on Windows) whose format is `terminal/otodock_proto.py`.
The daemon (`terminal/local_socket.py`) brokers the session through the
websocket (`local_session_*` frames). **The platform derives the identity from
the machine's owner, and the client's fields are untrusted input.** When the
dashboard takes a session over, `pty_local_detach` detaches the terminal while
the PTY keeps running. Such chats are marked `origin = 'otodock'` with their
`work_cwd`, so a resume from the dashboard spawns in the same folder.

### App steps

(0.5.122) `step_run` carries a script's text and sha256, the folder to run in
(relative to the synced agent folder), the environment, the payload and the
timeout. `sessions/step_runner.py` writes the script and the payload into
`~/.oto-dock/steps/<delivery id>/` (0700, outside `agents/` so the sync never
sees it, and wiped at start), checks the hash, runs the interpreter the script's
first line names in that folder with the curated environment plus
`OTODOCK_PROXY_URL` (the tunnel), `OTODOCK_STEP_PAYLOAD`, `OTODOCK_STEP_SCRIPT`,
`OTODOCK_WORKSPACE_DIR` and `OTODOCK_KNOWLEDGE_DIR`, streams `step_output`,
removes the folder, and acks `{exit_code, output (first 32 KB), timed_out,
seconds}`, or an error for a bad id, slug, hash, folder, bound or duplicate
delivery and (0.5.129) for any failure before the script runs.
- The exit code is the script's own: its output drains for up to 5 s after
  it exits, then its process group is killed (POSIX, 0.5.129), so a child it
  left behind neither holds the run to its timeout nor outlives it.
- At the timeout, and when the link drops, the step is signalled as one
  process group (0.5.142): SIGTERM, then SIGKILL after five seconds (the
  tree walk on Windows). A script's TERM trap still runs.
- A check's script (0.5.123) adds `payload_env` (up to four names set to the
  payload file's path, `OTODOCK_CHECK_INPUT`) and `cwd_absolute` (the judged
  session's working folder, refused if it is the filesystem root, inside
  `~/.oto-dock` or an ancestor of the home folder). The same version admits
  the checks tool's routes on the tunnel allowlist.
- The script runs as the satellite's own user, like every session here.
  Only the script travels.

## File sync

Agent folders are synced continuously, in real time during a turn:

| Folder | Direction | Authority |
|-----------|---------------|-----------|
| `config/` | platform to satellite | the platform (admin-managed, pushed at session start) |
| `workspace/` | both ways | last writer wins |
| `users/{name}/` | both ways | last writer wins |
| `.claude/`, `.codex/` (anywhere in the path) | platform to satellite | the platform (regenerated each session) |

- **At session start** the platform requests a manifest, diffs it with its
  own and pushes what is missing or changed before the engine spawns.
- **During a turn** the hook callbacks (`/v1/hooks/file`,
  `/v1/hooks/file-written`) pull or push single files at once, a pull only
  for a path the session may write back and a push only for a path it may
  write (the platform decides, and otherwise it serves or keeps its own copy).
  Dashboard uploads reach active remote sessions.
- **At the end of a turn** the satellite compares file hashes with the
  session-start snapshot and reports `file_changed`. The platform applies
  them (small files inline, large ones by `file_pull`). A terminal session
  has no turn boundary, so it runs the same scan when its transcript goes
  quiet after a burst, and once more at close (0.5.77). A pushed file updates
  the snapshot so it never echoes back as a change.
- Generated build folders (cargo `target/`, gradle `build/`, NuGet `obj/`,
  CMake and Meson trees, ...) are excluded by marker-confirmed rules the
  platform sends in the auth answer (0.5.110, `transport/file_sync.py`).
- **What the walk cannot read is never a delete** (0.5.145). The snapshot,
  the fingerprint and the manifest share one walk (`_walk_sync_tree`) and one
  file iterator (`_listed_files`). A folder the walk cannot list is
  collected (`os.walk(onerror=)`), and on Windows a child folder that is a
  name surrogate (a junction, a symlink, a mount point:
  `safe_fs.is_link_like`) is not entered (Windows' `os.walk` follows a
  junction). A OneDrive placeholder is entered as before. Asked with
  `report_skipped`, the manifest lists those folders, the files it could not
  hash and the files past the size cap as `skipped` entries, and the
  platform's merge leaves them alone. The end-of-turn scan reports a file
  deleted only when its probe says it is gone (`ENOENT`, `ENOTDIR`, `ELOOP`,
  or no longer a regular file): any other error keeps it in the snapshot and
  reports nothing, and a file hashed but not readable keeps its previous
  hash, so the next turn reports it.
- **Skips are summed up per walk.** Each snapshot and manifest walk counts
  what it could not read (a vanished or non-regular file is not counted)
  and logs only when that summary changes for the agent folder: a WARNING
  `sync <kind> skipped N of M files in <dir> it could not read, e.g. <path>
  (<error>)`, an ERROR when nothing was hashed, an INFO line once every file
  reads again.
- **Windows hashes with a blocking open.** `_hash_file` opens with
  `O_NONBLOCK` where it exists (POSIX, so a named pipe cannot stall the
  walk) and switches back to blocking only then: on Windows
  `os.set_blocking` takes only a pipe, and calling it on a regular file
  failed every hash from 0.5.131 to 0.5.143 (fixed in 0.5.144).

### Path translation

`path_translator.translate_env` rewrites the sandbox-style virtual paths in a
session's environment (`/users/{u}/workspace`, `/workspace`, `/.claude`, ...)
to paths under the agent's folder on this machine before a spawn, by the agent
tree's rule (`_vendored/layout.py::host_of_virtual`, the copy of
`proxy/core/layout.py`). The literal `{session_id}` token is expanded too. A
variable named in `multi_value_envs` is split on its separator, each segment
translated, empties dropped and the result rejoined, so
`ALLOWED_FILE_DIRS=/users/alice:/workspace:/config` becomes
`{agent_dir}/users/alice:{agent_dir}/workspace:{agent_dir}/config`: an MCP sees
the same values it would see in the platform's local sandbox.

## MCP installs

The satellite scans `mcps/` for `manifest.json` files (one or two levels
deep, flat or `custom/` and `community/`) and reports both the `name` and the
`server_name` of each, since a session's `mcpServers` keys use `server_name`
(`display`) while manifests use `name` (`display-mcp`).

Before every remote session the platform diffs the machine's installed MCPs
with what the sessions need (the union over the machine's active sessions,
plus the agent's MCPs the session leaves out by its context alone) and sends
the missing or outdated ones in `sync_mcps` (protocol 0.3.0).
- A session start removes only an MCP the platform no longer ships, and
  every removal is logged. A frame with `remove_any_category` (0.5.137) may
  remove it from `core` too, and without the flag only from `custom` and
  `community`.
- Each install runs in an atomic `{name}.new/` staging folder under a
  `.install-in-progress-{name}` marker. An install cut by a disconnect is
  found on reconnect by `sync_mcps_verify` and queued again.
- The vendored `mcp_installer` handles pip (through uv when present) and npm
  (the shipped `patches/*.patch` applied with git), keeps each MCP's own data
  (`keys/`, `config/`, `screenshots/`), and bounds every install subprocess
  over its whole run (300 s, 0.5.134). Past it the subprocess and everything
  it started are killed, the MCP is reported failed and the old version is
  kept.
- From 0.5.141 it parses a `pypi:` source's extras (`pypi:name[a,b]@1.2.0`),
  and from the same version the startup reconcile removes the uv-managed
  interpreters under `mcps/.uv-python` that no venv uses (POSIX,
  `mcp_installer.prune_uv_pythons`). An older satellite installs such a
  source too, only its version readback is skipped.

## Resilience

- **Reconnect:** jittered exponential backoff, 1 s to a 30 s cap.
- **Send buffer:** up to 10,000 messages kept during a disconnect and
  replayed on reconnect. Terminal output rides its own lossless lane.
- **Bulk lane** (0.5.137, `paced_transfers`): a pull's chunks ride a third
  lane of 3 frames, drained after control and terminal frames (one bulk
  frame after every 8 terminal frames while both wait), so heartbeats, acks
  and hook calls are not stuck behind a whole file on a slow uplink. A
  disconnect stops the running pulls and purges the lane, and
  `TCP_NOTSENT_LOWAT` (128 KB, Linux and macOS) keeps the kernel's unsent
  bytes small so the websocket's own pong never waits long.
- **Heartbeat** every 20 s with CPU and memory load.
- **Shutdown:** SIGTERM / SIGINT closes every session before exit. At
  start, processes left by a crashed run are killed.

## Versions

The satellite sends `satellite_version` (`satellite/config.py::SATELLITE_VERSION`)
in `auth`. The platform reads two values, each defined once:

- **`MIN_SATELLITE_VERSION`** (`proxy/ws/satellite.py`), reserved for a break
  in the wire protocol. A satellite below it with auto-update off is refused
  with an upgrade error, and so is one below it on an opted-in plaintext
  link, which never gets the update pushed (re-run the installer there).
- **`SATELLITE_VERSION_LATEST`**, read from `satellite/config.py` at import. A
  satellite below it is **updated** over the websocket (a tarball push), not
  refused, except on a plaintext link, where it runs its own version. The
  update ends the sessions the machine runs: it is pushed at the reconnect's
  auth without waiting for a turn, and the satellite reaps its child tree,
  the engines included, before it restarts (`transport/lifecycle_update.py`).
  It replaces the satellite's code (and rebuilds its venv when
  `requirements.txt` changed). The Claude Code and Codex CLIs follow the
  platform's pins on every reconnect, and the other baseline tools move only
  when the install command runs again.

Every change to the satellite's code bumps `SATELLITE_VERSION` (it drives the
update). Only an incompatible wire change bumps `MIN_SATELLITE_VERSION`.

**Plaintext links refuse code pushes.** `assert_transport_secure`
(`transport/ws_client.py`) runs before the first connect: `wss://` is
accepted, `ws://` to a loopback, RFC 1918 or link-local host is allowed with
a warning, and `ws://` to a public host is an error unless the machine's
owner opts in with `allow_insecure_transport = true` under `[satellite]` (a
split-horizon self-host whose public name resolves privately). On such a link
the satellite reports `insecure_transport` and **refuses the frames that
carry code or end the install** (`update_required`, `uninstall`, the 4006
machine-deleted close), logging a refusal instead. The platform withholds the
update push, so the machine updates only when the installer runs again.

## Vendored modules

Six modules are byte-for-byte copies of proxy source in `_vendored/`, so both
sides run one implementation. `scripts/sync-satellite-code.sh` copies each and
writes its sha256 into a `SHARED_*_HASH` constant in `satellite/config.py`. At
start (`__main__._verify_installer_drift`) the satellite hashes each copy and
exits with an error on a mismatch.

| Vendored copy (never edit) | Proxy source |
|---|---|
| `_vendored/mcp_installer.py` | `proxy/services/mcp/mcp_installer.py` |
| `_vendored/stdio_path_interceptor.py` | `proxy/core/stdio_path_interceptor.py` |
| `_vendored/app_server_client.py` | `proxy/core/layers/codex/app_server_client.py` |
| `_vendored/codex_approvals.py` | `proxy/core/layers/codex/codex_approvals.py` |
| `_vendored/terminal_queries.py` | `proxy/core/terminal_queries.py` (the terminal query and reply strips, the clipboard-write strip and the mouse-report pattern from 0.5.125, and the bracketed-paste mode tracker of the scrollback rings from 0.5.140) |
| `_vendored/layout.py` | `proxy/core/layout.py` (the agent tree: the folder names, the per-user tree, the virtual roots and `host_of_virtual`, `user_of`, `state_dir`, from 0.5.126) |

```bash
# after changing any of the six proxy sources:
./scripts/sync-satellite-code.sh
```

`mcp_installer.py` keeps its own `runtime` compares (`python`, `node`,
`docker`) and its own `sys.platform` reads: one file serving two trees can
import a leaf from neither, so the proxy's surface tests allow those sites
there. A change there is a satellite release.

**The host OS table is a twin, not a copy.** `config.py` is the boot guard's
leaf: `__main__.py` imports it before the rollback of a broken update runs,
so it imports nothing but the standard library and the stdlib-only
`engines.py` (`proxy/tests/core/test_host_os.py::test_the_satellite_config_stays_a_leaf`),
and a broken vendored module would raise before the rollback. The table
(`HostOS`, `LINUX` / `DARWIN` / `WINDOWS`, `ROWS` / `OTHER`, `family_of`, `of`,
`host_os`) is defined in `config.py` and again in `proxy/core/host_os.py`.
`proxy/tests/core/test_host_os.py` imports both and compares every row. Every
satellite module asks a fact on `config.HOST` at call time (`config.HOST.posix`,
`.conpty`, `.locks_running_files`, `.service_manager`, ...), never
`from .config import HOST`, so the tests can patch the row
(`monkeypatch.setattr(config, "HOST", config.ROWS[config.WINDOWS])`). The one
exception among the satellite's own modules is `terminal/otodock_cli.py`, the
standalone terminal client, which keeps its own two-row subcommand map
(`_EXEC_PATHS`) too. A function that binds the loaded `SatelliteConfig` as
`config` imports the module as `satconfig`.

## Version history

Additive unless it says otherwise. None of these moved `MIN_SATELLITE_VERSION`.
The versions the table skips carried fixes inside the daemon, CLI pin moves
or new routes on the tunnel allowlist.

| Version | What changed on the wire or on the machine |
|---|---|
| 0.5.147 | The permission gate's hook carries `onFailure: "block"` (a hook that cannot run denies the call). The Codex terminal's `[desktop] git-worktree-root` points beside `.codex`. Nothing on the wire. |
| 0.5.146 | `disableClaudeAiConnectors` written into the scope's `settings.json` (the pool account's claude.ai connectors never load). Nothing on the wire. |
| 0.5.145 | `report_skipped` on `request_manifest` and the `skipped` manifest entries. Unlistable folders collected and Windows junctions not followed. The end-of-turn scan's strict delete probe. The per-walk skip summary. A Windows satellite at this version restores the platform's held paths. |
| 0.5.144 | Windows hashes files again (`_hash_file`). The platform merges a Windows satellite from 0.5.130 to 0.5.143 without deleting or pushing any file its manifest leaves out. |
| 0.5.143 | Attended Codex chats run the permission gate too (`codex_attended_floor`, `permission_mode` the live platform mode, `hooks = true` and `bypass_hook_trust` on attended threads). |
| 0.5.142 | A timed-out app step, or one running when the link drops, is signalled as one process group. |
| 0.5.141 | The installer parses `pypi:` extras. The startup reconcile prunes unused uv interpreters. |
| 0.5.140 | Hook files written only when they change. `control_response` in the CLI's own shape. Codex resumes with `excludeTurns`. A gateway revoke carries `kind` and `reason` and leaves a tombstone. `update_received`. A trimmed terminal replay starts in the program's bracketed-paste mode. |
| 0.5.139 | `command_wrappers` and the per-command GitHub credential (`/v1/hooks/cli-credential` on the tunnel). A gateway revoke removes the entry at once. |
| 0.5.138 | `sessions_alive` gains `other_sessions` and an `incarnation` per entry. `close_session` honours an `incarnation`. The `_resume_handle` event. |
| 0.5.137 | Paced transfers: the bulk lane, off-loop pull reads, `file_pull_cancel`, `remove_any_category`. |
| 0.5.136 | The CLI reconcile passes `--allow-scripts` to npm (npm 12 blocks install scripts otherwise). |
| 0.5.135 | `--setting-sources user` and the disabled built-in plugins. The Codex TUI's `--no-daemon`. `CodexTerminalInput`. |
| 0.5.134 | Every MCP install subprocess bounded over its whole run. Network and device paths refused on Windows. |
| 0.5.133 | The per-session MCP config copies are host-local in the file sync. |
| 0.5.132 | The satellite-local credential gateway (`mcp_gateway_token`, `mcp_gateway_wipe`, `mcp_gateway`). |
| 0.5.130 | Paged `file_manifest`. |
| 0.5.129 | Every step is answered, failures before the script included. The exit code is the script's own. |
| 0.5.128 | `steer_turn` into a running headless Claude turn. Not additive: the platform checks the version before sending it. |
| 0.5.126 | `_vendored/layout.py`, the agent tree's rule. |
| 0.5.125 | `_vendored/terminal_queries.py`. |
| 0.5.124 | The `ENGINES` table and the `engines` capability. Unknown engine ids refused on both paths. |
| 0.5.123 | Check scripts (`payload_env`, `cwd_absolute`) and the checks routes on the tunnel. |
| 0.5.122 | App steps (`step_run`, `step_output`, the `steps` capability). |
| 0.5.121 | The hook writers name the same four events for both engines. |
| 0.5.118 | Unattended Codex sessions run the permission gate (`codex_hooks_floor`). |
| 0.5.117 | `stream_idle_timeout_ms` and `catalog_json` for local models. The Codex MCP warm gate. |
| 0.5.116 | `local_model_provider` for Codex on a local endpoint. |
| 0.5.115 | `http_abort`. |
| 0.5.110 | Marker-confirmed build-folder exclusion in the file sync. |
| 0.5.89 | `interrupt_turn`, the soft abort for a headless Claude CLI. |
| 0.5.77 | Terminal sessions run the file-change scan on quiescence and at close. |
| 0.3.0 | `sync_mcps`: the platform installs a machine's MCPs. |
