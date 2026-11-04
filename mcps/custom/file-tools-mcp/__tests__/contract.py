"""The key-coverage contract every module with a ``CATALOG`` keeps.

A catalogue is only true if its handlers read exactly the keys it declares:
a key read but not declared is refused at the door, and a key declared but
read under another name is silently ignored. Two passes hold it:

- at runtime, every example of every operation runs alone (after its
  ``setup``) through ``run_batch`` with key recording, its paths swapped for
  fixtures; it must not fail, must read only declared keys (canonical
  spellings: an alias never reaches a handler), and must never read the
  whole operation (a copy hides every later read);
- statically, an AST pass follows the operation from each handler into the
  module-level helpers it is handed to (in the handler's module and in any
  module of the folder it imports) and refuses an undeclared literal key, a
  key it cannot resolve, and any other use of the operation.

A module opts in by defining ``CATALOG`` and ``contract_job(tmp_dir)`` (and
``contract_fixtures(tmp_dir)`` when an operation reads a file).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import functools
import importlib
import inspect
import itertools
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from batch import declared_names, run_batch
from catalog import Catalog
from results import Status

MCP_DIR = Path(__file__).resolve().parent.parent
_READ_METHODS = frozenset({"get", "pop", "setdefault", "__contains__", "__getitem__"})
# Iterating the operation reads every key it holds (exempt for an op that
# says reads_whole_op); copying it hides every later read (never exempt).
_WHOLE_METHODS = frozenset({"items", "keys", "values", "__iter__"})
_COPY_METHODS = frozenset({"copy", "update"})
_ALLOWED_CALLS = frozenset({"isinstance"})


_CATALOG_LINE = re.compile(r"^CATALOG\b[^=\n]*=", re.MULTILINE)
FORMATS_PACKAGE = "formats"


def _module_name(path: Path, folder: Path) -> str:
    return ".".join(path.relative_to(folder).with_suffix("").parts).removesuffix(".__init__")


def catalogue_modules(folder: Path = MCP_DIR) -> list[str]:
    """Modules of the folder and of its ``formats`` sub-packages that assign
    ``CATALOG`` at top level (``CATALOG = Catalog(...)``, with an annotation
    or from a builder), found by reading their source: the others are never
    imported."""
    paths = [*sorted(folder.glob("*.py")), *sorted((folder / FORMATS_PACKAGE).glob("**/*.py"))]
    return [_module_name(path, folder) for path in paths
            if _CATALOG_LINE.search(path.read_text(encoding="utf-8"))]


@contextlib.contextmanager
def _fixture_paths(fixtures: dict[str, str], tmp_dir: str):
    import shared

    real = shared._resolve_path

    async def resolve(value, writing=False, **kw):
        return fixtures.get(value, os.path.join(tmp_dir, os.path.basename(value)))

    shared._resolve_path = resolve
    try:
        yield
    finally:
        shared._resolve_path = real


def run_examples(catalog: Catalog, job_factory: Callable[[str], object], tmp_dir: str,
                 fixtures: dict[str, str] | None = None) -> list[str]:
    """Every problem the runtime pass finds, one line each."""
    problems: list[str] = []
    fixtures = fixtures or {}
    for spec in catalog.ops.values():
        if not spec.examples:
            problems.append(f"{spec.name}: no example")
            continue
        declared = declared_names(spec.keys)
        given: set[str] = set()
        for number, example in enumerate(spec.examples):
            ops = list(enumerate([*spec.setup, example]))
            canonical, refused, _ = catalog.resolve_batch(ops)
            if refused:
                problems.append(f"{spec.name} example {number}: does not resolve: {refused[0].message}")
                continue
            with _fixture_paths(fixtures, tmp_dir):
                canonical, unresolved = asyncio.run(catalog.preresolve(canonical))
            if unresolved:
                problems.append(f"{spec.name} example {number}: a path did not resolve: {unresolved[0].message}")
                continue
            job = job_factory(tmp_dir)
            work_dir = os.path.join(tmp_dir, f"work-{spec.name}-{number}")
            os.makedirs(work_dir, exist_ok=True)
            *setup, (index, op) = canonical
            if setup:
                run_batch(setup, catalog, job, work_dir=work_dir)
            outcome = run_batch([(index, op)], catalog, job, work_dir=work_dir, record_keys=True)
            result = outcome.results[0]
            if result.status is Status.FAILED:
                problems.append(f"{spec.name} example {number}: failed: {result.message}")
            extra = sorted(outcome.keys_read.get(index, set()) - declared)
            if extra:
                problems.append(f"{spec.name} example {number}: reads undeclared keys {extra}")
            whole = outcome.whole_access.get(index)
            if whole and not spec.reads_whole_op:
                problems.append(f"{spec.name} example {number}: reads the whole operation ({', '.join(whole)})")
            given |= set(op)
        for group in spec.one_of:
            missing = [m for m in group if m not in given]
            if missing:
                problems.append(f"{spec.name}: no example gives {', '.join(missing)}")
    return problems


# ---------------------------------------------------------------------------
# The static pass
# ---------------------------------------------------------------------------


def _unwrap(fn):
    while True:
        if isinstance(fn, functools.partial):
            fn = fn.func
        elif hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        else:
            return fn


@functools.cache
def _module_ast(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)


def _top_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _module_file(module: str | None, level: int, current: Path, folder: Path) -> Path | None:
    """The file of a module of the folder named by an import (absolute,
    dotted, or relative to ``current``), else ``None``."""
    if level:
        base = current.parent
        for _ in range(level - 1):
            base = base.parent
    else:
        base = folder
    parts = module.split(".") if module else []
    target = base.joinpath(*parts)
    if parts and target.with_suffix(".py").is_file():
        return target.with_suffix(".py")
    if (target / "__init__.py").is_file() and (level or parts):
        return target / "__init__.py"
    return None


def _imports(nodes: list[ast.AST], current: Path, folder: Path) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """(name -> (module file, function) for ``from m import f``, alias ->
    module file for ``import m`` and ``from package import module``),
    limited to modules of the folder."""
    names: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            source = _module_file(node.module, node.level, current, folder)
            for alias in node.names:
                sub = _module_file(f"{node.module}.{alias.name}" if node.module else alias.name, node.level,
                                   current, folder)
                if sub is not None and sub.name != "__init__.py":
                    modules[alias.asname or alias.name] = str(sub)
                elif source is not None:
                    names[alias.asname or alias.name] = (str(source), alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                path = _module_file(alias.name, 0, current, folder)
                if path is not None:
                    modules[alias.asname or alias.name.split(".")[0]] = str(path)
    return names, modules


def _literal_keys(node: ast.AST) -> set[str] | None:
    """The keys of a dict literal whose keys are all strings."""
    if isinstance(node, ast.Dict) and node.keys and all(
            isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
        return {k.value for k in node.keys}
    return None


# Builtins that consume a whole mapping when handed the operation.
# Methods that change a dict in place.
_READ_METHODS = frozenset({"keys", "items", "values", "get"})


def _parameters(fn: ast.AST) -> list[str]:
    args = getattr(fn, "args", None)
    if args is None:
        return []
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return names + [a.arg for a in (args.vararg, args.kwarg) if a is not None]


_WHOLE_CONSUMERS = frozenset({"list", "tuple", "set", "sorted", "iter", "len"})
_COPY_CONSUMERS = frozenset({"dict", "str", "repr"})
WHOLE_ISSUE = "reads the whole operation"
COPY_ISSUE = "copies the operation, which hides every later read"


class _Scan(ast.NodeVisitor):
    """The keys one function reads from the operation bound to ``param``;
    ``bindings`` are the callee's parameters a call site gave as literal
    strings (a key name handed to a helper)."""

    def __init__(self, path: str, fn: ast.FunctionDef, param: str, seen: set, folder: Path,
                 bindings: dict[str, set[str] | None] | None = None) -> None:
        self.path = path
        self.fn = fn
        self.param = param
        self.seen = seen
        self.folder = folder
        self.keys: list[tuple[str, str]] = []
        self.issues: list[str] = []
        self.bindings: dict[str, set[str] | None] = dict(bindings or {})
        self.dict_keys: dict[str, set[str] | None] = {}
        tree = _module_ast(path)
        self.functions = _top_functions(tree)
        self.functions.update({n.name: n for n in ast.walk(fn)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn})
        local_imports = [n for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.imported, self.modules = _imports([*tree.body, *local_imports], Path(path), folder)
        self._parents: dict[int, ast.AST] = {}

    def where(self, node: ast.AST) -> str:
        return f"{Path(self.path).name}:{getattr(node, 'lineno', self.fn.lineno)}"

    def run(self) -> None:
        for parent in ast.walk(self.fn):
            for child in ast.iter_child_nodes(parent):
                self._parents[id(child)] = parent
        self._bind()
        self.visit(self.fn)

    # ------------------------------------------------------------ bindings

    def _strings(self, node: ast.AST) -> set[str] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id)
        if isinstance(node, ast.IfExp):
            body, orelse = self._strings(node.body), self._strings(node.orelse)
            return None if body is None or orelse is None else body | orelse
        if isinstance(node, ast.JoinedStr):
            parts: list[set[str]] = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append({str(value.value)})
                elif isinstance(value, ast.FormattedValue) and value.conversion == -1 and value.format_spec is None:
                    found = self._strings(value.value)
                    if found is None:
                        return None
                    parts.append(found)
                else:
                    return None
            return {"".join(p) for p in itertools.product(*parts)}
        return None

    def _mapping_keys(self, node: ast.AST) -> set[str] | None:
        """The keys a loop over a dict literal (or a name bound to one, or
        its ``.keys()``/``.items()``) yields."""
        if isinstance(node, ast.Call) and not node.args and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("keys", "items"):
            node = node.func.value
        if isinstance(node, ast.Name):
            return self.dict_keys.get(node.id)
        return _literal_keys(node)

    def _bind_target(self, target: ast.AST, iterable: ast.AST) -> None:
        keys = self._mapping_keys(iterable)
        if keys is not None:
            first = target.elts[0] if isinstance(target, ast.Tuple) and target.elts else target
            if isinstance(first, ast.Name):
                self._merge(first.id, keys)
            return
        if not isinstance(iterable, (ast.Tuple, ast.List, ast.Set)):
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    self.bindings[name.id] = None
            return
        if isinstance(target, ast.Name):
            values = [self._strings(e) for e in iterable.elts]
            merged = set().union(*values) if values and all(v is not None for v in values) else None
            self._merge(target.id, merged)
        elif isinstance(target, ast.Tuple):
            for position, element in enumerate(target.elts):
                if not isinstance(element, ast.Name):
                    continue
                values = []
                for row in iterable.elts:
                    if isinstance(row, ast.Tuple) and position < len(row.elts):
                        values.append(self._strings(row.elts[position]))
                    else:
                        values.append(None)
                merged = set().union(*values) if values and all(v is not None for v in values) else None
                self._merge(element.id, merged)

    def _merge(self, name: str, values: set[str] | None) -> None:
        if name in self.bindings:
            old = self.bindings[name]
            self.bindings[name] = None if old is None or values is None else old | values
        else:
            self.bindings[name] = values

    def _stable_dict_literals(self) -> dict[str, set[str]]:
        """Names bound to a dict literal exactly once (a parameter counts as
        a binding) and only ever read: iterated, its keys, items or values
        taken, indexed, tested with ``in``. Any other use (an alias, an
        element of a container, an argument, an item store, a mutating
        method) could change it, so only a read-only literal's keys are
        known."""
        bindings: Counter[str] = Counter(_parameters(self.fn))
        literal: dict[str, set[str]] = {}
        loads: list[ast.Name] = []
        for node in ast.walk(self.fn):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    keys = _literal_keys(node.value)
                    if keys is not None:
                        literal[targets[0].id] = keys
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.comprehension, ast.NamedExpr)):
                targets = [node.target]
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                targets = [node.optional_vars]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        bindings[name.id] += 1
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loads.append(node)
        changed = {name.id for name in loads if name.id in literal and not self._read_only_use(name)}
        return {name: keys for name, keys in literal.items() if bindings[name] == 1 and name not in changed}

    def _read_only_use(self, name: ast.Name) -> bool:
        parent = self._parents.get(id(name))
        if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is name:
            return True
        if isinstance(parent, ast.Attribute) and parent.value is name:
            call = self._parents.get(id(parent))
            return parent.attr in _READ_METHODS and isinstance(call, ast.Call) and call.func is parent
        if isinstance(parent, ast.Subscript) and parent.value is name:
            return isinstance(parent.ctx, ast.Load)
        if isinstance(parent, ast.Compare) and name in parent.comparators:
            position = parent.comparators.index(name)
            return isinstance(parent.ops[position], (ast.In, ast.NotIn))
        return False

    def _bind(self) -> None:
        self.dict_keys.update(self._stable_dict_literals())
        for _ in range(2):
            for node in ast.walk(self.fn):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    if node.targets[0].id not in self.dict_keys:
                        self._merge(node.targets[0].id, self._strings(node.value))
                elif isinstance(node, (ast.For, ast.comprehension)):
                    self._bind_target(node.target, node.iter)

    # --------------------------------------------------------------- reads

    def _key(self, node: ast.AST, at: ast.AST) -> None:
        values = self._strings(node)
        if values is None:
            self.issues.append(f"{self.where(at)}: a key the contract cannot resolve; read it with a literal key")
            return
        self.keys.extend((v, self.where(at)) for v in sorted(values))

    def visit_Name(self, node: ast.Name) -> None:
        if node.id != self.param or isinstance(node.ctx, ast.Store):
            return
        parent = self._parents.get(id(node))
        if isinstance(parent, ast.Subscript) and parent.value is node:
            self._key(parent.slice, parent)
            return
        if isinstance(parent, ast.Attribute) and parent.value is node:
            call = self._parents.get(id(parent))
            if parent.attr in _READ_METHODS and isinstance(call, ast.Call) and call.func is parent and call.args:
                self._key(call.args[0], call)
                return
            if parent.attr in _WHOLE_METHODS:
                self.issues.append(f"{self.where(parent)}: {WHOLE_ISSUE} ({parent.attr})")
                return
            if parent.attr in _COPY_METHODS:
                self.issues.append(f"{self.where(parent)}: {COPY_ISSUE} ({parent.attr})")
                return
            self.issues.append(f"{self.where(parent)}: uses the operation's attribute '{parent.attr}'")
            return
        if isinstance(parent, ast.Compare) and node in parent.comparators:
            if len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.In, ast.NotIn)):
                self._key(parent.left, parent)
                return
        if isinstance(parent, ast.keyword) and parent.arg is None:
            self.issues.append(f"{self.where(node)}: {COPY_ISSUE} (** unpacking)")
            return
        if isinstance(parent, ast.keyword):
            call = self._parents.get(id(parent))
            if isinstance(call, ast.Call) and self._follow(call, keyword=parent.arg):
                return
        if isinstance(parent, ast.Call) and node in parent.args:
            if isinstance(parent.func, ast.Name) and parent.func.id in _ALLOWED_CALLS:
                return
            if isinstance(parent.func, ast.Name) and parent.func.id in _WHOLE_CONSUMERS:
                self.issues.append(f"{self.where(node)}: {WHOLE_ISSUE} ({parent.func.id})")
                return
            if isinstance(parent.func, ast.Name) and parent.func.id in _COPY_CONSUMERS:
                self.issues.append(f"{self.where(node)}: {COPY_ISSUE} ({parent.func.id})")
                return
            if self._follow(parent, position=parent.args.index(node)):
                return
        self.issues.append(f"{self.where(node)}: the operation is used in a way the contract cannot follow "
                           "(a lambda, a method, a container); read its keys in the handler or a helper "
                           "function of the folder")

    def _follow(self, call: ast.Call, *, position: int | None = None, keyword: str | None = None) -> bool:
        target = self._resolve_callee(call.func)
        if target is None:
            return False
        path, fn = target
        params = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
        kwonly = [a.arg for a in fn.args.kwonlyargs]
        if keyword is not None:
            param = keyword if keyword in params or keyword in kwonly else None
        else:
            param = params[position] if position is not None and position < len(params) else None
        if param is None:
            return False
        scan_function(path, fn, param, self.seen, self.folder, self.keys, self.issues,
                      bindings=self._call_bindings(call, fn, params, param))
        return True

    def _call_bindings(self, call: ast.Call, fn: ast.FunctionDef, params: list[str],
                       param: str) -> dict[str, set[str] | None]:
        """The callee's other parameters as the call site gives them: a
        literal (or a literal default when nothing is passed) resolves; a
        value the pass cannot resolve, and every parameter a ``*`` or ``**``
        argument may fill, is unresolved (``None``), never its default."""
        bound: dict[str, set[str] | None] = {}
        others = [p for p in params if p != param]
        kwonly = [a.arg for a in fn.args.kwonlyargs]
        defaults = fn.args.defaults
        for name, default in zip(params[len(params) - len(defaults):], defaults, strict=True):
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                bound[name] = {default.value}
        for arg, kw_default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
            if isinstance(kw_default, ast.Constant) and isinstance(kw_default.value, str):
                bound[arg.arg] = {kw_default.value}
        for position, arg in enumerate(call.args):
            if isinstance(arg, ast.Starred):
                for name in params[position:]:
                    if name != param:
                        bound[name] = None
                break
            if position < len(params) and params[position] != param:
                bound[params[position]] = self._strings(arg)
        for keyword in call.keywords:
            if keyword.arg is None:
                for name in [*others, *kwonly]:
                    bound[name] = None
            elif keyword.arg != param:
                bound[keyword.arg] = self._strings(keyword.value)
        return bound

    def _resolve_callee(self, func: ast.AST) -> tuple[str, ast.FunctionDef] | None:
        if isinstance(func, ast.Name):
            if func.id in self.functions:
                return self.path, self.functions[func.id]
            if func.id in self.imported:
                path, name = self.imported[func.id]
                fn = _top_functions(_module_ast(path)).get(name)
                return (path, fn) if fn is not None else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in self.modules:
            path = self.modules[func.value.id]
            fn = _top_functions(_module_ast(path)).get(func.attr)
            return (path, fn) if fn is not None else None
        return None


def scan_function(path: str, fn: ast.FunctionDef, param: str, seen: set, folder: Path,
                  keys: list[tuple[str, str]], issues: list[str],
                  bindings: dict[str, set[str] | None] | None = None) -> None:
    marker = (path, fn.name, fn.lineno, param,
              frozenset((k, None if v is None else frozenset(v)) for k, v in (bindings or {}).items()))
    if marker in seen:
        return
    seen.add(marker)
    scan = _Scan(path, fn, param, seen, folder, bindings)
    scan.run()
    keys.extend(scan.keys)
    issues.extend(scan.issues)


def static_problems(catalog: Catalog, folder: Path = MCP_DIR) -> list[str]:
    """Every problem the static pass finds, one line each. An operation whose
    OpSpec says ``reads_whole_op`` may read the whole operation (its keys are
    the declared ones by construction)."""
    problems: list[str] = []
    for spec in catalog.ops.values():
        handler = _unwrap(spec.handler)
        path = inspect.getsourcefile(handler)
        tree = _module_ast(path)
        fn = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == handler.__name__ and n.lineno == handler.__code__.co_firstlineno), None)
        if fn is None:
            problems.append(f"{spec.name}: the handler's source was not found")
            continue
        params = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
        if len(params) < 3:
            problems.append(f"{spec.name}: a handler takes (job, ctx, op)")
            continue
        keys: list[tuple[str, str]] = []
        issues: list[str] = []
        scan_function(path, fn, params[2], set(), folder, keys, issues)
        declared = declared_names(spec.keys)
        for key, where in keys:
            if key not in declared:
                problems.append(f"{spec.name}: {where}: reads '{key}', which the catalogue does not declare")
        problems.extend(f"{spec.name}: {issue}" for issue in issues
                        if not (spec.reads_whole_op and WHOLE_ISSUE in issue))
    return problems


def check_module(name: str, tmp_dir: str) -> list[str]:
    """Both passes on a module that opted in."""
    if str(MCP_DIR) not in sys.path:
        sys.path.insert(0, str(MCP_DIR))
    module = importlib.import_module(name)
    fixtures = module.contract_fixtures(tmp_dir) if hasattr(module, "contract_fixtures") else {}
    return [*run_examples(module.CATALOG, module.contract_job, tmp_dir, fixtures),
            *static_problems(module.CATALOG)]
