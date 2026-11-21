"""The credential exit on every machine kind, the proxy's half: the one
machine rule (``mcp_session_config.exit_machine_refusal``), the placeholders a
machine session's bundles hold and the machine's variables beside them, a
tool's account token by reference, the command-line credential per OS, the
start payload's ``exit`` block, gh's process environment, and that a
refused machine gets no tool whose key the exit carries.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from core import placement
from core.credentials import credential_exit, mcp_gateway
from core.credentials.mcp_gateway import CliCredential, TokenCredential, TokenRef
from services.mcp import mcp_session_config
from services.oauth import credential_resolver
from tests.mcp.test_mcp_broker_activation import (
    _exit_manifest,
    _FakeManifest,
    _stub_assembly,
)

SECRET_VALUE = "sk-eleven-0123456789"
OTODOCK_DIR = "/home/alice/.oto-dock"


def _paired(*, kind=placement.KIND_USER_REMOTE, version="0.5.175", port=41111, ok=True, os="linux",
            otodock=OTODOCK_DIR, label="Laptop", why="") -> placement.PlacementCapabilities:
    return placement.PlacementCapabilities(kind=kind, machine_id="m-1", label=label, satellite_version=version,
                                           exit_port=port, exit_ok=ok, os=os, reported_otodock_dir=otodock,
                                           exit_why=why)


def _compute(*, version="0.5.175", port=41112, ok=True, why="") -> placement.PlacementCapabilities:
    return placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one",
                                           satellite_version=version, exit_port=port, exit_ok=ok, exit_why=why)


@pytest.fixture
def no_proxy_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.example.test/v1")
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: True)
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)
    yield tmp_path
    for sid in list(credential_exit._sessions):
        credential_exit.forget(sid)


def _assembly(monkeypatch, manifests, *, env_by_mcp, tmp_path, token_credentials=None, cli_credentials=None):
    """The build's collaborators stubbed (``_stub_assembly``), with the
    resolver's token and command-line credentials of the test's own."""
    _stub_assembly(monkeypatch, manifests, env_by_mcp=env_by_mcp, tmp_path=tmp_path)
    secret = {k for d in env_by_mcp.values() for k in d}
    monkeypatch.setattr(
        credential_resolver, "resolve_credentials",
        lambda *a, **k: SimpleNamespace(
            env_by_mcp={m: dict(v) for m, v in env_by_mcp.items()},
            env_vars={k2: v2 for d in env_by_mcp.values() for k2, v2 in d.items()},
            excluded_mcps=set(), exclusion_reasons={}, available_mcps=set(env_by_mcp),
            secret_keys=secret, bash_env_keys=set(), cli_credentials=dict(cli_credentials or {}),
            token_credentials=dict(token_credentials or {}), accounts={}, agent_accounts={},
        ),
    )


def _music():
    return _exit_manifest("music", [{"key": "ELEVENLABS_API_KEY", "header": "xi-api-key",
                                     "hosts": ["api.elevenlabs.io"]}])


def _build(where, *, session_id: str | None = None, manifests=None, env_by_mcp=None):
    from services.mcp import mcp_registry
    sid = session_id or str(uuid.uuid4())
    _p, _e, _x, bundles, _ = mcp_registry.build_session_mcp_config(
        "agent", "u-1", placement=where, session_id=sid)
    return sid, bundles


# --- the one machine rule -----------------------------------------------------------


def test_the_machine_rule_admits_the_exit_where_a_satellite_runs_one(no_proxy_set):
    refuse = mcp_session_config.exit_machine_refusal
    assert refuse(placement.LOCAL_PLACEMENT) == ""
    assert refuse(_paired()) == "" and refuse(_paired(kind=placement.KIND_ADMIN_REMOTE)) == ""
    assert refuse(_compute()) == ""
    off = "Tools that need a key are off on Laptop."
    restart = off + " Restart OtoDock on that computer."
    assert refuse(_paired(port=0)) == restart and refuse(_paired(ok=False)) == restart
    assert refuse(_paired(ok=False, why="bind")) == restart
    assert refuse(_compute(port=0)) == "Tools that need a key are off on Compute one. Restart OtoDock on that computer."
    assert refuse(_paired(otodock="")) == restart
    # The advice follows the reason: a restart does not bring a package or
    # change a proxy.
    assert refuse(_paired(port=0, why="packages")) == (
        off + " A package it needs is missing there. Run the OtoDock installer on it again.")
    assert refuse(_paired(port=0, ok=False, why="socks")) == off + " Its proxy is a SOCKS proxy, which OtoDock cannot use."
    # A machine still in its update starts no session (``machine_away``): a
    # placement that names it reads the update's one sentence.
    assert refuse(_paired(port=0, why="installing")) == "Laptop is finishing its update. Try again in a minute."
    # A machine with no name inside a sentence.
    assert refuse(_paired(port=0, label="")) == (
        "Tools that need a key are off on this computer. Restart OtoDock on that computer.")
    # Windows and macOS run the exit: nothing refuses them here.
    assert refuse(_paired(os="windows")) == "" and refuse(_paired(os="darwin")) == ""


# --- the placeholders on a machine ---------------------------------------------------


@pytest.mark.parametrize("where", [_paired(), _paired(kind=placement.KIND_ADMIN_REMOTE), _paired(os="windows"),
                                   _paired(os="darwin")], ids=["user", "admin", "windows", "darwin"])
def test_a_paired_computers_bundle_holds_the_placeholder_and_the_machines_variables(no_proxy_set, monkeypatch, where):
    _assembly(monkeypatch, [_music()], env_by_mcp={"music": {"ELEVENLABS_API_KEY": SECRET_VALUE}}, tmp_path=no_proxy_set)
    sid, bundles = _build(where)
    bundle = bundles["music"]
    (cred,) = bundle.exit
    assert bundle.env["ELEVENLABS_API_KEY"] == cred.placeholder and cred.placeholder.startswith("oto-exit-")
    assert cred.value == SECRET_VALUE and SECRET_VALUE not in str(bundle.env)
    # The machine's variables beside it, for this tool's process alone: the
    # machine's exit with the session's secret, the machine's bundle.
    secret = credential_exit.machine_secret(sid)
    assert secret and bundle.env["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:41111"
    assert bundle.env["SSL_CERT_FILE"] == f"{OTODOCK_DIR}/credential-exit/bundle.pem"
    assert bundle.env["GIT_SSL_CAINFO"] == bundle.env["NODE_EXTRA_CA_CERTS"] == bundle.env["SSL_CERT_FILE"]
    assert bundle.env["NO_PROXY"] == ("127.0.0.1,localhost,::1,.anthropic.com,.claude.com,.openai.com,.chatgpt.com,"
                                      "anthropic.com,claude.com,openai.com,chatgpt.com")
    # The payload's block: the secret, the hash the pushes are keyed by, the entry, no value.
    block = credential_exit.machine_block(sid, bundles, "hash-1")
    assert block == {"secret": secret, "token_hash": "hash-1", "carves": [],
                     "entries": [{"placeholder": cred.placeholder, "header": "xi-api-key", "prefix": "",
                                  "key": "ELEVENLABS_API_KEY", "hosts": ["api.elevenlabs.io"]}]}
    assert SECRET_VALUE not in str(block)
    # gh's process gets the same variables on this computer.
    assert credential_exit.cli_process_env(sid) == {k: v for k, v in bundle.env.items() if k in credential_exit.ENV_NAMES}
    # A second build of the same id (a terminal re-opened) keeps the secret.
    _sid, again = _build(where, session_id=sid)
    assert again["music"].env["HTTPS_PROXY"] == bundle.env["HTTPS_PROXY"]


def test_on_the_compute_machine_the_bundle_holds_the_placeholder_and_no_machine_variables(no_proxy_set, monkeypatch):
    """The compute session's variables are the sandbox's (the plan env):
    the bundle carries the placeholder alone."""
    _assembly(monkeypatch, [_music()], env_by_mcp={"music": {"ELEVENLABS_API_KEY": SECRET_VALUE}}, tmp_path=no_proxy_set)
    sid, bundles = _build(_compute())
    bundle = bundles["music"]
    (cred,) = bundle.exit
    assert bundle.env == {"ELEVENLABS_API_KEY": cred.placeholder}
    assert credential_exit.machine_env(sid, _compute()) == {}
    # No secret until the compute attach mints it: no block yet.
    assert credential_exit.machine_block(sid, bundles, "h") is None


@pytest.mark.parametrize("where", [_paired(port=0), _paired(ok=False), _paired(otodock=""),
                                   _paired(port=0, why="installing"), _compute(port=0)],
                         ids=["paired-no-port", "paired-down", "paired-no-dir", "paired-installing",
                              "compute-no-port"])
def test_a_machine_the_rule_refuses_leaves_out_every_tool_that_needs_a_key(no_proxy_set, monkeypatch, where):
    """No value is handed instead: the tool that holds a declared key is left
    out with the rule's sentence, and nothing of it reaches the session."""
    from services.mcp import mcp_registry
    _assembly(monkeypatch, [_music()], env_by_mcp={"music": {"ELEVENLABS_API_KEY": SECRET_VALUE}}, tmp_path=no_proxy_set)
    sid = str(uuid.uuid4())
    path, flat_env, excluded, bundles, _ = mcp_registry.build_session_mcp_config(
        "agent", "u-1", placement=where, session_id=sid)
    assert "music" not in bundles
    assert excluded["music"] == mcp_session_config.exit_machine_refusal(where)
    assert SECRET_VALUE not in str(flat_env) and (path is None or SECRET_VALUE not in path.read_text())
    assert credential_exit.machine_secret(sid) == "" and credential_exit.machine_block(sid, bundles, "h") is None


