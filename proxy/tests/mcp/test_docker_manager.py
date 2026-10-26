"""Manifest-driven `_inject_mcp_env`.

Verifies that the Docker MCP `.env` is generated entirely from manifest
declarations + the platform token registry — no hardcoded list, no leftover
hand-edits creep into community MCP archives.
"""

from pathlib import Path

import pytest

from services.mcp import docker_manager, mcp_registry
from tests._timing import bound


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


def test_the_frame_ancestors_setting_is_gone_and_its_placeholder_stays(
    file_tools_manifest, tmp_path, monkeypatch,
):
    """No compose reads it since Collabora 26.04: the file tools stop writing
    it, the setting is gone, and the placeholder a catalog manifest may name
    still resolves to the dashboard's host and port."""
    import config
    from services.mcp import mcp_templating
    monkeypatch.setattr(file_tools_manifest, "mcp_dir", tmp_path)
    docker_manager._inject_mcp_env(file_tools_manifest)
    assert "COLLABORA_FRAME_ANCESTORS" not in _read_env(tmp_path / ".env")
    assert not hasattr(config, "COLLABORA_FRAME_ANCESTORS")
    for url, host in (("https://otodock.example.com", "otodock.example.com"),
                      ("http://192.168.1.5:8400/", "192.168.1.5:8400"), ("", "")):
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", url)
        assert mcp_templating._resolve_template(
            "${platform.collabora_frame_ancestors}", file_tools_manifest) == host


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
    from storage.mcp import mcp_store

    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(
        mcp_store, "get_all_mcp_states",
        lambda: (_ for _ in ()).throw(AssertionError("must not query state in T2")),
    )
    shelled: list[list[str]] = []

    def _run(cmd, *a, **k):
        # The foreign-leftover warning lists containers in every topology
        # with a daemon; anything past it would be lifecycle work.
        shelled.append(cmd)
        if cmd[:2] != ["docker", "ps"]:
            raise AssertionError("must not shell docker in T2")
        return docker_manager.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docker_manager.subprocess, "run", _run)

    # Returns cleanly (early) — no state read, no lifecycle subprocess.
    docker_manager.startup_docker_mcps()
    assert [c[:2] for c in shelled] == [["docker", "ps"]]


def test_startup_docker_mcps_runs_in_t1(monkeypatch):
    """T1 (bare-metal): startup proceeds to read mcp_state (no early return)."""
    from core.config import deployment
    from storage.mcp import mcp_store

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


import logging
import subprocess as _subprocess


@pytest.fixture
def reset_perm_flag(monkeypatch):
    monkeypatch.setattr(docker_manager, "_docker_perm_logged", False)


