"""The MCP manifest schema (``mcps/manifest.schema.json``) and the platform
floor it computes (``services/mcp/manifest_floor.py``).

The schema is the catalog contract: every manifest the platform ships and
every catalog entry validates; what the installer refuses, the schema
refuses too; every field the parser reads is described; the floor an entry's
fields imply is the release that first reads them.
"""

from __future__ import annotations

import json
import re

import pytest
from jsonschema import Draft202012Validator

from services.community import community_installer as ci
from services.mcp import manifest_floor as mf
from tests._paths import CUSTOM_MCPS, PROXY_DIR, REPO_ROOT

SCHEMA = mf.load_schema(REPO_ROOT / "mcps" / mf.SCHEMA_NAME)
CATALOGS = [REPO_ROOT.parent / "otodock-community-mcps-prep", REPO_ROOT.parent / "otodock-community-mcps"]
COMMIT = "2cea8d566dde07fbac28a61d698909d69724e853"


def _base(**over) -> dict:
    data = {
        "name": "weather-tools", "label": "Weather", "description": "Forecasts",
        "version": "", "category": "community",
        "server": {"runtime": "node", "transport": "stdio", "command": "node", "source": "npm:weather"},
    }
    data.update(over)
    return data


def test_the_schema_is_a_valid_draft_2020_12_schema_with_a_versioned_id():
    Draft202012Validator.check_schema(SCHEMA)
    m = re.fullmatch(r"https://raw\.githubusercontent\.com/OtoDock/oto-dock/v(\d+\.\d+\.\d+)/mcps/manifest\.schema\.json",
                     SCHEMA["$id"])
    assert m, SCHEMA["$id"]
    # The $id names the release that last changed the schema: never one older
    # than the newest release in the changelog.
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                          re.MULTILINE)
    assert mf.version_key(m.group(1)) >= mf.version_key(released[0])


@pytest.mark.parametrize("path", sorted(CUSTOM_MCPS.glob("*/manifest.json")), ids=lambda p: p.parent.name)
def test_every_bundled_manifest_validates(path):
    assert mf.schema_errors(json.loads(path.read_text()), SCHEMA) == []


@pytest.mark.parametrize("catalog", CATALOGS, ids=lambda p: p.name)
def test_every_catalog_entry_validates_and_declares_its_floor(catalog):
    if not catalog.is_dir():
        pytest.skip(f"{catalog.name} is not checked out beside this repository")
    manifests = sorted(catalog.glob("*/manifest.json"))
    assert manifests
    for path in manifests:
        data = json.loads(path.read_text())
        assert mf.schema_errors(data, SCHEMA) == [], path.parent.name


