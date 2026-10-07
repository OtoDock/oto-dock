"""``credentials.api_key_header``: a vendor-hosted MCP that takes its key in
a header of its own. The parser accepts the declaration and refuses the
shapes the gateway cannot honour; the package check reports the same.
"""

import copy
import json
import sys

import pytest

from tests._paths import PROXY_DIR
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from services.mcp import mcp_manifest_parse as mp  # noqa: E402

_BASE = {
    "name": "maps-grounding",
    "label": "Maps Grounding Lite",
    "description": "Places, weather and routes",
    "version": "1.0.0",
    "category": "community",
    "server": {
        "transport": "streamable_http",
        "url_template": "https://mapstools.googleapis.com/mcp",
        "source": "remote:mapstools.googleapis.com",
    },
    "credentials": {
        "type": "infra",
        "fields": [{"key": "GOOGLE_MAPS_API_KEY", "label": "Maps API key", "input_type": "password"}],
        "api_key_header": {
            "name": "X-Goog-Api-Key",
            "value_from": "GOOGLE_MAPS_API_KEY",
            "proposed_hosts": ["mapstools.googleapis.com"],
        },
    },
}


def _write(tmp_path, data):
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    return tmp_path / "manifest.json"


def _variant(**changes):
    d = copy.deepcopy(_BASE)
    for dotted, value in changes.items():
        cur = d
        parts = dotted.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if value is None:
            cur.pop(parts[-1], None)
        else:
            cur[parts[-1]] = value
    return d


def test_the_declaration_parses_into_the_credential_config(tmp_path):
    m = mp._parse_manifest(_write(tmp_path, _BASE))
    assert m.credentials.api_key_header == _BASE["credentials"]["api_key_header"]
    assert m.credentials.oauth is None and m.audience == ""


def test_an_env_delivered_instance_field_is_an_accepted_source(tmp_path):
    d = _variant(**{"credentials.fields": [], "credentials.type": "none"})
    d["instances"] = {"delivery": "env", "fields": [{"key": "GOOGLE_MAPS_API_KEY", "label": "k",
                                                       "input_type": "password"}]}
    m = mp._parse_manifest(_write(tmp_path, d))
    assert m.credentials.api_key_header["value_from"] == "GOOGLE_MAPS_API_KEY"


@pytest.mark.parametrize("changes, fragment", [
    ({"credentials.api_key_header": "X-Goog-Api-Key"}, "must be an object"),
    ({"credentials.api_key_header.name": None}, "name is required"),
    ({"credentials.api_key_header.name": "Authorization"}, "header the platform owns"),
    ({"credentials.api_key_header.name": "Cookie"}, "header the platform owns"),
    ({"credentials.api_key_header.name": "Mcp-Session-Id"}, "header the platform owns"),
    ({"credentials.api_key_header.name": "X-Forwarded-For"}, "header the platform owns"),
    ({"credentials.api_key_header.name": "bad header"}, "HTTP header name"),
    ({"credentials.api_key_header.value_from": "NOT_DECLARED"}, "must be a credentials.fields key"),
    ({"credentials.fields": [{"key": "GOOGLE_MAPS_API_KEY", "label": "k", "input_type": "text"}]},
     "input_type password"),
    ({"credentials.api_key_header.proposed_hosts": []}, "non-empty list"),
    ({"credentials.api_key_header.proposed_hosts": ["other.example.com"]}, "not among proposed_hosts"),
    ({"credentials.api_key_header.proposed_hosts": ["bad host!"]}, "not a valid hostname"),
    ({"credentials.api_key_header.extra": 1}, "unknown keys"),
    ({"server.transport": "sse"}, "streamable-HTTP"),
    ({"server.transport": "stdio"}, "streamable-HTTP"),
    ({"server.url_template": "https://${config:maps:host}/mcp"}, "literal URL"),
    ({"server.url_template": "mapstools.googleapis.com/mcp"}, "http(s) URL"),
    ({"credentials.oauth": {"provider_id": "maps", "bearer_required": True,
                            "proposed_hosts": ["mapstools.googleapis.com"]}},
     "beside credentials.oauth.bearer_required"),
    ({"hosted": {"api_key_relay": {"available": True, "relay_path": "/maps"}}},
     "beside hosted.api_key_relay"),
])
def test_the_validator_refuses_what_the_gateway_cannot_honour(tmp_path, changes, fragment):
    with pytest.raises(ValueError) as info:
        mp._parse_manifest(_write(tmp_path, _variant(**changes)))
    assert "api_key_header" in str(info.value) and fragment in str(info.value)


def test_the_package_check_reports_the_refusal(tmp_path):
    from services.community import community_installer
    (tmp_path / "README.md").write_text("# maps\n")
    _write(tmp_path, _variant(**{"credentials.api_key_header.name": "Authorization"}))
    out = community_installer.check_package(tmp_path)
    assert out["ok"] is False
    assert any("api_key_header" in e for e in out["errors"])
    _write(tmp_path, _BASE)
    out = community_installer.check_package(tmp_path)
    assert out["ok"] is True, out["errors"]


def test_the_audience_field_is_validated(tmp_path):
    d = _variant()
    d["audience"] = "editor"
    assert mp._parse_manifest(_write(tmp_path, d)).audience == "editor"
    d["audience"] = "admin"
    with pytest.raises(ValueError) as info:
        mp._parse_manifest(_write(tmp_path, d))
    assert "audience must be one of" in str(info.value)
