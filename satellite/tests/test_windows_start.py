"""Every module of the satellite imports on Windows and on macOS, as a test.

mypy knows the standard library's names per system (``--platform``). It
checks the satellite for Linux, Windows and macOS with the same flags, at
the host floor (``PYTHON_MIN_VERSION`` in VERSIONS.md) and with no site
packages, so third-party modules read as ``Any`` and the result is the same
in every environment. A finding that the Windows or the macOS run has and
the Linux run lacks is a name that system lacks. Where it is read decides:

- At import (a module's body, a class body, a decorator, a default value):
  the module cannot load on that system, and the program dies before it
  signs in. Always a failure.
- Inside a function: it runs only when called, behind the satellite's own
  guard (``config.HOST``, ``_WINDOWS``), which mypy cannot read. Each one is
  listed in ``platform_names.json`` beside this test, by system, file,
  function and name: a new one fails until it is listed, and a listed one
  that is gone fails too, so the list only shrinks.

mypy cannot see a module a system lacks: its stub is empty there, so
``import fcntl`` checks clean for Windows. The syntax tree covers that part:
no module Windows lacks is imported at import level, except in the
satellite's own POSIX-only modules, which no module imports at import level.

Only a ``sys.platform`` test guards code at import level: mypy reads it,
and this test reads it the same way.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SATELLITE = ROOT / "satellite"
NAMES = Path(__file__).with_name("platform_names.json")
BASELINE = "linux"
SYSTEMS = ("win32", "darwin")
#: The standard library's modules that Windows lacks.
POSIX_MODULES = frozenset({
    "crypt", "curses", "fcntl", "grp", "nis", "posix", "pty", "pwd",
    "readline", "resource", "spwd", "syslog", "termios", "tty",
})
#: The satellite's own modules that run on Linux and macOS alone.
POSIX_ONLY = frozenset({"satellite/terminal/pty_relay.py"})
_ERROR = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+): error: (?P<msg>.*?)(?:  \[[a-z-]+\])?$")
_FROM_MODULE = re.compile(r'^Module "(?P<module>[\w.]+)" has no attribute "(?P<name>\w+)"')
_ATTRIBUTE = re.compile(r'^Module has no attribute "(?P<name>\w+)"')


@dataclass(frozen=True)
class Finding:
    system: str
    file: str
    line: int
    where: str | None  # the function's dotted name, None at import
    name: str


def _floor() -> str:
    text = (ROOT / "VERSIONS.md").read_text(encoding="utf-8")
    m = re.search(r"^PYTHON_MIN_VERSION=(\d+\.\d+)\s*$", text, re.M)
    assert m, "VERSIONS.md has no PYTHON_MIN_VERSION pin"
    return m.group(1)


def _sources(package: Path) -> list[str]:
    root = package.parent
    return sorted(
        p.relative_to(root).as_posix() for p in package.rglob("*.py")
        if not {"tests", "venv"} & set(p.relative_to(package).parts[:-1])
    )


def _mypy(root: Path, files: list[str], cache: Path, floor: str) -> dict[str, set[tuple[str, int, int, str]]]:
    """Each system's mypy errors as (file, line, column, message)."""
    try:
        import mypy  # noqa: F401
    except ImportError:
        pytest.fail("mypy is missing: pip install -r proxy/requirements-test.txt", pytrace=False)
    procs = {}
    for system in (BASELINE, *SYSTEMS):
        cmd = [
            sys.executable, "-m", "mypy", "--config-file", "", "--platform", system,
            "--python-version", floor, "--no-site-packages", "--ignore-missing-imports",
            "--follow-imports", "silent", "--explicit-package-bases", "--show-column-numbers",
            "--no-error-summary", "--no-pretty", "--hide-error-context", "--no-color-output",
            "--soft-error-limit", "-1", "--cache-dir", str(cache / system), *files,
        ]
        procs[system] = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
    errors: dict[str, set[tuple[str, int, int, str]]] = {}
    for system, proc in procs.items():
        out, err = proc.communicate(timeout=300)
        assert proc.returncode in (0, 1), f"mypy --platform {system} failed:\n{err}{out}"
        found = set()
        for line in out.splitlines():
            m = _ERROR.match(line)
            if m:
                msg = m["msg"].split('; maybe "')[0]
                found.add((m["file"], int(m["line"]), int(m["col"]), msg))
        errors[system] = found
    return errors