def test_run_compose_streaming_buffers_and_throttles(
    file_tools_manifest, tmp_path, caplog,
):
    """Full stderr is buffered for the callers; progress-shaped lines log at
    INFO throttled (~5s), so only the FIRST of a rapid burst appears."""
    text = (
        "collabora Pulling\n"
        "some unrelated compose chatter\n"
        " 3f4ca61aafcd Downloading [===>   ]  55MB/2.03GB\n"
        "boom: it failed\n"
    )
    (tmp_path / "stderr.txt").write_text(text)
    caplog.set_level(logging.INFO, logger="otodock.docker-manager")

    rc, stderr = docker_manager._run_compose_streaming(
        file_tools_manifest, ["bash", "-c", f"cat {tmp_path / 'stderr.txt'} >&2; exit 1"], timeout=30,
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
    caplog.set_level(logging.ERROR, logger="otodock.docker-manager")

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
    caplog.set_level(logging.ERROR, logger="otodock.docker-manager")

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
    from storage.mcp import mcp_store

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


# ---------------------------------------------------------------------------
# The running image against the manifest's, and Restart as a recreate
# ---------------------------------------------------------------------------

from types import SimpleNamespace as _NS


def _ps_lines(*containers: dict) -> str:
    import json as _json
    return "\n".join(_json.dumps(c) for c in containers)


def _image_manifest(tmp_path, image="ghcr.io/otodock/video-tools:0.2.1"):
    return _NS(name="video-tools", mcp_dir=tmp_path,
               server=_NS(runtime="docker", docker_compose="docker-compose.yml",
                          image=image, service_name="video-tools", port=9000))


@pytest.mark.parametrize("a, b", [
    ("busybox", "docker.io/library/busybox:latest"),
    ("library/busybox:1", "busybox:1"),
    ("index.docker.io/softeria/x:0.1", "softeria/x:0.1"),
    ("ghcr.io/otodock/camoufox:0.0.76", "ghcr.io/otodock/camoufox:0.0.76"),
])
def test_image_references_compare_as_docker_resolves_them(a, b):
    assert docker_manager.normalize_image(a) == docker_manager.normalize_image(b)


def test_container_state_reads_the_running_images(tmp_path, monkeypatch):
    out = _ps_lines({"State": "running", "Health": "", "Image": "ghcr.io/otodock/video-tools:0.2.0"})
    monkeypatch.setattr(docker_manager.subprocess, "run",
                        lambda *a, **k: _NS(returncode=0, stdout=out, stderr=""))
    m = _image_manifest(tmp_path)
    status, images = docker_manager.container_state(m)
    assert status == "running" and images == ["ghcr.io/otodock/video-tools:0.2.0"]
    assert docker_manager.get_container_status(m) == "running"
    assert docker_manager.image_stale(m, images) is True
    assert docker_manager.image_stale(m, ["ghcr.io/otodock/video-tools:0.2.1"]) is False
    assert docker_manager.image_stale(_image_manifest(tmp_path, image=""), images) is False
    assert docker_manager.image_stale(m, []) is False


@pytest.fixture
def recreate_rig(tmp_path, monkeypatch):
    from core.config import deployment
    from services.mcp import compose_rewrite
    from storage.mcp import mcp_store
    m = _image_manifest(tmp_path)
    state = {"status": "running", "up": [], "env": 0, "enabled": True}
    monkeypatch.setattr(mcp_store, "get_mcp_state", lambda n: {"enabled": state["enabled"]})
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
    monkeypatch.setattr(compose_rewrite, "ensure_t1_override", lambda m, **kw: None)
    monkeypatch.setattr(docker_manager, "_inject_mcp_env",
                        lambda m: state.__setitem__("env", state["env"] + 1))
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: state["status"])
    monkeypatch.setattr(docker_manager, "_compose_up",
                        lambda m, args, **kw: state["up"].append(args) or True)
    return m, state


@pytest.mark.parametrize("status", ["running", "unhealthy", "starting"])
def test_restart_recreates_an_existing_container_from_the_current_env(recreate_rig, status):
    m, state = recreate_rig
    state["status"] = status
    assert docker_manager.restart_container(m) is True
    assert state["env"] == 1
    assert state["up"] == [["up", "-d", "--force-recreate", "--no-build", "--pull", "never"]]


def test_an_exited_container_is_started_by_the_restart_button_only(recreate_rig):
    m, state = recreate_rig
    state["status"] = "stopped"
    assert docker_manager.restart_container(m) is False          # a saved tool filter
    assert docker_manager.restart_container(m, start_exited=True) is True
    assert len(state["up"]) == 1


@pytest.mark.parametrize("status", ["not_found", "error"])
def test_restart_never_starts_a_stopped_mcp(recreate_rig, status):
    m, state = recreate_rig
    state["status"] = status
    assert docker_manager.restart_container(m, start_exited=True) is False
    assert state["env"] == 1          # the new .env applies at its next start
    assert state["up"] == []


def test_restart_never_starts_a_disabled_mcp(recreate_rig):
    """A disable whose compose down failed can leave a container behind."""
    m, state = recreate_rig
    state["enabled"] = False
    assert docker_manager.restart_container(m, start_exited=True) is False
    assert state["up"] == []


# ── the MCP's own service decides its status ────────────────────────────────

_FILE_TOOLS_SHAPE = """services:
  file-tools:
    build: .
    container_name: file-tools-mcp
  collabora-proof:
    image: collabora/code:26
  collabora:
    image: collabora/code:26
    container_name: collabora-agent
"""


def _project(tmp_path, compose=_FILE_TOOLS_SHAPE):
    (tmp_path / "docker-compose.yml").write_text(compose)
    return _NS(name="file-tools", mcp_dir=tmp_path,
               server=_NS(runtime="docker", docker_compose="docker-compose.yml",
                          image="", service_name="", port=8932))


def _row(service, state, *, health="", image="", exit_code=0, name=""):
    return {"Service": service, "Name": name or service, "State": state, "Health": health,
            "Image": image, "ExitCode": exit_code}


def _state_of(monkeypatch, manifest, *rows):
    from core.config import deployment
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
    monkeypatch.setattr(docker_manager.subprocess, "run",
                        lambda *a, **k: _NS(returncode=0, stdout=_ps_lines(*rows), stderr=""))
    return docker_manager.container_state(manifest)