def test_on_a_refused_machine_the_github_tool_is_left_out_where_it_would_hold_a_placeholder(no_proxy_set, monkeypatch):
    """git and gh hold a placeholder on compute and on a Linux computer: a
    refused one leaves the tool out. On macOS and Windows they get the
    token per command, which needs no exit, so the tool stays."""
    from services.mcp import mcp_registry
    _assembly(monkeypatch, [_FakeManifest("github-mcp")], env_by_mcp={}, tmp_path=no_proxy_set,
              cli_credentials={"github-mcp": _github_cli(no_proxy_set)})
    for where in (_paired(port=0), _compute(port=0)):
        _p, _e, excluded, bundles, _ = mcp_registry.build_session_mcp_config("agent", "u-1", placement=where)
        assert "github-mcp" not in bundles and excluded["github-mcp"].startswith("Tools that need a key are off")
    for where in (_paired(port=0, os="darwin"), _paired(port=0, os="windows")):
        _p, _e, excluded, bundles, _ = mcp_registry.build_session_mcp_config("agent", "u-1", placement=where)
        assert "github-mcp" not in excluded and bundles["github-mcp"].cli.per_command is True


# --- the admitted hosts and the probe host ---------------------------------------------


def test_the_block_carries_the_hosts_the_allowlist_admits_and_drops_an_entry_with_none(no_proxy_set, monkeypatch):
    manifest = _exit_manifest("video", [
        {"key": "RUNWAY_API_KEY", "header": "Authorization", "prefix": "Bearer ", "hosts": ["api.dev.runwayml.com"]},
        {"key": "FAL_API_KEY", "header": "Authorization", "prefix": "Key ", "hosts": ["rest.fal.ai", "*.fal.run"]},
    ])
    _assembly(monkeypatch, [manifest], env_by_mcp={"video": {"RUNWAY_API_KEY": "r-1", "FAL_API_KEY": "f-1"}},
              tmp_path=no_proxy_set)
    sid, bundles = _build(_paired())
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: host in ("*.fal.run",))
    block = credential_exit.machine_block(sid, bundles, "h")
    assert [e["hosts"] for e in block["entries"]] == [["*.fal.run"]]
    fal = next(c for c in bundles["video"].exit if c.env_name == "FAL_API_KEY")
    assert credential_exit.admitted_hosts(fal) == ("*.fal.run",)
    assert credential_exit.probe_host(fal) == "exit.fal.run"
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: host == "rest.fal.ai")
    assert credential_exit.probe_host(fal) == "rest.fal.ai"
    # No host admitted yet (a wildcard awaiting an admin's Allow): the tools
    # still hold placeholders and the machine's exit variables, so the
    # session gets its door with no entry, a plain tunnel, and never a 407.
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: False)
    assert credential_exit.probe_host(fal) == ""
    block = credential_exit.machine_block(sid, bundles, "h")
    assert block["entries"] == [] and block["secret"] == credential_exit.machine_secret(sid)


