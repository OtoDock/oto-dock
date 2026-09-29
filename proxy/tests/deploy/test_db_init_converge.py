"""Drive the real `otodock-db-init` one-shot against a throwaway Postgres.

The script is taken from `docker-compose.yml` (the `otodock-db-init` command)
and run exactly as compose runs it: in a container of the pinned Postgres
image, the socket volume mounted read-only at /var/run/postgresql, on a
private internal bridge where the server answers as `otodock-postgres`. This
proves the role design end to end: the app role `otodock` is a
NOSUPERUSER owner that follows POSTGRES_PASSWORD, the superuser `otodock_admin`
keeps no network password unless OTODOCK_DB_ADMIN_PASSWORD opts in, and every
heal path works over the socket alone.

Skipped without a usable docker daemon or the pinned image (never pulled here).
Each test owns one Postgres and chains its cases: under xdist the functions of
a module land on different workers, so a case cannot depend on a sibling.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid

import pytest
import yaml

from tests._paths import REPO_ROOT

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

_COMPOSE = REPO_ROOT / "docker-compose.yml"
_SOCK = "/var/run/postgresql"
_APP_PW = "app-pw-" + uuid.uuid4().hex[:12]
_SUPER_Q = "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: int = 90):
    return subprocess.run(
        ["docker", *args], input=input, text=True, capture_output=True,
        check=check, timeout=timeout,
    )


def _image() -> str:
    m = re.search(r"\$\{POSTGRES_IMAGE:-([^}]+)\}", _COMPOSE.read_text())
    assert m, "docker-compose.yml pins POSTGRES_IMAGE"
    return m.group(1)


def _script() -> str:
    compose = yaml.safe_load(_COMPOSE.read_text())
    cmd = compose["services"]["otodock-db-init"]["command"]
    assert cmd[:2] == ["sh", "-c"]
    raw = cmd[2]
    assert "$" not in raw.replace("$$", "")
    return raw.replace("$$", "$")


@pytest.fixture(scope="module")
def image() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    try:
        _docker("info", timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("the docker daemon is not reachable")
    img = _image()
    if _docker("image", "inspect", img, check=False).returncode != 0:
        pytest.skip(f"{img} is not present locally (docker pull {img})")
    return img


class _Stack:
    """One throwaway Postgres on its own internal bridge with a shared socket volume."""

    def __init__(self, image: str, *, bootstrap_user: str, script: str):
        uid = uuid.uuid4().hex[:8]
        self.image = image
        self.script = script
        self.net = f"b2t-{uid}-net"
        self.sock = f"b2t-{uid}-sock"
        self.pg = f"b2t-{uid}-pg"
        self._up: list[str] = []
        try:
            _docker("network", "create", "--internal", self.net)
            self._up.append("net")
            _docker("volume", "create", self.sock)
            self._up.append("sock")
            _docker(
                "run", "-d", "--name", self.pg, "--network", self.net,
                "--network-alias", "otodock-postgres",
                "-v", f"{self.sock}:{_SOCK}",
                "-e", f"POSTGRES_USER={bootstrap_user}",
                "-e", f"POSTGRES_PASSWORD={_APP_PW}",
                "-e", "POSTGRES_DB=otodock",
                image,
            )
            self._up.append("pg")
            self.wait_tcp()
        except Exception:
            self.close()
            raise

    def wait_tcp(self, timeout: float = 60) -> None:
        # The entrypoint's initdb server is socket-only, so the socket answers
        # before the real server does: wait for TCP inside the container.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = _docker("inspect", "-f", "{{.State.Running}}", self.pg, check=False).stdout.strip()
            assert state == "true", _docker("logs", self.pg, check=False).stdout[-2000:]
            r = _docker("exec", self.pg, "pg_isready", "-q", "-h", "127.0.0.1", "-p", "5432", check=False)
            if r.returncode == 0:
                return
            time.sleep(0.5)
        raise AssertionError("postgres never accepted TCP")

    def sql(self, sql: str, *, user: str = "otodock_admin") -> str:
        r = _docker("exec", "-i", self.pg, "psql", "-X", "-tAq", "-v", "ON_ERROR_STOP=1",
                    "-U", user, "-d", "otodock", input=sql)
        return r.stdout.strip()

    def tcp_login(self, user: str, password: str) -> subprocess.CompletedProcess:
        return _docker(
            "run", "--rm", "--network", self.net, "-e", f"PGPASSWORD={password}", self.image,
            "psql", "-X", "-tAq", "-h", "otodock-postgres", "-U", user, "-d", "otodock",
            "-c", _SUPER_Q, check=False,
        )

    def run_init(self, *, admin_pw: str = "", app_pw: str = _APP_PW) -> subprocess.CompletedProcess:
        return _docker(
            "run", "--rm", "--network", self.net,
            "-v", f"{self.sock}:{_SOCK}:ro",
            "-e", "POSTGRES_DB=otodock",
            "-e", f"OTODOCK_DB_APP_PASSWORD={app_pw}",
            "-e", f"OTODOCK_DB_ADMIN_PASSWORD={admin_pw}",
            "-e", "OTODOCK_DB_INIT_WAIT_S=90",
            self.image, "sh", "-c", self.script, check=False, timeout=150,
        )

    def kill_and_start(self) -> None:
        _docker("kill", self.pg)
        _docker("start", self.pg)
        self.wait_tcp()

    def close(self) -> None:
        if "pg" in self._up:
            _docker("rm", "-f", "-v", self.pg, check=False)
        if "net" in self._up:
            _docker("network", "rm", self.net, check=False)
        if "sock" in self._up:
            _docker("volume", "rm", self.sock, check=False)
        self._up.clear()


def _ok(r: subprocess.CompletedProcess) -> str:
    assert r.returncode == 0, f"exit {r.returncode}\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}"
    return r.stdout


def _failed_auth(r: subprocess.CompletedProcess) -> None:
    assert r.returncode != 0 and "password authentication failed" in r.stderr, (r.stdout, r.stderr)


def _assert_converged(st: _Stack, *, admin_null: bool) -> None:
    assert st.sql("SELECT rolsuper FROM pg_roles WHERE rolname = 'otodock'") == "f"
    assert st.sql("SELECT rolsuper FROM pg_roles WHERE rolname = 'otodock_admin'") == "t"
    assert st.sql("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname = 'otodock_admin'") == ("t" if admin_null else "f")
    assert st.sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'otodock'") == "otodock"
    assert st.sql("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner <> 'otodock'") == "0"
    assert st.sql("SELECT count(*) FROM pg_sequences WHERE schemaname = 'public' AND sequenceowner <> 'otodock'") == "0"
    assert st.sql("SELECT count(*) FROM pg_views WHERE schemaname = 'public' AND viewowner <> 'otodock'") == "0"
    assert st.sql("SELECT count(*) FROM pg_roles WHERE rolname = 'otodock_db_init_tmp'") == "0"
    # The app role must be able to use and create in public, or the proxy's first
    # migration fails with "no schema has been selected to create in". A stock
    # cluster's public belongs to pg_database_owner and follows the database
    # owner; a recreated one must have been given to the app role.
    assert st.sql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'") in ("pg_database_owner", "otodock")
    assert st.sql("SELECT has_schema_privilege('otodock', 'public', 'USAGE') AND has_schema_privilege('otodock', 'public', 'CREATE')") == "t"
    st.sql("CREATE TABLE otodock_probe (id int); DROP TABLE otodock_probe;", user="otodock")
    # The proxy's own path: TCP, SCRAM, the app password, not a superuser.
    assert _ok(st.tcp_login("otodock", _APP_PW)).strip() == "f"
    # The superuser over the socket needs no password whatever its state.
    assert st.sql("SELECT 1", user="otodock_admin") == "1"


def test_fresh_volume_then_optin_toggle_and_rotation(image):
    """A: fresh volume (admin mode), re-run, opt-in on, opt-in off, rotated app password."""
    st = _Stack(image, bootstrap_user="otodock_admin", script=_script())
    try:
        out = _ok(st.run_init())
        assert "mode=admin" in out and "created role otodock" in out
        assert "no network password on otodock_admin" in out
        assert "otodock_admin rolsuper=t rolpassword_null=t" in out
        _assert_converged(st, admin_null=True)
        _failed_auth(st.tcp_login("otodock_admin", _APP_PW))

        out = _ok(st.run_init())  # re-run: a no-op that still enforces the null
        assert "created role" not in out and "app password set" not in out
        _assert_converged(st, admin_null=True)

        key = "k'e;y-" + uuid.uuid4().hex[:8]
        out = _ok(st.run_init(admin_pw=key))
        assert "network password set from OTODOCK_DB_ADMIN_PASSWORD" in out
        _assert_converged(st, admin_null=False)
        assert _ok(st.tcp_login("otodock_admin", key)).strip() == "t"
        _failed_auth(st.tcp_login("otodock_admin", _APP_PW))

        r = st.run_init(admin_pw=_APP_PW)  # equal to the app password: applied, but warned
        _ok(r)
        assert "WARNING: OTODOCK_DB_ADMIN_PASSWORD equals POSTGRES_PASSWORD" in r.stderr, r.stderr
        assert _ok(st.tcp_login("otodock_admin", _APP_PW)).strip() == "t"

        out = _ok(st.run_init())  # unset again
        assert "no network password on otodock_admin" in out
        _assert_converged(st, admin_null=True)
        _failed_auth(st.tcp_login("otodock_admin", key))
        _failed_auth(st.tcp_login("otodock_admin", _APP_PW))

        new_pw = "rotated-" + uuid.uuid4().hex[:8]
        out = _ok(st.run_init(app_pw=new_pw))
        assert "app password set from POSTGRES_PASSWORD" in out
        assert _ok(st.tcp_login("otodock", new_pw)).strip() == "f"
        _failed_auth(st.tcp_login("otodock", _APP_PW))
        assert st.sql("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname = 'otodock_admin'") == "t"
    finally:
        st.close()


def test_pre_17_volume_converges_and_survives_a_stale_socket(image):
    """B: a 1.6.1-shaped volume (bootstrap superuser `otodock` owns everything)."""
    st = _Stack(image, bootstrap_user="otodock", script=_script())
    try:
        st.sql(
            "CREATE TABLE t (id serial PRIMARY KEY, v text); CREATE SEQUENCE s;"
            " INSERT INTO t (v) VALUES ('a');",
            user="otodock",
        )
        out = _ok(st.run_init())
        assert "mode=existing" in out and "renamed the bootstrap superuser" in out
        _assert_converged(st, admin_null=True)
        # A stock public schema is left with pg_database_owner: nothing to move.
        assert st.sql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'") == "pg_database_owner"
        assert st.sql("SELECT tableowner FROM pg_tables WHERE tablename = 't'") == "otodock"
        assert st.sql("SELECT sequenceowner FROM pg_sequences WHERE sequencename = 't_id_seq'") == "otodock"
        assert st.sql("SELECT sequenceowner FROM pg_sequences WHERE sequencename = 's'") == "otodock"
        assert st.sql("SELECT v FROM t", user="otodock") == "a"
        assert st.sql("SELECT oid FROM pg_roles WHERE rolname = 'otodock_admin'") == "10"

        out = _ok(st.run_init())
        assert "mode=admin" in out and "created role" not in out
        _assert_converged(st, admin_null=True)

        # A killed server leaves its socket and lock in the shared volume.
        st.kill_and_start()
        assert st.sql("SELECT 1") == "1"
        out = _ok(st.run_init())
        assert "mode=admin" in out
        _assert_converged(st, admin_null=True)
    finally:
        st.close()


def test_crash_after_the_rename_heals_over_the_socket(image):
    """C: the rename happened, then nothing: no `otodock`, tables owned by the admin."""
    st = _Stack(image, bootstrap_user="otodock", script=_script())
    try:
        st.sql("CREATE TABLE t (id serial PRIMARY KEY, v text);", user="otodock")
        st.sql("CREATE ROLE otodock_db_init_tmp LOGIN SUPERUSER;", user="otodock")
        st.sql("ALTER ROLE otodock RENAME TO otodock_admin;", user="otodock_db_init_tmp")
        # The crash state: the renamed role still answers over TCP to the app password.
        assert _ok(st.tcp_login("otodock_admin", _APP_PW)).strip() == "t"
        assert st.sql("SELECT count(*) FROM pg_roles WHERE rolname = 'otodock'") == "0"

        out = _ok(st.run_init())
        assert "mode=admin" in out and "created role otodock" in out
        _assert_converged(st, admin_null=True)
        assert st.sql("SELECT tableowner FROM pg_tables WHERE tablename = 't'") == "otodock"
        _failed_auth(st.tcp_login("otodock_admin", _APP_PW))
    finally:
        st.close()


def test_tampered_clusters_are_refused_then_converge_once_fixed(image):
    """D: a foreign otodock_admin next to the bootstrap otodock, non-super then super."""
    st = _Stack(image, bootstrap_user="otodock", script=_script())
    try:
        st.sql("CREATE ROLE otodock_admin LOGIN NOSUPERUSER;", user="otodock")
        r = st.run_init()
        assert r.returncode == 1 and "otodock_admin exists but is not a superuser" in r.stderr, r.stderr

        st.sql("ALTER ROLE otodock_admin SUPERUSER;", user="otodock")
        r = st.run_init()
        assert r.returncode == 1 and "otodock is still the bootstrap superuser" in r.stderr, r.stderr
        assert st.sql("SELECT rolsuper FROM pg_roles WHERE rolname = 'otodock'", user="otodock") == "t"

        st.sql("DROP ROLE otodock_admin;", user="otodock")
        out = _ok(st.run_init())
        assert "mode=existing" in out
        _assert_converged(st, admin_null=True)
    finally:
        st.close()


def test_no_superuser_reachable(image):
    """E: `otodock` already demoted by hand, no otodock_admin at all (bootstrap `postgres`)."""
    st = _Stack(image, bootstrap_user="postgres", script=_script())
    try:
        st.sql(
            "CREATE ROLE otodock LOGIN NOSUPERUSER PASSWORD 'wrong';"
            " ALTER DATABASE otodock OWNER TO otodock; CREATE TABLE stranded (id int);",
            user="postgres",
        )
        r = st.run_init()
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "no superuser is reachable on the socket" in r.stderr and "postgres" in r.stderr, r.stderr

        st.sql("ALTER TABLE stranded OWNER TO otodock;", user="postgres")
        out = _ok(st.run_init())
        assert "mode=appnonsuper" in out
        assert "no superuser role otodock_admin exists" in out
        # The app proof still ran and repaired the app password as `otodock` itself.
        assert "app password set from POSTGRES_PASSWORD" in out
        assert _ok(st.tcp_login("otodock", _APP_PW)).strip() == "f"
        assert "rolpassword_null" not in out  # no superuser-only summary
    finally:
        st.close()


def test_a_recreated_public_schema_is_given_to_the_app_role(image):
    """F: a 1.6.1-shaped volume whose public schema was recreated by hand (a
    restore that dropped and created it), so the bootstrap superuser owns it and
    it carries no USAGE grant. Moving the tables alone leaves the renamed
    superuser holding the schema and the app role unable to create in it."""
    st = _Stack(image, bootstrap_user="otodock", script=_script())
    try:
        st.sql(
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
            " CREATE TABLE t (id serial PRIMARY KEY, v text); INSERT INTO t (v) VALUES ('a');"
            " CREATE VIEW tv AS SELECT v FROM t;",
            user="otodock",
        )
        assert st.sql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'", user="otodock") == "otodock"
        assert st.sql("SELECT nspacl IS NULL FROM pg_namespace WHERE nspname = 'public'", user="otodock") == "t"

        out = _ok(st.run_init())
        assert "mode=existing" in out
        _assert_converged(st, admin_null=True)
        assert st.sql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'") == "otodock"
        assert st.sql("SELECT viewowner FROM pg_views WHERE viewname = 'tv'") == "otodock"
        assert st.sql("SELECT v FROM t", user="otodock") == "a"

        # A second run finds nothing to move and still proves the schema.
        out = _ok(st.run_init())
        assert "mode=admin" in out
        _assert_converged(st, admin_null=True)
    finally:
        st.close()


def test_an_unusable_public_schema_stops_db_init_with_the_fix(image):
    """G: no superuser is reachable (appnonsuper) and public belongs to another
    role with no grant: the app role owns every table, so the ownership check
    passes, yet it cannot create. db-init stops with the one-line fix instead
    of letting the proxy crash-loop."""
    st = _Stack(image, bootstrap_user="postgres", script=_script())
    try:
        st.sql(
            "CREATE ROLE otodock LOGIN NOSUPERUSER PASSWORD 'wrong';"
            " ALTER DATABASE otodock OWNER TO otodock;"
            " DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
            user="postgres",
        )
        assert st.sql("SELECT has_schema_privilege('otodock', 'public', 'USAGE')", user="postgres") == "f"

        r = st.run_init()
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "cannot use or create in schema public" in r.stderr, r.stderr
        assert "ALTER SCHEMA public OWNER TO otodock" in r.stderr, r.stderr

        st.sql("ALTER SCHEMA public OWNER TO otodock;", user="postgres")
        out = _ok(st.run_init())
        assert "mode=appnonsuper" in out
        assert "can use and create in schema public" in out
    finally:
        st.close()
