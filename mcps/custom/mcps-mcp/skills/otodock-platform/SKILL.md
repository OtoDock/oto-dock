---
name: otodock-platform
description: "How the OtoDock platform itself works and where every setting lives — installing and first-run setup, agents, AI engines, sessions, apps and sharing, checks, departments and delegation, shared knowledge libraries, tasks/triggers/notifications, remote machines, voice, and the role-labeled settings map. Use when helping a user set up, configure, administer, troubleshoot, or navigate OtoDock."
---

# OtoDock Platform Guide

You are an agent **running on OtoDock** — a self-hosted platform for teams of AI agents.
This skill is your manual for the platform itself, so you can help users set it up, run
it, and find things in it. Answer from here instead of guessing; when a question goes
deeper than this file, read the matching reference in `references/`.

## The mental model (30 seconds)

- **The server** runs everything: agents, files, memory, schedules, tools, and the web
  **dashboard** users drive it from.
- **Agents** are configured workers — persona + files + tools + memory + settings. Users
  chat with them, schedule them, and let them collaborate.
- **AI engines** power agents' thinking: **Claude Code** and **Codex** (users connect
  their own Claude/ChatGPT subscriptions or API keys) plus a lightweight **direct**
  engine for low-latency work like phone calls. No model ships with the platform.
- **Sessions** are live running instances of an agent (a chat, a task run, a call).
  The platform warms, resumes, and idles them automatically. A message typed while the
  agent works reaches a Claude Code or Codex turn at its next step, otherwise it waits
  as a faded "queued" bubble at the end of the conversation (every tab of the chat sees
  it) and goes out with the next turn. A turn that cannot finish ends with a card that says why and, where a
  retry helps, **Send again**.
- **Apps** are pages an agent builds and keeps running (buttons that put it to work,
  live data, a server and database when needed): **shared** with the agent's team or
  **personal**, shareable with a person, another agent or a department, or by
  link. The first opens as the agent's home; a person approves what each may do.
- **Checks** judge an agent's work at the end of a turn and send it back to fix what
  they find.
- **Tools (MCPs)** give agents abilities; **skills** (like this one) give technique.
- Agents run inside a strict **sandbox** on the server — or with full access on a
  paired **remote machine** the user owns. The machine's agent software updates itself
  at reconnect; an update ends the turns running there and the chat continues from the
  next message.

## The two role systems — always state the required role

The dashboard is role-gated: users simply don't see what they can't use. So when you
point someone somewhere, **say which role it needs** — that's the difference between
help and confusion. Two independent systems:

**Platform roles** (whole installation): **admin** → runs the platform (all admin pages,
engines, users, MCP installs); **creator** → additionally creates/installs agents and
manages their own departments; **member** → uses the agents they've been given.

**Per-agent roles** (each agent separately): **manager** → configures the agent
(persona, tools, skills, settings); **editor** → works in the shared workspace and
automates as the agent (agent-scope tasks, triggers, delegation, shared apps, shared
memory); **contributor** → adds files to the shared workspace and nothing else — for an
outside collaborator on a project who must never act as the agent; **viewer** → chats,
with a private personal space. A person can be manager of one agent and viewer of
another. Platform admins override per-agent roles. On a **Shared only** agent every chat
runs as the agent itself, which takes **editor+**, so such an agent holds **editor** and
**manager** assignments only: nothing lower is offered or accepted there, and switching
an agent to Shared only names the viewers and contributors who lose their assignment and
asks a manager to confirm.

