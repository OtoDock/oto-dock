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
  Workspace · Knowledge · Agent Config** (config chip only for managers). People add
  files, whole folders included, by dropping them on the chat or the overlay.

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
  there runs as the agent itself, which takes **editor+**, so the agent holds **editor**
  and **manager** assignments only: Admin → Users offers nothing lower for it (a newly
  ticked one starts at editor), and so do the agent's Default-for-new-users setting and
  the map's "Add me". Switching an agent to Shared only lists the people who hold viewer
  or contributor there; a manager confirms exactly that list, their assignments are
  removed, and the switch saves. An agent's own mode tools make the switch only when
  nobody would lose an assignment; otherwise a manager confirms it in Agent Settings →
  Configuration. A viewer or contributor assignment kept from an
  older install stays as it was but opens no chat (a chat there shows a "This chat
  takes the editor role" card, and a personal task they made earlier fails each run
  with a message too): the agent's Users card marks it "no chat access",
  and an admin raises it to editor or removes it. For people who should only read or
  chat privately, pick a mode with personal chats instead.

Switching modes never deletes files. Chats on shared agents are visible per mode; on a
colleague's live interactive terminal you get read-only + a **Take over** button.

## Engines, models, effort (Agent Settings → Configuration, manager)

- Enable one or more **AI Engines** per agent (an engine needs a platform subscription
  to be enabled), pick a **Default Model** (or Auto), a **Default Session Mode**
  (headless vs interactive terminal), and a **Default Effort** (thinking depth).
- **Auto** (no pinned model) runs the engine's own default, chosen by tier: Claude Opus
  5.5 on Claude Code, GPT-6.1 Sol on Codex, Sonnet 5.5 on Direct LLM. A pin on a retired
  model runs on its successor.
- A message typed while the agent works reaches a Claude Code or Codex turn at its next
  tool step (in the sandbox, on a paired machine, and in a delegated worker still
  working). When the turn cannot take it (a Direct LLM chat, a turn that is ending) it
  waits as a bubble with a dashed edge labelled "queued" at the end of the conversation: the waiting
  messages belong to the chat, so every tab and a teammate on a shared chat see them,
  they survive a chat switch, a closed tab and a restart, and they go out together as
  the next turn, in order. The x on a waiting message takes it back (its author, or an
  admin); Edit returns it to the composer with its photos and files. A failed or
  stopped turn hands the waiting messages back to their authors.
- A turn that ends short shows a card that says why: the model declined it, the usage
  limit was reached, the engine reported an error or its process exited, it stayed
  silent past the ceiling (10 minutes with no tool running, `TURN_SILENCE_S`), or its
  machine was lost. When the turn had started its work (an error, an exit, a silence, a
  lost machine, a server restart), the card offers **Resume**: it asks the agent to carry
  on from where it stopped, and after an exit, a silence or a lost machine it carries the
  turn's photos and files again. When nothing of the turn ran (an error, an exit, a
  silence or a lost machine), or the message met a stopped engine, the card offers **Send
  again**, which sends that turn's messages again as a new turn. A decline, a usage limit
  and a restart that cut nothing offer no button. The agent then sees the answer so far
  but not the tools it called, so after either button a tool the turn already ran can run
  a second time.
- A chat's permission mode sits in its status bar:
  - **Ask**: the agent reads freely and asks before anything else (editing files,
    running most commands, using most tools).
  - **Auto** (a new chat's mode; unrelated to the model picker's Auto): the ordinary work
    runs (reading, editing files, the everyday tools) and the engine's own review judges
    the rest. On Claude Code that is every other action. On Codex the review sees only
    what reaches beyond the agent's workspace (a delete outside it, the network, a tool
    that needs approval): a command inside the workspace runs without a check, even one
    that deletes files there, just as in Codex's own Auto. When the review says no,
    nothing pops up: the agent tells you what it wanted to do, and your reply lets it
    try again. The commands the platform always blocks stay blocked.
  - **Don't Ask**: everything runs without asking.
  - **Plan**: the agent only reads and writes a plan; you then start it in Auto or Ask,
    ask for changes, or cancel. This works the same on Claude Code and Codex.
  Tools follow their own labels. A tool marked as only reading runs without asking, in
  Ask too. Every other tool asks in Ask. In Auto, one that says it changes nothing it
  cannot undo and stays inside its own service runs, and the engine's review judges the
  rest. Don't Ask runs them all. A tool with no label counts as one that changes
  something. Community tools follow the chat's mode like the rest. A device tool on a
  computer whose owner granted it (computer, browser or app control, Blender's code tool
  among them) runs without asking in Ask and Auto too.
  Direct LLM offers Ask and Don't Ask only (a chat moved onto it from Auto opens in
  Ask). A few tools always show OtoDock's card in a chat, in Ask, Auto and Don't Ask:
  sharing knowledge, attaching or detaching a shared library, changing a department,
  and creating or deleting a check. In Plan and in a task they are refused. In a Codex
  terminal in Auto they are refused too, so use them from the chat. A Codex terminal
  in Don't Ask runs them without the card.
  The platform's command floor holds on every engine and mode.
- In an interactive terminal the chat's mode is the terminal's: it starts in the mode
  the chat shows, switching modes inside a Claude Code terminal (Shift+Tab) changes the
  chat's mode too, and a badge in the terminal's corner shows it (Ask, Auto or Don't
  Ask).