def test_a_sidecar_starting_never_reads_as_the_mcp_starting(tmp_path, monkeypatch):
    # The rows in compose's name order: Collabora's container sorts first and
    # its image's health check is still in its start period.
    status, images = _state_of(
        monkeypatch, _project(tmp_path),
        _row("collabora", "running", health="starting", image="collabora/code:26",
             name="collabora-agent"),
        _row("collabora-proof", "exited", image="collabora/code:26"),
        _row("file-tools", "running", image="file-tools-mcp", name="file-tools-mcp"),
    )
    assert status == docker_manager.RUNNING
    assert images == ["file-tools-mcp"]


def test_the_mcps_own_health_still_reads(tmp_path, monkeypatch):
    status, _ = _state_of(
        monkeypatch, _project(tmp_path),
        _row("collabora", "running", name="collabora-agent"),
        _row("file-tools", "running", health="unhealthy", name="file-tools-mcp"),
    )
    assert status == docker_manager.UNHEALTHY


@pytest.mark.parametrize("sidecar", [
    _row("collabora", "exited", exit_code=137, name="collabora-agent"),
    _row("collabora", "created", name="collabora-agent"),
    _row("collabora", "dead", name="collabora-agent"),
])
def test_a_dead_sidecar_reads_the_project_as_stopped(tmp_path, monkeypatch, sidecar):
    # So the boot's `up -d` brings Collabora back, as the first-row rule did.
    status, _ = _state_of(monkeypatch, _project(tmp_path), sidecar,
                          _row("file-tools", "running", name="file-tools-mcp"))
    assert status == docker_manager.STOPPED


def test_a_finished_one_shot_is_not_down(tmp_path, monkeypatch):
    status, _ = _state_of(
        monkeypatch, _project(tmp_path),
        _row("collabora-proof", "exited", exit_code=0),
        _row("file-tools", "running", name="file-tools-mcp"),
    )
    assert status == docker_manager.RUNNING


def test_no_row_of_the_mcps_own_service_reads_not_found(tmp_path, monkeypatch):
    status, images = _state_of(monkeypatch, _project(tmp_path),
                               _row("collabora", "running", image="collabora/code:26"))
    assert (status, images) == (docker_manager.NOT_FOUND, [])


def test_an_unreadable_compose_or_rows_without_a_service_keep_the_first_row_rule(
        tmp_path, monkeypatch):
    rows = (_row("collabora", "running", health="starting", name="collabora-agent"),
            _row("file-tools", "running", name="file-tools-mcp"))
    unreadable = _project(tmp_path, compose="services: [not, a, mapping")
    assert _state_of(monkeypatch, unreadable, *rows)[0] == docker_manager.STARTING
    no_service = [{k: v for k, v in r.items() if k != "Service"} for r in rows]
    assert _state_of(monkeypatch, _project(tmp_path), *no_service)[0] == docker_manager.STARTING


def test_the_docker_compose_mode_reads_the_pull_forms_target(tmp_path, monkeypatch):
    from core.config import deployment
    pull_form = """services:
  server:
    image: ghcr.io/x/crm:1
    networks: {otodock: {aliases: [crm]}}
  cache:
    image: redis:7
"""
    manifest = _NS(name="crm", mcp_dir=tmp_path,
                   server=_NS(runtime="docker", docker_compose="docker-compose.yml",
                              image="ghcr.io/x/crm:1", service_name="", port=9000))
    (tmp_path / "docker-compose.yml").write_text(pull_form)
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(docker_manager.subprocess, "run", lambda *a, **k: _NS(
        returncode=0, stderr="", stdout=_ps_lines(
            _row("cache", "running", health="starting", image="redis:7"),
            _row("server", "running", image="ghcr.io/x/crm:1"))))
    assert docker_manager.container_state(manifest) == (docker_manager.RUNNING, ["ghcr.io/x/crm:1"])


# ── the engine floor of a volume subpath mount ──────────────────────────────

_SUBPATH_SHAPE = """services:
  app:
    image: x
    volumes:
      - type: volume
        source: keys
        target: /etc/key
        volume:
          subpath: key
volumes:
  keys: {}
"""