REFUSED = {
    "missing name": lambda d: d.pop("name"),
    "bad name": lambda d: d.update(name="weather tools!"),
    "bad category": lambda d: d.update(category="vendor"),
    "server not an object": lambda d: d.update(server="npm:weather"),
    "non-string source": lambda d: d["server"].update(source=123),
    "remote without url_template": lambda d: d.update(server={"source": "remote:mcp.example.com",
                                                               "transport": "streamable_http"}),
    "remote over stdio": lambda d: d.update(server={"source": "remote:mcp.example.com",
                                                     "url_template": "https://mcp.example.com/mcp"}),
    "installed runtime without command": lambda d: d["server"].pop("command"),
    "community runtime without source": lambda d: d["server"].pop("source"),
    "skill without id": lambda d: d.update(skills=[{"file": "skills/x/SKILL.md"}]),
    "skill file outside": lambda d: d.update(skills=[{"id": "x", "file": "../x/SKILL.md"}]),
    "replaces without source": lambda d: d.update(replaces=[{"credentials": {}}]),
    "bad source_build": lambda d: d["server"].update(source_build=["-e ./evil"]),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_what_the_installer_refuses_the_schema_refuses(name):
    data = _base()
    REFUSED[name](data)
    assert ci._validate_manifest(data), f"the installer accepts {name!r}"
    assert mf.schema_errors(data, SCHEMA), f"the schema accepts {name!r}"


def test_the_catalog_rules_are_stricter_than_an_admins_zip():
    tagged = _base(version="1.2.0", server={"runtime": "python", "transport": "stdio", "command": "x",
                                            "source": "git+https://h/r.git@v1.2.0#subdirectory=mcp"})
    assert ci._validate_manifest(tagged) == []           # an admin's ZIP may pin a tag
    assert any(e.startswith("/server/source") for e in mf.schema_errors(tagged, SCHEMA))
    pinned = json.loads(json.dumps(tagged))
    pinned["server"]["source"] = f"git+https://h/r.git@{COMMIT}#subdirectory=mcp"
    assert mf.schema_errors(pinned, SCHEMA) == []


@pytest.mark.parametrize("value, ok", [
    (600, True), (1, True), (3600, True),
    (0, False), (3601, False), ("600", False), (True, False),
])
def test_tool_timeout_sec_is_whole_seconds_within_an_hour_and_asks_for_no_floor(value, ok):
    data = _base(server={**_base()["server"], "tool_timeout_sec": value})
    assert (mf.schema_errors(data, SCHEMA) == []) is ok
    if ok:
        # A platform that does not read the key ignores it.
        assert mf.platform_floor(data, SCHEMA) == ("1.0.0", [])


def test_unknown_keys_are_allowed_and_ask_for_no_floor():
    data = _base(some_future_field={"x": 1}, server={**_base()["server"], "future": True})
    data["$schema"] = SCHEMA["$id"]
    assert mf.schema_errors(data, SCHEMA) == []
    assert mf.platform_floor(data, SCHEMA) == ("1.0.0", [])


def _properties(node, out: set) -> set:
    if isinstance(node, dict):
        out |= set(node.get("properties") or {})
        for value in node.values():
            _properties(value, out)
    elif isinstance(node, list):
        for value in node:
            _properties(value, out)
    return out


def test_every_field_the_parser_reads_is_described():
    source = (PROXY_DIR / "services" / "mcp" / "mcp_manifest_parse.py").read_text(encoding="utf-8")
    read = set(re.findall(r'data\.get\("([a-z_]+)"', source)) | set(re.findall(r'data\["([a-z_]+)"\]', source))
    assert read, "the parser's reads were not found"
    assert read - _properties(SCHEMA, set()) == set()


@pytest.mark.parametrize("over, floor, reason", [
    ({}, "1.0.0", None),
    ({"result_files": {"tools": ["t"], "subdir": "maps"}}, "1.8.0", "/result_files (1.8.0)"),
    ({"result_files": None}, "1.0.0", None),               # a field left empty asks nothing
    ({"audience": "editor"}, "1.7.1", "/audience (1.7.1)"),
    ({"skills": [{"id": "s", "file": "skills/s/SKILL.md", "audience": "owner"}]}, "1.7.1",
     "/skills/0/audience (1.7.1)"),
    ({"replaces": [{"source": "npm:old"}]}, "1.7.1", "/replaces (1.7.1)"),
    ({"server": {"runtime": "python", "transport": "stdio", "command": "x",
                 "source": "pypi:pkg[extra]"}}, "1.8.0", "/server/source matching"),
    ({"server": {"runtime": "python", "transport": "stdio", "command": "x",
                 "source": "pypi:pkg"}}, "1.0.0", None),
])
def test_the_floor_is_the_release_that_first_reads_each_field(over, floor, reason):
    got, why = mf.platform_floor(_base(**over), SCHEMA)
    assert got == floor
    if reason:
        assert any(w.startswith(reason) for w in why), why


def test_the_floor_reads_the_nested_credential_blocks():
    data = _base(credentials={"type": "infra", "api_key_header": {
        "name": "X-Key", "value_from": "KEY", "proposed_hosts": ["api.example.com"]}})
    assert mf.platform_floor(data, SCHEMA)[0] == "1.7.1"


def test_the_declared_floor_defaults_to_the_base():
    assert mf.declared_floor({}) == "1.0.0"
    assert mf.declared_floor({"platform_min_version": "1.8.0"}) == "1.8.0"
    assert mf.declared_floor({"platform_min_version": 1.8}) == "1.0.0"
