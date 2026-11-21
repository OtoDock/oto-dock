"""A tool's build: what one step may read and write (the render's build
description, ``core/sandbox/sandbox_render.py``, which a compute machine
renders for its own builds), what of the platform's network settings those
builds get (``mcp_egress``), and that nothing on the platform starts a build
but the bundled tools' pass (``mcp_venv_bootstrap``)."""

import ast
import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

import config
from core.sandbox import sandbox_render
from services.mcp import mcp_egress, mcp_installer

PROXY = Path(__file__).resolve().parents[2]


@pytest.fixture
def mcps_root(tmp_path, monkeypatch):
    """A tools root with one community tool (its keys and a declared data
    folder), a second tool and the bundled interpreter folder."""
    root = tmp_path / "mcps"
    tool = root / "community" / "demo"
    (tool / "keys").mkdir(parents=True)
    (tool / "state").mkdir()
    (tool / "manifest.json").write_text(json.dumps({"name": "demo", "data_dirs": {"db": "state/"}}))
    (root / "community" / "other").mkdir()
    (root / "community" / "other" / "secret.txt").write_text("other tool")
    (root / "custom").mkdir()
    (root / ".uv-python").mkdir()
    monkeypatch.setattr(config, "MCPS_DIR", root)
    return root


def _binds(argv: list[str]) -> list[tuple[str, str, str]]:
    """``(flag, source, dest)`` of every bind bwrap is given."""
    out = []
    for i, arg in enumerate(argv):
        if arg in ("--bind", "--ro-bind"):
            out.append((arg, argv[i + 1], argv[i + 2]))
    return out


def _writable(argv):
    return {dest for flag, _, dest in _binds(argv) if flag == "--bind"}


def _host(mcps_root: Path, tmp_path: Path) -> sandbox_render.RenderHost:
    """A host that renders the build descriptions it made, as a compute
    machine does: its tools root, its empty mask folder, the launcher."""
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    return sandbox_render.RenderHost(
        agents_root=str(tmp_path / "agents"), mcps_root=str(mcps_root.resolve()), empty_dir=str(empty),
        launcher=str(PROXY / "scripts" / "oto-sandbox-net"), uid=os.getuid(), gid=os.getgid(),
        allow_abs=True)


def _command(mcps_root, step, tmp_path, cmd=None, env=None):
    """The argv that runs ``cmd`` as ``step`` of the demo tool's build and
    the step's environment, made by the render alone: the outputs and the
    scratch folders (``prepare_build_step``), the description
    (``build_description``), the command (``render``)."""
    tool = (mcps_root / "community" / "demo").resolve()
    root = mcps_root.resolve()
    step_dir = tmp_path / f"scratch-{step}"
    step_dir.mkdir()
    cmd = cmd or ["/usr/bin/true"]
    step_env = sandbox_render.prepare_build_step(
        str(tool), step, str(step_dir), env or {"PATH": os.environ.get("PATH", "")})
    own = mcp_installer.uv_python_dir_for(tool)
    own.mkdir(exist_ok=True)
    desc = sandbox_render.build_description(
        str(tool), step, mcps_root=str(root), scratch=str(step_dir),
        interpreter_dirs=tuple(str(d) for d in mcp_installer.interpreter_dirs(root)),
        own_interpreter_dir=str(own), data_dirs=sandbox_render.build_data_dirs(str(tool)),
        ro_paths=sandbox_render.build_ro_paths(cmd, step, step_env))
    return sandbox_render.render(desc, _host(mcps_root, tmp_path), list(cmd)), step_env


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


