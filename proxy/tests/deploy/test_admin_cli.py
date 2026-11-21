"""The admin command line (``proxy/admin_cli.py``) and its launcher
(``scripts/otodock-admin.sh``): the sign-in recovery commands on the store
functions the dashboard uses, the audit line, the support information's
redaction, the background jobs, the refusal before any configuration is
read, and which install the launcher picks. ``drain``, ``resume`` and
``status`` run against a stand-in for the server's two internal routes on
a free loopback port."""

from __future__ import annotations

import ast
import contextlib
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import admin_cli
import config
from auth.password import hash_password, verify_password
from storage import database as task_store
from storage.identity import webauthn_store
from tests.deploy.fake_docker import host_commands_named, write_fake

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
    tree = ast.parse((REPO / "proxy" / "startup_sweeps.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "sweep_items"
                                                  for t in node.targets)):
            return [e.elts[0].value for e in node.value.elts]
    raise AssertionError("startup_sweeps.py has no sweep_items")


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


# ── drain, resume, status: the running server's open work ────────────────

KEY = "master-key-of-the-test"
NO_ANSWER = "The server did not answer the drain. It is not running, or it is older than 2.0."
HELD_IDLE = "Nothing is working. New work is held until the platform is back."


def _work(state="running", *, drained=False, waiting=0, queued=0, **kinds) -> dict:
    """The server's report of its open work."""
    counts = dict.fromkeys(("turns", "runs", "calls", "duplex", "meetings", "terminals", "moves",
                            "background"), 0)
    counts.update(kinds)
    return {"state": state, "drained": drained, "busy": any(counts.values()), "open": counts,
            "waiting_on_people": waiting, "queued": queued, "bound_left_s": 0}


@contextlib.contextmanager
def _routes(*answers):
    """A stand-in for the server on a free loopback port. Each request takes
    the next of ``answers`` (the last one again once they run out): a work
    report, a bare status, ``(302, location)``, or None for a server that
    accepts the connection and says nothing. Yields the port and the
    requests seen, each ``(method, path, authorization, body)``."""
    left, seen, done = list(answers), [], threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append((self.command, self.path, self.headers.get("Authorization"), body))
            answer = left.pop(0) if len(left) > 1 else left[0]
            if answer is None:
                done.wait(20)
                return
            data = json.dumps(answer).encode() if isinstance(answer, dict) else b""
            status, location = answer if isinstance(answer, tuple) else (answer, "")
            self.send_response(200 if isinstance(answer, dict) else status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], seen
    finally:
        done.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@pytest.fixture
def running(monkeypatch):
    """The command line pointed at a stand-in server: ``running(*answers)``
    starts it and returns the requests it sees. No pause between two reads.
    Until then the port is one nobody listens on and the key is the test's
    own, so no request of a test reaches a server that runs on this host."""
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "PORT", _free_port())
    monkeypatch.setattr(config, "API_KEY", KEY)
    monkeypatch.setattr(config, "DRAIN_BOUND_S", 600)
    monkeypatch.setattr(admin_cli, "POLL_S", 0.01)
    for name in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    with contextlib.ExitStack() as stack:
        def start(*answers):
            port, seen = stack.enter_context(_routes(*answers))
            monkeypatch.setattr(config, "PORT", port)
            return seen
        yield start


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _drain(**kwargs) -> tuple[int, list[str]]:
    out = io.StringIO()
    return admin_cli.drain(out=out, **kwargs), out.getvalue().splitlines()


def test_a_drain_says_what_it_waits_for_until_the_server_holds(running):
    busy = _work("draining", turns=2, runs=1)
    seen = running(busy, busy, _work("draining", turns=1), _work("draining", turns=1),
                   _work("held", drained=True))
    rc, lines = _drain()
    assert rc == admin_cli.DONE
    assert lines == ["Waiting for open work: 2 turns, 1 task run. Up to 10 minutes.",
                     "Still working: 1 turn.", HELD_IDLE]
    assert seen[0] == ("POST", "/v1/internal/drain", f"Bearer {KEY}",
                       {"action": "drain", "bound_s": 600, "away_s": 900})
    assert [r[:3] for r in seen[1:]] == [("GET", "/v1/internal/work", f"Bearer {KEY}")] * 4


def test_a_drain_of_an_idle_server_says_nothing_is_working(running):
    running(_work("draining"), _work("held", drained=True))
    assert _drain(bound=90, away=60) == (admin_cli.DONE, ["Nothing is working.", HELD_IDLE])


