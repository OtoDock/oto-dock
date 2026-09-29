"""Tests for the triggers system.

Covers:
- Slug validation, name → slug derivation
- Scope validation
- Cross-scope task linkage rejection (security boundary)
- At-least-one-action invariant (no empty triggers)
- User-scope notify target lock-down (no cross-user)
- Notify config required title/body
- Static trigger upsert / mutate restriction / pause-resume allowed
- Slug uniqueness within (scope, owner) but cross-scope coexistence
- Edit immutability of scope/agent/created_by
- Pause / Resume / Delete service paths + permission boundaries
- Debounce (in-memory state)
- Placeholder substitution
- Cleanup on user / agent deletion

Run: cd proxy && python -m pytest tests/tasks/test_triggers.py -v
"""

import sys
import time
from datetime import datetime, timezone

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _seed_user(sub: str, username: str = "tester"):
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login, username) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (sub) DO UPDATE SET username=%s",
            (sub, f"{username}@test.com", username, "creator", now, now, username, username),
        )
        conn.commit()


def _create_trigger_task(*, task_id="dyn-trig-1", scope="user", created_by="user-test", agent="agent-x"):
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, agent, "Trigger Task", "Run for {{name}}", "cli",
        "trigger", None, None, None, 600, created_by,
        None, None, None, None, False,
        scope=scope,
    )
    return task_id


# ───────────────────────────────────────────────────────────────────────────
# Trigger_payload plumbing
# ───────────────────────────────────────────────────────────────────────────


def test_build_trigger_payload_normalises_webhook_body(temp_db):
    """trigger_manager builds the structured payload threaded into the task
    session so manifest ``agent_context`` blocks resolve ${trigger.*}.
    """
    from services.scheduler.trigger_manager import _build_trigger_payload
    row = {"slug": "stripe-payment", "scope": "agent", "agent": "billing"}
    body = {"event_type": "charge.succeeded", "amount": 5000, "phone": "+1888"}
    payload = _build_trigger_payload(row, body)
    assert payload["source"] == "webhook"
    assert payload["route"] == "stripe-payment"
    assert payload["did"] == ""
    assert payload["body"] == body  # raw body preserved for ${trigger.body.*}


def test_build_trigger_payload_handles_non_dict_body(temp_db):
    from services.scheduler.trigger_manager import _build_trigger_payload
    row = {"slug": "x", "scope": "user", "agent": "y"}
    # If a webhook ships a JSON array or string, body falls back to {}.
    payload = _build_trigger_payload(row, [1, 2, 3])
    assert payload["body"] == {}


def test_trigger_payload_flows_to_dynamic_context_phone_token(temp_db):
    """Integration: the trigger payload built here is consumed by
    ``dynamic_context._build_trigger_tokens`` (same module). Smoke-test the
    contract end-to-end so a future schema change to either side surfaces
    quickly."""
    from services.scheduler.trigger_manager import _build_trigger_payload
    from services.mcp.dynamic_context import _build_trigger_tokens

    row = {"slug": "support-line", "scope": "agent", "agent": "support"}
    body = {"phone": "+15551234", "email": "alice@x.com"}
    payload = _build_trigger_payload(row, body)
    tokens = _build_trigger_tokens(payload)
    # Flat tokens populated via the normaliser dipping into body.
    assert tokens["trigger.phone"] == "+15551234"
    assert tokens["trigger.email"] == "alice@x.com"
    assert tokens["trigger.source"] == "webhook"
    assert tokens["trigger.route"] == "support-line"


# ───────────────────────────────────────────────────────────────────────────
# Slug + scope validation
# ───────────────────────────────────────────────────────────────────────────


def test_slug_validation_accepts_lowercase_alphanumeric(temp_db):
    from services.scheduler import trigger_manager as tm
    assert tm._validate_slug("github-pr") == "github-pr"
    assert tm._validate_slug("a") == "a"
    assert tm._validate_slug("a1") == "a1"
    assert tm._validate_slug("AGENT-EVT") == "agent-evt"  # lowercased


def test_slug_validation_rejects_invalid(temp_db):
    from services.scheduler import trigger_manager as tm
    bad_slugs = ["", "-leading", "trailing-", "with space", "with_underscore", "x" * 65, "with$symbol"]
    for s in bad_slugs:
        with pytest.raises(tm.TriggerValidationError):
            tm._validate_slug(s)


def test_slug_derivation_from_name(temp_db):
    from services.scheduler import trigger_manager as tm
    assert tm._slugify("My Cool Trigger!") == "my-cool-trigger"
    assert tm._slugify("github-pr") == "github-pr"
    assert tm._slugify("UPPERCASE") == "uppercase"


