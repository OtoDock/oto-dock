---
name: notifications-guide
description: Reference for the notifications-mcp tools — cron schedules, the list/pause/resume/edit/cancel tools, timezone semantics for run_at, and worked examples of one-time, recurring and immediate notifications. Load before scheduling or editing a notification.
---

# Notifications — the guide

The `notification-instructions` card in your instructions carries the rules
(when to notify, severity, scope, reminders vs tasks). This guide is the
reference behind them.

## Scheduling

Notifications support `run_at` (one-time future) and `schedule` (cron recurring). Omit both for immediate delivery.

## Cron schedules

`schedule` accepts standard 5-field POSIX cron — `minute hour day month weekday`. Sub-daily intervals are fully supported:

- `*/10 * * * *` — every 10 minutes
- `0 */3 * * *` — every 3 hours
- `*/15 9-17 * * 1-5` — every 15 minutes, 9am-5pm, weekdays
- `0 9 * * *` — daily at 9am
- `0 9 * * 1` — every Monday at 9am

## Managing existing notifications

- **`list_notifications`** — shows the scheduled notifications for this agent your user may see (the agent's own from the editor role, plus their own) with their current status (`active` or `paused`), severity, schedule, and ID. Always call this first to find the notification ID before pausing/resuming/cancelling.
- **`pause_notification(id)`** — stops a notification from firing on its schedule without deleting it. Can be resumed later. Use for "pause my reminders for X", "stop temporarily".
- **`resume_notification(id)`** — re-enables a paused notification. For one-time notifications whose `run_at` has already passed, resume keeps the row active but does NOT auto-fire — the user can fire it manually from the dashboard if they want.
- **`edit_notification(id, ...)`** — change the schedule, run time, title, body, or severity of an existing notification **without deleting and recreating it**. Pass only the fields you want to change. `schedule` and `run_at` are mutually exclusive — setting one switches the notification between recurring and one_time mode and clears the other. Use this whenever the user says "change the time of X", "update the message of X", "make the daily reminder weekly instead". Always prefer this over cancel+recreate — it preserves the notification ID and history.
- **`cancel_notification(id)`** — **permanently deletes** a notification. This cannot be undone. Use for "delete the reminder", "cancel forever". For temporary stops, use `pause_notification` instead.

One-time notifications auto-clean up after they fire successfully — the row is removed and won't appear in `list_notifications`. Recurring notifications persist and keep firing until paused or cancelled.

## Timezone semantics

The `[Current time: ...]` line at the start of each user message shows the user's **local** timezone with the IANA name and explicit UTC offset (e.g. `Europe/Athens (UTC+03:00)`, `America/New_York (UTC-04:00)`). The time is rendered in 24-hour form first, then in parentheses again as 12-hour with AM/PM (e.g. `04:02 (4:02 AM)`, `17:00 (5:00 PM)`) — always trust the AM/PM gloss; never guess the half-of-day from the 24-hour number alone. This is detected from the user's browser, so it follows them when they travel.

When you compute future times for `run_at`:

- **Prefer naive ISO** (no offset, e.g. `'2026-04-29T10:00:00'`). The proxy interprets it in the user's local timezone — same one you see in `[Current time: ...]`. This matches how users speak ("remind me at 10am") and travels with them.
- **Don't append `Z` or `+00:00`** unless you genuinely mean UTC. UTC-tagged times are stored at the literal absolute moment, which is rarely what the user meant when they said a wall-clock time.
- **Recurring `schedule` (cron)** is also evaluated in the user's local timezone, snapshotted on the row at creation. To change the timezone of a recurring notification, use `edit_notification(id, ...)` with a new schedule — the proxy resnapshots automatically.

## Examples

One-time reminder:
```
create_notification(title="Meeting Reminder", body="Your standup meeting starts in 15 minutes", severity="info", type="one_time", run_at="2026-03-22T09:45:00")
```

Recurring notification:
```
create_notification(title="Daily Health Check", body="Review the server health dashboard", severity="info", type="recurring", schedule="0 9 * * *")
```

Immediate alert:
```
create_notification(title="Backup Complete", body="Weekly backup finished successfully — 45GB transferred", severity="success", type="one_time")
```
