"""Static invariants over the shipped compose files (no stack spin-up).

These encode the deployment-isolation invariants:
the Postgres and docker-socket-proxy control planes are off the shared
`otodock` network; file-tools and Collabora are on an internal-only network;
the app DB role is demoted via a one-shot init service; the proxy port and the
phone ports honour their bind knobs; and the proxy carries fd/pids floors.
"""
from __future__ import annotations

import os
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
        "otodock", "socketproxy", "otodock-data", "otodock-internal", "otodock-compute",
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
    # A release's first boot migrates the database (the tools' environments
    # come with the image). Compose stops waiting for the proxy the moment Docker calls it
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


def test_the_native_compose_publishes_only_on_loopback_by_default():
    """The bare-metal file-tools compose: file-tools' port is loopback only,
    and Collabora's native port follows COLLABORA_BIND_IP, loopback when it
    is unset (the proxy reaches it on localhost; a LAN host must not reach
    its convert-to)."""
    native = _load("mcps/custom/file-tools-mcp/docker-compose.yml")
    assert _svc(native, "file-tools")["ports"] == ["127.0.0.1:8932:8932"]
    assert _svc(native, "collabora")["ports"] == ["${COLLABORA_BIND_IP:-127.0.0.1}:9981:9980"]
    for name, svc in native["services"].items():
        for port in svc.get("ports") or []:
            assert str(port).startswith(("127.0.0.1:", "${COLLABORA_BIND_IP:-127.0.0.1}:")), (name, port)


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
    assert re.search(rf"find [^;]*{_BIN} [^;]*-execdir chown -h 1000:1000", command)
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
    assert t2["read_only"] is True and t2["volume"] == {"nocopy": True, "subpath": "welcome.js"}
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


#: The volumes the one-shot gives to the proxy's user.
_INIT_OWNED = ("/opt/otodock/agents", "/opt/otodock/mcps/community", "/opt/otodock/mcps/skills",
               "/opt/otodock/mcps/custom/ssh-hosts/keys", "/opt/otodock/sessions",
               "/opt/otodock/recover-bin", "/opt/otodock/compute", "/opt/otodock/compute-link")


def _init_owner_line(base) -> str:
    script = _svc(base, "otodock-agents-init")["command"][2]
    return next(ln for ln in script.splitlines() if "/opt/otodock/agents " in ln)


def test_the_init_never_touches_a_file_that_already_has_the_right_owner(base):
    """A chown to the same owner moves a file's ctime, and the working-files
    window ages a file by it: on every `up` an unguarded walk would restart
    every file's age, and no file would ever reach the window."""
    script = _svc(base, "otodock-agents-init")["command"][2]
    assert "chown -R" not in script, "a recursive chown restarts the age of every file"
    line = _init_owner_line(base)
    assert line.startswith(f"{_INIT_PATH} find ")
    assert r"\( ! -uid 1000 -o ! -gid 1000 \)" in line
    assert "-execdir chown -h 1000:1000 {} +" in line, "a link is changed itself, never its target"
    for path in _INIT_OWNED:
        assert f" {path} " in line, path


_INIT_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"