@pytest.mark.parametrize("held, last", [
    (_work("held", turns=1, waiting=1),
     "The wait ran out with 1 turn still working. It is cut by the restart and continues after "
     "it. 1 turn waits for a person. It is asked again after the restart."),
    (_work("held", turns=2, runs=1, waiting=2),
     "The wait ran out with 2 turns, 1 task run still working. They are cut by the restart and "
     "continue after it. 2 turns wait for a person. They are asked again after the restart."),
    (_work("held", drained=True, waiting=1),
     HELD_IDLE + " 1 turn waits for a person. It is asked again after the restart."),
    ({**_work("held"), "open": {}},
     "The wait ran out with work still open. It is cut by the restart and continues after it."),
])
def test_a_wait_that_runs_out_says_what_the_restart_cuts(running, held, last):
    seen = running(_work("draining", turns=2, runs=1), held)
    rc, lines = _drain(bound=90, away=60)
    assert rc == admin_cli.DONE
    assert lines == ["Waiting for open work: 2 turns, 1 task run. Up to 90 seconds.", last]
    assert seen[0][3] == {"action": "drain", "bound_s": 90, "away_s": 60}


def test_a_quiet_drain_prints_the_last_line_only(running):
    running(_work("draining", turns=2), _work("draining", turns=1), _work("held", drained=True))
    assert _drain(quiet=True) == (admin_cli.DONE, [HELD_IDLE])


@pytest.mark.parametrize("held, last", [
    (_work("held", drained=True), HELD_IDLE),
    (_work("held", turns=1),
     "New work is held with 1 turn still working. It is cut by the restart and continues after it."),
])
def test_a_bound_of_zero_returns_as_soon_as_the_server_holds(running, held, last):
    seen = running(held)
    assert _drain(bound=0) == (admin_cli.DONE, [last])
    assert [(r[0], r[3]) for r in seen] == [("POST", {"action": "drain", "bound_s": 0, "away_s": 900})]


def test_a_drain_with_no_wait_names_what_is_still_working(running):
    running(_work("draining", turns=1), _work("held", turns=1))
    rc, lines = _drain(bound=0)
    assert rc == admin_cli.DONE and lines[0] == "Still working: 1 turn."


@pytest.mark.parametrize("answer, code, words", [
    ("nobody listens", admin_cli.NOT_RUNNING, admin_cli.NOT_RUNNING_LINE),
    (404, admin_cli.OLDER, admin_cli.OLDER_LINE),
    (403, admin_cli.REFUSED, admin_cli.FORBIDDEN_LINE),
    (401, admin_cli.REFUSED, admin_cli.FORBIDDEN_LINE),
    ({"detail": "Not Found"}, admin_cli.REFUSED, admin_cli.UNREADABLE_LINE),
])
def test_a_server_that_cannot_answer_is_told_apart_at_once(running, monkeypatch, capsys, answer, code, words):
    """A refused connection is a server that is not running, a 404 one older
    than 2.0, a 401 or a 403 a key or an address the server refuses (a shell
    whose own ``PROXY_API_KEY`` is not the server's): each is said at
    once, with its own exit status, and asked only once."""
    monkeypatch.setattr(admin_cli, "ANSWER_S", 0.3)
    seen = [] if answer == "nobody listens" else running(answer)
    for verb in (admin_cli.drain, admin_cli.resume, admin_cli.status):
        out = io.StringIO()
        with pytest.raises(admin_cli.Unanswered) as refused:
            verb(out=out)
        assert (refused.value.code, str(refused.value)) == (code, words) and out.getvalue() == ""
    assert len(seen) == (0 if answer == "nobody listens" else 3)
    from storage import pg
    monkeypatch.setattr(pg, "close_pool", lambda timeout=0: None)
    assert admin_cli.main(["drain", "--quiet"]) == code
    assert capsys.readouterr().err == f"admin_cli: {words}\n"


@pytest.mark.parametrize("answer", [None, 500, 503])
def test_a_silent_server_is_asked_again_and_the_wait_is_said(running, monkeypatch, answer):
    """A server that says nothing, or fails with a 5xx, may be held by a busy
    disk right after an image build: it is asked again for about a minute
    (here less), and the sentence says how long it waited."""
    monkeypatch.setattr(admin_cli, "ANSWER_S", 0.2)
    monkeypatch.setattr(admin_cli, "ASK_FOR_S", 1.0)
    monkeypatch.setattr(admin_cli, "POLL_S", 0.05)
    seen = running(answer)
    with pytest.raises(admin_cli.Unanswered) as refused:
        admin_cli.drain(out=io.StringIO())
    assert refused.value.code == admin_cli.REFUSED
    assert str(refused.value).startswith("The server did not answer the drain in ")
    assert str(refused.value).endswith(" seconds.")
    assert len(seen) >= 2


