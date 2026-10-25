"""Every key ``config.py`` reads is documented, and the documented keys are
read by something.

The keys are found through the AST (every ``_cfg``, ``_cfg_bool``,
``_cfg_int`` and ``_cfg_float`` call with a constant first argument), so a
key spelled over several lines or read with a computed default counts too.
``config.env.example`` documents a key with a ``KEY=`` token anywhere on a
line, commented out or not (several OIDC keys share one line).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import config

REPO = Path(__file__).resolve().parents[3]
CONFIG_PY = REPO / "proxy" / "config.py"
EXAMPLE = REPO / "config.env.example"
COMPOSE_FILES = [REPO / name for name in (
    "docker-compose.yml", "docker-compose.t1.yml", "docker-compose.phone.yml",
    "docker-compose.build.yml",
)]
_KEY_TOKEN = re.compile(r"(?:^|[\s#])([A-Z][A-Z0-9_]*)=")
_COMPOSE_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_READERS = {"_cfg", "_cfg_bool", "_cfg_int", "_cfg_float"}


def keys_read() -> set[str]:
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _READERS and node.args):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.add(first.value)
            elif isinstance(first, ast.JoinedStr):
                # The rate-limit buckets: RATE_LIMIT_<PREFIX>_<FIELD>, covered by the prefix.
                assert ast.unparse(first).startswith("f'RATE_LIMIT_"), ast.unparse(first)
            else:
                # The helpers pass their own ``key`` through.
                assert isinstance(first, ast.Name) and first.id == "key", ast.unparse(first)
    return out


def bool_keys_read() -> set[str]:
    """The keys read through ``_cfg_bool`` (the yes/no rule)."""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    return {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_cfg_bool"
            and node.args and isinstance(node.args[0], ast.Constant)}


def documented_keys() -> set[str]:
    out: set[str] = set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        out.update(_KEY_TOKEN.findall(line))
    return out


def compose_variables() -> set[str]:
    out: set[str] = set()
    for path in COMPOSE_FILES:
        if path.exists():
            out.update(_COMPOSE_VAR.findall(path.read_text(encoding="utf-8")))
    return out


def test_every_key_config_reads_is_documented_or_declared_internal():
    missing = sorted(keys_read() - documented_keys() - config.INTERNAL_CONFIG_KEYS)
    assert missing == [], (
        f"keys config.py reads that config.env.example does not document: {missing}; "
        "add a line to the example, or the key with its reason to INTERNAL_CONFIG_KEYS"
    )


def test_an_internal_key_is_not_also_documented():
    both = sorted(config.INTERNAL_CONFIG_KEYS & documented_keys())
    assert both == [], f"documented keys still listed as internal: {both}"


def test_every_documented_key_is_read_by_something():
    read = keys_read() | config.FOREIGN_CONFIG_KEYS | config.LAZY_CONFIG_KEYS
    dead = sorted(k for k in documented_keys() - read
                  if not any(k.startswith(p) for p in config.FOREIGN_CONFIG_PREFIXES))
    assert dead == [], f"config.env.example documents keys nothing reads: {dead}"


def test_the_foreign_list_covers_every_compose_variable():
    covered = keys_read() | config.FOREIGN_CONFIG_KEYS
    missing = sorted(v for v in compose_variables() - covered
                     if not any(v.startswith(p) for p in config.FOREIGN_CONFIG_PREFIXES))
    assert missing == [], f"compose variables the unknown-key warning would name: {missing}"


def test_upgrade_sh_judges_the_same_yes_no_keys_the_proxy_reads():
    """``scripts/upgrade.sh`` names a yes/no value the new release would refuse
    before anything changes: its list is config.py's ``_cfg_bool`` keys."""
    text = (REPO / "scripts" / "upgrade.sh").read_text(encoding="utf-8")
    m = re.search(r'^YESNO_KEYS="([^"]*)"', text, re.M)
    assert m, "upgrade.sh lost its YESNO_KEYS list"
    assert set(m.group(1).split()) == bool_keys_read()


def test_a_stock_env_file_raises_no_warning():
    """Every key install.sh's .env template names (active or commented out),
    every key it appends later, the secrets the proxy persists itself and
    T1's own config.env keys are all known."""
    # The heredoc's lines (active or commented out) and the lines it appends with echo.
    template = {m.group(1) for m in re.finditer(r'(?:^#?\s*|echo ")([A-Z][A-Z0-9_]*)=',
                                                (REPO / "scripts" / "install.sh").read_text(), re.M)}
    assert {"POSTGRES_PASSWORD", "DASHBOARD_PUBLIC_URL", "TZ", "COMPOSE_FILE", "OTODOCK_APPARMOR_PROFILE"} <= template
    persisted = {"PROXY_API_KEY", "JWT_SECRET", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY",
                 "OTODOCK_INSTALL_ID", "PHONE_API_SECRET"}
    t1 = {"DATABASE_URL", "CLAUDE_BIN", "CODEX_BIN", "CREDENTIAL_ENCRYPTION_KEY", "OTODOCK_VERSION"}
    known = keys_read() | config.FOREIGN_CONFIG_KEYS | config.LAZY_CONFIG_KEYS
    assert config.unknown_config_keys(template | persisted | t1, known) == []