def test_the_init_line_passes_only_a_wrong_owner_to_chown_from_its_folder(base, tmp_path):
    """The line run for real against a fake chown, under the line's own PATH
    and with the current folder in the PATH it was started with (GNU find
    refuses -execdir under such a PATH, so the line must set its own): this
    test's own user is the right owner in one run, a wrong one in the next.
    chown runs from each entry's own folder and is handed ./<name>, so no
    folder above an entry is named again, and the walk never follows a link:
    the folder a link names (this test's own, with a file in it) is never
    entered and nothing in it is handed to chown."""
    agents = tmp_path / "agents"
    (agents / "uploads").mkdir(parents=True)
    (agents / "uploads" / "a.pdf").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "outside.txt").write_text("not the volume's")
    (agents / "link").symlink_to(elsewhere)
    called = tmp_path / "called.txt"
    fake = tmp_path / "bin" / "fake-chown"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s %s\\n" "$PWD" "$*" >> "{called}"\n')
    fake.chmod(0o755)
    line = _init_owner_line(base).replace("/opt/otodock/", f"{tmp_path}/")
    assert line.count("-execdir chown ") == 1
    line = line.replace("-execdir chown ", f"-execdir {fake} ")

    def run(uid: int, gid: int) -> set[tuple[str, str]]:
        """(the folder chown ran in, a name it was handed), for every call."""
        called.unlink(missing_ok=True)
        text = line.replace("-uid 1000", f"-uid {uid}").replace("-gid 1000", f"-gid {gid}")
        r = subprocess.run(["sh", "-c", text], capture_output=True, text=True, timeout=60, cwd=tmp_path,
                           env={"PATH": ".:/usr/bin:/bin"})
        assert r.returncode in (0, 1), r.stderr
        seen = set()
        for entry in called.read_text().splitlines() if called.exists() else []:
            folder, rest = entry.split(" -h 1000:1000 ")
            seen |= {(folder, name) for name in rest.split()}
        return seen

    assert run(os.getuid(), os.getgid()) == set()
    assert (str(agents / "uploads"), "./a.pdf") in run(os.getuid(), os.getgid() + 1)
    seen = run(os.getuid() + 1, os.getgid())
    assert {(str(agents / "uploads"), "./a.pdf"), (str(agents), "./link")} <= seen
    assert all(name.startswith("./") and "/" not in name[2:] for _folder, name in seen)
    assert not any(name == "./outside.txt" or folder.startswith((str(elsewhere), str(agents / "link")))
                   for folder, name in seen), "the ownership pass followed a link"


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
    assert t2["volume"] == {"nocopy": True, "subpath": "proof_key"}
    assert _svc(base, "collabora")["depends_on"]["otodock-agents-init"]["condition"] == \
        "service_completed_successfully"
    assert base["volumes"]["otodock-collabora-proof"] == {"name": "otodock-collabora-proof"}
    t1 = _key_mount(_svc(native, "collabora"))
    assert t1["source"] == "collabora-proof" and t1["read_only"] is True
    assert t1["volume"] == {"nocopy": True, "subpath": "proof_key"}
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


def test_the_build_overlay_builds_the_proxy_image_once(base):
    """The one-shot runs on the image the proxy service builds: a build block of
    its own would export and unpack the image a second time, and a pull would take
    a release image the tree may not match."""
    build = _load("docker-compose.build.yml")
    init = _svc(build, "otodock-agents-init")
    assert init == {"pull_policy": "never"}
    assert _svc(base, "otodock-agents-init")["image"] == _svc(base, "otodock-proxy")["image"]
    proxy = _svc(build, "otodock-proxy")
    assert proxy["pull_policy"] == "build" and proxy["build"]["dockerfile"] == "proxy/Dockerfile"
    assert [name for name, svc in build["services"].items()
            if svc.get("build", {}).get("dockerfile") == "proxy/Dockerfile"] == ["otodock-proxy"]


def test_the_image_copies_what_the_installer_reads_before_it_runs():
    """The heavy layers rest on a COPY of the installer and what it reads beside
    itself, so a change to any other script keeps them. The installer only warns when
    a file beside it is missing, so a file left off this COPY would ship an image
    without it and no build would fail."""
    dockerfile = (_REPO / "proxy" / "Dockerfile").read_text()
    layer = re.search(r"^COPY ((?:scripts/\S+ )+)/opt/otodock/scripts/\nRUN [^\n]*install-baseline-tools\.sh",
                      dockerfile, re.MULTILINE)
    assert layer, "proxy/Dockerfile has no COPY of the installer right before it runs"
    installer = (_REPO / "scripts" / "install-baseline-tools.sh").read_text()
    beside = set(re.findall(r'\$SCRIPT_DIR/([\w.-]+)', installer))
    assert beside, "the installer reads nothing beside itself: drop this COPY's other names"
    assert {p.removeprefix("scripts/") for p in layer.group(1).split()} == {"install-baseline-tools.sh"} | beside
    # The rest of the folder still reaches the same path, after those layers.
    assert dockerfile.index("COPY scripts/ /opt/otodock/scripts/") > layer.end()


# --- the compute service (S-B2, S-B5) ----------------------------------------

def _mounts(svc: dict) -> dict[str, tuple[str, bool]]:
    """target -> (source, read_only), whichever syntax the entry uses."""
    out: dict[str, tuple[str, bool]] = {}
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):
            out[v["target"]] = (v["source"], bool(v.get("read_only")))
        else:
            parts = v.split(":")
            out[parts[1]] = (parts[0], len(parts) > 2 and "ro" in parts[2].split(","))
    return out