@pytest.mark.parametrize("api, warned", [("1.44", True), ("1.45", False), ("1.47", False), ("", False)])
def test_an_old_engine_under_a_subpath_mount_is_named_at_boot(
        tmp_path, monkeypatch, caplog, api, warned):
    import logging
    manifest = _project(tmp_path, compose=_SUBPATH_SHAPE)
    calls = []

    def _run(cmd, *a, **k):
        calls.append(cmd)
        return _NS(returncode=0 if api else 1, stdout=api + "\n", stderr="")

    monkeypatch.setattr(docker_manager.subprocess, "run", _run)
    cache: list = []
    with caplog.at_level(logging.WARNING, logger="otodock.docker-manager"):
        docker_manager._check_engine_for_subpath(manifest, cache)
        docker_manager._check_engine_for_subpath(manifest, cache)
    assert len(calls) == 1 and calls[0][:2] == ["docker", "version"]
    assert ("Docker Engine 26" in caplog.text) is warned


def test_no_subpath_mount_never_asks_the_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_manager.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked the engine")))
    docker_manager._check_engine_for_subpath(_project(tmp_path), [])
    docker_manager._check_engine_for_subpath(_project(tmp_path, compose="::"), [])


# ---------------------------------------------------------------------------
# The boot's build: bounded, retried once, skipped when current, concurrent
# ---------------------------------------------------------------------------

import json as _json
import threading as _threading
import time as _time


def _gone(pid: int) -> bool:
    """The process ended (no entry, or a zombie its new parent reaps)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state == "Z"


def test_a_silent_build_is_ended_at_its_stall_bound(file_tools_manifest):
    started = _time.monotonic()
    with pytest.raises(docker_manager.ComposeStalled):
        docker_manager._run_compose_streaming(
            file_tools_manifest, ["bash", "-c", "echo Building >&2; sleep 30"],
            timeout=60, stall=1.0,
        )
    assert _time.monotonic() - started < bound(10)


def test_the_whole_group_is_ended(file_tools_manifest, tmp_path):
    """compose's buildx child shares its group: it goes with it."""
    pidfile = tmp_path / "child.pid"
    with pytest.raises(_subprocess.TimeoutExpired):
        docker_manager._run_compose_streaming(
            file_tools_manifest, ["bash", "-c", f"sleep 60 & echo $! > {pidfile}; sleep 60"],
            timeout=1,
        )
    child = int(pidfile.read_text())
    deadline = _time.monotonic() + 5
    while not _gone(child) and _time.monotonic() < deadline:
        _time.sleep(0.1)
    assert _gone(child)
    assert not docker_manager._live_groups, "an ended run stays registered"


def test_a_child_holding_the_pipe_never_blocks_the_reader(file_tools_manifest):
    """compose exited, a process outside its group keeps stderr open."""
    started = _time.monotonic()
    rc, err = docker_manager._run_compose_streaming(
        file_tools_manifest, ["bash", "-c", "(setsid sleep 6 &); echo done >&2; exit 0"],
        timeout=60,
    )
    assert rc == 0 and "done" in err
    assert _time.monotonic() - started < bound(5)


def test_shutdown_ends_a_running_build(file_tools_manifest):
    result: dict = {}

    def _run():
        try:
            docker_manager._run_compose_streaming(
                file_tools_manifest, ["bash", "-c", "sleep 60"], timeout=120)
        except BaseException as e:  # noqa: BLE001  # the thread reports what ended it
            result["exc"] = e
        else:
            result["exc"] = None

    worker = _threading.Thread(target=_run)
    worker.start()
    deadline = _time.monotonic() + 5
    while not docker_manager._live_groups and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert docker_manager.end_running_compose() == 1
    worker.join(timeout=15)
    assert not worker.is_alive()
    # Ended by the shutdown, not timed out: the caller logs no failure for it.
    assert isinstance(result["exc"], docker_manager.ComposeEnded)


def test_a_stalled_build_gets_one_more_attempt(fake_docker_manifest, monkeypatch, caplog):
    runs = []

    def _run(manifest, cmd, *, timeout, cwd=None, stall=None):
        runs.append(stall)
        if len(runs) == 1:
            raise docker_manager.ComposeStalled(cmd, stall)
        return 0, ""

    monkeypatch.setattr(docker_manager, "_run_compose_streaming", _run)
    assert docker_manager._run_up(fake_docker_manifest, ["up", "-d", "--build"], 900) == (0, "")
    assert runs == [docker_manager._BUILD_STALL_S] * 2


def test_an_up_without_a_build_is_not_retried_nor_stall_bounded(fake_docker_manifest, monkeypatch):
    runs = []

    def _run(manifest, cmd, *, timeout, cwd=None, stall=None):
        runs.append(stall)
        raise _subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(docker_manager, "_run_compose_streaming", _run)
    with pytest.raises(_subprocess.TimeoutExpired):
        docker_manager._run_up(fake_docker_manifest, ["up", "-d"], 120)
    assert runs == [None]


