"""The admin command line (``proxy/admin_cli.py``) and its launcher
(``scripts/otodock-admin.sh``): the sign-in recovery commands on the store
functions the dashboard uses, the audit line, the support information's
redaction, the background jobs, the refusal before any configuration is
read, and which install the launcher picks."""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import admin_cli
import config
from auth.password import hash_password, verify_password
from storage import database as task_store
from storage.identity import webauthn_store
from tests.deploy.fake_docker import write_fake

REPO = Path(__file__).resolve().parents[3]
# A phrase the strength rule accepts (a fake, never a real sign-in).
PHRASE = "Correct-Horse-Battery-2026!"


def _user(email="owner@example.test", *, provider="local", totp=False) -> str:
    sub = task_store.create_local_user(email, "Owner", "Owner", "admin", hash_password("old-Password-1!"))
    fields = {}
    if provider != "local":
        fields["auth_provider"] = provider
    if totp:
        fields.update(totp_enabled=True, totp_secret_enc="enc", totp_recovery_enc="enc")
    if fields:
        task_store.update_user_auth_fields(sub, **fields)
    return sub


@pytest.fixture
def platform_log(tmp_path, monkeypatch):
    """The platform's log file in a folder of its own (``BASE_DIR``)."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setenv("OTODOCK_ADMIN_ACTOR", "ops@box")
    log = tmp_path / "proxy.log"
    log.write_text("")
    return log


# ── user reset-password ──────────────────────────────────────────────────

def test_a_reset_prints_a_temporary_password_the_person_must_change(temp_db, platform_log):
    sub = _user()
    out = io.StringIO()
    assert admin_cli.reset_password("Owner@Example.test", from_stdin=False, out=out) == admin_cli.DONE
    temp = out.getvalue().split("Temporary password for owner@example.test: ", 1)[1].split()[0]
    row = task_store.get_user(sub)
    assert verify_password(temp, row["password_hash"]) and row["must_change_password"]
    assert "within a minute" in out.getvalue()
    line = platform_log.read_text()
    assert "INFO otodock: Admin command line ops@box reset password for " in line
    assert "owner@example.test" not in line and temp not in line


def test_a_password_from_stdin_is_held_to_the_strength_rule(temp_db, platform_log):
    sub = _user()
    with pytest.raises(admin_cli.Refused):
        admin_cli.reset_password("owner@example.test", from_stdin=True,
                                 stdin=io.StringIO("abc\n"), out=io.StringIO())
    out = io.StringIO()
    admin_cli.reset_password("owner@example.test", from_stdin=True,
                             stdin=io.StringIO(PHRASE + "\n"), out=out)
    row = task_store.get_user(sub)
    assert verify_password(PHRASE, row["password_hash"]) and not row["must_change_password"]
    assert PHRASE not in out.getvalue() and PHRASE not in platform_log.read_text()


def test_a_single_sign_on_or_unknown_account_is_refused(temp_db, platform_log):
    _user("sso@example.test", provider="oidc")
    with pytest.raises(admin_cli.Refused, match="single sign-on"):
        admin_cli.reset_password("sso@example.test", from_stdin=False, out=io.StringIO())
    with pytest.raises(admin_cli.Refused, match="no account"):
        admin_cli.reset_password("nobody@example.test", from_stdin=False, out=io.StringIO())
    assert platform_log.read_text() == ""


def test_the_log_file_is_never_created_by_the_command_line(temp_db, platform_log):
    platform_log.unlink()
    _user()
    admin_cli.reset_password("owner@example.test", from_stdin=False, out=io.StringIO())
    assert not platform_log.exists()


# ── user disable-2fa ─────────────────────────────────────────────────────

def test_disable_2fa_turns_the_code_off_and_keeps_the_passkeys_unless_asked(
        temp_db, platform_log, monkeypatch):
    sub = _user(totp=True)
    webauthn_store.add_credential("cred-1", sub, "pk", 0, "phone", ["internal"])
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://otodock.example.test")
    task_store.set_platform_setting("passkey_login_mode", "second_factor")
    out = io.StringIO()
    admin_cli.disable_2fa("owner@example.test", passkeys=False, out=out)
    row = task_store.get_user(sub)
    assert not row["totp_enabled"] and row["totp_secret_enc"] is None
    assert webauthn_store.count_credentials(sub) == 1
    assert "still ask for a second step" in out.getvalue() and "--passkeys" in out.getvalue()
    out = io.StringIO()
    admin_cli.disable_2fa("owner@example.test", passkeys=True, out=out)
    assert webauthn_store.count_credentials(sub) == 0
    assert "1 passkey(s) removed" in out.getvalue()
    log = platform_log.read_text()
    assert "Admin command line ops@box disabled 2FA for " in log
    assert "Admin command line ops@box removed 1 passkey(s) for " in log
    out = io.StringIO()
    admin_cli.disable_2fa("owner@example.test", passkeys=True, out=out)
    assert "nothing to turn off" in out.getvalue()


def test_an_enrolment_never_confirmed_is_nothing_to_turn_off(temp_db, platform_log):
    sub = _user()
    task_store.update_user_auth_fields(sub, totp_secret_enc="pending", totp_enabled=False)
    out = io.StringIO()
    admin_cli.disable_2fa("owner@example.test", passkeys=False, out=out)
    assert "nothing to turn off" in out.getvalue()
    assert task_store.get_user(sub)["totp_secret_enc"] == "pending"
    assert platform_log.read_text() == ""


def test_a_changing_command_names_the_database_it_will_change(temp_db, monkeypatch, capsys):
    """A shell without the service's environment may reach another install:
    the target is named first, by host, port and name, never the password."""
    from storage import pg
    monkeypatch.setattr(pg, "close_pool", lambda timeout=0: None)
    monkeypatch.setattr(config, "DATABASE_URL",
                        "postgresql://otodock:s3cret-pw@db.example.test:5544/otodock_live")
    line = admin_cli.target_line()
    assert "database db.example.test:5544/otodock_live" in line and "config " in line
    assert "s3cret" not in line and "otodock:" not in line
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@[bad")
    assert "does not parse" in admin_cli.target_line()
    monkeypatch.setattr(admin_cli, "target_line", lambda: "admin_cli: TARGET")
    assert admin_cli.main(["jobs", "list"]) == admin_cli.DONE
    assert "TARGET" not in capsys.readouterr().err
    assert admin_cli.main(["jobs", "run", "nope"]) == admin_cli.REFUSED
    err = capsys.readouterr().err
    assert err.index("TARGET") < err.index("no job is named")


def test_a_command_leaves_the_platform_logger_as_it_found_it(temp_db, monkeypatch, capsys):
    """The command's handler is bound to its own stderr: left on the
    process-wide logger it would write to a closed stream from whatever
    runs next in the same process, and the logger would stop propagating."""
    from storage import pg
    monkeypatch.setattr(pg, "close_pool", lambda timeout=0: None)
    before = (list(admin_cli.logger.handlers), admin_cli.logger.level, admin_cli.logger.propagate)
    assert admin_cli.main(["jobs", "list"]) == admin_cli.DONE
    assert (list(admin_cli.logger.handlers), admin_cli.logger.level,
            admin_cli.logger.propagate) == before


# ── support-info ─────────────────────────────────────────────────────────

SECRETS = {
    "JWT_SECRET": "jwt-secret-value-0001",
    "PROXY_API_KEY": "proxy-key-value-0002",
    "POSTGRES_PASSWORD": "pg-pass-value-0003",
    "DATABASE_URL": "postgresql://otodock:Abc#123/x?y@db:5432/otodock",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.example/services/T000/B000/tokenvalue0004",
    "PHONE_PIN": "4321",
    "ODD_URL": "http://user:pwvalue0005@host:80/",
}


@pytest.mark.parametrize("key, value, shown", [
    ("OIDC_ENABLED", "true", "true"),
    ("PROXY_PORT", "8400", "8400"),
    ("DASHBOARD_PUBLIC_URL", "https://otodock.example.test/path?q=1", "https://otodock.example.test"),
    ("OLLAMA_URL", "http://10.0.0.5:11434", "http://10.0.0.5:11434"),
    ("DATABASE_URL", "postgresql://otodock:Abc#123@db:5432/otodock", "set (hidden)"),
    ("SOME_URL", "postgresql://otodock:Abc/123@db/x", "set (hidden)"),
    ("SOME_URL", "http://h:notaport/", "set (hidden)"),
    ("PHONE_PIN", "4321", "set (hidden)"),
    ("TWILIO_CALLER", "+306912345678", "set (hidden)"),
    ("SOME_ACCOUNT", "123456789012", "set (hidden)"),
    ("SILERO_THRESHOLD", "0.45", "0.45"),
    ("OTODOCK_LOG_LEVEL", "debug", "set (hidden)"),
])
def test_a_config_value_prints_only_when_it_cannot_hold_a_secret(key, value, shown):
    assert admin_cli.shown_value(key, value) == shown


def test_support_info_prints_no_secret(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_file_cfg", {**SECRETS, "OIDC_ENABLED": "true", "NOT_A_KEY": "z"})
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    out = io.StringIO()
    assert admin_cli.support_info(out=out) == admin_cli.DONE
    text = out.getvalue()
    assert "release: " in text and "postgres: " in text and "users: " in text
    assert "  OIDC_ENABLED = true" in text and "  JWT_SECRET = set (hidden)" in text
    unknown = text.split("no OtoDock component reads: ", 1)[1]
    assert "NOT_A_KEY" in unknown and "POSTGRES_PASSWORD" not in unknown
    for value in SECRETS.values():
        for piece in (value, "Abc", "0001", "0002", "0003", "0004", "4321", "pwvalue"):
            if piece in value:
                assert piece not in text, piece


# ── jobs ─────────────────────────────────────────────────────────────────

def _sweep_names() -> list[str]:
    tree = ast.parse((REPO / "proxy" / "startup.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "sweep_items"
                                                  for t in node.targets)):
            return [e.elts[0].value for e in node.value.elts]
    raise AssertionError("startup.py has no sweep_items")


def test_the_jobs_list_is_the_platforms_minute_sweep():
    assert [j[1] for j in admin_cli.JOBS] == _sweep_names()
    out = io.StringIO()
    admin_cli.jobs_list(out=out)
    assert "recover-bin-reap" in out.getvalue() and "inside the platform" in out.getvalue()


def test_a_job_runs_here_only_when_it_needs_no_server_state(temp_db, tmp_path, monkeypatch):
    out = io.StringIO()
    assert admin_cli.jobs_run("tombstone-reap", out=out) == admin_cli.DONE
    assert "tombstone-reap: done, 0 removed" in out.getvalue()
    with pytest.raises(admin_cli.Refused, match="inside the"):
        admin_cli.jobs_run("retention", out=io.StringIO())
    with pytest.raises(admin_cli.Refused, match="no job"):
        admin_cli.jobs_run("nope", out=io.StringIO())
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path / "missing")
    with pytest.raises(admin_cli.Refused, match="not writable"):
        admin_cli.jobs_run("recover-bin-reap", out=io.StringIO())


# ── nothing is read without a configuration ──────────────────────────────

def test_no_config_and_no_database_url_stops_before_config_is_read(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "TEST_DATABASE_URL") and not k.startswith("PLATFORM_")}
    env["PLATFORM_CONFIG_DIR"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(REPO / "proxy" / "admin_cli.py"), "support-info"],
                       cwd=REPO / "proxy", env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == admin_cli.REFUSED
    assert "no config.env here and no DATABASE_URL" in r.stderr
    assert list(tmp_path.iterdir()) == []


def test_a_usage_error_is_exit_2():
    assert admin_cli.main(["user"]) == admin_cli.USAGE


# ── the launcher ─────────────────────────────────────────────────────────

STUB_PYTHON = """#!/bin/sh
echo "ran: $*"
echo "actor: $OTODOCK_ADMIN_ACTOR"
read -r line || line=""
echo "stdin: $line"
"""


def _stub_proxy(root: Path, *, with_cli=True) -> Path:
    proxy = root / "proxy"
    (proxy / "venv" / "bin").mkdir(parents=True)
    py = proxy / "venv" / "bin" / "python"
    py.write_text(STUB_PYTHON)
    py.chmod(0o755)
    if with_cli:
        (proxy / "admin_cli.py").write_text("# the platform's admin command line\n")
    return proxy


def _container(cid, working_dir, proxy_dir, *, project="otodock"):
    return {"id": cid, "name": f"{project}-otodock-proxy-1", "running": True,
            "labels": {"com.docker.compose.project": project,
                       "com.docker.compose.service": "otodock-proxy",
                       "com.docker.compose.project.working_dir": str(working_dir)},
            "mounts": {"/opt/otodock/proxy": {"source": str(proxy_dir)}}}


def _launch(script: Path, tmp_path, containers, *args, stdin="", mode=None):
    bindir = tmp_path / "bin"
    if not (bindir / "docker").exists():
        write_fake(bindir)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTODOCK_", "COMPOSE_"))}
    env.update({"PATH": f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}",
                "FAKE_CONTAINERS": json.dumps(containers), "FAKE_LOG": str(tmp_path / "docker.log")})
    if mode:
        env["OTODOCK_ADMIN_MODE"] = mode
    return subprocess.run(["bash", str(script), *args], input=stdin, env=env,
                          capture_output=True, text=True, timeout=60)


def _install(tmp_path) -> Path:
    inst = tmp_path / "install"
    inst.mkdir()
    (inst / "docker-compose.yml").write_text("services: {}\n")
    shutil.copy2(REPO / "scripts" / "otodock-admin.sh", inst / "otodock-admin.sh")
    return inst


def test_a_docker_install_runs_the_command_in_its_own_proxy_container(tmp_path):
    inst = _install(tmp_path)
    image_proxy = _stub_proxy(tmp_path / "image")
    other = _container("c-other", tmp_path / "elsewhere", image_proxy)
    mine = _container("c-mine", inst, image_proxy)
    r = _launch(inst / "otodock-admin.sh", tmp_path, [other, mine], "support-info")
    assert r.returncode == 0, r.stderr
    assert f"ran: {image_proxy}/admin_cli.py support-info" in r.stdout
    assert "actor: " in r.stdout and "@" in r.stdout
    execs = [json.loads(line) for line in (tmp_path / "docker.log").read_text().splitlines()
             if json.loads(line)[:1] == ["exec"]]
    assert all("c-mine" in e for e in execs) and execs


def test_a_password_piped_in_reaches_the_command_line(tmp_path):
    inst = _install(tmp_path)
    proxy = _stub_proxy(tmp_path / "image")
    piped = PHRASE + "\n"
    command = ["user", "reset-password", "a@b.test", "--password-stdin"]
    r = _launch(inst / "otodock-admin.sh", tmp_path, [_container("c1", inst, proxy)], *command,
                stdin=piped)
    assert r.returncode == 0, r.stderr
    assert f"stdin: {PHRASE}" in r.stdout


@pytest.mark.parametrize("case", ["none", "another-folder", "older-release", "two"])
def test_the_launcher_refuses_a_container_it_cannot_use(tmp_path, case):
    inst = _install(tmp_path)
    proxy = _stub_proxy(tmp_path / "image", with_cli=case != "older-release")
    containers = {
        "none": [],
        "another-folder": [_container("c1", tmp_path / "elsewhere", proxy)],
        "older-release": [_container("c1", inst, proxy)],
        "two": [_container("c1", inst, proxy), _container("c2", inst, proxy)],
    }[case]
    r = _launch(inst / "otodock-admin.sh", tmp_path, containers, "support-info")
    assert r.returncode == 1
    expected = {"none": "no running otodock-proxy container", "another-folder": "no running otodock-proxy",
                "older-release": "needs OtoDock 1.9.0 or later", "two": "more than one"}[case]
    assert expected in r.stderr
    assert "ran:" not in r.stdout


def test_a_source_checkout_runs_its_venv_unless_its_own_stack_runs(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "docker-compose.yml").write_text("services: {}\n")
    shutil.copy2(REPO / "scripts" / "otodock-admin.sh", checkout / "scripts" / "otodock-admin.sh")
    venv_proxy = _stub_proxy(checkout)
    image_proxy = _stub_proxy(tmp_path / "image")
    script = checkout / "scripts" / "otodock-admin.sh"
    # Another stack under the same project name runs on this host: the venv.
    r = _launch(script, tmp_path, [_container("c-t2", tmp_path / "t2", image_proxy)], "jobs", "list")
    assert r.returncode == 0, r.stderr
    assert "ran: admin_cli.py jobs list" in r.stdout
    assert venv_proxy.exists()
    # This checkout's own stack runs: its container.
    r = _launch(script, tmp_path, [_container("c-own", checkout, image_proxy)], "jobs", "list")
    assert r.returncode == 0, r.stderr
    assert f"ran: {image_proxy}/admin_cli.py jobs list" in r.stdout
    # A forced mode wins.
    r = _launch(script, tmp_path, [_container("c-own", checkout, image_proxy)], "jobs", "list",
                mode="source")
    assert "ran: admin_cli.py jobs list" in r.stdout
