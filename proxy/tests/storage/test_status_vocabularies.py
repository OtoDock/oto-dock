"""The status vocabularies (core-seams phase 8): the three leaves, the
stores' constants, the questions generic code asks, the validators, and
the dashboard mirrors under ``lib/status/`` in lock-step (read by regex, the
way ``tests/remote/test_placement.py`` reads ``placement.ts``).

Every stored and wire spelling is frozen — the assertions here compare the
strings, never rename them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from services.apps import app_deploy, app_supervisor
from services.delegation import lane_status
from services.mcp import docker_manager
from services.remote import remote_status
from storage import db_app_deliveries, db_apps, remote_store
from storage.automation import run_status, webhook_subscription_store
from storage.billing import subscription_status, subscription_store
from storage.chat import meeting_status
from storage.mcp import mcp_request_store
from tests._paths import PROXY_DIR, REPO_ROOT
from ws import chat_phase

_STATUS_DIR = REPO_ROOT / "dashboard" / "src" / "lib" / "status"


# ---------------------------------------------------------------------------
# the three leaves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module, loaded", [
    ("storage.automation.run_status", ["storage", "storage.automation", "storage.automation.run_status"]),
    ("ws.chat_phase", ["ws", "ws.chat_phase"]),
    ("storage.chat.meeting_status", ["storage", "storage.chat", "storage.chat.meeting_status"]),
])
def test_the_leaf_imports_nothing_of_the_tree(module, loaded):
    script = (f"import sys, json\nimport {module}\n"
              "print(json.dumps(sorted(m for m in sys.modules if m.startswith("
              "('core', 'services', 'storage', 'auth', 'config', 'ws', 'api')))))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(PROXY_DIR),
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == loaded


# ---------------------------------------------------------------------------
# the task run and the delegate result
# ---------------------------------------------------------------------------

def test_the_run_vocabulary_and_its_questions():
    assert run_status.STATUSES == {"pending", "running", "completed", "failed", "cancelled", "limit_exceeded"}
    assert run_status.LIVE == {"pending", "running"}
    assert run_status.TERMINAL == {"completed", "failed", "cancelled", "limit_exceeded"}
    assert run_status.LIVE | run_status.TERMINAL == run_status.STATUSES
    for s in run_status.LIVE:
        assert run_status.is_live(s) and not run_status.is_terminal(s)
    for s in run_status.TERMINAL:
        assert run_status.is_terminal(s) and not run_status.is_live(s)
    # timeout was never written by anything: not a member, not terminal
    assert "timeout" not in run_status.STATUSES
    assert not run_status.is_terminal("timeout") and not run_status.is_live("timeout")
    assert not run_status.is_live(None) and not run_status.is_terminal(None)


def test_the_delegate_result_is_the_ends_plus_the_user_stop():
    assert run_status.USER_INTERRUPTED == "user_interrupted"
    assert run_status.DELEGATE_RESULTS == {"completed", "failed", "cancelled", "user_interrupted"}
    # never a stored run status
    assert run_status.USER_INTERRUPTED not in run_status.STATUSES


# ---------------------------------------------------------------------------
# the chat phase
# ---------------------------------------------------------------------------

def test_the_chat_phase_sets():
    assert chat_phase.WIRE_PHASES == {"streaming", "ready"}
    assert chat_phase.ACTIVE_ROW_PHASES == {"streaming", "warming", "finished"}


def test_the_emitter_refuses_a_word_outside_the_frame():
    from services.notifications import notification_manager
    with pytest.raises(ValueError):
        notification_manager.broadcast_chat_status("u", "chat-1", "warming", agent="a")
    with pytest.raises(ValueError):
        notification_manager.broadcast_chat_status("u", "chat-1", "idle", agent="a")
    # a legal word with no chat id is a no-op, never a raise
    notification_manager.broadcast_chat_status("u", "", chat_phase.READY, agent="a")


# ---------------------------------------------------------------------------
# the meeting
# ---------------------------------------------------------------------------

def test_the_meeting_vocabulary_its_sets_and_its_table():
    ms = meeting_status
    assert ms.STATUSES == {"pending", "active", "paused", "concluding", "concluded", "failed"}
    assert ms.LIVE == {"pending", "active", "concluding"}
    assert ms.TERMINAL == {"concluded", "failed"}
    assert ms.ROUND_STOPS == {"concluding", "failed"}
    assert ms.ENDABLE == {"active", "concluding", "paused"}
    assert ms.LEAVABLE == {"active", "paused"}
    assert ms.REQUEST_ENDABLE == {"active", "concluding"} and ms.REQUEST_LEAVABLE == {"active"}
    assert ms.PROPOSABLE == {"active"}
    assert set(ms.TRANSITIONS) == ms.STATUSES
    for end in ms.TERMINAL:
        assert ms.TRANSITIONS[end] == frozenset()
        assert ms.is_terminal(end)
    # the writers' transitions (the plan's machine table) are in the table
    assert ms.ACTIVE in ms.TRANSITIONS[ms.PENDING] and ms.FAILED in ms.TRANSITIONS[ms.PENDING]
    assert {ms.PAUSED, ms.CONCLUDING, ms.CONCLUDED, ms.FAILED} <= ms.TRANSITIONS[ms.ACTIVE]
    assert {ms.ACTIVE, ms.CONCLUDED, ms.CONCLUDING} <= ms.TRANSITIONS[ms.PAUSED]
    assert {ms.CONCLUDED, ms.FAILED} <= ms.TRANSITIONS[ms.CONCLUDING]
    # every target of every transition is a member
    for targets in ms.TRANSITIONS.values():
        assert targets <= ms.STATUSES


def test_update_meeting_refuses_a_word_outside_the_set():
    from storage.chat import db_meetings
    with pytest.raises(ValueError):
        db_meetings.update_meeting("m-1", status="cancelled")


# ---------------------------------------------------------------------------
# the stores' constants and validators
# ---------------------------------------------------------------------------

def test_the_mcp_request_vocabulary():
    s = mcp_request_store
    assert s.OPEN_STATES == ("pending", "approved", "installing", "install_failed")
    assert s.TERMINAL_STATES == ("installed", "rejected", "cancelled")
    assert s.STATUSES == set(s.OPEN_STATES) | set(s.TERMINAL_STATES)
    assert s.APPROVABLE == ("pending", "install_failed")
    assert set(s._ALLOWED_TRANSITIONS) <= s.STATUSES
    for targets in s._ALLOWED_TRANSITIONS.values():
        assert targets <= s.STATUSES


def test_the_webhook_subscription_vocabulary():
    s = webhook_subscription_store
    assert s.STATUSES == {"creating", "active", "failed", "renew_failed", "expired", "disabled"}
    assert s.RECEIVING == {"active", "renew_failed", "creating"}
    assert s.DELIVERING == {"active", "renew_failed"}
    assert set(s._ALLOWED_TRANSITIONS) == s.STATUSES
    for targets in s._ALLOWED_TRANSITIONS.values():
        assert targets <= s.STATUSES


def test_the_engine_subscription_vocabulary_and_its_validator():
    assert subscription_status.STATUSES == {"active", "disabled", "expired"}
    with pytest.raises(ValueError):
        subscription_store.update_subscription("sub-1", status="paused")


def test_the_engine_subscription_vocabulary_survives_a_mocked_store():
    """The suite stands a MagicMock in for ``subscription_store`` at more
    than a hundred sites; a reader must spell the leaf, which no such mock
    shadows."""
    from unittest.mock import patch

    from services.engines import subscription_pool

    with patch.object(subscription_pool, "subscription_store"):
        assert subscription_pool.subscription_status.ACTIVE == "active"
    for path in ("api/admin/execution_layers.py", "services/engines/subscription_pool.py",
                 "services/infra/subscription_health.py", "services/infra/subscription_window_alerts.py",
                 "api/auth/claude_oauth.py", "api/auth/openai_oauth.py", "api/agents/discovery.py",
                 "services/engines/subscription_windows.py", "services/engines/token_fanout.py",
                 "storage/billing/subscription_store.py"):
        text = (PROXY_DIR / path).read_text(encoding="utf-8")
        assert not re.search(r"subscription_store\.(ACTIVE|DISABLED|EXPIRED|STATUSES)\b", text), path
        assert not re.search(r"\b_store\.(ACTIVE|DISABLED|EXPIRED|STATUSES)\b", text), path


def test_the_app_server_vocabulary():
    s = app_supervisor
    assert s.STATES == {"stopped", "starting", "up", "backoff", "quota_full", "static", "unapproved", "secrets"}
    assert s.SERVING == {"up", "static"} and s.HELD == {"backoff", "quota_full"}
    assert s.SHIM_FAILED == "failed" and s.SHIM_FAILED not in s.STATES
    assert s.AppUnavailable("x").state == s.BACKOFF
    assert s.Instance.__dataclass_fields__["state"].default == s.STOPPED


def test_the_app_server_shim_words():
    """The runtime shim is JavaScript text served by ``api/apps/apps.py``;
    its three state words are bound here to the supervisor's."""
    src = (PROXY_DIR / "api" / "apps" / "apps.py").read_text(encoding="utf-8")
    default = re.search(r"X-OtoDock-Server'\)\s*\|\|\s*'(\w+)'", src)
    after = re.search(r"r\.status === 503 \? '(\w+)' : '(\w+)'", src)
    assert default and after, "the shim's server_status words moved"
    assert default.group(1) == app_supervisor.STARTING
    assert after.group(1) == app_supervisor.SHIM_FAILED
    assert after.group(2) == app_supervisor.UP


