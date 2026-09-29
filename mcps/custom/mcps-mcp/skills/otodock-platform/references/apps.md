# Apps and sharing: what an agent builds, who approves it, how it goes out

## What an app is

An app is a page an agent builds and keeps running. It has its own address people
bookmark (`/apps/<id>`, the app menu's **Open full screen**), and it can carry buttons
that put the agent to work, live data from the platform and, when it needs one, its own
server and database that everyone who opens it works on at the same time. It outlives
every chat: the agent keeps it fresh (a scheduled task re-pins it, a server updates its
own data).

Two kinds, one model:

- **Apps for the team** live on the agent. A project board the team moves cards on
  while the agent files the work, a company board a CEO agent keeps current, a brief a
  task rebuilds every morning at seven. A button fires the agent's task and the result
  lands back on the board.
- **Apps for customers** go out by a link to people without an account. A booking
  page: clients open the link, pick a slot and pay. The app keeps its own customer
  accounts in its database and takes the payment through its own keys, which the agent
  never sees.

Who sees one: a **personal** app is its owner's alone, a **shared** app is one copy for
everyone on the agent. When the agent has apps, opening the agent opens the first one:
that app is **the agent's home**, shown without tabs when it is the only one. The **apps
button** beside the chat composer opens and closes them; several sit in tabs (shared
first; an amber dot means an approval is waiting). An app pinned to one chat or one
delegation project sits on that chat's **Dock** instead and is reached only through it.

## What an app can do

- **Buttons** that fire a task, send a prompt into the open chat, or call one tool
  directly. They run on the approver's authority, so a viewer can press them, and a
  button can carry a role floor (contributor, editor or manager). A personal app's
  buttons use the owner's connected accounts whoever presses them; the app's card says
  whose.
- **Live platform data**: chats, tasks, notifications, file changes, checks and their
  verdicts as they happen, each viewer their own slice.
- **A server and a database** (a folder app: `apps/<slug>/` with `app.json`, `client/`
  and an optional `server/`), which the agent's own sessions reach too.
- **Wakes with nobody watching**: on a schedule, when a webhook trigger fires, when a
  task finishes, a turn ends, a file changes or a check lands, or when another app emits
  an event. A vendor's signed events (Stripe, GitHub) reach it through its own route.
- **Approved scripts** that run where the agent runs, with no model.
- **Other agents' apps**: a shared app calls the shared apps of the agents its agent
  delegates to, a personal app those of agents its owner belongs to, both sides
  approved.
- **Outside services** with secrets a person sets. A secret sent to an outside host is
  added by the platform on the way out and never enters the app; one the server reads
  itself is marked in amber on the card.

## The approval card

Nothing an app declares works until a person approves it. The card lists in plain lines
everything the app may do (buttons, platform data, files, hosts, wakes, what it offers
and uses from other apps, the secrets it needs), with **Details** and the exact manifest
beneath. Any change to what the app may do voids the approval and the new release waits
on the card, as does a release whose required secret has no value yet. The approver
must also be allowed to run every task the app's buttons fire. The card is never
approved automatically and your session cannot approve it.

## Releases and rollback

Viewers see a release, a copy cut when the app is pinned or deployed, never the file
being edited; the last three are kept. A folder app's new release goes live only once
its server answers, so a server that fails to start leaves the old one serving. Before a
deploy the platform checks the app: what the sandbox would refuse, and how it renders on
a phone, a tablet and a desktop in both themes. **Roll back** (app menu, or your
`rollback_app`) returns to the release before the current one, and a folder app's
database goes back with it.

## Who does what

| Action | Personal app | Shared app |
| --- | --- | --- |
| Pin (the person's chat pins it) | anyone with access | **editor+**; a contributor's or viewer's pin stays personal |
| Approve the card | its owner | **editor+** (a manager when its scripts receive the agent's accounts; the card says so) |
| Roll back, Logs, View the working copy, Unpin, Delete app and its data | its owner | **editor+** (**Unpin for everyone**) |
| Set a secret (app menu → **Settings**) | its owner | **editor+** |
| Share | its owner | **editor+** |
| **Hide for me** | a person it was shared with | anyone |

Logs, View the working copy, Settings and Delete exist only on an app with a server,
Roll back once there is an earlier release. Admins hold the same authority on every app
(another person's personal app is not in their list; they reach it at its address).

Approving, sharing and setting a secret are always a person's action on the dashboard.
You, in a session, build, check, preview and deploy an app, pin and re-pin it, push live
updates, open it on the user's screen, read its logs, roll it back, and export or import
it, all with your session's role (a scheduled agent-scope run pins shared apps). Load the `app-authoring` skill before building one: it carries the
tools and the contracts.

Per person, **User Settings → General → Apps** holds "Agents know which app I am looking
at" and "Agents may open apps on my screen" (both on by default).

## Templates and `.otoapp` bundles

- **Community agent templates ship apps**: shared copies for the agent and personal
  copies, one per member (one of which can be the agent's setup page), up to four in all,
  approved once in the install dialog (an admin's consent covers every member's copy; a
  copy that acts as the member needs that member's own approval). A template update
  replaces an untouched copy and keeps an edited one, with the new version beside it.
- **`.otoapp`**: `export_app(slug)` packs an app with a server as `apps/<slug>.otoapp`,
  with the tasks its buttons fire; `import_app(path)` installs a bundle on the agent
  whose session runs it (to move an app, copy the bundle into the other agent's
  workspace and import it there). An import always lands waiting for approval, with no
  secret set and no share.

## Sharing apps and chats

Default visibility never changes; a share adds access on top.

- **Where**: an app's menu → **Share**; a chat's row in the chat history → **Share**.
  A teammate gets a notification and finds it under **Shared with me** on the Agents
  page.
- **Who**: a personal app or your own chat, its owner; a shared app, or a chat on a
  Shared only agent, **editor+**; an admin always. An app on a chat's Dock and a task run
  cannot be shared.
- **A teammate** is a viewer of what was shared: buttons run on the approver's
  authority, and they see none of the agent's files or chats unless they are on the
  agent.
- **A link** is for someone without an account. It has a password by default (generated,
  shown once, editable) and expires by default (30 days, editable), and it can be
  revoked any time. A link's buttons stay off until its **Buttons** switch is turned on;
  a link never sends prompts, reads the agent's files or opens platform pages. Making a
  link and turning its buttons on ask the person to confirm who they are; a link without
  a password takes a second confirmation.
- **A shared chat** is a read-only copy of the conversation as it stood when shared
  (text, artifacts, images, media, files; tool calls only when asked, with tokens
  redacted). Share again to send a newer copy.
- **Admins**: **Admin → Shares** lists every link and revokes any. **Setup → Security →
  Sharing** holds **External links**, **Links without a password**, **User directory
  when sharing** and **Longest link expiry**; turning a link switch off also stops the
  existing links of that kind.

You have no share tool. When a user asks you to share, tell them where **Share** is and
the role it takes. A link serves the app's state document whole, so keep what a stranger
must not see out of it.
