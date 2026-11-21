"""Characterization of the session builders: the built config (or the
refusal) and every collaborator call they make, for the chat, task, meeting,
delivery and phone builders. A drift is a behaviour change, and the commit that records it says why."""
from types import SimpleNamespace

import pytest

from tests._compute import serve
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
    # A compute machine serves the platform's place: every builder here
    # builds a session of it there (the platform runs none itself).
    serve(monkeypatch)
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
    def test_external(self, rig, call_type, tools, monkeypatch):
        """A route-less caller with one tool picked Read and write and one
        knowledge folder: the rest of the agent's tools are left out as not
        picked, the home is a scratch tree keyed on the session."""
        from core.config.phone_config_builder import build_phone_agent_config
        from services.mcp import mcp_manifest_store
        from storage.agents import outside_store
        from tests.fixtures.loop_builder_rig import _manifest
        # The channel's own tool, whole, on an outbound call (the rig's cache
        # holds it on purpose: another worker's tests may have loaded the
        # real catalog, which is not this golden's business).
        monkeypatch.setitem(mcp_manifest_store._manifests, "call-control-mcp", _manifest("call-control-mcp"))
        outside_store.update(AGENT, upsert_tools=[
            {"mcp_name": "char-files", "access": "read_write", "functions": [], "granted": []}],
            files_access="read_only", web=False, add_knowledge=["faq"])
        check_golden(f"builder-phone-external-{call_type}", outcome(rig, lambda: build_phone_agent_config(
            AGENT, call_type=call_type, session_id="4f1d2c3b-0000-4000-8000-00000000c411", call_tools=tools)))

    def test_an_outside_caller_on_a_proxy_route_is_built_as_a_call(self, rig):
        """The phone service says ``phone_mode`` only on a ``direct`` route,
        and the builder takes no such word: every outside caller is a call.
        A tool a manager picked for outside people whose manifest keeps it
        out of calls is absent."""
        import asyncio
        import inspect

        from core.config.phone_config_builder import build_phone_agent_config
        from services.mcp import mcp_session_filter
        from services.mcp.outside_picks import OutsidePicks, ToolPick
        from tests.fixtures.loop_builder_rig import _manifest
        assert "phone_mode" not in inspect.signature(build_phone_agent_config).parameters
        asyncio.run(build_phone_agent_config(AGENT, call_type="inbound",
                                             session_id="4f1d2c3b-0000-4000-8000-00000000c412"))
        built = next(c["kwargs"] for c in rig.calls if c["call"] == "build_session_mcp_config")
        assert built["phone_mode"] is True and built["call"] is True and built["external"] is True
        contexts = mcp_session_filter._exclusion_contexts(
            built["phone_mode"], False, False, False, built["external"], shared_chat=built["shared_chat"])
        voice = _manifest("voice-mcp")
        voice.exclude_from = ["phone"]
        picks = OutsidePicks(tools={"voice-mcp": ToolPick("read_write")})
        assert mcp_session_filter.session_exclusion_reason(
            voice, contexts=contexts, external=True, picks=picks) == "Excluded in phone mode"

    def test_user_route(self, rig):
        from core.config.phone_config_builder import build_phone_agent_config
        from services.phone.phone_identity import EXTERNAL_ROUTE_ROLE, RouteIdentity
        ident = RouteIdentity(mode="user", role=EXTERNAL_ROUTE_ROLE, external=None,
                              user=_user("user-viewer"), user_role="editor",
                              caller_claim="phone:char")
        check_golden("builder-phone-user-route", outcome(rig, lambda: build_phone_agent_config(
            AGENT, call_type="inbound", route_identity=ident, session_id="s-phone-user")))


