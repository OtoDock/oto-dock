"""Static invariants over the shipped compose files (no stack spin-up).

These encode the deployment-isolation invariants:
the Postgres and docker-socket-proxy control planes are off the shared
`otodock` network; file-tools and Collabora are on an internal-only network;
the app DB role is demoted via a one-shot init service; the proxy port and the
phone ports honour their bind knobs; and the proxy carries fd/pids floors.
"""
from __future__ import annotations

import re
import shutil
import stat
import subprocess
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


# --- the platform's named volumes --------------------------------

_BIN = "/opt/otodock/recover-bin"


def test_recover_bin_is_a_named_volume_on_the_proxy_alone(base, phone):
    # The bin's rows live in Postgres; without a volume a recreate leaves
    # entries whose bytes are gone. It holds every person's deleted files, so
    # no sidecar mounts it.
    assert base["volumes"]["otodock-recover-bin"] == {"name": "otodock-recover-bin"}
    assert f"otodock-recover-bin:{_BIN}" in _svc(base, "otodock-proxy")["volumes"]
    init = _svc(base, "otodock-agents-init")
    assert f"otodock-recover-bin:{_BIN}" in init["volumes"]
    command = " ".join(init["command"])
    assert re.search(rf"chown -R 1000:1000 [^;]*{_BIN}", command)
    assert re.search(rf"chmod 0700 [^;]*{_BIN}", command)
    for compose in (base, phone):
        for name, svc in compose["services"].items():
            if name in ("otodock-proxy", "otodock-agents-init"):
                continue
            assert not any("otodock-recover-bin" in str(v) for v in svc.get("volumes") or []), name


def test_every_platform_volume_but_the_agents_tree_is_refused_to_mcps(base):
    # A community MCP compose naming a platform volume is refused by the
    # rewrite; the agents tree is reached through ${HOST_AGENTS_DIR} instead.
    from services.mcp.compose_rewrite import _PLATFORM_VOLUMES
    declared = {v.get("name", k) for k, v in base["volumes"].items()}
    assert declared - {"otodock-agents"} <= _PLATFORM_VOLUMES


# --- Collabora 26.04: no shell, settings from outside, the proof key ---------
_NATIVE = "mcps/custom/file-tools-mcp/docker-compose.yml"
_KEY = "/etc/coolwsd/proof_key"
_WELCOME = "/usr/share/coolwsd/browser/dist/welcome/welcome.js"


def _extra_params(svc: dict) -> str:
    env = svc["environment"]
    pairs = dict(e.split("=", 1) for e in env) if isinstance(env, list) else env
    return pairs["extra_params"]


def _key_mount(svc: dict) -> dict:
    mounts = [v for v in svc.get("volumes") or [] if isinstance(v, dict) and v.get("target") == _KEY]
    assert len(mounts) == 1, svc.get("volumes")
    return mounts[0]


def test_every_collabora_image_is_one_pin_with_its_digest():
    """The sweep reads the first collabora/code of each file: every one, in
    both files, must be the same tag and manifest-list digest."""
    pins = set()
    for name in ("docker-compose.yml", _NATIVE):
        pins |= set(re.findall(r"image:\s*(collabora/code:\S+)", (_REPO / name).read_text()))
    assert len(pins) == 1, pins
    assert re.fullmatch(r"collabora/code:26\.04\.[0-9.]+@sha256:[0-9a-f]{64}", pins.pop())


def test_collabora_takes_its_settings_from_outside(base):
    native = _load(_NATIVE)
    for svc in (_svc(base, "collabora"), _svc(native, "collabora")):
        assert "entrypoint" not in svc  # the image has no shell
        params = _extra_params(svc)
        assert "--o:mount_jail_tree=false" in params
        assert "--o:ssl.termination=${COLLABORA_SSL_TERMINATION:-" in params
        # The relay sends the public Host: no setting names it, and no
        # client header may rebuild Collabora's addresses.
        for gone in ("server_name", "frame_ancestors", "proxy_prefix"):
            assert gone not in params, gone
        assert "MKNOD" in svc["cap_add"]


def _welcome_mounts(svc: dict) -> list:
    return [v for v in svc.get("volumes") or [] if isinstance(v, dict) and v.get("target") == _WELCOME]


