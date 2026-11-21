"""Bootstrap headers must EXPORT the CLI pins.

install-baseline-tools.sh runs as a CHILD bash of install.sh, so an
unexported variable never reaches it — unlike the base64 payload vars, which
are read in-process and deliberately stay unexported (MAX_ARG_STRLEN). With
the pins exported, pairing-time baseline installs use the serving proxy's
pinned CLI versions instead of the script's baked defaults.
"""

from pathlib import Path

import pytest

import config as app_config
from api.remote.remote_machines import (
    _build_bash_header,
    _build_powershell_header,
)


def test_bash_header_exports_cli_pins(monkeypatch):
    monkeypatch.setattr(app_config, "PINNED_CLAUDE_CODE_VERSION", "9.9.9")
    monkeypatch.setattr(app_config, "PINNED_CODEX_VERSION", "8.8.8")
    hdr = _build_bash_header("m", "s", "https://x", "BB", "TT")
    assert 'export CLAUDE_CODE_VERSION="9.9.9"' in hdr
    assert 'export CODEX_VERSION="8.8.8"' in hdr
    # Payload vars stay unexported — exporting the ~140KiB tarball base64
    # breaks execve (MAX_ARG_STRLEN) for every child command.
    assert 'BASELINE_TOOLS_B64="BB"' in hdr
    assert "export BASELINE_TOOLS_B64" not in hdr
    assert "export SATELLITE_TARBALL_B64" not in hdr


def test_bash_header_omits_empty_pins(monkeypatch):
    monkeypatch.setattr(app_config, "PINNED_CLAUDE_CODE_VERSION", "")
    monkeypatch.setattr(app_config, "PINNED_CODEX_VERSION", "")
    hdr = _build_bash_header("m", "s", "https://x", "BB", "TT")
    assert "CLAUDE_CODE_VERSION" not in hdr
    assert "CODEX_VERSION" not in hdr


def test_powershell_header_sets_env_pins(monkeypatch):
    monkeypatch.setattr(app_config, "PINNED_CLAUDE_CODE_VERSION", "9.9.9")
    monkeypatch.setattr(app_config, "PINNED_CODEX_VERSION", "")
    hdr = _build_powershell_header("m", "s", "https://x", "BB", "TT")
    assert "$env:CLAUDE_CODE_VERSION = '9.9.9'" in hdr
    assert "CODEX_VERSION" not in hdr


def _passt_mode(hdr: str, preset: str | None) -> str:
    """The value the baseline script (a child bash) reads after the header."""
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "OTODOCK_PASST_BUILD"}
    if preset is not None:
        env["OTODOCK_PASST_BUILD"] = preset
    script = hdr + '\nbash -c \'printf %s "$OTODOCK_PASST_BUILD"\'\n'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=30, check=True).stdout


def test_pairing_never_compiles_passt(monkeypatch):
    """A paired machine runs no pasta: the baseline script takes the host's
    or the distro's passt instead of building the pinned one."""
    hdr = _build_bash_header("m", "s", "https://x", "BB", "TT")
    assert _passt_mode(hdr, None) == "distro"
    assert _passt_mode(hdr, "auto") == "auto"     # a value set before the bootstrap stays


def test_the_powershell_header_has_no_passt_mode():
    assert "OTODOCK_PASST_BUILD" not in _build_powershell_header("m", "s", "https://x", "BB", "TT")


def test_the_windows_pairing_command_runs_a_saved_file():
    """Windows' antimalware scan refuses the download-and-run shape (``iex``
    of what ``iwr`` fetched) before anything is fetched: the command saves
    the bootstrap to a file and runs that file, with no ``$`` a pasting
    shell could expand first."""
    from api.remote.remote_machines import _build_install_commands

    cmds = _build_install_commands("platform.example", "tok123")
    win = cmds["windows"]
    low = win.lower()
    assert win.startswith('powershell -ExecutionPolicy Bypass -Command "& { ')
    assert "iex" not in low and "invoke-expression" not in low and ".content" not in low
    assert "-OutFile" in win and win.count("'otodock-pair.ps1'") == 2
    assert "$" not in win
    assert "@{'X-Pairing-Token'='tok123'}" in win
    assert "'https://platform.example/v1/satellite/bootstrap?os=windows'" in win
    assert cmds["linux"] == cmds["macos"]
    assert "bash <(curl -sL" in cmds["linux"]


@pytest.mark.parametrize(("public_url", "web", "socket"), [
    ("http://192.168.1.10:8400", "http", "ws"),
    ("http://10.0.0.5:8400/", "http", "ws"),
    ("HTTP://172.16.3.4:8400", "http", "ws"),
    ("http://127.0.0.1:8400", "http", "ws"),
    ("http://localhost:8400", "http", "ws"),
    ("http://[fd00::1]:8400", "http", "ws"),
    ("http://169.254.10.1:8400", "http", "ws"),
    ("http://[fe80::1]:8400", "http", "ws"),
    ("http://[::1]:8400", "http", "ws"),
    ("https://192.168.1.10:8400", "https", "wss"),
    ("https://agents.example.com", "https", "wss"),
    # A public host or any name on plain HTTP keeps TLS: the token and the
    # machine's secret never cross a network nobody vouched for.
    ("http://8.8.8.8:8400", "https", "wss"),
    ("http://agents.example.com:8400", "https", "wss"),
    ("http://your-server:8400", "https", "wss"),
    ("http://100.64.0.1:8400", "https", "wss"),
    # Ranges Python calls private that can carry or stand for a public address:
    # 6to4 (this one wraps 192.168.1.10), NAT64, documentation, benchmarking.
    ("http://[2002:c0a8:10a::1]:8400", "https", "wss"),
    ("http://[64:ff9b:1::1]:8400", "https", "wss"),
    ("http://192.0.2.1:8400", "https", "wss"),
    ("http://198.18.0.1:8400", "https", "wss"),
    ("http://[::ffff:192.168.1.10]:8400", "https", "wss"),
    ("192.168.1.10:8400", "https", "wss"),
    ("", "https", "wss"),
])
def test_the_pairing_addresses_follow_a_private_plain_http_address(monkeypatch, public_url, web, socket):
    import config as cfg
    from api.remote.remote_machines import _build_install_commands
    from core.config.deployment import link_schemes

    monkeypatch.setattr(cfg, "DASHBOARD_PUBLIC_URL", public_url)
    assert link_schemes() == (web, socket)
    host = cfg.get_platform_public_url() or "unset.example"
    cmds = _build_install_commands(host, "tok123")
    assert f'"{web}://{host}/v1/satellite/bootstrap?os=linux"' in cmds["linux"]
    assert f"'{web}://{host}/v1/satellite/bootstrap?os=windows'" in cmds["windows"]


def test_the_windows_scripts_are_ascii_without_a_byte_order_mark():
    """Run as a saved file, Windows PowerShell 5.1 reads a BOM-less script in
    the system's ANSI code page: an em-dash's bytes end in a smart quote
    that closes a string early, and a BOM carried mid-file (the template
    follows the generated header) becomes a stray command name."""
    root = Path(__file__).resolve().parents[3]
    for rel in ("satellite/install.ps1", "scripts/install-baseline-tools.ps1"):
        raw = (root / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), rel
        assert raw.isascii(), rel
    header = _build_powershell_header("m1", "s1", "wss://p.example", "QUJD", "REVG")
    assert header.isascii()
