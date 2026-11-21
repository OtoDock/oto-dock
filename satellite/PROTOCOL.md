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
  `MIN_SATELLITE_VERSION` is pushed the update at the sign-in, or refused
  with an upgrade error where no update can be pushed ("Versions" below).
- **Frames.** Each frame either way is one JSON object with a string
  `type`. The satellite drops a frame it cannot read, or one that is not an
  object, with a warning naming its kind and length (never its content),
  and a frame of a type it does not know with a debug line; the link stays
  up for both (0.5.155).
- **A dumb pipe.** The satellite does not parse, filter or interpret an
  engine's output: every NDJSON / JSONL line goes to the platform verbatim
  as a `session_event`. Every turn-end decision (settle, background agents,
  the end marker) is the platform's. The satellite stops reading a turn when
  told (`stop_turn`) or at EOF.
- **No sandbox on a paired computer.** There the engines run as direct
  subprocesses of the satellite's own user on the host filesystem. The path
  policy is enforced by the platform's permission gate and checked again here
  for host paths. A compute machine runs every session in the platform's
  sandbox, rendered from the start payload (see "Compute mode").
- **Hooks ship with every session on a paired computer.** A compute machine's
  payload has none: it runs the files the platform wrote in its folder. On a
  paired computer the hook scripts (`permission_gate.py`,
  `tool_result_forwarder.py`, `subagent_tracker.py`, `stop_tracker.py`,
  `credential_helper.py`, `usage_report.py`, `session_text.py` and
  `start_context.py`) arrive in each `start_session` payload and are written
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

The tunnel answers some requests itself, with a JSON body `{"error": <code>}`
and no `detail`: `403 path-not-allowlisted` for a path it does not forward
(no frame is sent; the platform's own refusal of a path reaches the caller in
the same shape), `503 tunnel-not-connected` while the link is down,
`503 tunnel-shutting-down` as the satellite stops, `504 tunnel-no-response`
when the platform sends no first frame in time and `502 tunnel-disconnected`
for a request the link dropped on its way. A tool that calls back reads the
code, not a sentence (every delegation-mcp tool puts them in words, each
with its own outcome: `_tunnel_sentence`; the other platform MCPs show the
code).

### Capabilities

The `capabilities` object in `auth` tells the platform what this satellite
can do. The platform sends a frame only where it is handled.

| Capability | Since | What it gates |
|---|---|---|
| `home_dir`, `otodock_dir`, `mcps_dir` (and `*_unresolved`) | | the platform refuses the OtoDock folder under each spelling, and the MCP folder, to every session on the machine. The home folder itself is not refused. |
| `installed_mcps`, `installed_clis` | | the MCP set a session gets (stdio MCPs the machine lacks are filtered out, with a 5-minute staleness fallback) and the CLI versions on the machine card |
| `interactive_pty` | | the platform drives a remote terminal only where one can be spawned, else it falls back to headless |
| `steps` | 0.5.122 | `step_run` (app steps and check scripts). Every satellite the platform admits has it, so the platform does not read it. |
| `engines` | 0.5.124 | the wire ids of the engines this satellite runs. The platform refuses any other engine, and reads an absent value as no engine. |
| `mcp_gateway` | 0.5.132 | vendor MCP credentials go through the loopback gateway. Every satellite the platform admits has it, so the platform does not read it. |
| `paced_transfers` | 0.5.137 | `file_pull_cancel`. Every satellite the platform admits has it, so the platform does not read it. |
| `session_move` | 0.5.156 | `session_move`: a chat's engine session files moved on and off this machine. Every satellite the platform admits has it, so the platform does not read it. |
| `active_sessions` | 0.5.171 | the live session count at the sign-in: a machine running a session takes an automatic update once a heartbeat reports none |
| `insecure_transport` | | set on an opted-in plaintext link: the platform withholds the update push |
| `session_homes` | | the satellite makes a session a home of its own in its scope's engine folder (`<scope>/.claude/own/<session id>/`, and `<scope>/.codex/own/<session id>/` on a satellite whose `session_home_dirs` names `.codex`, `sessions/session_homes.py`, the platform's `session_home.py` vendored) when the start payload's `session_home` asks for one: the session's sign-in, its account record and its settings live there, so the platform picks an account per session. Without it the platform sends no `session_home`, and every session of a scope shares its folder and its account. |
| `compute` | 0.5.172 | the process runs in compute mode (`[satellite] kind = compute`). The platform admits a compute row's link only from a process that claims it, and never a paired row from one that does. |
| `memory` | 0.5.173 | the machine's memory as the platform's admission reads it: `total_bytes`, `available_bytes`, `swap_free_bytes`, `capped`, and from 0.5.177 `known`. `known` is false when nothing could be read. It comes from the vendored `host_resources.py`, and the heartbeat carries it too. A compute machine sends it, and from 0.5.177 so does every Linux machine whose memory guard runs (`sessions/memory_guard.py`, `frame_fields`). The platform then starts a paired machine's sessions by its memory. A Mac, a Windows computer and a Linux machine that cannot read its memory send none. |
| `memory_guard` | 0.5.177 | beside `memory` while the memory guard runs: `true`, or `"stalled"` when its last pass is older than 10 s. The heartbeat carries it too. Absent where no guard runs. |
| `tunnel_ok`, `tunnel_rebinds`, `last_tunnel_error` (beside `local_tunnel_port`) | 0.5.168 | the local link's state at connect, as the heartbeat carries it. `tunnel_ok: false` makes the platform refuse new sessions on the machine until a heartbeat says it answers again; a changed `local_tunnel_port` is the port the platform puts in the next sessions' `PROXY_URL` |
| `between_turns_stream` | 0.5.195 | `stream_between_turns` on `stop_turn`, and a steer into a turn the engine started by itself. Without it the platform reads a Claude session's lines between turns only on `drain_bg`. |
| `tools_listed` | 0.5.178 | the stand-ins say when the engine has a tool's list. Only then may the platform hold a terminal's first message for them. |
| `start_context_hook` | 0.5.180 | the start payload's `start_context_parts`. Without it the platform puts that part in a terminal's text. |
| `session_home_dirs` | 0.5.192 | the engine folders where the satellite makes session homes (`.claude`, `.codex`). A report of `session_homes` alone reads as `.claude`. |
| `compute_files`, `compute_tools`, `compute_image`, `compute_host` | 0.5.176 and later | a compute machine's claims at its sign-in (see "Compute mode"). |
| `exit_port`, `exit_ok`, `exit_why`, `exit_upstream` | 0.5.175, 0.5.196 | the machine's credential exit (`sessions/credential_exit.py`, the proxy's relay vendored). `exit_port` is the loopback port its listener holds (an int), and `exit_ok` whether it answers now (a bool). `exit_why` says why it does not: `installing` while its packages install after an update, `packages` when one is missing, `bind` when its port was refused, `socks` when the machine's proxy is a SOCKS proxy, `""` while it answers. `exit_upstream` (a bool) says whether it reaches the world through an outgoing proxy. The heartbeat carries all four. With no listener the machine sends `exit_ok: false` and its `exit_why` alone, or nothing when no reason is known. A session here gets placeholders and the `exit` block only on a live `exit_port` with `exit_ok: true`. A paired computer must also report its OtoDock folder. For a tool that supports it, the agent never sees the tool's key. OtoDock adds the key to each request on the way out. Elsewhere every tool whose key the exit carries is left out, with the reason (`mcp_session_config.exit_machine_refusal`). A start on a machine that reports `installing` waits up to 120 s for the exit first (`machine_away.exit_settled`). |

The platform admits 0.5.196 and later, and keeps no branch for the exit's
or the SSH agent's version. The update push is the one exception, since it
still serves a satellite below the floor: it waits for `update_received`
from 0.5.140 on.

`auth_result` carries `cli_pins` (`{claude_code, codex}`): the satellite
reconciles its installed CLIs to those versions and spawns only a binary
that satisfies the pin (`host/cli_versions.py`).

## Satellite to platform

