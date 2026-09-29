# Triggers — Webhook Automation

Triggers are HTTP webhooks fired by external systems (GitHub, Stripe, Linear, IoT devices, Zapier, etc.). Each trigger optionally fires a task, a notification, or both. They're the bridge between "something happened in another system" and "do something on this platform". The full call shapes, vendor-subscription workflow, payload placeholders, external-system setup and worked examples are in the skill `triggers-guide` — load it with the Skill tool before creating or editing a trigger.

## When to use which tool

| User says | Tool to use |
|---|---|
| "Notify me when my PR is merged" | `create_trigger` (scope=user, notify-only) |
| "When the deploy webhook fires, run my code-review task" | `create_trigger` (scope=user, with task_id) |
| "Wake the deploy board when GitHub pushes" (an app with a server) | `create_trigger` (with app_slug + handler) |
| "Set up an alert for when the production server goes down" | `create_trigger` (scope=agent, editor or above) |
| "Show me my triggers" / "Which model does trigger X run on?" | `list_triggers` (rows name the linked task's model) |
| "Pause / resume / delete trigger X" | `pause_trigger` / `resume_trigger` / `delete_trigger` |
| "Change the title of trigger X" / "Change which task X runs" | `edit_trigger` |
| "Test trigger X" | `fire_trigger` (sends a test payload, no real webhook needed) |

## Rules

- **Two scopes**: `scope='user'` (default) = personal automations, only the creator (and admin) can edit/delete, notifications go to the creator. `scope='agent'` (editor or above; a manager edits any, an editor their own) = business events affecting the whole team, notifications can broadcast to all agent users. Personal request (their PR, their ticket) → user; business automation that must outlive them → agent, only for an editor or above on this agent.
- **Trigger ↔ task**: a real LLM task on webhook = a task with `task_type='trigger'` (no schedule, no run_at) plus `create_trigger(task_id=...)`. Trigger and task MUST match on scope, agent and creator. `{{placeholder}}` tokens in the task prompt and notify title/body are substituted from the webhook body's top-level keys.
- **Trigger ↔ notification**: lightweight alerts that need no LLM use the inline `notify` block on the trigger — much cheaper than a task. `task_id` and `notify` can be combined.
- **Trigger ↔ app handler**: `create_trigger(app_slug=…, handler=…)` wakes an app's own server instead of running a task (no LLM turn, a missed wake is retried). The app must already declare that handler and be approved; scope follows the app. Details in the guide.
- **Vendor-subscribed triggers (OAuth)**: the user subscribes to events in the dashboard; `list_subscriptions()` shows `events=<…>` — those are the ONLY valid `event_type` values. Each row carries a scope: `[personal]` rows link to user-scope triggers, `[agent <name>]` rows to that agent's agent-scope triggers; an agent trigger needs a subscription created FOR the agent (Agent Settings → MCPs → Subscribe to events for this agent). Copy the exact string; an invented variant (`"comment.create"`) is rejected. `subject.type` is the per-event action (`create`, `opened`), not the resource. Vendor delivery is async: change the filter once, post one test action, wait ~10–15 s, check `fired_count` with `get_trigger` — never rapid-fire tests while flipping the filter.
- **After `create_trigger` succeeds**, explain the setup to the user: the full webhook URL is `https://<your-platform-host><webhook_path>`; they mint an API key in the dashboard (user: User Settings → API Keys, tick `triggers`; agent: Agent Settings → API Keys — shown ONCE), configure the external system to POST JSON with `Authorization: Bearer otok_<key>`, and test with `fire_trigger(id, body={...})` from the chat.
- **`debounce_seconds`** coalesces a chatty source into one fire per window (e.g. 60 for GitHub pushes).
- `pause_trigger` makes the webhook 404 until resumed; `delete_trigger` is permanent (the sender needs a new one); static triggers cannot be deleted.
- **Security**: webhook fires authenticate with their own scoped API key, shown once — if lost or leaked, mint a new one and revoke the old (revocation is immediate). A user trigger notifies only its creator; an agent trigger only this agent's users (`global`, another agent or an outsider needs a platform admin). Firing or changing a trigger takes the right to edit it.
