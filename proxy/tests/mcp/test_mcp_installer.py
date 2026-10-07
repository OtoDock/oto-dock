"""Tests for the shared MCP installer module (proxy/services/mcp/mcp_installer.py).

Covers the pure pieces — source parsing, system-dep detection, version_hash
stability — without actually running npm/pip. The end-to-end install is
exercised via the existing manual smoke test for the install flow."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.mcp import mcp_installer


class TestParseSource:
    def test_npm_plain(self):
        p = mcp_installer.parse_source("npm:mcp-mail-server@1.1.13")
        assert p is not None
        assert p.registry == "npm"
        assert p.package == "mcp-mail-server"
        assert p.version == "1.1.13"

    def test_npm_scoped(self):
        p = mcp_installer.parse_source("npm:@playwright/mcp@0.0.55")
        assert p is not None
        assert p.registry == "npm"
        assert p.package == "@playwright/mcp"
        assert p.version == "0.0.55"

    def test_pypi(self):
        p = mcp_installer.parse_source("pypi:ha-mcp@6.6.1")
        assert p is not None
        assert p.registry == "pypi"
        assert p.package == "ha-mcp"
        assert p.version == "6.6.1"

    def test_docker(self):
        p = mcp_installer.parse_source("docker:collabora")
        assert p is not None
        assert p.registry == "docker"

    def test_empty(self):
        assert mcp_installer.parse_source("") is None

    def test_unknown_prefix(self):
        assert mcp_installer.parse_source("cargo:foo@1.0") is None

    # ---- Unpinned sources: community node/python MCPs carry no version in the
    # catalog (the upstream registry is the version of record). parse_source
    # returns version="" rather than None so install + detection still work.

    def test_npm_unpinned(self):
        p = mcp_installer.parse_source("npm:mcp-mail-server")
        assert p is not None
        assert (p.registry, p.package, p.version) == ("npm", "mcp-mail-server", "")

    def test_npm_scoped_unpinned(self):
        p = mcp_installer.parse_source("npm:@notionhq/notion-mcp-server")
        assert p is not None
        assert (p.registry, p.package, p.version) == ("npm", "@notionhq/notion-mcp-server", "")

    def test_pypi_unpinned(self):
        p = mcp_installer.parse_source("pypi:workspace-mcp")
        assert p is not None
        assert (p.registry, p.package, p.version) == ("pypi", "workspace-mcp", "")

    def test_empty_package_is_none(self):
        # A prefix with no package is not a valid source.
        assert mcp_installer.parse_source("npm:") is None
        assert mcp_installer.parse_source("pypi:") is None


class TestVersionHash:
    def test_stable(self, tmp_path: Path):
        """Same inputs → same hash."""
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        (tmp_path / "requirements.txt").write_text("foo==1.0\n")
        h1 = mcp_installer.compute_version_hash(tmp_path)
        h2 = mcp_installer.compute_version_hash(tmp_path)
        assert h1 == h2
        assert len(h1) == 16  # first 16 hex chars

    def test_changes_when_manifest_changes(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "manifest.json").write_text('{"name": "x","v":"1"}')
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_includes_patches(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "patches").mkdir()
        (tmp_path / "patches" / "foo.patch").write_text("--- a\n+++ b\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_missing_files_ok(self, tmp_path: Path):
        """No install-relevant files → empty hash of nothing (still stable)."""
        h = mcp_installer.compute_version_hash(tmp_path)
        assert len(h) == 16

    # ---- Source-file hashing (added when the manifest-only hash was
    # missing edits to server.py and causing satellite drift).

    def test_changes_when_top_level_source_changes(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        (tmp_path / "server.py").write_text("print('v1')\n")
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "server.py").write_text("print('v2')\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_changes_when_nested_source_changes(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "helper.py").write_text("x = 1\n")
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "lib" / "helper.py").write_text("x = 2\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_picks_up_all_source_extensions(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        baseline = mcp_installer.compute_version_hash(tmp_path)
        for ext in (".py", ".js", ".mjs", ".ts", ".tsx", ".go", ".rs"):
            f = tmp_path / f"add{ext}"
            f.write_text(f"// content for {ext}\n")
            after = mcp_installer.compute_version_hash(tmp_path)
            assert after != baseline, f"hash didn't change after adding {ext}"
            f.unlink()  # reset

    def test_excludes_pycache(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "compiled.cpython-310.pyc").write_text("bytecode")
        (cache_dir / "noise.py").write_text("noise = 1\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_node_modules(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("module.exports = 1\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_venv(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "venv" / "lib").mkdir(parents=True)
        (tmp_path / "venv" / "lib" / "site.py").write_text("# venv noise\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_dotgit_screenshots_backups(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        for d in (".git", "screenshots", ".backups", "dist", "build"):
            sub = tmp_path / d
            sub.mkdir()
            (sub / "noise.py").write_text(f"# {d}\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_compiled_artifacts(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        (tmp_path / "server.py").write_text("# real source\n")
        before = mcp_installer.compute_version_hash(tmp_path)
        # Compiled artifacts in the same dir as legitimate source.
        (tmp_path / "server.pyc").write_bytes(b"\x00bytecode")
        (tmp_path / "native.so").write_bytes(b"\x7fELF noise")
        (tmp_path / "ext.dylib").write_bytes(b"\xfe\xed\xfa\xce")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_deterministic_irrespective_of_creation_order(self, tmp_path: Path):
        """Creating files in different orders must produce the same hash."""
        (tmp_path / "manifest.json").write_text('{}')
        (tmp_path / "a.py").write_text("a\n")
        (tmp_path / "b.py").write_text("b\n")
        (tmp_path / "c.py").write_text("c\n")
        h1 = mcp_installer.compute_version_hash(tmp_path)
        for f in ("a.py", "b.py", "c.py"):
            (tmp_path / f).unlink()
        # Recreate in reverse order; mtimes differ but the hash is
        # content + path based and should match.
        (tmp_path / "c.py").write_text("c\n")
        (tmp_path / "b.py").write_text("b\n")
        (tmp_path / "a.py").write_text("a\n")
        h2 = mcp_installer.compute_version_hash(tmp_path)
        assert h1 == h2

    def test_skips_double_hash_of_manifest_when_nested_name_collides(self, tmp_path: Path):
        """A nested file named ``manifest.json`` (rare but possible) must not
        accidentally double-hash through the source-walk path."""
        (tmp_path / "manifest.json").write_text('{"v": 1}')
        h_baseline = mcp_installer.compute_version_hash(tmp_path)
        sub = tmp_path / "fixtures"
        sub.mkdir()
        (sub / "manifest.json").write_text('{"fixture": true}')
        h_with_nested = mcp_installer.compute_version_hash(tmp_path)
        # Nested manifest.json is filtered by name from the walker and isn't
        # in _HASH_INPUT_FILES' top-level dir, so it's silently ignored —
        # baseline hash is unchanged. (If a future MCP needs to ship a
        # nested manifest.json as a runtime fixture, we'd revisit this.)
        assert h_baseline == h_with_nested


class TestNodePackageJson:
    """The single canonical serializer used for both the pre-install write and
    the post-readback re-canonicalize — must be byte-identical so the proxy
    (resolved "latest") and a satellite (pinned source) land on the same hash."""

    def test_byte_identical_for_same_inputs(self):
        a = mcp_installer._node_package_json("@scope/x", "1.2.3")
        b = mcp_installer._node_package_json("@scope/x", "1.2.3")
        assert a == b

    def test_is_valid_json_with_expected_shape(self):
        raw = mcp_installer._node_package_json("mcp-mail-server", "latest")
        data = json.loads(raw)
        assert data == {"private": True, "dependencies": {"mcp-mail-server": "latest"}}

    def test_returns_lf_bytes_for_cross_os_hash_stability(self):
        """Must be bytes with LF newlines, written via write_bytes. A
        text-mode write turns LF into CRLF on Windows, drifting
        compute_version_hash and looping the MCP into a reinstall on every
        session against a Linux platform."""
        raw = mcp_installer._node_package_json("@scope/x", "1.2.3")
        assert isinstance(raw, bytes)
        assert b"\r" not in raw
        assert b"\n" in raw

    def test_hash_stable_across_recanonicalize(self, tmp_path: Path):
        """Rewriting package.json with the same resolved version must not
        drift compute_version_hash — this is the exact satellite install
        cycle (extract → rewrite → hash → verify next session)."""
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        pj = tmp_path / "package.json"
        pj.write_bytes(mcp_installer._node_package_json("@scope/x", "1.2.3"))
        before = mcp_installer.compute_version_hash(tmp_path)
        pj.write_bytes(mcp_installer._node_package_json("@scope/x", "1.2.3"))
        assert mcp_installer.compute_version_hash(tmp_path) == before


class TestInstalledVersionReadback:
    def test_node_reads_version(self, tmp_path: Path):
        pkg_dir = tmp_path / "node_modules" / "mcp-mail-server"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.json").write_text('{"name": "mcp-mail-server", "version": "1.2.7"}')
        assert mcp_installer._node_installed_version(tmp_path, "mcp-mail-server") == "1.2.7"

    def test_node_reads_scoped_version(self, tmp_path: Path):
        pkg_dir = tmp_path / "node_modules" / "@notionhq" / "notion-mcp-server"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.json").write_text('{"version": "2.4.0"}')
        got = mcp_installer._node_installed_version(tmp_path, "@notionhq/notion-mcp-server")
        assert got == "2.4.0"

    def test_node_missing_returns_empty(self, tmp_path: Path):
        # No node_modules at all → "" (never raises). The node branch treats an
        # empty readback after a successful install as a failure.
        assert mcp_installer._node_installed_version(tmp_path, "nope") == ""

    def test_python_missing_venv_returns_empty(self, tmp_path: Path):
        # No venv on disk → "" without touching anything.
        assert mcp_installer._python_installed_version(tmp_path / "venv", "ha-mcp") == ""


class TestCheckSystemRequirements:
    def test_empty_requirements_returns_empty(self):
        req = mcp_installer.SystemRequirementsInput()
        assert mcp_installer.check_system_requirements(req) == []

    def test_node_min_reported_when_not_installed(self):
        req = mcp_installer.SystemRequirementsInput(node_min="99.0")
        # Either node isn't installed, or it's < 99 — either way the check must flag it.
        with patch("services.mcp.mcp_installer._node_version", return_value=""):
            missing = mcp_installer.check_system_requirements(req)
            assert any(m.kind == "interpreter" and m.name == "node" for m in missing)

    def test_missing_package_reported(self):
        req = mcp_installer.SystemRequirementsInput(debian=["this-package-does-not-exist"])
        with patch("services.mcp.mcp_installer._detect_os_keys", return_value=["debian"]), \
             patch("services.mcp.mcp_installer._is_package_installed", return_value=False):
            missing = mcp_installer.check_system_requirements(req)
            assert len(missing) == 1
            assert missing[0].kind == "package"
            assert missing[0].name == "this-package-does-not-exist"
            assert "apt install" in missing[0].install_cmd

    def test_installed_package_not_reported(self):
        req = mcp_installer.SystemRequirementsInput(debian=["some-package"])
        with patch("services.mcp.mcp_installer._detect_os_keys", return_value=["debian"]), \
             patch("services.mcp.mcp_installer._is_package_installed", return_value=True):
            missing = mcp_installer.check_system_requirements(req)
            assert missing == []


class TestPinLocalManifest:
    """Pinning the LOCAL manifest after an unpinned install — writes both
    `version` and `server.source`, and the pinned source round-trips through
    parse_source (incl. scoped npm)."""

    def _write_unpinned(self, tmp_path: Path, source: str) -> Path:
        mf = tmp_path / "manifest.json"
        mf.write_text(json.dumps({
            "name": "x", "label": "X", "description": "d", "version": "",
            "category": "community", "server": {"runtime": "node", "source": source},
        }, indent=2))
        return mf

    def test_pins_version_and_source_npm_scoped(self, tmp_path: Path):
        from services.mcp import mcp_updater
        self._write_unpinned(tmp_path, "npm:@notionhq/notion-mcp-server")
        mcp_updater.pin_local_manifest(tmp_path, "npm", "@notionhq/notion-mcp-server", "2.4.0")
        data = json.loads((tmp_path / "manifest.json").read_text())
        assert data["version"] == "2.4.0"
        assert data["server"]["source"] == "npm:@notionhq/notion-mcp-server@2.4.0"
        # The pinned source must parse back to the same package + version.
        p = mcp_installer.parse_source(data["server"]["source"])
        assert (p.registry, p.package, p.version) == ("npm", "@notionhq/notion-mcp-server", "2.4.0")

    def test_pins_pypi(self, tmp_path: Path):
        from services.mcp import mcp_updater
        self._write_unpinned(tmp_path, "pypi:workspace-mcp")
        mcp_updater.pin_local_manifest(tmp_path, "pypi", "workspace-mcp", "1.21.3")
        data = json.loads((tmp_path / "manifest.json").read_text())
        assert data["version"] == "1.21.3"
        assert data["server"]["source"] == "pypi:workspace-mcp@1.21.3"


class TestSelfHash:
    def test_returns_hex(self):
        h = mcp_installer.self_hash()
        assert len(h) == 64  # full sha256 hex
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# The install subprocesses: scrubbed environment, binary-only pypi,
# a clean venv, an in-process version readback, git-applied patches.
# ---------------------------------------------------------------------------

import asyncio
import os
import subprocess

_SECRETS = {"DATABASE_URL": "postgresql://u:p@db/x", "JWT_SECRET": "s3",
            "POSTGRES_PASSWORD": "pw", "NODE_OPTIONS": "--require /tmp/x.js",
            "OTO_MACHINE_SECRET": "m"}
_KEPT = {"PATH": os.environ.get("PATH", "/usr/bin"), "HTTPS_PROXY": "http://proxy:3128",
         "UV_CACHE_DIR": "/tmp/uv-cache", "NPM_CONFIG_REGISTRY": "https://registry.example"}


class _Proc:
    def __init__(self, rc: int, out: bytes = b""):
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return self._out, b""


def _fake_uv(tmp_path: Path) -> str:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    return str(uv)


def _recording_spawn(monkeypatch, script=None, *, passthrough=()):
    """Replace ``create_subprocess_exec`` with a recorder that answers from
    ``script`` (a list of _Proc) and lets ``passthrough`` argv[0] basenames
    run for real."""
    real = asyncio.create_subprocess_exec
    calls: list[dict] = []

    async def _spawn(*argv, **kw):
        calls.append({"argv": [str(a) for a in argv], "env": kw.get("env"), "cwd": kw.get("cwd")})
        if Path(argv[0]).name in passthrough:
            return await real(*argv, **kw)
        return script[len(calls) - 1] if script else _Proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    return calls


def _node_dir(tmp_path: Path, pkg: str = "pkg", version: str = "1.0.0") -> Path:
    mcp_dir = tmp_path / "mcps" / "community" / "node-mcp"
    (mcp_dir / "node_modules" / pkg).mkdir(parents=True)
    (mcp_dir / "node_modules" / pkg / "package.json").write_text(json.dumps({"version": version}))
    return mcp_dir


def _python_dir(tmp_path: Path) -> Path:
    mcp_dir = tmp_path / "mcps" / "community" / "py-mcp"
    mcp_dir.mkdir(parents=True)
    return mcp_dir


class TestInstallEnvironment:
    def test_allowlist_keeps_runtime_and_network_names_only(self, monkeypatch):
        for k, v in {**_SECRETS, **_KEPT}.items():
            monkeypatch.setenv(k, v)
        env = mcp_installer._install_env()
        for k in _SECRETS:
            assert k not in env
        for k in _KEPT:
            assert env[k] == _KEPT[k]
        assert env["UV_NO_CONFIG"] == "1"
        assert env["PIP_CONFIG_FILE"] == os.devnull
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    @pytest.mark.asyncio
    async def test_every_node_subprocess_gets_the_scrubbed_env(self, tmp_path, monkeypatch):
        for k, v in {**_SECRETS, **_KEPT}.items():
            monkeypatch.setenv(k, v)
        mcp_dir = _node_dir(tmp_path)
        (mcp_dir / "patches").mkdir()
        (mcp_dir / "patches" / "pkg+1.0.0.patch").write_text("x")
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(1), _Proc(0, b"Applied patch")])
        monkeypatch.setattr(mcp_installer.shutil, "which", lambda n: "/usr/bin/git" if n == "git" else None)
        r = await mcp_installer.install_mcp(mcp_dir, "node", "npm:pkg@1.0.0")
        assert r.ok, r.log
        assert calls, "no subprocess recorded"
        for c in calls:
            assert c["env"] is not None, c["argv"]
            assert not set(_SECRETS) & set(c["env"]), c["argv"]
            assert c["env"]["PATH"] == _KEPT["PATH"]
        assert not any(c["argv"][0].endswith("npx") for c in calls)
        git_calls = [c for c in calls if c["argv"][0].endswith("git")]
        assert git_calls and all(c["env"]["GIT_CEILING_DIRECTORIES"] == str(mcp_dir.parent) for c in git_calls)

    @pytest.mark.asyncio
    async def test_every_python_subprocess_gets_the_scrubbed_env(self, tmp_path, monkeypatch):
        for k, v in {**_SECRETS, **_KEPT}.items():
            monkeypatch.setenv(k, v)
        mcp_dir = _python_dir(tmp_path)
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(0)])
        r = await mcp_installer.install_mcp(mcp_dir, "python", "pypi:ha-mcp@6.7.0", uv_bin=_fake_uv(tmp_path))
        assert r.ok, r.log
        assert len(calls) == 2  # uv venv, uv pip install: no interpreter readback
        for c in calls:
            assert not set(_SECRETS) & set(c["env"]), c["argv"]
            assert c["env"]["UV_LINK_MODE"] == "copy"
            assert c["env"]["UV_PYTHON_INSTALL_DIR"].endswith(".uv-python")
            assert c["env"]["UV_NO_CONFIG"] == "1"


class TestBinaryOnlyPython:
    @pytest.mark.asyncio
    async def test_pypi_install_is_binary_only(self, tmp_path, monkeypatch):
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(0)])
        await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:unifi-network-mcp@1.0.0",
            uv_bin=_fake_uv(tmp_path), source_build=["antlr4-python3-runtime"],
        )
        pip = calls[1]["argv"]
        assert "--only-binary=:all:" in pip
        assert "--no-binary" not in pip
        assert pip[-1] == "unifi-network-mcp==1.0.0"

    @pytest.mark.asyncio
    async def test_pip_fallback_is_binary_only_and_isolated(self, tmp_path, monkeypatch):
        mcp_dir = _python_dir(tmp_path)
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(0)])
        await mcp_installer.install_mcp(mcp_dir, "python", "pypi:ha-mcp", uv_bin=None)
        pip = calls[1]["argv"]
        assert "--only-binary=:all:" in pip and "--isolated" in pip

    @pytest.mark.asyncio
    async def test_git_and_requirements_installs_are_not_binary_only(self, tmp_path, monkeypatch):
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(0)])
        await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "git+https://host/r.git@v1", uv_bin=_fake_uv(tmp_path),
        )
        assert not any("--only-binary=:all:" in c["argv"] for c in calls)
        mcp_dir = tmp_path / "mcps" / "custom" / "bundled"
        mcp_dir.mkdir(parents=True)
        (mcp_dir / "requirements.txt").write_text("x\n")
        calls[:] = []
        await mcp_installer.install_mcp(mcp_dir, "python", "", uv_bin=_fake_uv(tmp_path))
        assert calls and not any("--only-binary=:all:" in c["argv"] for c in calls)

    @pytest.mark.asyncio
    async def test_pypi_install_recreates_the_venv(self, tmp_path, monkeypatch):
        mcp_dir = _python_dir(tmp_path)
        (mcp_dir / "venv").mkdir()
        (mcp_dir / "venv" / "stale").write_text("x")
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(0)])
        await mcp_installer.install_mcp(mcp_dir, "python", "pypi:ha-mcp@6.7.0", uv_bin=_fake_uv(tmp_path))
        assert calls[0]["argv"][1] == "venv"
        assert not (mcp_dir / "venv" / "stale").exists()


class TestVersionReadback:
    def test_reads_dist_info_in_process(self, tmp_path, monkeypatch):
        venv = tmp_path / "venv"
        site = venv / "lib" / "python3.13" / "site-packages"
        (site / "ha_mcp-6.7.0.dist-info").mkdir(parents=True)
        (site / "ha_mcp-6.7.0.dist-info" / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: ha-mcp\nVersion: 6.7.0\n")
        spawned = []
        monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: spawned.append(a))
        assert mcp_installer._python_installed_version(venv, "ha-mcp") == "6.7.0"
        assert mcp_installer._python_installed_version(venv, "HA_MCP") == "6.7.0"
        assert mcp_installer._python_installed_version(venv, "other") == ""
        assert spawned == []

    def test_reads_the_windows_layout(self, tmp_path):
        site = tmp_path / "venv" / "Lib" / "site-packages"
        (site / "pkg-1.2.dist-info").mkdir(parents=True)
        (site / "pkg-1.2.dist-info" / "METADATA").write_text("Name: pkg\nVersion: 1.2\n")
        assert mcp_installer._python_installed_version(tmp_path / "venv", "pkg") == "1.2"


_PATCH = """diff --git a/node_modules/pkg/index.js b/node_modules/pkg/index.js
index 0000000..1111111 100644
--- a/node_modules/pkg/index.js
+++ b/node_modules/pkg/index.js
@@ -1 +1 @@
-old
+new
"""


class TestPatches:
    def _patched_node_dir(self, tmp_path: Path) -> Path:
        # The MCP folder sits INSIDE a git work tree, as on a bare-metal install
        # where mcps/ lives under the platform checkout.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        mcp_dir = _node_dir(tmp_path)
        (mcp_dir / "node_modules" / "pkg" / "index.js").write_text("old\n")
        (mcp_dir / "patches").mkdir()
        (mcp_dir / "patches" / "pkg+1.0.0.patch").write_text(_PATCH)
        return mcp_dir

    @pytest.mark.asyncio
    async def test_patches_are_applied_by_git_inside_a_work_tree(self, tmp_path, monkeypatch):
        mcp_dir = self._patched_node_dir(tmp_path)
        marker = tmp_path / "bin-ran"
        (mcp_dir / "node_modules" / ".bin").mkdir()
        bin_ = mcp_dir / "node_modules" / ".bin" / "patch-package"
        bin_.write_text(f"#!/bin/sh\ntouch {marker}\n")
        bin_.chmod(0o755)
        calls = _recording_spawn(monkeypatch, [_Proc(0)], passthrough=("git",))
        r = await mcp_installer.install_mcp(mcp_dir, "node", "npm:pkg@1.0.0")
        assert r.ok, r.log
        assert (mcp_dir / "node_modules" / "pkg" / "index.js").read_text() == "new\n"
        assert not marker.exists()
        assert not any("npx" in c["argv"][0] for c in calls)
        # A second install over the same node_modules is a no-op for the patch.
        calls[:] = []
        _recording_spawn(monkeypatch, [_Proc(0)], passthrough=("git",))
        r = await mcp_installer.install_mcp(mcp_dir, "node", "npm:pkg@1.0.0")
        assert r.ok, r.log
        assert (mcp_dir / "node_modules" / "pkg" / "index.js").read_text() == "new\n"
        assert "already applied" in r.log

    @pytest.mark.asyncio
    async def test_a_skipped_or_failed_patch_fails_the_install(self, tmp_path, monkeypatch):
        mcp_dir = self._patched_node_dir(tmp_path)
        _recording_spawn(monkeypatch, [_Proc(0), _Proc(1), _Proc(0, b"Skipped patch 'node_modules/pkg/index.js'.")])
        monkeypatch.setattr(mcp_installer.shutil, "which", lambda n: "/usr/bin/git")
        r = await mcp_installer.install_mcp(mcp_dir, "node", "npm:pkg@1.0.0")
        assert r.ok is False
        assert "Skipped patch" in r.log

    @pytest.mark.asyncio
    async def test_patches_need_git(self, tmp_path, monkeypatch):
        mcp_dir = self._patched_node_dir(tmp_path)
        _recording_spawn(monkeypatch, [_Proc(0)])
        monkeypatch.setattr(mcp_installer.shutil, "which", lambda n: None)
        r = await mcp_installer.install_mcp(mcp_dir, "node", "npm:pkg@1.0.0")
        assert r.ok is False
        assert "git" in r.log


class TestSystemRequirementNames:
    def test_bad_names_are_refused_before_any_command(self, monkeypatch):
        ran = []
        monkeypatch.setattr(mcp_installer.subprocess, "run", lambda *a, **k: ran.append(a))
        with patch("services.mcp.mcp_installer._detect_os_keys", return_value=["debian"]), \
             patch("services.mcp.mcp_installer._is_package_installed", return_value=False):
            ok, msg = mcp_installer.install_system_requirements(
                mcp_installer.SystemRequirementsInput(debian=["libmagic1", "--force-yes", "a b"]))
        assert ok is False and "--force-yes" in msg and ran == []

    def test_names_follow_a_double_dash(self, monkeypatch):
        ran = []

        class _R:
            returncode = 0
            stdout = stderr = ""

        monkeypatch.setattr(mcp_installer.subprocess, "run", lambda cmd, **k: ran.append(cmd) or _R())
        with patch("services.mcp.mcp_installer._detect_os_keys", return_value=["debian"]), \
             patch("services.mcp.mcp_installer._is_package_installed", return_value=False):
            ok, _ = mcp_installer.install_system_requirements(
                mcp_installer.SystemRequirementsInput(debian=["libmagic1", "poppler-utils"]))
        assert ok is True
        assert ran[0][-3:] == ["--", "libmagic1", "poppler-utils"]


class TestVenvRemovalOffTheLoop:
    """A venv holds thousands of files; removing it on the event loop stalls
    every other request for seconds."""

    class _Stop(Exception):
        pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["pypi:pkg", "git+https://host/r.git@v1"])
    async def test_the_package_venv_is_removed_in_a_worker_thread(self, tmp_path, monkeypatch, source):
        import threading
        seen: list[bool] = []

        def _rmtree(path, ignore_errors=False):
            seen.append(threading.current_thread() is threading.main_thread())
            raise self._Stop

        monkeypatch.setattr(mcp_installer.shutil, "rmtree", _rmtree)
        mcp_dir = tmp_path / "community" / "pkg"
        mcp_dir.mkdir(parents=True)
        with pytest.raises(self._Stop):
            await mcp_installer.install_mcp(mcp_dir, "python", source)
        assert seen == [False]


# ---------------------------------------------------------------------------
# The install timeout bounds the whole run of every subprocess, not its spawn:
# a hung child is killed with its whole process tree, reaped, and the timeout
# reaches the caller's rollback.
# ---------------------------------------------------------------------------

import sys
import textwrap


def _tool(path: Path, *, hang_on: str = "", body: str = "exit 0") -> Path:
    """A stand-in executable that records its pid in ``<path>.pids`` and, when
    its argument line holds ``hang_on``, starts a child that keeps the output
    pipe open and hangs itself; otherwise it runs ``body``."""
    pids = path.with_name(path.name + ".pids")
    hang = ""
    if hang_on:
        hang = (f'case " $* " in *" {hang_on} "*) sleep 20 & echo $! >> "{pids}"; '
                f'sleep 20; exit 0;; esac\n')
    path.write_text(f'#!/bin/sh\necho $$ >> "{pids}"\n{hang}{body}\n')
    path.chmod(0o755)
    return path


def _recorded_pids(tmp_path: Path) -> list[int]:
    return [int(x) for f in tmp_path.rglob("*.pids") for x in f.read_text().split()]


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


async def _none_alive(pids: list[int]) -> bool:
    for _ in range(40):
        if not any(_alive(p) for p in pids):
            return True
        await asyncio.sleep(0.05)
    return False


_UV_OK = ('case "$1" in venv) for a; do last="$a"; done; mkdir -p "$last/bin"; exit 0;; '
          'pip) exit 0;; esac')
_UV_FLOOR = ('case "$1" in venv) for a; do last="$a"; done; mkdir -p "$last/bin"; exit 0;; '
             'pip) echo "the current Python version (3.10.12) does not satisfy Python>=3.11"; '
             'exit 1;; esac')
_NPM_OK = ('mkdir -p node_modules/pkg && printf \'{"version":"1.0.0"}\' > '
           'node_modules/pkg/package.json')


def _hung_site(site: str, tmp_path: Path, monkeypatch):
    """The install call whose ``site`` subprocess hangs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin')}")
    if site in ("npm install", "git apply"):
        mcp_dir = tmp_path / "mcps" / "community" / "node-mcp"
        mcp_dir.mkdir(parents=True)
        _tool(bin_dir / "npm", hang_on="install" if site == "npm install" else "", body=_NPM_OK)
        if site == "git apply":
            (mcp_dir / "patches").mkdir()
            (mcp_dir / "patches" / "pkg+1.0.0.patch").write_text(_PATCH)
            git = _tool(bin_dir / "git", hang_on="apply")
            monkeypatch.setattr(mcp_installer.shutil, "which", lambda n: str(git) if n == "git" else None)
        return mcp_installer.install_mcp(mcp_dir, "node", "npm:pkg@1.0.0", timeout=1)
    if site in ("pypi venv", "pypi pip install"):
        uv = _tool(bin_dir / "uv", hang_on="venv" if site == "pypi venv" else "pip install", body=_UV_OK)
        return mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:ha-mcp@6.7.0", uv_bin=str(uv), timeout=1)
    mcp_dir = tmp_path / "mcps" / "custom" / "bundled"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "requirements.txt").write_text("x\n")
    hang_on, body = {
        "bundled venv": ("venv", _UV_OK),
        "requirements install": ("pip install", _UV_OK),
        "python floor retry venv": ("venv --python", _UV_FLOOR),
    }[site]
    uv = _tool(bin_dir / "uv", hang_on=hang_on, body=body)
    return mcp_installer.install_mcp(mcp_dir, "python", "", uv_bin=str(uv), timeout=1)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
