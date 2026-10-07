"""The proxy ↔ satellite engine contract (engine-contract lane, phase 4).

The satellite's ``ENGINES`` table (``satellite/engines.py``) and the proxy's
engine descriptors describe the same two engines from either side of the
wire. Where a fact appears on both — the config dir, the credential kind and
file, the pin key, the binary, whether an in-flight turn survives a proxy
restart — they must agree, and an engine registered on one side must exist
on the other. This is the operator's "``runtime.config_dir_name`` agrees
with ``auth.credential_file.dirname`` wherever both exist" made into the
whole cross-tree contract.

The satellite module is imported (it is stdlib-only and ``satellite/__init__``
is a docstring), not parsed: a dict of ``Engine(...)`` calls is not a literal.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from satellite.engines import BY_CREDENTIAL_KIND, ENGINES  # noqa: E402

from core.session.session_manager import get_all_layers  # noqa: E402


def _remote_layers():
    return {
        path: layer for path, layer in get_all_layers().items()
        if layer.capabilities.runtime.supports_remote_execution
    }


def test_every_remote_engine_has_a_satellite_row_and_vice_versa():
    assert set(_remote_layers()) == set(ENGINES)


def test_the_rows_agree_with_the_descriptors():
    for path, layer in _remote_layers().items():
        caps = layer.capabilities
        row = ENGINES[path]
        assert row.binary == caps.runtime.binary, path
        assert row.pin_key == caps.runtime.pin_key, path
        assert row.installed_name == caps.runtime.installed_name, path
        assert row.config_dir == caps.runtime.config_dir_name, path
        assert row.turn_replay == caps.runtime.supports_reattach_after_restart, path
        # Every remote engine's idle session is in the connect report (one
        # list or the other), so every one is taken back or closed there.
        assert caps.runtime.readopts_idle_session, path
        spec = caps.auth.credential_file
        assert spec is not None, f"{path}: a satellite row clamps a credential file the proxy does not declare"
        assert row.credential_kind == spec.wire_kind, path
        assert row.credential_filename == spec.filename, path
        assert row.config_dir == spec.dirname, path


def test_the_credential_clamp_knows_exactly_the_proxys_kinds():
    proxy_kinds = {
        layer.capabilities.auth.credential_file.wire_kind
        for layer in _remote_layers().values()
        if layer.capabilities.auth.credential_file is not None
    }
    assert set(BY_CREDENTIAL_KIND) == proxy_kinds


def test_the_remote_adapters_speak_the_rows_payload_keys():
    # The start-payload keys the satellite session classes read are the
    # engine's own wire vocabulary — declared as constants on its remote
    # adapter module and pinned here against the satellite's readers.
    from core.layers.cli import remote as cli_remote
    from core.layers.codex import remote as codex_remote
    assert (cli_remote.CREDENTIALS_PAYLOAD_KEY, cli_remote.CONFIG_DIR_PAYLOAD_KEY,
            cli_remote.MCP_CONFIG_PAYLOAD_KEY) == (
        "credentials_json", "claude_dir_relative", "mcp_config")
    assert (codex_remote.CREDENTIALS_PAYLOAD_KEY, codex_remote.CONFIG_DIR_PAYLOAD_KEY,
            codex_remote.RESUME_HANDLE_PAYLOAD_KEY, codex_remote.MCP_CONFIG_PAYLOAD_KEY) == (
        "auth_json", "codex_dir_relative", "thread_id", "mcp_config_toml")
    # And the satellite reads them under the same spellings.
    sat = _REPO_ROOT / "satellite"
    cli_src = (sat / "sessions" / "cli_session.py").read_text() + (sat / "terminal" / "pty_session.py").read_text()
    codex_src = (sat / "sessions" / "codex_session.py").read_text() + (sat / "terminal" / "codex_pty_session.py").read_text()
    for key in ("credentials_json", "claude_dir_relative", "mcp_config"):
        assert f'"{key}"' in cli_src, key
    for key in ("auth_json", "codex_dir_relative", "thread_id", "mcp_config_toml"):
        assert f'"{key}"' in codex_src, key
