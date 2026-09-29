"""The session-kind surface — the acceptance test of core-seams phase 4, in
the shape of ``tests/execution/test_engine_id_surface.py``.

One authority: ``core/session/session_kind.py`` (the kinds, the id shape,
the read-side resolver) with the owner sentinels in
``core/session/visibility.py``; the dashboard mirrors them in
``lib/session/kind.ts`` and ``lib/visibility.ts``. Generic code asks a
question; a spelling compared anywhere else is listed here WITH its
reason, and this test keeps the list exact in both directions.

Four passes over every tracked ``.py`` under ``proxy/``, ``satellite/`` and
``mcps/custom/`` (tests, docs, venvs, vendored copies and skills excluded)
and every ``.ts`` / ``.tsx`` under ``dashboard/src`` (tests excluded):

1. the id and owner shapes — ``task-``, ``task-run-``, ``task::``,
   ``agent::``, ``meeting-`` — as a ``startswith`` / ``endswith`` /
   ``removeprefix`` / ``removesuffix`` argument (tuple members included)
   or a ``Compare`` operand, outside the authority: the known sites;
2. a ``Compare`` between something spelled ``client_type`` /
   ``source_type`` (a name, an attribute, a subscript, a ``.get(...)``)
   and a string constant, outside the authority: none — the code asks
   ``session_kind.of`` / ``of_chat`` / ``attended`` / ``judged`` or
   compares the authority's constants;
3. the mint — an f-string starting with ``task-`` — in the authority only
   (the dynamic-task DEFINITION ids that share the word are listed);
4. the dashboard: the five shapes quoted only in the two mirror files, and
   no ``source_type`` / ``sourceType`` compared with a quoted kind outside
   ``lib/session/``.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from tests._paths import REPO_ROOT

_SCAN_ROOTS = ("proxy", "satellite", "mcps/custom")
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "_vendored", "skills", "node_modules", "__pycache__"}

AUTHORITY = (
    "proxy/core/session/session_kind.py",
    "proxy/core/session/visibility.py",
)

SHAPES = ("task-", "task-run-", "task::", "agent::", "meeting-")
_PREFIX_METHODS = ("startswith", "endswith", "removeprefix", "removesuffix")
_FIELDS = ("client_type", "source_type")

# {path: (reason, count of shape operands)} — sites outside the authority
# that compare a shape, each with the reason it stays.
KNOWN_SHAPE_SITES: dict[str, tuple[str, int]] = {}

# {path: (reason, count)} — f-strings that START with ``task-`` outside the
# authority: the dynamic-task DEFINITION ids (``dynamic_tasks.id``), a
# different key space that shares the word and never becomes a chat id.
_DEFINITION_ID = "a dynamic-task DEFINITION id (dynamic_tasks.id), not a chat id"
KNOWN_MINT_SITES: dict[str, tuple[str, int]] = {
    "proxy/services/apps/app_blueprints.py": (_DEFINITION_ID, 1),
    "proxy/services/community/community_agent_installer.py": (_DEFINITION_ID, 2),
}


def _tracked_files(suffixes: tuple[str, ...], roots: tuple[str, ...]) -> list[str]:
    """The tracked files plus the untracked ones git does not ignore — a
    module written this turn is seen before it is committed."""
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", *roots],
                             capture_output=True, check=True)
        rel = [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        rel = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for root in roots for s in suffixes for p in (REPO_ROOT / root).rglob(f"*{s}")]
    files = [p for p in rel if p.endswith(suffixes) and not (set(Path(p).parts) & _EXCLUDED_PARTS)]
    assert len(files) > 50, "the scan found almost nothing — a checkout problem, not a clean tree"
    return files


def _is_shape(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in SHAPES


def _shape_sites(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                elts = op.elts if isinstance(op, (ast.Tuple, ast.List, ast.Set)) else [op]
                n += sum(1 for e in elts if _is_shape(e))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _PREFIX_METHODS:
            for a in node.args:
                elts = a.elts if isinstance(a, (ast.Tuple, ast.List)) else [a]
                n += sum(1 for e in elts if _is_shape(e))
    return n


def _spells_field(node: ast.AST) -> bool:
    """A name / attribute / subscript / ``.get(...)`` spelled after one of
    the two fields."""
    if isinstance(node, ast.Name):
        return node.id in _FIELDS
    if isinstance(node, ast.Attribute):
        return node.attr in _FIELDS
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in _FIELDS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value in _FIELDS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
        return (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _FIELDS)
    if isinstance(node, ast.BoolOp):  # ``chat.get("source_type") or "chat"``
        return any(_spells_field(v) for v in node.values)
    return False


def _is_str_operand(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)
    return False


def _field_compares(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if any(_spells_field(o) for o in operands) and any(_is_str_operand(o) for o in operands):
            lines.append(node.lineno)
    return lines


def _mints(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and node.values \
                and isinstance(node.values[0], ast.Constant) \
                and str(node.values[0].value).startswith("task-"):
            lines.append(node.lineno)
    return lines


def _scan() -> tuple[dict[str, int], dict[str, list[int]], dict[str, list[int]]]:
    shapes: dict[str, int] = {}
    compares: dict[str, list[int]] = {}
    mints: dict[str, list[int]] = {}
    for rel in _tracked_files((".py",), _SCAN_ROOTS):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8", errors="surrogateescape"))
        if rel in AUTHORITY:
            continue
        if n := _shape_sites(tree):
            shapes[rel] = n
        if hits := _field_compares(tree):
            compares[rel] = hits
        if hits := _mints(tree):
            mints[rel] = hits
    return shapes, compares, mints


_HINT = ("ask core/session/session_kind (is_task_chat_id / run_id_of_chat / of_chat / attended / "
         "judged, or compare its constants) or core/session/visibility (the owner predicates); "
         "if the site must stay, add it here with its reason")


def test_every_shape_compare_outside_the_authority_is_a_known_site():
    found, _, _ = _scan()
    expected = {rel: count for rel, (_reason, count) in KNOWN_SHAPE_SITES.items()}
    new = {rel: n for rel, n in found.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in found)
    assert not new, f"new shape compare(s) {new} — {_HINT}"
    assert not gone, f"{gone} — retired: drop it from this test"
    drift = {rel: (found[rel], expected[rel]) for rel in expected if found[rel] != expected[rel]}
    assert not drift, f"the shape sites in a known file changed (found, expected): {drift} — {_HINT}"


def test_no_client_type_or_source_type_is_compared_with_a_literal():
    _, found, _ = _scan()
    assert not found, f"a client_type / source_type compared with a string literal: {found} — {_HINT}"


def test_the_task_chat_id_is_minted_in_the_authority_only():
    _, _, found = _scan()
    counts = {rel: len(lines) for rel, lines in found.items()}
    expected = {rel: n for rel, (_reason, n) in KNOWN_MINT_SITES.items()}
    assert counts == expected, (found, "a task- f-string outside session_kind.task_chat_id — "
                                "the scheduler mints a chat id there and nowhere else; a "
                                "definition id is listed here with its reason")


_TS_SHAPES = re.compile(r"""['"](?:task-|task-run-|task::|agent::|meeting-)['"]""")
_TS_KIND_COMPARE = re.compile(
    r"""(?:source_type|sourceType)\s*[!=]==\s*['"](?:task|phone|chat)['"]"""
    r"""|['"](?:task|phone|chat)['"]\s*[!=]==\s*(?:\w+\.)?(?:source_type|sourceType)\b""")
_TS_MIRROR = ("dashboard/src/lib/session/kind.ts", "dashboard/src/lib/visibility.ts")


def test_the_dashboard_spells_the_shapes_and_the_kinds_in_the_mirror_only():
    src = REPO_ROOT / "dashboard" / "src"
    shapes: dict[str, int] = {}
    compares: dict[str, int] = {}
    for p in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel:
            continue
        text = p.read_text(encoding="utf-8", errors="surrogateescape")
        if n := len(_TS_SHAPES.findall(text)):
            shapes[rel] = n
        if rel not in _TS_MIRROR and (n := len(_TS_KIND_COMPARE.findall(text))):
            compares[rel] = n
    assert set(shapes) <= set(_TS_MIRROR), (shapes, "ask lib/session/kind.ts (isTaskChatId / runIdOfChat / "
                                                    "taskChatId / chatKind) or lib/visibility.ts (isSharedChatOwner)")
    assert "dashboard/src/lib/session/kind.ts" in shapes
    assert not compares, (compares, "compare SOURCE_TYPE.* from lib/session/kind.ts, never a quoted kind")