def test_a_paired_session_whose_tools_hold_no_placeholder_gets_no_door(no_proxy_set, monkeypatch):
    """A paired computer's variables ride only the processes that hold a
    placeholder: a session with none has nothing aimed at the exit, so no
    door is opened for it."""
    _assembly(monkeypatch, [], env_by_mcp={}, tmp_path=no_proxy_set)
    sid, bundles = _build(_paired())
    # The machine's record exists (a secret held), so the None below is the
    # placeholder rule's answer and not a missing record's.
    assert credential_exit.machine_env(sid, _paired()) and credential_exit.machine_secret(sid)
    assert credential_exit.machine_block(sid, bundles, "h") is None


# --- a tool's account token by reference -------------------------------------------------


def _workspace(declared: list[str]):
    fm = _FakeManifest("workspace", oauth={"mcp_env_injection": ["GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_TOKEN_2"]})
    fm.credentials.exit = [{"key": k, "header": "Authorization", "prefix": "Bearer ",
                            "hosts": ["gmail.googleapis.com"]} for k in declared]
    return fm


def _token(tmp_path, renews=True) -> TokenCredential:
    return TokenCredential(env_names=("GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_TOKEN_2"),
                           token_ref=TokenRef(str(tmp_path), "alice@example.com"), manifest="workspace", renews=renews)


