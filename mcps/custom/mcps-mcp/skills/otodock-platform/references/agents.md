# Agents — anatomy, folders, modes, engines, tools, skills, creation

## What an agent is

A self-contained worker: **persona** (its role and working style) + **knowledge**
(reference library) + **tools** + **memory** + **workspace(s)** + **settings** (engines,
model, visibility mode, department, delegation targets). Each install ships one ready
agent (the Personal Assistant); the rest are created or installed.

## The folder model (what agents and users see)

- `config/agent.md` — the persona, loaded first into every session. `config/context/`
  — markdown auto-loaded into every conversation. `config/user-setup.md` — optional
  per-user onboarding. `config/checks/` — the agent's checks (automation.md). The whole
  `config/` tree is **manager-only**.
- `knowledge/` — reference files, read on demand, never auto-loaded. Managers curate;
  can be shared across agents as libraries (see company-management.md).
- `workspace/` — the shared team workspace (contributors+ write).
- `users/<name>/workspace/` + `users/<name>/context/` — each person's private space and
  personal auto-loaded context. Private, always.
- `apps/<slug>` inside the shared workspace or a person's workspace — the working files
  of a shared or a personal app; viewers see its release, never these (apps.md).
- In chat, the file overlay shows these as chips: **My Workspace · My Context · Shared
  Workspace · Knowledge · Agent Config** (config chip only for managers).

Per-agent roles map onto the folders: **viewer** = own space RW, everything else RO;
**contributor** = + shared workspace RW (files only — never agent-scope tasks, triggers,
delegation, shared apps or shared memory writes); **editor** = the same files, plus acting
as the agent; **manager** = + knowledge and config RW.

## Visibility & workspace modes (Agent Settings → Configuration, manager)

- **Personal + shared** — private space per person *and* a shared workspace; agent works
  in the personal space by default. Good default for collaborative agents.
- **Shared + personal** — same two spaces, shared is home base.
- **Personal only** — fully private per person (own files, chats, memory).
- **Shared only** — one space, one shared chat history for the whole team. Everything
  there runs as the agent itself, so chatting takes **editor+**: a viewer or contributor
  can read the history, but a chat, or a personal task they made before the switch,
  stops with a message. To let them chat, give them editor or switch to a mode with
  personal chats.

Switching modes never deletes files. Chats on shared agents are visible per mode; on a
colleague's live interactive terminal you get read-only + a **Take over** button.

## Engines, models, effort (Agent Settings → Configuration, manager)

- Enable one or more **AI Engines** per agent (an engine needs a platform subscription
  to be enabled), pick a **Default Model** (or Auto), a **Default Session Mode**
  (headless vs interactive terminal), and a **Default Effort** (thinking depth).
- **Auto** (no pinned model) runs the engine's own default, chosen by tier: Claude Opus
  5.5 on Claude Code, GPT-6 Sol on Codex, Sonnet 5 on Direct LLM. Since 1.7 Claude Opus
  5.5 replaces Opus 5, and GPT-6 Sol and GPT-6 Luna replace their GPT-5.6 versions;
  pins on the old ids move over at the next start.
- A message typed while the agent works reaches it mid-turn, at its next tool step (in
  the sandbox, on a paired machine, and in a delegated worker still working).
- A chat's permission mode sits in its status bar (**Default**, **Accept Edits**,
  **Plan**, **Don't Ask**); a Codex chat asks before commands and edits the way a Claude
  chat does.
- Users switch models per chat from the picker; when a chat's session has ended, other
  enabled engines appear too — picking one restarts the chat on that engine from saved
  history (works on finished task chats as well, with editor access).
- Both pickers order models by capability tier and mark each with four dots (●●●●
  frontier … ●○○○ fast; none = untiered); hover for the tier word and what the model
  is good at.
- Scheduled tasks can pin their own model/engine — see automation.md.

## Tools (MCPs)

Two switches gate every tool: **authorized** (admin — installed/configured platform
level, sometimes via per-agent instances) and **enabled** (manager — Agent Settings →
MCPs). Core tools (memory, tasks, delegation, triggers, notifications, meetings,
display and apps, checks (for agents created since 1.7), files/documents, self-config,
catalog browser) are on by default; optional and
community tools are enabled per agent. Agents can list what's available and either
enable directly (when the manager asks and it's allowed) or file an admin request.
Integrations (GitHub, Notion, and more as the catalog grows) connect per user under
**User Settings → Integrations**; shared agents get a **service account** — a manager
binds one of their own connected accounts on Agent Settings → MCPs; beneath the
binding the manager subscribes the agent to that vendor's events (needed before an
agent trigger or a shared app can react to them).

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
   onboarding, apps (shared, or one personal copy per member, one of which can be the
   agent's setup page) and checks, approved once on the install dialog — an admin's
   consent covers every member's copy, a creator's the shared apps and their own copy
   (members approve theirs); a personal copy that acts as the member is always that
   member's own approval. Missing admin pieces (tool instances, skill packages) become
   pending to-dos, not failures. When the catalog carries a newer version of an
   installed agent's template, the card and the agent's Config tab offer **Update**
   *(any manager of that agent, whatever their platform role — an admin manages every
   agent; an update also switches on the core tools the platform gained since the
   install)*: what nobody changed is replaced, what a manager edited is kept with the
   new version stored beside it ("Take the new version" per piece), new pieces are
   added, and what a manager removed or
   disabled since the install (a task, a check, a context file, an MCP, a hidden app
   copy) stays that way — the report lists it as left out. An agent made by "Ask an
   agent" never updates.
3. **Ask an agent** — any agent with the agent-creation tool interviews the user, writes
   the template, and installs it with the user as manager; un-installed tools go through
   the admin request queue.

Round out a new agent with: knowledge files, `config/context/` rules, a department +
delegation targets, schedules, checks where the work has a standard to meet, and a
shared app as its home.

## Where agents run

Server sandbox by default (locked-down filesystem + network, per-service allowlists).
An admin can set an agent's **Execution Target** to an admin-paired remote machine;
users can run agents on their own paired machines. Device-control tools (browser,
computer) exist only on remote machines with the matching grant.

In the server sandbox, packages installed into HOME vanish when the session ends. An
agent keeps Python packages across sessions in a virtualenv inside its workspace
(`uv venv .venv`, then `uv pip install --python .venv/bin/python <pkg>`): it persists,
never syncs to remote machines, and counts toward the agent's storage quota —
`node_modules` in the workspace behaves the same way.
