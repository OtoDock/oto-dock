# Triggers — Webhook Automation

Triggers are HTTP webhooks fired by external systems (GitHub, Stripe, Linear, IoT devices, Zapier, etc.). Each trigger optionally fires a task, a notification, or both. They're the bridge between "something happened in another system" and "do something on this platform".

## When to use which tool

| User says | Tool to use |
|---|---|
| "Notify me when my PR is merged" | `create_trigger` (scope=user, notify-only) |
| "When the deploy webhook fires, run my code-review task" | `create_trigger` (scope=user, with task_id) |
| "Set up an alert for when the production server goes down" | `create_trigger` (scope=agent, manager-only) |
| "Show me my triggers" / "What webhooks do I have?" | `list_triggers` |
| "Pause / resume / delete trigger X" | `pause_trigger` / `resume_trigger` / `delete_trigger` |
| "Change the title of trigger X" / "Change which task X runs" | `edit_trigger` |
| "Test trigger X" | `fire_trigger` (sends a test payload, no real webhook needed) |

## The two scopes

**`scope='user'`** (default): personal automations. Created by anyone with agent access. Only the creator (and admin) can edit/delete. Notifications go to the creator. Authenticated by the user's own API key (User Settings → API Keys).

**`scope='agent'`** (manager+ only): business events affecting the whole team / agent. Created by managers or admin. Notifications can broadcast to all agent users. Authenticated by an agent API key (Agent Settings → API Keys).

If the user is asking for a **personal** automation (their PR, their ticket, their account), use `scope='user'`. If they're asking for **business** automation that should still work when they leave the company, use `scope='agent'` — and only proceed if you can confirm they have manager role on this agent.

## The trigger ↔ task pattern

To run a real LLM task on webhook (not just a notification), pair the trigger with a task:

1. Create a task with `task_type='trigger'` (no `schedule`, no `run_at` — it only runs when something fires it):
   ```
   create_one_time_task(
     name="Code review on PR",
     prompt="Review PR #{{pr_number}} from {{author}}: {{pr_url}}",
     task_type="trigger",
   )
   ```
   The task prompt can use `{{placeholder}}` tokens — they're substituted from the webhook payload at fire time.

2. Create the trigger pointing at it:
   ```
   create_trigger(
     name="GitHub PR opened",
     scope="user",
     task_id="<the task id from step 1>",
   )
   ```

The trigger and task **MUST** match on scope, agent, and creator (cross-scope linkage is rejected). User-scoped triggers can only fire user-scoped tasks; agent triggers only agent tasks.

## The trigger ↔ notification pattern

For lightweight alerts that don't need an LLM (server down, payment received, etc.), use the inline `notify` block on the trigger directly — much cheaper than spinning up a task:

```
create_trigger(
  name="Server down alert",
  scope="agent",
  notify={
    "enabled": true,
    "severity": "danger",
    "title": "ALERT: {{service}} is down",
    "body": "{{service}} returned {{status_code}} at {{timestamp}}",
  },
)
```

You can combine both: a trigger with `task_id` AND `notify` enabled fires both on every webhook call.

## Vendor-subscribed triggers (OAuth: GitHub / Linear / Slack / Microsoft / Zoom)

For vendors connected via OAuth you do NOT configure a raw webhook URL + API key — the platform auto-registers the webhook. Workflow:

1. The user subscribes to events in the dashboard (Connected Accounts → expand the account → **Subscribe to events**). Subscriptions are read-only from this tool.
2. `list_subscriptions()` → each row shows `events=<…>`. **Those are the valid `event_type` values** for that subscription (the manifest event_catalog keys) — e.g. Linear `events=Issue, Comment, Project, Cycle, Reaction`; GitHub `events=push, pull_request, issue_comment, …`.
3. `create_trigger(subscription_id=<id from list_subscriptions>, event_filter={…}, notify={…} or task_id=…)`.

**Writing `event_filter` correctly — this is the #1 mistake:**
- `event_type` = the event **category**, and it MUST be one of the subscription's `events=` values. Copy the EXACT string from `list_subscriptions` (e.g. `{"event_type": "Comment"}` for a Linear comment). Do **not** invent variants like `"comment"` or `"comment.create"` — an `event_type` the subscription doesn't receive is **rejected** by the server (it could never fire), and the error lists the valid values.
- `subject.type` = the per-event **action** (e.g. `create`, `opened`, `removed`), NOT the resource. So: new Linear comments = `{"event_type": "Comment", "subject.type": "create"}`; any comment change = `{"event_type": "Comment"}`.
- Empty `{}` = fire on every event the subscription receives (use only if you truly want all of them — the subscription may carry several event types).
- Other keys: `actor.{id,name,email}`, `subject.{id,title,url}`, `target.{id,type}`, `vendor_event_id`.

