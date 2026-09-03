# Changelog

All notable changes to OtoDock are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
OtoDock uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each
release's entry is also published as its [GitHub Release](https://github.com/OtoDock/oto-dock/releases).

Upgrading is `git pull`, then `docker compose pull && docker compose up -d` —
the compose file pins each release's image version, so pulling the repo is what
moves an install to the new release. Anything that
changes the behaviour of a running install — a config key, a schema migration, a
changed default — is called out explicitly under its version.

## [Unreleased]

## [1.5.0] — 2026-09-03

Two headline additions — your agents on your own machines, and your agents
on the phone — plus departments, shared knowledge libraries, a 3D company
map, live voice conversations, and a long list of fixes.

### Added
- **Remote machines.** Pair a Linux, macOS or Windows computer with a
  one-line install and run your agents on it, with full access to that
  machine's files, apps, network and tools — same chat, same live streaming,
  same dashboard. The connection is outbound-only (no open ports), pairing
  codes are single-use, and every capability is an explicit grant. Files
  sync both ways (generated build folders are skipped automatically), idle
  sessions survive platform restarts, machine cards show each machine's CLI
  versions, and the satellite keeps itself up to date. Two tools ride along
  for agents on a machine: **browser control** (drive a real, logged-in
  Chrome/Edge) and **computer control** (screen, mouse and keyboard).
- **Phone calls.** Agents answer and place real phone calls, through Twilio
  (paste an account SID and token; calls enter through your existing
  dashboard URL) or your own Asterisk/FreePBX. Calls run on the live voice
  pipeline: natural replies, talk-over interruptions, a spoken line before
  each tool call. Routes can require a keypad PIN (lockouts after repeated
  failures) and keep a 30-day call log. The phone service is part of the
  standard install and idles until telephony is configured; its image is
  published with every release. A phone-calls tool places outbound calls
  from chats and tasks, and a transcription tool turns audio and video into
  text and SRT subtitles.