def _functions(tree: ast.Module) -> Iterator[tuple[str, ast.AST, tuple[int, int]]]:
    """Every function and lambda with its dotted name and where its body starts."""
    def walk(node: ast.AST, chain: tuple[str, ...]) -> Iterator[tuple[str, ast.AST, tuple[int, int]]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = (*chain, child.name)
                first = child.body[0]
                yield ".".join(name), child, (first.lineno, first.col_offset)
                yield from walk(child, name)
            elif isinstance(child, ast.Lambda):
                name = (*chain, "<lambda>")
                yield ".".join(name), child, (child.body.lineno, child.body.col_offset)
                yield from walk(child, name)
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, (*chain, child.name))
            else:
                yield from walk(child, chain)
    yield from walk(tree, ())


def _where(tree: ast.Module, line: int, col: int) -> str | None:
    """The innermost function whose body holds (line, col); None at import."""
    best = None
    for name, node, start in _functions(tree):
        end = (node.end_lineno or 0, node.end_col_offset or 0)
        if start <= (line, col) <= end and (best is None or start >= best[1]):
            best = (name, start)
    return best[0] if best else None


def _dotted(tree: ast.Module, line: int, col: int, msg: str) -> str:
    """``os.O_DIRECTORY`` for the attribute mypy names at (line, col)."""
    if m := _FROM_MODULE.match(msg):
        return f"{m['module']}.{m['name']}"
    if m := _ATTRIBUTE.match(msg):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == m["name"]
                    and (node.lineno, node.col_offset) == (line, col)):
                return f"{ast.unparse(node.value)}.{node.attr}"
        return m["name"]
    return msg


def platform_findings(root: Path, files: list[str], cache: Path, floor: str) -> list[Finding]:
    errors = _mypy(root, files, cache, floor)
    trees: dict[str, ast.Module] = {}
    findings = []
    for system in SYSTEMS:
        for file, line, col, msg in sorted(errors[system] - errors[BASELINE]):
            tree = trees.get(file) or trees.setdefault(file, ast.parse((root / file).read_text(encoding="utf-8")))
            findings.append(Finding(system, file, line, _where(tree, line, col - 1),
                                    _dotted(tree, line, col - 1, msg)))
    return findings


def _platform_test(test: ast.expr) -> bool | None:
    """What a ``sys.platform`` test says on Windows; None for any other test."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _platform_test(test.operand)
        return None if inner is None else not inner
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and ast.unparse(test.left) == "sys.platform"
            and isinstance(test.comparators[0], ast.Constant)):
        if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
                and ast.unparse(test.func) == "sys.platform.startswith"
                and len(test.args) == 1 and isinstance(test.args[0], ast.Constant)):
            return "win32".startswith(test.args[0].value)
        return None
    value = test.comparators[0].value
    if isinstance(test.ops[0], ast.Eq):
        return value == "win32"
    if isinstance(test.ops[0], ast.NotEq):
        return value != "win32"
    return None


def _catches_import_error(node: ast.Try) -> bool:
    names = set()
    for handler in node.handlers:
        if handler.type is None:
            return True
        for t in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]):
            names.add(ast.unparse(t))
    return bool(names & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def _import_level(body: list[ast.stmt]) -> Iterator[ast.Import | ast.ImportFrom]:
    """The imports a module runs when Windows imports it."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            says = _platform_test(node.test)
            if says is not False:
                yield from _import_level(node.body)
            if says is not True:
                yield from _import_level(node.orelse)
        elif isinstance(node, ast.Try):
            if not _catches_import_error(node):
                yield from _import_level(node.body)
            for handler in node.handlers:
                yield from _import_level(handler.body)
            yield from _import_level(node.orelse)
            yield from _import_level(node.finalbody)
        elif isinstance(node, (ast.ClassDef, ast.With, ast.For, ast.While)):
            yield from _import_level(node.body)


