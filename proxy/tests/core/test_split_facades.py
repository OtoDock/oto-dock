"""What holds a module split together after its commit.

A facade keeps its old import path by re-export, and a re-export is a copy of a
binding: ``facade.name = value`` in a test makes a new attribute the moved body
never reads, and passes for the wrong reason. So a test assigns a name on a facade
only when the facade defines it, and it patches a name the facade re-exports only
when something can see the patch: the facade's own bodies, or production code that
reads the name through the facade at call time. And every piece must be able to be
the first module a process imports: a piece never imports its facade.

The patch rule sees a patch no read can reach. It cannot see a partial miss: a
patch that reaches one reader through the facade while a body that moved reads its
own binding (a clock patched on one of two modules, a shared table swapped on one).
It cannot see a name held in a variable either. The test that patches one must
patch every module whose code reads the name.
"""

from __future__ import annotations

import ast
import functools
import subprocess
import symtable
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

PROXY = Path(__file__).resolve().parents[2]

FACADES = {
    "services.mcp.mcp_registry": ["services.mcp.mcp_manifest_store", "services.mcp.mcp_session_filter",
                                  "services.mcp.mcp_egress", "services.mcp.mcp_agent_skills",
                                  "services.mcp.mcp_server_resolve", "services.mcp.mcp_session_config"],
    "services.mcp.mcp_manifest_parse": ["services.mcp.mcp_manifest_blocks"],
    "services.mcp.dynamic_context": ["services.mcp.dynamic_providers"],
    "services.mcp.docker_manager": ["services.containers.compose_files"],
    "core.execution_layer": ["core.engine_records", "core.remote_engine_adapter"],
    "core.concurrency": ["core.concurrency_scan"],
    "core.remote.remote_file_flow": ["core.remote.remote_file_cache"],
    "core.remote.satellite_file_transfer": ["core.remote.satellite_file_verbs"],
    "core.remote.remote_workspace_sync": ["core.remote.remote_workspace_apply"],
    "core.session.interactive_session": ["core.session.interactive_common", "core.session.interactive_registry",
                                         "core.session.interactive_output", "core.session.interactive_turn",
                                         "core.session.interactive_queue", "core.session.interactive_input"],
    "services.scheduler.delivery": ["services.scheduler.wake_session"],
    "startup": ["startup_sweeps", "startup_shutdown"],
    "api.hooks.permission": ["api.hooks.questions", "api.hooks.credentials"],
    "storage.schema": ["storage.apps.schema"],
    "ws.dashboard_warmup": ["ws.dashboard_warmup_spawn"],
    "core.layers.codex.layer": ["core.layers.codex.capabilities", "core.layers.codex.mcp_toml",
                                "core.layers.codex.spawn", "core.layers.codex.resume"],
    "services.engines.subscription_pool": [
        f"services.engines.{p}" for p in ("pool_state", "pool_writer", "pool_rests", "pool_bindings", "pool_tokens",
                                          "pool_select", "pool_acquire", "pool_limits", "pool_rebind", "pool_gates",
                                          "pool_env")],
}


def _defined(module: str) -> set[str]:
    """What a facade's own statements bind: everything but its imports."""
    tree = ast.parse((PROXY / (module.replace(".", "/") + ".py")).read_text())
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            out |= {x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)}
    return out


def _dotted(node: ast.expr) -> str:
    """``a.b.c`` as text, "" for anything that is not a plain dotted name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else ""


@functools.cache
def _text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@functools.cache
def _index_of(path: Path) -> SimpleNamespace:
    """One walk of a file, for every question the tests below ask of it."""
    tree = ast.parse(_text_of(path))
    top = {id(node) for node in tree.body}
    index = SimpleNamespace(imports=[], lazy_from=[], patches=[], reads=set(), stores=[])
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            index.imports.append(node)
            if isinstance(node, ast.ImportFrom) and id(node) not in top:
                index.lazy_from.append(node)
        elif isinstance(node, ast.Call):
            word = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if word in {"setattr", "delattr", "patch", "object"} and node.args:
                index.patches.append(node)
        elif isinstance(node, ast.Attribute):
            head = _dotted(node.value)
            if head:
                index.reads.add((head, node.attr))
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    index.stores.append((head, node.attr, node.lineno))
    return index


def _aliases(path: Path, module: str) -> set[str]:
    head, _, leaf = module.rpartition(".")
    out: set[str] = set()
    for node in _index_of(path).imports:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == module and (a.asname or "." not in module):
                    out.add(a.asname or module)
        elif head and node.module == head:
            out |= {a.asname or a.name for a in node.names if a.name == leaf}
    return out


@functools.cache
def _production_files() -> tuple[Path, ...]:
    return tuple(p for p in PROXY.rglob("*.py")
                 if not {"venv", "tests", "__pycache__"} & set(p.relative_to(PROXY).parts))


@functools.cache
def _test_files() -> tuple[Path, ...]:
    return tuple(sorted((PROXY / "tests").rglob("*.py")))


def _own_reads(module: str) -> set[str]:
    """The names a facade's own statements read, at any depth."""
    path = PROXY / (module.replace(".", "/") + ".py")
    names: set[str] = set()

    def walk(table: symtable.SymbolTable) -> None:
        for symbol in table.get_symbols():
            module_level = table.get_type() == "module"
            if symbol.is_referenced() and (module_level or symbol.is_global()):
                names.add(symbol.get_name())
        for child in table.get_children():
            walk(child)

    walk(symtable.symtable(_text_of(path), str(path), "exec"))
    return names