- **Live voice conversations.** A phone-style mode in chat: open mic with
  echo cancellation, instant barge-in (even while the agent is silently
  using a tool), a live transcript in the composer, short spoken replies
  while documents and dashboards still render, click-to-edit drafts, and
  agents that can end the call themselves. Say **"Hey OtoDock"** (or "Hey"
  plus an agent's name) on any dashboard page to start one hands-free —
  detection runs entirely in your browser, per-user opt-in. Needs the phone
  service's voice engine; it replaces the earlier hands-free mode.
- **Departments.** Create departments with named levels (up to 8), give each
  agent a department and level, and delegation wires itself: peers to peers,
  each level to the one below or the whole subtree, and results back up to
  the level above. A chain guard stops delegation loops (depth capped at 4),
  the Agents page groups agents by department, and each agent's prompt
  states its place in the org.
- **Delegation between agents.** Hand work to another agent as a visible
  parallel session you can watch, steer and continue; the result comes back
  as a report. `send_files` drops workspace files into another agent's
  inbox. A scheduled run can read what the agents on its roster did (tasks,
  sessions, triggers, notifications) — enough for a CEO agent's morning
  briefing — while your own sessions read only what you could see yourself.
- **Shared knowledge libraries.** Share an agent's knowledge folder, or any
  subfolder, under a name of its own and attach it to other agents read-only
  or writable, on remote machines too. Writable copies flow edits and
  deletes back to the source at the end of the turn; read-only copies are
  healed, with stray edits kept in the Recover bin. A `bulletin/<library>.md`
  file is loaded into every attached agent's context — a standing team
  briefing. Manager-created scheduled tasks and delegated workers can write
  knowledge as well.
- **A 3D company map.** The Agents page is a moonlit world where each
  department stands on its own base: agents arranged by level, activity heat
  and live "responding" pulses, delegation lines between agents, zoom as
  navigation, arrow keys on desktop. The classic grid is one toggle away and
  the automatic fallback without WebGL.
- **Team dashboards.** Pin a mini-app for the whole team or just for you
  from any chat, hide a shared app for yourself, ship dashboards inside
  community agent templates, and a lone dashboard becomes the agent's
  chrome-free home page. Dashboards use the full width on desktop, and the
  mini-app kit gained three.js with glow effects.
- **Chat.** Drag or paste files into the composer with live upload progress;
  uploads of any size pass strict gateways in ~32 MB parts and resume after
  a blip; runs of tool calls fold into one summary line (Compact/Detailed
  setting); file paths in replies open a live preview; typing into a busy
  Claude chat stops the turn gracefully and answers at once; question cards
  show each option's full content; notifications open with the sending
  agent's identity and are capped to headline length.
- **Engines and models.** Claude Fable 5.1 is the default Claude model, with
  Claude Code 2.1.258 and Codex 0.149.1 pinned. Pin a single scheduled task
  to its own model or engine, switch a finished task chat to another engine,
  and get 72h/24h warnings before a connected Claude or ChatGPT login
  expires, with a one-click Reconnect; a dead login is reported at once
  instead of retried forever.
- **Skills and tools.** Skills can bundle scripts (Anthropic's skill-creator
  ships built in); install any standard skill folder from a zip; agents can
  browse and request skills and discover every MCP installed on your
  platform; a built-in `otodock-platform` skill teaches every agent how
  OtoDock itself works; approving an MCP request lets you pick the instance,
  and a missing instance shows as "Needs instance" instead of a failure.
  Agents can write their own persona, and managers are told while it is
  still empty.
- **Integrations.** Personal access tokens can be saved as separate labeled
  accounts (a repo-scoped token for one agent next to your own), API-key
  integrations connect with just the key, and a second Google-powered MCP
  extends the existing grant instead of replacing it. Excel writes gained
  real dates, format presets, named-range dropdowns and validation removal.
- `scripts/sandbox-doctor.sh` explains why agent sandboxes cannot start on a
  host (including the Ubuntu 24.04 user-namespace hardening), which the
  platform now also detects and names at startup.

### Changed
- Delegated workers report back; callers no longer reply by delegation just
  to acknowledge a result.
- The Groq text classifier is the default end-of-turn judge for every
  phone-mode language, with the on-box Smart Turn model as the fallback;
  existing installs keep their stored choice.
- Voice replies keep talking through tool work and start faster; the
  first-generation buffer is tunable per ElevenLabs provider row.
- Model pricing is refreshed to both vendors' current lists (GPT-5.6 tiers,
  Claude Sonnet 5 at $2/$10) for cost attribution.
- Satellite auto-update pauses after two rollbacks of the same version;
  "Update now" retries it.
- Meetings convened by sessions without a user are limited to the agent's
  delegation targets, and the roster is labeled "Delegation Targets".
- Document text extraction truncates at 512 KB with ranged-read guidance,
  and every document read, render and write runs in a memory-bounded worker
  so one pathological file cannot take the host down.
- The "Active now" panels collapse and scroll instead of pushing the page.

### Fixed
- Voice conversations: barge-in truly interrupts, including silent tool
  phases; the transcript keeps the whole utterance; replies no longer
  stutter, fall silent late in a session or lose your first words; the mic
  survives long pauses and stays muted when you mute it; a conversation
  survives its engine process dying; the halo and status follow reality.
- Phone calls no longer go deaf mid-call, crackle, or get cut by false
  barge-ins from background noise (`phone_bargein_timer_s` now means the
  minimum speech duration of an interruption).
- Dictation in non-English languages no longer drops words already shown.
- Shared libraries: a file deleted in a writable copy no longer comes back,
  and restoring from the Recover bin into a read-only library is refused.
- Uploads: no endless "Processing…" when no machine needs the file, chunked
  uploads finish on Docker installs, big batches queue with progress,
  oversized files say so, huge photos are downscaled instead of dropping the
  connection, and the photo viewer no longer closes by itself.