class TestWithNoComputeMachine:
    """No door starts a session when no machine serves. Every builder a door
    uses refuses a session of the platform's place with the one sentence,
    before it takes an account's seat and before it builds a tool config:
    nothing is started, nothing is held, and the door shows the words."""

    def _builders(self):
        from core.config.phone_config_builder import (
            build_phone_agent_config,
            build_wrapup_agent_config,
        )
        from storage.billing import subscription_store
        for engine, provider in (("direct-llm", "anthropic"), ("codex-cli", "openai")):
            subscription_store.add_model(engine, f"char-{engine}", "Char", provider=provider, tier=2)
        from core.config.task_config_builder import TaskIdentity
        from services.meetings.meeting_context import build_meeting_agent_config
        from services.phone.phone_identity import EXTERNAL_ROUTE_ROLE, RouteIdentity
        ident = TaskIdentity(username="vic", role="editor", scope="user", creds_user_sub="user-viewer")
        route = RouteIdentity(mode="user", role=EXTERNAL_ROUTE_ROLE, external=None,
                              user=_user("user-viewer"), user_role="editor", caller_claim="phone:char")
        return {
            "a chat, an admin's": _chat(AGENT, "user-admin", "admin"),
            "a chat, a member's": _chat(AGENT, "user-viewer", "editor"),
            "a chat that continues a task": _chat(AGENT, "user-admin", "admin", task_identity=ident),
            "a Direct LLM chat": _chat(AGENT, "user-admin", "admin", execution_path="direct-llm",
                                       model="char-direct-llm"),
            "a Codex chat": _chat(AGENT, "user-admin", "admin", execution_path="codex-cli",
                                  model="char-codex-cli"),
            "a task of the agent": _task("agent", "user-admin"),
            "a person's task": _task("user", "user-viewer"),
            "a judge": _task("agent", "user-admin", judge={"check": "c"}),
            "a meeting of a person": lambda: build_meeting_agent_config(
                AGENT, {"id": "meet-char", "scope": "user", "created_by": "user-viewer"}, "s-meet"),
            "a meeting of the agent": lambda: build_meeting_agent_config(
                AGENT, {"id": "meet-char", "scope": "agent", "created_by": "user-manager"}, "s-meet"),
            "a call from outside": lambda: build_phone_agent_config(
                AGENT, call_type="inbound", session_id="4f1d2c3b-0000-4000-8000-00000000c411"),
            "a call on a person's route": lambda: build_phone_agent_config(
                AGENT, call_type="inbound", route_identity=route, session_id="s-phone-user"),
            "a call's wrap-up": lambda: build_wrapup_agent_config(
                AGENT, {"chat_id": "chat-wrap", "identity_id": "", "home": ""}, "s-wrap"),
        }

    def test_every_builder_refuses_with_the_sentence_and_holds_nothing(self, rig, monkeypatch):
        from core import placement
        from tests._compute import serve_none
        serve_none(monkeypatch)
        builders = self._builders()
        for door, build in builders.items():
            rig.calls.clear()
            with pytest.raises(placement.NoComputeMachine) as refused:
                run(build())
            assert str(refused.value) == placement.NO_COMPUTE_SENTENCE, door
            held = {c["call"] for c in rig.calls} & {"resolve_subscription_env", "build_session_mcp_config",
                                                     "ensure_persistent_agent_dir"}
            assert not held, f"{door}: refused after {sorted(held)}"
        assert placement.NO_COMPUTE_SENTENCE == ("OtoDock's compute machine is not connected, so this "
                                                 "cannot run now. Try again in a few minutes.")
        assert issubclass(placement.NoComputeMachine, RuntimeError)

    def test_the_same_builders_build_on_the_machine_once_one_serves(self, rig):
        """The counterpart: with a compute machine connected each of those
        sessions is placed on it, and none on the platform."""
        from core import placement
        from tests._compute import COMPUTE
        for door, build in self._builders().items():
            cfg = run(build())
            placed = cfg.security_context.placement
            assert (placed.kind, placed.machine_id) == (placement.KIND_COMPUTE, COMPUTE["id"]), door
            assert placement.is_local(cfg.execution_target), door


class TestEachBuilderNamesTheHome:
    """On a machine that makes homes each builder acquires the session's
    account with its home's key and hands the start that home: the two name
    the same folder, so the pool's pick is the one the engine reads."""

    SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    HOME = f"/char/.claude/own/{SID}"

    @pytest.fixture
    def homes(self, rig, monkeypatch):
        from tests._compute import COMPUTE
        serve(monkeypatch, {**COMPUTE, "capabilities": {"session_homes": True}})
        return rig

    def _built(self, rig, build):
        got = outcome(rig, build)
        assert "config" in got, got
        sticky = [c["kwargs"]["sticky_scope"] for c in rig.calls if c["call"] == "resolve_subscription_env"]
        assert sticky and all(s.endswith(":" + self.HOME) for s in sticky), sticky
        assert got["config"]["session_home"] == self.HOME

    def test_a_chat(self, homes):
        self._built(homes, _chat(AGENT, "user-viewer", "editor", session_id=self.SID))

    def test_a_task(self, homes):
        from core.config.task_config_builder import build_task_agent_config
        task = SimpleNamespace(scope="user", created_by="user-viewer", task_type="scheduled",
                               notification_mode="manual", name="t", id="dyn-char")
        self._built(homes, lambda: build_task_agent_config(AGENT, task, self.SID))

    def test_a_meeting(self, homes):
        from services.meetings.meeting_context import build_meeting_agent_config
        meeting = {"id": "meet-char", "scope": "user", "created_by": "user-viewer"}
        self._built(homes, lambda: build_meeting_agent_config(AGENT, meeting, self.SID))
