"""Bootstrap headers must EXPORT the CLI pins.

install-baseline-tools.sh runs as a CHILD bash of install.sh, so an
unexported variable never reaches it — unlike the base64 payload vars, which
are read in-process and deliberately stay unexported (MAX_ARG_STRLEN). With
the pins exported, pairing-time baseline installs use the serving proxy's
pinned CLI versions instead of the script's baked defaults.
"""

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
