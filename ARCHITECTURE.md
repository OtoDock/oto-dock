# Architecture

This page shows how the OtoDock code fits together. It covers the
components, the path one chat message takes through them, the abstractions
the platform is built on and where every directory lives. Setup and the test
suites are in [`CONTRIBUTING.md`](CONTRIBUTING.md). The instructions for
coding agents are in [`AGENTS.md`](AGENTS.md). Using and running OtoDock is
documented at [docs.otodock.io](https://docs.otodock.io).

## The components

- **The proxy** is the platform core, in `proxy/`. It is a FastAPI
  application on Python 3.13. It owns sessions, the security model,
  scheduling, MCP wiring and files, and it serves the built dashboard.
  PostgreSQL is the only service it requires. Its entry point is
  `proxy/app.py`. Startup and shutdown live in `proxy/startup.py`. The proxy
  is the hub: it answers REST under `/v1/` and its main websockets under
  `/ws/`.
- **The dashboard** is the web app, in `dashboard/`. It is built with React
  19, Vite, TypeScript and Tailwind 4. Its entry point is
  `dashboard/src/main.tsx`. It calls the proxy's REST API under `/v1/`. One
  websocket, `/ws/dashboard`, carries the chat and the live state. Voice uses
  three more: `/ws/audio/stt`, `/ws/audio/tts` and `/ws/duplex`.
- **The Android app** is a Capacitor shell around the built dashboard, in
  `dashboard/android/`. It adds native push, alarms and a location bridge. It
  runs the same dashboard over the same sockets, against the server address
  the person enters.
- **The satellite** is a daemon for remote Linux, macOS and Windows
  machines, in `satellite/`. It runs agent sessions on those machines, on
  Python 3.10 and newer. It starts with `python -m satellite`. It keeps one
  outbound websocket to the proxy, `/v1/satellite`, and the hook and MCP HTTP
  calls ride it back. [`satellite/PROTOCOL.md`](satellite/PROTOCOL.md)
  describes that wire.
- **The phone service** takes and places phone calls for agents, in
  `phone/`. Calls arrive over Asterisk AudioSocket or Twilio Media Streams. A
  speech pipeline connects them to the agent's turns. Its entry point is
  `phone/main.py`. It talks to the proxy over three websockets:
  `/ws/phone-management` for its configuration, `/ws/phone` for each call and
  `/ws/duplex-engine/{id}` for voice in the dashboard. Its own page is
  [`phone/README.md`](phone/README.md).
- **The audio package** is `oto-audio`, in `audio/`. Speech to text, text to
  speech, voice activity and turn detection sit behind one provider registry.
  The registry is `audio/providers/registry.py`. The package is a library.
  The proxy imports it for chat audio, and the phone service imports it with
  its `localmodels` extra.
- **The first-party MCPs** live in `mcps/custom/`, one MCP server per folder.
  Each folder has a `manifest.json`. The manifests follow the schema in
  `mcps/manifest.schema.json`. The parts the container MCPs share are in
  `mcps/_shared/`. An MCP runs inside a session's sandbox over stdio, or as a
  container. It calls back to the proxy over HTTP with the session's token.
- **The catalogs** install at run time, through `proxy/services/community/`.
  Each kind has its schema beside the manifest one:
  `mcps/agent-template.schema.json` (an agent template's folder, read as one
  document) and `mcps/skill.schema.json` (a skill's front matter), read
  through `proxy/storage/catalog_schema.py`.
  Their sources are the OtoDock/community-mcps, community-skills and
  community-agents repositories. Community MCPs install into
  `mcps/community/`. Skills install into `mcps/skills/`.

### Deployment

- `docker-compose.yml` is the default self-hosted stack, with everything in
  containers. It runs PostgreSQL, the proxy, a socket proxy, the file tools
  container and Collabora. The proxy runs from the release image as a
  non-root user (uid 1000). It starts MCP containers through the socket
  proxy. The installer is `scripts/install.sh`. It writes the stack's `.env`
  and starts it. `scripts/upgrade.sh` moves an install to a new release.
- `docker-compose.phone.yml` adds the phone service. The default install
  stacks it, and the service idles until telephony is set up.
- `docker-compose.build.yml` builds the images from this checkout instead of
  pulling them. `scripts/compose.sh` stacks it and passes it the pinned
  versions.
- `docker-compose.t1.yml` runs PostgreSQL alone. It serves a proxy that runs
  on the host from the checkout, as `scripts/dev-setup.sh` sets it up.

Every pinned version lives in one place, `VERSIONS.md`. That covers the
runtimes, the images, the engine CLIs and the satellite.

## One message, end to end

A person types in an agent's chat in the dashboard, and an engine answers.
The steps below follow that message on the local path. Each step says what
happens, then where it happens in the code.

1. **Send.** The dashboard sends the text as a `chat` frame on the
   `/ws/dashboard` socket. When the chat has no live session yet, it sends a
   warmup with the first prompt instead. The proxy then starts the turn
   itself once the session is ready. The send starts in
   `AgentChat.handleSend`, in `dashboard/src/pages/agent/AgentChat.tsx`. The
   frame goes out through the `useDashboardWs` hook, in
   `dashboard/src/hooks/useDashboardWs.ts`. Each side defines the frame
   names once, and a test holds the two lists equal. The dashboard's list is
   `dashboard/src/api/wireEvents.ts`. The proxy's list is
   `proxy/ws/wire_events.py`.
2. **Receive.** When no turn is streaming, the proxy hands the frame to the
   send path. While a turn is streaming, the dispatcher steers the message
   into that turn or queues it in the chat's input queue.
   `ws_dashboard_handler` in `proxy/ws/dashboard.py` keeps one
   `DashboardConnection` per socket. The dispatcher in
   `proxy/ws/dashboard_dispatch.py` routes the frame. The send path is
   `proxy/ws/dashboard_chat_send.py`. The input queue is
   `proxy/core/events/input_queue.py`.
3. **Check and record.** The send path checks the usage limit and the pool
   cap. Then it writes the person's message through the chat's ordered
   writer, which keeps one lane per chat. The writer is
   `proxy/core/events/chat_writer.py`.
4. **Find or start a session.** Every start goes through one function,
   `_create_or_resume_session`, except the pre-warm a new chat gets before
   its first message. The function builds the session's configuration and
   prompt, takes a slot from the admission gate, resolves the engine and
   starts it. The function lives in the warmup controller,
   `proxy/ws/dashboard_warmup.py`. The configuration and the prompt come from
   `proxy/core/config/config_builder.py`. The admission gate is
   `proxy/core/concurrency.py`.
5. **Resolve the engine and where it runs.** The proxy reads the agent's
   engine: `claude-code-cli`, `codex-cli` or `direct-llm`. It also reads the
   placement. A person's own machine comes first, then the agent's machine,
   then the platform itself. A remote placement returns the remote layer. An
   unknown engine id is refused. `get_execution_layer` does this, in
   `proxy/core/session/session_manager.py`. The placement order is decided
   in `proxy/services/remote/remote_status.py` (`resolve_execution_target`),
   and `proxy/storage/remote_store.py` holds the paired machines.
6. **Start the engine in a sandbox.** For Claude Code, the CLI layer
   resolves the sandbox, and a persistent session spawns the CLI inside it.
   The sandbox is bubblewrap in its own network namespace. That namespace
   reaches the public internet but not the host, the local network or the
   cloud metadata address. The only local doors it opens are the proxy's
   port and the hosts the session's MCPs and local models need. The hook
   scripts are written into the session's config folder first. Codex has
   the same shape around a `codex app-server` daemon. The CLI layer is
   `CLIExecutionLayer`, in `proxy/core/layers/cli/layer.py`. The session is
   `PersistentSession`, in `proxy/core/layers/cli/session.py`. The sandbox
   wrapper is `proxy/core/sandbox/sandbox.py`.
7. **Run the turn.** A producer holds the session's lock and iterates the
   layer's `send_message`. Between turns it drains the queued messages.
   `_start_new_stream` starts the producer, in
   `proxy/ws/dashboard_chat_stream.py`.
8. **Translate.** Each engine's native output becomes the common event
   types. The direct engine translates in process. Local and remote sessions
   share the translators. The event types are in
   `proxy/core/events/common_events.py`. Claude Code's translator is
   `ClaudeCLIEventTranslator`, in `proxy/core/layers/cli/translator.py`.
   Codex's is `CodexEventTranslator`, in
   `proxy/core/layers/codex/translator.py`.
9. **Pump.** The pump owns the turn independently of the socket. It keeps
   the live state, forwards events to every attached viewer and survives a
   viewer leaving. Tasks, phone calls and voice conversations run through a
   pump too. The pump is `ChatStreamPump`, in
   `proxy/core/events/stream_pump.py`.
10. **End the turn.** For Claude Code, a settle controller decides when a
    turn is over once its result has arrived. A turn that ends abnormally
    carries a typed ending. The controller is `SettleController`, in
    `proxy/core/layers/cli/settle.py`. The endings are defined in
    `proxy/core/events/turn_ending.py`.
11. **Persist.** At the turn's boundaries, the pump saves the turn's blocks
    through the chat writer into the `chat_messages` table. The chat storage
    module is `proxy/storage/chat/db_chats.py`. The table's schema is in
    `proxy/storage/chat/schema.py`.

On a remote machine, steps 6 and 7 go through the remote layer instead. That
layer is a placement rather than an engine. It sends `start_session` and
`send_message` frames to the machine's satellite. The satellite spawns the
CLI as a plain subprocess and forwards its output verbatim. Steps 8 to 11 are
the same. The remote layer is `RemoteExecutionLayer`, in
`proxy/core/remote/remote_execution.py`. The satellite's connection is
`proxy/core/remote/satellite_connection.py`.

## The core abstractions

### Sessions and engines

- **The registry.** `proxy/core/session/session_manager.py` maps an engine id
  to its layer and resolves the placement. It provides
  `get_execution_layer`, `find_layer_for_session` and the capabilities the
  dashboard reads. Among the modules beside it in `proxy/core/session/`
  are these:
  - `session_state.py` holds the module-level session state.
  - `session_kind.py` names the session kinds: dashboard, phone, task,
    meeting, trigger, internal and app.
  - `visibility.py` holds the agent visibility modes.
- **The execution layer.** `ExecutionLayer` is the base every engine
  implements. Its methods start a session, send a turn, abort, steer and
  compact. A start is given an `AgentConfig`. Both are defined in
  `proxy/core/execution_layer.py`.
- **The engine descriptor.** Each engine declares what it is and what it can
  do in a `LayerCapabilities` constant, next to its layer. The descriptor
  covers identity, runtime, behaviour, model policy, authentication and
  usage. Shared code asks the descriptor, never the engine's name. Each
  engine has its own folder:
  - `proxy/core/layers/cli/` is Claude Code.
  - `proxy/core/layers/codex/` is Codex.
  - `proxy/core/layers/direct/` is the direct engine. It makes direct model
    API calls and always runs locally.
  - `proxy/core/layers/providers/` holds the direct engine's provider
    adapters.
- **Remote placement.** `proxy/core/remote/` holds the remote layer and the
  satellite connection. An engine's remote specifics sit behind its
  `RemoteEngineAdapter`. Claude Code's adapter is
  `proxy/core/layers/cli/remote.py`. Codex's is
  `proxy/core/layers/codex/remote.py`.
- **Admission.** `proxy/core/concurrency.py` reserves memory for each
  session. It vetoes a start when the host is short of memory.

### Events and the stream

- `proxy/core/events/common_events.py` is the one event vocabulary between
  the layers and everything downstream. It covers text, tool use and
  results, permission requests, sub-agents, background commands, delegation,
  the done and error events, and more.
- `proxy/core/events/stream_pump.py` is the pump.
- `input_queue.py`, in the same folder, keeps the chat's queue of waiting
  messages in the database.
- `chat_writer.py`, also there, is the ordered writer for each chat.
- `proxy/ws/wire_events.py` is the catalogue of websocket frame names. It
  also holds the tables that map events to frames.

### The permission gate and the floor

- **Hooks.** The scripts in `proxy/hooks/` run inside every Claude Code and
  Codex session:
  - `permission_gate.py` runs before each tool call, and it fails closed.
  - `tool_result_forwarder.py` runs after each tool call.
  - `subagent_tracker.py` tells the proxy a sub-agent finished.
  - `stop_tracker.py` tells the proxy the turn ended and asks whether it may
    end.
  - `credential_helper.py` hands `git` and `gh` the account token for each
    command, so no environment or file holds it. It asks with the session's
    command-line token, which the shell holds and no MCP server is given.
- **The decision.** The gate posts to `/v1/hooks/permission`. There,
  `decide_tool_permission` is the single authority. It checks the path
  policy and the revocations first. Then it applies the session's permission
  mode: ask, auto, don't ask or plan, or the unattended mode of a task or a
  call. In auto it allows reads, edits and the safe tools, and leaves the rest
  to the engine's own reviewer. The modes live in
  `proxy/core/permission_modes.py`, the route in
  `proxy/api/hooks/permission.py`.
- **The path policy.** `proxy/auth/path_policy.py` decides what a session may
  read and write, by role, scope and placement.
- **The floor.** `proxy/auth/path_shell.py` refuses catastrophic commands for
  every role on every host, an admin included. It holds on every engine,
  mode and placement:
  - On Claude Code, the hook enforces it.
  - On Codex, the hook enforces it on every call, and the approval bridge
    on what crosses Codex's sandbox. The bridge is
    `proxy/core/layers/codex/codex_approvals.py`.
  - On the direct engine, its built-in tools enforce it.
  - On a satellite, the hooks the satellite runs enforce it.

### Callers, tokens and agent folders

- **Role guards.** `proxy/auth/providers/__init__.py` resolves the caller
  and holds the base guards, such as `require_auth`, `require_admin` and
  `require_human`. `proxy/api/_guards.py` holds the two checks the API
  modules share beyond those, `require_manager` and
  `require_creator_or_admin`. Each route passes its own refusal sentence,
  and both answer 401 when there is no caller.
- **Tokens.** `proxy/auth/tokens.py` signs and checks every token the
  platform issues for itself. Each purpose is its own kind, with a key
  derived from the platform secret and an audience of its own, so a token
  of one kind never passes as another. A test keeps every read of the
  signing secrets inside this module and the few key holders.
- **Agent folders.** `get_agent_dir` in `proxy/config.py` names an agent's
  folder. It joins the name under the agents directory and refuses a name
  that is empty, absolute, holds a path separator or would leave that
  directory.

### MCP servers and credentials

- **The registry.** `proxy/services/mcp/mcp_registry.py` scans the manifests
  at startup. It builds each session's MCP configuration, as JSON for Claude
  Code and as TOML for Codex.
- **The broker.** `proxy/core/credentials/mcp_broker.py` holds a session's
  MCP secrets in memory. A stdio MCP fetches its own secrets when it spawns,
  from `/v1/hooks/mcp-credentials`. It uses a token scoped to that session
  and that server, never the session's own token.
- **The gateway.** `proxy/core/credentials/mcp_gateway.py` adds an HTTP
  MCP's vendor credential to each request on its way out, through
  `/v1/mcp-gateway/{mcp}/...`. The session never sees that credential. A
  satellite runs its own twin of the gateway for machine sessions.
- **Installs.** `proxy/services/mcp/mcp_installer.py` installs a package's
  environment. `proxy/services/mcp/docker_manager.py` runs the container
  MCPs.

### Storage

- `proxy/storage/pg.py` runs three psycopg pools. The first is the shared
  pool. The second is a lane pool behind `run_db`, for work kept off the
  event loop. The third is a small loop pool with a short timeout and a
  circuit breaker.
- `proxy/storage/schema.py` brings any database up to date at startup, with
  `migrate_database`. It runs four steps in order:
  1. It checks the version. A database that a newer release migrated is
     refused.
  2. `init_schema` creates every table with `CREATE ... IF NOT EXISTS`. Each
     domain has its own schema module, `proxy/storage/<domain>/schema.py`.
  3. `run_migrations` applies forward, idempotent changes such as
     `ADD COLUMN IF NOT EXISTS`.
  4. It records the release that migrated the database. The next version
     check reads that record.

  Every install upgrades in place, so a migration always assumes an
  existing database.
- Each domain keeps its database access in its own modules.
  `proxy/storage/database.py` is a facade that re-exports several of them
  under one import.

### Scheduling, apps and the rest

- **Scheduling.** `proxy/services/scheduler/` runs scheduled, one-time and
  trigger tasks. Task runs stream through a pump, like chats. Its main
  modules are these:
  - `scheduler.py` is the facade that starts, stops and registers tasks.
  - `runner.py` runs a task from its fire to its end.
  - `task_run.py` holds the steps of one admitted run, from its start to
    its result.
  - `trigger_manager.py` registers, validates and fires triggers.
- **Apps.** `proxy/services/apps/` runs each app's server in its own
  sandbox, from a hash-checked release copy. `proxy/api/apps/app_proxy.py`
  serves the apps' routes. Its main modules for the servers are these:
  - `app_supervisor.py` runs one process per app server.
  - `app_sandbox.py` builds the sandbox an app server runs in.
  - `releases.py` makes the release copies a viewer is served.
- **Everything else.** `proxy/services/` holds one package per domain. The
  others include delegation, meetings, memory, knowledge libraries,
  notifications, phone configuration, sharing, checks and billing.

### The satellite's shared modules

`satellite/_vendored/` holds byte-for-byte copies of six proxy modules. They
are the MCP installer, the stdio path interceptor, the Codex app-server
client, its approval bridge, the terminal query strips and the agent tree
layout. `scripts/sync-satellite-code.sh` copies them. It pins their hashes in
`satellite/config.py`, and the satellite refuses to start on a mismatch.

The host OS table is a twin instead. The proxy defines it in
`proxy/core/host_os.py`. The satellite defines it again in
`satellite/config.py`, and a test compares the two row by row.

## Rules the code enforces

- **Layers point down.** `storage` imports nothing from the layers above it.
  `core`, `services` and `configuration` never import `api` or `ws`. A few shared vocabulary
  modules may be imported from anywhere.
  `proxy/tests/core/test_import_layers.py` enforces these rules.
  No upward import is left since 1.9.0. A lower layer that needs a higher
  one's work takes a value passed in, or calls a function the higher layer
  registers at startup.
  `proxy/core/execution_layer.py` is a leaf that every side may import.
  `proxy/tests/execution/test_import_direction.py` keeps it a leaf.
- **No dropped tasks.** A task the proxy starts and does not keep goes
  through `core.background.spawn`. That function keeps the task referenced,
  logs how it died and cancels it at shutdown. It lives in
  `proxy/core/background.py`.
- **One authority per vocabulary.** Engine ids, placements, session kinds,
  roles, folder names, statuses and frame names are each defined once. The
  surface tests in `proxy/tests/` refuse a bare comparison anywhere else.
- **Colours come from the tokens.** The dashboard's colours, type, radii,
  shadows and motion are defined once, in `dashboard/design/tokens.json`.
  `npm run tokens:build` writes the stylesheets and constants that read
  them, and a test fails while a written file is stale. The ESLint rule
  `otodock/no-raw-palette` refuses a palette class or a literal colour in
  `dashboard/src/`. Palettes that are not the interface's, such as the
  terminal's colours, are left out.
- **No database on the event loop in periodic loops.** A periodic loop runs
  its store calls on the database executor.
  `proxy/tests/core/test_periodic_loops_offload.py` checks three such loops.
- **Lint and types ratchet.** Ruff froze the findings it was adopted
  with. ESLint's are all fixed, and mypy checks the whole proxy. A new
  finding fails. "Code style" in
  [`CONTRIBUTING.md`](CONTRIBUTING.md) explains the ratchet.

## Where things live

The tables below list every directory with its purpose, one table per node
of the tree. The maintainers generate them from the structure map their
checks keep. A pull request that adds a directory leaves the tables to them.

<!-- where-things-live:begin (generated from the structure map; do not edit by hand) -->
### The repository root

| Directory | What it holds |
|---|---|
| `.github/` | GitHub workflows, issue templates and README media |
| `LICENSES/` | the licence texts REUSE.toml points at (the FSL and the third-party ones) |
| `audio/` | shared editable package: cloud STT/TTS core used by proxy and phone |
| `dashboard/` | React + Vite + TypeScript frontend (and the Android wrapper) |
| `mcps/` | first-party MCP servers, shared helpers, the catalog's installed skill packages |
| `phone/` | phone-call service: telephony, STT/TTS pipeline, proxy client |
| `proxy/` | FastAPI backend, the platform core |
| `satellite/` | remote-machine agent (Windows / Linux / macOS) |
| `scripts/` | install and operations scripts |

### `audio/`

| Directory | What it holds |
|---|---|
| `audio/models/` | local model wrappers |
| `audio/providers/` | cloud STT/TTS providers |
| `audio/streaming/` | streaming helpers |

### `dashboard/`

| Directory | What it holds |
|---|---|
| `dashboard/android/` | the Capacitor Android app that wraps the dashboard |
| `dashboard/build/` | build helpers vite.config.ts imports (precompression, the build id) |
| `dashboard/design/` | the design tokens (tokens.json), their generator, the colour lint, the codemod, the reference page, the Figma variables and the mark's masters with their export (brand/) |
| `dashboard/public/` | static files served as they are (the favicons and logos, the setup page, the boot screen's script) |
| `dashboard/src/` | the React app |
| `dashboard/ui-kit/` | the UI kit served to artifacts and app pages (tokens CSS, licences) |

### `dashboard/src/`

| Directory | What it holds |
|---|---|
| `dashboard/src/api/` | typed API clients and react-query hooks |
| `dashboard/src/assets/` | static assets |
| `dashboard/src/audio/` | audio capture and playback |
| `dashboard/src/components/` | UI components by area |
| `dashboard/src/contexts/` | React contexts |
| `dashboard/src/hooks/` | shared hooks (chat stream, dashboard websocket, ...) |
| `dashboard/src/lib/` | utilities and shared logic |
| `dashboard/src/pages/` | routed pages |
| `dashboard/src/release/` | the release dialog's curated highlights (highlights.json) and its icon set |
| `dashboard/src/sharehost/` | the page an external share link renders (built to ui-kit/share-host.js) |
| `dashboard/src/store/` | client state |
| `dashboard/src/tests/` | vitest suite |
| `dashboard/src/uikit/` | the widget kit for app pages (built to ui-kit/otodock-widgets.js) |

### `dashboard/src/components/`

| Directory | What it holds |
|---|---|
| `dashboard/src/components/accounts/` | account and subscription UI |
| `dashboard/src/components/admin/` | admin page components |
| `dashboard/src/components/agents-map/` | the 3D company map |
| `dashboard/src/components/apps/` | apps and dashboards |
| `dashboard/src/components/audio/` | voice and dictation UI |
| `dashboard/src/components/chat/` | chat surface: messages, input, tool activity |
| `dashboard/src/components/common/` | shared widgets |
| `dashboard/src/components/departments/` | departments editor |
| `dashboard/src/components/engines/` | AI engine accounts: the API-key form and the subscription windows |
| `dashboard/src/components/icons/` | inline outline icons shared by the settings shell and the avatar menu |
| `dashboard/src/components/phone/` | phone call log and settings |
| `dashboard/src/components/projects/` | delegation projects |
| `dashboard/src/components/settings/` | the settings shell of User Settings, Agent Settings and Admin: the sidebar, the phone's section list, the section configs and the history trail behind back |
| `dashboard/src/components/sharing/` | the share popover and the shared-with-me surfaces |
| `dashboard/src/components/ui/` | primitives |
| `dashboard/src/components/usage/` | usage: the subscription pool cap section and the charts' colours |
| `dashboard/src/components/workspace/` | workspace file browser and previews |

### `mcps/`

| Directory | What it holds |
|---|---|
| `mcps/_shared/` | helpers shared by the first-party MCPs |
| `mcps/community/` | mirrors of the community catalog (runtime install dir, gitignored) |
| `mcps/custom/` | first-party MCP servers, one folder each |
| `mcps/skills/` | skill packages installed from the catalog (runtime install dir, gitignored) |

### `phone/`

| Directory | What it holds |
|---|---|
| `phone/assets/` | ambience and filler audio |
| `phone/calls/` | call lifecycle |
| `phone/duplex/` | the live voice pipeline |
| `phone/pipeline/` | STT, turn detection, TTS stages |
| `phone/proxy/` | the client to the proxy's phone websocket |
| `phone/telephony/` | Twilio and Asterisk adapters |
| `phone/tests/` | pytest suite |
| `phone/tools/` | operator tools |
| `phone/transport/` | audio transports (AudioSocket, media streams) |

### `proxy/`

| Directory | What it holds |
|---|---|
| `proxy/adapters/` | client adapters (dashboard, phone, ...): how a session type talks to the platform |
| `proxy/api/` | HTTP endpoint routers, one subpackage per surface |
| `proxy/assets/` | static assets the proxy serves |
| `proxy/auth/` | authentication, path policy, OAuth and webhook providers |
| `proxy/configuration/` | the parts of config.py behind its facade: the model registry, the system prompt's rendering, the value parsers and the key registry |
| `proxy/core/` | sessions, sandbox, execution layers, remote execution, events |
| `proxy/hooks/` | the scripts run inside agent sessions: the permission gate, the tool-result forwarder, the sub-agent and stop trackers, the command-line credential helper |
| `proxy/scripts/` | proxy-side maintenance scripts |
| `proxy/services/` | business logic, one subpackage per domain |
| `proxy/storage/` | schema and database access, one sub-package per domain |
| `proxy/tests/` | pytest suite |
| `proxy/ws/` | websocket handlers (dashboard, phone, satellite) |

### `proxy/api/`

| Directory | What it holds |
|---|---|
| `proxy/api/admin/` | admin surfaces: execution layers and models, storage and retention, chat-title generation |
| `proxy/api/agent_data/` | per-agent data endpoints |
| `proxy/api/agents/` | agent CRUD, workspace files, chats, user context |
| `proxy/api/apps/` | apps and dashboards (display artifacts) |
| `proxy/api/audio/` | speech providers and dictation |
| `proxy/api/auth/` | login, users, OAuth, API keys, setup |
| `proxy/api/billing/` | usage and limits, the relay credit balance, the OtoDock account connect |
| `proxy/api/checks/` | checks: the agent's and a person's own documents, the verdicts, the cap, the session tools' routes |
| `proxy/api/departments/` | departments: the org structure behind the agents map |
| `proxy/api/duplex/` | live voice conversations |
| `proxy/api/events/` | event subscriptions |
| `proxy/api/hooks/` | callbacks from inside sessions: permission, tool results, path resolution, credentials |
| `proxy/api/internal/` | internal service endpoints |
| `proxy/api/mcp/` | MCP catalog, instances, credentials |
| `proxy/api/media/` | media serving and transcoding |
| `proxy/api/meetings/` | multi-agent meetings |
| `proxy/api/notifications/` | notifications |
| `proxy/api/phone/` | phone servers, routes, call log, caller data, the call relay, the Twilio relay, the call tools' endpoint |
| `proxy/api/remote/` | remote machines and targets |
| `proxy/api/sessions/` | session control, plans, models, health |
| `proxy/api/sharing/` | shares of apps and chats: internal grants, external links, the grantee surfaces |
| `proxy/api/tasks/` | scheduled tasks and runs |

### `proxy/auth/`

| Directory | What it holds |
|---|---|
| `proxy/auth/oauth_providers/` | OAuth provider descriptors |
| `proxy/auth/providers/` | authentication providers (local, OIDC) |
| `proxy/auth/webhook_providers/` | webhook signature providers |

### `proxy/core/`

| Directory | What it holds |
|---|---|
| `proxy/core/config/` | config builders: the system prompt and session config per session type |
| `proxy/core/credentials/` | the MCP credential broker and gateway, the engines' credential files and their locks |
| `proxy/core/events/` | the chat stream pump and event types |
| `proxy/core/layers/` | execution layers: Claude Code CLI, Codex app-server, Direct LLM, and the provider adapters |
| `proxy/core/remote/` | satellite connection, tunnels, file sync, remote execution |
| `proxy/core/sandbox/` | bubblewrap sandbox, session config dirs, interceptor wrapping |
| `proxy/core/session/` | session state, identity, visibility, interactive sessions |

### `proxy/core/layers/`

| Directory | What it holds |
|---|---|
| `proxy/core/layers/cli/` | the Claude Code CLI engine: descriptor, layer, session, translator, settle, the remote adapter, the login and usage glue |
| `proxy/core/layers/codex/` | the OpenAI Codex engine: descriptor, layer, app-server session, translator, helpers, the remote adapter, the login and usage glue |
| `proxy/core/layers/direct/` | the Direct LLM API engine: descriptor, layer, in-process session, builtins, the tool catalog |
| `proxy/core/layers/providers/` | the provider adapters the API engine and the shared consumers speak (Anthropic, OpenAI, Groq, local servers) |

### `proxy/services/`

| Directory | What it holds |
|---|---|
| `proxy/services/agents/` | agent state: the setup state, the offboarding event and its subscribers, Shared-only membership, the repository instruction files lifted into the prompt, the provisioning steps after an agent or a membership commits |
| `proxy/services/apps/` | app storage and rendering, the actions manifest and the platform catalog |
| `proxy/services/billing/` | licensing, usage accounting |
| `proxy/services/checks/` | checks: documents and their index, the changed set, the evaluator and the four kinds, the judge's spend |
| `proxy/services/community/` | community catalogs: MCPs, agents (the template loader and its signatures), skills |
| `proxy/services/delegation/` | agent-to-agent delegation |
| `proxy/services/departments/` | the department-to-delegation-edge compiler |
| `proxy/services/engines/` | engine subscriptions, the pool, local endpoints |
| `proxy/services/infra/` | cross-cutting infrastructure: retention, storage quotas, safe file opens, checked outbound HTTP, the release check, the git audit trail, API keys |
| `proxy/services/knowledge/` | knowledge libraries and indexing |
| `proxy/services/mcp/` | MCP registry, permissions, updater, dynamic context |
| `proxy/services/media/` | media processing |
| `proxy/services/meetings/` | meeting orchestration |
| `proxy/services/memory/` | the memory system |
| `proxy/services/notifications/` | notification manager |
| `proxy/services/oauth/` | OAuth account flows |
| `proxy/services/phone/` | phone configuration, identity, adapters, health, the call tools' registry, calls inside a chat |
| `proxy/services/remote/` | remote-machine services |
| `proxy/services/scheduler/` | task scheduler and triggers |
| `proxy/services/scripts/` | the script runner app steps and check scripts share: the local sandbox and the step_run frame |
| `proxy/services/sharing/` | chat snapshots for shares |
| `proxy/services/webhooks/` | inbound webhooks |

### `proxy/storage/`

| Directory | What it holds |
|---|---|
| `proxy/storage/agents/` | agents, agent memory, departments |
| `proxy/storage/automation/` | tasks, triggers, notifications, webhook subscriptions |
| `proxy/storage/billing/` | subscriptions and usage |
| `proxy/storage/chat/` | chats and meetings |
| `proxy/storage/checks/` | the check index, the verdicts and the per-agent cap |
| `proxy/storage/files/` | file sync state, tombstones, authorship, the recover bin, file pins, transfers |
| `proxy/storage/identity/` | users, API keys, passkeys, the bearer allowlist, credentials |
| `proxy/storage/knowledge/` | knowledge libraries and the library mirror state |
| `proxy/storage/mcp/` | MCP instances, MCP requests, the MCP auto-update state |
| `proxy/storage/phone/` | phone routes, servers and the call log |
| `proxy/storage/prefs/` | per-user audio and UI preferences, audio providers |
| `proxy/storage/sharing/` | shares of apps and chats (internal grants and external links) |

### `satellite/`

| Directory | What it holds |
|---|---|
| `satellite/_vendored/` | byte-for-byte copies of proxy modules, kept by scripts/sync-satellite-code.sh and pinned by hash |
| `satellite/bin/` | CLI entry points (otodock) |
| `satellite/host/` | the machine: the host probe, path policy, safe file opens, environment hygiene, path translation, CLI versions, the tray, the service unit |
| `satellite/sessions/` | engine sessions on the machine (Claude Code, Codex), app steps, MCP install support |
| `satellite/terminal/` | PTY sessions and the local terminal socket |
| `satellite/tests/` | pytest suite |
| `satellite/transport/` | the websocket link to the proxy |

<!-- where-things-live:end -->
