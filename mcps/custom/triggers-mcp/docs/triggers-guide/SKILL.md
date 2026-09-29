---
name: triggers-guide
description: Reference for the triggers-mcp tools — the trigger↔task and trigger↔notification patterns with full call shapes, vendor-subscribed (OAuth) triggers and event_filter rules, payload placeholders, external-system setup and API keys, debounce, and worked examples. Load before creating or editing a webhook trigger.
---

# Triggers — the guide

The `trigger-instructions` card in your instructions carries the rules
(which tool for which request, the two scopes, exact `event_type` values,
what to tell the user). This guide is the reference behind them.

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

A trigger has no model of its own — the linked task's pin or its agent's default decides what a fire runs on. `list_triggers` shows it per row as `task=<name> model=<id> [pinned|agent default|layer default, tier N]`; `get_trigger` returns it as `task_effective_model` / `task_effective_model_source` / `task_effective_model_tier`. To change it, `edit_task(task_id, model=…)` in schedules-mcp (ask the user first); nothing on the trigger changes it.

## The trigger ↔ app handler pattern

An app with a server (a folder app the agent deploys with `deploy_app`)
can be woken by a webhook directly: no task, no LLM turn, no tokens. The
app's own code runs, and a wake that fails is retried for you. (An app
that came with a community agent template may carry its triggers already:
the template's `blueprint.json` names them and the platform seeded one per
copy at install — `list_triggers` shows them; nothing below is needed by
hand for those.)

1. The app declares the handler in its `app.json` and is deployed and
   approved:
   ```json
   {"title": "Deploy board", "handlers": {"on_trigger": ["github"]}}
   ```
   Its server answers `POST /_handler/github` (only the platform can reach
   that path) within 60 s.
2. Aim a trigger at it:
   ```
   create_trigger(
     name="GitHub pushes",
     scope="agent",            # a shared app takes agent scope (editor+);
                               # a personal app takes its owner's user scope
     app_slug="deploy-board",
     handler="github",
   )
   ```
   `app_slug` never combines with `task_id` (one or the other; an inline
   `notify` may join either), and `debounce_seconds` must stay 0 — a
   debounced fire is dropped, and a wake must not be.
3. The external system POSTs the webhook URL exactly as for a task
   trigger. The whole body reaches the handler as the delivery's payload.
   A caller that may retry can send `X-OtoDock-Event-Id: <its own id>`;
   the same id twice is a no-op that answers `{"status": "ok", "actions":
   ["duplicate"]}` with the first delivery's id. An event the app refused
   (`errors: ["app: queue full"]`) is not remembered, so resending it with
   the same id is taken as a new event.

Refusals worth recognising: **404** "no app … in this scope" (wrong scope,
or the slug is not an app of this agent), **400** with the declared
handler names (the handler is not in the app's `on_trigger` list),
**409** "approve the app first" (the app's card is still waiting), and on
an older platform **400** "at least one action" (app handlers are not
there yet). A wake that dies (the handler answered 4xx, or five attempts
failed) writes the reason to the trigger's `last_error`, and the app's
own `deploy_status` lists its last wakes.

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

An agent trigger's notify goes to all of this agent's users when it names no target (`target_scope="agent"` is the default), or to one of them (`target_scope="user"`, `target=<username>`). Every user on the platform (`target_scope="global"`), another agent, or someone outside this agent needs a platform admin; the create or edit is refused otherwise.

You can combine both: a trigger with `task_id` AND `notify` enabled fires both on every webhook call.

## Vendor-subscribed triggers (OAuth: GitHub / Linear / Slack / Microsoft / Zoom)

For vendors connected via OAuth you do NOT configure a raw webhook URL + API key — the platform auto-registers the webhook. Workflow:

1. The user subscribes to events in the dashboard (Connected Accounts → expand the account → **Subscribe to events**, choosing **Subscribe as: Me** for a personal subscription or an agent for one the agent's triggers can use; the agent option also lives in Agent Settings → MCPs → **Subscribe to events for this agent**). Subscriptions are read-only from this tool. A subscription's scope must match the trigger's: `[personal]` rows link to user-scope triggers, `[agent <name>]` rows to that agent's agent-scope triggers (a shared app is woken by an agent-scope trigger, so it needs the agent's subscription); the server refuses any other pairing and its message names the surface to use.
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

3. **Test it** with `fire_trigger(id, body={...sample payload...})` from the chat. This bypasses the Bearer check (uses session auth) and exercises the full task / notification path. It takes the right to edit the trigger: a user who may only view it cannot test-fire it.

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

**Agent-wide server-down alert (editor or above):**
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