def test_the_delivery_vocabulary_and_its_validator():
    d = db_app_deliveries
    assert d.STATUSES == {"pending", "inflight", "done", "dead"}
    assert d.ACTIVE == {"pending", "inflight"} and d.VERDICTS == {"done", "dead"}
    assert d.GONE == "gone" and d.GONE not in d.STATUSES
    with pytest.raises(ValueError):
        d.finish("d-1", "pending")
    with pytest.raises(ValueError):
        d.finish_step("d-1", "inflight", exit_code=0, output="")
    from services.apps import app_handlers
    assert app_handlers.RUN_STATUS_OF == {"done": run_status.COMPLETED, "dead": run_status.FAILED}
    assert app_handlers.HOLD_UNAPPROVED == "unapproved"


def test_the_deploy_answer_and_the_deploy_state():
    assert app_deploy.RESULTS == {"ok", "refused", "pending approval", "rejected"}
    assert db_apps.DEPLOY_STATES == {"idle", "pending"}
    with pytest.raises(ValueError):
        db_apps.set_deploy_state("app-1", deploy_state="approved")


def test_the_container_vocabulary():
    d = docker_manager
    assert d.STATUSES == {"running", "unhealthy", "starting", "stopped", "not_found", "error",
                          "not_checked", "unknown"}
    assert d.PRESENT == {"running", "starting", "unhealthy"}
    assert d.STARTED == {"running", "starting"}
    assert (d.ENABLE_STARTED, d.ENABLE_FAILED) == ("started", "failed")


