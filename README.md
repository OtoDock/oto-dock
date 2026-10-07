<p align="center">
  <img src=".github/media/logo-mark.png" alt="OtoDock" width="112" />
</p>

<p align="center"><b>OtoDock — Collaborative Agents</b></p>

<h1 align="center">The agentic company OS.</h1>

<p align="center">
  The brains of your company, built on Claude Code &amp; Codex, working on your Anthropic and OpenAI subscriptions.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: FSL-1.1-Apache-2.0" src="https://img.shields.io/badge/license-FSL--1.1--Apache--2.0-146bb5"></a>
  <img alt="Version" src="https://img.shields.io/badge/version-1.7.1-146bb5">
  <a href="https://github.com/OtoDock/oto-dock/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/OtoDock/oto-dock/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://docs.otodock.io"><img alt="Docs" src="https://img.shields.io/badge/docs-docs.otodock.io-0d9488"></a>
  <a href="https://otodock.io"><img alt="Website" src="https://img.shields.io/badge/website-otodock.io-673a97"></a>
</p>

<p align="center">
  <a href="#quick-start">Install</a> ·
  <a href="#the-two-minute-video">Video</a> ·
  <a href="https://docs.otodock.io">Docs</a> ·
  <a href="https://otodock.io/features">Features</a> ·
  <a href="https://github.com/OtoDock/oto-dock/discussions">Discussions</a> ·
  <a href="https://x.com/otodock">X</a> ·
  <a href="https://www.linkedin.com/company/otodock">LinkedIn</a>
</p>

<br/>

<p align="center">
  <b>Self-hosted · Multi-tenant by design · Fair source</b><br/>
  <sub>Runs on&nbsp; Claude Code · Codex · your API keys · local models</sub>
</p>

<br/>

<p align="center">
  <img src="https://github.com/user-attachments/assets/3a050b1b-0af3-42f5-abe5-0e4c95a26d6e" alt="Seven faces of the OtoDock dashboard: the company map, an agent's home, an Excel file in the chat, an artifact, the tools page, a meeting and the terminal" width="100%" />
</p>

<p align="center"><em>Dashboard highlights. <a href="#the-two-minute-video">The two-minute video</a> tells the whole story.</em></p>

<p align="center">If OtoDock is useful to you, a star on this repository helps other people find it.</p>

---

## Why OtoDock

OtoDock acts as the brain of your company. You create powerful agents that
connect to the tools your company runs on, work in departments, delegate to
each other, and keep working on their own when no one is watching. Your
agents can build and deploy internal apps for your team, or even public ones
you share with your customers.

OtoDock is multi-tenant by design. Many people work with the same agents,
and four modes decide how an agent is shared and where its work lands. You
work with your agents on your self-hosted dashboard, or you can even give
them a phone line and talk to them. All of it runs on your own Anthropic and
OpenAI subscriptions, and on local models.

## Quick start

