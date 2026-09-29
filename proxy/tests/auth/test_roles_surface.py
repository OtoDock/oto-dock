"""The role surface — the acceptance test of core-seams phase 5, in the
shape of ``tests/execution/test_engine_id_surface.py``.

One authority: ``auth/roles.py`` (the members, the tiers, the ranks, the
resolver); the dashboard mirrors it in ``lib/permissions.ts``. Generic
code asks a question or compares a constant; a spelling compared anywhere
else is listed here WITH its reason, and this test keeps the list exact in
both directions.

Three passes over every tracked ``.py`` under ``proxy/``, ``satellite/``
and ``mcps/custom/`` (tests, docs, venvs, vendored copies and skills
excluded) and every ``.ts`` / ``.tsx`` under ``dashboard/src`` (tests
excluded):

1. a role member — ``admin``, ``creator``, ``member``, ``manager``,
   ``editor``, ``viewer`` — as a ``Compare`` operand (tuple members
   included) or a ``startswith`` / ``endswith`` argument, outside the
   authority: the known sites, each another vocabulary that shares a word;
2. a per-agent map read with a role word as its default —
   ``agent_roles.get(agent, "viewer")`` and the store's
   ``get_user_agent_roles(sub).get(agent, "…")`` — outside the principal
   module: none — the code asks ``effective_role`` / ``acting_role``;
3. the dashboard: the six words as an equality, ``case`` or
   array-``includes`` operand only in the mirror, and no role union typed
   outside it (the API types import ``PlatformRole`` / ``AgentRole``).
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from tests._paths import REPO_ROOT

_SCAN_ROOTS = ("proxy", "satellite", "mcps/custom")
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "_vendored", "skills", "node_modules", "__pycache__"}

AUTHORITY = ("proxy/auth/roles.py",)
PRINCIPALS = ("proxy/auth/providers/__init__.py",)

MEMBERS = frozenset({"admin", "creator", "member", "manager", "editor", "contributor", "viewer"})
_PREFIX_METHODS = ("startswith", "endswith")
_MAP_READS = ("agent_roles", "get_user_agent_roles")

# {path: (reason, count of member operands)} — sites outside the authority
# that compare one of the six words, each ANOTHER vocabulary sharing it.
KNOWN_SITES: dict[str, tuple[str, int]] = {
    "proxy/services/notifications/notification_manager.py": (
        "the notification scope (user | agent | global | admin), not a role", 1),
    "proxy/api/apps/app_proxy.py": (
        "the app token basis (viewer | external | agent | app | step), not a role", 2),
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


def _is_member(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in MEMBERS


def _member_sites(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                elts = op.elts if isinstance(op, (ast.Tuple, ast.List, ast.Set)) else [op]
                n += sum(1 for e in elts if _is_member(e))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _PREFIX_METHODS:
            for a in node.args:
                elts = a.elts if isinstance(a, (ast.Tuple, ast.List)) else [a]
                n += sum(1 for e in elts if _is_member(e))
    return n


def _spells_map(node: ast.AST) -> bool:
    """``agent_roles`` / ``x.agent_roles`` / ``get_user_agent_roles(...)`` —
    the per-agent map, whatever holds it."""
    if isinstance(node, ast.Name):
        return node.id in _MAP_READS
    if isinstance(node, ast.Attribute):
        return node.attr in _MAP_READS
    if isinstance(node, ast.Call):
        return _spells_map(node.func)
    if isinstance(node, ast.BoolOp):   # ``(roles or {}).get(...)``
        return any(_spells_map(v) for v in node.values)
    return False


def _map_reads_with_a_role_default(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and len(node.args) == 2 and _is_member(node.args[1]) and _spells_map(node.func.value):
            lines.append(node.lineno)
    return lines


def _scan() -> tuple[dict[str, int], dict[str, list[int]]]:
    members: dict[str, int] = {}
    defaults: dict[str, list[int]] = {}
    for rel in _tracked_files((".py",), _SCAN_ROOTS):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8", errors="surrogateescape"))
        if rel not in AUTHORITY and (n := _member_sites(tree)):
            members[rel] = n
        if rel not in AUTHORITY + PRINCIPALS and (hits := _map_reads_with_a_role_default(tree)):
            defaults[rel] = hits
    return members, defaults


_HINT = ("ask auth/roles (is_admin / is_creator_or_above / can_manage / can_edit / meets_floor / "
         "may_mutate_shared, or compare its constants), auth/providers (effective_role_of / acting_role_of, "
         "UserContext.effective_role / acting_role); if the site is another vocabulary, list it here with its reason")


def test_every_role_compare_outside_the_authority_is_a_known_site():
    found, _ = _scan()
    expected = {rel: count for rel, (_reason, count) in KNOWN_SITES.items()}
    new = {rel: n for rel, n in found.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in found)
    assert not new, f"new role compare(s) {new} — {_HINT}"
    assert not gone, f"{gone} — retired: drop it from this test"
    drift = {rel: (found[rel], expected[rel]) for rel in expected if found[rel] != expected[rel]}
    assert not drift, f"the role sites in a known file changed (found, expected): {drift} — {_HINT}"


def test_no_per_agent_map_is_read_with_a_role_default_outside_the_principal_module():
    _, found = _scan()
    assert not found, f"a per-agent map read with a role word as its default: {found} — {_HINT}"


_TS_WORDS = "admin|creator|member|manager|editor|contributor|viewer"
_TS_COMPARE = re.compile(
    rf"""[!=]==\s*['"](?:{_TS_WORDS})['"]"""
    rf"""|['"](?:{_TS_WORDS})['"]\s*[!=]==""")
