"""Trigger registration, validation, and fire orchestration.

This is the service layer between the API/MCP and the storage layer. It
owns:

  - Trigger CRUD validation (slug regex, scope rules, cross-scope task
    linkage rejection, notify target rules, subscription scope-bridge)
  - Fire orchestration (debounce, placeholder substitution, fan out to task
    + notification)
  - Username resolution for user-scoped triggers

Webhook auth is handled in services/infra/api_key_manager.py for generic
(otok_) URL fires, OR by per-vendor signature verification in
``services/webhooks/webhook_dispatcher.py`` for vendor-subscribed
triggers — both paths converge here at ``fire_trigger``.
"""

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

from storage.automation import trigger_store
from storage import database as task_store
from storage.automation import notification_store

if TYPE_CHECKING:
    from auth.webhook_providers.base import NormalizedEvent

logger = logging.getLogger("claude-proxy.triggers")


# Slug must be URL-safe and human-readable. Lowercase letters, digits,
# dashes; 1-64 chars; can't start/end with a dash.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

# Per-trigger debounce state. In-memory only — multi-replica K8s setups
# accept best-effort. Keyed by trigger.id, value is last-fire monotonic ts.
_last_triggered: dict[str, float] = {}


VALID_SCOPES = {"user", "agent"}
VALID_SEVERITIES = {"info", "success", "warning", "danger"}
VALID_NOTIFY_TARGET_SCOPES = {"user", "agent", "global"}


# =====================================================================
# Validation
# =====================================================================


class TriggerValidationError(ValueError):
    """Raised when trigger create/edit input fails validation.

    Caller (API layer) maps to HTTP 400. Service layer raises rather than
    returning ``(ok, error)`` because validation can fail at multiple
    points and exception unwinding is cleaner than threading errors back.
    """


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str):
        raise TriggerValidationError("slug must be a string")
    slug = slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise TriggerValidationError(
            "slug must be 1-64 chars, lowercase letters / digits / dashes, "
            "no leading/trailing dash"
        )
    return slug


def _slugify(value: str) -> str:
    """Best-effort slug derivation from a name. Caller validates result."""
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower())
    return s.strip("-")[:64]


def _validate_scope(scope: str) -> str:
    if scope not in VALID_SCOPES:
        raise TriggerValidationError(f"scope must be one of {sorted(VALID_SCOPES)}")
    return scope


def _validate_severity(sev: str | None) -> str:
    if sev is None:
        return "info"
    if sev not in VALID_SEVERITIES:
        raise TriggerValidationError(
            f"severity must be one of {sorted(VALID_SEVERITIES)}"
        )
    return sev


def _validate_notify_target(
    *,
    scope: str,
    created_by: str,
    notify_enabled: bool,
    notify_target_scope: str | None,
    notify_target: str | None,
) -> tuple[str | None, str | None]:
    """Apply cross-scope rules for the trigger's inline notification config.

    Returns the canonical ``(notify_target_scope, notify_target)`` tuple.

    Rules:
      - If notify is disabled, target/scope MUST be NULL.
      - For user-scoped triggers: notify can only target the creator (NULL or
        explicitly the creator's user_sub). Cross-user notify forbidden.
      - For agent-scoped triggers: notify_target_scope must be one of the
        valid set; target may be a username (resolved later) or agent name
        or NULL (broadcast to scope).
    """
    if not notify_enabled:
        return None, None

    if scope == "user":
        # User triggers can only notify the creator. Force scope='user'.
        target_scope = "user"
        if notify_target is None:
            return target_scope, None  # defaults to creator at fire time
        if notify_target != created_by:
            # Allow username form too — resolve and compare.
            resolved = notification_store.resolve_username_to_sub(notify_target)
            if resolved != created_by:
                raise TriggerValidationError(
                    "user-scoped triggers can only notify their own creator"
                )
            return target_scope, created_by
        return target_scope, created_by

    # scope == "agent"
    target_scope = (notify_target_scope or "agent").lower()
    if target_scope not in VALID_NOTIFY_TARGET_SCOPES:
        raise TriggerValidationError(
            f"notify_target_scope must be one of {sorted(VALID_NOTIFY_TARGET_SCOPES)}"
        )
    if target_scope == "global":
        # global broadcasts have no specific target
        return target_scope, None
    if target_scope == "user" and notify_target:
        # Resolve username → user_sub for stable storage.
        if len(notify_target) < 30:  # heuristic from notifications API
            resolved = notification_store.resolve_username_to_sub(notify_target)
            if not resolved:
                raise TriggerValidationError(
                    f"notify_target user {notify_target!r} not found"
                )
            return target_scope, resolved
    return target_scope, notify_target


