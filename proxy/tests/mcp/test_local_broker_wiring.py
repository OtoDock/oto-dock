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

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR

if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.credentials import mcp_broker
from core.layers.codex.layer import _inject_fetch_tokens_toml
from core.sandbox.interceptor_wrap import wrap_toml_text
from core.sandbox.sandbox import prepare_mcp_config_for_sandbox


def _cfg_dir(tmp_path, monkeypatch, agent="agent-x"):
    """A session's config folder under the test's own agents root, which
    ``AGENTS_DIR`` points at: the copy is written beneath the agent."""
    import config as app_config
    agents = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    host_dir = agents / agent / "users" / "alice" / ".claude"
    host_dir.mkdir(parents=True)
    return host_dir


# --- local-claude: prepare_mcp_config_for_sandbox ----------------------------

def test_claude_injects_token_and_wraps_only_bundle_mcp(tmp_path, monkeypatch):
    cfg = {"mcpServers": {
        "google-maps": {"command": "python3", "args": ["s.py"],
                        "env": {"OTO_USERNAME": "alice"}},
        "display": {"command": "python3", "args": ["d.py"],
                    "env": {"OTO_USERNAME": "alice"}},
    }}
    src = tmp_path / "agent-abc123.json"
    src.write_text(json.dumps(cfg))
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    bundles = {"google-maps": mcp_broker.SecretBundle(env={"GOOGLE_MAPS_API_KEY": "secret"})}

    out_path = prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="sess-1", secret_bundles=bundles, agent_name="agent-x",
    )
    assert out_path == "/users/alice/.claude/agent-abc123-sess-1.json"
    written = json.loads((host_dir / "agent-abc123-sess-1.json").read_text())
    maps = written["mcpServers"]["google-maps"]
    disp = written["mcpServers"]["display"]

    # bundle MCP: token bound to (session, mcp) + command wrapped via interceptor
    tok = maps["env"]["OTO_MCP_FETCH_TOKEN"]
    assert mcp_broker.verify_token(tok) == ("sess-1", "google-maps")
    from core.sandbox.session_config_dir import sandbox_python
    assert maps["command"] == sandbox_python()
    assert maps["args"] == [
        "-I", "/users/alice/.claude/stdio_path_interceptor.py", "--", "python3", "s.py",
    ]
    # non-bundle MCP: untouched
    assert "OTO_MCP_FETCH_TOKEN" not in disp["env"]
    assert disp["command"] == "python3" and disp["args"] == ["d.py"]


def test_claude_no_bundles_is_plain_copy(tmp_path, monkeypatch):
    cfg = {"mcpServers": {"display": {"command": "python3", "args": ["d.py"], "env": {}}}}
    src = tmp_path / "agent.json"
    src.write_text(json.dumps(cfg))
    host_dir = _cfg_dir(tmp_path, monkeypatch)

    prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="sess-1", secret_bundles={}, agent_name="agent-x",
    )
    srv = json.loads((host_dir / "agent-sess-1.json").read_text())["mcpServers"]["display"]
    assert "OTO_MCP_FETCH_TOKEN" not in srv["env"]
    assert srv["command"] == "python3" and srv["args"] == ["d.py"]


def test_a_stand_in_with_an_empty_bundle_is_wrapped_with_its_own_fetch_token(tmp_path, monkeypatch):
    """A stdio tool with no secret whose program waits for its first call:
    the build gave it an empty bundle, so the session's copy mints its fetch
    token and wraps it like a credentialed one, the stand-in's marker kept."""
    marker = '{"account":"","fetch":false,"label":"Display","stamp":"1","v":1}'
    cfg = {"mcpServers": {"display": {"command": "python3", "args": ["d.py"],
                                      "env": {"OTO_TOOL_STANDIN": marker}}}}
    src = tmp_path / "agent-abc123.json"
    src.write_text(json.dumps(cfg))
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="sess-1", secret_bundles={"display": mcp_broker.SecretBundle()}, agent_name="agent-x",
    )
    disp = json.loads((host_dir / "agent-abc123-sess-1.json").read_text())["mcpServers"]["display"]
    assert mcp_broker.verify_token(disp["env"]["OTO_MCP_FETCH_TOKEN"]) == ("sess-1", "display")
    assert disp["env"]["OTO_TOOL_STANDIN"] == marker
    assert disp["args"][1:4] == ["/users/alice/.claude/stdio_path_interceptor.py", "--", "python3"]


def test_claude_missing_session_id_skips_broker(tmp_path, monkeypatch):
    cfg = {"mcpServers": {"google-maps": {"command": "python3", "args": ["s.py"], "env": {}}}}
    src = tmp_path / "a.json"
    src.write_text(json.dumps(cfg))
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    # bundles present but no session_id → generic copy path, no token/wrap.
    prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="", secret_bundles={"google-maps": mcp_broker.SecretBundle()}, agent_name="agent-x",
    )
    srv = json.loads((host_dir / "a.json").read_text())["mcpServers"]["google-maps"]
    assert "OTO_MCP_FETCH_TOKEN" not in srv["env"]
    assert srv["command"] == "python3"