def test_a_server_that_answers_on_its_second_ask_drains(running, monkeypatch):
    monkeypatch.setattr(admin_cli, "ANSWER_S", 0.2)
    seen = running(503, _work("held", drained=True))
    assert _drain() == (admin_cli.DONE, [HELD_IDLE])
    assert [r[0] for r in seen] == ["POST", "POST"]


def test_the_probe_and_the_compute_wait_ask_once_as_before(running, monkeypatch):
    monkeypatch.setattr(admin_cli, "ANSWER_S", 0.2)
    monkeypatch.setattr(admin_cli, "ASK_FOR_S", 30.0)
    seen = running(503)
    assert admin_cli.drain(probe=True) == admin_cli.REFUSED
    assert len(seen) == 1


def test_a_server_that_stops_answering_mid_drain_is_read_again_until_the_drains_end(running, monkeypatch):
    """A read the server misses is read again: the drain gives up only at its
    own end, a minute past its bound (here less)."""
    monkeypatch.setattr(admin_cli, "PAST_BOUND_S", 0.3)
    seen = running(_work("draining", turns=1), 500)
    out = io.StringIO()
    with pytest.raises(admin_cli.Refused, match="did not answer the drain"):
        admin_cli.drain(bound=0, out=out)
    assert out.getvalue().splitlines() == ["Still working: 1 turn."]
    assert len(seen) >= 3


def test_a_drain_gives_up_a_minute_past_its_bound(running, monkeypatch):
    monkeypatch.setattr(admin_cli, "PAST_BOUND_S", 0.05)
    seen = running(_work("draining", turns=1))
    with pytest.raises(admin_cli.Refused, match="did not finish the drain in time"):
        admin_cli.drain(bound=0, out=io.StringIO())
    assert len(seen) >= 2  # it read the work again; a slow runner has time for one read


def test_a_drain_called_off_from_elsewhere_ends(running):
    running(_work("draining", turns=1), _work("running", turns=1))
    with pytest.raises(admin_cli.Refused, match="called off"):
        admin_cli.drain(out=io.StringIO())


@pytest.mark.parametrize("answer, rc", [(_work("running", turns=3), admin_cli.DONE),
                                        (403, admin_cli.REFUSED), (404, admin_cli.REFUSED)])
def test_a_probe_asks_once_and_prints_nothing(running, answer, rc):
    seen = running(answer)
    assert _drain(probe=True) == (rc, [])
    assert [r[:2] for r in seen] == [("GET", "/v1/internal/work")]


def test_a_probe_of_a_server_that_is_not_running_is_refused(running):
    assert _drain(probe=True) == (admin_cli.REFUSED, [])


def test_resume_calls_the_drain_off(running):
    seen = running(_work("running"))
    out = io.StringIO()
    assert admin_cli.resume(out=out) == admin_cli.DONE
    assert out.getvalue() == "The platform takes new work again.\n"
    assert seen == [("POST", "/v1/internal/drain", f"Bearer {KEY}", {"action": "resume"})]


@pytest.mark.parametrize("work, line", [
    (_work(), "Idle. A good moment to update."),
    (_work(turns=2, runs=1), "Busy: 2 turns, 1 task run."),
    (_work(turns=1, runs=2, calls=1, duplex=1, meetings=1, terminals=1, moves=1, background=1),
     "Busy: 1 turn, 2 task runs, 1 call, 1 voice conversation, 1 meeting, 1 terminal, "
     "1 chat move, 1 background job."),
    (_work(calls=2, duplex=2, meetings=2, terminals=2, moves=2, background=2),
     "Busy: 2 calls, 2 voice conversations, 2 meetings, 2 terminals, 2 chat moves, "
     "2 background jobs."),
    (_work(turns=2, waiting=1, queued=3),
     "Busy: 2 turns. 1 turn waits for a person. 3 messages wait to be sent."),
    (_work(waiting=2, queued=1),
     "Idle. A good moment to update. 2 turns wait for a person. 1 message waits to be sent."),
    (_work("held", turns=1), "Busy: 1 turn. The platform is holding new work for an update."),
    (_work("draining"),
     "Idle. A good moment to update. The platform is holding new work for an update."),
    ({**_work(), "starting": True},
     "Idle. The platform is starting. New work waits until its compute machine has signed in and its tools "
     "are ready."),
    ({**_work(turns=1), "starting": True},
     "Busy: 1 turn. The platform is starting. New work waits until its compute machine has signed in and its "
     "tools are ready."),
])
def test_status_says_busy_or_idle_in_one_line(running, work, line):
    seen = running(work)
    out = io.StringIO()
    assert admin_cli.status(out=out) == admin_cli.DONE
    assert out.getvalue() == line + "\n"
    assert seen == [("GET", "/v1/internal/work", f"Bearer {KEY}", None)]


