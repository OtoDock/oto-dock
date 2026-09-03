"""Tests for the satellite stdio interceptor + batched hook.

Covers:
  * The batched ``/v1/hooks/resolve-tool-arg-paths`` proxy endpoint.
  * The pure functions inside the interceptor script (JSONPath
    tokenize/walk, path-string detection, ``_process_inbound_line``).
  * The satellite-side ``wrap_interceptor_in_mcp_config`` helper.

End-to-end (CLI ↔ interceptor ↔ MCP) testing happens manually on a
real satellite — the interceptor spawns subprocesses and uses
``urllib.request`` synchronously, neither of which is friendly to a
fast unit-test loop.
"""

import json
import sys
from unittest.mock import patch

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
_REPO_ROOT = _PROXY_DIR.parent
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from auth.path_policy import SecurityContext  # noqa: E402
from satellite.sessions.mcp_interceptor import (  # noqa: E402
    wrap_interceptor_in_mcp_config,
)
from satellite._vendored.stdio_path_interceptor import (  # noqa: E402
    _looks_like_path,
    _process_inbound_line,
    _synthesize_error_response,
    _tokenize_jsonpath,
    _walk,
)


# ---------------------------------------------------------------------------
# JSONPath tokenize + walk
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_single_field(self):
        assert _tokenize_jsonpath("path") == [("field", "path")]

    def test_dotted_chain(self):
        assert _tokenize_jsonpath("a.b.c") == [
            ("field", "a"), ("field", "b"), ("field", "c"),
        ]

    def test_wildcard(self):
        assert _tokenize_jsonpath("paths[*]") == [
            ("field", "paths"), ("wildcard", ""),
        ]

    def test_nested_wildcard(self):
        assert _tokenize_jsonpath("images[*].source") == [
            ("field", "images"), ("wildcard", ""), ("field", "source"),
        ]

    def test_multi_wildcard(self):
        assert _tokenize_jsonpath("a[*].b[*].c") == [
            ("field", "a"), ("wildcard", ""),
            ("field", "b"), ("wildcard", ""),
            ("field", "c"),
        ]


class TestWalk:
    def test_top_field(self):
        obj = {"path": "/foo", "other": "x"}
        out = _walk(obj, _tokenize_jsonpath("path"))
        assert out == [(obj, "path")]

    def test_nested_field(self):
        obj = {"data": {"file_path": "/foo"}}
        out = _walk(obj, _tokenize_jsonpath("data.file_path"))
        assert len(out) == 1
        parent, key = out[0]
        assert parent[key] == "/foo"

    def test_array_wildcard(self):
        obj = {"images": [{"source": "/a"}, {"source": "/b"}]}
        out = _walk(obj, _tokenize_jsonpath("images[*].source"))
        assert len(out) == 2
        assert [p[k] for p, k in out] == ["/a", "/b"]

    def test_missing_field_returns_empty(self):
        obj = {"other": "x"}
        out = _walk(obj, _tokenize_jsonpath("path"))
        assert out == []

    def test_wildcard_on_non_list_returns_empty(self):
        obj = {"images": "not-a-list"}
        out = _walk(obj, _tokenize_jsonpath("images[*]"))
        assert out == []

    def test_rewrite_in_place(self):
        obj = {"images": [{"source": "/a"}, {"source": "/b"}]}
        for parent, key in _walk(obj, _tokenize_jsonpath("images[*].source")):
            parent[key] = "REWRITTEN"
        assert obj == {"images": [{"source": "REWRITTEN"}, {"source": "REWRITTEN"}]}


# ---------------------------------------------------------------------------
# Path-string detection (mirrors path_policy_v2.is_path_string)
# ---------------------------------------------------------------------------


class TestLooksLikePath:
    @pytest.mark.parametrize("value", [
        "/etc/hosts", "/users/a/foo.png", "~/Desktop/foo.png",
        "C:/Users/a/foo.png", "C:\\Users\\a\\foo.png",
        "Desktop/foo.png",  # relative
        "foo.png",          # relative
    ])
    def test_accepts(self, value):
        assert _looks_like_path(value) is True

    @pytest.mark.parametrize("value", [
        "http://example.com/foo.png",
        "https://example.com/foo.png",
        "data:image/png;base64,iVBORw0KG",
        "/foo${BAR}/x.png",        # template
        "/foo{{BAR}}/x.png",       # template
        "A" * 600,                  # long, no slash → base64-like
        "",
    ])
    def test_rejects(self, value):
        assert _looks_like_path(value) is False