def _instance_manifest(name, server_name, delivery="config_file"):
    from unittest.mock import MagicMock
    m = MagicMock()
    m.name, m.server_name = name, server_name
    m.instances = MagicMock(delivery=delivery) if delivery else None
    return m


def test_only_a_tools_own_instance_file_is_copied_from_the_sessions_folder(tmp_path, monkeypatch):
    """The one file the platform puts in an argument under the sessions
    folder is the tool's own instance file. Another tool's instance file, a
    build file, a path that climbs out of the folder and a link are left
    where they are, and a refusal costs no other tool its token or wrap."""
    from unittest.mock import patch

    import config as app_config
    sessions = tmp_path / "sessions"
    configs = sessions / "user-mcp-configs"
    configs.mkdir(parents=True)
    monkeypatch.setattr(app_config, "SESSIONS_DIR", sessions)
    secret = sessions / "stand-in-secret.json"
    secret.write_bytes(b"STAND-IN-SECRET")
    (configs / "fake-mcp-agent-x.json").write_bytes(b'{"own": true}')
    (configs / "other-mcp-agent-x.json").write_bytes(b"STAND-IN-SECRET")
    (configs / "agent-x-0123abcd.json").write_bytes(b"STAND-IN-SECRET")
    (configs / "plain-mcp-agent-x.json").write_bytes(b"STAND-IN-SECRET")
    (configs / "linked-mcp-agent-x.json").symlink_to(secret)
    cfg = {"mcpServers": {
        "fake": {"command": "python3", "args": [
            "s.py", "--config-file", str(configs / "fake-mcp-agent-x.json"),
            "--extra", str(configs / "other-mcp-agent-x.json"),
            "--up", f"{configs}/{'..'}/stand-in-secret.json"], "env": {}},
        "plain": {"command": "python3", "args": ["p.py", str(configs / "agent-x-0123abcd.json"),
                                                 str(configs / "plain-mcp-agent-x.json")], "env": {}},
        "linked": {"command": "python3", "args": ["l.py", str(configs / "linked-mcp-agent-x.json")], "env": {}},
    }}
    src = tmp_path / "agent-x-0123abcd.json"
    src.write_text(json.dumps(cfg))
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    manifests = {"fake-mcp": _instance_manifest("fake-mcp", "fake"),
                 "plain-mcp": _instance_manifest("plain-mcp", "plain", delivery=None),
                 "linked-mcp": _instance_manifest("linked-mcp", "linked")}
    with patch("services.mcp.mcp_registry.get_all_manifests", return_value=manifests):
        prepare_mcp_config_for_sandbox(
            src, host_dir, sandbox_config_dir="/users/alice/.claude", session_id="sess-1",
            secret_bundles={"fake": mcp_broker.SecretBundle()}, agent_name="agent-x",
        )
    written = json.loads((host_dir / "agent-x-0123abcd-sess-1.json").read_text())["mcpServers"]
    assert sorted(p.name for p in host_dir.iterdir()) == ["agent-x-0123abcd-sess-1.json", "fake-mcp-agent-x.json"]
    assert (host_dir / "fake-mcp-agent-x.json").read_bytes() == b'{"own": true}'
    for path in host_dir.iterdir():
        assert b"STAND-IN-SECRET" not in path.read_bytes()
    fake = written["fake"]
    assert mcp_broker.verify_token(fake["env"]["OTO_MCP_FETCH_TOKEN"]) == ("sess-1", "fake")
    assert "/users/alice/.claude/fake-mcp-agent-x.json" in fake["args"]
    assert str(configs / "other-mcp-agent-x.json") in fake["args"]
    assert written["plain"]["args"] == ["p.py", str(configs / "agent-x-0123abcd.json"),
                                        str(configs / "plain-mcp-agent-x.json")]
    assert written["linked"]["args"] == ["l.py", str(configs / "linked-mcp-agent-x.json")]


def test_another_agents_instance_file_is_not_copied(tmp_path, monkeypatch):
    from unittest.mock import patch

    import config as app_config
    sessions = tmp_path / "sessions"
    (sessions / "user-mcp-configs").mkdir(parents=True)
    monkeypatch.setattr(app_config, "SESSIONS_DIR", sessions)
    other = sessions / "user-mcp-configs" / "fake-mcp-agent-x.json"
    other.write_bytes(b"{}")
    src = tmp_path / "a.json"
    src.write_text(json.dumps({"mcpServers": {"fake": {"command": "x", "args": [str(other)], "env": {}}}}))
    host_dir = _cfg_dir(tmp_path, monkeypatch, agent="agent-y")
    with patch("services.mcp.mcp_registry.get_all_manifests",
               return_value={"fake-mcp": _instance_manifest("fake-mcp", "fake")}):
        prepare_mcp_config_for_sandbox(src, host_dir, sandbox_config_dir="/users/alice/.claude",
                                       session_id="sess-1", secret_bundles={}, agent_name="agent-y")
    assert sorted(p.name for p in host_dir.iterdir()) == ["a-sess-1.json"]