def test_collabora_closes_its_welcome_dialog(base):
    """The free edition has no setting for its welcome dialog short of home
    mode, which caps the server: both stacks mount the same script over the
    one the editor loads it from. Never a compose config: Compose 2.26 to
    2.3x copy a config into the created container, which mounts the key's
    volume before the one-shot wrote the key, and the first start fails. On
    Docker the one-shot writes the script beside the key on every run (no
    single `$`: compose fills variables in the command); a native install
    binds the file shipped beside its compose file."""
    native = _load(_NATIVE)
    for doc in (base, native):
        assert "configs" not in doc
        assert "configs" not in _svc(doc, "collabora")
    (t2,) = _welcome_mounts(_svc(base, "collabora"))
    assert t2["type"] == "volume" and t2["source"] == "otodock-collabora-proof"
    assert t2["read_only"] is True and t2["volume"] == {"subpath": "welcome.js"}
    script = _svc(base, "otodock-agents-init")["command"][2]
    assert "w=/opt/otodock/collabora-proof/welcome.js" in script
    assert "cat > $$w.new <<'EOF' && chmod 0444 $$w.new && mv -fT $$w.new $$w" in script
    assert "chown 1001:1001 $$w" in script
    assert "$" not in script.replace("$$", ""), "a single $ in the init script"
    body = script.split("<<'EOF'", 1)[1].split("\nEOF\n", 1)[0].split("\n", 1)[1]
    (t1,) = _welcome_mounts(_svc(native, "collabora"))
    assert t1["type"] == "bind" and t1["source"] == "./collabora-welcome.js"
    assert t1["read_only"] is True and t1["bind"] == {"create_host_path": False}
    shipped = (_REPO / _NATIVE).parent / "collabora-welcome.js"
    assert shipped.read_text() == body + "\n"
    assert "welcome-translate" in body and "welcome-close" in body
    for doc in (base, native):
        # The script's path and its two messages were read off 26.04: a
        # bump re-checks both, or the mount misses and the welcome returns.
        assert _svc(doc, "collabora")["image"].startswith("collabora/code:26.04."), (
            "Collabora moved off 26.04: re-check the welcome override's path "
            "and messages, then this pin")


def test_the_init_writes_the_welcome_script_every_run_and_the_key_once(base, tmp_path):
    """The one-shot's command run for real: `$$` becomes `$` as compose
    makes it, the platform paths a temporary folder. The script lands with
    mode 0444 and the shipped text, a stale one is replaced at the next
    run, and the key is written once."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    script = _svc(base, "otodock-agents-init")["command"][2]
    script = script.replace("$$", "$").replace("/opt/otodock/", f"{tmp_path}/")
    proof = tmp_path / "collabora-proof"
    proof.mkdir()

    def run() -> None:
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr

    shipped = ((_REPO / _NATIVE).parent / "collabora-welcome.js").read_text()
    run()
    welcome, key = proof / "welcome.js", proof / "proof_key"
    assert welcome.read_text() == shipped
    assert stat.S_IMODE(welcome.stat().st_mode) == 0o444
    first_key = key.read_bytes()
    assert b"PRIVATE KEY" in first_key
    welcome.chmod(0o644)
    welcome.write_text("// another Collabora's script\n")
    run()
    assert welcome.read_text() == shipped
    assert key.read_bytes() == first_key
    assert not list(proof.glob("*.new"))


def test_the_compose_floor_pin_is_the_floor_every_script_checks():
    """The Compose the release candidate is booted with before a release
    (VERSIONS.md "Docker floors") is the oldest the install scripts accept:
    each script's check names the pin's major and minor."""
    pin = re.search(r"^COMPOSE_FLOOR_VERSION=(\d+)\.(\d+)\.\d+$", (_REPO / "VERSIONS.md").read_text(), re.M)
    assert pin, "VERSIONS.md has no COMPOSE_FLOOR_VERSION"
    want = [pin.groups()]
    for name in ("install.sh", "upgrade.sh", "compose.sh"):
        text = (_REPO / "scripts" / name).read_text()
        assert re.findall(r'at_least "[^"]*cv[^"]*" (\d+) (\d+)', text) == want, name
    setup = [line for line in (_REPO / "scripts" / "dev-setup.sh").read_text().splitlines() if '"${cv#v}"' in line]
    assert [re.search(r"== (\d+) && a\[2\] \+ 0 >= (\d+)", line).groups() for line in setup] == want


def test_collabora_mounts_the_proof_key_read_only(base):
    native = _load(_NATIVE)
    t2 = _key_mount(_svc(base, "collabora"))
    assert t2["source"] == "otodock-collabora-proof" and t2["read_only"] is True
    assert t2["volume"] == {"subpath": "proof_key"}
    assert _svc(base, "collabora")["depends_on"]["otodock-agents-init"]["condition"] == \
        "service_completed_successfully"
    assert base["volumes"]["otodock-collabora-proof"] == {"name": "otodock-collabora-proof"}
    t1 = _key_mount(_svc(native, "collabora"))
    assert t1["source"] == "collabora-proof" and t1["read_only"] is True
    assert t1["volume"] == {"subpath": "proof_key"}
    assert _svc(native, "collabora")["depends_on"]["collabora-proof"]["condition"] == \
        "service_completed_successfully"
    # The image's own health check stands: the proxy judges the project by
    # the file tools' own service, so Collabora's start period never shows.
    assert "healthcheck" not in _svc(native, "collabora")


def test_the_key_is_written_once_for_collaboras_user_alone(base):
    init = _svc(base, "otodock-agents-init")
    assert "otodock-collabora-proof:/opt/otodock/collabora-proof" in init["volumes"]
    command = " ".join(init["command"])
    assert "[ -s $$k ] || {" in command and "openssl genpkey" in command
    assert "mv $$k.new $$k" in command
    assert "chown 1001:1001 $$k" in command and "chmod 0400 $$k" in command
    one_shot = _svc(_load(_NATIVE), "collabora-proof")
    assert one_shot["entrypoint"][:2] == ["/usr/bin/openssl", "genpkey"]
    assert one_shot["network_mode"] == "none" and one_shot["healthcheck"] == {"disable": True}
    for name, svc in base["services"].items():
        if name in ("collabora", "otodock-agents-init"):
            continue
        assert not any("collabora-proof" in str(v) for v in svc.get("volumes") or []), name
