"""The command-line credential hook (``POST /v1/hooks/cli-credential``):
the session's command-line token (``OTO_CLI_TOKEN``, which the shell holds
and no MCP child does) opens it and gets the host's credential from the
current token file with the names the wrapper sets; the session token
itself is refused unless it was minted before 1.9 (a session started
before the upgrade has no command-line token); another session's token,
the master key, a stdio MCP's capability token, a dead session and a gone
holder are refused; a session without the host answers 404; an account
that needs a reconnect answers 409 with the resolver's wording.
"""

from __future__ import annotations

import json
import time
import uuid

import jwt
from fastapi.testclient import TestClient

import config
from app import app
from auth.session_token import CLI_TOKEN_ENV, attach_cli_token, create_session_token
from core.credentials import mcp_broker
from core.credentials.mcp_gateway import CliCredential, TokenRef
from tests.conftest import live_session_token

client = TestClient(app)


def _provision(sid: str, tmp_path, raw: dict | None = None) -> CliCredential:
    (tmp_path / "acct.json").write_text(json.dumps(
        raw if raw is not None else {"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cred = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"),
                         env_names=("GH_TOKEN", "GITHUB_TOKEN"), manifest="")
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(cli=cred)})
    return cred


def _cli(sid: str, user_sub: str = "user-admin", *, live: bool = True) -> str:
    """The command-line token a spawn sets beside a (live) session token."""
    token = (live_session_token(sid, "agent", user_sub) if live
             else create_session_token(sid, "agent", user_sub))
    env = {"PROXY_API_KEY": token}
    attach_cli_token(env)
    return env[CLI_TOKEN_ENV]


def _pre_upgrade_session_token(sid: str, user_sub: str = "user-admin") -> str:
    """A session token as 1.8 signed it (the raw secret, no audience)."""
    live_session_token(sid, "agent", user_sub)   # marks the session live
    now = int(time.time())
    return jwt.encode({"type": "session", "sid": sid, "agent": "agent", "user_sub": user_sub,
                       "iat": now, "exp": now + 3600}, config.JWT_SECRET, algorithm="HS256")


def _post(sid: str, token: str, host: str = "github.com"):
    return client.post("/v1/hooks/cli-credential", json={"session_id": sid, "host": host},
                       headers={"Authorization": f"Bearer {token}"})


