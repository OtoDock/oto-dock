# Automation — tasks, triggers, notifications, webhooks

## Tasks

Work an agent does on its own. Three shapes: **recurring** (cron or plain interval,
fired in the creator's timezone), **one-time** (`run_at` or a delay; the row cleans
itself up after firing), and **trigger-fired** (no schedule — runs when a wired trigger
fires). Chats can also schedule a **continuation** — a bounded future wake of the same
conversation, shown in the chat as a "Scheduled wake" card. A wake arrives at its time,
also when nobody writes in that chat, and a one-time wake is never dropped. In a person's
own chat it runs in that chat's mode with its tools. In Ask, a step that needs approval
waits for the person, who is told once.

- **Scopes**: *user* (private to the creator — any user) vs *agent* (the agent's own work,
  with results in the shared workspace. It needs per-agent **editor+**, and only editors
  and managers see it and its runs. A contributor or a viewer sees only their own tasks,
  and the page says so). Agent-scope runs created by a
  **manager/admin** get knowledge write access; editor-created stay read-only. Only a
  manager of the agent, or an admin, continues, steers, stops or answers such a run, or
  changes its mode, model or engine. An editor reads "Only a manager of this agent can
  continue this run. It can change the agent's knowledge." The same holds for a delegated
  worker whose run writes knowledge, in the dashboard, by `continue_id` and by API key. A run with no
  person in it changes the prompt, mode or checks of such a task only when its own run
  holds that right. A task or
  trigger that runs without a person reaches the agent's shared workspace and knowledge,
  never a person's private folder, and the agent's settings only when its creator manages
  the agent (since 1.9.0). A **Shared only** agent offers the agent scope alone, so its
  tasks need editor+; a viewer's or contributor's personal task from before the switch
  fails each run with a message saying so.
- **Completion notification** (required per task): *auto* (generic "Task Complete/
  Failed"), *manual* (the agent writes its own, with real results — the common choice),
  *none* (silent). Failures notify except in *none*, with one exception: a run the platform
  cut that cannot go on reads failed in its history and sends no notification (see **A restart**
  below).
- **Who is told** (a shared task): the row's **Settings** picks the lowest role that gets
  its notifications: **Everyone who uses the agent**, **Contributors, editors and
  managers**, **Editors and managers** (the default for a new task) or **Managers**.
  Admins are always told. A task made before 1.9.1 keeps telling everyone. The choice
  also covers what the task's runs send themselves, a wake the task schedules in its
  chat, and the work a run hands out. People below the editor role get the notification
  without a link to the agent's chat. Through the agent: `audience` on the schedules
  tools.
- **Checks**: a task can name checks that judge each run; a run whose check still fails
  is reported failed (see Checks below).
- **Permission mode**: a task runs in **Don't Ask** by default (nobody is there to
  answer, so every tool runs; the few tools that always ask are refused). A task can be
  set to **Auto** instead (`permission_mode` on the tasks API): reading and editing run,
  and the engine's own reviewer checks everything else (on Codex only what reaches
  beyond the workspace: a command inside it runs without a check); something that
  needs a check and cannot get one is refused, and the run says why. Auto needs Claude
  Code or Codex; a task on Direct LLM is refused Auto. An Auto task always runs
  headless, never in a terminal.
