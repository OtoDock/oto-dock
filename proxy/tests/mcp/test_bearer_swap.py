"""HTTP bearer-swap for proxy-terminable MCPs (github/m365).

The real upstream bearer is lifted out of the config FILE into the in-memory
``SecretBundle`` (see ``test_mcp_broker_activation`` for the build-side strip);
each spawn path then re-materializes it:

- **local claude**  (``sandbox.prepare_mcp_config_for_sandbox``) → real bearer
  inline in the per-session sandbox copy (trusted proxy host).
- **local codex**   (``codex.layer._inject_real_bearers_toml``) → same, in TOML.
- **remote claude** (``remote_execution._rewrite_mcp_json_for_remote``) → the
  per-session JWT (the tunnel ``_dispatch`` swaps it for the real token).
- **remote codex**  (``remote_execution._rewrite_mcp_toml_for_remote``) → same.
- **tunnel boundary** (``satellite_http_tunnel._swap_brokered_bearer``) → JWT →
  real upstream token, so the real secret never reaches the satellite.

Vendor HTTP MCPs (external host: slack/notion/…) are NOT in ``bearer_swap_keys``
and keep their inline bearer untouched — direct-to-vendor, gated on the
streamable-HTTP-over-tunnel fix.
"""

import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core.credentials.mcp_broker import SecretBundle, BROKER_BEARER_PLACEHOLDER  # noqa: E402

_SENTINEL = f"Bearer {BROKER_BEARER_PLACEHOLDER}"


# ---------------------------------------------------------------------------
# remote claude — _rewrite_mcp_json_for_remote
# ---------------------------------------------------------------------------


def _json_config():
    return {"mcpServers": {
        "github": {
            "type": "streamable-http",
            "url": "http://localhost:8935/mcp",
            "headers": {"Authorization": _SENTINEL},
        },
        "slack": {  # vendor, external host — direct-to-vendor
            "type": "streamable-http",
            "url": "https://mcp.slack.com/mcp",
            "headers": {"Authorization": "Bearer xoxb-real-slack"},
        },
        "file-tools": {  # tunneled docker MCP, no bearer
            "type": "sse",
            "url": "http://localhost:8932/sse",
        },
    }}


def test_remote_json_github_gets_session_jwt_and_tunnel_url():
    from core.remote.remote_execution import _rewrite_mcp_json_for_remote
    out = _rewrite_mcp_json_for_remote(
        _json_config(), 9000, session_id="sid1",
        bearer_swap_keys={"github"}, proxy_api_key="JWT123",
    )
    gh = out["mcpServers"]["github"]
    # Bearer is the per-session JWT (never the real token); URL tunnel-routed.
    assert gh["headers"]["Authorization"] == "Bearer JWT123"
    assert gh["url"] == "http://127.0.0.1:9000/mcp/github/mcp?session_id=sid1"


def test_remote_json_vendor_bearer_untouched():
    from core.remote.remote_execution import _rewrite_mcp_json_for_remote
    out = _rewrite_mcp_json_for_remote(
        _json_config(), 9000, session_id="sid1",
        bearer_swap_keys={"github"}, proxy_api_key="JWT123",
    )
    sl = out["mcpServers"]["slack"]
    # External vendor: real bearer stays, URL not tunneled (accepted residual).
    assert sl["headers"]["Authorization"] == "Bearer xoxb-real-slack"
    assert sl["url"] == "https://mcp.slack.com/mcp"


def test_remote_json_no_bearer_mcp_untouched():
    from core.remote.remote_execution import _rewrite_mcp_json_for_remote
    out = _rewrite_mcp_json_for_remote(
        _json_config(), 9000, session_id="sid1",
        bearer_swap_keys={"github"}, proxy_api_key="JWT123",
    )
    ft = out["mcpServers"]["file-tools"]
    # Tunneled but no bearer to swap — no Authorization header introduced.
    assert "Authorization" not in ft.get("headers", {})


# ---------------------------------------------------------------------------
# remote codex — _rewrite_mcp_toml_for_remote
# ---------------------------------------------------------------------------


_TOML = (
    "[mcp_servers.github]\n"
    "startup_timeout_sec = 10\n"
    'url = "http://localhost:8935/mcp"\n'
    "[mcp_servers.github.http_headers]\n"
    f'"Authorization" = "{_SENTINEL}"\n'
    "\n"
    "[mcp_servers.slack]\n"
    "startup_timeout_sec = 10\n"
    'url = "https://mcp.slack.com/mcp"\n'
    "[mcp_servers.slack.http_headers]\n"
    '"Authorization" = "Bearer xoxb-real-slack"\n'
)


def test_remote_toml_github_http_headers_get_session_jwt():
    from core.remote.remote_execution import _rewrite_mcp_toml_for_remote
    out = _rewrite_mcp_toml_for_remote(
        _TOML, 9000, session_id="sid1", proxy_api_key="JWT123",
        bearer_swap_keys={"github"},
    )
    assert '"Authorization" = "Bearer JWT123"' in out
    assert BROKER_BEARER_PLACEHOLDER not in out  # sentinel replaced
    assert "http://127.0.0.1:9000/mcp/github/mcp?session_id=sid1" in out


def test_remote_toml_vendor_http_headers_untouched():
    from core.remote.remote_execution import _rewrite_mcp_toml_for_remote
    out = _rewrite_mcp_toml_for_remote(
        _TOML, 9000, session_id="sid1", proxy_api_key="JWT123",
        bearer_swap_keys={"github"},
    )
    # Vendor bearer + external URL survive verbatim.
    assert '"Authorization" = "Bearer xoxb-real-slack"' in out
    assert 'url = "https://mcp.slack.com/mcp"' in out