- Delegation: worker results are no longer cut off or shown twice, the
  delegating agent reliably wakes to read them, and usage is attributed to
  the model that actually ran.
- Sessions: scheduled tasks no longer die at birth or get closed while
  waiting on a question or a stalled tool; interactive/headless races and
  duplicate spawns are gone; remote machines shed idle sessions instead of
  refusing at capacity, forget no ghost sessions after a restart, and run
  exactly the pinned CLI.
- Task runs no longer report engine failures as successful empty runs, and
  a task pinned to another engine no longer runs with the wrong model.
- Logins: a transient provider outage no longer expires a healthy
  subscription, and the "expires soon" warning fires at most twice.
- Permissions: agent sessions see only what their user could (tasks,
  triggers, notifications, webhook subscriptions), and viewers can no longer
  replace a team dashboard.
- Integrations: OAuth MCPs that refresh tokens in place start on shared-only
  agents, Codex keeps MCP URLs with query strings intact, the sign-in page
  no longer reload-loops with the wake word enabled, and bare-metal installs
  apply MCP compose changes on restart. Community catalog: github-mcp 1.1.0
  fixes schema failures on resumed sessions with Claude Code 2.1.243+.
- Also fixed: Excel named-range corruption and stacked validations, file
  paths pasted as links reloading the page, stale "Active now" titles, agent
  settings overflowing on phones, and the 3D map's texture loading, framing
  on large screens and pinch-zoom.

### Security
- A pre-release audit of the newly public surface (remote machines, phone,
  five tool sidecars), every finding fixed: sidecars validate the ids they
  put into URLs and refuse private-address URLs; skill and MCP installers
  reject crafted names, references and compose files; cross-agent file
  transfers re-verify paths at write time; a writable library attachment
  requires editor access on the source; the Asterisk dialplan filters
  caller-supplied variables and Twilio webhooks validate signatures before
  spending rate-limit budget; satellites refuse plaintext `ws://` to public
  hosts unless opted in, verify the process behind their Windows control
  pipe, strip escape bytes from injected text and store their config
  owner-only.
- Remote-machine API responses no longer include credential hashes.
- Dependency updates for the 2026-08/09 advisories: `cryptography` 50.0.0,
  `aiohttp` 3.14.3, `h2` 4.4.1, `click` 8.3.3, `pillow` 12.3.0,
  `react-router` 7.18.2, `brace-expansion` 5.0.9.

### Upgrade notes (1.4 → 1.5)
- **Phone service on an existing install** (new installs get it): in the
  install directory run
  `curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/v1.5.0/docker-compose.phone.yml`,
  append `COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml` to
  `.env`, then `docker compose up -d`. It idles until telephony is set up.
- The departments schema migration and the Fable 5 → 5.1 remap of pinned
  agents, chats and tasks run automatically on first boot. Fable 5.1 needs
  Claude Code 2.1.251 or newer; paired machines update their CLIs and their
  satellite on reconnect.
- Asterisk/FreePBX installs: re-paste the generated dialplan snippet.
- The hands-free half-duplex voice mode is gone; installs without the phone
  service keep dictation only.
- MCP containers on bare-metal installs are recreated once on the first
  restart if their compose configuration changed.
- Rolling the Codex pin back requires wiping `CODEX_HOME` runtime databases
  (its migrations are forward-only).

## [1.4.0] — 2026-07-26

### Added
- **Agents can create agents.** Ask an agent to build you a new one — it
  interviews you, writes the new agent's persona, tools, schedules and
  onboarding into a template, and installs it with you as its manager.
  For platform creators and admins; tools the new agent needs that aren't
  installed yet go through the usual approval queue. On upgraded installs,
  agents created before 1.4.0 don't get the new tool automatically —
  enable it per agent from Agent Settings (new agents have it).