- **Machine**: a task can name the machine its runs use instead of the agent's usual
  place (`machine` on the schedules tools, or the row's Settings). A person's own task
  can run on one of their own computers, and an admin can also pick an admin machine. A
  task that runs without a person never runs on a personal computer. A delegated job
  takes a machine the same way (`machine` on `delegate`). A chat lane may also run on
  the delegating person's own computer, even one that works for the whole agent. There
  it works as that person, with their role, and it stops when they lose the editor role
  or their place on the agent. Like every chat lane, it runs on their own AI account,
  wherever it runs. When the machine is offline
  at run time, the run uses the agent's usual place and its chat says so. A chat lane
  on the person's own computer waits for it instead. The round ends with "<name> is
  offline, so this work cannot run there now. Reconnect it, then continue the work."
  `get_task` shows the machine, and `get_task_history` shows where each run ran.
- **Tools in a task**: a task run and a delegated job have the agent's tools, image,
  music and video generation included (since 1.9.1). Tools that need a person in the
  chat stay out, such as the agent's own settings. A chat lane a person delegates
  from their own chat keeps them, as their chat does, the Settings tool included.
  A meeting goes without the generation tools.
- **"What are my schedules?"**: draw the list, never a paragraph. The Settings tool's
  `show_list` (`tasks`, `triggers`, `notifications`, `machines`, `mcps`; `scope` mine,
  agent or all; `status` active or paused) puts a live list card in the chat that the
  dashboard fetches with the person's own sign-in, with the page's own row actions
  (pause, resume, run now, open the page). You never see its rows.
- **Model pinning**: a task can pin a specific model and engine for its runs, leaving
  the agent's default untouched — the lever for "lighter default, one demanding daily job
  on a strong model". Without a pin, a person's own task runs on the engine their new
  chats take there. On a Shared-only agent it runs on the agent's default, with the
  subscriptions shared with agents. A chat lane runs on the engine its person's own chat
  takes, on every agent, Shared-only ones included. A model
  pinned without an engine runs on the engine the task would take unpinned (the
  person's own engine for their own task or chat lane, else the agent's default) when that engine
  offers it, else on the first engine of the agent's own order that does. Name the engine when the same model is on two engines and you want
  the other. Ask the user before
  pinning; read the current pin back from `list_tasks`
  (`runs on: <model> [pinned|your engine|agent default|layer default|the chat's, tier N]`,
  where `the chat's` marks a wake, which runs on its chat's model) or
  `get_task`, which also shows the prompt and warns when a pin no longer resolves;
  pick by capability tier (`[t1]` frontier … `[t4]` fast), never by the id's name.
  Both Scheduled Tasks pages show the effective model per task as a chip carrying the
  tier dots (the word in the tooltip); both Triggers pages show the linked task's model
  beside the task name, and an **App server** task row carries no model (no LLM turn).
- Every run's first prompt carries the current time in the task's zone and the agent's
  other running tasks, so a run knows when it is and what else is going on.
- Tasks sharing a fire time have their session starts spaced a few seconds apart
  automatically; fire times stay exact. Runs that die on an engine/provider error are
  reported **failed** with the provider message — never a silent empty success. A turn
  the model's safety classifier declines, or one that reaches the subscription's usage
  limit, ends at once. The chat shows the reason (and the reset time for a limit), the
  run is reported failed with it (the alert reads "Task stopped: declined" or "Task
  stopped: usage limit"), and a delegating agent is told how to continue: on another
  model (`delegate(continue_id=…, model=…)`), after the reset for a limit, or, when
  Codex stopped after too many denied actions ("Task stopped: declined"), after a
  permission-mode change. A run
  that completes while a background command is still running is marked **N left
  running** in the runs table (`Left running: N` in `get_task_history`); the session
  stays warm for that work up to its ceiling, and its output is not part of the run. On
  Claude, a headless chat's or task's background command stops after 30 minutes unless the
  agent gave it a longer timeout (2 hours at most); a longer job is a task of its own.
- **A restart**: a run the platform cut is reopened under its own id. Its chat shows
  "Continued after a restart", it goes on from where it stopped, and it ends with one
  result and its usual notification. A run that had not started starts then, from its own
  prompt. A one-time task or a scheduled wake that came due while OtoDock was down fires
  once when it is back, and a delay counts from the task's creation. A recurring task's
  missed fires are not caught up. After a restore or more than a day down nothing is
  caught up, and the same holds for a run whose chat a person wrote in first: the run
  fails with "Cut short when the platform restarted" and a delegating agent is told once.
  A one-time task whose time has passed and that nothing will run reads **Will not run**
  on the Scheduled Tasks pages: run it now or delete it.
  During an update a fire waits: the run stays pending and starts when OtoDock is back.
- **Transferred rows**: when a creator loses the editor role on the agent (or is
  removed from it, or deleted), their agent-scope tasks, triggers and notifications keep
  running under the admin who made the change and show "transferred from <person>" on
  their rows (hover for the date). A transferred task runs without writing knowledge
  until a manager of the agent (or an admin) adopts it by editing its prompt.
- **Managing**: Agent Settings → Monitoring → Scheduled Tasks (run/pause/resume/delete,
  role-gated per row, and **Settings** for the task's name, machine, mode and who is told
  for a person who may edit the task); run history in the sidebar's Task history view
  (rename/delete, role-gated); a run shows its own title once it has one, with the task's
  name under it, and a new name given there changes that run only (the task's own name
  changes in its **Settings** or through the agent's `edit_task`); a finished run's chat
  can be continued — and, once over, switched to another engine/model from its picker
  (editor+, a manager for a run that can change the agent's knowledge, on the
  subscriptions admins share with agents).

## Triggers

Rules that react to **events**: when X arrives → run a task, send a notification, wake
an app, or a mix. Same user/agent scoping as tasks: an agent-scope trigger takes
**editor+** (an editor changes their own, a manager any; contributors and viewers make
personal ones only). The dashboard's **+ New trigger** form offers the agent scope only
to managers (on a Shared only agent it is the only scope); elsewhere an editor makes one
through the agent in chat. Only editors and managers see the agent-scope triggers. The
form's notify block has **Who is told**, with the same four choices (editors and
managers by default, admins always).

- **Generic webhooks**: every trigger gets a URL under `/v1/webhooks/…`; external
  systems POST JSON with `Authorization: Bearer otok_<key>`. Keys are minted under
  **User Settings → Integrations → API Keys** (user scope) or **Agent Settings →
  Monitoring → Triggers → Agent API Keys** (agent scope, manager) — shown once. Webhook
  payload fields substitute into task prompts and notification text as
  `{{placeholders}}`.
- **Personal API keys** (**User Settings → Integrations → API Keys**, **+ Create key** *(any)*,
  shown once, ends after 7, 30, 90 or 365 days): a key acts as its person on all their
  agents or the ones it names, and does what its ticked permissions allow: send a chat
  message and wait for the answer, run tasks and read their results, read and write
  files in the person's own folder and the shared workspace, send the person a
  notification, and fire their own webhook triggers. A key's turn does what that chat
  can do, in the chat's mode. A key not made to run without asking sends only into chats
  in Ask or Plan, where each change waits for the person's card in the dashboard, and it
  runs no tasks. A key runs the person's own tasks and the agent's tasks they may run
  from the dashboard: a manager any, an editor the ones they made. A key never answers a
  permission card. The `otodock` command uses the same key.
- **Vendor events**: connected integrations with an event API (GitHub, Linear, Slack,
  Microsoft 365, Zoom, Notion) register subscriptions automatically — subscribe from the
  account card under **User Settings → Integrations**, then create a trigger against the
  subscription with an event filter (`event_type` must be one of the subscription's event
  names). GitHub offers **One repository** or **Every repository in an organization** (one
  hook for every present and future repository; the connected account needs the
  Organization webhooks permission and owner rights on the organization). A GitHub account
  connected through a GitHub App subscribes one of its installations instead, picked from
  a list. A subscription has a scope: **Subscribe as: Me** makes a personal one
  (user-scope triggers only); **Subscribe as: <agent>**, or Agent Settings → MCPs → one of
  the agent's accounts → **Subscribe to events** *(manager)*, subscribes the agent on its own
  behalf through that account, the only kind an agent-scope trigger (one that wakes
  a shared app, say) can link to. A user trigger links only to a personal subscription, an
  agent trigger only to that agent's; the trigger form's picker lists just the matching
  ones. Deleting a subscription that triggers still fire from asks twice; the triggers
  stay but stop receiving events. A subscription receives nothing while an admin has
  its MCP turned off. Its row says "Its MCP is turned off", and it works again once the
  MCP is on. Deleting the MCP deletes its subscriptions. When a person disconnects the
  account, or the person is removed, the account's subscriptions go and its events stop.
  Events from Slack, Notion, Zoom or OtoDock's GitHub App can stop after a restore from
  an older backup, and the subscribe form then says so. Check the server's clock first
  (within five minutes of the real time), then ask OtoDock support to reset this
  install's events. They resume within an hour of the reset.
  Delivery is asynchronous — wait ~10–15 s before judging
  a test. A GitHub event that reaches the install twice (a repository's and its
  organization's subscription, or two accounts) wakes an action once.
- Extras: per-trigger debounce; pause/resume; test-fire from chat; the trigger rows and
  webhook URLs live on **Agent Settings → Monitoring → Triggers**.

## Checks

A check judges the agent's work at the end of a turn, in a chat, a task run or a
delegation. It looks at what the turn changed and answered, then either lets the turn
end or hands the agent its findings, which the agent fixes in another round (0 to 3 fix
rounds, 3 by default). The judge never writes; the worker fixes.

- **Four kinds**: a **schema** the answer's JSON must match, a **script** whose exit code
  and output decide (it runs where the session runs), an approved app's **handler**, and
  a **judge**: a read-only session of the same agent applying a rubric.
- **Condition**: what the turn changed, never where it ran: file kinds (code, documents,
  spreadsheets and more), events (a commit, a push, a build, a test), paths or commands,
  or **always** for a check that judges the answer alone.
- **Mandatory or offered**: a mandatory check runs on every session of the agent its
  condition matches, and nobody detaches it inside a session; an offered one runs where
  someone attached it.
- **Who**: managers and admins create, edit and remove the agent's checks, mark them
  mandatory and set a daily spending cap, on **Agent Settings → Checks** or through the
  agent. Everyone with access sees that section (the checks and the verdicts they may see)
  and, through you, attaches an offered check to the current chat, runs one by hand,
  or names checks on a task or a delegation. Attaching is your tool, not a dashboard
  button: a user who wants a check on their chat asks you. A person with personal chats
  can keep private checks of their own.
- A verdict shows as a card in the chat ("Check coding did not pass · score 0.4 · round
  2 of 3"; the card counts evaluations, one more than the fix rounds); a task run whose
  check still fails at the end is reported failed. Community templates ship checks,
  listed in the install dialog; one the installer did not consent to lands offered.

The checks MCP carries the tools (`list_checks`, `attach_check`, `detach_check`,
`run_check`, `set_check`, `delete_check`, `create_private_check`,
`delete_private_check`) and the `checks` skill; load that skill before you create,
attach or explain one. It is a core tool for agents created since 1.7; an older agent
gets it when a manager enables it on **Agent Settings → MCPs** or updates the agent from
its template (until then the Checks section says the tool is off).

## Notifications

Delivered where the user is: dashboard toast + bell inbox when active, browser push and
the Android and iOS apps when away. Every notification lands in the inbox regardless. Severities: info/success
(chime), warning (amber, distinct sound), **danger** (stays until dismissed, alarm —
reserve for genuine emergencies). Titles cap at 100 chars, bodies ~2 short sentences —
write headlines, details stay in the linked chat/run. Notifications can be scheduled
(one-time or recurring) without an LLM run — prefer that over a task when the reminder
only needs to *say* something, not *do* something. A notification for the whole agent
reaches every member when it comes from a chat. When a task sends it, it reaches the
people the task's **Who is told** names. Management: bell panel; Agent
Settings → Monitoring → Notifications; admin audit page.

## Usage awareness

Costs are estimated at API pricing and attributed to the model that actually ran. A
spoken conversation's speech shows on the Usage pages under the person who spoke.
Budgets (admin, **Admin → Usage**) cap only **platform-paid** usage (borrowed API keys,
direct engine) — never a user's own subscription. At 80% the user gets a warning; at
100% new work is blocked until an admin raises the limit; blocked task runs record
`limit_exceeded` silently. One quirk worth explaining if asked: when background work
finishes, the agent may start a turn by itself to review it: a metered turn under the
line "Background work finished: the agent reviews it". It shows in the chat as it
happens, Stop works on it, and a message sent during it joins it. That's real work, not a
phantom charge.