def _validate_task_linkage(
    *,
    task_id: str | None,
    trigger_scope: str,
    trigger_owner: str,
    trigger_agent: str,
) -> None:
    """Enforce cross-scope rules between a trigger and its linked task.

    Trigger scope == task scope. Trigger created_by == task created_by.
    Trigger agent == task agent. Task type MUST be ``trigger`` (no schedule
    / run_at — only fired when a trigger fires it).

    Raises TriggerValidationError on any mismatch. NULL task_id is allowed
    (trigger fires only its inline notify).
    """
    if not task_id:
        return
    task = task_store.get_dynamic_task(task_id)
    if not task:
        raise TriggerValidationError(f"task_id {task_id!r} not found")
    if task.get("scope") != trigger_scope:
        raise TriggerValidationError(
            f"task scope {task.get('scope')!r} does not match trigger scope "
            f"{trigger_scope!r} (cross-scope linkage forbidden)"
        )
    if task.get("agent") != trigger_agent:
        raise TriggerValidationError(
            f"task agent {task.get('agent')!r} does not match trigger agent "
            f"{trigger_agent!r}"
        )
    if task.get("created_by") != trigger_owner:
        raise TriggerValidationError(
            "task creator does not match trigger creator (cross-user "
            "linkage forbidden)"
        )
    if task.get("task_type") != "trigger":
        raise TriggerValidationError(
            f"task task_type must be 'trigger' (got {task.get('task_type')!r}). "
            "Create a trigger-only task with task_type='trigger'."
        )


def _validate_action(
    *,
    task_id: str | None,
    notify_enabled: bool,
) -> None:
    """At least one action must be configured. Otherwise the trigger is a
    no-op and we reject it so users don't ship dead-end webhooks."""
    if not task_id and not notify_enabled:
        raise TriggerValidationError(
            "trigger must have at least one action: task_id or notify_enabled"
        )


def _validate_subscription_linkage(
    *,
    subscription_id: str | None,
    trigger_scope: str,
    trigger_owner: str,
    trigger_agent: str,
) -> None:
    """Enforce the scope bridge between subscriptions and triggers.

    Subscriptions use scope 'user'|'service' (mirroring account scope);
    triggers use 'user'|'agent'. The mapping is:
      * subscription.scope='user'  ⇔ trigger.scope='user'
        AND subscription.owner == trigger.created_by
      * subscription.scope='service' ⇔ trigger.scope='agent'
        AND subscription.agent == trigger.agent

    Any other combination is rejected so a user can't redirect a vendor
    event into another user's automation, and a service-account
    subscription can't fire a personal trigger.
    """
    if not subscription_id:
        return
    # Lazy import — storage layer is loaded after services in some startup paths.
    from storage.automation import webhook_subscription_store
    sub = webhook_subscription_store.get_subscription(subscription_id)
    if not sub:
        raise TriggerValidationError(
            f"subscription_id {subscription_id!r} not found"
        )
    sub_scope = sub.get("scope")
    if trigger_scope == "user":
        if sub_scope != "user":
            raise TriggerValidationError(
                f"trigger.scope='user' requires subscription.scope='user' "
                f"(got {sub_scope!r})"
            )
        if sub.get("owner") != trigger_owner:
            raise TriggerValidationError(
                "user-scope subscription must belong to the trigger creator"
            )
    elif trigger_scope == "agent":
        if sub_scope != "service":
            raise TriggerValidationError(
                f"trigger.scope='agent' requires subscription.scope='service' "
                f"(got {sub_scope!r})"
            )
        if sub.get("agent") != trigger_agent:
            raise TriggerValidationError(
                f"service-scope subscription is bound to agent "
                f"{sub.get('agent')!r}, not the trigger's agent {trigger_agent!r}"
            )


