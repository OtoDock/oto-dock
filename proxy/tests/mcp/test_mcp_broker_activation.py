"""Credential-broker activation (Sub-step A: assembly + remote inject).

Two halves:

1. ``build_session_mcp_config`` now returns a 4th element — a per-MCP
   ``SecretBundle`` map keyed by the mcpServers key (server_name or mcp name),
   carrying that one MCP's resolver creds + instance field_values + HTTP bearer.
   Heavy collaborators (DB / credential resolver / server-config builder) are
   stubbed so this isolates the assembly logic.

2. The remote rewriters inject the per-(session, mcp) cap-token into each stdio
   server that has a bundle — and ONLY those — so the satellite wraps + fetches.
   The satellite-path resolvers are stubbed so this isolates token injection.

The secret-strip at source now removes the PURE secrets from the flat union + the
config files (each MCP gets them via its bundle); these tests assert both the broker plumbing and the
strip (flat env stripped, bundle filtered to secrets, OTO_STRIP_KEYS injected).
"""

import sys
from types import SimpleNamespace

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core.credentials import mcp_broker # noqa: E402


# ---------------------------------------------------------------------------
# Half 1 — bundle assembly in build_session_mcp_config
# ---------------------------------------------------------------------------


class _FakeManifest:
    def __init__(self, name, *, server_name=None, instances=None, hosted=None,
                 proxy_callbacks=False):
        self.name = name
        self.label = name
        self.server_name = server_name
        self.exclude_from = []
        self.instances = instances
        self.hosted = hosted
        # Mirror the McpManifest fields the local build path reads, with the
        # real dataclass defaults. server.proxy_callbacks gates the bearer-swap
        # lifting (true for proxy-terminable localhost MCPs like github/m365);
        # agent_env/env carry secret/non-secret injection; the rest are the
        # eligibility / device / network axes build_session_mcp_config consults.
        # transport "stdio" (NOT "none" — that now means a context-only MCP,
        # which build_session_mcp_config skips before any bundle assembly).
        self.server = SimpleNamespace(proxy_callbacks=proxy_callbacks, port=0, transport="stdio")
        self.credentials = SimpleNamespace(oauth=None)
        self.agent_env = {}
        self.env = {}
        self.network_targets = []
        self.network_access_default = True
        self.placement = "any"
        self.remote_policy = "any"
        self.device_capability = None
        self.device_high_risk_tools = []
        self.assignment_mode = "auto"
        self.agent_context = []
        self.tool_filter = None
        self.requires_display = False
        self.requires_capability = None
        self.skills = []


