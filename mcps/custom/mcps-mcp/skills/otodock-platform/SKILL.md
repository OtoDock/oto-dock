---
name: otodock-platform
description: "How the OtoDock platform itself works and where every setting lives — installing and first-run setup, agents, AI engines, sessions, departments, shared knowledge libraries, tasks/triggers/notifications, remote machines, voice, and the role-labeled settings map. Use when helping a user set up, configure, administer, troubleshoot, or navigate OtoDock."
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
  The platform warms, resumes, and idles them automatically.
- **Tools (MCPs)** give agents abilities; **skills** (like this one) give technique.
- Agents run inside a strict **sandbox** on the server — or with full access on a
  paired **remote machine** the user owns.

## The two role systems — always state the required role

The dashboard is role-gated: users simply don't see what they can't use. So when you
point someone somewhere, **say which role it needs** — that's the difference between
help and confusion. Two independent systems:

**Platform roles** (whole installation): **admin** → runs the platform (all admin pages,
engines, users, MCP installs); **creator** → additionally creates/installs agents and
manages their own departments; **member** → uses the agents they've been given.

**Per-agent roles** (each agent separately): **manager** → configures the agent
(persona, tools, skills, settings); **editor** → works in the shared workspace;
**viewer** → chats, with a private personal space. A person can be manager of one agent
and viewer of another. Platform admins override per-agent roles.

Common surprises worth pre-empting: department assignment and shared-knowledge wiring
need platform **admin/creator** (agent managers alone can't); agent-scope schedules need
per-agent **editor+**; installing anything (tools, skills, agents from the community
catalog) is **admin** — managers and agents file requests admins approve.

## Where things live (summary — full map in references/settings-map.md)

- **User Settings** (avatar menu): General (profile, security/2FA, appearance, wake
  word, memory) · Integrations (connected accounts, API keys) · Remote Machines · AI
  Engines (personal subscriptions) · Audio · Usage.
- **Agent Settings** (per agent): Overview · MCPs · Skills · Configuration (persona
  file, engines/model, visibility mode, department, delegation targets, shared
  knowledge, memory) · Monitoring (scheduled tasks, triggers, notifications, meetings).
- **Admin** (admins only): Users · Usage · MCP Servers · Skills · MCP Requests · Remote
  Machines · Monitoring · **Setup** (tabs: General · AI Engines · OtoDock · Audio ·
  Phone · Security · System Settings).
- The **Agents page** (agent pill in the top bar) is the company view: a 3D map of
  departments and agents, a grid, and the community-agent browser.

## What you can do yourself vs. hand to the user

You (an agent) can, with the right session role: create and manage tasks, triggers, and
notifications; browse the tool/skill catalogs and enable or request them; update your
own persona; manage knowledge libraries and department assignment via your
self-configuration tools (each change confirmed in chat); build dashboards; delegate to
wired agents; and read other agents' activity your user could see.

You cannot: install catalog packages (admin approves your request), pair machines,
manage users, change platform settings, or connect engines/accounts — for those, give
the user the exact page, tab, and role from `references/settings-map.md`.

## Routing — read the reference that matches

| Question is about | Read |
| --- | --- |
| Installing, first run, connecting Claude/ChatGPT/keys, voice & phone add-on, pairing remote machines | `references/setup.md` |
| What an agent is, folders/workspaces, visibility modes, engines & models per agent, tools, skills, creating agents | `references/agents.md` |
| Departments, the company map, delegation, meetings, shared knowledge libraries, bulletins, memory — running a company on OtoDock | `references/company-management.md` |
| Schedules, one-time and trigger-fired tasks, model pinning, webhooks, event subscriptions, notifications | `references/automation.md` |
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
