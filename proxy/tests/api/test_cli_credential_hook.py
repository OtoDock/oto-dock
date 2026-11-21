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


def _provision(sid: str, tmp_path, raw: dict | None = None, *, per_command: bool = True) -> CliCredential:
    """A session's command-line credential with no placeholder: built per
    command (a paired macOS or Windows computer) unless ``per_command`` is
    False, the hook's tests of the bearer and the reach run on it."""
    (tmp_path / "acct.json").write_text(json.dumps(
        raw if raw is not None else {"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cred = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"),
                         env_names=("GH_TOKEN", "GITHUB_TOKEN"), manifest="", per_command=per_command)
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
                            "env": ["GH_TOKEN", "GITHUB_TOKEN"], "process_env": {}}
        # the current file, read at the call
        (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_rotated", "expires_at": "", "extra": {}}))
        r = _post(sid, _cli(sid), host="GitHub.com")
        assert r.json()["password"] == "ghp_rotated"
        # A session with no person (an agent-scope task) has its token too.
        assert _post(sid, _cli(sid, "")).status_code == 200
    finally:
        mcp_broker.purge_session(sid)


def test_a_session_that_holds_no_placeholder_and_no_per_command_credential_gets_no_token(tmp_path, caplog):
    """Everywhere but a paired macOS or Windows computer the session holds a
    placeholder: a credential without one hands nothing, never the token."""
    import logging
    sid = str(uuid.uuid4())
    _provision(sid, tmp_path, per_command=False)
    try:
        with caplog.at_level(logging.WARNING, logger="otodock"):
            r = _post(sid, _cli(sid))
        assert r.status_code == 409
        assert r.json() == {"detail": "This session holds no key for github.com. Start a new chat."}
        assert "ghp_live" not in r.text and "ghp_live" not in caplog.text
        assert any("holds no placeholder for github.com" in m for m in caplog.messages)
    finally:
        mcp_broker.purge_session(sid)


def test_a_sentence_names_a_host_only_when_it_is_a_host_name(tmp_path, caplog):
    """The host a request names reaches an answer and a log line only as a
    host name (letters, digits, dots, dashes and a port), cut to 253; any
    other text leaves the sentence without a host."""
    import logging
    sid = str(uuid.uuid4())
    token = _cli(sid)
    odd = "git hub.com\nnext line"
    assert _post(sid, token, host=odd).json() == {"detail": "No account for this host is connected for this session"}
    assert _post(sid, token, host="GitLab.com:8443").json() == {
        "detail": "No gitlab.com:8443 account is connected for this session"}
    long = "a" * 300 + ".com"
    assert _post(sid, token, host=long).json() == {"detail": f"No {'a' * 253} account is connected for this session"}
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "ghp_live", "expires_at": "", "extra": {}}))
    cred = CliCredential(host=odd, token_ref=TokenRef(str(tmp_path), "acct"), env_names=("GH_TOKEN",), manifest="")
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(cli=cred)})
    try:
        with caplog.at_level(logging.WARNING, logger="otodock"):
            r = _post(sid, token, host=odd)
        assert r.status_code == 409
        assert r.json() == {"detail": "This session holds no key for this host. Start a new chat."}
        assert not any("next line" in m for m in caplog.messages)
    finally:
        mcp_broker.purge_session(sid)


def test_the_per_command_mark_survives_the_descriptor():
    cred = CliCredential(host="github.com", token_ref=TokenRef("/t", "a"), per_command=True)
    assert CliCredential.from_descriptor(cred.descriptor()) == cred
    plain = CliCredential(host="github.com", token_ref=TokenRef("/t", "a"))
    assert CliCredential.from_descriptor(plain.descriptor()).per_command is False


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
        "core/layers/cli/spawn.py": "build_spawn_command",
        "core/layers/codex/env.py": "daemon_env",
        "core/layers/codex/spawn.py": "_tui_env",
        "core/remote/remote_start_payload.py": "_build_start_payload",
    }
    for rel, func in sites.items():
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        bodies = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func]
        calls = [c for b in bodies for c in ast.walk(b) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "attach_cli_token"]
        assert calls, f"{rel}:{func} sets no command-line token"