- **1GB file uploads and sync.** One per-file cap (`OTODOCK_MAX_FILE_MB`,
  default 1024, was 100MB) governs workspace/chat uploads and file sync to
  remote machines, streamed with bounded memory and written atomically. A
  new workspace popup shows live upload and per-machine sync progress.
  Satellites below 0.5.103 keep the old cap until they auto-update.
- **Claude Opus 5** replaces Opus 4.8 on the Claude Code engine (same
  price); chats and tasks still pinned to Opus 4.8 are remapped once at
  startup.
- **Continue a finished chat on a different AI engine.** Once a chat's
  session has ended, the model picker offers the agent's other engines —
  pick one and the chat restarts from its saved history.
- The model picker offers only AI engines you can actually use, and
  enabling an engine on an agent now requires a platform subscription
  for it.
- Live "Commands" badges for Codex background terminal commands, matching
  Claude chats — on remote machines too.
- Rename chats and task runs from the sidebar (task runs can also be
  deleted, role-gated); clipped titles show in full on hover/long-press.
- Community agent templates can bundle Agent Skills, and any agent can
  ship a per-user onboarding guide (`config/user-setup.md`) that loads
  into each new user's chats until that user completes it.
- Workspace quality-of-life: type-ahead selection, per-section recursive
  search, right-click Refresh, and a clear warning when deleting files
  too large for the Recover bin.

### Changed
- The agent persona file is now `config/agent.md` (was `prompt.md`) —
  existing agents migrate automatically at startup. Rolling back to 1.3.x
  afterwards needs `cp config/agent.md config/prompt.md` per agent.
- Interactive terminals follow the CLI's own permission modes (Shift+Tab)
  instead of the platform's generic approval prompt; the platform still
  enforces file-access rules and prompts for the highest-risk actions.
  Headless and "Don't ask" chats are unchanged — see UPGRADING.md.
- Core tools are granted when an agent is created or installed, and no
  longer silently re-added on every start — removing one now sticks.
  Templates can opt out entirely with `"core_mcps": "none"`.
- Agent-settings and MCP-marketplace tools no longer load in scheduled
  tasks or meetings — only where a person is present and asking.
- AI engine CLIs upgraded (Claude Code 2.1.220, Codex 0.145.0). Codex
  costs now include prompt-cache write tokens (previously undercounted),
  and GPT-5.6 context windows are corrected to 272k.
- Codex sessions on machines paired with full filesystem access are no
  longer confined to the workspace by Codex's own sandbox; the platform's
  permission prompts and path policy still apply.
- Task-history deletion follows the task role matrix, and on shared-only
  agents viewers can no longer delete shared chats.
- AI chat titles appear much sooner on long, tool-heavy first turns.

### Fixed
- Chat stability: no more self-scrolling when document previews load,
  long chats scroll inside the chat again, and the "Connect an AI engine"
  banner no longer covers the top bar.
- Mini-apps and artifacts: action buttons can call every platform tool,
  external links open properly (after a one-time consent), and apps
  written as complete HTML documents regain theming, actions and live
  data.
- Tools approved from Agent Settings now start automatically, and saving
  changed tool credentials restarts the tool's container by itself.
- Document tools: page screenshots return in seconds instead of timing
  out, scanned PDFs are read with vision (OCR gains a language option and
  much smaller output), and PDF page export no longer fails on files it
  just wrote.
- Codex: sandboxed commands work in personal chats, local model endpoints
  (Ollama/LM Studio) connect again, and permission hooks run on Windows.
- Also fixed: relative paths in terminal commands, opening a freshly
  granted agent without a page reload, agent managers without a platform
  role saving configuration, agent renames showing in chat, YouTube links
  embedding again, and the memory tool recovering from malformed calls.

### Security
- On a shared agent, a colleague's live terminal now opens read-only with
  a **Take over** button instead of accepting your input into a session
  running under their account and permissions; terminal messages are
  attributed to whoever typed them.
- Updated `pyasn1` to 0.6.4 (two denial-of-service advisories).

## [1.3.2] — 2026-07-23

