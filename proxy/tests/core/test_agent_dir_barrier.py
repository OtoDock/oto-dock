"""An agent's folder is reached through ``config.get_agent_dir``, the barrier
that refuses a name that is no agent folder (empty, absolute, holding a
separator, leaving the agents tree). The files that joined ``AGENTS_DIR`` and
a name directly route through it, and each answers a refused name the way it
answers a missing agent, never with a 500."""

import ast

import pytest

import config
from tests._paths import PROXY_DIR

# The files whose agent-folder joins went through the barrier in 1.9.0.
_ROUTED = (
    "api/agents/agents.py",
    "api/hooks/lifecycle.py",
    "configuration/prompts.py",
    "core/remote/remote_file_flow.py",
    "core/remote/satellite_file_transfer.py",
    "core/remote/remote_workspace_sync.py",
)


def _is_root(node: ast.AST) -> bool:
    return (isinstance(node, (ast.Attribute, ast.Name))
            and (getattr(node, "attr", "") or getattr(node, "id", "")) == "AGENTS_DIR")


def _named(nodes: list[ast.expr]) -> bool:
    """Any part that is not a string constant (a name, a call, a value)."""
    return any(not (isinstance(n, ast.Constant) and isinstance(n.value, str)) for n in nodes)


def _direct_joins(tree: ast.AST) -> list[int]:
    """Lines that join ``AGENTS_DIR`` and anything but a string constant:
    ``AGENTS_DIR / x``, ``AGENTS_DIR.joinpath(x)``, ``os.path.join(AGENTS_DIR,
    x)``, ``Path(AGENTS_DIR, x)`` and ``f"{AGENTS_DIR}/{x}"``."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            hit = _is_root(node.left) and _named([node.right])
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", "") or getattr(func, "id", "")
            hit = ((name == "joinpath" and _is_root(getattr(func, "value", None)) and _named(node.args))
                   or (name in ("join", "Path", "PurePath") and node.args
                       and _is_root(node.args[0]) and _named(node.args[1:])))
        elif isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
            hit = bool(parts) and _is_root(parts[0]) and len(parts) > 1
        else:
            hit = False
        if hit:
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize("rel", _ROUTED)
def test_the_routed_files_join_no_agent_name_to_the_agents_root(rel):
    tree = ast.parse((PROXY_DIR / rel).read_text(encoding="utf-8"))
    assert _direct_joins(tree) == []


def test_the_scan_sees_a_direct_join():
    sample = ast.parse("a = config.AGENTS_DIR / name\nb = AGENTS_DIR / '.cache'\n"
                       "c = _cfg.AGENTS_DIR / agent_slug / rel\n"
                       "d = AGENTS_DIR.joinpath(slug)\ne = os.path.join(config.AGENTS_DIR, slug)\n"
                       "f = Path(AGENTS_DIR, slug, 'x')\ng = f'{AGENTS_DIR}/{slug}'\n"
                       "h = AGENTS_DIR.joinpath('.cache')\ni = os.path.join(AGENTS_DIR, 'x')\n"
                       "j = f'{AGENTS_DIR}'\n")
    assert _direct_joins(sample) == [1, 3, 4, 5, 6, 7]


@pytest.mark.parametrize("name", ["", "..", "a/b", "/etc", "a\\b"])
def test_the_barrier_refuses_a_name_that_is_no_agent_folder(name):
    with pytest.raises(ValueError):
        config.get_agent_dir(name)


def test_the_remote_file_paths_refuse_a_bad_name():
    from core.remote import remote_file_flow
    with pytest.raises(ValueError):
        remote_file_flow._workspace_path("..", "workspace/x.md")
    assert remote_file_flow._platform_copy("..", "workspace/x.md") is None
    assert remote_file_flow._platform_copy("a/b", "workspace/x.md") is None


@pytest.mark.asyncio
async def test_a_file_written_under_a_bad_name_changes_nothing():
    from api.hooks import lifecycle
    assert await lifecycle._note_written("..", "workspace/x.md") is False
