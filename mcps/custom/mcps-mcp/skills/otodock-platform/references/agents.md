# Agents — anatomy, folders, modes, engines, tools, skills, creation

## What an agent is

A self-contained worker: **persona** (its role and working style) + **knowledge**
(reference library) + **tools** + **memory** + **workspace(s)** + **settings** (engines,
model, visibility mode, department, delegation targets). Each install ships one ready
agent (the Personal Assistant); the rest are created or installed.

## The folder model (what agents and users see)

- `config/agent.md` — the persona, loaded first into every session. `config/context/`
  — markdown auto-loaded into every conversation. `config/user-setup.md` — optional
  per-user onboarding. The whole `config/` tree is **manager-only**.
- `knowledge/` — reference files, read on demand, never auto-loaded. Managers curate;
  can be shared across agents as libraries (see company-management.md).
- `workspace/` — the shared team workspace (editors+ write).
- `users/<name>/workspace/` + `users/<name>/context/` — each person's private space and
  personal auto-loaded context. Private, always.
- In chat, the file overlay shows these as chips: **My Workspace · My Context · Shared
  Workspace · Knowledge · Agent Config** (config chip only for managers).

Per-agent roles map onto the folders: **viewer** = own space RW, everything else RO;
**editor** = + shared workspace RW; **manager** = + knowledge and config RW.

## Visibility & workspace modes (Agent Settings → Configuration, manager)

- **Personal + shared** — private space per person *and* a shared workspace; agent works
  in the personal space by default. Good default for collaborative agents.
- **Shared + personal** — same two spaces, shared is home base.
- **Personal only** — fully private per person (own files, chats, memory).
- **Shared only** — one space, one shared chat history for the whole team.

Switching modes never deletes files. Chats on shared agents are visible per mode; on a
colleague's live interactive terminal you get read-only + a **Take over** button.

## Engines, models, effort (Agent Settings → Configuration, manager)

- Enable one or more **AI Engines** per agent (an engine needs a platform subscription
  to be enabled), pick a **Default Model** (or Auto), a **Default Session Mode**
  (headless vs interactive terminal), and a **Default Effort** (thinking depth).
- Users switch models per chat from the picker; when a chat's session has ended, other
  enabled engines appear too — picking one restarts the chat on that engine from saved
  history (works on finished task chats as well, with editor access).
- Scheduled tasks can pin their own model/engine — see automation.md.

## Tools (MCPs)

Two switches gate every tool: **authorized** (admin — installed/configured platform
level, sometimes via per-agent instances) and **enabled** (manager — Agent Settings →
MCPs). Core tools (memory, tasks, delegation, triggers, notifications, meetings,
display, files/documents, self-config, catalog browser) are on by default; optional and
community tools are enabled per agent. Agents can list what's available and either
enable directly (when the manager asks and it's allowed) or file an admin request.
Integrations (GitHub, Notion, and more as the catalog grows) connect per user under
**User Settings → Integrations**; shared agents get a **service account** — a manager
binds one of their own connected accounts on Agent Settings → MCPs.

## Skills

Instruction packages in the standard SKILL.md format. Sources: the community skills
catalog (**Browse Community Skills** — install is admin, enable is manager), bundled
with MCPs (ride the tool's enablement), or an admin-uploaded ZIP (community package
format or a bare SKILL.md folder). Two loading modes: **always in context** (inlined in
the system prompt) vs **on demand** (loaded when the task matches the description —
the default). Per-agent enablement lives on **Agent Settings → Skills** *(manager)*.
Packages bundling executable scripts carry a "bundles scripts" badge.

## Creating agents (platform creators and admins)

Three paths from the **Agents** page:

1. **Create Agent** — empty agent; write the persona (`config/agent.md`) yourself or let
   the agent draft it in a manager chat. Personas hold role/working style/judgment —
   NOT capability lists (tools document themselves) and NOT day-to-day facts (that's
   memory). While a persona is empty, managers get an in-chat reminder.
2. **Browse Community** — ready-made agent templates with tools, skills, starter tasks,
   dashboards, onboarding. Missing admin pieces (tool instances, skill packages) become
   pending to-dos, not failures.
3. **Ask an agent** — any agent with the agent-creation tool interviews the user, writes
   the template, and installs it with the user as manager; un-installed tools go through
   the admin request queue.

Round out a new agent with: knowledge files, `config/context/` rules, a department +
delegation targets, schedules, and a pinned dashboard.

## Where agents run

Server sandbox by default (locked-down filesystem + network, per-service allowlists).
An admin can set an agent's **Execution Target** to an admin-paired remote machine;
users can run agents on their own paired machines. Device-control tools (browser,
computer) exist only on remote machines with the matching grant.