def _validate_event_filter(
    *,
    subscription_id: str | None,
    event_filter: dict | None,
) -> None:
    """Reject a subscription-linked trigger whose ``event_filter`` filters on an
    ``event_type`` the subscription doesn't actually receive — such a trigger
    would silently NEVER fire. Fail up-front with the valid set instead of
    letting a dead trigger sit there (the failure mode that makes agents/users
    guess the value empirically).

    Only ``event_type`` is checked: it maps to the subscription's
    ``selected_events`` (the manifest event_catalog keys). ``subject.type`` is
    the per-event ACTION (e.g. ``create`` / ``opened``), not a catalog key, so
    it is intentionally not validated here.
    """
    if not subscription_id or not isinstance(event_filter, dict):
        return
    et = event_filter.get("event_type")
    if isinstance(et, str):
        wanted = [et]
    elif isinstance(et, list):
        wanted = [x for x in et if isinstance(x, str)]
    else:
        return  # no event_type filter (or non-string) → nothing to check
    if not wanted:
        return

    from storage.automation import webhook_subscription_store
    sub = webhook_subscription_store.get_subscription(subscription_id)
    if not sub:
        return  # missing subscription is handled by _validate_subscription_linkage
    selected = sub.get("selected_events") or []
    if isinstance(selected, str):
        import json
        try:
            selected = json.loads(selected or "[]")
        except (ValueError, TypeError):
            selected = []
    if not isinstance(selected, list) or not selected:
        return  # nothing to validate against

    bad = [e for e in wanted if e not in selected]
    if bad:
        raise TriggerValidationError(
            f"event_filter.event_type {bad!r} is not one of the events this "
            f"subscription receives, so the trigger would never fire. Valid "
            f"event_type values: {sorted(selected)}. (event_type is the event "
            f"category, e.g. 'Comment'; for the action use subject.type, "
            f"e.g. 'create'.)"
        )


# =====================================================================
# Create / Update
# =====================================================================