class TestStepTable:
    def test_a_pip_step_writes_only_its_venv_and_its_scratch(self, mcps_root, tmp_path):
        argv, _ = _command(mcps_root, "pip", tmp_path)
        tool = (mcps_root / "community" / "demo").resolve()
        scratch = (tmp_path / "scratch-pip").resolve()
        assert _writable(argv) == {str(tool / "venv"), str(scratch)}
        sources = [src for _, src, _ in _binds(argv)]
        assert str(tool) in sources
        assert not [s for s in sources if "other" in Path(s).parts]
        # The tool's keys and its declared data folder show the empty folder.
        masked = {dest for flag, src, dest in _binds(argv)
                  if flag == "--ro-bind" and Path(dest).parent == tool and src != dest}
        assert masked == {str(tool / "keys"), str(tool / "state")}

    def test_a_step_binds_its_tool_its_scratch_the_interpreters_and_the_system_alone(self, mcps_root, tmp_path):
        """Every source a step is given, whatever its kind: nothing else of
        the host that renders it. No folder beside the tools root (an
        install's ``config.env``, its sessions, its agents), no other tool,
        no Docker socket."""
        root = mcps_root.resolve()
        system = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/snap/bin", "/etc")
        for step in sandbox_render.BUILD_OUTPUTS:
            # A program in a home folder (node under a version manager) would
            # be bound read-only too: this host's system folders hold them all.
            argv, _ = _command(mcps_root, step, tmp_path, env={"PATH": "/usr/bin:/bin"})
            own = (str(root / "community" / "demo"), str(root / ".uv-python"),
                   str(root / "community" / ".uv-python"), str(tmp_path / f"scratch-{step}"),
                   str(tmp_path / "empty"))
            sources = [src for _, src, _ in _binds(argv)]
            assert str(root / "community" / "demo") in sources
            assert [src for src in sources if not _within(src, own + system)] == [], step

    def test_only_a_venv_step_writes_its_interpreter_folder_and_it_starts_in_scratch(self, mcps_root, tmp_path):
        venv_argv, _ = _command(mcps_root, "venv", tmp_path)
        own = str((mcps_root / "community" / ".uv-python").resolve())
        bundled = str((mcps_root / ".uv-python").resolve())
        assert own in _writable(venv_argv)
        assert bundled not in _writable(venv_argv)
        chdir = venv_argv[venv_argv.index("--chdir") + 1]
        assert chdir == str((tmp_path / "scratch-venv").resolve())
        pip_argv, _ = _command(mcps_root, "pip", tmp_path)
        assert own not in _writable(pip_argv)
        assert pip_argv[pip_argv.index("--chdir") + 1] == str((mcps_root / "community" / "demo").resolve())

    def test_an_npm_step_writes_node_modules_and_its_lock(self, mcps_root, tmp_path):
        argv, _ = _command(mcps_root, "npm", tmp_path)
        tool = (mcps_root / "community" / "demo").resolve()
        assert {str(tool / "node_modules"), str(tool / "package-lock.json")} <= _writable(argv)
        assert (tool / "package-lock.json").is_file()

    def test_no_port_is_forwarded_and_the_sources_are_held_below_the_tools_root(self, mcps_root, tmp_path):
        argv, _ = _command(mcps_root, "pip", tmp_path)
        head = argv[:argv.index("bwrap")]
        assert "--forward" not in head and "--block-private" in head
        assert head[head.index("--beneath") + 1] == str(mcps_root.resolve())

    def test_a_step_gets_its_own_home_temporary_folder_and_caches(self, mcps_root, tmp_path):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/home/x", "PWD": "/somewhere",
               "UV_CACHE_DIR": "/home/x/.cache/uv", "npm_config_cache": "/home/x/.npm"}
        _, step_env = _command(mcps_root, "npm", tmp_path, env=env)
        scratch = str(tmp_path / "scratch-npm")
        for name in ("HOME", "TMPDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "XDG_CACHE_HOME"):
            assert step_env[name].startswith(scratch), name
        assert "PWD" not in step_env and "npm_config_cache" not in step_env


class TestEgress:
    @pytest.fixture
    def targets(self, monkeypatch):
        t = mcp_egress._PlatformTargets(compose=False, networks=(), addresses=frozenset({"10.0.0.5"}),
                                        ports=frozenset({8400}))
        monkeypatch.setattr(mcp_egress, "_platform_targets", lambda: t)
        monkeypatch.setattr("core.config.deployment.in_docker_compose", lambda: False)
        return t

    def test_a_private_proxy_is_carved_and_a_platform_address_never(self, targets):
        assert mcp_egress.platform_build_carves({"HTTPS_PROXY": "http://10.9.9.9:3128"}) == ((), ("10.9.9.9",))
        assert mcp_egress.platform_build_carves({"https_proxy": "http://10.0.0.5:3128"}) == ((), ())
        assert mcp_egress.platform_build_carves({"UV_INDEX_URL": "https://8.8.8.8/simple"}) == ((), ())
        assert mcp_egress.platform_build_carves({"HTTPS_PROXY": "http://169.254.169.254"}) == ((), ())

    def test_a_proxy_on_this_hosts_loopback_is_spliced_but_never_a_platform_port(self, targets, monkeypatch):
        assert mcp_egress.platform_build_carves({"HTTP_PROXY": "http://127.0.0.1:3128"}) == (("3128",), ())
        assert mcp_egress.platform_build_carves({"HTTP_PROXY": "http://127.0.0.1:8400"}) == ((), ())
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 41234)
        assert mcp_egress.platform_build_carves({"HTTP_PROXY": "http://127.0.0.1:41234"}) == ((), ())
        monkeypatch.setattr("core.config.deployment.in_docker_compose", lambda: True)
        assert mcp_egress.platform_build_carves({"HTTP_PROXY": "http://127.0.0.1:3128"}) == ((), ())

    @pytest.fixture
    def no_settings(self, monkeypatch):
        for name in (*sandbox_render.BUILD_EGRESS_NAMES, *sandbox_render.BUILD_SETTING_NAMES,
                     *sandbox_render.BUILD_TRUST_NAMES):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)

    def test_the_block_carries_the_platforms_names_files_and_carves_alone(
            self, targets, no_settings, monkeypatch, tmp_path):
        """What the compute machine's builds get of the platform: the proxy
        and index names, the trust file's bytes, the carve the platform's
        own rule gives. No other variable, no folder, no platform address."""
        ca = tmp_path / "corp-ca.pem"
        ca.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
        monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
        monkeypatch.setenv("NPM_CONFIG_REGISTRY", "http://10.0.0.5:4873")  # a platform address
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
        monkeypatch.setenv("PROXY_API_KEY", "the-platforms-own-secret")
        block = mcp_egress.build_egress_block()
        assert block["env"] == {"HTTPS_PROXY": "http://10.9.9.9:3128",
                                "NPM_CONFIG_REGISTRY": "http://10.0.0.5:4873"}
        assert [(t["env"], t["name"]) for t in block["trust"]] == [("SSL_CERT_FILE", "corp-ca.pem")]
        assert block["allow_hosts"] == ["10.9.9.9"] and block["forwards"] == []
        assert "the-platforms-own-secret" not in json.dumps(block)
        # A folder with no certificate a TLS library looks up sends none.
        assert sorted(block) == ["allow_hosts", "env", "forwards", "trust"]

    def test_every_setting_the_build_had_on_the_platform_rides_but_a_cache_folder(
            self, targets, no_settings, monkeypatch):
        """An install behind a company proxy that built on the platform
        builds on the compute machine: the hosts that skip the proxy, a
        trusted host, strict SSL off, uv's own settings. The installer's
        allowlist is the list, less the names that are each host's own."""
        from services.mcp import mcp_installer
        settings = {"NO_PROXY": "index.corp,10.0.0.0/8", "PIP_TRUSTED_HOST": "index.corp",
                    "NPM_CONFIG_STRICT_SSL": "false", "UV_NATIVE_TLS": "1", "UV_HTTP_TIMEOUT": "120",
                    "UV_OFFLINE": "0", "UV_PYTHON_DOWNLOADS": "manual",
                    "UV_PYPY_INSTALL_MIRROR": "https://10.9.9.8/pypy"}
        for name, value in {**settings, "UV_CACHE_DIR": "/platform/uv", "PIP_CACHE_DIR": "/platform/pip",
                            "NPM_CONFIG_CACHE": "/platform/npm", "XDG_CACHE_HOME": "/platform/xdg"}.items():
            monkeypatch.setenv(name, value)
        block = mcp_egress.build_egress_block()
        assert block["env"] == settings
        assert block["allow_hosts"] == ["10.9.9.8"]          # the mirror is an endpoint, the rest name none
        # Nothing of the allowlist's network, trust and package names is left behind unnamed.
        own_to_each_host = {"UV_CACHE_DIR", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "XDG_CACHE_HOME"}
        carried = {n.upper() for n in (*sandbox_render.BUILD_EGRESS_NAMES, *sandbox_render.BUILD_SETTING_NAMES,
                                       *sandbox_render.BUILD_TRUST_NAMES)}
        named = {n for n in mcp_installer._INSTALL_ENV_NAMES
                 if n.startswith(("UV_", "PIP_", "NPM_CONFIG_", "SSL_", "XDG_"))
                 or n.endswith(("_PROXY", "_CA_BUNDLE", "_CA_CERTS", "_CAINFO"))}
        assert named - carried == own_to_each_host

    def test_a_certificate_folder_rides_as_the_files_a_library_looks_up(
            self, targets, no_settings, monkeypatch, tmp_path):
        """``SSL_CERT_DIR`` names a folder read by file name: the hashed
        names travel, each link followed to its certificate."""
        import base64
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "Corp_Root.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nroot\n")
        (certs / "0a1b2c3d.0").symlink_to("Corp_Root.pem")
        (certs / "ffffffff.1").write_bytes(b"-----BEGIN CERTIFICATE-----\nother\n")
        (certs / "README").write_text("not a certificate\n")
        (certs / "deadbeef.0").symlink_to("gone.pem")          # a link to nothing is no file
        monkeypatch.setenv("SSL_CERT_DIR", str(certs))
        block = mcp_egress.build_egress_block()
        assert block["env"] == {} and block["trust"] == []
        assert [(f["name"], base64.b64decode(f["data_b64"])) for f in block["cert_dir"]] == [
            ("0a1b2c3d.0", b"-----BEGIN CERTIFICATE-----\nroot\n"),
            ("ffffffff.1", b"-----BEGIN CERTIFICATE-----\nother\n")]

    def test_a_certificate_folder_past_its_bounds_sends_none_of_it(
            self, targets, no_settings, monkeypatch, tmp_path, caplog):
        """A part of a trust store would read as the whole of it."""
        monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
        for i in range(2):
            (tmp_path / f"0000000{i}.0").write_bytes(b"x" * (mcp_egress.TRUST_FILE_LIMIT // 2 + 1))
        assert "cert_dir" not in mcp_egress.build_egress_block() and "is not sent" in caplog.text
        for entry in tmp_path.iterdir():
            entry.unlink()
        monkeypatch.setattr(mcp_egress, "TRUST_DIR_FILES_MAX", 3)
        for i in range(4):
            (tmp_path / f"0000000{i}.0").write_bytes(b"x")
        assert "cert_dir" not in mcp_egress.build_egress_block()
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "no-such-folder"))
        assert "cert_dir" not in mcp_egress.build_egress_block()

    def test_no_platform_address_is_carved_for_the_machines_builds(self, no_settings, monkeypatch):
        """The internal listener, the database and a Docker tool: named in
        the platform's own settings, none is opened to a build."""
        t = mcp_egress._PlatformTargets(compose=False, networks=(),
                                        addresses=frozenset({"10.0.0.6"}), ports=frozenset({8400, 5432}))
        monkeypatch.setattr(mcp_egress, "_platform_targets", lambda: t)
        monkeypatch.setattr("core.config.deployment.in_docker_compose", lambda: False)
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 41234)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:41234")
        monkeypatch.setenv("UV_INDEX_URL", "http://127.0.0.1:5432/simple")
        monkeypatch.setenv("PIP_INDEX_URL", "http://10.0.0.6:8080/simple")
        block = mcp_egress.build_egress_block()
        assert block["forwards"] == [] and block["allow_hosts"] == []

    def test_with_no_setting_there_is_no_block(self, targets, no_settings):
        assert mcp_egress.build_egress_block() is None

    def test_a_trust_file_past_the_limit_or_a_folder_is_not_sent(self, targets, no_settings, monkeypatch, tmp_path):
        big = tmp_path / "big.pem"
        big.write_bytes(b"x" * (mcp_egress.TRUST_FILE_LIMIT + 1))
        monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(big))
        monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path))
        # The name that means a folder never rides as a file, whatever it points at.
        one = tmp_path / "one.pem"
        one.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
        monkeypatch.setenv("SSL_CERT_DIR", str(one))
        block = mcp_egress.build_egress_block()
        assert block["trust"] == [] and "cert_dir" not in block

    def test_the_index_entries_name_their_hosts(self):
        found = sandbox_render.build_endpoints({"UV_INDEX": "corp=https://pypi.corp.lan/simple other=https://x.io",
                                                "NPM_CONFIG_REGISTRY": "https://npm.corp.lan:8443/"})
        assert ("pypi.corp.lan", 443) in found and ("x.io", 443) in found and ("npm.corp.lan", 8443) in found


