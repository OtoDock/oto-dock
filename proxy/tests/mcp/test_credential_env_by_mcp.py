"""Per-MCP credential attribution (ResolvedCredentials.env_by_mcp).

The credential broker needs each MCP's OWN secrets, not the flat union.
``resolve_credentials`` now records ``env_by_mcp[mcp]`` alongside the union. This
asserts the attribution is correct without DB seeding — the resolver's MCP list
and per-type resolvers are stubbed.
"""

import sys


from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from services.oauth import credential_resolver  # noqa: E402


class _FakeManifest:
    def __init__(self, name):
        self.name = name


def test_env_by_mcp_attributes_each_mcps_creds(monkeypatch):
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_agent_mcps_all_placements",
        lambda agent: [_FakeManifest("alpha"), _FakeManifest("beta")],
    )
    monkeypatch.setattr(
        mcp_registry, "get_credential_schema",
        lambda mcp: {"type": "infra", "fields": ["X"]},
    )
    per_mcp = {"alpha": {"ALPHA_KEY": "a"}, "beta": {"BETA_KEY": "b"}}
    monkeypatch.setattr(
        credential_resolver, "_resolve_infra",
        lambda mcp, schema: dict(per_mcp[mcp]),
    )

    result = credential_resolver.resolve_credentials("agent", None)

    assert result.env_by_mcp == per_mcp                              # per-MCP attribution
    assert result.env_vars == {"ALPHA_KEY": "a", "BETA_KEY": "b"}    # flat union unchanged
    # every brokerable secret is attributed to exactly one MCP
    union = {k: v for d in result.env_by_mcp.values() for k, v in d.items()}
    assert union == result.env_vars
    # secret classification: infra creds are PURE secrets; no bash env_injection.
    assert result.secret_keys == {"ALPHA_KEY", "BETA_KEY"}
    assert result.bash_env_keys == set()


def test_env_by_mcp_skips_credless_and_excluded(monkeypatch):
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_agent_mcps_all_placements",
        lambda agent: [_FakeManifest("nocreds"), _FakeManifest("missing")],
    )
    # nocreds → type "none"; missing → infra whose creds resolve to None (excluded)
    monkeypatch.setattr(
        mcp_registry, "get_credential_schema",
        lambda mcp: {"type": "none"} if mcp == "nocreds" else {"type": "infra", "fields": ["X"]},
    )
    monkeypatch.setattr(credential_resolver, "_resolve_infra", lambda mcp, schema: None)

    result = credential_resolver.resolve_credentials("agent", None)

    assert result.env_by_mcp == {}              # neither contributes secrets
    assert "nocreds" in result.available_mcps   # cred-less MCP still available
    assert "missing" in result.excluded_mcps    # missing creds → excluded
    assert result.secret_keys == set()          # nothing to strip


def test_oauth_env_injection_is_bash_not_secret(monkeypatch):
    """OAuth env = credentials_dir PATHS (kept in the flat env) + env_injection
    (GH_TOKEN, bash-only). Neither is a PURE secret → secret_keys stays empty;
    the injection names land in bash_env_keys (→ OTO_STRIP_KEYS)."""
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_agent_mcps_all_placements",
        lambda agent: [_FakeManifest("gh")],
    )
    monkeypatch.setattr(
        mcp_registry, "get_credential_schema",
        lambda mcp: {"type": "per_user", "oauth": {"env_injection": ["GH_TOKEN"]},
                     "label": "GitHub"},
    )
    monkeypatch.setattr(
        credential_resolver, "_resolve_oauth_mcp",
        lambda *a, **k: {"GH_TOKEN": "tok", "WS_DIR": "/virt"},
    )
    monkeypatch.setattr(
        mcp_registry, "get_credentials_dirs",
        lambda mcp: [("WS_DIR", "sub")],   # WS_DIR is a credentials_dir PATH
    )

    result = credential_resolver.resolve_credentials("agent", "user-1")

    assert result.env_by_mcp == {"gh": {"GH_TOKEN": "tok", "WS_DIR": "/virt"}}
    assert result.bash_env_keys == {"GH_TOKEN"}   # env_injection (oauth_env - paths)
    assert result.secret_keys == set()            # OAuth contributes NO pure secrets