### Added
- The installer now offers a one-time host tuning (`vm.swappiness=10` via
  `/etc/sysctl.d/`, sudo-gated, safe to decline) that prevents dashboard
  stalls after memory-heavy tool runs. Existing installs can apply it once
  with `sudo bash scripts/setup-host-tuning.sh`.

### Fixed
- Viewing a generating chat from more than one device or tab no longer makes
  the view reload every few seconds — all viewers now stream the turn live,
  simultaneously. This also removes the response lag those reloads caused.
- The chat no longer flashes a reload right after a response completes, and
  sending a message to a live interactive terminal from the chat input no
  longer reloads the terminal view.
- Hardened the Windows shell-wrapper analysis in the command permission gate
  against pathological regex backtracking on adversarial command strings.
- SSO login no longer fails with "OIDC not configured" until a proxy restart
  when the identity provider was unreachable at proxy startup (e.g. proxy and
  a co-hosted IdP cold-starting together after a power cut). OIDC endpoint
  discovery now retries automatically on the next login attempt, at most once
  per 30 seconds; explicitly configured endpoint URLs are never overwritten.
- Admin accounts now see only their **own** personal folder in the agent
  workspace file browser, like managers — this also fixes admins sometimes
  being shown an empty "My Workspace".
- Workspace sync to a user-paired remote machine now runs with the owner's
  per-agent role instead of full admin authority, and agent `config/` files
  are never deleted through sync absence-inference anymore (deliberate
  deletes still propagate). Previously a machine that had lost its working
  copy could delete the agent's prompt on the platform at sync time.
- Deleted personal files in the recover bin are now restorable only by their
  owner; shared workspace/knowledge/config entries keep their
  editor/manager tiers.

## [1.3.1] — 2026-07-21

### Fixed

- The Remote Machines settings tab no longer appears after a fresh login on
  builds that ship without the remote-machines feature. Every login response
  (password, 2FA, passkey, OAuth, account creation) now carries the same
  feature flags as `/auth/me`, so the dashboard sees the correct feature
  state immediately instead of only after a page reload.
- Idle session cleanup works from the moment the server boots: sessions
  with no recorded activity were mistakenly held open for the first
  15 minutes after a host restart.

## [1.3.0] — 2026-07-21

> **Upgrade note (Docker installs from before 1.2.0):** the compose file now
> reads operator settings from a `.env` file next to `docker-compose.yml`
> (older installs used `config.env`). If your settings still live in
> `config.env`, rename it — `mv config.env .env` — or set
> `OTODOCK_ENV_FILE=config.env`. Starting without a `.env` file no longer
> crash-loops: the proxy detects Docker's silently-created directory and
> stops with instructions naming the exact fix.

### Added