def _stub_assembly(monkeypatch, manifests, *, env_by_mcp, tmp_path,
                   server_entries=None, bearer_by_mcp=None,
                   secret_keys=None, bash_env_keys=None):
    """Stub build_session_mcp_config's heavy collaborators. ``server_entries``
    maps mcp name → the dict resolve_server_config returns (default: a plain
    stdio entry). ``bearer_by_mcp`` maps mcp name → an access token that
    maybe_inject_bearer_header writes into the entry's Authorization header.
    ``secret_keys`` defaults to EVERY resolver cred (the infra/per-user case);
    pass it + ``bash_env_keys`` to model OAuth paths / env_injection."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver

    monkeypatch.setattr(
        mcp_registry, "_get_agent_mcps_with_device_exclusions",
        lambda *a, **k: (list(manifests), {}),
    )
    _secret = (
        set(secret_keys) if secret_keys is not None
        else {k for d in env_by_mcp.values() for k in d}
    )
    _bash = set(bash_env_keys or ())
    monkeypatch.setattr(
        credential_resolver, "resolve_credentials",
        lambda *a, **k: SimpleNamespace(
            env_by_mcp={m: dict(v) for m, v in env_by_mcp.items()},
            env_vars={k2: v2 for d in env_by_mcp.values() for k2, v2 in d.items()},
            excluded_mcps=set(),
            exclusion_reasons={},
            available_mcps=set(env_by_mcp),
            secret_keys=_secret,
            bash_env_keys=_bash,
        ),
    )
    entries = server_entries or {}

    def _fake_server_config(manifest, agent_name, **k):
        return dict(entries.get(manifest.name, {"type": "stdio", "command": "x", "args": []}))

    monkeypatch.setattr(mcp_registry, "resolve_server_config", _fake_server_config)

    bearers = bearer_by_mcp or {}

    def _fake_bearer(entry, manifest, *a, **k):
        tok = bearers.get(manifest.name)
        if tok:
            entry.setdefault("headers", {})["Authorization"] = f"Bearer {tok}"
        return entry

    monkeypatch.setattr(mcp_registry, "maybe_inject_bearer_header", _fake_bearer)
    monkeypatch.setattr(mcp_registry.config, "SESSIONS_DIR", tmp_path)
    # Keep the assembly DB-free: server_name "local" (browser-mcp) would
    # otherwise hit get_browser_allowed_origins, opening an app DB connection
    # that deadlocks the autouse temp_db DDL.
    from storage import agent_store
    monkeypatch.setattr(
        agent_store, "get_browser_allowed_origins", lambda *a, **k: [],
    )


def test_bundle_attributes_resolver_creds_per_srv_key(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("alpha"), _FakeManifest("beta")],
        env_by_mcp={"alpha": {"ALPHA_KEY": "a"}, "beta": {"BETA_KEY": "b"}},
        tmp_path=tmp_path,
    )

    _path, env_vars, _excl, bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None,
    )

    assert set(bundles) == {"alpha", "beta"}
    assert bundles["alpha"].env == {"ALPHA_KEY": "a"}
    assert bundles["beta"].env == {"BETA_KEY": "b"}
    # The pure secrets are STRIPPED from the flat union — broker-only now.
    assert env_vars == {}


def test_zero_mcps_returns_full_5_tuple(monkeypatch, tmp_path):
    """Regression: a session that resolves to ZERO assigned MCPs must still return the
    full 5-tuple (mcp_config, credential_env, excluded_mcps, secret_bundles,
    bash_env_keys). A 4-tuple here crashed session start with 'not enough values to
    unpack (expected 5, got 4)' — hit e.g. when an agent has no MCPs (or all excluded)."""
    from services.mcp import mcp_registry
    _stub_assembly(monkeypatch, [], env_by_mcp={}, tmp_path=tmp_path)

    result = mcp_registry.build_session_mcp_config("agent", None)

    assert len(result) == 5
    mcp_config, credential_env, excluded_mcps, secret_bundles, bash_env_keys = result
    assert mcp_config is None
    assert credential_env == {} and secret_bundles == {}
    assert isinstance(bash_env_keys, set) and bash_env_keys == set()


def test_bundle_keyed_by_server_name_override(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("browser-mcp", server_name="local")],
        env_by_mcp={"browser-mcp": {"B": "1"}},
        tmp_path=tmp_path,
    )

    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)

    # Bundle key follows the mcpServers key (server_name), not the manifest name,
    # so it matches the token's mcp claim + the interceptor's lookup.
    assert set(bundles) == {"local"}
    assert bundles["local"].env == {"B": "1"}


def test_localhost_bearer_to_bundle_sentinel_in_file(monkeypatch, tmp_path):
    """A proxy-terminable HTTP MCP (localhost sidecar — github/m365) has its
    real bearer lifted into the bundle; the config FILE carries only a sentinel."""
    from services.mcp import mcp_registry
    from core.credentials.mcp_broker import BROKER_BEARER_PLACEHOLDER
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("gh", proxy_callbacks=True)],
        env_by_mcp={},  # no stdio resolver creds
        bearer_by_mcp={"gh": "ghp_secret"},
        server_entries={"gh": {"type": "http", "url": "http://localhost:8935/mcp"}},
        tmp_path=tmp_path,
    )

    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)

    # Real bearer → bundle only; the file gets the sentinel (no real token).
    assert bundles["gh"].http_bearer == "ghp_secret"
    assert bundles["gh"].env == {}
    file_text = _p.read_text()
    assert "ghp_secret" not in file_text
    assert BROKER_BEARER_PLACEHOLDER in file_text