Common surprises worth pre-empting: department assignment and shared-knowledge wiring
need platform **admin/creator** (agent managers alone can't); agent-scope schedules and
triggers, and pinning, approving or sharing a shared app, need per-agent **editor+**;
installing tools and skills is **admin**, installing an agent from the community catalog
**admin/creator** — managers and agents file requests admins approve.

## Where things live (summary — full map in references/settings-map.md)

- **User Settings** (avatar menu): General (profile, security/2FA, appearance, wake
  word, memory) · Integrations (connected accounts, API keys) · Remote Machines · AI
  Engines (personal subscriptions) · Audio · Usage.
- **Agent Settings** (per agent): Overview · MCPs · Skills · Configuration (persona
  file, engines/model, visibility mode, department, delegation targets, shared
  knowledge, memory) · Checks · Monitoring (scheduled tasks, triggers, notifications,
  meetings).
- **Apps**: the apps button beside the chat composer and each app's ⋯ menu; a chat is
  shared from its row in the chat history.
- **Documents** an agent delivers open in the chat's document pane (the right half of
  the chat; a floating window on a phone and in a terminal chat), one tab per document,
  where the person can edit and save them (on a phone the tick ends editing and saves)
  and reopen earlier deliveries read-only under its Versions button. The pane saves when
  it is minimized or hidden and before a message is sent, so the agent's next turn reads
  the edit.
- **Admin** (admins only): Users · Usage · MCP Servers · Skills · MCP Requests · Remote
  Machines · Shares · Monitoring · **Setup** (tabs: General · AI Engines · OtoDock ·
  Audio · Phone · Security · System Settings).
- The **Agents page** (agent pill in the top bar) is the company view: a 3D map of
  departments and agents, a grid, and the community-agent browser.

## What you can do yourself vs. hand to the user

You (an agent) can, with the right session role: create and manage tasks, triggers, and
notifications; browse the tool/skill catalogs and enable or request them; as an editor
or above, package an MCP and check it with `validate_mcp_package` (skill
`mcp-authoring`) before handing the zip to an admin; update your own persona; manage knowledge libraries and department assignment via your
self-configuration tools (each change confirmed in chat); build, check, deploy and pin
apps, push to them, open them on the user's screen and roll them back; attach and run
checks; delegate to wired agents (a worker's result carries the files it attached into
your workspace); call what an app placed in your agent exports; and read other agents'
activity your user could see.

You cannot: approve an app, share an app or a chat, set an app's secret, install
catalog packages (admin approves your request), pair machines, manage users, change
platform settings, or connect engines/accounts (a hosted MCP server's own sign-in is
the person's too, under User Settings → Integrations) — for those, give the user the
exact page, tab, and role from `references/settings-map.md`.

## Routing — read the reference that matches

| Question is about | Read |
| --- | --- |
| Installing, first run, connecting Claude/ChatGPT/keys, voice & phone add-on, pairing remote machines | `references/setup.md` |
| What an agent is, folders/workspaces, visibility modes, engines & models per agent, tools, skills, creating agents | `references/agents.md` |
| Apps (shared, personal, the agent's home), the approval card, releases and rollback, secrets, templates and `.otoapp`, sharing apps and chats (people, agents, departments, "Shared with you", links) | `references/apps.md` |
| Departments, delegation modes and targets, the company map, meetings, shared knowledge libraries, bulletins, memory — running a company on OtoDock | `references/company-management.md` |
| Schedules, one-time and trigger-fired tasks, model pinning, webhooks, event subscriptions, checks, notifications | `references/automation.md` |
| "Where is the setting for X?" / "who can do X?" | `references/settings-map.md` |

## House rules for platform help

- **Name the place exactly** — page → tab → control, with the role in parentheses:
  "Under **Setup → Audio** *(admin)*, add a Deepgram key…".
- **Check before promising**: features can be switched off per install (remote machines,
  interactive terminals, chat audio, user-paired machines) — if a user can't see what
  you describe, the feature may be disabled or their role too low; say which.
- **Voice, phone, and live voice conversations** need the phone service add-on; plain
  dictation and read-aloud don't.
- **Never ask users for secrets** (API keys, tokens, passwords) in chat — point them at
  the settings page that stores credentials encrypted.
- The public docs live at **https://docs.otodock.io** — link a page when the user wants
  the long-form read, and fetch a page from there yourself when you need detail beyond
  this skill (the site covers every feature; this skill carries the operating model and
  the settings map).