def test_a_build_that_stalls_twice_gives_up_with_the_way_by_hand(
    fake_docker_manifest, monkeypatch, caplog,
):
    def _run(manifest, cmd, *, timeout, cwd=None, stall=None):
        raise docker_manager.ComposeStalled(cmd, stall)

    monkeypatch.setattr(docker_manager, "_run_compose_streaming", _run)
    recorded = []
    monkeypatch.setattr(docker_manager, "_record_compose_hash", recorded.append)
    caplog.set_level(logging.ERROR, logger="otodock.docker-manager")
    assert docker_manager._compose_up(
        fake_docker_manifest, ["up", "-d", "--build"], timeout=900) is False
    assert recorded == []
    message = caplog.records[-1].getMessage()
    assert "printed nothing for 600 s twice" in message
    assert "running containers are unchanged" in message
    assert "By hand: cd " in message and "up -d --build" in message


def test_the_hash_file_keeps_each_line_its_writer_does_not_set(fake_docker_manifest):
    m = fake_docker_manifest
    path = m.mcp_dir / docker_manager._COMPOSE_HASH_NAME
    path.write_text("abc\n")                      # a file from before the build record
    assert docker_manager._read_hash_file(m) == ("abc", None)
    docker_manager._write_hash_file(m, record={"hash": "h1", "images": {"i": "sha256:1"}})
    assert docker_manager._read_hash_file(m) == ("abc", {"hash": "h1", "images": {"i": "sha256:1"}})
    docker_manager._write_hash_file(m, compose="def")
    assert docker_manager._read_recorded_compose_hash(m) == "def"
    assert docker_manager._read_hash_file(m)[1] == {"hash": "h1", "images": {"i": "sha256:1"}}
    path.write_text("def\nnot json\n")
    assert docker_manager._read_hash_file(m) == ("def", None)


def _compose_config_of(monkeypatch, services: dict):
    class _R:
        returncode = 0
        stdout = _json.dumps({"services": services})
        stderr = ""
    monkeypatch.setattr(docker_manager.subprocess, "run", lambda *a, **k: _R())


def test_the_build_state_follows_the_sources_and_nothing_else(
    fake_docker_manifest, tmp_path, monkeypatch,
):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")
    (ctx / "server.py").write_text("print(1)\n")
    _compose_config_of(monkeypatch, {
        "fake": {"build": {"context": str(ctx), "dockerfile": "Dockerfile"}},
        "sidecar": {"image": "collabora/code:26.04"},
    })
    first = docker_manager._build_state(fake_docker_manifest)
    assert first is not None and first[1] == ["otodock-" + docker_manager.config.INSTALL_ID.lower()
                                             + "-mcp-fake-fake"]
    for noise in ("venv/bin/python", "__pycache__/x.pyc", ".env", "node_modules/a/b.js",
                  docker_manager._COMPOSE_HASH_NAME, "docker-compose.override.yml"):
        (ctx / noise).parent.mkdir(parents=True, exist_ok=True)
        (ctx / noise).write_text("noise")
    assert docker_manager._build_state(fake_docker_manifest) == first
    (ctx / "server.py").write_text("print(2)\n")
    assert docker_manager._build_state(fake_docker_manifest)[0] != first[0]


@pytest.mark.parametrize("build", [
    {"context": "https://github.com/x/y.git"},
    {"context": "{ctx}", "additional_contexts": {"base": "../base"}},
    {"context": "{ctx}", "dockerfile_inline": "FROM scratch"},
])
def test_a_build_not_made_of_local_files_is_never_judged_current(
    fake_docker_manifest, tmp_path, monkeypatch, build,
):
    build = {k: (v.format(ctx=tmp_path) if isinstance(v, str) else v) for k, v in build.items()}
    _compose_config_of(monkeypatch, {"fake": {"build": build}})
    assert docker_manager._build_state(fake_docker_manifest) is None
    assert docker_manager._build_is_current(fake_docker_manifest, None) is False


def _record(m, h, images):
    docker_manager._write_hash_file(m, compose="c", record={"hash": h, "images": images})


