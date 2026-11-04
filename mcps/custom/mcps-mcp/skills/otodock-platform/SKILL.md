---
name: otodock-platform
description: "How the OtoDock platform itself works and where every setting lives: installing and first-run setup, agents, AI engines, permission modes, sessions and where a chat runs, apps and sharing, checks, departments and delegation, shared knowledge libraries, tasks, triggers and notifications, remote machines, the Android and iOS apps, voice, and the role-labeled settings map. Use when helping a user set up, configure, administer, troubleshoot, or navigate OtoDock."
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
  as a "queued" bubble with a dashed edge at the end of the conversation (every tab of the chat sees
  it) and goes out with the next turn. A turn that cannot finish ends with a card that says why. Where
  the turn had started, **Resume** asks the agent to carry on from where it stopped, without sending
  its photos and files again. Where nothing ran, **Send again** sends the message again, with its
  photos and files.
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
  next message. Each chat shows where it runs, and a person can move it (references/agents.md).
- The **Android and iOS apps** open the same dashboard and add push notifications, the
  alarm for urgent ones and the phone's location for agents that ask (references/setup.md).

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
**admin/creator** — managers and agents file requests admins approve. A contributor or a
viewer sees only their own tasks, triggers and notifications on an agent. What the agent
runs on its own shows for editors and managers, and the Scheduled Tasks and Triggers
pages say so.

## Where things live (summary — every setting is in references/settings-map.md)

- **User Settings** (avatar menu): General (profile, security/2FA, appearance, wake
  word, memory) · Integrations (connected accounts, API keys) · Remote Machines · AI
  Engines (personal subscriptions) · Audio · Usage.
- **Agent Settings** (per agent): Overview · MCPs · Skills · Configuration (persona
  file, engines/model, visibility mode, department, delegation targets, shared
  knowledge, memory) · Checks · Monitoring (conversations, scheduled tasks, triggers,
  notifications, meetings).
- Every settings page shows the labels. A setting's longer explanation sits behind the
  small round **i** beside its name: hover or tap it.
- **Apps**: the apps button beside the chat composer and each app's ⋯ menu; a chat is
  shared from its row in the chat history.
- **Documents** an agent delivers open in the chat's document pane (the right half of
  the chat; a floating window on a phone and in a terminal chat), one tab per document,
  where the person can edit and save them (on a phone the tick ends editing and saves)
  and reopen earlier deliveries read-only under its Versions button. The pane saves when
  it is minimized or hidden and before a message is sent, so the agent's next turn reads
  the edit.
- **Admin** (admins only): Overview · **Setup** (tabs: General · AI Engines · OtoDock ·
  Audio · Phone · Security · System Settings) · Users · Usage · MCP Servers · Skills ·
  MCP Requests · Remote Machines · Shares · Reports · Monitoring.
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

