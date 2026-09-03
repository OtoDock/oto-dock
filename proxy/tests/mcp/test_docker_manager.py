"""Manifest-driven `_inject_mcp_env`.

Verifies that the Docker MCP `.env` is generated entirely from manifest
declarations + the platform token registry — no hardcoded list, no leftover
hand-edits creep into community MCP archives.
"""

from pathlib import Path

import pytest

from services.mcp import docker_manager, mcp_registry


@pytest.fixture
def file_tools_manifest():
    mcp_registry.scan_manifests()
    m = mcp_registry.get_manifest("file-tools")
    if m is None:
        pytest.skip("file-tools manifest not present in this checkout")
    return m


@pytest.fixture
def camoufox_manifest():
    mcp_registry.scan_manifests()
    m = mcp_registry.get_manifest("camoufox")
    if m is None:
        pytest.skip("camoufox manifest not present in this checkout")
    return m


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k] = v
    return out


def test_inject_mcp_env_writes_declared_keys(file_tools_manifest, tmp_path, monkeypatch):
    """`.env` should contain exactly the keys declared in manifest.env (+ agent_env)."""
    # Redirect mcp_dir to a temp location so we don't disturb the real .env
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)

    docker_manager._inject_mcp_env(file_tools_manifest)

    env_path = tmp_path / ".env"
    assert env_path.exists(), "_inject_mcp_env must create .env"
    written = _read_env(env_path)

    # Every key from manifest.env should be present after token resolution
    for k in file_tools_manifest.env.keys():
        assert k in written, f"manifest-declared key {k!r} missing from .env"

    # And every key from manifest.agent_env (Docker MCP secrets)
    for k in (file_tools_manifest.agent_env or {}).keys():
        assert k in written, f"manifest-declared agent_env key {k!r} missing from .env"


def test_inject_mcp_env_resolves_platform_tokens(file_tools_manifest, tmp_path, monkeypatch):
    """`${platform.*}` tokens in manifest values get replaced by config values."""
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)

    docker_manager._inject_mcp_env(file_tools_manifest)
    written = _read_env(tmp_path / ".env")

    # No literal token strings should leak into .env
    for k, v in written.items():
        assert "${platform." not in v, f"unresolved platform token in {k}: {v!r}"
        assert "${session." not in v, f"unresolved session token in {k}: {v!r}"

    # The master key must NEVER land in a Docker MCP `.env`. Docker
    # MCPs that call back authenticate with a per-session JWT injected as the
    # Authorization header (server.proxy_callbacks), not PROXY_API_KEY.
    assert "PROXY_API_KEY" not in written, "master key must not be written to .env"
    assert written.get("MCP_PORT") == str(file_tools_manifest.server.port)
    assert written.get("PROXY_URL", "").startswith("http://"), "PROXY_URL should be a URL"


def test_inject_mcp_env_empty_manifest_writes_no_keys(camoufox_manifest, tmp_path, monkeypatch):
    """Camoufox declares no env — `.env` should be empty (or absent)."""
    monkeypatch.setattr(camoufox_manifest, "mcp_dir", tmp_path)

    docker_manager._inject_mcp_env(camoufox_manifest)

    env_path = tmp_path / ".env"
    # `.env` may or may not exist depending on implementation; if it does, it must be empty
    if env_path.exists():
        content = env_path.read_text().strip()
        assert content == "", f"camoufox .env should be empty; got: {content!r}"


def test_inject_mcp_env_preserves_undeclared_existing_keys(file_tools_manifest, tmp_path, monkeypatch):
    """Existing hand-edited keys not declared in manifest are preserved (escape hatch)."""
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)

    # Pre-populate .env with a key the manifest doesn't declare
    (tmp_path / ".env").write_text("CUSTOM_HANDEDIT_KEY=keep-me\nPROXY_URL=should-be-overridden\n")

    docker_manager._inject_mcp_env(file_tools_manifest)
    written = _read_env(tmp_path / ".env")

    assert written.get("CUSTOM_HANDEDIT_KEY") == "keep-me", \
        "Existing non-declared keys should be preserved"
    # Manifest values should win over hand-edits for declared keys
    assert written.get("PROXY_URL") != "should-be-overridden", \
        "Manifest declarations should override existing values for declared keys"


