"""The placement surface — the acceptance test of core-seams phase 7, in the
shape of ``tests/execution/test_engine_id_surface.py``.

One authority: ``core/placement.py`` (the stored target value, the resolved
kind, the pairing scope, the check/app site, ``PlacementCapabilities`` and
its questions); the dashboard mirrors it in ``lib/placement.ts``. Generic
code asks the object or the leaf's predicates; a spelling compared anywhere
else is listed here WITH its reason, and this test keeps the list exact in
both directions.

Five passes over every tracked ``.py`` under ``proxy/``, ``satellite/`` and
``mcps/custom/`` (tests, docs, venvs, vendored copies and skills excluded)
and every ``.ts`` / ``.tsx`` under ``dashboard/src`` (tests excluded):

1. ``"local"`` as a ``Compare`` operand (tuple members included) or a
   ``startswith`` / ``endswith`` argument outside the authority: the known
   sites, each another vocabulary that shares the word (the auth provider,
   the browser MCP's server key);
2. the offline sentinel's prefix spelled outside the leaf: none;
3. ``"admin_remote"`` / ``"user_remote"`` / ``"machine"`` compared outside
   the leaf: none (a check document and an app step target say ``site``);
4. the disassembled descriptor's old names — the nine unambiguous
   ``target_*`` fields in ANY form (an attribute, a keyword, a ``getattr``
   or ``.get`` string) and ``target_kind`` on a context-named receiver or as
   a ``getattr`` string — outside the security index's codec: none (the
   sharing routes' and the webhook store's ``target_kind`` — app | chat,
   organization — is a homonym this pass never scores);
5. the leaf is used module-qualified: ``from core.placement import`` names
   the class and the local placement only, never a string or a predicate;
   the dashboard compares ``'local'`` / ``'machine'`` only in the mirror
   (the auth-provider pages' ``startsWith('local')`` allowed by reason).
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from tests._paths import REPO_ROOT

_SCAN_ROOTS = ("proxy", "satellite", "mcps/custom")
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "_vendored", "skills", "node_modules", "__pycache__"}

AUTHORITY = ("proxy/core/placement.py",)
CODEC = ("proxy/core/session/session_state.py",)

_PREFIX_METHODS = ("startswith", "endswith")
_OLD_FIELDS = frozenset({
    "target_label", "target_agents_dir", "target_machine_id", "target_home_dir",
    "target_allow_full_fs", "target_claude_runtime_root", "target_os_user",
    "target_user_dirs", "target_device_grants",
})
_CTX_NAMES = frozenset({"ctx", "sec", "security_ctx", "security_context", "policy_ctx", "_ctx", "sc"})
_LEAF_NAMES_BY_NAME = frozenset({"PlacementCapabilities", "LOCAL_PLACEMENT"})

# {path: (reason, count of "local" compare operands)} — sites outside the
# authority that compare the word, each ANOTHER vocabulary sharing it.
KNOWN_SITES: dict[str, tuple[str, int]] = {
    "proxy/api/auth/identity.py": ("the auth provider (local | oidc:<name>), not a placement", 3),
    "proxy/api/auth/admin_users.py": ("the auth provider, not a placement", 2),
    "proxy/auth/providers/local_provider.py": ("the auth provider, not a placement", 1),
    "proxy/services/mcp/mcp_registry.py": ("the browser MCP's server key `local` on the servers map, not a placement", 2),
    "proxy/services/mcp/compose_rewrite.py": ("the Docker volume driver word local, not a placement", 1),
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
    files = [p for p in rel if p.endswith(suffixes) and not (set(Path(p).parts) & _EXCLUDED_PARTS)
             and (REPO_ROOT / p).is_file()]
    assert len(files) > 50, "the scan found almost nothing — a checkout problem, not a clean tree"
    return files


def _is_str(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _compare_operands(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                elts = op.elts if isinstance(op, (ast.Tuple, ast.List, ast.Set)) else [op]
                yield from elts
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _PREFIX_METHODS:
            for a in node.args:
                elts = a.elts if isinstance(a, (ast.Tuple, ast.List)) else [a]
                yield from elts


def _local_sites(tree: ast.AST) -> int:
    return sum(1 for e in _compare_operands(tree) if _is_str(e, "local"))


def _kind_or_site_sites(tree: ast.AST) -> list[str]:
    return [e.value for e in _compare_operands(tree)
            if isinstance(e, ast.Constant) and e.value in ("admin_remote", "user_remote", "machine")]


def _sentinel_spellings(tree: ast.AST) -> int:
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("__offline__"))


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _old_field_reads(tree: ast.AST) -> list[str]:
    """The nine names in any form; ``target_kind`` only on a context-named
    receiver or as a ``getattr`` string."""
    hits: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            if n.attr in _OLD_FIELDS:
                hits.append(f"{n.lineno}: .{n.attr}")
            elif n.attr == "target_kind" and _receiver_name(n.value) in _CTX_NAMES:
                hits.append(f"{n.lineno}: {_receiver_name(n.value)}.target_kind")
        elif isinstance(n, ast.keyword) and n.arg in _OLD_FIELDS:
            hits.append(f"{n.lineno}: {n.arg}=")
        elif isinstance(n, ast.Call) and n.args:
            fn = n.func
            is_getattr = isinstance(fn, ast.Name) and fn.id in ("getattr", "hasattr", "setattr")
            is_get = isinstance(fn, ast.Attribute) and fn.attr == "get"
            key = n.args[1] if is_getattr and len(n.args) > 1 else (n.args[0] if is_get else None)
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in _OLD_FIELDS or (is_getattr and key.value == "target_kind"):
                    hits.append(f"{n.lineno}: {'getattr' if is_getattr else '.get'}(\"{key.value}\")")
    return hits


def _named_leaf_imports(tree: ast.AST) -> list[str]:
    return [a.name for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "core.placement"
            for a in n.names if a.name not in _LEAF_NAMES_BY_NAME]


def _scan():
    local: dict[str, int] = {}
    kinds: dict[str, list[str]] = {}
    sentinels: dict[str, int] = {}
    fields: dict[str, list[str]] = {}
    imports: dict[str, list[str]] = {}
    for rel in _tracked_files((".py",), _SCAN_ROOTS):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8", errors="surrogateescape"))
        if rel not in AUTHORITY:
            if n := _local_sites(tree):
                local[rel] = n
            if k := _kind_or_site_sites(tree):
                kinds[rel] = k
            if s := _sentinel_spellings(tree):
                sentinels[rel] = s
            if names := _named_leaf_imports(tree):
                imports[rel] = names
        if rel not in AUTHORITY + CODEC:
            if f := _old_field_reads(tree):
                fields[rel] = f
    return local, kinds, sentinels, fields, imports


_HINT = ("ask core/placement — is_local / machine_of / is_offline_sentinel / runs_on on a stored value, "
         "the PlacementCapabilities questions (is_remote / admin_paired / site / os_family …) on a "
         "context, or compare its constants; if the site is another vocabulary, list it here with its reason")


def test_every_local_compare_outside_the_authority_is_a_known_site():
    found, *_ = _scan()
    expected = {rel: count for rel, (_reason, count) in KNOWN_SITES.items()}
    new = {rel: n for rel, n in found.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in found)
    assert not new, f"new local compare(s) {new} — {_HINT}"
    assert not gone, f"{gone} — retired: drop it from this test"
    drift = {rel: (found[rel], expected[rel]) for rel in expected if found[rel] != expected[rel]}
    assert not drift, f"the local sites in a known file changed (found, expected): {drift} — {_HINT}"


def test_the_sentinel_and_the_kinds_are_spelled_in_the_leaf_only():
    _, kinds, sentinels, _, _ = _scan()
    assert not sentinels, f"the offline sentinel's prefix spelled outside core/placement.py: {sentinels} — {_HINT}"
    assert not kinds, f"a placement kind or site compared outside core/placement.py: {kinds} — {_HINT}"


def test_the_disassembled_fields_are_gone():
    *_, fields, _ = _scan()
    assert not fields, f"the old target_* names read outside the codec: {fields} — {_HINT}"


def test_the_leaf_is_used_module_qualified():
    *_, imports = _scan()
    assert not imports, (imports, "use `from core import placement` and spell placement.X; a name import "
                                  "is for the class and LOCAL_PLACEMENT only")


_TS_COMPARE = re.compile(r"""[!=]==\s*['"](?:local|machine)['"]|['"](?:local|machine)['"]\s*[!=]==""")
_TS_CASE = re.compile(r"""case\s+['"](?:local|machine)['"]\s*:""")
_TS_INCLUDES = re.compile(r"""\[[^\]]*['"](?:local|machine)['"][^\]]*\]\s*\.includes\(""")
_TS_STARTS = re.compile(r"""startsWith\(['"]local['"]\)""")
_TS_MIRROR = "dashboard/src/lib/placement.ts"

# {path: (reason, count)} — dashboard files that keep a quoted word, each
# another vocabulary sharing it.
KNOWN_TS_SITES: dict[str, tuple[str, int]] = {
    "dashboard/src/pages/UserSettings.tsx": ("the auth provider (startsWith('local')), not a placement", 1),
    "dashboard/src/pages/UserSettings.general.tsx": ("the auth provider, not a placement", 1),
    "dashboard/src/pages/admin/UsersPage.tsx": ("the auth provider, not a placement", 2),
}


def test_the_dashboard_compares_the_mirror():
    src = REPO_ROOT / "dashboard" / "src"
    compares: dict[str, int] = {}
    for p in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or rel == _TS_MIRROR:
            continue
        text = p.read_text(encoding="utf-8", errors="surrogateescape")
        n = (len(_TS_COMPARE.findall(text)) + len(_TS_CASE.findall(text))
             + len(_TS_INCLUDES.findall(text)) + len(_TS_STARTS.findall(text)))
        if n:
            compares[rel] = n
    expected = {rel: count for rel, (_reason, count) in KNOWN_TS_SITES.items()}
    assert compares == expected, (compares, "compare TARGET_LOCAL / SITE.* or ask isLocalTarget / machineOf "
                                  "from lib/placement.ts; another vocabulary sharing the word is listed "
                                  "here with its reason")
