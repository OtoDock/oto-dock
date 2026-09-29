"""Import-time config-path guard.

The compose file bind-mounts ``${OTODOCK_ENV_FILE:-./.env}`` onto the
config.env path; a missing host file makes Docker create an empty DIRECTORY
there, which dotenv silently reads as "no config" and which crash-loops the
proxy at the first secret persist (IsADirectoryError) with no hint of the
cause — the 2026-07-19 internal-upgrade outage. The guard turns that into an
immediate, self-explaining fatal error.
"""

import stat

import pytest

import config
from config import _reject_directory_config


def test_directory_config_is_fatal_and_self_explaining(tmp_path):
    fake = tmp_path / "config.env"
    fake.mkdir()
    with pytest.raises(SystemExit) as exc:
        _reject_directory_config(fake)
    msg = str(exc.value)
    assert str(fake) in msg
    assert "directory, not a file" in msg
    assert "docker compose down" in msg
    assert "mv config.env .env" in msg
    assert "OTODOCK_ENV_FILE" in msg


def test_regular_file_passes(tmp_path):
    f = tmp_path / "config.env"
    f.write_text("KEY=value\n")
    _reject_directory_config(f)  # no raise


def test_missing_path_passes(tmp_path):
    # A missing config.env is legitimate (dev defaults + generated secrets).
    _reject_directory_config(tmp_path / "config.env")  # no raise


# config.env holds every master secret: the proxy writes it owner-only
# and tightens a copy other accounts can read.

def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_persisted_secret_creates_config_env_owner_only(tmp_path, monkeypatch):
    env = tmp_path / "config.env"
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "_config_env", env)
    monkeypatch.setattr(config, "_file_cfg", {})
    config._persist_secret("NEW_SECRET", "s3cret")
    assert env.read_text() == "NEW_SECRET=s3cret\n"
    assert _mode(env) == 0o600


def test_persisted_secret_tightens_an_existing_file(tmp_path, monkeypatch):
    env = tmp_path / "config.env"
    env.write_text("KEEP=1\nBLANK=\n")
    env.chmod(0o664)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "_config_env", env)
    monkeypatch.setattr(config, "_file_cfg", {})
    config._persist_secret("APPENDED", "a")
    assert _mode(env) == 0o600
    env.chmod(0o664)
    config._persist_secret("BLANK", "b")
    assert env.read_text() == "KEEP=1\nBLANK=b\nAPPENDED=a\n"
    assert _mode(env) == 0o600


def test_readable_config_env_is_tightened_at_boot(tmp_path, capsys):
    env = tmp_path / "config.env"
    env.write_text("JWT_SECRET=x\n")
    env.chmod(0o644)
    config._tighten_config_mode(env, in_container=False)
    assert _mode(env) == 0o600
    assert "set to 0600" in capsys.readouterr().err
    config._tighten_config_mode(env, in_container=False)  # already private: silent
    assert capsys.readouterr().err == ""


def test_a_file_of_another_owner_is_reported_only_when_everyone_can_read_it(
        tmp_path, monkeypatch, capsys):
    env = tmp_path / "config.env"
    env.write_text("JWT_SECRET=x\n")
    monkeypatch.setattr(config.os, "geteuid", lambda: env.stat().st_uid + 1)
    env.chmod(0o640)  # root:otodock 0640 is a valid layout
    config._tighten_config_mode(env, in_container=False)
    assert _mode(env) == 0o640 and capsys.readouterr().err == ""
    env.chmod(0o644)
    config._tighten_config_mode(env, in_container=False)
    assert _mode(env) == 0o644
    err = capsys.readouterr().err
    assert "WARNING" in err and "mode 600" in err


def test_in_a_container_the_host_fixes_the_mode(tmp_path, monkeypatch, capsys):
    # The file is the host's bind-mounted .env: the host's docker compose
    # must keep reading it, so the proxy never changes its mode.
    env = tmp_path / "config.env"
    env.write_text("KEEP=1\n")
    env.chmod(0o664)
    config._tighten_config_mode(env, in_container=True)
    assert _mode(env) == 0o664
    assert "On the Docker host: chmod 600 .env" in capsys.readouterr().err
    monkeypatch.setattr(config, "_config_env", env)
    monkeypatch.setattr(config, "_file_cfg", {})
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    config._persist_secret("NEW_SECRET", "s")
    assert _mode(env) == 0o664


def test_an_unreadable_config_env_is_fatal_and_self_explaining(tmp_path, monkeypatch):
    env = tmp_path / "config.env"
    env.write_text("JWT_SECRET=x\n")
    monkeypatch.setattr(config.os, "access", lambda *_a, **_k: False)
    with pytest.raises(SystemExit) as exc:
        config._reject_unreadable_config(env)
    assert "not readable" in str(exc.value) and "mode 600" in str(exc.value)


def test_https_container_without_trusted_proxy_is_reported(capsys):
    assert config._warn_untrusted_edge(True, "https://otodock.example.com", [])
    assert "TRUSTED_PROXY" in capsys.readouterr().err
    assert not config._warn_untrusted_edge(True, "https://otodock.example.com", ["10.200.0.1"])
    assert not config._warn_untrusted_edge(True, "http://192.168.1.10:8400", [])
    # Bare metal: a same-host edge connects from 127.0.0.1, which is trusted.
    assert not config._warn_untrusted_edge(False, "https://otodock.example.com", [])