def register_trigger(
    *,
    name: str,
    scope: str,
    agent: str,
    created_by: str,
    slug: str | None = None,
    task_id: str | None = None,
    notify_enabled: bool = False,
    notify_severity: str = "info",
    notify_title: str | None = None,
    notify_body: str | None = None,
    notify_target_scope: str | None = None,
    notify_target: str | None = None,
    debounce_seconds: int = 0,
    enabled: bool = True,
    subscription_id: str | None = None,
    event_filter: dict | None = None,
) -> dict:
    """Create a trigger row after validating all business rules.

    Maps slug derivation, scope/severity validation, cross-scope task
    linkage, notify target rules, at-least-one-action invariant, and
    subscription scope-bridge.

    Raises TriggerValidationError on validation failure (caller maps to
    400). Re-raises psycopg.errors.UniqueViolation on slug collision —
    caller should map to 400 with a clear message.
    """
    if not name or not name.strip():
        raise TriggerValidationError("name required")
    name = name.strip()

    scope = _validate_scope(scope)
    if not slug:
        slug = _slugify(name)
        if not slug:
            raise TriggerValidationError(
                "slug could not be derived from name; supply slug explicitly"
            )
    slug = _validate_slug(slug)

    if not agent or not agent.strip():
        raise TriggerValidationError("agent required")

    if debounce_seconds is None:
        debounce_seconds = 0
    if debounce_seconds < 0:
        raise TriggerValidationError("debounce_seconds must be >= 0")

    sev = _validate_severity(notify_severity)

    target_scope, target_resolved = _validate_notify_target(
        scope=scope, created_by=created_by,
        notify_enabled=notify_enabled,
        notify_target_scope=notify_target_scope,
        notify_target=notify_target,
    )

    _validate_task_linkage(
        task_id=task_id,
        trigger_scope=scope,
        trigger_owner=created_by,
        trigger_agent=agent,
    )

    _validate_subscription_linkage(
        subscription_id=subscription_id,
        trigger_scope=scope,
        trigger_owner=created_by,
        trigger_agent=agent,
    )

    if event_filter is not None and not isinstance(event_filter, dict):
        raise TriggerValidationError(
            "event_filter must be an object (equality dict) when supplied"
        )

    _validate_event_filter(
        subscription_id=subscription_id, event_filter=event_filter,
    )

    _validate_action(task_id=task_id, notify_enabled=notify_enabled)

    if notify_enabled:
        if not notify_title or not notify_title.strip():
            raise TriggerValidationError("notify_title required when notify enabled")
        if not notify_body or not notify_body.strip():
            raise TriggerValidationError("notify_body required when notify enabled")

    row = trigger_store.create_trigger(
        slug=slug, name=name, scope=scope, agent=agent.strip(),
        created_by=created_by,
        task_id=task_id,
        notify_enabled=notify_enabled,
        notify_severity=sev,
        notify_title=notify_title,
        notify_body=notify_body,
        notify_target_scope=target_scope,
        notify_target=target_resolved,
        debounce_seconds=debounce_seconds,
        enabled=enabled,
        subscription_id=subscription_id,
        event_filter=event_filter or {},
    )
    logger.info(
        f"Trigger created: id={row['id'][:8]} scope={scope} agent={agent} "
        f"slug={slug} by={created_by[:12]}"
    )
    return row


def update_trigger(trigger_id: str, fields: dict) -> tuple[bool, str | None]:
    """Apply a partial edit to an existing trigger.

    Returns ``(ok, error)``. ``error`` is non-empty for validation failures
    (caller maps to 400). ``(False, None)`` means the row doesn't exist
    (404).

    Scope, slug, agent, created_by are immutable once set. Caller should
    pre-filter the payload, but we also strip these fields here defensively.
    """
    existing = trigger_store.get_trigger(trigger_id)
    if not existing:
        return False, None

    payload = {k: v for k, v in fields.items() if k in
               trigger_store._EDITABLE_TRIGGER_COLUMNS}
    if not payload:
        return False, "no editable fields supplied"

    # Re-validate the post-edit row state. Compute final values for fields
    # that may have changed AND any fields they depend on.
    final = {**existing, **payload}

    try:
        if "notify_severity" in payload:
            payload["notify_severity"] = _validate_severity(payload.get("notify_severity"))
            final["notify_severity"] = payload["notify_severity"]

        if any(k in payload for k in (
            "notify_enabled", "notify_target_scope", "notify_target",
        )):
            target_scope, target_resolved = _validate_notify_target(
                scope=existing["scope"],
                created_by=existing["created_by"],
                notify_enabled=bool(final.get("notify_enabled")),
                notify_target_scope=final.get("notify_target_scope"),
                notify_target=final.get("notify_target"),
            )
            payload["notify_target_scope"] = target_scope
            payload["notify_target"] = target_resolved
            final["notify_target_scope"] = target_scope
            final["notify_target"] = target_resolved

        if "task_id" in payload and payload["task_id"]:
            _validate_task_linkage(
                task_id=payload["task_id"],
                trigger_scope=existing["scope"],
                trigger_owner=existing["created_by"],
                trigger_agent=existing["agent"],
            )

        if "event_filter" in payload:
            ef = payload["event_filter"]
            if ef is not None and not isinstance(ef, dict):
                raise TriggerValidationError(
                    "event_filter must be an object when supplied"
                )
            _validate_event_filter(
                subscription_id=final.get("subscription_id"),
                event_filter=final.get("event_filter"),
            )

        _validate_action(
            task_id=final.get("task_id"),
            notify_enabled=bool(final.get("notify_enabled")),
        )

        if final.get("notify_enabled"):
            if not (final.get("notify_title") or "").strip():
                raise TriggerValidationError(
                    "notify_title required when notify enabled"
                )
            if not (final.get("notify_body") or "").strip():
                raise TriggerValidationError(
                    "notify_body required when notify enabled"
                )

        if "debounce_seconds" in payload:
            ds = payload["debounce_seconds"]
            if ds is None or ds < 0:
                raise TriggerValidationError("debounce_seconds must be >= 0")

    except TriggerValidationError as e:
        return False, str(e)

    ok = trigger_store.update_trigger(trigger_id, payload)
    return (ok, None) if ok else (False, None)


