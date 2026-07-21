"""Credential-broker fetch in the stdio interceptor.

When given a per-(session, mcp) capability token (``OTO_MCP_FETCH_TOKEN``), the
universal wrapper fetches that MCP's secrets at spawn, merges them into the child
env, then strips every broker-only var (the token, ``OTO_STRIP_KEYS``-named keys,
``OTO_BEARER_*``) before exec. Fail-closed: a failed fetch injects nothing.

Pure logic — no real MCP spawn, no network (``_fetch_mcp_credentials`` /
``urlopen`` are stubbed).
"""

import sys
import urllib.error


from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core import stdio_path_interceptor as itc  # noqa: E402


# ── _pop_ci ────────────────────────────────────────────────────────────────

def test_pop_ci_case_insensitive():
    env = {"Foo": "1", "BAR": "2"}
    assert itc._pop_ci(env, "foo") == "1"
    assert itc._pop_ci(env, "bar") == "2"
    assert "Foo" not in env and "BAR" not in env
    assert itc._pop_ci(env, "missing") is None


# ── _apply_broker_credentials: merge + strip ───────────────────────────────

def test_apply_merges_creds_and_strips_token(monkeypatch):
    monkeypatch.setattr(
        itc, "_fetch_mcp_credentials",
        lambda token, **kw: {"env": {"GH_TOKEN": "secret"}, "http_bearer": None},
    )
    env = {"OTO_MCP_FETCH_TOKEN": "captok", "PATH": "/bin"}
    itc._apply_broker_credentials(env)
    assert env["GH_TOKEN"] == "secret"       # fetched secret merged in
    assert "OTO_MCP_FETCH_TOKEN" not in env   # token stripped (MCP never sees it)
    assert env["PATH"] == "/bin"              # untouched


def test_apply_fail_closed_injects_nothing(monkeypatch):
    monkeypatch.setattr(itc, "_fetch_mcp_credentials", lambda token, **kw: None)
    env = {"OTO_MCP_FETCH_TOKEN": "captok"}
    itc._apply_broker_credentials(env)
    assert env == {}                          # token stripped, nothing injected


def test_apply_no_token_skips_fetch(monkeypatch):
    called = []
    monkeypatch.setattr(
        itc, "_fetch_mcp_credentials",
        lambda token, **kw: called.append(token) or {"env": {}},
    )
    env = {"PATH": "/bin"}
    itc._apply_broker_credentials(env)
    assert env == {"PATH": "/bin"}
    assert called == []                       # no token → no fetch


def test_apply_strips_strip_keys_and_bearers(monkeypatch):
    monkeypatch.setattr(itc, "_fetch_mcp_credentials", lambda token, **kw: {"env": {}})
    env = {
        "OTO_MCP_FETCH_TOKEN": "t",
        "OTO_STRIP_KEYS": "GH_TOKEN, GIT_CONFIG_COUNT",
        "GH_TOKEN": "x", "GIT_CONFIG_COUNT": "1",
        "OTO_BEARER_SLACK": "xoxb", "KEEP": "1",
    }
    itc._apply_broker_credentials(env)
    assert "GH_TOKEN" not in env and "GIT_CONFIG_COUNT" not in env  # named strips
    assert "OTO_BEARER_SLACK" not in env                            # bearer prefix
    assert "OTO_STRIP_KEYS" not in env and "OTO_MCP_FETCH_TOKEN" not in env
    assert env["KEEP"] == "1"


# ── _fetch_mcp_credentials ─────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def test_fetch_returns_env_on_200(monkeypatch):
    import json
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        itc.urllib.request, "urlopen",
        lambda req, timeout=0: _Resp(json.dumps({"env": {"K": "V"}, "http_bearer": "b"}).encode()),
    )
    assert itc._fetch_mcp_credentials("tok") == {"env": {"K": "V"}, "http_bearer": "b"}


def test_fetch_terminal_on_401_no_retry(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:9")
    calls = []

    def _raise(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, "denied", {}, None)

    monkeypatch.setattr(itc.urllib.request, "urlopen", _raise)
    assert itc._fetch_mcp_credentials("tok", attempts=3) is None
    assert len(calls) == 1                     # 401 is terminal → no retry/backoff


def test_fetch_no_proxy_url_returns_none(monkeypatch):
    monkeypatch.delenv("PROXY_URL", raising=False)
    assert itc._fetch_mcp_credentials("tok") is None
