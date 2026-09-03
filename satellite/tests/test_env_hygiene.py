"""Tests for satellite/host/env_hygiene.py — child-env secret curation.

Denylist semantics: drop operator AMBIENT secret-VALUE vars, keep everything else
(system / runtime / GUI / non-secret vendor config). The platform config["env"]
overlay (applied by the caller AFTER this) is out of scope here.
"""

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from satellite.host.env_hygiene import curate_satellite_env, _is_secret_name


STRIPPED = [
    "GH_TOKEN", "GITHUB_TOKEN", "OTODOCK_RELAY_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
    "MY_PASSWORD", "DB_PASSWD", "FOO_PRIVATE_KEY", "SOME_APIKEY",
    "STRIPE_SECRET_KEY", "NPM_TOKEN", "HF_TOKEN", "RANDOM_KEY", "SSH_KEY",
    "JWT_SECRET", "CLIENT_SECRET", "ENCRYPTION_KEY",
]

KEPT = [
    # system / shell
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM",
    "TMPDIR", "TZ", "PWD",
    # Windows system / runtime
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
    "USERPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)", "WINDIR", "TEMP",
    # GUI / desktop (device MCPs need these)
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    # non-secret vendor CONFIG (must NOT be stripped — audit: no vendor prefixes)
    "AWS_PROFILE", "AWS_REGION", "AWS_CONFIG_FILE", "AWS_DEFAULT_REGION",
    "OPENAI_BASE_URL", "AZURE_CONFIG_DIR",
    # path POINTERS that look secret-ish but point to files (zero protection if
    # stripped + breaks the tool) — NOT matched (no CREDENTIAL pattern).
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "KUBECONFIG",
    # satellite/runtime
    "PYTHONUTF8", "PYTHONIOENCODING", "VIRTUAL_ENV", "NODE_OPTIONS",
]


class TestStripsSecrets:
    def test_secret_names_stripped(self):
        env = {n: "x" for n in STRIPPED}
        out = curate_satellite_env(env)
        leaked = [n for n in STRIPPED if n in out]
        assert not leaked, f"secret vars leaked: {leaked}"

    def test_lowercase_secret_names_stripped(self):
        # Matching is case-insensitive.
        out = curate_satellite_env({"gh_token": "x", "my_api_key": "y"})
        assert out == {}


class TestKeepsNonSecrets:
    def test_system_runtime_gui_kept(self):
        env = {n: "x" for n in KEPT}
        out = curate_satellite_env(env)
        dropped = [n for n in KEPT if n not in out]
        assert not dropped, f"non-secret vars wrongly stripped: {dropped}"

    def test_google_app_credentials_kept(self):
        # Path pointer (ADC) — would break Google auth if stripped; the operator's
        # uid reads the file regardless so stripping gives zero protection.
        out = curate_satellite_env({"GOOGLE_APPLICATION_CREDENTIALS": "/path/key.json"})
        assert out == {"GOOGLE_APPLICATION_CREDENTIALS": "/path/key.json"}

    def test_aws_config_not_stripped(self):
        # No broad vendor PREFIX stripping (audit) — config survives.
        out = curate_satellite_env({"AWS_PROFILE": "dev", "AWS_REGION": "us-east-1"})
        assert out == {"AWS_PROFILE": "dev", "AWS_REGION": "us-east-1"}


class TestMixedAndPurity:
    def test_mixed_env(self):
        env = {"PATH": "/usr/bin", "GH_TOKEN": "ghp_x", "DISPLAY": ":0",
               "OPENAI_API_KEY": "sk-x", "AWS_PROFILE": "dev"}
        out = curate_satellite_env(env)
        assert out == {"PATH": "/usr/bin", "DISPLAY": ":0", "AWS_PROFILE": "dev"}

    def test_source_not_mutated(self):
        env = {"GH_TOKEN": "x", "PATH": "/bin"}
        out = curate_satellite_env(env)
        assert "GH_TOKEN" in env and env["GH_TOKEN"] == "x"  # source intact
        assert "GH_TOKEN" not in out

    def test_returns_plain_dict(self):
        out = curate_satellite_env({"PATH": "/bin"})
        assert isinstance(out, dict) and out == {"PATH": "/bin"}


class TestOperatorKeepSet:
    def test_oto_env_keep_allows_back(self, monkeypatch):
        # Operator escape hatch for a false positive on the broad _KEY suffix.
        monkeypatch.setenv("OTO_ENV_KEEP", "SSH_KEY, MY_HOST_KEY")
        out = curate_satellite_env({"SSH_KEY": "/p/id", "MY_HOST_KEY": "abc", "GH_TOKEN": "x"})
        assert out.get("SSH_KEY") == "/p/id"
        assert out.get("MY_HOST_KEY") == "abc"
        assert "GH_TOKEN" not in out          # not in the keep-set → still stripped

    def test_no_keep_set_strips_all(self, monkeypatch):
        monkeypatch.delenv("OTO_ENV_KEEP", raising=False)
        out = curate_satellite_env({"SSH_KEY": "x"})
        assert out == {}


class TestIsSecretName:
    @pytest.mark.parametrize("name,secret", [
        ("GH_TOKEN", True), ("AWS_SECRET_ACCESS_KEY", True),
        ("AWS_ACCESS_KEY_ID", True), ("FOO_API_KEY", True),
        ("SOME_APIKEY", True), ("DB_PASSWORD", True), ("X_PASSWD", True),
        ("RSA_PRIVATE_KEY", True), ("RANDOM_KEY", True),
        ("PATH", False), ("DISPLAY", False), ("AWS_PROFILE", False),
        ("GOOGLE_APPLICATION_CREDENTIALS", False), ("OPENAI_BASE_URL", False),
        ("KEYBOARD_LAYOUT", False), ("MONKEY", False),  # 'KEY' substring but not _KEY suffix
    ])
    def test_is_secret_name(self, name, secret):
        assert _is_secret_name(name) is secret