def test_the_compute_service_mounts_what_it_needs_and_nothing_the_platform_trusts(base):
    """S-B2: the agents folder, the community tools as a volume of its own,
    the skills read-only and its own two volumes; never the proxy's sessions
    folder, config.env, the SSH keys, the recover bin, the database socket or
    the data root, and no database address or env file."""
    compute = _svc(base, "otodock-compute")
    assert _mounts(compute) == {
        "/opt/otodock/agents": ("otodock-agents", False),
        "/opt/otodock/mcps/community": ("otodock-compute-tools", False),
        "/opt/otodock/mcps/skills": ("otodock-skills", True),
        "/opt/otodock/compute": ("otodock-compute", False),
        "/opt/otodock/compute-link": ("otodock-compute-link", True),
    }
    sources = {src for src, _ro in _mounts(compute).values()}
    for forbidden in ("otodock-sessions", "otodock-ssh-keys", "otodock-recover-bin", "otodock-pgsock",
                      "otodock-pgdata", "otodock-collabora-proof"):
        assert forbidden not in sources, forbidden
    assert not any("config.env" in t or ".env" in s for t, (s, _ro) in _mounts(compute).items())
    assert "/opt/otodock" not in _mounts(compute)
    assert "env_file" not in compute
    env = compute.get("environment") or {}
    assert not any(k in env for k in ("DATABASE_URL", "POSTGRES_PASSWORD", "PROXY_API_KEY", "JWT_SECRET"))


def test_only_the_proxy_reaches_the_sessions_volume(base):
    """The sessions volume holds the connected accounts' tokens and the
    histories of memory and settings (services/infra/git_writer.py), which
    the proxy's own git reads: the proxy mounts it, the one-shot that sets
    its owner does, and no service a session can reach."""
    holders = {name for name, svc in base["services"].items()
               if any(source == "otodock-sessions" for source, _ro in _mounts(svc).values())}
    assert holders == {"otodock-proxy", "otodock-agents-init"}


def test_the_oom_scores_put_the_tools_and_sessions_before_the_control_plane(base):
    """The kernel takes a tool or a session before the control plane: the
    proxy and Postgres stay at the default, the compute service sits between
    them and its sessions (the satellite raises those to 500)."""
    assert _svc(base, "otodock-compute")["oom_score_adj"] == 250
    assert _svc(base, "file-tools")["oom_score_adj"] == 500
    assert _svc(base, "collabora")["oom_score_adj"] == 500
    assert "oom_score_adj" not in _svc(base, "otodock-proxy")
    assert "oom_score_adj" not in _svc(base, "otodock-postgres")


def test_the_image_carries_the_tools_environments_and_no_start_builds_them(base):
    """Built by the platform's bootstrap in a stage of their own, copied to
    the final image after its chown and owned by the runtime user; the compute
    service no longer builds its own copy."""
    dockerfile = (_REPO / "proxy" / "Dockerfile").read_text()
    assert "FROM base AS tools" in dockerfile
    assert "mcp_venv_bootstrap --build-image /tools-out" in dockerfile
    assert "PLATFORM_CONFIG_DIR=/tmp/build" in dockerfile
    chown = dockerfile.rindex("chown 1000:1000 /opt/otodock/compute /opt/otodock/compute-link")
    copy = dockerfile.index("COPY --from=tools --chown=1000:1000 /tools-out/ /opt/otodock/mcps/")
    assert copy > chown
    assert "OTODOCK_COMPUTE_OWN_TOOLS" not in str(_svc(base, "otodock-compute"))


def test_the_compute_service_shares_the_proxys_folders_declared_once(base):
    """S-B5: the shared folders come from one declaration each, so the base
    file never splits them (an override file is compared by upgrade.sh). The
    community tools are not shared, so they have no declaration to share."""
    proxy, compute = _mounts(_svc(base, "otodock-proxy")), _mounts(_svc(base, "otodock-compute"))
    for target in ("/opt/otodock/agents", "/opt/otodock/mcps/skills"):
        assert proxy[target][0] == compute[target][0], target
    assert proxy["/opt/otodock/compute-link"] == ("otodock-compute-link", False)
    text = (_REPO / "docker-compose.yml").read_text()
    for anchor in ("&agents-folder", "&skills-folder"):
        assert text.count(anchor) == 1, anchor
    assert "community-tools" not in text


_TOOLS = "/opt/otodock/mcps/community"


_NOT_SOURCE = {"node_modules", "venv", ".venv", ".git", "dist", "build", "__pycache__"}