@pytest.mark.parametrize("host, url", [
    ("0.0.0.0", "http://127.0.0.1:8400"), ("", "http://127.0.0.1:8400"),
    ("::", "http://127.0.0.1:8400"), ("10.0.0.5", "http://10.0.0.5:8400"),
    ("fd00::5", "http://[fd00::5]:8400"),
])
def test_the_server_is_asked_on_loopback_or_the_one_address_it_binds(monkeypatch, host, url):
    monkeypatch.setattr(config, "HOST", host)
    monkeypatch.setattr(config, "PORT", 8400)
    assert admin_cli.server_url() == url


def test_the_master_key_follows_no_redirect_and_no_proxy_of_the_environment(running, monkeypatch):
    with _routes(_work()) as (elsewhere, seen_elsewhere):
        running((302, f"http://127.0.0.1:{elsewhere}/v1/internal/work"))
        assert admin_cli.ask_server("GET", admin_cli.WORK_PATH) is None
        assert seen_elsewhere == []
    seen = running(_work())
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{_free_port()}")
    assert admin_cli.ask_server("GET", admin_cli.WORK_PATH) == _work()
    assert len(seen) == 1


def test_the_server_verbs_open_no_database_and_take_the_bound_of_the_configuration(
        running, monkeypatch):
    from storage import pg
    monkeypatch.setattr(pg, "close_pool", lambda timeout=0: None)

    def no_database():
        raise AssertionError("a verb that asks the server read the database")
    monkeypatch.setattr(admin_cli, "_warn_on_another_release", no_database)
    monkeypatch.setattr(config, "DRAIN_BOUND_S", 120)
    seen = running(_work("draining", turns=1), _work("held", drained=True), _work("running"),
                   _work("running"), _work("held", drained=True))
    assert admin_cli.main(["drain"]) == admin_cli.DONE
    assert admin_cli.main(["resume"]) == admin_cli.DONE
    assert admin_cli.main(["status"]) == admin_cli.DONE
    assert [(r[0], r[3]) for r in seen] == [
        ("POST", {"action": "drain", "bound_s": 120, "away_s": 900}), ("GET", None),
        ("POST", {"action": "resume"}), ("GET", None)]
    assert admin_cli.main(["drain", "--bound", "30", "--away", "45", "--quiet"]) == admin_cli.DONE
    assert seen[-1][3] == {"action": "drain", "bound_s": 30, "away_s": 45}
    assert admin_cli.main(["drain", "--bound", "-1"]) == admin_cli.USAGE
    assert admin_cli.main(["drain", "--bound", "soon"]) == admin_cli.USAGE


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


def test_the_launcher_names_no_host_command_this_suite_has_no_fake_for(tmp_path):
    """The launcher runs with the host's PATH behind the fake docker: a
    command that acts on the host needs a fake here before the script may
    name it."""
    write_fake(tmp_path / "bin")
    fakes = {p.name for p in (tmp_path / "bin").iterdir()}
    assert fakes == {"docker"}
    assert host_commands_named(REPO / "scripts" / "otodock-admin.sh") <= fakes


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


_ON = {"own": True, "connected": True, "shared": True, "machines": ["compute-1"], "open": 0, "sessions": 0}
_AWAY = {**_ON, "connected": False, "shared": False, "machines": []}
# The install's own compute service is away while one on another server is signed in.
_OWN_AWAY = {**_ON, "shared": False, "machines": ["compute-rack"]}
_NOT_SIGNED_IN = "The compute machine is not signed in. Chats, tasks and calls cannot run until it is."
_NONE_OF_ITS_OWN = ("No compute machine is signed in, and this install runs none of its own. "
                    "Chats, tasks and calls cannot run until one is set up.")


