"""Characterization of the session builders: the built config (or the
refusal) and every collaborator call they make, for the chat, task, meeting,
delivery and phone builders. The goldens were written from the tree before
the builders' store reads left the event loop (lane LOOP of 1.9.0); a drift
is a behaviour change to explain, never a golden to rewrite."""
from types import SimpleNamespace

import pytest

from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.loop_builder_rig import (
    AGENT,
    SHARED,
    install,
    normalize,
    outcome,
    run,
)


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


def _user(sub: str) -> dict:
    from storage import database as db
    return db.get_user(sub)


def _chat(agent: str, sub: str, role: str, **kw):
    from core.config.config_builder import build_agent_config
    return lambda: build_agent_config(
        agent, _user(sub), sub, role, client_type="dashboard",
        session_id=kw.pop("session_id", "s-chat"), **kw,
    )


def _task(scope: str, created_by: str, agent: str = AGENT, **kw):
    from core.config.task_config_builder import build_task_agent_config
    task = SimpleNamespace(scope=scope, created_by=created_by, task_type="scheduled",
                           notification_mode="manual", name="t", id="dyn-char", **kw)
    return lambda: build_task_agent_config(agent, task, "s-task")


class TestChatBuilder:
    def test_admin(self, rig):
        check_golden("builder-chat-admin", outcome(rig, _chat(AGENT, "user-admin", "admin")))

    def test_member_editor(self, rig):
        check_golden("builder-chat-member", outcome(rig, _chat(AGENT, "user-viewer", "editor")))

    def test_task_rewarm(self, rig):
        from core.config.task_config_builder import TaskIdentity
        ident = TaskIdentity(username="vic", role="editor", scope="user", creds_user_sub="user-viewer")
        check_golden("builder-chat-task-rewarm",
                     outcome(rig, _chat(AGENT, "user-admin", "admin", task_identity=ident)))

    def test_shared_only_viewer_is_refused(self, rig):
        check_golden("builder-chat-shared-viewer", outcome(rig, _chat(SHARED, "user-viewer", "viewer")))


class TestTaskBuilder:
    def test_agent_scope(self, rig):
        check_golden("builder-task-agent", outcome(rig, _task("agent", "user-admin")))

    def test_user_scope(self, rig):
        check_golden("builder-task-user", outcome(rig, _task("user", "user-viewer")))

    def test_shared_only_viewer_task_is_refused_after_the_mcp_build(self, rig):
        check_golden("builder-task-shared-viewer",
                     outcome(rig, _task("user", "user-viewer", agent=SHARED)))


class TestMeetingBuilder:
    @pytest.mark.parametrize("scope,creator", [("user", "user-viewer"), ("agent", "user-manager")])
    def test_scopes(self, rig, scope, creator):
        from services.meetings.meeting_context import build_meeting_agent_config
        meeting = {"id": "meet-char", "scope": scope, "created_by": creator}
        check_golden(f"builder-meeting-{scope}",
                     outcome(rig, lambda: build_meeting_agent_config(AGENT, meeting, "s-meet")))


    def test_agent_scope_meeting_of_a_creator_below_editor_is_refused(self, rig):
        from services.meetings.meeting_context import build_meeting_agent_config
        from storage.identity import db_users
        db_users.add_user_agent("user-viewer2", AGENT, "contributor", "user-admin")
        meeting = {"id": "meet-char", "scope": "agent", "created_by": "user-viewer2"}
        check_golden("builder-meeting-demoted-creator",
                     outcome(rig, lambda: build_meeting_agent_config(AGENT, meeting, "s-meet")))


class TestDeliveryContext:
    def test_person_and_agent(self, rig):
        from core.config.task_config_builder import build_delivery_security_context
        got = {
            who: normalize(run(build_delivery_security_context(
                AGENT, user_sub=sub, role=role, target="local")))
            for who, sub, role in (("person", "user-viewer", "editor"), ("agent", None, ""))
        }
        check_golden("builder-delivery-context", got)


class TestPhoneBuilder:
    @pytest.mark.parametrize("call_type,tools", [("inbound", False), ("outbound", True)])
    def test_external(self, rig, call_type, tools):
        from core.config.phone_config_builder import build_phone_agent_config
        check_golden(f"builder-phone-external-{call_type}", outcome(rig, lambda: build_phone_agent_config(
            AGENT, call_type=call_type, session_id="s-phone", call_tools=tools)))

    def test_user_route(self, rig):
        from core.config.phone_config_builder import build_phone_agent_config
        from services.phone.phone_identity import EXTERNAL_ROUTE_ROLE, RouteIdentity
        ident = RouteIdentity(mode="user", role=EXTERNAL_ROUTE_ROLE, external=None,
                              user=_user("user-viewer"), user_role="editor",
                              caller_claim="phone:char")
        check_golden("builder-phone-user-route", outcome(rig, lambda: build_phone_agent_config(
            AGENT, call_type="inbound", route_identity=ident, session_id="s-phone-user")))