You need a Linux server with Docker, x86-64 or arm64, and at least 4 GB of RAM
(see the [sizing guide](https://docs.otodock.io/getting-started/installation#how-much-ram)).
A Mac with Apple silicon, such as a Mac mini, can run it too with Docker
Desktop. Create a folder for the install, then run the script in it:

```bash
mkdir otodock && cd otodock
curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/main/scripts/install.sh
bash install.sh
```

The script checks Docker, writes a `.env` with a generated database password,
downloads the release-pinned compose files and starts the stack. It performs
fresh installs only. To upgrade later, download `scripts/upgrade.sh` into the
install folder the same way and run it
([upgrading](https://docs.otodock.io/administration/upgrading)).

The [installation guide](https://docs.otodock.io/getting-started/installation)
covers the server's address, HTTPS behind a reverse proxy, the phone service
and building from source, and the
[configuration reference](https://docs.otodock.io/administration/configuration)
documents every option.

### The first five minutes

Open **http://localhost:8400**, or your server's address on port 8400, and
the setup wizard greets you. Create the owner account, and OtoDock installs
the Personal Assistant for you, with the tools it needs. Connect your Claude
or ChatGPT subscription under Setup, AI Engines. Then open the Personal
Assistant from the Agents page and send it your first message. Its reply
streams in live, with every tool call and every file it creates
([First run](https://docs.otodock.io/getting-started/first-run)). From there
you add agents from the community catalog or build your own, and you add the
people who will work with them.

## Build your own AI agents

Every agent gets a name and a job, like a Personal Assistant, a System Admin
or a Marketing Manager, and runs on your server. An agent is made of six
parts, and you can edit every one of them.

- **Persona.** Plain-language instructions tell the agent who it is and how
  it works.
- **Memory.** The agent keeps its own notes across chats.
- **Workspace.** The folder where the everyday work lands. It is private for
  each person, shared by the whole team, or both, depending on the agent's
  mode.
- **Knowledge.** Reference documents are always on hand.
- **Skills.** The agent learns techniques it can apply to its work.
- **Tools.** The agent uses only what it is allowed to use.

<p align="center">
  <img src=".github/media/story-anatomy.jpg" alt="An agent with its six parts around it: persona, memory, workspace, knowledge, skills and tools" />
</p>

## Four ways to share one agent

Every agent has a mode that decides how people share it.

| Mode | What it means |
|---|---|
| **Personal only** | A private workspace for each person. |
| **Personal + shared** | A private workspace for each person as the default, and a shared one for the team. |
| **Shared + personal** | A shared team workspace as the default for everyday work, plus a private one per person. |
| **Shared only** | Everyone shares one history and one workspace. |

## Everyone gets the right seat

Admins add each person to the agents they need.

| Three platform roles | Four roles on every agent |
|---|---|
| An **Admin** runs the platform. | A **Manager** has full control of the agent. |
| A **Creator** creates agents. | An **Editor** edits the shared files and automates as the agent. |
| A **Member** uses the agents they are given. | A **Contributor** adds files to the shared workspace, nothing more. |
| | A **Viewer** chats and reads the shared files. |

<p align="center">
  <img src=".github/media/story-roles.jpg" alt="The three platform roles beside the four roles on every agent: manager, editor, contributor and viewer" />
</p>

## Claude Code or Codex on your subscription, or local models.

Claude Code runs on your Anthropic subscription, and Codex runs on your
ChatGPT subscription. Every user connects their own subscription. Local
models on your own hardware work as well. You pick the engine per agent,
and you can switch it per chat.

<p align="center">
  <img src=".github/media/story-engines.jpg" alt="One agent over three cards: Claude Code and Codex on your subscription, and local models on your hardware" />
</p>

## They work while you are away

Agents run on any schedule, when a webhook event fires, or once at a time
you choose. They work in your workspace, where their reports, files and
updates land, and they notify you when something needs you. Notifications
come in four severities, from a quiet chime to a persistent danger alarm.
Every run is a full chat you can open, read and continue, so no one has to
be watching.

Checks hold that work to your standard. A check judges what the agent did at
the end of a turn, in a chat, a task run or a delegation. If the work falls
short, the agent gets the findings and fixes them in another round.

<p align="center">
  <img src=".github/media/story-automation.jpg" alt="Three triggers feeding an agent that works in the workspace and sends notifications" />
</p>

## Build and deploy full apps, just by asking your agent.

An agent can build and deploy its own apps. An app is a page with its own
address, so you can bookmark it and come back to it any time. It can have
buttons that put the agent to work, show live data from the platform and,
when it needs one, run its own server and database.

- **Apps for you or your team.** They live inside OtoDock, on the agent. A
  developer agent can keep an app that shows your GitHub issues, pull
  requests and builds at a glance, or a project board where a button on a
  card hands the work to the agent.
- **Apps for your customers.** A public link opens the app for anyone, with
  no OtoDock account. Think of a hairdresser's booking page: clients open
  the link, pick a time and pay.

<p align="center">
  <img src=".github/media/story-apps.jpg" alt="A team board with the team's avatars, its own database, platform tools, schedules and notifications, beside a booking page shared by link and opened on a customer's phone" />
</p>

You can share an app or a chat with a teammate, place an app in another
agent's or a whole department's apps, where that agent's chats and tasks
call what the app offers, or share either with anyone by a link.

## Give an agent a phone number

Agents answer and place phone calls. Bring your own Twilio account, or
connect the Asterisk or FreePBX server you already run.

<p align="center">
  <img src=".github/media/story-phone.jpg" alt="An agent with a phone number, taking incoming and outgoing calls over Twilio or Asterisk and FreePBX" />
</p>

## Runs on your server. Works on your machines.

By default, all your agents run on your server, in an isolated sandbox, with
one dashboard controlling all of them. You can also pair a laptop, a
workstation or a PC running macOS, Linux or Windows with a one-line install.
The machine keeps a single outbound connection to your server, so it needs
no open ports, and its files stay in sync. The same agent then runs on that
machine with full access to it, and you work with it from the same
dashboard, anywhere. If the machine goes offline, your server takes over.

<p align="center">
  <img src=".github/media/story-remote.jpg" alt="Your server with its agents, linked to a paired laptop, workstation and PC" />
</p>

<h3 align="center">Your own cloud of agents.</h3>

## The two-minute video

https://github.com/user-attachments/assets/743de1c6-e582-42f6-9996-cde277befbf2

<p align="center"><em>This entire video was directed, captured and edited by an OtoDock agent.
<a href="https://otodock.io">Watch it in full quality on otodock.io</a>.</em></p>

## The company, running

<table>
  <tr>
    <td width="50%"><img src=".github/media/screen-map.jpg" alt="The 3D company map: the Engineering, Marketing and Independent stages with their agents and the delegation lines between them" /></td>
    <td width="50%"><img src=".github/media/screen-dashboard.jpg" alt="The CEO agent's home: an app with the company graph and the day's numbers, kept fresh by the agent" /></td>
  </tr>
  <tr>
    <td><b>Departments.</b> Organize your agents into departments, or put them in a meeting together. A department's delegation mode decides who can hand work to whom: <b>Down only</b>, <b>Down and across</b>, <b>All directions</b> or <b>Off</b>.</td>
    <td><b>Apps.</b> Your agents build and deploy apps inside OtoDock from a single prompt, for your team or for your customers.</td>
  </tr>
  <tr>
    <td><img src=".github/media/screen-tools.jpg" alt="The tools page: the tool servers assigned to an agent, each one switched on or off" /></td>
    <td><img src=".github/media/tour-documents.jpg" alt="An Excel workbook opened and edited inside the chat, sheets and charts included" /></td>
  </tr>
  <tr>
    <td><b>Endless tools.</b> Your agents are digital employees. They connect to endless tools, from your calendar to your smart home.</td>
    <td><b>Documents.</b> They edit Excel, Word and PowerPoint files right in the chat.</td>
  </tr>
  <tr>
    <td><img src=".github/media/screen-artifact.jpg" alt="An interactive party budget built by the agent, rendered inside the chat" /></td>
    <td><img src=".github/media/screen-meeting.jpg" alt="The CEO agent opening a meeting with the developer agent, its work streaming live in the chat" /></td>
  </tr>
  <tr>
    <td><b>Artifacts.</b> Agents answer with live, interactive UI, rendered right in the chat.</td>
    <td><b>Meetings.</b> Specialist agents discuss a topic together and converge on an answer, live in the dashboard.</td>
  </tr>
  <tr>
    <td><img src=".github/media/screen-terminal.jpg" alt="A Claude Code terminal running inside the dashboard with the agent's tools loaded" /></td>
    <td><img src=".github/media/screen-remote-machines.jpg" alt="Your server and the paired machines it runs agents on" /></td>
  </tr>
  <tr>
    <td><b>Terminal.</b> The Claude Code or Codex terminal opens in the dashboard, with the agent's tools already loaded.</td>
    <td><b>Remote machines.</b> The same agent, with all its tools and its synced workspace, runs on any machine you pair with one command, and it can use everything on that machine.</td>
  </tr>
</table>

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

## Everything included

One platform, the whole toolkit.

- **Memory that persists.** Agents keep transparent, editable memory files,
  per user and per agent.
- **Documents and files.** Agents create and edit Word, Excel, PowerPoint
  and PDF files, and every file opens in a live editor inside the chat.
- **Images.** Agents generate and edit images in the chat, and a
  professional-grade pipeline handles photo editing.
- **Video and audio.** Agents generate footage and transitions, cut
  timelines, add captions, voice-overs and music, transcribe audio and video
  into text and subtitles, and produce speech and music of their own.
- **Web browsing.** The browser tool from the community catalog lets agents
  research the live web.
- **Built-in tools.** Schedules, triggers, notifications, checks, meetings,
  delegation, phone calls, file transfer between agents, live charts and
  apps, SSH hosts, and browser and computer control on paired machines
  all ship with the platform.
- **Community catalog.** Ready-made agents, tools and skills install in one
  click, with GitHub, Notion, Home Assistant, Nextcloud, Prometheus, UniFi
  and Uptime Kuma among them, and more landing regularly. Agent templates
  arrive with their apps and checks, and follow new versions.
- **Extensible by design.** Any MCP tool server installs from a manifest,
  and tools are assigned per agent.
- **Usage and budgets.** Costs are tracked per user and per agent, with
  weekly or monthly limits.
- **Team-ready security.** SSO and OIDC, two-factor auth, per-agent roles,
  encrypted credentials and scoped API keys come from day one.

See all features at [otodock.io/features](https://otodock.io/features).

## How it fits together

```
 dashboard/   React dashboard: chat, agents, apps, tasks, files, admin
 proxy/       Platform core (FastAPI): sessions, security, scheduling,
              the agent sandbox, and the WebSocket hub the dashboard talks to
 mcps/        MCP tool servers: OtoDock's custom set (files, memory, tasks,
              meetings, notifications, …) plus community mirrors
 audio/       Speech package: STT, TTS and voice activity, provider-agnostic
 phone/       Telephony daemon: live calls over Twilio or FreePBX/Asterisk
 satellite/   Remote-machine agent: pairs your own hardware to the platform
 scripts/     Install, compose, backup/restore, and maintainer tooling
```

Agents run as Claude Code or Codex processes inside per-session kernel
sandboxes, talk to their tools over MCP, and stream every step back to the
dashboard. PostgreSQL holds the platform state.

## Community

- **Questions and ideas** go to
  [Discussions](https://github.com/OtoDock/oto-dock/discussions), and so do
  the things you build, in
  [Show and tell](https://github.com/OtoDock/oto-dock/discussions/categories/show-and-tell).
- **Bugs** go to [issues](https://github.com/OtoDock/oto-dock/issues), where
  the forms ask for what a maintainer needs first.
- **Security reports** go privately through [SECURITY.md](SECURITY.md),
  never into a public issue.
- **The docs** at [docs.otodock.io](https://docs.otodock.io) cover
  installing, running and extending OtoDock, and
  [otodock.io](https://otodock.io) tells the whole story.

Watch this repository's releases to hear about each new version. Every
release carries its notes, and the [CHANGELOG](CHANGELOG.md) keeps them all.
For news, follow OtoDock on [X](https://x.com/otodock) and
[LinkedIn](https://www.linkedin.com/company/otodock).

## Contributing

Contributions are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) takes you from
a clone to green tests and explains how a pull request gets merged.
Ready-made agents, tools and skills live in their own catalogs, and new
entries go there:
[community-agents](https://github.com/OtoDock/community-agents),
[community-mcps](https://github.com/OtoDock/community-mcps) and
[community-skills](https://github.com/OtoDock/community-skills).

## License

OtoDock is **fair source**: licensed under the
[Functional Source License, v1.1, with Apache 2.0 future grant](LICENSE)
(FSL-1.1-Apache-2.0). You can use, run, modify, and redistribute it for
anything except competing with OtoDock commercially, and each version
automatically becomes plain **Apache 2.0 two years** after its release.

Self-hosting is free for up to 5 users. Growing teams
[license by seats](https://otodock.io/pricing). Same software, signed key,
no feature gates on your data or your agents.

## A note from the author

OtoDock started with Claude Code. I wanted to use it beyond the terminal
and connect it to my services. So I built the platform around that idea.
It has since become the place where my own agents work: large parts of
OtoDock were built, tested, and shipped by agents running on OtoDock.

It's fair source so you can run it the same way for your company, and you
can share your builds in the
[discussions](https://github.com/OtoDock/oto-dock/discussions).

— Dimitris Mourtzis