class TestDescription:
    def test_a_build_description_never_comes_from_the_wire(self):
        desc = sandbox_render.build_description(
            "/m/community/x", "pip", mcps_root="/m", scratch="/s", interpreter_dirs=(),
            own_interpreter_dir="/m/community/.uv-python")
        with pytest.raises(sandbox_render.RenderRefused):
            sandbox_render.SandboxDescription.from_dict(desc.to_dict())

    def test_a_sessions_wire_form_is_unchanged(self):
        desc = sandbox_render.SandboxDescription(
            agent="a", binds=(), mcp_binds=(), mcps_root="/m", extra_binds=(), cwd="/", proxy_port=1,
            forwards=("proxy",))
        assert "kind" not in desc.to_dict()

    def test_only_the_host_that_made_a_build_renders_it(self):
        desc = sandbox_render.build_description(
            "/m/community/x", "pip", mcps_root="/m", scratch="/s", interpreter_dirs=(),
            own_interpreter_dir="/m/community/.uv-python")
        host = sandbox_render.RenderHost(agents_root="/a", mcps_root="/m", empty_dir="/e",
                                         launcher="/l", uid=1000, gid=1000, allow_abs=False)
        with pytest.raises(sandbox_render.RenderRefused):
            sandbox_render.render(desc, host, ["true"])


