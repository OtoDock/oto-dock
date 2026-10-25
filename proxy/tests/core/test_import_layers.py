"""The proxy's layer directions, as a test.

``storage`` is the bottom: it imports nothing from the packages above it.
``core``, ``services`` and ``configuration`` never import the surfaces
(``api``, ``ws``). Every absolute import counts, at module level or inside a
function (a function-local import is still a dependency; it only moves the
cycle to run time); an import under ``if TYPE_CHECKING`` does not (it never runs).

``LEAVES`` are the shared vocabulary modules any layer may import: each
imports nothing but the standard library and other leaves, which
``test_the_leaves_are_leaves`` holds. The edges that already existed when the
test was written are listed in ``import_layers_allowlist.json`` beside it: a
NEW edge fails, and so does a listed edge that no longer exists, so the list
only shrinks.

(The engine contract's own seam has its fresh-interpreter test in
``tests/execution/test_import_direction.py``.)
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

PROXY = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("import_layers_allowlist.json")

FORBIDDEN = {
    "storage": {"services", "core", "api", "ws", "auth", "adapters", "hooks"},
    "core": {"api", "ws"},
    "services": {"api", "ws"},
    "configuration": {"api", "ws"},
}
LEAVES = {
    "auth.roles",
    "core.events.common_events",
    "core.permission_modes",
    "core.host_os",
    "core.layout",
    "core.placement",
    "core.session.session_kind",
    "services.scheduler.task_kinds",
    "ws.chat_phase",
    "ws.wire_events",
}


def _module_exists(dotted: str) -> bool:
    path = PROXY.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _type_checking_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (
                (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
                or (isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING")):
            for child in node.body:
                lines.update(range(child.lineno, (child.end_lineno or child.lineno) + 1))
    return lines


def imports_of(path: Path) -> set[str]:
    """The absolute modules ``path`` imports at run time: ``from pkg import
    mod`` names ``pkg.mod`` when that is a module of the proxy."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _type_checking_lines(tree)
    out: set[str] = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", 0) in skip:
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                sub = f"{node.module}.{alias.name}"
                out.add(sub if _module_exists(sub) else node.module)
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return out


def current_edges() -> set[str]:
    """Every ``"<file> -> <module>"`` edge that crosses a forbidden direction."""
    edges: set[str] = set()
    for layer, above in FORBIDDEN.items():
        for path in sorted((PROXY / layer).rglob("*.py")):
            rel = path.relative_to(PROXY).as_posix()
            if "/tests/" in rel:
                continue
            for module in imports_of(path):
                if module.split(".")[0] in above and module not in LEAVES:
                    edges.add(f"{rel} -> {module}")
    return edges


def _allowed() -> set[str]:
    return set(json.loads(ALLOWLIST.read_text(encoding="utf-8"))["edges"])


def test_no_new_upward_import():
    new = sorted(current_edges() - _allowed())
    assert new == [], (
        "a lower layer imports an upper one (storage below everything; core and "
        "services below api and ws). Move the shared piece down, or pass it in:\n"
        + "\n".join(new)
    )


def test_the_allowlist_has_no_stale_edge():
    stale = sorted(_allowed() - current_edges())
    assert stale == [], (
        "these edges are gone: remove them from import_layers_allowlist.json "
        "(the list only shrinks):\n" + "\n".join(stale)
    )


def test_the_allowlist_is_sorted_and_unique():
    edges = json.loads(ALLOWLIST.read_text(encoding="utf-8"))["edges"]
    assert edges == sorted(set(edges))


def test_the_leaves_are_leaves():
    """A leaf any layer may import imports only the standard library and
    other leaves, or it would carry its own dependencies up with it."""
    stdlib = set(sys.stdlib_module_names)
    wrong = []
    for leaf in sorted(LEAVES):
        path = PROXY.joinpath(*leaf.split(".")).with_suffix(".py")
        for module in imports_of(path):
            if module.split(".")[0] in stdlib or module == "__future__" or module in LEAVES:
                continue
            wrong.append(f"{leaf} -> {module}")
    assert wrong == []


def test_every_ws_module_imports_first():
    """``ws/dashboard.py`` assembles the connection from its mixins at its
    end, so a mixin that imported the connection module at its top could not
    be the first module a process imports (a test file collected first, a
    script). Each ``ws`` module is imported alone in a fresh interpreter."""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    modules = sorted(f"ws.{p.stem}" for p in (PROXY / "ws").glob("*.py") if p.stem != "__init__")

    def first_import(module: str) -> str:
        out = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=str(PROXY),
                             capture_output=True, text=True, timeout=180)
        if out.returncode == 0:
            return ""
        return f"{module}: {(out.stderr.strip().splitlines() or ['?'])[-1]}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        failures = [f for f in pool.map(first_import, modules) if f]
    assert failures == []


if __name__ == "__main__":
    # Regenerate the list (only after removing edges; never to admit a new one).
    data = {
        "_comment": "Upward imports that existed when tests/core/test_import_layers.py was "
                    "written. The list only shrinks: remove an edge when it goes; never add one.",
        "edges": sorted(current_edges()),
    }
    ALLOWLIST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{len(data['edges'])} edges")