def _imported(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """The dotted modules an import names, relative ones resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = module.split(".")
    if node.level:
        base = base[:len(base) - node.level]
        stem = ".".join([*base, node.module] if node.module else base)
    else:
        stem = node.module or ""
    return [stem, *(f"{stem}.{alias.name}" for alias in node.names)]


def module_findings(root: Path, files: list[str], posix_only: frozenset[str]) -> list[str]:
    posix_dotted = {f.removesuffix(".py").replace("/", ".") for f in posix_only}
    found = []
    for file in files:
        if file in posix_only:
            continue
        module = file.removesuffix(".py").replace("/", ".")
        tree = ast.parse((root / file).read_text(encoding="utf-8"))
        for node in _import_level(tree.body):
            for name in _imported(node, module):
                if name.split(".")[0] in POSIX_MODULES or name in posix_dotted:
                    found.append(f"{file}:{node.lineno}: imports {name}")
    return found


def _listed(findings: list[Finding]) -> dict[str, dict[str, list[str]]]:
    listed: dict[str, dict[str, set[str]]] = {s: {} for s in SYSTEMS}
    for f in findings:
        if f.where is not None:
            listed[f.system].setdefault(f"{f.file}::{f.where}", set()).add(f.name)
    return {s: {k: sorted(v) for k, v in sorted(by.items())} for s, by in listed.items()}


@pytest.fixture(scope="module")
def satellite_findings(request) -> list[Finding]:
    store = getattr(request.config, "cache", None)
    cache = store.mkdir("platform-mypy") if store else request.getfixturevalue(
        "tmp_path_factory").mktemp("platform-mypy")
    return platform_findings(ROOT, _sources(SATELLITE), cache, _floor())


def test_no_name_a_system_lacks_is_read_at_import(satellite_findings):
    at_import = [f for f in satellite_findings if f.where is None]
    assert not at_import, (
        "Read when the module is imported, on a system that lacks the name, so the program "
        "dies before it signs in there. Read it inside a function, or guard it with sys.platform:\n"
        + "\n".join(f"  {f.file}:{f.line}: {f.name} ({f.system})" for f in at_import))


def test_the_names_read_inside_functions_are_the_listed_ones(satellite_findings):
    listed = json.loads(NAMES.read_text(encoding="utf-8"))
    found = _listed(satellite_findings)
    new, gone = [], []
    for system in SYSTEMS:
        have, want = found[system], listed.get(system, {})
        for key in sorted(set(have) | set(want)):
            new += [f"{system} {key}: {n}" for n in sorted(set(have.get(key, [])) - set(want.get(key, [])))]
            gone += [f"{system} {key}: {n}" for n in sorted(set(want.get(key, [])) - set(have.get(key, [])))]
    assert not new, (
        "A function reads a name this system lacks. Check that the satellite's own guard for the "
        f"system stands in front of it, then list it in {NAMES.name}:\n  " + "\n  ".join(new))
    assert not gone, f"Listed in {NAMES.name} but no longer read: remove it:\n  " + "\n  ".join(gone)


def test_no_module_windows_lacks_is_imported_at_import():
    found = module_findings(ROOT, _sources(SATELLITE), POSIX_ONLY)
    assert not found, (
        "Imported when the module is imported, and Windows lacks it. Import it inside the "
        "function that runs off Windows alone:\n  " + "\n  ".join(found))


def test_the_check_sees_the_fault_that_broke_windows(tmp_path):
    """The machinery itself, on the shape of the fault of 0.5.181: a class
    body that reads a flag Windows lacks, a name read inside a function, a
    module Windows lacks at import, and the guards that make each one fine."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "posix_only.py").write_text("import termios\n")
    (pkg / "mod.py").write_text(
        "import os\nimport signal\nimport sys\n"
        "from . import posix_only\n"
        "if sys.platform != 'win32':\n    import fcntl\n    FLAGS = os.O_NOFOLLOW\n"
        "try:\n    import pwd\nexcept ImportError:\n    pwd = None\n"
        "import grp\n"
        "class Ops:\n"
        "    _DIR = os.O_RDONLY | os.O_DIRECTORY\n"
        "    def kill(self, pid: int) -> None:\n"
        "        os.kill(pid, signal.SIGKILL)\n"
        "def later() -> None:\n"
        "    import pty\n"
        "    from . import posix_only\n"
    )
    files = _sources(pkg)
    findings = platform_findings(tmp_path, files, tmp_path / "cache", _floor())
    win = {(f.file, f.line, f.where, f.name) for f in findings if f.system == "win32"}
    assert win == {
        ("pkg/mod.py", 14, None, "os.O_DIRECTORY"),
        ("pkg/mod.py", 16, "Ops.kill", "signal.SIGKILL"),
    }
    assert module_findings(tmp_path, files, frozenset({"pkg/posix_only.py"})) == [
        "pkg/mod.py:4: imports pkg.posix_only", "pkg/mod.py:12: imports grp",
    ]
