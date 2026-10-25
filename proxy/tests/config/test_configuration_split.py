"""``config.py`` is the facade of the ``configuration`` package.

The model registry (``configuration.models``), the system prompt's rendering
(``configuration.prompts``) and the value parsers with the key registry
(``configuration.readers``) live in the package; ``config`` re-exports every
public name, so ``config.<name>`` keeps answering for every importer. The
package is a leaf at import time: it loads the standard library, ``psycopg``
and the vocabulary leaves only, and reads the facade inside functions, so
importing it never runs ``config`` (which reads config.env and writes the
generated secrets). Every config key stays read in ``config.py``, where
``test_config_keys.py`` finds it.

A function of the package that calls a sibling reads it in its own module:
``resolve_run_model`` calls ``configuration.models.get_cli_model``, so a test
whose code reaches ``get_cli_model`` through ``resolve_run_model`` patches it
there; a patch on ``config`` reaches only the callers that read
``config.get_cli_model``. ``KNOWN_FACADE_PATCHES`` lists, with the reason,
every test that patches such a name on ``config`` because its code reads the
facade, and every patch site whose target or name the scan cannot resolve
(it fails closed), and stays exact in both directions.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import json
import re
import subprocess
import sys

import pytest

import config
from tests._paths import PROXY_DIR

MODULES = ("configuration.models", "configuration.prompts", "configuration.readers")
LEAVES = {"auth.roles", "core.layout", "core.placement"}
THIRD_PARTY = {"psycopg"}
KEY_READERS = {"_cfg", "_cfg_bool", "_cfg_int", "_cfg_float"}
SURFACES = {"api", "ws"}
# A patch site whose target or name the scan cannot resolve: it may be the
# facade, so it is listed like any other.
DYNAMIC = "<dynamic>"

# {test path: (reason, {name: count})}: a patch on ``config`` of a name a
# function of the package also reads bare, kept because the code under test
# reads it through the facade.
_PROVIDER = "the code under test reads config.get_model_provider"
KNOWN_FACADE_PATCHES: dict[str, tuple[str, dict[str, int]]] = {
    "proxy/tests/agents/test_agent_context.py": (
        "the delegation roster reads config.resolve_agent_model", {"resolve_agent_model": 2}),
    "proxy/tests/agents/test_agent_grid_counts.py": (
        "the agents list reads config.resolve_layer_default_model", {"resolve_layer_default_model": 1}),
    "proxy/tests/billing/test_codex_local_provider.py": (
        "the subscription pool: " + _PROVIDER, {"get_model_provider": 2}),
    "proxy/tests/billing/test_hosted_llm_resolution.py": (
        "the subscription pool: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/billing/test_subscriptions.py": (
        "the subscription pool: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/execution/test_direct_change_model.py": (
        "the Direct LLM layer's model change: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/execution/test_direct_llm_target_local.py": (
        "the session config builder reads config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/loadtest/conftest.py": (
        "restores HOST, PORT, INTERNAL_LISTENER_PORT and DATABASE_URL from a dict (all facade names)",
        {DYNAMIC: 1}),
    "proxy/tests/fixtures/ws_dashboard_harness.py": (
        "the dashboard socket's warmup and config builder read config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/core/test_loop_store_inventory.py": (
        "puts back the real config.get_cli_model the harness stubs: the warmup reads the facade",
        {"get_cli_model": 1}),
    "proxy/tests/meetings/test_meeting_placement.py": (
        "the meeting context reads config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/phone/test_phone_config_builder_identity.py": (
        "the phone config builder reads config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/phone/test_ws_phone_turns.py": (
        "the phone socket reads config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/remote/test_remote_local_endpoint_payload.py": (
        "the chat's provider-switch guard: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/remote/test_transcript_tailer.py": (
        "the usage rows name the provider: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/session/test_codex_model_switch_guard.py": (
        "the chat's provider-switch guard: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/session/test_codex_rollout_tailer.py": (
        "the usage rows name the provider: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/session/test_model_layer_guard.py": (
        "the warmup's engine default reads config.resolve_layer_default_model",
        {"resolve_layer_default_model": 2}),
    "proxy/tests/session/test_ws_prewarm_seat.py": (
        "the warmup reads config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/tasks/test_delegation_spawn.py": (
        "the delegation route's provider check: " + _PROVIDER, {"get_model_provider": 1}),
    "proxy/tests/tasks/test_self_delegation_target.py": (
        "the session config builder reads config.get_cli_model", {"get_cli_model": 1}),
    "proxy/tests/tasks/test_task_model_override.py": (
        "the tasks list reads config.resolve_agent_model", {"resolve_agent_model": 1}),
    "proxy/tests/tasks/test_task_rewarm_scope.py": (
        "the session config builder reads config.get_cli_model", {"get_cli_model": 1}),
}


def _path(module: str):
    return PROXY_DIR.joinpath(*module.split(".")).with_suffix(".py")


def _tree(module: str) -> ast.Module:
    return ast.parse(_path(module).read_text(encoding="utf-8"))


def _defined(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _bound_in(fn: ast.AST) -> set[str]:
    """The names a function binds itself (parameters, assignments, imports)."""
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            out.add(n.name)
    return out


def read_bare() -> set[str]:
    """The names a top-level statement of the package reads from its own
    module's globals, other than the statement's own: the names a patch on
    ``config`` would not reach."""
    out: set[str] = set()
    for module in MODULES:
        tree = _tree(module)
        names = _defined(tree)
        for node in tree.body:
            loads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                loads -= _bound_in(node)
                loads.discard(node.name)
            out |= {n for n in loads & names if not hasattr(builtins, n)}
    return out


_REBINDS = {"setattr", "delattr", "patch", "object", "multiple"}


def module_aliases() -> set[str]:
    """``config`` and every name a proxy module imports it as
    (``import config as app_config``): a test reaches the facade through a
    module's binding too (``openai_adapter.app_config``)."""
    out = {"config"}
    for path in PROXY_DIR.rglob("*.py"):
        rel = path.relative_to(PROXY_DIR).as_posix()
        if rel.startswith(("tests/", "venv/")):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                out |= {a.asname for a in node.names if a.name == "config" and a.asname}
    return out


