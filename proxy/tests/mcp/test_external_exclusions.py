"""External sessions never attach the platform-management MCPs — one rule
(``mcp_registry.session_exclusion_reason``) applied by the session MCP
config, the prompt catalog, the inline skills, the Direct-LLM skill catalog,
the route preview and the Direct manager's own rebuild.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from tests.mcp.test_mcp_broker_activation import (  # noqa: E402
    _FakeManifest, _stub_assembly,
)


def _manifest(name, exclude_from=()):
    fm = _FakeManifest(name)
    fm.exclude_from = list(exclude_from)
    return fm


def _build(monkeypatch, tmp_path, manifests, **kwargs):
    from services.mcp import mcp_registry
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path)
    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, **kwargs,
    )
    return excluded


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

class TestRule:
    def test_hard_set_beats_a_manifest_without_the_key(self):
        from services.mcp import mcp_registry as reg
        m = _manifest("schedules-mcp")          # no "external" in exclude_from
        assert reg.session_exclusion_reason(m, contexts={"phone"}, external=True) \
            == reg.EXTERNAL_DENIED_REASON
        assert reg.session_exclusion_reason(m, contexts={"phone"}, external=False) is None

    def test_manifest_opt_out(self):
        from services.mcp import mcp_registry as reg
        m = _manifest("helpdesk", ["external"])
        assert reg.session_exclusion_reason(m, contexts={"phone"}, external=True) \
            == reg.EXTERNAL_CONTEXT_REASON
        assert reg.session_exclusion_reason(m, contexts={"phone"}, external=False) is None

    def test_base_context_outranks_external_for_the_reason_text(self):
        from services.mcp import mcp_registry as reg
        m = _manifest("display-mcp", ["phone", "external"])
        assert reg.session_exclusion_reason(m, contexts={"phone"}, external=True) \
            == "Excluded in phone mode"
        assert reg.session_exclusion_reason(m, contexts=set(), external=True) \
            == reg.EXTERNAL_CONTEXT_REASON

    def test_meeting_still_outranks_the_base_context(self):
        from services.mcp import mcp_registry as reg
        m = _manifest("x", ["task", "meeting"])
        assert reg.session_exclusion_reason(m, contexts={"task", "meeting"}, external=False) \
            == "Excluded in meeting mode"

    def test_filter_helper(self):
        from services.mcp import mcp_registry as reg
        kept = reg.filter_manifests_for_session(
            [_manifest("memory-mcp"), _manifest("delegation-mcp"), _manifest("x", ["external"])],
            contexts={"phone"}, external=True,
        )
        assert [m.name for m in kept] == ["memory-mcp"]


# ---------------------------------------------------------------------------
# Session MCP config
# ---------------------------------------------------------------------------

class TestSessionConfig:
    def test_external_phone_session_drops_the_management_set(self, monkeypatch, tmp_path):
        from services.mcp import mcp_registry as reg
        excluded = _build(
            monkeypatch, tmp_path,
            [
                _manifest("memory-mcp"), _manifest("file-tools"),
                _manifest("schedules-mcp"), _manifest("delegation-mcp"),
                _manifest("helpdesk", ["external"]), _manifest("display-mcp", ["phone"]),
            ],
            phone_mode=True, external=True,
        )
        assert excluded["schedules-mcp"] == reg.EXTERNAL_DENIED_REASON
        assert excluded["delegation-mcp"] == reg.EXTERNAL_DENIED_REASON
        assert excluded["helpdesk"] == reg.EXTERNAL_CONTEXT_REASON
        assert excluded["display-mcp"] == "Excluded in phone mode"
        assert "memory-mcp" not in excluded and "file-tools" not in excluded

    def test_user_tied_phone_session_keeps_them(self, monkeypatch, tmp_path):
        excluded = _build(
            monkeypatch, tmp_path,
            [_manifest("schedules-mcp"), _manifest("helpdesk", ["external"]),
             _manifest("display-mcp", ["phone"])],
            phone_mode=True,
        )
        assert "schedules-mcp" not in excluded and "helpdesk" not in excluded
        assert excluded["display-mcp"] == "Excluded in phone mode"

    def test_hard_set_applies_after_extra_mcps(self, monkeypatch, tmp_path):
        """A force-included MCP (the meetings escape hatch) is still denied."""
        from services.mcp import mcp_registry as reg
        _stub_assembly(monkeypatch, [_manifest("memory-mcp")], env_by_mcp={}, tmp_path=tmp_path)
        extra = _manifest("meetings-mcp")
        monkeypatch.setattr(reg, "get_manifest", lambda n: extra if n == "meetings-mcp" else None)
        _path, _env, excluded, _b, _k = reg.build_session_mcp_config(
            "agent", None, phone_mode=True, external=True, extra_mcps=["meetings-mcp"],
        )
        assert excluded["meetings-mcp"] == reg.EXTERNAL_DENIED_REASON


# ---------------------------------------------------------------------------
# Prompt side
# ---------------------------------------------------------------------------

def _skill(sid, loading="always", exclude=()):
    return SimpleNamespace(id=sid, file=f"{sid}.md", loading=loading,
                           description=f"about {sid}", default_exclude_from=list(exclude))


class TestPromptSide:
    @pytest.fixture
    def registry(self, monkeypatch, tmp_path):
        from services.mcp import mcp_registry as reg
        from storage.mcp import mcp_store
        manifests = [
            _manifest("memory-mcp"), _manifest("schedules-mcp"), _manifest("helpdesk", ["external"]),
        ]
        for m in manifests:
            m.category = "mcp"
            m.description = f"{m.name} does things."
            m.mcp_dir = tmp_path / m.name
            m.mcp_dir.mkdir()
            m.skills = [_skill(f"{m.name}-card"), _skill(f"{m.name}-guide", "on_demand")]
            for s in m.skills:
                (m.mcp_dir / s.file).write_text(f"# {s.id}\nbody\n")
        monkeypatch.setattr(reg, "get_agent_mcps", lambda *a, **k: manifests)
        monkeypatch.setattr(mcp_store, "get_agent_skills", lambda a: [])
        return reg

    def test_catalog_skills_and_skill_catalog(self, registry):
        reg = registry
        catalog = reg.build_available_mcps_section("agent", context="phone", external=True)
        assert "`memory-mcp`" in catalog
        assert "schedules-mcp" not in catalog and "helpdesk" not in catalog
        # Non-external phone sessions keep both.
        assert "schedules-mcp" in reg.build_available_mcps_section("agent", context="phone")

        skills = {sid for sid, _b, _l in reg.get_skills_for_agent("agent", "phone", external=True)}
        assert skills == {"memory-mcp-card", "memory-mcp-guide"}
        on_demand = [sid for sid, _d in reg.get_skill_catalog_for_agent("agent", "phone", external=True)]
        assert on_demand == ["memory-mcp-guide"]

    def test_skill_level_external_opt_out(self, registry):
        reg = registry
        manifests = reg.get_agent_mcps("agent")
        extra = _skill("memory-mcp-humans-only", exclude=["external"])
        (manifests[0].mcp_dir / extra.file).write_text("# humans only\nbody\n")
        manifests[0].skills.append(extra)
        skills = {sid for sid, _b, _l in reg.get_skills_for_agent("agent", "phone", external=True)}
        assert "memory-mcp-humans-only" not in skills
        skills = {sid for sid, _b, _l in reg.get_skills_for_agent("agent", "phone")}
        assert "memory-mcp-humans-only" in skills


class TestUnavailableTools:
    def test_external_reasons_are_not_listed(self, temp_db):
        import config
        from services.mcp import mcp_registry as reg
        from storage.agents import agent_store
        if not agent_store.agent_exists("ext-prompt"):
            agent_store.create_agent("ext-prompt", "Ext Prompt")
        persona = config.get_agent_dir("ext-prompt") / "config" / "agent.md"
        persona.parent.mkdir(parents=True, exist_ok=True)
        persona.write_text("# Ext Prompt\n\nA support agent.\n")
        excluded = {
            "schedules-mcp": reg.EXTERNAL_DENIED_REASON,
            "helpdesk": reg.EXTERNAL_CONTEXT_REASON,
            "crm": "Credentials not configured",
        }
        prompt = config.build_agent_prompt("ext-prompt", excluded_mcps=excluded,
                                           client_type="phone", external=True) or ""
        assert "crm" in prompt and "Credentials not configured" in prompt
        assert "schedules-mcp" not in prompt and "helpdesk" not in prompt
        # A non-external session still lists everything it was told about.
        prompt = config.build_agent_prompt("ext-prompt", excluded_mcps=excluded,
                                           client_type="phone") or ""
        assert "schedules-mcp" in prompt


# ---------------------------------------------------------------------------
# Direct manager + shipped manifests + HTTP warmup
# ---------------------------------------------------------------------------

class TestDirectManager:
    @pytest.mark.asyncio
    async def test_rebuild_carries_the_flag(self, monkeypatch):
        from core.layers.direct.mcp import AgentMCPManager
        from services.mcp import mcp_registry as reg
        seen = {}

        def _fake_build(agent, user_sub, **kw):
            seen.update(kw)
            return None, {}, {}, {}, set()

        monkeypatch.setattr(reg, "build_session_mcp_config", _fake_build)
        mgr = AgentMCPManager("agent", phone_mode=True, session_id="s", external=True)
        await mgr._start_impl()
        assert seen["external"] is True and seen["phone_mode"] is True
        # Built into the session's own directory, never the identity's
        # shared file another session of the agent rewrites.
        assert seen["session_id"] == "s"


SHIPPED_EXTERNAL_OPT_OUTS = [
    "delegation-mcp", "schedules-mcp", "triggers-mcp", "notifications-mcp",
    "meetings-mcp", "agent-config-mcp", "agent-creator-mcp", "mcps-mcp",
    "ssh-hosts", "phone-mcp", "location-mcp", "display-mcp", "computer-mcp",
]


@pytest.mark.parametrize("name", SHIPPED_EXTERNAL_OPT_OUTS)
def test_shipped_manifest_declares_the_external_opt_out(name):
    path = PROXY_DIR.parent / "mcps" / "custom" / name / "manifest.json"
    if not path.exists():
        # Not every MCP is in every cut (the public snapshot leaves out the
        # ones that ride the Android beat); the hard set below still binds.
        pytest.skip(f"{name} is not part of this tree")
    manifest = json.loads(path.read_text())
    assert "external" in manifest.get("exclude_from", []), name


def test_hard_set_is_a_subset_of_the_shipped_opt_outs():
    from services.mcp.mcp_registry import EXTERNAL_DENIED_MCPS
    assert EXTERNAL_DENIED_MCPS <= set(SHIPPED_EXTERNAL_OPT_OUTS)


class TestHttpWarmup:
    def test_phone_mode_is_refused(self, monkeypatch, temp_db):
        import config
        from fastapi.testclient import TestClient
        from app import app
        monkeypatch.setattr(config, "is_master_key", lambda k: k == "master")
        client = TestClient(app)
        r = client.post("/v1/sessions/warmup",
                        json={"model": "x", "phone_mode": True, "call_type": "inbound"},
                        headers={"Authorization": "Bearer master"})
        assert r.status_code == 410
        # A session token cannot warm sessions at all.
        from tests.conftest import live_session_token
        tok = live_session_token("00000000-0000-4000-8000-000000000000", "x")
        r = client.post("/v1/sessions/warmup", json={"model": "x"},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_a_cli_warmup_builds_its_mcp_config_for_its_session(self, monkeypatch):
        import config
        from api.sessions import sessions
        from services.mcp import mcp_registry as reg
        monkeypatch.setattr(config, "is_master_key", lambda k: k == "master")
        monkeypatch.setattr(config, "build_agent_prompt", lambda *a, **kw: "")
        seen = {}

        def _fake_build(agent, user_sub, **kw):
            seen.update(kw)
            return None, {}, {}, {}, set()

        async def _fake_session(**kw):
            seen["warmed"] = kw["session_id"]

        monkeypatch.setattr(reg, "build_session_mcp_config", _fake_build)
        monkeypatch.setattr(sessions, "get_or_create_persistent_session", _fake_session)
        out = await sessions.warmup_session_endpoint(
            sessions.WarmupRequest(model="x", session_id="s-warm"),
            authorization="Bearer master",
        )
        assert out["session_id"] == "s-warm"
        assert seen["session_id"] == "s-warm" and seen["warmed"] == "s-warm"
