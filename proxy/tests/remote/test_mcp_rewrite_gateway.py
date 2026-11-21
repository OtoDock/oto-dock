"""The remote rewriters on a credential-gateway entry, both formats: a
sidecar keeps the tunnel's ``/mcp/<name>/`` shape with the session id, a
vendor entry dials the machine's loopback gateway, and no entry dials a
vendor directly.
"""

import config
from auth.session_token import SESSION_JWT_SENTINEL_BEARER
from core.credentials.mcp_gateway import entry_url
from core.remote.remote_mcp_rewrite import (
    _rewrite_mcp_json_for_remote,
    _rewrite_mcp_toml_for_remote,
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


def _facts():
    return {
        "vendor": {"upstream": "https://mcp.example.com", "path": "/mcp", "header": "Authorization",
                   "proxy_local": False},
        "maps": {"upstream": "https://mapstools.googleapis.com", "path": "/mcp",
                 "header": "X-Goog-Api-Key", "proxy_local": False},
        "github-mcp": {"upstream": "http://localhost:8935", "path": "/mcp", "header": "Authorization",
                       "proxy_local": True},
    }


def test_json_dials_the_machine_gateway():
    out = _rewrite_mcp_json_for_remote(
        {"mcpServers": _servers()}, SAT, session_id=SID, proxy_api_key=JWT, gateway=_facts(),
    )["mcpServers"]
    assert out["vendor"]["url"] == f"http://127.0.0.1:{SAT}/v1/mcp-gateway/vendor/mcp"
    assert out["vendor"]["headers"]["Authorization"] == f"Bearer {JWT}"
    assert out["maps"]["url"] == f"http://127.0.0.1:{SAT}/v1/mcp-gateway/maps/mcp"
    assert out["github-mcp"]["url"] == f"http://127.0.0.1:{SAT}/mcp/github-mcp/mcp?session_id={SID}"
    assert out["github-mcp"]["headers"]["Authorization"] == f"Bearer {JWT}"
    assert out["file-tools"]["url"] == f"http://127.0.0.1:{SAT}/mcp/file-tools/mcp/?session_id={SID}"
    text = str(out)
    assert "mcp.example.com" not in text and "googleapis" not in text
    assert f"127.0.0.1:{config.PORT}" not in text


def test_toml_dials_the_machine_gateway():
    toml = _servers_to_toml({k: {**v, "type": "streamable-http"} for k, v in _servers().items()})
    out = _rewrite_mcp_toml_for_remote(
        toml, SAT, session_id=SID, proxy_api_key=JWT, gateway=_facts(),
    )
    assert f'url = "http://127.0.0.1:{SAT}/v1/mcp-gateway/vendor/mcp"' in out
    assert f'url = "http://127.0.0.1:{SAT}/v1/mcp-gateway/maps/mcp"' in out
    assert f'url = "http://127.0.0.1:{SAT}/mcp/github-mcp/mcp?session_id={SID}"' in out
    assert f'url = "http://127.0.0.1:{SAT}/mcp/file-tools/mcp/?session_id={SID}"' in out
    assert out.count(f'"Authorization" = "Bearer {JWT}"') == 4
    assert "mcp.example.com" not in out and "googleapis" not in out
    assert f"127.0.0.1:{config.PORT}" not in out


def _narrowed_facts():
    facts = _facts()
    facts["vendor"] = {**facts["vendor"], "tunneled": True}
    return facts


def test_a_narrowed_vendor_entry_goes_through_the_tunnel():
    out = _rewrite_mcp_json_for_remote(
        {"mcpServers": _servers()}, SAT, session_id=SID, proxy_api_key=JWT, gateway=_narrowed_facts(),
    )["mcpServers"]
    assert out["vendor"]["url"] == f"http://127.0.0.1:{SAT}/mcp/vendor/mcp?session_id={SID}"
    assert out["vendor"]["headers"]["Authorization"] == f"Bearer {JWT}"
    toml = _servers_to_toml({k: {**v, "type": "streamable-http"} for k, v in _servers().items()})
    out = _rewrite_mcp_toml_for_remote(
        toml, SAT, session_id=SID, proxy_api_key=JWT, gateway=_narrowed_facts(),
    )
    vendor_section = out.split("[mcp_servers.vendor]")[1].split("[mcp_servers.maps]")[0]
    assert f'url = "http://127.0.0.1:{SAT}/mcp/vendor/mcp?session_id={SID}"' in vendor_section
    assert f'"Authorization" = "Bearer {JWT}"' in vendor_section


def test_the_start_facts_are_secret_free_and_a_narrowed_credential_rides_the_tunnel():
    from core.credentials import mcp_broker
    from core.credentials.mcp_gateway import GatewayCredential
    from core.remote.remote_start_payload import _gateway_facts
    plain = GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key="k",
                              value="xoxb-real")
    narrowed = GatewayCredential(upstream="https://mcp.example.com", path="/mcp", allowlist_key="k",
                                 value="xoxb-real", reach={"repositories": [], "read_only": True})
    facts = _gateway_facts({"vendor": mcp_broker.SecretBundle(gateway=plain),
                            "narrowed": mcp_broker.SecretBundle(gateway=narrowed)})
    assert facts["vendor"]["tunneled"] is False and facts["narrowed"]["tunneled"] is True
    assert "xoxb-real" not in str(facts)



def test_a_live_token_stays_on_the_gateway_entries_and_the_callbacks_get_their_kind():
    """With a real session token (a fake key passes both branches unchanged):
    every gateway entry carries the token itself, which keys the machine's
    gateway table; a Docker tool's callback entry carries the callback
    kind minted from it, in both formats."""
    from auth.session_token import (
        CALLBACK_JWT_SENTINEL_BEARER,
        create_session_token,
        validate_callback_token,
        validate_session_token,
    )
    real = create_session_token(SID, "personal-assistant", "sub-9")
    servers = _servers()
    servers["file-tools"]["headers"]["Authorization"] = CALLBACK_JWT_SENTINEL_BEARER
    out = _rewrite_mcp_json_for_remote(
        {"mcpServers": servers}, SAT, session_id=SID, proxy_api_key=real, gateway=_facts(),
    )["mcpServers"]
    for name in ("vendor", "maps", "github-mcp"):
        assert out[name]["headers"]["Authorization"] == f"Bearer {real}", name
    callback = out["file-tools"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert callback != real and validate_session_token(callback) is None
    assert validate_callback_token(callback)["sid"] == SID
    toml = _rewrite_mcp_toml_for_remote(
        _servers_to_toml({k: {**v, "type": "streamable-http"} for k, v in servers.items()}),
        SAT, session_id=SID, proxy_api_key=real, gateway=_facts(),
    )
    assert toml.count(f'"Authorization" = "Bearer {real}"') == 3
    import re
    others = [b for b in re.findall(r'"Authorization" = "Bearer ([^"]+)"', toml) if b != real]
    assert len(others) == 1 and validate_callback_token(others[0])["sid"] == SID