# patch.multiple's own keywords: everything else it receives is a patched name.
_MULTIPLE_OWN = {"target", "spec", "create", "spec_set", "autospec", "new_callable"}
_IMPORTERS = {"import_module", "__import__"}


def _local_aliases(tree: ast.AST) -> set[str]:
    """The names a test module binds to ``config``: ``import config [as x]``,
    ``x = importlib.import_module("config")``, ``x = sys.modules["config"]``."""
    out = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
           for a in n.names if a.name == "config"}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        if isinstance(value, ast.Call):
            func = value.func.attr if isinstance(value.func, ast.Attribute) else getattr(value.func, "id", "")
            arg = value.args[0] if value.args else None
            if (func in _IMPORTERS and isinstance(arg, ast.Constant) and arg.value == "config"):
                out.add(node.targets[0].id)
        elif (isinstance(value, ast.Subscript) and ast.unparse(value.value) == "sys.modules"
              and isinstance(value.slice, ast.Constant) and value.slice.value == "config"):
            out.add(node.targets[0].id)
    return out


_NOT_A_PATH = re.compile(r"[^\w.]")


def _is_mock_patch(func: ast.AST) -> bool:
    """``patch(...)``, ``mock.patch(...)`` and their ``.object`` /
    ``.multiple``: unittest.mock's, never a test client's ``.patch``."""
    if isinstance(func, ast.Attribute) and func.attr in ("object", "multiple"):
        func = func.value
    if isinstance(func, ast.Name):
        return func.id == "patch"
    return (isinstance(func, ast.Attribute) and func.attr == "patch"
            and ast.unparse(func.value) in ("mock", "unittest.mock"))


def _is_config(expr: ast.AST | None, local: set[str], bound: set[str]) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in local
    return isinstance(expr, ast.Attribute) and expr.attr in bound


