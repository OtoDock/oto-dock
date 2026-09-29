"""Installer manifest validation — remote (vendor-hosted) MCPs.

``server.source: remote:*`` MCPs (linear/slack/zoom) run nothing locally, so
runtime/command must not be required; a URL + non-stdio transport are what the
connection actually needs. Locally-run MCPs keep the strict runtime checks.
"""

import pytest

from services.community.community_installer import _validate_manifest


def _base(server: dict) -> dict:
    return {
        "name": "some-mcp", "label": "Some MCP", "description": "d",
        "version": "1.0.0", "category": "community", "server": server,
    }


def test_remote_manifest_valid_without_runtime():
    errors = _validate_manifest(_base({
        "transport": "streamable_http",
        "url_template": "https://mcp.linear.app/mcp",
        "source": "remote:mcp.linear.app",
    }))
    assert errors == []


def test_remote_manifest_requires_url_template():
    errors = _validate_manifest(_base({
        "transport": "streamable_http",
        "source": "remote:mcp.linear.app",
    }))
    assert any("url_template" in e for e in errors)


def test_remote_manifest_rejects_stdio_transport():
    errors = _validate_manifest(_base({
        "url_template": "https://mcp.zoom.us/mcp",
        "source": "remote:mcp.zoom.us",
    }))
    assert any("non-stdio" in e for e in errors)


def test_local_manifest_still_requires_runtime():
    errors = _validate_manifest(_base({
        "transport": "stdio",
        "source": "npm:some-pkg",
    }))
    assert any("server.runtime" in e for e in errors)


_CONTEXT_ONLY = {"runtime": "none", "transport": "none"}


@pytest.mark.parametrize("server", ["a string", 42, None, ["list"]])
def test_malformed_server_block_is_an_error_not_an_exception(server):
    assert _validate_manifest(_base(server))


def test_non_string_source_is_an_error():
    errors = _validate_manifest(_base({
        "runtime": "node", "transport": "stdio", "command": "x", "source": 42,
    }))
    assert any("server.source" in e for e in errors)


@pytest.mark.parametrize("skills", ["nope", 42, [None], ["s"], [{"file": "f.md"}],
                                    [{"id": "x-skill"}], [{"id": 7, "file": "f.md"}]])
def test_malformed_skills_block_is_an_error_not_an_exception(skills):
    data = _base(dict(_CONTEXT_ONLY))
    data["skills"] = skills
    assert _validate_manifest(data)


@pytest.mark.parametrize("bad", [
    "/opt/otodock/config.env", "/proc/self/environ",
    "../../../agents/x/knowledge/.credentials/gmail/token.json",
    "skills/../../../../etc/passwd", "C:\\x", "./x", "a\nb",
])
def test_unconfined_skill_file_is_an_error(bad):
    """The install gate names every skills[].file that is not a plain
    relative path, so the admin sees a 400 instead of a silent install."""
    data = _base(dict(_CONTEXT_ONLY))
    data["skills"] = [{"id": "weather-usage", "file": bad}]
    errors = _validate_manifest(data)
    assert any("relative path inside the MCP folder" in e for e in errors), errors


def test_confined_skill_file_passes():
    data = _base(dict(_CONTEXT_ONLY))
    data["skills"] = [{"id": "weather-usage", "file": "skills/weather-usage/SKILL.md"}]
    assert _validate_manifest(data) == []


@pytest.mark.parametrize("bad", ["antlr4", [42], ["--no-binary"], ["a b"], [""]])
def test_source_build_must_be_a_list_of_package_names(bad):
    errors = _validate_manifest(_base({
        "runtime": "python", "transport": "stdio", "command": "x",
        "source": "pypi:pkg", "source_build": bad,
    }))
    assert any("source_build" in e for e in errors), errors
    assert _validate_manifest(_base({
        "runtime": "python", "transport": "stdio", "command": "x",
        "source": "pypi:pkg", "source_build": ["antlr4-python3-runtime"],
    })) == []


@pytest.mark.parametrize("runtime", ["python", "node"])
def test_community_manifest_that_installs_on_host_needs_a_source(runtime):
    """Without a package source the folder's own requirements would be
    built at the next proxy start; the catalog contract is a package pointer."""
    errors = _validate_manifest(_base({
        "runtime": runtime, "transport": "stdio", "command": "x",
    }))
    assert any("server.source" in e for e in errors), errors
    ok = _validate_manifest(_base({
        "runtime": runtime, "transport": "stdio", "command": "x",
        "source": "npm:pkg" if runtime == "node" else "pypi:pkg",
    }))
    assert ok == []
