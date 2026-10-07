"""``POST /v1/mcps/local-package/validate``: the authoring check tool's
route. A session with a signed-in person at the editor tier of its agent,
the folder read through the session's own path policy and snapshotted, the
check's verdict as the body."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.mcp import local_templates
from auth.providers import UserContext, get_current_user


def _client(principal):
    async def _stub():
        return principal

    app = FastAPI()
    app.include_router(local_templates.router)
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


def _session_user(*, role="editor", agent="pa", session_id="sid-1", is_api_key=True):
    agent_roles = {agent: role} if role else {}
    return UserContext(
        sub="user-manager", email="m@test.com", name="M", role="member",
        agents=[agent] if role else [], agent_roles=agent_roles,
        is_api_key=is_api_key, session_id=session_id, agent=agent,
    )


@pytest.fixture
def resolved_dir(monkeypatch, tmp_path):
    """The path resolver replaced by a map from the raw path to a real
    folder: the policy walk is the local-template route's own, tested there."""
    folders: dict[str, Path] = {}

    def _resolve(user, raw_path):
        from fastapi import HTTPException
        if raw_path not in folders:
            raise HTTPException(403, "access denied")
        return folders[raw_path]

    monkeypatch.setattr(local_templates, "_resolve_template_dir", _resolve)
    return folders


def _package(tmp_path: Path, name="weather-tools", **over) -> Path:
    root = tmp_path / name
    root.mkdir()
    data = {
        "name": name, "label": "Weather", "description": "d", "version": "",
        "category": "community", "author": "example",
        "author_url": "https://github.com/example/weather",
        "server": {"runtime": "node", "transport": "stdio", "command": "node",
                   "source": "npm:weather"},
    }
    data.update(over)
    (root / "manifest.json").write_text(json.dumps(data))
    (root / "README.md").write_text("Weather.\n")
    return root


@pytest.mark.parametrize("principal, status", [
    (_session_user(role="editor"), 200),
    (_session_user(role="manager"), 200),
    (_session_user(role="contributor"), 403),
    (_session_user(role="viewer"), 403),
    (_session_user(role=""), 403),
    (_session_user(role="editor", session_id=""), 403),   # not a session principal
])
def test_the_route_takes_a_session_at_the_editor_tier(temp_db, resolved_dir, tmp_path, principal, status):
    resolved_dir["/workspace/weather-tools"] = _package(tmp_path)
    r = _client(principal).post("/v1/mcps/local-package/validate", json={"path": "/workspace/weather-tools"})
    assert r.status_code == status, r.text
    if status == 200:
        assert r.json()["ok"] is True


def test_a_path_the_policy_refuses_is_a_403(temp_db, resolved_dir):
    r = _client(_session_user()).post("/v1/mcps/local-package/validate", json={"path": "/etc"})
    assert r.status_code == 403


def test_the_verdict_names_the_faults_and_the_skipped_content(temp_db, resolved_dir, tmp_path):
    root = _package(tmp_path, skills=[{"id": "weather-usage", "file": "skills/weather-usage/SKILL.md"}])
    (root / ".env").write_text("SECRET=1\n")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("")
    (root / "skills" / "weather-usage").mkdir(parents=True)
    (root / "skills" / "weather-usage" / "SKILL.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("outside\n")
    resolved_dir["/workspace/weather-tools"] = root
    r = _client(_session_user()).post("/v1/mcps/local-package/validate", json={"path": "/workspace/weather-tools"})
    # The symlinked skill file is refused by the snapshot before any rule runs.
    assert r.status_code == 400 and "Symlinks are not allowed" in r.json()["detail"]
    (root / "skills" / "weather-usage" / "SKILL.md").unlink()
    (root / "skills" / "weather-usage" / "SKILL.md").write_text("---\nname: weather-usage\n---\n")
    r = _client(_session_user()).post("/v1/mcps/local-package/validate", json={"path": "/workspace/weather-tools"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    joined = "\n".join(body["errors"])
    assert ".env: a .env file is refused" in joined
    assert "venv: a venv entry is refused" in joined
    assert body["summary"]["skills"] == ["weather-usage"]


def test_the_snapshot_is_deleted_after_the_check(temp_db, resolved_dir, tmp_path, monkeypatch):
    from services.community import community_installer
    resolved_dir["/workspace/weather-tools"] = _package(tmp_path)
    seen: dict[str, Path] = {}
    real = community_installer.check_package

    def _spy(root, *, skipped=()):
        seen["root"] = Path(root)
        assert seen["root"].is_dir() and (seen["root"] / "manifest.json").is_file()
        return real(root, skipped=skipped)

    monkeypatch.setattr(community_installer, "check_package", _spy)
    r = _client(_session_user()).post("/v1/mcps/local-package/validate", json={"path": "/workspace/weather-tools"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not seen["root"].exists()
    assert str(seen["root"]) != str(resolved_dir["/workspace/weather-tools"])