def test_inject_mcp_env_idempotent(file_tools_manifest, tmp_path, monkeypatch):
    """Running twice produces the same result."""
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)

    docker_manager._inject_mcp_env(file_tools_manifest)
    first = (tmp_path / ".env").read_text()

    docker_manager._inject_mcp_env(file_tools_manifest)
    second = (tmp_path / ".env").read_text()

    assert first == second, "_inject_mcp_env must be idempotent"


def test_startup_docker_mcps_skips_in_t2(monkeypatch):
    """Non-T1 (T2 docker-compose / T3 cloud): startup must NOT drive per-MCP
    `docker compose`.

    In T2 the core file-tools is an operator-managed compose sibling and
    community Docker MCPs are pull-managed — never started from their
    build-context compose files here (the socket-proxy blocks build). In T3
    (external-pool) there is no local daemon at all. Only bare-metal T1
    (managed-local) drives lifecycle here, so the gate keys off
    ``current_mode() == MANAGED_LOCAL`` and must return BEFORE reading
    mcp_state / manifests or shelling out — a raise from either proves it failed.
    """
    from core.config import deployment
    from storage import mcp_store

    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(
        mcp_store, "get_all_mcp_states",
        lambda: (_ for _ in ()).throw(AssertionError("must not query state in T2")),
    )
    monkeypatch.setattr(
        docker_manager.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not shell docker in T2")),
    )

    # Returns cleanly (early) — no state read, no subprocess.
    docker_manager.startup_docker_mcps()


def test_startup_docker_mcps_runs_in_t1(monkeypatch):
    """T1 (bare-metal): startup proceeds to read mcp_state (no early return)."""
    from core.config import deployment
    from storage import mcp_store

    monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)

    called = {"state": False}

    def _states():
        called["state"] = True
        return {}

    monkeypatch.setattr(mcp_store, "get_all_mcp_states", _states)
    monkeypatch.setattr(__import__("services.mcp", fromlist=["mcp_registry"]).mcp_registry,
                        "get_all_manifests", lambda: {})

    docker_manager.startup_docker_mcps()
    assert called["state"], "T1 must read mcp_state (no skip)"


# ── First-boot bring-up: streaming compose + docker-group loudness ──────────
#
# `_compose_up`/pull used `subprocess.run(capture_output=True)`, so a fresh
# box's multi-GB image pull produced zero output while blocking — and a
# docker.sock permission failure surfaced only as a silent `not_found`.


import io
import logging
import subprocess as _subprocess


class _FakePopen:
    def __init__(self, stderr_text: str, returncode: int = 0):
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.fixture
def reset_perm_flag(monkeypatch):
    monkeypatch.setattr(docker_manager, "_docker_perm_logged", False)


def test_run_compose_streaming_buffers_and_throttles(
    file_tools_manifest, monkeypatch, caplog,
):
    """Full stderr is buffered for the callers; progress-shaped lines log at
    INFO throttled (~5s), so only the FIRST of a rapid burst appears."""
    text = (
        "collabora Pulling\n"
        "some unrelated compose chatter\n"
        " 3f4ca61aafcd Downloading [===>   ]  55MB/2.03GB\n"
        "boom: it failed\n"
    )
    monkeypatch.setattr(
        docker_manager.subprocess, "Popen",
        lambda *a, **k: _FakePopen(text, returncode=1),
    )
    caplog.set_level(logging.INFO, logger="claude-proxy.docker-manager")

    rc, stderr = docker_manager._run_compose_streaming(
        file_tools_manifest, ["docker", "compose", "up"], timeout=30,
    )

    assert rc == 1
    assert stderr == text, "callers need the complete stderr buffer"
    progress_logs = [r.message for r in caplog.records if "Pulling" in r.message
                     or "Downloading" in r.message]
    assert len(progress_logs) == 1, "burst must throttle to one line"
    assert "Pulling" in progress_logs[0]


