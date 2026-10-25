"""First-run DASHBOARD_PUBLIC_URL capture (setup wizard, 2026-07-19).

The compose file ALWAYS injects a localhost default into the container env,
so "unset" is detectable only from the FILE view (config.env — the user's
explicit setting). Explicit config wins; localhost origins never capture.
"""

from __future__ import annotations

import pytest

import config


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    env_file = tmp_path / "config.env"
    env_file.write_text("")
    monkeypatch.setattr(config, "_config_env", env_file)
    monkeypatch.setattr(config, "_file_cfg", {})
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    return env_file


class TestCaptureDashboardPublicUrl:
    def test_captures_when_unset(self, cfg_env):
        assert config.capture_dashboard_public_url("http://192.168.1.10:8400")
        assert "DASHBOARD_PUBLIC_URL=http://192.168.1.10:8400" in cfg_env.read_text()

    def test_captures_over_compose_localhost_default(self, cfg_env, monkeypatch):
        # Compose always injects the localhost default via the environment.
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "http://localhost:8400")
        assert config.capture_dashboard_public_url("https://oto.example.com")
        assert "DASHBOARD_PUBLIC_URL=https://oto.example.com" in cfg_env.read_text()

    def test_refuses_when_file_set(self, cfg_env):
        config._file_cfg["DASHBOARD_PUBLIC_URL"] = "https://mine.example.com"
        assert not config.capture_dashboard_public_url("http://192.168.1.10:8400")
        assert "192.168.1.10" not in cfg_env.read_text()

    def test_refuses_when_env_set_to_real_origin(self, cfg_env, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://mine.example.com")
        assert not config.capture_dashboard_public_url("http://192.168.1.10:8400")

    def test_localhost_origin_never_captures(self, cfg_env):
        assert not config.capture_dashboard_public_url("http://localhost:8400")
        assert not config.capture_dashboard_public_url("http://127.0.0.1:8400")
        assert cfg_env.read_text() == ""

    def test_garbage_origin_refused(self, cfg_env):
        assert not config.capture_dashboard_public_url("not-a-url")
        assert not config.capture_dashboard_public_url("http://a b/c")
        assert cfg_env.read_text() == ""

    def test_trailing_slash_normalized(self, cfg_env):
        assert config.capture_dashboard_public_url("http://192.168.1.10:8400/")
        assert "DASHBOARD_PUBLIC_URL=http://192.168.1.10:8400\n" in cfg_env.read_text()


class TestCollaboraScheme:
    """Collabora 26.04 has no shell to derive its scheme from
    DASHBOARD_PUBLIC_URL: the wizard and the boot write
    COLLABORA_SSL_TERMINATION beside the URL instead."""

    def test_an_https_capture_writes_true_and_an_http_one_false(self, cfg_env):
        assert config.capture_dashboard_public_url("https://oto.example.com")
        assert "COLLABORA_SSL_TERMINATION=true" in cfg_env.read_text()
        cfg_env.write_text("")
        config._file_cfg.clear()
        assert config.capture_dashboard_public_url("http://192.168.1.10:8400")
        text = cfg_env.read_text()
        assert "COLLABORA_SSL_TERMINATION=false" in text and "=true" not in text

    def test_the_boot_appends_a_missing_line(self, cfg_env, capsys):
        cfg_env.write_text("DASHBOARD_PUBLIC_URL=https://oto.example.com\n")
        config._file_cfg.update({"DASHBOARD_PUBLIC_URL": "https://oto.example.com"})
        assert config.sync_collabora_scheme() == "true"
        assert cfg_env.read_text().splitlines() == [
            "DASHBOARD_PUBLIC_URL=https://oto.example.com", "COLLABORA_SSL_TERMINATION=true"]
        assert "docker compose up -d" in capsys.readouterr().err
        assert config.sync_collabora_scheme() is None  # now in step: nothing written

    def test_the_boot_rewrites_a_line_that_disagrees(self, cfg_env):
        cfg_env.write_text("DASHBOARD_PUBLIC_URL=http://h:8400\n"
                           "# COLLABORA_SSL_TERMINATION=true\n"
                           "export COLLABORA_SSL_TERMINATION=true\n")
        config._file_cfg.update({"DASHBOARD_PUBLIC_URL": "http://h:8400",
                                 "COLLABORA_SSL_TERMINATION": "true"})
        assert config.sync_collabora_scheme() == "false"
        assert cfg_env.read_text().splitlines() == [
            "DASHBOARD_PUBLIC_URL=http://h:8400", "# COLLABORA_SSL_TERMINATION=true",
            "COLLABORA_SSL_TERMINATION=false"]

    @pytest.mark.parametrize("extra", [
        {},  # no DASHBOARD_PUBLIC_URL in the file
        {"DASHBOARD_PUBLIC_URL": "https://oto.example.com",
         "COLLABORA_URL": "https://collabora.example.com"},  # Collabora on its own address
        {"DASHBOARD_PUBLIC_URL": "https://oto.example.com", "COLLABORA_SSL_TERMINATION": "yes"},
    ])
    def test_the_boot_leaves_alone(self, cfg_env, extra):
        config._file_cfg.update(extra)
        assert config.sync_collabora_scheme() is None
        assert cfg_env.read_text() == ""

    def test_a_file_that_cannot_be_written_is_named(self, cfg_env, monkeypatch, capsys):
        config._file_cfg.update({"DASHBOARD_PUBLIC_URL": "https://oto.example.com"})

        def _refuse(text, *, append):
            raise PermissionError("read-only")

        monkeypatch.setattr(config, "_write_config_env", _refuse)
        assert config.sync_collabora_scheme() is None
        assert "set it by hand" in capsys.readouterr().err