def test_scope_validation(temp_db):
    from services.scheduler import trigger_manager as tm
    assert tm._validate_scope("user") == "user"
    assert tm._validate_scope("agent") == "agent"
    with pytest.raises(tm.TriggerValidationError):
        tm._validate_scope("global")
    with pytest.raises(tm.TriggerValidationError):
        tm._validate_scope("admin")


# ───────────────────────────────────────────────────────────────────────────
# Cross-scope task linkage (security)
# ───────────────────────────────────────────────────────────────────────────


def test_register_rejects_cross_scope_task_linkage(temp_db):
    """User trigger cannot reference an agent-scoped task."""
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    # Create an AGENT-scoped task
    agent_task = _create_trigger_task(
        task_id="dyn-trig-agent", scope="agent", created_by="agent-x", agent="agent-x",
    )
    with pytest.raises(tm.TriggerValidationError) as e:
        tm.register_trigger(
            name="Cross",
            scope="user",
            agent="agent-x",
            created_by="user-test",
            task_id=agent_task,
        )
    assert "scope" in str(e.value).lower()


def test_register_rejects_cross_user_task_linkage(temp_db):
    """User trigger cannot reference another user's task."""
    _seed_user("user-alice", "alice")
    _seed_user("user-bob", "bob")
    from services.scheduler import trigger_manager as tm
    other_task = _create_trigger_task(
        task_id="dyn-bobs", scope="user", created_by="user-bob", agent="agent-x",
    )
    with pytest.raises(tm.TriggerValidationError) as e:
        tm.register_trigger(
            name="Steal",
            scope="user",
            agent="agent-x",
            created_by="user-alice",
            task_id=other_task,
        )
    assert "creator" in str(e.value).lower() or "user" in str(e.value).lower()


def test_register_rejects_non_trigger_task_type(temp_db):
    """Trigger can only run task_type='trigger' tasks."""
    _seed_user("user-test")
    from storage import database as task_store
    from services.scheduler import trigger_manager as tm
    # Create a regular one_time task
    task_store.create_dynamic_task(
        "dyn-onetime", "agent-x", "OneTime", "do {{x}}", "cli",
        "one_time", None, "2099-01-01T00:00:00", None, 600, "user-test",
        None, None, None, None, False, scope="user",
    )
    with pytest.raises(tm.TriggerValidationError) as e:
        tm.register_trigger(
            name="Bad",
            scope="user",
            agent="agent-x",
            created_by="user-test",
            task_id="dyn-onetime",
        )
    assert "trigger" in str(e.value).lower()