def _strings(expr: ast.AST | None, parents: dict, tree: ast.AST,
             seen: frozenset[str] = frozenset()) -> list[str] | None:
    """The string values ``expr`` can hold: a constant, or a name bound by an
    enclosing ``for`` over a literal tuple or list (``for k, v in (("A", 1),
    ...)`` too) or assigned such values in the module, an f-string or a
    conditional of them. None when unresolved."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [expr.value]
    if isinstance(expr, ast.IfExp):
        body = _strings(expr.body, parents, tree, seen)
        orelse = _strings(expr.orelse, parents, tree, seen)
        return None if body is None or orelse is None else body + orelse
    if isinstance(expr, ast.JoinedStr):
        parts: list[list[str]] = []
        for value in expr.values:
            got = _strings(value.value if isinstance(value, ast.FormattedValue) else value,
                           parents, tree, seen)
            if got is None:
                return None
            parts.append(got)
        combos = [""]
        for options in parts:
            combos = [c + o for c in combos for o in options]
        return combos
    if not isinstance(expr, ast.Name) or expr.id in seen:
        return None
    node = expr
    while node in parents:
        node = parents[node]
        if not (isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension))
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            continue
        if isinstance(node.target, ast.Name) and node.target.id == expr.id:
            picks = list(node.iter.elts)
        elif isinstance(node.target, ast.Tuple) and expr.id in [
                t.id for t in node.target.elts if isinstance(t, ast.Name)]:
            i = [getattr(t, "id", None) for t in node.target.elts].index(expr.id)
            picks = [e.elts[i] if isinstance(e, (ast.Tuple, ast.List)) and len(e.elts) > i else None
                     for e in node.iter.elts]
        else:
            continue
        if all(isinstance(p, ast.Constant) and isinstance(p.value, str) for p in picks):
            return [p.value for p in picks]
        return None
    assigned = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                and any(isinstance(t, ast.Name) and t.id == expr.id
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    if not assigned or any(not isinstance(n, ast.Assign) for n in assigned):
        return None
    values: list[str] = []
    for n in assigned:
        got = _strings(n.value, parents, tree, seen | {expr.id})
        if got is None:
            return None
        values += got
    return values


def facade_patches(text: str, bound: set[str]) -> list[str]:
    """The names a test module rebinds on ``config``, one entry per site:
    through its own binding (``import config``, ``import_module``), through a
    module's (``<module>.<alias>``) or a string target, the name a constant, a
    keyword (``target=``, ``name=``, ``attribute=``) or a loop variable over a
    literal tuple. A site whose target or name cannot be resolved is
    ``DYNAMIC``: it may be the facade, so it has to be listed."""
    tree = ast.parse(text)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    local = _local_aliases(tree)
    aliases = "|".join(sorted(bound))
    dotted = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?(?:" + aliases + r")\.([A-Za-z_]\w*)$")
    whole = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?(?:" + aliases + r")$")
    out: list[str] = []

    def names_of(expr: ast.AST | None) -> list[str]:
        values = _strings(expr, parents, tree)
        return values if values is not None else [DYNAMIC]

    def string_target(expr: ast.AST | None) -> list[str]:
        """The facade names a string target names (``"a.config.X"``)."""
        values = _strings(expr, parents, tree)
        if values is None and isinstance(expr, ast.JoinedStr) and _NOT_A_PATH.search("".join(
                v.value for v in expr.values if isinstance(v, ast.Constant))):
            return []  # its literal text alone rules out a dotted module path
        if values is None:
            return [DYNAMIC]
        return [m.group(1) for v in values if (m := dotted.match(v))]

    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                and _is_config(node.value, local, bound)):
            out.append(node.attr)
        if not isinstance(node, ast.Call):
            continue
        func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if func not in _REBINDS:
            continue
        if func in ("patch", "object", "multiple") and not _is_mock_patch(node.func):
            continue  # a test client's HTTP PATCH, an unrelated .object
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        args = list(node.args)
        first = args[0] if args else kw.get("target")
        if func == "multiple":
            facade = _is_config(first, local, bound) or any(
                whole.match(v) for v in (_strings(first, parents, tree) or []))
            if facade:
                out.extend(k for k in kw if k not in _MULTIPLE_OWN)
                out.extend(DYNAMIC for k in node.keywords if k.arg is None)
            continue
        if _is_config(first, local, bound):
            name = args[1] if len(args) > 1 else kw.get("name", kw.get("attribute"))
            out.extend(names_of(name))
        elif func == "patch" or (func in ("setattr", "delattr") and (
                len(args) + ("value" in kw) <= (1 if func == "delattr" else 2)
                and "name" not in kw)):
            # A string target: mock.patch's, or monkeypatch's one-string form.
            out.extend(string_target(first))
    return out


def _found() -> dict[str, dict[str, int]]:
    watched = read_bare()
    bound = module_aliases()
    found: dict[str, dict[str, int]] = {}
    for path in sorted((PROXY_DIR / "tests").rglob("*.py")):
        rel = path.relative_to(PROXY_DIR.parent).as_posix()
        for name in facade_patches(path.read_text(encoding="utf-8"), bound):
            if name in watched or name == DYNAMIC:
                found.setdefault(rel, {}).setdefault(name, 0)
                found[rel][name] += 1
    return found


def test_a_test_patches_a_name_where_its_reader_is():
    found = _found()
    known = {path: counts for path, (_, counts) in KNOWN_FACADE_PATCHES.items()}
    new = {p: c for p, c in found.items() if known.get(p) != c}
    gone = {p: c for p, c in known.items() if found.get(p) != c}
    assert not new, (
        "a test patches on config a name a configuration function reads in its own module: "
        "patch it on configuration.<module> when the code under test reaches it through the "
        f"package, or list the site in KNOWN_FACADE_PATCHES with the facade reader: {new}")
    assert not gone, f"retired or changed: update KNOWN_FACADE_PATCHES: {gone}"


@pytest.mark.parametrize("module", MODULES)
def test_every_public_name_reads_the_same_object_on_config(module):
    mod = importlib.import_module(module)
    public = sorted(n for n in _defined(_tree(module)) if not n.startswith("_"))
    assert public
    missing = [n for n in public if getattr(config, n, None) is not getattr(mod, n)]
    assert missing == [], f"config does not re-export {module}: {missing}"


@pytest.mark.parametrize("module", MODULES)
def test_the_module_imports_only_leaves_at_import(module):
    stdlib = set(sys.stdlib_module_names)
    wrong = []
    for node in _tree(module).body:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [f"{node.module}.{a.name}" if f"{node.module}.{a.name}" in LEAVES else node.module
                     for a in node.names]
        else:
            continue
        wrong += [n for n in names if n.split(".")[0] not in stdlib | THIRD_PARTY and n not in LEAVES]
    assert wrong == [], f"{module} imports at module level: {wrong}"


@pytest.mark.parametrize("module", MODULES)
def test_the_package_never_imports_the_surfaces(module):
    """Function-local imports included, as for core and services."""
    wrong = []
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            wrong += [a.name for a in node.names if a.name.split(".")[0] in SURFACES]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in SURFACES:
            wrong.append(node.module)
    assert wrong == []


def test_importing_the_package_never_runs_config():
    script = ("import json, sys\n"
              + "".join(f"import {m}\n" for m in MODULES)
              + "print(json.dumps(sorted(sys.modules)))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(PROXY_DIR),
                         capture_output=True, text=True, check=True)
    loaded = set(json.loads(out.stdout))
    assert "config" not in loaded
    assert not {m for m in loaded if m.split(".")[0] in {"storage", "services", "api", "ws", "adapters"}}


@pytest.mark.parametrize("module", MODULES)
def test_no_config_key_is_read_in_the_package(module):
    calls = [ast.unparse(n)[:80] for n in ast.walk(_tree(module))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in KEY_READERS]
    assert calls == [], f"{module} reads a key test_config_keys.py cannot see: {calls}"