# ---------------------------------------------------------------------------
# _process_inbound_line — the heart of the interceptor
# ---------------------------------------------------------------------------


def _hook_ok_response(items):
    """Build a mocked /v1/hooks/resolve-tool-arg-paths response."""
    return {
        "items": items,
        "tool": "",
    }


class TestProcessInboundLine:
    def test_non_tools_call_passthrough(self):
        line = (json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }) + "\n").encode()
        to_mcp, to_cli = _process_inbound_line(line, [], "sess-x")
        assert to_mcp == line
        assert to_cli == b""

    def test_no_declarations_passthrough(self):
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "anything", "arguments": {"path": "/a"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        to_mcp, to_cli = _process_inbound_line(line, [], "sess-x")
        assert to_mcp == line
        assert to_cli == b""

    def test_declaration_mismatch_passthrough(self):
        # Declaration is for tool X; the call is for tool Y → passthrough.
        decls = [{"tool": "X", "json_path": "path", "mode": "read"}]
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "Y", "arguments": {"path": "/a"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
        assert to_mcp == line

    def test_no_path_in_args_passthrough(self):
        decls = [{"tool": "T", "json_path": "path", "mode": "read"}]
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "T", "arguments": {}},  # path missing
        }
        line = (json.dumps(msg) + "\n").encode()
        to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
        assert to_mcp == line

    def test_url_path_value_skipped(self):
        decls = [{"tool": "T", "json_path": "path", "mode": "read"}]
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "T",
                       "arguments": {"path": "https://example.com/x.png"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        # No hook should fire — URL is not a path. Passthrough.
        with patch("satellite._vendored.stdio_path_interceptor._hook_post") as h:
            to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
            h.assert_not_called()
        assert to_mcp == line

    def test_path_rewritten_on_allow(self):
        decls = [{"tool": "T", "json_path": "path", "mode": "read"}]
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "T",
                       "arguments": {"path": "/users/a/foo.png"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        resp = _hook_ok_response([{
            "access_path": "/sat/agents/X/users/a/foo.png",
            "allowed": True, "error": "",
        }])
        with patch(
            "satellite._vendored.stdio_path_interceptor._hook_post", return_value=resp,
        ):
            to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
        assert to_cli == b""
        rewritten = json.loads(to_mcp.decode())
        assert (
            rewritten["params"]["arguments"]["path"]
            == "/sat/agents/X/users/a/foo.png"
        )

    def test_array_wildcard_rewritten(self):
        decls = [{
            "tool": "display_images",
            "json_path": "images[*].source",
            "mode": "read",
        }]
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "display_images",
                "arguments": {
                    "images": [
                        {"source": "/users/a/foo.png"},
                        {"source": "https://cdn.example/bar.png"},
                        {"source": "/users/a/baz.png"},
                    ],
                },
            },
        }
        line = (json.dumps(msg) + "\n").encode()
        # Hook receives only the 2 path-like values (the URL is skipped).
        resp = _hook_ok_response([
            {"access_path": "/sat/foo.png", "allowed": True, "error": ""},
            {"access_path": "/sat/baz.png", "allowed": True, "error": ""},
        ])
        with patch(
            "satellite._vendored.stdio_path_interceptor._hook_post", return_value=resp,
        ) as h:
            to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
            assert h.call_count == 1
        rewritten = json.loads(to_mcp.decode())
        sources = [img["source"] for img in rewritten["params"]["arguments"]["images"]]
        assert sources == [
            "/sat/foo.png", "https://cdn.example/bar.png", "/sat/baz.png",
        ]

    def test_denial_synthesizes_error_to_cli(self):
        decls = [{"tool": "T", "json_path": "path", "mode": "read"}]
        msg = {
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {"name": "T", "arguments": {"path": "/etc/hosts"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        resp = _hook_ok_response([{
            "access_path": "", "allowed": False,
            "error": "path '/etc/hosts' is outside the OS user's home directory",
        }])
        with patch(
            "satellite._vendored.stdio_path_interceptor._hook_post", return_value=resp,
        ):
            to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
        assert to_mcp == b""
        err = json.loads(to_cli.decode())
        assert err["jsonrpc"] == "2.0"
        assert err["id"] == 42
        assert err["error"]["code"] == -32603
        assert "outside the OS user's home" in err["error"]["message"]

    def test_hook_unreachable_fails_closed(self):
        from satellite._vendored.stdio_path_interceptor import HookError
        decls = [{"tool": "T", "json_path": "path", "mode": "read"}]
        msg = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "T", "arguments": {"path": "/a/b"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        with patch(
            "satellite._vendored.stdio_path_interceptor._hook_post",
            side_effect=HookError("unreachable"),
        ):
            to_mcp, to_cli = _process_inbound_line(line, decls, "sess-x")
        assert to_mcp == b""
        err = json.loads(to_cli.decode())
        assert err["id"] == 7
        assert "path policy unavailable" in err["error"]["message"]

    def test_write_mode_passed_to_hook(self):
        decls = [{"tool": "T", "json_path": "dest", "mode": "write"}]
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "T",
                       "arguments": {"dest": "/users/a/out.png"}},
        }
        line = (json.dumps(msg) + "\n").encode()
        resp = _hook_ok_response([{
            "access_path": "/sat/out.png", "allowed": True, "error": "",
        }])
        with patch(
            "satellite._vendored.stdio_path_interceptor._hook_post", return_value=resp,
        ) as h:
            _process_inbound_line(line, decls, "sess-x")
        # Confirm the request body's `write` flag matched the declaration.
        _endpoint, body = h.call_args.args
        assert body["items"][0]["write"] is True

    def test_malformed_json_passthrough(self):
        line = b"not-json\n"
        to_mcp, to_cli = _process_inbound_line(line, [], "sess-x")
        assert to_mcp == line
        assert to_cli == b""


class TestSynthesizeErrorResponse:
    def test_includes_request_id(self):
        out = _synthesize_error_response(
            {"jsonrpc": "2.0", "id": 99, "method": "tools/call"},
            "rejected",
        )
        parsed = json.loads(out.decode())
        assert parsed["id"] == 99
        assert parsed["error"]["message"] == "rejected"
        assert parsed["jsonrpc"] == "2.0"


# ---------------------------------------------------------------------------
# wrap_interceptor_in_mcp_config (satellite-side rewriter)
# ---------------------------------------------------------------------------


class TestWrapInterceptor:
    def test_stdio_with_declarations_wrapped(self):
        cfg = {
            "mcpServers": {
                "display": {
                    "command": "venv/bin/python",
                    "args": ["display_server.py"],
                    "env": {
                        "OTO_TOOL_ARG_PATHS": '[{"tool":"X","json_path":"p","mode":"read"}]',
                    },
                },
            },
        }
        wrap_interceptor_in_mcp_config(cfg)
        server = cfg["mcpServers"]["display"]
        # Args should now be: [<interceptor>, --, <orig-cmd>, <orig-args...>]
        assert server["args"][1] == "--"
        assert server["args"][2] == "venv/bin/python"
        assert server["args"][3] == "display_server.py"
        # Command is now Python — sys.executable on the satellite.
        assert server["command"].endswith("python") or server["command"].endswith("python3") or server["command"].endswith("python.exe")
        # The interceptor path is preserved.
        assert server["args"][0].endswith("stdio_path_interceptor.py")
        # Env vars preserved
        assert "OTO_TOOL_ARG_PATHS" in server["env"]

    def test_stdio_with_only_broker_token_wrapped(self):
        # A server with the credential-broker token but NO
        # tool_arg_paths must STILL be wrapped, so the interceptor can fetch its
        # secrets at spawn.
        cfg = {
            "mcpServers": {
                "github": {
                    "command": "venv/bin/python",
                    "args": ["server.py"],
                    "env": {"OTO_MCP_FETCH_TOKEN": "captok"},
                },
            },
        }
        wrap_interceptor_in_mcp_config(cfg)
        server = cfg["mcpServers"]["github"]
        assert server["args"][0].endswith("stdio_path_interceptor.py")
        assert server["args"][1:] == ["--", "venv/bin/python", "server.py"]
        # Token stays in the config env; the interceptor strips it at spawn.
        assert server["env"]["OTO_MCP_FETCH_TOKEN"] == "captok"

    def test_http_server_with_broker_token_env_stripped(self):
        cfg = {
            "mcpServers": {
                "http-mcp": {
                    "type": "http",
                    "url": "http://localhost:9000/mcp/",
                    "env": {"OTO_MCP_FETCH_TOKEN": "captok", "FOO": "bar"},
                },
            },
        }
        wrap_interceptor_in_mcp_config(cfg)
        srv = cfg["mcpServers"]["http-mcp"]
        assert "command" not in srv                       # no stdio → no wrap
        assert "OTO_MCP_FETCH_TOKEN" not in srv["env"]    # stripped (won't reach an HTTP MCP)
        assert srv["env"]["FOO"] == "bar"

    def test_stdio_without_declarations_left_alone(self):
        cfg = {
            "mcpServers": {
                "other": {
                    "command": "python",
                    "args": ["server.py"],
                    "env": {"FOO": "bar"},
                },
            },
        }
        wrap_interceptor_in_mcp_config(cfg)
        assert cfg["mcpServers"]["other"]["command"] == "python"
        assert cfg["mcpServers"]["other"]["args"] == ["server.py"]

    def test_http_server_left_alone_but_env_stripped(self):
        cfg = {
            "mcpServers": {
                "http-mcp": {
                    "type": "http",
                    "url": "http://localhost:9000/mcp/",
                    "env": {
                        "OTO_TOOL_ARG_PATHS": '[]',
                        "FOO": "bar",
                    },
                },
            },
        }
        wrap_interceptor_in_mcp_config(cfg)
        # No command → no wrap; OTO_TOOL_ARG_PATHS stripped to avoid
        # confusion downstream (HTTP MCPs don't use stdio).
        srv = cfg["mcpServers"]["http-mcp"]
        assert "command" not in srv
        assert "OTO_TOOL_ARG_PATHS" not in srv["env"]
        assert srv["env"]["FOO"] == "bar"

    def test_empty_config_is_noop(self):
        cfg = {"mcpServers": {}}
        wrap_interceptor_in_mcp_config(cfg)
        assert cfg == {"mcpServers": {}}

    def test_missing_mcpservers_is_noop(self):
        cfg: dict = {}
        wrap_interceptor_in_mcp_config(cfg)
        assert cfg == {}


# ---------------------------------------------------------------------------
# /v1/hooks/resolve-tool-arg-paths — end-to-end via FastAPI TestClient
# ---------------------------------------------------------------------------


class TestResolveToolArgPathsEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.hooks import hooks as hooks_mod

        app = FastAPI()
        app.include_router(hooks_mod.router)

        # Stub session-match auth so requests don't get rejected on
        # bearer-token shape.
        monkeypatch.setattr(
            hooks_mod, "verify_session_match", lambda *a, **kw: None,
        )
        return TestClient(app)

    def _ctx(self, *, allow_full_fs=False, home_dir="/home/dave"):
        return SecurityContext(
            role="manager", username="alice", agent="my-agent",
            is_admin_agent=False,
            target_kind="user_remote",
            target_machine_id="m1",
            target_agents_dir="/home/dave/.oto-dock/agents",
            target_home_dir=home_dir,
            target_allow_full_fs=allow_full_fs,
        )

    def test_session_not_found(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: None,
        )
        r = client.post("/v1/hooks/resolve-tool-arg-paths", json={
            "session_id": "missing",
            "tool": "X",
            "items": [{"value": "/users/a/foo.png"}],
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 404

    def test_batched_response_preserves_order(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: self._ctx(allow_full_fs=False),
        )
        body = {
            "session_id": "s1",
            "tool": "T",
            "items": [
                {"value": "/users/alice/workspace/foo.png", "write": False},
                {"value": "/etc/hosts", "write": False},
                {"value": "/home/dave/Desktop/x.png", "write": False},
            ],
        }
        r = client.post(
            "/v1/hooks/resolve-tool-arg-paths", json=body,
            headers={"Authorization": "Bearer test"},
        )
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 3
        # 0: sandbox-virtual → allowed, translated to satellite agent tree
        assert items[0]["allowed"] is True
        assert items[0]["access_path"].endswith(
            "/users/alice/workspace/foo.png"
        )
        assert items[0]["path_ref"]["kind"] == "agent_tree"
        # 1: /etc/hosts on home-only policy → rejected
        assert items[1]["allowed"] is False
        assert "home" in items[1]["error"]
        # 2: home path → allowed
        assert items[2]["allowed"] is True
        assert items[2]["access_path"] == "/home/dave/Desktop/x.png"
        assert items[2]["path_ref"]["kind"] == "satellite_host"

    def test_write_mode_marks_remote_push(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: self._ctx(allow_full_fs=True),
        )
        r = client.post("/v1/hooks/resolve-tool-arg-paths", json={
            "session_id": "s1",
            "items": [{"value": "/etc/hosts", "write": True}],
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert item["allowed"] is True
        assert item["is_remote_push"] is True
        assert item["is_remote_pull"] is False

    def test_full_fs_admits_system_path(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.hooks.hooks.get_session_security",
            lambda sid: self._ctx(allow_full_fs=True),
        )
        r = client.post("/v1/hooks/resolve-tool-arg-paths", json={
            "session_id": "s1",
            "items": [{"value": "/etc/hosts"}],
        }, headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json()["items"][0]["allowed"] is True
