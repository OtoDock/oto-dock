---
name: agent-creation
description: How to author an agent template folder and install it as a new agent - folder layout, manifest fields, persona rules, shipped apps and checks, and the scaffold-validate-create flow. Use when asked to create, build, or set up a new agent.
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
   platform cannot read it. The platform copies the folder before reading
   it: at most 300 files, 1 MB a file, 20 MB in all; dotfiles,
   `node_modules` and an app's `data/` are left out, a symlink is refused.
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
├── dashboards.json     # optional — app dashboards to seed + pin
├── dashboards/         # optional — the dashboards' *.html files
├── apps/               # optional — shared folder apps, one folder each
├── user-apps/          # optional — per-member folder apps, one folder each
├── checks/             # optional — agent checks, one folder each
└── context/            # optional — *.md / *.txt, always loaded
```

### `dashboards.json` — template-shipped dashboards

```json
{"dashboards": [{
  "slug": "team-board", "title": "Team Board", "file": "board.html",
  "visibility": "agent", "auto_pin_for_new_users": true
}]}
```

Ships a ready-made app dashboard with the agent (≤4 per template, ≤1MB
each, bare `*.html` names under `dashboards/`; `{agent_slug}` is
substituted). `visibility: "agent"` = ONE shared dashboard pinned for every
user of the installed agent; `"user"` = each user gets their own personal
copy — the installer now, later joiners automatically when
`auto_pin_for_new_users` (default true). The visibility must be one the
template's mode offers (an `"agent"` dashboard on a Personal-only template
is a validation error). HTML-only: seeded dashboards carry no action
buttons (nothing to approve); the installed agent can re-pin with actions
later. Author them mobile-responsive with Tailwind, exactly like `pin_app`
apps (see the display-mcp app-authoring skill).

### `apps/` and `user-apps/` — folder apps the agent ships with

```
apps/board/                # ONE shared app: every member opens the same copy
├── app.json               # required — the manifest deploy_app takes (title, actions, egress, files…); a folder without one is ignored
├── blueprint.json         # optional — {"format": 1, "tasks": [...], "triggers": [...], "auto_create_for_new_users": true, "roles": null}
├── client/index.html      # required — the page every viewer opens
└── server/index.ts        # optional — a Bun server with its own SQLite
user-apps/home/            # ONE app PER MEMBER: each gets their own copy and data
└── …same layout…
```

A folder under `apps/` is deployed once for the agent; a folder under
`user-apps/` is deployed for the installer and, when `blueprint.json` says
`auto_create_for_new_users` (default true; `roles` narrows it), for every
member attached later — exactly the folder apps of the display-mcp
app-authoring skill, so write them the same way (`app.json` keys, the
`/_health` route, `window.otodock.*` in the page). Each member's copy has
its own database; the template's copy stays under the new agent's
`config/community/user-apps/<slug>/` for later members, and a seeded
server is not started at install — the first open starts it.
`blueprint.json` also lists the tasks the app's `fire_task` buttons fire
(`tasks`, by slug — a button says `"task": "<slug>"`), the way
`export_app` writes them; they are seeded for the scope with the app. Its
`triggers` (`[{slug, handler, description}]`, at most eight, each aimed at
one of the manifest's `handlers.on_trigger` names) are seeded the same
way: one trigger per copy, owned by that member (a personal app) or by
the agent (a shared one), aimed at the copy — so a personal app can wake
on a webhook of its member's own; an app with handlers is each member's
to approve, and its trigger waits with the copy until they do. The
visibility must be one the template's mode offers: a Personal-only
template ships `user-apps/` only, a Shared-only one `apps/` only. Rules
the platform enforces: at most 4 apps per template across both folders,
one slug namespace (1–40 chars of `[a-z0-9-]`, starting alphanumeric);
dotfiles, `.env`, `data/` and `node_modules` inside an app folder never
ship; a per-user app never takes an inbound hook and never declares `files` under `knowledge/` (it may wake on a trigger — the blueprint's `triggers` above, one seeded per copy). Leave
`deploy_requires_approval` unset: `true` parks every seeded copy on its
card even when a person consented. Validation runs the deploy's own
checks over each app before the agent exists (the manifest validator and
the static checks — a failure is reported with its file and line,
warnings are not); an `mcp_tool` action may name an MCP that `mcps.json`
or the app's own `requires.mcps` lists.

**Consent is a person's, never yours.** A catalog install carries the
installer's checkbox; `create_agent` carries none. So apps you ship land
deployed but **waiting for approval** on their cards (a manager approves a
shared app, each member approves their own copy — the installer's
included) and become live the moment someone approves; until then the
app's page says its first release waits on the card. The one exception is
an app whose `app.json` declares nothing to approve: it goes live at
once. Say so in your report, and never claim an app is live because you
created it. A copy its owner removed (their delete, an `unpin_app`) is not
seeded again; `pin_app(slug)` in that member's session brings it back
from the template's copy.

**An app can be the agent's setup page.** A per-user app that reads the
platform method `setup.status` and calls `setup.complete` shows each new
member a welcome (what the agent does, a Setup button) and then its normal
dashboard, in place of walking them through `user-setup.md` in chat; the
answer names the agent and grades every MCP the app's manifest names — its
`requires.mcps` and the `mcp` of any tool button — (enabled for the agent, connected for the member, what
would fix it), so a card greys out where a tool is missing. The
app-authoring skill's "setup page" bullet has the contract. Keep
`user-setup.md` beside it: it is the chat path, and a platform before 1.7
installs the template without the app. Keep that app's manifest free of
anything that acts as the member (`mcp_tool` or `fire_task` buttons,
`files.write`, handlers, steps, inbound hooks, secrets), so a person's
consent on a catalog install can approve every member's copy and the
welcome opens without a card first. Tell the new agent in `context/` how
the app keeps its data and how to change it from a chat: `list_apps` →
the personal app's id → the app's own `/v1/apps/<id>/api/…` routes with
the session's proxy key (the current user's copy only), never a second
copy of that data in memory.

### `checks/` — checks the agent runs on its own work

```
checks/coding/
├── check.json             # the document set_check takes (name = the folder's)
└── lint.sh                # the script it names, if any
```

Each folder is one check in the shape of the checks-mcp examples
(`list_building_blocks` lists them); at most 8 per template. The
document's `name` must be the folder's; `script.run` names a file in the
folder (UTF-8, at most 256 KB); a `handler.app` may name one of the
template's own apps. A check's `mandatory: true` is a request: without a
person's consent the platform installs it **offered**, and a manager
makes it mandatory from the dashboard — `create_agent` carries no
consent, so every check you ship lands offered. Ship a check only when
the agent's work has a rule a script or a rubric can hold it to; a check
runs on every turn it applies to.

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
`interval_seconds`) or `run_at`. Write a cron for the company clock: an
agent-scope task fires in the platform timezone (and follows it if an admin
changes it); a user-scope task fires in that user's own zone. Never encode
a zone offset into the hour. Default new tasks to `"paused"` unless the
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
on an admin, which apps wait for approval on their cards and which checks
are offered, and what they should do next (finish setup, invite users,
attach it to people). If the new agent needs users attached or a role
granted, that is a platform-admin action in the dashboard — say so rather
than implying you did it.