# ── the surface: nothing else on the platform starts a build ──────────────────

def _proxy_modules():
    for path in PROXY.rglob("*.py"):
        rel = path.relative_to(PROXY).as_posix()
        if rel.startswith(("tests/", "venv/", "docs/")) or "/venv/" in rel:
            continue
        yield rel, ast.parse(path.read_text(), filename=rel)


# The installer's doors, and the bundled tools' build with its two entries.
_BUILD_DOORS = {"install_mcp", "_run_bounded", "run_unconfined",
                "build_bundled_venvs", "ensure_bundled_venvs_at_startup", "build_for_image", "build_in_place"}
_BUILD_PROGRAMS = {"npm", "npx", "uv", "pip", "pip3"}
_OWNERS = {"services/mcp/mcp_venv_bootstrap.py", "services/mcp/mcp_installer.py"}


def _called(node) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_no_module_but_the_bundled_tools_pass_starts_a_build():
    """``install_mcp``, the bounded run and the unconfined run are called by
    the bundled tools' pass and the installer alone; the pass's build and its
    two entries are called by no other module, so nothing the server runs
    reaches them; and no other module builds an argv that starts a package
    manager."""
    found = []
    for rel, tree in _proxy_modules():
        if rel in _OWNERS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called(node) in _BUILD_DOORS:
                found.append(f"{rel}:{node.lineno} calls {_called(node)}")
            if (isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant)
                    and node.elts[0].value in _BUILD_PROGRAMS):
                found.append(f"{rel}:{node.lineno} starts {node.elts[0].value}")
    assert found == []