In a chat with a person present you may also have the **Settings tool**
(`find_settings`, `get_setting`, `show_card`, `show_list`, `check_card`). It
finds a setting by words and says where it lives and what it does. For a setting
the chat can show, it draws a **card** to change it in. You never see the value,
and no tool of yours writes a setting. Every change is a person's click in the
card. In a chat several people see (a Shared only agent's chat), each of them
sees the card: a setting their role changes shows them their own value to
change, and an admin's setting shows anyone below admin only who changes it. The tool is absent in a task, a meeting, a call and a session
started with the `otodock` command. An older agent has it once a manager turns it
on under Agent Settings → MCPs. Where you do not have it, answer from the map.

**Cards with buttons** (`show_card`). When the conversation reaches a decision
the person makes with their own permissions, draw a card instead of asking in
prose: a title, one or two sentences, up to three buttons. Anyone in the chat may
press, and the card acts for whoever does. A **say** button sends its message to
you as the presser's own words (a yes, a no, a choice). On a chat that runs on a
platform machine, only a manager's press goes out, unless that machine lets every
role run there; an editor writes the answer instead. A **do** button runs one
action of the platform when someone presses it, with the presser's own role,
checked again at the click. Each action takes exactly its
own parameters: hand files to the team (`hand_files`: `paths` from their space
to the team's workspace, `dest_dir`, `copy` when they keep theirs), share a
memory topic with the team (`promote_memory`: `topic`, `new_name`), copy an
app to another agent (`share_app`: `app_slug`, `target_agent`), bring a copied
app up to its source's newer release or unpack that release for a merge
(`update_followed_app`: `app_slug`, `mode` `update` or `merge`, `visibility`
`user` for the person's own app), send a copied app's release back to the app
it follows (`send_release_to_source`: `app_slug`, `visibility`), attach a
shared knowledge library to an agent (`attach_library`: `source_agent`,
`target_agent`, `subdir` for a library that is one folder, `writable`; admins
and creators), create a department (`create_department`: `name`, `levels`,
`mode`, `reach`), set its delegation mode and reach (`set_department`:
`department_id`, `mode`, `reach`), and move an agent into a department
(`move_agent`: `agent`, `department_id`, `level_id`). The platform writes a
do button's title and text from its parameters; your own words go in the
note. A refusal names the parameter that did not fit: fix it and draw again.
A card about a person's own files, memory or personal app is theirs alone to
press, so draw it in their turn, for them.
Nothing happens by the call, the card may be ignored, one press answers it,
and `check_card` tells you who pressed it and what happened. Offer a
hand to the team when the conversation says a person's work is final (a report
done, photos to share), never on a guess. In a chat with a platform creator or
admin, the Agent Creator tool (`search_community_agents`, `propose_install`,
`propose_department_install`, `propose_move`) proposes a community agent, a
department set or a move into a department the same way.

**Live lists** (`show_list`). When the person asks what their scheduled tasks,
triggers, notifications, remote machines or MCP servers are, draw the list
instead of a paragraph: the dashboard fetches it with each viewer's own sign-in
when the card renders, so it is current, everyone in the chat sees their own
rows, and you copy nothing into text; the row
actions (pause, run now, open the page) are the page's own. Filters: `scope`
(mine, agent, all) and `status` (active, paused) for tasks, triggers and
notifications. The MCP servers list is a manager's.

## Routing — read the reference that matches

| Question is about | Read |
| --- | --- |
| Installing, first run, connecting Claude/ChatGPT/keys, voice & phone add-on, pairing remote machines | `references/setup.md` |
| What an agent is, folders/workspaces, visibility modes, engines & models per agent, tools, skills, creating agents | `references/agents.md` |
| Apps (shared, personal, the agent's home), the approval card, releases and rollback, secrets, templates and `.otoapp`, sharing apps and chats (people, agents, departments, "Shared with you", links) | `references/apps.md` |
| Departments, delegation modes and targets, the company map, meetings, shared knowledge libraries, the Notebook, memory — running a company on OtoDock | `references/company-management.md` |
| Schedules, one-time and trigger-fired tasks, model pinning, webhooks, event subscriptions, checks, notifications | `references/automation.md` |
| "Where is the setting for X?" / "who can do X?" | `references/settings-map.md` (every setting, one table per page) and `references/settings-notes.md` (the roles, the areas' shape, the apps and chats surfaces, the quick answers) |

## House rules for platform help

- **Name the place exactly** — page → tab → control, with the role in parentheses:
  "Under **Setup → Audio** *(admin)*, add a Deepgram key…".
- **Check before promising**: features can be switched off per install (remote machines,
  interactive terminals, chat audio, user-paired machines) — if a user can't see what
  you describe, the feature may be disabled or their role too low; say which.
- **Voice, phone, and live voice conversations** need the phone service add-on; plain
  dictation and read-aloud don't. On a call the agent can hang up when the conversation
  is done, hand the caller to one of the route's **transfer targets** and, on a call
  another agent placed, ask that agent.
- **A tool's pictures and files** reach the chat on their own only when its package opts
  in; when in doubt, show them yourself with the display tools.
- **Never ask users for secrets** (API keys, tokens, passwords) in chat — point them at
  the settings page that stores credentials encrypted.
- The public docs live at **https://docs.otodock.io** — link a page when the user wants
  the long-form read, and fetch a page from there yourself when you need detail beyond
  this skill (the site covers every feature; this skill carries the operating model and
  the settings map).
