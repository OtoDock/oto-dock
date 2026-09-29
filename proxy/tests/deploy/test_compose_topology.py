"""Static invariants over the shipped compose files (no stack spin-up).

These encode the deployment-isolation invariants:
the Postgres and docker-socket-proxy control planes are off the shared
`otodock` network; file-tools and Collabora are on an internal-only network;
the app DB role is demoted via a one-shot init service; the proxy port and the
phone ports honour their bind knobs; and the proxy carries fd/pids floors.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]


def _load(name: str) -> dict:
    return yaml.safe_load((_REPO / name).read_text())


def _svc(compose: dict, name: str) -> dict:
    return compose["services"][name]


def _nets(svc: dict) -> set[str]:
    """The set of network names a service joins (short-list or mapping form)."""
    n = svc.get("networks")
    if n is None:
        return set()
    if isinstance(n, list):
        return set(n)
    if isinstance(n, dict):
        return set(n.keys())
    raise AssertionError(f"unexpected networks shape: {n!r}")


@pytest.fixture(scope="module")
def base() -> dict:
    return _load("docker-compose.yml")


@pytest.fixture(scope="module")
def phone() -> dict:
    return _load("docker-compose.phone.yml")


# --- the network split -----------------------------------------

def test_new_networks_are_internal_with_pinned_subnets(base):
    nets = base["networks"]
    for name, var in (
        ("socketproxy", "OTODOCK_SOCKETPROXY_SUBNET"),
        ("otodock-data", "OTODOCK_DATA_SUBNET"),
        ("otodock-internal", "OTODOCK_INTERNAL_SUBNET"),
    ):
        assert name in nets, f"{name} network missing"
        assert nets[name].get("internal") is True, f"{name} must be internal"
        subnet = nets[name]["ipam"]["config"][0]["subnet"]
        assert var in subnet, f"{name} subnet must be pinned via {var}: {subnet!r}"
        assert "10.20" in subnet, f"{name} subnet must be a 10.x pin: {subnet!r}"


def test_otodock_network_unchanged(base):
    # The shared bridge stays a normal (non-internal) named, subnet-pinned net:
    # community MCPs attach to it; changing it would force a recreate.
    net = base["networks"]["otodock"]
    assert net["name"] == "otodock"
    assert "internal" not in net
    assert "OTODOCK_NETWORK_SUBNET" in net["ipam"]["config"][0]["subnet"]


def test_socketproxy_isolated(base):
    sp = _nets(_svc(base, "docker-socket-proxy"))
    assert sp == {"socketproxy"}, f"socket-proxy must be socketproxy-only, got {sp}"


def test_postgres_isolated(base):
    pg = _nets(_svc(base, "otodock-postgres"))
    assert pg == {"otodock-data"}, f"postgres must be otodock-data-only, got {pg}"


def test_file_tools_and_collabora_internal(base):
    assert _nets(_svc(base, "file-tools")) == {"otodock-internal"}
    assert _nets(_svc(base, "collabora")) == {"otodock-internal"}


def test_proxy_bridges_all_planes(base):
    assert _nets(_svc(base, "otodock-proxy")) == {
        "otodock", "socketproxy", "otodock-data", "otodock-internal",
    }


def test_control_planes_share_no_net_with_untrusted_sidecars(base):
    control = _nets(_svc(base, "otodock-postgres")) | _nets(_svc(base, "docker-socket-proxy"))
    for sidecar in ("file-tools", "collabora"):
        assert not (control & _nets(_svc(base, sidecar))), (
            f"{sidecar} shares a network with a control plane"
        )
    # community Docker MCPs attach to `otodock`; the control planes must not.
    assert "otodock" not in control


def test_socket_proxy_keeps_needed_grants(base):
    env = _svc(base, "docker-socket-proxy")["environment"]
    for k in ("CONTAINERS", "IMAGES", "NETWORKS", "VOLUMES", "POST"):
        assert str(env[k]) == "1", f"socket-proxy must keep {k}=1"


# --- DB role demotion; the superuser without a network password ---

_SOCK = "/var/run/postgresql"


def _db_init_script(base) -> str:
    """The one-shot's POSIX sh exactly as the container runs it.

    Compose hands a literal `$` to the shell only when the source writes `$$`,
    so the block must hold no bare `$` (it would be interpolated at `up`).
    """
    cmd = _svc(base, "otodock-db-init")["command"]
    assert cmd[:2] == ["sh", "-c"]
    raw = cmd[2]
    assert "$" not in raw.replace("$$", ""), "a bare $ in db-init would be compose-interpolated"
    return raw.replace("$$", "$")


def _code_lines(script: str) -> list[str]:
    return [ln for ln in script.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_postgres_bootstrap_is_admin_role(base):
    pg = _svc(base, "otodock-postgres")
    env = pg["environment"]
    assert env["POSTGRES_USER"] == "otodock_admin"
    # The initdb password is the app password and transient: the one-shot's
    # last statement replaces it. Only db-init reads the opt-in key, so setting
    # or unsetting it never recreates the database container.
    assert env["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert "OTODOCK_DB_ADMIN_PASSWORD" not in env["POSTGRES_PASSWORD"]
    # A long WAL replay must not flip the container unhealthy before the
    # one-shot ever runs (success is still immediate).
    assert pg["healthcheck"].get("start_period")


def test_proxy_healthcheck_start_period_covers_a_first_boot():
    # A release's first boot builds the bundled MCP venvs and migrates the
    # database. Compose stops waiting for the proxy the moment Docker calls it
    # unhealthy, so the image's start period must cover what upgrade.sh waits.
    dockerfile = (_REPO / "proxy" / "Dockerfile").read_text()
    start = re.search(r"^HEALTHCHECK\s.*--start-period=(\d+)s", dockerfile, re.M)
    assert start, "proxy/Dockerfile has no HEALTHCHECK start period"
    wait = re.search(r"OTODOCK_UPGRADE_WAIT_S:-(\d+)", (_REPO / "scripts" / "upgrade.sh").read_text())
    assert wait, "upgrade.sh has no default health wait"
    assert int(start.group(1)) >= int(wait.group(1))


def test_socket_volume_declared(base):
    assert base["volumes"]["otodock-pgsock"] == {"name": "otodock-pgsock"}
    pg_vols = _svc(base, "otodock-postgres")["volumes"]
    assert "otodock-pgdata:/var/lib/postgresql/data" in pg_vols
    assert f"otodock-pgsock:{_SOCK}" in pg_vols
    assert f"otodock-pgsock:{_SOCK}:ro" in _svc(base, "otodock-db-init")["volumes"]


def test_db_init_service(base):
    init = _svc(base, "otodock-db-init")
    assert init["restart"] == "no"
    assert init["depends_on"]["otodock-postgres"]["condition"] == "service_healthy"
    assert _nets(init) == {"otodock-data"}
    env = init["environment"]
    assert env["OTODOCK_DB_APP_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    # Empty means "no network password": no fallback to the app password here.
    assert env["OTODOCK_DB_ADMIN_PASSWORD"] == "${OTODOCK_DB_ADMIN_PASSWORD:-}"


def test_db_init_uses_the_socket_and_no_admin_tcp_login(base):
    code = _code_lines(_db_init_script(base))
    text = "\n".join(code)
    assert f"SOCK={_SOCK}" in text
    assert 'mountpoint -q "$SOCK"' in text
    assert "pg_isready -q -h" in text, "the TCP gate before any socket work"
    psql = [ln for ln in code if "psql -X" in ln]  # invocations, not the messages that name psql
    sock = [ln for ln in psql if '-h "$SOCK"' in ln]
    tcp = [ln for ln in psql if 'PGPASSWORD="$APPPW"' in ln and "-U otodock " in ln]
    assert len(psql) == 2 and len(sock) == 1 and len(tcp) == 1, psql
    assert "PGPASSWORD" not in sock[0]
    # The admin key is never a login credential inside the one-shot.
    assert not any("ADMPW" in ln and "PGPASSWORD" in ln for ln in code)
    assert "PASSWORD :'pw'" in text and "PASSWORD '" not in text, "passwords only via psql -v"


def test_db_init_password_step_is_last(base):
    text = "\n".join(_code_lines(_db_init_script(base)))
    null_at = text.rindex("ALTER ROLE otodock_admin PASSWORD NULL")
    optin_at = text.rindex("ALTER ROLE otodock_admin PASSWORD :'pw'")
    earlier = max(
        text.rindex("pg_advisory_xact_lock"),
        text.rindex("ALTER ROLE otodock PASSWORD :'pw'"),
        text.rindex("app role otodock verified over TCP"),
        text.rindex("esac"),
    )
    assert min(null_at, optin_at) > earlier, "the superuser's password is the LAST step"
    tail = text[max(null_at, optin_at):].split("\n", 1)[1]
    assert not re.search(r"\b(ALTER|CREATE|DROP|GRANT|REVOKE)\b", tail), tail


def test_proxy_waits_for_db_init(base):
    dep = _svc(base, "otodock-proxy")["depends_on"]
    assert dep["otodock-db-init"]["condition"] == "service_completed_successfully"


def test_proxy_connects_as_app_role(base):
    # The runtime DSN stays the non-super app role `otodock` (unchanged shape).
    url = _svc(base, "otodock-proxy")["environment"]["DATABASE_URL"]
    assert url.startswith("postgresql://otodock:")


# --- the operator files never push a network password on the superuser ------

def test_install_sh_writes_no_admin_key():
    t = (_REPO / "scripts/install.sh").read_text()
    assert "_admin_pw" not in t
    assert "OTODOCK_DB_ADMIN_PASSWORD=${" not in t
    assert "printf 'OTODOCK_DB_ADMIN_PASSWORD=" not in t, "no backfill into an existing .env"
    assert re.search(r"^#OTODOCK_DB_ADMIN_PASSWORD=$", t, re.M), "the key is commented out"
    assert "docker compose exec otodock-postgres psql -U otodock_admin -d otodock" in t
    assert "POSTGRES_PASSWORD=${_pw}" in t
    # The template heredoc is unquoted: a backtick or $( would run at install time.
    tmpl = t[t.index("( umask 077; cat > .env ) <<EOF"):t.index("\nEOF\n")]
    assert "`" not in tmpl and "$(" not in tmpl


def test_config_env_example_calls_admin_key_optional():
    t = (_REPO / "config.env.example").read_text()
    block = t[t.index("# ── Database ──"):t.index("# ── Server ──")]
    assert "# OTODOCK_DB_ADMIN_PASSWORD=" in block
    for s in ("Optional", "no network password",
              "docker compose exec otodock-postgres psql -U otodock_admin -d otodock"):
        assert s in block, s
    for s in ("falls back to POSTGRES_PASSWORD", "SHOULD add a distinct value"):
        assert s not in block, s


def test_compose_sh_hint_has_no_admin_key():
    t = (_REPO / "scripts/compose.sh").read_text()
    assert "OTODOCK_DB_ADMIN_PASSWORD" not in t
    hint = [ln for ln in t.splitlines() if "POSTGRES_PASSWORD=%s" in ln]
    assert len(hint) == 1 and "> config.env" in hint[0] and "{" not in hint[0], hint


# --- bind knobs ---------------------------------------------------

def test_proxy_port_honours_bind_ip(base):
    ports = _svc(base, "otodock-proxy")["ports"]
    assert any("PROXY_BIND_IP" in str(p) for p in ports), (
        "proxy port must honour PROXY_BIND_IP for the loopback layout"
    )


def test_phone_ports_bind_loopback_by_default(phone):
    ports = _svc(phone, "otodock-phone")["ports"]
    for p in ports:
        assert "OTO_PHONE_BIND:-${OTO_AUDIOSOCKET_PUBLIC_HOST:-127.0.0.1}" in str(p), (
            f"phone port {p!r} must default to the 127.0.0.1 bind"
        )


# --- resource floors --------------------------------------------

def test_proxy_has_fd_and_pids_floors(base):
    proxy = _svc(base, "otodock-proxy")
    nofile = proxy["ulimits"]["nofile"]
    assert nofile["soft"] == 65536 and nofile["hard"] == 524288
    assert "OTODOCK_PROXY_PIDS_LIMIT" in str(proxy["pids_limit"])