# ── the egress proxy a Docker install's .env names ────────────────────────

def test_the_egress_proxy_in_the_file_reaches_the_environment():
    from configuration.readers import export_egress_proxy
    env: dict[str, str] = {"PATH": "/usr/bin"}
    file_cfg = {"HTTPS_PROXY": "http://proxy.example.com:3128", "NO_PROXY": ".lan,10.0.0.0/8",
                "HTTP_PROXY": "http://proxy.example.com:3128", "ALL_PROXY": "socks5://proxy.example.com:1080",
                "TZ": "Europe/Athens"}
    assert sorted(export_egress_proxy(file_cfg, env)) == ["HTTPS_PROXY", "NO_PROXY"]
    assert env == {"PATH": "/usr/bin", "HTTPS_PROXY": "http://proxy.example.com:3128",
                   "NO_PROXY": ".lan,10.0.0.0/8"}


def test_an_https_proxy_with_no_exceptions_spares_this_machine_and_its_network():
    """No NO_PROXY anywhere: the exported proxy never takes a call to this
    machine or to a name the server dials on its own Docker networks."""
    from configuration.readers import DEFAULT_NO_PROXY, export_egress_proxy
    env: dict[str, str] = {}
    assert export_egress_proxy({"https_proxy": "http://proxy.example.com:3128"}, env) == [
        "https_proxy", "NO_PROXY"]
    assert env["NO_PROXY"] == DEFAULT_NO_PROXY
    names = set(DEFAULT_NO_PROXY.split(","))
    assert {"localhost", "127.0.0.1", "::1", "otodock-postgres", "docker-socket-proxy",
            "collabora", "otodock-phone"} <= names
    # A source install's unit hands the file's line over in the environment.
    env_unit = {"HTTPS_PROXY": "http://proxy.example.com:3128"}
    assert export_egress_proxy({"HTTPS_PROXY": "http://proxy.example.com:3128"}, env_unit) == ["NO_PROXY"]
    assert env_unit["NO_PROXY"] == DEFAULT_NO_PROXY
    # The environment's own NO_PROXY, even an empty one, is left as it is.
    env2 = {"no_proxy": ""}
    assert export_egress_proxy({"HTTPS_PROXY": "http://proxy.example.com:3128"}, env2) == ["HTTPS_PROXY"]
    assert env2 == {"no_proxy": "", "HTTPS_PROXY": "http://proxy.example.com:3128"}


def test_the_default_exceptions_name_the_compose_services_the_server_dials():
    """DEFAULT_NO_PROXY's service names are the compose files' own."""
    from configuration.readers import DEFAULT_NO_PROXY
    services = set()
    for path in COMPOSE_FILES:
        if path.exists():
            services |= set(re.findall(r"^  ([a-z][a-z0-9-]*):\n", path.read_text(encoding="utf-8"), re.M))
    hosts = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    assert set(DEFAULT_NO_PROXY.split(",")) - hosts <= services


def test_the_environment_keeps_its_own_egress_proxy():
    """Either spelling in the environment (a systemd unit's EnvironmentFile,
    an override file, a Docker client config) keeps the whole pair: urllib
    and httpx prefer the lowercase one, so a file's would otherwise win."""
    from configuration.readers import export_egress_proxy
    env = {"https_proxy": "http://from-docker-config:3128", "NO_PROXY": ""}
    file_cfg = {"HTTPS_PROXY": "http://from-the-file:3128", "https_proxy": "http://from-the-file:3128",
                "NO_PROXY": ".lan", "no_proxy": ".lan"}
    assert export_egress_proxy(file_cfg, env) == []
    assert env == {"https_proxy": "http://from-docker-config:3128", "NO_PROXY": ""}


def test_an_empty_egress_line_exports_nothing():
    from configuration.readers import export_egress_proxy
    env: dict[str, str] = {}
    assert export_egress_proxy({"HTTPS_PROXY": "", "https_proxy": None, "NO_PROXY": "  "}, env) == []
    assert env == {}


def test_the_egress_proxy_names_draw_no_unknown_key_line():
    known = keys_read() | config.FOREIGN_CONFIG_KEYS | config.LAZY_CONFIG_KEYS
    names = {"HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"}
    assert config.unknown_config_keys(names, known) == []