def test_run_compose_streaming_timeout_kills_process(file_tools_manifest):
    """The watchdog preserves the old subprocess.run(timeout=) contract."""
    with pytest.raises(_subprocess.TimeoutExpired):
        docker_manager._run_compose_streaming(
            file_tools_manifest,
            ["bash", "-c", "echo start >&2; sleep 30"],
            timeout=1,
        )


def test_compose_up_overlap_retry_preserved(file_tools_manifest, tmp_path, monkeypatch):
    """The T1 subnet-TOCTOU retry survives the streaming rewrite."""
    from services.mcp import compose_rewrite

    # Success now records the compose hash — keep it out of the real mcp dir.
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)
    results = iter([(1, "failed to create network: pool overlaps ..."), (0, "")])
    monkeypatch.setattr(
        docker_manager, "_run_compose_streaming",
        lambda *a, **k: next(results),
    )
    realloc = {"called": False}

    def _realloc(manifest, force_realloc=False):
        realloc["called"] = force_realloc

    monkeypatch.setattr(compose_rewrite, "ensure_t1_override", _realloc)

    assert docker_manager._compose_up(
        file_tools_manifest, ["up", "-d"], timeout=10, t1_retry=True,
    ) is True
    assert realloc["called"] is True


def test_compose_up_perm_denied_logs_hint_once(
    file_tools_manifest, monkeypatch, caplog, reset_perm_flag,
):
    perm_err = (
        "permission denied while trying to connect to the Docker daemon "
        "socket at unix:///var/run/docker.sock"
    )
    monkeypatch.setattr(
        docker_manager, "_run_compose_streaming", lambda *a, **k: (1, perm_err),
    )
    caplog.set_level(logging.ERROR, logger="claude-proxy.docker-manager")

    assert docker_manager._compose_up(file_tools_manifest, ["up"], timeout=10) is False
    assert docker_manager._compose_up(file_tools_manifest, ["up"], timeout=10) is False

    hints = [r for r in caplog.records if "docker' group" in r.message]
    assert len(hints) == 1, "actionable hint fires once per boot, at ERROR"
    fails = [r for r in caplog.records if "Failed to start Docker MCP" in r.message]
    assert len(fails) == 2, "the per-MCP failure line still logs every time"


def test_container_status_perm_denied_logs_hint(
    file_tools_manifest, monkeypatch, caplog, reset_perm_flag,
):
    """A group-less shell used to read as a silent `not_found`."""

    class _R:
        returncode = 1
        stdout = ""
        stderr = ("permission denied while trying to connect to the Docker "
                  "daemon socket at unix:///var/run/docker.sock")

    monkeypatch.setattr(docker_manager.subprocess, "run", lambda *a, **k: _R())
    caplog.set_level(logging.ERROR, logger="claude-proxy.docker-manager")

    assert docker_manager.get_container_status(file_tools_manifest) == "not_found"
    assert any("docker' group" in r.message for r in caplog.records)


def test_compose_up_name_conflict_self_heal(file_tools_manifest, tmp_path, monkeypatch):
    """A 'name already in use' collision on OUR exact container name (a
    previous deployment generation's container) is removed and retried."""
    import config as _config
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)
    expected = f"otodock-{_config.INSTALL_ID}-mcp-{file_tools_manifest.name}"
    conflict = (
        f'Error response from daemon: Conflict. The container name '
        f'"/{expected}" is already in use by container "abc123".'
    )
    results = iter([(1, conflict), (0, "")])
    monkeypatch.setattr(
        docker_manager, "_run_compose_streaming",
        lambda *a, **k: next(results),
    )
    removed = {}

    def _fake_run(cmd, **kw):
        removed["cmd"] = cmd
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_manager.subprocess, "run", _fake_run)
    assert docker_manager._compose_up(
        file_tools_manifest, ["up", "-d"], timeout=10,
    ) is True
    assert removed["cmd"][:3] == ["docker", "rm", "-f"]
    assert removed["cmd"][3] == expected