class TestInstallTimeout:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", [
        "npm install", "git apply", "pypi venv", "pypi pip install",
        "bundled venv", "requirements install", "python floor retry venv",
    ])
    async def test_a_hung_subprocess_is_killed_with_its_tree_and_times_out(
            self, tmp_path, monkeypatch, site):
        install = _hung_site(site, tmp_path, monkeypatch)
        with pytest.raises(asyncio.TimeoutError, match="did not finish within 1 s"):
            await asyncio.wait_for(install, 8)
        pids = _recorded_pids(tmp_path)
        assert len(pids) >= 2, pids  # the hung tool and the child it started
        assert await _none_alive(pids), [p for p in pids if _alive(p)]

    @pytest.mark.asyncio
    async def test_the_child_runs_in_its_own_process_group(self, tmp_path, monkeypatch):
        seen: list[dict] = []
        real = asyncio.create_subprocess_exec

        async def _spawn(*argv, **kw):
            seen.append(kw)
            return await real(*argv, **kw)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        rc, out = await mcp_installer._run_bounded(
            ["sh", "-c", "echo hi"], cwd=str(tmp_path), env=dict(os.environ), timeout=5)
        assert (rc, out.strip()) == (0, "hi")
        assert seen[0]["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_a_cancelled_install_kills_its_child(self, tmp_path, monkeypatch):
        install = _hung_site("npm install", tmp_path, monkeypatch)
        task = asyncio.ensure_future(install)
        for _ in range(100):
            if len(_recorded_pids(tmp_path)) >= 2:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pids = _recorded_pids(tmp_path)
        assert await _none_alive(pids), [p for p in pids if _alive(p)]

    def test_a_timeout_leaves_no_zombie_under_a_reaping_parent(self, tmp_path):
        """Where the proxy is the reaper of last resort (PID 1 of a container),
        the killed child's own children are its children once their parent
        dies: they are reaped, not left as zombies."""
        script = _tool(tmp_path / "hang", hang_on="go")
        code = textwrap.dedent(f"""
            import asyncio, ctypes, importlib.util, os, sys
            ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
            spec = importlib.util.spec_from_file_location("inst", {str(Path(mcp_installer.__file__))!r})
            inst = importlib.util.module_from_spec(spec)
            sys.modules["inst"] = inst
            spec.loader.exec_module(inst)

            def children():
                me, out = os.getpid(), []
                for d in os.listdir("/proc"):
                    if d.isdigit():
                        try:
                            stat = open(f"/proc/{{d}}/stat").read()
                        except OSError:
                            continue
                        fields = stat.rsplit(")", 1)[1].split()
                        if int(fields[1]) == me:
                            out.append((int(d), fields[0]))
                return out

            async def main():
                try:
                    await inst._run_bounded([{str(script)!r}, "go"], cwd={str(tmp_path)!r},
                                            env=dict(os.environ), timeout=1)
                except asyncio.TimeoutError:
                    pass
                print(children())

            asyncio.run(main())
        """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[]", r.stdout


class TestWindowsTreeKill:
    @pytest.mark.asyncio
    async def test_the_tree_is_killed_by_taskkill_then_the_child_itself(self, monkeypatch):
        """``cmd /c npm`` leaves npm running when only cmd is killed: the whole
        tree goes through ``taskkill /T /F``, then the child is killed and
        reaped whatever taskkill did."""
        proc = await asyncio.create_subprocess_exec(
            "sleep", "20", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        ran: list[list[str]] = []
        monkeypatch.setattr(mcp_installer.sys, "platform", "win32")
        monkeypatch.setattr(mcp_installer.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
        await mcp_installer._kill_tree(proc)
        assert ran == [["taskkill", "/T", "/F", "/PID", str(proc.pid)]]
        assert proc.returncode is not None


# ---------------------------------------------------------------------------
# source_build: a listed package installs from a wheel when one exists; only a
# wheels-only resolve that fails on it retries it from source.
# ---------------------------------------------------------------------------

_NO_WHEEL = (b"  x No solution found when resolving dependencies:\n"
             b"  Because antlr4-python3-runtime==4.13.2 has no usable wheels and building "
             b"from source is disabled, we can conclude that unifi-network-mcp==1.0.0 "
             b"cannot be used.\n")
_NO_WHEEL_2 = (b"  x No solution found when resolving dependencies:\n"
               b"  Because pycairo==1.26.0 has no usable wheels and building from source "
               b"is disabled, we can conclude that unifi-network-mcp==1.0.0 cannot be used.\n")


def _no_binary(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--no-binary"]


class TestSourceBuildFallback:
    @pytest.mark.asyncio
    async def test_a_listed_package_with_a_wheel_installs_from_the_wheel(self, tmp_path, monkeypatch):
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(0)])
        r = await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:unifi-network-mcp@1.0.0",
            uv_bin=_fake_uv(tmp_path), source_build=["antlr4-python3-runtime"],
        )
        assert r.ok, r.log
        assert len(calls) == 2
        assert "--only-binary=:all:" in calls[1]["argv"]
        assert _no_binary(calls[1]["argv"]) == []

    @pytest.mark.asyncio
    async def test_a_listed_package_the_wheels_only_resolve_fails_on_is_built(self, tmp_path, monkeypatch):
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(1, _NO_WHEEL), _Proc(0)])
        r = await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:unifi-network-mcp@1.0.0",
            uv_bin=_fake_uv(tmp_path), source_build=["Antlr4_Python3.Runtime", "pycairo"],
        )
        assert r.ok, r.log
        assert len(calls) == 3
        retry = calls[2]["argv"]
        assert "--only-binary=:all:" in retry
        assert _no_binary(retry) == ["Antlr4_Python3.Runtime"]
        assert retry[-1] == "unifi-network-mcp==1.0.0"

    @pytest.mark.asyncio
    async def test_each_listed_package_is_added_only_when_the_resolve_names_it(self, tmp_path, monkeypatch):
        calls = _recording_spawn(
            monkeypatch, [_Proc(0), _Proc(1, _NO_WHEEL), _Proc(1, _NO_WHEEL_2), _Proc(0)])
        r = await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:unifi-network-mcp@1.0.0",
            uv_bin=_fake_uv(tmp_path), source_build=["antlr4-python3-runtime", "pycairo"],
        )
        assert r.ok, r.log
        assert [_no_binary(c["argv"]) for c in calls[1:]] == [
            [], ["antlr4-python3-runtime"], ["antlr4-python3-runtime", "pycairo"]]

    @pytest.mark.asyncio
    async def test_a_failure_that_names_no_listed_package_is_not_retried(self, tmp_path, monkeypatch):
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(1, _NO_WHEEL_2)])
        r = await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:unifi-network-mcp@1.0.0",
            uv_bin=_fake_uv(tmp_path), source_build=["antlr4-python3-runtime"],
        )
        assert r.ok is False
        assert len(calls) == 2
        assert "pycairo" in r.log

    @pytest.mark.asyncio
    async def test_a_python_floor_is_retried_before_any_source_build(self, tmp_path, monkeypatch):
        floor = (b"Because the current Python version (3.10.12) does not satisfy Python>=3.13 "
                 b"and unifi-network-mcp==1.0.0 depends on Python>=3.13, we can conclude that "
                 b"unifi-network-mcp==1.0.0 cannot be used.\n")
        calls = _recording_spawn(monkeypatch, [_Proc(0), _Proc(1, floor), _Proc(0), _Proc(0)])
        r = await mcp_installer.install_mcp(
            _python_dir(tmp_path), "python", "pypi:unifi-network-mcp@1.0.0",
            uv_bin=_fake_uv(tmp_path), source_build=["unifi-network-mcp"],
        )
        assert r.ok, r.log
        assert calls[2]["argv"][1:4] == ["venv", "--python", ">=3.13"]
        assert all(_no_binary(c["argv"]) == [] for c in calls)