def _own_files(root: Path) -> list[Path]:
    """The repository's own files under ``root``: what git lists there,
    tracked or new, where the root is a checkout. An install that runs from
    the checkout keeps its data in ignored folders, and a file an agent
    writes there is none of the repository's. A tree that is no checkout (an
    exported one) is walked."""
    if (root / ".git").exists():
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                                capture_output=True, text=True, check=True)
        return [root / rel for rel in listed.stdout.split("\0") if rel and (root / rel).is_file()]
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NOT_SOURCE]
        found.extend(Path(folder) / name for name in names)
    return found


def _compose_files(root: Path = _REPO) -> list[Path]:
    """Every file of the repository that declares compose services: the
    shipped files and the rig's overrides."""
    found = []
    for path in _own_files(root):
        if path.suffix not in (".yml", ".yaml") or _NOT_SOURCE & set(path.relative_to(root).parts):
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
            found.append(path)
    return sorted(found)


def test_the_community_tools_are_two_volumes_one_for_each_service(base):
    """The proxy keeps the tools' sources, manifests, skills and patches in
    its volume. The compute service builds and runs them in a volume of its
    own at the same path, read and write, which is in no backup."""
    assert base["volumes"]["otodock-compute-tools"] == {"name": "otodock-compute-tools"}
    assert _mounts(_svc(base, "otodock-proxy"))[_TOOLS] == ("otodock-mcps", False)
    assert _mounts(_svc(base, "otodock-compute"))[_TOOLS] == ("otodock-compute-tools", False)
    assert "otodock-mcps" not in {src for src, _ro in _mounts(_svc(base, "otodock-compute")).values()}


def test_no_service_but_the_compute_service_the_init_and_the_copy_mounts_its_tools():
    """In every compose file of the repository: the proxy never mounts the
    volume a compute machine writes, and neither does a sidecar (the tools'
    copy runs as the compute service's own account)."""
    files = _compose_files()
    assert _REPO / "docker-compose.yml" in files and len(files) >= 4
    for path in files:
        for name, svc in (yaml.safe_load(path.read_text())["services"] or {}).items():
            if name in ("otodock-compute", "otodock-agents-init", "otodock-tools-copy"):
                continue
            assert "otodock-compute-tools" not in str((svc or {}).get("volumes") or []), (path.name, name)


def test_the_compute_service_links_on_its_own_plane_from_its_pinned_address(base):
    """`otodock` is its way out (the only plane with a gateway: the engines and
    the tool builds reach the internet through it) and `otodock-compute` its
    link. It dials no Docker tool: every call passes the proxy's door."""
    compute = _svc(base, "otodock-compute")
    assert _nets(compute) == {"otodock", "otodock-compute"}
    on_internal = {name for name, svc in base["services"].items() if "otodock-internal" in _nets(svc)}
    assert on_internal == {"otodock-proxy", "file-tools", "collabora"}
    plane = base["networks"]["otodock-compute"]
    assert plane["internal"] is True
    assert plane["ipam"]["config"][0]["subnet"] == "${OTODOCK_COMPUTE_SUBNET:-10.205.0.0/24}"
    pinned = compute["networks"]["otodock-compute"]["ipv4_address"]
    assert pinned == _svc(base, "otodock-proxy")["environment"]["OTODOCK_COMPUTE_LINK_FROM"]
    assert pinned == "${OTODOCK_COMPUTE_ADDRESS:-10.205.0.10}"
    assert compute["environment"]["OTODOCK_COMPUTE_PLATFORM_URL"] == "http://otodock-proxy-compute:8400"
    assert _svc(base, "otodock-proxy")["networks"]["otodock-compute"]["aliases"] == ["otodock-proxy-compute"]
    # Both ends pinned: a dynamic address handed to a recreated proxy could be
    # the compute service's own, and its next start would fail.
    assert _svc(base, "otodock-proxy")["networks"]["otodock-compute"]["ipv4_address"] == (
        "${OTODOCK_COMPUTE_PROXY_ADDRESS:-10.205.0.2}")
    members = {name for name, svc in base["services"].items() if "otodock-compute" in _nets(svc)}
    assert members == {"otodock-proxy", "otodock-compute"}
    from services.infra import network_selfcheck
    assert network_selfcheck._PLANE_MEMBERS["otodock-compute"] == members