@pytest.mark.parametrize("where", [placement.LOCAL_PLACEMENT, _paired(), _compute()], ids=["local", "paired", "compute"])
def test_a_tools_account_token_goes_by_reference_when_every_name_is_declared(no_proxy_set, monkeypatch, where):
    env = {"GOOGLE_OAUTH_ACCESS_TOKEN": "ya29.tok", "GOOGLE_TOKEN_2": "ya29.tok",
           mcp_gateway.TOKEN_EXPIRES_ENV: "2030-01-01T00:00:00Z", mcp_gateway.TOKEN_ACCOUNT_ENV: "alice@example.com"}
    _assembly(monkeypatch, [_workspace(["GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_TOKEN_2"])],
              env_by_mcp={"workspace": env}, tmp_path=no_proxy_set, token_credentials={"workspace": _token(no_proxy_set)})
    _sid, bundles = _build(where)
    bundle = bundles["workspace"]
    assert len(bundle.exit) == 2 and all(c.token_ref is not None and c.value == "" for c in bundle.exit)
    by_name = {c.env_name: c for c in bundle.exit}
    assert bundle.env["GOOGLE_OAUTH_ACCESS_TOKEN"] == by_name["GOOGLE_OAUTH_ACCESS_TOKEN"].placeholder
    assert bundle.env["GOOGLE_TOKEN_2"] == by_name["GOOGLE_TOKEN_2"].placeholder
    assert "ya29.tok" not in str(bundle.env)
    # No expiry rides a placeholder that never expires, and no renewal is minted.
    assert mcp_gateway.TOKEN_EXPIRES_ENV not in bundle.env and bundle.env[mcp_gateway.TOKEN_ACCOUNT_ENV] == "alice@example.com"
    assert bundle.token is not None and bundle.token.renews is False
    assert mcp_gateway.TOKEN_RENEWAL_ENV not in bundle.env


def test_a_token_with_one_name_undeclared_stays_by_value(no_proxy_set, monkeypatch):
    env = {"GOOGLE_OAUTH_ACCESS_TOKEN": "ya29.tok", "GOOGLE_TOKEN_2": "ya29.tok",
           mcp_gateway.TOKEN_EXPIRES_ENV: "2030-01-01T00:00:00Z"}
    _assembly(monkeypatch, [_workspace(["GOOGLE_OAUTH_ACCESS_TOKEN"])], env_by_mcp={"workspace": env},
              tmp_path=no_proxy_set, token_credentials={"workspace": _token(no_proxy_set)})
    _sid, bundles = _build(placement.LOCAL_PLACEMENT)
    bundle = bundles["workspace"]
    assert bundle.exit == () and bundle.env["GOOGLE_OAUTH_ACCESS_TOKEN"] == "ya29.tok"
    assert bundle.env[mcp_gateway.TOKEN_EXPIRES_ENV] == "2030-01-01T00:00:00Z"
    assert bundle.token is not None and bundle.token.renews is True


