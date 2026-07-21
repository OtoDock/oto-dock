"""Import-time config-path guard.

The compose file bind-mounts ``${OTODOCK_ENV_FILE:-./.env}`` onto the
config.env path; a missing host file makes Docker create an empty DIRECTORY
there, which dotenv silently reads as "no config" and which crash-loops the
proxy at the first secret persist (IsADirectoryError) with no hint of the
cause — the 2026-07-19 internal-upgrade outage. The guard turns that into an
immediate, self-explaining fatal error.
"""

import pytest

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
