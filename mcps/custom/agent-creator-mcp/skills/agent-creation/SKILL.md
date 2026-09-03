---
name: agent-creation
description: How to author an agent template folder and install it as a new agent - folder layout, manifest fields, persona rules, and the scaffold-validate-create flow. Use when asked to create, build, or set up a new agent.
---

# Creating a new agent

You create an agent by **writing a template folder** and installing it. The
template carries everything the new agent starts life with: who it is, which
tools it gets, what it knows, what it runs on a schedule, and how it onboards
its users.

This requires the `agent-creator-mcp` tools (`list_building_blocks`,
`validate_agent_template`, `create_agent`). They are only present in a chat
session driven by a platform **creator** or **admin** — if you can't see them,
you can't create agents in this session, and you should say so rather than
improvising.

## The flow

1. **Interview the user** — what should this agent do, for whom, what does it
   need access to? Don't guess a persona from one line of brief.
2. **`list_building_blocks`** — the canonical MCP names and skill packages
   available on THIS platform, plus taken slugs. Never invent an MCP name.
3. **Write the folder** somewhere you can write: `/workspace/<something>/` for
   a shared agent, or your own `/users/<you>/workspace/<something>/`. On a
   remote machine it must be inside the synced workspace, otherwise the
   platform cannot read it.
4. **`validate_agent_template`** — fix what it reports, repeat until clean.
   It changes nothing; validate as often as you like.
5. **`create_agent`** — installs it. You become the new agent's manager.
6. **Report back**: what was created, its link, what an admin still needs to
   approve, and what setup remains.

## Folder layout

```
<slug>/
├── agent.json          # required — the manifest
├── agent.md            # required — the persona
├── mcps.json           # required — which MCPs the agent needs
├── README.md           # required — what this agent is for
├── skills.json         # optional — standalone skill packages
├── tasks.json          # optional — scheduled tasks
├── triggers.json       # optional — webhook-fired tasks
├── notifications.json  # optional — scheduled notifications
├── setup.md            # optional — one-time setup guide for a manager
├── user-setup.md       # optional — per-user onboarding
├── dashboards.json     # optional — mini-app dashboards to seed + pin
├── dashboards/         # optional — the dashboards' *.html files
└── context/            # optional — *.md / *.txt, always loaded
```

### `dashboards.json` — template-shipped dashboards

```json
{"dashboards": [{
  "slug": "team-board", "title": "Team Board", "file": "board.html",
  "visibility": "agent", "auto_pin_for_new_users": true
}]}
```

Ships a ready-made mini-app dashboard with the agent (≤4 per template, ≤1MB
each, bare `*.html` names under `dashboards/`; `{agent_slug}` is
substituted). `visibility: "agent"` = ONE shared dashboard pinned for every
user of the installed agent; `"user"` = each user gets their own personal
copy — the installer now, later joiners automatically when
`auto_pin_for_new_users` (default true). The visibility must be one the
template's mode offers (an `"agent"` dashboard on a Personal-only template
is a validation error). HTML-only: seeded dashboards carry no action
buttons (nothing to approve); the installed agent can re-pin with actions
later. Author them mobile-responsive with Tailwind, exactly like `pin_app`
apps (see the display-mcp miniapp-authoring skill).

## `agent.json`

```json
{
  "schema_version": "1",
  "slug": "research-assistant",
  "display_name": "Research Assistant",
  "description": "Digs through sources and writes briefs.",
  "color": "#3B82F6",
  "version": "1.0.0"
}
```

`slug` (lowercase + hyphens, 3–40 chars), `display_name` and `version` are
required; `description` and `color` (`#RRGGBB`) are strongly recommended —
they are what a user sees on the agent card.

Optional, and worth a deliberate choice:

| Field | Meaning |
|---|---|
| `default_scope` | `"user"` (default) or `"agent"` — whose workspace and chats the agent works in. |
| `collaborative` | Default `true`. `false` + `default_scope: "user"` = **personal-only** (every user gets private chats and their own workspace). `false` + `default_scope: "agent"` = **shared-only** (one shared space for the whole team — service agents). |
| `core_mcps` | Default `"all"`: the platform's core toolkit is added automatically on top of `mcps.json`. `"none"`: the agent gets only what `mcps.json` lists — for narrow single-purpose agents. |
| `category` | `productivity` / `infrastructure` / `creative` / `research` / `customer-support` / `experimental`. |
| `tags` | Lowercase, searchable. |