_UV_PYTHON_OK = {
    # The owner: where each tool's interpreters live, its walks' skip lists
    # and the hash's.
    "proxy/services/mcp/mcp_installer.py",
    # The Direct engine's file tools hide the folder from an agent's listing.
    "proxy/core/layers/direct/files.py",
    # The image build carries the bundled tools' interpreters out of its stage.
    "proxy/services/mcp/mcp_venv_bootstrap.py",
    # The one copy of the community tools into the compute service's volume
    # carries their interpreters across (a shell script imports no helper).
    "scripts/copy-tools-to-compute.sh",
}


def _docstrings(tree: ast.AST) -> set[int]:
    return {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}


def test_the_interpreter_folders_are_spelled_by_one_helper():
    """``mcp_installer.uv_python_dir_for`` and ``interpreter_dirs`` say where
    a tool's interpreters live; a second spelling misses the community
    folder (app buttons and the listing job did, the second plan audit).
    Any string that holds the name counts, in the proxy, the satellite, the
    scripts and the compose files."""
    repo = PROXY.parent
    found = []
    roots = [(PROXY, ("tests/", "venv/", "docs/")), (repo / "satellite", ("tests/", "venv/", "_vendored/"))]
    for base, skip in roots:
        for path in base.rglob("*.py"):
            rel = path.relative_to(base).as_posix()
            if rel.startswith(skip) or "/venv/" in rel or path.relative_to(repo).as_posix() in _UV_PYTHON_OK:
                continue
            tree = ast.parse(path.read_text(), filename=rel)
            docs = _docstrings(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                        and ".uv-python" in node.value):
                    found.append(f"{path.relative_to(repo)}:{node.lineno}")
    for path in [*repo.glob("scripts/*.sh"), *repo.glob("docker-compose*.yml")]:
        rel = path.relative_to(repo).as_posix()
        if rel in _UV_PYTHON_OK:
            continue
        found += [f"{rel}:{n}" for n, line in enumerate(path.read_text().splitlines(), 1)
                  if ".uv-python" in line and not line.lstrip().startswith("#")]
    assert found == []


