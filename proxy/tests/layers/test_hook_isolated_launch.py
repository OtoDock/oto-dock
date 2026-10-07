"""The hook scripts launch with ``python3 -I`` on a local sandbox (no
PYTHON* variable, no user site, no script directory on the path), for both
engines; the interceptor wrap takes the same isolation flag. The scripts
are stdlib-only, so isolated mode runs them. Satellites are unchanged.
"""

import sys

from tests._paths import PROXY_DIR
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from core.layers.cli.config_dir import build_settings  # noqa: E402
from core.layers.codex.config_dir import build_hooks  # noqa: E402
from core.sandbox.interceptor_wrap import wrap_servers_json, wrap_toml_text  # noqa: E402


def _commands(hooks: dict) -> list[str]:
    out = []
    for group in hooks.values():
        for entry in group:
            for hook in entry["hooks"]:
                out.append(hook["command"])
    return out


def test_claude_hooks_run_python3_isolated_quoting_the_script():
    hooks = build_settings("/users/alice/.claude")["hooks"]
    cmds = _commands(hooks)
    assert cmds, "no hook commands"
    for c in cmds:
        assert c.startswith('python3 -I "/users/alice/.claude/')
        assert c.endswith('.py"')
    assert any("permission_gate.py" in c for c in cmds)


def test_codex_hooks_run_python3_isolated():
    hooks = build_hooks("/workspace/.codex")["hooks"]
    cmds = _commands(hooks)
    assert cmds
    for c in cmds:
        assert c.startswith('python3 -I "/workspace/.codex/') and c.endswith('.py"')


def test_the_interceptor_wrap_passes_the_isolation_flag_locally():
    cfg = {"mcpServers": {"m": {"command": "python3", "args": ["s.py"],
                                "env": {"OTO_MCP_FETCH_TOKEN": "t"}}}}
    wrap_servers_json(cfg, interpreter="python3", interpreter_args=("-I",),
                      interceptor_path="/u/.claude/stdio_path_interceptor.py")
    assert cfg["mcpServers"]["m"]["args"][:2] == ["-I", "/u/.claude/stdio_path_interceptor.py"]
    toml = wrap_toml_text(
        '[mcp_servers.m]\ncommand = "python3"\nargs = ["s.py"]\nenv = { "OTO_MCP_FETCH_TOKEN" = "t" }\n',
        interpreter="python3", interpreter_args=("-I",),
        interceptor_path="/u/.codex/stdio_path_interceptor.py",
    )
    assert 'args = ["-I", "/u/.codex/stdio_path_interceptor.py", "--", "python3", "s.py"]' in toml


def test_the_hook_scripts_and_the_interceptor_import_only_the_standard_library():
    import ast
    import pathlib
    root = pathlib.Path(PROXY_DIR)
    files = [root / "hooks" / n for n in (
        "permission_gate.py", "tool_result_forwarder.py", "subagent_tracker.py",
        "stop_tracker.py")]
    files.append(root / "core" / "stdio_path_interceptor.py")
    stdlib = set(sys.stdlib_module_names)
    for f in files:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] in stdlib, (f.name, a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] in stdlib, (f.name, node.module)