def pause_trigger(trigger_id: str) -> tuple[bool, str | None]:
    existing = trigger_store.get_trigger(trigger_id)
    if not existing:
        return False, None
    ok = trigger_store.set_trigger_enabled(trigger_id, False)
    return (ok, None) if ok else (False, None)


def resume_trigger(trigger_id: str) -> tuple[bool, str | None]:
    existing = trigger_store.get_trigger(trigger_id)
    if not existing:
        return False, None
    ok = trigger_store.set_trigger_enabled(trigger_id, True)
    return (ok, None) if ok else (False, None)


def delete_trigger(trigger_id: str) -> tuple[bool, str | None]:
    existing = trigger_store.get_trigger(trigger_id)
    if not existing:
        return False, None
    ok = trigger_store.delete_trigger(trigger_id)
    _last_triggered.pop(trigger_id, None)
    return (ok, None) if ok else (False, None)


# =====================================================================
# Fire path
# =====================================================================


def _substitute_placeholders(template: str | None, context: dict) -> str | None:
    """Replace ``{{key}}`` and ``{{a.b.c}}`` with values from ``context``.

    Dot-paths walk nested dicts — ``{{subject.title}}`` resolves
    ``context["subject"]["title"]``. Missing keys / non-dict intermediates
    render as empty string. Top-level keys still work (``{{phone}}``) so
    phone + generic-webhook templates that use flat raw-body keys keep
    working unchanged.

    For vendor fires, the dispatcher merges normalized event
    namespaces (``actor``, ``subject``, ``target``, ``event_type``,
    ``vendor_event_id``) into the context alongside the raw webhook body,
    so notify templates can reference both shapes.
    """
    if template is None:
        return None
    if not isinstance(context, dict):
        context = {}

    def _walk(key: str):
        cursor = context
        for seg in key.split("."):
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(seg)
            if cursor is None:
                return None
        return cursor

    def _repl(match):
        val = _walk(match.group(1).strip())
        return str(val) if val is not None else ""

    return re.sub(r"\{\{([^{}]+)\}\}", _repl, template)


def _build_substitution_context(
    body: dict, vendor_event: "NormalizedEvent | None" = None,
) -> dict:
    """Merge the raw webhook body with normalized vendor-event namespaces.

    Returns a flat-keyed dict where vendor_event fields are accessible
    as nested-dot paths (``{{subject.title}}`` → context["subject"]["title"]).
    The raw body's top-level keys remain accessible verbatim — for
    overlapping names, body keys win (e.g. if both raw body and the
    normalizer expose ``actor``, the body's value is preserved).
    """
    if not isinstance(body, dict):
        body = {}
    if vendor_event is None:
        return body
    return {
        # Normalized namespaces first so body overrides on collision.
        "actor": dict(vendor_event.actor or {}),
        "subject": dict(vendor_event.subject or {}),
        "target": dict(vendor_event.target or {}),
        "event_type": vendor_event.event_type,
        "vendor_event_id": vendor_event.vendor_event_id,
        **body,
    }