| Type | Purpose |
|------|---------|
| `auth` | Authentication (first message, 5 s timeout): `satellite_version` and the `capabilities` object, with `home_dir`, `otodock_dir` and `mcps_dir` resolved and `home_dir_unresolved` and `otodock_dir_unresolved` as the OS names them (a link or a Windows short name left as it is). |
| `heartbeat` | Every 20 s, and once at once when a heal outside the tick rebinds the tunnel. It carries system load, active sessions and per-agent stat fingerprints. It carries the local link's state (0.5.168: `tunnel_ok`, `local_tunnel_port`, `tunnel_rebinds`, `last_tunnel_error`) and the credential exit's `exit_port` and `exit_ok` (0.5.175), with `exit_why` and `exit_upstream` (0.5.196). It carries `memory` and `memory_guard` when the sign-in does (0.5.173, 0.5.177). |
| `ack` | Command acknowledgment. Carries `results` / `installed_mcps` for `sync_mcps`, and `mcps` for `sync_mcps_verify`. `file_push` is acked when the platform supplied a `command_id`: `ok` with the committed file's `size` and `mtime_ns` (0.5.150), or `stale` with `reason` (`changed`, `live`) and the file's current `size` and `mtime_ns` when the push left the file alone. From 2.0 the `ok` ack of a `start_session` names the session's `incarnation`, which the platform records. From 0.5.177 the machine's last check may refuse a `start_session` or `pty_open` by memory (`session_manager._check_capacity`). It refuses when its own reading is under its guard's reserve, never for an id it already holds. The ack is then `status: "error"` with `reason: "machine_memory"`. Its `error` is the sentence "This computer is low on memory. Close something on it or try again.", or on a compute machine "The server is low on memory. Try again in a few minutes.". The platform shows it as it is. |
| `session_event` | A raw CLI / Codex event, forwarded verbatim. A Codex revive whose `thread/resume` was refused first sends `{type: "_resume_handle", handle}`, and the platform records it as the session's thread (0.5.138). |
| `session_started` | The process spawned. |
| `session_ended` | The session's process is gone. After the platform's `close_session` it is `{session_id, exit_code: 0}`. From 2.0 a Claude Code session also sends it when its engine ended by itself between turns (`CLISession.watch_exit`). It then carries `reason` (`"exited"`, or `"memory"` for the memory guard) and `exit_code` when known. It names the session's `incarnation` too. The platform ignores it for an earlier object of the id. The session stays held on the machine until the platform's close. The platform reads the session dead, closes it at once and starts a session at the next message (`RemoteExecutionLayer.note_engine_gone`). An end `abort` or `close_session` made, and an exit inside a turn (that turn's `turn_ended` says it), send none. |
| `turn_ended` | The read loop of the current turn exited (`stop_turn` or EOF). The platform ends the turn on receipt. From 0.5.177 it carries `reason: "memory"` when the memory guard ended the session (`mark_ended_for_memory`). The platform then ends the turn with the chat's line about the stop. From 2.0 it carries two more reasons, each said once (`take_turn_end`). `reason: "exited"`: the engine's process ended inside the turn (Claude Code's process, Codex's daemon). It carries `exit_code` when known and `last_line`, the last line it wrote to stderr (300 characters at most). The platform ends the turn with the line that the engine stopped, never that the machine was lost. `reason: "interrupted"`: a Codex daemon did not take the platform's interrupt, so the machine dropped it. The thread is kept, and the next turn resumes it. The platform reads a stopped turn. A reason the platform does not know is read as none. |
| `memory_stop` | (0.5.177) The memory guard stopped a command or ended a session: `{session_id, kind, what, mb, reading_mb, reserve_mb, why}`. `session_id` is empty for work of no session. `kind` is the stopped unit's: `session`, `terminal`, `app`, `tool`, `step` or `own`. `what` is `command`, `session` or `own`, and `why` is `ceiling`, `reserve` or `swap`. `mb` is the stopped part's size, `reading_mb` the machine's free memory then, `reserve_mb` the guard's reserve. It is sent only after a sign-in's `auth_result` carried a `memory_policy`. The platform logs it and acts on nothing. |
| `codex_thread_id` | The thread id of a Codex session's first turn, persisted for a resume after a restart. |
| `file_changed` | A file changed during the session. |
| `file_manifest` | The answer to `request_manifest`: one frame, or, when the request carried `page_size` (0.5.130), pages in order with `page` and `more` (at most the clamped `page_size`, 256 to 8192 entries, and about 2 MiB of entry text each, with the walk and the sizing run off the event loop). A page out of order fails the wait, so a lost page never reads as a whole manifest. Each entry is `{path, hash, size, mtime}`. When the request carried `report_skipped: true` (0.5.145) the reply also lists what the walk could not read, with no `hash`: a file as `{path, skipped: "unreadable" \| "too_large", size, mtime}` (size and mtime 0 when even the stat failed), a folder it could not list or a Windows junction it did not follow as `{path, skipped: "unreadable_dir" \| "link", dir: true}` (`""` for the agent folder). A file that vanished under the walk, or is not a regular file, is never listed. |
| `file_content` | The answer to `file_pull`, in 512 KB chunks, on the bounded bulk lane (0.5.137). |
| `mcp_install_progress` | Progress during `sync_mcps`: `{command_id, mcp, phase, pct, message, error?}`. From 0.5.160 `error` is the install log's cause lines (see "MCP installs"). |
| `tool_out`, `tool_exit` | A compute machine alone (0.5.174): a line a relayed tool wrote on its stdout, and that tool's end. See "Direct LLM's tool processes". |
| `step_output` | What a running app step prints, as it prints it: `{command_id, delivery_id, text}`, coalesced per 4 KB / 300 ms. The verdict rides the ack (0.5.122). |
| `pty_output` / `pty_exit` | Terminal output bytes (base64) / the child's exit, for the dashboard terminal. From 0.5.171 `pty_exit` carries the `open_token` of the open it ends. The platform drops an exit whose token is not the one its handle holds. A frame or a handle with no token takes the exit of the id. From 0.5.177 it carries `reason: "memory"` when the memory guard ended the terminal's engine. |
| `pty_alive` | On reconnect: the live terminal `session_ids`, so the platform re-adopts, exits or reaps them. From 0.5.170 it repeats the `report_id` the platform's `auth_result` named for this sign-in. From 0.5.171 it carries `open_tokens` (`{session_id: open_token}`): a terminal the platform takes back keeps its open, and the close of an orphan names it. |
| `sessions_alive` | After auth, at every connect: the headless sessions this satellite holds, so a restarted or reconnected platform takes them back or closes them (`sessions/alive_report.py`). `sessions`: the live sessions of an engine whose turns the platform replays (`auth_result.replays_turns_of`: Claude Code, and Codex from 0.5.171), each `{session_id, execution_path, agent_slug, turn_active, incarnation, command_id, buffered_events, use_native_permissions}`, from 0.5.171 with `resume_handle`, `model` and `mcp_servers`. `other_sessions` (0.5.138): every other headless session, an engine the platform does not replay alive or not and a Claude Code process that died, each `{session_id, execution_path, agent_slug, alive, turn_active, incarnation, resume_handle, model, mcp_servers, use_native_permissions}`. `incarnation` names the session object that holds the id now (this boot's random nonce and a counter), so an entry from before a restart never matches a new object. When `sessions` cannot be built no frame is sent. When only `other_sessions` fails the frame goes without that key and, from 0.5.170, with `partial: true`. From 0.5.170 the frame repeats the sign-in's `report_id`: the platform reads the two alive frames as one report of that sign-in only when both carry the id its `auth_result` named, so a pair queued at an earlier sign-in and sent late is never taken for the current one. |
| `transcript_lines` | New lines of an interactive session's own transcript file, for the dashboard chat. From 0.5.171 with `offset`, the file offset after these lines: the platform answers `transcript_ack`, and a sign-in sends the lines past the last ack again (a platform that never acks is never sent a line twice). |
| `local_session_open` / `local_session_list` / `local_session_detached` | The local `otodock` command: open, list or detach a local interactive session (keyed by `request_id`). |
| `pty_inject_result` | The answer to every `pty_inject`. |
| `session_aborted` | An abort was acknowledged. |
| `update_received` | `{target_version}`: an `update_required` frame was read, sent before the update starts (0.5.140). The platform counts the push for its rollback check on it (from an older satellite, on its own close going out on a live socket), so an update lost to a dying socket is offered again instead of taken for a rollback. |
| `pause` | A deliberate pause (the tray): suppresses the offline alert before the socket closes. |
| `http_request` / `http_request_chunk` | A hook or MCP HTTP request through the loopback tunnel. It may carry `via: "app"` for an app server's own listener (0.5.174). |
| `cli_status` | `{clis: {<binary>: {version, path}}}`, the CLI versions after each pin reconcile. The platform keeps it on the machine card. |
| `app_server_output` / `app_server_exit` | A compute machine alone (0.5.174). What an app server printed, `{server_id, text}`, and its end, `{server_id, code}`. Both ride the bulk lane. |
| `http_abort` | The local client of a tunneled stream went away (disconnect, timeout, write reset): the platform closes its upstream at once (0.5.115). |

## Platform to satellite

| Type | Purpose |
|------|---------|
| `auth_result` | The authentication answer, with `cli_pins`. From a platform that reads alive reports by sign-in it carries `report_id`, which the satellite repeats in `pty_alive` and `sessions_alive` (0.5.170). It carries `replays_turns_of`, the engines whose turns the platform replays after a restart (Claude Code and Codex). The satellite keeps and tags a turn's events by its own table, for every engine whose row says `turn_replay` (`engines.py`: both), whatever the field names. The field picks the connect report: a live session of an engine it names is listed in `sessions_alive`'s `sessions`, every other headless session in `other_sessions`. A platform that sends no list is read as naming the table's default (`replayed_by_default`: Claude Code alone). From 2.0 it also carries `memory_policy`, the numbers of the machine's memory guard for its kind (0.5.177). `share`, `floor_mb` and `cap_mb` give the reserve, `max(floor_mb, min(share × total, cap_mb))`, with no cap at 0. A compute machine's are 5 %, `SESSION_RESERVE_FLOOR_MB` and 0, a personal or admin computer's 10 %, 1024 and 4096. `credit_mb` is the swap credit, and `min_stop_mb` the cap of the least growth the guard stops. `start_mb` is what a unit may grow in its first 90 s, and `session_max_mb` a ceiling per session, 0 for none. The satellite keeps each key within its range and drops an odd one, whose default stands. Without the field it uses its kind's defaults and sends no `memory_stop`. It may carry `session_starts`, the admin's `SESSION_SPAWN_CONCURRENCY` (a whole number above 0), and only to a compute machine. It bounds the session processes that machine starts at once (`sessions/start_slots.py`). A paired computer, and a compute machine sent none, keeps its own bound: half its CPUs, at least 2. From 0.5.196 it may carry `egress_proxy`, to the install's own compute machine alone (`mcp_egress.exit_upstream_block`). It is sent when the platform names an outgoing proxy for https and is not a hosted install. It holds `https` (the proxy's address), `no_proxy`, and `trust` and `cert_dir` (the platform's trust files, in the shape of `sync_mcps`'s `egress`). That machine's credential exit reaches the world through it, trusting those files beside the system's. It is kept in memory and never logged (`credential_exit.set_platform_egress`). Any other machine, and a frame without it, leaves the exit on the machine's own settings. |
| `start_session` | Spawn a CLI or Codex session. Carries `hook_scripts`, `use_native_permissions` (Claude), `multi_value_envs` (`{env_var: separator}` for joined path lists such as `OTO_ALLOWED_ROOTS=":"`) and the session's MCP `env` (manifest-declared `path_env` values and the standard `OTO_*` set, all sandbox-style virtual paths, translated before the spawn, see "Path translation"). From 0.5.139 `command_wrappers` (`bin/gh`, `bin/gh.cmd`) are written beside the hook scripts, `OTO_HOOKS_DIR` and `OTO_HOOK_PY` are set and `bin` leads the session's `PATH`, so `git` and `gh` ask the platform at each command (`POST /v1/hooks/cli-credential`). Where the machine's credential exit covers the host, the answer is a placeholder and the exit adds the token on the way out. On a macOS or Windows computer the answer is the token itself, for that one command. No environment or file holds it. Since platform 1.9.0 the payload `env` also carries `OTO_CLI_TOKEN`, the session's command-line token, the bearer the helper presents on that route (a session token minted since 1.9.0 is refused there); every satellite version passes it on unchanged, since the payload `env` is overlaid on the CLI's own, and the stdio MCP entries of the shipped config name it empty. Every payload carries `permission_mode`, the platform's word for the session's mode (`ask`, `autoReview`, `dontAsk`, `plan`, the unattended `auto`, `judge`). Claude payloads add `cli_permission_mode`, Claude Code's own word for the mode (`default`, `auto`, `dontAsk`, `plan`; the unattended `auto` is `default`), which the satellite spawns by, and `disallowed_tools`, the platform-wide deny list written into the scope's shared `settings.json` (a session's own denials never ride it: the permission gate refuses them). Codex payloads add `sandbox_mode`, the mapped `effort`, `auth_json`, `permission_mode` (the plan collaboration mode and the approval policy follow it), `codex_hooks_floor` (0.5.118), `codex_attended_floor` (0.5.143), `local_model_provider` (0.5.116: `{base_url, env_key}` for a local OpenAI-compatible endpoint, written into `config.toml` as `model_provider = "oto_local"`, and the key rides the payload `env` under the `env_key` name), and from 0.5.117 `stream_idle_timeout_ms` and `catalog_json` (written as `<CODEX_HOME>/models.json` so Codex defers its MCP tools on a local model, and the warm gate then waits for every configured server, 90 s at most on a local model, before the ack). From 0.5.173 the payload may carry `ssh_agent` (`{"public_keys": {<key name>: <OpenSSH public key line>}}`) for a session that takes the agent's SSH keys: the satellite writes the lines as `<key name>.pub` under the session's secrets folder, opens the session's agent socket (`~/.oto-dock/run/ssh-agent/<12 hex of the session id>.sock`, a named pipe on Windows, below) and relays each agent protocol message to `POST /v1/hooks/ssh-agent` through its own tunnel with the session's token (`sessions/ssh_agent.py`; the route is on both allowlists), and sets `SSH_AUTH_SOCK` and `OTO_SSH_KEY_DIR` in the spawn env. No private key reaches the machine. From 0.5.194 a Windows machine opens a named pipe instead, `\\.\pipe\oto-ssh-agent-<32 hex>`, a random name per start. Its access list names this machine's user alone. The spawn env also gets `OTO_SSH`, Windows' own ssh, and `GIT_SSH` naming it when the machine names no git ssh of its own. A compute payload carries `ssh_agent: {"public_keys": {}}` when its `sandbox` names the socket (the public keys are in the platform's folder). On a machine the exit's rule admits, the payload may carry `exit`, the session's door at this machine's own credential exit (`sessions/credential_exit.py`): `{secret, token_hash, entries: [{placeholder, header, prefix, key, hosts}], carves}`. `secret` is what the session's placeholder-holding processes present as the Basic proxy credential `oto:<secret>` (compared in constant time); `token_hash` the key under which the platform's `mcp_gateway_token` pushes of the values land; `entries` the placeholders with the header each is swapped in, the prefix the tool writes itself, the key's name and the hosts the value may go to; `carves` the addresses a compute machine's dial rule admits (empty for a paired computer). Nothing of a value. The satellite opens the door before the spawn (the authority and the bundle under `<otodock dir>/credential-exit/` made at the first), a second start under the same id replaces it, and the close drops it. A compute payload carries the block when its `sandbox` names `exit_port`. From 0.5.194 an entry may carry two more keys. Each is read only as a plain `true`. `streams`: a request body that is chunked or announces more than 1 MiB passes the exit as it arrives, searched as it passes, in place of the 16 MiB cap. `judged`: the exit asks the platform about each request that carries the entry's placeholder, before it adds the value. The ask is a POST to `/v1/hooks/cli-credential` through the session's tunnel, with the payload `env`'s `OTO_CLI_TOKEN` and the body `{session_id, host, request: {placeholder, method, host, target, content_type, content_encoding, body_b64?}}`. The answer is `{"verdict": {status, detail, content_type, wants_body, body_b64, link}}`. Any other answer, a failed ask or one past 10 s refuses the request with 503. |
| `send_message` | A user message for the session. |
| `steer_turn` / `codex_steer` | A message written into a running headless turn (Claude from 0.5.128). One handler serves both and acks a strict `{steered}`. A Claude session also takes one during a turn the engine started by itself (a machine that reports `between_turns_stream`). |
| `resume_session_stream` | Replay a turn's retained buffer to a platform that re-adopts the session after a restart: a begin marker, the replay, then the live stream. |
| `check_session_process` | Whether a headless session's process is alive, asked before the platform ends a turn that went silent. |
| `codex_compact` | Compact a headless Codex thread by hand. Always acked. |
| `codex_bg_terminals` | The background terminals a Codex session holds, for the platform's background-command drain. Always acked. |
| `stop_turn` | Leave the current read loop: the platform decided the turn is over. Answered with `turn_ended`. With `drain_bg` the session keeps forwarding its background work's lines after `turn_ended` for `drain_bg_s` seconds (0.5.167; ten minutes when absent). With `stream_between_turns` a Claude session forwards every line after the turn, with no time bound. It stops at the next `send_message`, its process's end or its close. Only a machine that reports `between_turns_stream` is sent it. |
| `abort` | Interrupt a running session. For the Claude CLI it is a tree kill (the hard fallback since 0.5.89, see `interrupt_turn`). Codex interrupts its daemon's turn (`turn/interrupt`, the daemon and its MCPs survive), the platform's graceful Codex path. |
| `interrupt_turn` | The soft abort for a headless Claude CLI (0.5.89): writes `control_request {interrupt}` to its stdin. The turn closes with a normal `result`, and the process and its MCPs survive. Claude only. The platform escalates to `abort` after 30 s if the turn does not close. |
| `start_session` (`session_home`) | A Claude payload for a satellite that reports `session_homes` may carry `session_home`, the engine's declaration (`{links, own_files, seed}`). The satellite makes the session's home in the scope's `.claude` (`own/<session id>/`, mode 0700): each name of `links` a link to the scope's folder of that name (a junction on Windows), `seed` the files copied from the scope's own copy without the named top-level keys (`.claude.json` without `oauthAccount`). It writes the sign-in there, copies the scope's `settings.json` there with the same bytes and points `CLAUDE_CONFIG_DIR` at the home. The hook scripts, the prompt and the tools' config stay in the scope's folder. The close removes the home, its conversation stays in the scope's `projects`, and the satellite's start sweeps every home the last run left. A terminal (`pty_open`) does the same. |
| `close_session` | A clean shutdown. An optional `incarnation` (0.5.138) closes only the session object that carries it, so a close decided on a `sessions_alive` report never ends a session a later `start_session` put under the same id. Without the key the session holding the id closes. |
| `control_request` | A model or mode change, written to the CLI's stdin as `{"type":"control_request",...}`. A Claude `set_permission_mode` carries the CLI's own word (`default`, `auto`, `dontAsk`, `plan`) and is written verbatim. A Codex session applies `set_model` and `set_permission_mode` (`sandbox_mode` and `permission_mode`) on its next `turn/start`. |
| `control_response` | The answer to a native `can_use_tool` prompt, written to the CLI's stdin. From 0.5.140 in the CLI's own shape: `response.response` with `behavior` (`allow` with the `updatedInput` the CLI printed, or `deny` with its message), the decision taken from the platform's `response` when its `request_id` matches, else from `approved`. From 0.5.171 an allow also carries the decision's `updatedPermissions` (a plan's exit moving the CLI to the mode the person chose). |
| `credentials_update` | The token-rotation fan-out: `{agent_slug, dir_relative, kind, content, command_id}` rewrites one scope's CLI credential file with a freshly rotated token. `kind` names the engine's row in `satellite/engines.py` (`claude`: `.claude/.credentials.json`, `codex`: `.codex/auth.json`). The target must be that engine's config folder inside the agent tree, relative, with no traversal, or the frame is refused. A session's home in that folder (`<folder>/own/<session id>`) is a target too, for a satellite that reports `session_homes`. A push for a home that is gone (the session closed) writes nothing and makes no folder; its `ok` ack carries `note: "session gone, not written"`. No part of the folder's path may be a link, and a link at the file's name is replaced, never written through. These files never sync, so this push is their only delivery. The running CLIs pick the rewrite up on their own. From 0.5.171 it also rewrites the credential of every live headless session whose home is the target folder, so a Codex revive writes the current token. |
| `mcp_gateway_token` | (0.5.132) One vendor MCP credential for the satellite-local gateway: `{session_id, token_hash, mcp, header, value, expires_in, upstream}`. See "The credential gateway". `expires_in: 0` is a revoke. From 0.5.140 it carries the refusal's `kind` and `reason` and leaves a tombstone, so the session's next request is refused at once with why (reconnect the account in Settings, Integrations). The platform sends a machine's revokes again right after it reconnects. From 0.5.175 `mcp` may be a placeholder (`oto-exit-<32 hex>`) in place of an MCP name: an entry of the session's credential exit, held in the same table under `(token_hash, placeholder)` and read by the machine's exit per request, with the same lease, renewal, revoke and wipe; `header` is the declared header, `value` the value with the tool's prefix stripped, `upstream` the host as an https origin. A compute machine takes these frames for the exit's entries alone. |
| `mcp_gateway_wipe` | (0.5.132) Remove a session's pushed credentials, by `token_hash`, or by `session_id` when the hash is absent. Sent at the session's close or purge. |
| `request_manifest` | Request the file manifest for a sync. An optional `page_size` asks for pages, and `report_skipped: true` (0.5.145) asks for the `skipped` entries too. The platform asks `report_skipped` in its merge and in a folder-app deploy's listing. Without it the reply never holds an entry without a hash. |
| `file_push` | Push a file, written beneath the agent's folder through `host/safe_fs.py` with no link followed: a temp file, fsync, rename in the parent (or append to `<path>.partial` and rename on the last chunk). A link on the way is an error. With a `command_id` the satellite acks after the write, a write barrier for the platform. A write frame from a 1.9.0 platform carries `expect`: the stat of this machine's copy the platform knows (`{size, mtime_ns}`, the manifest's rounding allowed) or `"live"` when it knows none. A regular file at the name whose stat differs, or one written within the last 5 s by this machine's clock when the platform knows none, is not replaced (0.5.150, `LIVE_WRITE_WINDOW_S`): the ack is `stale`, the staged bytes are removed, and a chunked push is refused at its first chunk and checked again at its last. A missing file, a link or a directory at the name is never stale; a frame without `expect` commits as before. A write into an engine home outside a person's own folders (`transport/file_sync.is_unsynced_engine_home_path`) is refused with `status: "rejected"`: those files are this machine's, and a platform before 1.9.0 still sends them (0.5.150). From 0.5.156 the path is canonicalised before that check (on Windows trailing dots and spaces dropped from every segment first, as Win32 drops them; `.` and empty segments dropped; the case folded where the filesystem folds it) and a `satellite_host` write landing under an engine home inside the agents tree is refused the same way, the paths compared canonical (realpath on both sides, the path taken as given and resolved, so an alias of the agents folder or a link inside it is caught): only `session_move` places a chat's session files. |
| `file_pull` | Pull a file (clamped to the agent folder, streamed from a descriptor opened beneath the agents root with no link followed). From 0.5.137 each block is read off the event loop and the pull is registered by its `request_id` until it ends. |
| `file_pull_cancel` | (0.5.137) `{request_id}`: stop a pull the platform gave up on and drop its queued chunks. An unknown id is ignored. |
| `file_stat` | A cheap revalidation probe for a cached file, answered as an ack and checked as `file_pull` is. |
| `session_move` | (0.5.156) A chat's engine session files, moved with the chat to another place: `{op, engine, agent_slug, username, refs, move_id?, rel?}`. The platform names the engine (a row of `engines.py`), the agent, the scope's user (empty: the `workspace` home) and the session's ids (`refs`: the session id and the thread id, UUID-shaped); the satellite resolves the home itself and accepts a file only where the engines' own layout names it a file of those ids (`host/session_retention.classify`): Claude's `.claude/projects/<key>/<id>.jsonl` with the `<id>/` folder beside it, Codex's `.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<thread>.jsonl`. `list` answers `{files: [{rel, size, sha256}]}`; `stat` (no `move_id`) answers `{files: [{rel, size}]}`, sizes only: the session files themselves (no file of a sibling folder), each size from an lstat that follows no link (a link or anything but a regular file left out), no hash; `pull` (with `rel` and a `request_id`) streams `file_content` as `file_pull` does, from a regular file with one link; `push` (chunks with the hash on the last, acked one by one, written in arrival order) stages beneath `<agent>/.session-move/<move_id>/`, the `.partial` name unlinked at the first chunk and a staged file with more than one link refused; `commit` puts the staged files in place after removing every other copy of the session under the home (one copy per home: a second one makes Claude's resume by id fail) and answers the placed files; `discard` drops the staging; `delete` removes the session's files. Every op works in the engine's own folder: another engine's file of the same id is never listed, removed or replaced. `list`, `pull`, `commit` and `delete` answer `busy` while a live session on the machine holds one of the ids; `stat` and `discard` never do (the move's proof reads a transcript a live session still appends to: its growth is the proof). A staging folder a day old is swept at the satellite's start and at a move's first push into the agent. Inline in the link's loop: a pull is registered before the next frame is read. Nothing on the way is a link (`host/safe_fs.py`). A Claude transcript whose full path would pass 240 characters is placed under the key `-m`. |
| `sync_mcps` | Batched MCP install, update and removal: per-MCP gzipped base64 tarballs, the manifest, `source`, `source_build`, `version_hash`, `system_requirements`, and from a 1.7.1 platform `remove_any_category`. Answered with `mcp_install_progress` while it works. See "MCP installs". From 0.5.183 a spec may carry `pin`, a result `resolved_version`, and the frame `egress` for the compute machine that keeps its own tools. |
| `sync_mcps_verify` | Compute a fresh `version_hash` for every installed MCP and report it in the ack. Sent on reconnect and before every `sync_mcps` diff. The compute machine that keeps its own tools reports its community tools alone (0.5.183). |
| `tool_open`, `tool_in`, `tool_close` | (0.5.174) A compute machine alone: Direct LLM's tool processes. See "Direct LLM's tool processes". |
| `step_run` | (0.5.122) An app step or a check's script. See "App steps". From 0.5.174 a compute machine takes only the form with a `sandbox` key; a paired machine refuses that form. |
| `app_server_start` | (0.5.174, compute machines alone) An app server: `{server_id, boot, app_id, instance, sandbox, argv, env}`. The ack carries `host_port` (the relay's) and `pid`; an error ack says why, the tail of what it printed included. |
| `app_server_stop` | (0.5.174) `{server_id}`: TERM to the group, 5 s, KILL. Always acks, `stopped` false when it is not here. |
| `app_server_sweep` | (0.5.174) `{boot}`, sent at a compute machine's sign-in: stops every server another boot started. The ack's `live` lists the rest, those still starting included. |
| `pty_open` | Spawn an interactive engine under a PTY (the terminal form of `start_session`). The ack carries the `pid`. From 0.5.171 it carries `open_token`, which the ack and the exit repeat; a PTY still held under the id is closed first, and its close leaves the new one's secret files. A Claude Code terminal on an API key gets the key under `OTO_CLAUDE_API_KEY` and a key helper on its command line (`--settings`), never the key in `ANTHROPIC_API_KEY`. Carries `ssh_agent` (0.5.173) and `exit` (0.5.175) as `start_session` does. |
| `pty_input` / `pty_resize` / `pty_close` | Terminal keystrokes, a resize, a close. From 0.5.171 a `pty_close` that names another open's `open_token` leaves the live PTY. |
| `transcript_ack` | `{session_id, offset, open_token}` (0.5.171): the platform took a terminal's transcript lines up to `offset`. No reply. An ack for another open is ignored. |
| `pty_local_detach` | The dashboard took a session over: detach the local `otodock` terminal and keep the PTY alive. |
| `pty_inject` | A platform prompt written into the stdin of a CLI attached through the `otodock` command (how a delegated result reaches it). Answered with `pty_inject_result`. |
| `local_session_opened` / `local_session_listed` / `local_session_error` | The answers to the `otodock` command's requests (keyed by `request_id`). |
| `policy_update` | A live refresh of the machine's path policy (`allow_full_fs`, `device_grants`). |
| `check_session_resumable` | Whether a chat's CLI or Codex session can be resumed on this machine. From 0.5.150 the ack carries `reason` (`missing`, `empty`, `unreadable`) beside `resumable: false`, a read fault is an error ack, and the transcript is read in a thread. From 0.5.156 an unsafe agent slug, session id or username is answered `missing` without a lookup. |
| `session_files_retention` | Remove the CLI transcripts under this machine's agent trees that no chat and no live session names (0.5.150, the platform's retention sweep): `keep` is every session and thread id the platform still needs here: the rows pinned to this machine, the unpinned rows and the scheduled tasks' continued sessions, and the sessions live on this machine; `min_age_s` is the grace a file must be older than by this machine's clock (never under an hour; seven days when the frame names none), `dry_run` lists and counts instead of deleting. A frame without a `keep` list, or with an empty one outside a dry run, is answered with an error and deletes nothing. Answered as an ack with `files` and `bytes` held after the pass, `deleted`, `deleted_bytes` and up to 50 `candidates`. |
| `uninstall` | Self-uninstall and exit (the machine was deleted in the dashboard). |
| `update_required` | Auto-update: apply the pushed tarball, swap the install folder atomically, restart. At the auth the platform sends it after `auth_result: ok` (with empty `cli_pins`). In its first 10 s of serving (`UPDATE_HOLD_S`) it first reads the socket for the rest of that window, and a disconnect there drops the push. It then waits up to 20 s for a 0.5.140 satellite's `update_received` and closes with 4007. It does not wait for the update to be applied. From 0.5.140 the satellite answers `update_received` as soon as it reads the frame, before it applies it. From 0.5.187 a frame for the running version is answered with `update_received` and not applied. So is a frame that comes while an update is being applied. |
| `http_response` / `http_response_chunk` | A tunneled HTTP response for the waiting subprocess. |
| `pong` | The heartbeat answer. |
| `platform_stopping` | The platform is about to stop for a planned restart (0.5.170): `back_within_s`, a whole number of seconds from 0 to 3600, is how long at most it expects to be away. No reply. The satellite keeps the time until its next sign-in, and while it stands the loopback tunnel adds `hold_s` (the seconds left) to its 503 `tunnel-not-connected` and to the 502 of the calls the drop cut, so a held permission call waits that long when that is longer than its two minutes. 0 takes an earlier frame back. Any other value is ignored. |

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
path, except for a credential an agent's GitHub reach narrows: its entry takes
the tunnel's `/mcp/<name>/` shape on every version, the platform judges and
forwards it, and its token is never pushed to the machine. The tunnel server mounts a handler under `/v1/mcp-gateway/`
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
- (0.5.175) The table also holds the session's **credential exit** values,
  pushed as `mcp_gateway_token` frames whose `mcp` is the placeholder; the
  machine's exit (`sessions/credential_exit.py`) reads them per request,
  waits up to 3 s for a push in flight, answers 403 with the refusal for a
  revoked one and 403 with its no-value sentence for one that never came,
  and fetches nothing itself. A compute machine, which holds no HTTP MCP
  lease, takes these pushes for the exit alone.

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
behaviour facts. `turn_replay` is the tagging and retention buffer of each
turn (Claude Code and Codex). `replayed_by_default` names the engine that a
platform sending no `replays_turns_of` replays (Claude Code). A live session
is listed in `sessions_alive`'s `sessions` only when its engine has
`turn_replay` and the sign-in's list names it. `revives_dead_process` is Codex
warming a dead daemon again. An id the table lacks is refused with an ack error before anything
spawns. The same table drives the credential clamp, the CLI pin reconcile and the
`installed_clis` and `engines` capabilities and the `cli_status` frame. The proxy's
`tests/remote/test_satellite_engine_table.py` checks every shared fact against
its engine descriptors.

### Compute mode (0.5.172)

With `[satellite] kind = compute` in `satellite.conf` the process is a compute machine: the platform's
own, which runs the platform's sessions in the platform's sandbox and reads the platform's agents and
tools folders itself (`agents_dir` and `mcps_dir` name shared mounts of them). The renderer's preflight runs
before the sign-in, and the process exits with the reason when the machine cannot run the sandbox. A
`start_session` or `pty_open` payload then carries `sandbox` (the proxy's description of the session's
sandbox), `argv` (the command with the engine's binary by its bare name), `env` (the environment the process
runs with, exactly), `cwd` (the sandbox's folder) and `tunnel_port` (the local link the proxy port is spliced
onto), from 0.5.173 with `ssh_agent` and `exit_port` possible in the `sandbox` (the file name of the session's
SSH agent socket in `~/.oto-dock/run/ssh-agent/`, which the satellite opens ahead of the render from the payload's
`ssh_agent` field and the render binds at `/tmp/oto-ssh-agent.sock`; a loopback splice for the credential exit,
from 0.5.175 served by this machine's own satellite, which splices it onto its listener's port and binds the
machine's bundle at `/tmp/oto-exit-ca.pem` (`credential_exit.render_values`; a description that names the exit
while the exit is not up is refused), and which the platform sends only to a compute machine whose heartbeat
reports a live exit; a 0.5.172 render refuses a description that
carries either), and none of the files a paired machine is sent: no hook scripts, wrappers, MCP config, login file
or prompt. The satellite renders the description with its own values (`_vendored/sandbox_render.py`,
the proxy's module byte for byte, and `_vendored/oto-sandbox-net`), resolves its pinned binary into the argv
and spawns with no cwd of its own. It writes nothing into the agents folder (an unknown agent slug is refused,
never made), translates no path, scans no file and wipes nothing at close; a Codex thread starts in the
sandbox's folder and the approval and question bridges post to the machine's own link. The link takes no
code and no uninstall (`update_required`, `uninstall` and the 4006 close are logged and the connection
retried), and `file_push`, `file_pull`, `file_stat`, `request_manifest`,
`session_files_retention`, `session_move` and `credentials_update`
are refused with an ack error. `sync_mcps` and `sync_mcps_verify` are refused too, except by the install's
own machine once it keeps the community tools itself (`tools = own`, 0.5.183): it takes both for its
`mcps/community` folder alone. `mcp_gateway_token` and `mcp_gateway_wipe` reach it for the credential exit's
values alone (0.5.175). The credential gateway's path is tunneled to the platform's gateway instead of
served here, so a compute machine holds no HTTP MCP lease. A satellite that is not a compute machine refuses to start on an agents folder the platform
marked as its own (`.otodock-platform`).

That is a compute machine beside the platform, which shares its folders. One on another server
(`[satellite] files = own`) keeps its own copy of the folders. It claims `compute_files: "own"` at its sign-in,
and the platform admits the link only when that matches its row. Its link takes the file, tool, sweep and
login file frames as a paired machine's link does, and refuses only `session_move`. Its `start_session` and
`pty_open` carry `host_files` and `host_folders`, which it writes under its own agent folder before the
render and removes at the close. Its sessions report their changes as `file_changed`.

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

The headless spawn's permission flag comes from the payload: `--permission-mode
plan` in Plan, `--permission-mode auto` when `cli_permission_mode` is `auto`
(0.5.156: the CLI's classifier judges what the hook leaves undecided), and
`--dangerously-skip-permissions` otherwise. All three pass
`--permission-prompt-tool stdio`. The CLI prints `control_request.can_use_tool`
under `use_native_permissions`, on its own under `--dangerously-skip-permissions`
for some calls (a background subagent's call, a dangerous `rm` inside
`bash -c`), and in auto after its classifier declines several actions in a row
(the platform refuses that one at once, with a message the model reads). The satellite forwards the request
unchanged and writes the platform's
`control_response` to stdin (`send_permission_response()`), in the CLI's own
shape from 0.5.140 (it remembers the last 64 requests' inputs, so an allow
carries the input the CLI printed).

### Codex CLI (`codex-cli`)

A persistent `codex app-server` JSON-RPC daemon (`sessions/codex_session.py`).
The satellite:
1. Writes the hook scripts into `.codex/`, and `hooks.json`, `config.toml` and
   `auth.json` (or removes a stale one) into the session's home when the start
   asks for one, else into `.codex/`. The payload's `codex_session_text` makes
   the session's text its own, so no `AGENTS.md` is written. The `config.toml`
   is composed here: the app-server header (`project_doc_max_bytes`,
   `[memories]` off, the plan tool on, one `[features]` table with
   `plugins = false` and `hooks = true` for unattended sessions and, from
   0.5.143, attended chats), the platform's `[mcp_servers.*]` sections
   (`~` expanded, env paths translated, `DISPLAY` injected,
   interceptor-wrapped; a headless session's carry the per-tool
   `approval_mode` tables where Codex's own MCP rule and the platform's tier
   disagree) and, on a local endpoint, the `oto_local` provider.
2. Spawns the daemon once, then `thread/start` (or `thread/resume` for a
   persisted thread, with `excludeTurns` from 0.5.140) with the mapped
   `sandbox_mode` and `effort`, the approval policy
   and reviewer from the vendored `codex_approvals`, read from the shipped
   platform mode with an older word taken as its current one
   (`_platform_mode`): in Auto `on-request` with `approvalsReviewer:
   "auto_review"`, Codex's own reviewer judging what crosses the sandbox;
   in every other mode the sandbox's own policy (`never` under
   `danger-full-access`, `on-request` otherwise) with `approvalsReviewer:
   "user"`, the bridge taking the requests) and, for unattended
   sessions and from 0.5.143 attended chats, `config = {"bypass_hook_trust":
   true}`. It then waits for the configured MCP servers to start.
3. Reports the thread id (`codex_thread_id`). A turn that finds the daemon
   dead re-warms it. When `thread/resume` is refused and a new thread
   starts, the new id goes to the platform before `turn/start`.
4. Each `turn/start` asks for the reasoning summary (`summary: "auto"`,
   0.5.158) unless the session saw it refused: a turn whose final error or
   failed completion says the organization is not verified for summaries is
   dropped and run once more without it, and the session stops asking.
5. On each `send_message`, `turn/start` with the turn's approval and sandbox
   policy. A forwarder ships every daemon notification verbatim, a
   background sub-agent's events after the main turn included. Approvals
   and questions go to the platform over the tunnel (`/v1/hooks/permission`,
   `/v1/hooks/codex-question`), each approval named as a tool by the
   vendored `codex_approvals` bridge (a command as `Bash` or `PowerShell`, a
   file change as `Write`, a sandbox escalation as `CodexEscalation`, and
   since 0.5.135 terminal input to an elevated command as
   `CodexTerminalInput`). While the platform cannot be reached a request is
   held and retried for up to 120 s, or for the `hold_s` the tunnel names when
   that is longer (a 401 gets a 2, 4, 8 s ladder), then declined, as the permission gate does.

No effort mapping or sandbox default is decided here: the platform is the
single source, including which sessions run the hook floor
(`codex_hooks_floor` for unattended sessions and `codex_attended_floor` from
0.5.143 for attended chats). From 0.5.156 the gate judges every call of both,
and the daemon env never carries `OTO_HOOK_BRIDGED`.

### Interactive terminals (`pty_open`)

Both engines can also run as a full interactive TUI under a real PTY,
driven from the dashboard's terminal or a local `otodock` command
(`pty_relay` on Unix, `winpty_relay` / ConPTY on Windows, and `pty_session.py`
and `codex_pty_session.py` on the shared `pty_session_base`). The Claude TUI takes
the headless argv without `-p` and the stream flags, with a real
`--permission-mode` (`terminal_permission_mode`: the payload's
`cli_permission_mode` when it is `auto` or `plan`, else `default`; from an older
platform, its mode word when the CLI knows it). The Codex TUI passes
`--no-daemon` on the fresh and the resume form (0.5.135), and in Auto
`-c approvals_reviewer="auto_review"` (0.5.156) and `-c approvals_reviewer="user"`
in every other mode (0.5.164). Output streams back
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
websocket (`local_session_*` frames). `--mode ask|auto|dontAsk|plan` (default
`auto`) picks the chat's permission mode and is sent as the platform's word
(`ask`, `autoReview`, `dontAsk`, `plan`; the older `default` and `acceptEdits`
are taken too). **The platform derives the identity from
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
- On a paired computer the script runs as the satellite's own user, like every
  session there. On a compute machine `step_run` carries `sandbox`, `argv`, `env`,
  `timeout` and `run`. The platform staged the script in the agent's folder, and
  the machine runs the command in the sandbox.
  Only the script travels.

### Direct LLM's tool processes (0.5.174)

Direct LLM's model loop and its MCP client stay in the platform. On a compute machine each stdio tool of
such a session runs here. So do its file tools (read, list, search, skill, write, edit, delete), as one
program of the platform's own whose text rides the open in `OTO_PROGRAM`. Only their lines cross the link
(`sessions/tool_relay.py`). A paired machine
refuses `tool_open` with an error ack.

`tool_open` carries `session_id`, `server`, `key` (32 hex the platform minted for this child),
`sandbox` (the session's description), `argv`, `env`, `tunnel_port` and, from 0.5.175 when the machine passes the exit's rule,
`exit` (the session's door at this machine's credential exit, the shape `start_session` carries: the machine
opens it before the render, and drops it with the session's last tool here and with the link). The machine renders the description
with its own values around the argv, one launcher per child (its own network namespace), spawns it at
`nice 10` in a new process group with the environment as sent, and answers the ack. It keeps
`(session, server) -> (key, process)`. A second open of the same pair, a description the render refuses or
a failed spawn answers an error ack.

`tool_in` carries one JSON-RPC line for the child's stdin and is written in arrival order. `tool_close` ends
the child's stdin after what is queued and its process group two seconds later. `tool_out` (a stdout line)
and `tool_exit` (`code`, an integer or null, and `reason`, empty on its own end) ride the bounded bulk lane,
so a chatty tool holds back itself and never another session's control frames, and `tool_exit` cannot pass
the output. A `tool_in` or `tool_close` that names a child this machine does not run, or another key, touches
nothing and is answered by a `tool_exit` with the reason `not running`.

A frame is at most 12 MiB encoded (the link takes 16 MiB, and encoding grows Greek text threefold). A longer
line is not sent: the machine reads the JSON-RPC `id` from its first 4 KB or its last 256 bytes and answers a
JSON-RPC error ("The tool's answer was too large to send."), or, with no id there, ends the child.

The children belong to the link. When it drops, and when the satellite stops, every child ends. Nothing is
reported in the alive report: after a platform restart a Direct LLM session is rebuilt, never taken back.

## File sync

Agent folders are synced continuously, in real time during a turn:

| Folder | Direction | Authority |
|-----------|---------------|-----------|
| `config/` | platform to satellite | the platform (admin-managed, pushed at session start) |
| `workspace/` | both ways | last writer wins |
| `users/{name}/` | both ways | last writer wins |
| `.claude/`, `.codex/` (anywhere in the path) | platform to satellite | the platform (regenerated each session); under an engine home (`workspace/.claude`, `users/<u>/.codex`) only a person's own folders sync (`skills`, `commands`, `agents`, `output-styles` under `.claude`; `prompts`, `skills` under `.codex`), the rest is this machine's (0.5.150), except a chat's session files a move places (`session_move`, 0.5.156) |

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
  the snapshot so it never echoes back as a change. The scan runs in a thread
  beside pushes. A push marks its path while it writes, and the scan moves a
  path's entry only when no push or other scan spoke for it since it began.
- **What the scan lists is what it reads.** The walk lists by name. Every
  file it hashes or sends is opened beneath its hold on the agent folder,
  with no link followed on the way. On Windows it is opened by name, without
  waiting. Every folder it visits is checked where it stands, and one swapped
  for a link is a skipped `link` folder. An inline `file_changed` carries the
  hash of exactly the bytes it carries. No frame changes shape.
  A platform from 2.0 applies an inline write only when its `hash` is the
  sha256 of its bytes. An older satellite whose file changed between its
  hash and its read reports it again at the next turn's end.
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
- **A push never replaces a file this machine is changing** (0.5.150). The
  platform's write frames say which copy they expect to replace (`expect`,
  above); a copy that moved since, or one written moments ago, stays, the
  ack says so with the copy's stat, and the platform keeps its own copy and
  compares both at the next merge, newest wins. A script appending to a log
  under the workspace keeps its file and its later lines (before, the
  rename left it writing an unlinked inode). The window between the stat
  and the rename is narrowed, not closed, and a delete of a live file still
  removes it.

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
the same values it would see in the platform's sandbox on a compute machine.

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
  `mcp_installer.prune_uv_pythons`).
- From 0.5.160 a fresh Python venv runs on the satellite's own base
  interpreter on a POSIX host (`mcp_install_support.mcp_python_spec`).
  Windows keeps uv's default. A failed install's `error`, in
  `mcp_install_progress` and in the ack's `results`, is the log's cause
  lines (`install_failure_cause`: the network, disk and file-lock lines
  first, at most 600
  characters, no exception prefix). The platform tells the session that
  cause and reads it to decide whether it skips that MCP for an hour. The
  startup reconcile rebuilds a venv on an older minor, and on macOS also one
  on a newer minor, keeping the old venv aside
  (`mcps/.venv-aside-<category>-<name>`) until the rebuild succeeds.

## Resilience

- **Reconnect:** jittered exponential backoff from 1 s. A refused or failed
  connect doubles the wait up to a 30 s cap (0.5.154: a refused or a 1013 auth
  keeps doubling). From 0.5.170 a sign-in alone no longer resets the wait:
  it goes back to 1 s only after a link that held 120 s, and a link that
  signs in and drops sooner keeps doubling it, capped at 10 s. A machine
  whose link keeps dropping therefore reconnects every few seconds, not
  every second. The platform may close a link with 4008 when the machine
  reads slower than its queue fills: the satellite reconnects as after any
  close and catches up.
- **Lines held after a sign-in.** A `session_event` for a session the platform
  has not taken back yet waits on the platform. It waits only for a session the
  platform placed on that machine, and for 120 s at most. At most 64 sessions, 1,000 lines each, 10,000
  lines and 32 MiB are held for a machine. A terminal's `transcript_lines` wait
  in the same store.
- **Send buffer:** up to 10,000 messages kept during a disconnect and
  replayed on reconnect. Terminal output rides its own lossless lane.
- **Bulk lane** (0.5.137, `paced_transfers`): a pull's chunks ride a third
  lane of 3 frames, drained after control and terminal frames (one bulk
  frame after every 8 terminal frames while both wait), so heartbeats, acks
  and hook calls are not stuck behind a whole file on a slow uplink. A
  disconnect stops the running pulls and purges the lane, and
  `TCP_NOTSENT_LOWAT` (128 KB, Linux and macOS) keeps the kernel's unsent
  bytes small so the websocket's own pong never waits long.
- **Heartbeat** every 20 s with CPU and memory load and the local link's
  state. Each tick checks the loopback tunnel first and rebinds it when its
  listener died (0.5.168), on the same port when it can. From 0.5.170 the
  frame goes out before the tick's other work: the per-agent fingerprints
  and the screenshot sweep run in a task of their own, at most once a
  minute, so a large tree's walk never makes a heartbeat late (the
  platform drops a machine it has not heard from for 75 s).
- **Shutdown:** SIGTERM / SIGINT closes every session before exit. At
  start, processes left by a crashed run are killed.

## Versions

The satellite sends `satellite_version` (`satellite/config.py::SATELLITE_VERSION`)
in `auth`. The platform reads two values, each defined once:

- **`MIN_SATELLITE_VERSION`** (`proxy/ws/satellite.py`), the floor. A
  satellite below it with auto-update on is pushed the update at its first
  sign-in. One with auto-update off or paused is refused with an upgrade
  error until an admin clicks Update now. So is one on an opted-in
  plaintext link, which never gets the update pushed (re-run the installer
  there). The platform keeps the refusal in memory
  (`services/remote/machine_away.py`). The machine's card says "Update
  needed", and a chat aimed at it says it needs an update.
- **`SATELLITE_VERSION_LATEST`**, read from `satellite/config.py` at import. A
  satellite below it is **updated** over the websocket (a tarball push), not
  refused, except on a plaintext link, where it runs its own version. The
  update ends the sessions the machine runs, so a machine that signs in
  running one (its `active_sessions`) connects on its version and is pushed
  the update at the first heartbeat that reports none. Below the floor, on
  Update now, or from a satellite that sends no count it is pushed at the
  sign-in. The satellite reaps its child tree, the engines included, before
  it restarts (`transport/lifecycle_update.py`).
  It replaces the satellite's code (and rebuilds its venv when
  `requirements.txt` changed). The Claude Code and Codex CLIs follow the
  platform's pins on every reconnect, and the other baseline tools move only
  when the install command runs again.

Every change to the satellite's code bumps `SATELLITE_VERSION` (it drives the
update). A major release (`X.0.0`) raises `MIN_SATELLITE_VERSION` to its own
`SATELLITE_VERSION`. The release gate refuses a major release with a lower
floor. Any release may raise it when it removes a path older satellites
need (VERSIONS.md, "MIN_SATELLITE_VERSION").

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

The modules below are byte-for-byte copies of proxy source in `_vendored/`, so both
sides run one implementation. `scripts/sync-satellite-code.sh` copies each and
writes its sha256 into a `SHARED_*_HASH` constant in `satellite/config.py`. At
start (`__main__._verify_installer_drift`) the satellite hashes each copy and
exits with an error on a mismatch. Some are hashed as files and not imported first. These are the
credential exit's three, `mcp_system_deps.py`, `mcp_pin.py` and the launcher `oto-sandbox-net`. The reason: they import packages a venv may lack, and a drift
is never skipped for want of an import.

| Vendored copy (never edit) | Proxy source |
|---|---|
| `_vendored/mcp_installer.py` | `proxy/services/mcp/mcp_installer.py` |
| `_vendored/stdio_path_interceptor.py` | `proxy/core/stdio_path_interceptor.py` |
| `_vendored/app_server_client.py` | `proxy/core/layers/codex/app_server_client.py` |
| `_vendored/codex_approvals.py` | `proxy/core/layers/codex/codex_approvals.py` |
| `_vendored/terminal_queries.py` | `proxy/core/terminal_queries.py` (the terminal query and reply strips, the clipboard-write strip and the mouse-report pattern from 0.5.125, and the bracketed-paste mode tracker of the scrollback rings from 0.5.140) |
| `_vendored/layout.py` | `proxy/core/layout.py` (the agent tree: the folder names, the per-user tree, the virtual roots and `host_of_virtual`, `user_of`, `state_dir`, from 0.5.126) |
| `_vendored/loop_watchdog.py` | `proxy/core/loop_watchdog.py` (the event-loop watchdog: a late tick logs the loop thread's stack, the watcher's own late wake with no CPU spent names a frozen process; the daemon runs it at its own cadence, from 0.5.150) |
| `_vendored/exit_relay.py` | `proxy/core/credentials/exit_relay.py` (the credential exit's relay: an HTTP CONNECT proxy on the loopback, TLS ended with the authority's leaf for a declared host, the placeholder swapped in the declared header alone; `sessions/credential_exit.py` is this machine's backend, from 0.5.175) |
| `_vendored/exit_authority.py` | `proxy/core/credentials/exit_authority.py` (the exit's certificate authority: a key and a root under `<otodock dir>/credential-exit/`, a leaf per host, the trust bundle, from 0.5.175) |
| `_vendored/session_text.py` | `proxy/core/layers/codex/session_text.py` |
| `_vendored/sandbox_render.py` | `proxy/core/sandbox/sandbox_render.py` |
| `_vendored/host_resources.py` | `proxy/core/sandbox/host_resources.py` |
| `_vendored/session_home.py` | `proxy/core/sandbox/session_home.py` |
| `_vendored/mcp_system_deps.py` | `proxy/services/mcp/mcp_system_deps.py` |
| `_vendored/mcp_pin.py` | `proxy/services/mcp/mcp_pin.py` |
| `_vendored/oto-sandbox-net` | `proxy/scripts/oto-sandbox-net` |
| `_vendored/outbound_url.py` | `proxy/services/infra/outbound_url.py` (the outbound-URL validator and the sandbox shim's address rule, `sandbox_dial_refused`, which a compute machine's exit applies, from 0.5.175) |

```bash
# after changing any of these proxy sources:
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

Additive unless it says otherwise. The floor, `MIN_SATELLITE_VERSION`, was
raised to 0.5.76 for the OAuth file delivery, to 0.5.171 and to 0.5.196 (2.0,
the major release's floor).
The versions the table skips carried fixes inside the daemon, CLI pin moves
or new routes on the tunnel allowlist.

| Version | What changed on the wire or on the machine |
|---|---|
| 0.5.196 | The floor: `MIN_SATELLITE_VERSION` is 0.5.196 (2.0's own satellite), and the platform keeps no branch for the exit's or the SSH agent's version. The capabilities and the heartbeat carry `exit_why` and `exit_upstream` beside `exit_port` and `exit_ok` (additive). An exit whose packages finish installing after an update sends a heartbeat out of turn. The exit's tunnels (`CONNECT`) and forwards go through the machine's own outgoing proxy for https, with Basic authentication alone. That is its `HTTPS_PROXY`, else its proxy for all, with its `NO_PROXY` honoured, and on Windows and macOS the system's settings too. A SOCKS proxy keeps the exit off (`socks`). The exit writes the proxy's address to no file, no log and no status report. Through such a proxy the proxy resolves the target's name. A compute machine first checks the target by the sandbox's rule where it can resolve it (`upstream_admits`, best effort). `auth_result` may carry `egress_proxy`, to the install's own compute machine alone: the platform's outgoing proxy, its `NO_PROXY` and its trust files. That machine's exit uses them in place of its own settings, the trust files beside the system's in its forwards and its sessions' bundle. A tool whose key goes to a host under an engine's domain presents the proxy user `oto-tool` (`exit_relay.TOOL_PROXY_USER`), with the same secret. The exit ends such a host for that user alone and passes it as a plain tunnel for `oto`. The session-file fetch is gone: a start carries no `session_files_token` or `session_files_env`. The tunnel forwards no `/v1/hooks/session-files`, and no private key reaches the machine. |
| 0.5.195 | Additive. The capability `between_turns_stream`. With `stream_between_turns` on `stop_turn` a Claude session forwards every line after the turn with no time bound, until the next `send_message`, its process's end or its close, and takes a `steer_turn` while a turn the engine started by itself runs. A machine that reports no such capability is never sent the field. |
| 0.5.194 | Additive. A Windows machine serves a session's SSH agent on a named pipe and sets `OTO_SSH` and `GIT_SSH`. An `exit` entry may carry `streams` and `judged`. |
| 0.5.192 | Additive. The capability `session_home_dirs`. A Codex start or terminal open may carry `session_home`. |
| 0.5.187 | Additive. An `update_required` frame for the version the satellite already runs is answered with `update_received` and not applied. So is a frame that comes while an update is being applied. At the auth such a frame ends that connection and the satellite connects again. |
| 0.5.185 | Additive. A Claude Code session watches its process. When its engine ends with no turn reading it, it sends `session_ended` with `reason` (`exited` or `memory`), `exit_code` when known and its `incarnation`. The `ok` ack of a `start_session` names the session's `incarnation`. The platform ignores the frame for an earlier object of the id, else reads the session dead and closes it at once. `turn_ended` may carry `reason: "exited"` with `exit_code` and `last_line`, or `reason: "interrupted"`: a Codex daemon that did not take the platform's interrupt was dropped, once however many Stops came, and its thread kept. A Codex Stop during `turn/start` waits up to 2 s for the turn's id. Every session process on a Linux or macOS machine runs at nice 10. A compute machine starts at most `auth_result.session_starts` of them at once. Every other machine, and a compute machine sent no number, keeps half its CPUs, at least 2. An older platform ignores the new keys and sends no number. |
| 0.5.184 | Additive. A machine that reports the capability `session_homes` runs each Claude Code session from a home of its own (`<scope>/.claude/own/<session id>/`, `sessions/session_homes.py`): `start_session` and a terminal's open may carry `session_home`, `credentials_update` may name that home as its target, and its ack carries `note: "session gone, not written"` for a home that is gone (nothing is made). A start or an open of an id waits for a close of the same id that still runs (`sessions/close_hold.py`). A compute machine's sandbox description carries `home_masks`, which an older render refuses: a compute machine signs in only on the platform's own version. A machine that does not report the capability is driven exactly as before. |
| 0.5.183 | Additive. The install's own compute machine keeps the community tools itself (`[satellite] tools = own` beside `kind = compute`, never with `files = own`). It claims `compute_tools: "own"` at its sign-in. Its link then takes `sync_mcps` and `sync_mcps_verify` and still refuses every file frame, the login file and the session move. There `sync_mcps` installs and removes in `mcps/community` alone. A spec of another category is answered "This machine keeps the community tools alone." and a removal looks in no other folder, also with `remove_any_category`. Its start sweeps that folder alone: the staged folders, the build scratch, the unused interpreters. It sweeps nothing of the agents folder. On every machine a `sync_mcps` spec may carry `pin: true`. After a build that succeeded the machine then settles the staged folder to the version the build read back (`_vendored/mcp_pin.py`: the manifest pinned, a Node tool's `package.json`) and hashes it again, before the swap. Only the platform's own install of a tool sends it. Each install result names the version the build read back as `resolved_version`, a plain version or absent. The frame may carry `egress` for the machine with `tools = own`: `env` (the names of `BUILD_EGRESS_NAMES` and `BUILD_SETTING_NAMES`, a value of at most 4096 characters), `trust` (at most 8 entries `{env, name, data_b64}`, `env` a name of `BUILD_TRUST_NAMES` other than `SSL_CERT_DIR`, `name` a bare file name, 1 MiB decoded at most), `cert_dir` (the certificates of the folder `SSL_CERT_DIR` names: at most 512 entries `{name, data_b64}`, `name` a hashed file name of `BUILD_TRUST_DIR_FILE`, 1 MiB decoded together, kept whole or not at all), `forwards` (at most 16 ports as digits, 1 to 65535) and `allow_hosts` (at most 32 IP addresses, never loopback, unspecified or a metadata address). What lies outside the bounds is dropped and logged by its name. The block is the platform's: the machine holds it in memory for the builds of that frame and forgets it when the frame ends (`sandbox_host.note_egress`, `clear_egress`). It writes no value of it to a file, a log or an argument, and its start builds no community tool by itself: a tool whose interpreter is gone is left for the platform's next frame. Such a machine's builds open what the block names and nothing else. They never use the render's own carve rule. A machine on another server and a paired computer do not read the block. `sync_mcps_verify` hashes off the event loop on every machine. On the machine with `tools = own` it reports the community category alone, and a tool there is also `healthy: false` when its manifest does not parse or it has no environment: a `python` tool from `pypi:` or `git+` whose `venv/bin/python` resolves to no file or to the host's own Python while the venv was made for another minor, a `node` tool from `npm:` with no `node_modules` or none that holds its own package. `tool_open` may carry `tool_dir`, the folder of a community tool. When the folder is not there the open is answered "This tool is being installed. Try again in a minute." and nothing is spawned. A `tool_dir` outside the machine's tools folder is refused as malformed. The number is set at the merge. |
| 0.5.180 | Additive. The capability `start_context_hook`, and `start_context_parts` in a start. |
| 0.5.178 | Additive. The capability `tools_listed`, and `codex_session_text` in a Codex payload. |
| 0.5.177 | Additive, compute machines on another server alone. A machine its host service started (`OTODOCK_COMPUTE_HOST_SHAPE`, `OTODOCK_COMPUTE_IMAGE` in its environment) claims `compute_host` (the host service's shape number) and `compute_image` (the image reference it runs) at its sign-in. A refused `auth_result` with `action: "update_image"` ends it with 76, `action: "uninstall"` with 77: doorbells that carry nothing, since its host service asks the platform which tag to run and whether the machine was removed (`GET /v1/satellite/compute-host`). Neither leaves the refused-sign-in mark, which a refused secret alone leaves. The process is not dumpable (`prctl(PR_SET_DUMPABLE, 0)`). With no host shape a compute machine behaves as 0.5.176 did. |
| 0.5.176 | Additive, compute machines alone (which sign in only on the platform's own version). A compute machine on another host (`[satellite] kind = compute` with `files = own`) keeps its own copy of the folders: its capabilities claim `compute_files: "own"` (one beside the platform claims `"shared"`; none reads as shared), and the platform admits the link only when the claim is its row's. It takes the file, tool, transcript-sweep and login-file frames as a paired machine does and still refuses `session_move` (`transport/compute_link.py`). A compute `start_session` or `pty_open` may carry `host_files` (`{<path below the agent's folder>: {content_b64, mode}}`, the files the platform prepared for the session) and `host_folders` (paths below the agent's folder its sandbox binds): the machine makes the folders and writes the files under its agent folder before the render, never through a link, and removes the files at the close; its sessions report their changes as `file_changed`. In compute mode the session count cap is not applied: the platform admits by memory. On every machine, a tool's program starts at its first call. A stdio entry whose env carries `OTO_TOOL_STANDIN` (the platform gives every such entry a fetch token, so every wrap already puts the interceptor in front of it) runs the vendored interceptor as its stand-in: it answers the engine's handshake and lists from the answers the platform saved for the session's agent and person (`GET /v1/hooks/tool-listing`), starts the program at the first request it cannot answer after asking `POST /v1/hooks/tool-start`, and reports the start there. The tunnel forwards `^/v1/hooks/(tool-listing|tool-start)$` on the tool's fetch token (both lists). An older satellite's interceptor passes the marker to the program, which starts with the chat as before. |
| 0.5.177 | `sync_mcps` gains an optional `hosted: true` on a hosted install (an older satellite ignores it; a compute machine keeps it for its builds, `sandbox_host.note_hosted`). On a compute machine every step of a tool's install and of the start-time reconcile runs in the platform's sandbox (`sessions/sandbox_host.build_run`: the vendored render's build table, kind `build`, made on the machine and never received, through the one door `mcp_install_support.install`; the Windows file-lock retry never runs there), and no tool is warmed after an install there. A file a build left is read through `read_built` (a regular file, bounded) and the shebang fix writes through a fresh name. A community tool's venv there sits on a uv-managed interpreter in `community/.uv-python`; the start rebuilds one whose interpreter a new image removed. The vendored installer's system requirements moved to `_vendored/mcp_system_deps.py` (hashed at the start, `SHARED_MCP_SYSTEM_DEPS_HASH`). A paired computer installs as before. |
| 0.5.177 | Additive, the memory guard on every Linux machine (`sessions/memory_guard.py`, `HostOS.memory_guard`). Such a machine sends `memory` (with `known`) and `memory_guard` at its sign-in and in every heartbeat. It takes `auth_result.memory_policy`, sends `memory_stop` and marks `turn_ended` and `pty_exit` with `reason: "memory"` when it ended a session. Its last start check refuses by memory, with the ack's `reason: "machine_memory"`. On a Mac or a Windows computer that check counts sessions alone. A paired Linux session's process starts at OOM score 500 (`sandbox_host.paired_command`). |
| 0.5.175 | Additive. The credential exit on every machine kind: a second lock, `requirements-exit.txt` (`httpx`, `h11`, `cryptography`), installed best effort from wheels alone (a host with none runs no exit and reports no `exit_port`), and `sessions/credential_exit.py` runs the proxy's relay (`_vendored/exit_relay.py`, `exit_authority.py`, `outbound_url.py`, hashed at boot as files) on the loopback from the start, on Linux, macOS and Windows paired machines and on the compute machine; the capabilities and the heartbeat carry `exit_port` and `exit_ok`. `start_session`, `pty_open` and `tool_open` may carry `exit` (the session's door: `secret`, `token_hash`, `entries`, `carves`; nothing of a value), the values arrive as `mcp_gateway_token` frames whose `mcp` is the placeholder, a compute machine takes those frames for the exit alone, and a compute `sandbox` with `exit_port` is spliced onto this machine's listener with its bundle bound at `/tmp/oto-exit-ca.pem`. The authority and the bundle under `<otodock dir>/credential-exit/` are made at the first door. Behind the machine's own egress proxy, or with the packages missing, no listener starts, the machine reports no `exit_port`, and the platform's exit rule refuses it. |
| 0.5.174 | Additive, compute machines alone (which sign in only on the platform's own version). `tool_open`, `tool_in`, `tool_close`, `tool_out` and `tool_exit` carry Direct LLM's stdio tool processes (see "Direct LLM's tool processes"). `app_server_start`, `app_server_stop`, `app_server_sweep`, and from the machine `app_server_output` and `app_server_exit` (both on the bulk lane). `step_run` with a `sandbox` key runs in the platform's sandbox, and a compute machine takes it. `http_request` may carry `via: "app"` (an app server's own listener). |
| 0.5.173 | Additive. A compute machine sends its `memory` at its sign-in and in every heartbeat, read by the vendored `host_resources.py` (hashed at boot as the other vendored modules are), and the platform admits the sessions it runs against it. A compute machine signs in only on the platform's own version. In compute mode a stop drops the link before it ends the sessions, so the platform reads a machine it lost and continues their turns at its return. The platform signs SSH logins: a `start_session` or `pty_open` payload carries `ssh_agent` (`{"public_keys": {<key name>: <public key line>}}`) when the session takes the agent's SSH keys, and the satellite opens a unix socket for the session (`~/.oto-dock/run/ssh-agent/<12 hex>.sock`), forwards each agent message to `POST /v1/hooks/ssh-agent` through its own tunnel with the session's token and writes the answer back, writes the public keys under the session's secrets folder and sets `SSH_AUTH_SOCK` and `OTO_SSH_KEY_DIR` in the spawn env. The tunnel forwards `^/v1/hooks/ssh-agent$` (both lists). A compute session's `sandbox` may carry `ssh_agent` (the socket's file name, bound at `/tmp/oto-ssh-agent.sock` by the render) and `exit_port` (a loopback splice for the credential exit); an older render refuses a description that carries either. A 0.5.172 machine gets no `ssh_agent`, and the SSH hosts tool is left out of its sessions with the reason. |
| 0.5.172 | Compute mode (`[satellite] kind = compute`, the capability `compute`): the platform's sessions in the platform's sandbox, rendered on the machine from the payload's `sandbox`, `argv`, `env`, `cwd` and `tunnel_port` by the vendored `sandbox_render.py` and `oto-sandbox-net`; nothing written, translated, scanned or wiped; the files, tools, login, move and lease frames refused; no code and no uninstall taken. The tunnel forwards `^/v1/knowledge-libraries(/.*)?$`, `^/v1/mcps/local-package/validate$`, `^/v1/conversations(/.*)?$`, `^/v1/phone/call-control(/.*)?$` and, from a compute machine alone, `^/v1/mcp-gateway/[A-Za-z0-9_-]+(/.*)?$` (mirrored in `core/remote/satellite_http_tunnel.py`). A paired satellite refuses to start on an agents folder carrying the platform's `.otodock-platform` marker. |
| 0.5.171 | The floor: `MIN_SATELLITE_VERSION` is 0.5.171, and the platform keeps no branch for an older satellite. Codex turns are kept like Claude Code's and replayed for the engines `auth_result.replays_turns_of` names, and `sessions` entries carry `resume_handle`, `model` and `mcp_servers`. Every terminal open carries an `open_token` (`pty_open`, its ack, `pty_exit`, `pty_close`, `pty_alive`'s `open_tokens`), `transcript_lines` carries its `offset` and `transcript_ack` answers it. The capabilities carry `active_sessions`. `credentials_update` reaches live sessions. One prompt file per session (`system-prompt-<hash>.json`). A Claude Code terminal on an API key signs in through a key helper. The Codex bridge reads the platform's `hold_s` as the permission gate does. A 1.9.1 satellite (0.5.168) is pushed this version at its first sign-in. |
| 0.5.170 | Additive. The two alive frames repeat the `report_id` of the sign-in's `auth_result`, and `sessions_alive` says `partial: true` when its second list failed. `platform_stopping` (`back_within_s`) and `hold_s` on the tunnel's 502 and 503 while it stands, which the Codex bridge's hold reads too. The reconnect wait resets only after a link that held 120 s, and a link that signed in and dropped keeps doubling it up to 10 s. The heartbeat frame goes out before the fingerprints, which run in a task of their own. An older platform sends no `report_id` and no `platform_stopping`, and the satellite then behaves as 0.5.169 did but for its reconnect wait. |
| 0.5.168 | The local link checks itself (additive fields only). Every heartbeat tick, before every connect and at once on an accept failure, the tunnel makes one request to `/v1/tunnel/self-check`, a path the tunnel answers itself (never forwarded, on neither allowlist) with a token of its own. A listening socket found closed, a refused or reset connection or a wrong answer rebinds: on the same port first (every session holds it), on Windows beside the old listener's open connections when nobody else listens there, else on a new port. Three slow answers in a row rebind too, and never report the link down. The heartbeat and the auth capabilities carry `tunnel_ok`, `local_tunnel_port`, `tunnel_rebinds` and `last_tunnel_error`. The loop's exception handler (every OS) logs "Accept failed on a socket" whatever its exception and heals at once, where 0.5.167 and older dropped every `ConnectionResetError` on Windows, this one with it. A 503 `tunnel-not-connected` logs once a minute with how long the link has been down. |
| 0.5.167 | `stop_turn` carries `drain_bg_s` (additive): the post-turn stdout forwarder `drain_bg` arms runs that long (the platform's background-work ceiling, `BACKGROUND_WORK_CEILING_S`, 7200 s by default) instead of ten minutes, so a background command's end and a background sub-agent past ten minutes still reach the platform: the command's completion, the sub-agent's own lines (a sign of life), its completion (the backup for a stop hook lost in a reconnect) and the CLI's own wake turn. A proxy that sends no `drain_bg_s` keeps ten minutes (it reads a sub-agent's lines no longer than that). |
| 0.5.166 | Machine only, no frame or payload change. Codex's turn/start carries the collaboration mode as the top-level `collaborationMode` (`{mode, settings: {model, reasoning_effort, developer_instructions: null}}`: the session's model and effort, else the thread's from the `thread/start` or `thread/resume` answer), where Codex reads it. The `settings.collaborationMode` key sent before was dropped, so Plan never planned. |
| 0.5.165 | Machine only, no frame or payload change. On Windows a `step_run` (an app step, a script check) runs a shell script, or one with no first line, under Git Bash (Git for Windows, which the baseline tools install: `host/interpreters`), a Python one under the install's own Python, an `env` line's assignments and `-u NAME` removals through Git's `env.exe`, and any other word as the program PATH names, a `.cmd` or `.bat` refused (Windows starts none without a shell) (`sessions/step_runner.argv_for`, the proxy's rule verbatim). Git is looked for in its install folders first, a `git` on PATH last. An interpreter the machine lacks acks `status=error` with "Scripts need bash on this computer: install the baseline tools." (or Python, or the word) instead of the OS's error code. WSL's `bash` and the Store's `python` alias are never taken. |
| 0.5.164 | Machine only, no frame or payload change; with it the platform sends Claude Code 2.1.312 and Codex 0.166.0. A headless Plan spawn passes `--permission-prompt-tool stdio` (Claude Code 2.1.299+ asks again for a call the hook allowed in plan mode; the platform answers that ask, from the gate's record, through the existing `control_response`), and the settings set `useAutoModeDuringPlan: false`. Both spawns set `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`; the built-in mods `cc-plugin-easel` and `cc-plugin-claude-platform` are off by name. A Codex terminal names `approvals_reviewer="user"` in every mode but Auto, and starts fresh, with its own notice, when the thread's rollout recorded another model provider (Codex 0.166 refuses that resume). |
| 0.5.163 | The tunnel forwards `^/v1/community/(agents\|departments)$` and `^/v1/departments$` (mirrored in `core/remote/satellite_http_tunnel.py`): the agent-creator tool searches the community catalog and lists the departments from a chat on the machine. Exact paths; the routes judge the session's role. An older satellite answers `403 {"error": "path-not-allowlisted"}`, which the tool reads as "This machine runs an older OtoDock satellite, which does not pass this call on." |
| 0.5.162 | The tunnel forwards `^/v1/cards(/<id>)?$` (mirrored in `core/remote/satellite_http_tunnel.py`): the agent-creator tool draws a proposal card and reads its state from a chat on the machine; the person's click (`/v1/cards/<id>/act`) stays the dashboard's. An older satellite answers the draw itself with `403 {"error": "path-not-allowlisted"}`, which the tool reads as its older-satellite sentence. The Settings tool's card, list and card-check tools use the same forward (the list route is one segment the same pattern allows). On an older satellite they say the machine runs an older OtoDock satellite and ask the person to act in the dashboard. |
| 0.5.161 | Machine only, no wire change: the Mac fixes found after 0.5.160 reached the internal fleet. `uninstall.sh` reads its config with `[[:space:]]` (macOS sed reads `\s` as a literal s, which mangled the secret and the URL, so a manual uninstall on a Mac never told the platform) and its cleanup hint names pnpm once on a Mac (Homebrew's). An Intel Mac's daemon puts `/usr/local/sbin` in front of `PATH` too (`host/cli_versions._DARWIN_TOOL_DIRS`). `install_failure_cause` leads with the disk and file-lock lines beside the network ones (`_NETWORK_WORDS`, held equal to the proxy's `_TRANSIENT_FAIL_RE`), so a full disk or a locked file is never kept as a failed install for the hour. |
| 0.5.160 | Machine only. A fresh MCP venv runs on the satellite's own base interpreter on a POSIX host (`mcp_install_support.mcp_python_spec`: `<sys.base_prefix>/bin/python<minor>`, else the minor). Windows keeps uv's default. That holds on a fresh install and on the boot reconcile's rebuild alike, and the venv is marked reconciled in its `.oto-runtime.json` only when it landed on that pin or on a floor the package asked for. On macOS the boot also rebuilds a venv newer than the satellite (Homebrew's newest Python, which no compiled package serves yet). Every rebuild keeps the old venv aside (`mcps/.venv-aside-<category>-<name>`) until it succeeds and puts it back when it fails. The next start settles an aside that a satellite stopped mid-rebuild left (`_settle_venv_asides`). A failed install's `error` is the log's cause lines alone (`install_failure_cause`: no `RuntimeError:` prefix, the network, disk and file-lock lines first, at most 600 characters). On macOS the daemon puts Homebrew's and the per-user folders in front of `PATH` at start. |
| 0.5.159 | The tunnel forwards `^/v1/settings/(catalog\|card)$` (mirrored in `core/remote/satellite_http_tunnel.py`): the settings tool reads the catalog and asks for a setting's card from a chat on the machine. An older satellite answers both calls itself, the catalog read first, with `403 {"error": "path-not-allowlisted"}`, which the tool reads as "This machine runs an older OtoDock satellite, which does not pass the settings on." No frame or payload change. No release ships 0.5.159 alone: 1.9.1's satellite is 0.5.163, which carries these routes. |
| 0.5.158 | Machine only: a Codex turn asks for the reasoning summary (`turn/start` `summary` "auto", the platform's twin); no frame or payload change. |
| 0.5.157 | Two fixes for a chat on this machine. The tunnel forwards `/v1/chats/agent-move`, the agent's `move_chat` (an older satellite answers it `403 path-not-allowlisted`). A Codex `thread/start` and `thread/resume` carry the session's working folder as `cwd`: a thread moved here from the platform resumed in the folder its rollout recorded (the sandbox's `/users/<name>`), where every stdio MCP failed at spawn. |
| 0.5.156 | Two additions in one release. The chat move: `session_move` and its `session_move` capability; a chat moved to another place takes its engine session files with it, scoped to the named session's ids, with the `stat` op sizing a transcript for the move's proof and day-old staging swept at start. The `file_push` engine-home refusal canonicalises the path (realpath on both sides; on Windows trailing dots and spaces dropped) and covers the `satellite_host` door; `check_session_resumable` refuses unsafe names. A start whose engine refused the session it was asked to resume (a Codex thread it cannot open) acks with `resume_refused: true`, and a revive's `_resume_handle` carries `resumed: false`: the platform reseeds the chat from its history instead of letting it run without its memory. The permission modes: the start config's `cli_permission_mode` (Claude Code's own word for the chat's mode): Auto spawns a headless chat with `--permission-mode auto` and the stdio prompt tool, and the terminal starts in auto or plan by itself, default otherwise. The platform's mode words `ask` and `autoReview` (an older platform's `default` and `acceptEdits` read as them). A Codex session in Auto runs `approvalsReviewer: auto_review` with `on-request`; every other mode takes its sandbox's policy, with no `untrusted` and no `OTO_HOOK_BRIDGED`. The `otodock` command's `--mode ask, auto, dontAsk, plan` sends the platform word. Behind a platform downgraded below 1.9, a 0.5.156 satellite reads that platform's `acceptEdits` as Auto (Codex's reviewer, `on-request`); that platform's hook still cards every call first, since the satellite no longer marks a session bridged. |
| 0.5.155 | Machine only: a frame the link cannot read, or one that is not a JSON object, is dropped with a warning (its kind and length) and the link reads the next one; it no longer ends the link. |
| 0.5.154 | Machine only: a refused sign-in (4001, or the platform's 1013) backs off, doubling to 30 s, instead of reconnecting every second. |
| 0.5.152 | Machine only: the link's message loop dispatches the platform's frames through a table instead of one chain; every frame is handled as before, and a frame of an unknown type is still dropped with a debug line. |
| 0.5.151 | Machine only: the scrollback ring's bracketed-paste tracker (`_vendored/terminal_queries.py`) follows a saved mode (`CSI ? 2004 s`, XTSAVE) and its restore (`CSI ? 2004 r`, XTRESTORE), so a local `otodock` terminal attaching after a trimmed replay pastes bracketed when the program expects it. |
| 0.5.150 | `session_files_retention` (the retention sweep's keep list; the transcripts it does not name go). `check_session_resumable` answers `reason` (`missing`, `empty`, `unreadable`) and an error ack for a read fault, the file read in a thread. A `file_push` write carries `expect` and is answered `stale` with the file's stat when the file moved under it, a committed write's ok ack carries the committed stat, a push into an engine home outside a person's own folders (Claude's `skills/`, `commands/`, `agents/`, `output-styles/`; Codex's `prompts/`, `skills/`) is refused, and only those folders sync under an engine home. The DISPLAY injection into a Codex MCP env table closes the table at the line's last brace. The CLI reconcile skips a probe that did not run instead of installing over it. The event-loop watchdog (the proxy's twin, seven vendored modules now) and the heartbeat's sleep rule tell a suspend from a starved daemon. |
| 0.5.149 | Lint only: every background task held until it ends and a task that dies logged under its name, the forward pause during a replay awaited instead of polled and released when the session closes, broad exception handlers narrowed to what the call raises. Nothing on the wire. |
| 0.5.148 | Lint only: import order, lazy logging arguments, explicit exception causes, newer syntax, no behaviour change. The PTY relay logs under `otodock.pty_relay`. Nothing on the wire. |
| 0.5.147 | The permission gate's hook carries `onFailure: "block"` (a hook that cannot run denies the call). The Codex terminal's `[desktop] git-worktree-root` points beside `.codex`. Nothing on the wire. |
| 0.5.146 | `disableClaudeAiConnectors` written into the scope's `settings.json` (the pool account's claude.ai connectors never load). Nothing on the wire. |
| 0.5.145 | `report_skipped` on `request_manifest` and the `skipped` manifest entries. Unlistable folders collected and Windows junctions not followed. The end-of-turn scan's strict delete probe. The per-walk skip summary. A Windows satellite at this version restores the platform's held paths. |
| 0.5.144 | Windows hashes files again (`_hash_file`). |
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
| 0.5.128 | `steer_turn` into a running headless Claude turn. |
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