- Claude Code and Codex read the instruction file at the top of each repository in the
  person's own workspace (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex), in that
  person's chats only: never in tasks, triggers or a Shared only agent's chats.
- Users switch models per chat from the picker; when a chat's session has ended, other
  enabled engines appear too — picking one restarts the chat on that engine from saved
  history (works on finished task chats as well, with editor access).
- Both pickers order models by capability tier and mark each with four dots (●●●●
  frontier … ●○○○ fast; none = untiered); hover for the tier word and what the model
  is good at.
- Scheduled tasks can pin their own model/engine — see automation.md.

## Tools (MCPs)

Two switches gate every tool: **authorized** (admin — installed/configured platform level,
sometimes via per-agent instances) and **enabled** (manager — Agent Settings → MCPs). Core
tools (memory, tasks, delegation, triggers, notifications, meetings, display and apps,
checks (for agents created since 1.7), files/documents, self-config, catalog browser) are
on by default; optional and community tools are enabled per agent. Agents can list what's
available and either enable directly (when the manager asks and it's allowed) or file an
admin request. Integrations (GitHub, Notion, and more as the catalog grows) connect per
user under **User Settings → Integrations**; shared agents get a **service account** — a
manager binds one of their own connected accounts on Agent Settings → MCPs (for a GitHub
account connected through a GitHub App, one of its installations the manager owns: the
agent's `git` and `gh` then reach only the repositories that installation covers); beneath
the binding the manager subscribes the agent to that vendor's events (needed before an
agent trigger or a shared app can react to them). A hosted MCP server that signs people in
itself (Notion, Linear) needs no OAuth app from an admin: the install registers itself
with the vendor on first use, each person signs in on the vendor's page from Connected
Accounts, and the token is added to the server's calls in transit, never handed to a
session or a machine. An admin sees the registration on **Admin → MCP Servers** and can
**Forget** it. A connected GitHub account reaches `git` and `gh` through a credential
helper that fetches the token per command, so no session file or variable holds it.
**GitHub reach**: beneath the GitHub service account on Agent Settings → MCPs a manager
picks the repositories the agent's own GitHub account reaches and **Read only**
*(manager)*. That reach holds for the agent's own work: its agent-scope tasks, triggers,
delegated runs and calls, and a Shared only agent's chats. A person's own chats use their
own GitHub account: on their account card under User Settings → Integrations each person
picks the repositories and **Read only** for an agent, and an account they never narrowed
reaches everything it can. OtoDock refuses everything past the reach, in the GitHub tool
and in `git` and `gh`. A token or GitHub sign-in account has no list, only Read only.

**SSH hosts**: an admin adds each host (name, address, port, user, key) as an instance
of the **SSH** MCP under Admin → MCP Servers and gives it to agents. The agent's
managers and editors then reach it from the shell, on the server too, as the user the
admin set. Viewers and contributors never get it.

## Skills

Instruction packages in the standard SKILL.md format. Sources: the community skills
catalog (**Browse Community Skills** — install is admin, enable is manager), bundled
with MCPs (ride the tool's enablement), or an admin-uploaded ZIP (community package
format or a bare SKILL.md folder). Two loading modes: **always in context** (inlined in
the system prompt) vs **on demand** (loaded when the task matches the description —
the default). Per-agent enablement lives on **Agent Settings → Skills** *(manager)*.
Packages bundling executable scripts carry a "bundles scripts" badge. A skill may be
meant for one tier (manager, editor or contributor and up); a person below it does not
get it in their sessions. `mcp-authoring`, for packaging an MCP, is for editors and up.

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
   installed agent's template, the card and the agent's Configuration section offer **Update**
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

**Runs on.** Every chat shows where it runs in the **Runs on** pill, a computer icon
beside the model; its tooltip names the place. Its menu lists the places the person may
pick: the OtoDock platform, their own paired computers and, for an admin, the admin
machines. An editor never runs a chat on an admin machine: an editor's message in a
shared chat there first moves it to the OtoDock platform or the editor's own computer.
A viewer or contributor on a Shared only agent sees the place with no menu, and a Direct
LLM chat always runs on the platform. A pick asks for confirmation, then
moves the chat, and the icon shows the copy's progress. The chat keeps its memory when
both places are online. When the current place is offline it continues there from a
summary of the conversation. Working files stay where they are. A move is refused while
a turn runs, so finish or stop the turn first. On a new chat the same menu picks where
it starts, before the first message. A person can also ask the agent to move the chat.
Its `move_chat` tool lists the places, the move happens when the agent's turn ends, and
the agent then continues on the new place by itself.

A task or a delegated job can name its own machine too (see Automation). When that
machine is offline at run time, the run uses the agent's usual place and says so.

**Shared only agents.** A member with the editor role or above can move a shared chat
to their own computer. While it runs there, everyone else sees it read only with "This chat is
running on <name>'s computer." Once that session closes, the next person's message
first moves the chat to that person's usual place. The agent's own accounts stay on the
server, so tools that sign in with them are not available on a personal computer.

In the server sandbox, packages installed into HOME vanish when the session ends. An
agent keeps Python packages across sessions in a virtualenv inside its workspace
(`uv venv .venv`, then `uv pip install --python .venv/bin/python <pkg>`): it persists,
never syncs to remote machines, and counts toward the agent's storage quota —
`node_modules` in the workspace behaves the same way.