def test_compose_up_no_removal_for_foreign_name(file_tools_manifest, monkeypatch):
    """A collision on a DIFFERENT install-id's container name must never
    trigger removal — that container may belong to another live install."""
    conflict = (
        'Error response from daemon: Conflict. The container name '
        '"/otodock-deadbeef-mcp-file-tools" is already in use by container "x".'
    )
    monkeypatch.setattr(
        docker_manager, "_run_compose_streaming", lambda *a, **k: (1, conflict),
    )
    ran = {"rm": False}

    def _fake_run(cmd, **kw):
        ran["rm"] = True
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_manager.subprocess, "run", _fake_run)
    assert docker_manager._compose_up(
        file_tools_manifest, ["up", "-d"], timeout=10,
    ) is False
    assert ran["rm"] is False


# ── T1 compose-config recreate trigger ───────────────────────────────────────
#
# The running-container skip path historically ran NOTHING, so base-compose /
# override changes never reached a standing T1 install. Startup now hashes
# the on-disk config (pre-refresh baseline → ensure_t1_override → post),
# compares against the recorded hash, and force-recreates on drift; any
# failure degrades to the plain skip.


from types import SimpleNamespace


@pytest.fixture
def fake_docker_manifest(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services:\n  fake: {}\n")
    return SimpleNamespace(
        name="fake",
        mcp_dir=tmp_path,
        server=SimpleNamespace(
            runtime="docker", docker_compose="docker-compose.yml",
            image="", service_name="fake", port=9999,
        ),
        env={}, agent_env={},
        credentials=SimpleNamespace(type="none"),
        tool_filter=None,
    )


def _drive_startup(monkeypatch, manifest, *, env_changed=False,
                   override_writer=None, status="running"):
    """Run startup_docker_mcps against one fake running docker MCP."""
    from core.config import deployment
    from services.mcp import compose_rewrite, mcp_registry
    from storage import mcp_store

    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_warn_foreign_mcp_leftovers", lambda: None)
    monkeypatch.setattr(mcp_store, "get_all_mcp_states", lambda: {manifest.name: True})
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {manifest.name: manifest})
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: env_changed)
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: status)
    monkeypatch.setattr(
        compose_rewrite, "ensure_t1_override",
        override_writer or (lambda m, **kw: None),
    )

    calls = []
    monkeypatch.setattr(
        docker_manager, "start_container",
        lambda m, force_recreate=False: calls.append(force_recreate) or True,
    )
    docker_manager.startup_docker_mcps()
    return calls


def test_compose_hash_stable_and_sensitive(fake_docker_manifest):
    m = fake_docker_manifest
    h1 = docker_manager._compose_config_hash(m)
    assert h1 == docker_manager._compose_config_hash(m)

    (m.mcp_dir / "docker-compose.yml").write_text("services:\n  fake: {}\n# c\n")
    h2 = docker_manager._compose_config_hash(m)
    assert h2 != h1

    (m.mcp_dir / "docker-compose.override.yml").write_text("services: {}\n")
    h3 = docker_manager._compose_config_hash(m)
    assert h3 != h2

    (m.mcp_dir / "docker-compose.yml").unlink()
    assert docker_manager._compose_config_hash(m) is None


def test_startup_recreates_on_recorded_hash_drift(fake_docker_manifest, monkeypatch):
    m = fake_docker_manifest
    (m.mcp_dir / docker_manager._COMPOSE_HASH_NAME).write_text("stale-recorded-hash\n")
    calls = _drive_startup(monkeypatch, m)
    assert calls == [True], "changed compose config must force-recreate"