# Every reader of a file a build wrote (a venv's config, a package's
# metadata, an entry point) goes through ``mcp_installer.read_built``. The
# check: a function that names a build's output and reads a file. These read
# no build output, each for its reason.
_READS_NO_BUILD_OUTPUT = {
    "proxy/api/mcp/local_templates.py:_snapshot_template_dir": "an agent's own package folder, venv skipped",
    "proxy/compute_start.py:check": "starts the image's own satellite, reads no file",
    "proxy/services/mcp/mcp_installer.py:install_mcp": "names the outputs, reads them through the readbacks",
    "proxy/services/mcp/mcp_venv_bootstrap.py:scan_bundled_venvs": "the bundled tools' manifests, in their root",
    "satellite/__main__.py:_check_post_update_state": "the satellite's own files",
    "satellite/config.py:ensure_windows_install_artifacts": "the satellite's own files",
    "satellite/host/tray.py:_load_base_image": "the satellite's own files",
    "satellite/transport/lifecycle_update.py:_self_update_and_restart": "the satellite's own files",
    "satellite/sessions/mcp_install_support.py:reconcile_mcp_runtimes": "a tool's manifest, in its root",
    "satellite/sessions/session_manager.py:sync_mcps": "the incoming tarball",
}
_BUILD_OUTPUT_NAMES = ("pyvenv.cfg", "METADATA", "node_modules", "site-packages", "dist-info")
_BUILD_OUTPUT_PARTS = {"venv", "bin", "Scripts"}
_RAW_READS = {"read_text", "read_bytes", "version", "metadata"}


def test_every_reader_of_a_build_output_reads_it_bounded():
    repo = PROXY.parent
    found = set()
    roots = [(PROXY, ("tests/", "venv/", "docs/")), (repo / "satellite", ("tests/", "venv/", "_vendored/"))]
    for base, skip in roots:
        for path in base.rglob("*.py"):
            rel = path.relative_to(base).as_posix()
            if rel.startswith(skip) or "/venv/" in rel:
                continue
            tree = ast.parse(path.read_text(), filename=rel)
            docs = _docstrings(tree)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == "read_built":
                    continue
                consts = {n.value for n in ast.walk(fn)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs}
                if not (consts & _BUILD_OUTPUT_PARTS or any(m in c for c in consts for m in _BUILD_OUTPUT_NAMES)):
                    continue
                names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "") for n in ast.walk(fn)}
                opens = any(isinstance(n, ast.Call) and _called(n) == "open" for n in ast.walk(fn))
                if (names & _RAW_READS or opens) and "read_built" not in names:
                    found.add(f"{path.relative_to(repo)}:{fn.name}")
    assert found == set(_READS_NO_BUILD_OUTPUT)