**Testing:** vendor webhook delivery is **async** — after the test action (e.g. posting a comment) wait ~10–15s, then check `fired_count` via `get_trigger`. Do NOT rapid-fire several test actions while flipping the filter: deliveries are deduped and delayed, so you'll misattribute which filter fired. Change the filter once, post one action, wait, check.

## Webhook payload → placeholder substitution

When the external system POSTs JSON to the webhook URL, every top-level key becomes available as `{{key}}` in the task prompt and notify title/body. Missing keys substitute to empty string. Example webhook body:

```json
{ "pr_number": 42, "author": "alice", "pr_url": "https://github.com/..." }
```

→ task prompt `"Review PR #{{pr_number}} from {{author}}"` becomes `"Review PR #42 from alice"`.

## Setting up the external system (the part the agent should explain to the user)

After `create_trigger` succeeds, the response includes the webhook URL (path). The full URL is `https://<your-platform-host><webhook_path>`. The user needs to:

1. **Mint an API key** in the dashboard:
   - For `scope='user'`: User Settings → API Keys → Create → tick `triggers` → save the key (shown ONCE).
   - For `scope='agent'`: Agent Settings → API Keys → Create → save the key.

2. **Configure the external system** to POST to the webhook URL with header:
   ```
   Authorization: Bearer otok_<the-key-from-step-1>
   Content-Type: application/json
   ```

3. **Test it** with `fire_trigger(id, body={...sample payload...})` from the chat. This bypasses the Bearer check (uses session auth) and exercises the full task / notification path.

Common external systems:
- **GitHub**: Settings → Webhooks → Add webhook. Set URL, content type = JSON, custom secret (the platform authenticates by the Bearer key, but setting one is good practice). For the `Authorization` header you may need to use a relay (GitHub doesn't support custom auth headers natively — Zapier or a Cloudflare Worker can add it).
- **Stripe**: Developers → Webhooks → Add endpoint. Stripe sends a `Stripe-Signature` header — the platform authenticates by the Bearer key, so use a relay if needed.
- **Linear**: Settings → API → Webhooks → Add. Linear lets you set custom headers directly. Add `Authorization: Bearer otok_…`.
- **Zapier / Make.com / n8n**: easiest — they let you set headers and body shape directly.
- **Custom scripts**: trivial `curl -H "Authorization: Bearer otok_…" -d '{...}' …`.

## Debounce

`debounce_seconds` rate-limits fires. Useful when the external system is chatty (e.g., GitHub fires `push` on every commit). Set to e.g. 60 to coalesce a flurry of pushes into one fire per minute. The first call passes; subsequent calls within the window return `{status: "debounced", retry_after_seconds: …}` and don't fire actions.

## Pause / Resume / Delete

- `pause_trigger(id)`: webhook returns 404 until resumed. Use for "stop alerting me about X for now".
- `resume_trigger(id)`: re-enable.
- `delete_trigger(id)`: permanent. The webhook URL becomes 404. The user will have to recreate + reconfigure the external system if they want it back. Static triggers (loaded from `triggers.json` files) cannot be deleted via this tool.

## Security notes — what to tell the user

- **Webhook fires need their own scoped API key** — minted with the trigger (shown once); external systems authenticate with that, nothing else.
- **API keys are shown ONCE** at creation. If lost, mint a new one and update the external system; the old one can be revoked.
- **Revoking a key** immediately invalidates webhooks using it. Use this if a key leaks (e.g., in a screenshot, GitHub Actions log, support ticket).
- **User-scoped triggers** can only notify the creator. They cannot target other users — that would be a privilege escalation vector.

## Examples to learn from

**Personal GitHub PR notification:**
```
1. create_one_time_task(
     name="PR merged celebrate",
     prompt="A PR was merged: {{title}} ({{html_url}}). Send me a celebratory note.",
     task_type="trigger",
   )
2. create_trigger(
     name="GitHub PR merged",
     scope="user",
     task_id=<id from step 1>,
     notify={"enabled": true, "title": "PR merged: {{title}}",
             "body": "{{html_url}}"},
   )
3. User mints a user API key with `triggers` permission.
4. User configures GitHub webhook (or relay) for `pull_request` event.
```

**Agent-wide server-down alert (manager only):**
```
create_trigger(
  name="Service down",
  scope="agent",
  notify={
    "enabled": true,
    "severity": "danger",
    "title": "ALERT: {{service_name}} is DOWN",
    "body": "{{service_name}} ({{url}}) returned status {{status}} at {{timestamp}}",
    "target_scope": "agent",
  },
  debounce_seconds=300,
)
```

**Per-user Stripe payment alert:**
```
create_trigger(
  name="Stripe payment received",
  scope="user",
  notify={
    "enabled": true,
    "severity": "success",
    "title": "Payment received: {{amount}}",
    "body": "From {{customer_email}}, charge {{id}}",
  },
)
```