def test_the_build_is_current_only_when_hash_and_image_ids_agree(fake_docker_manifest, monkeypatch):
    m = fake_docker_manifest
    ids = {"img": "sha256:1"}
    monkeypatch.setattr(docker_manager, "_image_id", ids.get)
    state = ("h1", ["img"])
    assert docker_manager._build_is_current(m, state) is False            # nothing recorded
    _record(m, "h1", {"img": "sha256:1"})
    assert docker_manager._build_is_current(m, state) is True
    assert docker_manager._build_is_current(m, ("h2", ["img"])) is False  # the sources changed
    ids["img"] = "sha256:2"                                               # rebuilt or retagged by hand
    assert docker_manager._build_is_current(m, state) is False
    ids.pop("img")                                                        # the image is gone
    assert docker_manager._build_is_current(m, state) is False


def test_a_current_image_starts_without_a_build(fake_docker_manifest, monkeypatch):
    ups = []
    monkeypatch.setattr(docker_manager, "_build_state", lambda m: ("h1", ["img"]))
    monkeypatch.setattr(docker_manager, "_build_is_current", lambda m, s: True)
    monkeypatch.setattr(docker_manager, "_compose_up",
                        lambda m, args, *, timeout, t1_retry=False: ups.append((args, timeout)) or True)
    monkeypatch.setattr(docker_manager, "_record_build", lambda m, s: ups.append("recorded"))
    assert docker_manager._build_up(fake_docker_manifest, ["up", "-d"]) is True
    assert ups == [(["up", "-d", "--no-build"], 120)]


@pytest.mark.parametrize("ok", [True, False])
def test_a_changed_source_builds_and_records_only_a_success(fake_docker_manifest, monkeypatch, ok):
    ups = []
    monkeypatch.setattr(docker_manager, "_build_state", lambda m: ("h2", ["img"]))
    monkeypatch.setattr(docker_manager, "_build_is_current", lambda m, s: False)
    monkeypatch.setattr(docker_manager, "_compose_up",
                        lambda m, args, *, timeout, t1_retry=False: ups.append((args, timeout)) or ok)
    monkeypatch.setattr(docker_manager, "_record_build", lambda m, s: ups.append(("recorded", s)))
    assert docker_manager._build_up(fake_docker_manifest, ["up", "-d", "--force-recreate"]) is ok
    expected = [(["up", "-d", "--force-recreate", "--build"], 900)]
    assert ups == (expected + [("recorded", ("h2", ["img"]))] if ok else expected)


def test_a_build_record_holds_the_hash_from_before_the_build(fake_docker_manifest, monkeypatch):
    from core.config import deployment
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_image_id", lambda image: "sha256:9")
    docker_manager._record_build(fake_docker_manifest, ("before", ["img"]))
    assert docker_manager._read_hash_file(fake_docker_manifest)[1] == {
        "hash": "before", "images": {"img": "sha256:9"}}


def test_a_slow_project_never_holds_the_next_ones_start(fake_docker_manifest, tmp_path, monkeypatch):
    from core.config import deployment
    from services.mcp import compose_rewrite, mcp_registry
    from storage.mcp import mcp_store

    other = SimpleNamespace(**{**vars(fake_docker_manifest), "name": "other"})
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_warn_foreign_mcp_leftovers", lambda: None)
    monkeypatch.setattr(mcp_store, "get_all_mcp_states", lambda: {"fake": True, "other": True})
    monkeypatch.setattr(mcp_registry, "get_all_manifests",
                        lambda: {"fake": fake_docker_manifest, "other": other})
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: False)
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: "not_found")
    monkeypatch.setattr(compose_rewrite, "ensure_t1_override", lambda m, **kw: None)
    other_started = _threading.Event()
    seen: dict = {}

    def _start(manifest, force_recreate=False):
        if manifest.name == "fake":
            seen["other_started_meanwhile"] = other_started.wait(timeout=10)
        else:
            other_started.set()
        return True

    monkeypatch.setattr(docker_manager, "start_container", _start)
    docker_manager.startup_docker_mcps()
    assert seen == {"other_started_meanwhile": True}


def test_a_project_whose_bring_up_raises_is_logged_and_the_others_go_on(
    fake_docker_manifest, monkeypatch, caplog,
):
    from core.config import deployment
    from services.mcp import mcp_registry
    from storage.mcp import mcp_store

    other = SimpleNamespace(**{**vars(fake_docker_manifest), "name": "other"})
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_warn_foreign_mcp_leftovers", lambda: None)
    monkeypatch.setattr(mcp_store, "get_all_mcp_states", lambda: {"fake": True, "other": True})
    monkeypatch.setattr(mcp_registry, "get_all_manifests",
                        lambda: {"fake": fake_docker_manifest, "other": other})
    done = []

    def _bring_up(name, manifest):
        if name == "fake":
            raise RuntimeError("boom")
        done.append(name)

    monkeypatch.setattr(docker_manager, "_bring_up", _bring_up)
    caplog.set_level(logging.ERROR, logger="otodock.docker-manager")
    docker_manager.startup_docker_mcps()
    assert done == ["other"]
    assert any("fake: bring-up failed" in r.getMessage() for r in caplog.records)


