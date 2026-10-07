# Automation — tasks, triggers, notifications, webhooks

## Tasks

Work an agent does on its own. Three shapes: **recurring** (cron or plain interval,
fired in the creator's timezone), **one-time** (`run_at` or a delay; the row cleans
itself up after firing), and **trigger-fired** (no schedule — runs when a wired trigger
fires). Chats can also schedule a **continuation** — a bounded future wake of the same
conversation.

- **Scopes**: *user* (private to the creator — any user) vs *agent* (team-visible,
  results in the shared workspace — needs per-agent **editor+**). Agent-scope runs
  created by a **manager/admin** get knowledge write access; editor-created stay
  read-only. A **Shared only** agent offers the agent scope alone, so its tasks need
  editor+; a viewer's or contributor's personal task from before the switch fails each
  run with a message saying so.
- **Completion notification** (required per task): *auto* (generic "Task Complete/
  Failed"), *manual* (the agent writes its own, with real results — the common choice),
  *none* (silent). Failures always notify except in *none*.
- **Checks**: a task can name checks that judge each run; a run whose check still fails
  is reported failed (see Checks below).
- **Model pinning**: a task can pin a specific model and engine for its runs, leaving
  the agent's default untouched — the lever for "lighter default, one demanding daily job
  on a strong model". Ask the user before pinning; read the current pin back from
  `list_tasks` (`runs on: <model> [pinned|agent default|layer default, tier N]`) or
  `get_task`, which also shows the prompt and warns when a pin no longer resolves;
  pick by capability tier (`[t1]` frontier … `[t4]` fast), never by the id's name.
  Both Scheduled Tasks pages show the effective model per task as a chip carrying the
  tier dots (the word in the tooltip); both Triggers pages show the linked task's model
  beside the task name; an app-handler task row carries no model (no LLM turn).
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
- **Transferred rows**: when a creator loses the editor role on the agent (or is
  removed from it, or deleted), their agent-scope tasks, triggers and notifications keep
  running under the admin who made the change and show "transferred from <person>" on
  their rows (hover for the date). A transferred task runs without writing knowledge
  until a manager of the agent (or an admin) adopts it by editing its prompt.
- **Managing**: Agent Settings → Monitoring → Scheduled Tasks (run/pause/resume/delete,
  role-gated per row); run history in the sidebar's Task history view (rename/delete,
  role-gated); a finished run's chat can be continued — and, once over, switched to
  another engine/model from its picker (editor+, platform-pool credentials).

## Triggers

Rules that react to **events**: when X arrives → run a task, send a notification, wake
an app, or a mix. Same user/agent scoping as tasks: an agent-scope trigger takes
**editor+** (an editor changes their own, a manager any; contributors and viewers make
personal ones only). The dashboard's **+ New trigger** form offers the agent scope only
to managers (on a Shared only agent it is the only scope); elsewhere an editor makes one
through the agent in chat.

- **Generic webhooks**: every trigger gets a URL under `/v1/webhooks/…`; external
  systems POST JSON with `Authorization: Bearer otok_<key>`. Keys are minted under
  **User Settings → Integrations → API Keys** (user scope) or **Agent Settings →
  Monitoring → Triggers → Agent API Keys** (agent scope, manager) — shown once. Webhook
  payload fields substitute into task prompts and notification text as
  `{{placeholders}}`.
- **Vendor events**: connected integrations with an event API (GitHub, Linear, Slack,
  Microsoft 365, Zoom, Notion) register subscriptions automatically — subscribe from
  the account card under **User Settings → Integrations**, then create a trigger
  against the subscription with an event filter (`event_type` must be one of the
  subscription's event names). GitHub offers **One repository** or **Every repository
  in an organization** (one hook for every present and future repository; the connected
  account needs the Organization webhooks permission and owner rights on the
  organization). A subscription has a scope: **Subscribe as: Me** makes a personal one
  (user-scope triggers only); **Subscribe as: <agent>**, or Agent Settings → MCPs →
  **Subscribe to events for this agent** *(manager)*, subscribes the agent on its own
  behalf through its service account, the only kind an agent-scope trigger (one that
  wakes a shared app, say) can link to. A
  user trigger links only to a personal subscription, an agent trigger only to that
  agent's; the trigger form's picker lists just the matching ones. Deleting a
  subscription that triggers still fire from asks twice; the triggers stay but stop
  receiving events. Delivery is asynchronous — wait ~10–15 s before judging a test.
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
  agent. Everyone with access sees that tab (the checks and the verdicts they may see)
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
its template (until then the Checks tab says the tool is off).

## Notifications

Delivered where the user is: dashboard toast + bell inbox when active, browser push when
away; every notification lands in the inbox regardless. Severities: info/success
(chime), warning (amber, distinct sound), **danger** (stays until dismissed, alarm —
reserve for genuine emergencies). Titles cap at 100 chars, bodies ~2 short sentences —
write headlines, details stay in the linked chat/run. Notifications can be scheduled
(one-time or recurring) without an LLM run — prefer that over a task when the reminder
only needs to *say* something, not *do* something. Management: bell panel; Agent
Settings → Monitoring → Notifications; admin audit page.

## Usage awareness

Costs are estimated at API pricing and attributed to the model that actually ran.
Budgets (admin, **Admin → Usage**) cap only **platform-paid** usage (borrowed API keys,
direct engine) — never a user's own subscription. At 80% the user gets a warning; at
100% new work is blocked until an admin raises the limit; blocked task runs record
`limit_exceeded` silently. One quirk worth explaining if asked: when background work
finishes while a chat is idle, the agent wakes to review it — an unprompted, metered
turn behind a "Background work finished" marker. That's real work, not a phantom
charge.