def _check_debounce(trigger_id: str, debounce_seconds: int) -> float | None:
    """Return remaining-debounce seconds (>0) if blocked, else None.

    Updates ``_last_triggered`` only when debounce passes (so debounced
    requests don't reset the timer).
    """
    if debounce_seconds <= 0:
        _last_triggered[trigger_id] = time.monotonic()
        return None
    last = _last_triggered.get(trigger_id, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < debounce_seconds:
        return debounce_seconds - elapsed
    _last_triggered[trigger_id] = time.monotonic()
    return None


async def fire_trigger(
    trigger_row: dict,
    body: dict,
    *,
    trigger_source: str | None = None,
    vendor_event: "NormalizedEvent | None" = None,
) -> dict:
    """Fire a trigger: substitute placeholders, run task and/or notification.

    Caller must have authenticated the request and verified ``trigger_row``
    is enabled. Returns a dict response suitable for the HTTP layer.

    Behaviour:
      - Debounce check first → ``{status: "debounced", retry_after_seconds}``
      - If task_id set: substitute placeholders into task prompt, call
        ``scheduler.trigger_task_now()``
      - If notify_enabled: substitute placeholders into title/body, call
        ``notification_manager.fire_notification()``
      - Updates ``fired_count``, ``last_fired_at``, ``last_error`` on the row
      - Errors in one branch don't block the other (partial response)

    ``vendor_event`` is populated by ``webhook_dispatcher`` for
    vendor-source fires. The normalized event lands in the trigger_payload
    under ``actor``/``subject``/``target``/``event_type`` keys for
    ``${trigger.*}`` token resolution in manifest agent_context blocks.
    Generic webhook fires + phone fires leave it None.
    """
    trigger_id = trigger_row["id"]

    # 1. Debounce
    remaining = _check_debounce(trigger_id, int(trigger_row.get("debounce_seconds") or 0))
    if remaining is not None:
        return {
            "status": "debounced",
            "trigger_id": trigger_id,
            "retry_after_seconds": round(remaining, 1),
        }

    errors: list[str] = []
    actions: list[str] = []

    # 2. Task — only the linked path exists in the current model (legacy
    # _fire_inline_prompt path removed; `prompt_template` column dropped).
    task_run_id: str | None = None
    if trigger_row.get("task_id"):
        try:
            task_run_id = await _fire_linked_task(
                trigger_row, body, trigger_source, vendor_event,
            )
            if task_run_id:
                actions.append("task")
        except Exception as e:
            errors.append(f"task: {e}")
            logger.exception(f"Trigger {trigger_id[:8]} task fire failed")

    # 3. Notification
    delivery_count = 0
    if trigger_row.get("notify_enabled"):
        try:
            delivery_count = await _fire_inline_notification(
                trigger_row, body, vendor_event,
            )
            if delivery_count:
                actions.append("notify")
        except Exception as e:
            errors.append(f"notify: {e}")
            logger.exception(f"Trigger {trigger_id[:8]} notify fire failed")

    # 4. Stats
    err_text = "; ".join(errors) if errors else None
    await asyncio.to_thread(trigger_store.record_fire, trigger_id, error=err_text)

    status = "ok" if not errors else ("partial" if actions else "failed")
    return {
        "status": status,
        "trigger_id": trigger_id,
        "actions": actions,
        "task_run_id": task_run_id,
        "delivery_count": delivery_count,
        "errors": errors or None,
    }


def _build_trigger_payload(
    trigger_row: dict,
    body: dict,
    vendor_event: "NormalizedEvent | None" = None,
) -> dict:
    """Assemble the structured payload threaded into the task session.

    ``dynamic_context._build_trigger_tokens`` reads this dict
    to populate ``${trigger.*}`` tokens for manifest ``agent_context``
    blocks (and ``builder.args`` templates). The flat normaliser dips into
    ``body`` for ``phone``/``email``/etc. when they're not at top level,
    so we pass the raw webhook body untouched.

    When ``vendor_event`` is set (webhook_dispatcher path for
    vendor-subscribed triggers), the normalized actor/subject/target dicts
    + event_type + vendor_event_id + provider_id + subscription_id are
    merged in alongside the existing phone-path fields. Phone path passes
    ``vendor_event=None`` so its tokens remain populated and the new
    vendor fields render empty — single payload shape, no branching.
    """
    payload: dict = {
        "source": "webhook",
        "route": trigger_row.get("slug") or "",
        "did": "",
        "body": body if isinstance(body, dict) else {},
        # Vendor-event fields default to empty; phone fires leave them
        # empty too, so existing phone tokens (${trigger.phone} etc.) still work.
        "event_type": "",
        "vendor_event_id": "",
        "provider_id": "",
        "subscription_id": trigger_row.get("subscription_id") or "",
        "actor": {},
        "subject": {},
        "target": {},
    }
    if vendor_event is not None:
        payload["event_type"] = vendor_event.event_type
        payload["vendor_event_id"] = vendor_event.vendor_event_id
        payload["actor"] = dict(vendor_event.actor or {})
        payload["subject"] = dict(vendor_event.subject or {})
        payload["target"] = dict(vendor_event.target or {})
        # provider_id comes from the subscription row, looked up via trigger_row.
        # The dispatcher knows it but we already have it on the trigger row
        # indirectly — webhook_dispatcher passes trigger_source like
        # "webhook:github/<sub_id>/pull_request", which the agent sees.
    return payload


async def _fire_linked_task(
    trigger_row: dict,
    body: dict,
    trigger_source: str | None,
    vendor_event: "NormalizedEvent | None" = None,
) -> str | None:
    """Run the linked task with placeholder-substituted prompt."""
    from services.scheduler import scheduler  # avoid circular at import-time
    task = await asyncio.to_thread(task_store.get_dynamic_task, trigger_row["task_id"])
    if not task:
        raise RuntimeError("linked task not found")
    if not task.get("enabled", True):
        raise RuntimeError("linked task is paused")

    task_def = scheduler._row_to_task(task)
    # Same enriched substitution context for linked-task prompts as for
    # notifications — task prompts can reference {{subject.title}} etc.
    # for vendor fires, or {{phone}}/raw-body keys for phone/generic fires.
    context = _build_substitution_context(body, vendor_event)
    final_prompt = _substitute_placeholders(task_def.prompt, context) or task_def.prompt
    return await scheduler.trigger_task_now(
        task_def,
        trigger_type="trigger",
        trigger_source=trigger_source or f"trigger:{trigger_row['slug']}",
        prompt_override=final_prompt,
        trigger_payload=_build_trigger_payload(trigger_row, body, vendor_event),
    )


async def _fire_inline_notification(
    trigger_row: dict,
    body: dict,
    vendor_event: "NormalizedEvent | None" = None,
) -> int:
    """Fire the trigger's inline notification with placeholder substitution.

    When ``vendor_event`` is present (webhook_dispatcher path),
    the substitution context includes the normalized actor/subject/target
    dicts so notify_title/notify_body templates can reference dot-paths
    like ``{{subject.title}}``, ``{{actor.name}}``, ``{{target.id}}``.

    Returns delivery count.
    """
    from services.notifications import notification_manager
    context = _build_substitution_context(body, vendor_event)
    title = _substitute_placeholders(
        trigger_row.get("notify_title") or "", context,
    ) or ""
    body_text = _substitute_placeholders(
        trigger_row.get("notify_body") or "", context,
    ) or ""

    target_scope = trigger_row.get("notify_target_scope")
    target = trigger_row.get("notify_target")
    # User-scoped trigger with NULL target → notify creator.
    if trigger_row["scope"] == "user" and target is None:
        target_scope = "user"
        target = trigger_row["created_by"]

    deliveries = await notification_manager.fire_notification(
        title=title,
        body=body_text,
        severity=trigger_row.get("notify_severity") or "info",
        scope=target_scope or "user",
        target=target,
        source="trigger",
        source_id=trigger_row["id"],
        agent_slug=trigger_row.get("agent"),
    )
    return len(deliveries)
