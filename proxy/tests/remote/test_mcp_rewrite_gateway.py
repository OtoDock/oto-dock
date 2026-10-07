"""The remote rewriters on a credential-gateway entry, both formats: a
sidecar keeps the tunnel's ``/mcp/<name>/`` shape with the session id, a
vendor entry dials the machine's loopback gateway when the machine runs
one and keeps the inline shape below the gateway version, and no vendor
ever receives a session token.
"""

import config
from auth.session_token import SESSION_JWT_SENTINEL_BEARER
from core.credentials.mcp_gateway import entry_url
from core.remote.remote_mcp_rewrite import (
    _rewrite_mcp_json_for_remote, _rewrite_mcp_toml_for_remote,
)
from services.mcp.mcp_registry import _servers_to_toml

SAT = 49152
SID = "11111111-2222-3333-4444-555555555555"
JWT = "eyJ.session.jwt"


def _servers():
    return {
        "vendor": {"type": "http", "url": entry_url("vendor", "/mcp"),
                   "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
        "maps": {"type": "http", "url": entry_url("maps", "/mcp"),
                 "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
        "github-mcp": {"type": "http", "url": entry_url("github-mcp", "/mcp"),
                       "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
        "file-tools": {"type": "http", "url": "http://localhost:8932/mcp/",
                       "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
    }


def _facts(gateway_mode: bool):
    return {
        "vendor": {"upstream": "https://mcp.example.com", "path": "/mcp", "header": "Authorization",
                   "proxy_local": False, "value": "" if gateway_mode else "Bearer xoxb-real"},
        "maps": {"upstream": "https://mapstools.googleapis.com", "path": "/mcp",
                 "header": "X-Goog-Api-Key", "proxy_local": False,
                 "value": "" if gateway_mode else "AIza-1"},
        "github-mcp": {"upstream": "http://localhost:8935", "path": "/mcp", "header": "Authorization",
                       "proxy_local": True, "value": ""},
    }


def test_json_on_a_machine_that_runs_the_gateway():
    out = _rewrite_mcp_json_for_remote(
        {"mcpServers": _servers()}, SAT, session_id=SID, proxy_api_key=JWT,
        gateway=_facts(True), gateway_mode=True,
    )["mcpServers"]
    assert out["vendor"]["url"] == f"http://127.0.0.1:{SAT}/v1/mcp-gateway/vendor/mcp"
    assert out["vendor"]["headers"]["Authorization"] == f"Bearer {JWT}"
    assert out["maps"]["url"] == f"http://127.0.0.1:{SAT}/v1/mcp-gateway/maps/mcp"
    assert out["github-mcp"]["url"] == f"http://127.0.0.1:{SAT}/mcp/github-mcp/mcp?session_id={SID}"
    assert out["github-mcp"]["headers"]["Authorization"] == f"Bearer {JWT}"
    assert out["file-tools"]["url"] == f"http://127.0.0.1:{SAT}/mcp/file-tools/mcp/?session_id={SID}"
    text = str(out)
    assert "xoxb-real" not in text and "AIza-1" not in text and f"127.0.0.1:{config.PORT}" not in text


def test_json_below_the_gateway_version_keeps_the_inline_shape_and_no_session_token_reaches_a_vendor():
    out = _rewrite_mcp_json_for_remote(
        {"mcpServers": _servers()}, SAT, session_id=SID, proxy_api_key=JWT,
        gateway=_facts(False), gateway_mode=False,
    )["mcpServers"]
    assert out["vendor"]["url"] == "https://mcp.example.com/mcp"
    assert out["vendor"]["headers"] == {"Authorization": "Bearer xoxb-real"}
    assert out["maps"]["url"] == "https://mapstools.googleapis.com/mcp"
    assert out["maps"]["headers"] == {"X-Goog-Api-Key": "AIza-1"}
    assert out["github-mcp"]["url"] == f"http://127.0.0.1:{SAT}/mcp/github-mcp/mcp?session_id={SID}"
    assert out["github-mcp"]["headers"]["Authorization"] == f"Bearer {JWT}"
    for name in ("vendor", "maps"):
        assert JWT not in str(out[name])


def test_json_an_inline_entry_with_no_value_carries_no_credential_header():
    facts = _facts(False)
    facts["maps"]["value"] = ""
    out = _rewrite_mcp_json_for_remote(
        {"mcpServers": _servers()}, SAT, session_id=SID, proxy_api_key=JWT,
        gateway=facts, gateway_mode=False,
    )["mcpServers"]
    assert out["maps"]["url"] == "https://mapstools.googleapis.com/mcp" and "headers" not in out["maps"]


def test_toml_on_a_machine_that_runs_the_gateway():
    toml = _servers_to_toml({k: {**v, "type": "streamable-http"} for k, v in _servers().items()})
    out = _rewrite_mcp_toml_for_remote(
        toml, SAT, session_id=SID, proxy_api_key=JWT, gateway=_facts(True), gateway_mode=True,
    )
    assert f'url = "http://127.0.0.1:{SAT}/v1/mcp-gateway/vendor/mcp"' in out
    assert f'url = "http://127.0.0.1:{SAT}/v1/mcp-gateway/maps/mcp"' in out
    assert f'url = "http://127.0.0.1:{SAT}/mcp/github-mcp/mcp?session_id={SID}"' in out
    assert f'url = "http://127.0.0.1:{SAT}/mcp/file-tools/mcp/?session_id={SID}"' in out
    assert out.count(f'"Authorization" = "Bearer {JWT}"') == 4
    assert "xoxb-real" not in out and "AIza-1" not in out and f"127.0.0.1:{config.PORT}" not in out


def test_toml_below_the_gateway_version_keeps_the_inline_shape():
    toml = _servers_to_toml({k: {**v, "type": "streamable-http"} for k, v in _servers().items()})
    out = _rewrite_mcp_toml_for_remote(
        toml, SAT, session_id=SID, proxy_api_key=JWT, gateway=_facts(False), gateway_mode=False,
    )
    assert 'url = "https://mcp.example.com/mcp"' in out
    assert '"Authorization" = "Bearer xoxb-real"' in out
    assert 'url = "https://mapstools.googleapis.com/mcp"' in out
    assert '"X-Goog-Api-Key" = "AIza-1"' in out
    # the session token reaches the sidecar and file-tools only
    assert out.count(f'"Authorization" = "Bearer {JWT}"') == 2
    vendor_section = out.split("[mcp_servers.vendor]")[1].split("[mcp_servers.maps]")[0]
    assert JWT not in vendor_section and SESSION_JWT_SENTINEL_BEARER not in vendor_section