def test_external_vendor_bearer_stays_in_file(monkeypatch, tmp_path):
    """A vendor HTTP MCP (external host — slack/notion/…) keeps its bearer
    inline (direct-to-vendor; not yet tunnel-routable) → bundle.http_bearer None."""
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("slack")],
        env_by_mcp={"slack": {"SLACK_X": "s"}},  # force a bundle to exist
        bearer_by_mcp={"slack": "xoxb-real"},
        server_entries={"slack": {"type": "http", "url": "https://mcp.slack.com/mcp"}},
        tmp_path=tmp_path,
    )

    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)

    # Bundle exists (for SLACK_X) but carries NO http_bearer; the real bearer
    # stays inline in the config file (the accepted residual until tunnel-routing).
    assert bundles["slack"].http_bearer is None
    assert "Bearer xoxb-real" in _p.read_text()


def test_bundle_includes_instance_field_values(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("inst", instances=SimpleNamespace(delivery="env"), hosted=None)],
        env_by_mcp={},
        tmp_path=tmp_path,
    )
    monkeypatch.setattr(
        mcp_registry.mcp_store, "get_instance_for_agent_env_delivery",
        lambda *a, **k: {"field_values": {"FV": "secret"}, "hosted_mode": "instance"},
    )

    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)

    # Instance field_values are secret material → in the bundle, NEVER the config
    # file.
    assert bundles["inst"].env == {"FV": "secret"}
    assert "secret" not in _p.read_text()


def test_flat_env_keeps_nonsecret_and_excludes_from_bundle(monkeypatch, tmp_path):
    """Paths + env_injection (non-secret) STAY in the flat env and are NEVER put
    in the bundle — the broker fetch would otherwise overwrite the MCP's
    sandbox-virtual path with a host path."""
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("ws")],
        env_by_mcp={"ws": {"WS_DIR": "/virt/p", "GH_TOKEN": "tok"}},
        secret_keys=set(),                 # neither key is a pure secret
        bash_env_keys={"GH_TOKEN"},
        tmp_path=tmp_path,
    )
    _p, env_vars, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)
    # both stay in the flat env (the MCP needs the path; bash needs GH_TOKEN)
    assert env_vars == {"WS_DIR": "/virt/p", "GH_TOKEN": "tok"}
    # nothing secret → no bundle for ws at all
    assert "ws" not in bundles


def test_oto_strip_keys_on_credentialed_stdio_only(monkeypatch, tmp_path):
    """A credentialed (wrapped) stdio MCP gets OTO_STRIP_KEYS listing the
    env_injection names; a cred-less MCP does not (it is never wrapped)."""
    import json
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("alpha"), _FakeManifest("plain")],
        env_by_mcp={"alpha": {"ALPHA_KEY": "a"}},   # alpha credentialed; plain not
        secret_keys={"ALPHA_KEY"},
        bash_env_keys={"GH_TOKEN", "GITHUB_TOKEN"},
        tmp_path=tmp_path,
    )
    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)
    cfg = json.loads(_p.read_text())["mcpServers"]
    # credentialed stdio MCP → OTO_STRIP_KEYS present (sorted, comma-joined)
    assert cfg["alpha"]["env"]["OTO_STRIP_KEYS"] == "GH_TOKEN,GITHUB_TOKEN"
    # cred-less MCP → none (not wrapped)
    assert "OTO_STRIP_KEYS" not in cfg["plain"].get("env", {})
    # the secret itself is bundle-only, absent from the file
    assert "ALPHA_KEY" not in _p.read_text()


def test_no_bundle_for_credless_mcp(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch,
        [_FakeManifest("plain")],
        env_by_mcp={},  # nothing to broker
        tmp_path=tmp_path,
    )

    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config("agent", None)

    assert bundles == {}


def test_inject_toml_excludes_bash_env_keys(tmp_path):
    """inject_credential_env_into_toml drops exclude_keys
    (env_injection) from the FILE — GH_TOKEN never lands on disk, while the
    non-secret OTO_* still ships into the Codex MCP env block."""
    import json as _json
    from services.mcp import mcp_registry
    toml_path = tmp_path / "agent-x.toml"
    toml_path.write_text("")  # regenerated by the injector
    (tmp_path / "agent-x.servers.json").write_text(_json.dumps({
        "alpha": {"type": "stdio", "command": "x", "args": [], "env": {}},
    }))
    out = mcp_registry.inject_credential_env_into_toml(
        toml_path, {"OTO_USERNAME": "alice", "GH_TOKEN": "ghp_secret"},
        exclude_keys={"GH_TOKEN"},
    )
    text = out.read_text()
    assert "OTO_USERNAME" in text and "alice" in text
    assert "GH_TOKEN" not in text and "ghp_secret" not in text