def test_the_resolver_withholds_a_renewing_token_unless_the_exit_carries_it(monkeypatch):
    """The personal-machine sentence reads the build's answer: a tool whose
    token the exit holds by reference renews nothing in the session."""
    manifest = _workspace(["GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_TOKEN_2"])
    manifest.credentials.type = "per_user"
    manifest.credentials.api_key_header = None
    manifest.credentials.label = "Google Workspace"
    manifest.instances = None
    binding = SimpleNamespace(account_owner_sub="sub-1", account_label="alice@example.com")
    monkeypatch.setattr(credential_resolver, "_picked_service_binding", lambda *a, **k: binding)
    monkeypatch.setattr(credential_resolver, "_renews_in_session", lambda m, b: True)
    assert credential_resolver.exit_declares_token(manifest) is True
    assert credential_resolver.personal_withheld_reason(manifest, "agent", "sub-1") == (
        "Google Workspace signs in with an account that stays on the platform")
    assert credential_resolver.personal_withheld_reason(manifest, "agent", "sub-1", exit_carries=True) == ""
    half = _workspace(["GOOGLE_OAUTH_ACCESS_TOKEN"])
    half.credentials.type, half.credentials.api_key_header, half.instances = "per_user", None, None
    half.credentials.label = "Google Workspace"
    assert credential_resolver.exit_declares_token(half) is False
    assert credential_resolver.personal_withheld_reason(half, "agent", "sub-1", exit_carries=True) != ""


# --- the command-line credential per OS ------------------------------------------------------


def _github_cli(tmp_path) -> CliCredential:
    return CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"), env_names=("GH_TOKEN",),
                         manifest="github-mcp")


@pytest.mark.parametrize("where, through", [
    (placement.LOCAL_PLACEMENT, True), (_compute(), True), (_paired(os="linux"), True),
    (_paired(os=""), True), (_paired(os="plan9"), True),
    (_paired(os="darwin"), False), (_paired(os="windows"), False),
], ids=["local", "compute", "linux", "no-system-named", "another-word", "darwin", "windows"])
def test_git_and_gh_get_a_placeholder_where_their_tools_read_the_trust_variables(no_proxy_set, monkeypatch, where, through):
    _assembly(monkeypatch, [_FakeManifest("github-mcp")], env_by_mcp={}, tmp_path=no_proxy_set,
              cli_credentials={"github-mcp": _github_cli(no_proxy_set)})
    sid, bundles = _build(where)
    bundle = bundles["github-mcp"]
    if through:
        (cred,) = bundle.exit
        assert cred.cli == _github_cli(no_proxy_set) and cred.hosts == ("github.com", "*.github.com")
        assert bundle.cli.per_command is False
    else:
        # gh, a Go program, reads no trust variable on macOS or Windows:
        # it gets the token itself there, per command, and the credential
        # says so, as the hook reads it.
        assert bundle.exit == ()
        assert bundle.cli.per_command is True
        assert credential_exit.machine_secret(sid) == ""


