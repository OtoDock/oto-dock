"""Local claude + codex broker wiring.

The per-session materialization points inject the per-(session, mcp) capability
token into ONLY the bundle MCPs, then wrap their command with the interceptor:
  * local-claude → `sandbox.prepare_mcp_config_for_sandbox`
  * local-codex  → `codex.layer._inject_fetch_tokens_toml` + `wrap_toml_text`
The token must bind (session_id, mcp) so the broker endpoint serves only that
MCP's secrets. Non-bundle MCPs stay untouched.
"""

import json
import re
import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.credentials import mcp_broker # noqa: E402
from core.sandbox.sandbox import prepare_mcp_config_for_sandbox  # noqa: E402
from core.layers.codex.layer import _inject_fetch_tokens_toml  # noqa: E402
from core.sandbox.interceptor_wrap import wrap_toml_text  # noqa: E402


# --- local-claude: prepare_mcp_config_for_sandbox ----------------------------

def test_claude_injects_token_and_wraps_only_bundle_mcp(tmp_path):
    cfg = {"mcpServers": {
        "google-maps": {"command": "python3", "args": ["s.py"],
                        "env": {"OTO_USERNAME": "alice"}},
        "display": {"command": "python3", "args": ["d.py"],
                    "env": {"OTO_USERNAME": "alice"}},
    }}
    src = tmp_path / "agent-abc123.json"
    src.write_text(json.dumps(cfg))
    host_dir = tmp_path / ".claude"
    host_dir.mkdir()
    bundles = {"google-maps": mcp_broker.SecretBundle(env={"GOOGLE_MAPS_API_KEY": "secret"})}

    out_path = prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="sess-1", secret_bundles=bundles,
    )
    assert out_path == "/users/alice/.claude/agent-abc123.json"
    written = json.loads((host_dir / "agent-abc123.json").read_text())
    maps = written["mcpServers"]["google-maps"]
    disp = written["mcpServers"]["display"]

    # bundle MCP: token bound to (session, mcp) + command wrapped via interceptor
    tok = maps["env"]["OTO_MCP_FETCH_TOKEN"]
    assert mcp_broker.verify_token(tok) == ("sess-1", "google-maps")
    assert maps["command"] == "python3"
    assert maps["args"] == [
        "/users/alice/.claude/stdio_path_interceptor.py", "--", "python3", "s.py",
    ]
    # non-bundle MCP: untouched
    assert "OTO_MCP_FETCH_TOKEN" not in disp["env"]
    assert disp["command"] == "python3" and disp["args"] == ["d.py"]


def test_claude_no_bundles_is_plain_copy(tmp_path):
    cfg = {"mcpServers": {"display": {"command": "python3", "args": ["d.py"], "env": {}}}}
    src = tmp_path / "agent.json"
    src.write_text(json.dumps(cfg))
    host_dir = tmp_path / ".claude"
    host_dir.mkdir()

    prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="sess-1", secret_bundles={},
    )
    srv = json.loads((host_dir / "agent.json").read_text())["mcpServers"]["display"]
    assert "OTO_MCP_FETCH_TOKEN" not in srv["env"]
    assert srv["command"] == "python3" and srv["args"] == ["d.py"]


def test_claude_missing_session_id_skips_broker(tmp_path):
    cfg = {"mcpServers": {"google-maps": {"command": "python3", "args": ["s.py"], "env": {}}}}
    src = tmp_path / "a.json"
    src.write_text(json.dumps(cfg))
    host_dir = tmp_path / ".claude"
    host_dir.mkdir()
    # bundles present but no session_id → generic copy path, no token/wrap.
    prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="", secret_bundles={"google-maps": mcp_broker.SecretBundle()},
    )
    srv = json.loads((host_dir / "a.json").read_text())["mcpServers"]["google-maps"]
    assert "OTO_MCP_FETCH_TOKEN" not in srv["env"]
    assert srv["command"] == "python3"


# --- local-codex: _inject_fetch_tokens_toml + wrap_toml_text -----------------

_CODEX_TOML = (
    '[mcp_servers.google-maps]\n'
    'command = "python3"\n'
    'args = ["s.py"]\n'
    'env = { "OTO_USERNAME" = "alice" }\n'
    '\n'
    '[mcp_servers.display]\n'
    'command = "python3"\n'
    'args = ["d.py"]\n'
    'env = { "OTO_USERNAME" = "alice" }\n'
)


def test_codex_inject_is_section_aware():
    out = _inject_fetch_tokens_toml(_CODEX_TOML, {"google-maps"}, "sess-1")
    tok = re.search(r'"OTO_MCP_FETCH_TOKEN" = "([^"]+)"', out).group(1)
    assert mcp_broker.verify_token(tok) == ("sess-1", "google-maps")
    # token only in the google-maps section, never in display's
    disp_idx = out.index("[mcp_servers.display]")
    assert "OTO_MCP_FETCH_TOKEN" not in out[disp_idx:]
    # existing env keys preserved
    assert '"OTO_USERNAME" = "alice"' in out


def test_codex_inject_then_wrap_full_chain():
    injected = _inject_fetch_tokens_toml(_CODEX_TOML, {"google-maps"}, "sess-1")
    out = wrap_toml_text(
        injected, interpreter="python3",
        interceptor_path="/users/alice/.codex/stdio_path_interceptor.py",
    )
    # google-maps wrapped
    assert 'args = ["/users/alice/.codex/stdio_path_interceptor.py", "--", "python3", "s.py"]' in out
    # display untouched (no token → no wrap)
    disp_idx = out.index("[mcp_servers.display]")
    assert 'args = ["d.py"]' in out[disp_idx:]