- **The video toolkit.** Agents can now produce finished videos end to end.
  Three MCPs ship with the platform: **video-gen** (AI footage, transitions
  and Runway Aleph edits, with your Google / Runway / fal.ai keys),
  **music-gen** (ElevenLabs music and sound effects) and **tts** (voice-overs
  via the platform's TTS providers). The **video-tools** editing MCP
  (timelines, transitions, captions, color grading, FFmpeg rendering) is in
  the community catalog, installable on any 1.0+ install.
- **One-command install.** `scripts/install.sh` bootstraps a fresh Docker
  install end to end: Docker preflight, `.env` generation, the Ubuntu 24.04+
  AppArmor step, and first start.
- Chats tell you where they run: a chat stays on the machine it started on,
  says so when that differs from the agent's current machine, and an
  owner/admin can move it to the current machine with its conversation
  reloaded.
- Delegation: a session can adopt an existing project as its orchestrator,
  and the delegation tool lists each agent's layers and models so requests
  are validated instead of failing blind.
- The SSH MCP can list its authorized hosts mid-session (`list_ssh_hosts`).

### Changed

- Interactive terminal sessions are **enabled by default**, and tool
  permission prompts are now risk-based: read-only tools never prompt,
  reversible actions stop prompting in Accept Edits mode, and outward-facing
  or costly actions always prompt. MCPs without a declared tier keep their
  old prompting behavior.
- The installer installs into the directory it is run from, refuses your
  home directory, and performs fresh installs only, as before.
- **Per-agent roles decoupled from the platform role**: any non-admin user
  can hold any per-agent role (manager / editor / viewer).
- **Shared-only agents bill the person, not the platform**: dashboard chats
  are attributed to the user whose subscription serves the session, and a
  shared chat changing hands recycles the live session under the new sender
  so one user's messages never spend another user's account.
- Remote agents allowed to run `ssh` can read `~/.ssh/config` and
  `known_hosts` (private keys stay unreadable, writes stay denied).

### Fixed

- Interactive terminals: the first message into a cold terminal is no longer
  lost, assistant text no longer duplicates, a dying CLI says "process
  exited" instead of going blank, opening a terminal no longer overwrites
  your clipboard, and Codex permission questions show their card in headless
  mode.
- Remote machines: replies no longer land one turn late after background
  work, the first workspace sync shows live progress instead of silent
  minutes, zero-byte files and folder deletions sync correctly, files on the
  machine's own disk preview and save reliably, and MCPs that need a newer
  Python install it instead of failing forever.
- Delegation: results reliably wake sleeping orchestrators (and are replayed
  if delivery fails), stopping a worker mid-turn no longer kills its
  session, and the "Delegated" badge always completes.
- Documents: previews in older chats no longer show "session expired", and
  the setup wizard pins document preview to the address you browse from, so
  fresh installs get a working preview on any origin.
- Tasks: "Run now" shows an honest queued state and attaches the open chat
  when streaming starts; `run_task` with `wait: true` works again.
- Connected Claude accounts keep their plan tier (Max/Pro) across token
  refreshes, so sessions no longer refuse plan-included models.
- Voice input no longer appends an invented filler word after you stop
  speaking.
- Plus a round of smaller fixes across meetings, chat deletion, skill
  loading, passkeys, browser-tool contention, and platform shutdown.

### Security

- Interactive terminal sessions enforce the chat's permission mode: in
  Default mode, file edits, gated shell commands and tool calls prompt in
  the terminal before running; mode changes made inside the terminal are
  respected.
- Display and media hooks are confined to the agent workspace, closing a
  path that could read arbitrary proxy-host files into the chat.
- OAuth connect pages escape reflected input and confine post-login
  redirects; API errors return clean messages and keep exception detail
  server-side.
- Template and text regexes are linear-time (ReDoS), the per-session Codex
  config is written owner-only, and prompt-file loading is confined to the
  agent tree.
- Dependency refresh across the proxy and bundled MCP servers — every open
  Dependabot advisory with an available fix is cleared.

## [1.2.0] — 2026-07-18

### Added

- **Agent Skills.** Skills now follow the industry-standard Agent Skills format
  (SKILL.md, agentskills.io) and load two ways: `always` (inlined into the
  system prompt) or `on_demand` (loaded only when a task matches — most skills
  moved there, slimming every system prompt). Standalone skill packages install
  from the new community-skills catalog — seeded with curated Anthropic and
  OpenAI skills — with the same admin approval flow as MCPs, managed from a new
  Skills tab in Agent Settings and a Skills page in admin settings. Docker
  installs gain a named volume (`otodock-skills`), created automatically by
  `docker compose up`.
- **Real equations in documents.** Agents write LaTeX math into Word (native,
  Word-editable), PDF (vector-rendered), PowerPoint and Excel (high-resolution
  images that persist across later edits), and read it back out as LaTeX.
  sympy now ships in the baseline agent toolchain — existing hosts pick it up
  on the next installer run or image rebuild.
- **Previous-version document previews.** When an agent delivers a new version
  of a document it already previewed, the old preview stays on screen as a
  view-only "previous version" you can scroll back to and compare; only the
  newest preview is editable, and a file keeps at most two full previews.

### Changed

- Spreadsheet reads return a coordinate-labeled grid and every write reports
  back the cells it touched — fixing a class of answers-one-column-off errors.
  Fresh formulas show as formulas, and malformed operations are reported
  instead of silently skipped.
- Skill context exclusions now actually apply: scheduled tasks and phone calls
  no longer load skills whose manifest excludes those contexts.
- Repeated reads of the same remote-machine file are now instant: the platform
  first asks the machine whether the file changed (satellite 0.5.95 —
  connected machines self-update) and serves its cached copy when it hasn't.
  Any doubt still transfers the full file.

### Fixed

- A connected account revoked at the vendor no longer retries its token
  refresh every minute forever — refreshes back off, and a definitively
  revoked account waits until you reconnect it.
- Document preview: now works on bare-metal installs served over plain HTTP or
  a non-default port ("refused to connect"), survives Collabora stalling on
  heavy spreadsheets, keeps an idle document loaded for 2 hours (was 1), and a
  view-only viewer can no longer place an editing lock that blocked other
  users' saves.
- The shared local browser no longer opens by itself: the window now appears
  only when an agent actually runs a browser action, and stays closed once you
  close it.
- Interactive terminal: content no longer goes missing after sitting idle, no
  keypress is needed to revive scrolling, switching chats no longer leaks a
  GPU context, and a prompt sent while the terminal was still starting is
  delivered once it's ready instead of silently dropped.
- Task runs opened from the sidebar now show the run's real model and
  permission posture instead of the viewer's own selections — and a task's
  "Don't Ask" mode no longer leaks into the next new chat you open.
- A scheduled task whose agent left a background command running forever no
  longer loops endlessly re-reviewing it — the task waits a bounded time and
  completes, noting anything still running.
- Remote machines: a newly created, deleted, or catalog-installed agent shows
  up in Remote Machines without a page reload; uploads to remote agents no
  longer stall for the length of the transfer (the copy continues in the
  background and a prompt referencing the file waits for it to land); and
  document reads over slow connections get the platform's full 150 s transfer
  window instead of failing at 5 s.
- Community MCP containers self-heal after the install's identity changes
  (stale container names are re-stamped and the start retried); containers
  from a *different* OtoDock install are named in a boot warning, never
  touched.
- Silent failures now fail loud at boot: one ERROR names the cause and fix
  when stored credentials can no longer be decrypted (changed `JWT_SECRET` /
  `config.env`), another lists the exact columns when the database schema is
  missing ones the code expects.
- Docker installs on Ubuntu 24.04+ no longer fail at boot with a namespace
  error. OtoDock ships a scoped AppArmor profile (`otodock_userns`) that
  grants the needed capability to the OtoDock container only — instead of the
  widely-circulated workaround that disables that kernel hardening
  system-wide. `scripts/compose.sh` installs it automatically (one-time sudo);
  pull-only installs run `scripts/setup-apparmor-userns.sh` once and set
  `OTODOCK_APPARMOR_PROFILE=otodock_userns` in `.env`.

## [1.1.1] — 2026-07-16

### Changed

- Chat dictation can now run up to 3 minutes per take (was 60 seconds, which
  cut long dictations off mid-sentence). Admins can tune this with the
  `audio_chat_stt_max_seconds` setting.

### Fixed

- Live voice mode no longer sends truncated turns. The auto-send fired the
  moment the silence timer expired, racing the speech provider's own
  end-of-utterance commit — long turns went out with the tail missing and
  anything said after was lost. The mic now stops first, the provider flushes
  everything it heard, and the full transcript is what gets sent. Stopping
  dictation likewise waits briefly for that flush, so the last sentence is
  never dropped.
- Dictation no longer doubles your sentences. Two ways a hidden second
  recording session could be left running — a slow microphone connect timing
  out, and stopping the mic while it was still connecting — meant a retry had
  multiple sessions transcribing the same microphone, so every sentence landed
  in the input twice (or more, stacking with each retry).
- The sidebar's "Active now" strip shows the real titles of live chats (it
  showed "New chat" for chats that started after the page loaded — most
  visible in the task-history view).