Never pin an engine or model — the platform picks what is connected and the
user can change it later. `default_for_new_users` is admin-only on locally
authored templates; if you set it as a creator it is ignored and reported.

## `agent.md` — the persona

This is the agent's identity, and the single highest-leverage file. Write it
as instructions to the agent itself, in second person.

**Include**: its role and purpose, who it works for, how it should think and
decide, its standards and tone, the boundaries of what it should and should
not do on its own.

**Leave out**:
- **Capability lists.** Every MCP ships its own instructions; listing "you can
  send email, you can make charts" duplicates them, goes stale the moment
  tools change, and buys nothing.
- **Day-to-day facts** (people, projects, current state). Those belong in the
  agent's memory, which it maintains itself, or in `context/` if they are
  genuinely stable.
- **Process boilerplate** the platform already injects (permissions, paths,
  scope rules).

A good persona is usually 20–60 lines of judgment and role, not a manual.

If the user hands you raw material — a job description, docs, their own
notes — read it and distill a persona from it rather than pasting it in.

## `mcps.json`

```json
{
  "required": [
    {"name": "schedules-mcp"},
    {"name": "file-tools", "skills": ["file-tools-usage"]},
    {"name": "github-mcp"}
  ]
}
```

- `name` must be the **canonical platform name** from
  `list_building_blocks` — folder names and labels are not always the same.
- **Never list core MCPs** (memory, schedules, notifications, display,
  file-tools, meetings, triggers, agent config, and this one). They are
  assigned automatically when the agent is installed — the platform fills in
  its own core set. Listing them is harmless but noise.
- **Single-purpose agents can opt out of the core set entirely**: put
  `"core_mcps": "none"` in `agent.json` and the new agent gets ONLY what
  `mcps.json` lists — possibly nothing at all. Use it for narrow personas
  (e.g. a phone caller agent that should not schedule tasks, browse tools or
  keep memory). Normal assistants should leave it unset: the default
  (`"all"`) is right for almost every agent.
- `skills` (optional) narrows which of that MCP's skills load. Omit it to get
  the defaults — right for almost every agent.
- MCPs the platform doesn't have yet are installed for you if you are an
  admin, or queued for an admin's approval if you are a creator. The new
  agent works either way; those particular tools appear after approval.

## `skills.json` (optional)

```json
{"required": [{"name": "theme-factory"}]}
```

Standalone skill packages — knowledge without tools. A package that isn't
installed on the platform yet can only be added by an admin; validation tells
you when that blocks you.

## `tasks.json` / `triggers.json` / `notifications.json` (optional)

```json
{"tasks": [{
  "slug": "morning-brief",
  "description": "Morning brief",
  "scope": "user",
  "prompt": "Summarise what needs attention today.",
  "schedule": {"type": "cron", "cron": "0 8 * * *"},
  "default_state": "paused",
  "auto_create_for_new_users": true
}]}
```

`scope: "user"` creates one per user (each user's own); `scope: "agent"`
creates a single shared row. Schedules are `cron`, `interval` (with
`interval_seconds`) or `run_at`. Default new tasks to `"paused"` unless the
user explicitly wants them live immediately — an agent that starts firing
schedules nobody asked for is a bad first impression.

`triggers.json` is the same shape without a schedule (fired by webhook, so
default them paused — the upstream system needs the URL first).
`notifications.json` takes `title` (≤80 chars), `body` (≤500), optional
`deep_link`, and the same scope/schedule fields.

## `setup.md` and `user-setup.md` (optional)

- **`setup.md`** — one-time configuration a *manager* does (connect an
  account, paste an API key). It auto-loads into the agent's context until
  the manager confirms it's done and the agent calls `complete_setup`.
- **`user-setup.md`** — onboarding each *individual* user goes through. Every
  user who joins gets their own copy, auto-loaded only into their own chats,
  removed when that user completes it.

Write both as instructions to the new agent about how to walk a person
through it — a conversation, not a checklist dump. Keep them short: they cost
context on every turn until completed.

## `context/` (optional)

Markdown or text that should be in the agent's head on every single turn —
stable operational rules, vocabulary, business context. It auto-loads
forever, so anything volatile belongs in memory instead.

## After creating

Tell the user plainly: the agent's name and link, whether any MCP is waiting
on an admin, and what they should do next (finish setup, invite users,
attach it to people). If the new agent needs users attached or a role
granted, that is a platform-admin action in the dashboard — say so rather
than implying you did it.