def _built_config(tmp_path):
    src = tmp_path / "agent-x-0123abcd.json"
    src.write_text(json.dumps({"mcpServers": {"t": {"command": "python3", "args": ["s.py"], "env": {}}}}))
    return src


def test_the_copy_refuses_a_config_folder_swapped_for_a_link_after_the_start(tmp_path, monkeypatch):
    """The session's engine folder made a link after the start's check: the
    build refuses, and the folder the link names gets no file and loses
    none."""
    import shutil
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent-x-0123abcd.json").write_text("stand-in")
    (elsewhere / "agent-x-0123abcd-old.json").write_text("stand-in old")
    import os
    import time
    aged = time.time() - 3 * 24 * 3600
    os.utime(elsewhere / "agent-x-0123abcd-old.json", (aged, aged))
    shutil.rmtree(host_dir)
    host_dir.symlink_to(elsewhere)
    with pytest.raises(OSError):
        prepare_mcp_config_for_sandbox(_built_config(tmp_path), host_dir, sandbox_config_dir="/users/alice/.claude",
                                       session_id="sess-1", secret_bundles={}, agent_name="agent-x")
    assert sorted(p.name for p in elsewhere.iterdir()) == ["agent-x-0123abcd-old.json", "agent-x-0123abcd.json"]


def test_an_agent_folder_that_is_itself_a_link_still_starts(tmp_path, monkeypatch):
    """The operator moved the agent's folder behind a link: honest, and the
    copy lands in its real place."""
    import config as app_config
    from core.sandbox.session_config_dir import scope_config_dir
    agents = tmp_path / "agents"
    agents.mkdir()
    real = tmp_path / "disk2" / "agent-x"
    real.mkdir(parents=True)
    (agents / "agent-x").symlink_to(real)
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    host_dir, sandbox_dir = scope_config_dir("agent-x", ".claude", username="alice")
    out = prepare_mcp_config_for_sandbox(_built_config(tmp_path), host_dir, sandbox_config_dir=sandbox_dir,
                                         session_id="sess-1", secret_bundles={}, agent_name="agent-x")
    assert out == "/users/alice/.claude/agent-x-0123abcd-sess-1.json"
    assert (real / "users" / "alice" / ".claude" / "agent-x-0123abcd-sess-1.json").is_file()


def test_the_copy_sweep_removes_old_copies_beneath_the_agent_and_never_through_a_link(tmp_path, monkeypatch):
    import os
    import time
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    old = time.time() - 3 * 24 * 3600
    stale = host_dir / "agent-x-0123abcd-stale.json"
    stale.write_text("old")
    os.utime(stale, (old, old))
    target = tmp_path / "stand-in.json"
    target.write_text("stand-in")
    os.utime(target, (old, old))
    (host_dir / "agent-x-0123abcd-link.json").symlink_to(target)
    (host_dir / "agent-x-0123abcd.json").write_text("shared copy of an earlier release")
    prepare_mcp_config_for_sandbox(_built_config(tmp_path), host_dir, sandbox_config_dir="/users/alice/.claude",
                                   session_id="sess-1", secret_bundles={}, agent_name="agent-x")
    names = {p.name for p in host_dir.iterdir()}
    assert "agent-x-0123abcd-sess-1.json" in names
    assert "agent-x-0123abcd-stale.json" not in names and "agent-x-0123abcd.json" not in names
    assert target.read_text() == "stand-in"


def test_the_copy_sweep_removes_through_the_folder_it_listed(tmp_path, monkeypatch):
    """The config folder made a link after the sweep listed it: the old copy
    goes from the folder that was listed, and the folder the link names loses
    nothing."""
    import os
    import time

    from core.sandbox import session_config_dir as scd
    host_dir = _cfg_dir(tmp_path, monkeypatch)
    old = time.time() - 3 * 24 * 3600
    stale = host_dir / "agent-x-0123abcd-stale.json"
    stale.write_text("old")
    os.utime(stale, (old, old))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent-x-0123abcd-stale.json").write_text("stand-in")
    real_older = scd._older
    swapped = []

    def older(entry, cutoff):
        if not swapped:
            host_dir.rename(host_dir.with_name("listed"))
            host_dir.symlink_to(elsewhere)
            swapped.append(True)
        return real_older(entry, cutoff)

    monkeypatch.setattr(scd, "_older", older)
    with pytest.raises(OSError):
        prepare_mcp_config_for_sandbox(_built_config(tmp_path), host_dir, sandbox_config_dir="/users/alice/.claude",
                                       session_id="sess-1", secret_bundles={}, agent_name="agent-x")
    assert swapped and (elsewhere / "agent-x-0123abcd-stale.json").read_text() == "stand-in"
    assert not (host_dir.with_name("listed") / "agent-x-0123abcd-stale.json").exists()


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


# --- a bundle the token-file delivery creates gets wired like any other ----

# --- the delivery helper the Direct-LLM and app-button builders share -------