_TS_CASE = re.compile(rf"""case\s+['"](?:{_TS_WORDS})['"]\s*:""")
_TS_INCLUDES = re.compile(rf"""\[[^\]]*['"](?:{_TS_WORDS})['"][^\]]*\]\s*\.includes\(""")
_TS_UNION = re.compile(r"""['"](?:manager|editor|contributor|viewer)['"]\s*\|\s*['"](?:manager|editor|contributor|viewer)['"]"""
                       r"""|['"](?:admin|creator|member)['"]\s*\|\s*['"](?:admin|creator|member)['"]""")
_TS_MIRROR = "dashboard/src/lib/permissions.ts"

# {path: (reason, count)} — dashboard files that keep a quoted word, each
# another vocabulary sharing it.
KNOWN_TS_SITES: dict[str, tuple[str, int]] = {
    "dashboard/src/api/remoteMachines.ts": ("MachineScope admin | me: the admin route or the owner's route", 2),
    "dashboard/src/pages/agent/AgentMcps.tsx": ("authorized_by admin: where an MCP's availability came from", 1),
}


def test_the_dashboard_compares_the_mirror_and_types_through_it():
    src = REPO_ROOT / "dashboard" / "src"
    compares: dict[str, int] = {}
    unions: dict[str, int] = {}
    for p in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or rel == _TS_MIRROR:
            continue
        text = p.read_text(encoding="utf-8", errors="surrogateescape")
        n = len(_TS_COMPARE.findall(text)) + len(_TS_CASE.findall(text)) + len(_TS_INCLUDES.findall(text))
        if n:
            compares[rel] = n
        if m := len(_TS_UNION.findall(text)):
            unions[rel] = m
    expected = {rel: count for rel, (_reason, count) in KNOWN_TS_SITES.items()}
    assert compares == expected, (compares, "compare ROLE.* from lib/permissions.ts or ask isAdmin / "
                                  "isCreatorOrAbove / actingRole / canManageAgent / canEditAgent; another "
                                  "vocabulary sharing the word is listed here with its reason")
    assert not unions, (unions, "type a role as PlatformRole / AgentRole / EffectiveRole from lib/permissions.ts")
