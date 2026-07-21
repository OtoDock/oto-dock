"""Tests for task notification_mode (auto / manual / none).

Covers:
- Required field validation on API request models (Pydantic Literal)
- Mode-specific success/failure notification firing in scheduler._run_task
- Prompt-injection contract (_notification_policy_block in task_config_builder)
- Edit endpoint accepts a partial update of notification_mode

Run: cd proxy && python -m pytest tests/session/test_notification_mode.py -v
"""

import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ───────────────────────────────────────────────────────────────────────────
# API request-model validation (FastAPI returns 422 on Pydantic failure)
# ───────────────────────────────────────────────────────────────────────────


class TestRequestModelValidation:
    def test_create_scheduled_missing_notification_mode_fails(self):
        from pydantic import ValidationError
        from api.tasks.tasks import CreateScheduledTaskRequest

        with pytest.raises(ValidationError) as exc:
            CreateScheduledTaskRequest(
                name="t", agent="a", prompt="p", schedule="* * * * *",
            )
        assert "notification_mode" in str(exc.value)

    def test_create_one_time_missing_notification_mode_fails(self):
        from pydantic import ValidationError
        from api.tasks.tasks import CreateOneTimeTaskRequest

        with pytest.raises(ValidationError) as exc:
            CreateOneTimeTaskRequest(
                name="t", agent="a", prompt="p", run_at="2030-01-01T00:00:00",
            )
        assert "notification_mode" in str(exc.value)

    def test_create_scheduled_invalid_notification_mode_fails(self):
        from pydantic import ValidationError
        from api.tasks.tasks import CreateScheduledTaskRequest

        with pytest.raises(ValidationError):
            CreateScheduledTaskRequest(
                name="t", agent="a", prompt="p", schedule="* * * * *",
                notification_mode="always",
            )

    def test_create_scheduled_accepts_each_valid_mode(self):
        from api.tasks.tasks import CreateScheduledTaskRequest

        for mode in ("auto", "manual", "none"):
            req = CreateScheduledTaskRequest(
                name="t", agent="a", prompt="p", schedule="* * * * *",
                notification_mode=mode,
            )
            assert req.notification_mode == mode

    def test_edit_task_notification_mode_optional(self):
        """EditTaskRequest leaves notification_mode unset when omitted (partial update)."""
        from api.tasks.tasks import EditTaskRequest

        req = EditTaskRequest(name="new name")
        assert req.notification_mode is None
        req = EditTaskRequest(notification_mode="none")
        assert req.notification_mode == "none"

    def test_edit_task_invalid_notification_mode_fails(self):
        from pydantic import ValidationError
        from api.tasks.tasks import EditTaskRequest

        with pytest.raises(ValidationError):
            EditTaskRequest(notification_mode="always")


# ───────────────────────────────────────────────────────────────────────────
# Prompt-injection: _notification_policy_block
# ───────────────────────────────────────────────────────────────────────────


class TestPromptInjection:
    def test_auto_mode_tells_agent_not_to_notify(self):
        from core.config.task_config_builder import _notification_policy_block

        block = _notification_policy_block("auto")
        assert "Notification Policy" in block
        assert "Do NOT call" in block
        # Sanity: don't accidentally tell it the opposite
        assert "you to call" not in block.lower()

    def test_manual_mode_tells_agent_to_notify(self):
        from core.config.task_config_builder import _notification_policy_block

        block = _notification_policy_block("manual")
        assert "Notification Policy" in block
        assert "create_notification" in block
        assert "exactly **one**" in block

    def test_none_mode_tells_agent_to_stay_silent(self):
        from core.config.task_config_builder import _notification_policy_block

        block = _notification_policy_block("none")
        assert "Notification Policy" in block
        assert "silently" in block.lower()
        assert "Do NOT call" in block

    def test_unknown_mode_returns_empty(self):
        from core.config.task_config_builder import _notification_policy_block

        assert _notification_policy_block("anything") == ""
        assert _notification_policy_block("") == ""


# ───────────────────────────────────────────────────────────────────────────
# scheduler fire paths — auto/manual/none success + failure behaviour
# ───────────────────────────────────────────────────────────────────────────


def _create_task(task_id, mode):
    """Persist a one-time task row with the given notification_mode."""
    from storage import database as task_store
    task_store.create_dynamic_task(
        task_id, "support-bot", f"Task {task_id}", "do thing", "cli",
        "one_time", None, None, 1, 600,
        "user-1", None, None, None, None, False,
        scope="user",
        notification_mode=mode,
    )


class TestPersistenceRoundTrip:
    def test_row_to_task_reads_notification_mode(self, temp_db):
        from services.scheduler import scheduler

        _create_task("t-auto", "auto")
        row = temp_db.get_dynamic_task("t-auto")
        task = scheduler._row_to_task(row)
        assert task.notification_mode == "auto"

    def test_each_mode_roundtrips(self, temp_db):
        from services.scheduler import scheduler

        for mode in ("auto", "manual", "none"):
            tid = f"t-{mode}"
            _create_task(tid, mode)
            row = temp_db.get_dynamic_task(tid)
            assert row["notification_mode"] == mode
            assert scheduler._row_to_task(row).notification_mode == mode

    def test_invalid_mode_rejected_by_check_constraint(self, temp_db):
        """The CHECK constraint on dynamic_tasks should refuse anything outside the enum."""
        import psycopg
        from storage.pg import get_conn

        with pytest.raises(psycopg.errors.CheckViolation):
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO dynamic_tasks "
                    "(id, agent, name, prompt, llm_mode, task_type, created_at, "
                    "notification_mode) "
                    "VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s)",
                    ("bad", "a", "n", "p", "cli", "one_time", "always"),
                )
                conn.commit()

    def test_editable_columns_includes_notification_mode(self):
        from storage.database import _EDITABLE_TASK_COLUMNS

        assert "notification_mode" in _EDITABLE_TASK_COLUMNS
        assert "notify_on_complete" not in _EDITABLE_TASK_COLUMNS

    def test_update_dynamic_task_changes_mode(self, temp_db):
        from storage import database as task_store

        _create_task("t-edit", "auto")
        ok = task_store.update_dynamic_task("t-edit", {"notification_mode": "none"})
        assert ok is True
        assert temp_db.get_dynamic_task("t-edit")["notification_mode"] == "none"


