"""Every hook script reaches PROXY_URL directly, whatever proxy the
environment names.

PROXY_URL is always a loopback address: the platform's own listener, or a
satellite's tunnel. urllib's default opener sends it to a proxy from the
environment and, on Windows, from Internet Options (whose ``<local>``
exemption skips dotless hosts only, never 127.0.0.1), where a hook's call
would be lost. Each script installs a no-proxy opener when it runs as a
script. Here each runs as its own process with every proxy variable pointing
at a dead port, against a stub platform that must receive its call.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests._paths import PROXY_DIR

_SCRIPTS = [
    PROXY_DIR / "hooks" / "permission_gate.py",
    PROXY_DIR / "hooks" / "stop_tracker.py",
    PROXY_DIR / "hooks" / "subagent_tracker.py",
    PROXY_DIR / "hooks" / "credential_helper.py",
    PROXY_DIR / "hooks" / "tool_result_forwarder.py",
    PROXY_DIR / "core" / "stdio_path_interceptor.py",
]


@pytest.fixture
def platform():
    """A stub platform on loopback: records each path, answers what a hook reads."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append(self.path)
            body = {"decision": "allow"}
            if self.path == "/v1/hooks/cli-credential":
                body = {"username": "x-access-token", "password": "pw-from-the-platform"}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def _env(url: str, **extra) -> dict:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("no_proxy", "http_proxy", "https_proxy", "all_proxy")
           and not k.startswith("OTO_")}
    env.update({"HTTP_PROXY": dead, "http_proxy": dead, "HTTPS_PROXY": dead,
                "ALL_PROXY": dead, "PROXY_URL": url, "PROXY_API_KEY": "tok",
                "OTO_SESSION_ID": "sess-1"}, **extra)
    return env


def _run(script, stdin: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(script), *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


def test_the_environment_proxy_would_capture_a_plain_urlopen(platform):
    """The premise: without the scripts' opener the call goes to the proxy."""
    url, seen = platform
    out = subprocess.run(
        [sys.executable, "-I", "-c",
         f"import urllib.request; urllib.request.urlopen(urllib.request.Request("
         f"'{url}/v1/hooks/stop', data=b'{{}}'), timeout=5)"],
        capture_output=True, text=True, env=_env(url), timeout=60)
    assert out.returncode != 0 and seen == []


def test_the_permission_gate_goes_direct(platform):
    url, seen = platform
    out = _run(_SCRIPTS[0], '{"tool_name": "Read", "tool_input": {}}', _env(url))
    assert seen == ["/v1/hooks/permission"], out.stderr
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_the_stop_and_subagent_trackers_go_direct(platform):
    url, seen = platform
    _run(_SCRIPTS[1], '{"hook_event_name": "Stop"}', _env(url))
    _run(_SCRIPTS[2], '{"agent_id": "agent-1", "hook_event_name": "SubagentStop"}', _env(url))
    assert seen == ["/v1/hooks/stop", "/v1/hooks/subagent"]


def test_the_tool_result_forwarder_goes_direct(platform):
    url, seen = platform
    _run(_SCRIPTS[4], json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"},
                                  "tool_response": "text"}), _env(url))
    assert seen == ["/v1/hooks/tool-result"]


def test_the_credential_helper_goes_direct(platform):
    url, seen = platform
    out = _run(_SCRIPTS[3], "protocol=https\nhost=github.com\n\n",
               _env(url, OTO_CLI_TOKEN="cli-tok"), "git", "get")
    assert seen == ["/v1/hooks/cli-credential"], out.stderr
    assert "password=pw-from-the-platform" in out.stdout


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_every_script_installs_the_no_proxy_opener_when_run(script):
    """In its ``__main__`` block, never at import: a test that loads the
    script keeps the process's own opener. The interceptor is checked here
    alone (it wraps a stdio MCP, its call needs a broker token)."""
    tree = ast.parse(script.read_text())
    main_blocks = [node for node in tree.body if isinstance(node, ast.If)
                   and "__name__" in ast.unparse(node.test)]
    assert len(main_blocks) == 1
    block = ast.unparse(main_blocks[0])
    assert "urllib.request.install_opener(" in block and "ProxyHandler({})" in block
    top = [ast.unparse(node) for node in tree.body if not isinstance(node, (ast.If, ast.FunctionDef))]
    assert not any("install_opener" in line for line in top)