def test_register_rejects_missing_task_id(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    with pytest.raises(tm.TriggerValidationError):
        tm.register_trigger(
            name="Missing",
            scope="user",
            agent="agent-x",
            created_by="user-test",
            task_id="dyn-nonexistent",
        )


# ───────────────────────────────────────────────────────────────────────────
# At-least-one-action invariant
# ───────────────────────────────────────────────────────────────────────────


def test_register_rejects_empty_action(temp_db):
    """No task_id, no notify → reject (prompt_template gone)."""
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    with pytest.raises(tm.TriggerValidationError) as e:
        tm.register_trigger(
            name="Empty",
            scope="user",
            agent="agent-x",
            created_by="user-test",
            notify_enabled=False,
        )
    assert "action" in str(e.value).lower()


def test_register_with_only_notify_succeeds(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="Notify only",
        scope="user",
        agent="agent-x",
        created_by="user-test",
        notify_enabled=True,
        notify_title="Hello",
        notify_body="World",
    )
    assert row["notify_enabled"]
    assert row["task_id"] is None


def test_notify_requires_title_and_body(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    with pytest.raises(tm.TriggerValidationError):
        tm.register_trigger(
            name="No title",
            scope="user",
            agent="agent-x",
            created_by="user-test",
            notify_enabled=True,
            notify_body="just body",
        )
    with pytest.raises(tm.TriggerValidationError):
        tm.register_trigger(
            name="No body",
            scope="user",
            agent="agent-x",
            created_by="user-test",
            notify_enabled=True,
            notify_title="just title",
        )


# ───────────────────────────────────────────────────────────────────────────
# User-scope notify target lockdown
# ───────────────────────────────────────────────────────────────────────────


def test_user_scope_trigger_cannot_target_other_user(temp_db):
    """Privilege escalation prevention: user-scoped triggers may only notify creator."""
    _seed_user("user-alice", "alice")
    _seed_user("user-bob", "bob")
    from services.scheduler import trigger_manager as tm
    with pytest.raises(tm.TriggerValidationError) as e:
        tm.register_trigger(
            name="Steal-notify",
            scope="user",
            agent="agent-x",
            created_by="user-alice",
            notify_enabled=True,
            notify_title="X",
            notify_body="Y",
            notify_target="bob",  # different user
        )
    assert "creator" in str(e.value).lower() or "own" in str(e.value).lower()


def test_user_scope_trigger_can_target_creator_explicitly(temp_db):
    _seed_user("user-alice", "alice")
    from services.scheduler import trigger_manager as tm
    # Pass own user_sub explicitly — accepted, normalised.
    row = tm.register_trigger(
        name="Self",
        scope="user",
        agent="agent-x",
        created_by="user-alice",
        notify_enabled=True,
        notify_title="X",
        notify_body="Y",
        notify_target="user-alice",
    )
    assert row["notify_target"] == "user-alice"


def test_user_scope_trigger_with_null_target_resolves_at_fire(temp_db):
    _seed_user("user-alice")
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="Null target",
        scope="user",
        agent="agent-x",
        created_by="user-alice",
        notify_enabled=True,
        notify_title="X",
        notify_body="Y",
    )
    # NULL on creation; fire path defaults to creator.
    assert row["notify_target"] is None
    assert row["notify_target_scope"] == "user"


# ───────────────────────────────────────────────────────────────────────────
# Slug uniqueness
# ───────────────────────────────────────────────────────────────────────────


def test_slug_uniqueness_within_scope_owner(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    tm.register_trigger(
        name="A", slug="dup",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Same user + same slug → unique violation
    with pytest.raises(Exception):
        tm.register_trigger(
            name="B", slug="dup",
            scope="user", agent="agent-x", created_by="user-test",
            notify_enabled=True, notify_title="X", notify_body="Y",
        )


def test_slug_can_repeat_across_scopes(temp_db):
    """User 'foo' and agent 'foo' coexist — partial unique indexes are scope-aware."""
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    tm.register_trigger(
        name="UserOne", slug="shared",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    tm.register_trigger(
        name="AgentOne", slug="shared",
        scope="agent", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Both succeed.


def test_slug_can_repeat_across_users(temp_db):
    _seed_user("user-alice", "alice")
    _seed_user("user-bob", "bob")
    from services.scheduler import trigger_manager as tm
    tm.register_trigger(
        name="A", slug="shared",
        scope="user", agent="agent-x", created_by="user-alice",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    tm.register_trigger(
        name="B", slug="shared",
        scope="user", agent="agent-x", created_by="user-bob",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )


# ───────────────────────────────────────────────────────────────────────────
# Edit
# ───────────────────────────────────────────────────────────────────────────


def test_edit_can_change_name_and_notify(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="Original",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    ok, err = tm.update_trigger(row["id"], {
        "name": "Renamed",
        "notify_title": "New title",
    })
    assert ok and err is None


def test_edit_rejects_immutable_scope(temp_db):
    """Scope is not in _EDITABLE_TRIGGER_COLUMNS — silently dropped."""
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    from storage.automation import trigger_store
    row = tm.register_trigger(
        name="X",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Caller passes scope=agent — silently ignored (not in editable cols)
    # and the row's scope stays 'user'.
    ok, err = tm.update_trigger(row["id"], {"scope": "agent", "name": "X2"})
    assert ok
    assert trigger_store.get_trigger(row["id"])["scope"] == "user"


def test_edit_rejects_disable_notify_with_no_other_action(temp_db):
    """Disabling notify on a notify-only trigger should fail (no action left)."""
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="X",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    ok, err = tm.update_trigger(row["id"], {"notify_enabled": False})
    assert not ok
    assert err and "action" in err.lower()


def test_edit_change_task_id_validates_scope(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="X",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Try to wire an agent-scoped task — should reject
    agent_task = _create_trigger_task(
        task_id="dyn-agnt", scope="agent", created_by="agent-x", agent="agent-x",
    )
    ok, err = tm.update_trigger(row["id"], {"task_id": agent_task})
    assert not ok
    assert err and "scope" in err.lower()


# ───────────────────────────────────────────────────────────────────────────
# Pause / Resume / Delete
# ───────────────────────────────────────────────────────────────────────────


def test_pause_resume_round_trip(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    from storage.automation import trigger_store
    row = tm.register_trigger(
        name="X",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Pause
    ok, _ = tm.pause_trigger(row["id"])
    assert ok
    assert not trigger_store.get_trigger(row["id"])["enabled"]
    # Resume
    ok, _ = tm.resume_trigger(row["id"])
    assert ok
    assert trigger_store.get_trigger(row["id"])["enabled"]


def test_delete_removes_row(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    from storage.automation import trigger_store
    row = tm.register_trigger(
        name="X",
        scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    ok, _ = tm.delete_trigger(row["id"])
    assert ok
    assert trigger_store.get_trigger(row["id"]) is None


def test_pause_returns_404_for_missing(temp_db):
    from services.scheduler import trigger_manager as tm
    ok, err = tm.pause_trigger("nonexistent")
    assert not ok and err is None


# ───────────────────────────────────────────────────────────────────────────
# Debounce (in-memory state)
# ───────────────────────────────────────────────────────────────────────────


def test_debounce_blocks_immediate_second_fire(temp_db):
    from services.scheduler import trigger_manager as tm
    tid = "test-debounce-1"
    # Reset state
    tm._last_triggered.pop(tid, None)
    # First call passes
    assert tm._check_debounce(tid, 5) is None
    # Immediate second call blocked
    remaining = tm._check_debounce(tid, 5)
    assert remaining is not None and remaining > 0
    # State preserved (not reset by debounced call)
    remaining2 = tm._check_debounce(tid, 5)
    assert remaining2 is not None


def test_debounce_zero_means_no_limit(temp_db):
    from services.scheduler import trigger_manager as tm
    tid = "test-debounce-zero"
    tm._last_triggered.pop(tid, None)
    assert tm._check_debounce(tid, 0) is None
    assert tm._check_debounce(tid, 0) is None
    assert tm._check_debounce(tid, 0) is None


def test_debounce_unblocks_after_window(temp_db, monkeypatch):
    """After debounce_seconds elapse, fire should pass again."""
    from services.scheduler import trigger_manager as tm
    tid = "test-debounce-elapsed"
    tm._last_triggered.pop(tid, None)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    assert tm._check_debounce(tid, 5) is None
    # 6 s later — past the window
    monkeypatch.setattr(time, "monotonic", lambda: base + 6)
    assert tm._check_debounce(tid, 5) is None


# ───────────────────────────────────────────────────────────────────────────
# Placeholder substitution
# ───────────────────────────────────────────────────────────────────────────


def test_substitute_basic(temp_db):
    from services.scheduler import trigger_manager as tm
    out = tm._substitute_placeholders("Hello {{name}}", {"name": "world"})
    assert out == "Hello world"


def test_substitute_multiple(temp_db):
    from services.scheduler import trigger_manager as tm
    out = tm._substitute_placeholders(
        "{{a}}/{{b}}/{{a}}", {"a": "X", "b": "Y"},
    )
    assert out == "X/Y/X"


def test_substitute_missing_key_blank(temp_db):
    """Missing keys substitute to empty string (don't crash)."""
    from services.scheduler import trigger_manager as tm
    out = tm._substitute_placeholders("Hello {{missing}}", {})
    assert out == "Hello "


def test_substitute_handles_none_template(temp_db):
    from services.scheduler import trigger_manager as tm
    assert tm._substitute_placeholders(None, {}) is None


def test_substitute_handles_non_dict_body(temp_db):
    """If caller passes a list/None as body, fall back to empty dict."""
    from services.scheduler import trigger_manager as tm
    out = tm._substitute_placeholders("X {{a}}", "not-a-dict")  # type: ignore
    assert out == "X "


# ───────────────────────────────────────────────────────────────────────────
# Cleanup helpers
# ───────────────────────────────────────────────────────────────────────────


def test_cleanup_user_triggers_removes_user_scoped_only(temp_db):
    _seed_user("user-alice", "alice")
    _seed_user("user-bob", "bob")
    from services.scheduler import trigger_manager as tm
    from storage.automation import trigger_store
    # Alice's user trigger
    a = tm.register_trigger(
        name="Alice", scope="user", agent="agent-x", created_by="user-alice",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Bob's user trigger (different owner)
    b = tm.register_trigger(
        name="Bob", scope="user", agent="agent-x", created_by="user-bob",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    # Agent trigger (alice as creator/manager)
    g = tm.register_trigger(
        name="Agent", scope="agent", agent="agent-x", created_by="user-alice",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )

    deleted = trigger_store.cleanup_user_triggers("user-alice")
    assert deleted == 1
    # Alice's user trigger gone, Bob's intact, agent trigger intact
    assert trigger_store.get_trigger(a["id"]) is None
    assert trigger_store.get_trigger(b["id"]) is not None
    assert trigger_store.get_trigger(g["id"]) is not None


def test_trigger_task_survives_post_fire_cleanup(temp_db):
    """Regression: scheduler._execute_task's finally block hard-deletes
    one-time tasks (no schedule, no interval). Trigger-only tasks must be
    EXEMPT from that cleanup so the same task can be re-fired by its
    wired-up trigger every time the webhook calls.

    This test covers the cleanup-condition logic. The actual cleanup
    happens after a real run inside `_execute_task` — we exercise the
    boolean expression directly to keep the test fast and deterministic.
    """
    from services.scheduler import scheduler
    _seed_user("user-test")
    # Create a trigger-only task and read it back through scheduler's
    # row→TaskDefinition adapter so task_type is set.
    _create_trigger_task(task_id="dyn-persist-test", scope="user",
                         created_by="user-test", agent="agent-x")
    from storage import database as task_store
    row = task_store.get_dynamic_task("dyn-persist-test")
    assert row is not None
    from services.scheduler import task_kinds
    task = scheduler._row_to_task(row)
    assert task.task_type == task_kinds.TRIGGER

    # The retire rule the run's frame asks (task_kinds.self_removes):
    assert not task_kinds.self_removes(task), (
        "trigger-only task would be deleted by post-fire cleanup — "
        "would break trigger ↔ task pairing on second fire"
    )

    # Sanity: a regular one-time task SHOULD be cleaned up.
    task_store.create_dynamic_task(
        "dyn-onetime-cleanup", "agent-x", "OneTime", "do", "cli",
        "one_time", None, "2099-01-01T00:00:00", None, 600, "user-test",
        None, None, None, None, False, scope="user",
    )
    row2 = task_store.get_dynamic_task("dyn-onetime-cleanup")
    task2 = scheduler._row_to_task(row2)
    assert task_kinds.self_removes(task2)


def test_cleanup_agent_triggers_removes_all_for_agent(temp_db):
    _seed_user("user-test")
    from services.scheduler import trigger_manager as tm
    from storage.automation import trigger_store
    a = tm.register_trigger(
        name="X", scope="user", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    b = tm.register_trigger(
        name="Y", scope="agent", agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    c = tm.register_trigger(
        name="Z", scope="user", agent="agent-other", created_by="user-test",
        notify_enabled=True, notify_title="X", notify_body="Y",
    )
    deleted = trigger_store.cleanup_agent_triggers("agent-x")
    assert deleted == 2
    assert trigger_store.get_trigger(a["id"]) is None
    assert trigger_store.get_trigger(b["id"]) is None
    assert trigger_store.get_trigger(c["id"]) is not None


# ───────────────────────────────────────────────────────────────────────────
# Subscription linkage: the refusal says which subscription to create
# ───────────────────────────────────────────────────────────────────────────


def _seed_subscription(*, scope: str, owner: str, agent: str | None) -> str:
    from storage.automation import webhook_subscription_store
    row = webhook_subscription_store.create_subscription(
        scope=scope, owner=owner, agent=agent, mcp_name="github-mcp",
        provider_id="github", account_label="acct", vendor_target="o/r",
        selected_events=["push"], selected_subevents={}, signing_secret="",
        created_by=owner or "user-admin",
    )
    return row["id"]


def _register(*, scope: str, subscription_id: str):
    from services.scheduler import trigger_manager
    return trigger_manager.register_trigger(
        name="On push", scope=scope, agent="agent-x", created_by="user-test",
        notify_enabled=True, notify_title="t", notify_body="b",
        subscription_id=subscription_id,
    )


def test_agent_trigger_on_personal_subscription_points_at_the_agent_surface(temp_db):
    from services.scheduler.trigger_manager import TriggerValidationError
    sid = _seed_subscription(scope="user", owner="user-test", agent=None)
    with pytest.raises(TriggerValidationError) as ei:
        _register(scope="agent", subscription_id=sid)
    msg = str(ei.value)
    assert "personal" in msg
    assert "Agent Settings → MCPs → github-mcp → Subscribe to events for this agent" in msg


def test_user_trigger_on_agent_subscription_points_at_connected_accounts(temp_db):
    from services.scheduler.trigger_manager import TriggerValidationError
    sid = _seed_subscription(scope="service", owner="", agent="agent-x")
    with pytest.raises(TriggerValidationError) as ei:
        _register(scope="user", subscription_id=sid)
    msg = str(ei.value)
    assert "belongs to agent 'agent-x'" in msg
    assert "Subscribe as: Me" in msg


def test_matching_scopes_link(temp_db):
    sid = _seed_subscription(scope="service", owner="", agent="agent-x")
    row = _register(scope="agent", subscription_id=sid)
    assert row["subscription_id"] == sid


def test_a_trigger_made_before_its_subscription_is_bound_later(temp_db):
    """An app's trigger is usually created before the agent's subscription
    exists (found on the internal install: the developer dashboard's
    github trigger sat with no subscription while the events went nowhere).
    An edit binds it under the creation rule; '' unbinds."""
    from services.scheduler import trigger_manager
    from storage.automation import trigger_store
    row = _register(scope="agent", subscription_id=None)
    assert row["subscription_id"] is None
    personal = _seed_subscription(scope="user", owner="user-test", agent=None)
    ok, err = trigger_manager.update_trigger(row["id"], {"subscription_id": personal})
    assert not ok and "Subscribe to events for this agent" in (err or "")
    sid = _seed_subscription(scope="service", owner="", agent="agent-x")
    ok, err = trigger_manager.update_trigger(row["id"], {"subscription_id": sid})
    assert ok and err is None
    assert trigger_store.get_trigger(row["id"])["subscription_id"] == sid
    # The dispatcher's fan-out sees it now.
    assert [t["id"] for t in trigger_store.list_triggers(subscription_id=sid)] == [row["id"]]
    ok, err = trigger_manager.update_trigger(row["id"], {"subscription_id": ""})
    assert ok and trigger_store.get_trigger(row["id"])["subscription_id"] is None


# ───────────────────────────────────────────────────────────────────────────
# Agent-scope inline notify: who it reaches and who may aim it
# ───────────────────────────────────────────────────────────────────────────


def _seed_member(sub: str, agent: str, role: str = "editor"):
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sub, agent) DO NOTHING",
            (sub, agent, now, "user-admin", role),
        )
        conn.commit()


def test_agent_trigger_notify_without_target_reaches_its_agent(temp_db, monkeypatch):
    """The dashboard's create modal and the phone-route button send no target:
    the notify broadcasts to the trigger's agent (TRIGGERS.md "NULL =
    broadcast"), it does not go to nobody."""
    import asyncio
    _seed_user("user-ed", "ed")
    _seed_member("user-ed", "agent-x")
    from services.notifications import notification_manager as nm
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="Deploy", scope="agent", agent="agent-x", created_by="user-ed",
        notify_enabled=True, notify_title="Deployed {{ref}}", notify_body="b",
    )
    assert (row["notify_target_scope"], row["notify_target"]) == ("agent", None)
    sent = []

    async def _capture(**kw):
        sent.append(kw)
        return nm.resolve_targets(kw["scope"], kw["target"])
    monkeypatch.setattr(nm, "fire_notification", _capture)
    out = asyncio.run(tm.fire_trigger(row, {"ref": "v2"}))
    assert out["actions"] == ["notify"], out
    assert (sent[0]["scope"], sent[0]["target"]) == ("agent", "agent-x")
    assert sent[0]["title"] == "Deployed v2"
    assert "user-ed" in nm.resolve_targets(sent[0]["scope"], sent[0]["target"])


def test_agent_trigger_notify_reach_follows_the_notifications_api(temp_db):
    """An agent-scoped trigger's notify reaches its own agent's users (all of
    them, or one member); another agent, a non-member or every user on the
    platform needs a platform admin — the rules POST /v1/notifications keeps."""
    _seed_user("user-ed", "ed")
    _seed_member("user-ed", "agent-x")
    _seed_user("user-mem", "mem")
    _seed_member("user-mem", "agent-x", "viewer")
    _seed_user("user-out", "outsider")
    _seed_member("user-out", "agent-y")
    long_sub = "oidc-" + "0" * 40
    _seed_user(long_sub, "longsub")
    from services.scheduler import trigger_manager as tm

    def reg(slug, admin=False, **notify):
        extra = {"caller_is_admin": True} if admin else {}
        return tm.register_trigger(
            name=slug, slug=slug, scope="agent", agent="agent-x", created_by="user-ed",
            notify_enabled=True, notify_title="t", notify_body="{{msg}}", **notify, **extra)

    for i, notify in enumerate((
        {"notify_target_scope": "global"},
        {"notify_target_scope": "agent", "notify_target": "agent-y"},
        {"notify_target_scope": "user", "notify_target": "outsider"},
        {"notify_target_scope": "user", "notify_target": long_sub},
    )):
        with pytest.raises(tm.TriggerValidationError):
            reg(f"bad-{i}", **notify)
    assert reg("own", notify_target_scope="agent", notify_target="agent-x")["notify_target"] == "agent-x"
    assert reg("member", notify_target_scope="user", notify_target="mem")["notify_target"] == "user-mem"
    assert reg("self", notify_target_scope="user", notify_target="ed")["notify_target"] == "user-ed"
    everyone = reg("everyone", admin=True, notify_target_scope="global")
    assert (everyone["notify_target_scope"], everyone["notify_target"]) == ("global", None)
    assert reg("out", admin=True, notify_target_scope="user",
               notify_target="outsider")["notify_target"] == "user-out"


def test_user_trigger_notify_stays_with_its_creator(temp_db):
    """A user-scoped trigger notifies its creator whatever scope is asked for."""
    _seed_user("user-alice", "alice")
    from services.scheduler import trigger_manager as tm
    row = tm.register_trigger(
        name="Mine", scope="user", agent="agent-x", created_by="user-alice",
        notify_enabled=True, notify_title="t", notify_body="b",
        notify_target_scope="global",
    )
    assert (row["notify_target_scope"], row["notify_target"]) == ("user", None)


def test_agent_trigger_notify_reach_is_checked_on_edit(temp_db):
    """An edit that aims the notify somewhere new meets the create rule; one
    that leaves the target where it is (a rename, a new title) passes."""
    _seed_user("user-ed", "ed")
    _seed_member("user-ed", "agent-x")
    from services.scheduler import trigger_manager as tm
    from storage.automation import trigger_store
    row = tm.register_trigger(
        name="Deploy", scope="agent", agent="agent-x", created_by="user-ed",
        notify_enabled=True, notify_title="t", notify_body="b",
    )
    ok, err = tm.update_trigger(row["id"], {"notify_target_scope": "global"})
    assert not ok and "admin" in (err or ""), err
    ok, err = tm.update_trigger(
        row["id"], {"notify_target_scope": "agent", "notify_target": "agent-y"})
    assert not ok and err, err
    ok, err = tm.update_trigger(row["id"], {"notify_target_scope": "global"},
                                caller_is_admin=True)
    assert ok and err is None
    ok, err = tm.update_trigger(row["id"], {
        "name": "Deploy 2", "notify_enabled": True, "notify_title": "t2"})
    assert ok and err is None
    assert trigger_store.get_trigger(row["id"])["notify_target_scope"] == "global"


def test_trigger_routes_hold_the_caller_to_the_notify_reach(temp_db):
    """Create and edit judge the target by the caller: an editor or the
    agent's own no-user session cannot aim a trigger's notify at every user;
    a manager may rename an admin's global trigger but not re-aim it."""
    import asyncio
    from fastapi import HTTPException
    from auth.providers import UserContext
    from api.events import triggers as api
    from storage.agents import agent_store
    agent_store.create_agent("agent-x", "X", created_by="user-admin")
    _seed_user("user-ed", "ed")
    _seed_member("user-ed", "agent-x")
    _seed_member("user-manager", "agent-x", "manager")
    editor = UserContext(sub="user-ed", email="", name="ed", role="creator",
                         agents=["agent-x"], agent_roles={"agent-x": "editor"})
    manager = UserContext(sub="user-manager", email="", name="m", role="creator",
                          agents=["agent-x"], agent_roles={"agent-x": "manager"})
    no_user = UserContext(sub="session:s-1", email="", name="", role="agent",
                          is_api_key=True, session_id="s-1", agent="agent-x")
    admin = UserContext(sub="user-admin", email="", name="a", role="admin")

    def req(slug):
        return api.CreateTriggerRequest(
            name=slug, slug=slug, scope="agent", agent="agent-x",
            notify=api.NotifyConfig(enabled=True, title="t", body="{{msg}}",
                                    target_scope="global"))

    for i, ctx in enumerate((editor, no_user)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(api.create_trigger_endpoint(req(f"g-{i}"), user=ctx))
        assert e.value.status_code == 400, e.value.detail
    tid = asyncio.run(api.create_trigger_endpoint(req("g-admin"), user=admin))["trigger"]["id"]

    def edit(ctx, **fields):
        return asyncio.run(api._edit_impl(tid, api.EditTriggerRequest(**fields), ctx))

    assert edit(manager, name="Everyone 2", notify_enabled=True, notify_title="t2")["status"] == "updated"
    with pytest.raises(HTTPException) as e:
        edit(manager, notify_target_scope="agent", notify_target="agent-y")
    assert e.value.status_code == 400
    assert edit(manager, notify_target_scope="agent")["status"] == "updated"
    with pytest.raises(HTTPException) as e:
        edit(manager, notify_target_scope="global")
    assert e.value.status_code == 400


# ───────────────────────────────────────────────────────────────────────────
# By-id fire and mutations: the edit authority, sessions on their own agent
# ───────────────────────────────────────────────────────────────────────────


def _by_id_world():
    from auth.providers import UserContext
    from storage.agents import agent_store
    from storage.automation import trigger_store
    agent_store.create_agent("agent-x", "X", created_by="user-admin")
    agent_store.create_agent("agent-y", "Y", created_by="user-admin")
    for sub, role in (("user-v", "viewer"), ("user-c", "contributor"),
                      ("user-e", "editor"), ("user-m", "manager")):
        _seed_user(sub, sub)
        _seed_member(sub, "agent-x", role)
    _seed_member("user-m", "agent-y", "manager")
    tx = trigger_store.create_trigger(
        slug="deploy", name="Deploy", scope="agent", agent="agent-x", created_by="user-admin",
        notify_enabled=True, notify_title="t", notify_body="b", notify_target_scope="agent")
    ty = trigger_store.create_trigger(
        slug="deploy", name="Deploy", scope="agent", agent="agent-y", created_by="user-admin",
        notify_enabled=True, notify_title="t", notify_body="b", notify_target_scope="agent")

    def cookie(sub, role):
        return UserContext(sub=sub, email="", name=sub, role="creator",
                           agents=["agent-x"], agent_roles={"agent-x": role})

    def session(sub, agent, platform_role="creator"):
        roles_ = {"agent-x": "manager", "agent-y": "manager"}
        return UserContext(sub=sub, email="", name=sub, role=platform_role,
                           agents=list(roles_), agent_roles=roles_,
                           is_api_key=True, session_id="s-1", agent=agent)
    return tx, ty, cookie, session


class _FireReq:
    async def json(self):
        return {"msg": "hi"}


def test_firing_a_trigger_by_id_needs_the_edit_authority(temp_db, monkeypatch):
    """Firing runs the agent's task and sends its notify with the caller's
    body: a viewer or contributor may not, an editor only on their own, a
    manager may; a user-backed session only on its own agent. ``can_fire``
    says the same."""
    import asyncio
    from fastapi import HTTPException
    from api.events import triggers as api
    tx, ty, cookie, session = _by_id_world()
    fired = []

    async def _fire(row, body, **kw):
        fired.append(row["id"])
        return {"status": "ok"}
    monkeypatch.setattr(api.trigger_manager, "fire_trigger", _fire)

    def fire(row, ctx):
        return asyncio.run(api.fire_test_endpoint(row["id"], _FireReq(), user=ctx))

    for ctx in (cookie("user-v", "viewer"), cookie("user-c", "contributor"),
                cookie("user-e", "editor"), session("user-m", "agent-y")):
        with pytest.raises(HTTPException) as e:
            fire(tx, ctx)
        assert e.value.status_code == 403
        assert api._decorate_for_user(tx, ctx)["can_fire"] is False
    assert fired == []
    for ctx in (cookie("user-m", "manager"), session("user-m", "agent-x")):
        assert fire(tx, ctx)["status"] == "ok"
        assert api._decorate_for_user(tx, ctx)["can_fire"] is True
    assert fire(ty, session("user-m", "agent-y"))["status"] == "ok"
    assert fired == [tx["id"], tx["id"], ty["id"]]


def test_a_user_backed_session_manages_only_its_own_agents_triggers(temp_db):
    """Edit, pause, resume and delete by id pin a session to its own agent,
    as create does — a manager's (or an admin's) session included; a no-user
    session keeps its own agent's triggers."""
    import asyncio
    from fastapi import HTTPException
    from auth.providers import UserContext
    from api.events import triggers as api
    from storage.automation import trigger_store
    tx, ty, _cookie, session = _by_id_world()
    for ctx in (session("user-m", "agent-y"), session("user-admin", "agent-y", "admin")):
        assert api._can_manage_trigger(tx, ctx) is False
        with pytest.raises(HTTPException) as e:
            api._check_trigger_mutation_authority(tx, ctx)
        assert e.value.status_code == 403
        for call in (
            lambda: api._edit_impl(tx["id"], api.EditTriggerRequest(name="x"), ctx),
            lambda: api.pause_trigger_endpoint(tx["id"], user=ctx),
            lambda: api.resume_trigger_endpoint(tx["id"], user=ctx),
            lambda: api.delete_trigger_endpoint(tx["id"], user=ctx),
        ):
            with pytest.raises(HTTPException) as e:
                asyncio.run(call())
            assert e.value.status_code == 403
        assert api._decorate_for_user(tx, ctx)["can_edit"] is False
    assert trigger_store.get_trigger(tx["id"])["name"] == "Deploy"
    own = session("user-m", "agent-y")
    assert asyncio.run(api.pause_trigger_endpoint(ty["id"], user=own))["status"] == "paused"
    no_user = UserContext(sub="session:s-2", email="", name="", role="agent",
                          is_api_key=True, session_id="s-2", agent="agent-y")
    assert asyncio.run(api.resume_trigger_endpoint(ty["id"], user=no_user))["status"] == "resumed"
    assert asyncio.run(api._edit_impl(
        ty["id"], api.EditTriggerRequest(name="Renamed"), no_user))["status"] == "updated"


# ───────────────────────────────────────────────────────────────────────────
# The webhook body read under the streamed body cap
# ───────────────────────────────────────────────────────────────────────────


def _request(receive):
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "POST", "path": "/v1/webhooks/agent/a/s",
        "headers": [], "query_string": b"",
    }
    return Request(scope, receive)


def test_safe_json_re_raises_a_client_disconnect():
    """A body the middleware cut at its tier reaches the route as a
    disconnect; the fire must not proceed with an empty payload."""
    import asyncio
    from starlette.requests import ClientDisconnect
    from api.events import triggers

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        asyncio.run(triggers._safe_json(_request(receive)))


def test_safe_json_tolerates_a_malformed_body():
    import asyncio
    from api.events import triggers

    async def receive():
        return {"type": "http.request", "body": b"not json", "more_body": False}

    assert asyncio.run(triggers._safe_json(_request(receive))) == {}