def test_the_machine_vocabularies():
    assert remote_status.STATES == {"online", "stale", "paused", "disconnected", "never_connected"}
    assert remote_status.REACHABLE == {"online", "stale"}
    assert (remote_store.STATUS_OFFLINE, remote_store.STATUS_ONLINE, remote_store.STATUS_DISCONNECTED) \
        == ("offline", "online", "disconnected")


# ---------------------------------------------------------------------------
# the dashboard mirrors
# ---------------------------------------------------------------------------

def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def _mirror(name: str) -> str:
    return (_STATUS_DIR / name).read_text(encoding="utf-8")


def test_the_run_mirror():
    text = _mirror("run.ts")
    assert set(_ts_const_strings(text, "RUN_STATUS")) == run_status.STATUSES
    assert len(_ts_const_strings(text, "RUN_STATUS")) == len(run_status.STATUSES)
    assert set(_ts_const_strings(text, "DELEGATE_RESULT")) == run_status.DELEGATE_RESULTS
    assert "export function isLiveRunStatus(" in text


def test_the_chat_mirror():
    text = _mirror("chat.ts")
    words = _ts_const_strings(text, "CHAT_PHASE")
    # one union: the frame's words, the active rows' words, the client's own
    assert set(words) == chat_phase.WIRE_PHASES | chat_phase.ACTIVE_ROW_PHASES | {"idle", "failed"}
    assert len(words) == 6
    assert _ts_const_strings(text, "LANE_STATUS") == [
        lane_status.STATUS_GENERATING, lane_status.STATUS_AWAITING_USER, lane_status.STATUS_IDLE]
    for fn in ("isLiveChatPhase", "laneStatusOf"):
        assert f"export function {fn}(" in text, fn
    for t in ("ChatPhase", "ChatStreamPhase", "LiveChatPhase", "ActiveRowPhase", "LaneStatus"):
        assert f"export type {t}" in text, t


def test_the_meeting_mirror():
    words = _ts_const_strings(_mirror("meeting.ts"), "MEETING_STATUS")
    assert set(words) == meeting_status.STATUSES and len(words) == len(meeting_status.STATUSES)


def test_the_mcp_request_mirror():
    words = _ts_const_strings(_mirror("mcpRequest.ts"), "MCP_REQUEST_STATUS")
    assert set(words) == mcp_request_store.STATUSES and len(words) == len(mcp_request_store.STATUSES)


def test_the_webhook_subscription_mirror():
    words = _ts_const_strings(_mirror("webhookSubscription.ts"), "WEBHOOK_SUBSCRIPTION_STATUS")
    assert set(words) == webhook_subscription_store.STATUSES and len(words) == 6


def test_the_engine_subscription_mirror():
    words = _ts_const_strings(_mirror("engineSubscription.ts"), "ENGINE_SUBSCRIPTION_STATUS")
    assert set(words) == subscription_status.STATUSES and len(words) == 3


def test_the_app_server_mirror():
    text = _mirror("appServer.ts")
    words = _ts_const_strings(text, "APP_SERVER_STATE")
    assert set(words) == app_supervisor.STATES | {app_supervisor.SHIM_FAILED} and len(words) == 9
    assert _ts_const_strings(text, "FRAME_STATE") == ["connecting", "unreachable"]


def test_the_deploy_state_mirror():
    words = _ts_const_strings(_mirror("appDeploy.ts"), "DEPLOY_STATE")
    assert set(words) == db_apps.DEPLOY_STATES and len(words) == 2


def test_the_docker_mirror():
    text = _mirror("docker.ts")
    words = _ts_const_strings(text, "DOCKER_STATUS")
    assert set(words) == docker_manager.STATUSES and len(words) == 8
    assert _ts_const_strings(text, "ENABLE_DOCKER") == [docker_manager.ENABLE_STARTED,
                                                        docker_manager.ENABLE_FAILED]


def test_the_machine_mirror():
    text = _mirror("machine.ts")
    words = _ts_const_strings(text, "MACHINE_STATE")
    assert set(words) == remote_status.STATES and len(words) == 5
    # the column's offline never reaches the dashboard
    assert remote_store.STATUS_OFFLINE not in words
    assert "export function isReachableMachine(" in text
