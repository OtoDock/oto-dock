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
from storage import db_apps
from storage.automation import notification_store
from services.scheduler import task_kinds
from core.session import visibility as _vis

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
        or NULL (broadcast to scope). Who it may reach is
        ``_check_notify_reach``.
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

    # An agent-scoped trigger: the notify may broadcast or name a target.
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


def _check_notify_reach(
    *,
    agent: str,
    created_by: str,
    target_scope: str | None,
    target: str | None,
    caller_is_admin: bool,
) -> None:
    """Who an agent-scoped trigger's notify may reach, by the rules
    ``POST /v1/notifications`` keeps: the trigger's own agent (``agent``
    with no target or this agent) and one of its users or the creator
    (``user``); another agent, a user outside the agent, or every user
    (``global``) only for a platform admin. The caller is whoever aims the
    notify: the creator at create, the editor at edit."""
    if caller_is_admin:
        return
    if target_scope == "global":
        raise TriggerValidationError(
            "notifying every user (notify_target_scope 'global') needs a platform admin"
        )
    if target_scope == _vis.SCOPE_AGENT and target and target != agent:
        raise TriggerValidationError(
            f"an agent-scoped trigger notifies its own agent's users, not agent {target!r}"
        )
    if target_scope == _vis.SCOPE_USER and target and target != created_by:
        from services.notifications.notification_manager import agent_audience
        if target not in agent_audience(agent):
            raise TriggerValidationError(
                f"notify_target must be a user of agent {agent!r}"
            )


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
    if task.get("task_type") != task_kinds.TRIGGER:
        raise TriggerValidationError(
            f"task task_type must be 'trigger' (got {task.get('task_type')!r}). "
            "Create a trigger-only task with task_type='trigger'."
        )


class TriggerConflict(TriggerValidationError):
    """A valid target that is not ready yet (the route answers 409)."""


def _validate_app_linkage(
    *,
    app_id: str | None,
    handler: str | None,
    trigger_scope: str,
    trigger_owner: str,
    trigger_agent: str,
    debounce_seconds: int,
    require_approval: bool = True,
) -> None:
    """The ``app`` action (APPS.md "Handlers"): the app is a folder app of
    the trigger's agent; a shared app takes an agent-scoped trigger, a
    personal app a user-scoped trigger of its owner (the creator of an
    agent-scoped trigger already held editor+, the authority approving the
    app needed); the handler is one of the app's signed ``on_trigger``
    names; the app is approved — except for a template seed
    (``require_approval=False``: the copy may still wait on its card, and
    the handler drain holds every wake as "unapproved" until it is); no
    debounce (a debounced fire is dropped, and a wake must not be)."""
    if not app_id:
        return
    from api.apps import manifest as _mf
    row = task_store.get_app(app_id)
    if not row:
        raise TriggerValidationError("app not found")
    if (row.get("agent") or "") != trigger_agent:
        raise TriggerValidationError("the app belongs to another agent")
    if not db_apps.app_kind_of(row).may_serve:
        raise TriggerValidationError("only an app with a server has handlers")
    if row.get("hidden"):
        raise TriggerValidationError("the app is unpinned")
    if row.get("scope_chat_id") or row.get("scope_project_id"):
        raise TriggerValidationError("a Dock app has no handlers")
    if row.get("username"):
        if trigger_scope != "user" or (row.get("owner_sub") or "") != trigger_owner:
            raise TriggerValidationError(
                "a personal app takes a user-scoped trigger of its owner")
    elif trigger_scope != "agent":
        raise TriggerValidationError("a shared app takes an agent-scoped trigger")
    names = _mf.parse_handlers(row).get("on_trigger") or []
    if not handler or handler not in names:
        raise TriggerValidationError(
            f"handler {handler!r} is not an on_trigger handler of the app "
            f"(declared: {', '.join(names) or 'none'})")
    if int(debounce_seconds or 0):
        raise TriggerValidationError("debounce_seconds must be 0 for an app trigger")
    if require_approval and not task_store.app_actions_approved(row):
        raise TriggerConflict("approve the app first")