## [1.1.0] — 2026-07-15

### Added

- Images opened full-screen from a gallery can now be zoomed: pinch on touch,
  mouse wheel or double-click on desktop, with panning while zoomed. Long
  captions are shown in full.

### Fixed

- Video fullscreen on mobile no longer pins the screen to landscape: wide
  videos still rotate as a starting cue, but turning the phone upright rotates
  the video back to portrait fullscreen. Vertical videos are no longer rotated
  to landscape when fullscreen starts before the video dimensions are known.
- ElevenLabs speech-to-text no longer invents trailing text after you stop the
  chat mic. Stopping after a pause committed a silence-only buffer, which the
  model would "transcribe" into words never spoken (most noticeable in
  non-English dictation).
- A speech-to-text provider that fails (bad API key, quota, connection) now
  shows an error on the chat mic instead of a mic that hears nothing.
- Audio settings: the provider API-key row (Save/Remove buttons) no longer
  overflows the screen on mobile.

## [1.0.2] — 2026-07-14

### Added

- This changelog. Every release from here on ships its notes with it, so you can
  see what changed before you pull.

### Fixed

- The proxy reported version `1.0.0` in its API documentation regardless of which
  release was actually running. It now reports the running version.

## [1.0.1] — 2026-07-13

### Fixed