def test_startup_skips_on_equal_hash(fake_docker_manifest, monkeypatch):
    m = fake_docker_manifest
    digest = docker_manager._compose_config_hash(m)
    (m.mcp_dir / docker_manager._COMPOSE_HASH_NAME).write_text(digest + "\n")
    calls = _drive_startup(monkeypatch, m)
    assert calls == [], "unchanged config must keep the plain skip"


def test_startup_seeds_quietly_without_pending_drift(fake_docker_manifest, monkeypatch):
    """First boot with the feature, override refresh changes nothing:
    seed the hash, do NOT recreate (no storm on unchanged installs)."""
    m = fake_docker_manifest
    calls = _drive_startup(monkeypatch, m)
    assert calls == []
    recorded = (m.mcp_dir / docker_manager._COMPOSE_HASH_NAME).read_text().strip()
    assert recorded == docker_manager._compose_config_hash(m)


def test_startup_first_boot_delivers_pending_override_drift(
    fake_docker_manifest, monkeypatch,
):
    """THE acceptance case: no recorded hash, and the startup override
    refresh materializes a pending change (e.g. the shipped mem_limit
    default) — the pre-refresh baseline must trigger one recreate."""
    m = fake_docker_manifest

    def _refresh(manifest, **kw):
        (manifest.mcp_dir / "docker-compose.override.yml").write_text(
            "services:\n  fake:\n    mem_limit: 2g\n"
        )

    calls = _drive_startup(monkeypatch, m, override_writer=_refresh)
    assert calls == [True], "pending drift on first boot must recreate"


def test_startup_env_trigger_unaffected(fake_docker_manifest, monkeypatch):
    m = fake_docker_manifest
    digest = docker_manager._compose_config_hash(m)
    (m.mcp_dir / docker_manager._COMPOSE_HASH_NAME).write_text(digest + "\n")
    calls = _drive_startup(monkeypatch, m, env_changed=True)
    assert calls == [True], ".env changes must keep force-recreating"


def test_startup_drift_check_failure_degrades_to_skip(
    fake_docker_manifest, monkeypatch, caplog,
):
    """The skip path must stay bulletproof: an exploding override refresh
    logs a warning and skips — never raises, never recreates."""
    m = fake_docker_manifest

    def _boom(manifest, **kw):
        raise OSError("disk on fire")

    calls = _drive_startup(monkeypatch, m, override_writer=_boom)
    assert calls == []


def test_record_compose_hash_gated_to_t1(fake_docker_manifest, monkeypatch):
    from core.config import deployment

    monkeypatch.setattr(
        deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    docker_manager._record_compose_hash(fake_docker_manifest)
    assert not (fake_docker_manifest.mcp_dir / docker_manager._COMPOSE_HASH_NAME).exists(), \
        "T2 must never grow hash files (start_container runs there too)"


def test_compose_up_success_records_hash_from_disk(
    fake_docker_manifest, monkeypatch,
):
    from core.config import deployment

    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_run_compose_streaming", lambda *a, **k: (0, ""))
    assert docker_manager._compose_up(fake_docker_manifest, ["up", "-d"], timeout=10) is True
    recorded = (
        fake_docker_manifest.mcp_dir / docker_manager._COMPOSE_HASH_NAME
    ).read_text().strip()
    assert recorded == docker_manager._compose_config_hash(fake_docker_manifest)


def test_record_failure_never_breaks_a_successful_start(
    fake_docker_manifest, monkeypatch,
):
    from core.config import deployment

    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_run_compose_streaming", lambda *a, **k: (0, ""))
    monkeypatch.setattr(
        docker_manager, "_compose_config_hash",
        lambda m: (_ for _ in ()).throw(OSError("disk on fire")),
    )
    assert docker_manager._compose_up(fake_docker_manifest, ["up", "-d"], timeout=10) is True