def test_gits_entries_name_the_exit_on_a_linux_computer_alone(no_proxy_set, monkeypatch):
    asked: list[dict] = []

    def resolve(*a, **k):
        asked.append(k)
        return SimpleNamespace(env_by_mcp={}, env_vars={}, excluded_mcps=set(), exclusion_reasons={},
                               available_mcps=set(), secret_keys=set(), bash_env_keys=set(), cli_credentials={},
                               token_credentials={}, accounts={}, agent_accounts={})
    _stub_assembly(monkeypatch, [_FakeManifest("github-mcp")], env_by_mcp={}, tmp_path=no_proxy_set)
    monkeypatch.setattr(credential_resolver, "resolve_credentials", resolve)
    sid, _bundles = _build(_paired(os="linux"))
    assert asked[-1]["exit_carries"] is True
    git_exit = asked[-1]["git_exit"]
    assert git_exit["HTTPS_PROXY"].endswith("@127.0.0.1:41111") and credential_exit.machine_secret(sid) in git_exit["HTTPS_PROXY"]
    for where in (_paired(os="windows"), _paired(os="darwin"), _compute(), placement.LOCAL_PLACEMENT):
        _build(where)
        assert asked[-1]["git_exit"] is None
    _build(_paired(port=0))
    assert asked[-1]["exit_carries"] is False and asked[-1]["git_exit"] is None
    env = credential_resolver.git_helper_env("github.com", proxy="http://oto:s@127.0.0.1:41111", ca_info="/b.pem")
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["http.https://github.com/.proxy"] == "http://oto:s@127.0.0.1:41111"
    assert pairs["http.https://github.com/.sslCAInfo"] == "/b.pem"
    plain = credential_resolver.git_helper_env("github.com")
    assert all("http.https" not in plain[f"GIT_CONFIG_KEY_{i}"] for i in range(int(plain["GIT_CONFIG_COUNT"])))


# --- the compute attach -------------------------------------------------------------------


def _builder(*carves: str):
    return SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=tuple(carves)), exit_port=0, ssh_agent="")


