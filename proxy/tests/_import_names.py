"""What a module's names stand for: the one reader the surface tests share.

A surface test reads the server's code as syntax and judges each call by the
name it carries. That name means what the module's imports bind it to, so
``import subprocess as sp`` and ``from shutil import rmtree as rm`` are read
through here: ``bindings`` reads one module's imports, ``through`` follows a
name a tree module imported into the module that imports it from there, and
``tree_files`` lists a tree's modules, which a reader parses one at a time
(every tree of the proxy held at once is about half a gigabyte).

A relative import (``from ..config import force_rmtree``) is resolved against
the file's own package when the reader knows the file's place in its tree;
read from text alone it stays dotted (``..config.force_rmtree``).
"""

from __future__ import annotations

import ast
import collections
import contextlib
import gc
from collections.abc import Iterator
from pathlib import Path


def module_of(rel: str) -> str:
    """A file's import name from its path in its tree: ``core/a.py`` is
    ``core.a``, a package's ``__init__.py`` is the package, and a file listed
    from another tree under ``../<tree>/`` is ``<tree>.<path>``."""
    tree, path = "", rel
    if rel.startswith("../"):
        tree, _, path = rel[3:].partition("/")
    name = "" if path == "__init__.py" else path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
    return ".".join(part for part in (tree, name) if part)


def _absolute(level: int, module: str | None, rel: str) -> str | None:
    """The absolute name of ``from <level dots><module> import`` in the file
    ``rel``, or None when the dots climb above the tree."""
    parts = module_of(rel).split(".") if module_of(rel) else []
    package = parts if rel.endswith("__init__.py") else parts[:-1]
    if level - 1 > len(package):
        return None
    base = package[:len(package) - (level - 1)]
    return ".".join([*base, module] if module else base)


def _imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Every import of a module, in ``ast.walk``'s order. An import is a
    statement, so the walk enters statements only, never an expression."""
    found: list[ast.Import | ast.ImportFrom] = []
    queue = collections.deque([tree])
    while queue:
        for child in ast.iter_child_nodes(queue.popleft()):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                found.append(child)
            elif isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                queue.append(child)
    return found


def bindings(tree: ast.AST, rel: str | None = None) -> dict[str, str]:
    """The names a module's imports bind, wherever in the module they stand:
    ``import x as y`` binds ``y`` to ``x``, ``import a.b`` binds ``a``, and
    ``from x import name as n`` binds ``n`` to ``x.name``. With ``rel``, the
    file's path in its tree, a relative import is made absolute."""
    bound: dict[str, str] = {}
    for node in _imports(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                bound[alias.asname or head] = alias.name if alias.asname else head
        elif isinstance(node, ast.ImportFrom):
            module = (_absolute(node.level, node.module, rel) if node.level and rel is not None else None)
            if module is None:
                module = "." * node.level + (node.module or "")
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{module}.{alias.name}"
    return bound


def through(target: str, exports: dict[str, dict[str, str]]) -> str:
    """``target`` (``module.name``) followed through the tree's own modules:
    a name a module of the tree imported is what that module bound it to, as
    far as the chain goes."""
    for _hop in range(5):
        module, _, name = target.rpartition(".")
        onward = exports.get(module, {}).get(name)
        if onward is None or onward == target:
            break
        target = onward
    return target


@contextlib.contextmanager
def paused_gc() -> Iterator[None]:
    """The collector off while a tree's modules are parsed and walked: their
    syntax is many small objects with no cycle among them, and the
    collector's passes over them cost more than the parse."""
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


def tree_files(root: Path, prefix: str, excluded: set[str],
               skip: frozenset[str] = frozenset()) -> list[tuple[str, Path]]:
    """Each ``.py`` under ``root``, keyed by ``prefix`` and its path in the
    tree; a file with a part in ``excluded`` or a key in ``skip`` is left
    out."""
    files = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        key = prefix + rel.as_posix()
        if not excluded & set(rel.parts) and key not in skip:
            files.append((key, path))
    return files
