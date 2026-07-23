<p align="center">
  <img src=".github/media/logo-mark.png" alt="OtoDock" width="112" />
</p>

<p align="center"><b>OtoDock — Collaborative Agents</b></p>

<h1 align="center">Your AI workforce, self-hosted.</h1>

<p align="center">
  OtoDock turns Claude Code and Codex into a team of agents that work for you.<br/>
  It runs on your server, and connects with your Anthropic and OpenAI subscriptions.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: FSL-1.1-Apache-2.0" src="https://img.shields.io/badge/license-FSL--1.1--Apache--2.0-146bb5"></a>
  <img alt="Version" src="https://img.shields.io/badge/version-1.3.2-146bb5">
  <a href="https://github.com/OtoDock/oto-dock/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/OtoDock/oto-dock/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://docs.otodock.io"><img alt="Docs" src="https://img.shields.io/badge/docs-docs.otodock.io-0d9488"></a>
  <a href="https://otodock.io"><img alt="Website" src="https://img.shields.io/badge/website-otodock.io-673a97"></a>
</p>

<p align="center">
  <b>Free to self-host up to 5 users. No credit card. Your hardware, your data.</b><br/>
  <sub>Runs on&nbsp; Claude Code · Codex · your API keys · local models (Ollama)</sub>
</p>

https://github.com/user-attachments/assets/76cd5989-be4f-4a85-bc58-8b2c4675cc91

<p align="center"><em>This entire video was directed, captured and edited by an OtoDock agent.
<a href="https://otodock.io">Watch it in full quality on otodock.io</a>.</em></p>

---

The most capable coding agents as the engine, a live dashboard on top, and
the security to let them work, on infrastructure you control.

- **Claude Code and Codex are the engine.** You inherit every capability the
  CLIs ship.
- **Everyone brings their own AI.** Every user connects their own Claude or
  ChatGPT subscription. Admins can also share API keys or local models
  (Ollama).
- **Security, enforced.** Every agent works in a locked-down sandbox,
  isolated from your network by default.
- **Your server, your data.** Chats, files, memory, credentials: everything
  lives on your server, and never leaves.

## What you get

### A workspace for everyone, and one for the team

Every person gets their own drive: files, charts, photos, videos, live
previews. Collaborative agents add a shared workspace everyone can browse.
Personal, shared, or both: set it per agent with one switch.

### Dashboard or terminal

Watch every tool call stream in the chat, or open the interactive Claude
Code and Codex CLIs in the terminal.

In the chat, every step streams in live: the reasoning, each tool call,
file edits as red/green diffs, plans and to-do lists ticking off as the
agent moves. Approve sensitive actions inline, or let trusted agents run.

In the terminal, you get the CLI itself, with the agent's credentials,
tools and MCPs already loaded.

<p align="center">
  <img src=".github/media/readme-chat-tools.jpg" alt="A chat mid-task: streaming tool calls with expandable detail and live progress" />
</p>

### Build

Reports, dashboards and mini-apps render right in the chat — pin one as
your agent's home page.

Agents build interactive charts, tables, calculators and little tools,
rendered right in the conversation, theme-matched and safely sandboxed.

Turn an agent-built dashboard into a standing mini-app: a morning brief, a
status board, a control panel. Refreshed live by scheduled tasks, with
buttons you approve once that run tasks, message the agent, or call a tool
instantly.

### Automate

Schedule tasks that work in the background and get notified when they
finish.

Work runs on any interval: every 17 hours, every 3 days, exactly as you
mean it. Fire agents from webhooks. Every run is a full chat you can open,
read and continue. Notifications escalate through four severities, from a
quiet chime to a persistent danger alarm.

### Meetings for agents

Agents can hold meetings to collaborate and share information.

Put specialist agents in one room and give them a topic. A moderator runs
the discussion, agents address each other and answer in parallel, and you
watch the whole conversation converge, or join in. Meetings run inside
scheduled tasks too.

<p align="center">
  <img src=".github/media/readme-meeting.gif" alt="A live meeting: three agents replying in parallel while the moderator directs" />
</p>

### Documents

Agents create and edit Word, Excel, PowerPoint and PDF documents inline in
the chat.

The files are complete with tables, charts and formatting, and the document
opens right in the conversation, in a live editor you and your team can
type into.

<p align="center">
  <img src=".github/media/readme-documents.jpg" alt="An agent-created document open in the live in-chat editor" />
</p>

### Anywhere

The same agents on your phone, with voice and notifications. Hands-free
conversations, plus dictation and read-aloud in every chat, in multiple
languages.

### Everything included

One platform, the whole toolkit:

- **Memory that persists.** Agents keep transparent, editable memory files,
  per user and per agent.
- **Image generation & editing.** Generate and iterate on images in chat,
  plus a professional-grade editing pipeline for photos.
- **Video toolkit.** A full editing pipeline for agents: AI-generated footage
  and transitions, timeline edits, captions, voice-overs, and music.
- **Web browsing.** Add the browser tool from the community catalog to
  enable your agents to research the live web.
- **Community catalog.** Install ready-made agents, tools and skills in one
  click: a browser, GitHub, Notion, and more landing regularly.
- **Extensible by design.** Add capabilities with MCP tool servers: install
  from a manifest and assign tools per agent.
