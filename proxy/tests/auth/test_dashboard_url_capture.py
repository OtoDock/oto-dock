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