def _validate_action(
    *,
    task_id: str | None,
    notify_enabled: bool,
    app_id: str | None = None,
) -> None:
    """At least one action must be configured. Otherwise the trigger is a
    no-op and we reject it so users don't ship dead-end webhooks. A task
    and an app never share one trigger (the notify may join either)."""
    if task_id and app_id:
        raise TriggerValidationError("a trigger runs a task or wakes an app, not both")
    if not task_id and not notify_enabled and not app_id:
        raise TriggerValidationError(
            "trigger must have at least one action: task_id, app_slug or notify_enabled"
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
    mcp = sub.get("mcp_name") or "the MCP"
    if trigger_scope == "user":
        if sub_scope != "user":
            raise TriggerValidationError(
                f"this subscription belongs to agent {sub.get('agent')!r}; a "
                f"user trigger needs a personal subscription: Connected "
                f"Accounts → Subscribe to events → Subscribe as: Me"
            )
        if sub.get("owner") != trigger_owner:
            raise TriggerValidationError(
                "user-scope subscription must belong to the trigger creator"
            )
    elif trigger_scope == "agent":
        if sub_scope != "service":
            raise TriggerValidationError(
                f"this subscription is personal; an agent trigger needs a "
                f"subscription created for the agent: Agent Settings → MCPs "
                f"→ {mcp} → Subscribe to events for this agent"
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
    app_id: str | None = None,
    handler: str | None = None,
    require_approval: bool = True,
    trigger_id: str | None = None,
    community_template: str | None = None,
    community_template_item_slug: str | None = None,
    caller_is_admin: bool = False,
) -> dict:
    """Create a trigger row after validating all business rules.

    Maps slug derivation, scope/severity validation, cross-scope task
    linkage, notify target rules, at-least-one-action invariant, and
    subscription scope-bridge. A template seed (``app_blueprints``) passes
    its own id and provenance and ``require_approval=False``.
    ``caller_is_admin`` (a platform admin or the master key) widens the
    notify's reach (``_check_notify_reach``).

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
    if scope == _vis.SCOPE_AGENT:
        _check_notify_reach(
            agent=agent.strip(), created_by=created_by,
            target_scope=target_scope, target=target_resolved,
            caller_is_admin=caller_is_admin,
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

    _validate_action(task_id=task_id, notify_enabled=notify_enabled, app_id=app_id)
    _validate_app_linkage(
        app_id=app_id, handler=handler, trigger_scope=scope, trigger_owner=created_by,
        trigger_agent=agent, debounce_seconds=debounce_seconds,
        require_approval=require_approval,
    )

    if notify_enabled:
        if not notify_title or not notify_title.strip():
            raise TriggerValidationError("notify_title required when notify enabled")
        if not notify_body or not notify_body.strip():
            raise TriggerValidationError("notify_body required when notify enabled")

    row = trigger_store.create_trigger(
        slug=slug, name=name, scope=scope, agent=agent.strip(),
        created_by=created_by,
        trigger_id=trigger_id,
        community_template=community_template,
        community_template_item_slug=community_template_item_slug,
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
        app_id=app_id or None,
        handler=(handler or None) if app_id else None,
    )
    logger.info(
        f"Trigger created: id={row['id'][:8]} scope={scope} agent={agent} "
        f"slug={slug} by={created_by[:12]}"
    )
    return row


def update_trigger(
    trigger_id: str, fields: dict, *, caller_is_admin: bool = False,
) -> tuple[bool, str | None]:
    """Apply a partial edit to an existing trigger.

    Returns ``(ok, error)``. ``error`` is non-empty for validation failures
    (caller maps to 400). ``(False, None)`` means the row doesn't exist
    (404).

    Scope, slug, agent, created_by are immutable once set. Caller should
    pre-filter the payload, but we also strip these fields here defensively.
    An edit that aims the notify somewhere new meets ``_check_notify_reach``
    for the editor (``caller_is_admin``); one that leaves it where it is
    does not.
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
            aimed = ((existing.get("notify_target_scope"), existing.get("notify_target"))
                     if existing.get("notify_enabled") else None)
            if existing["scope"] == _vis.SCOPE_AGENT and (target_scope, target_resolved) != aimed:
                _check_notify_reach(
                    agent=existing["agent"], created_by=existing["created_by"],
                    target_scope=target_scope, target=target_resolved,
                    caller_is_admin=caller_is_admin,
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
            # A task replaces an app target (one action of the two).
            payload["app_id"] = None
            payload["handler"] = None
            final["app_id"] = None
            final["handler"] = None

        if payload.get("app_id") or (
                "handler" in payload and final.get("app_id")):
            _validate_app_linkage(
                app_id=final.get("app_id"),
                handler=final.get("handler"),
                trigger_scope=existing["scope"],
                trigger_owner=existing["created_by"],
                trigger_agent=existing["agent"],
                debounce_seconds=int(final.get("debounce_seconds") or 0),
            )
            payload["task_id"] = None
            final["task_id"] = None
        elif "app_id" in payload and not payload["app_id"]:
            payload["handler"] = None
            final["handler"] = None

        if "subscription_id" in payload:
            # Re-binding the vendor source keeps the creation rule (a user
            # trigger ↔ the creator's personal subscription, an agent trigger
            # ↔ the agent's service subscription); '' unbinds.
            sid = (payload.get("subscription_id") or "").strip() or None
            if sid:
                _validate_subscription_linkage(
                    subscription_id=sid,
                    trigger_scope=existing["scope"],
                    trigger_owner=existing["created_by"],
                    trigger_agent=existing["agent"],
                )
            payload["subscription_id"] = sid
            final["subscription_id"] = sid

        if "event_filter" in payload or "subscription_id" in payload:
            ef = final.get("event_filter")
            if "event_filter" in payload and ef is not None and not isinstance(ef, dict):
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
            app_id=final.get("app_id"),
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
            if ds and final.get("app_id"):
                raise TriggerValidationError("debounce_seconds must be 0 for an app trigger")

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


async def _fire_app_handler(
    trigger_row: dict, body: dict, vendor_event: "NormalizedEvent | None", event_id: str,
) -> dict:
    """The ``app`` action (APPS.md "Handlers"): a durable delivery for the
    app's handler carrying the same payload a task would see, plus the
    trigger; ``event_id`` (the caller's ``X-OtoDock-Event-Id``) makes a
    replay a no-op that names the first delivery."""
    from services.apps import app_handlers
    from storage import db_app_deliveries as deliveries
    row = await asyncio.to_thread(task_store.get_app, trigger_row["app_id"])
    if not row:
        raise RuntimeError("the app is gone")
    handler = trigger_row.get("handler") or ""
    payload = _build_trigger_payload(trigger_row, body, vendor_event)
    payload["trigger"] = {"id": trigger_row["id"], "slug": trigger_row.get("slug") or "",
                          "name": trigger_row.get("name") or ""}
    d = await app_handlers.enqueue(row, handler, f"trigger:{trigger_row.get('slug') or ''}",
                                   payload, event_id=event_id, trigger_id=trigger_row["id"])
    if d is None:
        first = await asyncio.to_thread(deliveries.find_event, row["id"], handler, event_id)
        return {"delivery_id": (first or {}).get("id"), "duplicate": True}
    if d["status"] != deliveries.PENDING:
        raise RuntimeError(d.get("last_error") or "not queued")
    return {"delivery_id": d["id"], "duplicate": False}


async def fire_trigger(
    trigger_row: dict,
    body: dict,
    *,
    trigger_source: str | None = None,
    vendor_event: "NormalizedEvent | None" = None,
    event_id: str = "",
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

    # 2b. App handler (APPS.md "Handlers"): the delivery row is the fire;
    # its verdict lands on the row later (``trigger_store.set_last_error``).
    delivery_id: str | None = None
    duplicate = False
    if trigger_row.get("app_id"):
        try:
            out = await _fire_app_handler(trigger_row, body, vendor_event, event_id)
            delivery_id, duplicate = out.get("delivery_id"), bool(out.get("duplicate"))
            actions.append("duplicate" if duplicate else "app")
        except Exception as e:
            errors.append(f"app: {e}")
            logger.exception(f"Trigger {trigger_id[:8]} app fire failed")

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
    try:
        from api.apps import catalog
        catalog.trigger_fired(trigger_row, err_text)
    except Exception:
        logger.debug("catalog trigger_fires delta failed", exc_info=True)

    status = "ok" if not errors else ("partial" if actions else "failed")
    return {
        "status": status,
        "trigger_id": trigger_id,
        "actions": actions,
        "task_run_id": task_run_id,
        "delivery_count": delivery_count,
        "delivery_id": delivery_id,
        "duplicate": duplicate,
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
        trigger_type=task_kinds.TRIGGER_TRIGGER,
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
    # User-scoped trigger with NULL target → notify creator; an agent notify
    # with no target → the trigger's agent.
    if trigger_row["scope"] == "user" and target is None:
        target_scope = "user"
        target = trigger_row["created_by"]
    elif target_scope == _vis.SCOPE_AGENT and not target:
        target = trigger_row.get("agent")

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