- **Usage & budgets.** Per-user and per-agent cost tracking with weekly or
  monthly limits.

See the full tour at [otodock.io/features](https://otodock.io/features).

## Everyone gets the right seat

Admins add each person to the agents they need.

| Three platform roles | Three roles on every agent |
|---|---|
| **Admin** — runs the platform and its users | **Manager** — builds and changes the agent itself |
| **Agent creator** — builds and configures new agents | **Editor** — works in the shared workspace every day |
| **Member** — works with the agents they're given | **Viewer** — sees the shared work, changes nothing |

Roles govern the shared space; everyone also keeps a private workspace of
their own.

## Locked down by default

Agents are powerful, so OtoDock assumes they can't be trusted. Every
server-side agent runs inside a kernel sandbox with always-on network
isolation, and you grant access one service at a time.

- **A kernel sandbox around every agent.** Each session runs in its own
  mount and process namespace. Folders are mounted automatically from each
  user's role per agent.
- **Network isolation.** Private ranges, your LAN, and cloud metadata
  endpoints are unreachable by design. MCP tools that need a local service
  can be granted scoped access by the admin, per agent.
- **Secure credentials.** Credentials are encrypted at rest and injected
  only per session. Agents can use them, but never see them.
- **Ready for teams.** SSO sign-in, two-factor auth, and per-user cost
  budgets come standard, from the first install.

[Read the security model →](https://docs.otodock.io/security/overview)

## Quick start

A Linux server with Docker is all you need (4 GB RAM minimum, see the
[sizing guide](https://docs.otodock.io/getting-started/installation#how-much-ram)).
Create a folder for the install, then run the script in it:

```bash
mkdir otodock && cd otodock
curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/main/scripts/install.sh
bash install.sh
```

The installer checks Docker, writes a `.env` with a generated database
password, handles the Ubuntu 24.04+ host step automatically when the host
needs it, downloads the release-pinned `docker-compose.yml`, and starts the
stack. Everything lands in the folder you run it from, and it performs
fresh installs only: it stops rather than touch an existing install.

Then open **http://localhost:8400**. A fresh install greets you with the
setup wizard: create your admin account, connect your AI, and you're
chatting with your first agent minutes later
([First run](https://docs.otodock.io/getting-started/first-run)). If your
users browse to the server by name or IP, set `DASHBOARD_PUBLIC_URL` in the
generated `.env`. Behind a reverse proxy, also set `TRUSTED_PROXY` to your
proxy's IP
([reverse proxy & HTTPS](https://docs.otodock.io/getting-started/installation#put-it-behind-https));
every optional knob is documented in the
[Configuration reference](https://docs.otodock.io/administration/configuration).

Building from source, bare-metal development, reverse proxy and HTTPS:
the [installation guide](https://docs.otodock.io/getting-started/installation)
covers them all.

## How it fits together

```
 dashboard/   React dashboard — chat, agents, tasks, files, admin
 proxy/       Platform core (FastAPI) — sessions, security, scheduling,
              the agent sandbox, and the WebSocket hub the dashboard talks to
 mcps/        MCP tool servers: OtoDock's custom set (files, memory, tasks,
              meetings, notifications, …) + community mirrors
 audio/       Speech package — STT / TTS / voice activity, provider-agnostic
 scripts/     Install, compose, backup/restore, and maintainer tooling
```

Agents run as Claude Code / Codex processes inside per-session kernel
sandboxes, talk to their tools over MCP, and stream every step back to the
dashboard. PostgreSQL holds the platform state.

**Roadmap:** remote machines, phone calls, the Android app, Projects & the
Dock, and more integrations (Google Workspace, Slack, Linear,
Microsoft 365, Zoom, and more). What each brings:
[docs.otodock.io/roadmap](https://docs.otodock.io/roadmap).

## Community

- **Docs:** [docs.otodock.io](https://docs.otodock.io)
- **Website:** [otodock.io](https://otodock.io)
- **Community agents:** [OtoDock/community-agents](https://github.com/OtoDock/community-agents)
- **Community MCPs:** [OtoDock/community-mcps](https://github.com/OtoDock/community-mcps)
- **Community skills:** [OtoDock/community-skills](https://github.com/OtoDock/community-skills)
<!-- COMMUNITY: Discord invite lands here at launch (Phase 8) -->

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Security
reports: [SECURITY.md](SECURITY.md).

## License

OtoDock is **fair source**: licensed under the
[Functional Source License, v1.1, with Apache 2.0 future grant](LICENSE)
(FSL-1.1-Apache-2.0). You can use, run, modify, and redistribute it for
anything except competing with OtoDock commercially — and each version
automatically becomes plain **Apache 2.0 two years** after its release.

Self-hosting is free for up to 5 users. Growing teams
[license by seats](https://otodock.io/pricing). Same software, signed key,
no feature gates on your data or your agents.

## A note from the author

OtoDock started with Claude Code. I wanted to use it beyond the terminal
and connect it to my services. So I built the platform around that idea.
It has since become the place where my own agents work: large parts of
OtoDock were built, tested, and shipped by agents running on OtoDock.

It's fair source so you can run it the same way. Make it yours, and show
me what you build: the
[discussions](https://github.com/OtoDock/oto-dock/discussions) are open.

— Dimitris Mourtzis