def test_the_sessions_command_line_token_gets_the_hosts_credential(tmp_path):
    sid = str(uuid.uuid4())
    _provision(sid, tmp_path)
    try:
        r = _post(sid, _cli(sid))
        assert r.status_code == 200
        assert r.json() == {"username": "x-access-token", "password": "ghp_live",
                            "env": ["GH_TOKEN", "GITHUB_TOKEN"]}
        # the current file, read at the call
        (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_rotated", "expires_at": "", "extra": {}}))
        r = _post(sid, _cli(sid), host="GitHub.com")
        assert r.json()["password"] == "ghp_rotated"
        # A session with no person (an agent-scope task) has its token too.
        assert _post(sid, _cli(sid, "")).status_code == 200
    finally:
        mcp_broker.purge_session(sid)


def _post_named(sid: str, token: str, **names):
    return client.post("/v1/hooks/cli-credential", json={"session_id": sid, "host": "github.com", **names},
                       headers={"Authorization": f"Bearer {token}"})


def test_a_narrowed_reach_judges_what_each_command_names(tmp_path):
    sid = str(uuid.uuid4())
    cred = _provision(sid, tmp_path)
    cred.reach = {"repositories": ["octo/hello"], "read_only": True}
    try:
        cli = _cli(sid)
        assert _post_named(sid, cli, path="octo/hello.git").json()["password"] == "ghp_live"
        r = _post_named(sid, cli, path="octo/secret.git")
        assert r.status_code == 403 and "octo/secret is not one of them" in r.json()["detail"]
        r = _post_named(sid, cli)                                  # an older helper names nothing
        assert r.status_code == 403 and "Start a new session" in r.json()["detail"]
        r = _post_named(sid, cli, path="otodock-read-only/octo/hello.git")
        assert r.status_code == 403 and r.json()["detail"].endswith("git push was refused.")
        ok = _post_named(sid, cli, command=["pr", "list"], remotes=["git@github.com:octo/hello.git"])
        assert ok.status_code == 200
        r = _post_named(sid, cli, command=["pr", "create"], remotes=["git@github.com:octo/hello.git"])
        assert r.status_code == 403 and "it did not run" in r.json()["detail"]
        r = _post_named(sid, cli, command=["pr", "list"], remotes=["git@github.com:octo/hello.git"],
                        gh_repo="octo/secret")
        assert r.status_code == 403 and "octo/secret is not one of them" in r.json()["detail"]
        r = _post_named(sid, cli, command=["pr", "list"], gh_repo="octo/hello", gh_host="ghe.example")
        assert r.status_code == 403 and "set to another host" in r.json()["detail"]
        assert "ghp_" not in r.text
        # No reach: nothing is judged, the old body still answers.
        cred.reach = None
        assert _post_named(sid, cli, path="anyone/anything").status_code == 200
        assert _post(sid, cli).status_code == 200
    finally:
        mcp_broker.purge_session(sid)


def test_the_git_entries_of_a_narrowed_reach():
    from auth import github_reach
    from services.oauth import credential_resolver
    plain = credential_resolver.git_helper_env("github.com")
    assert plain["GIT_CONFIG_COUNT"] == "2"
    env = credential_resolver.git_helper_env("github.com", reach=github_reach.reach_of(["a/b"], True))
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert (env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"]) == (
        "credential.https://github.com.useHttpPath", "true")
    assert (env["GIT_CONFIG_KEY_3"], env["GIT_CONFIG_VALUE_3"]) == (
        "url.https://github.com/otodock-read-only/.pushInsteadOf", "https://github.com/")
    subset = credential_resolver.git_helper_env("github.com", reach=github_reach.reach_of(["a/b"], False))
    assert subset["GIT_CONFIG_COUNT"] == "3"


def test_the_session_token_of_a_session_started_after_the_upgrade_is_refused(tmp_path):
    """The token every MCP child holds: a session minted by this release
    has a command-line token, so its session token opens nothing here."""
    sid = str(uuid.uuid4())
    _provision(sid, tmp_path)
    try:
        r = _post(sid, live_session_token(sid, "agent", "user-admin"))
        assert r.status_code == 403
        assert r.json()["detail"] == "This session's command-line token is required"
        assert "ghp_live" not in r.text
    finally:
        mcp_broker.purge_session(sid)


def test_a_session_started_before_the_upgrade_keeps_its_session_token(tmp_path):
    """Re-adopted after the restart, its env has no command-line token and
    its helper presents the session token 1.8 minted: taken until it expires,
    with the same session and holder checks as before."""
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    _provision(sid, tmp_path)
    try:
        r = _post(sid, _pre_upgrade_session_token(sid))
        assert r.status_code == 200 and r.json()["password"] == "ghp_live"
        assert _post(sid, _pre_upgrade_session_token(other)).status_code == 403
    finally:
        mcp_broker.purge_session(sid)


def test_another_sessions_token_the_master_key_and_a_capability_token_are_refused(tmp_path):
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    _provision(sid, tmp_path)
    try:
        r = _post(sid, _cli(other))
        assert r.status_code == 403
        r = _post(sid, config.API_KEY)
        assert r.status_code == 403
        r = _post(sid, mcp_broker.mint_token(sid, "github-mcp"))
        assert r.status_code == 401
        r = client.post("/v1/hooks/cli-credential", json={"session_id": sid, "host": "github.com"})
        assert r.status_code == 401
        # A session that is not live, and a holder who is gone.
        dead = str(uuid.uuid4())
        assert _post(dead, _cli(dead, live=False)).status_code == 401
        assert _post(sid, _cli(sid, "user-nobody-has")).status_code == 401
        # A command-line token is no session token anywhere else.
        r = client.get("/v1/session/current", headers={"Authorization": f"Bearer {_cli(sid)}"})
        assert r.status_code == 401
    finally:
        mcp_broker.purge_session(sid)


def test_no_credential_for_the_host_is_404_and_a_dead_account_409(tmp_path):
    sid = str(uuid.uuid4())
    token = _cli(sid)
    r = _post(sid, token)
    assert r.status_code == 404 and "github.com" in r.json()["detail"]
    _provision(sid, tmp_path)
    try:
        r = _post(sid, token, host="gitlab.com")
        assert r.status_code == 404
        (tmp_path / "acct.json").write_text(json.dumps({
            "access_token": "ghp_old", "expires_at": "", "extra": {"refresh_failed": "invalid_grant"},
        }))
        r = _post(sid, token)
        assert r.status_code == 409 and "ended the account's grant" in r.json()["detail"]
        assert "ghp_old" not in r.text
    finally:
        mcp_broker.purge_session(sid)
    # purged: the store is empty again
    r = _post(sid, token)
    assert r.status_code == 404


def test_an_external_callers_session_gets_no_command_line_token_and_the_route_refuses_one(tmp_path):
    """A phone caller who is no platform user has no shell: no token is set
    for its session, and one minted by hand is refused as its session token
    is on every route outside its tools'."""
    from auth import tokens
    sid = str(uuid.uuid4())
    session = live_session_token(sid, "agent", "", external="phone:+300000000")
    env = {"PROXY_API_KEY": session}
    attach_cli_token(env)
    assert CLI_TOKEN_ENV not in env
    _provision(sid, tmp_path)
    try:
        claims = {k: v for k, v in tokens.decode(session, tokens.SESSION).items() if k != "aud"}
        r = _post(sid, tokens.mint(tokens.CLI, claims))
        assert r.status_code == 403 and "external" in r.json()["detail"]
    finally:
        mcp_broker.purge_session(sid)


def test_every_engine_spawn_that_hosts_a_shell_attaches_the_token():
    """The four places an env with a shell is assembled: the Claude CLI
    (headless and the TUI), the Codex daemon, the Codex TUI and every remote
    start. A new spawn path must join this list or its git and gh fail."""
    import ast
    from pathlib import Path
    root = Path(config.BASE_DIR)
    sites = {
        "core/layers/cli/session.py": "build_spawn_command",
        "core/layers/codex/session.py": "_build_env",
        "core/layers/codex/layer.py": "_tui_env",
        "core/remote/remote_start_payload.py": "_build_start_payload",
    }
    for rel, func in sites.items():
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        bodies = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func]
        calls = [c for b in bodies for c in ast.walk(b) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "attach_cli_token"]
        assert calls, f"{rel}:{func} sets no command-line token"
