"""Standalone skill packages (``category: "skill"``) — API surfaces + registry.

A skill package is a context-only
manifest carrying skills; the Skills tab is its ONLY control surface. Pins:
- skill packages never surface as MCPs (GET /mcps, prompt catalog,
  discovery count);
- the replace-all PUT /mcps REJECTS skill packages in the payload and
  PRESERVES assigned ones absent from it (the MCPs tab and mcps-mcp
  round-trip a skill-filtered list — a plain replace would wipe standalone
  enablement);
- GET /skills lists unassigned visible packages (assigned:false) so the tab
  can offer them; PATCH enable auto-assigns the package; unknown ids 404;
- registry scan is first-wins on name collision;
- approval requests carry kind ('mcp' default | 'skill').
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_manifest(name, category="custom", skills=None, tmp_path=None):
    from services.mcp.mcp_registry import (
        McpManifest, ServerConfig, CredentialConfig, SkillDef,
    )
    skill_defs = []
    mcp_dir = None
    if skills:
        mcp_dir = tmp_path / name
        (mcp_dir / "skills").mkdir(parents=True, exist_ok=True)
        for sid, loading in skills:
            (mcp_dir / "skills" / f"{sid}.md").write_text(f"# {sid}\n\nBody.\n")
            skill_defs.append(SkillDef(
                id=sid, file=f"skills/{sid}.md",
                description=f"{sid} skill", loading=loading,
            ))
    runtime = "none" if category == "skill" else "python"
    transport = "none" if category == "skill" else "stdio"
    return McpManifest(
        name=name,
        label=name.replace("-", " ").title(),
        description="",
        version="1.0.0",
        category=category,
        server=ServerConfig(runtime=runtime, transport=transport),
        credentials=CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=skill_defs,
        assignment_mode="auto",
        mcp_dir=mcp_dir,
    )


def _patch_manifests(monkeypatch, manifests: dict):
    from services.mcp import mcp_registry
    from storage import mcp_store
    monkeypatch.setattr(mcp_registry, "_manifests", manifests)
    for name in manifests:
        mcp_store.set_mcp_enabled(name, True)


def _seed_agent(slug: str):
    from storage.pg import get_conn
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agents (slug, display_name, created_at, updated_at)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (slug, slug.replace("-", " ").title(), now, now),
        )
        conn.commit()


@pytest.fixture
def client(temp_db, monkeypatch):
    from fastapi import Request
    from fastapi.testclient import TestClient
    from auth.providers import UserContext, get_current_user

    admin_ctx = UserContext(
        sub="user-admin", email="admin@test.com", name="Admin",
        role="admin", is_api_key=False,
    )

    async def _get_admin(request: Request) -> UserContext:
        return admin_ctx

    from app import app
    app.dependency_overrides[get_current_user] = _get_admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def wired(client, monkeypatch, tmp_path):
    """One ordinary MCP with a bundled skill + one standalone skill package."""
    _seed_agent("alice-agent")
    _patch_manifests(monkeypatch, {
        "tts-mcp": _make_manifest(
            "tts-mcp", skills=[("voiceover", "on_demand")], tmp_path=tmp_path),
        "pdf-skills": _make_manifest(
            "pdf-skills", category="skill",
            skills=[("pdf-processing", "on_demand")], tmp_path=tmp_path),
    })
    return client


# ── MCP surfaces ───────────────────────────────────────────────────────

def test_get_mcps_excludes_skill_packages(wired):
    payload = wired.get("/v1/agents/alice-agent/mcps").json()
    names = {m["name"] for m in payload["mcps"]}
    assert "tts-mcp" in names
    assert "pdf-skills" not in names


def test_prompt_catalog_excludes_skill_packages(wired, monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    manifests = [
        _make_manifest("tts-mcp"),
        _make_manifest("pdf-skills", category="skill"),
    ]
    with patch.object(mcp_registry, "get_agent_mcps", return_value=manifests):
        text = mcp_registry.build_available_mcps_section("alice-agent")
    assert "tts-mcp" in text
    assert "pdf-skills" not in text


def test_discovery_count_excludes_skill_packages(wired):
    from api.agents.discovery import _get_mcp_info
    from storage import mcp_store
    mcp_store.add_agent_mcp("alice-agent", "tts-mcp")
    mcp_store.add_agent_mcp("alice-agent", "pdf-skills")
    count, names = _get_mcp_info("alice-agent")
    assert names == ["tts-mcp"]
    assert count == 1


def test_put_rejects_skill_packages_in_payload(wired):
    r = wired.put("/v1/agents/alice-agent/mcps",
                  json={"mcps": ["tts-mcp", "pdf-skills"]})
    assert r.status_code == 400
    assert r.json()["detail"]["skill_packages"] == ["pdf-skills"]


def test_put_preserves_assigned_skill_packages(wired):
    from storage import mcp_store
    mcp_store.add_agent_mcp("alice-agent", "pdf-skills")
    r = wired.put("/v1/agents/alice-agent/mcps", json={"mcps": ["tts-mcp"]})
    assert r.status_code == 200
    assert set(r.json()["mcps"]) == {"tts-mcp", "pdf-skills"}
    assert set(mcp_store.get_manager_enabled_mcps("alice-agent")) == {
        "tts-mcp", "pdf-skills"}


# ── Skills tab endpoint ────────────────────────────────────────────────

def test_skills_list_includes_unassigned_standalone(wired):
    from storage import mcp_store
    mcp_store.add_agent_mcp("alice-agent", "tts-mcp")
    rows = wired.get("/v1/agents/alice-agent/skills").json()["skills"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["voiceover"]["standalone"] is False
    assert by_id["voiceover"]["assigned"] is True
    assert by_id["voiceover"]["enabled"] is True
    assert by_id["voiceover"]["loading"] == "on_demand"
    pdf = by_id["pdf-processing"]
    assert pdf["standalone"] is True
    assert pdf["assigned"] is False
    assert pdf["enabled"] is False   # unassigned package → off regardless of rows


def test_patch_enable_standalone_auto_assigns(wired):
    from storage import mcp_store
    r = wired.patch("/v1/agents/alice-agent/skills/pdf-processing",
                    json={"enabled": True, "exclude_from": []})
    assert r.status_code == 200
    assert "pdf-skills" in mcp_store.get_manager_enabled_mcps("alice-agent")
    rows = wired.get("/v1/agents/alice-agent/skills").json()["skills"]
    pdf = next(x for x in rows if x["id"] == "pdf-processing")
    assert pdf["enabled"] is True and pdf["assigned"] is True


def test_patch_disable_keeps_package_assigned(wired):
    from storage import mcp_store
    wired.patch("/v1/agents/alice-agent/skills/pdf-processing",
                json={"enabled": True, "exclude_from": []})
    wired.patch("/v1/agents/alice-agent/skills/pdf-processing",
                json={"enabled": False, "exclude_from": []})
    assert "pdf-skills" in mcp_store.get_manager_enabled_mcps("alice-agent")
    rows = wired.get("/v1/agents/alice-agent/skills").json()["skills"]
    pdf = next(x for x in rows if x["id"] == "pdf-processing")
    assert pdf["enabled"] is False


def test_patch_unknown_skill_404s(wired):
    r = wired.patch("/v1/agents/alice-agent/skills/no-such-skill",
                    json={"enabled": True, "exclude_from": []})
    assert r.status_code == 404


# ── Registry + request store ───────────────────────────────────────────

def test_scan_first_wins_on_name_collision(temp_db, tmp_path, monkeypatch):
    import json as _json
    import config as app_config
    from services.mcp import mcp_registry

    for cat in ("custom", "skills"):
        d = tmp_path / cat / "clash"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(_json.dumps({
            "name": "clash", "label": f"From {cat}", "description": "d",
            "version": "1.0.0",
            "category": "custom" if cat == "custom" else "skill",
            "server": {"runtime": "none", "transport": "none"},
        }))
    monkeypatch.setattr(app_config, "MCPS_DIR", tmp_path)
    saved = mcp_registry._manifests
    try:
        mcp_registry.scan_manifests()
        m = mcp_registry.get_manifest("clash")
        assert m.label == "From custom"      # custom scans before skills
    finally:
        mcp_registry._manifests = saved


def test_create_request_kind_roundtrip(temp_db):
    from storage import mcp_request_store as rs
    row_mcp = rs.create_request("camoufox", "a1", "user-1")
    assert row_mcp["kind"] == "mcp"
    row_skill = rs.create_request("pdf-skills", "a1", "user-1", kind="skill")
    assert row_skill["kind"] == "skill"


# ── Admin delete ───────────────────────────────────────────────────────

def test_delete_skill_package_removes_folder_and_rows(wired, monkeypatch):
    """DELETE /v1/admin/mcps/{name} accepts category=skill and unwires it:
    folder gone, agent_mcps assignment gone, agent_skills rows gone (skill
    ids are BARE — the old '{mcp}/%' LIKE cleanup matched nothing)."""
    from services.mcp import mcp_registry
    from storage import mcp_store
    from storage.pg import get_conn

    r = wired.patch(
        "/v1/agents/alice-agent/skills/pdf-processing",
        json={"enabled": True, "exclude_from": []},
    )
    assert r.status_code == 200
    assert "pdf-skills" in mcp_store.get_manager_enabled_mcps("alice-agent")

    pkg_dir = mcp_registry.get_manifest("pdf-skills").mcp_dir
    assert pkg_dir.exists()
    monkeypatch.setattr(mcp_registry, "scan_manifests", lambda: None)

    r = wired.delete("/v1/admin/mcps/pdf-skills")
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert not pkg_dir.exists()
    assert "pdf-skills" not in mcp_store.get_manager_enabled_mcps("alice-agent")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM agent_skills WHERE skill_id = %s",
            ("pdf-processing",),
        ).fetchone()
    assert row["cnt"] == 0


def test_delete_custom_mcp_still_rejected(wired, monkeypatch):
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "scan_manifests", lambda: None)
    r = wired.delete("/v1/admin/mcps/tts-mcp")
    assert r.status_code == 400
