"""``community_installer.check_package``: the installer's rules applied to an
author's folder without installing (the ``validate_mcp_package`` tool of
mcps-mcp runs it on a snapshot)."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from services.community import community_installer as ci


def _manifest(**over) -> dict:
    data = {
        "name": "weather-tools", "label": "Weather", "description": "Forecasts",
        "version": "", "category": "community",
        "author": "example", "author_url": "https://github.com/example/weather",
        "server": {"runtime": "node", "transport": "stdio", "command": "node",
                   "args": ["${mcp_dir}/node_modules/weather/dist/index.js"],
                   "source": "npm:weather"},
    }
    data.update(over)
    return data


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk
            + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF))


def _folder(tmp_path: Path, manifest: dict | str, *, files: dict[str, bytes | str] | None = None) -> Path:
    root = tmp_path / "pkg"
    root.mkdir(exist_ok=True)
    (root / "manifest.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    (root / "README.md").write_text("Weather.\n")
    for rel, body in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body)
    return root


def test_a_good_package_passes_with_no_warnings(tmp_path):
    root = _folder(tmp_path, _manifest(), files={"icon.png": _png(256, 256)})
    out = ci.check_package(root)
    assert out["ok"] is True
    assert out["errors"] == [] and out["warnings"] == []
    assert out["summary"]["name"] == "weather-tools"
    assert out["summary"]["runtime"] == "node" and out["summary"]["source"] == "npm:weather"
    assert out["summary"]["files"] == 3


def test_refused_content_is_an_error_even_when_the_snapshot_left_it_out(tmp_path):
    root = _folder(tmp_path, _manifest(), files={"lib/.env": "X=1\n", "node_modules/x/index.js": ""})
    out = ci.check_package(root, skipped=[".env", "venv", ".git"])
    assert out["ok"] is False
    joined = "\n".join(out["errors"])
    assert ".env: a .env file is refused" in joined
    assert "venv: a venv entry is refused" in joined
    assert ".git: a .git entry is refused" in joined
    assert "lib/.env: a .env file is refused" in joined
    assert "node_modules: a node_modules entry is refused" in joined


def test_dropped_files_are_warnings(tmp_path):
    root = _folder(tmp_path, _manifest(server={
        "runtime": "python", "transport": "stdio", "command": "venv/bin/python",
        "args": ["-m", "weather"], "source": "pypi:weather",
    }), files={"package-lock.json": "{}", "requirements.txt": "weather==1\n"})
    out = ci.check_package(root)
    assert out["ok"] is True
    assert any("package-lock.json is dropped" in w for w in out["warnings"])
    assert any("requirements.txt is dropped" in w for w in out["warnings"])


@pytest.mark.parametrize("manifest, needle", [
    ("{not json", "not valid JSON"),
    ("[]", "must hold an object"),
    (_manifest(category="custom"), "category must be"),
    (_manifest(name="Weather Tools"), "must be lowercase"),
    (_manifest(server={"runtime": "node", "transport": "stdio", "command": "node",
                       "source": "ext::weather"}), "not an accepted source"),
    (_manifest(skills=[{"id": "x", "file": "../../etc/passwd"}]), "relative path inside"),
    (_manifest(skills=[{"id": "x", "file": "skills/x/SKILL.md"}]), "not a regular file inside"),
    (_manifest(replaces=[{"source": "npm:a", "credentials": {"_X": "Y"}}]), "replaces entry"),
    (_manifest(env={"OTO_SESSION_ID": "1"}, server={"runtime": "node", "transport": "stdio",
                                                     "command": "node", "source": "npm:w",
                                                     "args": ["${platform.api_key}"]}), "master key"),
    (_manifest(version="", server={"runtime": "python", "transport": "stdio", "command": "x",
                                   "source": "git+https://host/r.git@v1"}), "version must be set for a git+"),
])
def test_errors_name_the_rule(tmp_path, manifest, needle):
    out = ci.check_package(_folder(tmp_path, manifest))
    assert out["ok"] is False
    assert any(needle in e for e in out["errors"]), out["errors"]


def test_a_container_package_s_rules(tmp_path):
    manifest = _manifest(version="1.0.0", server={
        "runtime": "docker", "transport": "http", "port": 8999,
        "docker_compose": "docker-compose.yml", "source": "docker:weather",
        "url_template": "http://localhost:8999",
    })
    out = ci.check_package(_folder(tmp_path, manifest))
    assert any("docker_compose" in e and "not a regular file" in e for e in out["errors"])
    root = _folder(tmp_path, manifest, files={"docker-compose.yml": "services:\n  w:\n    build: .\n"})
    out = ci.check_package(root)
    assert out["ok"] is True
    assert any("server.image is absent" in w for w in out["warnings"])
    assert any("docker_mcp_host" in w for w in out["warnings"])
    manifest["version"] = ""
    out = ci.check_package(_folder(tmp_path, manifest, files={"docker-compose.yml": "services: {}\n"}))
    assert any("version must be set for a docker" in e for e in out["errors"])


def test_conventions_are_warnings(tmp_path):
    root = _folder(tmp_path, _manifest(author="", author_url="http://x",
                                       env={"OTO_AGENT_NAME": "x", "PROXY_URL": "y"},
                                       server={"runtime": "node", "transport": "stdio", "command": "node",
                                               "source": "npm:weather@1.2.3"}),
                   files={"icon.png": _png(128, 128)})
    (root / "README.md").unlink()
    out = ci.check_package(root)
    assert out["ok"] is True
    joined = "\n".join(out["warnings"])
    assert "README.md is missing" in joined
    assert "icon.png is 128x128" in joined
    assert "author is missing" in joined
    assert "author_url should be" in joined
    assert "env.OTO_AGENT_NAME" in joined and "env.PROXY_URL" in joined
    assert "pins 1.2.3" in joined


def test_a_critical_rule_is_a_warning_naming_the_tool(tmp_path):
    root = _folder(tmp_path, _manifest(permissions={"default_tier": "standard", "rules": [
        {"tool": "read", "tier": "open"}, {"tool": "ban_user", "tier": "critical"}]}),
        files={"icon.png": _png(256, 256)})
    out = ci.check_package(root)
    assert out["ok"] is True and out["errors"] == []
    assert out["warnings"] == [
        "permissions: critical is kept for OtoDock's own tools, so ban_user will run as sensitive"]


def test_a_critical_default_tier_is_a_warning(tmp_path):
    root = _folder(tmp_path, _manifest(permissions={"default_tier": "critical", "rules": [
        {"tool": "send_*", "tier": "critical"}]}), files={"icon.png": _png(256, 256)})
    out = ci.check_package(root)
    assert out["ok"] is True and out["errors"] == []
    assert out["warnings"] == [
        "permissions: critical is kept for OtoDock's own tools, so default_tier, send_* "
        "will run as sensitive"]


def test_a_block_the_parser_rejects_is_an_error(tmp_path):
    root = _folder(tmp_path, _manifest(costs={"currency": "EUR", "provider": "x", "rules": []}))
    out = ci.check_package(root)
    assert out["ok"] is False
    assert any("rejected" in e or "costs" in e for e in out["errors"]), out["errors"]


@pytest.mark.parametrize(("rules", "error"), [
    (5, "permissions.rules must be a list, got int"),
    (True, "permissions.rules must be a list, got bool"),
    ([5, {"tool": "ban_user", "tier": "critical"}], "permissions.rules[0] must be an object, got int"),
])
def test_a_malformed_rules_block_is_the_parse_s_error_not_a_crash(tmp_path, rules, error):
    root = _folder(tmp_path, _manifest(permissions={"default_tier": "standard", "rules": rules}))
    out = ci.check_package(root)
    assert out["ok"] is False
    assert any(error in e for e in out["errors"]), out["errors"]


def test_a_credential_token_in_agent_context_is_a_warning(tmp_path):
    # F56: the token renders empty at runtime, so it is flagged, not refused.
    base = ci.check_package(_folder(tmp_path, _manifest()))
    out = ci.check_package(_folder(tmp_path, _manifest(agent_context=[{"template": "${credential.X}"}])))
    assert out["ok"] == base["ok"]
    assert any("credential.X" in w for w in out["warnings"]), out["warnings"]
    assert out["errors"] == base["errors"]


def test_a_remote_package(tmp_path):
    root = _folder(tmp_path, _manifest(version="1.0.0", server={
        "transport": "streamable_http", "url_template": "https://mcp.vendor.com/mcp",
        "source": "remote:mcp.vendor.com",
    }))
    out = ci.check_package(root)
    assert out["ok"] is True, out["errors"]
    assert out["summary"]["runtime"] == "remote" and out["summary"]["host"] == "mcp.vendor.com"


def test_no_manifest(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    out = ci.check_package(root)
    assert out["ok"] is False and "manifest.json not found" in out["errors"][0]


def test_a_skills_block_that_is_not_a_list_is_an_error_not_a_crash(tmp_path):
    root = _folder(tmp_path, _manifest(skills=5))
    out = ci.check_package(root)
    assert out["ok"] is False
    assert any("skills" in e for e in out["errors"]), out["errors"]


def test_a_git_source_at_a_tag_is_a_catalog_warning(tmp_path):
    root = _folder(tmp_path, _manifest(version="1.2.0", server={
        "runtime": "python", "transport": "stdio", "command": "venv/bin/python",
        "args": ["-m", "weather"], "source": "git+https://github.com/example/weather.git@v1.2.0",
    }))
    out = ci.check_package(root)
    assert out["ok"] is True
    assert any("full 40-character commit" in w for w in out["warnings"])
    # The schema's own pattern error for the same field is not repeated.
    assert not any(w.startswith("catalog schema: /server/source") for w in out["warnings"])


def test_the_platform_floor_a_package_needs_is_computed_and_named(tmp_path):
    root = _folder(tmp_path, _manifest(result_files={"tools": ["weather_map"], "subdir": "maps"}))
    out = ci.check_package(root)
    assert out["summary"]["platform_floor"] == "1.8.0"
    assert any("platform_min_version should be at least 1.8.0" in w and "/result_files (1.8.0)" in w
               for w in out["warnings"])
    root = _folder(tmp_path, _manifest(result_files={"tools": ["weather_map"], "subdir": "maps"},
                                       platform_min_version="1.8.0"))
    assert not any("platform_min_version" in w for w in ci.check_package(root)["warnings"])


def test_a_schema_violation_the_installer_lets_through_is_a_warning(tmp_path):
    root = _folder(tmp_path, _manifest(audience="viewer"))
    out = ci.check_package(root)
    assert any(w.startswith("catalog schema: /audience") for w in out["warnings"])



def test_a_bundled_skills_front_matter_is_held_to_the_skill_schema_as_warnings(tmp_path):
    # The installer refuses none of it (the scrub keeps the declarative
    # keys); the author is told what an engine would stumble on.
    skills = [{"id": "good", "file": "skills/good/SKILL.md"}, {"id": "bad", "file": "skills/bad/SKILL.md"},
              {"id": "flat", "file": "skills/flat.md"}, {"id": "broken", "file": "skills/broken/SKILL.md"},
              {"id": "other", "file": "skills/other/SKILL.md"}]
    root = _folder(tmp_path, _manifest(skills=skills), files={
        "skills/good/SKILL.md": "---\nname: good\ndescription: Does good things.\nallowed-tools: Bash\n---\nBody\n",
        "skills/bad/SKILL.md": "---\nname: Bad Name\ndescription: ''\n---\nBody\n",
        "skills/flat.md": "A flat skill file the platform frames itself.\n",
        "skills/broken/SKILL.md": "---\nname: broken\ndescription: [unclosed\n---\nBody\n",
        "skills/other/SKILL.md": "---\nname: elsewhere\ndescription: Named apart.\n---\nBody\n",
    })
    out = ci.check_package(root)
    assert out["errors"] == [], out["errors"]
    warnings = [w for w in out["warnings"] if "front matter" in w]
    assert any(w.startswith("skills/bad/SKILL.md front matter: name: invalid skill name 'Bad Name'")
               for w in warnings), warnings
    assert any(w.startswith("skills/bad/SKILL.md front matter: description must not be empty") for w in warnings)
    assert any(w.startswith("skills/broken/SKILL.md front matter is not valid YAML") for w in warnings)
    assert "skills/other/SKILL.md front matter: name 'elsewhere' is not the skill's id 'other'" in warnings
    assert not any("skills/good" in w or "skills/flat" in w for w in warnings), warnings
    # Never a schema path or jsonschema's own words.
    assert not any("does not match" in w or "$defs" in w for w in out["warnings"])