# ── live: the render's build description in a sandbox on this host ────────────

_live = pytest.mark.skipif(
    not (shutil.which("pasta") and shutil.which("bwrap") and shutil.which("ip")),
    reason="needs bwrap, pasta and ip")


def _plant(tmp_path):
    secret = tmp_path / "platform"
    (secret / "sessions").mkdir(parents=True)
    (secret / "config.env").write_text("JWT_SECRET=not-for-a-build\n")
    (secret / "sessions" / "token").write_text("session state\n")
    return secret


@_live
@pytest.mark.asyncio
async def test_a_live_build_step_sees_its_tool_and_writes_only_its_outputs(mcps_root, tmp_path):
    """The planted package's own record of what it could see and write."""
    secret = _plant(tmp_path)
    tool = (mcps_root / "community" / "demo").resolve()
    other = (mcps_root / "community" / "other" / "secret.txt").resolve()
    script = (
        f"for f in {secret}/config.env {secret}/sessions/token {other} /var/run/docker.sock "
        f"{config.AGENTS_DIR}; do test -e \"$f\" && echo \"seen $f\"; done; "
        f"for f in {tool}/manifest.json {tool}/new {mcps_root.resolve()}/.uv-python/x "
        f"{mcps_root.resolve()}/community/.uv-python/x {tool}/keys/x; do "
        f"touch \"$f\" 2>/dev/null && echo \"wrote $f\"; done; "
        f"touch {tool}/venv/ok && echo done"
    )
    argv, env = _command(mcps_root, "pip", tmp_path, ["/bin/sh", "-c", script], dict(os.environ))
    rc, out = await mcp_installer._run_bounded(argv, cwd=str(tmp_path / "scratch-pip"), env=env, timeout=60)
    assert rc == 0, out
    assert "seen" not in out and "wrote" not in out, out
    assert out.strip().endswith("done") and (tool / "venv" / "ok").exists()


@_live
@pytest.mark.asyncio
async def test_a_live_venv_step_writes_its_interpreter_folder_alone(mcps_root, tmp_path):
    tool = (mcps_root / "community" / "demo").resolve()
    own = (mcps_root / "community" / ".uv-python").resolve()
    script = f"touch {own}/fetched && echo fetched; touch {tool}/x 2>/dev/null && echo wrote; pwd"
    argv, env = _command(mcps_root, "venv", tmp_path, ["/bin/sh", "-c", script], dict(os.environ))
    step_dir = tmp_path / "scratch-venv"
    rc, out = await mcp_installer._run_bounded(argv, cwd=str(step_dir), env=env, timeout=60)
    assert rc == 0, out
    assert "fetched" in out and "wrote" not in out
    assert out.strip().splitlines()[-1] == str(step_dir.resolve())


@_live
@pytest.mark.asyncio
async def test_a_killed_step_leaves_nothing_running_before_the_rollback(mcps_root, tmp_path):
    """A process that left the step's group (``setsid``) still ends with the
    step's sandbox: by the time the timeout reaches the caller, nothing writes."""
    tool = (mcps_root / "community" / "demo").resolve()
    beat = tool / "venv" / "beat"
    script = f"setsid sh -c 'while :; do echo x >> {beat}; sleep 0.05; done' & sleep 30"
    argv, env = _command(mcps_root, "pip", tmp_path, ["/bin/sh", "-c", script], dict(os.environ))
    with pytest.raises(asyncio.TimeoutError):
        await mcp_installer._run_bounded(argv, cwd=str(tmp_path / "scratch-pip"), env=env, timeout=3)
    size = beat.stat().st_size if beat.exists() else 0
    await asyncio.sleep(0.5)
    assert (beat.stat().st_size if beat.exists() else 0) == size
