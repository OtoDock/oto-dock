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
  read-only.
- **Completion notification** (required per task): *auto* (generic "Task Complete/
  Failed"), *manual* (the agent writes its own, with real results — the common choice),
  *none* (silent). Failures always notify except in *none*.
- **Model pinning**: a task can pin a specific model and engine for its runs, leaving
  the agent's default untouched — the lever for "cheap default, one demanding daily job
  on a strong model". Ask the user before pinning; read the current pin back from the
  task list ("runs on: <model> [pinned] / [agent default]") instead of assuming. Both
  Scheduled Tasks pages show the effective model per task.
- Tasks sharing a fire time have their session starts spaced a few seconds apart
  automatically; fire times stay exact. Runs that die on an engine/provider error are
  reported **failed** with the provider message — never a silent empty success.
- **Managing**: Agent Settings → Monitoring → Scheduled Tasks (run/pause/resume/delete,
  role-gated per row); run history in the sidebar's Task history view (rename/delete,
  role-gated); a finished run's chat can be continued — and, once over, switched to
  another engine/model from its picker (editor+, platform-pool credentials).

## Triggers

Rules that react to **events**: when X arrives → run a task, send a notification, or
both. Same user/agent scoping as tasks (agent-scope creation is manager+).

- **Generic webhooks**: every trigger gets a URL under `/v1/webhooks/…`; external
  systems POST JSON with `Authorization: Bearer otok_<key>`. Keys are minted under
  **User Settings → Integrations → API Keys** (user scope) or **Agent Settings →
  Triggers → Agent API Keys** (agent scope, manager) — shown once. Webhook payload
  fields substitute into task prompts and notification text as `{{placeholders}}`.
- **Vendor events**: connected integrations with an event API (GitHub, and more as
  integrations land) register subscriptions automatically — subscribe from the account
  card under **User Settings → Integrations**, then create a trigger against the
  subscription with an event filter (`event_type` must be one of the subscription's
  event names). Delivery is asynchronous — wait ~10–15 s before judging a test.
- Extras: per-trigger debounce; pause/resume; test-fire from chat; the trigger rows and
  webhook URLs live on **Agent Settings → Monitoring → Triggers**.

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