def test_a_session_behind_the_exit_gets_its_placeholder_as_the_password(tmp_path):
    """The build held a placeholder for the host
    (``mcp_session_config._exit_placeholders``): git and gh get it as the
    password and the exit writes the token on the way out, so the token
    never enters the session."""
    from core.credentials.mcp_gateway import ExitCredential
    sid = str(uuid.uuid4())
    cred = _provision(sid, tmp_path, per_command=False)
    holder = ExitCredential(placeholder="oto-exit-" + "a" * 32, header="Authorization",
                            hosts=("github.com", "*.github.com"), allowlist_key="github-mcp",
                            manifest="github-mcp", cli=cred)
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(cli=cred, exit=(holder,))})
    try:
        r = _post(sid, _cli(sid))
        assert r.status_code == 200
        assert r.json()["password"] == holder.placeholder and r.json()["username"] == "x-access-token"
        assert "ghp_live" not in r.text
    finally:
        mcp_broker.purge_session(sid)


def test_on_a_paired_computer_gh_gets_the_placeholder_and_the_machines_exit_variables(tmp_path, monkeypatch):
    """The hook hands the placeholder and, for a session on a paired
    computer, the variables gh's process needs to reach the host through
    the machine's exit; a session with no door of a machine's exit gets the
    placeholder alone."""
    from core.credentials import credential_exit
    from core.credentials.mcp_gateway import ExitCredential
    sid = str(uuid.uuid4())
    cred = _provision(sid, tmp_path, per_command=False)
    placeholder = credential_exit.new_placeholder()
    entry = ExitCredential(placeholder=placeholder, header="Authorization", hosts=("github.com", "*.github.com"),
                           allowlist_key="github-mcp", manifest="github-mcp", cli=cred)
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(cli=cred, exit=(entry,))})
    machine_env = {"HTTPS_PROXY": "http://oto:s@127.0.0.1:41111", "SSL_CERT_FILE": "/home/u/.oto-dock/credential-exit/bundle.pem"}
    credential_exit._sessions[sid] = credential_exit._Session(secret="s", env=dict(machine_env))
    try:
        r = _post(sid, _cli(sid))
        assert r.status_code == 200
        assert r.json()["password"] == placeholder and r.json()["process_env"] == machine_env
        # A compute session's variables are the sandbox's: nothing from the hook.
        credential_exit._sessions[sid].compute = True
        assert _post(sid, _cli(sid)).json()["process_env"] == {}
        # No door held for the session: the placeholder alone.
        credential_exit.forget(sid)
        r = _post(sid, _cli(sid))
        assert r.json()["password"] == placeholder and r.json()["process_env"] == {}
    finally:
        credential_exit.forget(sid)
        mcp_broker.purge_session(sid)


# ---------------------------------------------------------------------------
# The exit's ask for one request (``request``): a verdict, never a credential
# ---------------------------------------------------------------------------

def _held(sid: str, tmp_path, reach: dict | None):
    """A session behind the exit whose command-line credential carries ``reach``."""
    from core.credentials.mcp_gateway import ExitCredential
    cred = _provision(sid, tmp_path, per_command=False)
    cred.reach = reach
    holder = ExitCredential(placeholder="oto-exit-" + "b" * 32, header="Authorization",
                            hosts=("github.com", "*.github.com"), allowlist_key="github-mcp",
                            manifest="github-mcp", cli=cred)
    mcp_broker.provision(sid, {"github-mcp": mcp_broker.SecretBundle(cli=cred, exit=(holder,))})
    return holder


def _ask(sid: str, token: str, placeholder: str, method: str, host: str, target: str, **more):
    return client.post("/v1/hooks/cli-credential", headers={"Authorization": f"Bearer {token}"}, json={
        "session_id": sid, "host": host,
        "request": {"placeholder": placeholder, "method": method, "host": host, "target": target, **more}})