def _call_time_readers(module: str) -> set[str]:
    """The names production code reads through the facade when it runs: an
    attribute of the module, or a ``from`` import inside a function. A ``from``
    import at a module's top holds what it first found and never sees a patch."""
    leaf = module.rsplit(".", 1)[-1]
    own_file = PROXY / (module.replace(".", "/") + ".py")
    names: set[str] = set()
    for path in _production_files():
        if path == own_file or leaf not in _text_of(path):
            continue
        index = _index_of(path)
        heads = _aliases(path, module) | {module}
        names |= {name for head, name in index.reads if head in heads}
        for node in index.lazy_from:
            if node.module == module:
                names |= {a.name for a in node.names}
    return names


def _patched_names(path: Path, module: str) -> list[tuple[str, int]]:
    """Every (name, line) a test patches on the module in a form that names it:
    ``setattr``, ``patch.object``, ``delattr``, or a dotted string."""
    aliases = _aliases(path, module)
    out: list[tuple[str, int]] = []
    for node in _index_of(path).patches:
        first = node.args[0]
        if (isinstance(first, ast.Name) and first.id in aliases and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            out.append((node.args[1].value, node.lineno))
        elif (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and first.value.startswith(module + ".")):
            out.append((first.value[len(module) + 1:].split(".")[0], node.lineno))
    return out


def test_every_patch_of_a_re_exported_name_has_a_reader():
    """A patch of a name the facade only re-exports reaches nothing unless the
    facade's own bodies read it or production reads it through the facade."""
    unread: dict[str, list[str]] = defaultdict(list)
    for module in FACADES:
        leaf = module.rsplit(".", 1)[-1]
        seen = _defined(module) | _own_reads(module) | _call_time_readers(module)
        for path in _test_files():
            if leaf not in _text_of(path):
                continue
            for name, line in _patched_names(path, module):
                if name not in seen:
                    unread[module].append(f"{path.relative_to(PROXY)}:{line} {name}")
    assert dict(unread) == {}, "patch the module whose code reads the name"


def test_no_test_assigns_a_name_a_facade_only_re_exports():
    wrong: list[str] = []
    for module in FACADES:
        leaf = module.rsplit(".", 1)[-1]
        own = _defined(module)
        for path in _test_files():
            if leaf not in _text_of(path):
                continue
            names = _aliases(path, module)
            for head, attr, line in _index_of(path).stores:
                if head in names and attr not in own:
                    wrong.append(f"{path.relative_to(PROXY)}:{line} {head}.{attr}")
    assert wrong == []


@pytest.mark.parametrize("facade", sorted(FACADES))
def test_a_piece_never_imports_its_facade(facade):
    for piece in FACADES[facade]:
        tree = ast.parse((PROXY / (piece.replace(".", "/") + ".py")).read_text())
        checking = {id(x) for n in tree.body if isinstance(n, ast.If) and "TYPE_CHECKING" in ast.unparse(n.test)
                    for x in ast.walk(n)}
        for node in ast.walk(tree):
            if id(node) in checking:
                continue
            if isinstance(node, ast.Import):
                assert facade not in {a.name for a in node.names}, piece
            elif isinstance(node, ast.ImportFrom):
                assert node.module != facade, piece
                head, _, leaf = facade.rpartition(".")
                assert not (head and node.module == head and leaf in {a.name for a in node.names}), piece


def test_every_piece_imports_first():
    """Each piece alone in a fresh interpreter."""
    pieces = sorted(p for ps in FACADES.values() for p in ps)

    def first_import(module: str) -> str:
        out = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=str(PROXY),
                             capture_output=True, text=True, timeout=180)
        if out.returncode == 0:
            return ""
        return f"{module}: {(out.stderr.strip().splitlines() or ['?'])[-1]}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert [f for f in pool.map(first_import, pieces) if f] == []
