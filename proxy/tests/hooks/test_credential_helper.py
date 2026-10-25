"""The credential helper (``proxy/hooks/credential_helper.py``), run as a
subprocess the way git and the command wrappers run it, against a fake
broker: the git protocol answers the credential; a refusal or an
unreachable platform ends in ``quit=1`` with the reason on stderr and no
prompt; ``store`` and ``erase`` are silent; the ``run`` mode sets the
broker's names on the real command found past the wrapper directory and
passes the exit code through, runs the command plainly only when the
session holds no account, stops it on every other refusal, finds the real
command however PATH spells the wrapper directory, hands a command through
two wrappers to the real one, and never puts the token in an argument;
nothing is written under the helper's HOME.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import config as app_config

HELPER = Path(app_config.BASE_DIR) / "hooks" / "credential_helper.py"


class _Broker(BaseHTTPRequestHandler):
    answers: dict = {}
    seen: list = []
    flaky = 0

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).seen.append((self.path, self.headers.get("Authorization"), body))
        if type(self).flaky > 0:
            type(self).flaky -= 1
            self.send_response(503)
            self.end_headers()
            return
        status, payload = type(self).answers.get(body.get("host"), (404, {"detail": "No account"}))
        out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def broker():
    _Broker.answers = {
        "github.com": (200, {"username": "x-access-token", "password": "ghp_live",
                             "env": ["GH_TOKEN", "GITHUB_TOKEN"]}),
        "dead.example": (409, {"detail": "the vendor ended the account's grant"}),
    }
    _Broker.seen = []
    _Broker.flaky = 0
    srv = HTTPServer(("127.0.0.1", 0), _Broker)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env(broker, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PROXY_URL": f"http://127.0.0.1:{broker.server_port}",
        "PROXY_API_KEY": "sess-token-1",
        "OTO_CLI_TOKEN": "cli-token-1",
        "OTO_SESSION_ID": "sid-1",
    }


def _git(env, operation, host="github.com", timeout=30):
    return subprocess.run(
        [sys.executable, "-I", str(HELPER), "git", operation],
        input=f"protocol=https\nhost={host}\n\n", capture_output=True, text=True,
        env=env, timeout=timeout,
    )


def test_git_get_answers_the_credential_under_the_command_line_token(env):
    r = _git(env, "get")
    assert r.returncode == 0
    assert r.stdout == "username=x-access-token\npassword=ghp_live\n" and r.stderr == ""
    path, auth, body = _Broker.seen[-1]
    assert path == "/v1/hooks/cli-credential" and auth == "Bearer cli-token-1"
    assert body == {"session_id": "sid-1", "host": "github.com", "path": ""}
    assert not any((Path(env["HOME"])).iterdir())   # nothing written


def test_git_sends_the_repository_path_it_names(env):
    """With ``useHttpPath`` (a narrowed GitHub reach) git names the
    repository, and the broker is told it."""
    r = subprocess.run(
        [sys.executable, "-I", str(HELPER), "git", "get"],
        input="protocol=https\nhost=github.com\npath=octo/hello.git\n\n", capture_output=True, text=True,
        env=env, timeout=30,
    )
    assert r.returncode == 0 and r.stdout.endswith("password=ghp_live\n")
    assert _Broker.seen[-1][2] == {"session_id": "sid-1", "host": "github.com", "path": "octo/hello.git"}


def test_a_session_started_before_the_upgrade_presents_its_session_token(env):
    """Its env has no command-line token (the helper reached it from a
    sibling's start in the same config dir): the session token, which the
    route takes for such a session."""
    del env["OTO_CLI_TOKEN"]
    assert _git(env, "get").returncode == 0
    assert _Broker.seen[-1][1] == "Bearer sess-token-1"


@pytest.mark.parametrize("host,reason", [
    ("dead.example", "the vendor ended the account's grant"),
    ("other.example", "No account"),
])
def test_a_refusal_quits_git_with_the_reason_and_no_prompt(env, host, reason):
    r = _git(env, "get", host=host)
    assert r.returncode == 0 and r.stdout == "quit=1\n"
    assert reason in r.stderr and "ghp_" not in r.stderr


def test_an_unreachable_platform_retries_briefly_then_quits(env):
    env["PROXY_URL"] = "http://127.0.0.1:1"
    r = _git(env, "get")
    assert r.stdout == "quit=1\n" and "cannot be reached" in r.stderr


def test_a_tunnel_still_reconnecting_is_retried(env):
    _Broker.flaky = 2
    r = _git(env, "get")
    assert r.stdout.endswith("password=ghp_live\n") and len(_Broker.seen) == 3


def test_an_old_satellites_403_names_the_update(env, broker):
    """Only the old tunnel's exact body means the update; a 403 with any
    other body, from anything else in the path, is reported as it is."""
    _Broker.answers["github.com"] = (403, {"error": "path-not-allowlisted"})
    r = _git(env, "get")
    assert r.stdout == "quit=1\n" and "1.8.0 update" in r.stderr
    _Broker.answers["github.com"] = (403, {})
    r = _git(env, "get")
    assert r.stdout == "quit=1\n" and "1.8.0 update" not in r.stderr and "HTTP 403" in r.stderr


def test_a_malformed_answer_quits_git_without_a_traceback(env):
    """A platform that answers garbage (a half-written status line) is a
    failure the helper reports as unreachable with ``quit=1``, never a
    traceback, which would let git prompt on a terminal."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(4096)
                conn.sendall(b"garbage\r\n\r\n")
    threading.Thread(target=serve, daemon=True).start()
    try:
        env["PROXY_URL"] = f"http://127.0.0.1:{srv.getsockname()[1]}"
        r = _git(env, "get")
    finally:
        srv.close()
    assert r.returncode == 0 and r.stdout == "quit=1\n"
    assert "cannot be reached" in r.stderr and "Traceback" not in r.stderr


def test_store_and_erase_are_silent(env):
    for op in ("store", "erase"):
        r = _git(env, op)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert _Broker.seen == []


def test_an_mcp_servers_env_is_told_the_credential_is_not_for_it(env):
    """The platform names the command-line token empty in every MCP's env:
    the helper asks nothing with the session token there."""
    env["OTO_CLI_TOKEN"] = ""
    r = _git(env, "get")
    assert r.stdout == "quit=1\n" and "not available to MCP servers" in r.stderr
    assert _Broker.seen == []


def test_a_missing_platform_link_quits_too(env):
    del env["PROXY_API_KEY"]
    del env["OTO_CLI_TOKEN"]
    r = _git(env, "get")
    assert r.stdout == "quit=1\n" and "OTO_CLI_TOKEN" in r.stderr


def _rig_wrapper(tmp_path, env):
    """A config dir with the helper and a wrapper, a real ``gh`` behind
    them that reports what it received, and a PATH with the wrapper first."""
    cfg = tmp_path / ".claude"
    (cfg / "bin").mkdir(parents=True)
    (cfg / "credential_helper.py").write_bytes(HELPER.read_bytes())
    from core.sandbox.session_config_dir import COMMAND_WRAPPERS
    (cfg / "bin" / "gh").write_text(COMMAND_WRAPPERS["bin/gh"])
    (cfg / "bin" / "gh").chmod(0o755)
    real = tmp_path / "real"
    real.mkdir()
    (real / "gh").write_text(
        "#!/bin/sh\n"
        'echo "GH_TOKEN=${GH_TOKEN:-unset} GITHUB_TOKEN=${GITHUB_TOKEN:-unset}"\n'
        'echo "args=$*"\n'
        "exit 7\n"
    )
    (real / "gh").chmod(0o755)
    env["PATH"] = f"{cfg / 'bin'}:{real}:{env['PATH']}"
    env["OTO_HOOK_PY"] = sys.executable
    return cfg


def _helper_run(cfg, env, host, *args):
    return subprocess.run(
        [sys.executable, "-I", str(cfg / "credential_helper.py"), "run", host, "gh", "--", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_the_wrapper_runs_the_real_command_with_the_names_set(env, tmp_path):
    _rig_wrapper(tmp_path, env)
    r = subprocess.run(["gh", "api", "user"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 7
    assert r.stdout == "GH_TOKEN=ghp_live GITHUB_TOKEN=ghp_live\nargs=api user\n"
    assert r.stderr == ""
    # The token travels in the one process's environment, never an argument.
    assert "ghp_" not in r.stdout.splitlines()[1]


def test_the_wrapper_tells_the_broker_the_arguments_and_the_remotes(env, tmp_path):
    cfg = _rig_wrapper(tmp_path, env)
    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env, timeout=30)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/octo/hello.git"],
                   check=True, env=env, timeout=30)
    r = subprocess.run([sys.executable, "-I", str(cfg / "credential_helper.py"), "run", "github.com", "gh",
                        "--", "pr", "list"], capture_output=True, text=True, env=env, timeout=30, cwd=repo)
    assert r.returncode == 7
    body = _Broker.seen[-1][2]
    assert body["command"] == ["pr", "list"] and body["remotes"] == ["https://github.com/octo/hello.git"]
    assert body["gh_repo"] == "" and body["gh_host"] == ""
    # gh's own repository and host settings go along: gh reads them before the remote.
    subprocess.run([sys.executable, "-I", str(cfg / "credential_helper.py"), "run", "github.com", "gh",
                    "--", "issue", "list"], capture_output=True, text=True, timeout=30, cwd=repo,
                   env={**env, "GH_REPO": "octo/secret", "GH_HOST": "github.com"})
    assert (_Broker.seen[-1][2]["gh_repo"], _Broker.seen[-1][2]["gh_host"]) == ("octo/secret", "github.com")
    # Outside a repository the remotes are none.
    plain = tmp_path / "plain"
    plain.mkdir()
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    subprocess.run([sys.executable, "-I", str(cfg / "credential_helper.py"), "run", "github.com", "gh",
                    "--", "api", "user"], capture_output=True, text=True, env=env, timeout=30, cwd=plain)
    assert _Broker.seen[-1][2]["remotes"] == []


def test_the_wrapper_runs_plainly_when_the_session_holds_no_account(env, tmp_path):
    """The broker's quiet 404 (no account for this session): the command
    runs as it would without the platform, nothing on stderr."""
    cfg = _rig_wrapper(tmp_path, env)
    r = _helper_run(cfg, env, "other.example", "x")
    assert r.returncode == 7 and r.stdout == "GH_TOKEN=unset GITHUB_TOKEN=unset\nargs=x\n"
    assert r.stderr == ""


@pytest.mark.parametrize("answer,reason", [
    ((409, {"detail": "the vendor ended the account's grant"}), "ended the account's grant"),
    ((403, {"error": "path-not-allowlisted"}), "1.8.0 update"),
    ((403, {}), "HTTP 403"),
    ((500, {"detail": "boom"}), "boom"),
])
def test_every_other_refusal_stops_the_command(env, tmp_path, answer, reason):
    """An account the platform holds but refuses (a reconnect needed, a
    satellite that needs its update, any other refusal) never falls through
    to the machine's own identity (its owner's gh login on a paired
    machine): the reason is printed and the command does not run."""
    cfg = _rig_wrapper(tmp_path, env)
    _Broker.answers["other.example"] = answer
    r = _helper_run(cfg, env, "other.example", "x")
    assert r.returncode == 1 and r.stdout == "" and reason in r.stderr


def test_an_unreachable_platform_stops_the_command_too(env, tmp_path):
    cfg = _rig_wrapper(tmp_path, env)
    env["PROXY_URL"] = "http://127.0.0.1:1"
    r = _helper_run(cfg, env, "github.com", "x")
    assert r.returncode == 1 and r.stdout == "" and "cannot be reached" in r.stderr


def test_a_relative_or_linked_path_entry_for_the_wrapper_dir_still_ends_at_the_real_command(env, tmp_path):
    """The wrapper directory is left out by its real path: a relative
    spelling (``bin`` with the config dir as cwd) or a link to it no longer
    makes gh exec its own wrapper without end, a broker request per turn."""
    cfg = _rig_wrapper(tmp_path, env)
    real = env["PATH"].split(":")[1]
    env["PATH"] = f"bin:{real}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    r = subprocess.run(["bin/gh", "--version"], capture_output=True, text=True, env=env,
                       cwd=cfg, timeout=30)
    assert r.returncode == 7 and r.stdout.endswith("args=--version\n"), r.stderr
    link = tmp_path / "linked-bin"
    link.symlink_to(cfg / "bin")
    env["PATH"] = f"{link}:{real}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    r = subprocess.run(["gh", "--version"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 7 and r.stdout.endswith("args=--version\n"), r.stderr
    assert len(_Broker.seen) == 2   # one request per command


def test_two_wrappers_on_path_hand_the_command_through_to_the_real_one(env, tmp_path):
    """Another config dir's wrapper ahead on PATH (a session that put its
    other engine's ``bin`` first): each helper leaves out its own dir and
    every dir an outer helper passed through (OTO_WRAPPED), so the chain
    ends at the real command instead of the two wrappers taking turns."""
    cfg = _rig_wrapper(tmp_path, env)
    other = tmp_path / ".codex"
    (other / "bin").mkdir(parents=True)
    (other / "credential_helper.py").write_bytes(HELPER.read_bytes())
    (other / "bin" / "gh").write_bytes((cfg / "bin" / "gh").read_bytes())
    (other / "bin" / "gh").chmod(0o755)
    env["PATH"] = f"{other / 'bin'}:{env['PATH']}"
    r = subprocess.run(["gh", "x"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 7 and r.stdout == "GH_TOKEN=ghp_live GITHUB_TOKEN=ghp_live\nargs=x\n", r.stderr
    assert len(_Broker.seen) == 2   # one request per helper, then the real gh


def test_a_missing_command_is_127(env, tmp_path):
    cfg = _rig_wrapper(tmp_path, env)
    r = subprocess.run(
        [sys.executable, "-I", str(cfg / "credential_helper.py"), "run", "github.com", "no-such-command-xyz", "--"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 127 and "not found" in r.stderr


def test_usage_for_anything_else(env):
    r = subprocess.run([sys.executable, "-I", str(HELPER), "what"], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "usage" in r.stderr