def test_a_shutdown_mid_bring_up_starts_nothing_more(fake_docker_manifest, tmp_path, monkeypatch):
    """Five projects, four workers: the shutdown ends the four builds that
    run, the fifth never starts, the stalled ones are not started again and
    the bring-up returns (its thread no longer holds the process)."""
    from core.config import deployment
    from services.mcp import compose_rewrite, mcp_registry
    from storage.mcp import mcp_store

    spawns = tmp_path / "spawns.log"
    names = [f"p{i}" for i in range(5)]
    manifests = {n: SimpleNamespace(**{**vars(fake_docker_manifest), "name": n}) for n in names}
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(docker_manager, "_warn_foreign_mcp_leftovers", lambda: None)
    monkeypatch.setattr(mcp_store, "get_all_mcp_states", lambda: dict.fromkeys(names, True))
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: manifests)
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: False)
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: "not_found")
    monkeypatch.setattr(compose_rewrite, "ensure_t1_override", lambda m, **kw: None)
    monkeypatch.setattr(docker_manager, "_compose_cmd", lambda m, *a: [
        "bash", "-c", f'echo "{m.name}" >> {spawns}; echo Building >&2; sleep 60'])
    monkeypatch.setattr(docker_manager, "start_container",
                        lambda m, force_recreate=False: docker_manager._compose_up(
                            m, ["up", "-d", "--build"], timeout=120))
    bring_up = _threading.Thread(target=docker_manager.startup_docker_mcps)
    bring_up.start()
    deadline = _time.monotonic() + 10
    while len(docker_manager._live_groups) < 4 and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert len(docker_manager._live_groups) == 4
    assert docker_manager.end_running_compose() == 4
    bring_up.join(timeout=30)
    assert not bring_up.is_alive()
    assert sorted(spawns.read_text().split()) == names[:4]
    assert docker_manager._live_groups == {}


def test_a_stalled_build_is_not_started_again_once_the_proxy_stops(fake_docker_manifest, monkeypatch):
    """The shutdown lands between a stalled attempt and its retry."""
    runs = []

    def _run(manifest, cmd, *, timeout, cwd=None, stall=None):
        runs.append(cmd)
        docker_manager._stopping.set()
        raise docker_manager.ComposeStalled(cmd, stall)

    monkeypatch.setattr(docker_manager, "_run_compose_streaming", _run)
    with pytest.raises(docker_manager.ComposeStalled):
        docker_manager._run_up(fake_docker_manifest, ["up", "-d", "--build"], 900)
    assert len(runs) == 1