@pytest.mark.asyncio
async def test_the_compute_attach_names_the_splice_the_sandbox_bundle_and_the_secret(no_proxy_set):
    from core.sandbox.sandbox_render import EXIT_BUNDLE_PATH
    builder, env, sid = _builder("10.0.0.5"), {"PATH": "/bin"}, str(uuid.uuid4())
    cfg = Path(no_proxy_set) / "cfg"
    cfg.mkdir()
    await credential_exit.attach_sandbox(sid, builder, env, cfg, sandbox_config_dir="/workspace/.claude",
                                   direct_hosts=("file-tools",), machine=_compute())
    # The splice's port inside the namespace, not the machine's listener
    # (the render puts the two together, ``RenderHost.exit_target_port``).
    assert builder.exit_port == 41443
    secret = credential_exit.machine_secret(sid)
    assert secret and env["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:41443"
    assert env["SSL_CERT_FILE"] == EXIT_BUNDLE_PATH and "10.0.0.5" in env["NO_PROXY"] and "file-tools" in env["NO_PROXY"]
    # Each engine's host in both forms: a client that reads a bare name as
    # that one host keeps the engines' subdomains direct by the dotted one.
    for engine in credential_exit.ENGINE_HOSTS:
        assert {engine, f".{engine}"} <= set(env["NO_PROXY"].split(",")), engine
    assert credential_exit.env_of(sid) == {k: v for k, v in env.items() if k != "PATH"}
    # Nothing is written into the config dir: the machine's render binds its own bundle.
    assert list(cfg.iterdir()) == []
    # The block for this session carries the carves for the machine's dial rule.
    from core.credentials.mcp_broker import SecretBundle
    from core.credentials.mcp_gateway import ExitCredential
    cred = ExitCredential(placeholder=credential_exit.new_placeholder(), header="xi-api-key", hosts=("api.elevenlabs.io",),
                          allowlist_key="music", manifest="music", env_name="ELEVENLABS_API_KEY", value="v")
    block = credential_exit.machine_block(sid, {"music": SecretBundle(env={}, exit=(cred,))}, "h")
    assert block["carves"] == ["10.0.0.5"] and block["secret"] == secret
    # gh's variables ride the sandbox env there, not the hook's answer.
    assert credential_exit.cli_process_env(sid) == {}


@pytest.mark.parametrize("where", [_compute(port=0), _compute(ok=False)])
@pytest.mark.asyncio
async def test_the_compute_attach_leaves_a_refused_machine_a_description_with_no_exit(no_proxy_set, where):
    builder, env, sid = _builder(), {}, str(uuid.uuid4())
    await credential_exit.attach_sandbox(sid, builder, env, no_proxy_set, sandbox_config_dir="/workspace/.claude", machine=where)
    assert builder.exit_port == 0 and env == {} and credential_exit.machine_secret(sid) == ""


def test_the_command_line_entry_streams_and_is_judged_only_under_a_narrowed_reach(no_proxy_set, tmp_path):
    """The door's entry for git and gh says a large body passes as it
    arrives; it says the platform is asked about each request only when the
    session's reach narrows, so with nothing chosen a machine's exit asks
    nothing. A tool's entry carries neither key."""
    from core.credentials.mcp_broker import SecretBundle
    from core.credentials.mcp_gateway import ExitCredential

    def block_for(reach):
        sid = str(uuid.uuid4())
        credential_exit._sessions[sid] = credential_exit._Session(secret="s")
        cli = CliCredential(host="github.com", token_ref=TokenRef(str(tmp_path), "acct"), reach=reach)
        git = ExitCredential(placeholder=credential_exit.new_placeholder(), header="Authorization",
                             hosts=("github.com", "*.github.com"), allowlist_key="github-mcp",
                             manifest="github-mcp", cli=cli)
        tool = ExitCredential(placeholder=credential_exit.new_placeholder(), header="xi-api-key",
                              hosts=("api.elevenlabs.io",), allowlist_key="music", manifest="music",
                              env_name="ELEVENLABS_API_KEY", value="v")
        try:
            block = credential_exit.machine_block(
                sid, {"github-mcp": SecretBundle(cli=cli, exit=(git,)), "music": SecretBundle(exit=(tool,))}, "h")
        finally:
            credential_exit.forget(sid)
        by_key = {e["key"]: e for e in block["entries"]}
        return by_key["github-mcp"], by_key["ELEVENLABS_API_KEY"], git, tool

    for reach in (None, {"repositories": [], "read_only": False}):
        git_entry, tool_entry, git, tool = block_for(reach)
        assert git_entry["streams"] is True and "judged" not in git_entry
        assert git.streams is True and git.judged is False
        assert "streams" not in tool_entry and "judged" not in tool_entry
        assert tool.streams is False and tool.judged is False
    for reach in ({"repositories": ["acme/app"], "read_only": False}, {"repositories": [], "read_only": True}):
        git_entry, _tool_entry, git, _tool = block_for(reach)
        assert git_entry["streams"] is True and git_entry["judged"] is True and git.judged is True
    # Neither is stored: a session taken back from its descriptor has both again.
    assert "streams" not in git.descriptor() and "judged" not in git.descriptor()


def test_a_tools_entry_that_asks_for_the_larger_path_says_so_to_a_machine_and_keeps_it_after_a_restart(no_proxy_set):
    """A manifest entry's ``streams`` rides the door's entry as the command
    line's does, and the descriptor keeps it: a session taken back after a
    restart does not meet the body cap again."""
    from core.credentials.mcp_broker import SecretBundle
    from core.credentials.mcp_gateway import ExitCredential
    sid = str(uuid.uuid4())
    credential_exit._sessions[sid] = credential_exit._Session(secret="s")

    def tool(key, **more):
        return ExitCredential(placeholder=credential_exit.new_placeholder(), header="x-goog-api-key",
                              hosts=("generativelanguage.googleapis.com",), allowlist_key="pictures",
                              manifest="pictures", env_name=key, value="v", **more)

    large, small = tool("LARGE_KEY", streams_declared=True), tool("SMALL_KEY")
    try:
        block = credential_exit.machine_block(sid, {"pictures": SecretBundle(exit=(large, small))}, "h")
    finally:
        credential_exit.forget(sid)
    by_key = {e["key"]: e for e in block["entries"]}
    assert by_key["LARGE_KEY"]["streams"] is True and "judged" not in by_key["LARGE_KEY"]
    assert "streams" not in by_key["SMALL_KEY"]
    back = ExitCredential.from_descriptor(large.descriptor())
    assert back.streams is True and back.judged is False and back.value == ""
    assert ExitCredential.from_descriptor(small.descriptor()).streams is False
    # Only a plain true counts in a descriptor, as in a manifest.
    assert ExitCredential.from_descriptor({**large.descriptor(), "streams_declared": "yes"}).streams is False
