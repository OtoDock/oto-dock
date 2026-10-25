"""Codex's own sandbox inside the platform's user-scope sandbox, on the
pinned Codex binary: a nested bwrap that mounts a read-only tmpfs over named
folders of every writable root (cwd included), which bwrap must create. The
user folder's root is read-only, so each of those folders must already be
there: one a new Codex release adds (``.aws`` from 0.160) breaks the first
command of every chat. This test runs the real thing, so a Codex bump that
protects a new folder fails here, not in the field.

Skipped where it cannot run: no binary of the pinned version (set
``CODEX_BIN`` to the one to try), no bwrap, or a host whose sandbox or nested
bwrap cannot start (the platform's own preflights say so on such a host).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import config as app_config
from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, cli_install_ro_binds


def _pinned_codex() -> str | None:
    """A Codex binary of the pinned version, or None."""
    pin = app_config.PINNED_CODEX_VERSION
    candidates = [os.environ.get("CODEX_BIN"), getattr(app_config, "CODEX_BIN", ""),
                  shutil.which("codex"), str(Path.home() / ".npm-global" / "bin" / "codex")]
    for cand in dict.fromkeys(c for c in candidates if c):
        if not os.path.exists(cand):
            continue
        try:
            out = subprocess.run([cand, "--version"], capture_output=True, text=True,
                                 timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if pin and pin in out.split():
            return cand
    return None


def _run(prefix_of, inner: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(prefix_of(inner), capture_output=True, text=True, env=env, timeout=120)


def test_codex_runs_its_own_sandbox_in_the_user_folder(tmp_path):
    codex = _pinned_codex()
    if codex is None:
        pytest.skip(f"no codex {app_config.PINNED_CODEX_VERSION} here (set CODEX_BIN)")
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap is not installed")
    agents, mcps = tmp_path / "agents", tmp_path / "mcps"
    agent = agents / "probe-agent"
    user = agent / "users" / "alice"
    for d in (user / ".codex", user / ".claude", agent / "config", agent / "workspace",
              agent / "knowledge", mcps):
        d.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    binds = cli_install_ro_binds(codex)
    if node:
        binds.append(str(Path(node).resolve().parent.parent))
    cfg = SandboxConfig(
        role="manager", username="alice", agent_name="probe-agent", is_admin_agent=False,
        host_agents_dir=agents.resolve(), host_mcps_dir=mcps.resolve(),
        host_claude_dir=(user / ".claude").resolve(), mcp_sandbox_mounts=[],
        mcp_dir_binds=[], net_forwards=["8400"], extra_ro_binds=binds,
    )
    builder = SandboxBuilder(cfg)
    path = os.pathsep.join(p for p in (str(Path(node).resolve().parent) if node else "",
                                       "/usr/local/bin", "/usr/bin", "/bin") if p)
    env = {"PATH": path, "LANG": "C.UTF-8",
           **builder.get_env_overrides(config_dir_name=".codex", config_env_var="CODEX_HOME")}

    ready = _run(builder.build_command_prefix, ["true"], env)
    if ready.returncode != 0:
        pytest.skip(f"the platform sandbox cannot start here: {ready.stderr.strip()[-300:]}")
    nested = _run(builder.build_command_prefix, ["bwrap", "--ro-bind", "/", "/", "true"], env)
    if nested.returncode != 0:
        pytest.skip(f"nested bwrap is denied here: {nested.stderr.strip()[-300:]}")

    r = _run(builder.build_command_prefix,
             [codex, "sandbox", "-c", "sandbox_mode=workspace-write", "ls", "-a"], env)
    out = r.stdout + r.stderr
    assert "Read-only file system" not in out, (
        "Codex's own sandbox needs a folder the read-only user root lacks: add it to the "
        f"pre-created list in core/sandbox/sandbox.py\n{out[-1500:]}")
    assert r.returncode == 0, out[-1500:]
    assert "workspace" in r.stdout.split()