- Signing in could land you on a dead page. The redirect from `/` resolved
  against the wrong list of agents, so anyone who had agents but had not picked a
  favourite hit a broken "Back to Chat". It now falls back to the first agent you
  can actually see, for every role.

## [1.0.0] — 2026-07-13

Initial public release.

OtoDock is a self-hosted platform for running a team of AI agents on
infrastructure you control. It runs the real Claude Code and Codex as its engine,
so your agents inherit everything those CLIs can do, and wraps them in a live
dashboard, a security model built for shared servers, and the plumbing that turns
a coding tool into a team of coworkers.

### Added

- **Agents and chat.** Every step streams live — reasoning, tool calls, file
  edits as diffs, plans ticking off. Approve sensitive actions inline, or let
  trusted agents run unattended.
- **Multi-agent meetings.** Put specialists in one room for a moderated
  discussion where agents address each other, answer in parallel, and converge.
- **Delegation.** Agents hand work to parallel agent sessions you can watch,
  steer, and continue.
- **Bring your own engine.** Connect the Claude or ChatGPT plan you already pay
  for, an API key, or a local model.
- **Sandboxed by default.** Each agent runs locked down and isolated from your
  network; you grant one folder or one service at a time.
- **Schedules and triggers.** Recurring and one-off background tasks, plus
  webhooks that let outside systems start work.
- **Persistent memory** that survives across sessions.
- **Documents and images.** Read and author Word, Excel, PowerPoint and PDF, with
  a live in-chat preview; edit and generate images.
- **Voice.** Speak to your agents and have them answer out loud.
- **Interactive artifacts and pinned mini-apps** — dashboards and small tools
  your agents build and you keep.
- **Community catalogs** for installable MCP capabilities and ready-made agent
  templates.
- **Self-hosted install** via Docker Compose, with your chats, files, memory and
  credentials staying on hardware you run.

[Unreleased]: https://github.com/OtoDock/oto-dock/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/OtoDock/oto-dock/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/OtoDock/oto-dock/compare/v1.3.2...v1.4.0
[1.3.2]: https://github.com/OtoDock/oto-dock/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/OtoDock/oto-dock/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/OtoDock/oto-dock/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/OtoDock/oto-dock/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/OtoDock/oto-dock/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/OtoDock/oto-dock/compare/v1.0.2...v1.1.0
[1.0.2]: https://github.com/OtoDock/oto-dock/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/OtoDock/oto-dock/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/OtoDock/oto-dock/releases/tag/v1.0.0
