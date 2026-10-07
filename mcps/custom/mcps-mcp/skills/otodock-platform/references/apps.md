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
  verdicts as they happen, each viewer their own slice; a single-file app also keeps a
  small document per viewer, which only that viewer's own page reads and writes.
- **A server and a database** (a folder app: `apps/<slug>/` with `app.json`, `client/`
  and an optional `server/`), which the agent's own sessions reach too, and whose
  exported methods the sessions of agents it is shared with reach at the share's role.
- **Wakes with nobody watching**: on a schedule, when a webhook trigger fires, when a
  task finishes, a turn ends, a file changes or a check lands, or when another app emits
  an event. A vendor's signed events (Stripe, GitHub) reach it through its own route.
- **Approved scripts** that run where the agent runs, with no model.
- **Other agents' apps**: a shared app calls the shared apps of the agents its agent
  delegates to, a personal app those of agents its owner belongs to, both sides
  approved. An agent's chats and tasks also call the exported methods of an app a
  share placed in it, at the role the share gives, and nothing else of that app (an
  app a person placed with their own share: only that person's own chats and tasks
  there, never a Shared only agent's chats).
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
| Roll back, Logs, View the working copy, Unpin (**Stop app** on an app with a server), Delete app and its data | its owner | **editor+** (**Unpin for everyone** / **Stop app for everyone**) |
| Set a secret (app menu → **Settings**) | its owner | **editor+** |
| Share | its owner (with people only) | **editor+** (with a person; with an agent they are also editor+ on; a department: admin) |
| **Hide for me** | a person it was shared with | anyone, and any member of an agent it was placed in |
| **Remove for me** (in place of Hide for me) | a person who accepted it into one of their agents (not where an agent or department share also places it: that row is the team's) | the same; it removes their share, and anyone who may share the app can share it again |
| Where it comes from | — | anyone, on a placed app |
| Remove from this agent | — | **editor+** on the receiving agent (an agent share); admin (a department share, which ends it for the whole department) |

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
  A person gets a notification and finds it under **Shared with you** at the top of the
  notifications panel (the bell): an app waits there until they accept it into one of
  their agents (it opens from its own page meanwhile) or decline it (once accepted, the
  app's menu there offers **Remove for me**, which removes it, unless a share to that
  agent or its department also places it there); a chat opens from there. A team app
  can also be shared with a whole **agent** (by an editor or manager of both agents: it
  appears in that agent's Apps panel for every member, as a purple chip with a teal
  border and a small mark (one a person accepted into their own agent is blue with the
  same border) and a reduced menu, and the agent's chats and tasks may call what the app exports) or,
  by an admin, with a **department** (every agent in it, following the department as it
  changes). The sharer picks the role the recipients act as, up to
  their own.
- **Who**: a personal app or your own chat, its owner; a shared app, or a chat on a
  Shared only agent, **editor+**; an admin always. An app on a chat's Dock and a task run
  cannot be shared.
- **A teammate** acts at the role the share gave them (viewer unless the sharer picked
  higher; a placement's role is also capped by their own role on the agent it sits
  in): buttons run on the approver's authority, and they see none of the agent's
  files or chats unless they are on the agent. A personal app and a chat go to
  people only.
- **A link** is for someone without an account. It has a password by default (generated,
  shown once, editable) and expires by default (30 days, editable), and it can be
  revoked any time. A link's buttons stay off until its **Buttons** switch is turned on;
  a link never sends prompts, reads the agent's files or opens platform pages. Making a
  link and turning its buttons on ask the person to confirm who they are; a link without
  a password takes a second confirmation.
- **A shared chat** is a read-only copy of the conversation as it stood when shared
  (text, artifacts, images, media, files; tool calls only when asked, with tokens
  redacted). Share again to send a newer copy.
- **Admins**: **Admin → Shares** lists every share (to people, agents and departments,
  and every link) with its standing, filtered by kind, agent and standing, and revokes
  any; nobody is notified. **Setup → Security → Sharing** holds **Sharing to agents**, **Sharing to departments** (off refuses new
  shares of that kind; the existing ones stand until removed), **External links**,
  **Links without a password**, **User directory when sharing** and **Longest share
  expiry**; turning a link switch off also stops the existing links of that kind.

You have no share tool. When a user asks you to share, tell them where **Share** is and
the role it takes. An app shared with your agent shows in `list_apps` under "Placed
here" with its id; `describe_app(agent, slug)` names its exported methods and the role
you act at. An app can list who uses it (`app.audience`): its own server, and on a page its own
agent's editors and managers, an admin, or a personal app's owner. A link serves the app's state document whole, so keep what a stranger
must not see out of it.
