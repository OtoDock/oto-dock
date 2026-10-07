"""The path-guard surface — the acceptance test of core-seams phase 3, in
the shape of ``tests/execution/test_engine_id_surface.py``.

One helper per guard: ``services/infra/path_confinement.py`` (``resolve_under``,
``join_under``, ``normalize_rel_path``, ``safe_agent_dir``) for filesystem
paths, ``auth/request_path.py`` (``has_traversal``) for request paths,
``services/infra/outbound_url.py`` for destinations the platform fetches;
the satellite's ``host/auth_paths.py`` and its tunnel twin. A guard written
anywhere else is listed here WITH its reason, and this test keeps the list
exact in both directions — a new site fails ("route it through the helper,
or add it here with its reason"), a retired site fails too ("drop it").

Four passes over every tracked ``.py`` under ``proxy/``, ``satellite/`` and
``mcps/custom/`` (tests, docs, venvs, vendored copies and skills excluded):

1. ``".."`` as a BRANCH OPERAND — a ``Compare`` operand or a member of its
   tuple, a ``startswith`` / ``endswith`` argument, a ``match`` case —
   counted per file; a site outside the authority names its reason;
2. a ``startswith`` / ``endswith`` whose argument is built from a
   ``str(...)`` call — the resolve-then-string-prefix containment the
   helpers own (the separator-less form was the phase's defect): none;
3. no ``_has_traversal`` / ``has_traversal`` definition outside the
   authority and the satellite twin;
4. the dashboard's ``'..'`` comparisons are the two named sites (a rename
   NAME validator, the open-target parser) — the dashboard's URL authority
   is ``lib/safeUrl.ts`` and is not this test's.
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
    "proxy/services/infra/path_confinement.py",
    "proxy/services/infra/safe_fs.py",       # the descriptor-based twin: refuses a dot segment itself
    "mcps/custom/file-tools-mcp/safe_fs.py",  # its byte copy in the file-tools image (no proxy import there)
    "satellite/host/safe_fs.py",             # the satellite's twin for the applier, the pull and the stat
    "proxy/auth/request_path.py",
    "satellite/host/auth_paths.py",
    "satellite/transport/http_tunnel.py",   # the tunnel's byte twin of has_traversal
)

_MCP = ("an MCP process cannot import the helper: the interceptor gates its declared path "
        "arguments on a satellite and bwrap's mount set locally; the resolver re-anchors an "
        "escape under the workspace as a courtesy (MCP-FRAMEWORK.md)")

# {path: (reason, count of ".." branch operands)}
KNOWN_DOTDOT_SITES: dict[str, tuple[str, int]] = {
    "proxy/api/agents/files.py": ("a rename NAME: no separator and not a dot name", 1),
    "proxy/api/apps/app_proxy.py": ("the app's Location: header — a response, not a request path", 1),
    "proxy/api/sessions/sessions.py": ("a plan FILENAME: one segment, no dot name", 1),
    "proxy/auth/path_policy.py": ("the candidate-order heuristic of a relative tool path, never a verdict", 1),
    "proxy/config.py": ("the agent-name rule; safe_agent_dir is its barrier form", 1),
    "proxy/core/layers/direct/files.py": ("a glob PATTERN; every hit is re-confined with resolve() + is_relative_to", 1),
    "proxy/core/remote/file_sync.py": ("sync-rule NAMES (a twin of the satellite's, the gate pins it)", 1),
    "proxy/core/sandbox/session_config_dir.py": ("an ssh key NAME must be a bare basename", 1),
    "proxy/services/mcp/mcp_manifest_parse.py": ("a JSONPath expression's recursive descent, not a path", 1),
    "proxy/services/mcp/mcp_output_relocation.py": ("a basename scraped from a tool's output text", 1),
    "proxy/services/path_policy_v2.py": ("the UX reject of a relative '..' tool path (frozen)", 1),
    "proxy/services/webhooks/vendor_http.py": ("a vendor URL's path value; it never names a file", 1),
    "satellite/sessions/session_manager.py": ("the satellite-host ABSOLUTE path rule, and the credentials_update slug (a NAME)", 2),
    "satellite/transport/file_sync.py": ("sync-rule NAMES (the twin)", 1),
    "mcps/custom/delegation-mcp/server.py": ("an output_dir validator in the MCP; the proxy re-validates with the helper", 1),
    "mcps/custom/image-gen-mcp/server.py": (_MCP, 1),
    "mcps/custom/image-search-mcp/server.py": (_MCP, 5),
    "mcps/custom/music-gen-mcp/server.py": (_MCP, 1),
    "mcps/custom/tts-mcp/server.py": (_MCP, 1),
    "mcps/custom/video-gen-mcp/server.py": (_MCP, 1),
}

_TRAVERSAL_DEFS = ("_has_traversal", "has_traversal")


def _tracked_py_files() -> list[str]:
    """The tracked files plus the untracked ones git does not ignore — a
    module written this turn is seen before it is committed."""
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", *_SCAN_ROOTS],
                             capture_output=True, check=True)
        rel = [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        rel = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for root in _SCAN_ROOTS for p in (REPO_ROOT / root).rglob("*.py")]
    files = [p for p in rel if p.endswith(".py") and not (set(Path(p).parts) & _EXCLUDED_PARTS)]
    assert len(files) > 100, "the scan found almost nothing — a checkout problem, not a clean tree"
    return files


def _is_dotdot(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == ".."


def _dotdot_sites(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                elts = op.elts if isinstance(op, (ast.Tuple, ast.List, ast.Set)) else [op]
                n += sum(1 for e in elts if _is_dotdot(e))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("startswith", "endswith"):
            for a in node.args:
                elts = a.elts if isinstance(a, (ast.Tuple, ast.List)) else [a]
                n += sum(1 for e in elts if _is_dotdot(e))
        elif isinstance(node, ast.match_case):
            n += sum(1 for c in ast.walk(node.pattern) if _is_dotdot(c))
    return n


def _str_prefix_containments(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("startswith", "endswith") and node.args:
            if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "str"
                   for c in ast.walk(node.args[0])):
                lines.append(node.lineno)
    return lines


def _scan() -> tuple[dict[str, int], dict[str, list[int]], dict[str, list[str]]]:
    dotdot: dict[str, int] = {}
    prefix: dict[str, list[int]] = {}
    defs: dict[str, list[str]] = {}
    for rel in _tracked_py_files():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8", errors="surrogateescape"))
        if rel not in AUTHORITY:
            if n := _dotdot_sites(tree):
                dotdot[rel] = n
            if hits := _str_prefix_containments(tree):
                prefix[rel] = hits
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in _TRAVERSAL_DEFS]
        if names:
            defs[rel] = names
    return dotdot, prefix, defs


_HINT_NEW = ("route the value through path_confinement (normalize_rel_path / resolve_under / "
             "join_under) or auth.request_path.has_traversal; if the site must stay, add it "
             "here with its reason")


def test_every_dotdot_branch_outside_the_helpers_is_a_known_site():
    found, _, _ = _scan()
    expected = {rel: count for rel, (_reason, count) in KNOWN_DOTDOT_SITES.items()}
    new = {rel: n for rel, n in found.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in found)
    assert not new, f"new '..' guard site(s) {new} — {_HINT_NEW}"
    assert not gone, f"{gone} — retired: drop it from this test"
    drift = {rel: (found[rel], expected[rel]) for rel in expected if found[rel] != expected[rel]}
    assert not drift, f"the '..' sites in a known file changed (found, expected): {drift} — {_HINT_NEW}"


def test_no_resolve_then_string_prefix_containment_survives():
    _, found, _ = _scan()
    assert not found, (f"a str(...)-prefix containment: {found} — resolve_under / join_under "
                       "(the proxy) or auth_paths.is_path_under_root (the satellite) own it")


def test_the_traversal_guard_is_defined_in_the_authority_and_its_twin_only():
    _, _, defs = _scan()
    assert defs == {
        "proxy/auth/request_path.py": ["has_traversal"],
        "satellite/transport/http_tunnel.py": ["_has_traversal"],
    }, defs


_TS_DOTDOT = re.compile(
    r"""(?:[=!]==\s*['"]\.\.['"]|['"]\.\.['"]\s*[=!]==|case\s+['"]\.\.['"]\s*:"""
    r"""|\.(?:includes|has)\(\s*['"]\.\.['"]\s*\))""")
_TS_ALLOWED = {
    "dashboard/src/components/workspace/InlineRename.tsx": "a rename NAME validator (no separator, not a dot name)",
    "dashboard/src/lib/openTarget.ts": "the open-target parser refuses a dot or empty segment of a chip path",
}


def test_the_dashboard_compares_dotdot_in_the_two_named_sites_only():
    src = REPO_ROOT / "dashboard" / "src"
    hits: dict[str, int] = {}
    for p in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel:
            continue
        n = len(_TS_DOTDOT.findall(p.read_text(encoding="utf-8", errors="surrogateescape")))
        if n:
            hits[rel] = n
    assert set(hits) == set(_TS_ALLOWED), (hits, "add the site here with its reason, or drop the retired one")