#: What a container needs to start the platform's sandbox, and no more.
_SANDBOX_RELAXATIONS = ["seccomp=unconfined", "apparmor=${OTODOCK_APPARMOR_PROFILE:-unconfined}",
                        "systempaths=unconfined"]


def test_the_compose_files_read_are_the_repositorys_own(tmp_path):
    """T1 runs from the main checkout, and its agents write under ignored
    folders of it. A compose file drafted there, with a privileged proxy
    service, is no file of the repository and must not turn this gate red."""
    relaxed = "services:\n  otodock-proxy:\n    devices: [/dev/net/tun]\n"
    plain = "services:\n  otodock-proxy:\n    image: x\n"
    for rel, text in {"docker-compose.yml": plain, "rig/override.yml": plain, "new/untracked.yaml": plain,
                      "agents/dev/workspace/draft.yml": relaxed, "notes.yml": "- no services here\n",
                      "node_modules/pkg/docker-compose.yml": relaxed}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    walked = {str(p.relative_to(tmp_path)) for p in _compose_files(tmp_path)}
    assert walked == {"docker-compose.yml", "rig/override.yml", "new/untracked.yaml", "agents/dev/workspace/draft.yml"}
    (tmp_path / ".gitignore").write_text("agents/\nnode_modules/\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "docker-compose.yml", "rig/override.yml"], check=True)
    listed = {str(p.relative_to(tmp_path)) for p in _compose_files(tmp_path)}
    assert listed == {"docker-compose.yml", "rig/override.yml", "new/untracked.yaml"}


def test_the_proxys_container_holds_no_relaxation_in_any_compose_file():
    """The platform starts no sandbox, so its container is given nothing a
    sandbox needs: in every compose file of the repository the proxy's
    service relaxes no security profile, maps no device, adds no capability
    and is not privileged."""
    files = _compose_files()
    assert _REPO / "docker-compose.yml" in files
    if (_REPO / "rig").is_dir():
        assert any("rig" in f.relative_to(_REPO).parts for f in files), "the rig's overrides are read too"
    for path in files:
        proxy = yaml.safe_load(path.read_text())["services"].get("otodock-proxy")
        if not proxy:
            continue
        for key in ("security_opt", "devices", "cap_add"):
            assert not proxy.get(key), f"{path.relative_to(_REPO)}: otodock-proxy has {key}"
        assert not proxy.get("privileged"), str(path.relative_to(_REPO))


def test_the_compute_service_alone_runs_the_sandbox(base):
    proxy, compute = _svc(base, "otodock-proxy"), _svc(base, "otodock-compute")
    assert compute["image"] == proxy["image"] and compute["user"] == "1000:1000"
    assert compute["security_opt"] == _SANDBOX_RELAXATIONS
    assert compute["devices"] == ["/dev/net/tun"]
    assert "cap_add" not in compute and not compute.get("privileged")
    assert compute["mem_limit"] == "${OTODOCK_COMPUTE_MEM_LIMIT:-0}"
    assert compute["pids_limit"] == "${OTODOCK_COMPUTE_PIDS_LIMIT:-32768}"
    assert compute["command"][-1] == "/opt/otodock/proxy/compute_start.py"
    assert "otodock-proxy" not in compute.get("depends_on", {})
    assert compute["stop_grace_period"] == "120s"


def test_the_build_overlay_never_pulls_the_compute_service():
    build = _load("docker-compose.build.yml")
    assert build["services"]["otodock-compute"] == {"pull_policy": "never"}


def test_the_init_one_shot_owns_the_compute_volumes(base):
    init = _svc(base, "otodock-agents-init")
    assert "otodock-compute:/opt/otodock/compute" in init["volumes"]
    assert "otodock-compute-link:/opt/otodock/compute-link" in init["volumes"]
    script = "\n".join(init["command"])
    assert "/opt/otodock/compute /opt/otodock/compute-link" in script


_COPY = "/opt/otodock/scripts/copy-tools-to-compute.sh"


def test_the_tools_copy_runs_as_the_compute_services_account_beside_its_two_volumes_only(base):
    """The compute service writes its tools volume, so the copy into it runs
    as that service's own account, sees nothing but the two tools volumes
    (the proxy's read-only) and holds no privilege: whatever the compute
    service left there, the copy reaches nothing it cannot reach itself. The
    init owns the volume's root first; both services wait for the copy, which
    never fails the start."""
    copy = _svc(base, "otodock-tools-copy")
    assert copy["user"] == "1000:1000"
    assert copy["network_mode"] == "none" and copy["read_only"] is True and copy["restart"] == "no"
    assert copy["cap_drop"] == ["ALL"] and copy["security_opt"] == ["no-new-privileges:true"]
    assert copy["image"] == _svc(base, "otodock-proxy")["image"]
    assert _mounts(copy) == {_TOOLS: ("otodock-mcps", True),
                             "/opt/otodock/compute-tools": ("otodock-compute-tools", False)}
    assert "env_file" not in copy and not copy.get("environment")
    assert copy["command"][:2] == ["sh", "-c"]
    assert copy["command"][2] == f"sh {_COPY} {_TOOLS} /opt/otodock/compute-tools || true"
    assert copy["depends_on"] == {"otodock-agents-init": {"condition": "service_completed_successfully"}}
    for name in ("otodock-proxy", "otodock-compute"):
        waits = _svc(base, name)["depends_on"]
        assert waits["otodock-agents-init"] == {"condition": "service_completed_successfully"}
        assert waits["otodock-tools-copy"] == {"condition": "service_completed_successfully"}
    init = _svc(base, "otodock-agents-init")
    assert _mounts(init)["/opt/otodock/compute-tools"] == ("otodock-compute-tools", False)
    chown = re.search(r"find ([^\n]*) \\\( ! -uid 1000 [^\n]*-execdir chown -h 1000:1000", "\n".join(init["command"]))
    assert chown and "/opt/otodock/compute-tools" in chown.group(1).split()
    # The image carries the script where the one-shot runs it.
    assert (_REPO / "scripts" / "copy-tools-to-compute.sh").is_file()
    assert "COPY scripts/ /opt/otodock/scripts/" in (_REPO / "proxy" / "Dockerfile").read_text()
    assert _svc(_load("docker-compose.build.yml"), "otodock-tools-copy") == {"pull_policy": "never"}
    from services.mcp.compose_rewrite import _RESERVED_PLATFORM_NAMES
    assert "otodock-tools-copy" in _RESERVED_PLATFORM_NAMES


def test_no_root_one_shot_writes_by_name_into_a_volume_a_session_can_write(base):
    """A root one-shot reaches into a volume the compute service writes only
    through the ownership pass (from each entry's own folder) and the chmod
    of a mount point: it runs no script there, copies nothing and writes no
    file there by name."""
    writable = {source for source, ro in _mounts(_svc(base, "otodock-compute")).values() if not ro}
    for name, svc in base["services"].items():
        if str(svc.get("user", "")).split(":")[0] not in ("0", "root"):
            continue
        script = "\n".join(map(str, svc.get("command") or []))
        assert _COPY not in script, name
        targets = {t for t, (source, _ro) in _mounts(svc).items() if source in writable}
        for line in script.splitlines():
            named = [t for t in targets if re.search(rf"{re.escape(t)}(/|\s|$)", line)]
            if not named:
                continue
            if "-execdir chown -h 1000:1000 {} +" in line and line.startswith(f"{_INIT_PATH} find "):
                continue
            words = line.split()
            assert words[0] == "chmod", (name, line)
            assert all(w in _mounts(svc) or re.fullmatch(r"0[0-7]{3}|2>/dev/null", w) for w in words[1:]), (name, line)


def test_a_community_tool_cannot_take_the_compute_services_names():
    from services.mcp import compose_rewrite
    assert "otodock-compute" in compose_rewrite._RESERVED_PLATFORM_NAMES
    assert "otodock-proxy-compute" in compose_rewrite._RESERVED_PLATFORM_NAMES
    assert {"otodock-compute", "otodock-compute-link", "otodock-compute-tools"} <= compose_rewrite._PLATFORM_VOLUMES



def test_an_app_servers_way_in_is_the_compute_plane_alone(base):
    """The proxy dials an app server at the address the compute machine's
    link comes from, on the one network only the two of them share: nothing
    is published, and no other service stands on that plane."""
    compute = _svc(base, "otodock-compute")
    assert not compute.get("ports") and not compute.get("expose")
    assert base["networks"]["otodock-compute"]["internal"] is True
    assert compute["networks"]["otodock-compute"]["ipv4_address"] == (
        _svc(base, "otodock-proxy")["environment"]["OTODOCK_COMPUTE_LINK_FROM"])
    members = {name for name, svc in base["services"].items() if "otodock-compute" in _nets(svc)}
    assert members == {"otodock-proxy", "otodock-compute"}