@pytest.mark.parametrize("compute, line", [
    (_ON, "Idle. A good moment to update. The compute machine is signed in."),
    (_AWAY, "Idle. A good moment to update. " + _NOT_SIGNED_IN),
    # What readiness asks for too: the machine that shares the platform's folders.
    (_OWN_AWAY, "Idle. A good moment to update. " + _NOT_SIGNED_IN),
    # A server that names no shared machine yet: any compute machine counts, as before.
    ({k: v for k, v in _ON.items() if k != "shared"}, "Idle. A good moment to update. The compute machine is signed in."),
    # An install with none of its own is served by the ones on other servers.
    ({**_ON, "own": False, "shared": False}, "Idle. A good moment to update. The compute machine is signed in."),
    # A source install without the compute unit: nothing runs until one is set up.
    ({**_AWAY, "own": False},
     "Idle. A good moment to update. " + _NONE_OF_ITS_OWN),
    # A server older than 2.0 names no compute machine.
    (None, "Idle. A good moment to update."),
])
def test_status_names_the_compute_machine(running, compute, line):
    running({**_work(), **({"compute": compute} if compute is not None else {})})
    out = io.StringIO()
    assert admin_cli.status(out=out) == admin_cli.DONE
    assert out.getvalue() == line + "\n"


def test_compute_waits_for_the_compute_machine_to_sign_in(running):
    away = {**_work(), "compute": _AWAY}
    seen = running(away, away, {**_work(), "compute": _ON})
    out = io.StringIO()
    assert admin_cli.compute(wait=5, out=out) == admin_cli.DONE
    assert out.getvalue() == "The compute machine is signed in.\n" and len(seen) == 3


def test_compute_waits_for_the_installs_own_service_not_for_any_machine(running):
    """A backup stops this server's compute service and starts it again. A
    compute machine on another server never stopped: the wait is for the
    one that shares the platform's folders, as readiness is."""
    seen = running({**_work(), "compute": _OWN_AWAY}, {**_work(), "compute": _OWN_AWAY}, {**_work(), "compute": _ON})
    out = io.StringIO()
    assert admin_cli.compute(wait=5, out=out) == admin_cli.DONE
    assert out.getvalue() == "The compute machine is signed in.\n" and len(seen) == 3
    running({**_work(), "compute": _OWN_AWAY})
    out = io.StringIO()
    assert admin_cli.compute(wait=0, out=out) == admin_cli.REFUSED
    assert out.getvalue() == _NOT_SIGNED_IN + "\n"


def test_compute_waits_for_the_start_to_take_work_after_the_sign_in(running):
    """Signed in, the start's hold may still run a few seconds (the machine's
    sessions adopted, what was held sent): a chat sent then is refused, so the
    wait (install.sh, upgrade.sh) ends only once the platform takes work."""
    held = {**_work(), "starting": True, "compute": _ON}
    seen = running(held, held, {**_work(), "compute": _ON})
    out = io.StringIO()
    assert admin_cli.compute(wait=5, out=out) == admin_cli.DONE
    assert out.getvalue() == "The compute machine is signed in.\n" and len(seen) == 3


def test_compute_at_its_deadline_while_the_platform_still_starts_blames_no_one(running):
    held = {**_work(), "starting": True, "compute": _ON}
    running(held)
    out = io.StringIO()
    assert admin_cli.compute(wait=0, out=out) == admin_cli.DONE
    assert out.getvalue() == "The compute machine is signed in. The platform is still starting.\n"


def test_compute_on_an_install_with_no_compute_machine_of_its_own_says_nothing_can_run(running):
    running({**_work(), "compute": {**_AWAY, "own": False}})
    out = io.StringIO()
    assert admin_cli.compute(wait=0, out=out) == admin_cli.REFUSED
    assert out.getvalue() == _NONE_OF_ITS_OWN + "\n"


def test_compute_past_its_wait_says_nothing_can_run_and_exits_1(running):
    running({**_work(), "compute": _AWAY})
    out = io.StringIO()
    assert admin_cli.compute(wait=0, out=out) == admin_cli.REFUSED
    assert out.getvalue() == _NOT_SIGNED_IN + "\n"
    assert "platform itself" not in out.getvalue()


def test_compute_on_a_server_older_than_two_answers_at_once(running):
    running(_work())
    out = io.StringIO()
    assert admin_cli.compute(wait=60, out=out) == admin_cli.DONE
    assert out.getvalue() == "This server is older than 2.0: it runs no compute machine.\n"