def test_the_exits_ask_is_answered_with_a_verdict_and_never_a_credential(tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    holder = _held(sid, tmp_path, {"repositories": ["acme/app"], "read_only": True})
    try:
        token = _cli(sid)
        go = _ask(sid, token, holder.placeholder, "GET", "api.github.com", "/repos/acme/app/issues")
        assert go.status_code == 200 and set(go.json()) == {"verdict"}
        assert go.json()["verdict"] == {
            "status": 0, "detail": "", "content_type": "", "wants_body": 0, "body_b64": None,
            "link": [r"https://api\.github\.com/repositories/\d+/", "https://api.github.com/repos/acme/app/"]}
        outside = _ask(sid, token, holder.placeholder, "GET", "api.github.com", "/repos/other/repo")
        assert outside.json()["verdict"]["status"] == 403
        assert outside.json()["verdict"]["detail"].endswith("other/repo is not one of them.")
        assert outside.json()["verdict"]["content_type"].startswith("application/json")
        push = _ask(sid, token, holder.placeholder, "POST", "github.com", "/acme/app.git/git-receive-pack")
        assert push.json()["verdict"]["status"] == 403 and push.json()["verdict"]["content_type"] == ""
        assert "git push was refused" in push.json()["verdict"]["detail"]
        # A body the rule asks for, then judged and answered rebuilt.
        first = _ask(sid, token, holder.placeholder, "POST", "api.github.com", "/graphql")
        assert first.json()["verdict"]["wants_body"] == 32 * 1024
        import base64
        body = base64.b64encode(b'{"query": "{viewer{login}}"}').decode()
        second = _ask(sid, token, holder.placeholder, "POST", "api.github.com", "/graphql",
                      content_type="application/json", body_b64=body)
        assert json.loads(base64.b64decode(second.json()["verdict"]["body_b64"])) == {"query": "{viewer{login}}"}
        # Neither the token nor the placeholder is ever in a verdict.
        for answer in (go, outside, push, first, second):
            assert "ghp_live" not in answer.text and "password" not in answer.text
    finally:
        mcp_broker.purge_session(sid)


def test_the_exits_ask_takes_the_command_line_token_alone(tmp_path, monkeypatch):
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    holder = _held(sid, tmp_path, {"repositories": ["acme/app"], "read_only": False})
    try:
        args = (holder.placeholder, "GET", "api.github.com", "/repos/acme/app")
        assert _ask(sid, live_session_token(sid, "agent", "user-admin"), *args).status_code == 403
        # A session token from before 1.9 has the route's grace for a command, never for the exit's ask.
        assert _ask(sid, _pre_upgrade_session_token(sid), *args).status_code == 403
        assert _ask(sid, _cli(other), *args).status_code == 403
        assert _ask(sid, config.API_KEY, *args).status_code == 403
        assert client.post("/v1/hooks/cli-credential", json={
            "session_id": sid, "host": "github.com", "request": {
                "placeholder": holder.placeholder, "method": "GET", "host": "api.github.com",
                "target": "/repos/acme/app"}}).status_code == 401
        assert _ask(sid, _cli(sid), *args).status_code == 200
    finally:
        mcp_broker.purge_session(sid)


def test_a_placeholder_the_session_does_not_hold_and_one_of_a_tool_are_refused(tmp_path, monkeypatch):
    from core.credentials.mcp_gateway import ExitCredential
    sid = str(uuid.uuid4())
    _provision(sid, tmp_path)
    tool = ExitCredential(placeholder="oto-exit-" + "c" * 32, header="xi-api-key", hosts=("api.elevenlabs.io",),
                          allowlist_key="music", manifest="music", env_name="ELEVENLABS_API_KEY", value="v")
    mcp_broker.provision(sid, {"music": mcp_broker.SecretBundle(env={}, exit=(tool,))})
    try:
        for placeholder in ("oto-exit-" + "d" * 32, tool.placeholder):
            r = _ask(sid, _cli(sid), placeholder, "GET", "api.github.com", "/repos/acme/app")
            assert r.status_code == 200 and r.json()["verdict"]["status"] == 403
            assert r.json()["verdict"]["detail"] == "This session holds no such credential"
    finally:
        mcp_broker.purge_session(sid)


def test_an_ask_that_is_not_base64_or_past_the_bound_is_refused(tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    holder = _held(sid, tmp_path, {"repositories": [], "read_only": True})
    try:
        bad = _ask(sid, _cli(sid), holder.placeholder, "POST", "api.github.com", "/graphql", body_b64="***")
        assert bad.status_code == 400
        huge = _ask(sid, _cli(sid), holder.placeholder, "POST", "api.github.com", "/graphql",
                    body_b64="A" * 44_001)
        assert huge.status_code == 422
        # The largest body the rule reads fits the ask: the middleware gives a
        # command-line token the body tier of a request with no credential.
        import base64

        from auth import github_reach
        full = base64.b64encode(b'{"query": "{' + b"a" * (github_reach.REQUEST_BODY_MAX - 14) + b'}"}').decode()
        assert len(full) <= 44_000
        fits = _ask(sid, _cli(sid), holder.placeholder, "POST", "api.github.com", "/graphql",
                    content_type="application/json", body_b64=full)
        assert fits.status_code == 200 and fits.json()["verdict"]["status"] == 0
        long_target = _ask(sid, _cli(sid), holder.placeholder, "GET", "api.github.com", "/" + "a" * 8192)
        assert long_target.status_code == 422
    finally:
        mcp_broker.purge_session(sid)