# ---------------------------------------------------------------------------
# Half 2 — remote token injection (the proxy half of remote claude/codex)
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_remote_paths(monkeypatch):
    """Neutralize the satellite-path rewriting so the tests isolate the
    OTO_MCP_FETCH_TOKEN injection."""
    from core.remote import remote_execution
    # These are called internally by the rewriters, which now live in
    # remote_mcp_rewrite — patch them there so the internal calls hit the stubs.
    from core.remote import remote_mcp_rewrite
    monkeypatch.setattr(
        remote_mcp_rewrite, "_resolve_satellite_mcp_path_info",
        lambda name: ("core", name, "custom", None),
    )
    monkeypatch.setattr(
        remote_mcp_rewrite, "_rewrite_stdio_paths",
        lambda cmd, args, **k: (cmd, args),
    )
    monkeypatch.setattr(
        remote_mcp_rewrite, "_rewrite_env_for_remote",
        lambda env, port: env,
    )
    return remote_execution


def test_rewrite_json_injects_token_for_bundle_stdio_only(_isolate_remote_paths):
    re_mod = _isolate_remote_paths
    cfg = {
        "mcpServers": {
            "alpha": {"command": "x", "args": [], "env": {}},   # stdio + bundle
            "gamma": {"command": "y", "args": [], "env": {}},   # stdio, NO bundle
            "beta": {"url": "https://example/mcp"},             # http + bundle
        }
    }
    out = re_mod._rewrite_mcp_json_for_remote(
        cfg, sat_port=9999, session_id="sid-1",
        secret_bundle_keys={"alpha", "beta"},
    )
    servers = out["mcpServers"]
    # alpha (stdio + bundle) → token present + valid
    tok = servers["alpha"]["env"]["OTO_MCP_FETCH_TOKEN"]
    assert mcp_broker.verify_token(tok) == ("sid-1", "alpha")
    # gamma (stdio, not in bundle) → no token
    assert "OTO_MCP_FETCH_TOKEN" not in servers["gamma"].get("env", {})
    # beta (http, no command) → no token even though it has a bundle (its bearer
    # is handled by the proxy swap, not the stdio interceptor)
    assert "OTO_MCP_FETCH_TOKEN" not in servers["beta"].get("env", {})


def test_rewrite_toml_injects_token_for_bundle_section(_isolate_remote_paths):
    re_mod = _isolate_remote_paths
    toml = (
        "[mcp_servers.alpha]\n"
        'command = "python3"\n'
        'args = ["-m", "alpha"]\n'
        'env = { "OTO_SESSION_ID" = "sid-1" }\n'
        "\n"
        "[mcp_servers.beta]\n"
        'url = "https://example/mcp"\n'
    )
    out = re_mod._rewrite_mcp_toml_for_remote(
        toml, sat_port=9999, session_id="sid-1",
        proxy_api_key="pk", secret_bundle_keys={"alpha"},
    )
    # alpha's env line carries a valid token
    import re as _re
    m = _re.search(r'"OTO_MCP_FETCH_TOKEN"\s*=\s*"([^"]+)"', out)
    assert m, out
    assert mcp_broker.verify_token(m.group(1)) == ("sid-1", "alpha")
    # beta (no env block / not in bundle) gets none
    assert out.count("OTO_MCP_FETCH_TOKEN") == 1


def test_rewrite_toml_no_token_when_not_in_bundle(_isolate_remote_paths):
    re_mod = _isolate_remote_paths
    toml = (
        "[mcp_servers.alpha]\n"
        'command = "python3"\n'
        'args = []\n'
        'env = { "OTO_SESSION_ID" = "sid-1" }\n'
    )
    out = re_mod._rewrite_mcp_toml_for_remote(
        toml, sat_port=9999, session_id="sid-1",
        proxy_api_key="pk", secret_bundle_keys=set(),
    )
    assert "OTO_MCP_FETCH_TOKEN" not in out
    # PROXY callback creds are still injected (unchanged behavior)
    assert "PROXY_API_KEY" in out