def test_no_compose_run_starts_once_the_proxy_stops(file_tools_manifest, tmp_path, caplog):
    marker = tmp_path / "ran"
    docker_manager.end_running_compose()
    with pytest.raises(docker_manager.ComposeEnded):
        docker_manager._run_compose_streaming(
            file_tools_manifest, ["bash", "-c", f"touch {marker}"], timeout=30)
    assert not marker.exists()
    caplog.set_level(logging.INFO, logger="otodock.docker-manager")
    assert docker_manager._compose_up(file_tools_manifest, ["up", "-d"], timeout=30) is False
    assert any("not started, the proxy is stopping" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_shutdown_lets_a_recreate_in_progress_finish(file_tools_manifest, monkeypatch):
    """A run that reached the containers gets its grace: cut there, compose
    leaves the new container under a temporary name."""
    monkeypatch.setattr(docker_manager, "_RECREATE_GRACE_S", 10.0)
    result: dict = {}

    def _run():
        result["out"] = docker_manager._run_compose_streaming(
            file_tools_manifest,
            ["bash", "-c", "echo ' Container otodock-x-mcp-fake  Recreate' >&2; sleep 1; "
                           "echo ' Container otodock-x-mcp-fake  Started' >&2"],
            timeout=60)

    worker = _threading.Thread(target=_run)
    worker.start()
    deadline = _time.monotonic() + 5
    while not any(r.containers for r in list(docker_manager._live_groups.values())) \
            and _time.monotonic() < deadline:
        _time.sleep(0.05)
    docker_manager.end_running_compose()
    worker.join(timeout=20)
    assert result["out"][0] == 0 and "Started" in result["out"][1]


def test_an_ended_run_says_whether_compose_reached_the_containers(file_tools_manifest):
    with pytest.raises(_subprocess.TimeoutExpired) as building:
        docker_manager._run_compose_streaming(
            file_tools_manifest, ["bash", "-c", "echo 'Image x Building' >&2; sleep 30"], timeout=1)
    assert building.value.recreating is False
    with pytest.raises(_subprocess.TimeoutExpired) as recreating:
        docker_manager._run_compose_streaming(
            file_tools_manifest,
            ["bash", "-c", "echo '✔ Container otodock-x-mcp-fake  Recreate' >&2; sleep 30"], timeout=1)
    assert recreating.value.recreating is True


def _docker_names(monkeypatch, names, *, rename_rc=0):
    calls = []

    class _R:
        def __init__(self, stdout="", returncode=0):
            self.stdout, self.returncode, self.stderr = stdout, returncode, ""

    def _run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _R("\n".join(names) + "\n")
        return _R(returncode=rename_rc)

    monkeypatch.setattr(docker_manager.subprocess, "run", _run)
    return calls


def test_a_cut_recreate_gets_its_container_renamed_back(fake_docker_manifest, monkeypatch):
    real = docker_manager._project_name(fake_docker_manifest)
    calls = _docker_names(monkeypatch, [f"0123456789ab_{real}", f"{real}-sidecar-1"])
    note = docker_manager._settle_interrupted_recreate(fake_docker_manifest)
    assert ["docker", "rename", f"0123456789ab_{real}", real] in calls
    assert note == f"0123456789ab_{real} renamed back to {real}"


def test_a_cut_recreate_beside_the_old_container_names_the_way_out(fake_docker_manifest, monkeypatch):
    real = docker_manager._project_name(fake_docker_manifest)
    calls = _docker_names(monkeypatch, [f"0123456789ab_{real}", real])
    note = docker_manager._settle_interrupted_recreate(fake_docker_manifest)
    assert not [c for c in calls if c[:2] == ["docker", "rename"]]
    assert note == (f"0123456789ab_{real} is left beside {real}: docker stop {real}; "
                    f"docker rm {real}; docker rename 0123456789ab_{real} {real}")


def test_a_timeout_mid_recreate_is_not_called_unchanged(fake_docker_manifest, monkeypatch, caplog):
    def _run(manifest, cmd, *, timeout, cwd=None, stall=None):
        e = _subprocess.TimeoutExpired(cmd, timeout)
        e.recreating = True
        raise e

    monkeypatch.setattr(docker_manager, "_run_compose_streaming", _run)
    monkeypatch.setattr(docker_manager, "_settle_interrupted_recreate", lambda m: "x renamed back to y")
    caplog.set_level(logging.ERROR, logger="otodock.docker-manager")
    assert docker_manager._compose_up(fake_docker_manifest, ["up", "-d", "--build"], timeout=900) is False
    message = caplog.records[-1].getMessage()
    assert "while it recreated the containers (x renamed back to y)" in message
    assert "unchanged" not in message


def test_a_stall_then_a_timeout_names_both(fake_docker_manifest, monkeypatch, caplog):
    raised = iter([docker_manager.ComposeStalled(["c"], 600.0), _subprocess.TimeoutExpired(["c"], 900)])

    def _run(manifest, cmd, *, timeout, cwd=None, stall=None):
        raise next(raised)

    monkeypatch.setattr(docker_manager, "_run_compose_streaming", _run)
    caplog.set_level(logging.ERROR, logger="otodock.docker-manager")
    assert docker_manager._compose_up(fake_docker_manifest, ["up", "-d", "--build"], timeout=900) is False
    assert "its build printed nothing for 600 s, then ran past 900 s; its running containers are " \
           "unchanged" in caplog.records[-1].getMessage()


def test_a_context_past_the_cap_is_never_read(fake_docker_manifest, tmp_path, monkeypatch):
    """The size decides before a file is opened: a model or a dump in a
    build context is never read whole, four workers at once."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")
    (ctx / "model.bin").write_bytes(b"x" * 4096)
    _compose_config_of(monkeypatch, {"fake": {"build": {"context": str(ctx), "dockerfile": "Dockerfile"}}})
    monkeypatch.setattr(docker_manager, "_BUILD_HASH_MAX_BYTES", 1024)
    opened = []
    real_open = open

    def _open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _open)
    assert docker_manager._build_state(fake_docker_manifest) is None
    assert not [p for p in opened if p.endswith("model.bin")]