# ---------------------------------------------------------------------------
# local codex — _inject_real_bearers_toml
# ---------------------------------------------------------------------------


def test_local_codex_swaps_sentinel_for_real_bearer():
    from core.layers.codex.layer import _inject_real_bearers_toml
    bundles = {"github": SecretBundle(http_bearer="github_pat_REAL")}
    out = _inject_real_bearers_toml(_TOML, bundles)
    assert '"Authorization" = "Bearer github_pat_REAL"' in out
    assert BROKER_BEARER_PLACEHOLDER not in out
    # Vendor (no bundle bearer) untouched.
    assert '"Authorization" = "Bearer xoxb-real-slack"' in out


def test_local_codex_noop_without_bundle_bearer():
    from core.layers.codex.layer import _inject_real_bearers_toml
    # No bundle carries an http_bearer → the sentinel is left as-is (a session
    # with no proxy-terminable MCP, or all-stdio bundles).
    out = _inject_real_bearers_toml(_TOML, {"github": SecretBundle(env={"X": "1"})})
    assert _SENTINEL in out


# ---------------------------------------------------------------------------
# local claude — prepare_mcp_config_for_sandbox
# ---------------------------------------------------------------------------


def test_local_claude_swaps_sentinel_for_real_bearer(tmp_path):
    import json
    from core.sandbox.sandbox import prepare_mcp_config_for_sandbox

    cfg = {"mcpServers": {
        "github": {
            "type": "streamable-http",
            "url": "http://localhost:8935/mcp",
            "headers": {"Authorization": _SENTINEL},
        },
        "file-tools": {"type": "sse", "url": "http://localhost:8932/sse"},
    }}
    src = tmp_path / "agent-abc.json"
    src.write_text(json.dumps(cfg))
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()

    prepare_mcp_config_for_sandbox(
        str(src), str(cfg_dir), "/users/u/.claude",
        session_id="sid1",
        secret_bundles={"github": SecretBundle(http_bearer="github_pat_REAL")},
    )
    out = json.loads((cfg_dir / "agent-abc.json").read_text())
    gh = out["mcpServers"]["github"]
    assert gh["headers"]["Authorization"] == "Bearer github_pat_REAL"
    # No bundle for file-tools → untouched.
    assert "Authorization" not in out["mcpServers"]["file-tools"].get("headers", {})


# ---------------------------------------------------------------------------
# tunnel boundary — _swap_brokered_bearer
# ---------------------------------------------------------------------------


@pytest.fixture
def provisioned():
    """Provision a session bundle for github; purge after."""
    from core.credentials import mcp_broker
    sid = "tunnel-sid-1"
    mcp_broker.provision(sid, {
        "github": SecretBundle(http_bearer="github_pat_REAL"),
        "file-tools": SecretBundle(env={"X": "1"}),  # no http_bearer
    })
    yield sid
    mcp_broker.purge_session(sid)


def _jwt(sid):
    from auth.session_token import create_session_token
    return create_session_token(sid, "agent")


def test_dispatch_swaps_jwt_for_real_bearer(provisioned):
    from core.remote.satellite_http_tunnel import _swap_brokered_bearer
    headers = {"Authorization": f"Bearer {_jwt(provisioned)}"}
    _swap_brokered_bearer("/mcp/github/mcp", headers)
    assert headers["Authorization"] == "Bearer github_pat_REAL"


def test_dispatch_case_insensitive_header(provisioned):
    from core.remote.satellite_http_tunnel import _swap_brokered_bearer
    headers = {"authorization": f"Bearer {_jwt(provisioned)}"}
    _swap_brokered_bearer("/mcp/github/mcp", headers)
    # Lowercase variant dropped; canonical header carries the real token.
    assert headers.get("Authorization") == "Bearer github_pat_REAL"
    assert "authorization" not in headers


def test_dispatch_no_swap_when_mcp_has_no_bearer(provisioned):
    from core.remote.satellite_http_tunnel import _swap_brokered_bearer
    jwt = _jwt(provisioned)
    headers = {"Authorization": f"Bearer {jwt}"}
    _swap_brokered_bearer("/mcp/file-tools/sse", headers)
    # file-tools bundle has no http_bearer → JWT forwarded untouched.
    assert headers["Authorization"] == f"Bearer {jwt}"


def test_dispatch_store_miss_fails_closed():
    from core.remote.satellite_http_tunnel import _swap_brokered_bearer
    jwt = _jwt("never-provisioned-sid")
    headers = {"Authorization": f"Bearer {jwt}"}
    _swap_brokered_bearer("/mcp/github/mcp", headers)
    # No store entry → sentinel/JWT left in place (sidecar 401s; fail-closed).
    assert headers["Authorization"] == f"Bearer {jwt}"


def test_dispatch_non_jwt_authorization_untouched(provisioned):
    from core.remote.satellite_http_tunnel import _swap_brokered_bearer
    headers = {"Authorization": "Bearer not-a-jwt"}
    _swap_brokered_bearer("/mcp/github/mcp", headers)
    assert headers["Authorization"] == "Bearer not-a-jwt"


def test_dispatch_non_mcp_path_untouched(provisioned):
    from core.remote.satellite_http_tunnel import _swap_brokered_bearer
    headers = {"Authorization": f"Bearer {_jwt(provisioned)}"}
    _swap_brokered_bearer("/v1/hooks/permission", headers)
    # Hook paths never carry a brokered bearer — leave them alone.
    assert headers["Authorization"] == f"Bearer {_jwt(provisioned)}"
